# PHASE G2 ACCEPTANCE — systemd User-Service Activation + Controlled Live Restart/Recovery Proof

**Branch:** `phase-g2-systemd-live-activation` (Base `6550013` = G1 GREEN).
**Datum:** 2026-09-03
**Owner-Autorisierung:** Explizit erteilt für G2-Aktivierungs-Scope (Brief 00:45).

SERVICE CREATED/VALIDATED + ACTIVATED/VALIDATED LIVE == **nicht** G3 (kein WSL-Reboot-Test).

---

## 0. Aktivierungs-Bilanz (Live, vom Supervisor ausgeführt)

| Aktion | Status |
|---|---|
| Protected runtime/config locations | YES — `~/.config/argent/` (0700), `~/.local/state|share/argent`, `~/.cache/argent` |
| Evidence-MAC-Key provisioniert | YES — `~/.config/argent/evidence_mac.key`, 0600, 44 Bytes, zufällig, **nie gedruckt/committet** |
| service.env | YES — `~/.config/argent/service.env` 0600, nur `ARGENT_EVIDENCE_MAC_KEY_FILE=`-Pfad-Referenz, KEIN Key-Wert |
| Unit installiert | YES — `~/.config/systemd/user/argent-supervisor.service` (aus G1-Template, Deployment-Werte aufgelöst, offline validiert rc=0, keine Secrets) |
| daemon-reload | YES (erfolgreich, Unit erkannt) |
| First live start | YES — PID 146124, Instance ACTIVE (revision 2) |
| enable | YES (`default.target.wants`-Symlink) |
| **service enabled** | **YES** |
| **service active** | **YES** (MainPID 146616 nach Crash-Test-Restart) |
| WSL reboot performed | NO |
| Windows startup modified | NO |
| login linger modified | NO |
| Unrelated systemd service modified | NO |
| push/PR/merge/deploy | NO |

## 1. Live-Proofs (bounded, deterministische Checks statt LLM-Polling)

| Proof | Evidenz |
|---|---|
| First start → READY | Service `active (running)`, Instance-Row ACTIVE, genau 1 Prozess (`argent_core.argent_service`), kein Crash-Loop |
| Singleton | DB: genau **1** ACTIVE `supervisor_instances`-Row über ALLE Tests hinweg |
| Second-Instance-Rejection | Zweitstart exit=3, Log: `fatal: another supervisor holds the lock`; DB unverändert 1 ACTIVE |
| Controlled restart | `systemctl --user restart` → neuer PID (146473), neue Instance-ID, revision 4→6, active |
| Graceful stop | `stop` → `inactive`, Instance-Status **RELEASED** (revision 7) persistiert — sauberes Ownership-Release |
| Start nach Stop | `start` → neue Instance (revision 9), active, persistenter State erhalten (argent.db blieb) |
| Crash-Style-Recovery | `systemctl --user kill -s SIGKILL` → systemd `Restart=on-failure` startete neu (PID 146616, revision 11), genau 1 ACTIVE, kein Duplikat |
| Terminal-Jobs unverändert | 0 Jobs im Store vor/nach allen Restarts; notification_outbox 0, supervisor_actions 0 |
| Notification-Dedup | 0-Row-Null-Job-Beobachtung beweist KEINE Dedup-Eigenschaft (nichts zu duplizieren). Externe Zustellung ist **at-least-once mit bounded dedup**: 1 Outbox-Row pro logischem Event, SENT sticky, Claim-Token-CAS + 30s-Lease, Crash-after-send-before-SENT kann identisches Payload erneut senden, ≤5-Versuche-Ceiling. Deterministischer Beweis: `tests/test_phase3a_delivery.py` (`test_crash_after_accept_before_sent_at_least_once`, `test_crash_after_accept_before_sent_idempotent`, `test_crash_after_sent_no_resend`, `test_repeated_lease_reclaim_never_exceeds_max_attempts`) und SPEC V3A §9.4/§11.5. |
| Idle-Resource | CPU ~0.5%, RSS ~33 MB, kein /tmp-Wachstum durch Service, keine Child-Prozesse |
| Journal-Secret-Scan | Key-Wert **nicht** im Journal; 0 Secret-Wert-Treffer (api_key=/password=/token=) |
| Kind-Env-Grenze | `agent_spawn_env()` strippt `ARGENT_EVIDENCE_MAC_KEY` + `ARGENT_EVIDENCE_MAC_KEY_FILE` (Tests + Live-Code-Pfad) |
| Restart-Loop-Safety | `Restart=on-failure`, `RestartSec=5`, `StartLimitBurst=5`/`StartLimitIntervalSec=120`; NRestarts=1 (kein Loop) |
| Kein Public-Listener | Service bindet nichts; Gateway weiter loopback-only (127.0.0.1:18789) |
| R1 ungültige Config | `{"idle_sleep_seconds": NaN, "unknown_field": 1}` → exit 2 `fatal: unknown service config keys` (fail-closed) |
| R2 fehlender Key | Ohne `ARGENT_EVIDENCE_MAC_KEY(_FILE)` → exit 2 (fail-closed, kein PASS-Signing) |

