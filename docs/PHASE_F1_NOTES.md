# PHASE F1 NOTES — Test Inventory + Change Impact + Deterministic Test Planning

**Branch:** `phase-f1-test-inventory-impact-planning` (Base `006eec3` = E GREEN).
**Datum:** 2026-09-02

Phase F realisiert **Test Economy** = "minimal ausreichende Test-Evidenz pro
Entwicklungsstufe, deterministisch erweitert bei Risiko/Evidence".  F1 liefert
nur die *Planungs*-Ebene (Inventory, Impact, Risiko, deterministischer Plan);
die **Execution** (Staging/Reuse/Eskalation zur Laufzeit) ist bewusst F2.

Kein LLM entscheidet Test-Suffizienz.  Der Planner ist deterministisch.

## 1. Test-Inventory-Modell (A)

Ein einziges versioniertes, maschinenlesbares Metadata-File:

* `argent_core/registry/test_inventory_v1.json` (Version `"1"`)

Enthaltene Fakten:

* `module_ownership` — Modul-Pfad → Subsystem (`CORE`, `SUPERVISOR`,
  `RESOURCE`, `CONTEXT`, `MODEL_ROUTING`, `SECURITY`, `PERSISTENCE`,
  `TEST_INFRA`, `DOCUMENTATION`).  Vollständig, deterministisch, read-only.
* `subsystem_tests` — pro Subsystem `module_selectors` (Modul-/Unit-Regression)
  und `phase_selectors` (Phasen-Regression), als pytest-Selektor-Globs.
* `targeted_tests` — Modul → die exakt zugehörigen Testdateien (stabiler
  Selektor statt nur Namensheuristik).
* `full_suite_selector` — `tests/`.

Klassifikation (Scope): UNIT/TARGETED (via `targeted_tests`), MODULE (via
`module_selectors`), PHASE_REGRESSION (via `phase_selectors`),
SECURITY/TRUST_BOUNDARY und CRASH_RECOVERY (via `SECURITY`-/`SUPERVISOR`-
Subsystem-Selektoren), FULL_SUITE (via `full_suite_selector`).

## 2. Change-Impact-Modell (B)

`ChangeEvidence` trägt **nur trusted facts**: `changed_paths`, `base_ref`,
`schema_migration`, `phase_closing`, `security_reviewed`.  Es gibt **kein**
freies "das ist nur eine kleine Änderung"-Feld.

`derive_change_impact()` mappt Pfade → Subsysteme (via `module_ownership`),
erkennt Dokumentations-Pfade und Test-Infrastruktur-Pfade *vor* der
Subsystem-Auflösung (Test-Infra wird nie als UNKNOWN gemeldet) und leitet
`ChangeImpact` ab (Subsysteme, Risiko-Tags, unbekannte Pfade, Risiko).

## 3. Risiko-Modell (C)

Bounded `RiskLevel`: `LOW` / `MEDIUM` / `HIGH` / `CRITICAL`.

Risiko wird **ausschließlich** aus Evidence abgeleitet:

* `subsystem_risk_tags` (Policy) — Subsystem → Standard-Risiko-Tags.
* `module_tag_overrides` (Policy) — feingranulare Tags (z.B. `state_machine.py`
  → `TERMINAL_STATE_TRANSITION`, `workspace_broker.py` →
  `WRITE_BROKER_EXECUTION_BOUNDARY`).
* `risk_raising_changes` (Policy) — Tag → Risiko-Level.

Erhöhende Beispiele (aus der echten Repo-Architektur): Persistenz/Schema
(`store.py` → `SCHEMA_MIGRATION` → CRITICAL), Lease/Fencing/Scheduler,
Security/Trust-Boundary, Write-Broker/Execution-Boundary, Crash-Recovery,
Process-Ownership, Resource-Enforcement, Context-Integrity, Model-Routing-
Independence, Terminal-State-Transitions, Test-Infrastruktur.

Ein Dokumentation-only-Change bleibt LOW (nachweisbar docs-only).
`schema_migration=True` erzwingt CRITICAL, auch ohne `store.py`-Pfad.

## 4. Deterministischer Plan-Algorithmus (D)

`build_test_plan(change_evidence, policy, inventory)` erzeugt geordnete,
deduplizierte Stufen:

1. **targeted** — exakte Testdateien der geänderten Module.
2. **module** — Modul-Regression der betroffenen Subsysteme.
3. **phase_regression** — Phasen-Regression (≥ MEDIUM, oder immer bei
   Hard-Invariants), plus Hard-Invariant-Selektoren.
4. **full_suite** — wenn die Policy es verlangt.

UNKNOWN-Pfade und Test-Infra-Änderungen erweitern die Phasen-Regression und
erzwingen die Full Suite.

## 5. Hard Safety Invariants (E)

Policy `hard_invariants`: `PERSISTENCE`, `SECURITY`, `SUPERVISOR` (jeweils
`full_suite: true`) sowie `RESOURCE`, `MODEL_ROUTING`, `CONTEXT`.  Diese
Regressionen sind **immer** enthalten und können nicht durch Kosten-/Laufzeit-
Ranking entfernt werden ("hard rule wins").

## 6. Full-Suite-Policy (F)

Full Suite bleibt Pflicht bei: Phase-Closing, Risiko HIGH/CRITICAL,
Test-Infra-Änderung, Schema/Migration, Multi-Subsystem, Test-Plan-Unsicherheit
(UNKNOWN), Planner-Metadata-Unsicherheit, unabhängig reviewtem Security-Patch.

Die Full Suite kostet aktuell ~36 s (2215 Tests).  Es gibt daher **keine**
aggressive Reduktion; nur LOW-Risiko-lokale Iterationen dürfen schmaler planen.

