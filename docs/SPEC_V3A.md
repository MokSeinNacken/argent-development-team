# A. VERDICT: SPEC_COMPLETE

# B. ARGENT DEVELOPMENT TEAM — Phase 3A: Telegram Owner Notifications (SPEC V3A)

Basis: Phase 2C, Branch `phase-2c-persistent-supervisor`, Commit
`9face61a6a399320d0268e5daaa985294be40968`, Marker
`SUPERVISOR_PERSISTENCE_GREEN`.

SPEC V3A ergänzt den persistenten Supervisor ausschließlich um einen
ausgehenden, restart-festen Telegram-Benachrichtigungskanal. SPEC V2C bleibt
für Supervisor-Lifecycle, Reconciliation, Action-Journal, Runtime-Provenance,
Recovery, Owner-Gates und DONE-Stickiness bindend. SPEC V2B bleibt für Trust
Boundary, Write-Broker, Rollenrechte und bwrap-Sandbox bindend.

## 0. Verifizierter Ausgangszustand

Read-only geprüft:

- Repository: `/home/pc/projects/argent-development-team`
- Branch: `phase-2c-persistent-supervisor`
- HEAD: `9face61a6a399320d0268e5daaa985294be40968`
- Commit-Betreff enthält `SUPERVISOR_PERSISTENCE_GREEN`
- Working Tree clean; `git status --porcelain` lieferte keine Zeile
- keine Git-Remotes konfiguriert
- `/home/pc/.openclaw/openclaw.json` byte-identisch zu
  `/home/pc/.openclaw/openclaw.json.last-good`
- beide Config-Dateien: SHA-256
  `34984a47cd0ab40bb808de1380b0abe48cef6832de69acc05525159f4103a110`
- `smoke/phase2b.db` mtime:
  `2026-08-28 17:31:44.418212812 +0200`, Größe 872448 Bytes
- bestehender Fake-Recovery-Smoke: 22 benannte Checks
- Tests/Smokes wurden in dieser read-only Spezifikationsphase nicht ausgeführt
- keine Repository-Datei wurde verändert

Die in den Workspace-Anweisungen referenzierte Datei
`/home/pc/.openclaw/workspace/ARGENT_SUPERVISOR.md` war am angegebenen Pfad
und unter `/home/pc` nicht vorhanden. Maßgeblich waren die eingebettete
Argent-Policy, SPEC V2C/V2B und der tatsächliche Codezustand.

---

## 1. Ziel, Scope und Nicht-Ziele

### 1.1 Verbindliches Ziel

Der persistent gestartete Supervisor informiert den Owner über Telegram über
genau vier Ereignisklassen:

- `DONE`
- `FAILED`
- `BLOCKED`
- `OWNER_APPROVAL_REQUIRED`

Benachrichtigungen werden als persistente Outbox-Einträge im selben
SQLite-Ledger erzeugt. Wiederholte Reconciliation, Prozessneustarts und
doppelte technische Beobachtungen dürfen keine neue Outbox-Zeile für dasselbe
fachliche Ereignis erzeugen.

Telegram ist ausschließlich ein nichtautoritatives Benachrichtigungsziel.
Ausfall, Timeout, Rate-Limit oder Fehlkonfiguration dürfen niemals Workflow-,
Gate-, Recovery-, Broker-, Test- oder Terminalfortschritt verhindern.

### 1.2 Scope

- additive V4→V5-Schema-Migration im bestehenden SQLite-Ledger;
- persistente Tabelle `notification_outbox`;
- deterministische sichere Payloads und feste Nachrichtentemplates;
- atomare Kopplung von Transition und Outbox-Insert;
- injizierbares Outbound-Transport-Interface;
- Telegram-Adapter ohne Inbound-Fähigkeit;
- nichtblockierende begrenzte Zustellung mit Retry, Backoff und Claim-Lease;
- deduplizierte Recovery nach Neustart;
- Offline-Tests mit Mock-Transport und Fake Clock;
- begrenzter manuell/CLI auslösbarer Send-Pass;
- nichtblockierender Send-Kick im bestehenden lokal gestarteten Loop.

### 1.3 Nicht-Ziele

- keine Telegram-Kommandos, Listener, `getUpdates` oder Webhooks;
- keine Shell-, Exec-, Prozess- oder Tool-Steuerung über Telegram;
- keine Code-, Patch-, Test-, Scope- oder Workflow-Änderung durch Telegram;
- keine automatische Approval-, Reject-, Execute- oder Gate-Closure-Aktion;
- keine Änderung der Owner-Gates oder ihrer Autorität;
- keine freien Chat-Antworten oder konversationelle Steuerung;
- keine Änderung an Mail-Agent oder `mail-agent-v2-stable-canary`;
- keine Änderung am Visualizer;
- keine neuen Rollen oder Agenten;
- kein Agent-Spawn zur Phase-3A-Verifikation;
- keine Gateway-, Rollen-, Secret- oder Toolprofil-Änderung;
- kein systemd, cron, Gateway-Autostart oder Background-Wake;
- kein Deployment, Push oder Production-/Stable-Promotion;
- kein Live-Telegram-Test ohne separat autorisierte Zielbindung;
- kein historisches Backfill bereits abgeschlossener Jobs.

