# PHASE I1 ACCEPTANCE — Controlled Multi-Job Parallelization Core

**Branch:** `phase-i1-controlled-parallelization` (Base `ffd87b61` = Phase-G GREEN closing commit).
**Datum:** 2026-09-03.
**Scope:** code + deterministic tests + docs only. **No commit, no push, no live
service/systemd/state-dir mutation, no network writes, no provider credentials,
no LLM agents.**

**STATUS: I1 GREEN — ARGENT_PHASE_I1_CONTROLLED_PARALLELISM_GREEN (Main-verifiziert 2026-09-03, nach unabhängiger Verifikation + Sol-Closing-Review + Fix-Round).**
**Kein ARGENT_PHASE_I_GREEN** (Phase-I-Gesamt erst nach I2–I4).

---

## 1. Deliverables

- **NEW** `argent_core/concurrency_policy.py` — pure, deterministic, I/O-free,
  LLM-free structural concurrency decision (worktree/repo/mutation-footprint/
  dependency/action-class). Output enum `ConcurrencyVerdict`
  (ALLOW_PARALLEL / SERIALIZE / DEFER / BLOCK) + `ConcurrencyReasonCode`.
- **EXTENDED** `argent_core/resource_governor.py` — aggregate host-reserve
  admission (`Σ(active class ceilings) + candidate ceiling vs reserve`), keeping
  the existing class-count concurrency (`CONCURRENCY_LIMIT`) authoritative.
- **EXTENDED** `argent_core/store.py` — SCHEMA 18 → 20; additive columns on
  `supervisor_jobs` (`mutation_path_roots`, `mutation_modules`,
  `external_action_class`, `integration_target`, `action_scope`, `depends_on`,
  `last_concurrency_reason_code`, `last_concurrency_at`); new `action_locks`
  table (with a `holder_job_id → supervisor_jobs(id)` FK) + lease-fenced CAS
  primitives; a partial unique index `idx_supervisor_jobs_writer_worktree`
  enforcing ONE-worktree = ONE-authoritative-writer-lease;
  `set_job_metadata` (now supervisor-authorized + lease-fenced) /
  `get_dependency_terminal` / `list_active_job_facts`; dependency gate +
  missing-dependency BLOCK in `claim_next_job`.
- **EXTENDED** `argent_core/models.py` — `WorktreeConflictError` (bounded store
  error for a second writer binding the same canonical worktree).
- **EXTENDED** `argent_core/concurrency_policy.py` — role-aware writer
  classification (`WRITE_ROLES`), symmetric GLOBAL/REPO_GLOBAL serialization,
  `DISTINCT_REPO` emission, fail-closed mutation-root validation.
- **EXTENDED** `argent_core/job_state.py` — `QueueReason.CONCURRENCY_SERIALIZED`.
- **EXTENDED** `argent_core/scheduler.py` — structural concurrency gate
  (`_concurrency_preflight` / `_concurrency_gate`) wired before the resource
  gate on every new claim.
- **EXTENDED** `argent_core/supervisor.py` — `SupervisorStore` facades for the
  new primitives.
- **Tests** `tests/test_phase_i1_policy.py` (29), `tests/test_phase_i1_gate.py`
  (32), `tests/test_phase_i1_isolation.py` (9).
- **Docs** `docs/PHASE_I1_NOTES.md` (analysis A–I, design, conservative limits,
  demo, limitations, boundary) + this file.
- **Demo** `docs/i1_demo.py` (bounded real demo evidence).

## 2. Concurrency policy contract

`decide(candidate: JobFacts, active_jobs: Sequence[JobFacts]) -> ConcurrencyDecision`

- `ConcurrencyVerdict`: `ALLOW_PARALLEL | SERIALIZE | DEFER | BLOCK`.
- `ConcurrencyReasonCode` (structural, produced by the policy):
  `READONLY_SAFE`, `DISTINCT_WORKTREE`, `DISTINCT_REPO`, `WORKTREE_CONFLICT`,
  `REPO_OVERLAP`, `UNKNOWN_OVERLAP`, `DEPENDENCY_NOT_MET`,
  `DEPENDENCY_UNKNOWN`, `ACTION_GLOBAL_SERIALIZE`. (Contract-only, emitted by
  Phase C: `WRITER_SLOT_FULL`, `HEAVY_ALONE`, `EXCLUSIVE_ALONE`,
  `LIGHT_SLOT_FULL`, `RESOURCE_RESERVE`.)
- Decision order: dependency → action-class global/repo-global → structural
  writer overlap → read-only/disjoint allow.

## 3. Conservative initial limits (host: 8 CPU / ~7.7 GiB RAM)

Module constants in `concurrency_policy.py` (documented, later-configurable —
no config system built):

- `MAX_WRITERS_CONCURRENT = 1` (mirrors `ResourcePolicy.max_writers_global=1`).
- `MAX_READONLY_LIGHT_CONCURRENT = 2` (mirrors `max_light=2`).
- `EXCLUSIVE_ALONE = True`; `HEAVY_ALONE = False` (HEAVY is a writer, gated by
  the single-writer budget — matching the existing validated Phase C matrix;
  on this host the aggregate-memory admission is the binding constraint).
