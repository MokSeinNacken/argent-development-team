# ARGENT DEVELOPMENT TEAM — Phase 1: Deterministic Team-Control-Core (SPEC V1)

Owner-Auftrag: ausschließlich der deterministische Team-Control-Core als neues,
vollständig separates Projekt unter `/home/pc/projects/argent-development-team`.
Keine echten Agenten, kein Docker/Sandbox, keine OpenClaw-/Gateway-Konfiguration,
keine Änderungen an Mail-Agent oder System-Visualizer. Keine externen Aktionen.
Kein Push. Nur Dateien innerhalb dieses Projektverzeichnisses.

Diese Spezifikation ist der verbindliche Architektur-Vertrag (Implementierung + Review).

## 0. Tech-Stack

- Python 3.14 (nur Standardbibliothek + pytest als Test-Harness, nutzerlokal installiert).
- SQLite über `sqlite3` (stdlib), explizite Transaktionen (`BEGIN IMMEDIATE`), Foreign Keys ON.
- Keine Redis/Kafka/Postgres, keine weiteren Runtime-Abhängigkeiten.
- Determinismus: keine Zufälligkeit in der Kernlogik (UUIDs nur als IDs erlaubt).

## 1. Zustandsmaschine (deterministisch, fail-closed)

Hauptpfad-Zustände:
`NEW → PLANNING → ANALYZING → LEAD_DECISION → IMPLEMENTING → TESTING → REVIEWING → FINAL_DECISION → DONE`

Zusatzzustände: `REWORK, BLOCKED, OWNER_APPROVAL_REQUIRED, PAUSED, FAILED, RECOVERING, CANCELLED`

Erlaubte Übergänge (vollständige Tabelle, alles andere ist verboten):

- NEW → PLANNING
- PLANNING → ANALYZING
- ANALYZING → LEAD_DECISION
- LEAD_DECISION → IMPLEMENTING
- IMPLEMENTING → TESTING
- TESTING → REVIEWING | REWORK (Tests fehlgeschlagen)
- REVIEWING → FINAL_DECISION | REWORK (Findings offen)
- FINAL_DECISION → DONE | REWORK
- REWORK → PLANNING
- BLOCKED → RECOVERING
- OWNER_APPROVAL_REQUIRED → (resume_state nach Approval) | BLOCKED (Reject) | CANCELLED
- PAUSED → (resume_state) | CANCELLED
- FAILED → RECOVERING | CANCELLED
- RECOVERING → (resume_state = letzter sicherer Zustand) | BLOCKED | CANCELLED
- Jeder Nicht-Endzustand (außer sich selbst) → BLOCKED | PAUSED | CANCELLED
  (ausgenommen DONE, CANCELLED als Endzustände: keine ausgehenden Übergänge)

Regeln:
- `resume_state` wird pro Task persistiert (Task-Spalte), wenn in OWNER_APPROVAL_REQUIRED/PAUSED/RECOVERING gewechselt wird.
- Unbekannter Von-/Zu-Zustand oder nicht gelisteter Übergang → Exception `InvalidTransition` (fail-closed), kein State-Change, kein Event.
- Jeder gültige Übergang erzeugt genau ein `task.state_changed`-Event.

## 2. Rollen (genau eine aktive Rolle gleichzeitig)

Feste Rollen: `lead, analyst, implementer, qa, reviewer`

Artefakt-Kategorien: `PRODUCT_CODE`, `TEST_CODE`, `OTHER` (Docs/Plan/Decisionen)

Berechtigungen (write = modifizieren, read = nur lesen):

| Rolle     | PRODUCT_CODE | TEST_CODE | OTHER |
|-----------|--------------|-----------|-------|
| lead      | read         | read      | write |
| analyst   | read         | read      | write |
| implementer | write      | write     | write |
| qa        | read         | write     | write |
| reviewer  | read         | read      | read  |

