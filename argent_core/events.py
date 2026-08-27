"""Event system (SPEC V1 chapter 6).

All 19 mandatory event types are declared here.  Events are privacy-filtered
fail-closed: any deny-listed substring in an envelope field or payload
raises :class:`PrivacyViolation` and the event is never written.
"""

from __future__ import annotations

from typing import Any, Optional

from .models import PrivacyViolation

# The 19 mandatory event types from SPEC V1 chapter 6.
V1_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "task.created",
        "task.state_changed",
        "role.started",
        "role.completed",
        "role.failed",
        "handoff.created",
        "finding.created",
        "finding.resolved",
        "test.started",
        "test.completed",
        "review.started",
        "review.completed",
        "lead.decision",
        "gate.owner_required",
        "gate.owner_approved",
        "gate.owner_rejected",
        "system.recovery_started",
        "system.recovery_completed",
        "task.completed",
    }
)

# The 12 new event types from SPEC V2 chapter 10.
AGENT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "agent.dispatch_created",
        "agent.started",
        "agent.result_received",
        "agent.result_accepted",
        "agent.result_rejected",
        "agent.result_duplicate",
        "agent.completed",
        "agent.failed",
        "agent.recovery_pending",
        "handoff.expected",
        "handoff.accepted",
        "policy.role_violation",
    }
)

# Union of V1 + V2 (31 total).
EVENT_TYPES: frozenset[str] = V1_EVENT_TYPES | AGENT_EVENT_TYPES

# Fail-closed privacy deny list.  Matching is case-insensitive substring
# matching against both payload keys and string payload values.
PRIVACY_DENYLIST: tuple[str, ...] = (
    "prompt",
    "chain_of_thought",
    "cot",
    "reasoning",
    "secret",
    "password",
    "api_key",
    "token",
    "credential",
    "mail_content",
    "mail_address",
    "email_address",
    "source_code",
    "code",
    "diff",
    "body",
    "subject",
    "content",
    "recipient",
)

_LOWER_DENYLIST: tuple[str, ...] = tuple(w.lower() for w in PRIVACY_DENYLIST)


def _contains_denylisted(text: str) -> Optional[str]:
    low = text.lower()
    for word in _LOWER_DENYLIST:
        if word in low:
            return word
    return None


def _scan_value(value: Any) -> Optional[str]:
    """Return the first deny-listed word found in ``value`` or ``None``."""
    if isinstance(value, str):
        return _contains_denylisted(value)
    if isinstance(value, dict):
        return _scan_payload(value)
    if isinstance(value, (list, tuple)):
        for item in value:
            hit = _scan_value(item)
            if hit is not None:
                return hit
        return None
    # ints, floats, bools, None are always safe.
    return None


def _scan_payload(payload: dict) -> Optional[str]:
    for key, value in payload.items():
        if isinstance(key, str):
            hit = _contains_denylisted(key)
            if hit is not None:
                return hit
        hit = _scan_value(value)
        if hit is not None:
            return hit
    return None


def check_privacy(payload: dict) -> None:
    """Raise :class:`PrivacyViolation` if the payload violates the deny list."""
    hit = _scan_payload(payload)
    if hit is not None:
        raise PrivacyViolation(
            f"event payload contains deny-listed term {hit!r}"
        )


def scan_value_for_denylist(value: Any) -> Optional[str]:
    """Return the first deny-listed word in ``value`` or ``None``.

    Public wrapper around the internal scanner, used by ``outputs`` to apply
    the same fail-closed deny-list to structured agent outputs (SPEC V2 5).
    """
    return _scan_value(value)


def check_event(ev) -> None:
    """Fail-closed privacy scan over the whole event (SPEC V1.2 12.5).

    Scans every string field of the envelope — ``type``, ``task_id``, ``role``
    and ``state`` — in addition to the payload (keys and values, recursively).
    A deny-listed substring in any of them raises :class:`PrivacyViolation` and
    nothing is written.
    """
    for field in (ev.type, ev.task_id, ev.role, ev.state):
        if isinstance(field, str):
            hit = _contains_denylisted(field)
            if hit is not None:
                raise PrivacyViolation(
                    f"event field contains deny-listed term {hit!r}"
                )
    check_privacy(ev.payload)
