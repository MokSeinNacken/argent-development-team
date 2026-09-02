"""Phase E1 — Fix-Round (F1–F7) adversarial + regression tests.

Covers the Supervisor-confirmed findings against the registry/descriptor
hardening:

* F1 — factory-only construction (key↔id equality, frozen descriptors, version
  consistency, read-only maps) + ``Core`` rejects duck-typed registries.
* F2 — claim invariants (``benchmarked:false``, trusted-local source allowlist,
  provider ``capabilities_supported`` as an upper bound at load).
* F3 — schema/secret strictness (exact key allowlists, secret key names,
  opaque-ref/endpoint grammar, top-level type checks, policy_version).
* F4 — ``CapabilityRequirements`` canonicalization (bool exclusion, list|tuple →
  frozen tuple, duplicate rejection, enum ``.value``, frozen instance).
* F5 — one canonical eligibility predicate shared by ``eligible_models`` and
  ``is_fallback_eligible`` (RETIRED excluded, unknown reference fail-closed).
* F6 — data claims (flash COORDINATION + SIMPLE_ANALYSIS, provider caps = union).
* F7 — registry validation runs inside ``work()`` after idempotency replay.

Deterministic, local, no network, no provider calls.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from argent_core import (
    MODEL_CONFIG_INVALID,
    Capability,
    CapabilityRequirements,
    Core,
    ModelDescriptor,
    ModelRegistry,
    ModelRegistryError,
    ProviderDescriptor,
    ReasoningLevel,
    Role,
    SequenceKind,
    get_default_registry,
    reset_default_registry,
    role_source,
)
from argent_core import OWNER_SOURCE

LEAD = role_source(Role.LEAD)
OWNER = OWNER_SOURCE

# A test provider supports every bounded capability so capability-bound
# enforcement does not distract from the invariant each test checks.
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


# ---------------------------------------------------------------------------
# F1 — factory-only construction + fail-closed Core injection
# ---------------------------------------------------------------------------


def test_f1_key_mismatch_rejected():
    pd = ProviderDescriptor(
        provider_id="deepseek", provider_type="openai-completions",
        display_name="DeepSeek", enabled=True, availability_state="AVAILABLE",
    )
    with pytest.raises(ModelRegistryError) as e:
        ModelRegistry({"wrong-key": pd}, {})
    assert e.value.code == MODEL_CONFIG_INVALID


def test_f1_non_descriptor_rejected():
    with pytest.raises(ModelRegistryError) as e:
        ModelRegistry({"deepseek": object()}, {})
    assert e.value.code == MODEL_CONFIG_INVALID


def test_f1_version_mismatch_rejected():
    pd = ProviderDescriptor(
        provider_id="deepseek", provider_type="openai-completions",
        display_name="DeepSeek", enabled=True, availability_state="AVAILABLE",
    )
    with pytest.raises(ModelRegistryError) as e:
        ModelRegistry({"deepseek": pd}, {}, version="99")
    assert e.value.code == MODEL_CONFIG_INVALID


def test_f1_maps_read_only():
    r = ModelRegistry.from_payload([_prov()], [_model()])
    assert isinstance(r._providers, MappingProxyType)
    assert isinstance(r._models, MappingProxyType)
    pd = r.get_provider("deepseek")
    with pytest.raises(TypeError):
        r._providers["hacked"] = pd
    with pytest.raises(TypeError):
        r._models["hacked"] = r.get_model("deepseek-v4-pro")


def test_f1_fake_registry_in_core_fails_closed(tmp_path):
    class FakeRegistry:
        def validate_identity(self, *a, **k):
            return True

    with pytest.raises(ModelRegistryError) as e:
        Core(str(tmp_path / "fake.db"), registry=FakeRegistry())
    assert e.value.code == MODEL_CONFIG_INVALID


def test_f1_valid_injected_registry_ok(tmp_path):
    reg = ModelRegistry.from_payload([_prov()], [_model()])
    c = Core(str(tmp_path / "ok.db"), registry=reg)
    assert c._model_registry() is reg
    c.close()


# ---------------------------------------------------------------------------
# F2 — claim invariants + provider capability upper bound
# ---------------------------------------------------------------------------


def test_f2_benchmarked_true_rejected():
    with pytest.raises(ModelRegistryError) as e:
        ModelRegistry.from_payload(
            [_prov()],
            [_model(provenance={"source": "test", "benchmarked": True})],
        )
    assert e.value.code == MODEL_CONFIG_INVALID


def test_f2_agent_origin_source_rejected():
    with pytest.raises(ModelRegistryError) as e:
        ModelRegistry.from_payload(
            [_prov()],
            [_model(provenance={"source": "model-self-report: sol", "benchmarked": False})],
        )
    assert e.value.code == MODEL_CONFIG_INVALID


def test_f2_untrusted_source_rejected():
    with pytest.raises(ModelRegistryError) as e:
        ModelRegistry.from_payload(
            [_prov()],
            [_model(provenance={"source": "https://example.com/claims", "benchmarked": False})],
        )
    assert e.value.code == MODEL_CONFIG_INVALID


def test_f2_model_tags_subset_of_provider_caps():
    # Model claims a tag its provider does not support -> fail at load.
    with pytest.raises(ModelRegistryError) as e:
        ModelRegistry.from_payload(
            [_prov(capabilities_supported=["CODE_IMPLEMENTATION"])],
            [_model(capability_tags=["CODE_IMPLEMENTATION", "DEBUGGING"])],
        )
    assert e.value.code == MODEL_CONFIG_INVALID
    # A proper subset is accepted.
    r = ModelRegistry.from_payload(
        [_prov(capabilities_supported=["CODE_IMPLEMENTATION", "DEBUGGING"])],
        [_model(capability_tags=["CODE_IMPLEMENTATION"])],
    )
    assert r.get_model("deepseek-v4-pro") is not None


# ---------------------------------------------------------------------------
# F3 — schema/secret strictness
# ---------------------------------------------------------------------------


def test_f3_secret_key_name_rejected():
    with pytest.raises(ModelRegistryError) as e:
        ModelRegistry.from_payload([_prov(api_key="sk-abcdef")], [])
    assert e.value.code == MODEL_CONFIG_INVALID
    # case-insensitive separator-insensitive
    with pytest.raises(ModelRegistryError):
        ModelRegistry.from_payload([_prov(Authorization="Bearer x")], [])


def test_f3_userinfo_endpoint_rejected():
    with pytest.raises(ModelRegistryError) as e:
        ModelRegistry.from_payload(
            [_prov(endpoint_ref="https://user:pass@api.deepseek.com")], []
        )
    assert e.value.code == MODEL_CONFIG_INVALID


def test_f3_abilities_not_dict_rejected():
    with pytest.raises(ModelRegistryError):
        ModelRegistry.from_payload([_prov()], [_model(abilities=[])])
    with pytest.raises(ModelRegistryError):
        ModelRegistry.from_payload([_prov()], [_model(abilities="")])
    # Missing abilities -> default (no truthiness coercion).
    m = _model()
    m.pop("abilities")
    r = ModelRegistry.from_payload([_prov()], [m])
    assert r.get_model("deepseek-v4-pro").abilities.vision is None


def test_f3_top_level_list_rejected(tmp_path):
    (tmp_path / "providers.json").write_text("[]", encoding="utf-8")
    (tmp_path / "models.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ModelRegistryError) as e:
        ModelRegistry.load_files(str(tmp_path))
    assert e.value.code == MODEL_CONFIG_INVALID


def test_f3_unknown_top_level_key_rejected(tmp_path):
    (tmp_path / "providers.json").write_text(
        '{"registry_version":"1","policy_version":"1","providers":[],"extra":1}',
        encoding="utf-8",
    )
    (tmp_path / "models.json").write_text(
        '{"registry_version":"1","policy_version":"1","models":[]}',
        encoding="utf-8",
    )
    with pytest.raises(ModelRegistryError) as e:
        ModelRegistry.load_files(str(tmp_path))
    assert e.value.code == MODEL_CONFIG_INVALID


def test_f3_policy_version_mismatch_rejected(tmp_path):
    (tmp_path / "providers.json").write_text(
        '{"registry_version":"1","policy_version":"2","providers":[]}',
        encoding="utf-8",
    )
    (tmp_path / "models.json").write_text(
        '{"registry_version":"1","policy_version":"1","models":[]}',
        encoding="utf-8",
    )
    with pytest.raises(ModelRegistryError) as e:
        ModelRegistry.load_files(str(tmp_path))
    assert e.value.code == MODEL_CONFIG_INVALID


def test_f3_entry_policy_version_mismatch_rejected():
    with pytest.raises(ModelRegistryError) as e:
        ModelRegistry.from_payload([_prov(policy_version="9")], [])
    assert e.value.code == MODEL_CONFIG_INVALID


# ---------------------------------------------------------------------------
# F4 — CapabilityRequirements canonicalization
# ---------------------------------------------------------------------------


def test_f4_context_requirement_bool_rejected():
    with pytest.raises(ModelRegistryError) as e:
        CapabilityRequirements(context_requirement=True)
    assert e.value.code == MODEL_CONFIG_INVALID


def test_f4_sequence_canonicalized_to_tuple():
    req = CapabilityRequirements(
        required_capabilities=["SECURITY_REVIEW"],
        tool_requirements=["code_edit"],
    )
    assert isinstance(req.required_capabilities, tuple)
    assert req.required_capabilities == ("SECURITY_REVIEW",)
    assert req.tool_requirements == ("code_edit",)


def test_f4_enum_member_canonicalized():
    req = CapabilityRequirements(
        required_capabilities=[Capability.SECURITY_REVIEW],
        minimum_reasoning_level=ReasoningLevel.HIGH,
    )
    assert req.required_capabilities == ("SECURITY_REVIEW",)
    assert req.minimum_reasoning_level == "HIGH"


def test_f4_duplicates_rejected():
    with pytest.raises(ModelRegistryError) as e:
        CapabilityRequirements(
            required_capabilities=("SECURITY_REVIEW", "SECURITY_REVIEW")
        )
    assert e.value.code == MODEL_CONFIG_INVALID


def test_f4_non_sequence_rejected():
    with pytest.raises(ModelRegistryError) as e:
        CapabilityRequirements(required_capabilities="SECURITY_REVIEW")
    assert e.value.code == MODEL_CONFIG_INVALID


def test_f4_frozen_after_construction():
    req = CapabilityRequirements(required_capabilities=("SECURITY_REVIEW",))
    with pytest.raises(AttributeError):
        req.required_capabilities = ()


# ---------------------------------------------------------------------------
# F5 — single canonical eligibility predicate
# ---------------------------------------------------------------------------


def test_f5_retired_not_fallback_eligible():
    r = ModelRegistry.from_payload([_prov()], [_model(lifecycle_state="RETIRED")])
    req = CapabilityRequirements()
    assert not r.is_fallback_eligible("deepseek-v4-pro", req)
    assert "deepseek-v4-pro" not in {m.model_id for m in r.eligible_models(req)}


def test_f5_consistency_eligible_and_fallback():
    r = ModelRegistry.from_payload(
        [_prov()],
        [_model(model_id="a"), _model(model_id="b", lifecycle_state="RETIRED")],
    )
    req = CapabilityRequirements()
    eligible = {m.model_id for m in r.eligible_models(req)}
    assert eligible == {"a"}
    for m in r.list_models():
        assert r.is_fallback_eligible(m.model_id, req) == (m.model_id in eligible)


def test_f5_unknown_reference_model_invalid():
    r = ModelRegistry.from_payload([_prov()], [_model()])
    req = CapabilityRequirements()
    with pytest.raises(ModelRegistryError) as e:
        r.eligible_models(req, reference_model_id="nope")
    assert e.value.code == MODEL_CONFIG_INVALID
    with pytest.raises(ModelRegistryError) as e:
        r.is_fallback_eligible("deepseek-v4-pro", req, reference_model_id="nope")
    assert e.value.code == MODEL_CONFIG_INVALID


def test_f5_policy_allows_fallback_false():
    r = ModelRegistry.from_payload([_prov()], [_model()])
    req = CapabilityRequirements()
    assert r.is_fallback_eligible(
        "deepseek-v4-pro", req, policy_allows_fallback=False
    ) is False


# ---------------------------------------------------------------------------
# F6 — data claims
# ---------------------------------------------------------------------------


def test_f6_flash_coordination_and_simple_analysis():
    reset_default_registry()
    r = get_default_registry()
    flash = r.get_model("deepseek-v4-flash")
    assert "COORDINATION" in flash.capability_tags
    assert "SIMPLE_ANALYSIS" in flash.capability_tags
    reset_default_registry()


def test_f6_provider_caps_upper_bound():
    reset_default_registry()
    r = get_default_registry()
    for p in r.list_providers():
        for m in r.list_models():
            if m.provider_id == p.provider_id:
                assert set(m.capability_tags) <= set(p.capabilities_supported), (
                    m.model_id, p.provider_id,
                )
    ds = r.get_provider("deepseek")
    union = set(r.get_model("deepseek-v4-flash").capability_tags) | set(
        r.get_model("deepseek-v4-pro").capability_tags
    )
    assert set(ds.capabilities_supported) >= union
    reset_default_registry()


def test_f6_security_review_only_sol():
    reset_default_registry()
    r = get_default_registry()
    req = CapabilityRequirements(required_capabilities=("SECURITY_REVIEW",))
    ids = {m.model_id for m in r.eligible_models(req)}
    assert ids == {"gpt-5.6-sol"}
    reset_default_registry()


# ---------------------------------------------------------------------------
# F7 — registry validation after idempotency replay (new dispatches only)
# ---------------------------------------------------------------------------


def test_f7_idempotent_replay_skips_registry_validation(tmp_path):
    db = str(tmp_path / "f7.db")

    # Registry B: sol is DISABLED (a NEW lead dispatch would fail closed).
    reg_b = ModelRegistry.from_payload(
        [_openai_prov(), _deepseek_prov()],
        [
            _sol_model(enabled=False),
            _model(model_id="deepseek-v4-pro"),
        ],
    )

    # Core A uses the default registry (sol enabled) and creates the dispatch.
    c_a = Core(db)
    project = c_a.create_project("p", OWNER)
    task = c_a.create_task(project.id, "t", OWNER)
    task_run = c_a.start_task_run(task.id, OWNER)
    c_a.start_role(task.id, Role.LEAD, LEAD)
    d1 = c_a.create_dispatch(
        task.id, task_run.id, Role.LEAD, 0, 1, SequenceKind.STANDARD, None, LEAD,
        idempotency_key="k1",
    )
    assert d1.expected_model_class == "gpt-5.6-sol"
    c_a.close()

    # Core B: same DB, registry B (sol disabled).  The identical idempotent
    # create_dispatch must return the persisted dispatch WITHOUT raising.
    c_b = Core(db, registry=reg_b)
    d2 = c_b.create_dispatch(
        task.id, task_run.id, Role.LEAD, 0, 1, SequenceKind.STANDARD, None, LEAD,
        idempotency_key="k1",
    )
    assert d2.id == d1.id
    assert d2.expected_model_class == "gpt-5.6-sol"
    c_b.close()
