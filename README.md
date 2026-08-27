# Argent Development Team — Phase 1: Deterministic Team-Control Core

Deterministischer, fail-closed Team-Control-Core (SPEC V1). Keine echten
Agenten, kein Docker, keine Netzwerk-Exfiltration — nur der lokale
Architektur-Vertrag aus [`docs/SPEC.md`](docs/SPEC.md).

## Zweck

Der Core kontrolliert den Lebenszyklus von Entwicklungs-Tasks deterministisch:

- **State Machine** — 16 Task-Zustände mit exakter Übergangstabelle
  (fail-closed: ungültige Übergänge werfen `InvalidTransition`).
- **Rollen** — fünf feste Rollen mit Berechtigungsmatrix, genau eine aktive
  Rolle gleichzeitig (`RoleConflict`).
- **Gated Autonomy** — `classify_action` → `AUTONOMOUS` /
  `OWNER_APPROVAL_REQUIRED` / `FORBIDDEN`; single-use Approvals, streng an
  `task_id + action + scope` gebunden.
- **Trust Boundary** — jede Public API prüft die Source-Klasse; UNTRUSTED
  Input kann nie einen Task starten oder ein Approval erzeugen/genehmigen.
- **Persistenz** — SQLite (stdlib), Foreign Keys ON, `BEGIN IMMEDIATE`
  Transaktionen, idempotente Events (`INSERT OR IGNORE`).
- **Privacy-safe Events** — 19 Pflicht-Eventtypen mit fail-closed
  Deny-List-Filter (`PrivacyViolation`).
- **Idempotenz & Recovery** — Commands mit `idempotency_key` idempotent;
  `recover()` rollt in-flight Runs auf `failed` und Tasks konservativ zurück.

## Architektur

```
argent_core/
  models.py         Enums, Dataclasses, typisierte Exceptions
  state_machine.py  Übergangstabelle + validate_transition
  roles.py          Berechtigungsmatrix + check_permission
  gates.py          Aktionsklassifikation (Gated Autonomy)
  trust.py          Source-Klassifikation (Trust Boundary)
  events.py         19 Eventtypen + Privacy-Denylist
  store.py          SQLite-Persistenz (11 Tabellen + schema_meta)
  recovery.py       Safe-State-Milestone-Logik
  core.py           Public API (Command-Handler)
tests/              pytest-Suite (deterministisch)
```

Die Public API in `core.Core`: `create_project`, `create_task`, `transition`,
`start_role`, `complete_role`, `fail_role`, `request_action`, `approve`,
`reject`, `execute_approved`, `add_finding`, `resolve_finding`,
`record_test_run`, `record_review`, `record_decision`, `recover`,
`list_events`.

## Testausführung

```bash
cd /home/pc/projects/argent-development-team
python3 -m pytest tests/ -q
```

Voraussetzungen: Python 3.14, nur Standardbibliothek + `pytest`
(nutzerlokal installiert).

## Sicherheits-Härtung V1.1

SPEC V1.1 (siehe `docs/SPEC.md`, Kapitel 11) schließt 15 verifizierte
Security-Findings (R1–R15):

- **State Machine** — Gate-Eintritt in die Transitionstabelle; `transition()`
  lehnt Pause-Zustände ab; neuer `resume()`-Befehl; unbekannter Zielzustand
  wirft `InvalidTransition`.
- **Rollen-Autorität** — jede rollengebundene Operation ist an den aktiven
  Role-Run + `role:<R>`-Source gebunden; lead-only-Operationen; Handoffs werden
  erzwungen (`DEFAULT_NEXT_ROLE`).
- **Owner-Gates** — `approve`/`reject`/`execute_approved`/`create_project`/
  `create_task`/`recover` erfordern `owner:authenticated` (`require_owner`);
  verpflichtende, vollständige Bindungen; `execute_approved` konsumiert nur
  `approved` + unabgelaufen; Ausführungen werden in `action_executions`
  persistiert (atomar: INSERT + consume + resume).
- **Kapselung** — `Store.conn`/Mutatoren privat; `Core.store` entfällt
  zugunsten der read-only Fassade `Core.queries` (`get_*`/`list_*`).
