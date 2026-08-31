# PHASE B2 — Durable Scheduler + Restart/Recovery (Notes)

**Status:** implementation complete, tests green. **Not committed** (supervisor
commits after independent Sol review).

## 1. What was added

New module `argent_core/scheduler.py` (bounded passes + restart reconciliation),
plus two additive store primitives. `supervisor.py` gained only two thin facade
delegations (`SupervisorStore.clear_lease`, `.quarantine_lost`). No existing
method changed semantics.

### Files
- `argent_core/scheduler.py` — NEW: `Scheduler`, `SchedulerPassResult`,
  `RestartReconcileSummary`, `DEFAULT_LEASE_TTL_SECONDS`.
- `argent_core/store.py` — NEW primitives `Store.clear_lease`,
  `Store.quarantine_lost` (both CAS-guarded, additive).
- `argent_core/supervisor.py` — NEW facade methods only (no behavior change).
- `tests/test_phase_b2_scheduler_recovery.py` — NEW: 19 tests (A–G).

## 2. Scheduler pass model

`Scheduler.run_pass(job_id=None)` performs exactly one bounded pass:

1. read persisted facts;
2. determine a claimable job (B1 predicates: QUEUED eligible, no valid foreign
   lease, expired-lease takeover allowed, terminal never);
3. atomic claim (`claim_job` / `claim_next_job`, epoch+1, facts_version bump);
4. exactly one safe step (`reconcile` → `perform_next_safe_action_if_required`);
5. result persisted (fenced by the B1 fencing token);
6. renew the lease (still RUNNING + still held) or clear it (job left RUNNING);
7. the pass ends — no `while-not-terminal` loop, no agent held in-process.

`SupervisorLoop.run_until_terminal` is left unchanged as the documented
single-job compatibility entry point (its internals already only poll via the
interruptible `Waiter`; the agent is always spawned detached).

## 3. Renewal policy

Renewal happens **only** when, immediately after the pass's safe step:

- the job is still `RUNNING` (`primary_state=RUNNING` / `status=ACTIVE`), **and**
- this scheduler still holds the current, unexpired lease
  (`lease_is_current(job_id, owner_instance_id, lease_epoch)`).

"Agent still running" is NOT evidence (no process registry): only a persisted
RUNNING job this scheduler is actively driving authorises renewal. TTL is
caller-supplied local policy, bounded by `store.MAX_LEASE_TTL_SECONDS`; an
expired lease is never silently extended (`renew_lease` CAS). Progress is never
treated as liveness. No automatic TTL increase for a slow agent.

## 4. Restart / recovery model

`Scheduler.reconcile_after_restart()` is a deterministic, idempotent scan over
persisted facts (reopened DB / fresh Supervisor; no in-memory cache authority):

- terminal → untouched (sticky);
- QUEUED / OWNER_GATE / WAITING_EXTERNAL → left in place (gate ledger idempotent);
- RUNNING, `lease_expires_at IS NULL` → fail-closed `LOST` quarantine
  (`quarantine_lost`, error_code `AMBIGUOUS_WRITER`), never claimable, no
  respawn, no second writer — this is the only write the method performs;
- RUNNING, valid lease, held by this instance → `rebound` (context re-established
  per-pass in `run_pass`);
- RUNNING, valid lease, foreign holder → left alone (no takeover while valid);
- RUNNING, expired concrete lease → `takeover_candidate` (scheduler claims via
  epoch+1 later; the old holder is fenced by the epoch bump).

Action-journal crash windows (before effect / after effect before finalize /
after finalize) are handled by the existing `_begin_action` get-or-create replay
and are proven with lease/epoch context in the F tests.

## 5. Singleton scheduler lease — decision

**Deferred to Phase G (not implemented).** Rationale: the job-level atomic
claim already gives the exactly-one-claim-winner guarantee needed for
dual-supervisor protection (the losing instance's `claim_job`/`claim_next_job`
returns nothing and its later commits are fenced). A separate scheduler-level
lease would introduce a second authority with no provable benefit for the
dual-supervisor guarantee, while adding a new fence object to reason about. The
job lease/epoch remains the single fencing authority. Re-evaluate at Phase G
(background service) if a concrete benefit emerges.

## 6. Conflicts / alternatives

None. All owner requirements were implementable as specified; no scope,
security, or architecture change was made. Liveness/progress separation reuses
the existing fields (lease vs. `last_progress_at`); no new table was needed.
