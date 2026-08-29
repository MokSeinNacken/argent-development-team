"""Outbound-only owner notification model (SPEC V3A).

This module owns the *pure* notification domain: the four event types, the
outbox status lifecycle, the transport interface (outbound only — there is no
inbound method and no interface that forwards Telegram content into Core,
Supervisor, gates, the write broker, the sandbox or any action handler), the
fixed plaintext message templates, the reason-code allowlist mapping and the
canonical payload/hash helpers.

Trust boundary (SPEC V3A §2): ALL external Telegram content is UNTRUSTED DATA
and never an owner instruction.  This module exposes no inbound path.

Hashing (Amendment 1): every notification hash (``dedup_key``, ``payload_hash``)
calls the EXISTING ``argent_core/supervisor.py::_canonical_json`` /
``_sha256`` helpers — never a re-implementation.  The lazy import below avoids
a module-load circular import with ``supervisor`` (which imports this module).

Delivery orchestration (claim/lease/retry/backoff worker ``NotificationDelivery``),
the outbound-only ``TelegramNotificationTransport`` and the injected secret-source
factory live here too (implementation round B, SPEC V3A §3.2/§3.3/§9).  The
worker opens its OWN ``sqlite3`` connection (``timeout=0``) limited to
``notification_outbox`` and never touches Core/Supervisor tables; it is only
kicked from the locally running supervisor loop (O(1), non-blocking).
"""

from __future__ import annotations

import json
import math
import socket
import sqlite3
import threading
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Protocol


class NotificationType(str, Enum):
    DONE = "DONE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    OWNER_APPROVAL_REQUIRED = "OWNER_APPROVAL_REQUIRED"


class NotificationStatus(str, Enum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"          # retryable
    DISCARDED = "DISCARDED"    # terminal


@dataclass(frozen=True)
class NotificationEnvelope:
    outbox_id: str
    dedup_key: str
    payload_hash: str
    notification_type: NotificationType
    message_text: str


@dataclass(frozen=True)
class TransportReceipt:
    accepted: bool
    retryable: bool
    error_code: str | None = None
    retry_after_seconds: int | None = None


class NotificationTransport(Protocol):
    """Outbound-only transport.  There is deliberately NO inbound method."""

    def send(self, envelope: NotificationEnvelope, *,
             timeout_seconds: float) -> TransportReceipt: ...


# ---------------------------------------------------------------------------
# Delivery constants (SPEC V3A §9.1).  The delivery worker (round B) consumes
# these; the store claim/due primitives take the lease as a parameter.
# ---------------------------------------------------------------------------

NOTIFICATION_SEND_BATCH = 1
NOTIFICATION_MAX_ATTEMPTS = 5
NOTIFICATION_BACKOFF_INITIAL_SECONDS = 5
NOTIFICATION_BACKOFF_MULTIPLIER = 2
NOTIFICATION_BACKOFF_MAX_SECONDS = 300
NOTIFICATION_REQUEST_TIMEOUT_SECONDS = 5
NOTIFICATION_CLAIM_LEASE_SECONDS = 30

# ---------------------------------------------------------------------------
# Canonical JSON / hashing (Amendment 1: delegate to supervisor's helpers).
# ---------------------------------------------------------------------------


def _canonical_json(obj) -> str:
    from .supervisor import _canonical_json as _impl
    return _impl(obj)


def _sha256(text: str) -> str:
    from .supervisor import _sha256 as _impl
    return _impl(text)


# ---------------------------------------------------------------------------
# Allowlist (SPEC V3A §5.3): outgoing reason codes.  Internal lowercase plan
# reasons are reduced to one of these BEFORE payload/message creation — never
# free text, never an unexpected code in payload or message.
# ---------------------------------------------------------------------------

ALLOWED_REASON_CODES: frozenset[str] = frozenset(
    {
        "TASK_DONE",
        "TASK_FAILED",
        "TASK_CANCELLED",
        "MAX_ATTEMPTS",
        "PERSISTENT_ERROR",
        "TASK_BLOCKED",
        "GATE_REJECTED",
        "SPAWN_UNRESOLVABLE",
        "AMBIGUOUS_WRITER",
        "WAITING_GATE",
    }
)

_ALLOWED_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "template_version",
        "notification_type",
        "supervisor_job_id",
        "task_id",
        "event_ref",
        "event_at",
        "reason_code",
        "gate_id",
        "scope_ref",
    }
)

TEMPLATE_VERSION = "v1"

