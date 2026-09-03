# PHASE I3-C1 NOTES — CI External-Wait / PR Lifecycle Core (GitHub READ-ONLY)

**Branch:** `phase-i3c1-ci-external-wait` (Base `2b804a13` = Phase I3-B GREEN).
**Datum:** 2026-09-03.
**Scope:** additive, provider-neutral CI-wait core + deterministic tests + docs.
**No commit, no push, no live-service/systemd/state-dir mutation, no GitHub
writes of any kind, no LLM agents.** Bounded GitHub READS (read-only) are
permitted for the live PR #1 probe only; deterministic fixtures are used
everywhere else.

---

## A. Scope, reuse, and the single source of truth

I3-C1 adds a **provider-neutral CI external-wait core** on top of the existing
Phase-B/G wait core. There is **NO second scheduler and NO second source of
truth**: the new module reuses the same `external_waits` table, the same
`WAITING_EXTERNAL` primary job state, the same atomic store primitives, and the
same bounded background loop.

- **NEW** `argent_core/ci_external_wait.py` — `CiState` model, deterministic
  check aggregation, partial failure classification, `CiWaitAdapter` protocol,
  `FakeCiAdapter`, trusted `CiWaitSpec`, `CiWaitManager`, `GitHubCiAdapter`
  (READ-ONLY).
- **EXTENDED** `argent_core/store.py` — SCHEMA 22 → 23 (additive nullable
  `ci_policy` / `ci_evidence` columns on `external_waits`); `list_due_external_waits`
  gains an optional `kind` filter; `complete_wait_and_requeue` gains optional
  `ci_policy`/`ci_evidence` so evidence is persisted **atomically** with the
  wake.
- **EXTENDED** `argent_core/external_wait.py` — `_build_wait_row` now emits the
  two new columns (None); `ExternalWaitManager` gains an optional `kinds`
  filter (default `None` = unchanged B3 semantics) so it can be scoped to
  non-CI wait kinds.
- **EXTENDED** `argent_core/background_runtime.py` + `argent_service.py` — the
  runtime runs `CiWaitManager.check_due_ci_waits()` in the SAME iteration as the
  scheduler pass and the existing external-wait check; `build_service` wires a
  CI-scoped `ExternalWaitManager` (non-CI kinds) plus a dedicated `CiWaitManager`
  (default no adapters = fail-closed; Main wires the GitHub adapter for the live
  read-only probe).

Reuse is deliberate: `CiWaitManager` calls `Store.transition_to_waiting_external`
(atomic RUNNING→WAITING_EXTERNAL) and `Store.complete_wait_and_requeue`
(idempotent wake) and `external_wait.next_check_delay_seconds` (the bounded
1/2/5/10/30-min backoff ladder). It does **not** reimplement the WAITING_EXTERNAL
state machine.

## B. Normalized CI state model + hard rules (code: `CiState`, `aggregate_ci_state`, `classify_ci_failure`)

The normalized, closed CI state model is exactly 12 states:

`PENDING · SUCCESS · FAILURE · CANCELLED · TIMED_OUT · ACTION_REQUIRED ·
NEUTRAL · SKIPPED · NO_CHECKS_CONFIGURED · PROVIDER_UNAVAILABLE · RATE_LIMITED ·
UNKNOWN`

Hard rules are code-enforced and tested:

- **NO_CHECKS_CONFIGURED != SUCCESS** — zero observed checks ⇒
  `NO_CHECKS_CONFIGURED` (never a fabricated green).
- **UNKNOWN != SUCCESS** — an unknown requirement set (`required=None`) ⇒
  `UNKNOWN`, never `SUCCESS`; a required check missing from the observed set ⇒
  `UNKNOWN`.
- **PROVIDER_UNAVAILABLE != CODE_FAILURE** and **RATE_LIMITED != CODE_FAILURE**
  — both classify as `PROVIDER`, never a code/test failure.
