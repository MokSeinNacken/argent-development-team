"""Public API of the deterministic team-control core (SPEC V1 + V1.1).

Every public entry point validates the source trust class first (fail-closed),
then runs its work inside a single ``BEGIN IMMEDIATE`` transaction.  Commands
accept an optional ``idempotency_key``: a repeat with the same key and the same
canonical arguments produces no second state change and no duplicate event; a
repeat with the same key but different arguments raises ``IdempotencyError``.

SPEC V1.1 hardenings (R1–R15) are implemented here: owner-authority gates, role
authority bound to the active role run + source, strict handoff enforcement,
gate entry into the transition table, persisted action executions, conservative
recovery V2 and store encapsulation via the read-only ``Core.queries`` facade.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional
from uuid import uuid4

from . import context, events, gates, outputs, recovery, roles, routing, state_machine, trust, workflow
from .models import (
    ActionClass,
    ActionExecution,
    ActionExecutionStatus,
    AgentContextSnapshot,
    AgentDispatch,
    AgentResultQuarantine,
    ApprovalError,
    ApprovalStatus,
    ArgentError,
    Decision,
    DispatchError,
    DispatchStatus,
    ExternalActionsPolicy,
    Finding,
    FindingStatus,
    ForbiddenAction,
    IdempotencyError,
    InvalidTransition,
    NotFound,
    OutputValidationError,
    OwnerApproval,
    PermissionDenied,
    Project,
    RiskClass,
    Role,
    RoleConflict,
    RolePolicyViolation,
    RoleRun,
    RoleRunStatus,
    SequenceKind,
    SourceClass,
    Task,
    TaskRun,
    TaskRunStatus,
    TaskState,
    TestResult,
    TestRun,
    UntrustedSource,
    Event,
    Review,
)
from .roles import DEFAULT_NEXT_ROLE
from .state_machine import PAUSE_STATES, TERMINAL_STATES, is_actionable, is_valid_resume_target
from .store import Queries, Store, utcnow
from .trust import role_source

APPROVAL_TTL_SECONDS = 3600

# Map command name -> refetch kind (idempotent replay).
_REPLAY_KIND: dict[str, str] = {
    "create_project": "project",
    "create_task": "task",
    "transition": "task",
    "resume": "task",
    "start_role": "role_run",
    "complete_role": "role_run",
    "fail_role": "role_run",
    "start_task_run": "task_run",
    "complete_task_run": "task_run",
    "fail_task_run": "task_run",
    "approve": "approval",
    "reject": "approval",
    "execute_approved": "approval",
    "add_finding": "finding",
    "resolve_finding": "finding",
    "record_test_run": "test_run",
    "record_review": "review",
    "record_decision": "decision",
    "create_dispatch": "dispatch",
    "bind_spawn_result": "dispatch",
    "mark_agent_failed": "dispatch",
    "receive_agent_result": "dispatch",
}


@dataclass(frozen=True)
class ActionRequestResult:
    action: str
    scope: str
    action_class: ActionClass
    allowed: bool
    approval: Optional[OwnerApproval] = None
    execution_id: Optional[str] = None


@dataclass(frozen=True)
class RecoveryReport:
    interrupted_role_runs: int
    interrupted_task_runs: int
    rolled_back: tuple = field(default_factory=tuple)
    recovery_pending_dispatches: int = 0


@dataclass(frozen=True)
class ReceiveResult:
    """Outcome of ``receive_agent_result`` (fail-closed, non-raising)."""

    dispatch_id: Optional[str]
    status: str  # 'consumed' | 'duplicate' | 'rejected' | 'unknown'
    reason: Optional[str] = None


class _ApprovalExpired(Exception):
    """Internal signal: approval is expired and should be marked expired."""

    def __init__(self, approval_id: str):
        self.approval_id = approval_id
        super().__init__(approval_id)


def _hash_args(args: dict) -> str:
    """Canonical SHA-256 of a command's arguments (R9)."""
    canonical = json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Quarantine-metadata value cap and redaction (SPEC V2.2 16.7).
_SANITIZE_VALUE_LIMIT = 512


def _redact_event_value(value: str) -> str:
    """Rotated placeholder for a deny-listed/oversized quarantine value."""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"<redacted:{digest}>"


