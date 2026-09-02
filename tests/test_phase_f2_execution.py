"""Phase F2 — staged execution, evidence reuse, restart/classification (unit).

Deterministic, offline.  No network, no shell, no real pytest subprocess —
all execution goes through an injected fake runner/gate/store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from argent_core import test_execution as te
from argent_core.test_execution import (
    EvidenceRecord,
    EvidenceStore,
    PytestRunner,
    ResultClass,
    RunnerOutcome,
    SnapshotIdentity,
    StageState,
    Verdict,
    execute_plan,
    reconcile_running,
)

from f2_helpers import (
    FakeProc,
    FakeRunner,
    TEST_MAC_KEY,
    mk_plan,
    pass_record,
    snap,
    stage,
)


# ---------------------------------------------------------------------------
# PytestRunner: classification + execution boundary
# ---------------------------------------------------------------------------


def test_pytest_runner_classifies_exit_codes():
    assert PytestRunner.classify(0, b"1 passed", b"") == ResultClass.TEST_PASS
    assert PytestRunner.classify(1, b"1 failed", b"") == ResultClass.TEST_FAILURE
    assert PytestRunner.classify(2, b"", b"") == ResultClass.CANCELLED_BLOCKED
    for rc in (3, 4, 5):
        assert PytestRunner.classify(rc, b"", b"") == ResultClass.TEST_INFRA_FAILURE
    assert PytestRunner.classify(42, b"", b"") == ResultClass.UNKNOWN


def test_pytest_runner_never_uses_shell_true():
    captured = {}

    def fake_run(argv, timeout=None, capture_output=None, shell=None, cwd=None):
        captured["argv"] = argv
        captured["shell"] = shell
        captured["cwd"] = cwd
        return FakeProc(0, b"2 passed in 0.1s", b"")

    r = PytestRunner(runner_fn=fake_run, python="python3")
    oc = r.run("tests/test_phase_f1*.py")
    assert captured["shell"] is False
    assert captured["argv"][0] == "python3"
    assert "-m" in captured["argv"] and "pytest" in captured["argv"]
    assert captured["cwd"] == str(te._PROJECT_ROOT.resolve())
    assert oc.classification == ResultClass.TEST_PASS
    assert oc.test_count == 2


def test_pytest_runner_rejects_untrusted_selector():
    r = PytestRunner(runner_fn=lambda *a, **k: FakeProc(0))
    for bad in ("", "rm -rf /", "/etc/passwd", "../secret", "pytest tests/ -x"):
        with pytest.raises(ValueError):
            r.run(bad)


def test_pytest_runner_timeout_classifies():
    import subprocess as sp

    def fake_run(argv, timeout=None, capture_output=None, shell=None, cwd=None):
        raise sp.TimeoutExpired(cmd=argv, timeout=timeout)

    r = PytestRunner(runner_fn=fake_run, timeout_seconds=1)
    assert r.run("tests/test_phase_f1*.py").classification == ResultClass.TIMEOUT


def test_pytest_runner_process_failure_classifies():
    def fake_run(argv, timeout=None, capture_output=None, shell=None, cwd=None):
        raise OSError("exec not found")

    r = PytestRunner(runner_fn=fake_run)
    assert r.run("tests/test_phase_f1*.py").classification == ResultClass.PROCESS_FAILURE


def test_case18_no_shell_or_eval_in_product_code():
    src = Path(te.__file__).read_text()
    assert "shell=True" not in src
    assert "eval(" not in src
    assert "exec(" not in src


# ---------------------------------------------------------------------------
# Snapshot identity + executor guardrails
# ---------------------------------------------------------------------------


def test_snapshot_identity_requires_nonempty_fields():
    with pytest.raises(ValueError):
        SnapshotIdentity("", "t")
    with pytest.raises(ValueError):
        SnapshotIdentity("s", "")


def test_executor_rejects_non_testplan():
    with pytest.raises(TypeError):
        execute_plan("not a plan", FakeRunner(), snapshot=snap())


def test_case16_interrupted_running_never_becomes_pass():
    rec = EvidenceRecord(
        selector="tests/a.py",
        source_hash="s", test_definition_hash="t", plan_hash="p",
        inventory_hash="i", policy_hash="o", executor_id="e",
        classification=ResultClass.UNKNOWN, timestamp="x", evidence_hash="",
    )
    assert reconcile_running(rec).classification == ResultClass.UNKNOWN
    good = pass_record("tests/a.py", snap(), mk_plan([stage("targeted", ["tests/a.py"])]))
    tampered = te.replace(good, evidence_hash="bad" * 16)
    assert reconcile_running(tampered).classification == ResultClass.UNKNOWN


def test_case24_malformed_persisted_evidence_fails_closed(tmp_path):
    p = tmp_path / "ev.json"
    p.write_text(json.dumps({"evidence_store_version": "1", "records": [
        {"selector": "tests/a.py", "source_hash": "s", "test_definition_hash": "t",
         "plan_hash": "p", "inventory_hash": "i", "policy_hash": "o",
         "executor_id": "e", "classification": "TEST_PASS", "timestamp": "x",
         "evidence_hash": "bogus"}]}))
    with pytest.raises(ValueError):
        EvidenceStore(path=str(p), mac_key=TEST_MAC_KEY)


def test_case24b_wrong_version_fails_closed(tmp_path):
    p = tmp_path / "ev.json"
    p.write_text(json.dumps({"evidence_store_version": "999", "records": []}))
    with pytest.raises(ValueError):
        EvidenceStore(path=str(p), mac_key=TEST_MAC_KEY)


def test_evidence_store_rejects_tampered_add():
    store = EvidenceStore(mac_key=TEST_MAC_KEY)
    rec = pass_record("tests/a.py", snap(), mk_plan([stage("targeted", ["tests/a.py"])]))
    bad = te.replace(rec, evidence_hash="bogus" * 16)
    with pytest.raises(ValueError):
        store.add(bad)


def test_evidence_store_persists_and_reloads(tmp_path):
    p = str(tmp_path / "ev.json")
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    s1 = EvidenceStore(path=p, mac_key=TEST_MAC_KEY)
    s1.add(pass_record("tests/a.py", snap(), plan))
    s2 = EvidenceStore(path=p, mac_key=TEST_MAC_KEY)
    assert len(s2.records()) == 1
    assert s2.find_reusable_pass("tests/a.py", snap(), plan) is not None