Regeln:
- `start_role(task_id, role)`: blockiert, wenn bereits ein aktiver Role-Run (Status `started`) für den Task existiert → `RoleConflict`. Genau eine aktive Rolle gleichzeitig.
- Role-Run-Lebenszyklus: `started → completed | failed`. `complete_role` erzeugt einen `handoff` zum nächsten Rollenstart.
- Jeder Rollenwechsel erzwingt: vorherige Rolle completed/failed, sonst `RoleConflict`.
- `lead` koordiniert (Transitionen, Decisions, Rollenzuweisung); schreibt keinen Produktcode.

## 3. Gated Autonomy

Aktionsklassen: `AUTONOMOUS`, `OWNER_APPROVAL_REQUIRED`, `FORBIDDEN`

AUTONOMOUS (Beispiele): analyze, implement, run_tests, review, rework, create_local_artifact,
git_local_commit (nur Task-Kontext, kein Push), create_handoff.

OWNER_APPROVAL_REQUIRED (Beispiele): deploy_production, change_secrets, expose_gateway,
modify_allowlist, promote_stable, modify_policy, external_send, install_software,
raise_privileges, enable_self_improvement, production_write.

FORBIDDEN (immer verboten, auch mit Approval/Agentenentscheidung):
bypass_owner_approval, forge_owner_approval, treat_untrusted_as_owner_approval,
disclose_secrets, disable_security_boundary, exfiltrate_data.

Mechanik:
- `request_action(task_id, action, scope, actor_role, source)`:
  - Source muss TRUSTED sein, sonst `UntrustedSource` (gilt für alle Public-APIs).
  - FORBIDDEN → immer blockiert (`ForbiddenAction`), **kein** Approval wird erzeugt, `lead.decision`/Blockade-Event.
  - OWNER_APPROVAL_REQUIRED → Aktion wird blockiert, `owner_approvals`-Request (Status `pending`) erzeugt,
    Task → `OWNER_APPROVAL_REQUIRED` (resume_state = aktueller Zustand), Event `gate.owner_required`.
  - AUTONOMOUS → Ausführung (Rollenberechtigung via Kapitel 2 geprüft).
- `approve(approval_id, source)`:
  - Source TRUSTED, Status `pending`, nicht abgelaufen (`expires_at`), sonst `ApprovalError`.
  - Bindungen: approval.task_id == task.id, approval.action == action, approval.scope == scope.
    Jede Abweichung → fail-closed blockieren (Approval wird NICHT verbraucht).
  - Erfolg → Status `approved`, Event `gate.owner_approved`.
- Ausführung einer genehmigten Aktion (`execute_approved`): atomar Status `pending|approved → consumed`
  UND Aktion ausführen in EINER Transaktion. Zweiter Konsum → Fehler (rowcount=0), keine Doppelausführung.
- `reject(approval_id, source)`: TRUSTED + `pending` → `rejected`, Task → `BLOCKED`, Event `gate.owner_rejected`.
  Rejected/consumed/expired Approvals sind nie wiederverwendbar.
- Einmalige Freigabe + nur für genau diesen Task: per Konstruktion (single-use, Bindung task+action+scope).

## 4. Trust Boundary

- Source-Klassen: `TRUSTED` (nur explizit authentifizierter Owner, z. B. `owner:authenticated`,
  `role:<role>` als deterministische Systemkomponente eines Owner-gestarteten Tasks) vs.
  `UNTRUSTED` (`email`, `website`, `download`, `document`, `repo_content`, `tool_output`, `network`).
- UNTRUSTED Input kann NIE: einen Dev-Task starten, ein Approval erzeugen/genehmigen/ablehnen,
  Shell-/Agent-/Codex-/Sol-/Pro-Aktionen autorisieren, Policy ändern.
- Alle Public-API-Einstiegspunkte (create_task, approve, reject, request_action, transition, …)
  prüfen die Source-Klasse; UNTRUSTED → `UntrustedSource`, kein Seiteneffekt.

