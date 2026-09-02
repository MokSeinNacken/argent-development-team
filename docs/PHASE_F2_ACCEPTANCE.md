# PHASE F2 ACCEPTANCE — Staged Test Execution + Safe Evidence Reuse

**Branch:** `phase-f2-staged-test-execution` (Base `100a2b6` = F1 GREEN).
**Datum:** 2026-09-02

Evidenz-Klassifikation: deterministische, offline Tests über injizierte Fakes
(`tests/f2_helpers.py`). Keine echten Subprozesse/Scopes in den Tests.

## 0. Exit-Kriterien (F2 Spec) — Evidenz

| Kriterium | Evidenz |
|---|---|
| F1 TestPlan = einzige Selektions-Autorität | `execute_plan` verlangt `TestPlan` (TypeError sonst), keine zweite Planung |
| Staged Executor deterministisch | `execute_plan` + `test_case1/2/22` |
| echter früher Fehler verhindert spätere Ausführung | `test_case3`, `test_case12`, Economy-Demo |
| Pflicht-Closing-Stufen nie durch frühen PASS entfernt | `test_case4`, `test_case20`, `test_case21` |
| Exact-Snapshot-Reuse existiert | `test_case5`, `test_case23` |
| unsicherer Cross-Snapshot-Reuse abgelehnt | `test_case6/7/8/9/25` |
| Evidence-Provenienz bounded + reproduzierbar | `EvidenceRecord.evidence_hash`, `test_case22` |
| stale PASS kann Änderung nicht falsch befriedigen | `test_case6/7/8/9` |
| FAIL nie → PASS via Reuse | `test_case11` |
| UNKNOWN nie → PASS | `test_case10`, `test_unknown_not_pass` |
| Resource/Infra/Process-Fehler nicht als PASS | `test_case13/14/15`, `test_pytest_runner_*` |
| unterbrochene Ausführung nie → PASS ohne Beweis | `test_case16`, `reconcile_running` |
| Agent-Prosa steuert keine Testbefehle | `test_case17`, `test_pytest_runner_rejects_untrusted_selector` |
| kein shell=True/eval/exec | `test_case18` (Quellscan + argv-Liste) |
| Phase-C-Resource bleibt bindend | `test_case13`, `test_case29` |
| Phase-D-Kontext unverändert | kein Phase-D-Modul angefasst (kein Diff dort) |
| Phase-E-Routing unverändert | kein Phase-E-Modul angefasst; `test_case30` |
| F1-Hard-Regeln bleiben bindend | `test_case19/20/21` |
| DONE verlangt valide Evidence je Pflicht-Stufe | `test_case26`, `test_case27` |
| F2 targeted grün | 68 PASS (42 Basis + 26 Fix-Round F1–F9) |
| F1-Regression grün | 71 PASS |
| E/D/C/B + Full Suite grün | Full Suite 2308 PASS (2282 + 26 Fix-Round, additiv) |
| unabhängiger Sol-Review | durch Supervisor (separat) |
| jede bestätigte Finding geschlossen | durch Supervisor (separat) |
| ein lokaler Commit / kein Push / clean | durch Supervisor (separat) |

## 1. Acceptance Cases 1–30

