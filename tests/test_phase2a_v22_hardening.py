"""SPEC V2.2 hardening regression tests (F1–F8, section 16).

Each test reproduces one verified finding from the independent Sol
implementation review; the fixes are documented in SPEC V2.2 16.1–16.8.
"""

import json
import sqlite3

import pytest

from argent_core import (
    Core,
    DispatchError,
    DispatchStatus,
    OutputValidationError,
    PrivacyViolation,
    RiskClass,
    Role,
    RolePolicyViolation,
    RoleRunStatus,
    SequenceKind,
    TaskRunStatus,
    TaskState,
    OWNER_SOURCE,
    role_source,
)
from argent_core.outputs import validate_role_output

from conftest import LEAD, events_of
from mock_runtime import (
    MockRuntime,
    build_output,
    lead_output,
    qa_output,
    reviewer_output,
)
from phase2a_helpers import (
    orchestrated_task,
    receive_valid,
    run_role,
    start_and_dispatch,
)

OWNER = OWNER_SOURCE

_STANDARD = [
    Role.LEAD,
    Role.ANALYST,
    Role.LEAD,
    Role.IMPLEMENTER,
    Role.QA,
    Role.REVIEWER,
    Role.LEAD,
]


def _drive_to_implementer(core, runtime, task, task_run):
    run_role(core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD)
    run_role(core, runtime, task, task_run, Role.ANALYST, 1, 1, SequenceKind.STANDARD)
    run_role(core, runtime, task, task_run, Role.LEAD, 1, 2, SequenceKind.STANDARD)
    core.start_role(task.id, Role.IMPLEMENTER, LEAD)


# ---------------------------------------------------------------------------
# F1 — orchestration drives the authoritative state machine
# ---------------------------------------------------------------------------

