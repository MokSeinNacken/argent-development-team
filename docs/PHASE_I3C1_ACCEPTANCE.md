# PHASE I3-C1 ACCEPTANCE — CI External-Wait / PR Lifecycle Core (GitHub READ-ONLY)

**Branch:** `phase-i3c1-ci-external-wait` (Base `2b804a13` = Phase I3-B GREEN).
**Datum:** 2026-09-03.
**Scope:** code + deterministic tests + docs. **No commit, no push, no
live-service/systemd/state-dir mutation, no GitHub writes, no LLM agents.**
Bounded GitHub READS (read-only) permitted for the live PR #1 probe only.

**STATUS: I3-C1 GREEN — ARGENT_PHASE_I3C1_CI_EXTERNAL_WAIT_GREEN (Main-verifiziert nach Fix-Round + unabhängiger Verifikation; kein I3-/I-GREEN).** GREEN is marked ONLY by Main after independent
verification + the live read-only PR #1 probe + 1× Sol HIGH review (and a fix
round if required). Marker (only after Main):
`ARGENT_PHASE_I3C1_CI_EXTERNAL_WAIT_GREEN`. No `ARGENT_PHASE_I3_GREEN`.

---

## 1. Deliverables

- **NEW** `argent_core/ci_external_wait.py` — normalized `CiState` model
  (12 states), `CiCheck`/`CiRead`/`CiSnapshot`, deterministic
  `aggregate_ci_state` + `classify_ci_failure`, `CiWaitAdapter` protocol,
  `FakeCiAdapter`, trusted `CiWaitSpec`, `CiWaitManager` (trusted non-LLM
  controller), `GitHubCiAdapter` (READ-ONLY, argv `gh`).
- **EXTENDED** `argent_core/store.py` — SCHEMA 22 → 23 (additive nullable
  `ci_policy`/`ci_evidence` columns on `external_waits`); `kind` filter on
  `list_due_external_waits`; evidence-aware `complete_wait_and_requeue`.
- **EXTENDED** `argent_core/external_wait.py` — `_build_wait_row` emits the new
  columns; `ExternalWaitManager` gains an optional `kinds` filter (default
  unchanged).
- **EXTENDED** `argent_core/background_runtime.py` + `argent_service.py` — the
  CI wait manager runs in the same bounded loop; `build_service` wires a
  CI-scoped external manager + a dedicated CI manager.
- **Deployment-tracking defaults** — `tests/test_phase_g2_unit_static.py` +
  `g2-systemd/install-check.sh` now default `ARGENT_WORKTREE`/`ARGENT_DOC` to
  the **phase-i3b-github-live-acceptance** worktree / `PHASE_I3B_ACCEPTANCE.md`
  (env-parameterizable), tracking the authorized Phase-I3-B deploy.
- **Tests** `tests/test_phase_i3c1_ci_state.py` (20), `_wait.py` (28),
  `_adapter.py` (8), `_migration.py` (4), `_runtime.py` (2) — **62 I3-C1 tests**.
- **Docs** `docs/PHASE_I3C1_NOTES.md` + this file.

## 2. Wait-identity / CI-state / aggregation contracts (summary)

- **Identity:** `provider` + `ref` (`owner/repo#<pr_number>`) + `expected_subject`
  (bound head SHA) + `ci_policy` (expected base, required/optional checks,
  candidate id) + `job_id`. A CI wait requires a non-empty SHA-like subject.
- **CI state model:** closed 12-state set (see NOTES §B); hard rules enforced
  (NO_CHECKS_CONFIGURED≠SUCCESS, UNKNOWN≠SUCCESS, PROVIDER_UNAVAILABLE/RATE_LIMITED≠
  CODE_FAILURE, CANCELLED≠SUCCESS, no "looks green").
- **Aggregation:** pure `aggregate_ci_state(checks, required, optional)` with the
  deterministic priority order FAILURE > CANCELLED > TIMED_OUT > ACTION_REQUIRED >
  UNKNOWN(neutral/skipped/stale) > UNKNOWN(missing) > PENDING > SUCCESS (a
  terminal non-success wins over a missing required check — never a masked
  failure); an empty required set is conservative (never SUCCESS from a partial
  universe — a terminal non-success is still reported, else UNKNOWN); unknown
  required set ⇒ UNKNOWN.
