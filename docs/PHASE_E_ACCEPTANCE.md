# PHASE E ACCEPTANCE — Integrierte E1–E3-Zusammenfassung

**Branch:** `phase-e3-benchmarks-validated-fallback` (Base `f546b68` = E2 GREEN).
**Datum:** 2026-09-02

Phase E ist abgeschlossen. Diese Datei fasst die integrierte Architektur und das explizite
Phase-E-Exit-Verdikt zusammen.

---

## 1. Finale Architektur (E1 + E2 + E3)

- **E1 — Provider-/Model-Abstraktion** (`model_registry.py` + `models.json`/`providers.json`):
  Provider/Modell sind Daten; Rollen sind Fähigkeiten, Modelle austauschbare Implementationen.
  Immutable, fail-closed, `benchmarked:false`-Claims (keine Fakten). Keine Secrets, keine
  Agent-Mutation, kein Live-Call.
- **E2 — Deterministischer adaptiver Router** (`model_router.py` +
  `routing_policy_v1.json`): bootstrap-only Autorisierung, Floor-before-Cost,
  Minimum-Sufficient-Ranking (Kosten/Latenz nur Tiebreaker), bounded monotone
  Escalation-Ladder (0 ROUTINE → 4 OWNER), Provider≠Capability-Distinktion,
  Reviewer-Independence. Kein LLM, keine Agent-Prosa.
- **E3 — Evidence + Validated Fallback + Provenienz** (`evidence_registry.py` +
  `benchmarks_v1.json` + additiver Router/Policy/Schema): unabhängiger Evidence-Filter,
  deterministischer Availability-Fallback, versionierte Provenienz.

## 2. Routing-Invarianten

1. **FILTER zuerst, DANN Ranking**: Floor/Reasoning/Context/Tools/Policy-Allowlist/
   Independence/Evidence → Überlebende; danach Minimum-Sufficient (Kosten nur Tiebreaker).
2. **`benchmarked` ≠ autorisiert**; **`policy-erlaubt` ≠ fähig genug**; **fähig ≠
   policy-erlaubt**. Kandidat nur, wenn ALLE unabhängigen Anforderungen erfüllt sind.
3. **Kein Escalation-by-Text**; nur bounded strukturierte Evidence triggert.
4. **Bounded, monoton**: Level steigt nie leise ab; max_auto=3, owner=4 fail-closed.
5. **Provider-Failure ≠ Capability-Failure**: Provider-Outage erhöht keine Capability-
   Escalation und senkt keine Capability-Requirements.

## 3. Evidence-Modell

Bounded `EvidenceStatus` (VERIFIED/PROVISIONAL/UNKNOWN/REJECTED) × task-relevante
`EvidenceCategory` (7 Kategorien), deterministische Capability→Kategorie-Abbildung,
versionierte `benchmarks_v1.json` (fail-closed). Bestandsmodelle PROVISIONAL/UNKNOWN,
nichts VERIFIED, keine Scores. Evidence ist ein **unabhängiger** Filter (fehlend = UNKNOWN
= nie eligible bei PROVISIONAL/VERIFIED-Minimum).

## 4. Fallback-Regeln

Fallback **nur** bei Provider-/Model-Verfügbarkeitsproblemen (`AvailabilitySnapshot`), nach
den harten Filtern, Minimum-Sufficient unter Überlebenden, Independence erneut geprüft,
Evidence erfüllt, Level unverändert. Kein Fallback-Kandidat → `NO_VALID_FALLBACK` → bounded
Backoff (WAIT), nie stiller schwächerer Ersatz, nie Floor-Senkung. Rate-Limit/Transient →
Backoff, kein Fallback. Nie bei Code-Failures/schlechtem Output/unbekanntem Root-Cause/
Security-Review-Reject/unzureichender Evidence.

## 5. Abstraktionsgrenze

Argent-Routing-Entscheidung ≠ OpenClaw-Provider-Konfiguration. Registry = Architektur-
Abstraktion, kein Call-Recht, kein Live-Provider, kein neuer Provider in der Live-Config.

## 6. Escalation + Reviewer-Independence

Escalation-Ladder unverändert (E2). Closing Review immer writer-unabhängig
(`DIFFERENT_MODEL_REQUIRED`), kein SAME_MODEL_ALLOWED-Fallback bei required closing review;
fehlende Writer-Provenienz → fail-closed. Independence übersteht den Fallback.

## 7. Persistierte Provenienz

`RoutingDecision` + `routing_decisions` (Schema 16, additiv erweitert) tragen
`registry_version`, `evidence_version`, `policy_version` („2“) und die unveränderlichen
Inhaltsdigests `policy_hash`/`registry_hash`/`evidence_hash` sowie `inputs_hash` (canonical
sha256 des **vollständigen** bounded Input-Kanons; `inputs_hash` ist Teil der
Decision-Bindung). Gleiche Input-Snapshots → gleiche Decision+Provenienz; jede Inhalts-/
Versions-Änderung ist sichtbar. Der Core bindet alle Decision-Felder gegen die kanonische
Bindung und lehnt manipulierte (auch wohlgeformte) Digests ab.

## 8. Testergebnisse

| Suite | Anzahl | Ergebnis |
|---|---|---|
| E3 (inkl. Fix-Round F1–F4) | 50 | ✅ |
| E2 | 76 | ✅ |
| E1 | 82 | ✅ |
| D1–D3 | 243 | ✅ |
| C1–C3 | 296 | ✅ |
| B1–B4 | 166 | ✅ |
| **Full Suite** | **2169** | ✅ |

## 9. Explizites Phase-E-Exit-Verdikt

**Phase E ist abgeschlossen.** Alle E1–E3-Acceptance-Cases (CASE 1–24 inkl. adversarieller
Fälle) sind mit ehrlicher UNIT/COMPONENT/INTEGRATED-Evidenz grün. Die vier
Sol-Closing-Review-Findings (F1–F4) wurden gebündelt geschlossen: geleaste
NO_VALID_FALLBACK-Backoffs requeuen atomar (F1), ein echter controller-authentisierter
Provider-Availability-Producer speist den bounded-TTL-Snapshot (F2), die Provenienz ist
vollständig reproduzierbar und an den Core-Trust-Boundary gebunden (F3), und die
Evidence-Registry ist bei Duplikat-Keys/Entry-Version fail-closed (F4). Keine offenen Punkte.
Kein Commit/Push durch den Writer (Supervisor committet nach unabhängigem Sol-Closing-Review).
