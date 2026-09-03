# PHASE G3 ACCEPTANCE — REAL WSL Restart Recovery (G3-A pre-reboot + G3-B post-reboot)

**Branch:** `phase-g3-wsl-restart-acceptance` (Base `54a1bfb` = G2 GREEN, `ARGENT_PHASE_G2_SYSTEMD_LIVE_GREEN`).
**Datum:** 2026-09-03 (G3-A 14:14–14:38, G3-B 14:44–15:10)
**Owner-Autorisierung:** G3-A-Brief (14:14) + G3-B-Brief (14:44); Deployment/Restart im autorisierten Phase-G-Service-Scope.

**STATUS: G3 COMPLETE — REAL WSL RESTART PERFORMED AND PROVEN.**
**REAL WSL RESTART: PERFORMED (Owner, `wsl --shutdown` + `wsl -d Ubuntu`), bewiesen durch boot_id-Wechsel.**

---

## 1. G2-Basis-Verifikation (G3-A)

- `54a1bfb393a829303794dc0b11dc09fa4dab1dd6` = tatsächlicher G2-GREEN-Commit (`ops(supervisor): activate and verify systemd user runtime (G2, F1-F7 closed)`), G2-Worktree sauber, keine spätere Mutation.
- `docs/PHASE_G2_ACCEPTANCE.md` existiert; Marker `ARGENT_PHASE_G2_SYSTEMD_LIVE_GREEN`; Unit aus akzeptierter G2-Config (install-check OK); keine ungelösten G2-HIGH/CRITICAL.

## 2. PRE-REBOOT-Zustand (Checkpoint, v1.0)

Checkpoint: `~/.local/state/argent/g3/g3-pre-reboot-checkpoint.json` (validiert, exit 0).
boot_id `17b1e3d1-0db3-4784-9766-9de4092bad59` · host_id `c93d7bd30f4149b8abe9cf3371c7c6d3` · Instance `instance:bb3d4ad9eb9f45a0912fe1b942d9d51c` pid 216366 ticks 10933666 rev 557 ACTIVE · Service enabled+active+READY · Unit-SHA `40b408fa…` · DB SCHEMA 18 · Jobs/Outbox/Waits 0.

## 3. REAL REBOOT PROOF (G3-B, §0/§2)

| Metrik | Pre | Post | Beweis |
|---|---|---|---|
| boot_id | `17b1e3d1-0db3-4784-9766-9de4092bad59` | `28e93f86-8c93-43ed-bc88-a083851b5703` | `/proc/sys/kernel/random/boot_id`; Journal enthält beide Boots (`-- Boot 28e93f86… --`) |
| machine-id | `c93d7bd3…` | `c93d7bd3…` (unverändert) | gleiche Distro-Installation |
| uptime | — | 3 min | neuer Boot |
| user manager | systemd[351] | systemd[231] | neuer User-Manager im neuen Boot |

**REAL WSL RESTART PROVEN — kein PID-only-/Prozess-Neustart-Inferenz.**

## 4. AUTO-START + OLD-AUTHORITY-INVALIDIERUNG (G3-B, §3/§4)

- **Auto-Start bewiesen:** Journal im neuen Boot: `-- Boot 28e93f86… --` → `systemd[231]: Started argent-supervisor.service` **14:41:56** — der enabled User-Service startete automatisch mit dem User-Systemd (kein manueller Start vor der Beweissammlung; NRestarts=0).
- **Old Authority tot:** alter Prozess 216366 existiert nicht mehr; alte (boot_id `17b1e3d1…`, pid 216366, ticks 10933666, instance `bb3d4ad9…`) kann keine Live-Autorität sein (boot_id-Fencing; kein PID-Reuse möglich — neue PID/ticks/instance völlig verschieden).
- **Instance-Kette (revisionsmonoton, kein Split-Brain):**
  `557` (pre, bb3d4ad9…/216366/17b1e3d1…) → `568` (post-reboot Auto-Start, d52f613d…/319/28e93f86…) → `572` (G3-Deploy-Restart, dd91d579…/1219) → `598` (Fix-Round-Deploy-Restart, bb1014eb…/7096).
- **Genau 1 ACTIVE** `supervisor_instances`-Row durchgehend; Zweitinstanz-Probe live: exit 3 `fatal: another supervisor holds the lock`, DB unverändert.
- Start-Ticks post-reboot `533`/`26482` (neue Boot-Uhr) — alte Ticks `10933666` nicht wiederholbar.

