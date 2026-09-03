# Phase I1 — Controlled Multi-Job Parallelization Core (NOTES)

Base commit: `ffd87b61` (Phase-G GREEN). Worktree: `phase-i1-controlled-parallelization`.

This document records the mandatory read-only analysis (A–I), the design, the
conservative limits, the bounded real demo evidence, known limitations and the
boundary to I2/I3/I4. It is written by the implementation writer; **GREEN is
marked only by Main after independent verification + Sol review.**

---

## Part 1 — Mandatory read-only analysis (A–I)

### A. What currently prevents two runnable jobs from executing concurrently

There is **no** hard single-dispatch lock; serialisation today is the combined
effect of three mechanisms:

1. **One claim per pass.** `Scheduler.run_pass` (`argent_core/scheduler.py`)
   performs *exactly one safe step of exactly one job* per pass (Design-Vorgabe
   1). `_resolve_target` calls `store.claim_next_job(...)` which atomically
   claims *one* QUEUED job (priority DESC, then FIFO `rowid`) and flips it
   `QUEUED → RUNNING` (`store._do_claim_locked`). The background loop
   (`SupervisorRuntime._run_one_iteration` in `argent_core/background_runtime.py`)
   calls `scheduler.run_pass()` once per iteration and then sleeps. So each
   loop iteration advances at most one job by one step.

2. **Resource-class concurrency limits in the Resource Governor.** On a fresh
   claim, `run_pass` runs `_resource_preflight` → `ResourceGovernor.decide`.
   `resource_governor._decide` step 2 enforces: `max_writers_global=1`
   (MEDIUM/HEAVY/EXCLUSIVE count as writers), `max_light=2`, EXCLUSIVE is
   host-exclusive, and any candidate is blocked by an active EXCLUSIVE. A
   non-ALLOW verdict requeues the job (`_defer_resource_job` /
   `_deny_resource_job`) with no spawn. This is why, in practice, only one
   *writer* runs at a time and at most two LIGHT jobs.

3. **Per-task uniqueness.** `idx_supervisor_jobs_active_task` is a unique index
   on `supervisor_jobs(task_id) WHERE terminal IS NULL` — at most one
   non-terminal supervisor job per *task*. This is a per-task (not global)
   invariant.

There is no repository-global or machine-global mutex at the scheduler level:
`claim_next_job` will happily claim a second QUEUED job while a first is
RUNNING (the claim predicate only excludes the job's own `next_eligible_at`,
foreign lease, and non-QUEUED states — it never inspects *other* jobs). The
serialisation is therefore enforced *admission-side* by the Resource Governor,
not claim-side by any cross-job predicate. The claim happens first
(QUEUED→RUNNING), then the admission gate may requeue (RUNNING→QUEUED) — a
documented, single-threaded, no-spawn pattern.

### B. Architectural invariants vs. temporary serial-safety limits

**Architectural invariants (must stay):**
- One job = one authoritative worktree writer lease (`writer_binding_mode`,
  `writer_owner_instance_id`, `writer_lease_epoch`, `writer_dispatch_id`) —
  enforced by `store.bind_writer_worktree` CAS and `worktree.writer_guard_for`.
- Exactly-one claim winner per job (`lease_epoch` monotonic, `owner_instance_id`,
  `lease_expires_at`); terminal states sticky (`_transition_job` F6).
- No duplicate spawn (SPAWN_RUN journal idempotency, `_begin_action` args_hash).
- Single active supervisor (`SupervisorInstance` CAS + advisory lock).
- Store is the single authoritative source of truth (no second registry).
- Fencing: agent prose never authoritative for footprint/writer/resource.
- Sandbox + env-allowlist + key handling unchanged or stronger.

**Temporary serial-safety limits (generalizable in I1):**
- `ResourcePolicy.max_writers_global = 1` / `max_light = 2` / `max_medium = 1`
  / `max_heavy = 1` — *conservative* starting points, not architectural.
- Per-job (not aggregate) host-reserve check in `resource_governor._decide`
  step 6 — generalisable to aggregate reservations (§I below).
- No repository/worktree *structural* overlap policy (only class counts).

### C. Repository identity today

- `supervisor_jobs.repo_identity` (TEXT) — canonical realpath of the repo root,
  validated by `worktree.validate_repo_identity` (non-empty, ≤ 256 chars).
- `supervisor_jobs.canonical_worktree_path` (TEXT) — realpath-canonicalised via
  `worktree.validate_worktree_binding_path` (no `..`/symlink escape, bounded by
  `worktree_root`).
