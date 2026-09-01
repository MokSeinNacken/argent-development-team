# PHASE C3 NOTES — Resource Failure Classification + Recovery Acceptance

Worktree: `phase-c3-resource-failure-recovery` (Base `d359373` = C2-GREEN).
Writer: Argent C3 (single writer). **No commit/push** — worktree left dirty for
supervisor review.

## 1. Scope

C3 adds the **job-level resource-failure classification + bounded recovery
decision** on top of the C1 (admission) / C2 (enforcement evidence) layers.
No new primary state, no second taxonomy, no limit/timeout/class escalation, no
model escalation, no code-rework authorisation from a resource failure, no
background service, no new external rights. Phase D (Context Engineering) is
**not** implemented here.

## 2. Design basis (supervisor Analyse-Antworten A–G)

- **A.** C2 evidence exists in production: `enforce_and_run` (sync) produces
  `EnforcementResult` with `exit_code` / `timed_out` / `scope_events` delta /
  `termination_class`; `enforce_and_spawn` (async) produces `SCOPE_OK` +
  verified scope + `memory_events_baseline` (no post-termination class); the
  Process Registry carries `termination_class` / `timed_out` / `scope_events`
  and `mark_process_terminal_with_evidence`; the Scheduler already handles
  pre-spawn enforcement failures (`_enforcement_failed_job` →
  QUEUED+RESOURCE, `_enforcement_lost_job` → `quarantine_lost`).
- **B.** Provably distinguishable: NORMAL_EXIT, NONZERO_EXIT, TIMEOUT (trusted
  wrapper flag), OOM_KILL (`memory.events` oom_kill/oom_group_kill delta > 0),
  MEMORY_LIMIT (`max`/`high` delta > 0), SCOPE_CREATION/VERIFICATION_FAILED +
  ENFORCEMENT_UNAVAILABLE (pre-spawn), UNKNOWN_TERMINATION.
- **C.** Fundamentally UNKNOWN: no exit + no events; ambiguous process/scope
  identity; stale boot_id; malformed/missing evidence; SCOPE_CLEANUP_UNVERIFIED.
- **D.** Automatically requeueable (bounded): temporary host pressure
  (DEFER_RESOURCE with bounded backoff), transient enforcement-backend failure
  (bounded RETRY_BOUNDED only when provable), pre-spawn admission DEFER.
- **E.** NOT identically retried: proven OOM under an unchanged limit
  (BLOCK_RESOURCE / PREFER_EXTERNAL), SCOPE_CLEANUP_UNVERIFIED (LOST),
  enforcement unavailable without a provable path (LOST, no legacy fallback,
  no unbounded retry).
- **F.** Safe without owner: bounded RETRY/DEFER (QUEUED + next_eligible_at +
  error_class=RESOURCE), LOST quarantine (`quarantine_lost`), BLOCK_RESOURCE
  (existing BLOCKED, non-terminal — owner reopen, fail-closed),
  PREFER_EXTERNAL as a persisted hint only.
- **G.** Smallest trusted classification point: the Process-Registry terminal
  detection in the Scheduler (`process_evidence` / `_process_identity_verdict`
  exist) + a new central step AFTER process/scope end that reads persisted
  `termination_class`/`scope_events`/`timed_out` and, under a valid lease/epoch
  (fencing), commits a recovery decision. Async agents are classified at
  terminal detection (registry path), not at spawn.

## 3. New module — `argent_core/resource_recovery.py`

### FailureClass (C3 job-level, closed)

