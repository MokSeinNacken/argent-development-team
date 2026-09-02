# PHASE F ACCEPTANCE — Integrierte Test-Economy-Architektur (F1–F3)

**Branches:** F1 `phase-f1-test-inventory-impact-planning` → F2
`phase-f2-staged-test-execution` → F3 `phase-f3-test-economy-acceptance`.
**Datum:** 2026-09-02

Diese Datei ist die **integrierte** Abschlussdokumentation der gesamten Phase F
(Test Economy): deterministische Planung (F1), gestufte Execution + sichere
Evidence-Wiederverwendung (F2) und adversariale Akzeptanz + Closure (F3).

---

## 1. Architektur-Überblick

```
                      F1 (Planung)                         F2 (Execution)
   ┌───────────────────────────────────┐   ┌───────────────────────────────────┐
   │ ChangeEvidence (trusted facts)    │   │ execute_plan(TestPlan, runner,    │
   │   └─> derive_change_impact        │   │    snapshot, resource_gate, store)│
   │   └─> build_test_plan             │   │   _validate_plan (fail-closed)    │
   │ TestInventory / TestPolicy        │   │   staged: targeted→module→        │
   │   (versioniert, fail-closed load) │──▶│     phase_regression→full_suite   │
   │ RiskLevel / Subsystem / RiskTag   │   │   early-stop (genuine failure)    │
   │ hard invariants / safety floors   │   │   EvidenceStore (HMAC, reuse)     │
   │ plan_hash + plan_mac (provenance) │   │   PytestRunner (argv, no shell)   │
   └───────────────────────────────────┘   │   reconcile_running (crash-safe) │
                                            └───────────────────────────────────┘
```

**Trennlinie:** F1 ist die **alleinige Autorität für WAS** getestet wird; F2 ist
die **Autorität für WIE** (Reihenfolge, Stopp, Reuse). F2 baut keinen zweiten
Planungspfad und kann weder Pflicht-Stufen entfernen noch Risiko senken noch
UNKNOWN „sicher" machen noch die Full Suite downgraden noch einen alternativen
Plan erfinden.

---

## 2. Planner-/Executor-Trennung (Invariante A)

- `execute_plan` akzeptiert **nur** einen F1-`TestPlan` (sonst `TypeError`) und
  re-verifiziert `plan_hash` gegen den authentischen Inhalt **und** `plan_mac`
  gegen die F1-Herkunft (`_validate_plan`, `test_execution.py:994`). Ein
  in-flight mutierter Plan (Pflicht-Stufe entfernt, `required→optional`,
  Stufenreihenfolge geändert, forged Hash, doppelte/leere/unbekannte Stufen,
  fehlendes `full_suite`) wird fail-closed abgelehnt; ein Plan ohne gültigen
  `plan_mac` ebenso (nur `build_test_plan(mac_key=…)` münzt ihn — F3-Fix F1).
- F2 kann keine Stage *inhaltlich* entfernen: die Stufen kommen ausschließlich
  aus `build_test_plan`, der Pflicht-Stufen (Hard-Invariants, Full-Suite-Policy)
  immer einfügt.

---

## 3. Risiko- & Plan-Invarianten (F1)

- Risiko wird **ausschließlich** aus trusted Evidence abgeleitet
  (`derive_change_impact`); `ChangeEvidence` hat **kein** Freitext-Risikofeld.
- Safety-Floors liegen **im Code**, nicht in der änderbaren Policy:
  `_MANDATORY_RISK_TAGS`, `_CORE_HARD_INVARIANT_SUBSYSTEMS`,
  `_MANDATORY_FULL_SUITE_CONDITIONS`, Planner-Selbstreklassifizierungs-Verbot
  (`_PLANNER_OWNED_EXACT_PATHS`), **Basename-Alias-Schutz** (`_TEST_INFRA_BASENAMES`,
  test_planning.py:182) und **Full-Suite-Floor** (`_FULL_SUITE_SELECTOR_FLOOR`,
  :196).
- UNKNOWN → **breiter**, nie enger; Test-Infra-Änderung → breites Closing +
  Full Suite; der Planner kann sich nicht mit einem gerade geschwächten Testset
  selbst freibeweisen.

---

## 4. Early-Stopping (Invariante F)

- Nur ein **echter** `TEST_FAILURE` stoppt die Ausführung und überspringt
  spätere Stufen (`SKIPPED`). Ein unvollständiger Lauf mit `UNKNOWN` ist kein
  valider Failure (→ `BLOCKED`).
- Ein früher PASS entfernt **nie** später im Plan markierte Pflicht-Closing-
  Stufen (Full Suite bleibt Pflicht).
- Nicht ausgeführte Stufen sind `SKIPPED`, **nie** `PASSED`.

---

## 5. Exakter Reuse (Invariante G)

