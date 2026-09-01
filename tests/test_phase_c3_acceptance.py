"""Phase C3 — integrated acceptance (deterministic, fake backend, FakeClock).

The 10 mandatory Phase-C acceptance cases (ARGENT ARCHITECTURE V1 FINAL §19).
Every case is deterministic: pure classification/decision functions plus the
``FakeScopeBackend`` enforcer and the fenced ``commit_recovery_decision`` store
primitive — no systemd, no cgroup, no host I/O, no real OOM/swap/disk stress.
"""

from __future__ import annotations

import pytest

from argent_core import Core
from argent_core.models import LeaseError, LeaseFencedError
from argent_core.resource_failure import TerminationClass
from argent_core.resource_governor import (
    AdmissionDecision,
    AdmissionVerdict,
    ResourceGovernor,
    ResourceReasonCode,
)
from argent_core.resource_policy import ResourceClass, ResourcePolicy
from argent_core.resource_recovery import (
    FailureClass,
    RecoveryDecision,
    RecoveryPolicy,
    classify_failure,
    decide_recovery,
    failure_class_from_admission,
    failure_class_from_enforcement_status,
    is_resource_failure,
)
from argent_core.scope_enforcer import EnforcementStatus, ExecutionEnforcer
from c1_helpers import make_snapshot
from c2_helpers import FakeScopeBackend, FakeGovernor, FakeSnapshotProvider, make_env, verified_properties
from c3_helpers import build_running_job, fake_identity_provider, register_terminal_evidence


def _policy(**kw):
    return RecoveryPolicy(**kw)


# ---------------------------------------------------------------------------
# CASE 1 — HEALTHY
# ---------------------------------------------------------------------------

def test_case1_healthy_normal_exit_is_not_a_resource_failure():
    # C1 ALLOW -> C2 verified scope -> exit 0 -> C3 NORMAL_EXIT.
    pol = ResourcePolicy()
    limits = pol.limits_for(ResourceClass.LIGHT)
    eff = {
        "memory_high_bytes": limits.memory_high_bytes,
        "memory_max_bytes": limits.memory_max_bytes,
        "swap_max_bytes": limits.swap_max_bytes,
        "cpu_quota_percent": limits.cpu_quota_percent,
        "timeout_seconds": limits.timeout_seconds,
    }
    backend = FakeScopeBackend(
        verify_properties=verified_properties(eff),
        memory_events={"oom_kill": 0, "oom_group_kill": 0, "max": 0, "high": 0},
        run_result={"exit_code": 0, "stdout_bounded": "", "stderr_bounded": "",
                    "timed_out": False, "pid": 424242},
    )
    result = ExecutionEnforcer(backend).enforce_and_run(
        command=["true"], effective_limits=eff,
        resource_class=ResourceClass.LIGHT, policy_version="1",
        job_id="job-1", dispatch_id="dispatch-1",
    )
    assert result.exit_code == 0
    assert result.termination_class == TerminationClass.NORMAL_EXIT.value
    fc = classify_failure(
        termination_class=result.termination_class,
        exit_code=result.exit_code, timed_out=result.timed_out,
        scope_events=result.scope_events,
    )
    assert fc is FailureClass.NORMAL_EXIT
    assert not is_resource_failure(fc)
    assert decide_recovery(fc, attempt_no=0, policy=_policy()) \
        is RecoveryDecision.COMPLETE


# ---------------------------------------------------------------------------
# CASE 2 — HOST PRESSURE BEFORE START
# ---------------------------------------------------------------------------

