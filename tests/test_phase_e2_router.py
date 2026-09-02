"""Phase E2 — adaptive model router unit/component tests (deterministic, offline).

Covers the pure router semantics that do not need a full supervisor runtime:
evidence classification (provider ≠ capability), trigger derivation, the
minimum-sufficient ranking + deterministic tiebreaker, reasoning defaults/
ceilings, the bounded escalation ladder, bootstrap-only eligibility (CASE 15),
independence (CASE 11) and canonical sha256 determinism (§4/§5/§20).

No network, no shell, no provider calls.  Uses the real registry + policy files
plus constructed fixtures where the deterministic semantics need a controlled
model set.
"""

from __future__ import annotations

import json

import pytest

from argent_core import model_router as mr
from argent_core.model_registry import ModelRegistry, reset_default_registry
from argent_core.models import Role


# ---------------------------------------------------------------------------
# thinking_to_reasoning
# ---------------------------------------------------------------------------

def test_thinking_to_reasoning_maps_known_tiers():
    assert mr.thinking_to_reasoning("high") == "HIGH"
    assert mr.thinking_to_reasoning("medium") == "MEDIUM"
    assert mr.thinking_to_reasoning("low") == "LOW"


def test_thinking_to_reasoning_unknown_is_none():
    assert mr.thinking_to_reasoning(None) is None
    assert mr.thinking_to_reasoning("") is None
    assert mr.thinking_to_reasoning("MAX") is None
    assert mr.thinking_to_reasoning(123) is None  # type: ignore


def test_thinking_to_reasoning_case_insensitive():
    assert mr.thinking_to_reasoning("HIGH") == "HIGH"
    assert mr.thinking_to_reasoning(" Medium ") == "MEDIUM"


# ---------------------------------------------------------------------------
# classify_attempt (provider/transport never capability, §14/§15)
# ---------------------------------------------------------------------------

def test_classify_attempt_provider_transport_never_capability():
    # Provider/transport error classes must NEVER become a capability gap.
    assert mr.classify_attempt("CONSUMED", "EXTERNAL", True, True) == "EXTERNAL"
    assert mr.classify_attempt("CONSUMED", "TRANSIENT", True, True) == "TRANSIENT"
    assert mr.classify_attempt("FAILED", "PROVIDER", True, True) == "PROVIDER"


def test_classify_attempt_resource_context_security_owner():
    assert mr.classify_attempt("FAILED", "RESOURCE", False, False) == "RESOURCE"
    assert mr.classify_attempt("FAILED", "CONTEXT", False, False) == "CONTEXT"
    assert mr.classify_attempt("FAILED", "SECURITY", False, False) == "SECURITY"
    assert mr.classify_attempt("FAILED", "OWNER_REQUIRED", False, False) == "OWNER_REQUIRED"


def test_classify_attempt_deterministic_code_failure_is_capability():
    assert mr.classify_attempt("FAILED", "DETERMINISTIC", False, False) == "CAPABILITY"


def test_classify_attempt_consumed_tests_red_is_capability():
    # A successful run followed by a CODE failure signal (tests still red) is
    # the capability gap — the model ran but its code was wrong.
    assert mr.classify_attempt("CONSUMED", "NONE", True, False) == "CAPABILITY"


def test_classify_attempt_consumed_reviewer_reject_is_capability():
    assert mr.classify_attempt("CONSUMED", "NONE", False, True) == "CAPABILITY"


def test_classify_attempt_consumed_clean_is_success():
    assert mr.classify_attempt("CONSUMED", "NONE", False, False) == "SUCCESS"


def test_classify_attempt_incomplete_is_other():
    assert mr.classify_attempt("PENDING", "NONE", False, False) == "OTHER"
    assert mr.classify_attempt("RUNNING", "NONE", False, False) == "OTHER"
    assert mr.classify_attempt("RECOVERY_PENDING", "NONE", False, False) == "OTHER"
    assert mr.classify_attempt("QUARANTINED", "NONE", False, False) == "OTHER"


