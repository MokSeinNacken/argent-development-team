"""Phase I2 — schema migration 20 → 22 (additive integration_candidates table; SCHEMA_VERSION now 22 after I3-A).

Deterministic.  The ``integration_candidates`` table + indexes are additive;
an existing V20 database gains them idempotently on reopen, existing rows are
preserved, and reopening an already-migrated DB is a no-op.
"""

from __future__ import annotations

import pytest

from argent_core import Core, OWNER_SOURCE
from argent_core.store import SCHEMA_VERSION


def _schema_version(core) -> str:
    return core._store._conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()["value"]


def _tables(core) -> set:
    return {r[0] for r in core._store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _build_v20_db(path: str) -> None:
    """Build a V20 DB by dropping the I2 table/indexes and stamping v20."""
    core = Core(path)
    project = core.create_project("p", OWNER_SOURCE)
    core.create_task(project.id, "t", OWNER_SOURCE)
    conn = core._store._conn
    conn.execute("DROP INDEX IF EXISTS idx_integration_candidates_source_target")
    conn.execute("DROP INDEX IF EXISTS idx_integration_candidates_repo_target_pos")
    conn.execute("DROP INDEX IF EXISTS idx_integration_candidates_one_integrating")
    conn.execute("DROP TABLE IF EXISTS integration_candidates")
    conn.execute("UPDATE schema_meta SET value = '20' WHERE key = 'schema_version'")
    core.close()


def test_fresh_db_lands_on_v21(tmp_path):
    core = Core(str(tmp_path / "fresh.db"))
    try:
        assert _schema_version(core) == SCHEMA_VERSION
        assert SCHEMA_VERSION == "22"
        assert "integration_candidates" in _tables(core)
    finally:
        core.close()


def test_v20_to_v21_adds_table(tmp_path):
    db = str(tmp_path / "v20.db")
    _build_v20_db(db)
    core = Core(db)
    try:
        assert _schema_version(core) == SCHEMA_VERSION
        assert "integration_candidates" in _tables(core)
        # The candidate CHECK constraint is present (bounded states).
        cols = {r[1] for r in core._store._conn.execute(
            "PRAGMA table_info(integration_candidates)")}
        for c in ("id", "repository", "integration_target", "source_job_id",
                  "state", "queue_position", "revision", "holder_lease_epoch"):
            assert c in cols, f"missing column {c}"
    finally:
        core.close()


def test_migration_is_idempotent(tmp_path):
    db = str(tmp_path / "v20.db")
    _build_v20_db(db)
    core1 = Core(db)
    core1.close()
    core2 = Core(db)
    try:
        assert _schema_version(core2) == SCHEMA_VERSION
        assert "integration_candidates" in _tables(core2)
    finally:
        core2.close()


def test_candidate_state_check_constraint(tmp_path):
    import sqlite3

    core = Core(str(tmp_path / "c.db"))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            core._store._conn.execute(
                "INSERT INTO integration_candidates (id, repository, "
                "integration_target, source_job_id, state, queue_position, "
                "created_at, updated_at) VALUES "
                "('ic_x', 'r', 'main', 'job:x', 'BOGUS', 0, 't', 't')")
    finally:
        core.close()