- **Idempotenz** — alle Commands inkl. AUTONOMOUS/FORBIDDEN/`recover` laufen
  durch den Wrapper; `args_hash` wird gespeichert; Replay mit abweichenden
  Argumenten wirft `IdempotencyError`.
- **Recovery V2** — nur `RECOVERING` → `resume_state`/`BLOCKED`; alle anderen
  Zustände bleiben unverändert (kein Milestone-Rollback, kein Gate-Bypass).
- **Schema V2** — CHECK-Constraints, partieller Unique-Index auf aktive
  Role-Runs, `action_executions`-Tabelle, `args_hash`-Spalte.

## Nachkonsolidierung V1.2

SPEC V1.2 (siehe `docs/SPEC.md`, Kapitel 12) schließt die verifizierten
Restbefunde aus dem Sol-Recheck:

- **Gate-Eintritt strikt** — Resume-Ziele der dynamischen Regel müssen
  Nicht-End- **und** Nicht-Pause-Zustände sein; `request_action` lehnt ab, wenn
  der Task in einem Terminal- oder Pause-Zustand ist (auch für
  OWNER-APPROVAL-Requests und AUTONOMOUS/FORBIDDEN).
- **Aktionsfähige Zustände** — `request_action` ist nur aus den
  Hauptpfad-Zuständen (`NEW`..`FINAL_DECISION`) und `REWORK` erlaubt;
  `BLOCKED`/`FAILED`, Terminal- und Pause-Zustände sind gesperrt
  (fail-closed, Supervisor-Entscheidung zu SPEC V1.2 §12.2).
- **Approval-Expiry ohne Deadlock** — `execute_approved` auf einem abgelaufenen
  `approved`-Approval überführt ihn atomar in `expired` (der partielle
  Unique-Index blockt nur `pending`/`approved`) und wirft `ApprovalError`;
  danach ist ein frischer Request möglich.
- **Robustes Recovery** — `tasks.resume_state` erhält einen CHECK-Constraint;
  ein defekter/unbekannter `resume_state` crasht `recover()` nicht mehr, sondern
  setzt den Task defensiv auf `BLOCKED` (Rest des Recovery läuft weiter).
- **Event-Privacy über alle Felder** — der Deny-List-Filter scannt neben dem
  Payload auch die Envelope-Felder `type`, `task_id`, `role` und `state`
  (fail-closed).

## Bekannte Einschränkungen

- **Substring-Matching (Privacy-Filter)** — der Deny-List-Filter arbeitet
  bewusst mit case-insensitive Substring-Matching. Das ist
  false-positive-sicher (fail-closed): legitime Werte, die ein
  Deny-List-Wort als Teilstring enthalten (z. B. „body“ in einem
  zusammengesetzten Begriff), werden abgelehnt, bevor sensible Inhalte
  durchrutschen. Dies ist eine akzeptierte, dokumentierte Einschränkung.
- **Erreichbare Python-Private** — `core._store` und `core.queries._store`
  sind technisch über den Namen erreichbar (Python kennt kein hartes
  Sichtbarkeits-Enforcement). Dies ist ein akzeptiertes LOW-Risiko: die
  reale Verteidigung ist, dass es **keine öffentliche Schreib-API** an der
  `Core`-Fassade vorbei gibt und die DB-Constraints (CHECKs, partielle
  Unique-Indizes, Foreign Keys) als zweite Verteidigungslinie greifen
  (R8/R14).

## Phase 2A — Orchestrierung & Provenance (SPEC V2/V2.1)

Phase 2A verbindet den deterministischen Core mit echten, isolierten
OpenClaw-Agent-Runs und erzwingt harte Run-/Session-/Handoff-Provenance.
**Der Core bleibt die einzige Autoritätsinstanz**; Agenten liefern Empfehlungen
(DATA), der Provenance-Layer validiert und konsumiert atomar. Der **Controller
(Lead) ist die einzige Core-Schnittstelle**.

### Architektur (neu)

