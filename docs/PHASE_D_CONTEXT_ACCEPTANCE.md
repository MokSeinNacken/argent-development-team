# PHASE D — Context Engineering Acceptance & Economy Report

**Branch:** `phase-d3-context-integrated-acceptance` · **Status:** Acceptance GREEN
(nach D3-Fix-Round F1–F4).  Nur **gemessene/verifizierte Fakten** — keine
Marketingzahlen, keine Phase-E-Funktionen (Adaptive Roles / Model Routing /
Test Economy existieren in Phase D nicht).

---

## 0. Evidence-Klassifikation (verbindlich, ehrlich)

Jede Behauptung dieses Reports trägt exakt die Testart, die sie belegt.  Drei
Evidenz-Stufen:

- **UNIT** — direkter Aufruf eines Bausteins (Builder / Validator / Checkpoint /
  Handoff) ohne Scheduler/Dispatch.
- **COMPONENT** — mehrere Bausteine integriert (Builder + Retrieval + Store /
  Checkpoint-Store), aber OHNE den produktiven Scheduler-/Spawn-Pfad.
- **INTEGRATED** — über den ECHTEN `Scheduler`/`_perform_spawn_run`-Pfad mit
  Fake-Enforcer/Governor/Clock (kein Provider-Run); Job-/Action-/Spawn-Wirkung
  wird geprüft.

Es gibt **keine** globale „alles ist integriert"-Behauptung mehr.  Die
Zuordnung je CASE steht in §2.1.

---

## 1. Ziel

Phase D (§12 ARGENT_ARCHITECTURE_V1_FINAL) fordert: immutable Context Packs pro
Dispatch, Budgets (soft/hard), Retrieval, Invalidation und Artifact-Handoff —
**kein History-Dump**, **stale Pack fail-closed**, messbare Context-Reduktion.

Dieser Report dokumentiert, was D3 **gemessen** hat (provider-neutral) und mit
**welcher Testart** jede Behauptung belegt ist, sowie das **Legacy-Inventar**
der Dispatch-Pfade.

---

## 2. Gemessene Pack-Größen (provider-neutral)

Token-Schätzung: `estimate_tokens = max(1, len(render)//4)` (4 Zeichen ≈ 1 Token,
deterministisch, provider-neutral). `render` = voller kanonischer Prompt
(Identitätsfelder + Labels + Referenz-/Metadatenfelder + Abschlussinstruktion),
nicht nur `item.content`.

| Fixture (Acceptance) | Rolle / Tier | token_count | soft | hard | items | expansion | Evidenz |
|---|---:|---:|---:|---:|---:|---:|
| CASE 1 Simple Task | lead / FLASH | 80 | 8 000 | 16 000 | 3 | – | INTEGRATED |
| Implementer | implementer / PRO | 88 | 24 000 | 48 000 | 4 | – | COMPONENT |
| QA (mit Handoff=AGENT_RESULT) | qa / PRO | 92 | 24 000 | 48 000 | 4 | – | COMPONENT |
| Reviewer | reviewer / SOL | 80 | 48 000 | 96 000 | 3 | – | COMPONENT |
| CASE 4 Oversized optional History | qa / FLASH | 77 (getrimmt) | 8 000 | 16 000 | 4 | – | UNIT |
| CASE 12 Amplification (dedup) | qa / FLASH | 4 062 | 8 000 | 16 000 | 3 | – | UNIT |
| CASE 14 Security Review | reviewer / FLASH | 10 389 | 8 000 | 16 000 | 101 | SECURITY_REVIEW | UNIT |

**Integrated E2E (CASE 15 / Flow A, echter `SPAWN_RUN`-Pfad, 7 Dispatches):**

| Dispatch (Reihenfolge) | Rolle | token_count | soft | hard |
|---|---:|---:|---:|---:|
| 1 | lead | 184 | 48 000 | 96 000 |
| 2 | analyst | 235 | 24 000 | 48 000 |
| 3 | lead | 272 | 48 000 | 96 000 |
| 4 | implementer | 312 | 24 000 | 48 000 |
| 5 | qa | 357 | 24 000 | 48 000 |
| 6 | reviewer | 386 | 48 000 | 96 000 |
| 7 | lead | 424 | 48 000 | 96 000 |

