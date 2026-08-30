# Phase 3C-B1 — OpenClaw Telegram Approval Adapter (Implementation Notes)

> **Status:** implementation notes only. This file is **not** part of the
> binding specification. The binding document is `docs/SPEC_V3C.md`
> (VERDICT: `SPEC_COMPLETE`) and its owner amendments A1–A7 remain unchanged
> and authoritative. The Phase-3C-A core
> (`argent_core/approval_core.py`, `argent_core/approval_processor.py`) is
> VERIFIED and is NOT modified or security-weakened here — the adapter is a
> THIN layer on top. `docs/PHASE3CB1_PROOF.md` is the technical proof that
> integration path A (OpenClaw plugin interactive-handler) is possible.

Scope of this round: implement the transport-neutral adapter and its offline
tests. **No live inbound, no commit, no push.**

---

## 1. Implemented scope

Phase 3C-B1 adds exactly one module and its tests:

- `argent_core/telegram_approval_adapter.py` — a transport-neutral adapter
  between a future OpenClaw interactive-handler host (Phase 3C-B2) and the
  VERIFIED Phase 3C-A `ApprovalProcessor`. It reuses the existing processor
  interface and reimplements NOTHING (no challenge lookup, no binding check,
  no expiry, no gate CAS, no identity authorization, no dedup).
- `tests/test_phase3cb1_adapter.py` — 44 deterministic offline tests
  (temp DB, fake clock, fake identity source, recording-only UX, no network,
  no real Telegram).

## 2. Module layout

| Component | Responsibility |
| --- | --- |
| `CALLBACK_NAMESPACE = "argent"` | The OpenClaw plugin interactive-handler namespace. |
| `split_namespace(callback_data)` | Pure string split of `argent:<payload>` → `payload`; non-`argent` / non-string → `None`. No OpenClaw, no network. |
| `ApprovalCallbackHost(Protocol)` | Minimal host contract the future Phase-3C-B2 plugin must satisfy. Pure Python `Protocol`, no OpenClaw imports. |
| `FakePostDecisionUx` | Recording-only post-decision UX (3C-B1). No real Telegram API. |
| `TelegramApprovalAdapter` | Thin adapter; single entry `handle_callback(...)` → `CallbackOutcome | AdapterOutcome`; total (never raises); owns the post-decision UX wiring (A6). |
| `dispatch_callback(adapter, ...)` | Host-delivery entry mirroring the OpenClaw interactive-handler call (used by tests and by future Phase-3C-B2 plugin wiring). |

## 3. Host contract (`ApprovalCallbackHost`)

The future OpenClaw plugin interactive handler (registered for namespace
`argent`) receives a Telegram inline-button `callback_query` whose
`callback_data` is `argent:<payload>`. After the namespace split it must
deliver ONLY the following structured fields to the adapter — never free
text, never the raw `callback_data`, never prompts:

```
deliver_callback(
    *,
    payload: str,                 # e.g. "A:<43-char challenge>" (post-split)
    sender_identity: str,         # from.id (canonical string)
    private_chat_identity: str,   # message.chat.id (canonical string)
    update_id: int,               # update.update_id (REQUIRED, verbatim)
    message_date: int,            # callback.message.date (REQUIRED, verbatim)
    callback_ref: str,            # callback.id  (callback-query id)
    message_ref: str | None,      # callback.message.message_id (may be None)
) -> CallbackOutcome | AdapterOutcome
```

Return type is the union `CallbackOutcome | AdapterOutcome`: `AdapterOutcome`
covers host-contract violations at the adapter boundary (e.g.
`HOST_CONTRACT_VIOLATION` when `update_id`/`message_date` are omitted or
unusable); `CallbackOutcome` covers all core decision outcomes.

`TelegramApprovalAdapter.handle_callback(...)` is the concrete implementation
(and `deliver_callback` is an alias, so the adapter structurally satisfies
`ApprovalCallbackHost`).

### 3a. Host-boundary honesty (F1)

The adapter is written against a **TARGET contract**: `update_id` and
`message_date` are REQUIRED nonnegative `int` fields forwarded verbatim from
the original Telegram update; the adapter never synthesizes them and never
substitutes `callbackId` for `update_id` (a host that cannot supply them
returns `HOST_CONTRACT_VIOLATION`, with no core call and no ledger change).