```
argent_core/
  routing.py   Kanonisches Modellrouting (resolve_model / validate_model_choice)
  workflow.py  STANDARD-/REWORK-Sequenzen + expected_next_role-Replay
  outputs.py   Fail-closed Validierung strukturierter Rollen-Outputs
  context.py   Rollen-spezifische Kontext-Isolation + Snapshots
  core.py      create_dispatch / bind_spawn_result / receive_agent_result /
               mark_agent_failed / build_agent_context / expected_next_role /
               list_dispatches / quarantine_log; recover()-Erweiterung
  store.py     Schema V3: agent_dispatches, agent_result_quarantine,
               agent_context_snapshots + tasks.description/risk_class/
               external_actions_policy + Migration
  gates.py     EXTERNAL_ACTIONS (geschlossene Menge) + policy-aware classify
  events.py    +12 Eventtypen (agent.*, handoff.expected/accepted,
               policy.role_violation)
tests/
  mock_runtime.py          Offline Mock-Runtime (Spawn + Completion-Events)
  test_phase2a_*.py        8 Testdateien (Workflow, Provenance, Routing,
                           Outputs, Context, Gates, Recovery, Events)
```

### Workflow-Sequenzen

- **Standard**: `lead → analyst → lead → implementer → qa → reviewer → lead → DONE`
- **Rework**:  `lead → implementer → qa → [reviewer] → lead → DONE`
  (Reviewer konditional via `lead_decision.rework_include_reviewer`, Default
  `true` wenn Findings offen). Rework setzt `cycle_no+1` + `sequence_kind=REWORK`.
- `expected_next_role(task)` wird deterministisch aus `(cycle_no, position,
  sequence_kind)` + offenen Findings + letzter Lead-Decision replayt.

### Modellrouting (kanonisch)

| Rolle       | Provider | Modell             | Thinking |
|-------------|----------|--------------------|----------|
| lead        | openai   | gpt-5.6-sol        | high     |
| analyst     | deepseek | deepseek-v4-pro    | medium   |
| implementer | deepseek | deepseek-v4-pro    | medium   |
| qa          | deepseek | deepseek-v4-pro    | medium   |
| reviewer    | openai   | gpt-5.6-sol        | high     |

- `deepseek-v4-flash` **nur** für implementer/qa mit `risk_class='LOW'`.
- **Sol Max existiert nicht** in Doku/Konfiguration (`openclaw models list`,
  read-only geprüft); höchste Stufe ist `openai/gpt-5.6-sol` = **„Sol High“**.
  Lead und Reviewer verwenden deshalb Sol High (Owner-Vorgabe, dokumentiert).
- Lead/Reviewer müssen unterschiedliche `child_session_id`s haben (partielle
  Unique-Indizes erzwingen dies fail-closed).

### Provenance-Mechanismus

- `create_dispatch` ist der persistente Spawn-Intent (vor `sessions_spawn`);
  Korrelationslabel `argent-dispatch-<dispatch_id>` (Controller-Verfahren).
- `bind_spawn_result` = EINE atomare `PENDING→RUNNING`-Operation (bindet alle
  Spawn-Rückgaben); IDs werden NIE geraten.
- `receive_agent_result` validiert alle IDs verpflichtend (task_id/session/
  run_id/parent/envelope/provider/model), Handoff, Task-Zustand und den
  strukturierten Output fail-closed; nur vollständig gebundene
  `RUNNING|RECOVERY_PENDING`-Dispatches sind konsumierbar (CAS, rowcount==1).
- Ghost-Writer-Ausschluss: Schreibrollen-`PENDING|RUNNING` werden bei Recovery
  NIE auto-failed → `RECOVERY_PENDING` + Task konservativ `RECOVERING`.

### Rollenrechte & Context-Isolation

- Lead/Analyst/Reviewer read-only Produktcode; Implementer einziger Writer;
  QA nur Test-Scope. Verstöße → `policy.role_violation`.
- Reviewer-Kontext ohne Implementer-`own_assessment`/`proposal`; Analyst-Kontext
  ohne Implementer-Lösung. Snapshots persistiert in `agent_context_snapshots`
  (`context_hash` = SHA-256 der strukturierten Sektionen).

### Smoke-Run-Verfahren (Controller, separat von der Testsuite)