## 2. Deterministische G2-Tests (Writer, unabhängig vom Supervisor ausgeführt)

| Datei | Tests | Inhalt |
|---|---|---|
| `tests/test_phase_g2_config.py` | 21 | Config-Loading, service.env-Vertrag, Store-Pfad-Invariante (state_dir, nicht /tmp/Worktree) |
| `tests/test_phase_g2_unit_static.py` | 18 | Unit-Statik: kein Secret-Literal, kein User=root, Rate-Limiting-Restart (StartLimitBurst=5/120s), SIGTERM, NoNewPrivileges, ExecStart ohne Shell, State/CacheDirectory, StateDirectoryMode/UMask-Härtung, Installed-Unit-Substitutions-Normalisierung |
| `tests/test_phase_g2_agent_env.py` | 7 | Spawn-Env-Geheimnisgrenze (Key + Key-Dateipfad strippen) |
| `tests/test_phase_g2_singleton_deterministic.py` | 8 | revision-CAS/Single-Active-Deterministik (kein Split-Brain) |
| `tests/test_phase_g2_sandbox.py` | 6 | bwrap-Agent-Sandbox: argv (ro-root + tmpfs-Masken), adversarielle Real-Spawn-Probe (Key/DB unlesbar), fail-closed ohne bwrap, Short-Timeout-Kill |
| `tests/test_phase_g2_prompts.py` | 8 | Prompt-File-Lifecycle: deterministischer Unlink auf CONSUME/FAILED/Enforcement-Fehler, Sweep-Alter/Count-Floor |
| `g2-systemd/install-check.sh` | — | read-only Validator (keine Aktivierung); Template + INSTALLED Unit (FragmentPath); gegen echte Installation ausgeführt: **OK** |
| `docs/PHASE_G2_NOTES.md` | — | Deployment-Architektur, Agent-Sandbox (F1), deterministisch vs. Live |

**G2 total: 68 PASS** · Full Suite: **2538 PASS** (~40 s) · G1 78 · F 223 · E 208 · D 243 · C 296 · B 166 — alle unabhängig vom Supervisor ausgeführt.

## 3. Trust-Boundary (G2)

- **CODE-ENFORCED / DETERMINISTISCH GETESTET:** Config fail-closed (unknown fields/NaN/Pfad-/tmp-Verbot), Unit-Statik (keine Secrets, kein Root, Rate-Limiting-Restart), **bwrap-Agent-Sandbox (read-only root + tmpfs-Masken über `~/.config/argent`/`~/.local/state/argent` — Key/DB für den Agenten unlesbar)**, agent_spawn_env-Key-Stripping, revision-CAS-Singleton, Store-Pfad-Invariante, Prompt-File-Lifecycle (deterministischer Unlink + bounded Sweep).
- **OPERATIONALLY REQUIRED (G2 ausgeführt):** Key-Datei-Platzierung unter `~/.config/argent` (0600, außerhalb Agent-Write-Area), service.env nur Pfad-Referenz, Unit-Installation + daemon-reload/enable/start, Evidence-Store unter `~/.local/state/argent`.
- **Live-bewiesen (Supervisor):** Singleton, Second-Instance-Rejection, Controlled Restart, Graceful Stop/Start, Crash-Recovery, Journal-Secret-Freiheit, Idle-Resource.

## 4. Bekannte Limitationen / G3-Deferrals

- **Kein WSL-Reboot-Test** (G3): automatische Wiederherstellung nach kompletter WSL-Distro-Terminierung ist NICHT bewiesen — Service läuft, solange der User-systemd/WSL läuft.
- Crash-Test wurde sicher als systemd-kontrollierter SIGKILL ausgeführt (keine Jobs aktiv, kein User-Work gefährdet).
- `_resolve_mac_key` akzeptiert jeden Operator-Key-Pfad; `/tmp`-Residenz der Key-Datei ist operativ verhindert (Provisionierung unter `~/.config/argent`), nicht zusätzlich code-erzwungen.
- Live-Evidence wurde mit echten systemd-Aufrufen (Owner-autorisiert) erzeugt; deterministische Tests fassen die Live-DB nicht an.

## 5. Verifikation

- G2 targeted 68 PASS · G1 78 · F 223 · E 208 · D 243 · C 296 · B 166 · Full Suite **2538 PASS** (~40 s)
- Sol Live-Ops-Closing-Review: siehe Abschnitt 6 (nach Review ausgefüllt)
- Kein Commit bis alle Exit-Kriterien grün; kein Push.