## 5. Persistenz (SQLite)

Tabellen: `projects, tasks, task_runs, role_runs, handoffs, findings, test_runs, reviews,
decisions, owner_approvals, events` (+ Schema-Versionstabelle `schema_meta`).

Pflichtfelder (Kern):
- tasks: id, project_id, title, state, resume_state, source, source_class, created_at, updated_at, idempotency_key (UNIQUE)
- role_runs: id, task_id, role, status(started/completed/failed), started_at, finished_at
- owner_approvals: id, task_id, action, scope, status(pending/approved/rejected/consumed/expired),
  requested_by, source_class, created_at, decided_at, consumed_at, expires_at
  + partieller UNIQUE-Index (task_id, action, scope) WHERE status IN ('pending','approved') → keine Duplikat-Requests
- events: id (UNIQUE), type, task_id (NULL), role (NULL), state (NULL), payload_json, created_at
- events.id UNIQUE → INSERT OR IGNORE für Idempotenz.

Transaktionen: jeder Command-Handler = eine Transaktion (BEGIN IMMEDIATE … COMMIT/ROLLBACK).
Foreign Keys ON. Nach Crash ist der Workflow aus dem persistierten Zustand rekonstruierbar.

Idempotenz: Commands akzeptieren optionale `idempotency_key`; Wiederholung mit gleichem Key
→ kein zweiter State-Change, kein Doppel-Event (Unique-Constraints + INSERT OR IGNORE bzw. UPDATE-…-WHERE-Checks).

## 6. Event-System (privacy-safe, lokal)

Pflicht-Eventtypen: task.created, task.state_changed, role.started, role.completed, role.failed,
handoff.created, finding.created, finding.resolved, test.started, test.completed, review.started,
review.completed, lead.decision, gate.owner_required, gate.owner_approved, gate.owner_rejected,
system.recovery_started, system.recovery_completed, task.completed.

Event-Format: {id, type, task_id?, role?, state?, payload, created_at}

Privacy-Filter (fail-closed): Payload darf KEINE Schlüssel/Werte enthalten aus
PRIVACY_DENYLIST: prompt, chain_of_thought, cot, reasoning, secret, password, api_key, token,
credential, mail_content, mail_address, email_address, source_code, code, diff, body, subject,
content, recipient. Versuch → `PrivacyViolation` (Event wird nicht geschrieben).

Verboten in Events: Prompts, Chain-of-Thought, Secrets, Credentials, Mailinhalte, Mailadressen,
vollständiger Sourcecode. Kein Senden an den Visualizer — nur lokaler Event-Contract + Tests.

## 7. Idempotenz & Recovery

- Wiederholte Commands/Events: kein doppelter Zustandswechsel, keine doppelte Approval-Ausführung.
- Crash-Simulation: (a) Transaktion begonnen, nicht committet, Connection zerstört; (b) Subprozess,
  der mitten in einer Transaktion per `os._exit(1)` stirbt.
- `recover()` nach Restart (eine Transaktion):
  1. In-flight role_runs (status=started) → `failed` (interrupted).
  2. In-flight task_runs (status=started) → `failed` (interrupted).
  3. Pro Task: letzten sicher abgeschlossenen Phasen-Meilenstein aus role_runs/events ermitteln;
     konservativ auf diesen Zustand zurückrollen (nie vorwärts springen).
     Zustand RECOVERING → resume_state; kein safe-state gefunden → BLOCKED.
  4. Events `system.recovery_started` / `system.recovery_completed`.

## 8. Tests (pytest, deterministisch, vollständig)