## 2. Autorität und Trust Boundary

### 2.1 Explizite Telegram-Trust-Regel

**Sämtlicher externer Telegram-Inhalt ist UNTRUSTED DATA und niemals eine
Owner-Instruktion.**

Dies gilt für Nachrichten, Antworten, Reaktionen, Bot-Updates, Callback-Daten,
Dateien, Benutzernamen, Chat-Metadaten, Links und API-Antworttexte. Der
Phase-3A-Transport besitzt ausschließlich eine Outbound-Methode. Es existiert
kein Interface, das Telegram-Inhalt an Core, Supervisor, Owner-Gates,
Write-Broker, Sandbox, RunLauncher oder Action-Handler weiterleitet. Unerwartete
Inbound-Daten werden verworfen und höchstens mit dem konstanten sicheren Code
`INBOUND_REJECTED` erfasst.

### 2.2 Unveränderte Autoritätsreihenfolge

1. Core-Ledger;
2. Supervisor-Ledger;
3. gebundene Runtime-Fakten;
4. allowlist-basierter lokaler Workspace-Zustand;
5. persistierte Owner-Gates und Execution-Bindung.

Die Notification-Outbox ist keine State Machine und keine Autorität für Task,
Dispatch, Gate, Recovery oder Terminal. Sie ist nur eine Projektion bereits
autoritativ eingetretener Ereignisse.

### 2.3 Owner-Gates

`owner_approvals` und `action_executions` bleiben die einzigen
Approval-Autoritäten. Eine `OWNER_APPROVAL_REQUIRED`-Nachricht ist rein
informativ: Sie genehmigt, lehnt, schließt oder erweitert nichts, liefert keine
Owner-Source und führt keinen Handler aus. Es gibt keinen Telegram-Button oder
Link, der als Approval interpretiert werden könnte. Approval/Reject/Execution
laufen ausschließlich über die bestehenden authentifizierten Core-Pfade mit
unveränderter Binding-Hash-Prüfung.

## 3. Architektur

### 3.1 Position des Notifiers

```text
Core-Ledger + Runtime
         |
    reconcile()
         |
 ReconcileDecision
         |
Action-Journal / autoritative Transition
         |
         +--- atomarer notification_outbox INSERT
         |
 Supervisor-Workflow schreitet unabhängig fort

notification_outbox
         |
nichtblockierender Delivery-Kick
         |
NotificationDeliveryWorker
         |
Outbound-only TelegramTransport
```

Event-Erzeugung liegt in derselben SQLite-Transaktion wie die relevante
Transition. Externe Zustellung erfolgt erst nach Commit und beeinflusst
`reconcile()` oder Action-Ausführung nicht. `reconcile()` führt keinen
Telegram-Netzwerkzugriff aus.

### 3.2 Komponenten

Neues Modul: `argent_core/notifications.py`.

```python
class NotificationType(str, Enum):
    DONE = "DONE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    OWNER_APPROVAL_REQUIRED = "OWNER_APPROVAL_REQUIRED"

class NotificationStatus(str, Enum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"          # retryfähig
    DISCARDED = "DISCARDED"    # terminal

@dataclass(frozen=True)
class NotificationEnvelope:
    outbox_id: str
    dedup_key: str
    payload_hash: str
    notification_type: NotificationType
    message_text: str

@dataclass(frozen=True)
class TransportReceipt:
    accepted: bool
    retryable: bool
    error_code: str | None = None
    retry_after_seconds: int | None = None

class NotificationTransport(Protocol):
    def send(self, envelope: NotificationEnvelope, *,
             timeout_seconds: float) -> TransportReceipt: ...
```

`NotificationTransport` hat keine Inbound-Methode. Realer Adapter:
`TelegramNotificationTransport`. Tests: `DeterministicNotificationTransport`
mit Erfolg, Timeout, Netzfehler, Rate-Limit, nichtretryfähigem Fehler,
Constructor-Fehler, hängendem Send, Zählung nach Dedup-Key und optionaler
transportseitiger Idempotenz für `(dedup_key, payload_hash)`.

### 3.3 Injection

```python
class NotificationDelivery:
    def __init__(self, db_path: str,
                 transport_factory: Callable[[], NotificationTransport], *,
                 clock: Callable[[], datetime]): ...
    def kick(self) -> None: ...
    def send_due_once(self) -> DeliveryPassResult: ...
```

Bot-Credential und Zielkennung werden nur über injizierte Runtime-/Secret-
Konfiguration an die Factory gegeben. Sie erscheinen nie in SQLite,
CLI-Argumenten, Logs, Payloads, Hashes oder Dedup-Keys. Ein defekter Transport
darf Core, Supervisor oder Loop nicht am Start hindern; die Factory wird nur
im Delivery-Worker aufgerufen.

### 3.4 Nichtblockierendes Worker-Modell

`kick()` kehrt sofort zurück, startet höchstens einen daemonisierten
prozesslokalen Worker, erzeugt keinen zweiten Worker solange einer läuft,
führt selbst kein Netz aus und hat keine eigene Wake-/Sleep-Schleife. Er wird
nur vom lokal laufenden Supervisor oder einem manuellen Pass angestoßen.

