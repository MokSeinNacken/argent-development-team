# PHASE D2 — Retrieval + Handoffs + Checkpoints

Phase D2 erweitert den D1-Context-Pack-Core um gezielten, bounded lokalen
Kontextabruf, strukturierte Agent-Handoffs und persistente immutable
Checkpoints mit Resume. **Keine Vector DB, keine Embeddings, keine externe
Semantic Search, kein Model Routing (Phase E), kein neuer Provider.**

## 1. Analyse-Antworten (Supervisor, read-only)

* **A — Vertrauenswürdige lokale Quellen:** Job-Facts (Store),
  Context-Pack-Metadaten (`context_packs`), immutable Argent-Artifakte
  (`~/.local/share/argent/`), aktueller Job-Worktree (kanonischer Pfad aus
  Worktree-Binding/`GitProvenanceProvider`), explizit task-gebundene
  Repo-Dateien, strukturierte Handoffs/Checkpoints. Ausgeschlossen: `~/.ssh`,
  `~/.config`, `~/.gnupg`, Keyrings, `/etc`, `/proc`, `/sys`, `/dev`, fremde
  Repos, Web/Mail/Telegram.
* **B — Retrieval-Arten ohne Vector DB:** `EXACT_REF`, `FILE_EXCERPT`,
  `SYMBOL_OR_TEXT_MATCH` (deterministische Suche in bounded Roots),
  `ARTIFACT_LOOKUP`, `FACT_LOOKUP`, `HANDOFF_LOOKUP`, `CHECKPOINT_LOOKUP`.
* **C — Agent-Ergebnis-Weitergabe:** `result_json` ist bereits strukturiert;
  D2 ersetzt keine großen Textflüsse, sondern ergänzt strukturierte Handoffs.
* **D — Handoff-Integrationspunkt:** `Core._sequence_handoff` (bestehender
  minimaler Handoff) + D2-HandoffRecord-Erzeugung im Supervisor-Konsum-Pfad
  (nach konsumiertem Agent-Result, `_default_handoff_record`).
* **E — Checkpoint-Inhalt:** identity, workflow (primary_state/step/attempt/
  queue-Metadaten), context (letzter pack_id/hash, required source refs,
  artifact refs, handoff refs), code (worktree/repo identity, base/head
  revision), progress (bounded milestones), integrity (canonical hash) —
  ausreichend für reproduzierbaren Neubau eines Context Packs.
* **F — Stale-Erkennung:** Artifact fehlt / Hash verändert, Worktree-head ≠
  Referenz, unbekannte Handoff-/Pack-Ref, Checkpoint-Hash falsch, falsche
  job_id/lineage → `STALE_CONTEXT_REFERENCE` / `CONTEXT_CHECKPOINT_INVALID`.
* **G — Referenz vs. Excerpt:** große Dateien/Diffs/Logs → Referenz + Hash +
  optional kleiner bounded Excerpt (truncation marker); Owner-Objective/
  Acceptance/Policy immer vollständig REQUIRED (D1).

## 2. Module

| Datei | Inhalt |
|---|---|
| `argent_core/retrieval.py` (NEU) | `RetrievalType`, `RetrievalRequest`, `RetrievalResult`, `RetrievalPolicy` (Allowlist-Roots + Denylist, globale Limits), `RetrievalEngine` (realpath-Prefix-Check, Symlink-Escape fail-closed, bounded top-N deterministisch, keine Shell) |
| `argent_core/handoff.py` (NEU) | `HandoffRecord` v1 (result/artifacts/evidence/next_step/provenance), `validate_handoff_record` (trust_class zwingend `AGENT_RESULT`, Policy-/Owner-Marker → ValueError), `build_handoff_record`, `handoff_content_hash` |
| `argent_core/checkpoint.py` (NEU) | `CheckpointRecord` v1 (workflow/context/code/progress/integrity), immutable INSERT-only, sequentielles `checkpoint_no`, fenced creation (owner+lease_epoch), `latest_checkpoint`, `validate_checkpoint_integrity`, `checkpoint_references_valid` (Stale-Detection), `resume_context` (neuer D1-Pack, keine rohe History) |
| `argent_core/context_handoff_integration.py` (NEU) | `build_pack_with_retrieval`: Retrieval-Ergebnisse + Handoff-Refs → D1-ContextBuilder (TrustClass lokal: TRUSTED_ARTIFACT/TRUSTED_LOCAL_FACT/AGENT_RESULT), D1 bleibt einzige Budget-/Integrity-Autorität |
| `argent_core/store.py` (GEÄNDERT) | SCHEMA_VERSION 12→13 additiv: `handoffs_v2` (bounded result/artifacts/evidence/next_step/provenance JSON + content_hash), `checkpoints` (workflow/context/code/progress JSON + content_hash, UNIQUE(job_id, checkpoint_no)) |
| `argent_core/supervisor.py` (GEÄNDERT) | D2-Integration: `build_pack_with_retrieval` im Pack-Build, `_default_handoff_record`/`_handoff_builder` nach konsumiertem Result, `_create_checkpoint` an bounded Triggern (nach Agent-Step / vor WAITING_EXTERNAL), Checkpoint-Store/Handoff-Builder/Retriever-Injection für Tests |

## 3. Retrieval-Policy

