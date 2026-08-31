# Argent Architecture Review V1

**Review-Basis:** Branch `phase-2c-persistent-supervisor`, HEAD `6e12928`, Phase 3D `VISUALIZER_GREEN`; relevante Specs V1–V3D, Kernmodule, Workspace-Memory und WSL-Ressourcen-Learnings wurden read-only geprüft. Der referenzierte Pfad `/home/pc/.openclaw/workspace/ARGENT_SUPERVISOR.md` existiert nicht; maßgeblich waren deshalb die eingebettete Argent-Policy in `AGENTS.md`, die Specs und der tatsächliche Code. Der vorhandene untracked Repo-Zustand wurde nicht verändert.

---

## 1. Executive Summary

Argent besitzt bereits ungewöhnlich starke Sicherheits- und Recovery-Grundlagen:

- ein autoritatives SQLite-Ledger;
- eine deterministische, fail-closed Core-State-Machine;
- persistente Supervisor-Projektion und crash-sicheres Action-Journal;
- harte Dispatch-/Run-Provenance;
- Owner-Gates mit exakter Binding-Semantik;
- Write-Broker, Rollenrechte und read-only-bwrap-Testgrenze;
- deduplizierte Telegram-Outbox;
- streng begrenzte Telegram-Approval-Architektur;
- einen vollständig read-only Visualizer.

Diese Bausteine sollen nicht ersetzt werden. Die nächste Architekturphase muss sie zu einem zuverlässigen, dauerhaft laufenden Job-System ergänzen.

Die wichtigsten heutigen Lücken sind:

1. **Job Queue und Supervisor-Projektion sind noch nicht dasselbe belastbare Betriebsmodell.** Es fehlt ein sauberer Queue-/Admission-/Claim-Layer für mehrere nacheinander liegende Jobs.
2. **Ownership und Leases sind auf einzelne Dispatch-/Action-Races verteilt**, aber noch nicht als explizite Job-, Writer- und Prozess-Leases modelliert.
3. **Liveness ist zu grob.** Agent lebt, OS-Prozess lebt, Job macht Fortschritt und legitimer externer Wait werden nicht getrennt.
4. **Externe Wartezustände halten potenziell teure Agenten unnötig aktiv.**
5. **Es gibt keinen Resource Governor.** Ein technisch korrekt gestarteter Job kann die gesamte WSL-Instanz destabilisieren.
6. **Rollen- und Modellrouting sind statisch.** Der Standardpfad aktiviert immer viele Rollen und reserviert Sol für Lead/Reviewer auch dann, wenn Flash oder Pro ausreichen.
7. **Context Engineering ist sicher, aber noch nicht als budgetierter, versionierter Artifact-Handoff-Layer ausgeprägt.**
8. **Worktree-Ownership ist nicht vollständig im Ledger verankert.**
9. **Der Supervisor ist restart-fähig, aber noch kein unabhängig von TUI/Session dauerhaft betriebener Dienst.**

Empfohlene Zielrichtung:

- **ein einziger langlebiger Supervisor-Dienst** als Scheduler, Reconciler und Policy-Enforcer;
- **SQLite bleibt die einzige lokale Autorität**; keine Redis-/Kafka-/Distributed-System-Erweiterung;
- **ein Primary Job State plus orthogonale Substates/Felder**, keine zweite Workflow-State-Machine;
- **1 Job = 1 eindeutiger Worktree = maximal 1 Writer-Lease**;
- **hierarchische Leases** für Job, Agent-Run und Writer;
- **Resource Governor vor Spawn und vor jedem schweren Schritt**;
- **externe Waits als persistierte Subscription/Deadline**, ohne aktiven LLM-Agent;
- **adaptive Rollenpläne** statt obligatorischem 5-Rollen-Durchlauf;
- **Flash-first, Pro bei Implementierungs-/Debugging-Komplexität, Sol nur für Architektur, Security, harte Root Causes und unabhängige Closing Reviews**;
- **immutable Context Packs und Artifact-Handoffs** statt History-Dumping;
- **targeted → relevanter CI-Shard → Closing Regression**;
- breite Parallelisierung erst nach belegter stundenlanger Einzeljob-Stabilität.

---

## 2. KEEP / CHANGE / SIMPLIFY / REMOVE / ADD

### KEEP

| Baustein | Klassifikation | Begründung |
|---|---|---|
| Core V1 und `TaskState`-Workflow | KEEP | Autoritative fachliche State Machine, deterministisch, fail-closed und umfangreich gehärtet. |
| SQLite-Ledger mit `BEGIN IMMEDIATE`, CAS und Unique Constraints | KEEP | Für ein Home-/Development-System ausreichend, lokal, auditierbar und restart-fähig. |
| Supervisor-Reconciliation | KEEP | Richtiger Ansatz: persisted facts gewinnen gegen Cache, Events und Agent-Prosa. |
| `supervisor_actions` Action-Journal | KEEP | Zentrale Crash-/Exactly-once-Grundlage für innere Effekte. |
| Dispatch-/Session-/Run-Provenance | KEEP | Verhindert stale/foreign Results und Ghost-Writer-Effekte. |
| fünf Rollen als Capability Set | KEEP | Gute Trennung von Analyse, Schreiben, QA und unabhängiger Prüfung. |
| WorkspaceBroker | KEEP | Einziger kontrollierter Produkt-Write-Pfad; Scope-, Path-, Symlink- und Content-Härtung ist notwendig. |
| bwrap-Test-Runner | KEEP | Read-only Workspace und isolierte Testausführung bleiben verbindlich. |
| Trust-, Normalization-, Privacy- und Output-Validation | KEEP | Agent- und externe Inhalte bleiben Daten, nie Autorität. |
| Approval Core und Telegram Approval Adapter | KEEP | Exakte Gate-Bindung und atomare Challenge-/Approval-Semantik sind sicher. |
| Telegram Notification Outbox | KEEP | Non-blocking, bounded, dedupliziert und fachlich nichtautoritativ. |
| Read-only Visualizer Snapshot | KEEP | Beobachtbarkeit ohne Control-Pfad oder direkten Ledger-Zugriff. |
| DONE-Stickiness | KEEP | Unverzichtbare Terminalinvariante. |
| persistente Gate-/Supervisor-Zustände | KEEP | Neustart- und Crash-Konsistenz hängen davon ab. |

### CHANGE

| Baustein | Klassifikation | Begründung |
|---|---|---|
| Supervisor-Jobmodell | CHANGE | Um Queue, Claim, Lease, Wait-Subscription, Error Class, Resource Class und Worktree-Provenance erweitern. |
| Liveness | CHANGE | Vier getrennte Signale: Supervisor/Agent-Liveness, Prozess-Liveness, fachlicher Fortschritt, legitimer Wait. |
| Retry-Modell | CHANGE | Nicht nur Zähler, sondern klassifizierte Fehler mit separater Policy pro Fehlerklasse. |
| Rollenaktivierung | CHANGE | Rollen bleiben erhalten, werden aber pro Job als minimaler Plan aktiviert. |
| Modellrouting | CHANGE | Von role-fixed zu task-/step-/risk-/evidence-basiert; Flash-first. |
| Context-Snapshots | CHANGE | Zu versionierten Context Packs mit Budget, Artifact Refs, Invalidation und Handoff entwickeln. |
| Testauswahl | CHANGE | Relevanten Ziel-CI-Kontext als Pflichtmetadatum behandeln. |
| SupervisorLoop | CHANGE | Vom einzelnen `run_until_terminal(job_id)` zu einem langlebigen Scheduler mit bounded passes. |
| Workspaces | CHANGE | Worktree-ID, Basis-Commit, Writer-Lease und Dirty-/HEAD-Fakten im Ledger binden. |
| Backoff | CHANGE | Jitter für externe/mehrere Jobs ergänzen; deterministische Fake-Clock-Tests behalten. |
| Visualizer-Snapshot | CHANGE | Neue Job-/Lease-/Resource-/Wait-Felder nur allowlist-basiert ergänzen. |

### SIMPLIFY

