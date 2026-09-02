# PHASE F3 ACCEPTANCE — Adversarial Test-Economy Acceptance + Phase-F Closure

**Branch:** `phase-f3-test-economy-acceptance` (Base `1f5b2ed` = F2 GREEN).
**Datum:** 2026-09-02

F3 ist die **Closing-Phase** von Phase F: sie beweist das integrierte
F1-Planer + F2-Executor-System adversariell, dokumentiert die exakte
Trust-Grenze (CODE-ENFORCED vs. OPERATIONALLY REQUIRED) und schließt die
Acceptance. Ein unabhängiger Sol-Closing-Review fand **10 Findings (F1–F10)**;
alle wurden vom Supervisor live reproduziert und in **einem** Fix-Round
vollständig geschlossen (§2). Kein Feature-Neubau; nur dokumentierte,
adversarial erzwungene Produkt-Härtungen.

Deterministische, offline Tests über die F2-Fakes (`tests/f2_helpers.py`).
Keine echten Subprozesse/Scopes in den Tests.

---

## 0. Exit-Kriterien — Evidenz

| Kriterium (Spec Sektion) | Ergebnis |
|---|---|
| A F1 = alleinige WAS-Autorität; F2 = WIE | `execute_plan` verlangt `TestPlan` (TypeError), re-verifiziert `plan_hash` **und** `plan_mac` (F1-Fix); Case 1, 34 |
| B Plan-Tampering fail-closed | Case 1–5; `_validate_plan` (test_execution.py:994) |
| C Evidence-Tampering nie PASS | Case 6–10, 38; `_verify_mac` |
| D HMAC/Store-Trust-Boundary dokumentiert | §1 unten + PHASE_F_ACCEPTANCE.md §7 |
| E Restart/Crash-Adversarial | Case 12, 13, 39; `reconcile_running` + Store-Generation-Fencing |
| F Early-Stopping-Adversarial | Case 14–16, 40 |
| G Reuse-Adversarial | Case 17–20 |
| H Failure-Klassifikation stabil | Case 21–24 + `test_h_*` |
| I Terminal-Verdict-Safety | Case 34, 35 + F6-Fix (`_authoritative`) |
| J Agent-Trust-Boundary | Case 25–27, 29, 30 + `test_j_*` |
| K Resource/Context/Routing-Unabhängigkeit | Case 31–33 + `test_k_*` |
| L Economy-Value (3 Szenarien) | `test_l_economy_three_scenarios`, `test_l_no_bloated_metrics` |
| M Self-Protection (hartes Closing) | `test_m_*` (Matrix) + F4/F5-Fix |
| N Cases 1–40 deterministisch | `tests/test_phase_f3_acceptance.py` |
| O Kein Gaming | keine xfail/skip, keine Bestands-Test-Schwächung; nur additive Tests + Fixes |
| F1/F2-Regression grün | F2 68 PASS, F1 71 PASS |
| Full Suite grün | **2392 PASS** (2376 + 16 Fix-Round-Tests, additiv) |
| R Docs | diese Datei + `PHASE_F_ACCEPTANCE.md` |
| S Commit | Supervisor (genau einer) |

---

## 1. Trust-Boundary-Befund (Sektion D, korrigiert F9)

Die Produktarchitektur trennt sauber zwischen **code-erzwungen** und
**operational erforderlich**. Nach dem Fix-Round sind die folgenden
Behauptungen **tatsächlich code-erzwungen** (mit Datei/Zeile):

**CODE-ENFORCED (maschinell erzwungen):**
- HMAC-SHA256 über kanonische Identity-/Resultatfelder; Verifikation vor jeder
  Wiederverwendung (`compute_evidence_mac`, `_verify_mac`, `find_reusable_pass`).
- Fail-closed Key-Auflösung **inkl. Mindestlänge 16 Bytes** (F7,
  `_resolve_mac_key` test_execution.py:640).
- Tamper-/MAC-Mismatch beim Laden → `ValueError` (`_load`).
- Atomarer Store-Write (`os.replace`) **+ Single-Writer-Generation-Fencing**
  (F3, `StaleWriteError` test_execution.py:723, `_save` :845).
