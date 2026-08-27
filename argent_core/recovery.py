"""Recovery helpers (SPEC V1.1 chapter 11.6, R6, R11; V1.3 13.1).

Conservative recovery V2: no milestone computation from role runs/events.  Only
tasks that are already in ``RECOVERING`` are moved — to their validated
resume target (non-terminal AND non-pause), or to ``BLOCKED`` otherwise.  Every
other task state is left unchanged (in particular ``OWNER_APPROVAL_REQUIRED``,
``PAUSED``, ``BLOCKED``, ``DONE`` and ``CANCELLED``).

The resume-target validation is reused from the state machine
(``is_valid_resume_target``), the single source of truth (SPEC V1.3 13.1).
"""

from __future__ import annotations

from .models import Task, TaskState
from .state_machine import is_valid_resume_target


def recovery_target(task: Task) -> TaskState:
    """Resolve the recovery target for a single task (conservative V2).

    Only a task in ``RECOVERING`` may change state.  It moves to its
    ``resume_state`` when that state is a valid resume target — non-terminal
    AND non-pause (``PAUSE_STATES`` fully excluded, including
    ``OWNER_APPROVAL_REQUIRED`` and ``PAUSED``) — otherwise to ``BLOCKED``.
    All other states are returned unchanged.
    """
    if task.state is not TaskState.RECOVERING:
        return task.state
    rs = task.resume_state
    if rs is not None and is_valid_resume_target(rs):
        return rs
    return TaskState.BLOCKED
