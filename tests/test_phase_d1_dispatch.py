"""Phase D1 — dispatch integration (J).  Deterministic Scheduler integration.

Proves the single D1 dispatch point: ``Supervisor._perform_spawn_run`` builds an
immutable Context Pack from trusted facts BEFORE ``_spawn_scoped``; a build
failure is fail-closed (no dispatch, no legacy fallback).  A permanent context
error (e.g. CONTEXT_BUDGET_EXCEEDED) fail-closes the job to BLOCKED, a
provably transient context error is bounded re-queued with
``error_class=CONTEXT`` (an ORCHESTRATION error — never CODE_FAILURE, never
RESOURCE).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from argent_core import Core, OWNER_SOURCE, Role, role_source
from argent_core.context_pack import (
    CONTEXT_BUDGET_EXCEEDED,
    ContextBuildError,
    ContextBuilder,
)
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


def _limits():
    pol = ResourcePolicy()
    base = pol.limits_for(ResourceClass.HEAVY)
    return {
        "memory_high_bytes": base.memory_high_bytes,
        "memory_max_bytes": base.memory_max_bytes,
        "swap_max_bytes": base.swap_max_bytes,
        "cpu_quota_percent": base.cpu_quota_percent,
        "timeout_seconds": base.timeout_seconds,
    }


def _admission():
    return AdmissionDecision(
        resource_class=ResourceClass.HEAVY.value, policy_version="1",
        snapshot_ref="snap-1", decision=AdmissionVerdict.ALLOW.value,
        reason_code=ResourceReasonCode.OK.value, next_eligible_at=None,
        effective_limits=_limits(), timestamp="2026-09-01T00:00:00+00:00",
    )


class _FailingContextBuilder:
    """A builder that always raises (simulates CONTEXT_BUDGET_EXCEEDED)."""

    def __init__(self, error):
        self.error = error
        self.calls = []

    def build(self, **kwargs):
        self.calls.append(kwargs)
        raise self.error


def _make_env(db_path, context_builder=None):
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER, description="fix the bug")
    core.start_task_run(task.id, OWNER)
    launch = FakeRunLauncher()
    backend = FakeScopeBackend(verify_properties=verified_properties(_limits()))
    enforcer = ExecutionEnforcer(backend)
    sup = Supervisor(core, FakeRunStatusProvider(), launch, clock=clock,
                     enforcer=enforcer, context_builder=context_builder,
                     prompts_dir=Path(db_path).parent / "prompts")
    job = sup.store.create_job(task.id, idempotency_key="job-main",
                               resource_class=ResourceClass.HEAVY.value)
    jid = job.supervisor_job_id
    return SimpleNamespace(
        core=core, project=project, task=task, launch=launch, sup=sup,
        clock=clock, jid=jid, backend=backend,
    )


def _drive(sched, jid, max_passes=15):
    final = None
    for _ in range(max_passes):
        r = sched.run_pass(jid)
        final = r
        if r.outcome in ("resource_deferred", "resource_denied",
                         "context_build_failed"):
            break
    return final


def test_success_builds_and_persists_pack_before_spawn(db_path):
    env = _make_env(db_path)  # default real ContextBuilder
    gov = FakeGovernor(_admission())
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=gov, snapshot_provider=FakeSnapshotProvider())

    final = _drive(sched, env.jid)

    assert final is not None
    assert len(env.backend.created) == 1  # exactly one scope -> one spawn
    assert env.launch.spawns == []  # enforcer path, not the legacy launcher
    row = env.core._store.get_supervisor_job(env.jid)
    assert row["primary_state"] == "RUNNING"
    # The pack metadata was persisted with a traceable id + hash.
    dispatch_id = row["expected_dispatch_id"]
    rec = env.core._store.get_context_pack(dispatch_id)
    assert rec is not None
    assert rec.dispatch_id == dispatch_id
    assert rec.content_hash and len(rec.content_hash) == 64
    assert rec.version == "1"
    # The message file rendered from the pack carries the pack id + objective.
    msg_path = None
    for cmd in env.backend.started:
        if "--message-file" in cmd["command"]:
            msg_path = cmd["command"][cmd["command"].index("--message-file") + 1]
    assert msg_path is not None
    content = open(msg_path, encoding="utf-8").read()
    assert "context_pack_id:" in content
    assert "fix the bug" in content
    env.core.close()


def test_build_failure_permanent_blocks_no_dispatch(db_path):
    env = _make_env(db_path, context_builder=_FailingContextBuilder(
        ContextBuildError(CONTEXT_BUDGET_EXCEEDED, "required exceeds hard")))
    gov = FakeGovernor(_admission())
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=gov, snapshot_provider=FakeSnapshotProvider())

    final = _drive(sched, env.jid)

    assert final is not None
    assert final.outcome == "context_build_failed"
    # No scope created, no spawn, no legacy launcher fallback.
    assert len(env.backend.created) == 0
    assert env.launch.spawns == []
    row = env.core._store.get_supervisor_job(env.jid)
    # F6: CONTEXT_BUDGET_EXCEEDED is permanent -> fail-closed to BLOCKED
    # (no bounded retry loop), still taxonomised as an ORCHESTRATION error.
    assert row["primary_state"] == "BLOCKED"
    assert row["terminal"] == "BLOCKED"
    assert row["error_class"] == "CONTEXT"
    assert row["error_class"] not in ("DETERMINISTIC", "RESOURCE")
    assert row["last_error_code"] == CONTEXT_BUDGET_EXCEEDED
    # No pack row was persisted for a failed build.
    assert env.core._store.get_context_pack(row["expected_dispatch_id"]) is None
    env.core.close()


def test_no_silent_legacy_fallback_on_build_failure(db_path):
    """A D1-migrated dispatch must NEVER fall back to the legacy prompt."""
    env = _make_env(db_path, context_builder=_FailingContextBuilder(
        ContextBuildError("CONTEXT_INVALID_VERSION", "bad version")))
    gov = FakeGovernor(_admission())
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=gov, snapshot_provider=FakeSnapshotProvider())

    final = _drive(sched, env.jid)

    assert final.outcome == "context_build_failed"
    # Nothing was launched at all (the legacy path would have called the
    # launcher directly; the enforcer path would have created a scope).
    assert len(env.backend.created) == 0
    assert env.backend.started == []
    assert env.launch.spawns == []
    env.core.close()
