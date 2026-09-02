# PHASE F1 ACCEPTANCE — Test Inventory + Change Impact + Deterministic Test Planning

**Branch:** `phase-f1-test-inventory-impact-planning` (Base `006eec3` = E GREEN).
**Datum:** 2026-09-02

## 0. Exit-Kriterien (F1 Spec) — Evidenz

| Kriterium | Evidenz |
|---|---|
| versioniertes Test-Inventory | `argent_core/registry/test_inventory_v1.json` (`inventory_version:"1"`), fail-closed geladen (`TestInventory.from_dict`) |
| deterministische Change-Impact-Repräsentation | `ChangeEvidence`/`ChangeImpact`/`derive_change_impact` in `test_planning.py` |
| bounded Risiko-Klassifikation | `RiskLevel` (LOW/MEDIUM/HIGH/CRITICAL), deterministisch aus Tags |
| trusted Evidence, nicht Agent-Prosa | `ChangeEvidence` hat kein Freitext-Risikofeld; `test_case_13` |
| deterministische TestPlan-Generierung | `build_test_plan` + `plan_hash`; `test_case_14` (identische Inputs → identischer Hash) |
| Hard-Safety-Regeln nicht kosten-rankbar | Policy `hard_invariants`; `test_case_12` |
| UNKNOWN → konservativ breiter | `test_case_10`, `test_case_17` |
| Test-Infra erzwingt breites Closing | `test_case_9`, `test_adversarial_planner_self_change` |
| B/C/D/E-Subsysteme → relevante Regressionen | `test_case_3..6` |
| Phase-Closing verlangt Full Suite | `test_case_20` |
| Provenienz bounded + reproduzierbar | `TestPlan`-Felder (Hashes, Stufen, Gründe); `test_case_15` |
| malformed Inventory/Policy fail-closed | `test_case_16` + Inventory/Policy-Tests |
| keine Tests versteckt/geschwächt | Keine Bestands-Test-Änderung; 7 neue Dateien |
| F1 Acceptance grün | 71 PASS (`tests/test_phase_f1*.py`: 46 Acceptance/Inventory + 25 Fix-Round) |
| E/D/C/B Regression grün | Teil der Full Suite (E=208, D=243, C=296, B=166) |
| Full Suite grün | 2240 PASS in ~32 s (2169 Baseline + 71 F1, additiv) |
| unabhängiger Sol-Closing-Review | durch Supervisor (separat) |
| jede bestätigte Finding geschlossen | durch Supervisor (separat) |
| 0 ungelöste HIGH/CRITICAL | durch Supervisor (separat) |
| ein lokaler Commit / kein Push / clean | durch Supervisor (separat) |

## 1. Acceptance Cases 1–20

| Case | Bedeutung | Test |
|---|---|---|
| 1 | Dokumentation-only → kleiner targeted Plan | `test_case_1_*` |
| 2 | isoliertes low-risk Modul → targeted + Modul | `test_case_2_*` |
| 3 | Supervisor-State-Transition → Phase-B | `test_case_3_*` |
| 4 | Resource-Governor → Phase-C | `test_case_4_*` |
| 5 | Context-Integrity → Phase-D | `test_case_5_*` |
| 6 | Model-Routing → Phase-E | `test_case_6_*` |
| 7 | Security/Trust-Boundary → breit + Full Suite | `test_case_7_*` |
| 8 | Schema/Migration → Migration + Phase + Full | `test_case_8_*` |
| 9 | Test-Infra → nicht nur eigene Tests; Full Suite | `test_case_9_*` |
| 10 | Unbekannter Pfad → konservativ breiter | `test_case_10_*` |
| 11 | Multi-Subsystem → Union, dedupliziert | `test_case_11_*` |
| 12 | Hard-Regel schlägt billigeren Plan | `test_case_12_*` |
| 13 | Evidence HIGH schlägt Agent-"low"-Claim | `test_case_13_*` |
| 14 | deterministisch für identische Inputs | `test_case_14_*` |
| 15 | Policy/Inventory-Hash in Provenienz | `test_case_15_*` |
| 16 | malformed Metadata fail-closed | `test_case_16_*` |
| 17 | fehlendes Mapping lässt Security-Tests nicht weg | `test_case_17_*` |
| 18 | Terminal-State/Security nicht mit Mini-Subset schließbar | `test_case_18_*` |
| 19 | docs-only löst keine unnötigen B–E-Regressionen aus | `test_case_19_*` |
| 20 | Phase-Closing verlangt Full Suite | `test_case_20_*` |

## 2. Adversarielle Fälle

* leerer Change-Set → fail-closed
* Planner-Selbst-Änderung → kann sich nicht selbst freibeweisen
* Inventory-Selbst-Änderung → breites Closing
* `schema_migration=True` ohne `store.py`-Pfad → CRITICAL
* `security_reviewed=True` → Full Suite
* Risiko-Tags können Risiko nie senken
* Workspace-Broker/Execution-Boundary → HIGH + Full Suite

## 3. Messergebnisse (Writer, unabhängig ausgeführt)

* `python3 -m pytest tests/test_phase_f1*.py -q` → **71 passed** (46 Acceptance/Inventory + 25 Fix-Round F1–F9)
* `python3 -m pytest tests/ -q` → **2240 passed in ~31 s** (2169 Baseline + 71 F1, additiv)
* `git diff --check` sauber; **kein `shell=True`** im Produktcode.

## 4. Fix-Round (Sol-Review F1–F9)

Alle 9 Findings (F1–F5 HIGH, F6–F9 MEDIUM) vom Supervisor unabhängig bestätigt
und geschlossen.  Testnachweis: `tests/test_phase_f1_fix_round.py` (`test_f1*`–
`test_f9*`).  Details in `PHASE_F1_NOTES.md` §14.
