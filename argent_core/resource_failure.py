"""Phase C2 — bounded termination classification (evidence -> class).

This module provides the deterministic, testable mapping from bounded
post-termination evidence (exit code, cgroup ``memory.events`` deltas, timeout
flag, spawn/enforcement status) to a single :class:`TerminationClass`.

C2 does **not** perform the final job-level classification (that is C3's
Retry-/Routing-Policy).  C2 only produces the *bounded evidence* plus a pure
``classify_termination`` helper that C3 can call.  The classification is a pure
function of its inputs — no host reads, no shell, no secrets.

Classification semantics (ARGENT ARCHITECTURE V1 FINAL §9 / §21):

* A spawn/enforcement failure (scope could not be created or verified, or
  enforcement was unavailable) is a ``RESOURCE`` failure — never a code
  failure, never a rework signal.
* ``oom_kill`` delta > 0 -> ``OOM_KILL`` (the kernel killed the process).
* ``max`` / ``high`` deltas > 0 -> ``MEMORY_LIMIT`` (the cgroup memory ceiling /
  soft limit was reached or throttled).
* a wall-clock timeout -> ``TIMEOUT``.
* ``NONZERO_EXIT`` alone is **not** a resource failure (a normal non-zero exit
  is a deterministic/transient signal for C3 to classify); only OOM/memory
  evidence turns a termination into a resource failure.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class TerminationClass(str, Enum):
    """Bounded, closed set of termination classes (persisted in the registry)."""

    NORMAL_EXIT = "NORMAL_EXIT"
    NONZERO_EXIT = "NONZERO_EXIT"
    TIMEOUT = "TIMEOUT"
    OOM_KILL = "OOM_KILL"
    MEMORY_LIMIT = "MEMORY_LIMIT"
    SCOPE_CREATION_FAILED = "SCOPE_CREATION_FAILED"
    SCOPE_VERIFICATION_FAILED = "SCOPE_VERIFICATION_FAILED"
    ENFORCEMENT_UNAVAILABLE = "ENFORCEMENT_UNAVAILABLE"
    UNKNOWN_TERMINATION = "UNKNOWN_TERMINATION"


#: Canonical order for the DB CHECK constraint (deterministic DDL string).
TERMINATION_CLASS_VALUES: tuple[str, ...] = tuple(c.value for c in TerminationClass)

#: Spawn/enforcement failure classes that are always RESOURCE failures.
_ENFORCEMENT_FAILURES: frozenset[str] = frozenset({
    TerminationClass.SCOPE_CREATION_FAILED.value,
    TerminationClass.SCOPE_VERIFICATION_FAILED.value,
    TerminationClass.ENFORCEMENT_UNAVAILABLE.value,
})


def _delta(scope_events: Optional[dict], key: str) -> int:
    """Bounded, non-negative delta for a ``memory.events`` counter key.

    Unreadable / missing / non-int values are treated as 0 (no evidence), never
    guessed.  A negative value (should not happen) is also treated as 0 so the
    classification stays monotonic and fail-closed toward "no OOM/memory event".
    """
    if not isinstance(scope_events, dict):
        return 0
    raw = scope_events.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return raw if raw > 0 else 0


def memory_events_delta(
    baseline: Optional[dict], current: Optional[dict],
) -> dict:
    """Bounded, non-negative per-key delta between two ``memory.events`` reads.

    Returns exactly the four keys ``oom_kill`` / ``oom_group_kill`` / ``max`` /
    ``high`` (missing/unreadable/non-int counters treated as 0).  A negative
    raw delta (counter reset) is floored at 0 so the classification stays
    monotonic and fail-closed toward "no event".
    """
    b = baseline if isinstance(baseline, dict) else {}
    c = current if isinstance(current, dict) else {}
    out: dict = {}
    for key in ("oom_kill", "oom_group_kill", "max", "high"):
        bv = b.get(key) if isinstance(b.get(key), int) and not isinstance(b.get(key), bool) else 0
        cv = c.get(key) if isinstance(c.get(key), int) and not isinstance(c.get(key), bool) else 0
        out[key] = max(0, cv - bv)
    return out


def classify_termination(
    exit_code: Optional[int] = None,
    *,
    scope_events: Optional[dict] = None,
    timed_out: bool = False,
    enforcement_status: Optional[str] = None,
) -> TerminationClass:
    """Map bounded post-termination evidence to a single termination class.

    Deterministic precedence (first match wins):

    1. enforcement status -> the matching failure class;
    2. ``timed_out`` -> ``TIMEOUT``;
    3. ``scope_events.oom_kill`` / ``oom_group_kill`` delta > 0 -> ``OOM_KILL``;
    4. ``scope_events.max`` / ``scope_events.high`` delta > 0 -> ``MEMORY_LIMIT``;
    5. ``exit_code == 0`` -> ``NORMAL_EXIT``;
    6. ``exit_code`` non-zero -> ``NONZERO_EXIT``;
    7. otherwise -> ``UNKNOWN_TERMINATION``.

    ``NONZERO_EXIT`` alone is deliberately NOT a resource failure.
    """
    if enforcement_status in _ENFORCEMENT_FAILURES:
        return TerminationClass(enforcement_status)
    if enforcement_status == TerminationClass.TIMEOUT.value:
        return TerminationClass.TIMEOUT
    if timed_out:
        return TerminationClass.TIMEOUT
    if _delta(scope_events, "oom_kill") > 0 or _delta(scope_events, "oom_group_kill") > 0:
        return TerminationClass.OOM_KILL
    if _delta(scope_events, "max") > 0 or _delta(scope_events, "high") > 0:
        return TerminationClass.MEMORY_LIMIT
    if exit_code == 0:
        return TerminationClass.NORMAL_EXIT
    if exit_code is not None and exit_code != 0:
        return TerminationClass.NONZERO_EXIT
    return TerminationClass.UNKNOWN_TERMINATION