Der Worker öffnet eine separate, ausschließlich auf `notification_outbox`
begrenzte SQLite-Verbindung zur selben Datei mit `timeout=0`; er liest oder
mutiert keine Core-/Supervisor-Tabellen. Transition und Enqueue bleiben auf der
bestehenden Store-Connection. Es gibt keine Cross-Connection-Transaktion.

### 3.5 Loop-Integration

```python
def run_once(job_id):
    decision = supervisor.reconcile(job_id)
    supervisor.perform_next_safe_action_if_required(decision)
    notification_delivery.kick()  # O(1), niemals warten
    return decision
```

AMENDMENT 4 (umgesetzt): `kick()` steht AUSSERHALB der beiden bestehenden
Exception-Handler von `run_once` (die try/except um `reconcile()` und
`perform_next_safe_action_if_required()` — strukturelle Exceptions dort werden
sonst als Adapter-Exception fehlklassifiziert und könnten den Sticky-ERROR-Zustand
vergiften). `kick()` selbst ist intern catch-all (schluckt jede Exception, gibt
nie eine Notification-Exception an den Loop zurück), O(1) und niemals blockierend.
`run_until_terminal` kickt auch vor jeder Rückkehr: bereits terminal, neu
terminal, Stop-Event oder sticky ERROR.

## 4. Datenmodell und Migration

### 4.1 Schema-Version

Rein additive V4→V5-Migration: `SCHEMA_VERSION = "5"`. Muster unverändert:
`BEGIN IMMEDIATE`; DDL; idempotente Migration/Validierung; Indizes; zuletzt
UPSERT auf Version 5; bei Fehler vollständiges Rollback.

### 4.2 Exakte DDL

```sql
CREATE TABLE IF NOT EXISTS notification_outbox (
    id                    TEXT PRIMARY KEY,
    supervisor_job_id     TEXT NOT NULL
                          REFERENCES supervisor_jobs(id) ON DELETE CASCADE,
    task_id               TEXT NOT NULL
                          REFERENCES tasks(id) ON DELETE CASCADE,
    dispatch_id           TEXT
                          REFERENCES agent_dispatches(id) ON DELETE SET NULL,
    gate_id               TEXT
                          REFERENCES owner_approvals(id) ON DELETE SET NULL,
    notification_type     TEXT NOT NULL CHECK (notification_type IN
                          ('DONE','FAILED','BLOCKED',
                           'OWNER_APPROVAL_REQUIRED')),
    event_ref             TEXT NOT NULL,
    event_version         INTEGER NOT NULL DEFAULT 1 CHECK (event_version >= 1),
    dedup_key             TEXT NOT NULL,
    payload_json          TEXT NOT NULL,
    payload_hash          TEXT NOT NULL CHECK (length(payload_hash) = 64),
    status                TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN
                          ('PENDING','SENDING','SENT','FAILED','DISCARDED')),
    attempt_count         INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at       TEXT,
    claimed_at            TEXT,
    claim_token           TEXT,
    last_attempt_at       TEXT,
    sent_at               TEXT,
    last_error_code       TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    CHECK ((status = 'SENDING' AND claim_token IS NOT NULL
            AND claimed_at IS NOT NULL)
        OR (status <> 'SENDING' AND claim_token IS NULL)),
    CHECK (status <> 'SENT' OR sent_at IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_outbox_dedup
    ON notification_outbox(dedup_key);
CREATE INDEX IF NOT EXISTS idx_notification_outbox_due
    ON notification_outbox(status, next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS idx_notification_outbox_job
    ON notification_outbox(supervisor_job_id, created_at);
```

### 4.3 Spaltenregeln

`payload_json` enthält nur das validierte Template-Modell; `payload_hash` ist
dessen SHA-256; `event_ref` enthält nur interne technische Referenzen;
`claim_token` ist eine interne Claim-ID; `last_error_code` nur allowlist-
basiert. Es gibt keine Spalten für Bot-Credential, Zielkennung, URL, Header,
Response-Body, Usertext oder Inbound-Inhalt. Rohe Transportantworten werden
nicht persistiert.

### 4.4 Kein historisches Backfill

Die Migration erzeugt keine Rows für bereits terminale, fehlerhafte oder
präsentierte Jobs. Nur eine nach Upgrade tatsächlich ausgeführte Transition
erzeugt eine Row. Dies verhindert Upgrade-/Restart-Floods.

## 5. Payload-, Secret- und Template-Regeln

### 5.1 Payload-Modell

Zulässige Schlüssel: `template_version`, `notification_type`,
`supervisor_job_id`, `task_id`, `event_ref`, `event_at`, `reason_code`,
`gate_id`, `scope_ref`.

Verboten: Task-Titel/-Beschreibung, Agent-Output, Finding-/Handoff-Prosa,
roher Gate-Scope, Pfade, Patch-Inhalte, Testausgabe, rohe Fehler/Exceptions,
Transportdaten, Maildaten und Sourcecode.

Für Gates: `scope_ref = "sha256:" + binding_hash[:16]`. Der rohe Scope wird
weder gespeichert noch gesendet.

### 5.2 Feste Plaintext-Templates

Kein Markdown-/HTML-`parse_mode`, keine Links.

