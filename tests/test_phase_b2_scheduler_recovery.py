"""Phase B2 durable scheduler + restart/recovery tests (offline, deterministic).

Covers (A) bounded scheduler passes, (B) dual-supervisor exactly-one-claim,
(C) renewal policy, (D) epoch takeover + fencing a stale owner, (E) restart
reconciliation, (F) action-journal crash windows, (G) ambiguous-writer
fail-closed, and (H, as a separate run) B1 regression.

All time is controlled via ``FakeClock``; no sleep, no network, no real runtime.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from argent_core import Core, OWNER_SOURCE, Role, role_source
from argent_core.models import LeaseError, LeaseFencedError
from argent_core.scheduler import (
    OUTCOME_NO_WORK,
    OUTCOME_RENEWED,
    RestartReconcileSummary,
    Scheduler,
)
from argent_core.supervisor import ReconcileAction, Supervisor, _canonical_json, _sha256
from argent_core.store import _format_dt
from mock_supervisor_runtime import FakeClock, FakeRunLauncher, FakeRunStatusProvider

OWNER = OWNER_SOURCE
LEAD = role_source(Role.LEAD)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_env(db_path, clock=None):
    """Core + Supervisor over a temp DB with one started task run (no job yet)."""
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


def add_queued_job(env, title="job", *, idem=None):
    """Create a supervisor job; ``create_job`` already lands it QUEUED."""
    task = env.core.create_task(env.project.id, title, OWNER)
    job = env.sup.store.create_job(task.id, idempotency_key=idem or f"job-{task.id}")
    return job.supervisor_job_id


def job_row(core, job_id):
    return core._store.get_supervisor_job(job_id)


def set_terminal(core, job_id, terminal):
    core._store._update_supervisor_job(
        job_id, status="TERMINAL", terminal=terminal, next_action="NONE"
    )
    return job_row(core, job_id)


def add_job_for_task(env):
    """Create a supervisor job for ``env.task`` (which already has a run)."""
    job = env.sup.store.create_job(env.task.id, idempotency_key="job-main")
    return job.supervisor_job_id


# ---------------------------------------------------------------------------
# A. Bounded scheduler passes
# ---------------------------------------------------------------------------

def test_pass_terminates_and_claims_queued_job(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env)
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60)
    r = sched.run_pass(jid)
    # The pass returns (bounded) and exactly one safe step advanced the job.
    assert r.outcome in (OUTCOME_RENEWED, "stepped", "released")
    row = job_row(env.core, jid)
    assert row["primary_state"] == "RUNNING"
    assert row["owner_instance_id"] == "A"
    assert row["lease_epoch"] == 1
    assert row["lease_expires_at"] is not None
    env.core.close()


def test_pass_picks_next_claimable_job_without_job_id(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env)
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60)
    r = sched.run_pass()  # no job_id -> claim_next_job
    assert r.job_id == jid
    assert job_row(env.core, jid)["owner_instance_id"] == "A"
    env.core.close()


def test_pass_ignores_terminal_jobs(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env)
    set_terminal(env.core, jid, "DONE")
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60)
    assert sched.run_pass(jid).outcome == OUTCOME_NO_WORK
    assert sched.run_pass().outcome == OUTCOME_NO_WORK
    assert job_row(env.core, jid)["terminal"] == "DONE"
    assert job_row(env.core, jid)["owner_instance_id"] is None
    env.core.close()


def test_pass_respects_next_eligible_at(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env)
    future = _format_dt(env.clock() + timedelta(seconds=100))
    env.core._store.enqueue_job(jid, queue_reason="RETRY_BACKOFF",
                                next_eligible_at=future)
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60)
    assert sched.run_pass(jid).outcome == OUTCOME_NO_WORK
    env.clock.advance(101)
    r = sched.run_pass(jid)
    assert r.outcome != OUTCOME_NO_WORK
    assert job_row(env.core, jid)["owner_instance_id"] == "A"
    env.core.close()


def test_pass_no_work_when_queue_empty(db_path):
    env = make_env(db_path)
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60)
    assert sched.run_pass().outcome == OUTCOME_NO_WORK
    assert sched.run_pass("supervisor:missing").outcome == OUTCOME_NO_WORK
    env.core.close()


# ---------------------------------------------------------------------------
# B. Dual-supervisor: exactly one claim winner
# ---------------------------------------------------------------------------

def test_dual_scheduler_exactly_one_wins(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    jid = add_queued_job(env)

    core2 = Core(db_path, clock=clock)
    prov2 = FakeRunStatusProvider()
    sup2 = Supervisor(core2, prov2, FakeRunLauncher(), clock=clock)
    sched_a = Scheduler(env.sup, owner_instance_id="instance-A", lease_ttl_seconds=60)
    sched_b = Scheduler(sup2, owner_instance_id="instance-B", lease_ttl_seconds=60)
    try:
        ra = sched_a.run_pass(jid)
        assert ra.outcome != OUTCOME_NO_WORK
        rb = sched_b.run_pass(jid)
        assert rb.outcome == OUTCOME_NO_WORK  # loser is effectless
        row = job_row(env.core, jid)
        assert row["owner_instance_id"] == "instance-A"
        assert row["lease_epoch"] == 1
    finally:
        core2.close()
        env.core.close()


def test_dual_scheduler_disjoint_jobs_both_work(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    jid1 = add_queued_job(env, "one")
    jid2 = add_queued_job(env, "two")

    core2 = Core(db_path, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock)
    sched_a = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60)
    sched_b = Scheduler(sup2, owner_instance_id="B", lease_ttl_seconds=60)
    try:
        ra = sched_a.run_pass(jid1)
        rb = sched_b.run_pass(jid2)
        assert ra.outcome != OUTCOME_NO_WORK and rb.outcome != OUTCOME_NO_WORK
        assert job_row(env.core, jid1)["owner_instance_id"] == "A"
        assert job_row(env.core, jid2)["owner_instance_id"] == "B"
    finally:
        core2.close()
        env.core.close()


# ---------------------------------------------------------------------------
# C. Renewal policy
# ---------------------------------------------------------------------------

def test_renewal_extends_lease_for_active_holder(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env)
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60)
    r1 = sched.run_pass(jid)
    assert r1.outcome == OUTCOME_RENEWED  # claimed + stepped + still RUNNING
    row = job_row(env.core, jid)
    assert row["owner_instance_id"] == "A"
    assert row["lease_epoch"] == 1
    expires1 = row["lease_expires_at"]
    env.clock.advance(10)
    r2 = sched.run_pass(jid)  # already held -> step -> renew (same epoch)
    assert r2.outcome == OUTCOME_RENEWED
    row2 = job_row(env.core, jid)
    assert row2["lease_epoch"] == 1  # renewed, not re-claimed
    assert row2["lease_expires_at"] > expires1
    env.core.close()


def test_renewal_refuses_stale_epoch_wrong_owner_expired(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env)
    env.sup.store.claim_job(jid, owner_instance_id="A", ttl_seconds=30)
    # Wrong owner.
    with pytest.raises(LeaseError):
        env.sup.store.renew_lease(jid, owner_instance_id="B", lease_epoch=1,
                                  ttl_seconds=60)
    # Stale epoch.
    with pytest.raises(LeaseError):
        env.sup.store.renew_lease(jid, owner_instance_id="A", lease_epoch=0,
                                  ttl_seconds=60)
    # Expired lease is never silently resurrected.
    env.clock.advance(3600)
    with pytest.raises(LeaseError):
        env.sup.store.renew_lease(jid, owner_instance_id="A", lease_epoch=1,
                                  ttl_seconds=60)
    env.core.close()


def test_expired_lease_is_taken_over_not_resurrected(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env)
    env.sup.store.claim_job(jid, owner_instance_id="A", ttl_seconds=30)
    env.clock.advance(31)  # expired
    # F1: a RUNNING job is never re-claimed directly; the expired lease is
    # taken over via the evidence-bound recovery path (epoch+1, never the old
    # lease silently resurrected).
    taken = env.sup.store.recover_takeover_job(
        jid, expected=job_row(env.core, jid), owner_instance_id="A",
        ttl_seconds=60, process_alive=False, worktree_verdict=None,
    )
    assert taken["lease_epoch"] == 2  # takeover -> new epoch, not the old lease
    # The stale epoch-1 holder is fenced.
    env.sup.set_lease_owner("A", 1)
    with pytest.raises(LeaseFencedError):
        env.sup.reconcile(jid)
    env.core.close()


# ---------------------------------------------------------------------------
# D. Takeover + fencing a stale owner
# ---------------------------------------------------------------------------

def test_takeover_a_then_b_then_stale_a_fenced(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    jid = add_queued_job(env)
    sched_a = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=30)
    assert sched_a.run_pass(jid).outcome != OUTCOME_NO_WORK
    assert job_row(env.core, jid)["owner_instance_id"] == "A"

    clock.advance(31)  # A's lease expires

    core2 = Core(db_path, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock)
    try:
        # F1: RUNNING takeover goes through the recovery path (never claim_job).
        taken = core2._store.recover_takeover_job(
            jid, expected=core2._store.get_supervisor_job(jid),
            owner_instance_id="B", ttl_seconds=60,
            process_alive=False, worktree_verdict=None,
        )
        assert taken["owner_instance_id"] == "B"
        assert taken["lease_epoch"] == 2
    finally:
        core2.close()

    # A returns: cannot claim (B holds a valid lease) -> effectless.
    assert sched_a.run_pass(jid).outcome == OUTCOME_NO_WORK
    # A's direct reconcile under its stale epoch is fenced.
    env.sup.set_lease_owner("A", 1)
    with pytest.raises(LeaseFencedError):
        env.sup.reconcile(jid)
    env.core.close()


# ---------------------------------------------------------------------------
# E. Restart reconciliation
# ---------------------------------------------------------------------------

def test_restart_preserves_facts_and_reconciles(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    jid_running = add_queued_job(env, "running")
    jid_queued = add_queued_job(env, "queued")
    jid_done = add_queued_job(env, "done")
    env.sup.store.claim_job(jid_running, owner_instance_id="A", ttl_seconds=3600)
    set_terminal(env.core, jid_done, "DONE")

    before = {j: job_row(env.core, j) for j in (jid_running, jid_queued, jid_done)}
    env.core.close()

    core2 = Core(db_path, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock)
    sched2 = Scheduler(sup2, owner_instance_id="A", lease_ttl_seconds=60)
    try:
        # Facts identical after reopen (no in-memory cache authority).
        for j in (jid_running, jid_queued, jid_done):
            after = job_row(core2, j)
            assert after["primary_state"] == before[j]["primary_state"]
            assert after["owner_instance_id"] == before[j]["owner_instance_id"]
            assert after["lease_epoch"] == before[j]["lease_epoch"]
            assert after["terminal"] == before[j]["terminal"]

        summary = sched2.reconcile_after_restart()
        assert isinstance(summary, RestartReconcileSummary)
        assert summary.rebound == 1  # running job held by A, valid lease
        assert summary.quarantined_lost == 0
        # The terminal job is skipped (never duplicated / reopened).
        done_detail = [d for d in summary.details
                       if d[0] == jid_done and d[1] == "skip_terminal"]
        assert len(done_detail) == 1
        assert job_row(core2, jid_done)["terminal"] == "DONE"
        # Re-running reconciliation is a no-op (idempotent).
        summary2 = sched2.reconcile_after_restart()
        assert summary2.rebound == summary.rebound
        assert summary2.quarantined_lost == summary.quarantined_lost
    finally:
        core2.close()


def test_restart_ambiguous_null_expiry_quarantined(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env)
    # Force an inconsistent RUNNING row with NULL lease expiry.
    env.core._store._update_supervisor_job(
        jid, primary_state="RUNNING", status="ACTIVE",
        owner_instance_id="A", lease_epoch=1,
    )
    sched = Scheduler(env.sup, owner_instance_id="B", lease_ttl_seconds=60)
    summary = sched.reconcile_after_restart()
    assert summary.quarantined_lost == 1
    row = job_row(env.core, jid)
    assert row["primary_state"] == "LOST"
    # LOST is never claimable -> no second writer.
    with pytest.raises(LeaseError) as exc:
        env.sup.store.claim_job(jid, owner_instance_id="B", ttl_seconds=60)
    assert "not_claimable:LOST" in str(exc.value)
    env.core.close()


# ---------------------------------------------------------------------------
# F. Action-journal crash windows (lease/epoch context)
# ---------------------------------------------------------------------------

def _start_role_key(env, jid):
    sup = env.sup
    task_id = env.task.id
    f = env.core.workflow_frontier(task_id, sup.controller_source)
    role = f.expected_role
    cycle, pos, attempt = sup._frontier_attempt(task_id, f)
    key = (f"supervisor:{jid}:cycle:{cycle}:pos:{pos}:attempt:{attempt}:"
           f"start-role")
    args_hash = _sha256(_canonical_json({
        "task_id": task_id, "role": role.value, "source": sup.controller_source,
    }))
    return key, args_hash


def test_crash_window_before_effect(db_path):
    """Crash BEFORE the effect: recovery re-executes safely, exactly once."""
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    jid = add_job_for_task(env)
    env.sup.store.claim_job(jid, owner_instance_id="A", ttl_seconds=3600)
    env.sup.set_lease_owner("A", 1)
    key, args_hash = _start_role_key(env, jid)
    row, outcome = env.sup._begin_action(key, "START_ROLE",
                                         job_row(env.core, jid), None, args_hash)
    assert outcome == "new" and row["status"] == "RUNNING"
    assert env.core.queries.get_active_role_run(env.task.id) is None
    env.core.close()

    # Restart over the same DB; same owner rebinds and replays.
    core2 = Core(db_path, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock)
    try:
        sup2.set_lease_owner("A", 1)
        dec = sup2.reconcile(jid)
        assert dec.action is ReconcileAction.START_ROLE
        sup2.perform_next_safe_action_if_required(dec)
        assert len(core2.queries.list_role_runs(env.task.id)) == 1
    finally:
        core2.close()


def test_crash_window_after_effect_before_finalize(db_path):
    """Crash AFTER the effect but BEFORE finalize: no double effect."""
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    jid = add_job_for_task(env)
    env.sup.store.claim_job(jid, owner_instance_id="A", ttl_seconds=3600)
    env.sup.set_lease_owner("A", 1)
    key, args_hash = _start_role_key(env, jid)
    row, outcome = env.sup._begin_action(key, "START_ROLE",
                                         job_row(env.core, jid), None, args_hash)
    # Effect applied, journal left RUNNING (crash before _finish_action).
    env.core.start_role(env.task.id, Role.LEAD, env.sup.controller_source,
                        idempotency_key=key)
    assert row["status"] == "RUNNING"
    env.core.close()

    core2 = Core(db_path, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock)
    try:
        sup2.set_lease_owner("A", 1)
        dec = sup2.reconcile(jid)
        # Role run already exists -> decision moves past START_ROLE.
        assert dec.action is not ReconcileAction.START_ROLE
        sup2.perform_next_safe_action_if_required(dec)
        assert len(core2.queries.list_role_runs(env.task.id)) == 1  # no double
    finally:
        core2.close()


def test_crash_window_after_finalize(db_path):
    """Crash AFTER finalize: nothing re-executes, exactly one journaled row."""
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    jid = add_job_for_task(env)
    env.sup.store.claim_job(jid, owner_instance_id="A", ttl_seconds=3600)
    env.sup.set_lease_owner("A", 1)
    key, args_hash = _start_role_key(env, jid)
    row, _ = env.sup._begin_action(key, "START_ROLE",
                                   job_row(env.core, jid), None, args_hash)
    env.core.start_role(env.task.id, Role.LEAD, env.sup.controller_source,
                        idempotency_key=key)
    env.sup._finish_action(row["id"], "SUCCEEDED")
    env.core.close()

    core2 = Core(db_path, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock)
    try:
        sup2.set_lease_owner("A", 1)
        dec = sup2.reconcile(jid)
        assert dec.action is not ReconcileAction.START_ROLE
        sup2.perform_next_safe_action_if_required(dec)
        assert len(core2.queries.list_role_runs(env.task.id)) == 1
        starts = [a for a in core2._store.list_supervisor_actions(jid)
                  if a["action_type"] == "START_ROLE"]
        assert len(starts) == 1 and starts[0]["status"] == "SUCCEEDED"
    finally:
        core2.close()


def test_recovery_fencing_stale_epoch_cannot_execute(db_path):
    """Fencing also applies during recovery: a stale-epoch owner cannot run."""
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    jid = add_queued_job(env)
    env.sup.store.claim_job(jid, owner_instance_id="A", ttl_seconds=30)
    env.sup.set_lease_owner("A", 1)
    decision = env.sup.reconcile(jid)
    assert decision is not None and decision.owner_instance_id == "A"
    clock.advance(31)  # expire A
    # F1: RUNNING takeover goes through the recovery path (never claim_job).
    env.sup.store.recover_takeover_job(
        jid, expected=job_row(env.core, jid), owner_instance_id="B",
        ttl_seconds=60, process_alive=False, worktree_verdict=None,
    )
    # A (stale epoch 1) executes its old decision -> fenced, nothing written.
    with pytest.raises(LeaseFencedError):
        env.sup.perform_next_safe_action_if_required(decision)
    assert job_row(env.core, jid)["owner_instance_id"] == "B"
    assert job_row(env.core, jid)["lease_epoch"] == 2
    env.core.close()


# ---------------------------------------------------------------------------
# G. Ambiguous writer fail-closed (no second writer, no blind respawn)
# ---------------------------------------------------------------------------

def test_ambiguous_writer_no_second_writer_no_blind_respawn(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env)
    # RUNNING with a valid foreign lease and no progress evidence -> NOT taken
    # over (belongs to the holder); no second writer, no blind respawn.
    env.sup.store.claim_job(jid, owner_instance_id="writer-A", ttl_seconds=3600)
    sched_b = Scheduler(env.sup, owner_instance_id="writer-B", lease_ttl_seconds=60)
    assert sched_b.run_pass(jid).outcome == OUTCOME_NO_WORK
    row = job_row(env.core, jid)
    assert row["owner_instance_id"] == "writer-A"  # unchanged
    assert row["lease_epoch"] == 1
    # No dispatch was spawned by a second writer.
    assert len(env.launch.spawns) == 0
    env.core.close()


def test_ambiguous_writer_quarantine_lost_no_claim(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env)
    env.core._store._update_supervisor_job(
        jid, primary_state="RUNNING", status="ACTIVE",
        owner_instance_id="writer-A", lease_epoch=1,
    )  # NULL lease_expires_at -> ambiguous
    sched_b = Scheduler(env.sup, owner_instance_id="writer-B", lease_ttl_seconds=60)
    summary = sched_b.reconcile_after_restart()
    assert summary.quarantined_lost == 1
    assert job_row(env.core, jid)["primary_state"] == "LOST"
    with pytest.raises(LeaseError):
        env.sup.store.claim_job(jid, owner_instance_id="writer-B", ttl_seconds=60)
    assert len(env.launch.spawns) == 0
    env.core.close()
