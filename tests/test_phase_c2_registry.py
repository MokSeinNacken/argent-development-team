"""Phase C2 — process registry binding + restart (deterministic).

Proves that scope metadata (scope_ref / resource_class / policy_version /
bounded effective_limits) is persisted and survives a reopen, that the B4
identity classification is unchanged (PID reuse / boot change), and that a stale
scope ref from an old boot is historical evidence only (never re-authorises a
spawn).
"""

from __future__ import annotations

import json

from argent_core import Core, OWNER_SOURCE, Role, role_source
from argent_core.process_registry import (
    IDENTITY_BOOT_CHANGED,
    IDENTITY_PID_REUSE,
    ProcessIdentity,
    ProcessRegistry,
)
from argent_core.resource_policy import ResourceClass, ResourcePolicy

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


def _make_job(core):
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    core.start_task_run(task.id, OWNER)
    return task.id


def test_scope_metadata_persists_across_reopen(db_path):
    core = Core(db_path)
    task_id = _make_job(core)
    # Use the supervisor to create a job id deterministically.
    from argent_core.supervisor import Supervisor
    from mock_supervisor_runtime import FakeRunLauncher, FakeRunStatusProvider
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher())
    job = sup.store.create_job(task_id, idempotency_key="job-1",
                               resource_class=ResourceClass.HEAVY.value)
    jid = job.supervisor_job_id

    reg = ProcessRegistry(core._store)
    reg.register(
        job_id=jid, dispatch_id=None,
        identity=ProcessIdentity(boot_id="boot-1", pid=100, process_start_ticks=42),
        cgroup_ref="/user.slice/test/x.scope",
        scope_ref="argent-c2-job-dispatch-abcdef01.scope",
        resource_class=ResourceClass.HEAVY.value,
        policy_version="1",
        effective_limits=_limits(),
        timed_out=False,
        scope_events={"oom_kill": 0, "max": 0, "high": 0},
    )
    core.close()

    # Reopen (new Store on the same file) -> scope metadata still there.
    core2 = Core(db_path)
    try:
        rows = core2._store.list_process_registrations(jid)
        assert len(rows) == 1
        row = rows[0]
        assert row["scope_ref"] == "argent-c2-job-dispatch-abcdef01.scope"
        assert row["cgroup_ref"] == "/user.slice/test/x.scope"
        assert row["resource_class"] == "HEAVY"
        assert row["policy_version"] == "1"
        assert row["timed_out"] == 0
        # effective_limits + scope_events are bounded JSON (parseable).
        limits = json.loads(row["effective_limits"])
        assert limits["memory_max_bytes"] == _limits()["memory_max_bytes"]
        assert json.loads(row["scope_events"]) == {"oom_kill": 0, "max": 0, "high": 0}
    finally:
        core2.close()


def test_effective_limits_not_serializable_is_dropped_fail_closed(db_path):
    core = Core(db_path)
    task_id = _make_job(core)
    from argent_core.supervisor import Supervisor
    from mock_supervisor_runtime import FakeRunLauncher, FakeRunStatusProvider
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher())
    job = sup.store.create_job(task_id, idempotency_key="job-1")
    jid = job.supervisor_job_id

    reg = ProcessRegistry(core._store)
    row = reg.register(
        job_id=jid, dispatch_id=None,
        identity=ProcessIdentity(boot_id="boot-1", pid=101, process_start_ticks=7),
        effective_limits={"bad": object()},  # non-serialisable -> dropped
    )
    assert row["effective_limits"] is None  # bounded evidence, never a dump
    core.close()


def test_pid_reuse_and_boot_change_classification_unchanged():
    reg = ProcessRegistry(object())
    registered = {"boot_id": "boot-1", "pid": 200, "process_start_ticks": 100}
    assert reg.classify_identity(
        registered, ProcessIdentity(boot_id="boot-1", pid=200, process_start_ticks=100),
    ) == "same"
    assert reg.classify_identity(
        registered, ProcessIdentity(boot_id="boot-1", pid=200, process_start_ticks=999),
    ) == IDENTITY_PID_REUSE
    assert reg.classify_identity(
        registered, ProcessIdentity(boot_id="boot-2", pid=200, process_start_ticks=100),
    ) == IDENTITY_BOOT_CHANGED


def test_old_boot_id_scope_ref_is_historical_only(db_path):
    core = Core(db_path)
    task_id = _make_job(core)
    from argent_core.supervisor import Supervisor
    from mock_supervisor_runtime import FakeRunLauncher, FakeRunStatusProvider
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher())
    job = sup.store.create_job(task_id, idempotency_key="job-1")
    jid = job.supervisor_job_id

    reg = ProcessRegistry(core._store)
    reg.register(
        job_id=jid, dispatch_id=None,
        identity=ProcessIdentity(boot_id="boot-old", pid=300, process_start_ticks=5),
        scope_ref="argent-c2-old.scope",
        resource_class="LIGHT",
    )
    row = core._store.list_process_registrations(jid)[-1]
    # A live process from a NEW boot never matches the old scope ref: the old
    # registration is historical evidence only (boot_changed).
    assert reg.classify_identity(
        row, ProcessIdentity(boot_id="boot-new", pid=300, process_start_ticks=5),
    ) == IDENTITY_BOOT_CHANGED
    core.close()
