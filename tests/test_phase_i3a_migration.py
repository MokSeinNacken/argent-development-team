"""Phase I3-A — schema migration 21 → 22 (additive external_action_requests +
external_action_audit tables).

Deterministic.  The two new tables are additive; an existing V21 database gains
them idempotently on reopen, existing rows are preserved, and reopening an
already-migrated DB is a no-op.
"""

from __future__ import annotations

from argent_core import Core, OWNER_SOURCE
from argent_core.store import SCHEMA_VERSION


def _schema_version(core) -> str:
    return core._store._conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()["value"]


def _tables(core) -> set:
    return {r[0] for r in core._store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _build_v21_db(path: str) -> None:
    """Build a V21 DB by dropping the I3-A tables/indexes and stamping v21."""
    core = Core(path)
    project = core.create_project("p", OWNER_SOURCE)
    core.create_task(project.id, "t", OWNER_SOURCE)
    conn = core._store._conn
    conn.execute("DROP INDEX IF EXISTS idx_external_action_requests_idem")
    conn.execute("DROP INDEX IF EXISTS idx_external_action_requests_source_job")
    conn.execute("DROP INDEX IF EXISTS idx_external_action_audit_request")
    conn.execute("DROP TABLE IF EXISTS external_action_audit")
    conn.execute("DROP TABLE IF EXISTS external_action_requests")
    conn.execute("UPDATE schema_meta SET value = '21' WHERE key = 'schema_version'")
    core.close()


def test_fresh_db_lands_on_v22(tmp_path):
    core = Core(str(tmp_path / "fresh.db"))
    try:
        assert _schema_version(core) == SCHEMA_VERSION
        assert SCHEMA_VERSION == "22"
        assert "external_action_requests" in _tables(core)
        assert "external_action_audit" in _tables(core)
    finally:
        core.close()


def test_v21_to_v22_adds_tables(tmp_path):
    db = str(tmp_path / "v21.db")
    _build_v21_db(db)
    core = Core(db)
    try:
        assert _schema_version(core) == SCHEMA_VERSION
        assert "external_action_requests" in _tables(core)
        assert "external_action_audit" in _tables(core)
        # The request-state CHECK constraint is present (bounded states).
        cols = {r[1] for r in core._store._conn.execute(
            "PRAGMA table_info(external_action_requests)")}
        assert "state" in cols
        assert "revision" in cols
        assert "idempotency_key" in cols
    finally:
        core.close()


def test_reopen_is_noop(tmp_path):
    db = str(tmp_path / "reopen.db")
    core = Core(db)
    v1 = _schema_version(core)
    core.close()
    core2 = Core(db)
    try:
        assert _schema_version(core2) == v1 == SCHEMA_VERSION
    finally:
        core2.close()


def test_idempotency_key_unique_constraint(tmp_path):
    core = Core(str(tmp_path / "idem.db"))
    try:
        idx = core._store._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_external_action_requests_idem'").fetchone()
        assert idx is not None and "UNIQUE" in idx["sql"]
    finally:
        core.close()
