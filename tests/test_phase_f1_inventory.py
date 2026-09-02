"""Phase F1 — deterministic test inventory + policy metadata (fail-closed).

Deterministic, offline.  Covers: versioned inventory loading, subsystem/module
resolution, test-infra/documentation classification, fail-closed rejection of
malformed metadata (duplicate keys, unknown subsystem/tag/risk, missing
fields), and the immutable read-only nature of both metadata files.

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
    RiskTag,
    Subsystem,
    TestPlanningError,
)


# ---------------------------------------------------------------------------
# Inventory: loading + resolution
# ---------------------------------------------------------------------------


def test_inventory_loads_version_and_has_hash():
    inv = tp.load_inventory()
    assert inv.version == "1"
    assert inv.content_hash and len(inv.content_hash) == 64


def test_inventory_resolves_module_to_subsystem():
    inv = tp.load_inventory()
    assert inv.subsystem_for_path("argent_core/scheduler.py") == "SUPERVISOR"
    assert inv.subsystem_for_path("argent_core/store.py") == "PERSISTENCE"
    assert inv.subsystem_for_path("argent_core/trust.py") == "SECURITY"
    assert inv.subsystem_for_path("argent_core/context_pack.py") == "CONTEXT"
    assert inv.subsystem_for_path("argent_core/model_router.py") == "MODEL_ROUTING"
    assert inv.subsystem_for_path("argent_core/resource_policy.py") == "RESOURCE"
    assert inv.subsystem_for_path("argent_core/visualizer_snapshot.py") == "CORE"


def test_inventory_resolves_by_basename():
    inv = tp.load_inventory()
    # Paths may be given without the "argent_core/" prefix.
    assert inv.subsystem_for_path("scheduler.py") == "SUPERVISOR"


def test_inventory_unknown_path_returns_none():
    inv = tp.load_inventory()
    assert inv.subsystem_for_path("mystery_dir/unknown_thing.py") is None


def test_inventory_documentation_and_test_infra_classification():
    inv = tp.load_inventory()
    assert inv.is_documentation_path("docs/README.md")
    assert inv.is_documentation_path("README.md")
    assert not inv.is_documentation_path("argent_core/store.py")
    assert inv.is_test_infra_path("tests/test_trust.py")
    assert inv.is_test_infra_path("tests/conftest.py")
    assert inv.is_test_infra_path("argent_core/test_planning.py")
    assert not inv.is_test_infra_path("argent_core/store.py")


def test_inventory_targeted_tests_are_known_selectors():
    inv = tp.load_inventory()
    assert "tests/test_phase_b2_scheduler_recovery.py" in inv.targeted_for("scheduler.py")
    assert "tests/test_trust.py" in inv.targeted_for("trust.py")


def test_inventory_is_immutable_read_only():
    inv = tp.load_inventory()
    with pytest.raises(TypeError):
        inv.module_ownership["argent_core/store.py"] = "SECURITY"  # type: ignore[index]


# ---------------------------------------------------------------------------
# Policy: loading
# ---------------------------------------------------------------------------


def test_policy_loads_version_and_has_hash():
    pol = tp.load_policy()
    assert pol.version == "1"
    assert pol.content_hash and len(pol.content_hash) == 64


def test_policy_risk_level_for_tag():
    pol = tp.load_policy()
    assert pol.risk_level_for_tag(RiskTag.SCHEMA_MIGRATION.value) == RiskLevel.CRITICAL
    assert pol.risk_level_for_tag(RiskTag.SECURITY_TRUST_BOUNDARY.value) == RiskLevel.HIGH
    assert pol.risk_level_for_tag(RiskTag.CONTEXT_INTEGRITY.value) == RiskLevel.MEDIUM


def test_policy_hard_invariants_present():
    pol = tp.load_policy()
    for sub in ("PERSISTENCE", "SECURITY", "SUPERVISOR", "RESOURCE", "MODEL_ROUTING", "CONTEXT"):
        assert sub in pol.hard_invariants
    assert pol.hard_invariants["PERSISTENCE"].full_suite is True
    assert pol.hard_invariants["RESOURCE"].full_suite is False


# ---------------------------------------------------------------------------
# Fail-closed metadata (F1 exit: malformed inventory/policy fails closed)
# ---------------------------------------------------------------------------


def _inventory_dict():
    return json.loads(
        """
        {
          "inventory_version": "1",
          "module_ownership": {"argent_core/store.py": "PERSISTENCE"},
          "subsystem_tests": {
            "PERSISTENCE": {"phase": null, "module_selectors": ["tests/test_persistence.py"], "phase_selectors": ["tests/test_persistence.py"]}
          },
          "targeted_tests": {"store.py": ["tests/test_persistence.py"]},
          "full_suite_selector": "tests/"
        }
        """
    )


def _policy_dict():
    return json.loads(
        """
        {
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
            "TEST_INFRASTRUCTURE": "HIGH"
          },
          "subsystem_risk_tags": {"PERSISTENCE": ["SCHEMA_MIGRATION"]},
          "module_tag_overrides": {},
          "hard_invariants": {
            "SECURITY": {"required_regression": ["tests/test_trust.py"], "full_suite": true},
            "SUPERVISOR": {"required_regression": ["tests/test_phase_b*.py"], "full_suite": true},
            "PERSISTENCE": {"required_regression": ["tests/test_persistence.py"], "full_suite": true}
          },
          "full_suite_required_when": ["phase_closing", "risk_CRITICAL", "risk_HIGH", "test_infrastructure_change", "multiple_subsystems"],
          "unknown_handling": {"policy": "BROADEN", "required_regression": ["tests/test_trust.py"], "full_suite": true},
          "test_infra_handling": {"policy": "BROAD_CLOSING", "required_regression": ["tests/test_trust.py"], "full_suite": true}
        }
        """
    )


def test_inventory_missing_version_fails_closed():
    d = _inventory_dict()
    del d["inventory_version"]
    with pytest.raises(InventoryError):
        tp.TestInventory.from_dict(d)


def test_inventory_unknown_subsystem_fails_closed():
    d = _inventory_dict()
    d["module_ownership"]["argent_core/foo.py"] = "NOT_A_SUBSYSTEM"
    with pytest.raises(InventoryError):
        tp.TestInventory.from_dict(d)


def test_inventory_unknown_subsystem_tests_fails_closed():
    d = _inventory_dict()
    d["subsystem_tests"]["NOPE"] = {"phase": None, "module_selectors": [], "phase_selectors": []}
    with pytest.raises(InventoryError):
        tp.TestInventory.from_dict(d)


def test_inventory_unsafe_selector_fails_closed():
    d = _inventory_dict()
    d["full_suite_selector"] = "/etc/passwd"
    with pytest.raises(InventoryError):
        tp.TestInventory.from_dict(d)


def test_policy_missing_version_fails_closed():
    d = _policy_dict()
    del d["policy_version"]
    with pytest.raises(PolicyError):
        tp.TestPolicy.from_dict(d)


def test_policy_unknown_risk_tag_fails_closed():
    d = _policy_dict()
    d["risk_raising_changes"]["NOT_A_TAG"] = "HIGH"
    with pytest.raises(PolicyError):
        tp.TestPolicy.from_dict(d)


def test_policy_unknown_risk_level_fails_closed():
    d = _policy_dict()
    d["risk_raising_changes"]["SCHEMA_MIGRATION"] = "SUPER_DANGER"
    with pytest.raises(PolicyError):
        tp.TestPolicy.from_dict(d)


def test_policy_unknown_full_suite_condition_fails_closed():
    d = _policy_dict()
    d["full_suite_required_when"] = ["risk_HIGH", "totally_made_up_condition"]
    with pytest.raises(PolicyError):
        tp.TestPolicy.from_dict(d)


def test_duplicate_json_key_fails_closed(tmp_path):
    p = tmp_path / "dup.json"
    p.write_text(
        '{"inventory_version": "1", "inventory_version": "2", "module_ownership": {}}',
        encoding="utf-8",
    )
    with pytest.raises(TestPlanningError):
        tp.load_inventory(str(p))
