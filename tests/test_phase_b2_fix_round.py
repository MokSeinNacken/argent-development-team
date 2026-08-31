"""Phase B2 Fix Round regression tests (Sol REJECT follow-up).

Covers the four confirmed findings:

* F1 (CRITICAL) — stale lease-holder TOCTOU across the three action windows
  (initial fence -> journal begin -> core effect -> journal finalize).
* F2 (HIGH) — a persisted BACKOFF must not be run by the scheduler before its
  admission deadline.
* F3 (HIGH) — restart quarantine is CAS-fenced against the stale scan snapshot;
  a newer interleaved state is never overwritten.
* F4 (MEDIUM) — incomplete RUNNING lease tuples are quarantined LOST, never
  treated as a foreign lease or a takeover candidate.

Offline and deterministic (FakeClock); no sleep, no network, no real runtime.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from argent_core import Core, OWNER_SOURCE, Role, role_source
from argent_core.models import LeaseError, LeaseFencedError
from argent_core.scheduler import OUTCOME_NO_WORK, Scheduler
from argent_core.supervisor import ReconcileAction, Supervisor
from argent_core.store import _format_dt
from mock_supervisor_runtime import FakeClock, FakeRunLauncher, FakeRunStatusProvider

OWNER = OWNER_SOURCE
LEAD = role_source(Role.LEAD)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_env(db_path, clock=None):
    clock = clock or FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    core.start_task_run(task.id, OWNER)
    prov = FakeRunStatusProvider()
    launch = FakeRunLauncher()
    sup = Supervisor(core, prov, launch, clock=clock)
    return SimpleNamespace(core=core, project=project, task=task, prov=prov,
                           launch=launch, sup=sup, clock=clock)


def add_job_for_task(env):
    job = env.sup.store.create_job(env.task.id, idempotency_key="job-main")
    return job.supervisor_job_id


def job_row(core, job_id):
    return core._store.get_supervisor_job(job_id)


def make_dual_env(db_path):
    """env (A) + a second Core/Supervisor (B) over the SAME db file."""
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    core2 = Core(db_path, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(),
                      clock=clock)
    return env, core2, sup2, clock


def takeover(clock, sup2, jid):
    """B takes over after A's short lease expires (epoch bumps to 2)."""
    clock.advance(31)  # A claimed with ttl=30
    sup2.store.claim_job(jid, owner_instance_id="B", ttl_seconds=60)


def start_role_rows(core, jid):
    return [a for a in core._store.list_supervisor_actions(jid)
            if a["action_type"] == "START_ROLE"]


# ---------------------------------------------------------------------------
# F1 — stale lease-holder TOCTOU (three windows)
# ---------------------------------------------------------------------------

def _claim_and_decision(env, jid, ttl=30):
    env.sup.store.claim_job(jid, owner_instance_id="A", ttl_seconds=ttl)
    env.sup.set_lease_owner("A", 1)
    decision = env.sup.reconcile(jid)
    assert decision.action is ReconcileAction.START_ROLE
    assert decision.owner_instance_id == "A" and decision.lease_epoch == 1
    return decision


def test_f1_takeover_between_initial_check_and_begin_action(db_path):
    env, core2, sup2, clock = make_dual_env(db_path)
    try:
        jid = add_job_for_task(env)
        decision = _claim_and_decision(env, jid)

        real_begin = env.sup._begin_action

        def begin_with_takeover(*args, **kwargs):
            takeover(clock, sup2, jid)  # B claims (epoch 2) before journal begin
            return real_begin(*args, **kwargs)

        env.sup._begin_action = begin_with_takeover
        with pytest.raises(LeaseFencedError):
            env.sup.perform_next_safe_action_if_required(decision)

        # stale A wrote NOTHING: no journal row, no role run; B is the owner.
        assert start_role_rows(env.core, jid) == []
        assert env.core.queries.get_active_role_run(env.task.id) is None
        row = job_row(env.core, jid)
        assert row["owner_instance_id"] == "B"
        assert row["lease_epoch"] == 2
    finally:
        core2.close()
        env.core.close()


