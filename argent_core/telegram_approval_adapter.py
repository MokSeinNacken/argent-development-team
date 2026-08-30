"""Phase 3C-B1 — transport-neutral OpenClaw Telegram approval adapter.

A THIN, transport-neutral layer between a future OpenClaw interactive-handler
host (Phase 3C-B2) and the VERIFIED Phase 3C-A approval core
(:class:`argent_core.approval_processor.ApprovalProcessor`).  It reuses the
existing processor interface and reimplements NOTHING: no challenge lookup, no
binding check, no expiry, no gate CAS, no identity authorization, no dedup.

Design invariants (SPEC V3C §4/§5/§10/§14, owner amendments A1/A3/A6/A7):

- **Structured callbacks only.**  The adapter accepts exactly one shape of
  input: an ``A:``/``R:``/``D:`` + 43-character opaque challenge payload
  (already split off the ``argent:`` namespace — see :func:`split_namespace`).
  Anything else — free text, ``APPROVE <token>``, unknown actions, malformed
  lengths/charsets — is rejected fail-closed BEFORE the core and is NEVER
  interpreted as text or a prompt.
- **Host contract is explicit and fail-closed (F1).**  ``update_id`` and
  ``message_date`` are REQUIRED nonnegative ``int`` fields forwarded verbatim
  from the original Telegram update.  The adapter never synthesizes them and
  never substitutes ``callback_id`` for ``update_id``.  A host that cannot
  supply them yields :class:`AdapterOutcome.HOST_CONTRACT_VIOLATION` with no
  core call and no ledger change.
- **Host-boundary honesty (F1).**  This module is written against the TARGET
  contract — a host that supplies ``update_id`` and ``message_date`` verbatim.
  The currently installed OpenClaw handler context
  (``TelegramInteractiveHandlerContext``) does NOT yet expose either field;
  Phase 3C-B2 (owner-gated) must provide them via a supported OpenClaw ingress
  extension that forwards the original ``update_id`` and callback-message
  ``date``.  This module performs no OpenClaw modification.
- **Reference mapping (F2).**  ``callback_ref`` (the callback-query id) is the
  ONLY reference for ``answer_callback_query``; ``message_ref`` (the
  originating message id) is the target for ``edit_approval_message`` and
  ``remove_buttons``.  Both references are forwarded explicitly and
  observably — they are NOT interchangeable.  When ``message_ref`` is ``None``,
  edit/remove are SKIPPED (no fake target); only ``answer_callback_query``
  runs.
- **No network.**  This module performs no Telegram API call, no socket, no
  HTTP, no ``getUpdates``, no poller.  The OpenClaw gateway remains the SOLE
  owner of the Telegram update stream (A4).
- **Total.**  :meth:`TelegramApprovalAdapter.handle_callback` never raises;
  every exception becomes a fail-closed outcome (ERROR).  It cannot crash or
  block the supervisor.
- **Post-decision UX is best-effort (A6).**  The adapter owns the
  :class:`PostDecisionUx` wiring: ``answer_callback_query`` /
  ``edit_approval_message`` / ``remove_buttons`` run ONLY after the decision
  has committed, and every UI failure is swallowed — never a rollback, never a
  gate change.  In 3C-B1 the UX is a recording-only :class:`FakePostDecisionUx`.
- **No secrets (§15).**  No real identity, token or credential appears in this
  module, its outcomes or its logs; dummy identities live only in tests.

This module has NO OpenClaw import and no dependency on OpenClaw internals.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional, Protocol

from .approval_core import CallbackAction, parse_callback
from .approval_processor import (
    CallbackOutcome,
    PostDecisionUx,
)

# The OpenClaw plugin interactive-handler namespace this adapter is registered
# under (per docs/PHASE3CB1_PROOF.md): Telegram ``callback_data`` is
# ``argent:<payload>`` and the plugin splits ``namespace:payload`` BEFORE the
# adapter is invoked, so the adapter sees ``payload`` only (e.g. ``A:<43>``).
CALLBACK_NAMESPACE = "argent"


def split_namespace(callback_data: str) -> Optional[str]:
    """Split a raw Telegram ``callback_data`` ``namespace:payload`` and return
    the payload AFTER the ``argent:`` namespace, or ``None`` when the namespace
    does not match (or the input is not a string).

    Pure string handling — no OpenClaw, no network.  A non-``argent`` namespace
    is NOT an approval callback and must never reach the adapter.  The adapter
    then receives only ``payload`` (e.g. ``A:<challenge>``).
    """
    if not isinstance(callback_data, str):
        return None
    prefix = CALLBACK_NAMESPACE + ":"
    if not callback_data.startswith(prefix):
        return None
    return callback_data[len(prefix):]


class AdapterOutcome(str, Enum):
    """Fail-closed outcomes produced ONLY at the adapter's host boundary.

    These are NOT core outcomes — the Phase-3C-A :class:`CallbackOutcome` is
    untouched.  ``HOST_CONTRACT_VIOLATION`` is the fail-closed result when the
    host fails to supply a REQUIRED security field (``update_id`` /
    ``message_date``).  The adapter returns it WITHOUT invoking the processor
    and WITHOUT synthesizing a value, so the host-contract gap is observable
    and is never confused with payload ``MALFORMED``.
    """

    HOST_CONTRACT_VIOLATION = "HOST_CONTRACT_VIOLATION"


def _valid_host_int(value) -> bool:
    """True iff ``value`` is a usable host-supplied nonnegative integer.

    Enforces the adapter's host contract (F1) at the boundary: ``update_id``
    and ``message_date`` are REQUIRED nonnegative ``int`` (never ``bool``,
    never ``None``, never synthesized).  This is a boundary check only — it
    does not reimplement the core's decision logic; the core still performs its
    own full range/well-formedness validation.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class ApprovalCallbackHost(Protocol):
    """Minimal host contract the Phase-3C-B2 OpenClaw interactive handler must
    deliver (pure Python Protocol — no OpenClaw imports).

    The host receives a Telegram inline-button ``callback_query`` whose
    ``callback_data`` is ``argent:<payload>`` and, AFTER the namespace split,
    delivers ONLY the following structured fields.  Normal Telegram messages
    and non-``argent`` callbacks never reach this surface.

    **REQUIRED security fields (F1).**  ``update_id`` and ``message_date`` are
    REQUIRED nonnegative ``int`` values.  They come from the ORIGINAL Telegram
    update (``update.update_id`` and the callback-message date); the host must
    forward them verbatim and MUST NOT synthesize them and MUST NOT substitute
    ``callback_id`` for ``update_id``.  A host that cannot supply them must not
    call the adapter — the adapter fails closed with
    :class:`AdapterOutcome.HOST_CONTRACT_VIOLATION` if they are missing.

    **Host-boundary honesty (F1).**  This is the TARGET contract: the currently
    installed OpenClaw handler context does NOT yet expose ``update_id`` or
    ``message_date``; Phase 3C-B2 (owner-gated) must provide them via a
    supported OpenClaw ingress extension forwarding the original ``update_id``
    and callback-message ``date``.
    """

    def deliver_callback(
        self,
        *,
        payload: str,
        sender_identity: str,
        private_chat_identity: str,
        update_id: int,
        message_date: int,
        callback_ref: str,
        message_ref: Optional[str] = None,
    ) -> CallbackOutcome | AdapterOutcome: ...