| Baustein | Klassifikation | Begründung |
|---|---|---|
| Öffentliche Jobzustände | SIMPLIFY | Keine parallelen Zustände `WAITING`, `WAITING_FOR_EXTERNAL` und `WAITING_FOR_CI`; ein `WAITING_EXTERNAL` plus `wait_kind`. |
| Supervisor-Status vs. Recovery-State | SIMPLIFY | Primary State, Recovery Phase, Wait Kind und Error Class getrennt speichern; keine kombinatorische Enum-Explosion. |
| Standardrollenfolge | SIMPLIFY | Nicht jede Änderung benötigt Analyst, QA und Reviewer als separate Agent-Runs. |
| Notifications | SIMPLIFY | Benutzerseitig nur `GATE`, `ERROR`, `DONE`; BLOCKED/FAILED intern unter ERROR-Template differenzieren. |
| Telemetrie | SIMPLIFY | Ledger + read-only Snapshot; keine Metrics-Plattform, Tracing-Cluster oder Event-Bus. |
| Parallelisierung | SIMPLIFY | Initial global maximal ein Writer und wenige read-only/light Jobs. |

### REMOVE

| Baustein | Klassifikation | Begründung |
|---|---|---|
| Teurer aktiver Agent während externer Wartezeit | REMOVE | Agent wird nach Persistierung der Wait-Subscription freigegeben. |
| Generischer langlebiger `WAITING`-State | REMOVE | Ohne Wait-Grund nicht operationalisierbar; interne kurze Sleeps gehören in `next_wake_at`. |
| `WAITING_FOR_CI` als eigener Primary State | REMOVE | CI ist ein `wait_kind` von `WAITING_EXTERNAL`, sonst entstehen Zustandsduplikate. |
| Pflicht-Sol für jeden Lead-/Reviewer-Step | REMOVE | Kostspielig und für Routinekoordination nicht gerechtfertigt. |
| vollständiges History-Dumping in Agentenkontexte | REMOVE | Erhöht Kosten, Prompt-Injection-Fläche und Stale-Context-Risiko. |
| große Repos oder Package-Stores in `/tmp` | REMOVE | tmpfs ist zu klein und konkurriert direkt mit RAM. |
| blinde Retry-Schleifen nach Resource Failure | REMOVE | Resource Failure ist kein Code Failure. |
| unbekannte Writer-/Fixer-Sessions im gleichen Worktree | REMOVE | Verletzt Writer-Provenance und Recovery-Eindeutigkeit. |

### ADD

| Baustein | Klassifikation | Begründung |
|---|---|---|
| Durable Job Queue | ADD | Persistente Admission, Priorität, Claim und Scheduling. |
| Job-/Writer-Leases | ADD | Sichere eindeutige Ownership und Crash-Übernahme. |
| Resource Governor | ADD | Verhindert WSL-weite Memory-/Swap-/tmpfs-Stalls. |
| External Wait Registry | ADD | Event-/Deadline-basierte Reaktivierung ohne aktiven Agent. |
| Process Registry | ADD | PID, Startzeit, cgroup/scope, Exit-Fakten und Reboot-Erkennung. |
| Worktree Registry | ADD | 1:1-Bindung zwischen Job, Worktree, Base Commit und Writer. |
| Context Pack Registry | ADD | Budgetierte, immutable, hash-gebundene Rollenübergaben. |
| Test Plan Artifact | ADD | Ziel-Shard, lokale Tests, CI-Checks und Closing Gate explizit. |
| Error Taxonomy | ADD | `TRANSIENT`, `DETERMINISTIC`, `RESOURCE`, `EXTERNAL`, `SECURITY`, `OWNER_REQUIRED`. |
| Resource Classes | ADD | `LIGHT`, `MEDIUM`, `HEAVY`, `EXCLUSIVE`. |
| Background Service Host | ADD | Supervisor unabhängig von TUI, Telegram und Visualizer. |
| Controlled Merge Queue | ADD | Erst später, seriell und owner-/policy-konform. |
| Self-Improvement Proposal Pipeline | ADD | Erst nach stabiler V1 und immer Owner-gated. |

---

## 3. Wichtigste Architekturprobleme heute

### 3.1 Überlagerte Zustandswelten

Argent hat bereits:

- fachliche `TaskState`;
- `SupervisorJobStatus`;
- `RecoveryState`;
- `DispatchStatus`;
- `RunStatus`;
- `supervisor_actions.status`;
- Gate-/Notification-/Challenge-Status.

Diese sind jeweils sinnvoll, aber es fehlt eine explizite Schichtung. Ein zusätzlicher Durable-Queue-State darf **nicht** zur dritten Workflow-State-Machine werden.

### 3.2 Keine explizite Job-Lease

Der partielle Unique-Index verhindert mehrere aktive Supervisor-Jobs pro Task; Action- und Dispatch-CAS verhindern viele Doppelwirkungen. Dennoch fehlt eine persistierte Aussage:

> Supervisor-Instanz X besitzt Job Y bis Zeitpunkt Z mit Epoch N.

Ohne Epoch/Fencing kann ein nach Partition/Stall zurückkehrender alter Worker neue Entscheidungen eines Übernehmers überschreiben.

### 3.3 Liveness und Progress sind vermischt

`last_progress_at`, Runtime-Beobachtung und Polling reichen nicht zur Unterscheidung:

- Supervisor lebt, Agent lebt, aber Testprozess hängt;
- Agent-Prozess lebt und arbeitet legitim 30 Minuten ohne Output;
- Agent ist beendet, Child-Prozess lebt weiter;
- Job wartet korrekt auf GitHub CI;
- Agent sendet Heartbeats, produziert aber keinen fachlichen Fortschritt;
- WSL ist unter Memory Pressure und sämtliche Heartbeats verzögern sich.

### 3.4 Externe Waits sind kein eigener Lifecycle

Ein `WAIT` mit kurzem `next_wake_at` ist passend für interne Reconciliation, nicht für minuten- oder tagelange CI-/Upstream-Wartezeiten. Polling durch einen teuren aktiven Agent ist ein Kosten- und Reliability-Fehler.

### 3.5 Keine Admission Control für Ressourcen

Der bwrap-Runner schützt Dateisystemgrenzen, aber nicht den Host vor:

- RAM-/Swap-Erschöpfung;
- tmpfs-Konkurrenz;
- verwaisten Child-Prozessen;
- CPU-Sättigung;
- langen Typechecks ohne Fortschrittsgrenze.

### 3.6 Starres Rollen-/Modellmodell

Der heutige kanonische Pfad ist sicher, aber teuer:

- Lead und Reviewer sind immer Sol High;
- Analyst ist immer Pro;
- Standardsequenz aktiviert alle Rollen;
- Aufgabenkomplexität, Diff-Größe und Security-Relevanz beeinflussen den Rollenplan nur indirekt.

### 3.7 Context-Handoff ist noch kein vollständiges Betriebsartefakt

Die Context-Isolation verhindert bereits viele Leaks. Es fehlen jedoch:

- explizites Token-/Byte-Budget;
- `base_commit`/`diff_hash`/`facts_version` als Invalidation-Key;
- Artifact References statt erneutem Text;
- Zusammenfassungs-Checkpoints;
- Kennzeichnung von confirmed vs. unverified facts;
- gezielte Retrieval-Policy.

### 3.8 Worktree-Provenance ist außerhalb des Jobledgers

Der Implementer-Dispatch ist gebunden, nicht aber vollständig:

- Worktree-Pfad/ID;
- Base-/Current-HEAD;
- Writer-Lease;
- erlaubte Branch-ID;
- Dirty-State;
- Besitzerwechsel;
- Abandonment-Status.

### 3.9 Kein dauerhafter, UI-unabhängiger Host

TUI, Telegram und Visualizer dürfen nicht den Supervisor-Lifecycle besitzen. Momentan ist `SupervisorLoop` restart-proof, aber ausdrücklich keine Background-Installation.

---

## 4. Zielarchitektur

### A. Komponenten

