# ARGENT DEVELOPMENT TEAM — Phase 2C: Persistent Supervisor Loop & Reconciliation (SPEC V2C)

Basis: Phase 2B, Commit `e66fc44`, Branch `phase-2c-persistent-supervisor`, 815
Bestandstests. Phase 2C ergänzt die bestehende deterministische Core-/SQLite-Ledger um
einen lokal startbaren, restart-festen Supervisor. SPEC V2C überschreibt SPEC V2–V2B nur
für Supervisor-Lifecycle und Reconciliation; State Machine, Rollenfolge, Provenance,
Write-Broker, bwrap-Sandbox, Rollenrechte und Trust Boundary bleiben verbindlich.

Zielmarker nach vollständiger Implementierung und Verifikation:

`ARGENT DEVELOPMENT TEAM PHASE 2C — SUPERVISOR_PERSISTENCE_GREEN`

---

## 1. Ziel, Scope und Nicht-Ziele

### 1.1 Verbindliches Ziel

Ein Supervisor-Job überlebt Prozess-, TUI- und Controller-Neustarts und kann aus
persistierten technischen Fakten exakt fortgesetzt werden. Der zentrale
`reconcile()`-Pfad entscheidet für jeden erwarteten Workflow-Schritt deterministisch:

- vorhandener Run läuft → warten, niemals neu starten;
- vorhandener Run ist erfolgreich, Dispatch noch nicht konsumiert → Provenance prüfen und
  über den bestehenden Core-CAS genau einmal konsumieren;
- Dispatch ist bereits `CONSUMED` → Duplikat ignorieren, Workflow-Frontier neu berechnen;
- Event/Run gehört zur falschen Session, run_id, Rolle oder Attempt → ablehnen/quarantänisieren,
  Workflow-Zustand unverändert lassen;
- Run ist technisch fehlgeschlagen → definierte, begrenzte Recovery anwenden;
- Binding/Spawn-Intent existiert, Run ist aber nicht direkt sichtbar → zuerst vorhandene
  Session/Run suchen, niemals blind respawnen;
- Arbeit ist erwartet und es existiert kein aktiver gültiger Dispatch → genau einen Dispatch
  für die aktuelle Frontier erzeugen;
- Task ist `DONE` → keine Rolle, keinen Dispatch, keinen Run und kein Gate mehr erzeugen.

### 1.2 Scope

- neues Modul `argent_core/supervisor.py`;
- Schema V4 in derselben SQLite-Datei wie der Core;
- persistierter Supervisor-Job und crash-sicheres Action-Journal;
- pluggbarer Runtime-Status-Adapter, deterministischer Mock und reale lokale
  OpenClaw-Trajectory-Implementierung;
- lokal startbarer Supervisor-Loop und CLI/Smoke-Driver;
- deterministische Restart-, Duplicate-, Stale-, Gate- und Crash-Tests;
- echter lokaler Recovery-E2E-Smoke ohne Push und ohne OpenClaw-/Systemkonfigurationsänderung.

### 1.3 Nicht-Ziele

- keine neuen Rollen oder Agenten;
- keine parallelen Produktcode-Writer;
- keine Änderung an Mail-Agent, Visualizer, Gateway, Secrets oder Agent-Toolprofilen;
- kein systemd-, cron-, Gateway-Autostart oder anderer Background-Wake in Phase 2C;
- keine Production-Promotion, kein Deployment und kein Push;
- Agent-Prosa, Completion-Pushes und TUI-Text sind niemals Autorität.

Ein späterer Background-Wake darf nur als Interface/Design vorbereitet werden. Seine reale
Installation ist eine neue, exakt gescopte Owner-Aktion und benötigt ein neues Owner Gate.

---

## 2. Autoritäten und Trust Boundary

### 2.1 Rangfolge technischer Fakten

`reconcile()` vertraut ausschließlich, in dieser Reihenfolge:

1. Core-Ledger in SQLite: `tasks`, `task_runs`, `role_runs`, `handoffs`, `findings`,
   `decisions`, `owner_approvals`, `action_executions`, `agent_dispatches`,
   `agent_result_quarantine`, `command_idempotency`;
2. Supervisor-Ledger in derselben SQLite-Datei: `supervisor_jobs`,
   `supervisor_actions`;
3. Runtime-Fakten des injizierten `RunStatusProvider`, gebunden an die persistierten
   Dispatch-IDs, Agent-IDs, Session-IDs und run_ids;
4. über allowlist-basierte lokale Provider erhobener Git-/Workspace-Zustand, nur soweit
   Broker-/Test-Aktionen rekonstruiert werden müssen;
5. persistierte Owner-Gates und deren exakt gebundene Execution-Zeile.

Events sind Hinweise zum Aufwecken, keine Zustandsautorität. Das Wegfallen eines Completion-
Events darf die Fortsetzung nicht verhindern. Agent-Output ist UNTRUSTED DATA und wird erst
durch `Core.receive_agent_result()` validiert. Ein `session.ended(status=success)` beweist nur
den technischen Run-Abschluss, nicht die fachliche Korrektheit des Outputs.

### 2.2 Ledger gewinnt gegen Prosa

Verbindliche Regressionen:

- Main-/Agent-Prosa behauptet „waiting“, Ledger+Runtime zeigen `SUCCEEDED` → Result wird
  validiert/konsumiert;
- Main-Prosa behauptet „session not started by me“, persistierter Spawn-Intent und exakte
  `session.started`-Bindung belegen den Run → Binding gilt;
- Agent-Prosa empfiehlt Gate, Retry, neue Rolle oder DONE → ohne passende technische Core-
  Fakten entsteht keine Folgeaktion;
- ein Completion-Push behauptet Erfolg, Runtime/Ledger zeigen falsche Provenance → Reject/
  Quarantäne, keine Folgeaktion.

### 2.3 Keine zweite State Machine

`tasks.state` plus `Core._workflow_frontier()` bleiben die fachliche Quelle der Wahrheit.
Der Supervisor implementiert keine parallele Rollenfolge und berechnet DONE nicht aus Events.
Die in `supervisor_jobs` gespeicherten Workflow-Felder sind restart-feste Projektionen/Caches;
bei jeder Reconciliation werden sie aus den autoritativen Tabellen neu gebildet und atomar
überschrieben.

---

## 3. Persistentes Supervisor-Modell

### 3.1 Rekonstruierbarer Zustand

`SupervisorState` muss nach Schließen und erneutem Öffnen der DB mindestens folgende Sicht
liefern. „Quelle“ bezeichnet die Autorität, nicht nur den Cache:

| Feld | Autoritative Quelle |
|---|---|
| `supervisor_job_id` | `supervisor_jobs.id` |
| `task_id` | `supervisor_jobs.task_id` |
| `workflow_state` | `tasks.state` |
| `expected_role` | `Core._workflow_frontier(task_id).expected_role` |
| `expected_dispatch` | aktiver Dispatch an der Frontier, sonst `NULL` |
| `agent_id` | feste Phase-2B-Role→Agent-ID-Map, im Job gespiegelt |
| `session_id`, `run_id` | `agent_dispatches.child_session_id/openclaw_run_id` |
| `attempt` | `agent_dispatches.attempt_no` |
| `dispatch_status` | `agent_dispatches.status` |
| `result_status` | letzter technischer `RunObservation.status`, im Job gespiegelt |
| `result_consumed` | `dispatch.status == CONSUMED` und `consumed_at IS NOT NULL` |
| `current_handoff` | jüngster `handoffs.id` des Tasks |
| `open_findings` | Anzahl `findings.status == open` |
| `rework_cycle` | maximale `agent_dispatches.cycle_no`, Default 1 |
| `recovery_state` | `supervisor_jobs.recovery_state` |
| `owner_gate_id/status/scope` | aktuelles `owner_approvals`-Binding |
| `gate_closed` | `owner_approvals.closed_at IS NOT NULL` |
| `last_progress` | `supervisor_jobs.last_progress_at` |
| `terminal` | `NULL | DONE | FAILED | BLOCKED` in `supervisor_jobs` |

