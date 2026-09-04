# Phase I3-B — GitHub Feature-Branch Push + PR Live Acceptance (NOTES)

Base commit: `ffc2664` (= `ffc266421ca0d53d1a5a7c2d078194f88e65868b`, Phase I3-A
GREEN). Worktree: `phase-i3b-github-live-acceptance`. Closing snapshot: `e825111b`.

These notes record the design of the **real** GitHub provider adapter, the
deterministic live-write activation gate, the task-scoped acceptance allowlist,
the credential-handling boundary, the **Sol closing-review fixes** (HIGH
6/13/17 + LOW 14/16/18), and the boundary to I3-C. **GREEN is marked only by
Main after independent verification + Sol review** — this document is NOT GREEN
until then.

---

## 1. What I3-B adds on top of I3-A

I3-A built the provider-neutral broker, policy engine, fencing, and the
`ExternalProviderAdapter` ABC + `FakeGitHubAdapter` fixture — but had **no real
write path** (every mutation structurally `ProviderWriteDisabled` in acceptance
mode). I3-B adds the **real** GitHub adapter and the deterministic gate that
lets Main activate live writes safely:

- **NEW** `argent_core/github_provider_adapter.py` — real `GitHubProviderAdapter`
  (argv subprocesses), the live-write activation gate, acceptance identity
  constants, repository-identity canonicalization, failure classification, and
  the task-scoped allowlist/standing-policy builders. Closing fixes (see §6)
  add the bound-SHA mutation boundary and fail-closed PR parsing/author binding.
- **EXTENDED** `argent_core/external_provider_adapter.py` — additive
  `ProviderNetworkError(ProviderUnavailable)` so a transport failure is
  distinguishable from a provider-reported outage while still mapping to the
  broker's existing retryable `OUTCOME_UNAVAILABLE`.
- **HARDENED** `argent_core/external_action_broker.py` — authoritative audit
  history (HIGH-13).
- **Tests** `tests/test_phase_i3b_live_gate.py` (9), `_adapter.py` (25),
  `_allowlist.py` (8), `_broker_live.py` (8) + `tests/i3b_helpers.py`
  fake-executable harness — **50 I3-B tests**; full suite **2853 passed**.
- **Docs** `docs/PHASE_I3B_ACCEPTANCE.md` + this file.

## 2. Adapter contract (`GitHubProviderAdapter`)

Implements the I3-A `ExternalProviderAdapter` protocol:

| Operation | Executable (injectable) | Mechanism |
|---|---|---|
| `read_repository` | `gh` | `gh repo view <repo> --json name,defaultBranchRef` |
| `read_ref` | `gh` | `gh api repos/<repo>/commits/<ref> --jq .sha` |
| `read_pull_request` | `gh` | `gh pr view <n>` (by number) or `gh pr list --head <b>` (by head-branch) |
| `read_checks` | `gh` | `gh api repos/<repo>/commits/<ref>/check-runs` (best-effort: degrades to empty on failure) |
| `push_feature_branch` | `git` | local `rev-parse --verify` bound-SHA check → `git push <trusted-url> refs/heads/<b>:refs/heads/<b>` → remote `ls-remote` readback |
| `create_pull_request` | `gh`/`git` | remote head `ls-remote` == `head_sha` check → `gh pr create --repo <repo> --head <b> --base <base> --title <t> --body-file <tmp>` |
| `update_pull_request` | `gh` | `gh pr edit <n>` (own-PR only, author-verified) |
| `observe` | `git`/`gh` | `git ls-remote` (push) / `gh pr list` (PR) reconciliation probes |

Hard guarantees:

- **argv-only** — every op runs `subprocess` with an explicit argv list (NO
  `shell=True`/`eval`/`exec`); executables injectable for tests.
- **NO-WRITE default** — `write_enabled` is derived from the live-write gate;
  mutation methods also re-assert it (defense in depth).
- **Trusted push URL** — the remote URL is resolved from `trusted_repo_urls`
  keyed by canonical repo identity; an agent-supplied URL is never accepted
  (CASE 4/5/6).
- **Bound-SHA mutation boundary (HIGH-6)** — push verifies the local ref equals
  the bound SHA before pushing AND the remote ref equals it after; PR create
  verifies the remote head equals `head_sha` before creating. A mismatch is a
  `ProviderConflict`, never a fabricated success.
- **Credential handling is controller-side** — the controller injects any
  credential via the `env` mapping; the adapter never reads/prints/logs
  credential VALUES and never places them in argv (code-enforced + tested).
- **Failure classification** — `classify_gh_failure` maps provider failures to
  the closed I3-A taxonomy (401→credential, 403→credential/policy, 409→conflict,
  400/422→validation, 429→rate-limit, ≥500→unavailable); transport failures
  (spawn/OS/timeout) → `ProviderNetworkError` (retryable outage).

## 3. Live-write activation gate (CASE 1/2)

```python
LIVE_WRITE_REQUIRED_COMMIT = "ffc266421ca0d53d1a5a7c2d078194f88e65868b"  # I3-A GREEN

def live_write_gate(activation_flag, *, resolver_present=None) -> bool
```