```text
                         Owner / Authenticated Control
                                   |
                         +---------v----------+
                         |   Owner Gate Core  |
                         +---------+----------+
                                   |
+-------------+   read-only   +----v-----------------------------+
| TUI         |-------------->|                                  |
| Telegram UI |<-- outbox ----|      Durable Supervisor Service  |
| Visualizer  |<-- snapshot --|                                  |
+-------------+               +----+--------+---------+----------+
                                   |        |         |
                         +---------v--+  +--v------+  +v----------------+
                         | Job Queue  |  |Resource |  |External Wait    |
                         | + Leases   |  |Governor |  |Registry/Watcher |
                         +-----+------+  +----+----+  +--------+--------+
                               |              |                |
                         +-----v--------------v----------------v------+
                         | Reconciler + Action Journal + Process Reg. |
                         +-----+------------------+-------------------+
                               |                  |
                    +----------v------+    +------v----------------+
                    | Core/Workflow   |    | Runtime/Agent Adapter |
                    | Dispatch/Gates  |    | Process/cgroup facts  |
                    +----------+------+    +------+----------------+
                               |                  |
                    +----------v------------------v----+
                    | Worktree Registry / Write Broker |
                    | bwrap Tests / Git Evidence       |
                    +----------------------------------+

                         SQLite = einzige Autorität
```

### B. Verantwortlichkeiten

**Core**

- fachlicher Task-State;
- Rollenrechte;
- Dispatch-Provenance;
- Output-Validierung;
- Owner-Gates;
- Findings, Decisions, Tests und Reviews.

**Durable Supervisor**

- Job Admission und Scheduling;
- Lease Claim/Renewal/Fencing;
- Reconciliation;
- adaptive Role-/Model-Planung;
- sichere Action-Ausführung;
- Recovery-Orchestrierung;
- Terminalentscheidung.

**Resource Governor**

- Preflight;
- Ressourcenklasse;
- cgroup-/systemd-scope-Limits;
- Admission/Deferral;
- Resource-Failure-Klassifikation.

**External Wait Manager**

- persistente Wait-Subscription;
- billiger bounded Check;
- Deadline;
- Event Dedup;
- Reaktivierung.

**Worktree Manager**

- Erstellung;
- exklusive Jobbindung;
- Writer-Lease;
- Git-/Dirty-/HEAD-Reconciliation;
- Cleanup-Vorschlag.

**Context Router**

- Rollenbezogene Context Packs;
- Budget;
- Retrieval;
- Artifact-Handoffs;
- Invalidation.

**Interfaces**

- TUI: Control/Status, aber kein Prozess-Owner;
- Telegram: GATE/ERROR/DONE und strikt begrenzte Approval-Callbacks;
- Visualizer: ausschließlich read-only Snapshot.

### C. Persistenz

Bestehende Tabellen bleiben. Additiv empfohlen:

1. `job_queue`
   - `job_id`, `task_id`, `primary_state`;
   - `priority`, `resource_class`;
   - `owner_instance_id`, `lease_epoch`, `lease_expires_at`;
   - `next_eligible_at`, `attempt_no`;
   - `wait_kind`, `wait_ref`, `wait_deadline_at`;
   - `error_class`, `error_code`;
   - `role_plan_version`, `context_checkpoint_id`;
   - `worktree_id`;
   - Zeitstempel und Terminalfelder.

2. `job_leases`
   - optional separates Auditlog oder aktuelle Lease direkt in `job_queue`;
   - `lease_epoch` muss als Fencing Token in mutierenden Supervisor-Aktionen geprüft werden.

3. `process_registry`
   - `process_id`, `job_id`, `dispatch_id`, `pid`;
   - `boot_id`, `process_start_ticks`;
   - `cgroup_ref`, `resource_class`;
   - `status`, `exit_code`, `oom_observed`, Zeitstempel.

4. `worktree_registry`
   - `worktree_id`, `job_id`, `repo_identity`;
   - `canonical_path`, `branch_name`;
   - `base_commit`, `expected_head`;
   - `writer_dispatch_id`, `writer_lease_epoch`;
   - `state=ACTIVE|ABANDONED|CLEANUP_PENDING|RELEASED`.

5. `external_waits`
   - `wait_id`, `job_id`, `kind`;
   - allowlisted Provider/Repo/Run-/Check-Referenz;
   - `last_observed_state`, `next_check_at`, `deadline_at`;
   - `check_attempt`, `event_version`, `terminal_observed_at`.

6. `context_packs`
   - Manifest und Hash, keine Secrets;
   - `job_id`, `dispatch_id`, `role`, `facts_version`;
   - `base_commit`, `diff_hash`, `policy_version`;
   - `budget`, `artifact_refs`, `created_at`, `invalidated_at`.

7. `resource_samples`
   - nur bounded/latest oder aggregierte Samples;
   - keine hochfrequente Enterprise-Telemetrie.

Alle Schemaänderungen transaktional, additiv und mit Migration-/Rollback-Crash-Tests.

### D. State Machine

Die Job-State-Machine ist eine Betriebsprojektion, **keine zweite fachliche Task-State-Machine**.

Empfohlene Primary States:

```text
QUEUED
RUNNING
WAITING_EXTERNAL
RETRYING
OWNER_GATE
BLOCKED
FAILED
LOST
DONE
```

Orthogonale Felder:

```text
wait_kind = CI | UPSTREAM | RATE_LIMIT | NETWORK | TIMER | NONE
recovery_phase = NONE | DISCOVERING | REBINDING | RECONCILING_WORKTREE | ...
error_class = TRANSIENT | DETERMINISTIC | RESOURCE | EXTERNAL | SECURITY |
              OWNER_REQUIRED | NONE
```

### E. Job Lifecycle

```text
create/admit
    |
    v
 QUEUED --claim+preflight--> RUNNING
    |                           |
    |                           +--> RETRYING --deadline--> QUEUED
    |                           |
    |                           +--> WAITING_EXTERNAL --event/deadline--> QUEUED
    |                           |
    |                           +--> OWNER_GATE --approve/execute--> QUEUED
    |                           |       |
    |                           |       +--reject/expire-policy--> BLOCKED
    |                           |
    |                           +--> LOST --reconcile--> QUEUED|BLOCKED|FAILED
    |                           |
    |                           +--> BLOCKED
    |                           +--> FAILED
    |                           +--> DONE
    |
    +--resource unavailable--> QUEUED(next_eligible_at)
```

Wichtig: Nach Wait-/Retry-Ende geht der Job zunächst nach `QUEUED`, nicht direkt zu `RUNNING`. Dadurch werden Lease und Resource Admission erneut geprüft.

### F. Recovery Lifecycle

```text
service start
  -> acquire singleton scheduler lease
  -> read boot_id + ledger
  -> expire stale job leases
  -> inspect process registry
  -> inspect Git/worktrees
  -> inspect dispatch/runtime facts
  -> inspect waits/gates/actions
  -> classify each nonterminal job
  -> repair only provable effects
  -> QUEUED / WAITING_EXTERNAL / OWNER_GATE / LOST / BLOCKED
  -> resume scheduler
```

Kein Agent wird während globaler Reconciliation gestartet.

### G. Resource Lifecycle

```text
classify step
  -> host preflight
  -> admission decision
  -> create bounded cgroup/scope
  -> register process identity
  -> execute
  -> sample bounded health/progress
  -> classify exit
       success
       code failure
       resource limit
       external failure
       timeout/stall
  -> cleanup verified process scope
```

### H. Agent-/Role Routing

Rollen sind Fähigkeiten, nicht Pflichtstationen. Der Supervisor persistiert vor dem ersten Dispatch einen `role_plan_version` mit Begründungscodes. Änderungen am Plan erfordern neue Fakten und ein neues Plan-Artefakt.

### I. Model Routing

Modellwahl wird aus vier Dimensionen abgeleitet:

- Step Capability;
- Task Complexity;
- Risk/Security;
- Evidence of Failure.

Die billigste ausreichende Stufe gewinnt. Eskalation ist ein persistiertes Ereignis mit Grund.

### J. Context Routing

```text
Ledger facts + policy + scoped repo evidence
             |
        Context Router
             |
    immutable Context Pack
     /         |          \
 rules     artifact refs   tests/evidence
             |
          Agent
             |
 structured output + artifact handoff
             |
 validation -> ledger -> next Context Pack
```

### K. Git/Worktree Lifecycle

```text
job accepted
 -> reserve unique worktree identity
 -> create from persisted base_commit
 -> verify canonical path/repo
 -> acquire writer lease
 -> writer applies only via broker
 -> tests/review
 -> local commit according to policy
 -> release writer
 -> merge queue or owner gate
 -> cleanup only after terminal + verification
```

### L. External Wait Lifecycle