Mindestens abdecken:
1. alle gültigen State-Transitions (parametrisiert)
2. ungültige Transition wird blockiert (fail-closed)
3. maximal eine aktive Rolle
4. Rollenrechte (Tabelle aus Kapitel 2, inkl. qa/Produktcode-Verbot, lead/analyst/reviewer read-only Produktcode)
5. Owner-Gate stoppt Aktion (Approval-Request + Zustand OWNER_APPROVAL_REQUIRED)
6. Approval setzt Workflow fort (resume_state, Aktion ausgeführt, consumed)
7. Approval nur für richtigen Task + richtige Aktion + richtigen Scope
8. Approval nicht wiederverwendbar (Doppel-Konsum, Doppel-Execute)
9. Reject bleibt blockiert (kein Resume, kein Execute)
10. abgelaufenes Approval nicht nutzbar
11. FORBIDDEN nicht durch Approval aushebelbar (kein Approval erzeugt, Execute schlägt fehl)
12. UNTRUSTED Input kann keinen Dev-Task starten (alle UNTRUSTED-Quellklassen)
13. UNTRUSTED Input kann kein Approval erzeugen/genehmigen/ablehnen
14. Crash/Restart-Recovery (inkl. Subprozess-Crash)
15. SQLite-Persistenz (Schema, Reopen, Transaktions-Rollback, Foreign Keys)
16. Event-Idempotenz (doppeltes Insert = 1 Zeile)
17. Event-Privacy (Denylist fail-closed; Scan aller in der Testsuite erzeugten Events auf
    verbotene Inhalte: keine Secrets/Prompts/Cot/Mail/Code)
18. alle Pflicht-Eventtypen werden an den richtigen Stellen emittiert
19. Idempotenz der Commands (idempotency_key)

## 9. Abnahmekriterien

- Alle Tests grün (`python3 -m pytest tests/ -q`).
- Keine offenen HIGH/CRITICAL Findings.
- State Machine, Owner Gates, Trust Boundary, Recovery, Privacy Events verifiziert.
- Mail-Agent und System-Visualizer unverändert.
- Lokaler Git-Commit (kein Push) nach grünen Tests und geschlossenem Review erlaubt.

## 10. Nicht-Ziele (Phase 1)

Keine echten Agenten/Subprozesse, kein Docker/Sandbox, keine Netzwerk-Exfiltration,
keine Visualizer-Anbindung, kein Control-Pfad, keine Policy-Persistenz außerhalb des Projekts.

---

## 11. SPEC V1.1 — Sicherheits-Härtung (nach unabhängigem Architektur-/Security-Review)

V1.1 überschreibt V1 bei Widersprüchen. Jede Änderung schließt ein verifiziertes Review-Finding (R1–R15).

### 11.1 Zustandsmaschine (R1, R4, R6)

- Eintritt in `OWNER_APPROVAL_REQUIRED` ist ein gültiger Übergang von jedem Nicht-End-, Nicht-Pause-Zustand (Gate-Eintritt), NICHT von `DONE`/`CANCELLED` und NICHT aus `PAUSE_STATES` heraus (kein Selbstloop, kein Doppel-Gate).
- Verlassen von `OWNER_APPROVAL_REQUIRED` NUR über: `execute_approved` (→ resume_state), `reject` (→ BLOCKED), `CANCELLED`. **Nicht** über die öffentliche `transition()`.
- `transition()` (öffentlich) lehnt ab, wenn Von- oder Zu-Zustand in `PAUSE_STATES` liegt (reserviert für dedizierte Befehle: Gate-Pfad, `resume`, `recover`).
- Neuer dedizierter Befehl `resume(task_id, source)` (lead-only): `PAUSED → resume_state`.
- `RECOVERING` wird ausschließlich innerhalb von `recover()` verlassen.
- `start_role` ist in Terminal- und Pause-Zuständen verboten (kein Role-Run auf `DONE`/`CANCELLED`/`OWNER_APPROVAL_REQUIRED`/`PAUSED`/`RECOVERING`/`BLOCKED`).
- Unbekannter Zielzustand in `transition()` → `InvalidTransition` (nicht `ValueError`; R15).

