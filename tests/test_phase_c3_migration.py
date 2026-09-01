"""Phase C3 — schema migration (J).  Deterministic.

V10 → V11 is additive, idempotent and non-destructive: the three bounded
``last_recovery_decision`` / ``last_failure_class`` / ``last_recovery_at`` audit
columns are added to an existing V10 ``supervisor_jobs`` table, existing rows
are preserved (NULL backfill), and reopening an already-migrated DB is a no-op.
"""

from __future__ import annotations

from argent_core import Core, OWNER_SOURCE
from argent_core.store import SCHEMA_VERSION
from argent_core.supervisor import Supervisor
from mock_supervisor_runtime import FakeRunLauncher, FakeRunStatusProvider

_C3_COLUMNS = ("last_recovery_decision", "last_failure_class", "last_recovery_at")


def _schema_version(core) -> str:
    return core._store._conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()["value"]


def _job_cols(core) -> set:
    return {r[1] for r in core._store._conn.execute(
        "PRAGMA table_info(supervisor_jobs)")}


def _build_v10_db(path: str) -> str:
    """Build a V10 DB (fresh V11 with the C3 columns dropped + a job row)."""
    core = Core(path)
    project = core.create_project("p", OWNER_SOURCE)
    task = core.create_task(project.id, "t", OWNER_SOURCE)
    core.start_task_run(task.id, OWNER_SOURCE)
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher())
    job = sup.store.create_job(task.id, idempotency_key="job-1")
    jid = job.supervisor_job_id
    conn = core._store._conn
    for col in _C3_COLUMNS:
        conn.execute(f"ALTER TABLE supervisor_jobs DROP COLUMN {col}")
    conn.execute("UPDATE schema_meta SET value = '10' WHERE key = 'schema_version'")
    core.close()
    return jid


def test_fresh_db_lands_on_v11_with_c3_columns(tmp_path):
    core = Core(str(tmp_path / "fresh.db"))
    try:
        assert _schema_version(core) == "11"
        assert _schema_version(core) == SCHEMA_VERSION
        cols = _job_cols(core)
        for c in _C3_COLUMNS:
            assert c in cols, f"missing column {c}"
    finally:
        core.close()


def test_v10_to_v11_adds_columns_and_preserves_rows(tmp_path):
    db = str(tmp_path / "v10.db")
    jid = _build_v10_db(db)
    core = Core(db)
    try:
        assert _schema_version(core) == "11"
        cols = _job_cols(core)
        for c in _C3_COLUMNS:
            assert c in cols, f"missing migrated column {c}"
        # Non-destructive: the pre-existing job row survives, with NULL backfill.
        row = core._store.get_supervisor_job(jid)
        assert row["id"] == jid
        assert row["last_recovery_decision"] is None
        assert row["last_failure_class"] is None
        assert row["last_recovery_at"] is None
    finally:
        core.close()


def test_migration_is_idempotent(tmp_path):
    db = str(tmp_path / "v10.db")
    _build_v10_db(db)
    c1 = Core(db)
    v1 = _schema_version(c1)
    row_before = c1._store.get_supervisor_job(
        c1._store.list_supervisor_jobs()[0]["id"])
    c1.close()

    c2 = Core(db)
    try:
        assert _schema_version(c2) == v1 == "11"
        assert c2._store.get_supervisor_job(row_before["id"]) == row_before
    finally:
        c2.close()


def test_migration_is_deterministic_across_instances(tmp_path):
    versions = []
    for name in ("a.db", "b.db"):
        core = Core(str(tmp_path / name))
        versions.append(_schema_version(core))
        core.close()
    assert versions == [SCHEMA_VERSION, SCHEMA_VERSION]