```text
RUNNING
  -> persist external_wait + release agent + release compute allocation
  -> WAITING_EXTERNAL
  -> cheap watcher/event callback
  -> validate external observation as UNTRUSTED DATA
  -> translate only through allowlisted adapter
  -> persist observed status
  -> QUEUED for trusted reconciliation
```

Ein CI-Event darf den Job wecken, aber niemals selbst Code schreiben, Approval erteilen oder DONE setzen.

### M. Owner Gate Lifecycle

Bestehendes Modell bleibt:

```text
trusted local action request
 -> exact binding hash
 -> pending gate + optional Telegram challenge/outbox
 -> OWNER_GATE
 -> authenticated callback validation
 -> approval/reject committed atomically
 -> approval is permission, not execution
 -> registered local handler executes exact binding
 -> consumed/closed
 -> QUEUED
```

### N. Observability

Minimaler Snapshot pro Job:

- Job-ID, Task-ID gekürzt;
- Primary State;
- fachlicher Task-State;
- aktueller Step/Rolle;
- Lease Owner/Epoch/Expiry ohne sensitive Prozessdetails;
- Agent-/Process-Liveness;
- letzter Progress;
- Retry Count/Deadline;
- Resource Class und letzter Resource Outcome;
- Modell und grobe Token-/Kostenwerte;
- Wait Kind/Deadline;
- Error Class/allowlisted Error Code;
- Worktree-Status;
- Zeitstempel.

Keine Prompts, Diffs, Rohpfade, Ergebnisse, Tokens, Chat-IDs, binding hashes oder Prozess-Commands.

---

## 5. State Machine: finale Bewertung der 11 vorgeschlagenen Zustände

| Vorgeschlagener Zustand | Bewertung | Finale Behandlung |
|---|---|---|
| `QUEUED` | nötig | Primary State. Job wartet auf Admission/Lease. |
| `RUNNING` | nötig | Primary State. Job besitzt Lease und hat einen ausführbaren aktiven Step. |
| `WAITING` | zu unspezifisch | Entfernen. Kurzes internes Warten über `next_wake_at`; externes Warten über `WAITING_EXTERNAL`. |
| `WAITING_FOR_EXTERNAL` | nötig | Primary State, finaler Name `WAITING_EXTERNAL`. |
| `WAITING_FOR_CI` | fachlich nützlich, aber kein eigener Primary State | `WAITING_EXTERNAL + wait_kind=CI`. |
| `RETRYING` | nötig | Primary State für klassifizierten, zeitlich geplanten Retry. |
| `OWNER_GATE` | nötig | Primary State; technisch auf bestehendem Gate-Ledger aufgebaut. |
| `BLOCKED` | nötig | Terminal/semi-terminal: kein autonom sicherer Weg; Owner kann später neuen Recovery-Auftrag geben. |
| `FAILED` | nötig | Terminal für deterministisch ausgeschöpfte oder nicht reparierbare technische/codebezogene Fehler. |
| `LOST` | nötig, aber eng verwenden | Persistierter Recovery-Quarantänezustand bei unauflösbarer Ownership/Prozess-/Writer-Evidenz. Nie automatisch als Codefehler behandeln. |
| `DONE` | nötig | Terminal und sticky. |

### Übergänge und Begründung

- `QUEUED → RUNNING`: nur atomarer Lease-Claim plus erfolgreicher Resource-/Worktree-Preflight.
- `QUEUED → BLOCKED`: invalide Policy, unauflösbarer Worktree-Konflikt oder fehlende zwingende Owner-Entscheidung.
- `RUNNING → WAITING_EXTERNAL`: nur nachdem Wait-Referenz, Checker-Policy und Deadline committed sind.
- `RUNNING → RETRYING`: nur klassifizierter retryfähiger Fehler.
- `RUNNING → OWNER_GATE`: bestehendes Gate muss im selben autoritativen Commit existieren.
- `RUNNING → LOST`: Lease/Runtime/Writer-Fakten widersprechen sich und sichere Fortsetzung ist nicht beweisbar.
- `RUNNING → FAILED`: deterministischer Fehler oder ausgeschöpfte erlaubte Attempts.
- `RUNNING → DONE`: nur wenn Core-Task-DONE bzw. fachliche Closing-Invarianten bewiesen sind.
- `WAITING_EXTERNAL → QUEUED`: externes Event oder Deadline; niemals direkt Agent starten.
- `RETRYING → QUEUED`: Retry-Zeit erreicht; erneute Admission.
- `OWNER_GATE → QUEUED`: Gate genehmigt und exakte Ausführung/Closure erlaubt Fortsetzung.
- `OWNER_GATE → BLOCKED`: Reject, terminale Expiry-Policy oder Binding-Konflikt.
- `LOST → QUEUED`: Reconciliation beweist eindeutigen sicheren Zustand.
- `LOST → BLOCKED|FAILED`: Ambiguität bleibt oder Recovery-Beleg zeigt Terminalfehler.
- `BLOCKED|FAILED → RUNNING`: verboten. Recovery beginnt als neuer, owner-/policy-autorisierter Übergang über `QUEUED`.
- `DONE → *`: immer verboten.

`BLOCKED`, `FAILED` und `DONE` sollen wie bisher terminale Supervisor-Projektionen sein. `LOST` ist nicht „automatisch retrybar“.

---

## 6. Recovery-Modell

### 6.1 Autoritätsreihenfolge

1. Core-Ledger;
2. Supervisor-/Queue-Ledger;
3. Action-Journal;
4. Dispatch-/Run-Bindungen;
5. Prozessidentität aus `boot_id + pid + start_ticks + cgroup`;
6. Worktree-/Git-Fakten;
7. allowlisted externe Provider-Fakten;
8. Events und Agent-Prosa nur als nichtautoritative Hinweise.

### 6.2 Recovery nach Fehlerart

**Agent-Crash**

- Prozess terminal belegt;
- Dispatch markieren;
- Writer-Worktree unverändert sichern;
- Output nur bei vollständig gebundener, validierter Completion konsumieren;
- Retry nach Fehlerklasse und Attempts.

**Supervisor-/Gateway-Restart**

- laufende Agent-/Testprozesse nicht blind beenden;
- Job-Leases neu bewerten;
- Dispatch/Action-Journal rebind;
- legitime Waits bleiben Waits;
- keine Notifications oder Gates duplizieren.

**WSL-Restart/PC-Reboot**

- `boot_id` ändert sich: alle alten Prozessregistrierungen sind sicher nicht mehr lebend;
- Leases verfallen;
- Worktrees und SQLite bleiben;
- implementierende Writer werden zunächst `LOST`, bis Git-/Action-Journal-Reconciliation eindeutig ist;
- externe Waits bleiben erhalten;
- Jobs werden erst nach globaler Reconciliation admitted.

### 6.3 Fencing

Jeder mutierende Supervisor-Commit prüft:

```text
job_id
owner_instance_id
lease_epoch
lease_expires_at > now
facts_version
```

Ein alter Supervisor kann nach Lease-Übernahme keine Wirkung mehr committen.

Externe Prozesse erhalten keine direkte Ledger-Schreibautorität. Ihre Resultate bleiben untrusted und werden durch die existierende Provenance-Grenze konsumiert.

### 6.4 Stall-Erkennung

Getrennte Zeitwerte:

- `supervisor_heartbeat_at`;
- `agent_heartbeat_at`;
- `process_observed_at`;
- `last_progress_at`;
- `external_wait_observed_at`.

Ein Stall liegt erst vor, wenn:

- Job `RUNNING`;
- kein deklarierter External Wait;
- kein erlaubter Long-Running-Step;
- Prozess-/Agent-Signale fehlen oder kein Progress innerhalb step-spezifischer Frist;
- Resource Governor meldet nicht nur temporäre Host-Pressure;
- mindestens zwei voneinander unabhängige Beobachtungen den Befund bestätigen.

### 6.5 Writer-Recovery

Für Writer gilt strenger:

- unbekannter Writerstatus → `LOST`, kein zweiter Implementer;
- Recovery prüft Worktree HEAD, Dirty Hash, Journal Pre-/Effect-Hash, Prozessscope und Dispatch;
- nur wenn der alte Writer technisch terminal und der Worktree konsistent ist, darf ein neuer Writer-Dispatch entstehen;
- bei divergiertem Worktree → `BLOCKED/WORKTREE_DIVERGED`.

---

## 7. Resource-Governor-Modell