def test_f1_standard_workflow_task_states(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    expected = [
        TaskState.PLANNING,
        TaskState.ANALYZING,
        TaskState.LEAD_DECISION,
        TaskState.IMPLEMENTING,
        TaskState.TESTING,
        TaskState.REVIEWING,
        TaskState.DONE,
    ]
    for pos, role in enumerate(_STANDARD):
        run_role(core, runtime, task, task_run, role, 1, pos, SequenceKind.STANDARD)
        assert core.queries.get_task(task.id).state is expected[pos]
    # Final DONE + task.completed event.
    assert core.queries.get_task(task.id).state is TaskState.DONE
    assert len(events_of(core, "task.completed", task.id)) == 1


def test_f1_rework_workflow_task_states(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    run_role(core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD)
    assert core.queries.get_task(task.id).state is TaskState.PLANNING
    run_role(core, runtime, task, task_run, Role.ANALYST, 1, 1, SequenceKind.STANDARD)
    assert core.queries.get_task(task.id).state is TaskState.ANALYZING
    run_role(
        core, runtime, task, task_run, Role.LEAD, 1, 2, SequenceKind.STANDARD,
        decision="rework", rework_include_reviewer=True,
    )
    # LEAD_DECISION -> REWORK (new transition).
    assert core.queries.get_task(task.id).state is TaskState.REWORK
    # Rework cycle (with reviewer): lead(pos0, no change) -> implementer -> qa
    # -> reviewer -> lead(accept -> DONE).
    run_role(core, runtime, task, task_run, Role.LEAD, 2, 0, SequenceKind.REWORK)
    assert core.queries.get_task(task.id).state is TaskState.REWORK
    run_role(core, runtime, task, task_run, Role.IMPLEMENTER, 2, 1, SequenceKind.REWORK)
    assert core.queries.get_task(task.id).state is TaskState.IMPLEMENTING
    run_role(core, runtime, task, task_run, Role.QA, 2, 2, SequenceKind.REWORK)
    assert core.queries.get_task(task.id).state is TaskState.TESTING
    run_role(core, runtime, task, task_run, Role.REVIEWER, 2, 3, SequenceKind.REWORK)
    assert core.queries.get_task(task.id).state is TaskState.REVIEWING
    run_role(core, runtime, task, task_run, Role.LEAD, 2, 4, SequenceKind.REWORK)
    assert core.queries.get_task(task.id).state is TaskState.DONE
    assert len(events_of(core, "task.completed", task.id)) == 1


def test_f1_rework_without_reviewer_final_lead_done(core):
    # Reviewer-excluded rework: after qa the task is TESTING; the final lead
    # must still reach FINAL_DECISION (via REVIEWING) and then DONE.
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    run_role(core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD)
    run_role(core, runtime, task, task_run, Role.ANALYST, 1, 1, SequenceKind.STANDARD)
    run_role(
        core, runtime, task, task_run, Role.LEAD, 1, 2, SequenceKind.STANDARD,
        decision="rework",  # no toggle, no findings -> reviewer excluded
    )
    run_role(core, runtime, task, task_run, Role.LEAD, 2, 0, SequenceKind.REWORK)
    run_role(core, runtime, task, task_run, Role.IMPLEMENTER, 2, 1, SequenceKind.REWORK)
    run_role(core, runtime, task, task_run, Role.QA, 2, 2, SequenceKind.REWORK)
    assert core.queries.get_task(task.id).state is TaskState.TESTING
    run_role(core, runtime, task, task_run, Role.LEAD, 2, 3, SequenceKind.REWORK)
    assert core.queries.get_task(task.id).state is TaskState.DONE


def test_f1_state_machine_two_new_transitions():
    from argent_core import is_allowed

    assert is_allowed(TaskState.LEAD_DECISION, TaskState.REWORK) is True
    assert is_allowed(TaskState.REWORK, TaskState.IMPLEMENTING) is True


# ---------------------------------------------------------------------------
# F2 — pre-existing RECOVERY_PENDING dispatches stay unresolved
# ---------------------------------------------------------------------------

def test_f2_twice_recover_keeps_runs_started(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = start_and_dispatch(
        core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD
    )
    core.recover(OWNER)
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.RECOVERY_PENDING
    # A second recover() must keep the role run and task run STARTED.
    core.recover(OWNER)
    active = core.queries.get_active_role_run(task.id)
    assert active is not None and active.status is RoleRunStatus.STARTED
    tr = core.queries.get_task_run(task_run.id)
    assert tr.status is TaskRunStatus.STARTED
    # The legitimate result is still consumable afterwards.
    res = receive_valid(core, runtime, d, session, run, task.id, Role.LEAD)
    assert res.status == "consumed"
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED


# ---------------------------------------------------------------------------
# F3 — spawn-before-bind crash is reconcilable
# ---------------------------------------------------------------------------

def test_f3_spawn_before_bind_e2e(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    _drive_to_implementer(core, runtime, task, task_run)
    d = core.create_dispatch(
        task.id, task_run.id, Role.IMPLEMENTER, 3, 1, SequenceKind.STANDARD, None, LEAD
    )
    assert d.status is DispatchStatus.PENDING
    # Crash before bind: recover turns the write-role PENDING into
    # RECOVERY_PENDING (never auto-failed).
    core.recover(OWNER)
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.RECOVERY_PENDING
    # Bind with the real spawn returns -> RUNNING.
    session, run = runtime.spawn()
    d2 = core.bind_spawn_result(
        d.id, session, run, d.expected_agent_class, d.expected_model_class, "medium", LEAD
    )
    assert d2.status is DispatchStatus.RUNNING
    # The result is now consumable.
    out = build_output(Role.IMPLEMENTER, task.id, d.id)
    em = runtime.completion_event(task.id, session, run)
    res = core.receive_agent_result(d.id, em, out, LEAD)
    assert res.status == "consumed"
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED


# ---------------------------------------------------------------------------
# F4 — provenance fully mandatory (thinking tier, exact bind, parent, event_meta)
# ---------------------------------------------------------------------------

def test_f4_flash_dispatch_bound_with_pro_rejected(tmp_path):
    c = Core(str(tmp_path / "f4.db"))
    project = c.create_project("p", OWNER)
    task = c.create_task(project.id, "t", OWNER, risk_class=RiskClass.LOW)
    task_run = c.start_task_run(task.id, OWNER)
    runtime = MockRuntime()
    _drive_to_implementer(c, runtime, task, task_run)
    d = c.create_dispatch(
        task.id, task_run.id, Role.IMPLEMENTER, 3, 1, SequenceKind.STANDARD,
        {"provider": "deepseek", "model": "deepseek-v4-flash", "thinking_tier": "medium"},
        LEAD,
    )
    assert d.expected_model_class == "deepseek-v4-flash"
    # Pro is policy-allowed for implementer, but is an exact mismatch with the
    # persisted Flash expectation -> REJECTED, not a hanging RUNNING dispatch.
    with pytest.raises(RolePolicyViolation):
        c.bind_spawn_result(
            d.id, "s", "r", "deepseek", "deepseek-v4-pro", "medium", LEAD
        )
    assert c.queries.get_dispatch(d.id).status is DispatchStatus.REJECTED
    c.close()


def test_f4_thinking_tier_mismatch_rejected(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    _drive_to_implementer(core, runtime, task, task_run)
    d = core.create_dispatch(
        task.id, task_run.id, Role.IMPLEMENTER, 3, 1, SequenceKind.STANDARD, None, LEAD
    )
    assert d.expected_thinking_tier == "medium"
    # Implementer policy does not check thinking, so only the exact-equality
    # check catches this -> REJECTED.
    with pytest.raises(RolePolicyViolation):
        core.bind_spawn_result(
            d.id, "s", "r", "deepseek", "deepseek-v4-pro", "high", LEAD
        )
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.REJECTED


def test_f4_missing_event_meta_fields_rejected(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = start_and_dispatch(
        core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD
    )
    out = build_output(Role.LEAD, task.id, d.id)

    em = runtime.completion_event(task.id, session, run)
    del em["parent_dispatch_id"]
    res = core.receive_agent_result(d.id, em, out, LEAD)
    assert res.status == "rejected" and res.reason == "missing_metadata"

    em2 = runtime.completion_event(task.id, session, run)
    del em2["event_type"]
    res2 = core.receive_agent_result(d.id, em2, out, LEAD)
    assert res2.status == "rejected" and res2.reason == "missing_metadata"

    em3 = runtime.completion_event(task.id, session, run, status="failed")
    res3 = core.receive_agent_result(d.id, em3, out, LEAD)
    assert res3.status == "rejected" and res3.reason == "invalid_status"


def test_f4_parent_dispatch_required(core):
    task, task_run = orchestrated_task(core)
    core.start_role(task.id, Role.LEAD, LEAD)
    # A non-existent parent must be rejected.
    with pytest.raises(DispatchError):
        core.create_dispatch(
            task.id, task_run.id, Role.LEAD, 0, 1, SequenceKind.STANDARD, None, LEAD,
            parent_dispatch_id="does-not-exist",
        )
    # None (controller parent) is valid; the thinking tier is persisted.
    d = core.create_dispatch(
        task.id, task_run.id, Role.LEAD, 0, 1, SequenceKind.STANDARD, None, LEAD
    )
    assert d.parent_dispatch_id is None
    assert d.expected_thinking_tier == "high"


# ---------------------------------------------------------------------------
# F5 — context snapshots immutable, dispatch-bound, privacy-safe
# ---------------------------------------------------------------------------

def _lead_dispatch(core, task):
    core.start_role(task.id, Role.LEAD, LEAD)
    task_run = core.queries.get_latest_task_run(task.id)
    return core.create_dispatch(
        task.id, task_run.id, Role.LEAD, 0, 1, SequenceKind.STANDARD, None, LEAD
    )


def test_f5_repo_summary_denylisted_rejected(core):
    task, _ = orchestrated_task(core)
    d = _lead_dispatch(core, task)
    with pytest.raises(PrivacyViolation):
        core.snapshot_agent_context(
            d.id, Role.LEAD, 0, {"summary": "the full diff of source code"}, LEAD
        )


def test_f5_repo_summary_unknown_field_rejected(core):
    task, _ = orchestrated_task(core)
    d = _lead_dispatch(core, task)
    with pytest.raises(DispatchError):
        core.snapshot_agent_context(d.id, Role.LEAD, 0, {"sneaky": "x"}, LEAD)


def test_f5_snapshot_role_position_must_match_dispatch(core):
    task, _ = orchestrated_task(core)
    d = _lead_dispatch(core, task)
    with pytest.raises(DispatchError):
        core.snapshot_agent_context(d.id, Role.ANALYST, 0, {}, LEAD)
    with pytest.raises(DispatchError):
        core.snapshot_agent_context(d.id, Role.LEAD, 1, {}, LEAD)


def test_f5_second_differing_snapshot_rejected(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    # Drive to analyst: the analyst context embeds repo_summary (repo_state),
    # so varying it changes the snapshot content deterministically.
    run_role(core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD)
    core.start_role(task.id, Role.ANALYST, LEAD)
    d = core.create_dispatch(
        task.id, task_run.id, Role.ANALYST, 1, 1, SequenceKind.STANDARD, None, LEAD
    )
    core.snapshot_agent_context(d.id, Role.ANALYST, 1, {"summary": "one"}, LEAD)
    with pytest.raises(DispatchError):
        core.snapshot_agent_context(d.id, Role.ANALYST, 1, {"summary": "two"}, LEAD)
    # Same content is idempotent (no error).
    core.snapshot_agent_context(d.id, Role.ANALYST, 1, {"summary": "one"}, LEAD)


# ---------------------------------------------------------------------------
# F6 — nested role-output validation before the CAS
# ---------------------------------------------------------------------------

def test_f6_findings_non_dict_rejected():
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.LEAD, lead_output("t", "d", findings=["nope"]))


def test_f6_findings_bad_severity_rejected():
    out = lead_output("t", "d", findings=[{"severity": "extreme", "description": "x"}])
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.LEAD, out)


def test_f6_findings_unknown_key_rejected():
    out = lead_output("t", "d", findings=[{"severity": "low", "sneaky": "x"}])
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.LEAD, out)


def test_f6_string_list_non_str_rejected():
    out = lead_output("t", "d", concerns=[123])
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.LEAD, out)


def test_f6_tests_bad_result_rejected():
    out = qa_output("t", "d", tests=[{"name": "a", "result": "weird"}])
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.QA, out)


def test_f6_tests_missing_name_rejected():
    out = qa_output("t", "d", tests=[{"result": "passed"}])
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.QA, out)


def test_f6_reviewer_severity_enum_rejected():
    out = reviewer_output("t", "d", severity="extreme")
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.REVIEWER, out)


