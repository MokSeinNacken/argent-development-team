"""SPEC V2.3 hardening regression tests (G1–G4, section 17).

Each test reproduces one verified finding from the independent Sol re-check;
the fixes are documented in SPEC V2.3 17.1–17.4.
"""

import json
import sqlite3
import threading

import pytest

from argent_core import (
    Core,
    DispatchError,
    DispatchStatus,
    OutputValidationError,
    Role,
    SequenceKind,
    OWNER_SOURCE,
)
from argent_core.outputs import validate_role_output
from argent_core.store import Store

from conftest import LEAD
from mock_runtime import MockRuntime, build_output, lead_output, reviewer_output
from phase2a_helpers import orchestrated_task, receive_valid, start_and_dispatch

OWNER = OWNER_SOURCE


# ---------------------------------------------------------------------------
# G1 — bind fully atomic, REJECTED via CAS (17.1)
# ---------------------------------------------------------------------------

def test_g1_mismatch_bind_cannot_overwrite_valid_bind(tmp_path):
    # Two independent connections to the same DB.  A valid bind on the first
    # connection leaves the dispatch RUNNING; a mismatched bind on the second
    # connection must be refused, never overwriting the valid bind (the
    # ghost-writer race).
    db = str(tmp_path / "g1.db")
    c1 = Core(db)
    runtime = MockRuntime()
    task, task_run = orchestrated_task(c1)
    d, session, run = start_and_dispatch(
        c1, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD
    )
    assert d.status is DispatchStatus.RUNNING

    c2 = Core(db)
    with pytest.raises(DispatchError):
        c2.bind_spawn_result(
            d.id, "forged-session", "forged-run",
            "deepseek", "deepseek-v4-pro", "medium", LEAD,
        )
    # The valid bind survives; no ghost-writer retry is possible.
    assert c2.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING
    c2.close()

    # The legitimate result is still consumable on the first connection.
    res = receive_valid(c1, runtime, d, session, run, task.id, Role.LEAD)
    assert res.status == "consumed"
    c1.close()


def test_g1_parallel_valid_vs_mismatch_bind(tmp_path):
    # Genuine parallel bind (two connections).  SQLite's BEGIN IMMEDIATE
    # serializes the writers, so exactly one bind wins; crucially a successful
    # valid bind is never downgraded to REJECTED by the concurrent mismatch.
    db = str(tmp_path / "g1_parallel.db")
    setup = Core(db)
    project = setup.create_project("p", OWNER)
    task = setup.create_task(project.id, "t", OWNER)
    task_run = setup.start_task_run(task.id, OWNER)
    setup.start_role(task.id, Role.LEAD, LEAD)
    d = setup.create_dispatch(
        task.id, task_run.id, Role.LEAD, 0, 1, SequenceKind.STANDARD, None, LEAD
    )
    setup.close()

    barrier = threading.Barrier(2)
    outcomes = {}

    def run_bind(key, session, run_id, provider, model, thinking):
        barrier.wait()
        c = Core(db)
        try:
            c.bind_spawn_result(
                d.id, session, run_id, provider, model, thinking, LEAD
            )
            outcomes[key] = "bound"
        except Exception as exc:  # noqa: BLE001 - record any outcome
            outcomes[key] = type(exc).__name__
        finally:
            c.close()

    t_valid = threading.Thread(
        target=run_bind,
        args=("valid", "v-s", "v-r", "openai", "gpt-5.6-sol", "high"),
    )
    t_mismatch = threading.Thread(
        target=run_bind,
        args=("mismatch", "m-s", "m-r", "deepseek", "deepseek-v4-pro", "medium"),
    )
    t_valid.start()
    t_mismatch.start()
    t_valid.join()
    t_mismatch.join()

    check = Core(db)
    final = check.queries.get_dispatch(d.id).status
    check.close()

    if outcomes["valid"] == "bound":
        # Valid won: it must remain RUNNING (never overwritten to REJECTED).
        assert final is DispatchStatus.RUNNING
        assert outcomes["mismatch"] == "DispatchError"
    else:
        # Mismatch won the race first: it rejected atomically; the valid bind
        # was refused (no ghost-writer retry possible).
        assert final is DispatchStatus.REJECTED
        assert outcomes["valid"] == "DispatchError"
        assert outcomes["mismatch"] == "RolePolicyViolation"


def test_g1_reject_cas_guards_running(core):
    # White-box CAS check: rejecting an already-RUNNING dispatch affects 0 rows.
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, _, _ = start_and_dispatch(
        core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD
    )
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING
    assert core._store._reject_dispatch_cas(d.id) == 0
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING


def test_g1_reject_cas_accepts_pending(core):
    task, task_run = orchestrated_task(core)
    core.start_role(task.id, Role.LEAD, LEAD)
    d = core.create_dispatch(
        task.id, task_run.id, Role.LEAD, 0, 1, SequenceKind.STANDARD, None, LEAD
    )
    assert d.status is DispatchStatus.PENDING
    assert core._store._reject_dispatch_cas(d.id) == 1
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.REJECTED


# ---------------------------------------------------------------------------
# G2 — nested element schemas fully enforced (17.2)
# ---------------------------------------------------------------------------

