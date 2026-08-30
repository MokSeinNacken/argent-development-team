# Phase 3D — Spec: Read-Only Supervisor Visualizer Integration

Status: **PHASE3D_SPEC_READY / IMPLEMENTIERT** (2026-08-30).
Implementierung abgeschlossen in isoliertem Visualizer-Worktree
(`/home/pc/projects/system-visualizer-3d`, Branch `phase-3d-visualizer`)
und Argent-Repo (`argent_core/visualizer_snapshot.py`); Haupt-Worktree des
Visualizers (Mail-Agent-Arbeit) unveraendert. Diese Spec ist die
Referenz fuer die Implementierung; Abweichungen sind unten kanonisch
dokumentiert.

## 0. Ausgangslage (reconciled, 2026-08-30)

- Argent HEAD `17b51b6` (Phase 3C-B2A), Working Tree: nur untracked
  `docs/PHASE3CB2A_UPSTREAM_PREP_PR.md`, kein Remote, kein Push.
- Visualizer `/home/pc/projects/system-visualizer`: Branch
  `feat/mail-agent-integration`, HEAD `d7e4c2f`. **Vorbestehende
  uncommittete Änderungen** (dürfen NICHT angefasst/committet werden):
  `backend/app/models/events.py` + `schemas/event.schema.json`
  (message_ref 6→8 hex), `tests/e2e/test_dashboard.py`,
  `docs/MAIL_AGENT_INTEGRATION.md`, `tests/integration/test_mail_agent_contract.py`.
- OpenClaw-PR #133267 (B2A) bleibt unberührt; Phase 3C-B2B blockiert
  (WAITING_UPSTREAM).

## 1. Architektur-Entscheidung (bestätigt)

**Variante (b): sanitisierter, Supervisor-seitig erzeugter JSON-Snapshot.**
Der Snapshot-Publisher gehört zur **Argent-Trust-Domain**. Der Visualizer
liest ausschließlich den Snapshot und erhält weder Pfad noch Zugriff auf
die produktive Argent-SQLite.

- **Explizit verworfen:** (a) direkter Visualizer-SQLite-Zugriff (Leserechte
  auf gesamtes Ledger inkl. binding_hash/Challenges/Agentenantworten;
  Sanitization erst nach Prozessgrenze; Kopplung an internes Schema; bei
  DELETE-Journal-Mode verzögert jede Read-Transaktion Writer-Commits;
  `immutable=1` ungeeignet für live beschriebene DB), und der bestehende
  `integrations/emitter.py`-Pfad (POST-Schreibpfad, Visualizer-SQLite-Write,
  Schema-Erweiterung, WS-Verteilung — verletzt „kein SQLite-Write auf dem
  Argent-Integrationspfad“).
- **DB-Fakt (verifiziert):** Argent-Store nutzt **DELETE journal mode**
  (kein WAL; `store.py:597` ohne journal_mode-Pragma). Bekannte
  Fixture-DBs `smoke/smoke.db`, `smoke/phase2b.db` = journal_mode=delete,
  keine `-wal`/`-shm`. Kein produktiver Standard-DB-Pfad existiert im Code
  (`Core.__init__(db_path)` explizit; keine Env-Var/Config-Datei).

### Publisher-Regeln (Argent-Trust-Domain)

```python
sqlite3.connect(f"file:{resolved_db_path}?mode=ro", uri=True,
                isolation_level=None, timeout=0)
# zwingend:
PRAGMA query_only = ON; PRAGMA busy_timeout = 0; PRAGMA temp_store = MEMORY;
BEGIN;  -- deferred read transaction
-- ausschließlich explizite SELECT-Spaltenlisten, niemals SELECT *
COMMIT;
```

- Niemals `immutable=1` an einer live beschriebenen DB.
- Niemals `BEGIN IMMEDIATE`/`BEGIN EXCLUSIVE`; kein `ATTACH`, kein DDL,
  kein `PRAGMA journal_mode=...`.
- Harte Zeitgrenze: Ziel < 50 ms, max. 100 ms pro Publikation; bei
  `SQLITE_BUSY`/Timeout Publikation **überspringen**, nie Supervisor
  blockieren.