```text
ARGENT · DONE
Job: <supervisor_job_id>
Task: <task_id>
Time: <event_at>
Ref: <dedup_key-prefix>
```

```text
ARGENT · FAILED
Job: <supervisor_job_id>
Task: <task_id>
Reason: <allowlisted reason_code>
Time: <event_at>
Ref: <dedup_key-prefix>
```

```text
ARGENT · BLOCKED
Job: <supervisor_job_id>
Task: <task_id>
Reason: <allowlisted reason_code>
Time: <event_at>
Ref: <dedup_key-prefix>
```

```text
ARGENT · OWNER APPROVAL REQUIRED
Job: <supervisor_job_id>
Task: <task_id>
Gate: <gate_id>
Scope ref: <scope_ref>
Time: <event_at>
Ref: <dedup_key-prefix>
Informational only. Use the authenticated owner-control path.
```

### 5.3 Reason-Code-Allowlist (ausgehende Codes)

`TASK_DONE`, `TASK_FAILED`, `TASK_CANCELLED`, `MAX_ATTEMPTS`,
`PERSISTENT_ERROR`, `TASK_BLOCKED`, `GATE_REJECTED`, `SPAWN_UNRESOLVABLE`,
`AMBIGUOUS_WRITER`, `WAITING_GATE`. Interne Details werden vor Payload-
Erzeugung auf `PERSISTENT_ERROR` reduziert.

AMENDMENT 2a (umgesetzt): Die internen Plan-Reasons im Supervisor sind
LOWERCASE (z. B. `task_failed_cancelled`, `task_blocked`, `max_attempts`,
`spawn_unresolvable`, `ambiguous_writer`, `frontier_exhausted`, `adapter_*`,
`*_exhausted`, `workspace_diverged`, `lock_unavailable`, `write_result_hash_mismatch`).
Der Enqueue-Helper übersetzt ausschließlich über die folgende explizite Mapping-
Tabelle (kein freier Text, kein unerwarteter Code in Payload oder Nachricht):

| interner Reason | Notification | ausgehender Code |
|---|---|---|
| `task_done` | DONE | `TASK_DONE` |
| `task_failed_cancelled` + `tasks.state == FAILED` | FAILED | `TASK_FAILED` |
| `task_failed_cancelled` + `tasks.state == CANCELLED` | FAILED | `TASK_CANCELLED` |
| `max_attempts` | FAILED | `MAX_ATTEMPTS` |
| jeder sticky ERROR/PERSISTENT_ERROR (inkl. `frontier_exhausted`, `adapter_*`, `*_exhausted`, `workspace_diverged`, `lock_unavailable`, `write_result_hash_mismatch`, `args_hash_mismatch`, …) | FAILED | `PERSISTENT_ERROR` |
| `task_blocked` | BLOCKED | `TASK_BLOCKED` |
| `spawn_unresolvable` (BLOCKED-Close) | BLOCKED | `SPAWN_UNRESOLVABLE` |
| `ambiguous_writer` (BLOCKED-Close) | BLOCKED | `AMBIGUOUS_WRITER` |
| WAITING_GATE-Präsentation | OWNER_APPROVAL_REQUIRED | `WAITING_GATE` |

AMENDMENT 2b (umgesetzt): `task_failed_cancelled` kollabiert FAILED und
CANCELLED (supervisor.py ~1700, `if task.state in (TaskState.FAILED,
TaskState.CANCELLED)`). Der Enqueue-Helper liest in derselben Transaktion
`tasks.state` und unterscheidet so `TASK_FAILED` vs `TASK_CANCELLED` exakt.

AMENDMENT 2c (umgesetzt): Es gibt KEINEN internen `gate_rejected`-Plan-Reason.
Der ausgehende Code `GATE_REJECTED` wird abgeleitet: Bei `CLOSE_BLOCKED` mit
`task_blocked` prüft der Helper in derselben Transaktion `owner_approvals` auf
eine Zeile des Tasks mit `status='rejected'` (bzw. die zuletzt präsentierte
Gate-ID). Existiert eine rejected-Zeile → `GATE_REJECTED`, sonst `TASK_BLOCKED`.
`owner_approvals.status`-Werte sind lowercase ('pending'/'approved'/'rejected'/
'expired', konsistent mit Core; exakte Werte im Implementierungstest fixieren).

### 5.4 Logging

Logs dürfen nur Outbox-ID/gekürzte Dedup-Ref, Typ, Status, Attempt-Zahl,
allowlist-Fehlercode und Zeit enthalten. Nie Nachrichtentext, Payload, URL,
Header, Request/Response, Exception-Text, Transportkonfiguration, Credential
oder Zielkennung.

## 6. Idempotenz und Deduplizierung

Kanonisches JSON: ausschließlich der BESTEHENDE Helper
`argent_core/supervisor.py::_canonical_json` (identisch zu `Core._hash_args`),
verifiziert Zeile ~270:

```python
def _canonical_json(obj) -> str:
    """Canonical JSON used for hashes (identical to Core ``_hash_args``)."""
    return json.dumps(obj, sort_keys=True, default=str)
```

