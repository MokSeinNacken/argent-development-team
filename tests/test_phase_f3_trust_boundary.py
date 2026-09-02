"""Phase F3 — trust boundary proofs (sections D, J, K, H).

Deterministic, offline.  These tests pin the *exact* trust boundary of the
evidence store: what is CODE-ENFORCED (MAC verification, fail-closed key
resolution, tamper rejection on load, atomic writes) versus what is
OPERATIONALLY REQUIRED (key/store location outside the agent write area, who
holds the keyed store instance).  They also pin the agent-trust boundary
(writer/reviewer prose is untrusted data) and the failure-classification
invariants.

The corresponding prose distinction lives in docs/PHASE_F_ACCEPTANCE.md.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from argent_core import test_execution as te
from argent_core import test_planning as tp
from argent_core.test_execution import (
    EvidenceRecord,
    EvidenceStore,
    PytestRunner,
    ResultClass,
    RunnerOutcome,
)

from f2_helpers import (
    TEST_MAC_KEY,
    FakeGate,
    FakeProc,
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
# D — HMAC / evidence-store trust boundary
# ---------------------------------------------------------------------------


def test_d_mac_verification_is_code_enforced():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    st = store()
    st.add(pass_record("tests/a.py", snap(), plan))
    signed = st.records()[0]
    # Any tamper of an identity/result field breaks the MAC -> reject on add.
    for field, value in (
        ("selector", "tests/OTHER.py"),
        ("source_hash", "sX"),
        ("classification", ResultClass.TEST_FAILURE),
        ("summary", "forged summary"),
        ("artifact_ref", "forged"),
        ("plan_hash", "forged-plan"),
        ("test_count", 999),
    ):
        bad = te.replace(signed, **{field: value})
        with pytest.raises(ValueError):
            store().add(bad)


def test_d_fail_closed_key_resolution(monkeypatch, tmp_path):
    # No key anywhere -> ValueError.
    monkeypatch.delenv(te._MAC_KEY_ENV, raising=False)
    monkeypatch.delenv(te._MAC_KEY_FILE_ENV, raising=False)
    with pytest.raises(ValueError):
        te._resolve_mac_key(None)
    # Empty key file -> ValueError.
    empty = tmp_path / "empty.key"
    empty.write_bytes(b"")
    monkeypatch.setenv(te._MAC_KEY_FILE_ENV, str(empty))
    with pytest.raises(ValueError):
        te._resolve_mac_key(None)
    # Empty env var -> ValueError.
    monkeypatch.delenv(te._MAC_KEY_FILE_ENV, raising=False)
    monkeypatch.setenv(te._MAC_KEY_ENV, "   ")
    with pytest.raises(ValueError):
        te._resolve_mac_key(None)
    # Explicit argument has highest precedence (and must meet the min length).
    assert te._resolve_mac_key(b"explicit-key-0000000000000000") == b"explicit-key-0000000000000000"


def test_d_store_write_is_atomic(tmp_path):
    p = tmp_path / "ev.json"
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    st = EvidenceStore(path=str(p), mac_key=TEST_MAC_KEY)
    st.add(pass_record("tests/a.py", snap(), plan))
    # No partial .tmp file remains; the main file is valid and reloadable.
    assert not list(tmp_path.glob("*.tmp"))
    st2 = EvidenceStore(path=str(p), mac_key=TEST_MAC_KEY)
    assert len(st2.records()) == 1


def test_d_key_never_lives_in_evidence_record():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    rec = pass_record("tests/a.py", snap(), plan)
    # The record carries only a MAC (evidence_hash), never the key itself.
    assert "evidence_hash" in rec.__dict__
    assert not any("key" in k.lower() for k in rec.__dict__)


def test_d_agent_cannot_choose_store_path_or_key():
    # The store path and MAC key are controller constructor arguments; the
    # evidence record has no field to influence either.
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    rec = pass_record("tests/a.py", snap(), plan)
    for k in rec.__dict__:
        assert "path" not in k.lower() and "key" not in k.lower(), k
    # The only signing API is EvidenceStore.add, which requires the key at
    # construction time (fail-closed otherwise).
    with pytest.raises(ValueError):
        EvidenceStore(path=None)  # no key -> fail-closed


def test_d_signing_authority_is_controller_owned():
    # MACs are produced with the store's key, never with anything the writer
    # controls.  Same record, different controller keys -> different MACs.
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    rec = pass_record("tests/a.py", snap(), plan)
    assert te.compute_evidence_mac(rec, b"k1") != te.compute_evidence_mac(rec, b"k2")
    # A store signed under key A is not verifiable under key B.
    st = EvidenceStore(mac_key=b"kA" + b"0" * 15)
    st.add(rec)
    signed = st.records()[0]
    assert not signed.evidence_hash == te.compute_evidence_mac(rec, b"kB")


# ---------------------------------------------------------------------------
# J — agent trust boundary (writer/reviewer output is untrusted data)
# ---------------------------------------------------------------------------


def test_j_no_free_prose_fields_on_trusted_objects():
    # ChangeEvidence has no "this is small" free-text field.
    with pytest.raises(TypeError):
        tp.ChangeEvidence(changed_paths=("x.py",), risk_claim="low")  # type: ignore[call-arg]
    # EvidenceRecord has no prose/verdict/reusable field.
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    rec = pass_record("tests/a.py", snap(), plan)
    for field in ("prose", "verdict", "reusable", "agent_claim", "risk"):
        assert not hasattr(rec, field), field
    # TestPlan has no prose/verdict/permission field.
    assert not hasattr(plan, "verdict")
    assert not hasattr(plan, "permissions")


def test_j_agent_cannot_mark_evidence_reusable():
    # Reusability is derived (exact identity + valid MAC), never a settable flag.
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    rec = pass_record("tests/a.py", snap(), plan)
    assert not hasattr(rec, "reusable")
    st = store()
    st.add(rec)
    # A foreign-identity record is not reusable regardless of any flag.
    foreign = te.replace(st.records()[0], selector="tests/other.py")
    with pytest.raises(ValueError):
        store().add(foreign)


def test_j_agent_cannot_force_done():
    # There is no API to construct an authoritative DONE verdict; the verdict
    # is derived from the staged execution only.
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    rep = exec_plan(plan, FakeRunner(), snapshot=snap())
    assert rep.verdict.name == "DONE"
    # ExecutionReport is frozen and provides no setter/mutator.
    for mut in ("set_verdict", "mark_done", "force_done"):
        assert not hasattr(rep, mut), mut


# ---------------------------------------------------------------------------
# K — resource / context / routing independence
# ---------------------------------------------------------------------------


def test_k_reuse_grants_no_permissions():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    st = store()
    st.add(pass_record("tests/a.py", snap(), plan))
    rep = exec_plan(plan, FakeRunner(), snapshot=snap(), store=st)
    sr = rep.stages[0].selector_results[0]
    assert sr.reused is True
    assert sr.classification == ResultClass.TEST_PASS
    assert not hasattr(sr, "permissions") and not hasattr(rep, "permissions")


def test_k_plan_expands_no_provider_permissions():
    plan = real_plan("argent_core/model_router.py")
    assert plan.full_suite_required
    for attr in ("provider_permissions", "model", "tool_grants", "permissions"):
        assert not hasattr(plan, attr), attr


def test_k_test_failure_does_not_escalate_model():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    rep = exec_plan(plan, FakeRunner({"tests/a.py": ResultClass.TEST_FAILURE}), snapshot=snap())
    assert rep.verdict.name == "FAILED"
    assert not hasattr(rep, "model") and not hasattr(rep, "provider")


# ---------------------------------------------------------------------------
# H — failure classification is stable and only TEST_PASS satisfies
# ---------------------------------------------------------------------------


def test_h_classification_is_stable_and_bounded():
    c = PytestRunner.classify
    assert c(0, b"1 passed", b"") == ResultClass.TEST_PASS
    assert c(1, b"1 failed", b"") == ResultClass.TEST_FAILURE
    assert c(1, b"1 error in 0.1s", b"") == ResultClass.TEST_INFRA_FAILURE
    assert c(2, b"", b"") == ResultClass.CANCELLED_BLOCKED
    assert c(3, b"", b"") == ResultClass.TEST_INFRA_FAILURE
    assert c(4, b"", b"") == ResultClass.TEST_INFRA_FAILURE
    assert c(5, b"", b"") == ResultClass.TEST_INFRA_FAILURE
    assert c(124, b"command timed out", b"") == ResultClass.TIMEOUT
    assert c(124, b"", b"") == ResultClass.UNKNOWN
    assert c(137, b"", b"") == ResultClass.UNKNOWN
    assert c(137, b"", b"", scope_evidence=True) == ResultClass.RESOURCE_FAILURE
    assert c(99, b"", b"") == ResultClass.UNKNOWN


def test_h_only_test_pass_satisfies():
    # Every non-PASS classification keeps the verdict away from DONE.
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    for cls in (
        ResultClass.TEST_FAILURE,
        ResultClass.TEST_INFRA_FAILURE,
        ResultClass.RESOURCE_FAILURE,
        ResultClass.PROCESS_FAILURE,
        ResultClass.TIMEOUT,
        ResultClass.UNKNOWN,
        ResultClass.CANCELLED_BLOCKED,
    ):
        rep = exec_plan(plan, FakeRunner({"tests/a.py": cls}), snapshot=snap())
        assert rep.verdict.name in ("FAILED", "BLOCKED"), cls


def test_h_resource_failure_is_not_code_failure():
    plan = mk_plan([stage("targeted", ["tests/a.py"])])
    gate = FakeGate(allowed=False, reason="disk low")
    rep = exec_plan(plan, FakeRunner(), resource_gate=gate, snapshot=snap())
    assert rep.first_failure_class == ResultClass.RESOURCE_FAILURE
    # Distinct from a genuine TEST_FAILURE.
    rep2 = exec_plan(plan, FakeRunner({"tests/a.py": ResultClass.TEST_FAILURE}), snapshot=snap())
    assert rep2.first_failure_class == ResultClass.TEST_FAILURE
    assert rep.verdict != rep2.verdict or rep.first_failure_class != rep2.first_failure_class


def test_h_no_shell_argv_list():
    captured = {}

    def fake_run(argv, timeout=None, capture_output=None, shell=None, cwd=None):
        captured["argv"] = argv
        captured["shell"] = shell
        return FakeProc(0, b"1 passed in 0.1s", b"")

    r = PytestRunner(runner_fn=fake_run, python="python3")
    r.run("tests/test_phase_f1*.py")
    assert captured["shell"] is False
    assert isinstance(captured["argv"], list)
    assert captured["argv"][1:3] == ["-m", "pytest"]