- `supervisor_jobs.branch_identity` (TEXT) — `validate_branch_identity`
  (no whitespace, ≤ 256 chars); `base_commit`/`expected_head`/`current_head`
  (TEXT).
- `GitProvenanceProvider` (read-only, injectable) yields `repo_identity`
  (`git rev-parse --show-toplevel`), `head`, `branch`, `dirty`, `changed_paths`.
- **There is no full repo registry** (explicitly deferred: “No full Worktree
  Registry (Phase I)” in `worktree.py`). Identity is per-job persisted columns
  only.

### D. How to decide whether two jobs may run concurrently

Available trusted inputs today (all store data, never agent output):
- `role` (via frontier dispatch `expected_role`), `resource_class`,
  `repo_identity`, `canonical_worktree_path`, `branch_identity`,
  `writer_binding_mode`, process registry evidence, action class (journal
  `action_type`). I1 adds the bounded trusted mutation footprint (§3) and
  `depends_on` (§9). Concurrency decision = pure function over (candidate
  facts, active-job facts) → verdict; no LLM, no host I/O.

### E. Naturally read-only roles

- `Role` enum: `lead`, `analyst`, `implementer`, `qa`, `reviewer`.
- Write broker (`workspace_broker.WorkspaceBroker`): `implementer` → whole
  fixture root; `qa` → `fixture_root/tests/**` only; **any other role →
  `PermissionDenied`** (`_allowed_root`). So `lead`/`analyst`/`reviewer` are
  read-only w.r.t. the workspace broker.
- Writer binding (`bind_writer_worktree`, `writer_binding_mode=BOUND`) is
  established only on the **IMPLEMENTER** write action (`_invoke_broker_locked`
  during `APPLY_PATCH_SET`). `qa` writes tests/ but does not get a product
  writer binding.
- Consequence: read-only = `resource_class=LIGHT` + non-implementer role, and
  these jobs must never receive a writer binding or a broker write scope.

### F. Two writers on DIFFERENT worktrees stay isolated today

- `store.bind_writer_worktree` persists `writer_dispatch_id`,
  `writer_owner_instance_id`, `writer_lease_epoch` (CAS on the job's current
  owner/epoch/expiry) + `canonical_worktree_path` + repo/branch/head provenance
  in one `BEGIN IMMEDIATE` transaction.
- `worktree.writer_guard_for` re-checks the full fencing token
  (job/dispatch/owner/epoch/facts_version + canonical path == broker scope
  root) before *every* OS effect in the broker.
- `process_registry` binds process identity `(boot_id, pid, process_start_ticks)`
  + `scope_ref`/`cgroup_ref` per dispatch.
- Isolation is therefore already per-`canonical_worktree_path`; the *policy*
  that decides when two writers may coexist across different worktrees is what
  I1 adds.

### G. Actions still requiring global / repository-level serialisation

- Worktree creation (deferred to I2/I3 — not implemented).
- Broker `APPLY` critical section: already fenced per-job via the writer guard +
  a cross-controller apply lock (`APPLY_LOCK_*` in `supervisor.py`, dir
  `.argent-supervisor-locks`); I1 adds an explicit named **action-lock**
  boundary for future repo-global actions (§17).
- Owner-gate prompt / notification outbox: per-job tables; outbox delivery is
  already idempotent/dedup-guarded.
- Integration/merge/release/deploy/config (I2/I3) — **not implemented in I1**;
  only the serialization *boundary* is provided.

### H. Restart reconciliation today and what must hold for N concurrent jobs

`Scheduler.reconcile_after_restart` scans all non-terminal jobs and applies
per-job D-rules (`_classify_running`): terminal → untouched; QUEUED/OWNER_GATE/
WAITING_EXTERNAL → left; RUNNING + valid own lease → `rebound`; RUNNING + valid
foreign lease → `foreign_lease_kept`; RUNNING + expired concrete lease → decided
by live process evidence (alive/terminal/unknown) + worktree verdict, with CAS
fences and a bounded re-classification loop. It is **already per-job and
idempotent** — every decision is keyed on one job's (lease, process, worktree)
evidence and the CAS `expected` snapshot prevents one job's ambiguity from
corrupting another. What must additionally hold for N concurrent jobs: (i) the
concurrency gate must re-run on re-admission of recovered jobs; (ii) the
active-jobs view used for admission must be recomputed from the *post-reconcile*
store (no in-memory cache); (iii) `recover_takeover_job` must remain the only
RUNNING re-claim path (no bulk "restart everything"). No change is required to
the per-job mechanics — only tests asserting independence are added.