AMENDMENT 1 (umgesetzt): Phase 3A führt KEINEN neuen Canonical-JSON-Code ein und
keine abweichenden `separators`/`ensure_ascii`-Optionen. Alle Notification-Hashes
(`dedup_key`, `payload_hash`) rufen exakt diesen Helper auf. Dadurch sind Hashes
über Core-/Supervisor-/Store-Grenzen identisch, Restart-stabil und testbar gegen
die bestehende Hash-Praxis (z. B. `_sha256(_canonical_json([...]))`).

SHA-256 über UTF-8 als 64-stellige lowercase Hex-Darstellung.

Ereignisreferenzen:

- DONE: `supervisor:<job>:close:DONE`
- FAILED: `supervisor:<job>:close:FAILED`
- BLOCKED: `supervisor:<job>:close:BLOCKED`
- PERSISTENT_ERROR: `supervisor:<job>:persistent-error:v1`
- Gate: `supervisor:<job>:present-gate:<gate_id>`

```python
dedup_key = sha256(canonical_json([
    "argent-notification-v1",
    supervisor_job_id,
    notification_type,
    event_ref,
    event_version,
]))
```

Für OWNER_APPROVAL_REQUIRED:

```python
dedup_key = sha256(canonical_json([
    "argent-notification-v1",
    supervisor_job_id,
    "OWNER_APPROVAL_REQUIRED",
    gate_id,
    binding_hash,
    event_version,
]))
```

`id = "notification:" + dedup_key`.
`payload_hash = sha256(canonical_json(payload))`; vor jedem Send neu berechnen.
Abweichung: kein Send, `DISCARDED/PAYLOAD_HASH_MISMATCH`.

Genau eine Outbox-Zeile entsteht pro fachlicher Transition. Reconcile-/Restart-
Wiederholungen und dasselbe Gate erzeugen keine zweite. `SENT` wird nie erneut
gesendet. Dedup-Key und Payload-Hash gehen an den Transport.

End-to-end gilt bewusst at-least-once, nicht mathematisch exactly-once: Ein
Crash nach Telegram-Annahme vor lokalem `SENT` kann bei der rohen Bot API eine
identische zweite Nachricht erzeugen. Derselbe Ref-Wert macht sie erkennbar;
ein idempotenter Adapter/Relay unterdrückt sie; maximal fünf Attempts verhindern
einen Flood.

## 7. Trigger-Matrix

AMENDMENT 2d (umgesetzt): Die Matrix nennt pro Zeile den internen Plan-Reason
(lowercase, wie im Code) und den daraus abgeleiteten ausgehenden Code gemäß
§5.3-Tabelle. „Atomarer Insert-Ort“ = dieselbe SQLite-Transaktion wie die
autoritative Transition.

| Autoritative Transition (interner Reason) | Notification | Ausgehender Code | Atomarer Insert-Ort |
|---|---|---|---|
| `_close_job(..., DONE)` / `task_done` setzt erstmals DONE | `DONE` | `TASK_DONE` | CLOSE_JOB-Journal + Terminal-Update |
| Close FAILED bei Task FAILED (`task_failed_cancelled`, state==FAILED) | `FAILED` | `TASK_FAILED` | CLOSE_JOB-Transaktion |
| Close FAILED bei Task CANCELLED (`task_failed_cancelled`, state==CANCELLED) | `FAILED` | `TASK_CANCELLED` | CLOSE_JOB-Transaktion |
| Close FAILED nach max Attempts (`max_attempts`) | `FAILED` | `MAX_ATTEMPTS` | CLOSE_JOB-Transaktion |
| erster Wechsel zu ERROR/PERSISTENT_ERROR (alle sticky-ERROR-Pfade) | `FAILED` | `PERSISTENT_ERROR` | dieselbe sticky-ERROR-Transaktion |
| Close BLOCKED bei Core BLOCKED (`task_blocked`) | `BLOCKED` | `TASK_BLOCKED` oder `GATE_REJECTED` (siehe §5.3 2c) | CLOSE_JOB-Transaktion |
| Blocked-Close bei rejected Gate (aus `owner_approvals.status='rejected'` abgeleitet) | `BLOCKED` | `GATE_REJECTED` | CLOSE_JOB-Transaktion |
| `spawn_unresolvable` (BLOCKED-Close) | `BLOCKED` | `SPAWN_UNRESOLVABLE` | CLOSE_JOB-Transaktion |
| `ambiguous_writer` (BLOCKED-Close) | `BLOCKED` | `AMBIGUOUS_WRITER` | CLOSE_JOB-Transaktion |
| erster PRESENT_OWNER_GATE-Plan | `OWNER_APPROVAL_REQUIRED` | `WAITING_GATE` | `_commit` mit WAITING_GATE |

BLOCKED-Priorität: Gate rejected; spawn unresolvable; ambiguous writer; sonst
Task blocked.

Keine Notification für RUNNING, WAITING, BACKOFF, RECOVERING, noch retryfähige
Fehler, Gate approved/consumed/expired, Completion-Hints, Duplicate/Stale/
Quarantäne, Cache-Reparatur oder wiederholtes Reconcile eines terminalen bzw.
sticky-ERROR-Jobs.