- **Wake-once:** evidence persisted atomically with `complete_wait_and_requeue`
  (terminal + requeue in one transaction); idempotent; terminal waits are never
  re-listed; `event_version` is the durable transition identity.

## 3. Case → test mapping (50 cases)

| Case | Meaning | Test(s) |
|---|---|---|
| 1 | CiState closed 12-state set + closed conclusion/status/classification sets | `test_ci_state_closed_set_exact`, `test_check_conclusion_and_status_closed_sets`, `test_failure_classification_closed_set` |
| 2 | NO_CHECKS_CONFIGURED != SUCCESS | `test_no_checks_configured_is_not_success` |
| 3 | UNKNOWN != SUCCESS (unknown requirement set) | `test_unknown_requirement_set_is_not_success`, `test_unknown_requirement_set_conservative_through_manager` |
| 4 | PROVIDER_UNAVAILABLE != CODE_FAILURE | `test_provider_unavailable_is_not_code_failure` |
| 5 | RATE_LIMITED != CODE_FAILURE | `test_rate_limited_is_not_code_failure` |
| 6 | CANCELLED != SUCCESS | `test_cancelled_is_not_success` |
| 7 | all required SUCCESS ⇒ SUCCESS | `test_all_required_success_is_success` |
| 8 | any required FAILURE ⇒ FAILURE | `test_any_required_failure_is_failure` |
| 9 | any required PENDING ⇒ PENDING | `test_any_required_pending_is_pending` |
| 10 | required check missing ⇒ UNKNOWN | `test_missing_required_is_unknown` |
| 11 | required NEUTRAL/SKIPPED ⇒ UNKNOWN | `test_required_neutral_or_skipped_is_unknown` |
| 12 | optional failure never fails aggregate | `test_optional_failure_never_fails_aggregate` |
| 13 | empty required + no checks ⇒ NO_CHECKS_CONFIGURED | `test_required_empty_and_no_checks` |
| 14 | empty required + all success ⇒ SUCCESS | `test_required_empty_and_all_success` |
| 15 | empty required + observed failure ⇒ FAILURE | `test_required_empty_but_observed_failure_is_failure` |
| 16 | TIMEOUT / CANCELLED classification | `test_classify_timed_out_and_cancelled` |
| 17 | infra vs code vs unknown (partial) classification | `test_classify_infra_vs_code_vs_unknown` |
| 18 | provider error precedence over check signal | `test_classify_provider_error_precedence` |
| 19 | canonical ref + fail-closed parsing | `test_ci_ref_roundtrip_and_malformed` |
| 20 | atomic enter + full identity binding | `test_enter_ci_wait_atomic_and_binds_identity` |
| 21 | unallowlisted provider can never be entered | `test_enter_ci_wait_rejects_unallowlisted_provider` |
| 22 | bad head SHA / base / required checks rejected | `test_enter_ci_wait_rejects_bad_identity_fields` |
| 23 | failed transition rolls back (no half-wait) | `test_enter_ci_wait_rollback_on_bad_epoch` |
| 24 | WAITING_EXTERNAL holds no LLM/role-run/scope | `test_waiting_job_not_claimable_and_no_agent_dispatch` |
| 25 | pending poll ⇒ backoff, no wake, no LLM | `test_pending_backoff_no_wake` |
| 26 | UNKNOWN aggregate ⇒ backoff, no wake | `test_unknown_aggregate_backoff_no_wake` |
| 27 | NO_CHECKS_CONFIGURED ⇒ backoff (not success) | `test_no_checks_configured_backoff_not_success` |
| 28 | SUCCESS wakes exactly once | `test_ci_success_wakes_exactly_once` |
| 29 | FAILURE persists failing-check evidence + classification before wake | `test_ci_failure_wakes_with_failing_evidence` |
| 30 | cross-repo isolation (Repo A never wakes Repo B) | `test_cross_repo_never_wakes` |
| 31 | cross-PR isolation (PR #1 never wakes PR #2) | `test_cross_pr_never_wakes` |
| 32 | CANCELLED wakes (not success) | `test_ci_cancelled_wakes_not_success` |
| 33 | ACTION_REQUIRED wakes with OWNER_REQUIRED | `test_ci_action_required_wakes_with_owner_required` |
| 34 | head-SHA change ⇒ STALE wake + invalidate prior evidence | `test_head_sha_change_wakes_stale_and_invalidates_prior` |
| 35 | PR closed ⇒ conservative wake | `test_pr_closed_wakes_unexpected_mutation` |
| 36 | PR merged ⇒ distinct unexpected-mutation wake | `test_pr_merged_wakes_distinct_unexpected` |
| 37 | required-check set materially changed ⇒ wake | `test_required_check_set_materially_changed_wakes` |
| 38 | provider outage ⇒ keep waiting, classify PROVIDER, no LLM | `test_provider_unavailable_keeps_waiting` |
| 39 | rate limit ⇒ keep waiting + respect reset | `test_rate_limit_keeps_waiting_respects_reset` |
| 40 | adapter exception contained; pass continues | `test_adapter_exception_backs_off_and_pass_continues` |
| 41 | malformed read ⇒ backoff, no crash | `test_malformed_read_backs_off_no_crash` |
| 42 | deadline ⇒ wake with EXTERNAL, never DONE | `test_deadline_wakes_with_external_error_class` |
| 43 | wait survives restart (ci_policy round-trips) | `test_wait_survives_restart_and_later_check_works` |
| 44 | reopen before first check keeps wait due | `test_reopen_before_first_check_keeps_wait_due` |
| 45 | wake idempotent (no duplicate task) | `test_wake_is_idempotent_no_duplicate_task` |
| 46 | unknown requirement set conservative through manager | `test_unknown_requirement_set_conservative_through_manager` |
| 47 | GitHub adapter normalization (check-runs/status/PR/merged/head-movement/rate-limit/500) | `test_normalize_check_runs_maps_conclusion_and_event_version`, `test_normalize_statuses_maps_commit_status`, `test_read_ci_state_open_with_checks`, `test_read_ci_state_merged_pr`, `test_read_ci_state_reveals_head_movement`, `test_read_ci_state_rate_limited`, `test_read_ci_state_provider_unavailable` |
| 48 | GitHub CI adapter structurally READ-ONLY (no write path) | `test_github_ci_adapter_has_no_write_path` |
| 49 | schema 22→23 migration (additive columns, idempotent, rows preserved) | `test_fresh_db_lands_on_v23`, `test_v22_to_v23_adds_columns`, `test_existing_rows_preserved_on_migration`, `test_reopen_is_noop` |
| 50 | runtime integration (single scheduler; kinds exclusion; loop wake) | `test_external_wait_manager_excludes_ci_kinds`, `test_ci_wait_integrated_into_runtime_loop` |

## 4. Deterministic test counts

| Group | Result |
|---|---|
| CI state model + aggregation + classification | 26 passed |
| CI wait lifecycle (identity/wake/backoff/lifecycle/outage/crash) | 39 passed |
| GitHub CI adapter (normalization + no-write) | 12 passed |
| Schema migration 22→23 | 4 passed |
| Runtime integration | 2 passed |
| **I3-C1 total** | **83 passed** |
| **FULL SUITE** | **2936 passed** (baseline 2853 + 83) |

## 4b. Sol HIGH-review fix round (5 HIGH + 2 LOW, all closed)

Independent read-only Sol review found 5 HIGH + 2 LOW; all were fixed with
regression tests (deterministic, no schema change):

- **HIGH-1** positive identity binding (head SHA == expected_subject, base ==
  expected_base, pr_state == OPEN) is mandatory before any aggregation;
  missing/mismatched fields fail closed (never SUCCESS).  Tests: wrong base
  (`test_wrong_base_never_wakes_success`), null head
  (`test_null_head_never_wakes_success`), unknown PR state
  (`test_unknown_pr_state_never_wakes_success`), missing base
  (`test_missing_base_never_wakes_success`).
- **HIGH-2** singleton fencing + terminal immutability: the poll path re-checks
  the fence before polling (`test_singleton_loss_before_poll_stale_runtime_cannot_requeue`);
  `complete_wait_and_requeue` and CI backoff/evidence writes are instance-fenced
  and require `terminal_observed_at IS NULL`
  (`test_late_pending_response_cannot_corrupt_terminal_evidence`,
  `test_stale_finalizer_cannot_win`).
- **HIGH-3** empty-required is conservative: never SUCCESS from a partial
  universe; failing checks derived from observed conclusions
  (`test_required_empty_and_all_success` updated,
  `test_required_empty_multiple_success_not_success`,
  `test_empty_required_fast_success_not_success`,
  `test_failure_with_empty_required_stores_failing_check`).
- **HIGH-4** documented aggregation priority (terminal non-success wins over
  missing) (`test_failure_beats_missing_required`, `test_cancelled_beats_missing_required`,
  `test_timed_out_beats_missing_required`, `test_action_required_beats_missing_required`,
  `test_startup_failure_beats_missing_required`).
- **HIGH-5** CI wait entry refuses a job with active dispatch/role-run/process
  (`test_enter_ci_wait_refuses_with_active_process`).
- **LOW-6** malformed/non-dict/empty provider success responses ⇒ provider_error;
  contradictory IN_PROGRESS+SUCCESS is invalid
  (`test_malformed_check_runs_json_yields_provider_error`,
  `test_non_dict_check_runs_yields_provider_error`,
  `test_malformed_pr_view_json_yields_provider_error`,
  `test_contradictory_check_normalizes_then_is_rejected_by_validation`,
  `test_contradictory_in_progress_success_is_malformed`).
- **LOW-7** docs corrected to the post-fix reality (fenced polls,
  terminal-immutable evidence, instance-fenced wake; `ci_policy` only set at
  entry).

## 5. Intentionally updated existing tests (documented)

- `tests/test_phase_d3_regression.py::test_regression_schema_version` — 22 → 23.
- `tests/test_phase_i3a_migration.py` — `SCHEMA_VERSION == "22"` → `"23"`.
- `tests/test_phase_i2_migration.py` — `SCHEMA_VERSION == "22"` → `"23"`.
- `tests/test_phase3c_approval_core.py::test_schema_version_is_15` — 22 → 23.
- `tests/test_phase_g2_unit_static.py` + `g2-systemd/install-check.sh` —
  deployment-tracking defaults advanced to the I3-B worktree/doc (the required
  deployment-default update).
- `tests/test_phase_i3c1_ci_state.py::test_required_empty_and_all_success` —
  updated to the HIGH-3 empty-required semantics (empty required + all SUCCESS ⇒
  UNKNOWN, never SUCCESS).

No other I3-A/I3-B test semantics changed.

## 6. Residual provider authority (OPERATIONALLY REQUIRED, not code-enforced)

`MokSeinNacken/argent-development-team` `main` has **no branch-protection
rulesets**; the classic PAT (`repo`-scope) retains direct push authority to
`main`. The CI core never pretends branch protection exists and never infers
required checks from a non-existent protection config. **Roadmap requirement:**
replace the broad classic PAT with a fine-grained PAT or GitHub App before broad
productive autonomous writes / I4.

## 7. Boundary to I3-C2

READ-ONLY only. Real CI acceptance (a real check-run) + any provider mutation
or protected-branch-dependent scheduler wiring is I3-C2 / later and requires
`REAL_CI_ACCEPTANCE_SETUP_REQUIRED` (or an Owner gate) per §38.

## 8. GREEN (only Main marks this)

Pending Main independent verification + live read-only PR #1 probe + 1× Sol HIGH
review (and a fix round if required). Marker (only after Main):
`ARGENT_PHASE_I3C1_CI_EXTERNAL_WAIT_GREEN`. No `ARGENT_PHASE_I3_GREEN`.

## Main Independent Verification + GREEN (2026-09-03 23:55)

| Prüfung | Ergebnis |
|---|---|
| I3-C1 targeted | **83 passed** (Fix-Round) + Runtime/Adapter-Suites grün |
| **FULL SUITE** | **2936 passed — 6/6 aufeinanderfolgende Läufe grün (je ~57–60 s, Main-eigene Läufe unter Last)** |
| Live-Read-only-PR-#1-Probe (§10/§29) | PR #1 OPEN, head `argent/efe311ca-…-i3b-live-acceptance` @ `e825111b…`, base `main` (`ffc26642…`), MERGEABLE; **check-runs 0, commit-statuses 0 → NO_CHECKS_CONFIGURED** (valides beobachtetes Ergebnis); Rate-Limit 5000/5000; **0 externe Writes** |
| Fix-Round-Code-Review | HIGH-1 positive Identity-Bindung (head==expected_subject, base==expected_base, state==OPEN vor Aggregation) · HIGH-2 Singleton-Fence vor Poll + terminal-immutable Evidence (instance-fenced, Stale-Finalizer None) · HIGH-3 empty-required konservativ (nie SUCCESS aus Teil-Universum; failing-set aus observed) · HIGH-4 Aggregations-Priorität (Terminal-non-success vor missing-required) · HIGH-5 Wait-Entry verweigert bei aktivem Dispatch/Role-Run/Process · LOW-6 Provider-Normalisierung fail-closed (malformed/contradictory) · LOW-7 Docs korrigiert |
| Zusätzliche Main-Härtung (dokumentiert) | Vorbestehender last-sensitiver Test-Flake (Default-REAL-ResourceGovernor bei Scheduler-Unit-Tests; unter Memory-Druck DEFER; an I3-B-Base 2b804a13 mit ~40% Full-Suite-Flake reproduziert, NICHT durch I3-C1 verursacht, Produktions-Single-Supervisor-Pfad unberührt) → deterministische Admission-Injektion (`allow_governor`/`make_deterministic_scheduler` in tests/mock_supervisor_runtime.py) in den Claim-Treibenden Tests (b2_scheduler_recovery, b2_fix_round, b3_fix_round, i1_gate, g1_fix_round, g1_helpers.make_runtime_env); danach 6/6 Full-Suite-Läufe grün |

Exit-Kriterien (§37): finale I3-B-Code live deployed (2b804a13; Instanz ac6775f5→(Deploy I3-B) rev ≥1131) ✓ · Service READY/Singleton ✓ · Credential-Isolation ✓ · CI-Wait nutzt WAITING_EXTERNAL (kein zweiter Scheduler) ✓ · Provider/Repo/PR/Head-Identität gebunden (HIGH-1) ✓ · deterministisches CI-State-Modell ✓ · NO_CHECKS_CONFIGURED != SUCCESS ✓ · UNKNOWN != SUCCESS ✓ · Provider-Outage != CODE-Failure ✓ · Rate-Limit behandelt ✓ · bounded Backoff ✓ · kein Busy-Polling ✓ · kein LLM im Wartezustand (Entry-Refusal bei aktivem Process — HIGH-5; Runtime-Loop ohne LLM) ✓ · exact-Head-CI-Evidence (Snapshot-gebunden) ✓ · stale SHA invalidiert frühere Evidence ✓ · SUCCESS-Wake exactly-once (event_version/instance-fenced) ✓ · FAILURE-Wake exactly-once ✓ · Duplikat-Provider-Response deduped ✓ · Restart-/Crash-Recovery idempotent ✓ · Stale-Holder kann nicht finalisieren ✓ · Cross-Job/Repo-Wake-Isolation ✓ · External Content = DATA ✓ · PR-Lifecycle normiert (OPEN/CLOSED/MERGED/UNKNOWN konservativ) ✓ · keine Notification-Spam ✓ · Read/Write-Provider-Permissions getrennt (Read-Adapter ohne Write-Pfad, CASE 44) ✓ · **0 neue externe GitHub-Writes** ✓ · PR-#1-Read-only-Zustand erfasst (NO_CHECKS_CONFIGURED) ✓ · Fake-Provider-Wait-Demo grün ✓ · I3-C1 targeted grün ✓ · I3-B/I3-A/I2/I1/G/F/E/D/C/B grün (in Full Suite) ✓ · Full Suite grün (6/6) ✓ · genau 1 Sol-HIGH-Review (5 HIGH + 2 LOW, alle in genau EINER Fix-Round geschlossen) ✓ · 0 ungelöste HIGH/CRITICAL ✓ · 1 lokaler Commit ✓ · kein Push ✓ · Worktree clean ✓.

Marker: `ARGENT_PHASE_I3C1_CI_EXTERNAL_WAIT_GREEN`. KEIN `ARGENT_PHASE_I3_GREEN`, KEIN `ARGENT_PHASE_I_GREEN`.