## 5. PERSISTENT-STATE-SURVIVAL (G3-B, §5)

- DB `~/.local/state/argent/argent.db` überlebte: quick_check `ok`, SCHEMA 18, 0600/0700, nicht /tmp.
- G3-Checkpoint überlebte + validiert (0600/0700).
- Evidence-MAC-Key überlebte (0600, 44 B, mtime unverändert 00:47 — Reboot/Deploy hat ihn nicht angefasst); `service.env` 0600, nur Pfad-Referenz.
- Installed Unit überlebte; Identität verstanden: G2-Hash `40b408fa…` → nach autorisiertem G3-Deploy (WorkingDirectory/Documentation → G3-Worktree) Hash `868f1192…`; install-check (G3-Werte) OK; Unit-Diff exakt nur die 2 Deployment-Zeilen.
- Kein `/tmp`-Dependency für durable Recovery (kanonische XDG-Pfade; `/tmp` im Sandbox maskiert; Code lehnt `/tmp|/run|/dev/shm` für State ab).
- Job-State-Counts pre == post: alle 0 (QUEUED/RUNNING/WAITING_EXTERNAL/OWNER_GATE/BLOCKED/FAILED/LOST/DONE je 0; outbox 0; waits 0; registry 0; actions 0). Terminale Jobs konnten nicht reopenen; kein unfertiger State wurde fabriziert DONE/PASS (Store leer + deterministische State-Machine-Tests). Reconciliation idempotent (Startup-Reconciliation nach Instance-Acquire, fail-closed).

## 6. ACTION-/NOTIFICATION-REBOOT-SAFETY (G3-B, §7)

Reboot erzeugte keine Duplikate: `supervisor_actions` 0, `notification_outbox` 0 (keine neue Row durch Reboot), `external_waits` 0 (kein Doppel-Wake). Semantik unverändert: 1 logische Outbox-Row pro Event, lokale Dedup, externe Zustellung at-least-once (Phase-3A-Tests: `test_crash_after_accept_before_sent_at_least_once` u. a.). Kein Anspruch auf exactly-once (Telegram).

## 7. TRUST-BOUNDARY: G2-Befund, G3-Narrowing, Sol-HIGH #12, Fix-Round (§8/§9/§10)

### 7.1 G3-A-Befund (live bestätigt, G2-Policy)
G2-Sandbox band **ganz `~/.openclaw` rw** → Agent-Child konnte `openclaw.json`, `.env`, `credentials/`, `identity/`, `exec-approvals.json` überschreiben (Live-Probe: VISIBLE+WRITABLE). **Real Defekt.**

### 7.2 G3-Narrowing (deployed 14:46, Instanz dd91d579…/rev 572)
Writable = `~/.openclaw/agents/<agent_id>` + `~/.openclaw/workspace/<agent_id>` + expliziter Workdir; Rest von `~/.openclaw` RO via ro-root; `~/.config/argent` + `~/.local/state/argent` maskiert (absent); Agent-ID-Fail-closed. Produktions-Live-Probe durch echte Enforcer→bwrap-Kette: PASS (Config/Credentials RO + EROFS-Denials, Key/DB absent, Per-Agent-Runtime rw, Scope-Binding exact, Cleanup bewiesen, keine Residuen).

### 7.3 Sol-HIGH #12 (implicit cwd) + Fix-Round (deployed 15:05, Instanz bb1014eb…/rev 598)
Sol fand: Sandbox band implizit `Path.cwd()` des Service (= deploytes Quell-Worktree) rw → jedes Kind konnte den deployed Argent-Codebaum beschreiben. **Fix (ein Pro-Fix-Round, Fortsetzung des G3-Writer-Tasks):** kein impliziter cwd-Bind mehr; Workdir wird ausschließlich explizit aus `job.canonical_worktree_path` durch `enforce_and_spawn(workdir)` → `start_in_scope(workdir)` → `build_agent_sandbox_argv(cwd=workdir)` gereicht; ohne Workdir startet das Kind mit `cwd="/"` (ro) und ohne Worktree-Bind. Neue Tests `tests/test_phase_g3_workdir.py` (5).
**Live-Nachweis (Produktions-Enforcer, fix-round-Code):** (a) ohne Workdir: Mounts zeigen NUR `agents/<id>` + `workspace/<id>` rw; Schreibversuche auf Service-Worktree + `argent_core/execution_scope.py` → EROFS; Config/.env → EROFS; Key/DB absent; Per-Agent-Runtime rw. (b) mit explizitem Workdir: Kind startet dort (pwd), Service-Worktree weiterhin EROFS. Keine Residuen. Scope-Cleanup + Inactivity bewiesen.

