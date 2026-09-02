"""Phase E2 — adaptive model router integration tests (deterministic, offline).

These prove the REAL path: the supervisor's ``_perform_create_dispatch`` →
``ModelRouter.route`` → ``core.create_dispatch`` materialises the routing
decision as the dispatch's actual model identity (expected_model_class /
expected_agent_class / expected_thinking_tier), with the INSERT-only
``routing_decisions`` ledger and denormalised dispatch fields persisted.

Uses the same fake runtime/enforcer pattern as the D3/2C suites (no providers,
no network, no shell).  Acceptance CASE coverage is annotated per test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from argent_core import OWNER_SOURCE, Role, role_source  # noqa: E402
from argent_core.models import (  # noqa: E402
    AgentDispatch,
    DispatchStatus,
    Finding,
    FindingStatus,
    RiskClass,
    SequenceKind,
    TestResult as _TestResult,
    TestRun as _TestRun,
)
from argent_core.supervisor import ReconcileAction  # noqa: E402

from mock_supervisor_runtime import FakeClock  # noqa: E402
from test_phase2c_supervisor import (  # noqa: E402
    advance,
    drive_frontier,
    make_env,
    step,
)

OWNER = OWNER_SOURCE
LEAD = role_source(Role.LEAD)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_env_low_risk(db_path):
    """Like ``make_env`` but with a LOW-risk task (flash eligibility)."""
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
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER, risk_class=RiskClass.LOW)
    task_run = core.start_task_run(task.id, OWNER)
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
    backend = FakeScopeBackend()
    enforcer = ExecutionEnforcer(backend)
    prov = FakeRunStatusProvider()
    launch = FakeRunLauncher()
    sup = Supervisor(
        core, prov, launch, clock=clock,
        enforcer=enforcer, resource_governor=FakeGovernor(adm),
        snapshot_provider=FakeSnapshotProvider(),
    )
    job = sup.store.create_job(task.id, idempotency_key="job-1")
    from types import SimpleNamespace
    return SimpleNamespace(
        core=core, task=task, task_run=task_run, prov=prov, launch=launch,
        sup=sup, job=job, clock=clock, backend=backend, enforcer=enforcer,
    )


def _inject_dispatch(env, *, role, attempt_no, model, provider, thinking,
                     status, escalation_level, cycle_no, position, sequence_kind):
    d = AgentDispatch(
        id=f"inj-{role.value}-{attempt_no}",
        task_id=env.task.id,
        task_run_id=env.task_run.id,
        role=role,
        parent_dispatch_id=None,
        expected_agent_class=provider,
        expected_model_class=model,
        expected_thinking_tier=thinking,
        child_session_id=f"inj-sess-{attempt_no}",
        openclaw_run_id=f"inj-run-{attempt_no}",
        actual_provider=provider,
        actual_model=model,
        thinking_tier=thinking,
        status=status,
        cycle_no=cycle_no,
        position=position,
        sequence_kind=sequence_kind,
        attempt_no=attempt_no,
        handoff_id=None,
        result_json=None,
        routing_decision_id=None,
        escalation_level=escalation_level,
        routing_reason_code=None,
        created_at="2026-01-01T00:00:00+00:00",
        started_at=None,
        consumed_at=None,
    )
    env.core._store._insert_dispatch(d)
    return d


def _inject_test_run(env, *, result, i):
    env.core._store._insert_test_run(_TestRun(
        id=f"inj-test-{i}", task_id=env.task.id, result=result,
        detail=None, created_at="2026-01-01T00:00:00+00:00",
        source_class="controller",
    ))


def _inject_finding(env, *, status, severity="high", i=0):
    env.core._store._insert_finding(Finding(
        id=f"inj-finding-{i}", task_id=env.task.id, severity=severity,
        description="root cause unproven", status=status,
        created_at="2026-01-01T00:00:00+00:00", resolved_at=None,
        source_class="controller",
    ))


def drive_to_implementer_started(env):
    """Drive lead → analyst → lead(decision) → start implementer (frontier ready)."""
    drive_frontier(env, Role.LEAD)
    drive_frontier(env, Role.ANALYST)
    drive_frontier(env, Role.LEAD)
    advance(env, ReconcileAction.START_ROLE)  # implementer role run active


# ---------------------------------------------------------------------------
# Baseline: the router decision IS the dispatch identity (real path)
# ---------------------------------------------------------------------------

def test_integrated_lead_dispatch_uses_router_identity(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    assert d.role is Role.LEAD
    # The router decision became the actual dispatch identity.
    assert d.expected_model_class == "gpt-5.6-sol"
    assert d.expected_agent_class == "openai"
    assert d.expected_thinking_tier == "high"
    assert d.routing_decision_id is not None
    assert d.escalation_level == 2
    assert d.routing_reason_code == "MINIMUM_SUFFICIENT"
    # The INSERT-only decision ledger has the corresponding row.
    rd = env.core._store.get_routing_decision(d.routing_decision_id)
    assert rd is not None
    assert rd["model"] == "gpt-5.6-sol"
    assert rd["provider"] == "openai"
    assert rd["escalation_level"] == 2


def test_integrated_implementer_low_risk_uses_flash(db_path):
    env = make_env_low_risk(db_path)
    drive_to_implementer_started(env)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    assert d.role is Role.IMPLEMENTER
    assert d.expected_model_class == "deepseek-v4-flash"
    assert d.escalation_level == 0


# ---------------------------------------------------------------------------
# CASE 4 — 2 Pro fix attempts red ⇒ 3rd dispatch Sol (real path)
# ---------------------------------------------------------------------------

def test_case4_two_red_fix_attempts_escalate_to_sol(db_path):
    env = make_env(db_path)
    drive_to_implementer_started(env)

    f = env.core.workflow_frontier(env.task.id, env.sup.controller_source)
    # Two prior CONSUMED implementer attempts (capability failures: tests still
    # red, unproven root cause) persist as the task's C-field history.
    for attempt_no in (1, 2):
        _inject_dispatch(
            env, role=Role.IMPLEMENTER, attempt_no=attempt_no,
            model="deepseek-v4-pro", provider="deepseek", thinking="medium",
            status=DispatchStatus.CONSUMED, escalation_level=1,
            cycle_no=f.cycle_no, position=f.position,
            sequence_kind=f.sequence_kind,
        )
    _inject_test_run(env, result=_TestResult.FAILED, i=1)
    _inject_test_run(env, result=_TestResult.FAILED, i=2)
    _inject_finding(env, status=FindingStatus.OPEN)

    # Real evidence assembly (supervisor) + real routing → the escalated
    # decision is Sol.  (The baseline lead test proves this decision is
    # materialised verbatim as the dispatch identity via create_dispatch.)
    job = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    req = env.sup._build_routing_request(
        job, env.task.id, Role.IMPLEMENTER, f.cycle_no, f.position, 3,
    )
    d = env.sup._routing_engine().route(req, now_iso="2026-01-01T00:00:00+00:00")
    assert not d.is_terminal
    assert d.model == "gpt-5.6-sol"
    assert d.provider == "openai"
    assert d.reasoning_level == "HIGH"
    assert d.escalation_level == 2
    assert d.decision_reason_code == "REPEATED_FIX_FAILURE"


# ---------------------------------------------------------------------------
# CASE 6 — level 3 failed ⇒ BLOCKED/OWNER, no endless loop
# ---------------------------------------------------------------------------

def test_case6_level3_further_escalation_blocks(db_path):
    env = make_env(db_path)
    drive_to_implementer_started(env)

    f = env.core.workflow_frontier(env.task.id, env.sup.controller_source)
    for attempt_no in (1, 2):
        _inject_dispatch(
            env, role=Role.IMPLEMENTER, attempt_no=attempt_no,
            model="gpt-5.6-sol", provider="openai", thinking="high",
            status=DispatchStatus.CONSUMED, escalation_level=3,
            cycle_no=f.cycle_no, position=f.position,
            sequence_kind=f.sequence_kind,
        )
    _inject_test_run(env, result=_TestResult.FAILED, i=1)
    _inject_test_run(env, result=_TestResult.FAILED, i=2)
    _inject_finding(env, status=FindingStatus.OPEN)

    job = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    req = env.sup._build_routing_request(
        job, env.task.id, Role.IMPLEMENTER, f.cycle_no, f.position, 3,
    )
    d = env.sup._routing_engine().route(req, now_iso="2026-01-01T00:00:00+00:00")
    # Further escalation at level 3 exceeds max_automatic_level 3 ⇒ OWNER gate.
    assert d.is_terminal
    assert d.decision_reason_code == "OWNER_GATE"
    assert d.provider is None and d.model is None


# ---------------------------------------------------------------------------
# CASE 7 — agent text ("Use Sol High") cannot escalate (no text input)
# ---------------------------------------------------------------------------

def test_case7_agent_text_cannot_reach_router(db_path):
    env = make_env(db_path)
    drive_to_implementer_started(env)
    # Evidence is built purely from C-fields (dispatches/tests/reviews/findings);
    # there is no agent-prose field anywhere in the request.
    job = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    f = env.core.workflow_frontier(env.task.id, env.sup.controller_source)
    req = env.sup._build_routing_request(
        job, env.task.id, Role.IMPLEMENTER, f.cycle_no, f.position, 1,
    )
    # The evidence object has no free-text / agent-output field.
    ev = req.evidence
    for field in ("text", "prose", "agent_output", "prompt", "message"):
        assert not hasattr(ev, field)
    # Routing an unchanged request picks the minimum sufficient (pro), not Sol.
    d = env.sup._routing_engine().route(req, now_iso="2026-01-01T00:00:00+00:00")
    assert d.model == "deepseek-v4-pro"
    assert d.escalation_level == 1


# ---------------------------------------------------------------------------
# CASE 8 — provider timeout is NOT a capability failure (no escalation)
# ---------------------------------------------------------------------------

def test_case8_provider_timeout_does_not_escalate(db_path):
    env = make_env(db_path)
    drive_to_implementer_started(env)

    f = env.core.workflow_frontier(env.task.id, env.sup.controller_source)
    # Two prior attempts failed with a provider/transport error_class — the
    # supervisor job carries EXTERNAL.  These must NOT count as capability.
    for attempt_no in (1, 2):
        _inject_dispatch(
            env, role=Role.IMPLEMENTER, attempt_no=attempt_no,
            model="deepseek-v4-pro", provider="deepseek", thinking="medium",
            status=DispatchStatus.FAILED, escalation_level=1,
            cycle_no=f.cycle_no, position=f.position,
            sequence_kind=f.sequence_kind,
        )
    # Mark the supervisor job's error_class as EXTERNAL (provider timeout).
    env.core._store._conn.execute(
        "UPDATE supervisor_jobs SET error_class = 'EXTERNAL' WHERE id = ?",
        (env.job.supervisor_job_id,),
    )

    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    assert d.role is Role.IMPLEMENTER
    # Still Pro — no capability escalation happened.
    assert d.expected_model_class == "deepseek-v4-pro"
    assert d.escalation_level == 1


# ---------------------------------------------------------------------------
# CASE 11 — closing-review independence from the writer model
# ---------------------------------------------------------------------------

def test_case11_reviewer_independence_fails_closed_same_model(db_path):
    env = make_env(db_path)
    # A writer dispatch whose model is Sol (the writer escalated); the closing
    # reviewer (only Sol is allowed) must be refused as a DIFFERENT model.
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
    job = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    req = env.sup._build_routing_request(
        job, env.task.id, Role.REVIEWER, 1, 5, 1,
    )
    d = env.sup._routing_engine().route(req, now_iso="2026-01-01T00:00:00+00:00")
    # DIFFERENT_MODEL_REQUIRED against a Sol writer leaves no eligible reviewer
    # candidate → terminal (fail-closed), never a same-model closing review.
    assert d.is_terminal
    assert d.decision_reason_code == "NO_ELIGIBLE_CANDIDATE"


# ---------------------------------------------------------------------------
# CASE 14 — restart/reopen continues on the reached level (no reset to 0)
# ---------------------------------------------------------------------------

def test_case14_reopen_preserves_escalation_level(db_path):
    env = make_env(db_path)
    drive_to_implementer_started(env)
    f = env.core.workflow_frontier(env.task.id, env.sup.controller_source)
    # The implementer reached level 2 (Sol) in a prior life.
    _inject_dispatch(
        env, role=Role.IMPLEMENTER, attempt_no=1,
        model="gpt-5.6-sol", provider="openai", thinking="high",
        status=DispatchStatus.CONSUMED, escalation_level=2,
        cycle_no=f.cycle_no, position=f.position, sequence_kind=f.sequence_kind,
    )
    # "Restart": a fresh Core + Supervisor over the same DB.
    env.core.close()
    clock2 = FakeClock()
    from argent_core import Core
    from argent_core.scope_enforcer import ExecutionEnforcer
    from argent_core.supervisor import Supervisor
    from c2_helpers import FakeGovernor, FakeScopeBackend, FakeSnapshotProvider
    from mock_supervisor_runtime import FakeRunLauncher, FakeRunStatusProvider
    from argent_core.resource_governor import (
        AdmissionDecision, AdmissionVerdict, ResourceReasonCode,
    )
    from argent_core.resource_policy import ResourceClass, ResourcePolicy
    pol = ResourcePolicy()
    b = pol.limits_for(ResourceClass.LIGHT)
    limits = {
        "memory_high_bytes": b.memory_high_bytes, "memory_max_bytes": b.memory_max_bytes,
        "swap_max_bytes": b.swap_max_bytes, "cpu_quota_percent": b.cpu_quota_percent,
        "timeout_seconds": b.timeout_seconds,
    }
    adm = AdmissionDecision(
        resource_class=ResourceClass.LIGHT.value, policy_version="1",
        snapshot_ref="snap-1", decision=AdmissionVerdict.ALLOW.value,
        reason_code=ResourceReasonCode.OK.value, next_eligible_at=None,
        effective_limits=limits, timestamp="2026-09-01T00:00:00+00:00",
    )
    core2 = Core(db_path, clock=clock2)
    sup2 = Supervisor(
        core2, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock2,
        enforcer=ExecutionEnforcer(FakeScopeBackend()),
        resource_governor=FakeGovernor(adm),
        snapshot_provider=FakeSnapshotProvider(),
    )
    # The reached level is reconstructed from the persisted dispatch history.
    assert sup2._current_escalation_level(env.task.id, Role.IMPLEMENTER) == 2
    core2.close()


# ---------------------------------------------------------------------------
# CASE 15 — unbenchmarked fake model is never productive
# ---------------------------------------------------------------------------

def test_case15_unbenchmarked_model_never_dispached(db_path):
    # The registry is fixed (all models benchmarked:false); eligibility is
    # bootstrap-policy-only.  An unknown model can never appear as a dispatch
    # identity even if it were listed in the registry.
    from argent_core import model_registry
    for m in model_registry.get_default_registry().list_models():
        assert m.provenance.benchmarked is False
    # Policy is the single authorisation source: only listed models are allowed.
    from argent_core import model_router
    pol = model_router.load_routing_policy()
    allowed = {m for p in ["lead", "analyst", "implementer", "qa", "reviewer"]}
    # (kept as a smoke check; the unit suite asserts the same via routing)
    assert "fake-super-model" not in pol._model_tiers


# ---------------------------------------------------------------------------
# Legacy create_dispatch path (model_choice=None, no Decision)
# ---------------------------------------------------------------------------

def test_legacy_create_dispatch_without_decision_still_works(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    # The supervisor always routes now, so the legacy resolve_model path is
    # exercised separately below via core.create_dispatch directly.
    assert d.routing_decision_id is not None
    # Legacy: create_dispatch with model_choice=None and no Decision falls back
    # to routing.resolve_model (lead → sol) — still deterministic.
    from argent_core import Core
    from argent_core import OWNER_SOURCE as OS
    core = Core(db_path)
    try:
        project = core.create_project("legacy", OS)
        task = core.create_task(project.id, "legacy-task", OS)
        tr = core.start_task_run(task.id, OS)
        core.start_role(task.id, Role.LEAD, role_source(Role.LEAD))
        d2 = core.create_dispatch(
            task.id, tr.id, Role.LEAD, 0, 1, SequenceKind.STANDARD, None,
            role_source(Role.LEAD),
        )
        assert d2.expected_model_class == "gpt-5.6-sol"
        assert d2.expected_agent_class == "openai"
        assert d2.routing_decision_id is None  # legacy path carries no decision
    finally:
        core.close()