def test_classify_attempt_failed_without_transport_is_capability():
    # A FAILED dispatch with no transport error class is a deterministic code
    # failure → capability gap.
    assert mr.classify_attempt("FAILED", "NONE", False, False) == "CAPABILITY"


def test_classify_attempt_always_in_bounded_vocabulary():
    for ec in ("NONE", "EXTERNAL", "PROVIDER", "TRANSIENT", "RESOURCE",
               "CONTEXT", "SECURITY", "OWNER_REQUIRED", "DETERMINISTIC"):
        for status in ("CONSUMED", "FAILED", "REJECTED", "PENDING", "RUNNING",
                       "QUARANTINED", "RECOVERY_PENDING"):
            outcome = mr.classify_attempt(status, ec, True, True)
            assert outcome in mr._ATTEMPT_OUTCOMES, (status, ec, outcome)


# ---------------------------------------------------------------------------
# detect_triggers (bounded, evidence-only, §11)
# ---------------------------------------------------------------------------

def _attempt(no, outcome, model="deepseek-v4-pro"):
    return mr.AttemptEvidence(
        attempt_no=no, model_id=model, reasoning_level="MEDIUM",
        outcome_class=outcome, status="CONSUMED",
    )


def test_detect_triggers_repeated_fix_failure_distinct_attempts():
    ev = mr.RoutingEvidence(prior_attempts=(
        _attempt(1, "CAPABILITY"), _attempt(2, "CAPABILITY"),
    ))
    assert "REPEATED_FIX_FAILURE" in mr.detect_triggers(ev)


def test_detect_triggers_not_a_raw_counter():
    # §13: repeated failure is NOT a raw counter — two capability failures on
    # the SAME attempt_no (a duplicate) do not count as "repeated".
    ev = mr.RoutingEvidence(prior_attempts=(
        _attempt(1, "CAPABILITY"), _attempt(1, "CAPABILITY"),
    ))
    assert "REPEATED_FIX_FAILURE" not in mr.detect_triggers(ev)


def test_detect_triggers_tests_still_red():
    ev = mr.RoutingEvidence(test_results=("passed", "failed"))
    assert "TESTS_STILL_RED" in mr.detect_triggers(ev)


def test_detect_triggers_root_cause_unproven():
    ev = mr.RoutingEvidence(open_findings_count=1, confirmed_finding=False)
    assert "ROOT_CAUSE_UNPROVEN" in mr.detect_triggers(ev)
    ev2 = mr.RoutingEvidence(open_findings_count=1, confirmed_finding=True)
    assert "ROOT_CAUSE_UNPROVEN" not in mr.detect_triggers(ev2)


def test_detect_triggers_reviewer_rejected():
    ev = mr.RoutingEvidence(reviewer_verdicts=("reject",))
    assert "REVIEWER_REJECTED_CANDIDATE" in mr.detect_triggers(ev)


def test_detect_triggers_contradictory_evidence():
    ev = mr.RoutingEvidence(
        reviewer_verdicts=("approve", "reject"),
    )
    assert "CONTRADICTORY_EVIDENCE" in mr.detect_triggers(ev)
    ev2 = mr.RoutingEvidence(test_results=("passed", "failed"))
    assert "CONTRADICTORY_EVIDENCE" in mr.detect_triggers(ev2)


def test_detect_triggers_security_and_concurrency():
    assert "SECURITY_COMPLEXITY" in mr.detect_triggers(
        mr.RoutingEvidence(security_relevant=True))
    assert "CONCURRENCY_COMPLEXITY" in mr.detect_triggers(
        mr.RoutingEvidence(concurrency_relevant=True))


