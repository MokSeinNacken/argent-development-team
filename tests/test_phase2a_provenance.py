"""Phase 2A provenance / result-validation tests (SPEC V2 3.3, 15.3/15.7/15.11)."""

import pytest

from argent_core import (
    DispatchStatus,
    OutputValidationError,
    Role,
    SequenceKind,
    TaskState,
)

from conftest import LEAD
from mock_runtime import MockRuntime
from phase2a_helpers import (
    orchestrated_task,
    receive_valid,
    start_and_dispatch,
)


def _bound_lead(core, runtime, task, task_run):
    return start_and_dispatch(
        core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD
    )


def test_wrong_task_id_rejected_and_quarantined(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _bound_lead(core, runtime, task, task_run)
    em = runtime.completion_event("wrong-task", session, run)
    out = {"role": "lead", "task_id": "wrong-task", "dispatch_id": d.id}
    res = core.receive_agent_result(d.id, em, out, LEAD)
    assert res.status == "rejected"
    assert res.reason == "task_mismatch"
    q = core.quarantine_log(LEAD, task.id)
    assert len(q) == 1 and q[0].reason == "task_mismatch"
    # Dispatch untouched (still RUNNING).
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING


def test_wrong_role_rejected(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _bound_lead(core, runtime, task, task_run)
    em = runtime.completion_event(task.id, session, run)
    out = {"role": "analyst", "task_id": task.id, "dispatch_id": d.id}
    res = core.receive_agent_result(d.id, em, out, LEAD)
    assert res.status == "rejected" and res.reason == "role_mismatch"


def test_wrong_dispatch_id_rejected(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _bound_lead(core, runtime, task, task_run)
    em = runtime.completion_event(task.id, session, run)
    out = {"role": "lead", "task_id": task.id, "dispatch_id": "other-dispatch"}
    res = core.receive_agent_result(d.id, em, out, LEAD)
    assert res.status == "rejected" and res.reason == "dispatch_mismatch"


def test_wrong_session_rejected(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _bound_lead(core, runtime, task, task_run)
    em = runtime.completion_event(task.id, "other-session", run)
    out = {"role": "lead", "task_id": task.id, "dispatch_id": d.id}
    res = core.receive_agent_result(d.id, em, out, LEAD)
    assert res.status == "rejected" and res.reason == "session_mismatch"


def test_wrong_run_id_rejected(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _bound_lead(core, runtime, task, task_run)
    em = runtime.completion_event(task.id, session, "other-run")
    out = {"role": "lead", "task_id": task.id, "dispatch_id": d.id}
    res = core.receive_agent_result(d.id, em, out, LEAD)
    assert res.status == "rejected" and res.reason == "run_id_mismatch"


def test_wrong_parent_rejected(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _bound_lead(core, runtime, task, task_run)
    em = runtime.completion_event(task.id, session, run)
    em["parent_dispatch_id"] = "forged-parent"
    out = {"role": "lead", "task_id": task.id, "dispatch_id": d.id}
    res = core.receive_agent_result(d.id, em, out, LEAD)
    assert res.status == "rejected" and res.reason == "parent_mismatch"


def test_duplicate_completion_idempotent(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _bound_lead(core, runtime, task, task_run)
    res1 = receive_valid(core, runtime, d, session, run, task.id, Role.LEAD)
    assert res1.status == "consumed"
    # Re-deliver the exact same result -> idempotent duplicate, no state change.
    res2 = receive_valid(core, runtime, d, session, run, task.id, Role.LEAD)
    assert res2.status == "duplicate"
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED
    # Only one lead decision was recorded.
    assert len(core.queries.list_decisions(task.id)) == 1


def test_stale_run_rejected(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _bound_lead(core, runtime, task, task_run)
    # Start a NEW task run; the old dispatch now references a stale run.
    core.start_task_run(task.id, LEAD)
    em = runtime.completion_event(task.id, session, run)
    out = {"role": "lead", "task_id": task.id, "dispatch_id": d.id}
    res = core.receive_agent_result(d.id, em, out, LEAD)
    assert res.status == "rejected" and res.reason == "stale_run"
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING


def test_stale_dispatch_rejected(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _bound_lead(core, runtime, task, task_run)
    # Mark it failed, then a late result must be quarantined as stale.
    core.mark_agent_failed(d.id, "timeout", LEAD)
    em = runtime.completion_event(task.id, session, run)
    out = {"role": "lead", "task_id": task.id, "dispatch_id": d.id}
    res = core.receive_agent_result(d.id, em, out, LEAD)
    assert res.status == "rejected" and res.reason == "stale_dispatch"


def test_pending_injection_rejected(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    core.start_role(task.id, Role.LEAD, LEAD)
    d = core.create_dispatch(
        task.id, task_run.id, Role.LEAD, 0, 1, SequenceKind.STANDARD, None, LEAD
    )
    # A result arrives before spawn/bind -> PENDING injection, rejected.
    em = runtime.completion_event(task.id, "s", "r")
    out = {"role": "lead", "task_id": task.id, "dispatch_id": d.id}
    res = core.receive_agent_result(d.id, em, out, LEAD)
    assert res.status == "rejected" and res.reason == "pending_injection"
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.PENDING


def test_missing_metadata_rejected(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _bound_lead(core, runtime, task, task_run)
    out = {"role": "lead", "task_id": task.id, "dispatch_id": d.id}
    res = core.receive_agent_result(d.id, {}, out, LEAD)  # no event metadata
    assert res.status == "rejected" and res.reason == "missing_metadata"


def test_unknown_dispatch_quarantined(core):
    runtime = MockRuntime()
    task, _ = orchestrated_task(core)
    em = runtime.completion_event(task.id, "s", "r")
    res = core.receive_agent_result("does-not-exist", em, {}, LEAD)
    assert res.status == "unknown" and res.reason == "dispatch_unknown"


def test_completion_after_task_end_rejected(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _bound_lead(core, runtime, task, task_run)
    # Move the task to a terminal state directly (white-box), keeping the
    # dispatch RUNNING and the role run active.
    core._store._conn.execute(
        "UPDATE tasks SET state='DONE', updated_at='x' WHERE id=?", (task.id,)
    )
    em = runtime.completion_event(task.id, session, run)
    out = {"role": "lead", "task_id": task.id, "dispatch_id": d.id}
    res = core.receive_agent_result(d.id, em, out, LEAD)
    assert res.status == "rejected" and res.reason == "task_ended"


def test_late_attempt_result_rejected(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _bound_lead(core, runtime, task, task_run)
    # Fail attempt 1, retry attempt 2; the attempt-1 result is then late.
    core.mark_agent_failed(d.id, "timeout", LEAD)
    core.start_role(task.id, Role.LEAD, LEAD)  # retry role run
    d2 = core.create_dispatch(
        task.id, task_run.id, Role.LEAD, 0, 1, SequenceKind.STANDARD, None, LEAD
    )
    assert d2.attempt_no == 2
    # Late result for attempt 1 -> stale (FAILED).
    em = runtime.completion_event(task.id, session, run)
    out = {"role": "lead", "task_id": task.id, "dispatch_id": d.id}
    res = core.receive_agent_result(d.id, em, out, LEAD)
    assert res.status == "rejected" and res.reason == "stale_dispatch"


def test_unexpected_completion_no_state_change(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _bound_lead(core, runtime, task, task_run)
    before = core.queries.get_dispatch(d.id).status
    # A Sol completion event for a different dispatch (unexpected) -> no change.
    em = runtime.completion_event(task.id, "unexpected-session", "unexpected-run")
    out = {"role": "lead", "task_id": task.id, "dispatch_id": "unexpected"}
    res = core.receive_agent_result(d.id, em, out, LEAD)
    assert res.status == "rejected"
    assert core.queries.get_dispatch(d.id).status is before
    assert core.queries.get_active_role_run(task.id) is not None


def test_legit_dispatch_after_forged_rejection_stays_running(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _bound_lead(core, runtime, task, task_run)
    # Forged result (wrong session) -> rejected, dispatch stays RUNNING.
    em = runtime.completion_event(task.id, "forged", run)
    out = {"role": "lead", "task_id": task.id, "dispatch_id": d.id}
    res = core.receive_agent_result(d.id, em, out, LEAD)
    assert res.status == "rejected" and res.reason == "session_mismatch"
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING
    # The legitimate result is still consumable.
    res2 = receive_valid(core, runtime, d, session, run, task.id, Role.LEAD)
    assert res2.status == "consumed"


def test_malformed_output_with_matching_identity_rejected(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _bound_lead(core, runtime, task, task_run)
    em = runtime.completion_event(task.id, session, run)
    # Matching identity but missing required lead fields -> malformed.
    out = {"role": "lead", "task_id": task.id, "dispatch_id": d.id}
    res = core.receive_agent_result(d.id, em, out, LEAD)
    assert res.status == "rejected" and res.reason == "malformed_output"
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.REJECTED


def test_agent_output_cannot_create_approval(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _bound_lead(core, runtime, task, task_run)
    # An output can only recommend; it never creates an approval.
    em = runtime.completion_event(task.id, session, run)
    out = {
        "role": "lead", "task_id": task.id, "dispatch_id": d.id,
        "status": "ok", "findings": [], "own_assessment": "x", "concerns": [],
        "proposal": "x", "alternatives": [], "confidence": 1.0, "blockers": [],
        "requested_next_state": "owner_gate", "decision": "request_owner_gate",
        "accepted_findings": [], "rejected_findings": [], "rationale": "y",
    }
    core.receive_agent_result(d.id, em, out, LEAD)
    assert core.queries.list_approvals(task.id) == []


def test_receive_requires_controller_source(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _bound_lead(core, runtime, task, task_run)
    with pytest.raises(Exception):
        core.receive_agent_result(d.id, {}, {}, "role:qa")


def test_quarantine_sanitizes_event_meta(core):
    import json

    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _bound_lead(core, runtime, task, task_run)
    em = runtime.completion_event(task.id, "forged", run)
    em["secret_stuff"] = "should-not-be-logged"
    out = {"role": "lead", "task_id": task.id, "dispatch_id": d.id}
    core.receive_agent_result(d.id, em, out, LEAD)
    q = core.quarantine_log(LEAD, task.id)[0]
    meta = json.loads(q.event_meta_json)
    assert set(meta.keys()) <= {"session_key", "run_id", "event_type", "status"}
    assert "secret_stuff" not in meta
