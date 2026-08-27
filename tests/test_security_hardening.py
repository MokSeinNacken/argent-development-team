"""Regression tests for the verified security findings R1–R15 (SPEC V1.1 11.7).

Each test reproduces one finding and proves the fix.  A test that passed before
the fix would have failed against the pre-V1.1 implementation.
"""

import sqlite3

import pytest

from argent_core import (
    ActionExecutionStatus,
    ApprovalError,
    ApprovalStatus,
    Core,
    ForbiddenAction,
    IdempotencyError,
    InvalidTransition,
    OwnerAuthorityRequired,
    PermissionDenied,
    Role,
    RoleConflict,
    TaskState,
    UntrustedSource,
    role_source,
    OWNER_SOURCE,
)
from argent_core.store import Store

from conftest import LEAD, IMPLEMENTER, events_of, pipeline_to, start_lead

OWNER = OWNER_SOURCE


def _bind(task_id, action="deploy_production", scope="prod"):
    return dict(task_id=task_id, action=action, scope=scope)


def _set_task_state(db_path, task_id, state, resume_state=None):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE tasks SET state = ?, resume_state = ? WHERE id = ?",
        (state, resume_state, task_id),
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- R1


def test_R1_transition_cannot_leave_owner_approval_required(core, task):
    start_lead(core, task.id)
    core.transition(task.id, TaskState.PLANNING, LEAD)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    assert core.queries.get_task(task.id).state is TaskState.OWNER_APPROVAL_REQUIRED
    with pytest.raises(InvalidTransition):
        core.transition(task.id, TaskState.PLANNING, LEAD)  # resume_state escape
    assert core.queries.get_task(task.id).state is TaskState.OWNER_APPROVAL_REQUIRED


# --------------------------------------------------------------------------- R2