def test_case2_host_pressure_defers_and_never_spawns():
    # Low MemAvailable / high swap -> C1 DEFER/DENY -> C2 starts nothing -> no
    # false code failure (the job is a RESOURCE defer, never a code failure).
    snap = make_snapshot(mem_available=1, swap_total=100, swap_free=0,
                         mem_total=100)
    decision = ResourceGovernor().decide(
        resource_class=ResourceClass.HEAVY, snapshot=snap, now_iso="2026-09-01T00:00:00+00:00",
    )
    assert decision.decision in (AdmissionVerdict.DEFER.value,
                                 AdmissionVerdict.DENY_LOCAL.value)
    fc = failure_class_from_admission(decision.decision)
    assert fc is FailureClass.RESOURCE_CAPACITY_FAILURE
    assert is_resource_failure(fc)
    assert decide_recovery(fc, attempt_no=0, policy=_policy()) \
        is RecoveryDecision.DEFER_RESOURCE
    # A resource defer is NEVER a code failure (no rework authorisation).
    assert fc is not FailureClass.CODE_OR_PROCESS_FAILURE


# ---------------------------------------------------------------------------
# CASE 3 — ENFORCEMENT UNAVAILABLE
# ---------------------------------------------------------------------------

def test_case3_enforcement_unavailable_is_bounded_recovery():
    # C1 ALLOW -> C2 unavailable -> no unbounded child -> C3
    # RESOURCE_ENFORCEMENT_FAILURE -> bounded recovery (no unbounded retry).
    fc = failure_class_from_enforcement_status(
        EnforcementStatus.ENFORCEMENT_UNAVAILABLE.value,
    )
    assert fc is FailureClass.RESOURCE_ENFORCEMENT_FAILURE
    assert is_resource_failure(fc)
    # Provable path (no evidence-unknown) -> bounded DEFER.
    assert decide_recovery(fc, attempt_no=0, policy=_policy()) \
        is RecoveryDecision.DEFER_RESOURCE
    # Unproven path (evidence unknown) -> LOST quarantine (fail-closed).
    assert decide_recovery(fc, attempt_no=0, policy=_policy(),
                           has_evidence_unknown=True) \
        is RecoveryDecision.QUARANTINE_LOST


# ---------------------------------------------------------------------------
# CASE 4 — OOM
# ---------------------------------------------------------------------------

def test_case4_oom_never_retries_identically_or_raises_limits(db_path):
    # OOM evidence -> RESOURCE_OOM -> BLOCK_RESOURCE (no rework, no limit
    # increase, no identical retry).
    fc = classify_failure(
        termination_class=TerminationClass.OOM_KILL.value,
        exit_code=137, timed_out=False,
        scope_events={"oom_kill": 1, "oom_group_kill": 0, "max": 0, "high": 0},
    )
    assert fc is FailureClass.RESOURCE_OOM
    assert is_resource_failure(fc)
    decision = decide_recovery(fc, attempt_no=0, policy=_policy())
    assert decision is RecoveryDecision.BLOCK_RESOURCE
    assert decision is not RecoveryDecision.RETRY_BOUNDED
    assert decision is not RecoveryDecision.FAIL_NONRESOURCE

    # Fenced commit lands the job in BLOCKED (terminal=BLOCKED, not DONE).
    env = build_running_job(Core(db_path))
    row = env.core._store.commit_recovery_decision(
        env.jid, owner_instance_id="A", lease_epoch=env.epoch,
        failure_class=fc, recovery_decision=decision, reason_code="RESOURCE_OOM",
    )
    assert row["primary_state"] == "BLOCKED"
    assert row["terminal"] == "BLOCKED"
    assert row["last_failure_class"] == "RESOURCE_OOM"


# ---------------------------------------------------------------------------
# CASE 5 — TIMEOUT
# ---------------------------------------------------------------------------