### 7.1 Klassen

| Klasse | Typische Schritte | Parallelität |
|---|---|---|
| `LIGHT` | Status, kleine Analyse, kleine targeted Tests, Git-Metadaten | mehrere möglich, initial max. 2 |
| `MEDIUM` | normale Implementierung, begrenzte Test-Shards, moderate Builds | initial max. 1 |
| `HEAVY` | große Typechecks, Monorepo-Build, größere Regression | max. 1, keine anderen MEDIUM/HEAVY |
| `EXCLUSIVE` | bekannte memory-intensive Tools, Full Regression lokal | gesamter Host exklusiv; bevorzugt CI statt lokal |

Initiale Gesamtregel: **maximal ein Writer-Job**, unabhängig von Resource Class.

### 7.2 Preflight

Vor Spawn und vor jedem schweren Substep:

- `MemAvailable`;
- Swap total/used;
- Root-/Workspace-Disk free;
- `/tmp` Typ und free space;
- CPU Load und laufende Argent-cgroups;
- existierende schwere Fremdprozesse;
- geschätzter Workspace-/Package-Store-Bedarf.

Admission wird verweigert oder verschoben, wenn:

- Hostreserve von mindestens `max(1.5 GiB, 20 % RAM)` nicht erhalten werden kann;
- Swap bereits ≥70 % belegt ist; ab ≥85 % keine MEDIUM/HEAVY-Jobs;
- Workspace-Disk weniger als 10 GiB oder 15 % frei hat, sofern projektspezifisch nicht kleiner bewiesen;
- benötigte temporäre Daten nicht mindestens mit Faktor 2 in persistentem Storage Platz haben;
- `/tmp` tmpfs ist und ein Job dort Repo, `node_modules`, Package Store oder große Artefakte ablegen würde;
- Load/Memory Pressure bereits kritisch ist.

Diese Werte sind konservative Startwerte für 7.7 GiB RAM/2 GiB Swap und müssen aus Messdaten angepasst werden, nicht durch automatische Limit-Erhöhung.

### 7.3 Startprofile

Empfohlene maximale Startprofile:

| Klasse | MemoryHigh | MemoryMax | MemorySwapMax | CPUQuota | Default Timeout |
|---|---:|---:|---:|---:|---:|
| LIGHT | 768 MiB | 1 GiB | 256 MiB | 100 % | 15 min |
| MEDIUM | 2 GiB | 2.5 GiB | 512 MiB | 200 % | 45 min |
| HEAVY | 3 GiB | 4 GiB | 1 GiB | 300 % | 120 min |
| EXCLUSIVE | 4.5 GiB | 5.5 GiB | 1.5 GiB | 400 % | step-spezifisch |

`MemoryMax` darf nur gestartet werden, wenn die Hostreserve trotzdem erhalten bleibt. Limits sind Ceiling, keine garantierte Zuteilung.

### 7.4 Failure-Klassifikation

- OOM kill, cgroup memory event, Swap-/Pressure-Abbruch, ENOSPC oder Governor-Termination → `RESOURCE`;
- Test Assertion/Compilerdiagnostik bei normalem Exit → `DETERMINISTIC`;
- Timeout ohne Pressure → `TRANSIENT` oder `DETERMINISTIC_STALL` nach Toolprofil;
- CI unavailable → `EXTERNAL`.

`RESOURCE` darf niemals automatisch als Codefehler an Implementer/QA zurückgegeben werden.

### 7.5 Retry-Regel

Bei `RESOURCE`:

1. keinen identischen sofortigen Retry;
2. Messdaten persistieren;
3. höchstens eine kontrollierte Alternative:
   - kleinere targeted Auswahl;
   - höhere Resource Class, wenn Hostreserve bewiesen;
   - externe CI;
4. andernfalls `BLOCKED/LOCAL_CAPACITY_INSUFFICIENT`.

Der erlebte `tsgo`-Fall soll direkt zu „autoritativer CI-Typecheck“ routen.

### 7.6 Temporäre Daten

- Repos, Package Stores, `node_modules`, Build Caches und große Artefakte in persistentem Workspace/Cache;
- `/tmp` nur für kleine bounded Dateien;
- pro Job eigener Temp-Root unter dem Job-Worktree oder persistentem Job-Cache;
- Cleanup nur nach Prozess- und Worktree-Verifikation.

---

## 8. Adaptive-Role-Modell

Die fünf Rollen bleiben. Aktiviert wird der kleinste sichere Plan.

| Jobklasse | Rollenplan | Beispiel |
|---|---|---|
| trivial | Lead-Funktion im Supervisor, Implementer, targeted verification | Tippfehler, kleine Config-unabhängige Korrektur |
| small | Lead, Implementer, QA-Funktion; Reviewer nur bei Findings | kleine Bugfix-Datei mit klarer Reproduktion |
| normal | Lead, Analyst, Implementer, QA, optional Reviewer | Multi-File Feature oder nichttrivialer Bug |
| complex | Lead, Analyst, Implementer, QA, unabhängiger Reviewer | Refactor, schwieriges Debugging, mehrere Subsysteme |
| security-critical | Lead/Sol, Analyst/Pro oder Sol, Implementer/Pro, QA, unabhängiger Sol-Reviewer, Owner-Gate falls sensibel | Trust Boundary, Auth, Sandbox, Secrets, Owner Approval |

### Regeln

- Lead ist eine Fähigkeit; Routineplanung kann der Flash-Supervisor übernehmen.
- Analyst wird aktiviert, wenn Problem/Root Cause nicht bereits beweiskräftig ist.
- Separate QA wird aktiviert bei Verhalten, Regression, mehreren Pfaden oder relevantem CI-Kontext.
- Reviewer muss unabhängig vom Writer sein.
- Security-/Architektur-Closing Review darf keine Änderungen schreiben.
- Ein Rollenplan wird nicht von Agent-Output erweitert. Agent-Findings sind Input für eine vertrauenswürdige Supervisor-Entscheidung.
- Rollenplanänderung wird mit reason code und `facts_version` persistiert.

---

## 9. Model-Routing-Modell

### Flash

Standard für:

- Supervisor-/Queue-Koordination;
- Status und Reconciliation-Zusammenfassungen;
- einfache Analyse;
- triviale/small Implementierung;
- targeted QA;
- Context-Pack-Erstellung;
- CI-/Wait-Statusauswertung;
- einfache Git-/Logdiagnostik.

### Pro

Für:

- normale bis komplexe Implementierung;
- Multi-File-Änderungen;
- nichttriviales Debugging;
- Refactoring;
- tiefergehende Analyse;
- robuste Testentwicklung.

### Sol High

Nur für:

- Architekturentscheidungen;
- Security-/Trust-Boundary-Review;
- schwierige Root Causes nach unzureichendem Flash/Pro-Ergebnis;
- riskante, mehrkomponentige Planung;
- wichtige unabhängige Closing Reviews.

### Routing-Algorithmus

```text
capability floor
 + task complexity
 + risk/security
 + prior failed evidence
 + independence requirement
 = minimum sufficient model
```

Eskalation:

```text
Flash
  -> verifizieren
  -> Pro nur bei begründetem Capability Gap
  -> verifizieren
  -> Sol nur bei hartem Problem oder Review-Pflicht
```

Keine Eskalation allein aufgrund von:

- langem externem Wait;
- Resource-Limit;
- fehlendem CI-Ergebnis;
- Agent-Selbsteinschätzung;
- einer ersten reparierbaren Formatabweichung.

Persistieren:

- requested/actual provider/model/tier;
- routing reason;
- escalation source;
- grobe Input-/Output-Token;
- optional geschätzte Kostenklasse, keine Secrets.

---

## 10. Context-Engineering-Modell

### 10.1 Context Pack

Jeder Dispatch erhält genau ein immutable Manifest:

```text
task contract
role + capability
policy/version refs
job state + facts_version
base_commit + current diff_hash
relevant file refs
confirmed findings
open questions
allowed write scope
test plan + target CI shard
resource class
artifact handoff refs
output schema
context budget
```

### 10.2 Budgets

Startwerte:

- Flash coordination/status: 8–16k Token;
- Flash/Pro implementation: 24–48k;
- Sol architecture/security review: 48–96k nur bei Bedarf;
- einzelne Datei-/Diff-Artefakte werden gezielt retrieved, nicht vollständig eingebettet.