def test_g2_findings_missing_severity_rejected():
    out = lead_output("t", "d", findings=[{"description": "x"}])
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.LEAD, out)


def test_g2_findings_missing_description_and_title_rejected():
    out = lead_output("t", "d", findings=[{"severity": "low"}])
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.LEAD, out)


def test_g2_findings_empty_dict_rejected():
    out = lead_output("t", "d", findings=[{}])
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.LEAD, out)


def test_g2_findings_title_only_valid():
    out = lead_output("t", "d", findings=[{"severity": "low", "title": "T1"}])
    assert validate_role_output(Role.LEAD, out) is out


def test_g2_security_findings_missing_severity_rejected():
    out = reviewer_output("t", "d", security_findings=[{"description": "x"}])
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.REVIEWER, out)


def test_g2_security_findings_missing_description_rejected():
    out = reviewer_output("t", "d", security_findings=[{"severity": "low"}])
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.REVIEWER, out)


def test_g2_security_findings_empty_dict_rejected():
    out = reviewer_output("t", "d", security_findings=[{}])
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.REVIEWER, out)


def test_g2_architecture_findings_missing_severity_rejected():
    out = reviewer_output("t", "d", architecture_findings=[{"description": "x"}])
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.REVIEWER, out)


def test_g2_architecture_findings_missing_description_rejected():
    out = reviewer_output("t", "d", architecture_findings=[{"severity": "low"}])
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.REVIEWER, out)


def test_g2_architecture_findings_empty_dict_rejected():
    out = reviewer_output("t", "d", architecture_findings=[{}])
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.REVIEWER, out)


def test_g2_sec_arch_valid_dicts_pass():
    out = reviewer_output(
        "t", "d",
        security_findings=[{"severity": "high", "description": "sec issue"}],
        architecture_findings=[{"severity": "low", "description": "arch note"}],
    )
    assert validate_role_output(Role.REVIEWER, out) is out


def test_g2_title_fallback_in_apply_role_effects(core):
    # A title-only finding must persist its title as the finding description.
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = start_and_dispatch(
        core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD
    )
    out = build_output(
        Role.LEAD, task.id, d.id,
        findings=[{"severity": "low", "title": "T1"}],
    )
    em = runtime.completion_event(task.id, session, run)
    res = core.receive_agent_result(d.id, em, out, LEAD)
    assert res.status == "consumed"
    findings = core.queries.list_findings(task.id)
    assert len(findings) == 1
    assert findings[0].severity == "low"
    assert findings[0].description == "T1"


# ---------------------------------------------------------------------------
# G3 — schema creation + migration in ONE transaction (17.3)
# ---------------------------------------------------------------------------

def test_g3_migration_failure_rolls_back_v2_structure(tmp_path, monkeypatch):
    db = str(tmp_path / "g3.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('schema_version', '2')"
    )
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

    def failing_migrate(self):
        # Simulate a partial migration (one ALTER) followed by a crash.
        self._conn.execute("ALTER TABLE tasks ADD COLUMN description TEXT")
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(Store, "_migrate", failing_migrate)
    with pytest.raises(RuntimeError):
        Core(db)
    monkeypatch.undo()

    # The partial ALTER and all DDL (including newly created _SCHEMA tables)
    # were rolled back: V2 structure intact.
    conn = sqlite3.connect(db)
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "projects" not in tables  # _SCHEMA DDL rolled back with migration
    tcols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "description" not in tcols
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    assert row[0] == "2"
    conn.close()

    # A subsequent open succeeds and migrates to V3.
    c = Core(db)
    tcols2 = {r[1] for r in c._store._conn.execute("PRAGMA table_info(tasks)")}
    assert "description" in tcols2
    row2 = c._store._conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    assert row2["value"] == "3"
    c.close()


# ---------------------------------------------------------------------------
# G4 — quarantine values: None and failing __str__ fail-safe (17.4)
# ---------------------------------------------------------------------------

class _Unprintable:
    def __str__(self):
        raise RuntimeError("cannot stringify")


def test_g4_none_becomes_placeholder():
    meta = Core._sanitize_event_meta(
        {"child_session_id": None, "run_id": None, "event_type": None,
         "status": None}
    )
    assert meta == {
        "session_key": "<none>",
        "run_id": "<none>",
        "event_type": "<none>",
        "status": "<none>",
    }
    json.dumps(meta)  # must be serializable


def test_g4_unprintable_becomes_placeholder():
    meta = Core._sanitize_event_meta(
        {"child_session_id": _Unprintable(), "run_id": "r",
         "event_type": "e", "status": "completed"}
    )
    assert meta["session_key"] == "<unprintable>"
    json.dumps(meta)  # must be serializable


def test_g4_quarantine_none_value_serializable(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, _, run = start_and_dispatch(
        core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD
    )
    out = build_output(Role.LEAD, task.id, d.id)

    em = runtime.completion_event(task.id, None, run)
    res = core.receive_agent_result(d.id, em, out, LEAD)
    assert res.status == "rejected"

    rows = core.quarantine_log(LEAD, task.id)
    assert len(rows) == 1
    meta = json.loads(rows[0].event_meta_json)
    assert meta["session_key"] == "<none>"