Ein Cache-Konflikt wird nicht „mehrheitlich“ entschieden: Core-/Runtime-Fakten gewinnen, Cache
wird repariert, Event `supervisor.snapshot_repaired` enthält nur IDs und Feldnamen.

### 3.2 Supervisor-Lifecycle

`SupervisorJobStatus`:

`ACTIVE | WAITING_RUN | WAITING_GATE | BACKOFF | RECOVERING | ERROR | TERMINAL`

`RecoveryState`:

`NONE | DISCOVERING_RUN | RESTORING_BINDING | CONSUMING_RESULT | RETRYING_STEP |
AMBIGUOUS_WRITER | RUNTIME_UNKNOWN | CORE_RECOVERY_REQUIRED | PERSISTENT_ERROR`

Terminal ist separat und irreversibel innerhalb desselben Jobs:

- Task `DONE` → `terminal=DONE`;
- Task `FAILED|CANCELLED` oder ausgeschöpfte sichere Run-Attempts → `terminal=FAILED`;
- Task `BLOCKED`, abgelehntes Gate oder unauflösbare Writer-Ambiguität →
  `terminal=BLOCKED`.

`terminal != NULL` erzwingt `status=TERMINAL` und `next_action=NONE`. Ein neuer Versuch nach
FAILED/BLOCKED benötigt einen expliziten Owner-/Controller-Befehl, der einen neuen Job erzeugt
oder den Task über die bestehende Core-State-Machine zulässig recovered; ein stale Event kann
den alten Job nie reaktivieren.

---

## 4. Schema V4 (verbindlich)

Schema-Erstellung und V3→V4-Migration laufen wie V2.3 in EINEM gemeinsamen
`BEGIN IMMEDIATE`; Fehler rollen DDL, Backfill, Indizes und `schema_version` vollständig zurück.

### 4.1 Tabelle `supervisor_jobs`

```sql
CREATE TABLE IF NOT EXISTS supervisor_jobs (
    id                    TEXT PRIMARY KEY,
    task_id               TEXT NOT NULL
                          REFERENCES tasks(id) ON DELETE CASCADE,
    status                TEXT NOT NULL CHECK (status IN
                          ('ACTIVE','WAITING_RUN','WAITING_GATE','BACKOFF',
                           'RECOVERING','ERROR','TERMINAL')),
    workflow_state        TEXT NOT NULL,
    expected_role         TEXT,
    expected_dispatch_id  TEXT REFERENCES agent_dispatches(id),
    agent_id              TEXT,
    session_id            TEXT,
    run_id                TEXT,
    attempt_no            INTEGER NOT NULL DEFAULT 0 CHECK (attempt_no >= 0),
    dispatch_status       TEXT,
    result_status         TEXT NOT NULL DEFAULT 'NOT_OBSERVED' CHECK (result_status IN
                          ('NOT_OBSERVED','NOT_FOUND','RUNNING','SUCCEEDED',
                           'FAILED','CANCELLED','UNKNOWN','CONFLICT')),
    result_consumed       INTEGER NOT NULL DEFAULT 0 CHECK (result_consumed IN (0,1)),
    current_handoff_id    TEXT,
    open_findings_count   INTEGER NOT NULL DEFAULT 0 CHECK (open_findings_count >= 0),
    rework_cycle          INTEGER NOT NULL DEFAULT 1 CHECK (rework_cycle >= 1),
    recovery_state        TEXT NOT NULL DEFAULT 'NONE',
    owner_gate_id         TEXT REFERENCES owner_approvals(id),
    gate_status           TEXT,
    gate_scope            TEXT,
    gate_closed           INTEGER NOT NULL DEFAULT 0 CHECK (gate_closed IN (0,1)),
    owner_prompted_at     TEXT,
    next_action           TEXT NOT NULL DEFAULT 'NONE',
    next_wake_at          TEXT,
    retry_count           INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    missing_confirmations INTEGER NOT NULL DEFAULT 0 CHECK (missing_confirmations >= 0),
    last_error_code       TEXT,
    last_progress_at      TEXT NOT NULL,
    terminal              TEXT CHECK (terminal IS NULL OR terminal IN
                          ('DONE','FAILED','BLOCKED')),
    facts_version         INTEGER NOT NULL DEFAULT 0 CHECK (facts_version >= 0),
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    CHECK ((terminal IS NULL) OR (status = 'TERMINAL' AND next_action = 'NONE'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_supervisor_jobs_active_task
    ON supervisor_jobs(task_id) WHERE terminal IS NULL;
```

Die Spiegelspalten dürfen nie direkt als Autorität für Dispatch-/Gate-Aktionen verwendet werden.
Jede Aktion lädt unmittelbar vorher die referenzierten Core-Zeilen erneut.

### 4.2 Tabelle `supervisor_actions` (crash-sicheres Outbox/Journal)

```sql
CREATE TABLE IF NOT EXISTS supervisor_actions (
    id                 TEXT PRIMARY KEY,
    supervisor_job_id  TEXT NOT NULL REFERENCES supervisor_jobs(id) ON DELETE CASCADE,
    dispatch_id        TEXT REFERENCES agent_dispatches(id),
    action_type        TEXT NOT NULL CHECK (action_type IN
                       ('START_ROLE','CREATE_DISPATCH','SPAWN_RUN','BIND_RUN',
                        'APPLY_PATCH_SET','RUN_SANDBOX_TESTS','RECORD_TEST_RESULT',
                        'CONSUME_RESULT','MARK_RUN_FAILED','CORE_RECOVER',
                        'PRESENT_OWNER_GATE','CLOSE_JOB')),
    action_key         TEXT NOT NULL UNIQUE,
    args_hash          TEXT NOT NULL,
    input_hash         TEXT,
    precondition_hash  TEXT,
    effect_hash        TEXT,
    status             TEXT NOT NULL CHECK (status IN
                       ('PLANNED','RUNNING','SUCCEEDED','FAILED','UNCERTAIN')),
    attempt_count      INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at    TEXT,
    started_at         TEXT,
    finished_at        TEXT,
    last_error_code    TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_supervisor_one_spawn_per_dispatch
    ON supervisor_actions(dispatch_id, action_type)
    WHERE action_type = 'SPAWN_RUN';
```

`args_hash` ist SHA-256 über kanonisches JSON der technischen Argumente. Kein Prompt, Agent-
Output, Sourcecode oder Secret wird im Journal gespeichert. `input_hash` darf nur den Hash eines
untrusted Payloads enthalten.

Für Core-Mutationen ist `action_key` zugleich der bestehende `idempotency_key`; damit werden
`command_idempotency` und die vorhandenen Core-CAS-Mechanismen wiederverwendet. Das Journal
ersetzt diese Mechanismen nicht. `SPAWN_RUN` ist die einzige externe, nicht transaktional mit
SQLite koppelbare Aktion und hat deshalb die Sonderregel aus §8.2.

### 4.3 Owner-Gate-Erweiterung, keine zweite Gate-Tabelle

`owner_approvals` und `action_executions` bleiben autoritativ. Es wird keine Supervisor-
Gate-Schatten-Tabelle angelegt. V4 ergänzt ausschließlich fehlende Closure-/Binding-Felder:

```sql
ALTER TABLE owner_approvals ADD COLUMN binding_hash TEXT;
ALTER TABLE owner_approvals ADD COLUMN approved_at TEXT;
ALTER TABLE owner_approvals ADD COLUMN execution_id TEXT;
ALTER TABLE owner_approvals ADD COLUMN executed_at TEXT;
ALTER TABLE owner_approvals ADD COLUMN closed_at TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_owner_approvals_execution_id
    ON owner_approvals(execution_id) WHERE execution_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_action_execution_one_per_approval
    ON action_executions(approval_id) WHERE approval_id IS NOT NULL;
```

