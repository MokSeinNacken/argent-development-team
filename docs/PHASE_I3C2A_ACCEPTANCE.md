# PHASE I3-C2-A ACCEPTANCE — Minimal Safe GitHub Actions CI Bootstrap

**Branch:** `phase-i3c2a-ci-bootstrap` (Base `29de775ad12088cba13cf2285a6514b12f9d770d` = Phase I3-C1 GREEN).
**Datum:** 2026-09-04.
**Scope:** ADDITIVE only (one workflow + one test module + one `conftest.py`
hook + portability guards + two docs). No commit, no push, no
GitHub/broker/systemd/service/state-dir mutation by this writer, no LLM agents.
The PR carrying this workflow is created by the supervisor via the trusted
external-action broker.

**STATUS: FIX ROUND APPLIED — first live CI run failed (fail-closed), fixes in
place; GREEN marked only by Main after the final fix push.** GREEN marker (only
after Main): `ARGENT_PHASE_I3C2A_CI_BOOTSTRAP_GREEN`. No
`ARGENT_PHASE_I3_GREEN`.

---

## 1. Deliverables

- **NEW** `.github/workflows/ci.yml` — minimal, safe, read-only CI workflow
  (`on: pull_request → branches: [main]`, `permissions: contents: read`, single
  job `test`, portable fail-closed `python -m pytest tests/ -q -m "not
  host_acceptance"`, `timeout-minutes: 15`, Python `'3.14'`, pytest `==9.1.1`,
  only `actions/checkout@3d3c42e…` + `actions/setup-python@5fda3b9…` immutable
  SHA pins, checkout `persist-credentials: false`, bubblewrap provisioned).
- **NEW** `tests/test_phase_i3c2a_ci_workflow.py` — deterministic stdlib-only
  canonical structural security validation (19 cases → 19 test functions).
- **NEW** `tests/conftest.py` `pytest_configure` hook — registers the
  `host_acceptance` marker.
- **UPDATED** `tests/test_sandbox_runner.py` (docstring), `tests/test_phase_c2_real_scope_smoke.py`
  (`host_acceptance` marker), `tests/test_phase_g2_unit_static.py` (empty
  `FragmentPath` guard), `tests/test_phase3d_visualizer_snapshot.py`
  (skip-if-absent sibling guard).
- **NEW/UPDATED** `docs/PHASE_I3C2A_NOTES.md`, `docs/PHASE_I3C2A_ACCEPTANCE.md`.

## 2. Check identity (the source of truth I3-C2-B will use)

| Field | Value |
|---|---|
| workflow `name` | `Argent CI` |
| trigger | `pull_request` → `branches: [main]` |
| top-level `permissions` | `contents: read` |
| job id | `test` (no display-name override) |
| required check name | **`test`** |
| test command | `python -m pytest tests/ -q -m "not host_acceptance"` (portable, fail-closed) |
| timeout | `timeout-minutes: 15` |
| trusted actions | `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1` (v7.0.1), `actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97` (v7.0.0) |
| checkout token boundary | `persist-credentials: false` (ephemeral token not persisted; no later step receives a token) |

**Check provenance (LOW-4):** the required-check identity for I3-C2-B comprises
check name `test` **AND** GitHub Actions app slug `github-actions` **AND**
workflow path `.github/workflows/ci.yml` — a same-named check from another app
must never satisfy the wait.

## 3. Security case → test mapping (19 cases)

| Case | Meaning | Test function |
|---|---|---|
| 1 | pull_request trigger restricted to `branches: [main]` | `test_case1_pull_request_trigger_restricted_to_main` |
| 2 | no `pull_request_target` | `test_case2_no_pull_request_target` |
| 3 | no secrets referenced | `test_case3_no_secrets_referenced` |
| 4 | permissions read-only (`contents: read` only, no write scope) | `test_case4_permissions_read_only_contents_only` |
| 5 | no deploy/release/publish/artifact capability | `test_case5_no_deploy_release_publish` |
| 6 | no external write token / credentials | `test_case6_no_external_write_token_or_credentials` |
| 7 | authoritative test command fail-closed | `test_case7_test_command_fail_closed` |
| 8 | no `continue-on-error` | `test_case8_no_continue_on_error` |
| 9 | no `${{` in any run block (PR title/body/head/labels) | `test_case9_no_run_shell_interpolation_from_pr` |
| 10 | no branch/ref/event input → shell command | `test_case10_no_branch_ref_shell_command` |
| 11 | timeout bounded (5 ≤ value ≤ 30) | `test_case11_timeout_bounded` |
| 12 | only trusted actions (exact set, SHA-pinned) | `test_case12_only_trusted_actions` |
| 13 | required-check identity (name/job/CHECK_NAME) | `test_case13_required_check_identity` |
| 14 | workflow path exactly `.github/workflows/ci.yml` | `test_case14_workflow_path_exact` |
| 15 | PR content cannot become command authority | `test_case15_pr_content_cannot_become_command_authority` |
| 16 | no schedule/workflow_dispatch/repository_dispatch | `test_case16_no_other_triggers` |
| 17 | no env block / GITHUB_TOKEN / env injection | `test_case17_no_env_or_github_token` |
| 18 | exact canonical step set + structural key allowlists | `test_case18_exact_canonical_step_set` |
| 19 | checkout `persist-credentials: false` | `test_case19_checkout_persist_credentials_false` |