def test_f1_takeover_between_begin_action_and_effect(db_path):
    env, core2, sup2, clock = make_dual_env(db_path)
    try:
        jid = add_job_for_task(env)
        decision = _claim_and_decision(env, jid)

        real_start_role = env.core.start_role

        def start_role_with_takeover(*args, **kwargs):
            takeover(clock, sup2, jid)  # B claims after journal begin
            return real_start_role(*args, **kwargs)

        env.core.start_role = start_role_with_takeover
        with pytest.raises(LeaseFencedError):
            env.sup.perform_next_safe_action_if_required(decision)

        # The core effect was rolled back (no role run); the journal begin
        # committed before the takeover and is left RUNNING (crash window).
        assert env.core.queries.get_active_role_run(env.task.id) is None
        starts = start_role_rows(env.core, jid)
        assert len(starts) == 1 and starts[0]["status"] == "RUNNING"
        row = job_row(env.core, jid)
        assert row["owner_instance_id"] == "B"
        assert row["lease_epoch"] == 2
    finally:
        core2.close()
        env.core.close()


def test_f1_takeover_between_effect_and_finish_action(db_path):
    env, core2, sup2, clock = make_dual_env(db_path)
    try:
        jid = add_job_for_task(env)
        decision = _claim_and_decision(env, jid)

        real_finish = env.sup._finish_action

        def finish_with_takeover(*args, **kwargs):
            takeover(clock, sup2, jid)  # B claims after the core effect
            return real_finish(*args, **kwargs)

        env.sup._finish_action = finish_with_takeover
        with pytest.raises(LeaseFencedError):
            env.sup.perform_next_safe_action_if_required(decision)

        # The effect (role run) committed before the takeover; the finalize
        # (SUCCEEDED) was rolled back -> journal stays RUNNING.
        assert env.core.queries.get_active_role_run(env.task.id) is not None
        starts = start_role_rows(env.core, jid)
        assert len(starts) == 1 and starts[0]["status"] == "RUNNING"
        row = job_row(env.core, jid)
        assert row["owner_instance_id"] == "B"
        assert row["lease_epoch"] == 2
    finally:
        core2.close()
        env.core.close()


def test_f1_fence_action_locked_signature(db_path):
    """The central in-transaction fence check is callable and fails closed."""
    env = make_env(db_path)
    try:
        jid = add_job_for_task(env)
        env.sup.store.claim_job(jid, owner_instance_id="A", ttl_seconds=60)
        env.sup.set_lease_owner("A", 1)
        decision = env.sup.reconcile(jid)
        # Current holder passes.
        env.sup._fence_action_locked(
            jid, decision.owner_instance_id, decision.lease_epoch,
            decision.facts_version,
        )
        # A stale token is fenced.
        with pytest.raises(LeaseFencedError):
            env.sup._fence_action_locked(jid, "A", 0, decision.facts_version)
        with pytest.raises(LeaseFencedError):
            env.sup._fence_action_locked(jid, "B", 1, decision.facts_version)
        with pytest.raises(LeaseFencedError):
            env.sup._fence_action_locked(jid, "A", 1, decision.facts_version + 1)
    finally:
        env.core.close()


# ---------------------------------------------------------------------------
# F2 — persisted BACKOFF is not run before its admission deadline
# ---------------------------------------------------------------------------

def test_f2_backoff_before_deadline_is_effectless(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    jid = add_job_for_task(env)
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60)
    env.sup.store.claim_job(jid, owner_instance_id="A", ttl_seconds=60)
    env.sup.set_lease_owner("A", 1)
    # Produce a snapshot-contention backoff (the F2 persistence shape).
    dec = env.sup._backoff_decision(jid)
    env.sup.clear_lease_owner()
    assert dec.action is ReconcileAction.WAIT
    row = job_row(env.core, jid)
    assert row["primary_state"] == "QUEUED"
    assert row["queue_reason"] == "RETRY_BACKOFF"
    assert row["next_eligible_at"] is not None
    assert row["owner_instance_id"] is None  # lease released

    # Before the deadline the pass is effectless: no claim, no START_ROLE.
    r = sched.run_pass(jid)
    assert r.outcome == OUTCOME_NO_WORK
    assert env.core.queries.get_active_role_run(env.task.id) is None

    # After the deadline it is claimable and executable.
    clock.advance(5)
    r2 = sched.run_pass(jid)
    assert r2.outcome != OUTCOME_NO_WORK
    row2 = job_row(env.core, jid)
    assert row2["owner_instance_id"] == "A"
    assert row2["lease_epoch"] == 2
    assert env.core.queries.get_active_role_run(env.task.id) is not None
    env.core.close()


