# PHASE D1 — Context Pack Core + Token Budgets

**Status:** Implementiert (Writer, dirty Worktree, kein Commit/Push — Supervisor committet nach Review).
**Branch:** `phase-d1-context-pack-core`, Base `65bfa1b` (Phase-C-GREEN).
**Verbindliche Basis:** `docs/ARGENT_ARCHITECTURE_V1_FINAL.md` §12 (Context Engineering),
§2 (Context Router), §5 (orthogonale Jobfelder), §16 (Trust Boundaries — Context Router),
§17 (Storage-Konvention), §19 (Schema-Plan `context_packs`), §22/§23.

---

## 1. Analyse-Antworten (Supervisor, read-only) — übernommen

- **A.** Agent-Kontext entsteht heute minimal in `Supervisor._build_message_file(d)`
  (task_id/dispatch_id/role/title/description). Zusätzlich existiert
  `argent_core/context.py` (role-scoped, pure, statische Regel-Strings
  `PROJECT_RULES`/`SECURITY_ARCH_RULES`, `repo_summary`-Allowlist, hash/summary) und
  die Tabelle `agent_context_snapshots` (dispatch_id PK, role, position,
  context_hash, context_summary_json, created_at).
- **B.** Bestandteile: (1) trusted local facts, (2) owner instruction, (3) arch/policy
  (statische Regeln), (4) task-specific evidence (repo_summary bounded), (5) previous
  agent output (structured/bounded), (6) optional history (kaum vorhanden, keine
  Transcript-Dumps im Core).
- **C.** Kleinster vertrauenswürdiger Punkt: `_perform_spawn_run` → `_build_message_file`
  VOR `_spawn_scoped` (supervisor.py ~3184) — genau EIN produktiver Dispatch-Punkt
  (`SPAWN_RUN`), der bereits C1-Admission + C2-Enforcer passiert.
- **D.** Mehrfachkopien heute: repo_summary/Regel-Strings/Handoffs dupliziert. D1 dedupliziert.
- **E.** Vollständig bleiben MÜSSEN (REQUIRED): Owner-Objective/Acceptance-Criteria,
  aktive Safety-/Trust-Constraints, relevante Architektur-Invarianten, notwendige
  Task-Facts. NIEMALS semantisch verändern/kürzen.
- **F.** Wiederverwendet: `agent_context_snapshots` (bleibt unangetastet, V2-Zweck),
  `context.py` (Regeln/Role-Allowlist/bounded repo_summary), `outputs.py`,
  Handoff-/Finding-Strukturen im Store.

---

## 2. Neues Modul: `argent_core/context_pack.py` (rein, deterministisch, keine Provider-Kopplung)

### 2.1 Objektmodell

| Typ | Felder | Anmerkung |
|---|---|---|
| `TrustClass` (Enum) | `OWNER_INSTRUCTION, TRUSTED_POLICY, TRUSTED_LOCAL_FACT, TRUSTED_ARTIFACT, AGENT_RESULT, EXTERNAL_UNTRUSTED, OPTIONAL_HISTORY` | Trust wird **lokal** vom Builder aus dem Input-Slot bestimmt, nie aus Agent-Text |
| `Importance` (Enum) | `REQUIRED, HIGH, NORMAL, OPTIONAL` | `REQUIRED` wird nie getrimmt |
| `ExpansionReason` (Enum) | `REQUIRED_CONTEXT, LARGE_CODE_EVIDENCE, SECURITY_REVIEW, ROOT_CAUSE_ANALYSIS, INTEGRATED_REVIEW` | bounded, persistiert |
| `CapabilityTier` (Enum) | `FLASH, PRO, SOL` | Pre-Phase-E Mapping aus trusted `expected_model_class` |
| `ContextItem` (frozen) | `id, trust_class, importance, source_type, source_ref, content, content_hash, metadata` | `id` = `ci_`+sha256(trust,type,ref,content)[:16], stabil/lokal |
| `ArtifactRef` (frozen) | `ref, location, excerpt, content_hash` | bounded ref + optionaler Excerpt |
| `ProvenanceEntry` (frozen) | `source_type, source_ref, trust_class` | |
| `BudgetTier` (frozen) | `soft_min, soft_max, soft_target, hard` | |
| `ContextBudgetPolicy` (frozen) | `policy_version, allow_expansion, flash, pro, sol` | versioniert |
| `ContextPack` (frozen) | siehe §2.3 | Manifest, KEINE freien Agentenfelder als trusted Policy |
| `ContextPackRecord` (frozen) | bounded Metadaten für `context_packs`-Tabelle | |

