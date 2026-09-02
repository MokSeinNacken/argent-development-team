"""Phase E1 — Registry data model (Matrix A–E).

Deterministic, local, no network, no provider calls.  Covers:

* A — Provider registry (valid/duplicate/malformed/unknown/disabled/unavailable)
* B — Model registry (valid/unknown provider/duplicate/invalid metadata/lifecycle/enabled)
* C — Capabilities (taxonomy valid/unknown rejected/requirements/required vs optional/floor)
* D — Reasoning (supported/unsupported rejected / not agent-controlled)
* E — Independence (same-model allowed / different-model required / provider independence)
"""

from __future__ import annotations

import pytest

from argent_core import (
    CAPABILITY_FLOOR_UNMET,
    MODEL_CONFIG_INVALID,
    MODEL_NOT_ALLOWED,
    MODEL_UNAVAILABLE,
    PROVIDER_UNAVAILABLE,
    AvailabilityState,
    Capability,
    CapabilityRequirements,
    Independence,
    LifecycleState,
    ModelRegistry,
    ModelRegistryError,
    ReasoningLevel,
    ReliabilityClass,
    ToolCapability,
)


# ---------------------------------------------------------------------------
# Helpers (raw payload builders — test-only, never shipped)
# ---------------------------------------------------------------------------

# A test provider supports every bounded capability so capability-bound
# enforcement (model tags ⊆ provider capabilities_supported) does not distract
# from the specific invariant each unit test is checking.
_ALL_CAPS = [c.value for c in Capability]


def _prov(**over):
    d = {
        "provider_id": "deepseek",
        "provider_type": "openai-completions",
        "display_name": "DeepSeek",
        "enabled": True,
        "availability_state": "AVAILABLE",
        "capabilities_supported": list(_ALL_CAPS),
        "credential_ref": "openclaw:provider:deepseek",
        "auth_mode": "api-key",
        "endpoint_ref": None,
        "profile_ref": None,
        "policy_version": "1",
    }
    d.update(over)
    return d


def _model(**over):
    d = {
        "model_id": "deepseek-v4-pro",
        "provider_id": "deepseek",
        "canonical_model_name": "DeepSeek V4 Pro",
        "enabled": True,
        "lifecycle_state": "ACTIVE",
        "context_window_metadata": 1000000,
        "output_limit_metadata": 384000,
        "reasoning_levels_supported": ["MEDIUM"],
        "tool_capabilities": ["code_edit", "shell_exec"],
        "abilities": {"vision": False, "coding": "implementation", "review": False},
        "latency_class": "UNKNOWN",
        "cost_class": "MEDIUM",
        "reliability_class": "UNKNOWN",
        "capability_tags": ["CODE_IMPLEMENTATION", "DEBUGGING"],
        "policy_version": "1",
        "provenance": {"source": "test", "benchmarked": False},
    }
    d.update(over)
    return d


def _registry(providers=("deepseek",), models=(("deepseek", "deepseek-v4-pro"),)):
    p = [_prov(provider_id=pid) for pid in providers]
    m = [_model(provider_id=pid, model_id=mid) for pid, mid in models]
    return ModelRegistry.from_payload(p, m)


# ---------------------------------------------------------------------------
# A — Provider registry
# ---------------------------------------------------------------------------


def test_provider_valid():
    r = _registry()
    p = r.get_provider("deepseek")
    assert p is not None
    assert p.provider_id == "deepseek"
    assert p.enabled is True
    assert p.availability_state == AvailabilityState.AVAILABLE.value
    assert p.credential_ref == "openclaw:provider:deepseek"


def test_provider_duplicate_rejected():
    with pytest.raises(ModelRegistryError) as e:
        ModelRegistry.from_payload([_prov(), _prov()], [])
    assert e.value.code == MODEL_CONFIG_INVALID


def test_provider_malformed_availability_rejected():
    with pytest.raises(ModelRegistryError) as e:
        ModelRegistry.from_payload([_prov(availability_state="SOMETIMES")], [])
    assert e.value.code == MODEL_CONFIG_INVALID


