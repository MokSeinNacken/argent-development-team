"""Phase C1 — scheduler admission-gate tests (deterministic, no real host).

Proves the smallest trusted admission point: a fresh claim runs the resource
preflight BEFORE reconcile/spawn.  DEFER/DENY_LOCAL requeue the job as QUEUED
(no dispatch, no launcher call, no code rework) and ALLOW continues the
existing Phase-B path unchanged.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from argent_core import Core, OWNER_SOURCE, Role, role_source
from argent_core.resource_governor import (
    AdmissionVerdict,
    ResourceReasonCode,
)
from argent_core.resource_policy import ResourceClass
from argent_core.scheduler import Scheduler
from argent_core.supervisor import Supervisor
from c1_helpers import make_snapshot
from mock_supervisor_runtime import FakeClock, FakeRunLauncher, FakeRunStatusProvider

OWNER = OWNER_SOURCE
LEAD = role_source(Role.LEAD)


class FakeGovernor:
    """Scriptable resource governor returning a canned decision."""

    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    def decide(self, **kwargs):
        self.calls.append(kwargs)
        return self.decision


class FakeSnapshotProvider:
    """Scriptable snapshot provider (never touches the real host)."""

    def __init__(self, snapshot=None):
        self.snapshot = snapshot or make_snapshot()
        self.captures = []

    def capture(self, workspace_path=None):
        self.captures.append(workspace_path)
        return self.snapshot


def _make_admission(verdict, reason, *, next_eligible_at=None, snapshot_ref="snap-1"):
    from argent_core.resource_governor import AdmissionDecision

    return AdmissionDecision(
        resource_class=ResourceClass.HEAVY.value,
        policy_version="1",
        snapshot_ref=snapshot_ref,
        decision=verdict,
        reason_code=reason,
        next_eligible_at=next_eligible_at,
        effective_limits={},
        timestamp="2026-09-01T00:00:00+00:00",
    )


def make_env(db_path, clock=None, resource_class=None):
    clock = clock or FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    core.start_task_run(task.id, OWNER)
    prov = FakeRunStatusProvider()
    launch = FakeRunLauncher()
    sup = Supervisor(core, prov, launch, clock=clock)
    job = sup.store.create_job(
        task.id, idempotency_key="job-main",
        resource_class=resource_class or ResourceClass.HEAVY.value,
    )
    jid = job.supervisor_job_id
    return SimpleNamespace(core=core, project=project, task=task, prov=prov,
                           launch=launch, sup=sup, clock=clock, jid=jid)


def _row(env):
    return env.core._store.get_supervisor_job(env.jid)


def test_defer_keeps_job_queued_and_never_spawns(db_path):
    env = make_env(db_path)
    gov = FakeGovernor(_make_admission(
        AdmissionVerdict.DEFER.value,
        ResourceReasonCode.INSUFFICIENT_MEMORY_RESERVE.value,
        next_eligible_at="2026-09-01T00:05:00+00:00",
    ))
    snap = FakeSnapshotProvider()
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=gov, snapshot_provider=snap)
    r = sched.run_pass(env.jid)

    assert r.outcome == "resource_deferred"
    assert len(env.launch.spawns) == 0
    row = _row(env)
    assert row["primary_state"] == "QUEUED"
    assert row["owner_instance_id"] is None
    assert row["lease_expires_at"] is None
    assert row["next_eligible_at"] == "2026-09-01T00:05:00+00:00"
    assert row["queue_reason"] == "RESOURCE_DEFERRED"
    assert row["error_class"] == "RESOURCE"
    assert row["last_resource_decision"] == AdmissionVerdict.DEFER.value
    assert row["last_resource_reason_code"] == \
        ResourceReasonCode.INSUFFICIENT_MEMORY_RESERVE.value
    # No dispatch was created (no reconcile, no spawn).
    assert len(env.core._store.list_dispatches(env.task.id)) == 0
    assert len(gov.calls) == 1
    assert gov.calls[0]["resource_class"] == ResourceClass.HEAVY.value
    env.core.close()


def test_deny_local_no_identical_immediate_retry(db_path):
    env = make_env(db_path)
    now_before = env.clock.now_iso()
    gov = FakeGovernor(_make_admission(
        AdmissionVerdict.DENY_LOCAL.value, ResourceReasonCode.DISK_LOW.value,
    ))
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=gov,
                      snapshot_provider=FakeSnapshotProvider())
    r = sched.run_pass(env.jid)

    assert r.outcome == "resource_denied"
    assert len(env.launch.spawns) == 0
    row = _row(env)
    assert row["primary_state"] == "QUEUED"
    # RESOURCE (never DETERMINISTIC / code failure), umbrella error code.
    assert row["error_class"] == "RESOURCE"
    assert row["last_error_code"] == \
        ResourceReasonCode.LOCAL_CAPACITY_INSUFFICIENT.value
    assert row["queue_reason"] == "RESOURCE_DENIED"
    # far-future horizon: a second pass must NOT immediately re-claim it.
    assert row["next_eligible_at"] is not None
    assert row["next_eligible_at"] > now_before
    r2 = sched.run_pass()  # claim_next_job: not eligible yet -> no_work
    assert r2.outcome == "no_work"
    assert len(env.launch.spawns) == 0
    env.core.close()


def test_allow_continues_phase_b_path_unchanged(db_path):
    env = make_env(db_path)
    gov = FakeGovernor(_make_admission(
        AdmissionVerdict.ALLOW.value, ResourceReasonCode.OK.value,
    ))
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=gov,
                      snapshot_provider=FakeSnapshotProvider())
    r = sched.run_pass(env.jid)

    # Not a resource outcome; the claim + reconcile proceeded (RUNNING).
    assert r.outcome not in ("resource_deferred", "resource_denied")
    row = _row(env)
    assert row["primary_state"] == "RUNNING"
    assert row["owner_instance_id"] == "A"
    assert row["lease_epoch"] == 1
    assert len(gov.calls) == 1
    env.core.close()


def test_prefer_external_persists_hint_then_continues_locally(db_path):
    env = make_env(db_path)
    gov = FakeGovernor(_make_admission(
        AdmissionVerdict.PREFER_EXTERNAL.value,
        ResourceReasonCode.EXTERNAL_CI_PREFERRED.value,
    ))
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=gov,
                      snapshot_provider=FakeSnapshotProvider())
    r = sched.run_pass(env.jid)

    # Local admission continues as ALLOW (no external CI action in C1).
    assert r.outcome not in ("resource_deferred", "resource_denied")
    row = _row(env)
    assert row["primary_state"] == "RUNNING"
    assert row["last_resource_decision"] == AdmissionVerdict.PREFER_EXTERNAL.value
    assert row["last_resource_reason_code"] == \
        ResourceReasonCode.EXTERNAL_CI_PREFERRED.value
    env.core.close()


def test_resource_denied_job_is_not_classified_as_code_failure(db_path):
    env = make_env(db_path)
    gov = FakeGovernor(_make_admission(
        AdmissionVerdict.DENY_LOCAL.value, ResourceReasonCode.DISK_LOW.value,
    ))
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=gov,
                      snapshot_provider=FakeSnapshotProvider())
    sched.run_pass(env.jid)
    row = _row(env)
    # RESOURCE taxonomy only — never DETERMINISTIC / never a code rework signal.
    assert row["error_class"] == "RESOURCE"
    assert row["error_class"] != "DETERMINISTIC"
    assert row["rework_cycle"] == 1  # no code rework was triggered
    env.core.close()
