"""Phase F1 fix-round — Sol-review findings F1-F9 regression tests.

Deterministic, offline.  Each finding (F1-F9) from the independent Sol closing
review is reproduced with a named test ``test_fN_*``.  These are additive; they
do not weaken any B/C/D/E test.

No network, no shell, no provider calls.
"""

from __future__ import annotations

import json

import pytest

from argent_core import test_planning as tp
from argent_core.test_planning import (
    InventoryError,
    PolicyError,
    RiskLevel,
)


@pytest.fixture(scope="module")
def inv():
    return tp.load_inventory()


@pytest.fixture(scope="module")
def pol():
    return tp.load_policy()


def _plan(paths, inv, pol, **kw):
    return tp.build_test_plan(tp.ChangeEvidence(changed_paths=tuple(paths), **kw), pol, inv)


# ---------------------------------------------------------------------------
# F1 — policy/inventory semantic fail-closed floors (code-level)
# ---------------------------------------------------------------------------


def _weak_policy():
    """A policy that tries to remove every safety net."""
    return json.loads(
        """
        {
          "policy_version": "1",
          "risk_raising_changes": {"SCHEMA_MIGRATION": "CRITICAL"},
          "subsystem_risk_tags": {"PERSISTENCE": ["SCHEMA_MIGRATION"]},
          "module_tag_overrides": {},
          "hard_invariants": {
            "PERSISTENCE": {"required_regression": ["tests/test_persistence.py"], "full_suite": true}
          },
          "full_suite_required_when": ["phase_closing"],
          "unknown_handling": {"policy": "BROADEN", "required_regression": [], "full_suite": false},
          "test_infra_handling": {"policy": "BROAD_CLOSING", "required_regression": [], "full_suite": false}
        }
        """
    )


def test_f1a_policy_missing_mandatory_risk_tag_fails_closed():
    d = _weak_policy()  # missing LEASE_FENCING_SCHEDULER etc.
    with pytest.raises(PolicyError):
        tp.TestPolicy.from_dict(d)


def test_f1b_policy_weakened_risk_tag_below_floor_fails_closed():
    d = _weak_policy()
    # Add all mandatory tags but weaken SCHEMA_MIGRATION below CRITICAL floor.
    d["risk_raising_changes"] = {
        "SCHEMA_MIGRATION": "LOW",
        "LEASE_FENCING_SCHEDULER": "HIGH",
        "SECURITY_TRUST_BOUNDARY": "HIGH",
        "WRITE_BROKER_EXECUTION_BOUNDARY": "HIGH",
        "CRASH_RECOVERY": "HIGH",
        "PROCESS_OWNERSHIP": "HIGH",
        "RESOURCE_ENFORCEMENT": "HIGH",
        "CONTEXT_INTEGRITY": "MEDIUM",
        "MODEL_ROUTING_INDEPENDENCE": "HIGH",
        "TERMINAL_STATE_TRANSITION": "HIGH",
        "TEST_INFRASTRUCTURE": "HIGH",
    }
    with pytest.raises(PolicyError):
        tp.TestPolicy.from_dict(d)


def test_f1c_policy_missing_core_hard_invariant_fails_closed():
    d = _weak_policy()
    # Fix risk tags, but keep hard_invariants missing SECURITY/SUPERVISOR.
    d["risk_raising_changes"] = {
        "SCHEMA_MIGRATION": "CRITICAL",
        "LEASE_FENCING_SCHEDULER": "HIGH",
        "SECURITY_TRUST_BOUNDARY": "HIGH",
        "WRITE_BROKER_EXECUTION_BOUNDARY": "HIGH",
        "CRASH_RECOVERY": "HIGH",
        "PROCESS_OWNERSHIP": "HIGH",
        "RESOURCE_ENFORCEMENT": "HIGH",
        "CONTEXT_INTEGRITY": "MEDIUM",
        "MODEL_ROUTING_INDEPENDENCE": "HIGH",
        "TERMINAL_STATE_TRANSITION": "HIGH",
        "TEST_INFRASTRUCTURE": "HIGH",
    }
    with pytest.raises(PolicyError):
        tp.TestPolicy.from_dict(d)


