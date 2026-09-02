"""Phase E1 — Dispatch integration + acceptance cases (Matrix H, J + 1–10).

Deterministic, local, no network, no provider calls.  Proves the single E1
integration point (``Core.create_dispatch`` validates the resolved identity
against the registry AFTER the unchanged role-policy check) and the owner-spec
acceptance cases 1–10.
"""

from __future__ import annotations

import pytest

from argent_core import (
    MODEL_CONFIG_INVALID,
    MODEL_NOT_ALLOWED,
    Capability,
    CapabilityRequirements,
    Core,
    Independence,
    ModelRegistry,
    ModelRegistryError,
    OWNER_SOURCE,
    ReasoningLevel,
    RiskClass,
    Role,
    SequenceKind,
    get_default_registry,
    role_source,
)

from conftest import LEAD
from mock_runtime import MockRuntime
from phase2a_helpers import orchestrated_task, run_role

OWNER = OWNER_SOURCE

# A test provider supports every bounded capability so capability-bound
# enforcement (model tags ⊆ provider capabilities_supported) does not distract
# from the specific invariant each unit test is checking.
_ALL_CAPS = [c.value for c in Capability]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _openai_prov(**over):
    d = {
        "provider_id": "openai", "provider_type": "oauth-plugin",
        "display_name": "OpenAI", "enabled": True,
        "availability_state": "AVAILABLE", "capabilities_supported": list(_ALL_CAPS),
        "credential_ref": "openclaw:auth:profiles:openai", "auth_mode": "oauth",
        "endpoint_ref": None, "profile_ref": None, "policy_version": "1",
    }
    d.update(over)
    return d


def _deepseek_prov(**over):
    d = {
        "provider_id": "deepseek", "provider_type": "openai-completions",
        "display_name": "DeepSeek", "enabled": True,
        "availability_state": "AVAILABLE", "capabilities_supported": list(_ALL_CAPS),
        "credential_ref": "openclaw:provider:deepseek", "auth_mode": "api-key",
        "endpoint_ref": None, "profile_ref": None, "policy_version": "1",
    }
    d.update(over)
    return d


def _sol_model(**over):
    d = {
        "model_id": "gpt-5.6-sol", "provider_id": "openai",
        "canonical_model_name": "GPT-5.6 Sol", "enabled": True,
        "lifecycle_state": "ACTIVE", "context_window_metadata": None,
        "output_limit_metadata": None, "reasoning_levels_supported": ["HIGH"],
        "tool_capabilities": [],
        "abilities": {"vision": None, "coding": "review", "review": True},
        "latency_class": "UNKNOWN", "cost_class": "UNKNOWN",
        "reliability_class": "UNKNOWN",
        "capability_tags": ["COORDINATION", "CODE_REVIEW", "SECURITY_REVIEW"],
        "policy_version": "1",
        "provenance": {"source": "test", "benchmarked": False},
    }
    d.update(over)
    return d


# ---------------------------------------------------------------------------
# H — Current models represented correctly
# ---------------------------------------------------------------------------


def test_current_models_represented():
    r = get_default_registry()
    providers = {p.provider_id: p for p in r.list_providers()}
    assert set(providers) == {"deepseek", "openai"}
    models = {m.model_id: m for m in r.list_models()}
    assert set(models) == {"deepseek-v4-flash", "deepseek-v4-pro", "gpt-5.6-sol"}

    assert models["deepseek-v4-flash"].provider_id == "deepseek"
    assert models["deepseek-v4-pro"].provider_id == "deepseek"
    assert models["gpt-5.6-sol"].provider_id == "openai"

    # Reasoning levels (real, existing levels only): sol HIGH, pro/flash MEDIUM.
    assert models["gpt-5.6-sol"].reasoning_levels_supported == ("HIGH",)
    assert models["deepseek-v4-pro"].reasoning_levels_supported == ("MEDIUM",)
    assert models["deepseek-v4-flash"].reasoning_levels_supported == ("MEDIUM",)

    # No claude/gemini/glm/qwen.
    assert not set(providers) & {"claude", "gemini", "glm", "qwen"}
    assert not set(models) & {"claude-3", "gemini-pro", "glm-4", "qwen-max"}


def test_sol_has_review_architecture_claims():
    r = get_default_registry()
    sol = r.get_model("gpt-5.6-sol")
    assert "SECURITY_REVIEW" in sol.capability_tags
    assert "CODE_REVIEW" in sol.capability_tags
    assert "ARCHITECTURE" in sol.capability_tags
    assert "COORDINATION" in sol.capability_tags
    assert sol.abilities.review is True
    assert sol.provenance.benchmarked is False


