"""Owner-approval challenge core (SPEC V3C §7/§8, owner amendments A1/A2).

Transport-neutral capability primitives with NO Telegram, OpenClaw, or
configuration coupling.  Persistence happens through ``Store`` and only ever
touches the three V6 tables (``approval_challenges``, ``telegram_update_log``,
``telegram_inbound_state``).

Security model (A2):

- A challenge token is a 256-bit CSPRNG capability
  (``secrets.token_urlsafe(32)``, ~43 URL-safe Base64URL characters, no
  padding).  There is NO HMAC key, NO persistent secret, NO key rotation.
- Only ``sha256(token)`` is persisted; the raw token exists only in memory and
  must never be persisted or logged.
- A challenge is single-use and every target state is terminal.
- The callback ``challenge`` reference (``A:``/``R:``/``D:`` + 43 characters)
  is the raw token; the ``challenge_id`` (``"challenge:" + uuid4().hex``) is
  the internal row id and is never shown in a callback.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from .models import ApprovalError, ApprovalStatus, OwnerApproval

CHALLENGE_TOKEN_BYTES = 32
CHALLENGE_TTL_SECONDS = 3600  # A2 / §7.4: 60 minutes, never longer than approval TTL.

# §5: strictly ``[ARD]:`` + exactly 43 opaque URL-safe characters, ASCII only.
_CALLBACK_RE = re.compile(r"\A([ARD]):([A-Za-z0-9_-]{43})\Z")


class ChallengeStatus(str, Enum):
    ISSUED = "ISSUED"
    CONSUMED_APPROVED = "CONSUMED_APPROVED"
    CONSUMED_REJECTED = "CONSUMED_REJECTED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


class CallbackAction(str, Enum):
    APPROVE = "A"
    REJECT = "R"
    DETAILS = "D"


@dataclass(frozen=True)
class Challenge:
    """Mirror of an ``approval_challenges`` row (SPEC V3C §8.1)."""

    id: str
    approval_id: str
    task_id: str
    supervisor_job_id: str
    binding_hash: str
    notification_outbox_id: Optional[str]
    token_hash: str
    status: str
    created_at: str
    expires_at: str
    consumed_at: Optional[str]
    consumed_update_id: Optional[int]
    invalidated_at: Optional[str]


def generate_challenge_token() -> str:
    """A 256-bit CSPRNG capability token (A2): ~43 URL-safe chars, no padding."""
    return secrets.token_urlsafe(CHALLENGE_TOKEN_BYTES)


def token_hash(token: str) -> str:
    """The only persisted representation of a token: its full SHA-256 hex."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def challenge_id() -> str:
    """Internal row id (never a callback reference)."""
    return "challenge:" + uuid.uuid4().hex


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def compute_expires_at(now: datetime, approval_expires_at: str) -> str:
    """``min(now + CHALLENGE_TTL_SECONDS, approval.expires_at)`` (§7.4)."""
    ttl_expiry = now + timedelta(seconds=CHALLENGE_TTL_SECONDS)
    approval_expiry = _parse_dt(approval_expires_at)
    if approval_expiry.tzinfo is None:
        approval_expiry = approval_expiry.replace(tzinfo=timezone.utc)
    if ttl_expiry.tzinfo is None:
        ttl_expiry = ttl_expiry.replace(tzinfo=timezone.utc)
    return _iso(min(ttl_expiry, approval_expiry))


