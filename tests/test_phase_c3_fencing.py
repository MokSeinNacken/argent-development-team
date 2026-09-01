"""Phase C3 — fencing + exactly-once (G).  Deterministic.

A stale lease can never classify/transition a job; a dual supervisor race is
resolved by the holder CAS; the recovery commit is exactly-once.
"""

from __future__ import annotations

import pytest

from argent_core import Core
from argent_core.models import LeaseFencedError
from argent_core.resource_failure import TerminationClass
from argent_core.resource_recovery import FailureClass, RecoveryDecision
from c3_helpers import (
    build_running_job,
    make_scheduler,
    register_terminal_evidence,
)


def test_stale_owner_cannot_commit(db_path):
    env = build_running_job(Core(db_path), owner="A")
    with pytest.raises(LeaseFencedError):
        env.core._store.commit_recovery_decision(
            env.jid, owner_instance_id="B", lease_epoch=env.epoch,
            failure_class=FailureClass.RESOURCE_OOM,
            recovery_decision=RecoveryDecision.BLOCK_RESOURCE,
            reason_code="RESOURCE_OOM",
        )
    # Job unchanged (still RUNNING under A).
    row = env.core._store.get_supervisor_job(env.jid)
    assert row["primary_state"] == "RUNNING"
    assert row["owner_instance_id"] == "A"


def test_stale_epoch_cannot_commit(db_path):
    env = build_running_job(Core(db_path), owner="A")
    with pytest.raises(LeaseFencedError):
        env.core._store.commit_recovery_decision(
            env.jid, owner_instance_id="A", lease_epoch=env.epoch + 1,
            failure_class=FailureClass.RESOURCE_OOM,
            recovery_decision=RecoveryDecision.BLOCK_RESOURCE,
            reason_code="RESOURCE_OOM",
        )


def test_dual_supervisor_only_valid_holder_mutates(db_path):
    env = build_running_job(Core(db_path), owner="A")
    # Register terminal OOM evidence for the job.
    register_terminal_evidence(
        env.core, env.jid, termination_class=TerminationClass.OOM_KILL.value,
        exit_code=137, scope_events={"oom_kill": 1, "oom_group_kill": 0,
                                     "max": 0, "high": 0},
    )
    # Supervisor B (foreign owner) cannot classify/recover the job.
    sched_b = make_scheduler(env, owner="B")
    result = sched_b.classify_and_recover(env.jid, env.epoch)
    assert result is None  # fenced -> no-op
    row = env.core._store.get_supervisor_job(env.jid)
    assert row["primary_state"] == "RUNNING"
    assert row["owner_instance_id"] == "A"

    # Supervisor A (valid holder) commits exactly once.
    sched_a = make_scheduler(env, owner="A")
    result = sched_a.classify_and_recover(env.jid, env.epoch)
    assert result is not None
    assert result.outcome == "resource_recovered"
    row = env.core._store.get_supervisor_job(env.jid)
    assert row["primary_state"] == "BLOCKED"

    # A second attempt is refused (job no longer RUNNING).
    assert sched_a.classify_and_recover(env.jid, env.epoch) is None


def test_exactly_once_commit_is_idempotent(db_path):
    env = build_running_job(Core(db_path), owner="A")
    env.core._store.commit_recovery_decision(
        env.jid, owner_instance_id="A", lease_epoch=env.epoch,
        failure_class=FailureClass.RESOURCE_TIMEOUT,
        recovery_decision=RecoveryDecision.RETRY_BOUNDED,
        reason_code="RESOURCE_TIMEOUT",
        next_eligible_at="2026-09-01T00:05:00+00:00",
    )
    # attempt_no bumped exactly once (1), not twice.
    row = env.core._store.get_supervisor_job(env.jid)
    assert row["attempt_no"] == 1
    # A second commit for the same event is refused.
    with pytest.raises(Exception):
        env.core._store.commit_recovery_decision(
            env.jid, owner_instance_id="A", lease_epoch=env.epoch,
            failure_class=FailureClass.RESOURCE_TIMEOUT,
            recovery_decision=RecoveryDecision.RETRY_BOUNDED,
            reason_code="RESOURCE_TIMEOUT",
            next_eligible_at="2026-09-01T00:05:00+00:00",
        )
    assert env.core._store.get_supervisor_job(env.jid)["attempt_no"] == 1