- **CANCELLED != SUCCESS** — a cancelled required check is a terminal
  non-success (distinct aggregate `CANCELLED`).
- **No LLM "looks green"** — aggregation is a pure function over structured
  `CiCheck` evidence only; there is no model call and no prose interpretation
  anywhere in the polling path.

Individual-check conclusions/statuses are closed sets too
(`CHECK_CONCLUSIONS`, `CHECK_STATUSES`), and unknown/malformed values are
rejected at validation, never reinterpreted.

## C. Wait identity binding + head-SHA fencing (code: `CiWaitSpec`, `ci_ref`, `_validate_read`)

A CI wait is created ONLY from a resolved, trusted `CiWaitSpec` (provider,
repository, PR number, expected head SHA, expected base, required/optional
check policy, candidate id). Agent prose can never create wait authority.

- `ref` = `owner/repo#<pr_number>` (canonical, bounded, parsed fail-closed via
  `parse_ci_ref`).
- `expected_subject` = the bound head SHA (a CI wait **requires** a non-empty
  SHA-like subject; `_validate_spec` enforces it).
- `expected_base` + required/optional checks + candidate id persist in the
  `ci_policy` JSON column, written ONLY at entry (pre-terminal); the fenced
  wake/backoff paths never rewrite it (terminal-immutable).

**Head-SHA binding:** the adapter reads check-runs/status for the BOUND head SHA
and separately reports the PR's CURRENT head (`CiRead.pr_head_sha`). If the
observed head differs from the bound SHA, the controller wakes `STALE`, persists
stale evidence (expected vs observed head), invalidates any prior success
evidence, and **requires new evidence for the new SHA** — a wait for PR #1 @ X
can never silently become PR #1 @ Y (no stale PASS reuse).

**Cross-job/repo/PR isolation:** `_validate_read` requires the read's
`repository` and `pr_number` to exactly match the parsed wait ref (CASE 30/31);
a mismatched read is `malformed`/`wrong_identity` and never wakes.

## D. Check aggregation + required/optional policy (code: `aggregate_ci_state`)

`aggregate_ci_state(checks, required, optional)` is a pure function:

1. unknown requirement set ⇒ `UNKNOWN`;
2. zero checks ⇒ `NO_CHECKS_CONFIGURED`;
3. any required check FAILURE/STARTUP_FAILURE ⇒ `FAILURE` (wins over missing);
4. any required check CANCELLED ⇒ `CANCELLED`;
5. any required check TIMED_OUT ⇒ `TIMED_OUT`;
6. any required check ACTION_REQUIRED ⇒ `ACTION_REQUIRED` (owner-action gate);
7. any required check NEUTRAL/SKIPPED/STALE ⇒ `UNKNOWN`;
8. a required check missing from the observed set ⇒ `UNKNOWN` (never SUCCESS);
9. any required check still pending ⇒ `PENDING`;
10. all required present + SUCCESS ⇒ `SUCCESS`.

The documented priority is terminal non-success (FAILURE > CANCELLED >
TIMED_OUT > ACTION_REQUIRED) > missing/neutral ⇒ UNKNOWN > PENDING > SUCCESS —
a failing required check wins over a missing one (a failure is never masked).

An explicit **empty** required set means "no named required checks".  Because
this phase retrieves a SINGLE unpaginated check-runs request with no
branch-protection/ruleset completeness proof, an empty required set is **never
aggregated to SUCCESS** from a partial universe: a terminal non-success (an
observed failing check) is still reported, otherwise the aggregate is
conservatively `UNKNOWN`.  Optional checks are informational and never fail the
aggregate.

Required-check policy comes from **trusted sources only** (the `CiWaitSpec` /
task config / repo policy); external PR text/agent prose never defines the
required set. A required check that was previously observed and then vanishes
is a **material required-check-set change** and wakes conservatively
(`required_check_set_changed`).