- **Plan-Herkunft** via `plan_mac` (F1): nur `build_test_plan(mac_key=…)`
  münzt einen gültigen MAC; `_validate_plan` lehnt Pläne ohne gültigen MAC ab
  (test_execution.py:994, :1010; test_planning.py:277).
- **Snapshot-Neuberechnung** bei gesetztem Root (F2): `execute_plan`
  berechnet die Identität aus dem echten Baum und vergleicht
  (test_execution.py:1140–1147).
- **Basename-Alias-Schutz** (F4): `_TEST_INFRA_BASENAMES` (test_planning.py:182)
  verhindert die Reklassifikation geschützter Module über einen Bare-Basename.
- **Full-Suite-Floors** (F5): `full_suite_selector` muss `tests/` sein
  (test_planning.py:599); `unknown_handling`/`test_infra_handling` dürfen keine
  leere `required_regression` haben (test_planning.py:824).
- **DONE-Herkunft** (F6): `ExecutionReport.all_pass()` verlangt eine
  autoritative Origin-Bindung; ein 0-Stufen-Plan liefert BLOCKED, nie DONE
  (test_execution.py:980, :1111).
- `_validate_plan` re-verifiziert `plan_hash` + Stufenform; `reconcile_running`
  downgraded nie auf PASS; Safety-Floors liegen im Code, nicht in der Policy.

**OPERATIONALLY REQUIRED (Deployment/Verfahren, NICHT als Code-Eigenschaft
verkauft):**
- Der MAC-Key liegt **außerhalb** des Agent-Schreibbereichs (Env
  `ARGENT_EVIDENCE_MAC_KEY_FILE`/`ARGENT_EVIDENCE_MAC_KEY`); der Code kann nicht
  wissen, was der Agent beschreiben darf.
- **Signier-Autorität** = wer den keyed `EvidenceStore` hält; der Controller
  darf den keyed Store nicht an Agenten weiterreichen.
- Der **reale Runner** ist `PytestRunner` (argv-Liste, `shell=False`); der
  Controller kontrolliert, dass kein Fake injiziert wird.
- Die **Resource-Gate-Injektion** (F8): `ResourceGovernorGate` ist ein dünner,
  injizierbarer Seam. Der Executor scheitert fail-closed **ohne** Gate (BLOCKED)
  und bei `DENY`, aber die echte Phase-C-Verdrahtung (wer das Gate injiziert) ist
  Supervisor-Pflicht — **keine** falsche CODE-ENFORCED-Behauptung.
- **Report-Consumption**: die `_authoritative`-Markierung ist ein weicher
  Marker; die harte Garantie ist, dass **nur** der Supervisor
  `execute_plan`-Reports konsumiert und direkt konstruierte Reports ignoriert.
- **Single-Writer-Koordination im Deployment**: das Generation-Fencing ist
  code-erzwungen, aber dass höchstens ein Executor gleichzeitig schreibt (bzw.
  stale Instanzen verworfen werden) ist operational.

---

## 2. Fix-Round F1–F10 (ein Fix-Round, vollständig geschlossen)