def test_case5_timeout_is_bounded_retry_without_longer_timeout(db_path):
    fc = classify_failure(
        termination_class=TerminationClass.TIMEOUT.value,
        exit_code=124, timed_out=True, scope_events=None,
    )
    assert fc is FailureClass.RESOURCE_TIMEOUT
    assert is_resource_failure(fc)
    decision = decide_recovery(fc, attempt_no=0, policy=_policy())
    assert decision is RecoveryDecision.RETRY_BOUNDED
    # No timeout increase: the policy has no timeout field (structural).
    pol = _policy()
    assert not hasattr(pol, "timeout_seconds")
    assert pol.max_resource_retries == 2

    env = build_running_job(Core(db_path))
    row = env.core._store.commit_recovery_decision(
        env.jid, owner_instance_id="A", lease_epoch=env.epoch,
        failure_class=fc, recovery_decision=decision, reason_code="RESOURCE_TIMEOUT",
        next_eligible_at="2026-09-01T00:05:00+00:00",
    )
    assert row["primary_state"] == "QUEUED"
    assert row["attempt_no"] == 1
    assert row["queue_reason"] == "RETRY_BACKOFF"
    assert row["error_class"] == "RESOURCE"


# ---------------------------------------------------------------------------
# CASE 6 — NORMAL NONZERO
# ---------------------------------------------------------------------------

def test_case6_nonzero_exit_is_code_failure_not_oom_or_timeout():
    fc = classify_failure(
        termination_class=TerminationClass.NONZERO_EXIT.value,
        exit_code=1, timed_out=False, scope_events=None,
    )
    assert fc is FailureClass.CODE_OR_PROCESS_FAILURE
    assert not is_resource_failure(fc)
    assert decide_recovery(fc, attempt_no=0, policy=_policy()) \
        is RecoveryDecision.FAIL_NONRESOURCE


# ---------------------------------------------------------------------------
# CASE 7 — UNKNOWN
# ---------------------------------------------------------------------------

def test_case7_unknown_termination_fails_closed_to_lost(db_path):
    fc = classify_failure(
        termination_class=TerminationClass.UNKNOWN_TERMINATION.value,
        exit_code=None, timed_out=False, scope_events=None,
    )
    assert fc is FailureClass.UNKNOWN_TERMINATION
    assert is_resource_failure(fc)
    assert decide_recovery(fc, attempt_no=0, policy=_policy()) \
        is RecoveryDecision.QUARANTINE_LOST

    env = build_running_job(Core(db_path))
    row = env.core._store.commit_recovery_decision(
        env.jid, owner_instance_id="A", lease_epoch=env.epoch,
        failure_class=fc, recovery_decision=RecoveryDecision.QUARANTINE_LOST,
        reason_code="RESOURCE_EVIDENCE_UNKNOWN",
    )
    assert row["primary_state"] == "LOST"
    # No duplicate spawn: a second commit on the same event is refused.
    with pytest.raises((LeaseError, LeaseFencedError)):
        env.core._store.commit_recovery_decision(
            env.jid, owner_instance_id="A", lease_epoch=env.epoch,
            failure_class=fc, recovery_decision=RecoveryDecision.QUARANTINE_LOST,
            reason_code="RESOURCE_EVIDENCE_UNKNOWN",
        )


# ---------------------------------------------------------------------------
# CASE 8 — RESTART (exactly-once recovery)
# ---------------------------------------------------------------------------

def test_case8_restart_recovery_is_exactly_once(db_path):
    env = build_running_job(Core(db_path))
    fc = FailureClass.RESOURCE_OOM
    # First commit transitions RUNNING -> BLOCKED.
    env.core._store.commit_recovery_decision(
        env.jid, owner_instance_id="A", lease_epoch=env.epoch,
        failure_class=fc, recovery_decision=RecoveryDecision.BLOCK_RESOURCE,
        reason_code="RESOURCE_OOM",
    )
    env.core.close()

    # Reopen (fresh Store on the same file) -> the job is no longer RUNNING,
    # so a re-classification can never re-commit (exactly-once).
    core2 = Core(db_path)
    try:
        row = core2._store.get_supervisor_job(env.jid)
        assert row["primary_state"] == "BLOCKED"
        with pytest.raises((LeaseError, LeaseFencedError)):
            core2._store.commit_recovery_decision(
                env.jid, owner_instance_id="A", lease_epoch=env.epoch,
                failure_class=fc, recovery_decision=RecoveryDecision.BLOCK_RESOURCE,
                reason_code="RESOURCE_OOM",
            )
    finally:
        core2.close()


