# Phase I3-B — GitHub Feature-Branch Push + PR Live Acceptance (NOTES)

Base commit: `ffc2664` (= `ffc266421ca0d53d1a5a7c2d078194f88e65868b`, Phase I3-A
GREEN). Worktree: `phase-i3b-github-live-acceptance`.

This document records the design of the **real** GitHub provider adapter, the
deterministic live-write activation gate, the task-scoped acceptance
allowlist, the credential-handling boundary, the deployment-tracking change,
known limitations, and the boundary to I3-C. It is written by the
implementation writer; **GREEN is marked only by Main after independent
verification + Sol review.**

**Scope:** code + deterministic tests + docs only. **No commit, no push, no
live service/systemd/state-dir/gh-config mutation, no network writes, no real
GitHub writes, no credential/token changes or printing, no LLM agents.** The
REAL external write is performed later by Main through the broker.

---

## 1. What I3-B adds on top of I3-A

I3-A built the provider-neutral broker, policy engine, fencing, and the
`ExternalProviderAdapter` ABC + `FakeGitHubAdapter` fixture — but had **no real
write path** (every mutation structurally `ProviderWriteDisabled` in acceptance
mode). I3-B adds the **real** GitHub adapter and the deterministic gate that
will let Main activate live writes safely:

- **NEW** `argent_core/github_provider_adapter.py` — real `GitHubProviderAdapter`
  (argv subprocesses), the live-write activation gate, acceptance identity
  constants, repository-identity canonicalization, failure classification, and
  the task-scoped allowlist/standing-policy builders.
- **EXTENDED** `argent_core/external_provider_adapter.py` — additive
  `ProviderNetworkError(ProviderUnavailable)` so a transport failure is
  distinguishable from a provider-reported outage while still mapping to the
  broker's existing retryable `OUTCOME_UNAVAILABLE`.
- **Tests** `tests/test_phase_i3b_live_gate.py`, `_adapter.py`, `_allowlist.py`,
  `_broker_live.py` (+ `tests/i3b_helpers.py` fake-executable harness) — **43
  I3-B tests** covering the brief's 30 cases + adversarial extras.
- **Docs** `docs/PHASE_I3B_NOTES.md` + `docs/PHASE_I3B_ACCEPTANCE.md`.
- **Deployment-tracking defaults** updated in `tests/test_phase_g2_unit_static.py`
  + `g2-systemd/install-check.sh` (G3 → I3-A worktree/doc).

## 2. Adapter contract (`GitHubProviderAdapter`)

Implements the I3-A `ExternalProviderAdapter` protocol:

| Operation | Executable (injectable) | Mechanism |
|---|---|---|
| `read_repository` | `gh` | `gh repo view <repo> --json name,defaultBranchRef` |
| `read_ref` | `gh` | `gh api repos/<repo>/commits/<ref> --jq .sha` |
| `read_pull_request` | `gh` | `gh pr view <n>` (by number) or `gh pr list --head <b>` (by head-branch) |
| `read_checks` | `gh` | `gh api repos/<repo>/commits/<ref>/check-runs` (best-effort: degrades to empty on failure) |
| `push_feature_branch` | `git` | `git push <trusted-url> refs/heads/<b>:refs/heads/<b>` |
| `create_pull_request` | `gh` | `gh pr create --repo <repo> --head <b> --base <base> --title <t> --body-file <tmp>` |
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
- **Credential handling is controller-side** — the controller injects any
  credential via the `env` mapping; the adapter never reads/prints/logs
  credential VALUES and never places them in argv.
- **Failure classification** — `classify_gh_failure` maps provider failures to
  the closed I3-A taxonomy (401→credential, 403→credential/policy, 409→conflict,
  400/422→validation, 429→rate-limit, ≥500→unavailable); transport failures
  (spawn/OS/timeout) → `ProviderNetworkError` (retryable outage).

## 3. Live-write activation gate (CASE 1/2)

```python
LIVE_WRITE_REQUIRED_COMMIT = "ffc266421ca0d53d1a5a7c2d078194f88e65868b"  # I3-A GREEN

def live_write_gate(activation_flag, *, resolver_present=None) -> bool
```

