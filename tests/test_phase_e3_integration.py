"""Phase E3 — integrated fallback/provenance/evidence tests (deterministic, offline).

These prove the REAL path: supervisor ``_perform_create_dispatch`` →
``ModelRouter.route`` → ``core.create_dispatch`` materialises the validated
fallback decision as the dispatch's actual model identity, with the INSERT-only
``routing_decisions`` ledger carrying versioned provenance (registry_version /
evidence_version / inputs_hash).

Uses the same fake runtime/enforcer pattern as the D3/2C/E2 suites (no providers,
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
    RiskClass,
    SequenceKind,
)
from argent_core.supervisor import ReconcileAction  # noqa: E402

from mock_supervisor_runtime import FakeClock  # noqa: E402
from test_phase2c_supervisor import advance, drive_frontier  # noqa: E402
from test_phase_e2_integration import (  # noqa: E402
    drive_to_implementer_started,
    make_env,
    make_env_low_risk,
)

OWNER = OWNER_SOURCE
LEAD = role_source(Role.LEAD)


def _inject_dispatch(env, *, role, attempt_no, model, provider, thinking, status,
                     escalation_level, cycle_no, position, sequence_kind,
                     attempt_outcome=None):
    d = AgentDispatch(
        id=f"inj-{role.value}-{attempt_no}",
        task_id=env.task.id,
        task_run_id=env.task_run.id,
        role=role,
        parent_dispatch_id=None,
        expected_agent_class=provider,
        expected_model_class=model,
        expected_thinking_tier=thinking,
        child_session_id=f"inj-{role.value}-{attempt_no}-sess",
        openclaw_run_id=f"inj-{role.value}-{attempt_no}-run",
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
        attempt_outcome=attempt_outcome,
        created_at="2026-01-01T00:00:00+00:00",
        started_at=None,
        consumed_at=None,
    )
    env.core._store._insert_dispatch(d)
    return d


# ---------------------------------------------------------------------------
# CASE 5 — validated fallback in the REAL dispatch path + provenance
# ---------------------------------------------------------------------------

def test_case5_integrated_fallback_real_path(db_path):
    env = make_env_low_risk(db_path)
    drive_to_implementer_started(env)

    f = env.core.workflow_frontier(env.task.id, env.sup.controller_source)
    # A prior implementer attempt on flash ended in a HARD provider/model
    # failure (attempt_outcome PROVIDER) -> the snapshot marks flash UNAVAILABLE.
    _inject_dispatch(
        env, role=Role.IMPLEMENTER, attempt_no=1,
        model="deepseek-v4-flash", provider="deepseek", thinking="medium",
        status=DispatchStatus.FAILED, escalation_level=0,
        cycle_no=f.cycle_no, position=f.position,
        sequence_kind=f.sequence_kind, attempt_outcome="PROVIDER",
    )

    # The supervisor assembles the snapshot from the persisted PROVIDER outcome.
    job = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    req = env.sup._build_routing_request(
        job, env.task.id, Role.IMPLEMENTER, f.cycle_no, f.position, 2,
    )
    assert req.availability_snapshot is not None
    assert req.availability_snapshot.model_states["deepseek-v4-flash"] == "UNAVAILABLE"

    # Real routing -> validated fallback to pro (same escalation level 0).
    d = env.sup._routing_engine().route(req, now_iso="2026-01-01T00:00:00+00:00")
    assert not d.is_terminal
    assert d.model == "deepseek-v4-pro"
    assert d.decision_reason_code == "VALIDATED_FALLBACK"
    assert d.escalation_level == 0

    # Materialise through the REAL core.create_dispatch path (persists the
    # decision ledger + provenance, and the dispatch identity).
    disp = env.core.create_dispatch(
        env.task.id, env.task_run.id, Role.IMPLEMENTER, f.position, f.cycle_no,
        f.sequence_kind, None, env.sup.controller_source,
        routing_decision=d,
    )
    assert disp.expected_model_class == "deepseek-v4-pro"
    assert disp.routing_decision_id == d.decision_id

    rd = env.core._store.get_routing_decision(d.decision_id)
    assert rd is not None
    assert rd["model"] == "deepseek-v4-pro"
    assert rd["decision_reason_code"] == "VALIDATED_FALLBACK"
    # CASE 16/17: provenance persisted.
    assert rd["registry_version"] == "1"
    assert rd["evidence_version"] == "1"
    assert isinstance(rd["inputs_hash"], str) and len(rd["inputs_hash"]) == 64
    assert rd["inputs_hash"] == d.inputs_hash


# ---------------------------------------------------------------------------
# CASE 21 — restart/reopen reads the persisted provenance
# ---------------------------------------------------------------------------

def test_case21_reopen_reads_persisted_provenance(db_path):
    env = make_env_low_risk(db_path)
    drive_to_implementer_started(env)
    f = env.core.workflow_frontier(env.task.id, env.sup.controller_source)
    _inject_dispatch(
        env, role=Role.IMPLEMENTER, attempt_no=1,
        model="deepseek-v4-flash", provider="deepseek", thinking="medium",
        status=DispatchStatus.FAILED, escalation_level=0,
        cycle_no=f.cycle_no, position=f.position,
        sequence_kind=f.sequence_kind, attempt_outcome="PROVIDER",
    )
    job = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    req = env.sup._build_routing_request(
        job, env.task.id, Role.IMPLEMENTER, f.cycle_no, f.position, 2,
    )
    d = env.sup._routing_engine().route(req, now_iso="2026-01-01T00:00:00+00:00")
    env.core.create_dispatch(
        env.task.id, env.task_run.id, Role.IMPLEMENTER, f.position, f.cycle_no,
        f.sequence_kind, None, env.sup.controller_source, routing_decision=d,
    )
    decision_id = d.decision_id

    # "Restart": a fresh Core over the same DB.
    env.core.close()
    from argent_core import Core
    core2 = Core(db_path)
    try:
        rd = core2._store.get_routing_decision(decision_id)
        assert rd is not None
        assert rd["registry_version"] == "1"
        assert rd["evidence_version"] == "1"
        assert rd["policy_version"] == "2"
        assert rd["inputs_hash"] == d.inputs_hash
        assert rd["decision_reason_code"] == "VALIDATED_FALLBACK"
        # F3: full content-digest provenance persists across reopen, and the
        # decision can be re-verified from its persisted canonical binding.
        assert rd["policy_hash"] == d.policy_hash
        assert rd["registry_hash"] == d.registry_hash
        assert rd["evidence_hash"] == d.evidence_hash
        import hashlib
        import json as _json
        canon = _json.loads(d.canonical_json)
        assert canon["inputs_hash"] == rd["inputs_hash"]
        assert hashlib.sha256(d.canonical_json.encode("utf-8")).hexdigest() == d.sha256
    finally:
        core2.close()


# ---------------------------------------------------------------------------
# CASE 24 — unavailable strong security model: no silent weaker review
# ---------------------------------------------------------------------------

def test_case24_integrated_reviewer_sol_unavailable_fail_closed(db_path):
    env = make_env(db_path)
    # The writer is pro (implementer); the closing reviewer (only sol allowed)
    # will find sol unavailable (a prior sol provider failure).
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
    _inject_dispatch(
        env, role=Role.REVIEWER, attempt_no=1,
        model="gpt-5.6-sol", provider="openai", thinking="high",
        status=DispatchStatus.FAILED, escalation_level=2,
        cycle_no=1, position=5, sequence_kind=SequenceKind.STANDARD,
        attempt_outcome="PROVIDER",
    )
    job = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    req = env.sup._build_routing_request(job, env.task.id, Role.REVIEWER, 1, 5, 1)
    d = env.sup._routing_engine().route(req, now_iso="2026-01-01T00:00:00+00:00")
    assert d.is_terminal
    assert d.model is None
    assert d.decision_reason_code == "NO_VALID_FALLBACK"


# ---------------------------------------------------------------------------
# CASE 22/23 — terminal immutability is not reopened by fallback logic
# ---------------------------------------------------------------------------

def test_case22_23_terminal_not_reopened_by_fallback(db_path):
    env = make_env(db_path)
    drive_to_implementer_started(env)
    # CASE 22: a DONE terminal job is immutable and never re-decided.
    job = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    env.sup._close_job(job, "DONE")
    cur = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    assert cur["terminal"] == "DONE"
    d = env.sup.reconcile(env.job.supervisor_job_id)
    assert d.action is ReconcileAction.NONE

    # CASE 23: a BLOCKED terminal job is likewise never reopened by the
    # fallback/routing path (the store refuses a terminal->reopen here).
    env2 = make_env(db_path, idempotency_key="job-2")
    drive_to_implementer_started(env2)
    job2 = env2.core._store.get_supervisor_job(env2.job.supervisor_job_id)
    env2.sup._close_job(job2, "BLOCKED")
    cur2 = env2.core._store.get_supervisor_job(env2.job.supervisor_job_id)
    assert cur2["terminal"] == "BLOCKED"
    d2 = env2.sup.reconcile(env2.job.supervisor_job_id)
    assert d2.action is ReconcileAction.NONE


# ---------------------------------------------------------------------------
# CASE 18/19/20 — fallback changes neither tool permissions nor policy budgets
# ---------------------------------------------------------------------------

def test_case18_19_20_fallback_keeps_permissions_and_policies(db_path):
    env = make_env_low_risk(db_path)
    drive_to_implementer_started(env)
    f = env.core.workflow_frontier(env.task.id, env.sup.controller_source)
    _inject_dispatch(
        env, role=Role.IMPLEMENTER, attempt_no=1,
        model="deepseek-v4-flash", provider="deepseek", thinking="medium",
        status=DispatchStatus.FAILED, escalation_level=0,
        cycle_no=f.cycle_no, position=f.position,
        sequence_kind=f.sequence_kind, attempt_outcome="PROVIDER",
    )
    job = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    req = env.sup._build_routing_request(
        job, env.task.id, Role.IMPLEMENTER, f.cycle_no, f.position, 2,
    )
    d = env.sup._routing_engine().route(req, now_iso="2026-01-01T00:00:00+00:00")
    assert d.decision_reason_code == "VALIDATED_FALLBACK"
    # The decision carries a model identity only — no tool permission, no
    # resource/context budget change.
    assert not hasattr(d, "tool_permissions")
    assert not hasattr(d, "resource_limits")
    assert not hasattr(d, "context_budget")
