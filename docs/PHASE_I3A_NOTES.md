# Phase I3-A — External Action Broker Core (NOTES)

Base commit: `ab238ff` (Phase I2 GREEN). Worktree:
`phase-i3a-external-action-broker`.

This document records the mandatory read-only analysis (A–I), the design, the
credential-architecture fix, the code-enforced vs operationally-required
boundary, the local demo, known limitations, and the boundary to I3-B. It is
written by the implementation writer; **GREEN is marked only by Main after
independent verification + Sol review.**

---

## Part 1 — Mandatory read-only analysis (A–I)

### A. What external writes exist today (before I3-A)

Today the product does **not** write to GitHub or any provider. The only
write-capable boundary that reaches toward "outside" is:

- **Local git** in the integration worktree (`integration_candidate.GitClient`
  argv-only `worktree add/merge/reset/branch`) — this is LOCAL and integration-
  scoped, never a remote push.
- **Notifications** (`notifications.outbox`) — outbound-only, at-least-once,
  local-dedup, no secret columns; transport is a bounded protocol.
- **`external_wait`** — a read-only checker that never writes to a provider.

So the external *write* trust boundary is **absent today**; I3-A is the first
time a durable, controller-authoritative external write request + policy +
lifecycle exists, and it is deliberately **provider-neutral** and **write-
disabled in acceptance mode** (no real provider write path).

### B. Where the credential architecture lives today (gh CLI)

- `gh` CLI `/usr/bin/gh` is authenticated **only** via `~/.config/gh/hosts.yml`
  (mode 0600, dir `~/.config/gh` mode 751) holding the token for account
  `MokSeinNacken`, plus `~/.config/gh/config.yml` (mode 600).
- **No** `GH_*` / `GITHUB_*` env vars in the argent service env or shell env;
  `gh` reads `hosts.yml` from `~/.config/gh` at runtime. That is the credential
  architecture today and I3-A **does not change it** (token/scope changes are
  I3-B / owner).
- `~/.git-credentials` and `~/.netrc` are **absent** on this host (masked only
  if present — tmpfs masks need no source).

### C. The confirmed live defect (§12 / §31)

A current production-sandbox child **can read** the authenticated GitHub
credential material: `~/.config/gh/hosts.yml` and `~/.config/gh/config.yml` are
VISIBLE (read-only) inside the agent-dispatch `bwrap` sandbox because only
`~/.config/argent` and `~/.local/state/argent` are tmpfs-masked; `~/.config/gh`
sits under the broad read-only root (`--ro-bind / /`) and is therefore
readable by the untrusted same-UID agent child. **Main already live-probed the
before-fix exposure**; I3-A reproduces it deterministically (the before-fix
argv leaves `hosts.yml` OPENED inside the sandbox) and closes it.

### D. What the I2 integration result provides (source provenance)

`integration_candidates` (SCHEMA 21) carries the authoritative merge-queue
result: `repository`, `integration_target`, `source_job_id`, `base_commit`,
`source_head`, `source_branch`, `integration_branch`, `integrated_head`,
`merge_classification`, `revision` (CAS), `holder_*`, `result_json` (bounded,
authenticated via Phase-F `plan_hash` + keyed `evidence_mac`). An INTEGRATED
candidate is the **only** admissible source for an external write request.

### E. How fencing works (reused by I3-A)

- `action_locks` (I1): named repo-global / global locks, CAS-acquired by a
  `(job_id, lease_epoch)` holder, lease-fenced, stale-holder atomically
  reclaimable (`store.try_acquire_action_lock` / `reclaim_stale_action_lock`).
- Fenced store transitions: `transition_integration_candidate_authoritative`
  atomically re-verifies the holder's **live job lease** AND the **action-lock
  ownership** before each write (a stale holder can never finalize).
- `supervisor_jobs` 8-state model + `QueueReason` + owner gates
  (`owner_approvals` / `approval_challenges`, `gates.binding_hash(task_id,
  action, scope)`).

### F. What owner authorization provides (reused by I3-A)

`gates.binding_hash` (sha256 over `[task_id, action, scope]`) + the
`approval_core`/`approval_processor` single-use challenge lifecycle: an approval
authorizes exactly (task, action, scope), is single-use, and can never be a
blanket approve-everything. I3-A reuses `binding_hash` with a scope that also
binds `provider` + `repository` + `resource_ref` + `requested_scope`.

