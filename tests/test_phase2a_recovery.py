"""Phase 2A recovery + atomic-consumption tests (SPEC V2 11, 15.2/15.6/15.7)."""

import threading

import pytest

from argent_core import (
    Core,
    DispatchError,
    DispatchStatus,
    Role,
    RoleRunStatus,
    SequenceKind,
    TaskState,
    OWNER_SOURCE,
)

from conftest import LEAD, events_of
from mock_runtime import MockRuntime
from phase2a_helpers import (
    orchestrated_task,
    receive_valid,
    run_role,
    start_and_dispatch,
)

OWNER = OWNER_SOURCE


def _drive_to_implementer(core, runtime, task, task_run):
    run_role(core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD)
    run_role(core, runtime, task, task_run, Role.ANALYST, 1, 1, SequenceKind.STANDARD)
    run_role(core, runtime, task, task_run, Role.LEAD, 1, 2, SequenceKind.STANDARD)
    core.start_role(task.id, Role.IMPLEMENTER, LEAD)


def test_recovery_inflight_implementer_never_auto_failed(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    _drive_to_implementer(core, runtime, task, task_run)
    d = core.create_dispatch(
        task.id, task_run.id, Role.IMPLEMENTER, 3, 1, SequenceKind.STANDARD, None, LEAD
    )
    session, run = runtime.spawn()
    core.bind_spawn_result(
        d.id, session, run, d.expected_agent_class, d.expected_model_class, "medium", LEAD
    )
    core.recover(OWNER, idempotency_key="k")
    d = core.queries.get_dispatch(d.id)
    assert d.status is DispatchStatus.RECOVERY_PENDING
    # Write-role run stays STARTED (unresolved dispatch).
    active = core.queries.get_active_role_run(task.id)
    assert active is not None and active.role is Role.IMPLEMENTER
    assert active.status is RoleRunStatus.STARTED
    # Task conservatively RECOVERING.
    assert core.queries.get_task(task.id).state is TaskState.RECOVERING
    # No second write role possible (active dispatch + active role run).
    with pytest.raises(Exception):
        core.start_role(task.id, Role.QA, LEAD)


def test_recovery_spawn_before_bind_no_ghost_writer(core):
    # A write-role PENDING dispatch (spawn intent persisted, no bind yet) must
    # NOT be auto-failed; it becomes RECOVERY_PENDING so no second writer runs.
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    _drive_to_implementer(core, runtime, task, task_run)
    d = core.create_dispatch(
        task.id, task_run.id, Role.IMPLEMENTER, 3, 1, SequenceKind.STANDARD, None, LEAD
    )
    assert d.status is DispatchStatus.PENDING
    core.recover(OWNER)
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.RECOVERY_PENDING
    assert core.queries.get_task(task.id).state is TaskState.RECOVERING


def test_recovery_readonly_pending_failed(core):
    # A read-only PENDING dispatch (never spawned) is harmless -> FAILED.
    task, task_run = orchestrated_task(core)
    core.start_role(task.id, Role.LEAD, LEAD)
    d = core.create_dispatch(
        task.id, task_run.id, Role.LEAD, 0, 1, SequenceKind.STANDARD, None, LEAD
    )
    core.recover(OWNER)
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.FAILED


def test_restart_unconsumed_result_consumable(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = start_and_dispatch(
        core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD
    )
    # Crash/restart: recover -> RECOVERY_PENDING.
    core.recover(OWNER)
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.RECOVERY_PENDING
    # The already-delivered, not-yet-consumed result is consumable.
    res = receive_valid(core, runtime, d, session, run, task.id, Role.LEAD)
    assert res.status == "consumed"
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED


def test_absent_recovery_result_stays_recovering(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    _drive_to_implementer(core, runtime, task, task_run)
    d = core.create_dispatch(
        task.id, task_run.id, Role.IMPLEMENTER, 3, 1, SequenceKind.STANDARD, None, LEAD
    )
    session, run = runtime.spawn()
    core.bind_spawn_result(
        d.id, session, run, d.expected_agent_class, d.expected_model_class, "medium", LEAD
    )
    core.recover(OWNER)
    assert core.queries.get_task(task.id).state is TaskState.RECOVERING
    # A second recover without a result keeps it conservatively RECOVERING.
    core.recover(OWNER)
    assert core.queries.get_task(task.id).state is TaskState.RECOVERING


def test_atomic_consumption_two_connections_cas(tmp_path):
    # Two independent connections race to consume the same valid result; the
    # CAS (rowcount) + CONSUMED check ensure exactly one consumption.
    db = str(tmp_path / "cas.db")
    runtime = MockRuntime()
    c0 = Core(db)
    task, task_run = orchestrated_task(c0)
    c0.start_role(task.id, Role.LEAD, LEAD)
    d = c0.create_dispatch(
        task.id, task_run.id, Role.LEAD, 0, 1, SequenceKind.STANDARD, None, LEAD
    )
    session, run = runtime.spawn()
    c0.bind_spawn_result(
        d.id, session, run, d.expected_agent_class, d.expected_model_class, "high", LEAD
    )
    c0.close()

    barrier = threading.Barrier(2)
    results = {}

    def consume(key):
        core = Core(db)  # each thread opens its own connection
        em = runtime.completion_event(task.id, session, run)
        out = {
            "role": "lead", "task_id": task.id, "dispatch_id": d.id,
            "status": "ok", "findings": [], "own_assessment": "x", "concerns": [],
            "proposal": "x", "alternatives": [], "confidence": 1.0, "blockers": [],
            "requested_next_state": "PLANNING", "decision": "accept",
            "accepted_findings": [], "rejected_findings": [], "rationale": "y",
        }
        barrier.wait()
        results[key] = core.receive_agent_result(d.id, em, out, LEAD)
        core.close()

    t1 = threading.Thread(target=consume, args=("a",))
    t2 = threading.Thread(target=consume, args=("b",))
    t1.start(); t2.start(); t1.join(); t2.join()

    statuses = sorted(results.values(), key=lambda r: r.status)
    assert [r.status for r in statuses] == ["consumed", "duplicate"]
    # Effects applied exactly once.
    c0 = Core(db)
    assert len(c0.queries.list_decisions(task.id)) == 1
    c0.close()


def test_effect_binding_foreign_finding_rolls_back(core):
    # A lead result referencing a finding from ANOTHER task must abort and
    # roll back entirely (SPEC V2 15.7).
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    other_task, _ = orchestrated_task(core)
    # Insert a finding bound to the other task via store internals.
    from argent_core import Finding, FindingStatus
    fid = "foreign-finding"
    core._store._insert_finding(
        Finding(id=fid, task_id=other_task.id, severity="low",
                description="a foreign finding", status=FindingStatus.OPEN,
                created_at="t")
    )
    d, session, run = start_and_dispatch(
        core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD
    )
    em = runtime.completion_event(task.id, session, run)
    out = {
        "role": "lead", "task_id": task.id, "dispatch_id": d.id,
        "status": "ok", "findings": [], "own_assessment": "x", "concerns": [],
        "proposal": "x", "alternatives": [], "confidence": 1.0, "blockers": [],
        "requested_next_state": "PLANNING", "decision": "accept",
        "accepted_findings": [fid], "rejected_findings": [], "rationale": "y",
    }
    with pytest.raises(DispatchError):
        core.receive_agent_result(d.id, em, out, LEAD)
    # No partial state: dispatch not consumed, no decision, no handoff.
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING
    assert core.queries.list_decisions(task.id) == []
    assert core.queries.list_handoffs(task.id) == []
