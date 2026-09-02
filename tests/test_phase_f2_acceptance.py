"""Phase F2 — acceptance cases 1–30 + economy demonstration.

Deterministic, offline.  Every ``test_case_N_*`` maps to the corresponding
acceptance case in the F2 owner spec.  Uses only injected fakes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from argent_core import test_planning as tp
from argent_core import test_execution as te
from argent_core.test_execution import (
    ResultClass,
    StageState,
    Verdict,
)

from f2_helpers import (
    TEST_MAC_KEY,
    FakeGate,
    FakeRunner,
    exec_plan,
    fail_record,
    mk_plan,
    pass_record,
    real_plan,
    snap,
    stage,
    store,
)


# ---------------------------------------------------------------------------
# Stage execution semantics
# ---------------------------------------------------------------------------


def test_case1_low_risk_only_required_stages_run():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    rep = exec_plan(plan, FakeRunner(), snapshot=snap())
    assert rep.verdict == Verdict.DONE
    assert [s.name for s in rep.stages] == ["targeted"]
    assert rep.stages[0].state == StageState.PASSED


def test_case2_targeted_then_module_in_order():
    plan = real_plan("argent_core/visualizer_snapshot.py")
    names = [s.name for s in plan.stages]
    assert names[0] == "targeted" and "module" in names
    runner = FakeRunner()
    rep = exec_plan(plan, runner, snapshot=snap())
    assert rep.verdict == Verdict.DONE
    assert [s.name for s in rep.stages if s.state == StageState.PASSED] == names
    assert rep.stages[0].name == "targeted"
    assert runner.calls and runner.calls[0] in plan.stages[0].selectors


def test_case3_targeted_failure_stops_later_stages():
    plan = real_plan("argent_core/scheduler.py")
    first_sel = plan.stages[0].selectors[0]
    runner = FakeRunner({first_sel: ResultClass.TEST_FAILURE})
    rep = exec_plan(plan, runner, snapshot=snap())
    assert rep.verdict == Verdict.FAILED
    assert rep.stages[0].state == StageState.FAILED
    assert all(s.state == StageState.SKIPPED for s in rep.stages[1:])
    assert first_sel in runner.calls
    assert not any(c in plan.stages[1].selectors for c in runner.calls)


def test_case4_targeted_pass_but_full_suite_mandatory_still_runs():
    plan = real_plan("argent_core/scheduler.py")
    assert plan.full_suite_required
    runner = FakeRunner()
    rep = exec_plan(plan, runner, snapshot=snap())
    assert rep.verdict == Verdict.DONE
    assert rep.stages[-1].name == "full_suite"
    assert rep.stages[-1].state == StageState.PASSED
    assert "tests/" in runner.calls


def test_case12_early_failure_preserves_actionable_evidence():
    plan = real_plan("argent_core/scheduler.py")
    first_sel = plan.stages[0].selectors[0]
    runner = FakeRunner(
        {first_sel: te.RunnerOutcome(ResultClass.TEST_FAILURE, summary="assertion broke")}
    )
    rep = exec_plan(plan, runner, snapshot=snap())
    assert rep.first_failure_selector == first_sel
    assert rep.first_failure_stage == "targeted"
    assert rep.first_failure_class == ResultClass.TEST_FAILURE
    assert rep.stages_avoided >= 3


# ---------------------------------------------------------------------------
# Failure classification (never PASS)
# ---------------------------------------------------------------------------


def test_case13_resource_failure_not_pass_not_code_failure():
    plan = mk_plan([stage("targeted", ["tests/a.py"]), stage("full_suite", ["tests/"])])
    gate = FakeGate(allowed=False, reason="disk low")
    rep = exec_plan(plan, FakeRunner(), resource_gate=gate, snapshot=snap())
    assert rep.verdict == Verdict.BLOCKED
    assert rep.stages[0].state == StageState.BLOCKED
    assert rep.stages[0].selector_results[0].classification == ResultClass.RESOURCE_FAILURE
    assert rep.stages[1].state == StageState.SKIPPED
    assert rep.first_failure_class == ResultClass.RESOURCE_FAILURE


def test_case14_test_infra_failure_not_pass():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    runner = FakeRunner({"tests/a.py": ResultClass.TEST_INFRA_FAILURE})
    rep = exec_plan(plan, runner, snapshot=snap())
    assert rep.verdict == Verdict.BLOCKED
    assert rep.stages[0].state == StageState.BLOCKED
    assert rep.first_failure_class == ResultClass.TEST_INFRA_FAILURE


def test_case15_timeout_not_pass():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    rep = exec_plan(plan, FakeRunner({"tests/a.py": ResultClass.TIMEOUT}), snapshot=snap())
    assert rep.verdict == Verdict.BLOCKED
    assert rep.first_failure_class == ResultClass.TIMEOUT


def test_unknown_not_pass():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    rep = exec_plan(plan, FakeRunner({"tests/a.py": ResultClass.UNKNOWN}), snapshot=snap())
    assert rep.verdict == Verdict.BLOCKED
    assert rep.first_failure_class == ResultClass.UNKNOWN


def test_case29_resource_gate_independently_binding():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    gate = FakeGate(allowed=False, reason="capacity")
    rep = exec_plan(plan, FakeRunner(), resource_gate=gate, snapshot=snap())
    assert rep.verdict == Verdict.BLOCKED
    assert gate.calls == 1
    assert rep.first_failure_class == ResultClass.RESOURCE_FAILURE


def test_case30_test_failure_does_not_self_escalate_model():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    rep = exec_plan(
        plan, FakeRunner({"tests/a.py": ResultClass.TEST_FAILURE}), snapshot=snap()
    )
    assert rep.verdict == Verdict.FAILED
    assert not hasattr(rep, "model") and not hasattr(rep, "provider")


# ---------------------------------------------------------------------------
# Evidence reuse + invalidation
# ---------------------------------------------------------------------------


def test_case5_exact_identity_reuse():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    runner = FakeRunner()
    st = store()
    st.add(pass_record("tests/a.py", snap(), plan))
    rep = exec_plan(plan, runner, snapshot=snap(), store=st)
    assert rep.verdict == Verdict.DONE
    assert rep.stages[0].selector_results[0].reused is True
    assert runner.calls == []


def test_case6_source_change_invalidates():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    runner = FakeRunner()
    st = store()
    st.add(pass_record("tests/a.py", snap(source="old"), plan))
    rep = exec_plan(plan, runner, snapshot=snap(source="new"), store=st)
    assert rep.stages[0].selector_results[0].reused is False
    assert runner.calls == ["tests/a.py"]


def test_case7_test_definition_change_invalidates():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    runner = FakeRunner()
    st = store()
    st.add(pass_record("tests/a.py", snap(testdef="old"), plan))
    rep = exec_plan(plan, runner, snapshot=snap(testdef="new"), store=st)
    assert rep.stages[0].selector_results[0].reused is False


def test_case8_inventory_hash_change_invalidates():
    runner = FakeRunner()
    st = store()
    plan_old = mk_plan([stage("targeted", ["tests/a.py"])], inventory_hash="ih_old")
    st.add(pass_record("tests/a.py", snap(), plan_old))
    plan_new = mk_plan([stage("targeted", ["tests/a.py"])], inventory_hash="ih_new")
    rep = exec_plan(plan_new, runner, snapshot=snap(), store=st)
    assert rep.stages[0].selector_results[0].reused is False


def test_case9_policy_hash_change_invalidates():
    runner = FakeRunner()
    st = store()
    plan_old = mk_plan([stage("targeted", ["tests/a.py"])], policy_hash="poh_old")
    st.add(pass_record("tests/a.py", snap(), plan_old))
    plan_new = mk_plan([stage("targeted", ["tests/a.py"])], policy_hash="poh_new")
    rep = exec_plan(plan_new, runner, snapshot=snap(), store=st)
    assert rep.stages[0].selector_results[0].reused is False


def test_case10_unknown_provenance_reruns():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    runner = FakeRunner()
    st = store()
    rec = pass_record("tests/a.py", snap(), plan)
    bad = te.replace(rec, evidence_hash="deadbeef" * 8)
    st._records[bad.evidence_hash] = bad  # non-intact record injected directly
    rep = exec_plan(plan, runner, snapshot=snap(), store=st)
    assert rep.stages[0].selector_results[0].reused is False
    assert runner.calls == ["tests/a.py"]


def test_case11_previous_fail_never_reused():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    runner = FakeRunner()
    st = store()
    st.add(fail_record("tests/a.py", snap(), plan))
    rep = exec_plan(plan, runner, snapshot=snap(), store=st)
    assert rep.stages[0].selector_results[0].reused is False
    assert runner.calls == ["tests/a.py"]


def test_case22_same_inputs_same_stage_order_and_identities():
    plan = real_plan("argent_core/scheduler.py")
    r1, r2 = FakeRunner(), FakeRunner()
    s = snap()
    rep1 = exec_plan(plan, r1, snapshot=s)
    rep2 = exec_plan(plan, r2, snapshot=s)
    assert [st.name for st in rep1.stages] == [st.name for st in rep2.stages]
    assert r1.calls == r2.calls
    assert rep1.plan_hash == rep2.plan_hash == plan.plan_hash


def test_case23_duplicate_stage_avoided_only_with_exact_evidence(tmp_path):
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    st = store(str(tmp_path / "ev.json"))
    runner = FakeRunner()
    rep1 = exec_plan(plan, runner, snapshot=snap(), store=st)
    assert rep1.stages[0].selector_results[0].reused is False
    assert runner.calls == ["tests/a.py"]
    rep2 = exec_plan(plan, runner, snapshot=snap(), store=st)
    assert rep2.stages[0].selector_results[0].reused is True
    assert runner.calls == ["tests/a.py"]


def test_case25_fix_changing_snapshot_forces_recompute(tmp_path):
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    st = store(str(tmp_path / "ev.json"))
    runner = FakeRunner()
    exec_plan(plan, runner, snapshot=snap(source="v1"), store=st)
    assert runner.calls == ["tests/a.py"]
    exec_plan(plan, runner, snapshot=snap(source="v2"), store=st)
    assert runner.calls == ["tests/a.py", "tests/a.py"]


def test_case28_reuse_never_expands_permissions():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    st = store()
    st.add(pass_record("tests/a.py", snap(), plan))
    rep = exec_plan(plan, FakeRunner(), snapshot=snap(), store=st)
    assert rep.stages[0].selector_results[0].reused is True
    assert rep.stages[0].selector_results[0].classification == ResultClass.TEST_PASS


# ---------------------------------------------------------------------------
# High-risk / closing / terminal invariants
# ---------------------------------------------------------------------------


def test_case19_high_risk_still_runs_broad_regressions():
    plan = real_plan("argent_core/model_router.py")
    assert plan.full_suite_required
    rep = exec_plan(plan, FakeRunner(), snapshot=snap())
    assert rep.verdict == Verdict.DONE
    names = [s.name for s in rep.stages]
    assert "phase_regression" in names and "full_suite" in names


def test_case20_phase_closing_forces_full_suite():
    plan = tp.build_test_plan(
        tp.ChangeEvidence(("docs/PHASE_F2_NOTES.md",), phase_closing=True),
        tp.load_policy(),
        tp.load_inventory(),
        mac_key=TEST_MAC_KEY,
    )
    assert plan.full_suite_required
    runner = FakeRunner()
    rep = exec_plan(plan, runner, snapshot=snap())
    assert rep.verdict == Verdict.DONE
    assert rep.stages[-1].name == "full_suite"
    assert "tests/" in runner.calls


def test_case21_test_infra_change_forces_broad_closing():
    plan = real_plan("tests/conftest.py")
    assert plan.full_suite_required
    runner = FakeRunner()
    rep = exec_plan(plan, runner, snapshot=snap())
    assert rep.verdict == Verdict.DONE
    assert rep.stages[-1].name == "full_suite"
    assert "tests/" in runner.calls


def test_case26_done_requires_all_mandatory_stages_pass():
    plan = mk_plan(
        [
            stage("targeted", ["tests/a.py"], mandatory=["tests/a.py"]),
            stage("full_suite", ["tests/"], mandatory=["tests/"]),
        ],
        full_suite_required=True,
    )
    rep = exec_plan(plan, FakeRunner({"tests/a.py": ResultClass.TEST_FAILURE}), snapshot=snap())
    assert rep.verdict == Verdict.FAILED


def test_case27_terminal_semantics_done_failed_blocked():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    assert exec_plan(plan, FakeRunner(), snapshot=snap()).verdict == Verdict.DONE
    assert exec_plan(plan, FakeRunner({"tests/a.py": ResultClass.TEST_FAILURE}), snapshot=snap()).verdict == Verdict.FAILED
    assert exec_plan(plan, FakeRunner({"tests/a.py": ResultClass.UNKNOWN}), snapshot=snap()).verdict == Verdict.BLOCKED


def test_case17_agent_prose_cannot_inject_command():
    r = te.PytestRunner(runner_fn=lambda *a, **k: te.RunnerOutcome(ResultClass.TEST_PASS))
    with pytest.raises(ValueError):
        r.run("pytest tests/ -x --lf")


# ---------------------------------------------------------------------------
# Economy demonstration (CASE O)
# ---------------------------------------------------------------------------


def test_economy_demo_early_failure_avoids_later_stages_then_fix_reruns():
    plan = real_plan("argent_core/scheduler.py")
    first_sel = plan.stages[0].selectors[0]

    runner1 = FakeRunner({first_sel: ResultClass.TEST_FAILURE})
    rep1 = exec_plan(plan, runner1, snapshot=snap(source="v1"))
    assert rep1.verdict == Verdict.FAILED
    assert rep1.stages_avoided >= 3
    assert rep1.full_suite_avoided is True
    assert "tests/" not in runner1.calls

    runner2 = FakeRunner()
    rep2 = exec_plan(plan, runner2, snapshot=snap(source="v2"))
    assert rep2.verdict == Verdict.DONE
    assert "tests/" in runner2.calls
    assert rep2.stages_executed == len(plan.stages)