def test_provider_unknown_rejected():
    r = _registry()
    with pytest.raises(ModelRegistryError) as e:
        r.validate_identity("claude", "deepseek-v4-pro")
    assert e.value.code == MODEL_CONFIG_INVALID


def test_provider_disabled_rejected():
    r = _registry(providers=("deepseek",), models=(("deepseek", "deepseek-v4-pro"),))
    # Build a registry where deepseek is disabled.
    r = ModelRegistry.from_payload(
        [_prov(enabled=False)], [_model()]
    )
    with pytest.raises(ModelRegistryError) as e:
        r.validate_identity("deepseek", "deepseek-v4-pro")
    assert e.value.code == PROVIDER_UNAVAILABLE


def test_provider_unavailable_rejected():
    r = ModelRegistry.from_payload(
        [_prov(availability_state="UNAVAILABLE")], [_model()]
    )
    with pytest.raises(ModelRegistryError) as e:
        r.validate_identity("deepseek", "deepseek-v4-pro")
    assert e.value.code == PROVIDER_UNAVAILABLE


def test_provider_unknown_availability_never_available():
    # UNKNOWN must never be treated as AVAILABLE (spec §7).
    r = ModelRegistry.from_payload(
        [_prov(availability_state="UNKNOWN")], [_model()]
    )
    with pytest.raises(ModelRegistryError) as e:
        r.validate_identity("deepseek", "deepseek-v4-pro")
    assert e.value.code == PROVIDER_UNAVAILABLE


# ---------------------------------------------------------------------------
# B — Model registry
# ---------------------------------------------------------------------------


def test_model_valid():
    r = _registry()
    m = r.get_model("deepseek-v4-pro")
    assert m is not None
    assert m.model_id == "deepseek-v4-pro"
    assert m.provider_id == "deepseek"
    assert m.context_window_metadata == 1000000
    assert m.reasoning_levels_supported == ("MEDIUM",)
    assert m.provenance.benchmarked is False


def test_model_unknown_provider_rejected():
    with pytest.raises(ModelRegistryError) as e:
        ModelRegistry.from_payload([_prov()], [_model(provider_id="openai")])
    assert e.value.code == MODEL_CONFIG_INVALID


def test_model_duplicate_rejected():
    with pytest.raises(ModelRegistryError) as e:
        ModelRegistry.from_payload([_prov()], [_model(), _model()])
    assert e.value.code == MODEL_CONFIG_INVALID


def test_model_invalid_metadata_rejected():
    # Invalid reasoning level.
    with pytest.raises(ModelRegistryError) as e:
        ModelRegistry.from_payload([_prov()], [_model(reasoning_levels_supported=["MAXIMUM"])])
    assert e.value.code == MODEL_CONFIG_INVALID


def test_model_invalid_cost_class_rejected():
    with pytest.raises(ModelRegistryError) as e:
        ModelRegistry.from_payload([_prov()], [_model(cost_class="FREE")])
    assert e.value.code == MODEL_CONFIG_INVALID


def test_model_context_bound_rejected():
    with pytest.raises(ModelRegistryError) as e:
        ModelRegistry.from_payload([_prov()], [_model(context_window_metadata=-1)])
    assert e.value.code == MODEL_CONFIG_INVALID


def test_model_lifecycle_retired_rejected():
    r = ModelRegistry.from_payload(
        [_prov()], [_model(lifecycle_state="RETIRED")]
    )
    with pytest.raises(ModelRegistryError) as e:
        r.validate_identity("deepseek", "deepseek-v4-pro")
    assert e.value.code == MODEL_UNAVAILABLE


def test_model_disabled_rejected():
    r = ModelRegistry.from_payload(
        [_prov()], [_model(enabled=False)]
    )
    with pytest.raises(ModelRegistryError) as e:
        r.validate_identity("deepseek", "deepseek-v4-pro")
    assert e.value.code == MODEL_NOT_ALLOWED


def test_model_provider_mismatch_rejected():
    r = ModelRegistry.from_payload(
        [_prov(), _prov(provider_id="openai", display_name="OpenAI")],
        [_model(provider_id="deepseek")],
    )
    # Model belongs to deepseek but identity claims openai -> config invalid.
    with pytest.raises(ModelRegistryError) as e:
        r.validate_identity("openai", "deepseek-v4-pro")
    assert e.value.code == MODEL_CONFIG_INVALID