### I. Resource Governor rules to generalise (not bypass)

- **Aggregate host-reserve admission**: replace the per-job rule
  `mem_available - candidate.memory_max < reserve → DEFER` with
  `mem_available - Σ(active ceilings) - candidate.memory_max < reserve → DEFER`
  (still `INSUFFICIENT_MEMORY_RESERVE`). The active-jobs reader already returns
  `[(job_id, resource_class), ...]`; each active job's ceiling is
  `policy.limits_for(class).memory_max_bytes`.
- Class-count concurrency (`max_writers_global`/`max_light`/HEAVY/EXCLUSIVE)
  stays as-is (already authoritative) and continues to emit
  `CONCURRENCY_LIMIT`.
- Swap/disk/tmpfs/load rules: unchanged.

---

## Part 2 — Design

### 2.1 New pure module `argent_core/concurrency_policy.py`

Deterministic, I/O-free, LLM-free. Inputs: candidate facts + active-job facts
(role, resource_class, repo_identity, canonical_worktree_path, trusted mutation
footprint, `depends_on` + resolved dependency terminal state, action class).
Output enum `ConcurrencyVerdict` (ALLOW_PARALLEL / SERIALIZE / DEFER / BLOCK)
with machine-readable `ConcurrencyReasonCode`.

Reason codes produced by the policy (structural):

| Verdict | Reason | Meaning |
|---|---|---|
| ALLOW_PARALLEL | READONLY_SAFE | read-only candidate, no structural conflict |
| ALLOW_PARALLEL | DISTINCT_WORKTREE | writers, distinct worktrees, disjoint footprints |
| ALLOW_PARALLEL | DISTINCT_REPO | different repositories |
| SERIALIZE | WORKTREE_CONFLICT | two writers, same canonical worktree |
| SERIALIZE | REPO_OVERLAP | same repo + (same branch ∨ shared integration target ∨ overlapping path roots) |
| SERIALIZE | UNKNOWN_OVERLAP | cannot prove disjointness (missing repo/path roots) |
| SERIALIZE | ACTION_GLOBAL_SERIALIZE | repo-global/global action while anything active |
| DEFER | DEPENDENCY_NOT_MET | prerequisite exists but not in a satisfied terminal state |
| BLOCK | DEPENDENCY_UNKNOWN | prerequisite id missing/unknown |

Class-budget reason codes (WRITER_SLOT_FULL / HEAVY_ALONE / EXCLUSIVE_ALONE /
LIGHT_SLOT_FULL) and RESOURCE_RESERVE remain owned by **Phase C**
(`ResourceGovernor` → `CONCURRENCY_LIMIT` / `INSUFFICIENT_MEMORY_RESERVE`),
which is already authoritative and which I1 extends with aggregate admission.
This avoids a second authority on the same slots.

### 2.2 Hard invariants (never weakened)

- ONE worktree = max ONE authoritative writer lease — enforced HARD in the
  store (F3: ``bind_writer_worktree`` raises ``WorktreeConflictError`` when a
  DIFFERENT non-terminal job already holds ``writer_binding_mode='BOUND'`` for
  the same canonical worktree, backed by the partial unique index
  ``idx_supervisor_jobs_writer_worktree``) and re-checked by the policy
  (WORKTREE_CONFLICT).
- Two writers concurrent only if distinct canonical worktrees ∧ disjoint trusted
  footprints ∧ distinct fenced leases ∧ resource admission allows.
- UNKNOWN overlap ⇒ SERIALIZE.
- Agent prose never authoritative for footprint (footprint originates only from
  trusted controller/task metadata setters).
- Terminal states immutable; no duplicate spawn (unchanged); WAITING_EXTERNAL /
  OWNER_GATE consume no active slot (they are never `RUNNING` → not in the
  active set); provider/resource failures stay non-CODE failure classes.

### 2.3 Trusted mutation footprint (§9)

Additive columns on `supervisor_jobs` (single source of truth, no second table):

- `mutation_path_roots` (TEXT, bounded JSON list of relative path prefixes)
- `mutation_modules` (TEXT, bounded JSON list of subsystem/module names)
- `external_action_class` (TEXT)
- `integration_target` (TEXT)

Plus `depends_on` (TEXT, prerequisite `supervisor_jobs.id`) and audit columns
`last_concurrency_reason_code` / `last_concurrency_at`.

