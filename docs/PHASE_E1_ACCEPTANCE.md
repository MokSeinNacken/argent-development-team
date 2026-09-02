# PHASE E1 ACCEPTANCE — Evidence Classification

**Branch:** `phase-e1-model-provider-abstraction` · **Writer:** E1 · **Datum:** 2026-09-02

Evidence-Klassifikation nach dem D3-Muster. **UNIT** = reine Registry-/Descriptor-
Semantik ohne produktiven Dispatch-Bezug; **COMPONENT** = Registry-Modul gegen eine
reale Phase-D-Komponente (Budget-Policy) ohne Dispatch; **INTEGRATED** = echter
`Core.create_dispatch`-Pfad mit realer Pipeline / injizierter Registry.

**Explizit NICHT implementiert:** E2 (dynamische Auswahl/Entscheidung/Routing),
E3 (Benchmarks), neue Provider (Claude/GLM/Gemini/Qwen). Es wird nirgends eine
End-to-End-Routing- oder Auswahlbehauptung aufgestellt.

---

## Acceptance Cases (Owner-Spec 1–10)

| # | Case | Evidenz-Klasse | Test |
|---|---|---|---|
| 1 | current flash valid + Coordination + Dispatch möglich | INTEGRATED | `test_case1_flash_valid_and_dispatchable` (echter LOW-risk Implementer-Dispatch) |
| 2 | pro + Code Implementation + Writer-Pfad | INTEGRATED | `test_case2_pro_writer_path` (NORMAL Implementer-Dispatch → pro) |
| 3 | sol + Review/Architecture + reasoning modelliert | UNIT | `test_case3_sol_review_architecture_reasoning` (Descriptor) |
| 4 | unknown → fail-closed (MODEL_NOT_ALLOWED/MODEL_CONFIG_INVALID) | INTEGRATED | `test_case4_unknown_identity_fails_closed` (injizierte Registry ohne sol, echter Lead-Dispatch) |
| 5 | disabled → kein Dispatch | INTEGRATED | `test_case5_disabled_model_no_dispatch` (injizierte Registry, sol disabled) |
| 6 | Modell ohne SECURITY_REVIEW → kein Security-Reviewer-Kandidat | UNIT | `test_case6_no_security_review_no_candidate` |
| 7 | Tool-Capability-Description verleiht keine Tool-Rechte | UNIT | `test_case7_tool_capability_grants_no_tool_rights` (roles.py unverändert) |
| 8 | Provider disabled → Modelle nicht eligible | UNIT | `test_case8_provider_disabled_models_not_eligible` |
| 9 | Independence: Writer-Modell nicht eigener Closing Reviewer | UNIT | `test_case9_writer_not_own_closing_reviewer` |
| 10 | Fake-Future-Provider via Registry ohne Core-Umbau | UNIT | `test_case10_future_provider_no_core_change` + `test_future_provider_via_registry_only` |

Hinweis zur Ehrlichkeit: CASE 4/5/10 sind über eine **injizierte Registry** (öffentliche
`Core(..., registry=...)`-Schnittstelle) geprüft — der produktive Default-Registry enthält
die kanonischen Modelle vollständig und kann die Negativ-Fälle daher nicht erzeugen.
CASE 4/5 nutzen trotzdem den echten `create_dispatch`-Pfad (INTEGRATED).

---

## Matrix A–J