| Class | Evidence basis |
|---|---|
| `NORMAL_EXIT` | C2 `NORMAL_EXIT` (exit 0, no events) |
| `CODE_OR_PROCESS_FAILURE` | C2 `NONZERO_EXIT` (exit ≠ 0, no resource evidence) |
| `RESOURCE_OOM` | `memory.events` oom_kill / oom_group_kill delta > 0 |
| `RESOURCE_MEMORY_LIMIT` | `memory.events` max / high delta > 0 |
| `RESOURCE_TIMEOUT` | trusted `timed_out` marker / C2 `TIMEOUT` |
| `RESOURCE_ENFORCEMENT_FAILURE` | C2 SCOPE_CREATION/VERIFICATION_FAILED / ENFORCEMENT_UNAVAILABLE |
| `RESOURCE_CAPACITY_FAILURE` | pre-spawn admission DEFER / DENY_LOCAL |
| `SCOPE_CLEANUP_UNVERIFIED` | EnforcementStatus `SCOPE_CLEANUP_UNVERIFIED` |
| `UNKNOWN_TERMINATION` | no exit + no events / malformed / missing |

### RecoveryDecision (bounded, exact)

`COMPLETE`, `RETRY_BOUNDED`, `DEFER_RESOURCE`, `BLOCK_RESOURCE`,
`PREFER_EXTERNAL`, `QUARANTINE_LOST`, `FAIL_NONRESOURCE`.

### RecoveryPolicy (frozen, versioned)

`max_resource_retries=2`, `retry_backoff_seconds=300`,
`defer_backoff_seconds=300`, closed `retryable_failure_classes`
(`RESOURCE_CAPACITY_FAILURE`, `RESOURCE_TIMEOUT`),
`allow_memory_limit_defer=False`, `allow_enforcement_defer=True`. There is
**no** limit/timeout/class/model field — the absence is the guarantee that C3
can never raise a limit, raise a timeout, raise a class, or escalate a model.
The never-retry classes (RESOURCE_OOM / SCOPE_CLEANUP_UNVERIFIED /
UNKNOWN_TERMINATION) are rejected at `RecoveryPolicy` construction.

### Evidence priority (`classify_failure`)

Process/Scope identity (validated by the CALLER) → trusted `termination_class`
→ trusted `timed_out` → `memory.events` delta → exit code → UNKNOWN.

- `exit 137` WITHOUT `memory.events` OOM delta → `CODE_OR_PROCESS_FAILURE`
  (NOT OOM).
- `exit 124` WITHOUT trusted `timed_out` → `CODE_OR_PROCESS_FAILURE`
  (NOT TIMEOUT).
- Agent output is UNTRUSTED: it can never determine a FailureClass /
  RecoveryDecision / limit / timeout / retry. `FailureClass(...)` /
  `RecoveryDecision(...)` / `decide_recovery(...)` raise `ValueError` on free
  strings; the store validates the closed enums on every commit.

### `decide_recovery` rules

| FailureClass | Decision |
|---|---|
| `NORMAL_EXIT` | `COMPLETE` |
| `CODE_OR_PROCESS_FAILURE` | `FAIL_NONRESOURCE` (no resource retry) |
| `RESOURCE_OOM` | `BLOCK_RESOURCE` (or `PREFER_EXTERNAL` if allowed) — never identical retry |
| `RESOURCE_MEMORY_LIMIT` | `BLOCK_RESOURCE` (default fail-closed) / `DEFER_RESOURCE` only if policy allows + budget remains |
| `RESOURCE_TIMEOUT` | `RETRY_BOUNDED` if budget + policy allow, else `DEFER_RESOURCE` — never a longer timeout |
| `RESOURCE_ENFORCEMENT_FAILURE` | `DEFER_RESOURCE` (provable) / `QUARANTINE_LOST` (evidence unknown) |
| `RESOURCE_CAPACITY_FAILURE` | `DEFER_RESOURCE` (bounded) |
| `SCOPE_CLEANUP_UNVERIFIED` / `UNKNOWN_TERMINATION` | `QUARANTINE_LOST` (fail-closed, no duplicate spawn) |

### Reason codes (bounded, no free agent strings)

`RESOURCE_OOM`, `RESOURCE_MEMORY_LIMIT`, `RESOURCE_TIMEOUT`,
`RESOURCE_ENFORCEMENT_UNAVAILABLE`, `RESOURCE_ENFORCEMENT_UNVERIFIED`,
`RESOURCE_CAPACITY_INSUFFICIENT`, `RESOURCE_PRESSURE`,
`SCOPE_CLEANUP_UNVERIFIED`, `RESOURCE_EVIDENCE_UNKNOWN`.