### G. How external-wait works (reused by I3-A)

`external_wait.WaitSpec`/`WaitObservation`/`ExternalWaitAdapter` +
`next_check_delay_seconds` backoff (1/2/5/10/30 min + jitter): a job enters
`WAITING_EXTERNAL` (releasing the compute lease) and is woken only by a
bounded, validated observation. I3-A mirrors this for request-level
`WAITING_EXTERNAL` with `next_check_at` / `attempt_count` — no LLM occupies a
slot while waiting, no aggressive polling.

### H. What validation / git safety exists (reused by I3-A)

`worktree.validate_repo_identity` / `validate_branch_identity` (rejects `-`,
`..`, `~`, `^`, `:`, `@{`, glob/control chars), `is_sha_like`, argv-only git
(no `shell=True`/`eval`/`exec`). I3-A validates owner/repo/ref/branch/SHA/base/
title/body/URL-like params before they reach any argv; no command
substitution, no `--upload-pack`/`--receive-pack`/`ext::`/`file::` injection.

### I. What I3-A must close (this phase)

1. Close the credential-read defect (C) by extending the sandbox with
   additional tmpfs masks for the external-credential homes.
2. Introduce the provider-neutral external-write trust boundary with a
   deterministic policy engine (no LLM), bounded states, fencing,
   idempotency/reconciliation, secret-free audit, publication safety, and a
   structurally write-disabled provider path (no real writes in I3-A).

---

## Part 2 — Design

### 2.1 `argent_core/external_action_broker.py` (new)

- `ActionTaxonomy` (READ / BOUNDED_WRITE / SENSITIVE) + a closed action
  registry (`ACTIONS`) with the GitHub-oriented initial set.
- `RequestState` — the SEPARATE 8-state broker lifecycle
  (PENDING/AUTHORIZED/EXECUTING/WAITING_EXTERNAL/SUCCEEDED/FAILED/BLOCKED/
  DENIED); terminal clearly defined; NOT new job states.
- `ExternalActionRequest` — versioned controller-authoritative record (request
  id, provider, account, action, policy class, repo, resource, source job/
  candidate refs, scope, bounded parameters/preconditions, idempotency key,
  provenance version/hash, revision, holder identity + lease epoch, action
  lock, provider state/object id, attempt/backoff, expiry, failure class).
- `PolicyDecision` (ALLOW_AUTONOMOUS / OWNER_GATE_REQUIRED / DENY / DEFER) +
  bounded reason codes; `PolicyEngine` is a pure deterministic function (no
  LLM); unknown provider/action/repo → DENY; no string-prefix authz.
- `ExternalActionAllowlist` / `AllowlistEntry` / `StandingPolicy` — trusted
  controller config (agents cannot modify; changing it is owner-gated and NOT
  activated beyond fixture config in I3-A).
- Branch safety: `is_protected_ref` (main/master/stable/release*/production*)
  + `autonomous_branch_ok` (autonomous pushes restricted to `argent/<task-id>-
  <slug>`).
- Fencing: request revision CAS + holder-verified authoritative transitions
  (`store.transition_external_action_request_authoritative`) reusing I1/I2
  lease + action-lock fences.
- Idempotency/reconciliation: UNIQUE `idempotency_key`; `reconcile` probes
  provider-visible state (push: remote ref == expected SHA; create PR: existing
  Argent-owned PR for head) — never claims exactly-once beyond provider
  semantics.
- Retry budget/backoff (bounded ladder) + action expiry.
- Secret-free audit (REQUESTED/AUTHORIZED/EXECUTED/RECONCILED) with a bounded
  failure-class taxonomy (AUTHORIZATION / POLICY_DENIED / RATE_LIMIT /
  PROVIDER_UNAVAILABLE / NETWORK / PRECONDITION_FAILED / CONFLICT / CREDENTIAL /
  REMOTE_VALIDATION / LOCAL_CODE_ERROR / UNKNOWN).
- Publication safety (`validate_pr_title` / `validate_pr_body`: bounded,
  secret-rejected/redacted, injection-marker rejection).
- External-wait integration (`WAITING_EXTERNAL` + `next_check_at`/attempt).
- Read/write capability separation (READ autonomous; mutations gated + write-
  enabled check).

### 2.2 `argent_core/external_provider_adapter.py` (new)