Budgetüberschreitung führt zu Summarization/Artifact Refs, nicht automatisch zu größerem Modell.

### 10.3 Retrieval Policy

Ein Kontext darf nur enthalten, was mindestens eine Regel erfüllt:

- direkt vom Task referenziert;
- durch Reproduktion/Test berührt;
- im aktuellen Diff geändert;
- Dependency/Caller des betroffenen Codes;
- bestätigtes Finding;
- Ziel-CI-Konfiguration;
- relevante Policy-/Security-Regel.

Repo-weiter Dump ist verboten.

### 10.4 Facts und Trust

Jeder Eintrag wird klassifiziert:

- `OWNER_REQUIREMENT`;
- `POLICY`;
- `LEDGER_FACT`;
- `REPO_EVIDENCE`;
- `TEST_EVIDENCE`;
- `AGENT_CLAIM_UNVERIFIED`;
- `EXTERNAL_OBSERVATION_UNTRUSTED`.

Nur bestätigte Findings gehen als `confirmed_findings` in den Implementer-Kontext.

### 10.5 Invalidation

Context Pack wird ungültig bei Änderung von:

- `facts_version`;
- Base-/HEAD-Commit;
- Diff Hash;
- Worktree Identity;
- Policy Version;
- Role Plan;
- akzeptierten Findings;
- Test Plan/CI Target;
- Gate Scope.

Stale Pack darf nicht für einen neuen Dispatch wiederverwendet werden.

### 10.6 Checkpoints

Nach jedem Rollenabschluss wird ein bounded Checkpoint persistiert:

- was bewiesen ist;
- was geändert wurde;
- welche Tests liefen;
- offene Findings;
- Artifact Refs;
- nächster sicherer Schritt.

Keine Chain-of-Thought, keine Rohprompts.

### 10.7 Artifact-Handoff

Zwischen Rollen werden referenziert:

- Plan Artifact;
- Patch/Commit Ref;
- Diff Summary;
- Test Evidence;
- Review Findings;
- External CI Evidence.

Der Ledger speichert Hash/Ref/Metadaten; große oder sensitive Inhalte bleiben im rollen- und worktree-gescopten Artefaktspeicher.

---

## 11. Teststrategie

### Testleiter

1. **Targeted tests**
   - kleinster reproduzierender Test;
   - Formatter/Linter nur für betroffene Dateien;
   - schnelle Unit Tests.

2. **Relevanter Integrationstest oder CI-Shard**
   - exakt derselbe Runner/Config/Extension-Shard wie Ziel-CI;
   - Root-local PASS ersetzt keinen Extension-Shard-PASS.

3. **Größere Regression**
   - nach stabiler targeted/integration Evidenz;
   - ressourcenbewusst.

4. **Full Regression**
   - nur am Closing Gate;
   - bevorzugt externe CI, wenn lokale Hardware ungeeignet ist.

### Pflichtartefakt `test_plan`

- geänderte Komponenten;
- gezielte Tests;
- relevanter CI-Kontext;
- benötigte Resource Class;
- maximale lokale Laufzeit;
- Closing Regression;
- autoritative externe Checks.

### Resultatklassifikation

- `PASS`;
- `CODE_FAILURE`;
- `TEST_INFRA_FAILURE`;
- `RESOURCE_LIMIT`;
- `EXTERNAL_PENDING`;
- `EXTERNAL_FAILURE`;
- `SECURITY_BLOCK`.

Nur `CODE_FAILURE` autorisiert fachliches Rework.

### Recovery-/Architekturtests

Pflichtmatrizen:

- Lease Expiry/Fencing;
- zwei Supervisoren claimen denselben Job;
- Supervisor-Tod bei laufendem read-only Agent;
- Supervisor-Tod bei Writer;
- WSL Boot-ID-Wechsel;
- External Wait über Restart;
- CI-Event-Duplikat;
- Resource OOM/ENOSPC/Timeout;
- Worktree-Divergenz;
- stale Context Pack;
- DONE-Stickiness;
- Gate-/Notification-Dedup;
- Visualizer bleibt read-only.

---

## 12. Worktree-/Git-Modell

### Invariante

> Ein logischer Job besitzt genau einen eindeutigen Writer-Worktree. Zu jedem Zeitpunkt existiert höchstens ein gültiger Writer-Lease für diesen Worktree.

### Naming

```text
argent/<short-job-id>/<sanitized-purpose>
worktree path: /home/pc/projects/argent-worktrees/<short-job-id>
```

Pfad, Repo-Identity und Base Commit werden kanonisch aufgelöst und persistiert.

### Erstellung

- nur Supervisor/Worktree Manager;
- expliziter Base Commit;
- Worktree muss neu oder eindeutig wiederverwendbar sein;
- bestehender unbekannter Dirty State → kein Claim.

### Writer-Lease

Gebunden an:

- job_id;
- worktree_id;
- dispatch_id;
- lease_epoch;
- owner_instance_id.

WorkspaceBroker prüft zusätzlich zur Controller-Source die aktuelle Writer-Bindung.

### Commit Policy

- lokale Commits sind je nach Task-Policy autonom erlaubt;
- Commit Message und Scope vom Supervisorvertrag, nicht aus Agent-Prosa;
- kein Push, Merge, Tag, Stable-Promotion oder externe PR-Aktion ohne passende Policy/Owner-Autorisierung;
- Tests und Evidence vor Closing Commit.

### Abandoned Worktree Recovery

- Prozessstatus prüfen;
- Job-/Writer-Lease prüfen;
- `git status`, HEAD, Base und Journal Hash read-only prüfen;
- konsistent und terminal: `CLEANUP_PENDING`;
- dirty, aber eindeutig job-owned: für Recovery behalten;
- divergent/mehrdeutig: `BLOCKED`, nichts überschreiben.

### Merge Queue

Erst Phase I:

- seriell;
- Rebase/Merge gegen aktuellen Ziel-HEAD in isoliertem Integrations-Worktree;
- relevante Tests erneut;
- Konflikt → zurück an denselben Job, kein fremder Writer im Job-Worktree;
- keine breite parallele Writer-Flotte.

---

## 13. Background-/External-Wait-Modell

### Supervisor Service

Ein einzelner langlebiger lokaler Dienst:

- besitzt Scheduler-Lease;
- führt kurze bounded Reconcile-Passes aus;
- hält keine TUI-Session offen;
- startet Agenten detached und registriert Prozesse;
- beendet sich nicht bei Telegram-/Visualizer-Ausfall;
- lädt nach Reboot Ledger und Boot-ID.

Die konkrete systemd-/Autostart-Aktivierung ist eine Konfigurationsänderung und benötigt den vorgesehenen Owner-Gate in Phase G.

### External Wait

Wenn ein Step externe Abhängigkeit erreicht:

1. aktueller Agent erzeugt strukturiertes Wait-Artefakt;
2. Supervisor validiert Provider/Ref/Allowed Check;
3. `external_waits` und Job-State werden atomar committed;
4. Agent/Compute werden freigegeben;
5. billiger Watcher prüft per Event oder bounded Poll;
6. Backoff z. B. 1, 2, 5, 10, 30 Minuten mit Jitter und Deadline;
7. relevante Änderung erzeugt genau ein Wake-Ereignis;
8. Job geht nach `QUEUED`;
9. neue Context Pack enthält nur Wait-Ergebnis und vorherigen Checkpoint.

CI-spezifisch:

- `wait_kind=CI`;
- persistiert werden Repo-/PR-/Run-/Check-Refs, erwarteter Commit SHA und erlaubte Checks;
- CI für falschen SHA ist stale und bewirkt nichts;
- pending/queued ist kein Fehler;
- roter Check führt nur nach Logklassifikation zu Code-Rework;
- grüner Check kann Closing Gate auslösen, aber nicht direkt DONE setzen.

### Notifications

Telegram sendet nur:

- `GATE`;
- `ERROR` inklusive BLOCKED/FAILED/PERSISTENT_ERROR mit allowlisted reason code;
- `DONE`.

Keine Meldung für normales Queuing, Running, Retry, CI-pending oder Recovery-Fortschritt.

---

## 14. Security-/Trust-Boundary-Prüfung

### Ergebnis

