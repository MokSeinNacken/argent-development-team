"""Phase E2 — fix-round tests (F1–F6).

Deterministic, offline.  These pin the fixes applied after the independent
Sol closing review:

* F1 — closing-review independence is ALWAYS enforced (fail-closed when the
  task has no valid writer reference; never a same-model fallback).
* F2 — agent output is never escalation authority (canonical reviewer verdicts,
  controller-only test evidence + findings, provenance separation).
* F3 — role-appropriate escalation (analyst on HIGH risk reaches Sol via deep
  analysis; the security-review REPLACE is reviewer-scoped).
* F4 — attempt outcomes are persisted at completion (no retroactive
  reinterpretation) and CONTRADICTORY_EVIDENCE is current-cycle only.
* F5 — the routing policy is strictly validated (duplicates, allow-lists,
  bootstrap flags, registry cross-validation, monotonic tiers, floor/ceiling).
* F6 — the decision audit is fully context-bound (task_id + evidence in the
  canonical payload; conflict/consistency checks in Core.create_dispatch).
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from argent_core import OWNER_SOURCE, Role, role_source  # noqa: E402
from argent_core import model_router as mr  # noqa: E402
from argent_core import model_registry  # noqa: E402
from argent_core.core import (  # noqa: E402
    CANONICAL_VERDICT_APPROVE,
    CANONICAL_VERDICT_REJECT,
    _canonical_review_verdict,
)
from argent_core.models import (  # noqa: E402
    AgentDispatch,
    DispatchStatus,
    Finding,
    FindingStatus,
    RiskClass,
    RolePolicyViolation,
    SequenceKind,
    TestResult as _TestResult,
    TestRun as _TestRun,
)
from argent_core.supervisor import ReconcileAction  # noqa: E402

from mock_supervisor_runtime import FakeClock  # noqa: E402
from test_phase2c_supervisor import advance, drive_frontier, make_env  # noqa: E402
from test_phase_e2_integration import (  # noqa: E402
    _inject_dispatch,
    _inject_finding,
    _inject_test_run,
    drive_to_implementer_started,
)

LEAD = role_source(Role.LEAD)


def make_env_high_risk(db_path):
    """Like ``make_env`` but with a HIGH-risk task."""
    clock = FakeClock()
    from argent_core import Core
    from argent_core.resource_governor import (
        AdmissionDecision, AdmissionVerdict, ResourceReasonCode,
    )
    from argent_core.resource_policy import ResourceClass, ResourcePolicy
    from argent_core.scope_enforcer import ExecutionEnforcer
    from argent_core.supervisor import Supervisor
    from c2_helpers import FakeGovernor, FakeScopeBackend, FakeSnapshotProvider
    from mock_supervisor_runtime import FakeRunLauncher, FakeRunStatusProvider

    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER_SOURCE)
    task = core.create_task(project.id, "t", OWNER_SOURCE, risk_class=RiskClass.HIGH)
    task_run = core.start_task_run(task.id, OWNER_SOURCE)
    prov = FakeRunStatusProvider()
    launch = FakeRunLauncher()
    pol = ResourcePolicy()
    b = pol.limits_for(ResourceClass.LIGHT)
    limits = {
        "memory_high_bytes": b.memory_high_bytes,
        "memory_max_bytes": b.memory_max_bytes,
        "swap_max_bytes": b.swap_max_bytes,
        "cpu_quota_percent": b.cpu_quota_percent,
        "timeout_seconds": b.timeout_seconds,
    }
    adm = AdmissionDecision(
        resource_class=ResourceClass.LIGHT.value, policy_version="1",
        snapshot_ref="snap-1", decision=AdmissionVerdict.ALLOW.value,
        reason_code=ResourceReasonCode.OK.value, next_eligible_at=None,
        effective_limits=limits, timestamp="2026-09-01T00:00:00+00:00",
    )
    sup = Supervisor(
        core, prov, launch, clock=clock,
        enforcer=ExecutionEnforcer(FakeScopeBackend()),
        resource_governor=FakeGovernor(adm),
        snapshot_provider=FakeSnapshotProvider(),
    )
    job = sup.store.create_job(task.id, idempotency_key="job-1")
    from types import SimpleNamespace
    return SimpleNamespace(
        core=core, task=task, task_run=task_run, prov=prov, launch=launch,
        sup=sup, job=job, clock=clock,
    )


def _route_req(env, role, cycle, pos, attempt):
    job = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    req = env.sup._build_routing_request(
        job, env.task.id, role, cycle, pos, attempt,
    )
    return req, env.sup._routing_engine().route(req, now_iso="2026-01-01T00:00:00+00:00")


# ---------------------------------------------------------------------------
# F1 — closing-review independence is ALWAYS enforced
# ---------------------------------------------------------------------------

def test_f1_reviewer_without_writer_reference_is_terminal(db_path):
    env = make_env(db_path)
    drive_to_implementer_started(env)
    # No writer_dispatch_id on the job -> the reviewer request carries the hard
    # DIFFERENT_MODEL_REQUIRED constraint with no reference -> fail-closed.
    req, d = _route_req(env, Role.REVIEWER, 1, 5, 1)
    assert req.independence_requirement == "DIFFERENT_MODEL_REQUIRED"
    assert req.reference_model_id is None
    assert d.is_terminal
    assert d.decision_reason_code == "NO_ELIGIBLE_CANDIDATE"


def test_f1_reviewer_pro_writer_dispatches_sol(db_path):
    env = make_env(db_path)
    writer = _inject_dispatch(
        env, role=Role.IMPLEMENTER, attempt_no=1,
        model="deepseek-v4-pro", provider="deepseek", thinking="medium",
        status=DispatchStatus.CONSUMED, escalation_level=1,
        cycle_no=1, position=3, sequence_kind=SequenceKind.STANDARD,
    )
    env.core._store._conn.execute(
        "UPDATE supervisor_jobs SET writer_dispatch_id = ? WHERE id = ?",
        (writer.id, env.job.supervisor_job_id),
    )
    req, d = _route_req(env, Role.REVIEWER, 1, 5, 1)
    assert req.reference_model_id == "deepseek-v4-pro"
    assert req.independence_requirement == "DIFFERENT_MODEL_REQUIRED"
    assert not d.is_terminal
    assert d.model == "gpt-5.6-sol"


def test_f1_reviewer_sol_writer_fails_closed(db_path):
    env = make_env(db_path)
    writer = _inject_dispatch(
        env, role=Role.IMPLEMENTER, attempt_no=1,
        model="gpt-5.6-sol", provider="openai", thinking="high",
        status=DispatchStatus.CONSUMED, escalation_level=2,
        cycle_no=1, position=3, sequence_kind=SequenceKind.STANDARD,
    )
    env.core._store._conn.execute(
        "UPDATE supervisor_jobs SET writer_dispatch_id = ? WHERE id = ?",
        (writer.id, env.job.supervisor_job_id),
    )
    req, d = _route_req(env, Role.REVIEWER, 1, 5, 1)
    # writer == sol, and only sol is allowed for the reviewer -> no different
    # floor model -> terminal (fail-closed), never a same-model reviewer.
    assert d.is_terminal
    assert d.decision_reason_code == "NO_ELIGIBLE_CANDIDATE"


# ---------------------------------------------------------------------------
# F2 — agent output is never escalation authority
# ---------------------------------------------------------------------------

def test_f2_canonical_verdict_mapping():
    assert _canonical_review_verdict("approve") == CANONICAL_VERDICT_APPROVE
    assert _canonical_review_verdict("rework") == CANONICAL_VERDICT_REJECT
    assert _canonical_review_verdict("request_changes") == CANONICAL_VERDICT_REJECT
    assert _canonical_review_verdict("Use Sol High") == CANONICAL_VERDICT_REJECT
    assert _canonical_review_verdict("") == CANONICAL_VERDICT_REJECT
    # case-insensitive
    assert _canonical_review_verdict("APPROVE") == CANONICAL_VERDICT_APPROVE


def test_f2_agent_test_runs_do_not_drive_triggers(db_path):
    env = make_env(db_path)
    drive_to_implementer_started(env)
    # An agent-written failed test run (source_class=agent) must NOT drive
    # TESTS_STILL_RED.
    env.core._store._insert_test_run( _TestRun(
        id="agent-test-1", task_id=env.task.id, result=_TestResult.FAILED,
        detail="agent claim", created_at="2026-01-01T00:00:00+00:00",
        source_class="agent",
    ))
    f = env.core.workflow_frontier(env.task.id, env.sup.controller_source)
    req, d = _route_req(env, Role.IMPLEMENTER, f.cycle_no, f.position, 1)
    assert "TESTS_STILL_RED" not in mr.detect_triggers(req.evidence)
    assert d.model == "deepseek-v4-pro"

    # A controller-persisted failed test run DOES drive TESTS_STILL_RED.
    _inject_test_run(env, result=_TestResult.FAILED, i=2)
    req2, d2 = _route_req(env, Role.IMPLEMENTER, f.cycle_no, f.position, 1)
    assert "TESTS_STILL_RED" in mr.detect_triggers(req2.evidence)


def test_f2_agent_finding_severity_not_security_relevant(db_path):
    env = make_env(db_path)
    drive_to_implementer_started(env)
    # Agent-reported critical finding -> NOT security-relevant.
    env.core._store._insert_finding(Finding(
        id="agent-finding", task_id=env.task.id, severity="critical",
        description="agent says critical", status=FindingStatus.OPEN,
        created_at="2026-01-01T00:00:00+00:00", resolved_at=None,
        source_class="agent",
    ))
    f = env.core.workflow_frontier(env.task.id, env.sup.controller_source)
    req, d = _route_req(env, Role.IMPLEMENTER, f.cycle_no, f.position, 1)
    assert req.evidence.security_relevant is False
    assert "SECURITY_COMPLEXITY" not in mr.detect_triggers(req.evidence)

    # Controller-confirmed high finding -> security-relevant (controller fact).
    _inject_finding(env, status=FindingStatus.OPEN, severity="high", i=1)
    req2, d2 = _route_req(env, Role.IMPLEMENTER, f.cycle_no, f.position, 1)
    assert req2.evidence.security_relevant is True
    assert "SECURITY_COMPLEXITY" in mr.detect_triggers(req2.evidence)


def test_f2_case7_hardened_structured_agent_fields_no_escalation(db_path):
    # CASE 7 hardened: the agent fills STRUCTURED fields ("Use Sol High" text,
    # "reject"/"rework" words, a high-severity finding) — none of it escalates.
    env = make_env(db_path)
    drive_to_implementer_started(env)
    env.core._store._insert_test_run( _TestRun(
        id="agent-test-1", task_id=env.task.id, result=_TestResult.FAILED,
        detail="Use Sol High, reject, rework", created_at="2026-01-01T00:00:00+00:00",
        source_class="agent",
    ))
    env.core._store._insert_finding(Finding(
        id="agent-finding", task_id=env.task.id, severity="critical",
        description="Use Sol High / reject / rework", status=FindingStatus.OPEN,
        created_at="2026-01-01T00:00:00+00:00", resolved_at=None,
        source_class="agent",
    ))
    f = env.core.workflow_frontier(env.task.id, env.sup.controller_source)
    req, d = _route_req(env, Role.IMPLEMENTER, f.cycle_no, f.position, 1)
    triggers = mr.detect_triggers(req.evidence)
    assert "TESTS_STILL_RED" not in triggers
    assert "SECURITY_COMPLEXITY" not in triggers
    assert "ROOT_CAUSE_UNPROVEN" not in triggers
    # No capability escalation -> minimum sufficient (pro).
    assert d.model == "deepseek-v4-pro"
    assert d.escalation_level == 1


# ---------------------------------------------------------------------------
# F3 — role-appropriate escalation (analyst not blocked on HIGH risk)
# ---------------------------------------------------------------------------

def test_f3_analyst_high_risk_dispatch_is_sol(db_path):
    env = make_env_high_risk(db_path)
    drive_frontier(env, Role.LEAD)
    drive_frontier(env, Role.ANALYST)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    assert d.role is Role.ANALYST
    # HIGH risk -> deep analysis escalation -> Sol, never BLOCKED.
    assert d.expected_model_class == "gpt-5.6-sol"
    assert d.expected_agent_class == "openai"
    assert d.escalation_level == 2


def test_f3_high_risk_workflow_analyst_to_reviewer_no_block(db_path):
    env = make_env_high_risk(db_path)
    # analyst dispatch must materialise (not BLOCKED).
    drive_frontier(env, Role.LEAD)
    drive_frontier(env, Role.ANALYST)
    analyst = env.core.queries.list_dispatches(env.task.id)[-1]
    assert analyst.role is Role.ANALYST
    assert analyst.expected_model_class == "gpt-5.6-sol"
    # continue: lead decision -> implementer dispatch stays pro (no BLOCKED).
    drive_frontier(env, Role.LEAD)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    imp = env.core.queries.list_dispatches(env.task.id)[-1]
    assert imp.role is Role.IMPLEMENTER
    # The implementer stays on pro (implementation capability, never SECURITY_REVIEW).
    assert imp.expected_model_class == "deepseek-v4-pro"
    # reviewer (writer=pro) dispatches Sol -> no BLOCKED.
    env.core._store._conn.execute(
        "UPDATE supervisor_jobs SET writer_dispatch_id = ? WHERE id = ?",
        (imp.id, env.job.supervisor_job_id),
    )
    req, d = _route_req(env, Role.REVIEWER, imp.cycle_no, 5, 1)
    assert not d.is_terminal
    assert d.model == "gpt-5.6-sol"


# ---------------------------------------------------------------------------
# F4 — persisted attempt outcomes + current-cycle contradiction
# ---------------------------------------------------------------------------

def test_f4_persisted_outcome_no_retroactive_capability(db_path):
    env = make_env(db_path)
    drive_to_implementer_started(env)
    f = env.core.workflow_frontier(env.task.id, env.sup.controller_source)
    # attempt 1 passed its tests (SUCCESS persisted at completion), attempt 2
    # failed (CAPABILITY persisted).  A later red test must NOT retroactively
    # turn attempt 1 into a capability failure.
    d1 = _inject_dispatch(
        env, role=Role.IMPLEMENTER, attempt_no=1,
        model="deepseek-v4-pro", provider="deepseek", thinking="medium",
        status=DispatchStatus.CONSUMED, escalation_level=1,
        cycle_no=f.cycle_no, position=f.position, sequence_kind=f.sequence_kind,
    )
    d2 = _inject_dispatch(
        env, role=Role.IMPLEMENTER, attempt_no=2,
        model="deepseek-v4-pro", provider="deepseek", thinking="medium",
        status=DispatchStatus.CONSUMED, escalation_level=1,
        cycle_no=f.cycle_no, position=f.position, sequence_kind=f.sequence_kind,
    )
    env.core._store._set_dispatch_attempt_outcome(d1.id, "SUCCESS")
    env.core._store._set_dispatch_attempt_outcome(d2.id, "CAPABILITY")
    _inject_test_run(env, result=_TestResult.FAILED, i=1)
    req, d = _route_req(env, Role.IMPLEMENTER, f.cycle_no, f.position, 3)
    outcomes = {a.attempt_no: a.outcome_class for a in req.evidence.prior_attempts}
    assert outcomes == {1: "SUCCESS", 2: "CAPABILITY"}
    # exactly one capability failure -> no REPEATED_FIX_FAILURE.
    assert "REPEATED_FIX_FAILURE" not in mr.detect_triggers(req.evidence)


def test_f4_contradictory_evidence_not_sticky():
    # Old reject superseded by a later approve must NOT stick as contradictory.
    ev = mr.RoutingEvidence(reviewer_verdicts=("reject", "approve"))
    assert "CONTRADICTORY_EVIDENCE" not in mr.detect_triggers(ev)
    # A pass superseded by a later pass is not contradictory either.
    ev2 = mr.RoutingEvidence(test_results=("failed", "passed"))
    assert "CONTRADICTORY_EVIDENCE" not in mr.detect_triggers(ev2)
    # A genuine current-cycle flip (approve -> reject) IS contradictory.
    ev3 = mr.RoutingEvidence(reviewer_verdicts=("approve", "reject"))
    assert "CONTRADICTORY_EVIDENCE" in mr.detect_triggers(ev3)
    # A genuine test regression (pass -> fail) IS contradictory.
    ev4 = mr.RoutingEvidence(test_results=("passed", "failed"))
    assert "CONTRADICTORY_EVIDENCE" in mr.detect_triggers(ev4)


# ---------------------------------------------------------------------------
# F5 — strict routing-policy validation
# ---------------------------------------------------------------------------

def _policy_doc():
    real = Path(mr.__file__).resolve().parent / "registry" / "routing_policy_v1.json"
    return json.loads(real.read_text())


def test_f5_duplicate_json_key_rejected(tmp_path):
    p = tmp_path / "routing_policy_v1.json"
    p.write_text('{"policy_version":"1","policy_version":"1",'
                 '"bootstrap":true,"benchmark_required_for_new_models":true}')
    with pytest.raises(mr.RoutingError) as exc:
        mr.load_routing_policy(str(tmp_path))
    assert exc.value.code == mr.ROUTING_POLICY_INVALID


def test_f5_bootstrap_false_rejected():
    doc = _policy_doc()
    doc["bootstrap"] = False
    with pytest.raises(mr.RoutingError):
        mr.RoutingPolicy(doc)


def test_f5_benchmark_flag_false_rejected():
    doc = _policy_doc()
    doc["benchmark_required_for_new_models"] = False
    with pytest.raises(mr.RoutingError):
        mr.RoutingPolicy(doc)


def test_f5_unknown_reasoning_key_rejected():
    doc = _policy_doc()
    doc["reasoning"]["totally_unknown"] = {}
    with pytest.raises(mr.RoutingError):
        mr.RoutingPolicy(doc)


def test_f5_extra_level_key_rejected():
    doc = _policy_doc()
    doc["reasoning"]["level_defaults"]["99"] = "HIGH"
    with pytest.raises(mr.RoutingError):
        mr.RoutingPolicy(doc)


def test_f5_invalid_role_rejected():
    doc = _policy_doc()
    doc["profiles"]["analyst"]["roles"] = ["not-a-role"]
    with pytest.raises(mr.RoutingError):
        mr.RoutingPolicy(doc)


def test_f5_duplicate_allowed_models_rejected():
    doc = _policy_doc()
    doc["profiles"]["implementer"]["allowed_models"] = [
        "deepseek-v4-pro", "deepseek-v4-pro",
    ]
    with pytest.raises(mr.RoutingError):
        mr.RoutingPolicy(doc)


def test_f5_non_monotonic_tiers_rejected():
    doc = _policy_doc()
    doc["model_tiers"] = {"deepseek-v4-flash": 0, "deepseek-v4-pro": 2}
    with pytest.raises(mr.RoutingError):
        mr.RoutingPolicy(doc)


def test_f5_profile_floor_above_ceiling_rejected():
    doc = _policy_doc()
    # analyst entry_level 1 has a MEDIUM ceiling; force a HIGH floor.
    doc["profiles"]["analyst"]["minimum_reasoning_level"] = "HIGH"
    with pytest.raises(mr.RoutingError):
        mr.RoutingPolicy(doc)


def test_f5_unknown_model_reference_rejected():
    doc = _policy_doc()
    # A profile allowing a model absent from model_tiers is refused.
    doc["profiles"]["analyst"]["allowed_models"] = ["ghost-model"]
    with pytest.raises(mr.RoutingError):
        mr.RoutingPolicy(doc)


def test_f5_registry_cross_validation():
    # A policy referencing a registry-unknown model fails validate_models.
    reg = model_registry.ModelRegistry.from_payload(
        providers=[{
            "provider_id": "deepseek", "provider_type": "openai-completions",
            "display_name": "DeepSeek", "enabled": True,
            "availability_state": "AVAILABLE",
            "capabilities_supported": ["CODE_IMPLEMENTATION"],
            "credential_ref": "openclaw:provider:deepseek",
            "auth_mode": "api-key", "endpoint_ref": "https://api.deepseek.com",
            "profile_ref": None, "policy_version": "1",
        }],
        models=[{
            "model_id": "deepseek-v4-pro", "provider_id": "deepseek",
            "canonical_model_name": "DeepSeek V4 Pro", "enabled": True,
            "lifecycle_state": "ACTIVE", "reasoning_levels_supported": ["MEDIUM"],
            "tool_capabilities": [], "abilities": {},
            "latency_class": "UNKNOWN", "cost_class": "MEDIUM",
            "reliability_class": "UNKNOWN", "capability_tags": ["CODE_IMPLEMENTATION"],
            "policy_version": "1", "provenance": {"source": "test", "benchmarked": False},
        }],
    )
    pol = mr.load_routing_policy()
    with pytest.raises(mr.RoutingError):
        pol.validate_models(reg)


# ---------------------------------------------------------------------------
# F6 — decision audit is fully context-bound
# ---------------------------------------------------------------------------

def test_f6_different_task_evidence_yields_different_decision_ids():
    router = mr.ModelRouter()
    req_a = mr.RoutingRequest(job_id="j", task_id="tA", role="implementer")
    req_b = mr.RoutingRequest(job_id="j", task_id="tB", role="implementer")
    d1 = router.route(req_a, now_iso="2026-01-01T00:00:00+00:00")
    d2 = router.route(req_b, now_iso="2026-01-01T00:00:00+00:00")
    assert d1.decision_id != d2.decision_id
    assert d1.sha256 != d2.sha256


def test_f6_decision_id_collision_with_different_content_errors(db_path):
    env = make_env(db_path)
    # Insert a decision row, then a different-content row with the SAME id.
    store = env.core._store
    rec = {
        "decision_id": "x" * 32, "job_id": "j", "provider": "openai",
        "model": "gpt-5.6-sol", "reasoning_level": "HIGH", "escalation_level": 2,
        "decision_reason_code": "MINIMUM_SUFFICIENT", "policy_version": "1",
        "requirements_hash": "r1", "evidence_refs_json": "[]",
        "decision_sha256": "a" * 64, "created_at": "2026-01-01T00:00:00+00:00",
    }
    store._insert_routing_decision(rec)
    rec2 = dict(rec, decision_sha256="b" * 64)
    with pytest.raises(Exception):
        store._insert_routing_decision(rec2)
    # Identical re-insert is a no-op.
    store._insert_routing_decision(rec)


def test_f6_model_choice_and_decision_rejected(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    router = env.sup._routing_engine()
    req = mr.RoutingRequest(
        job_id=env.job.supervisor_job_id, task_id=env.task.id, role="lead",
    )
    decision = router.route(req, now_iso="2026-01-01T00:00:00+00:00")
    with pytest.raises(RolePolicyViolation):
        env.core.create_dispatch(
            env.task.id, env.task_run.id, Role.LEAD, 0, 1, SequenceKind.STANDARD,
            {"provider": "openai", "model": "gpt-5.6-sol", "thinking_tier": "high"},
            LEAD,
            routing_decision=decision,
        )


def test_f6_tampered_decision_rejected(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    router = env.sup._routing_engine()
    req = mr.RoutingRequest(
        job_id=env.job.supervisor_job_id, task_id=env.task.id, role="lead",
    )
    decision = router.route(req, now_iso="2026-01-01T00:00:00+00:00")
    tampered = dataclasses.replace(decision, sha256="0" * 64)
    with pytest.raises(RolePolicyViolation):
        env.core.create_dispatch(
            env.task.id, env.task_run.id, Role.LEAD, 0, 1, SequenceKind.STANDARD,
            None, LEAD, routing_decision=tampered,
        )
    # Also a tampered canonical_json (sha recompute mismatch).
    tampered2 = dataclasses.replace(decision, canonical_json='{"tampered": true}')
    with pytest.raises(RolePolicyViolation):
        env.core.create_dispatch(
            env.task.id, env.task_run.id, Role.LEAD, 0, 1, SequenceKind.STANDARD,
            None, LEAD, routing_decision=tampered2,
        )
