# PHASE E2 NOTES — Adaptive Model Routing + Capability Escalation

**Branch:** `phase-e2-adaptive-model-routing` (Base `35ddc7e` = E1 GREEN, verifiziert clean).
**Rolle:** E2-Writer (einziger Writer; kein Commit, kein Push — Worktree bleibt dirty; Supervisor committet nach Sol-Review).
**Datum:** 2026-09-02

> **Hinweis (unterbrochener Erstlauf + Vervollständigung):** Der erste E2-Writer-Lauf
> starb an einem LLM-Timeout, **nachdem** er einen kohärenten Teilstand implementiert
> hatte (`model_router.py`, Policy, Store/Core/Supervisor-Integration). Der Supervisor
> hat den Zustand read-only rekonziliert (KEIN Blind-Neustart, KEIN zweiter paralleler
> Writer). Dieser Lauf setzt den Teilstand fort und vollendet E2: er fixte den bekannten
> `NameError`-Bruch (zwei fehlende Evidence-Helper), vervollständigte die
> Trigger-Ableitung und die Closing-Review-Independence, schrieb die Tests und diese Doku.

---

## 0. Scope-Zusammenfassung (verbindlich)

E2 liefert den **deterministischen, provider-neutralen, adaptiven Model-Router** mit
**bounded Capability-Escalation**, basierend auf E1 (Registry + Bootstrap-Policy).
**NICHT implementiert** (explizit, keine Abweichung): E3 (Benchmarks), neue Provider,
Credentials, Background-Service, Parallelisierung, Live-Agent-Modellbindung
(`openclaw.json` bleibt unverändert — Bindung ist Ops-Thema außerhalb des Repos).

**Fundamentale Invarianten:** Qualität/Security/Capability-Floor **vor** Cost; Rollen
sind Capabilities, Modelle sind austauschbare Implementierungen (E1); **nur** die
versionierte Bootstrap-Routing-Policy autorisiert Modelle; **keine Eskalation durch
Text**; **bounded monotone Escalation**; der Router gibt **nur eine Modell-Identität**
zurück (keine Tool-Rechte, keine Budget-/Ceiling-Änderung, kein Fallback).

---

## 1. Analyse-Antworten A–G (read-only, unabhängig verifiziert)

### A — Hard verdrahtete Modell-Identitäten  ✅ BESTÄTIGT
`argent_core/routing.py` (`_CANONICAL`, `resolve_model`, `validate_model_choice`) und
`supervisor._capability_for`/`_thinking_tier`/`AGENT_IDS` waren E1-Zustand. E2 führt
**keinen** neuen hard-verdrahteten Provider-Zweig ein: `ModelRouter` arbeitet
provider-neutral über `ModelDescriptor`/`CapabilityRequirements`, das **einzige**
policy-seitige Bindeglied ist `allowed_models` je Profil (Daten, kein Code-Branch).
Der **Legacy-Pfad** `routing.resolve_model`/`validate_model_choice` bleibt für
Tests/Legacy-Inventar unverändert erhalten (§30).

### B — Reine Config vs. echte Architektur-Kopplung  ✅ BESTÄTIGT
- **Reine Config:** `openclaw.json` Agent-/Provider-Modelllisten, Kosten, AGENTS.md-Policy.
- **Echte Code-Kopplung (E2 neu):** `model_router.py` (Router), `routing_policy_v1.json`
  (Policy), `supervisor._build_routing_request/_build_routing_evidence/
  _current_escalation_level/_routing_engine` (Evidence-Assemblierung), `core.create_dispatch`
  (Decision als Identitätsautorität) + `bind_spawn_result` (Router-Dispatch von Rollen-Policy
  befreit, Exakt-Equality bleibt), Store (Schema 13→14).

