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
from .models import (
    ActionExecution,
    ActionExecutionStatus,
    ApprovalStatus,
    Decision,
    Event,
    Finding,
    FindingStatus,
    Handoff,
    OwnerApproval,
    Project,
    Role,
    RoleRun,
    RoleRunStatus,
    SourceClass,
    Task,
    TaskRun,
    TaskRunStatus,
    TaskState,
    TestResult,
    TestRun,
)

SCHEMA_VERSION = "2"

_TASK_STATES = "', '".join(s.value for s in TaskState)
_TASK_RUN_STATUSES = "', '".join(s.value for s in TaskRunStatus)
_ROLE_VALUES = "', '".join(r.value for r in Role)
_ROLE_RUN_STATUSES = "', '".join(s.value for s in RoleRunStatus)
_APPROVAL_STATUSES = "', '".join(s.value for s in ApprovalStatus)
_SOURCE_CLASSES = "', '".join(s.value for s in SourceClass)
_EXECUTION_STATUSES = "', '".join(s.value for s in ActionExecutionStatus)

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
        state          TEXT NOT NULL CHECK (state IN ('{_TASK_STATES}')),
        resume_state   TEXT CHECK (resume_state IS NULL OR resume_state IN ('{_TASK_STATES}')),
        source         TEXT NOT NULL,
        source_class   TEXT NOT NULL CHECK (source_class IN ('{_SOURCE_CLASSES}')),
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
        idempotency_key TEXT UNIQUE
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
        for stmt in _SCHEMA:
            self._conn.execute(stmt)
        self._conn.execute(
            "INSERT OR IGNORE INTO schema_meta (key, value) VALUES (?, ?)",
            ("schema_version", SCHEMA_VERSION),
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
            "INSERT INTO tasks (id, project_id, title, state, resume_state, "
            "source, source_class, created_at, updated_at, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                t.id,
                t.project_id,
                t.title,
                t.state.value,
                t.resume_state.value if t.resume_state else None,
                t.source,
                t.source_class.value,
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
            )
            out.append((task, valid))
        return out

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
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

    # -- owner_approvals -----------------------------------------------------

    def _insert_approval(self, a: OwnerApproval) -> None:
        self._conn.execute(
            "INSERT INTO owner_approvals (id, task_id, action, scope, status, "
            "requested_by, source_class, created_at, decided_at, consumed_at, "
            "expires_at, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        )

    # Atomic approval transitions (return rowcount for single-use enforcement).

    def _mark_approved(self, approval_id: str, now_iso: str) -> int:
        cur = self._conn.execute(
            "UPDATE owner_approvals SET status = 'approved', decided_at = ? "
            "WHERE id = ? AND status = 'pending' AND expires_at > ?",
            (now_iso, approval_id, now_iso),
        )
        return cur.rowcount

    def _mark_rejected(self, approval_id: str, now_iso: str) -> int:
        cur = self._conn.execute(
            "UPDATE owner_approvals SET status = 'rejected', decided_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (now_iso, approval_id),
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

    def _mark_expired(self, approval_id: str, now_iso: str) -> int:
        # Covers both 'pending' and 'approved' (SPEC V1.2 12.3): an expired
        # 'approved' approval must also transition to 'expired' so the unique
        # index no longer blocks a fresh request.
        cur = self._conn.execute(
            "UPDATE owner_approvals SET status = 'expired' "
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
