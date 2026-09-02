"""Phase F3 — adversarial acceptance cases 1–40 (integrated F1→F2 system).

Deterministic, offline.  Each ``test_case_N_*`` maps to the corresponding
acceptance case in the F3 owner spec (sections A–S).  The tests exercise the
*integrated* F1 planner + F2 executor + evidence store and prove the adversarial
fail-closed invariants; they reuse the F2 offline fakes via ``f2_helpers``.

Section mapping (see docs/PHASE_F3_ACCEPTANCE.md):
  A  -> cases 1, 34                    B  -> cases 1, 2, 3, 4, 5
  C  -> cases 6, 7, 8, 9, 10, 38       D  -> (trust boundary: separate file)
  E  -> cases 12, 13, 39               F  -> cases 14, 15, 16, 40
  G  -> cases 17, 18, 19, 20           H  -> cases 21, 22, 23, 24
  I  -> cases 34, 35                   J  -> cases 25, 26, 27
  K  -> cases 31, 32, 33               L  -> (economy: separate file)
  M  -> case 36, 37                    N  -> (this file)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from argent_core import test_execution as te
from argent_core import test_planning as tp
from argent_core.test_execution import (
    EvidenceRecord,
    EvidenceStore,
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
# A — F1 is the sole WHAT authority; F2 never invents/weakens the plan
# ---------------------------------------------------------------------------


def test_case1_f2_cannot_remove_f1_mandatory_stage():
    # trust.py -> SECURITY -> HIGH -> phase_regression + full_suite mandatory.
    plan = real_plan("argent_core/trust.py")
    assert plan.full_suite_required
    assert "full_suite" in [s.name for s in plan.stages]
    # Dropping a mandatory stage while keeping the authentic plan_hash is
    # detected as tampering (plan_hash no longer matches content).
    without_full = te.replace(
        plan, stages=tuple(s for s in plan.stages if s.name != "full_suite")
    )
    with pytest.raises(ValueError):
        te.execute_plan(without_full, FakeRunner(), snapshot=snap(), resource_gate=FakeGate(), mac_key=TEST_MAC_KEY)


# ---------------------------------------------------------------------------
# B — adversarial plan tampering -> fail-closed
# ---------------------------------------------------------------------------


def test_case2_forged_plan_hash_rejected():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    forged = te.replace(plan, plan_hash="0" * 64)
    with pytest.raises(ValueError):
        te.execute_plan(forged, FakeRunner(), snapshot=snap(), resource_gate=FakeGate(), mac_key=TEST_MAC_KEY)


def test_case3_policy_mismatch_rejected():
    # A plan minted against a *divergent* policy produces a different
    # plan_hash, so evidence bound to the other policy can never satisfy it.
    inv = tp.load_inventory()
    p1 = tp.load_policy()
    d = json.loads(Path("argent_core/registry/test_policy_v1.json").read_text(encoding="utf-8"))
    d["module_tag_overrides"] = dict(d["module_tag_overrides"])
    d["module_tag_overrides"]["visualizer_snapshot.py"] = ["SECURITY_TRUST_BOUNDARY"]
    p2 = tp.TestPolicy.from_dict(d)

    ev = tp.ChangeEvidence(changed_paths=("argent_core/visualizer_snapshot.py",))
    plan1 = tp.build_test_plan(ev, p1, inv, mac_key=TEST_MAC_KEY)
    plan2 = tp.build_test_plan(ev, p2, inv, mac_key=TEST_MAC_KEY)
    assert plan1.policy_hash != plan2.policy_hash
    assert plan1.plan_hash != plan2.plan_hash

    # A PASS minted under plan2 must not be reusable under plan1.
    sel = plan1.stages[0].selectors[0]
    st = store()
    st.add(pass_record(sel, snap(), plan2))
    runner = FakeRunner()
    rep = exec_plan(plan1, runner, snapshot=snap(), store=st)
    assert rep.stages[0].selector_results[0].reused is False


def test_case4_inventory_mismatch_rejected():
    # Two plans built against divergent inventories differ; evidence is not
    # portable across them.
    inv1 = tp.load_inventory()
    d = json.loads(Path("argent_core/registry/test_inventory_v1.json").read_text(encoding="utf-8"))
    d = dict(d)
    d["module_ownership"] = dict(d["module_ownership"])
    d["module_ownership"]["argent_core/visualizer_snapshot.py"] = "SECURITY"
    inv2 = tp.TestInventory.from_dict(d)
    assert inv1.content_hash != inv2.content_hash

    pol = tp.load_policy()
    ev = tp.ChangeEvidence(changed_paths=("argent_core/visualizer_snapshot.py",))
    plan1 = tp.build_test_plan(ev, pol, inv1, mac_key=TEST_MAC_KEY)
    plan2 = tp.build_test_plan(ev, pol, inv2, mac_key=TEST_MAC_KEY)
    assert plan1.inventory_hash != plan2.inventory_hash

    sel = plan1.stages[0].selectors[0]
    st = store()
    st.add(pass_record(sel, snap(), plan2))
    rep = exec_plan(plan1, FakeRunner(), snapshot=snap(), store=st)
    assert rep.stages[0].selector_results[0].reused is False


def test_case5_snapshot_mismatch_rejected():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    st = store()
    st.add(pass_record("tests/a.py", snap(source="s_old"), plan))
    rep = exec_plan(plan, FakeRunner(), snapshot=snap(source="s_new"), store=st)
    assert rep.stages[0].selector_results[0].reused is False
    # Also: test-definition change invalidates.
    st2 = store()
    st2.add(pass_record("tests/a.py", snap(testdef="t_old"), plan))
    rep2 = exec_plan(plan, FakeRunner(), snapshot=snap(testdef="t_new"), store=st2)
    assert rep2.stages[0].selector_results[0].reused is False


# ---------------------------------------------------------------------------
# C — adversarial evidence tampering -> never PASS proof
# ---------------------------------------------------------------------------


def _signed_pass(sel="tests/a.py"):
    plan = mk_plan([stage("targeted", [sel])])
    st = store()
    st.add(pass_record(sel, snap(), plan))
    return st.records()[0]


def test_case6_tampered_pass_rejected():
    signed = _signed_pass()
    tampered = te.replace(signed, selector="tests/OTHER.py")  # keep old MAC
    with pytest.raises(ValueError):
        store().add(tampered)


def test_case7_fail_to_forged_pass_rejected():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    st = store()
    st.add(fail_record("tests/a.py", snap(), plan))
    fail_signed = st.records()[0]
    forged = te.replace(fail_signed, classification=ResultClass.TEST_PASS)  # keep FAIL MAC
    with pytest.raises(ValueError):
        store().add(forged)


def test_case8_missing_mac_rejected(tmp_path):
    # A persisted record without a MAC is rejected on load (fail-closed).
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    st = store(str(tmp_path / "ev.json"))
    st.add(pass_record("tests/a.py", snap(), plan))
    data = json.loads(Path(tmp_path / "ev.json").read_text(encoding="utf-8"))
    data["records"][0]["evidence_hash"] = ""
    Path(tmp_path / "ev.json").write_text(json.dumps(data))
    with pytest.raises(ValueError):
        EvidenceStore(path=str(tmp_path / "ev.json"), mac_key=TEST_MAC_KEY)


def test_case9_invalid_mac_rejected():
    signed = _signed_pass()
    bad = te.replace(signed, evidence_hash="deadbeef" * 8)
    with pytest.raises(ValueError):
        store().add(bad)


def test_case10_partial_evidence_rejected(tmp_path):
    # A persisted record missing a required identity field fails closed.
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    st = store(str(tmp_path / "ev.json"))
    st.add(pass_record("tests/a.py", snap(), plan))
    data = json.loads(Path(tmp_path / "ev.json").read_text(encoding="utf-8"))
    del data["records"][0]["selector"]
    Path(tmp_path / "ev.json").write_text(json.dumps(data))
    with pytest.raises(ValueError):
        EvidenceStore(path=str(tmp_path / "ev.json"), mac_key=TEST_MAC_KEY)


def test_case11_unknown_identity_reruns():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    runner = FakeRunner()
    st = store()
    # UNKNOWN classification -> never reused.
    st.add(fail_record("tests/a.py", snap(), plan, cls=ResultClass.UNKNOWN))
    rep = exec_plan(plan, runner, snapshot=snap(), store=st)
    assert rep.stages[0].selector_results[0].reused is False
    assert runner.calls == ["tests/a.py"]
    # Foreign executor_id -> identity mismatch -> rerun.
    st2 = store()
    foreign = te.replace(
        pass_record("tests/a.py", snap(), plan), executor_id="foreign-executor"
    )
    st2.add(foreign)
    runner2 = FakeRunner()
    rep2 = exec_plan(plan, runner2, snapshot=snap(), store=st2)
    assert rep2.stages[0].selector_results[0].reused is False
    assert runner2.calls == ["tests/a.py"]


# ---------------------------------------------------------------------------
# E — restart / crash adversarial
# ---------------------------------------------------------------------------


def test_case12_running_after_crash_never_pass():
    # There is no RUNNING ResultClass; interrupted work is only ever UNKNOWN or
    # terminal.  An UNKNOWN record never becomes a reusable PASS.
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    st = store()
    unk = fail_record("tests/a.py", snap(), plan, cls=ResultClass.UNKNOWN)
    st.add(unk)
    assert st.find_reusable_pass("tests/a.py", snap(), plan) is None
    # reconcile_running never promotes a non-PASS to PASS.
    out = te.reconcile_running(unk, mac_key=TEST_MAC_KEY)
    assert out.classification != ResultClass.TEST_PASS


def test_case13_process_disappearance_proves_no_pass():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    rep = exec_plan(
        plan, FakeRunner({"tests/a.py": ResultClass.PROCESS_FAILURE}), snapshot=snap()
    )
    assert rep.verdict == Verdict.BLOCKED
    assert rep.first_failure_class == ResultClass.PROCESS_FAILURE


# ---------------------------------------------------------------------------
# F — early-stopping adversarial
# ---------------------------------------------------------------------------


def test_case14_later_stages_skipped_after_genuine_test_failure():
    plan = real_plan("argent_core/scheduler.py")
    first_sel = plan.stages[0].selectors[0]
    runner = FakeRunner({first_sel: ResultClass.TEST_FAILURE})
    rep = exec_plan(plan, runner, snapshot=snap())
    assert rep.verdict == Verdict.FAILED
    assert rep.stages[0].state == StageState.FAILED
    assert all(s.state == StageState.SKIPPED for s in rep.stages[1:])
    assert rep.full_suite_avoided is True


def test_case15_early_pass_cannot_skip_mandatory_full_suite():
    plan = real_plan("argent_core/scheduler.py")
    assert plan.full_suite_required
    runner = FakeRunner()
    rep = exec_plan(plan, runner, snapshot=snap())
    assert rep.verdict == Verdict.DONE
    assert rep.stages[-1].name == "full_suite"
    assert rep.stages[-1].state == StageState.PASSED
    assert "tests/" in runner.calls


def test_case16_avoided_stage_on_broken_snapshot_not_pass_on_fixed():
    plan = real_plan("argent_core/scheduler.py")
    first_sel = plan.stages[0].selectors[0]

    # Broken snapshot v1: targeted fails, full_suite is SKIPPED (avoided).
    runner1 = FakeRunner({first_sel: ResultClass.TEST_FAILURE})
    rep1 = exec_plan(plan, runner1, snapshot=snap(source="v1"))
    assert rep1.verdict == Verdict.FAILED
    fs1 = rep1.stages[-1]
    assert fs1.name == "full_suite" and fs1.state == StageState.SKIPPED
    # No PASS evidence was produced for the avoided stage.
    assert not any(sr.classification == ResultClass.TEST_PASS for sr in fs1.selector_results)

    # Fixed snapshot v2: full_suite actually runs (not "reused" from v1).
    runner2 = FakeRunner()
    rep2 = exec_plan(plan, runner2, snapshot=snap(source="v2"))
    assert rep2.verdict == Verdict.DONE
    assert rep2.stages[-1].name == "full_suite"
    assert rep2.stages[-1].state == StageState.PASSED
    assert "tests/" in runner2.calls


# ---------------------------------------------------------------------------
# G — exact reuse adversarial
# ---------------------------------------------------------------------------


def test_case17_identical_snapshot_reuse():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    st = store()
    st.add(pass_record("tests/a.py", snap(), plan))
    runner = FakeRunner()
    rep = exec_plan(plan, runner, snapshot=snap(), store=st)
    assert rep.stages[0].selector_results[0].reused is True
    assert runner.calls == []


def test_case18_test_definition_change_invalidates_reuse():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    st = store()
    st.add(pass_record("tests/a.py", snap(testdef="t_old"), plan))
    rep = exec_plan(plan, FakeRunner(), snapshot=snap(testdef="t_new"), store=st)
    assert rep.stages[0].selector_results[0].reused is False


def test_case19_policy_change_invalidates_reuse():
    runner = FakeRunner()
    st = store()
    old = mk_plan([stage("targeted", ["tests/a.py"])], policy_hash="poh_old")
    st.add(pass_record("tests/a.py", snap(), old))
    new = mk_plan([stage("targeted", ["tests/a.py"])], policy_hash="poh_new")
    rep = exec_plan(new, runner, snapshot=snap(), store=st)
    assert rep.stages[0].selector_results[0].reused is False


def test_case20_inventory_change_invalidates_reuse():
    runner = FakeRunner()
    st = store()
    old = mk_plan([stage("targeted", ["tests/a.py"])], inventory_hash="ih_old")
    st.add(pass_record("tests/a.py", snap(), old))
    new = mk_plan([stage("targeted", ["tests/a.py"])], inventory_hash="ih_new")
    rep = exec_plan(new, runner, snapshot=snap(), store=st)
    assert rep.stages[0].selector_results[0].reused is False


# ---------------------------------------------------------------------------
# H — failure classification is stable and never PASS
# ---------------------------------------------------------------------------


def test_case21_resource_failure_never_pass():
    plan = mk_plan([stage("targeted", ["tests/a.py"]), stage("full_suite", ["tests/"])])
    gate = FakeGate(allowed=False, reason="disk low")
    rep = exec_plan(plan, FakeRunner(), resource_gate=gate, snapshot=snap())
    assert rep.verdict == Verdict.BLOCKED
    assert rep.first_failure_class == ResultClass.RESOURCE_FAILURE
    assert rep.stages[0].selector_results[0].classification == ResultClass.RESOURCE_FAILURE


def test_case22_test_infra_failure_never_pass():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    rep = exec_plan(plan, FakeRunner({"tests/a.py": ResultClass.TEST_INFRA_FAILURE}), snapshot=snap())
    assert rep.verdict == Verdict.BLOCKED
    assert rep.first_failure_class == ResultClass.TEST_INFRA_FAILURE


def test_case23_timeout_never_pass():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    rep = exec_plan(plan, FakeRunner({"tests/a.py": ResultClass.TIMEOUT}), snapshot=snap())
    assert rep.verdict == Verdict.BLOCKED
    assert rep.first_failure_class == ResultClass.TIMEOUT


def test_case24_unknown_never_pass():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    rep = exec_plan(plan, FakeRunner({"tests/a.py": ResultClass.UNKNOWN}), snapshot=snap())
    assert rep.verdict == Verdict.BLOCKED
    assert rep.first_failure_class == ResultClass.UNKNOWN


# ---------------------------------------------------------------------------
# J — agent trust boundary
# ---------------------------------------------------------------------------


def test_case25_agent_prose_cannot_force_selector_or_command():
    with pytest.raises(ValueError):
        te._assert_trusted_selector("pytest tests/ -x --lf")
    with pytest.raises(ValueError):
        te._assert_trusted_selector("../etc/passwd")
    with pytest.raises(ValueError):
        te._assert_trusted_selector("/tmp/evil.py")
    # A selector is a *path*, never a command.  Shell metacharacters do not
    # become executable: the bogus selector fails closed and the runner is
    # never invoked (no shell, no arbitrary command).
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        raise AssertionError("runner must not be invoked for a bogus selector")

    r = te.PytestRunner(runner_fn=fake_run, python="python3")
    oc = r.run("tests/; rm -rf /")
    assert oc.classification == ResultClass.TEST_INFRA_FAILURE
    assert calls == []


def test_case26_agent_prose_cannot_force_result_risk_or_done():
    # No free-text risk field on the trusted change evidence.
    with pytest.raises(TypeError):
        tp.ChangeEvidence(changed_paths=("x.py",), agent_claim="it's tiny")  # type: ignore[call-arg]
    # Classification is a bounded enum; prose cannot invent one.
    with pytest.raises(ValueError):
        ResultClass("DONE")
    # Risk is derived, frozen, and never mutable from outside.
    plan = real_plan("argent_core/trust.py")
    with pytest.raises((AttributeError, TypeError)):
        plan.risk_level = tp.RiskLevel.LOW  # type: ignore[misc]


def test_case27_no_shell_or_eval_in_product_code():
    # Precise detection: the builtins ``eval(``/``exec(`` as a call (word
    # boundary), plus the literal ``shell=True``.  Word boundaries avoid false
    # positives like ``retrieval(`` or ``execute_approved(``.
    import re

    eval_re = re.compile(r"\beval\s*\(")
    exec_re = re.compile(r"\bexec\s*\(")
    for f in Path("argent_core").glob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert "shell=True" not in text, f"shell=True found in {f}"
        assert not eval_re.search(text), f"eval( found in {f}"
        assert not exec_re.search(text), f"exec( found in {f}"


# ---------------------------------------------------------------------------
# D — signing authority is controller-owned (see trust-boundary file too)
# ---------------------------------------------------------------------------


def test_case28_missing_key_fails_closed(monkeypatch):
    monkeypatch.delenv(te._MAC_KEY_ENV, raising=False)
    monkeypatch.delenv(te._MAC_KEY_FILE_ENV, raising=False)
    with pytest.raises(ValueError):
        EvidenceStore()


def test_case29_signing_is_controller_owned():
    # The key is never part of the evidence record; it is resolved externally.
    rec = _signed_pass()
    assert "key" not in rec.__dict__
    assert "evidence_hash" in rec.__dict__  # only the MAC is stored, not the key
    # Two different keys -> two different MACs for the identical record.
    a = te.compute_evidence_mac(rec, b"key-A")
    b = te.compute_evidence_mac(rec, b"key-B")
    assert a != b


def test_case30_agent_writable_artifact_alone_cannot_make_trusted_pass(tmp_path):
    # An agent can write a JSON "store" file, but without the key it cannot
    # produce a valid MAC, so the file is rejected on load.
    p = tmp_path / "forged.json"
    p.write_text(
        json.dumps(
            {
                "evidence_store_version": "1",
                "records": [
                    {
                        "selector": "tests/a.py",
                        "source_hash": "s",
                        "test_definition_hash": "t",
                        "plan_hash": "p",
                        "inventory_hash": "i",
                        "policy_hash": "o",
                        "executor_id": te.EXECUTOR_ID,
                        "classification": "TEST_PASS",
                        "timestamp": "x",
                        "artifact_ref": "",
                        "summary": "",
                        "test_count": 1,
                        "evidence_hash": "0" * 64,  # no valid MAC
                        "root": "",
                        "config_hash": "",
                    }
                ],
            }
        )
    )
    with pytest.raises(ValueError):
        EvidenceStore(path=str(p), mac_key=TEST_MAC_KEY)


# ---------------------------------------------------------------------------
# K — resource / context / routing independence
# ---------------------------------------------------------------------------


def test_case31_phase_c_resource_gate_binding():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    gate = FakeGate(allowed=False, reason="capacity")
    rep = exec_plan(plan, FakeRunner(), resource_gate=gate, snapshot=snap())
    assert rep.verdict == Verdict.BLOCKED
    assert rep.first_failure_class == ResultClass.RESOURCE_FAILURE
    assert gate.calls == 1


def test_case32_phase_e_router_independent():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    rep = exec_plan(
        plan, FakeRunner({"tests/a.py": ResultClass.TEST_FAILURE}), snapshot=snap()
    )
    assert rep.verdict == Verdict.FAILED
    assert not hasattr(rep, "model") and not hasattr(rep, "provider")


def test_case33_phase_d_context_policy_unchanged():
    # A context-integrity change still demands Phase-D regression (unchanged).
    plan = real_plan("argent_core/context_pack.py")
    sels = plan.all_selectors()
    assert any(s.startswith("tests/test_phase_d") for s in sels)


# ---------------------------------------------------------------------------
# I — terminal verdict safety
# ---------------------------------------------------------------------------


def test_case34_done_requires_all_stage_evidence():
    plan = mk_plan(
        [
            stage("targeted", ["tests/a.py"], mandatory=["tests/a.py"]),
            stage("full_suite", ["tests/"], mandatory=["tests/"]),
        ],
        full_suite_required=True,
    )
    rep = exec_plan(plan, FakeRunner({"tests/a.py": ResultClass.TEST_FAILURE}), snapshot=snap())
    assert rep.verdict == Verdict.FAILED


def test_case35_terminal_done_immutable():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    rep = exec_plan(plan, FakeRunner(), snapshot=snap())
    assert rep.verdict == Verdict.DONE
    # The report is a frozen dataclass: the verdict cannot be flipped.
    with pytest.raises((AttributeError, TypeError)):
        rep.verdict = Verdict.FAILED  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        rep.plan_hash = "forged"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# M — test-economy self-protection
# ---------------------------------------------------------------------------


def test_case36_phase_f_infra_change_requires_full_suite():
    for path in (
        "argent_core/test_execution.py",
        "argent_core/test_planning.py",
        "argent_core/registry/test_policy_v1.json",
        "tests/f2_helpers.py",
    ):
        plan = real_plan(path)
        assert plan.full_suite_required, path
        assert "tests/" in plan.all_selectors(), path


def test_case37_malformed_authoritative_metadata_fails_closed():
    # Policy that tries to make UNKNOWN safe (no full suite) is rejected.
    d = json.loads(Path("argent_core/registry/test_policy_v1.json").read_text(encoding="utf-8"))
    d = json.loads(json.dumps(d))  # deep copy
    d["unknown_handling"] = {"policy": "IGNORE", "required_regression": [], "full_suite": False}
    with pytest.raises(tp.PolicyError):
        tp.TestPolicy.from_dict(d)
    # Inventory missing full_suite_selector is rejected.
    inv = json.loads(Path("argent_core/registry/test_inventory_v1.json").read_text(encoding="utf-8"))
    inv = json.loads(json.dumps(inv))
    del inv["full_suite_selector"]
    with pytest.raises(tp.InventoryError):
        tp.TestInventory.from_dict(inv)


def test_case38_duplicate_conflict_evidence_conservative():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    st = store()
    st.add(pass_record("tests/a.py", snap(), plan))
    st.add(fail_record("tests/a.py", snap(), plan))
    assert st.find_reusable_pass("tests/a.py", snap(), plan) is None
    runner = FakeRunner()
    rep = exec_plan(plan, runner, snapshot=snap(), store=st)
    assert rep.stages[0].selector_results[0].reused is False
    assert runner.calls == ["tests/a.py"]


def test_case39_restart_reconcile_idempotent():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    rec = fail_record("tests/a.py", snap(), plan, cls=ResultClass.UNKNOWN)
    once = te.reconcile_running(rec, mac_key=TEST_MAC_KEY)
    twice = te.reconcile_running(once, mac_key=TEST_MAC_KEY)
    assert once == twice
    # A tampered PASS downgrades to UNKNOWN deterministically.
    forged = te.replace(_signed_pass(), evidence_hash="0" * 64)
    d1 = te.reconcile_running(forged, mac_key=TEST_MAC_KEY)
    assert d1.classification == ResultClass.UNKNOWN
    assert d1 == te.reconcile_running(forged, mac_key=TEST_MAC_KEY)


def test_case40_integrated_broken_fix_closing_flow(tmp_path):
    plan = real_plan("argent_core/scheduler.py")
    first_sel = plan.stages[0].selectors[0]
    st = store(str(tmp_path / "ev.json"))

    # 1) Broken intermediate snapshot -> targeted FAIL -> later avoided.
    r1 = FakeRunner({first_sel: ResultClass.TEST_FAILURE})
    rep1 = exec_plan(plan, r1, snapshot=snap(source="v1"), store=st)
    assert rep1.verdict == Verdict.FAILED
    assert rep1.full_suite_avoided is True
    assert "tests/" not in r1.calls

    # 2) Fixed snapshot -> stale PASS invalidated -> early stages rerun,
    #    mandatory closing stages after green, full suite closes.
    r2 = FakeRunner()
    rep2 = exec_plan(plan, r2, snapshot=snap(source="v2"), store=st)
    assert rep2.verdict == Verdict.DONE
    assert "tests/" in r2.calls
    assert rep2.stages_executed == len(plan.stages)

    # 3) Identical fixed snapshot -> exact reuse -> duplicates avoided.
    r3 = FakeRunner()
    rep3 = exec_plan(plan, r3, snapshot=snap(source="v2"), store=st)
    assert rep3.verdict == Verdict.DONE
    assert r3.calls == []  # every selector reused exactly
    assert rep3.stages_reused == len(plan.stages)
