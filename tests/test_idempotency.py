"""Idempotency & recovery tests (SPEC V1 chapter 7 + V1.1 11.6)."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from argent_core import (
    ActionExecutionStatus,
    ApprovalError,
    Core,
    IdempotencyError,
    Role,
    RoleRunStatus,
    TaskState,
    ForbiddenAction,
    role_source,
    OWNER_SOURCE,
)

from conftest import LEAD, events_of, pipeline_to, start_lead

OWNER = OWNER_SOURCE


def _force_state(db_path, task_id, state, resume_state=None):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE tasks SET state = ?, resume_state = ? WHERE id = ?",
        (state, resume_state, task_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- recovery


def test_recover_interrupted_role_run(core, task):
    rr = start_lead(core, task.id)
    report = core.recover(OWNER)
    assert report.interrupted_role_runs == 1
    assert core.queries.get_role_run(rr.id).status is RoleRunStatus.FAILED


def test_recover_interrupted_task_run(core, task):
    tr = core.start_task_run(task.id, OWNER)
    report = core.recover(OWNER)
    assert report.interrupted_task_runs == 1
    assert core.queries.get_task_run(tr.id).status.value == "failed"


def test_recover_leaves_normal_task_unchanged(core, task):
    # V2 (R11): a task in a normal state with completed roles is NOT rolled
    # back based on milestones.
    start_lead(core, task.id)
    core.transition(task.id, TaskState.PLANNING, LEAD)
    rr = core.complete_role(core.queries.get_active_role_run(task.id).id, LEAD)
    core.recover(OWNER)
    assert core.queries.get_task(task.id).state is TaskState.PLANNING


def test_recover_recovering_resumes(db_path, core, task):
    _force_state(db_path, task.id, "RECOVERING", "TESTING")
    core.recover(OWNER)
    assert core.queries.get_task(task.id).state is TaskState.TESTING


def test_recover_recovering_without_resume_blocks(db_path, core, task):
    _force_state(db_path, task.id, "RECOVERING", None)
    core.recover(OWNER)
    assert core.queries.get_task(task.id).state is TaskState.BLOCKED


def test_recover_recovering_terminal_resume_blocks(db_path, core, task):
    # A terminal resume_state is invalid -> BLOCKED.
    _force_state(db_path, task.id, "RECOVERING", "DONE")
    core.recover(OWNER)
    assert core.queries.get_task(task.id).state is TaskState.BLOCKED


def test_recover_does_not_leave_owner_approval_required(db_path, core, task):
    # R6: OWNER_APPROVAL_REQUIRED must not be exited by recovery.
    _force_state(db_path, task.id, "OWNER_APPROVAL_REQUIRED", "REVIEWING")
    core.recover(OWNER)
    assert core.queries.get_task(task.id).state is TaskState.OWNER_APPROVAL_REQUIRED


def test_recover_does_not_leave_done(db_path, core, task):
    # R6: terminal states must not be exited by recovery.
    _force_state(db_path, task.id, "DONE", None)
    core.recover(OWNER)
    assert core.queries.get_task(task.id).state is TaskState.DONE


def test_recovery_events(core, task):
    start_lead(core, task.id)
    core.recover(OWNER)
    assert len(events_of(core, "system.recovery_started")) == 1
    assert len(events_of(core, "system.recovery_completed")) == 1


def test_subprocess_crash_recovery(db_path, project):
    c = Core(db_path)
    task = c.create_task(project.id, "crash-task", OWNER)
    c.close()

    helper = str(Path(__file__).parent / "crash_helper.py")
    env = dict(os.environ)
    proc = subprocess.run([sys.executable, helper, db_path, task.id], env=env)
    assert proc.returncode == 1

    c2 = Core(db_path)
    active = c2.queries.list_role_runs(task.id, status=RoleRunStatus.STARTED)
    assert len(active) == 1
    assert c2.queries.get_task(task.id).state is TaskState.NEW  # uncommitted rolled back
    c2.recover(OWNER)
    assert c2.queries.get_role_run(active[0].id).status is RoleRunStatus.FAILED
    assert c2.queries.get_task(task.id).state is TaskState.NEW  # V2: unchanged
    c2.close()


# ---------------------------------------------------------------- idempotency


def test_create_task_idempotent(core, project):
    t1 = core.create_task(project.id, "same", OWNER, idempotency_key="k1")
    t2 = core.create_task(project.id, "same", OWNER, idempotency_key="k1")
    assert t1.id == t2.id
    assert len(core.queries.list_tasks()) == 1
    assert len(events_of(core, "task.created", t1.id)) == 1


def test_transition_idempotent(core, task):
    start_lead(core, task.id)
    core.transition(task.id, TaskState.PLANNING, LEAD, idempotency_key="k2")
    core.transition(task.id, TaskState.PLANNING, LEAD, idempotency_key="k2")
    assert core.queries.get_task(task.id).state is TaskState.PLANNING
    assert len(events_of(core, "task.state_changed", task.id)) == 1


def test_start_role_idempotent(core, task):
    r1 = core.start_role(task.id, Role.LEAD, LEAD, idempotency_key="k3")
    r2 = core.start_role(task.id, Role.LEAD, LEAD, idempotency_key="k3")
    assert r1.id == r2.id
    assert len(core.queries.list_role_runs(task.id)) == 1


def test_add_finding_idempotent(core, task):
    pipeline_to(core, task.id, Role.QA)
    f1 = core.add_finding(task.id, "high", "x", role_source(Role.QA), idempotency_key="k4")
    f2 = core.add_finding(task.id, "high", "x", role_source(Role.QA), idempotency_key="k4")
    assert f1.id == f2.id
    assert len(core.queries.list_findings(task.id)) == 1
    assert len(events_of(core, "finding.created", task.id)) == 1


def test_approve_idempotent(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    a1 = core.approve(res.approval.id, OWNER, task_id=task.id,
                      action="deploy_production", scope="prod", idempotency_key="k5")
    a2 = core.approve(res.approval.id, OWNER, task_id=task.id,
                      action="deploy_production", scope="prod", idempotency_key="k5")
    assert a1.id == a2.id
    assert a2.status.value == "approved"
    assert len(events_of(core, "gate.owner_approved", task.id)) == 1


def test_approve_with_different_key_not_idempotent(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    core.approve(res.approval.id, OWNER, task_id=task.id,
                 action="deploy_production", scope="prod")
    with pytest.raises(ApprovalError):
        core.approve(res.approval.id, OWNER, task_id=task.id,
                     action="deploy_production", scope="prod")


def test_autonomous_request_idempotent(core, task):
    pipeline_to(core, task.id, Role.IMPLEMENTER)
    r1 = core.request_action(task.id, "implement", "src", Role.IMPLEMENTER,
                             role_source(Role.IMPLEMENTER), idempotency_key="k7")
    r2 = core.request_action(task.id, "implement", "src", Role.IMPLEMENTER,
                             role_source(Role.IMPLEMENTER), idempotency_key="k7")
    assert r1.execution_id == r2.execution_id
    assert len(core.queries.list_action_executions(task.id)) == 1


def test_forbidden_request_idempotent(core, task):
    start_lead(core, task.id)
    with pytest.raises(ForbiddenAction):
        core.request_action(task.id, "exfiltrate_data", "prod", Role.LEAD, LEAD,
                            idempotency_key="k8")
    with pytest.raises(ForbiddenAction):
        core.request_action(task.id, "exfiltrate_data", "prod", Role.LEAD, LEAD,
                            idempotency_key="k8")
    assert len(core.queries.list_action_executions(task.id)) == 1
    assert len(events_of(core, "lead.decision", task.id)) == 1


def test_same_key_different_args_raises(core, project):
    core.create_task(project.id, "title-a", OWNER, idempotency_key="k9")
    with pytest.raises(IdempotencyError):
        core.create_task(project.id, "title-b", OWNER, idempotency_key="k9")


def test_recover_idempotent(core, task):
    start_lead(core, task.id)
    r1 = core.recover(OWNER, idempotency_key="k10")
    r2 = core.recover(OWNER, idempotency_key="k10")
    assert r1.interrupted_role_runs == 1
    assert r2.interrupted_role_runs == 0
    assert core.queries.get_role_run(
        core.queries.list_role_runs(task.id)[0].id
    ).status is RoleRunStatus.FAILED


def test_repeat_command_no_double_event(core, task):
    pipeline_to(core, task.id, Role.QA)
    core.record_test_run(task.id, "passed", role_source(Role.QA), idempotency_key="k6")
    core.record_test_run(task.id, "passed", role_source(Role.QA), idempotency_key="k6")
    assert len(events_of(core, "test.completed", task.id)) == 1
    assert len(events_of(core, "test.started", task.id)) == 1