### 2.2 Trust-/Importance-Zuordnung (lokal, durch Slot)

| Input-Slot | TrustClass | Importance (default) |
|---|---|---|
| `objective` | OWNER_INSTRUCTION | REQUIRED |
| `acceptance_criteria` | OWNER_INSTRUCTION | REQUIRED |
| `constraints` | TRUSTED_POLICY | REQUIRED |
| `policy_references` | TRUSTED_POLICY | REQUIRED |
| `facts` (FactInput) | TRUSTED_LOCAL_FACT | NORMAL (überschreibbar) |
| `artifacts` (ArtifactRef) | TRUSTED_ARTIFACT | NORMAL |
| `history` | OPTIONAL_HISTORY | OPTIONAL |
| `prior_results` (ResultInput) | AGENT_RESULT | OPTIONAL (überschreibbar) |

### 2.3 ContextPack-Felder (identity / task / policy / facts / artifacts / history / budget / provenance / integrity)

`version, context_pack_id, job_id, dispatch_id, role, created_at` (identity) ·
`objective, acceptance_criteria, constraints` (task) · `policy_references` (policy) ·
`facts` · `artifacts` · `history` · `budget_soft, budget_hard, budget_estimated,
token_count, expansion_reason` (budget) · `provenance` (ProvenanceEntry-Tupel) ·
`content_hash` (integrity) · `items` (kanonische geordnete Item-Liste, Single Source of Truth).

---

## 3. Budget-Policy (Defaults aus §12)

| Tier | soft range | soft_target | hard |
|---|---:|---:|---:|
| FLASH | 4k – 8k | 6k | 16k |
| PRO | 12k – 24k | 16k | 48k |
| SOL | 24k – 48k | 32k | 96k |

- Der **Enforcement-Schwellwert** `budget_soft` = `soft_max` (8k/24k/48k). `budget_hard` = `hard`.
- `allow_expansion` (Default `true`): ein Pack darf `soft` bis `hard` nur mit bounded
  persistiertem `ExpansionReason` überschreiten; ohne Reason → `CONTEXT_BUDGET_EXCEEDED`.
- Budget-Auswahl erfolgt NIE agent-gesteuert, sondern aus dem trusted
  `expected_model_class` (Mapping: `*flash*`→FLASH, `*sol*`→SOL, sonst PRO; konservativer
  Default PRO) — siehe `Supervisor._capability_for`.

---

## 4. Token-Estimator (warum konservative Approximation statt Vendor-Tokenizer)

```python
def estimate_tokens(text) -> int:
    return max(1, len(text) // 4)
```

- **Formel:** `chars / 4`, abgerundet, Minimum 1.
- **Warum keine Vendor-Tokenizer:** (a) null Provider-Kopplung (Modul bleibt rein und
  deterministisch, testbar ohne Netz/API-Key); (b) reproduzierbar über alle Anbieter;
  (c) kein Drift zwischen Provider-Versionen. `chars/4` ist die konservative
  4-Zeichen-≈-1-Token-Regel; die Abrundung hält ein „unter Budget" liegendes Pack
  sicher innerhalb der echten Token-Grenzen.

---

## 5. Trimming- / Dedup-Ordnung

**Dedup:** Key `(trust_class, source_type, source_ref, content_hash)`; bei Kollision
gewinnt die höhere Importance (REQUIRED > HIGH > NORMAL > OPTIONAL). Gleiche Fact/Policy/
Artifact → je genau einmal.

**Trimming (deterministisch, nur wenn `total > soft`; `total` = Render-Token, F4):**
1. `OPTIONAL_HISTORY`
2. redundante `AGENT_RESULT`
3. `OPTIONAL` (sonstige)
4. `NORMAL`
5. `HIGH` **nur wenn referenzierbar** (`source_ref != ""`; inline-HIGH ohne Ref bleibt)
6. `REQUIRED` → **nie**

Innerhalb einer Gruppe wird nach stabiler Item-`id` sortiert entfernt (deterministisch).
Greedy: stoppt sobald `total <= soft`.

**Fail-closed:** `REQUIRED` allein `> hard` → `CONTEXT_BUDGET_EXCEEDED` (kein stilles
Kürzen, kein Dispatch). `soft < total <= hard` nur mit bounded Reason + Policy-Erlaubnis.

---

## 6. Hash-Design (content vs. instance)