AMENDMENT 3 (umgesetzt): Die bestehende Sticky-ERROR-Statuslogik
(`_persist_error`, `_perform_persistent_error`, Adapter-Backoff,
Apply-Lock-Fehler, `_commit`, ~14+ PERSISTENT_ERROR-Planstellen in
`argent_core/supervisor.py`) wird NICHT umgebaut. Phase 3A ergänzt ausschließlich
einen dedup-geschützten Notification-Enqueue-Helper: atomarer
`notification_outbox`-INSERT mit deterministischem `dedup_key` (UNIQUE-
Konflikt = No-op, keine Exception), aufgerufen an jeder Stelle, an der ein Job
ERSTMALS auf ERROR/PERSISTENT_ERROR gesetzt wird. Der Helper ändert keinen
Job-, Dispatch-, Action- oder Gate-Status und ist selbst nie Autorität.

## 8. Owner-Gate-Integration

1. Core-Ledger und Binding bleiben autoritativ.
2. `_decide_gate()` liefert wie bisher einmalig `PRESENT_OWNER_GATE`.
3. `_commit()` setzt WAITING_GATE und erzeugt atomar die Outbox-Zeile.
4. `_perform_present_owner_gate()` behält seine Prompt-Memory-/Journal-Logik.
5. Zustellung erfolgt unabhängig.
6. Telegram-Ausfall lässt das Gate pending.
7. Telegram-Erfolg genehmigt oder schließt nichts.
8. Restart lädt dieselbe Gate-ID/denselben Dedup-Key; keine neue Row.

Ein neues Gate mit neuer Gate-ID oder anderem Binding-Hash ist ein neues
Ereignis und erhält genau eine neue Notification.

## 9. Delivery-Algorithmus und Failure Modes

### 9.1 Konstanten

```python
NOTIFICATION_SEND_BATCH = 1
NOTIFICATION_MAX_ATTEMPTS = 5
NOTIFICATION_BACKOFF_INITIAL_SECONDS = 5
NOTIFICATION_BACKOFF_MULTIPLIER = 2
NOTIFICATION_BACKOFF_MAX_SECONDS = 300
NOTIFICATION_REQUEST_TIMEOUT_SECONDS = 5
NOTIFICATION_CLAIM_LEASE_SECONDS = 30
```

Backoff: `min(5 * 2 ** (attempt_count - 1), 300)`. Valides Retry-After wird auf
5..300 Sekunden begrenzt.

### 9.2 Claim

Unter `BEGIN IMMEDIATE`: älteste fällige Row auswählen (`PENDING`, fälliges
retryfähiges `FAILED` oder lease-expired `SENDING`), Status SENDING, neuen
Claim-Token, Zeitfelder und `attempt_count += 1` setzen, committen.

Abschluss-CAS:

```sql
UPDATE notification_outbox SET ...
WHERE id=? AND status='SENDING' AND claim_token=?;
```

### 9.3 Erfolg und Fehler

Erfolg: `SENT`, `sent_at=now`, kein next_attempt/claim/error.

Retryfähig (Timeout, Netz, Rate-Limit, 5xx): bei Attempts <5 `FAILED` mit
Backoff, sonst `DISCARDED/ATTEMPTS_EXHAUSTED`.

Nichtretryfähig (Payload-Integrität, fehlende Konfiguration, Auth-/Policy-
Fehler, definitiver 4xx): sofort `DISCARDED`. Temporärer Constructor-Fehler ist
retryfähig; fehlende Konfiguration nicht.

### 9.4 Exhaustive Failure Semantics

- **API down/Netz:** persistent, Retry/Backoff, Workflow läuft weiter.
- **Timeout/Hang:** nur einzelner Worker hängt; Supervisor nicht; höchstens eine
  Row SENDING; nach Restart/Lease recoverbar; realer Adapter mit harten Timeouts.
- **Rate-Limit:** `RATE_LIMITED`, begrenztes Retry-After, Batch 1.
- **Constructor-Fehler:** nur Worker; kein Startfehler für Core/Supervisor;
  kein Fehlertext persistiert.
- **SQLite locked:** Delivery-Verbindung `timeout=0`; sofortiger Abbruch, Row
  unverändert, kein Propagieren.
- **Crash Transition/Insert:** atomar; vor Commit beides weg, nach Commit beides da.
- **Crash nach Insert:** PENDING bleibt; kein neuer Eintrag.
- **Crash nach Claim:** SENDING bis Lease-Ablauf.
- **Crash nach Send vor SENT:** at-least-once Retry; identischer Payload; rohe
  Bot API kann Duplikat erzeugen; höchstens fünf Attempts.
- **Restart-Flood:** kein State-Scan/Backfill; SENT/DISCARDED nie; nur fällige
  Rows; Unique-Dedup, Batch 1 und Backoff.
- **Unbounded Retry:** verhindert durch Attempt-Maximum, Backoff, DISCARDED,
  einen Worker und Batch 1.
- **Concurrent Sender:** BEGIN IMMEDIATE, Claim-Token, CAS und Lease; stale
  Worker kann neuen Claim nicht überschreiben.
- **Payload-Manipulation:** kein Send, DISCARDED/PAYLOAD_HASH_MISMATCH.

## 10. Restart- und Recovery-Invarianten