- Transaktion schließen, bevor Sanitization/JSON/Datei-I/O beginnt.
- Bei WAL (falls später): normale SQLite-Leselogik muss `-wal`/`-shm`
  berücksichtigen dürfen; Sidecars nie separat kopieren/ignorieren.

## 2. Datenfluss

```
Argent SQLite (DELETE journal, query_only=ON)
  → Argent Snapshot Publisher (neuer, eng begrenzter Code in Argent-Trust-Domain)
  → Sanitization (strikte Feld-Allowlist + ID-Kürzung)
  → atomarer Snapshot-Austausch (Tempfile + os.replace; Version+generated_at)
  → Visualizer Snapshot Reader (liest NUR die Snapshot-Datei)
  → GET /api/v1/argent/snapshot   (NEU, disjunkt zu /api/v1/snapshot)
  → Browser-Polling mit ETag/If-None-Match (kein WS für Argent-Daten)
```

Kein Schreibpfad: Visualizer → Supervisor existiert nicht. Der bestehende
`GET /api/v1/snapshot` (System-Event-Store-Snapshot) bleibt unverändert.

## 3. Sanitization-Matrix (schema-verankert, verifiziert)

**Quelle der Wahrheit: tatsächliche CREATE-TABLE-Spalten in
`argent_core/store.py` (SCHEMA_VERSION "6").**

| Ansicht | Erlaubt (beschreibend) | Verboten / nie ins UI |
|---|---|---|
| System Status | supervisor_jobs.status, workflow_state, next_action, last_progress_at, last_error_code, retry_count, terminal, next_wake_at | expected_dispatch_id, agent_id, session_id, run_id (nur gekürzt), owner_gate_id |
| Current Workflow | tasks.state (16 Zustände, s.u.), expected_role, rework_cycle, recovery_state | — |
| Tasks | task_id (gekürzt 8–12), state, resume_state, source_class, risk_class, created_at/updated_at, external_actions_policy | **title/description ausgeschlossen** (B3: alternative ID = gekürzte task_id) |
| Agents | Rolle (Lead/Analyst/Implementer/QA/Reviewer), idle/running aus letztem Dispatch, expected_model_class, actual_model, Dispatch-Status | child_session_id, openclaw_run_id (nur gekürzt), result_json |
| Owner Approvals | id (gekürzt), action, scope, status, requested_by, created_at/decided_at/consumed_at/expires_at, source_class | **binding_hash, token_hash, execution_id, gate_id, challenge_id** |
| Notifications | notification_type, status, attempt_count, event_ref (gekürzt), last_attempt_at, last_error_code | **payload_json, payload_hash, claim_token, dedup_key, gate_id** |
| System Health | DB erreichbar (Publisher-Status), Snapshot-Alter (fresh/stale), Visualizer-Gesundheit (/healthz), Git-HEAD des Argent-Repos (nur Hash, aus Snapshot-Publisher), Recovery-State, letzter Test-Run | alles andere (kein Live-Gateway-Check — Scope-Freigabe v1; Gateway-Status wird nicht live geprüft) |
| Phase Status | statische Konfiguration (2C/3A/3B/3C-A/3C-B1/3C-B2A GREEN, 3C-B2B WAITING_UPSTREAM, 3D ACTIVE) + Live-Status aus supervisor_jobs | — |

**Canary-/Verbotsfeld-Liste (an tatsächliche Spalten gekoppelt — Fix B1):**
`binding_hash`, `token_hash`, `claim_token`, `result_json`,
`context_summary_json`, `payload_json` (events + outbox),
`event_meta_json`, `patch_set_json`, `authorization`, `password`,
`api_key`, `cookie`. **Keine Phantom-Felder** (chat_id/sender_id/token/
prompt/response/body existieren NICHT als Spalten — nicht als Canary
führen, sondern als Schutzregel: „niemals neue Spalten ohne Sanitizer-
Review ausgeben“).

**ID-Kürzung (8–12 Zeichen):** task_id, approval_id, dispatch_id,
handoff_id, session_id, run_id, child_session_id, openclaw_run_id,
event_ref.

