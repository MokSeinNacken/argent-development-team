# PHASE G1 — Background Runtime & Reboot-Recovery Core (Notes)

> **SERVICE CREATED/VALIDATED != SERVICE ACTIVATED.**
> G1 delivers code, deterministic offline tests, and a statically validated
> systemd **user** service template. It does **not** enable, start, restart or
> activate anything. Activation is Phase G2 and is an explicit owner action.

## Scope

G1 adds the non-interactive background-runtime kernel on top of the existing
Phase B/C/D/E/F modules (durable queue/leases, scheduler/recovery, resource
governor, context packs, routing, test assurance). It does **not** duplicate
any of them.

New files:

| File | Purpose |
|---|---|
| `argent_core/runtime_paths.py` | Canonical runtime paths (SPEC G1 §A). |
| `argent_core/background_runtime.py` | `SupervisorInstance`, `ServiceHealth`/`HealthSnapshot`, `SupervisorRuntime` (loop/shutdown). |
| `argent_core/argent_service.py` | Non-interactive `main()` entrypoint + `ServiceConfig`. |
| `g1-systemd/argent-supervisor.service` | Versioned systemd **user** service template (validated, never activated). |
| `tests/g1_helpers.py`, `tests/test_phase_g1_*.py` | Deterministic offline tests (55 cases). |
| `docs/PHASE_G1_NOTES.md`, `docs/PHASE_G1_ACCEPTANCE.md` | This document + acceptance matrix. |

Minimal store change: `SCHEMA_VERSION` 16 → 17 (additive
`supervisor_instances` table) then 17 → 18 (fix-round F2: additive monotonic
`revision` INTEGER column + `host_id` column on `supervisor_instances`) plus
read/CAS primitives. No other product change.

## Canonical paths (SPEC G1 §A)

Durable data never lives under `/tmp` (Phase-B tmpfs-ENOSPC rule). Defaults
resolve per `Path.home()` (never a hardcoded `/home/<user>`), honouring the XDG
base directories when set:

- persistent state: `$XDG_STATE_HOME/argent` else `~/.local/state/argent`
- persistent artifacts: `$XDG_DATA_HOME/argent` else `~/.local/share/argent`
- cache: `$XDG_CACHE_HOME/argent` else `~/.cache/argent`

The systemd unit uses `StateDirectory=argent` / `CacheDirectory=argent`, which
map to exactly these locations.

## Single-active supervisor (SPEC G1 §C/§D/§O)

The authoritative liveness signal is the canonical identity tuple
`(boot_id, pid, process_start_ticks)` (Phase B3 `process_registry`). **PID is
never authority.** A persisted singleton row in `supervisor_instances` is
recovery evidence plus an atomic compare-and-swap fence
(`cas_supervisor_instance`), so two processes can never both believe they own
the store.

`SupervisorInstance.acquire()` classification (pure, tested):

- persisted identity incomplete → **AMBIGUOUS** (fail closed);
- live `boot_id` unreadable → **AMBIGUOUS** (fail closed);
- `owner.boot_id != live.boot_id` **and** same host (`host_id` matches) →
  **TAKEOVER** (provably dead across a same-host reboot);
- `owner.boot_id != live.boot_id` **and** different/unknown host →
  **AMBIGUOUS** (a shared FS/network store's foreign owner is never assumed
  dead — it may be alive on its own host);
- same boot, owner pid gone → **TAKEOVER**;
- same boot, owner pid alive but different `start_ticks` → **TAKEOVER** (PID reuse);
- same boot, pid alive, same `start_ticks` → **LIVE_OWNER** (refuse, no split-brain);
- any unreadable fact → **AMBIGUOUS** (refuse, never a takeover).

The `host_id` (D-Bus machine id, ``/etc/machine-id``) is persisted on the
instance row and is the shared-store fence: without provable same-host
identity a foreign boot can never be declared dead (F3a).

A second live supervisor is refused (`LIVE_OWNER`/`AMBIGUOUS`), and a non-blocking
`flock` on `~/.local/state/argent/supervisor.lock` adds defense-in-depth. The
entrypoint exits non-zero (`EXIT_OWNER_CONFLICT=3`) in those cases.

Instance identity is per-process (`instance:<uuid4>`), persisted, and fenced:
heartbeats/releases are CAS'd on `instance_id`, so a since-replaced instance
can never write again.  The singleton-row CAS fence is a **monotonic integer
`revision`** atomically incremented on every write (never `updated_at`), so two
concurrent takeover candidates can never both win even under a frozen clock
(F2, ABA-free).

## Startup reconciliation (SPEC G1 §E)

The entrypoint reuses `Scheduler.reconcile_after_restart()` (Phase B) verbatim:
terminal (DONE/FAILED/BLOCKED) jobs are immutable, QUEUED stays claimable,
WAITING_EXTERNAL is preserved without any LLM polling, RUNNING is reconciled by
process evidence (live → no takeover; provably terminal → bounded recovery;
ambiguous → LOST quarantine). No success is ever fabricated after a restart.

## Background loop (SPEC G1 §I)

`SupervisorRuntime.run_loop()` runs one bounded scheduler pass
(`Scheduler.run_pass`) plus one bounded non-LLM `check_due_waits()` per
iteration, then sleeps deterministically (idle 5s / active 1s), sliced into
0.25s steps for responsive stop-event checking. Idle passes cost ~one no-work
claim — negligible compute, no busy-spin, no held LLM. Pass exceptions are
contained (health degrades to `DEGRADED`, bounded error code) without aborting
the loop.

