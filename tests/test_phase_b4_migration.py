"""Phase B4 — migration / reopen acceptance (integrated, deterministic).

Proves the additive schema migration across the Phase-B versions:

* a fresh DB lands on the current ``SCHEMA_VERSION`` with all Phase-B tables;
* a legacy pre-B1 (V6) DB migrates forward (B1 + B3 columns/tables) with
  correct ``primary_state`` backfill and NO row loss;
* a B1 (V7) DB migrates forward to B3 (worktree/writer columns + external_wait
  / process_registry tables);
* reopening an already-migrated DB is idempotent (no error, no data change);
* the migration is non-destructive and the schema version is deterministic.

There is no fixtures/ legacy DB in ``tests/fixtures/`` (only an unrelated
``.d.ts`` snippet), so the legacy DBs are built programmatically to the exact
pre-B1/B1 column sets.
"""

from __future__ import annotations

import sqlite3

from argent_core import Core
from argent_core.store import SCHEMA_VERSION

#: Pre-B1 (V6-era) ``supervisor_jobs`` shape (no B1 queue/lease columns, no B3
#: worktree columns).
_OLD_SUPERVISOR_JOBS_DDL = """
CREATE TABLE supervisor_jobs (
    id                    TEXT PRIMARY KEY,
    task_id               TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    status                TEXT NOT NULL CHECK (status IN
                          ('ACTIVE','WAITING_RUN','WAITING_GATE','BACKOFF',
                           'RECOVERING','ERROR','TERMINAL')),
    workflow_state        TEXT NOT NULL,
    expected_role         TEXT,
    expected_dispatch_id  TEXT,
    agent_id              TEXT,
    session_id            TEXT,
    run_id                TEXT,
    attempt_no            INTEGER NOT NULL DEFAULT 0,
    dispatch_status       TEXT,
    result_status         TEXT NOT NULL DEFAULT 'NOT_OBSERVED',
    result_consumed       INTEGER NOT NULL DEFAULT 0,
    current_handoff_id    TEXT,
    open_findings_count   INTEGER NOT NULL DEFAULT 0,
    rework_cycle          INTEGER NOT NULL DEFAULT 1,
    recovery_state        TEXT NOT NULL DEFAULT 'NONE',
    owner_gate_id         TEXT,
    gate_status           TEXT,
    gate_scope            TEXT,
    gate_closed           INTEGER NOT NULL DEFAULT 0,
    owner_prompted_at     TEXT,
    owner_prompted_gate_id TEXT,
    next_action           TEXT NOT NULL DEFAULT 'NONE',
    next_wake_at          TEXT,
    retry_count           INTEGER NOT NULL DEFAULT 0,
    missing_confirmations INTEGER NOT NULL DEFAULT 0,
    last_error_code       TEXT,
    last_progress_at      TEXT NOT NULL,
    terminal              TEXT CHECK (terminal IS NULL OR terminal IN
                          ('DONE','FAILED','BLOCKED')),
    facts_version         INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
)
"""

_B1_COLUMNS = (
    ("primary_state", "TEXT NOT NULL DEFAULT 'QUEUED'"),
    ("queue_reason", "TEXT NOT NULL DEFAULT 'NEW'"),
    ("priority", "INTEGER NOT NULL DEFAULT 0"),
    ("owner_instance_id", "TEXT"),
    ("lease_epoch", "INTEGER NOT NULL DEFAULT 0"),
    ("lease_expires_at", "TEXT"),
    ("next_eligible_at", "TEXT"),
    ("error_class", "TEXT NOT NULL DEFAULT 'NONE'"),
    ("wait_kind", "TEXT NOT NULL DEFAULT 'NONE'"),
)