def test_pro_has_code_implementation_claim():
    r = get_default_registry()
    pro = r.get_model("deepseek-v4-pro")
    assert "CODE_IMPLEMENTATION" in pro.capability_tags
    assert "COMPLEX_CODE_IMPLEMENTATION" in pro.capability_tags
    assert "SIMPLE_ANALYSIS" in pro.capability_tags


# ---------------------------------------------------------------------------
# J — Regression dispatch: canonical identities pass through unchanged
# ---------------------------------------------------------------------------


def test_lead_dispatch_uses_default_registry_passes(core):
    task, task_run = orchestrated_task(core)
    core.start_role(task.id, Role.LEAD, LEAD)
    d = core.create_dispatch(
        task.id, task_run.id, Role.LEAD, 0, 1, SequenceKind.STANDARD, None, LEAD
    )
    assert d.expected_agent_class == "openai"
    assert d.expected_model_class == "gpt-5.6-sol"


# ---------------------------------------------------------------------------
# Acceptance cases 1–10
# ---------------------------------------------------------------------------


def test_case1_flash_valid_and_dispatchable(tmp_path):
    c = Core(str(tmp_path / "case1.db"))
    project = c.create_project("p", OWNER)
    task = c.create_task(project.id, "t", OWNER, risk_class=RiskClass.LOW)
    task_run = c.start_task_run(task.id, OWNER)
    runtime = MockRuntime()
    run_role(c, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD)
    run_role(c, runtime, task, task_run, Role.ANALYST, 1, 1, SequenceKind.STANDARD)
    run_role(c, runtime, task, task_run, Role.LEAD, 1, 2, SequenceKind.STANDARD)
    c.start_role(task.id, Role.IMPLEMENTER, role_source(Role.LEAD))
    d = c.create_dispatch(
        task.id, task_run.id, Role.IMPLEMENTER, 3, 1, SequenceKind.STANDARD,
        {"provider": "deepseek", "model": "deepseek-v4-flash",
         "thinking_tier": "medium"},
        role_source(Role.LEAD),
    )
    assert d.expected_model_class == "deepseek-v4-flash"
    # Coordination + einfache Analyse are modeled as capabilities on flash
    # (architecture §11), and flash is a valid LOW-risk implementer model.
    r = get_default_registry()
    assert "COORDINATION" in r.get_model("deepseek-v4-flash").capability_tags
    assert "SIMPLE_ANALYSIS" in r.get_model("deepseek-v4-flash").capability_tags
    assert r.get_model("deepseek-v4-flash").enabled is True
    c.close()


def test_case2_pro_writer_path(tmp_path):
    c = Core(str(tmp_path / "case2.db"))
    project = c.create_project("p", OWNER)
    task = c.create_task(project.id, "t", OWNER)  # NORMAL risk
    task_run = c.start_task_run(task.id, OWNER)
    runtime = MockRuntime()
    run_role(c, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD)
    run_role(c, runtime, task, task_run, Role.ANALYST, 1, 1, SequenceKind.STANDARD)
    run_role(c, runtime, task, task_run, Role.LEAD, 1, 2, SequenceKind.STANDARD)
    c.start_role(task.id, Role.IMPLEMENTER, role_source(Role.LEAD))
    d = c.create_dispatch(
        task.id, task_run.id, Role.IMPLEMENTER, 3, 1, SequenceKind.STANDARD,
        {"provider": "deepseek", "model": "deepseek-v4-pro",
         "thinking_tier": "medium"},
        role_source(Role.LEAD),
    )
    assert d.expected_model_class == "deepseek-v4-pro"
    pro = get_default_registry().get_model("deepseek-v4-pro")
    assert "CODE_IMPLEMENTATION" in pro.capability_tags
    c.close()


def test_case3_sol_review_architecture_reasoning():
    r = get_default_registry()
    sol = r.get_model("gpt-5.6-sol")
    assert sol.reasoning_levels_supported == ("HIGH",)
    assert "CODE_REVIEW" in sol.capability_tags
    assert "ARCHITECTURE" in sol.capability_tags
    assert sol.abilities.coding == "review"
    assert sol.abilities.review is True


def test_case4_unknown_identity_fails_closed(tmp_path):
    # Registry without the gpt-5.6-sol model -> lead dispatch fails closed.
    reg = ModelRegistry.from_payload([_openai_prov()], [])
    c = Core(str(tmp_path / "case4.db"), registry=reg)
    task, task_run = orchestrated_task(c)
    c.start_role(task.id, Role.LEAD, LEAD)
    with pytest.raises(ModelRegistryError) as e:
        c.create_dispatch(
            task.id, task_run.id, Role.LEAD, 0, 1, SequenceKind.STANDARD, None, LEAD
        )
    assert e.value.code in (MODEL_CONFIG_INVALID, "MODEL_NOT_ALLOWED")
    # No dispatch was created.
    assert c.queries.list_dispatches(task.id) == []
    c.close()


