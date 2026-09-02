# PHASE E3 NOTES — Versioned Evidence + Validated Fallback + Provenance

**Branch:** `phase-e3-benchmarks-validated-fallback` (Base `f546b68` = E2 GREEN, verifiziert clean).
**Writer:** DeepSeek Pro (einziger E3-Writer). **Kein Commit** — Worktree bleibt dirty (Supervisor committet nach Sol-Review).

E3 schließt Phase E additiv um die E2-Architektur herum. **Kein Redesign des E2-Routers**:
E2-Eligibility/Floor/Ranking, Escalation-Ladder, E1-Registry-Semantik, D-Budgets, C-Ceilings,
Rollen-Permissions und OpenClaw-Config bleiben unverändert.

---

## 0. Fix-Round F1–F4 (Sol-Review-Findings, gebündelt umgesetzt)

Der erste E3-Candidate hatte vier bestätigte Schwächen. Diese Fix-Round schließt sie
gebündelt (kein Blind-Neustart, Worktree-Identität `f546b68` unverändert, kein Commit).

### F1 (HIGH) — NO_VALID_FALLBACK-Backoff strandete geleaste Jobs nicht mehr

`_provider_unavailable_backoff` schrieb `status=BACKOFF + owner=NULL + lease=NULL` **ohne**
`primary_state`-Transition → geleaster Job blieb RUNNING+BACKOFF+owner-NULL+lease-NULL
(weder claimbar noch recoverbar). **Fix:** Umstellung auf das `_backoff_decision`-Muster:
atomarer `_transition_job(to_primary_state=QUEUED, to_status=BACKOFF)` mit
`queue_reason=RETRY_BACKOFF` + `next_eligible_at`/`next_wake_at` + Lease-Release
(`owner_instance_id=None`, `lease_expires_at=None`) + Holder-CAS-Fence. Der
PERSISTENT_ERROR-Zweig (retry ≥ `MAX_RUNTIME_UNKNOWN`) bleibt konsistent mit
`_persist_error` (sticky `status=ERROR`, kein RUNNING-Claim-Corpse).

### F2 (HIGH) — echter Provider-Availability-Producer + Registry-Baseline + TTL

- **(a) Producer:** `core.mark_agent_failed` akzeptiert jetzt `error_class`/`error_code`
  (bounded) und klassifiziert über den **gemeinsamen** `model_router.classify_attempt`
  (der um `error_code` erweitert wurde): `PROVIDER_UNAVAILABLE`/`MODEL_UNAVAILABLE` →
  `ATTEMPT_OUTCOME_PROVIDER`, `RATE_LIMIT`/`REQUEST_TIMEOUT` → `TRANSIENT`, sonst
  `CAPABILITY`. Der Supervisor threadet einen bounded `data.errorCode` aus dem
  Trajectory-Terminalrow über `RunObservation.error_code` → `_perform_mark_run_failed` →
  `mark_agent_failed` — der **reale** Completion-/Konsum-Pfad persistiert damit
  `ATTEMPT_OUTCOME_PROVIDER` (kein pauschales CAPABILITY, E2-Distinktion bleibt).
- **(b) Registry-Baseline:** `_eligible_candidates` filtert nur noch disabled/unknown
  (nicht mehr Registry-UNAVAILABLE); `_effective_availability` startet am
  Registry-Default (`provider.availability_state`) und der Snapshot kann nur **senken**
  (`_worse_availability`). Registry-UNAVAILABLE → Fallback-Suche über die übrigen validen
  Modelle; erst leere valide Menge → WAIT/Backoff/BLOCKED (CASE 5/7/24).
- **(c) Bounded TTL:** `_build_availability_snapshot` wertet nur noch die **jüngste**
  Observation pro Modell innerhalb `AVAILABILITY_OBSERVATION_TTL_SECONDS` (1800s) aus;
  Ablauf oder spätere AVAILABLE/SUCCESS-Observation hebt UNAVAILABLE auf (kein
  dauerhaftes Poisoning).

### F3 (HIGH) — vollständig reproduzierbare, an den Core-Trust-Boundary gebundene Provenienz

