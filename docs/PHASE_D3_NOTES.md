# PHASE D3 NOTES — Integrated Context Engineering Acceptance

**Branch:** `phase-d3-context-integrated-acceptance` (Base `ee46eb3` = D2-GREEN)
**Rolle:** D3-Writer (kein Reviewer, kein Commit/Push — Worktree bleibt dirty).
**Datum:** 2026-09-01

## 1. Analyse-Antworten A–H (Design-Grundlage, read-only)

- **A — Produktiver Spawn-Pfad:** genau EIN produktiver Pfad —
  `Supervisor._perform_spawn_run` (supervisor.py ~3170):
  `_build_context_pack` → `validate_context_pack` → `_persist_context_pack` →
  `_build_message_file(d, pack, pack_id)` → `_spawn_scoped`. Pack-Pflicht,
  fail-closed (kein stiller Fallback).
- **B — Legacy-Context:** `_build_message_file(pack=None)` existiert nur noch als
  isolierte, dokumentierte Funktion. Keine produktiven Caller (kein
  `pack=None`-Aufruf im Produktpfad). Bestätigt per Test
  (`test_no_silent_legacy_fallback_on_build_failure` in D1 + D3-CASE 5).
- **C — Raw-History-Fallback:** im migrierten Pfad unmöglich (Pack-Pflicht,
  fail-closed). D3 beweist zusätzlich, dass kein Prompt/Pack
  Session-Transcript-Felder enthält (Test B).
