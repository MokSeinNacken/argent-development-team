# PHASE G ACCEPTANCE — Background Runtime → systemd Live → REAL WSL Restart Recovery

**Branch-Kette:** `phase-g1-background-runtime-recovery` (6550013) → `phase-g2-systemd-live-activation` (54a1bfb) → `phase-g3-wsl-restart-acceptance` (Base 54a1bfb, Closing-Commit s. u.).
**Datum:** 2026-09-03 (G1 00:40, G2 13:00, G3-A 14:38, G3-B 15:10)
**Owner-Autorisierung:** explizit pro Phase/Scope (G1-Code-only, G2-Activation, G3-A-Pre-Reboot, G3-B-Post-Reboot + Deployment im autorisierten Phase-G-Service-Scope).

**VERDICT: PHASE G GREEN — `ARGENT_PHASE_G3_WSL_RECOVERY_GREEN` + `ARGENT_PHASE_G_GREEN`.**
Kein Push. EIN lokaler Closing-Commit. Worktree sauber.

---

## Was Phase G bewiesen hat

| # | Invariante | Beweis |
|---|---|---|
| 1 | Realer WSL-`--shutdown`/Restart | boot_id `17b1e3d1…` → `28e93f86-8c93-43ed-bc88-a083851b5703`; beide Boots im Journal; uptime 3 min; neuer User-Manager systemd[231] |
| 2 | Pre-Reboot-Checkpoint validiert | `~/.local/state/argent/g3/g3-pre-reboot-checkpoint.json` (v1.0), Validator exit 0, überlebte den Reboot |
| 3 | Persistenter State überlebt | argent.db (SCHEMA 18, quick_check ok), Key 0600, service.env 0600, Unit, Checkpoint — alle auf kanonischen Pfaden, kein /tmp |
| 4 | Alte Prozess-Autorität invalidiert | alte (boot `17b1e3d1…`, pid 216366, ticks 10933666, instance bb3d4ad9…) nicht mehr live; PID-Reuse unmöglich (neue Identität vollständig anders) |
| 5 | Genau EIN neuer Supervisor | `instance:bb1014eb…` pid 7096 rev 598 ACTIVE; 1 ACTIVE-Row; Zweitinstanz exit 3 „another supervisor holds the lock" |
| 6 | Auto-Start bewiesen | enabled Unit startete im neuen Boot automatisch (Journal `-- Boot 28e93f86…` → `Started` 14:41:56 durch systemd[231]; NRestarts=0; kein manueller Start vor Beweis) |
| 7 | Health READY | ACTIVE + frischer Heartbeat + genau 1 Prozess + kein error_code (Semantik dokumentiert; health.json-Deferral) |
| 8 | Startup-Reconciliation korrekt | Pflicht-Reconciliation nach Instance-Acquire (fail-closed); Revisionen monoton 557→568→572→598; kein Reopen |
| 9 | Terminale States immutabel | Store pre/post leer (0 Jobs); State-Machine-Deterministik (Tests); kein fabrizierter DONE/PASS |
| 10 | Keine Duplikat-Action/-Wake/-Outbox durch Reboot | supervisor_actions 0, external_waits 0, notification_outbox 0 (keine neue Row); Notification: lokale Dedup + extern at-least-once |
| 11 | WAITING_EXTERNAL konservativ | 0 Rows; Semantik deterministisch getestet; konservatives 8-State-Modell erhalten |
| 12 | Evidence-MAC-Key überlebt sicher | 0600, 44 B, mtime unverändert; Kind-Env strippt Key + Key-Pfad (Allowlist); Sandbox maskiert Key/DB (absent, Live-Probe) |
| 13 | Key-/Store-Permissions korrekt | 0600/0700 durchgehend (auch post-reboot/post-deploy) |
| 14 | bwrap bleibt Pflicht | Sandbox-Wrap Default True; Fail-closed ohne bwrap/ohne gültige Agent-ID/bei Bind-Fehler; kein Unwrapped-Fallback (Produktions-Dispatch nur Enforcer→bwrap) |
| 15 | Argent Key/State vor Agenten verborgen | tmpfs-Masken über `~/.config/argent` + `~/.local/state/argent`; Live-Probe: ABSENT |
| 16 | Autorisierende OpenClaw-Config vor Agent-Writes geschützt | G3-Narrowing deployed: `~/.openclaw` nicht mehr ganz rw; openclaw.json/.env/credentials/identity/exec-approvals.json VISIBLE+RO (EROFS-Denials live); nur `agents/<id>` + `workspace/<id>` rw |
| 17 | Writable OpenClaw-Runtime minimiert | Per-Agent-Runtime-Dirs + EXPLIZIT autorisierter Job-Worktree; KEIN impliziter Service-cwd-Bind (Sol-HIGH #12 fix-round: ohne Workdir cwd="/", Service-Codebaum EROFS — Live-Probe) |
| 18 | Resource Governor bindend | systemd-run--scope-Start-Barrier, Cgroup-Verifikation/Binding, Fresh-Admission via Governor; nach Reboot + Deploys funktional |
| 19 | Idle-Supervisor leicht | 1 Task, 0 Running-Children, RSS ~33 MB, CPU idle, kein Listener, kein /tmp-Wachstum, kein Restart-Loop (NRestarts=0 je Deploy) |
| 20 | Kein Secret-Leak | Journal-Scans 0 Treffer; Checkpoint ohne Secrets (Secret-Key-Name-Denylist); Key-Wert nie gelesen/gedruckt/committet |
| 21 | Linger-/Windows-Grenze | Linger=yes vorbestehend (mtime 2026-08-20 19:24), von G2/G3 unverändert; kein Windows-Startup; G3-claim = Distro-Start→User-Systemd→Service, nicht Windows-Boot→WSL |
| 22 | Unit/Live-Deployment konsistent | installed Unit == Template + 3 Deployment-Substitutionen (G3-Worktree; install-check OK); Hash-Änderung `40b408fa…`→`868f1192…` dokumentiert + verstanden |
| 23 | 0 ungelöste HIGH/CRITICAL | Sol-Closing-Review (1×): kein CRITICAL; 3 HIGH alle geschlossen (#12 Fix+Live-Probe, #18 Docs, #20 mitigiert+dokumentiert) |

## Phasen-Bausteine

- **G1 (6550013):** Background-Runtime-Kernel (Singleton mit boot_id+pid+ticks+host_id-Fencing, revision-CAS, 8-State-Modell, Health-Status, Allowlist-Env, kanonische XDG-Pfade, KEINE Aktivierung). 78 G1-Tests.
- **G2 (54a1bfb):** systemd-User-Service erstellt/aktiviert/verifiziert (enabled+active+READY, Singleton, Controlled Restart, SIGKILL→Recovery, Journal-Secret-Freiheit, bwrap-Sandbox F1 an der Agent-Spawn-Grenze: Key/DB maskiert). 69 G2-Tests.
- **G3-A (14:38):** Pre-Reboot-Checkpoint, Job-Safety-Gate (Store leer), `~/.openclaw`-RW-Befund live bestätigt + Schließung im G3-Code (Per-Agent-Runtime rw, Config/Credentials RO), GATE — READY_FOR_WSL_RESTART.
- **G3-B (15:10):** Real-Reboot-Probe, Auto-Start, Recovery-Invarianten, G3-Deploy (+Fix-Round: expliziter Workdir statt implizitem Service-cwd), Produktions-Enforcer-Live-Proben, Full Suite 2568, Sol-Closing-Review, Docs, Closing-Commit.

## Sol-Closing-Review (1×, read-only)

Siehe docs/PHASE_G3_ACCEPTANCE.md §11: kein CRITICAL; 3 HIGH (cwd-Bind, Doc-Konsistenz, Deployment-Mutability) — alle geschlossen mit deterministischen + Live-Beweisen; Rest LOW/INFO dokumentiert.

## Tests (Supervisor-eigene Läufe, G3-Worktree)

G3 29 · G2 69 · G1 78 · F 223 · E 208 · D 243 · C 296 · B 166 · Übrige 1256 · **FULL SUITE 2568 passed (38.95 s)**. Validator: exit 0.

## Verifikations-Distinktion

- **CODE-ENFORCED:** Fencing/Singleton, Reconciliation, bwrap-Pflicht + Narrowing (kein impliziter cwd), Env-Allowlist, Cgroup-Verifikation, Durable-Path-Rejection, Checkpoint-Fail-closed.
- **OPERATIONALLY REQUIRED:** Key/service.env-Platzierung + Modi, Unit-Installation/Enable, Deployment-Substitutionen, Worktree-Pfad der Unit.
- **OBSERVED LIVE:** boot_id-Wechsel, Auto-Start, Singleton/Zweitinstanz-exit-3, Revision-Monotonie, Enforcer-Proben (RO/EROFS/absent/rw), Journal-Scan, Idle-Ressourcen, Linger-mtime.

## Limitationen / Restrisiken (kein GREEN-Hindernis)

health.json nicht verdrahtet · `/tmp`-Key-Residenz teils operational · Exited-Agent-Zombies bis Prozess-Ende (bounded) · Unit-WorkingDirectory hängt am committed G3-Worktree-Pfad (Operator-Risiko bei Worktree-Löschung; Modell wie G2) · kein echter External-Wait-Adapter.

## Commit

EIN lokaler Phase-G-Closing-Commit auf `phase-g3-wsl-restart-acceptance` (Base 54a1bfb): s. Commit-Kopf dieses Dokuments/`git log`. Kein Push. Worktree sauber.

## Markers

`ARGENT_PHASE_G3_WSL_RECOVERY_GREEN` · `ARGENT_PHASE_G_GREEN`

## Next

**Phase H — Telegram Owner Approval B2B** (nicht automatisch begonnen). Separater Hinweis: Upstream OpenClaw PR #133267 bleibt als eigenständiger Blocker/Parallelstrang offen (WAITING_FOR_CI), unabhängig von Phase G. Keine automatische Upstream-Arbeit.
