"""Transport-neutral owner-approval callback processor (SPEC V3C §4/§5/§6.2/
§6.3/§10/§11/§12/§14, owner amendments A1/A3/A6/A7).

This module turns a *structurally pre-validated* Telegram inline-button
callback (``A:``/``R:``/``D:`` + opaque challenge) into a deterministic
decision against the persisted ``owner_approvals`` ledger.  It is the single
security boundary between untrusted callback data and the approval authority.

Design invariants:

- **No free text.**  The input contract is fully structured: ``action`` (a
  :class:`CallbackAction`), ``challenge`` (the opaque 43-char capability), an
  integer ``update_id``, an integer ``message_date`` and two identities.
  There is no text parser and no way for a callback to name a task, gate,
  action or scope.
- **Fail-closed identity (A3/§5).**  There is ONE canonical owner identity:
  the injected expected user AND chat values must resolve to the SAME string,
  and the presented sender AND private-chat identities must both equal that
  single canonical value (and each other).  Any inconsistency — a provider
  whose two expected values differ, or a presented pair whose sender != chat —
  performs no action, consumes no challenge and renders no details, and is
  persisted as ``WRONG_CHAT`` / ``SPOOFED_SENDER`` (§14).
- **Exactly-once decision (A7/§10/§11).**  Update dedup (``telegram_update_log``
  PK), challenge CAS (``ISSUED`` -> terminal) and the approval CAS all commit
  in ONE ``BEGIN IMMEDIATE`` transaction together with the update-outcome and
  the cursor advance.  Two controllers can never both decide a gate.
- **Stale updates are no-ops (A7/§6.2).**  Every update below the persisted
  cursor (``telegram_inbound_state.next_update_id``) is atomically recognized
  as old, persisted as ``STALE_UPDATE`` (or ``DUPLICATE_UPDATE`` when already
  logged) and never mutates the gate nor consumes a challenge.
- **Message time is validated (§10/§16.6).**  ``message_date`` must be a
  bounded nonnegative integer and lie within the challenge window
  ``[created_at, expires_at]`` with a bounded future-clock-skew allowance
  (``FUTURE_SKEW_BOUND``).
- **No secrets (§15).**  The raw token, the token hash, chat/user ids and the
  raw scope never appear in persisted rows, outcomes or the details payload.
- **Malformed input never crashes (§10/§14).**  Every parsing/type error yields
  a structured, persisted outcome; no exception escapes ``process_callback``.
- **Immediate termination on DB lock (§14).**  Callback processing runs on a
  dedicated connection with ``busy_timeout=0``; a writer lock aborts
  ``BEGIN IMMEDIATE`` immediately as ``LOCKED``, leaving cursor and gate
  untouched.
- **Post-decision UX is best-effort (A6).**  Telegram UI edits run only AFTER
  the decision has committed; a UI failure never rolls the decision back.

Persistence happens only through ``Store`` and only touches the V6 tables
(``approval_challenges``, ``telegram_update_log``, ``telegram_inbound_state``)
plus the pre-existing ``owner_approvals`` / ``events`` / ``command_idempotency``
tables via the ``Core`` bridge helpers.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Optional, Protocol
from uuid import uuid4

from . import state_machine
from .approval_core import CallbackAction, ChallengeStatus, token_hash
from .gates import binding_hash
from .models import ApprovalError, ApprovalStatus, Event, TaskState
from .state_machine import is_valid_resume_target

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .core import Core

# Strict opaque challenge reference: exactly 43 URL-safe Base64URL chars (A1/A2).
_CHALLENGE_RE = re.compile(r"\A[A-Za-z0-9_-]{43}\Z")

# Bounded future clock-skew allowance for ``message_date`` (SPEC V3C §10/§16.6):
# a message timestamp may be at most this many seconds ahead of local time.
FUTURE_SKEW_BOUND = 300

# SQLite INTEGER is a signed 64-bit integer: values beyond 2**63-1 cannot be
# represented and would raise OverflowError mid-transaction (F4).  Both the
# ``update_id`` and ``message_date`` must fit this range or be MALFORMED.
_SQLITE_MAX_INT = 2**63 - 1


class CallbackOutcome(str, Enum):
    """Structured, terminal result of one callback (SPEC V3C §14)."""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DETAILS = "DETAILS"
    DUPLICATE_UPDATE = "DUPLICATE_UPDATE"
    STALE_UPDATE = "STALE_UPDATE"
    STALE_MESSAGE = "STALE_MESSAGE"
    USED_TOKEN = "USED_TOKEN"
    EXPIRED = "EXPIRED"
    UNKNOWN_CHALLENGE = "UNKNOWN_CHALLENGE"
    WRONG_CHAT = "WRONG_CHAT"
    SPOOFED_SENDER = "SPOOFED_SENDER"
    GATE_CHANGED = "GATE_CHANGED"
    GATE_DECIDED = "GATE_DECIDED"
    MALFORMED = "MALFORMED"
    LOCKED = "LOCKED"
    ERROR = "ERROR"


class OwnerIdentitySource(Protocol):
    """Injected owner-identity provider (A3): the single owner allowlist.

    The processor verifies BOTH identities (sender AND private chat) against
    these two expected values; there is no other owner identity in the system.
    """

    def expected_owner_user_id(self) -> Optional[str]: ...

    def expected_owner_chat_id(self) -> Optional[str]: ...


class PostDecisionUx(Protocol):
    """Best-effort, non-authoritative post-decision Telegram UI (A6).

    These are called ONLY after the persisted decision has committed.  Every
    implementation must be fail-safe: an exception here must never roll back
    the decision or alter any gate state.
    """

    def answer_callback_query(self, ref: str) -> None: ...

    def edit_approval_message(self, ref: str, decided: bool) -> None: ...

    def remove_buttons(self, ref: str) -> None: ...


class DeterministicMockPostDecisionUx:
    """Deterministic, offline mock of :class:`PostDecisionUx` for tests.

    Records every call and can be scripted to raise on any individual call to
    prove that a UI failure never rolls back the committed decision (A6).
    """

    def __init__(
        self,
        *,
        fail_answer: bool = False,
        fail_edit: bool = False,
        fail_remove: bool = False,
    ) -> None:
        self.calls: list = []
        self._fail_answer = fail_answer
        self._fail_edit = fail_edit
        self._fail_remove = fail_remove

    def answer_callback_query(self, ref: str) -> None:
        self.calls.append(("answer_callback_query", ref))
        if self._fail_answer:
            raise RuntimeError("injected answer_callback_query failure")

    def edit_approval_message(self, ref: str, decided: bool) -> None:
        self.calls.append(("edit_approval_message", ref, decided))
        if self._fail_edit:
            raise RuntimeError("injected edit_approval_message failure")

    def remove_buttons(self, ref: str) -> None:
        self.calls.append(("remove_buttons", ref))
        if self._fail_remove:
            raise RuntimeError("injected remove_buttons failure")


class _NoopUx:
    """Default silent UX used when no :class:`PostDecisionUx` is injected."""

    def answer_callback_query(self, ref: str) -> None:
        return None

    def edit_approval_message(self, ref: str, decided: bool) -> None:
        return None

    def remove_buttons(self, ref: str) -> None:
        return None


def _hash_json(obj) -> str:
    """Canonical SHA-256 of a small JSON object (idempotency args hash)."""
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _iso_to_unix(iso: str) -> int:
    """Parse an ISO-8601 timestamp to a whole-second UTC unix timestamp."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _terminal_outcome(status: str) -> tuple[CallbackOutcome, str]:
    """Map an already-terminal challenge status to (outcome, update-log code)."""
    if status in (ChallengeStatus.CONSUMED_APPROVED.value, ChallengeStatus.CONSUMED_REJECTED.value):
        return CallbackOutcome.USED_TOKEN, "USED_TOKEN"
    if status == ChallengeStatus.EXPIRED.value:
        return CallbackOutcome.EXPIRED, "EXPIRED_TOKEN"
    # INVALIDATED (or any unknown terminal state): fail closed, no oracle.
    return CallbackOutcome.UNKNOWN_CHALLENGE, "UNKNOWN_TOKEN"