### C — Zum Vergleich nötig  ✅ UMGESETZT
Capability-Tags, Reasoning-Level, Context/Output-Limits, Tool-Capabilities (getrennt von
Permissions), Kosten-/Latenz-/Reliability-Klassen, Availability/Lifecycle, Independence —
bereits in E1 als `ClaimProvenance(source, benchmarked=False)`. E2 **ergänzt** die
Routing-Seite: `RoutingEvidence`/`AttemptEvidence` (bounded C-Felder), `RoutingRequest`/
`RoutingDecision` (versioniert, canonical sha256), bounded `RoutingReasonCode`.

### D — Lokal trusted  ✅ UMGESETZT
Versionierte Repo-Dateien `argent_core/registry/*.json` (Registry aus E1 + neue
`routing_policy_v1.json`), checked-in, read-only. Claims mit `source` + `benchmarked:false`.
Der Router lädt **nur** Registry + Policy; keine Runtime-Polling, kein Netz, kein Shell.

### E — Agenten dürfen NUR empfehlen  ✅ UMGESETZT
Der Router hat **keinen** Text-Eingang (§12). `RoutingEvidence` enthält ausschließlich
bounded C-Felder (prior_attempts, test_results, reviewer_verdicts, open_findings_count,
confirmed_finding, security_relevant, concurrency_relevant). Agent-Prosa kann weder Level
heben, Modell erzwingen, Provider aktivieren noch `benchmarked` ändern.

### F — Nutzbare bestehende Schnittstellen ohne E3-Vorwegnahme  ✅ GENUTZT
- `core.create_dispatch(..., routing_decision=...)` = der Identitäts-Autoritäts-Punkt
  (Decision überschreibt die Rollen-Policy-Prüfung; Registry-Identität bleibt validiert).
- `supervisor._close_job(..., "BLOCKED", ...)` = der **bestehende** terminale Mechanismus
  für OWNER-Gate/No-Candidate (keine neue Gate-Infrastruktur, §24).
- Store `SCHEMA_VERSION` 13 → **14** (additive Spalten + INSERT-only `routing_decisions`).
- `routing.resolve_model`/`validate_model_choice` unverändert (Legacy-Inventar).

### G — Provider-/Modell-Failures vs. Code/Context/Resource  ✅ UMGESETZT
`classify_attempt` bildet `error_class` **deterministisch** ab: `EXTERNAL/PROVIDER/
TRANSIENT` → **nie** `CAPABILITY` (§14/§15); `RESOURCE/CONTEXT/SECURITY/OWNER_REQUIRED`
→ eigene Klassen; `DETERMINISTIC` → `CAPABILITY`; ein **konsumierter** Lauf mit
Code-Folgefehler (Tests rot / Reviewer-Reject) → `CAPABILITY`. Provider/Transport-Signale
routen über die bestehenden WAIT/Backoff-Pfade, **nie** über Capability-Escalation.

---

## 2. Design-Summary

### 2.1 Module

| Datei | Rolle |
|---|---|
| `argent_core/model_router.py` (neu, ~1100 Z.) | Deterministic Router: `RoutingRequest`/`RoutingDecision`/`RoutingEvidence`/`AttemptEvidence`, `RoutingPolicy`, `ModelRouter.route`, Trigger-/Level-Logik, Evidence-Klassifikation (`thinking_to_reasoning`, `classify_attempt`). Rein, kein LLM/Shell/Netz. |
| `argent_core/registry/routing_policy_v1.json` (neu) | Versionierte Bootstrap-Policy (Escalation, Reasoning-Defaults/Ceilings, Level-Min-Tiers, Modell-Tiers, Profile + Escalation-Profile, Cost/Latency-Order). |
| `argent_core/models.py` | `AgentDispatch` + `routing_decision_id`/`escalation_level`/`routing_reason_code`. |
| `argent_core/store.py` | `SCHEMA_VERSION` 13→14; additive Dispatch-Spalten + Migration; neue INSERT-only Tabelle `routing_decisions`; `_insert_routing_decision`/`get_routing_decision`/`list_routing_decisions`; `_insert_dispatch`/`_row_to_dispatch` erweitert. |
| `argent_core/core.py` | `create_dispatch(routing_decision=...)` (Decision = Identitätsautorität); `routing_decisions`-Insert in `work()`; `bind_spawn_result` (Router-Dispatch von Rollen-Policy befreit). |
| `argent_core/supervisor.py` | `_perform_create_dispatch` berechnet Decision via Router; terminal → `_close_job BLOCKED`; `_routing_engine`/`_build_routing_request`/`_build_routing_evidence`/`_current_escalation_level`; Router-Injection. |