- **Policy-Version 2** (`ROUTING_POLICY_VERSION="2"`, `routing_policy_v1.json`
  `policy_version:"2"`) **plus** unveränderliche Inhaltsdigests der drei Dokumente
  `policy_hash`/`registry_hash`/`evidence_hash` (sha256 des kanonischen Dateiinhalts)
  als eigene Decision-Felder + `routing_decisions`-Spalten (additiv).
- **Vollständiger bounded Input-Kanon** für `inputs_hash`: die drei Inhaltsdigests +
  Versionslabels + `current_escalation_level` + Requirements-Kanon + `evidence_minimum_status`
  + **vollständiges** `RoutingEvidence.canonical()` + `evidence_gate` (genutzte
  Evidence-Einträge je Kandidat/Floor) + Availability-Snapshot.
- **`inputs_hash` ist Teil der Decision-Bindung**: `inputs_hash` steht in
  `canonical_json` und damit in `decision_id`/`sha256` — jede Input-Änderung ändert die
  Provenienz.
- **Core-Validierung** (`_validate_routing_decision`) bindet **alle** Decision-Felder
  gegen `canonical_json`, verlangt `inputs_hash`/Content-Digests als echte 64-hex und
  recomputet sha256/decision_id. Ein manipulierter (aber wohlgeformter) `inputs_hash` wird
  abgelehnt.

### F4 (MEDIUM) — Evidence-Registry fail-closed bei mehrdeutigem JSON/Entry-Version

`load_files` nutzt jetzt `object_pairs_hook=_no_duplicate_keys` (Duplikat-Keys auf
Dokument- **und** Entry-Ebene → Fehler). `entry.version` muss strikt ==
Dokument-`evidence_version` sein (kein „banana“).

---

## 1. Read-only Analyse der E3-Basis (verifiziert)

- **E2-Stand `f546b68`**: `model_router.py` (1424 Z.) mit `RoutingReasonCode` (inkl.
  `PROVIDER_FAILURE`), `_ATTEMPT_OUTCOMES`, `thinking_to_reasoning`/`classify_attempt`
  (Provider ≠ Capability hart getrennt), `RoutingError`, `AttemptEvidence/RoutingEvidence/
  RoutingRequest/RoutingDecision` (sha256, `is_terminal`, `thinking_tier`), `RoutingPolicy`
  (strikte Validierung inkl. Duplikate, Key-Allowlists, Role-Enum,
  `bootstrap`+`benchmark_required_for_new_models==true`, Registry-Kreuzvalidierung,
  monotone Tiers, Floor>Ceiling→Reject), `load_routing_policy`/`get_default_policy`,
  `detect_triggers`, `ModelRouter.route` (Eligibility→Minimum-Sufficient→Reasoning-Level,
  **keine** Fallback-Auswahl), `_canonical_json`.