def test_detect_triggers_provider_failure_never_capability():
    # §14/§15: provider/transport evidence surfaces PROVIDER_FAILURE (a
    # non-capability code), never a capability trigger.
    ev = mr.RoutingEvidence(prior_attempts=(
        _attempt(1, "EXTERNAL"), _attempt(2, "TRANSIENT"),
    ))
    triggers = mr.detect_triggers(ev)
    assert "PROVIDER_FAILURE" in triggers
    assert "REPEATED_FIX_FAILURE" not in triggers
    assert "TESTS_STILL_RED" not in triggers


def test_detect_triggers_agent_text_has_no_effect():
    # §12: there is no text input to the router at all — evidence is the only
    # input.  This is structural (no free-text field exists on RoutingEvidence).
    fields = [f for f in mr.RoutingEvidence.__dataclass_fields__]
    assert "text" not in fields
    assert "agent_prose" not in fields


# ---------------------------------------------------------------------------
# Router: minimum-sufficient + reasoning + escalation ladder
# ---------------------------------------------------------------------------

def _router():
    reset_default_registry()
    return mr.ModelRouter()


def _req(role, risk="NORMAL", evidence=None, current=0):
    return mr.RoutingRequest(
        job_id="j1", task_id="t1", role=role, risk_class=risk,
        evidence=evidence or mr.RoutingEvidence(),
        current_escalation_level=current,
    )


def test_router_implementer_normal_minimum_sufficient_pro():
    d = _router().route(_req(Role.IMPLEMENTER.value), now_iso="2026-01-01T00:00:00+00:00")
    assert not d.is_terminal
    assert d.model == "deepseek-v4-pro"
    assert d.provider == "deepseek"
    assert d.escalation_level == 1
    assert d.reasoning_level == "MEDIUM"


def test_router_implementer_low_risk_minimum_sufficient_flash():
    d = _router().route(_req(Role.IMPLEMENTER.value, risk="LOW"),
                        now_iso="2026-01-01T00:00:00+00:00")
    assert d.model == "deepseek-v4-flash"
    assert d.escalation_level == 0


def test_router_lead_and_reviewer_are_sol():
    d = _router().route(_req(Role.LEAD.value), now_iso="2026-01-01T00:00:00+00:00")
    assert d.model == "gpt-5.6-sol"
    assert d.provider == "openai"
    assert d.escalation_level == 2
    assert d.reasoning_level == "HIGH"
    # F1: a reviewer is ALWAYS writer-independent.  With a pro writer the
    # reviewer dispatches to sol (a different model); with no writer reference
    # it fails closed (terminal), never a same-model fallback.
    r = _router().route(
        mr.RoutingRequest(
            job_id="j1", task_id="t1", role=Role.REVIEWER.value, risk_class="NORMAL",
            reference_model_id="deepseek-v4-pro",
            independence_requirement="DIFFERENT_MODEL_REQUIRED",
        ),
        now_iso="2026-01-01T00:00:00+00:00",
    )
    assert r.model == "gpt-5.6-sol"
    assert r.reasoning_level == "HIGH"


def test_router_analyst_is_pro():
    d = _router().route(_req(Role.ANALYST.value), now_iso="2026-01-01T00:00:00+00:00")
    assert d.model == "deepseek-v4-pro"
    assert d.escalation_level == 1


def test_router_hard_root_cause_escalates_to_sol():
    # CASE 4 semantics: 2 capability failures + tests red + unproven root cause
    # ⇒ hard root cause ⇒ DEEP_REASONING ⇒ sol (for an implementer).
    ev = mr.RoutingEvidence(
        prior_attempts=(_attempt(1, "CAPABILITY"), _attempt(2, "CAPABILITY")),
        test_results=("failed", "failed"),
        open_findings_count=1, confirmed_finding=False,
    )
    d = _router().route(
        _req(Role.IMPLEMENTER.value, evidence=ev, current=1),
        now_iso="2026-01-01T00:00:00+00:00",
    )
    assert not d.is_terminal
    assert d.model == "gpt-5.6-sol"
    assert d.escalation_level == 2
    assert d.decision_reason_code == "REPEATED_FIX_FAILURE"