def test_f1d_policy_unknown_full_suite_disabled_fails_closed():
    d = _weak_policy()
    d["risk_raising_changes"] = {
        "SCHEMA_MIGRATION": "CRITICAL",
        "LEASE_FENCING_SCHEDULER": "HIGH",
        "SECURITY_TRUST_BOUNDARY": "HIGH",
        "WRITE_BROKER_EXECUTION_BOUNDARY": "HIGH",
        "CRASH_RECOVERY": "HIGH",
        "PROCESS_OWNERSHIP": "HIGH",
        "RESOURCE_ENFORCEMENT": "HIGH",
        "CONTEXT_INTEGRITY": "MEDIUM",
        "MODEL_ROUTING_INDEPENDENCE": "HIGH",
        "TERMINAL_STATE_TRANSITION": "HIGH",
        "TEST_INFRASTRUCTURE": "HIGH",
    }
    d["hard_invariants"] = {
        "SECURITY": {"required_regression": ["tests/test_trust.py"], "full_suite": True},
        "SUPERVISOR": {"required_regression": ["tests/test_phase_b*.py"], "full_suite": True},
        "PERSISTENCE": {"required_regression": ["tests/test_persistence.py"], "full_suite": True},
    }
    # unknown_handling.full_suite=False is still forbidden by the floor.
    with pytest.raises(PolicyError):
        tp.TestPolicy.from_dict(d)


def test_f1e_policy_missing_mandatory_full_suite_condition_fails_closed():
    d = _weak_policy()
    d["risk_raising_changes"] = {
        "SCHEMA_MIGRATION": "CRITICAL",
        "LEASE_FENCING_SCHEDULER": "HIGH",
        "SECURITY_TRUST_BOUNDARY": "HIGH",
        "WRITE_BROKER_EXECUTION_BOUNDARY": "HIGH",
        "CRASH_RECOVERY": "HIGH",
        "PROCESS_OWNERSHIP": "HIGH",
        "RESOURCE_ENFORCEMENT": "HIGH",
        "CONTEXT_INTEGRITY": "MEDIUM",
        "MODEL_ROUTING_INDEPENDENCE": "HIGH",
        "TERMINAL_STATE_TRANSITION": "HIGH",
        "TEST_INFRASTRUCTURE": "HIGH",
    }
    d["hard_invariants"] = {
        "SECURITY": {"required_regression": ["tests/test_trust.py"], "full_suite": True},
        "SUPERVISOR": {"required_regression": ["tests/test_phase_b*.py"], "full_suite": True},
        "PERSISTENCE": {"required_regression": ["tests/test_persistence.py"], "full_suite": True},
    }
    d["unknown_handling"] = {"policy": "BROADEN", "required_regression": ["tests/test_trust.py"], "full_suite": True}
    d["test_infra_handling"] = {"policy": "BROAD_CLOSING", "required_regression": ["tests/test_trust.py"], "full_suite": True}
    # full_suite_required_when only has phase_closing (missing mandatory set).
    with pytest.raises(PolicyError):
        tp.TestPolicy.from_dict(d)


def test_f1f_inventory_self_reclass_to_core_fails_closed():
    # Inventory tries to reclassify its own policy registry file to CORE.
    d = {
        "inventory_version": "1",
        "module_ownership": {
            "argent_core/registry/test_policy_v1.json": "CORE",
            "argent_core/store.py": "PERSISTENCE",
        },
        "subsystem_tests": {},
        "targeted_tests": {},
        "full_suite_selector": "tests/",
    }
    with pytest.raises(InventoryError):
        tp.TestInventory.from_dict(d)


def test_f1g_inventory_planner_module_self_reclass_fails_closed():
    d = {
        "inventory_version": "1",
        "module_ownership": {
            "argent_core/test_planning.py": "CORE",
            "argent_core/store.py": "PERSISTENCE",
        },
        "subsystem_tests": {},
        "targeted_tests": {},
        "full_suite_selector": "tests/",
    }
    with pytest.raises(InventoryError):
        tp.TestInventory.from_dict(d)