- `DEPENDENCY_SATISFIED_TERMINALS = ("DONE",)`.

## 4. Deterministic test counts

| Gruppe | Ergebnis |
|---|---|
| I1 policy (structural + dependency + action-lock + role/symmetry) | 29 passed |
| I1 gate (scheduler/store/resource/restart/fairness/demo boundary + F1/F3/F5) | 32 passed |
| I1 isolation (read-only no-write, context/routing/evidence, trust) | 9 passed |
| **I1 total** | **70 passed** |
| Affected older suites (B1/B2/B4/C1/C3c/D3 regression + schema-version) | green |
| **FULL SUITE** | **2638 passed in ~45 s** |

Cases 1–30 from the brief are covered across the three I1 files (see
`docs/PHASE_I1_NOTES.md` and inline test comments).

## 5. Bounded real demo (§22)

`docs/i1_demo.py` (no LLM, no network, no production repos, no stress) ran TWO
concurrent LIGHT read-only-style scope runs through the real
`SystemdRunScopeBackend` (bubblewrap + systemd-run present) with separate job
ids, separate temp worktrees, separate process evidence, and aggregate
admission. Result: two transient scopes concurrently ACTIVE with distinct
cgroup paths and distinct process ids, both `memory.max=64 MiB` enforced and
bound, both ended `inactive` (no residue). Aggregate admission live: 2×LIGHT
active ⇒ LIGHT3 `CONCURRENCY_LIMIT`, MEDIUM `INSUFFICIENT_MEMORY_RESERVE`.

**Scope precision (Sol round):** this demo drives the real scope backend + the
real governor DIRECTLY and does **not** go through the `scheduler → supervisor`
spawn-admission path, so it does not prove production parallel dispatch. The
real-path proof is the deterministic regression
`test_f1_second_light_job_reaches_spawn_while_first_running` (injected store +
providers + real enforcer path), which proves a second LIGHT job is ADMITTED and
REACHES spawn while the first is RUNNING. The demo's claim is limited to: two
scopes ran concurrently with distinct evidence, and aggregate admission is live.

## 6. Verification

- `/usr/bin/python3 -m pytest tests/test_phase_i1*.py -q` → **70 passed**.
- `/usr/bin/python3 -m pytest tests/ -q` → **2638 passed** (~45 s).
- Intentionally updated existing tests (documented): schema-version assertions
  (3 files: 19→20), `test_dual_supervisor_store_reader_blocks_second_writer`
  (gave the two writers distinct trusted footprints via the lease-fenced setter
  so the structural gate ALLOWs and the Resource Governor remains the authority
  that blocks the second writer), and every `set_job_metadata` caller (now
  lease-fenced: jobs are leased before metadata is set).

## 7. Known limitations / boundary to I2/I3/I4

- No Worktree Registry, no merge queue, no integration/merge/deploy/release
  implementation — only the named action-lock boundary + policy branch.
- No config system (conservative limits are constants).
- Dependency is single-`depends_on` (no DAG).
- Footprint overlap is path-prefix only (no semantic merge prediction).
- Writers are capped at 1 by the conservative resource budget; the structural
  policy already reports two disjoint writers as structurally eligible
  (`DISTINCT_WORKTREE`) so I2/I3 can lift the budget without a policy change.
- Provider-concurrency beyond the existing routing/availability machinery is
  not added; provider/resource outcomes remain non-CODE failure classes.
- `set_job_metadata` is now lease-fenced: trusted mutation metadata (footprint /
  `depends_on`) is set while the job is RUNNING (held by the current lease), not
  pre-claim.  The structural gate therefore runs conservatively at first claim
  (NULL footprint ⇒ UNKNOWN ⇒ SERIALIZE against writers) and precisely at the
  spawn re-gate once the footprint is known (F2).

## 8. NOT GREEN

Independent verification + Sol closing review pending (Main).

## 9. Sol closing review (fix round) — NOT GREEN

Main + Sol independently verified the implementation and raised **5 HIGH +
several LOW** findings, all fixed (see `docs/PHASE_I1_NOTES.md` Part 6 for the
per-finding detail):

- **F1** candidate self-count at spawn admission (unified exclusion cell).
- **F2** role-aware writer classification + spawn re-gate for continuations.
- **F3** hard one-writer-per-worktree store invariant (transactional check +
  partial unique index + `WorktreeConflictError`).
- **F4** symmetric GLOBAL/REPO_GLOBAL serialization.
- **F5** lease-fenced, crash-safe action locks with stale-holder reclaim + FK.
- **LOW** `DISTINCT_REPO` emission, fail-closed root validation,
  lease-fenced `set_job_metadata`, docs precision (`HEAVY_ALONE=False`; real
  action-lock API names).

**Acceptance adequacy (Sol area 14):** the missing material-path coverage is
now present and green — real two-job scheduler→supervisor admission regression
(F1), hard store binding invariant (F3), role-based writer classification (F2),
symmetric global-action checks (F4), and action-lock durability/reclaim (F5).
The demo's scope is stated precisely and does not overclaim production parallel
dispatch.
