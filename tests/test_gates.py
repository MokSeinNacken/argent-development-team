"""Gated autonomy tests (SPEC V1 chapter 3 + V1.1 11.3, test points 5-11)."""

from datetime import datetime, timedelta, timezone

import pytest

from argent_core import (
    ActionClass,
    ActionExecutionStatus,
    ApprovalError,
    ApprovalStatus,
    Core,
    ForbiddenAction,
    PermissionDenied,
    Role,
    TaskState,
    classify_action,
    role_source,
    OWNER_SOURCE,
)

from conftest import (
    LEAD,
    IMPLEMENTER,
    QA,
    events_of,
    pipeline_to,
    start_lead,
)

OWNER = OWNER_SOURCE


def _bind(task_id):
    return dict(task_id=task_id, action="deploy_production", scope="prod")


def test_classify_autonomous():
    for a in ["analyze", "implement", "run_tests", "review", "rework",
              "create_local_artifact", "git_local_commit", "create_handoff"]:
        assert classify_action(a) is ActionClass.AUTONOMOUS


def test_classify_owner_approval():
    for a in ["deploy_production", "change_secrets", "expose_gateway",
              "modify_allowlist", "promote_stable", "modify_policy",
              "external_send", "install_software", "raise_privileges",
              "enable_self_improvement", "production_write"]:
        assert classify_action(a) is ActionClass.OWNER_APPROVAL_REQUIRED


def test_classify_forbidden():
    for a in ["bypass_owner_approval", "forge_owner_approval",
              "treat_untrusted_as_owner_approval", "disclose_secrets",
              "disable_security_boundary", "exfiltrate_data"]:
        assert classify_action(a) is ActionClass.FORBIDDEN


def test_classify_unknown_is_forbidden():
    assert classify_action("totally_unknown_action") is ActionClass.FORBIDDEN


def test_autonomous_executed_for_implementer(core, task):
    pipeline_to(core, task.id, Role.IMPLEMENTER)
    res = core.request_action(task.id, "implement", "src", Role.IMPLEMENTER, IMPLEMENTER)
    assert res.allowed is True
    assert res.action_class is ActionClass.AUTONOMOUS
    # R13: persisted execution.
    ex = core.queries.get_action_execution(res.execution_id)
    assert ex is not None
    assert ex.status is ActionExecutionStatus.EXECUTED


def test_autonomous_qa_cannot_implement(core, task):
    pipeline_to(core, task.id, Role.QA)
    with pytest.raises(PermissionDenied):
        core.request_action(task.id, "implement", "src", Role.QA, QA)


def test_autonomous_lead_cannot_implement(core, task):
    start_lead(core, task.id)
    with pytest.raises(PermissionDenied):
        core.request_action(task.id, "implement", "src", Role.LEAD, LEAD)


def test_request_action_requires_active_role(core, task):
    # No active role run -> PermissionDenied.
    with pytest.raises(PermissionDenied):
        core.request_action(task.id, "implement", "src", Role.IMPLEMENTER, IMPLEMENTER)


def test_request_action_source_must_match_actor(core, task):
    pipeline_to(core, task.id, Role.IMPLEMENTER)
    # actor_role implementer but source role:qa -> mismatch.
    with pytest.raises(PermissionDenied):
        core.request_action(task.id, "implement", "src", Role.IMPLEMENTER, QA)
    # actor_role qa but source role:implementer -> mismatch.
    with pytest.raises(PermissionDenied):
        core.request_action(task.id, "implement", "src", Role.QA, IMPLEMENTER)


def test_owner_approval_required_blocks(core, task):
    start_lead(core, task.id)
    core.transition(task.id, TaskState.PLANNING, LEAD)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    assert res.allowed is False
    assert res.action_class is ActionClass.OWNER_APPROVAL_REQUIRED
    assert res.approval.status is ApprovalStatus.PENDING
    t = core.queries.get_task(task.id)
    assert t.state is TaskState.OWNER_APPROVAL_REQUIRED
    assert t.resume_state is TaskState.PLANNING
    assert len(events_of(core, "gate.owner_required", task.id)) == 1


