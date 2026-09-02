"""Phase E3 — evidence registry + validated fallback + provenance (unit/component).

Deterministic, offline.  Covers the new E3 semantics that do not need a full
supervisor runtime: bounded evidence status/categories, fail-closed evidence
loading, the independent evidence gate (UNKNOWN/REJECTED never eligible), the
validated availability fallback (CASE 5/6/7/14/24), determinism + provenance
(CASE 16/17), and malformed policy/registry rejection (CASE 10).

No network, no shell, no provider calls.  Uses the real registry/policy/evidence
files plus constructed fixtures where deterministic semantics need a controlled
set.
"""

from __future__ import annotations

import json

import pytest

from argent_core import evidence_registry as er
from argent_core import model_router as mr
from argent_core.model_registry import reset_default_registry
from argent_core.models import Role


# ---------------------------------------------------------------------------
# Evidence registry: loading + bounded status
# ---------------------------------------------------------------------------

def test_evidence_registry_loads_bootstrap_models():
    reset_default_registry()
    er.reset_default_evidence_registry()
    ev = er.get_default_evidence_registry()
    assert ev.version == "1"
    assert set(ev.list_models()) == {
        "deepseek-v4-flash", "deepseek-v4-pro", "gpt-5.6-sol",
    }
    # Honest bounded status: nothing VERIFIED (no real benchmarks exist).
    for model in ev.list_models():
        for cat in er.EvidenceCategory:
            status = ev.get_status(model, cat.value)
            assert status in {s.value for s in er.EvidenceStatus}
            assert status != er.EvidenceStatus.VERIFIED.value


def test_evidence_registry_provisional_for_required_categories():
    ev = er.get_default_evidence_registry()
    assert ev.get_status("deepseek-v4-pro", "repository_coding") == "PROVISIONAL"
    assert ev.get_status("gpt-5.6-sol", "security_review") == "PROVISIONAL"
    assert ev.get_status("deepseek-v4-flash", "coordination_basic_reasoning") == "PROVISIONAL"


def test_evidence_missing_is_unknown():
    ev = er.get_default_evidence_registry()
    assert ev.get_status("gpt-5.6-sol", "long_context") == "UNKNOWN"
    assert ev.get_status("deepseek-v4-flash", "architecture") == "UNKNOWN"


def test_capability_category_mapping_is_total():
    from argent_core.model_registry import Capability
    for cap in Capability:
        assert er.capability_category(cap.value) in {c.value for c in er.EvidenceCategory}


def test_satisfies_evidence_provisional_minimum():
    ev = er.get_default_evidence_registry()
    # pro meets PROVISIONAL for CODE_IMPLEMENTATION (repository_coding).
    assert er.satisfies_evidence(ev, "deepseek-v4-pro", ["CODE_IMPLEMENTATION"], "PROVISIONAL")
    # flash has no ARCHITECTURE evidence (UNKNOWN) -> fails a PROVISIONAL floor.
    assert not er.satisfies_evidence(ev, "deepseek-v4-flash", ["ARCHITECTURE"], "PROVISIONAL")
    # A minimum of UNKNOWN is vacuous (satisfies everything) — sanity only.
    assert er.satisfies_evidence(ev, "deepseek-v4-flash", ["ARCHITECTURE"], "UNKNOWN")


# ---------------------------------------------------------------------------
# Evidence registry: fail-closed loading
# ---------------------------------------------------------------------------

def _ev_payload():
    return [
        {
            "model_id": "deepseek-v4-pro",
            "categories": {
                "repository_coding": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x",
                    "version": "1", "benchmarked": False,
                },
            },
        },
    ]


def test_evidence_rejects_unknown_field():
    payload = _ev_payload()
    payload[0]["categories"]["repository_coding"]["bogus"] = 1
    with pytest.raises(er.ModelRegistryError):
        er.EvidenceRegistry.from_payload(payload)


def test_evidence_rejects_invalid_status():
    payload = _ev_payload()
    payload[0]["categories"]["repository_coding"]["status"] = "GOLD"
    with pytest.raises(er.ModelRegistryError):
        er.EvidenceRegistry.from_payload(payload)