# ---------------------------------------------------------------------------
# CASE 9 — DUAL SUPERVISOR (fencing)
# ---------------------------------------------------------------------------

def test_case9_only_valid_lease_epoch_may_mutate(db_path):
    env = build_running_job(Core(db_path))
    fc = FailureClass.RESOURCE_OOM
    # A foreign owner cannot commit.
    with pytest.raises(LeaseFencedError):
        env.core._store.commit_recovery_decision(
            env.jid, owner_instance_id="B", lease_epoch=env.epoch,
            failure_class=fc, recovery_decision=RecoveryDecision.BLOCK_RESOURCE,
            reason_code="RESOURCE_OOM",
        )
    # A stale epoch cannot commit.
    with pytest.raises(LeaseFencedError):
        env.core._store.commit_recovery_decision(
            env.jid, owner_instance_id="A", lease_epoch=env.epoch + 99,
            failure_class=fc, recovery_decision=RecoveryDecision.BLOCK_RESOURCE,
            reason_code="RESOURCE_OOM",
        )
    # The valid holder still commits exactly once.
    row = env.core._store.commit_recovery_decision(
        env.jid, owner_instance_id="A", lease_epoch=env.epoch,
        failure_class=fc, recovery_decision=RecoveryDecision.BLOCK_RESOURCE,
        reason_code="RESOURCE_OOM",
    )
    assert row["primary_state"] == "BLOCKED"


# ---------------------------------------------------------------------------
# CASE 10 — TSGO REFERENZFALL
# ---------------------------------------------------------------------------

def test_case10_tsgo_reference():
    # (a) Host under pressure -> C1 prevents start (no spawn).
    snap_pressure = make_snapshot(mem_available=1, mem_total=100)
    decision = ResourceGovernor().decide(
        resource_class=ResourceClass.HEAVY, snapshot=snap_pressure,
        now_iso="2026-09-01T00:00:00+00:00",
    )
    assert decision.decision != AdmissionVerdict.ALLOW.value

    # (b) Healthy + admitted -> C2 bounded MemoryMax; process hits MemoryMax ->
    # C3 RESOURCE_MEMORY_LIMIT -> no rework, no limit increase, no infinite
    # identical retry.
    fc = classify_failure(
        termination_class=TerminationClass.MEMORY_LIMIT.value,
        exit_code=137, timed_out=False,
        scope_events={"oom_kill": 0, "oom_group_kill": 0, "max": 1, "high": 0},
    )
    assert fc is FailureClass.RESOURCE_MEMORY_LIMIT
    assert is_resource_failure(fc)
    decision = decide_recovery(fc, attempt_no=0, policy=_policy())
    assert decision is RecoveryDecision.BLOCK_RESOURCE  # no identical retry
    assert decision is not RecoveryDecision.RETRY_BOUNDED
    # No limit increase: the policy has no limit fields (structural).
    assert not hasattr(_policy(), "memory_max_bytes")
    assert not hasattr(_policy(), "timeout_seconds")


# ---------------------------------------------------------------------------
# F6 — integrated acceptance (Scheduler + Supervisor + Store, FakeScopeBackend)
# ---------------------------------------------------------------------------

from argent_core.scheduler import Scheduler  # noqa: E402


def _light_limits():
    pol = ResourcePolicy()
    limits = pol.limits_for(ResourceClass.LIGHT)
    return {
        "memory_high_bytes": limits.memory_high_bytes,
        "memory_max_bytes": limits.memory_max_bytes,
        "swap_max_bytes": limits.swap_max_bytes,
        "cpu_quota_percent": limits.cpu_quota_percent,
        "timeout_seconds": limits.timeout_seconds,
    }


