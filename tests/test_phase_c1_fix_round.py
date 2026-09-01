"""Phase C1 — fix-round acceptance tests (F1–F5, deterministic, no host stress).

Covers the Sol closing-review findings:

* F1 — concurrency evidence in the default scheduler path (store-backed
  active-jobs reader + UNKNOWN fail-closed);
* F2 — full host-exclusivity / concurrency matrix;
* F3 — spawn-adjacent preflight is the binding admission point (continuation);
* F4 — longest enclosing mountpoint for the /tmp filesystem type;
* F5 — ``persist_resource_decision`` holder-CAS store operation.
"""

from __future__ import annotations

from types import SimpleNamespace

from argent_core import Core, OWNER_SOURCE, Role, role_source
from argent_core.host_snapshot import HostSnapshotProvider, parse_mounts
from argent_core.models import LeaseError, LeaseFencedError
from argent_core.resource_governor import (
    AdmissionVerdict,
    ResourceGovernor,
    ResourceReasonCode,
)
from argent_core.resource_policy import ResourceClass, gib
from argent_core.scheduler import Scheduler
from argent_core.supervisor import RunStatus, Supervisor
from c1_helpers import make_snapshot
from mock_supervisor_runtime import (
    FakeClock,
    FakeRunLauncher,
    FakeRunStatusProvider,
    make_run_observation,
)

OWNER = OWNER_SOURCE
LEAD = role_source(Role.LEAD)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class FakeGovernor:
    """Scriptable governor returning a canned decision."""

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


class ScriptedSnapshotProvider:
    """Returns snapshots from a queue (sticky-last)."""

    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.captures = []

    def capture(self, workspace_path=None):
        self.captures.append(workspace_path)
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]


def _admission(verdict, reason, *, next_eligible_at=None, snapshot_ref="snap-1"):
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


def _healthy_fake_readers():
    """Deterministic healthy-host readers (8 GiB RAM, 6 GiB avail, tmpfs /tmp)."""
    return {
        "meminfo_reader": lambda: (
            "MemTotal: 8388608 kB\nMemAvailable: 6291456 kB\n"
            "SwapTotal: 2097152 kB\nSwapFree: 2097152 kB\n"
        ),
        "loadavg_reader": lambda: "0.1 0.2 0.3 1/1 1\n",
        "mounts_reader": lambda: "tmpfs /tmp tmpfs rw 0 0\n",
        "statvfs_reader": lambda path: (gib(100), 0.5),
        "cpu_count_reader": lambda: 8,
    }


def make_env(db_path, clock=None, resource_class=None):
    clock = clock or FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    core.start_task_run(task.id, OWNER)
    launch = FakeRunLauncher()
    sup = Supervisor(core, FakeRunStatusProvider(), launch, clock=clock)
    job = sup.store.create_job(
        task.id, idempotency_key="job-main",
        resource_class=resource_class or ResourceClass.HEAVY.value,
    )
    jid = job.supervisor_job_id
    return SimpleNamespace(core=core, project=project, task=task, launch=launch,
                           sup=sup, clock=clock, jid=jid)


def _row(env):
    return env.core._store.get_supervisor_job(env.jid)


# ---------------------------------------------------------------------------
# F1 — concurrency evidence in the default path
# ---------------------------------------------------------------------------

def test_unreadable_active_jobs_is_unknown_and_fails_closed():
    gov = ResourceGovernor()
    now = "2026-09-01T00:00:00+00:00"
    snap = make_snapshot(unknown_fields={"active_jobs"})
    for cls in (ResourceClass.MEDIUM, ResourceClass.HEAVY, ResourceClass.EXCLUSIVE):
        d = gov.decide(resource_class=cls, snapshot=snap, now_iso=now)
        assert d.decision == AdmissionVerdict.DENY_LOCAL.value
        assert d.reason_code == ResourceReasonCode.RESOURCE_EVIDENCE_UNKNOWN.value
    d = gov.decide(resource_class=ResourceClass.LIGHT, snapshot=snap, now_iso=now)
    assert d.decision == AdmissionVerdict.DEFER.value
    assert d.reason_code == ResourceReasonCode.RESOURCE_EVIDENCE_UNKNOWN.value


def test_default_scheduler_wires_store_reader_returning_known_empty(db_path):
    env = make_env(db_path, resource_class=ResourceClass.MEDIUM.value)
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60)
    # Default wiring: a real store-backed reader (not None).
    reader = sched._snapshot_provider._active_jobs_reader
    assert reader is not None
    # A readable store with no RUNNING jobs yields [] (known-empty, NOT UNKNOWN),
    # so LIGHT jobs with [] active jobs + healthy host still ALLOW.
    assert reader() == []
    env.core.close()


def test_single_medium_job_not_self_blocked_by_default_reader(db_path):
    env = make_env(db_path, resource_class=ResourceClass.MEDIUM.value)
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60)
    sched._snapshot_provider = HostSnapshotProvider(
        **_healthy_fake_readers(),
        active_jobs_reader=sched._store_active_jobs_reader(),
    )
    r = sched.run_pass(env.jid)
    # A lone MEDIUM job must not concurrency-block itself.
    assert r.outcome not in ("resource_deferred", "resource_denied")
    assert _row(env)["primary_state"] == "RUNNING"
    env.core.close()


def test_dual_supervisor_store_reader_blocks_second_writer(db_path):
    clock = FakeClock()
    core1 = Core(db_path, clock=clock)
    project = core1.create_project("p", OWNER)
    t1 = core1.create_task(project.id, "t1", OWNER)
    t2 = core1.create_task(project.id, "t2", OWNER)
    core1.start_task_run(t1.id, OWNER)
    core1.start_task_run(t2.id, OWNER)
    sup1 = Supervisor(core1, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock)
    j1 = sup1.store.create_job(t1.id, idempotency_key="j1",
                               resource_class=ResourceClass.MEDIUM.value)
    j2 = sup1.store.create_job(t2.id, idempotency_key="j2",
                               resource_class=ResourceClass.MEDIUM.value)
    jid1, jid2 = j1.supervisor_job_id, j2.supervisor_job_id

    # A claims job1 (deterministic ALLOW) -> RUNNING.
    allow = FakeGovernor(_admission(
        AdmissionVerdict.ALLOW.value, ResourceReasonCode.OK.value,
    ))
    sched_a = Scheduler(sup1, owner_instance_id="A", lease_ttl_seconds=60,
                        resource_governor=allow,
                        snapshot_provider=FakeSnapshotProvider())
    assert sched_a.run_pass(jid1).outcome not in (
        "resource_deferred", "resource_denied")
    assert core1._store.get_supervisor_job(jid1)["primary_state"] == "RUNNING"

    # B on a second connection sees job1 via the store-backed reader.
    core2 = Core(db_path, clock=clock)
    launch2 = FakeRunLauncher()
    sup2 = Supervisor(core2, FakeRunStatusProvider(), launch2, clock=clock)
    sched_b = Scheduler(sup2, owner_instance_id="B", lease_ttl_seconds=60)
    sched_b._snapshot_provider = HostSnapshotProvider(
        **_healthy_fake_readers(),
        active_jobs_reader=sched_b._store_active_jobs_reader(),
    )
    rb = sched_b.run_pass(jid2)
    assert rb.outcome == "resource_deferred"
    assert rb.detail == ResourceReasonCode.CONCURRENCY_LIMIT.value
    assert launch2.spawns == []  # no spawn
    row = core2._store.get_supervisor_job(jid2)
    assert row["primary_state"] == "QUEUED"
    assert row["queue_reason"] == "RESOURCE_DEFERRED"
    core1.close()
    core2.close()


# ---------------------------------------------------------------------------
# F2 — full host-exclusivity / concurrency matrix
# ---------------------------------------------------------------------------

_MATRIX = [
    # (candidate, active classes, expected verdict)
    ("LIGHT", [], "ALLOW"),
    ("LIGHT", ["LIGHT"], "ALLOW"),
    ("LIGHT", ["LIGHT", "LIGHT"], "DEFER"),
    ("LIGHT", ["MEDIUM"], "ALLOW"),
    ("LIGHT", ["HEAVY"], "ALLOW"),
    ("LIGHT", ["EXCLUSIVE"], "DEFER"),
    ("LIGHT", ["LIGHT", "MEDIUM"], "ALLOW"),
    ("MEDIUM", [], "ALLOW"),
    ("MEDIUM", ["LIGHT"], "ALLOW"),
    ("MEDIUM", ["MEDIUM"], "DEFER"),
    ("MEDIUM", ["HEAVY"], "DEFER"),
    ("MEDIUM", ["EXCLUSIVE"], "DEFER"),
    ("MEDIUM", ["LIGHT", "MEDIUM"], "DEFER"),
    ("HEAVY", [], "ALLOW"),
    ("HEAVY", ["LIGHT"], "ALLOW"),
    ("HEAVY", ["MEDIUM"], "DEFER"),
    ("HEAVY", ["HEAVY"], "DEFER"),
    ("HEAVY", ["EXCLUSIVE"], "DEFER"),
    ("HEAVY", ["LIGHT", "HEAVY"], "DEFER"),
    ("EXCLUSIVE", [], "ALLOW"),
    ("EXCLUSIVE", ["LIGHT"], "DEFER"),
    ("EXCLUSIVE", ["MEDIUM"], "DEFER"),
    ("EXCLUSIVE", ["HEAVY"], "DEFER"),
    ("EXCLUSIVE", ["EXCLUSIVE"], "DEFER"),
    ("EXCLUSIVE", ["LIGHT", "HEAVY"], "DEFER"),
]


def test_full_concurrency_matrix():
    gov = ResourceGovernor()
    now = "2026-09-01T00:00:00+00:00"
    for candidate, active, expected in _MATRIX:
        snap = make_snapshot(
            mem_total=gib(32), mem_available=gib(24),
            active_jobs=[(f"j{i}", cls) for i, cls in enumerate(active)],
        )
        d = gov.decide(
            resource_class=ResourceClass(candidate), snapshot=snap, now_iso=now,
        )
        assert d.decision == expected, (
            f"candidate={candidate} active={active}: expected {expected}, "
            f"got {d.decision} ({d.reason_code})"
        )
        if expected == "DEFER":
            assert d.reason_code == ResourceReasonCode.CONCURRENCY_LIMIT.value


# ---------------------------------------------------------------------------
# F3 — spawn-adjacent preflight is the binding admission point
# ---------------------------------------------------------------------------

def test_spawn_adjacent_preflight_defers_after_host_pressure(db_path):
    env = make_env(db_path, resource_class=ResourceClass.HEAVY.value)
    # Healthy on the claim pass; low memory on every later (spawn-gate) capture.
    snap = ScriptedSnapshotProvider([
        make_snapshot(),
        make_snapshot(mem_available=gib(4)),
    ])
    gov = ResourceGovernor()
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=gov, snapshot_provider=snap)

    final = None
    for _ in range(12):
        r = sched.run_pass(env.jid)
        final = r
        if r.outcome in ("resource_deferred", "resource_denied"):
            break
        # The launcher must never fire in this test (host pressure blocks it).
        assert env.launch.spawns == [], "agent must never spawn"

    assert final is not None
    assert final.outcome == "resource_deferred"
    assert final.detail == ResourceReasonCode.INSUFFICIENT_MEMORY_RESERVE.value
    assert env.launch.spawns == []
    row = _row(env)
    assert row["primary_state"] == "QUEUED"
    assert row["queue_reason"] == "RESOURCE_DEFERRED"
    assert row["next_eligible_at"] is not None
    env.core.close()


def test_spawn_adjacent_preflight_after_wait_wake(db_path):
    """A WAIT-WAKE reclaim pass also re-runs preflight before spawn (F3)."""
    env = make_env(db_path, resource_class=ResourceClass.HEAVY.value)
    snap = ScriptedSnapshotProvider([
        make_snapshot(),
        make_snapshot(mem_available=gib(4)),
    ])
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=ResourceGovernor(),
                      snapshot_provider=snap)

    # Pass 1: claim (ALLOW) -> START_ROLE.
    assert sched.run_pass(env.jid).outcome not in (
        "resource_deferred", "resource_denied")
    # Pass 2: CREATE_DISPATCH (dispatch now PENDING).
    assert sched.run_pass(env.jid).outcome not in (
        "resource_deferred", "resource_denied")
    dispatches = env.core._store.list_dispatches(env.task.id)
    assert len(dispatches) == 1
    d = dispatches[0]

    # Script a RUNNING-without-binding observation -> reconcile plans WAIT.
    env.sup._run_status.script(d.id, [
        make_run_observation(
            dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        ),
    ])
    # Pass 3: WAIT (no spawn).
    assert sched.run_pass(env.jid).outcome not in (
        "resource_deferred", "resource_denied")
    assert _row(env)["next_wake_at"] is not None

    # Wake: advance past the 2s wake deadline (well within the 60s lease) and
    # script NOT_FOUND -> SPAWN_RUN.
    env.clock.advance(10)
    env.sup._run_status.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.NOT_FOUND,
        authoritative_not_found=True,
    ))
    # Pass 4: SPAWN_RUN gated by a fresh (low-memory) preflight -> DEFER.
    r = sched.run_pass(env.jid)
    assert r.outcome == "resource_deferred"
    assert r.detail == ResourceReasonCode.INSUFFICIENT_MEMORY_RESERVE.value
    assert env.launch.spawns == []
    row = _row(env)
    assert row["primary_state"] == "QUEUED"
    assert row["queue_reason"] == "RESOURCE_DEFERRED"
    env.core.close()


# ---------------------------------------------------------------------------
# F4 — longest enclosing mountpoint for /tmp fs type
# ---------------------------------------------------------------------------

def test_tmp_fs_type_separate_tmpfs_mount():
    mounts = parse_mounts(
        "/dev/sda1 / ext4 rw 0 0\ntmpfs /tmp tmpfs rw 0 0\n"
    )
    assert HostSnapshotProvider._enclosing_fs_type(mounts, "/tmp") == "tmpfs"


def test_tmp_fs_type_root_mount_when_no_dedicated_tmp():
    mounts = parse_mounts("/dev/sda1 / ext4 rw 0 0\n")
    assert HostSnapshotProvider._enclosing_fs_type(mounts, "/tmp") == "ext4"


def test_tmp_fs_type_nested_mountpoints():
    mounts = parse_mounts(
        "/dev/sda1 / ext4 rw 0 0\n"
        "tmpfs /tmp tmpfs rw 0 0\n"
        "tmpfs /tmp/sub tmpfs rw 0 0\n"
    )
    assert HostSnapshotProvider._enclosing_fs_type(mounts, "/tmp") == "tmpfs"
    assert HostSnapshotProvider._enclosing_fs_type(mounts, "/tmp/foo") == "tmpfs"
    assert HostSnapshotProvider._enclosing_fs_type(mounts, "/tmp/sub/x") == "tmpfs"
    assert HostSnapshotProvider._enclosing_fs_type(mounts, "/var") == "ext4"


def test_provider_resolves_root_fs_type_not_unknown():
    provider = HostSnapshotProvider(
        meminfo_reader=lambda: (
            "MemTotal: 1000 kB\nMemAvailable: 500 kB\n"
            "SwapTotal: 0 kB\nSwapFree: 0 kB\n"
        ),
        loadavg_reader=lambda: "0.1 0.2 0.3 1/1 1\n",
        mounts_reader=lambda: "/dev/sda1 / ext4 rw 0 0\n",
        statvfs_reader=lambda path: (gib(100), 0.5),
        cpu_count_reader=lambda: 8,
    )
    snap = provider.capture(None)
    assert snap.tmp_fs_type == "ext4"
    assert "tmp_fs_type" not in snap.unknown_fields


# ---------------------------------------------------------------------------
# F5 — persist_resource_decision holder-CAS store operation
# ---------------------------------------------------------------------------

def test_persist_resource_decision_holder_cas(db_path):
    env = make_env(db_path, resource_class=ResourceClass.MEDIUM.value)
    store = env.core._store
    store.claim_job(env.jid, owner_instance_id="A", ttl_seconds=60)

    # Correct holder writes the audit columns.
    store.persist_resource_decision(
        env.jid,
        owner_instance_id="A",
        lease_epoch=1,
        last_resource_decision=AdmissionVerdict.ALLOW.value,
        last_resource_reason_code=ResourceReasonCode.OK.value,
        last_resource_snapshot_hash="snap-x",
        last_resource_at="2026-09-01T00:00:00+00:00",
    )
    row = store.get_supervisor_job(env.jid)
    assert row["last_resource_decision"] == "ALLOW"
    assert row["last_resource_reason_code"] == "OK"
    env.core.close()


def test_persist_resource_decision_refuses_stale_epoch(db_path):
    env = make_env(db_path, resource_class=ResourceClass.MEDIUM.value)
    store = env.core._store
    store.claim_job(env.jid, owner_instance_id="A", ttl_seconds=60)

    with __import__("pytest").raises(LeaseFencedError):
        store.persist_resource_decision(
            env.jid,
            owner_instance_id="A",
            lease_epoch=999,  # stale epoch
            last_resource_decision="ALLOW",
            last_resource_reason_code="OK",
            last_resource_snapshot_hash="snap-x",
            last_resource_at="2026-09-01T00:00:00+00:00",
        )
    row = store.get_supervisor_job(env.jid)
    assert row["last_resource_decision"] is None  # nothing persisted
    env.core.close()


def test_persist_resource_decision_refuses_terminal_job(db_path):
    env = make_env(db_path, resource_class=ResourceClass.MEDIUM.value)
    store = env.core._store
    store._update_supervisor_job(
        env.jid, terminal="DONE", status="TERMINAL", next_action="NONE",
    )
    with __import__("pytest").raises(LeaseError):
        store.persist_resource_decision(
            env.jid,
            owner_instance_id="A",
            lease_epoch=1,
            last_resource_decision="ALLOW",
            last_resource_reason_code="OK",
            last_resource_snapshot_hash="snap-x",
            last_resource_at="2026-09-01T00:00:00+00:00",
        )
    env.core.close()