def test_evidence_rejects_verified_without_benchmarks():
    payload = _ev_payload()
    payload[0]["categories"]["repository_coding"]["status"] = "VERIFIED"
    with pytest.raises(er.ModelRegistryError):
        er.EvidenceRegistry.from_payload(payload)


def test_evidence_rejects_benchmarked_true():
    payload = _ev_payload()
    payload[0]["categories"]["repository_coding"]["benchmarked"] = True
    with pytest.raises(er.ModelRegistryError):
        er.EvidenceRegistry.from_payload(payload)


def test_evidence_rejects_agent_origin_ref():
    payload = _ev_payload()
    payload[0]["categories"]["repository_coding"]["evidence_ref"] = "agent-output: x"
    with pytest.raises(er.ModelRegistryError):
        er.EvidenceRegistry.from_payload(payload)


def test_evidence_rejects_duplicate_model():
    payload = _ev_payload() + _ev_payload()
    with pytest.raises(er.ModelRegistryError):
        er.EvidenceRegistry.from_payload(payload)


def test_evidence_rejects_unknown_model_ref():
    payload = [
        {
            "model_id": "totally-unknown-model",
            "categories": {
                "repository_coding": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x",
                    "version": "1", "benchmarked": False,
                },
            },
        },
    ]
    ev = er.EvidenceRegistry.from_payload(payload)
    from argent_core.model_registry import get_default_registry
    with pytest.raises(er.ModelRegistryError):
        ev.validate_model_refs(get_default_registry())


# ---------------------------------------------------------------------------
# AvailabilitySnapshot
# ---------------------------------------------------------------------------

def test_availability_snapshot_valid():
    snap = mr.AvailabilitySnapshot(model_states={"deepseek-v4-flash": "UNAVAILABLE"})
    assert snap.model_states["deepseek-v4-flash"] == "UNAVAILABLE"
    assert snap.canonical()["model_states"] == {"deepseek-v4-flash": "UNAVAILABLE"}


def test_availability_snapshot_rejects_unknown_state():
    with pytest.raises(mr.RoutingError):
        mr.AvailabilitySnapshot(model_states={"deepseek-v4-flash": "BROKEN"})


def test_availability_snapshot_rejects_too_many():
    states = {f"model-{i}": "UNAVAILABLE" for i in range(200)}
    with pytest.raises(mr.RoutingError):
        mr.AvailabilitySnapshot(model_states=states)


# ---------------------------------------------------------------------------
# Router: evidence gate (CASE 11/12 — UNKNOWN/REJECTED never eligible)
# ---------------------------------------------------------------------------

def _router_with_evidence(evidence):
    reset_default_registry()
    er.reset_default_evidence_registry()
    reg = mr.get_default_registry()
    pol = mr.load_routing_policy()
    return mr.ModelRouter(reg, pol, evidence=evidence)


def _evidence_without_flash_repository_coding():
    """Evidence for pro + sol, but flash's repository_coding is ABSENT (UNKNOWN)."""
    return er.EvidenceRegistry.from_payload([
        {
            "model_id": "deepseek-v4-pro",
            "categories": {
                "coordination_basic_reasoning": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
                "repository_coding": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
                "debugging_root_cause": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
                "tool_agent": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
                "long_context": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
            },
        },
        {
            "model_id": "gpt-5.6-sol",
            "categories": {
                "coordination_basic_reasoning": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
                "repository_coding": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
                "debugging_root_cause": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
                "architecture": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
                "security_review": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
                "tool_agent": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
            },
        },
    ])


def test_router_evidence_gate_unknown_not_eligible():
    # flash is cheaper and policy-authorised for implementer LOW risk, but its
    # repository_coding evidence is UNKNOWN -> never eligible (CASE 11/12).
    ev = _evidence_without_flash_repository_coding()
    router = _router_with_evidence(ev)
    req = mr.RoutingRequest(job_id="j", task_id="t", role=Role.IMPLEMENTER.value,
                            risk_class="LOW")
    d = router.route(req, now_iso="2026-01-01T00:00:00+00:00")
    assert d.model == "deepseek-v4-pro"  # NOT flash
    assert d.model != "deepseek-v4-flash"