Live write requires **BOTH**:

(a) an explicit activation flag (the controller opts in), AND
(b) the running code carries the I3-A credential-mask fix, enforced by
    (i) `credential_mask_fix_present()` (the
    `execution_scope.resolve_credential_mask_paths` resolver is present) AND
    (ii) `LIVE_WRITE_REQUIRED_COMMIT == _I3A_CREDENTIAL_MASK_MARKER`.

Fails closed on any of: no flag, no resolver, or a marker pinned to a
pre-I3-A commit (tests patch `LIVE_WRITE_REQUIRED_COMMIT` to prove this).
`credential_mask_fix_present()` is the module-constant I3-A-hardening check:
it verifies the running `execution_scope` exposes
`resolve_credential_mask_paths` (which masks `~/.config/gh` inside the agent
sandbox — the fix required before any real write may happen).

## 4. Task-scoped acceptance allowlist (CASE 7/8)

`github_acceptance_allowlist()` / `github_acceptance_standing_policy()` build
the I3-B acceptance policy (exact-match only, no wildcards):

- provider `github`, account `MokSeinNacken`, repository
  `MokSeinNacken/argent-development-team`;
- autonomous classes `read_repository`/`read_ref`/`read_pull_request`/
  `read_checks`/`push_feature_branch`/`create_pull_request`;
- branch namespace `argent/`, PR base `main`;
- SENSITIVE (`merge_pull_request`/`create_release`/`deploy_production`) is
  **PERMITTED but never autonomous** → `OWNER_GATE_REQUIRED` (CASE 20);
- standing-policy autonomous writes = `{push_feature_branch, create_pull_request}`
  only.

This is a **builder** (usable by Main's live flow and by tests); it is never
activated as a persistent standing policy.

## 5. Credential / helper mechanism note

- The GitHub account `MokSeinNacken` authenticates via `gh` (`~/.config/gh/hosts.yml`
  mode 0600, classic PAT; scopes gist/read:org/repo/workflow). **No**
  `git credential.helper` is configured — `gh`'s auth is the credential source.
- The adapter does **not** modify git config. For `gh` subprocesses the live
  controller may inject `GH_TOKEN`/`GH_HOST` via the `env` mapping, or rely on
  `gh`'s own config; for `git push` the live flow will use `gh`'s auth (or a
  controller-side credential mechanism). This is controller-side and untested
  here — the adapter is fully testable with an injected fake `gh`/`git`
  executable script and never embeds/echoes tokens.
- Acceptance repo: `github.com/MokSeinNacken/argent-development-team`
  (owner-controlled, NOT a fork, default branch `main`, remote `main` SHA ==
  `ffc2664…`, origin configured in `/home/pc/projects/argent-development-team`).

## 6. Deployment-tracking change (I3-B)

The live deployed systemd unit now points at the **I3-A worktree**
(`/home/pc/projects/argent-worktrees/phase-i3a-external-action-broker`, doc
`docs/PHASE_I3A_ACCEPTANCE.md`). The deployment-substitution defaults in
`tests/test_phase_g2_unit_static.py` and `g2-systemd/install-check.sh` were
updated from the G3 worktree/`PHASE_G3_ACCEPTANCE.md` to the I3-A
worktree/`PHASE_I3A_ACCEPTANCE.md` (env-parameterizable, same pattern).

## 7. Known limitations / boundary to I3-C

- **No real write here.** The adapter is implemented and tested only against
  fake executables; the actual live push/PR is a Main-driven operation through
  the broker (not part of I3-B code).
- **No scheduler wiring.** `redrive_waiting` (the I3-A hook) is still not wired
  to a background runtime; I3-B adds no scheduler changes.
- **Credential transport for `git push`** (how git authenticates to GitHub
  without a credential.helper) is controller-side and NOT implemented here —
  it is resolved by Main's live flow (I3-C or the live activation step).
- The adapter's `read_checks` is best-effort (degrades to empty on failure).
- `ProviderNetworkError` is additive and maps to the existing retryable outage
  class; the broker's `FAILURE_NETWORK` class remains unused (future I3-C
  granularity) rather than inventing a new closed-set outcome.