def test_f2_held_continue_requires_running_state(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    jid = add_job_for_task(env)
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60)
    # A holds a valid lease, but the job was persisted into BACKOFF (QUEUED)
    # with a future eligibility deadline while the lease lingered.
    env.sup.store.claim_job(jid, owner_instance_id="A", ttl_seconds=60)
    future = _format_dt(env.clock() + timedelta(seconds=100))
    env.core._store._update_supervisor_job(
        jid, primary_state="QUEUED", status="BACKOFF",
        queue_reason="RETRY_BACKOFF", next_eligible_at=future,
    )
    # The held-continue path must NOT treat this as continuable.
    assert sched.run_pass(jid).outcome == OUTCOME_NO_WORK
    assert env.core.queries.get_active_role_run(env.task.id) is None
    env.core.close()


# ---------------------------------------------------------------------------
# F3 — restart quarantine CAS (stale scan never overwrites a newer state)
# ---------------------------------------------------------------------------

def test_f3_quarantine_lost_cas_mismatch_returns_none(db_path):
    env = make_env(db_path)
    jid = add_job_for_task(env)
    env.core._store._update_supervisor_job(
        jid, primary_state="RUNNING", status="ACTIVE",
        owner_instance_id="A", lease_epoch=1,
    )
    snapshot = job_row(env.core, jid)
    # Another commit moves the job to OWNER_GATE/WAITING_GATE.
    env.core._store._update_supervisor_job(
        jid, primary_state="OWNER_GATE", status="WAITING_GATE",
    )
    result = env.sup.store.quarantine_lost(
        jid, error_code="AMBIGUOUS_WRITER", expected=snapshot,
    )
    assert result is None  # cas_lost
    row = job_row(env.core, jid)
    assert row["primary_state"] == "OWNER_GATE"  # newer state preserved
    assert row["status"] == "WAITING_GATE"
    env.core.close()


def test_f3_interleaving_scan_quarantine_does_not_overwrite(db_path):
    env = make_env(db_path)
    jid = add_job_for_task(env)
    env.core._store._update_supervisor_job(
        jid, primary_state="RUNNING", status="ACTIVE",
        owner_instance_id="A", lease_epoch=1,
    )
    sched = Scheduler(env.sup, owner_instance_id="B", lease_ttl_seconds=60)
    real_quarantine = env.sup.store.quarantine_lost
    interleaved = {"done": False}

    def quarantine_with_interleave(job_id, *, error_code="AMBIGUOUS_WRITER",
                                   expected=None):
        if not interleaved["done"]:
            interleaved["done"] = True
            # A concurrent commit moves the job to OWNER_GATE/WAITING_GATE.
            env.core._store._update_supervisor_job(
                job_id, primary_state="OWNER_GATE", status="WAITING_GATE",
            )
        return real_quarantine(job_id, error_code=error_code, expected=expected)

    env.sup.store.quarantine_lost = quarantine_with_interleave
    summary = sched.reconcile_after_restart()
    row = job_row(env.core, jid)
    assert row["primary_state"] == "OWNER_GATE"
    assert row["status"] == "WAITING_GATE"
    assert summary.quarantined_lost == 0
    env.core.close()


# ---------------------------------------------------------------------------
# F4 — incomplete RUNNING lease tuples are quarantined LOST
# ---------------------------------------------------------------------------

def test_f4_owner_null_future_expiry_is_lost_not_foreign(db_path):
    env = make_env(db_path)
    jid = add_job_for_task(env)
    future = _format_dt(env.clock() + timedelta(seconds=3600))
    env.core._store._update_supervisor_job(
        jid, primary_state="RUNNING", status="ACTIVE",
        owner_instance_id=None, lease_epoch=0, lease_expires_at=future,
    )
    sched = Scheduler(env.sup, owner_instance_id="B", lease_ttl_seconds=60)
    summary = sched.reconcile_after_restart()
    assert summary.quarantined_lost == 1
    assert summary.foreign_lease_kept == 0
    assert summary.takeover_candidates == 0
    assert job_row(env.core, jid)["primary_state"] == "LOST"
    env.core.close()


def test_f4_owner_null_expired_expiry_is_lost_not_takeover(db_path):
    env = make_env(db_path)
    jid = add_job_for_task(env)
    past = _format_dt(env.clock() - timedelta(seconds=3600))
    env.core._store._update_supervisor_job(
        jid, primary_state="RUNNING", status="ACTIVE",
        owner_instance_id=None, lease_epoch=0, lease_expires_at=past,
    )
    sched = Scheduler(env.sup, owner_instance_id="B", lease_ttl_seconds=60)
    summary = sched.reconcile_after_restart()
    assert summary.quarantined_lost == 1
    assert summary.takeover_candidates == 0
    assert job_row(env.core, jid)["primary_state"] == "LOST"
    env.core.close()