def test_router_evidence_gate_rejected_not_eligible():
    # A REJECTED evidence status must never be eligible (CASE 12).
    ev = er.EvidenceRegistry.from_payload([
        {
            "model_id": "deepseek-v4-flash",
            "categories": {
                "coordination_basic_reasoning": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
                "repository_coding": {
                    "status": "REJECTED", "evidence_ref": "routing.py: x", "version": "1",
                },
                "debugging_root_cause": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
                "tool_agent": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
                "long_context": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
            },
        },
        {
            "model_id": "deepseek-v4-pro",
            "categories": {
                "coordination_basic_reasoning": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
                "repository_coding": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
                "debugging_root_cause": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
                "tool_agent": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
                "long_context": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
            },
        },
        {
            "model_id": "gpt-5.6-sol",
            "categories": {
                "coordination_basic_reasoning": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
                "repository_coding": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
                "debugging_root_cause": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
                "architecture": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
                "security_review": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
                "tool_agent": {
                    "status": "PROVISIONAL", "evidence_ref": "routing.py: x", "version": "1",
                },
            },
        },
    ])
    router = _router_with_evidence(ev)
    req = mr.RoutingRequest(job_id="j", task_id="t", role=Role.IMPLEMENTER.value,
                            risk_class="LOW")
    d = router.route(req, now_iso="2026-01-01T00:00:00+00:00")
    assert d.model == "deepseek-v4-pro"


# ---------------------------------------------------------------------------
# Validated fallback (CASE 5/6/7/14/24)
# ---------------------------------------------------------------------------

def _router():
    reset_default_registry()
    er.reset_default_evidence_registry()
    return mr.ModelRouter()


def _req(role, risk="NORMAL", evidence=None, current=0, snapshot=None,
         reference_model_id=None, independence=None):
    return mr.RoutingRequest(
        job_id="j1", task_id="t1", role=role, risk_class=risk,
        evidence=evidence or mr.RoutingEvidence(),
        current_escalation_level=current,
        availability_snapshot=snapshot,
        reference_model_id=reference_model_id,
        independence_requirement=independence,
    )


def test_case5_fallback_flash_unavailable_to_pro():
    # CASE 5: primary provider/model unavailable + policy-authorised floor-
    # meeting alternative -> validated fallback, same escalation level.
    snap = mr.AvailabilitySnapshot(model_states={"deepseek-v4-flash": "UNAVAILABLE"})
    d = _router().route(
        _req(Role.IMPLEMENTER.value, risk="LOW", snapshot=snap),
        now_iso="2026-01-01T00:00:00+00:00",
    )
    assert not d.is_terminal
    assert d.model == "deepseek-v4-pro"
    assert d.provider == "deepseek"
    assert d.decision_reason_code == "VALIDATED_FALLBACK"
    assert d.escalation_level == 0  # fallback never raises the level


def test_case6_fallback_never_below_floor():
    # CASE 6/4: a cheaper below-floor candidate is never chosen as fallback.
    # For implementer, sol (openai) has no CODE_IMPLEMENTATION -> below floor;
    # it must never be selected even if deepseek is entirely unavailable.
    snap = mr.AvailabilitySnapshot(provider_states={"deepseek": "UNAVAILABLE"})
    d = _router().route(
        _req(Role.IMPLEMENTER.value, risk="LOW", snapshot=snap),
        now_iso="2026-01-01T00:00:00+00:00",
    )
    # flash + pro are both deepseek -> unavailable; sol is below floor -> no
    # candidate meets floor -> terminal (NO_VALID_FALLBACK, never a downgrade).
    assert d.is_terminal
    assert d.model is None


def test_case7_no_candidate_meets_floor_fail_closed():
    # CASE 7: no candidate meets the floor -> fail-closed, never a downgrade.
    snap = mr.AvailabilitySnapshot(provider_states={"deepseek": "UNAVAILABLE"})
    d = _router().route(
        _req(Role.IMPLEMENTER.value, snapshot=snap),
        now_iso="2026-01-01T00:00:00+00:00",
    )
    assert d.is_terminal
    assert d.decision_reason_code == "NO_VALID_FALLBACK"