| Finding | Fix (Datei/Zeile) | Testnachweis |
|---|---|---|
| **F1 HIGH** — `plan_hash` authentifiziert keine F1-Herkunft | `plan_mac` (HMAC über kanonische Planfelder inkl. `plan_hash`) in test_planning.py:277; `build_test_plan(..., mac_key=…)` :1058; `_validate_plan` prüft MAC :1010 | `test_f1_unsigned_plan_rejected`, `test_f1_rehashed_weakened_plan_rejected` |
| **F2 HIGH** — Snapshot-Identität Caller-Versprechen | `execute_plan` berechnet `compute_snapshot_identity(root)` bei gesetztem Root und vergleicht Hashes :1140–1147 | `test_f2_snapshot_identity_recomputed_at_real_root` |
| **F3 HIGH** — stale Executor überschreibt neueres Ergebnis | `store_generation`-Fencing + `StaleWriteError` :723, `_save` :845, per-Instanz-`.tmp` + `os.replace` :876 | `test_f3_stale_executor_write_rejected`, `test_f3_single_instance_still_persists` |
| **F4 HIGH** — Self-Protection per Basename-Alias umgehbar | `_TEST_INFRA_BASENAMES` :182; Loader-Prüfung :523 | `test_f4_basename_alias_reclassification_rejected`, `test_f4_basename_alias_planner_rejected` |
| **F5 HIGH** — Full-Suite-Narrowing | `_FULL_SUITE_SELECTOR_FLOOR` :196/:599; nicht-leere `required_regression` :824 | `test_f5_full_suite_selector_narrowing_rejected`, `test_f5_test_infra_handling_empty_regression_rejected`, `test_f5_unknown_handling_empty_regression_rejected` |
| **F6 HIGH** — Terminal-DONE nicht herkunftsgesichert | `_authoritative`-Bindung :980; `all_pass()` :982; leerer Plan → BLOCKED :1111 | `test_f6_direct_done_construction_not_all_pass`, `test_f6_empty_plan_not_done`, `test_f6_authoritative_done_all_pass` |
| **F7 MEDIUM** — leerer MAC-Key akzeptiert | `_MIN_MAC_KEY_BYTES = 16` :81; `_resolve_mac_key` :666 | `test_f7_empty_and_short_keys_rejected`, `test_f7_valid_key_accepted` |
| **F8 MEDIUM** — Phase-C-Bindung nicht authentifiziert | ehrlich als OPERATIONALLY REQUIRED dokumentiert (§1, PHASE_F_ACCEPTANCE.md §7); `ResourceGovernorGate` verlangt Callable (bleibt) | `test_f8_resource_governor_gate_requires_callable` |
| **F9 HIGH** — Docs CODE-ENFORCED/OPERATIONALLY-REQUIRED falsch | beide Docs korrigiert (§1 hier; PHASE_F_ACCEPTANCE.md §7/§15) | Review-Nachweis: keine falsche CODE-ENFORCED-Behauptung mehr |
| **F10 MEDIUM** — F3-Tests beweisen Claims nicht | neue `tests/test_phase_f3_fix_round.py` (16 Tests) | alle `test_fN_*` |

**Mechanisch angepasste Helpers/Bestandstests (gleiche Semantik, nur Plan-MAC/
Key-Injektion):**
- `tests/f2_helpers.py`: `mk_plan` signiert jetzt mit `TEST_MAC_KEY`
  (`compute_plan_mac`); `real_plan`/`exec_plan` reichen `TEST_MAC_KEY` durch.
- `tests/test_phase_f2_acceptance.py` + `test_phase_f2_fix_round.py` +
  `test_phase_f3_acceptance.py`: direkte `tp.build_test_plan`/`te.execute_plan`
  Aufrufe reichen `mac_key=TEST_MAC_KEY` durch; Kurz-Key-Literale auf ≥16 Bytes
  verlängert (`test_d_fail_closed_key_resolution`, `test_d_signing_authority_*`,
  `test_f4_different_key_rejects`).
- Keine Bestandstest-Schwächung: F1 71, F2 68, F3 84 bleiben grün.

---

## 3. Acceptance Cases 1–40

