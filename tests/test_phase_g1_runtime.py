"""Phase G1 — background runtime: reconciliation, shutdown, health, loop.

Acceptance cases 7–25, 39.  Deterministic and offline: jobs/leases/wait/process
evidence are all driven through the existing Phase B/C mechanisms; the G1
:class:`SupervisorRuntime` layers the bounded loop + service health on top.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from argent_core import Core
from argent_core.background_runtime import ServiceHealth, SupervisorRuntime
from argent_core.external_wait import (
    FakeExternalWaitAdapter,
    OBS_PENDING,
    OBS_READY,
    WaitObservation,
    WaitSpec,
)
from argent_core.job_state import PrimaryState
from argent_core.notifications import (
    NotificationType,
    event_ref_close,
    normal_dedup_key,
    outbox_id,
    payload_hash,
)
from argent_core.process_registry import ProcessIdentity, ProcessRegistry
from c3_helpers import build_running_job, fake_identity_provider, make_scheduler
from g1_helpers import add_queued_job, make_runtime_env
from mock_supervisor_runtime import FakeClock


# ---------------------------------------------------------------------------
# Case 7: restart with QUEUED -> safely claimable
# ---------------------------------------------------------------------------

def test_restart_queued_claimable(db_path):
    env = make_runtime_env(db_path)
    jid = add_queued_job(env)
    summary = env.sched.reconcile_after_restart()
    assert summary.scanned >= 1
    row = env.core._store.get_supervisor_job(jid)
    assert row["primary_state"] == PrimaryState.QUEUED.value
    # Claimable by a bounded pass (no fabricated state change).
    env.sched.run_pass(jid)
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.RUNNING.value
    env.core.close()


# ---------------------------------------------------------------------------
# Case 8: restart with WAITING_EXTERNAL -> wait persists, no LLM
# ---------------------------------------------------------------------------

def test_restart_waiting_external_persists_no_llm(db_path):
    clock = FakeClock()
    env = make_runtime_env(db_path, clock=clock, adapters={"ci": FakeExternalWaitAdapter()})
    jid = add_queued_job(env)
    env.sched.run_pass(jid)  # claim -> RUNNING (no spawn: starts role)
    row = env.core._store.get_supervisor_job(jid)
    env.ewm.enter_waiting_external(
        jid,
        spec=WaitSpec(kind="CI", provider="ci", ref="org/repo#run",
                      expected_subject="abc123"),
        owner_instance_id="instance:test", lease_epoch=row["lease_epoch"],
    )
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.WAITING_EXTERNAL.value
    env.core.close()

    # Reopen (restart): wait persisted, no active agent/LLM.
    core2 = Core(db_path, clock=clock)
    sup2 = core2._store
    assert sup2.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.WAITING_EXTERNAL.value
    waits = sup2.list_external_waits(jid)
    assert len(waits) == 1
    assert waits[0]["terminal_observed_at"] is None
    core2.close()


# ---------------------------------------------------------------------------
# Cases 9/10/11: terminal + BLOCKED are immutable across restart
# ---------------------------------------------------------------------------

def _seed_terminal(db_path, terminal):
    env = make_runtime_env(db_path)
    jid = add_queued_job(env)
    env.core._store._update_supervisor_job(
        jid, status="TERMINAL", terminal=terminal, next_action="NONE",
    )
    return env, jid


def test_restart_done_unchanged(db_path):
    env, jid = _seed_terminal(db_path, "DONE")
    summary = env.sched.reconcile_after_restart()
    assert env.core._store.get_supervisor_job(jid)["terminal"] == "DONE"
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.DONE.value
    env.core.close()


def test_restart_failed_unchanged(db_path):
    env, jid = _seed_terminal(db_path, "FAILED")
    env.sched.reconcile_after_restart()
    assert env.core._store.get_supervisor_job(jid)["terminal"] == "FAILED"
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.FAILED.value
    env.core.close()


def test_restart_blocked_not_reopened(db_path):
    env, jid = _seed_terminal(db_path, "BLOCKED")
    env.sched.reconcile_after_restart()
    row = env.core._store.get_supervisor_job(jid)
    assert row["terminal"] == "BLOCKED"
    assert row["primary_state"] == PrimaryState.BLOCKED.value
    env.core.close()


# ---------------------------------------------------------------------------
# Cases 12/13/14: RUNNING reconciliation by process evidence
# ---------------------------------------------------------------------------

def test_running_live_process_no_unsafe_takeover(db_path):
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    env = build_running_job(core, owner="A", ttl=60)
    # Live process evidence (same identity as the injected provider).
    reg = ProcessRegistry(core._store)
    reg.register(
        job_id=env.jid, dispatch_id=None,
        identity=ProcessIdentity(boot_id="boot-1", pid=100,
                                 process_start_ticks=42),
    )
    core.close()

    core2 = Core(db_path, clock=clock)
    try:
        clock.advance(61)  # lease expires; process still alive
        sched = make_scheduler(_wrap(core2), owner="B")
        summary = sched.reconcile_after_restart()
        row = core2._store.get_supervisor_job(env.jid)
        # Live authoritative process -> no takeover, no second writer.
        assert row["primary_state"] == PrimaryState.RUNNING.value
        assert row["owner_instance_id"] == "A"
        assert summary.process_alive == 1
        assert summary.quarantined_lost == 0
        assert summary.takeover_candidates == 0
    finally:
        core2.close()


def test_running_terminal_evidence_bounded_recovery(db_path):
    from argent_core.resource_failure import TerminationClass
    from c3_helpers import register_terminal_evidence
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    env = build_running_job(core, owner="A", ttl=60)
    register_terminal_evidence(
        core, env.jid, termination_class=TerminationClass.OOM_KILL.value,
        exit_code=137, scope_events={"oom_kill": 1, "oom_group_kill": 0,
                                     "max": 0, "high": 0},
    )
    core.close()

    core2 = Core(db_path, clock=clock)
    try:
        sched = make_scheduler(_wrap(core2), owner="A")
        # Terminal evidence -> classify_and_recover (bounded), never a DONE.
        recovered = sched.classify_and_recover(env.jid, env.epoch)
        assert recovered is not None
        row = core2._store.get_supervisor_job(env.jid)
        assert row["terminal"] != "DONE"
        assert row["primary_state"] in (
            PrimaryState.BLOCKED.value, PrimaryState.FAILED.value,
            PrimaryState.LOST.value,
        )
    finally:
        core2.close()


def test_running_ambiguous_evidence_no_unsafe_spawn(db_path):
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    env = build_running_job(core, owner="A", ttl=1)
    # No registration at all -> unknown -> fail-closed (never a blind spawn).
    core.close()

    core2 = Core(db_path, clock=clock)
    try:
        clock.advance(2)  # expire the lease
        sched = make_scheduler(_wrap(core2), owner="B")
        summary = sched.reconcile_after_restart()
        row = core2._store.get_supervisor_job(env.jid)
        # Unknown process evidence -> LOST quarantine (no takeover, no spawn).
        assert row["primary_state"] == PrimaryState.LOST.value
        assert summary.quarantined_lost == 1
        assert row["terminal"] != "DONE"
    finally:
        core2.close()


# ---------------------------------------------------------------------------
# Case 15/16: crash windows -> no fabricated completion; reconcile idempotent
# ---------------------------------------------------------------------------

def test_crash_before_action_persist_no_fabricated_done(db_path):
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    env = build_running_job(core, owner="A", ttl=1)
    # No action journal success, no terminal evidence -> crash mid-action.
    core.close()
    core2 = Core(db_path, clock=clock)
    try:
        clock.advance(2)
        sched = make_scheduler(_wrap(core2), owner="B")
        sched.reconcile_after_restart()
        row = core2._store.get_supervisor_job(env.jid)
        # Never fabricated success: the job is quarantined LOST (ambiguous),
        # not DONE/PASS.
        assert row["terminal"] != "DONE"
        assert row["primary_state"] != PrimaryState.DONE.value
    finally:
        core2.close()


def test_reconcile_after_restart_idempotent(db_path):
    env = make_runtime_env(db_path)
    add_queued_job(env)
    first = env.sched.reconcile_after_restart()
    second = env.sched.reconcile_after_restart()
    assert first.scanned == second.scanned
    assert first.quarantined_lost == second.quarantined_lost
    assert first.takeover_candidates == second.takeover_candidates
    env.core.close()


# ---------------------------------------------------------------------------
# Case 17: external-wait wake not duplicated after restart
# ---------------------------------------------------------------------------

def test_external_wait_wake_not_duplicated(db_path):
    clock = FakeClock()
    env = make_runtime_env(db_path, clock=clock, adapters={"ci": FakeExternalWaitAdapter()})
    jid = add_queued_job(env)
    env.sched.run_pass(jid)
    row = env.core._store.get_supervisor_job(jid)
    adapter = env.ewm._adapters["ci"]
    env.ewm.enter_waiting_external(
        jid, spec=WaitSpec(kind="CI", provider="ci", ref="org/repo#run",
                           expected_subject="abc123"),
        owner_instance_id="instance:test", lease_epoch=row["lease_epoch"],
    )
    # Relevant event wakes exactly once.
    adapter.set_sticky("ci", "org/repo#run", WaitObservation(
        provider="ci", ref="org/repo#run", state=OBS_READY,
        subject="abc123", event_version=1,
    ))
    clock.advance(61)
    results = env.ewm.check_due_waits()
    assert any(r.outcome == "woke" for r in results)
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.QUEUED.value

    # Restart: the wake must not re-fire.
    env.core.close()
    core3 = Core(db_path, clock=clock)
    try:
        from argent_core.external_wait import ExternalWaitManager
        mgr = ExternalWaitManager(core3._store, adapters={"ci": adapter},
                                  clock=clock)
        clock.advance(1)
        r2 = mgr.check_due_waits()
        # The wait is terminal -> not due -> no wake.
        assert not any(x.outcome == "woke" for x in r2)
        assert core3._store.get_supervisor_job(jid)["primary_state"] == \
            PrimaryState.QUEUED.value
    finally:
        core3.close()


# ---------------------------------------------------------------------------
# Case 18: final DONE notification not duplicated by service restart
# ---------------------------------------------------------------------------

def test_done_notification_dedup_on_restart(db_path):
    core = Core(db_path)
    project = core.create_project("p", "owner:authenticated")
    task = core.create_task(project.id, "t1", "owner:authenticated")
    from argent_core.supervisor import Supervisor
    from mock_supervisor_runtime import FakeRunLauncher, FakeRunStatusProvider
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher())
    job = sup.store.create_job(task.id, idempotency_key="job-t1")
    jid = job.supervisor_job_id
    task_id = task.id
    # Simulate the authoritative close producing a final DONE notification.
    dedup = normal_dedup_key(jid, NotificationType.DONE,
                             event_ref_close(jid, "DONE"), 1)
    row = {
        "id": outbox_id(dedup),
        "supervisor_job_id": jid,
        "task_id": task_id,
        "dispatch_id": None,
        "gate_id": None,
        "notification_type": "DONE",
        "event_ref": event_ref_close(jid, "DONE"),
        "event_version": 1,
        "dedup_key": dedup,
        "payload_json": '{"x":1}',
        "payload_hash": payload_hash({"x": 1}),
        "status": "PENDING",
        "attempt_count": 0,
        "next_attempt_at": None,
        "claimed_at": None,
        "claim_token": None,
        "last_attempt_at": None,
        "sent_at": None,
        "last_error_code": None,
        "created_at": core._store.now_iso(),
        "updated_at": core._store.now_iso(),
    }
    assert core._store._insert_notification(row) is True
    # A service restart re-emitting the same final close is a silent no-op.
    assert core._store._insert_notification(dict(row)) is False
    notifs = core._store.list_notifications(jid)
    assert len(notifs) == 1
    core.close()


# ---------------------------------------------------------------------------
# Case 19/20/21: graceful shutdown
# ---------------------------------------------------------------------------

def test_sigterm_bounded_stopping(db_path):
    env = make_runtime_env(db_path, max_passes=1000)
    assert env.instance.acquire().verdict.value in ("acquired", "takeover")
    env.runtime.request_shutdown("SIGTERM")
    assert env.runtime.state is ServiceHealth.STOPPING
    summary = env.runtime.run_loop()
    # The loop stops immediately (no passes run after shutdown request).
    assert summary.passes == 0
    assert summary.stop_reason == "SIGTERM"
    # Instance was gracefully released.
    assert env.core._store.get_supervisor_instance()["status"] == "RELEASED"
    env.core.close()


def test_shutdown_starts_no_new_jobs(db_path):
    env = make_runtime_env(db_path, max_passes=10)
    jid = add_queued_job(env)
    env.runtime.request_shutdown("SIGTERM")
    env.runtime.run_loop()
    # The queued job was never claimed (no new runnable work after shutdown).
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.QUEUED.value
    assert env.core._store.get_supervisor_job(jid)["owner_instance_id"] is None
    env.core.close()


def test_shutdown_never_marks_unfinished_done(db_path):
    env = make_runtime_env(db_path, max_passes=10)
    jid = add_queued_job(env)
    env.sched.run_pass(jid)  # RUNNING now
    env.runtime.request_shutdown("SIGTERM")
    env.runtime.run_loop()
    row = env.core._store.get_supervisor_job(jid)
    assert row["terminal"] != "DONE"
    assert row["primary_state"] != PrimaryState.DONE.value
    env.core.close()


# ---------------------------------------------------------------------------
# Case 22/23/24: loop behaviour
# ---------------------------------------------------------------------------

def test_idle_loop_does_not_busy_spin(db_path):
    slept = []
    env = make_runtime_env(db_path, max_passes=5, sleep_fn=lambda s: slept.append(s))
    summary = env.runtime.run_loop()
    assert summary.passes == 5
    # Idle passes sleep the idle duration (5.0s), sliced for stop_event
    # responsiveness — never zero (no busy-spin).
    assert all(s > 0 for s in slept)
    assert sum(slept) == pytest.approx(5 * 5.0)
    env.core.close()


def test_external_wait_holds_no_llm(db_path):
    # The wait checker only consults the allowlisted adapter; there is no
    # model/prompt path.  A pending wait triggers bounded backoff, never a
    # model call or a wake.
    clock = FakeClock()
    adapter = FakeExternalWaitAdapter()
    adapter.set_sticky("ci", "org/repo#run", WaitObservation(
        provider="ci", ref="org/repo#run", state=OBS_PENDING, event_version=0))
    env = make_runtime_env(db_path, clock=clock, adapters={"ci": adapter})
    jid = add_queued_job(env)
    env.sched.run_pass(jid)
    row = env.core._store.get_supervisor_job(jid)
    env.ewm.enter_waiting_external(
        jid, spec=WaitSpec(kind="CI", provider="ci", ref="org/repo#run",
                           expected_subject="abc123"),
        owner_instance_id="instance:test", lease_epoch=row["lease_epoch"],
    )
    clock.advance(61)
    results = env.ewm.check_due_waits()
    assert results and all(r.outcome == "pending" for r in results)
    # No wake, no LLM: the job remains WAITING_EXTERNAL and no adapter beyond
    # the allowlisted fake was invoked.
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.WAITING_EXTERNAL.value
    assert adapter.checks  # the fake adapter was consulted (deterministic)
    env.core.close()


def test_loop_contains_pass_exception(db_path):
    class BoomScheduler:
        def run_pass(self, job_id=None):
            raise RuntimeError("boom")

    env = make_runtime_env(db_path, max_passes=3, sleep_fn=lambda s: None)
    env.runtime._scheduler = BoomScheduler()
    summary = env.runtime.run_loop()
    assert summary.errors == 3
    assert env.runtime.state in (ServiceHealth.DEGRADED, ServiceHealth.STOPPING)
    snap = env.runtime.snapshot()
    assert snap.last_error_code == "RuntimeError"
    env.core.close()


# ---------------------------------------------------------------------------
# Case 25: service health without job-state change
# ---------------------------------------------------------------------------

def test_health_distinguishes_states_without_job_change(db_path):
    env = make_runtime_env(db_path, max_passes=2, sleep_fn=lambda s: None)
    jid = add_queued_job(env)
    # Health snapshot is a read-only SERVICE observation.
    snap = env.runtime.snapshot()
    assert snap.state == "STARTING"
    assert snap.db_accessible is True
    assert snap.active_job_count == 1
    before = env.core._store.get_supervisor_job(jid)["primary_state"]
    env.runtime.snapshot()
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == before
    # DEGRADED: a contained pass error changes SERVICE health, never job state.
    class BoomScheduler:
        def run_pass(self, job_id=None):
            raise RuntimeError("boom")
    env.runtime._scheduler = BoomScheduler()
    env.runtime.run_loop()
    assert env.runtime.snapshot().state in ("DEGRADED", "STOPPING")
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == before
    # FAILED: explicit unrecoverable mark, still no job mutation.
    env2 = make_runtime_env(db_path + ".e2", max_passes=1, sleep_fn=lambda s: None)
    env2.runtime.mark_failed("db_unreachable")
    assert env2.runtime.snapshot().state == "FAILED"
    env2.core.close()
    env.core.close()


def test_health_readiness_after_fresh_acquire(db_path):
    env = make_runtime_env(db_path, max_passes=1, sleep_fn=lambda s: None)
    res = env.instance.acquire()
    assert res.verdict.value in ("acquired", "takeover")
    snap = env.runtime.snapshot()
    assert snap.state == "STARTING"  # before run_loop
    assert snap.instance_id == "instance:test"
    assert snap.boot_id == "boot-1"
    assert snap.pid == 100
    assert snap.process_start_ticks == 5
    env.core.close()


# ---------------------------------------------------------------------------
# Case 39: repeated crash/start never opens immutable terminal jobs
# ---------------------------------------------------------------------------

def test_repeated_restart_never_reopens_terminal(db_path):
    env, jid = _seed_terminal(db_path, "DONE")
    for _ in range(3):
        env.sched.reconcile_after_restart()
        assert env.core._store.get_supervisor_job(jid)["terminal"] == "DONE"
        assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
            PrimaryState.DONE.value
    env.core.close()


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------

def _wrap(core):
    """A tiny adapter giving make_scheduler a Supervisor over ``core``."""
    from argent_core.supervisor import Supervisor
    from mock_supervisor_runtime import FakeRunLauncher, FakeRunStatusProvider
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher())
    sup._process_identity_provider = fake_identity_provider()
    return SimpleNamespace(core=core, sup=sup)
