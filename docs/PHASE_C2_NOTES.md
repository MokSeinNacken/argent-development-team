# PHASE C2 — Bounded Execution Scopes + Enforcement

Phase C2 implementiert den **Execution-Enforcement**-Teil des Resource Governors
(ARGENT ARCHITECTURE V1 FINAL §9): die C1-Admission (Entscheidungsbasis) wird um
die **Erzwingung** ergänzt — jeder ressourcenrelevante Prozess läuft in einem
transienten, unprivilegierten `systemd-run --user --scope` (cgroup v2) mit den
von der Admission berechneten Ceilings. Kein Scope ⇒ kein Start (fail-closed).

Status: **Nur Implementierung + Tests + Doku. Kein Commit, kein Push, kein
Merge. Keine systemweite Konfiguration, kein Background-Service, kein Root.**

---

## 1. Analyse-Antworten A–E (Supervisor, read-only)

* **A — Ein Spawn-Pfad:** `Supervisor._perform_spawn_run`
  (`argent_core/supervisor.py`) → `OpenClawRunLauncher.spawn`
  (`subprocess.Popen(start_new_session=True)`), nach `_recheck_lease_fence`.
  Das ist die Zentralisierungsstelle.  Der bwrap-`SandboxRunner` (Tests,
  read-only) wurde NICHT angefasst.  Der Scheduler behält seine C1-Gates
  (claim-time + spawn-adjacent preflight in `run_pass`).
* **B — Launcher-Erweiterung:** Ja.  Der Enforcement-Pfad startet den
  openclaw-Prozess **innerhalb** eines `systemd-run --user --scope`-Scopes.
  Der Scope entsteht VOR Prozessstart (`systemd-run --scope` startet den Befehl
  im Scope); kein ressourcenrelevanter Prozess läuft ohne Scope.
* **C — systemd/cgroup:** voll unprivilegiert verfügbar (siehe Messdaten §2).
* **D — Evidence nach Scope-Erstellung:** `ControlGroup`-Pfad, `memory.max`/
  `memory.high`/`memory.swap.max`, `cpu.max`, `pids.max`, `/proc/<pid>/cgroup`
  (Prozess→Scope-Zuordnung), `ProcessIdentityProvider` (boot_id, pid,
  process_start_ticks).  Alles rücklesbar.  (Abweichung: `MainPID` ist bei
  `--scope`-Units NICHT gesetzt — siehe §7.)
* **E — Enforcement nicht verfügbar/nicht beweisbar:** MEDIUM/HEAVY/EXCLUSIVE
  **und auch LIGHT** fail-closed ⇒ kein Start, Ergebnis
  `RESOURCE_ENFORCEMENT_UNAVAILABLE` (`error_class=RESOURCE`, KEIN
  `CODE_FAILURE`).  Es gibt keinen stillen unbounded Fallback.

---

## 2. Execution-Technik (live verifiziert, read-only)

Verwendet: **`systemd-run --user --scope` + cgroup-v2-Readback** (kein Root,
kein sudo, nur transiente Scopes).

Messdaten (Host, 2026-09-01, systemd 259 user session, WSL, cgroup v2):

```text
systemd-run --user --scope --unit=argent-c2-probe-<pid> \
    --property=MemoryMax=67108864 --property=MemoryHigh=33554432 \
    --property=MemorySwapMax=16777216 --property=CPUQuota=100% \
    --property=TasksMax=64 sleep 2
→ "Running as unit: ...scope", EXIT 0, danach 0 geladene Units.
```

Rücklesung während des Laufs (identisch bestätigt):

```text
systemctl --user show <unit>.scope -p ControlGroup -p MemoryMax -p MemoryHigh \
    -p MemorySwapMax -p CPUQuotaPerSecUSec -p TasksMax -p ActiveState
ControlGroup=/user.slice/user-1000.slice/user@1000.service/app.slice/argent-c2-probe12.scope
MemoryMax=67108864  MemoryHigh=33554432  MemorySwapMax=16777216
CPUQuotaPerSecUSec=1s   TasksMax=64   ActiveState=active

/sys/fs/cgroup/<ControlGroup>/memory.max       → 67108864
/sys/fs/cgroup/<ControlGroup>/memory.high      → 33554432
/sys/fs/cgroup/<ControlGroup>/memory.swap.max  → 16777216
/sys/fs/cgroup/<ControlGroup>/cpu.max          → "100000 100000"   (100%)
/sys/fs/cgroup/<ControlGroup>/pids.max         → 64
```