### 11.2 Rollen-Autorität (R5, R12)

- Jede rollengebundene Operation erfordert: aktiven Role-Run der Rolle R für den Task UND `source == "role:<R>"`. Der Akteur wird ausschließlich aus dem aktiven Role-Run abgeleitet; freie `actor_role`-Parameter entfallen bzw. werden strikt gegen den aktiven Run geprüft.
- `start_role`: lead-only (aktiver Lead-Run + `source=role:lead`); erster Rollenstart eines Tasks = `lead`; jeder weitere Start muss dem `to_role` des jüngsten offenen Handoffs entsprechen (Handoff wird erzwungen, R12).
- `complete_role`/`fail_role`: `source` muss `role:<run.role>` sein; Parameter `next_role` entfällt; Handoff-Ziel = deterministisches `DEFAULT_NEXT_ROLE`.
- `transition`, `record_decision`: lead-only.
- `record_test_run`: qa oder implementer (aktiver Run + passende Source). `record_review`: reviewer. `add_finding`/`resolve_finding`: reviewer oder qa.
- `request_action`: `actor_role` == aktive Rolle des Tasks und `source == "role:<actor_role>"` (R5).

### 11.3 Owner-Gates (R2, R3, R7, R10, R13)

- `approve`, `reject`, `execute_approved`, `create_project`, `create_task`, `recover`: Source MUSS `owner:authenticated` sein (Owner-Authority; Rollenquellen sind TRUSTED, aber NIE Owner; R3).
- `task_id`, `action`, `scope` sind für `approve`/`reject`/`execute_approved` verpflichtend; vollständiger Vergleich gegen das Approval (R7).
- `execute_approved` konsumiert NUR `status='approved' AND expires_at > now` — niemals `pending` (R2), niemals abgelaufen (R10). Vor dem atomaren Update: Aktionsklasse erneut prüfen (`FORBIDDEN` → blockieren), `source_class` prüfen (R7).
- Ausführung wird persistiert: neue Tabelle `action_executions(id, task_id, approval_id NULL, action, scope, actor_role, status(executed|blocked), created_at)`. AUTONOMOUS: Ausführungszeile + Ergebnis. Genehmigt: `INSERT action_executions` + `consume` + Resume in EINER Transaktion (R13). FORBIDDEN: `blocked`-Zeile, kein Approval, `lead.decision`-Event.

### 11.4 Trust Boundary (R3)

- `trust.require_owner(source)` für alle Owner-Authority-Operationen (siehe 11.3). `role:<R>` bleibt TRUSTED für Rollenoperationen, erhält aber nie Owner-Befugnisse.

### 11.5 Persistenz & Kapselung (R8, R14)

- Schema V2: `CHECK`-Constraints für `tasks.state`, `role_runs.role/status`, `owner_approvals.status`, `source_class`-Spalten, `action_executions.status`; partieller Unique-Index `role_runs(task_id) WHERE status='started'` (genau ein aktiver Role-Run); Tabelle `action_executions`; `command_idempotency` erhält Spalte `args_hash`.
- Kapselung: `Store.conn` privat; alle Store-Mutatoren privat (nur `Core` ruft sie); `Core.store` entfällt zugunsten einer read-only Query-Fassade (`Core.queries` mit ausschließlich `get_*`/`list_*`). Kein öffentlicher Schreibpfad an der API vorbei.

### 11.6 Idempotenz & Recovery (R6, R9, R11)