class ApprovalProcessor:
    """Processes one structured owner-approval callback (SPEC V3C).

    ``core`` provides the store + the ``_approve_work_in_transaction`` /
    ``_reject_work_in_transaction`` / ``_expire_and_release`` bridge helpers.
    ``identity_source`` supplies the expected owner user/chat ids (A3) and
    ``ux`` (optional) performs best-effort post-decision Telegram UI (A6).
    """

    def __init__(
        self,
        core: "Core",
        *,
        identity_source: OwnerIdentitySource,
        ux: Optional[PostDecisionUx] = None,
    ) -> None:
        self._core = core
        self._store = core._store
        self._identity_source = identity_source
        self._ux = ux if ux is not None else _NoopUx()

    # -- public API ---------------------------------------------------------

    def process_callback(
        self,
        *,
        action: CallbackAction,
        challenge: str,
        update_id: int,
        message_date: int,
        private_chat_identity: str,
        sender_identity: str,
        ref: Optional[str] = None,
    ) -> CallbackOutcome:
        """Process one callback.  Never raises (SPEC V3C §10/§14).

        Fail-closed order (SPEC V3C §5/§6.2): update-id/message-date well-
        formedness, identity (chat AND sender), parse action + challenge
        grammar, then challenge lookup / decision / read-only details.  Every
        terminal outcome (including MALFORMED / WRONG_CHAT / SPOOFED_SENDER) is
        persisted with a log row and advances the cursor exactly once; a
        replayed update is a no-op (DUPLICATE_UPDATE / STALE_UPDATE).
        """
        try:
            # (a) update-id must be a usable nonnegative integer (§6.2).
            #     The rejected terminal value (2**63-1) is still representable
            #     as the update-log PK, so it is persisted as MALFORMED with an
            #     exactly-once cursor advance (§14); truly unrepresentable
            #     values (>= 2**63, negative, non-int, bool) cannot be logged
            #     and stay a bare MALFORMED (no persisted row).
            if not self._valid_update_id(update_id):
                if self._representable_update_id(update_id):
                    return self._persist_terminal(
                        update_id, "MALFORMED",
                        chat_authorized=0, sender_authorized=0,
                    )
                return CallbackOutcome.MALFORMED

            # (b) message_date must be a bounded nonnegative integer
            #     (§10/§16.6).
            if not self._valid_message_date(message_date):
                return self._persist_terminal(
                    update_id, "MALFORMED", chat_authorized=0, sender_authorized=0
                )

            # (c) identity: BOTH sender and private chat (A3/§5), then the
            #     strict action + challenge grammar (A1).
            chat_ok, sender_ok = self._authorization_flags(
                sender_identity, private_chat_identity
            )
            parsed = (
                self._parse_action_challenge(action, challenge)
                if chat_ok and sender_ok
                else None
            )
            if not chat_ok:
                return self._persist_terminal(
                    update_id, "WRONG_CHAT",
                    chat_authorized=0,
                    sender_authorized=1 if sender_ok else 0,
                )
            if not sender_ok:
                return self._persist_terminal(
                    update_id, "SPOOFED_SENDER",
                    chat_authorized=1, sender_authorized=0,
                )
            if parsed is None:
                return self._persist_terminal(
                    update_id, "MALFORMED", chat_authorized=1, sender_authorized=1
                )
            action, challenge = parsed

            # (d) dispatch: details is read-only; approve/reject decide.
            if action is CallbackAction.DETAILS:
                if self.safe_details(challenge) is None:
                    return CallbackOutcome.UNKNOWN_CHALLENGE
                return CallbackOutcome.DETAILS
            return self._decide(action, challenge, update_id, message_date, ref)
        except Exception:
            # Absolute fail-closed: never crash the supervisor (SPEC V3C §10).
            return CallbackOutcome.ERROR

    def safe_details(self, challenge: str) -> Optional[dict]:
        """Read-only safe details payload (SPEC V3C §5/A1), or ``None``.

        The challenge stays ``ISSUED``, no expiry extension, no gate change, no
        approval/reject.  The payload carries only internal ids, a scope
        reference (never the raw scope) and the validity end — no raw token,
        no token hash, no chat/user ids (SPEC V3C §15).
        """
        if not isinstance(challenge, str) or _CHALLENGE_RE.match(challenge) is None:
            return None
        ch = self._store.get_challenge_by_token_hash(token_hash(challenge))
        if ch is None or ch["status"] != ChallengeStatus.ISSUED.value:
            return None
        if ch["expires_at"] <= self._store.now_iso():
            return None
        ap = self._store.get_approval(ch["approval_id"])
        if ap is None:
            return None
        recomputed = binding_hash(ap.task_id, ap.action, ap.scope)
        if (
            ap.binding_hash != recomputed
            or ch["binding_hash"] != recomputed
            or ch["task_id"] != ap.task_id
            or ch["approval_id"] != ap.id
        ):
            return None
        from .notifications import scope_ref

        return {
            "job_id": ch["supervisor_job_id"],
            "task_id": ch["task_id"],
            "gate_id": ch["approval_id"],
            "scope_ref": scope_ref(ch["binding_hash"]),
            "valid_until": ch["expires_at"],
        }

    # -- validation (pure, never raise) -------------------------------------

    @staticmethod
    def _valid_update_id(update_id) -> bool:
        """A ``update_id`` must be a nonnegative int that can be BOTH processed
        AND cursor-advanced: ``0 <= value <= _SQLITE_MAX_INT - 1``.

        The terminal value ``_SQLITE_MAX_INT`` (2**63-1) is representable as the
        ``telegram_update_log`` PK, but ``update_id + 1`` (2**63) overflows
        SQLite's signed 64-bit INTEGER range, so it is rejected here and handled
        as a persisted MALFORMED by :meth:`process_callback` (via
        :meth:`_representable_update_id`)."""
        return (
            isinstance(update_id, int)
            and not isinstance(update_id, bool)
            and 0 <= update_id < _SQLITE_MAX_INT
        )

    @staticmethod
    def _representable_update_id(update_id) -> bool:
        """True iff ``update_id`` is a proper nonnegative int that fits in
        SQLite's signed 64-bit INTEGER PK: ``0 <= value <= _SQLITE_MAX_INT``.

        Distinguishes the rejected terminal value ``_SQLITE_MAX_INT`` (which can
        still be persisted as a MALFORMED update-log row) from truly
        unrepresentable values (>= 2**63, negative, non-int, bool), which cannot
        be logged at all."""
        return (
            isinstance(update_id, int)
            and not isinstance(update_id, bool)
            and 0 <= update_id <= _SQLITE_MAX_INT
        )

    @staticmethod
    def _valid_message_date(message_date) -> bool:
        """A ``message_date`` must be a bounded nonnegative int within
        SQLite's signed 64-bit integer range (§10, F4)."""
        return (
            isinstance(message_date, int)
            and not isinstance(message_date, bool)
            and 0 <= message_date <= _SQLITE_MAX_INT
        )

    def _authorization_flags(self, sender_identity, private_chat_identity):
        """Return ``(chat_authorized, sender_authorized)`` booleans (0/1).

        A3/§5 single canonical owner: BOTH injected expected identities must
        resolve to the SAME canonical string (a provider whose values differ
        is a misconfiguration and fails closed ``(0, 0)``), and the presented
        sender AND private-chat identities must both equal that canonical
        value — with the private-chat-consistency constraint
        ``sender == chat`` (SPEC §5).  Never exposes the identities
        (A3/§5/§15); ``(0, 0)`` on any identity-provider or type failure.
        """
        try:
            expected_user = self._identity_source.expected_owner_user_id()
            expected_chat = self._identity_source.expected_owner_chat_id()
        except Exception:
            return (0, 0)
        if not expected_user or not expected_chat:
            return (0, 0)
        # Canonical single owner (F3): the two expected values must be equal.
        if expected_user != expected_chat:
            return (0, 0)
        if not isinstance(sender_identity, str) or not isinstance(private_chat_identity, str):
            return (0, 0)
        canonical = expected_user
        chat_ok = private_chat_identity == canonical
        # Sender must match the canonical owner AND the presented chat
        # (private-chat consistency, §5) — a mismatched pair never authorizes.
        sender_ok = sender_identity == canonical and sender_identity == private_chat_identity
        return (1 if chat_ok else 0, 1 if sender_ok else 0)

    @staticmethod
    def _parse_action_challenge(action, challenge):
        try:
            if isinstance(action, CallbackAction):
                act = action
            elif isinstance(action, str):
                act = CallbackAction(action)
            else:
                return None
        except ValueError:
            return None
        if not isinstance(challenge, str):
            return None
        if _CHALLENGE_RE.match(challenge) is None:
            return None
        return act, challenge

    @staticmethod
    def _is_lock_error(exc) -> bool:
        """True iff an exception is a SQLite busy/locked error (§14)."""
        code = getattr(exc, "sqlite_errorcode", None)
        if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return True
        return "locked" in str(exc).lower()

    # -- terminal (non-decision) persistence --------------------------------

    def _persist_terminal(
        self,
        update_id: int,
        outcome: str,
        *,
        chat_authorized: int,
        sender_authorized: int,
    ) -> CallbackOutcome:
        """Persist a non-decision terminal outcome (MALFORMED / WRONG_CHAT /
        SPOOFED_SENDER / STALE_UPDATE) and advance the cursor in one short
        ``BEGIN IMMEDIATE`` transaction (SPEC V3C §14).

        The persisted row carries ONLY the authorization booleans and the
        allowlisted outcome — never real identities, no message_date, no token
        lookup, no gate mutation (§8.2).  Idempotent on ``update_id`` (PK) and
        monotonic on the cursor: a duplicate (already logged) or below-cursor
        update is a no-op that never re-advances.  Never raises.
        """
        try:
            with self._store._inbound_transaction() as store:
                now = store.now_iso()
                cur = store.get_inbound_state()
                next_id = cur["next_update_id"] if cur is not None else 0
                if update_id < next_id:
                    # Below the cursor: stale no-op (§6.2), persisted only if
                    # the PK is still free.
                    row = self._update_log_row(
                        update_id, message_date=None,
                        chat_authorized=chat_authorized,
                        sender_authorized=sender_authorized,
                        outcome="STALE_UPDATE", now=now,
                    )
                    if not store._insert_update_log(row):
                        return CallbackOutcome.DUPLICATE_UPDATE
                    return CallbackOutcome.STALE_UPDATE
                row = self._update_log_row(
                    update_id, message_date=None,
                    chat_authorized=chat_authorized,
                    sender_authorized=sender_authorized,
                    outcome=outcome, now=now,
                )
                if not store._insert_update_log(row):
                    return CallbackOutcome.DUPLICATE_UPDATE
                self._advance_cursor(store, update_id, now)
                return CallbackOutcome(outcome)
        except sqlite3.OperationalError as exc:
            if self._is_lock_error(exc):
                return CallbackOutcome.LOCKED
            return CallbackOutcome.ERROR
        except Exception:
            return CallbackOutcome.ERROR

    # -- decision -----------------------------------------------------------

    def _decide(
        self,
        action: CallbackAction,
        challenge: str,
        update_id: int,
        message_date: int,
        ref: Optional[str],
    ) -> CallbackOutcome:
        approve = action is CallbackAction.APPROVE
        decision = "APPROVE" if approve else "REJECT"
        tok_hash = token_hash(challenge)
        outcome = CallbackOutcome.ERROR
        try:
            with self._store._inbound_transaction() as store:
                outcome = self._decide_in_transaction(
                    store, approve, decision, tok_hash, update_id, message_date
                )
        except sqlite3.OperationalError as exc:
            # Immediate termination on DB lock (SPEC V3C §14): a writer lock
            # aborts the pass now, leaving cursor and gate untouched.
            if self._is_lock_error(exc):
                return CallbackOutcome.LOCKED
            return CallbackOutcome.ERROR
        except Exception:
            # Rolled back by _inbound_transaction; structured failure, never crash.
            return CallbackOutcome.ERROR
        # Post-decision UX runs ONLY after the decision has committed (A6).
        if outcome in (CallbackOutcome.APPROVED, CallbackOutcome.REJECTED):
            self._run_post_decision_ux(ref, approved=(outcome is CallbackOutcome.APPROVED))
        return outcome

    def _decide_in_transaction(
        self, store, approve: bool, decision: str, tok_hash: str,
        update_id: int, message_date: int,
    ) -> CallbackOutcome:
        now = store.now_iso()
        now_unix = _iso_to_unix(now)

        # 0. Stale-update guard (SPEC V3C §6.2): every update below the
        #    persisted cursor is an old no-op.  Persisted as STALE_UPDATE (or
        #    DUPLICATE_UPDATE when already logged); no challenge lookup, no
        #    consumption, no gate mutation, no cursor change.
        cursor = store.get_inbound_state()
        next_id = cursor["next_update_id"] if cursor is not None else 0
        if update_id < next_id:
            row = self._update_log_row(
                update_id, message_date=message_date, chat_authorized=1,
                sender_authorized=1, outcome="STALE_UPDATE", now=now,
            )
            if not store._insert_update_log(row):
                return CallbackOutcome.DUPLICATE_UPDATE
            return CallbackOutcome.STALE_UPDATE

        # 1. Update dedup CAS: the PK insert is the authoritative gate.
        if not self._insert_processing(store, update_id, message_date, now):
            return CallbackOutcome.DUPLICATE_UPDATE

        # 2. Challenge lookup by full token hash only (A2).
        ch = store.get_challenge_by_token_hash(tok_hash)
        if ch is None:
            store._finalize_update_log(
                update_id, decision=None, challenge_id=None, approval_id=None,
                outcome="UNKNOWN_TOKEN", processed_at=now,
            )
            self._advance_cursor(store, update_id, now)
            return CallbackOutcome.UNKNOWN_CHALLENGE
        cid = ch["id"]

        # 3. Challenge must be ISSUED (terminal states never reopen).
        if ch["status"] != ChallengeStatus.ISSUED.value:
            outcome, log_outcome = _terminal_outcome(ch["status"])
            store._finalize_update_log(
                update_id, decision=None, challenge_id=cid,
                approval_id=ch["approval_id"], outcome=log_outcome, processed_at=now,
            )
            self._advance_cursor(store, update_id, now)
            return outcome

        # 4. Challenge expiry (A2: min(now+3600, approval.expires_at)).
        if ch["expires_at"] <= now:
            store._mark_challenge_expired(cid, now)
            store._finalize_update_log(
                update_id, decision=None, challenge_id=cid, approval_id=None,
                outcome="EXPIRED_TOKEN", processed_at=now,
            )
            self._advance_cursor(store, update_id, now)
            return CallbackOutcome.EXPIRED

        # 5. Message-date window (SPEC V3C §10.2 step 4 / §16.6): the message
        #    time must lie in the strict window [created_at, expires_at) with a
        #    bounded future-clock-skew allowance.  Post-expiry is checked
        #    BEFORE the future-skew check (F2), so a message at/past the expiry
        #    boundary atomically expires the challenge and never leaves it
        #    ISSUED, even when it is also implausibly far in the future.
        created_unix = _iso_to_unix(ch["created_at"])
        expires_unix = _iso_to_unix(ch["expires_at"])
        if message_date < created_unix:
            # Pre-challenge (old) message: stale, no consumption (§16.6).
            store._finalize_update_log(
                update_id, decision=None, challenge_id=cid, approval_id=None,
                outcome="STALE_MESSAGE", processed_at=now,
            )
            self._advance_cursor(store, update_id, now)
            return CallbackOutcome.STALE_MESSAGE
        if message_date >= expires_unix:
            # Post-expiry (boundary inclusive): atomically expire (F2).
            store._mark_challenge_expired(cid, now)
            store._finalize_update_log(
                update_id, decision=None, challenge_id=cid, approval_id=None,
                outcome="EXPIRED_TOKEN", processed_at=now,
            )
            self._advance_cursor(store, update_id, now)
            return CallbackOutcome.EXPIRED
        if message_date > now_unix + FUTURE_SKEW_BOUND:
            # Implausibly-future message: stale, no consumption (§16.6).
            store._finalize_update_log(
                update_id, decision=None, challenge_id=cid, approval_id=None,
                outcome="STALE_MESSAGE", processed_at=now,
            )
            self._advance_cursor(store, update_id, now)
            return CallbackOutcome.STALE_MESSAGE

        # 6. Approval must exist and its persisted gate content must be
        #    unchanged (recompute the full binding hash, fail-closed).
        ap = store.get_approval(ch["approval_id"])
        if ap is None:
            store._mark_challenge_invalidated(cid, now)
            store._finalize_update_log(
                update_id, decision=None, challenge_id=cid, approval_id=None,
                outcome="BINDING_MISMATCH", processed_at=now,
            )
            self._advance_cursor(store, update_id, now)
            return CallbackOutcome.GATE_CHANGED
        recomputed = binding_hash(ap.task_id, ap.action, ap.scope)
        if (
            ap.binding_hash != recomputed
            or ch["binding_hash"] != recomputed
            or ch["task_id"] != ap.task_id
            or ch["approval_id"] != ap.id
        ):
            store._mark_challenge_invalidated(cid, now)
            store._finalize_update_log(
                update_id, decision=None, challenge_id=cid, approval_id=ap.id,
                outcome="BINDING_MISMATCH", processed_at=now,
            )
            self._advance_cursor(store, update_id, now)
            return CallbackOutcome.GATE_CHANGED

        # 7. Gate must still be pending (no reopen of decided gates).
        if ap.status is not ApprovalStatus.PENDING:
            store._finalize_update_log(
                update_id, decision=None, challenge_id=cid, approval_id=ap.id,
                outcome="APPROVAL_NOT_PENDING", processed_at=now,
            )
            self._advance_cursor(store, update_id, now)
            return CallbackOutcome.GATE_DECIDED

        # 8. Approval expiry (A7/§12): reject also fails on expiry; run the
        #    release path on the SAME inbound connection, record no decision.
        if ap.expires_at <= now:
            self._expire_and_release_on(store, ap.id)
            store._finalize_update_log(
                update_id, decision=None, challenge_id=cid, approval_id=ap.id,
                outcome="EXPIRED_APPROVAL", processed_at=now,
            )
            self._advance_cursor(store, update_id, now)
            return CallbackOutcome.EXPIRED

        # 9. Challenge CAS (single-use, terminal).
        target = (
            ChallengeStatus.CONSUMED_APPROVED.value
            if approve
            else ChallengeStatus.CONSUMED_REJECTED.value
        )
        if store._consume_challenge(cid, target, now, update_id) != 1:
            store._finalize_update_log(
                update_id, decision=None, challenge_id=cid, approval_id=ap.id,
                outcome="USED_TOKEN", processed_at=now,
            )
            self._advance_cursor(store, update_id, now)
            return CallbackOutcome.USED_TOKEN

        # 10. Approval decision on the SAME inbound connection (no nested BEGIN).
        if approve:
            self._approve_decision_on(store, ap)
        else:
            self._reject_decision_on(store, ap)

        # 11. Stable idempotency key (SPEC V3C §10.2 step 14).
        key = f"telegram-owner-approval:{update_id}:{cid}:{decision}"
        args_hash = _hash_json(
            {"update_id": update_id, "challenge_id": cid,
             "decision": decision, "approval_id": ap.id}
        )
        store._set_command_idempotency(
            key, "telegram_owner_decision", ap.id, args_hash, now
        )

        # 12. Terminal update outcome + cursor (SPEC V3C §10.2 steps 16/17).
        store._finalize_update_log(
            update_id, decision=decision, challenge_id=cid, approval_id=ap.id,
            outcome=("APPROVED" if approve else "REJECTED"), processed_at=now,
        )
        self._advance_cursor(store, update_id, now)
        return CallbackOutcome.APPROVED if approve else CallbackOutcome.REJECTED

    # -- decision bridge (connection-scoped Core helpers) -------------------
    # Replicates the Core approve/reject/expire bridge bodies against the
    # explicit inbound connection (F1), so the approval CAS, the event
    # emission and the task transition commit in the SAME ``BEGIN IMMEDIATE``
    # as the challenge CAS and cursor advance.  ``Core._store._conn`` is never
    # touched; binding/status/expiry were already verified in steps 6-8 above.

    def _emit_event(self, store, type_, *, task_id=None, role=None, state=None,
                    payload=None):
        ev = Event(
            id=str(uuid4()), type=type_, task_id=task_id, role=role,
            state=state, payload=payload or {}, created_at=store.now_iso(),
        )
        store._insert_event(ev)
        return ev

    def _apply_transition_on(self, store, task, to_state, new_resume) -> None:
        store._update_task_state(task.id, to_state, new_resume, store.now_iso())
        self._emit_event(
            store, "task.state_changed", task_id=task.id, state=to_state.value,
            payload={"from_state": task.state.value, "to_state": to_state.value},
        )
        if to_state is TaskState.DONE:
            self._emit_event(store, "task.completed", task_id=task.id,
                             state=TaskState.DONE.value, payload={})

    def _approve_decision_on(self, store, ap) -> None:
        now = store.now_iso()
        rc = store._mark_approved(ap.id, now)
        if rc == 0:
            raise ApprovalError(f"approval {ap.id!r} could not be approved")
        self._emit_event(store, "gate.owner_approved", task_id=ap.task_id,
                         payload={"approval_id": ap.id})

    def _reject_decision_on(self, store, ap) -> None:
        now = store.now_iso()
        rc = store._mark_rejected(ap.id, now)
        if rc == 0:
            ap2 = store.get_approval(ap.id)
            raise ApprovalError(
                f"approval {ap.id!r} is not pending ({ap2.status.value})"
            )
        task = store.get_task(ap.task_id)
        if task is not None and task.state is TaskState.OWNER_APPROVAL_REQUIRED:
            state_machine.validate_transition(
                task.state, TaskState.BLOCKED, task.resume_state
            )
            self._apply_transition_on(store, task, TaskState.BLOCKED, None)
        self._emit_event(store, "gate.owner_rejected", task_id=ap.task_id,
                         payload={"approval_id": ap.id})

    def _expire_and_release_on(self, store, approval_id) -> None:
        now = store.now_iso()
        store._mark_expired(approval_id, now)
        ap = store.get_approval(approval_id)
        if ap is None:
            return
        task = store.get_task(ap.task_id)
        if task is None or task.state is not TaskState.OWNER_APPROVAL_REQUIRED:
            return
        resume = task.resume_state
        if resume is not None and is_valid_resume_target(resume):
            state_machine.validate_transition(task.state, resume, task.resume_state)
            self._apply_transition_on(store, task, resume, None)
        else:
            state_machine.validate_transition(
                task.state, TaskState.BLOCKED, task.resume_state
            )
            self._apply_transition_on(store, task, TaskState.BLOCKED, None)

    # -- helpers ------------------------------------------------------------

    def _update_log_row(
        self, update_id: int, *, message_date, chat_authorized: int,
        sender_authorized: int, outcome: str, now: str,
    ) -> dict:
        """Build a telegram_update_log row with no secrets and no identities."""
        return {
            "update_id": update_id,
            "message_date": message_date,
            "chat_authorized": chat_authorized,
            "sender_authorized": sender_authorized,
            "decision": None,
            "challenge_id": None,
            "approval_id": None,
            "outcome": outcome,
            "received_at": now,
            "processed_at": now,
        }

    def _insert_processing(self, store, update_id: int, message_date: int,
                           now: str) -> bool:
        row = self._update_log_row(
            update_id, message_date=message_date, chat_authorized=1,
            sender_authorized=1, outcome="PROCESSING", now=now,
        )
        return store._insert_update_log(row)

    def _advance_cursor(self, store, update_id: int, now: str) -> None:
        cur = store.get_inbound_state()
        current = cur["next_update_id"] if cur is not None else 0
        # Never persist a cursor outside SQLite's signed 64-bit INTEGER range:
        # for the rejected terminal value _SQLITE_MAX_INT the advance
        # (update_id + 1 == 2**63) would overflow, so saturate at
        # _SQLITE_MAX_INT.  Replay dedup at that terminal value relies on the
        # update-log PK (a replayed _SQLITE_MAX_INT is a DUPLICATE_UPDATE).
        store._set_inbound_state(
            max(current, min(update_id + 1, _SQLITE_MAX_INT)), now
        )

    def _run_post_decision_ux(self, ref: Optional[str], approved: bool) -> None:
        """Best-effort, non-authoritative post-decision UI (A6).

        Every call is swallowed; a UI failure never rolls back the committed
        decision and never alters gate state.
        """
        ux = self._ux
        for fn in (
            lambda: ux.answer_callback_query(ref),
            lambda: ux.edit_approval_message(ref, decided=approved),
            lambda: ux.remove_buttons(ref),
        ):
            try:
                fn()
            except Exception:
                pass