def test_router_single_failure_no_escalation():
    # A single capability failure (no repeated) does not escalate by itself.
    ev = mr.RoutingEvidence(prior_attempts=(_attempt(1, "CAPABILITY"),))
    d = _router().route(
        _req(Role.IMPLEMENTER.value, evidence=ev, current=1),
        now_iso="2026-01-01T00:00:00+00:00",
    )
    assert d.model == "deepseek-v4-pro"
    assert d.escalation_level == 1


def test_router_provider_failure_does_not_escalate():
    # CASE 8 semantics: provider/transport failures never raise the level.
    ev = mr.RoutingEvidence(prior_attempts=(
        _attempt(1, "EXTERNAL"), _attempt(2, "TRANSIENT"),
    ))
    d = _router().route(
        _req(Role.IMPLEMENTER.value, evidence=ev, current=1),
        now_iso="2026-01-01T00:00:00+00:00",
    )
    assert d.model == "deepseek-v4-pro"  # no escalation to sol
    assert d.escalation_level == 1
    assert d.decision_reason_code == "PROVIDER_FAILURE"


def test_router_no_downgrade_floor_monotonic():
    # §23: once at level 3, the floor never drops back to a lower entry level.
    d = _router().route(
        _req(Role.IMPLEMENTER.value, current=3),
        now_iso="2026-01-01T00:00:00+00:00",
    )
    assert d.escalation_level == 3


def test_router_max_automatic_level_then_owner_gate():
    # CASE 6 semantics: a further escalation at level 3 exceeds max_automatic
    # level 3 ⇒ terminal OWNER_GATE (fail-closed), never an automatic dispatch.
    ev = mr.RoutingEvidence(
        prior_attempts=(_attempt(1, "CAPABILITY"), _attempt(2, "CAPABILITY")),
        test_results=("failed", "failed"),
        open_findings_count=1, confirmed_finding=False,
    )
    d = _router().route(
        _req(Role.IMPLEMENTER.value, evidence=ev, current=3),
        now_iso="2026-01-01T00:00:00+00:00",
    )
    assert d.is_terminal
    assert d.decision_reason_code == "OWNER_GATE"
    assert d.provider is None and d.model is None


def test_router_escalation_levels_bounded_0_to_4():
    # §10: the ladder is bounded 0..4; never negative, never above 4.
    for cur in (0, 1, 2, 3, 4, 5):
        d = _router().route(
            _req(Role.IMPLEMENTER.value, current=cur),
            now_iso="2026-01-01T00:00:00+00:00",
        )
        assert 0 <= d.escalation_level <= 4


def test_router_security_complexity_role_scoped():
    # F3: SECURITY_COMPLEXITY is role-scoped.  The implementer keeps its
    # implementation capability (never SECURITY_REVIEW) and stays on pro; the
    # analyst escalates to Sol via deep analysis (no SECURITY_REVIEW either).
    ev = mr.RoutingEvidence(security_relevant=True)
    imp = _router().route(
        _req(Role.IMPLEMENTER.value, evidence=ev, current=0),
        now_iso="2026-01-01T00:00:00+00:00",
    )
    assert imp.model == "deepseek-v4-pro"
    assert "SECURITY_REVIEW" not in imp.required_capabilities
    ana = _router().route(
        _req(Role.ANALYST.value, evidence=ev, current=0),
        now_iso="2026-01-01T00:00:00+00:00",
    )
    assert ana.model == "gpt-5.6-sol"
    assert ana.escalation_level == 2
    assert "SECURITY_REVIEW" not in ana.required_capabilities


# ---------------------------------------------------------------------------
# Bootstrap-only eligibility (CASE 15 / §3 / §31)
# ---------------------------------------------------------------------------