**Wichtige Erkenntnis (bestimmt die PID-Bindung):** `systemd-run --scope`
**exec't** den Befehl.  Das `Popen.pid` des `systemd-run`-Subprozesses IST der
Scope-Befehlsprozess; `/proc/<pid>/cgroup` zeigt den Scope-cgroup-Pfad und
`/proc/<pid>/stat` Feld 22 liefert die start-ticks.  `systemd-run --scope`
blockiert bis zum Befehlsende und systemd räumt den transienten Scope danach
automatisch ab.

---

## 3. Property-Übersetzungstabelle

`translate_limits_to_properties(effective_limits)` (rein, deterministisch):

| effective-limit (Byte/Prozent) | systemd-Property | Beispiel |
|---|---|---|
| `memory_high_bytes` | `MemoryHigh` | `33554432` |
| `memory_max_bytes` | `MemoryMax` | `67108864` |
| `swap_max_bytes` | `MemorySwapMax` | `16777216` |
| `cpu_quota_percent` | `CPUQuota` | `300%` |
| `tasks_max` (konstant) | `TasksMax` | `64` |
| `timeout_seconds` | **NICHT** systemd | → externer `timeout`-Wrapper |

`timeout` wird bewusst NICHT auf `TimeoutStopSec` abgebildet: `TimeoutStopSec`
ist eine *Stop*-Timeout, das step-timeout ist *Wallclock*.

**Validation (`validate_effective_limits`, fail-closed):** jedes Limit ist ein
strikt positiver endlicher `int` (kein `None`/negativ/`inf`/`NaN`/`bool`);
`MemoryHigh <= MemoryMax <= Klassendecke`; `SwapMax <= Klassendecke`;
`CPUQuota <= Klassendecke`; `timeout > 0` und `<=` größtes Klassen-Timeout
(HEAVY = 120 min = 7200 s).  EXCLUSIVE hat `timeout=None` (step-spezifisch) und
wird in C2 fail-closed abgewiesen (kein per-step-timeout vorhanden).

---

## 4. Scope-Naming-Regeln

* Lokal generiert, nie aus Agent-Text: `argent-c2-<shortjob>-<shortdispatch>-<randomhex>`.
* Zeichensatz **nur** `[a-z0-9-]`, keine führenden/abschließenden `-`,
  Länge `<= 64`.
* `generate_scope_name()` reduziert lokale job-/dispatch-IDs defensiv auf
  `[a-z0-9]`-Chunks und hängt `secrets.token_hex(4)` an (eindeutig pro
  Spawn-Versuch — ein Retry kollidiert nie mit einem noch aufräumenden Scope).
* `is_valid_scope_name()` lehnt Injection-Versuche ab
  (`;`, `|`, `$(...)`, Leerzeichen, `/`, Großbuchstaben, `.service`-Suffixe).
* `sanitize_scope_name()` ist ein fail-closed-Reduzierer, nie eine Autorität.

---

## 5. Timeout-Modell

* **Wallclock** step-timeout ⇒ externer `timeout`-Wrapper um das Kind:
  `timeout -k <grace> <seconds> <command...>` (SIGTERM, dann nach `<grace>` s
  SIGKILL; `TIMEOUT_KILL_AFTER_SECONDS=10`).
* Der Timeout-Wert ist der Policy-Wert (`effective_limits["timeout_seconds"]`);
  der Agent kann ihn weder setzen noch entfernen.
* **Kein** automatischer Retry mit längerem Timeout.  (Test
  `test_no_automatic_longer_retry` erzwingt den exakt gleichen Wert.)
* `timeout` gilt zusätzlich zum bestehenden Agent-internen `--timeout 900`
  (`AGENT_TIMEOUT_SECONDS`), der unverändert bleibt (Agent-eigener, konservativer
  Limit — separater Mechanismus).

---

## 6. Restart-/Cleanup-Modell

* **Scope-Lebensdauer = Prozess-Lebensdauer.**  `systemd-run --scope` blockiert
  bis zum Befehlsende; systemd räumt den transienten Scope automatisch ab
  (0 geladene Units danach).  Kein expliziter Cleanup nötig.
