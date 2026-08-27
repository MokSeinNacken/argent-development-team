"""SQLite persistence tests (SPEC V1 chapter 5 + V1.1 11.5, test point 15)."""

import sqlite3

import pytest

from argent_core import Core, Project, Role, TaskState, OWNER_SOURCE
from argent_core.store import Store

REQUIRED_TABLES = {
    "projects", "tasks", "task_runs", "role_runs", "handoffs", "findings",
    "test_runs", "reviews", "decisions", "owner_approvals", "events",
    "action_executions", "schema_meta", "agent_dispatches",
    "agent_result_quarantine", "agent_context_snapshots",
}


def _raw(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_names(db_path):
    conn = _raw(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {r["name"] for r in rows}
    finally:
        conn.close()


def _schema_sql(db_path, kind):
    conn = _raw(db_path)
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type=?", (kind,)
        ).fetchall()
        return {r["name"]: r["sql"] for r in rows}
    finally:
        conn.close()


def test_all_required_tables_exist(db_path, core):
    assert REQUIRED_TABLES <= _table_names(db_path)


def test_schema_meta_version(db_path, core):
    conn = _raw(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row["value"] == "3"


def test_foreign_keys_enabled(core):
    row = core._store._conn.execute("PRAGMA foreign_keys").fetchone()
    assert row[0] == 1


def test_foreign_keys_enforced(core):
    with pytest.raises(sqlite3.IntegrityError):
        core._store._conn.execute(
            "INSERT INTO tasks (id, project_id, title, state, resume_state, "
            "source, source_class, created_at, updated_at) "
            "VALUES ('t1','nope','x','NEW',NULL,'owner:authenticated',"
            "'TRUSTED','2026','2026')"
        )


def test_reopen_persistence(db_path):
    c = Core(db_path)
    p = c.create_project("p", OWNER_SOURCE)
    t = c.create_task(p.id, "t", OWNER_SOURCE)
    c.start_role(t.id, Role.LEAD, "role:lead")
    c.transition(t.id, TaskState.PLANNING, "role:lead")
    c.close()

    c2 = Core(db_path)
    assert c2.queries.get_project(p.id).name == "p"
    assert c2.queries.get_task(t.id).state is TaskState.PLANNING
    c2.close()


def test_transaction_rollback(db_path):
    # White-box: the private transaction context rolls back on exception.
    s = Store(db_path)
    with pytest.raises(RuntimeError):
        with s._transaction():
            s._insert_project(Project(id="tmp-proj", name="tmp",
                                      created_at=s.now_iso()))
            raise RuntimeError("boom")
    assert s.get_project("tmp-proj") is None
    s.close()


def test_partial_unique_index_approvals_exists(db_path, core):
    idx = _schema_sql(db_path, "index")
    assert "idx_owner_approvals_unique_active" in idx
    assert "status IN ('pending', 'approved')" in idx["idx_owner_approvals_unique_active"]


def test_partial_unique_index_role_runs_exists(db_path, core):
    idx = _schema_sql(db_path, "index")
    assert "idx_role_runs_active" in idx
    assert "WHERE status = 'started'" in idx["idx_role_runs_active"]


def test_check_constraints_present(db_path, core):
    tables = _schema_sql(db_path, "table")
    assert "CHECK" in tables["tasks"]
    assert "CHECK" in tables["role_runs"]
    assert "CHECK" in tables["owner_approvals"]
    assert "CHECK" in tables["action_executions"]


def test_check_constraint_blocks_invalid_state(db_path, core):
    conn = _raw(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tasks (id, project_id, title, state, resume_state, "
                "source, source_class, created_at, updated_at) "
                "VALUES ('t1','p1','x','BOGUS',NULL,'owner:authenticated',"
                "'TRUSTED','2026','2026')"
            )
    finally:
        conn.close()


def test_partial_unique_index_role_runs_blocks_second_active(db_path, core, task):
    # The DB-level partial unique index is a backstop for single-active-role.
    core.start_role(task.id, Role.LEAD, "role:lead")
    conn = _raw(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO role_runs (id, task_id, role, status, started_at) "
                "VALUES ('dup', ?, 'qa', 'started', '2026')",
                (task.id,),
            )
    finally:
        conn.close()


def test_partial_unique_index_approvals_blocks_duplicate(db_path, core, task):
    core.start_role(task.id, Role.LEAD, "role:lead")
    core.request_action(task.id, "deploy_production", "prod", Role.LEAD, "role:lead")
    conn = _raw(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO owner_approvals (id, task_id, action, scope, status, "
                "requested_by, source_class, created_at, expires_at) "
                "VALUES ('dup', ?, 'deploy_production', 'prod', 'pending', "
                "'lead', 'TRUSTED', '2026', '2099')",
                (task.id,),
            )
    finally:
        conn.close()


def test_partial_unique_index_approvals_allows_different_scope(db_path, core, task):
    core.start_role(task.id, Role.LEAD, "role:lead")
    core.request_action(task.id, "deploy_production", "prod", Role.LEAD, "role:lead")
    # A different scope for the same action/task is allowed by the DB index.
    conn = _raw(db_path)
    try:
        conn.execute(
            "INSERT INTO owner_approvals (id, task_id, action, scope, status, "
            "requested_by, source_class, created_at, expires_at) "
            "VALUES ('d2', ?, 'deploy_production', 'staging', 'pending', "
            "'lead', 'TRUSTED', '2026', '2099')",
            (task.id,),
        )
        conn.commit()
    finally:
        conn.close()
    conn2 = _raw(db_path)
    try:
        n = conn2.execute("SELECT COUNT(*) FROM owner_approvals").fetchone()[0]
    finally:
        conn2.close()
    assert n == 2


def test_command_idempotency_has_args_hash(db_path, core):
    cols = _raw(db_path)
    try:
        rows = cols.execute("PRAGMA table_info(command_idempotency)").fetchall()
    finally:
        cols.close()
    names = {r["name"] for r in rows}
    assert "args_hash" in names


def test_task_idempotency_key_unique_column(core, project):
    t1 = core.create_task(project.id, "a", OWNER_SOURCE, idempotency_key="k-abc")
    assert core.queries.get_task(t1.id).idempotency_key == "k-abc"


def test_tasks_v3_columns_present(db_path, core):
    cols = _raw(db_path)
    try:
        names = {r[1] for r in cols.execute("PRAGMA table_info(tasks)")}
    finally:
        cols.close()
    assert {"description", "risk_class", "external_actions_policy"} <= names


def test_agent_dispatch_unique_indexes_exist(db_path, core):
    idx = _schema_sql(db_path, "index")
    assert "idx_agent_dispatches_unique" in idx
    assert "idx_agent_dispatches_active" in idx
    assert "idx_agent_dispatches_session" in idx
    assert "idx_agent_dispatches_run" in idx
    assert "status IN ('PENDING', 'RUNNING', 'RECOVERY_PENDING')" in idx["idx_agent_dispatches_active"]


def test_migration_from_v2_adds_columns(tmp_path):
    # Build a pre-V3 tasks table (V2 shape) without the new columns, then open
    # the Core and verify the migration adds them and sets schema version 3.
    db = str(tmp_path / "v2.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
        "title TEXT NOT NULL, state TEXT NOT NULL, resume_state TEXT, "
        "source TEXT NOT NULL, source_class TEXT NOT NULL, created_at TEXT "
        "NOT NULL, updated_at TEXT NOT NULL, idempotency_key TEXT UNIQUE)"
    )
    conn.execute(
        "INSERT INTO tasks (id, project_id, title, state, source, source_class, "
        "created_at, updated_at) VALUES ('t1', 'p1', 'x', 'NEW', "
        "'owner:authenticated', 'TRUSTED', '2026', '2026')"
    )
    conn.commit()
    conn.close()

    c = Core(db)
    cols = {r[1] for r in c._store._conn.execute("PRAGMA table_info(tasks)")}
    assert {"description", "risk_class", "external_actions_policy"} <= cols
    row = c._store._conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    assert row is not None and row["value"] == "3"
    # The migrated task still exists and gained the defaults.
    t = c.queries.get_task("t1")
    assert t.risk_class.value == "NORMAL"
    assert t.external_actions_policy.value == "ALLOWED_WITH_GATE"
    c.close()