def test_f6_security_findings_unknown_key_rejected():
    out = reviewer_output(
        "t", "d", security_findings=[{"severity": "low", "extra": "x"}]
    )
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.REVIEWER, out)


def test_f6_nested_invalid_output_rejected_via_receive(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = start_and_dispatch(
        core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD
    )
    out = lead_output(
        task.id, d.id, findings=[{"severity": "extreme", "description": "x"}]
    )
    em = runtime.completion_event(task.id, session, run)
    res = core.receive_agent_result(d.id, em, out, LEAD)
    assert res.status == "rejected" and res.reason == "malformed_output"
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.REJECTED


# ---------------------------------------------------------------------------
# F7 — quarantine metadata values validated and bounded
# ---------------------------------------------------------------------------

def test_f7_sanitize_non_serializable_value():
    meta = Core._sanitize_event_meta(
        {"child_session_id": {"k": object()}, "run_id": "r",
         "event_type": "e", "status": "completed"}
    )
    json.dumps(meta)  # must not raise
    assert isinstance(meta["session_key"], str)


def test_f7_sanitize_oversized_value_truncated():
    big = "x" * 1000
    meta = Core._sanitize_event_meta(
        {"child_session_id": big, "run_id": "r", "event_type": "e", "status": "completed"}
    )
    assert len(meta["session_key"]) == 512


def test_f7_sanitize_denylisted_value_redacted():
    meta = Core._sanitize_event_meta(
        {"child_session_id": "secret-token", "run_id": "r",
         "event_type": "e", "status": "completed"}
    )
    assert meta["session_key"].startswith("<redacted:")


def test_f7_quarantine_with_denylisted_value_redacted(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = start_and_dispatch(
        core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD
    )
    em = runtime.completion_event(task.id, "secret-token", run)
    out = build_output(Role.LEAD, task.id, d.id)
    res = core.receive_agent_result(d.id, em, out, LEAD)
    assert res.status == "rejected"  # session_mismatch -> quarantined
    q = core.quarantine_log(LEAD, task.id)[0]
    meta = json.loads(q.event_meta_json)
    assert meta["session_key"].startswith("<redacted:")


def test_f7_quarantine_non_serializable_value_json_safe(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = start_and_dispatch(
        core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD
    )
    em = runtime.completion_event(task.id, session, run)
    em["child_session_id"] = {"bad": object()}
    out = build_output(Role.LEAD, task.id, d.id)
    res = core.receive_agent_result(d.id, em, out, LEAD)
    assert res.status == "rejected"  # session_mismatch -> quarantined
    q = core.quarantine_log(LEAD, task.id)[0]
    meta = json.loads(q.event_meta_json)  # must be valid JSON
    assert isinstance(meta["session_key"], str)


# ---------------------------------------------------------------------------
# F8 — transactional V2 -> V3 migration with version UPSERT
# ---------------------------------------------------------------------------

def test_f8_realistic_v2_to_v3_migration(tmp_path):
    db = str(tmp_path / "v2.db")
    conn = sqlite3.connect(db)

    # schema_meta already present at version 2 (the F8 regression).
    conn.execute(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('schema_version', '2')"
    )

    # V2 tasks table (no description/risk_class/external_actions_policy).
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

    # V2 agent_dispatches table (no expected_thinking_tier).
    conn.execute(
        "CREATE TABLE agent_dispatches (id TEXT PRIMARY KEY, task_id TEXT NOT "
        "NULL, task_run_id TEXT NOT NULL, role TEXT NOT NULL, "
        "parent_dispatch_id TEXT, expected_agent_class TEXT NOT NULL, "
        "expected_model_class TEXT NOT NULL, child_session_id TEXT, "
        "openclaw_run_id TEXT, actual_provider TEXT, actual_model TEXT, "
        "thinking_tier TEXT, status TEXT NOT NULL, cycle_no INTEGER NOT NULL "
        "DEFAULT 1, position INTEGER NOT NULL, sequence_kind TEXT NOT NULL, "
        "attempt_no INTEGER NOT NULL DEFAULT 1, handoff_id TEXT, result_json "
        "TEXT, created_at TEXT NOT NULL, started_at TEXT, consumed_at TEXT)"
    )
    conn.execute(
        "INSERT INTO agent_dispatches (id, task_id, task_run_id, role, "
        "expected_agent_class, expected_model_class, status, cycle_no, position, "
        "sequence_kind, attempt_no, created_at) VALUES ('d1', 't1', 'r1', 'lead', "
        "'openai', 'gpt-5.6-sol', 'PENDING', 1, 0, 'STANDARD', 1, '2026')"
    )
    conn.commit()
    conn.close()

    c = Core(db)
    # New columns present.
    tcols = {r[1] for r in c._store._conn.execute("PRAGMA table_info(tasks)")}
    assert {"description", "risk_class", "external_actions_policy"} <= tcols
    dcols = {r[1] for r in c._store._conn.execute("PRAGMA table_info(agent_dispatches)")}
    assert "expected_thinking_tier" in dcols
    # Version UPSERTed 2 -> 5.
    row = c._store._conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    assert row is not None and row["value"] == "5"
    # Existing data intact.
    t = c.queries.get_task("t1")
    assert t.title == "x" and t.state is TaskState.NEW
    assert t.risk_class is RiskClass.NORMAL
    d = c.queries.get_dispatch("d1")
    assert d.status is DispatchStatus.PENDING
    assert d.expected_model_class == "gpt-5.6-sol"
    assert d.expected_thinking_tier == "medium"  # migrated default
    c.close()