Bei einer frischen V4-Datenbank stehen diese Spalten direkt im `CREATE TABLE`; die `ALTER`-
Anweisungen gelten nur für eine erkannte V3-Tabelle. Nach dem `ALTER` erfolgt im selben
Migrationstransakt ein deterministischer Backfill:

- `binding_hash = sha256(canonical_json(["argent-gate-v1", task_id, action, scope]))`;
- `approved_at = decided_at` nur für `approved|consumed`;
- für bestehende `consumed`-Approvals: die exakt eine `action_executions`-Zeile binden,
  `execution_id=id`, `executed_at=created_at`, `closed_at=consumed_at`;
- `rejected`: `closed_at=decided_at`;
- `expired`: `closed_at=expires_at` als deterministischer Legacy-Backfill;
- mehrere Execution-Zeilen für dasselbe Approval oder ein consumed Approval ohne Execution-
  Zeile → Migration fail-closed, kein erratener Abschluss.

Neue Core-Mutationen setzen die Felder atomar:

- `approve`: `decided_at` und `approved_at`;
- `reject`: `decided_at` und `closed_at`;
- `_expire_and_release`: `closed_at` auf den tatsächlichen Expiry-Zeitpunkt;
- `execute_approved`: Execution-Insert plus `execution_id`, `executed_at`, `consumed_at`,
  `closed_at` in derselben Transaktion.

Nach erfolgreichem Backfill wird `binding_hash` für neue Zeilen verpflichtend. Da SQLite ein
nachträgliches `NOT NULL` nicht zuverlässig per `ALTER` erzwingt, muss die V4-Migration entweder
die Tabelle transaktional neu aufbauen oder INSERT/UPDATE-Trigger mit `RAISE(ABORT, ...)` setzen;
Tests müssen NULL ablehnen. `schema_meta.schema_version` wird erst zuletzt auf `4` gesetzt.

---

## 5. Öffentliche API (`argent_core/supervisor.py`)

### 5.1 Datentypen

```python
class RunStatus(str, Enum):
    NOT_FOUND = "NOT_FOUND"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"
    CONFLICT = "CONFLICT"

@dataclass(frozen=True)
class RunLookup:
    dispatch_id: str
    agent_id: str
    expected_session_label: str       # dispatch-<dispatch_id>
    bound_session_id: str | None
    bound_run_id: str | None

@dataclass(frozen=True)
class RunObservation:
    status: RunStatus
    agent_id: str | None
    session_id: str | None
    run_id: str | None
    provider: str | None
    model: str | None
    thinking_tier: str | None
    started_at: str | None
    finished_at: str | None
    result: dict | None                # UNTRUSTED; nie ohne Core validieren
    result_hash: str | None
    authoritative_not_found: bool
    evidence_id: str                  # privacy-safe Pfad-/Runtime-Fingerprint
    error_code: str | None = None

class ReconcileAction(str, Enum):
    NONE = "NONE"
    WAIT = "WAIT"
    START_ROLE = "START_ROLE"
    CREATE_DISPATCH = "CREATE_DISPATCH"
    SPAWN_RUN = "SPAWN_RUN"
    BIND_RUN = "BIND_RUN"
    APPLY_PATCH_SET = "APPLY_PATCH_SET"
    RUN_SANDBOX_TESTS = "RUN_SANDBOX_TESTS"
    CONSUME_RESULT = "CONSUME_RESULT"
    MARK_RUN_FAILED = "MARK_RUN_FAILED"
    CORE_RECOVER = "CORE_RECOVER"
    PRESENT_OWNER_GATE = "PRESENT_OWNER_GATE"
    CLOSE_DONE = "CLOSE_DONE"
    CLOSE_FAILED = "CLOSE_FAILED"
    CLOSE_BLOCKED = "CLOSE_BLOCKED"
    PERSISTENT_ERROR = "PERSISTENT_ERROR"

@dataclass(frozen=True)
class ReconcileDecision:
    job_id: str
    facts_version: int
    action: ReconcileAction
    reason: str
    dispatch_id: str | None = None
    wake_at: str | None = None
```

### 5.2 Adapter-Interfaces

```python
class RunStatusProvider(Protocol):
    def observe(self, lookup: RunLookup) -> RunObservation: ...

class RunLauncher(Protocol):
    def spawn(self, *, agent_id: str, dispatch_id: str,
              message_file: Path, timeout_seconds: int) -> None: ...

class WorkspaceStateProvider(Protocol):
    def scoped_hash(self, scope_root: Path) -> str: ...
    def predicted_hash(self, scope_root: Path, patch_set: list[dict]) -> str: ...
```

`RunLauncher.spawn()` liefert bewusst keine behauptete fachliche Completion. Binding und Result
werden ausschließlich durch `RunStatusProvider.observe()` entdeckt. Die reale Implementierung
startet `openclaw agent --agent <agent_id> --session-id dispatch-<dispatch_id>
--message-file <file> --json --timeout 900`; sie darf keine Konfiguration ändern.

### 5.3 Supervisor-API

```python
class SupervisorStore:
    def create_job(self, task_id: str, *, idempotency_key: str) -> SupervisorState: ...
    def get_job(self, job_id: str) -> SupervisorState: ...
    def get_job_for_task(self, task_id: str) -> SupervisorState | None: ...
    def list_nonterminal_jobs(self) -> list[SupervisorState]: ...

class Supervisor:
    def reconcile(self, job_id: str) -> ReconcileDecision: ...
    def perform_next_safe_action_if_required(
        self, decision: ReconcileDecision
    ) -> ActionOutcome: ...
    def receive_completion_hint(
        self, dispatch_id: str, event_meta: dict, result: dict
    ) -> ReceiveResult: ...

class SupervisorLoop:
    def run_once(self, job_id: str) -> ReconcileDecision: ...
    def run_until_terminal(self, job_id: str, stop_event=None) -> SupervisorState: ...
```

`SupervisorStore` wird wie `Store` gekapselt; öffentliche Queries sind read-only. Keine externe
Komponente erhält die rohe SQLite-Connection. Alle Mutationen verwenden `BEGIN IMMEDIATE`.
`SupervisorStore` darf Core-Tabellen in seiner Snapshot-Transaktion lesen, aber niemals mutieren;
alle Core-Mutationen laufen weiterhin ausschließlich über `Core`.

Da der Supervisor außer der Rolle auch `(cycle_no, position, sequence_kind)` benötigt, ergänzt
Phase 2C genau einen öffentlichen read-only Controller-Wrapper:

```python
Core.workflow_frontier(task_id: str, source: str) -> workflow.WorkflowFrontier
```

Er ruft nach `_require_controller(source)` die vorhandene `_workflow_frontier()`-Logik auf.
Der Supervisor kopiert diese Berechnung nicht und greift nicht direkt auf die private Methode zu.

---

## 6. Runtime-Adapter

### 6.1 Deterministischer Mock

`tests/mock_supervisor_runtime.py` erweitert nicht die Core-Semantik, sondern implementiert
`RunStatusProvider` und `RunLauncher` mit persistierbaren Fixtures:

- beliebige Statusfolge `NOT_FOUND → RUNNING → SUCCEEDED|FAILED`;
- forgefähige `agent_id/session_id/run_id/role/attempt`;
- gezählter Spawn pro Dispatch;
- Result bleibt nach Erzeugen einer neuen `Supervisor`-Instanz sichtbar;
- `authoritative_not_found` explizit steuerbar;
- Failure-Injection vor/nach jedem Journal-/Core-Schritt.

Zeit kommt aus einer Fake Clock; kein `sleep()` in Unit-Tests.

### 6.2 Reale `TrajectoryRunStatusProvider`

Die reale Implementierung ist read-only und allowlist-basiert:

1. Role→Agent-ID ist exakt die bestehende Phase-2B-Map
   `argent-lead|argent-analyst|argent-implementer|argent-qa|argent-reviewer`.
2. Für einen Dispatch wird ausschließlich
   `~/.openclaw/agents/<agent_id>/sessions/dispatch-<dispatch_id>.trajectory.jsonl`
   gelesen; keine freie Pfadübernahme aus Agent-Output.
