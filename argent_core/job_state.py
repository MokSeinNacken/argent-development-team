"""Phase B1 — durable queue / lease operational state model.

This module is the single, central authority for the **operational projection**
of a supervisor job (ARGENT ARCHITECTURE V1 FINAL §3, §5, §22).

``primary_state`` (persisted on ``supervisor_jobs.primary_state``) is the one
authoritative operational quantity.  It is an **8-state projection** of the
existing V2C fields (``status``, ``terminal``, ``recovery_state``,
``wait_kind``, ``queue_reason``); there is exactly ONE authoritative value and
the V2C ``status`` column is kept in sync as a backwards-compatible projection
so existing readers/tests keep working.  No second business state machine is
introduced: the ``TaskState`` workflow is untouched.

The 8 primary states (exact set, per §3):

    QUEUED, RUNNING, WAITING_EXTERNAL, OWNER_GATE, BLOCKED, FAILED, LOST, DONE
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class PrimaryState(str, Enum):
    """The authoritative 8-state operational projection (§3)."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    OWNER_GATE = "OWNER_GATE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    LOST = "LOST"
    DONE = "DONE"


#: Canonical order for the CHECK constraint (deterministic DDL string).
PRIMARY_STATE_VALUES: tuple[str, ...] = tuple(s.value for s in PrimaryState)


class QueueReason(str, Enum):
    """Why a job is (re-)queued (§5).  Not a state.  Default is ``NEW``."""

    NEW = "NEW"
    RETRY_BACKOFF = "RETRY_BACKOFF"
    WAIT_EVENT = "WAIT_EVENT"
    WAIT_DEADLINE = "WAIT_DEADLINE"
    GATE_APPROVED = "GATE_APPROVED"
    RECOVERY = "RECOVERY"
    # C1: resource-governor deferrals/denials (not new primary states — the
    # job stays QUEUED; these are just queue_reason values).
    RESOURCE_DEFERRED = "RESOURCE_DEFERRED"
    RESOURCE_DENIED = "RESOURCE_DENIED"


class ErrorClass(str, Enum):
    """Failure taxonomy for retry metadata (§5, §9).  Default ``NONE``."""

    NONE = "NONE"
    TRANSIENT = "TRANSIENT"
    DETERMINISTIC = "DETERMINISTIC"
    RESOURCE = "RESOURCE"
    EXTERNAL = "EXTERNAL"
    SECURITY = "SECURITY"
    OWNER_REQUIRED = "OWNER_REQUIRED"
    # D1 (Phase D): Context-Pack build failures are ORCHESTRATION errors
    # (distinct from DETERMINISTIC code failures and RESOURCE failures).
    CONTEXT = "CONTEXT"
    # E1 (Phase E): provider/model registry validation failures are PROVIDER
    # errors (distinct from CODE/RESOURCE/CONTEXT).  Laufzeit-Providerausfälle
    # (Netz) bleiben job-seitig EXTERNAL/TRANSIENT (B/C); die Registry-Seite ist
    # statisch.
    PROVIDER = "PROVIDER"


class WaitKind(str, Enum):
    """External-wait kind for ``WAITING_EXTERNAL`` (§5).  Default ``NONE``."""

    NONE = "NONE"
    CI = "CI"
    UPSTREAM = "UPSTREAM"
    RATE_LIMIT = "RATE_LIMIT"
    NETWORK = "NETWORK"
    TIMER = "TIMER"


# ---------------------------------------------------------------------------
# status <-> primary_state mapping (central, single source of truth)
# ---------------------------------------------------------------------------

# V2C status -> primary state (the terminal column overrides TERMINAL).
_STATUS_TO_PRIMARY: dict[str, PrimaryState] = {
    "ACTIVE": PrimaryState.RUNNING,
    "WAITING_RUN": PrimaryState.QUEUED,  # refined to WAITING_EXTERNAL if wait_kind set
    "WAITING_GATE": PrimaryState.OWNER_GATE,
    "BACKOFF": PrimaryState.QUEUED,      # queue_reason=RETRY_BACKOFF
    "RECOVERING": PrimaryState.LOST,     # recovery quarantine (§22)
    "ERROR": PrimaryState.BLOCKED,       # fail-closed; classification resolves BLOCKED/FAILED
    "TERMINAL": None,                    # resolved by the terminal column
}

# primary state -> V2C status projection (used when the queue/lease layer
# drives a transition and must keep the V2C column consistent).
_PRIMARY_TO_STATUS: dict[PrimaryState, str] = {
    PrimaryState.QUEUED: "WAITING_RUN",
    PrimaryState.RUNNING: "ACTIVE",
    PrimaryState.WAITING_EXTERNAL: "WAITING_RUN",
    PrimaryState.OWNER_GATE: "WAITING_GATE",
    PrimaryState.BLOCKED: "TERMINAL",
    PrimaryState.FAILED: "TERMINAL",
    PrimaryState.DONE: "TERMINAL",
    PrimaryState.LOST: "RECOVERING",
}

_TERMINAL_TO_PRIMARY: dict[str, PrimaryState] = {
    "DONE": PrimaryState.DONE,
    "FAILED": PrimaryState.FAILED,
    "BLOCKED": PrimaryState.BLOCKED,
}


def derive_primary_state(
    status: str,
    *,
    terminal: Optional[str] = None,
    recovery_state: Optional[str] = None,
    wait_kind: Optional[str] = None,
    queue_reason: Optional[str] = None,
) -> PrimaryState:
    """Derive the authoritative ``primary_state`` from the V2C projection.

    Order of authority (matches §22):

    1. ``terminal`` is the sticky-terminal authority (DONE/FAILED/BLOCKED).
    2. ``status`` maps through the V2C table; ``WAITING_RUN`` refines to
       ``WAITING_EXTERNAL`` when ``wait_kind`` is a real (non-NONE) wait.
    3. Anything unrecognised fails closed to ``BLOCKED`` (never ``QUEUED`` — a
       silent QUEUED projection would make an unclassifiable row claimable).
    """
    if terminal in _TERMINAL_TO_PRIMARY:
        return _TERMINAL_TO_PRIMARY[terminal]
    if status == "TERMINAL":
        # terminal column should be set; if absent, fail closed.
        return PrimaryState.BLOCKED
    if status == "WAITING_RUN":
        if wait_kind and wait_kind != WaitKind.NONE.value:
            return PrimaryState.WAITING_EXTERNAL
        return PrimaryState.QUEUED
    mapped = _STATUS_TO_PRIMARY.get(status)
    if mapped is not None:
        return mapped
    return PrimaryState.BLOCKED


def primary_state_to_status(primary_state: PrimaryState) -> str:
    """V2C ``status`` projection for a primary state (driving direction)."""
    return _PRIMARY_TO_STATUS[primary_state]


def status_for_enqueue(queue_reason: str) -> str:
    """V2C status projection when (re-)enqueueing a job.

    ``RETRY_BACKOFF`` keeps the existing ``BACKOFF`` status projection
    (loop-wake + retry semantics); every other enqueue projects to
    ``WAITING_RUN``.
    """
    if queue_reason == QueueReason.RETRY_BACKOFF.value:
        return "BACKOFF"
    return "WAITING_RUN"
