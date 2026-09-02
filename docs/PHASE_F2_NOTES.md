# PHASE F2 NOTES — Staged Test Execution + Safe Evidence Reuse

**Branch:** `phase-f2-staged-test-execution` (Base `100a2b6` = F1 GREEN).
**Datum:** 2026-09-02

F2 konsumiert den F1-Planner direkt und baut **keine zweite Test-Selektions-Autorität**.
`execute_plan(plan, runner, *, snapshot, resource_gate=None, store=None)` führt genau die
im F1-`TestPlan` vorhandenen Stufen in Reihenfolge aus.

## 1. Executor-Architektur

- `argent_core/test_execution.py` — ein Modul, reine Execution-/Evidence-Logik.
- `SelectorRunner` (Protocol) — injizierbarer Runner. `PytestRunner` ist die reale
  Implementierung: `python -m pytest <resolved...> -q --tb=line` als fixe argv-Liste,
  **nie** `shell=True`, keine freien Shell-Strings. Globs werden **controllerseitig**
  deterministisch aufgelöst (sortiert, gegen das reale Dateisystem, erlaubte Wurzeln),
  Zero-Match ist fail-closed `TEST_INFRA_FAILURE`. Der Runner bindet einen kanonischen
  Projekt-Root und führt mit `cwd=root` aus (F5).
- `ResourceGate` (Protocol) — bindet Phase C. **Kein** stiller Default; ohne Gate wird
  fail-closed `BLOCKED`/`RESOURCE_FAILURE` geliefert (F6). Vor jeder Stufe wird
  Admission eingeholt; eine Ablehnung BLOCKIERT die Stufe und stoppt spätere Stufen.

## 2. Stage-State-Machine

Bounded Enum `StageState`: `PENDING | RUNNING | PASSED | FAILED | BLOCKED | SKIPPED`.
`SKIPPED` = „in diesem Versuch nicht ausgeführt" (z. B. frühere Stufe fehlgeschlagen).
Terminales `Verdict`: `DONE | FAILED | BLOCKED`.

## 3. Early-Failure-Semantik

- `TEST_FAILURE` → Stufe `FAILED`, alle späteren Stufen `SKIPPED` (teure spätere Läufe
  werden für diesen kaputten Snapshot vermieden), Verdict `FAILED`.
- Ein früher **PASS** entfernt niemals später im Plan markierte Pflicht-Stufen
  (Full Suite bleibt Pflicht, wenn der Plan sie verlangt).

## 4. Result-Klassifikation

`ResultClass`: `TEST_PASS | TEST_FAILURE | TEST_INFRA_FAILURE | RESOURCE_FAILURE |
PROCESS_FAILURE | TIMEOUT | CANCELLED_BLOCKED | UNKNOWN`.

Trennungen (Invarianten):
- `RESOURCE_FAILURE != TEST_FAILURE` (kein Code-Fehler, keine Modell-Eskalation)
- `TEST_INFRA_FAILURE != Beweis, dass Code kaputt ist`
- nichts davon zählt als PASS.

pytest-Exitcodes: 0→PASS, 1→FAILURE, 2→CANCELLED_BLOCKED, 3/4/5→INFRA, sonst UNKNOWN.

## 5. Evidence-Identität & Reuse

Ein `EvidenceRecord` bindet an: Selektor, `source_hash` (Produktcode-Content),
`test_definition_hash` (Testdefinitionen-Content, inkl. `e2e-fixture/tests`),
`config_hash` (externe pytest-Konfiguration), `root` (kanonisierter Projekt-Root),
`plan_hash`, `inventory_hash`, `policy_hash`, `executor_id`, Klassifikation,
Timestamp, Artifact-Ref, bounded Summary, Test-Count und `evidence_hash`.

`evidence_hash` ist seit dem Fix-Round **kein** öffentlicher unkeyed SHA-256 mehr,
sondern ein **HMAC-SHA256** über die kanonischen Identity-/Resultatfelder (F4). Der
Schlüssel liegt **nicht** im Agenten-Schreibbereich: er kommt aus
`ARGENT_EVIDENCE_MAC_KEY_FILE` (Datei außerhalb des Worktrees, bevorzugt) oder
`ARGENT_EVIDENCE_MAC_KEY` (Env), und ist für Tests injizierbar. Evidence ohne gültige
MAC wird fail-closed abgelehnt und nie wiederverwendet.

Reuse-Regeln (nur exakt):
- Nur `TEST_PASS`-Records sind wiederverwendbar.
- Identität muss **exakt** matchen (alle Felder) und `evidence_hash` muss intakt sein
  (keyed MAC verifiziert).
- `FAIL` → nie PASS via Reuse. `UNKNOWN`/tampered/fremde Provenienz → Rerun.
- Konservative Invalidierung: jede Änderung an Source, Testdefinition, Inventory, Policy
  oder Plan ändert einen Hash und entwertet alte PASS-Evidence.

## 6. Invalidierungsregeln

Änderung an → entwertet:
- Produktcode (`source_hash`) → alte PASS-Evidence ungültig
- Testdefinition (`test_definition_hash`) → ungültig
- Inventory-Version/Hash → ungültig
- Policy-Version/Hash → ungültig
- Plan-Hash → ungültig (fix/changed snapshot erzwingt Recompute)

## 7. Restart/Crash-Verhalten

- Evidence wird erst **nach** terminaler Klassifikation persistiert; ein unterbrochenes
  `RUNNING` wird nie als PASS persistiert.
- `reconcile_running(record)` (expliziter konservativer Pfad): nicht-terminaler Record
  bleibt/`UNKNOWN`, ein PASS nur wenn `is_intact()`; tampered PASS → `UNKNOWN`.
