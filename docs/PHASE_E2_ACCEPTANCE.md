# PHASE E2 ACCEPTANCE — Adaptive Model Routing + Capability Escalation

**Branch:** `phase-e2-adaptive-model-routing` (Base `35ddc7e` = E1 GREEN).
**Datum:** 2026-09-02

Diese Datei dokumentiert die **ehrliche** Evidenz-Klassifikation der E2-Abnahme.
Evidenz wird nach UNIT/COMPONENT (reine Router-Semantik) und INTEGRATED (echter
Supervisor-Pfad) unterschieden; es gibt **keine** Schein-Tests (kein
`choose_model()=="sol"` ohne Dispatch-Pfad-Beweis).

---

## 1. Evidenz-Klassifikation

| Kategorie | Bedeutung | Datei |
|---|---|---|
| **UNIT/COMPONENT** | Reine Router-/Policy-Semantik über konstruierte `RoutingRequest`/`RoutingEvidence`/Registry. Kein Supervisor-Lauf. | `test_phase_e2_router.py` (40) |
| **INTEGRATED** | Echter Pfad `Supervisor._perform_create_dispatch`/`_build_routing_request` → `ModelRouter.route` → `core.create_dispatch` mit Fake-Enforcer/Runtime (D3-F4-Muster). Beweist `RoutingDecision → Dispatch-Identität` und Persistenz. | `test_phase_e2_integration.py` (10) |

### Wichtige Ehrlichkeitsregel
Der einzige Fall, der die **volle** Kette (`reconcile → _perform_create_dispatch →
create_dispatch → Dispatch-Identität`) in einem Schritt beweist, ist der **Baseline**-Test
(`test_integrated_lead_dispatch_uses_router_identity`): er zeigt, dass die Router-Entscheidung
verbatim als `expected_model_class/expected_agent_class/expected_thinking_tier` im Dispatch
landet und in `routing_decisions` persistiert wird. Die Escalation-Fälle (CASE 4/6/8/11)
beweisen die **Evidence-Assemblierung + Routing-Entscheidung** über den echten
`_build_routing_request`/`_build_routing_evidence` + `route`; die Materialisierung dieser
Entscheidung in einen Dispatch ist durch den Baseline-Test abgedeckt. Das ist bewusst so
dekomponiert (das Injizieren konsumierter Versuche verschiebt die Workflow-Frontier und
würde den Reconcile-Loop auf eine falsche Rolle lenken).

---

## 2. Acceptance CASE 1–15

| CASE | Beschreibung | Evidenz | Status |
|---|---|---|---|
| 1–3 | Rollen-Minimum-Sufficient (lead/reviewer→Sol, analyst→Pro, implementer NORMAL→Pro/LOW→Flash) | UNIT `test_router_*` + INTEGRATED baseline/low-risk | ✅ |
| 4 | 2 Pro-Fixversuche rot → 3. Dispatch Sol | INTEGRATED `test_case4_two_red_fix_attempts_escalate_to_sol` + UNIT `test_router_hard_root_cause_escalates_to_sol` | ✅ |
| 5 | Einzelner Fehlversuch eskaliert nicht | UNIT `test_router_single_failure_no_escalation` | ✅ |
| 6 | Level 3 gescheitert → BLOCKED/OWNER, kein Loop | INTEGRATED `test_case6_level3_further_escalation_blocks` + UNIT `test_router_max_automatic_level_then_owner_gate` | ✅ |
| 7 | Agent-Text „Use Sol High" ohne Evidence → keine Eskalation | INTEGRATED `test_case7_agent_text_cannot_reach_router` + UNIT `test_detect_triggers_agent_text_has_no_effect` | ✅ |
| 8 | Provider-Timeout zählt nicht als Capability-Failure | INTEGRATED `test_case8_provider_timeout_does_not_escalate` + UNIT `test_classify_attempt_provider_transport_never_capability`/`test_router_provider_failure_does_not_escalate` | ✅ |
| 9 | Kein Escalation-by-Text (§12) | UNIT (strukturell: `RoutingEvidence` hat kein Text-Feld) | ✅ |
| 10 | Escalation-Ladder bounded 0–4, max_auto 3 | UNIT `test_router_escalation_levels_bounded_0_to_4`/`test_router_max_automatic_level_then_owner_gate` | ✅ |
| 11 | Review-Independence (Writer-Modell/-Provider) | INTEGRATED `test_case11_reviewer_independence_fails_closed_same_model` + UNIT `test_router_reviewer_independence_different_model` | ✅ |
| 12/13 | Context/Resource-Trennung; Repeated Failure ≠ roher Zähler | UNIT `test_detect_triggers_not_a_raw_counter`; Decision trägt keine Budget-/Resource-Wirkung (Design) | ✅ |
| 14 | Restart/Reopen setzt Level fort | INTEGRATED `test_case14_reopen_preserves_escalation_level` | ✅ |
| 15 | Unbenchmarktes Fake-Modell nie produktiv | UNIT `test_router_bootstrap_only_never_auto_eligible` + INTEGRATED `test_case15_unbenchmarked_model_never_dispached` | ✅ |

