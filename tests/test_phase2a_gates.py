"""Phase 2A external-action + owner-gate tests (SPEC V2 8.4, 15.10; V1 gates)."""

import pytest

from argent_core import (
    ActionClass,
    ApprovalStatus,
    ExternalActionsPolicy,
    ForbiddenAction,
    Role,
    SequenceKind,
    TaskState,
    classify_action,
    OWNER_SOURCE,
)

from conftest import LEAD, events_of
from mock_runtime import MockRuntime
from phase2a_helpers import (
    orchestrated_task,
    receive_valid,
    start_and_dispatch,
)

OWNER = OWNER_SOURCE


def test_classify_external_actions_with_gate():
    for a in ["install_software", "download_dependency", "network_fetch",
              "external_send", "deploy_production"]:
        assert classify_action(a) is ActionClass.OWNER_APPROVAL_REQUIRED


def test_classify_external_actions_forbidden_policy():
    for a in ["install_software", "download_dependency", "system_install",
              "network_fetch", "external_send", "deploy_production",
              "change_secrets", "expose_gateway", "modify_allowlist",
              "promote_stable", "modify_policy", "raise_privileges",
              "enable_self_improvement", "production_write"]:
        assert classify_action(a, ExternalActionsPolicy.FORBIDDEN) is ActionClass.FORBIDDEN


def test_unknown_external_action_forbidden():
    assert classify_action("totally_unknown_action") is ActionClass.FORBIDDEN
    assert classify_action(
        "totally_unknown_action", ExternalActionsPolicy.FORBIDDEN
    ) is ActionClass.FORBIDDEN


def test_no_external_actions_blocks_dependency_download(core):
    # get-pip regression: a FORBIDDEN-policy task cannot download/install.
    project = core.create_project("p", OWNER)
    task = core.create_task(
        project.id, "t", OWNER,
        external_actions_policy=ExternalActionsPolicy.FORBIDDEN,
    )
    core.start_role(task.id, Role.LEAD, LEAD)
    for action in ["download_dependency", "install_software", "system_install",
                   "network_fetch"]:
        with pytest.raises(ForbiddenAction):
            core.request_action(task.id, action, "deps", Role.LEAD, LEAD)
    assert core.queries.list_approvals(task.id) == []


def test_external_actions_allowed_with_gate_creates_approval(core):
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    core.start_role(task.id, Role.LEAD, LEAD)
    res = core.request_action(task.id, "download_dependency", "deps", Role.LEAD, LEAD)
    assert res.action_class is ActionClass.OWNER_APPROVAL_REQUIRED
    assert res.approval.status is ApprovalStatus.PENDING


def test_owner_gate_full_flow(core):
    # The controller (lead) turns an agent recommendation into a real gate.
    task, _ = orchestrated_task(core)
    core.start_role(task.id, Role.LEAD, LEAD)
    req = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    assert req.action_class is ActionClass.OWNER_APPROVAL_REQUIRED
    assert core.queries.get_task(task.id).state is TaskState.OWNER_APPROVAL_REQUIRED
    assert len(events_of(core, "gate.owner_required", task.id)) == 1

    ap = core.approve(req.approval.id, OWNER, task_id=task.id,
                      action="deploy_production", scope="prod")
    assert ap.status is ApprovalStatus.APPROVED
    ex = core.execute_approved(req.approval.id, OWNER, task_id=task.id,
                               action="deploy_production", scope="prod")
    assert ex.status is ApprovalStatus.CONSUMED
    assert core.queries.get_task(task.id).state is not TaskState.OWNER_APPROVAL_REQUIRED


def test_agent_recommendation_then_controller_gate(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = start_and_dispatch(
        core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD
    )
    out = {
        "role": "lead", "task_id": task.id, "dispatch_id": d.id,
        "status": "ok", "findings": [], "own_assessment": "x", "concerns": [],
        "proposal": "x", "alternatives": [], "confidence": 1.0, "blockers": [],
        "requested_next_state": "owner_gate", "decision": "request_owner_gate",
        "accepted_findings": [], "rejected_findings": [], "rationale": "y",
    }
    em = runtime.completion_event(task.id, session, run)
    res = core.receive_agent_result(d.id, em, out, LEAD)
    assert res.status == "consumed"
    # The recommendation alone creates no approval.
    assert core.queries.list_approvals(task.id) == []


def test_agent_output_cannot_change_policy(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d, session, run = start_and_dispatch(
        core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD
    )
    # A lead output attempting to recommend a forbidden action cannot change
    # the external-actions policy; there is no API for it and no approval.
    out = {
        "role": "lead", "task_id": task.id, "dispatch_id": d.id,
        "status": "ok", "findings": [], "own_assessment": "x", "concerns": [],
        "proposal": "x", "alternatives": [], "confidence": 1.0, "blockers": [],
        "requested_next_state": "owner_gate", "decision": "request_owner_gate",
        "accepted_findings": [], "rejected_findings": [], "rationale": "y",
    }
    em = runtime.completion_event(task.id, session, run)
    core.receive_agent_result(d.id, em, out, LEAD)
    # Policy unchanged and no approval was created by the agent output.
    assert core.queries.get_task(task.id).external_actions_policy.value == "ALLOWED_WITH_GATE"
    assert core.queries.list_approvals(task.id) == []
