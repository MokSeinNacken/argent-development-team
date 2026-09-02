# PHASE E1 NOTES — Provider/Model Abstraction + Capability Registry

**Branch:** `phase-e1-model-provider-abstraction` (Base `c93986b` = Phase-D GREEN, verifiziert clean).
**Rolle:** E1-Writer (kein Commit, kein Push — Worktree bleibt dirty; Supervisor committet nach Sol-Review).
**Datum:** 2026-09-02

---

## 0. Scope-Zusammenfassung (verbindlich)

E1 liefert die **statische Provider-/Modell-Abstraktion + Capability Registry** als
Grundlage für Phase E (Adaptive Roles / Model Routing in E2). **NICHT implementiert**
(explizit, keine Abweichung): dynamische Modell-Auswahl, Rolle→Modell-Entscheidung,
Escalation, automatische Fallback-*Ausführung*, neue Provider (Claude/GLM/Gemini/Qwen),
Credentials, Benchmarks, Background-Service, Parallelisierung, DB-Schema-Änderung,
Secret-Handling-Architektur.

**Fundamentale Invariante:** Rollen sind Fähigkeiten; Modelle sind austauschbare
Implementierungen. Kein `if provider == "deepseek"` im Core — Provider sind Daten.

---

## 1. Analyse-Antworten A–G (read-only, unabhängig verifiziert)

### A — Hart verdrahtete Modelle  ✅ BESTÄTIGT
- `argent_core/routing.py`: `_CANONICAL` (lead/reviewer→`openai/gpt-5.6-sol`/high,
  analyst/implementer/qa→`deepseek/deepseek-v4-pro`/medium, implementer/qa LOW→
  `deepseek-v4-flash`), `validate_model_choice`, `resolve_model` (exportiert via `__init__`).
- `argent_core/supervisor.py`: `AGENT_IDS` (role→argent-*), `_capability_for`
  (substring `flash`→FLASH, `sol`→SOL, sonst PRO), `_thinking_tier` (OpenAI-C1-Regel
  `openai/gpt-5.6-sol→high` vs DeepSeek `thinkLevel`), Binding-CONFLICT-Codes.
- `argent_core/context_pack.py`: `CapabilityTier` FLASH/PRO/SOL + `ContextBudgetPolicy`
  (soft/hard 4–8k/12–24k/24–48k, hard 16k/48k/96k) — Phase-D-Policy, **bleibt bindend**.
- Agent→Modell-Zuordnung real in `~/.openclaw/openclaw.json` (`agents.list` +
  `models.providers.deepseek`); `openai/gpt-5.6-sol` via Plugin (oauth).

### B — Reine Config vs. echte Architektur-Kopplung  ✅ BESTÄTIGT
- **Reine Config:** OpenClaw-Agent-Modelle, Provider-Modelllisten/Kosten, AGENTS.md-Policy-Text.
- **Echte Code-Kopplung:** `routing._CANONICAL` + `validate_model_choice`;
  `core.create_dispatch`/`bind_spawn_result`/`complete_role` (expected-vs-actual +
  Policy-Revalidation); `supervisor._capability_for`; `supervisor._thinking_tier`;
  `AgentDispatch`-Felder `expected_agent_class`/`expected_model_class`/
  `expected_thinking_tier` (Persistenz, Schema unverändert).

### C — Zum späteren Vergleich nötig  ✅ UMGESETZT
Capability-Tags, Reasoning-Level, Context-Window/Output-Limits, Tool-Capabilities
(getrennt von Permissions), Vision, Kosten-/Latenz-/Reliability-Klassen
(provider-neutral, evidence-tagged), Availability/Lifecycle, Independence-Constraints,
Fallback-Eligibility, `policy_version` + Claims-Provenienz. Keine Qualitäts-Reihung
ohne Benchmark.

### D — Lokal trusted  ✅ UMGESETZT
Versionierte Repo-Registry-Dateien (`argent_core/registry/*.json`, checked-in, **kein
Agent-Write-Pfad**) + read-only importierte lokale OpenClaw-Config-Fakten; Claims mit
`source` + `benchmarked:false`.

### E — Agenten dürfen NUR empfehlen  ✅ UMGESETZT (keine Mutations-API)
`ModelRegistry` und alle Descriptors sind `frozen` und expose ausschließlich
Read-Only-Zugriff. Es existiert **keine** Methode für: Registry-Einträge, Enable,
Claims, Kostenklassen, Floors, Reasoning-Autorisierung, Fallback-Aktivierung,
Tool-Rechte, Credential-Refs, Independence-Aus.