3. Eine gültige Start-Bindung benötigt genau ein `type=session.started` mit
   `sessionId=dispatch-<dispatch_id>`, erwarteter `sessionKey`, nichtleerer `runId`,
   passendem `provider/modelId` und `data.agentId` (wenn vorhanden). Das tatsächliche Thinking-
   Tier wird aus der zugehörigen `trace.metadata.data.model.thinkLevel`-Zeile derselben run_id
   gelesen. **C1-Regel (verbindlich):** OpenAI-Runs (`openai/gpt-5.6-sol`, Rollen Lead/Reviewer)
   enthalten in der Trajectory KEINE `trace.metadata`-Zeile und `model.completed.data.model == null`;
   wenn `provider/modelId` der Startzeile exakt dem kanonischen Lead/Reviewer-Tupel entsprechen,
   gilt das Thinking-Tier als das im Dispatch erwartete (`high` — der einzige von
   `routing.validate_model_choice` für dieses Modell zugelassene Wert). `CONFLICT` wird NUR bei
   Widerspruch von `provider/modelId` gegen den Dispatch gemeldet, nie bei fehlender
   `trace.metadata`-Zeile. Fehlt die Zeile bei DeepSeek-Rollen (analyst/implementer/qa), bleibt es
   bei `UNKNOWN|CONFLICT`.
4. `type=session.ended` muss dieselbe `runId`, Session und Agent-ID tragen.
   `data.status=success` → technisch `SUCCEEDED`; terminaler Fehler/Timeout/Cancel →
   `FAILED|CANCELLED`; Start ohne Ende → `RUNNING`.
5. Das Result wird aus der in `session.started.data.sessionFile` referenzierten Datei gelesen,
   aber nur wenn deren kanonischer Pfad unter dem erwarteten Agent-Session-Verzeichnis liegt.
   Verwendet wird ausschließlich der letzte `message.role=assistant`-Text mit Timestamp zwischen
   `started_at` und `finished_at`; Thinking-/Tool-/Custom-Blöcke werden ignoriert. Der bestehende
   balancierte JSON-Extractor des E2E-Drivers wird in eine gemeinsame Helper-Funktion verschoben.
6. Mehrere unterschiedliche `session.started.runId` für denselben Dispatch, widersprüchliche
   Terminalzeilen, malformed JSONL oder nicht eindeutig zuordenbarer Assistant-Text → `CONFLICT`
   bzw. `UNKNOWN`, niemals Erfolg raten.
7. Eine fehlende Datei ist nur dann `authoritative_not_found=True`, wenn der erwartete Agent-
   Session-Ordner erfolgreich und vollständig gelesen wurde. I/O-Fehler, Permission-Fehler oder
   teilweise geschriebene letzte JSONL-Zeile → `UNKNOWN` und Backoff.

Trajectory-/Session-Inhalt ist externe DATA. Der Adapter führt daraus keine Befehle aus und liest
keine darin genannten weiteren Pfade außer dem streng validierten `sessionFile`.

### 6.3 Binding-Reparatur

Findet der Adapter für einen `PENDING|RECOVERY_PENDING`-Dispatch eine eindeutige Startzeile,
wird `Core.bind_spawn_result()` mit den **tatsächlich BEOBACHTETEN** Werten aufgerufen:
beobachtete `child_session_id` (sessionKey), beobachtete `runId` sowie beobachtete
provider/model/thinking-Werte aus der Trajectory (bzw. der C1-Regel in §6.2.3). Es werden
NIE die im Dispatch persistierten erwarteten Provider-/Model-/Thinking-Werte als
„beobachtet“ übergeben — die bestehende atomare Exact-Equality-/Policy-CAS-Prüfung
(`actual_provider == expected_agent_class` usw.) ist die maßgebliche Provenance-Grenze und
darf nicht durch Einspeisen erwarteter Werte ausgehebelt werden. Eine abweichende Beobachtung
wird über den normalen Reject-/Quarantänepfad verarbeitet; der Supervisor „korrigiert“ keine IDs.

---

## 7. `reconcile()` — Algorithmus und Entscheidungstabelle

### 7.1 Snapshot/CAS-Ablauf

Jeder Reconcile-Durchlauf ist dreiphasig:

1. unter kurzer Read-Transaktion: Job, Task, Task-Run, aktive Role-Run, Frontier, Dispatches,
   Handoff, Findings, Gate und Action-Journal laden;
2. außerhalb der SQLite-Transaktion höchstens eine Runtime-/Workspace-Beobachtung durchführen;
3. `BEGIN IMMEDIATE`, alle entscheidungsrelevanten Zeilen erneut laden; wenn deren IDs/Status oder
   `facts_version` abweichen, Beobachtung verwerfen und bei Schritt 1 neu beginnen; sonst
   `SupervisorState`-Projektion, `next_action`, Wake-Zeit und `facts_version+1` atomar persistieren.

Maximal drei interne Snapshot-Retries pro `reconcile()`-Aufruf; danach `BACKOFF` statt Busy-Loop.
`reconcile()` startet selbst keinen Agenten und wendet keinen Patch an. Es darf nur die Projektion,
Action-Planung und privacy-safe Supervisor-Events persistieren.

### 7.2 Priorisierte Entscheidungstabelle (verbindlich)

Die erste passende Zeile gewinnt:

| Priorität / Bedingung | Entscheidung / Aktion |
|---|---|
| Job bereits terminal | `NONE`; stale Events/Observations dürfen nichts öffnen |
| `tasks.state == DONE` | `CLOSE_DONE`; danach für immer `terminal=DONE` |
| Task `FAILED|CANCELLED` | `CLOSE_FAILED` |
| Task `BLOCKED` | `CLOSE_BLOCKED` |
| Job-Cache widerspricht Core-Ledger | Cache reparieren, dann in demselben Reconcile mit Core-Fakten fortfahren |
| aktuelles Gate `pending`, Task `OWNER_APPROVAL_REQUIRED` | einmalig `PRESENT_OWNER_GATE`, danach `WAITING_GATE`; keinen Dispatch starten |
| Gate `approved`, noch keine gebundene Execution/Closure | `WAIT`; nur exakt gescopeter Owner-Action-Executor darf fortfahren |
| Gate `consumed` + Execution + `closed_at` | Gate als geschlossen spiegeln, nie erneut präsentieren, normale Frontier fortsetzen |
| Gate `rejected` | `CLOSE_BLOCKED` |
| Gate `expired` und Task vom Core freigegeben | geschlossen spiegeln, normale Frontier fortsetzen; altes Event ignorieren |
| Gate-/Task-Kombination inkonsistent oder mehrere aktive Gates | `PERSISTENT_ERROR`, keine Folgeaktion |
| Frontier hat `expected_role=None`, Task ist nicht DONE | `PERSISTENT_ERROR`; Supervisor setzt DONE niemals selbst aus Sequenzlänge |
| aktiver Dispatch gehört nicht zu Frontier `(cycle,position,role,attempt)` | fremdes Result reject/quarantänisieren; bei Ledger-Konflikt `PERSISTENT_ERROR` |
| Dispatch `CONSUMED` | keine Result-Wirkung wiederholen; Frontier neu berechnen |
| Dispatch `FAILED|REJECTED|QUARANTINED`, Attempts < 3 | `START_ROLE` falls nötig, danach `CREATE_DISPATCH` für dieselbe Frontier |
| Dispatch terminal fehlgeschlagen, Attempts >= 3 | `CLOSE_FAILED` |
| kein aktiver Dispatch, passende aktive Role-Run fehlt | `START_ROLE` exakt für `expected_role` |
| kein aktiver Dispatch, passende Role-Run aktiv | `CREATE_DISPATCH` exakt für aktuelle Frontier; UNIQUE-Indizes entscheiden Race |
| Dispatch `PENDING|RECOVERY_PENDING`, eindeutiger vorhandener Start gefunden, noch ungebunden | `BIND_RUN` mit beobachteter Provenance |
| Dispatch `PENDING`, kein Run gefunden, kein `SPAWN_RUN`-Journal vorhanden | genau ein `SPAWN_RUN` planen |
| Spawn-Journal `RUNNING|UNCERTAIN`, noch kein Run sichtbar | `WAIT`, `DISCOVERING_RUN`; niemals Spawn erneut aufrufen |
| Spawn-Journal `RUNNING|UNCERTAIN` und 5 autoritative Missing-Bestätigungen erreicht | `CLOSE_BLOCKED`; die äußere Spawn-Grenze ist unauflösbar, kein Blind-Respawn (beim Implementer Grund `AMBIGUOUS_WRITER`) |
| gebundener Run `RUNNING` | `WAIT`; IDs spiegeln, kein Restart |
| gebundener Run `SUCCEEDED`, Dispatch nicht konsumiert, Provenance falsch | `receive_agent_result` Reject/Quarantäne; State/Frontier unverändert |
| gebundener Run `SUCCEEDED`, Write-Role, Broker/Test-Voraktionen offen | `APPLY_PATCH_SET` bzw. `RUN_SANDBOX_TESTS` über bestehende Grenzen |
| gebundener Run `SUCCEEDED`, alle Voraktionen erfüllt | `CONSUME_RESULT` über `Core.receive_agent_result()` |
| gebundener Run `FAILED|CANCELLED` | `MARK_RUN_FAILED`; danach ggf. `CORE_RECOVER`, dann Retry bis Attempt 3 |
| Binding existiert, Adapter `NOT_FOUND` autoritativ < 3-mal | `WAIT` mit Backoff; parallel nach exakter Session und run_id suchen |
| Binding existiert, 3 autoritative Missing-Bestätigungen | read-only: `MARK_RUN_FAILED`; Implementer: `CLOSE_BLOCKED` (`AMBIGUOUS_WRITER`) |
| Adapter `UNKNOWN|CONFLICT` | begrenzter Backoff; nach 5 Adapterfehlern `PERSISTENT_ERROR`, kein Respawn |