class Core:
    """Deterministic team-control core over a SQLite store."""

    def __init__(self, db_path: str, clock: Optional[Callable[[], datetime]] = None):
        self._clock = clock or utcnow
        self._store = Store(db_path, clock=self._clock)
        self._queries = Queries(self._store)

    # ------------------------------------------------------------------ utils

    @property
    def queries(self) -> Queries:
        """Read-only ``get_*``/``list_*`` query facade (SPEC V1.1 11.5, R8)."""
        return self._queries

    def close(self) -> None:
        self._store.close()

    def _check_source(self, source: str) -> SourceClass:
        cls = trust.classify_source(source)
        if cls is not SourceClass.TRUSTED:
            raise UntrustedSource(f"untrusted source rejected: {source!r}")
        return cls

    def _require_owner(self, source: str) -> None:
        trust.require_owner(source)

    def _coerce_state(self, value) -> TaskState:
        if isinstance(value, TaskState):
            return value
        try:
            return TaskState(value)
        except (ValueError, TypeError):
            raise InvalidTransition(f"unknown state: {value!r}") from None

    def _coerce_role(self, value) -> Role:
        return value if isinstance(value, Role) else Role(value)

    def _coerce_result(self, value) -> TestResult:
        return value if isinstance(value, TestResult) else TestResult(value)

    # ---------------------------------------------------- role authorization

    def _require_role_actor(self, task_id: str, actor_role: Role, source: str) -> None:
        """Require an active ``actor_role`` run and ``source == role:<actor_role>``."""
        expected = role_source(actor_role)
        if source != expected:
            raise PermissionDenied(
                f"operation requires source {expected!r}, got {source!r}"
            )
        active = self._store.get_active_role_run(task_id)
        if active is None or active.role is not actor_role:
            raise PermissionDenied(
                f"task {task_id!r} has no active {actor_role.value} role run"
            )

    def _require_active_role_in(
        self, task_id: str, allowed_roles: tuple[Role, ...], source: str
    ) -> None:
        """Require an active role run whose role is in ``allowed_roles`` and whose
        source matches the active role exactly."""
        active = self._store.get_active_role_run(task_id)
        if active is None:
            raise PermissionDenied(f"task {task_id!r} has no active role run")
        if active.role not in allowed_roles:
            raise PermissionDenied(
                f"role {active.role.value!r} is not allowed for this operation"
            )
        if source != role_source(active.role):
            raise PermissionDenied(
                f"operation requires source {role_source(active.role)!r}, got {source!r}"
            )

    def _require_actionable_state(self, task: Task) -> None:
        """Reject gated actions requested from terminal or pause states.

        A task in ``DONE``/``CANCELLED`` (terminal) or in any pause state
        (``OWNER_APPROVAL_REQUIRED``/``PAUSED``/``RECOVERING``) cannot request
        an action (SPEC V1.2 12.1/12.2): fail-closed, no side effect.
        """
        if not is_actionable(task.state):
            raise InvalidTransition(
                f"cannot request action from state {task.state.value}"
            )

    # -------------------------------------------------------- orchestration

    def _require_controller(self, source: str) -> None:
        """Require the controller (``role:lead``) as the dispatch interface.

        The controller is the only process interface to the Core for the
        orchestration layer (SPEC V2 15.1).  ``source`` strings are valid only
        within the controller interface; they are not an authentication
        mechanism against external attackers (documented 2A boundary).
        """
        self._check_source(source)
        if source != role_source(Role.LEAD):
            raise PermissionDenied(f"controller (role:lead) required, got {source!r}")

    def _workflow_frontier(self, task_id: str) -> workflow.WorkflowFrontier:
        """Compute the next dispatch position from the persisted workflow state.

        Deterministic replay from ``agent_dispatches`` + the latest lead
        decision (SPEC V2 15.4).  The frontier is:

        - no dispatches -> cycle 1, position 0, STANDARD, expected ``lead``;
        - a pending rework lead decision -> new cycle (cycle+1, position 0,
          REWORK), expected ``lead``;
        - otherwise the first not-yet-consumed position of the current cycle.
        """
        dispatches = self._store.list_dispatches(task_id)
        open_findings = any(
            f.status is FindingStatus.OPEN for f in self._store.list_findings(task_id)
        )
        decisions = self._store.list_decisions(task_id)

        if not dispatches:
            return workflow.WorkflowFrontier(1, 0, SequenceKind.STANDARD, True, Role.LEAD)

        max_cycle = max(d.cycle_no for d in dispatches)
        in_cycle = [d for d in dispatches if d.cycle_no == max_cycle]
        kind = in_cycle[-1].sequence_kind

        # Rework decision check: the latest lead decision that starts a new cycle.
        last_decision = decisions[-1] if decisions else None
        if last_decision is not None and last_decision.decision == "rework":
            detail = self._parse_decision_detail(last_decision.detail)
            if detail.get("cycle_no") == max_cycle:
                include = workflow.rework_include_reviewer(detail, open_findings)
                return workflow.WorkflowFrontier(
                    max_cycle + 1, 0, SequenceKind.REWORK, include, Role.LEAD
                )

        include_reviewer = True
        if kind is SequenceKind.REWORK:
            include_reviewer = self._rework_include_for_cycle(
                task_id, max_cycle, decisions, open_findings
            )

        consumed = {d.position for d in in_cycle if d.status is DispatchStatus.CONSUMED}
        seq = workflow.effective_sequence(kind, include_reviewer)
        for pos in range(len(seq)):
            if pos not in consumed:
                return workflow.WorkflowFrontier(
                    max_cycle, pos, kind, include_reviewer, seq[pos]
                )
        return workflow.WorkflowFrontier(max_cycle, len(seq), kind, include_reviewer, None)

    def _rework_include_for_cycle(
        self,
        task_id: str,
        cycle_no: int,
        decisions: list,
        open_findings: bool,
    ) -> bool:
        """Resolve the reviewer toggle for a rework cycle from its originating
        rework decision (made in ``cycle_no - 1``)."""
        for d in reversed(decisions):
            if d.decision != "rework":
                continue
            detail = self._parse_decision_detail(d.detail)
            if detail.get("cycle_no") == cycle_no - 1:
                return workflow.rework_include_reviewer(detail, open_findings)
        return workflow.rework_include_reviewer(None, open_findings)

    @staticmethod
    def _parse_decision_detail(detail: Optional[str]) -> dict:
        if not detail:
            return {}
        try:
            parsed = json.loads(detail)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}

    def expected_next_role(self, task_id: str, source: str) -> Optional[Role]:
        """Controller helper: the role the controller should dispatch next."""
        self._require_controller(source)
        if self._store.get_task(task_id) is None:
            raise NotFound(f"task {task_id!r} not found")
        return self._workflow_frontier(task_id).expected_role

    # ---------------------------------------------------------------- events

    def _emit(
        self,
        type_: str,
        task_id: Optional[str] = None,
        role: Optional[str] = None,
        state: Optional[str] = None,
        payload: Optional[dict] = None,
    ) -> Event:
        ev = Event(
            id=str(uuid4()),
            type=type_,
            task_id=task_id,
            role=role,
            state=state,
            payload=payload or {},
            created_at=self._store.now_iso(),
        )
        self._store._insert_event(ev)
        return ev

    def _apply_transition(
        self,
        task: Task,
        to_state: TaskState,
        new_resume: Optional[TaskState],
    ) -> None:
        self._store._update_task_state(
            task.id, to_state, new_resume, self._store.now_iso()
        )
        self._emit(
            "task.state_changed",
            task_id=task.id,
            state=to_state.value,
            payload={"from_state": task.state.value, "to_state": to_state.value},
        )
        if to_state is TaskState.DONE:
            self._emit(
                "task.completed",
                task_id=task.id,
                state=TaskState.DONE.value,
                payload={},
            )

    def _check_full_binding(
        self, approval: OwnerApproval, task_id: str, action: str, scope: str
    ) -> None:
        if (
            approval.task_id != task_id
            or approval.action != action
            or approval.scope != scope
        ):
            raise ApprovalError(
                "approval binding mismatch: task_id/action/scope must all match "
                f"(approval={approval.task_id!r}/{approval.action!r}/{approval.scope!r}, "
                f"requested={task_id!r}/{action!r}/{scope!r})"
            )

    # ------------------------------------------------------------- idempotency

    def _idempotent(self, key, command, args, work, refetch):
        args_hash = _hash_args(args)
        if key is None:
            with self._store._transaction():
                entity, _rid = work()
            return entity
        with self._store._transaction():
            existing = self._store.get_command_idempotency(key, command)
            if existing is not None:
                result_id, stored_hash = existing
                if stored_hash != args_hash:
                    raise IdempotencyError(
                        f"idempotency key {key!r} reused for {command!r} with "
                        "different arguments"
                    )
                return refetch(result_id)
            entity, rid = work()
            self._store._set_command_idempotency(
                key, command, rid, args_hash, self._store.now_iso()
            )
            return entity

    def _refetch(self, command: str, result_id: str):
        kind = _REPLAY_KIND.get(command)
        if kind == "project":
            obj = self._store.get_project(result_id)
        elif kind == "task":
            obj = self._store.get_task(result_id)
        elif kind == "role_run":
            obj = self._store.get_role_run(result_id)
        elif kind == "task_run":
            obj = self._store.get_task_run(result_id)
        elif kind == "approval":
            return self._approval_result_refetch(result_id)
        elif kind == "finding":
            obj = self._store.get_finding(result_id)
        elif kind == "test_run":
            obj = self._store.get_test_run(result_id)
        elif kind == "review":
            obj = self._store.get_review(result_id)
        elif kind == "decision":
            obj = self._store.get_decision(result_id)
        elif kind == "dispatch":
            obj = self._store.get_dispatch(result_id)
        else:
            raise IdempotencyError(f"unknown command {command!r}")
        if obj is None:
            raise IdempotencyError(f"idempotent replay: entity {result_id!r} missing")
        return obj

    def _approval_result_refetch(self, result_id: str):
        ap = self._store.get_approval(result_id)
        if ap is None:
            raise IdempotencyError(f"idempotent replay: approval {result_id!r} missing")
        return ap

    def _request_action_refetch(self, result_id: str):
        if result_id.startswith("approval:"):
            ap = self._store.get_approval(result_id[len("approval:"):])
            if ap is None:
                raise IdempotencyError(
                    f"idempotent replay: approval {result_id!r} missing"
                )
            return ActionRequestResult(
                action=ap.action,
                scope=ap.scope,
                action_class=ActionClass.OWNER_APPROVAL_REQUIRED,
                allowed=False,
                approval=ap,
            )
        if result_id.startswith("execution:"):
            ex = self._store.get_action_execution(result_id[len("execution:"):])
            if ex is None:
                raise IdempotencyError(
                    f"idempotent replay: execution {result_id!r} missing"
                )
            cls = gates.classify_action(ex.action)
            return ActionRequestResult(
                action=ex.action,
                scope=ex.scope,
                action_class=cls,
                allowed=(ex.status is ActionExecutionStatus.EXECUTED),
                execution_id=ex.id,
            )
        if result_id.startswith("blocked:"):
            return None
        raise IdempotencyError(f"unknown request_action result {result_id!r}")

    # ------------------------------------------------------------- projects

    def create_project(
        self, name: str, source: str, idempotency_key: Optional[str] = None
    ) -> Project:
        self._require_owner(source)
        args = {"name": name, "source": source}

        def work():
            pid = str(uuid4())
            p = Project(id=pid, name=name, created_at=self._store.now_iso(),
                        idempotency_key=idempotency_key)
            self._store._insert_project(p)
            return p, pid

        return self._idempotent(idempotency_key, "create_project", args, work,
                                lambda rid: self._refetch("create_project", rid))

    # ----------------------------------------------------------------- tasks

    def create_task(
        self,
        project_id: str,
        title: str,
        source: str,
        idempotency_key: Optional[str] = None,
        description: Optional[str] = None,
        risk_class: RiskClass = RiskClass.NORMAL,
        external_actions_policy: ExternalActionsPolicy = ExternalActionsPolicy.ALLOWED_WITH_GATE,
    ) -> Task:
        self._require_owner(source)
        risk_class = (
            risk_class if isinstance(risk_class, RiskClass) else RiskClass(risk_class)
        )
        external_actions_policy = (
            external_actions_policy
            if isinstance(external_actions_policy, ExternalActionsPolicy)
            else ExternalActionsPolicy(external_actions_policy)
        )
        args = {
            "project_id": project_id, "title": title, "source": source,
            "description": description, "risk_class": risk_class.value,
            "external_actions_policy": external_actions_policy.value,
        }

        def work():
            if self._store.get_project(project_id) is None:
                raise NotFound(f"project {project_id!r} not found")
            tid = str(uuid4())
            now = self._store.now_iso()
            t = Task(
                id=tid,
                project_id=project_id,
                title=title,
                state=TaskState.NEW,
                resume_state=None,
                source=source,
                source_class=SourceClass.TRUSTED,
                created_at=now,
                updated_at=now,
                idempotency_key=idempotency_key,
                description=description,
                risk_class=risk_class,
                external_actions_policy=external_actions_policy,
            )
            self._store._insert_task(t)
            self._emit("task.created", task_id=tid, state=TaskState.NEW.value,
                       payload={"project_id": project_id})
            return t, tid

        return self._idempotent(idempotency_key, "create_task", args, work,
                                lambda rid: self._refetch("create_task", rid))

    def transition(
        self,
        task_id: str,
        to_state,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> Task:
        self._check_source(source)
        to_state = self._coerce_state(to_state)
        args = {"task_id": task_id, "to_state": to_state.value, "source": source}

        def work():
            task = self._store.get_task(task_id)
            if task is None:
                raise NotFound(f"task {task_id!r} not found")
            self._require_role_actor(task_id, Role.LEAD, source)
            if task.state in PAUSE_STATES or to_state in PAUSE_STATES:
                raise InvalidTransition(
                    "pause states are reserved for the dedicated gate/resume/"
                    "recover commands"
                )
            state_machine.validate_transition(task.state, to_state, task.resume_state)
            self._apply_transition(task, to_state, None)
            return self._store.get_task(task_id), task_id

        return self._idempotent(idempotency_key, "transition", args, work,
                                lambda rid: self._refetch("transition", rid))

    def resume(
        self,
        task_id: str,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> Task:
        self._check_source(source)
        args = {"task_id": task_id, "source": source}

        def work():
            task = self._store.get_task(task_id)
            if task is None:
                raise NotFound(f"task {task_id!r} not found")
            self._require_role_actor(task_id, Role.LEAD, source)
            if task.state is not TaskState.PAUSED:
                raise InvalidTransition(
                    f"resume requires state PAUSED, got {task.state.value}"
                )
            rs = task.resume_state
            if rs is None:
                raise InvalidTransition("task is PAUSED without a resume_state")
            state_machine.validate_transition(task.state, rs, rs)
            self._apply_transition(task, rs, None)
            return self._store.get_task(task_id), task_id

        return self._idempotent(idempotency_key, "resume", args, work,
                                lambda rid: self._refetch("resume", rid))

    # ------------------------------------------------------------- role runs

    def start_role(
        self,
        task_id: str,
        role,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> RoleRun:
        self._check_source(source)
        role = self._coerce_role(role)
        args = {"task_id": task_id, "role": role.value, "source": source}

        def work():
            task = self._store.get_task(task_id)
            if task is None:
                raise NotFound(f"task {task_id!r} not found")
            if source != role_source(Role.LEAD):
                raise PermissionDenied("start_role is lead-only")
            if (
                task.state in TERMINAL_STATES
                or task.state in PAUSE_STATES
                or task.state is TaskState.BLOCKED
            ):
                raise InvalidTransition(
                    f"cannot start a role run from state {task.state.value}"
                )
            if self._store.get_active_role_run(task_id) is not None:
                raise RoleConflict(f"task {task_id!r} already has an active role run")
            latest = self._store.get_latest_handoff(task_id)
            if latest is None:
                expected = Role.LEAD
            else:
                expected = latest.to_role
            if role is not expected:
                raise RoleConflict(
                    f"next role must be {expected.value!r} (handoff required), "
                    f"got {role.value!r}"
                )
            rid = str(uuid4())
            rr = RoleRun(id=rid, task_id=task_id, role=role,
                         status=RoleRunStatus.STARTED, started_at=self._store.now_iso())
            self._store._insert_role_run(rr)
            self._emit("role.started", task_id=task_id, role=role.value,
                       payload={"role": role.value})
            return rr, rid

        return self._idempotent(idempotency_key, "start_role", args, work,
                                lambda rid: self._refetch("start_role", rid))

    def complete_role(
        self,
        role_run_id: str,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> RoleRun:
        self._check_source(source)
        args = {"role_run_id": role_run_id, "source": source}

        def work():
            rr = self._store.get_role_run(role_run_id)
            if rr is None:
                raise NotFound(f"role run {role_run_id!r} not found")
            if rr.status is not RoleRunStatus.STARTED:
                raise IdempotencyError(
                    f"role run {role_run_id!r} is not started ({rr.status.value})"
                )
            if source != role_source(rr.role):
                raise PermissionDenied(
                    f"complete_role requires source {role_source(rr.role)!r}, "
                    f"got {source!r}"
                )
            now = self._store.now_iso()
            self._store._update_role_run_status(role_run_id, RoleRunStatus.COMPLETED, now)
            to_role = DEFAULT_NEXT_ROLE[rr.role]
            self._store._insert_handoff(
                models_handoff(rr.task_id, rr.role, to_role, now)
            )
            self._emit("role.completed", task_id=rr.task_id, role=rr.role.value,
                       payload={"role": rr.role.value})
            self._emit("handoff.created", task_id=rr.task_id,
                       payload={"from_role": rr.role.value, "to_role": to_role.value})
            return self._store.get_role_run(role_run_id), role_run_id

        return self._idempotent(idempotency_key, "complete_role", args, work,
                                lambda rid: self._refetch("complete_role", rid))

    def fail_role(
        self,
        role_run_id: str,
        source: str,
        detail: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> RoleRun:
        self._check_source(source)
        args = {"role_run_id": role_run_id, "source": source, "detail": detail}

        def work():
            rr = self._store.get_role_run(role_run_id)
            if rr is None:
                raise NotFound(f"role run {role_run_id!r} not found")
            if rr.status is not RoleRunStatus.STARTED:
                raise IdempotencyError(
                    f"role run {role_run_id!r} is not started ({rr.status.value})"
                )
            if source != role_source(rr.role):
                raise PermissionDenied(
                    f"fail_role requires source {role_source(rr.role)!r}, got {source!r}"
                )
            now = self._store.now_iso()
            self._store._update_role_run_status(role_run_id, RoleRunStatus.FAILED, now)
            # Deterministic handoff after failure (SPEC V1.3 13.3): like
            # complete_role, record the next role so the pipeline can continue.
            to_role = DEFAULT_NEXT_ROLE[rr.role]
            self._store._insert_handoff(
                models_handoff(rr.task_id, rr.role, to_role, now)
            )
            payload = {"role": rr.role.value}
            if detail is not None:
                payload["detail"] = detail
            self._emit("role.failed", task_id=rr.task_id, role=rr.role.value,
                       payload=payload)
            self._emit("handoff.created", task_id=rr.task_id,
                       payload={"from_role": rr.role.value, "to_role": to_role.value})
            return self._store.get_role_run(role_run_id), role_run_id

        return self._idempotent(idempotency_key, "fail_role", args, work,
                                lambda rid: self._refetch("fail_role", rid))

    # ------------------------------------------------------------- task runs

    def start_task_run(
        self,
        task_id: str,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> TaskRun:
        self._check_source(source)
        args = {"task_id": task_id, "source": source}

        def work():
            if self._store.get_task(task_id) is None:
                raise NotFound(f"task {task_id!r} not found")
            rid = str(uuid4())
            tr = TaskRun(id=rid, task_id=task_id, status=TaskRunStatus.STARTED,
                         started_at=self._store.now_iso())
            self._store._insert_task_run(tr)
            return tr, rid

        return self._idempotent(idempotency_key, "start_task_run", args, work,
                                lambda rid: self._refetch("start_task_run", rid))

    def complete_task_run(
        self,
        task_run_id: str,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> TaskRun:
        self._check_source(source)
        args = {"task_run_id": task_run_id, "source": source}

        def work():
            tr = self._store.get_task_run(task_run_id)
            if tr is None:
                raise NotFound(f"task run {task_run_id!r} not found")
            if tr.status is not TaskRunStatus.STARTED:
                raise IdempotencyError(f"task run {task_run_id!r} is not started")
            self._store._update_task_run_status(
                task_run_id, TaskRunStatus.COMPLETED, self._store.now_iso()
            )
            return self._store.get_task_run(task_run_id), task_run_id

        return self._idempotent(idempotency_key, "complete_task_run", args, work,
                                lambda rid: self._refetch("complete_task_run", rid))

    def fail_task_run(
        self,
        task_run_id: str,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> TaskRun:
        self._check_source(source)
        args = {"task_run_id": task_run_id, "source": source}

        def work():
            tr = self._store.get_task_run(task_run_id)
            if tr is None:
                raise NotFound(f"task run {task_run_id!r} not found")
            if tr.status is not TaskRunStatus.STARTED:
                raise IdempotencyError(f"task run {task_run_id!r} is not started")
            self._store._update_task_run_status(
                task_run_id, TaskRunStatus.FAILED, self._store.now_iso()
            )
            return self._store.get_task_run(task_run_id), task_run_id

        return self._idempotent(idempotency_key, "fail_task_run", args, work,
                                lambda rid: self._refetch("fail_task_run", rid))

    # ----------------------------------------------------------- gated actions

    def request_action(
        self,
        task_id: str,
        action: str,
        scope: str,
        actor_role,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> ActionRequestResult:
        self._check_source(source)
        actor_role = self._coerce_role(actor_role)
        # Classify with the task's external-actions policy (SPEC V2 8.4/15.10).
        task_for_policy = self._store.get_task(task_id)
        if task_for_policy is None:
            raise NotFound(f"task {task_id!r} not found")
        cls = gates.classify_action(action, task_for_policy.external_actions_policy)
        args = {
            "task_id": task_id,
            "action": action,
            "scope": scope,
            "actor_role": actor_role.value,
            "source": source,
        }

        # SPEC V2B §6/§7: a role that lacks the write permission for an
        # AUTONOMOUS action is denied with a ``policy.role_violation`` event
        # (emitted outside the idempotent transaction so it persists).
        if cls is ActionClass.AUTONOMOUS:
            category, mode = gates.permission_for(action)
            try:
                roles.check_permission(actor_role, category, mode)
            except PermissionDenied:
                self._emit(
                    "policy.role_violation",
                    task_id=task_id,
                    role=actor_role.value,
                    payload={"reason": "permission_denied", "action": action},
                )
                raise

        if cls is ActionClass.FORBIDDEN:
            def work():
                task = self._store.get_task(task_id)
                if task is None:
                    raise NotFound(f"task {task_id!r} not found")
                self._require_role_actor(task_id, actor_role, source)
                self._require_actionable_state(task)
                eid = str(uuid4())
                self._store._insert_action_execution(
                    ActionExecution(
                        id=eid, task_id=task_id, approval_id=None, action=action,
                        scope=scope, actor_role=actor_role.value,
                        status=ActionExecutionStatus.BLOCKED,
                        created_at=self._store.now_iso(),
                    )
                )
                self._emit("lead.decision", task_id=task_id,
                           payload={"blocked": True})
                return None, f"blocked:{eid}"

            self._idempotent(idempotency_key, "request_action", args, work,
                             lambda rid: None)
            raise ForbiddenAction(f"action {action!r} is forbidden")

        if cls is ActionClass.OWNER_APPROVAL_REQUIRED:
            def work():
                task = self._store.get_task(task_id)
                if task is None:
                    raise NotFound(f"task {task_id!r} not found")
                self._require_role_actor(task_id, actor_role, source)
                self._require_actionable_state(task)
                for a in self._store.list_approvals(task_id):
                    if (a.action == action and a.scope == scope
                            and a.status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)):
                        raise ApprovalError(
                            f"duplicate pending/approved approval for {action!r}/{scope!r}"
                        )
                aid = str(uuid4())
                now = self._store.now_iso()
                ap = OwnerApproval(
                    id=aid, task_id=task_id, action=action, scope=scope,
                    status=ApprovalStatus.PENDING, requested_by=actor_role.value,
                    source_class=SourceClass.TRUSTED, created_at=now,
                    decided_at=None, consumed_at=None,
                    expires_at=self._store.expiry_iso(APPROVAL_TTL_SECONDS),
                )
                self._store._insert_approval(ap)
                task = self._store.get_task(task_id)
                state_machine.validate_transition(
                    task.state, TaskState.OWNER_APPROVAL_REQUIRED, task.resume_state
                )
                self._apply_transition(task, TaskState.OWNER_APPROVAL_REQUIRED, task.state)
                self._emit("gate.owner_required", task_id=task_id,
                           payload={"approval_id": aid})
                return (ActionRequestResult(action, scope, cls, allowed=False,
                                            approval=ap), f"approval:{aid}")

            return self._idempotent(idempotency_key, "request_action", args, work,
                                    self._request_action_refetch)

        # AUTONOMOUS: enforce the role permission (chapter 2).
        def work():
            task = self._store.get_task(task_id)
            if task is None:
                raise NotFound(f"task {task_id!r} not found")
            self._require_role_actor(task_id, actor_role, source)
            self._require_actionable_state(task)
            category, mode = gates.permission_for(action)
            roles.check_permission(actor_role, category, mode)
            eid = str(uuid4())
            self._store._insert_action_execution(
                ActionExecution(
                    id=eid, task_id=task_id, approval_id=None, action=action,
                    scope=scope, actor_role=actor_role.value,
                    status=ActionExecutionStatus.EXECUTED,
                    created_at=self._store.now_iso(),
                )
            )
            return (ActionRequestResult(action, scope, cls, allowed=True,
                                        execution_id=eid), f"execution:{eid}")

        return self._idempotent(idempotency_key, "request_action", args, work,
                                self._request_action_refetch)

    # ------------------------------------------------------------ owner gates

    def approve(
        self,
        approval_id: str,
        source: str,
        *,
        task_id: str,
        action: str,
        scope: str,
        idempotency_key: Optional[str] = None,
    ) -> OwnerApproval:
        self._require_owner(source)
        args = {
            "approval_id": approval_id, "task_id": task_id, "action": action,
            "scope": scope, "source": source,
        }

        def work():
            ap = self._store.get_approval(approval_id)
            if ap is None:
                raise ApprovalError(f"approval {approval_id!r} not found")
            self._check_full_binding(ap, task_id, action, scope)
            now = self._store.now_iso()
            if ap.status is not ApprovalStatus.PENDING:
                raise ApprovalError(
                    f"approval {approval_id!r} is not pending ({ap.status.value})"
                )
            if ap.expires_at <= now:
                raise _ApprovalExpired(approval_id)
            rc = self._store._mark_approved(approval_id, now)
            if rc == 0:
                raise ApprovalError(f"approval {approval_id!r} could not be approved")
            self._emit("gate.owner_approved", task_id=ap.task_id,
                       payload={"approval_id": approval_id})
            return self._store.get_approval(approval_id), approval_id

        try:
            return self._idempotent(idempotency_key, "approve", args, work,
                                    lambda rid: self._refetch("approve", rid))
        except _ApprovalExpired as exc:
            with self._store._transaction():
                self._expire_and_release(exc.approval_id)
            raise ApprovalError(f"approval {exc.approval_id!r} expired") from None

    def reject(
        self,
        approval_id: str,
        source: str,
        *,
        task_id: str,
        action: str,
        scope: str,
        idempotency_key: Optional[str] = None,
    ) -> OwnerApproval:
        self._require_owner(source)
        args = {
            "approval_id": approval_id, "task_id": task_id, "action": action,
            "scope": scope, "source": source,
        }

        def work():
            ap = self._store.get_approval(approval_id)
            if ap is None:
                raise ApprovalError(f"approval {approval_id!r} not found")
            self._check_full_binding(ap, task_id, action, scope)
            now = self._store.now_iso()
            rc = self._store._mark_rejected(approval_id, now)
            if rc == 0:
                ap2 = self._store.get_approval(approval_id)
                raise ApprovalError(
                    f"approval {approval_id!r} is not pending ({ap2.status.value})"
                )
            task = self._store.get_task(ap.task_id)
            if task is not None and task.state is TaskState.OWNER_APPROVAL_REQUIRED:
                state_machine.validate_transition(
                    task.state, TaskState.BLOCKED, task.resume_state
                )
                self._apply_transition(task, TaskState.BLOCKED, None)
            self._emit("gate.owner_rejected", task_id=ap.task_id,
                       payload={"approval_id": approval_id})
            return self._store.get_approval(approval_id), approval_id

        return self._idempotent(idempotency_key, "reject", args, work,
                                lambda rid: self._refetch("reject", rid))

    def _expire_and_release(self, approval_id: str) -> None:
        """Expire an approval and release its task from the gate (SPEC V1.3 13.2).

        Runs inside a single transaction.  The approval is marked ``expired``
        and, if the task is still parked in ``OWNER_APPROVAL_REQUIRED``, it is
        moved to its validated ``resume_state`` (non-terminal, non-pause) or to
        ``BLOCKED`` otherwise.  No consumption, no execution.
        """
        now = self._store.now_iso()
        self._store._mark_expired(approval_id, now)
        ap = self._store.get_approval(approval_id)
        if ap is None:
            return
        task = self._store.get_task(ap.task_id)
        if task is None or task.state is not TaskState.OWNER_APPROVAL_REQUIRED:
            return
        resume = task.resume_state
        if resume is not None and is_valid_resume_target(resume):
            state_machine.validate_transition(task.state, resume, task.resume_state)
            self._apply_transition(task, resume, None)
        else:
            state_machine.validate_transition(
                task.state, TaskState.BLOCKED, task.resume_state
            )
            self._apply_transition(task, TaskState.BLOCKED, None)

    def execute_approved(
        self,
        approval_id: str,
        source: str,
        *,
        task_id: str,
        action: str,
        scope: str,
        idempotency_key: Optional[str] = None,
    ) -> OwnerApproval:
        self._require_owner(source)
        args = {
            "approval_id": approval_id, "task_id": task_id, "action": action,
            "scope": scope, "source": source,
        }

        def work():
            ap = self._store.get_approval(approval_id)
            if ap is None:
                raise ApprovalError(f"approval {approval_id!r} not found")
            self._check_full_binding(ap, task_id, action, scope)
            now = self._store.now_iso()
            if ap.status is not ApprovalStatus.APPROVED:
                raise ApprovalError(
                    f"approval {approval_id!r} is not approved ({ap.status.value})"
                )
            if ap.expires_at <= now:
                # Expired 'approved' approval: release it (mark 'expired' and
                # free the task from the gate) so a fresh request can be made
                # (SPEC V1.3 13.2).  Handled atomically in _expire_and_release.
                raise _ApprovalExpired(approval_id)
            # Re-check action class and source class before consuming (R7).
            cls = gates.classify_action(ap.action)
            if cls is ActionClass.FORBIDDEN:
                raise ForbiddenAction(f"action {ap.action!r} is forbidden")
            if ap.source_class is not SourceClass.TRUSTED:
                raise ApprovalError(
                    f"approval {approval_id!r} has an untrusted source class"
                )
            rc = self._store._consume_approval(approval_id, now)
            if rc == 0:
                ap2 = self._store.get_approval(approval_id)
                raise ApprovalError(
                    f"approval {approval_id!r} not consumable ({ap2.status.value})"
                )
            self._store._insert_action_execution(
                ActionExecution(
                    id=str(uuid4()), task_id=ap.task_id, approval_id=approval_id,
                    action=ap.action, scope=ap.scope, actor_role=ap.requested_by,
                    status=ActionExecutionStatus.EXECUTED, created_at=self._store.now_iso(),
                )
            )
            task = self._store.get_task(ap.task_id)
            if task is not None and task.state is TaskState.OWNER_APPROVAL_REQUIRED:
                resume = task.resume_state
                if resume is not None:
                    state_machine.validate_transition(task.state, resume, task.resume_state)
                    self._apply_transition(task, resume, None)
            return self._store.get_approval(approval_id), approval_id

        try:
            return self._idempotent(idempotency_key, "execute_approved", args, work,
                                    lambda rid: self._refetch("execute_approved", rid))
        except _ApprovalExpired as exc:
            with self._store._transaction():
                self._expire_and_release(exc.approval_id)
            raise ApprovalError(f"approval {exc.approval_id!r} expired") from None

    # -------------------------------------------------------- findings / more

    def add_finding(
        self,
        task_id: str,
        severity: str,
        description: str,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> Finding:
        self._check_source(source)
        args = {
            "task_id": task_id, "severity": severity, "description": description,
            "source": source,
        }

        def work():
            if self._store.get_task(task_id) is None:
                raise NotFound(f"task {task_id!r} not found")
            self._require_active_role_in(task_id, (Role.REVIEWER, Role.QA), source)
            fid = str(uuid4())
            f = Finding(id=fid, task_id=task_id, severity=severity,
                        description=description, status=FindingStatus.OPEN,
                        created_at=self._store.now_iso())
            self._store._insert_finding(f)
            self._emit("finding.created", task_id=task_id,
                       payload={"finding_id": fid, "severity": severity})
            return f, fid

        return self._idempotent(idempotency_key, "add_finding", args, work,
                                lambda rid: self._refetch("add_finding", rid))

    def resolve_finding(
        self,
        finding_id: str,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> Finding:
        self._check_source(source)
        args = {"finding_id": finding_id, "source": source}

        def work():
            f = self._store.get_finding(finding_id)
            if f is None:
                raise NotFound(f"finding {finding_id!r} not found")
            self._require_active_role_in(f.task_id, (Role.REVIEWER, Role.QA), source)
            if f.status is not FindingStatus.OPEN:
                raise IdempotencyError(
                    f"finding {finding_id!r} is not open ({f.status.value})"
                )
            now = self._store.now_iso()
            self._store._update_finding_status(finding_id, FindingStatus.RESOLVED, now)
            self._emit("finding.resolved", task_id=f.task_id,
                       payload={"finding_id": finding_id})
            return self._store.get_finding(finding_id), finding_id

        return self._idempotent(idempotency_key, "resolve_finding", args, work,
                                lambda rid: self._refetch("resolve_finding", rid))

    def record_test_run(
        self,
        task_id: str,
        result,
        source: str,
        detail: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> TestRun:
        self._check_source(source)
        result = self._coerce_result(result)
        args = {"task_id": task_id, "result": result.value, "source": source,
                "detail": detail}

        def work():
            if self._store.get_task(task_id) is None:
                raise NotFound(f"task {task_id!r} not found")
            self._require_active_role_in(task_id, (Role.QA, Role.IMPLEMENTER), source)
            rid = str(uuid4())
            tr = TestRun(id=rid, task_id=task_id, result=result, detail=detail,
                         created_at=self._store.now_iso())
            self._store._insert_test_run(tr)
            self._emit("test.started", task_id=task_id,
                       payload={"test_run_id": rid})
            self._emit("test.completed", task_id=task_id,
                       payload={"test_run_id": rid, "result": result.value})
            return tr, rid

        return self._idempotent(idempotency_key, "record_test_run", args, work,
                                lambda rid: self._refetch("record_test_run", rid))

    def record_review(
        self,
        task_id: str,
        verdict: str,
        source: str,
        detail: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Review:
        self._check_source(source)
        args = {"task_id": task_id, "verdict": verdict, "source": source,
                "detail": detail}

        def work():
            if self._store.get_task(task_id) is None:
                raise NotFound(f"task {task_id!r} not found")
            self._require_active_role_in(task_id, (Role.REVIEWER,), source)
            rid = str(uuid4())
            rv = Review(id=rid, task_id=task_id, verdict=verdict, detail=detail,
                        created_at=self._store.now_iso())
            self._store._insert_review(rv)
            self._emit("review.started", task_id=task_id,
                       payload={"review_id": rid})
            self._emit("review.completed", task_id=task_id,
                       payload={"review_id": rid, "verdict": verdict})
            return rv, rid

        return self._idempotent(idempotency_key, "record_review", args, work,
                                lambda rid: self._refetch("record_review", rid))

    def record_decision(
        self,
        task_id: str,
        decision: str,
        source: str,
        detail: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Decision:
        self._check_source(source)
        args = {"task_id": task_id, "decision": decision, "source": source,
                "detail": detail}

        def work():
            if self._store.get_task(task_id) is None:
                raise NotFound(f"task {task_id!r} not found")
            self._require_role_actor(task_id, Role.LEAD, source)
            did = str(uuid4())
            d = Decision(id=did, task_id=task_id, decision=decision, detail=detail,
                         created_at=self._store.now_iso())
            self._store._insert_decision(d)
            self._emit("lead.decision", task_id=task_id,
                       payload={"decision_id": did})
            return d, did

        return self._idempotent(idempotency_key, "record_decision", args, work,
                                lambda rid: self._refetch("record_decision", rid))

    # --------------------------------------------------- orchestration layer

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _sanitize_event_meta(event_meta) -> dict:
        """Sanitize quarantine metadata (SPEC V2 15.11 / V2.2 16.7).

        Values are coerced to ``str``, capped at 512 chars, deny-list scanned
        (rotated placeholder ``<redacted:<sha256-prefix>>`` on a hit) and are
        therefore always JSON-serializable.
        """
        if not isinstance(event_meta, dict):
            return {}

        def clean(value):
            # V2.3 (G4): always produce a str.  ``None`` becomes ``<none>`` and
            # a failing ``__str__`` becomes ``<unprintable>``, so the sanitized
            # meta is always JSON-serializable and deny-list safe.
            if value is None:
                s = "<none>"
            elif isinstance(value, str):
                s = value
            else:
                try:
                    s = str(value)
                except Exception:
                    s = "<unprintable>"
            if len(s) > _SANITIZE_VALUE_LIMIT:
                s = s[:_SANITIZE_VALUE_LIMIT]
            if events.scan_value_for_denylist(s) is not None:
                s = _redact_event_value(s)
            return s

        return {
            "session_key": clean(event_meta.get("child_session_id")),
            "run_id": clean(event_meta.get("run_id")),
            "event_type": clean(event_meta.get("event_type")),
            "status": clean(event_meta.get("status")),
        }

    def _quarantine(
        self,
        task_id: Optional[str],
        dispatch_id: Optional[str],
        reason: str,
        event_meta,
    ) -> None:
        qid = str(uuid4())
        meta = json.dumps(self._sanitize_event_meta(event_meta), sort_keys=True)
        self._store._insert_quarantine(
            AgentResultQuarantine(
                id=qid,
                task_id=task_id,
                dispatch_id=dispatch_id,
                reason=reason,
                event_meta_json=meta,
                created_at=self._store.now_iso(),
            )
        )

    def _emit_rejected(
        self, task_id: Optional[str], reason: str, dispatch_id: Optional[str] = None
    ) -> None:
        self._emit(
            "agent.result_rejected",
            task_id=task_id,
            payload={"reason": reason, "dispatch_id": dispatch_id},
        )

    def _event_meta_mismatch(self, d: AgentDispatch, event_meta) -> Optional[str]:
        """Validate mandatory event metadata against the dispatch (16.4).

        V2.2 (F4): ``task_id``, ``child_session_id``, ``run_id``,
        ``parent_dispatch_id`` (exact, not optional), ``event_type`` and
        ``status`` are ALL mandatory; ``status`` must be ``completed`` or
        ``succeeded``.
        """
        if not isinstance(event_meta, dict):
            return "missing_metadata"
        for key in (
            "task_id",
            "child_session_id",
            "run_id",
            "parent_dispatch_id",
            "event_type",
            "status",
        ):
            if key not in event_meta:
                return "missing_metadata"
        if event_meta.get("task_id") != d.task_id:
            return "task_mismatch"
        if event_meta.get("child_session_id") != d.child_session_id:
            return "session_mismatch"
        if event_meta.get("run_id") != d.openclaw_run_id:
            return "run_id_mismatch"
        if event_meta.get("parent_dispatch_id") != d.parent_dispatch_id:
            return "parent_mismatch"
        if event_meta.get("status") not in ("completed", "succeeded"):
            return "invalid_status"
        return None

    def _result_envelope_mismatch(self, d: AgentDispatch, result) -> Optional[str]:
        if not isinstance(result, dict):
            return "malformed_output"
        if result.get("task_id") != d.task_id:
            return "task_mismatch"
        if result.get("dispatch_id") != d.id:
            return "dispatch_mismatch"
        if result.get("role") != d.role.value:
            return "role_mismatch"
        return None

    def _model_mismatch(self, d: AgentDispatch) -> Optional[str]:
        if d.actual_provider != d.expected_agent_class:
            return "provider_mismatch"
        if d.actual_model != d.expected_model_class:
            return "model_mismatch"
        if d.thinking_tier != d.expected_thinking_tier:
            return "thinking_mismatch"
        return None

    def _validate_effect_bindings(self, task_id: str, validated: dict) -> None:
        """Ensure agent-supplied finding IDs belong to the same task (15.7)."""
        for field in ("accepted_findings", "rejected_findings"):
            for fid in validated.get(field, []) or []:
                f = self._store.get_finding(fid)
                if f is None or f.task_id != task_id:
                    raise DispatchError(
                        f"finding {fid!r} does not belong to task {task_id!r}"
                    )

    def _changed_files(self, task_id: str) -> tuple[str, ...]:
        """Extract ``changed_files`` from the latest consumed implementer dispatch."""
        for d in reversed(self._store.list_dispatches(task_id)):
            if d.role is Role.IMPLEMENTER and d.status is DispatchStatus.CONSUMED:
                if d.result_json:
                    try:
                        data = json.loads(d.result_json)
                        files = data.get("changed_files") or []
                        return tuple(str(f) for f in files)
                    except (ValueError, TypeError):
                        return ()
        return ()

    def _apply_role_effects(
        self, d: AgentDispatch, validated: dict, task: Task
    ) -> None:
        now = self._store.now_iso()
        # Common findings -> findings table (finding.created).
        for f in validated.get("findings") or []:
            if not isinstance(f, dict):
                continue
            fid = str(uuid4())
            severity = str(f.get("severity", "medium"))
            # V2.3 (G2): title is a fallback for description (title-only
            # findings carry the title as their description).
            description = f.get("description") or f.get("title") or ""
            description = str(description)
            self._store._insert_finding(
                Finding(
                    id=fid,
                    task_id=d.task_id,
                    severity=severity,
                    description=description,
                    status=FindingStatus.OPEN,
                    created_at=now,
                )
            )
            self._emit("finding.created", task_id=d.task_id,
                       payload={"finding_id": fid, "severity": severity})

        if d.role is Role.LEAD:
            decision = validated["decision"]
            detail = json.dumps(
                {
                    "rationale": validated.get("rationale", ""),
                    "rework_include_reviewer": validated.get(
                        "rework_include_reviewer"
                    ),
                    "accepted_findings": validated.get("accepted_findings", []),
                    "rejected_findings": validated.get("rejected_findings", []),
                    "cycle_no": d.cycle_no,
                    "position": d.position,
                },
                sort_keys=True,
            )
            did = str(uuid4())
            self._store._insert_decision(
                Decision(
                    id=did,
                    task_id=d.task_id,
                    decision=decision,
                    detail=detail,
                    created_at=now,
                )
            )
            self._emit("lead.decision", task_id=d.task_id,
                       payload={"decision_id": did, "decision": decision})
            # Accepted findings are resolved; rejected findings stay open.
            for fid in validated.get("accepted_findings", []) or []:
                f = self._store.get_finding(fid)
                if f is not None and f.status is FindingStatus.OPEN:
                    self._store._update_finding_status(
                        fid, FindingStatus.RESOLVED, now
                    )
                    self._emit("finding.resolved", task_id=d.task_id,
                               payload={"finding_id": fid})
        elif d.role is Role.QA:
            for t in validated.get("tests", []) or []:
                name = ""
                result = "passed"
                if isinstance(t, dict):
                    name = str(t.get("name", ""))
                    result = str(t.get("result", "passed"))
                elif isinstance(t, str):
                    result = t
                tr = TestRun(
                    id=str(uuid4()),
                    task_id=d.task_id,
                    result=self._coerce_result(result),
                    detail=name or None,
                    created_at=now,
                )
                self._store._insert_test_run(tr)
                self._emit("test.started", task_id=d.task_id,
                           payload={"test_run_id": tr.id})
                self._emit("test.completed", task_id=d.task_id,
                           payload={"test_run_id": tr.id, "result": tr.result.value})
        elif d.role is Role.REVIEWER:
            rid = str(uuid4())
            verdict = str(validated.get("recommendation", ""))
            detail = json.dumps(
                {
                    "severity": validated.get("severity", ""),
                    "security_findings": validated.get("security_findings", []),
                    "architecture_findings": validated.get(
                        "architecture_findings", []
                    ),
                },
                sort_keys=True,
            )
            self._store._insert_review(
                Review(
                    id=rid,
                    task_id=d.task_id,
                    verdict=verdict,
                    detail=detail,
                    created_at=now,
                )
            )
            self._emit("review.started", task_id=d.task_id,
                       payload={"review_id": rid})
            self._emit("review.completed", task_id=d.task_id,
                       payload={"review_id": rid, "verdict": verdict})

    def _complete_active_role(self, d: AgentDispatch) -> None:
        active = self._store.get_active_role_run(d.task_id)
        if active is not None and active.role is d.role:
            self._store._update_role_run_status(
                active.id, RoleRunStatus.COMPLETED, self._store.now_iso()
            )
            self._emit("role.completed", task_id=d.task_id, role=d.role.value,
                       payload={"role": d.role.value})

    def _sequence_handoff(self, d: AgentDispatch) -> None:
        frontier = self._workflow_frontier(d.task_id)
        nxt = frontier.expected_role
        if nxt is not None:
            now = self._store.now_iso()
            self._store._insert_handoff(
                models_handoff(d.task_id, d.role, nxt, now)
            )
            self._emit("handoff.created", task_id=d.task_id,
                       payload={"from_role": d.role.value, "to_role": nxt.value})
            self._emit("handoff.accepted", task_id=d.task_id,
                       payload={"from_role": d.role.value, "to_role": nxt.value})

    def _state_sync_plan(
        self,
        d: AgentDispatch,
        decision: Optional[str],
        current_state: TaskState,
    ) -> list[tuple[TaskState, TaskState]]:
        """Deterministic ``(sequence_kind, position, decision)`` transition plan.

        V2.2 (F1): the consume transaction drives the authoritative state
        machine.  Returns the ordered ``(from_state, to_state)`` steps.
        """
        plan: list[tuple[TaskState, TaskState]] = []
        kind = d.sequence_kind
        pos = d.position
        role = d.role

        if kind is SequenceKind.STANDARD:
            base = {
                0: (TaskState.NEW, TaskState.PLANNING),
                1: (TaskState.PLANNING, TaskState.ANALYZING),
                2: (TaskState.ANALYZING, TaskState.LEAD_DECISION),
                3: (TaskState.LEAD_DECISION, TaskState.IMPLEMENTING),
                4: (TaskState.IMPLEMENTING, TaskState.TESTING),
                5: (TaskState.TESTING, TaskState.REVIEWING),
                6: (TaskState.REVIEWING, TaskState.FINAL_DECISION),
            }
            if pos in base:
                plan.append(base[pos])
            final_lead = pos == 6
        else:
            # REWORK: role-based (positions vary with the reviewer toggle).
            if role is Role.IMPLEMENTER:
                plan.append((TaskState.REWORK, TaskState.IMPLEMENTING))
            elif role is Role.QA:
                plan.append((TaskState.IMPLEMENTING, TaskState.TESTING))
            elif role is Role.REVIEWER:
                plan.append((TaskState.TESTING, TaskState.REVIEWING))
            elif role is Role.LEAD and pos > 0:
                # Final lead: reach FINAL_DECISION.  With a reviewer run the
                # task is already REVIEWING; without one it is still TESTING
                # (the reviewer step was skipped) — pass through REVIEWING.
                if current_state is TaskState.REVIEWING:
                    plan.append((TaskState.REVIEWING, TaskState.FINAL_DECISION))
                elif current_state is TaskState.TESTING:
                    plan.append((TaskState.TESTING, TaskState.REVIEWING))
                    plan.append((TaskState.REVIEWING, TaskState.FINAL_DECISION))
            final_lead = role is Role.LEAD and pos > 0

        if role is Role.LEAD:
            if kind is SequenceKind.STANDARD and pos == 0 and decision == "rework":
                # V2B 16.3 (F9): a rework decision at the first (spec) gate
                # enters the rework cycle via PLANNING -> REWORK (the frontier
                # already starts cycle 2; the state sync must follow).
                plan.append((TaskState.PLANNING, TaskState.REWORK))
            elif kind is SequenceKind.STANDARD and pos == 2:
                if decision == "rework":
                    plan.append((TaskState.LEAD_DECISION, TaskState.REWORK))
                elif decision == "cancel":
                    plan.append((TaskState.LEAD_DECISION, TaskState.CANCELLED))
            elif final_lead:
                if decision == "accept":
                    plan.append((TaskState.FINAL_DECISION, TaskState.DONE))
                elif decision == "rework":
                    plan.append((TaskState.FINAL_DECISION, TaskState.REWORK))
                elif decision == "cancel":
                    plan.append((TaskState.FINAL_DECISION, TaskState.CANCELLED))
            elif kind is SequenceKind.REWORK and pos == 0 and decision == "cancel":
                # Rework-start gate: a cancel must actually cancel (escape).
                plan.append((TaskState.REWORK, TaskState.CANCELLED))
        return plan

    def _apply_state_sync(
        self, task: Task, d: AgentDispatch, decision: Optional[str]
    ) -> Task:
        """Apply the workflow state-sync for a consumed dispatch (16.1).

        Runs inside the consume transaction.  If the task is parked in
        ``RECOVERING`` (by ``recover()``) it is resumed to its pre-recovery
        state first, then the deterministic plan is applied step by step via
        ``state_machine.validate_transition`` (fail-closed).
        """
        current = task
        if current.state is TaskState.RECOVERING:
            rs = current.resume_state
            if rs is None:
                raise InvalidTransition("RECOVERING without a resume_state")
            state_machine.validate_transition(current.state, rs, rs)
            self._apply_transition(current, rs, None)
            current = self._store.get_task(d.task_id)
        plan = self._state_sync_plan(d, decision, current.state)
        for from_state, to_state in plan:
            if current.state != from_state:
                raise InvalidTransition(
                    f"expected task state {from_state.value} for state sync, "
                    f"got {current.state.value}"
                )
            state_machine.validate_transition(
                current.state, to_state, current.resume_state
            )
            self._apply_transition(current, to_state, None)
            current = self._store.get_task(d.task_id)
        return current

    # -- public orchestration commands ---------------------------------------

    def create_dispatch(
        self,
        task_id: str,
        task_run_id: str,
        role,
        position: int,
        cycle_no: int,
        sequence_kind,
        model_choice,
        source: str,
        parent_dispatch_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> AgentDispatch:
        self._require_controller(source)
        role = self._coerce_role(role)
        sequence_kind = (
            sequence_kind
            if isinstance(sequence_kind, SequenceKind)
            else SequenceKind(sequence_kind)
        )
        args = {
            "task_id": task_id,
            "task_run_id": task_run_id,
            "role": role.value,
            "position": position,
            "cycle_no": cycle_no,
            "sequence_kind": sequence_kind.value,
            "model_choice": model_choice,
            "source": source,
            "parent_dispatch_id": parent_dispatch_id,
        }

        # Resolve/validate the model choice up front (outside the idempotent
        # transaction) so a policy violation emits its event in a committed
        # transaction before raising.
        task0 = self._store.get_task(task_id)
        if task0 is None:
            raise NotFound(f"task {task_id!r} not found")
        if model_choice is None:
            provider, model, thinking = routing.resolve_model(role, task0.risk_class)
        else:
            provider = model_choice.get("provider")
            model = model_choice.get("model")
            thinking = model_choice.get("thinking_tier")
        if not routing.validate_model_choice(
            role, provider, model, thinking, task0.risk_class
        ):
            self._emit(
                "policy.role_violation",
                task_id=task_id,
                role=role.value,
                payload={"reason": "model_choice"},
            )
            raise RolePolicyViolation(
                f"invalid model choice for {role.value}: "
                f"{provider}/{model}/{thinking}"
            )

        def work():
            task = self._store.get_task(task_id)
            if task is None:
                raise NotFound(f"task {task_id!r} not found")
            active = self._store.get_active_role_run(task_id)
            if active is None or active.role is not role:
                raise PermissionDenied(
                    f"task {task_id!r} has no active {role.value} role run"
                )
            if task.state in TERMINAL_STATES or task.state in PAUSE_STATES:
                raise InvalidTransition(
                    f"cannot dispatch from state {task.state.value}"
                )
            frontier = self._workflow_frontier(task_id)
            if frontier.expected_role is not role:
                raise RoleConflict(
                    f"expected next role is "
                    f"{frontier.expected_role.value if frontier.expected_role else None!r}, "
                    f"got {role.value!r}"
                )
            if (
                cycle_no != frontier.cycle_no
                or position != frontier.position
                or sequence_kind is not frontier.sequence_kind
            ):
                raise DispatchError(
                    "dispatch position/cycle/kind does not match workflow frontier"
                )
            for d in self._store.list_dispatches(task_id):
                if d.status in (
                    DispatchStatus.PENDING,
                    DispatchStatus.RUNNING,
                    DispatchStatus.RECOVERY_PENDING,
                ):
                    raise DispatchError("task already has an active dispatch")
            tr = self._store.get_task_run(task_run_id)
            if tr is None or tr.task_id != task_id:
                raise DispatchError(
                    f"task_run {task_run_id!r} does not belong to task {task_id!r}"
                )
            # V2.2 (F4): parent_dispatch_id is mandatory; None = controller,
            # otherwise it must reference an existing dispatch.
            if parent_dispatch_id is not None:
                if self._store.get_dispatch(parent_dispatch_id) is None:
                    raise DispatchError(
                        f"parent dispatch {parent_dispatch_id!r} does not exist"
                    )
            existing = [
                d
                for d in self._store.list_dispatches(task_id)
                if d.cycle_no == cycle_no and d.position == position
            ]
            attempt_no = len(existing) + 1
            latest = self._store.get_latest_handoff(task_id)
            handoff_id = latest.id if latest is not None else None
            did = str(uuid4())
            d = AgentDispatch(
                id=did,
                task_id=task_id,
                task_run_id=task_run_id,
                role=role,
                parent_dispatch_id=parent_dispatch_id,
                expected_agent_class=provider,
                expected_model_class=model,
                expected_thinking_tier=thinking,
                child_session_id=None,
                openclaw_run_id=None,
                actual_provider=None,
                actual_model=None,
                thinking_tier=None,
                status=DispatchStatus.PENDING,
                cycle_no=cycle_no,
                position=position,
                sequence_kind=sequence_kind,
                attempt_no=attempt_no,
                handoff_id=handoff_id,
                result_json=None,
                created_at=self._store.now_iso(),
                started_at=None,
                consumed_at=None,
            )
            self._store._insert_dispatch(d)
            self._emit(
                "agent.dispatch_created",
                task_id=task_id,
                role=role.value,
                payload={
                    "dispatch_id": did,
                    "position": position,
                    "cycle_no": cycle_no,
                },
            )
            self._emit(
                "handoff.expected",
                task_id=task_id,
                role=role.value,
                payload={"dispatch_id": did},
            )
            return d, did

        return self._idempotent(
            idempotency_key, "create_dispatch", args, work,
            lambda rid: self._refetch("create_dispatch", rid),
        )

    def bind_spawn_result(
        self,
        dispatch_id: str,
        child_session_id: str,
        openclaw_run_id: str,
        actual_provider: str,
        actual_model: str,
        thinking_tier: str,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> AgentDispatch:
        self._require_controller(source)
        args = {
            "dispatch_id": dispatch_id,
            "child_session_id": child_session_id,
            "openclaw_run_id": openclaw_run_id,
            "actual_provider": actual_provider,
            "actual_model": actual_model,
            "thinking_tier": thinking_tier,
            "source": source,
        }

        # V2.3 (G1): status read, exact-equality check, policy check AND the
        # status change all run inside ONE ``BEGIN IMMEDIATE`` block (no stale
        # ``d0`` read outside).  The mismatch path rejects via CAS so it can
        # never overwrite a dispatch a parallel valid bind moved to RUNNING.
        args_hash = _hash_args(args)
        reject: Optional[tuple] = None
        with self._store._transaction():
            if idempotency_key is not None:
                existing = self._store.get_command_idempotency(
                    idempotency_key, "bind_spawn_result"
                )
                if existing is not None:
                    result_id, stored_hash = existing
                    if stored_hash != args_hash:
                        raise IdempotencyError(
                            f"idempotency key {idempotency_key!r} reused for "
                            "bind_spawn_result with different arguments"
                        )
                    return self._refetch("bind_spawn_result", result_id)

            d = self._store.get_dispatch(dispatch_id)
            if d is None:
                raise NotFound(f"dispatch {dispatch_id!r} not found")
            if d.status not in (
                DispatchStatus.PENDING, DispatchStatus.RECOVERY_PENDING
            ):
                raise DispatchError(
                    f"dispatch {dispatch_id!r} is not PENDING/RECOVERY_PENDING "
                    f"({d.status.value})"
                )
            task = self._store.get_task(d.task_id)
            # V2.2 (F4): exact equality of every spawn value with the expected
            # values, ADDITIONALLY to the role policy (validate_model_choice).
            policy_ok = routing.validate_model_choice(
                d.role, actual_provider, actual_model, thinking_tier,
                task.risk_class,
            )
            exact_ok = (
                actual_provider == d.expected_agent_class
                and actual_model == d.expected_model_class
                and thinking_tier == d.expected_thinking_tier
            )
            if not policy_ok or not exact_ok:
                # CAS reject: rowcount must be 1, otherwise a parallel valid
                # bind already won and we must NOT overwrite it.
                rc = self._store._reject_dispatch_cas(dispatch_id)
                if rc != 1:
                    raise DispatchError(
                        f"dispatch {dispatch_id!r} could not be rejected "
                        "(already bound)"
                    )
                self._emit(
                    "policy.role_violation",
                    task_id=d.task_id,
                    role=d.role.value,
                    payload={"reason": "model_mismatch"},
                )
                reject = (d.role, actual_provider, actual_model, thinking_tier)
            else:
                try:
                    rc = self._store._update_dispatch_bind(
                        dispatch_id,
                        child_session_id,
                        openclaw_run_id,
                        actual_provider,
                        actual_model,
                        thinking_tier,
                        self._store.now_iso(),
                    )
                except sqlite3.IntegrityError as exc:
                    raise DispatchError(
                        f"session/run already bound to another dispatch: {exc}"
                    ) from exc
                if rc != 1:
                    raise DispatchError(
                        f"dispatch {dispatch_id!r} could not be bound"
                    )
                self._emit(
                    "agent.started",
                    task_id=d.task_id,
                    role=d.role.value,
                    payload={"dispatch_id": dispatch_id},
                )

            if idempotency_key is not None:
                self._store._set_command_idempotency(
                    idempotency_key,
                    "bind_spawn_result",
                    dispatch_id,
                    args_hash,
                    self._store.now_iso(),
                )

        if reject is not None:
            role, prov, model, think = reject
            raise RolePolicyViolation(
                f"model mismatch for {role.value}: {prov}/{model}/{think}"
            )

        return self._store.get_dispatch(dispatch_id)

    def receive_agent_result(
        self,
        dispatch_id: str,
        event_meta,
        result,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> ReceiveResult:
        self._require_controller(source)
        args = {
            "dispatch_id": dispatch_id,
            "event_meta": event_meta,
            "result": result,
            "source": source,
        }
        args_hash = _hash_args(args)

        with self._store._transaction():
            if idempotency_key is not None:
                existing = self._store.get_command_idempotency(
                    idempotency_key, "receive_agent_result"
                )
                if existing is not None:
                    _result_id, stored_hash = existing
                    if stored_hash != args_hash:
                        raise IdempotencyError(
                            f"idempotency key {idempotency_key!r} reused for "
                            "receive_agent_result with different arguments"
                        )
                    d = self._store.get_dispatch(dispatch_id)
                    if d is not None and d.status is DispatchStatus.CONSUMED:
                        return ReceiveResult(dispatch_id, "duplicate")
                    return ReceiveResult(dispatch_id, "rejected")
            res = self._receive_work(dispatch_id, event_meta, result)
            if idempotency_key is not None:
                self._store._set_command_idempotency(
                    idempotency_key,
                    "receive_agent_result",
                    dispatch_id,
                    args_hash,
                    self._store.now_iso(),
                )
            return res

    def _receive_work(self, dispatch_id, event_meta, result) -> ReceiveResult:
        d = self._store.get_dispatch(dispatch_id)
        if d is None:
            claimed_task = event_meta.get("task_id") if isinstance(event_meta, dict) else None
            self._quarantine(claimed_task, dispatch_id, "dispatch_unknown", event_meta)
            self._emit_rejected(claimed_task, "dispatch_unknown", dispatch_id)
            return ReceiveResult(dispatch_id, "unknown", reason="dispatch_unknown")

        task = self._store.get_task(d.task_id)

        # Status-based handling first (state-independent, fail-closed).
        if d.status is DispatchStatus.CONSUMED:
            # Duplicate idempotency is valid ONLY for the same run: verify the
            # event identity against the stored bindings before swallowing the
            # re-delivery (SPEC V2 3.3 / V2.1 15.3).  A foreign/fabricated
            # completion event for an already-consumed dispatch must be
            # quarantined and rejected, never silently accepted as a duplicate.
            mismatch = self._event_meta_mismatch(d, event_meta)
            if mismatch is None:
                self._emit(
                    "agent.result_duplicate",
                    task_id=d.task_id,
                    payload={"dispatch_id": dispatch_id},
                )
                return ReceiveResult(dispatch_id, "duplicate")
            self._quarantine(d.task_id, dispatch_id, mismatch, event_meta)
            self._emit_rejected(d.task_id, mismatch, dispatch_id)
            return ReceiveResult(dispatch_id, "rejected", reason=mismatch)
        if d.status in (
            DispatchStatus.FAILED,
            DispatchStatus.REJECTED,
            DispatchStatus.QUARANTINED,
        ):
            self._quarantine(d.task_id, dispatch_id, "stale_dispatch", event_meta)
            self._emit_rejected(d.task_id, "stale_dispatch", dispatch_id)
            return ReceiveResult(dispatch_id, "rejected", reason="stale_dispatch")
        if d.status is DispatchStatus.PENDING:
            self._quarantine(d.task_id, dispatch_id, "pending_injection", event_meta)
            self._emit_rejected(d.task_id, "pending_injection", dispatch_id)
            return ReceiveResult(dispatch_id, "rejected", reason="pending_injection")

        # Mandatory event metadata + result envelope identity (fail-closed).
        mismatch = self._event_meta_mismatch(d, event_meta)
        if mismatch is None:
            mismatch = self._result_envelope_mismatch(d, result)
        if mismatch is None:
            mismatch = self._model_mismatch(d)
        if mismatch is not None:
            self._quarantine(d.task_id, dispatch_id, mismatch, event_meta)
            self._emit_rejected(d.task_id, mismatch, dispatch_id)
            return ReceiveResult(dispatch_id, "rejected", reason=mismatch)

        # Full binding required (SPEC V2 15.3).
        if d.child_session_id is None or d.openclaw_run_id is None:
            self._quarantine(d.task_id, dispatch_id, "not_bound", event_meta)
            self._emit_rejected(d.task_id, "not_bound", dispatch_id)
            return ReceiveResult(dispatch_id, "rejected", reason="not_bound")

        # Current task run.
        latest_run = self._store.get_latest_task_run(d.task_id)
        if latest_run is None or d.task_run_id != latest_run.id:
            self._quarantine(d.task_id, dispatch_id, "stale_run", event_meta)
            self._emit_rejected(d.task_id, "stale_run", dispatch_id)
            return ReceiveResult(dispatch_id, "rejected", reason="stale_run")

        # Active role must match the dispatch role.
        active = self._store.get_active_role_run(d.task_id)
        if active is None or active.role is not d.role:
            self._quarantine(d.task_id, dispatch_id, "role_mismatch", event_meta)
            self._emit_rejected(d.task_id, "role_mismatch", dispatch_id)
            return ReceiveResult(dispatch_id, "rejected", reason="role_mismatch")

        # Handoff must be the currently expected open handoff.
        latest_handoff = self._store.get_latest_handoff(d.task_id)
        expected_handoff = latest_handoff.id if latest_handoff else None
        if d.handoff_id != expected_handoff:
            self._quarantine(d.task_id, dispatch_id, "handoff_mismatch", event_meta)
            self._emit_rejected(d.task_id, "handoff_mismatch", dispatch_id)
            return ReceiveResult(dispatch_id, "rejected", reason="handoff_mismatch")

        # Task not terminal.
        if task is None or task.state in TERMINAL_STATES:
            self._quarantine(d.task_id, dispatch_id, "task_ended", event_meta)
            self._emit_rejected(d.task_id, "task_ended", dispatch_id)
            return ReceiveResult(dispatch_id, "rejected", reason="task_ended")

        # Structured output validation (fail-closed; malformed -> REJECTED).
        try:
            validated = outputs.validate_role_output(d.role, result)
        except OutputValidationError:
            self._store._update_dispatch_status(
                dispatch_id, DispatchStatus.REJECTED, self._store.now_iso()
            )
            self._quarantine(d.task_id, dispatch_id, "malformed_output", event_meta)
            self._emit_rejected(d.task_id, "malformed_output", dispatch_id)
            return ReceiveResult(dispatch_id, "rejected", reason="malformed_output")

        # Task-bound effect validation (rolls back on failure).
        self._validate_effect_bindings(d.task_id, validated)

        # CAS consume (atomic; rowcount == 1 required).
        rc = self._store._consume_dispatch(
            dispatch_id,
            json.dumps(validated, sort_keys=True),
            self._store.now_iso(),
        )
        if rc != 1:
            self._emit(
                "agent.result_duplicate",
                task_id=d.task_id,
                payload={"dispatch_id": dispatch_id},
            )
            return ReceiveResult(dispatch_id, "duplicate")

        self._emit(
            "agent.result_received",
            task_id=d.task_id,
            role=d.role.value,
            payload={"dispatch_id": dispatch_id},
        )
        self._apply_role_effects(d, validated, task)
        self._complete_active_role(d)
        self._sequence_handoff(d)
        # V2.2 (F1): synchronize the authoritative task state in the same
        # consume transaction (state machine is the single source of truth).
        self._apply_state_sync(task, d, validated.get("decision"))
        self._emit(
            "agent.result_accepted",
            task_id=d.task_id,
            role=d.role.value,
            payload={"dispatch_id": dispatch_id},
        )
        self._emit(
            "agent.completed",
            task_id=d.task_id,
            role=d.role.value,
            payload={"dispatch_id": dispatch_id},
        )
        return ReceiveResult(dispatch_id, "consumed")

    def mark_agent_failed(
        self,
        dispatch_id: str,
        reason: str,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> AgentDispatch:
        self._require_controller(source)
        args = {"dispatch_id": dispatch_id, "reason": reason, "source": source}

        def work():
            d = self._store.get_dispatch(dispatch_id)
            if d is None:
                raise NotFound(f"dispatch {dispatch_id!r} not found")
            if d.status not in (
                DispatchStatus.RUNNING,
                DispatchStatus.RECOVERY_PENDING,
            ):
                raise DispatchError(
                    f"dispatch {dispatch_id!r} is not RUNNING/RECOVERY_PENDING "
                    f"({d.status.value})"
                )
            now = self._store.now_iso()
            self._store._update_dispatch_status(
                dispatch_id, DispatchStatus.FAILED, now
            )
            active = self._store.get_active_role_run(d.task_id)
            if active is not None and active.role is d.role:
                self._store._update_role_run_status(
                    active.id, RoleRunStatus.FAILED, now
                )
                self._emit("role.failed", task_id=d.task_id, role=d.role.value,
                           payload={"role": d.role.value, "detail": "agent_failed"})
            # Retry handoff to the SAME role (attempt_no + 1).
            self._store._insert_handoff(
                models_handoff(d.task_id, d.role, d.role, now)
            )
            self._emit("handoff.created", task_id=d.task_id,
                       payload={"from_role": d.role.value, "to_role": d.role.value})
            self._emit("agent.failed", task_id=d.task_id, role=d.role.value,
                       payload={"dispatch_id": dispatch_id, "reason": reason})
            return self._store.get_dispatch(dispatch_id), dispatch_id

        return self._idempotent(
            idempotency_key, "mark_agent_failed", args, work,
            lambda rid: self._refetch("mark_agent_failed", rid),
        )

    # -- controller helpers ---------------------------------------------------

    def build_agent_context(
        self,
        task_id: str,
        role,
        position: int,
        repo_summary,
        source: str,
    ) -> dict:
        self._require_controller(source)
        role = self._coerce_role(role)
        task = self._store.get_task(task_id)
        if task is None:
            raise NotFound(f"task {task_id!r} not found")
        sections = context.build_agent_context(
            task,
            role,
            position,
            repo_summary,
            findings=tuple(self._store.list_findings(task_id)),
            decisions=tuple(self._store.list_decisions(task_id)),
            test_runs=tuple(self._store.list_test_runs(task_id)),
            reviews=tuple(self._store.list_reviews(task_id)),
            changed_files=self._changed_files(task_id),
        )
        return sections

    def snapshot_agent_context(
        self,
        dispatch_id: str,
        role,
        position: int,
        repo_summary,
        source: str,
    ) -> AgentContextSnapshot:
        """Persist an immutable context snapshot for a dispatch (15.8 / 16.5)."""
        self._require_controller(source)
        role = self._coerce_role(role)
        d = self._store.get_dispatch(dispatch_id)
        if d is None:
            raise NotFound(f"dispatch {dispatch_id!r} not found")
        # V2.2 (F5): role/position must match the dispatch; repo_summary is
        # allow-list/limit/deny-list filtered (metadata only, no full diffs).
        if role is not d.role:
            raise DispatchError(
                f"snapshot role {role.value!r} does not match dispatch role "
                f"{d.role.value!r}"
            )
        if position != d.position:
            raise DispatchError(
                f"snapshot position {position!r} does not match dispatch "
                f"position {d.position!r}"
            )
        filtered_repo = context.filter_repo_summary(repo_summary)
        sections = self.build_agent_context(
            d.task_id, role, position, filtered_repo, source
        )
        snap = AgentContextSnapshot(
            dispatch_id=dispatch_id,
            role=role,
            position=position,
            context_hash=context.context_hash(sections),
            context_summary_json=context.context_summary_json(sections),
            created_at=self._store.now_iso(),
        )
        with self._store._transaction():
            existing = self._store.get_context_snapshot(dispatch_id)
            if existing is not None:
                if existing.context_hash == snap.context_hash:
                    return existing  # idempotent re-snapshot of the same content
                raise DispatchError(
                    f"context snapshot for dispatch {dispatch_id!r} already "
                    "exists with different content"
                )
            self._store._insert_context_snapshot(snap)
        return snap

    def list_dispatches(
        self, source: str, task_id: Optional[str] = None,
        status: Optional[DispatchStatus] = None,
    ) -> list[AgentDispatch]:
        self._check_source(source)
        if status is not None and not isinstance(status, DispatchStatus):
            status = DispatchStatus(status)
        return self._store.list_dispatches(task_id, status)

    def quarantine_log(
        self, source: str, task_id: Optional[str] = None
    ) -> list[AgentResultQuarantine]:
        self._check_source(source)
        return self._store.list_quarantine(task_id)

    # ------------------------------------------------------------- recovery

    def recover(self, source: str, idempotency_key: Optional[str] = None) -> RecoveryReport:
        self._require_owner(source)
        args = {"source": source}

        def work():
            report = self._recover_work()
            return report, "recovered"

        return self._idempotent(
            idempotency_key, "recover", args, work,
            lambda rid: RecoveryReport(0, 0, ()),
        )

    def _recover_work(self) -> RecoveryReport:
        now = self._store.now_iso()
        self._emit("system.recovery_started", payload={})

        # --- Phase 2A: dispatch recovery ------------------------------------
        conservative_tasks: set[str] = set()
        # Pre-existing RECOVERY_PENDING dispatches from a previous recover()
        # must stay unresolved (their role/task runs stay STARTED) — 16.2.
        recovery_pending_dispatches: list[AgentDispatch] = list(
            self._store.list_dispatches(status=DispatchStatus.RECOVERY_PENDING)
        )
        for d in recovery_pending_dispatches:
            conservative_tasks.add(d.task_id)

        for d in self._store.list_dispatches():
            if d.status not in (DispatchStatus.PENDING, DispatchStatus.RUNNING):
                continue
            if d.role is Role.IMPLEMENTER:
                # Write role: NEVER auto-failed (SPEC V2 15.2, ghost-writer rule).
                self._store._update_dispatch_status(
                    d.id, DispatchStatus.RECOVERY_PENDING, now
                )
                self._emit("agent.recovery_pending", task_id=d.task_id,
                           role=d.role.value,
                           payload={"dispatch_id": d.id})
                conservative_tasks.add(d.task_id)
                recovery_pending_dispatches.append(
                    self._store.get_dispatch(d.id)
                )
            elif d.status is DispatchStatus.PENDING:
                # Read-only role, never spawned -> FAILED (harmless, redispatch).
                self._store._update_dispatch_status(
                    d.id, DispatchStatus.FAILED, now
                )
            else:
                # Read-only role RUNNING -> RECOVERY_PENDING (result may arrive).
                self._store._update_dispatch_status(
                    d.id, DispatchStatus.RECOVERY_PENDING, now
                )
                self._emit("agent.recovery_pending", task_id=d.task_id,
                           role=d.role.value,
                           payload={"dispatch_id": d.id})
                conservative_tasks.add(d.task_id)
                recovery_pending_dispatches.append(
                    self._store.get_dispatch(d.id)
                )

        # Move conservative tasks into RECOVERING (non-terminal, non-pause).
        for tid in conservative_tasks:
            task = self._store.get_task(tid)
            if task is None:
                continue
            if task.state in TERMINAL_STATES or task.state in PAUSE_STATES:
                continue
            self._store._update_task_state(tid, TaskState.RECOVERING, task.state, now)
            self._emit("task.state_changed", task_id=tid,
                       state=TaskState.RECOVERING.value,
                       payload={"from_state": task.state.value,
                                "to_state": TaskState.RECOVERING.value})

        # --- Role runs: fail started, except unresolved dispatches ----------
        unresolved = {(d.task_id, d.role.value) for d in recovery_pending_dispatches}
        unresolved_run_ids = {d.task_run_id for d in recovery_pending_dispatches}

        interrupted_role_runs = self._store.list_role_runs(status=RoleRunStatus.STARTED)
        for rr in interrupted_role_runs:
            if (rr.task_id, rr.role.value) in unresolved:
                continue  # keep STARTED (result may still arrive)
            self._store._update_role_run_status(rr.id, RoleRunStatus.FAILED, now)
            self._emit("role.failed", task_id=rr.task_id, role=rr.role.value,
                       payload={"role": rr.role.value, "detail": "interrupted"})

        interrupted_task_runs = self._store.list_task_runs(status=TaskRunStatus.STARTED)
        for tr in interrupted_task_runs:
            if tr.id in unresolved_run_ids:
                continue  # referenced by an unresolved dispatch
            self._store._update_task_run_status(tr.id, TaskRunStatus.FAILED, now)

        rolled_back = []
        for task, valid in self._store.list_tasks_for_recovery():
            if task.id in conservative_tasks:
                continue  # left in RECOVERING conservatively
            if not valid:
                # Defensive (SPEC V1.2 12.4): unknown resume_state -> BLOCKED,
                # the rest of the recovery continues.
                self._store._update_task_state(task.id, TaskState.BLOCKED, None, now)
                self._emit("task.state_changed", task_id=task.id,
                           state=TaskState.BLOCKED.value,
                           payload={"from_state": task.state.value,
                                    "to_state": TaskState.BLOCKED.value})
                rolled_back.append(
                    (task.id, task.state.value, TaskState.BLOCKED.value)
                )
                continue
            target = recovery.recovery_target(task)
            if target is not task.state:
                # Defense in depth (SPEC V1.3 13.1): reuse the state-machine
                # validation before applying the recovery target; on failure,
                # fall back to BLOCKED.
                try:
                    state_machine.validate_transition(
                        TaskState.RECOVERING, target, task.resume_state
                    )
                except InvalidTransition:
                    target = TaskState.BLOCKED
                if target is not task.state:
                    self._store._update_task_state(task.id, target, None, now)
                    self._emit("task.state_changed", task_id=task.id, state=target.value,
                               payload={"from_state": task.state.value, "to_state": target.value})
                    rolled_back.append((task.id, task.state.value, target.value))

        self._emit("system.recovery_completed",
                   payload={"interrupted_role_runs": len(interrupted_role_runs),
                            "interrupted_task_runs": len(interrupted_task_runs),
                            "recovery_pending_dispatches": len(recovery_pending_dispatches)})
        return RecoveryReport(
            interrupted_role_runs=len(interrupted_role_runs),
            interrupted_task_runs=len(interrupted_task_runs),
            rolled_back=tuple(rolled_back),
            recovery_pending_dispatches=len(recovery_pending_dispatches),
        )

    # ----------------------------------------------------------------- events

    def list_events(self, source: str, task_id: Optional[str] = None):
        self._check_source(source)
        return self._store.list_events(task_id)


def models_handoff(task_id: str, from_role: Role, to_role: Role, created_at: str):
    from .models import Handoff

    return Handoff(id=str(uuid4()), task_id=task_id, from_role=from_role,
                   to_role=to_role, created_at=created_at)
