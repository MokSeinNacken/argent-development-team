"""State machine tests (SPEC V1 chapter 1 + V1.1 11.1, test points 1 & 2)."""

import pytest

from argent_core import (
    Core,
    InvalidTransition,
    Role,
    TaskState,
    is_allowed,
    validate_transition,
    role_source,
    OWNER_SOURCE,
)

from conftest import LEAD, events_of, start_lead

# The exact transition table (without the dynamic resume_state transitions),
# derived strictly from SPEC V1 chapter 1 as tightened by V1.1 11.1.
_STATIC: set = {
    # main path
    (TaskState.NEW, TaskState.PLANNING),
    (TaskState.PLANNING, TaskState.ANALYZING),
    (TaskState.ANALYZING, TaskState.LEAD_DECISION),
    (TaskState.LEAD_DECISION, TaskState.IMPLEMENTING),
    (TaskState.IMPLEMENTING, TaskState.TESTING),
    (TaskState.TESTING, TaskState.REVIEWING),
    (TaskState.TESTING, TaskState.REWORK),
    (TaskState.REVIEWING, TaskState.FINAL_DECISION),
    (TaskState.REVIEWING, TaskState.REWORK),
    (TaskState.FINAL_DECISION, TaskState.DONE),
    (TaskState.FINAL_DECISION, TaskState.REWORK),
    (TaskState.REWORK, TaskState.PLANNING),
    # additional states
    (TaskState.BLOCKED, TaskState.RECOVERING),
    (TaskState.FAILED, TaskState.RECOVERING),
    (TaskState.FAILED, TaskState.CANCELLED),
}

# Static exits out of pause states (dedicated commands).
_PAUSE_EXITS: set = {
    (TaskState.OWNER_APPROVAL_REQUIRED, TaskState.BLOCKED),
    (TaskState.OWNER_APPROVAL_REQUIRED, TaskState.CANCELLED),
    (TaskState.PAUSED, TaskState.CANCELLED),
    (TaskState.RECOVERING, TaskState.BLOCKED),
}

_TERMINAL = {TaskState.DONE, TaskState.CANCELLED}
_PAUSE = {TaskState.OWNER_APPROVAL_REQUIRED, TaskState.PAUSED, TaskState.RECOVERING}

# Gate entry: every non-terminal, non-pause state -> OWNER_APPROVAL_REQUIRED.
_GATE_ENTRY: set = {
    (s, TaskState.OWNER_APPROVAL_REQUIRED)
    for s in TaskState
    if s not in _TERMINAL and s not in _PAUSE
}

# General escape rule: non-terminal, non-pause -> BLOCKED/CANCELLED.
_ESCAPE: set = {
    (s, t)
    for s in TaskState
    if s not in _TERMINAL and s not in _PAUSE
    for t in (TaskState.BLOCKED, TaskState.CANCELLED)
    if s is not t
}

VALID: set = _STATIC | _PAUSE_EXITS | _GATE_ENTRY | _ESCAPE

ALL_STATES = list(TaskState)


@pytest.mark.parametrize("from_state,to_state", sorted(VALID, key=str))
def test_valid_transition(from_state, to_state):
    assert is_allowed(from_state, to_state) is True


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        (a, b)
        for a in ALL_STATES
        for b in ALL_STATES
        if (a, b) not in VALID
    ],
)
def test_invalid_transition_blocked(from_state, to_state):
    # Without a resume_state, any pair not in VALID must be rejected.
    assert is_allowed(from_state, to_state) is False
    with pytest.raises(InvalidTransition):
        validate_transition(from_state, to_state)


@pytest.mark.parametrize("state", ALL_STATES)
def test_no_self_transition(state):
    assert is_allowed(state, state) is False


@pytest.mark.parametrize("terminal", [TaskState.DONE, TaskState.CANCELLED])
@pytest.mark.parametrize("to_state", ALL_STATES)
def test_terminal_has_no_outgoing(terminal, to_state):
    assert is_allowed(terminal, to_state) is False