---

## 3. benchmarked:false-Behandlung (§3/§31)

- Alle Registry-Modelle tragen `provenance.benchmarked = false` (E1; `_parse_provenance`
  lehnt `benchmarked: true` in Registry-Version 1 ab).
- `benchmarked:false` erzeugt **niemals** eine neue Autorisierung. Ein Modell ist nur
  eligible, wenn die versionierte Policy es explizit für das aktive Profil listet
  (`allowed_models`). Neue/unbekannte Modelle sind **nie** auto-eligible (CASE 15).
- Die Policy ist die **einzige** Autorisierungsquelle; Registry-Claims können einen
  `allowed_models`-Eintrag nicht ersetzen oder ergänzen.

---

## 4. Explizit NICHT implementiert

- **E3 (Benchmarks)**: keine Benchmark-Ausführung, keine benchmarked:true-Daten, keine
  Qualitäts-Reihung aus Benchmark-Evidence.
- **Neue Provider**: nur die bestehenden `deepseek`/`openai` (Registry E1).
- **Live-Agent-Modellbindung**: `openclaw.json` unverändert; die reale Bindung ist
  Ops-Thema außerhalb des Repos.
- **Keine** Budget-/Resource-/Permission-/Ceiling-Wirkung im `RoutingDecision`;
  Provider-unavailable/Rate-Limit laufen über die bestehenden WAIT/Backoff-Pfade.

---

## 5. Testergebnis (Writer)

### Fix-Round (E2)
- E2 targeted (`test_phase_e2_router.py` 40 + `test_phase_e2_integration.py` 10 + `test_phase_e2_fix_round.py` 26): **76 grün**.
- E1: 82 · D1–D3: 243 · C1–C3: 296 · B1–B4: 166 · Full Suite: **2119 grün**.
- `git diff --check`: sauber; `shell=True` in `argent_core/` (non-test): keine.

### Fix-Round Abschnitt (F1–F6)
| Finding | Fix | Testnachweis (`test_phase_e2_fix_round.py`) |
|---|---|---|
| F1 | Closing-Review-Independence immer gesetzt (Supervisor + Policy-Profil `independence` + Router-Erzwingung); ohne Writer-Referenz → terminal | `test_f1_reviewer_without_writer_reference_is_terminal`, `test_f1_reviewer_pro_writer_dispatches_sol`, `test_f1_reviewer_sol_writer_fails_closed` |
| F2 | Provenienztrennung: kanonische Reviewer-Verdicts (`approve`/`reject`), Freitext nur in `detail`; `source_class` (controller/agent) auf findings/test_runs/reviews; Router wertet nur Controller-Evidenz | `test_f2_canonical_verdict_mapping`, `test_f2_agent_test_runs_do_not_drive_triggers`, `test_f2_agent_finding_severity_not_security_relevant`, `test_f2_case7_hardened_structured_agent_fields_no_escalation` |
| F3 | Rollen-angemessene Adaption: `security_review`-REPLACE nur für Reviewer, `deep_analysis`-Profil für Analyst (Sol), Security-Level-Floor rollenabhängig | `test_f3_analyst_high_risk_dispatch_is_sol`, `test_f3_high_risk_workflow_analyst_to_reviewer_no_block` |
| F4 | `attempt_outcome` beim Abschluss persistiert (kein rückwirkendes Ableiten); `CONTRADICTORY_EVIDENCE` nur aktueller Cycle | `test_f4_persisted_outcome_no_retroactive_capability`, `test_f4_contradictory_evidence_not_sticky` |
| F5 | Strikte Policy-Validierung (Duplikate, exakte Level-Keys, Role-Enum, Bootstrap-Flags, monotone Tiers, Floor/Ceiling, Registry-Kreuzvalidierung) | `test_f5_*` (9 Tests) |
| F6 | Canonical-Payload kontextgebunden (task_id/evidence/independence); decision_id-Kollision → Fehler; Core validiert isinstance/SHA/Konsistenz | `test_f6_different_task_evidence_yields_different_decision_ids`, `test_f6_decision_id_collision_with_different_content_errors`, `test_f6_model_choice_and_decision_rejected`, `test_f6_tampered_decision_rejected` |