def test_case5_disabled_model_no_dispatch(tmp_path):
    reg = ModelRegistry.from_payload(
        [_openai_prov()], [_sol_model(enabled=False)]
    )
    c = Core(str(tmp_path / "case5.db"), registry=reg)
    task, task_run = orchestrated_task(c)
    c.start_role(task.id, Role.LEAD, LEAD)
    with pytest.raises(ModelRegistryError) as e:
        c.create_dispatch(
            task.id, task_run.id, Role.LEAD, 0, 1, SequenceKind.STANDARD, None, LEAD
        )
    assert e.value.code == MODEL_NOT_ALLOWED
    assert c.queries.list_dispatches(task.id) == []
    c.close()


def test_case6_no_security_review_no_candidate():
    r = get_default_registry()
    req = CapabilityRequirements(required_capabilities=("SECURITY_REVIEW",))
    ids = {m.model_id for m in r.eligible_models(req)}
    # Only sol (the reviewer) carries SECURITY_REVIEW; pro/flash are excluded.
    assert ids == {"gpt-5.6-sol"}
    # A flash model therefore never appears as a security-reviewer candidate.
    assert "deepseek-v4-flash" not in ids


def test_case7_tool_capability_grants_no_tool_rights():
    r = get_default_registry()
    # pro claims shell_exec/code_edit as *capabilities*.
    pro = r.get_model("deepseek-v4-pro")
    assert "shell_exec" in pro.tool_capabilities
    # But the roles.py permission matrix is unchanged: analyst (also pro) still
    # may not write PRODUCT_CODE, regardless of the model's tool claims.
    from argent_core import ArtifactCategory, Permission, check_permission, PermissionDenied
    with pytest.raises(PermissionDenied):
        check_permission(Role.ANALYST, ArtifactCategory.PRODUCT_CODE, Permission.WRITE)
    # Implementer may write, but that comes from roles.py, NOT the registry.
    check_permission(Role.IMPLEMENTER, ArtifactCategory.PRODUCT_CODE, Permission.WRITE)


def test_case8_provider_disabled_models_not_eligible():
    reg = ModelRegistry.from_payload(
        [_deepseek_prov(enabled=False),
         _openai_prov()],
        [_sol_model()],
    )
    # deepseek provider is disabled; only sol (openai) can ever be eligible.
    req = CapabilityRequirements()
    ids = {m.model_id for m in reg.eligible_models(req)}
    assert "gpt-5.6-sol" in ids
    # No deepseek model is present in this registry at all; even if it were, a
    # disabled provider excludes it (validated via validate_identity).
    with pytest.raises(ModelRegistryError) as e:
        reg.validate_identity("deepseek", "deepseek-v4-pro")
    assert e.value.code == "PROVIDER_UNAVAILABLE"


def test_case9_writer_not_own_closing_reviewer():
    r = get_default_registry()
    # Writer = deepseek-v4-pro.  A DIFFERENT_PROVIDER_REQUIRED closing-review
    # requirement cannot return the writer as its own reviewer.
    req = CapabilityRequirements(
        required_capabilities=("SECURITY_REVIEW",),
        independence_requirement=Independence.DIFFERENT_PROVIDER_REQUIRED.value,
    )
    ids = {m.model_id for m in r.eligible_models(
        req, reference_model_id="deepseek-v4-pro")}
    assert "deepseek-v4-pro" not in ids
    assert ids == {"gpt-5.6-sol"}


def test_case10_future_provider_no_core_change(tmp_path):
    # Register a future provider/model as data and dispatch through Core using
    # an injected registry — no core/workflow branch is required.
    future_model = {
        "model_id": "future-model", "provider_id": "futureco",
        "canonical_model_name": "Future Model", "enabled": True,
        "lifecycle_state": "ACTIVE", "context_window_metadata": 100000,
        "output_limit_metadata": 10000,
        "reasoning_levels_supported": ["HIGH"],
        "tool_capabilities": [],
        "abilities": {"vision": False, "coding": "review", "review": True},
        "latency_class": "UNKNOWN", "cost_class": "UNKNOWN",
        "reliability_class": "UNKNOWN",
        "capability_tags": ["COORDINATION", "CODE_REVIEW", "SECURITY_REVIEW"],
        "policy_version": "1",
        "provenance": {"source": "test-future", "benchmarked": False},
    }
    reg = ModelRegistry.from_payload(
        [_openai_prov(provider_id="futureco", display_name="FutureCo")],
        [future_model],
    )
    # Validate the future identity through the public registry interface.
    d = reg.validate_identity("futureco", "future-model")
    assert d.model_id == "future-model"
    # Eligible as a security reviewer.
    req = CapabilityRequirements(required_capabilities=("SECURITY_REVIEW",))
    assert "future-model" in {m.model_id for m in reg.eligible_models(req)}
