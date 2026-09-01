"""Phase C2 — spawn gate (deterministic Scheduler integration, fake backend).

Proves the binding invariant: a resource-relevant process is only started when
the admission is ALLOW/PREFER_EXTERNAL AND the execution scope was created and
verified.  Scope/enforcement failures requeue the job as QUEUED with
``error_class=RESOURCE`` (never CODE_FAILURE, never rework); DEFER/DENY_LOCAL
never create a scope.
"""

from __future__ import annotations

from argent_core.execution_scope import ScopeCreateError
from argent_core.resource_governor import (
    AdmissionVerdict,
    ResourceGovernor,
    ResourceReasonCode,
)
from argent_core.resource_policy import ResourceClass, gib
from argent_core.scheduler import Scheduler
from argent_core.scope_enforcer import ExecutionEnforcer
from c1_helpers import make_snapshot
from c2_helpers import (
    FakeGovernor,
    FakeScopeBackend,
    FakeSnapshotProvider,
    make_env,
    verified_properties,
)


def _limits():
    from argent_core.resource_policy import ResourcePolicy
    pol = ResourcePolicy()
    base = pol.limits_for(ResourceClass.HEAVY)
    return {
        "memory_high_bytes": base.memory_high_bytes,
        "memory_max_bytes": base.memory_max_bytes,
        "swap_max_bytes": base.swap_max_bytes,
        "cpu_quota_percent": base.cpu_quota_percent,
        "timeout_seconds": base.timeout_seconds,
    }


def _admission(verdict, reason, *, next_eligible_at=None):
    from argent_core.resource_governor import AdmissionDecision
    return AdmissionDecision(
        resource_class=ResourceClass.HEAVY.value, policy_version="1",
        snapshot_ref="snap-1", decision=verdict, reason_code=reason,
        next_eligible_at=next_eligible_at, effective_limits=_limits(),
        timestamp="2026-09-01T00:00:00+00:00",
    )


def _drive(sched, jid, max_passes=15):
    """Run passes until a resource outcome or spawn is observed."""
    final = None
    for _ in range(max_passes):
        r = sched.run_pass(jid)
        final = r
        if r.outcome in ("resource_deferred", "resource_denied"):
            break
    return final


def test_allow_with_verified_scope_spawns_and_binds_registry(db_path):
    backend = FakeScopeBackend(verify_properties=verified_properties(_limits()))
    enforcer = ExecutionEnforcer(backend)
    env = make_env(db_path, enforcer=enforcer)
    gov = FakeGovernor(_admission(
        AdmissionVerdict.ALLOW.value, ResourceReasonCode.OK.value,
    ))
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=gov, snapshot_provider=FakeSnapshotProvider())

    final = _drive(sched, env.jid)

    assert final is not None
    assert len(backend.created) == 1  # exactly one scope created
    assert env.launch.spawns == []  # the enforcer path, not the legacy launcher
    row = env.core._store.get_supervisor_job(env.jid)
    assert row["primary_state"] == "RUNNING"
    # Process registry bound with scope metadata.
    regs = env.core._store.list_process_registrations(env.jid)
    assert len(regs) == 1
    reg = regs[0]
    assert reg["scope_ref"] == backend.created[0]["scope"].unit_name
    assert reg["resource_class"] == "HEAVY"
    assert reg["policy_version"] == "1"
    assert reg["cgroup_ref"] == backend.cgroup_path
    env.core.close()


def test_allow_with_scope_failure_requeues_resource_no_spawn(db_path):
    backend = FakeScopeBackend(fail_create=ScopeCreateError("no systemd"))
    enforcer = ExecutionEnforcer(backend)
    env = make_env(db_path, enforcer=enforcer)
    gov = FakeGovernor(_admission(
        AdmissionVerdict.ALLOW.value, ResourceReasonCode.OK.value,
    ))
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=gov, snapshot_provider=FakeSnapshotProvider())

    final = _drive(sched, env.jid)

    assert final.outcome == "resource_deferred"
    assert final.detail == ResourceReasonCode.RESOURCE_ENFORCEMENT_UNAVAILABLE.value
    assert env.launch.spawns == []
    row = env.core._store.get_supervisor_job(env.jid)
    assert row["primary_state"] == "QUEUED"
    assert row["error_class"] == "RESOURCE"
    assert row["error_class"] != "DETERMINISTIC"  # never a code failure
    assert row["next_eligible_at"] is not None
    env.core.close()


def test_defer_never_creates_scope(db_path):
    backend = FakeScopeBackend()
    enforcer = ExecutionEnforcer(backend)
    env = make_env(db_path, enforcer=enforcer)
    gov = FakeGovernor(_admission(
        AdmissionVerdict.DEFER.value,
        ResourceReasonCode.INSUFFICIENT_MEMORY_RESERVE.value,
        next_eligible_at="2026-09-01T00:05:00+00:00",
    ))
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=gov, snapshot_provider=FakeSnapshotProvider())

    final = _drive(sched, env.jid)

    assert final.outcome == "resource_deferred"
    assert len(backend.created) == 0  # no scope, no spawn
    assert env.launch.spawns == []
    row = env.core._store.get_supervisor_job(env.jid)
    assert row["primary_state"] == "QUEUED"
    env.core.close()


def test_deny_local_never_creates_scope(db_path):
    backend = FakeScopeBackend()
    enforcer = ExecutionEnforcer(backend)
    env = make_env(db_path, enforcer=enforcer)
    gov = FakeGovernor(_admission(
        AdmissionVerdict.DENY_LOCAL.value, ResourceReasonCode.DISK_LOW.value,
    ))
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=gov, snapshot_provider=FakeSnapshotProvider())

    final = _drive(sched, env.jid)

    assert final.outcome == "resource_denied"
    assert len(backend.created) == 0
    assert env.launch.spawns == []
    env.core.close()


class _ScriptedSnapshotProvider:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)

    def capture(self, workspace_path=None):
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]


def test_stale_admission_runs_fresh_preflight_and_blocks_spawn(db_path):
    """Host pressure between claim and spawn gate -> fresh preflight DEFERs."""
    backend = FakeScopeBackend(verify_properties=verified_properties(_limits()))
    enforcer = ExecutionEnforcer(backend)
    env = make_env(db_path, enforcer=enforcer, resource_class="HEAVY")
    # Healthy first (claim pass ALLOW), low memory on every later capture.
    snap = _ScriptedSnapshotProvider([make_snapshot(), make_snapshot(mem_available=gib(4))])
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=ResourceGovernor(),
                      snapshot_provider=snap)

    final = _drive(sched, env.jid)

    assert final is not None
    assert final.outcome == "resource_deferred"
    assert final.detail == ResourceReasonCode.INSUFFICIENT_MEMORY_RESERVE.value
    # Host pressure blocked the spawn before a scope could ever be created.
    assert len(backend.created) == 0
    assert env.launch.spawns == []
    env.core.close()