def _registry_with_fake_model():
    """A registry that adds an unknown, benchmarked:false model (never eligible)."""
    from argent_core.model_registry import (
        Capability,
        ModelRegistry,
    )
    providers = [
        {
            "provider_id": "deepseek", "provider_type": "openai-completions",
            "display_name": "DeepSeek", "enabled": True,
            "availability_state": "AVAILABLE",
            "capabilities_supported": [
                "COORDINATION", "SIMPLE_ANALYSIS", "REPOSITORY_REASONING",
                "CODE_IMPLEMENTATION", "COMPLEX_CODE_IMPLEMENTATION",
                "DEBUGGING", "TOOL_USE", "STRUCTURED_OUTPUT", "LONG_CONTEXT",
            ],
            "credential_ref": "openclaw:provider:deepseek",
            "auth_mode": "api-key", "endpoint_ref": "https://api.deepseek.com",
            "profile_ref": None, "policy_version": "1",
        },
    ]
    models = [
        {
            "model_id": "deepseek-v4-pro", "provider_id": "deepseek",
            "canonical_model_name": "DeepSeek V4 Pro", "enabled": True,
            "lifecycle_state": "ACTIVE", "context_window_metadata": 1000000,
            "output_limit_metadata": 384000,
            "reasoning_levels_supported": ["MEDIUM"],
            "tool_capabilities": ["code_edit", "shell_exec", "file_access"],
            "abilities": {"vision": False, "coding": "implementation",
                          "review": False},
            "latency_class": "UNKNOWN", "cost_class": "MEDIUM",
            "reliability_class": "UNKNOWN",
            "capability_tags": [
                "SIMPLE_ANALYSIS", "REPOSITORY_REASONING", "CODE_IMPLEMENTATION",
                "COMPLEX_CODE_IMPLEMENTATION", "DEBUGGING", "TOOL_USE",
                "STRUCTURED_OUTPUT", "LONG_CONTEXT",
            ],
            "policy_version": "1",
            "provenance": {"source": "routing.py", "benchmarked": False},
        },
        {
            # A model the policy does NOT list for any profile — it must never
            # be auto-eligible, even though the registry describes it.
            "model_id": "fake-super-model", "provider_id": "deepseek",
            "canonical_model_name": "Fake Super Model", "enabled": True,
            "lifecycle_state": "ACTIVE", "context_window_metadata": 1000000,
            "output_limit_metadata": 384000,
            "reasoning_levels_supported": ["HIGH"],
            "tool_capabilities": ["code_edit", "shell_exec", "file_access"],
            "abilities": {"vision": True, "coding": "implementation",
                          "review": True},
            "latency_class": "UNKNOWN", "cost_class": "HIGH",
            "reliability_class": "HIGH",
            "capability_tags": [
                "COORDINATION", "SIMPLE_ANALYSIS", "REPOSITORY_REASONING",
                "CODE_IMPLEMENTATION", "COMPLEX_CODE_IMPLEMENTATION",
                "DEBUGGING", "TOOL_USE", "STRUCTURED_OUTPUT", "LONG_CONTEXT",
            ],
            "policy_version": "1",
            "provenance": {"source": "test", "benchmarked": False},
        },
    ]
    return ModelRegistry.from_payload(providers, models)


def test_router_bootstrap_only_never_auto_eligible():
    # CASE 15: a registry-listed but policy-unlisted model (benchmarked:false)
    # is never selected, even if it claims superior capabilities.
    reg = _registry_with_fake_model()
    pol = mr.load_routing_policy()
    router = mr.ModelRouter(reg, pol)
    d = router.route(_req(Role.IMPLEMENTER.value), now_iso="2026-01-01T00:00:00+00:00")
    assert d.model == "deepseek-v4-pro"
    assert d.model != "fake-super-model"
    # Even for lead (which wants HIGH): the fake model is not in allowed_models.
    d2 = router.route(_req(Role.LEAD.value), now_iso="2026-01-01T00:00:00+00:00")
    # lead's allowed_models = ["gpt-5.6-sol"], which is NOT in this registry →
    # no eligible candidate → terminal.
    assert d2.is_terminal