„Read-only Rolle“ bedeutet Lead, Analyst, QA und Reviewer bezüglich Produktcode. QA-Patches
bleiben über den bestehenden QA-Testscope gebunden. Für den Implementer gilt weiterhin die
Ghost-Writer-Regel: ohne technischen Terminalbeleg niemals automatisch einen zweiten Writer starten.

### 7.3 Completion-Hints und stale Events

`receive_completion_hint()` ist nur ein schneller Pfad in den bestehenden Core:

- es lädt den Dispatch anhand der behaupteten `dispatch_id`;
- ruft ausschließlich `Core.receive_agent_result()` auf; keine eigene Effect-Anwendung;
- verwendet als Idempotency-Key
  `supervisor:consume:<dispatch_id>:<run_id>:<sha256(canonical_result)>`;
- danach ruft der Loop `reconcile()` auf, unabhängig vom Hint-Ergebnis.

Gleiche Completion 2x/5x/20x → ein CAS-Konsum, alle weiteren `duplicate`. Alter Attempt N während
Attempt N+1 → alter Dispatch ist `FAILED|REJECTED|CONSUMED`, daher `stale_dispatch` oder valides
Same-Run-Duplikat; aktuelle Job-Projektion und Frontier bleiben unverändert. Falsche Session/run_id/
Task/Rolle/Envelope-Dispatch-ID → bestehende Phase-2B-Gründe und Quarantäne.

### 7.4 Integration mit `Core.recover()`

Ein normaler Supervisor-Prozessrestart ruft `Core.recover()` **nicht blind** auf: zuerst werden
Runtime und Bindings reconciled, damit ein laufender Agent `RUNNING` bleibt. Der bestehende
`Core.recover()`-Pfad bleibt die einzige konservative Ledger-Recovery und wird mit stabilem
Idempotency-Key nur verwendet, wenn ein technischer Terminal-/Missing-Befund einen vorher
`RECOVERY_PENDING`/`RECOVERING` gewordenen Task auflösen muss.

Da `Core.recover()` Owner-Authority verlangt, erhält der Supervisor die `OWNER_SOURCE` nur als
explizite Konstruktor-Abhängigkeit des lokal vom Owner gestarteten Controllers. Weder Result,
Event noch Runtime-Adapter kann diese Source liefern oder einen Recovery-Aufruf autorisieren.

Für legitime Results eines `RECOVERY_PENDING`-Dispatches wird `_receive_work` in Phase 2C minimal
ergänzt: Ist der Task `RECOVERING`, wird sein validiertes `resume_state` im selben
`BEGIN IMMEDIATE` vor State-Sync wiederhergestellt; danach greifen unverändert Output-Validierung,
CAS-Konsum, Effects, Role-Abschluss, Handoff und State-Sync. Ungültiges Resume-Ziel → Rollback und
BLOCKED. So wird die bestehende Recovery wiederverwendet, nicht als zweite Recovery-Logik kopiert.

---

## 8. Sichere Aktionsausführung und Crash-Punkte

### 8.1 Allgemeines Action-Protokoll

`perform_next_safe_action_if_required(decision)`:

1. prüft `decision.facts_version` gegen den aktuellen Job; stale Decision → No-op;
2. legt/liest die durch `action_key` eindeutige Journal-Zeile unter `BEGIN IMMEDIATE`;
3. `SUCCEEDED` → No-op; `PLANNED|FAILED` und Retry zulässig → `RUNNING` committen;
4. führt genau eine Aktion außerhalb der DB-Transaktion aus;
5. persistiert Ergebnis oder Fehler; Core-Kommandos verwenden denselben `action_key` als
   `idempotency_key`;
6. ruft unmittelbar erneut `reconcile()` auf.

Kanonische Action-Keys (alle Kleinbestandteile sind persistierte IDs/Zahlen, nie Agent-Text):

```text
supervisor:<job>:cycle:<cycle>:pos:<position>:attempt:<attempt>:start-role
supervisor:<job>:cycle:<cycle>:pos:<position>:attempt:<attempt>:create-dispatch
supervisor:<job>:dispatch:<dispatch>:spawn
supervisor:<job>:dispatch:<dispatch>:bind:<run_id>
supervisor:<job>:dispatch:<dispatch>:apply:<result_hash>
supervisor:<job>:dispatch:<dispatch>:tests:<workspace_effect_hash>
supervisor:<job>:dispatch:<dispatch>:consume:<run_id>:<result_hash>
supervisor:<job>:dispatch:<dispatch>:fail:<run_id>
supervisor:<job>:close:<DONE|FAILED|BLOCKED>
```

`attempt` für `START_ROLE/CREATE_DISPATCH` ist `1 + max(attempt_no)` an der aktuellen
`(cycle,position)`-Frontier. Der Core berechnet und prüft denselben Wert beim Insert; eine Race-
Abweichung führt zum Reload, nicht zu einem zweiten Dispatch.

Crash nach Core-Mutation, aber vor Journal-`SUCCEEDED`: Wiederholung trifft
`command_idempotency`/CAS und repariert anschließend das Journal. Crash vor Core-Mutation:
Wiederholung führt die Mutation einmal aus.

### 8.2 Spawn-vor-Bind (Sonderfall)

Vor `RunLauncher.spawn()` wird die eindeutige `SPAWN_RUN`-Zeile auf `RUNNING` gesetzt und committed.
Danach gilt:

- normal: Launcher startet mit `session-id=dispatch-<dispatch_id>`; Provider entdeckt
  `session.started`; `bind_spawn_result` bindet exakt;
- Crash nach Spawn, vor Binding: Journal bleibt `RUNNING`; Restart sucht zuerst Trajectory/
  Session, bindet den bestehenden Run und startet keinen zweiten;
- Crash nach Intent, vor Spawn-Aufruf: derselbe unauflösbare äußere Zustand; der Supervisor
  respawnt nicht blind. Nach fünf autoritativen Missing-Beobachtungen wird der Job BLOCKED;
