"""Shared deterministic helpers for the Phase D1 dispatch-integration tests.

Builds a Core+Supervisor+job environment with a fake scope backend/enforcer (no
systemd, no cgroup, no host I/O) and an injectable Context Builder, mirroring
the Phase C2 helpers so the D1 dispatch path is exercised fully deterministically.
"""

from __future__ import annotations

from types import SimpleNamespace

from argent_core import Core, OWNER_SOURCE, Role, role_source
from argent_core.context_pack import ContextBuildError
from argent_core.resource_governor import (
    AdmissionDecision,
    AdmissionVerdict,
    ResourceReasonCode,
)
from argent_core.resource_policy import ResourceClass, ResourcePolicy
from argent_core.scheduler import Scheduler
from argent_core.scope_enforcer import ExecutionEnforcer
from argent_core.supervisor import Supervisor
from c2_helpers import FakeGovernor, FakeScopeBackend, FakeSnapshotProvider, verified_properties
from mock_supervisor_runtime import FakeClock, FakeRunLauncher, FakeRunStatusProvider

OWNER = OWNER_SOURCE
LEAD = role_source(Role.LEAD)


def d1_limits():
    pol = ResourcePolicy()
    base = pol.limits_for(ResourceClass.HEAVY)
    return {
        "memory_high_bytes": base.memory_high_bytes,
        "memory_max_bytes": base.memory_max_bytes,
        "swap_max_bytes": base.swap_max_bytes,
        "cpu_quota_percent": base.cpu_quota_percent,
        "timeout_seconds": base.timeout_seconds,
    }


def d1_admission():
    return AdmissionDecision(
        resource_class=ResourceClass.HEAVY.value, policy_version="1",
        snapshot_ref="snap-1", decision=AdmissionVerdict.ALLOW.value,
        reason_code=ResourceReasonCode.OK.value, next_eligible_at=None,
        effective_limits=d1_limits(), timestamp="2026-09-01T00:00:00+00:00",
    )


class FailingContextBuilder:
    """A builder that always raises (simulates a budget/validation failure)."""

    def __init__(self, error: ContextBuildError):
        self.error = error
        self.calls = []

    def build(self, **kwargs):
        self.calls.append(kwargs)
        raise self.error


def make_d1_env(db_path, context_builder=None):
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER, description="fix the bug")
    core.start_task_run(task.id, OWNER)
    launch = FakeRunLauncher()
    backend = FakeScopeBackend(verify_properties=verified_properties(d1_limits()))
    enforcer = ExecutionEnforcer(backend)
    sup = Supervisor(core, FakeRunStatusProvider(), launch, clock=clock,
                     enforcer=enforcer, context_builder=context_builder)
    job = sup.store.create_job(task.id, idempotency_key="job-main",
                               resource_class=ResourceClass.HEAVY.value)
    jid = job.supervisor_job_id
    return SimpleNamespace(
        core=core, project=project, task=task, launch=launch, sup=sup,
        clock=clock, jid=jid, backend=backend,
    )


def make_d1_scheduler(env, governor=None):
    gov = governor or FakeGovernor(d1_admission())
    return Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60,
                     resource_governor=gov, snapshot_provider=FakeSnapshotProvider())


def drive_d1(sched, jid, max_passes=15):
    final = None
    for _ in range(max_passes):
        r = sched.run_pass(jid)
        final = r
        if r.outcome in ("resource_deferred", "resource_denied",
                         "context_build_failed"):
            break
    return final
