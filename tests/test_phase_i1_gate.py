"""Phase I1 — scheduler/store/resource-governor integration tests.

Covers the concurrency gate, aggregate resource admission, dependency
enforcement, restart/recovery independence, fairness, and the action-lock
boundary.  Deterministic: no live DB, no network, no LLM.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from argent_core import Core, OWNER_SOURCE
from argent_core import job_state
from argent_core.concurrency_policy import (
    ConcurrencyReasonCode,
    ConcurrencyVerdict,
)
from argent_core.models import LeaseError, LeaseFencedError, WorktreeConflictError
from argent_core.resource_governor import (
    AdmissionVerdict,
    ResourceGovernor,
    ResourceReasonCode,
)
from argent_core.resource_policy import ResourceClass, gib
from argent_core.resource_recovery import (
    FailureClass,
    classify_failure,
    is_resource_failure,
)
from argent_core.scheduler import (
    OUTCOME_CONCURRENCY_BLOCKED,
    OUTCOME_CONCURRENCY_SERIALIZED,
    Scheduler,
)
from argent_core.supervisor import Supervisor
from c1_helpers import make_snapshot
from mock_supervisor_runtime import FakeClock, FakeRunLauncher, FakeRunStatusProvider

OWNER = OWNER_SOURCE


def make_env(db_path, clock=None):
    clock = clock or FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock)
    return SimpleNamespace(core=core, project=project, sup=sup, clock=clock)


def add_job(env, title, *, rc="LIGHT"):
    task = env.core.create_task(env.project.id, title, OWNER)
    job = env.sup.store.create_job(
        task.id, idempotency_key=f"k-{title}", resource_class=rc,
    )
    return job.supervisor_job_id


def row(env, job_id):
    return env.core._store.get_supervisor_job(job_id)


def claim_meta(env, job_id, *, owner="A", ttl=3600, requeue=False, **meta):
    """Lease a job and set its trusted mutation metadata under the lease
    (F5 supervisor-authorized + lease-fenced setter).  Returns the lease epoch.

    With ``requeue=True`` the job is re-enqueued back to QUEUED (metadata
    persists) so a later fresh claim re-runs the structural gate against it.
    """
    claimed = env.sup.store.claim_job(
        job_id, owner_instance_id=owner, ttl_seconds=ttl)
    epoch = claimed["lease_epoch"]
    if meta:
        env.sup.store.set_job_metadata(
            job_id, owner_instance_id=owner, lease_epoch=epoch, **meta)
    if requeue:
        env.sup.store.enqueue_job(
            job_id, queue_reason="NEW", owner_instance_id=owner, lease_epoch=epoch)
    return epoch


# ---------------------------------------------------------------------------
# Case 4/18: same-worktree writers serialize at the scheduler gate
# ---------------------------------------------------------------------------

def test_case4_scheduler_serializes_same_worktree_writers(db_path):
    env = make_env(db_path)
    j1 = add_job(env, "w1", rc="MEDIUM")
    j2 = add_job(env, "w2", rc="MEDIUM")
    # Lease each job and set its trusted same-worktree footprint; j2 is
    # re-enqueued so its fresh claim re-runs the structural gate against the
    # RUNNING j1 (the authoritative writer on /wt/A).
    claim_meta(env, j1, repo_identity="repo-A",
               canonical_worktree_path="/wt/A", branch_identity="f1",
               mutation_path_roots=["src/a"])
    claim_meta(env, j2, repo_identity="repo-A",
               canonical_worktree_path="/wt/A", branch_identity="f2",
               mutation_path_roots=["src/b"], requeue=True)
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60)
    r2 = sched.run_pass(j2)
    assert r2.outcome == OUTCOME_CONCURRENCY_SERIALIZED
    assert r2.detail == ConcurrencyReasonCode.WORKTREE_CONFLICT.value
    assert row(env, j2)["primary_state"] == "QUEUED"
    assert row(env, j2)["queue_reason"] == "CONCURRENCY_SERIALIZED"
    env.core.close()


def test_case3_scheduler_allows_distinct_worktrees(db_path):
    env = make_env(db_path)
    j1 = add_job(env, "w1", rc="MEDIUM")
    j2 = add_job(env, "w2", rc="MEDIUM")
    claim_meta(env, j1, repo_identity="repo-A",
               canonical_worktree_path="/wt/A", branch_identity="f1",
               mutation_path_roots=["src/a"])
    claim_meta(env, j2, repo_identity="repo-B",
               canonical_worktree_path="/wt/B", branch_identity="f1",
               mutation_path_roots=["src/b"], requeue=True)
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60)
    # j2 structurally eligible (distinct repo/worktree); the gate does NOT
    # serialize it (the resource governor would then cap writers at 1).
    r2 = sched.run_pass(j2)
    assert r2.outcome != OUTCOME_CONCURRENCY_SERIALIZED
    env.core.close()


# ---------------------------------------------------------------------------
# Case 8/10: aggregate resource admission
# ---------------------------------------------------------------------------

def test_case8_aggregate_reservation_defers_when_active_jobs_consume_memory():
    gov = ResourceGovernor()
    # mem_available 4.5 GiB; reserve 1.6 GiB; one active LIGHT (1 GiB ceiling).
    # Per-job rule: 4.5 - 2.5 = 2.0 >= 1.6 -> ALLOW.
    # Aggregate rule: 4.5 - 1.0 - 2.5 = 1.0 < 1.6 -> DEFER.
    snap = make_snapshot(
        mem_total=gib(8), mem_available=gib(4.5),
        active_jobs=[("j1", "LIGHT")],
    )
    d = gov.decide(
        resource_class=ResourceClass.MEDIUM, snapshot=snap,
        now_iso="2026-09-01T00:00:00+00:00",
    )
    assert d.decision == AdmissionVerdict.DEFER.value
    assert d.reason_code == ResourceReasonCode.INSUFFICIENT_MEMORY_RESERVE.value


def test_case10_safe_light_runs_while_unrelated_capacity_remains():
    gov = ResourceGovernor()
    snap = make_snapshot(
        mem_total=gib(8), mem_available=gib(5),
        active_jobs=[("j1", "LIGHT")],
    )
    d = gov.decide(
        resource_class=ResourceClass.LIGHT, snapshot=snap,
        now_iso="2026-09-01T00:00:00+00:00",
    )
    assert d.decision == AdmissionVerdict.ALLOW.value


def test_case9_heavy_and_exclusive_alone_via_governor():
    gov = ResourceGovernor()
    now = "2026-09-01T00:00:00+00:00"
    # EXCLUSIVE is blocked by ANY active job.
    for active in ([("j1", "LIGHT")], [("j1", "MEDIUM")], [("j1", "HEAVY")]):
        d = gov.decide(
            resource_class=ResourceClass.EXCLUSIVE,
            snapshot=make_snapshot(active_jobs=active), now_iso=now,
        )
        assert d.decision == AdmissionVerdict.DEFER.value
        assert d.reason_code == ResourceReasonCode.CONCURRENCY_LIMIT.value
    # HEAVY is gated by the single-writer budget (blocked by another writer).
    d = gov.decide(
        resource_class=ResourceClass.HEAVY,
        snapshot=make_snapshot(active_jobs=[("j1", "MEDIUM")]), now_iso=now,
    )
    assert d.decision == AdmissionVerdict.DEFER.value


# ---------------------------------------------------------------------------
# Case 11: provider/resource failure != code failure
# ---------------------------------------------------------------------------

def test_case11_resource_failure_is_not_code_failure():
    fc = classify_failure(
        "ENFORCEMENT_UNAVAILABLE", exit_code=None, timed_out=False,
    )
    assert fc == FailureClass.RESOURCE_ENFORCEMENT_FAILURE
    assert is_resource_failure(fc) is True
    assert fc.value != FailureClass.CODE_OR_PROCESS_FAILURE.value
    # A plain non-zero exit is NOT a resource failure.
    fc2 = classify_failure("NONZERO_EXIT", exit_code=1, timed_out=False)
    assert fc2 == FailureClass.CODE_OR_PROCESS_FAILURE
    assert is_resource_failure(fc2) is False


# ---------------------------------------------------------------------------
# Case 12/13: dependency enforcement at the store claim level
# ---------------------------------------------------------------------------

def test_case12_dependency_prevents_premature_claim(db_path):
    env = make_env(db_path)
    ja = add_job(env, "a", rc="LIGHT")
    jb = add_job(env, "b", rc="LIGHT")
    claim_meta(env, jb, depends_on=ja, requeue=True)
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60)
    # B must not be executed while A is not DONE: the concurrency gate DEFERs
    # it (DEPENDENCY_NOT_MET) and requeues it QUEUED (no spawn).
    r = sched.run_pass(jb)
    assert r.outcome == OUTCOME_CONCURRENCY_SERIALIZED
    assert r.detail == ConcurrencyReasonCode.DEPENDENCY_NOT_MET.value
    assert row(env, jb)["primary_state"] == "QUEUED"
    env.core.close()


def test_case13_completed_dependency_wakes_dependent_exactly_once(db_path):
    env = make_env(db_path)
    ja = add_job(env, "a", rc="LIGHT")
    jb = add_job(env, "b", rc="LIGHT")
    # Lease jb and set its dependency while RUNNING.
    claim_meta(env, jb, depends_on=ja)
    # Mark A DONE (terminal success).
    env.core._store._update_supervisor_job(
        ja, status="TERMINAL", terminal="DONE", next_action="NONE",
    )
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60)
    r = sched.run_pass(jb)
    assert r.outcome != "no_work"
    assert row(env, jb)["primary_state"] == "RUNNING"
    assert row(env, jb)["lease_epoch"] == 1
    # A second claim attempt must not re-claim (already RUNNING).
    assert sched.run_pass(jb).outcome != "no_work"  # continuation, not re-claim
    assert row(env, jb)["lease_epoch"] == 1  # epoch unchanged -> exactly once
    env.core.close()


def test_missing_dependency_blocks_conservatively(db_path):
    env = make_env(db_path)
    jb = add_job(env, "b", rc="LIGHT")
    claim_meta(env, jb, depends_on="supervisor:does-not-exist", requeue=True)
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60)
    # Explicit claim -> concurrency gate BLOCKs on a missing prerequisite.
    r = sched.run_pass(jb)
    assert r.outcome == OUTCOME_CONCURRENCY_BLOCKED
    assert r.detail == ConcurrencyReasonCode.DEPENDENCY_UNKNOWN.value
    assert row(env, jb)["terminal"] == "BLOCKED"
    env.core.close()


def test_missing_dependency_blocked_at_claim_next_job(db_path):
    env = make_env(db_path)
    jb = add_job(env, "b", rc="LIGHT")
    claim_meta(env, jb, depends_on="supervisor:does-not-exist", requeue=True)
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60)
    # The claim loop BLOCKs the job (terminal) and returns no claimable work.
    assert sched.run_pass().outcome == "no_work"
    assert row(env, jb)["terminal"] == "BLOCKED"
    env.core.close()


# ---------------------------------------------------------------------------
# Case 14–17, 29: restart/recovery independence
# ---------------------------------------------------------------------------

def _claim(env, job_id, owner="A", ttl=3600):
    env.sup.store.claim_job(job_id, owner_instance_id=owner, ttl_seconds=ttl)


def test_case14_restart_two_live_jobs_no_duplicate_spawn(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    j1 = add_job(env, "a", rc="LIGHT")
    j2 = add_job(env, "b", rc="LIGHT")
    _claim(env, j1, "A", 3600)
    _claim(env, j2, "A", 3600)
    e1 = row(env, j1)["lease_epoch"]
    e2 = row(env, j2)["lease_epoch"]
    env.core.close()

    core2 = Core(db_path, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock)
    sched2 = Scheduler(sup2, owner_instance_id="A", lease_ttl_seconds=60)
    summary = sched2.reconcile_after_restart()
    assert summary.rebound == 2
    assert summary.quarantined_lost == 0
    # No re-claim / epoch bump (no duplicate spawn).
    assert core2._store.get_supervisor_job(j1)["lease_epoch"] == e1
    assert core2._store.get_supervisor_job(j2)["lease_epoch"] == e2
    assert core2._store.get_supervisor_job(j1)["owner_instance_id"] == "A"
    assert core2._store.get_supervisor_job(j2)["owner_instance_id"] == "A"
    core2.close()


def test_case15_restart_one_live_one_stale_independent(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    j_live = add_job(env, "live", rc="LIGHT")
    j_stale = add_job(env, "stale", rc="LIGHT")
    _claim(env, j_live, "A", 3600)          # live: valid lease
    _claim(env, j_stale, "A", 1)            # stale: short TTL
    env.clock.advance(60)                    # expire the stale lease
    env.core.close()

    core2 = Core(db_path, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock)
    sched2 = Scheduler(sup2, owner_instance_id="A", lease_ttl_seconds=60)
    summary = sched2.reconcile_after_restart()
    # live -> rebound; stale (expired, no process evidence) -> LOST quarantine.
    assert summary.rebound == 1
    assert summary.quarantined_lost == 1
    assert core2._store.get_supervisor_job(j_live)["primary_state"] == "RUNNING"
    assert core2._store.get_supervisor_job(j_stale)["primary_state"] == "LOST"
    core2.close()


def test_case16_ambiguous_A_does_not_steal_B_lease(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    ja = add_job(env, "a", rc="LIGHT")
    jb = add_job(env, "b", rc="LIGHT")
    _claim(env, ja, "A", 1)                 # A: expired -> ambiguous -> LOST
    _claim(env, jb, "B", 3600)              # B: valid foreign lease
    env.clock.advance(60)
    env.core.close()

    core2 = Core(db_path, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock)
    sched2 = Scheduler(sup2, owner_instance_id="A", lease_ttl_seconds=60)
    summary = sched2.reconcile_after_restart()
    # A ambiguous -> LOST; B foreign valid lease -> kept untouched.
    assert summary.quarantined_lost == 1
    assert summary.foreign_lease_kept == 1
    assert core2._store.get_supervisor_job(ja)["primary_state"] == "LOST"
    assert core2._store.get_supervisor_job(jb)["primary_state"] == "RUNNING"
    assert core2._store.get_supervisor_job(jb)["owner_instance_id"] == "B"
    core2.close()


def test_case17_terminal_A_immutable_while_B_recovers(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    ja = add_job(env, "a", rc="LIGHT")
    jb = add_job(env, "b", rc="LIGHT")
    env.core._store._update_supervisor_job(
        ja, status="TERMINAL", terminal="DONE", next_action="NONE",
    )
    _claim(env, jb, "A", 1)
    env.clock.advance(60)
    env.core.close()

    core2 = Core(db_path, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock)
    sched2 = Scheduler(sup2, owner_instance_id="A", lease_ttl_seconds=60)
    summary = sched2.reconcile_after_restart()
    assert summary.quarantined_lost == 1  # B recovered (LOST)
    assert core2._store.get_supervisor_job(ja)["terminal"] == "DONE"  # A immutable
    core2.close()


def test_case29_reconcile_idempotent_with_multiple_jobs(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    j1 = add_job(env, "a", rc="LIGHT")
    j2 = add_job(env, "b", rc="LIGHT")
    _claim(env, j1, "A", 3600)
    _claim(env, j2, "A", 3600)
    env.core.close()

    core2 = Core(db_path, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock)
    sched2 = Scheduler(sup2, owner_instance_id="A", lease_ttl_seconds=60)
    s1 = sched2.reconcile_after_restart()
    s2 = sched2.reconcile_after_restart()
    assert s1.rebound == 2 and s2.rebound == 2
    assert s1.quarantined_lost == s2.quarantined_lost == 0
    core2.close()


# ---------------------------------------------------------------------------
# Case 19: stale lease epoch cannot mutate parallel state
# ---------------------------------------------------------------------------

def test_case19_stale_lease_epoch_cannot_mutate(db_path):
    env = make_env(db_path)
    j = add_job(env, "a", rc="LIGHT")
    _claim(env, j, "A", 3600)  # epoch -> 1
    # A stale holder (epoch 0) cannot requeue/mutate.
    with pytest.raises(LeaseError):
        env.sup.store.enqueue_job(
            j, queue_reason="NEW", owner_instance_id="A", lease_epoch=0,
        )
    assert row(env, j)["primary_state"] == "RUNNING"
    env.core.close()


# ---------------------------------------------------------------------------
# Case 24/25/26/27: fairness, backoff, external wait, failing job isolation
# ---------------------------------------------------------------------------

def test_case24_blocked_heavy_does_not_starve_light(db_path):
    env = make_env(db_path)
    j_heavy = add_job(env, "heavy", rc="HEAVY")
    j_light = add_job(env, "light", rc="LIGHT")
    # Heavy blocked by a missing dependency (conservative BLOCK).
    claim_meta(env, j_heavy, depends_on="supervisor:missing", requeue=True)
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60)
    r = sched.run_pass()  # claims the LIGHT job (heavy is blocked, not claimable)
    assert r.job_id == j_light
    assert row(env, j_light)["primary_state"] == "RUNNING"
    env.core.close()


def test_case24b_fifo_order_preserved(db_path):
    env = make_env(db_path)
    j1 = add_job(env, "first", rc="LIGHT")
    j2 = add_job(env, "second", rc="LIGHT")
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60)
    # claim_next_job returns the earliest rowid (FIFO) among equal priority.
    r = sched.run_pass()
    assert r.job_id == j1
    env.core.close()


def test_case25_retry_backoff_consumes_no_active_slot(db_path):
    env = make_env(db_path)
    j = add_job(env, "a", rc="LIGHT")
    future = (env.clock() + timedelta(seconds=1000)).astimezone().isoformat()
    env.sup.store.enqueue_job(j, queue_reason="RETRY_BACKOFF",
                              next_eligible_at=future)
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60)
    assert sched.run_pass(j).outcome == "no_work"  # not eligible
    # Not RUNNING -> not in the active set.
    assert env.sup.store.list_active_job_facts() == []
    env.core.close()


def test_case26_waiting_external_releases_capacity(db_path):
    env = make_env(db_path)
    j1 = add_job(env, "waiting", rc="LIGHT")
    j2 = add_job(env, "run", rc="LIGHT")
    env.core._store._update_supervisor_job(
        j1, primary_state="WAITING_EXTERNAL", status="WAITING_RUN",
        wait_kind="CI",
    )
    # WAITING_EXTERNAL is not RUNNING -> excluded from active set.
    assert env.sup.store.list_active_job_facts() == []
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60)
    r = sched.run_pass(j2)
    assert r.outcome != "no_work"
    assert row(env, j2)["primary_state"] == "RUNNING"
    env.core.close()


def test_case27_failing_job_does_not_fail_unrelated(db_path):
    env = make_env(db_path)
    j_fail = add_job(env, "fail", rc="LIGHT")
    j_ok = add_job(env, "ok", rc="LIGHT")
    env.core._store._update_supervisor_job(
        j_fail, status="TERMINAL", terminal="FAILED", next_action="NONE",
    )
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60)
    r = sched.run_pass(j_ok)
    assert r.outcome != "no_work"
    assert row(env, j_ok)["primary_state"] == "RUNNING"
    assert row(env, j_fail)["terminal"] == "FAILED"
    env.core.close()


# ---------------------------------------------------------------------------
# Case 28: shutdown stops new admission
# ---------------------------------------------------------------------------

def test_case28_shutdown_stops_new_admission(db_path):
    from g1_helpers import add_queued_job, make_runtime_env
    clock = FakeClock()
    env = make_runtime_env(db_path, clock=clock)
    j1 = add_queued_job(env, "a")
    j2 = add_queued_job(env, "b")
    # Request shutdown BEFORE the loop runs -> no pass, no claim.
    env.runtime.request_shutdown("test")
    env.runtime.run_loop()
    assert env.core._store.get_supervisor_job(j1)["primary_state"] == "QUEUED"
    assert env.core._store.get_supervisor_job(j2)["primary_state"] == "QUEUED"
    assert env.core._store.get_supervisor_job(j1)["owner_instance_id"] is None
    assert env.core._store.get_supervisor_job(j2)["owner_instance_id"] is None


# ---------------------------------------------------------------------------
# Migration + action-lock boundary
# ---------------------------------------------------------------------------

def test_migration_adds_i1_columns_and_action_locks(db_path):
    core = Core(db_path)
    cols = {r[1] for r in core._store._conn.execute(
        "PRAGMA table_info(supervisor_jobs)")}
    for c in ("mutation_path_roots", "mutation_modules",
              "external_action_class", "integration_target", "action_scope",
              "depends_on", "last_concurrency_reason_code",
              "last_concurrency_at"):
        assert c in cols
    tables = {r[0] for r in core._store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "action_locks" in tables
    core.close()


def test_set_job_metadata_roundtrip(db_path):
    env = make_env(db_path)
    j = add_job(env, "a", rc="MEDIUM")
    claim_meta(env, j, repo_identity="repo-A",
               canonical_worktree_path="/wt/A", branch_identity="main",
               mutation_path_roots=["src/a", "tests/a"],
               mutation_modules=["core"], integration_target="main")
    r = row(env, j)
    assert r["repo_identity"] == "repo-A"
    assert r["canonical_worktree_path"] == "/wt/A"
    assert "src/a" in r["mutation_path_roots"]
    assert r["integration_target"] == "main"
    env.core.close()


def test_set_job_metadata_requires_current_lease(db_path):
    env = make_env(db_path)
    j = add_job(env, "a", rc="MEDIUM")
    epoch = claim_meta(env, j, repo_identity="repo-A")
    # A stale epoch / wrong owner / unleased caller must fail closed.
    with pytest.raises(LeaseFencedError):
        env.sup.store.set_job_metadata(
            j, owner_instance_id="A", lease_epoch=epoch + 1, repo_identity="repo-B")
    with pytest.raises(LeaseFencedError):
        env.sup.store.set_job_metadata(
            j, owner_instance_id="B", lease_epoch=epoch, repo_identity="repo-B")
    # The metadata write bumped facts_version.
    assert row(env, j)["facts_version"] >= 1
    assert row(env, j)["repo_identity"] == "repo-A"  # unchanged by refused writes
    env.core.close()


def test_action_lock_cas_boundary(db_path):
    env = make_env(db_path)
    st = env.sup.store
    # Two leased (RUNNING) jobs with current leases (real jobs, not phantoms).
    j1 = add_job(env, "lock-a", rc="LIGHT")
    j2 = add_job(env, "lock-b", rc="LIGHT")
    e1 = claim_meta(env, j1)
    e2 = claim_meta(env, j2, owner="B")
    assert st.try_acquire_action_lock("global:merge", job_id=j1,
                                      lease_epoch=e1) is True
    # Same holder re-entrant.
    assert st.try_acquire_action_lock("global:merge", job_id=j1,
                                      lease_epoch=e1) is True
    # Another VALID holder refused (j2 holds a current lease, different job).
    assert st.try_acquire_action_lock("global:merge", job_id=j2,
                                      lease_epoch=e2) is False
    # Wrong holder cannot release.
    assert st.release_action_lock("global:merge", job_id=j2,
                                  lease_epoch=e2) is False
    # Correct holder releases.
    assert st.release_action_lock("global:merge", job_id=j1,
                                  lease_epoch=e1) is True
    assert st.try_acquire_action_lock("global:merge", job_id=j2,
                                      lease_epoch=e2) is True
    env.core.close()


# ---------------------------------------------------------------------------
# F1 — spawn-time admission must NOT count the candidate itself (Sol round)
# ---------------------------------------------------------------------------

def _limits():
    from argent_core.resource_policy import ResourcePolicy
    pol = ResourcePolicy()
    base = pol.limits_for(ResourceClass.LIGHT)
    return {
        "memory_high_bytes": base.memory_high_bytes,
        "memory_max_bytes": base.memory_max_bytes,
        "swap_max_bytes": base.swap_max_bytes,
        "cpu_quota_percent": base.cpu_quota_percent,
        "timeout_seconds": base.timeout_seconds,
    }


class _StoreBackedSnapshotProvider:
    """Deterministic snapshot whose ``active_jobs`` come from the REAL store
    reader (so the active set reflects actually-RUNNING jobs)."""

    def __init__(self, reader):
        self._reader = reader
        self.captures = []

    def capture(self, workspace_path=None):
        active = self._reader()
        return make_snapshot(active_jobs=active or ())


def _drive_to_spawn(sched, jid, max_passes=15):
    final = None
    for _ in range(max_passes):
        r = sched.run_pass(jid)
        final = r
        if r.outcome in ("resource_deferred", "resource_denied",
                         "concurrency_serialized", "concurrency_blocked"):
            break
    return final


def test_f1_second_light_job_reaches_spawn_while_first_running(db_path):
    from c2_helpers import FakeScopeBackend, verified_properties
    from argent_core.scope_enforcer import ExecutionEnforcer

    clock = FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    t1 = core.create_task(project.id, "t1", OWNER)
    t2 = core.create_task(project.id, "t2", OWNER)
    core.start_task_run(t1.id, OWNER)
    core.start_task_run(t2.id, OWNER)
    backend = FakeScopeBackend(verify_properties=verified_properties(_limits()))
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher(),
                     clock=clock, enforcer=ExecutionEnforcer(backend))
    j1 = sup.store.create_job(t1.id, idempotency_key="j1",
                              resource_class=ResourceClass.LIGHT.value)
    j2 = sup.store.create_job(t2.id, idempotency_key="j2",
                              resource_class=ResourceClass.LIGHT.value)
    jid1, jid2 = j1.supervisor_job_id, j2.supervisor_job_id
    sched = Scheduler(
        sup, owner_instance_id="A", lease_ttl_seconds=60,
        snapshot_provider=_StoreBackedSnapshotProvider(
            sup._default_active_jobs_reader()),
    )
    # First LIGHT job reaches spawn (one scope created).
    _drive_to_spawn(sched, jid1)
    assert len(backend.created) == 1
    assert core._store.get_supervisor_job(jid1)["primary_state"] == "RUNNING"
    # Second LIGHT job, while the first is RUNNING, is ADMITTED and REACHES
    # spawn: no self-count in the LIGHT concurrency limit, no double
    # subtraction of its own ceiling in the aggregate reserve.
    _drive_to_spawn(sched, jid2)
    assert len(backend.created) == 2, (
        "second LIGHT job did not reach spawn (self-blocked?)")
    assert core._store.get_supervisor_job(jid2)["primary_state"] == "RUNNING"
    core.close()


# ---------------------------------------------------------------------------
# F2 — spawn re-gate catches a role/footprint transition (continuation)
# ---------------------------------------------------------------------------

def test_f2_spawn_regate_catches_role_footprint_transition(db_path):
    from c2_helpers import FakeGovernor, FakeScopeBackend, FakeSnapshotProvider, verified_properties
    from argent_core.resource_governor import AdmissionDecision, ResourceReasonCode
    from argent_core.scope_enforcer import ExecutionEnforcer

    clock = FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    t1 = core.create_task(project.id, "t1", OWNER)
    t2 = core.create_task(project.id, "t2", OWNER)
    core.start_task_run(t1.id, OWNER)
    core.start_task_run(t2.id, OWNER)
    backend = FakeScopeBackend(verify_properties=verified_properties(_limits()))
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher(),
                     clock=clock, enforcer=ExecutionEnforcer(backend))
    j1 = sup.store.create_job(t1.id, idempotency_key="j1",
                              resource_class=ResourceClass.MEDIUM.value)
    j2 = sup.store.create_job(t2.id, idempotency_key="j2",
                              resource_class=ResourceClass.LIGHT.value)
    jid1, jid2 = j1.supervisor_job_id, j2.supervisor_job_id

    gov = FakeGovernor(AdmissionDecision(
        resource_class=ResourceClass.LIGHT.value, policy_version="1",
        snapshot_ref="s", decision=AdmissionVerdict.ALLOW.value,
        reason_code=ResourceReasonCode.OK.value, effective_limits={},
        timestamp="2026-01-01T00:00:00+00:00",
    ))
    sched = Scheduler(sup, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=gov,
                      snapshot_provider=FakeSnapshotProvider())

    # j1 becomes the active writer on /wt/A (lease + footprint).
    e1 = sup.store.claim_job(jid1, owner_instance_id="A",
                             ttl_seconds=3600)["lease_epoch"]
    sup.store.set_job_metadata(jid1, owner_instance_id="A", lease_epoch=e1,
                               repo_identity="repo-A",
                               canonical_worktree_path="/wt/A",
                               branch_identity="f1", mutation_path_roots=["src/a"])

    # First claim of j2 (LIGHT, read-only role) is structurally allowed.
    r = sched.run_pass(jid2)
    assert r.outcome != OUTCOME_CONCURRENCY_SERIALIZED
    assert core._store.get_supervisor_job(jid2)["primary_state"] == "RUNNING"

    # Transition BEFORE spawn: j2 becomes a writer (MEDIUM) on the SAME
    # worktree /wt/A — a role/footprint change that must NOT slip past the
    # spawn re-gate as read-only.
    core._store._update_supervisor_job(jid2, resource_class="MEDIUM")
    epoch = core._store.get_supervisor_job(jid2)["lease_epoch"]
    sup.store.set_job_metadata(jid2, owner_instance_id="A", lease_epoch=epoch,
                               repo_identity="repo-A",
                               canonical_worktree_path="/wt/A",
                               branch_identity="f2", mutation_path_roots=["src/b"])

    # Drive toward spawn: the spawn re-gate must SERIALIZE j2 (WORKTREE_CONFLICT).
    final = None
    for _ in range(12):
        final = sched.run_pass(jid2)
        if final.outcome == OUTCOME_CONCURRENCY_SERIALIZED:
            break
    assert final is not None
    assert final.outcome == OUTCOME_CONCURRENCY_SERIALIZED
    assert final.detail == ConcurrencyReasonCode.WORKTREE_CONFLICT.value
    core.close()


# ---------------------------------------------------------------------------
# F3 — hard ONE-worktree = ONE-authoritative-writer-lease store invariant
# ---------------------------------------------------------------------------

def test_f3_one_writer_per_worktree_hard_invariant(db_path):
    env = make_env(db_path)
    j1 = add_job(env, "w1", rc="MEDIUM")
    j2 = add_job(env, "w2", rc="MEDIUM")
    j3 = add_job(env, "w3", rc="MEDIUM")
    e1 = claim_meta(env, j1)
    e2 = claim_meta(env, j2)
    e3 = claim_meta(env, j3)
    st = env.core._store  # the raw Store (bind_writer_worktree lives there)

    st.bind_writer_worktree(
        j1, dispatch_id="d1", owner_instance_id="A", lease_epoch=e1,
        repo_identity="repo-A", base_commit="0" * 40, branch_identity="f1",
        canonical_worktree_path="/wt/A",
    )
    # A second job cannot bind the same worktree (hard invariant).
    with pytest.raises(WorktreeConflictError):
        st.bind_writer_worktree(
            j2, dispatch_id="d2", owner_instance_id="A", lease_epoch=e2,
            repo_identity="repo-A", base_commit="0" * 40, branch_identity="f2",
            canonical_worktree_path="/wt/A",
        )
    # A different worktree binds fine.
    st.bind_writer_worktree(
        j2, dispatch_id="d2", owner_instance_id="A", lease_epoch=e2,
        repo_identity="repo-A", base_commit="0" * 40, branch_identity="f2",
        canonical_worktree_path="/wt/B",
    )
    # Releasing j1 (terminal) frees /wt/A for a fresh job.
    env.core._store._update_supervisor_job(
        j1, status="TERMINAL", terminal="DONE", next_action="NONE",
    )
    st.bind_writer_worktree(
        j3, dispatch_id="d3", owner_instance_id="A", lease_epoch=e3,
        repo_identity="repo-A", base_commit="0" * 40, branch_identity="f3",
        canonical_worktree_path="/wt/A",
    )
    env.core.close()


# ---------------------------------------------------------------------------
# F5 — action-lock durability / lease fencing / reclaim
# ---------------------------------------------------------------------------

def test_f5_action_lock_rejects_phantom_and_stale(db_path):
    env = make_env(db_path)
    st = env.sup.store
    j = add_job(env, "a", rc="LIGHT")
    e = claim_meta(env, j)
    # Phantom job id rejected (no such non-terminal leased job).
    with pytest.raises(LeaseFencedError):
        st.try_acquire_action_lock("global:merge", job_id="supervisor:phantom",
                                   lease_epoch=1)
    # Stale epoch rejected (job holds a different current epoch).
    with pytest.raises(LeaseFencedError):
        st.try_acquire_action_lock("global:merge", job_id=j, lease_epoch=e + 1)
    env.core.close()


def test_f5_action_lock_expired_holder_reclaimed(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    st = env.sup.store
    j1 = add_job(env, "a", rc="LIGHT")
    j2 = add_job(env, "b", rc="LIGHT")
    e1 = claim_meta(env, j1, ttl=1)  # short lease
    assert st.try_acquire_action_lock("repo:r:merge", job_id=j1,
                                      lease_epoch=e1) is True
    clock.advance(60)  # expire j1's lease
    e2 = claim_meta(env, j2, owner="B")
    # j1's lock is stale (holder lease expired) -> j2 reclaims atomically.
    assert st.try_acquire_action_lock("repo:r:merge", job_id=j2,
                                      lease_epoch=e2) is True
    env.core.close()


def test_f5_action_lock_reassigned_holder_reclaimed(db_path):
    env = make_env(db_path)
    st = env.sup.store
    j1 = add_job(env, "a", rc="LIGHT")
    j2 = add_job(env, "b", rc="LIGHT")
    e1 = claim_meta(env, j1)
    assert st.try_acquire_action_lock("global:merge", job_id=j1,
                                      lease_epoch=e1) is True
    # Simulate a restart: j1 is requeued + re-claimed with a NEW epoch, so the
    # old (j1, e1) lock holder no longer holds a current lease.
    env.sup.store.enqueue_job(j1, queue_reason="NEW", owner_instance_id="A",
                              lease_epoch=e1)
    e1b = env.sup.store.claim_job(j1, owner_instance_id="A",
                                  ttl_seconds=3600)["lease_epoch"]
    assert e1b != e1
    e2 = claim_meta(env, j2, owner="B")
    # A different valid holder can now reclaim the stale lock.
    assert st.try_acquire_action_lock("global:merge", job_id=j2,
                                      lease_epoch=e2) is True
    env.core.close()
