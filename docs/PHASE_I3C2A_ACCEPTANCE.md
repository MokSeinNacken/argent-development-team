# PHASE I3-C2-A ACCEPTANCE — Minimal Safe GitHub Actions CI Bootstrap

**Branch:** `phase-i3c2a-ci-bootstrap` (Base `29de775ad12088cba13cf2285a6514b12f9d770d` = Phase I3-C1 GREEN).
**Datum:** 2026-09-04.
**Scope:** ADDITIVE only (one workflow + one test module + two docs). No commit,
no push, no GitHub/broker/systemd/service/state-dir mutation, no LLM agents. The
PR carrying this workflow is created **later** by the supervisor via the trusted
external-action broker.

**STATUS: PENDING — completed by Main after the live flow.** GREEN marker (only
after Main): `ARGENT_PHASE_I3C2A_CI_BOOTSTRAP_GREEN`. No `ARGENT_PHASE_I3_GREEN`.

---

## 1. Deliverables

- **NEW** `.github/workflows/ci.yml` — minimal, safe, read-only CI workflow
  (`on: pull_request → branches: [main]`, `permissions: contents: read`, single
  job `test`, fail-closed `python -m pytest tests/ -q`, `timeout-minutes: 15`,
  Python `'3.14'`, pytest `==9.1.1`, only `actions/checkout@v7.0.1` +
  `actions/setup-python@v7.0.0`).
- **NEW** `tests/test_phase_i3c2a_ci_workflow.py` — deterministic stdlib-only
  structural security validation (17 cases → 17 test functions).
- **NEW** `docs/PHASE_I3C2A_NOTES.md` — design decisions, evidence, check
  identity, limitations.
- **NEW** `docs/PHASE_I3C2A_ACCEPTANCE.md` — this file.

## 2. Check identity (the source of truth I3-C2-B will use)

| Field | Value |
|---|---|
| workflow `name` | `Argent CI` |
| trigger | `pull_request` → `branches: [main]` |
| top-level `permissions` | `contents: read` |
| job id | `test` (no display-name override) |
| required check name | **`test`** |
| test command | `python -m pytest tests/ -q` (fail-closed) |
| timeout | `timeout-minutes: 15` |
| trusted actions | `actions/checkout@v7.0.1`, `actions/setup-python@v7.0.0` |

## 3. Security case → test mapping (17 cases)

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
| 12 | only trusted actions (exact set) | `test_case12_only_trusted_actions` |
| 13 | required-check identity (name/job/CHECK_NAME) | `test_case13_required_check_identity` |
| 14 | workflow path exactly `.github/workflows/ci.yml` | `test_case14_workflow_path_exact` |
| 15 | PR content cannot become command authority | `test_case15_pr_content_cannot_become_command_authority` |
| 16 | no schedule/workflow_dispatch/repository_dispatch | `test_case16_no_other_triggers` |
| 17 | no env block / GITHUB_TOKEN / env injection | `test_case17_no_env_or_github_token` |

## 4. Deterministic test counts

| Group | Result |
|---|---|
| I3-C2-A targeted | **17 passed** |
| **FULL SUITE** | **2953 passed** (2936 baseline + 17 new) |

## 5. Local verification (this writer)

| Prüfung | Ergebnis |
|---|---|
| YAML syntax (`yaml.safe_load`) | OK — top-level keys `['True', 'jobs', 'name', 'permissions']` (`on`→`True` YAML 1.1 boolean quirk, expected; repo tests are text-based) |
| I3-C2-A targeted | **17 passed** (0.07 s) |
| FULL SUITE (with deployment env exports) | **2953 passed** |
| Worktree | clean; only the 4 deliverables added |

## 6. PLACEHOLDER — live push / PR evidence (completed by Main)

- Branch: `__TBD__`
- Remote SHA (pushed head): `__TBD__`
- PR number: `__TBD__`
- Readback: `__TBD__`

## 7. PLACEHOLDER — broker audit summary (completed by Main)

`__TBD__`

## 8. PLACEHOLDER — credential isolation re-probe (completed by Main)

`__TBD__`

## 9. PLACEHOLDER — secret scan (completed by Main)

`__TBD__`

## 10. PLACEHOLDER — Sol review findings (completed by Main)

`__TBD__`

## 11. PLACEHOLDER — regression counts (completed by Main)

`__TBD__`

## 12. Closing statement

This phase ends **GATE — `CI_BOOTSTRAP_PR_READY_FOR_OWNER_MERGE`**. The owner
merges **only the bootstrap PR** (no auto-merge). **I3-C2-B must NOT start
automatically** — it begins only after owner merge.