# ---------------------------------------------------------------------------
# F2 — all selector fields validated (root + zero-match glob rejection)
# ---------------------------------------------------------------------------


def test_f2a_targeted_selector_outside_root_fails_closed():
    d = {
        "inventory_version": "1",
        "module_ownership": {"argent_core/store.py": "PERSISTENCE"},
        "subsystem_tests": {
            "PERSISTENCE": {"phase": None, "module_selectors": [], "phase_selectors": []}
        },
        "targeted_tests": {"store.py": ["/tmp/outside.py"]},
        "full_suite_selector": "tests/",
    }
    with pytest.raises(InventoryError):
        tp.TestInventory.from_dict(d)


def test_f2b_zero_match_glob_fails_closed():
    d = {
        "inventory_version": "1",
        "module_ownership": {"argent_core/store.py": "PERSISTENCE"},
        "subsystem_tests": {
            "PERSISTENCE": {
                "phase": None,
                "module_selectors": ["tests/DOES_NOT_EXIST_*.py"],
                "phase_selectors": [],
            }
        },
        "targeted_tests": {},
        "full_suite_selector": "tests/",
    }
    with pytest.raises(InventoryError):
        tp.TestInventory.from_dict(d)


def test_f2c_policy_required_regression_zero_match_fails_closed():
    d = {
        "policy_version": "1",
        "risk_raising_changes": {
            "SCHEMA_MIGRATION": "CRITICAL",
            "LEASE_FENCING_SCHEDULER": "HIGH",
            "SECURITY_TRUST_BOUNDARY": "HIGH",
            "WRITE_BROKER_EXECUTION_BOUNDARY": "HIGH",
            "CRASH_RECOVERY": "HIGH",
            "PROCESS_OWNERSHIP": "HIGH",
            "RESOURCE_ENFORCEMENT": "HIGH",
            "CONTEXT_INTEGRITY": "MEDIUM",
            "MODEL_ROUTING_INDEPENDENCE": "HIGH",
            "TERMINAL_STATE_TRANSITION": "HIGH",
            "TEST_INFRASTRUCTURE": "HIGH",
        },
        "subsystem_risk_tags": {"PERSISTENCE": ["SCHEMA_MIGRATION"]},
        "module_tag_overrides": {},
        "hard_invariants": {
            "SECURITY": {"required_regression": ["tests/test_trust.py"], "full_suite": True},
            "SUPERVISOR": {"required_regression": ["tests/test_phase_b*.py"], "full_suite": True},
            "PERSISTENCE": {"required_regression": ["tests/NOPE_*.py"], "full_suite": True},
        },
        "full_suite_required_when": ["phase_closing", "risk_CRITICAL", "risk_HIGH", "test_infrastructure_change", "multiple_subsystems"],
        "unknown_handling": {"policy": "BROADEN", "required_regression": ["tests/test_trust.py"], "full_suite": True},
        "test_infra_handling": {"policy": "BROAD_CLOSING", "required_regression": ["tests/test_trust.py"], "full_suite": True},
    }
    with pytest.raises(PolicyError):
        tp.TestPolicy.from_dict(d)


def test_f2d_valid_e2e_fixture_selector_accepted():
    # e2e-fixture/tests/ root is allowed and resolves to real files.
    d = {
        "inventory_version": "1",
        "module_ownership": {"e2e-fixture/service.py": "TEST_INFRA"},
        "subsystem_tests": {
            "TEST_INFRA": {
                "phase": None,
                "module_selectors": ["e2e-fixture/tests/test_*.py"],
                "phase_selectors": [],
            }
        },
        "targeted_tests": {},
        "full_suite_selector": "tests/",
    }
    inv2 = tp.TestInventory.from_dict(d)
    assert "e2e-fixture/tests/test_*.py" in inv2.module_selectors("TEST_INFRA")


# ---------------------------------------------------------------------------
# F3 — documentation vs test-infra ordering; .md only under docs/ or root
# ---------------------------------------------------------------------------