def _admission(verdict, reason):
    return AdmissionDecision(
        resource_class=ResourceClass.LIGHT.value,
        policy_version="1",
        snapshot_ref="snap-1",
        decision=verdict,
        reason_code=reason,
        next_eligible_at=None,
        effective_limits=_light_limits(),
        timestamp="2026-09-01T00:00:00+00:00",
    )


class OOMBackend(FakeScopeBackend):
    """Stateful memory.events: first read per scope = baseline, later = OOM."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._seen_scopes = set()

    def read_memory_events(self, scope):
        key = getattr(scope, "unit_name", None) or getattr(scope, "scope_name", None)
        if key not in self._seen_scopes:
            self._seen_scopes.add(key)
            return {"oom_kill": 0, "oom_group_kill": 0, "max": 0, "high": 0}
        return {"oom_kill": 1, "oom_group_kill": 0, "max": 0, "high": 0}


def _scheduler_for(env, *, owner="A", recovery_policy=None):
    return Scheduler(
        env.sup, owner_instance_id=owner, lease_ttl_seconds=300,
        resource_governor=FakeGovernor(
            _admission(AdmissionVerdict.ALLOW.value, ResourceReasonCode.OK.value)
        ),
        snapshot_provider=FakeSnapshotProvider(),
        recovery_policy=recovery_policy,
    )


def _scheduler_on(core, *, owner="A", recovery_policy=None):
    from argent_core.supervisor import Supervisor
    from mock_supervisor_runtime import FakeRunLauncher, FakeRunStatusProvider
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher())
    sup._process_identity_provider = fake_identity_provider()
    return Scheduler(
        sup, owner_instance_id=owner, lease_ttl_seconds=300,
        resource_governor=FakeGovernor(
            _admission(AdmissionVerdict.ALLOW.value, ResourceReasonCode.OK.value)
        ),
        snapshot_provider=FakeSnapshotProvider(),
        recovery_policy=recovery_policy,
    )


def _drive_to_dispatch(sched, env):
    """Drive passes until the job's frontier dispatch is projected (FK binding)."""
    for _ in range(20):
        sched.run_pass(env.jid)
        job = env.sup.store._job_row(env.jid)
        if job.get("expected_dispatch_id") is not None:
            return job["expected_dispatch_id"]
    return None


def test_case4_oom_integrated_fenced_recovery_no_rework(db_path):
    # OOM evidence is produced by the Supervisor scoped-sandbox path (FakeScopeBackend),
    # then classified + recovered exactly once by the Scheduler over the Store.
    backend = OOMBackend(
        verify_properties=verified_properties(_light_limits()),
        run_result={"exit_code": 137, "stdout_bounded": "", "stderr_bounded": "",
                    "timed_out": False, "pid": 424242},
    )
    enforcer = ExecutionEnforcer(backend)
    env = make_env(db_path, enforcer=enforcer)
    env.sup._process_identity_provider = fake_identity_provider()
    env.sup._workspace_root = "/tmp/argent-fake-workspace"
    sched = _scheduler_for(env)

    dispatch_id = _drive_to_dispatch(sched, env)
    assert dispatch_id is not None

    job_row = env.sup.store._job_row(env.jid)
    assert job_row["primary_state"] == "RUNNING"

    # Supervisor produces the terminal OOM evidence (scoped sandbox run).
    exit_code = env.sup._run_sandbox_scoped(None, job_row, dispatch_id)
    assert exit_code != 0  # OOM sentinel (never a silent pass)
    assert env.sup._sandbox_resource_termination(dispatch_id) is True

    # Scheduler classifies + commits the fenced recovery exactly once.
    epoch = env.sup.store._job_row(env.jid)["lease_epoch"]
    result = sched.classify_and_recover(env.jid, epoch)
    assert result is not None
    assert result.outcome == "resource_recovered"

    row = env.sup.store._job_row(env.jid)
    assert row["primary_state"] == "BLOCKED"
    assert row["terminal"] == "BLOCKED"
    assert row["last_failure_class"] == "RESOURCE_OOM"
    # No code rework, no identical retry.
    assert row["last_failure_class"] != "CODE_OR_PROCESS_FAILURE"
    assert row["queue_reason"] != "RETRY_BACKOFF"
    env.core.close()