- **`content_hash(pack)`** = SHA-256 über kanonisches JSON `{version, role, items[]}`
  mit **sortierten** Items (`trust_class, importance, source_type, source_ref, content,
  metadata`; **ohne** Item-`id`). Volatile/Instance-Metadaten (`context_pack_id`,
  `created_at`, `job_id`, `dispatch_id`) sind **NICHT** enthalten (§19: gleiche
  semantische Inputs → gleicher Hash; Reordering nicht-semantischer Items → gleicher Hash).
- **`instance_id` / `context_pack_id`** = `cp_` + SHA-256(`dispatch_id`+`content_hash`)[:24]
  — deterministisch und **content-stabil** (F2): ein Retry mit gleichem semantischem
  Content liefert dieselbe Pack-ID; `created_at` ist reine Instance-Metadaten und
  nicht Teil der ID.
- `validate_context_pack` erzwingt: Version, Item-/Pack-ID-Format, Trust-/Importance-
  Enums, Budget-Konsistenz (`0 <= soft <= hard`, `token_count <= hard`), gültiger
  `ExpansionReason`, und **Hash-Match** (Mutation → `CONTEXT_HASH_MISMATCH`).
- **F5 (fix-round):** `validate_context_pack` rechnet kanonisch neu — jede Item-`content_hash`
  und stabile Item-`id` werden aus dem Content abgeleitet, `token_count` muss gleich
  `estimate_tokens(render_pack(pack))` sein, `budget_estimated >= token_count`, und die
  Expansion-Semantik (`token_count > soft ⇔ expansion_reason`) ist konsistent. Manipulierte
  Felder → `CONTEXT_*`-Fehler.
- **F4 (fix-round):** `token_count` ist jetzt die Token-Schätzung des **vollständigen
  kanonischen Render** (Identitätsfelder, Labels, Referenz-/Metadatenfelder,
  Abschlussinstruktion), nicht nur `item.content`. Zusätzlich bounded Limits für
  Referenz-/Metadatenfelder (Item-Anzahl, Provenance-Einträge, `source_ref`/Artifact-Ref/
  Location, Metadaten-Schlüssel/-Werte) — `CONTEXT_INVALID_REFERENCE`.

---

## 7. Storage-Entscheidung (DB-Metadaten + persistente Artifakte)

- **NEUE Tabelle `context_packs`** (Phase D, §19) — nicht `agent_context_snapshots`
  (die dient dem V2-Rollensnapshot mit `position`/`context_summary_json` und trägt die
  D1-Felder `context_pack_id/job_id/version/budgets/expansion_reason/artifact_location`
  nicht). `SCHEMA_VERSION` 11 → 12 (additiv, idempotent, B4-Muster).
  - `context_pack_id` PK, `dispatch_id` UNIQUE FK → `agent_dispatches`, `job_id`, `role`,
    `version`, `content_hash`, `size_estimate`, `token_count`, `soft_budget`,
    `hard_budget`, `expansion_reason`, `artifact_location`, `created_at`.
- **Nur bounded Metadaten in SQLite.** Große Pack-Inhalte gehen in die Message-Datei
  (kleine bounded temp-Datei) bzw. in D2/D3 in persistente Artifacts unter
  `~/.local/share/argent/` (`artifact_location`-Referenz); **NICHT `/tmp`** für persistente
  Packs. Cache-artig Regenerierbares unter `~/.cache/argent/` (D2/D3).
- `_persist_context_pack` ist idempotent (gleicher `content_hash` → Wiederverwendung;
  anderer Hash für dieselbe Dispatch → `CONTEXT_STALE_PACK`, fail-closed).

---

## 8. Dispatch-Integrationspunkt (exakte Stelle)

`argent_core/supervisor.py` → `Supervisor._perform_spawn_run` (nach C1-Admission +
C2-Enforcer-Check, **vor** `_build_message_file`/`_spawn_scoped`):

```python
pack = self._build_context_pack(d, job)   # raises ContextBuildError (fail-closed)
self._persist_context_pack(pack)
message_file = self._build_message_file(d, pack)
...
except ContextBuildError as exc:
    return ActionOutcome("SPAWN_RUN", "context_build_failed", exc.code, ...)
```

- `_build_context_pack` baut den Pack aus **trusted local facts**: Objective
  (F1: `Title` + `Description` verlustfrei zu EINEM REQUIRED OWNER_INSTRUCTION-Objective
  gefaltet — nie ein trimmbarer NORMAL-Titel-Fact), statische `PROJECT_RULES` +
  `SECURITY_ARCH_RULES` als Constraints, Task-/Dispatch-Facts (`task_id`, `dispatch_id`
  = REQUIRED; `risk_class`, `state` = NORMAL), Capability aus `d.expected_model_class`.