# ---------------------------------------------------------------------------
# Independence (CASE 11 / §17)
# ---------------------------------------------------------------------------

def test_router_reviewer_independence_different_model():
    # A reviewer must never be the same model as the writer when the
    # independence requirement is DIFFERENT_MODEL_REQUIRED.
    reg = mr.get_default_registry()
    pol = mr.load_routing_policy()
    router = mr.ModelRouter(reg, pol)
    # reference_model_id = sol (the writer); the reviewer profile allows only
    # sol → with DIFFERENT_MODEL_REQUIRED there is NO candidate → terminal.
    req = mr.RoutingRequest(
        job_id="j1", task_id="t1", role=Role.REVIEWER.value, risk_class="NORMAL",
        reference_model_id="gpt-5.6-sol",
        independence_requirement="DIFFERENT_MODEL_REQUIRED",
    )
    d = router.route(req, now_iso="2026-01-01T00:00:00+00:00")
    assert d.is_terminal
    assert d.decision_reason_code == "NO_ELIGIBLE_CANDIDATE"


def test_router_independence_no_reference_fails_closed():
    # A hard independence constraint without a reference cannot be evaluated →
    # fail closed.
    reg = mr.get_default_registry()
    pol = mr.load_routing_policy()
    router = mr.ModelRouter(reg, pol)
    req = mr.RoutingRequest(
        job_id="j1", task_id="t1", role=Role.REVIEWER.value, risk_class="NORMAL",
        independence_requirement="DIFFERENT_PROVIDER_REQUIRED",
    )
    d = router.route(req, now_iso="2026-01-01T00:00:00+00:00")
    assert d.is_terminal


# ---------------------------------------------------------------------------
# Determinism + canonical sha256 (§4/§5/§20)
# ---------------------------------------------------------------------------

def test_router_deterministic_decision_and_sha256():
    d1 = _router().route(_req(Role.IMPLEMENTER.value),
                         now_iso="2026-01-01T00:00:00+00:00")
    d2 = _router().route(_req(Role.IMPLEMENTER.value),
                         now_iso="2026-01-01T00:00:00+00:00")
    assert d1.decision_id == d2.decision_id
    assert d1.sha256 == d2.sha256
    assert d1.requirements_hash == d2.requirements_hash
    # sha256 is a full 64-hex digest of the canonical decision payload.
    assert len(d1.sha256) == 64


def test_router_decision_reason_code_is_bounded():
    d = _router().route(_req(Role.IMPLEMENTER.value),
                        now_iso="2026-01-01T00:00:00+00:00")
    assert d.decision_reason_code in {c.value for c in mr.RoutingReasonCode}


# ---------------------------------------------------------------------------
# Policy loading fail-closed
# ---------------------------------------------------------------------------

def test_policy_load_valid():
    pol = mr.load_routing_policy()
    assert pol.version == "1"
    assert pol.max_automatic_level == 3
    assert pol.owner_level == 4
    assert pol.bootstrap is True
    assert pol.benchmark_required_for_new_models is True


def test_policy_load_rejects_unknown_field():
    import pathlib
    real = pathlib.Path(mr.__file__).resolve().parent / "registry" / "routing_policy_v1.json"
    doc = json.loads(real.read_text())
    doc["totally_unknown_key"] = True
    with pytest.raises(mr.RoutingError) as exc:
        mr.RoutingPolicy(doc)
    assert exc.value.code == mr.ROUTING_POLICY_INVALID


def test_policy_load_rejects_bad_escalation():
    import pathlib
    real = pathlib.Path(mr.__file__).resolve().parent / "registry" / "routing_policy_v1.json"
    doc = json.loads(real.read_text())
    doc["escalation"]["max_automatic_level"] = 4  # == owner_level → invalid
    with pytest.raises(mr.RoutingError) as exc:
        mr.RoutingPolicy(doc)
    assert exc.value.code == mr.ROUTING_POLICY_INVALID
