"""Role permission and single-active-role tests (SPEC V1 chapter 2 + V1.1 11.2)."""

import pytest

from argent_core import (
    ArtifactCategory,
    IdempotencyError,
    Permission,
    PermissionDenied,
    Role,
    RoleConflict,
    can_read,
    can_write,
    check_permission,
    role_source,
    OWNER_SOURCE,
)

from conftest import LEAD, ANALYST, IMPLEMENTER, events_of, start_lead

PC = ArtifactCategory.PRODUCT_CODE
TC = ArtifactCategory.TEST_CODE
OT = ArtifactCategory.OTHER

EXPECTED_WRITE = {
    Role.LEAD: {PC: False, TC: False, OT: True},
    Role.ANALYST: {PC: False, TC: False, OT: True},
    Role.IMPLEMENTER: {PC: True, TC: True, OT: True},
    Role.QA: {PC: False, TC: True, OT: True},
    Role.REVIEWER: {PC: False, TC: False, OT: False},
}


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("category", [PC, TC, OT])
def test_permission_matrix(role, category):
    assert can_write(role, category) is EXPECTED_WRITE[role][category]
    assert can_read(role, category) is True


def test_qa_cannot_write_product_code():
    assert can_write(Role.QA, PC) is False
    with pytest.raises(PermissionDenied):
        check_permission(Role.QA, PC, Permission.WRITE)


def test_lead_analyst_reviewer_read_only_product_code():
    for role in (Role.LEAD, Role.ANALYST, Role.REVIEWER):
        assert can_write(role, PC) is False
        assert can_read(role, PC) is True


def test_implementer_writes_everything():
    for cat in (PC, TC, OT):
        assert can_write(Role.IMPLEMENTER, cat) is True


def test_reviewer_read_only_everything():
    for cat in (PC, TC, OT):
        assert can_write(Role.REVIEWER, cat) is False


def test_lead_cannot_write_product_code_via_check():
    with pytest.raises(PermissionDenied):
        check_permission(Role.LEAD, PC, Permission.WRITE)


# ------------------------------------------------------------ role runs (V1.1)


def test_first_role_start_must_be_lead(core, task):
    with pytest.raises(RoleConflict):
        core.start_role(task.id, Role.ANALYST, LEAD)


def test_start_role_is_lead_only(core, task):
    with pytest.raises(PermissionDenied):
        core.start_role(task.id, Role.LEAD, role_source(Role.QA))
    with pytest.raises(PermissionDenied):
        core.start_role(task.id, Role.LEAD, OWNER_SOURCE)


def test_exactly_one_active_role(core, task):
    start_lead(core, task.id)
    with pytest.raises(RoleConflict):
        core.start_role(task.id, Role.LEAD, LEAD)


def test_role_lifecycle_completed(core, task):
    rr = start_lead(core, task.id)
    assert rr.status.value == "started"
    rr2 = core.complete_role(rr.id, LEAD)
    assert rr2.status.value == "completed"


def test_role_lifecycle_failed(core, task):
    rr = start_lead(core, task.id)
    rr2 = core.fail_role(rr.id, LEAD)
    assert rr2.status.value == "failed"


def test_complete_creates_handoff(core, task):
    rr = start_lead(core, task.id)
    core.complete_role(rr.id, LEAD)
    assert len(events_of(core, "handoff.created", task.id)) == 1
    handoffs = core.queries.list_handoffs(task.id)
    assert len(handoffs) == 1
    assert handoffs[0].from_role is Role.LEAD
    assert handoffs[0].to_role is Role.ANALYST


def test_role_switch_requires_handoff(core, task):
    rr = start_lead(core, task.id)
    core.complete_role(rr.id, LEAD)  # handoff lead -> analyst
    # Skipping the handoff target is forbidden.
    with pytest.raises(RoleConflict):
        core.start_role(task.id, Role.IMPLEMENTER, LEAD)
    core.start_role(task.id, Role.ANALYST, LEAD)


def test_complete_requires_role_source(core, task):
    rr = start_lead(core, task.id)
    with pytest.raises(PermissionDenied):
        core.complete_role(rr.id, ANALYST)  # wrong role source
    with pytest.raises(PermissionDenied):
        core.complete_role(rr.id, OWNER_SOURCE)


def test_complete_twice_raises(core, task):
    rr = start_lead(core, task.id)
    core.complete_role(rr.id, LEAD)
    with pytest.raises(IdempotencyError):
        core.complete_role(rr.id, LEAD)


def test_lead_coordinates_pipeline(core, task):
    rr = start_lead(core, task.id)
    for expected in (Role.ANALYST, Role.IMPLEMENTER, Role.QA, Role.REVIEWER, Role.LEAD):
        core.complete_role(rr.id, role_source(rr.role))
        rr = core.start_role(task.id, expected, LEAD)
    assert rr.role is Role.LEAD
