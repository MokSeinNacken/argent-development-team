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

SCHEMA_VERSION = "4"

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
    """
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
        assignments = ", ".join(f"{c} = ?" for c in fields)
        cur = self._conn.execute(
            f"UPDATE supervisor_jobs SET {assignments} WHERE id = ?",
            list(fields.values()) + [job_id],
        )
        return cur.rowcount

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
