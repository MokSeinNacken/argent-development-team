"""Phase C3 — failure classification (A).  Deterministic, pure-function tests.

Every :class:`FailureClass` value and the normal/nonzero/OOM/memory-limit/
timeout/enforcement/cleanup-unknown/unknown mappings are proven, including the
fail-closed rules (exit 137 without events is NOT OOM; exit 124 without a
trusted timeout marker is NOT a timeout).
"""

from __future__ import annotations

import pytest

from argent_core.resource_failure import TerminationClass
from argent_core.resource_recovery import (
    FailureClass,
    classify_failure,
    failure_class_from_enforcement_status,
    failure_class_from_termination,
    is_resource_failure,
)
from argent_core.scope_enforcer import EnforcementStatus


def test_all_failure_class_values_are_distinct_and_closed():
    values = [c.value for c in FailureClass]
    assert len(values) == len(set(values))
    assert "RESOURCE_OOM" in values
    assert "CODE_OR_PROCESS_FAILURE" in values
    assert "UNKNOWN_TERMINATION" in values


def test_normal_exit():
    assert failure_class_from_termination(TerminationClass.NORMAL_EXIT) \
        is FailureClass.NORMAL_EXIT
    assert not is_resource_failure(FailureClass.NORMAL_EXIT)


def test_nonzero_exit():
    assert failure_class_from_termination(TerminationClass.NONZERO_EXIT) \
        is FailureClass.CODE_OR_PROCESS_FAILURE
    assert not is_resource_failure(FailureClass.CODE_OR_PROCESS_FAILURE)


def test_oom():
    assert failure_class_from_termination(TerminationClass.OOM_KILL) \
        is FailureClass.RESOURCE_OOM
    assert is_resource_failure(FailureClass.RESOURCE_OOM)


def test_memory_limit():
    assert failure_class_from_termination(TerminationClass.MEMORY_LIMIT) \
        is FailureClass.RESOURCE_MEMORY_LIMIT
    assert is_resource_failure(FailureClass.RESOURCE_MEMORY_LIMIT)


def test_timeout():
    assert failure_class_from_termination(TerminationClass.TIMEOUT) \
        is FailureClass.RESOURCE_TIMEOUT
    assert is_resource_failure(FailureClass.RESOURCE_TIMEOUT)


def test_enforcement_failures_all_map_to_enforcement_failure():
    for tc in (TerminationClass.SCOPE_CREATION_FAILED,
               TerminationClass.SCOPE_VERIFICATION_FAILED,
               TerminationClass.ENFORCEMENT_UNAVAILABLE):
        assert failure_class_from_termination(tc) \
            is FailureClass.RESOURCE_ENFORCEMENT_FAILURE
    assert is_resource_failure(FailureClass.RESOURCE_ENFORCEMENT_FAILURE)


def test_cleanup_unverified():
    fc = failure_class_from_enforcement_status(
        EnforcementStatus.SCOPE_CLEANUP_UNVERIFIED.value,
    )
    assert fc is FailureClass.SCOPE_CLEANUP_UNVERIFIED
    assert is_resource_failure(fc)


def test_unknown_termination():
    assert failure_class_from_termination(TerminationClass.UNKNOWN_TERMINATION) \
        is FailureClass.UNKNOWN_TERMINATION
    assert is_resource_failure(FailureClass.UNKNOWN_TERMINATION)


def test_missing_termination_class_is_unknown():
    assert failure_class_from_termination(None) is FailureClass.UNKNOWN_TERMINATION


def test_unknown_termination_class_string_fails_closed():
    assert failure_class_from_termination("NOT_A_CLASS") \
        is FailureClass.UNKNOWN_TERMINATION


def test_exit_137_without_oom_events_is_not_oom():
    fc = classify_failure(
        termination_class=None, exit_code=137, timed_out=False, scope_events=None,
    )
    assert fc is FailureClass.CODE_OR_PROCESS_FAILURE  # NONZERO_EXIT, not OOM


def test_exit_124_without_timeout_marker_is_not_timeout():
    fc = classify_failure(
        termination_class=None, exit_code=124, timed_out=False, scope_events=None,
    )
    assert fc is FailureClass.CODE_OR_PROCESS_FAILURE  # NONZERO_EXIT, not TIMEOUT


def test_trusted_timeout_marker_wins_over_exit_code():
    fc = classify_failure(
        termination_class=None, exit_code=124, timed_out=True, scope_events=None,
    )
    assert fc is FailureClass.RESOURCE_TIMEOUT


def test_oom_events_win_over_exit_code():
    fc = classify_failure(
        termination_class=None, exit_code=137, timed_out=False,
        scope_events={"oom_kill": 1, "oom_group_kill": 0, "max": 0, "high": 0},
    )
    assert fc is FailureClass.RESOURCE_OOM


def test_classify_failure_rejects_unknown_termination_class_value():
    # An unknown persisted termination_class fails closed to UNKNOWN (never
    # guessed, never a code failure).
    fc = classify_failure(termination_class="BOGUS", exit_code=0)
    assert fc is FailureClass.UNKNOWN_TERMINATION
    assert is_resource_failure(fc)
