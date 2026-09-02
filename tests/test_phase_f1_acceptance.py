"""Phase F1 — deterministic test-plan acceptance (F1 spec K, cases 1-20).

Deterministic, offline.  Maps each mandated acceptance case to a concrete
``ChangeEvidence`` and asserts the resulting :class:`TestPlan`.  Every case is
named ``test_case_N_*`` so the mapping to the Owner-Spec is explicit.

No network, no shell, no provider calls.
"""

from __future__ import annotations

import pytest

from argent_core import test_planning as tp
from argent_core.test_planning import RiskLevel


@pytest.fixture(scope="module")
def inv():
    return tp.load_inventory()


@pytest.fixture(scope="module")
def pol():
    return tp.load_policy()


def _plan(paths, inv, pol, **kw):
    ev = tp.ChangeEvidence(changed_paths=tuple(paths), **kw)
    return tp.build_test_plan(ev, pol, inv)


def _selectors(plan):
    return set(plan.all_selectors())


def _has_phase(plan, prefix):
    return any(s.startswith(f"tests/test_phase_{prefix}") for s in plan.all_selectors())


# ---------------------------------------------------------------------------
# CASE 1 — documentation-only change -> small targeted plan
# ---------------------------------------------------------------------------


def test_case_1_documentation_only_small_targeted_plan(inv, pol):
    plan = _plan(["docs/README.md"], inv, pol)
    assert plan.risk_level == RiskLevel.LOW
    assert plan.documentation_only is True
    assert plan.full_suite_required is False
    # No B/C/D/E phase regression, no full suite.
    assert not _has_phase(plan, "b")
    assert not _has_phase(plan, "c")
    assert not _has_phase(plan, "d")
    assert not _has_phase(plan, "e")


# ---------------------------------------------------------------------------
# CASE 2 — single isolated low-risk module -> targeted + relevant module tests
# ---------------------------------------------------------------------------


def test_case_2_isolated_low_risk_module_targeted_plus_module(inv, pol):
    # visualizer_snapshot.py is CORE with no risk tags -> LOW.
    plan = _plan(["argent_core/visualizer_snapshot.py"], inv, pol)
    assert plan.risk_level == RiskLevel.LOW
    assert plan.full_suite_required is False
    stage_names = [s.name for s in plan.stages]
    assert "targeted" in stage_names
    assert "module" in stage_names
    assert "full_suite" not in stage_names
    # Directly targeted test present.
    assert "tests/test_phase3d_visualizer_snapshot.py" in _selectors(plan)


# ---------------------------------------------------------------------------
# CASE 3 — supervisor state-transition -> Phase-B regression required
# ---------------------------------------------------------------------------


def test_case_3_supervisor_state_transition_requires_phase_b(inv, pol):
    plan = _plan(["argent_core/supervisor.py"], inv, pol)
    assert plan.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
    assert _has_phase(plan, "b")


# ---------------------------------------------------------------------------
# CASE 4 — resource governor -> Phase-C regression required
# ---------------------------------------------------------------------------


def test_case_4_resource_governor_requires_phase_c(inv, pol):
    plan = _plan(["argent_core/resource_policy.py"], inv, pol)
    assert _has_phase(plan, "c")


# ---------------------------------------------------------------------------
# CASE 5 — context integrity/budget -> Phase-D regression required
# ---------------------------------------------------------------------------


def test_case_5_context_integrity_requires_phase_d(inv, pol):
    plan = _plan(["argent_core/context_pack.py"], inv, pol)
    assert _has_phase(plan, "d")


# ---------------------------------------------------------------------------
# CASE 6 — model routing/capability-floor -> Phase-E regression required
# ---------------------------------------------------------------------------


def test_case_6_model_routing_requires_phase_e(inv, pol):
    plan = _plan(["argent_core/model_router.py"], inv, pol)
    assert _has_phase(plan, "e")


# ---------------------------------------------------------------------------
# CASE 7 — security/trust-boundary -> broad security regression + full suite
# ---------------------------------------------------------------------------


def test_case_7_security_requires_broad_regression_and_full_suite(inv, pol):
    plan = _plan(["argent_core/trust.py"], inv, pol)
    assert plan.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
    assert plan.full_suite_required is True
    sels = _selectors(plan)
    assert "tests/test_trust.py" in sels
    assert "tests/test_roles.py" in sels
    assert "tests/test_security_hardening.py" in sels
    assert "tests/" in sels