Alle Packs liegen **weit unter dem Soft-Budget**; kein Pack expandiert ohne
Grund. Der Implementer-Handoff trägt 1 Artifact-Ref (`src/module.py`) mit
full-file sha256 + git-HEAD-Revision + bounded Excerpt.

### 2.1 CASE → Evidenz-Zuordnung (ehrlich)

| CASE | Eigenschaft | Evidenz (vor Fix-Round) | Nach D3-Fix-Round |
|---|---|---|---|
| 1 | einfacher Task, kleiner Pack, Dispatch | INTEGRATED (echter Scheduler) | INTEGRATED |
| 2 | Implementer→QA Handoff als AGENT_RESULT | COMPONENT (Builder+Retrieval+Store) | COMPONENT |
| 3 | QA→Reviewer, Injection als Daten | COMPONENT | COMPONENT |
| 4 | oversized optional History getrimmt | **UNIT** (Builder) | UNIT (produktiver Pfad injiziert keine optionale History) |
| 5 | REQUIRED > Hard → BLOCKED, kein Spawn | UNIT (a) + INTEGRATED (b, injizierter failing Builder) | INTEGRATED (auch echter Builder + oversized Objective) |
| 6 | stale File/Hash → STALE | **UNIT** (Validator) | **INTEGRATED** (echter Dispatch + alter Checkpoint → kein Spawn) |
| 7 | missing Artifact → fail-closed | **UNIT** (Validator) | **INTEGRATED** (echter Dispatch + fehlender Artifact → kein Spawn) |
| 8 | Prompt-Injection wirkungslos | UNIT + COMPONENT (Retrieval+Builder) | + **INTEGRATED** (Injection-Artefakt im echten Pfad → Daten, Spawn normal) |
| 9 | Restart → neuer Pack | COMPONENT (Checkpoint+Builder) | COMPONENT |
| 10 | Crash-Window | COMPONENT (Checkpoint-Store) | COMPONENT |
| 11 | Fencing | COMPONENT (Checkpoint-Store) | COMPONENT |
| 12 | Amplification/Dedup | **UNIT** (Builder) | UNIT |
| 13 | große Code-Evidence bounded | COMPONENT (Retrieval+Builder) | COMPONENT |
| 14 | Security-Review-Expansion | **UNIT** (Builder) | UNIT |
| 15 | E2E Dev-Flow | INTEGRATED (echter Scheduler) | INTEGRATED |

Fett markiert = die im Sol-Review als überzeichnet beanstandeten CASEs; die
Spalte „nach Fix-Round" zeigt die ehrliche Einordnung bzw. die neu ergänzte
INTEGRATED-Evidence (CASE 5/6/7/8, s. §5.1).

---

## 3. Was zwischen Agents übertragen wird / NICHT wird

**Übertragen (bounded):**
- Owner-Objective/Acceptance-Criteria/Constraints/Policy-Refs — `REQUIRED`
  (TrustClass `OWNER_INSTRUCTION`/`TRUSTED_POLICY`).
- Bounded Facts (`TRUSTED_LOCAL_FACT`), bounded Artifact-Refs
  (`TRUSTED_ARTIFACT`: Pfad relativ zum Worktree + sha256 + Revision + bounded
  Excerpt ≤ 4 KiB), Handoff als `AGENT_RESULT` (nie Policy), optional
  deterministisch getrimmte History.
- Implementer-/QA-Handoff: **Refs + Hashes + bounded Excerpts + git-Revision**
  (max. 32 Refs; 4-MiB-Hash-Cap/Datei; 4-KiB-Excerpt). **Keine ganzen
  Datei-Inhalte.**
- **F1 (Fix-Round):** ein deklarierter Pfad wird NUR übernommen, wenn er (a) im
  autoritativen Write-/Diff-Scope liegt (Broker-Write-Evidence `patch_set_json`
  + `git diff --name-only HEAD`) ODER (b) ein `tests_run`-Pfad im erlaubten
  Test-Scope ist (`tests/`/`test_*.py`/`*_test.py`).  Zusätzlich verweigert eine
  bounded Secret-/Forbidden-Denylist (`artifact_refs.is_forbidden_ref`):
  `.env`, `*.pem`, `*.key`, `credentials*`, `id_rsa`/`id_dsa`/…, `token*`,
  `secrets`, `.ssh`/`.gnupg`/`.config`/`keyrings` sowie beliebige versteckte
  Punktdateien/-verzeichnisse.  Unbestätigte oder verbotene Pfade → **weggelassen**.