def test_case8_restart_integrated_exactly_one_recovery(db_path):
    # Terminal OOM evidence persisted, DB reopened between classification and
    # transition -> exactly one fenced recovery via the restart reconcile path.
    env = build_running_job(Core(db_path), owner="A")
    register_terminal_evidence(
        env.core, env.jid, termination_class=TerminationClass.OOM_KILL.value,
        exit_code=137,
        scope_events={"oom_kill": 1, "oom_group_kill": 0, "max": 0, "high": 0},
    )
    jid = env.jid
    env.core.close()

    core2 = Core(db_path)
    try:
        sched = _scheduler_on(core2, owner="A")
        summary = sched.reconcile_after_restart()
        assert summary.resource_recovered == 1
        row = core2._store.get_supervisor_job(jid)
        assert row["primary_state"] == "BLOCKED"
        assert row["last_failure_class"] == "RESOURCE_OOM"
        # Exactly once: a second reconcile is a no-op.
        summary2 = sched.reconcile_after_restart()
        assert summary2.resource_recovered == 0
        assert core2._store.get_supervisor_job(jid)["primary_state"] == "BLOCKED"
    finally:
        core2.close()


def test_case10_tsgo_integrated_pressure_and_oom(db_path):
    # (a) Host under pressure -> C1 DEFER before start (no spawn).
    backend = FakeScopeBackend(verify_properties=verified_properties(_light_limits()))
    enforcer = ExecutionEnforcer(backend)
    env = make_env(db_path, enforcer=enforcer)
    env.sup._process_identity_provider = fake_identity_provider()
    env.sup._workspace_root = "/tmp/argent-fake-workspace"
    sched = Scheduler(
        env.sup, owner_instance_id="A", lease_ttl_seconds=300,
        resource_governor=FakeGovernor(
            _admission(
                AdmissionVerdict.DEFER.value,
                ResourceReasonCode.INSUFFICIENT_MEMORY_RESERVE.value,
            )
        ),
        snapshot_provider=FakeSnapshotProvider(),
    )
    final = sched.run_pass(env.jid)
    assert final.outcome == "resource_deferred"
    assert env.launch.spawns == []  # never spawned under pressure
    row = env.sup.store._job_row(env.jid)
    assert row["primary_state"] == "QUEUED"
    assert row["error_class"] == "RESOURCE"
    assert row["error_class"] != "CODE"
    env.core.close()

    # (b) Healthy + admitted -> bounded MemoryMax; process hits the limit ->
    # RESOURCE_OOM -> BLOCK (no rework, no limit increase, no endless retry).
    env2 = make_env(db_path + ".b", enforcer=ExecutionEnforcer(OOMBackend(
        verify_properties=verified_properties(_light_limits()),
        run_result={"exit_code": 137, "stdout_bounded": "", "stderr_bounded": "",
                    "timed_out": False, "pid": 424242},
    )))
    env2.sup._process_identity_provider = fake_identity_provider()
    env2.sup._workspace_root = "/tmp/argent-fake-workspace"
    sched2 = _scheduler_for(env2)
    dispatch_id = _drive_to_dispatch(sched2, env2)
    assert dispatch_id is not None
    job_row = env2.sup.store._job_row(env2.jid)
    env2.sup._run_sandbox_scoped(None, job_row, dispatch_id)
    epoch = env2.sup.store._job_row(env2.jid)["lease_epoch"]
    result = sched2.classify_and_recover(env2.jid, epoch)
    assert result is not None
    row = env2.sup.store._job_row(env2.jid)
    assert row["primary_state"] == "BLOCKED"
    # No limit increase (structural): the policy has no limit fields.
    assert not hasattr(RecoveryPolicy(), "memory_max_bytes")
    env2.core.close()