## 7. UNKNOWN-Handling (G)

UNKNOWN → **breiter**, nie enger.  Ein nicht mappbarer Pfad erzwingt mindestens
MEDIUM-Risiko, alle vier Phasen-Regressionen + Security-Regression + Full
Suite.

## 8. Test-Infra-Sonderrisiko (H)

Änderungen an Test-Helfern/Fixtures/pytest-Config/Planner-Metadata werden vor
der Subsystem-Auflösung als `TEST_INFRA` klassifiziert und erzwingen
breite Regression + Full Suite.  Der Planner kann sich nicht mit einem gerade
geschwächten Testset selbst freibeweisen.

## 9. Provenienz (I)

`TestPlan` enthält bounded Provenienz: `change_set_hash`, `risk_level`,
`policy_version`+`policy_hash`, `inventory_version`+`inventory_hash`, alle
Stufen mit per-Selektor-Begründung, `escalation_reasons`, `plan_hash`.  Keine
stdout-Riesenlogs — nur Hashes und Selektor-Listen.

## 10. No Fake Economy (J)

Keine xfail/skip/Timeout-Tricks, keine Bestands-Test-Änderungen, keine
Assertion-Änderung.  `all_selectors()` ist dedupliziert; Selektor-Pfade werden
fail-closed validiert (müssen unter `tests/` liegen, keine `..`/absoluten
Pfade).

## 11. Baseline (L)

Gemessen (Supervisor, bounded, kein Host-Stress):

* Full Suite: **2215 passed in ~36 s** (2169 Baseline + 46 F1).
* Phasen-Gruppen (collect-counts): B=166, C=296, D=243, E=208 (E1=82, E2=76,
  E3=50).

## 12. Grenze zu F2 (M)

F1 bietet die API `build_test_plan(change_evidence, policy, inventory)`.
F2 wird daraus die **Execution** bauen (staged run, Evidence-Reuse,
Eskalation).  Bestehende Test-Invocations-Flows sind **nicht** ersetzt.

## 13. Bekannte Limitationen

* `module_ownership` ist eine statische, versionierte Abbildung; neue Module
  brauchen einen expliziten Eintrag (fail-closed: unbekannt → UNKNOWN →
  breiter Plan).
* Kosten/Runtime je Gruppe ist noch nicht in der Inventory verankert (nur
  selektorbasiert); F2 kann das ergänzen.
* `planner_metadata_uncertainty` ist als Policy-Condition vorhanden, aber in
  F1 noch ohne separaten Trigger (Metadata fail-closed beim Laden stattdessen).
* `smoke/` ist als manuell/nicht-autoritativ markiert (keine automatischen
  Selektoren); Änderungen dort bleiben konservativ via UNKNOWN.

## 14. Fix-Round (Sol-Review F1–F9)

Der unabhängige Sol-Closing-Review lieferte 9 Findings (F1–F5 HIGH,
F6–F9 MEDIUM), alle vom Supervisor mit eigenen Repro-Skripten bestätigt und in
einem einzigen Fix-Round geschlossen.  Zusammenfassung:

* **F1 (HIGH) — semantische Fail-closed-Floors:** Pflicht-Risk-Tags
  (Mindest-Level), Kern-Hard-Invariants (SECURITY/SUPERVISOR/PERSISTENCE,
  full_suite=true), UNKNOWN/Test-Infra-full_suite=true, Pflicht-Full-Suite-
  Bedingungen, und Planner-Selbstreklassifizierungs-Verbot sind jetzt im CODE
  verankert, nicht in der änderbaren Policy.
* **F2 (HIGH) — Selector-Validierung überall:** alle Selector-Felder werden
  validiert (erlaubte Wurzeln `tests/` + `e2e-fixture/tests/`, kein
  `..`/absoluter Pfad) und deterministisch gegen das echte Dateisystem mit
  Zero-Match-Rejection geprüft.
* **F3 (HIGH) — Doku-/Test-Infra-Reihenfolge:** Test-Infra wird VOR Doku
  geprüft; `.md` gilt nur unter `docs/` oder als Root-`*.md` als Doku;
  `.md` unter `tests/`/`e2e-fixture/` = TEST_INFRA; `.md` unter
  Produktverzeichnissen = UNKNOWN (konservativ).
* **F4 (HIGH) — worktree.py:** von CORE nach SUPERVISOR (Phase-B-Writer-/
  Lease-/Fencing-Boundary); Plan enthält Phase-B-Regression + Full Suite.
* **F5 (HIGH) — e2e-fixture/smoke:** e2e-fixture/* → TEST_INFRA mit echten
  `e2e-fixture/tests/`-Selektoren; `smoke/` als manuell dokumentiert.
* **F6 (MEDIUM) — Tiefen-Immutability:** innere Records sind frozen
  Dataclasses (`SubsystemTests`, `HardInvariant`, `HandlingPolicy`).
* **F7 (MEDIUM) — checkpoint.py:** Modul-Override
  LEASE_FENCING_SCHEDULER/CRASH_RECOVERY + Modul-Hard-Invariant → Phase-B +
  Full Suite (lease-gefencete, transaktionale Writes).
* **F8 (MEDIUM) — Pflichtgründe:** Gründe je Selector werden als Menge aller
  Gründe erhalten; Pflicht-Selektoren tragen ein explizites
  `mandatory`-Flag/`HARD INVARIANT`-Grund, unabhängig von früherer
  targeted/module-Einordnung.
* **F9 (MEDIUM) — Basename-Ambiguität:** kollidierende Basenames mit
  unterschiedlichen Subsystem-Zuordnungen werden fail-closed abgelehnt;
  identischer kanonischer Inhalt → identisches Verhalten (Case 14 verstärkt).
