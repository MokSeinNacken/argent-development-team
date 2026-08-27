"""Phase 2A Fix-Runde 3: foreign completion events on a CONSUMED dispatch.

Reproduces the verified MEDIUM finding: the ``CONSUMED`` branch of
``_receive_work`` used to swallow ANY re-delivery as ``duplicate`` without
checking event identity.  A forged/divergent completion event for an
already-consumed dispatch is now quarantined and rejected (SPEC V2 3.3 /
V2.1 15.3) — duplicate idempotency is valid only for the same run.
"""

from argent_core import DispatchStatus, Role, SequenceKind

from conftest import LEAD
from mock_runtime import MockRuntime
from phase2a_helpers import (
    orchestrated_task,
    receive_valid,
    start_and_dispatch,
)


def _consume_lead(core, runtime, task, task_run):
    """Bind and consume a single lead dispatch; return (d, session, run)."""
    d, session, run = start_and_dispatch(
        core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD
    )
    res = receive_valid(core, runtime, d, session, run, task.id, Role.LEAD)
    assert res.status == "consumed"
    return d, session, run


def _typed_events(core, task_id, type_):
    return [
        e for e in core.list_events(LEAD, task_id=task_id) if e.type == type_
    ]


def _lead_out(task_id, dispatch_id):
    return {"role": "lead", "task_id": task_id, "dispatch_id": dispatch_id}


# (a) same run re-delivered with IDENTICAL event_meta -> idempotent duplicate.
def test_consumed_identical_redelivery_is_duplicate(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _consume_lead(core, runtime, task, task_run)

    assert _typed_events(core, task.id, "agent.result_duplicate") == []

    em = runtime.completion_event(task.id, session, run)
    res = core.receive_agent_result(d.id, em, _lead_out(task.id, d.id), LEAD)

    assert res.status == "duplicate"
    assert res.reason is None
    assert len(_typed_events(core, task.id, "agent.result_duplicate")) == 1
    assert core.quarantine_log(LEAD, task.id) == []
    # No state change: dispatch stays CONSUMED, single decision.
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED
    assert len(core.queries.list_decisions(task.id)) == 1


# (b) consumed dispatch + forged child_session_id (correct run_id) -> rejected.
def test_consumed_foreign_session_rejected_and_quarantined(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _consume_lead(core, runtime, task, task_run)

    em = runtime.completion_event(task.id, "forged-session", run)
    res = core.receive_agent_result(d.id, em, _lead_out(task.id, d.id), LEAD)

    assert res.status == "rejected"
    assert res.reason == "session_mismatch"
    q = core.quarantine_log(LEAD, task.id)
    assert len(q) == 1 and q[0].reason == "session_mismatch"
    assert len(_typed_events(core, task.id, "agent.result_rejected")) == 1
    assert _typed_events(core, task.id, "agent.result_duplicate") == []
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED


# (c) consumed dispatch + forged run_id -> rejected + quarantined.
def test_consumed_foreign_run_id_rejected_and_quarantined(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _consume_lead(core, runtime, task, task_run)

    em = runtime.completion_event(task.id, session, "forged-run")
    res = core.receive_agent_result(d.id, em, _lead_out(task.id, d.id), LEAD)

    assert res.status == "rejected"
    assert res.reason == "run_id_mismatch"
    q = core.quarantine_log(LEAD, task.id)
    assert len(q) == 1 and q[0].reason == "run_id_mismatch"
    assert len(_typed_events(core, task.id, "agent.result_rejected")) == 1
    assert _typed_events(core, task.id, "agent.result_duplicate") == []
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED


# (d) consumed dispatch + forged task_id -> rejected + quarantined.
def test_consumed_foreign_task_id_rejected_and_quarantined(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _consume_lead(core, runtime, task, task_run)

    em = runtime.completion_event("forged-task", session, run)
    res = core.receive_agent_result(d.id, em, _lead_out(task.id, d.id), LEAD)

    assert res.status == "rejected"
    assert res.reason == "task_mismatch"
    q = core.quarantine_log(LEAD, task.id)
    assert len(q) == 1 and q[0].reason == "task_mismatch"
    assert len(_typed_events(core, task.id, "agent.result_rejected")) == 1
    assert _typed_events(core, task.id, "agent.result_duplicate") == []
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED


# (e) consumed dispatch + missing event_meta fields -> rejected + quarantined.
def test_consumed_missing_metadata_rejected_and_quarantined(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = _consume_lead(core, runtime, task, task_run)

    res = core.receive_agent_result(d.id, {}, _lead_out(task.id, d.id), LEAD)

    assert res.status == "rejected"
    assert res.reason == "missing_metadata"
    q = core.quarantine_log(LEAD, task.id)
    assert len(q) == 1 and q[0].reason == "missing_metadata"
    assert len(_typed_events(core, task.id, "agent.result_rejected")) == 1
    assert _typed_events(core, task.id, "agent.result_duplicate") == []
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED
