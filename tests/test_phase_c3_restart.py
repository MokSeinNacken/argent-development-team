"""Phase C3 — restart / crash recovery (H).  Deterministic.

Classification + recovery survive a crash at every boundary: before the
classification is persisted, after the classification, before the transition,
after an old boot, and for a still-running scope (no duplicate spawn).
"""

from __future__ import annotations

from argent_core import Core
from argent_core.resource_failure import TerminationClass
from argent_core.resource_recovery import FailureClass, RecoveryDecision
from argent_core.supervisor import Supervisor
from c2_helpers import FakeGovernor, FakeSnapshotProvider
from c3_helpers import (
    build_running_job,
    fake_identity_provider,
    make_scheduler,
    register_terminal_evidence,
)
from mock_supervisor_runtime import FakeRunLauncher, FakeRunStatusProvider


def _scheduler_on(core, *, owner="A", recovery_policy=None):
    from argent_core.scheduler import Scheduler
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher())
    sup._process_identity_provider = fake_identity_provider()
    return Scheduler(
        sup, owner_instance_id=owner, lease_ttl_seconds=300,
        resource_governor=FakeGovernor(),
        snapshot_provider=FakeSnapshotProvider(),
        recovery_policy=recovery_policy,
    )


def test_crash_before_classification_persist_reclassifies_once(db_path):
    # Terminal evidence persisted, but NO recovery decision committed; a fresh
    # scheduler re-classifies and commits exactly once.
    env = build_running_job(Core(db_path), owner="A")
    register_terminal_evidence(
        env.core, env.jid, termination_class=TerminationClass.OOM_KILL.value,
        exit_code=137, scope_events={"oom_kill": 1, "oom_group_kill": 0,
                                     "max": 0, "high": 0},
    )
    jid = env.jid
    epoch = env.epoch
    env.core.close()

    core2 = Core(db_path)
    try:
        sched = _scheduler_on(core2, owner="A")
        result = sched.classify_and_recover(jid, epoch)
        assert result is not None
        row = core2._store.get_supervisor_job(jid)
        assert row["primary_state"] == "BLOCKED"
        assert row["last_failure_class"] == "RESOURCE_OOM"
    finally:
        core2.close()


def test_crash_after_classification_before_transition_is_exactly_once(db_path):
    env = build_running_job(Core(db_path), owner="A")
    env.core._store.commit_recovery_decision(
        env.jid, owner_instance_id="A", lease_epoch=env.epoch,
        failure_class=FailureClass.RESOURCE_OOM,
        recovery_decision=RecoveryDecision.BLOCK_RESOURCE,
        reason_code="RESOURCE_OOM",
    )
    jid = env.jid
    env.core.close()
    core2 = Core(db_path)
    try:
        row = core2._store.get_supervisor_job(jid)
        assert row["primary_state"] == "BLOCKED"
        assert row["last_failure_class"] == "RESOURCE_OOM"
        # A re-open + re-drive cannot commit twice (job not RUNNING).
        sched = _scheduler_on(core2, owner="A")
        assert sched.classify_and_recover(jid, 1) is None
    finally:
        core2.close()


def test_old_boot_evidence_is_not_reapplied(db_path):
    from argent_core.process_registry import ProcessIdentity, ProcessRegistry
    env = build_running_job(Core(db_path), owner="A")
    reg = ProcessRegistry(env.core._store)
    reg.register(
        job_id=env.jid, dispatch_id=None,
        identity=ProcessIdentity(boot_id="boot-old", pid=100,
                                 process_start_ticks=42),
    )
    # Not TERMINAL -> not a classification point (old boot is historical only).
    sched = make_scheduler(env, owner="A")
    assert sched.classify_and_recover(env.jid, env.epoch) is None
    assert env.core._store.get_supervisor_job(env.jid)["primary_state"] == "RUNNING"


def test_running_scope_is_not_duplicated(db_path):
    from argent_core.process_registry import ProcessIdentity, ProcessRegistry
    env = build_running_job(Core(db_path), owner="A")
    reg = ProcessRegistry(env.core._store)
    reg.register(
        job_id=env.jid, dispatch_id=None,
        identity=ProcessIdentity(boot_id="boot-1", pid=100,
                                 process_start_ticks=42),
        status="RUNNING",
    )
    sched = make_scheduler(env, owner="A")
    assert sched.classify_and_recover(env.jid, env.epoch) is None
    assert env.core._store.get_supervisor_job(env.jid)["primary_state"] == "RUNNING"


def test_no_registration_is_unknown_not_a_failure(db_path):
    env = build_running_job(Core(db_path), owner="A")
    sched = make_scheduler(env, owner="A")
    assert sched.classify_and_recover(env.jid, env.epoch) is None