# ---------------------------------------------------------------------------
# C — Capabilities
# ---------------------------------------------------------------------------


def test_taxonomy_bounded():
    # The taxonomy is a fixed, bounded set (no micro-capabilities).
    assert {c.value for c in Capability} >= {
        "COORDINATION", "SIMPLE_ANALYSIS", "CODE_IMPLEMENTATION",
        "COMPLEX_CODE_IMPLEMENTATION", "DEBUGGING", "REPOSITORY_REASONING",
        "ARCHITECTURE", "SECURITY_REVIEW", "CODE_REVIEW", "ROOT_CAUSE_ANALYSIS",
        "TOOL_USE", "LONG_CONTEXT", "VISION", "STRUCTURED_OUTPUT",
    }


def test_unknown_capability_rejected():
    with pytest.raises(ModelRegistryError) as e:
        ModelRegistry.from_payload(
            [_prov()], [_model(capability_tags=["SUPER_CODING"])]
        )
    assert e.value.code == MODEL_CONFIG_INVALID


def test_requirements_validation():
    req = CapabilityRequirements(
        required_capabilities=("SECURITY_REVIEW",),
        minimum_reasoning_level=ReasoningLevel.HIGH.value,
        tool_requirements=(),
        context_requirement=None,
        independence_requirement=Independence.SAME_MODEL_ALLOWED.value,
        quality_floor=ReliabilityClass.UNKNOWN.value,
    )
    req.validate()  # does not raise

    with pytest.raises(ModelRegistryError):
        CapabilityRequirements(
            required_capabilities=("NOT_A_CAPABILITY",)
        ).validate()
    with pytest.raises(ModelRegistryError):
        CapabilityRequirements(minimum_reasoning_level="EXTREME").validate()


def test_required_vs_optional_capabilities():
    r = ModelRegistry.from_payload(
        [_prov()],
        [_model(capability_tags=["CODE_IMPLEMENTATION"])],
    )
    req = CapabilityRequirements(
        required_capabilities=("CODE_IMPLEMENTATION",),
        optional_capabilities=("SECURITY_REVIEW",),
    )
    # Optional capability is missing -> still a candidate (optional never gates).
    assert r.satisfies_floor(r.get_model("deepseek-v4-pro"), req)
    # Required capability is missing -> not a candidate.
    req2 = CapabilityRequirements(required_capabilities=("SECURITY_REVIEW",))
    assert not r.satisfies_floor(r.get_model("deepseek-v4-pro"), req2)


def test_floor_matching():
    r = ModelRegistry.from_payload(
        [_prov()],
        [
            _model(capability_tags=["CODE_IMPLEMENTATION", "DEBUGGING"],
                   reasoning_levels_supported=["MEDIUM"],
                   context_window_metadata=1000000),
        ],
    )
    m = r.get_model("deepseek-v4-pro")
    ok = CapabilityRequirements(
        required_capabilities=("CODE_IMPLEMENTATION",),
        minimum_reasoning_level=ReasoningLevel.MEDIUM.value,
        context_requirement=100000,
    )
    assert r.satisfies_floor(m, ok)
    # Reasoning floor too high -> fails.
    too_high = CapabilityRequirements(
        required_capabilities=("CODE_IMPLEMENTATION",),
        minimum_reasoning_level=ReasoningLevel.HIGH.value,
    )
    assert not r.satisfies_floor(m, too_high)
    # Context floor beyond metadata -> fails.
    too_big = CapabilityRequirements(context_requirement=5_000_000)
    assert not r.satisfies_floor(m, too_big)


def test_no_cost_selection():
    # E1 never selects/sorts by cost: eligible_models is deterministic by id.
    r = ModelRegistry.from_payload(
        [_prov()],
        [
            _model(model_id="b-model", cost_class="HIGH"),
            _model(model_id="a-model", cost_class="LOW"),
        ],
    )
    req = CapabilityRequirements()
    ids = [m.model_id for m in r.eligible_models(req)]
    assert ids == sorted(ids)
    assert ids == ["a-model", "b-model"]  # id order, NOT cost order


