"""Shared deterministic helpers for the Phase C3 resource-recovery tests.

Builds a RUNNING (claimed) supervisor job and provides the fenced recovery
commit path without any host I/O.  Every value is explicit and deterministic.
"""

from __future__ import annotations

from types import SimpleNamespace

from argent_core import Core, OWNER_SOURCE
from argent_core.resource_policy import ResourceClass
from argent_core.supervisor import Supervisor
from mock_supervisor_runtime import FakeRunLauncher, FakeRunStatusProvider

#: Deterministic boot_id used by every C3 helper (matches the persisted
#: terminal evidence).  A fake identity provider is injected so the C3/F3
#: boot_id-consistency binding is deterministic (no real /proc reads).
BOOT_ID = "boot-1"


def _stat_line(starttime):
    """A well-formed /proc/<pid>/stat line with the given field-22 starttime."""
    tail = " ".join(str(v) for v in range(4, 22))
    return f"123 (fake) S {tail} {starttime}"


def fake_identity_provider(boot_id=BOOT_ID, pid=100, ticks=42):
    from argent_core.process_registry import ProcessIdentityProvider
    return ProcessIdentityProvider(
        boot_id_reader=lambda: boot_id,
        stat_reader=lambda p: _stat_line(ticks) if p == pid else None,
    )


def build_running_job(
    core: Core,
    *,
    owner: str = "A",
    ttl: int = 300,
    resource_class: str = ResourceClass.LIGHT.value,
    idempotency_key: str = "job-1",
) -> SimpleNamespace:
    """Create + claim a job so it is RUNNING under ``owner`` at epoch 1."""
    project = core.create_project("p", OWNER_SOURCE)
    task = core.create_task(project.id, "t", OWNER_SOURCE)
    core.start_task_run(task.id, OWNER_SOURCE)
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher())
    sup._process_identity_provider = fake_identity_provider()
    job = sup.store.create_job(
        task.id, idempotency_key=idempotency_key, resource_class=resource_class,
    )
    jid = job.supervisor_job_id
    claimed = core._store.claim_job(jid, owner_instance_id=owner, ttl_seconds=ttl)
    return SimpleNamespace(
        core=core, sup=sup, task_id=task.id, jid=jid,
        epoch=claimed["lease_epoch"], owner=owner,
    )


def register_terminal_evidence(
    core: Core,
    jid: str,
    *,
    termination_class,
    exit_code=None,
    timed_out: bool = False,
    scope_events=None,
) -> str:
    """Register a process and mark it TERMINAL with C3 evidence."""
    from argent_core.process_registry import (
        ProcessIdentity,
        ProcessRegistry,
    )
    reg = ProcessRegistry(core._store)
    row = reg.register(
        job_id=jid, dispatch_id=None,
        identity=ProcessIdentity(boot_id="boot-1", pid=100,
                                 process_start_ticks=42),
    )
    reg.mark_terminal(
        row["process_id"], exit_code=exit_code,
        terminal_at="2026-09-01T00:00:00+00:00",
        termination_class=termination_class, timed_out=timed_out,
        scope_events=scope_events,
    )
    return row["process_id"]


def make_scheduler(env, *, owner="A", recovery_policy=None):
    """Build a Scheduler wired to ``env`` with deterministic fakes."""
    from argent_core.scheduler import Scheduler
    from c2_helpers import FakeGovernor, FakeSnapshotProvider
    if getattr(env.sup, "_process_identity_provider", None) is None:
        env.sup._process_identity_provider = fake_identity_provider()
    return Scheduler(
        env.sup, owner_instance_id=owner, lease_ttl_seconds=300,
        resource_governor=FakeGovernor(),
        snapshot_provider=FakeSnapshotProvider(),
        recovery_policy=recovery_policy,
    )