### 2.2 Escalation-Ladder (bounded 0–4, §10/§24)

`ROUTINE(0) → IMPLEMENTATION(1) → DEEP_REASONING(2) → MAX_APPROVED(3) → OWNER(4)`.
`max_automatic_level = 3`; `owner_level = 4`. Ein Level > 3 (oder ein expliziter
OWNER-Gate/No-Candidate) ergibt eine **terminale** Decision (`is_terminal`) → der
Supervisor schließt den Job `BLOCKED` (keine neue Gate-Infrastruktur, kein Loop).

### 2.3 Objective Trigger (§11, evidence-only, kein Text)

`detect_triggers` leitet aus `RoutingEvidence` ab:
`REPEATED_FIX_FAILURE` (≥2 **distinkte** Capability-Attempts, §13 kein roher Zähler),
`TESTS_STILL_RED`, `ROOT_CAUSE_UNPROVEN`, `REVIEWER_REJECTED_CANDIDATE`,
`CONTRADICTORY_EVIDENCE`, `SECURITY_COMPLEXITY`, `CONCURRENCY_COMPLEXITY`,
`PROVIDER_FAILURE` (non-capability). Das Tripel `{REPEATED_FIX_FAILURE, TESTS_STILL_RED,
ROOT_CAUSE_UNPROVEN}` oder `SECURITY_COMPLEXITY` setzt einen **DEEP_REASONING-Floor**
(direkter Tier-Sprung, kein blindes lineares Durchprobieren); ein persistierender Root
Cause **auf** Level ≥2 eskaliert genau einen Schritt (Sol → Sol HIGH), nie ein Loop.

### 2.4 Eligibility (§6) + Minimum-Sufficient-Ranking (§8) + Reasoning (§9)

`_eligible_candidates`: Registry valid+enabled+available ∧ Bootstrap-Policy-
`allowed_models` ∧ Tier-Floor ∧ Capability-Floor ∧ Reasoning-Floor ∧ Independence.
Ranking: `tier → cost_rank → latency_rank → model_id` (deterministischer Tiebreaker,
§20). Reasoning-Level = Policy-Default des Levels, geklemmt auf [Floor, Ceiling].

### 2.5 Independence (§17/G)

Closing-Review (`reviewer`) verlangt **immer** `DIFFERENT_MODEL_REQUIRED` gegen das
Writer-Modell (Fail-closed: kein Kandidat → terminal `NO_ELIGIBLE_CANDIDATE` → BLOCKED).
Harte Independence ohne Referenzmodell → fail-closed.

### 2.6 Persistenz/Restart (§21/§22/CASE 14)

`routing_decisions` INSERT-only (decision_id = sha256 der kanonischen Payload).
`agent_dispatches` trägt `routing_decision_id`/`escalation_level`/`routing_reason_code`
denormalisiert. `_current_escalation_level` = max der persistierten Level je Rolle ⇒
Reopen setzt auf erreichtem Level fort (kein Reset auf 0).

---

## 3. Legacy-Model-Selection-Inventory (Verbleib)

- `routing.resolve_model(role, risk_class)` → **unverändert**, weiterhin der Legacy-
  Pfad für `create_dispatch(model_choice=None)` **ohne** `routing_decision` (nur
  Tests/Legacy-Inventar; produktiver Supervisor nutzt immer den Router, §30).
