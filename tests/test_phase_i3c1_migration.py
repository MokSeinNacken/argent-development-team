"""Phase I3-C1 — schema migration 22 → 23 (additive ci_policy/ci_evidence
columns on external_waits).

Deterministic.  The two new columns are additive + nullable; an existing V22
database gains them idempotently on reopen, existing rows are preserved, and a
fresh database already carries them.  Reopening an already-migrated DB is a
no-op.
"""

from __future__ import annotations

from argent_core import Core, OWNER_SOURCE
from argent_core.store import SCHEMA_VERSION


def _schema_version(core) -> str:
    return core._store._conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()["value"]


def _ewait_cols(core) -> set:
    return {r[1] for r in core._store._conn.execute(
        "PRAGMA table_info(external_waits)")}


def test_fresh_db_lands_on_v23(tmp_path):
    core = Core(str(tmp_path / "fresh.db"))
    try:
        assert _schema_version(core) == SCHEMA_VERSION
        assert SCHEMA_VERSION == "23"
        assert {"ci_policy", "ci_evidence"} <= _ewait_cols(core)
    finally:
        core.close()


def test_v22_to_v23_adds_columns(tmp_path):
    db = str(tmp_path / "v22.db")
    core = Core(db)
    conn = core._store._conn
    conn.execute("ALTER TABLE external_waits DROP COLUMN ci_policy")
    conn.execute("ALTER TABLE external_waits DROP COLUMN ci_evidence")
    conn.execute("UPDATE schema_meta SET value = '22' WHERE key = 'schema_version'")
    core.close()

    core2 = Core(db)
    try:
        assert _schema_version(core2) == SCHEMA_VERSION
        assert {"ci_policy", "ci_evidence"} <= _ewait_cols(core2)
    finally:
        core2.close()


def test_existing_rows_preserved_on_migration(tmp_path):
    db = str(tmp_path / "preserve.db")
    core = Core(db)
    project = core.create_project("p", OWNER_SOURCE)
    core.create_task(project.id, "t", OWNER_SOURCE)
    core.close()

    # Simulate a pre-migration v22 DB: drop the CI columns, reopen.
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE external_waits DROP COLUMN ci_policy")
    conn.execute("ALTER TABLE external_waits DROP COLUMN ci_evidence")
    conn.execute("UPDATE schema_meta SET value = '22' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    core2 = Core(db)
    try:
        assert _schema_version(core2) == "23"
        # The task rows from before the migration are still present.
        assert core2._store.list_tasks()
    finally:
        core2.close()


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
