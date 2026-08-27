"""Trust boundary tests (SPEC V1 chapter 4 + V1.1 11.4)."""

import pytest

from argent_core import (
    OwnerAuthorityRequired,
    PermissionDenied,
    Role,
    SourceClass,
    TaskState,
    UntrustedSource,
    classify_source,
    role_source,
    OWNER_SOURCE,
)

from conftest import LEAD, start_lead

UNTRUSTED = ["email", "website", "download", "document", "repo_content",
             "tool_output", "network"]


def test_classify_owner_trusted():
    assert classify_source(OWNER_SOURCE) is SourceClass.TRUSTED


@pytest.mark.parametrize("role", list(Role))
def test_classify_role_trusted(role):
    assert classify_source(role_source(role)) is SourceClass.TRUSTED


@pytest.mark.parametrize("src", UNTRUSTED)
def test_classify_untrusted(src):
    assert classify_source(src) is SourceClass.UNTRUSTED


def test_classify_unknown_untrusted():
    assert classify_source("role:not_a_role") is SourceClass.UNTRUSTED
    assert classify_source("random_string") is SourceClass.UNTRUSTED


@pytest.mark.parametrize("src", UNTRUSTED)
def test_untrusted_cannot_create_task(core, project, src):
    with pytest.raises(UntrustedSource):
        core.create_task(project.id, "t", src)
    assert core.queries.list_tasks() == []


@pytest.mark.parametrize("src", UNTRUSTED)
def test_untrusted_cannot_create_project(core, src):
    with pytest.raises(UntrustedSource):
        core.create_project("p", src)
    assert core.queries.list_tasks() == []


@pytest.mark.parametrize("src", UNTRUSTED)
def test_untrusted_cannot_transition(core, task, src):
    with pytest.raises(UntrustedSource):
        core.transition(task.id, TaskState.PLANNING, src)
    assert core.queries.get_task(task.id).state is TaskState.NEW


@pytest.mark.parametrize("src", UNTRUSTED)
def test_untrusted_cannot_request_action(core, task, src):
    with pytest.raises(UntrustedSource):
        core.request_action(task.id, "deploy_production", "prod", Role.LEAD, src)
    assert core.queries.list_approvals(task.id) == []


def test_untrusted_cannot_approve(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    with pytest.raises(UntrustedSource):
        core.approve(res.approval.id, "email", task_id=task.id,
                     action="deploy_production", scope="prod")
    assert core.queries.get_approval(res.approval.id).status.value == "pending"


def test_untrusted_cannot_reject(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    with pytest.raises(UntrustedSource):
        core.reject(res.approval.id, "tool_output", task_id=task.id,
                    action="deploy_production", scope="prod")
    assert core.queries.get_approval(res.approval.id).status.value == "pending"


def test_untrusted_cannot_execute(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    core.approve(res.approval.id, OWNER_SOURCE, task_id=task.id,
                 action="deploy_production", scope="prod")
    with pytest.raises(UntrustedSource):
        core.execute_approved(res.approval.id, "website", task_id=task.id,
                              action="deploy_production", scope="prod")
    assert core.queries.get_approval(res.approval.id).status.value == "approved"


def test_untrusted_cannot_list_events(core, task):
    with pytest.raises(UntrustedSource):
        core.list_events("document")


def test_untrusted_cannot_add_finding(core, task):
    with pytest.raises(UntrustedSource):
        core.add_finding(task.id, "high", "a finding", "repo_content")
    assert core.queries.list_findings(task.id) == []


def test_untrusted_cannot_recover(core, task):
    with pytest.raises(UntrustedSource):
        core.recover("download")


# -------------------------------------------------- role sources are not owner


def test_role_cannot_create_project(core):
    with pytest.raises(OwnerAuthorityRequired):
        core.create_project("p", LEAD)


def test_role_cannot_create_task(core, project):
    with pytest.raises(OwnerAuthorityRequired):
        core.create_task(project.id, "t", LEAD)


def test_role_cannot_approve(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    with pytest.raises(OwnerAuthorityRequired):
        core.approve(res.approval.id, LEAD, task_id=task.id,
                     action="deploy_production", scope="prod")
    assert core.queries.get_approval(res.approval.id).status.value == "pending"


def test_role_cannot_reject(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    with pytest.raises(OwnerAuthorityRequired):
        core.reject(res.approval.id, LEAD, task_id=task.id,
                    action="deploy_production", scope="prod")
    assert core.queries.get_approval(res.approval.id).status.value == "pending"


def test_role_cannot_execute(core, task):
    start_lead(core, task.id)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    core.approve(res.approval.id, OWNER_SOURCE, task_id=task.id,
                 action="deploy_production", scope="prod")
    with pytest.raises(OwnerAuthorityRequired):
        core.execute_approved(res.approval.id, LEAD, task_id=task.id,
                              action="deploy_production", scope="prod")
    assert core.queries.get_approval(res.approval.id).status.value == "approved"


def test_role_cannot_recover(core, task):
    with pytest.raises(OwnerAuthorityRequired):
        core.recover(LEAD)


# -------------------------------------------------------- role-bound authority


def test_lead_can_transition_when_active(core, task):
    start_lead(core, task.id)
    core.transition(task.id, TaskState.PLANNING, LEAD)
    assert core.queries.get_task(task.id).state is TaskState.PLANNING


def test_role_cannot_transition_without_active_run(core, task):
    with pytest.raises(PermissionDenied):
        core.transition(task.id, TaskState.PLANNING, LEAD)


def test_non_lead_role_cannot_transition(core, task):
    # Lead must be active to transition; a qa source is never allowed.
    start_lead(core, task.id)
    with pytest.raises(PermissionDenied):
        core.transition(task.id, TaskState.PLANNING, role_source(Role.QA))


def test_owner_cannot_transition(core, task):
    # transition is lead-only; the owner is not the lead.
    start_lead(core, task.id)
    with pytest.raises(PermissionDenied):
        core.transition(task.id, TaskState.PLANNING, OWNER_SOURCE)