1. Status stammt aus SQLite, nicht Memory.
2. SENT und DISCARDED sticky.
3. PENDING nicht dupliziert.
4. Terminale Jobs erzeugen beim Laden nichts.
5. Präsentierte Gates erzeugen beim Laden nichts.
6. Crash vor Close-Commit → vollständiger Close, eine Row.
7. Crash nach Close-Commit → genau eine Row vorhanden.
8. SENDING erst nach Lease-Ablauf reclaimbar.
9. Restarts erzeugen höchstens Attempts derselben Row, keine neuen Rows.
10. DONE-Stickiness unverändert.
11. Telegram öffnet keinen terminalen/sticky-ERROR-Job.

## 11. Testplan und Acceptance Tests

Alle Tests offline mit temporärer DB, Fake Clock und Mock-Transport; kein Netz,
keine Sleeps, keine echten Telegram-Anfragen und keine Agenten.

### 11.1 Schema/Migration

- frische DB, Tabelle/Indizes, Version 5;
- V4→V5 erhält V4-Daten;
- gemeinsame Transaktion und Rollback-Injection;
- CHECKs/Unique-Dedup;
- keine Secret-Spalten;
- kein historisches Backfill.

### 11.2 Trigger

Je ein Test für DONE, Task FAILED, CANCELLED, max Attempts,
PERSISTENT_ERROR, Task BLOCKED, Gate rejected, spawn unresolvable, ambiguous
writer und owner approval required. Negativtests für WAIT/RUNNING/BACKOFF,
retryfähigen Dispatch-Fehler und closed/approved Gates.

### 11.3 Atomizität/Idempotenz

- gemeinsamer Commit;
- Crash vor/nach Commit;
- gleiche Transition 2x/5x/20x → eine Row;
- terminales Reconcile wiederholt → eine Row;
- gleiches Gate → eine, neues Gate/Binding → neue;
- Hashes über Restart identisch.

### 11.4 Delivery Failure Matrix

API down, Connection-/Read-Timeout, Hang, Rate-Limit, 5xx,
Constructor-Fehler, fehlende Konfiguration, 4xx, Hash-Mismatch, SQLite locked,
Claim-CAS-Verlust, fünf Attempts→DISCARDED; Backoff 5/10/20/40/80, Cap 300.

### 11.5 Crash-Delivery

Crash nach Insert, Claim, Transportannahme vor SENT und nach SENT; Lease
abgelaufen/nicht; idempotenter Mock unterdrückt externes Duplikat;
nichtidempotenter Mock belegt at-least-once; stets eine Outbox-Row.

### 11.6 Restart-Flood

100 SENT→0 Sends; 100 DISCARDED→0; nichtfällige FAILED→0; mehrere fällige
Rows→maximal eine pro Kick; terminale Jobs→0 neue Rows; Restart nach SENT→0;
Upgrade historischer Jobs→0 Backfill-Sends.

### 11.7 Non-Blocking

Unbegrenzt blockierender Mock; Supervisor läuft bis DONE, FAILED, BLOCKED bzw.
WAITING_GATE. Workflow persistiert; nur ein Worker; kein Telegram-Zustand in
`next_action`, `recovery_state` oder Gate-Status.

### 11.8 Concurrent Sender

Zwei Delivery-Instanzen: genau ein Claim, höchstens ein normaler Send, stale
Completion verliert per CAS, Lease reclaimbar, kein verlorener Attempt.

### 11.9 No-Secrets

Canary-Daten in Task-Prosa, Gate-Scope, Agent-Result, Transport-Exception und
Remote-Fehler. Assert: nicht in Outbox, Nachricht oder Logs; Credential/
Zielkennung nicht SQLite; Rohscope nicht Payload; Text exakt festes Template.

### 11.10 Owner-Gate

Pending Gate → eine informative Row; Send ändert/approvt/schließt nichts;
Ausfall blockiert Gate nicht; Inbound verworfen; keine Owner-Source; neues Gate
separat; closed Gate nach Restart nicht erneut.

### 11.11 Bestandssuite/Smokes

- aktuelle 1012 Tests plus neue grün, keine Skips/Xfails;
- bwrap-Fixture 90 grün;
- Fake-Recovery-Smoke 22/22 oder dokumentierter neuer Zähler;
- kein echtes Telegram in pytest;
- keine Agenten in Phase-3A-Tests/Smokes.

### 11.12 Real-Smoke ohne Ausführung

`smoke/phase2c_recovery_real.py` wird nicht ausgeführt. Spätere owner-
autorisierte Vorbereitung: Recording-Mock in `_make_supervisor`; Phase 1
persistiert Outbox vor Tod; Phase 2 prüft gleiche IDs/Dedup-Keys, eine DONE-Row
und nach Restart keine neue Row/keinen zweiten Mock-Send. Bestehende Launch-/
Trajectory-/Exactly-once-Assertions bleiben. Statische Integration zulässig,
realer Harness nicht starten.

## 12. Implementierungs- und Abnahmegrenze

### 12.1 Kein Background-Wake

Zulässig sind nur ein prozesslokaler vom Loop angestoßener Worker und ein
begrenzter manueller `send-once`-Pass. Der Worker existiert nur während des
lokalen Prozesses, wacht nicht selbst auf, wird nicht durch systemd/cron/
Gateway/OpenClaw gestartet, versucht pro Kick höchstens eine Row und hält den
Supervisor nicht am Leben.