- Crash nach Binding: Dispatch `RUNNING` gewinnt; Journal wird aus Binding repariert.

Der Launcher darf nie zweimal für dieselbe `dispatch_id` aufgerufen werden; Test und Unique-Index
erzwingen dies. Ein Retry ist immer ein neuer Core-Dispatch mit höherer `attempt_no`, nie ein
zweiter Spawn desselben Dispatches. `MAX_ACTION_RETRIES` gilt daher nicht für `SPAWN_RUN`:
dessen maximale externe Invocation-Anzahl pro Dispatch ist exakt 1.

### 8.3 Result-Konsum und Write-Aktionen

Für Lead/Analyst/Reviewer kann nach erfolgreicher Provenance direkt konsumiert werden. Für
Implementer/QA bleibt der Phase-2B-Ablauf verbindlich:

1. untrusted Patch-Feld extrahieren, Envelope für Core-Validierung getrennt halten;
2. erwarteten Scope-Hash vorab persistieren;
3. deterministischen erwarteten Nachher-Hash aus vollständigem Patch-Set berechnen;
4. ausschließlich `WorkspaceBroker.apply_patch_set()` verwenden;
5. Tests ausschließlich im bestehenden read-only bwrap-Runner ausführen;
6. tatsächliches Testresultat idempotent im Core persistieren;
7. erst danach Rollen-Envelope über den CAS konsumieren.

Crash während Broker-Aktion:

- aktueller Scope-Hash == `effect_hash` → Aktion als ausgeführt reparieren;
- Hash == `precondition_hash` → Broker-Aktion mit gleichem Action-Key erneut zulässig
  (Broker ist Patch-Set-atomar);
- anderer Hash → `PERSISTENT_ERROR/WORKSPACE_DIVERGED`, kein Überschreiben.

Agent-Output darf weder Broker-Scope, Workspace-Root, Testkommando noch Folgeaktion bestimmen.
Scope und bwrap-Kommando stammen aus dem persistierten Task-/Rollenvertrag.

---

## 9. Retry, Backoff, Wait und Fehlerzustände

Verbindliche Konstanten:

```python
RUNNING_POLL_SECONDS = 2.0
BACKOFF_INITIAL_SECONDS = 1.0
BACKOFF_MULTIPLIER = 2.0
BACKOFF_MAX_SECONDS = 30.0
MAX_SNAPSHOT_RETRIES = 3
MAX_ACTION_RETRIES = 5
MAX_RUNTIME_UNKNOWN = 5
MISSING_BOUND_RUN_CONFIRMATIONS = 3
MISSING_UNBOUND_SPAWN_CONFIRMATIONS = 5
MAX_DISPATCH_ATTEMPTS_PER_STEP = 3
AGENT_TIMEOUT_SECONDS = 900
```

Backoff ist deterministisch und ohne Jitter:
`min(1 * 2**retry_count, 30)` Sekunden. Ein erfolgreicher technischer Fortschritt
(neue Bindung, Statuswechsel, Konsum, Handoff, Gate-Closure, Frontier-Wechsel) setzt
`retry_count=0`, aktualisiert `last_progress_at` und löscht `last_error_code`.

Der Loop lautet verbindlich:

```python
while state.terminal is None and not stop_event.is_set():
    decision = supervisor.reconcile(job_id)
    supervisor.perform_next_safe_action_if_required(decision)
    state = supervisor.store.get_job(job_id)
    waiter.wait_until(state.next_wake_at, stop_event)  # interruptible, kein Busy-Loop
```

`Waiter` ist injizierbar; Tests verwenden Fake Clock. Pro Iteration höchstens eine äußere
Runtime-Beobachtung und eine sichere Aktion. Dauerfehler werden als `ERROR`/
`PERSISTENT_ERROR` persistiert und nach den Grenzen nicht endlos weiterprobiert.

---

## 10. Owner-Gate-Memory

### 10.1 Exakte Bindung

Ein Gate ist ausschließlich die persistierte Core-Zeile mit
`gate_id, task_id, action, scope, binding_hash, created_at, status, approved_at,
execution_id, executed_at, closed_at`. Approval gilt nur, wenn der vom Owner gelieferte
`task_id/action/scope` und der neu berechnete `binding_hash` exakt entsprechen. Kein Agent-Output,
Event oder Supervisor-Cache darf Approval erzeugen oder Scope erweitern.

### 10.2 Zustände

- `pending`: Gate offen, höchstens einmal in der lokalen UI präsentieren;
- `approved`: Permission für exakt diesen Scope, noch keine Behauptung der Ausführung;
- `consumed`: Execution-Zeile gebunden, `executed_at` und `closed_at` gesetzt, Gate endgültig zu;
- `rejected|expired`: endgültig zu; keine Ausführung.

`approved + executed + closed` wird nach jedem Restart als CLOSED geladen. Es gibt keinen neuen
Owner-Prompt, keine neue Approval-Zeile und kein Wiederöffnen. Ein stale `gate.owner_required`-
oder `gate.owner_approved`-Event ändert nichts. Eine neue Aktion mit anderem `action` oder `scope`
hat einen anderen `binding_hash` und benötigt ein neues Gate.

### 10.3 Gate und Supervisor-Aktionen

Der Supervisor führt keine beliebige approved Aktion selbst aus. Nur ein registrierter,
technisch fest verdrahteter Handler darf exakt das gespeicherte Binding ausführen; andernfalls
bleibt der Job `WAITING_GATE`. Background-Wake-Aktivierung besitzt in Phase 2C keinen Handler und
kann daher weder installiert noch als ausgeführt markiert werden.

---

## 11. Restart-/Recovery-Invarianten

Für jede Simulation gilt nach Neuinstanziierung von `Core`, `SupervisorStore`, Provider und Loop:

1. **Tod während Analyst-Run:** exakte Trajectory-Bindung wird gefunden; `RUNNING` → warten;
   genau ein Analyst-Dispatch/Run.
2. **Tod während Implementer-Run:** vorhandenen Writer finden und warten; bei unbekanntem Status
   `AMBIGUOUS_WRITER`, niemals zweiter Implementer.
3. **Tod nach QA-Completion vor Consume:** Provider zeigt `SUCCEEDED`; Test-/Broker-Voraktionen
   anhand Journal/Workspace reparieren; Core-CAS genau einmal.
4. **Tod während Rework:** `cycle_no`, `sequence_kind`, offene Findings und Lead-Decision aus
   Ledger rekonstruieren; keine Findings verlieren; exakt aktuelle Frontier fortsetzen.
5. **Tod bei offenem Gate:** dasselbe Gate/Binding laden; pending bleibt pending, closed bleibt
   closed; kein Duplikatprompt nach Closure.
6. **Tod direkt vor DONE:** finaler Lead-Dispatch entweder unconsumed → einmal konsumieren oder
   consumed → State-Sync/DONE aus Ledger erkennen; keinen neuen Lead starten.
7. **Tod direkt nach DONE:** `tasks.state=DONE` gewinnt; Job auf DONE schließen; keine Aktion.
8. **Tod zwischen Dispatch-Create und Spawn-Binding:** vorhandenen PENDING-Dispatch und dessen
   Spawn-Journal finden; Trajectory suchen; nie blind respawnen.
9. **Tod zwischen Result-State-Mutation und Event-Verarbeitung:** Core-Transaktion ist ganz oder
   gar nicht; nach Restart `CONSUMED` ignorieren oder unconsumed Result erneut validieren.

DONE ist sticky. Weder alte Completion, alter Gate-Event, Runtime-UNKNOWN noch Cache-Abweichung
darf DONE verlassen oder eine neue Folgeaktion planen.

---

## 12. Deterministische Tests

Neue Tests laufen offline mit temporärer SQLite-Datei, Fake Clock, Mock Runtime und vorhandenem
`tests/phase2a_helpers.py`-/`tests/mock_runtime.py`-Muster. Keine Sleeps, kein Netz, keine echten
OpenClaw-Runs in pytest.