def test_f3a_markdown_under_tests_is_test_infra_not_docs(inv, pol):
    plan = _plan(["tests/README.md"], inv, pol)
    assert plan.documentation_only is False
    assert plan.risk_level == RiskLevel.HIGH  # TEST_INFRA -> HIGH
    assert plan.full_suite_required is True


def test_f3b_markdown_under_argent_core_is_not_docs_only(inv, pol):
    # argent_core/SECURITY.md: neither docs-only nor test-infra -> UNKNOWN ->
    # conservative (broad + full suite), never a LOW docs-only plan.
    plan = _plan(["argent_core/SECURITY.md"], inv, pol)
    assert plan.documentation_only is False
    assert plan.risk_level in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL)
    assert plan.full_suite_required is True


def test_f3c_real_documentation_stays_low(inv, pol):
    plan = _plan(["docs/PHASE_F1_NOTES.md"], inv, pol)
    assert plan.documentation_only is True
    assert plan.risk_level == RiskLevel.LOW
    assert plan.full_suite_required is False


# ---------------------------------------------------------------------------
# F4 — worktree.py is SUPERVISOR (Phase-B writer/lease/fencing boundary)
# ---------------------------------------------------------------------------


def test_f4_worktree_requires_phase_b_and_full_suite(inv, pol):
    assert inv.subsystem_for_path("argent_core/worktree.py") == "SUPERVISOR"
    plan = _plan(["argent_core/worktree.py"], inv, pol)
    assert plan.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
    assert plan.full_suite_required is True
    assert any(s.startswith("tests/test_phase_b") for s in plan.all_selectors())


# ---------------------------------------------------------------------------
# F5 — e2e-fixture owned; smoke documented as manual
# ---------------------------------------------------------------------------


def test_f5a_e2e_fixture_change_includes_fixture_suite_selector(inv, pol):
    assert inv.subsystem_for_path("e2e-fixture/service.py") == "TEST_INFRA"
    plan = _plan(["e2e-fixture/service.py"], inv, pol)
    sels = plan.all_selectors()
    assert any(s.startswith("e2e-fixture/tests/") for s in sels)


def test_f5b_smoke_is_documented_manual_not_auto_authoritative(inv, pol):
    # smoke/ has no automatic selectors; a change there stays conservative via
    # UNKNOWN (broad + full suite), never a reduced plan.
    assert "smoke/" in inv.manual_suites
    plan = _plan(["smoke/phase2b_e2e.py"], inv, pol)
    assert plan.full_suite_required is True


# ---------------------------------------------------------------------------
# F6 — deep immutability (every level must raise TypeError on mutation)
# ---------------------------------------------------------------------------


def test_f6_deep_immutability_every_level(inv, pol):
    # MappingProxyType raises TypeError; frozen dataclasses raise
    # FrozenInstanceError (AttributeError); tuples have no mutator.  All
    # indicate rejection of mutation.
    Immutable = (TypeError, AttributeError)
    with pytest.raises(Immutable):
        inv.module_ownership["argent_core/store.py"] = "SECURITY"  # type: ignore[index]
    with pytest.raises(Immutable):
        inv.subsystem_tests["CONTEXT"] = None  # type: ignore[assignment]
    with pytest.raises(Immutable):
        inv.subsystem_tests["CONTEXT"].module_selectors = ()  # type: ignore[misc]
    with pytest.raises(Immutable):
        inv.targeted_tests["store.py"] = ()  # type: ignore[index]
    with pytest.raises(Immutable):
        pol.hard_invariants["PERSISTENCE"] = None  # type: ignore[assignment]
    with pytest.raises(Immutable):
        pol.hard_invariants["PERSISTENCE"].required_regression = ()  # type: ignore[misc]
    with pytest.raises(Immutable):
        pol.unknown_handling.full_suite = False  # type: ignore[misc]
    with pytest.raises(Immutable):
        pol.subsystem_risk_tags["PERSISTENCE"] = ()  # type: ignore[index]
    with pytest.raises(Immutable):
        pol.module_tag_overrides["store.py"] = ()  # type: ignore[index]