### F — Nutzbare bestehende Schnittstellen ohne E2-Vorwegnahme  ✅ GENUTZT
- `core.create_dispatch` (model_choice dict oder Default-Resolution) = **der** Identity-Validierungspunkt.
- `routing.resolve_model`/`validate_model_choice` (statische Kanonik, **unverändert**).
- `supervisor._build_context_pack` / `_capability_for` (D-Budget, **unverändert**).
- `AgentDispatch`-Persistenzfelder; Store `SCHEMA_VERSION` 13 (**unverändert**).

### G — Provider-/Modell-Failures vs. Code/Context/Resource  ✅ UMGESETZT
`job_state.ErrorClass` bekommt **additiv** `PROVIDER = "PROVIDER"` (Muster D's
`CONTEXT`). `ModelRegistryError` trägt bounded Codes `PROVIDER_UNAVAILABLE` /
`MODEL_UNAVAILABLE` / `MODEL_NOT_ALLOWED` / `CAPABILITY_FLOOR_UNMET` /
`MODEL_CONFIG_INVALID`; `.error_class` → `PROVIDER`. Keine automatische
CODE/RESOURCE/CONTEXT-Zuordnung. Laufzeit-Providerausfälle (Netz) bleiben job-seitig
`EXTERNAL`/`TRANSIENT` (B/C); die Registry-Seite ist statisch.

### Korrektur/Nuance zu A (relevante Abweichung von der Supervisordarstellung)
Die Formulierung „implementer/qa LOW→flash" bezieht sich auf die **Risk-Class LOW**,
nicht auf einen LOW-**Reasoning-Level**. Tatsächlich wird `deepseek-v4-flash` in
`routing.resolve_model` mit `thinking=medium` (nicht `low`) dispatched. Deshalb ist
`reasoning_levels_supported(flash) = ("MEDIUM",)`. Der bounded Enum-Wert
`ReasoningLevel.LOW` ist **reserviert/unverwendet** (kein Modell mappt aktuell darauf);
`HIGH`→sol, `MEDIUM`→pro+flash. Dies ist dokumentiert und getestet.

### Zusätzlich verifizierte Config-Fakten (nicht in A–G explizit)
`~/.openclaw/openclaw.json` enthält neben flash/pro auch `deepseek-chat`
(reasoning false, 131072/8192) und `deepseek-reasoner` (reasoning true, 131072/65536).
Diese sind **Config-Fakten**, aber **keine Rollen-Identitäten** und werden daher gemäß
Spec §19 **nicht** als Registry-Modelle aufgenommen (Registry = genau die 3
Rollen-Modelle flash/pro/sol).

---

## 2. Design-Summary

### 2.1 Module

| Datei | Rolle |
|---|---|
| `argent_core/model_registry.py` (neu) | Provider-/Modell-Abstraktion, Capability-Taxonomie, Requirements, Registry, Loader, Fehlerklassen. Rein, deterministisch, kein I/O zur Laufzeit, keine Provider-Calls. |
| `argent_core/registry/providers.json` (neu) | Versionierte Provider-Daten (deepseek, openai). |
| `argent_core/registry/models.json` (neu) | Versionierte Model-Daten (flash, pro, sol) mit Claims-Provenienz. |
| `argent_core/job_state.py` | `ErrorClass.PROVIDER` additiv. |
| `argent_core/core.py` | `Core.__init__(..., registry=None)` + `_model_registry()` + **ein** Integrationspunkt in `create_dispatch`. |
| `argent_core/__init__.py` | Public-Exports der Registry-API. |

### 2.2 Schemas (frozen dataclasses)

- **`ProviderDescriptor`**: `provider_id, provider_type, display_name, enabled,
  availability_state, capabilities_supported, credential_ref (opaque), auth_mode
  (opaque), endpoint_ref, profile_ref, policy_version`.
- **`ModelDescriptor`**: `model_id, provider_id, canonical_model_name, enabled,
  lifecycle_state, context_window_metadata, output_limit_metadata,
  reasoning_levels_supported, tool_capabilities (claims), abilities
  (vision/coding/review), latency_class, cost_class, reliability_class,
  capability_tags, policy_version, provenance (source + benchmarked=False)`.