- `routing.validate_model_choice(...)` → **unverändert**, weiterhin die Rollen-Policy-
  Prüfung im Legacy-Pfad und in `bind_spawn_result` für **nicht**-Router-Dispatches.
  Router-autorisierte Dispatches (`routing_decision_id` gesetzt) sind davon befreit
  (der Router autorisiert die Identität), die **Exakt-Equality** (expected vs actual)
  bleibt bindend.
- `supervisor._capability_for` / `_thinking_tier` / ContextBudgets → **unverändert**
  (D-Budget/Context sind nicht Router-Sache, §25–§27).

---

## 4. Bekannte Grenzen / dokumentierte Entscheidungen

- **Live-Agent-Modellbindung:** `openclaw.json` ist unverändert; die reale Bindung eines
  `agent_id` an provider/model/tier ist Ops-Thema **außerhalb** dieses Repos. E2 liefert
  die *Entscheidung* (provider/model/reasoning), nicht die Ausführungskonfiguration.
- **Reservierte Reason-Codes:** `MODEL_FAILURE`, `UNEXPECTED_SCOPE_GROWTH`,
  `RECOVERY_COMPLEXITY`, `CAPABILITY_FLOOR_UNMET`, `MISSING_REQUIRED_EXTERNAL_INFO` sind
  bounded im Enum vorhanden; die Bootstrap-Evidence-Struktur trägt (noch) keine Felder,
  sie daraus abzuleiten (Scope-Growth/Recovery-Komplexität/Missing-External-Info), bzw.
  sie sind strukturell abgedeckt (Capability-Floor-Unmet = Eligibility-Filter →
  `NO_ELIGIBLE_CANDIDATE`; Model-Failure ohne Transport-Signal ist bereits `CAPABILITY`).
  Dies ist **keine** Abweichung vom Scope, sondern die dokumentierte Bootstrap-Grenze
  (E3/reichere Evidence-Struktur später).
- **`security_review` Rolle-Adaption (§18):** die Security-Escalation **ersetzt** die
  Capability-Anforderungen (nicht merged), symmetrisch zu `root_cause_analysis` — ein
  Security-Review verschiebt die Aufgabennatur von „Code schreiben" zu „Security reviewen"
  (kein einzelnes Modell kann beides). Dies war ein Teilstand-Bug (Merge → kein Kandidat).
- **`classify_attempt` Task-Level-Vereinfachung:** `tests_red`/`reviewer_rejected`/
  `error_class` sind Task-/Job-Level-Signale, die für jeden früheren Attempt konsistent
  verwendet werden; die Klassifikation ist deterministisch und provider/transport-safe.

---

## 5. Verifikationsergebnis (Writer)

- **E2-Tests: 50 grün** — `test_phase_e2_router.py` (40: UNIT/COMPONENT Router-Semantik)
  + `test_phase_e2_integration.py` (10: INTEGRATED Supervisor-Pfad, CASE 1–15 zentral).
- **E1-Subset:** `test_phase_e1_*` → **82 grün**.
- **D-Subset:** `test_phase_d1/d2/d3` → **243 grün**.
- **C-Subset:** `test_phase_c1/c2/c3` → **296 grün**.
- **B-Subset:** `test_phase_b1/b2/b3/b4` → **166 grün**.
- **Full Suite** (`tests/`): **2093 grün** (2043 Basis + 50 E2).
- `git diff --check`: **sauber**. `grep shell=True argent_core/` (non-test): **keine**.
- Genau zwei bestehende Schema-Versions-Tests angepasst (`13` → `14`; legitim, da E2 das
  Schema additiv auf 14 hebt).

---

## 6. NICHT implementiert (explizit) / No-Overengineering-Statement

- E3 (Benchmarks), neue Provider, Credentials, Background-Service, Parallelisierung,
  Secret-Handling — **alle nicht implementiert**.
- Keine `openclaw.json`-/Agent-Config-Änderung; keine neue Gate-Infrastruktur (OWNER/
  BLOCKED nutzt den bestehenden `_close_job`-Mechanismus); keine Budget-/Resource-/
  Permission-Wirkung im `RoutingDecision` (§16/§25–§27); keine Ceiling-Änderung.
