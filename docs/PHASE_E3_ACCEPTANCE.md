# PHASE E3 ACCEPTANCE — Evidence + Validated Fallback + Provenance

**Branch:** `phase-e3-benchmarks-validated-fallback` (Base `f546b68` = E2 GREEN).
**Datum:** 2026-09-02

Ehrliche Evidenz-Klassifikation: **UNIT/COMPONENT** (reine Router-/Registry-Semantik) vs
**INTEGRATED** (echter Supervisor-/create_dispatch-Pfad, D3-F4-Variante-A-Muster). Keine
Schein-Tests.

---

## 1. Evidenz-Klassifikation

| Kategorie | Bedeutung | Datei |
|---|---|---|
| **UNIT/COMPONENT** | Reine Router-/Evidence-/Policy-Semantik über konstruierte Requests/Registries. Kein Supervisor-Lauf. | `test_phase_e3_router.py` (32) |
| **INTEGRATED** | Echter Pfad `_build_routing_request` (Snapshot) → `route` (Fallback) → `core.create_dispatch` (Materialisierung + Provenienz-Persistenz) mit Fake-Enforcer/Runtime. | `test_phase_e3_integration.py` (5) |
| **FIX-ROUND F1–F4** | Regression der vier Sol-Findings: real-path Provider-Producer, TTL-Recovery, Registry-Baseline, adversarial inputs_hash, Duplikat-Keys/Entry-Version. | `test_phase_e3_fix_round.py` (13) |

---

## 0. Fix-Round F1–F4 (Sol-Review-Findings)

| Finding | Fix | Testnachweis |
|---|---|---|
| **F1** NO_VALID_FALLBACK-Backoff strandete geleaste Jobs (RUNNING+owner-NULL+lease-NULL, weder claimbar noch recoverbar) | `_provider_unavailable_backoff` → `_transition_job(QUEUED, BACKOFF)` + Lease-Release + Fence | `test_f1_provider_backoff_requeues_leased_job`, `test_f1_provider_backoff_persistent_error_branch` |
| **F2** Kein realer Producer für Provider-Availability; Registry-UNAVAILABLE vor Fallback weggefiltert; kein TTL | `mark_agent_failed(error_class/error_code)` + `classify_attempt(error_code)` + Trajectory-`errorCode`-Threading; Registry-Baseline in `_effective_availability`; bounded TTL | `test_f2_real_provider_producer_and_fallback`, `test_f2_snapshot_latest_success_lifts_unavailable`, `test_f2_registry_unavailable_triggers_fallback_not_no_eligible` |
| **F3** Provenienz nicht vollständig reproduzierbar / nicht Core-gebunden | policy v2 + `policy_hash`/`registry_hash`/`evidence_hash` + voller Input-Kanon + `inputs_hash` in canonical + Core-Feld-Bindung | `test_f3_content_change_changes_decision_id`, `test_f3_adversarial_tampered_inputs_hash_rejected`, `test_f3_decision_binds_all_fields` |
| **F4** Evidence-Registry nicht fail-closed bei Duplikat-Keys/Entry-Version | `object_pairs_hook=_no_duplicate_keys` + `entry.version == evidence_version` | `test_f4_evidence_rejects_duplicate_document_key`, `test_f4_evidence_rejects_duplicate_entry_key`, `test_f4_evidence_rejects_mismatched_entry_version` |

---

## 2. Acceptance CASE 1–24