- **`CapabilityRequirements`**: `required_capabilities, optional_capabilities,
  minimum_reasoning_level, tool_requirements, context_requirement,
  independence_requirement, quality_floor` — validierbar; E1 stellt **nur** die
  Kandidaten-Menge bereit, entscheidet nicht.
- **`ClaimProvenance`**: `source, benchmarked` (immer `False` in E1).

### 2.3 Taxonomie (bounded Enums)

- `Capability`: COORDINATION, SIMPLE_ANALYSIS, CODE_IMPLEMENTATION,
  COMPLEX_CODE_IMPLEMENTATION, DEBUGGING, REPOSITORY_REASONING, ARCHITECTURE,
  SECURITY_REVIEW, CODE_REVIEW, ROOT_CAUSE_ANALYSIS, TOOL_USE, LONG_CONTEXT, VISION,
  STRUCTURED_OUTPUT (keine Micro-Capabilities, nicht Agent-gesteuert).
- `ReasoningLevel`: LOW/MEDIUM/HIGH (LOW reserviert). `ProviderType`,
  `AvailabilityState`, `LifecycleState`, `CostClass`/`LatencyClass`/`ReliabilityClass`
  (je LOW/MEDIUM/HIGH/UNKNOWN), `Independence`, `ToolCapability`, `CodingMode`.

### 2.4 Policy (klein, nicht überkomplex)

- **Floor** = required capabilities + reasoning floor + tool requirements +
  context requirement + reliability quality floor. `UNKNOWN`-Reliability erfüllt
  eine konkrete Floor **nie** (fail-closed); eine Floor `UNKNOWN` bedeutet „keine
  Reliability-Anforderung". **Kein** Kostensortierung/Selection.
- **Independence**: `SAME_MODEL_ALLOWED` / `DIFFERENT_MODEL_REQUIRED` /
  `DIFFERENT_PROVIDER_PREFERRED` (soft hint, filtert nicht) /
  `DIFFERENT_PROVIDER_REQUIRED`.
- **Fallback-Eligibility** (`is_fallback_eligible`): reine Metadaten-/Prüffunktion
  (`enabled ∧ provider allowed ∧ floor ∧ tools/capabilities ∧ independence ∧ policy
  erlaubt Fallback`). **Keine** Fallback-Ausführung.

### 2.5 Integration (GENAU EIN Punkt)

`Core.create_dispatch` — **nach** der unveränderten `routing.validate_model_choice`-
Prüfung — validiert die aufgelöste Identität (`provider`/`model` aus `model_choice`
oder `routing.resolve_model`) gegen die Registry:
`self._model_registry().validate_identity(provider, model, thinking)`.
- Provider existiert+enabled+verfügbar, Modell existiert+enabled+`provider_id` passt →
  Dispatch wie bisher.
- Sonst fail-closed `ModelRegistryError` (bounded code), **kein** Dispatch.
- Registry injizierbar (`Core(..., registry=...)`, nur `isinstance(ModelRegistry)`,
  sonst fail-closed); Default = lazy Singleton aus den Repo-Dateien. Kanonische
  Identitäten (flash/pro/sol) laufen **exakt** wie bisher.
- **Fix-Round F7:** die Registry-Validierung liegt **innerhalb** `work()` — **nach**
  dem Idempotenz-/Existing-Dispatch-Replay und **vor** dem Insert — und gate
  damit nur **neue** Dispatches; ein idempotenter Replay desselben Dispatches wird
  auch dann unverändert zurückgegeben, wenn eine zweite Core-Instanz mit einer
  Registry gestartet wird, die das Modell inzwischen disabled hätte.
- `routing.py`, `_capability_for`, ContextBudgets, Store, `bind_spawn_result`/
  `complete_role` **unverändert**.

### 2.6 Fehlerklassen

`ModelRegistryError` (bounded codes, s. §1 G) + `ErrorClass.PROVIDER` additiv.
`registry_error_class()` ist die einzige Mapping-Stelle (Registry-Fehler → PROVIDER),
bewusst **nicht** im Scheduler-erzwungen.

---

## 3. Registry-Daten (initial, verifiziert)

