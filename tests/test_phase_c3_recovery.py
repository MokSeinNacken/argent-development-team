"""Phase C3 — recovery decisions (E).  Deterministic.

Bounded retry / defer / block / prefer-external hint / LOST quarantine, and the
hard invariant that a resource failure NEVER authorises code rework.
"""

from __future__ import annotations

from argent_core.resource_recovery import (
    FailureClass,
    RecoveryDecision,
    RecoveryPolicy,
    decide_recovery,
    is_resource_failure,
)


def test_normal_exit_completes():
    assert decide_recovery(FailureClass.NORMAL_EXIT, attempt_no=0) \
        is RecoveryDecision.COMPLETE


def test_code_failure_is_fail_nonresource():
    assert decide_recovery(FailureClass.CODE_OR_PROCESS_FAILURE, attempt_no=0) \
        is RecoveryDecision.FAIL_NONRESOURCE


def test_resource_capacity_defers():
    assert decide_recovery(FailureClass.RESOURCE_CAPACITY_FAILURE, attempt_no=0) \
        is RecoveryDecision.DEFER_RESOURCE


def test_oom_blocks():
    assert decide_recovery(FailureClass.RESOURCE_OOM, attempt_no=0) \
        is RecoveryDecision.BLOCK_RESOURCE


def test_oom_prefer_external_hint_only():
    # PREFER_EXTERNAL is a hint (no external action) — still never an identical
    # retry.
    assert decide_recovery(FailureClass.RESOURCE_OOM, attempt_no=0,
                           prefer_external_available=True) \
        is RecoveryDecision.PREFER_EXTERNAL
    assert decide_recovery(FailureClass.RESOURCE_OOM, attempt_no=0,
                           prefer_external_available=True) \
        is not RecoveryDecision.RETRY_BOUNDED


def test_memory_limit_blocks_by_default():
    assert decide_recovery(FailureClass.RESOURCE_MEMORY_LIMIT, attempt_no=0) \
        is RecoveryDecision.BLOCK_RESOURCE


def test_memory_limit_defer_only_when_policy_allows():
    pol = RecoveryPolicy(allow_memory_limit_defer=True)
    assert decide_recovery(FailureClass.RESOURCE_MEMORY_LIMIT, attempt_no=0,
                           policy=pol) is RecoveryDecision.DEFER_RESOURCE
    # Budget exhausted -> BLOCK (fail-closed).
    assert decide_recovery(FailureClass.RESOURCE_MEMORY_LIMIT, attempt_no=2,
                           policy=pol) is RecoveryDecision.BLOCK_RESOURCE


def test_enforcement_failure_defer_when_provable():
    assert decide_recovery(FailureClass.RESOURCE_ENFORCEMENT_FAILURE, attempt_no=0,
                           has_evidence_unknown=False) \
        is RecoveryDecision.DEFER_RESOURCE


def test_enforcement_failure_lost_when_unknown():
    assert decide_recovery(FailureClass.RESOURCE_ENFORCEMENT_FAILURE, attempt_no=0,
                           has_evidence_unknown=True) \
        is RecoveryDecision.QUARANTINE_LOST


def test_cleanup_unverified_lost():
    assert decide_recovery(FailureClass.SCOPE_CLEANUP_UNVERIFIED, attempt_no=0) \
        is RecoveryDecision.QUARANTINE_LOST


def test_unknown_lost():
    assert decide_recovery(FailureClass.UNKNOWN_TERMINATION, attempt_no=0) \
        is RecoveryDecision.QUARANTINE_LOST


def test_resource_failure_never_authorises_rework():
    # Every resource failure decision is a resource action, never a code
    # rework decision.
    for fc in (FailureClass.RESOURCE_OOM, FailureClass.RESOURCE_MEMORY_LIMIT,
               FailureClass.RESOURCE_TIMEOUT,
               FailureClass.RESOURCE_ENFORCEMENT_FAILURE,
               FailureClass.RESOURCE_CAPACITY_FAILURE,
               FailureClass.SCOPE_CLEANUP_UNVERIFIED,
               FailureClass.UNKNOWN_TERMINATION):
        assert is_resource_failure(fc)
        decision = decide_recovery(fc, attempt_no=0)
        assert decision is not RecoveryDecision.FAIL_NONRESOURCE
        assert decision is not RecoveryDecision.COMPLETE


def test_never_retry_classes_are_rejected_by_policy():
    import pytest
    with pytest.raises(ValueError):
        RecoveryPolicy(retryable_failure_classes=frozenset({"RESOURCE_OOM"}))
    with pytest.raises(ValueError):
        RecoveryPolicy(retryable_failure_classes=frozenset({"UNKNOWN_TERMINATION"}))