Origin: trusted controller/task analysis only, via a new
`store.set_job_metadata(...)` primitive and `Supervisor.set_job_metadata(...)`
facade (validated: bounded lengths, well-formed JSON arrays, non-empty strings).
Conservative overlap = path-prefix intersection on `mutation_path_roots`;
same repo + same branch or shared `integration_target` ⇒ SERIALIZE; no semantic
merge prediction.

### 2.4 Repository/project collision policy (§8)

- Different repos ⇒ SAFE/PARALLEL (subject to resource admission).
- Same repo + same branch ⇒ SERIALIZE.
- Same repo + different branches + overlapping path roots ⇒ SERIALIZE.
- Same repo + shared integration target ⇒ SERIALIZE.
- Missing/unbounded footprint ⇒ SERIALIZE (UNKNOWN_OVERLAP).
Conservative default: SERIALIZE unless disjointness is provable.

### 2.5 Read-only parallelism (§10)

Analyst/Reviewer/Lead (LIGHT, non-implementer) may run concurrently up to the
LIGHT cap. Tests assert they never receive a writer binding / broker write
scope.

### 2.6 Resource-aware concurrency (§11)

Phase C authoritative. Added: aggregate admission in `ResourceGovernor._decide`
(sum of active class ceilings + candidate ceiling vs host reserve). Conservative
limits for THIS host (8 CPU / ~7.7 GiB total RAM) as module constants (see §3).

### 2.7 Model/provider concurrency (§12)

Provider concurrency is already a resource/policy input (routing decisions +
availability TTL). Provider-limit outcomes remain PROVIDER/RESOURCE error
classes — never CODE. No provider permissions are broadened.

### 2.8 Priority/fairness (§13)

FIFO default preserved (`claim_next_job` ORDER BY priority DESC, rowid). No
starvation: DEFER/SERIALIZE requeue with a bounded `next_eligible_at`; a
blocked heavy job cannot starve unrelated LIGHT work (LIGHT cap is independent).
Retry/backoff jobs are not eligible while backing off (`next_eligible_at` in
`_job_is_claimable`) and consume no active slot.

### 2.9 Dependencies (§14)

`depends_on` (single prerequisite, no general DAG). Enforced in two places:
(i) `claim_next_job` skips a QUEUED job whose prerequisite exists but is not
`terminal='DONE'`, and **BLOCK**s (terminal, `DEPENDENCY_UNKNOWN`) a QUEUED job
whose prerequisite id is missing (fail conservative); (ii) the concurrency gate
re-checks dependency for explicit `claim_job`/takeover paths (DEFER on
`DEPENDENCY_NOT_MET`, BLOCK on `DEPENDENCY_UNKNOWN`). Satisfied prerequisite
wakes the dependent exactly once (atomic claim + SPAWN_RUN idempotency).

### 2.10 Active job set (§15)

Authoritative view = existing `supervisor_jobs` columns (additive only). The
concurrency gate reads all non-terminal `RUNNING` jobs (id, role, resource_class,
repo, worktree, footprint, depends_on) from the store — no second source of truth.

### 2.11 Parallel restart/recovery (§16)

`reconcile_after_restart` is already per-job and idempotent; I1 adds tests that
two live jobs rebound independently, one live + one stale are treated
independently, terminal A is immutable while B recovers, and one job's ambiguous
process identity never steals another's lease/worktree.

### 2.12 Action serialization boundary (§17)

Minimal named action-lock: ``action_lock_name(...)`` helper in the concurrency
policy (pure scope derivation: ``global:<name>`` / ``repo:<repo_identity>:<name>``)
+ the store-backed ``Store.try_acquire_action_lock`` /
``Store.release_action_lock`` CAS primitives (lease-fenced, crash-safe with
bounded stale-holder reclaim) for future I2/I3 (merge/integrate/deploy/release/
config). Only the boundary + tests are implemented; no merge queue.

### 2.13–2.15 Job-local provenance (context / routing / evidence)

Context packs are dispatch/job-bound (`context_pack.py` `job_id`+`dispatch_id`).
Routing decisions are per-dispatch (`model_router.route(request.dispatch_id)`,
`routing_decisions.dispatch_id`). Test evidence is bound to an exact immutable
snapshot + HMAC (`test_execution.py`). I1 adds tests asserting no cross-job
leak/reuse.

### 2.16–2.17 WAITING_EXTERNAL & shutdown