- `ExternalProviderAdapter` ABC with `read_repository`/`read_ref`/
  `read_pull_request`/`read_checks`/`push_feature_branch`/`create_pull_request`/
  `update_pull_request` + `observe`; mutations raise `ProviderWriteDisabled` by
  default (I3-B-ready, structurally write-disabled).
- `NoWriteExternalProviderAdapter` (read-capable, write-disabled default).
- `FakeGitHubAdapter` (deterministic in-memory fixture): fast-forward push,
  duplicate-PR detection (Argent-owned marker), scripting hooks, `observe`
  reconciliation probes.
- `ProviderResult` / `ProviderObservation` bounded result types + provider
  exception taxonomy (rate-limit ≠ code failure).

### 2.3 Schema (additive, 21 → 22)

`external_action_requests` (bounded CHECK over the 8 broker states + taxonomy +
audit/failure-class closed sets; UNIQUE `idempotency_key`) and
`external_action_audit` (secret-free event ledger) via the existing `_SCHEMA` /
`_migrate` mechanism. Store methods: `create/get/list/
transition_external_action_request`,
`transition_external_action_request_authoritative`,
`append_external_action_audit`, `list_external_action_audit`. No new job
states; queue/action tables separate from `supervisor_jobs`.

### 2.4 Credential isolation fix (§31)

`execution_scope.resolve_credential_mask_paths()` returns `~/.config/gh`
(always) + `~/.git-credentials` / `~/.netrc` (when present). The
`build_agent_sandbox_argv` builder gained an explicit `credential_dirs`
parameter emitted as additional empty `--tmpfs` masks; the production backend
(`_resolve_sandbox_dirs` → `_wrap_for_sandbox`) resolves and passes them. The
G3 narrowing (ro root + per-agent runtime dirs + the two trusted-dir masks) is
preserved unchanged. Docstrings + sandbox tests updated.

### 2.5 Provenance (source)

`create_request` requires a provenance dict verified by `verify_provenance`
against trusted store facts: the source candidate is INTEGRATED with a sha-like
`integrated_head` AND `candidate.source_job_id == provenance.source_job_id`;
the source job is terminal-DONE with a proven source head; the repository AND
integration target match the candidate; and there are no unresolved
HIGH/CRITICAL findings.  The provenance carries a **keyed HMAC**
(`compute_provenance_mac`, reusing the Phase-F evidence MAC key — NOT a plain
unkeyed SHA-256), so an agent cannot forge it.  Push `sha` and PR `head_sha`
must equal the candidate's integrated HEAD (bound to the integrated result).
Agent statements are never sufficient (CASE 11–15); missing provenance fails
closed.

### 2.6 Owner authorization

`authorize_owner` loads the approval from the authoritative `owner_approvals`
store, re-runs the policy engine (DENY can never be owner-authorized), verifies
APPROVED + unexpired + TRUSTED-source, binds exactly (task, action, provider,
account, repository, resource, requested_scope, provenance, idempotency key,
parameters/preconditions) via `gates.binding_hash`, and atomically consumes it
single-use via `store._consume_approval`.  A consumed/expired/forged approval
can never authorize; no blanket approve-everything (CASE 32–34); SENSITIVE
(merge/release/deploy) is OWNER_GATE_REQUIRED (CASE 48/49).

### 2.7 Untrusted external content

Provider/GitHub/PR/CI/web content can never self-authorize: only the bounded
`ProviderResult`/`ProviderObservation` types are interpreted; no new action is
created from tool output (CASE 10).

---

## Part 3 — Credential architecture (unchanged)

`gh` (2.46.0) reads `~/.config/gh/hosts.yml` (0600) at runtime; no env-var
credentials. I3-A does **not** change tokens/scopes (I3-B / owner). The only
change is defensive: the agent-dispatch sandbox now masks `~/.config/gh` (+ a
future `~/.ssh` and `~/.git-credentials`/`~/.netrc` when present) so untrusted
role agents cannot read provider credentials, while the trusted
broker/controller side (outside the sandbox) retains read access.  Provider
credentials do NOT live under `~/.openclaw` (that is the OpenClaw config/
credential home, masked by G3).

---

## Part 4 — CODE-ENFORCED vs OPERATIONALLY REQUIRED vs OBSERVED LIVE