## 4. Store extension (minimal, additive)

- `SCHEMA_VERSION` `"10"` → `"11"`.
- `supervisor_jobs` gains three bounded audit columns (NULL-able, no CHECK):
  `last_recovery_decision TEXT`, `last_failure_class TEXT`,
  `last_recovery_at TEXT`. They are **audit only** — they never authorise an
  automatic admission (a new claim always re-runs preflight).
- No new table: `supervisor_jobs` + `process_registry` already carry the C3
  facts (`termination_class`/`timed_out`/`scope_events` on the registry;
  `error_class`/`error_code`/`attempt_no`/`next_eligible_at`/`queue_reason` on
  the job).
- Migration follows the B4 additive pattern exactly (idempotent,
  non-destructive, NULL backfill for pre-existing rows).

### `Store.commit_recovery_decision` (single atomic commit point)

Runs in ONE `BEGIN IMMEDIATE` and, in order: (a) fresh read + optional
snapshot-CAS (`expected`); (b) fence — job RUNNING, non-terminal, held by the
current `(owner_instance_id, lease_epoch)` with unexpired lease (stale/foreign
holder writes NOTHING → `LeaseFencedError`); (c) exactly-once (structural — the
transition atomically moves the job out of RUNNING, so a re-invocation for the
same terminal event fails the RUNNING fence); (d) audit columns persisted WITH
the transition.

Decision → transition mapping (no new primary states):

- `RETRY_BOUNDED` → QUEUED + `RETRY_BACKOFF` + attempt_no+1 + bounded
  `next_eligible_at` + `error_class=RESOURCE`.
- `DEFER_RESOURCE` → QUEUED + `RESOURCE_DEFERRED` + bounded
  `next_eligible_at` + `error_class=RESOURCE` (no attempt bump).
- `BLOCK_RESOURCE` / `PREFER_EXTERNAL` → BLOCKED (`terminal=BLOCKED`, not DONE)
  + `error_class=RESOURCE`; PREFER_EXTERNAL is a hint only.
- `QUARANTINE_LOST` → LOST (`status=RECOVERING`) + `error_class=OWNER_REQUIRED`.
- `COMPLETE` / `FAIL_NONRESOURCE` are rejected (`ValueError`) — "no resource
  action" outcomes are handled by the caller, never persisted as a transition.

## 5. Scheduler integration (classification point)

- `Scheduler.classify_and_recover(job_id, epoch)` is the central C3
  classification point. It reads the latest Process-Registry evidence (trusted
  store data), requires authoritative terminal evidence
  (`ProcessRegistry.is_terminally_dead`), derives the FailureClass, decides the
  RecoveryDecision, and commits exactly-once under the holder lease.
- `run_pass` routes a synchronous sandbox termination with resource evidence
  (`ActionOutcome.status == "resource_termination_failed"`) through
  `classify_and_recover`.
- `Supervisor._perform_run_sandbox_tests` signals `resource_termination_failed`
  when `_sandbox_resource_termination` classifies the persisted termination
  evidence as a resource failure (never a code failure). The Supervisor only
  signals; the Scheduler performs the fenced commit.
- Async agents: the classification is applied at terminal detection (the
  registry path) — `classify_and_recover` is the reusable point; the async
  process observation seam (populating registry terminal evidence for detached
  agents) is out of C3 scope and documented below.

## 6. Fencing / exactly-once

- The recovery commit is holder-CAS fenced (`owner_instance_id` +
  `lease_epoch` + unexpired lease), the same authority as Phase B.
- Exactly-once is **structural**: the classification point fires only for a
  RUNNING job; the commit atomically transitions RUNNING → non-RUNNING; a
  re-invocation (crash/restart/dual supervisor) fails the RUNNING fence. No
  separate idempotency table is required (and adding a new `supervisor_actions`
  `action_type` would require a risky CHECK-constraint rebuild with no
  additional correctness benefit — documented decision).