## 4. Deterministic test counts (fix round, local)

| Group | Result |
|---|---|
| I3-C2-A targeted | **19 passed** |
| **FULL SUITE** (deployment env exports) | **2955 passed** in ~48 s |
| Portable collect (`-m "not host_acceptance"`) | **2954 collected, 1 deselected** |

## 5. Local verification (this writer, fix round)

| Prüfung | Ergebnis |
|---|---|
| YAML syntax (`yaml.safe_load`) | OK — top-level keys `['name', True, 'permissions', 'jobs']` (`on`→`True` YAML 1.1 boolean quirk, expected; repo tests are text-based) |
| I3-C2-A targeted | **19 passed** (0.08 s) |
| FULL SUITE (with deployment env exports) | **2955 passed** (48.45 s) |
| Portable collect | **2954 collected, 1 deselected** |
| Worktree | clean; only the in-scope files changed |

## 6. Live push / PR evidence (known BEFORE the fix push)

- Branch: `argent/ea3e0f45-d42b-426a-99cb-0b294abd0945-i3c2a-ci-bootstrap`
- Pushed head: `4c0ad046667acd60eed8ba688a29246f256bd234`
- PR: **#2** OPEN, base `main`
- Readback: readback SHA == pushed head `4c0ad046…` (consistent)
- Duplicate push: idempotent (no drift)
- Duplicate create-PR: reconciled to **#2** (`RECONCILED_SUCCESS`)
- Merge policy: `OWNER_GATE_REQUIRED` (`SENSITIVE_ACTION`, never executed)
- PR #1: OPEN, unchanged @ `e825111b`
- `main`: unchanged `ffc26642`

> **First real CI run (head `4c0ad046`) FAILED — fail-closed worked:** 13
> failed, 2934 passed, 6 skipped. The 13 failures are 5 environmental root
> causes (see NOTES §7.3); the fixes in this round are the response.

**⚠ AWAITS MAIN:** final fix push readback, and green CI on the fixed head
(`__TBD__`).

## 7. Broker audit summary (known BEFORE the fix push)

- Ledger + logs: `~/.local/state/argent/i3c2a/`
- Actions recorded: push (idempotent), create-PR (reconciled to #2
  `RECONCILED_SUCCESS`), merge policy `OWNER_GATE_REQUIRED`
  (`SENSITIVE_ACTION`, never executed).

**⚠ AWAITS MAIN:** post-fix-push broker audit entries (final readback,
green-CI observation) — `__TBD__`.

## 8. Credential isolation re-probe

**⚠ AWAITS MAIN:** `__TBD__` (re-probe after the final fix push).

## 9. Secret scan

**⚠ AWAITS MAIN:** `__TBD__` (scan after the final fix push).

## 10. Sol review findings (HIGH/LOW — closed this round)

An independent Sol HIGH review found 2 HIGH + 3 LOW. All are closed in this
fix round:

- **HIGH-1** — `actions/checkout@v7.0.1` without `persist-credentials: false`
  persisted the ephemeral token into `.git/config`; docs overstated the token
  boundary. **CLOSED:** `persist-credentials: false` (CASE 19) + honest token
  boundary in NOTES §1.1.
- **HIGH-2** — non-canonical parser (missed `run: |` bodies, no exact step-set
  assertion, allowed hidden steps). **CLOSED:** canonical exact step-set parser
  + key allowlists (CASE 18).
- **LOW-3** — mutable tag pins. **CLOSED:** immutable full-commit SHAs
  (checkout `3d3c42e…`, setup-python `5fda3b9…`), tag kept as inline comment.
- **LOW-4** — I3-C2-B must bind check provenance. **CLOSED:** NOTES §6.1
  (name + app slug `github-actions` + workflow path).
- **LOW-5** — provider-token residual not precisely documented. **CLOSED:**
  NOTES §10 (exact PAT scopes, no rotation, fine-grained PAT/GitHub App as
  pre-I4 hardening).

## 11. Regression counts

**Local (this writer, fix round):** targeted **19 passed**; full suite **2955
passed**.

**⚠ AWAITS MAIN:** regression counts on the final fixed head + green CI
(`__TBD__`), and the closing commit (`__TBD__`).

## 12. Closing statement

This phase ends **GATE — `CI_BOOTSTRAP_PR_READY_FOR_OWNER_MERGE`**. The owner
merges **only the bootstrap PR** (no auto-merge). **I3-C2-B must NOT start
automatically** — it begins only after owner merge.

**GREEN is marked only by Main** after: final fix push readback, green CI on
the fixed head, broker audit, credential re-probe, secret scan, Sol review
confirmation, and full regression. This writer filled everything known BEFORE
the fix push; Main completes the remainder.