def test_case14_reviewer_independence_survives_fallback():
    # CASE 14: the writer is pro; the closing reviewer (only sol allowed) has
    # sol unavailable -> the ONLY candidate is unavailable, so it fails closed
    # (NO_VALID_FALLBACK).  It NEVER falls back to the writer model (pro is not
    # even a reviewer candidate) — independence survives the fallback path.
    snap = mr.AvailabilitySnapshot(model_states={"gpt-5.6-sol": "UNAVAILABLE"})
    d = _router().route(
        _req(Role.REVIEWER.value, snapshot=snap,
             reference_model_id="deepseek-v4-pro",
             independence="DIFFERENT_MODEL_REQUIRED"),
        now_iso="2026-01-01T00:00:00+00:00",
    )
    assert d.is_terminal
    assert d.model is None
    assert d.decision_reason_code == "NO_VALID_FALLBACK"


def test_case24_unavailable_strong_security_model_no_weaker_review():
    # CASE 24: strong security model unavailable without an equivalent fallback
    # -> NO silent weaker security review (fail-closed).
    snap = mr.AvailabilitySnapshot(model_states={"gpt-5.6-sol": "UNAVAILABLE"})
    ev = mr.RoutingEvidence(security_relevant=True)
    d = _router().route(
        _req(Role.REVIEWER.value, evidence=ev, snapshot=snap,
             reference_model_id="deepseek-v4-pro",
             independence="DIFFERENT_MODEL_REQUIRED"),
        now_iso="2026-01-01T00:00:00+00:00",
    )
    assert d.is_terminal
    assert d.model is None
    assert d.decision_reason_code in ("NO_VALID_FALLBACK", "NO_ELIGIBLE_CANDIDATE")


def test_fallback_keeps_escalation_level_unchanged():
    # Fallback must never raise NOR lower the escalation level.
    snap = mr.AvailabilitySnapshot(model_states={"deepseek-v4-flash": "UNAVAILABLE"})
    d = _router().route(
        _req(Role.IMPLEMENTER.value, risk="LOW", snapshot=snap),
        now_iso="2026-01-01T00:00:00+00:00",
    )
    assert d.escalation_level == 0


def test_transient_provider_failure_is_not_a_fallback_trigger():
    # Provider/transient evidence (EXTERNAL) surfaces PROVIDER_FAILURE (a
    # non-capability code) but does NOT mark a model unavailable -> no fallback.
    ev = mr.RoutingEvidence(prior_attempts=(
        mr.AttemptEvidence(attempt_no=1, model_id="deepseek-v4-pro",
                           reasoning_level="MEDIUM", outcome_class="EXTERNAL",
                           status="FAILED"),
    ))
    d = _router().route(
        _req(Role.IMPLEMENTER.value, evidence=ev),
        now_iso="2026-01-01T00:00:00+00:00",
    )
    assert d.model == "deepseek-v4-pro"
    assert d.decision_reason_code == "PROVIDER_FAILURE"
    assert d.escalation_level == 1  # no escalation


# ---------------------------------------------------------------------------
# Determinism + provenance (CASE 16/17)
# ---------------------------------------------------------------------------

def test_case16_same_inputs_same_decision_and_provenance():
    snap = mr.AvailabilitySnapshot(model_states={"deepseek-v4-flash": "UNAVAILABLE"})
    d1 = _router().route(
        _req(Role.IMPLEMENTER.value, risk="LOW", snapshot=snap),
        now_iso="2026-01-01T00:00:00+00:00",
    )
    d2 = _router().route(
        _req(Role.IMPLEMENTER.value, risk="LOW", snapshot=snap),
        now_iso="2026-01-01T00:00:00+00:00",
    )
    assert d1.decision_id == d2.decision_id
    assert d1.sha256 == d2.sha256
    assert d1.inputs_hash == d2.inputs_hash
    assert d1.registry_version == "1"
    assert d1.evidence_version == "1"
    assert len(d1.inputs_hash) == 64