## 7. Integrated acceptance (§19) — `tests/test_phase_c3_acceptance.py`

1. HEALTHY: C1 ALLOW → C2 verified scope → exit 0 → NORMAL_EXIT → no resource
   failure (COMPLETE).
2. HOST PRESSURE: low MemAvailable → C1 DEFER/DENY → no spawn → no false code
   failure (RESOURCE_CAPACITY_FAILURE).
3. ENFORCEMENT UNAVAILABLE: C1 ALLOW → C2 unavailable → no unbounded child →
   RESOURCE_ENFORCEMENT_FAILURE → bounded recovery (DEFER / LOST on unknown).
4. OOM: RESOURCE_OOM → BLOCK_RESOURCE → no rework, no limit increase, no
   identical retry.
5. TIMEOUT: trusted timeout → RESOURCE_TIMEOUT → bounded RETRY_BOUNDED → no
   longer timeout (structural).
6. NORMAL NONZERO: exit ≠ 0 without resource evidence →
   CODE_OR_PROCESS_FAILURE → not relabelled OOM/timeout.
7. UNKNOWN: ambiguous → UNKNOWN_TERMINATION → LOST → no duplicate spawn.
8. RESTART: interrupted → reopen → exactly-once recovery.
9. DUAL SUPERVISOR: two owners → only the valid lease/epoch mutates.
10. TSGO reference: (a) host pressure → C1 blocks; (b) healthy+admitted → C2
    MemoryMax → process hits MemoryMax → RESOURCE_MEMORY_LIMIT → no rework, no
    limit increase, no infinite identical retry.

## 8. Test matrix (`tests/test_phase_c3_*.py`, 81 tests)

- `classification` (16): every FailureClass value + fail-closed rules.
- `evidence_integrity` (9): wrong scope/identity/stale boot/cgroup, malformed/
  missing/bounded evidence, agent reason ignored, store rejects free strings.
- `oom` (8): memory.events evidence; exit 137 without events ≠ OOM; unrelated
  (wrong-scope) events rejected; never identical retry.
- `timeout` (6): trusted marker; exit 124 alone ≠ timeout; no timeout increase.
- `recovery` (13): every decision + no-rework invariant + never-retry policy.
- `retry_bounds` (6): max attempts, backoff, next_eligible_at, reopen, no
  infinite retry.
- `fencing` (4): stale owner/epoch, dual supervisor, exactly-once.
- `restart` (5): crash before/after classification, old boot, running scope.
- `migration` (4): V10→V11 idempotent + non-destructive + deterministic.
- `acceptance` (10): the 10 integrated cases.

Regression: C2 (112), C1 (82), Phase-B (166) all green; full suite
(`--ignore=e2e-fixture`) green (1697).

## 9. Explicit non-goals / deviations

- **Phase D (Context Engineering) not implemented.** No `context_packs`.
- **No background service.** No systemd unit, no new privileges.
- **No new external rights.** `PREFER_EXTERNAL` is a persisted audit hint only;
  no external action is taken.
- **No `supervisor_actions` action_type added.** Exactly-once is structural
  (see §6); a CHECK-constraint rebuild was avoided as unnecessary migration
  risk.
- **Async-agent terminal observation** (populating registry terminal evidence
  for detached `enforce_and_spawn` agents) is not added in C3 — the agent
  result is consumed via the run-status provider, not the registry. The
  `classify_and_recover` point is fully reusable when that seam lands.
- **`test_phase3c_approval_core.test_schema_version_is_10`** was updated to
  assert `SCHEMA_VERSION == "11"` (renamed `test_schema_version_is_11`) — the
  only pre-existing test that hard-coded the version.

## 10. Open points

- Async agent terminal evidence seam (who calls `mark_terminal_with_evidence`
  for detached agents, and from which observation path).
- Whether `error_class` for `BLOCK_RESOURCE` should remain `RESOURCE` (chosen)
  vs `OWNER_REQUIRED` (the reopen still requires owner authorization
  regardless).
- Notification template reuse for new reason codes (Phase F/E concern; no
  free text added).