### 12.1 Persistence und Rekonstruktion

- SupervisorState vollständig speichern, Connections schließen, neu öffnen, identische
  rekonstruierte Sicht;
- Cache-Felder absichtlich stale → Core-Ledger gewinnt, Cache repariert;
- aktuelle Handoff-ID, offene Findings, Rework-Zyklus und Attempts korrekt nach Reload;
- `last_progress_at`, Backoff, Recovery-/Error-State persistieren.

### 12.2 Completion, Provenance und Exactly-once

- gleiche Completion 2x, 5x und 20x → exakt ein `CONSUMED`, eine Effect-Anwendung, ein Role-
  Abschluss, ein Handoff;
- alter Attempt N während N+1 → Reject/Quarantäne, Task/Frontier/Job unverändert;
- falsche run_id, session_id, task_id, role und Envelope-dispatch_id jeweils separat;
- verlorener Completion-Push: Provider `SUCCEEDED` → trotzdem konsumiert;
- `RUNNING` nach Supervisor-Restart → kein Spawn, kein neuer Dispatch;
- `SUCCEEDED` aber unconsumed nach Restart → genau einmal konsumiert;
- `CONSUMED` vor Restart → keinerlei Wiederanwendung;
- Prosa-Regressionsfälle aus §2.2.

### 12.3 Failure und Retry

- terminal fehlgeschlagener read-only Run → `mark_agent_failed`, neue Attempt 2;
- drei fehlgeschlagene Attempts → terminal FAILED, kein Attempt 4;
- gebundener Run fehlt: erst Suche/Backoff, kein Blind-Respawn;
- Implementer fehlt/unknown → BLOCKED/AMBIGUOUS_WRITER, kein zweiter Writer;
- UNKNOWN fünfmal → persistenter Fehler;
- Backoff exakt 1,2,4,8,16,30,30 Sekunden und Fake-Waiter statt Busy-Loop;
- erfolgreicher Fortschritt setzt Zähler zurück.

### 12.4 Gate-Persistence

- pending nach Restart bleibt dasselbe Gate;
- Approval nur mit exaktem Task/Action/Scope/Hash;
- out-of-scope Aktion → neues Gate;
- approved+executed+closed → Restart → CLOSED, null neue Prompts;
- rejected/expired/consumed durch stale Event nicht wieder geöffnet;
- Agent-Output mit erfundener Approval-ID erzeugt keine Zeile;
- Migration-Backfill und NULL-/Mehrfach-Execution-Fail-Closed.

### 12.5 Crash-Matrix

Failure-Injection jeweils vor und nach persistiertem Schritt:

- Crash zwischen Role-Start und Dispatch-Create;
- Crash nach Dispatch-Create vor Spawn-Journal;
- Crash nach Spawn-Journal vor Launcher-Aufruf;
- Crash nach realisiertem Mock-Spawn vor Binding;
- Crash nach Binding;
- Crash nach Broker-Mutation vor Journal-Success;
- Crash nach Result-CAS vor Supervisor-Snapshot;
- Crash zwischen Event-Hint und Reconcile;
- Crash direkt vor und nach DONE.

Nach jedem Crash: neue Objekte/DB-Connection; Dispatch-/Spawn-/Effect-Zähler und alle Ledger-
Zeilen assertieren. Keine reine In-memory-Fortsetzung zählt als Restart-Test.

### 12.6 Bestehende Recovery

- `Core.recover()` zweimal bleibt idempotent für `RECOVERY_PENDING`;
- runtime-aware Supervisor wartet bei vorhandenem Run;
- legitimes Result aus RECOVERING wird über den atomaren Recovery-Prelude konsumiert;
- fehlgeschlagener Recovery-Run wird konservativ beendet und Core-Recovery aufgerufen;
- keine verlorenen Findings/Handoffs/Gates.

---

## 13. Echter Phase-2C-Recovery-E2E-Smoke

Neuer Driver: `smoke/phase2c_recovery_e2e.py`. Er arbeitet auf Scratch-DB und einer temporären
Kopie von `e2e-fixture`; keine Config-Änderung, kein Push.

### 13.1 Vorbereitung

1. Baseline-Hash von OpenClaw-Konfiguration und `git status --porcelain` erfassen (read-only).
2. frische Scratch-DB, Projekt, Task und Task-Run wie Phase-2B Task 1 erzeugen;
3. Supervisor-Job mit stabilem Idempotency-Key anlegen;
4. Position 0 (Lead) über denselben realen Controllerpfad erfolgreich konsumieren, damit die
   Frontier exakt auf Analyst Position 1 steht.

### 13.2 Tod während eines echten Analyst-Runs

5. Supervisor als separaten lokalen Prozess starten; er erzeugt genau einen Analyst-Dispatch,
   `agent_id=argent-analyst`, Session-Label `dispatch-<dispatch_id>` und Spawn-Journal;
6. warten, bis die exakte Analyst-Trajectory `session.started` mit run_id enthält; Dispatch-ID,
   run_id und Anzahl Analyst-Trajectories festhalten;
7. ausschließlich den Supervisor-Prozess hart beenden (Test-Hook/SIGKILL); der OpenClaw-Gateway-
   Run ist getrennt und darf weiterlaufen; keine Agent-/Gateway-Konfiguration verändern;
8. neue Core-/Supervisor-Instanz aus derselben DB laden und `reconcile()` aufrufen;
9. Provider muss denselben Analyst-Run finden: solange `RUNNING` warten, niemals Launcher erneut
   aufrufen; nach `session.ended(success)` bestehende Session/run_id binden und Result einmal
   konsumieren;
10. assertieren: exakt ein Dispatch für `(cycle=1, position=1, attempt=1)`, exakt eine
    `session.started.runId`, kein zweiter Analyst-Spawn.

### 13.3 Tod zwischen Workflow-Schritten und autonome Fortsetzung

11. direkt nach Analyst-Konsum, aber vor Erzeugung des nächsten Lead-Dispatches, Supervisor erneut
    beenden;
12. neu laden; Frontier muss Lead Position 2 ergeben und genau einen legitimen nächsten Dispatch
    erzeugen;
13. den vorhandenen Phase-2B-Broker-/bwrap-/Agentpfad durch den Supervisor bis `tasks.state=DONE`
    weiterlaufen lassen; jeder Role-Run ist eine getrennte Session, nur Implementer schreibt
    Produktcode, QA nur Tests;
14. nach DONE weiteren Reconcile-Durchlauf ausführen: `next_action=NONE`, keine neue Trajectory.

### 13.4 Duplicate-/Stale-Injektion

15. die exakte Analyst-Completion nach Konsum 20-mal über `receive_completion_hint` zustellen;
    Ergebnis jedes Mal `duplicate`, Findings/Role/Handoff/State nur einmal;
16. zusätzlich Completion mit fremder run_id und eine alte Completion aus Attempt N gegen einen
    künstlich vorbereiteten Attempt N+1 zustellen; beide rejected/quarantined, aktueller State
    unverändert;
17. Supervisor nochmals neu laden und DONE-Stickiness prüfen.

### 13.5 Abschlussasserts des Smokes

- gleicher Run nach Restart gefunden, kein doppelter Agent;
- autonomous bis DONE;
- exakt einmal konsumiert;
- Duplicate/Stale fail-closed;
- keine offenen HIGH/CRITICAL oder unakzeptierten relevanten MEDIUM Findings;
- Config-Hash unverändert, keine systemd-/cron-Einträge, kein Mail-/Visualizer-/Gateway-Change;
- Scratch-Artefakte dokumentiert und kontrolliert entfernt; Projekt-Working-Tree unverändert außer
  den vorgesehenen Phase-2C-Dateien.

---

## 14. Sicherheitsgrenzen (unverändert Phase 2B)

- Rollen-Agenten behalten zero dangerous or mutating tools; direkte Turns haben nur die bereits
  freigegebene harmlose Status-Fähigkeit, Subagent-Spawns fail closed.