| Case | Test | Ergebnis |
|---|---|---|
| 1 LOW-risk nur Pflicht-Stufen | `test_case1_low_risk_only_required_stages_run` | nur `targeted` läuft, DONE |
| 2 targeted+module in Reihenfolge | `test_case2_targeted_then_module_in_order` | beide, korrekte Reihenfolge |
| 3 targeted-Fehler stoppt später | `test_case3_targeted_failure_stops_later_stages` | spätere SKIPPED |
| 4 targeted pass + Full Suite Pflicht | `test_case4_targeted_pass_but_full_suite_mandatory_still_runs` | Full Suite läuft |
| 5 exakte Identität → Reuse | `test_case5_exact_identity_reuse` | reused, kein Runner-Call |
| 6 Source-Änderung entwertet | `test_case6_source_change_invalidates` | rerun |
| 7 Testdef-Änderung entwertet | `test_case7_test_definition_change_invalidates` | rerun |
| 8 Inventory-Hash-Änderung entwertet | `test_case8_inventory_hash_change_invalidates` | rerun |
| 9 Policy-Hash-Änderung entwertet | `test_case9_policy_hash_change_invalidates` | rerun |
| 10 UNKNOWN-Provenienz → Rerun | `test_case10_unknown_provenance_reruns` | rerun |
| 11 FAIL nie → PASS | `test_case11_previous_fail_never_reused` | rerun |
| 12 TEST_FAILURE stoppt teure Stufen | `test_case12_early_failure_preserves_actionable_evidence` | stages_avoided≥3 |
| 13 RESOURCE_FAILURE ≠ FAIL/PASS | `test_case13_resource_failure_not_pass_not_code_failure` | BLOCKED |
| 14 TEST_INFRA_FAILURE ≠ PASS | `test_case14_test_infra_failure_not_pass` | BLOCKED |
| 15 Timeout ≠ PASS | `test_case15_timeout_not_pass` | BLOCKED |
| 16 unterbrochenes RUNNING ≠ PASS | `test_case16_interrupted_running_never_becomes_pass` | UNKNOWN |
| 17 Prosa kann Kommando nicht injizieren | `test_case17_agent_prose_cannot_inject_command` | ValueError |
| 18 kein shell=True | `test_case18_no_shell_or_eval_in_product_code` | kein Treffer |
| 19 HIGH-risk breite Regression | `test_case19_high_risk_still_runs_broad_regressions` | phase+full |
| 20 Phase-Closing → Full Suite | `test_case20_phase_closing_forces_full_suite` | full läuft |
| 21 Test-Infra → breites Closing | `test_case21_test_infra_change_forces_broad_closing` | full läuft |
| 22 gleiche Inputs → gleiche Reihenfolge | `test_case22_same_inputs_same_stage_order_and_identities` | identisch |
| 23 Duplikat nur bei exakter Evidence | `test_case23_duplicate_stage_avoided_only_with_exact_evidence` | 2. Lauf reused |
| 24 malformierte Evidence fail-closed | `test_case24_malformed_persisted_evidence_fails_closed` (+`24b`) | ValueError |
| 25 Fix (Snapshot) erzwingt Recompute | `test_case25_fix_changing_snapshot_forces_recompute` | rerun |
| 26 DONE braucht alle Pflicht-PASS | `test_case26_done_requires_all_mandatory_stages_pass` | FAILED |
| 27 DONE/FAILED/BLOCKED-Semantik | `test_case27_terminal_semantics_done_failed_blocked` | korrekt |
| 28 Reuse erweitert keine Permissions | `test_case28_reuse_never_expands_permissions` | nur PASS |
| 29 Resource-Gate bindend | `test_case29_resource_gate_independently_binding` | BLOCKED |
| 30 Router unabhängig bindend | `test_case30_test_failure_does_not_self_escalate_model` | kein Modellpfad |

## 2. Economy-Demonstration (CASE O)

`test_economy_demo_early_failure_avoids_later_stages_then_fix_reruns`:
- Versuch 1 (Scheduler-Plan, 4 Stufen): targeted scheitert echt → Verdict FAILED,
  3+ Stufen vermieden, Full Suite vermieden, `tests/` nicht ausgeführt.
- Versuch 2 (neuer Snapshot nach Fix): targeted reruns, spätere Pflicht-Stufen erst nach
  erfolgreicher früherer Stufe, Full Suite schließt.

## 3. Messzahlen (Supervisor, unabhängig)

- `pytest tests/test_phase_f2*.py -q` → **68 passed** (42 Basis + 26 Fix-Round)
- `pytest tests/test_phase_f1*.py -q` → **71 passed** (unverändert)
- `pytest tests/ -q` → **2308 passed** (2282 + 26 Fix-Round, additiv, ~35 s)
- `git diff --check` sauber; kein `shell=True`/`eval(`/`exec(` im Produktcode

## 4. Fix-Round F1–F9 (Sol Review)

Alle neun bestätigten Findings wurden geschlossen; adversarielle Tests in
`tests/test_phase_f2_fix_round.py` (`test_f1_*` … `test_f9_*`, 26 Tests):

| Finding | Testnachweis |
|---|---|
| F1 HIGH | `test_f1_glob_selector_resolved_to_explicit_files`, `test_f1_zero_match_glob_fails_closed` |
| F2 HIGH | `test_f2_extra_roots_change_identity`, `test_f2_default_covers_e2e_fixture`, `test_f2_artifacts_excluded`, `test_f2_symlink_target_change_detected` |
| F3 HIGH | `test_f3_plan_hash_tamper_rejected`, `test_f3_duplicate_stage_names_rejected`, `test_f3_empty_stage_rejected`, `test_f3_out_of_order_rejected`, `test_f3_full_suite_missing_when_required` |
| F4 HIGH | `test_f4_mac_key_required_fail_closed`, `test_f4_unkeyed_hash_is_not_valid_mac`, `test_f4_tampered_fail_to_pass_rejected`, `test_f4_different_key_rejects` |
| F5 HIGH | `test_f5_runner_binds_cwd`, `test_f5_snapshot_root_mismatch_fails_closed`, `test_f5_evidence_binds_root` |
| F6 HIGH | `test_f6_no_gate_fails_closed`, `test_f6_resource_governor_gate_bridge` |
| F7 MEDIUM | `test_f7_fixture_setup_error_is_infra`, `test_f7_rc137_needs_scope_evidence`, `test_f7_rc124_never_pass` |
| F8 MEDIUM | `test_f8_failure_summary_preserved_in_report` |
| F9 LOW | `test_f9_summary_and_artifact_ref_truncated`, `test_f9_store_trims_on_load` |