- **D — Implementer→QA→Reviewer:** strukturell vorhanden; Lücke
  `_default_handoff_record` („wired in D3") — **in D3 geschlossen** (s. §3).
- **E — Restart/Resume:** checkpoint.py + `latest_checkpoint` Integration
  vorhanden; D3 beweist die Unabhängigkeit von Session-History (Test C + CASE 9).
- **F — Stale:** `checkpoint_references_valid` mit Pflicht-`current_facts`;
  D3 beweist die Fälle integriert (Test H + CASE 6/7).
- **G — Amplification:** D1-Dedup (stabile content-Hashes); D3 beweist
  dedup + tatsächlichen Render (Test I + CASE 12).
- **H — Provider-neutrale Metriken:** `estimate_tokens`/`render_pack` + Item-Zählung
  + Expansion-Reason messbar ohne Provider-Kopplung (alle Cases messen).

## 2. Was D3 gemessen hat (Auszug; vollständig in PHASE_D_CONTEXT_ACCEPTANCE.md)

Provider-neutrale Pack-Größen (token estimate = `len(render)//4`):

| Fixture | Rolle | token_count | soft | hard | items | expansion |
|---|---:|---:|---:|---:|---:|---|
| Simple Task | lead | 80 | 8k | 16k | 3 | – |
| Implementer | implementer | 88 | 24k | 48k | 4 | – |
| QA (mit Handoff) | qa | 92 | 24k | 48k | 4 | – |
| Reviewer | reviewer | 80 | 48k | 96k | 3 | – |
| Integrated E2E (7 Dispatches) | lead→…→lead | 184–424 | 24k–48k | 48k–96k | – | – |
| Oversized optional History | qa | 77 (getrimmt) | 8k | 16k | 4 | – |
| Security Review | reviewer | 10389 | 8k | 16k | 101 | SECURITY_REVIEW |

Budget-Evidence: überschüssige optionale History deterministisch getrimmt
(`budget_estimated` 10077 → `token_count` 77); dedup (2 identische 8k-Facts → 1
Item); Expansion nur mit persistiertem reason code.

## 3. Minimale Fixes (nur BESTÄTIGTE Integrationslücke aus D2)

1. **`argent_core/artifact_refs.py` (neu):** bounded Datei-Hash-/Excerpt-Helfer
   (`sha256_file` mit 4-MiB-Cap, `bounded_excerpt` 4 KiB, `resolve_ref_within`
   mit Traversal-/Symlink-Abwehr). Kein Shell, kein Provider, kein unbounded Read.
2. **`handoff.py`:** `HandoffArtifact.revision` (bounded, 64) ergänzt (canonical
   doc + Store-Serialisierung + Validierung); `build_bounded_artifact_refs`
   (Pfad relativ zum Worktree + full-file sha256 + bounded Excerpt + Revision;
   best effort, max. 32 Refs).
3. **`supervisor.py` `_default_handoff_record`:** erzeugt jetzt Diff-/Artifact-Refs
   MIT Hashes/Revision über `GitProvenanceProvider` (head/base) + bounded
   Datei-Hashes aus `envelope.changed_files`/`tests_run`; füllt
   `evidence.commit_refs`/`diff_refs`. Fehlende Git-/Datei-Info → Refs weggelassen,
   Handoff bleibt gültig (best effort, nie blockierend).
4. **`checkpoint.py` `CheckpointStore.current_facts`:** `artifact_hashes` wird
   jetzt (bounded, best effort) aus den declared Ref-Referenzen des letzten
   Checkpoints berechnet — damit die Hash-verifizierte Stale-Erkennung über einen
   Restart hinweg funktioniert (nötige Folge des Handoff-Hash-Fixes, sonst würde
   ein Checkpoint mit Hash-Refs beim Resume fälschlich STALE).

Keine neue Context-Architektur, keine Phase-E-Funktionen, kein Schema-Change
(SCHEMA_VERSION bleibt 13; die Handoff-Revision ist ein additives JSON-Feld der
bestehenden `handoffs_v2`-Spalte).

## 4. Legacy-Inventar

- **MIGRATED (produktiv):** `_perform_spawn_run` (einziger produktiver
  Spawn-Pfad), Pack-Pflicht.
- **LEGACY (nicht produktiv):** `_build_message_file(pack=None)`-Zweig (keine
  produktiven Caller) + `smoke/*.py`-Skripte (manueller Betrieb, bauen ihren
  Prompt selbst). Details in PHASE_D_CONTEXT_ACCEPTANCE.md §Legacy-Inventar.

## 5. Test-/Verifikationsergebnis

- D3-Tests: **79** (`test_phase_d3_*.py`): Acceptance 15, Artifact-Refs 12,
  Flow 3, Hardening 18, Recovery 6, Regression 7, **Fix-Round 18** (F1–F4).
- D2+D1: **164 grün**. C (c1/c2/c3): **296 grün**. B (b1–b4): **166 grün**.
- Full Suite (`--ignore=e2e-fixture`): **1961 grün** (~46 s).
- `grep shell=True argent_core/` (non-test): **keine**. `git diff --check`: **sauber**.

## 6. Offene Punkte / Entscheidungen

- **Cross-Slot-Dedup (Fakt vs Artifact vs History) wird bewusst NICHT gemergt:**
  unterschiedliche Trust-Slots tragen unterschiedliche Autorität; ein Merge wäre
  ein stiller Autoritätswechsel. Dokumentiert + getestet (`test_cross_slot_not_merged_fail_closed`).
  Die sichere (fail-closed) Interpretation der Amplification-Anforderung.
- **`_artifact_hashes` cap 4 MiB/Datei:** größere Dateien ergeben keinen Hash →
  Stale-Erkennung fail-closed (kein unbounded Read). Grenze dokumentiert.
- E2E (CASE 15/A) nutzt einen echten git-Worktree + Fake-Enforcer/Governor; der
  D3-Provider (`D3AutoProvider`) meldet für ungebundene Dispatches ein
  autoritatives NOT_FOUND, damit der ECHTE `SPAWN_RUN`-Pfad (Pack-Build) läuft
  (im Gegensatz zu `AutoRunStatusProvider`, der einen bereits laufenden Run
  simuliert und den Spawn-Pfad überspringt).

## 7. D3-Fix-Round (Sol-Review REJECT, F1–F4)

Vier unabhängig im Code BESTÄTIGTE Findings wurden geschlossen:

- **F1 (HIGH, Scope-/Secret-Verifikation):** `build_bounded_artifact_refs`
  verweigert jetzt per `artifact_refs.is_forbidden_ref` secret/verborgene Pfade
  (`.env`, `*.pem`, `*.key`, `credentials*`, `id_rsa`/…, `token*`, `secrets`,
  `.ssh`/`.gnupg`/`.config`/`keyrings`, Punktdateien) und übernimmt nur Pfade,
  die autoritativ bestätigt sind.  Der Supervisor berechnet den Write-/Diff-Scope
  aus der Broker-Write-Evidence (`APPLY_PATCH_SET.patch_set_json` via
  `_write_scope_paths`) + `git diff --name-only HEAD` (`GitProvenanceProvider.changed_paths`)
  und akzeptiert `tests_run`-Pfade nur im erlaubten Test-Scope (`_in_test_scope`).
  Agent-gesteuerte Scope-Erweiterung ist wirkungslos.
- **F2 (HIGH, harte I/O-Caps):** `sha256_file` liest nur reguläre Dateien
  (`stat.S_ISREG`) mit Byte-Counter (Abbruch bei `max_bytes + 1`, Growth/Race-capped);
  alle öffentlichen Parameter werden geklemmt (`max_refs` ≤ 32,
  `max_excerpt_bytes` ≤ 4 KiB, `max_bytes` ≤ 4 MiB); `bounded_excerpt` liest
  hart-capped (`max_bytes + 1` zur Truncation-Erkennung).
- **F3 (MEDIUM, SKIP statt leerem Hash):** unauflösbare/nicht vollständig
  hashbare Dateien werden vollständig weggelassen (kein `HandoffArtifact` mit
  `content_hash=""`); der Checkpoint übernimmt nur Refs mit validem Hash.
  Kein künstliches `STALE_CONTEXT_REFERENCE` beim Restart.
- **F4 (MEDIUM, ehrliche Acceptance):** Report in UNIT/COMPONENT/INTEGRATED
  aufgeteilt (keine globale Integrationsbehauptung).  Variante A (echter
  Dispatch-Pfad) für CASE 5/6/7/8 ergänzt; reine Builder-/Retrieval-Semantik
  (CASE 4/12/13/14) ehrlich als UNIT/COMPONENT ausgewiesen.

Entscheidung F4: **Kombination** — dispatch-relevante Fälle (5/6/7/8) echt
integriert (Variante A); Core-Semantik ohne produktiven Dispatch-Bezug
(4/12/13/14) ehrlich als Component/Unit (Variante B).