- Kein Rollen-Agent erhält Core-, SQLite-, Exec-, Process-, Web-, Secret-, Mail- oder Host-FS-
  Zugriff.
- Agent-Code/-Tests/-Patches sind untrusted; Produktwrites nur über Write-Broker, Ausführung nur
  im bestehenden bwrap-Sandbox-Runner.
- Der Supervisor schreibt keinen Produktcode direkt und interpretiert Agent-Text nicht als
  Kommando.
- Externe Inhalte dürfen keinen Spawn, Retry, Gate, Scope, Approval oder DONE autorisieren.
- Keine technische Notwendigkeit in Phase 2C rechtfertigt eine OpenClaw-Konfigänderung.
- `mail-agent-v2-stable-canary` bleibt unangetastet.

---

## 15. Implementierungsreihenfolge (verbindlich)

1. Schema V4 + Migration/Queries/Modelle;
2. Owner-Gate-Closure-Felder atomar in bestehende Core-Kommandos integrieren;
3. `SupervisorStore`, Projektion und pure Decision-Funktion;
4. Mock Runtime/Fake Clock und Entscheidungstabellen-Tests;
5. Action-Journal + idempotente Core-Aktionen;
6. Trajectory-Provider und Spawn-Recovery;
7. Broker-/Workspace-Crash-Reconciliation;
8. Loop/CLI ohne Background-Installation;
9. vollständige deterministische Crash-/Gate-/Duplicate-Suite;
10. echter Recovery-E2E-Smoke;
11. unabhängiger GPT-5.6 Sol High READ-ONLY Closing Review und verifizierte Remediation.

Nach jedem delegierten Implementierungsschritt verifiziert der Supervisor selbst Diff, Tests und
Ledger-Invarianten. Worker-Erfolgsmeldungen sind keine Abnahme.

---

## 16. Abnahmekriterien Phase 2C

Phase 2C ist nur abgeschlossen, wenn alle Punkte gleichzeitig erfüllt sind:

1. alle bisherigen 815 Tests grün, keine Deaktivierungen/Skips;
2. alle neuen Phase-2C-Tests grün;
3. Persistenz-/Reload-, Restart-, Crash-, Duplicate-, Stale- und Exactly-once-Matrix grün;
4. Owner-Gate-Persistence inkl. approved+executed+closed→Restart→CLOSED grün;
5. echter Recovery-E2E bis DONE grün, vorhandener Run nach Supervisor-Tod gefunden, kein
   Doppelagent;
6. Duplicate-/Stale-E2E grün;
7. bounded Retry/Backoff und persistent error states nachgewiesen, kein Busy-Loop;
8. keine verlorenen Findings/Handoffs, keine wiedergeöffneten Gates, DONE bleibt DONE;
9. keine unautorisierten Config-/System-/Gateway-/Secret-/Mail-/Visualizer-Änderungen;
10. unabhängiger Sol-High Closing Review = `VERIFIED`, alle bestätigten Findings behoben und
    Regressionen erneut grün;
11. Working Tree nach lokalem Abschlusscommit clean; Commit lokal, kein Push.

Erst dann darf der Abschlussbericht exakt den Marker ausgeben:

`ARGENT DEVELOPMENT TEAM PHASE 2C — SUPERVISOR_PERSISTENCE_GREEN`

## 17. Analyst-Review-Amendment (verbindlich, 2026-08-28, Lead-bewertet)

Der unabhängige Spec-vs-Code-Review (Pro-Analyst, gegen den realen Code und reale
Trajectories) ergab SPEC_NEEDS_AMENDMENT. Die nach Lead-Bewertung bestätigten
Amendments sind verbindlich; Abweichungen in früheren Abschnitten sind hiermit
ersetzt:

A1 (C1, HIGH, §6.2.3 — IN PLACE EINGEARBEITET): OpenAI-Runs (Lead/Reviewer,
`openai/gpt-5.6-sol`) haben keine `trace.metadata.data.model.thinkLevel`-Zeile.
Wenn `provider/modelId` der Startzeile exakt dem kanonischen Tupel entspricht,
gilt das erwartete Tier `high`; `CONFLICT` nur bei provider/model-Widerspruch.

A2 (C2, MEDIUM-HIGH, §6.3 — IN PLACE EINGEARBEITET): `bind_spawn_result()` erhält
ausschließlich BEOBACHTETE Werte (sessionKey, runId, provider/model/thinking aus
der Trajectory). Niemals Dispatch-erwartete Werte als „beobachtet“ übergeben; die
Exact-Equality-CAS bleibt die Provenance-Grenze.

A3 (C3, MEDIUM, §7.4): KEINE Änderung an `_receive_work`. Der RECOVERING→
`resume_state`-Prelude existiert bereits am Anfang von `_apply_state_sync` INNERHALB
der Consume-Transaktion (`BEGIN IMMEDIATE`); dort bleibt er. §7.4 ist entsprechend
zu lesen: „wird … im selben BEGIN IMMEDIATE vor State-Sync wiederhergestellt“
beschreibt den IST-Zustand des Core, keine neue Ergänzung.

A4 (C4, MEDIUM, §5.1/§6.2): `RunObservation.session_id` == **sessionKey**
(`agent:<agent_id>:explicit:dispatch-<id>`), identisch zu `child_session_id` des
Dispatches. `sessionId` (`dispatch-<id>`) ist nur das Trajectory-Datei-Label und
wird nie gebunden.

A5 (G1, MEDIUM, §5.3/§7.4): Der Supervisor-Konstruktor erhält ZWEI explizite
Quellen: `controller_source="role:lead"` (create_dispatch/bind_spawn_result/
receive_agent_result/mark_agent_failed/expected_next_role/snapshot_agent_context/
start_role/workflow_frontier) und `owner_source=OWNER_SOURCE` (recover/approve/
reject/execute_approved). Keine andere Quelle autorisiert Orchestrierung.

A6 (G2, MEDIUM, §5.3/§4.1): `create_job` ist idempotent über deterministischen
`job_id = "supervisor:" + task_id` UND `command_idempotency`-Reuse
(`command="create_supervisor_job"`); Replay liefert denselben Job.

A7 (G3, MEDIUM, §7.2): Entscheidungstabelle erhält die Zeile: „gebundener Run
SUCCEEDED, Write-Rolle (implementer|qa), Sandbox-Tests gelaufen, Ergebnis noch
nicht persistiert → `RECORD_TEST_RESULT` (idempotent via `record_test_run`, Source
= Rollen-Source, NUR solange die Rolle aktiv ist) → danach `CONSUME_RESULT`.“

A8 (G4, MEDIUM, §5.2/§8.2): `RunLauncher.spawn()` startet den Agenten DETACHED
(`Popen(..., start_new_session=True)`, nicht `subprocess.run`) und kehrt sofort
zurück; der Agent-Prozess überlebt den Supervisor-SIGKILL (Voraussetzung für den
§13-Smoke).

A9 (G5/G6, LOW, §7.3): `receive_completion_hint` fängt alle `ArgentError`-Subtypen
fail-closed (→ rejected/unknown ReceiveResult, nie Propagation in den Loop);
`canonical_result = json.dumps(result, sort_keys=True, default=str)` (identisch zu
`_hash_args`-Semantik des Core).

A10 (C5/C6 + Kleinigkeiten, LOW): §7.2-Retry-Zeile nennt nur `FAILED|REJECTED`
(QUARANTINED ist im Core tot; ein quarantäniertes RESULT lässt den Dispatch
RUNNING). `NOT_OBSERVED` ist reiner Persistenz-Sentinel ohne `RunStatus`-Gegenstück.
§6.2.5: `sessionFile`-Message-Timestamps (ms-Epoch-Ints) vs. Trajectory-`ts`
(ISO-Strings) — Vergleich einheitlich in Sekunden (ms/1000). §6.2.1: die
Role→Agent-Map wird in `argent_core/supervisor.py` als EINZIGE Quelle definiert;
`smoke/phase2b_e2e.py` importiert sie (kein Drift).