| Case | Bedeutung | Test | Ergebnis |
|---|---|---|---|
| 1 | F2 kann F1-Pflicht-Stufe nicht entfernen | `test_case1_f2_cannot_remove_f1_mandatory_stage` | tamper → ValueError |
| 2 | forged plan_hash rejected | `test_case2_forged_plan_hash_rejected` | ValueError |
| 3 | Policy-Mismatch rejected | `test_case3_policy_mismatch_rejected` | kein Reuse |
| 4 | Inventory-Mismatch rejected | `test_case4_inventory_mismatch_rejected` | kein Reuse |
| 5 | Snapshot-Mismatch rejected | `test_case5_snapshot_mismatch_rejected` | rerun |
| 6 | PASS→tampered rejected | `test_case6_tampered_pass_rejected` | ValueError |
| 7 | FAIL→forged PASS rejected | `test_case7_fail_to_forged_pass_rejected` | ValueError |
| 8 | Missing MAC rejected | `test_case8_missing_mac_rejected` | ValueError |
| 9 | Invalid MAC rejected | `test_case9_invalid_mac_rejected` | ValueError |
| 10 | Partial Evidence rejected | `test_case10_partial_evidence_rejected` | ValueError |
| 11 | UNKNOWN-Identität → rerun | `test_case11_unknown_identity_reruns` | rerun |
| 12 | RUNNING nach Crash nie→PASS | `test_case12_running_after_crash_never_pass` | UNKNOWN |
| 13 | Prozess-Verschwinden ≠ PASS | `test_case13_process_disappearance_proves_no_pass` | BLOCKED |
| 14 | spätere Stufen nach echtem FAIL übersprungen | `test_case14_later_stages_skipped_after_genuine_test_failure` | SKIPPED |
| 15 | früher PASS überspringt Full Suite nicht | `test_case15_early_pass_cannot_skip_mandatory_full_suite` | full läuft |
| 16 | avoided Stage ≠ PASS auf fixiertem Snapshot | `test_case16_avoided_stage_on_broken_snapshot_not_pass_on_fixed` | full reruns |
| 17 | identischer Snapshot → Reuse | `test_case17_identical_snapshot_reuse` | reused |
| 18 | Testdef-Änderung invalidiert | `test_case18_test_definition_change_invalidates_reuse` | rerun |
| 19 | Policy-Änderung invalidiert | `test_case19_policy_change_invalidates_reuse` | rerun |
| 20 | Inventory-Änderung invalidiert | `test_case20_inventory_change_invalidates_reuse` | rerun |
| 21 | RESOURCE_FAILURE nie PASS | `test_case21_resource_failure_never_pass` | BLOCKED |
| 22 | TEST_INFRA_FAILURE nie PASS | `test_case22_test_infra_failure_never_pass` | BLOCKED |
| 23 | TIMEOUT nie PASS | `test_case23_timeout_never_pass` | BLOCKED |
| 24 | UNKNOWN nie PASS | `test_case24_unknown_never_pass` | BLOCKED |
| 25 | Prosa erzwingt Selector/Command nicht | `test_case25_agent_prose_cannot_force_selector_or_command` | fail-closed |
| 26 | Prosa erzwingt Resultat/Risiko/DONE nicht | `test_case26_agent_prose_cannot_force_result_risk_or_done` | kein Feld |
| 27 | kein shell=True/eval/exec | `test_case27_no_shell_or_eval_in_product_code` | kein Treffer |
| 28 | fehlender Key fail-closed | `test_case28_missing_key_fails_closed` | ValueError |
| 29 | Signierung Controller-owned | `test_case29_signing_is_controller_owned` | Key extern |
| 30 | Agent-writeable Artifact ≠ trusted PASS | `test_case30_agent_writable_artifact_alone_cannot_make_trusted_pass` | ValueError |
| 31 | Phase-C-Gate bindend | `test_case31_phase_c_resource_gate_binding` | BLOCKED |
| 32 | Phase-E-Router unabhängig | `test_case32_phase_e_router_independent` | kein Modellpfad |
| 33 | Phase-D-Kontext unberührt | `test_case33_phase_d_context_policy_unchanged` | Phase-D-Regression |
| 34 | DONE erfordert alle Stufen | `test_case34_done_requires_all_stage_evidence` | FAILED |
| 35 | Terminal-DONE immutabel | `test_case35_terminal_done_immutable` | frozen |
| 36 | Phase-F-Infra → Full Suite | `test_case36_phase_f_infra_change_requires_full_suite` | full required |
| 37 | malformed F-Policy/Inventory fail-closed | `test_case37_malformed_authoritative_metadata_fails_closed` | PolicyError/InventoryError |
| 38 | Duplikat/Konflikt konservativ | `test_case38_duplicate_conflict_evidence_conservative` | rerun |
| 39 | Restart/Reconcile idempotent | `test_case39_restart_reconcile_idempotent` | idempotent |
| 40 | broken→fix→closing-Fluss | `test_case40_integrated_broken_fix_closing_flow` | korrekte Evidence |

---

## 4. Sektions-Evidenz (A–M, kompakt)