| Modell | Provider | reasoning | cost_class | context | output | tags (Auszug) |
|---|---|---|---|---|---|---|
| deepseek-v4-flash | deepseek | MEDIUM | LOW (0.14/0.28) | 1 000 000 | 384 000 | COORDINATION, SIMPLE_ANALYSIS, CODE_IMPLEMENTATION, DEBUGGING, TOOL_USE, STRUCTURED_OUTPUT, LONG_CONTEXT |
| deepseek-v4-pro | deepseek | MEDIUM | MEDIUM (1.74/3.48) | 1 000 000 | 384 000 | SIMPLE_ANALYSIS, REPOSITORY_REASONING, CODE_IMPLEMENTATION, COMPLEX_CODE_IMPLEMENTATION, DEBUGGING, TOOL_USE, STRUCTURED_OUTPUT, LONG_CONTEXT |
| gpt-5.6-sol | openai | HIGH | UNKNOWN | null | null | COORDINATION, ARCHITECTURE, CODE_REVIEW, SECURITY_REVIEW, ROOT_CAUSE_ANALYSIS, REPOSITORY_REASONING, STRUCTURED_OUTPUT |

- **deepseek**: `provider_type=openai-completions`, `endpoint_ref=https://api.deepseek.com`,
  `auth_mode=api-key`, `credential_ref=openclaw:provider:deepseek` (opaque).
- **openai**: `provider_type=oauth-plugin`, `auth_mode=oauth`,
  `credential_ref=openclaw:auth:profiles:openai` (opaque). **Unbekannte Felder → null/UNKNOWN,
  nie erfunden** (kein `models.providers`-Block; Cost/Latency/Reliability/Context/Output
  von sol = UNKNOWN/null).
- `latency_class` überall `UNKNOWN` (keine verifizierten Latenzdaten), `reliability_class`
  überall `UNKNOWN` (keine Benchmark-/Policy-Evidence für einen konkreten Wert).
- Alle Claims: `benchmarked:false` (E3 benchmarkt).

---

## 4. Verifikationsergebnis (Writer)

- **E1-Tests: 82 grün** — `test_phase_e1_registry.py` (29: Matrix A–E),
  `test_phase_e1_security.py` (8: Matrix F/G/I), `test_phase_e1_dispatch.py`
  (14: Matrix H/J + Acceptance 1–10) + `test_phase_e1_fix_round.py`
  (31: Fix-Round F1–F7).
- **D-Subsets:** `test_phase_d1/d2/d3` → **243 grün**.
- **C-Subsets:** `test_phase_c1/c2/c3` → **296 grün**.
- **B-Subsets:** `test_phase_b1/b2/b3/b4` → **166 grün**.
- **Full Suite** (`tests/`): **2043 grün** (1961 vorher + 82 E1).
- `git diff --check`: **sauber**. `grep shell=True argent_core/` (non-test): **keine**.
- Genau eine bestehende Test-Erwartung angepasst (CASE 1, s. F6): der Rest der
  kanonischen Identitäten läuft durch die Registry-Prüfung unverändert hindurch.

---

## 5. Fix-Round (Supervisor-Review, F1–F7)

Nach unabhängiger Supervisor-Review bestätigte Findings wurden in dieser Fix-Round
behoben (Writer, DeepSeek Pro).  Die 51 Basis-Tests bleiben grün; genau eine
Test-Erwartung wurde angepasst (CASE 1, F6).  Je Finding: Fix + Testnachweis
(`tests/test_phase_e1_fix_round.py`, 31 Tests).

- **F1 (HIGH) — factory-only Konstruktion + fail-closed Core-Injection:**
  `ModelRegistry.__init__` erzwingt Versions-Konsistenz, Key↔Descriptor-Id-Gleichheit,
  frozen Descriptor-Instanzen und Entry-`policy_version`; interne Maps sind
  `MappingProxyType` (read-only, Mutation → `TypeError`).  `Core(registry=...)`
  akzeptiert nur `isinstance(ModelRegistry)`.  Tests: Key-Mismatch, Non-Descriptor,
  Version-Mismatch, Maps read-only, Fake-Registry in Core fail-closed, valide
  injizierte Registry ok.
- **F2 (HIGH) — Claim-Invarianten + Provider-Obergrenze:** `benchmarked is False`
  erzwungen (True → `MODEL_CONFIG_INVALID`); Provenienz-`source` bounded Allowlist
  (`_TRUSTED_SOURCE_AUTHORITIES`) + Agent-Origin-Ablehnung; Provider-
  `capabilities_supported` als Obergrenze bei Load (`Model-tags ⊆ Provider-caps`).
  Tests: benchmarked True, Agent-Origin, untrusted Source, Subset-Verletzung.
