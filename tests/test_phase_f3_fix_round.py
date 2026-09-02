"""Phase F3 Fix-Round — adversarial regression tests for findings F1–F8.

These tests *prove* the code-enforced hardenings added in the F3 fix round (see
docs/PHASE_F3_ACCEPTANCE.md § Fix-Round).  Each ``test_fN_*`` reproduces the
exact attack from the Sol review and asserts it now fails closed.

Offline and deterministic: injected fakes, a real tmp filesystem where a real
execution root is required (F2), no network, no shell, no subprocess.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from argent_core import test_execution as te
from argent_core import test_planning as tp
from argent_core.test_execution import (
    EvidenceStore,
    ResourceGovernorGate,
    ResultClass,
    StaleWriteError,
    Verdict,
    compute_snapshot_identity,
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
)


def _mk_tmp_project(tmp_path: Path) -> None:
    (tmp_path / "argent_core").mkdir()
    (tmp_path / "argent_core" / "core.py").write_text("x = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n")
    (tmp_path / "e2e-fixture").mkdir()
    (tmp_path / "e2e-fixture" / "parser.py").write_text("def p(): return 1\n")
    (tmp_path / "e2e-fixture" / "tests").mkdir()
    (tmp_path / "e2e-fixture" / "tests" / "test_p.py").write_text("def test_p(): pass\n")


# ---------------------------------------------------------------------------
# F1 — plan provenance: a re-hashed/weakened plan must never be accepted
# ---------------------------------------------------------------------------


def test_f1_unsigned_plan_rejected():
    # build_test_plan without a key mints plan_mac="" -> execute_plan rejects.
    plan = tp.build_test_plan(
        tp.ChangeEvidence(("argent_core/visualizer_snapshot.py",)),
        tp.load_policy(),
        tp.load_inventory(),
    )
    assert plan.plan_mac == ""
    with pytest.raises(ValueError):
        te.execute_plan(plan, FakeRunner(), snapshot=snap(), resource_gate=FakeGate(), mac_key=TEST_MAC_KEY)


def test_f1_rehashed_weakened_plan_rejected():
    # Attacker takes a real HIGH-risk plan, drops full_suite, lowers risk, and
    # recomputes the *public* plan_hash.  Without the key they cannot mint a
    # valid plan_mac, so execute_plan must refuse (never DONE).
    plan = real_plan("argent_core/trust.py")
    assert plan.full_suite_required
    weakened = te.replace(
        plan,
        risk_level=tp.RiskLevel.LOW,
        full_suite_required=False,
        stages=(stage("targeted", ["tests/a.py"]),),
    )
    weakened = te.replace(weakened, plan_hash=te.recompute_plan_hash(weakened))
    with pytest.raises(ValueError):
        te.execute_plan(weakened, FakeRunner(), snapshot=snap(), resource_gate=FakeGate(), mac_key=TEST_MAC_KEY)


# ---------------------------------------------------------------------------
# F2 — snapshot identity is recomputed from the real root, not caller-promised
# ---------------------------------------------------------------------------


def test_f2_snapshot_identity_recomputed_at_real_root(tmp_path):
    _mk_tmp_project(tmp_path)
    computed = compute_snapshot_identity(str(tmp_path))
    plan = mk_plan([stage("targeted", ["tests/a.py"])])

    # Correct snapshot -> the runner is actually invoked and DONE is reachable.
    runner = FakeRunner()
    rep = te.execute_plan(
        plan, runner, snapshot=computed, resource_gate=FakeGate(),
        project_root=str(tmp_path), mac_key=TEST_MAC_KEY,
    )
    assert rep.verdict == Verdict.DONE
    assert runner.calls == ["tests/a.py"]

    # Stale/wrong source_hash at the same root -> fail-closed (never reuse, never DONE).
    bad = te.replace(computed, source_hash="0" * 64)
    with pytest.raises(ValueError):
        te.execute_plan(
            plan, FakeRunner(), snapshot=bad, resource_gate=FakeGate(),
            project_root=str(tmp_path), mac_key=TEST_MAC_KEY,
        )

    # Wrong test_definition_hash is also rejected.
    bad_td = te.replace(computed, test_definition_hash="1" * 64)
    with pytest.raises(ValueError):
        te.execute_plan(
            plan, FakeRunner(), snapshot=bad_td, resource_gate=FakeGate(),
            project_root=str(tmp_path), mac_key=TEST_MAC_KEY,
        )


# ---------------------------------------------------------------------------
# F3 — store single-writer fencing: a stale executor cannot clobber newer data
# ---------------------------------------------------------------------------


def test_f3_stale_executor_write_rejected(tmp_path):
    p = str(tmp_path / "ev.json")
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    a = EvidenceStore(path=p, mac_key=TEST_MAC_KEY)
    b = EvidenceStore(path=p, mac_key=TEST_MAC_KEY)

    # B (newer) persists a FAIL first.
    b.add(fail_record("tests/a.py", snap(), plan))

    # A (stale, loaded before B wrote) is refused fail-closed.
    with pytest.raises(StaleWriteError):
        a.add(pass_record("tests/a.py", snap(), plan))

    # Reload shows only B's FAIL — A's stale PASS was never persisted.
    c = EvidenceStore(path=p, mac_key=TEST_MAC_KEY)
    recs = c.records()
    assert any(r.classification == ResultClass.TEST_FAILURE for r in recs)
    assert all(r.classification != ResultClass.TEST_PASS for r in recs)


def test_f3_single_instance_still_persists(tmp_path):
    # The fencing must not break the normal single-writer path.
    p = str(tmp_path / "ev.json")
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    s = EvidenceStore(path=p, mac_key=TEST_MAC_KEY)
    s.add(pass_record("tests/a.py", snap(), plan))
    s.add(fail_record("tests/a.py", snap(), plan))
    reloaded = EvidenceStore(path=p, mac_key=TEST_MAC_KEY)
    assert len(reloaded.records()) == 2


# ---------------------------------------------------------------------------
# F4 — executor self-protection cannot be dodged via a bare basename
# ---------------------------------------------------------------------------


def test_f4_basename_alias_reclassification_rejected():
    d = json.loads(Path("argent_core/registry/test_inventory_v1.json").read_text(encoding="utf-8"))
    d = json.loads(json.dumps(d))
    ownership = dict(d["module_ownership"])
    # Remove the exact protected entry and re-add the bare basename as CORE.
    del ownership["argent_core/test_execution.py"]
    ownership["test_execution.py"] = "CORE"
    d["module_ownership"] = ownership
    with pytest.raises(tp.InventoryError):
        tp.TestInventory.from_dict(d)


def test_f4_basename_alias_planner_rejected():
    d = json.loads(Path("argent_core/registry/test_inventory_v1.json").read_text(encoding="utf-8"))
    d = json.loads(json.dumps(d))
    ownership = dict(d["module_ownership"])
    del ownership["argent_core/test_planning.py"]
    ownership["test_planning.py"] = "CORE"
    d["module_ownership"] = ownership
    with pytest.raises(tp.InventoryError):
        tp.TestInventory.from_dict(d)


# ---------------------------------------------------------------------------
# F5 — full-suite narrowing cannot shrink the closing floor
# ---------------------------------------------------------------------------


def test_f5_full_suite_selector_narrowing_rejected():
    d = json.loads(Path("argent_core/registry/test_inventory_v1.json").read_text(encoding="utf-8"))
    d = json.loads(json.dumps(d))
    d["full_suite_selector"] = "tests/test_phase_f3_acceptance.py"
    with pytest.raises(tp.InventoryError):
        tp.TestInventory.from_dict(d)


def test_f5_test_infra_handling_empty_regression_rejected():
    d = json.loads(Path("argent_core/registry/test_policy_v1.json").read_text(encoding="utf-8"))
    d = json.loads(json.dumps(d))
    d["test_infra_handling"] = {"policy": "BROAD_CLOSING", "required_regression": [], "full_suite": True}
    with pytest.raises(tp.PolicyError):
        tp.TestPolicy.from_dict(d)


def test_f5_unknown_handling_empty_regression_rejected():
    d = json.loads(Path("argent_core/registry/test_policy_v1.json").read_text(encoding="utf-8"))
    d = json.loads(json.dumps(d))
    d["unknown_handling"] = {"policy": "BROADEN", "required_regression": [], "full_suite": True}
    with pytest.raises(tp.PolicyError):
        tp.TestPolicy.from_dict(d)


# ---------------------------------------------------------------------------
# F6 — terminal DONE is origin-bound and an empty plan is never DONE
# ---------------------------------------------------------------------------


def test_f6_direct_done_construction_not_all_pass():
    rep = te.ExecutionReport(plan_hash="p", snapshot=snap(), verdict=Verdict.DONE, stages=())
    assert rep.all_pass() is False


def test_f6_empty_plan_not_done():
    plan = mk_plan([])
    rep = exec_plan(plan, FakeRunner(), snapshot=snap())
    assert rep.verdict == Verdict.BLOCKED
    assert rep.all_pass() is False


def test_f6_authoritative_done_all_pass():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    rep = exec_plan(plan, FakeRunner(), snapshot=snap())
    assert rep.verdict == Verdict.DONE
    assert rep.all_pass() is True


# ---------------------------------------------------------------------------
# F7 — empty / too-short MAC keys are rejected fail-closed
# ---------------------------------------------------------------------------


def test_f7_empty_and_short_keys_rejected():
    for bad in (b"", "", b"short", b"0123456789abcdef"[:15]):
        with pytest.raises(ValueError):
            te._resolve_mac_key(bad)
    with pytest.raises(ValueError):
        EvidenceStore(mac_key=b"")


def test_f7_valid_key_accepted():
    assert te._resolve_mac_key(TEST_MAC_KEY) == TEST_MAC_KEY


# ---------------------------------------------------------------------------
# F8 — the Phase-C resource gate seam requires a callable decision function
# ---------------------------------------------------------------------------


def test_f8_resource_governor_gate_requires_callable():
    with pytest.raises(ValueError):
        ResourceGovernorGate("not a callable")
    with pytest.raises(ValueError):
        ResourceGovernorGate(None)
