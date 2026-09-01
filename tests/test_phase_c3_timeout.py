"""Phase C3 — timeout classification (D).  Deterministic.

Only the trusted wrapper ``timed_out`` marker (or the trusted ``TIMEOUT``
termination class) is a timeout; ``exit 124`` alone is NOT; there is never an
automatic timeout increase.
"""

from __future__ import annotations

from argent_core.resource_failure import TerminationClass
from argent_core.resource_recovery import (
    FailureClass,
    RecoveryDecision,
    RecoveryPolicy,
    classify_failure,
    decide_recovery,
)


def test_trusted_timed_out_marker_is_timeout():
    fc = classify_failure(
        termination_class=None, exit_code=124, timed_out=True, scope_events=None,
    )
    assert fc is FailureClass.RESOURCE_TIMEOUT


def test_trusted_timeout_class_is_timeout():
    fc = classify_failure(
        termination_class=TerminationClass.TIMEOUT.value,
        exit_code=None, timed_out=False, scope_events=None,
    )
    assert fc is FailureClass.RESOURCE_TIMEOUT


def test_exit_124_alone_is_not_timeout():
    fc = classify_failure(
        termination_class=None, exit_code=124, timed_out=False, scope_events=None,
    )
    assert fc is FailureClass.CODE_OR_PROCESS_FAILURE
    assert fc is not FailureClass.RESOURCE_TIMEOUT


def test_no_automatic_timeout_increase():
    # The recovery policy is structurally incapable of raising a timeout (no
    # timeout field; no escalation).
    pol = RecoveryPolicy()
    assert not hasattr(pol, "timeout_seconds")
    assert not hasattr(pol, "memory_max_bytes")
    assert not hasattr(pol, "resource_class")


def test_timeout_retry_is_bounded_and_never_raises_timeout():
    pol = RecoveryPolicy(max_resource_retries=2)
    assert decide_recovery(FailureClass.RESOURCE_TIMEOUT, attempt_no=0,
                           policy=pol) is RecoveryDecision.RETRY_BOUNDED
    # Budget exhausted -> BLOCK (bounded defer, still no timeout increase).
    assert decide_recovery(FailureClass.RESOURCE_TIMEOUT, attempt_no=2,
                           policy=pol) is RecoveryDecision.BLOCK_RESOURCE


def test_timeout_never_blocks_as_oom():
    assert decide_recovery(FailureClass.RESOURCE_TIMEOUT, attempt_no=0,
                           policy=RecoveryPolicy()) \
        is not RecoveryDecision.BLOCK_RESOURCE