- Kein Fallback im Router, keine Kostensortierung über den Floor hinaus (Cost/Latency nur
  als deterministische Tiebreaker), kein Runtime-Polling.

---

## 7. Fix-Round (unabhängiger Sol-Closing-Review, 6 Findings F1–F6)

Der unabhängige Sol-Closing-Review lieferte NO-GO mit 6 Findings; der Supervisor hat alle
6 im Code bestätigt. Dieser Abschnitt dokumentiert die gebündelten Fixes (kein zweiter
Sol-Review, keine Scope-Erweiterung, keine E3-Vorwegnahme; kein Commit — Worktree dirty).

### 7.1 F1 (HIGH) — Closing-Review-Independence immer bindend
- `supervisor._build_routing_request`: für `Role.REVIEWER` wird `DIFFERENT_MODEL_REQUIRED`
  **immer** gesetzt (nicht nur bei vorhandener Writer-Referenz). Fehlt die taskgebundene
  Writer-Referenz (`writer_dispatch_id` fehlt / Dispatch fehlt / kein Modell), bleibt
  `reference_model_id=None` → Router fail-closed (`NO_ELIGIBLE_CANDIDATE`) → BLOCKED. Nie
  Same-Model-Fallback.
- Policy: `reviewer`-Profil trägt `"independence": "DIFFERENT_MODEL_REQUIRED"` (Policy-Default).
- `model_router.route`: für `Role.REVIEWER` wird die Independence hart auf
  `DIFFERENT_MODEL_REQUIRED` erzwungen (der Request kann sie nicht abschwächen).
- **Test-Infra:** die Offline-Harnesse (`drive_frontier`/`_drive_to_role_dispatch` in
  `test_phase2c_supervisor.py` + Kopien in `test_phase3a_{delivery,notifications}.py` und
  `AutoRunStatusProvider.observe`) binden die Writer-Referenz (letzter Implementer-Dispatch)
  vor dem Reviewer-Schritt — die echte Writer-Bindung ist ein externes (B3/I-)Thema.

### 7.2 F2 (HIGH) — Provenienztrennung (Agent-Output ≠ Eskalationsautorität)
- **Kanonische Reviewer-Verdicts:** `core._canonical_review_verdict` bildet die freie
  `recommendation` auf `approve`/`reject` ab (fail-closed: unbekannt → `reject`); der
  Freitext landet nur im untrusted `detail`. `record_review` (Controller) kanonisiert
  ebenfalls. Router + Supervisor werten nur `approve`/`reject` aus.
- **`source_class` (nullable, controller/agent)** auf `findings`/`test_runs`/`reviews`
  (SCHEMA 14→15, additiv). Controller-APIs (`add_finding`/`record_test_run`/
  `record_review`) schreiben `controller`; `_apply_role_effects` (Agent-Output) schreibt
  `agent`.
- **Router-Evidence:** `_build_routing_evidence` nutzt für `test_results` und `findings`
  **nur** `source_class == controller`; `security_relevant` nur aus Controller-Fakten
  (`risk_class==HIGH` ODER Escalation-Level ≥2 ODER controller-bestätigtes high/critical-
  Finding). Agent-Severity-Claims beeinflussen Routing nie.

### 7.3 F3 (HIGH) — Rollen-angemessene Adaption statt pauschalem Replace+Intersect
- `security_review`-Escalation-Profil ist `roles: ["reviewer"]`-scoped (REPLACE nur im
  Reviewer-/Closing-Review-Kontext); `root_cause_analysis` ist `["implementer","qa"]`-scoped.