WAITING_EXTERNAL jobs are never `RUNNING` → excluded from the active set → release
capacity. Shutdown: `run_pass` already aborts before spawn on `stop_check`;
`request_shutdown` sets the stop event so no new claim happens after stop
(`_run_one_iteration` returns early when stopping). Tests added.

---

## Part 3 — Conservative initial limits (host: 8 CPU / ~7.7 GiB RAM)

Derived from `ResourcePolicy` ceilings (`memory_max_bytes`):

- LIGHT ≤ 1 GiB, MEDIUM ≤ 2.5 GiB, HEAVY ≤ 4 GiB, EXCLUSIVE ≤ 5.5 GiB.
- host reserve = max(1.5 GiB, 20% × ~7.7 GiB) ≈ 1.54 GiB.
- available ~5.17 GiB at check → usable ≈ 5.17 − 1.54 ≈ 3.63 GiB.

Module constants in `concurrency_policy.py` (deliberately conservative,
documented, later-configurable — **no config system now**):

```
MAX_WRITERS_CONCURRENT        = 1      # one writer (MEDIUM-class or heavier) at a time
MAX_READONLY_LIGHT_CONCURRENT = 2      # LIGHT cap
HEAVY_ALONE                   = False  # HEAVY is gated by the single-writer budget
EXCLUSIVE_ALONE               = True   # EXCLUSIVE always alone
DEPENDENCY_SATISFIED_TERMINALS = ("DONE",)
```

These mirror the existing `ResourcePolicy` concurrency defaults so the policy
and the governor agree. Rationale: 1 MEDIUM writer (~2.5 GiB) leaves ~1.1 GiB
headroom above the reserve; 2 LIGHT (~1 GiB each) leave ~1.6 GiB headroom;
HEAVY (4 GiB) is gated by the single-writer budget (it IS a writer class,
``HEAVY_ALONE=False``), and on this host a HEAVY 4 GiB ceiling plus any LIGHT
work already exceeds the ~3.63 GiB usable headroom — so the aggregate-memory
admission is the binding constraint, not a dedicated "alone" slot;
EXCLUSIVE (5.5 GiB) always alone.

---

## Part 4 — Bounded real demo (§22)

Ran `docs/i1_demo.py` (no LLM, no network, no production repos, no stress; two
LIGHT read-only-style scope runs through the REAL `SystemdRunScopeBackend`,
`bubblewrap`/`systemd-run` present on this host).  Results (trimmed):

```
host mem_total=8264478720 avail=6012026880
active=LIGHT x2 -> LIGHT3 DEFER/CONCURRENCY_LIMIT, MEDIUM DEFER/INSUFFICIENT_MEMORY_RESERVE
demo-job-A: pid=15007 cgroup=.../argent-c2-i1demo-demojoba-2616f61f.scope memory.max=67108864 bound=True final_state=inactive
demo-job-B: pid=15012 cgroup=.../argent-c2-i1demo-demojobb-af29a4b0.scope memory.max=67108864 bound=True final_state=inactive
DEMO OK
```

Observations:
- Two transient user scopes ran CONCURRENTLY with **distinct job ids, distinct
  cgroup paths, distinct process ids** (15007 vs 15012), each bound to its own
  verified scope (`memory.max=64 MiB`) and each writing only its own temp
  worktree marker.
- Both scopes ended `inactive` (transient scopes self-cleaned; no residue).
- **Aggregate admission is live**: with two active LIGHT jobs, a third LIGHT is
  DEFERred on `CONCURRENCY_LIMIT` (max_light=2), and a MEDIUM is DEFERred on
  `INSUFFICIENT_MEMORY_RESERVE` because `avail − 2×1 GiB − 2.5 GiB ≈ 1.1 GiB
  < host reserve (~1.54 GiB)` — the aggregate rule (not the per-job rule)
  rejects it.

**Scope precision (Sol round):** this demo drives the REAL
`SystemdRunScopeBackend` (scope create/verify/start/bind) and the REAL
`ResourceGovernor` aggregate admission DIRECTLY — it does **NOT** go through the
`scheduler → supervisor` spawn-admission path, so it does not prove that path
end-to-end. The real-path proof is the deterministic regression
`test_f1_second_light_job_reaches_spawn_while_first_running` (injected store +
providers + real enforcer path), which shows a second LIGHT job is ADMITTED and
REACHES spawn while the first is RUNNING (no self-count, no double reserve
subtraction). The demo's claim is limited to: two scopes ran concurrently with
distinct evidence, and the aggregate admission rule is live.