def test_approve_flow(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    ap = core.approve(res.approval.id, OWNER, **_bind(task.id))
    assert ap.status is ApprovalStatus.APPROVED
    assert len(events_of(core, "gate.owner_approved", task.id)) == 1


def test_approve_then_execute_resumes(core, task):
    start_lead(core, task.id)
    core.transition(task.id, TaskState.PLANNING, LEAD)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    core.approve(res.approval.id, OWNER, **_bind(task.id))
    ap = core.execute_approved(res.approval.id, OWNER, **_bind(task.id))
    assert ap.status is ApprovalStatus.CONSUMED
    t = core.queries.get_task(task.id)
    assert t.state is TaskState.PLANNING  # resumed to resume_state
    assert t.resume_state is None


def test_execute_consumes_atomically_once(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    core.approve(res.approval.id, OWNER, **_bind(task.id))
    core.execute_approved(res.approval.id, OWNER, **_bind(task.id))
    with pytest.raises(ApprovalError):
        core.execute_approved(res.approval.id, OWNER, **_bind(task.id))


def test_approval_binding_task(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    with pytest.raises(ApprovalError):
        core.approve(res.approval.id, OWNER, task_id="wrong-task-id",
                     action="deploy_production", scope="prod")
    assert core.queries.get_approval(res.approval.id).status is ApprovalStatus.PENDING


def test_approval_binding_action(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    with pytest.raises(ApprovalError):
        core.approve(res.approval.id, OWNER, task_id=task.id,
                     action="other_action", scope="prod")
    assert core.queries.get_approval(res.approval.id).status is ApprovalStatus.PENDING


def test_approval_binding_scope(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    with pytest.raises(ApprovalError):
        core.approve(res.approval.id, OWNER, task_id=task.id,
                     action="deploy_production", scope="other_scope")
    assert core.queries.get_approval(res.approval.id).status is ApprovalStatus.PENDING


def test_approval_not_reusable_after_approve(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    core.approve(res.approval.id, OWNER, **_bind(task.id))
    with pytest.raises(ApprovalError):
        core.approve(res.approval.id, OWNER, **_bind(task.id))


def test_reject_blocks(core, task):
    start_lead(core, task.id)
    core.transition(task.id, TaskState.PLANNING, LEAD)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    ap = core.reject(res.approval.id, OWNER, **_bind(task.id))
    assert ap.status is ApprovalStatus.REJECTED
    t = core.queries.get_task(task.id)
    assert t.state is TaskState.BLOCKED
    assert len(events_of(core, "gate.owner_rejected", task.id)) == 1
    with pytest.raises(ApprovalError):
        core.execute_approved(res.approval.id, OWNER, **_bind(task.id))
    assert core.queries.get_task(task.id).state is TaskState.BLOCKED


def test_expired_approval_not_usable(tmp_path):
    class Clock:
        def __init__(self):
            self.t = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        def __call__(self):
            return self.t

    clock = Clock()
    c = Core(str(tmp_path / "exp.db"), clock=clock)
    p = c.create_project("p", OWNER)
    task = c.create_task(p.id, "t", OWNER)
    c.start_role(task.id, Role.LEAD, LEAD)
    res = c.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    clock.t += timedelta(hours=2)  # advance past the 1h TTL
    with pytest.raises(ApprovalError):
        c.approve(res.approval.id, OWNER, **_bind(task.id))
    assert c.queries.get_approval(res.approval.id).status is ApprovalStatus.EXPIRED
    c.close()


def test_approved_but_expired_not_executable(tmp_path):
    class Clock:
        def __init__(self):
            self.t = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        def __call__(self):
            return self.t

    clock = Clock()
    c = Core(str(tmp_path / "exp2.db"), clock=clock)
    p = c.create_project("p", OWNER)
    task = c.create_task(p.id, "t", OWNER)
    c.start_role(task.id, Role.LEAD, LEAD)
    res = c.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    c.approve(res.approval.id, OWNER, **_bind(task.id))
    clock.t += timedelta(hours=2)  # advance past TTL after approval
    with pytest.raises(ApprovalError):
        c.execute_approved(res.approval.id, OWNER, **_bind(task.id))
    # R10 + V1.2 12.3: the expired approved approval must NOT be consumed and
    # is atomically marked 'expired' (no deadlock on the unique index).
    assert c.queries.get_approval(res.approval.id).status is ApprovalStatus.EXPIRED
    c.close()


def test_forbidden_no_approval_created(core, task):
    start_lead(core, task.id)
    events_before = len(core.list_events(OWNER))
    with pytest.raises(ForbiddenAction):
        core.request_action(task.id, "exfiltrate_data", "prod", Role.LEAD, LEAD)
    assert core.queries.list_approvals(task.id) == []
    assert len(events_of(core, "lead.decision", task.id)) == 1
    assert len(core.list_events(OWNER)) == events_before + 1
    # R13: a blocked execution row is persisted.
    execs = core.queries.list_action_executions(task.id)
    assert len(execs) == 1
    assert execs[0].status is ActionExecutionStatus.BLOCKED


def test_forbidden_cannot_be_executed(core, task):
    start_lead(core, task.id)
    with pytest.raises(ForbiddenAction):
        core.request_action(task.id, "bypass_owner_approval", "prod", Role.LEAD, LEAD)
    assert core.queries.list_approvals(task.id) == []
    with pytest.raises(ApprovalError):
        core.execute_approved("nonexistent", OWNER,
                              task_id=task.id, action="x", scope="y")


def test_execute_approved_persists_execution(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    core.approve(res.approval.id, OWNER, **_bind(task.id))
    core.execute_approved(res.approval.id, OWNER, **_bind(task.id))
    execs = core.queries.list_action_executions(task.id)
    assert len(execs) == 1
    assert execs[0].status is ActionExecutionStatus.EXECUTED
    assert execs[0].approval_id == res.approval.id
