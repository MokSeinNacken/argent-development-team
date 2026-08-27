"""Deterministic, fail-closed task state machine (SPEC V1 chapter 1 + V1.1 11.1).

V1.1 hardenings (R1, R4, R6, R15):

- Gate entry into ``OWNER_APPROVAL_REQUIRED`` is a valid transition from every
  non-terminal, non-pause state.  It is *not* allowed from ``DONE``/``CANCELLED``
  and *not* out of any pause state (no self-loop, no double gate).
- Leaving a pause state is only possible via the dedicated commands:
  ``execute_approved`` (``OWNER_APPROVAL_REQUIRED -> resume_state``),
  ``reject`` (``OWNER_APPROVAL_REQUIRED -> BLOCKED``), ``CANCELLED``,
  ``resume`` (``PAUSED -> resume_state``) and ``recover``
  (``RECOVERING -> resume_state | BLOCKED``).
- The public ``transition()`` command rejects pause states (reserved for the
  dedicated commands); it may still reach ``BLOCKED``/``CANCELLED``.
- Unknown from/to states raise :class:`InvalidTransition` (never ``ValueError``).

V1.2 hardenings (12.1): dynamic resume targets must be non-terminal AND
non-pause states; ``resume_state`` pointing at a pause state is invalid.
"""

from __future__ import annotations

from typing import Optional

from .models import InvalidTransition, TaskState

# Main-path states in canonical order.
MAIN_PATH: tuple[TaskState, ...] = (
    TaskState.NEW,
    TaskState.PLANNING,
    TaskState.ANALYZING,
    TaskState.LEAD_DECISION,
    TaskState.IMPLEMENTING,
    TaskState.TESTING,
    TaskState.REVIEWING,
    TaskState.FINAL_DECISION,
    TaskState.DONE,
)

MAIN_PATH_INDEX: dict[TaskState, int] = {s: i for i, s in enumerate(MAIN_PATH)}

# Terminal states have no outgoing transitions at all.
TERMINAL_STATES = frozenset({TaskState.DONE, TaskState.CANCELLED})

# States that carry a resume point while they are active.
PAUSE_STATES = frozenset(
    {
        TaskState.OWNER_APPROVAL_REQUIRED,
        TaskState.PAUSED,
        TaskState.RECOVERING,
    }
)

# Explicitly listed static transitions (main path + additional states).
_STATIC: dict[TaskState, frozenset[TaskState]] = {
    TaskState.NEW: frozenset({TaskState.PLANNING}),
    TaskState.PLANNING: frozenset({TaskState.ANALYZING}),
    TaskState.ANALYZING: frozenset({TaskState.LEAD_DECISION}),
    TaskState.LEAD_DECISION: frozenset({TaskState.IMPLEMENTING, TaskState.REWORK}),
    TaskState.IMPLEMENTING: frozenset({TaskState.TESTING}),
    TaskState.TESTING: frozenset({TaskState.REVIEWING, TaskState.REWORK}),
    TaskState.REVIEWING: frozenset({TaskState.FINAL_DECISION, TaskState.REWORK}),
    TaskState.FINAL_DECISION: frozenset({TaskState.DONE, TaskState.REWORK}),
    TaskState.REWORK: frozenset({TaskState.PLANNING, TaskState.IMPLEMENTING}),
    TaskState.BLOCKED: frozenset({TaskState.RECOVERING}),
    TaskState.FAILED: frozenset({TaskState.RECOVERING, TaskState.CANCELLED}),
}

# Static exits out of pause states that the dedicated commands are allowed to
# perform (besides the dynamic ``-> resume_state`` transition).
_PAUSE_EXITS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.OWNER_APPROVAL_REQUIRED: frozenset(
        {TaskState.BLOCKED, TaskState.CANCELLED}
    ),
    TaskState.PAUSED: frozenset({TaskState.CANCELLED}),
    TaskState.RECOVERING: frozenset({TaskState.BLOCKED}),
}

# General escape rule (V1.1): every non-terminal, non-pause state may move to
# BLOCKED or CANCELLED.  PAUSED is reserved and cannot be entered by any
# command, so it is deliberately not part of the escape targets.
_ESCAPE_TARGETS = frozenset({TaskState.BLOCKED, TaskState.CANCELLED})


def is_terminal(state: TaskState) -> bool:
    return state in TERMINAL_STATES


def is_pause(state: TaskState) -> bool:
    return state in PAUSE_STATES


# States in which a gated action may be requested (SPEC V1.2 12.2): the main
# path (NEW..FINAL_DECISION, i.e. MAIN_PATH without the terminal DONE) plus
# REWORK.  BLOCKED, FAILED, pause states and terminal states are never
# actionable (fail-closed).
_ACTIONABLE_STATES: frozenset[TaskState] = frozenset(
    set(MAIN_PATH) - TERMINAL_STATES | {TaskState.REWORK}
)


def is_actionable(state: TaskState) -> bool:
    """Return True iff ``state`` allows requesting a gated action.

    Only the main-path states (NEW..FINAL_DECISION) and REWORK are actionable
    (SPEC V1.2 12.2, supervisor decision: BLOCKED/FAILED are not actionable).
    """
    return state in _ACTIONABLE_STATES


def is_valid_resume_target(resume_state: Optional[TaskState]) -> bool:
    """Return True iff ``resume_state`` is a valid dynamic resume target.

    A resume target must be a concrete, non-terminal AND non-pause state
    (SPEC V1.2 12.1, V1.3 13.1).  This is the single source of truth reused by
    ``is_allowed`` and by the recovery logic in ``recovery.recovery_target``.
    """
    return (
        isinstance(resume_state, TaskState)
        and resume_state not in TERMINAL_STATES
        and resume_state not in PAUSE_STATES
    )


def is_allowed(
    from_state: TaskState,
    to_state: TaskState,
    resume_state: Optional[TaskState] = None,
) -> bool:
    """Return ``True`` iff ``from_state -> to_state`` is permitted.

    ``resume_state`` resolves the dynamic ``-> resume_state`` transitions out of
    the pause states.
    """
    if not isinstance(from_state, TaskState) or not isinstance(to_state, TaskState):
        raise InvalidTransition(
            f"unknown state: from={from_state!r} to={to_state!r}"
        )
    if from_state == to_state:
        return False
    if is_terminal(from_state):
        return False

    # Pause states have restricted exits (dedicated commands only).
    if from_state in PAUSE_STATES:
        if (
            resume_state is not None
            and to_state == resume_state
            and is_valid_resume_target(resume_state)
            and resume_state is not from_state
        ):
            return True
        return to_state in _PAUSE_EXITS.get(from_state, frozenset())

    # Non-pause, non-terminal state.
    if to_state in _STATIC.get(from_state, frozenset()):
        return True
    if to_state is TaskState.OWNER_APPROVAL_REQUIRED:
        return True  # gate entry
    if to_state in _ESCAPE_TARGETS:
        return True
    return False


def validate_transition(
    from_state: TaskState,
    to_state: TaskState,
    resume_state: Optional[TaskState] = None,
) -> None:
    """Raise :class:`InvalidTransition` unless the transition is allowed."""
    if not is_allowed(from_state, to_state, resume_state):
        raise InvalidTransition(
            f"invalid transition: {from_state.value} -> {to_state.value}"
        )
