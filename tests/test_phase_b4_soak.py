"""Phase B4 — bounded deterministic soak/stress simulation.

Hundreds of controlled state transitions across the durable primitives with
lease renewals, wait/wake cycles and crash/restart interleavings (DB reopen),
asserting the cross-cutting invariants after every step and at the end:

* no duplicate ownership (a RUNNING job has exactly one valid holder);
* no epoch regression (``lease_epoch`` never decreases);
* no terminal reopen (terminal value is sticky);
* no duplicate action journal keys (UNIQUE action_key enforced);
* no duplicate wake (each wait becomes terminal at most once);
* no orphaned writer binding (BOUND => complete binding tuple);
* DB reopen is consistent (facts identical before/after reopen).

No sleep, no network, no real process; time is a ``FakeClock``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from argent_core import Core, OWNER_SOURCE
from argent_core.external_wait import (
    ExternalWaitManager,
    FakeExternalWaitAdapter,
    OBS_READY,
    WaitObservation,
    WaitSpec,
)
from argent_core.job_state import PrimaryState
from argent_core.models import LeaseError
from argent_core.resource_governor import (
    AdmissionDecision,
    AdmissionVerdict,
    ResourceReasonCode,
)
from argent_core.resource_policy import ResourceClass, ResourcePolicy
from argent_core.scheduler import Scheduler
from argent_core.scope_enforcer import ExecutionEnforcer
from argent_core.supervisor import Supervisor
from c2_helpers import FakeGovernor, FakeScopeBackend, FakeSnapshotProvider
from mock_supervisor_runtime import FakeClock, FakeRunLauncher, FakeRunStatusProvider

OWNER = OWNER_SOURCE

CYCLES = 600
REOPEN_EVERY = 50
LEASE_TTL = 30


def _light_limits():
    pol = ResourcePolicy()
    b = pol.limits_for(ResourceClass.LIGHT)
    return {
        "memory_high_bytes": b.memory_high_bytes,
        "memory_max_bytes": b.memory_max_bytes,
        "swap_max_bytes": b.swap_max_bytes,
        "cpu_quota_percent": b.cpu_quota_percent,
        "timeout_seconds": b.timeout_seconds,
    }


def _allow_admission():
    return AdmissionDecision(
        resource_class=ResourceClass.LIGHT.value,
        policy_version="1",
        snapshot_ref="snap-1",
        decision=AdmissionVerdict.ALLOW.value,
        reason_code=ResourceReasonCode.OK.value,
        next_eligible_at=None,
        effective_limits=_light_limits(),
        timestamp="2026-09-01T00:00:00+00:00",
    )


def _build_env(db_path, clock):
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    # C2: spawn now goes through the execution enforcer (never the legacy
    # launcher); inject a deterministic fake enforcer + governor/snapshot so
    # this soak stays offline (no real systemd-run / host reads).
    sup = Supervisor(
        core, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock,
        enforcer=ExecutionEnforcer(FakeScopeBackend()),
        resource_governor=FakeGovernor(_allow_admission()),
        snapshot_provider=FakeSnapshotProvider(),
        prompts_dir=Path(db_path).parent / "prompts",
    )
    sched = Scheduler(sup, owner_instance_id="soak-A", lease_ttl_seconds=LEASE_TTL)
    adapter = FakeExternalWaitAdapter()
    adapter.set_sticky("ci", "org/repo#run", WaitObservation(
        provider="ci", ref="org/repo#run", state=OBS_READY, subject="abc123",
        event_version=1))
    mgr = ExternalWaitManager(core._store, adapters={"ci": adapter}, clock=clock)
    return SimpleNamespace(core=core, project=project, sup=sup, sched=sched,
                           adapter=adapter, mgr=mgr, clock=clock)


def _new_job(env, n):
    task = env.core.create_task(env.project.id, f"t{n}", OWNER)
    env.core.start_task_run(task.id, OWNER)
    job = env.sup.store.create_job(task.id, idempotency_key=f"soak-{n}")
    env.core._store.enqueue_job(job.supervisor_job_id, queue_reason="NEW")
    return job.supervisor_job_id


def test_soak_durable_transitions_no_invariant_violation(db_path):
    clock = FakeClock()
    env = _build_env(db_path, clock)
    jids = [_new_job(env, n) for n in range(4)]

    passes = 0
    reopens = 0
    wakes = 0
    wait_entries = 0
    min_epoch = {j: 0 for j in jids}
    seen_terminals = {}
    seen_wait_terminal = {}

    def check_invariants():
        for jid in jids:
            row = env.core._store.get_supervisor_job(jid)
            if row is None:
                continue
            assert row["lease_epoch"] >= min_epoch[jid], \
                f"epoch regression on {jid}"
            min_epoch[jid] = max(min_epoch[jid], row["lease_epoch"])
            term = row["terminal"]
            if jid in seen_terminals:
                assert term == seen_terminals[jid], f"terminal reopened {jid}"
            if term is not None:
                seen_terminals[jid] = term
            keys = [a["action_key"]
                    for a in env.core._store.list_supervisor_actions(jid)]
            assert len(keys) == len(set(keys)), \
                f"duplicate action_key in snapshot for {jid}"
            for w in env.core._store.list_external_waits(jid):
                if w["terminal_observed_at"] is not None:
                    # A wait becomes terminal exactly once: its terminal
                    # timestamp must never change after being set.
                    if w["wait_id"] in seen_wait_terminal:
                        assert seen_wait_terminal[w["wait_id"]] == \
                            w["terminal_observed_at"], \
                            f"wait re-woke {w['wait_id']}"
                    else:
                        seen_wait_terminal[w["wait_id"]] = w["terminal_observed_at"]
            if row["writer_binding_mode"] == "BOUND":
                assert row["writer_dispatch_id"] is not None
                assert row["writer_owner_instance_id"] is not None
                assert row["writer_lease_epoch"] >= 1

    for i in range(CYCLES):
        # One job per 6-op super-cycle (claim -> wait -> wake -> reclaim -> backoff)
        # so the wait entry always targets the job that op 0 just claimed.
        jid = jids[(i // 6) % len(jids)]
        op = i % 6
        row = env.core._store.get_supervisor_job(jid)
        if op == 0:
            # Scheduler pass: claim / renew / release (bounded reconcile).
            env.sched.run_pass(jid)
            passes += 1
        elif op == 1:
            # Immediately enter an external wait while the lease is still fresh
            # (no clock advance since the claim/renew in op 0).
            if row is not None and row["primary_state"] == PrimaryState.RUNNING.value \
                    and row["owner_instance_id"] == "soak-A":
                try:
                    env.mgr.enter_waiting_external(
                        jid,
                        spec=WaitSpec(kind="CI", provider="ci",
                                      ref="org/repo#run",
                                      expected_subject="abc123"),
                        owner_instance_id="soak-A",
                        lease_epoch=row["lease_epoch"],
                    )
                    wait_entries += 1
                except (LeaseError, ValueError):
                    pass
        elif op == 2:
            # Advance past the wait's first check and process due waits (wake).
            env.clock.advance(61)
            results = env.mgr.check_due_waits()
            wakes += sum(1 for r in results if r.outcome == "woke")
        elif op == 3:
            # Expire the lease, then run restart reconciliation (takeover/quarantine).
            env.clock.advance(31)
            env.sched.reconcile_after_restart()
            passes += 1
        elif op == 4:
            # Scheduler pass again (re-claim after wake / takeover).
            env.sched.run_pass(jid)
            passes += 1
        else:  # op == 5
            # Holder-requeue with retry backoff metadata (bounded, exercised).
            if row is not None and row["primary_state"] == PrimaryState.RUNNING.value \
                    and row["owner_instance_id"] == "soak-A":
                try:
                    env.core._store.enqueue_job(
                        jid, queue_reason="RETRY_BACKOFF",
                        error_class="TRANSIENT", error_code="timeout",
                        bump_attempt=True,
                        owner_instance_id="soak-A",
                        lease_epoch=row["lease_epoch"],
                    )
                except LeaseError:
                    pass

        check_invariants()

        # Crash/restart interleaving: reopen the DB with a fresh instance.
        if i % REOPEN_EVERY == REOPEN_EVERY - 1:
            before = {j: env.core._store.get_supervisor_job(j) for j in jids}
            env.core.close()
            env = _build_env(db_path, clock)
            reopens += 1
            for j in jids:
                assert env.core._store.get_supervisor_job(j) == before[j], \
                    f"reopen changed facts for {j}"
            env.sched.reconcile_after_restart()
            check_invariants()

    # Prove the simulation did real work across all axes.
    assert reopens == CYCLES // REOPEN_EVERY
    assert passes >= 300
    assert wakes >= 1
    assert wait_entries >= 1
    # No duplicate ownership / orphaned lease tuples at rest.
    for jid in jids:
        row = env.core._store.get_supervisor_job(jid)
        if row["primary_state"] == PrimaryState.RUNNING.value:
            assert row["owner_instance_id"] is not None
            assert row["lease_epoch"] >= 1
            assert row["lease_expires_at"] is not None
        assert row["lease_epoch"] >= min_epoch[jid]
    env.core.close()
