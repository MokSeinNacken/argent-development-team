"""Phase F3 — economy value + self-protection (sections L, M).

Deterministic, offline.  Section L proves the *measurable* economy value of the
staged executor with three deterministic scenarios (broken intermediate
snapshot, fixed snapshot, identical snapshot) without inflated numbers.
Section M proves the test economy cannot weaken its own closing: any change to
the planner/executor/inventory/policy/test-helpers forces broad regression +
the full suite, and the executor cannot be reclassified out of TEST_INFRA.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from argent_core import test_execution as te
from argent_core import test_planning as tp
from argent_core.test_execution import ResultClass, StageState, Verdict

from f2_helpers import (
    FakeRunner,
    exec_plan,
    real_plan,
    snap,
    store,
)


# ---------------------------------------------------------------------------
# L — economy value acceptance (3 deterministic scenarios)
# ---------------------------------------------------------------------------


def test_l_economy_three_scenarios(tmp_path):
    plan = real_plan("argent_core/scheduler.py")
    first_sel = plan.stages[0].selectors[0]
    st = store(str(tmp_path / "ev.json"))

    # Scenario 1 — broken intermediate snapshot: targeted genuinely fails,
    # later stages are not executed (measurable avoided work).
    r1 = FakeRunner({first_sel: ResultClass.TEST_FAILURE})
    rep1 = exec_plan(plan, r1, snapshot=snap(source="v1"), store=st)
    assert rep1.verdict == Verdict.FAILED
    assert rep1.stages_executed == 0
    assert rep1.stages_avoided == len(plan.stages) - 1
    assert rep1.full_suite_avoided is True
    assert "tests/" not in r1.calls
    assert all(
        s.state == StageState.SKIPPED for s in rep1.stages[1:]
    )

    # Scenario 2 — fixed snapshot: the stale v1 failure does not satisfy v2,
    # early stages rerun, mandatory closing stages run after green, full suite
    # closes.
    r2 = FakeRunner()
    rep2 = exec_plan(plan, r2, snapshot=snap(source="v2"), store=st)
    assert rep2.verdict == Verdict.DONE
    assert rep2.stages_executed == len(plan.stages)
    assert "tests/" in r2.calls
    assert rep2.stages[-1].state == StageState.PASSED

    # Scenario 3 — identical snapshot: exact reuse, duplicates avoided, no
    # inflated counts.
    r3 = FakeRunner()
    rep3 = exec_plan(plan, r3, snapshot=snap(source="v2"), store=st)
    assert rep3.verdict == Verdict.DONE
    assert r3.calls == []
    assert rep3.stages_reused == len(plan.stages)
    assert rep3.total_tests == rep2.total_tests  # not double-counted


def test_l_no_bloated_metrics():
    plan = real_plan("argent_core/scheduler.py")
    rep = exec_plan(plan, FakeRunner(), snapshot=snap())
    n_selectors = sum(len(st.selectors) for st in plan.stages)
    assert rep.verdict == Verdict.DONE
    assert rep.stages_planned == len(plan.stages)
    assert rep.stages_executed == len(plan.stages)
    assert rep.stages_avoided == 0
    assert rep.stages_reused == 0
    assert rep.full_suite_avoided is False
    # total_tests equals the number of executed selectors (test_count=1 each).
    assert rep.total_tests == n_selectors


# ---------------------------------------------------------------------------
# M — test-economy self-protection (hard closing invariant)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "argent_core/test_planning.py",
        "argent_core/test_execution.py",
        "argent_core/registry/test_inventory_v1.json",
        "argent_core/registry/test_policy_v1.json",
        "tests/f2_helpers.py",
        "tests/conftest.py",
        "tests/test_phase_f2_execution.py",
    ],
)
def test_m_self_protection_forces_broad_closing_and_full_suite(path):
    plan = real_plan(path)
    assert plan.full_suite_required, path
    sels = plan.all_selectors()
    assert "tests/" in sels, path
    # Broad regression across B/C/D/E, not only the changed file's own tests.
    for prefix in ("tests/test_phase_b", "tests/test_phase_c", "tests/test_phase_d", "tests/test_phase_e"):
        assert any(s.startswith(prefix) for s in sels), (path, prefix)


def test_m_executor_is_test_infra_not_unknown():
    inv = tp.load_inventory()
    pol = tp.load_policy()
    imp = tp.derive_change_impact(
        tp.ChangeEvidence(changed_paths=("argent_core/test_execution.py",)), inv, pol
    )
    assert imp.test_infrastructure is True
    assert imp.unknown_paths == ()
    plan = tp.build_test_plan(
        tp.ChangeEvidence(changed_paths=("argent_core/test_execution.py",)), pol, inv
    )
    assert plan.full_suite_required is True


def test_m_executor_cannot_be_reclassified_away_from_test_infra():
    d = {
        "inventory_version": "1",
        "module_ownership": {
            "argent_core/test_execution.py": "CORE",
            "argent_core/store.py": "PERSISTENCE",
        },
        "subsystem_tests": {},
        "targeted_tests": {},
        "full_suite_selector": "tests/",
    }
    with pytest.raises(tp.InventoryError):
        tp.TestInventory.from_dict(d)


def test_m_policy_cannot_make_test_infra_safe():
    d = json.loads(Path("argent_core/registry/test_policy_v1.json").read_text(encoding="utf-8"))
    d = json.loads(json.dumps(d))
    d["test_infra_handling"] = {"policy": "NARROW", "required_regression": [], "full_suite": False}
    with pytest.raises(tp.PolicyError):
        tp.TestPolicy.from_dict(d)