def test_R2_execute_approved_does_not_consume_pending(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    with pytest.raises(ApprovalError):
        core.execute_approved(res.approval.id, OWNER, **_bind(task.id))
    assert core.queries.get_approval(res.approval.id).status is ApprovalStatus.PENDING


# --------------------------------------------------------------------------- R3


def test_R3_role_source_cannot_create_project(core):
    with pytest.raises(OwnerAuthorityRequired):
        core.create_project("p", LEAD)


def test_R3_role_source_cannot_create_task(core, project):
    with pytest.raises(OwnerAuthorityRequired):
        core.create_task(project.id, "t", LEAD)


def test_R3_role_source_cannot_approve(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    with pytest.raises(OwnerAuthorityRequired):
        core.approve(res.approval.id, LEAD, **_bind(task.id))
    assert core.queries.get_approval(res.approval.id).status is ApprovalStatus.PENDING


def test_R3_role_source_cannot_reject(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    with pytest.raises(OwnerAuthorityRequired):
        core.reject(res.approval.id, LEAD, **_bind(task.id))


def test_R3_role_source_cannot_execute(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    core.approve(res.approval.id, OWNER, **_bind(task.id))
    with pytest.raises(OwnerAuthorityRequired):
        core.execute_approved(res.approval.id, LEAD, **_bind(task.id))
    assert core.queries.get_approval(res.approval.id).status is ApprovalStatus.APPROVED


def test_R3_role_source_cannot_recover(core, task):
    with pytest.raises(OwnerAuthorityRequired):
        core.recover(LEAD)


# --------------------------------------------------------------------------- R4


def test_R4_no_gate_from_done(core, task):
    start_lead(core, task.id)
    for s in [TaskState.PLANNING, TaskState.ANALYZING, TaskState.LEAD_DECISION,
              TaskState.IMPLEMENTING, TaskState.TESTING, TaskState.REVIEWING,
              TaskState.FINAL_DECISION, TaskState.DONE]:
        core.transition(task.id, s, LEAD)
    with pytest.raises(InvalidTransition):
        core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    assert core.queries.list_approvals(task.id) == []


def test_R4_no_double_gate(core, task):
    start_lead(core, task.id)
    core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    assert core.queries.get_task(task.id).state is TaskState.OWNER_APPROVAL_REQUIRED
    with pytest.raises(InvalidTransition):
        core.request_action(task.id, "promote_stable", "prod", Role.LEAD, LEAD)
    assert len(core.queries.list_approvals(task.id)) == 1


# --------------------------------------------------------------------------- R5


def test_R5_record_decision_requires_active_lead(core, task):
    with pytest.raises(PermissionDenied):
        core.record_decision(task.id, "x", LEAD)


def test_R5_role_source_must_match(core, task):
    start_lead(core, task.id)
    with pytest.raises(PermissionDenied):
        core.record_decision(task.id, "x", role_source(Role.QA))


def test_R5_request_action_bound_to_active_role(core, task):
    start_lead(core, task.id)  # active role is lead
    with pytest.raises(PermissionDenied):
        core.request_action(task.id, "implement", "src", Role.IMPLEMENTER,
                            role_source(Role.IMPLEMENTER))


def test_R5_role_source_cannot_start_role(core, task):
    with pytest.raises(PermissionDenied):
        core.start_role(task.id, Role.LEAD, role_source(Role.QA))


# --------------------------------------------------------------------------- R6


def test_R6_recovery_does_not_leave_owner_approval_required(db_path, core, task):
    _set_task_state(db_path, task.id, "OWNER_APPROVAL_REQUIRED", "REVIEWING")
    core.recover(OWNER)
    assert core.queries.get_task(task.id).state is TaskState.OWNER_APPROVAL_REQUIRED


def test_R6_recovery_does_not_leave_done(db_path, core, task):
    _set_task_state(db_path, task.id, "DONE", None)
    core.recover(OWNER)
    assert core.queries.get_task(task.id).state is TaskState.DONE


def test_R6_recovery_does_not_leave_paused(db_path, core, task):
    _set_task_state(db_path, task.id, "PAUSED", "REVIEWING")
    core.recover(OWNER)
    assert core.queries.get_task(task.id).state is TaskState.PAUSED


# --------------------------------------------------------------------------- R7


def test_R7_binding_args_required(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    with pytest.raises(TypeError):
        core.approve(res.approval.id, OWNER)
    with pytest.raises(TypeError):
        core.execute_approved(res.approval.id, OWNER)


def test_R7_full_binding_enforced_on_approve(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    with pytest.raises(ApprovalError):
        core.approve(res.approval.id, OWNER, task_id=task.id,
                     action="wrong_action", scope="prod")
    with pytest.raises(ApprovalError):
        core.approve(res.approval.id, OWNER, task_id="wrong", action="deploy_production",
                     scope="prod")
    assert core.queries.get_approval(res.approval.id).status is ApprovalStatus.PENDING


def test_R7_full_binding_enforced_on_execute(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    core.approve(res.approval.id, OWNER, **_bind(task.id))
    with pytest.raises(ApprovalError):
        core.execute_approved(res.approval.id, OWNER, task_id=task.id,
                              action="deploy_production", scope="wrong_scope")
    assert core.queries.get_approval(res.approval.id).status is ApprovalStatus.APPROVED


# --------------------------------------------------------------------------- R8


def test_R8_core_store_removed(core):
    assert not hasattr(core, "store")
    assert hasattr(core, "queries")


def test_R8_queries_is_read_only(core):
    q = core.queries
    for name in dir(q):
        if name.startswith("_"):
            continue
        assert name.startswith("get_") or name.startswith("list_"), name


def test_R8_store_connection_and_mutators_private():
    assert not hasattr(Store, "conn")
    assert not hasattr(Store, "insert_task")
    assert not hasattr(Store, "update_task_state")
    assert not hasattr(Store, "insert_role_run")
    assert not hasattr(Store, "consume_approval")
    assert not hasattr(Store, "insert_event")
    assert not hasattr(Store, "set_command_idempotency")
    assert not hasattr(Store, "transaction")
    assert hasattr(Store, "get_task")
    assert hasattr(Store, "list_approvals")


# --------------------------------------------------------------------------- R9


def test_R9_same_key_different_args_raises(core, project):
    core.create_task(project.id, "a", OWNER, idempotency_key="r9-key")
    with pytest.raises(IdempotencyError):
        core.create_task(project.id, "b", OWNER, idempotency_key="r9-key")


def test_R9_autonomous_goes_through_wrapper(core, task):
    pipeline_to(core, task.id, Role.IMPLEMENTER)
    r1 = core.request_action(task.id, "implement", "src", Role.IMPLEMENTER,
                             role_source(Role.IMPLEMENTER), idempotency_key="r9-a")
    r2 = core.request_action(task.id, "implement", "src", Role.IMPLEMENTER,
                             role_source(Role.IMPLEMENTER), idempotency_key="r9-a")
    assert r1.execution_id == r2.execution_id
    assert len(core.queries.list_action_executions(task.id)) == 1


def test_R9_forbidden_goes_through_wrapper(core, task):
    start_lead(core, task.id)
    for _ in range(2):
        with pytest.raises(ForbiddenAction):
            core.request_action(task.id, "exfiltrate_data", "prod", Role.LEAD, LEAD,
                                idempotency_key="r9-f")
    assert len(core.queries.list_action_executions(task.id)) == 1
    assert len(events_of(core, "lead.decision", task.id)) == 1


def test_R9_recover_idempotent(core, task):
    start_lead(core, task.id)
    r1 = core.recover(OWNER, idempotency_key="r9-rec")
    r2 = core.recover(OWNER, idempotency_key="r9-rec")
    assert r1.interrupted_role_runs == 1
    assert r2.interrupted_role_runs == 0


# --------------------------------------------------------------------------- R10


def test_R10_approved_expired_not_consumable(tmp_path):
    from datetime import datetime, timedelta, timezone

    class Clock:
        def __init__(self):
            self.t = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        def __call__(self):
            return self.t

    clock = Clock()
    c = Core(str(tmp_path / "r10.db"), clock=clock)
    p = c.create_project("p", OWNER)
    task = c.create_task(p.id, "t", OWNER)
    c.start_role(task.id, Role.LEAD, LEAD)
    res = c.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    c.approve(res.approval.id, OWNER, **_bind(task.id))
    clock.t += timedelta(hours=2)
    with pytest.raises(ApprovalError):
        c.execute_approved(res.approval.id, OWNER, **_bind(task.id))
    # R10 + V1.2 12.3: not consumed, atomically marked 'expired'.
    assert c.queries.get_approval(res.approval.id).status is ApprovalStatus.EXPIRED
    c.close()


# --------------------------------------------------------------------------- R11


def test_R11_recovery_no_milestone_rollback(core, task):
    # Complete the lead (a milestone), move the task forward, then leave an
    # in-flight role run.  Recovery V2 must NOT roll the task back to the
    # milestone state.
    rr = start_lead(core, task.id)
    for s in [TaskState.PLANNING, TaskState.ANALYZING, TaskState.LEAD_DECISION,
              TaskState.IMPLEMENTING, TaskState.TESTING]:
        core.transition(task.id, s, LEAD)
    core.complete_role(rr.id, LEAD)  # completed lead milestone
    core.start_role(task.id, Role.ANALYST, LEAD)  # in-flight
    core.recover(OWNER)
    # No rollback to LEAD_DECISION; the task stays in TESTING.
    assert core.queries.get_task(task.id).state is TaskState.TESTING


# --------------------------------------------------------------------------- R12


def test_R12_next_role_parameter_removed(core, task):
    rr = start_lead(core, task.id)
    with pytest.raises(TypeError):
        core.complete_role(rr.id, LEAD, next_role=Role.QA)


def test_R12_handoff_enforced_on_start(core, task):
    rr = start_lead(core, task.id)
    core.complete_role(rr.id, LEAD)  # handoff lead -> analyst
    with pytest.raises(RoleConflict):
        core.start_role(task.id, Role.QA, LEAD)  # skipping the handoff target
    core.start_role(task.id, Role.ANALYST, LEAD)


# --------------------------------------------------------------------------- R13


def test_R13_autonomous_execution_persisted(core, task):
    pipeline_to(core, task.id, Role.IMPLEMENTER)
    res = core.request_action(task.id, "implement", "src", Role.IMPLEMENTER,
                              role_source(Role.IMPLEMENTER))
    ex = core.queries.get_action_execution(res.execution_id)
    assert ex is not None
    assert ex.status is ActionExecutionStatus.EXECUTED
    assert ex.action == "implement"


def test_R13_approved_execution_persisted(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    core.approve(res.approval.id, OWNER, **_bind(task.id))
    core.execute_approved(res.approval.id, OWNER, **_bind(task.id))
    execs = core.queries.list_action_executions(task.id)
    assert len(execs) == 1
    assert execs[0].status is ActionExecutionStatus.EXECUTED
    assert execs[0].approval_id == res.approval.id


def test_R13_forbidden_blocked_persisted(core, task):
    start_lead(core, task.id)
    with pytest.raises(ForbiddenAction):
        core.request_action(task.id, "exfiltrate_data", "prod", Role.LEAD, LEAD)
    execs = core.queries.list_action_executions(task.id)
    assert len(execs) == 1
    assert execs[0].status is ActionExecutionStatus.BLOCKED
    assert core.queries.list_approvals(task.id) == []


# --------------------------------------------------------------------------- R14


def test_R14_check_constraints_and_partial_index(db_path, core):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            r["name"]: r["sql"]
            for r in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table'"
            )
        }
        indexes = {
            r["name"]: r["sql"]
            for r in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index'"
            )
        }
    finally:
        conn.close()
    assert "CHECK" in tables["tasks"]
    assert "CHECK" in tables["role_runs"]
    assert "CHECK" in tables["owner_approvals"]
    assert "CHECK" in tables["action_executions"]
    assert "idx_role_runs_active" in indexes
    assert "WHERE status = 'started'" in indexes["idx_role_runs_active"]
    assert "action_executions" in tables


def test_R14_args_hash_column(db_path, core):
    conn = sqlite3.connect(db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(command_idempotency)")}
    finally:
        conn.close()
    assert "args_hash" in cols


# --------------------------------------------------------------------------- R15


def test_R15_unknown_target_state_is_invalid_transition(core, task):
    with pytest.raises(InvalidTransition):
        core.transition(task.id, "NONSENSE", OWNER)


def test_R15_unknown_state_not_value_error(core, task):
    try:
        core.transition(task.id, "NONSENSE", OWNER)
    except InvalidTransition:
        pass
    except ValueError:
        pytest.fail("unknown state raised ValueError instead of InvalidTransition")
    else:
        pytest.fail("unknown state did not raise")


# ---------------------------------------------------------------- resume (R1)


def test_resume_paused_to_resume_state(db_path, core, task):
    start_lead(core, task.id)
    _set_task_state(db_path, task.id, "PAUSED", "ANALYZING")
    core.resume(task.id, LEAD)
    assert core.queries.get_task(task.id).state is TaskState.ANALYZING


def test_resume_rejects_non_paused(core, task):
    start_lead(core, task.id)
    with pytest.raises(InvalidTransition):
        core.resume(task.id, LEAD)


def test_resume_is_lead_only(db_path, core, task):
    _set_task_state(db_path, task.id, "PAUSED", "ANALYZING")
    with pytest.raises(PermissionDenied):
        core.resume(task.id, LEAD)  # no active lead run


def test_start_role_forbidden_in_terminal_state(core, task):
    start_lead(core, task.id)
    for s in [TaskState.PLANNING, TaskState.ANALYZING, TaskState.LEAD_DECISION,
              TaskState.IMPLEMENTING, TaskState.TESTING, TaskState.REVIEWING,
              TaskState.FINAL_DECISION, TaskState.DONE]:
        core.transition(task.id, s, LEAD)
    with pytest.raises(InvalidTransition):
        core.start_role(task.id, Role.ANALYST, LEAD)


def test_start_role_forbidden_in_pause_state(db_path, core, task):
    start_lead(core, task.id)
    _set_task_state(db_path, task.id, "PAUSED", "ANALYZING")
    with pytest.raises(InvalidTransition):
        core.start_role(task.id, Role.ANALYST, LEAD)
