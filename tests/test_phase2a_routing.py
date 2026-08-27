"""Phase 2A model-routing tests (SPEC V2 6, V2.1 15.9, 15.12)."""

import pytest

from argent_core import (
    DispatchStatus,
    RiskClass,
    Role,
    RolePolicyViolation,
    SequenceKind,
    resolve_model,
    validate_model_choice,
)

from conftest import LEAD, events_of
from mock_runtime import MockRuntime
from phase2a_helpers import orchestrated_task, start_and_dispatch


def test_resolve_model_canonical():
    assert resolve_model(Role.LEAD, RiskClass.HIGH) == ("openai", "gpt-5.6-sol", "high")
    assert resolve_model(Role.REVIEWER, RiskClass.NORMAL) == ("openai", "gpt-5.6-sol", "high")
    assert resolve_model(Role.ANALYST, RiskClass.NORMAL) == ("deepseek", "deepseek-v4-pro", "medium")
    assert resolve_model(Role.IMPLEMENTER, RiskClass.NORMAL) == ("deepseek", "deepseek-v4-pro", "medium")
    assert resolve_model(Role.QA, RiskClass.NORMAL) == ("deepseek", "deepseek-v4-pro", "medium")
    # Flash for LOW risk implementer/qa only.
    assert resolve_model(Role.IMPLEMENTER, RiskClass.LOW) == ("deepseek", "deepseek-v4-flash", "medium")
    assert resolve_model(Role.QA, RiskClass.LOW) == ("deepseek", "deepseek-v4-flash", "medium")


def test_flash_limitation():
    assert validate_model_choice(Role.IMPLEMENTER, "deepseek", "deepseek-v4-flash", "medium", RiskClass.LOW)
    assert not validate_model_choice(Role.IMPLEMENTER, "deepseek", "deepseek-v4-flash", "medium", RiskClass.NORMAL)
    assert not validate_model_choice(Role.IMPLEMENTER, "deepseek", "deepseek-v4-flash", "medium", RiskClass.HIGH)
    # Pro is always allowed for implementer/qa.
    assert validate_model_choice(Role.IMPLEMENTER, "deepseek", "deepseek-v4-pro", "medium", RiskClass.NORMAL)
    # Analyst never uses flash.
    assert not validate_model_choice(Role.ANALYST, "deepseek", "deepseek-v4-flash", "medium", RiskClass.LOW)
    # Lead/reviewer require openai/gpt-5.6-sol + thinking high.
    assert not validate_model_choice(Role.LEAD, "deepseek", "deepseek-v4-pro", "medium", RiskClass.NORMAL)
    assert not validate_model_choice(Role.LEAD, "openai", "gpt-5.6-sol", "medium", RiskClass.NORMAL)


def test_create_dispatch_flash_forbidden_normal_risk(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)  # risk NORMAL
    # Drive the pipeline to implementer (lead -> analyst -> lead -> implementer).
    from phase2a_helpers import run_role
    run_role(core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD)
    run_role(core, runtime, task, task_run, Role.ANALYST, 1, 1, SequenceKind.STANDARD)
    run_role(core, runtime, task, task_run, Role.LEAD, 1, 2, SequenceKind.STANDARD)
    core.start_role(task.id, Role.IMPLEMENTER, LEAD)
    # Flash is forbidden for NORMAL risk -> policy.role_violation.
    with pytest.raises(RolePolicyViolation):
        core.create_dispatch(
            task.id, task_run.id, Role.IMPLEMENTER, 3, 1, SequenceKind.STANDARD,
            {"provider": "deepseek", "model": "deepseek-v4-flash", "thinking_tier": "medium"},
            LEAD,
        )


def test_implementer_low_risk_allows_flash(tmp_path):
    from argent_core import Core, OWNER_SOURCE, role_source
    c = Core(str(tmp_path / "low.db"))
    project = c.create_project("p", OWNER_SOURCE)
    task = c.create_task(project.id, "t", OWNER_SOURCE, risk_class=RiskClass.LOW)
    task_run = c.start_task_run(task.id, OWNER_SOURCE)
    runtime = MockRuntime()
    # Drive lead -> analyst -> lead -> implementer.
    from phase2a_helpers import run_role
    run_role(c, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD)
    run_role(c, runtime, task, task_run, Role.ANALYST, 1, 1, SequenceKind.STANDARD)
    run_role(c, runtime, task, task_run, Role.LEAD, 1, 2, SequenceKind.STANDARD)
    c.start_role(task.id, Role.IMPLEMENTER, role_source(Role.LEAD))
    d = c.create_dispatch(
        task.id, task_run.id, Role.IMPLEMENTER, 3, 1, SequenceKind.STANDARD,
        {"provider": "deepseek", "model": "deepseek-v4-flash", "thinking_tier": "medium"},
        role_source(Role.LEAD),
    )
    assert d.expected_model_class == "deepseek-v4-flash"
    c.close()


def test_provider_thinking_mismatch_bind_rejected(core):
    task, task_run = orchestrated_task(core)
    core.start_role(task.id, Role.LEAD, LEAD)
    d = core.create_dispatch(
        task.id, task_run.id, Role.LEAD, 0, 1, SequenceKind.STANDARD, None, LEAD
    )
    # Wrong model for lead -> policy.role_violation + rejected.
    with pytest.raises(RolePolicyViolation):
        core.bind_spawn_result(
            d.id, "other-session", "other-run", "deepseek", "deepseek-v4-pro", "medium", LEAD
        )
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.REJECTED
    assert len(events_of(core, "policy.role_violation", task.id)) >= 1


def test_lead_thinking_must_be_high_bind_rejected(core):
    task, task_run = orchestrated_task(core)
    core.start_role(task.id, Role.LEAD, LEAD)
    d = core.create_dispatch(
        task.id, task_run.id, Role.LEAD, 0, 1, SequenceKind.STANDARD, None, LEAD
    )
    # Correct provider/model but thinking medium -> rejected.
    with pytest.raises(RolePolicyViolation):
        core.bind_spawn_result(
            d.id, "s2", "r2", "openai", "gpt-5.6-sol", "medium", LEAD
        )
    assert core.queries.get_dispatch(d.id).status is DispatchStatus.REJECTED


def test_role_violation_event_emitted(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    core.start_role(task.id, Role.LEAD, LEAD)
    with pytest.raises(RolePolicyViolation):
        core.create_dispatch(
            task.id, task_run.id, Role.LEAD, 0, 1, SequenceKind.STANDARD,
            {"provider": "deepseek", "model": "deepseek-v4-pro", "thinking_tier": "medium"},
            LEAD,
        )
    assert len(events_of(core, "policy.role_violation", task.id)) == 1


def test_lead_reviewer_require_distinct_sessions(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    d1, s1, r1 = start_and_dispatch(
        core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD
    )
    # Consume the lead so a second dispatch can be created.
    from phase2a_helpers import receive_valid
    receive_valid(core, runtime, d1, s1, r1, task.id, Role.LEAD)
    core.start_role(task.id, Role.ANALYST, LEAD)
    d2 = core.create_dispatch(
        task.id, task_run.id, Role.ANALYST, 1, 1, SequenceKind.STANDARD, None, LEAD
    )
    # Reusing the lead's session for the analyst dispatch is fail-closed.
    with pytest.raises(Exception):
        core.bind_spawn_result(
            d2.id, s1, r1, "deepseek", "deepseek-v4-pro", "medium", LEAD
        )
    assert core.queries.get_dispatch(d2.id).status is DispatchStatus.PENDING