| Matrix | Inhalt | Evidenz-Klasse |
|---|---|---|
| A | Provider Registry (valid/duplicate/malformed/unknown/disabled/unavailable) | UNIT |
| B | Model Registry (valid/unknown provider/duplicate/invalid metadata/lifecycle/enabled) | UNIT |
| C | Capabilities (Taxonomie, Requirements, required vs optional, Floor) | UNIT |
| D | Reasoning (supported/unsupported/not agent-controlled) | UNIT |
| E | Independence (same-model/different-model/provider) | UNIT |
| F | Security (keine Mutations-API, frozen, Injection wirkungslos, opaque credential_ref, Floor nicht senkbar) | UNIT |
| G | Context Integration (Descriptor-Metadaten überschreiben D1-Budget nicht) | COMPONENT |
| H | Current Models (flash/pro/sol + deepseek/openai korrekt, kein claude…) | UNIT/COMPONENT (Default-Registry-Load aus Repo-Dateien) |
| I | Future Provider (Fake via öffentliche Schnittstelle, kein Core-Umbau) | UNIT |
| J | Regression (D/C/B-Subsets + kanonischer Dispatch-Pfad) | INTEGRATED |

---

## Verifikation (Writer, ausgeführt)

- E1-Tests gesamt: **82 grün** (51 Basis + 31 Fix-Round `test_phase_e1_fix_round.py`).
- D/C/B-Subsets: **243 / 296 / 166 grün** (unverändert).
- Full Suite (`tests/`): **2043 grün** (1961 Basis + 82 E1).
- `git diff --check` sauber; kein `shell=True` in `argent_core/` (non-test).

---

## Fix-Round (Supervisor-Review, F1–F7)

Nach unabhängiger Supervisor-Review wurden sieben Findings bestätigt und in dieser
Fix-Round behoben.  Je Finding: Fix + Testnachweis.  Alle 51 Basis-Tests bleiben
grün; die Acceptance-Cases 1–10 bleiben grün (CASE 1 wurde auf die Spec korrigiert,
s. F6).

| # | Finding | Fix | Testnachweis (`test_phase_e1_fix_round.py`) |
|---|---|---|---|
| F1 | Registry-Konstruktion bypassbar + Core akzeptiert Duck-Typed Registry | `ModelRegistry.__init__` erzwingt Versions-Konsistenz, Key↔Descriptor-Id-Gleichheit, frozen Descriptor-Instanzen, entry `policy_version`; interne Maps `MappingProxyType` (read-only). `Core(registry=...)` akzeptiert nur `isinstance(ModelRegistry)`, sonst fail-closed `ModelRegistryError`. | `test_f1_key_mismatch_rejected`, `test_f1_non_descriptor_rejected`, `test_f1_version_mismatch_rejected`, `test_f1_maps_read_only`, `test_f1_fake_registry_in_core_fails_closed`, `test_f1_valid_injected_registry_ok` |
| F2 | Claim-Invariante `benchmarked:false`/trusted-local source + Provider-Obergrenze | `benchmarked is False` erzwungen; `_validate_source` bounded Allowlist + Agent-Origin-Ablehnung; Provider-`capabilities_supported` als Obergrenze bei Load erzwungen (`Model-tags ⊆ Provider-caps`). | `test_f2_benchmarked_true_rejected`, `test_f2_agent_origin_source_rejected`, `test_f2_untrusted_source_rejected`, `test_f2_model_tags_subset_of_provider_caps` |
| F3 | Schema-/Secret-Striktness | Exakte Key-Allowlists je Entry + Top-Level (unbekannte Keys reject, Secret-Key-Namen case-insensitiv); `credential_ref`/`auth_mode`/`profile_ref` Opaque-Grammatik; `endpoint_ref` http(s) ohne Userinfo; `load_files` Top-Level-Typen vor `.get()`, `registry_version`+`policy_version` beider Dokumente == `"1"`; Entry-`policy_version` == Dokument-`policy_version`; `abilities`/`provenance` ohne Truthiness-Coercion. | `test_f3_secret_key_name_rejected`, `test_f3_userinfo_endpoint_rejected`, `test_f3_abilities_not_dict_rejected`, `test_f3_top_level_list_rejected`, `test_f3_unknown_top_level_key_rejected`, `test_f3_policy_version_mismatch_rejected`, `test_f3_entry_policy_version_mismatch_rejected` |
| F4 | `CapabilityRequirements.validate()` zu lax | `__post_init__` (via `object.__setattr__`) kanonisiert Sequenzfelder list|tuple → frozen Tupel, Enum-Werte → `.value`, lehnt Duplikate ab; `context_requirement` schließt `bool` aus; alles `ModelRegistryError(MODEL_CONFIG_INVALID)`. | `test_f4_context_requirement_bool_rejected`, `test_f4_sequence_canonicalized_to_tuple`, `test_f4_enum_member_canonicalized`, `test_f4_duplicates_rejected`, `test_f4_non_sequence_rejected`, `test_f4_frozen_after_construction` |
| F5 | Zwei inkonsistente Eligibility-Pfade | EIN kanonisches Prädikat `_candidate_eligibility` (enabled ∧ Provider enabled+available ∧ lifecycle ACTIVE ∧ Floor ∧ Independence ∧ policy_allows_fallback), genutzt von `eligible_models` UND `is_fallback_eligible`; `requirements.validate()` immer zuerst; unbekanntes Referenzmodell in beiden Pfaden `MODEL_CONFIG_INVALID`. | `test_f5_retired_not_fallback_eligible`, `test_f5_consistency_eligible_and_fallback`, `test_f5_unknown_reference_model_invalid`, `test_f5_policy_allows_fallback_false` |
| F6 | Daten/Claims (flash ohne COORDINATION/SIMPLE_ANALYSIS, Provider-caps unvollständig) | `models.json`: flash erhält `COORDINATION` + `SIMPLE_ANALYSIS` (Architektur §11), Provenienz evidence-basiert (Architektur §11 + routing.py + openclaw.json). `providers.json`: `capabilities_supported` = Obergrenze (deepseek = Flash∪Pro-Tags, openai = Sol-Tags) + Load-Validierung. CASE 1 auf Spec korrigiert (`COORDINATION ∈ flash.capability_tags`). | `test_f6_flash_coordination_and_simple_analysis`, `test_f6_provider_caps_upper_bound`, `test_f6_security_review_only_sol` + `test_case1_flash_valid_and_dispatchable` (korrigiert) |
| F7 | Registry-Validierung außerhalb des Idempotenz-Replay | `validate_identity` in `create_dispatch` von außerhalb `work()` NACH Idempotenz-/Existing-Dispatch-Replay und VOR Insert verschoben (nur neue Dispatches); routing-role-policy-Check bleibt außen unverändert. | `test_f7_idempotent_replay_skips_registry_validation` |