**Operational fact:** `MokSeinNacken/argent-development-team` `main` has **no
branch-protection rulesets** — the code does not pretend branch protection
exists; the required-check set is therefore always explicitly supplied from
local policy (never inferred from a non-existent protection config).

## E. Wake semantics + wake-once (code: `CiWaitManager._process_ci_wait`, `_wake`)

Wake conditions (exactly these; **not** on poll-attempt increments alone):

- terminal aggregate `SUCCESS` / `FAILURE` / `CANCELLED` / `TIMED_OUT` /
  `ACTION_REQUIRED`;
- head SHA changed (`stale_head_change`) or base ref changed
  (`base_ref_changed`) — a changed/missing identity field never SUCCESS-wakes;
- PR closed (`pr_closed`) / PR merged (`pr_merged_unexpected` — distinct; merged
  ≠ Argent-authorized merge);
- required-check set materially changed (`required_check_set_changed`);
- deadline (`WAIT_DEADLINE`, `ErrorClass.EXTERNAL`).

`PENDING`, `UNKNOWN`, `NO_CHECKS_CONFIGURED`, `NEUTRAL`, `SKIPPED` and provider
outage/rate-limit do **not** wake.  A clean read whose head SHA is missing
(`missing_head`), whose base is missing/mismatched, or whose PR lifecycle is
`UNKNOWN` fails closed (conservative backoff / `STALE` wake) — never a SUCCESS
wake from an unbound read.

**Wake-once** is enforced by: (a) persisting evidence **first**, then
(b) `Store.complete_wait_and_requeue` atomically marking the wait terminal AND
requeueing the job in ONE `BEGIN IMMEDIATE` transaction, and (c) idempotence
(a second call returns `None` → `ignored`).  The wake and every backoff/evidence
update are **instance-fenced** (`expected_instance_id` must still hold the
single-active singleton fence) and **terminal-immutable**
(`terminal_observed_at IS NULL` is required), so a stale instance or a late
provider response can never requeue or corrupt a terminal wait.  A terminal
wait is never re-listed
(`list_due_external_waits` filters `terminal_observed_at IS NULL`), so a
duplicate provider response cannot create a second wake/task. `event_version`
is persisted as the durable provider transition identity (GitHub adapter: max
check-run id).

**Success/failure paths:** CI success = external prerequisite satisfied; the
parent workflow (not the wait manager) decides the next step — a job is NEVER
set `DONE`/`FAILED` directly, only re-queued (`QUEUED`) with `WAIT_EVENT`.
Failure persists failing check identity / conclusion / provider run ref / head
SHA / bounded logs ref / classification before the wake, with deterministic
partial classification (`classify_ci_failure`): `CODE_FAILURE`,
`INFRASTRUCTURE_FAILURE`, `CANCELLED`, `TIMEOUT`, `PROVIDER`, `UNKNOWN`.
Post-wake reasoning is routed per the Phase-E capability policy; the manager
never automatically blames the Writer.

## F. Provider outage / rate-limit / backoff (code: `_backoff`, `_backoff_rate_limited`)

- Outage/network/unknown provider error ⇒ keep `WAITING_EXTERNAL`, persist
  `PROVIDER_UNAVAILABLE` evidence + `PROVIDER` classification, bounded backoff,
  no LLM, **no conversion to a Writer failure** (the job stays waiting; never
  `FAILED`).
- Rate limit ⇒ same, but `_backoff_rate_limited` respects the reset/eligible
  time when observable (`rate_limit_reset_at`), else the bounded ladder.
- The backoff ladder is reused from `external_wait.next_check_delay_seconds`
  (1/2/5/10/30 min + jitter, capped) — no busy polling, no retry storms.

## G. LLM-release / no-active-LLM invariant (code: `test_waiting_job_not_claimable_and_no_agent_dispatch`, runtime wiring)