class FakePostDecisionUx:
    """Recording-only post-decision UX for Phase 3C-B1 (NO real Telegram API).

    Records every call in ``self.calls`` and can be scripted to raise on any
    individual call to prove (A6) that a UI failure never rolls back the
    already-committed decision.

    Reference mapping (F2): ``answer_callback_query`` records ``callback_ref``
    (the callback-query id); ``edit_approval_message`` / ``remove_buttons``
    record ``message_ref`` (the originating message id).  The recorded tuple
    makes each reference observable.
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


class TelegramApprovalAdapter:
    """Transport-neutral adapter between a structured-callback host and the
    Phase 3C-A :class:`ApprovalProcessor`.

    ``processor`` is the VERIFIED Phase 3C-A processor (the decision core).  It
    must be constructed WITHOUT a ``ux`` — the adapter OWNS the post-decision
    UX wiring (A6); wiring the same UX into both would run it twice.  ``ux`` is
    the best-effort :class:`PostDecisionUx` (default :class:`FakePostDecisionUx`).
    """

    def __init__(
        self,
        processor,
        *,
        ux: Optional[PostDecisionUx] = None,
    ) -> None:
        self._processor = processor
        self._ux = ux if ux is not None else FakePostDecisionUx()

    def handle_callback(
        self,
        *,
        payload: str,
        sender_identity: str,
        private_chat_identity: str,
        update_id: int | None = None,
        message_date: int | None = None,
        callback_ref: str,
        message_ref: Optional[str] = None,
    ) -> CallbackOutcome | AdapterOutcome:
        """Process ONE structured approval callback.  TOTAL — never raises.

        Fail-closed order:

        0. **Host-contract boundary (F1).**  ``update_id`` and ``message_date``
           are REQUIRED nonnegative ``int`` values.  If either is missing or
           unusable, the adapter returns
           :class:`AdapterOutcome.HOST_CONTRACT_VIOLATION` WITHOUT calling the
           core and WITHOUT synthesizing a value.  ``message_date`` is required
           for DETAILS too: the core validates it for every action, and the
           adapter never fabricates one.
        1. Parse ``payload`` into ``(CallbackAction, challenge)`` via the core
           ``parse_callback`` (strict ``[ARD]:<43-char challenge>``).  Anything
           that does not match — free text, unknown action, wrong length,
           wrong charset — returns ``MALFORMED`` WITHOUT calling the core and
           without any ledger change.
        2. Map the structured fields to the core processor and delegate the
           decision (identity authorization, dedup, challenge lookup, binding,
           expiry, gate CAS, cursor advance) — never reimplemented here.
        3. After a committed APPROVED/REJECTED decision, trigger the
           best-effort post-decision UX (A6) — failures swallowed, no rollback.

        Reference mapping (F2): ``callback_ref`` (the callback-query id) is the
        reference for ``answer_callback_query``; ``message_ref`` (the
        originating message id) is the reference for ``edit_approval_message``
        and ``remove_buttons``.
        """
        try:
            # Fail-closed host-contract boundary: never proceed, never
            # synthesize when the REQUIRED security fields are absent/unusable.
            if not _valid_host_int(update_id) or not _valid_host_int(message_date):
                return AdapterOutcome.HOST_CONTRACT_VIOLATION
            parsed = parse_callback(payload)
            if parsed is None:
                # Fail-closed before the core: no core call, no ledger change.
                return CallbackOutcome.MALFORMED
            action, challenge = parsed
            outcome = self._processor.process_callback(
                action=action,
                challenge=challenge,
                update_id=update_id,
                message_date=message_date,
                private_chat_identity=private_chat_identity,
                sender_identity=sender_identity,
                ref=callback_ref,
            )
            if outcome in (CallbackOutcome.APPROVED, CallbackOutcome.REJECTED):
                self._run_post_decision_ux(
                    callback_ref,
                    message_ref,
                    approved=(outcome is CallbackOutcome.APPROVED),
                )
            return outcome
        except Exception:
            # Absolute fail-closed: never crash the supervisor (SPEC V3C §10).
            return CallbackOutcome.ERROR

    def _run_post_decision_ux(
        self, callback_ref: str, message_ref: Optional[str], approved: bool,
    ) -> None:
        """Best-effort, non-authoritative post-decision UI (A6).

        Reference mapping (F2): ``answer_callback_query`` answers the callback
        query with ``callback_ref`` (the callback-query id); ``edit_approval_message``
        and ``remove_buttons`` target the originating message with ``message_ref``
        (the message id).  When ``message_ref`` is ``None`` there is no message
        target, so ``edit_approval_message`` and ``remove_buttons`` are SKIPPED
        (never called with a fake/None target) — only ``answer_callback_query``
        runs.  Every call is swallowed; a UI failure never rolls back the
        committed decision and never alters gate state.
        """
        ux = self._ux
        try:
            ux.answer_callback_query(callback_ref)
        except Exception:
            pass
        if message_ref is None:
            # No originating message target: edit/remove are skipped entirely.
            return
        for fn in (
            lambda: ux.edit_approval_message(message_ref, decided=approved),
            lambda: ux.remove_buttons(message_ref),
        ):
            try:
                fn()
            except Exception:
                pass


# ``deliver_callback`` is the Protocol method name; the adapter satisfies
# ApprovalCallbackHost structurally via ``handle_callback``.
TelegramApprovalAdapter.deliver_callback = TelegramApprovalAdapter.handle_callback


def dispatch_callback(
    adapter: TelegramApprovalAdapter,
    *,
    payload: str,
    sender_id: str,
    chat_id: str,
    update_id: int | None = None,
    message_date: int | None = None,
    callback_ref: str,
    message_ref: Optional[str] = None,
) -> CallbackOutcome | AdapterOutcome:
    """Host-delivery entry — mirrors the OpenClaw interactive-handler call that
    Phase 3C-B2 will wire.

    The OpenClaw plugin receives ``callback_data`` == ``argent:<payload>`` and,
    AFTER the namespace split (see :func:`split_namespace`), forwards ``payload``
    here together with the sender id (``from.id``), the private chat id
    (``message.chat.id``), the Telegram ``update_id`` (REQUIRED, verbatim), the
    callback-message ``message_date`` (REQUIRED, verbatim), the callback-query
    id (``callback_ref``) and the originating message id (``message_ref``).  No
    OpenClaw import, no network.  ``update_id`` / ``message_date`` are never
    synthesized here — a host that cannot supply them must not call this (the
    adapter fails closed with ``HOST_CONTRACT_VIOLATION``).
    """
    return adapter.handle_callback(
        payload=payload,
        sender_identity=sender_id,
        private_chat_identity=chat_id,
        update_id=update_id,
        message_date=message_date,
        callback_ref=callback_ref,
        message_ref=message_ref,
    )


__all__ = [
    "CALLBACK_NAMESPACE",
    "AdapterOutcome",
    "ApprovalCallbackHost",
    "FakePostDecisionUx",
    "TelegramApprovalAdapter",
    "dispatch_callback",
    "split_namespace",
]