# ---------------------------------------------------------------------------
# CASE 8 — schema/migration -> migration tests + phase + full suite
# ---------------------------------------------------------------------------


def test_case_8_schema_migration_requires_migration_plus_full_suite(inv, pol):
    plan = _plan(["argent_core/store.py"], inv, pol)
    assert plan.risk_level == RiskLevel.CRITICAL
    assert plan.full_suite_required is True
    sels = _selectors(plan)
    assert "tests/test_persistence.py" in sels
    assert "tests/test_phase_b4_migration.py" in sels
    assert "tests/" in sels


# ---------------------------------------------------------------------------
# CASE 9 — test infrastructure -> may not select only own targeted; full suite
# ---------------------------------------------------------------------------


def test_case_9_test_infra_requires_broad_closing_full_suite(inv, pol):
    plan = _plan(["tests/test_phase_f1_inventory.py"], inv, pol)
    assert plan.full_suite_required is True
    sels = _selectors(plan)
    assert "tests/" in sels
    # Broad B/C/D/E regression must be present, not only F1's own tests.
    assert _has_phase(plan, "b")
    assert _has_phase(plan, "c")
    assert _has_phase(plan, "d")
    assert _has_phase(plan, "e")


# ---------------------------------------------------------------------------
# CASE 10 — unknown changed path -> conservative broader plan
# ---------------------------------------------------------------------------


def test_case_10_unknown_path_conservative_broader_plan(inv, pol):
    plan = _plan(["mystery_dir/unknown_file.py"], inv, pol)
    assert plan.risk_level in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL)
    assert plan.full_suite_required is True
    # Conservative breadth: all four phase regressions present.
    assert _has_phase(plan, "b")
    assert _has_phase(plan, "c")
    assert _has_phase(plan, "d")
    assert _has_phase(plan, "e")


# ---------------------------------------------------------------------------
# CASE 11 — multiple subsystems -> union of regressions, deduplicated
# ---------------------------------------------------------------------------


def test_case_11_multiple_subsystems_union_deduplicated(inv, pol):
    plan = _plan(["argent_core/scheduler.py", "argent_core/resource_policy.py"], inv, pol)
    sels = plan.all_selectors()
    assert len(sels) == len(set(sels))  # no duplicates
    assert _has_phase(plan, "b")
    assert _has_phase(plan, "c")
    assert plan.full_suite_required is True


# ---------------------------------------------------------------------------
# CASE 12 — cheaper/faster plan conflicts with hard risk rule -> hard rule wins
# ---------------------------------------------------------------------------


def test_case_12_hard_rule_wins_over_cheaper_plan(inv, pol):
    # A persistence change cannot be reduced to only a tiny unit subset even
    # though a "cheap" plan would want just one migration test.
    plan = _plan(["argent_core/store.py"], inv, pol)
    sels = _selectors(plan)
    # The hard invariant regression is present despite any economy pressure.
    assert "tests/test_persistence.py" in sels
    assert "tests/test_phase_b4_migration.py" in sels
    assert _has_phase(plan, "b")
    assert "tests/" in sels  # full suite is not cost-ranked away


# ---------------------------------------------------------------------------
# CASE 13 — agent claims "low risk" but trusted evidence HIGH -> HIGH wins
# ---------------------------------------------------------------------------


def test_case_13_evidence_high_beats_agent_low_claim(inv, pol):
    # Trusted evidence (scheduler.py) is HIGH regardless of any prose.  The
    # planner has no field for an agent risk claim; risk is derived only from
    # evidence.
    plan = _plan(["argent_core/scheduler.py"], inv, pol)
    assert plan.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)


# ---------------------------------------------------------------------------
# CASE 14 — deterministic for identical inputs
# ---------------------------------------------------------------------------


def test_case_14_deterministic_identical_inputs(inv, pol):
    paths = ["argent_core/scheduler.py", "argent_core/context_pack.py"]
    p1 = _plan(paths, inv, pol)
    p2 = _plan(paths, inv, pol)
    assert p1.plan_hash == p2.plan_hash
    assert p1.all_selectors() == p2.all_selectors()


# ---------------------------------------------------------------------------
# CASE 15 — policy/inventory hash appears in plan provenance
# ---------------------------------------------------------------------------


def test_case_15_policy_and_inventory_hash_in_provenance(inv, pol):
    plan = _plan(["argent_core/store.py"], inv, pol)
    assert plan.policy_hash == pol.content_hash
    assert plan.inventory_hash == inv.content_hash
    assert plan.policy_version == pol.version
    assert plan.inventory_version == inv.version


