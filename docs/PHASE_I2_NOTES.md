# Phase I2 — Integration / Merge Queue Core (NOTES)

Base commit: `0c8ee0ad` (Phase I1 GREEN). Worktree: `phase-i2-integration-merge-queue`.

This document records the mandatory read-only analysis (A–H), the design, the
conservative initial policy, the local controlled demo, known limitations and
the boundary to I3/J. It is written by the implementation writer; **GREEN is
marked only by Main after independent verification + Sol review.**

---

## Part 1 — Mandatory read-only analysis (A–H)

### A. What constitutes a completed Writer output today

A writer job is authoritatively complete when `supervisor_jobs.terminal ==
'DONE'` (`primary_state == DONE`), set by the supervisor's closing path only
after the writer result is consumed and the policy tests are satisfied.
Concretely (`store.py` `supervisor_jobs`):

- `terminal` (`'DONE'`/`'FAILED'`/`'BLOCKED'`) is the sticky-terminal authority
  (immutable once DONE/FAILED).
- `result_status` (`SUCCEEDED`/`FAILED`/`CONFLICT`/`NOT_OBSERVED`/…`) +
  `result_consumed` mark result consumption.
- The final HEAD lives in `supervisor_jobs.expected_head` and
  `supervisor_jobs.current_head`; the base is `base_commit`; the branch is
  `branch_identity`; the repository is `repo_identity`; the worktree is
  `canonical_worktree_path`. These are persisted by
  `store.bind_writer_worktree` (real git provenance) and re-read by
  `worktree.GitProvenanceProvider` (read-only, fail-closed).

### B. Where repository/base/SHA/branch/worktree/final HEAD/tests/review evidence lives

- Repository identity → `supervisor_jobs.repo_identity` (canonical realpath).
- Base commit / final HEAD / branch / worktree → `supervisor_jobs.base_commit`,
  `expected_head`, `current_head`, `branch_identity`, `canonical_worktree_path`.
- Tests → `test_runs` (task-scoped, `result`/`source_class`) **and** the Phase F
  `test_execution.EvidenceStore` (keyed-HMAC, snapshot-bound, reusable only on
  exact identity) + `test_planning.TestPlan` (deterministic, plan_hash/plan_mac).
- Review → `reviews` (task-scoped, `verdict`/`source_class`).
- Findings → `findings` (task-scoped, `severity`/`status`/`source_class`) +
  `supervisor_jobs.open_findings_count`.
- Gate → `supervisor_jobs.gate_status`/`gate_scope`/`gate_closed` +
  `owner_approvals`.

### C. Which evidence is missing before integration can be authoritative

1. **Committed HEAD vs dirty worktree** — `GitProvenanceProvider.dirty()` is
   fail-closed, but nothing re-verifies at integration time that the source's
   `expected_head` equals the real git HEAD. I2 adds a source-head re-read and
   re-validation (`merge_queue._integrate_locked` step 2b).
2. **Provenance persistence** — I1 persists repo/base/head per job but does not
   cross-check against the target tip; I2 re-reads the target tip
   (`resolve_sha(target)`) and classifies stale-base.
3. **Review binding** — reviews are task-scoped, not bound to a commit; I2
   requires independent (controller-source) review evidence for HIGH risk.
4. **A dedicated integration worktree / queue** — absent (deferred to I2); I2
   adds `integration_candidates` + the integration worktree.

### D. Which existing action lock protects repository integration

`action_lock_name(ACTION_REPO_GLOBAL, repo_identity=repo,
name=f"integrate:{target}")` → `repo:<repo>:integrate:<target>`, acquired via
`store.try_acquire_action_lock` (I1 lease-fenced CAS; stale-holder atomic
reclaim). This yields exactly ONE integration holder per (repository,
integration-target).

### E. How integration remains restart-safe

- Fenced candidate transitions: every `integration_candidates` transition is a
  revision CAS (`transition_integration_candidate` raises
  `CandidateRevisionError` on drift).
- The I1 action lock is lease/epoch-fenced and stale-holder reclaimable (never
  PID-only).
- Git evidence is re-derived each pass (target tip, source head, integration
  worktree HEAD) — never cached in memory.
- Recovery (`MergeQueue.reconcile_target`) resets in-flight INTEGRATING →
  PENDING conservatively and re-drives integration idempotently (deterministic
  merge + deterministic integration worktree name).

### F. What must invalidate a candidate

- **Stale base** — source `base_commit` is not an ancestor of the current target
  tip → `STALE_BASE`.
- **HEAD mismatch** — source `expected_head != current_head`, or the source head
  changed after candidate creation → `STALE`.
- **Repo / footprint mismatch** — missing/invalid `repo_identity` or missing
  `base_commit`/`source_head`/`branch_identity` → not admissible.
- **Terminal source mutation** — source job no longer `terminal='DONE'` after
  candidate creation → `STALE`.

### G. How dependencies determine merge order

A candidate's `depends_on` is a candidate id (translated from the source job's
I1 single `depends_on`). `deterministic_order` performs a Kahn topological sort
over READY candidates with the brief's exact precedence: (1) dependencies, (2)
FIFO `queue_position`, (3) trusted `priority`, (4) stale status. Unknown/
unintegrated dependencies defer; cycles block. No LLM ordering.

### H. Which operations can use ordinary Git safely (argv only)

`integration_candidate.GitClient` (controller-constructed argv lists, no shell,
no `eval`/`exec`): `rev-parse`, `merge-base`, `merge-tree --write-tree`,
`worktree add/remove`, `checkout`, `merge --no-ff`, `merge --abort`, `reset
--hard`, `diff --name-only`, `branch -D`, `status`. Every ref/SHA/path/branch is
validated before reaching git (`is_sha_like`, `validate_branch_identity`,
`resolve_canonical_worktree_path`).

---

## Part 2 — Design

### 2.1 New pure module `argent_core/integration_candidate.py`

- `CandidateState` (PENDING/READY/INTEGRATING/CONFLICTED/STALE/BLOCKED/
  INTEGRATED/FAILED) — a SEPARATE state machine; the 8-state
  `job_state.PrimaryState` model is untouched.
- `MergeClassification` (CLEAN_APPLY/DIVERGED_CLEAN/CONFLICT/STALE_BASE/
  DEPENDENCY_NOT_INTEGRATED/UNKNOWN).
- `IntegrationCandidate` (frozen, versioned) + `candidate_id_for` (deterministic
  id over repository+target+source_job).
- `GitClient` (argv-only) + `classify_merge` (authoritative git: merge-base +
  merge-tree; no LLM).
- `deterministic_order` (Kahn topological sort + FIFO/priority/stale).

### 2.2 New controller `argent_core/merge_queue.py`

`MergeQueue(store, worktrees_root, *, git, plan_builder, test_runner,
review_policy, mac_key)`:

- `admission_errors` / `enqueue_candidate` — controller-authoritative creation
  from trusted facts (terminal DONE + proven provenance + no open HIGH findings).
- `evaluate_candidate` — PENDING/STALE → READY (dependency + review gate).
- `integrate_candidate` — the fenced single-candidate driver (lock → INTEGRATING
  → classify → worktree → merge → TestPlan → tests → INTEGRATED/FAILED/…).
- `process_target` — serial queue processing per target.
- `reconcile_target` — conservative crash/restart recovery.

### 2.3 Schema (additive, 20 → 21)

New `integration_candidates` table (+ indexes) via the existing `_SCHEMA` /
`_migrate` mechanism:

- `id` (deterministic), `repository`, `integration_target`, `source_job_id`
  (FK → supervisor_jobs), `state` (CHECK over the 8 candidate states),
  `queue_position`, `priority`, `depends_on`, `base_commit`, `source_head`,
  `source_branch`, `integration_worktree_path`, `integration_branch`,
  `integrated_head`, `merge_classification`, `conflict_detail`, `revision`
  (CAS), `holder_owner_instance_id`, `holder_lease_epoch`, `result_json`,
  `last_error_code`, timestamps.
- UNIQUE `(source_job_id, integration_target)`; partial UNIQUE
  `(repository, integration_target) WHERE state='INTEGRATING'` (defensive second
  layer beneath the action lock).

Store methods: `create_integration_candidate`, `get_integration_candidate`,
`get_integration_candidate_for_source`, `list_integration_candidates`,
`transition_integration_candidate` (revision-fenced).

### 2.4 Single integration authority (§2)

One holder per (repository, integration-target) via the I1 action lock
`repo:<repo>:integrate:<target>`; lease/epoch-fenced, restart-safe, stale-holder
reclaimable. Not PID-only, not agent-controlled.

### 2.5 Dedicated integration worktree (§3)

Never inside a Writer worktree. `git worktree add -b integration/<target>
<worktrees_root>/integration-<hash> <base>`; result HEAD on the integration
branch. The target branch (main/master/stable/release/production) is never
written — only read for the tip; promotion is I3/J.

### 2.6 Stale-base + conflict detection (§5)

`classify_merge` with authoritative git: merge-base for stale-base detection;
`merge-tree --write-tree` for the conflict test. No LLM conflict declaration, no
blind ours/theirs, no force-rebase, no `checkout --theirs`. No partial
authoritative INTEGRATED on conflict (the worktree is aborted).

### 2.7 Integration TestPlan (§6)

The integrated snapshot is a NEW snapshot; a fresh TestPlan is built via the
existing Phase F `test_planning` (default `_default_plan_builder` → `build_test_plan`
with the git-derived changed paths + base ref). Stale source-worktree PASS
evidence can never close integration (the runner is handed only the integration
worktree).

### 2.8 Review independence (§7)

Default `review_policy`: a `risk_class='HIGH'` source requires an independent
(controller-source, approving) review; the writer's own agent review never
satisfies it. A clean hook for a later integration reviewer (no huge reviewer
system).

### 2.9 Restart/crash safety (§9)

`reconcile_target` resets INTEGRATING → PENDING (revision-fenced), never infers
INTEGRATED from process disappearance. Idempotent re-integration (deterministic
merge + deterministic worktree name).

### 2.10 Git safety (§10)

Controller-constructed argv only; validate repo/worktree/branch/ref/SHA/path; no
shell/eval/exec.

### 2.11 Constraints honoured

Additive schema only; reuse B/C/D/E/F/G/I1 mechanisms; no new scheduler (the
queue never creates supervisor jobs); no config system; no weakened
fencing/trust/sandbox; single supervisor; integration is not a second source of
truth (candidates are store rows; git is evidence only).

---

## Part 3 — Conservative initial policy (§25)

- Serialize integration per (repository, integration-target): ONE holder (action
  lock) and the queue is processed serially per target.
- Different targets in the same repo (and different repos) are independent
  locks and may progress independently if resources allow.
- No throughput tuning; no priority inversion beyond the FIFO/priority/stale
  order.

---

## Part 4 — Local controlled demo (§24)

`docs/i2_demo.py` (no network, no push, no user projects, no stress) builds
disposable fixture repos in a temp dir and drives the real store + real git
(argv only):

- **Happy path**: Base → Candidate A + Candidate B → merge queue → dedicated
  integration worktree (`integration/main`) → FIFO order → clean integrated
  HEAD (A = CLEAN_APPLY, B = DIVERGED_CLEAN) → fresh TestPlan (real
  `build_test_plan`) → real pytest on the integrated snapshot → PASS. Target
  branch `main` HEAD unchanged.
- **Conflict fixture**: Candidate C + Candidate D both edit `app.py` on the same
  line → C integrates, D is classified CONFLICT via git and marked CONFLICTED
  with `integrated_head=None` (no partial authoritative result).

Output is redacted (short ids, truncated hashes); exits 0 on success and notes
gracefully if git is unavailable.

---

## Part 5 — Known limitations / boundary to I3/J

- No target-branch promotion (I3/J): integration only produces an integrated
  snapshot HEAD on `integration/<target>`; main/master/stable/release/production
  are never advanced.
- Single `depends_on` (no DAG) — mirrors I1.
- No automatic rebase of a STALE candidate (it is marked STALE for re-basing).
- The default integration test path (`_default_test_runner`) uses the real
  `execute_plan` + `PytestRunner`; the demo injects a lighter deterministic
  runner that runs the fixture's own test via `PytestRunner` (documented).
- Review gate is a minimal default; a full independent-reviewer workflow is I3+.
- No merge-queue UI/TUI; no promotion ordering policy beyond the serial queue.
- `result_json` is bounded (≤64 KiB) redacted evidence.

## Part 5a — Closing-review fix round (7 HIGH + 2 LOW)

The independent read-only Sol closing review found 7 HIGH + 2 LOW (all
CONFIRMED by Main).  Fixes (behaviour changes):

1. **HIGH-1** — every authoritative transition inside the integration sequence
   re-verifies holder lease + action-lock ownership atomically
   (`store.transition_integration_candidate_authoritative`); a lost lease/lock
   aborts to `holder_lease_or_lock_lost` (never INTEGRATED).
2. **HIGH-2** — `reconcile_target` is per-candidate: a live holder (lease +
   lock) is preserved; a stale holder is reset with holder columns explicitly
   cleared (`clear_holder=True`) and the stale lock reclaimed truthfully.
3. **HIGH-3** — admission + integration verify authoritative git evidence
   (top-level realpath == repo identity, source head resolves, branch tip ==
   source head, base ancestry, clean); repo identity is realpath-canonicalised
   for lock keys / worktree naming; `classify_merge` validates claimed base
   BEFORE `CLEAN_APPLY`.
4. **HIGH-4** — severity is canonicalised case-insensitively and covers `high`
   AND `critical` open findings.
5. **HIGH-5** — dependency satisfaction requires the prerequisite's CANDIDATE
   to be INTEGRATED (no terminal-DONE shortcut); cycles transition members to
   candidate BLOCKED.
6. **HIGH-6** — the integration TestPlan is `phase_closing=True` and inherits
   the source task's `risk_class` (new `ChangeEvidence.risk_class` folded into
   `derive_change_impact`).
7. **HIGH-7** — the default runner persists authenticated evidence via the real
   `EvidenceStore` at a durable path under the worktrees root; `_integrate_locked`
   verifies the runner evidence carries the expected `plan_hash` + a valid keyed
   MAC (fail-closed).
8. **LOW-8** — `GitClient` validates/canonicalises all repo/worktree paths;
   branch validation rejects `-`, `..`, `~`, `^`, `:`, `@{`, glob/control
   chars; `integration/<target>` == `integration/<queue-id>` (documented); an
   existing integration branch is never force-deleted unless provably owned.
9. **LOW-9** — `REBASE_REQUIRED` renamed `DIVERGED_CLEAN`; disposition explicit:
   proceed with a NORMAL `--no-ff` merge commit (never a rebase).

## Part 6 — Status

**NOT GREEN.** Independent verification + Sol closing review pending (Main).

---

## Appendix — CODE-ENFORCED vs OPERATIONALLY REQUIRED vs OBSERVED LIVE

- **CODE-ENFORCED** (enforced by deterministic code + tests): candidate
  admission from trusted facts (now verified against real git evidence);
  revision-fenced candidate transitions; holder-verified authoritative
  transitions (every integration transition re-checks the live lease + action
  lock); the single-holder action lock (lease/epoch-fenced, stale-holder
  reclaim); argv-only git (no shell/eval/exec) with ref/SHA/path validation;
  authoritative merge-base/merge-tree conflict + stale-base classification;
  dedicated integration worktree + `integration/<target>` branch (target never
  written); unowned-branch fail-closed (never force-delete an unproven branch);
  conservative recovery (INTEGRATING → PENDING, never infer INTEGRATED);
  bounded candidate states; per-candidate `result_json` isolation with
  authenticated evidence (keyed MAC + plan-hash verification); the
  one-INTEGRATING-per-target partial unique index.
- **OPERATIONALLY REQUIRED** (must hold in the deployed supervisor, not
  enforceable by the queue alone): the supervisor provides a real leased holder
  job (the queue never mints one); the Resource Governor gate is wired with the
  real admission decision; a MAC key is provisioned for the default test
  execution path; the worktrees root is the canonical
  `/home/pc/projects/argent-worktrees`; promotion of the target branch is a
  later I3/J gate.
- **OBSERVED LIVE** (shown by the local demo, not a regression guarantee): two
  candidates integrate in FIFO order with clean heads (CLEAN_APPLY +
  DIVERGED_CLEAN), the combined snapshot contains both files, the target
  branch HEAD is unchanged, and a same-file conflict is classified CONFLICT
  with no partial authoritative result.