Design-Entscheidungen (Fix-Round):
- **Factory-only:** `ModelRegistry` ist weiterhin via `from_payload`/`load_files` zu bauen;
  direkte `__init__`-Konstruktion mit inkonsistenten Daten scheitert fail-closed.
- **Read-only-Maps:** `MappingProxyType` statt bloßem `dict` — Mutationen der internen
  Maps werfen `TypeError`; es existiert weiterhin keine öffentliche Mutations-API.
- **Ein Eligibility-Prädikat:** eliminiert die zuvor unterschiedlichen
  `eligible_models`/`is_fallback_eligible`-Pfade (RETIRED war zuvor als
  fallback-eligible durchgeschlüpft).
- **Evidenz-Klassifikation unverändert:** Fix-Round-Tests sind UNIT (Registry-/Descriptor-
  Semantik) bzw. INTEGRATED (F7: echter `create_dispatch`-Replay-Pfad über zwei
  Core-Instanzen auf derselben DB).

---

## No-Overengineering / Scope-Disziplin

- Genau **ein** Integrationspunkt (`Core.create_dispatch`), **kein** Routing-Umbau.
- Registry statisch/lokal/versioniert, **kein** Polling, **kein** Live-Availability,
  **kein** DB-Schema-Change (SCHEMA_VERSION bleibt 13), **keine** Secrets.
- **Keine** dynamische Auswahl/Entscheidung, **keine** Fallback-Ausführung,
  **keine** Kostensortierung/Selection in E1.
