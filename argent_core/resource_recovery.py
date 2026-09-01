"""Phase C3 — resource failure classification + bounded recovery decision.

C2 (:mod:`argent_core.resource_failure`) produced the *bounded evidence* (a
:class:`~argent_core.resource_failure.TerminationClass` plus ``timed_out`` /
``scope_events`` / ``exit_code``).  C3 turns that evidence into a **job-level
recovery decision** — but WITHOUT introducing a second taxonomy and WITHOUT any
new primary state.

Key properties (ARGENT ARCHITECTURE V1 FINAL §9 / §21 / §22):

* :class:`FailureClass` is the C3 *semantic* job-level class.  It reuses C2's
  ``TerminationClass`` as the raw evidence basis, but a recovery decision needs
  its own closed set of *decision* classes.  The mapping is a pure function.
* :class:`RecoveryDecision` is the bounded, exact set of recovery actions.
* :class:`RecoveryPolicy` is a versioned, frozen policy that only ever
  *bounds* retries — it can NEVER raise a limit, raise a timeout, raise a
  resource class, or escalate a model.  There is no such field; the absence is
  the guarantee.
* ``classify_failure`` is deterministic and testable; its evidence priority is
  Process/Scope identity (validated by the CALLER) → trusted timeout →
  ``memory.events`` → exit code → UNKNOWN.  ``exit 137`` WITHOUT a
  ``memory.events`` OOM delta is **not** OOM; ``exit 124`` WITHOUT the trusted
  timeout marker is **not** a timeout.
* ``decide_recovery`` maps a failure class to a decision; ``RESOURCE_OOM`` is
  NEVER retried identically (BLOCK or PREFER_EXTERNAL), a resource failure
  NEVER authorises code rework, and an ambiguous/unknown termination fails
  closed to LOST quarantine (no duplicate spawn).

Agent output is UNTRUSTED and can never determine a ``FailureClass``,
``RecoveryDecision``, limit, timeout or retry — this module only ever consumes
trusted registry/store evidence produced by the LOCAL enforcement path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from .resource_failure import TerminationClass, classify_termination


class FailureClass(str, Enum):
    """C3 job-level failure classes (decision semantics, not a second taxonomy)."""

    NORMAL_EXIT = "NORMAL_EXIT"
    CODE_OR_PROCESS_FAILURE = "CODE_OR_PROCESS_FAILURE"
    RESOURCE_OOM = "RESOURCE_OOM"
    RESOURCE_MEMORY_LIMIT = "RESOURCE_MEMORY_LIMIT"
    RESOURCE_TIMEOUT = "RESOURCE_TIMEOUT"
    RESOURCE_ENFORCEMENT_FAILURE = "RESOURCE_ENFORCEMENT_FAILURE"
    RESOURCE_CAPACITY_FAILURE = "RESOURCE_CAPACITY_FAILURE"
    SCOPE_CLEANUP_UNVERIFIED = "SCOPE_CLEANUP_UNVERIFIED"
    UNKNOWN_TERMINATION = "UNKNOWN_TERMINATION"


#: Canonical order for any future DB CHECK constraint (deterministic string).
FAILURE_CLASS_VALUES: tuple[str, ...] = tuple(c.value for c in FailureClass)


class RecoveryDecision(str, Enum):
    """Bounded, exact recovery actions (no free strings)."""

    COMPLETE = "COMPLETE"
    RETRY_BOUNDED = "RETRY_BOUNDED"
    DEFER_RESOURCE = "DEFER_RESOURCE"
    BLOCK_RESOURCE = "BLOCK_RESOURCE"
    PREFER_EXTERNAL = "PREFER_EXTERNAL"
    QUARANTINE_LOST = "QUARANTINE_LOST"
    FAIL_NONRESOURCE = "FAIL_NONRESOURCE"


RECOVERY_DECISION_VALUES: tuple[str, ...] = tuple(c.value for c in RecoveryDecision)


class RecoveryReasonCode(str, Enum):
    """Bounded reason codes for resource-failure recovery (persisted audit)."""

    RESOURCE_OOM = "RESOURCE_OOM"
    RESOURCE_MEMORY_LIMIT = "RESOURCE_MEMORY_LIMIT"
    RESOURCE_TIMEOUT = "RESOURCE_TIMEOUT"
    RESOURCE_ENFORCEMENT_UNAVAILABLE = "RESOURCE_ENFORCEMENT_UNAVAILABLE"
    RESOURCE_ENFORCEMENT_UNVERIFIED = "RESOURCE_ENFORCEMENT_UNVERIFIED"
    RESOURCE_CAPACITY_INSUFFICIENT = "RESOURCE_CAPACITY_INSUFFICIENT"
    RESOURCE_PRESSURE = "RESOURCE_PRESSURE"
    SCOPE_CLEANUP_UNVERIFIED = "SCOPE_CLEANUP_UNVERIFIED"
    RESOURCE_EVIDENCE_UNKNOWN = "RESOURCE_EVIDENCE_UNKNOWN"


#: Termination class -> C3 failure class (pure, deterministic).
_TERMINATION_TO_FAILURE: dict[str, str] = {
    TerminationClass.NORMAL_EXIT.value: FailureClass.NORMAL_EXIT.value,
    TerminationClass.NONZERO_EXIT.value: FailureClass.CODE_OR_PROCESS_FAILURE.value,
    TerminationClass.TIMEOUT.value: FailureClass.RESOURCE_TIMEOUT.value,
    TerminationClass.OOM_KILL.value: FailureClass.RESOURCE_OOM.value,
    TerminationClass.MEMORY_LIMIT.value: FailureClass.RESOURCE_MEMORY_LIMIT.value,
    TerminationClass.SCOPE_CREATION_FAILED.value: FailureClass.RESOURCE_ENFORCEMENT_FAILURE.value,
    TerminationClass.SCOPE_VERIFICATION_FAILED.value: FailureClass.RESOURCE_ENFORCEMENT_FAILURE.value,
    TerminationClass.ENFORCEMENT_UNAVAILABLE.value: FailureClass.RESOURCE_ENFORCEMENT_FAILURE.value,
    TerminationClass.SCOPE_CLEANUP_UNVERIFIED.value: FailureClass.SCOPE_CLEANUP_UNVERIFIED.value,
    TerminationClass.UNKNOWN_TERMINATION.value: FailureClass.UNKNOWN_TERMINATION.value,
}

#: C2 enforcement status -> C3 failure class (pre-/post-spawn subset).
_ENFORCEMENT_STATUS_TO_FAILURE: dict[str, str] = {
    "SCOPE_CLEANUP_UNVERIFIED": FailureClass.SCOPE_CLEANUP_UNVERIFIED.value,
    "SCOPE_CREATION_FAILED": FailureClass.RESOURCE_ENFORCEMENT_FAILURE.value,
    "SCOPE_VERIFICATION_FAILED": FailureClass.RESOURCE_ENFORCEMENT_FAILURE.value,
    "ENFORCEMENT_UNAVAILABLE": FailureClass.RESOURCE_ENFORCEMENT_FAILURE.value,
}

#: Admission verdict -> C3 failure class (pre-spawn host-pressure subset).
_ADMISSION_TO_FAILURE: dict[str, str] = {
    "DEFER": FailureClass.RESOURCE_CAPACITY_FAILURE.value,
    "DENY_LOCAL": FailureClass.RESOURCE_CAPACITY_FAILURE.value,
}

#: Failure classes that are resource-related (never CODE_FAILURE, never rework).
_RESOURCE_FAILURE_CLASSES: frozenset[str] = frozenset({
    FailureClass.RESOURCE_OOM.value,
    FailureClass.RESOURCE_MEMORY_LIMIT.value,
    FailureClass.RESOURCE_TIMEOUT.value,
    FailureClass.RESOURCE_ENFORCEMENT_FAILURE.value,
    FailureClass.RESOURCE_CAPACITY_FAILURE.value,
    FailureClass.SCOPE_CLEANUP_UNVERIFIED.value,
    FailureClass.UNKNOWN_TERMINATION.value,
})

#: Failure class -> bounded reason code (persisted as ``last_error_code``).
_FAILURE_REASON_CODES: dict[str, str] = {
    FailureClass.RESOURCE_OOM.value: RecoveryReasonCode.RESOURCE_OOM.value,
    FailureClass.RESOURCE_MEMORY_LIMIT.value: RecoveryReasonCode.RESOURCE_MEMORY_LIMIT.value,
    FailureClass.RESOURCE_TIMEOUT.value: RecoveryReasonCode.RESOURCE_TIMEOUT.value,
    FailureClass.RESOURCE_ENFORCEMENT_FAILURE.value: RecoveryReasonCode.RESOURCE_ENFORCEMENT_UNAVAILABLE.value,
    FailureClass.RESOURCE_CAPACITY_FAILURE.value: RecoveryReasonCode.RESOURCE_CAPACITY_INSUFFICIENT.value,
    FailureClass.SCOPE_CLEANUP_UNVERIFIED.value: RecoveryReasonCode.SCOPE_CLEANUP_UNVERIFIED.value,
    FailureClass.UNKNOWN_TERMINATION.value: RecoveryReasonCode.RESOURCE_EVIDENCE_UNKNOWN.value,
}

#: Closed (FailureClass -> allowed RecoveryDecision) pairing table (C3/F7).
#: ``commit_recovery_decision`` refuses any pair outside this table; a free
#: string or an invalid pairing can never reach the DB.
_ALLOWED_RECOVERY_PAIRS: dict[str, frozenset[str]] = {
    FailureClass.NORMAL_EXIT.value: frozenset({
        RecoveryDecision.COMPLETE.value,
    }),
    FailureClass.CODE_OR_PROCESS_FAILURE.value: frozenset({
        RecoveryDecision.FAIL_NONRESOURCE.value,
    }),
    FailureClass.RESOURCE_OOM.value: frozenset({
        RecoveryDecision.BLOCK_RESOURCE.value,
        RecoveryDecision.PREFER_EXTERNAL.value,
    }),
    FailureClass.RESOURCE_MEMORY_LIMIT.value: frozenset({
        RecoveryDecision.BLOCK_RESOURCE.value,
        RecoveryDecision.DEFER_RESOURCE.value,
    }),
    FailureClass.RESOURCE_TIMEOUT.value: frozenset({
        RecoveryDecision.RETRY_BOUNDED.value,
        RecoveryDecision.DEFER_RESOURCE.value,
        RecoveryDecision.BLOCK_RESOURCE.value,
    }),
    FailureClass.RESOURCE_ENFORCEMENT_FAILURE.value: frozenset({
        RecoveryDecision.DEFER_RESOURCE.value,
        RecoveryDecision.QUARANTINE_LOST.value,
    }),
    FailureClass.RESOURCE_CAPACITY_FAILURE.value: frozenset({
        RecoveryDecision.DEFER_RESOURCE.value,
        RecoveryDecision.BLOCK_RESOURCE.value,
    }),
    FailureClass.SCOPE_CLEANUP_UNVERIFIED.value: frozenset({
        RecoveryDecision.QUARANTINE_LOST.value,
    }),
    FailureClass.UNKNOWN_TERMINATION.value: frozenset({
        RecoveryDecision.QUARANTINE_LOST.value,
    }),
}


@dataclass(frozen=True)
class RecoveryPolicy:
    """Versioned, frozen recovery policy (bounded retry ONLY — never escalation).

    There is deliberately NO field that could raise a limit, raise a timeout,
    raise a resource class, or escalate a model.  ``retryable_failure_classes``
    is a closed set; it can never contain ``RESOURCE_OOM``,
    ``SCOPE_CLEANUP_UNVERIFIED`` or ``UNKNOWN_TERMINATION`` (those are never
    retried identically).
    """

    policy_version: str = "1"
    #: bounded max resource retries (``attempt_no < max`` authorises a retry).
    max_resource_retries: int = 2
    #: bounded max resource DEFERs (C3/F2).  ``DEFER_RESOURCE`` is countable:
    #: it consumes the shared ``attempt_no`` counter (commit bumps ``attempt_no``),
    #: so a bounded number of defers can never loop forever.  Exhausted ->
    #: BLOCK_RESOURCE / QUARANTINE_LOST (decided per failure class).
    max_resource_defers: int = 2
    #: bounded backoff (seconds) for ``RETRY_BOUNDED``.
    retry_backoff_seconds: int = 300
    #: bounded backoff (seconds) for ``DEFER_RESOURCE``.
    defer_backoff_seconds: int = 300
    #: closed set of failure classes eligible for a bounded retry/defer.
    retryable_failure_classes: frozenset = frozenset({
        FailureClass.RESOURCE_CAPACITY_FAILURE.value,
        FailureClass.RESOURCE_TIMEOUT.value,
    })
    #: ``RESOURCE_MEMORY_LIMIT``: default fail-closed BLOCK (no identical retry
    #: and no limit raise).  A defer is only authorised when the caller proves
    #: host reserve returned (an explicit policy decision), never automatically.
    allow_memory_limit_defer: bool = False
    #: ``RESOURCE_ENFORCEMENT_FAILURE``: bounded defer only when the failure is
    #: provably transient (scope inactivity proven).  Unknown/unproven evidence
    #: always fails closed to LOST quarantine.
    allow_enforcement_defer: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.max_resource_retries, int) \
                or self.max_resource_retries < 0:
            raise ValueError("max_resource_retries must be a non-negative int")
        if not isinstance(self.max_resource_defers, int) \
                or self.max_resource_defers < 0:
            raise ValueError("max_resource_defers must be a non-negative int")
        if not isinstance(self.retry_backoff_seconds, int) \
                or self.retry_backoff_seconds <= 0:
            raise ValueError("retry_backoff_seconds must be a positive int")
        if not isinstance(self.defer_backoff_seconds, int) \
                or self.defer_backoff_seconds <= 0:
            raise ValueError("defer_backoff_seconds must be a positive int")
        # Fail-closed: the retryable set can never contain the never-retry
        # classes (a misconfigured policy is rejected at construction).
        forbidden = {
            FailureClass.RESOURCE_OOM.value,
            FailureClass.SCOPE_CLEANUP_UNVERIFIED.value,
            FailureClass.UNKNOWN_TERMINATION.value,
        }
        if forbidden & set(self.retryable_failure_classes):
            raise ValueError(
                "retryable_failure_classes may never contain a never-retry "
                "class (RESOURCE_OOM / SCOPE_CLEANUP_UNVERIFIED / UNKNOWN)"
            )


# -- pure helpers -------------------------------------------------------------


def _as_failure_class(value) -> FailureClass:
    if isinstance(value, FailureClass):
        return value
    return FailureClass(value)  # raises ValueError on unknown (never guessed)


def failure_class_from_termination(
    termination_class,
    *,
    exit_code: Optional[int] = None,
) -> FailureClass:
    """Map a trusted C2 :class:`TerminationClass` to a C3 :class:`FailureClass`.

    ``exit_code`` is accepted for signature compatibility but is NEVER used to
    relabel a trusted termination class (fail-closed): a raw exit code cannot
    turn a ``NONZERO_EXIT`` into an OOM/timeout, and vice versa.  An unknown or
    missing termination class fails closed to ``UNKNOWN_TERMINATION``.
    """
    del exit_code  # accepted for compatibility; never an authority here
    if termination_class is None:
        return FailureClass.UNKNOWN_TERMINATION
    if isinstance(termination_class, TerminationClass):
        key = termination_class.value
    else:
        key = termination_class
    return FailureClass(_TERMINATION_TO_FAILURE.get(
        key, FailureClass.UNKNOWN_TERMINATION.value,
    ))


def failure_class_from_enforcement_status(status: Optional[str]) -> FailureClass:
    """Map a C2 :class:`~argent_core.scope_enforcer.EnforcementStatus` value.

    ``SCOPE_CLEANUP_UNVERIFIED`` is its own C3 class (LOST quarantine);
    every other spawn/enforcement failure maps to
    ``RESOURCE_ENFORCEMENT_FAILURE``.  Unknown values fail closed to
    ``UNKNOWN_TERMINATION``.
    """
    if status is None:
        return FailureClass.UNKNOWN_TERMINATION
    return FailureClass(_ENFORCEMENT_STATUS_TO_FAILURE.get(
        status, FailureClass.UNKNOWN_TERMINATION.value,
    ))


def failure_class_from_admission(admission_verdict: Optional[str]) -> FailureClass:
    """Map a pre-spawn admission verdict (DEFER/DENY_LOCAL) to a failure class.

    Host-pressure deferrals/denials are ``RESOURCE_CAPACITY_FAILURE`` (a
    resource event, never a code failure, never a spawn).  ``ALLOW`` /
    ``PREFER_EXTERNAL`` are not failures and return ``NORMAL_EXIT``.
    """
    if admission_verdict is None:
        return FailureClass.NORMAL_EXIT
    return FailureClass(_ADMISSION_TO_FAILURE.get(
        admission_verdict, FailureClass.NORMAL_EXIT.value,
    ))


def classify_failure(
    termination_class=None,
    *,
    exit_code: Optional[int] = None,
    timed_out: bool = False,
    scope_events: Optional[dict] = None,
    policy: Optional[RecoveryPolicy] = None,
) -> FailureClass:
    """Classify bounded post-termination evidence to a C3 :class:`FailureClass`.

    Evidence priority (first match wins; the Process/Scope identity is validated
    by the CALLER before this is invoked): trusted ``termination_class`` →
    trusted ``timed_out`` → ``scope_events`` ``memory.events`` → ``exit_code`` →
    UNKNOWN.  When ``termination_class`` is absent it is derived from the raw
    evidence via C2 :func:`classify_termination`.

    ``exit 137`` WITHOUT a ``memory.events`` OOM delta is NOT OOM; ``exit 124``
    WITHOUT the trusted timeout marker is NOT a timeout (both fall through to
    ``NONZERO_EXIT`` → ``CODE_OR_PROCESS_FAILURE`` via the C2 helper).

    ``policy`` is accepted for signature stability but does not change the
    classification (classification is evidence-only; policy bounds recovery).
    """
    del policy  # classification is evidence-only; policy bounds recovery only
    if termination_class is None:
        tc = classify_termination(
            exit_code=exit_code, scope_events=scope_events, timed_out=timed_out,
        )
    elif isinstance(termination_class, TerminationClass):
        tc = termination_class
    else:
        try:
            tc = TerminationClass(termination_class)
        except ValueError:
            tc = TerminationClass.UNKNOWN_TERMINATION
    return failure_class_from_termination(tc)


def is_resource_failure(failure_class) -> bool:
    """True for every resource-related failure class (never a code failure).

    ``NORMAL_EXIT`` and ``CODE_OR_PROCESS_FAILURE`` are NOT resource failures
    (they never authorise a resource retry, defer, block, or rework).
    """
    fc = _as_failure_class(failure_class)
    return fc.value in _RESOURCE_FAILURE_CLASSES


def reason_code_for_failure(failure_class) -> str:
    """Bounded reason code for a failure class (persisted as ``last_error_code``)."""
    fc = _as_failure_class(failure_class)
    return _FAILURE_REASON_CODES.get(fc.value, RecoveryReasonCode.RESOURCE_EVIDENCE_UNKNOWN.value)


def is_valid_recovery_pair(failure_class, recovery_decision) -> bool:
    """True when ``recovery_decision`` is an allowed pairing for ``failure_class``.

    The pairing table is CLOSED (C3/F7): any pair outside it is refused, so no
    free string and no invalid (failure -> decision) combination ever reaches
    the DB.  Unknown failure classes / decisions fail closed to False.
    """
    try:
        fc = _as_failure_class(failure_class)
    except (ValueError, TypeError):
        return False
    if isinstance(recovery_decision, RecoveryDecision):
        dec = recovery_decision.value
    else:
        try:
            dec = RecoveryDecision(recovery_decision).value
        except (ValueError, TypeError):
            return False
    allowed = _ALLOWED_RECOVERY_PAIRS.get(fc.value)
    return allowed is not None and dec in allowed


def assert_valid_recovery_pair(failure_class, recovery_decision) -> None:
    """Raise ``ValueError`` when the (failure -> decision) pairing is invalid."""
    fc = _as_failure_class(failure_class)
    dec = recovery_decision if isinstance(recovery_decision, RecoveryDecision) \
        else RecoveryDecision(recovery_decision)
    if not is_valid_recovery_pair(fc, dec):
        raise ValueError(
            f"invalid recovery pairing {fc.value!r} -> {dec.value!r}"
        )


def normalized_reason_code(failure_class, reason_code: Optional[str]) -> str:
    """Return a bounded reason code (derived when ``reason_code`` is None).

    A caller-supplied ``reason_code`` must be a :class:`RecoveryReasonCode`
    value (closed enum); anything else raises ``ValueError`` — a free string
    can never be persisted.  ``None`` derives the canonical code from the
    failure class.
    """
    if reason_code is None:
        return reason_code_for_failure(failure_class)
    try:
        return RecoveryReasonCode(reason_code).value
    except (ValueError, TypeError):
        raise ValueError(f"unknown recovery reason code {reason_code!r}")


def decide_recovery(
    failure_class,
    *,
    attempt_no: int,
    policy: Optional[RecoveryPolicy] = None,
    has_evidence_unknown: bool = False,
    prefer_external_available: bool = False,
) -> RecoveryDecision:
    """Map a failure class + attempt count to a bounded recovery decision.

    Rules (ARGENT ARCHITECTURE V1 FINAL §9 / §21):

    * ``NORMAL_EXIT`` -> ``COMPLETE`` (no resource action);
    * ``CODE_OR_PROCESS_FAILURE`` -> ``FAIL_NONRESOURCE`` (existing code path,
      never a resource retry, never a rework authorisation from here);
    * ``RESOURCE_OOM`` -> ``BLOCK_RESOURCE`` (or ``PREFER_EXTERNAL`` when
      allowed) — NEVER an identical retry, never a limit raise;
    * ``RESOURCE_MEMORY_LIMIT`` -> ``BLOCK_RESOURCE`` by default (fail-closed),
      ``DEFER_RESOURCE`` only when the policy explicitly allows it AND the
      bounded defer budget remains;
    * ``RESOURCE_TIMEOUT`` -> ``RETRY_BOUNDED`` only when the attempt budget
      remains AND the policy allows it, else ``DEFER_RESOURCE`` (bounded) only
      while the defer budget remains, else ``BLOCK_RESOURCE`` (never a longer
      timeout, never an unbounded defer);
    * ``RESOURCE_ENFORCEMENT_FAILURE`` -> ``DEFER_RESOURCE`` (bounded) when the
      failure is provably transient and the defer budget remains, else
      ``QUARANTINE_LOST`` (no provable path, no unbounded retry, no legacy
      fallback);
    * ``RESOURCE_CAPACITY_FAILURE`` -> ``DEFER_RESOURCE`` (bounded); once the
      defer budget is exhausted -> ``BLOCK_RESOURCE`` (no unbounded pressure
      loop);
    * ``SCOPE_CLEANUP_UNVERIFIED`` / ``UNKNOWN_TERMINATION`` ->
      ``QUARANTINE_LOST`` (fail-closed, no duplicate spawn).
    """
    fc = _as_failure_class(failure_class)
    pol = policy or RecoveryPolicy()
    if fc is FailureClass.NORMAL_EXIT:
        return RecoveryDecision.COMPLETE
    if fc is FailureClass.CODE_OR_PROCESS_FAILURE:
        return RecoveryDecision.FAIL_NONRESOURCE
    if fc is FailureClass.RESOURCE_OOM:
        if prefer_external_available:
            return RecoveryDecision.PREFER_EXTERNAL
        return RecoveryDecision.BLOCK_RESOURCE
    if fc is FailureClass.RESOURCE_MEMORY_LIMIT:
        if pol.allow_memory_limit_defer and attempt_no < pol.max_resource_defers:
            return RecoveryDecision.DEFER_RESOURCE
        return RecoveryDecision.BLOCK_RESOURCE
    if fc is FailureClass.RESOURCE_TIMEOUT:
        if attempt_no < pol.max_resource_retries and \
                FailureClass.RESOURCE_TIMEOUT.value in pol.retryable_failure_classes:
            return RecoveryDecision.RETRY_BOUNDED
        if attempt_no < pol.max_resource_defers:
            return RecoveryDecision.DEFER_RESOURCE
        return RecoveryDecision.BLOCK_RESOURCE
    if fc is FailureClass.RESOURCE_ENFORCEMENT_FAILURE:
        if has_evidence_unknown:
            return RecoveryDecision.QUARANTINE_LOST
        if pol.allow_enforcement_defer and attempt_no < pol.max_resource_defers:
            return RecoveryDecision.DEFER_RESOURCE
        return RecoveryDecision.QUARANTINE_LOST
    if fc is FailureClass.RESOURCE_CAPACITY_FAILURE:
        if attempt_no < pol.max_resource_defers:
            return RecoveryDecision.DEFER_RESOURCE
        return RecoveryDecision.BLOCK_RESOURCE
    if fc is FailureClass.SCOPE_CLEANUP_UNVERIFIED:
        return RecoveryDecision.QUARANTINE_LOST
    if fc is FailureClass.UNKNOWN_TERMINATION:
        return RecoveryDecision.QUARANTINE_LOST
    return RecoveryDecision.QUARANTINE_LOST  # fail-closed default


def next_eligible_at_after(now_iso: Optional[str], seconds: int) -> Optional[str]:
    """Return ``now_iso + seconds`` as an ISO string (bounded horizon).

    ``seconds <= 0`` returns ``now_iso`` unchanged; an unparseable timestamp
    returns ``None`` (no horizon) so the caller stays fail-closed.
    """
    if not isinstance(seconds, int) or seconds <= 0:
        return now_iso
    try:
        dt = datetime.fromisoformat(now_iso) if now_iso else datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()