- Build-Fehler → **kein Dispatch**, ActionOutcome `context_build_failed`.
- **F3 (fix-round):** der Integrationspunkt ruft nach JEDEM Builder-Aufruf
  `validate_context_pack(pack)` zwingend auf (vor Persistenz/Message-Rendering/Spawn) —
  ein injizierter Builder kann keinen formal ungültigen Pack durchschleusen.
- **F6 (fix-round):** Scheduler routet `context_build_failed` klassifiziert:
  permanente Context-Codes (`is_permanent_context_code()` in `context_pack.py`)
  → fail-closed nach **BLOCKED** (`quarantine_blocked`, kein Retry, kein Spawn,
  Owner-/Policy-Reopen); nur nachweislich transiente Codes (I/O beim Persist/Artifact-Write)
  → bounded Requeue als **QUEUED mit `error_class=CONTEXT`** — ORCHESTRATION-Fehler,
  **nie CODE_FAILURE/DETERMINISTIC, nie RESOURCE, nie Spawn**.
- `Supervisor.__init__` akzeptiert optionalen `context_builder` (Tests injizieren Fake;
  Default = echter Builder).

### Legacy-Inventar

- `_build_message_file(d, pack=None)`: `pack is not None` → `render_pack(pack)`;
  `pack is None` → der bisherige Minimal-Prompt, **nur** für NICHT-D1-migrierte Pfade
  (heute existieren keine weiteren produktiven Dispatch-Pfade; `SPAWN_RUN` ist der einzige).
  Ein D1-migrierter Dispatch fällt bei fehlendem/ungültigem Pack **nie** auf diesen
  Legacy-Prompt zurück (fail-closed, getestet in CASE 8 / test_phase_d1_dispatch).

---

## 9. NICHT implementiert (explizit)

- **D2** (Retrieval/Handoffs/Checkpoints/Artifact-Handoff), **D3** (Invalidation/Stale-Pack-
  Widerruf über facts_version/HEAD/diff-hash), **Phase E** (Adaptive Roles + Model Routing):
  NICHT Teil von D1.
- **Keine Vector DB**, **keine Provider-Kopplung**, **keine Vendor-Tokenizer**.
- Keine Erweiterung des Owner-Scope, keine erfundenen Fakten, keine Secrets, kein
  Routing-Change durch den Builder.

---

## 10. Tests

- `tests/test_phase_d1_context_pack.py` (38) — Schema A, Required B, Budgets C, Trimming D,
  Dedup E, Provenance F, Integrity/Hash G, Security H.
- `tests/test_phase_d1_storage.py` (4) — Persistenz/Storage I.
- `tests/test_phase_d1_dispatch.py` (3) — Dispatch-Integration J.
- `tests/test_phase_d1_acceptance.py` (8) — Acceptance-Cases K (CASE 1–8).
- `tests/test_phase_d1_fix_round.py` (20) — adversariale Fix-Round-Tests F1–F6.
- `tests/d1_helpers.py` — geteilte deterministische Env-Helfer (FakeScopeBackend etc.).

**Verifikation (Writer, nach Fix-Round):**
- `test_phase_d1_*.py` → 73 grün (53 + 20 neue).
- C1 82 + C2 112 + C3 102 = 296 grün; B 166 grün.
- Full Suite (`--ignore=e2e-fixture`) → **1791 grün** (vorher 1771; +20 Fix-Round).
- `grep shell=True argent_core/` → keine neuen; `git diff --check` → sauber.
- Zwei bestehende Tests aktualisiert (nur Versions-Pins, keine Verhaltensänderung):
  `test_phase3c_approval_core.py::test_schema_version_is_11` → `..._is_12` und
  `test_phase_c3_migration.py` (hardcodierte `"11"` → `SCHEMA_VERSION`), da der
  verbindliche `SCHEMA_VERSION`-Bump 11→12 diese Literale notwendig verschiebt.
- D1-Fix-Round: F1 (Titel REQUIRED), F2 (stabile Pack-ID), F3 (Integrationspunkt
  validiert), F4 (Render-basiertes Token-Budget + bounded Metadaten), F5 (kanonische
  Re-Validierung), F6 (permanent→BLOCKED / transient→Requeue) — siehe
  `test_phase_d1_fix_round.py`.