@pytest.mark.parametrize(
    "from_state,resume_state",
    [
        (TaskState.OWNER_APPROVAL_REQUIRED, TaskState.IMPLEMENTING),
        (TaskState.OWNER_APPROVAL_REQUIRED, TaskState.REVIEWING),
        (TaskState.PAUSED, TaskState.ANALYZING),
        (TaskState.PAUSED, TaskState.TESTING),
        (TaskState.RECOVERING, TaskState.PLANNING),
        (TaskState.RECOVERING, TaskState.TESTING),
    ],
)
def test_dynamic_resume_transition(from_state, resume_state):
    assert is_allowed(from_state, resume_state, resume_state) is True
    # The same target is rejected when the resume point differs.
    assert is_allowed(from_state, resume_state, None) is False


def test_gate_entry_not_from_terminal():
    assert is_allowed(TaskState.DONE, TaskState.OWNER_APPROVAL_REQUIRED) is False
    assert is_allowed(TaskState.CANCELLED, TaskState.OWNER_APPROVAL_REQUIRED) is False


def test_gate_entry_not_from_pause():
    assert is_allowed(
        TaskState.PAUSED, TaskState.OWNER_APPROVAL_REQUIRED
    ) is False
    assert is_allowed(
        TaskState.RECOVERING, TaskState.OWNER_APPROVAL_REQUIRED
    ) is False


def test_unknown_state_raises():
    with pytest.raises(InvalidTransition):
        is_allowed("NONSENSE", TaskState.NEW)
    with pytest.raises(InvalidTransition):
        is_allowed(TaskState.NEW, "NONSENSE")


def test_unknown_target_state_is_invalid_transition(core, task):
    # R15: unknown target state must raise InvalidTransition, not ValueError.
    with pytest.raises(InvalidTransition):
        core.transition(task.id, "NONSENSE", OWNER_SOURCE)


def test_transition_rejects_pause_states(core, task):
    start_lead(core, task.id)
    core.transition(task.id, TaskState.PLANNING, LEAD)
    for pause in (TaskState.PAUSED, TaskState.RECOVERING, TaskState.OWNER_APPROVAL_REQUIRED):
        with pytest.raises(InvalidTransition):
            core.transition(task.id, pause, LEAD)


def test_invalid_transition_no_state_change_no_event(core, task):
    start_lead(core, task.id)
    before = core.queries.get_task(task.id)
    events_before = len(core.list_events(OWNER_SOURCE))
    with pytest.raises(InvalidTransition):
        core.transition(task.id, TaskState.DONE, LEAD)  # NEW -> DONE invalid
    after = core.queries.get_task(task.id)
    assert after.state is before.state
    assert len(core.list_events(OWNER_SOURCE)) == events_before


def test_valid_transition_emits_exactly_one_state_changed(core, task):
    start_lead(core, task.id)
    core.transition(task.id, TaskState.PLANNING, LEAD)
    assert len(events_of(core, "task.state_changed", task.id)) == 1


def test_full_happy_path(core, task):
    start_lead(core, task.id)
    path = [
        TaskState.PLANNING,
        TaskState.ANALYZING,
        TaskState.LEAD_DECISION,
        TaskState.IMPLEMENTING,
        TaskState.TESTING,
        TaskState.REVIEWING,
        TaskState.FINAL_DECISION,
        TaskState.DONE,
    ]
    for target in path:
        core.transition(task.id, target, LEAD)
    assert core.queries.get_task(task.id).state is TaskState.DONE
    assert len(events_of(core, "task.state_changed", task.id)) == 8
    assert len(events_of(core, "task.completed", task.id)) == 1


def test_rework_loop(core, task):
    start_lead(core, task.id)
    for target in [TaskState.PLANNING, TaskState.ANALYZING, TaskState.LEAD_DECISION,
                   TaskState.IMPLEMENTING, TaskState.TESTING, TaskState.REVIEWING,
                   TaskState.REWORK, TaskState.PLANNING]:
        core.transition(task.id, target, LEAD)
    assert core.queries.get_task(task.id).state is TaskState.PLANNING


def test_escape_transitions_persist(core, task):
    start_lead(core, task.id)
    core.transition(task.id, TaskState.BLOCKED, LEAD)
    assert core.queries.get_task(task.id).state is TaskState.BLOCKED