`WAITING_EXTERNAL` jobs stay in place in scheduler passes (existing B3/G
behavior). Polling is deterministic provider work by the trusted controller
path — there is **no model call, no role-run, no execution scope** while
waiting. Tests assert `list_dispatches(task_id) == []` and `primary_state ==
WAITING_EXTERNAL` across pending polls. Models are activated only AFTER a wake,
via the parent workflow's normal admission/claim path. There is no second
daemon: the CI manager runs inside `SupervisorRuntime._run_one_iteration`
alongside the scheduler pass.

## H. Crash/restart recovery, PR lifecycle, boundaries, notifications, PAT risk

**Crash/restart** (deterministic, via the store + manager):

- *before first check* — the wait row (incl. `ci_policy`) is durable; a reopen
  re-lists it as due (`test_wait_survives_restart_and_later_check_works`,
  `test_reopen_before_first_check_keeps_wait_due`).
- *after provider response before persistence* — provider reads are
  side-effect-free; a crash discards only the in-memory read and the next poll
  re-reads (no lost wake, no fabricated SUCCESS).
- *after persistence before parent wake* — evidence + terminal + requeue are ONE
  transaction, so there is no intermediate window.
- *during wake transition* — `complete_wait_and_requeue` is idempotent
  (`test_wake_is_idempotent_no_duplicate_task`).

No duplicate logical wake, no lost wait, no fabricated SUCCESS, no stale-holder
finalization.  The poll path re-verifies the single-active singleton fence
BEFORE processing due waits (a taken-over instance aborts with no writes); the
wake and backoff/evidence writes are instance-fenced and terminal-immutable
(`complete_wait_and_requeue` + `update_external_wait_fenced` require the current
fence and `terminal_observed_at IS NULL`).  CI wait entry is fail-closed against
an active dispatch/role-run/process (`_ensure_no_active_process`).

**PR lifecycle** is normalized read-only (`OPEN`/`CLOSED`/`MERGED`/`UNKNOWN`);
unexpected `CLOSED`/`MERGED` wake conservatively with a distinct reason and
authorize **nothing** new from provider state.

**External-content boundary:** every provider observation is UNTRUSTED DATA,
normalized into `CiRead`, strictly validated (`_validate_read`: bounded length,
closed sets, identity match), and only a bounded subset ever takes effect. No
provider value can write code, approve, set DONE, expand scope, escalate a
model, or read credentials.

**Notification policy:** unchanged pending polls do **not** notify the owner
(the manager returns `pending` with no wake); only meaningful DONE/ERROR/GATE
transitions follow the existing notification policy. A failing individual check
is not an automatic ERROR. Notification is the supervisor's concern, not the
manager's.

**Broad-PAT residual risk (roadmap requirement, documented):** the live `gh`
auth is a **classic PAT** (`gist/read:org/repo/workflow`) on account
`MokSeinNacken`. Because `main` has no rulesets, a broad classic PAT retains
direct push authority to `main`. The mitigations are policy fences (broker
protected-ref + branch-namespace policy, task-scoped allowlist), not provider
protection. **Roadmap requirement:** the Owner should replace the broad classic
PAT with a fine-grained PAT or a GitHub App before broad productive autonomous
writes / I4. No rotation in this phase.

**Boundary to I3-C2:** this phase is READ-ONLY. Real CI acceptance (a real
check-run on the acceptance repo) and any provider mutation/scheduler wiring
that depends on a real protected-branch setup are I3-C2 / later; I3-C2 requires
`REAL_CI_ACCEPTANCE_SETUP_REQUIRED` (or an Owner gate) per §38 of the brief.

## Verification (this writer)

- Targeted: `tests/test_phase_i3c1_*.py` → **62 passed**.
- Full suite: **2915 passed** (baseline 2853 + 62 I3-C1; Python 3.14 / pytest
  9.1.1).
- Schema-version regression assertions (I3-A/I2/3C/D3) updated 22 → 23 (same
  necessity as I3-A's 21 → 22 update); no other I3-A/I3-B test semantics
  changed beyond the documented deployment-default update.