G1 fix-round: the loop's scheduler pass now ALSO steers (a) continuation of its
own still-leased RUNNING jobs (so a multi-step job progresses in background
operation instead of stalling at RUNNING) and (b) evidence-bound takeover of
expired-lease RUNNING jobs after a restart (F1).  A SIGTERM that lands mid-pass
aborts the pass BEFORE any spawn/test action via a stop-predicate wired into
the scheduler (F6); the job stays RUNNING under a valid lease (consistent,
re-claimable).  A lost instance fence (``heartbeat() == False`` or exception)
immediately fails the runtime (F3b); external-wait checks and the heartbeat
still run in error passes (F7d).

## Graceful shutdown (SPEC G1 §G)

SIGTERM/SIGINT → `request_shutdown()` sets `STOPPING` and the stop event. No new
runnable work is claimed after shutdown begins; the current bounded pass
completes (already fenced). The instance lease is released (CAS-fenced) with a
stop reason. Unfinished jobs are **never** marked PASS/DONE. `mark_failed()`
sets `FAILED` for unrecoverable errors without mutating any job.

## Service health (SPEC G1 §J)

Machine-readable `HealthSnapshot` (STARTING/READY/DEGRADED/STOPPING/FAILED) —
SERVICE health, distinct from job states. Fields: `instance_id`, boot identity,
`started_at`, `last_scheduler_pass_at`, `db_accessible`, `active_job_count`,
`external_wait_count`, `recovery_result`, `last_error_code`, `stopping_reason`.
No secrets.  G1 fix-round (F7): `_finalize` preserves `FAILED` (never
overwrites it with `STOPPING`); `main()` exits non-zero (`EXIT_FAILED=1`) when
the final state is `FAILED`; repeated (structural) scheduler errors escalate
to `FAILED` after a bounded consecutive-error threshold (transient errors stay
`DEGRADED`).

## HMAC / evidence-store boundary (SPEC G1 §M)

The Phase-F evidence MAC key is resolved **only** from the process environment
(`ARGENT_EVIDENCE_MAC_KEY_FILE` / `ARGENT_EVIDENCE_MAC_KEY`) or an explicit
trusted constructor argument — never from the store or from agent output, and
never committed. The service references the key only through the unit's
optional `EnvironmentFile=-%h/.config/argent/service.env`. The agent cannot
choose the key/store authority path (CODE-ENFORCED).

## Restart policy (SPEC G1 §L)

The unit uses `Restart=on-failure`, `RestartSec=5`, `StartLimitBurst=5`,
`StartLimitIntervalSec=120` — systemd rate limiting: `StartLimitBurst=5` within
a sliding 120s window bounds aggressive respawn bursts; NOT an absolute restart
cap (a process failing less often than ~30s restarts indefinitely; a stronger
persistent-flap policy is deferred to G3/operator gate).

## WSL boundary (SPEC G1 §P)

The unit is a Linux **user** service (`WantedBy=default.target`); its lifecycle
is designable/testable under WSL2. G1 changes **nothing** about `.wslconfig`,
Windows startup, Task Scheduler, or `loginctl enable-linger`.

## Resource governor (SPEC G1 §N)

The loop delegates to `Scheduler.run_pass()`, which performs the Phase-C
resource preflight/spawn-gate. The governor remains binding through the
background dispatch path (tested).

## CODE-ENFORCED vs OPERATIONALLY REQUIRED

CODE-ENFORCED (this phase, tested):

- single-active ownership via (boot_id, pid, start_ticks) + host identity + a
  monotonic-revision CAS (no split-brain, no ABA);
- fail-closed on ambiguous/live owner, malformed config, ephemeral paths
  (XDG overrides + symlinks via ``resolve()``), DB-path-outside-state,
  NaN/Infinity config, unknown config keys, missing store;
- reconciliation idempotency + immutable terminal jobs;
- graceful shutdown never fabricates DONE/PASS;
- no embedded secrets, no public listener, no systemd activation code;
- evidence MAC key sourced only from process env (never agent/store data);
- **spawn-environment allowlist**: every agent/test spawn passes an explicit
  minimal `env=` that strips ``ARGENT_EVIDENCE_MAC_KEY`` /
  ``ARGENT_EVIDENCE_MAC_KEY_FILE`` and the key-file path (F4);
- `FAILED` → non-zero exit; a lost instance fence stops the runtime.

OPERATIONALLY REQUIRED (Phase G2, owner action — not code):

- `daemon-reload`, `systemctl --user enable/start argent-supervisor`;
- provisioning the protected key file (`~/.config/argent/service.env` +
  `ARGENT_EVIDENCE_MAC_KEY_FILE`) outside the agent write area;
- choosing the working directory (`%h/argent`) and ensuring `argent_core` is
  importable there;
- a controlled live restart test and (optionally) a WSL reboot test.

## Limitations

- No real external-wait adapters yet (Phase J) — the wait manager runs with an
  empty adapter registry; existing waits are preserved but only allowlisted
  providers can ever wake.
- No health-file emission daemon; `HealthSnapshot` is in-process (a `health.json`
  writer is a possible G2 addition).
- `flock` is POSIX-only; on non-POSIX hosts the persisted CAS remains the
  authority (documented fallback).
- Instance-id is per-process (not persistent across restarts); job leases
  reconcile via process evidence, not instance-id continuity.
- The `host_id` shared-store fence assumes `/etc/machine-id` is a reliable
  per-host identifier; a host that clones its machine-id across machines would
  defeat it (documented operational assumption, not code-enforced).