# ---------------------------------------------------------------------------
# CASE 16 — malformed inventory/policy fails closed
# ---------------------------------------------------------------------------


def test_case_16_malformed_metadata_fails_closed(inv, pol):
    bad_inv = {
        "inventory_version": "1",
        "module_ownership": {"x": "NOT_REAL"},
        "subsystem_tests": {},
        "targeted_tests": {},
        "full_suite_selector": "tests/",
    }
    with pytest.raises(tp.InventoryError):
        tp.TestInventory.from_dict(bad_inv)


# ---------------------------------------------------------------------------
# CASE 17 — missing mapping does not silently omit security tests
# ---------------------------------------------------------------------------


def test_case_17_missing_mapping_keeps_security_regression(inv, pol):
    plan = _plan(["totally/unknown.py"], inv, pol)
    sels = _selectors(plan)
    assert "tests/test_trust.py" in sels
    assert "tests/test_security_hardening.py" in sels


# ---------------------------------------------------------------------------
# CASE 18 — terminal-state/security change cannot close with tiny unit subset
# ---------------------------------------------------------------------------


def test_case_18_terminal_state_requires_broad_not_tiny_subset(inv, pol):
    plan = _plan(["argent_core/state_machine.py"], inv, pol)
    assert plan.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
    assert plan.full_suite_required is True
    # Not just the single targeted state-machine test.
    assert len(plan.all_selectors()) > 1


# ---------------------------------------------------------------------------
# CASE 19 — docs-only diff does not trigger needless B-E regressions
# ---------------------------------------------------------------------------


def test_case_19_docs_only_no_needless_phase_regressions(inv, pol):
    plan = _plan(["docs/PHASE_F1_NOTES.md", "README.md"], inv, pol)
    assert not _has_phase(plan, "b")
    assert not _has_phase(plan, "c")
    assert not _has_phase(plan, "d")
    assert not _has_phase(plan, "e")
    assert plan.full_suite_required is False


# ---------------------------------------------------------------------------
# CASE 20 — phase-closing workflow still requires full suite
# ---------------------------------------------------------------------------


def test_case_20_phase_closing_requires_full_suite(inv, pol):
    # Even a LOW-risk module change requires the full suite at closing.
    plan = _plan(["argent_core/visualizer_snapshot.py"], inv, pol, phase_closing=True)
    assert plan.full_suite_required is True
    assert "tests/" in _selectors(plan)


# ---------------------------------------------------------------------------
# Adversarial cases (F1 spec K: add cases where implementation reveals risks)
# ---------------------------------------------------------------------------


def test_adversarial_empty_change_set_fails_closed(inv, pol):
    with pytest.raises(tp.TestPlanningError):
        _plan([], inv, pol)


def test_adversarial_planner_self_change_cannot_self_prove(inv, pol):
    # Changing the planner itself must force broad regression + full suite,
    # never only the planner's own F1 tests.
    plan = _plan(["argent_core/test_planning.py"], inv, pol)
    assert plan.full_suite_required is True
    assert "tests/" in _selectors(plan)
    assert _has_phase(plan, "b")


def test_adversarial_inventory_self_change_cannot_self_prove(inv, pol):
    plan = _plan(["argent_core/registry/test_inventory_v1.json"], inv, pol)
    assert plan.full_suite_required is True
    assert "tests/" in _selectors(plan)


def test_adversarial_schema_flag_raises_to_critical_even_without_store_path(inv, pol):
    # The controller can assert a schema migration independent of path mapping.
    plan = _plan(["argent_core/visualizer_snapshot.py"], inv, pol, schema_migration=True)
    assert plan.risk_level == RiskLevel.CRITICAL
    assert plan.full_suite_required is True


def test_adversarial_security_reviewed_patch_requires_full_suite(inv, pol):
    plan = _plan(["argent_core/visualizer_snapshot.py"], inv, pol, security_reviewed=True)
    assert plan.full_suite_required is True


def test_adversarial_risk_tags_never_lower_risk(inv, pol):
    # A SECURITY change carries SECURITY_TRUST_BOUNDARY (HIGH).  There is no
    # tag combination that can lower it back to LOW.
    plan = _plan(["argent_core/trust.py"], inv, pol)
    assert plan.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)


def test_adversarial_workspace_broker_execution_boundary_high(inv, pol):
    plan = _plan(["argent_core/workspace_broker.py"], inv, pol)
    assert plan.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
    assert plan.full_suite_required is True
