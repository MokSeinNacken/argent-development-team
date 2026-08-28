"""Phase 2B state-sync regression (V2B 16.3 F9, found by the real E2E).

A rework decision at the FIRST lead gate (STANDARD position 0) must enter the
rework cycle through ``PLANNING -> REWORK``; otherwise the rework-cycle
implementer's ``REWORK -> IMPLEMENTING`` sync crashes with InvalidTransition
(``expected task state REWORK, got PLANNING``).

The real E2E (Task 2) hit exactly this: the pos0 lead decided ``rework``, the
frontier started cycle 2, the cycle-2 lead accepted, and the implementer
receive failed.  This test pins the fixed behaviour with the mock runtime.
"""

import pytest

from argent_core import (
    FindingStatus,
    Role,
    SequenceKind,
    TaskState,
)

from conftest import LEAD
from mock_runtime import MockRuntime
from phase2a_helpers import orchestrated_task, receive_valid, start_and_dispatch


def _run_role(core, runtime, task, task_run, role, cycle_no, position, kind,
              decision=None):
    d, session, run = start_and_dispatch(
        core, runtime, task, task_run, role, cycle_no, position, kind
    )
    overrides = {}
    if decision is not None:
        overrides["decision"] = decision
    result = receive_valid(
        core, runtime, d, session, run, task.id, role, **overrides
    )
    assert result.status == "consumed", result
    return d


def test_pos0_rework_enters_rework_cycle(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)

    # pos0 lead: rework at the spec gate.
    _run_role(core, runtime, task, task_run, Role.LEAD, 1, 0,
              SequenceKind.STANDARD, decision="rework")
    t = core.queries.get_task(task.id)
    assert t.state is TaskState.REWORK, t.state
    frontier = core._workflow_frontier(task.id)
    assert frontier.cycle_no == 2 and frontier.position == 0
    assert frontier.sequence_kind is SequenceKind.REWORK
    assert frontier.expected_role is Role.LEAD

    # cycle-2 lead accepts the rework plan (state stays REWORK).
    _run_role(core, runtime, task, task_run, Role.LEAD, 2, 0,
              SequenceKind.REWORK, decision="accept")
    t = core.queries.get_task(task.id)
    assert t.state is TaskState.REWORK, t.state

    # rework implementer consumes cleanly (REWORK -> IMPLEMENTING).
    _run_role(core, runtime, task, task_run, Role.IMPLEMENTER, 2, 1,
              SequenceKind.REWORK)
    t = core.queries.get_task(task.id)
    assert t.state is TaskState.IMPLEMENTING, t.state

    # qa (IMPLEMENTING -> TESTING) and final lead (accept -> DONE).
    _run_role(core, runtime, task, task_run, Role.QA, 2, 2,
              SequenceKind.REWORK)
    t = core.queries.get_task(task.id)
    assert t.state is TaskState.TESTING, t.state
    _run_role(core, runtime, task, task_run, Role.LEAD, 2, 3,
              SequenceKind.REWORK, decision="accept")
    t = core.queries.get_task(task.id)
    assert t.state is TaskState.DONE, t.state


def test_rework_start_cancel_actually_cancels(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    _run_role(core, runtime, task, task_run, Role.LEAD, 1, 0,
              SequenceKind.STANDARD, decision="rework")
    # rework-start lead cancels -> escape to CANCELLED.
    _run_role(core, runtime, task, task_run, Role.LEAD, 2, 0,
              SequenceKind.REWORK, decision="cancel")
    t = core.queries.get_task(task.id)
    assert t.state is TaskState.CANCELLED, t.state


def test_rework_start_rework_keeps_cycle(core):
    """A second rework at the rework-start gate stays in REWORK (no loop crash)."""
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    _run_role(core, runtime, task, task_run, Role.LEAD, 1, 0,
              SequenceKind.STANDARD, decision="rework")
    _run_role(core, runtime, task, task_run, Role.LEAD, 2, 0,
              SequenceKind.REWORK, decision="rework")
    t = core.queries.get_task(task.id)
    assert t.state is TaskState.REWORK, t.state
    frontier = core._workflow_frontier(task.id)
    assert frontier.cycle_no == 3 and frontier.expected_role is Role.LEAD


def test_rework_keeps_accepted_findings_open_then_accept_resolves(core):
    """FIX 2(b): a lead decision only resolves accepted findings on 'accept'.

    A rework decision that lists an accepted finding must NOT mark it RESOLVED
    (the remediation was never verified).  A subsequent accept resolves it.
    """
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)

    # pos0 lead (accept) creates a HIGH finding via its findings payload.
    d, session, run = start_and_dispatch(
        core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD
    )
    res = receive_valid(
        core, runtime, d, session, run, task.id, Role.LEAD,
        decision="accept",
        findings=[{"severity": "high", "description": "persistent defect remains"}],
    )
    assert res.status == "consumed", res
    findings = core.queries.list_findings(task.id)
    assert len(findings) == 1
    fid = findings[0].id
    assert findings[0].status is FindingStatus.OPEN

    # pos1 analyst, then pos2 lead decides REWORK while "accepting" the finding.
    d, session, run = start_and_dispatch(
        core, runtime, task, task_run, Role.ANALYST, 1, 1, SequenceKind.STANDARD
    )
    assert receive_valid(core, runtime, d, session, run, task.id, Role.ANALYST).status == "consumed"
    d, session, run = start_and_dispatch(
        core, runtime, task, task_run, Role.LEAD, 1, 2, SequenceKind.STANDARD
    )
    res = receive_valid(
        core, runtime, d, session, run, task.id, Role.LEAD,
        decision="rework",
        accepted_findings=[fid],
    )
    assert res.status == "consumed", res
    assert core.queries.get_finding(fid).status is FindingStatus.OPEN  # rework keeps it open

    # rework-cycle lead accept resolves the same finding.
    d, session, run = start_and_dispatch(
        core, runtime, task, task_run, Role.LEAD, 2, 0, SequenceKind.REWORK
    )
    res = receive_valid(
        core, runtime, d, session, run, task.id, Role.LEAD,
        decision="accept",
        accepted_findings=[fid],
    )
    assert res.status == "consumed", res
    assert core.queries.get_finding(fid).status is FindingStatus.RESOLVED