- ALLE Commands (inkl. `request_action` AUTONOMOUS/FORBIDDEN und `recover`) laufen durch den idempotenten Command-Wrapper; `args_hash` (kanonischer Hash der Argumente) wird gespeichert; Replay mit abweichenden Argumenten → `IdempotencyError` (R9).
- `recover()`: Idempotenz-Key innerhalb derselben `BEGIN IMMEDIATE`-Transaktion prüfen (R9).
- Recovery konservativ V2 (R6, R11): In-flight `role_runs`/`task_runs` → `failed` (interrupted). Task-Zustand: NUR Tasks in `RECOVERING` werden auf `resume_state` gesetzt (validiert, nicht-terminal), sonst `BLOCKED`. Alle anderen Zustände bleiben unverändert (insb. `OWNER_APPROVAL_REQUIRED`, `PAUSED`, `BLOCKED`, `DONE`, `CANCELLED` — kein Gate-Bypass, kein Verlassen von Terminalzuständen). Keine Milestone-Berechnung aus Role-Runs/Events; der zuletzt committete Zustand ist der letzte sichere Meilenstein.

### 11.7 Regressionstests (alle Findings)

Neue/angepasste Tests, die jedes Finding R1–R15 reproduzieren und den Fix belegen (siehe Kap. 8).

---

## 12. SPEC V1.2 — Nachkonsolidierung (nach Sol-Recheck)

V1.2 überschreibt V1/V1.1 bei Widersprüchen. Schließt die verifizierten Restbefunde.

### 12.1 Zustandsmaschine — Gate-Eintritt strikt (R4-Rest, HIGH)

- Resume-Ziele der dynamischen Regel (`PAUSED`/`RECOVERING`/`OWNER_APPROVAL_REQUIRED` → `resume_state`) müssen Nicht-End- UND Nicht-Pause-Zustände sein. `resume_state == OWNER_APPROVAL_REQUIRED` (oder anderer Pause-Zustand) ist als Ziel ungültig.
- Gate-Eintritt (`X → OWNER_APPROVAL_REQUIRED`) nur von Nicht-End-, Nicht-Pause-Zuständen. Zusätzliche Absicherung im Gate-Pfad: `request_action` lehnt ab, wenn `task.state` in `PAUSE_STATES` oder terminal ist (auch für OWNER_APPROVAL_REQUIRED-Requests).

### 12.2 AUTONOMOUS nur in arbeitsfähigen Task-Zuständen (MEDIUM)

- `request_action` (AUTONOMOUS **und** FORBIDDEN) validiert den Task-Zustand: nur Nicht-End-, Nicht-Pause-Zustände (Hauptpfad + REWORK). Aktionen auf `DONE`/`CANCELLED`/Pause-Zuständen → `InvalidTransition`/blockiert, keine `action_executions`-Zeile.

### 12.3 Approval-Expiry ohne Deadlock (MEDIUM)

- `execute_approved` auf einem abgelaufenen `approved`-Approval: atomar in `expired` überführen (neue/erweiterte Store-Methode `_mark_expired` deckt `status IN ('pending','approved') AND expires_at <= now` ab), dann `ApprovalError` — kein Konsum, kein Resume, kein Deadlock: Danach kann der Owner einen frischen Approval-Request stellen (der Unique-Index blockt nur `pending`/`approved`).
- `reject` bleibt auf `pending` beschränkt; der Weg aus dem Deadlock ist der Execute-Pfad (→ `expired` → neuer Request).

### 12.4 Robustes Recovery bei defektem `resume_state` (LOW)

- Schema: `tasks.resume_state` erhält `CHECK (resume_state IS NULL OR resume_state IN (<alle gültigen TaskStates>))`.
- `recover()`: defekter/unbekannter `resume_state` beim Laden darf nicht crashen → Task wird defensiv nach `BLOCKED` gesetzt (Try/Except um die State-Konvertierung im Recovery-Pfad), Rest des Recovery läuft weiter.

### 12.5 Event-Privacy über alle Felder (R16, LOW)

- `check_privacy` scannt ALLE String-Felder eines Events: `type`, `task_id`, `role`, `state` UND `payload` (Schlüssel + Werte, rekursiv) — fail-closed.
- Substring-Matching bleibt bewusst (false-positive-sicher = fail-closed); als bekannte Einschränkung im README dokumentieren.
- Python-Private (`core._store`, `core.queries._store`) bleiben erreichbar: akzeptiertes LOW-Risiko, dokumentieren; reale Verteidigung = keine öffentliche Schreib-API + DB-Constraints (R8/R14).