### 7.4 Unwrapped-Fallback / Resource Governor
bwrap-Mandatory (Dry-Run OK, Preflight EXIT_INIT_ERROR bei Fehlen, Spawn-Fail-closed ScopeCreateError); Produktions-Dispatch ausschließlich `_spawn_scoped` → `ExecutionEnforcer.enforce_and_spawn` (`sandbox_wrap=True` Default); Legacy-`OpenClawRunLauncher.spawn` ohne Produktions-Caller; `run_in_scope`/`enforce_and_run` nur Sandbox-Test-/Bounded-Run-Pfad (unverändert). Cgroup/Scope-Pfad live funktional (transiente Scopes inkl. Probes; Start-Barrier + exaktes Binding verifiziert; kein Leftover-Scope im Idle).

## 8. POST-DEPLOY SERVICE HEALTH (G3-B, §11)

- enabled YES · active YES (`running`) · READY (ACTIVE-Row + frischer Heartbeat + genau 1 Prozess + `last_error_code=None`; health.json nicht verdrahtet = dokumentierte Limitation).
- Einzige Autorität: `instance:bb1014eb0bab4d2385ab5580da36328a` pid 7096, boot `28e93f86…`, rev 598, ACTIVE; 1 ACTIVE-Row; Zweitinstanz exit 3.
- 0 Children (kein Child-Leak; beendete Agenten können als Zombie unter dem Spawner verbleiben bis Prozess-Ende — beobachtet, bounded, erhält PID-Evidenz, reaped bei Exit; kein Running-Child), kein Busy-Spin (Idle-CPU ~0.1–0.2 %, RSS ~33 MB, cumulative CPU niedrig), kein Restart-Loop (NRestarts=0 nach jedem kontrollierten Deploy), kein Listener, Journal-Secret-Scan 0, kein Memory-Pressure durch Idle-Argent, kein `/tmp`-Dauerwachstum durch Service.

## 9. LINGER / WINDOWS-BOUNDARY (G3-B, §12)

- `Linger=yes` **vorbestehend**: `/var/lib/systemd/linger/pc` mtime **2026-08-20 19:24** (vor G1/G2/G3). Weder G2 noch G3 erzeugte/modifizierte es. Unverändert.
- **PROVEN BY G3:** WSL-Distro-Start → Linux/User-Systemd verfügbar → enabled Argent-Service recovered (Journal-Beweis).
- **NOT PROVEN / NOT CLAIMED:** Windows-Boot startet WSL automatisch. Kein Windows Task Scheduler/Startup-Mechanismus hinzugefügt; keine `.wslconfig`-Änderung.

## 10. DETERMINISTIC TESTS (G3-B, §13/§15; Supervisor-eigene Läufe)

| Gruppe | Ergebnis |
|---|---|
| G3 (Checkpoint 24 + Workdir 5) | 29 passed |
| G2 | 69 passed |
| G1 | 78 passed |
| F | 223 passed |
| E | 208 passed |
| D | 243 passed |
| C | 296 passed |
| B | 166 passed |
| übrige Dateien (2A/2B/2C/3A/3C/3D/… inkl. delivery, security, sandbox_runner) | 1256 passed |
| **FULL SUITE** | **2568 passed in 38.95 s** |

Validator auf realem Checkpoint: `checkpoint valid (v1.0)`, exit 0. Abdeckung inkl.: Checkpoint-Validierung, Boot-Identität geändert/unverändert, Old-Instance-Invalidierung, Persisted-DB-Survival, Job-State-Preservation, Terminal-Immutability, Singleton-Post-Reboot, Auto-Start-Erwartung, geschnallte OpenClaw-Sandbox-Permissions, Argent-Key/State hidden, kein Unwrapped-Fallback, malformed/tampered Checkpoint fail-closed, Pre/Post-Vergleich, Closing-Invariants. Zusätzlich echte Live-Beweise (Reboot, Enforcer-Proben) — Simulation ersetzt nichts.

## 11. SOL CLOSING REVIEW (GENAU 1×, read-only, gpt-5.6-sol, low reasoning)

