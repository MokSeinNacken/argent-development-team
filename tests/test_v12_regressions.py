"""Regression tests for the verified residual findings (SPEC V1.2 12.6).

Each test reproduces one residual finding from the Sol-Recheck and proves the
V1.2 fix:

(a) R4-rest bypass: pause -> gate via a pause ``resume_state`` is blocked
    (direct ``validate_transition`` and the ``request_action`` path).
(b) AUTONOMOUS/FORBIDDEN actions on terminal/pause states are blocked without
    persisting an ``action_executions`` row.
(c) Approval-expiry deadlock: an expired ``approved`` approval is atomically
    marked ``expired`` on ``execute_approved`` and a fresh request works.
(d) A corrupt ``resume_state`` in the DB does not crash ``recover()``; the task
    is moved to ``BLOCKED`` and the rest of the recovery continues.
(e) Event-envelope privacy: a deny-listed word in ``type``/``task_id``/``role``/
    ``state`` raises ``PrivacyViolation`` and nothing is written.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from argent_core import (
    ActionExecutionStatus,
    ApprovalError,
    ApprovalStatus,
    Core,
    Event,
    InvalidTransition,
    PrivacyViolation,
    Role,
    TaskState,
    is_allowed,
    validate_transition,
    OWNER_SOURCE,
)

from conftest import LEAD, events_of, start_lead

OWNER = OWNER_SOURCE


def _set_state(db_path, task_id, state, resume_state=None):
    """Inject a task state directly (bypasses the public transition table).

    Only used to place a task into a state that is unreachable through the
    public API (``PAUSED``/``RECOVERING``/``FAILED``) so the fail-closed
    guards around those corrupt/unreachable states can be exercised.  It is
    never used to mask a bug or to paper over a public-API deadlock.
    """
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE tasks SET state = ?, resume_state = ? WHERE id = ?",
        (state, resume_state, task_id),
    )
    conn.commit()
    conn.close()


def _drive_to(core, task_id, state):
    """Move a task (with an active lead run) to ``state`` via public commands."""
    if state is TaskState.DONE:
        for s in [TaskState.PLANNING, TaskState.ANALYZING, TaskState.LEAD_DECISION,
                  TaskState.IMPLEMENTING, TaskState.TESTING, TaskState.REVIEWING,
                  TaskState.FINAL_DECISION, TaskState.DONE]:
            core.transition(task_id, s, LEAD)
    elif state is TaskState.CANCELLED:
        core.transition(task_id, TaskState.CANCELLED, LEAD)
    else:
        core.transition(task_id, state, LEAD)


# ----------------------------------------------------------------- (a) R4-rest


def test_resume_target_cannot_be_pause_direct():
    # The dynamic resume rule must reject any pause state as a resume target.
    assert is_allowed(
        TaskState.PAUSED, TaskState.OWNER_APPROVAL_REQUIRED,
        TaskState.OWNER_APPROVAL_REQUIRED,
    ) is False
    assert is_allowed(
        TaskState.RECOVERING, TaskState.OWNER_APPROVAL_REQUIRED,
        TaskState.OWNER_APPROVAL_REQUIRED,
    ) is False
    with pytest.raises(InvalidTransition):
        validate_transition(
            TaskState.PAUSED, TaskState.OWNER_APPROVAL_REQUIRED,
            TaskState.OWNER_APPROVAL_REQUIRED,
        )
    with pytest.raises(InvalidTransition):
        validate_transition(
            TaskState.RECOVERING, TaskState.OWNER_APPROVAL_REQUIRED,
            TaskState.OWNER_APPROVAL_REQUIRED,
        )


@pytest.mark.parametrize("from_state", [TaskState.PAUSED, TaskState.RECOVERING])
@pytest.mark.parametrize(
    "resume",
    [TaskState.OWNER_APPROVAL_REQUIRED, TaskState.PAUSED, TaskState.RECOVERING],
)
def test_resume_target_cannot_be_any_pause(from_state, resume):
    assert is_allowed(from_state, resume, resume) is False
    with pytest.raises(InvalidTransition):
        validate_transition(from_state, resume, resume)


def test_R4_rest_gate_from_pause_blocked_via_request_action(db_path, core, task):
    # A PAUSED task whose resume_state points at a pause state (here the gate)
    # must not be able to enter the gate again.
    start_lead(core, task.id)
    _set_state(db_path, task.id, "PAUSED", "OWNER_APPROVAL_REQUIRED")
    with pytest.raises(InvalidTransition):
        core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    # No approval, no gate entry, no state change.
    assert core.queries.list_approvals(task.id) == []
    assert core.queries.get_task(task.id).state is TaskState.PAUSED


# ------------------------------------------------- (b) terminal/pause actions


@pytest.mark.parametrize("state", [TaskState.DONE, TaskState.CANCELLED])
def test_autonomous_blocked_on_terminal(core, task, state):
    start_lead(core, task.id)
    _drive_to(core, task.id, state)
    with pytest.raises(InvalidTransition):
        core.request_action(task.id, "analyze", "x", Role.LEAD, LEAD)
    assert core.queries.list_action_executions(task.id) == []


@pytest.mark.parametrize("state", [TaskState.DONE, TaskState.CANCELLED])
def test_forbidden_blocked_on_terminal(core, task, state):
    start_lead(core, task.id)
    _drive_to(core, task.id, state)
    with pytest.raises(InvalidTransition):
        core.request_action(task.id, "exfiltrate_data", "x", Role.LEAD, LEAD)
    assert core.queries.list_action_executions(task.id) == []
    assert core.queries.list_approvals(task.id) == []
    assert events_of(core, "lead.decision", task.id) == []


@pytest.mark.parametrize(
    "state", [TaskState.PAUSED, TaskState.RECOVERING, TaskState.OWNER_APPROVAL_REQUIRED]
)
def test_autonomous_blocked_on_pause(db_path, core, task, state):
    start_lead(core, task.id)
    if state is TaskState.OWNER_APPROVAL_REQUIRED:
        core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    else:
        _set_state(db_path, task.id, state.value, None)
    with pytest.raises(InvalidTransition):
        core.request_action(task.id, "analyze", "x", Role.LEAD, LEAD)
    assert core.queries.list_action_executions(task.id) == []


@pytest.mark.parametrize(
    "state", [TaskState.PAUSED, TaskState.RECOVERING, TaskState.OWNER_APPROVAL_REQUIRED]
)
def test_forbidden_blocked_on_pause(db_path, core, task, state):
    start_lead(core, task.id)
    if state is TaskState.OWNER_APPROVAL_REQUIRED:
        core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    else:
        _set_state(db_path, task.id, state.value, None)
    with pytest.raises(InvalidTransition):
        core.request_action(task.id, "exfiltrate_data", "x", Role.LEAD, LEAD)
    assert core.queries.list_action_executions(task.id) == []


# Supervisor decision (SPEC V1.2 12.2, literal "main path + REWORK"):
# BLOCKED/FAILED are NOT actionable, even though they are non-terminal and
# non-pause.
@pytest.mark.parametrize("state", [TaskState.BLOCKED, TaskState.FAILED])
def test_autonomous_blocked_on_blocked_failed(db_path, core, task, state):
    start_lead(core, task.id)
    _set_state(db_path, task.id, state.value, None)
    with pytest.raises(InvalidTransition):
        core.request_action(task.id, "analyze", "x", Role.LEAD, LEAD)
    assert core.queries.list_action_executions(task.id) == []


@pytest.mark.parametrize("state", [TaskState.BLOCKED, TaskState.FAILED])
def test_forbidden_blocked_on_blocked_failed(db_path, core, task, state):
    start_lead(core, task.id)
    _set_state(db_path, task.id, state.value, None)
    with pytest.raises(InvalidTransition):
        core.request_action(task.id, "exfiltrate_data", "x", Role.LEAD, LEAD)
    assert core.queries.list_action_executions(task.id) == []
    assert core.queries.list_approvals(task.id) == []


@pytest.mark.parametrize("state", [TaskState.BLOCKED, TaskState.FAILED])
def test_gate_request_blocked_on_blocked_failed(db_path, core, task, state):
    start_lead(core, task.id)
    _set_state(db_path, task.id, state.value, None)
    with pytest.raises(InvalidTransition):
        core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    assert core.queries.list_approvals(task.id) == []
    assert core.queries.get_task(task.id).state is state


def test_expiry_lapse_end_to_end(tmp_path):
    """Expiry-lapse E2E (SPEC V1.3 13.2, 13.4): no SQL state reset.

    An approved approval that expires must be atomically marked ``expired`` and
    its task released back to the validated ``resume_state`` (not parked in
    ``OWNER_APPROVAL_REQUIRED``).  The workflow then stays alive over the
    public API: the active role run re-requests the same gated action, the
    owner approves, and ``execute_approved`` consumes it and executes.
    """
    class Clock:
        def __init__(self):
            self.t = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        def __call__(self):
            return self.t

    clock = Clock()
    db = str(tmp_path / "exp3.db")
    c = Core(db, clock=clock)
    p = c.create_project("p", OWNER)
    task = c.create_task(p.id, "t", OWNER)
    c.start_role(task.id, Role.LEAD, LEAD)
    c.transition(task.id, TaskState.PLANNING, LEAD)
    res = c.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    c.approve(res.approval.id, OWNER, task_id=task.id,
              action="deploy_production", scope="prod")
    clock.t += timedelta(hours=2)  # expire the approved approval

    # execute_approved on an expired approved approval -> ApprovalError,
    # approval 'expired', task released back to its resume_state (no deadlock).
    with pytest.raises(ApprovalError):
        c.execute_approved(res.approval.id, OWNER, task_id=task.id,
                           action="deploy_production", scope="prod")
    assert c.queries.get_approval(res.approval.id).status is ApprovalStatus.EXPIRED
    assert c.queries.get_task(task.id).state is TaskState.PLANNING
    assert c.queries.get_task(task.id).resume_state is None

    # Fresh request over the public API (the active lead run is still active).
    res2 = c.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    assert res2.approval.id != res.approval.id
    c.approve(res2.approval.id, OWNER, task_id=task.id,
              action="deploy_production", scope="prod")
    ap2 = c.execute_approved(res2.approval.id, OWNER, task_id=task.id,
                             action="deploy_production", scope="prod")
    assert ap2.status is ApprovalStatus.CONSUMED
    assert c.queries.get_task(task.id).state is TaskState.PLANNING
    execs = c.queries.list_action_executions(task.id)
    assert len(execs) == 1
    assert execs[0].status is ActionExecutionStatus.EXECUTED
    assert execs[0].approval_id == res2.approval.id
    c.close()


def test_pending_expiry_approve_releases_task(tmp_path):
    """Expired-pending approve-path liveness (supervisor decision on 13.2).

    An approval that expires while still ``pending`` must also be released:
    ``approve()`` on it marks it ``expired`` and moves the task back to its
    validated ``resume_state`` (no deadlock), then raises ``ApprovalError``.
    A fresh request is then possible over the public API.
    """
    class Clock:
        def __init__(self):
            self.t = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        def __call__(self):
            return self.t

    clock = Clock()
    db = str(tmp_path / "exp_pending.db")
    c = Core(db, clock=clock)
    p = c.create_project("p", OWNER)
    task = c.create_task(p.id, "t", OWNER)
    c.start_role(task.id, Role.LEAD, LEAD)
    c.transition(task.id, TaskState.PLANNING, LEAD)
    res = c.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    assert c.queries.get_task(task.id).state is TaskState.OWNER_APPROVAL_REQUIRED
    clock.t += timedelta(hours=2)  # expire the pending approval

    # approve() on an expired pending approval -> ApprovalError, approval
    # 'expired', task released back to its resume_state (no deadlock).
    with pytest.raises(ApprovalError):
        c.approve(res.approval.id, OWNER, task_id=task.id,
                  action="deploy_production", scope="prod")
    assert c.queries.get_approval(res.approval.id).status is ApprovalStatus.EXPIRED
    assert c.queries.get_task(task.id).state is TaskState.PLANNING
    assert c.queries.get_task(task.id).resume_state is None

    # Fresh request over the public API works and completes normally.
    res2 = c.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    assert res2.approval.id != res.approval.id
    c.approve(res2.approval.id, OWNER, task_id=task.id,
              action="deploy_production", scope="prod")
    ap2 = c.execute_approved(res2.approval.id, OWNER, task_id=task.id,
                             action="deploy_production", scope="prod")
    assert ap2.status is ApprovalStatus.CONSUMED
    assert c.queries.get_task(task.id).state is TaskState.PLANNING
    execs = c.queries.list_action_executions(task.id)
    assert len(execs) == 1
    assert execs[0].status is ActionExecutionStatus.EXECUTED
    assert execs[0].approval_id == res2.approval.id
    c.close()


# ------------------------------------------------------ (d) corrupt resume_state


def test_resume_state_check_constraint_blocks_unknown(db_path, core, task):
    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE tasks SET resume_state = 'BOGUS' WHERE id = ?", (task.id,)
            )
    finally:
        conn.close()


def test_broken_resume_state_recovery_blocks(db_path, core, project, task):
    # A second, healthy task that must be left untouched by the recovery.
    task2 = core.create_task(project.id, "healthy", OWNER)
    # Inject a corrupt resume_state that pre-dates the CHECK (bypass it to
    # simulate a legacy/buggy write).
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA ignore_check_constraints = ON")
    conn.execute(
        "UPDATE tasks SET state = 'RECOVERING', resume_state = 'BOGUS' WHERE id = ?",
        (task.id,),
    )
    conn.commit()
    conn.close()

    # recover() must not raise, must block the corrupt task, and continue.
    report = core.recover(OWNER)
    assert core.queries.get_task(task.id).state is TaskState.BLOCKED
    assert core.queries.get_task(task2.id).state is TaskState.NEW
    assert len(report.rolled_back) == 1


# ----------------------------------------------------- (e) envelope privacy


@pytest.mark.parametrize(
    "field,word,value",
    [
        ("type", "secret", "task.secret"),
        ("type", "code", "lead.code_decision"),
        ("task_id", "email_address", "t-email_address-x"),
        ("role", "password", "role-with-password"),
        ("state", "credential", "STATE_credential"),
        ("state", "token", "token_state"),
    ],
)
def test_event_envelope_privacy(core, field, word, value):
    kwargs = dict(
        id="env-" + field,
        type="lead.decision",
        task_id=None,
        role=None,
        state=None,
        payload={"note": "safe"},
        created_at="t",
    )
    kwargs[field] = value
    ev = Event(**kwargs)
    with pytest.raises(PrivacyViolation):
        core._store._insert_event(ev)
    assert all(e.id != ev.id for e in core._store.list_events())
