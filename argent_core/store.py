"""SQLite persistence layer (SPEC V1 chapter 5 + V1.1 11.5).

Schema V2 hardenings (R8, R14):

- ``CHECK`` constraints on ``tasks.state``, ``role_runs.role/status``,
  ``owner_approvals.status``, all ``source_class`` columns and
  ``action_executions.status``.
- A partial UNIQUE index on ``role_runs(task_id) WHERE status='started'``
  (exactly one active role run per task).
- A new ``action_executions`` table for persisted gated-action executions (R13).
- ``command_idempotency`` gains an ``args_hash`` column (R9).

Encapsulation (R8): the connection and every mutator are private (only ``Core``
calls them).  ``Core.queries`` exposes a strictly read-only ``get_*``/``list_*``
facade built on the ``Queries`` class below.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator, Optional

from . import events as events_mod
from . import job_state
from .gates import binding_hash
from .models import (
    ActionExecution,
    ActionExecutionStatus,
    AgentContextSnapshot,
    AgentDispatch,
    AgentResultQuarantine,
    ApprovalStatus,
    Decision,
    DispatchStatus,
    Event,
    ExternalActionsPolicy,
    Finding,
    FindingStatus,
    Handoff,
    LeaseError,
    LeaseFencedError,
    NotFound,
    OwnerApproval,
    Project,
    RiskClass,
    Role,
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
)

# B1 (Phase B): durable queue / lease schema version bump.
SCHEMA_VERSION = "7"

_TASK_STATES = "', '".join(s.value for s in TaskState)
_TASK_RUN_STATUSES = "', '".join(s.value for s in TaskRunStatus)
_ROLE_VALUES = "', '".join(r.value for r in Role)
_ROLE_RUN_STATUSES = "', '".join(s.value for s in RoleRunStatus)
_APPROVAL_STATUSES = "', '".join(s.value for s in ApprovalStatus)
_SOURCE_CLASSES = "', '".join(s.value for s in SourceClass)
_EXECUTION_STATUSES = "', '".join(s.value for s in ActionExecutionStatus)
_RISK_CLASSES = "', '".join(r.value for r in RiskClass)
_EXT_ACTION_POLICIES = "', '".join(p.value for p in ExternalActionsPolicy)
_DISPATCH_STATUSES = "', '".join(s.value for s in DispatchStatus)
_SEQUENCE_KINDS = "', '".join(k.value for k in SequenceKind)
_PRIMARY_STATES = "', '".join(job_state.PRIMARY_STATE_VALUES)

# B1 (F7): upper policy bound for any lease TTL (caller-supplied local policy).
# Values above this are rejected fail-closed so a misconfigured controller can
# never mint a near-infinite lease.
MAX_LEASE_TTL_SECONDS = 86400  # 24h

# B1 (F6): the orthogonal enum columns that must be value-validated on every
# write path (enqueue/claim/update).  ``None`` values (nullable paths) skip.
_ENUM_FIELDS: dict[str, type] = {
    "queue_reason": job_state.QueueReason,
    "wait_kind": job_state.WaitKind,
    "error_class": job_state.ErrorClass,
}


def _validate_enum_fields(fields: dict) -> None:
    """Reject invalid values for the orthogonal B1 enum columns (F6).

    Raises :class:`ValueError` on any value not present in the matching enum;
    ``None`` values are skipped (nullable call sites).
    """
    for key, enum_cls in _ENUM_FIELDS.items():
        if key in fields and fields[key] is not None:
            valid = {m.value for m in enum_cls}
            if fields[key] not in valid:
                raise ValueError(
                    f"invalid {key} {fields[key]!r}; expected one of "
                    f"{sorted(valid)}"
                )

_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS projects (
        id             TEXT PRIMARY KEY,
        name           TEXT NOT NULL,
        created_at     TEXT NOT NULL,
        idempotency_key TEXT UNIQUE
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS tasks (
        id             TEXT PRIMARY KEY,
        project_id     TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
        title          TEXT NOT NULL,
        description    TEXT,
        state          TEXT NOT NULL CHECK (state IN ('{_TASK_STATES}')),
        resume_state   TEXT CHECK (resume_state IS NULL OR resume_state IN ('{_TASK_STATES}')),
        source         TEXT NOT NULL,
        source_class   TEXT NOT NULL CHECK (source_class IN ('{_SOURCE_CLASSES}')),
        risk_class     TEXT NOT NULL DEFAULT 'NORMAL' CHECK (risk_class IN ('{_RISK_CLASSES}')),
        external_actions_policy TEXT NOT NULL DEFAULT 'ALLOWED_WITH_GATE' CHECK (external_actions_policy IN ('{_EXT_ACTION_POLICIES}')),
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL,
        idempotency_key TEXT UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_runs (
        id             TEXT PRIMARY KEY,
        task_id        TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        status         TEXT NOT NULL CHECK (status IN ('%s')),
        started_at     TEXT NOT NULL,
        finished_at    TEXT,
        idempotency_key TEXT UNIQUE
    )
    """
    % _TASK_RUN_STATUSES,
    f"""
    CREATE TABLE IF NOT EXISTS role_runs (
        id             TEXT PRIMARY KEY,
        task_id        TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        role           TEXT NOT NULL CHECK (role IN ('{_ROLE_VALUES}')),
        status         TEXT NOT NULL CHECK (status IN ('{_ROLE_RUN_STATUSES}')),
        started_at     TEXT NOT NULL,
        finished_at    TEXT,
        idempotency_key TEXT UNIQUE
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_role_runs_active
        ON role_runs (task_id)
        WHERE status = 'started'
    """,
    """
    CREATE TABLE IF NOT EXISTS handoffs (
        id             TEXT PRIMARY KEY,
        task_id        TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        from_role      TEXT NOT NULL,
        to_role        TEXT NOT NULL,
        created_at     TEXT NOT NULL,
        idempotency_key TEXT UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS findings (
        id             TEXT PRIMARY KEY,
        task_id        TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        severity       TEXT NOT NULL,
        description    TEXT NOT NULL,
        status         TEXT NOT NULL,
        created_at     TEXT NOT NULL,
        resolved_at    TEXT,
        idempotency_key TEXT UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS test_runs (
        id             TEXT PRIMARY KEY,
        task_id        TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        result         TEXT NOT NULL,
        detail         TEXT,
        created_at     TEXT NOT NULL,
        idempotency_key TEXT UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reviews (
        id             TEXT PRIMARY KEY,
        task_id        TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        verdict        TEXT NOT NULL,
        detail         TEXT,
        created_at     TEXT NOT NULL,
        idempotency_key TEXT UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decisions (
        id             TEXT PRIMARY KEY,
        task_id        TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        decision       TEXT NOT NULL,
        detail         TEXT,
        created_at     TEXT NOT NULL,
        idempotency_key TEXT UNIQUE
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS owner_approvals (
        id             TEXT PRIMARY KEY,
        task_id        TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        action         TEXT NOT NULL,
        scope          TEXT NOT NULL,
        status         TEXT NOT NULL CHECK (status IN ('{_APPROVAL_STATUSES}')),
        requested_by   TEXT NOT NULL,
        source_class   TEXT NOT NULL CHECK (source_class IN ('{_SOURCE_CLASSES}')),
        created_at     TEXT NOT NULL,
        decided_at     TEXT,
        consumed_at    TEXT,
        expires_at     TEXT NOT NULL,
        idempotency_key TEXT UNIQUE,
        binding_hash   TEXT NOT NULL,
        approved_at    TEXT,
        execution_id   TEXT,
        executed_at    TEXT,
        closed_at      TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_owner_approvals_unique_active
        ON owner_approvals (task_id, action, scope)
        WHERE status IN ('pending', 'approved')
    """,
    f"""
    CREATE TABLE IF NOT EXISTS action_executions (
        id          TEXT PRIMARY KEY,
        task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        approval_id TEXT REFERENCES owner_approvals(id) ON DELETE SET NULL,
        action      TEXT NOT NULL,
        scope       TEXT NOT NULL,
        actor_role  TEXT NOT NULL,
        status      TEXT NOT NULL CHECK (status IN ('{_EXECUTION_STATUSES}')),
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id           TEXT PRIMARY KEY,
        type         TEXT NOT NULL,
        task_id      TEXT,
        role         TEXT,
        state        TEXT,
        payload_json TEXT NOT NULL,
        created_at   TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS command_idempotency (
        key        TEXT NOT NULL,
        command    TEXT NOT NULL,
        result_id  TEXT,
        args_hash  TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (key, command)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS agent_dispatches (
        id                  TEXT PRIMARY KEY,
        task_id             TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        task_run_id         TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
        role                TEXT NOT NULL CHECK (role IN ('{_ROLE_VALUES}')),
        parent_dispatch_id  TEXT REFERENCES agent_dispatches(id),
        expected_agent_class TEXT NOT NULL,
        expected_model_class TEXT NOT NULL,
        expected_thinking_tier TEXT NOT NULL DEFAULT 'medium',
        child_session_id    TEXT,
        openclaw_run_id     TEXT,
        actual_provider     TEXT,
        actual_model        TEXT,
        thinking_tier       TEXT,
        status              TEXT NOT NULL CHECK (status IN ('{_DISPATCH_STATUSES}')),
        cycle_no            INTEGER NOT NULL DEFAULT 1,
        position            INTEGER NOT NULL,
        sequence_kind       TEXT NOT NULL CHECK (sequence_kind IN ('{_SEQUENCE_KINDS}')),
        attempt_no          INTEGER NOT NULL DEFAULT 1,
        handoff_id          TEXT,
        result_json         TEXT,
        created_at          TEXT NOT NULL,
        started_at          TEXT,
        consumed_at         TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_dispatches_unique
        ON agent_dispatches (task_id, cycle_no, position, attempt_no)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_dispatches_active
        ON agent_dispatches (task_id)
        WHERE status IN ('PENDING', 'RUNNING', 'RECOVERY_PENDING')
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_dispatches_session
        ON agent_dispatches (child_session_id)
        WHERE child_session_id IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_dispatches_run
        ON agent_dispatches (openclaw_run_id)
        WHERE openclaw_run_id IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_result_quarantine (
        id              TEXT PRIMARY KEY,
        task_id         TEXT,
        dispatch_id     TEXT,
        reason          TEXT NOT NULL,
        event_meta_json TEXT NOT NULL,
        created_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_context_snapshots (
        dispatch_id        TEXT PRIMARY KEY REFERENCES agent_dispatches(id) ON DELETE CASCADE,
        role               TEXT NOT NULL,
        position           INTEGER NOT NULL,
        context_hash       TEXT NOT NULL,
        context_summary_json TEXT NOT NULL,
        created_at         TEXT NOT NULL
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS supervisor_jobs (
        id                    TEXT PRIMARY KEY,
        task_id               TEXT NOT NULL
                              REFERENCES tasks(id) ON DELETE CASCADE,
        status                TEXT NOT NULL CHECK (status IN
                              ('ACTIVE','WAITING_RUN','WAITING_GATE','BACKOFF',
                               'RECOVERING','ERROR','TERMINAL')),
        workflow_state        TEXT NOT NULL,
        expected_role         TEXT,
        expected_dispatch_id  TEXT REFERENCES agent_dispatches(id),
        agent_id              TEXT,
        session_id            TEXT,
        run_id                TEXT,
        attempt_no            INTEGER NOT NULL DEFAULT 0 CHECK (attempt_no >= 0),
        dispatch_status       TEXT,
        result_status         TEXT NOT NULL DEFAULT 'NOT_OBSERVED' CHECK (result_status IN
                              ('NOT_OBSERVED','NOT_FOUND','RUNNING','SUCCEEDED',
                               'FAILED','CANCELLED','UNKNOWN','CONFLICT')),
        result_consumed       INTEGER NOT NULL DEFAULT 0 CHECK (result_consumed IN (0,1)),
        current_handoff_id    TEXT,
        open_findings_count   INTEGER NOT NULL DEFAULT 0 CHECK (open_findings_count >= 0),
        rework_cycle          INTEGER NOT NULL DEFAULT 1 CHECK (rework_cycle >= 1),
        recovery_state        TEXT NOT NULL DEFAULT 'NONE',
        owner_gate_id         TEXT REFERENCES owner_approvals(id),
        gate_status           TEXT,
        gate_scope            TEXT,
        gate_closed           INTEGER NOT NULL DEFAULT 0 CHECK (gate_closed IN (0,1)),
        owner_prompted_at     TEXT,
        owner_prompted_gate_id TEXT,
        next_action           TEXT NOT NULL DEFAULT 'NONE',
        next_wake_at          TEXT,
        retry_count           INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
        missing_confirmations INTEGER NOT NULL DEFAULT 0 CHECK (missing_confirmations >= 0),
        last_error_code       TEXT,
        last_progress_at      TEXT NOT NULL,
        terminal              TEXT CHECK (terminal IS NULL OR terminal IN
                              ('DONE','FAILED','BLOCKED')),
        facts_version         INTEGER NOT NULL DEFAULT 0 CHECK (facts_version >= 0),
        primary_state         TEXT NOT NULL DEFAULT 'QUEUED' CHECK (primary_state IN
                              ('{_PRIMARY_STATES}')),
        queue_reason          TEXT NOT NULL DEFAULT 'NEW',
        priority              INTEGER NOT NULL DEFAULT 0,
        owner_instance_id     TEXT,
        lease_epoch           INTEGER NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
        lease_expires_at      TEXT,
        next_eligible_at      TEXT,
        error_class           TEXT NOT NULL DEFAULT 'NONE',
        wait_kind             TEXT NOT NULL DEFAULT 'NONE',
        created_at            TEXT NOT NULL,
        updated_at            TEXT NOT NULL,
        CHECK ((terminal IS NULL) OR (status = 'TERMINAL' AND next_action = 'NONE'))
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_supervisor_jobs_active_task
        ON supervisor_jobs(task_id) WHERE terminal IS NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS supervisor_actions (
        id                 TEXT PRIMARY KEY,
        supervisor_job_id  TEXT NOT NULL REFERENCES supervisor_jobs(id) ON DELETE CASCADE,
        dispatch_id        TEXT REFERENCES agent_dispatches(id),
        action_type        TEXT NOT NULL CHECK (action_type IN
                           ('START_ROLE','CREATE_DISPATCH','SPAWN_RUN','BIND_RUN',
                            'APPLY_PATCH_SET','RUN_SANDBOX_TESTS','RECORD_TEST_RESULT',
                            'CONSUME_RESULT','MARK_RUN_FAILED','CORE_RECOVER',
                            'PRESENT_OWNER_GATE','CLOSE_JOB')),
        action_key         TEXT NOT NULL UNIQUE,
        args_hash          TEXT NOT NULL,
        input_hash         TEXT,
        precondition_hash  TEXT,
        effect_hash        TEXT,
        patch_set_json     TEXT,
        status             TEXT NOT NULL CHECK (status IN
                           ('PLANNED','RUNNING','SUCCEEDED','FAILED','UNCERTAIN')),
        attempt_count      INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        next_attempt_at    TEXT,
        started_at         TEXT,
        finished_at        TEXT,
        last_error_code    TEXT,
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_supervisor_one_spawn_per_dispatch
        ON supervisor_actions(dispatch_id, action_type)
        WHERE action_type = 'SPAWN_RUN'
    """,
    """
    CREATE TABLE IF NOT EXISTS dispatch_write_intents (
        dispatch_id          TEXT PRIMARY KEY REFERENCES agent_dispatches(id),
        canonical_input_hash TEXT NOT NULL,
        intent_action_id     TEXT NOT NULL,
        created_at           TEXT NOT NULL,
        updated_at           TEXT NOT NULL
    )
    """,
    # V5 (SPEC V3A §4.2): the persistent, outbound-only notification outbox.
    # A projection of already-authoritative events; never a state machine and
    # never an authority for task/dispatch/gate/recovery/terminal.  No secret
    # columns (no credential, target, URL, header, response body or inbound
    # content).  Raw transport responses are never persisted.
    """
    CREATE TABLE IF NOT EXISTS notification_outbox (
        id                    TEXT PRIMARY KEY,
        supervisor_job_id     TEXT NOT NULL
                              REFERENCES supervisor_jobs(id) ON DELETE CASCADE,
        task_id               TEXT NOT NULL
                              REFERENCES tasks(id) ON DELETE CASCADE,
        dispatch_id           TEXT
                              REFERENCES agent_dispatches(id) ON DELETE SET NULL,
        gate_id               TEXT
                              REFERENCES owner_approvals(id) ON DELETE SET NULL,
        notification_type     TEXT NOT NULL CHECK (notification_type IN
                              ('DONE','FAILED','BLOCKED',
                               'OWNER_APPROVAL_REQUIRED')),
        event_ref             TEXT NOT NULL,
        event_version         INTEGER NOT NULL DEFAULT 1 CHECK (event_version >= 1),
        dedup_key             TEXT NOT NULL,
        payload_json          TEXT NOT NULL,
        payload_hash          TEXT NOT NULL CHECK (length(payload_hash) = 64),
        status                TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN
                              ('PENDING','SENDING','SENT','FAILED','DISCARDED')),
        attempt_count         INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        next_attempt_at       TEXT,
        claimed_at            TEXT,
        claim_token           TEXT,
        last_attempt_at       TEXT,
        sent_at               TEXT,
        last_error_code       TEXT,
        created_at            TEXT NOT NULL,
        updated_at            TEXT NOT NULL,
        CHECK ((status = 'SENDING' AND claim_token IS NOT NULL
                AND claimed_at IS NOT NULL)
            OR (status <> 'SENDING' AND claim_token IS NULL)),
        CHECK (status <> 'SENT' OR sent_at IS NOT NULL)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_outbox_dedup
        ON notification_outbox(dedup_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_notification_outbox_due
        ON notification_outbox(status, next_attempt_at, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_notification_outbox_job
        ON notification_outbox(supervisor_job_id, created_at)
    """,
    # V6 (SPEC V3C §8.1): owner-approval challenges + Telegram inbound dedup
    # and offset cursor.  Transport-neutral persistence only: the raw challenge
    # token is NEVER stored (only its sha256 token_hash) and the real
    # chat/sender IDs are NEVER stored (only authorized booleans, §8.2).
    """
    CREATE TABLE IF NOT EXISTS approval_challenges (
        id                       TEXT PRIMARY KEY,
        approval_id              TEXT NOT NULL
                                 REFERENCES owner_approvals(id) ON DELETE CASCADE,
        task_id                  TEXT NOT NULL
                                 REFERENCES tasks(id) ON DELETE CASCADE,
        supervisor_job_id        TEXT NOT NULL
                                 REFERENCES supervisor_jobs(id) ON DELETE CASCADE,
        binding_hash             TEXT NOT NULL
                                 CHECK (length(binding_hash) = 64),
        notification_outbox_id   TEXT UNIQUE
                                 REFERENCES notification_outbox(id)
                                 ON DELETE SET NULL,
        token_hash               TEXT NOT NULL
                                 CHECK (length(token_hash) = 64),
        status                   TEXT NOT NULL CHECK (status IN
                                 ('ISSUED','CONSUMED_APPROVED',
                                  'CONSUMED_REJECTED','EXPIRED','INVALIDATED')),
        created_at               TEXT NOT NULL,
        expires_at               TEXT NOT NULL,
        consumed_at              TEXT,
        consumed_update_id       INTEGER UNIQUE,
        invalidated_at           TEXT,
        CHECK (expires_at > created_at),
        CHECK (
            (status = 'ISSUED'
             AND consumed_at IS NULL
             AND consumed_update_id IS NULL
             AND invalidated_at IS NULL)
            OR
            (status IN ('CONSUMED_APPROVED','CONSUMED_REJECTED')
             AND consumed_at IS NOT NULL
             AND consumed_update_id IS NOT NULL
             AND invalidated_at IS NULL)
            OR
            (status IN ('EXPIRED','INVALIDATED')
             AND consumed_at IS NULL
             AND consumed_update_id IS NULL
             AND invalidated_at IS NOT NULL)
        )
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_challenges_token_hash
        ON approval_challenges(token_hash)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_challenges_active_approval
        ON approval_challenges(approval_id)
        WHERE status = 'ISSUED'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_approval_challenges_due
        ON approval_challenges(status, expires_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_approval_challenges_approval
        ON approval_challenges(approval_id, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS telegram_update_log (
        update_id          INTEGER PRIMARY KEY CHECK (update_id >= 0),
        message_date       INTEGER,
        chat_authorized    INTEGER NOT NULL
                           CHECK (chat_authorized IN (0,1)),
        sender_authorized  INTEGER NOT NULL
                           CHECK (sender_authorized IN (0,1)),
        decision           TEXT CHECK
                           (decision IS NULL OR decision IN ('APPROVE','REJECT')),
        challenge_id       TEXT
                           REFERENCES approval_challenges(id) ON DELETE SET NULL,
        approval_id        TEXT
                           REFERENCES owner_approvals(id) ON DELETE SET NULL,
        outcome            TEXT NOT NULL CHECK (outcome IN (
                           'PROCESSING',
                           'APPROVED',
                           'REJECTED',
                           'WRONG_CHAT',
                           'SPOOFED_SENDER',
                           'MALFORMED',
                           'UNKNOWN_TOKEN',
                           'USED_TOKEN',
                           'EXPIRED_TOKEN',
                           'EXPIRED_APPROVAL',
                           'APPROVAL_NOT_PENDING',
                           'STALE_MESSAGE',
                           'STALE_UPDATE',
                           'BINDING_MISMATCH')),
        received_at        TEXT NOT NULL,
        processed_at       TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_telegram_update_log_outcome
        ON telegram_update_log(outcome, processed_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_telegram_update_log_challenge
        ON telegram_update_log(challenge_id, processed_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS telegram_inbound_state (
        stream_id       TEXT PRIMARY KEY
                        CHECK (stream_id = 'telegram-owner-approval-v1'),
        next_update_id  INTEGER NOT NULL CHECK (next_update_id >= 0),
        updated_at      TEXT NOT NULL
    )
    """,
    """
    INSERT INTO telegram_inbound_state (
        stream_id, next_update_id, updated_at
    )
    SELECT 'telegram-owner-approval-v1', 0, CURRENT_TIMESTAMP
    WHERE NOT EXISTS (
        SELECT 1 FROM telegram_inbound_state
        WHERE stream_id = 'telegram-owner-approval-v1'
    )
    """,
)