def create_challenge(
    store,
    *,
    approval: OwnerApproval,
    supervisor_job_id: str,
    notification_outbox_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> tuple[Challenge, str]:
    """Create and persist a challenge for a pending owner approval.

    Validates the preconditions (§7.1): the approval must be ``pending``, its
    ``binding_hash`` must match ``gates.binding_hash(task_id, action, scope)``,
    the approval must not be expired, and there must be no other active
    (``ISSUED``) challenge for the same approval id.  Returns the persisted
    ``Challenge`` row and the raw token — the raw token exists ONLY here in
    memory and is never persisted or logged.

    Raises ``ApprovalError`` (fail-closed) on any failed precondition.
    """
    from .gates import binding_hash

    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if approval.status != ApprovalStatus.PENDING:
        raise ApprovalError("approval is not pending; cannot create a challenge")

    expected = binding_hash(approval.task_id, approval.action, approval.scope)
    if approval.binding_hash != expected:
        raise ApprovalError("approval binding_hash mismatch; no challenge")

    if _parse_dt(approval.expires_at) <= now:
        raise ApprovalError("approval expired; no challenge")

    if store.get_active_challenge_for_approval(approval.id) is not None:
        raise ApprovalError("an active challenge already exists for this approval")

    cid = challenge_id()
    raw_token = generate_challenge_token()
    created_iso = _iso(now)
    expires_iso = compute_expires_at(now, approval.expires_at)

    row = {
        "id": cid,
        "approval_id": approval.id,
        "task_id": approval.task_id,
        "supervisor_job_id": supervisor_job_id,
        "binding_hash": expected,
        "notification_outbox_id": notification_outbox_id,
        "token_hash": token_hash(raw_token),
        "status": ChallengeStatus.ISSUED.value,
        "created_at": created_iso,
        "expires_at": expires_iso,
        "consumed_at": None,
        "consumed_update_id": None,
        "invalidated_at": None,
    }
    store._insert_challenge(row)

    challenge = Challenge(
        id=cid,
        approval_id=approval.id,
        task_id=approval.task_id,
        supervisor_job_id=supervisor_job_id,
        binding_hash=expected,
        notification_outbox_id=notification_outbox_id,
        token_hash=row["token_hash"],
        status=ChallengeStatus.ISSUED.value,
        created_at=created_iso,
        expires_at=expires_iso,
        consumed_at=None,
        consumed_update_id=None,
        invalidated_at=None,
    )
    return challenge, raw_token


def parse_callback(callback_data: str):
    """Parse a strict ``[ARD]:<43-char challenge>`` callback (A1/§5).

    Returns ``(CallbackAction, challenge)`` on an exact match, or ``None``
    (fail-closed) for anything else: wrong action, wrong length, extra
    characters, non-ASCII, lowercase action, leading slash, etc.
    """
    if not isinstance(callback_data, str):
        return None
    m = _CALLBACK_RE.match(callback_data)
    if m is None:
        return None
    action = CallbackAction(m.group(1))
    return action, m.group(2)


def challenge_from_row(row: dict) -> Challenge:
    """Build a :class:`Challenge` from a persisted ``approval_challenges`` row."""
    return Challenge(
        id=row["id"],
        approval_id=row["approval_id"],
        task_id=row["task_id"],
        supervisor_job_id=row["supervisor_job_id"],
        binding_hash=row["binding_hash"],
        notification_outbox_id=row["notification_outbox_id"],
        token_hash=row["token_hash"],
        status=row["status"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        consumed_at=row["consumed_at"],
        consumed_update_id=row["consumed_update_id"],
        invalidated_at=row["invalidated_at"],
    )


# -- challenge state machine (§7.3) -----------------------------------------
# Every target state is terminal; the underlying CAS transitions only from
# ``ISSUED``, so a consumed/expired/invalidated challenge can never be
# reopened (a second transition silently returns False — fail-closed).


def consume_approved(
    store, challenge_id: str, *, consumed_at: str, consumed_update_id: int,
) -> bool:
    """ISSUED -> CONSUMED_APPROVED (terminal).  False if not ISSUED."""
    return store._consume_challenge(
        challenge_id, ChallengeStatus.CONSUMED_APPROVED.value,
        consumed_at, consumed_update_id,
    ) == 1


def consume_rejected(
    store, challenge_id: str, *, consumed_at: str, consumed_update_id: int,
) -> bool:
    """ISSUED -> CONSUMED_REJECTED (terminal).  False if not ISSUED."""
    return store._consume_challenge(
        challenge_id, ChallengeStatus.CONSUMED_REJECTED.value,
        consumed_at, consumed_update_id,
    ) == 1


def expire_challenge(store, challenge_id: str, *, now_iso: str) -> bool:
    """ISSUED -> EXPIRED (terminal).  False if not ISSUED."""
    return store._mark_challenge_expired(challenge_id, now_iso) == 1


def invalidate_challenge(store, challenge_id: str, *, now_iso: str) -> bool:
    """ISSUED -> INVALIDATED (terminal).  False if not ISSUED."""
    return store._mark_challenge_invalidated(challenge_id, now_iso) == 1
