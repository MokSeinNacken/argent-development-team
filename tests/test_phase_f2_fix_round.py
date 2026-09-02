"""Phase F2 Fix-Round F1–F9 — adversarial tests for the Sol review findings.

Each ``test_fN_*`` verifies the corresponding HIGH/MEDIUM/LOW finding from the
independent Sol closing review, all of which were confirmed in code.  Offline
and deterministic (injected fakes, real tmp filesystem where needed).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from argent_core import test_execution as te
from argent_core.test_execution import (
    EvidenceRecord,
    EvidenceStore,
    PytestRunner,
    ResourceGovernorGate,
    ResultClass,
    SnapshotIdentity,
    StageState,
    Verdict,
    compute_snapshot_identity,
)

from f2_helpers import (
    FakeGate,
    FakeProc,
    FakeRunner,
    TEST_MAC_KEY,
    exec_plan,
    fail_record,
    mk_plan,
    pass_record,
    snap,
    stage,
    store,
)


class _Decision:
    """Minimal Phase-C admission decision fake for the F6 bridge test."""

    def __init__(self, decision: str, reason_code: str = ""):
        self.decision = decision
        self.reason_code = reason_code


def _mk_tmp_project(tmp_path: Path) -> None:
    (tmp_path / "argent_core").mkdir()
    (tmp_path / "argent_core" / "core.py").write_text("x = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n")
    (tmp_path / "e2e-fixture").mkdir()
    (tmp_path / "e2e-fixture" / "parser.py").write_text("def parse(): return 1\n")
    (tmp_path / "e2e-fixture" / "tests").mkdir()
    (tmp_path / "e2e-fixture" / "tests" / "test_p.py").write_text("def test_p(): pass\n")


# ---------------------------------------------------------------------------
# F1 — glob resolution (HIGH)
# ---------------------------------------------------------------------------


def test_f1_glob_selector_resolved_to_explicit_files():
    captured = {}

    def fake_run(argv, timeout=None, capture_output=None, shell=None, cwd=None):
        captured["argv"] = argv
        return FakeProc(0, b"3 passed in 0.1s", b"")

    r = PytestRunner(runner_fn=fake_run, python="python3")
    r.run("tests/test_phase_f1*.py")
    argv = captured["argv"]
    i = argv.index("pytest")
    resolved = [a for a in argv[i + 1 :] if a not in ("-q", "--tb=line")]
    assert resolved, "glob must resolve to at least one explicit path"
    assert all("*" not in a and "?" not in a and "[" not in a for a in resolved)
    assert len(resolved) == 3  # acceptance / fix_round / inventory


def test_f1_zero_match_glob_fails_closed():
    calls = []

    def fake_run(*a, **k):
        calls.append(a)
        return FakeProc(0)

    r = PytestRunner(runner_fn=fake_run, python="python3")
    oc = r.run("tests/zzz_no_such_file_*.py")
    assert oc.classification == ResultClass.TEST_INFRA_FAILURE
    assert calls == []  # runner never invoked on zero-match


# ---------------------------------------------------------------------------
# F2 — extra_roots in snapshot identity (HIGH)
# ---------------------------------------------------------------------------


def test_f2_extra_roots_change_identity(tmp_path):
    _mk_tmp_project(tmp_path)
    base = compute_snapshot_identity(str(tmp_path), extra_roots=("e2e-fixture",))
    (tmp_path / "e2e-fixture" / "parser.py").write_text("def parse(): return 2\n")
    changed = compute_snapshot_identity(str(tmp_path), extra_roots=("e2e-fixture",))
    assert changed.source_hash != base.source_hash
    (tmp_path / "e2e-fixture" / "tests" / "test_p.py").write_text(
        "def test_p(): assert True\n"
    )
    changed2 = compute_snapshot_identity(str(tmp_path), extra_roots=("e2e-fixture",))
    assert changed2.test_definition_hash != changed.test_definition_hash


def test_f2_default_covers_e2e_fixture():
    with_default = compute_snapshot_identity()
    without = compute_snapshot_identity(extra_roots=())
    assert with_default.source_hash != without.source_hash


def test_f2_artifacts_excluded(tmp_path):
    _mk_tmp_project(tmp_path)
    base = compute_snapshot_identity(str(tmp_path), extra_roots=("e2e-fixture",))
    pycache = tmp_path / "argent_core" / "__pycache__"
    pycache.mkdir()
    (pycache / "core.cpython-314.pyc").write_bytes(b"\x00\x01")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".pytest_cache" / "v").write_text("cache")
    after = compute_snapshot_identity(str(tmp_path), extra_roots=("e2e-fixture",))
    assert after.source_hash == base.source_hash
    assert after.test_definition_hash == base.test_definition_hash


def test_f2_symlink_target_change_detected(tmp_path):
    _mk_tmp_project(tmp_path)
    base = compute_snapshot_identity(str(tmp_path), extra_roots=("e2e-fixture",))
    target = tmp_path / "target.txt"
    target.write_text("v1\n")
    link = tmp_path / "argent_core" / "linked.txt"
    link.symlink_to(target)
    with_link = compute_snapshot_identity(str(tmp_path), extra_roots=("e2e-fixture",))
    assert with_link.source_hash != base.source_hash
    target.write_text("v2\n")
    after_change = compute_snapshot_identity(str(tmp_path), extra_roots=("e2e-fixture",))
    assert after_change.source_hash != with_link.source_hash


# ---------------------------------------------------------------------------
# F3 — plan integrity fail-closed (HIGH)
# ---------------------------------------------------------------------------


def test_f3_plan_hash_tamper_rejected():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    tampered = te.replace(plan, stages=(stage("targeted", ["tests/DIFFERENT.py"]),))
    with pytest.raises(ValueError):
        te.execute_plan(tampered, FakeRunner(), snapshot=snap(), resource_gate=FakeGate(), mac_key=TEST_MAC_KEY)


def test_f3_duplicate_stage_names_rejected():
    plan = mk_plan(
        [stage("targeted", ["tests/a.py"]), stage("targeted", ["tests/b.py"])]
    )
    with pytest.raises(ValueError):
        te.execute_plan(plan, FakeRunner(), snapshot=snap(), resource_gate=FakeGate(), mac_key=TEST_MAC_KEY)


def test_f3_empty_stage_rejected():
    plan = mk_plan([stage("targeted", [])])
    with pytest.raises(ValueError):
        te.execute_plan(plan, FakeRunner(), snapshot=snap(), resource_gate=FakeGate(), mac_key=TEST_MAC_KEY)


def test_f3_out_of_order_rejected():
    plan = mk_plan(
        [stage("full_suite", ["tests/"]), stage("targeted", ["tests/a.py"])]
    )
    with pytest.raises(ValueError):
        te.execute_plan(plan, FakeRunner(), snapshot=snap(), resource_gate=FakeGate(), mac_key=TEST_MAC_KEY)


def test_f3_full_suite_missing_when_required():
    plan = mk_plan([stage("targeted", ["tests/a.py"])], full_suite_required=True)
    with pytest.raises(ValueError):
        te.execute_plan(plan, FakeRunner(), snapshot=snap(), resource_gate=FakeGate(), mac_key=TEST_MAC_KEY)


# ---------------------------------------------------------------------------
# F4 — authenticated evidence provenance (HIGH)
# ---------------------------------------------------------------------------


def test_f4_mac_key_required_fail_closed(monkeypatch):
    monkeypatch.delenv(te._MAC_KEY_ENV, raising=False)
    monkeypatch.delenv(te._MAC_KEY_FILE_ENV, raising=False)
    with pytest.raises(ValueError):
        EvidenceStore()


def test_f4_unkeyed_hash_is_not_valid_mac():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    rec = pass_record("tests/a.py", snap(), plan)
    unkeyed = te.sha256_hex(te.canonical_bytes({"x": 1}))  # public unkeyed sha256
    bad = te.replace(rec, evidence_hash=unkeyed)
    with pytest.raises(ValueError):
        store().add(bad)


def test_f4_tampered_fail_to_pass_rejected(tmp_path):
    p = str(tmp_path / "ev.json")
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    s = EvidenceStore(path=p, mac_key=TEST_MAC_KEY)
    s.add(fail_record("tests/a.py", snap(), plan))
    # Tamper: flip FAIL -> PASS but keep the (now-invalid) MAC.
    data = json.loads(Path(p).read_text())
    for r in data["records"]:
        r["classification"] = "TEST_PASS"
    Path(p).write_text(json.dumps(data))
    with pytest.raises(ValueError):
        EvidenceStore(path=p, mac_key=TEST_MAC_KEY)


def test_f4_different_key_rejects(tmp_path):
    p = str(tmp_path / "ev.json")
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    s = EvidenceStore(path=p, mac_key=TEST_MAC_KEY)
    s.add(pass_record("tests/a.py", snap(), plan))
    with pytest.raises(ValueError):
        EvidenceStore(path=p, mac_key=b"a-different-key-0000000000000000")


# ---------------------------------------------------------------------------
# F5 — cwd / root binding (HIGH)
# ---------------------------------------------------------------------------


def test_f5_runner_binds_cwd(tmp_path):
    captured = {}

    def fake_run(argv, timeout=None, capture_output=None, shell=None, cwd=None):
        captured["cwd"] = cwd
        return FakeProc(0, b"1 passed in 0.1s", b"")

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_x(): pass\n")
    r = PytestRunner(runner_fn=fake_run, python="python3", project_root=str(tmp_path))
    oc = r.run("tests/test_a.py")
    assert captured["cwd"] == str(tmp_path.resolve())
    assert oc.classification == ResultClass.TEST_PASS


def test_f5_snapshot_root_mismatch_fails_closed():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])

    class RootedRunner(FakeRunner):
        project_root = "/fake/root/y"

    snapshot = snap(root="/fake/root/x")
    with pytest.raises(ValueError):
        exec_plan(plan, RootedRunner(), snapshot=snapshot)


def test_f5_evidence_binds_root():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    st = store()
    st.add(pass_record("tests/a.py", snap(root="/r1"), plan))
    runner = FakeRunner()
    rep = exec_plan(plan, runner, snapshot=snap(root="/r2"), store=st)
    assert rep.stages[0].selector_results[0].reused is False
    assert runner.calls == ["tests/a.py"]


# ---------------------------------------------------------------------------
# F6 — gate fail-closed + Phase-C bridge (HIGH)
# ---------------------------------------------------------------------------


def test_f6_no_gate_fails_closed():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    rep = te.execute_plan(plan, FakeRunner(), snapshot=snap(), mac_key=TEST_MAC_KEY)
    assert rep.verdict == Verdict.BLOCKED
    assert rep.first_failure_class == ResultClass.RESOURCE_FAILURE
    assert rep.stages[0].state == StageState.BLOCKED


def test_f6_resource_governor_gate_bridge():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    allow = ResourceGovernorGate(lambda: _Decision("ALLOW"))
    rep = te.execute_plan(plan, FakeRunner(), snapshot=snap(), resource_gate=allow, mac_key=TEST_MAC_KEY)
    assert rep.verdict == Verdict.DONE

    deny = ResourceGovernorGate(lambda: _Decision("DENY_LOCAL", "DISK_LOW"))
    rep2 = te.execute_plan(plan, FakeRunner(), snapshot=snap(), resource_gate=deny, mac_key=TEST_MAC_KEY)
    assert rep2.verdict == Verdict.BLOCKED
    assert rep2.first_failure_class == ResultClass.RESOURCE_FAILURE


# ---------------------------------------------------------------------------
# F7 — bounded classification (MEDIUM)
# ---------------------------------------------------------------------------


def test_f7_fixture_setup_error_is_infra():
    assert (
        PytestRunner.classify(1, b"ERROR at setup of test_x", b"")
        == ResultClass.TEST_INFRA_FAILURE
    )
    assert PytestRunner.classify(1, b"1 error in 0.5s", b"") == ResultClass.TEST_INFRA_FAILURE
    # A genuine assertion failure is still TEST_FAILURE.
    assert PytestRunner.classify(1, b"1 failed in 0.5s", b"") == ResultClass.TEST_FAILURE


def test_f7_rc137_needs_scope_evidence():
    assert PytestRunner.classify(137, b"", b"") == ResultClass.UNKNOWN
    assert (
        PytestRunner.classify(137, b"", b"", scope_evidence=True)
        == ResultClass.RESOURCE_FAILURE
    )


def test_f7_rc124_never_pass():
    assert PytestRunner.classify(124, b"", b"") == ResultClass.UNKNOWN
    assert PytestRunner.classify(124, b"command timed out", b"") == ResultClass.TIMEOUT


# ---------------------------------------------------------------------------
# F8 — actionable non-PASS evidence (MEDIUM)
# ---------------------------------------------------------------------------


def test_f8_failure_summary_preserved_in_report():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    runner = FakeRunner(
        {
            "tests/a.py": te.RunnerOutcome(
                ResultClass.TEST_FAILURE, summary="assertion broke", artifact_ref="af123"
            )
        }
    )
    st = store()
    rep = exec_plan(plan, runner, snapshot=snap(), store=st)
    sr = rep.stages[0].selector_results[0]
    assert sr.classification == ResultClass.TEST_FAILURE
    assert sr.summary == "assertion broke"
    assert sr.artifact_ref == "af123"
    # Persisted (bounded) but never reusable as PASS.
    fails = [r for r in st.records() if r.classification == ResultClass.TEST_FAILURE]
    assert len(fails) == 1
    assert fails[0].summary == "assertion broke"
    assert st.find_reusable_pass("tests/a.py", snap(), plan) is None


# ---------------------------------------------------------------------------
# F9 — bounded gaps (LOW)
# ---------------------------------------------------------------------------


def test_f9_summary_and_artifact_ref_truncated():
    rec = EvidenceRecord(
        selector="tests/a.py",
        source_hash="s",
        test_definition_hash="t",
        plan_hash="p",
        inventory_hash="i",
        policy_hash="o",
        executor_id="e",
        classification=ResultClass.TEST_PASS,
        timestamp="x",
        summary="x" * 5000,
        artifact_ref="y" * 500,
    )
    assert len(rec.summary) == te._MAX_SUMMARY
    assert len(rec.artifact_ref) == te._MAX_ARTIFACT_REF


def test_f9_store_trims_on_load(tmp_path):
    p = str(tmp_path / "ev.json")
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    s1 = EvidenceStore(path=p, max_records=2, mac_key=TEST_MAC_KEY)
    for sel in ("tests/a.py", "tests/b.py", "tests/c.py"):
        s1.add(pass_record(sel, snap(), plan))
    assert len(s1.records()) == 2
    s2 = EvidenceStore(path=p, max_records=2, mac_key=TEST_MAC_KEY)
    assert len(s2.records()) == 2
