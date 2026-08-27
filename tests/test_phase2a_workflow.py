"""Phase 2A workflow-sequence tests (SPEC V2 2/12, V2.1 15.4/15.12)."""

import pytest

from argent_core import (
    DispatchError,
    PermissionDenied,
    Role,
    RoleConflict,
    SequenceKind,
    role_source,
)

from conftest import LEAD
from mock_runtime import MockRuntime, build_output
from phase2a_helpers import (
    orchestrated_task,
    receive_valid,
    run_role,
    start_and_dispatch,
)

STANDARD = [
    Role.LEAD,
    Role.ANALYST,
    Role.LEAD,
    Role.IMPLEMENTER,
    Role.QA,
    Role.REVIEWER,
    Role.LEAD,
]


def _run_standard(core, runtime, task, task_run, cycle=1):
    for pos, role in enumerate(STANDARD):
        run_role(core, runtime, task, task_run, role, cycle, pos, SequenceKind.STANDARD)


def test_full_standard_workflow_end_to_end(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    _run_standard(core, runtime, task, task_run)
    dispatches = core.queries.list_dispatches(task.id)
    assert len(dispatches) == 7
    assert all(d.status.value == "CONSUMED" for d in dispatches)
    assert [d.role.value for d in dispatches] == [r.value for r in STANDARD]
    # After the final lead the workflow is complete (no next role).
    assert core.expected_next_role(task.id, LEAD) is None
    # Exactly the seven handoffs of the standard sequence.
    handoffs = core.queries.list_handoffs(task.id)
    assert [(h.from_role.value, h.to_role.value) for h in handoffs] == [
        ("lead", "analyst"),
        ("analyst", "lead"),
        ("lead", "implementer"),
        ("implementer", "qa"),
        ("qa", "reviewer"),
        ("reviewer", "lead"),
    ]


def test_full_rework_workflow_end_to_end(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    # Cycle 1: lead -> analyst -> lead (decides rework).
    run_role(core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD)
    run_role(core, runtime, task, task_run, Role.ANALYST, 1, 1, SequenceKind.STANDARD)
    d, res = run_role(
        core, runtime, task, task_run, Role.LEAD, 1, 2, SequenceKind.STANDARD,
        decision="rework", rework_include_reviewer=True,
    )
    assert res.status == "consumed"
    # Rework branch: new cycle 2, REWORK sequence.
    assert core.expected_next_role(task.id, LEAD) is Role.LEAD
    run_role(core, runtime, task, task_run, Role.LEAD, 2, 0, SequenceKind.REWORK)
    run_role(core, runtime, task, task_run, Role.IMPLEMENTER, 2, 1, SequenceKind.REWORK)
    run_role(core, runtime, task, task_run, Role.QA, 2, 2, SequenceKind.REWORK)
    run_role(core, runtime, task, task_run, Role.REVIEWER, 2, 3, SequenceKind.REWORK)
    run_role(core, runtime, task, task_run, Role.LEAD, 2, 4, SequenceKind.REWORK)
    assert core.expected_next_role(task.id, LEAD) is None
    dispatches = core.queries.list_dispatches(task.id)
    assert [d.sequence_kind.value for d in dispatches[:3]] == ["STANDARD"] * 3
    assert [d.sequence_kind.value for d in dispatches[3:]] == ["REWORK"] * 5
    assert [d.cycle_no for d in dispatches[3:]] == [2, 2, 2, 2, 2]


def test_controller_is_only_dispatcher(core):
    task, task_run = orchestrated_task(core)
    core.start_role(task.id, Role.LEAD, LEAD)
    with pytest.raises(PermissionDenied):
        core.create_dispatch(
            task.id, task_run.id, Role.LEAD, 0, 1, SequenceKind.STANDARD, None,
            role_source(Role.QA),
        )


def test_role_cannot_start_another_role(core):
    task, task_run = orchestrated_task(core)
    core.start_role(task.id, Role.LEAD, LEAD)
    # Only lead (controller) may start roles.
    with pytest.raises(PermissionDenied):
        core.start_role(task.id, Role.ANALYST, role_source(Role.QA))


def test_exactly_one_active_role(core):
    task, _ = orchestrated_task(core)
    core.start_role(task.id, Role.LEAD, LEAD)
    with pytest.raises(RoleConflict):
        core.start_role(task.id, Role.LEAD, LEAD)


def test_dispatch_requires_matching_frontier_position(core):
    task, task_run = orchestrated_task(core)
    core.start_role(task.id, Role.LEAD, LEAD)
    # Wrong position (0 expected) -> DispatchError.
    with pytest.raises(DispatchError):
        core.create_dispatch(
            task.id, task_run.id, Role.LEAD, 1, 1, SequenceKind.STANDARD, None, LEAD
        )
    # Wrong cycle -> DispatchError.
    with pytest.raises(DispatchError):
        core.create_dispatch(
            task.id, task_run.id, Role.LEAD, 0, 2, SequenceKind.STANDARD, None, LEAD
        )
    # Wrong sequence kind -> DispatchError.
    with pytest.raises(DispatchError):
        core.create_dispatch(
            task.id, task_run.id, Role.LEAD, 0, 1, SequenceKind.REWORK, None, LEAD
        )


def test_multiple_lead_positions_have_distinct_unique(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    _run_standard(core, runtime, task, task_run)
    # Three lead dispatches at positions 0, 2, 6 (SPEC V2 15.12).
    lead_positions = [
        d.position
        for d in core.queries.list_dispatches(task.id)
        if d.role is Role.LEAD
    ]
    assert lead_positions == [0, 2, 6]
    assert len({d.id for d in core.queries.list_dispatches(task.id)}) == 7


def test_multiple_rework_cycles(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    # Cycle 1 rework.
    run_role(core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD)
    run_role(core, runtime, task, task_run, Role.ANALYST, 1, 1, SequenceKind.STANDARD)
    run_role(core, runtime, task, task_run, Role.LEAD, 1, 2, SequenceKind.STANDARD,
             decision="rework")
    # Cycle 2 rework again.
    run_role(core, runtime, task, task_run, Role.LEAD, 2, 0, SequenceKind.REWORK)
    run_role(core, runtime, task, task_run, Role.IMPLEMENTER, 2, 1, SequenceKind.REWORK)
    run_role(core, runtime, task, task_run, Role.QA, 2, 2, SequenceKind.REWORK)
    run_role(core, runtime, task, task_run, Role.LEAD, 2, 3, SequenceKind.REWORK,
             decision="rework")
    # Cycle 3 resolves.
    run_role(core, runtime, task, task_run, Role.LEAD, 3, 0, SequenceKind.REWORK)
    run_role(core, runtime, task, task_run, Role.IMPLEMENTER, 3, 1, SequenceKind.REWORK)
    run_role(core, runtime, task, task_run, Role.QA, 3, 2, SequenceKind.REWORK)
    run_role(core, runtime, task, task_run, Role.LEAD, 3, 3, SequenceKind.REWORK)
    assert core.expected_next_role(task.id, LEAD) is None
    cycles = {d.cycle_no for d in core.queries.list_dispatches(task.id)}
    assert cycles == {1, 2, 3}


def test_expected_run_accepted(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = start_and_dispatch(
        core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD
    )
    assert d.status.value == "RUNNING"
    res = receive_valid(core, runtime, d, session, run, task.id, Role.LEAD)
    assert res.status == "consumed"
    assert core.queries.get_dispatch(d.id).status.value == "CONSUMED"


def test_rework_excludes_reviewer_without_findings(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    run_role(core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD)
    run_role(core, runtime, task, task_run, Role.ANALYST, 1, 1, SequenceKind.STANDARD)
    run_role(core, runtime, task, task_run, Role.LEAD, 1, 2, SequenceKind.STANDARD,
             decision="rework")
    # No explicit toggle, no open findings -> reviewer excluded (4 roles).
    run_role(core, runtime, task, task_run, Role.LEAD, 2, 0, SequenceKind.REWORK)
    run_role(core, runtime, task, task_run, Role.IMPLEMENTER, 2, 1, SequenceKind.REWORK)
    run_role(core, runtime, task, task_run, Role.QA, 2, 2, SequenceKind.REWORK)
    assert core.expected_next_role(task.id, LEAD) is Role.LEAD