**NICHT übertragen:**
- Rohe Session-Transcripts / Tool-Logs / komplette Diffs / komplette History.
- Session-Felder (`child_session_id`, `run_id`, `session_key`, `transcript`,
  `trajectory`) — per Test B asserted: kein Prompt/Pack enthält diese Felder.
- Secrets (Retrieval verweigert `~/.ssh`, `~/.config`, `~/.gnupg`, `/etc`,
  `/proc`, `/sys`, `/dev` fail-closed; Artifact-Refs zusätzlich per F1-Denylist).

---

## 4. Trimming / Dedup / Budget-Evidence

- **Deterministic Trimming (CASE 4, UNIT):** 40 000 Zeichen optionale History
  (`budget_estimated` 10 077 Tokens) → getrimmt auf `token_count` 77
  (REQUIRED unverändert). Reihenfolge `OPTIONAL_HISTORY → redundantes
  AGENT_RESULT → OPTIONAL → NORMAL → HIGH (nur referenzierbar) → REQUIRED (nie)`.
- **Dedup (CASE 12, UNIT):** 2 identische 8k-Facts → 1 Item; 2 identische
  8k-History → 1 Item; identische Artifact-Refs → 1 Artifact-Item. Budget zählt
  den **tatsächlichen Render**.
- **Cross-Slot-Dedup bewusst NICHT gemergt** (fail-closed, s. §7).
- **Expansion (CASE 14, UNIT):** > Soft nur mit reason code (`SECURITY_REVIEW`);
  ohne reason → `CONTEXT_BUDGET_EXCEEDED`.
- **REQUIRED > Hard (CASE 5, INTEGRATED):** `CONTEXT_BUDGET_EXCEEDED`, kein
  Dispatch, Job → `BLOCKED`, `error_class=CONTEXT` (nie CODE_FAILURE/RESOURCE).
  Neu auch mit **echtem** ContextBuilder über einen oversized Objective belegt.

---

## 5. Restart / Stale / Injection-Evidence

- **Restart (CASE 9, Test C, COMPONENT):** Checkpoint → reopen → neuer Pack aus
  Checkpoint + aktuellen Facts. Objective immer vom trusted Caller.
- **Stale (CASE 6/7, Test H, UNIT + INTEGRATED):** geänderte Datei/Hash, HEAD-/
  Base-/Repo-Mismatch, unbekannter Handoff-/Pack-Ref → `STALE_CONTEXT_REFERENCE`;
  fehlende `current_facts` → `CONTEXT_CHECKPOINT_INVALID`. Alte Evidence wird
  **nie** still wiederverwendet. Kein Raw-History-Fallback, kein „ähnliche
  Datei"-Substitut.
- **F3 (Fix-Round):** unauflösbare/nicht vollständig hashbare Dateien werden im
  Handoff **vollständig weggelassen** (kein `HandoffArtifact` mit leerem Hash);
  der Checkpoint übernimmt nur Refs mit validem Full-File-Hash.  Damit gibt es
  **kein** künstliches `STALE_CONTEXT_REFERENCE` beim Restart durch leere
  Hash-Refs mehr (getestet: `test_f3_no_empty_hash_ref_restart_no_false_stale`).
- **Injection (CASE 8, UNIT + COMPONENT + INTEGRATED):** Handoff-Payload mit
  Policy-Marker wird **abgelehnt**; Retrieval verweigert Root-Extension/Traversal;
  Datei-CONTENT, der Policy behauptet, wird als `TRUSTED_ARTIFACT` eingebettet
  (Trust durch Slot, nie durch Inhalt). Neu INTEGRATED: ein Injection-Artefakt im
  Worktree fließt durch den echten Pfad als **Daten** (bounded Excerpt,
  AGENT_RESULT), Trust/Budget unverändert, Spawn erfolgt normal
  (`test_f4_case8_integrated_injection_artifact_data_only`).
