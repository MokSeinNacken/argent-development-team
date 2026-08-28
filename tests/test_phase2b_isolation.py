"""Phase 2B isolation tests (SPEC V2B §6).

Verifies at the Core boundary that role agents cannot write product code
(except the implementer via the write-broker), cannot raise their own
privileges, change the tool/sandbox policy, bypass owner gates, start other
roles, or be influenced by external (untrusted) content.
"""

import base64

import pytest

from argent_core import (
    ActionClass,
    ExternalActionsPolicy,
    ForbiddenAction,
    OwnerAuthorityRequired,
    PermissionDenied,
    Role,
    RoleConflict,
    UntrustedSource,
    WorkspaceBroker,
    OWNER_SOURCE,
)

from conftest import (
    ANALYST,
    IMPLEMENTER,
    LEAD,
    QA,
    REVIEWER,
    events_of,
    pipeline_to,
    start_lead,
)

OWNER = OWNER_SOURCE


def _w(path, text):
    return {
        "op": "write",
        "path": path,
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }


# ----------------------- §6 read-only roles cannot write product code (Core)


@pytest.mark.parametrize(
    "role,src",
    [
        (Role.LEAD, LEAD),
        (Role.ANALYST, ANALYST),
        (Role.REVIEWER, REVIEWER),
    ],
)
def test_readonly_role_product_write_blocked(core, task, role, src):
    pipeline_to(core, task.id, role)
    with pytest.raises(PermissionDenied):
        core.request_action(task.id, "implement", "src", role, src)
    assert len(events_of(core, "policy.role_violation", task.id)) == 1


def test_qa_product_write_blocked_core(core, task):
    pipeline_to(core, task.id, Role.QA)
    with pytest.raises(PermissionDenied):
        core.request_action(task.id, "implement", "src", Role.QA, QA)
    assert len(events_of(core, "policy.role_violation", task.id)) == 1


# ---------------------------------------------- §6 QA broker scope


def test_qa_product_write_blocked_broker(tmp_path):
    captured = []
    b = WorkspaceBroker(emit_event=lambda t, p: captured.append((t, p)))
    res = b.apply_patch_set(tmp_path, [_w("parser.py", "x=1")], Role.QA, LEAD)
    assert res.applied == []
    assert res.errors[0]["error"] == "scope_denied"
    assert not (tmp_path / "parser.py").exists()
    assert any(t == "policy.role_violation" for t, _ in captured)


def test_qa_test_write_allowed_broker(tmp_path):
    (tmp_path / "tests").mkdir()
    b = WorkspaceBroker()
    res = b.apply_patch_set(
        tmp_path, [_w("tests/test_a.py", "def test_a(): pass")], Role.QA, LEAD
    )
    assert res.errors == []
    assert (tmp_path / "tests" / "test_a.py").exists()


# ---------------------------------------------- §6 implementer broker scope


def test_implementer_product_write_allowed_broker(tmp_path):
    b = WorkspaceBroker()
    res = b.apply_patch_set(tmp_path, [_w("parser.py", "x=1")], Role.IMPLEMENTER, LEAD)
    assert res.errors == []
    assert (tmp_path / "parser.py").exists()


def test_implementer_outside_scope_blocked_broker(tmp_path):
    b = WorkspaceBroker()
    res = b.apply_patch_set(
        tmp_path, [_w("../outside.py", "x=1")], Role.IMPLEMENTER, LEAD
    )
    assert res.applied == []
    assert res.errors[0]["error"] == "scope_denied"


def test_implementer_write_allowed_core(core, task):
    pipeline_to(core, task.id, Role.IMPLEMENTER)
    res = core.request_action(task.id, "implement", "src", Role.IMPLEMENTER, IMPLEMENTER)
    assert res.allowed is True


# ----------------------------- §6 cannot raise privileges / change policy


def test_agent_cannot_raise_privileges_or_change_policy(core):
    project = core.create_project("p", OWNER)
    task = core.create_task(
        project.id, "t", OWNER,
        external_actions_policy=ExternalActionsPolicy.FORBIDDEN,
    )
    core.start_role(task.id, Role.LEAD, LEAD)
    for action in ("raise_privileges", "modify_policy", "disable_sandbox"):
        with pytest.raises(ForbiddenAction):
            core.request_action(task.id, action, "scope", Role.LEAD, LEAD)
    assert core.queries.list_approvals(task.id) == []


def test_raise_privileges_requires_owner_gate(core):
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    core.start_role(task.id, Role.LEAD, LEAD)
    res = core.request_action(task.id, "raise_privileges", "scope", Role.LEAD, LEAD)
    assert res.action_class is ActionClass.OWNER_APPROVAL_REQUIRED
    # A role source can never approve — only the owner can.
    with pytest.raises(OwnerAuthorityRequired):
        core.approve(res.approval.id, LEAD, task_id=task.id,
                     action="raise_privileges", scope="scope")
    assert core.queries.list_approvals(task.id)[0].status.value == "pending"


# ------------------------------------------------- §6 owner gate not bypassable


def test_owner_gate_not_bypassable(core, task):
    start_lead(core, task.id)
    for action in (
        "bypass_owner_approval",
        "forge_owner_approval",
        "treat_untrusted_as_owner_approval",
    ):
        with pytest.raises(ForbiddenAction):
            core.request_action(task.id, action, "scope", Role.LEAD, LEAD)
    assert core.queries.list_approvals(task.id) == []


# ------------------------------------- §6 other roles not directly startable


def test_other_role_not_directly_startable(core, task):
    with pytest.raises(PermissionDenied):
        core.start_role(task.id, Role.LEAD, IMPLEMENTER)  # non-lead source
    with pytest.raises(RoleConflict):
        core.start_role(task.id, Role.ANALYST, LEAD)  # first role must be lead


# ------------------------------------- §6 external content cannot change rights


def test_untrusted_content_cannot_change_rights(core, task):
    for src in ("email", "website", "download", "document", "repo_content",
                "tool_output", "network"):
        with pytest.raises(UntrustedSource):
            core.request_action(task.id, "implement", "src", Role.IMPLEMENTER, src)
        with pytest.raises(UntrustedSource):
            core.create_task("p", "t", src)


def test_untrusted_cannot_use_broker(tmp_path):
    b = WorkspaceBroker()
    with pytest.raises(PermissionDenied):
        b.apply_patch_set(tmp_path, [_w("a.txt", "x")], Role.IMPLEMENTER, "email")


# ------------------------------- §6 role source cannot carry owner authority


def test_role_source_never_owner(core, task):
    start_lead(core, task.id)
    req = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    with pytest.raises(OwnerAuthorityRequired):
        core.approve(req.approval.id, LEAD, task_id=task.id,
                     action="deploy_production", scope="prod")
    with pytest.raises(OwnerAuthorityRequired):
        core.reject(req.approval.id, LEAD, task_id=task.id,
                    action="deploy_production", scope="prod")