# ---------------------------------------------------------------------------
# D — Reasoning
# ---------------------------------------------------------------------------


def test_reasoning_supported():
    r = ModelRegistry.from_payload(
        [_prov()],
        [_model(reasoning_levels_supported=["MEDIUM", "HIGH"])],
    )
    m = r.get_model("deepseek-v4-pro")
    req = CapabilityRequirements(
        minimum_reasoning_level=ReasoningLevel.MEDIUM.value
    )
    assert r.satisfies_floor(m, req)
    req_high = CapabilityRequirements(
        minimum_reasoning_level=ReasoningLevel.HIGH.value
    )
    assert r.satisfies_floor(m, req_high)


def test_reasoning_unsupported_rejected():
    r = ModelRegistry.from_payload(
        [_prov()],
        [_model(reasoning_levels_supported=["MEDIUM"])],
    )
    m = r.get_model("deepseek-v4-pro")
    req = CapabilityRequirements(
        minimum_reasoning_level=ReasoningLevel.HIGH.value
    )
    assert not r.satisfies_floor(m, req)


def test_reasoning_not_agent_controlled():
    # Reasoning level is a bounded enum; an arbitrary string is rejected and
    # there is no mutation API to change a model's reasoning levels.
    with pytest.raises(ValueError):
        ReasoningLevel("agent-requested-super-reasoning")
    r = _registry()
    m = r.get_model("deepseek-v4-pro")
    assert m.reasoning_levels_supported == ("MEDIUM",)
    # No mutator exists: attempting attribute assignment is impossible (frozen).
    with pytest.raises(AttributeError):
        m.reasoning_levels_supported = ("HIGH",)


# ---------------------------------------------------------------------------
# E — Independence
# ---------------------------------------------------------------------------


def test_independence_same_model_allowed():
    r = ModelRegistry.from_payload(
        [_prov()],
        [_model(model_id="m1"), _model(model_id="m2")],
    )
    req = CapabilityRequirements(
        independence_requirement=Independence.SAME_MODEL_ALLOWED.value
    )
    ids = {m.model_id for m in r.eligible_models(req, reference_model_id="m1")}
    assert "m1" in ids and "m2" in ids


def test_independence_different_model_required():
    r = ModelRegistry.from_payload(
        [_prov()],
        [_model(model_id="writer"), _model(model_id="reviewer")],
    )
    req = CapabilityRequirements(
        independence_requirement=Independence.DIFFERENT_MODEL_REQUIRED.value
    )
    ids = {m.model_id for m in r.eligible_models(req, reference_model_id="writer")}
    assert "writer" not in ids
    assert "reviewer" in ids


def test_independence_different_provider_required():
    r = ModelRegistry.from_payload(
        [_prov(), _prov(provider_id="openai", display_name="OpenAI")],
        [
            _model(model_id="deepseek-model", provider_id="deepseek"),
            _model(model_id="openai-model", provider_id="openai"),
        ],
    )
    req = CapabilityRequirements(
        independence_requirement=Independence.DIFFERENT_PROVIDER_REQUIRED.value
    )
    ids = {m.model_id for m in r.eligible_models(
        req, reference_model_id="deepseek-model")}
    assert "deepseek-model" not in ids
    assert "openai-model" in ids


def test_independence_preferred_does_not_filter():
    # PREFERRED is a soft hint; E1 does not reorder or filter on it.
    r = ModelRegistry.from_payload(
        [_prov(), _prov(provider_id="openai", display_name="OpenAI")],
        [
            _model(model_id="deepseek-model", provider_id="deepseek"),
            _model(model_id="openai-model", provider_id="openai"),
        ],
    )
    req = CapabilityRequirements(
        independence_requirement=Independence.DIFFERENT_PROVIDER_PREFERRED.value
    )
    ids = {m.model_id for m in r.eligible_models(
        req, reference_model_id="deepseek-model")}
    assert ids == {"deepseek-model", "openai-model"}