- **F3 (MEDIUM) — Schema-/Secret-Striktness:** exakte Key-Allowlists je Entry +
  Top-Level (unbekannte Keys + Secret-Key-Namen case-insensitiv reject);
  `credential_ref`/`auth_mode`/`profile_ref` Opaque-Grammatik (kein `@`/Whitespace/
  URL-Scheme); `endpoint_ref` null ODER http(s) ohne Userinfo; `load_files`
  Top-Level-Typen vor `.get()` (dict), `registry_version` UND `policy_version` beider
  Dokumente == `"1"`, Entry-`policy_version` == Dokument-`policy_version`;
  `abilities`/`provenance` ohne Truthiness-Coercion.  Tests: api_key-Key,
  Authorization, Userinfo-URL, `abilities=[]/""`, Top-Level-Liste, unbekannter
  Top-Level-Key, policy_version-Mismatch (Dokument + Entry).
- **F4 (MEDIUM) — `CapabilityRequirements` Kanonisierung:** `__post_init__` (via
  `object.__setattr__`) kanonisiert Sequenzfelder list|tuple → frozen Tupel,
  Enum-Werte → `.value`, lehnt Duplikate ab; `context_requirement` schließt `bool`
  aus; alles `ModelRegistryError(MODEL_CONFIG_INVALID)`.  Tests: bool-Reject,
  list→tuple, Enum→value, Duplikate, Non-Sequence, frozen Instance.
- **F5 (MEDIUM) — EIN kanonisches Eligibility-Prädikat:** `_candidate_eligibility`
  (enabled ∧ Provider enabled+available ∧ lifecycle ACTIVE ∧ Floor ∧ Independence ∧
  policy_allows_fallback) wird von `eligible_models` UND `is_fallback_eligible`
  genutzt; `requirements.validate()` immer zuerst; unbekanntes Referenzmodell in
  beiden Pfaden `MODEL_CONFIG_INVALID`.  Tests: RETIRED nicht fallback-eligible,
  Konsistenz, unbekanntes Referenzmodell, `policy_allows_fallback=False`.
- **F6 (HIGH) — Daten/Claims:** `models.json` flash erhält `COORDINATION` +
  `SIMPLE_ANALYSIS` (Architektur §11), Provenienz evidence-basiert (Architektur §11
  + routing.py + openclaw.json); `providers.json` `capabilities_supported` =
  Obergrenze (deepseek = Flash∪Pro, openai = Sol) + Load-Validierung; CASE 1 auf
  Spec korrigiert (`COORDINATION ∈ flash.capability_tags`); CASE 6 bleibt grün
  (SECURITY_REVIEW nur Sol).  Tests: Flash-Claims, Provider-Obergrenze, Sol-only.
- **F7 (LOW) — Registry-Validierung im Replay:** `validate_identity` liegt jetzt
  **innerhalb** `work()` (nach Idempotenz-/Existing-Dispatch-Replay, vor Insert);
  routing-role-policy-Check bleibt außen unverändert.  Test: Dispatch mit Registry A,
  zweite Core-Instanz gleiche DB mit Registry B (Modell disabled) → identischer
  `create_dispatch` liefert den persistierten Dispatch, kein `ModelRegistryError`.

Design-Entscheidungen (Fix-Round): factory-only Konstruktion; read-only Maps via
`MappingProxyType`; ein einziges Eligibility-Prädikat; Registry-Fehler bleiben
bounded `ModelRegistryError` (nie rohe `TypeError`/`AttributeError`); Evidenz-
Klassifikation unverändert (UNIT für Registry-/Descriptor-Semantik, INTEGRATED für
F7-`create_dispatch`-Replay).

---

## 6. NICHT implementiert (explizit) / No-Overengineering-Statement

- E2 (Adaptive Roles / Model Routing / Auswahl/Entscheidung), E3 (Benchmarks),
  neue Provider, Credentials, Background-Service, Parallelisierung, DB-Schema-Änderung,
  Secret-Handling-Architektur — **alle nicht implementiert**.
- Kein Routing-Umbau, keine Änderung an `routing.py`-Kanonik, `_capability_for`,
  ContextBudgets, Store, `bind_spawn_result`/`complete_role`.
- Registry ist **statisch, lokal, versioniert, read-only**; Runtime-Availability wird
  **nie** gepollt (`UNKNOWN` nie als `AVAILABLE` erfunden).
- Keine Kostensortierung, kein „billiger ist besser", keine Micro-Capabilities, keine
  Agent-Mutations-API. Der Registry-Fehler ist bewusst **nicht** global in den
  Scheduler verdrahtet (Mapping nur an der Stelle des realen Auftretens).