Wiederverwendung nur bei **exakter** Identität über alle autoritativen
Dimensionen: Selektor, `source_hash`, `test_definition_hash`, `config_hash`,
`root`, `plan_hash`, `inventory_hash`, `policy_hash`, `executor_id`. Jede
Änderung an Source, Testdefinition, Inventory, Policy oder Plan ändert einen
Hash und entwertet alte PASS-Evidence. `UNKNOWN`/tampered/fremde Provenienz →
Rerun.

---

## 6. Evidence-Integrität (Invariante C)

`EvidenceRecord` bindet Identität + Klassifikation + `evidence_hash` (HMAC-SHA256,
kein öffentlicher unkeyed SHA-256). Nur ein `TEST_PASS` mit gültigem MAC und
exakter Identität ist wiederverwendbar; `FAIL`/`UNKNOWN`/tampered nie.
Konflikt-Evidence (PASS + FAIL für dieselbe Identität) wird konservativ
behandelt (Rerun).

---

## 7. HMAC-/Store-Trust-Boundary (Invariante D, korrigiert F9)

### CODE-ENFORCED (maschinell erzwungen)

| Eigenschaft | Ort |
|---|---|
| HMAC über kanonische Identity-/Resultatfelder | `compute_evidence_mac` |
| Verifikation vor jeder Wiederverwendung | `_verify_mac`, `find_reusable_pass` |
| Fail-closed Key-Auflösung **inkl. Mindestlänge 16 Bytes** (F7) | `_resolve_mac_key` test_execution.py:640 |
| Tamper-/MAC-Mismatch beim Laden → Abbruch | `_load` |
| Atomarer Store-Write **+ Generation-Fencing** (F3) | `_save` :845, `StaleWriteError` :723 |
| **Plan-Herkunft** via `plan_mac` (F1) | `compute_plan_mac` test_planning.py:277, `_validate_plan` test_execution.py:1010 |
| **Snapshot-Neuberechnung bei Root** (F2) | `execute_plan` test_execution.py:1140–1147 |
| **Basename-Alias-Schutz** (F4) | `_TEST_INFRA_BASENAMES` test_planning.py:182/:523 |
| **Full-Suite-/Regression-Floors** (F5) | test_planning.py:599/:824 |
| **DONE-Herkunft + leerer Plan → BLOCKED** (F6) | `_authoritative`/`all_pass` test_execution.py:980/:982, :1111 |
| Reconcile promotet nie auf PASS | `reconcile_running` |
| Safety-Floors im Code | test_planning.py (Risk-Tags/Hard-Invariants/Full-Suite-Conditions) |

### OPERATIONALLY REQUIRED (Deployment/Verfahren)

| Versprechen | Bedeutung |
|---|---|
| MAC-Key außerhalb des Agent-Schreibbereichs | Env `ARGENT_EVIDENCE_MAC_KEY_FILE`/`ARGENT_EVIDENCE_MAC_KEY`; Code kann nicht wissen, was der Agent beschreiben darf |
| Store-Pfad außerhalb des Agent-Schreibbereichs | Konstruktor-Argument, Controller-set |
| Signier-Autorität = Controller | `add()` ist der einzige Signierpfad und hält den Key; Controller darf den keyed Store nicht an Agenten weiterreichen |
| Reale Ausführung über `PytestRunner` | Controller kontrolliert die `execute_plan`-Invocation; kein Fake-Runner in Produktion |
| **Resource-Gate-Injektion** (F8) | `ResourceGovernorGate` ist ein injizierbarer Seam; die echte Phase-C-Verdrahtung ist Supervisor-Pflicht (Executor fail-closed ohne Gate) |
| **Report-Consumption** | `_authoritative` ist ein weicher Marker; die harte Garantie ist, dass nur der Supervisor `execute_plan`-Reports konsumiert |
| **Single-Writer-Koordination** | Generation-Fencing ist code-erzwungen; dass höchstens ein Executor gleichzeitig schreibt, ist operational |

**Regel (Spec R):** operationale Versprechen werden **niemals** als
Code-Eigenschaft verkauft. Die Dokumentation unterscheidet beide Kategorien
explizit; es gibt keinen Code-Pfad, der ohne Key einen PASS signiert, einen
tampered PASS wiederverwendet oder ohne F1-Herkunft DONE liefert.

---

## 8. Restart/Crash (Invariante E)

- Evidence wird erst **nach** terminaler Klassifikation persistiert; RUNNING
  wird nie persistiert (es gibt keine RUNNING-`ResultClass`).
- Partielle Writes sind durch atomaren `os.replace` ausgeschlossen; ein stale
  Executor kann ein neueres Ergebnis nicht überschreiben (Generation-Fencing,
  `StaleWriteError` — F3-Fix F3).
- Crash nach PASS-Persistenz ist deterministisch (Record ist vollständig + MAC-
  verifiziert).
- `reconcile_running` ist idempotent und konservativ (nie PASS aus Prozess-
  Verschwinden); `UNKNOWN` bleibt konservativ.

---

## 9. Failure-Klassifikation (Invariante H)