Live write requires **BOTH**: (a) an explicit activation flag, AND (b) the
running code carries the I3-A credential-mask fix (`credential_mask_fix_present()`
AND `LIVE_WRITE_REQUIRED_COMMIT == _I3A_CREDENTIAL_MASK_MARKER`). Fails closed
on any missing piece.

## 4. Task-scoped acceptance allowlist (CASE 7/8)

`github_acceptance_allowlist()` / `github_acceptance_standing_policy()` build
the I3-B acceptance policy (exact-match only, no wildcards): provider `github`,
account `MokSeinNacken`, repository `MokSeinNacken/argent-development-team`;
autonomous `read_repository`/`read_ref`/`read_pull_request`/`read_checks`/
`push_feature_branch`/`create_pull_request`; branch namespace `argent/`; PR base
`main`; SENSITIVE (merge/release/deploy) is PERMITTED but never autonomous →
`OWNER_GATE_REQUIRED`. This is a **builder**, never activated as a persistent
standing policy.

## 5. Credential / helper mechanism (OBSERVED LIVE)

- `MokSeinNacken` authenticates via `gh` (`~/.config/gh/hosts.yml` mode 0600,
  **classic PAT**, scopes `repo` + `workflow`).
- **`git push` authenticates via `gh`'s git-credential helper** (`gh auth` is the
  configured `git credential.helper`). The adapter does not modify git config;
  for `git push` the live flow uses `gh`'s auth. The adapter is fully testable
  with injected fake `gh`/`git` executables and never embeds/echoes tokens.
- **Residual authority:** GitHub `main` is **unprotected** (no rulesets); the
  `repo`-scoped PAT has direct push authority to `main`. This is mitigated only
  by the broker's code-enforced protected-ref/namespace policy and by Main
  never exercising it — an OPERATIONALLY-REQUIRED constraint, not a
  provider-side protection.
- Acceptance repo: `github.com/MokSeinNacken/argent-development-team`
  (owner-controlled, NOT a fork, default branch `main`, remote `main` SHA ==
  `ffc2664…`).

## 6. Sol closing-review fixes (this round)

- **HIGH-6** — bound SHA enforced at the mutation boundary (local ref check →
  push → remote readback; remote head check before PR create). See §2.
- **HIGH-13** — authoritative audit: `AUTHORIZED` on every successful
  authorization (autonomous + owner, with authorization reference / policy
  decision); `EXECUTED`/`RECONCILED_SUCCESS` only on the success branch of the
  authoritative transition (failed lease/revision CAS ⇒ bounded outcome with no
  fabricated execution row).
- **HIGH-17** — this doc rewrite (truthful live-flow evidence, credential
  architecture, findings, distinctions, unprotected main, C1-as-artifact).
- **LOW-14** — `_parse_pr_number` binds the `pull/<digits>` URL to the expected
  `owner/repo`; `_find_own_pr` fails closed on a missing/non-dict author.
- **LOW-16** — removed unreachable leftover code after `return None` in
  `_parse_pr_number`; URL-parser regression test covers expected-repo binding.
- **LOW-18** — docs staleness (credential helper, test count, pushed state,
  unprotected main, PAT scopes) — resolved by HIGH-17.

## 7. Deployment-tracking change (I3-B)

The live deployed systemd unit points at the **I3-A worktree**
(`docs/PHASE_I3A_ACCEPTANCE.md`). The deployment-substitution defaults in
`tests/test_phase_g2_unit_static.py` and `g2-systemd/install-check.sh` were
updated from the G3 worktree/`PHASE_G3_ACCEPTANCE.md` to the I3-A
worktree/`PHASE_I3A_ACCEPTANCE.md` (env-parameterizable, same pattern).

## 8. Live flow evidence (OBSERVED LIVE) + split-store caveat

- **1 branch** `argent/efe311ca-7647-4915-bf9a-d63bca966c1b-i3b-live-acceptance`
  @ `e825111b`; **1 PR** (PR #1, OPEN, DO-NOT-MERGE, head `e825111b`, base `main`
  `ffc26642`); **0 merges**; remote `main` unchanged at `ffc26642`.
- **Broker-only writes**; no direct/adapter writes, no shell, no LLM.
- **Split-store ledger:** the original store's `create_request` SUCCEEDED with
  `provider_object_id = NULL` + failed readback; a **fresh reconciliation store**
  recorded `RECONCILED_SUCCESS`. The durable success evidence is therefore split
  across two ledgers (documented limitation of the live run, not a hidden write).

## 9. Known limitations / boundary to I3-C

- **No provider-side changes** and **no schema change** are part of this work.
- **No scheduler wiring.** `redrive_waiting` is still not wired to a background
  runtime; I3-B adds no scheduler changes.
- The adapter's `read_checks` is best-effort (degrades to empty on failure).
- `ProviderNetworkError` is additive and maps to the existing retryable outage
  class; the broker's `FAILURE_NETWORK` class remains unused (future I3-C
  granularity).
- GitHub `main` protection (ruleset) is a later operational/I3-C item; until
  then it is OPERATIONALLY REQUIRED to treat `main` as protected.

## 10. Status

**NOT GREEN.** Closing fix round applied; pending Main independent verification +
full regression run. GREEN marker (only after Main):
`ARGENT_PHASE_I3B_GITHUB_LIVE_ACCEPTANCE_GREEN`.
