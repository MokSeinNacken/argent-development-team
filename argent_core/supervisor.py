"""Persistent supervisor loop & reconciliation (SPEC V2C).

This module adds a restart-proof supervisor on top of the deterministic Core /
SQLite ledger.  It owns:

- Schema V4 supervisor tables (``supervisor_jobs``, ``supervisor_actions``),
  persisted through the *same* ``Store`` connection as the Core (no second
  connection, no cross-connection transactions).
- A pluggable runtime adapter (``RunStatusProvider``) + detached launcher
  (``RunLauncher``) + workspace state provider.
- The central ``reconcile()`` decision table (§7.2 + amendments A7/A10).
- A crash-safe action journal (§8) and a local, interruptible loop (§9).

Trust boundary: the supervisor never trusts agent prose or events as authority;
it only reads the Core ledger, its own ledger, and allow-list-bound runtime
facts.  Agent output is UNTRUSTED DATA and is validated exclusively through
``Core.receive_agent_result()`` / ``outputs.validate_role_output``.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional, Protocol

from . import job_state, notifications, outputs, workflow
from . import model_router
from .core import ReceiveResult
from .notifications import NotificationStatus, NotificationType
from .resource_policy import RESOURCE_CLASS_VALUES, ResourceClass
from .resource_governor import AdmissionVerdict, ResourceReasonCode
from .execution_scope import SystemdRunScopeBackend, agent_spawn_env
from .scope_enforcer import ExecutionEnforcer
from .models import (
    AgentDispatch,
    ApprovalStatus,
    ArgentError,
    DispatchStatus,
    IdempotencyError,
    LeaseFencedError,
    NotFound,
    Role,
    TaskState,
)
from .store import Store, utcnow
from .workspace_broker import WorkspaceBroker
from .worktree import GitProvenanceProvider
from .process_registry import ProcessIdentity, ProcessIdentityProvider, ProcessRegistry
from .context import PROJECT_RULES, SECURITY_ARCH_RULES
from .context_pack import (
    CapabilityTier,
    ContextBuilder,
    ContextBuildError,
    ContextError,
    ContextPackRecord,
    FactInput,
    Importance,
    render_pack,
    validate_context_pack,
)
from .context_handoff_integration import build_pack_with_retrieval
from .retrieval import RetrievalRequest, RetrievalType
from . import handoff as handoff_mod
from . import checkpoint as checkpoint_mod

# ---------------------------------------------------------------------------
# Constants (SPEC V2C §9)
# ---------------------------------------------------------------------------

RUNNING_POLL_SECONDS = 2.0
BACKOFF_INITIAL_SECONDS = 1.0
BACKOFF_MULTIPLIER = 2.0
BACKOFF_MAX_SECONDS = 30.0
MAX_SNAPSHOT_RETRIES = 3
MAX_ACTION_RETRIES = 5
MAX_RUNTIME_UNKNOWN = 5
MISSING_BOUND_RUN_CONFIRMATIONS = 8
# Real OpenClaw runs (esp. Sol/Codex) can take 40-90s just to create the
# trajectory/session files after spawn.  The unbound-spawn budget must cover
# agent startup latency + a typical run, not seconds: 15 confirmations with
# backoff 1,2,4,8,16,30... ≈ 5 minutes before declaring the spawn unresolvable
# (E2E finding from the real recovery smoke).
MISSING_UNBOUND_SPAWN_CONFIRMATIONS = 15
MAX_DISPATCH_ATTEMPTS_PER_STEP = 3
AGENT_TIMEOUT_SECONDS = 900

# E3 (fix-round F2): bounded validity window for a provider-availability
# observation.  A persisted ``attempt_outcome == PROVIDER`` only marks a model
# UNAVAILABLE for this many seconds; after expiry (or a later AVAILABLE/SUCCESS
# observation) the model becomes eligible again — never poisoned forever.
AVAILABILITY_OBSERVATION_TTL_SECONDS = 1800

# Cross-controller apply fence (R14-F1, SPEC V2C §17 exactly-once write
# pre-effects): a bounded interprocess lock around the broker critical section.
APPLY_LOCK_TIMEOUT_SECONDS = 30.0
APPLY_LOCK_POLL_SECONDS = 0.05
APPLY_LOCK_DIRNAME = ".argent-supervisor-locks"

# Single source of truth for the role -> OpenClaw agent id map (A10).  The
# smoke E2E driver imports this exact map (no drift).
AGENT_IDS: dict[Role, str] = {
    Role.LEAD: "argent-lead",
    Role.ANALYST: "argent-analyst",
    Role.IMPLEMENTER: "argent-implementer",
    Role.QA: "argent-qa",
    Role.REVIEWER: "argent-reviewer",
}

# NOT_OBSERVED is a pure persistence sentinel (A10): it has no RunStatus
# counterpart and only ever lives in the supervisor_jobs.result_status column.
NOT_OBSERVED = "NOT_OBSERVED"


# ---------------------------------------------------------------------------
# Enums (SPEC V2C §5.1 / §3.2)
# ---------------------------------------------------------------------------

class RunStatus(str, Enum):
    NOT_FOUND = "NOT_FOUND"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"
    CONFLICT = "CONFLICT"


class SupervisorJobStatus(str, Enum):
    ACTIVE = "ACTIVE"
    WAITING_RUN = "WAITING_RUN"
    WAITING_GATE = "WAITING_GATE"
    BACKOFF = "BACKOFF"
    RECOVERING = "RECOVERING"
    ERROR = "ERROR"
    TERMINAL = "TERMINAL"


class RecoveryState(str, Enum):
    NONE = "NONE"
    DISCOVERING_RUN = "DISCOVERING_RUN"
    RESTORING_BINDING = "RESTORING_BINDING"
    CONSUMING_RESULT = "CONSUMING_RESULT"
    RETRYING_STEP = "RETRYING_STEP"
    AMBIGUOUS_WRITER = "AMBIGUOUS_WRITER"
    RUNTIME_UNKNOWN = "RUNTIME_UNKNOWN"
    CORE_RECOVERY_REQUIRED = "CORE_RECOVERY_REQUIRED"
    PERSISTENT_ERROR = "PERSISTENT_ERROR"


class ReconcileAction(str, Enum):
    NONE = "NONE"
    WAIT = "WAIT"
    START_ROLE = "START_ROLE"
    CREATE_DISPATCH = "CREATE_DISPATCH"
    SPAWN_RUN = "SPAWN_RUN"
    BIND_RUN = "BIND_RUN"
    APPLY_PATCH_SET = "APPLY_PATCH_SET"
    RUN_SANDBOX_TESTS = "RUN_SANDBOX_TESTS"
    RECORD_TEST_RESULT = "RECORD_TEST_RESULT"
    CONSUME_RESULT = "CONSUME_RESULT"
    MARK_RUN_FAILED = "MARK_RUN_FAILED"
    CORE_RECOVER = "CORE_RECOVER"
    PRESENT_OWNER_GATE = "PRESENT_OWNER_GATE"
    CLOSE_DONE = "CLOSE_DONE"
    CLOSE_FAILED = "CLOSE_FAILED"
    CLOSE_BLOCKED = "CLOSE_BLOCKED"
    PERSISTENT_ERROR = "PERSISTENT_ERROR"


# ---------------------------------------------------------------------------
# Dataclasses (SPEC V2C §5.1)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunLookup:
    dispatch_id: str
    agent_id: str
    expected_session_label: str  # "dispatch-<dispatch_id>"
    bound_session_id: Optional[str]
    bound_run_id: Optional[str]
    # Expected/bound runtime identity (persisted binding from the dispatch).
    # When set, EVERY start/terminal trajectory row must match exactly
    # (fail-closed).  ``None`` disables that specific comparison (only used by
    # unit tests that build a bare RunLookup); the supervisor always sets them.
    expected_provider: Optional[str] = None
    expected_model: Optional[str] = None
    expected_thinking_tier: Optional[str] = None


@dataclass(frozen=True)
class RunObservation:
    status: RunStatus
    agent_id: Optional[str]
    session_id: Optional[str]  # == sessionKey (A4)
    run_id: Optional[str]
    provider: Optional[str]
    model: Optional[str]
    thinking_tier: Optional[str]
    started_at: Optional[str]
    finished_at: Optional[str]
    result: Optional[dict]  # UNTRUSTED; never validated outside Core
    result_hash: Optional[str]
    authoritative_not_found: bool
    evidence_id: str
    error_code: Optional[str] = None
    #: Set (non-None) when ``_guarded_observe`` caught a structural adapter
    #: exception while interpreting untrusted runtime data and converted it
    #: into a fail-closed CONFLICT.  ``None`` for genuine provider CONFLICTs
    #: (e.g. a provenance mismatch), which keep their existing semantics.
    adapter_exception_type: Optional[str] = None


@dataclass(frozen=True)
class ReconcileDecision:
    job_id: str
    facts_version: int
    action: ReconcileAction
    reason: str
    dispatch_id: Optional[str] = None
    wake_at: Optional[str] = None
    # B1 fencing token (F1): the (owner, epoch) lease under which this decision
    # was authored.  ``None`` means an unleased legacy job (no fence applied at
    # action time).  A leased decision may only be executed while that exact
    # (owner, epoch) is still the current, unexpired holder.
    owner_instance_id: Optional[str] = None
    lease_epoch: Optional[int] = None


@dataclass(frozen=True)
class ActionOutcome:
    action: str
    status: str  # 'executed' | 'skipped' | 'already_succeeded' | 'failed' | 'noop'
    detail: Optional[str] = None
    dispatch_id: Optional[str] = None


@dataclass(frozen=True)
class SupervisorState:
    supervisor_job_id: str
    task_id: str
    status: str
    workflow_state: str
    expected_role: Optional[str]
    expected_dispatch_id: Optional[str]
    agent_id: Optional[str]
    session_id: Optional[str]
    run_id: Optional[str]
    attempt_no: int
    dispatch_status: Optional[str]
    result_status: str
    result_consumed: bool
    current_handoff_id: Optional[str]
    open_findings_count: int
    rework_cycle: int
    recovery_state: str
    owner_gate_id: Optional[str]
    gate_status: Optional[str]
    gate_scope: Optional[str]
    gate_closed: bool
    owner_prompted_at: Optional[str]
    next_action: str
    next_wake_at: Optional[str]
    retry_count: int
    missing_confirmations: int
    last_error_code: Optional[str]
    last_progress_at: str
    terminal: Optional[str]
    facts_version: int
    created_at: str
    updated_at: str
    # B1 durable queue / lease fields (operational projection + lease)
    primary_state: str
    queue_reason: str
    priority: int
    owner_instance_id: Optional[str]
    lease_epoch: int
    lease_expires_at: Optional[str]
    next_eligible_at: Optional[str]
    error_class: str
    wait_kind: str


# ---------------------------------------------------------------------------
# Adapter interfaces (SPEC V2C §5.2)
# ---------------------------------------------------------------------------

class RunStatusProvider(Protocol):
    def observe(self, lookup: RunLookup) -> RunObservation: ...


class RunLauncher(Protocol):
    def spawn(
        self, *, agent_id: str, dispatch_id: str,
        message_file: Path, timeout_seconds: int,
    ) -> Optional[int]: ...


class WorkspaceStateProvider(Protocol):
    def scoped_hash(self, scope_root: Path) -> str: ...
    def predicted_hash(self, scope_root: Path, patch_set: list) -> str: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _canonical_json(obj) -> str:
    """Canonical JSON used for hashes (identical to Core ``_hash_args``)."""
    return json.dumps(obj, sort_keys=True, default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def session_key_for(agent_id: str, dispatch_id: str) -> str:
    """Build the exact sessionKey used by the OpenClaw runtime (A4)."""
    return f"agent:{agent_id}:explicit:dispatch-{dispatch_id}"


def extract_balanced_json(text: str) -> dict:
    """Extract the first balanced JSON object from a text blob (lenient).

    Shared helper (SPEC V2C §6.2.5): the smoke E2E driver imports this exact
    implementation so parsing stays identical across the controller and the
    trajectory provider.
    """
    start = text.find("{")
    if start < 0:
        raise ValueError("no '{' found in agent reply")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("unbalanced JSON object in agent reply")


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(iso: str) -> float:
    """Parse an ISO-8601 timestamp into epoch seconds (UTC)."""
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).timestamp()


def _safe_parse_iso(ts) -> Optional[float]:
    """Parse an ISO timestamp fail-closed: any non-string or unparsable value
    returns ``None`` (no timestamp -> skip time filtering), never raises.

    Used at the untrusted runtime-data boundary (trajectory ``ts`` fields) where
    a JSON list/dict/number/bool/null can never be a valid ISO string.
    """
    if not isinstance(ts, str):
        return None
    try:
        return _parse_iso(ts)
    except (ValueError, TypeError):
        return None


def _ms_to_seconds(ms) -> float:
    return float(ms) / 1000.0


def _safe_ms_to_seconds(ts) -> Optional[float]:
    """Convert a message timestamp to seconds fail-closed: any non-numeric (or
    non-numeric-string) value returns ``None`` (ignore the timestamp filter),
    never raises.  Bools are excluded (``True`` is not a meaningful epoch ms).
    """
    if isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        return float(ts) / 1000.0
    if isinstance(ts, str):
        try:
            return float(ts) / 1000.0
        except ValueError:
            return None
    return None


def backoff_seconds(retry_count: int) -> float:
    """Deterministic, jitter-free backoff: ``min(1 * 2**retry, 30)`` (A10/§9)."""
    return min(BACKOFF_INITIAL_SECONDS * (BACKOFF_MULTIPLIER ** max(retry_count, 0)),
               BACKOFF_MAX_SECONDS)


def _is_implementer(role: Optional[Role]) -> bool:
    return role is Role.IMPLEMENTER


def _is_write_role(role: Optional[Role]) -> bool:
    return role in (Role.IMPLEMENTER, Role.QA)


# Legitimate write-role result extensions (SPEC V2C §8.3): the ONLY fields a
# write role may add beyond its standard envelope.  Any other top-level field
# (e.g. the forbidden ``encoded``) must be rejected fail-closed by
# ``outputs.validate_role_output``, never silently stripped (F1).
_WRITE_EXTENSIONS: dict[Role, tuple[str, ...]] = {
    Role.IMPLEMENTER: ("patch_set",),
    Role.QA: ("test_patch_set",),
}


def _write_envelope(role: Optional[Role], result: dict) -> dict:
    """Extract the standard envelope from a write-role result.

    Strips ONLY the role's legitimate patch extension so the complete write-
    result schema is validated (envelope + extension).  Everything else stays
    in the envelope so ``validate_role_output`` rejects forbidden/unknown
    fields (fail-closed), never silently dropping them.
    """
    extensions = _WRITE_EXTENSIONS.get(role, ())
    return {k: v for k, v in result.items() if k not in extensions}


# ---------------------------------------------------------------------------
# SupervisorStore
# ---------------------------------------------------------------------------

class SupervisorStore:
    """Persistent supervisor ledger over the same ``Store`` as the Core.

    Public queries are read-only; mutations use ``BEGIN IMMEDIATE``.  It may
    read Core tables in a snapshot but never mutates them (Core mutations run
    exclusively through ``Core``).
    """

    def __init__(
        self,
        store: Store,
        frontier_fn: Callable[[str], workflow.WorkflowFrontier],
    ):
        self._store = store
        self._frontier_fn = frontier_fn

    # -- read ---------------------------------------------------------------

    def get_job(self, job_id: str) -> Optional[SupervisorState]:
        row = self._store.get_supervisor_job(job_id)
        if row is None:
            return None
        return self._build_state(row)

    def get_job_for_task(self, task_id: str) -> Optional[SupervisorState]:
        row = self._store.get_supervisor_job_for_task(task_id)
        if row is None:
            return None
        return self._build_state(row)

    def list_nonterminal_jobs(self) -> list[SupervisorState]:
        rows = self._store.list_supervisor_jobs(nonterminal_only=True)
        return [self._build_state(r) for r in rows]

    def _job_row(self, job_id: str) -> Optional[dict]:
        return self._store.get_supervisor_job(job_id)

    # -- create (idempotent via deterministic id + command_idempotency, A6) --

    def create_job(
        self,
        task_id: str,
        *,
        idempotency_key: str,
        resource_class: Optional[str] = None,
    ) -> SupervisorState:
        job_id = "supervisor:" + task_id
        args_hash = _sha256(_canonical_json({"task_id": task_id}))
        rc = resource_class or ResourceClass.LIGHT.value
        if rc not in RESOURCE_CLASS_VALUES:
            raise ValueError(f"invalid resource_class {rc!r}")
        with self._store._transaction():
            existing = self._store.get_command_idempotency(
                idempotency_key, "create_supervisor_job"
            )
            if existing is not None:
                result_id, stored_hash = existing
                if stored_hash != args_hash:
                    raise IdempotencyError(
                        f"idempotency key {idempotency_key!r} reused for "
                        "create_supervisor_job with different arguments"
                    )
                row = self._store.get_supervisor_job(result_id)
                if row is None:
                    raise IdempotencyError(
                        f"idempotent replay: supervisor job {result_id!r} missing"
                    )
                return self._build_state(row)
            existing_job = self._store.get_supervisor_job_for_task(task_id)
            if existing_job is not None:
                self._store._set_command_idempotency(
                    idempotency_key, "create_supervisor_job",
                    existing_job["id"], args_hash, self._store.now_iso(),
                )
                return self._build_state(existing_job)
            task = self._store.get_task(task_id)
            if task is None:
                raise NotFound(f"task {task_id!r} not found")
            now = self._store.now_iso()
            row = {
                "id": job_id,
                "task_id": task_id,
                "status": SupervisorJobStatus.WAITING_RUN.value,
                "workflow_state": task.state.value,
                "expected_role": None,
                "expected_dispatch_id": None,
                "agent_id": None,
                "session_id": None,
                "run_id": None,
                "attempt_no": 0,
                "dispatch_status": None,
                "result_status": NOT_OBSERVED,
                "result_consumed": 0,
                "current_handoff_id": None,
                "open_findings_count": 0,
                "rework_cycle": 1,
                "recovery_state": RecoveryState.NONE.value,
                "owner_gate_id": None,
                "gate_status": None,
                "gate_scope": None,
                "gate_closed": 0,
                "owner_prompted_at": None,
                "owner_prompted_gate_id": None,
                "next_action": ReconcileAction.NONE.value,
                "next_wake_at": None,
                "retry_count": 0,
                "missing_confirmations": 0,
                "last_error_code": None,
                "last_progress_at": now,
                "terminal": None,
                "facts_version": 0,
                "primary_state": job_state.PrimaryState.QUEUED.value,
                "queue_reason": job_state.QueueReason.NEW.value,
                "priority": 0,
                "owner_instance_id": None,
                "lease_epoch": 0,
                "lease_expires_at": None,
                "next_eligible_at": None,
                "error_class": job_state.ErrorClass.NONE.value,
                "wait_kind": job_state.WaitKind.NONE.value,
                "canonical_worktree_path": None,
                "repo_identity": None,
                "base_commit": None,
                "branch_identity": None,
                "writer_dispatch_id": None,
                "writer_owner_instance_id": None,
                "writer_lease_epoch": 0,
                "writer_binding_mode": None,
                "expected_head": None,
                "current_head": None,
                "resource_class": rc,
                "last_resource_decision": None,
                "last_resource_reason_code": None,
                "last_resource_snapshot_hash": None,
                "last_resource_at": None,
                "created_at": now,
                "updated_at": now,
            }
            self._store._insert_supervisor_job(row)
            self._store._set_command_idempotency(
                idempotency_key, "create_supervisor_job", job_id, args_hash, now,
            )
            return self._build_state(row)

    # -- B1 durable queue / lease facade (delegates to Store primitives) ---

    def claim_job(
        self, job_id: str, *, owner_instance_id: str, ttl_seconds: int
    ) -> dict:
        return self._store.claim_job(
            job_id, owner_instance_id=owner_instance_id, ttl_seconds=ttl_seconds
        )

    def claim_next_job(self, *, owner_instance_id: str, ttl_seconds: int):
        return self._store.claim_next_job(
            owner_instance_id=owner_instance_id, ttl_seconds=ttl_seconds
        )

    def renew_lease(
        self,
        job_id: str,
        *,
        owner_instance_id: str,
        lease_epoch: int,
        ttl_seconds: int,
    ) -> dict:
        return self._store.renew_lease(
            job_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            ttl_seconds=ttl_seconds,
        )

    def release_lease(
        self, job_id: str, *, owner_instance_id: str, lease_epoch: int
    ) -> dict:
        return self._store.release_lease(
            job_id, owner_instance_id=owner_instance_id, lease_epoch=lease_epoch
        )

    def clear_lease(
        self, job_id: str, *, owner_instance_id: str, lease_epoch: int
    ) -> dict:
        return self._store.clear_lease(
            job_id, owner_instance_id=owner_instance_id, lease_epoch=lease_epoch
        )

    def quarantine_lost(
        self, job_id: str, *, error_code: str = "AMBIGUOUS_WRITER",
        expected: Optional[dict] = None,
    ) -> Optional[dict]:
        return self._store.quarantine_lost(
            job_id, error_code=error_code, expected=expected,
        )

    def quarantine_blocked(
        self, job_id: str, *, error_code: str = "WORKTREE_DIVERGED",
        error_class: str = "OWNER_REQUIRED", expected: Optional[dict] = None,
    ) -> Optional[dict]:
        return self._store.quarantine_blocked(
            job_id, error_code=error_code, error_class=error_class,
            expected=expected,
        )

    def recover_takeover_job(
        self,
        job_id: str,
        *,
        expected: dict,
        owner_instance_id: str,
        ttl_seconds: int,
        process_alive: bool = False,
        worktree_verdict: Optional[str] = None,
    ) -> dict:
        return self._store.recover_takeover_job(
            job_id,
            expected=expected,
            owner_instance_id=owner_instance_id,
            ttl_seconds=ttl_seconds,
            process_alive=process_alive,
            worktree_verdict=worktree_verdict,
        )

    def enqueue_job(self, job_id: str, **kwargs) -> dict:
        return self._store.enqueue_job(job_id, **kwargs)

    def persist_resource_decision(self, job_id: str, **kwargs) -> dict:
        return self._store.persist_resource_decision(job_id, **kwargs)

    def commit_recovery_decision(self, job_id: str, **kwargs) -> dict:
        return self._store.commit_recovery_decision(job_id, **kwargs)

    def has_recovery_marker(self, job_id: str, process_id: str) -> bool:
        return self._store.has_recovery_marker(job_id, process_id)

    def lease_is_current(
        self, job_id: str, owner_instance_id: str, lease_epoch: int
    ) -> bool:
        return self._store.lease_is_current(job_id, owner_instance_id, lease_epoch)

    def assert_lease_current(
        self, job_id: str, owner_instance_id: str, lease_epoch: int
    ) -> None:
        self._store.assert_lease_current(job_id, owner_instance_id, lease_epoch)

    # -- projection ---------------------------------------------------------

    def _current_dispatch(self, task_id: str) -> Optional[AgentDispatch]:
        """The single active dispatch (PENDING/RUNNING/RECOVERY_PENDING)."""
        for d in self._store.list_dispatches(task_id):
            if d.status in (
                DispatchStatus.PENDING,
                DispatchStatus.RUNNING,
                DispatchStatus.RECOVERY_PENDING,
            ):
                return d
        return None

    def _dispatch_at_frontier(
        self, task_id: str, frontier: workflow.WorkflowFrontier
    ) -> Optional[AgentDispatch]:
        """Latest (max attempt) dispatch at the frontier position, if any."""
        matches = [
            d
            for d in self._store.list_dispatches(task_id)
            if d.cycle_no == frontier.cycle_no and d.position == frontier.position
        ]
        if not matches:
            return None
        return max(matches, key=lambda d: d.attempt_no)

    def _max_cycle(self, task_id: str) -> int:
        dispatches = self._store.list_dispatches(task_id)
        if not dispatches:
            return 1
        return max(d.cycle_no for d in dispatches)

    def _current_gate(self, task_id: str):
        approvals = self._store.list_approvals(task_id)
        active = [
            a for a in approvals
            if a.status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)
        ]
        if active:
            return active[0], len(active)
        if approvals:
            # reflect the most recently created gate (closed reflection).
            return approvals[-1], 0
        return None, 0

    def _build_state(self, row: dict) -> SupervisorState:
        task_id = row["task_id"]
        task = self._store.get_task(task_id)
        frontier = self._frontier_fn(task_id) if task is not None else None

        expected_role = frontier.expected_role if frontier else None
        active = self._current_dispatch(task_id)
        at_frontier = self._dispatch_at_frontier(task_id, frontier) if frontier else None
        disp = active if active is not None else at_frontier

        gate, _n = self._current_gate(task_id)

        agent_id = AGENT_IDS.get(expected_role) if expected_role else None

        open_findings = sum(
            1 for f in self._store.list_findings(task_id)
            if f.status.value == "open"
        )

        result_consumed = bool(
            disp is not None
            and disp.status is DispatchStatus.CONSUMED
            and disp.consumed_at is not None
        )

        return SupervisorState(
            supervisor_job_id=row["id"],
            task_id=task_id,
            status=row["status"],
            workflow_state=task.state.value if task else row["workflow_state"],
            expected_role=expected_role.value if expected_role else None,
            expected_dispatch_id=disp.id if disp is not None else None,
            agent_id=agent_id,
            session_id=disp.child_session_id if disp is not None else None,
            run_id=disp.openclaw_run_id if disp is not None else None,
            attempt_no=disp.attempt_no if disp is not None else 0,
            dispatch_status=disp.status.value if disp is not None else None,
            result_status=row["result_status"],
            result_consumed=result_consumed,
            current_handoff_id=(
                self._store.get_latest_handoff(task_id).id
                if self._store.get_latest_handoff(task_id) is not None else None
            ),
            open_findings_count=open_findings,
            rework_cycle=self._max_cycle(task_id),
            recovery_state=row["recovery_state"],
            owner_gate_id=gate.id if gate is not None else None,
            gate_status=gate.status.value if gate is not None else None,
            gate_scope=gate.scope if gate is not None else None,
            gate_closed=bool(gate is not None and gate.closed_at is not None),
            owner_prompted_at=row["owner_prompted_at"],
            next_action=row["next_action"],
            next_wake_at=row["next_wake_at"],
            retry_count=row["retry_count"],
            missing_confirmations=row["missing_confirmations"],
            last_error_code=row["last_error_code"],
            last_progress_at=row["last_progress_at"],
            terminal=row["terminal"],
            facts_version=row["facts_version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            primary_state=row["primary_state"],
            queue_reason=row["queue_reason"],
            priority=row["priority"],
            owner_instance_id=row["owner_instance_id"],
            lease_epoch=row["lease_epoch"],
            lease_expires_at=row["lease_expires_at"],
            next_eligible_at=row["next_eligible_at"],
            error_class=row["error_class"],
            wait_kind=row["wait_kind"],
        )


# ---------------------------------------------------------------------------
# TrajectoryRunStatusProvider (SPEC V2C §6.2, A1/A4/A10)
# ---------------------------------------------------------------------------

class TrajectoryRunStatusProvider:
    """Read-only, allow-list bound runtime adapter over OpenClaw trajectories.

    Reads exactly ``~/.openclaw/agents/<agent_id>/sessions/dispatch-<id>
    .trajectory.jsonl`` (plus the strictly validated ``sessionFile``).  No free
    path is taken from agent output; no command is ever executed from the
    trajectory content (it is external DATA).
    """

    def __init__(self, state_dir: Optional[Path] = None):
        self._state_dir = Path(state_dir) if state_dir is not None \
            else Path.home() / ".openclaw"

    def _trajectory_path(self, agent_id: str, dispatch_id: str) -> Path:
        return (
            self._state_dir / "agents" / agent_id / "sessions"
            / f"dispatch-{dispatch_id}.trajectory.jsonl"
        )

    def _session_dir(self, agent_id: str) -> Path:
        return self._state_dir / "agents" / agent_id / "sessions"

    def observe(self, lookup: RunLookup) -> RunObservation:
        agent_id = lookup.agent_id
        traj = self._trajectory_path(agent_id, lookup.dispatch_id)
        session_dir = self._session_dir(agent_id)

        # Authoritative NOT_FOUND requires the session dir to be readable.
        if not session_dir.is_dir():
            return self._unknown(lookup, "session_dir_missing")
        if not traj.exists():
            # The trajectory file is flushed late by the runtime (real runs
            # only write it at/near completion).  An existing session file or
            # its lock proves the agent process started and the run is ACTIVE
            # - that is RUNNING, never NOT_FOUND (E2E finding from the real
            # recovery smoke: slow runs must not burn the missing budget).
            sess = session_dir / f"dispatch-{lookup.dispatch_id}.jsonl"
            lock = session_dir / f"dispatch-{lookup.dispatch_id}.jsonl.lock"
            if sess.exists() or lock.exists():
                return RunObservation(
                    status=RunStatus.RUNNING,
                    agent_id=agent_id,
                    session_id=session_key_for(agent_id, lookup.dispatch_id),
                    run_id=None,
                    provider=None,
                    model=None,
                    thinking_tier=None,
                    started_at=None,
                    finished_at=None,
                    result=None,
                    result_hash=None,
                    authoritative_not_found=False,
                    evidence_id=f"active_session:{sess.name}",
                )
            return RunObservation(
                status=RunStatus.NOT_FOUND,
                agent_id=agent_id,
                session_id=None,
                run_id=None,
                provider=None,
                model=None,
                thinking_tier=None,
                started_at=None,
                finished_at=None,
                result=None,
                result_hash=None,
                authoritative_not_found=True,
                evidence_id=f"missing:{traj}",
            )

        lines: list[dict] = []
        try:
            raw = traj.read_text(encoding="utf-8")
        except OSError:
            return self._unknown(lookup, "trajectory_io_error")
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # A trailing partial line or malformed JSONL -> UNKNOWN (never
                # guess success).
                return self._unknown(lookup, "malformed_jsonl")
            # F-R4: every decoded row MUST be a JSON object.  A scalar/list/null
            # top-level row is structurally malformed and cannot be interpreted
            # as start/terminal/metadata -> deterministic UNKNOWN (never raise,
            # never treated as a valid run).
            if not isinstance(obj, dict):
                return self._unknown(lookup, "malformed_row")
            lines.append(obj)

        started = [o for o in lines if o.get("type") == "session.started"]
        ended = [o for o in lines if o.get("type") == "session.ended"]
        metadata = [o for o in lines if o.get("type") == "trace.metadata"]

        if not started:
            return self._unknown(lookup, "no_session_started")

        expected_session_key = session_key_for(agent_id, lookup.dispatch_id)

        # F4: validate EVERY session.started row against the exact
        # expected/bound tuple.  Any foreign sessionId label, missing/empty
        # runId, wrong sessionKey/agentId, or a distinct runId is CONFLICT
        # (never silently filter a foreign start just because the lookup is
        # bound to a different run).
        run_ids: list = []
        providers: list = []
        models: list = []
        for o in started:
            if o.get("sessionId") != lookup.expected_session_label:
                return self._conflict(lookup, "start_session_label_mismatch")
            rid = o.get("runId")
            if not rid:
                return self._conflict(lookup, "start_missing_run_id")
            # F-R4 fail-closed: a PRESENT non-string ``runId`` (JSON list,
            # dict, number, bool, ...) is structurally malformed identity data.
            # It can never be a bindable run id and must never reach the
            # ``set(run_ids)`` dedupe (unhashable list -> TypeError).  A
            # deterministic CONFLICT, never raise.
            if not isinstance(rid, str):
                return self._conflict(lookup, "start_run_id_not_string")
            if o.get("sessionKey") != expected_session_key:
                return self._conflict(lookup, "start_session_key_mismatch")
            adata = o.get("data")
            # F-R5: distinguish field ABSENCE from PRESENCE.  An explicitly
            # PRESENT non-object ``data`` (JSON null, bool, list, number,
            # string) is a malformed start identity row -> CONFLICT (never
            # RUNNING/SUCCEEDED).  An ABSENT ``data`` keeps the existing
            # missing-agent-id semantics (start_missing_agent_id).
            if "data" in o and not isinstance(o["data"], dict):
                return self._conflict(lookup, "start_malformed_data")
            if adata is None:
                adata = {}
            # F1: agent identity is a REQUIRED bound field on every start row;
            # a missing ``data.agentId`` is a CONFLICT (never silently accepted).
            if "agentId" not in adata:
                return self._conflict(lookup, "start_missing_agent_id")
            if adata.get("agentId") != agent_id:
                return self._conflict(lookup, "start_agent_id_mismatch")
            # F1: provider/model must match the persisted expected/bound tuple
            # (fail-closed on any mismatch, including a missing field).
            if (lookup.expected_provider is not None
                    and o.get("provider") != lookup.expected_provider):
                return self._conflict(lookup, "start_provider_binding_mismatch")
            if (lookup.expected_model is not None
                    and o.get("modelId") != lookup.expected_model):
                return self._conflict(lookup, "start_model_binding_mismatch")
            run_ids.append(rid)
            providers.append(o.get("provider"))
            models.append(o.get("modelId"))

        distinct_run_ids = set(run_ids)
        if len(distinct_run_ids) > 1:
            return self._conflict(lookup, "multiple_run_ids")

        run_id = next(iter(distinct_run_ids))
        if lookup.bound_run_id is not None and run_id != lookup.bound_run_id:
            return self._conflict(lookup, "foreign_run_id")
        if (lookup.bound_session_id is not None
                and expected_session_key != lookup.bound_session_id):
            return self._conflict(lookup, "foreign_bound_session_id")

        # Every start must agree on provider/model (F4: duplicate starts
        # sharing one runId but disagreeing on provider/model are CONFLICT).
        s = started[0]
        provider = providers[0]
        model = models[0]
        if any(p != provider for p in providers):
            return self._conflict(lookup, "start_provider_mismatch")
        if any(m != model for m in models):
            return self._conflict(lookup, "start_model_mismatch")
        session_key = expected_session_key

        # F-R5: a structurally malformed metadata row (PRESENT non-object
        # ``data`` or PRESENT non-object ``data.model``) is uninterpretable
        # corruption -> CONFLICT (never raise, never guess a tier, never
        # SUCCEEDED).  Missing data/model is NOT malformed.
        for m in metadata:
            if self._metadata_malformed(m):
                return self._conflict(lookup, "metadata_malformed")

        # Thinking tier: trace.metadata.data.model.thinkLevel, else the C1 rule
        # for OpenAI (lead/reviewer) which has no trace.metadata line (A1).
        thinking = self._thinking_tier(metadata, run_id, provider, model, agent_id)
        # F1: the observed thinking tier must match the persisted expected/bound
        # tier (fail-closed).  The C1 rule (A1) guarantees lead/reviewer always
        # yields 'high' (matching the dispatch); DeepSeek roles without a
        # trace.metadata line stay None and must NOT be reported as CONFLICT for
        # a missing metadata line alone (SPEC §6.2.3: CONFLICT only on
        # provider/model contradiction).  A DETERMINED foreign tier is still
        # rejected.
        if (lookup.expected_thinking_tier is not None
                and thinking is not None
                and thinking != lookup.expected_thinking_tier):
            return self._conflict(lookup, "thinking_tier_binding_mismatch")

        started_at = s.get("ts")

        # F4: terminal rows must match the exact runId, sessionId label,
        # sessionKey, agentId, provider AND model of the single observed run;
        # ANY mismatch (including a different runId) -> CONFLICT (never
        # silently skipped, never SUCCEEDED).
        matching_ended = []
        for e in ended:
            erid = e.get("runId")
            # F-R4 fail-closed: a PRESENT non-string (or empty) terminal
            # ``runId`` is structurally malformed identity data -> CONFLICT
            # (never raise, never SUCCEEDED).  A string that merely differs
            # from the observed start runId keeps the existing mismatch code.
            if not isinstance(erid, str) or not erid:
                return self._conflict(lookup, "terminal_run_id_not_string")
            if erid != run_id:
                return self._conflict(lookup, "terminal_run_id_mismatch")
            if e.get("sessionId") != lookup.expected_session_label:
                return self._conflict(lookup, "terminal_session_label_mismatch")
            if e.get("sessionKey") != expected_session_key:
                return self._conflict(lookup, "terminal_session_key_mismatch")
            edata = e.get("data")
            # F-R5: PRESENT non-object ``data`` (JSON null, bool, list,
            # number, string) is malformed -> CONFLICT (never SUCCEEDED).
            # ABSENT ``data`` keeps the existing missing-agent-id semantics.
            if "data" in e and not isinstance(e["data"], dict):
                return self._conflict(lookup, "terminal_malformed_data")
            if edata is None:
                edata = {}
            # F1: agent identity is a REQUIRED bound field on every terminal row.
            if "agentId" not in edata:
                return self._conflict(lookup, "terminal_missing_agent_id")
            if edata.get("agentId") != agent_id:
                return self._conflict(lookup, "terminal_agent_id_mismatch")
            if e.get("provider") != provider:
                return self._conflict(lookup, "terminal_provider_mismatch")
            if e.get("modelId") != model:
                return self._conflict(lookup, "terminal_model_mismatch")
            # F1: terminal provider/model must match the persisted expected/bound
            # tuple as well (a consistently-foreign start+terminal tuple must not
            # slip through the row-to-row checks).
            if (lookup.expected_provider is not None
                    and e.get("provider") != lookup.expected_provider):
                return self._conflict(lookup, "terminal_provider_binding_mismatch")
            if (lookup.expected_model is not None
                    and e.get("modelId") != lookup.expected_model):
                return self._conflict(lookup, "terminal_model_binding_mismatch")
            matching_ended.append(e)

        # Conflict: several different terminal lines / runIds.
        terminal_states = set()
        for e in matching_ended:
            terminal_states.add(self._terminal_status(e))

        if len(matching_ended) == 0:
            status = RunStatus.RUNNING
            finished_at = None
        elif len(matching_ended) == 1:
            status = self._terminal_status(matching_ended[0])
            finished_at = matching_ended[0].get("ts")
        else:
            # Multiple session.ended lines -> CONFLICT unless all identical.
            if len(terminal_states) == 1:
                status = next(iter(terminal_states))
                finished_at = matching_ended[-1].get("ts")
            else:
                return self._conflict(lookup, "conflicting_terminal")

        result, result_hash = self._extract_result(
            s, finished_at, session_dir
        )

        # F2 (E3 fix-round): thread a bounded provider-side error code from the
        # terminal row into the observation, so a provider failure can persist
        # ATTEMPT_OUTCOME_PROVIDER (never CAPABILITY) in the real path.
        error_code = None
        if matching_ended:
            error_code = self._terminal_error_code(matching_ended[-1])

        return RunObservation(
            status=status,
            agent_id=agent_id,
            session_id=session_key,
            run_id=run_id,
            provider=provider,
            model=model,
            thinking_tier=thinking,
            started_at=started_at,
            finished_at=finished_at,
            result=result,
            result_hash=result_hash,
            authoritative_not_found=False,
            evidence_id=str(traj),
            error_code=error_code,
        )

    def _terminal_error_code(self, e: dict) -> Optional[str]:
        """Bounded provider-side error code from a terminal row (F2, E3).

        Reads ``data.errorCode`` (a non-empty string).  Anything else (missing,
        non-string, non-dict ``data``) returns ``None`` — an error code is never
        fabricated from malformed runtime data.
        """
        data = e.get("data")
        if not isinstance(data, dict):
            return None
        code = data.get("errorCode")
        if not isinstance(code, str) or not code.strip():
            return None
        return code.strip()

    def _terminal_status(self, e: dict) -> RunStatus:
        data = e.get("data")
        if not isinstance(data, dict):
            # F-R4: structurally malformed terminal row -> UNKNOWN (never
            # raise, never SUCCEEDED).  Defensive; observe() rejects these
            # rows as CONFLICT before reaching here.
            return RunStatus.UNKNOWN
        if data.get("status") == "success":
            return RunStatus.SUCCEEDED
        if data.get("aborted") or data.get("externalAbort"):
            return RunStatus.CANCELLED
        if data.get("timedOut"):
            return RunStatus.FAILED
        if data.get("status") in ("failed", "error", "timeout", "cancel"):
            return RunStatus.CANCELLED if data.get("status") == "cancel" else RunStatus.FAILED
        return RunStatus.UNKNOWN

    @staticmethod
    def _metadata_malformed(m: dict) -> bool:
        """True when a ``trace.metadata`` row is structurally malformed:
        a PRESENT non-object ``data`` (JSON null, bool, list, number,
        string) or a PRESENT non-object ``data.model``.  A MISSING
        ``data``/``model`` is NOT malformed (treated as "no thinkLevel")."""
        if "data" in m and not isinstance(m["data"], dict):
            return True
        data = m.get("data")
        if isinstance(data, dict) and "model" in data \
                and not isinstance(data["model"], dict):
            return True
        return False

    def _thinking_tier(
        self, metadata, run_id, provider, model, agent_id
    ) -> Optional[str]:
        for m in metadata:
            # F-R4: malformed/non-object metadata rows are uninterpretable ->
            # skip (return None), never raise.
            if not isinstance(m, dict) or self._metadata_malformed(m):
                continue
            if m.get("runId") != run_id:
                continue
            data = m.get("data")
            model_data = data.get("model") if isinstance(data, dict) else None
            if not isinstance(model_data, dict):
                continue
            if "thinkLevel" in model_data and model_data["thinkLevel"]:
                return model_data["thinkLevel"]
        # C1 rule (A1): OpenAI lead/reviewer have no trace.metadata; the
        # canonical tuple implies the expected tier 'high'.  DeepSeek roles
        # with a missing line stay UNKNOWN (never guessed).
        if provider == "openai" and model == "gpt-5.6-sol":
            return "high"
        return None

    def _extract_result(self, started, finished_at, session_dir: Path):
        data = started.get("data") or {}
        session_file = data.get("sessionFile")
        if not session_file:
            return None, None
        # sessionFile is untrusted data: only a non-empty string is a usable
        # path label (never raise on list/dict/number/bool/null).
        if not isinstance(session_file, str):
            return None, None
        # Strict canonical path validation (A10 / §6.2.5): only under the
        # expected agent session directory.
        try:
            sp = Path(session_file).resolve()
        except OSError:
            return None, None
        if not self._within(session_dir.resolve(), sp):
            return None, None
        if not sp.is_file():
            return None, None
        try:
            raw = sp.read_text(encoding="utf-8")
        except OSError:
            return None, None

        # Timestamps are untrusted data: a non-string/non-ISO ``ts`` (JSON
        # list/dict/number/bool/null) must NOT raise; treat it as "no
        # timestamp" and skip the time filter (never fail the extraction).
        start_s = _safe_parse_iso(started.get("ts"))
        end_s = _safe_parse_iso(finished_at)

        assistant_texts = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            # F-R4 structural guard (same as the trajectory decode loop): every
            # decoded row MUST be a JSON object.  A top-level ``null`` / scalar
            # / list row is uninterpretable -> skip (never raise).
            if not isinstance(obj, dict):
                continue
            if obj.get("type") != "message":
                continue
            msg = obj.get("message")
            # ``message`` must be an object; a scalar/list/null message is
            # uninterpretable -> skip (never raise).
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "assistant":
                continue
            ts = msg.get("timestamp")
            if ts is not None:
                ts_s = _safe_ms_to_seconds(ts)
                if ts_s is not None:
                    if start_s is not None and ts_s < start_s:
                        continue
                    if end_s is not None and ts_s > end_s:
                        continue
            text = self._assistant_text(msg)
            if text is not None:
                assistant_texts.append(text)
        if not assistant_texts:
            return None, None
                # The FINAL assistant message is the agent's reply.  Intermediate
        # assistant messages (status cards, partial JSON, tool summaries)
        # must never be used: the first JSON object of a concatenation could
        # come from such an intermediate message and carry a foreign task_id
        # (real-E2E finding: consume was rejected with task_mismatch).
        if not assistant_texts:
            return None, None
        last = assistant_texts[-1]
        try:
            result = extract_balanced_json(last)
        except (ValueError, json.JSONDecodeError):
            return None, None
        return result, _sha256(_canonical_json(result))

    @staticmethod
    def _assistant_text(msg: dict) -> Optional[str]:
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                b.get("text")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
            ]
            if parts:
                return "\n".join(parts)
        return None

    @staticmethod
    def _within(root: Path, path: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _unknown(self, lookup: RunLookup, code: str) -> RunObservation:
        return RunObservation(
            status=RunStatus.UNKNOWN,
            agent_id=lookup.agent_id,
            session_id=None,
            run_id=None,
            provider=None,
            model=None,
            thinking_tier=None,
            started_at=None,
            finished_at=None,
            result=None,
            result_hash=None,
            authoritative_not_found=False,
            evidence_id=code,
            error_code=code,
        )

    def _conflict(self, lookup: RunLookup, code: str) -> RunObservation:
        return RunObservation(
            status=RunStatus.CONFLICT,
            agent_id=lookup.agent_id,
            session_id=None,
            run_id=None,
            provider=None,
            model=None,
            thinking_tier=None,
            started_at=None,
            finished_at=None,
            result=None,
            result_hash=None,
            authoritative_not_found=False,
            evidence_id=code,
            error_code=code,
        )


# ---------------------------------------------------------------------------
# RunLauncher (SPEC V2C §5.2 / §8.2 / A8)
# ---------------------------------------------------------------------------


def build_agent_command(
    *, agent_id: str, dispatch_id: str, message_file: Path, timeout_seconds: int,
) -> list:
    """Build the exact openclaw-agent argv (single source of truth).

    Used by both :meth:`OpenClawRunLauncher.spawn` (legacy detached path) and the
    C2 :class:`argent_core.scope_enforcer.ExecutionEnforcer` (scoped path), so the
    two paths can never drift in the command they launch.
    """
    return [
        "openclaw", "agent",
        "--agent", agent_id,
        "--session-id", f"dispatch-{dispatch_id}",
        "--message-file", str(message_file),
        "--json",
        "--timeout", str(timeout_seconds),
    ]


class OpenClawRunLauncher:
    """Launches role agents DETACHED (``start_new_session=True``).

    The agent process survives a supervisor SIGKILL (prerequisite for the §13
    recovery smoke).  Returns immediately; binding/result are discovered later
    exclusively through the ``RunStatusProvider``.

    If ``counter_path`` is given, a persistent per-dispatch launch counter is
    incremented BEFORE the spawn so the actual launch invocation count survives
    an abrupt supervisor SIGKILL (F8: independent no-double-spawn proof).
    """

    def __init__(self, counter_path: Optional[Path] = None):
        self._counter_path = Path(counter_path) if counter_path is not None else None

    @staticmethod
    def _read_counter_file(counter_path: Optional[Path]) -> dict:
        if counter_path is None:
            return {}
        try:
            raw = counter_path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _write_counter_file(counter_path: Optional[Path], data: dict) -> None:
        if counter_path is None:
            return
        counter_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = counter_path.with_name(counter_path.name + ".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(tmp, counter_path)

    def spawn(
        self, *, agent_id: str, dispatch_id: str,
        message_file: Path, timeout_seconds: int,
    ) -> Optional[int]:
        # F8: increment the persistent launch counter BEFORE the detached spawn
        # so the count is durable even if the supervisor is SIGKILLed right
        # after the launcher returns (the independent no-double-spawn proof).
        self.increment_counter(dispatch_id)
        cmd = build_agent_command(
            agent_id=agent_id, dispatch_id=dispatch_id,
            message_file=message_file, timeout_seconds=timeout_seconds,
        )
        popen = subprocess.Popen(
            cmd,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            # G1 (F4): a minimal allowlisted environment — never inherit the
            # supervisor's evidence-MAC key / key-file path.
            env=agent_spawn_env(),
        )
        # B3: the trusted spawn path returns the child PID so the supervisor
        # can register process evidence (boot_id + pid + start_ticks).  A
        # None/placeholder Popen (test seam) yields None.
        return popen.pid if popen is not None else None

    def increment_counter(self, dispatch_id: str) -> None:
        """Persist the launch count for ``dispatch_id`` BEFORE a spawn (F8)."""
        if self._counter_path is not None:
            data = self._read_counter_file(self._counter_path)
            data[dispatch_id] = int(data.get(dispatch_id, 0)) + 1
            self._write_counter_file(self._counter_path, data)


def read_launch_counter(counter_path) -> dict:
    """Read a persistent launcher invocation counter file (F8)."""
    if counter_path is None:
        return {}
    return OpenClawRunLauncher._read_counter_file(Path(counter_path))


# ---------------------------------------------------------------------------
# Workspace state provider (real, hash based) — §5.2 / §8.3
# ---------------------------------------------------------------------------

class WorkspaceHashProvider:
    """Deterministic scope hash for broker crash reconciliation (§8.3)."""

    def scoped_hash(self, scope_root: Path) -> str:
        parts = []
        root = Path(scope_root).resolve()
        for p in sorted(root.rglob("*")):
            if p.is_file():
                rel = p.relative_to(root)
                parts.append(f"{rel}:{_sha256(p.read_bytes().hex())}")
        return _sha256("\n".join(parts))

    def predicted_hash(self, scope_root: Path, patch_set: list) -> str:
        # F2 §8.3: deterministic expected after-hash computed from the full
        # patch set (matches the broker's write/delete effect on plain paths).
        # Used for crash reconciliation: current scope hash == effect_hash means
        # the patch set was already applied (exactly-once).
        root = Path(scope_root).resolve()
        files: dict[str, bytes] = {}
        for p in sorted(root.rglob("*")):
            if p.is_file():
                files[str(p.relative_to(root))] = p.read_bytes()
        for patch in patch_set:
            if not isinstance(patch, dict):
                continue
            op = patch.get("op")
            path = patch.get("path")
            if not isinstance(path, str) or os.path.isabs(path):
                continue
            key = os.path.normpath(path)
            if op == "write":
                raw = patch.get("content")
                if isinstance(raw, str):
                    try:
                        files[key] = base64.b64decode(raw, validate=True)
                    except (binascii.Error, ValueError):
                        continue
            elif op == "delete":
                files.pop(key, None)
        parts = [
            f"{rel}:{_sha256(content.hex())}"
            for rel, content in sorted(files.items())
        ]
        return _sha256("\n".join(parts))


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------

@dataclass
class _Snapshot:
    job: dict
    task: object
    frontier: Optional[workflow.WorkflowFrontier]
    active_dispatch: Optional[AgentDispatch]
    dispatch_at_frontier: Optional[AgentDispatch]
    max_attempt_at_frontier: int
    active_role_run: Optional[object]
    gate: Optional[object]
    open_gates_count: int
    open_findings_count: int
    latest_handoff_id: Optional[str]
    action_journal_fingerprint: tuple


@dataclass
class _Plan:
    action: ReconcileAction
    reason: str
    dispatch_id: Optional[str] = None
    wake_at: Optional[str] = None
    status: Optional[str] = None
    recovery_state: Optional[str] = None
    retry_count: Optional[int] = None
    missing_confirmations: Optional[int] = None
    last_error_code: Optional[str] = None
    terminal: Optional[str] = None


_RETRY = object()  # sentinel: snapshot changed, retry


class Supervisor:
    """Persistent, restart-proof supervisor over a Core instance."""

    def __init__(
        self,
        core,
        run_status_provider: RunStatusProvider,
        run_launcher: Optional[RunLauncher] = None,
        *,
        controller_source: str = "role:lead",
        owner_source: str = "owner:authenticated",
        workspace_root: Optional[Path] = None,
        workspace_state_provider: Optional[WorkspaceStateProvider] = None,
        run_tests_fn: Optional[Callable] = None,
        broker_factory: Optional[Callable[[], WorkspaceBroker]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        process_registry: Optional["ProcessRegistry"] = None,
        process_identity_provider: Optional["ProcessIdentityProvider"] = None,
        git_provenance_provider: Optional["GitProvenanceProvider"] = None,
        # C1: resource governor + host-snapshot provider injection (tests pass
        # deterministic fakes; defaults are created lazily by the Scheduler).
        resource_governor=None,
        snapshot_provider=None,
        # C2: execution enforcement (scope backend + enforcer).  Default ``None``
        # wires the PRODUCTION default (real ``SystemdRunScopeBackend``) in
        # ``__init__`` — enforcement is mandatory (F1, no opt-in); tests inject
        # a fake backend/enforcer and the Scheduler may also wire one.
        enforcer=None,
        scope_backend=None,
        # D1: optional Context Builder injection (tests pass a deterministic
        # fake; default = the real pure ContextBuilder).  Build failures are
        # fail-closed (no dispatch), never a legacy fallback.
        context_builder=None,
        # D2: optional retrieval / checkpoint / handoff injections (tests pass
        # deterministic fakes; default None = the D1-only path, unchanged).
        retriever=None,
        checkpoint_store=None,
        handoff_builder=None,
        # E2: optional router injection (tests pass a deterministic router or a
        # custom policy; default = ModelRouter over the core's registry).
        router=None,
    ):
        self.core = core
        self.controller_source = controller_source
        self.owner_source = owner_source
        self.store = SupervisorStore(
            core._store,
            frontier_fn=lambda tid: core.workflow_frontier(tid, controller_source),
        )
        self._run_status = run_status_provider
        self._launcher = run_launcher or OpenClawRunLauncher()
        # B1: optional lease-holder identity used by the fencing check in
        # ``_commit``.  ``None`` means "no lease held" (legacy single-supervisor
        # path, where jobs are never leased).
        self._lease_owner: Optional[str] = None
        self._lease_epoch: Optional[int] = None
        # F1 (R15): FREEZE the workspace root to a single canonical spelling at
        # initialization, so every lock path, hash scope and broker reference
        # uses the SAME physical path for the lifetime of this Supervisor.
        # Two controllers naming the same physical workspace through different
        # aliases (symlink vs real path) otherwise derive DIFFERENT lockfiles
        # and BOTH bypass the cross-controller apply fence (exactly-once write
        # pre-effects).  ``Path.resolve()`` collapses symlinks/relative segments
        # to the canonical real path.
        self._workspace_root = (
            str(Path(workspace_root).resolve()) if workspace_root is not None else None
        )
        self._workspace_state = workspace_state_provider
        self._run_tests_fn = run_tests_fn
        self._broker_factory = broker_factory or (lambda: WorkspaceBroker())
        self._clock = clock or utcnow
        # B3: process-registry + identity provider (registered ONLY at the
        # trusted local spawn path in ``_perform_spawn_run``).  F2: wired
        # MANDATORY by default (a default instance is created here, never
        # ``None``), so a restart reconciliation always has registry evidence
        # to reason about; the identity provider is created lazily.
        self._process_registry = process_registry or ProcessRegistry(
            self.core._store
        )
        self._process_identity_provider = process_identity_provider
        # B4 (F3): read-only git provenance provider for the real writer/worktree
        # binding (repo identity, HEAD, branch, dirty).  Injectable for tests;
        # a default instance reads the workspace root via ``git`` (fail-closed).
        self._git_provenance_provider = git_provenance_provider or GitProvenanceProvider(
            self._workspace_root
        )
        # C1: optional resource-governor / snapshot-provider injection (fakes in
        # tests; real defaults are created by the Scheduler when unset).
        self._resource_governor = resource_governor
        self._snapshot_provider = snapshot_provider
        # C2: execution enforcement (scope backend + enforcer).  Enforcement is
        # MANDATORY (F1): when neither ``enforcer`` nor ``scope_backend`` is
        # injected, the PRODUCTION default is wired here
        # (``ExecutionEnforcer`` over the real ``SystemdRunScopeBackend``) —
        # there is NO legacy unbounded ``launcher.spawn`` fallback.  Tests
        # inject a fake enforcer/backend; the Scheduler may also wire one.
        if enforcer is None and scope_backend is None:
            backend = SystemdRunScopeBackend()
            enforcer = ExecutionEnforcer(backend)
            scope_backend = backend
        elif enforcer is None and scope_backend is not None:
            enforcer = ExecutionEnforcer(scope_backend)
        self._enforcer = enforcer
        self._scope_backend = scope_backend
        # D1: Context Builder (pure/deterministic).  The default is the real
        # builder; tests inject a fake to script build success/failure.
        self._context_builder = context_builder or ContextBuilder()
        # D2: optional retrieval / checkpoint / handoff wiring (None = D1 path).
        self._retriever = retriever
        self._checkpoint_store = checkpoint_store
        self._handoff_builder = handoff_builder
        # E2: adaptive model router (default = ModelRouter over the core's registry).
        self._router = router

    # ---------------------------------------------------------------- utils

    def _now(self) -> datetime:
        return self._clock()

    def _now_iso(self) -> str:
        return self.core._store.now_iso()

    # -- B1 lease-holder context + fencing --------------------------------

    def set_lease_owner(self, owner_instance_id: str, lease_epoch: int) -> None:
        """Declare the (owner, epoch) lease this Supervisor currently holds.

        Once a job is leased, ``_commit`` fences any mutation that does not
        carry the job's current holder, so a stale owner after a takeover can
        no longer write.
        """
        self._lease_owner = owner_instance_id
        self._lease_epoch = lease_epoch

    def clear_lease_owner(self) -> None:
        """Clear the lease-holder context (unleased / legacy path)."""
        self._lease_owner = None
        self._lease_epoch = None

    def _enforce_lease_fence(self, job: dict) -> None:
        """Fence a mutating commit to the job's current lease holder (B1).

        No-op when the job is unleased (``owner_instance_id IS NULL``) so the
        legacy single-supervisor flow is unaffected.  For a leased job the
        caller MUST hold the exact (owner, epoch) and the lease must not be
        expired; otherwise :class:`LeaseFencedError` is raised before anything
        is written.

        F5: a leased job with ``lease_expires_at IS NULL`` is fenced fail-closed
        (consistent with ``assert_lease_current``).  After F3 there are no NEW
        unleased RUNNING jobs (``create_job`` starts QUEUED and only ``claim``
        produces RUNNING), so the unleased no-op here is a legacy path only.
        """
        owner = job.get("owner_instance_id")
        if owner is None:
            return
        if self._lease_owner != owner or self._lease_epoch != job["lease_epoch"]:
            raise LeaseFencedError(
                f"lease fence: holder ({self._lease_owner!r}, {self._lease_epoch}) "
                f"is not current for job {job['id']!r} "
                f"(current {owner!r}, epoch {job['lease_epoch']})"
            )
        expires = job.get("lease_expires_at")
        if expires is None or _parse_iso(expires) <= self._now().timestamp():
            raise LeaseFencedError(f"lease expired for job {job['id']!r}")

    def _recheck_lease_fence(self, job_id: str) -> None:
        """F1: re-assert the lease fence IMMEDIATELY before an external effect.

        Action handlers re-read the job and re-run :meth:`_enforce_lease_fence`
        right before a broker call / dispatch spawn, closing the TOCTOU window
        between the top-of-action fence check and the external side effect.  For
        an unleased legacy job this is a no-op (owner NULL).
        """
        job = self.core._store.get_supervisor_job(job_id)
        if job is not None:
            self._enforce_lease_fence(job)

    def _fence_action_locked(
        self,
        job_id: str,
        owner_instance_id: Optional[str],
        lease_epoch: Optional[int],
        expected_facts_version: int,
    ) -> None:
        """Central in-transaction lease fence (F1, Phase B2).

        Re-reads the job on the store connection and raises
        :class:`LeaseFencedError` unless the job's current (owner, epoch,
        expiry, facts_version) exactly matches the decision token.  This is the
        single check bound to the journal begin, every transactional core
        effect, and the journal finalize.  Delegates to
        :meth:`argent_core.store.Store._fence_action`.
        """
        self.core._store._fence_action(
            job_id, owner_instance_id, lease_epoch, expected_facts_version,
        )

    def _build_lookup(self, d: AgentDispatch) -> RunLookup:
        return RunLookup(
            dispatch_id=d.id,
            agent_id=AGENT_IDS[d.role],
            expected_session_label=f"dispatch-{d.id}",
            bound_session_id=d.child_session_id,
            bound_run_id=d.openclaw_run_id,
            # Persisted expected/bound tuple: use the bound values once the
            # dispatch is bound (bind_spawn_result enforces exact equality with
            # the expected values), otherwise the expected values.
            expected_provider=(
                d.actual_provider if d.actual_provider is not None
                else d.expected_agent_class
            ),
            expected_model=(
                d.actual_model if d.actual_model is not None
                else d.expected_model_class
            ),
            expected_thinking_tier=(
                d.thinking_tier if d.thinking_tier is not None
                else d.expected_thinking_tier
            ),
        )

    # ------------------------------------------------------------- snapshot

    def _read_core_facts(self, task_id: str, job_id: str) -> dict:
        """Read every decision-relevant persisted fact (F6).

        Must be called inside the read snapshot AND re-read inside the commit
        transaction so the CAS key can compare them exactly.
        """
        task = self.core._store.get_task(task_id)
        if task is None:
            return None
        frontier = self.core.workflow_frontier(task.id, self.controller_source)
        active = self.store._current_dispatch(task.id)
        at_frontier = self.store._dispatch_at_frontier(task.id, frontier)
        matches = [
            d for d in self.core._store.list_dispatches(task.id)
            if d.cycle_no == frontier.cycle_no and d.position == frontier.position
        ]
        max_attempt = max((d.attempt_no for d in matches), default=0)
        active_role_run = self.core._store.get_active_role_run(task.id)
        gate, open_count = self.store._current_gate(task.id)
        open_findings = sum(
            1 for f in self.core._store.list_findings(task.id)
            if f.status.value == "open"
        )
        latest_handoff = self.core._store.get_latest_handoff(task.id)
        latest_handoff_id = latest_handoff.id if latest_handoff is not None else None
        journal = tuple(sorted(
            (a["action_key"], a["status"], a["attempt_count"])
            for a in self.core._store.list_supervisor_actions(job_id)
        ))
        return {
            "task": task, "frontier": frontier, "active": active,
            "at_frontier": at_frontier, "max_attempt": max_attempt,
            "active_role_run": active_role_run, "gate": gate,
            "open_count": open_count, "open_findings": open_findings,
            "latest_handoff_id": latest_handoff_id, "journal": journal,
        }

    def _read_snapshot(self, job_id: str) -> Optional[_Snapshot]:
        # F6: complete snapshot under a single read transaction so every fact
        # is observed at one consistent point in time.  F6-new: the ``finally``
        # guarantees commit/rollback on EVERY return path (including the early
        # missing-job/task returns), so ``in_transaction`` is never left True
        # for the next Core write.
        conn = self.core._store._conn
        conn.execute("BEGIN")
        try:
            job = self.store._job_row(job_id)
            if job is None:
                return None
            facts = self._read_core_facts(job["task_id"], job_id)
            if facts is None:
                return None
            snap = _Snapshot(
                job=job, task=facts["task"], frontier=facts["frontier"],
                active_dispatch=facts["active"],
                dispatch_at_frontier=facts["at_frontier"],
                max_attempt_at_frontier=facts["max_attempt"],
                active_role_run=facts["active_role_run"], gate=facts["gate"],
                open_gates_count=facts["open_count"],
                open_findings_count=facts["open_findings"],
                latest_handoff_id=facts["latest_handoff_id"],
                action_journal_fingerprint=facts["journal"],
            )
            conn.execute("COMMIT")
            return snap
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")

    def _guarded_observe(self, lookup: RunLookup) -> RunObservation:
        """Observe through the guarded untrusted-runtime-data boundary (F3).

        Shared by ``_observe`` (reconcile) and every action handler that reads
        the provider directly.  A structural exception raised while
        interpreting untrusted runtime data is converted into a fail-closed
        CONFLICT observation (never raises), so neither the decision table nor
        the loop can die.  Genuine ArgentError/NotFound keep their existing
        semantics (not caught here).
        """
        try:
            return self._run_status.observe(lookup)
        except (TypeError, AttributeError, ValueError, KeyError) as exc:
            return RunObservation(
                status=RunStatus.CONFLICT,
                agent_id=lookup.agent_id,
                session_id=None,
                run_id=None,
                provider=None,
                model=None,
                thinking_tier=None,
                started_at=None,
                finished_at=None,
                result=None,
                result_hash=None,
                authoritative_not_found=False,
                evidence_id=f"adapter_exception:{type(exc).__name__}",
                error_code="malformed_runtime_data",
                adapter_exception_type=type(exc).__name__,
            )

    def _observe(self, snap: _Snapshot) -> Optional[RunObservation]:
        d = snap.active_dispatch
        if d is None:
            return None
        return self._guarded_observe(self._build_lookup(d))

    @staticmethod
    def _adapter_exception_type(obs: Optional[RunObservation]) -> Optional[str]:
        """The structural adapter exception type behind an observation, or
        None.  Only a fail-closed CONFLICT produced by ``_guarded_observe``
        (the provider raised TypeError/AttributeError/ValueError/KeyError while
        the runtime data was being interpreted) carries a non-None
        ``adapter_exception_type``; a genuine provenance CONFLICT from the
        provider does not and must keep its existing skipped/WAIT semantics.
        """
        if obs is None or obs.status is not RunStatus.CONFLICT:
            return None
        return obs.adapter_exception_type

    def _action_adapter_conflict(
        self, action: str, dispatch_id: Optional[str], obs: RunObservation,
    ) -> Optional[ActionOutcome]:
        """Marker outcome for an action-time structural provider failure.

        Returns an ``ActionOutcome`` with status ``adapter_exception`` (detail =
        the exception type) when ``obs`` is an exception-caused CONFLICT, else
        None.  The caller (``perform_next_safe_action_if_required``) routes this
        marker into ``adapter_exception_decision`` so the bounded backoff
        (retry_count++, BACKOFF/WAIT, then sticky PERSISTENT_ERROR) applies to
        action-time adapter failures exactly like reconcile-time ones.
        """
        etype = self._adapter_exception_type(obs)
        if etype is None:
            return None
        return ActionOutcome(action, "adapter_exception", etype,
                             dispatch_id=dispatch_id)

    # ------------------------------------------------------------ reconcile

    def reconcile(self, job_id: str) -> ReconcileDecision:
        for _ in range(MAX_SNAPSHOT_RETRIES + 1):
            snap = self._read_snapshot(job_id)
            if snap is None:
                raise NotFound(f"supervisor job {job_id!r} not found")
            obs = self._observe(snap)
            result = self._commit(job_id, snap, obs)
            if result is _RETRY:
                continue
            return result
        # Snapshot contention -> BACKOFF (no busy-loop).
        return self._backoff_decision(job_id)

    def _snapshot_key(self, snap: _Snapshot) -> tuple:
        """Full decision-relevant facts tuple used as the commit CAS key (F6)."""
        f = snap.frontier
        ad = snap.active_dispatch
        af = snap.dispatch_at_frontier
        rr = snap.active_role_run
        g = snap.gate
        job = snap.job
        return (
            job["facts_version"], job["status"], job["retry_count"],
            job["missing_confirmations"], job["owner_prompted_at"],
            job.get("owner_prompted_gate_id"),
            snap.task.state.value,
            snap.task.resume_state.value if snap.task.resume_state else None,
            f.cycle_no if f else None,
            f.position if f else None,
            f.sequence_kind.value if f else None,
            f.expected_role.value if (f and f.expected_role) else None,
            f.include_reviewer if f else None,
            ad.id if ad else None,
            ad.status.value if ad else None,
            ad.openclaw_run_id if ad else None,
            ad.child_session_id if ad else None,
            ad.role.value if ad else None,
            ad.cycle_no if ad else None,
            ad.position if ad else None,
            ad.attempt_no if ad else None,
            ad.handoff_id if ad else None,
            ad.actual_provider if ad else None,
            ad.actual_model if ad else None,
            af.id if af else None,
            af.status.value if af else None,
            af.attempt_no if af else None,
            rr.id if rr else None,
            rr.role.value if rr else None,
            rr.status.value if rr else None,
            g.id if g else None,
            g.status.value if g else None,
            g.closed_at if g else None,
            g.binding_hash if g else None,
            snap.open_gates_count,
            snap.open_findings_count,
            snap.latest_handoff_id,
            snap.max_attempt_at_frontier,
            snap.action_journal_fingerprint,
        )

    def _commit(self, job_id, snap, obs):
        with self.core._store._transaction():
            current_job = self.core._store.get_supervisor_job(job_id)
            if current_job is None:
                raise NotFound(f"supervisor job {job_id!r} not found")
            # B1 fencing: a leased job may only be mutated by its current lease
            # holder.  Fails closed BEFORE any write for a stale owner.
            self._enforce_lease_fence(current_job)
            # F1: carry the fencing token into the authored decision so the
            # action executor can re-check it immediately before any external
            # effect (broker/spawn).
            fence_owner = current_job.get("owner_instance_id")
            fence_epoch = current_job["lease_epoch"]
            if current_job["facts_version"] != snap.job["facts_version"]:
                return _RETRY
            # F6: re-read EVERY decision-relevant fact under BEGIN IMMEDIATE
            # and compare against the snapshot; any drift -> RETRY.
            facts = self._read_core_facts(snap.task.id, job_id)
            if facts is None:
                return _RETRY
            fresh = _Snapshot(
                job=current_job, task=facts["task"], frontier=facts["frontier"],
                active_dispatch=facts["active"],
                dispatch_at_frontier=facts["at_frontier"],
                max_attempt_at_frontier=facts["max_attempt"],
                active_role_run=facts["active_role_run"], gate=facts["gate"],
                open_gates_count=facts["open_count"],
                open_findings_count=facts["open_findings"],
                latest_handoff_id=facts["latest_handoff_id"],
                action_journal_fingerprint=facts["journal"],
            )
            if self._snapshot_key(fresh) != self._snapshot_key(snap):
                return _RETRY
            plan = self._decide(fresh, obs)
            proj = self._projection_fields(fresh, obs)
            fields = dict(proj)
            fields["next_action"] = plan.action.value
            fields["facts_version"] = current_job["facts_version"] + 1
            fields["updated_at"] = self._now_iso()
            if plan.wake_at is not None:
                fields["next_wake_at"] = plan.wake_at
            else:
                fields["next_wake_at"] = None
            if plan.status is not None:
                fields["status"] = plan.status
            # F2 (Phase B2): a persisted BACKOFF is an admission-delayed
            # requeue, not an immediate-run.  Persist it as
            # QUEUED + RETRY_BACKOFF + next_eligible_at and release the lease
            # (holder-fenced above), so the scheduler can never run it before
            # the wake deadline.  ``next_wake_at`` is kept for loop
            # compatibility, but ``next_eligible_at`` is the admission
            # authority checked by ``claim_next_job``/``_job_is_claimable``.
            if plan.status == SupervisorJobStatus.BACKOFF.value:
                fields["queue_reason"] = job_state.QueueReason.RETRY_BACKOFF.value
                if plan.wake_at is not None:
                    fields["next_eligible_at"] = plan.wake_at
                fields["owner_instance_id"] = None
                fields["lease_expires_at"] = None
            if plan.recovery_state is not None:
                fields["recovery_state"] = plan.recovery_state
            if plan.retry_count is not None:
                fields["retry_count"] = plan.retry_count
            if plan.missing_confirmations is not None:
                fields["missing_confirmations"] = plan.missing_confirmations
            if plan.last_error_code is not None:
                fields["last_error_code"] = plan.last_error_code
            if plan.terminal is not None:
                fields["terminal"] = plan.terminal
                fields["status"] = SupervisorJobStatus.TERMINAL.value
                fields["next_action"] = ReconcileAction.NONE.value
                fields["next_wake_at"] = None
            if plan.action in (
                ReconcileAction.START_ROLE, ReconcileAction.CREATE_DISPATCH,
                ReconcileAction.SPAWN_RUN, ReconcileAction.BIND_RUN,
                ReconcileAction.APPLY_PATCH_SET, ReconcileAction.RUN_SANDBOX_TESTS,
                ReconcileAction.RECORD_TEST_RESULT, ReconcileAction.CONSUME_RESULT,
                ReconcileAction.MARK_RUN_FAILED, ReconcileAction.CORE_RECOVER,
                ReconcileAction.PRESENT_OWNER_GATE,
            ):
                fields["last_progress_at"] = self._now_iso()
            self.core._store._update_supervisor_job(job_id, **fields)
            # SPEC V3A §7: enqueue notifications for transitions that first
            # set a sticky ERROR or a WAITING_GATE, in the SAME transaction
            # as the authoritative status change (dedup-guarded, no-op for
            # non-first transitions).
            if plan.action is ReconcileAction.PERSISTENT_ERROR:
                self._enqueue_persistent_error_notification(current_job)
            elif plan.action is ReconcileAction.PRESENT_OWNER_GATE:
                self._enqueue_waiting_gate_notification(current_job, fresh.gate)
            new_version = current_job["facts_version"] + 1
        return ReconcileDecision(
            job_id=job_id,
            facts_version=new_version,
            action=plan.action,
            reason=plan.reason,
            dispatch_id=plan.dispatch_id,
            wake_at=plan.wake_at,
            owner_instance_id=fence_owner,
            lease_epoch=fence_epoch if fence_owner is not None else None,
        )

    def _backoff_decision(self, job_id) -> ReconcileDecision:
        now = self._now()
        with self.core._store._transaction():
            job = self.core._store.get_supervisor_job(job_id)
            if job is None:
                raise NotFound(f"supervisor job {job_id!r} not found")
            # F1: a backoff write is still an authoritative write-commit; fence
            # a leased job to its current holder before mutating.
            self._enforce_lease_fence(job)
            retry = job["retry_count"]
            wake = _iso(now + timedelta(seconds=backoff_seconds(retry)))
            owner = job.get("owner_instance_id")
            epoch = job["lease_epoch"]
            # F2 (Phase B2): persist the backoff atomically as
            # QUEUED + RETRY_BACKOFF + next_eligible_at and release the lease
            # (holder-fenced via the B1 CAS below) so the scheduler cannot run
            # the job before the wake deadline.  ``next_wake_at`` stays for
            # loop compatibility; ``next_eligible_at`` is the admission
            # authority.
            fields = {
                "queue_reason": job_state.QueueReason.RETRY_BACKOFF.value,
                "next_action": ReconcileAction.WAIT.value,
                "next_wake_at": wake,
                "next_eligible_at": wake,
            }
            if owner is not None:
                updated = self.core._store._transition_job(
                    job_id,
                    to_primary_state=job_state.PrimaryState.QUEUED.value,
                    to_status=SupervisorJobStatus.BACKOFF.value,
                    fields={**fields, "owner_instance_id": None,
                            "lease_expires_at": None},
                    bump_facts_version=True,
                    cas_owner_instance_id=owner,
                    cas_lease_epoch=epoch,
                    cas_lease_unexpired=True,
                )
            else:
                updated = self.core._store._transition_job(
                    job_id,
                    to_primary_state=job_state.PrimaryState.QUEUED.value,
                    to_status=SupervisorJobStatus.BACKOFF.value,
                    fields=fields,
                    bump_facts_version=True,
                )
            version = updated["facts_version"]
        return ReconcileDecision(
            job_id=job_id, facts_version=version,
            action=ReconcileAction.WAIT, reason="snapshot_contention",
            wake_at=wake,
            owner_instance_id=None,
            lease_epoch=None,
        )

    # ------------------------------------------------------------ projection

    def _projection_fields(self, snap: _Snapshot, obs) -> dict:
        f = snap.frontier
        role = f.expected_role if f else None
        disp = snap.active_dispatch if snap.active_dispatch is not None \
            else snap.dispatch_at_frontier
        result_status = obs.status.value if obs is not None else NOT_OBSERVED
        gate = snap.gate
        return {
            "workflow_state": snap.task.state.value,
            "expected_role": role.value if role else None,
            "expected_dispatch_id": disp.id if disp is not None else None,
            "agent_id": AGENT_IDS.get(role) if role else None,
            "session_id": disp.child_session_id if disp is not None else None,
            "run_id": disp.openclaw_run_id if disp is not None else None,
            "attempt_no": disp.attempt_no if disp is not None else 0,
            "dispatch_status": disp.status.value if disp is not None else None,
            "result_status": result_status,
            "result_consumed": (
                1 if (disp is not None and disp.status is DispatchStatus.CONSUMED
                       and disp.consumed_at is not None) else 0
            ),
            "current_handoff_id": (
                self.core._store.get_latest_handoff(snap.task.id).id
                if self.core._store.get_latest_handoff(snap.task.id) is not None
                else None
            ),
            "open_findings_count": sum(
                1 for fd in self.core._store.list_findings(snap.task.id)
                if fd.status.value == "open"
            ),
            "rework_cycle": self.store._max_cycle(snap.task.id),
            "owner_gate_id": gate.id if gate is not None else None,
            "gate_status": gate.status.value if gate is not None else None,
            "gate_scope": gate.scope if gate is not None else None,
            "gate_closed": 1 if (gate is not None and gate.closed_at is not None) else 0,
        }

    # ------------------------------------------------------------- decision

    def _decide(self, snap: _Snapshot, obs) -> _Plan:
        job = snap.job
        task = snap.task
        frontier = snap.frontier

        if job["terminal"] is not None:
            return _Plan(ReconcileAction.NONE, "job_terminal")

        # A persisted ERROR state is sticky: no further autonomous action
        # (PERSISTENT_ERROR is not re-decided forever, SPEC V2C §9).
        if job["status"] == SupervisorJobStatus.ERROR.value:
            return _Plan(ReconcileAction.NONE, "error_state")

        if task.state is TaskState.DONE:
            return _Plan(ReconcileAction.CLOSE_DONE, "task_done")
        if task.state in (TaskState.FAILED, TaskState.CANCELLED):
            return _Plan(ReconcileAction.CLOSE_FAILED, "task_failed_cancelled")
        if task.state is TaskState.BLOCKED:
            return _Plan(ReconcileAction.CLOSE_BLOCKED, "task_blocked")

        gate_plan = self._decide_gate(snap)
        if gate_plan is not None:
            return gate_plan

        if frontier.expected_role is None:
            return _Plan(ReconcileAction.PERSISTENT_ERROR, "frontier_exhausted",
                         status=SupervisorJobStatus.ERROR.value,
                         last_error_code="frontier_exhausted")

        role = frontier.expected_role

        ad = snap.active_dispatch
        if ad is not None and (
            ad.cycle_no != frontier.cycle_no
            or ad.position != frontier.position
            or ad.role is not role
        ):
            return _Plan(
                ReconcileAction.PERSISTENT_ERROR, "ledger_conflict_active_dispatch",
                dispatch_id=ad.id,
                status=SupervisorJobStatus.ERROR.value,
                last_error_code="ledger_conflict_active_dispatch",
            )

        if ad is None:
            return self._decide_no_active(snap, role)

        if ad.status is DispatchStatus.PENDING or (
            ad.status is DispatchStatus.RECOVERY_PENDING
            and ad.child_session_id is None
        ):
            return self._decide_unbound(snap, obs, role, ad)
        return self._decide_bound(snap, obs, role, ad)

    def _decide_gate(self, snap: _Snapshot) -> Optional[_Plan]:
        job = snap.job
        task = snap.task
        if snap.open_gates_count > 1:
            return _Plan(ReconcileAction.PERSISTENT_ERROR, "multiple_active_gates",
                         status=SupervisorJobStatus.ERROR.value,
                         last_error_code="multiple_active_gates")
        gate = snap.gate
        if task.state is TaskState.OWNER_APPROVAL_REQUIRED:
            if gate is None or gate.status not in (
                ApprovalStatus.PENDING, ApprovalStatus.APPROVED,
            ):
                return _Plan(ReconcileAction.PERSISTENT_ERROR,
                             "gate_task_inconsistent_no_gate",
                             status=SupervisorJobStatus.ERROR.value,
                             last_error_code="gate_task_inconsistent")
            if gate.status is ApprovalStatus.PENDING:
                prompted_gate = job.get("owner_prompted_gate_id")
                if job["owner_prompted_at"] is None or prompted_gate != gate.id:
                    return _Plan(ReconcileAction.PRESENT_OWNER_GATE,
                                 "present_gate",
                                 status=SupervisorJobStatus.WAITING_GATE.value)
                return _Plan(ReconcileAction.WAIT, "waiting_gate",
                             status=SupervisorJobStatus.WAITING_GATE.value,
                             wake_at=_iso(self._now() + timedelta(seconds=RUNNING_POLL_SECONDS)))
            return _Plan(ReconcileAction.WAIT, "gate_approved_waiting_execution",
                         status=SupervisorJobStatus.WAITING_GATE.value,
                         wake_at=_iso(self._now() + timedelta(seconds=RUNNING_POLL_SECONDS)))
        # Task not in gate state.
        if gate is not None and gate.status in (
            ApprovalStatus.PENDING, ApprovalStatus.APPROVED,
        ):
            return _Plan(ReconcileAction.PERSISTENT_ERROR,
                         "gate_task_inconsistent_active_gate",
                         status=SupervisorJobStatus.ERROR.value,
                         last_error_code="gate_task_inconsistent")
        return None

    def _decide_no_active(self, snap: _Snapshot, role: Role) -> _Plan:
        job = snap.job
        latest = snap.dispatch_at_frontier
        if latest is not None and latest.status in (
            DispatchStatus.FAILED, DispatchStatus.REJECTED,
        ):
            if latest.attempt_no >= MAX_DISPATCH_ATTEMPTS_PER_STEP:
                return _Plan(ReconcileAction.CLOSE_FAILED, "max_attempts")
        active_rr = snap.active_role_run
        if active_rr is None or active_rr.role is not role:
            return _Plan(ReconcileAction.START_ROLE, "need_role",
                         dispatch_id=latest.id if latest is not None else None)
        return _Plan(ReconcileAction.CREATE_DISPATCH, "need_dispatch",
                     dispatch_id=latest.id if latest is not None else None,
                     missing_confirmations=0, retry_count=0)

    def _decide_unbound(self, snap, obs, role, ad) -> _Plan:
        job = snap.job
        if obs is None:
            return _Plan(ReconcileAction.WAIT, "no_observation",
                         status=SupervisorJobStatus.ACTIVE.value,
                         wake_at=_iso(self._now() + timedelta(seconds=RUNNING_POLL_SECONDS)))

        if obs.status in (RunStatus.RUNNING, RunStatus.SUCCEEDED,
                          RunStatus.FAILED, RunStatus.CANCELLED):
            if obs.run_id is None or obs.session_id is None:
                # The runtime flushes the trajectory late: an active session
                # without bindable values yet must WAIT (bounded poll), never
                # tight-loop on BIND_RUN (real-E2E finding).
                return _Plan(ReconcileAction.WAIT, "run_active_no_binding_values",
                             dispatch_id=ad.id,
                             status=SupervisorJobStatus.ACTIVE.value,
                             recovery_state=RecoveryState.DISCOVERING_RUN.value,
                             wake_at=_iso(self._now() + timedelta(seconds=RUNNING_POLL_SECONDS)))
            # NOTE: retry_count is deliberately NOT reset here.  A structural
            # adapter failure during the BIND_RUN action re-observation is
            # routed into ``adapter_exception_decision`` (retry_count++);
            # resetting it to 0 on every re-decision would keep the job stuck
            # in an unbounded busy-loop with retry_count pinned at 0.
            return _Plan(ReconcileAction.BIND_RUN, "bind_observed_run",
                         dispatch_id=ad.id,
                         recovery_state=RecoveryState.RESTORING_BINDING.value,
                         missing_confirmations=0)

        if obs.status is RunStatus.NOT_FOUND:
            spawn = self._spawn_action(ad.id)
            # F3: an exhausted SPAWN_RUN (persisted FAILED row with
            # attempt_count >= MAX_ACTION_RETRIES) is a terminal ambiguity, not
            # a pending spawn.  Never keep waiting indefinitely (retry_count
            # growing forever) — persist a sticky ERROR.
            if (spawn is not None and spawn["status"] == "FAILED"
                    and spawn["attempt_count"] >= MAX_ACTION_RETRIES):
                return _Plan(ReconcileAction.PERSISTENT_ERROR,
                             "spawn_run_exhausted", dispatch_id=ad.id,
                             status=SupervisorJobStatus.ERROR.value,
                             recovery_state=RecoveryState.PERSISTENT_ERROR.value,
                             last_error_code="spawn_run_exhausted")
            if not obs.authoritative_not_found:
                return self._wait_missing(snap, ad, "not_found_non_authoritative",
                                          RecoveryState.DISCOVERING_RUN.value)
            if spawn is None:
                return _Plan(ReconcileAction.SPAWN_RUN, "spawn_run",
                             dispatch_id=ad.id,
                             recovery_state=RecoveryState.DISCOVERING_RUN.value)
            # Spawn already planned: wait / confirm.  Increment AND persist
            # retry_count so the backoff actually grows (F3).  The wake uses
            # the *pre-increment* count so the sequence is 1,2,4,8,16,30s.
            missing = job["missing_confirmations"] + 1
            retry = job["retry_count"] + 1
            if missing >= MISSING_UNBOUND_SPAWN_CONFIRMATIONS:
                return _Plan(ReconcileAction.CLOSE_BLOCKED,
                             "spawn_unresolvable",
                             recovery_state=RecoveryState.AMBIGUOUS_WRITER.value if _is_implementer(role) else None)
            return _Plan(ReconcileAction.WAIT, "spawn_pending",
                         dispatch_id=ad.id,
                         status=SupervisorJobStatus.ACTIVE.value,
                         recovery_state=RecoveryState.DISCOVERING_RUN.value,
                         missing_confirmations=missing,
                         retry_count=retry,
                         wake_at=_iso(self._now() + timedelta(seconds=backoff_seconds(retry - 1))))

        # UNKNOWN / CONFLICT
        return self._adapter_backoff(snap, ad, obs)

    def _decide_bound(self, snap, obs, role, ad) -> _Plan:
        job = snap.job
        if obs is None:
            return _Plan(ReconcileAction.WAIT, "no_observation",
                         status=SupervisorJobStatus.ACTIVE.value,
                         wake_at=_iso(self._now() + timedelta(seconds=RUNNING_POLL_SECONDS)))

        if obs.status is RunStatus.RUNNING:
            return _Plan(ReconcileAction.WAIT, "run_running",
                         dispatch_id=ad.id,
                         status=SupervisorJobStatus.ACTIVE.value,
                         wake_at=_iso(self._now() + timedelta(seconds=RUNNING_POLL_SECONDS)),
                         retry_count=0)

        if obs.status is RunStatus.SUCCEEDED:
            if _is_write_role(role):
                return self._decide_write_preconditions(snap, obs, ad)
            # A permanently failed consume action (rejected result, exhausted
            # retries) must NOT re-plan CONSUME_RESULT forever (real-E2E
            # finding: rejected consume looped ~380x).  Route to the bounded
            # run-failure policy instead.
            act = self._consume_action(ad.id)
            if act is not None and act["status"] == "FAILED" \
                    and act["attempt_count"] >= MAX_ACTION_RETRIES:
                return _Plan(ReconcileAction.MARK_RUN_FAILED,
                             "consume_unresolvable", dispatch_id=ad.id,
                             recovery_state=RecoveryState.RETRYING_STEP.value)
            return _Plan(ReconcileAction.CONSUME_RESULT, "consume_result",
                         dispatch_id=ad.id,
                         recovery_state=RecoveryState.CONSUMING_RESULT.value)

        if obs.status in (RunStatus.FAILED, RunStatus.CANCELLED):
            return _Plan(ReconcileAction.MARK_RUN_FAILED, "run_failed",
                         dispatch_id=ad.id,
                         recovery_state=RecoveryState.RETRYING_STEP.value)

        if obs.status is RunStatus.NOT_FOUND:
            missing = job["missing_confirmations"] + 1
            retry = job["retry_count"] + 1
            if missing >= MISSING_BOUND_RUN_CONFIRMATIONS:
                if _is_implementer(role):
                    return _Plan(ReconcileAction.CLOSE_BLOCKED,
                                 "ambiguous_writer",
                                 recovery_state=RecoveryState.AMBIGUOUS_WRITER.value)
                return _Plan(ReconcileAction.MARK_RUN_FAILED,
                             "missing_bound_run", dispatch_id=ad.id,
                             recovery_state=RecoveryState.CORE_RECOVERY_REQUIRED.value)
            return _Plan(ReconcileAction.WAIT, "bound_run_missing",
                         dispatch_id=ad.id,
                         status=SupervisorJobStatus.ACTIVE.value,
                         recovery_state=RecoveryState.DISCOVERING_RUN.value,
                         missing_confirmations=missing,
                         retry_count=retry,
                         wake_at=_iso(self._now() + timedelta(seconds=backoff_seconds(retry - 1))))

        return self._adapter_backoff(snap, ad, obs)

    def _decide_write_preconditions(self, snap, obs, ad) -> _Plan:
        # Implementer/QA: broker apply -> sandbox tests -> record test result
        # -> consume (§8.3 + A7).  Each step is journaled; check their status.
        job = snap.job
        # F2: an exhausted (FAILED with retries >= MAX) precondition action must
        # route to a persisted ERROR, never be re-planned forever.
        for atype in ("APPLY_PATCH_SET", "RUN_SANDBOX_TESTS",
                      "RECORD_TEST_RESULT", "CONSUME_RESULT"):
            act = self._latest_action(ad.id, atype)
            if act is not None and act["status"] == "FAILED" \
                    and act["attempt_count"] >= MAX_ACTION_RETRIES:
                return _Plan(ReconcileAction.PERSISTENT_ERROR,
                             f"{atype.lower()}_exhausted", dispatch_id=ad.id,
                             status=SupervisorJobStatus.ERROR.value,
                             recovery_state=RecoveryState.PERSISTENT_ERROR.value,
                             last_error_code=f"{atype.lower()}_exhausted")
        # F1: a diverged canonical apply intent (workspace hash matched neither
        # the persisted precondition nor effect after a partial broker-effect
        # crash) is a bounded sticky error — never re-plan APPLY (which could
        # mint a second result-keyed intent) and never advance the dispatch.
        if self._diverged_apply_intent(ad.id) is not None:
            return self._apply_diverged_plan(snap, ad)
        apply_done = self._action_succeeded(ad.id, "APPLY_PATCH_SET")
        tests_done = self._action_succeeded(ad.id, "RUN_SANDBOX_TESTS")
        record_done = self._action_succeeded(ad.id, "RECORD_TEST_RESULT")
        if not apply_done:
            # R7-F1: plan APPLY_PATCH_SET whether or not a committed apply
            # intent already exists.  ``_perform_apply_patch_set`` reconciles an
            # existing RUNNING/UNCERTAIN intent exactly-once from its PERSISTED
            # precondition/effect hashes (never minting a second intent, never
            # trusting a changed observation).  A changed observation is failed
            # closed on the NEXT decision via the frozen-hash backoff below.
            return _Plan(ReconcileAction.APPLY_PATCH_SET, "apply_patch_set",
                         dispatch_id=ad.id,
                         recovery_state=RecoveryState.CONSUMING_RESULT.value)
        # F1: once APPLY succeeded, the frozen write-result hash is the single
        # immutable binding for the rest of the pipeline.  Recompute the
        # canonical full-result hash from the CURRENT observation and require
        # exact equality; a mismatch (provider result changed after apply) must
        # NEVER proceed to tests/record/consume — bounded backoff instead.
        frozen = self._frozen_write_result_hash(ad.id)
        if frozen is not None \
                and self._canonical_full_result_hash(obs.result) != frozen:
            return self._write_hash_mismatch_plan(snap, ad)
        if not tests_done:
            return _Plan(ReconcileAction.RUN_SANDBOX_TESTS, "run_sandbox_tests",
                         dispatch_id=ad.id,
                         recovery_state=RecoveryState.CONSUMING_RESULT.value)
        if not record_done:
            return _Plan(ReconcileAction.RECORD_TEST_RESULT, "record_test_result",
                         dispatch_id=ad.id,
                         recovery_state=RecoveryState.CONSUMING_RESULT.value)
        return _Plan(ReconcileAction.CONSUME_RESULT, "consume_result",
                     dispatch_id=ad.id,
                     recovery_state=RecoveryState.CONSUMING_RESULT.value)

    def _adapter_backoff(self, snap, ad, obs) -> _Plan:
        job = snap.job
        retry = job["retry_count"] + 1
        if retry >= MAX_RUNTIME_UNKNOWN:
            return _Plan(ReconcileAction.PERSISTENT_ERROR,
                         f"adapter_{obs.status.value.lower()}",
                         dispatch_id=ad.id,
                         status=SupervisorJobStatus.ERROR.value,
                         recovery_state=RecoveryState.PERSISTENT_ERROR.value,
                         retry_count=retry,
                         last_error_code=f"adapter_{obs.status.value.lower()}")
        return _Plan(ReconcileAction.WAIT, f"adapter_{obs.status.value.lower()}",
                     dispatch_id=ad.id,
                     status=SupervisorJobStatus.BACKOFF.value,
                     recovery_state=RecoveryState.RUNTIME_UNKNOWN.value,
                     retry_count=retry,
                     wake_at=_iso(self._now() + timedelta(seconds=backoff_seconds(retry))))

    def _write_hash_mismatch_plan(self, snap, ad) -> _Plan:
        """Bounded backoff for a write-result hash mismatch (F1).

        The current observation no longer describes the frozen write-result
        hash (the provider result changed after APPLY).  Never proceed to
        tests/record/consume; after MAX_RUNTIME_UNKNOWN backoffs land in a
        sticky PERSISTENT_ERROR.
        """
        job = snap.job
        retry = job["retry_count"] + 1
        if retry >= MAX_RUNTIME_UNKNOWN:
            return _Plan(ReconcileAction.PERSISTENT_ERROR,
                         "write_result_hash_mismatch", dispatch_id=ad.id,
                         status=SupervisorJobStatus.ERROR.value,
                         recovery_state=RecoveryState.PERSISTENT_ERROR.value,
                         retry_count=retry,
                         last_error_code="write_result_hash_mismatch")
        return _Plan(ReconcileAction.WAIT, "write_result_hash_mismatch",
                     dispatch_id=ad.id,
                     status=SupervisorJobStatus.BACKOFF.value,
                     recovery_state=RecoveryState.RUNTIME_UNKNOWN.value,
                     retry_count=retry,
                     wake_at=_iso(self._now() + timedelta(seconds=backoff_seconds(retry))))

    def _apply_diverged_plan(self, snap, ad) -> _Plan:
        """Bounded backoff for a diverged canonical apply intent (F1).

        The workspace hash matched neither the persisted precondition nor the
        persisted effect (a partial broker-effect crash).  Never re-plan APPLY
        (never mint a second result-keyed intent) and never advance to
        tests/record/consume; after MAX_RUNTIME_UNKNOWN backoffs land in a
        sticky PERSISTENT_ERROR.  Mirrors ``_write_hash_mismatch_plan``.
        """
        job = snap.job
        retry = job["retry_count"] + 1
        if retry >= MAX_RUNTIME_UNKNOWN:
            return _Plan(ReconcileAction.PERSISTENT_ERROR,
                         "workspace_diverged", dispatch_id=ad.id,
                         status=SupervisorJobStatus.ERROR.value,
                         recovery_state=RecoveryState.PERSISTENT_ERROR.value,
                         retry_count=retry,
                         last_error_code="workspace_diverged")
        return _Plan(ReconcileAction.WAIT, "workspace_diverged",
                     dispatch_id=ad.id,
                     status=SupervisorJobStatus.BACKOFF.value,
                     recovery_state=RecoveryState.RUNTIME_UNKNOWN.value,
                     retry_count=retry,
                     wake_at=_iso(self._now() + timedelta(seconds=backoff_seconds(retry))))

    def _wait_missing(self, snap, ad, reason, recovery_state) -> _Plan:
        job = snap.job
        missing = job["missing_confirmations"] + 1
        retry = job["retry_count"] + 1
        return _Plan(ReconcileAction.WAIT, reason,
                     dispatch_id=ad.id,
                     status=SupervisorJobStatus.ACTIVE.value,
                     recovery_state=recovery_state,
                     missing_confirmations=missing,
                     retry_count=retry,
                     wake_at=_iso(self._now() + timedelta(seconds=backoff_seconds(retry - 1))))

    def _spawn_action(self, dispatch_id: str) -> Optional[dict]:
        return self.core._store.get_supervisor_action_by_key(
            f"supervisor:dispatch:{dispatch_id}:spawn"
        )

    def _consume_action(self, dispatch_id: str) -> Optional[dict]:
        """The most recent CONSUME_RESULT journal entry for a dispatch."""
        return self._latest_action(dispatch_id, "CONSUME_RESULT")

    def _action_succeeded(self, dispatch_id: str, action_type: str) -> bool:
        rows = self.core._store.list_supervisor_actions()
        for a in rows:
            if a["dispatch_id"] == dispatch_id and a["action_type"] == action_type \
                    and a["status"] == "SUCCEEDED":
                return True
        return False

    # ---------------------------------------------------------- completion

    def receive_completion_hint(self, dispatch_id, event_meta, result) -> ReceiveResult:
        """Completion hint into the persisted pipeline (A9: fail-closed).

        For write roles (Implementer/QA) a completion hint is ADVISORY ONLY: it
        must NEVER consume the dispatch directly (that would bypass the
        broker apply -> sandbox tests -> record test result -> consume flow).
        The hint is dropped/deferred and the persisted provider observation
        drives the normal pipeline via ``reconcile()``.  Non-write roles
        (LEAD/ANALYST/REVIEWER) keep the direct ``Core.receive_agent_result``
        fast path exactly as today.
        """
        d = self.core._store.get_dispatch(dispatch_id)
        if d is not None and _is_write_role(d.role):
            return ReceiveResult(
                dispatch_id, "deferred",
                reason="write_role_completion_is_advisory",
            )
        canonical = _canonical_json(result)
        run_id = event_meta.get("run_id") if isinstance(event_meta, dict) else None
        key = (
            f"supervisor:consume:{dispatch_id}:{run_id}:"
            f"{_sha256(canonical)}"
        )
        try:
            return self.core.receive_agent_result(
                dispatch_id, event_meta, result, self.controller_source,
                idempotency_key=key,
            )
        except ArgentError as exc:
            return ReceiveResult(
                dispatch_id, "rejected", reason=f"completion_hint:{type(exc).__name__}"
            )

    # --------------------------------------------------- notifications
    # (SPEC V3A §7, Amendments 2/3) Dedup-guarded, atomic
    # ``notification_outbox`` enqueue.  The helper NEVER changes
    # job/dispatch/action/gate status and is never an authority; it only
    # projects an already-authoritative transition into the outbox.  It MUST
    # be called inside the caller's ``BEGIN IMMEDIATE`` transaction.  A
    # ``dedup_key`` UNIQUE conflict is a silent no-op (no exception).

    def _task_state_value(self, task_id: str) -> Optional[str]:
        task = self.core._store.get_task(task_id)
        return task.state.value if task is not None else None

    def _task_has_rejected_gate(self, task_id: str) -> bool:
        for a in self.core._store.list_approvals(task_id):
            if a.status is ApprovalStatus.REJECTED:
                return True
        return False

    def _enqueue_notification(
        self, job, *, notification_type, reason_code, event_ref,
        event_version=1, gate_id=None, binding_hash=None,
    ) -> bool:
        """Single dedup-guarded outbox INSERT (Amendment 3)."""
        ntype = (
            notification_type
            if isinstance(notification_type, NotificationType)
            else NotificationType(notification_type)
        )
        if ntype is NotificationType.OWNER_APPROVAL_REQUIRED:
            dedup_key = notifications.gate_dedup_key(
                job["id"], gate_id, binding_hash, event_version)
        else:
            dedup_key = notifications.normal_dedup_key(
                job["id"], ntype, event_ref, event_version)
        now = self._now_iso()
        scope = notifications.scope_ref(binding_hash) if binding_hash else None
        payload = notifications.build_payload(
            notification_type=ntype.value, supervisor_job_id=job["id"],
            task_id=job["task_id"], event_ref=event_ref, event_at=now,
            reason_code=reason_code, gate_id=gate_id, scope_ref=scope,
        )
        payload_hash = notifications.payload_hash(payload)
        row = {
            "id": notifications.outbox_id(dedup_key),
            "supervisor_job_id": job["id"],
            "task_id": job["task_id"],
            "dispatch_id": None,
            "gate_id": gate_id,
            "notification_type": ntype.value,
            "event_ref": event_ref,
            "event_version": event_version,
            "dedup_key": dedup_key,
            "payload_json": notifications.canonical_payload_json(payload),
            "payload_hash": payload_hash,
            "status": NotificationStatus.PENDING.value,
            "attempt_count": 0,
            "next_attempt_at": None,
            "claimed_at": None,
            "claim_token": None,
            "last_attempt_at": None,
            "sent_at": None,
            "last_error_code": None,
            "created_at": now,
            "updated_at": now,
        }
        return self.core._store._insert_notification(row)

    def _enqueue_close_notification(self, job, terminal: str, reason: str) -> bool:
        """Project a CLOSE_JOB transition (DONE/FAILED/BLOCKED) into the
        outbox, deriving the outgoing code from the internal reason + the
        same-transaction ``tasks.state`` / ``owner_approvals`` facts
        (Amendments 2b/2c)."""
        task_state = self._task_state_value(job["task_id"])
        gate_rejected = self._task_has_rejected_gate(job["task_id"])
        outcome = notifications.resolve_close_outcome(
            terminal, reason, task_state=task_state, gate_rejected=gate_rejected)
        if outcome is None:
            return False
        ntype, code = outcome
        return self._enqueue_notification(
            job, notification_type=ntype, reason_code=code,
            event_ref=notifications.event_ref_close(job["id"], terminal),
        )

    def _enqueue_persistent_error_notification(self, job) -> bool:
        ntype, code = notifications.persistent_error_outcome()
        return self._enqueue_notification(
            job, notification_type=ntype, reason_code=code,
            event_ref=notifications.event_ref_persistent_error(job["id"]),
        )

    def _enqueue_waiting_gate_notification(self, job, gate) -> bool:
        ntype, code = notifications.waiting_gate_outcome()
        gate_id = gate.id if gate is not None else None
        bh = gate.binding_hash if gate is not None else None
        return self._enqueue_notification(
            job, notification_type=ntype, reason_code=code,
            event_ref=notifications.event_ref_gate(job["id"], gate_id),
            gate_id=gate_id, binding_hash=bh,
        )

    # --------------------------------------------------- action execution

    def perform_next_safe_action_if_required(
        self, decision: ReconcileDecision,
    ) -> ActionOutcome:
        if decision.action in (ReconcileAction.NONE, ReconcileAction.WAIT):
            return ActionOutcome(decision.action.value, "noop")

        job = self.core._store.get_supervisor_job(decision.job_id)
        if job is None:
            return ActionOutcome(decision.action.value, "skipped", "job_missing")

        # F1: fencing check FIRST — a decision authored under a lease may only
        # be executed while that exact (owner, epoch) is still the current,
        # unexpired holder.  A stale owner after a takeover raises
        # ``LeaseFencedError`` and writes NOTHING.  This runs before the stale
        # facts_version guard so a fenced decision surfaces as a fence failure
        # (not a silent skip).  Unleased legacy decisions skip this (owner None).
        if decision.owner_instance_id is not None:
            self.store.assert_lease_current(
                decision.job_id, decision.owner_instance_id, decision.lease_epoch
            )

        # Stale decision guard (§8.1).
        if job["facts_version"] != decision.facts_version:
            return ActionOutcome(decision.action.value, "skipped", "stale_decision")

        handler = getattr(self, f"_perform_{decision.action.value.lower()}", None)
        if handler is None:
            return ActionOutcome(decision.action.value, "skipped", "no_handler")
        # F1 (Phase B2): arm the in-transaction action fence for the handler's
        # duration.  Every journal begin / core effect / finalize write opens a
        # ``BEGIN IMMEDIATE`` transaction which re-asserts the decision token
        # atomically with the write lock; a stale holder after a takeover
        # raises ``LeaseFencedError`` and writes NOTHING (rollback).
        self.core._store._set_action_fence(
            decision.job_id, decision.owner_instance_id, decision.lease_epoch,
            decision.facts_version,
        )
        try:
            result = handler(decision, job)
        except LeaseFencedError:
            raise
        except IdempotencyError as exc:
            # F2: an args-hash-mismatch is an invalid journal action and must
            # never livelock the job in ACTIVE re-planning.  Persist a sticky
            # ERROR atomically.
            self._persist_error(
                job["id"], f"{decision.action.value}_args_hash_mismatch"
            )
            return ActionOutcome(
                decision.action.value, "failed",
                f"args_hash_mismatch:{exc}", dispatch_id=decision.dispatch_id,
            )
        except ArgentError as exc:
            return ActionOutcome(
                decision.action.value, "failed", f"{type(exc).__name__}:{exc}",
                dispatch_id=decision.dispatch_id,
            )
        finally:
            self.core._store._clear_action_fence()
        if result.status == "exhausted":
            # F2: an exhausted journal action (attempt_count >= MAX) must
            # atomically persist an error state instead of returning the same
            # exhausted outcome forever while the job stays ACTIVE.
            self._persist_error(
                job["id"], f"{decision.action.value}_exhausted"
            )
        elif result.status == "adapter_exception":
            # F3: an action-time structural adapter failure (the handler
            # re-observed the runtime and ``_guarded_observe`` turned a
            # TypeError/AttributeError/ValueError/KeyError into a fail-closed
            # CONFLICT) must be bounded exactly like a reconcile-time one:
            # retry_count++, BACKOFF/WAIT, then sticky PERSISTENT_ERROR after
            # MAX_RUNTIME_UNKNOWN.  Never a plain ``skipped`` that re-plans the
            # action unboundedly with retry_count stuck at 0.
            self.adapter_exception_decision(job["id"], result.detail)
        return result

    def _persist_error(self, job_id: str, error_code: str) -> None:
        """Atomically persist a sticky job ERROR state (F2, no livelock).

        Every exhausted / args-hash-mismatch / unreconciled-RUNNING journal
        outcome must land here so the job leaves ACTIVE re-planning and is never
        re-decided (``_decide`` short-circuits on status == ERROR).  Terminal
        jobs are never downgraded.
        """
        with self.core._store._transaction():
            cur = self.core._store.get_supervisor_job(job_id)
            if cur is None or cur["terminal"] is not None:
                return
            # F1: a sticky-ERROR write is an authoritative commit; fence a
            # leased job to its current holder before mutating.
            self._enforce_lease_fence(cur)
            self.core._store._update_supervisor_job(
                job_id,
                status=SupervisorJobStatus.ERROR.value,
                recovery_state=RecoveryState.PERSISTENT_ERROR.value,
                last_error_code=error_code,
                next_action=ReconcileAction.NONE.value,
                next_wake_at=None,
                updated_at=self._now_iso(),
                facts_version=cur["facts_version"] + 1,
            )
            # SPEC V3A Amendment 3: first transition to sticky ERROR -> one
            # PERSISTENT_ERROR notification (dedup-guarded).
            self._enqueue_persistent_error_notification(cur)

    def adapter_exception_decision(
        self, job_id: str, type_name: str
    ) -> ReconcileDecision:
        """Persist a bounded backoff for a structural adapter exception (F3).

        Mirrors ``_adapter_backoff``: increments ``retry_count``, persists
        BACKOFF+WAIT (or a sticky PERSISTENT_ERROR after MAX_RUNTIME_UNKNOWN),
        and returns a structured decision so ``SupervisorLoop.run_once`` never
        dies on untrusted runtime data.  Only structural adapter exceptions
        (TypeError/AttributeError/ValueError/KeyError) ever reach here; Core/DB
        and ArgentError keep their normal semantics.
        """
        now = self._now()
        error_code = f"adapter_exception:{type_name}"
        with self.core._store._transaction():
            job = self.core._store.get_supervisor_job(job_id)
            if job is None:
                raise NotFound(f"supervisor job {job_id!r} not found")
            # F1: fence a leased job to its current holder before mutating.
            self._enforce_lease_fence(job)
            retry = job["retry_count"] + 1
            if retry >= MAX_RUNTIME_UNKNOWN:
                self.core._store._update_supervisor_job(
                    job_id,
                    status=SupervisorJobStatus.ERROR.value,
                    recovery_state=RecoveryState.PERSISTENT_ERROR.value,
                    last_error_code=error_code,
                    next_action=ReconcileAction.NONE.value,
                    next_wake_at=None,
                    retry_count=retry,
                    updated_at=self._now_iso(),
                    facts_version=job["facts_version"] + 1,
                )
                self._enqueue_persistent_error_notification(job)
                return ReconcileDecision(
                    job_id=job_id, facts_version=job["facts_version"] + 1,
                    action=ReconcileAction.PERSISTENT_ERROR, reason=error_code,
                )
            wake = _iso(now + timedelta(seconds=backoff_seconds(retry)))
            self.core._store._update_supervisor_job(
                job_id,
                status=SupervisorJobStatus.BACKOFF.value,
                recovery_state=RecoveryState.RUNTIME_UNKNOWN.value,
                queue_reason=job_state.QueueReason.RETRY_BACKOFF.value,
                next_eligible_at=wake,
                owner_instance_id=None,
                lease_expires_at=None,
                last_error_code=error_code,
                next_action=ReconcileAction.WAIT.value,
                next_wake_at=wake,
                retry_count=retry,
                updated_at=self._now_iso(),
                facts_version=job["facts_version"] + 1,
            )
            return ReconcileDecision(
                job_id=job_id, facts_version=job["facts_version"] + 1,
                action=ReconcileAction.WAIT, reason=error_code, wake_at=wake,
            )

    def _apply_job_backoff(self, job_id: str, error_code: str) -> None:
        """Persist a contention-safe bounded backoff for an apply-fence
        failure (F2/R15): lock unavailable or workspace-identity mismatch.

        Mirrors ``adapter_exception_decision`` (the existing bounded budget):
        increments AND persists ``retry_count``, sets a growing ``next_wake_at``
        (BACKOFF + WAIT via ``backoff_seconds``), and transitions to a sticky
        PERSISTENT_ERROR once ``retry_count >= MAX_RUNTIME_UNKNOWN``.  The
        bounded error lives on the JOB level ONLY: the shared canonical RUNNING
        intent row is NEVER touched, so another lock holder may still be
        executing it (the intent stays RUNNING/UNCERTAIN as appropriate).
        """
        now = self._now()
        with self.core._store._transaction():
            job = self.core._store.get_supervisor_job(job_id)
            if job is None:
                return
            # F1: fence a leased job to its current holder before mutating.
            self._enforce_lease_fence(job)
            retry = job["retry_count"] + 1
            if retry >= MAX_RUNTIME_UNKNOWN:
                self.core._store._update_supervisor_job(
                    job_id,
                    status=SupervisorJobStatus.ERROR.value,
                    recovery_state=RecoveryState.PERSISTENT_ERROR.value,
                    last_error_code=error_code,
                    next_action=ReconcileAction.NONE.value,
                    next_wake_at=None,
                    retry_count=retry,
                    updated_at=self._now_iso(),
                    facts_version=job["facts_version"] + 1,
                )
                self._enqueue_persistent_error_notification(job)
                return
            wake = _iso(now + timedelta(seconds=backoff_seconds(retry)))
            self.core._store._update_supervisor_job(
                job_id,
                status=SupervisorJobStatus.BACKOFF.value,
                recovery_state=RecoveryState.RUNTIME_UNKNOWN.value,
                queue_reason=job_state.QueueReason.RETRY_BACKOFF.value,
                next_eligible_at=wake,
                owner_instance_id=None,
                lease_expires_at=None,
                last_error_code=error_code,
                next_action=ReconcileAction.WAIT.value,
                next_wake_at=wake,
                retry_count=retry,
                updated_at=self._now_iso(),
                facts_version=job["facts_version"] + 1,
            )

    # ---- journal helpers -------------------------------------------------

    def _begin_action(self, key, action_type, job, dispatch_id, args_hash, *,
                      input_hash=None, precondition_hash=None,
                      effect_hash=None, patch_set_json=None):
        """Atomically get-or-create the journal row and advance it to RUNNING.

        Returns ``(row, outcome)`` where outcome is one of
        ``new`` (fresh RUNNING row), ``retry`` (FAILED/PLANNED -> RUNNING,
        attempt_count+1), ``running`` (already RUNNING/UNCERTAIN),
        ``succeeded`` (already SUCCEEDED) or ``exhausted`` (retry budget
        consumed).  ``args_hash`` is compared on replay so a key reuse with
        different arguments fails closed (idempotency).
        """
        now = self._now_iso()
        with self.core._store._transaction():
            return self._begin_action_locked(
                key, action_type, job, dispatch_id, args_hash, now,
                input_hash=input_hash, precondition_hash=precondition_hash,
                effect_hash=effect_hash, patch_set_json=patch_set_json,
            )

    def _begin_action_locked(self, key, action_type, job, dispatch_id, args_hash,
                             now, *, input_hash=None, precondition_hash=None,
                             effect_hash=None, patch_set_json=None):
        """Transaction-scoped core of :meth:`_begin_action` (no own BEGIN)."""
        existing = self.core._store.get_supervisor_action_by_key(key)
        if existing is not None:
            if existing["args_hash"] != args_hash:
                raise IdempotencyError(
                    f"action key {key!r} reused with different args_hash "
                    f"({existing['args_hash']!r} != {args_hash!r})"
                )
            if existing["status"] == "SUCCEEDED":
                return existing, "succeeded"
            if existing["status"] in ("RUNNING", "UNCERTAIN"):
                return existing, "running"
            if existing["status"] in ("PLANNED", "FAILED"):
                if existing["attempt_count"] >= MAX_ACTION_RETRIES:
                    return existing, "exhausted"
                updates = {
                    "status": "RUNNING",
                    "attempt_count": existing["attempt_count"] + 1,
                    "started_at": now,
                    "updated_at": now,
                }
                if input_hash is not None:
                    updates["input_hash"] = input_hash
                if precondition_hash is not None:
                    updates["precondition_hash"] = precondition_hash
                if effect_hash is not None:
                    updates["effect_hash"] = effect_hash
                if patch_set_json is not None:
                    updates["patch_set_json"] = patch_set_json
                self.core._store._update_supervisor_action(existing["id"], **updates)
                return self.core._store.get_supervisor_action(existing["id"]), "retry"
        aid = _sha256(key)[:32]
        row = {
            "id": aid, "supervisor_job_id": job["id"],
            "dispatch_id": dispatch_id, "action_type": action_type,
            "action_key": key, "args_hash": args_hash,
            "input_hash": input_hash, "precondition_hash": precondition_hash,
            "effect_hash": effect_hash, "patch_set_json": patch_set_json,
            "status": "RUNNING", "attempt_count": 1,
            "next_attempt_at": None, "started_at": now, "finished_at": None,
            "last_error_code": None, "created_at": now, "updated_at": now,
        }
        self.core._store._insert_supervisor_action(row)
        return row, "new"

    def _begin_apply_action(self, key, job, dispatch_id, canonical_input_hash,
                            args_hash, *, input_hash=None,
                            precondition_hash=None, effect_hash=None,
                            patch_set_json=None):
        """Atomically claim the single canonical write intent for a dispatch
        AND get-or-create its APPLY_PATCH_SET journal row (R13-F1).

        The claim and the APPLY row INSERT happen in ONE ``BEGIN IMMEDIATE``
        transaction, so no interleaving can observe zero intents twice.  The
        ``dispatch_write_intents.dispatch_id`` primary key is the DB-enforced
        single-intent-per-dispatch invariant.

        Returns ``(conflict, winner_hash, winner_action_id, row, outcome)``:

        - ``conflict=True``: an existing canonical intent with a DIFFERENT
          hash already owns the dispatch (a concurrent controller won the
          claim).  No new row is created; the caller must fail closed and
          reconcile the WINNER's persisted intent.
        - ``conflict=False``: ``(row, outcome)`` is the normal
          ``_begin_action`` result, and ``(winner_hash, winner_action_id)``
          are the canonical binding (equal to this observation's hash).
        """
        now = self._now_iso()
        with self.core._store._transaction():
            winner = self.core._store.get_dispatch_write_intent(dispatch_id)
            if winner is not None:
                if winner["canonical_input_hash"] != canonical_input_hash:
                    return (True, winner["canonical_input_hash"],
                            winner["intent_action_id"], None, None)
                row, outcome = self._begin_action_locked(
                    key, "APPLY_PATCH_SET", job, dispatch_id, args_hash, now,
                    input_hash=input_hash, precondition_hash=precondition_hash,
                    effect_hash=effect_hash, patch_set_json=patch_set_json,
                )
                return (False, winner["canonical_input_hash"],
                        winner["intent_action_id"], row, outcome)
            row, outcome = self._begin_action_locked(
                key, "APPLY_PATCH_SET", job, dispatch_id, args_hash, now,
                input_hash=input_hash, precondition_hash=precondition_hash,
                effect_hash=effect_hash, patch_set_json=patch_set_json,
            )
            self.core._store._insert_dispatch_write_intent({
                "dispatch_id": dispatch_id,
                "canonical_input_hash": canonical_input_hash,
                "intent_action_id": row["id"],
                "created_at": now,
                "updated_at": now,
            })
            return (False, canonical_input_hash, row["id"], row, outcome)

    def _apply_conflict_outcome(self, job, dispatch_id, d, winner_action_id):
        """Fail closed when another controller already owns the dispatch's
        canonical write intent with a DIFFERENT full-result hash (R13-F1).

        Never applies or journals OUR own observation; reconcile the WINNER's
        persisted intent exactly-once (its persisted input/precondition/effect/
        patch set, never the current observation) and report the fail-closed
        hash-mismatch outcome.  The loser's own result is rejected at the
        decision level via the frozen-hash backoff.
        """
        winner_row = self.core._store.get_supervisor_action(winner_action_id)
        if winner_row is not None:
            self._reconcile_existing_apply(job, dispatch_id, d, winner_row)
        return ActionOutcome(
            "APPLY_PATCH_SET", "failed", "write_result_hash_mismatch",
            dispatch_id=dispatch_id,
        )

    def _finish_action(self, action_id, status, error_code=None):
        now = self._now_iso()
        with self.core._store._transaction():
            self.core._store._update_supervisor_action(
                action_id, status=status, finished_at=now,
                last_error_code=error_code, updated_at=now,
            )

    def _frontier_attempt(self, task_id, f=None):
        """The attempt number the next dispatch at the frontier will get."""
        f = f or self.core.workflow_frontier(task_id, self.controller_source)
        matches = [
            d for d in self.core._store.list_dispatches(task_id)
            if d.cycle_no == f.cycle_no and d.position == f.position
        ]
        return f.cycle_no, f.position, 1 + max((d.attempt_no for d in matches), default=0)

    def _latest_action(self, dispatch_id, action_type):
        """The most recent journal row of ``action_type`` for a dispatch."""
        rows = self.core._store.list_supervisor_actions()
        matches = [
            a for a in rows
            if a["dispatch_id"] == dispatch_id and a["action_type"] == action_type
        ]
        if not matches:
            return None
        return max(matches, key=lambda a: a["created_at"])

    @staticmethod
    def _canonical_full_result_hash(result) -> str:
        """Canonical hash of the FULL result (F1), identical to the apply-time
        freeze (``obs.result or {}``).  A missing/falsy result hashes as the
        empty object, exactly as ``_perform_apply_patch_set`` freezes it."""
        return _sha256(_canonical_json(result or {}))

    def _committed_apply_intents(self, dispatch_id: str) -> list:
        """APPLY_PATCH_SET rows that committed to a write, in persistence order.

        A "committed" intent is any APPLY_PATCH_SET row in a non-terminal
        (RUNNING/UNCERTAIN) or SUCCEEDED state; FAILED/PLANNED rows are
        rejections or exhausted retries and never committed a write.  A
        diverged canonical intent stays UNCERTAIN (never FAILED) so it remains
        in this set and keeps its immutable binding forever (F1).  Rows are
        returned in ``rowid`` (insertion) order, so the FIRST element is the
        FIRST persisted intent — the canonical binding for the dispatch.
        """
        rows = self.core._store.list_supervisor_actions()
        return [
            a for a in rows
            if a["dispatch_id"] == dispatch_id
            and a["action_type"] == "APPLY_PATCH_SET"
            and a["status"] in ("RUNNING", "UNCERTAIN", "SUCCEEDED")
        ]

    def _diverged_apply_intent(self, dispatch_id: str) -> Optional[dict]:
        """The canonical apply intent when it is in the diverged/ambiguous state.

        A diverged intent (workspace hash matched neither the persisted
        precondition nor effect after a partial broker-effect crash) is kept as
        ``UNCERTAIN`` + ``last_error_code='workspace_diverged'`` (never FAILED)
        so it stays in ``_committed_apply_intents`` and remains the dispatch's
        canonical binding forever.  Returns None when no such intent exists.
        """
        for a in self._committed_apply_intents(dispatch_id):
            if a["status"] == "UNCERTAIN" \
                    and a.get("last_error_code") == "workspace_diverged":
                return a
        return None

    def _frozen_write_result_hash(self, dispatch_id: str) -> Optional[str]:
        """The persisted frozen write-result hash for a dispatch (F1/R7-F1).

        This is the canonical FULL-result hash persisted atomically with the
        FIRST apply intent (its ``input_hash``) — frozen BEFORE the broker is
        ever invoked and returned regardless of the row's status
        (RUNNING/UNCERTAIN/SUCCEEDED).  It is the single immutable binding for
        the entire write pipeline (APPLY -> RUN_SANDBOX_TESTS ->
        RECORD_TEST_RESULT -> CONSUME_RESULT); every downstream stage
        re-derives and compares against it and never trusts a freshly observed
        result.  Never None while a committed apply intent exists.

        R13-F1: the canonical ``dispatch_write_intents`` table is the
        AUTHORITATIVE source (the winner's hash regardless of the action row's
        status); the status-filtered ``_committed_apply_intents`` view is only
        a fallback for intent rows persisted before this fix (crash-recovery
        fixtures that never went through the atomic claim).
        """
        winner = self.core._store.get_dispatch_write_intent(dispatch_id)
        if winner is not None:
            return winner["canonical_input_hash"]
        intents = self._committed_apply_intents(dispatch_id)
        if not intents:
            return None
        return intents[0].get("input_hash")

    @staticmethod
    def _validate_patch_set(patch_set) -> Optional[str]:
        """Return an error reason if ``patch_set`` is malformed, else None (F2).

        Every entry must be a dict carrying ONLY the write-broker fields
        (``op``/``path``/``content``): ``op`` in ('write','delete'), a non-empty
        string ``path``, and (for 'write') a string ``content``.  The caller has
        already verified ``patch_set`` is a list.
        """
        for patch in patch_set:
            if not isinstance(patch, dict):
                return "invalid_patch_entry_type"
            for key in patch:
                if key not in ("op", "path", "content"):
                    return "invalid_patch_key"
            op = patch.get("op")
            if op not in ("write", "delete"):
                return "invalid_patch_op"
            path = patch.get("path")
            if not isinstance(path, str) or not path:
                return "invalid_patch_path"
            if op == "write":
                content = patch.get("content")
                if not isinstance(content, str) or not content:
                    return "invalid_patch_content"
        return None

    # ---- concrete actions -------------------------------------------------

    def _perform_start_role(self, decision, job):
        task_id = job["task_id"]
        f = self.core.workflow_frontier(task_id, self.controller_source)
        role = f.expected_role
        if role is None:
            return ActionOutcome("START_ROLE", "skipped", "no_role")
        cycle, pos, attempt = self._frontier_attempt(task_id, f)
        key = (f"supervisor:{job['id']}:cycle:{cycle}:pos:{pos}:"
               f"attempt:{attempt}:start-role")
        args_hash = _sha256(_canonical_json({
            "task_id": task_id, "role": role.value,
            "source": self.controller_source,
        }))
        row, outcome = self._begin_action(key, "START_ROLE", job, None, args_hash)
        if outcome == "succeeded":
            return ActionOutcome("START_ROLE", "already_succeeded")
        if outcome == "exhausted":
            return ActionOutcome("START_ROLE", "exhausted")
        if outcome == "running":
            active = self.core._store.get_active_role_run(task_id)
            if active is not None and active.role is role:
                self._finish_action(row["id"], "SUCCEEDED")
                return ActionOutcome("START_ROLE", "executed", "reconciled")
        try:
            self.core.start_role(task_id, role, self.controller_source,
                                 idempotency_key=key)
        except ArgentError as exc:
            self._finish_action(row["id"], "FAILED", f"{type(exc).__name__}")
            return ActionOutcome("START_ROLE", "failed", f"{type(exc).__name__}")
        self._finish_action(row["id"], "SUCCEEDED")
        return ActionOutcome("START_ROLE", "executed")

    def _perform_create_dispatch(self, decision, job):
        task_id = job["task_id"]
        f = self.core.workflow_frontier(task_id, self.controller_source)
        role = f.expected_role
        if role is None:
            return ActionOutcome("CREATE_DISPATCH", "skipped", "no_role")
        task_run = self.core._store.get_latest_task_run(task_id)
        if task_run is None:
            return ActionOutcome("CREATE_DISPATCH", "skipped", "no_task_run")
        cycle, pos, attempt = self._frontier_attempt(task_id, f)
        key = (f"supervisor:{job['id']}:cycle:{cycle}:pos:{pos}:"
               f"attempt:{attempt}:create-dispatch")

        # E2: compute the adaptive routing decision from trusted facts (never
        # agent prose).  A terminal decision (no candidate / owner gate) is
        # fail-closed into the existing BLOCKED mechanism.
        router = self._routing_engine()
        routing_decision = None
        try:
            request = self._build_routing_request(job, task_id, role, cycle, pos, attempt)
            routing_decision = router.route(request, now_iso=self._now_iso())
        except model_router.RoutingError as exc:
            self._close_job(job, "BLOCKED", reason=exc.code)
            return ActionOutcome("CREATE_DISPATCH", "failed", exc.code)
        if routing_decision.is_terminal:
            # E3: a NO_VALID_FALLBACK decision is provider/model unavailability
            # (NOT a capability gap) — it must re-queue with bounded backoff,
            # never become a permanent BLOCKED.  NO_ELIGIBLE_CANDIDATE (floor/
            # evidence) and OWNER_GATE stay fail-closed BLOCKED.
            if routing_decision.decision_reason_code == model_router.RoutingReasonCode.NO_VALID_FALLBACK.value:
                return self._provider_unavailable_backoff(job)
            self._close_job(job, "BLOCKED", reason=routing_decision.decision_reason_code)
            return ActionOutcome("CREATE_DISPATCH", "failed",
                                 routing_decision.decision_reason_code)
        # F6(c): the decision must be bound to THIS job (the Core verifies
        # task/role/policy/level/reason/SHA; the job binding is supervisor-side).
        if routing_decision.job_id != job["id"]:
            self._close_job(job, "BLOCKED", reason="ROUTING_DECISION_JOB_MISMATCH")
            return ActionOutcome("CREATE_DISPATCH", "failed", "job_mismatch")

        args_hash = _sha256(_canonical_json({
            "task_id": task_id, "task_run_id": task_run.id, "role": role.value,
            "position": pos, "cycle_no": cycle,
            "sequence_kind": f.sequence_kind.value, "model_choice": None,
            "source": self.controller_source, "parent_dispatch_id": None,
            "routing_decision": routing_decision.sha256,
        }))
        row, outcome = self._begin_action(key, "CREATE_DISPATCH", job, None, args_hash)
        if outcome == "succeeded":
            return ActionOutcome("CREATE_DISPATCH", "already_succeeded")
        if outcome == "exhausted":
            return ActionOutcome("CREATE_DISPATCH", "exhausted")
        if outcome == "running":
            af = self.store._dispatch_at_frontier(task_id, f)
            if af is not None and af.attempt_no == attempt:
                self._finish_action(row["id"], "SUCCEEDED")
                return ActionOutcome("CREATE_DISPATCH", "executed", "reconciled",
                                     dispatch_id=af.id)
        try:
            d = self.core.create_dispatch(
                task_id, task_run.id, role, pos, cycle, f.sequence_kind, None,
                self.controller_source, idempotency_key=key,
                routing_decision=routing_decision,
            )
        except ArgentError as exc:
            self._finish_action(row["id"], "FAILED", f"{type(exc).__name__}")
            return ActionOutcome("CREATE_DISPATCH", "failed",
                                 f"{type(exc).__name__}")
        self._finish_action(row["id"], "SUCCEEDED")
        return ActionOutcome("CREATE_DISPATCH", "executed", dispatch_id=d.id)

    def _provider_unavailable_backoff(self, job) -> ActionOutcome:
        """Bounded backoff for a NO_VALID_FALLBACK routing decision (E3).

        Provider/model unavailability with no valid fallback is NOT a permanent
        BLOCKED: it re-queues with the existing bounded retry budget (growing
        ``backoff_seconds``), transitioning to a sticky PERSISTENT_ERROR once
        the budget is exhausted.  Never a silent weaker-substitute dispatch.

        F1 (E3 fix-round): the backoff write MUST transition ``primary_state``
        atomically (RUNNING -> QUEUED) AND release the lease via the central
        ``_transition_job`` primitive.  The previous ``_update_supervisor_job``
        write left a leased job RUNNING + BACKOFF + owner-NULL + lease-NULL —
        neither claimable (RUNNING is never claimable) nor recoverable (no
        concrete expired lease) — an unrecoverable corpse.
        """
        now = self._now()
        error_code = model_router.RoutingReasonCode.NO_VALID_FALLBACK.value
        with self.core._store._transaction():
            cur = self.core._store.get_supervisor_job(job["id"])
            if cur is None or cur["terminal"] is not None:
                return ActionOutcome("CREATE_DISPATCH", "skipped", "job_terminal")
            # F1: a backoff write is an authoritative commit; fence a leased
            # job to its current holder before mutating.
            self._enforce_lease_fence(cur)
            retry = cur["retry_count"] + 1
            if retry >= MAX_RUNTIME_UNKNOWN:
                # Sticky PERSISTENT_ERROR (consistent with ``_persist_error``):
                # status=ERROR short-circuits ``_decide`` forever, so no
                # RUNNING corpse is left in the *unclaimable* sense — the job
                # is intentionally terminal-side and requires owner action.
                self.core._store._update_supervisor_job(
                    job["id"],
                    status=SupervisorJobStatus.ERROR.value,
                    recovery_state=RecoveryState.PERSISTENT_ERROR.value,
                    last_error_code=error_code,
                    next_action=ReconcileAction.NONE.value,
                    next_wake_at=None,
                    retry_count=retry,
                    updated_at=self._now_iso(),
                    facts_version=cur["facts_version"] + 1,
                )
                self._enqueue_persistent_error_notification(cur)
                return ActionOutcome("CREATE_DISPATCH", "failed", error_code)
            wake = _iso(now + timedelta(seconds=backoff_seconds(retry)))
            owner = cur.get("owner_instance_id")
            epoch = cur["lease_epoch"]
            fields = {
                "queue_reason": job_state.QueueReason.RETRY_BACKOFF.value,
                "next_eligible_at": wake,
                "last_error_code": error_code,
                "next_action": ReconcileAction.WAIT.value,
                "next_wake_at": wake,
                "retry_count": retry,
            }
            if owner is not None:
                # Fenced release: QUEUED + BACKOFF + lease release in ONE atomic
                # transition (holder CAS), so the job is claimable again after
                # ``next_eligible_at`` and never stranded as RUNNING.
                self.core._store._transition_job(
                    job["id"],
                    to_primary_state=job_state.PrimaryState.QUEUED.value,
                    to_status=SupervisorJobStatus.BACKOFF.value,
                    fields={**fields, "owner_instance_id": None,
                            "lease_expires_at": None},
                    bump_facts_version=True,
                    cas_owner_instance_id=owner,
                    cas_lease_epoch=epoch,
                    cas_lease_unexpired=True,
                )
            else:
                self.core._store._transition_job(
                    job["id"],
                    to_primary_state=job_state.PrimaryState.QUEUED.value,
                    to_status=SupervisorJobStatus.BACKOFF.value,
                    fields=fields,
                    bump_facts_version=True,
                )
        return ActionOutcome("CREATE_DISPATCH", "wait", error_code)

    # -- E2 routing helpers -------------------------------------------------

    def _routing_engine(self):
        """Lazily build the adaptive model router over the core's registry."""
        if self._router is None:
            self._router = model_router.ModelRouter(
                registry=self.core._model_registry(),
            )
        return self._router

    def _build_routing_request(self, job, task_id, role, cycle, pos, attempt):
        task = self.core._store.get_task(task_id)
        risk_class = task.risk_class.value if task is not None else "NORMAL"
        evidence = self._build_routing_evidence(task_id, job, role)

        reference_model_id = None
        independence = None
        if role is Role.REVIEWER:
            # F1: a closing review is ALWAYS writer-independent (bootstrap
            # semantics).  The hard constraint is set unconditionally; when the
            # task has no valid writer reference, the router fails closed
            # (terminal NO_ELIGIBLE_CANDIDATE -> BLOCKED), never a same-model
            # fallback.
            independence = "DIFFERENT_MODEL_REQUIRED"
            writer_id = job.get("writer_dispatch_id")
            if writer_id:
                writer = self.core._store.get_dispatch(writer_id)
                if writer is not None and writer.expected_model_class:
                    reference_model_id = writer.expected_model_class

        current = self._current_escalation_level(task_id, role)
        return model_router.RoutingRequest(
            job_id=job["id"],
            task_id=task_id,
            role=role.value,
            risk_class=risk_class,
            reference_model_id=reference_model_id,
            independence_requirement=independence,
            evidence=evidence,
            current_escalation_level=current,
            availability_snapshot=self._build_availability_snapshot(task_id),
        )

    def _build_availability_snapshot(
        self, task_id: str
    ) -> model_router.AvailabilitySnapshot:
        """Assemble the deterministic availability snapshot from trusted facts.

        The snapshot is the registry default plus OBSERVED deviations: a prior
        dispatch whose bounded ``attempt_outcome`` is ``PROVIDER`` (a provider/
        model registry/validation failure, i.e. a HARD unavailability — never a
        transient EXTERNAL/TRANSIENT) marks that specific model UNAVAILABLE.

        Provider-wide unavailability remains expressible via ``provider_states``
        (the router honours both); the bootstrap builder synthesises model-level
        facts only, because a persisted PROVIDER outcome is attributed to the
        specific model that failed validation.  Transient/rate-limit failures
        map to the existing backoff/WAIT mechanism and are NEVER marked here.
        """
    def _build_availability_snapshot(
        self, task_id: str
    ) -> model_router.AvailabilitySnapshot:
        """Assemble the deterministic availability snapshot from trusted facts.

        The snapshot is the registry default plus OBSERVED deviations: a prior
        dispatch whose bounded ``attempt_outcome`` is ``PROVIDER`` (a provider/
        model registry/validation failure, i.e. a HARD unavailability — never a
        transient EXTERNAL/TRANSIENT) marks that specific model UNAVAILABLE.

        F2 (E3 fix-round): the observation is BOUNDED.  Only the MOST RECENT
        outcome per model counts, and only within
        ``AVAILABILITY_OBSERVATION_TTL_SECONDS``: an expired PROVIDER outcome,
        or a later AVAILABLE/SUCCESS observation, leaves the model available
        again (never poisoned forever).  The history scan is limited to the
        current bounded window.

        Provider-wide unavailability remains expressible via ``provider_states``
        (the router honours both); the bootstrap builder synthesises model-level
        facts only, because a persisted PROVIDER outcome is attributed to the
        specific model that failed validation.  Transient/rate-limit failures
        map to the existing backoff/WAIT mechanism and are NEVER marked here.
        """
        now_s = self._now().timestamp()
        latest: dict = {}  # model_id -> (obs_s, outcome)
        for d in self.core._store.list_dispatches(task_id):
            if not d.expected_model_class:
                continue
            outcome = d.attempt_outcome
            if outcome not in (
                model_router.ATTEMPT_OUTCOME_PROVIDER,
                model_router.ATTEMPT_OUTCOME_SUCCESS,
            ):
                continue
            obs_iso = d.consumed_at or d.started_at or d.created_at
            obs_s = _safe_parse_iso(obs_iso)
            if obs_s is None:
                continue
            prev = latest.get(d.expected_model_class)
            if prev is None or obs_s >= prev[0]:
                latest[d.expected_model_class] = (obs_s, outcome)
        model_states: dict = {}
        for model_id, (obs_s, outcome) in latest.items():
            age_s = now_s - obs_s
            if (
                outcome == model_router.ATTEMPT_OUTCOME_PROVIDER
                and 0 <= age_s <= AVAILABILITY_OBSERVATION_TTL_SECONDS
            ):
                model_states[model_id] = "UNAVAILABLE"
        return model_router.AvailabilitySnapshot(model_states=model_states)

    def _current_escalation_level(self, task_id, role) -> int:
        """Max persisted escalation level among prior dispatches for this role.

        (CASE 14: a reopen continues on the reached level, never resets to 0.)
        """
        levels = [
            d.escalation_level
            for d in self.core._store.list_dispatches(task_id)
            if d.role is role
        ]
        return max(levels, default=0)

    def _build_routing_evidence(self, task_id, job, role):
        from .models import SOURCE_CLASS_CONTROLLER
        dispatches = self.core._store.list_dispatches(task_id)
        test_runs = self.core._store.list_test_runs(task_id)
        reviews = self.core._store.list_reviews(task_id)
        findings = self.core._store.list_findings(task_id)

        # F2(b): only controller-persisted test outcomes (the supervisor's
        # RUN_SANDBOX_TESTS -> record_test_run) are controlled test evidence.
        # Agent-written QA test_runs never drive routing triggers.
        controlled_tests = tuple(
            t for t in test_runs if t.source_class == SOURCE_CLASS_CONTROLLER
        )
        tests = tuple(
            (t.result.value if hasattr(t.result, "value") else str(t.result))
            for t in controlled_tests
        )
        # F2(a): reviews carry ONLY canonical verdicts (core canonicalises at
        # persist); the canonical reject is the bounded reviewer signal.
        verdicts = tuple(r.verdict for r in reviews)
        tests_red = bool(tests and tests[-1] == "failed")
        reviewer_rejected = any(v == "reject" for v in verdicts)
        error_class = job.get("error_class") or "NONE"

        prior = []
        for d in dispatches:
            if d.role is not role:
                continue
            # F4: use the outcome persisted at attempt completion; fall back to
            # the deterministic classifier only for legacy/unclassified attempts.
            outcome_class = d.attempt_outcome
            if outcome_class is None:
                outcome_class = model_router.classify_attempt(
                    d.status.value, error_class, tests_red, reviewer_rejected,
                )
            prior.append(model_router.AttemptEvidence(
                attempt_no=d.attempt_no,
                model_id=d.expected_model_class,
                reasoning_level=model_router.thinking_to_reasoning(
                    d.expected_thinking_tier),
                outcome_class=outcome_class,
                status=d.status.value,
                sequence_kind=d.sequence_kind.value,
                escalation_level=d.escalation_level,
            ))
        prior = tuple(sorted(prior, key=lambda a: (a.attempt_no, a.model_id or "")))

        # F2(c): findings influence routing only when controller-confirmed.
        controlled_findings = tuple(
            f for f in findings if f.source_class == SOURCE_CLASS_CONTROLLER
        )
        open_findings = sum(1 for f in controlled_findings if f.status.value == "open")
        confirmed = any(f.status.value == "resolved" for f in controlled_findings)
        task = self.core._store.get_task(task_id)
        risk_high = task.risk_class.value == "HIGH" if task is not None else False
        # security_relevant only from controller facts: task HIGH risk, OR an
        # already-reached escalation level >= 2, OR a controller-confirmed
        # high/critical finding.  Agent severity claims never influence it.
        reached_escalation = self._current_escalation_level(task_id, role) >= 2
        controller_severity = any(
            (f.severity or "").lower() in {"high", "critical"}
            for f in controlled_findings
        )
        security_relevant = risk_high or reached_escalation or controller_severity

        return model_router.RoutingEvidence(
            prior_attempts=prior,
            test_results=tests,
            reviewer_verdicts=verdicts,
            open_findings_count=open_findings,
            confirmed_finding=confirmed,
            security_relevant=security_relevant,
        )

    def _perform_spawn_run(self, decision, job):
        dispatch_id = decision.dispatch_id
        d = self.core._store.get_dispatch(dispatch_id)
        if d is None:
            return ActionOutcome("SPAWN_RUN", "skipped", "dispatch_missing")
        key = f"supervisor:dispatch:{dispatch_id}:spawn"
        args_hash = _sha256(_canonical_json({"dispatch_id": dispatch_id}))
        row, outcome = self._begin_action(key, "SPAWN_RUN", job, dispatch_id, args_hash)
        if outcome == "succeeded":
            return ActionOutcome("SPAWN_RUN", "already_succeeded",
                                 dispatch_id=dispatch_id)
        if outcome == "exhausted":
            # F3: an exhausted SPAWN_RUN (persisted FAILED row with
            # attempt_count >= MAX) must surface as 'exhausted' so the generic
            # sticky-ERROR handler in perform_next_safe_action_if_required
            # persists the job ERROR state.  The launcher is NEVER invoked
            # (no double-spawn).
            return ActionOutcome("SPAWN_RUN", "exhausted",
                                 dispatch_id=dispatch_id)
        if outcome == "running":
            # SPAWN_RUN is the single non-transactional external action: the
            # launcher must never be invoked twice for the same dispatch (§8.2).
            return ActionOutcome("SPAWN_RUN", "already_running",
                                 dispatch_id=dispatch_id)
        # Build the prompt message file (task contract + context).
        try:
            # F1.3: a FRESH C1 admission is enforced HERE at the enforcement
            # point (single source of truth for the effective limits).  Only
            # ALLOW proceeds to enforcement + spawn; DEFER/DENY_LOCAL/
            # PREFER_EXTERNAL (non-local) fail-closed into a requeue.
            admission = self._fresh_admission(job)
            if admission.decision != AdmissionVerdict.ALLOW.value:
                reason = admission.reason_code \
                    or ResourceReasonCode.RESOURCE_ENFORCEMENT_UNAVAILABLE.value
                self._finish_action(row["id"], "FAILED",
                                    f"admission:{admission.decision}")
                return ActionOutcome("SPAWN_RUN", "resource_enforcement_failed",
                                     reason, dispatch_id=dispatch_id)
            # F1.2: enforcement is MANDATORY — no enforcer => fail-closed for
            # ALL classes (incl. LIGHT), no legacy ``launcher.spawn`` fallback.
            if self._enforcer is None:
                self._finish_action(row["id"], "FAILED", "no_enforcer")
                return ActionOutcome(
                    "SPAWN_RUN", "resource_enforcement_failed",
                    ResourceReasonCode.RESOURCE_ENFORCEMENT_UNAVAILABLE.value,
                    dispatch_id=dispatch_id,
                )
            # D1: build the immutable Context Pack BEFORE the message file /
            # spawn.  A build failure (CONTEXT_BUDGET_EXCEEDED / invalid) is a
            # fail-closed orchestration error: NO dispatch, NO legacy prompt
            # fallback (§23-J/§30).
            pack = self._build_context_pack(d, job)
            # F3: the integration point re-validates ANY builder result before
            # persistence / message-file rendering / spawn.  An injected builder
            # must never slip a formally-invalid pack through.
            validate_context_pack(pack)
            pack_id = self._persist_context_pack(pack)
            message_file = self._build_message_file(d, pack, pack_id=pack_id)
            # F1: re-assert the lease fence IMMEDIATELY before the external
            # spawn effect (a stale holder must never launch an agent).
            self._recheck_lease_fence(job["id"])
            outcome = self._spawn_scoped(d, job, message_file, row, admission)
            if outcome is not None:
                return outcome
        except ContextError as exc:
            # Context errors (build/retrieval/checkpoint/handoff) are
            # ORCHESTRATION errors — never CODE_FAILURE, never a resource/model
            # failure.  No spawn happened.
            self._finish_action(row["id"], "FAILED", exc.code)
            return ActionOutcome("SPAWN_RUN", "context_build_failed",
                                 exc.code, dispatch_id=dispatch_id)
        except Exception as exc:
            self._finish_action(row["id"], "FAILED", f"{type(exc).__name__}")
            return ActionOutcome("SPAWN_RUN", "failed", str(exc),
                                 dispatch_id=dispatch_id)
        self._finish_action(row["id"], "SUCCEEDED")
        return ActionOutcome("SPAWN_RUN", "executed", dispatch_id=dispatch_id)

    def _default_active_jobs_reader(self):
        """Store-backed active-jobs reader for the default host provider (F1)."""
        store = self.core._store

        def reader():
            try:
                rows = store.list_supervisor_jobs()
            except Exception:
                return None
            try:
                return [
                    (row["id"], row.get("resource_class") or ResourceClass.LIGHT.value)
                    for row in rows
                    if row.get("terminal") is None
                    and row.get("primary_state") == job_state.PrimaryState.RUNNING.value
                ]
            except Exception:
                return None

        return reader

    def _fresh_admission(self, job: dict):
        """Fresh C1 admission at the enforcement point (F1.3).

        Uses the SAME injected governor/snapshot provider the Scheduler wires
        (``self._resource_governor``/``self._snapshot_provider``), falling back
        to the real (read-only) defaults when none are injected.  The effective
        limits come from THIS admission — a single source, never a separate
        recomputation.
        """
        from .host_snapshot import HostSnapshotProvider
        from .resource_governor import ResourceGovernor

        governor = self._resource_governor or ResourceGovernor()
        provider = self._snapshot_provider or HostSnapshotProvider(
            active_jobs_reader=self._default_active_jobs_reader(),
        )
        rc = ResourceClass(job.get("resource_class") or ResourceClass.LIGHT.value)
        snapshot = provider.capture(self._workspace_root)
        return governor.decide(
            resource_class=rc, snapshot=snapshot, now_iso=self._now_iso(),
        )

    def _spawn_scoped(self, d, job, message_file, row, admission):
        """C2 scoped spawn (Start-Barrier + verify + registry binding).

        Returns ``None`` on success (the caller finalizes the SPAWN_RUN action
        as SUCCEEDED) or a bounded ``ActionOutcome`` on enforcement failure
        (fail-closed: no unbounded process is ever started).  A cleanup that
        could not be proven inactive maps to ``resource_enforcement_lost``
        (LOST quarantine, never a requeue — F2).
        """
        from .resource_governor import ResourceReasonCode
        from .scope_enforcer import EnforcementStatus

        dispatch_id = d.id
        effective_limits = admission.effective_limits or {}

        # F8: persist the launch count BEFORE the scoped spawn (no-double-spawn
        # proof), mirroring the legacy launcher path.
        increment = getattr(self._launcher, "increment_counter", None)
        if increment is not None:
            increment(dispatch_id)

        command = build_agent_command(
            agent_id=AGENT_IDS[d.role], dispatch_id=dispatch_id,
            message_file=message_file, timeout_seconds=AGENT_TIMEOUT_SECONDS,
        )
        result = self._enforcer.enforce_and_spawn(
            command=command,
            effective_limits=effective_limits,
            resource_class=admission.resource_class,
            policy_version=admission.policy_version,
            job_id=job["id"],
            dispatch_id=dispatch_id,
        )
        if not result.ok:
            self._finish_action(row["id"], "FAILED", f"enforcement:{result.status}")
            if result.status == EnforcementStatus.SCOPE_CLEANUP_UNVERIFIED.value:
                return ActionOutcome(
                    "SPAWN_RUN", "resource_enforcement_lost",
                    result.status, dispatch_id=dispatch_id,
                )
            return ActionOutcome(
                "SPAWN_RUN", "resource_enforcement_failed",
                ResourceReasonCode.RESOURCE_ENFORCEMENT_UNAVAILABLE.value,
                dispatch_id=dispatch_id,
            )
        # Scope created + verified; bind process evidence (scope_ref, class,
        # policy version, effective limits).  The identity comes from the LOCAL
        # provider, never agent output.
        self._register_process_evidence(
            job["id"], dispatch_id, result.scope.process_id, scope=result.scope,
        )
        return None

    def _register_process_evidence(
        self, job_id: str, dispatch_id, pid, *, scope=None,
    ) -> None:
        """Register process evidence at the trusted spawn path (F2, fail-closed).

        The identity comes from the LOCAL provider, never agent output.  An
        unreadable identity is persisted as UNKNOWN (never a concrete tuple); a
        registration failure leaves NO registration, which restart recovery
        treats as unknown -> LOST (never "surely dead"/takeover).  This must
        never flip the (already detached) spawn to FAILED.

        C2: when ``scope`` is provided (scoped spawn), its bounded metadata
        (scope_ref / resource_class / policy_version / effective_limits) is
        persisted alongside the identity and the cgroup path.
        """
        if pid is None or self._process_registry is None:
            return
        identity_provider = self._process_identity_provider \
            or ProcessIdentityProvider()
        try:
            identity = identity_provider.current(pid)
        except Exception:
            identity = ProcessIdentity(boot_id=None, pid=pid,
                                       process_start_ticks=None)
        try:
            self._process_registry.register(
                job_id=job_id, dispatch_id=dispatch_id, identity=identity,
                cgroup_ref=getattr(scope, "cgroup_path", None) if scope else None,
                scope_ref=getattr(scope, "unit_name", None) if scope else None,
                resource_class=getattr(scope, "resource_class", None) if scope else None,
                policy_version=getattr(scope, "policy_version", None) if scope else None,
                effective_limits=getattr(scope, "effective_limits", None) if scope else None,
                # F5: the bounded memory.events baseline captured at scope build
                # is persisted so a later terminal detection can compute the delta.
                scope_events=getattr(scope, "memory_events_baseline", None) if scope else None,
            )
        except Exception:
            # A store failure leaves NO registration; recovery treats a missing
            # registration as unknown evidence -> LOST (fail-closed).
            pass

    def _capability_for(self, model_class: Optional[str]) -> str:
        """Map a trusted dispatch model class to a capability tier (D1).

        The budget tier is selected from the trusted ``expected_model_class``
        (set by the controller via :mod:`routing`), NEVER from agent output.
        Conservative default is PRO (the ordinary writer tier).
        """
        mc = (model_class or "").lower()
        if "flash" in mc:
            return CapabilityTier.FLASH.value
        if "sol" in mc:
            return CapabilityTier.SOL.value
        return CapabilityTier.PRO.value

    def _build_context_pack(self, d: AgentDispatch, job):
        """Build the immutable Context Pack from trusted local facts (D1).

        Inputs come exclusively from the Core ledger / static policy / bounded
        repo facts — never from agent text.  Raises :class:`ContextBuildError`
        (fail-closed) on any budget/validation violation.
        """
        task = self.core._store.get_task(d.task_id)
        if task is None:
            raise ContextBuildError("CONTEXT_MISSING_TASK",
                                    f"task {d.task_id!r} not found")
        # F1: title AND description are both part of the owner task contract and
        # must never be silently trimmed.  Fold them losslessly into the single
        # REQUIRED OWNER_INSTRUCTION objective (the builder assigns that slot
        # TrustClass.OWNER_INSTRUCTION / Importance.REQUIRED) — never a trimmable
        # NORMAL fact.
        title = task.title or ""
        if task.description:
            objective = f"Title: {title}\nDescription: {task.description}"
        else:
            objective = title
        facts = [
            FactInput(f"task_id: {d.task_id}", source_ref="task.id",
                      importance=Importance.REQUIRED.value),
            FactInput(f"dispatch_id: {d.id}", source_ref="dispatch.id",
                      importance=Importance.REQUIRED.value),
            FactInput(f"risk_class: {task.risk_class.value}",
                      source_ref="task.risk_class"),
            FactInput(f"state: {task.state.value}", source_ref="task.state"),
        ]
        constraints = tuple(PROJECT_RULES) + tuple(SECURITY_ARCH_RULES)
        capability = self._capability_for(d.expected_model_class)

        # D2: when retrieval/checkpoint wiring is present, enrich the pack with
        # bounded prior-handoff refs (AGENT_RESULT) and/or a checkpoint resume.
        # D1 remains the single budget/integrity authority; without D2 wiring
        # the path is byte-identical to D1.
        if self._retriever is not None or self._checkpoint_store is not None:
            requests = []
            if self._retriever is not None:
                requests.append(RetrievalRequest(
                    job_id=job["id"], dispatch_id=d.id,
                    source_type=RetrievalType.HANDOFF_LOOKUP,
                    task_id=d.task_id, max_results=16,
                ))
            checkpoint = None
            checkpoint_current_facts = None
            if self._checkpoint_store is not None:
                checkpoint = self._checkpoint_store.latest_checkpoint(job["id"])
                if checkpoint is not None:
                    # F1: current trusted facts are OBLIGATORY for a resume —
                    # assembled from the Store + git provenance (fail-closed if
                    # incomplete, never guessed).
                    checkpoint_current_facts = \
                        self._checkpoint_store.current_facts(job["id"])
            return build_pack_with_retrieval(
                context_builder=self._context_builder,
                job_id=job["id"], dispatch_id=d.id, role=d.role.value,
                objective=objective, constraints=constraints,
                facts=tuple(facts), capability=capability,
                retriever=self._retriever,
                retrieval_requests=requests,
                checkpoint=checkpoint,
                checkpoint_current_facts=checkpoint_current_facts,
                now_iso=self._now_iso(),
            )

        return self._context_builder.build(
            job_id=job["id"],
            dispatch_id=d.id,
            role=d.role.value,
            objective=objective,
            constraints=constraints,
            facts=tuple(facts),
            capability=capability,
            now_iso=self._now_iso(),
        )

    def _persist_context_pack(self, pack) -> str:
        """Persist bounded pack metadata (idempotent, fail-closed on drift).

        Only bounded metadata is stored in SQLite (``context_packs``); large
        pack contents live in the message file / persistent artifacts, never in
        a DB blob.  Rebuilding the same trusted inputs yields the same
        ``content_hash`` (and now the same content-stable ``context_pack_id``,
        F2), so a repeated build reuses the existing row.  Returns the canonical
        ``context_pack_id`` to render (the message file must carry the EXACT
        persisted id).  A different hash for the same dispatch is a stale pack
        (fail-closed).
        """
        with self.core._store._transaction():
            existing = self.core._store.get_context_pack(pack.dispatch_id)
            if existing is not None:
                if existing.content_hash == pack.content_hash:
                    return existing.context_pack_id  # idempotent re-build
                raise ContextBuildError(
                    "CONTEXT_STALE_PACK",
                    f"dispatch {pack.dispatch_id!r} already has a pack with a "
                    "different content hash",
                )
            self.core._store._insert_context_pack(ContextPackRecord(
                context_pack_id=pack.context_pack_id,
                dispatch_id=pack.dispatch_id,
                job_id=pack.job_id,
                role=pack.role,
                version=pack.version,
                content_hash=pack.content_hash,
                size_estimate=pack.budget_estimated,
                token_count=pack.token_count,
                soft_budget=pack.budget_soft,
                hard_budget=pack.budget_hard,
                expansion_reason=pack.expansion_reason,
                artifact_location=None,
                created_at=pack.created_at,
            ))
            return pack.context_pack_id

    def _build_message_file(self, d: AgentDispatch, pack=None, pack_id=None) -> Path:
        """Write the agent prompt to a temp file.

        D1: when ``pack`` is provided (the only D1-migrated dispatch path), the
        file is rendered from the immutable Context Pack via
        :func:`render_pack` (with ``pack_id`` overriding the id so the message
        file carries the EXACT persisted id — F2).  ``pack=None`` is the LEGACY
        minimal-prompt path used ONLY by non-D1-migrated callers (none exist
        today; kept isolated and documented in PHASE_D1_NOTES.md — a migrated
        dispatch must NEVER fall back to it).
        """
        import tempfile
        if pack is not None:
            prompt = render_pack(pack, context_pack_id=pack_id)
        else:
            task = self.core._store.get_task(d.task_id)
            prompt = (
                "You are an agent in a deterministic, isolated development team.\n"
                f"task_id: {d.task_id}\n"
                f"dispatch_id: {d.id}\n"
                f"role: {d.role.value}\n"
                f"title: {task.title}\n"
                f"{task.description or ''}\n"
                "Reply with exactly one JSON object matching your role schema.\n"
            )
        fd, path = tempfile.mkstemp(suffix=".md", prefix="argent-supervisor-", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(prompt)
        return Path(path)

    # -- D2: structured handoff + checkpoint persistence --------------------

    def _write_scope_paths(self, dispatch_id: str) -> frozenset:
        """Authoritative broker write paths for a dispatch (patch_set_json).

        The dispatch's committed APPLY_PATCH_SET rows carry the persisted
        ``patch_set_json`` of the writes the broker actually applied.  Their
        ``path`` fields are the ONLY broker-authoritative write scope for this
        dispatch (never agent prose).  Empty on any parse failure (fail-closed).
        """
        paths = set()
        for a in self._committed_apply_intents(dispatch_id):
            raw = a.get("patch_set_json")
            if not raw:
                continue
            try:
                patch_set = json.loads(raw)
            except Exception:
                continue
            if not isinstance(patch_set, list):
                continue
            for patch in patch_set:
                if isinstance(patch, dict):
                    p = patch.get("path")
                    if isinstance(p, str) and p:
                        paths.add(p)
        return frozenset(paths)

    def _artifact_write_scope(self, d, job) -> frozenset:
        """Authoritative write/diff scope (broker evidence + git diff) for F1.

        The union of the dispatch's broker write paths and the job worktree's
        ``git diff --name-only HEAD`` (bounded).  A declared ``changed_files``
        path is only accepted as an artifact ref when it is confirmed here.
        """
        scope = set(self._write_scope_paths(d.id))
        worktree = job.get("canonical_worktree_path") or ""
        git = self._git_provenance_provider
        if worktree and git is not None:
            try:
                scope.update(git.changed_paths(worktree))
            except Exception:
                pass
        return frozenset(scope)

    @staticmethod
    def _in_test_scope(ref) -> bool:
        """True if a declared ``tests_run`` ref belongs to the allowed test scope.

        A test path is under a ``tests/`` (or ``test/``) directory, or named
        ``test_*.py`` / ``*_test.py``.  Anything else is NOT test scope and is
        dropped (an agent cannot smuggle arbitrary files through ``tests_run``).
        """
        if not isinstance(ref, str) or not ref.strip():
            return False
        norm = ref.replace("\\", "/")
        parts = [p for p in norm.split("/") if p not in ("", ".")]
        if not parts:
            return False
        base = parts[-1].lower()
        dirs = {p.lower() for p in parts[:-1]}
        if "tests" in dirs or "test" in dirs:
            return True
        if base.startswith("test_") and base.endswith(".py"):
            return True
        if base.endswith("_test.py"):
            return True
        return False

    def _default_handoff_record(self, d, job, envelope):
        """Build a bounded HandoffRecord from a validated envelope (best effort).

        Extracts bounded text fields (status/proposal/own_assessment/findings/
        decision/recommendation/blockers) AND — wired in D3 — bounded
        artifact/diff refs WITH full-file sha256 content hashes + git revision
        (via ``GitProvenanceProvider``) for the files the envelope declares.
        A declared ref is embedded ONLY when it is authoritatively confirmed
        (write/diff scope or allowed test scope — F1) and passes the
        secret/forbidden-path deny-list.  Only refs + hashes + bounded excerpts
        are embedded (never whole files, never secrets).  Unconfirmed/missing/
        unreadable/foreign files are skipped and the handoff stays valid (best
        effort, never block).
        """
        def _bounded(v, limit):
            s = str(v or "")
            return s[:limit]

        outcome = _bounded(envelope.get("status"), 128)
        key_observations = []
        for f in (envelope.get("findings") or [])[:8]:
            key_observations.append(_bounded(f, 1024))
        proposal = _bounded(envelope.get("proposal"), 1024)
        own = _bounded(envelope.get("own_assessment"), 1024)
        if proposal:
            key_observations.append(proposal)
        if own:
            key_observations.append(own)

        decisions = []
        for k in ("decision", "recommendation", "requested_next_state"):
            v = envelope.get(k)
            if v:
                decisions.append(_bounded(v, 1024))

        unresolved = []
        for k in ("blockers", "concerns"):
            for v in (envelope.get(k) or [])[:8]:
                unresolved.append(_bounded(v, 1024))

        # D3 (F1): bounded artifact/diff refs WITH hashes + git revision.  The
        # ref list comes from the envelope's declared changed files / tests, but
        # a ref is ONLY accepted when it is confirmed by authoritative evidence
        # — never agent prose alone:
        #   * ``changed_files`` must be in the write/diff scope (broker write
        #     evidence + ``git diff --name-only HEAD``);
        #   * ``tests_run`` must belong to the allowed test scope.
        # Everything is additionally filtered by the secret/forbidden-path
        # deny-list inside ``build_bounded_artifact_refs``.  Unconfirmed paths
        # are dropped; the handoff stays valid (best effort, never block).
        worktree = job.get("canonical_worktree_path") or ""
        revision = ""
        try:
            if worktree:
                git = self._git_provenance_provider
                revision = (git.head(worktree) if git is not None else "") or ""
        except Exception:
            revision = ""
        revision = revision or job.get("current_head") or \
            job.get("expected_head") or ""

        changed_files = envelope.get("changed_files") or ()
        tests_run = envelope.get("tests_run") or ()
        write_scope = self._artifact_write_scope(d, job)
        confirmed = []
        for ref in changed_files:
            if isinstance(ref, str) and ref in write_scope:
                confirmed.append(ref)
        for ref in tests_run:
            if isinstance(ref, str) and self._in_test_scope(ref):
                confirmed.append(ref)
        artifacts = handoff_mod.build_bounded_artifact_refs(
            confirmed, worktree_root=worktree or None, revision=revision)
        commit_refs = (revision,) if revision else ()
        diff_refs = tuple(sorted({a.ref for a in artifacts}))

        evidence = handoff_mod.HandoffEvidence(
            test_refs=tuple(_bounded(t, 512) for t in
                            (envelope.get("tests_run") or
                             envelope.get("tests") or [])[:16]),
            commit_refs=commit_refs,
            diff_refs=diff_refs,
            trusted_facts=(),
            observations=tuple(key_observations),
        )
        nxt = handoff_mod.HandoffNextStep(
            proposed_capability=_bounded(
                envelope.get("requested_next_state"), 128),
            required_context_refs=(),
        )
        prov = handoff_mod.HandoffProvenance(
            source_agent_id=_bounded(
                d.expected_agent_class or d.role.value, 128),
            source_dispatch_id=d.id,
            trust_class="AGENT_RESULT",
        )
        return handoff_mod.build_handoff_record(
            job_id=job["id"],
            source_dispatch_id=d.id,
            source_role=d.role.value,
            created_at=self._now_iso(),
            result=handoff_mod.HandoffResult(
                outcome=outcome,
                key_observations=tuple(key_observations),
                decisions=tuple(decisions),
                unresolved_questions=tuple(unresolved),
            ),
            artifacts=artifacts,
            evidence=evidence,
            next_step=nxt,
            provenance=prov,
        )

    def _persist_structured_handoff(self, d, job, envelope) -> None:
        """Best-effort structured handoff persistence after a consumed result.

        Never fails the consume; a handoff build/persist error is swallowed
        (the existing minimal ``Handoff`` workflow row already carries the
        workflow transition).
        """
        try:
            builder = self._handoff_builder or self._default_handoff_record
            record = builder(d, job, envelope)
            if record is None:
                return
            existing = self.core._store.get_handoff_v2(record.handoff_id)
            if existing is not None:
                return
            self.core._store._insert_handoff_v2(
                **handoff_mod.handoff_to_store_json(record))
        except Exception:
            pass

    def _create_checkpoint(self, d, job) -> None:
        """Best-effort bounded checkpoint after a consumed agent result.

        INSERT-only; fenced by the current lease holder (a stale holder is
        refused) and the store derives the sequential ``checkpoint_no``.  The
        checkpoint persists REAL refs (last pack id+hash, latest handoff refs,
        artifact refs, bounded progress) — never empty placeholders.  Never
        fails the consume.
        """
        try:
            if self._checkpoint_store is None:
                return
            cs = self._checkpoint_store

            # Real last context pack (id + content hash) for this dispatch.
            pack = self.core._store.get_context_pack(d.id)
            pack_id = pack.context_pack_id if pack is not None else ""
            pack_hash = pack.content_hash if pack is not None else ""

            # Real latest structured handoff (refs + bounded progress).
            latest_ho = self.core._store.get_latest_handoff_v2(job["id"])
            handoff_refs = (latest_ho["handoff_id"],) if latest_ho else ()
            artifact_refs: tuple = ()
            milestones: tuple = ()
            if latest_ho:
                try:
                    arts = json.loads(latest_ho.get("artifacts_json") or "[]")
                    artifact_refs = tuple(
                        (a.get("ref", ""), a.get("content_hash", ""))
                        for a in arts
                        if a.get("ref") and a.get("content_hash")
                    )
                except Exception:
                    artifact_refs = ()
                try:
                    result = json.loads(latest_ho.get("result_json") or "{}")
                    obs = (result.get("key_observations") or [])[
                        :checkpoint_mod.MAX_MILESTONES]
                    milestones = tuple(
                        str(o)[:checkpoint_mod.MAX_MILESTONE_LEN] for o in obs
                    )
                except Exception:
                    milestones = ()

            rec = checkpoint_mod.build_checkpoint_record(
                job_id=job["id"],
                checkpoint_no=1,  # placeholder — the store derives MAX+1
                created_at=self._now_iso(),
                workflow=checkpoint_mod.CheckpointWorkflow(
                    primary_state=job.get("primary_state") or "",
                    logical_step=(job.get("workflow_state") or "")
                    [:checkpoint_mod.MAX_STEP_LEN],
                    attempt_no=job.get("attempt_no") or 0,
                    queue_meta=(),
                ),
                context=checkpoint_mod.CheckpointContext(
                    last_context_pack_id=pack_id,
                    last_context_pack_hash=pack_hash,
                    required_trusted_source_refs=(),
                    selected_artifact_refs=artifact_refs,
                    latest_handoff_refs=handoff_refs,
                ),
                code=checkpoint_mod.CheckpointCode(
                    worktree_path=job.get("canonical_worktree_path") or "",
                    repo_identity=job.get("repo_identity") or "",
                    base_commit=job.get("base_commit") or "",
                    head_commit=job.get("current_head") or job.get("expected_head") or "",
                ),
                progress=checkpoint_mod.CheckpointProgress(
                    completed_milestones=milestones,
                    remaining_milestones=(),
                    unresolved_questions=(),
                ),
            )
            cs.create_checkpoint(
                rec,
                owner_instance_id=self._lease_owner,
                lease_epoch=self._lease_epoch,
            )
        except Exception:
            pass

    def _perform_bind_run(self, decision, job):
        dispatch_id = decision.dispatch_id
        d = self.core._store.get_dispatch(dispatch_id)
        if d is None:
            return ActionOutcome("BIND_RUN", "skipped", "dispatch_missing")
        obs = self._guarded_observe(self._build_lookup(d))
        conflict = self._action_adapter_conflict("BIND_RUN", dispatch_id, obs)
        if conflict is not None:
            return conflict
        if obs.status in (RunStatus.RUNNING, RunStatus.SUCCEEDED,
                          RunStatus.FAILED, RunStatus.CANCELLED):
            if obs.session_id is None or obs.run_id is None:
                return ActionOutcome("BIND_RUN", "skipped", "no_binding_values")
            key = f"supervisor:dispatch:{dispatch_id}:bind:{obs.run_id}"
            args_hash = _sha256(_canonical_json({
                "dispatch_id": dispatch_id,
                "child_session_id": obs.session_id,
                "openclaw_run_id": obs.run_id,
                "actual_provider": obs.provider, "actual_model": obs.model,
                "thinking_tier": obs.thinking_tier,
                "source": self.controller_source,
            }))
            row, outcome = self._begin_action(
                key, "BIND_RUN", job, dispatch_id, args_hash)
            if outcome == "succeeded":
                return ActionOutcome("BIND_RUN", "already_succeeded",
                                     dispatch_id=dispatch_id)
            if outcome == "exhausted":
                return ActionOutcome("BIND_RUN", "exhausted",
                                     dispatch_id=dispatch_id)
            if outcome == "running":
                dd = self.core._store.get_dispatch(dispatch_id)
                if dd is not None and dd.status is DispatchStatus.RUNNING \
                        and dd.child_session_id == obs.session_id \
                        and dd.openclaw_run_id == obs.run_id:
                    self._finish_action(row["id"], "SUCCEEDED")
                    return ActionOutcome("BIND_RUN", "executed", "reconciled",
                                         dispatch_id=dispatch_id)
            # Bind with OBSERVED values only (A2); never the dispatch-expected
            # values (the Core CAS is the provenance boundary).
            try:
                self.core.bind_spawn_result(
                    dispatch_id, obs.session_id, obs.run_id,
                    obs.provider, obs.model, obs.thinking_tier,
                    self.controller_source, idempotency_key=key,
                )
            except ArgentError as exc:
                self._finish_action(row["id"], "FAILED", f"{type(exc).__name__}")
                return ActionOutcome("BIND_RUN", "failed", f"{type(exc).__name__}",
                                     dispatch_id=dispatch_id)
            self._finish_action(row["id"], "SUCCEEDED")
            return ActionOutcome("BIND_RUN", "executed", dispatch_id=dispatch_id)
        return ActionOutcome("BIND_RUN", "skipped", f"status_{obs.status.value}",
                             dispatch_id=dispatch_id)

    @staticmethod
    def _persisted_patch_set(row) -> Optional[list]:
        """The canonical patch set persisted on an apply intent, or None."""
        raw = row.get("patch_set_json")
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, list) else None

    # ---- cross-controller apply fence (R14-F1, SPEC V2C §17) ----------------
    #
    # The DB (dispatch_write_intents PRIMARY KEY + the APPLY action key)
    # guarantees a single canonical INTENT per dispatch, but it does NOT
    # guarantee exclusive broker EXECUTION: two controllers sharing one
    # RUNNING canonical intent can both observe workspace == precondition_hash
    # and both fall through to ``broker.apply_patch_set`` (interleaved
    # multi-file writes, competing rollback snapshots, competing journal
    # completion).  This fence makes broker execution exactly-once across
    # separate Core/SQLite connections.
    #
    # Lock design choice: an ``fcntl.flock`` on a per-dispatch lockfile.  For
    # this single-host local workspace this is the most robust, deadlock-free
    # option:
    #   * advisory, released automatically if the holder dies (no stale-lock
    #     cleanup, no lost-wakeup after a crash in the critical section);
    #   * contends correctly across separate PROCESSES and across separate
    #     THREADS of one process (each ``os.open`` yields a distinct file
    #     description, so two threads do not share the lock);
    #   * independent of the SQLite connection, so it does not interact with
    #     ``BEGIN IMMEDIATE`` transactions or the thread-bound connection.
    # A SQLite advisory-lock row was rejected: holding a ``BEGIN IMMEDIATE``
    # transaction across the broker's file I/O would block every other DB
    # writer (including the journal ``_finish_action`` on the same connection)
    # and is not deadlock-free.  The lockfile lives OUTSIDE the workspace so
    # ``WorkspaceHashProvider.scoped_hash`` (which hashes every file under the
    # workspace) is never perturbed by lock state.

    def _apply_lock_path(self, dispatch_id: str) -> Path:
        # ``self._workspace_root`` is the FROZEN canonical spelling (resolved at
        # ``__init__``), so two controllers naming the same physical workspace
        # through different aliases (symlink vs real path) derive the SAME
        # lockfile and cannot bypass the fence by path spelling.
        root = Path(self._workspace_root)
        return root.parent / APPLY_LOCK_DIRNAME / f"apply-{dispatch_id}.lock"

    def _acquire_dispatch_lock(self, dispatch_id: str):
        """Acquire the per-dispatch apply lock (bounded, fail-closed).

        Returns an open file descriptor holding the exclusive lock, or ``None``
        if the lock could not be acquired within the bounded timeout (the
        caller must then fail closed: no broker invocation without the lock).
        """
        if self._workspace_root is None:
            return None
        try:
            lock_path = self._apply_lock_path(dispatch_id)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return None
        deadline = time.monotonic() + APPLY_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except OSError:
                if time.monotonic() >= deadline:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    return None
                time.sleep(APPLY_LOCK_POLL_SECONDS)

    @staticmethod
    def _release_dispatch_lock(fd) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _apply_patch_set_fenced(self, job, dispatch_id, d, row):
        """Fenced broker critical section (R14-F1, SPEC V2C §17 exactly-once).

        The ONLY entry point that may invoke ``broker.apply_patch_set`` for an
        APPLY intent.  Acquires the per-dispatch interprocess lock BEFORE the
        workspace-hash recheck + broker invocation and releases it AFTER the
        journal row is updated, so two controllers sharing one canonical
        RUNNING intent can never both execute the broker.

        ``row`` is the persisted APPLY_PATCH_SET row for the current claim
        (``new``/``retry``/``running`` from ``_begin_apply_action``, or an
        existing committed intent from ``_reconcile_existing_apply``).
        """
        fd = self._acquire_dispatch_lock(dispatch_id)
        if fd is None:
            # F2 (R15): lock-acquisition failure (open OSError or timeout) is a
            # bounded job-level backoff -> sticky PERSISTENT_ERROR, NOT a
            # silent ACTIVE retry with retry_count pinned at 0.  The shared
            # canonical RUNNING intent row is left UNTOUCHED (another lock
            # holder may still be executing it); the bounded error lives on the
            # JOB level only.  The broker is never invoked without the lock.
            self._apply_job_backoff(job["id"], "apply_lock_unavailable")
            return ActionOutcome("APPLY_PATCH_SET", "failed", "lock_unavailable",
                                 dispatch_id=dispatch_id)
        try:
            return self._apply_patch_set_locked(job, dispatch_id, d, row)
        finally:
            self._release_dispatch_lock(fd)

    def _apply_patch_set_locked(self, job, dispatch_id, d, row):
        """Critical-section body: decide exactly-once under the apply lock."""
        # REFETCH the persisted row AFTER acquiring the lock.  A winner
        # controller may have finished (SUCCEEDED/FAILED/UNCERTAIN) while we
        # waited for the lock; we must never re-invoke the broker on top of
        # the winner's execution.
        fresh = self.core._store.get_supervisor_action(row["id"])
        if fresh is None:
            fresh = row
        if fresh["status"] == "SUCCEEDED":
            return ActionOutcome("APPLY_PATCH_SET", "already_succeeded",
                                 dispatch_id=dispatch_id)
        if fresh["status"] != "RUNNING":
            # The winner already executed this claim and finished it in a
            # non-success state (FAILED = broker errors/rollback, UNCERTAIN =
            # divergence).  Never re-invoke from this overlap; the bounded
            # retry / sticky-error machinery owns the next attempt.
            if fresh["status"] == "UNCERTAIN":
                return ActionOutcome("APPLY_PATCH_SET", "failed",
                                     "workspace_diverged", dispatch_id=dispatch_id)
            return ActionOutcome("APPLY_PATCH_SET", "failed",
                                 fresh.get("last_error_code") or "broker_failed",
                                 dispatch_id=dispatch_id)
        if self._workspace_root is None:
            return ActionOutcome("APPLY_PATCH_SET", "skipped", "no_workspace_root",
                                 dispatch_id=dispatch_id)
        if self._workspace_state is None:
            self._workspace_state = WorkspaceHashProvider()
        current = self._workspace_state.scoped_hash(self._workspace_root)
        if fresh["effect_hash"] is not None and current == fresh["effect_hash"]:
            # The winner already applied before we acquired the lock.
            self._finish_action(fresh["id"], "SUCCEEDED")
            return ActionOutcome("APPLY_PATCH_SET", "executed", "reconciled",
                                 dispatch_id=dispatch_id)
        if fresh["precondition_hash"] is not None \
                and current == fresh["precondition_hash"]:
            return self._invoke_broker_locked(job, dispatch_id, d, fresh)
        # Neither the persisted precondition nor the persisted effect matches:
        # the workspace diverged (e.g. a hard kill DURING a multi-file broker
        # loop left a partial write).  Keep the canonical intent in a bounded
        # ambiguity state (UNCERTAIN + error code) so it stays in
        # ``_committed_apply_intents`` and remains the dispatch's immutable
        # binding FOREVER (never FAILED — a second result-keyed intent must
        # not be minted while this canonical intent exists).
        self._finish_action(fresh["id"], "UNCERTAIN", "workspace_diverged")
        return ActionOutcome("APPLY_PATCH_SET", "failed", "workspace_diverged",
                             dispatch_id=dispatch_id)

    def _writer_guard_for(self, job, dispatch_id):
        """Build a broker writer-binding guard bound to the full fencing token
        ``(job_id, dispatch_id, owner_instance_id, lease_epoch, facts_version)``
        captured from a FRESH job read at install time (F1).
        """
        from .worktree import writer_guard_for
        fresh = self.core._store.get_supervisor_job(job["id"])
        return writer_guard_for(
            lambda: self.core._store.get_supervisor_job(job["id"]),
            job_id=job["id"],
            dispatch_id=dispatch_id,
            owner_instance_id=(fresh["owner_instance_id"] if fresh else None),
            lease_epoch=(fresh["lease_epoch"] if fresh else None),
            facts_version=(fresh["facts_version"] if fresh else None),
            now_iso=self._now_iso,
        )

    def bind_writer_worktree(
        self,
        job_id: str,
        *,
        dispatch_id: str,
        owner_instance_id: str,
        lease_epoch: int,
        repo_identity: Optional[str] = None,
        base_commit: Optional[str] = None,
        branch_identity: Optional[str] = None,
        canonical_path: Optional[str] = None,
        worktree_root: Optional[str] = None,
        expected_head: Optional[str] = None,
        current_head: Optional[str] = None,
    ) -> dict:
        """Supervisor-authorized writer/worktree binding primitive (F3).

        Validates and persists, atomically with a lease CAS:

        * the writer dispatch (``dispatch_id``);
        * the current owner/epoch (``owner_instance_id``/``lease_epoch``);
        * the canonical worktree path (realpath, no symlink escape, within the
          fixed ``worktree_root`` when given);
        * ``repo_identity``/``base_commit``/``branch_identity`` (validated);
        * ``expected_head``/``current_head`` (real git provenance);
        * ``writer_binding_mode=BOUND``.

        A stale epoch / wrong owner / expired lease raises (fail-closed).
        """
        from .worktree import (
            validate_branch_identity,
            validate_repo_identity,
            validate_worktree_binding_path,
        )
        path = canonical_path if canonical_path is not None else self._workspace_root
        if path is None:
            raise ValueError("no canonical worktree path available to bind")
        canonical = validate_worktree_binding_path(path, base_root=worktree_root)
        repo = validate_repo_identity(repo_identity)
        branch = validate_branch_identity(branch_identity)
        return self.core._store.bind_writer_worktree(
            job_id,
            dispatch_id=dispatch_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            repo_identity=repo,
            base_commit=base_commit,
            branch_identity=branch,
            canonical_worktree_path=canonical,
            expected_head=expected_head,
            current_head=current_head,
        )

    def _git_provenance(self) -> dict:
        """Real git provenance for the canonical workspace (F3, read-only)."""
        prov = self._git_provenance_provider
        root = self._workspace_root
        return {
            "repo_identity": prov.repo_identity(root),
            "base_commit": prov.head(root),
            "branch_identity": prov.branch(root),
            "expected_head": prov.head(root),
        }

    def _invoke_broker_locked(self, job, dispatch_id, d, row):
        """Invoke the broker for a persisted APPLY intent exactly once (locked).

        Re-derives the patch set from the PERSISTED ``patch_set_json`` (never
        from a possibly-changed observation), verifies the persisted args_hash,
        then applies all-or-nothing and reconciles the effect hash.
        """
        patch_set = self._persisted_patch_set(row)
        if patch_set is None:
            # F1: the intent committed (persisted precondition/effect) but its
            # persisted patch set is missing — divergence/ambiguity, NOT a
            # pre-commit rejection.  Keep the row UNCERTAIN so it stays in
            # ``_committed_apply_intents`` and remains the canonical binding.
            self._finish_action(row["id"], "UNCERTAIN", "workspace_diverged")
            return ActionOutcome("APPLY_PATCH_SET", "failed", "workspace_diverged",
                                 dispatch_id=dispatch_id)
        recomputed = _sha256(_canonical_json(
            {"dispatch_id": dispatch_id, "patch_set": patch_set,
             "workspace_root": self._workspace_root}))
        if recomputed != row["args_hash"]:
            # F1 (R15): the persisted intent's workspace identity (or its
            # patch-set/dispatch arguments) disagrees with the local canonical
            # workspace.  Fail CLOSED: never invoke the broker, never mark the
            # canonical intent FAILED (which would drop it from
            # ``_committed_apply_intents`` and allow a second result-keyed
            # intent to be minted).  The bounded error lives on the JOB level.
            self._apply_job_backoff(job["id"], "workspace_identity_mismatch")
            return ActionOutcome("APPLY_PATCH_SET", "failed",
                                 "workspace_identity_mismatch",
                                 dispatch_id=dispatch_id)
        broker = self._broker_factory()
        # F3: establish the writer/worktree binding (atomic, supervisor-
        # authorized) so a BOUND job cannot reach the broker with a NULL
        # binding.  Binding is CAS-fenced against the current lease holder; a
        # stale epoch raises LeaseFencedError and the broker is never invoked.
        # An UNLEASED legacy job (owner NULL, the phase2c single-supervisor
        # path) stays explicitly unbound: no writer binding to enforce.
        fresh_job = self.core._store.get_supervisor_job(job["id"])
        if fresh_job is not None \
                and fresh_job.get("writer_binding_mode") != "BOUND" \
                and fresh_job.get("owner_instance_id") is not None:
            # F3: persist REAL git provenance (repo identity, base commit,
            # branch, expected HEAD) instead of a NULL-provenance BOUND row.
            prov = self._git_provenance()
            self.bind_writer_worktree(
                job["id"],
                dispatch_id=dispatch_id,
                owner_instance_id=fresh_job["owner_instance_id"],
                lease_epoch=fresh_job["lease_epoch"],
                repo_identity=prov["repo_identity"],
                base_commit=prov["base_commit"],
                branch_identity=prov["branch_identity"],
                expected_head=prov["expected_head"],
            )
        # F1: install a writer-binding guard (fresh job read at guard time, so
        # a stale writer binding fails closed after any takeover).  Guard
        # installation is fail-closed: no broker write without an installed
        # guard (a swallowed ``except Exception: pass`` would open a write path
        # that bypasses the binding check).
        try:
            broker._writer_guard = self._writer_guard_for(job, dispatch_id)
        except Exception:
            self._apply_job_backoff(job["id"], "writer_guard_install_failed")
            return ActionOutcome(
                "APPLY_PATCH_SET", "failed", "writer_guard_install_failed",
                dispatch_id=dispatch_id,
            )
        # F1: re-assert the lease fence IMMEDIATELY before the external
        # workspace/broker effect (a stale holder must never write the
        # workspace).
        self._recheck_lease_fence(job["id"])
        res = broker.apply_patch_set(
            self._workspace_root, patch_set, d.role, self.controller_source)
        if res.errors:
            self._finish_action(row["id"], "FAILED", f"broker:{len(res.errors)}")
            return ActionOutcome("APPLY_PATCH_SET", "failed",
                                 f"broker_errors:{len(res.errors)}",
                                 dispatch_id=dispatch_id)
        current = self._workspace_state.scoped_hash(self._workspace_root)
        if current != row["effect_hash"]:
            # F1: the broker was invoked and the workspace no longer equals the
            # persisted effect — divergence AFTER broker invocation, never a
            # plain rejection that drops the canonical binding.
            self._finish_action(row["id"], "UNCERTAIN", "workspace_diverged")
            return ActionOutcome("APPLY_PATCH_SET", "failed", "workspace_diverged",
                                 dispatch_id=dispatch_id)
        # F3: advance the persisted ``current_head`` to the real HEAD after the
        # broker effect (fenced via the action fence inside the transaction).
        current_head = self._git_provenance()["expected_head"]
        with self.core._store._transaction():
            self.core._store._update_supervisor_job(
                job["id"], current_head=current_head,
            )
        self._finish_action(row["id"], "SUCCEEDED")
        return ActionOutcome("APPLY_PATCH_SET", "executed", "reconciled",
                             dispatch_id=dispatch_id)

    def _reconcile_existing_apply(self, job, dispatch_id, d, existing):
        """Reconcile an existing broker-bound apply intent exactly-once (R7-F1).

        A committed intent (persisted precondition/effect hashes) already exists
        for this dispatch.  Reconcile it from the PERSISTED hashes and PERSISTED
        patch set — never re-derive the patch set from the current observation
        (which may describe a DIFFERENT result after a crash) and never mint a
        second intent.
        """
        if existing["status"] == "SUCCEEDED":
            return ActionOutcome("APPLY_PATCH_SET", "already_succeeded",
                                 dispatch_id=dispatch_id)
        # F1: a diverged canonical intent is sticky.  The workspace hash
        # already matched neither the persisted precondition nor effect (a
        # partial broker-effect crash).  NEVER re-examine the workspace,
        # re-apply, mark FAILED (which would drop it from
        # ``_committed_apply_intents`` and let a second result-keyed intent be
        # minted), or advance the dispatch.  The decision table routes it to a
        # bounded sticky PERSISTENT_ERROR.
        if existing["status"] == "UNCERTAIN" \
                and existing.get("last_error_code") == "workspace_diverged":
            return ActionOutcome("APPLY_PATCH_SET", "failed",
                                 "workspace_diverged", dispatch_id=dispatch_id)
        # A no-op intent (empty patch set) persisted no precondition/effect
        # hashes: recover it SUCCEEDED exactly-once (nothing to apply).
        if existing["precondition_hash"] is None \
                and existing["effect_hash"] is None:
            self._finish_action(existing["id"], "SUCCEEDED")
            return ActionOutcome("APPLY_PATCH_SET", "executed", "noop",
                                 dispatch_id=dispatch_id)
        if self._workspace_root is None:
            return ActionOutcome("APPLY_PATCH_SET", "skipped", "no_workspace_root",
                                 dispatch_id=dispatch_id)
        # R14-F1: the workspace-hash recheck + broker invocation run inside the
        # single cross-controller fenced critical section (exactly-once).
        return self._apply_patch_set_fenced(job, dispatch_id, d, existing)

    def _perform_apply_patch_set(self, decision, job):
        dispatch_id = decision.dispatch_id
        d = self.core._store.get_dispatch(dispatch_id)
        if d is None:
            return ActionOutcome("APPLY_PATCH_SET", "skipped", "dispatch_missing")
        obs = self._guarded_observe(self._build_lookup(d))
        conflict = self._action_adapter_conflict("APPLY_PATCH_SET", dispatch_id, obs)
        if conflict is not None:
            return conflict

        # R7-F1 / F1: enforce one-broker-intent-per-dispatch.  If ANY committed
        # apply intent exists (RUNNING/UNCERTAIN/SUCCEEDED — including a
        # diverged UNCERTAIN row), reconcile it exactly-once BEFORE any
        # observation-derived action.  A new observation B must NEVER mint a
        # second result-keyed APPLY intent while the canonical A intent exists.
        committed = self._committed_apply_intents(dispatch_id)
        if committed:
            return self._reconcile_existing_apply(
                job, dispatch_id, d, committed[0])

        result = obs.result or {}

        # F1: recompute the canonical result hash locally and use it for every
        # apply key and input_hash.  An adapter-supplied hash that disagrees
        # with the canonical hash is rejected fail-closed (never trust it).
        canonical_result_hash = _sha256(_canonical_json(result))
        if obs.result_hash is not None and obs.result_hash != canonical_result_hash:
            return self._fail_apply(job, dispatch_id, canonical_result_hash,
                                    "result_hash_mismatch")
        result_hash = canonical_result_hash

        # F1: validate the complete write-result schema (envelope + the role's
        # legitimate patch extension).  Only the role's patch extension is
        # separated; any other field (e.g. the forbidden 'encoded') stays in
        # the envelope and is rejected by validate_role_output (never silently
        # stripped) BEFORE any broker action.
        envelope = _write_envelope(d.role, result)
        try:
            outputs.validate_role_output(d.role, envelope)
        except ArgentError as exc:
            return self._fail_apply(job, dispatch_id, result_hash,
                                    f"invalid_envelope:{type(exc).__name__}")
        if envelope.get("task_id") != d.task_id:
            return self._fail_apply(job, dispatch_id, result_hash, "task_mismatch")
        if envelope.get("dispatch_id") != d.id:
            return self._fail_apply(job, dispatch_id, result_hash, "dispatch_mismatch")

        # F2: distinguish field ABSENCE from PRESENCE.  A PRESENT but non-list
        # patch field (including falsy scalars/objects) is malformed and must
        # fail-closed (never a silent no-op).  Only a legit empty list is a
        # valid no-op.  Each entry is then structurally validated BEFORE any
        # broker action or no-op journaling.
        patch_field = "patch_set" if d.role is Role.IMPLEMENTER else "test_patch_set"
        if patch_field in result:
            raw_patch_set = result[patch_field]
            if not isinstance(raw_patch_set, list):
                return self._fail_apply(job, dispatch_id, result_hash,
                                        "invalid_patch_set_type")
            patch_error = self._validate_patch_set(raw_patch_set)
            if patch_error is not None:
                return self._fail_apply(job, dispatch_id, result_hash, patch_error)
            patch_set = raw_patch_set
        else:
            patch_set = []

        if not patch_set:
            # Nothing to apply: the precondition is trivially satisfied.
            key = f"supervisor:dispatch:{dispatch_id}:apply:{result_hash}"
            (conflict, _winner_hash, winner_action_id,
             row, outcome) = self._begin_apply_action(
                key, job, dispatch_id, result_hash, _sha256("empty"),
                input_hash=result_hash)
            if conflict:
                return self._apply_conflict_outcome(
                    job, dispatch_id, d, winner_action_id)
            if outcome in ("new", "retry", "running"):
                self._finish_action(row["id"], "SUCCEEDED")
            return ActionOutcome("APPLY_PATCH_SET", "executed", "noop",
                                 dispatch_id=dispatch_id)

        if self._workspace_root is None:
            return ActionOutcome("APPLY_PATCH_SET", "skipped", "no_workspace_root",
                                 dispatch_id=dispatch_id)
        if self._workspace_state is None:
            self._workspace_state = WorkspaceHashProvider()

        # F2 §8.3: persist the expected before-hash and the deterministic
        # after-hash BEFORE applying, so a crash between the broker mutation
        # and the journal-success can be reconciled exactly-once.
        precondition_hash = self._workspace_state.scoped_hash(self._workspace_root)
        effect_hash = self._workspace_state.predicted_hash(
            self._workspace_root, patch_set)
        key = f"supervisor:dispatch:{dispatch_id}:apply:{result_hash}"
        # F1 (R15): bind the canonical workspace identity into the apply
        # intent's args_hash, so the persisted intent records WHICH canonical
        # workspace it applies to.  A controller whose frozen workspace root
        # disagrees with the persisted one fails closed at invoke time (never
        # invokes the broker; the bounded error lives on the JOB level).
        args_hash = _sha256(_canonical_json(
            {"dispatch_id": dispatch_id, "patch_set": patch_set,
             "workspace_root": self._workspace_root}))
        (conflict, _winner_hash, winner_action_id,
         row, outcome) = self._begin_apply_action(
            key, job, dispatch_id, result_hash, args_hash,
            input_hash=result_hash, precondition_hash=precondition_hash,
            effect_hash=effect_hash, patch_set_json=_canonical_json(patch_set))
        if conflict:
            return self._apply_conflict_outcome(
                job, dispatch_id, d, winner_action_id)
        if outcome == "succeeded":
            return ActionOutcome("APPLY_PATCH_SET", "already_succeeded",
                                 dispatch_id=dispatch_id)
        if outcome == "exhausted":
            return ActionOutcome("APPLY_PATCH_SET", "exhausted",
                                 dispatch_id=dispatch_id)
        # R14-F1: every broker invocation (new/retry/running) runs inside the
        # single cross-controller fenced critical section, re-decided from the
        # PERSISTED row under the interprocess lock (exactly-once).
        return self._apply_patch_set_fenced(job, dispatch_id, d, row)

    def _fail_apply(self, job, dispatch_id, result_hash, reason):
        """Journal an APPLY_PATCH_SET rejection (bounded, never applies)."""
        key = f"supervisor:dispatch:{dispatch_id}:apply:{result_hash}"
        row, outcome = self._begin_action(
            key, "APPLY_PATCH_SET", job, dispatch_id,
            _sha256(_canonical_json({"invalid": reason})), input_hash=result_hash)
        if outcome not in ("succeeded", "exhausted"):
            self._finish_action(row["id"], "FAILED", reason)
        return ActionOutcome("APPLY_PATCH_SET", "failed", reason,
                             dispatch_id=dispatch_id)

    def _perform_run_sandbox_tests(self, decision, job):
        dispatch_id = decision.dispatch_id
        # F3: the PRODUCTION sandbox run goes through the enforcement path
        # (the bwrap command is built and run INSIDE a bounded scope); the
        # injected ``run_tests_fn`` seam is a deterministic TEST-ONLY
        # substitute (never set in production) so unit tests stay fast and
        # deterministic.  Both need a workspace root.
        if self._workspace_root is None:
            return ActionOutcome("RUN_SANDBOX_TESTS", "skipped", "no_workspace")
        frozen = self._frozen_write_result_hash(dispatch_id)
        if frozen is None:
            return ActionOutcome("RUN_SANDBOX_TESTS", "skipped",
                                 "no_frozen_result_hash")
        d = self.core._store.get_dispatch(dispatch_id)
        if d is None:
            return ActionOutcome("RUN_SANDBOX_TESTS", "skipped", "dispatch_missing")
        # F1: verify the CURRENT observation still describes the frozen result
        # before invoking the sandbox (never test a workspace against a foreign
        # result).
        obs = self._guarded_observe(self._build_lookup(d))
        conflict = self._action_adapter_conflict("RUN_SANDBOX_TESTS", dispatch_id, obs)
        if conflict is not None:
            return conflict
        if self._canonical_full_result_hash(obs.result) != frozen:
            return ActionOutcome("RUN_SANDBOX_TESTS", "skipped",
                                 "result_hash_mismatch")
        key = f"supervisor:dispatch:{dispatch_id}:tests:{frozen}"
        args_hash = _sha256(_canonical_json(
            {"workspace": str(self._workspace_root), "result_hash": frozen}))
        row, outcome = self._begin_action(
            key, "RUN_SANDBOX_TESTS", job, dispatch_id, args_hash)
        if outcome == "succeeded":
            return ActionOutcome("RUN_SANDBOX_TESTS", "already_succeeded")
        if outcome == "exhausted":
            return ActionOutcome("RUN_SANDBOX_TESTS", "exhausted")
        # RUNNING: the bwrap run is read-only; re-running it is safe and
        # bounded by MAX_ACTION_RETRIES via the journal retry budget.
        # F3: the injected ``run_tests_fn`` seam runs the deterministic
        # sandbox substitute (test-only; production never sets it).
        if self._run_tests_fn is not None:
            res = self._run_tests_fn(self._workspace_root)
            self._finish_action(
                row["id"],
                "SUCCEEDED" if res.exit_code == 0 else "FAILED",
                None if res.exit_code == 0 else f"exit_code_{res.exit_code}",
            )
            return ActionOutcome("RUN_SANDBOX_TESTS", "executed",
                                 dispatch_id=dispatch_id)
        # F3 (production): the sandbox test path runs through the SAME
        # enforcement path as spawn — a fresh C1 admission, a bounded scope,
        # bwrap INSIDE the scope, and registry binding (bwrap's own
        # prlimit/timeout remain as defense-in-depth).
        admission = self._fresh_admission(job)
        if admission.decision != AdmissionVerdict.ALLOW.value:
            reason = admission.reason_code \
                or ResourceReasonCode.RESOURCE_ENFORCEMENT_UNAVAILABLE.value
            self._finish_action(row["id"], "FAILED",
                                f"admission:{admission.decision}")
            return ActionOutcome("RUN_SANDBOX_TESTS", "resource_enforcement_failed",
                                 reason, dispatch_id=dispatch_id)
        exit_code = self._run_sandbox_scoped(d, job, dispatch_id)
        # C3: a sandbox termination with resource evidence (OOM / memory-limit /
        # timeout / unknown) is a resource event, never a code failure.  The
        # scheduler performs the fenced recovery commit; here we only SIGNAL it.
        if self._sandbox_resource_termination(dispatch_id):
            self._finish_action(row["id"], "FAILED", "resource_termination")
            return ActionOutcome("RUN_SANDBOX_TESTS", "resource_termination_failed",
                                 dispatch_id=dispatch_id)
        self._finish_action(
            row["id"],
            "SUCCEEDED" if exit_code == 0 else "FAILED",
            None if exit_code == 0 else f"exit_code_{exit_code}",
        )
        return ActionOutcome("RUN_SANDBOX_TESTS", "executed",
                             dispatch_id=dispatch_id)

    def _sandbox_resource_termination(self, dispatch_id) -> bool:
        """True when the sandbox process ended with resource evidence (C3).

        Reads the bounded termination evidence persisted by
        :meth:`_run_sandbox_scoped` and classifies it via the shared C3
        classifier.  A resource failure class routes to the scheduler's fenced
        recovery commit; a NORMAL_EXIT / code failure returns False (the normal
        code-failure workflow continues).
        """
        reg = self.core._store.get_process_registration_for_dispatch(dispatch_id)
        if reg is None:
            return False
        scope_events = None
        if reg.get("scope_events"):
            try:
                import json
                scope_events = json.loads(reg["scope_events"])
            except (ValueError, TypeError):
                scope_events = None
        from .resource_recovery import classify_failure, is_resource_failure
        fc = classify_failure(
            termination_class=reg.get("termination_class"),
            exit_code=reg.get("exit_code"),
            timed_out=bool(reg.get("timed_out")),
            scope_events=scope_events,
        )
        return is_resource_failure(fc)

    def _run_sandbox_scoped(self, d, job, dispatch_id) -> int:
        """Run the bwrap sandbox tests inside an enforced scope (F3/F5).

        Returns the exit code (0 = green).  Enforcement failures return a
        non-zero sentinel so the write pipeline fails closed (never a silent
        pass).  The bwrap process is bound into the registry with its scope
        metadata, then marked TERMINAL with the termination evidence (F5).
        """
        from .resource_governor import ResourceGovernor
        from .sandbox_runner import build_command
        from .scope_enforcer import EnforcementStatus

        governor = self._resource_governor or ResourceGovernor()
        policy = governor.policy
        limits = policy.limits_for(ResourceClass.LIGHT)
        effective_limits = {
            "memory_high_bytes": limits.memory_high_bytes,
            "memory_max_bytes": limits.memory_max_bytes,
            "swap_max_bytes": limits.swap_max_bytes,
            "cpu_quota_percent": limits.cpu_quota_percent,
            "timeout_seconds": limits.timeout_seconds,
        }
        command = build_command(self._workspace_root)
        result = self._enforcer.enforce_and_run(
            command=command,
            effective_limits=effective_limits,
            resource_class=ResourceClass.LIGHT,
            policy_version=policy.policy_version,
            job_id=job["id"],
            dispatch_id=dispatch_id,
        )
        if result.status == EnforcementStatus.SCOPE_CLEANUP_UNVERIFIED.value:
            # C3/F4: an unproven cleanup means a process may STILL be running —
            # this is a resource failure (LOST quarantine), never a code failure
            # and never a silent pass.  Persist authoritative terminal evidence
            # (termination_class=SCOPE_CLEANUP_UNVERIFIED) so
            # :meth:`_sandbox_resource_termination` signals it and the scheduler
            # maps it to QUARANTINE_LOST (no code rework, no retry).
            if result.scope is not None and result.scope.process_id:
                self._register_process_evidence(
                    job["id"], dispatch_id, result.scope.process_id,
                    scope=result.scope,
                )
                reg = self.core._store.get_process_registration_for_dispatch(
                    dispatch_id,
                )
                if reg is not None:
                    self._process_registry.mark_terminal(
                        reg["process_id"],
                        exit_code=result.exit_code,
                        terminal_at=self._now_iso(),
                        termination_class=EnforcementStatus.SCOPE_CLEANUP_UNVERIFIED.value,
                        timed_out=False,
                        scope_events=None,
                    )
            return -1
        if result.scope is None:
            return -1
        if result.scope.process_id:
            self._register_process_evidence(
                job["id"], dispatch_id, result.scope.process_id,
                scope=result.scope,
            )
            reg = self.core._store.get_process_registration_for_dispatch(
                dispatch_id,
            )
            if reg is not None:
                self._process_registry.mark_terminal(
                    reg["process_id"],
                    exit_code=result.exit_code,
                    terminal_at=self._now_iso(),
                    termination_class=result.termination_class,
                    timed_out=result.timed_out,
                    scope_events=result.scope_events,
                )
        return result.exit_code if result.exit_code is not None else -1

    def _perform_record_test_result(self, decision, job):
        dispatch_id = decision.dispatch_id
        d = self.core._store.get_dispatch(dispatch_id)
        if d is None:
            return ActionOutcome("RECORD_TEST_RESULT", "skipped", "dispatch_missing")
        frozen = self._frozen_write_result_hash(dispatch_id)
        if frozen is None:
            return ActionOutcome("RECORD_TEST_RESULT", "skipped",
                                 "no_frozen_result_hash")
        # F1: verify the CURRENT observation still describes the frozen result
        # before recording the test outcome.
        obs = self._guarded_observe(self._build_lookup(d))
        conflict = self._action_adapter_conflict("RECORD_TEST_RESULT", dispatch_id, obs)
        if conflict is not None:
            return conflict
        if self._canonical_full_result_hash(obs.result) != frozen:
            return ActionOutcome("RECORD_TEST_RESULT", "skipped",
                                 "result_hash_mismatch")
        key = f"supervisor:dispatch:{dispatch_id}:record-test:{frozen}"
        passed = self._action_succeeded(dispatch_id, "RUN_SANDBOX_TESTS")
        result = "passed" if passed else "failed"
        role_source = f"role:{d.role.value}"
        args_hash = _sha256(_canonical_json({
            "task_id": d.task_id, "result": result, "source": role_source,
            "result_hash": frozen}))
        row, outcome = self._begin_action(
            key, "RECORD_TEST_RESULT", job, dispatch_id, args_hash)
        if outcome == "succeeded":
            return ActionOutcome("RECORD_TEST_RESULT", "already_succeeded")
        if outcome == "exhausted":
            return ActionOutcome("RECORD_TEST_RESULT", "exhausted")
        if outcome == "running":
            # Reconcile from the persisted test_run row via Core idempotency.
            if self.core._store.get_command_idempotency(key, "record_test_run") is not None:
                self._finish_action(row["id"], "SUCCEEDED")
                return ActionOutcome("RECORD_TEST_RESULT", "executed", "reconciled")
        try:
            self.core.record_test_run(
                d.task_id, result, role_source, detail="supervisor sandbox run",
                idempotency_key=key,
            )
            # F4: bind the controlled sandbox test outcome to the attempt NOW
            # (then-valid facts) — the code either passed or failed.  This is
            # the only authoritative outcome; it is never re-derived later.
            self.core._store._set_dispatch_attempt_outcome(
                dispatch_id,
                "SUCCESS" if result == "passed" else "CAPABILITY",
            )
        except ArgentError as exc:
            self._finish_action(row["id"], "FAILED", f"{type(exc).__name__}")
            return ActionOutcome("RECORD_TEST_RESULT", "failed",
                                 f"{type(exc).__name__}")
        self._finish_action(row["id"], "SUCCEEDED")
        return ActionOutcome("RECORD_TEST_RESULT", "executed",
                             dispatch_id=dispatch_id)

    def _perform_consume_result(self, decision, job):
        dispatch_id = decision.dispatch_id
        d = self.core._store.get_dispatch(dispatch_id)
        if d is None:
            return ActionOutcome("CONSUME_RESULT", "skipped", "dispatch_missing")
        obs = self._guarded_observe(self._build_lookup(d))
        conflict = self._action_adapter_conflict("CONSUME_RESULT", dispatch_id, obs)
        if conflict is not None:
            return conflict
        if obs.status is not RunStatus.SUCCEEDED:
            return ActionOutcome("CONSUME_RESULT", "skipped",
                                 f"status_{obs.status.value}")
        result = obs.result or {}
        envelope = _write_envelope(d.role, result)
        # Validate envelope (fail-closed) before consume.
        try:
            outputs.validate_role_output(d.role, envelope)
        except ArgentError as exc:
            return self._fail_consume(job, dispatch_id, envelope,
                                      f"invalid_envelope:{type(exc).__name__}")
        run_id = obs.run_id or d.openclaw_run_id
        # F1: bind the consume idempotency key to the FULL result hash (not the
        # stripped envelope); for write roles the observation must still equal
        # the persisted frozen hash before consuming.
        full_hash = _sha256(_canonical_json(result))
        if _is_write_role(d.role):
            frozen = self._frozen_write_result_hash(dispatch_id)
            if frozen is None:
                return ActionOutcome("CONSUME_RESULT", "skipped",
                                     "no_frozen_result_hash")
            if full_hash != frozen:
                return ActionOutcome("CONSUME_RESULT", "skipped",
                                     "result_hash_mismatch")
            canonical = _canonical_json(result)  # FULL result (incl. patch ext)
        else:
            canonical = _canonical_json(envelope)
        key = f"supervisor:consume:{dispatch_id}:{run_id}:{_sha256(canonical)}"
        row, outcome = self._begin_action(
            key, "CONSUME_RESULT", job, dispatch_id, _sha256(canonical))
        if outcome == "succeeded":
            return ActionOutcome("CONSUME_RESULT", "already_succeeded")
        if outcome == "exhausted":
            return ActionOutcome("CONSUME_RESULT", "exhausted")
        if outcome == "running":
            # Reconcile from dispatch status: a CONSUMED dispatch means the
            # consume already happened (crash between CAS and journal-success).
            dd = self.core._store.get_dispatch(dispatch_id)
            if dd is not None and dd.status is DispatchStatus.CONSUMED:
                self._finish_action(row["id"], "SUCCEEDED")
                return ActionOutcome("CONSUME_RESULT", "executed", "reconciled",
                                     dispatch_id=dispatch_id)
        event_meta = {
            "task_id": d.task_id,
            "child_session_id": d.child_session_id,
            "run_id": d.openclaw_run_id,
            "parent_dispatch_id": d.parent_dispatch_id,
            "event_type": "agent.completed",
            "status": "completed",
        }
        try:
            res = self.core.receive_agent_result(
                dispatch_id, event_meta, envelope, self.controller_source,
                idempotency_key=key,
            )
        except ArgentError as exc:
            self._finish_action(row["id"], "FAILED", f"{type(exc).__name__}")
            return ActionOutcome("CONSUME_RESULT", "failed",
                                 f"{type(exc).__name__}", dispatch_id=dispatch_id)
        # exactly-once: "duplicate" is a success (already consumed).  Any
        # rejection (wrong provenance, malformed envelope the pre-check
        # missed, task/run mismatch) must FAIL the action so the bounded
        # retry policy applies - never a silent infinite re-plan.
        if res.status in ("consumed", "duplicate"):
            # D2: best-effort structured handoff + bounded checkpoint after a
            # consumed agent result (never fails the consume; the existing
            # minimal workflow handoff already carries the transition).
            self._persist_structured_handoff(d, job, envelope)
            self._create_checkpoint(d, job)
            self._finish_action(row["id"], "SUCCEEDED")
            return ActionOutcome("CONSUME_RESULT", "executed",
                                 detail=res.status, dispatch_id=dispatch_id)
        self._finish_action(row["id"], "FAILED", f"consume_{res.status}")
        return ActionOutcome("CONSUME_RESULT", "failed",
                             f"consume_{res.status}", dispatch_id=dispatch_id)

    def _fail_consume(self, job, dispatch_id, envelope, reason):
        """Journal a malformed-envelope consume rejection (bounded retries)."""
        try:
            canonical = _canonical_json(envelope)
        except Exception:
            canonical = "{}"
        d = self.core._store.get_dispatch(dispatch_id)
        run_id = d.openclaw_run_id if d is not None else "unknown"
        key = f"supervisor:consume:{dispatch_id}:{run_id}:{_sha256(canonical)}"
        row, outcome = self._begin_action(
            key, "CONSUME_RESULT", job, dispatch_id, _sha256(canonical))
        if outcome not in ("succeeded", "exhausted"):
            self._finish_action(row["id"], "FAILED", reason)
        return ActionOutcome("CONSUME_RESULT", "failed", reason,
                             dispatch_id=dispatch_id)

    def _perform_mark_run_failed(self, decision, job):
        dispatch_id = decision.dispatch_id
        d = self.core._store.get_dispatch(dispatch_id)
        if d is None:
            return ActionOutcome("MARK_RUN_FAILED", "skipped", "dispatch_missing")
        # F2 (E3 fix-round): derive the provider/transport signal from the
        # observed run so a provider failure persists ATTEMPT_OUTCOME_PROVIDER
        # (never CAPABILITY).  A missing/unreadable observation -> no signal
        # (plain code failure -> CAPABILITY).
        error_code = None
        obs = self._guarded_observe(self._build_lookup(d))
        if obs is not None:
            error_code = obs.error_code
        run_id = d.openclaw_run_id or "unknown"
        key = f"supervisor:dispatch:{dispatch_id}:fail:{run_id}"
        args_hash = _sha256(_canonical_json({
            "dispatch_id": dispatch_id, "reason": "run_failed",
            "source": self.controller_source, "error_code": error_code}))
        row, outcome = self._begin_action(
            key, "MARK_RUN_FAILED", job, dispatch_id, args_hash)
        if outcome == "succeeded":
            return ActionOutcome("MARK_RUN_FAILED", "already_succeeded",
                                 dispatch_id=dispatch_id)
        if outcome == "exhausted":
            return ActionOutcome("MARK_RUN_FAILED", "exhausted",
                                 dispatch_id=dispatch_id)
        if outcome == "running":
            dd = self.core._store.get_dispatch(dispatch_id)
            if dd is not None and dd.status is DispatchStatus.FAILED:
                self._finish_action(row["id"], "SUCCEEDED")
                return ActionOutcome("MARK_RUN_FAILED", "executed", "reconciled",
                                     dispatch_id=dispatch_id)
        try:
            self.core.mark_agent_failed(dispatch_id, "run_failed",
                                        self.controller_source,
                                        idempotency_key=key,
                                        error_code=error_code)
        except ArgentError as exc:
            self._finish_action(row["id"], "FAILED", f"{type(exc).__name__}")
            return ActionOutcome("MARK_RUN_FAILED", "failed",
                                 f"{type(exc).__name__}", dispatch_id=dispatch_id)
        self._finish_action(row["id"], "SUCCEEDED")
        return ActionOutcome("MARK_RUN_FAILED", "executed",
                             dispatch_id=dispatch_id)

    def _perform_core_recover(self, decision, job):
        key = f"supervisor:{job['id']}:core-recover"
        args_hash = _sha256(_canonical_json({"source": self.owner_source}))
        row, outcome = self._begin_action(key, "CORE_RECOVER", job, None, args_hash)
        if outcome == "succeeded":
            return ActionOutcome("CORE_RECOVER", "already_succeeded")
        if outcome == "exhausted":
            return ActionOutcome("CORE_RECOVER", "exhausted")
        # RUNNING: Core.recover is idempotent via its own idempotency key.
        try:
            self.core.recover(self.owner_source, idempotency_key=key)
        except ArgentError as exc:
            self._finish_action(row["id"], "FAILED", f"{type(exc).__name__}")
            return ActionOutcome("CORE_RECOVER", "failed", f"{type(exc).__name__}")
        self._finish_action(row["id"], "SUCCEEDED")
        return ActionOutcome("CORE_RECOVER", "executed")

    def _perform_present_owner_gate(self, decision, job):
        gate, _n = self.store._current_gate(job["task_id"])
        gate_id = gate.id if gate is not None else None
        key = f"supervisor:{job['id']}:present-gate:{gate_id}"
        args_hash = _sha256(_canonical_json({"gate_id": gate_id}))
        row, outcome = self._begin_action(
            key, "PRESENT_OWNER_GATE", job, None, args_hash)
        if outcome == "succeeded":
            return ActionOutcome("PRESENT_OWNER_GATE", "already_succeeded")
        if outcome == "exhausted":
            return ActionOutcome("PRESENT_OWNER_GATE", "exhausted")
        if outcome == "running":
            cur = self.core._store.get_supervisor_job(job["id"])
            if cur is not None and cur.get("owner_prompted_at") is not None \
                    and cur.get("owner_prompted_gate_id") == gate_id:
                self._finish_action(row["id"], "SUCCEEDED")
                return ActionOutcome("PRESENT_OWNER_GATE", "executed", "reconciled")
        with self.core._store._transaction():
            self.core._store._update_supervisor_job(
                job["id"],
                owner_prompted_at=self._now_iso(),
                owner_prompted_gate_id=gate_id,
                updated_at=self._now_iso(),
            )
        self._finish_action(row["id"], "SUCCEEDED")
        return ActionOutcome("PRESENT_OWNER_GATE", "executed")

    def _perform_close_done(self, decision, job):
        return self._close_job(job, "DONE", reason=decision.reason)

    def _perform_close_failed(self, decision, job):
        return self._close_job(job, "FAILED", reason=decision.reason)

    def _perform_close_blocked(self, decision, job):
        return self._close_job(job, "BLOCKED", reason=decision.reason)

    def _close_job(self, job, terminal, reason=None):
        # F2: the CLOSE_JOB journal effect and the terminal persistence happen
        # in ONE transaction so a terminal job can never exist without its
        # journaled CLOSE_JOB row (and vice versa).  CLOSE_JOB is journaled
        # exactly once (unique action_key).
        key = f"supervisor:{job['id']}:close:{terminal}"
        args_hash = _sha256(_canonical_json({"terminal": terminal}))
        now = self._now_iso()
        with self.core._store._transaction():
            cur = self.core._store.get_supervisor_job(job["id"])
            if cur is None:
                return ActionOutcome("CLOSE_JOB", "skipped", "job_missing")
            existing = self.core._store.get_supervisor_action_by_key(key)
            if existing is not None:
                if existing["args_hash"] != args_hash:
                    raise IdempotencyError(
                        f"action key {key!r} reused with different args_hash"
                    )
                action_id = existing["id"]
                if existing["status"] != "SUCCEEDED":
                    self.core._store._update_supervisor_action(
                        action_id, status="SUCCEEDED", finished_at=now,
                        last_error_code=None, updated_at=now,
                    )
            else:
                action_id = _sha256(key)[:32]
                self.core._store._insert_supervisor_action({
                    "id": action_id, "supervisor_job_id": job["id"],
                    "dispatch_id": None, "action_type": "CLOSE_JOB",
                    "action_key": key, "args_hash": args_hash,
                    "input_hash": None, "precondition_hash": None,
                    "effect_hash": None, "status": "SUCCEEDED",
                    "attempt_count": 1, "next_attempt_at": None,
                    "started_at": now, "finished_at": now,
                    "last_error_code": None, "created_at": now,
                    "updated_at": now,
                })
            if cur["terminal"] != terminal:
                self.core._store._update_supervisor_job(
                    job["id"], terminal=terminal,
                    status=SupervisorJobStatus.TERMINAL.value,
                    next_action=ReconcileAction.NONE.value,
                    next_wake_at=None, updated_at=now,
                    facts_version=cur["facts_version"] + 1,
                )
                # SPEC V3A §7: first-time terminal transition -> one outbox
                # row (dedup-guarded) in the SAME transaction.
                self._enqueue_close_notification(job, terminal, reason or "")
        return ActionOutcome("CLOSE_JOB", "executed", detail=terminal)

    def _perform_persistent_error(self, decision, job):
        with self.core._store._transaction():
            self.core._store._update_supervisor_job(
                job["id"], status=SupervisorJobStatus.ERROR.value,
                last_error_code=decision.reason, updated_at=self._now_iso(),
                next_action=ReconcileAction.NONE.value,
            )
        return ActionOutcome("PERSISTENT_ERROR", "executed", detail=decision.reason)


# ---------------------------------------------------------------------------
# SupervisorLoop + Waiter (SPEC V2C §9)
# ---------------------------------------------------------------------------

class Waiter:
    """Interruptible sleep-based waiter (default)."""

    def __init__(self, clock: Optional[Callable[[], datetime]] = None):
        self._clock = clock or utcnow

    def wait_until(self, wake_at: Optional[str], stop_event=None) -> bool:
        if stop_event is not None and stop_event.is_set():
            return True
        if not wake_at:
            return False
        try:
            target = _parse_iso(wake_at)
        except (ValueError, TypeError):
            return False
        now = self._clock().timestamp()
        delay = target - now
        if delay <= 0:
            return False
        if stop_event is not None:
            stop_event.wait(delay)
        else:
            import time
            time.sleep(delay)
        return True


class SupervisorLoop:
    """Local, restart-proof loop (no background installation in Phase 2C)."""

    def __init__(
        self, supervisor: Supervisor, waiter: Optional[Waiter] = None,
        stop_event=None, notification_delivery=None,
        # B1 (F3): optional durable-queue lease identity.  When set, the loop
        # CLAIMS a claimable job before reconciling it, so RUNNING is only ever
        # entered with a valid lease.  ``None`` preserves the legacy unleased
        # single-supervisor path.
        owner_instance_id: Optional[str] = None,
        lease_ttl_seconds: int = 60,
    ):
        self.supervisor = supervisor
        self.waiter = waiter or Waiter(clock=supervisor._clock)
        self._stop_event = stop_event
        # Optional non-blocking delivery kick target (SPEC V3A §3.5).  None
        # (default) makes kick a no-op so existing callers are unaffected.
        self._notification_delivery = notification_delivery
        self._owner_instance_id = owner_instance_id
        self._lease_ttl_seconds = lease_ttl_seconds

    def _acquire_loop_lease(self, job_id: str) -> None:
        """F3: claim a claimable job before working it (durable queue).

        Only active when ``owner_instance_id`` was configured.  A QUEUED job is
        claimed (QUEUED→RUNNING + lease); a RUNNING job we already hold merely
        re-establishes the in-memory lease context (restart-safe).  A foreign
        active lease or a terminal job is left untouched (the reconcile fence
        fails closed for a non-holder).
        """
        if self._owner_instance_id is None:
            return
        job = self.supervisor.store._job_row(job_id)
        if job is None:
            return
        ps = job.get("primary_state")
        owner = job.get("owner_instance_id")
        if ps == job_state.PrimaryState.QUEUED.value:
            row = self.supervisor.store.claim_job(
                job_id, owner_instance_id=self._owner_instance_id,
                ttl_seconds=self._lease_ttl_seconds,
            )
            self.supervisor.set_lease_owner(self._owner_instance_id, row["lease_epoch"])
        elif ps == job_state.PrimaryState.RUNNING.value and \
                owner == self._owner_instance_id:
            self.supervisor.set_lease_owner(
                self._owner_instance_id, job["lease_epoch"]
            )

    def _kick_delivery(self) -> None:
        # Amendment 4: the kick is internally catch-all and must never
        # propagate a notification exception into the loop.
        if self._notification_delivery is None:
            return
        try:
            self._notification_delivery.kick()
        except BaseException:  # noqa: BLE001 - never escape into the loop
            pass

    def run_once(self, job_id: str) -> ReconcileDecision:
        # F3: when this loop owns leases, claim the job before reconciling it
        # (RUNNING is only ever entered with a valid lease).
        self._acquire_loop_lease(job_id)
        # F3: loop-level containment — a structural adapter exception escaping
        # reconcile()/perform_next_safe_action_if_required() must never kill
        # the loop.  Convert it to a structured decision and let the existing
        # backoff/sticky-error machinery handle it.  Core/DB/ArgentError are
        # NOT caught here.
        try:
            decision = self.supervisor.reconcile(job_id)
        except (TypeError, AttributeError, ValueError, KeyError) as exc:
            decision = self.supervisor.adapter_exception_decision(
                job_id, type(exc).__name__)
        else:
            try:
                self.supervisor.perform_next_safe_action_if_required(decision)
            except (TypeError, AttributeError, ValueError, KeyError) as exc:
                decision = self.supervisor.adapter_exception_decision(
                    job_id, type(exc).__name__)
        # SPEC V3A §3.5 + Amendment 4: kick OUTSIDE both exception handlers,
        # O(1), never blocking, never propagating.
        self._kick_delivery()
        return decision

    def run_until_terminal(self, job_id: str, stop_event=None) -> Optional[SupervisorState]:
        stop_event = stop_event or self._stop_event
        while True:
            state = self.supervisor.store.get_job(job_id)
            if state is None:
                self._kick_delivery()
                return None
            if state.terminal is not None:
                self._kick_delivery()
                return state
            # F2: a sticky ERROR job must return immediately after one final
            # delivery kick (SPEC V3A Amendment 4) — never re-enter run_once
            # for the same sticky ERROR state (which would tight-loop kicking
            # short-lived delivery workers on a next_wake_at of None).
            if state.status == SupervisorJobStatus.ERROR.value:
                self._kick_delivery()
                return state
            if stop_event is not None and stop_event.is_set():
                self._kick_delivery()
                return state
            decision = self.run_once(job_id)
            state = self.supervisor.store.get_job(job_id)
            if state is None or state.terminal is not None:
                self._kick_delivery()
                return state
            if state.status == SupervisorJobStatus.ERROR.value:
                self._kick_delivery()
                return state
            if stop_event is not None and stop_event.is_set():
                self._kick_delivery()
                return state
            self.waiter.wait_until(state.next_wake_at, stop_event)