Die bestehende Trust Boundary ist für die Zielarchitektur geeignet, wenn alle neuen Queue-/Wait-/Resource-/Worktree-Pfade dieselbe Regel übernehmen:

> Agent-Ausgabe und sämtliche externen Inhalte sind UNTRUSTED DATA.

### Prüfung neuer Zustände

**QUEUED**

- darf nur durch authentifizierten lokalen Task-Create-/Recovery-Pfad entstehen;
- Email, Website, Repo-Text, Tooloutput oder CI-Text dürfen keinen Job anlegen.

**RUNNING**

- nur Scheduler-Lease + Policy + Resource Admission;
- Agent-Text darf keinen eigenen Spawn oder Modellwechsel auslösen.

**WAITING_EXTERNAL**

- External Ref muss aus lokalem Taskvertrag/allowlisted Adapter stammen;
- externe Payload kann Statusdaten liefern, aber keine Shell, Writes, Approval oder Scope-Erweiterung.

**RETRYING**

- Retry wird aus lokaler Fehlerklassifikation abgeleitet;
- ein Agent darf „retry me with Sol/unlimited memory“ nur als unverifizierte Empfehlung äußern.

**OWNER_GATE**

- ausschließlich bestehendes Approval Ledger;
- Wait-/CI-/Agent-Events dürfen kein Gate erzeugen, approve/reject/consume oder dessen Scope ändern.

**LOST**

- ist fail-closed;
- untrusted „process completed“ oder Agent-Completion reicht nicht zur Auflösung;
- Writer-Recovery braucht technische Provenance.

**DONE**

- ausschließlich Core-DONE plus Closing-Invarianten;
- CI-grün, Telegram-Callback, Agent-Behauptung oder Visualizerstatus allein reichen nicht.

### Neue Komponenten

**Resource Governor**

- Limits und Klassen kommen aus lokaler Policy;
- Agent kann keine cgroup-Limits erhöhen;
- Tooldiagnostik ist Daten, nicht Autorisierung.

**Worktree Manager**

- Agent kann Pfad, Repo oder Branch nicht frei bestimmen;
- kanonische Pfade/Repo-Identity aus persistiertem Jobvertrag;
- keine Symlink-/Path-Traversal-Erweiterung.

**Context Router**

- untrusted Inhalte behalten Herkunftsklasse;
- keine automatische Aufnahme externer Instruktionen in `project_rules`;
- Secrets und verbotene Felder bleiben ausgeschlossen.

**External Watcher**

- enges, read-only Provider-Interface;
- keine generische Webhook-to-command-Brücke;
- Event Dedup und SHA-Bindung;
- kein zweiter Telegram-Poller.

**Background Service**

- keine neuen Privilegien;
- kein Root;
- keine freie Shell-/Secret-Schnittstelle;
- Dienstaktivierung und Credential-/Gateway-Änderungen bleiben Owner-Gates.

**Self-Improvement**

- niemals automatische Adoption;
- Agenten können nur Proposals liefern;
- Umsetzung isoliert, Review unabhängig, Adoption Owner-gated.

### Offene Security-Feststellung

Das aktuelle `routing.py` bindet Lead/Reviewer statisch an Sol und wird künftig Policy-Code. Änderungen daran sind sicherheitsrelevant, weil Modell-/Rollenrechte Dispatch-Provenance beeinflussen. Routing-Policy muss versioniert und im Dispatch persistiert werden.

---

## 15. Konkrete Implementierungsphasen A–L

Die vorgegebene Reihenfolge ist technisch sinnvoll und bleibt erhalten. Insbesondere werden Parallelisierung und Self-Improvement bewusst spät eingeordnet.

### Phase A — Architecture Freeze

**Scope**

- dieses Zielmodell als verbindlichen Vertrag festlegen;
- Primary States, orthogonale Felder, Error Taxonomy, Lease-/Fencing-Invarianten;
- Schemaentwurf und Compatibility Mapping zum bestehenden Supervisor.

**Deliverables**

- finale Architektur-Spec;
- State-/Transition-Tabelle;
- Schema-/Migration-Plan;
- Failure-/Crash-Matrix;
- Owner-Entscheidungen dokumentiert.

**Exit-Kriterien**

- keine ungeklärte zweite State Machine;
- alle bestehenden V2C/V3-Invarianten gemappt;
- unabhängiger read-only Architektur-/Security-Review abgeschlossen;
- keine Implementierung begonnen.

### Phase B — Durable Supervisor Core

**Scope**

- `job_queue`;
- Claims, TTL, Epoch/Fencing;
- Scheduler bounded passes;
- Error Taxonomy;
- Migration bestehender `supervisor_jobs` ohne Semantikverlust.

**Deliverables**

- persistente Queue;
- atomare Claim-/Renew-/Release-Operationen;
- Lease-aware Action Commits;
- Restart-/Dual-Supervisor-Tests.

**Exit-Kriterien**

- nie zwei gültige Owner desselben Jobs;
- alter Lease-Holder kann nicht committen;
- Queue/Restart/DONE-Stickiness vollständig getestet;
- einzelner Job läuft mehrere Stunden mit simulierten Crashes zuverlässig.

### Phase C — Resource Governor

**Scope**

- Klassen, Preflight, Admission;
- cgroup/systemd-scope Adapter;
- Process Registry;
- Resource Error Classification.

**Deliverables**

- Host-/tmpfs-/Disk-/Swap-Checks;
- Limits pro Klasse;
- OOM-/ENOSPC-/Timeout-Evidence;
- Cleanup-/orphan recovery.

**Exit-Kriterien**

- Heavy-Test kann Host nicht unkontrolliert erschöpfen;
- Resource Failure wird nie als Code Failure geroutet;
- `/tmp`-Policy durch Tests erzwungen;
- verwaiste Scopes werden erkannt.

### Phase D — Context Engineering

**Scope**

- Context Packs;
- Budgets;
- Retrieval;
- Invalidation;
- Artifact-Handoff.

**Deliverables**

- versioniertes Pack-Schema;
- role-spezifische Builder;
- confirmed/unverified fact labels;
- checkpoint/artifact registry.

**Exit-Kriterien**

- kein History-Dump;
- stale Pack wird fail-closed abgewiesen;
- Security-/Privacy-Tests;
- messbare Context-Reduktion ohne Verlust relevanter Tests/Fakten.

### Phase E — Adaptive Roles + Model Routing

**Scope**

- persistierte Rollenpläne;
- Complexity/Risk Classifier;
- Flash-first Routing;
- begründete Eskalation.

**Deliverables**

- Plan Templates trivial–security-critical;
- Routing Policy Version;
- Independence Rules;
- Cost-/Token-Telemetrie.

**Exit-Kriterien**

- triviale Jobs benötigen nicht automatisch fünf Agenten;
- Routine Lead/QA läuft mit Flash;
- Sol nur bei allowlisted Gründen;
- Closing Reviewer unabhängig vom Writer.

### Phase F — Test Economy

**Scope**

- Test Plan Artifact;
- targeted/integration/shard/regression Stufen;
- CI-Kontext-Bindung;
- Resultatklassifikation.

**Deliverables**

- Test Selector;
- CI-Shard-Metadaten;
- Resource-aware Test Scheduler;
- Closing Gate Policy.

**Exit-Kriterien**

- Root-local PASS kann relevanten Shard nicht ersetzen;
- identischer Commit SHA bindet Test-/CI-Evidence;
- Full Regression nur am Closing Gate;
- Resource Limit bleibt separat.

### Phase G — Background Operation / Reboot Recovery

**Scope**

- dauerhafter Supervisor Host;
- Boot-ID-Reconciliation;
- Service Health;
- UI-Unabhängigkeit.

**Deliverables**

- bounded Service Loop;
- singleton Scheduler Lease;
- Reboot-Recovery;
- explicit Owner-gated Installation Plan.

**Exit-Kriterien**

- TUI kann geschlossen werden, Job läuft/wartet weiter;
- WSL-/PC-Reboot führt deterministisch zu Reconciliation;
- kein Blind-Respawn;
- Telegram/Visualizer-Ausfall ist wirkungsneutral;
- Service-Aktivierung vom Owner genehmigt.

### Phase H — Telegram Owner Approval B2B

**Scope**

- erst wenn Upstream `updateId/messageDate` autoritativ bereitstellt;
- bestehender OpenClaw Interactive Handler;
- kein zweiter Poller.