**Telegram-Daten (verifiziert):** Argent speichert NIE echte Chat-/Sender-
IDs (nur Booleans `chat_authorized`/`sender_authorized` +
`update_id`/`message_date` in `telegram_update_log`). `update_id`/
`message_date` sind öffentliche Telegram-Metadaten — dürfen als Counts/
Zeitstempel erscheinen, aber nicht als Einzelwerte verknüpft mit
Identitäten. `approval_challenges` enthält nur `token_hash` (SHA-256),
nie den Rohtoken — der Hash bleibt trotzdem im UI verboten.

## 4. Snapshot-/Polling-Modell

- Snapshot-Datei: versioniertes JSON (`snapshot_version`, `generated_at`,
  `source_schema_version`), atomar via `tempfile + os.replace`.
- Publisher-Frequenz: konfigurierbar, Default **5 s** (kein bestehender
  2s-Mechanismus existiert — Fix A3: neuer Mechanismus, explizit
  spezifiziert). Ort: Argent-seitiger Publisher (siehe Owner-Gates).
- Visualizer: `GET /api/v1/argent/snapshot` liest die Datei, liefert
  `ETag` (Hash des Inhalts); Browser pollt mit `If-None-Match` (304).
- Staleness: `generated_at` älter als N×Intervall (Default 3×) →
  Frontend zeigt Banner „STALE / Supervisor nicht aktiv“, Daten bleiben
  lesbar.
- Fehlt die Datei: HTTP 200 mit `status=missing` + Frontend-Status
  „kein Snapshot“ (kanonische Semantik: fail-safe, kein 404 — der Poller
  behandelt fehlende Datei als Zustand, nicht als Fehler).

## 5. UI-Aufbau (8 Sektionen, bestehendes Prozess-Frontend erweitert)

Neues eigenständiges Argent-Panel im bestehenden Prozess (Fix A2:
separate Route `/api/v1/argent/snapshot` + separates Frontend-Panel;
bestehende Mail-Agent-Ansichten unverändert). Sektionen:

1. **System Status** — Supervisor-Status, letzter Reconcile
   (last_progress_at), aktueller Job, Zähler queued/running/blocked/
   failed, offene Owner-Gates (Anzahl), letzter Fehler (last_error_code).
2. **Current Workflow** — Pipeline OWNER→Lead→Analyst→Implementer→QA→
   Reviewer→DONE; aktive Rolle visuell markiert. **Alle 16 Zustände**
   (Fix B2): Main-Path (NEW, PLANNING, ANALYZING, LEAD_DECISION,
   IMPLEMENTING, TESTING, REVIEWING, FINAL_DECISION, DONE) +
   Sonderzustände als Badges (REWORK, BLOCKED, OWNER_APPROVAL_REQUIRED,
   PAUSED, FAILED, RECOVERING, CANCELLED). REWORK-Rückkante von
   PLANNING/LEAD_DECISION/TESTING/REVIEWING/FINAL_DECISION → REWORK →
   {PLANNING, IMPLEMENTING} (state_machine.py `_STATIC`, verifiziert).
3. **Tasks** — gekürzte task_id, state, Rollen-Historie (letzter
   Dispatch je Rolle), Modell, Startzeit, Dauer (aus started_at/consumed_at,
   keine Synthese bei fehlendem consumed_at), retry_count, Ergebnis
   (result_status), letzter Fehler.

   *Scope-Freigabe (v1): Die Rollen-Historie wird auf den **letzten
   Dispatch je Task** reduziert (Rolle, Modell, Startzeit, Status); eine
   vollständige Historie über alle Dispatches je Task entfällt in v1
   (keine zusätzliche Query-Last). Die Task-Dauer wird aus
   ``started_at``/``consumed_at`` des letzten Dispatches abgeleitet;
   fehlt ``consumed_at``, entfällt die Dauer (keine Synthese).*
   `tasks.source` wird NICHT publiziert (nur `source_class`) — der Reader
   lehnt `source`-Keys als malformed ab.
4. **Agents** — 5 Rollen: idle/running, aktueller Task, Modell, letzter
   Run (gekürzt), letzter Status.
