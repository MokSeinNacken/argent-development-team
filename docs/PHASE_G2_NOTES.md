# PHASE G2 — systemd User-Service Activation + Controlled Live Restart/Recovery (Notes)

> **STATUS: draft / notes.** Live-activation evidence is owned by the
> Supervisor and documented separately (see `PHASE_G2_ACCEPTANCE.md`).  This
> file captures the deterministic/code side and the deployment architecture
> only — **no secrets, no key values, no non-canonical path secrets**.

## Scope

G2 turns the Phase-G1 background-runtime kernel into a *running* Linux
**user** service and proves controlled live restart/recovery.  It splits into
two disjoint parts:

1. **Deterministic / code side (this phase's writer)** — config loading, unit
   static contract, the spawn-environment secret boundary, single-active
   ownership determinism, store-path invariants.  All offline, all injected.
2. **Live-activation evidence (Supervisor)** — the owner-authorized
   `daemon-reload`/`enable`/`start`, the controlled restart, the SIGKILL →
   bounded-restart recovery, journal secret-freedom, idle CPU, and the
   single-active second-instance refusal.  **Not performed by the writer.**

## Deployment architecture

```
operator (explicit owner action)
   │  provision protected files + enable/start (Supervisor-run, owner-authorized)
   ▼
~/.config/argent/            ← protected (0700), OUTSIDE any agent write area
   ├─ service.env            ← 0600; ONLY  ARGENT_EVIDENCE_MAC_KEY_FILE=<path>
   └─ evidence_mac.key       ← 0600; the evidence MAC key bytes (never printed)
~/.local/state/argent/       ← StateDirectory=argent  (XDG canonical, never /tmp)
   ├─ argent.db              ← durable SQLite (SCHEMA 18)
   ├─ supervisor.lock        ← advisory flock (defense-in-depth)
   └─ health.json            ← optional machine-readable health (G1 §J)
~/.config/systemd/user/
   └─ argent-supervisor.service  ← versioned template, WorkingDirectory=%h/argent
```

The unit (see `g1-systemd/argent-supervisor.service`) is:

- a **user** service (`WantedBy=default.target`, no `User=root`, no `User=` at all);
- `Type=simple`, `ExecStart=/usr/bin/python3 -m argent_core.argent_service`
  (absolute interpreter, **no shell**);
- `EnvironmentFile=-%h/.config/argent/service.env` (the `-` prefix = optional;
  a missing file is not a unit error).  It references the key **by path only**;
- `StateDirectory=argent` / `CacheDirectory=argent` → the canonical XDG dirs
  (never `/tmp` — the Phase-B tmpfs-ENOSPC rule);
- `Restart=on-failure`, `RestartSec=5`, `StartLimitBurst=5`,
  `StartLimitIntervalSec=120` → systemd rate limiting: `StartLimitBurst=5`
  within a sliding 120s window bounds aggressive respawn bursts; NOT an
  absolute restart cap — a process failing less often than ~30s restarts
  indefinitely (a stronger persistent-flap policy is deferred to G3);
- `KillSignal=SIGTERM`, `TimeoutStopSec=30`, `NoNewPrivileges=yes`.

The evidence MAC key is resolved **only** from the process environment
(`ARGENT_EVIDENCE_MAC_KEY_FILE` → the file, else `ARGENT_EVIDENCE_MAC_KEY`), and
never from the store or agent output.  Every agent/command spawn passes a
minimal allowlisted environment that strips `ARGENT_EVIDENCE_MAC_KEY`,
`ARGENT_EVIDENCE_MAC_KEY_FILE` and the key-file path (see
`argent_core.execution_scope.agent_spawn_env`).

## What is deterministically tested (this phase)

| Concern | Tests |
|---|---|
| Config loading: canonical XDG defaults + env overrides + relative→home | `tests/test_phase_g2_config.py` |
| Config fail-closed: malformed JSON / non-object / wrong type / unknown field / NaN·Infinity / ephemeral / symlink→`/tmp` / DB-outside-state | `tests/test_phase_g2_config.py` |
| `service.env` contract: path reference only, never a key value; resolves through `_resolve_mac_key` | `tests/test_phase_g2_config.py` |
| Store-path invariant: DB/health/lock under `state_dir`, never worktree/`/tmp` | `tests/test_phase_g2_config.py` |
| Unit static: no secret, no root, rate-limited restart, SIGTERM, NoNewPrivileges, no shell, dirs/EnvironmentFile semantics | `tests/test_phase_g2_unit_static.py` |
| Deployment helper is read-only (never activates) + installed-unit directive validation | `tests/test_phase_g2_unit_static.py` |
| Spawn-environment boundary: strips key value + key path + all non-allowlisted vars | `tests/test_phase_g2_agent_env.py` |
| Single-active determinism: revision-CAS exactly-one-winner, no split-brain, liveness table, host fence | `tests/test_phase_g2_singleton_deterministic.py` |
| Agent-dispatch sandbox (F1): bwrap argv (ro-root + tmpfs masks), adversarial real-spawn probe, fail-closed missing bwrap, short-timeout kill | `tests/test_phase_g2_sandbox.py` |
| Prompt-file lifecycle (F3): deterministic unlink on terminal outcomes, sweep age/count floor | `tests/test_phase_g2_prompts.py` |

## What is proven LIVE (Supervisor, not this writer)

See `PHASE_G2_ACCEPTANCE.md` (Supervisor).  Summary of the live evidence:

- protected paths (`~/.config/argent` 0700) and key file (0600) provisioned;
- `service.env` 0600 with only `ARGENT_EVIDENCE_MAC_KEY_FILE=…`;
- unit installed, `daemon-reload` OK, first start OK (instance ACTIVE), `enable` OK;
- second instance refused (exit 3, "another supervisor holds the lock");
- controlled restart OK (new instance/rev); graceful stop (RELEASED)/start OK;
- crash-style SIGKILL → bounded systemd restart OK;
- journal secret-free; idle CPU ~0.5%; rate-limited restart policy;
- `notification_outbox` 0; exactly 1 ACTIVE `supervisor_instance`; DB at
  `~/.local/state/argent/argent.db` (the 0-row outbox is a null-job
  observation — it proves nothing was duplicated, NOT a dedup property;
  external delivery is at-least-once with bounded dedup per the phase3a tests).

## Deployment helper (read-only)

`g2-systemd/install-check.sh` validates the deployment contract **without**
activating anything: unit presence, no embedded key value, `service.env`
path-reference-only + mode 0600, key-file mode 0600.  It never prints secrets
or key-file paths, and contains no activation command.  It also validates the
INSTALLED unit (read-only, via `systemctl --user show -p FragmentPath`): its
effective directives must equal the template with the three deployment
substitutions applied (Documentation / WorkingDirectory / EnvironmentFile).

## Agent-dispatch filesystem sandbox (F1)

The agent-dispatch boundary is a REAL filesystem trust boundary, not just an
environment allowlist.  Each dispatched agent child (same UID 1000) is wrapped
in a read-only-root `bwrap` namespace (`argent_core.execution_scope.
build_agent_sandbox_argv`):

* `--ro-bind / /` — read-only root;
* writable binds for `~/.openclaw` (sessions/trajectories the supervisor reads
  back) and the inherited working directory/worktree the child must write;
* empty `--tmpfs` masks over `~/.config/argent` and `~/.local/state/argent` —
  the real key/DB are ABSENT (a read raises `FileNotFoundError`; a write lands
  on an ephemeral tmpfs that vanishes);
* `--tmpfs /tmp`, `--dev /dev`, `--proc /proc`, and NO `--unshare-net` (the
  agent needs provider network).

Fail-closed: if `bwrap` is unavailable or the wrap fails at spawn time, the
dispatch fails (`SCOPE_CREATION_FAILED`) — there is NO unwrapped fallback — and
the service startup preflight refuses to start (`EXIT_INIT_ERROR`).

`tools.profile=minimal` is NOT the boundary — it is an operational
belt-and-braces profile setting only; the code-enforced boundary is the bwrap
sandbox above.

## Limitations

- The live-activation evidence is inherently non-deterministic and is owned by
  the Supervisor; this writer proves only the code/static side.
- `_resolve_mac_key` accepts any readable key-file path the operator supplies
  via `service.env`; `/tmp`-residency of the *key file* is an operational
  concern (operator provisions it under `~/.config/argent`), not code-enforced.
- `flock` is POSIX-only; on non-POSIX hosts the persisted revision CAS remains
  the single-active authority (documented G1 fallback).
- `health.json` emission is not yet wired into the running loop (G1 limitation,
  unchanged here).
- Pre-existing (deferred, outside this repo): the workspace policy reference
  `/home/pc/.openclaw/workspace/ARGENT_SUPERVISOR.md` (AGENTS.md) is absent;
  the embedded Argent policy in AGENTS.md governed this phase as in prior
  phases.  Not fixed here — creating/restoring a policy file is an owner
  policy decision, not a G2 action.