* `cleanup_scope()` / `terminate_scope()` sind **nur** Best-Effort-Sicherheitsnetze
  (`systemctl --user stop` / `kill --signal=SIGKILL`), die ausschließlich den
  eigenen, lokal generierten Unit-Namen anfassen (`is_valid_scope_name`-Guard).
* Ein verifizierter Scope, dessen Verifikation fehlschlägt, wird vor dem
  fail-closed-Ergebnis best-effort gestoppt (kein verwaister Scope).
* **Restart:** Der Prozessidentitäts-Verdikt bleibt unverändert (B4): gleicher
  `(boot_id, pid, start_ticks)` = lebend; `boot_id`-Wechsel = alter Scope-Ref nur
  historische Evidence; PID-Reuse = nicht derselbe Prozess.  Ein alter
  `scope_ref` autorisiert nie einen neuen Spawn.

---

## 7. OOM-/Memory-Evidence-Strategie

* **Kein** `dmesg`-Parsing (untrusted/noisy).  Stattdessen cgroup
  `memory.events` im Scope-Cgroup lesen (bounded Deltas):
  * `oom_kill` delta > 0 → `OOM_KILL`
  * `max` delta > 0 → `MEMORY_LIMIT` (memory.max erreicht)
  * `high` delta > 0 → `MEMORY_LIMIT` (Soft-Limit-Druck)
* Persistiert als bounded JSON (`scope_events`) in der Registry; zusätzlich
  `timed_out` (0/1) und `termination_class` (geschlossenes Enum).
* C2 liefert **nur** bounded Evidence + `classify_termination()` (rein);
  die endgültige Job-Klassifikation macht C3.