## Part 5 — Known limitations / boundary to I2/I3/I4

- No Worktree Registry, no merge queue, no integration/merge/release/deploy
  implementation — only the action-lock boundary.
- No config system for the conservative limits (constants only).
- No provider-concurrency enforcement beyond existing routing/availability.
- Dependency is single-`depends_on` (no DAG).
- Footprint overlap is path-prefix only (no semantic merge prediction).
- Writers are still capped at 1 by the conservative resource budget; the
  structural policy already reports two disjoint writers as structurally
  eligible (DISTINCT_WORKTREE) so I2/I3 can lift the budget without policy
  change.

---

## Part 6 — Sol closing review (fix round) — NOT GREEN

Main + Sol independently verified the I1 implementation and raised **5 HIGH +
several LOW** findings, all fixed in this worktree.  Summary:

- **F1 (HIGH) — spawn-time admission counted the candidate itself.**
  The Supervisor's binding admission reader had no candidate exclusion, so the
  candidate (already RUNNING at spawn) counted against its own concurrency
  limit and was double-subtracted from the aggregate reserve.  Fix: a single
  shared exclusion cell (``Supervisor._admission_exclude_job_id``) is now used
  by BOTH the Scheduler claim preflight and the Supervisor ``_fresh_admission``
  binding point.  Regression:
  ``test_f1_second_light_job_reaches_spawn_while_first_running``.
- **F2 (HIGH) — write capability vs concurrency "writer" classification
  disagreed.**  ``JobFacts.is_writer`` was resource-class-only; a LIGHT
  implementer/qa job slipped through as READONLY_SAFE.  Fix: a job is a writer
  when its role is write-capable (``WRITE_ROLES = ("implementer", "qa")``,
  mirroring ``supervisor._is_write_role``) OR its class is a writer class; the
  structural gate now RE-RUNS at the spawn gate for continuations too.
  Tests: ``test_write_capable_role_is_writer_regardless_of_class``,
  ``test_two_implementer_light_same_worktree_serialize``,
  ``test_f2_spawn_regate_catches_role_footprint_transition``.
- **F3 (HIGH) — one-writer-per-worktree was not hard-enforced by the store.**
  ``bind_writer_worktree`` validated only the target job.  Fix: a transactional
  cross-job check (raises ``WorktreeConflictError``) plus the partial unique
  index ``idx_supervisor_jobs_writer_worktree`` (``WHERE terminal IS NULL AND
  writer_binding_mode='BOUND'``).  Test:
  ``test_f3_one_writer_per_worktree_hard_invariant``.
- **F4 (HIGH) — GLOBAL/REPO_GLOBAL serialization was asymmetric.**  Fix: the
  policy now checks candidate-side AND active-side (an ordinary candidate
  serializes against an active GLOBAL job, or an active REPO_GLOBAL job in the
  same repo).  Tests: ``test_active_global_blocks_ordinary_candidate``,
  ``test_active_repo_global_blocks_same_repo_candidate_only``.
- **F5 (HIGH) — action locks were not crash-safe/lease-fenced.**  Fix:
  ``try_acquire_action_lock`` now verifies the caller is the current unexpired
  lease holder of an existing non-terminal job (fail closed), reclaims a stale
  holder atomically, and ``action_locks.holder_job_id`` gained a FK.
  Tests: ``test_f5_action_lock_rejects_phantom_and_stale``,
  ``test_f5_action_lock_expired_holder_reclaimed``,
  ``test_f5_action_lock_reassigned_holder_reclaimed``.
- **LOW items** — ``DISTINCT_REPO`` is now actually emitted (not just declared);
  mutation path roots are validated fail-closed (absolute / drive-letter /
  empty / above-root escape are rejected with ``ValueError``);
  ``set_job_metadata`` is now supervisor-authorized + lease-fenced (requires the
  current ``(owner_instance_id, lease_epoch)``, bumps ``facts_version``); docs
  corrected (``HEAVY_ALONE=False`` matches the code; the action-lock API names
  are the real ``try_acquire_action_lock``/``release_action_lock``/
  ``action_lock_name``).

**Acceptance adequacy (Sol area 14):** the missing material-path coverage is
now present — the real two-job scheduler→supervisor admission regression (F1),
the hard store binding invariant (F3), role-based writer classification (F2),
symmetric global-action checks (F4), and action-lock durability/reclaim (F5).
The demo's scope is stated precisely (it did not drive the scheduler→supervisor
path; the deterministic F1 regression does).

---