**Deliverables**

- Host-Contract-Verifikation;
- bounded Callback Dispatcher;
- reale, owner-autorisierte Loopback-/Live-Tests;
- Version Compatibility Guard.

**Exit-Kriterien**

- Upstream-Metadaten unverfälscht verfügbar;
- Callback verlässt Agenten-Textpfad;
- exactly-once lokale Entscheidung;
- Approval ist weiterhin nicht Execution;
- separater Owner-Gate für Aktivierung.

### Phase I — Controlled Parallelization + Worktrees + Merge Queue

**Scope**

- Worktree Registry;
- Writer Lease;
- initial wenige read-only Jobs;
- serielle Merge Queue.

**Deliverables**

- 1:1 Job-/Worktree-Bindung;
- abandoned worktree recovery;
- Merge/Rebase Verification;
- Concurrency Policy.

**Exit-Kriterien**

- nie zwei Writer im selben Worktree;
- global zunächst max. ein Writer;
- Konflikte sind reproduzierbar und fail-closed;
- Einzeljob-Stabilitätsziele aus B–H nachgewiesen.

### Phase J — Productive Autonomous E2E

**Scope**

- reale kleine bis normale Owner-Tasks;
- stundenlange Lauf-, Wait-, Crash- und Recovery-Szenarien;
- Kosten-/Ressourcenmessung.

**Deliverables**

- E2E Task Suite;
- CI-Wait-Fall;
- Reboot-Fall;
- Owner-Gate-Fall;
- Resource-Failure-Fall.

**Exit-Kriterien**

- mehrere produktive Tasks bis DONE ohne manuelles Babysitting;
- kein Ghost Writer;
- kein teurer Agent im External Wait;
- kein WSL-Stall;
- Notifications nur GATE/ERROR/DONE.

### Phase K — Optional Specialists

**Scope**

- nur evidenzbasiert zusätzliche Fähigkeiten, keine festen Pflichtrollen;
- z. B. Security Specialist, Performance Specialist, Dependency Specialist.

**Deliverables**

- Capability Contracts;
- activation criteria;
- scoped Context/Permissions;
- Cost Review.

**Exit-Kriterien**

- Spezialist löst wiederholt ein belegtes Problem besser;
- kein Rollen-/State-Sprawl;
- keine neuen gefährlichen Tools ohne Owner-Gate.

### Phase L — Owner-gated Self-Improvement

**Scope**

- Proposal → isolated implementation → tests → independent review → Owner Gate → adoption.

**Deliverables**

- Proposal Schema;
- isolated self-change Worktree;
- immutable before/after policy refs;
- rollback plan;
- adoption gate.

**Exit-Kriterien**

- keine automatische Selbstmodifikation;
- unabhängiger Reviewer;
- vollständige Regression;
- Owner genehmigt exakt gebundene Änderung;
- Stable Tags/Policies niemals automatisch überschrieben.

---

## 16. Risiken

| Risiko | Auswirkung | Mitigation |
|---|---|---|
| Zustandsmodell wächst erneut unkontrolliert | schwer verständliche Recovery | Primary State + orthogonale Felder strikt durchsetzen. |
| Lease TTL zu kurz | falsche Übernahme bei langsamen Steps | Step-spezifische TTL, Renewal und Fencing; Progress nicht mit Liveness verwechseln. |
| Lease TTL zu lang | langsame Crash-Recovery | Prozess-/Boot-ID-Evidenz darf Lease vorzeitig sicher invalidieren. |
| SQLite Writer Contention | verzögerter Scheduler | kurze Transaktionen, ein Scheduler, bounded Worker, kein Visualizer-DB-Zugriff. |
| Resource Limits zu niedrig | falsche Resource Failures | Messdaten, eine kontrollierte Reclassification, externe CI. |
| Resource Limits zu hoch | WSL-Stall | Hostreserve und globale Admission vor cgroup Start. |
| External Watcher wird Polling-Loop | Kosten/Rate Limits | Event-first, exponentielles Backoff, Deadline, kein LLM. |
| CI-Result für falschen SHA | falsche Abnahme | Wait-/Test-Evidence an Commit SHA und Check-Set binden. |
| Adaptive Rollen sparen zu aggressiv | übersehene Regression/Security | Mindestpläne nach Risk/Diff/Subsystem; Closing Gates. |
| Model Routing eskaliert schleichend | hohe Kosten | persistierte reason codes und Budgetauswertung. |
| Context Summary verliert entscheidende Fakten | fehlerhafte Implementierung | Artifact Refs, confirmed facts, Retrieval-on-demand. |
| Context Pack enthält Prompt Injection | Autoritätsausweitung | Herkunftsklassen, keine externen Texte als Policy, strikte Schemas. |
| Worktree Recovery löscht wertvolle Arbeit | Datenverlust | kein automatisches Löschen dirty/ambiguous Worktrees. |
| Service-Autostart erweitert Host-Angriffsfläche | Security-/Betriebsrisiko | unprivilegiert, lokale Pfade, Owner-Gate, kein Gateway Exposure. |
| Telegram-Upstream ändert Contract | falsche Approval-Metadaten | Compatibility Guard; fail-closed deaktivieren. |
| Visualizer wird versehentlich Control Plane | Trust-Boundary-Bruch | nur Snapshot-Datei/GET, kein POST/Command-Pfad. |
| Self-Improvement verändert Policy | Sicherheitsverlust | erst Phase L, isoliert, unabhängiger Review, Owner Gate. |

---

## 17. Offene Owner-Entscheidungen

1. **Background Hosting:** Darf Phase G einen unprivilegierten systemd-user-Service installieren, oder soll zunächst ein manuell gestarteter langlebiger Prozess verwendet werden?
2. **Lokaler Storage:** Finaler Root für Job-Worktrees, Context Artifacts und persistente Temp-Daten.
3. **Resource Defaults:** Zustimmung zu den konservativen Startwerten und dazu, große Typechecks standardmäßig an externe CI zu delegieren.
4. **Queue Priorität:** FIFO als Default oder einfache Klassen `URGENT/NORMAL/BACKGROUND`; Empfehlung: FIFO + manuelle Priority, keine komplexe Fairnesslogik.
5. **Blocked Semantik:** Soll `BLOCKED` nur durch neuen Owner-Auftrag reaktivierbar sein? Empfehlung: ja.
6. **LOST Semantik:** Soll ein über Deadline unauflösbarer Writer automatisch `BLOCKED` werden oder dauerhaft `LOST` bleiben? Empfehlung: nach bounded Reconciliation `BLOCKED/AMBIGUOUS_WRITER`.
7. **Commit Policy:** Dürfen normale lokale Jobs weiterhin autonom lokale Commits erzeugen? Empfehlung: ja, sofern Task-Scope, Tests und keine externen Aktionen.
8. **Merge Queue:** Soll lokales Mergen in Phase I autonom sein oder Owner-gated bleiben? Empfehlung: zunächst Owner-gated, bis mehrere E2E-Runs stabil sind.
9. **External CI Access:** Welche Repositories/Checks dürfen vom Watcher read-only abgefragt werden?
10. **Notification Mapping:** Zustimmung zu nur drei Owner-Kategorien `GATE`, `ERROR`, `DONE`, wobei FAILED/BLOCKED/PERSISTENT_ERROR unter ERROR differenziert werden.
11. **Telegram B2B:** Aktivierung erst nach verfügbarem Upstream-Release und separatem Live-Test-Gate bestätigen.
12. **Visualizer-Erweiterung:** Darf der Snapshot um Lease-, Resource-, Wait- und Cost-Felder erweitert werden, sofern die bestehende Closed-Allowlist und Secret-Canaries aktualisiert werden?
13. **Self-Improvement:** Phase L grundsätzlich zulassen oder dauerhaft deaktiviert lassen? Empfehlung: standardmäßig deaktiviert; Aktivierung nur durch expliziten Owner-Beschluss.
14. **Policy-Datei:** Der in den Workspace-Regeln referenzierte `ARGENT_SUPERVISOR.md`-Pfad fehlt. Soll die eingebettete Policy bewusst alleinige Quelle bleiben oder eine separate, integritätsgeprüfte Policy-Datei wiederhergestellt werden? Dies ist eine Policy-/Security-Entscheidung und darf nicht implizit erfolgen.