### 12.6 Regressionstests (Restbefunde)

Tests für: R4-Restbypass (Pause→Gate via `resume_state` blockiert), AUTONOMOUS/FORBIDDEN auf `DONE`/Pause blockiert, Expiry-Deadlock (approved+abgelaufen → `expired` + neuer Request möglich), defekter `resume_state` → Recovery `BLOCKED` ohne Crash, Event-Envelope-Privacy (Denylist-Wort in `type`/`task_id`/`role`/`state` → `PrivacyViolation`).

---

## 13. SPEC V1.3 — Abschlusskonsolidierung (nach Sol-Closing-Recheck)

V1.3 überschreibt V1–V1.2 bei Widersprüchen. Schließt die verifizierten Closing-Befunde.

### 13.1 Recovery nutzt dieselbe Resume-Zielvalidierung wie die State Machine (HIGH)

- `recovery_target` darf als Resume-Ziel NUR Nicht-End- UND Nicht-Pause-Zustände akzeptieren (`PAUSE_STATES` vollständig ausgeschlossen, inkl. `OWNER_APPROVAL_REQUIRED` und `PAUSED`), sonst `BLOCKED`.
- Die Validierung wird aus der State Machine wiederverwendet (eine Quelle der Wahrheit); `recover()` ruft zusätzlich `validate_transition(RECOVERING, target, resume_state)` vor der Anwendung auf (Defense in Depth).

### 13.2 Approval-Expiry-Lapse ohne Deadlock (MEDIUM)

- `execute_approved` auf einem abgelaufenen `approved`-Approval: in EINER Transaktion (a) Approval → `expired`, (b) Task → `resume_state` (validiert: Nicht-End-, Nicht-Pause-Zustand; sonst `BLOCKED`), dann `ApprovalError`. Kein Konsum, keine Execution.
- Symmetrisch (Supervisor-Entscheidung): `approve` auf einem abgelaufenen `pending`-Approval verwendet denselben `_expire_and_release`-Mechanismus — Approval → `expired`, Task → `resume_state` (sonst `BLOCKED`), dann `ApprovalError`. Auch hier bleibt der Workflow über die öffentliche API lebendig.
- Damit ist der Workflow über die öffentliche API lebendig: Danach kann der aktive Role-Run denselben Gated-Action-Request erneut stellen, der Owner approven, `execute_approved` konsumiert und die Aktion ausführt.
- Regressionstests als echte End-to-End-Flows OHNE SQL-State-Reset (kein `_set_state`-Helfer).

### 13.3 `fail_role` erzeugt deterministischen Handoff (MEDIUM)

- `fail_role` legt wie `complete_role` einen Handoff an: `from_role = run.role`, `to_role = DEFAULT_NEXT_ROLE[run.role]`; zusätzlich Event `handoff.created`. Der nächste `start_role` muss dem offenen Handoff entsprechen (deterministische Fortsetzung nach Fehlschlag, SPEC 11.2).

### 13.4 Regressionstests

- Recovery: Task mit `state=RECOVERING, resume_state=OWNER_APPROVAL_REQUIRED` und mit `resume_state=PAUSED` → `BLOCKED` (kein Pause-Eintritt), gesunder Resume-Ziel-Task → `resume_state`, Rest läuft weiter.
- Expiry-Lapse: E2E — Request → approve → Uhr +2h → execute_approved → `ApprovalError`, Approval `expired`, Task zurück im Resume-Zustand → erneuter Request → approve → execute → `consumed` + Execution-Zeile. Kein SQL-Reset.
- `fail_role`: Handoff `lead→analyst` erzeugt; `start_role(analyst)` danach gültig, `start_role(lead)` blockiert.