The **currently installed** OpenClaw handler context
(`TelegramInteractiveHandlerContext`) does **NOT** yet expose `update_id` or
`message_date` — it provides `callbackId`, `senderId`, `chatId`, `messageId`,
`callback.data/namespace/payload`, and
`respond.reply/editMessage/editButtons/clearButtons/deleteMessage`, but no
Telegram `update_id` and no callback-message `date`. Phase 3C-B2 (owner-gated)
MUST provide them via a supported OpenClaw ingress extension that forwards the
original `update_id` and callback-message `date`; no OpenClaw modification is
made in this phase.

## 4. Field mapping (host field → core parameter)

| Host / OpenClaw field | `dispatch_callback` arg | adapter `handle_callback` arg | Phase-3C-A core parameter |
| --- | --- | --- | --- |
| `callback_data` `argent:<payload>` (namespace split first) | `payload` | `payload` | → `parse_callback(payload)` → `action`, `challenge` |
| `from.id` (`senderId`) | `sender_id` | `sender_identity` | `sender_identity` |
| `message.chat.id` (`chatId`) | `chat_id` | `private_chat_identity` | `private_chat_identity` |
| `update.update_id` | `update_id` | `update_id` | `update_id` |
| `callback.id` (`callbackId`) | `callback_ref` | `callback_ref` | `ref` (callback-query id) |
| `callback.message.message_id` (`messageId`) | `message_ref` | `message_ref` | *(not a core parameter — carried for the future real UX edit/remove)* |
| `callback.message.date` | `message_date` | `message_date` | `message_date` |

The adapter performs NO authorization, dedup or decision itself. It only
parses the payload via the core `parse_callback` (strict `[ARD]:<43-char
challenge>`) and forwards the structured fields to
`ApprovalProcessor.process_callback(...)`, which does all the security work.

## 5. Namespace split (`argent:<payload>`)

Per `docs/PHASE3CB1_PROOF.md`, the OpenClaw plugin interactive handler is
registered for namespace `argent` and matches `namespace:payload`. The raw
Telegram `callback_data` is therefore `argent:<payload>`
(e.g. `argent:A:<challenge>`), and the adapter receives **the payload AFTER
the split** (`A:<challenge>`). `split_namespace()` documents this split in
pure string form; the adapter itself has no OpenClaw import and no awareness
of the raw callback data.

## 6. Post-decision UX (A6) — `FakePostDecisionUx`

The adapter OWNS the post-decision UX wiring. After
`ApprovalProcessor.process_callback(...)` returns `APPROVED`/`REJECTED` (the
decision has already committed), the adapter calls, best-effort:

1. `answer_callback_query(callback_ref)`   — `callback_ref` (callback-query id)
2. `edit_approval_message(message_ref, decided=...)`  — `message_ref` (message id)
3. `remove_buttons(message_ref)`           — `message_ref` (message id)

Reference mapping (F2/F3): `callback_ref` is the ONLY reference for
`answer_callback_query`; `message_ref` is the target for
`edit_approval_message`/`remove_buttons`. When `message_ref` is `None`, steps 2
and 3 are SKIPPED (no fake target) — only `answer_callback_query` runs.

Every call is swallowed; a UI failure NEVER rolls back the persisted decision
and NEVER alters gate state (A6). In 3C-B1 the UX is a
`FakePostDecisionUx` that only records calls — there is no real Telegram API
and no `answerCallbackQuery` responder on the installed handler context (the
gateway answers the callback before plugin dispatch). The adapter's processor
must be constructed WITHOUT a `ux` (the adapter owns it; wiring the same UX
into both would run it twice).

## 7. What does NOT exist yet (explicit)

There is **NO** live inbound, **NO** `getUpdates` caller, **NO** second
poller, **NO** webhook, **NO** background/periodic fetch, and **NO** real
callback consumption. The OpenClaw gateway remains the SOLE owner of the
Telegram update stream (A4). The adapter module contains no socket, no HTTP,
no Telegram API call and no poller — verified by AST-level tests.

**Phase 3C-B2** (plugin registration in the gateway, real callback delivery
through `dispatch_callback`, real `PostDecisionUx`, and the supported OpenClaw
ingress extension that forwards the original `update_id` and callback-message
`date` to the handler) is NOT part of this round and requires a **separate,
explicitly scoped owner gate** — together with any live approval test and any
persistent installation (SPEC V3C §18/§19). No
gateway/config/credential/allowlist/systemd/cron change has been made.