- Neues `deep_analysis`-Profil (`roles: ["analyst"]`, `REPOSITORY_REASONING`+Sol) autorisiert
  den Analysten auf HIGH-risk zu Sol **ohne** SECURITY_REVIEW zu vergeben (§10 „Analyst Pro/Sol").
- `analyst.allowed_models` = `["deepseek-v4-pro", "gpt-5.6-sol"]` (Sol als dokumentierte
  Escalation); Minimum-Sufficient-Ranking wählt pro bei Level 1, Sol bei Level 2.
- SECURITY_COMPLEXITY-Level-Floor ist rollenabhängig (`_SECURITY_DEEP_REASONING_ROLES` =
  analyst/reviewer/lead); Implementer/QA behalten ihre Implementer-Capability und springen
  nicht (die separate Closing-Review trägt den Security-Review).

### 7.4 F4 (MEDIUM) — Evidenz an Attempt/Cycle binden
- `attempt_outcome` (nullable) auf `agent_dispatches`; der Controller persistiert die
  Outcome-Klasse beim Abschluss (Supervisor `_perform_record_test_result` aus dem
  RUN_SANDBOX_TESTS-Ergebnis; Core bei CONSUME→SUCCESS, FAILED→CAPABILITY).
- `_build_routing_evidence` nutzt persistierte `attempt_outcome` (Fallback `classify_attempt`
  nur für Legacy/unclassifizierte Dispatches).
- `CONTRADICTORY_EVIDENCE` nur aus gleichzeitig gültiger Evidenz des aktuellen Cycle
  (letzter Wert widerspricht dem unmittelbar vorhergehenden); „irgendwann approve + irgendwann
  reject" klebt nicht mehr.

### 7.5 F5 (MEDIUM) — Strikte Policy-Validierung
- Duplikat-averse Loader (`object_pairs_hook`); vollständige Key-Allowlists inkl. innerer
  dicts (`level_names`/`level_defaults`/`level_ceilings`/`level_min_tiers` exakte Level-Keys);
  Rollen gegen Role-Enum + Duplikat-Abwehr; `bootstrap==true` UND
  `benchmark_required_for_new_models==true` erzwungen; monotone/kontiguierliche Tiers;
  Profil-Floor > Level-Ceiling → Ablehnung (kein Clamp); Registry-Kreuzvalidierung
  (`RoutingPolicy.validate_models` gegen E1-Registry, aufgerufen in `get_default_policy()`).

### 7.6 F6 (MEDIUM) — Decision-Audit vollständig kontextgebunden
- Canonical-Payload enthält jetzt `task_id`, `reference_model_id`, `independence_requirement`
  und `evidence_refs` → `decision_id`/`sha256` vollständig kontextgebunden (gleiche Modellwahl
  bei verschiedener Task-Evidence ⇒ verschiedene decision_ids).
- `store._insert_routing_decision`: bei `decision_id`-Konflikt wird die vorhandene Row auf
  vollständige Gleichheit (inkl. `decision_sha256`) geprüft; Abweichung ⇒ `DispatchError`
  statt blindem INSERT OR IGNORE.
- `core.create_dispatch`: `routing_decision` muss echte `RoutingDecision`-Instanz sein,
  `model_choice`+`routing_decision` gleichzeitig ⇒ `RolePolicyViolation`, SHA im Core neu
  berechnet und verglichen, task/role/policy/level/reason Konsistenz geprüft
  (`_validate_routing_decision`); Supervisor prüft `decision.job_id == job["id"]`.

### 7.7 Geänderte Dateien (git status --porcelain)
- `argent_core/{core,models,store,supervisor,model_router}.py`,
  `argent_core/registry/routing_policy_v1.json`.
- Tests: `test_phase_e2_router.py`, `test_phase_e2_integration.py` (Helper: controller-
  `source_class`), `test_phase_e2_fix_round.py` (neu, 26), `mock_supervisor_runtime.py`,
  `test_phase2c_supervisor.py`, `test_phase3a_delivery.py`, `test_phase3a_notifications.py`
  (Writer-Bindung), `test_phase3c_approval_core.py` + `test_phase_d3_regression.py`
  (SCHEMA 14→15).
- Docs: `PHASE_E2_NOTES.md`, `PHASE_E2_ACCEPTANCE.md`.

### 7.8 Offene Punkte: **0** (kein Commit/Push; Worktree bleibt dirty).