5. **Owner Approvals** — nur Status-Kategorien (OPEN/PENDING, APPROVED,
   REJECTED, CONSUMED, EXPIRED) + Zähler; action/scope/Zeitstempel ohne
   Secrets.
6. **Notifications** — Outbox-Status (SENT/PENDING/FAILED/DISCARDED) +
   Zähler + attempt_count; keine Payloads.
7. **System Health** — Publisher/DB erreichbar, Snapshot-Alter,
   Visualizer-/Gateway-Status, Git-HEAD (Hash), Test-/Recovery-Indikatoren.

   *Scope-Freigabe (v1): Der Visualizer-/Gateway-Status wird nicht live
   geprüft (kein neuer Netzwerkzugriff in v1); „db_reachable“ reflektiert
   den Publisher-Erfolg, der Visualizer-Status ist implizit über die
   Antwort selbst (HTTP 200 = erreichbar). Recovery-/Test-Indikatoren
   kommen aus ``supervisor_jobs.recovery_state`` und der letzten
   ``test_runs``-Zeile (nur id/result/created_at).*
8. **Phase / Project Status** — statische Konfiguration (Phasen-Marker)
   + Live-Status.

## 6. Failure Modes

| Fall | Verhalten |
|---|---|
| Argent-DB zu / beschäftigt | Publisher überspringt (busy_timeout=0); letzter guter Snapshot bleibt; UI „STALE“ |
| Snapshot fehlt | HTTP 200 + status=missing; UI „kein Snapshot“ (kein 404) |
| Snapshot veraltet | UI-Banner „STALE / Supervisor nicht aktiv“ |
| Schema-Migration (V6→V7) | Snapshot enthält `source_schema_version` (im JSON, Version validiert); neue Spalten nur nach Sanitizer-Review |
| Visualizer down | nichts (kein Einfluss auf Supervisor) |
| Gateway down | Argent-Daten unabhängig; kein Live-Gateway-Check in v1 (Scope-Freigabe §5.7) — Visualizer-Erreichbarkeit implizit via HTTP 200 |
| Leere DB | leere Sektionen mit „keine Daten“ |

## 7. DB-Lock-Vermeidung (verifiziert, Fix C)

- Nur Publisher berührt die DB; ausschließlich `mode=ro` + `query_only=ON`.
- Kurze deferred Read-Transaktion, explizite Spalten, Zeitgrenze 100 ms.
- Kein BEGIN IMMEDIATE/EXCLUSIVE, kein DDL, kein ATTACH.
- DELETE-Journal-Mode: Read-Transaktionen können Writer kurz verzögern →
  daher minimale Transaktionsdauer + Skip-on-Busy (niemals Supervisor
  blockieren).
- Visualizer hat **keinen** SQLite-Zugriff auf die Argent-DB.

## 8. Secret-Leak-Schutz (Defense-in-Depth)

1. Publisher-Sanitizer: Feld-Allowlist + Verbotsfeld-Canary (schema-
   verankert) + ID-Kürzung; Unit-Test garantiert: Verbotsfelder nie im
   Snapshot.
2. Snapshot-Schema closed (extra=forbid analog Visualizer-Schema v1.0).
3. UI rendert nur Allowlist-Felder (zweite Filterung im Frontend).
4. E2E-Security-Test: HTML/WS-Antworten enthalten keine Verbots-Tokens.
5. Kein Credential-/Token-/Chat-ID-Pfad im gesamten Argent-→UI-Pfad.

## 9. Acceptance Tests

- Unit: Sanitizer-Verbotsfeld-Test (jede Canary-Spalte → Assertion);
  ID-Kürzung; Status-Enum-Validierung (16 Zustände).
- Unit: Publisher-Lock-Regeln (mode=ro/query_only/kein IMMEDIATE) gegen
  Fixture-DB (DELETE journal).
- Integration: Snapshot-Reader gegen Fixture-Snapshot (Version, ETag,
  Staleness, missing→HTTP 200 + status=missing, kein 404).
- E2E: Dashboard zeigt 8 Sektionen; Workflow-Pipeline mit aktiver Rolle;
  Phase-Status.