def test_f6_inner_record_values_are_immutable(inv, pol):
    # A live in-place mutation of inner records must fail; the plan is stable.
    with pytest.raises((TypeError, AttributeError)):
        inv.subsystem_tests["CONTEXT"].phase_selectors.append("tests/evil.py")  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# F7 — checkpoint.py is lease-fenced/transactional -> Phase-B + full suite
# ---------------------------------------------------------------------------


def test_f7_checkpoint_requires_phase_b_and_full_suite(inv, pol):
    plan = _plan(["argent_core/checkpoint.py"], inv, pol)
    assert plan.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
    assert plan.full_suite_required is True
    assert any(s.startswith("tests/test_phase_b") for s in plan.all_selectors())


# ---------------------------------------------------------------------------
# F8 — mandatory reasons are never lost to first-wins dedup
# ---------------------------------------------------------------------------


def test_f8_mandatory_marking_survives_dedup(inv, pol):
    plan = _plan(["argent_core/trust.py"], inv, pol)
    mandatory = plan.mandatory_selectors()
    # Every security hard-invariant selector is explicitly mandatory.
    for sel in ("tests/test_trust.py", "tests/test_roles.py", "tests/test_security_hardening.py",
                "tests/test_sandbox_runner.py", "tests/test_workspace_broker.py"):
        assert sel in mandatory
    # test_trust.py is also the targeted selector; it must STILL carry a
    # hard-invariant reason even though it was first placed in the targeted
    # stage.
    stage = next(s for s in plan.stages if s.name == "targeted")
    reasons = stage.reasons.get("tests/test_trust.py", ())
    assert any("HARD INVARIANT" in r for r in reasons)


def test_f8_reasons_are_sorted_and_deterministic(inv, pol):
    p1 = _plan(["argent_core/trust.py"], inv, pol)
    p2 = _plan(["argent_core/trust.py"], inv, pol)
    assert p1.plan_hash == p2.plan_hash


# ---------------------------------------------------------------------------
# F9 — ambiguous basenames rejected; deterministic under reordering
# ---------------------------------------------------------------------------


def test_f9_ambiguous_basename_fails_closed():
    d = {
        "inventory_version": "1",
        "module_ownership": {
            "x/dup.py": "CORE",
            "y/dup.py": "SECURITY",
        },
        "subsystem_tests": {},
        "targeted_tests": {},
        "full_suite_selector": "tests/",
    }
    with pytest.raises(InventoryError):
        tp.TestInventory.from_dict(d)


def test_f9_identical_canonical_content_identical_behavior(inv):
    # Reordering the JSON object keys must not change the content hash or the
    # plan (canonical serialisation sorts keys).
    d = json.loads(
        __import__("pathlib").Path(
            "argent_core/registry/test_inventory_v1.json"
        ).read_text(encoding="utf-8")
    )
    reordered = {
        "inventory_version": d["inventory_version"],
        "full_suite_selector": d["full_suite_selector"],
        "targeted_tests": d["targeted_tests"],
        "subsystem_tests": d["subsystem_tests"],
        "module_ownership": d["module_ownership"],
        "manual_suites": d["manual_suites"],
    }
    inv2 = tp.TestInventory.from_dict(reordered)
    assert inv2.content_hash == inv.content_hash


def test_f9_case14_reinforced_plan_hash_identical_from_file_vs_reordered(inv, pol):
    import pathlib

    d = json.loads(pathlib.Path("argent_core/registry/test_inventory_v1.json").read_text(encoding="utf-8"))
    reordered = {
        "inventory_version": d["inventory_version"],
        "full_suite_selector": d["full_suite_selector"],
        "targeted_tests": d["targeted_tests"],
        "subsystem_tests": d["subsystem_tests"],
        "module_ownership": d["module_ownership"],
        "manual_suites": d["manual_suites"],
    }
    inv2 = tp.TestInventory.from_dict(reordered)
    ev = tp.ChangeEvidence(changed_paths=("argent_core/store.py",))
    p1 = tp.build_test_plan(ev, pol, inv)
    p2 = tp.build_test_plan(ev, pol, inv2)
    assert p1.plan_hash == p2.plan_hash