_DEDUP_REF_PREFIX_LEN = 16


def _dedup_ref(dedup_key: str) -> str:
    return dedup_key[:_DEDUP_REF_PREFIX_LEN]


# ---------------------------------------------------------------------------
# Deterministic event references (SPEC V3A §6)
# ---------------------------------------------------------------------------


def event_ref_close(job_id: str, terminal: str) -> str:
    """``supervisor:<job>:close:<TERMINAL>`` (DONE/FAILED/BLOCKED)."""
    return f"supervisor:{job_id}:close:{terminal}"


def event_ref_persistent_error(job_id: str) -> str:
    return f"supervisor:{job_id}:persistent-error:v1"


def event_ref_gate(job_id: str, gate_id: str) -> str:
    return f"supervisor:{job_id}:present-gate:{gate_id}"


# ---------------------------------------------------------------------------
# Dedup keys / ids / hashes (SPEC V3A §6, Amendment 1)
# ---------------------------------------------------------------------------


def normal_dedup_key(
    supervisor_job_id: str,
    notification_type: NotificationType | str,
    event_ref: str,
    event_version: int = 1,
) -> str:
    """``sha256(canonical_json(["argent-notification-v1", job_id,
    notification_type, event_ref, event_version]))``."""
    ntype = (
        notification_type.value
        if isinstance(notification_type, NotificationType)
        else notification_type
    )
    return _sha256(_canonical_json([
        "argent-notification-v1",
        supervisor_job_id,
        ntype,
        event_ref,
        event_version,
    ]))


def gate_dedup_key(
    supervisor_job_id: str,
    gate_id: str,
    binding_hash: str,
    event_version: int = 1,
) -> str:
    """``sha256(canonical_json(["argent-notification-v1", job_id,
    "OWNER_APPROVAL_REQUIRED", gate_id, binding_hash, event_version]))``."""
    return _sha256(_canonical_json([
        "argent-notification-v1",
        supervisor_job_id,
        "OWNER_APPROVAL_REQUIRED",
        gate_id,
        binding_hash,
        event_version,
    ]))


def outbox_id(dedup_key: str) -> str:
    return "notification:" + dedup_key


def payload_hash(payload: dict) -> str:
    return _sha256(_canonical_json(payload))


def canonical_payload_json(payload: dict) -> str:
    return _canonical_json(payload)


def scope_ref(binding_hash: str) -> str:
    """``sha256:<binding_hash[:16]>`` — the raw gate scope is never stored or
    sent (SPEC V3A §5.1)."""
    return "sha256:" + binding_hash[:16]


# ---------------------------------------------------------------------------
# Payload builder (SPEC V3A §5.1) — allowed keys only.
# ---------------------------------------------------------------------------


def build_payload(
    *,
    notification_type: str,
    supervisor_job_id: str,
    task_id: str,
    event_ref: str,
    event_at: str,
    reason_code: str,
    gate_id: str | None = None,
    scope_ref: str | None = None,
) -> dict:
    """Build a validated payload from the allowed key set ONLY.

    Task title/description, agent output, finding/handoff prose, raw gate
    scope, paths, patch contents, test output, raw errors, transport data and
    mail data are FORBIDDEN and simply have no key here (fail-closed by
    construction).
    """
    if reason_code not in ALLOWED_REASON_CODES:
        raise ValueError(f"reason_code not allowlisted: {reason_code!r}")
    payload: dict = {
        "template_version": TEMPLATE_VERSION,
        "notification_type": notification_type,
        "supervisor_job_id": supervisor_job_id,
        "task_id": task_id,
        "event_ref": event_ref,
        "event_at": event_at,
        "reason_code": reason_code,
    }
    if gate_id is not None:
        payload["gate_id"] = gate_id
    if scope_ref is not None:
        payload["scope_ref"] = scope_ref
    unknown = set(payload) - _ALLOWED_PAYLOAD_KEYS
    if unknown:
        raise ValueError(f"forbidden payload keys: {sorted(unknown)}")
    return payload


# ---------------------------------------------------------------------------
# Fixed plaintext templates (SPEC V3A §5.2) — no parse_mode, no links.
# ---------------------------------------------------------------------------


