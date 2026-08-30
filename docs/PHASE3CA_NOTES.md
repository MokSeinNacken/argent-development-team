# Phase 3C-A — Implementation Notes (NON-BINDING)

> **Status:** implementation notes only. This file is **not** part of the
> binding specification. The binding document is `docs/SPEC_V3C.md`
> (VERDICT: `SPEC_COMPLETE`) and its owner amendments A1–A7 remain unchanged
> and authoritative. Nothing here overrides, relaxes or extends any binding
> section. If anything here ever conflicts with `SPEC_V3C.md`, the spec wins.

Scope of this note: document what was actually implemented in the local
working tree as "Phase 3C-A — Secure Owner Approval Core", and make the
future integration boundary explicit. **No commit, no push.**

---

## 1. Implemented scope

Phase 3C-A implements the **offline, transport-neutral owner-approval
decision core** only — the part of SPEC V3C that can be built and verified
with zero Telegram, zero network, zero live processes, and zero
configuration changes:

- V6 schema (`approval_challenges`, `telegram_update_log`,
  `telegram_inbound_state`) with CHECK constraints, FKs, indexes, and a
  V5→V6 migration that preserves V5 data and rolls back on failure
  (`argent_core/store.py`).
- CSPRNG challenge capability (`secrets.token_urlsafe(32)`, ~43 URL-safe
  chars), hash-only persistence (`sha256(token)` only), 60-minute TTL, and a
  terminal single-use state machine (`argent_core/approval_core.py`).
- A strict `A:/R:/D:` + 43-char callback parser (fail-closed; no free text).
- A transport-neutral callback processor
  (`argent_core/approval_processor.py`) that turns a *structurally
  pre-validated* callback into a deterministic decision: update-id dedup,
  fail-closed identity (sender AND private chat), challenge lookup, binding
  re-verification, gate-status check, challenge CAS, and the approval
  decision — all in one `BEGIN IMMEDIATE` transaction via the Core bridge.
- Core bridge refactor (`argent_core/core.py`): `_approve_work_in_transaction`,
  `_reject_work_in_transaction`, `_expire_and_release` are shared by the
  public `Core.approve`/`Core.reject` and the processor, plus a REJECT expiry
  guard (A7/§12).
- Best-effort, non-authoritative post-decision UX abstraction
  (`PostDecisionUx`); a UI failure never rolls back a committed decision (A6).
- Deterministic test coverage: challenge core + processor tests
  (`tests/test_phase3c_approval_core.py`,
  `tests/test_phase3c_approval_processor.py`), including the full owner
  adversarial catalog (see §2).

## 2. Module layout

| Module | Responsibility |
| --- | --- |
| `argent_core/approval_core.py` | Challenge token/hash, TTL, terminal state machine, strict `[ARD]:` parser. No Telegram, no config. |
| `argent_core/approval_processor.py` | `ApprovalProcessor.process_callback(...)`, `CallbackOutcome`, `OwnerIdentitySource`, `PostDecisionUx`, safe details payload. Transport-neutral decision boundary. |
| `argent_core/core.py` | `_approve_work_in_transaction` / `_reject_work_in_transaction` / `_expire_and_release` bridge helpers shared with `Core.approve`/`Core.reject`. |
| `argent_core/store.py` | V6 schema, migration, challenge/log/cursor store methods (`_consume_challenge`, `_mark_challenge_*`, `_insert_update_log`, `_set_inbound_state`, …). |
| `docs/SPEC_V3C.md` | Binding spec (frozen; unchanged). |
| `tests/test_phase3c_approval_core.py` | Schema/token/state-machine/parser tests. |
| `tests/test_phase3c_approval_processor.py` | End-to-end processor + adversarial catalog tests. |

## 3. Transport-neutral boundary (what a future Phase-3C-B host must provide)

The processor is deliberately **not** coupled to any inbound transport. A
future host (the still-unimplemented "Phase 3C-B" live inbound adapter) must,
before a callback reaches `ApprovalProcessor.process_callback(...)`, supply:

1. **Structured callbacks.** It must parse a Telegram inline-button
   `callback_data` into `(CallbackAction, challenge)` via
   `approval_core.parse_callback`, and pass `action`, `challenge`,
   `update_id`, `sender_identity`, and `private_chat_identity` as explicit
   typed arguments. There is no text parser and no free-form command path.
2. **`OwnerIdentitySource`.** It must inject the single owner allowlist as the
   two expected identities (`expected_owner_user_id()` and
   `expected_owner_chat_id()`). Both are verified fail-closed by the
   processor; no other owner identity exists.
3. **`PostDecisionUx`.** It may inject a best-effort implementation
   (`answer_callback_query` / `edit_approval_message` / `remove_buttons`) that
   runs only *after* the decision commits; it is non-authoritative and its
   failures are swallowed.

The processor handles everything after that: update-id dedup, identity,
challenge lookup/binding/status/expiry, challenge CAS, approval decision, and
cursor advance.

## 4. What does NOT exist yet (explicit)

There is **no** Telegram inbound implementation, no `getUpdates` caller, no
webhook, no second poller, no background/periodic fetch, no inbound kick in
the supervisor loop, and **no live callback path**. No live approval has ever
run. No configuration, credential, allowlist, gateway, systemd, or cron
change has been made. A real inbound adapter (Phase 3C-B), any live approval
test, and any persistent installation each require a new, separately scoped
owner gate (SPEC V3C §18/§19).
