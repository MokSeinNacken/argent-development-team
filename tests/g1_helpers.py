"""Shared deterministic helpers for the Phase G1 background-runtime tests.

Builds the single-active :class:`SupervisorInstance`, the bounded
:class:`SupervisorRuntime`, and a fully wired fake service environment without
any host I/O, systemd activation, network, or real subprocess.  Process
identity is injected through a fake :class:`ProcessIdentityProvider` (same
pattern as Phase B3/C3 helpers).
"""

from __future__ import annotations

from types import SimpleNamespace

from argent_core import Core, OWNER_SOURCE
from argent_core.background_runtime import SupervisorInstance, SupervisorRuntime
from argent_core.external_wait import ExternalWaitManager
from argent_core.process_registry import ProcessIdentityProvider
from argent_core.scheduler import Scheduler
from argent_core.supervisor import Supervisor
from c2_helpers import FakeScopeBackend
from argent_core.scope_enforcer import ExecutionEnforcer
from mock_supervisor_runtime import FakeClock, FakeRunLauncher, FakeRunStatusProvider

OWNER = OWNER_SOURCE


def stat_line(starttime):
    """A well-formed /proc/<pid>/stat line with field-22 = ``starttime``."""
    tail = " ".join(str(v) for v in range(4, 22))
    return f"123 (fake) S {tail} {starttime}"


def make_identity_provider(boot_id, ticks_by_pid, machine_id="machine-1"):
    """Deterministic identity provider (boot_id + per-pid start ticks + host id)."""
    return ProcessIdentityProvider(
        boot_id_reader=lambda: boot_id,
        stat_reader=lambda p: stat_line(ticks_by_pid[p]) if p in ticks_by_pid else None,
        machine_id_reader=lambda: machine_id,
    )


def make_instance(
    store,
    *,
    boot_id="boot-1",
    own_pid=100,
    own_ticks=5,
    instance_id="instance:test",
    pid_alive=None,
    clock=None,
    ttl_seconds=60,
    machine_id="machine-1",
):
    ident = make_identity_provider(boot_id, {own_pid: own_ticks},
                                   machine_id=machine_id)
    return SupervisorInstance(
        store,
        identity_provider=ident,
        instance_id=instance_id,
        own_pid=own_pid,
        pid_alive=pid_alive,
        clock=clock,
        ttl_seconds=ttl_seconds,
    )


def seed_owner(
    store,
    *,
    boot_id="boot-1",
    pid=100,
    ticks=5,
    status="ACTIVE",
    instance_id="instance:old",
    updated_at="u1",
    host_id="machine-1",
):
    """Persist a singleton owner row (for takeover/liveness tests)."""
    row = {
        "singleton_id": "primary",
        "instance_id": instance_id,
        "boot_id": boot_id,
        "host_id": host_id,
        "pid": pid,
        "process_start_ticks": ticks,
        "status": status,
        "acquired_at": "2026-01-01T00:00:00+00:00",
        "lease_expires_at": None,
        "last_heartbeat_at": "2026-01-01T00:00:00+00:00",
        "stopped_at": None,
        "stop_reason": None,
        "last_error_code": None,
        "updated_at": updated_at,
    }
    assert store.cas_supervisor_instance(row=row, expected_revision=None)
    return row


def make_runtime_env(
    db_path,
    *,
    boot_id="boot-1",
    own_pid=100,
    own_ticks=5,
    clock=None,
    sleep_fn=None,
    max_passes=None,
    pid_alive=None,
    adapters=None,
    instance_id="instance:test",
    enforcer=None,
    max_consecutive_errors=None,
):
    """Build a full Core+Supervisor+Scheduler+Instance+Runtime env."""
    clock = clock or FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    sup = Supervisor(
        core,
        FakeRunStatusProvider(),
        FakeRunLauncher(),
        clock=clock,
        enforcer=enforcer or ExecutionEnforcer(FakeScopeBackend()),
    )
    sched = Scheduler(sup, owner_instance_id=instance_id, lease_ttl_seconds=60)
    ewm = ExternalWaitManager(core._store, adapters=adapters or {}, clock=clock)
    instance = make_instance(
        core._store,
        boot_id=boot_id,
        own_pid=own_pid,
        own_ticks=own_ticks,
        instance_id=instance_id,
        pid_alive=pid_alive,
        clock=clock,
    )
    runtime_kwargs = dict(
        scheduler=sched,
        external_wait_manager=ewm,
        instance=instance,
        store=core._store,
        clock=clock,
        sleep_fn=sleep_fn,
        max_passes=max_passes,
    )
    if max_consecutive_errors is not None:
        runtime_kwargs["max_consecutive_errors"] = max_consecutive_errors
    runtime = SupervisorRuntime(**runtime_kwargs)
    return SimpleNamespace(
        core=core, project=project, sup=sup, sched=sched, ewm=ewm,
        instance=instance, runtime=runtime, clock=clock,
    )


def add_queued_job(env, title="job", *, idem=None):
    task = env.core.create_task(env.project.id, title, OWNER)
    job = env.sup.store.create_job(task.id, idempotency_key=idem or f"job-{task.id}")
    return job.supervisor_job_id