- **A** — F1 ist alleinige WAS-Autorität; F2 konsumiert nur den `TestPlan` und
  re-verifiziert `plan_hash` **+ `plan_mac`** (F1-Fix). Case 1/34.
- **B** — entfernte Pflicht-Stufe, forged Hash, Policy/Inventory/Snapshot-Mismatch,
  malformed Selector, doppelte Stage-IDs, unbekannter Typ, leere Stufe,
  fehlendes `full_suite` → `_validate_plan` fail-closed. Case 1–5 + F2-Fix-Round.
- **C** — jede Identitäts-/Resultat-Dimension ist MAC-gebunden; Tamper bricht MAC.
  Case 6–10, 38.
- **D** — siehe §1; Beweis `test_d_*` + `test_fN_*` (F1/F2/F3/F7).
- **E** — kein RUNNING-ResultClass; `reconcile_running` promotet nie; atomare
  Writes + Generation-Fencing gegen stale Executor (F3). Case 12/13/39.
- **F** — nur echter TEST_FAILURE stoppt; früher PASS entfernt keine
  Pflicht-Closing-Stufe; SKIPPED ≠ PASS. Case 14–16.
- **G** — exakter Reuse über alle Identitätsdimensionen; UNKNOWN → rerun.
  Case 17–20.
- **H** — stabile Klassen; nur TEST_PASS erfüllt. Case 21–24 + `test_h_*`.
- **I** — DONE nur aus vollständigem autoritativen Lauf; `_authoritative`-Bindung
  + leerer Plan → BLOCKED (F6). Case 34/35 + `test_f6_*`.
- **J** — kein Prosa-Feld, kein Reuse-Flag, kein DONE-Forcer, kein shell/eval.
  Case 25–27, 29, 30 + `test_j_*`.
- **K** — Reuse/Plan erweitern keine Permissions. Case 31–33 + `test_k_*`.
- **L** — drei deterministische Szenarien mit exakten Metriken. `test_l_*`.
- **M** — Self-Protection-Matrix + Basename-/Narrowing-Floors (F4/F5). `test_m_*`
  + `test_f4_*`/`test_f5_*`.

---

## 5. Messzahlen (Writer, unabhängig ausgeführt)

- `pytest tests/test_phase_f3*.py -q` → **84 passed** (68 Writer + 16 Fix-Round)
- `pytest tests/test_phase_f2*.py -q` → **68 passed** (unverändert)
- `pytest tests/test_phase_f1*.py -q` → **71 passed** (unverändert)
- `pytest tests/ -q` → **2392 passed** (2376 Baseline + 16 Fix-Round, additiv, ~36 s)
- `git diff --check` sauber; **kein** `shell=True`/`eval(`/`exec(` im Produktcode

## 6. Limitationen (ehrlich)

- Die MAC-Key-/Store-Pfad-Platzierung **außerhalb** des Agent-Schreibbereichs ist
  OPERATIONALLY REQUIRED (Deployment), nicht code-erzwungen — siehe §1.
- Die **Resource-Gate-Injektion** (echte Phase-C-Verdrahtung) ist OPERATIONALLY
  REQUIRED (F8); der Executor scheitert nur fail-closed ohne Gate, fälscht aber
  keine CODE-ENFORCED-Behauptung.
- Die **Report-Consumption** ist OPERATIONALLY REQUIRED: `_authoritative` ist ein
  weicher Marker; die harte Garantie ist, dass nur der Supervisor
  `execute_plan`-Reports konsumiert.
- `compute_snapshot_identity` wird bei gesetztem Root **einmal pro
  `execute_plan`-Aufruf** neu berechnet (nicht pro Stage); die Full Suite bleibt
  dadurch bei ~36 s.
- Das Generation-Fencing deckt die single-writer-Koordination im Code ab; die
  TOCTOU-Lücke zwischen Generation-Read und `os.replace` bleibt eine minimale
  Betriebsannahme (kein paralleler Mehrprozess-Writer), wie in §1 dokumentiert.
- `reconcile_running` ist ein expliziter konservativer Pfad für einen
  hypothetischen RUNNING-Record; im F2-Modell wird RUNNING nie persistiert.