def test_case17_version_change_visible_in_provenance():
    # A REAL document-content change (not an unused snapshot key) changes the
    # persisted provenance: the content digest and the decision_id/sha256 must
    # differ even when the routing OUTCOME is unchanged.
    import copy
    import pathlib
    real = pathlib.Path(mr.__file__).resolve().parent / "registry" / "routing_policy_v1.json"
    doc1 = json.loads(real.read_text())
    doc2 = copy.deepcopy(doc1)
    # A cosmetic label change (does NOT alter the routing outcome).
    doc2["escalation"]["level_names"]["0"] = "ROUTINE-X"
    pol1 = mr.RoutingPolicy(doc1)
    pol2 = mr.RoutingPolicy(doc2)
    d1 = mr.ModelRouter(policy=pol1).route(
        _req(Role.IMPLEMENTER.value), now_iso="2026-01-01T00:00:00+00:00",
    )
    d2 = mr.ModelRouter(policy=pol2).route(
        _req(Role.IMPLEMENTER.value), now_iso="2026-01-01T00:00:00+00:00",
    )
    assert pol1.content_hash != pol2.content_hash
    assert d1.model == d2.model  # same outcome
    assert d1.policy_hash != d2.policy_hash
    assert d1.inputs_hash != d2.inputs_hash
    assert d1.decision_id != d2.decision_id
    assert d1.sha256 != d2.sha256


def test_decision_provenance_fields_present():
    d = _router().route(
        _req(Role.LEAD.value), now_iso="2026-01-01T00:00:00+00:00",
    )
    assert d.registry_version == "1"
    assert d.evidence_version == "1"
    assert isinstance(d.inputs_hash, str) and len(d.inputs_hash) == 64


# ---------------------------------------------------------------------------
# Malformed policy (CASE 10)
# ---------------------------------------------------------------------------

def _policy_doc():
    import pathlib
    real = pathlib.Path(mr.__file__).resolve().parent / "registry" / "routing_policy_v1.json"
    return json.loads(real.read_text())


def test_policy_rejects_bad_evidence_minimum_status():
    doc = _policy_doc()
    doc["evidence_requirements"]["minimum_status"] = "UNKNOWN"
    with pytest.raises(mr.RoutingError) as exc:
        mr.RoutingPolicy(doc)
    assert exc.value.code == mr.ROUTING_POLICY_INVALID


def test_policy_rejects_unknown_evidence_field():
    doc = _policy_doc()
    doc["evidence_requirements"]["bogus"] = True
    with pytest.raises(mr.RoutingError) as exc:
        mr.RoutingPolicy(doc)
    assert exc.value.code == mr.ROUTING_POLICY_INVALID


def test_policy_rejects_bad_fallback_trigger_state():
    doc = _policy_doc()
    doc["fallback"]["trigger_states"] = ["NOPE"]
    with pytest.raises(mr.RoutingError):
        mr.RoutingPolicy(doc)


def test_policy_fallback_absent_means_disabled():
    doc = _policy_doc()
    del doc["fallback"]
    pol = mr.RoutingPolicy(doc)
    assert pol.fallback_enabled is False


# ---------------------------------------------------------------------------
# CASE 13: capable but not policy-authorised
# ---------------------------------------------------------------------------

def test_case13_capable_but_not_policy_authorised_not_selected():
    # A model that satisfies the floor but is not in the profile allow-list is
    # never selected.  (The bootstrap registry is fixed; this is a smoke check
    # that the policy allow-list is the single authorisation source.)
    pol = mr.load_routing_policy()
    router = mr.ModelRouter()
    # lead allows only sol; pro (capable) is not allowed -> still sol.
    d = router.route(_req(Role.LEAD.value), now_iso="2026-01-01T00:00:00+00:00")
    assert d.model == "gpt-5.6-sol"
    assert list(pol.profile_for_role(Role.LEAD.value)["allowed_models"]) == ["gpt-5.6-sol"]