Areas 1–20 geprüft; Ergebnis: **kein CRITICAL**; 17× INFO/PASS; 1× LOW (Code-enforced vs. operational: health.json unwired, /tmp-Key-Residency teils operational); **3× HIGH — alle geschlossen:**
1. **#12 minimal writable surface / production cwd** → Fix-Round (7.3), deterministisch + live bewiesen. GESCHLOSSEN.
2. **#18 live documentation consistency** (PHASE_G3_ACCEPTANCE.md behauptete noch „NOT YET PERFORMED") → diese Datei jetzt vollständig POST-REBOOT + PHASE_G_ACCEPTANCE.md. GESCHLOSSEN.
3. **#20 mutable deployment risk** → mitigiert: (a) Sandbox-Kinder können den deployten Codebaum nicht mehr schreiben (Fix #12, Live-Probe); (b) Closing-Commit macht das Ziel-Worktree zum stabilen Committed Artifact; Restrisiko (Unit-`WorkingDirectory` hängt am Worktree-Pfad, Löschung durch Operator würde Unit brechen) = dokumentiertes operatives Risiko, identisches Modell wie G2. GESCHLOSSEN (als dokumentiertes Restrisiko).

Keine weiteren HIGH/CRITICAL. Kein zweiter Sol-Review.

## 12. DEPLOYMENT-ENDZUSTAND

- Installed Unit: `/home/pc/.config/systemd/user/argent-supervisor.service`, SHA-256 `868f1192422635db62d73d3e9064740c54db3b79a69654768fca8a3f12b49f4a`, enabled, WorkingDirectory/Documentation → G3-Worktree (einzige Abweichungen vom Template = die 3 Deployment-Substitutionen; install-check OK). Pre-G3-Deploy-Backup: `$UNIT.pre-g3-deploy`.
- Service: active/running, PID 7096, Instance `bb1014eb…`, rev 598, READY.
- G3-Worktree-Code == live deployter Code (gleicher Pfad).

## 13. BEKANNTE LIMITATIONEN (dokumentiert, kein G3-Blocker)

- `health.json`-Emission nicht verdrahtet (G1-Deferral); READY operativ aus Instance-Row/Heartbeat abgeleitet.
- `_resolve_mac_key` akzeptiert jeden Operator-Key-Pfad; `/tmp`-Residenz operativ verhindert, nicht code-erzwungen (State-/Cache-Pfade aber code-rejected unter /tmp//run//dev/shm).
- Exited-Agenten können bis Service-Exit als Zombie unter dem Spawner verbleiben (bounded; PID-Evidenz bleibt gültig; kein Running-Child-Leak; Scope-Cleanup/Inactivity jeweils bewiesen).
- Unit-WorkingDirectory hängt am (nun committed) G3-Worktree-Pfad — Worktree-Löschung wäre Operator-Risiko (Modell wie G2).
- Kein echter External-Wait-Adapter (WAITING_EXTERNAL-Semantik deterministisch getestet, Adapter später).

## 14. VERIFIKATIONS-DISTINKTION

- **CODE-ENFORCED:** Singleton-CAS/Fencing, Reconciliation-Reihenfolge, bwrap-Preflight/Wrap, Env-Allowlist, Cgroup-Verifikation, Durable-Path-Rejection, Sandbox-Narrowing (kein impliziter cwd-Bind; nur explizite Workdirs + Per-Agent-Runtime rw), Agent-ID-Fail-closed, Checkpoint-Schema fail-closed.
- **OPERATIONALLY REQUIRED:** Key-Platzierung 0600 unter `~/.config/argent`, service.env-Pfad-Referenz, Unit-Installation/Enable, Deploy-Restarts, Worktree-Pfad der Unit.
- **OBSERVED LIVE:** boot_id-Wechsel, Auto-Start 14:41:56, Singleton + Zweitinstanz-exit-3, Revision-Monotonie 557→598, Enforcer-Proben (RO/EROFS/absent/rw), Journal-Secret-Freiheit, Idle-Ressourcen, Linger-mtime 2026-08-20.

## 15. MARKER

- `ARGENT_PHASE_G3_WSL_RECOVERY_GREEN`
- `ARGENT_PHASE_G_GREEN`

(Beide nur, weil ALLE Exit-Kriterien des G3-B-Briefs §20 erfüllt und dokumentiert sind.)