- Security: kein Secret/Token/Chat-ID im HTML, in API-Antworten, in WS.
- Regression: bestehende Visualizer-Suite (212 Tests) + Argent-Suite
  (1227) unverändert grün; B2A-Guard unverändert.

## 10. Bestätigte Amendments (aus Analyst-Review, Main-verifiziert)

- **A1 (MEDIUM):** Neuer Pfad `GET /api/v1/argent/snapshot` (disjunkt zum
  bestehenden `/api/v1/snapshot`). Bestätigt.
- **A2 (HIGH):** Produktentscheidung: Argent-Panel im bestehenden
  Visualizer-Prozess, aber als eigenständige Datenquelle + eigenes Panel +
  neue Route. Bestätigt.
- **A3 (LOW):** Polling-Modell explizit: Frontend-Poll mit ETag, Intervall
  konfigurierbar (Default 5 s), Backoff; kein WS für Argent-Daten.
  Bestätigt.
- **B1 (HIGH):** Canary-Liste an tatsächliche Spalten gekoppelt;
  `*_json`-Spalten + binding_hash/token_hash/claim_token als Kern;
  session_id/run_id/child_session_id/openclaw_run_id in Kürzungsregel.
  Bestätigt (Code-Grep).
- **B2 (MEDIUM):** Mapping aller 16 TaskStates + REWORK-Quellen.
  Bestätigt (models.py:53, state_machine.py:57-64).
- **B3 (LOW):** Task-Identifier = gekürzte task_id (title ausgeschlossen).
  Bestätigt (store.py:96 title NOT NULL).
- **C (HIGH):** Kein periodischer Loop existiert (Supervisor wake-
  scheduled, NotificationDelivery bounded pass, Recovery pure function,
  Smokes one-shot). Publisher-Hosting ist **Owner-Gate**. Bestätigt.

## 11. Implementierungsreihenfolge (für die Folge-Phase, nach Freigabe)

1. **Snapshot-Schema + Sanitizer** (Argent-seitig, rein additiv, eigene
   Module `argent_core/visualizer_snapshot.py` o.ä.) mit Unit-Tests.
2. **Publisher** (read-only DB-Zugriff + atomarer Snapshot-Write) +
   Hosting-Entscheidung (Owner-Gate, s.u.).
3. **Visualizer-Reader-Route** `GET /api/v1/argent/snapshot` + ETag +
   Fixture-Tests.
4. **Frontend-Panel** (8 Sektionen, Polling, STALE-Banner) + E2E.
5. **Security-Tests + volle Regression** (Visualizer 212 + Argent 1227 +
   bwrap + Fake-Smoke + B2A-Guard).
6. **Lokaler Commit** (kein Push), Marker, STOP.

## 12. Benötigte Owner-Gates (vor Implementierung)

- **GATE-1 (zwingend): Publisher-Hosting.** Es existiert kein dauerhaft
  laufender Argent-Host-Prozess (nur One-Shot-Smokes). Optionen:
  (a) Publisher als Teil des Supervisor-Loops (zu selten — nur bei
  Reconciliation), (b) eigener leichtgewichtiger Publisher-Prozess/Thread
  im Argent-Runtime (neuer Service → Gate), (c) systemd/Cron-Timer (→
  Gate, laut Auftrag ausdrücklich erforderlich). Empfehlung: (b) als
  eigenständiger, eng begrenzter Publisher mit eigenem Gate; kein
  systemd/Cron ohne separates Gate.
- **GATE-2 (klärend): „kein SQLite-Write“-Scope.** Falls prozessweit
  gemeint (Visualizer schreibt heute eigene Events/Metriken/Retention in
  seine eigene DB), ist ein `ARGENT_READONLY`-Runtime-Profil nötig, das
  Store/Collector/Retention/`POST /api/v1/events` nicht initialisiert.
  Falls nur „kein Write auf Argent-DB/Integrationspfad“ gemeint: entfällt.
- **GATE-3 (Info, kein Gate):** Snapshot-Dateipfad/-format festlegen
  (lokal, z. B. `~/.openclaw/workspace/argent-snapshot.json` o.ä.).

Kein systemd/Cron, kein neuer Service, keine Netzwerkfreigabe ohne
Owner-Freigabe. Kein Push.