- Keine neue Scheduler-Architektur; schwere Restart-Semantik bleibt Phase G.

## 8. Resource-Integration (Phase C bleibt bindend)

`ResourceGate` wird vor jeder Stufe konsultiert; **ohne** konfiguriertes Gate wird die
Ausführung fail-closed verweigert (BLOCKED + RESOURCE_FAILURE). Ablehnung → `BLOCKED` +
`RESOURCE_FAILURE`, Limits werden nie abgeschaltet, um Tests „fertig zu bekommen".
Full Suite ist ~32 s → kein Über-Engineering bei Scheduling.

## 9. Economy-Metriken (observierend)

`ExecutionReport` trägt: `stages_planned`, `stages_executed`, `stages_reused`,
`stages_avoided`, `full_suite_avoided`, `wall_clock_seconds`, `total_tests`.
Keine Optimierung auf einen Score; Assurance-Regeln bleiben autoritativ.

## 10. Demonstrierte eingesparte Arbeit

`test_economy_demo_early_failure_avoids_later_stages_then_fix_reruns` (CASE O):
1. Scheduler-Plan (4 Stufen), targeted schlägt echt fehl → Verdict `FAILED`,
   `stages_avoided >= 3`, `full_suite_avoided = True`, `tests/` wird **nicht** aufgerufen.
2. Fix → neuer Snapshot → targeted reruns, spätere Pflicht-Stufen laufen erst nach
   erfolgreicher früherer Stufe, Full Suite schließt.

## 11. Bekannte Limitationen

- Gruppen-Runtime/Kosten nicht im Inventory (bewusst F3/Inventory überlassen).
- `planner_metadata_uncertainty` weiterhin ohne separaten Trigger (Metadata fail-closed
  beim Laden, F1-Konvention).
- Reuse ist exakt-identitätsbasiert (safe correctness > Cache-Hit-Rate); inkrementelle
  Dependency-Proofs für „unrelated change reuse" sind bewusst NICHT enthalten.
- `PytestRunner` nutzt `subprocess.run` (argv-Liste, `cwd=root`); echte cgroup-Scope-
  Enforcement des Phase-C-Pfads wird vom Supervisor über den bestehenden
  Enforcement-Pfad erzwungen, nicht vom Executor dupliziert. Für den realen Einsatz ist
  eine Bridge `ResourceGovernorGate` (→ `ResourceGovernor`/`ExecutionEnforcer`)
  dokumentiert und injizierbar.

## 13. Fix-Round F1–F9 (Sol Review, alle bestätigt)

Trust-Grenze des Evidence-Stores: Der Store-Pfad soll **außerhalb** des
Agenten-Schreibbereichs liegen (vom Supervisor gesetzt); die HMAC-Schlüsseldatei liegt
ebenfalls außerhalb des Worktrees. Beides zusammen (MAC + Store-Pfad außerhalb) ist die
Verteidigung gegen „FAIL→PASS drehen und Hash neu berechnen".

| Finding | Fix |
|---|---|
| F1 HIGH | Globs controllerseitig deterministisch auflösen (sortiert, validiert, erlaubte Wurzeln), explizite Dateipfade an pytest; Zero-Match fail-closed `TEST_INFRA_FAILURE` |
| F2 HIGH | `extra_roots` (Default `e2e-fixture`) wirklich in die Scans einbezogen; Symlink-Ziele sicher aufgelöst (cycle-safe); `__pycache__`/`.pyc`/`.pytest_cache` ausgeschlossen; externe pytest-Konfig als `config_hash` |
| F3 HIGH | `execute_plan` prüft fail-closed: `plan_hash` über authentischen Inhalt re-verifiziert, Stufenreihenfolge targeted→module→phase_regression→full_suite, eindeutige Namen, keine leeren Stufen/Selektoren, `full_suite` vorhanden wenn `full_suite_required` |
| F4 HIGH | `evidence_hash` = HMAC-SHA256 (Schlüssel außerhalb des Agenten-Schreibbereichs, injizierbar); Evidence ohne gültige MAC fail-closed abgelehnt |
| F5 HIGH | Runner bindet kanonischen Root + `cwd=root`; Snapshot-Root gegen Ausführungs-Root validiert (Mismatch fail-closed); Root/Config/Executor in die Evidence gebunden |
| F6 HIGH | Kein `AllowAll`-Default; ohne Gate fail-closed BLOCKED/RESOURCE_FAILURE; Bridge `ResourceGovernorGate` zum echten Phase-C-Pfad |
| F7 MEDIUM | Klassifikation geschärft: Setup-/Collection-/Fixture-Fehler → `TEST_INFRA_FAILURE`; RC 137 nur mit Scope-Evidence `RESOURCE_FAILURE`, sonst UNKNOWN; RC 124 nie PASS/TEST_FAILURE |
| F8 MEDIUM | Non-PASS `summary`/`artifact_ref` in `SelectorResult`/`StageExecution` erhalten + bounded persistiert (nie als PASS wiederverwendbar) |
| F9 LOW | Snapshot-Scan schließt `__pycache__`/`.pyc`/`.pytest_cache` aus; Store trimmt auch bei `_load`; Längenlimits summary (1000) / artifact_ref (256) |

## 12. F3-Grenze

F3 (Adversarial Test-Economy Acceptance + Phase-F-Closure) konsumiert F1-Planner und
F2-Executor; es erweitert die adversariellen Akzeptanztests und schließt Phase F. F2
führt **keine** F3-Arbeit aus.
