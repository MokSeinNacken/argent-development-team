# PHASE I2 ACCEPTANCE — Integration / Merge Queue Core

**Branch:** `phase-i2-integration-merge-queue` (Base `0c8ee0ad` = Phase I1 GREEN).
**Datum:** 2026-09-03.
**Scope:** code + deterministic tests + docs + local demo only. **No commit, no
push, no live service/systemd/state-dir mutation, no network writes, no provider
credentials, no LLM agents.**

**STATUS: NOT GREEN** — independent verification + Sol closing review pending
(Main).

---

## 1. Deliverables

- **NEW** `argent_core/integration_candidate.py` — candidate state model
  (`CandidateState`), `MergeClassification`, `IntegrationCandidate`,
  `GitClient` (argv-only), `classify_merge` (authoritative git),
  `deterministic_order` (dependency→FIFO→priority→stale).
- **NEW** `argent_core/merge_queue.py` — the single integration authority
  (`MergeQueue`): controller-authoritative admission, fenced transitions,
  action-lock boundary, dedicated integration worktree, merge + fresh TestPlan
  + tests, serial target processing, conservative recovery.
- **EXTENDED** `argent_core/store.py` — SCHEMA 20 → 21; additive
  `integration_candidates` table (+ 3 indexes); `create/get/list/
  transition_integration_candidate` primitives (revision-fenced).
- **Tests** `tests/test_phase_i2_candidate.py` (14), `_admission.py` (9),
  `_git.py` (9), `_queue.py` (10), `_recovery.py` (8), `_isolation.py` (3),
  `_migration.py` (4) — **57 I2 tests**, each mapped to a brief case (below).
- **Docs** `docs/PHASE_I2_NOTES.md` (analysis A–H, design, demo, limits,
  I3 boundary) + this file.
- **Demo** `docs/i2_demo.py` (local controlled demo: happy path + conflict).

## 2. Candidate model + queue state machine

`CandidateState`: `PENDING → READY → INTEGRATING → INTEGRATED`, with terminal
`CONFLICTED` / `STALE` / `BLOCKED` / `FAILED` and re-evaluable `PENDING`/`STALE`.
This is a SEPARATE state machine stored in `integration_candidates`; the
8-state `job_state.PrimaryState` model is untouched (no new job states).

## 3. Ordering / dependency contract

`deterministic_order`: (1) explicit `depends_on` (topological, A before B when B
depends_on A), (2) FIFO `queue_position`, (3) trusted `priority` (desc), (4)
stale status. Unknown/unintegrated dependencies defer
(`DEPENDENCY_NOT_INTEGRATED`); cycles block. No LLM ordering.

## 4. Lock / authority contract

One integration holder per (repository, integration-target) via the I1 action
lock `repo:<repo>:integrate:<target>` (lease/epoch-fenced, restart-safe,
stale-holder reclaimable). Holder = a currently-leased supervisor job; the queue
never creates jobs (no second scheduler).

## 5. Case → test mapping