Ein echter Smoke-Run wird außerhalb von pytest ausgeführt: Controller startet
`openclaw sessions_spawn` mit Modell aus `routing.resolve_model` und stabilem
Label `argent-dispatch-<dispatch_id>`, bindet die Rückgaben mit
`bind_spawn_result`, liefert den Completion-Event mit `receive_agent_result`
und dokumentiert `openclaw tasks show`/`openclaw models list` (read-only) im
Abschlussbericht. Kein echter Spawn in den Tests (offline-deterministisch).

### V2.2-Härtung (F1–F8)

Nach dem unabhängigen Sol-Implementierungs-Review (SPEC V2.2 §16):

- **F1** — Orchestrierung treibt die autoritative State Machine: der Konsum
  synchronisiert den Task-Zustand in derselben Transaktion über eine
  deterministische Mapping-Tabelle (`sequence_kind, position, decision`); die
  State Machine erhielt genau zwei Übergänge (`LEAD_DECISION→REWORK`,
  `REWORK→IMPLEMENTING`).
- **F2** — vorhandene UND neue `RECOVERY_PENDING`-Dispatches bleiben bei
  `recover()` ungelöst (Role-/Task-Run bleiben STARTED).
- **F3** — `bind_spawn_result` akzeptiert `PENDING|RECOVERY_PENDING→RUNNING`
  (Spawn-vor-Bind-Crash rekonzilierbar).
- **F4** — `expected_thinking_tier` wird persistiert; Bindung erzwingt exakte
  Gleichheit (provider/model/thinking) zusätzlich zur Rollen-Policy
  (Mismatch → atomar `REJECTED`); `parent_dispatch_id` verpflichtend;
  `event_meta` muss `task_id/child_session_id/run_id/parent_dispatch_id/
  event_type/status` enthalten, `status ∈ {completed, succeeded}`.
- **F5** — Context-Snapshots unveränderlich (Plain INSERT), dispatchgebunden
  (Rolle/Position müssen passen), `repo_summary` allow-list-/limit-/deny-list-
  gefiltert.
- **F6** — verschachtelte Rollenoutput-Validierung VOR dem CAS (Element-Schemas/
  Enums je Feld → `REJECTED(malformed_output)`).
- **F7** — Quarantäne-Metadaten: Werte erzwungen `str`, ≤512 Zeichen,
  Deny-List-Scan → `<redacted:<sha256-prefix>>` (immer JSON-serialisierbar).
- **F8** — Migration in EINEM `BEGIN IMMEDIATE`-Block + UPSERT der
  `schema_version` auf 3.

### V2.3-Härtung (G1–G4)

Nach dem Sol-Recheck (SPEC V2.3 §17):

- **G1** — `bind_spawn_result` liest Status, prüft Exact-Equality/Policy UND
  ändert den Status in EINEM `BEGIN IMMEDIATE`-Block; der Mismatch-Pfad setzt
  `REJECTED` per CAS (`WHERE status IN ('PENDING','RECOVERY_PENDING')`,
  `rowcount==1`), überschreibt also nie einen parallel gültig gebundenen
  `RUNNING`-Dispatch (kein Ghost-Writer-Retry).
- **G2** — verschachtelte Element-Schemas vollständig erzwungen: `findings[]`
  braucht `severity` (Enum) + `description`/`title`; Sec/Arch-Dicts brauchen
  `severity` + `description` (leere Dicts abgelehnt); `_apply_role_effects`
  nutzt `title` als `description`-Fallback.
- **G3** — Schema-Erstellung (`_SCHEMA`) und Migration laufen in EINEM
  gemeinsamen `BEGIN IMMEDIATE`-Block; ein Migrationsfehler rollt alles zurück
  (keine persistierte Teilmigration).
- **G4** — `_sanitize_event_meta`: `None` → `<none>`, fehlschlagendes `__str__`
  → `<unprintable>`; immer String, ≤512 Zeichen, Deny-List-Scan.

### Testausführung

```bash
cd /home/pc/projects/argent-development-team
python3 -m pytest tests/ -q
```

Voraussetzungen unverändert: Python 3.14, nur Standardbibliothek + pytest.