def render_message(
    notification_type: NotificationType | str,
    *,
    supervisor_job_id: str,
    task_id: str,
    event_at: str,
    reason_code: str,
    dedup_key: str,
    gate_id: str | None = None,
    scope_ref: str | None = None,
) -> str:
    """Render the exact fixed plaintext template for a notification type."""
    if isinstance(notification_type, str):
        notification_type = NotificationType(notification_type)
    ref = _dedup_ref(dedup_key)
    if notification_type is NotificationType.DONE:
        lines = [
            "ARGENT · DONE",
            f"Job: {supervisor_job_id}",
            f"Task: {task_id}",
            f"Time: {event_at}",
            f"Ref: {ref}",
        ]
    elif notification_type is NotificationType.FAILED:
        lines = [
            "ARGENT · FAILED",
            f"Job: {supervisor_job_id}",
            f"Task: {task_id}",
            f"Reason: {reason_code}",
            f"Time: {event_at}",
            f"Ref: {ref}",
        ]
    elif notification_type is NotificationType.BLOCKED:
        lines = [
            "ARGENT · BLOCKED",
            f"Job: {supervisor_job_id}",
            f"Task: {task_id}",
            f"Reason: {reason_code}",
            f"Time: {event_at}",
            f"Ref: {ref}",
        ]
    elif notification_type is NotificationType.OWNER_APPROVAL_REQUIRED:
        lines = [
            "ARGENT · OWNER APPROVAL REQUIRED",
            f"Job: {supervisor_job_id}",
            f"Task: {task_id}",
            f"Gate: {gate_id}",
            f"Scope ref: {scope_ref}",
            f"Time: {event_at}",
            f"Ref: {ref}",
            "Informational only. Use the authenticated owner-control path.",
        ]
    else:  # pragma: no cover - defensive
        raise ValueError(f"unknown notification type: {notification_type!r}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reason-code mapping (SPEC V3A §5.3, Amendments 2a/2b/2c) — explicit table,
# no free text.
# ---------------------------------------------------------------------------


def resolve_close_outcome(
    terminal: str,
    reason: str,
    *,
    task_state: str | None = None,
    gate_rejected: bool = False,
):
    """Map an internal lowercase plan reason for a CLOSE_JOB transition to
    ``(NotificationType, outgoing_reason_code)``, or None when no notification
    applies.

    ``task_state`` is the ``tasks.state`` value read in the SAME transaction
    (Amendment 2b distinguishes TASK_FAILED vs TASK_CANCELLED); ``gate_rejected``
    is derived from ``owner_approvals.status='rejected'`` (Amendment 2c).
    """
    if terminal == "DONE":
        return NotificationType.DONE, "TASK_DONE"
    if terminal == "FAILED":
        if reason == "max_attempts":
            return NotificationType.FAILED, "MAX_ATTEMPTS"
        # task_failed_cancelled collapses FAILED and CANCELLED (supervisor.py
        # ``if task.state in (TaskState.FAILED, TaskState.CANCELLED)``).
        if task_state == "CANCELLED":
            return NotificationType.FAILED, "TASK_CANCELLED"
        return NotificationType.FAILED, "TASK_FAILED"
    if terminal == "BLOCKED":
        # SPEC V3A §7 BLOCKED priority: rejected gate FIRST, then spawn
        # unresolvable, then ambiguous writer, then plain task blocked.
        if gate_rejected:
            return NotificationType.BLOCKED, "GATE_REJECTED"
        if reason == "spawn_unresolvable":
            return NotificationType.BLOCKED, "SPAWN_UNRESOLVABLE"
        if reason == "ambiguous_writer":
            return NotificationType.BLOCKED, "AMBIGUOUS_WRITER"
        return NotificationType.BLOCKED, "TASK_BLOCKED"
    return None


def persistent_error_outcome():
    """Any sticky ERROR/PERSISTENT_ERROR -> FAILED with code PERSISTENT_ERROR
    (SPEC V3A §5.3 Amendment 3 — internal details are reduced before payload
    creation)."""
    return NotificationType.FAILED, "PERSISTENT_ERROR"


def waiting_gate_outcome():
    """WAITING_GATE presentation -> OWNER_APPROVAL_REQUIRED / WAITING_GATE."""
    return NotificationType.OWNER_APPROVAL_REQUIRED, "WAITING_GATE"


# ---------------------------------------------------------------------------
# Delivery error codes (SPEC V3A §4.3/§5.4) — allowlisted.  A code not in this
# set is NEVER persisted to ``last_error_code``; the worker reduces it to
# ``TRANSPORT_ERROR`` before write.  ``SQLITE_LOCKED`` is result-only (a locked
# pass claims no row, so nothing is persisted).
# ---------------------------------------------------------------------------

ERROR_NETWORK = "NETWORK_ERROR"
ERROR_TIMEOUT = "TIMEOUT"
ERROR_RATE_LIMITED = "RATE_LIMITED"
ERROR_HTTP_5XX = "HTTP_5XX"
ERROR_HTTP_4XX = "HTTP_4XX"
ERROR_AUTH = "AUTH_ERROR"
ERROR_POLICY = "POLICY_ERROR"
ERROR_CONFIG = "CONFIG_ERROR"
ERROR_PAYLOAD_HASH_MISMATCH = "PAYLOAD_HASH_MISMATCH"
ERROR_ATTEMPTS_EXHAUSTED = "ATTEMPTS_EXHAUSTED"
ERROR_TRANSPORT = "TRANSPORT_ERROR"
ERROR_SQLITE_LOCKED = "SQLITE_LOCKED"  # result-only, never persisted

ALLOWED_DELIVERY_ERROR_CODES: frozenset[str] = frozenset(
    {
        ERROR_NETWORK,
        ERROR_TIMEOUT,
        ERROR_RATE_LIMITED,
        ERROR_HTTP_5XX,
        ERROR_HTTP_4XX,
        ERROR_AUTH,
        ERROR_POLICY,
        ERROR_CONFIG,
        ERROR_PAYLOAD_HASH_MISMATCH,
        ERROR_ATTEMPTS_EXHAUSTED,
        ERROR_TRANSPORT,
    }
)


class NotificationConfigError(Exception):
    """Non-retryable: the transport has no usable configuration (disabled).

    Raised by a transport factory when no credential/target is injected; the
    supervisor keeps running (the row is DISCARDED, never retried).
    """


def backoff_seconds(attempt_count: int, retry_after_seconds: int | None = None) -> int:
    """SPEC V3A §9.1: ``min(5 * 2 ** (attempt_count - 1), 300)``; a valid
    Retry-After is clamped to 5..300 and can only extend (never shorten) the
    default backoff."""
    base = min(
        NOTIFICATION_BACKOFF_INITIAL_SECONDS
        * (NOTIFICATION_BACKOFF_MULTIPLIER ** (attempt_count - 1)),
        NOTIFICATION_BACKOFF_MAX_SECONDS,
    )
    if retry_after_seconds is not None and retry_after_seconds > 0:
        clamped = min(
            max(retry_after_seconds, NOTIFICATION_BACKOFF_INITIAL_SECONDS),
            NOTIFICATION_BACKOFF_MAX_SECONDS,
        )
        return max(base, clamped)
    return base


@dataclass(frozen=True)
class DeliveryPassResult:
    """Outcome of a single ``NotificationDelivery.send_due_once()`` pass."""

    claimed: bool
    outbox_id: str | None = None
    outcome: str | None = None  # SENT | FAILED | DISCARDED | NOT_DUE | LOCKED | ERROR
    error_code: str | None = None
    retry_after_seconds: int | None = None


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Secret source interface (SPEC V3A §3.3).  Credentials/target are injected
# into the transport factory only — never read from disk by this module, never
# persisted and never logged.  There is no inbound method anywhere.
# ---------------------------------------------------------------------------


class NotificationSecretSource(Protocol):
    def telegram_bot_token(self) -> str | None: ...

    def telegram_chat_id(self) -> str | None: ...


def telegram_transport_factory(
    secret_source: NotificationSecretSource, *,
    timeout_seconds: float = NOTIFICATION_REQUEST_TIMEOUT_SECONDS,
) -> Callable[[], NotificationTransport]:
    """Build a factory that reads the injected secret source ONLY at call time
    (inside the delivery worker).  Missing configuration raises the non-
    retryable :class:`NotificationConfigError` — the supervisor keeps running."""

    def factory() -> NotificationTransport:
        token = secret_source.telegram_bot_token()
        chat_id = secret_source.telegram_chat_id()
        if not token or not chat_id:
            raise NotificationConfigError(
                "telegram notifications disabled (no credential/target configured)"
            )
        return TelegramNotificationTransport(
            token, chat_id, timeout_seconds=timeout_seconds,
        )

    return factory


# ---------------------------------------------------------------------------
# Outbound-only Telegram adapter (SPEC V3A §3.2/§5).  No inbound method, no
# parse_mode, no links; fixed template text comes from ``render_message``.
# ``send()`` NEVER raises out of the worker: every transport/HTTP failure is
# mapped to a :class:`TransportReceipt`.  Contains NO real credentials.
# ---------------------------------------------------------------------------


class TelegramNotificationTransport:
    API_BASE = "https://api.telegram.org"

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        timeout_seconds: float = NOTIFICATION_REQUEST_TIMEOUT_SECONDS,
        request_fn: Callable | None = None,
    ):
        if not bot_token or not chat_id:
            raise NotificationConfigError(
                "telegram notifications disabled (missing bot_token/chat_id)"
            )
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._timeout_seconds = timeout_seconds
        # Injectable HTTP primitive for offline tests; the real one uses
        # urllib with a hard timeout.  Never persisted/logged.
        self._request_fn = request_fn

    # -- inbound: there is deliberately NO method that accepts Telegram input.

    def send(self, envelope: NotificationEnvelope, *,
             timeout_seconds: float | None = None) -> TransportReceipt:
        timeout = (
            self._timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        url = f"{self.API_BASE}/bot{self._bot_token}/sendMessage"
        body = json.dumps(
            {"chat_id": self._chat_id, "text": envelope.message_text}
        ).encode("utf-8")
        try:
            status, body_text = self._post(url, body, timeout)
            # _map_response stays INSIDE the safe classification boundary so
            # no numeric-conversion error from untrusted Telegram API content
            # can escape send() (send() is total, never raises).
            return self._map_response(status, body_text)
        except NotificationConfigError:
            return TransportReceipt(False, False, ERROR_CONFIG)
        except socket.timeout:
            return TransportReceipt(False, True, ERROR_TIMEOUT)
        except Exception:
            # Connection error / DNS / read failure / unexpected -> retryable.
            return TransportReceipt(False, True, ERROR_NETWORK)

    def _post(self, url: str, body: bytes, timeout: float) -> tuple[int, str]:
        if self._request_fn is not None:
            return self._request_fn(url, body, timeout)
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", "replace") if exc.fp else ""
            return exc.code, body_text

    @staticmethod
    def _json_field(body_text: str, field: str, default=None):
        try:
            data = json.loads(body_text)
        except (ValueError, TypeError):
            return default
        return data.get(field, default) if isinstance(data, dict) else default

    def _parse_retry_after(self, body_text: str) -> int | None:
        try:
            data = json.loads(body_text)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        params = data.get("parameters")
        if not isinstance(params, dict):
            return None
        ra = params.get("retry_after")
        # Accept ONLY finite, positive numeric values.  Non-finite floats
        # (NaN/inf) would raise OverflowError in int() and must never escape
        # send() for untrusted Telegram API content.  bool is a numeric
        # subclass but never a valid retry_after.  The 5..300 window is
        # enforced downstream by backoff_seconds.
        if isinstance(ra, bool):
            return None
        if isinstance(ra, (int, float)):
            if not math.isfinite(ra) or ra <= 0:
                return None
            return int(ra)
        return None

    def _map_response(self, status: int, body_text: str) -> TransportReceipt:
        if status == 429:
            return TransportReceipt(
                False, True, ERROR_RATE_LIMITED, self._parse_retry_after(body_text),
            )
        if 500 <= status < 600:
            return TransportReceipt(False, True, ERROR_HTTP_5XX)
        if 200 <= status < 300:
            if self._json_field(body_text, "ok", False) is True:
                return TransportReceipt(True, False)
            code = self._json_field(body_text, "error_code", None)
            if isinstance(code, int) and code == 429:
                return TransportReceipt(
                    False, True, ERROR_RATE_LIMITED,
                    self._parse_retry_after(body_text),
                )
            if isinstance(code, int) and 500 <= code < 600:
                return TransportReceipt(False, True, ERROR_HTTP_5XX)
            if isinstance(code, int) and code in (401, 403):
                return TransportReceipt(False, False, ERROR_AUTH)
            return TransportReceipt(False, False, ERROR_HTTP_4XX)
        if 400 <= status < 500:
            if status in (401, 403):
                return TransportReceipt(False, False, ERROR_AUTH)
            return TransportReceipt(False, False, ERROR_HTTP_4XX)
        return TransportReceipt(False, False, ERROR_HTTP_4XX)


# ---------------------------------------------------------------------------
# Delivery worker (SPEC V3A §3.3/§3.4/§9).  Non-blocking, bounded, restart-
# proof.  ``kick()`` returns immediately and starts at most ONE process-local
# daemon worker; the worker runs a single bounded pass and has no wake/sleep
# loop.  ``send_due_once()`` performs the pass synchronously (also usable as a
# bounded manual send).  The worker uses its OWN SQLite connection limited to
# ``notification_outbox`` with ``timeout=0`` and never touches other tables.
# ---------------------------------------------------------------------------


class NotificationDelivery:
    def __init__(
        self,
        db_path: str,
        transport_factory: Callable[[], NotificationTransport],
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self._db_path = db_path
        self._transport_factory = transport_factory
        self._clock = clock or utcnow
        self._worker_lock = threading.Lock()
        self._worker: threading.Thread | None = None

    @property
    def worker_running(self) -> bool:
        """True iff the single process-local delivery worker is currently
        alive (used by tests to assert the at-most-one-worker invariant)."""
        with self._worker_lock:
            return self._worker is not None and self._worker.is_alive()

    def kick(self) -> None:
        """O(1), non-blocking, catch-all: start at most one daemon worker."""
        try:
            with self._worker_lock:
                if self._worker is not None and self._worker.is_alive():
                    return
                worker = threading.Thread(
                    target=self._run_worker, daemon=True,
                    name="notification-delivery-worker",
                )
                self._worker = worker
                worker.start()
        except BaseException:  # noqa: BLE001 - never propagate into the loop
            pass

    def _run_worker(self) -> None:
        try:
            self.send_due_once()
        except BaseException:  # noqa: BLE001 - worker must never escape
            pass

    def _now(self) -> datetime:
        return self._clock()

    def _now_iso(self) -> str:
        return _iso(self._now())

    def _lease_cutoff_iso(self) -> str:
        return _iso(self._now() - timedelta(seconds=NOTIFICATION_CLAIM_LEASE_SECONDS))

    @staticmethod
    def _new_claim_token() -> str:
        return "claim:" + uuid.uuid4().hex

    def send_due_once(self) -> DeliveryPassResult:
        """One bounded delivery pass (NOTIFICATION_SEND_BATCH = 1 row).

        Opens a dedicated ``timeout=0`` connection, claims the oldest due row,
        sends it, and completes/fails/discards it.  A locked DB aborts cleanly
        (row unchanged).  Never propagates a transport exception."""
        now_iso = self._now_iso()
        try:
            conn = sqlite3.connect(
                self._db_path, timeout=0, isolation_level=None,
            )
        except sqlite3.Error:
            return DeliveryPassResult(False, outcome="ERROR", error_code=ERROR_TRANSPORT)
        conn.row_factory = sqlite3.Row
        try:
            try:
                return self._pass(conn, now_iso)
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower():
                    return DeliveryPassResult(
                        False, outcome="LOCKED", error_code=ERROR_SQLITE_LOCKED,
                    )
                return DeliveryPassResult(
                    False, outcome="ERROR", error_code=ERROR_TRANSPORT,
                )
        finally:
            try:
                conn.close()
            except BaseException:  # noqa: BLE001
                pass

    def _pass(self, conn: sqlite3.Connection, now_iso: str) -> DeliveryPassResult:
        row, discarded = self._claim(conn, now_iso)
        if discarded is not None:
            return discarded
        if row is None:
            return DeliveryPassResult(False, outcome="NOT_DUE")
        outbox_id = row["id"]
        claim_token = row["claim_token"]
        attempt_count = row["attempt_count"]

        payload, hash_ok = self._decode_and_verify(row)
        if not hash_ok:
            self._discard(conn, outbox_id, claim_token, now_iso,
                          error_code=ERROR_PAYLOAD_HASH_MISMATCH)
            return DeliveryPassResult(
                True, outbox_id=outbox_id, outcome="DISCARDED",
                error_code=ERROR_PAYLOAD_HASH_MISMATCH,
            )

        envelope = NotificationEnvelope(
            outbox_id=outbox_id,
            dedup_key=row["dedup_key"],
            payload_hash=row["payload_hash"],
            notification_type=NotificationType(row["notification_type"]),
            message_text=self._render(row, payload),
        )

        try:
            transport = self._transport_factory()
        except NotificationConfigError:
            self._discard(conn, outbox_id, claim_token, now_iso,
                          error_code=ERROR_CONFIG)
            return DeliveryPassResult(
                True, outbox_id=outbox_id, outcome="DISCARDED",
                error_code=ERROR_CONFIG,
            )
        except BaseException:  # noqa: BLE001 - temporary constructor failure
            return self._finalize(
                conn, row, claim_token, attempt_count, now_iso,
                TransportReceipt(False, True, ERROR_TRANSPORT),
            )

        try:
            receipt = transport.send(
                envelope, timeout_seconds=NOTIFICATION_REQUEST_TIMEOUT_SECONDS,
            )
        except NotificationConfigError:
            self._discard(conn, outbox_id, claim_token, now_iso,
                          error_code=ERROR_CONFIG)
            return DeliveryPassResult(
                True, outbox_id=outbox_id, outcome="DISCARDED",
                error_code=ERROR_CONFIG,
            )
        except BaseException:  # noqa: BLE001 - transport must not escape
            receipt = TransportReceipt(False, True, ERROR_TRANSPORT)
        if not isinstance(receipt, TransportReceipt):
            receipt = TransportReceipt(False, True, ERROR_TRANSPORT)

        return self._finalize(conn, row, claim_token, attempt_count, now_iso, receipt)

    def _finalize(self, conn, row, claim_token, attempt_count, now_iso,
                  receipt: TransportReceipt) -> DeliveryPassResult:
        outbox_id = row["id"]
        # F1: the ceiling check applies to ALL outcomes.  attempt_count is the
        # post-increment value, so strictly-greater-than the ceiling means the
        # row was claimed when already at the ceiling and must NEVER reach SENT
        # — even an ACCEPTED receipt is terminally discarded (strictest
        # interpretation of the five-attempt bound).  Rows that legitimately
        # complete within the budget keep their SENT stickiness.
        if attempt_count > NOTIFICATION_MAX_ATTEMPTS:
            self._discard(conn, outbox_id, claim_token, now_iso,
                          error_code=ERROR_ATTEMPTS_EXHAUSTED)
            return DeliveryPassResult(
                True, outbox_id=outbox_id, outcome="DISCARDED",
                error_code=ERROR_ATTEMPTS_EXHAUSTED,
            )
        if receipt.accepted:
            self._complete_sent(conn, outbox_id, claim_token, now_iso)
            return DeliveryPassResult(
                True, outbox_id=outbox_id, outcome="SENT",
            )
        code = self._normalize_error_code(receipt.error_code)
        if receipt.retryable:
            if attempt_count >= NOTIFICATION_MAX_ATTEMPTS:
                self._discard(conn, outbox_id, claim_token, now_iso,
                              error_code=ERROR_ATTEMPTS_EXHAUSTED)
                return DeliveryPassResult(
                    True, outbox_id=outbox_id, outcome="DISCARDED",
                    error_code=ERROR_ATTEMPTS_EXHAUSTED,
                )
            delay = backoff_seconds(attempt_count, receipt.retry_after_seconds)
            next_attempt_at = _iso(self._now() + timedelta(seconds=delay))
            self._mark_failed(conn, outbox_id, claim_token, now_iso,
                              next_attempt_at=next_attempt_at, error_code=code)
            return DeliveryPassResult(
                True, outbox_id=outbox_id, outcome="FAILED", error_code=code,
                retry_after_seconds=delay,
            )
        self._discard(conn, outbox_id, claim_token, now_iso, error_code=code)
        return DeliveryPassResult(
            True, outbox_id=outbox_id, outcome="DISCARDED", error_code=code,
        )

    @staticmethod
    def _normalize_error_code(code: str | None) -> str:
        if code in ALLOWED_DELIVERY_ERROR_CODES:
            return code
        return ERROR_TRANSPORT

    def _decode_and_verify(self, row) -> tuple[dict | None, bool]:
        try:
            payload = json.loads(row["payload_json"])
        except (ValueError, TypeError):
            return None, False
        if payload_hash(payload) != row["payload_hash"]:
            return payload, False
        return payload, True

    def _render(self, row, payload: dict) -> str:
        return render_message(
            NotificationType(row["notification_type"]),
            supervisor_job_id=payload["supervisor_job_id"],
            task_id=payload["task_id"],
            event_at=payload["event_at"],
            reason_code=payload["reason_code"],
            dedup_key=row["dedup_key"],
            gate_id=payload.get("gate_id"),
            scope_ref=payload.get("scope_ref"),
        )

    # -- claim / CAS (SPEC V3A §9.2/§9.3), on the dedicated connection ------

    def _claim(self, conn: sqlite3.Connection, now_iso: str):
        """Claim the oldest due row, or discard it if it is already at the
        attempt ceiling.

        Returns ``(row, discarded)``: ``row`` is the claimed row dict (or None
        when nothing is due); ``discarded`` is a :class:`DeliveryPassResult`
        when the due row was terminally DISCARDED at the ceiling (F1), else
        None.  Everything happens under BEGIN IMMEDIATE.
        """
        cutoff = self._lease_cutoff_iso()
        conn.execute("BEGIN IMMEDIATE")
        try:
            due = conn.execute(
                "SELECT id, attempt_count FROM notification_outbox WHERE "
                "status = 'PENDING' "
                "OR (status = 'FAILED' AND next_attempt_at IS NOT NULL "
                "AND next_attempt_at <= ?) "
                "OR (status = 'SENDING' AND claimed_at IS NOT NULL "
                "AND claimed_at <= ?) "
                "ORDER BY created_at, rowid LIMIT 1",
                (now_iso, cutoff),
            ).fetchone()
            if due is None:
                conn.execute("COMMIT")
                return None, None
            if due["attempt_count"] >= NOTIFICATION_MAX_ATTEMPTS:
                # F1: any due row already at the attempt ceiling is terminally
                # DISCARDED (ATTEMPTS_EXHAUSTED) BEFORE any claim/send, so the
                # five-attempt bound cannot be exceeded across lease-reclaim or
                # restart cycles.  It is never claimed or sent again.
                conn.execute(
                    "UPDATE notification_outbox SET status = 'DISCARDED', "
                    "claim_token = NULL, claimed_at = NULL, "
                    "next_attempt_at = NULL, last_error_code = ?, "
                    "updated_at = ? "
                    "WHERE id = ? AND status IN ('PENDING', 'FAILED', 'SENDING')",
                    (ERROR_ATTEMPTS_EXHAUSTED, now_iso, due["id"]),
                )
                conn.execute("COMMIT")
                return None, DeliveryPassResult(
                    True, outbox_id=due["id"], outcome="DISCARDED",
                    error_code=ERROR_ATTEMPTS_EXHAUSTED,
                )
            token = self._new_claim_token()
            cur = conn.execute(
                "UPDATE notification_outbox SET status = 'SENDING', "
                "claim_token = ?, claimed_at = ?, last_attempt_at = ?, "
                "attempt_count = attempt_count + 1, next_attempt_at = NULL, "
                "updated_at = ? "
                "WHERE id = ? AND status IN ('PENDING', 'FAILED', 'SENDING') "
                "AND attempt_count < ?",
                (token, now_iso, now_iso, now_iso, due["id"],
                 NOTIFICATION_MAX_ATTEMPTS),
            )
            conn.execute("COMMIT")
            if cur.rowcount != 1:
                return None, None
            claimed = conn.execute(
                "SELECT * FROM notification_outbox WHERE id = ?", (due["id"],),
            ).fetchone()
            return (dict(claimed) if claimed is not None else None), None
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except BaseException:  # noqa: BLE001
                pass
            raise

    def _complete_sent(self, conn, outbox_id, claim_token, now_iso) -> bool:
        cur = conn.execute(
            "UPDATE notification_outbox SET status = 'SENT', sent_at = ?, "
            "claim_token = NULL, claimed_at = NULL, next_attempt_at = NULL, "
            "last_error_code = NULL, last_attempt_at = ?, updated_at = ? "
            "WHERE id = ? AND status = 'SENDING' AND claim_token = ?",
            (now_iso, now_iso, now_iso, outbox_id, claim_token),
        )
        return cur.rowcount == 1

    def _mark_failed(self, conn, outbox_id, claim_token, now_iso, *,
                     next_attempt_at, error_code) -> bool:
        cur = conn.execute(
            "UPDATE notification_outbox SET status = 'FAILED', "
            "claim_token = NULL, claimed_at = NULL, next_attempt_at = ?, "
            "last_error_code = ?, updated_at = ? "
            "WHERE id = ? AND status = 'SENDING' AND claim_token = ?",
            (next_attempt_at, error_code, now_iso, outbox_id, claim_token),
        )
        return cur.rowcount == 1

    def _discard(self, conn, outbox_id, claim_token, now_iso, *, error_code) -> bool:
        cur = conn.execute(
            "UPDATE notification_outbox SET status = 'DISCARDED', "
            "claim_token = NULL, claimed_at = NULL, next_attempt_at = NULL, "
            "last_error_code = ?, updated_at = ? "
            "WHERE id = ? AND status = 'SENDING' AND claim_token = ?",
            (error_code, now_iso, outbox_id, claim_token),
        )
        return cur.rowcount == 1