- **registry/**: `models.json`+`providers.json` (E1; availability statisch AVAILABLE;
  provenance `benchmarked:false`) + `routing_policy_v1.json` (E2; **keine**
  fallback-/evidence-/benchmark-/provider_failure-Sektionen; profiles lead/analyst/
  implementer/qa/reviewer + escalation_profiles security_review/deep_analysis/
  root_cause_analysis).
- **supervisor.py**: `_perform_create_dispatch` baut `RoutingRequest` aus trusted Evidenz
  (Dispatcher-History mit `attempt_outcome`, kanonische Review-Verdicts, source_class
  controller/agent, security_relevant nur Controller-Fakten), `router.route(...)` → terminal
  → bestehender BLOCKED-Pfad. `core.create_dispatch` validiert die Decision (isinstance,
  SHA-Recompute, job/task/role/policy/level/reason-Konsistenz) und persistiert in
  `routing_decisions` (INSERT-only) + `agent_dispatches`.
- **store.py**: `SCHEMA_VERSION "15"` (additiv: `routing_decisions`, `source_class`,
  `attempt_outcome`/Routing-Felder); Migrationsmuster etabliert.
- **Verfügbarkeits-Snapshot**: existierte als eigenständiger Router-Input **noch nicht**.
- **Fallback-Kandidaten (Bestandsdaten)**: implementer LOW (Floor `CODE_IMPLEMENTATION`;
  allowed flash+pro+sol) → flash primary, pro = valider Fallback (beide deepseek, daher
  **model-level** Unavailability nötig). reviewer/security (Floor `SECURITY_REVIEW`+…;
  allowed sol) → sol unavailable → **kein** valider Fallback → fail-closed (CASE 6/7/24).
  pro hat kein COORDINATION-Tag, sol kein CODE_IMPLEMENTATION-Tag — Floor-Kreuzungen geprüft,
  keine Tags erfunden.

---

## 2. Design-Entscheidungen

### 2.1 Evidence-Modell (A) — `argent_core/evidence_registry.py` + `benchmarks_v1.json`

- Bounded `EvidenceStatus`: `VERIFIED/PROVISIONAL/UNKNOWN/REJECTED` (Rank nur für
  Minimum-Vergleich). `VERIFIED` wird in Registry-Version 1 **beim Laden abgelehnt**
  (keine echten Benchmarks → nichts als VERIFIED ausweisbar).
- Bounded `EvidenceCategory` (task-relevant, keine Einheits-Score):
  `coordination_basic_reasoning`, `repository_coding`, `debugging_root_cause`,
  `architecture`, `security_review`, `tool_agent`, `long_context`.
- Deterministische `Capability→Category`-Abbildung (`_CAPABILITY_TO_CATEGORY`) — die
  einzige strukturelle Klammer zwischen E1-Vokabular und E3-Evidence, hartcodiert.
- Registries sind immutable, **fail-closed** beim Laden: unbekannte Felder, Duplikat-IDs
  (Modell bzw. Modell+Kategorie), invalide Status/Kategorie, unbekannte Modell-Refs
  (Kreuzvalidierung gegen E1-Registry), `benchmarked:true`, Agent-Origin-Refs → Fehler.
- Bestandsmodelle ehrlich: **PROVISIONAL** (Claims aus `routing.py`/`architecture`/
  `local-config`, `benchmarked:false` dokumentiert); fehlende Kategorie = **UNKNOWN**.
  Nichts VERIFIED, keine Scores.

### 2.2 Policy-Erweiterung (B) — `routing_policy_v1.json` (Version **2** nach Fix-Round F3)

- Neue optionale Sektionen (additiv, ohne Semantikbruch; fehlend ⇒ Default):
  - `evidence_requirements.minimum_status` (Default `PROVISIONAL`; `UNKNOWN/REJECTED`
    als Minimum werden als Policy-Fehler abgelehnt — das Gate wäre sonst leer).
  - `fallback.enabled` (Default `false` — Fallback NUR wenn Policy es explizit erlaubt),
    `fallback.trigger_states` (Default `["UNAVAILABLE"]`), `allow_rate_limit_fallback`
    (Default `false`; dokumentiert, nicht aktiv genutzt).
- **Fix-Round F3:** `policy_version` wurde von „1“ auf „2“ erhöht (die Evidence-/Fallback-
  Erweiterung ist eine Inhaltsänderung der Policy und wird versioniert sichtbar), zusätzlich
  zum unveränderlichen `content_hash`. Der E2-Assert `pol.version == "1"` wurde auf „2“
  umgestellt (dokumentierte Einzeländerung; die E2-76 bleiben grün).
- Validierung auf die neuen Sektionen ausgedehnt (`_EVIDENCE_REQUIREMENTS_KEYS`,
  `_FALLBACK_KEYS`), fail-closed beim Laden.

### 2.3 Validated Fallback (C) — deterministisch im Router

- Filter-Zuerst, Ranking-Danach: **FILTER** (Floor/Reasoning/Context/Tools/Policy-Allowlist/
  Independence/**Evidence**) erzeugt die Überlebendenmenge; **DANN** Ranking (Minimum-Sufficient;
  Kosten/Latenz nur Tiebreaker).
- `AvailabilitySnapshot` (bounded Router-Input): `provider_states`/`model_states` als
  Override über den Registry-Default; nur `AvailabilityState`-Werte; max. Einträge begrenzt.
- Fallback-Algorithmus (`_select`): primary = Rang-0. Ist primary *unusable* (Snapshot
  ≠ AVAILABLE/DEGRADED) **und** `fallback.enabled` **und** State ∈ `trigger_states` →
  nächster *usable* Kandidat aus derselben gefilterten Menge (≠ primary; Floor/Policy/
  Independence/Evidence bereits erfüllt). Kein Fallback-Kandidat → terminal
  `NO_VALID_FALLBACK`.
- **Invarianten**: Fallback erhöht/senkt das Escalation-Level **nie**; löst **nur** bei
  Provider-/Model-Verfügbarkeitsproblemen aus (nie bei Code-Failures/schlechtem Output/
  unbekanntem Root-Cause/Security-Review-Reject); nie ein stiller schwächerer Ersatz.
- **Provider-Failure ≠ Capability-Failure** bleibt gewahrt: TRANSIENT/EXTERNAL (Netz/
  Rate-Limit) laufen über die bestehenden WAIT/Backoff-Pfade und erzeugen **keinen**
  Snapshot-Eintrag (→ kein Fallback).

### 2.4 Provenienz (D) — `RoutingDecision` + `routing_decisions` (Schema 16, additiv erweitert)

- Neue Decision-Felder: `registry_version`, `evidence_version`, `policy_hash`,
  `registry_hash`, `evidence_hash`, `inputs_hash`.
- `inputs_hash` = canonical sha256 des **vollständigen** bounded Input-Kanons (drei
  Inhaltsdigests + Versionslabels + `current_escalation_level` + Requirements-Kanon +
  `evidence_minimum_status` + `RoutingEvidence.canonical()` + `evidence_gate` +
  Availability-Snapshot).
- `canonical_json` (und damit `sha256`/`decision_id`) enthält `policy_hash`/`registry_hash`/
  `evidence_hash` **und** `inputs_hash` — jede Input-/Dokument-Änderung ist in der
  persistierten Provenienz sichtbar (CASE 16/17).
- Schema: drei weitere additive Spalten auf `routing_decisions` (`policy_hash`,
  `registry_hash`, `evidence_hash`) nach Bestandsmuster, idempotente Migration. **Kein
  Schema-Bump nötig** (die `routing_decisions`-Tabelle selbst ist neu in Schema 16 und
  noch nicht versandt; die Spalten sind additiv und die Migration idempotent).
- Core-Validierung bindet alle Felder gegen `canonical_json` und verlangt 64-hex-Digests.

### 2.5 Supervisor-Mapping (C) — kleinste sichere Zuordnung

- `_build_availability_snapshot`: Registry-Default + beobachtete Abweichung **mit bounded
  TTL** — die jüngste Observation pro Modell innerhalb `AVAILABILITY_OBSERVATION_TTL_SECONDS`
  zählt; `attempt_outcome == PROVIDER` markiert das Modell `UNAVAILABLE`, eine spätere
  SUCCESS-Observation oder Ablauf hebt die Marke auf. Transient/External werden **nicht**
  markiert.
- `_perform_create_dispatch`: `NO_VALID_FALLBACK` → **bounded Backoff (WAIT)** über den
  bestehenden Retry-Budget (`retry_count++`, `backoff_seconds`, nach `MAX_RUNTIME_UNKNOWN`
  sticky `PERSISTENT_ERROR`). Der Backoff läuft jetzt über `_transition_job` (QUEUED+
  BACKOFF+Lease-Release, F1). `NO_ELIGIBLE_CANDIDATE` (Floor/Evidence) und `OWNER_GATE`
  bleiben BLOCKED.

### 2.6 Betriebsgrenze (dokumentiert)

- Argent-Routing-Entscheidung ≠ OpenClaw-Provider-Konfiguration. Die Registry ist eine
  Architektur-Abstraktion, **kein** Call-Recht; kein Live-Provider wird aktiviert, kein
  Provider/Modell wird in der Live-Config neu angelegt.

---

## 3. Bekannte Grenzen (dokumentierte Design-Grenzen, keine offenen Punkte)

- **Kein echtes Benchmark-Laufwerk**: Evidence bleibt PROVISIONAL/UNKNOWN; VERIFIED ist
  strukturell vorbereitet, aber in Version 1 beim Laden abgelehnt.
- **Snapshot-Builder (Bootstrap)** synthetisiert model-level Unavailability aus der
  **jüngsten** `attempt_outcome == PROVIDER`-Observation im bounded TTL-Fenster (F2);
  provider-weite Unavailability ist über `provider_states` ausdrückbar (Router honoriert
  beides), wird vom Bootstrap-Builder aber nicht aus TRANSIENT/EXTERNAL synthetisiert
  (die bleiben Backoff).
- **Rate-Limit** ist kein Fallback-Trigger (läuft über WaitKind/Backoff; `error_code
  RATE_LIMIT` → `ATTEMPT_OUTCOME_TRANSIENT`); der Policy-Knopf `allow_rate_limit_fallback`
  ist dokumentiert, aber `false` und nicht aktiv genutzt.
- **Kein SAME_MODEL_ALLOWED-Fallback** bei required closing review: Independence ist im
  Filter vor dem Fallback und wird beim Fallback-Kandidaten erneut erfüllt (strukturell).
- **Synthetische DB-Injektion in `test_phase_e3_integration.py`** (`_inject_dispatch`) bleibt
  als **Unit-Fixture** für den Snapshot-Builder/Provenienz-Pfad erhalten; der reale
  Producer-Pfad ist in `tests/test_phase_e3_fix_round.py::test_f2_real_provider_producer_and_fallback`
  abgedeckt (kein DB-Injekt, `mark_agent_failed` mit Provider-Signal).

---

## 4. Dateien

| Datei | Änderung |
|---|---|
| `argent_core/evidence_registry.py` | neu — Evidence-Status/-Kategorie/-Registry (fail-closed; F4: Duplikat-Keys + strict entry.version) |
| `argent_core/registry/benchmarks_v1.json` | neu — PROVISIONAL-Evidence der drei Bestandsmodelle |
| `argent_core/model_router.py` | additiv — AvailabilitySnapshot, Fallback, Evidence-Gate, Provenienz, Policy-Sektionen; F2(b) Registry-Baseline; F3 content-digests + voller Input-Kanon + policy v2 |
| `argent_core/model_registry.py` | additiv — `content_hash` (sha256 des kanonischen Registry-Inhalts) |
| `argent_core/registry/routing_policy_v1.json` | additiv — `evidence_requirements` + `fallback`; F3: `policy_version` → „2“ |
| `argent_core/store.py` | additiv — Schema 16, `routing_decisions`-Provenienzspalten (inkl. F3 content-digests) + Migration |
| `argent_core/core.py` | additiv — Provenienz-Persistenz + Decision-Validierung; F2 producer (`mark_agent_failed` error_class/error_code); F3 Feld-Bindung |
| `argent_core/supervisor.py` | additiv — Snapshot-Builder (bounded TTL) + NO_VALID_FALLBACK→Backoff (F1 `_transition_job`) + F2 trajectory-errorCode-Threading |
| `tests/test_phase_e3_router.py` | neu — 32 UNIT/COMPONENT-Tests (CASE 17 auf echte Inhaltsänderung umgestellt) |
| `tests/test_phase_e3_integration.py` | neu — 5 INTEGRATED-Tests (CASE 21 auf vollständige Provenienz-Verifikation umgestellt) |
| `tests/test_phase_e3_fix_round.py` | neu — 13 F1–F4-Regressionstests (real-path Producer, TTL-Recovery, adversarial inputs_hash, Duplikat-Keys) |
| `tests/test_phase_e2_router.py` | Assert `pol.version` „1“→„2“ (dokumentierte Einzeländerung) |
| `tests/test_phase3c_approval_core.py`, `tests/test_phase_d3_regression.py` | Schema-Assert 15→16 (unvermeidbar) |