**Abweichung vom Analyse-Entwurf D (dokumentiert):** `MainPID` ist bei
`systemd-run --scope`-Units **nicht** gesetzt (systemd pflegt `MainPID` nur für
Service-Units).  Die Prozess→Scope-Bindung wird deshalb über `/proc/<pid>/cgroup`
(und `cgroup.procs`) bewiesen; `process_id` = `Popen.pid` (der von
`systemd-run` exec'te Befehlsprozess, der exakt so lange lebt wie der Scope).

---

## 8. Modul-Übersicht

| Datei | Inhalt |
|---|---|
| `argent_core/execution_scope.py` (NEU) | `ExecutionScope`, `ScopeNaming`/`sanitize_scope_name`/`is_valid_scope_name`/`generate_scope_name`, `validate_effective_limits`, `translate_limits_to_properties`, `ExecutionScopeBackend` (Protocol), `SystemdRunScopeBackend` |
| `argent_core/scope_enforcer.py` (NEU) | `EnforcementStatus`, `EnforcementResult`, `TimeoutRunner`, `ExecutionEnforcer.enforce_and_spawn` |
| `argent_core/resource_failure.py` (NEU) | `TerminationClass`, `classify_termination` |
| `argent_core/resource_governor.py` (GEÄNDERT) | + `ResourceReasonCode.RESOURCE_ENFORCEMENT_UNAVAILABLE` |
| `argent_core/process_registry.py` (GEÄNDERT) | `register(...)` additiv: `scope_ref`, `resource_class`, `policy_version`, `effective_limits`, `termination_class`, `timed_out`, `scope_events` |
| `argent_core/store.py` (GEÄNDERT) | `SCHEMA_VERSION` 9→10; process_registry +7 additive Spalten + Migration; `_PROCESS_REGISTRY_COLUMNS` |
| `argent_core/supervisor.py` (GEÄNDERT) | `__init__` +`enforcer`/`scope_backend`; `_perform_spawn_run` Enforcement-Pfad (`_fresh_admission`, F1.3); `_default_active_jobs_reader`; `_spawn_scoped`; `_register_process_evidence` +scope; `_perform_run_sandbox_tests`/`_run_sandbox_scoped` (F3); `build_agent_command`; `OpenClawRunLauncher.increment_counter` |
| `argent_core/scheduler.py` (GEÄNDERT) | Enforcement-Wiring (`enforcer`/`scope_backend`), `_enforcement_failed_job` (DEFER-Pfad), `_enforcement_lost_job` (LOST-Quarantäne) |
| `docs/PHASE_C2_NOTES.md` (NEU) | diese Datei |

**`cgroup_ref` vs `scope_ref` (Entscheidung, dokumentiert):** `cgroup_ref`
(B3-Platzhalter) speichert ab C2 den cgroup-**Pfad**; `scope_ref` den
systemd-**Unit-/Scope-Namen**.  Zwei verschiedene Zwecke, keine Duplikation.

**FakeScopeBackend (Entscheidung):** liegt in `tests/c2_helpers.py`, nicht im
Produktmodul — Test-Doubles gehören nicht in `argent_core`.

---

## 9. Explizit NICHT implementiert (C3+)

* C3 Retry-/Routing-Policy und die endgültige Job-Klassifikation (C2 liefert nur
  bounded Evidence + `classify_termination`).
* Externe CI-Aktionen / `PREFER_EXTERNAL`-Ausführung.
* Kein Background-Service (Phase G), keine systemweiten Änderungen, kein Root.
* Kein `dmesg`-Parsing, keine großen Allokationen/Lasttests.

---

## 10. Tests

`tests/test_phase_c2_*.py` (112 Tests) + `tests/c2_helpers.py`:

* `test_phase_c2_limits.py` — Limit-Validation (fail-closed).
* `test_phase_c2_scope.py` — Naming + Scope-Erstellung.
* `test_phase_c2_verify.py` — Verification (reale `SystemdRunScopeBackend`-Logik
  mit injizierten Readern).
* `test_phase_c2_spawn_gate.py` — Spawn-Gate (Scheduler-Integration, Fake-Backend).
* `test_phase_c2_registry.py` — Registry-Bindung + Restart.
* `test_phase_c2_timeout.py` — Timeout-Modell.
* `test_phase_c2_evidence.py` — Resource-Limit-Evidence-Klassifikation.
* `test_phase_c2_security.py` — kein `shell=True`, keine Agent-bestimmten Scopes/Limits.
* `test_phase_c2_real_scope_smoke.py` — echter, `skipif`-bewachter Smoke-Test.
* `test_phase_c2_fix_round.py` — Fix-Round-Regressionstests (F1–F5, siehe §11).

Regression: C1 (82) + B1–B4 (166) + Gesamtsuite (1616 = 1504 + 112) grün.

**Konsequenz des SCHEMA_VERSION-Bumps (dokumentiert):** drei C1-Tests
(`test_phase_c1_restart.py`) und ein Phase-3C-Test
(`test_phase3c_approval_core.py::test_schema_version_is_9`) hatten die Version
hart auf `"9"` fixiert und wurden auf `"10"` bzw. versionsagnostisch
(`SCHEMA_VERSION`-Konstante) aktualisiert — reine Versions-Anpassung, keine
Testabschwächung.

---

## 11. Fix-Round (nach Sol-Closing-Review, REJECT → F1–F5)

Der Sol-Closing-Review hat den C2-Candidate mit **REJECT** bewertet (Findings
F1–F5).  Ein erster Fix-Round-Lauf ist an einem LLM-Timeout gescheitert und hat
den Worktree inkonsistent hinterlassen (Produktcode teils gefixt, Fake-Backend
und Tests nicht nachgezogen).  Diese Fix-Round hat die API konsistent gemacht
und alle Findings adversariell abgesichert.

### Was geändert wurde

* **F1 — kein Spawn ohne Enforcer.**  `_perform_spawn_run` erzwingt einen FRESHEN
  C1-Admission-Check am Enforcement-Punkt (`_fresh_admission`, F1.3) und einen
  MANDATORY-Enforcer (F1.2): `enforcer=None` → `resource_enforcement_failed` /
  `RESOURCE_ENFORCEMENT_UNAVAILABLE`, **kein** `launcher.spawn`-Fallback.
  Default-Wiring erzeugt einen echten
  `ExecutionEnforcer(SystemdRunScopeBackend())` (Supervisor + Scheduler).  Der
  Scheduler requeued `resource_enforcement_failed` als QUEUED/`RESOURCE`
  (DEFER-Pfad, kein CODE_FAILURE).

* **F2 — Start-Barriere.**  Der Scope wird mit einem harmlosen **Placeholder**
  (`sleep 600`, bounded — kein `sleep infinity`) erstellt und verifiziert, BEVOR
  der Agent startet; danach wird der Agent in den verifizierten Scope-cgroup
  verschoben (`cgroup.procs`), die Bindung exakt bewiesen und der Placeholder
  beendet.  Ein Verifikationsfehler NACH Prozessstart terminiert + beweist
  Inaktivität (kein Doppel-Agent).  Ist Cleanup **nicht** beweisbar →
  `SCOPE_CLEANUP_UNVERIFIED` → `_enforcement_lost_job` → **LOST**-Quarantäne
  (nie 300s-DEFER-Requeue).

* **F3 — Sandbox-Testpfad.**  Produktion: `RUN_SANDBOX_TESTS` läuft durch
  denselben Pfad — frische C1-Admission (`_fresh_admission`), dann bwrap INNERHALB
  eines bounded Scope (`_run_sandbox_scoped` → `ExecutionEnforcer.enforce_and_run`),
  inkl. Registry-Bindung + terminal Evidence.  Der injizierte `run_tests_fn`-Seam
  ist ein **Test-only**-Ersatz (nie in Produktion gesetzt) — dokumentiert+getestet
  als bewusst gated (die B-/E2E-Tests bleiben deterministisch).

* **F4 — verify_scope fail-closed.**  Fehlendes `ControlGroup` → Fehler (KEIN
  Fallback auf den alten Wert); `CPUQuotaPerSecUSec`/`TasksMax`/`pids.max` werden
  tatsächlich verglichen; abweichende Property → `SCOPE_VERIFICATION_FAILED`.

* **F5 — bounded Evidence.**  `_bounded_json` begrenzt `effective_limits`/
  `scope_events` auf <= 4 KB (fail-closed, nie ein Dump); `termination_class` ist
  ein geschlossenes DB-CHECK-Enum (kein fremder Write);
  `mark_process_terminal_with_evidence` persistiert `timed_out` (0/1) +
  `termination_class` + `scope_events`.

### Konsistenz-Fixes (abgebrochener Fix-Round hinterlassen)

* `tests/c2_helpers.py` — `FakeScopeBackend` an die neue Start-Barrier-API
  angepasst (`create_scope(scope=, placeholder_command=, properties=)`,
  `start_in_scope`, `verify_process_binding`, `stop_placeholder`,
  `prove_inactive`, `read_memory_events`, `run_in_scope`) + `started`-Record.
  Name-Kollision `prove_inactive` (bool-Flag vs. Protokoll-Methode) behoben.
* `tests/test_phase_c2_scope.py` / `test_phase_c2_timeout.py` — Assertions auf
  die Start-Barrier-Semantik umgestellt (Placeholder in `created`, gewrapptes
  Kommando in `started`).
* `tests/test_phase_c2_real_scope_smoke.py` — an die Start-Barrier-API angepasst
  (Placeholder + `start_in_scope` + Bindung + Placeholder-Stop); Limits unverändert
  klein/sicher (64/32/16 MiB, `sleep 3`, harter Timeout).
* `tests/test_phase2c_supervisor.py` — `make_env` injiziert jetzt deterministisch
  einen Fake-Enforcer + Fake-Governor/Snapshot-Provider (Spawn läuft ab C2 über
  den Enforcer, nicht mehr den Legacy-Launcher); `test_spawn_plan_then_launch_once`
  prüft Scope-Erstellung statt `launcher.spawns`.  Verhindert echte
  `systemd-run`-/`openclaw`-Seiteneffekte in Offline-Tests.
* `tests/test_phase_b4_soak.py` — `_build_env` injiziert ebenfalls einen
  deterministischen Fake-Enforcer + Fake-Governor/Snapshot-Provider (vorher
  erreichte der Soak `SPAWN_RUN` mit dem echten Enforcer → echte
  `systemd-run`-Scopes/`openclaw`-Spwans, was die Host-Last-bedingten B2-Flakes
  in der Gesamtsuite verursachte).
* Neu: `tests/test_phase_c2_fix_round.py` (17 adversarielle Regressionstests,
  F1–F5, analog zu `test_phase_c1_fix_round.py`).

### Testzahlen (nach Fix-Round)

* `tests/test_phase_c2_*.py`: 112 (95 + 17 Fix-Round)
* `tests/test_phase_c1_*.py`: 82
* B1–B4: 166
* Gesamtsuite (`--ignore=e2e-fixture`): 1616 (1599 + 17)
