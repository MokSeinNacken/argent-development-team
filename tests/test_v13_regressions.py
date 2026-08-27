"""Regression tests for the verified closing findings (SPEC V1.3 13.4).

Each test reproduces one closing finding and proves the V1.3 fix:

(a) Recovery must never re-enter a pause state as a resume target: a task in
    ``RECOVERING`` with ``resume_state`` pointing at ``OWNER_APPROVAL_REQUIRED``
    or ``PAUSED`` is moved to ``BLOCKED`` (13.1); a healthy resume target still
    resumes and the rest of the recovery continues.
(c) ``fail_role`` records a deterministic handoff (``from_role`` -> the
    ``DEFAULT_NEXT_ROLE`` target) plus a ``handoff.created`` event, and the next
    ``start_role`` must match that handoff (13.3).

The expiry-lapse E2E test (13.2) lives in ``test_v12_regressions.py``.
"""

import sqlite3

import pytest

from argent_core import (
    Core,
    Role,
    RoleConflict,
    TaskState,
    OWNER_SOURCE,
)

from conftest import LEAD, events_of, start_lead

OWNER = OWNER_SOURCE


def _force_state(db_path, task_id, state, resume_state=None):
    """Inject an unreachable/corrupt task state directly (recovery scenarios).

    ``RECOVERING`` is not reachable through any public command; these tests
    place a task there to exercise the recovery resume-target validation.
    """
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE tasks SET state = ?, resume_state = ? WHERE id = ?",
        (state, resume_state, task_id),
    )
    conn.commit()
    conn.close()


# ------------------------------------------------------------------ 13.4 (a)


@pytest.mark.parametrize(
    "resume_state",
    [TaskState.OWNER_APPROVAL_REQUIRED, TaskState.PAUSED],
)
def test_recovery_rejects_pause_resume_target(db_path, core, task, resume_state):
    _force_state(db_path, task.id, "RECOVERING", resume_state.value)
    core.recover(OWNER)
    assert core.queries.get_task(task.id).state is TaskState.BLOCKED
    assert core.queries.get_task(task.id).resume_state is None


def test_recovery_rejects_recovering_resume_target(db_path, core, task):
    # A self-loop resume target (RECOVERING is itself a pause state) is invalid.
    _force_state(db_path, task.id, "RECOVERING", "RECOVERING")
    core.recover(OWNER)
    assert core.queries.get_task(task.id).state is TaskState.BLOCKED


def test_recovery_healthy_resume_target_and_rest_continue(db_path, core, project, task):
    # A healthy RECOVERING task resumes to its target; an unrelated task is left
    # untouched and the recovery completes without error.
    task2 = core.create_task(project.id, "other", OWNER)
    _force_state(db_path, task.id, "RECOVERING", "TESTING")
    core.recover(OWNER)
    assert core.queries.get_task(task.id).state is TaskState.TESTING
    assert core.queries.get_task(task2.id).state is TaskState.NEW


# ------------------------------------------------------------------ 13.4 (c)


def test_fail_role_creates_deterministic_handoff(core, task):
    rr = start_lead(core, task.id)
    core.fail_role(rr.id, LEAD)
    handoffs = core.queries.list_handoffs(task.id)
    assert len(handoffs) == 1
    assert handoffs[0].from_role is Role.LEAD
    assert handoffs[0].to_role is Role.ANALYST
    assert len(events_of(core, "handoff.created", task.id)) == 1
    assert len(events_of(core, "role.failed", task.id)) == 1


def test_fail_role_handoff_enforced_on_next_start(core, task):
    rr = start_lead(core, task.id)
    core.fail_role(rr.id, LEAD)
    # Restarting the failed role is blocked; the handoff target is allowed.
    with pytest.raises(RoleConflict):
        core.start_role(task.id, Role.LEAD, LEAD)
    core.start_role(task.id, Role.ANALYST, LEAD)