def _format_dt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    """Owns a single SQLite connection and all low-level persistence.

    The connection and every mutator are private (SPEC V1.1 11.5, R8); only
    ``Core`` uses them.  Read access is exposed through the public ``get_*`` /
    ``list_*`` methods and the read-only ``Queries`` facade.
    """

    def __init__(self, db_path: str, clock: Optional[Callable[[], datetime]] = None):
        # isolation_level=None -> autocommit; we drive transactions explicitly.
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._clock = clock or utcnow
        self._create_schema()

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def now_iso(self) -> str:
        return _format_dt(self._clock())

    def expiry_iso(self, ttl_seconds: int) -> str:
        return _format_dt(self._clock() + timedelta(seconds=ttl_seconds))

    def _create_schema(self) -> None:
        # V2.3 (G3): schema DDL AND migration share ONE ``BEGIN IMMEDIATE``
        # block, so a migration failure leaves no partial schema behind.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in _SCHEMA:
                self._conn.execute(stmt)
            self._migrate()
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def _migrate(self) -> None:
        """Bring a pre-V3 database forward in place (SPEC V2 3.1 / V2.2 16.8).

        The ``CREATE TABLE IF NOT EXISTS`` statements above only create missing
        tables; an existing ``tasks`` table from a V1/V2 database will not gain
        the new columns automatically.  We detect and add them idempotently.

        V2.3 (G3): this method runs INSIDE ``_create_schema``'s single
        ``BEGIN IMMEDIATE`` transaction (no own BEGIN/COMMIT); after the DDL
        succeeds the ``schema_version`` is UPSERTed to 3.  Any failure rolls the
        whole schema + migration back (no partial schema / stale version).
        """
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(tasks)")}
        if "description" not in cols:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN description TEXT")
        if "risk_class" not in cols:
            self._conn.execute(
                "ALTER TABLE tasks ADD COLUMN risk_class TEXT NOT NULL "
                "DEFAULT 'NORMAL' CHECK (risk_class IN ('LOW','NORMAL','HIGH'))"
            )
        if "external_actions_policy" not in cols:
            self._conn.execute(
                "ALTER TABLE tasks ADD COLUMN external_actions_policy TEXT NOT NULL "
                "DEFAULT 'ALLOWED_WITH_GATE' "
                "CHECK (external_actions_policy IN ('ALLOWED_WITH_GATE','FORBIDDEN'))"
            )
        dcols = {
            r[1] for r in self._conn.execute("PRAGMA table_info(agent_dispatches)")
        }
        if "expected_thinking_tier" not in dcols:
            self._conn.execute(
                "ALTER TABLE agent_dispatches ADD COLUMN "
                "expected_thinking_tier TEXT NOT NULL DEFAULT 'medium'"
            )

        # --- V4: owner-gate closure/binding fields (SPEC V2C §4.3) ---------
        # Additive columns only.  SQLite cannot reliably add a NOT NULL column
        # via ALTER, so the fresh CREATE TABLE carries ``binding_hash TEXT NOT
        # NULL`` and a migrated V3 table is backfilled + guarded by triggers.
        acols = {
            r[1] for r in self._conn.execute("PRAGMA table_info(owner_approvals)")
        }
        for col, ddl in (
            ("binding_hash", "ALTER TABLE owner_approvals ADD COLUMN binding_hash TEXT"),
            ("approved_at", "ALTER TABLE owner_approvals ADD COLUMN approved_at TEXT"),
            ("execution_id", "ALTER TABLE owner_approvals ADD COLUMN execution_id TEXT"),
            ("executed_at", "ALTER TABLE owner_approvals ADD COLUMN executed_at TEXT"),
            ("closed_at", "ALTER TABLE owner_approvals ADD COLUMN closed_at TEXT"),
        ):
            if col not in acols:
                self._conn.execute(ddl)

        # Deterministic closure backfill (§4.3).  A consumed approval without
        # exactly one execution row, or with several, fails the whole migration
        # (no guessed closure).
        arows = self._conn.execute(
            "SELECT id, task_id, action, scope, status, decided_at, "
            "consumed_at, expires_at FROM owner_approvals"
        ).fetchall()
        for r in arows:
            bh = binding_hash(r["task_id"], r["action"], r["scope"])
            status = r["status"]
            approved_at = r["decided_at"] if status in ("approved", "consumed") else None
            closed_at = None
            execution_id = None
            executed_at = None
            if status == "rejected":
                closed_at = r["decided_at"]
            elif status == "expired":
                closed_at = r["expires_at"]
            elif status == "consumed":
                exrows = self._conn.execute(
                    "SELECT id, created_at FROM action_executions "
                    "WHERE approval_id = ? ORDER BY rowid",
                    (r["id"],),
                ).fetchall()
                if len(exrows) != 1:
                    raise sqlite3.IntegrityError(
                        f"V4 migration fail-closed: consumed approval {r['id']!r} "
                        f"has {len(exrows)} execution row(s) (expected exactly 1)"
                    )
                execution_id = exrows[0]["id"]
                executed_at = exrows[0]["created_at"]
                closed_at = r["consumed_at"]
            self._conn.execute(
                "UPDATE owner_approvals SET binding_hash = ?, approved_at = ?, "
                "execution_id = ?, executed_at = ?, closed_at = ? WHERE id = ?",
                (bh, approved_at, execution_id, executed_at, closed_at, r["id"]),
            )

        # Enforce a non-null binding_hash for future rows on a migrated V3 table
        # (the fresh CREATE TABLE already has NOT NULL).
        self._conn.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_owner_approvals_binding_hash_insert "
            "BEFORE INSERT ON owner_approvals "
            "WHEN NEW.binding_hash IS NULL "
            "BEGIN SELECT RAISE(ABORT, 'binding_hash must not be null'); END"
        )
        self._conn.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_owner_approvals_binding_hash_update "
            "BEFORE UPDATE ON owner_approvals "
            "WHEN NEW.binding_hash IS NULL "
            "BEGIN SELECT RAISE(ABORT, 'binding_hash must not be null'); END"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_owner_approvals_execution_id "
            "ON owner_approvals(execution_id) WHERE execution_id IS NOT NULL"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_action_execution_one_per_approval "
            "ON action_executions(approval_id) WHERE approval_id IS NOT NULL"
        )

        # F5 (SPEC V2C §10): gate-scoped owner-prompt memory (additive).
        # Fresh V4 tables carry the column in CREATE TABLE; an already-created
        # supervisor_jobs table (earlier build) gains it idempotently here.
        sjcols = {
            r[1] for r in self._conn.execute("PRAGMA table_info(supervisor_jobs)")
        }
        if "owner_prompted_gate_id" not in sjcols:
            self._conn.execute(
                "ALTER TABLE supervisor_jobs ADD COLUMN "
                "owner_prompted_gate_id TEXT"
            )

        # R7-F1 (SPEC V2C §17): persist the canonical patch-set JSON on each
        # APPLY intent so a crash before journal-success can re-apply the
        # SAME patch set exactly-once (never re-derived from a later, changed
        # observation).  Additive column only.
        sact_cols = {
            r[1] for r in self._conn.execute("PRAGMA table_info(supervisor_actions)")
        }
        if "patch_set_json" not in sact_cols:
            self._conn.execute(
                "ALTER TABLE supervisor_actions ADD COLUMN patch_set_json TEXT"
            )

        # --- B1 (Phase B): durable queue + job lease (additive) ------------
        # Lease fields live DIRECTLY on the job (§6/§19): no ``job_leases``
        # table.  ``primary_state`` is the 8-state operational projection; the
        # V2C ``status`` column is kept as a backwards-compatible projection.
        # Additive columns only; fresh CREATE TABLE already carries them.
        sjcols = {
            r[1] for r in self._conn.execute("PRAGMA table_info(supervisor_jobs)")
        }
        _sj_b1_add = (
            ("primary_state",
             f"ALTER TABLE supervisor_jobs ADD COLUMN primary_state TEXT NOT NULL "
             f"DEFAULT 'QUEUED' CHECK (primary_state IN ('{_PRIMARY_STATES}'))"),
            ("queue_reason",
             "ALTER TABLE supervisor_jobs ADD COLUMN queue_reason TEXT NOT NULL "
             "DEFAULT 'NEW'"),
            ("priority",
             "ALTER TABLE supervisor_jobs ADD COLUMN priority INTEGER NOT NULL "
             "DEFAULT 0"),
            ("owner_instance_id",
             "ALTER TABLE supervisor_jobs ADD COLUMN owner_instance_id TEXT"),
            ("lease_epoch",
             "ALTER TABLE supervisor_jobs ADD COLUMN lease_epoch INTEGER NOT NULL "
             "DEFAULT 0 CHECK (lease_epoch >= 0)"),
            ("lease_expires_at",
             "ALTER TABLE supervisor_jobs ADD COLUMN lease_expires_at TEXT"),
            ("next_eligible_at",
             "ALTER TABLE supervisor_jobs ADD COLUMN next_eligible_at TEXT"),
            ("error_class",
             "ALTER TABLE supervisor_jobs ADD COLUMN error_class TEXT NOT NULL "
             "DEFAULT 'NONE'"),
            ("wait_kind",
             "ALTER TABLE supervisor_jobs ADD COLUMN wait_kind TEXT NOT NULL "
             "DEFAULT 'NONE'"),
        )
        b1_added = False
        for col, ddl in _sj_b1_add:
            if col not in sjcols:
                self._conn.execute(ddl)
                b1_added = True

        # Backfill ``primary_state`` for pre-existing rows from the projection
        # fields (terminal is the sticky-terminal authority).  This runs ONLY
        # during the actual B1 migration (``b1_added``), never on every open, so
        # it cannot clobber the runtime primary_state that claim/release/enqueue
        # set (idempotent-reopen safety).
        # F3: a legacy ``ACTIVE`` row (pre-B1) has NO lease, so it must NOT be
        # projected to ``RUNNING`` (which would be a lease-less RUNNING that is
        # un-claimable under the fail-closed takeover rule).  Bootstrap it as
        # ``QUEUED``/``WAITING_RUN`` so it re-enters the queue and is claimed
        # normally.
        if b1_added:
            sjrows = self._conn.execute(
                "SELECT id, status, terminal, recovery_state, wait_kind, "
                "queue_reason FROM supervisor_jobs"
            ).fetchall()
            for r in sjrows:
                if r["status"] == "ACTIVE" and r["terminal"] is None:
                    self._conn.execute(
                        "UPDATE supervisor_jobs SET primary_state = ?, "
                        "status = ? WHERE id = ?",
                        (job_state.PrimaryState.QUEUED.value, "WAITING_RUN", r["id"]),
                    )
                else:
                    ps = job_state.derive_primary_state(
                        r["status"],
                        terminal=r["terminal"],
                        recovery_state=r["recovery_state"],
                        wait_kind=r["wait_kind"],
                        queue_reason=r["queue_reason"],
                    ).value
                    self._conn.execute(
                        "UPDATE supervisor_jobs SET primary_state = ? WHERE id = ?",
                        (ps, r["id"]),
                    )

        # UPSERT the schema version after successful DDL + migration.
        self._conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SCHEMA_VERSION,),
        )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Run a block inside a single ``BEGIN IMMEDIATE`` transaction."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def open_inbound_connection(self) -> sqlite3.Connection:
        """Open a dedicated inbound connection with ``busy_timeout=0``
        (SPEC V3C §14: immediate termination on DB lock).

        Used by the approval callback processor so a concurrent writer lock
        aborts ``BEGIN IMMEDIATE`` immediately instead of blocking the
        supervisor.  The main ``self._conn`` is left untouched."""
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 0")
        return conn

    def _bound(self, conn: sqlite3.Connection) -> "Store":
        """Return a connection-scoped :class:`Store` view bound to ``conn``.

        Shares this Store's clock; every reader/mutator runs on ``conn`` while
        ``self._conn`` is left completely untouched.  Used by inbound callback
        processing so the supervisor's main connection stays usable
        concurrently (SPEC V3C §14).  The returned view is ephemeral: the
        caller owns ``conn`` and must close it; the view itself must never be
        closed.
        """
        view = Store.__new__(Store)
        view._db_path = self._db_path
        view._conn = conn
        view._clock = self._clock
        return view

    @contextmanager
    def _inbound_transaction(self) -> Iterator["Store"]:
        """Run a block inside a dedicated ``BEGIN IMMEDIATE`` transaction with
        ``busy_timeout=0`` (immediate termination on DB lock, SPEC V3C §14).

        Yields a connection-scoped :class:`Store` bound to the dedicated
        inbound connection so every store mutator AND the approval decision
        bridge participate in the SAME transaction.  ``self._conn`` is NEVER
        swapped or touched: the main supervisor connection remains open and
        usable concurrently throughout and after the inbound pass."""
        inbound = self.open_inbound_connection()
        bound = self._bound(inbound)
        try:
            inbound.execute("BEGIN IMMEDIATE")
            try:
                yield bound
            except BaseException:
                inbound.execute("ROLLBACK")
                raise
            else:
                inbound.execute("COMMIT")
        finally:
            inbound.close()

    # -- projects ------------------------------------------------------------

    def _insert_project(self, p: Project) -> None:
        self._conn.execute(
            "INSERT INTO projects (id, name, created_at, idempotency_key) "
            "VALUES (?, ?, ?, ?)",
            (p.id, p.name, p.created_at, p.idempotency_key),
        )

    def get_project(self, project_id: str) -> Optional[Project]:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            return None
        return Project(
            id=row["id"],
            name=row["name"],
            created_at=row["created_at"],
            idempotency_key=row["idempotency_key"],
        )

    # -- tasks ---------------------------------------------------------------

    def _insert_task(self, t: Task) -> None:
        self._conn.execute(
            "INSERT INTO tasks (id, project_id, title, description, state, "
            "resume_state, source, source_class, risk_class, "
            "external_actions_policy, created_at, updated_at, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                t.id,
                t.project_id,
                t.title,
                t.description,
                t.state.value,
                t.resume_state.value if t.resume_state else None,
                t.source,
                t.source_class.value,
                t.risk_class.value,
                t.external_actions_policy.value,
                t.created_at,
                t.updated_at,
                t.idempotency_key,
            ),
        )

    def get_task(self, task_id: str) -> Optional[Task]:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_task(row)

    def _update_task_state(
        self,
        task_id: str,
        state: TaskState,
        resume_state: Optional[TaskState],
        updated_at: str,
    ) -> None:
        self._conn.execute(
            "UPDATE tasks SET state = ?, resume_state = ?, updated_at = ? "
            "WHERE id = ?",
            (
                state.value,
                resume_state.value if resume_state else None,
                updated_at,
                task_id,
            ),
        )

    def list_tasks(self) -> list[Task]:
        rows = self._conn.execute("SELECT * FROM tasks").fetchall()
        return [self._row_to_task(r) for r in rows]

    def list_tasks_for_recovery(self) -> list[tuple[Task, bool]]:
        """Defensively load all tasks for recovery (SPEC V1.2 12.4).

        Returns ``(task, valid)`` pairs.  ``valid=False`` means the stored
        ``resume_state`` was an unknown value that could not be converted to a
        :class:`TaskState`; the returned task carries ``resume_state=None`` and
        the caller must handle it defensively (move the task to ``BLOCKED``).

        A corrupt ``resume_state`` (which the DB CHECK constraint now prevents
        going forward, but may pre-date it) must never crash ``recover()``.
        """
        rows = self._conn.execute("SELECT * FROM tasks").fetchall()
        out: list[tuple[Task, bool]] = []
        for row in rows:
            resume_state: Optional[TaskState] = None
            valid = True
            if row["resume_state"] is not None:
                try:
                    resume_state = TaskState(row["resume_state"])
                except (ValueError, KeyError):
                    valid = False
                    resume_state = None
            task = Task(
                id=row["id"],
                project_id=row["project_id"],
                title=row["title"],
                state=TaskState(row["state"]),
                resume_state=resume_state,
                source=row["source"],
                source_class=SourceClass(row["source_class"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                idempotency_key=row["idempotency_key"],
                description=row["description"] if "description" in row.keys() else None,
                risk_class=RiskClass(row["risk_class"])
                if "risk_class" in row.keys() and row["risk_class"]
                else RiskClass.NORMAL,
                external_actions_policy=ExternalActionsPolicy(
                    row["external_actions_policy"]
                )
                if "external_actions_policy" in row.keys()
                and row["external_actions_policy"]
                else ExternalActionsPolicy.ALLOWED_WITH_GATE,
            )
            out.append((task, valid))
        return out

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        keys = row.keys()
        return Task(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            state=TaskState(row["state"]),
            resume_state=TaskState(row["resume_state"]) if row["resume_state"] else None,
            source=row["source"],
            source_class=SourceClass(row["source_class"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            idempotency_key=row["idempotency_key"],
            description=row["description"] if "description" in keys else None,
            risk_class=RiskClass(row["risk_class"])
            if "risk_class" in keys and row["risk_class"]
            else RiskClass.NORMAL,
            external_actions_policy=ExternalActionsPolicy(
                row["external_actions_policy"]
            )
            if "external_actions_policy" in keys and row["external_actions_policy"]
            else ExternalActionsPolicy.ALLOWED_WITH_GATE,
        )

    # -- task_runs -----------------------------------------------------------

    def _insert_task_run(self, tr: TaskRun) -> None:
        self._conn.execute(
            "INSERT INTO task_runs (id, task_id, status, started_at, finished_at, "
            "idempotency_key) VALUES (?, ?, ?, ?, ?, ?)",
            (
                tr.id,
                tr.task_id,
                tr.status.value,
                tr.started_at,
                tr.finished_at,
                None,
            ),
        )

    def get_task_run(self, run_id: str) -> Optional[TaskRun]:
        row = self._conn.execute(
            "SELECT * FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return TaskRun(
            id=row["id"],
            task_id=row["task_id"],
            status=TaskRunStatus(row["status"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    def _update_task_run_status(
        self, run_id: str, status: TaskRunStatus, finished_at: str
    ) -> None:
        self._conn.execute(
            "UPDATE task_runs SET status = ?, finished_at = ? WHERE id = ?",
            (status.value, finished_at, run_id),
        )

    def list_task_runs(
        self, task_id: Optional[str] = None, status: Optional[TaskRunStatus] = None
    ) -> list[TaskRun]:
        q = "SELECT * FROM task_runs"
        clauses, params = [], []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        rows = self._conn.execute(q, params).fetchall()
        return [
            TaskRun(
                id=r["id"],
                task_id=r["task_id"],
                status=TaskRunStatus(r["status"]),
                started_at=r["started_at"],
                finished_at=r["finished_at"],
            )
            for r in rows
        ]

    # -- role_runs -----------------------------------------------------------

    def _insert_role_run(self, rr: RoleRun) -> None:
        self._conn.execute(
            "INSERT INTO role_runs (id, task_id, role, status, started_at, "
            "finished_at, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                rr.id,
                rr.task_id,
                rr.role.value,
                rr.status.value,
                rr.started_at,
                rr.finished_at,
                None,
            ),
        )

    def get_role_run(self, run_id: str) -> Optional[RoleRun]:
        row = self._conn.execute(
            "SELECT * FROM role_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return RoleRun(
            id=row["id"],
            task_id=row["task_id"],
            role=Role(row["role"]),
            status=RoleRunStatus(row["status"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    def _update_role_run_status(
        self, run_id: str, status: RoleRunStatus, finished_at: str
    ) -> None:
        self._conn.execute(
            "UPDATE role_runs SET status = ?, finished_at = ? WHERE id = ?",
            (status.value, finished_at, run_id),
        )

    def get_active_role_run(self, task_id: str) -> Optional[RoleRun]:
        row = self._conn.execute(
            "SELECT * FROM role_runs WHERE task_id = ? AND status = 'started' "
            "LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return RoleRun(
            id=row["id"],
            task_id=row["task_id"],
            role=Role(row["role"]),
            status=RoleRunStatus(row["status"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    def list_role_runs(
        self, task_id: Optional[str] = None, status: Optional[RoleRunStatus] = None
    ) -> list[RoleRun]:
        q = "SELECT * FROM role_runs"
        clauses, params = [], []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        rows = self._conn.execute(q, params).fetchall()
        return [
            RoleRun(
                id=r["id"],
                task_id=r["task_id"],
                role=Role(r["role"]),
                status=RoleRunStatus(r["status"]),
                started_at=r["started_at"],
                finished_at=r["finished_at"],
            )
            for r in rows
        ]

    # -- handoffs ------------------------------------------------------------

    def _insert_handoff(self, h: Handoff) -> None:
        self._conn.execute(
            "INSERT INTO handoffs (id, task_id, from_role, to_role, created_at, "
            "idempotency_key) VALUES (?, ?, ?, ?, ?, ?)",
            (
                h.id,
                h.task_id,
                h.from_role.value,
                h.to_role.value,
                h.created_at,
                None,
            ),
        )

    def get_latest_handoff(self, task_id: str) -> Optional[Handoff]:
        row = self._conn.execute(
            "SELECT * FROM handoffs WHERE task_id = ? ORDER BY rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return Handoff(
            id=row["id"],
            task_id=row["task_id"],
            from_role=Role(row["from_role"]),
            to_role=Role(row["to_role"]),
            created_at=row["created_at"],
        )

    def list_handoffs(self, task_id: Optional[str] = None) -> list[Handoff]:
        q = "SELECT * FROM handoffs"
        params: list = []
        if task_id is not None:
            q += " WHERE task_id = ?"
            params.append(task_id)
        q += " ORDER BY rowid"
        rows = self._conn.execute(q, params).fetchall()
        return [
            Handoff(
                id=r["id"],
                task_id=r["task_id"],
                from_role=Role(r["from_role"]),
                to_role=Role(r["to_role"]),
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # -- findings ------------------------------------------------------------

    def _insert_finding(self, f: Finding) -> None:
        self._conn.execute(
            "INSERT INTO findings (id, task_id, severity, description, status, "
            "created_at, resolved_at, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f.id,
                f.task_id,
                f.severity,
                f.description,
                f.status.value,
                f.created_at,
                f.resolved_at,
                None,
            ),
        )

    def get_finding(self, finding_id: str) -> Optional[Finding]:
        row = self._conn.execute(
            "SELECT * FROM findings WHERE id = ?", (finding_id,)
        ).fetchone()
        if row is None:
            return None
        return Finding(
            id=row["id"],
            task_id=row["task_id"],
            severity=row["severity"],
            description=row["description"],
            status=FindingStatus(row["status"]),
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
        )

    def _update_finding_status(
        self, finding_id: str, status: FindingStatus, resolved_at: str
    ) -> None:
        self._conn.execute(
            "UPDATE findings SET status = ?, resolved_at = ? WHERE id = ?",
            (status.value, resolved_at, finding_id),
        )

    def list_findings(self, task_id: Optional[str] = None) -> list[Finding]:
        q = "SELECT * FROM findings"
        params: list = []
        if task_id is not None:
            q += " WHERE task_id = ?"
            params.append(task_id)
        rows = self._conn.execute(q, params).fetchall()
        return [
            Finding(
                id=r["id"],
                task_id=r["task_id"],
                severity=r["severity"],
                description=r["description"],
                status=FindingStatus(r["status"]),
                created_at=r["created_at"],
                resolved_at=r["resolved_at"],
            )
            for r in rows
        ]

    # -- test_runs -----------------------------------------------------------

    def _insert_test_run(self, tr: TestRun) -> None:
        self._conn.execute(
            "INSERT INTO test_runs (id, task_id, result, detail, created_at, "
            "idempotency_key) VALUES (?, ?, ?, ?, ?, ?)",
            (tr.id, tr.task_id, tr.result.value, tr.detail, tr.created_at, None),
        )

    def get_test_run(self, run_id: str) -> Optional[TestRun]:
        row = self._conn.execute(
            "SELECT * FROM test_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return TestRun(
            id=row["id"],
            task_id=row["task_id"],
            result=TestResult(row["result"]),
            detail=row["detail"],
            created_at=row["created_at"],
        )

    def list_test_runs(self, task_id: Optional[str] = None) -> list[TestRun]:
        q = "SELECT * FROM test_runs"
        params: list = []
        if task_id is not None:
            q += " WHERE task_id = ?"
            params.append(task_id)
        rows = self._conn.execute(q, params).fetchall()
        return [
            TestRun(
                id=r["id"],
                task_id=r["task_id"],
                result=TestResult(r["result"]),
                detail=r["detail"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # -- reviews -------------------------------------------------------------

    def _insert_review(self, rev) -> None:
        self._conn.execute(
            "INSERT INTO reviews (id, task_id, verdict, detail, created_at, "
            "idempotency_key) VALUES (?, ?, ?, ?, ?, ?)",
            (rev.id, rev.task_id, rev.verdict, rev.detail, rev.created_at, None),
        )

    def get_review(self, review_id: str):
        row = self._conn.execute(
            "SELECT * FROM reviews WHERE id = ?", (review_id,)
        ).fetchone()
        if row is None:
            return None
        from .models import Review

        return Review(
            id=row["id"],
            task_id=row["task_id"],
            verdict=row["verdict"],
            detail=row["detail"],
            created_at=row["created_at"],
        )

    # -- decisions -----------------------------------------------------------

    def _insert_decision(self, d: Decision) -> None:
        self._conn.execute(
            "INSERT INTO decisions (id, task_id, decision, detail, created_at, "
            "idempotency_key) VALUES (?, ?, ?, ?, ?, ?)",
            (d.id, d.task_id, d.decision, d.detail, d.created_at, None),
        )

    def get_decision(self, decision_id: str) -> Optional[Decision]:
        row = self._conn.execute(
            "SELECT * FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        if row is None:
            return None
        return Decision(
            id=row["id"],
            task_id=row["task_id"],
            decision=row["decision"],
            detail=row["detail"],
            created_at=row["created_at"],
        )

    def list_decisions(self, task_id: Optional[str] = None) -> list[Decision]:
        q = "SELECT * FROM decisions"
        params: list = []
        if task_id is not None:
            q += " WHERE task_id = ?"
            params.append(task_id)
        q += " ORDER BY rowid"
        rows = self._conn.execute(q, params).fetchall()
        return [
            Decision(
                id=r["id"],
                task_id=r["task_id"],
                decision=r["decision"],
                detail=r["detail"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def list_reviews(self, task_id: Optional[str] = None):
        q = "SELECT * FROM reviews"
        params: list = []
        if task_id is not None:
            q += " WHERE task_id = ?"
            params.append(task_id)
        q += " ORDER BY rowid"
        rows = self._conn.execute(q, params).fetchall()
        from .models import Review

        return [
            Review(
                id=r["id"],
                task_id=r["task_id"],
                verdict=r["verdict"],
                detail=r["detail"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # -- owner_approvals -----------------------------------------------------

    def _insert_approval(self, a: OwnerApproval) -> None:
        bh = binding_hash(a.task_id, a.action, a.scope)
        self._conn.execute(
            "INSERT INTO owner_approvals (id, task_id, action, scope, status, "
            "requested_by, source_class, created_at, decided_at, consumed_at, "
            "expires_at, idempotency_key, binding_hash, approved_at, "
            "execution_id, executed_at, closed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                a.id,
                a.task_id,
                a.action,
                a.scope,
                a.status.value,
                a.requested_by,
                a.source_class.value,
                a.created_at,
                a.decided_at,
                a.consumed_at,
                a.expires_at,
                None,
                bh,
                a.approved_at,
                a.execution_id,
                a.executed_at,
                a.closed_at,
            ),
        )

    def get_approval(self, approval_id: str) -> Optional[OwnerApproval]:
        row = self._conn.execute(
            "SELECT * FROM owner_approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_approval(row)

    def list_approvals(self, task_id: Optional[str] = None) -> list[OwnerApproval]:
        q = "SELECT * FROM owner_approvals"
        params: list = []
        if task_id is not None:
            q += " WHERE task_id = ?"
            params.append(task_id)
        rows = self._conn.execute(q, params).fetchall()
        return [self._row_to_approval(r) for r in rows]

    @staticmethod
    def _row_to_approval(row: sqlite3.Row) -> OwnerApproval:
        keys = row.keys()
        return OwnerApproval(
            id=row["id"],
            task_id=row["task_id"],
            action=row["action"],
            scope=row["scope"],
            status=ApprovalStatus(row["status"]),
            requested_by=row["requested_by"],
            source_class=SourceClass(row["source_class"]),
            created_at=row["created_at"],
            decided_at=row["decided_at"],
            consumed_at=row["consumed_at"],
            expires_at=row["expires_at"],
            binding_hash=row["binding_hash"] if "binding_hash" in keys else None,
            approved_at=row["approved_at"] if "approved_at" in keys else None,
            execution_id=row["execution_id"] if "execution_id" in keys else None,
            executed_at=row["executed_at"] if "executed_at" in keys else None,
            closed_at=row["closed_at"] if "closed_at" in keys else None,
        )

    # Atomic approval transitions (return rowcount for single-use enforcement).

    def _mark_approved(self, approval_id: str, now_iso: str) -> int:
        cur = self._conn.execute(
            "UPDATE owner_approvals SET status = 'approved', decided_at = ?, "
            "approved_at = ? "
            "WHERE id = ? AND status = 'pending' AND expires_at > ?",
            (now_iso, now_iso, approval_id, now_iso),
        )
        return cur.rowcount

    def _mark_rejected(self, approval_id: str, now_iso: str) -> int:
        cur = self._conn.execute(
            "UPDATE owner_approvals SET status = 'rejected', decided_at = ?, "
            "closed_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (now_iso, now_iso, approval_id),
        )
        return cur.rowcount

    def _consume_approval(self, approval_id: str, now_iso: str) -> int:
        # Only 'approved' AND unexpired may be consumed (R2, R10).  A 'pending'
        # approval is never consumed by execute_approved.
        cur = self._conn.execute(
            "UPDATE owner_approvals SET status = 'consumed', consumed_at = ? "
            "WHERE id = ? AND status = 'approved' AND expires_at > ?",
            (now_iso, approval_id, now_iso),
        )
        return cur.rowcount

    def _consume_approval_with_execution(
        self, approval_id: str, now_iso: str, execution_id: str
    ) -> int:
        """Consume an approval AND bind its execution row atomically (V4)."""
        cur = self._conn.execute(
            "UPDATE owner_approvals SET status = 'consumed', consumed_at = ?, "
            "execution_id = ?, executed_at = ?, closed_at = ? "
            "WHERE id = ? AND status = 'approved' AND expires_at > ?",
            (now_iso, execution_id, now_iso, now_iso, approval_id, now_iso),
        )
        return cur.rowcount

    def _mark_expired(self, approval_id: str, now_iso: str) -> int:
        # Covers both 'pending' and 'approved' (SPEC V1.2 12.3): an expired
        # 'approved' approval must also transition to 'expired' so the unique
        # index no longer blocks a fresh request.  V4: closed_at = expires_at.
        cur = self._conn.execute(
            "UPDATE owner_approvals SET status = 'expired', closed_at = expires_at "
            "WHERE id = ? AND status IN ('pending', 'approved') "
            "AND expires_at <= ?",
            (approval_id, now_iso),
        )
        return cur.rowcount

    # -- action_executions ---------------------------------------------------

    def _insert_action_execution(self, ex: ActionExecution) -> None:
        self._conn.execute(
            "INSERT INTO action_executions (id, task_id, approval_id, action, "
            "scope, actor_role, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ex.id,
                ex.task_id,
                ex.approval_id,
                ex.action,
                ex.scope,
                ex.actor_role,
                ex.status.value,
                ex.created_at,
            ),
        )

    def get_action_execution(self, execution_id: str) -> Optional[ActionExecution]:
        row = self._conn.execute(
            "SELECT * FROM action_executions WHERE id = ?", (execution_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_execution(row)

    def list_action_executions(self, task_id: Optional[str] = None) -> list[ActionExecution]:
        q = "SELECT * FROM action_executions"
        params: list = []
        if task_id is not None:
            q += " WHERE task_id = ?"
            params.append(task_id)
        q += " ORDER BY rowid"
        rows = self._conn.execute(q, params).fetchall()
        return [self._row_to_execution(r) for r in rows]

    @staticmethod
    def _row_to_execution(row: sqlite3.Row) -> ActionExecution:
        return ActionExecution(
            id=row["id"],
            task_id=row["task_id"],
            approval_id=row["approval_id"],
            action=row["action"],
            scope=row["scope"],
            actor_role=row["actor_role"],
            status=ActionExecutionStatus(row["status"]),
            created_at=row["created_at"],
        )

    # -- events --------------------------------------------------------------

    def _insert_event(self, ev: Event) -> bool:
        """Insert an event idempotently.  Returns True if a row was inserted.

        The whole event (envelope fields + payload) is privacy-filtered first
        (fail-closed): a deny-listed substring in any string field raises
        :class:`PrivacyViolation` and nothing is written (SPEC V1.2 12.5).
        """
        events_mod.check_event(ev)
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO events (id, type, task_id, role, state, "
            "payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ev.id,
                ev.type,
                ev.task_id,
                ev.role,
                ev.state,
                json.dumps(ev.payload, sort_keys=True),
                ev.created_at,
            ),
        )
        return cur.rowcount == 1

    def list_events(self, task_id: Optional[str] = None) -> list[Event]:
        q = "SELECT * FROM events"
        params: list = []
        if task_id is not None:
            q += " WHERE task_id = ?"
            params.append(task_id)
        q += " ORDER BY created_at, id"
        rows = self._conn.execute(q, params).fetchall()
        out = []
        for r in rows:
            out.append(
                Event(
                    id=r["id"],
                    type=r["type"],
                    task_id=r["task_id"],
                    role=r["role"],
                    state=r["state"],
                    payload=json.loads(r["payload_json"]),
                    created_at=r["created_at"],
                )
            )
        return out

    # -- command idempotency -------------------------------------------------

    def get_command_idempotency(
        self, key: str, command: str
    ) -> Optional[tuple[str, str]]:
        """Return ``(result_id, args_hash)`` for an existing replay, else None."""
        row = self._conn.execute(
            "SELECT result_id, args_hash FROM command_idempotency "
            "WHERE key = ? AND command = ?",
            (key, command),
        ).fetchone()
        if row is None:
            return None
        return (row["result_id"], row["args_hash"])

    def _set_command_idempotency(
        self, key: str, command: str, result_id: str, args_hash: str, now: str
    ) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO command_idempotency (key, command, result_id, "
            "args_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (key, command, result_id, args_hash, now),
        )

    # -- agent dispatches ----------------------------------------------------

    def _insert_dispatch(self, d: AgentDispatch) -> None:
        self._conn.execute(
            "INSERT INTO agent_dispatches (id, task_id, task_run_id, role, "
            "parent_dispatch_id, expected_agent_class, expected_model_class, "
            "expected_thinking_tier, child_session_id, openclaw_run_id, "
            "actual_provider, actual_model, thinking_tier, status, cycle_no, "
            "position, sequence_kind, attempt_no, handoff_id, result_json, "
            "created_at, started_at, consumed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?)",
            (
                d.id,
                d.task_id,
                d.task_run_id,
                d.role.value,
                d.parent_dispatch_id,
                d.expected_agent_class,
                d.expected_model_class,
                d.expected_thinking_tier,
                d.child_session_id,
                d.openclaw_run_id,
                d.actual_provider,
                d.actual_model,
                d.thinking_tier,
                d.status.value,
                d.cycle_no,
                d.position,
                d.sequence_kind.value,
                d.attempt_no,
                d.handoff_id,
                d.result_json,
                d.created_at,
                d.started_at,
                d.consumed_at,
            ),
        )

    def get_dispatch(self, dispatch_id: str) -> Optional[AgentDispatch]:
        row = self._conn.execute(
            "SELECT * FROM agent_dispatches WHERE id = ?", (dispatch_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dispatch(row)

    @staticmethod
    def _row_to_dispatch(row: sqlite3.Row) -> AgentDispatch:
        return AgentDispatch(
            id=row["id"],
            task_id=row["task_id"],
            task_run_id=row["task_run_id"],
            role=Role(row["role"]),
            parent_dispatch_id=row["parent_dispatch_id"],
            expected_agent_class=row["expected_agent_class"],
            expected_model_class=row["expected_model_class"],
            expected_thinking_tier=row["expected_thinking_tier"],
            child_session_id=row["child_session_id"],
            openclaw_run_id=row["openclaw_run_id"],
            actual_provider=row["actual_provider"],
            actual_model=row["actual_model"],
            thinking_tier=row["thinking_tier"],
            status=DispatchStatus(row["status"]),
            cycle_no=row["cycle_no"],
            position=row["position"],
            sequence_kind=SequenceKind(row["sequence_kind"]),
            attempt_no=row["attempt_no"],
            handoff_id=row["handoff_id"],
            result_json=row["result_json"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            consumed_at=row["consumed_at"],
        )

    def list_dispatches(
        self,
        task_id: Optional[str] = None,
        status: Optional[DispatchStatus] = None,
    ) -> list[AgentDispatch]:
        q = "SELECT * FROM agent_dispatches"
        clauses: list[str] = []
        params: list = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY cycle_no, position, attempt_no"
        rows = self._conn.execute(q, params).fetchall()
        return [self._row_to_dispatch(r) for r in rows]

    def _update_dispatch_bind(
        self,
        dispatch_id: str,
        child_session_id: str,
        openclaw_run_id: str,
        actual_provider: str,
        actual_model: str,
        thinking_tier: str,
        started_at: str,
    ) -> int:
        cur = self._conn.execute(
            "UPDATE agent_dispatches SET status = 'RUNNING', child_session_id = ?, "
            "openclaw_run_id = ?, actual_provider = ?, actual_model = ?, "
            "thinking_tier = ?, started_at = ? "
            "WHERE id = ? AND status IN ('PENDING', 'RECOVERY_PENDING')",
            (
                child_session_id,
                openclaw_run_id,
                actual_provider,
                actual_model,
                thinking_tier,
                started_at,
                dispatch_id,
            ),
        )
        return cur.rowcount

    def _reject_dispatch_cas(self, dispatch_id: str) -> int:
        """Atomically reject a dispatch that is still PENDING/RECOVERY_PENDING.

        V2.3 (G1): the mismatch path must never overwrite a dispatch that a
        parallel valid bind already moved to RUNNING.  The ``WHERE status IN
        ('PENDING','RECOVERY_PENDING')`` guard makes this a compare-and-set;
        ``rowcount == 1`` means this call performed the rejection.
        """
        cur = self._conn.execute(
            "UPDATE agent_dispatches SET status = 'REJECTED' "
            "WHERE id = ? AND status IN ('PENDING', 'RECOVERY_PENDING')",
            (dispatch_id,),
        )
        return cur.rowcount

    def _update_dispatch_status(
        self, dispatch_id: str, status: DispatchStatus, now: str
    ) -> int:
        cur = self._conn.execute(
            "UPDATE agent_dispatches SET status = ? WHERE id = ?",
            (status.value, dispatch_id),
        )
        return cur.rowcount

    def _consume_dispatch(
        self,
        dispatch_id: str,
        result_json: str,
        consumed_at: str,
        allowed: tuple[DispatchStatus, ...] = (
            DispatchStatus.RUNNING,
            DispatchStatus.RECOVERY_PENDING,
        ),
    ) -> int:
        allowed_vals = ", ".join(f"'{s.value}'" for s in allowed)
        cur = self._conn.execute(
            f"UPDATE agent_dispatches SET status = 'CONSUMED', result_json = ?, "
            f"consumed_at = ? WHERE id = ? AND status IN ({allowed_vals})",
            (result_json, consumed_at, dispatch_id),
        )
        return cur.rowcount

    def get_latest_decision(self, task_id: str) -> Optional[Decision]:
        row = self._conn.execute(
            "SELECT * FROM decisions WHERE task_id = ? ORDER BY rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return Decision(
            id=row["id"],
            task_id=row["task_id"],
            decision=row["decision"],
            detail=row["detail"],
            created_at=row["created_at"],
        )

    def get_latest_task_run(self, task_id: str) -> Optional[TaskRun]:
        row = self._conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return TaskRun(
            id=row["id"],
            task_id=row["task_id"],
            status=TaskRunStatus(row["status"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    # -- quarantine log ------------------------------------------------------

    def _insert_quarantine(self, q: AgentResultQuarantine) -> None:
        self._conn.execute(
            "INSERT INTO agent_result_quarantine (id, task_id, dispatch_id, "
            "reason, event_meta_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (q.id, q.task_id, q.dispatch_id, q.reason, q.event_meta_json, q.created_at),
        )

    def list_quarantine(
        self, task_id: Optional[str] = None
    ) -> list[AgentResultQuarantine]:
        q = "SELECT * FROM agent_result_quarantine"
        params: list = []
        if task_id is not None:
            q += " WHERE task_id = ?"
            params.append(task_id)
        q += " ORDER BY rowid"
        rows = self._conn.execute(q, params).fetchall()
        return [
            AgentResultQuarantine(
                id=r["id"],
                task_id=r["task_id"],
                dispatch_id=r["dispatch_id"],
                reason=r["reason"],
                event_meta_json=r["event_meta_json"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # -- context snapshots ----------------------------------------------------

    def _insert_context_snapshot(self, s: AgentContextSnapshot) -> None:
        # V2.2 (F5): plain INSERT (no REPLACE) — context snapshots are immutable.
        self._conn.execute(
            "INSERT INTO agent_context_snapshots (dispatch_id, role, "
            "position, context_hash, context_summary_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                s.dispatch_id,
                s.role.value,
                s.position,
                s.context_hash,
                s.context_summary_json,
                s.created_at,
            ),
        )

    def get_context_snapshot(
        self, dispatch_id: str
    ) -> Optional[AgentContextSnapshot]:
        row = self._conn.execute(
            "SELECT * FROM agent_context_snapshots WHERE dispatch_id = ?",
            (dispatch_id,),
        ).fetchone()
        if row is None:
            return None
        return AgentContextSnapshot(
            dispatch_id=row["dispatch_id"],
            role=Role(row["role"]),
            position=row["position"],
            context_hash=row["context_hash"],
            context_summary_json=row["context_summary_json"],
            created_at=row["created_at"],
        )

    def list_context_snapshots(
        self, task_id: Optional[str] = None
    ) -> list[AgentContextSnapshot]:
        q = "SELECT s.* FROM agent_context_snapshots s"
        params: list = []
        if task_id is not None:
            q += " JOIN agent_dispatches d ON d.id = s.dispatch_id WHERE d.task_id = ?"
            params.append(task_id)
        q += " ORDER BY s.dispatch_id"
        rows = self._conn.execute(q, params).fetchall()
        return [
            AgentContextSnapshot(
                dispatch_id=r["dispatch_id"],
                role=Role(r["role"]),
                position=r["position"],
                context_hash=r["context_hash"],
                context_summary_json=r["context_summary_json"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # -- supervisor jobs (V4, SPEC V2C §4.1) --------------------------------
    # The supervisor subsystem lives in ``argent_core/supervisor.py``; these
    # methods are the only persistence surface it uses.  Rows are returned as
    # plain dicts (never the raw connection), and every mutator is private.

    _SUPERVISOR_JOB_COLUMNS: frozenset[str] = frozenset(
        {
            "id", "task_id", "status", "workflow_state", "expected_role",
            "expected_dispatch_id", "agent_id", "session_id", "run_id",
            "attempt_no", "dispatch_status", "result_status", "result_consumed",
            "current_handoff_id", "open_findings_count", "rework_cycle",
            "recovery_state", "owner_gate_id", "gate_status", "gate_scope",
            "gate_closed", "owner_prompted_at", "owner_prompted_gate_id",
            "next_action", "next_wake_at",
            "retry_count", "missing_confirmations", "last_error_code",
            "last_progress_at", "terminal", "facts_version", "created_at",
            "updated_at",
            "primary_state", "queue_reason", "priority",
            "owner_instance_id", "lease_epoch", "lease_expires_at",
            "next_eligible_at", "error_class", "wait_kind",
        }
    )

    _SUPERVISOR_ACTION_COLUMNS: frozenset[str] = frozenset(
        {
            "id", "supervisor_job_id", "dispatch_id", "action_type", "action_key",
            "args_hash", "input_hash", "precondition_hash", "effect_hash",
            "patch_set_json",
            "status", "attempt_count", "next_attempt_at", "started_at",
            "finished_at", "last_error_code", "created_at", "updated_at",
        }
    )

    def get_supervisor_job(self, job_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM supervisor_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def get_supervisor_job_for_task(self, task_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM supervisor_jobs WHERE task_id = ? "
            "AND terminal IS NULL ORDER BY rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_supervisor_jobs(self, nonterminal_only: bool = False) -> list[dict]:
        q = "SELECT * FROM supervisor_jobs"
        if nonterminal_only:
            q += " WHERE terminal IS NULL"
        q += " ORDER BY rowid"
        rows = self._conn.execute(q).fetchall()
        return [dict(r) for r in rows]

    def _insert_supervisor_job(self, row: dict) -> None:
        missing = [c for c in self._SUPERVISOR_JOB_COLUMNS if c not in row]
        if missing:
            raise ValueError(f"missing supervisor_jobs columns: {missing}")
        cols = sorted(row.keys())
        ph = ", ".join("?" for _ in cols)
        self._conn.execute(
            f"INSERT INTO supervisor_jobs ({', '.join(cols)}) VALUES ({ph})",
            tuple(row[c] for c in cols),
        )

    def _update_supervisor_job(self, job_id: str, **fields) -> int:
        unknown = set(fields) - (self._SUPERVISOR_JOB_COLUMNS - {"id", "created_at"})
        if unknown:
            raise ValueError(f"unknown supervisor_jobs columns: {sorted(unknown)}")
        if not fields:
            return 0
        # F6: validate the orthogonal B1 enum columns on every write path.
        _validate_enum_fields(fields)
        # B1: keep ``primary_state`` in sync with the projection fields it is
        # derived from, unless the caller sets it explicitly (the queue/lease
        # primitives set it directly and skip this derivation).
        if "primary_state" not in fields:
            derived = self._derive_primary_state_for_update(job_id, fields)
            if derived is not None:
                fields = dict(fields)
                fields["primary_state"] = derived
        # F6: terminal stickiness — a sticky DONE/FAILED/BLOCKED row may not be
        # reopened by clearing its terminal or moving primary_state off the
        # terminal set.  Metadata-only updates that keep the terminal value
        # (and therefore the terminal primary_state) are still allowed.
        cur = self.get_supervisor_job(job_id)
        if cur is not None and cur.get("terminal") in ("DONE", "FAILED", "BLOCKED"):
            new_terminal = fields.get("terminal", cur["terminal"])
            new_ps = fields.get("primary_state", cur["primary_state"])
            if new_terminal is None or new_ps not in ("DONE", "FAILED", "BLOCKED"):
                raise LeaseError(
                    f"terminal job {job_id!r} is sticky and cannot be reopened"
                )
        assignments = ", ".join(f"{c} = ?" for c in fields)
        cur = self._conn.execute(
            f"UPDATE supervisor_jobs SET {assignments} WHERE id = ?",
            list(fields.values()) + [job_id],
        )
        return cur.rowcount

    def _derive_primary_state_for_update(
        self, job_id: str, fields: dict
    ) -> Optional[str]:
        """Derive ``primary_state`` when a projection field is being changed.

        Returns ``None`` when none of the projection inputs (status, terminal,
        recovery_state, wait_kind, queue_reason) is present in ``fields``, so
        the update proceeds unchanged.
        """
        proj = ("status", "terminal", "recovery_state", "wait_kind", "queue_reason")
        if not any(k in fields for k in proj):
            return None
        cur = self.get_supervisor_job(job_id)
        if cur is None:
            return None
        status = fields.get("status", cur["status"])
        terminal = fields.get("terminal", cur.get("terminal"))
        recovery_state = fields.get("recovery_state", cur.get("recovery_state"))
        wait_kind = fields.get("wait_kind", cur.get("wait_kind", "NONE"))
        queue_reason = fields.get("queue_reason", cur.get("queue_reason", "NEW"))
        return job_state.derive_primary_state(
            status,
            terminal=terminal,
            recovery_state=recovery_state,
            wait_kind=wait_kind,
            queue_reason=queue_reason,
        ).value

    # -- B1 durable queue / lease primitives (central, atomic, CAS) --------
    #
    # All lease operations run inside a single ``BEGIN IMMEDIATE`` transaction
    # so exactly one writer wins any claim.  ``lease_epoch`` is a monotonic
    # fencing token; ``lease_expires_at`` is the ONLY authority for expiry.
    # TTL is always caller-supplied local policy -- never agent output.

    @staticmethod
    def _validate_lease_owner(owner_instance_id: str) -> None:
        """F7: ``owner_instance_id`` must be a non-empty string."""
        if owner_instance_id is None or not str(owner_instance_id).strip():
            raise ValueError("owner_instance_id must be a non-empty string")

    @staticmethod
    def _validate_ttl(ttl_seconds: int) -> None:
        """F7: ``ttl_seconds`` must be > 0 and policy-bounded."""
        if ttl_seconds is None or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        if ttl_seconds > MAX_LEASE_TTL_SECONDS:
            raise ValueError(
                f"ttl_seconds {ttl_seconds} exceeds MAX_LEASE_TTL_SECONDS "
                f"({MAX_LEASE_TTL_SECONDS})"
            )

    #: status projections that are legal for each primary state (F6
    #: cross-consistency).  ``BACKOFF`` and ``WAITING_RUN`` both project to
    #: ``QUEUED`` (RETRY_BACKOFF keeps the V2C ``BACKOFF`` spelling).
    _PRIMARY_STATE_STATUSES: dict[str, frozenset] = {
        job_state.PrimaryState.QUEUED.value: frozenset({"WAITING_RUN", "BACKOFF"}),
        job_state.PrimaryState.RUNNING.value: frozenset({"ACTIVE"}),
        job_state.PrimaryState.WAITING_EXTERNAL.value: frozenset({"WAITING_RUN"}),
        job_state.PrimaryState.OWNER_GATE.value: frozenset({"WAITING_GATE"}),
        job_state.PrimaryState.LOST.value: frozenset({"RECOVERING"}),
        job_state.PrimaryState.BLOCKED.value: frozenset({"TERMINAL"}),
        job_state.PrimaryState.FAILED.value: frozenset({"TERMINAL"}),
        job_state.PrimaryState.DONE.value: frozenset({"TERMINAL"}),
    }

    def _transition_job(
        self,
        job_id: str,
        *,
        to_primary_state: str,
        to_status: str,
        fields: Optional[dict] = None,
        owner_authorized: bool = False,
        bump_facts_version: bool = False,
        cas_primary_state: Optional[str] = None,
        cas_owner_instance_id: Optional[str] = None,
        cas_lease_epoch: Optional[int] = None,
        cas_lease_unexpired: bool = False,
        now_iso: Optional[str] = None,
    ) -> dict:
        """Central primary-state transition primitive (F6).

        The single place where the queue/lease layer changes ``primary_state``
        + its V2C ``status`` projection.  Runs inside the caller's transaction
        and enforces, in order:

        1. enum validation of ``queue_reason``/``wait_kind``/``error_class``;
        2. terminal stickiness (DONE/FAILED never reopen; BLOCKED only with
           ``owner_authorized=True``);
        3. optional CAS preconditions (primary_state / owner / epoch / lease);
        4. cross-consistency: ``to_status`` must project back to
           ``to_primary_state`` (no drift);
        5. atomic write with a rowcount==1 check (F7).

        Returns the updated row.
        """
        now_iso = now_iso or self.now_iso()
        fields = dict(fields or {})
        _validate_enum_fields(fields)

        row = self._conn.execute(
            "SELECT * FROM supervisor_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"supervisor job {job_id!r} not found")
        job = dict(row)

        # -- terminal stickiness (F6) -------------------------------------
        terminal = job.get("terminal")
        cur_ps = job.get("primary_state")
        if terminal in ("DONE", "FAILED"):
            raise LeaseError(
                f"terminal job {job_id!r} is sticky ({terminal}) and cannot "
                f"transition"
            )
        if terminal == "BLOCKED" or cur_ps == job_state.PrimaryState.BLOCKED.value:
            if not owner_authorized:
                raise LeaseError(
                    f"blocked job {job_id!r} requires owner authorization to reopen"
                )

        # -- CAS preconditions (F7) ---------------------------------------
        if cas_primary_state is not None and cur_ps != cas_primary_state:
            raise LeaseError(
                f"primary_state CAS mismatch for job {job_id!r} "
                f"(expected {cas_primary_state!r}, got {cur_ps!r})"
            )
        if cas_owner_instance_id is not None and \
                job.get("owner_instance_id") != cas_owner_instance_id:
            raise LeaseError(f"owner CAS mismatch for job {job_id!r}")
        if cas_lease_epoch is not None and job.get("lease_epoch") != cas_lease_epoch:
            raise LeaseError(f"lease_epoch CAS mismatch for job {job_id!r}")
        if cas_lease_unexpired:
            expires = job.get("lease_expires_at")
            if expires is None or expires <= now_iso:
                raise LeaseError(f"lease expired for job {job_id!r}")

        # -- cross-consistency (F6) ---------------------------------------
        allowed = self._PRIMARY_STATE_STATUSES.get(to_primary_state)
        if allowed is None:
            raise ValueError(f"unknown primary_state {to_primary_state!r}")
        if to_status not in allowed:
            raise ValueError(
                f"status {to_status!r} does not project to primary_state "
                f"{to_primary_state!r}"
            )

        # -- write --------------------------------------------------------
        updates = dict(fields)
        updates["primary_state"] = to_primary_state
        updates["status"] = to_status
        if bump_facts_version:
            updates["facts_version"] = job["facts_version"] + 1
        updates["updated_at"] = now_iso
        assignments = ", ".join(f"{c} = ?" for c in updates)
        cur = self._conn.execute(
            f"UPDATE supervisor_jobs SET {assignments} WHERE id = ?",
            list(updates.values()) + [job_id],
        )
        if cur.rowcount != 1:
            raise LeaseError(f"transition CAS lost for job {job_id!r}")
        updated = self._conn.execute(
            "SELECT * FROM supervisor_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return dict(updated)

    @staticmethod
    def _job_is_claimable(job: dict, now_iso: str) -> tuple[bool, Optional[str]]:
        """Predicate deciding whether a job can be claimed (or taken over).

        Returns ``(claimable, reason)``.  Rules (Phase B1):

        * ``QUEUED``: claimable iff ``next_eligible_at`` is NULL or in the past
          AND no still-valid foreign lease is held.
        * ``RUNNING``: claimable ONLY as safe takeover when its lease has a
          CONCRETE, expired ``lease_expires_at`` (F3: a NULL expiry is never
          treated as "safely expired" — fail-closed, no takeover).
        * ``DONE``/``FAILED``: never (sticky terminal).
        * ``BLOCKED``: never via normal claim (only an explicit owner/policy
          requeue may reopen it -- no automatic path in B1).
        * ``LOST``: never treated like QUEUED (recovery path only).
        * ``WAITING_EXTERNAL``/``OWNER_GATE``: never via normal claim.
        """
        ps = job.get("primary_state")
        expires = job.get("lease_expires_at")
        if ps == job_state.PrimaryState.QUEUED.value:
            eligible = job.get("next_eligible_at")
            if eligible is not None and eligible > now_iso:
                return False, "not_eligible_yet"
            if (
                job.get("owner_instance_id") is not None
                and expires is not None
                and expires > now_iso
            ):
                return False, "foreign_lease"
            return True, None
        if ps == job_state.PrimaryState.RUNNING.value:
            # F3: takeover is only safe when there is a concrete expiry that
            # has actually lapsed.  A RUNNING row with ``lease_expires_at IS
            # NULL`` is a legacy/unleased row and must NEVER be claimed as a
            # "safe takeover" (no expiry evidence -> fail-closed).
            if expires is None:
                return False, "running_no_lease"
            if expires > now_iso:
                return False, "lease_active"
            return True, None
        return False, f"not_claimable:{ps}"

    def _do_claim_locked(
        self,
        job_id: str,
        *,
        owner_instance_id: str,
        ttl_seconds: int,
        now: datetime,
    ) -> dict:
        """Claim a job, assuming the caller already holds ``BEGIN IMMEDIATE``.

        Shared by :meth:`claim_job` and :meth:`claim_next_job` (which iterates
        candidates inside a single transaction).  Returns the updated row or
        raises :class:`LeaseError` / :class:`NotFound`.
        """
        now_iso = _format_dt(now)
        self._validate_lease_owner(owner_instance_id)
        self._validate_ttl(ttl_seconds)
        row = self._conn.execute(
            "SELECT * FROM supervisor_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"supervisor job {job_id!r} not found")
        job = dict(row)
        claimable, reason = self._job_is_claimable(job, now_iso)
        if not claimable:
            raise LeaseError(f"job {job_id!r} is not claimable: {reason}")
        new_epoch = job["lease_epoch"] + 1
        new_expires = _format_dt(now + timedelta(seconds=ttl_seconds))
        # F1: a claim invalidates any previously-authored decisions/contexts by
        # bumping ``facts_version``; F6/F7: the transition runs through the
        # central primitive with a primary_state + lease_epoch CAS.
        return self._transition_job(
            job_id,
            to_primary_state=job_state.PrimaryState.RUNNING.value,
            to_status="ACTIVE",
            fields={
                "owner_instance_id": owner_instance_id,
                "lease_epoch": new_epoch,
                "lease_expires_at": new_expires,
            },
            bump_facts_version=True,
            cas_primary_state=job["primary_state"],
            cas_lease_epoch=job["lease_epoch"],
            now_iso=now_iso,
        )

    def claim_job(
        self, job_id: str, *, owner_instance_id: str, ttl_seconds: int
    ) -> dict:
        """Atomically claim a claimable job for ``owner_instance_id``.

        Sets ``primary_state=RUNNING`` / ``status=ACTIVE``, bumps
        ``lease_epoch`` monotonically and sets ``lease_expires_at = now + ttl``.
        Raises :class:`LeaseError` when not claimable; returns the updated row.
        """
        now = self._clock()
        with self._transaction():
            return self._do_claim_locked(
                job_id,
                owner_instance_id=owner_instance_id,
                ttl_seconds=ttl_seconds,
                now=now,
            )

    def claim_next_job(
        self, *, owner_instance_id: str, ttl_seconds: int
    ) -> Optional[dict]:
        """Claim the next eligible job: highest ``priority`` first, then FIFO
        (``rowid``).  Returns the claimed row or ``None`` if nothing is
        claimable.  Exactly one caller wins any given job.
        """
        now = self._clock()
        now_iso = _format_dt(now)
        with self._transaction():
            rows = self._conn.execute(
                "SELECT * FROM supervisor_jobs ORDER BY priority DESC, rowid"
            ).fetchall()
            for row in rows:
                job = dict(row)
                claimable, _reason = self._job_is_claimable(job, now_iso)
                if not claimable:
                    continue
                return self._do_claim_locked(
                    job["id"],
                    owner_instance_id=owner_instance_id,
                    ttl_seconds=ttl_seconds,
                    now=now,
                )
            return None

    def renew_lease(
        self,
        job_id: str,
        *,
        owner_instance_id: str,
        lease_epoch: int,
        ttl_seconds: int,
    ) -> dict:
        """Atomically renew a still-valid lease held by (owner, epoch).

        Raises :class:`LeaseError` on owner/epoch mismatch or expiry (an
        expired lease cannot be silently extended by a stale holder).
        """
        now = self._clock()
        now_iso = _format_dt(now)
        self._validate_lease_owner(owner_instance_id)
        self._validate_ttl(ttl_seconds)
        new_expires = _format_dt(now + timedelta(seconds=ttl_seconds))
        with self._transaction():
            row = self._conn.execute(
                "SELECT * FROM supervisor_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"supervisor job {job_id!r} not found")
            job = dict(row)
            if job["owner_instance_id"] != owner_instance_id:
                raise LeaseError(f"renew owner mismatch for job {job_id!r}")
            if job["lease_epoch"] != lease_epoch:
                raise LeaseError(f"renew epoch mismatch for job {job_id!r}")
            if job["lease_expires_at"] is None or job["lease_expires_at"] <= now_iso:
                raise LeaseError(f"lease expired for job {job_id!r}")
            # F7: CAS rowcount must be exactly 1 (no silent drift on a lost
            # race); the WHERE clause also re-checks the unexpired lease.
            cur = self._conn.execute(
                "UPDATE supervisor_jobs SET lease_expires_at = ?, updated_at = ? "
                "WHERE id = ? AND owner_instance_id = ? AND lease_epoch = ? "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
                (new_expires, now_iso, job_id, owner_instance_id, lease_epoch,
                 now_iso),
            )
            if cur.rowcount != 1:
                raise LeaseError(f"renew CAS lost for job {job_id!r}")
            updated = self._conn.execute(
                "SELECT * FROM supervisor_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return dict(updated)

    def release_lease(
        self, job_id: str, *, owner_instance_id: str, lease_epoch: int
    ) -> dict:
        """Release the lease back to the queue (``primary_state=QUEUED``).

        Verifies ownership + epoch; clears ``owner_instance_id`` and
        ``lease_expires_at`` (``lease_epoch`` stays monotonic).  Raises
        :class:`LeaseError` on mismatch.
        """
        now = self._clock()
        now_iso = _format_dt(now)
        self._validate_lease_owner(owner_instance_id)
        with self._transaction():
            # F5: an expired lease may not be mutated by its (stale) holder.
            # F6/F7: route through the central transition primitive with an
            # owner+epoch+unexpired-lease CAS and a facts_version bump (F1).
            return self._transition_job(
                job_id,
                to_primary_state=job_state.PrimaryState.QUEUED.value,
                to_status="WAITING_RUN",
                fields={
                    "owner_instance_id": None,
                    "lease_expires_at": None,
                },
                bump_facts_version=True,
                cas_owner_instance_id=owner_instance_id,
                cas_lease_epoch=lease_epoch,
                cas_lease_unexpired=True,
                now_iso=now_iso,
            )

    def lease_is_current(
        self, job_id: str, owner_instance_id: str, lease_epoch: int
    ) -> bool:
        """True iff (owner, epoch) is the current, unexpired lease holder."""
        now = self._clock()
        now_iso = _format_dt(now)
        row = self._conn.execute(
            "SELECT owner_instance_id, lease_epoch, lease_expires_at "
            "FROM supervisor_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return False
        if row["owner_instance_id"] != owner_instance_id:
            return False
        if row["lease_epoch"] != lease_epoch:
            return False
        expires = row["lease_expires_at"]
        return expires is not None and expires > now_iso

    def assert_lease_current(
        self, job_id: str, owner_instance_id: str, lease_epoch: int
    ) -> None:
        """Central reusable fencing check (Phase B1).

        Verifies ``job_id`` + ``owner_instance_id`` + ``lease_epoch`` +
        ``lease_expires_at > now``.  Raises :class:`LeaseFencedError` on any
        mismatch (stale owner after takeover, wrong epoch, or expired lease).
        """
        now = self._clock()
        now_iso = _format_dt(now)
        row = self._conn.execute(
            "SELECT owner_instance_id, lease_epoch, lease_expires_at "
            "FROM supervisor_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise LeaseFencedError(f"job {job_id!r} not found")
        if row["owner_instance_id"] != owner_instance_id:
            raise LeaseFencedError(
                f"lease fence: {owner_instance_id!r} is not the current owner "
                f"of job {job_id!r}"
            )
        if row["lease_epoch"] != lease_epoch:
            raise LeaseFencedError(
                f"lease fence: epoch {lease_epoch} is stale for job {job_id!r} "
                f"(current {row['lease_epoch']})"
            )
        expires = row["lease_expires_at"]
        if expires is None or expires <= now_iso:
            raise LeaseFencedError(f"lease expired for job {job_id!r}")

    def enqueue_job(
        self,
        job_id: str,
        *,
        queue_reason: str = "NEW",
        priority: int = 0,
        next_eligible_at: Optional[str] = None,
        wait_kind: str = "NONE",
        error_class: str = "NONE",
        error_code: Optional[str] = None,
        bump_attempt: bool = False,
        # F2: authorization for holder-requeue / BLOCKED-reopen.
        owner_instance_id: Optional[str] = None,
        lease_epoch: Optional[int] = None,
        owner_authorized: bool = False,
        policy_ref: Optional[str] = None,
    ) -> dict:
        """(Re-)enqueue a job as ``QUEUED`` with queue/retry metadata (F2).

        Three disjoint authorization paths (a foreign valid lease is NEVER
        silently removed):

        a) *Initial enqueue / lease-free QUEUED* — the job holds no lease and
           is already QUEUED (new-creation bootstrap).  No ownership CAS.
        b) *Holder retry / requeue from RUNNING* — requires the current,
           unexpired ``(owner_instance_id, lease_epoch)`` as a CAS; a foreign or
           expired lease is refused (:class:`LeaseError`).
        c) *Authorized BLOCKED→QUEUED requeue* — requires ``owner_authorized=True``
           AND a ``policy_ref`` (no automatic reopen).

        DONE/FAILED (sticky terminal) are always refused as a domain error.
        ``queue_reason=RETRY_BACKOFF`` projects to ``BACKOFF``; other reasons
        to ``WAITING_RUN``.  ``error_code`` lands on ``last_error_code``.
        """
        now = self._clock()
        now_iso = _format_dt(now)
        status = job_state.status_for_enqueue(queue_reason)
        with self._transaction():
            row = self._conn.execute(
                "SELECT * FROM supervisor_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"supervisor job {job_id!r} not found")
            job = dict(row)
            ps = job.get("primary_state")
            terminal = job.get("terminal")
            # F2: DONE/FAILED are sticky domain errors (never requeue).
            if terminal in ("DONE", "FAILED"):
                raise LeaseError(
                    f"terminal job {job_id!r} ({terminal}) cannot be requeued"
                )
            attempt_no = job["attempt_no"] + 1 if bump_attempt else job["attempt_no"]
            common_fields = {
                "queue_reason": queue_reason,
                "priority": priority,
                "next_eligible_at": next_eligible_at,
                "wait_kind": wait_kind,
                "error_class": error_class,
                "last_error_code": error_code,
                "attempt_no": attempt_no,
                "owner_instance_id": None,
                "lease_expires_at": None,
            }
            if owner_authorized:
                # (c) authorized BLOCKED→QUEUED requeue.
                if policy_ref is None:
                    raise LeaseError(
                        "owner_authorized requeue requires a policy_ref"
                    )
                # A reopened BLOCKED job must also clear its terminal marker so
                # the supervisor no longer short-circuits on it.
                reopen_fields = dict(common_fields, terminal=None)
                return self._transition_job(
                    job_id,
                    to_primary_state=job_state.PrimaryState.QUEUED.value,
                    to_status=status,
                    fields=reopen_fields,
                    owner_authorized=True,
                    bump_facts_version=True,
                    now_iso=now_iso,
                )
            if owner_instance_id is not None or lease_epoch is not None:
                # (b) holder retry / requeue from RUNNING (CAS on owner+epoch).
                self._validate_lease_owner(owner_instance_id)
                return self._transition_job(
                    job_id,
                    to_primary_state=job_state.PrimaryState.QUEUED.value,
                    to_status=status,
                    fields=common_fields,
                    bump_facts_version=True,
                    cas_owner_instance_id=owner_instance_id,
                    cas_lease_epoch=lease_epoch,
                    cas_lease_unexpired=True,
                    now_iso=now_iso,
                )
            # (a) initial enqueue / lease-free QUEUED job.
            if job.get("owner_instance_id") is not None:
                raise LeaseError(
                    f"job {job_id!r} holds a lease; requeue requires the "
                    f"holder (owner_instance_id, lease_epoch) CAS"
                )
            if ps != job_state.PrimaryState.QUEUED.value:
                raise LeaseError(
                    f"job {job_id!r} is {ps!r}; initial enqueue requires a "
                    f"lease-free QUEUED job"
                )
            return self._transition_job(
                job_id,
                to_primary_state=job_state.PrimaryState.QUEUED.value,
                to_status=status,
                fields=common_fields,
                bump_facts_version=True,
                now_iso=now_iso,
            )

    def get_supervisor_action(self, action_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM supervisor_actions WHERE id = ?", (action_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def get_supervisor_action_by_key(self, action_key: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM supervisor_actions WHERE action_key = ?", (action_key,)
        ).fetchone()
        return dict(row) if row is not None else None

    def list_supervisor_actions(self, job_id: Optional[str] = None) -> list[dict]:
        q = "SELECT * FROM supervisor_actions"
        params: list = []
        if job_id is not None:
            q += " WHERE supervisor_job_id = ?"
            params.append(job_id)
        q += " ORDER BY rowid"
        rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def _insert_supervisor_action(self, row: dict) -> None:
        row = dict(row)
        # ``patch_set_json`` is a newly added nullable column; existing callers
        # (and older persisted rows) may omit it, so default it here.
        if "patch_set_json" not in row:
            row["patch_set_json"] = None
        missing = [c for c in self._SUPERVISOR_ACTION_COLUMNS if c not in row]
        if missing:
            raise ValueError(f"missing supervisor_actions columns: {missing}")
        cols = sorted(row.keys())
        ph = ", ".join("?" for _ in cols)
        self._conn.execute(
            f"INSERT INTO supervisor_actions ({', '.join(cols)}) VALUES ({ph})",
            tuple(row[c] for c in cols),
        )

    def _update_supervisor_action(self, action_id: str, **fields) -> int:
        unknown = set(fields) - (self._SUPERVISOR_ACTION_COLUMNS - {"id"})
        if unknown:
            raise ValueError(f"unknown supervisor_actions columns: {sorted(unknown)}")
        if not fields:
            return 0
        assignments = ", ".join(f"{c} = ?" for c in fields)
        cur = self._conn.execute(
            f"UPDATE supervisor_actions SET {assignments} WHERE id = ?",
            list(fields.values()) + [action_id],
        )
        return cur.rowcount

    # -- canonical write intents (V4, SPEC V2C §17 R13) ---------------------
    # The DB-enforced single-canonical-APPLY-intent-per-dispatch binding.  A
    # row is inserted exactly once per dispatch (in the SAME transaction as
    # the APPLY journal row it binds) and is immutable thereafter.

    _DISPATCH_WRITE_INTENT_COLUMNS: frozenset[str] = frozenset(
        {
            "dispatch_id", "canonical_input_hash", "intent_action_id",
            "created_at", "updated_at",
        }
    )

    def get_dispatch_write_intent(self, dispatch_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM dispatch_write_intents WHERE dispatch_id = ?",
            (dispatch_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def _insert_dispatch_write_intent(self, row: dict) -> None:
        missing = [
            c for c in self._DISPATCH_WRITE_INTENT_COLUMNS if c not in row
        ]
        if missing:
            raise ValueError(f"missing dispatch_write_intents columns: {missing}")
        self._conn.execute(
            "INSERT INTO dispatch_write_intents "
            "(dispatch_id, canonical_input_hash, intent_action_id, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                row["dispatch_id"], row["canonical_input_hash"],
                row["intent_action_id"], row["created_at"], row["updated_at"],
            ),
        )

    # -- notification outbox (V5, SPEC V3A §4) -----------------------------
    # The only persistence surface the notification subsystem uses.  Rows are
    # plain dicts (never the raw connection); every mutator is private and all
    # queries read/write ONLY ``notification_outbox``.

    _NOTIFICATION_OUTBOX_COLUMNS: frozenset[str] = frozenset(
        {
            "id", "supervisor_job_id", "task_id", "dispatch_id", "gate_id",
            "notification_type", "event_ref", "event_version", "dedup_key",
            "payload_json", "payload_hash", "status", "attempt_count",
            "next_attempt_at", "claimed_at", "claim_token", "last_attempt_at",
            "sent_at", "last_error_code", "created_at", "updated_at",
        }
    )

    def get_notification(self, notification_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM notification_outbox WHERE id = ?", (notification_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def list_notifications(self, job_id: Optional[str] = None) -> list[dict]:
        q = "SELECT * FROM notification_outbox"
        params: list = []
        if job_id is not None:
            q += " WHERE supervisor_job_id = ?"
            params.append(job_id)
        q += " ORDER BY created_at, rowid"
        rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def _insert_notification(self, row: dict) -> bool:
        """Insert a notification row idempotently on ``dedup_key`` (SPEC V3A
        Amendment 3).

        Returns True if a row was inserted, False on a ``dedup_key`` UNIQUE
        conflict (silent no-op).  Any OTHER constraint violation (CHECK / FK /
        NOT NULL) still raises.  Must be called inside the caller's
        transaction (the authoritative transition's ``BEGIN IMMEDIATE``).
        """
        missing = [c for c in self._NOTIFICATION_OUTBOX_COLUMNS if c not in row]
        if missing:
            raise ValueError(f"missing notification_outbox columns: {missing}")
        cur = self._conn.execute(
            "INSERT INTO notification_outbox (id, supervisor_job_id, task_id, "
            "dispatch_id, gate_id, notification_type, event_ref, event_version, "
            "dedup_key, payload_json, payload_hash, status, attempt_count, "
            "next_attempt_at, claimed_at, claim_token, last_attempt_at, "
            "sent_at, last_error_code, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?) "
            "ON CONFLICT(dedup_key) DO NOTHING",
            (
                row["id"], row["supervisor_job_id"], row["task_id"],
                row["dispatch_id"], row["gate_id"], row["notification_type"],
                row["event_ref"], row["event_version"], row["dedup_key"],
                row["payload_json"], row["payload_hash"], row["status"],
                row["attempt_count"], row["next_attempt_at"], row["claimed_at"],
                row["claim_token"], row["last_attempt_at"], row["sent_at"],
                row["last_error_code"], row["created_at"], row["updated_at"],
            ),
        )
        return cur.rowcount == 1

    def _select_due_notification(
        self, now_iso: str, lease_seconds: int
    ) -> Optional[dict]:
        """The oldest due row: PENDING, retryable FAILED with
        ``next_attempt_at <= now``, or lease-expired SENDING with
        ``claimed_at + lease <= now`` (SPEC V3A §9.2).  Returns None when
        nothing is due."""
        cutoff = _format_dt(self._clock() - timedelta(seconds=lease_seconds))
        row = self._conn.execute(
            "SELECT * FROM notification_outbox "
            "WHERE status = 'PENDING' "
            "   OR (status = 'FAILED' AND next_attempt_at IS NOT NULL "
            "       AND next_attempt_at <= ?) "
            "   OR (status = 'SENDING' AND claimed_at IS NOT NULL "
            "       AND claimed_at <= ?) "
            "ORDER BY created_at, rowid LIMIT 1",
            (now_iso, cutoff),
        ).fetchone()
        return dict(row) if row is not None else None

    def _claim_notification(
        self, notification_id: str, claim_token: str, now_iso: str,
        lease_seconds: int,
    ) -> bool:
        """Atomically claim the oldest due row under ``BEGIN IMMEDIATE``
        (SPEC V3A §9.2): status SENDING, a fresh claim_token, time fields and
        ``attempt_count += 1``.  Returns True iff this caller performed the
        claim for ``notification_id``."""
        with self._transaction():
            row = self._select_due_notification(now_iso, lease_seconds)
            if row is None or row["id"] != notification_id:
                return False
            cur = self._conn.execute(
                "UPDATE notification_outbox SET status = 'SENDING', "
                "claim_token = ?, claimed_at = ?, last_attempt_at = ?, "
                "attempt_count = attempt_count + 1, next_attempt_at = NULL, "
                "updated_at = ? "
                "WHERE id = ? AND status IN ('PENDING', 'FAILED', 'SENDING')",
                (claim_token, now_iso, now_iso, now_iso, notification_id),
            )
            return cur.rowcount == 1

    def _complete_notification_sent(
        self, notification_id: str, claim_token: str, now_iso: str,
    ) -> bool:
        """Completion CAS (SPEC V3A §9.3): SENDING -> SENT, sent_at=now, and
        clear the claim/attempt fields.  Returns True iff this claim won."""
        cur = self._conn.execute(
            "UPDATE notification_outbox SET status = 'SENT', sent_at = ?, "
            "claim_token = NULL, claimed_at = NULL, next_attempt_at = NULL, "
            "last_error_code = NULL, last_attempt_at = ?, updated_at = ? "
            "WHERE id = ? AND status = 'SENDING' AND claim_token = ?",
            (now_iso, now_iso, now_iso, notification_id, claim_token),
        )
        return cur.rowcount == 1

    def _mark_notification_failed(
        self, notification_id: str, claim_token: str, now_iso: str, *,
        next_attempt_at: str, error_code: str,
    ) -> bool:
        """CAS transition SENDING -> FAILED (retryable) with backoff + code."""
        cur = self._conn.execute(
            "UPDATE notification_outbox SET status = 'FAILED', "
            "claim_token = NULL, claimed_at = NULL, next_attempt_at = ?, "
            "last_error_code = ?, updated_at = ? "
            "WHERE id = ? AND status = 'SENDING' AND claim_token = ?",
            (next_attempt_at, error_code, now_iso, notification_id, claim_token),
        )
        return cur.rowcount == 1

    def _discard_notification(
        self, notification_id: str, claim_token: str, now_iso: str, *,
        error_code: str,
    ) -> bool:
        """CAS transition SENDING -> DISCARDED (terminal) with a code."""
        cur = self._conn.execute(
            "UPDATE notification_outbox SET status = 'DISCARDED', "
            "claim_token = NULL, claimed_at = NULL, next_attempt_at = NULL, "
            "last_error_code = ?, updated_at = ? "
            "WHERE id = ? AND status = 'SENDING' AND claim_token = ?",
            (error_code, now_iso, notification_id, claim_token),
        )
        return cur.rowcount == 1

    # -- owner-approval challenges (V6, SPEC V3C §8.1) ---------------------
    # The only persistence surface for the challenge core.  Rows are plain
    # dicts; every mutator is private.  These methods read/write ONLY
    # ``approval_challenges``.

    _APPROVAL_CHALLENGE_COLUMNS: frozenset[str] = frozenset(
        {
            "id", "approval_id", "task_id", "supervisor_job_id",
            "binding_hash", "notification_outbox_id", "token_hash", "status",
            "created_at", "expires_at", "consumed_at", "consumed_update_id",
            "invalidated_at",
        }
    )

    def _insert_challenge(self, row: dict) -> None:
        """Insert a full challenge row (must be called inside a transaction)."""
        missing = [c for c in self._APPROVAL_CHALLENGE_COLUMNS if c not in row]
        if missing:
            raise ValueError(f"missing approval_challenges columns: {missing}")
        self._conn.execute(
            "INSERT INTO approval_challenges (id, approval_id, task_id, "
            "supervisor_job_id, binding_hash, notification_outbox_id, "
            "token_hash, status, created_at, expires_at, consumed_at, "
            "consumed_update_id, invalidated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row["approval_id"], row["task_id"],
                row["supervisor_job_id"], row["binding_hash"],
                row["notification_outbox_id"], row["token_hash"], row["status"],
                row["created_at"], row["expires_at"], row["consumed_at"],
                row["consumed_update_id"], row["invalidated_at"],
            ),
        )

    def get_challenge(self, challenge_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM approval_challenges WHERE id = ?", (challenge_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def get_active_challenge_for_approval(
        self, approval_id: str
    ) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM approval_challenges "
            "WHERE approval_id = ? AND status = 'ISSUED'",
            (approval_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_challenge_by_token_hash(self, token_hash: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM approval_challenges WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_challenges(
        self,
        task_id: Optional[str] = None,
        supervisor_job_id: Optional[str] = None,
    ) -> list[dict]:
        q = "SELECT * FROM approval_challenges"
        params: list = []
        clauses: list[str] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if supervisor_job_id is not None:
            clauses.append("supervisor_job_id = ?")
            params.append(supervisor_job_id)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY created_at, rowid"
        rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def _consume_challenge(
        self, challenge_id: str, status: str, consumed_at: str,
        consumed_update_id: int,
    ) -> int:
        """CAS ISSUED -> CONSUMED_APPROVED / CONSUMED_REJECTED (single-use).

        Returns the rowcount (exactly 1 iff this caller won)."""
        cur = self._conn.execute(
            "UPDATE approval_challenges SET status = ?, consumed_at = ?, "
            "consumed_update_id = ? "
            "WHERE id = ? AND status = 'ISSUED'",
            (status, consumed_at, consumed_update_id, challenge_id),
        )
        return cur.rowcount

    def _mark_challenge_expired(self, challenge_id: str, now_iso: str) -> int:
        """CAS ISSUED -> EXPIRED (terminal; invalidated_at set)."""
        cur = self._conn.execute(
            "UPDATE approval_challenges SET status = 'EXPIRED', "
            "invalidated_at = ? WHERE id = ? AND status = 'ISSUED'",
            (now_iso, challenge_id),
        )
        return cur.rowcount

    def _mark_challenge_invalidated(self, challenge_id: str, now_iso: str) -> int:
        """CAS ISSUED -> INVALIDATED (terminal; invalidated_at set)."""
        cur = self._conn.execute(
            "UPDATE approval_challenges SET status = 'INVALIDATED', "
            "invalidated_at = ? WHERE id = ? AND status = 'ISSUED'",
            (now_iso, challenge_id),
        )
        return cur.rowcount

    # -- telegram update log + inbound cursor (V6, SPEC V3C §8.1) ---------
    # Update-ID dedup + restart-fixed offset.  Real chat/sender IDs are never
    # stored; only ``chat_authorized`` / ``sender_authorized`` booleans (§8.2).

    _UPDATE_LOG_COLUMNS: frozenset[str] = frozenset(
        {
            "update_id", "message_date", "chat_authorized", "sender_authorized",
            "decision", "challenge_id", "approval_id", "outcome",
            "received_at", "processed_at",
        }
    )

    def _insert_update_log(self, row: dict) -> bool:
        """Insert an update-log row idempotently on ``update_id`` (the PK).

        Returns True iff a row was inserted; False on a PK (duplicate update)
        conflict.  Any OTHER constraint violation (CHECK/FK/NOT NULL) raises.
        Must be called inside the caller's transaction."""
        missing = [c for c in self._UPDATE_LOG_COLUMNS if c not in row]
        if missing:
            raise ValueError(f"missing telegram_update_log columns: {missing}")
        cur = self._conn.execute(
            "INSERT INTO telegram_update_log (update_id, message_date, "
            "chat_authorized, sender_authorized, decision, challenge_id, "
            "approval_id, outcome, received_at, processed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(update_id) DO NOTHING",
            (
                row["update_id"], row["message_date"], row["chat_authorized"],
                row["sender_authorized"], row["decision"], row["challenge_id"],
                row["approval_id"], row["outcome"], row["received_at"],
                row["processed_at"],
            ),
        )
        return cur.rowcount == 1

    def _finalize_update_log(
        self, update_id: int, *, decision: Optional[str],
        challenge_id: Optional[str], approval_id: Optional[str],
        outcome: str, processed_at: str,
    ) -> bool:
        """Move a ``PROCESSING`` update-log row to its terminal outcome.

        Must be called inside the caller's transaction (SPEC V3C §10.2 step 16).
        Returns True iff exactly one row was updated (it always should be, since
        the row was inserted with ``PROCESSING`` in the same transaction)."""
        cur = self._conn.execute(
            "UPDATE telegram_update_log SET decision = ?, challenge_id = ?, "
            "approval_id = ?, outcome = ?, processed_at = ? WHERE update_id = ?",
            (decision, challenge_id, approval_id, outcome, processed_at, update_id),
        )
        return cur.rowcount == 1

    def get_update_log(self, update_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM telegram_update_log WHERE update_id = ?", (update_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def get_inbound_state(self) -> Optional[dict]:
        """The single-row cursor (stream_id is fixed to the one V6 stream)."""
        row = self._conn.execute(
            "SELECT * FROM telegram_inbound_state "
            "WHERE stream_id = 'telegram-owner-approval-v1'",
        ).fetchone()
        return dict(row) if row is not None else None

    def _set_inbound_state(self, next_update_id: int, updated_at: str) -> None:
        """UPSERT the single cursor row (monotonic next_update_id)."""
        self._conn.execute(
            "INSERT INTO telegram_inbound_state (stream_id, next_update_id, "
            "updated_at) VALUES ('telegram-owner-approval-v1', ?, ?) "
            "ON CONFLICT(stream_id) DO UPDATE SET next_update_id = excluded."
            "next_update_id, updated_at = excluded.updated_at",
            (next_update_id, updated_at),
        )


class Queries:
    """Strictly read-only ``get_*``/``list_*`` facade (SPEC V1.1 11.5, R8)."""

    def __init__(self, store: Store):
        self._store = store

    def get_project(self, project_id: str):
        return self._store.get_project(project_id)

    def get_task(self, task_id: str):
        return self._store.get_task(task_id)

    def list_tasks(self):
        return self._store.list_tasks()

    def get_task_run(self, run_id: str):
        return self._store.get_task_run(run_id)

    def list_task_runs(self, task_id=None, status=None):
        return self._store.list_task_runs(task_id, status)

    def get_role_run(self, run_id: str):
        return self._store.get_role_run(run_id)

    def list_role_runs(self, task_id=None, status=None):
        return self._store.list_role_runs(task_id, status)

    def get_active_role_run(self, task_id: str):
        return self._store.get_active_role_run(task_id)

    def list_handoffs(self, task_id=None):
        return self._store.list_handoffs(task_id)

    def get_finding(self, finding_id: str):
        return self._store.get_finding(finding_id)

    def list_findings(self, task_id=None):
        return self._store.list_findings(task_id)

    def get_test_run(self, run_id: str):
        return self._store.get_test_run(run_id)

    def list_test_runs(self, task_id=None):
        return self._store.list_test_runs(task_id)

    def get_review(self, review_id: str):
        return self._store.get_review(review_id)

    def get_decision(self, decision_id: str):
        return self._store.get_decision(decision_id)

    def list_decisions(self, task_id=None):
        return self._store.list_decisions(task_id)

    def list_reviews(self, task_id=None):
        return self._store.list_reviews(task_id)

    def get_approval(self, approval_id: str):
        return self._store.get_approval(approval_id)

    def list_approvals(self, task_id=None):
        return self._store.list_approvals(task_id)

    def get_action_execution(self, execution_id: str):
        return self._store.get_action_execution(execution_id)

    def list_action_executions(self, task_id=None):
        return self._store.list_action_executions(task_id)

    def list_events(self, task_id=None):
        return self._store.list_events(task_id)

    def get_dispatch(self, dispatch_id: str):
        return self._store.get_dispatch(dispatch_id)

    def list_dispatches(self, task_id=None, status=None):
        return self._store.list_dispatches(task_id, status)

    def list_quarantine(self, task_id=None):
        return self._store.list_quarantine(task_id)

    def get_context_snapshot(self, dispatch_id: str):
        return self._store.get_context_snapshot(dispatch_id)

    def list_context_snapshots(self, task_id=None):
        return self._store.list_context_snapshots(task_id)

    def get_latest_decision(self, task_id: str):
        return self._store.get_latest_decision(task_id)

    def get_latest_task_run(self, task_id: str):
        return self._store.get_latest_task_run(task_id)