- Roots: Allowlist (job-worktree kanonisch, `~/.local/share/argent/`) + Denylist
  (`~/.ssh`, `~/.config`, `~/.gnupg`, Keyrings, `/etc`, `/proc`, `/sys`,
  `/dev`). Jeder Request: `job_id`, `dispatch_id`, `source_type`,
  `authorized_root`, `query/reference`, `max_results`, `max_bytes`,
  `max_excerpt_bytes`.
- Path-Sicherheit: `os.path.realpath` + Prefix-Check gegen authorized_root;
  `..`, absolute Fremdpfade, Symlink-Escape → fail-closed
  (`RETRIEVAL_ROOT_DENIED` / `RETRIEVAL_PATH_ESCAPE`).
- Deterministische Ordnung (lexikografisch bei gleichem Treffer), bounded
  Top-N, kein LLM als Safety-Entscheidung. Kein unbounded
  „read entire repository".

## 4. Handoff-Schema (v1)

`handoff_id, job_id, source_dispatch_id, source_role, created_at` +
`result` (outcome/status bounded, key_observations, decisions,
unresolved_questions) + `artifacts` (refs+hashes+bounded excerpts) +
`evidence` (test/result refs, commit/diff refs, trusted facts vs observations
getrennt) + `next_step` (proposed capability, required refs) + `provenance`
(trust_class zwingend `AGENT_RESULT`, content_hash).

**Handoff ≠ Policy:** ein Handoff kann niemals Owner-/Policy-Regeln tragen;
Versuch → `ValueError`. Context Builder übernimmt Handoff-Refs nur als
`AGENT_RESULT` (D1), nie als REQUIRED/Policy.

## 5. Checkpoint-Schema (v1) + Fencing

`checkpoint_id, job_id, checkpoint_no, created_at` + workflow + context
(pack_id/hash, source refs, artifact refs, handoff refs) + code (repo
identity, base/head) + progress + canonical hash.

- **Immutable:** INSERT-only; neuer Stand → `checkpoint_no+1`. Kein stilles
  Umschreiben.
- **Fenced:** nur aktueller Holder (`owner_instance_id` + `lease_epoch`) darf
  den nächsten Checkpoint erzeugen; stale Supervisor → abgewiesen.
- **Stale-Detection:** `checkpoint_references_valid` prüft Artifact-Existenz/
  Hash, Worktree-head vs. Referenz, Handoff-/Pack-Refs, job_id/lineage.
- **Resume:** `resume_context` baut aus neuestem gültigen Checkpoint + aktuellen
  trusted Facts einen NEUEN Context Pack über den D1-Builder — keine rohe
  Session-History. Ungültig/unvollständig → fail-closed
  (`CONTEXT_CHECKPOINT_INVALID` / `STALE_CONTEXT_REFERENCE`).

## 6. D1-Integration

D2 umgeht D1 nicht: Retrieval-Ergebnisse und Handoff-Refs fließen als
Context-Items in den D1-`ContextBuilder`; REQUIRED bleibt erhalten,
Budget-Enforcement bleibt bindend, `validate_context_pack` läuft vor
Persistenz/Dispatch. Ungültiger Pack → kein Dispatch (bestehender
`context_build_failed`-Pfad, `ErrorClass.CONTEXT`, kein CODE_FAILURE, kein
Legacy-History-Fallback).

## 7. Acceptance-Cases (alle deterministisch, grün)

CASE 1 IMPLEMENTER→QA · CASE 2 QA→REVIEWER (selektierte Evidence + Handoff,
keine komplette History) · CASE 3 RESTART (Checkpoint → neuer Pack) ·
CASE 4 CODE CHANGED (stale erkannt) · CASE 5 PROMPT INJECTION (~/.ssh →
keine Root-Erweiterung) · CASE 6 OVERSIZED FILE (bounded Excerpt, D1-Budget)
· CASE 7 MISSING ARTIFACT (fail-closed, kein Legacy-Fallback) ·
CASE 8 DETERMINISM (gleiche Auswahl → gleicher content hash).

## 8. Test-Evidence (vom Supervisor unabhängig ausgeführt)

- D2 targeted: **66 PASS** · D1: **73 PASS** · Phase C (C3+C2+C1): **296 PASS**
  · Phase B: **166 PASS** · Full Suite `--ignore=e2e-fixture`: **1857 PASS**
  (~31 s) · kein `shell=True` im Produktcode · `git diff --check` sauber.
- Der D2-Writer-Lauf wurde durch einen LLM-Infrastruktur-Timeout abgebrochen,
  nachdem die Implementierung vollständig in den Worktree geschrieben war;
  der Supervisor hat die Arbeit verifiziert und ausschließlich den
  mechanischen Schema-Versions-Pin (12→13) in `test_phase3c_approval_core.py`
  sowie diese Doku ergänzt. Keine Code-Änderungen durch den Supervisor.

## 9. Explizit NICHT implementiert

D3 (integrierter Phase-D-Abschlussnachweis) und Phase E (Model Routing):
NICHT implementiert. Keine Vector DB, keine Embeddings, keine externe
Semantic Search, keine neuen Provider, kein Claude/GLM, keine Adaptive Roles,
keine Parallelisierung, kein Background Service, keine neuen externen Rechte.