`TEST_PASS | TEST_FAILURE | TEST_INFRA_FAILURE | RESOURCE_FAILURE |
PROCESS_FAILURE | TIMEOUT | CANCELLED_BLOCKED | UNKNOWN`. Nur `TEST_PASS`
erfüllt. `RESOURCE_FAILURE` ist **kein** Code-Fehler; `TEST_INFRA_FAILURE`
beweist keinen Code-Fehler; `TIMEOUT`/`UNKNOWN` nie PASS.

---

## 10. Self-Protection (Invariante M)

Änderungen an Planner, Executor, Inventory, Evidence-Helpers, F1–F3-Helpers oder
Policy werden als `TEST_INFRA` klassifiziert und erzwingen breites Closing
(B/C/D/E-Regression + Security) **inkl. Full Suite** — hart, nicht weg-rankbar.
F3 schloss zusätzlich: Executor fehlte im Inventory/Planner-Owned-Set;
Konflikt-Evidence; **Basename-Alias** (F4) und **Full-Suite-Narrowing** (F5)
sind code-seitig blockiert (siehe PHASE_F3_ACCEPTANCE.md §2).

---

## 11. Economy-Demonstration (Invariante L)

Drei deterministische Szenarien (siehe `test_l_economy_three_scenarios`):
1. **Kaputter Zwischensnapshot** → echter targeted FAIL → spätere Stufen nicht
   ausgeführt → messbar vermiedene Arbeit.
2. **Fixierter neuer Snapshot** → stale PASS invalidiert → frühe Stufen rerun →
   Pflicht-Stufen nach grün → Full Suite schließt.
3. **Identischer Snapshot** → exakter Reuse → Duplikate vermieden, keine
   aufgeblähten Zahlen.

---

## 12. Full-Suite-Policy

Full Suite bleibt Pflicht bei: Phase-Closing, Risiko HIGH/CRITICAL,
Test-Infra-Änderung, Schema/Migration, Multi-Subsystem, Test-Plan-Unsicherheit
(UNKNOWN), Planner-Metadata-Unsicherheit, unabhängig reviewtem Security-Patch,
sowie den Hard-Invariants (SECURITY/SUPERVISOR/PERSISTENCE). Der
`full_suite_selector` ist code-seitig auf `tests/` fixiert (F5).

---

## 13. Verifikationsergebnisse

- `pytest tests/test_phase_f1*.py -q` → **71 passed**
- `pytest tests/test_phase_f2*.py -q` → **68 passed**
- `pytest tests/test_phase_f3*.py -q` → **84 passed** (68 Writer + 16 Fix-Round)
- `pytest tests/ -q` → **2392 passed** (2376 + 16 Fix-Round, additiv)
- `git diff --check` sauber; kein `shell=True`/`eval(`/`exec(` im Produktcode

## 14. Sol-Review

Unabhängiger Sol-Closing-Review durch den Supervisor (separat). F1 lieferte
9 Findings, F2 lieferte 9 Findings, alle bestätigt und geschlossen; F3 lieferte
**10 Findings (F1–F10)**, alle vom Supervisor reproduziert und in **einem**
Fix-Round vollständig geschlossen (PHASE_F3_ACCEPTANCE.md §2). F9 (falsche
CODE-ENFORCED/OPERATIONALLY-REQUIRED-Trennung) ist in §7 dieser Datei korrigiert.

## 15. Findings & Limitationen

- **CODE-ENFORCED** Anteile: MAC-Verifikation, fail-closed Key-Auflösung +
  Mindestlänge, Tamper-Rejection, atomare Writes + Generation-Fencing,
  Plan-Herkunft (`plan_mac`), Snapshot-Neuberechnung, Basename-Alias-Schutz,
  Full-Suite-Floors, DONE-Herkunft, Plan-Integrität, Self-Protection-Floors.
- **OPERATIONALLY REQUIRED** Anteile: Key-/Store-Platzierung außerhalb des
  Agent-Schreibbereichs; Signier-Autorität des Controllers; reale
  Runner-Invocation; **Resource-Gate-Injektion**; **Report-Consumption**;
  **Single-Writer-Koordination** (siehe §7).
- `compute_snapshot_identity` wird bei gesetztem Root **einmal pro
  `execute_plan`-Aufruf** berechnet (Full Suite bleibt ~36 s).
- `reconcile_running` ist ein expliziter konservativer Pfad (RUNNING wird im
  F2-Modell nie persistiert).

## 16. Finales Verdict

Das integrierte F1→F2-System erfüllt die Phase-F-Invarianten. Die Trust-Grenze
ist korrekt umgesetzt und ehrlich dokumentiert (code-erzwungen vs. operational).
Alle adversariellen Acceptance-Cases 1–40, die Sektionen A–M und die
F3-Fix-Round-Findings F1–F10 sind durch deterministische, offline Tests belegt;
die Full Suite ist grün (2392 PASS). Keine offenen HIGH/CRITICAL-Findings aus F3.
