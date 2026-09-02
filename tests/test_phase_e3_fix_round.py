"""Phase E3 fix-round (F1-F4) regression tests (deterministic, offline).

Each test pins the CORRECTED semantics of one supervisor finding:

* F1 — ``_provider_unavailable_backoff`` must transition ``primary_state``
  (RUNNING -> QUEUED) AND release the lease atomically, so a leased job is
  never stranded as RUNNING + owner-NULL + lease-NULL (unclaimable corpse).
* F2 — a real controller-authenticated provider-failure producer persists
  ``ATTEMPT_OUTCOME_PROVIDER``; the bounded availability snapshot reads it,
  the router falls back, and expiry lifts the mark.
* F3 — provenance is bound to the EXACT document content (content digests) and
  the full inputs canon; a real content change changes ``decision_id``; a
  tampered (but well-formed) ``inputs_hash`` is rejected by the Core.
* F4 — the evidence registry is fail-closed on duplicate JSON keys and on a
  mismatched entry ``version``.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path

import pytest

from argent_core import Role, RolePolicyViolation
from argent_core import evidence_registry as er
from argent_core import model_router as mr
from argent_core.model_registry import ModelRegistry, reset_default_registry
from argent_core.models import DispatchStatus

from test_phase2c_supervisor import advance, bind_running  # noqa: E402
from test_phase_e2_integration import (  # noqa: E402
    drive_to_implementer_started,
    make_env_low_risk,
)
from mock_supervisor_runtime import (  # noqa: E402
    canonical_binding,
    make_run_observation,
)
from argent_core.supervisor import ReconcileAction, RunStatus  # noqa: E402

_ISO = "2026-01-01T00:00:00+00:00"


def _bind_implementer(env):
    """Drive to a bound RUNNING implementer dispatch (flash at LOW risk)."""
    drive_to_implementer_started(env)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    assert d.role is Role.IMPLEMENTER
    provider, model, thinking, session = canonical_binding(d)
    run_id = f"run-{d.id[:8]}"
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    advance(env, ReconcileAction.BIND_RUN)
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING
    return d


# ---------------------------------------------------------------------------
# F1 — NO_VALID_FALLBACK backoff must requeue + release the lease
# ---------------------------------------------------------------------------

def test_f1_provider_backoff_requeues_leased_job(db_path):
    from test_phase2c_supervisor import make_env

    env = make_env(db_path)
    jid = env.job.supervisor_job_id
    claimed = env.core._store.claim_job(jid, owner_instance_id="inst-1",
                                        ttl_seconds=300)
    env.sup.set_lease_owner("inst-1", claimed["lease_epoch"])
    job = env.core._store.get_supervisor_job(jid)
    assert job["primary_state"] == "RUNNING"
    assert job["owner_instance_id"] == "inst-1"

    out = env.sup._provider_unavailable_backoff(job)
    assert out.status == "wait"

    cur = env.core._store.get_supervisor_job(jid)
    assert cur["primary_state"] == "QUEUED"
    assert cur["status"] == "BACKOFF"
    assert cur["queue_reason"] == "RETRY_BACKOFF"
    assert cur["owner_instance_id"] is None
    assert cur["lease_expires_at"] is None
    assert cur["next_eligible_at"] is not None

    # No RUNNING stranding: after next_eligible_at the job is claimable again.
    env.clock.advance(3600)
    cur2 = env.core._store.get_supervisor_job(jid)
    claimable, reason = env.core._store._job_is_claimable(
        cur2, env.core._store.now_iso())
    assert claimable, reason


def test_f1_provider_backoff_persistent_error_branch(db_path):
    from argent_core import job_state
    from test_phase2c_supervisor import make_env

    env = make_env(db_path)
    jid = env.job.supervisor_job_id
    claimed = env.core._store.claim_job(jid, owner_instance_id="inst-1",
                                        ttl_seconds=300)
    env.sup.set_lease_owner("inst-1", claimed["lease_epoch"])
    # Push retry_count to the sticky-ERROR threshold (MAX_RUNTIME_UNKNOWN = 5).
    env.core._store._update_supervisor_job(
        jid, retry_count=4, facts_version=claimed["facts_version"] + 1)
    job = env.core._store.get_supervisor_job(jid)
    out = env.sup._provider_unavailable_backoff(job)
    assert out.status == "failed"

    cur = env.core._store.get_supervisor_job(jid)
    assert cur["status"] == "ERROR"
    assert cur["recovery_state"] == "PERSISTENT_ERROR"


# ---------------------------------------------------------------------------
# F2 — real provider-failure producer + bounded availability snapshot
# ---------------------------------------------------------------------------

def test_f2_real_provider_producer_and_fallback(db_path):
    env = make_env_low_risk(db_path)
    # Drive to a bound RUNNING implementer dispatch (flash at LOW risk).
    d = _bind_implementer(env)
    assert d.expected_model_class == "deepseek-v4-flash"

    # REAL producer (controller-authenticated Core API, no DB injection): a
    # provider failure persists ATTEMPT_OUTCOME_PROVIDER (never CAPABILITY).
    env.core.mark_agent_failed(
        d.id, "provider_unavailable", env.sup.controller_source,
        error_code="PROVIDER_UNAVAILABLE",
    )
    dd = env.core.queries.get_dispatch(d.id)
    assert dd.status is DispatchStatus.FAILED
    assert dd.attempt_outcome == "PROVIDER"

    # The snapshot builder reads the persisted PROVIDER outcome.
    snap = env.sup._build_availability_snapshot(env.task.id)
    assert snap.model_states["deepseek-v4-flash"] == "UNAVAILABLE"

    # The next routing request falls back to pro (same escalation level 0).
    f = env.core.workflow_frontier(env.task.id, env.sup.controller_source)
    job = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    req = env.sup._build_routing_request(
        job, env.task.id, Role.IMPLEMENTER, f.cycle_no, f.position, 2,
    )
    rd = env.sup._routing_engine().route(req, now_iso=_ISO)
    assert rd.model == "deepseek-v4-pro"
    assert rd.decision_reason_code == "VALIDATED_FALLBACK"
    assert rd.escalation_level == 0

    # Recovery: the bounded TTL expires -> flash is selectable again.
    env.clock.advance(3600)  # > AVAILABILITY_OBSERVATION_TTL_SECONDS (1800)
    snap2 = env.sup._build_availability_snapshot(env.task.id)
    assert "deepseek-v4-flash" not in snap2.model_states


def test_f2_snapshot_latest_success_lifts_unavailable(db_path):
    """A later SUCCESS observation for a model lifts an earlier PROVIDER mark."""
    env = make_env_low_risk(db_path)
    drive_to_implementer_started(env)
    f = env.core.workflow_frontier(env.task.id, env.sup.controller_source)
    # Direct-insert is a *unit* fixture for the bounded snapshot builder's
    # "latest outcome wins" rule (the real-path producer is covered above).
    from test_phase_e3_integration import _inject_dispatch
    _inject_dispatch(
        env, role=Role.IMPLEMENTER, attempt_no=1,
        model="deepseek-v4-flash", provider="deepseek", thinking="medium",
        status=DispatchStatus.FAILED, escalation_level=0,
        cycle_no=f.cycle_no, position=f.position,
        sequence_kind=f.sequence_kind, attempt_outcome="PROVIDER",
    )
    # Later (attempt_no=2) SUCCESS for the same model -> not UNAVAILABLE.
    _inject_dispatch(
        env, role=Role.IMPLEMENTER, attempt_no=2,
        model="deepseek-v4-flash", provider="deepseek", thinking="medium",
        status=DispatchStatus.CONSUMED, escalation_level=0,
        cycle_no=f.cycle_no, position=f.position,
        sequence_kind=f.sequence_kind, attempt_outcome="SUCCESS",
    )
    snap = env.sup._build_availability_snapshot(env.task.id)
    assert snap.model_states.get("deepseek-v4-flash") != "UNAVAILABLE"


def test_f2_registry_unavailable_triggers_fallback_not_no_eligible():
    """Registry-UNAVAILABLE provider is the baseline, not a pre-filter (F2(b))."""
    base = Path(mr.__file__).resolve().parent / "registry"
    providers = json.loads((base / "providers.json").read_text())["providers"]
    models = json.loads((base / "models.json").read_text())["models"]
    for p in providers:
        if p["provider_id"] == "deepseek":
            p["availability_state"] = "UNAVAILABLE"
    reg = ModelRegistry.from_payload(providers, models)

    router = mr.ModelRouter(registry=reg, policy=mr.get_default_policy())
    d = router.route(
        mr.RoutingRequest(job_id="j", task_id="t",
                          role=Role.IMPLEMENTER.value, risk_class="LOW"),
        now_iso=_ISO,
    )
    # flash+pro are deepseek (registry-UNAVAILABLE); sol is below the
    # implementer floor -> NO_VALID_FALLBACK (never a pre-filtered
    # NO_ELIGIBLE_CANDIDATE, never a weaker substitute).
    assert d.is_terminal
    assert d.decision_reason_code == "NO_VALID_FALLBACK"


# ---------------------------------------------------------------------------
# F3 — provenance bound to content + full inputs canon
# ---------------------------------------------------------------------------

def test_f3_content_change_changes_decision_id():
    base = Path(mr.__file__).resolve().parent / "registry" / "routing_policy_v1.json"
    doc1 = json.loads(base.read_text())
    doc2 = copy.deepcopy(doc1)
    # A content-only change that does NOT alter the routing outcome (a cosmetic
    # level_names label) — the content digest must still change the provenance.
    doc2["escalation"]["level_names"]["0"] = "ROUTINE-X"
    pol1 = mr.RoutingPolicy(doc1)
    pol2 = mr.RoutingPolicy(doc2)
    assert pol1.content_hash != pol2.content_hash
    assert pol1.version == pol2.version == "2"

    d1 = mr.ModelRouter(policy=pol1).route(
        mr.RoutingRequest(job_id="j", task_id="t", role=Role.LEAD.value),
        now_iso=_ISO,
    )
    d2 = mr.ModelRouter(policy=pol2).route(
        mr.RoutingRequest(job_id="j", task_id="t", role=Role.LEAD.value),
        now_iso=_ISO,
    )
    assert d1.model == d2.model  # same outcome
    assert d1.policy_hash != d2.policy_hash
    assert d1.inputs_hash != d2.inputs_hash
    assert d1.decision_id != d2.decision_id
    assert d1.sha256 != d2.sha256


def test_f3_inputs_hash_changes_with_evidence():
    router = mr.ModelRouter()
    d1 = router.route(
        mr.RoutingRequest(job_id="j", task_id="t", role=Role.IMPLEMENTER.value),
        now_iso=_ISO,
    )
    ev = mr.RoutingEvidence(prior_attempts=(
        mr.AttemptEvidence(attempt_no=1, model_id="deepseek-v4-flash",
                           reasoning_level="MEDIUM", outcome_class="EXTERNAL",
                           status="FAILED"),
    ))
    d2 = router.route(
        mr.RoutingRequest(job_id="j", task_id="t", role=Role.IMPLEMENTER.value,
                          evidence=ev),
        now_iso=_ISO,
    )
    assert d1.inputs_hash != d2.inputs_hash


def test_f3_adversarial_tampered_inputs_hash_rejected():
    from argent_core import core as core_mod

    d = mr.ModelRouter().route(
        mr.RoutingRequest(job_id="j", task_id="t", role=Role.LEAD.value),
        now_iso=_ISO,
    )
    # Well-formed 64-hex but WRONG content -> the Core must reject it.
    tampered = dataclasses.replace(d, inputs_hash="a" * 64)
    with pytest.raises(RolePolicyViolation):
        core_mod._validate_routing_decision(tampered, "t", Role.LEAD)


def test_f3_decision_binds_all_fields(db_path):
    from argent_core import Core
    from test_phase_e2_integration import make_env_low_risk

    env = make_env_low_risk(db_path)
    drive_to_implementer_started(env)
    f = env.core.workflow_frontier(env.task.id, env.sup.controller_source)
    job = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    req = env.sup._build_routing_request(
        job, env.task.id, Role.IMPLEMENTER, f.cycle_no, f.position, 1,
    )
    d = env.sup._routing_engine().route(req, now_iso=_ISO)
    disp = env.core.create_dispatch(
        env.task.id, env.task_run.id, Role.IMPLEMENTER, f.position, f.cycle_no,
        f.sequence_kind, None, env.sup.controller_source, routing_decision=d,
    )
    rd = env.core._store.get_routing_decision(d.decision_id)
    assert rd is not None
    assert rd["policy_hash"] == d.policy_hash
    assert rd["registry_hash"] == d.registry_hash
    assert rd["evidence_hash"] == d.evidence_hash
    assert rd["policy_version"] == "2"
    # Restart over the same DB preserves the full provenance (CASE 21).
    env.core.close()
    core2 = Core(db_path)
    try:
        rd2 = core2._store.get_routing_decision(d.decision_id)
        assert rd2 is not None
        assert rd2["policy_hash"] == d.policy_hash
        assert rd2["inputs_hash"] == d.inputs_hash
    finally:
        core2.close()


# ---------------------------------------------------------------------------
# F4 — evidence registry fail-closed on duplicate keys / mismatched version
# ---------------------------------------------------------------------------

def test_f4_evidence_rejects_duplicate_document_key(tmp_path):
    p = tmp_path / "benchmarks_v1.json"
    p.write_text('{"evidence_version":"1","evidence_version":"1","models":[]}')
    with pytest.raises(er.ModelRegistryError):
        er.EvidenceRegistry.load_files(str(tmp_path))


def test_f4_evidence_rejects_duplicate_entry_key(tmp_path):
    p = tmp_path / "benchmarks_v1.json"
    p.write_text(
        '{"evidence_version":"1","models":[{"model_id":"deepseek-v4-pro",'
        '"categories":{"repository_coding":{"status":"PROVISIONAL",'
        '"status":"PROVISIONAL","evidence_ref":"routing.py: x",'
        '"version":"1","benchmarked":false}}}]}'
    )
    with pytest.raises(er.ModelRegistryError):
        er.EvidenceRegistry.load_files(str(tmp_path))


def test_f4_evidence_rejects_mismatched_entry_version():
    payload = [{
        "model_id": "deepseek-v4-pro",
        "categories": {
            "repository_coding": {
                "status": "PROVISIONAL", "evidence_ref": "routing.py: x",
                "version": "banana", "benchmarked": False,
            },
        },
    }]
    with pytest.raises(er.ModelRegistryError):
        er.EvidenceRegistry.from_payload(payload)


def test_f4_evidence_valid_file_still_loads():
    reset_default_registry()
    er.reset_default_evidence_registry()
    ev = er.get_default_evidence_registry()
    assert ev.version == "1"
    assert len(ev.content_hash) == 64
