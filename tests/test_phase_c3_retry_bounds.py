"""Phase C3 — retry bounds (F).  Deterministic.

max attempts, bounded backoff, ``next_eligible_at`` persistence, DB reopen, and
the invariant that a resource failure can never produce an infinite retry.
"""

from __future__ import annotations

from argent_core import Core
from argent_core.resource_recovery import (
    FailureClass,
    RecoveryDecision,
    RecoveryPolicy,
    decide_recovery,
    next_eligible_at_after,
)
from c3_helpers import build_running_job


def test_timeout_retry_is_bounded_by_max_attempts():
    pol = RecoveryPolicy(max_resource_retries=2)
    # attempt 0, 1 -> retry; attempt 2+ -> no more RETRY_BOUNDED (and, with the
    # bounded defer budget also exhausted, no unbounded DEFER loop -> BLOCK).
    assert decide_recovery(FailureClass.RESOURCE_TIMEOUT, attempt_no=0,
                           policy=pol) is RecoveryDecision.RETRY_BOUNDED
    assert decide_recovery(FailureClass.RESOURCE_TIMEOUT, attempt_no=1,
                           policy=pol) is RecoveryDecision.RETRY_BOUNDED
    assert decide_recovery(FailureClass.RESOURCE_TIMEOUT, attempt_no=2,
                           policy=pol) is RecoveryDecision.BLOCK_RESOURCE
    assert decide_recovery(FailureClass.RESOURCE_TIMEOUT, attempt_no=99,
                           policy=pol) is RecoveryDecision.BLOCK_RESOURCE


def test_backoff_horizon_is_bounded_and_deterministic():
    t0 = "2026-09-01T00:00:00+00:00"
    t1 = next_eligible_at_after(t0, 300)
    assert t1 == "2026-09-01T00:05:00+00:00"
    # zero/negative -> unchanged (never a negative horizon).
    assert next_eligible_at_after(t0, 0) == t0
    assert next_eligible_at_after(t0, -5) == t0
    # unparseable -> None (fail-closed).
    assert next_eligible_at_after("garbage", 300) is None


def test_next_eligible_at_is_persisted_on_retry(db_path):
    env = build_running_job(Core(db_path))
    row = env.core._store.commit_recovery_decision(
        env.jid, owner_instance_id="A", lease_epoch=env.epoch,
        failure_class=FailureClass.RESOURCE_TIMEOUT,
        recovery_decision=RecoveryDecision.RETRY_BOUNDED,
        reason_code="RESOURCE_TIMEOUT",
        next_eligible_at="2026-09-01T00:05:00+00:00",
    )
    assert row["next_eligible_at"] == "2026-09-01T00:05:00+00:00"
    assert row["attempt_no"] == 1
    assert row["error_class"] == "RESOURCE"


def test_retry_state_survives_reopen(db_path):
    env = build_running_job(Core(db_path))
    env.core._store.commit_recovery_decision(
        env.jid, owner_instance_id="A", lease_epoch=env.epoch,
        failure_class=FailureClass.RESOURCE_TIMEOUT,
        recovery_decision=RecoveryDecision.RETRY_BOUNDED,
        reason_code="RESOURCE_TIMEOUT",
        next_eligible_at="2026-09-01T00:05:00+00:00",
    )
    env.core.close()

    core2 = Core(db_path)
    try:
        row = core2._store.get_supervisor_job(env.jid)
        assert row["primary_state"] == "QUEUED"
        assert row["attempt_no"] == 1
        assert row["queue_reason"] == "RETRY_BACKOFF"
        assert row["last_recovery_decision"] == "RETRY_BOUNDED"
        assert row["last_failure_class"] == "RESOURCE_TIMEOUT"
    finally:
        core2.close()


def test_no_infinite_retry_for_oom(db_path):
    env = build_running_job(Core(db_path))
    # OOM blocks on the first attempt; it can never loop back to QUEUED.
    row = env.core._store.commit_recovery_decision(
        env.jid, owner_instance_id="A", lease_epoch=env.epoch,
        failure_class=FailureClass.RESOURCE_OOM,
        recovery_decision=RecoveryDecision.BLOCK_RESOURCE,
        reason_code="RESOURCE_OOM",
    )
    assert row["primary_state"] == "BLOCKED"
    assert row["terminal"] == "BLOCKED"
    # No automatic reopen (the job is terminal-BLOCKED, not claimable).
    assert not env.core._store._job_is_claimable(row, "2999-01-01T00:00:00+00:00")[0]


def test_defer_resource_bumps_attempt_within_bounded_budget(db_path):
    env = build_running_job(Core(db_path))
    row = env.core._store.commit_recovery_decision(
        env.jid, owner_instance_id="A", lease_epoch=env.epoch,
        failure_class=FailureClass.RESOURCE_CAPACITY_FAILURE,
        recovery_decision=RecoveryDecision.DEFER_RESOURCE,
        reason_code="RESOURCE_CAPACITY_INSUFFICIENT",
        next_eligible_at="2026-09-01T00:05:00+00:00",
    )
    assert row["primary_state"] == "QUEUED"
    # F2: a defer consumes the shared bounded budget (countable, hard-capped).
    assert row["attempt_no"] == 1
    assert row["queue_reason"] == "RESOURCE_DEFERRED"
