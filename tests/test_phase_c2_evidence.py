"""Phase C2 — resource-limit evidence classification (deterministic).

Proves :func:`classify_termination` maps bounded ``memory.events`` deltas +
exit code + timeout + enforcement status to the correct :class:`TerminationClass`.
OOM/memory evidence IS a resource failure; ``NONZERO_EXIT`` alone is NOT.
"""

from __future__ import annotations

from argent_core.resource_failure import (
    TerminationClass,
    classify_termination,
)
from argent_core.scope_enforcer import EnforcementStatus


def test_oom_kill_delta_classifies_as_oom_kill():
    assert classify_termination(
        exit_code=137, scope_events={"oom_kill": 1, "max": 0, "high": 0},
    ) == TerminationClass.OOM_KILL


def test_memory_high_events_classify_as_memory_limit():
    assert classify_termination(
        exit_code=0, scope_events={"oom_kill": 0, "max": 0, "high": 3},
    ) == TerminationClass.MEMORY_LIMIT


def test_memory_max_breach_classifies_as_memory_limit():
    assert classify_termination(
        exit_code=0, scope_events={"oom_kill": 0, "max": 1, "high": 0},
    ) == TerminationClass.MEMORY_LIMIT


def test_oom_kill_wins_over_memory_limit():
    assert classify_termination(
        exit_code=137, scope_events={"oom_kill": 1, "max": 1, "high": 1},
    ) == TerminationClass.OOM_KILL


def test_scope_creation_failure_is_enforcement_failure():
    assert classify_termination(
        enforcement_status=EnforcementStatus.SCOPE_CREATION_FAILED.value,
    ) == TerminationClass.SCOPE_CREATION_FAILED


def test_scope_verification_failure_is_enforcement_failure():
    assert classify_termination(
        enforcement_status=EnforcementStatus.SCOPE_VERIFICATION_FAILED.value,
    ) == TerminationClass.SCOPE_VERIFICATION_FAILED


def test_enforcement_unavailable_is_enforcement_failure():
    assert classify_termination(
        enforcement_status=EnforcementStatus.ENFORCEMENT_UNAVAILABLE.value,
    ) == TerminationClass.ENFORCEMENT_UNAVAILABLE


def test_normal_exit():
    assert classify_termination(exit_code=0) == TerminationClass.NORMAL_EXIT


def test_nonzero_exit_is_not_a_resource_failure():
    # A plain non-zero exit is NOT OOM/memory evidence.
    assert classify_termination(exit_code=1) == TerminationClass.NONZERO_EXIT
    assert classify_termination(exit_code=1) != TerminationClass.OOM_KILL


def test_unknown_termination_without_evidence():
    assert classify_termination() == TerminationClass.UNKNOWN_TERMINATION


def test_unreadable_scope_events_treated_as_no_evidence():
    # Missing / non-int / negative deltas are never guessed as an OOM.
    assert classify_termination(exit_code=1, scope_events={"oom_kill": "x"}) \
        == TerminationClass.NONZERO_EXIT
    assert classify_termination(exit_code=1, scope_events={"oom_kill": -1}) \
        == TerminationClass.NONZERO_EXIT
    assert classify_termination(exit_code=1, scope_events=None) \
        == TerminationClass.NONZERO_EXIT