| CASE | Beschreibung | Evidenz | Status |
|---|---|---|---|
| 1 | Routine-Koordination → minimum-sufficient Flash-Klasse | UNIT `test_router_implementer_low_risk_minimum_sufficient_flash` (E2) | ✅ |
| 2 | Normal-Coding → Pro-Klasse | UNIT `test_router_implementer_normal_minimum_sufficient_pro` (E2) | ✅ |
| 3 | Security-Closing-Review → Sol-Floor + Independence | UNIT `test_router_lead_and_reviewer_are_sol` + `test_router_reviewer_independence_different_model` (E2) | ✅ |
| 4 | billigerer Kandidat unter Floor → nie gewählt | UNIT `test_case6_fallback_never_below_floor` + E2 `test_router_bootstrap_only_never_auto_eligible` | ✅ |
| 5 | primärer Provider unavailable + policy-autorisierter Floor-erfüllender Alternativkandidat → validierter Fallback | UNIT `test_case5_fallback_flash_unavailable_to_pro` + INTEGRATED `test_case5_integrated_fallback_real_path` | ✅ |
| 6 | Fallback billiger aber unter Floor → abgelehnt | UNIT `test_case6_fallback_never_below_floor` | ✅ |
| 7 | kein Kandidat erfüllt Floor → fail-closed/block, kein Downgrade | UNIT `test_case7_no_candidate_meets_floor_fail_closed` | ✅ |
| 8 | Provider-Timeout erhöht NICHT Capability-Escalation | UNIT `test_transient_provider_failure_is_not_a_fallback_trigger` + E2 `test_case8_provider_timeout_does_not_escalate` | ✅ |
| 9 | wiederholte distinkte Code-Failures → Capability-Escalation (E2), KEIN Provider-Fallback | E2 `test_case4_two_red_fix_attempts_escalate_to_sol` + UNIT `test_router_hard_root_cause_escalates_to_sol` | ✅ |
| 10 | malformed Benchmark-Registry/Policy → deterministisch abgelehnt | UNIT `test_evidence_rejects_*` (7) + `test_policy_rejects_bad_*` (4) | ✅ |
| 11 | unbenchmarkt/UNKNOWN autorisiert NICHT | UNIT `test_router_evidence_gate_unknown_not_eligible` + `test_evidence_missing_is_unknown` | ✅ |
| 12 | policy-autorisiert aber unzureichende Evidence/Capability → nicht wählbar | UNIT `test_router_evidence_gate_rejected_not_eligible` | ✅ |
| 13 | fähig aber nicht policy-autorisiert → nicht wählbar | UNIT `test_case13_capable_but_not_policy_authorised_not_selected` | ✅ |
| 14 | Reviewer-Independence übersteht Fallback | UNIT `test_case14_reviewer_independence_survives_fallback` | ✅ |
| 15 | fehlende Writer-Provenienz bei required closing review → fail-closed | E2 `test_case11_reviewer_independence_fails_closed_same_model` | ✅ |
| 16 | gleiche Input-Snapshots → gleiche Decision+Provenienz | UNIT `test_case16_same_inputs_same_decision_and_provenance` | ✅ |
| 17 | Versions-/Hash-Änderung in persistierter Provenienz sichtbar | UNIT `test_case17_version_change_visible_in_provenance` + INTEGRATED `test_case5_integrated_fallback_real_path` (asserts inputs_hash) | ✅ |
| 18 | Fallback/Provider-Failure ändert Tool-Permissions NICHT | INTEGRATED `test_case18_19_20_fallback_keeps_permissions_and_policies` | ✅ |
| 19 | Context-Policy bleibt bindend | INTEGRATED `test_case18_19_20_fallback_keeps_permissions_and_policies` (Decision trägt kein Budget) | ✅ |
| 20 | Resource-Policy bleibt bindend | INTEGRATED `test_case18_19_20_fallback_keeps_permissions_and_policies` | ✅ |
| 21 | Restart/Reopen erklärt Decision aus persistierter Provenienz | INTEGRATED `test_case21_reopen_reads_persisted_provenance` | ✅ |
| 22 | DONE-Terminal-Immutabilität intakt | INTEGRATED `test_case22_23_terminal_not_reopened_by_fallback` | ✅ |
| 23 | BLOCKED/FAILED werden von Fallback-Logik nicht wieder geöffnet | INTEGRATED `test_case22_23_terminal_not_reopened_by_fallback` | ✅ |
| 24 | unavailable starkes Security-Modell ohne gleichwertigen Fallback → KEIN stiller schwächerer Security-Review | UNIT `test_case24_unavailable_strong_security_model_no_weaker_review` + INTEGRATED `test_case24_integrated_reviewer_sol_unavailable_fail_closed` | ✅ |

---

## 3. Testergebnis (Writer)

| Suite | Anzahl | Ergebnis |
|---|---|---|
| E3 targeted (`test_phase_e3_router.py` + `test_phase_e3_integration.py` + `test_phase_e3_fix_round.py`) | 50 | ✅ grün |
| E2 (`test_phase_e2_*.py`) | 76 | ✅ grün |
| E1 (`test_phase_e1_*.py`) | 82 | ✅ grün |
| D1–D3 (`test_phase_d*.py`) | 243 | ✅ grün |
| C1–C3 (`test_phase_c*.py`) | 296 | ✅ grün |
| B1–B4 (`test_phase_b*.py`) | 166 | ✅ grün |
| **Full Suite** (`tests/`) | **2169** | ✅ grün |

`git diff --check`: sauber.

---

## 4. Ehrlichkeitsregeln

- **`benchmarked` ≠ autorisiert**: Evidence ist ein unabhängiger Filter **zusätzlich** zu
  Floor und Policy-Allowlist; keiner der drei reicht allein.
- **`Policy-erlaubt` ≠ fähig genug**: Floor und Evidence werden unabhängig geprüft.
- **Kein VERIFIED ohne echte Benchmarks**: Bestandsmodelle PROVISIONAL/UNKNOWN, `VERIFIED`
  beim Laden abgelehnt, `benchmarked:false` dokumentiert.
- **Provider-Failure ≠ Capability-Failure**: TRANSIENT/EXTERNAL erzeugen keinen
  Snapshot-Eintrag und keinen Fallback; Fallback löst nie Escalation aus.