Eine spätere dauerhafte/periodische Installation ist eine neue externe Aktion
und benötigt ein neues exakt gescoptes Owner-Gate sowie separate Security-/
Config-Verifikation. Die Inbound-Sperre bleibt.

### 12.2 Implementierungsreihenfolge

1. Schema V5/Store-Queries;
2. Typen, canonical payload, Validator;
3. atomarer Enqueue-Helper;
4. DONE/FAILED/BLOCKED in `_close_job`;
5. gemeinsamer sticky-PERSISTENT_ERROR-Helper;
6. WAITING_GATE-Enqueue in `_commit`;
7. Mock-Transport;
8. Claim/Retry/Lease;
9. outbound-only Telegram-Adapter;
10. nichtblockierender Loop-Kick;
11. Schema/Trigger/Crash/Flood/Secret-Tests;
12. 1012 Bestandstests;
13. bwrap 90;
14. Fake-Smoke;
15. unabhängiger read-only Security-/Closing-Review;
16. lokaler Abschlusscommit erst nach Abnahme, kein Push.

### 12.3 Abnahmekriterien

Phase 3A ist nur abgeschlossen, wenn: genau vier Typen; outbound-only; kein
Inbound/Command/Exec; Owner-Gates allein autoritativ; Telegram-Ausfall
workflow-neutral; eine Row je Transition; Reconcile/Restart ohne neue Rows;
SENT nie erneut; Retry begrenzt und endet DISCARDED; Crashfenster dokumentiert
und getestet; keine sensitiven Inhalte in Ledger/Log/Text; kein Restart-Flood;
kein Background-Wake; keine Agenten; keine Mail-/Visualizer-/Gateway-/System-/
Config-Änderung; 1012+neue Tests grün; bwrap 90; Fake-Smoke grün; Working Tree
nach lokalem Abschlusscommit clean; kein Push/Promotion.

### 12.4 Offene Fragen und Annahmen

1. Sichere Runtime-Quelle für Bot-Credential/Zielkennung braucht Owner-Entscheid;
   ohne sie bleibt der Transport deaktiviert/fehlkonfiguriert und der Supervisor
   läuft weiter.
2. Kein realer Telegram-Send in Phase 3A; späterer Live-Test benötigt explizit
   autorisierte Zielbindung.
3. At-least-once-Crashfenster ist bewusst akzeptiert. Harte externe
   Exactly-once-Anforderung braucht idempotenten Relay/anderen Transport und
   neue Spezifikation.
4. Keine historischen Notifications vor Phase 3A.
5. Templates sind fest Englisch; andere Sprache darf keine freien Ledger-
   Inhalte einführen.
6. Kein dauerhafter Wake; spätere Installation nur mit neuem Owner-Gate.

---

# C. EXPLICIT ANSWERS

## 1. Ist das Design bei Telegram-Fehlern non-blocking?

Ja. Reconcile und sichere Workflow-Aktion committen vor einem O(1)-Kick; Netz
läuft isoliert. Timeout, Outage, Rate-Limit, Constructor-Fehler,
Ledger-Contention oder Hang ändern oder verzögern keinen Workflowzustand.

## 2. Idempotenz/Exactly-once inklusive Crashfenster?

Genau eine Outbox-Row pro Transition durch atomaren Insert und eindeutigen
deterministischen Dedup-Key. Reconcile/Restart erzeugen keine zweite;
bestätigtes SENT wird nie erneut gesendet. Externe Zustellung ist at-least-once:
Crash nach Telegram-Annahme vor SENT kann bei der rohen Bot API einen
identischen Retry erzeugen, maximal fünf. Ein idempotenter Adapter/Relay
unterdrückt ihn über `(dedup_key, payload_hash)`.

## 3. No-Secrets-Garantie?

Ja für den gesamten Phase-3A-Pfad. Credential/Zielkennung nur im Speicher, nie
SQLite/CLI/Logs. Payload nur interne IDs, Zeit, allowlist-Code und gehashte
Scope-Ref. Keine Task-Prosa, Agentdaten, Rohscope, Pfade, Exceptions oder
HTTP-Daten.

## 4. Restart-no-flood-Garantie?

Ja. Kein Backfill/State-Scan; SENT/DISCARDED sticky; nur fällige PENDING/FAILED/
lease-expired SENDING; Unique-Dedup, Batch 1, Backoff und fünf Attempts.

## 5. Welche SPEC-V2C-Invarianten bleiben unberührt?

- `tasks.state`/`workflow_frontier` bleiben alleinige Workflowautorität;
- keine zweite State Machine;
- unveränderte Core-/Runtime-Autorität;
- Reconcile ohne externe Side Effects;
- Action-Journal/CAS/Idempotenz;
- Dispatch-Provenance;
- Retry, Writer-Ambiguität und no-blind-respawn;
- Broker/bwrap;
- Owner-Gate-Binding und authentifizierte Owner-Source;
- DONE-Stickiness;
- Recovery/PERSISTENT_ERROR-Semantik;
- keine Mail-, Visualizer-, Gateway-, Rollen-/Toolprofil-, Background-Wake-,
  Push- oder Promotion-Änderung.