- **Fencing (CASE 11, Test E, COMPONENT):** stale Lease/owner/epoch →
  `LeaseFencedError`, kein Checkpoint-/Resume-Write.
- **Crash-Window (CASE 10, Test D, COMPONENT):** Handoff-PK verhindert Duplikat;
  Checkpoint INSERT-only; Pack idempotent + `CONTEXT_STALE_PACK` bei Drift.

### 5.1 Neue INTEGRATED-Evidence (D3-Fix-Round, F4 Variante A)

- `test_f4_case5_integrated_budget_overflow_blocked` — echter ContextBuilder +
  oversized Objective → `context_build_failed` → BLOCKED, kein Spawn.
- `test_f4_case6_integrated_stale_checkpoint_no_spawn` — alter Checkpoint →
  `STALE_CONTEXT_REFERENCE` → `context_build_failed` → BLOCKED, kein Spawn.
- `test_f4_case7_integrated_missing_artifact_no_spawn` — fehlender Artifact →
  `STALE_CONTEXT_REFERENCE` → kein Spawn, kein Raw-History-Fallback.
- `test_f4_case8_integrated_injection_artifact_data_only` — Injection-Artefakt
  als Daten, Spawn normal, Budget unverändert.

---

## 6. Legacy-Inventar (Dispatch-Pfade)

| Pfad | Status | Notiz |
|---|---|---|
| `Supervisor._perform_spawn_run` → `_build_context_pack` → `validate_context_pack` → `_persist_context_pack` → `_build_message_file(d, pack, pack_id)` → `_spawn_scoped` | **MIGRATED (produktiv)** | einziger produktiver Spawn-Pfad; Pack-Pflicht, fail-closed |
| `_build_message_file(pack=None)` (Legacy-Minimal-Prompt) | **LEGACY** | keine produktiven Caller; isoliert, dokumentiert (PHASE_D1_NOTES.md) |
| `smoke/*.py` (phase2b_e2e, phase2c_recovery_*, phase3b_live_smoke) | **LEGACY (manuell)** | manueller Betrieb; bauen ihren Prompt selbst |

Kein stiller Fallback: `_build_context_pack` wirft bei Fehler
(`ContextBuildError`), es gibt keinen try/except-Fallback auf den Legacy-Prompt.

---

## 7. Bekannte Grenzen

- **Hash-Cap 4 MiB/Datei (F2):** größere Dateien ergeben keinen Handoff-Hash →
  Stale-Erkennung fail-closed. `sha256_file` liest nur reguläre Dateien
  (`stat.S_ISREG`), mit Byte-Counter (Abbruch bei `max_bytes + 1` bei Wachstum);
  alle öffentlichen Parameter werden hart geklemmt (`max_refs` ≤ 32,
  `max_excerpt_bytes` ≤ 4 KiB, `max_bytes` ≤ 4 MiB).
- **F1-Scope:** ein Agent kann den autoritativen Scope **nicht** erweitern —
  nur Broker-Write-Evidence + `git diff` + erlaubter Test-Scope bestätigen einen
  Pfad; alles andere wird verworfen.
- **Cross-Slot-Dedup nicht aktiv** (s. §4) — unterschiedliche Trust-Slots werden
  nie still zusammengelegt.
- **Phase E** (Adaptive Roles / Model Routing / `test_plan`) ist **nicht** Teil
  von Phase D; Capability-Tier wird explizit übergeben.
- Token-Schätzung ist eine **Approximation** (4 Zeichen ≈ 1 Token), bewusst ohne
  Provider-Tokenizer; Budget-Enforcement bleibt konservativ.

---

## 8. Verifikation

- D3: **79** Tests grün (`test_phase_d3_*.py`), davon **18** neue
  Fix-Round-Tests (`test_phase_d3_fix_round.py`, F1–F4).
- D2 + D1: **164** grün · C: **296** grün · B: **166** grün.
- Full Suite (`--ignore=e2e-fixture`): **1961** grün (~46 s).
- `shell=True` in `argent_core/` (non-test): **keine**. `git diff --check`: **sauber**.