- **CODE-ENFORCED** (deterministic code + tests): closed taxonomy/action
  registry; bounded broker states; deterministic policy engine (unknown →
  DENY, no string-prefix authz; full allowlist BEFORE the SENSITIVE gate;
  standing policy consulted; `branch_namespaces` enforced); branch-safety
  (protected refs + autonomous namespace); request revision CAS + holder-
  verified authoritative transitions (execution AND reconciliation) + a closed
  edge map with terminal immutability; UNIQUE `idempotency_key` with full-
  equivalence reuse; keyed provenance MAC (Phase-F evidence key); store-backed
  single-use owner approval; enforced expiry + unified retry budget + a
  deterministic `redrive_waiting` hook; provider reconciliation (push: repo +
  ref + SHA; PR: repo + head SHA + base + idempotency marker + argent-owned);
  secret-free audit + closed failure classes + provider-detail redaction;
  publication sanitization; write-disabled provider path (CASE 50); the
  credential tmpfs masks in `build_agent_sandbox_argv` +
  `resolve_credential_mask_paths` (incl. a conditional `~/.ssh`).
- **OPERATIONALLY REQUIRED** (must hold in the deployed supervisor, not
  enforceable by the broker alone): the supervisor provides a real leased
  holder job; a real (I3-B) provider adapter + credential provisioning; the
  allowlist/standing-policy is loaded from trusted controller config (changing
  it is owner-gated); a real `gh`/provider binary is present and authenticated
  on the controller side; the sandbox `credential_dirs` are wired through the
  production backend (they are, by `_resolve_sandbox_dirs`); the evidence MAC
  key is provisioned via the existing Phase-F `_resolve_mac_key` contract.
- **OBSERVED LIVE** (local demo / live bwrap probe, not a regression
  guarantee): the fixed sandbox makes `~/.config/gh/hosts.yml`/`config.yml`
  ENOENT inside while the supervisor-side read still works; the before-fix
  argv leaves them OPENED (documented, reproduced).  **The live deployed
  service still runs the G3 code path until an authorized redeploy — an
  operational rollout state, not an I3-A code defect.**

---

## Part 5 — Local demo (§30)

`docs/i3a_demo.py` (no network, no real writes): Integrated Candidate →
ExternalActionRequest → Policy → ALLOW_AUTONOMOUS → Broker → FakeGitHubAdapter →
feature-branch push + PR creation → provider object ids → audit persisted; then
provider-accepted PR + crash-before-SUCCESS + restart/reconcile → existing PR
detected, no duplicate; then MERGE → OWNER_GATE_REQUIRED. Exits 0; graceful
note if git is unavailable.

---

## Part 6 — Known limitations / boundary to I3-B

- **No real provider write path** — mutations run only against the fake
  adapter / no-write mode (I3-B wires the real `gh` adapter + credentials).
- **No live Telegram owner gate** — `authorize_owner` loads a store-backed
  `OwnerApproval` (single-use, atomic consume); wiring it to the Telegram
  approval adapter is I3-B.
- **Standing policy consulted, not provisioned** — the broker consults the
  standing policy (empty default ⇒ no autonomous write), but loading a real
  non-empty policy + owner-gated mutation of allowlists is I3-B.
- **`redrive_waiting` is a marked hook** — the deterministic broker method
  exists + is tested; the real scheduler/background-runtime consumer is I3-B.
- **No branch-namespace slug enforcement beyond `<ns><task-id>-<slug>`**
  (the slug itself is not further constrained; the namespace is per-allowlist).
- **`read_checks` / merge/release/deploy** have no fixture mutation semantics
  (they are structurally write-disabled / owner-gated).
- **Credential masks** cover `~/.config/gh` (+ `~/.ssh`, git-credentials/netrc
  when present); a customized `XDG_CONFIG_HOME` gh dir is honored by
  `resolve_credential_mask_paths` (operationally, the operator must ensure the
  mask set covers the real credential home).
- **`parameters`/`provider_state`** are bounded JSON (≤64 KiB), redacted; the
  broker persists only sanitized, bounded reason codes (never raw provider
  responses).
- **Provenance MAC key** is the existing Phase-F evidence key (resolved via
  `_resolve_mac_key`, fail-closed when absent) — no new secret is introduced.

## Part 7 — Status

**NOT GREEN.** Independent verification + Sol closing review pending (Main).
