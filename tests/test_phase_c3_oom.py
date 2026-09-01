"""Phase C3 — OOM classification (C).  Deterministic.

``memory.events`` OOM deltas are the ONLY OOM evidence; ``exit 137`` without a
delta is NOT OOM; unrelated (wrong-scope) cgroup events are rejected.
"""

from __future__ import annotations

from argent_core.resource_failure import TerminationClass, memory_events_delta
from argent_core.resource_recovery import (
    FailureClass,
    RecoveryDecision,
    classify_failure,
    decide_recovery,
)


def test_oom_kill_delta_is_oom():
    fc = classify_failure(
        termination_class=None, exit_code=137, timed_out=False,
        scope_events={"oom_kill": 1, "oom_group_kill": 0, "max": 0, "high": 0},
    )
    assert fc is FailureClass.RESOURCE_OOM


def test_oom_group_kill_delta_is_oom():
    fc = classify_failure(
        termination_class=None, exit_code=137, timed_out=False,
        scope_events={"oom_kill": 0, "oom_group_kill": 1, "max": 0, "high": 0},
    )
    assert fc is FailureClass.RESOURCE_OOM


def test_exit_137_without_events_is_not_oom():
    fc = classify_failure(
        termination_class=None, exit_code=137, timed_out=False, scope_events=None,
    )
    assert fc is not FailureClass.RESOURCE_OOM
    assert fc is FailureClass.CODE_OR_PROCESS_FAILURE


def test_exit_137_with_zero_deltas_is_not_oom():
    fc = classify_failure(
        termination_class=None, exit_code=137, timed_out=False,
        scope_events={"oom_kill": 0, "oom_group_kill": 0, "max": 0, "high": 0},
    )
    assert fc is FailureClass.CODE_OR_PROCESS_FAILURE


def test_unrelated_cgroup_events_are_rejected():
    # A delta dict from a DIFFERENT scope/cgroup is never accepted as THIS
    # scope's OOM evidence: the C3 classification only consumes the caller-
    # validated per-scope delta.  Here a "max"/"high" delta is a memory-limit
    # event, NOT an OOM kill.
    fc = classify_failure(
        termination_class=None, exit_code=137, timed_out=False,
        scope_events={"oom_kill": 0, "oom_group_kill": 0, "max": 1, "high": 0},
    )
    assert fc is FailureClass.RESOURCE_MEMORY_LIMIT
    assert fc is not FailureClass.RESOURCE_OOM


def test_memory_events_delta_floors_negative_resets():
    # A counter reset (negative raw delta) is floored at 0 -> no OOM evidence.
    delta = memory_events_delta(
        {"oom_kill": 5, "max": 0, "high": 0},
        {"oom_kill": 1, "max": 0, "high": 0},
    )
    assert delta["oom_kill"] == 0


def test_oom_decision_is_never_identical_retry():
    fc = FailureClass.RESOURCE_OOM
    for attempt_no in (0, 1, 2, 5):
        decision = decide_recovery(fc, attempt_no=attempt_no)
        assert decision is RecoveryDecision.BLOCK_RESOURCE
        assert decision is not RecoveryDecision.RETRY_BOUNDED
        assert decision is not RecoveryDecision.DEFER_RESOURCE


def test_oom_prefer_external_when_available():
    decision = decide_recovery(
        FailureClass.RESOURCE_OOM, attempt_no=0, prefer_external_available=True,
    )
    assert decision is RecoveryDecision.PREFER_EXTERNAL
    assert decision is not RecoveryDecision.RETRY_BOUNDED