| Case | Meaning (brief) | Test(s) |
|---|---|---|
| 1 | Controller-authoritative admission (agent prose never sufficient) | `test_case1_admission_rejects_non_done_source`, `test_case1_admission_requires_provenance`, `test_case1_admission_rejects_open_high_finding`, `test_case1_agent_prose_is_never_sufficient` |
| 2 | Bounded candidate states, separate from primary job states | `test_case2_candidate_states_are_bounded_and_separate` |
| 3 | Deterministic candidate id + idempotent creation | `test_case3_candidate_id_deterministic`, `test_enqueue_idempotent_and_starts_pending` |
| 4 | Ordering: dependencies before dependents | `test_case4_dependency_orders_before_dependent` |
| 5 | Ordering: FIFO queue position | `test_case5_fifo_queue_position_when_no_dependency` |
| 6 | Ordering: trusted priority tie-break | `test_case6_priority_tiebreak_after_fifo` |
| 7 | Ordering: unknown/cycle → defer/block (no LLM) | `test_case7_unknown_dependency_defers_no_llm_ordering`, `test_case7_cycle_is_blocked_conservatively` |
| 8 | Single integration authority (one holder per target) | `test_case8_single_holder_per_target` |
| 9 | Action lock lease-fenced, stale-holder reclaim | `test_case9_stale_holder_reclaimed` |
| 10 | Not a second job scheduler | `test_case10_integration_is_not_a_second_scheduler` |
| 11 | No second source of truth (store rows; git is evidence) | `test_case11_store_is_single_source_of_truth` |
| 12 | Dedicated integration worktree (never writer worktree) | `test_case12_integration_uses_dedicated_worktree_not_writer` |
| 13 | Target branch never mutated | `test_case13_target_branch_never_mutated` |
| 14 | Integration branch ≠ target | `test_case14_integration_branch_never_equals_target` |
| 15 | Conflict detection via authoritative git (merge-tree) | `test_case15_conflict_detected_via_git`, `test_classify_merge_real_git_classifications` |
| 16 | No blind ours/theirs, no force-rebase | `test_case16_no_blind_ours_theirs_or_force_rebase` |
| 17 | No partial authoritative INTEGRATED on conflict | `test_case15_conflict_detected_via_git` (asserts `integrated_head is None`) |
| 18 | Multi-candidate sequential A→tests→B | `test_case18_sequential_a_then_b` |
| 19 | Failed-candidate isolation | `test_case19_failed_candidate_isolated` |
| 20 | No global rollback of integrated evidence | `test_case19_failed_candidate_isolated` |
| 21 | Fresh integration TestPlan (new snapshot) | `test_case21_integration_plan_is_fresh_snapshot` |
| 22 | Plan built via Phase F `test_planning` | `test_case22_default_plan_builder_uses_test_planning` |
| 23 | Stale source-worktree PASS cannot close integration | `test_case23_stale_source_pass_cannot_close_integration` |
| 24 | Two green candidates not assumed jointly green (combined snapshot) | `test_case24_combined_snapshot_failure_detected` |
| 25 | Failed candidate does not corrupt already-integrated evidence | `test_case19_failed_candidate_isolated` |
| 26 | Restart before integration begins | `test_case26_queue_survives_restart_before_integration`, `test_case26_restart_and_process` |
| 27 | Crash after checkout/preparation | `test_crash_window_resets_conservatively[after checkout]` |
| 28 | Crash during git op (mid-merge) | `test_crash_window_resets_conservatively[mid-merge]` |
| 29 | Crash after integrated snapshot, before tests | `test_crash_window_resets_conservatively[after snapshot]` |
| 30 | Crash after tests, before state update | `test_crash_window_resets_conservatively[after tests]` |
| 31 | Idempotent duplicate processing | `test_case31_idempotent_duplicate_processing`, `test_reconcile_clears_in_flight_then_reintegrate` |
| 32 | Git argv-only | `test_case32_git_client_builds_argv_only` |
| 33 | Validate repo/worktree/branch/ref/SHA/path | `test_case33_ref_validation_fail_closed`, `test_case33_path_injection_fail_closed` |
| 34 | No shell/eval/exec | `test_case34_merge_tree_rejects_invalid_sha` (+ existing `test_no_shell_true_in_product_code`) |
| 35 | Review independence (writer can't approve own) | `test_case35_high_risk_requires_independent_review`, `test_case35_normal_risk_no_review_required` |
| 36 | Holder-checked (stale holder can't commit) | `test_case36_stale_holder_cannot_drive_integration` |
| 37 | Single authority + one-INTEGRATING-per-target | `test_case37_lock_boundary_is_per_repo_target`, `test_case37_one_integrating_per_target_unique_index` |
| 38 | Resource Governor binding during integration tests | `test_case38_resource_governor_gate_used` |
| 39 | Per-candidate context/routing evidence never overwrites another | `test_case39_candidate_evidence_isolated`, `test_case39_revision_fence_prevents_stale_overwrite` |
| 40 | Terminal source job immutable after integration failure | `test_case40_source_job_terminal_immutable_after_failure`, `test_case40_source_head_change_invalidates_candidate` |

Schema migration (additive 20→21) is covered by `tests/test_phase_i2_migration.py`
(4 tests).

## 6. Deterministic test counts

| Group | Result |
|---|---|
| I2 candidate (model/order/git-safety) | 15 passed |
| I2 admission (CASE 1/35/40 + HIGH-4) | 11 passed |
| I2 git (CASE 12–17/21–23 + HIGH-6/8/9) | 12 passed |
| I2 queue (CASE 8–11/18/19/24/38 + HIGH-1/5/7) | 15 passed |
| I2 recovery (CASE 26–31 + HIGH-2) | 10 passed |
| I2 isolation (CASE 37/39) | 3 passed |
| I2 migration (20→21) | 4 passed |
| **I2 total** | **70 passed** |
| **FULL SUITE** | **2708 passed** (baseline 2638 + 70) in ~51 s |

## 7. Intentionally updated existing tests (documented)

- `tests/test_phase_d3_regression.py::test_regression_schema_version` — schema
  version assertion 20 → 21 (additive I2 table).
- `tests/test_phase3c_approval_core.py::test_fresh_db_...` — schema version
  assertion 20 → 21 (comment updated).
- `tests/i2_helpers.py` — fixtures made **truthful** for admission's new
  authoritative git-evidence verification (I2 HIGH-3): `make_source` ensures
  the recorded source branch exists at the recorded head; `init_repo`'s base
  test imports `app` correctly; `make_mq`/`pass_test_runner` now sign the
  authenticated integration-evidence contract (I2 HIGH-7).
- `tests/test_phase_i2_queue.py::test_case24_*` — rewritten to actually run the
  REAL pytest suite and prove the combined (A+B) snapshot fails (I2 HIGH-6),
  not merely that both files exist.
- `tests/test_phase_i2_isolation.py::test_case39_candidate_evidence_isolated` —
  isolation is now asserted via per-candidate authenticated evidence summaries
  (the old ``plan_hash`` inequality no longer applies under the authenticated
  contract).
- Inline fake runners in `_queue.py`/`_git.py` updated to emit the signed
  evidence contract (via `i2_helpers.make_test_evidence`) — extended, not
  bypassed.

No other existing test was changed; `test_phase_g2_sandbox`/`g3`/`i1` semantics
are untouched.

## 8. Bounded local demo (§24)

`docs/i2_demo.py` (no network/push/user-projects/stress) ran: Base → A + B →
merge queue → dedicated integration worktree → FIFO → clean integrated HEAD
(A = CLEAN_APPLY, B = DIVERGED_CLEAN) → fresh TestPlan → real pytest PASS,
target `main` untouched; plus C + D conflict → CONFLICTED, `integrated_head=None`.
Evidence is now signed with a demo MAC key (I2 HIGH-7).

## 9. Closing review findings (7 HIGH + 2 LOW) — fixed, verified by tests

The independent read-only Sol closing review reported the following; all are
CONFIRMED by Main's spot-verification and FIXED here, each pinned by a
deterministic regression test:

- **HIGH-1 (stale holder can finalize)** — every authoritative queue transition
  now re-verifies the holder's live job lease AND action-lock ownership
  atomically (`store.transition_integration_candidate_authoritative`); a lost
  lease/lock aborts to a bounded `holder_lease_or_lock_lost` (never
  INTEGRATED). Test: `test_high1_stale_holder_cannot_finalize_after_takeover`.
- **HIGH-2 (reconcile incomplete/corrupting)** — `reconcile_target` is now
  per-candidate: a live holder (lease + lock) is preserved; a stale holder is
  reset with holder fields **explicitly cleared** (`clear_holder=True`) and the
  stale lock reclaimed truthfully (`reclaim_stale_action_lock`). Tests:
  `test_reconcile_preserves_live_holder`,
  `test_reconcile_resets_stale_holder_and_reclaims_lock`.
- **HIGH-3 (provenance shape-checked not verified)** — admission now verifies
  authoritative git evidence (worktree exists, `--show-toplevel` realpath ==
  repo identity, source head resolves, branch tip == source head, base is an
  ancestor, not dirty); repo identity is realpath-canonicalised for lock
  keys/worktree naming; `classify_merge` validates claimed base BEFORE
  `CLEAN_APPLY`. Tests: admission fixtures made truthful; `test_case33_path_*`
  (path injection).
- **HIGH-4 (lowercase severities bypass admission)** — severity is canonicalised
  case-insensitively and covers `high` **and** `critical` open findings. Tests:
  `test_admission_rejects_lowercase_open_high_finding`,
  `test_admission_rejects_open_critical_finding` (uppercase still passes via
  canonicalisation, not masking).
- **HIGH-5 (dependency satisfied before candidate integrates)** — dependency
  satisfaction now requires the prerequisite's **candidate** to be INTEGRATED
  (source-job terminal-DONE is no longer sufficient); cycle members are
  transitioned to candidate BLOCKED. Tests:
  `test_high5_dependency_requires_prerequisite_candidate_integrated`,
  `test_high5_cycle_members_transition_to_blocked`.
- **HIGH-6 (TestPlan not closing / no risk inheritance)** — the integration
  plan is built `phase_closing=True` and inherits the source task's
  `risk_class` (union) so HIGH-risk integration forces the broad closing full
  suite (`ChangeEvidence.risk_class` folded into `derive_change_impact`).
  Tests: `test_high6_plan_is_phase_closing_and_inherits_risk`,
  `test_case24_combined_snapshot_failure_detected` (two individually-green
  candidates whose COMBINED snapshot fails).
- **HIGH-7 (Phase-F evidence discarded before durable closure)** — the default
  runner persists bounded authenticated evidence via the REAL `EvidenceStore`
  at a durable path under the worktrees root; `_integrate_locked` verifies the
  runner evidence carries the expected `plan_hash` + a valid keyed MAC
  (fail-closed). Tests: `test_high7_unauthenticated_evidence_rejected`,
  `test_high7_wrong_plan_hash_rejected`; fake runners extended to sign.
- **LOW-8 (git/path validation + branch ownership)** — `GitClient` validates/
  canonicalises every repository/worktree path (realpath, existing dir, optional
  allowed root); branch validation rejects option-like `-`, `..`, `~`, `^`,
  `:`, `@{`, glob/control chars; integration branch naming is documented as
  `integration/<target>` == `integration/<queue-id>`; an existing integration
  branch is never force-deleted unless provably owned. Tests:
  `test_case33_path_injection_fail_closed`,
  `test_low8_unowned_integration_branch_not_force_deleted`.
- **LOW-9 (REBASE_REQUIRED disposition undefined)** — the classification is
  renamed `DIVERGED_CLEAN` and its disposition is explicit: proceed with a
  NORMAL `--no-ff` merge commit (never a rebase/history rewrite; merge-tree
  already proved the three-way merge is clean). Test:
  `test_high9_diverged_clean_uses_normal_merge_not_rebase`.

## 10. NOT GREEN

Independent verification + Sol closing review pending (Main).

## 11. Main Independent Verification + GREEN (2026-09-03)

Main (Supervisor) führte nach dem Fix-Round unabhängig aus (nicht Writer-/Fix-Round-Zahlen):

| Prüfung | Ergebnis |
|---|---|
| I2 targeted (7 Dateien) | **70 passed** (eigener Lauf, 6.3 s) |
| Gruppen B/C/D/E/F/G1/G2/G3/I1/I2 | B 166 · C 296 · D 243 · E 208 · F 223 · G1 78 · G2 69 · G3 29 · I1 70 · I2 70 |
| übrige Dateien | 1256 passed |
| **FULL SUITE** | **2708 passed (52.25 s, eigener Lauf nach Fix-Round)** |
| Bounded Demo (docs/i2_demo.py, reproduziert) | Happy Path: A INTEGRATED (CLEAN_APPLY) → B INTEGRATED (DIVERGED_CLEAN, normal --no-ff), kombinierter Snapshot mit frischem phase-closing TestPlan (plan_hash 6882be25…), `main` unangetastet; Conflict-Fixture: D CONFLICTED, `integrated_head=None`, kein partieller INTEGRATED-Zustand |
| Diff-Review Fix-Round | HIGH-1..7 + LOW-8/9 im Code verifiziert: transition_integration_candidate_authoritative (Lease+Lock-Re-Verify je Write, _HolderLostError → holder_lease_or_lock_lost), Reconcile per Candidate mit clear_holder + reclaim_stale_action_lock, _git_evidence_errors + _canonical_repo + Base-Validierung vor CLEAN_APPLY, Severity-Kanonisierung (high/critical, open), Dependency via Candidate-INTEGRATED + Cycle→BLOCKED, phase_closing=True + risk_class-Vererbung (CASE-24 real), authentifizierte Evidence (Keyed MAC + plan_hash + durable EvidenceStore), GitClient-Pfad-Kanonisierung + Branch-Ref-Validierung + kein Force-Delete unowned Branches, REBASE_REQUIRED→DIVERGED_CLEAN |

Exit-Kriterien (§32 des I2-Briefs): IntegrationCandidate ✓ · Eligibility controller-authoritative (Agent-Prosa ohne Autorität, HIGH-3-Git-Verifikation) ✓ · durable Merge Queue (SCHEMA 21 additiv) ✓ · deterministische Ordnung (depends_on→FIFO) ✓ · Dependencies erzwungen (HIGH-5: Prerequisite-Candidate muss INTEGRATED sein) ✓ · ein Holder pro Repo/Target (action_locks) ✓ · Action-Lock-Fencing (HIGH-1: jede autoritative Transition lease+lock-geprüft) ✓ · dediziertes Integrations-Worktree ✓ · Writer-Worktree nie Merge-Autorität ✓ · stable/main geschützt (Demo: untouched) ✓ · Stale-Base-Erkennung + DIVERGED_CLEAN-Disposition ✓ · deterministische Konflikt-Erkennung (Git) ✓ · keine blinde Auto-Konfliktauflösung ✓ · frischer TestPlan je integriertem Snapshot (phase_closing, risk_class) ✓ · kein stale Source-PASS-Reuse (HIGH-7 MAC/plan_hash) ✓ · Review-Independence (HIGH-4 Severities; Writer kann eigene Integration nicht approven) ✓ · Restart-/Crash-Recovery konservativ (HIGH-2 per Candidate) ✓ · Stale Holder kann nicht finalisieren (HIGH-1) ✓ · Git-Pfade/Refs validiert (LOW-8) ✓ · kein shell=True/eval/exec ✓ · Queue-Isolation pro Repo (CASE 9/37) ✓ · Resource Governor bindend (CASE 38) ✓ · Context/Model/Test-Isolation ✓ · Clean-Demo grün ✓ · Conflict-Demo grün ✓ · I2 targeted grün ✓ · I1–G–F–E–D–C–B grün ✓ · Full Suite grün ✓ · genau 1 Sol-Review (7 HIGH + 2 LOW, alle in genau EINER Fix-Round geschlossen) ✓ · 0 ungelöste HIGH/CRITICAL ✓ · genau 1 lokaler Commit (s. git log HEAD) ✓ · kein Push ✓ · Worktree clean ✓.

Marker: `ARGENT_PHASE_I2_MERGE_QUEUE_GREEN`. KEIN `ARGENT_PHASE_I_GREEN`.