def _build_legacy_db(path: str, *, b1: bool) -> None:
    """Build a minimal legacy DB (V6 pre-B1, or V7 = B1 without B3)."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
        ("7" if b1 else "6",))
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
        "title TEXT NOT NULL, description TEXT, state TEXT NOT NULL, "
        "resume_state TEXT, source TEXT NOT NULL, source_class TEXT NOT NULL, "
        "risk_class TEXT NOT NULL DEFAULT 'NORMAL', "
        "external_actions_policy TEXT NOT NULL DEFAULT 'ALLOWED_WITH_GATE', "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "idempotency_key TEXT UNIQUE)")
    conn.execute(_OLD_SUPERVISOR_JOBS_DDL)
    if b1:
        for col, ddl in _B1_COLUMNS:
            conn.execute(f"ALTER TABLE supervisor_jobs ADD COLUMN {col} {ddl}")
    conn.execute(
        "INSERT INTO tasks (id, project_id, title, state, source, source_class, "
        "created_at, updated_at) VALUES ('t1', 'p1', 'x', 'NEW', 'owner', "
        "'OWNER', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')")
    conn.execute(
        "INSERT INTO supervisor_jobs (id, task_id, status, workflow_state, "
        "result_status, result_consumed, recovery_state, next_action, "
        "last_progress_at, facts_version, created_at, updated_at) VALUES "
        "('j1', 't1', 'ACTIVE', 'NEW', 'NOT_OBSERVED', 0, 'NONE', 'NONE', "
        "'2026-01-01T00:00:00+00:00', 0, '2026-01-01T00:00:00+00:00', "
        "'2026-01-01T00:00:00+00:00')")
    conn.execute(
        "INSERT INTO supervisor_jobs (id, task_id, status, workflow_state, "
        "result_status, result_consumed, recovery_state, next_action, "
        "last_progress_at, terminal, facts_version, created_at, updated_at) VALUES "
        "('j2', 't1', 'TERMINAL', 'NEW', 'NOT_OBSERVED', 0, 'NONE', 'NONE', "
        "'2026-01-01T00:00:00+00:00', 'DONE', 0, "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')")
    conn.close()


def _schema_version(core) -> str:
    return core._store._conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()["value"]


def _job_cols(core) -> set:
    return {r[1] for r in core._store._conn.execute(
        "PRAGMA table_info(supervisor_jobs)")}


def test_fresh_db_lands_on_current_version_and_has_phase_b_tables(tmp_path):
    db = str(tmp_path / "fresh.db")
    core = Core(db)
    try:
        assert _schema_version(core) == SCHEMA_VERSION
        tables = {r[0] for r in core._store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"external_waits", "process_registry", "supervisor_jobs"} <= tables
        cols = _job_cols(core)
        for c in ("primary_state", "queue_reason", "priority",
                  "owner_instance_id", "lease_epoch", "lease_expires_at",
                  "next_eligible_at", "error_class", "wait_kind",
                  "canonical_worktree_path", "writer_dispatch_id",
                  "writer_binding_mode"):
            assert c in cols, f"missing column {c}"
    finally:
        core.close()


def test_migrate_pre_b1_adds_b1_and_b3_preserves_rows(tmp_path):
    db = str(tmp_path / "v6.db")
    _build_legacy_db(db, b1=False)
    core = Core(db)
    try:
        assert _schema_version(core) == SCHEMA_VERSION
        cols = _job_cols(core)
        # B1 queue/lease columns + B3 worktree/writer columns all present.
        for c in ("primary_state", "queue_reason", "priority",
                  "owner_instance_id", "lease_epoch", "lease_expires_at",
                  "next_eligible_at", "error_class", "wait_kind",
                  "canonical_worktree_path", "writer_dispatch_id",
                  "writer_lease_epoch", "writer_binding_mode"):
            assert c in cols, f"missing migrated column {c}"
        # Correct backfill: legacy ACTIVE (no lease) -> QUEUED; DONE stays DONE.
        assert core._store.get_supervisor_job("j1")["primary_state"] == "QUEUED"
        assert core._store.get_supervisor_job("j2")["primary_state"] == "DONE"
        # Rows preserved (no destructive migration).
        assert core._store.get_supervisor_job("j1")["id"] == "j1"
        assert core._store.get_supervisor_job("j2")["task_id"] == "t1"
    finally:
        core.close()


def test_migrate_b1_to_b3_adds_worktree_and_wait_tables(tmp_path):
    db = str(tmp_path / "v7.db")
    _build_legacy_db(db, b1=True)
    core = Core(db)
    try:
        assert _schema_version(core) == SCHEMA_VERSION
        cols = _job_cols(core)
        for c in ("canonical_worktree_path", "repo_identity", "base_commit",
                  "branch_identity", "writer_dispatch_id",
                  "writer_owner_instance_id", "writer_lease_epoch",
                  "writer_binding_mode", "expected_head", "current_head"):
            assert c in cols, f"missing B3 column {c}"
        tables = {r[0] for r in core._store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"external_waits", "process_registry"} <= tables
        # Pre-existing B1 lease fields were not clobbered.
        assert core._store.get_supervisor_job("j1")["lease_epoch"] == 0
        assert core._store.get_supervisor_job("j1")["primary_state"] == "QUEUED"
    finally:
        core.close()


def test_reopen_is_idempotent_and_deterministic(tmp_path):
    db = str(tmp_path / "v6.db")
    _build_legacy_db(db, b1=False)
    c1 = Core(db)
    v1 = _schema_version(c1)
    j1_before = c1._store.get_supervisor_job("j1")
    c1.close()

    # Reopening the already-migrated DB is a no-op (same version, same data).
    c2 = Core(db)
    try:
        assert _schema_version(c2) == v1 == SCHEMA_VERSION
        assert c2._store.get_supervisor_job("j1") == j1_before
        assert c2._store.get_supervisor_job("j1")["primary_state"] == "QUEUED"
        assert c2._store.get_supervisor_job("j2")["primary_state"] == "DONE"
    finally:
        c2.close()


def test_schema_version_is_deterministic_across_instances(tmp_path):
    dbs = [str(tmp_path / "a.db"), str(tmp_path / "b.db")]
    versions = []
    for db in dbs:
        core = Core(db)
        versions.append(_schema_version(core))
        core.close()
    # Two independent fresh DBs agree on the exact same deterministic version.
    assert versions == [SCHEMA_VERSION, SCHEMA_VERSION]
