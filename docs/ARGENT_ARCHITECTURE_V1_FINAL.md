# ARGENT ARCHITECTURE V1 FINAL

**Phase A — Architecture Freeze** (2026-08-31)

Basis: `docs/ARCHITECTURE_REVIEW_V1.md` (Sol-Review, extern vom Owner geprüft) plus
verbindliche Owner-Entscheidungen/-Korrekturen vom 2026-08-31. Diese Spec ist der
verbindliche Zielvertrag für die Implementierungsphasen B–L.

Status: **Nur Dokumentation. Kein produktiver Code, keine Migration, kein systemd,
keine Policy-Aktivierung, kein Commit, kein Push, kein Merge.**

---

## 1. Architekturprinzipien

1. **Reliability** > **Security** > **Correctness** > **Resource Safety** > **Cost Efficiency** > **Speed** > **Parallelism**.
2. **SQLite ist die einzige lokale Autorität.** Keine Redis/Kafka/verteilten Systeme.
3. **Agent-Ausgabe und alle externen Inhalte sind UNTRUSTED DATA** — nie Autorität, nie Owner-Instruktion.
4. **1 logical job = 1 uniquely owned writer worktree = maximal 1 gültige Writer-Lease.**
5. Keine starke Parallelisierung, bevor ein einzelner autonomer Job zuverlässig stundenlang laufen, warten, crashen und recovern kann.
6. **Resource-Limit-Failure ist kein Code-Failure.** Keine automatische Limit-Erhöhung.
7. Kein Overengineering: Argent ist ein Home-/Development-System, kein Enterprise-Rechenzentrum. Neue Tabellen/Registries nur phasenweise, nur wenn nötig.
8. Owner-Gates bleiben für sensitive Aktionen. TUI/Telegram/Visualizer sind Interfaces, nicht Prozess-Owner.

## 2. Komponenten (Überblick)

| Komponente | Verantwortung | Quelle |
|---|---|---|
| Core V1 (Ledger, TaskState, Gates, Broker, Sandbox) | fachlicher Zustand, Rollenrechte, Dispatch-Provenance, Write-Broker, bwrap | KEEP (unverändert) |
| Durable Supervisor Service | Queue/Admission, Claim/Renewal/Fencing, Reconciliation, adaptive Role-/Model-Planung, Terminalentscheidung | CHANGE (V2C-Supervisor erweitert) |
| Job Queue (additiv auf `supervisor_jobs`) | persistente Job-Felder, Lease am Job, `next_eligible_at`, attempt, wait-Refs | ADD (Phase B) |
| Resource Governor | Preflight, Resource Classes, cgroup/systemd-scope Ceilings, Failure-Klassifikation | ADD (Phase C) |
| External Wait Manager | persistente Wait-Subscription, event-first/bounded non-LLM Watcher, Deadline, Wake-Dedup | ADD (Phase B) |
| Process Registry (minimal) | PID, boot_id, Startzeit, cgroup-Ref, Exit-Fakten | ADD (Phase B) |
| Worktree Manager (minimal) | 1:1 Job-/Worktree-Bindung, Writer-Lease, Base-Commit, Dirty-/HEAD-Fakten | ADD (Phase B, voll in Phase I) |
| Context Router | immutable Context Packs, Budget, Retrieval, Invalidation, Artifact-Handoff | ADD (Phase D) |
| Interfaces | TUI (Status/Control), Telegram (nur GATE/ERROR/DONE + begrenzte Approval-Callbacks), Visualizer (nur read-only Snapshot) | KEEP |

## 3. Primary State Machine — exakt 8 States

```text
QUEUED ──────────────► RUNNING ──────────────► WAITING_EXTERNAL
   ▲                      │  │                     │
   │                      │  │                     │ (Event/Deadline)
   │                      │  │                     ▼
   │                      │  └──► OWNER_GATE        QUEUED ◄──┐
   │                      │         │  │                        │
   │                      │         │  └──► BLOCKED             │
   │                      │         └────► QUEUED (approve)     │
   │                      │                                     │
   │                      ├──► LOST ──bounded reconcile──► QUEUED | BLOCKED
   │                      ├──► BLOCKED
   │                      ├──► FAILED
   │                      └──► DONE
   └── resource/retry ───► QUEUED (next_eligible_at)
```

| State | Semantik |
|---|---|
| `QUEUED` | wartet auf Admission/Lease. Retry/Backoff ist **kein State**, sondern `QUEUED` + `queue_reason=RETRY_BACKOFF` + `next_eligible_at` + `attempt_no` + `error_class/error_code`. |
| `RUNNING` | besitzt Lease, hat einen ausführbaren aktiven Step. |
| `WAITING_EXTERNAL` | wartet auf externe Abhängigkeit; **kein aktiver LLM-Agent**. CI = `wait_kind=CI`. `WAITING`/`WAITING_FOR_CI` sind keine Primary States. |
| `OWNER_GATE` | Owner-Entscheidung offen; baut auf bestehendem Approval-Ledger auf. |
| `BLOCKED` | suspended/fail-closed. Nur explizite Owner-/Policy-Autorisierung darf `BLOCKED → QUEUED`. |
| `FAILED` | **immutable terminal**. Späterer Reparaturversuch = neuer referenzierender Job. |
| `LOST` | Recovery-Quarantäne. Bounded Reconciliation; danach bestehende Writer-/Ownership-Ambiguität → `BLOCKED/AMBIGUOUS_WRITER`. |
| `DONE` | **immutable terminal**, sticky. |

## 4. Transition Table

| Von | Nach | Guard/Bedingung |
|---|---|---|
| QUEUED | RUNNING | atomarer Lease-Claim + Resource-/Worktree-Preflight erfolgreich |
| QUEUED | BLOCKED | invalide Policy, unauflösbarer Worktree-Konflikt, fehlende zwingende Owner-Entscheidung |
| RUNNING | WAITING_EXTERNAL | Wait-Artefakt validiert + atomar persistiert; danach Agent/Compute freigegeben |
| RUNNING | OWNER_GATE | bestehendes Gate im selben autoritativen Commit |
| RUNNING | LOST | Lease-/Runtime-/Writer-Fakten widersprüchlich, sichere Fortsetzung unbeweisbar |
| RUNNING | BLOCKED | unauflösbarer Zustand, keine autonome sichere Aktion |
| RUNNING | FAILED | deterministischer Fehler oder ausgeschöpfte erlaubte Attempts |
| RUNNING | DONE | Core-Task-DONE + fachliche Closing-Invarianten bewiesen |
| RUNNING | QUEUED | klassifizierter retryfähiger Fehler (attempt_no+1, `queue_reason=RETRY_BACKOFF`) |
| WAITING_EXTERNAL | QUEUED | relevantes Event oder Deadline; niemals direkt Agent starten |
| OWNER_GATE | QUEUED | Approval erteilt + exakte Closure erlaubt Fortsetzung |
| OWNER_GATE | BLOCKED | Reject, terminale Expiry-Policy, Binding-Konflikt |
| LOST | QUEUED | Reconciliation beweist eindeutigen sicheren Zustand |
| LOST | BLOCKED | Ambiguität bleibt → `BLOCKED/AMBIGUOUS_WRITER` |
| LOST | FAILED | Recovery-Beleg zeigt Terminalfehler |
| BLOCKED | QUEUED | **nur** explizite Owner-/Policy-Autorisierung |
| FAILED | – | keine Transition; Reparatur = neuer referenzierender Job |
| DONE | – | keine Transition (immutable, sticky) |

## 5. Orthogonale Jobfelder (nicht als States modellieren)

```text
queue_reason      = NEW | RETRY_BACKOFF | WAIT_EVENT | WAIT_DEADLINE | GATE_APPROVED | RECOVERY
wait_kind         = CI | UPSTREAM | RATE_LIMIT | NETWORK | TIMER | NONE
recovery_phase    = NONE | DISCOVERING | REBINDING | RECONCILING_WORKTREE | ...
error_class       = TRANSIENT | DETERMINISTIC | RESOURCE | EXTERNAL | SECURITY | OWNER_REQUIRED | NONE
error_code        = allowlisted Code, z. B. WORKTREE_DIVERGED | LOCAL_CAPACITY_INSUFFICIENT | AMBIGUOUS_WRITER
attempt_no        = Anzahl Versuche
next_eligible_at  = frühester Admissionszeitpunkt (Retry/Backoff/Wait-Deadline)
priority          = FIFO Default; optionale explizite Owner-Priority
resource_class    = LIGHT | MEDIUM | HEAVY | EXCLUSIVE
role_plan_version / context_checkpoint_id / worktree_id
```

## 6. Lease-/Fencing-Invarianten

- Lease-Felder **direkt am Job**: `owner_instance_id`, `lease_epoch`, `lease_expires_at`. **Keine separate `job_leases`-Tabelle in V1.**
- Jeder mutierende Supervisor-Commit prüft: `job_id` + `owner_instance_id` + `lease_epoch` + `lease_expires_at > now` + `facts_version`.
- `lease_epoch` ist Fencing Token: ein alter Supervisor kann nach Lease-Übernahme keine Wirkung mehr committen.
- Prozessidentität: `boot_id` + `pid` + `process_start_ticks` + cgroup-Ref (Process Registry). Boot-ID-Wechsel invalidiert alle alten Prozessregistrierungen.
- Externe Prozesse/Agent-Resultate haben keine direkte Ledger-Schreibautorität; sie bleiben untrusted und laufen durch die bestehende Provenance-Grenze.
- Bestehendes `supervisor_actions`-Action-Journal bleibt Audit-/Crash-Evidence (unverändert).

## 7. Recoverymodell

Autoritätsreihenfolge: Core-Ledger → Supervisor-/Queue-Ledger → Action-Journal → Dispatch-/Run-Bindungen → Prozessidentität → Worktree-/Git-Fakten → allowlistete externe Provider-Fakten → Events/Agent-Prosa (nichtautoritativ).

| Ereignis | Verhalten |
|---|---|
| Agent-Crash | Prozess terminal belegen, Dispatch markieren, Writer-Worktree unverändert sichern; Output nur bei vollständig gebundener validierter Completion konsumieren; Retry nach Fehlerklasse |
| Supervisor-/Gateway-Restart | laufende Prozesse nicht blind beenden; Leases neu bewerten; Action-Journal rebinden; legitime Waits bleiben Waits; keine Notifications/Gates duplizieren |
| WSL-Restart/PC-Reboot | boot_id-Wechsel ⇒ alle Prozessregistrierungen tot; Leases verfallen; Worktrees + SQLite bleiben; Writer zunächst `LOST` bis Git-/Journal-Reconciliation eindeutig; externe Waits bleiben; Jobs erst nach globaler Reconciliation admitted |

- **Stall-Erkennung:** getrennte Zeitwerte `supervisor_heartbeat_at`, `agent_heartbeat_at`, `process_observed_at`, `last_progress_at`, `external_wait_observed_at`. Stall erst bei: RUNNING, kein deklarierter External Wait, kein erlaubter Long-Running-Step, fehlende Signale/Progress innerhalb step-spezifischer Frist, kein reiner Host-Pressure, **mind. 2 unabhängige Beobachtungen**.
- **Writer-Recovery:** unbekannter Writerstatus → `LOST`, kein zweiter Implementer; Worktree-HEAD, Dirty-Hash, Journal-Pre-/Effect-Hash, Prozessscope und Dispatch prüfen; nur bei terminalem altem Writer + konsistentem Worktree neuer Writer-Dispatch; Divergenz → `BLOCKED/WORKTREE_DIVERGED`.

## 8. External Wait Modell (Core in Phase B)

```text
RUNNING
 -> Agent erzeugt strukturiertes Wait-Artefakt
 -> Supervisor validiert Provider/Ref/erlaubte Checks
 -> external_waits + Job-State atomar persistieren
 -> Agent und Compute freigeben
 -> WAITING_EXTERNAL
 -> event-first / bounded non-LLM Watcher (Backoff 1/2/5/10/30 min + Jitter, Deadline)
 -> relevantes Event (genau ein Wake, dedupliziert)
 -> QUEUED
 -> neue Admission (Lease + Preflight)
```

- **CI:** `wait_kind=CI`; persistiert werden Repo/PR/Run/Check-Refs, **erwarteter Commit-SHA**, erlaubte Checks. CI für falschen SHA ist stale und bewirkt nichts. pending/queued ist kein Fehler und erzeugt **keine LLM-Polling-Schleife**. Roter Check → erst Logklassifikation → ggf. Code-Rework. Grüner Check kann Closing Gate auslösen, setzt aber nie direkt DONE.
- Ein CI-/externes Event darf den Job wecken, aber nie Code schreiben, Approvals erteilen oder DONE setzen.

## 9. Resource Governor (Phase C)

**Resource Classes** (Ceilings, keine garantierten Zuteilungen):

| Klasse | MemoryHigh | MemoryMax (Ceiling) | MemorySwapMax | CPUQuota | Default Timeout | Parallelität |
|---|---:|---:|---:|---:|---:|---|
| LIGHT | 768 MiB | 1 GiB | 256 MiB | 100 % | 15 min | initial max. 2 |
| MEDIUM | 2 GiB | 2.5 GiB | 512 MiB | 200 % | 45 min | initial max. 1 |
| HEAVY | 3 GiB | 4 GiB | 1 GiB | 300 % | 120 min | max. 1 |
| EXCLUSIVE | 4.5 GiB | 5.5 GiB | 1.5 GiB | 400 % | step-spezifisch | Host exklusiv |

**Effektives MemoryMax:**

```text
MemoryMax_eff = min(class_ceiling, MemAvailable - required_host_reserve)
required_host_reserve = max(1.5 GiB, 20 % Gesamt-RAM)
```

- Kein Job wird gestartet, wenn dadurch die Reserve nicht erhalten bleibt (Admission verweigert/verschoben → Job bleibt QUEUED).
- Preflight vor Spawn und vor jedem schweren Substep: MemAvailable, Swap (≥70 % Vorsicht, ≥85 % keine MEDIUM/HEAVY), Workspace-Disk (≥10 GiB/15 %), `/tmp`-Typ und Free Space, CPU-Load, laufende Argent-cgroups, schwere Fremdprozesse.
- **EXCLUSIVE lokale Full Regression auf diesem Host vermeiden**, wenn dieselbe autoritative Prüfung über externe CI möglich ist.
- `/tmp`-Regel: Repos, `node_modules`, Package Stores, Build-Caches und große Artefakte gehören in persistenten Workspace/Cache; `/tmp` nur für kleine bounded Dateien.
- Failure-Klassifikation: OOM/cgroup-Event/ENOSPC/Pressure-Abbruch/Governor-Termination → `RESOURCE`; Assertion/Compiler bei normalem Exit → `DETERMINISTIC`; Timeout ohne Pressure → `TRANSIENT`/`DETERMINISTIC_STALL`; CI unavailable → `EXTERNAL`.
- **`RESOURCE` ist niemals `CODE_FAILURE`.** Bei RESOURCE: kein identischer Sofort-Retry; Messdaten persistieren; max. eine kontrollierte Alternative (kleinere targeted Auswahl, höhere Klasse nur bei bewiesener Hostreserve, externe CI); sonst `BLOCKED/LOCAL_CAPACITY_INSUFFICIENT`. Keine automatische Limit-Erhöhung.
- Resource-Messdaten (Phase C): nur bounded/latest oder aggregierte Samples.

## 10. Adaptive Roles (Phase E)

Rollen (Lead/Analyst/Implementer/QA/Reviewer) bleiben Fähigkeiten; aktiviert wird der kleinste sichere Plan:

| Jobklasse | Rollenplan |
|---|---|
| trivial | Supervisor-Lead, Implementer, targeted Verification |
| small | Lead, Implementer, QA-Funktion; Reviewer nur bei Findings |
| normal | Lead, Analyst, Implementer, QA, optional Reviewer |
| complex | Lead, Analyst, Implementer, QA, unabhängiger Reviewer |
| security-critical | Sol-Lead (falls Architektur offen), Analyst Pro/Sol, Implementer Pro, QA, **mind. 1 unabhängiger hochwertiger Sol-Closing-Review**, Owner-Gate falls sensibel |

Regeln: Lead ist Fähigkeit (Routineplanung = Flash-Supervisor); Analyst nur wenn Root Cause nicht beweiskräftig; QA bei Verhalten/Regression/mehreren Pfaden/relevantem CI-Kontext; Reviewer immer writer-unabhängig; Closing-Review schreibt nichts; Rollenplan nie durch Agent-Output erweiterbar; Planänderung mit reason code + `facts_version` persistiert.

## 11. Model Routing (Phase E)

- **Flash:** Supervisor, Queue/Status, Reconciliation, CI-/Wait-Auswertung, Context-Pack-Erstellung, einfache Analyse, mechanische/triviale Mini-Fixes.
- **Pro = DEFAULT WRITER für normale Codearbeit:** Implementierung, Multi-File, Debugging, Refactoring, robuste Tests.
- **Sol:** Architektur, Security, schwieriger Root Cause (nach unzureichendem Flash/Pro), wichtige unabhängige Closing Reviews.
- **Security Critical:** mindestens EIN unabhängiger hochwertiger Sol-Closing-Review ist verpflichtend. Zusätzlicher Sol-Planungsreview nur, wenn die Security-Architektur selbst noch entschieden werden muss. **Keine Pflicht-Sol am Anfang UND Ende jeder security-relevanten mechanischen Änderung.**
- Eskalationspfad: Flash → verifizieren → Pro nur bei Capability-Gap → verifizieren → Sol nur bei hartem Problem/Review-Pflicht. Keine Eskalation wegen Wait, Resource-Limit, fehlendem CI-Ergebnis, Agent-Selbsteinschätzung, einer ersten reparierbaren Formatabweichung.
- Persistieren: requested/actual provider/model, routing reason, escalation source, grobe Token-/Kostenklasse.

## 12. Context Engineering (Phase D)

- **Immutable Context Pack** pro Dispatch (Manifest): Task-Contract, Rolle, Policy-Versionen, Job-State + `facts_version`, `base_commit` + `diff_hash`, relevante Datei-Refs, confirmed findings, offene Fragen, Write-Scope, Testplan + Ziel-CI-Shard, Resource Class, Artifact-Refs, Output-Schema, Budget.
- **Soft Defaults:** Flash-Koordination 4–8k; Pro-Implementierung 12–24k; Sol-Architektur/Review 24–48k Token.
- **Hard Expansion nur mit persistiertem reason code:** Flash bis 16k, Pro bis 48k, Sol bis 96k.
- Budgetüberschreitung: **Retrieval → Artifact Ref → bounded Summary. Nie automatisch größere Kontexte. Kein History-Dump.**
- Retrieval-Policy: nur taskreferenziert, testberührt, diff-geändert, Dependency/Caller, bestätigtes Finding, Ziel-CI-Konfig, relevante Policy/Security-Regel.
- Faktenklassen: `OWNER_REQUIREMENT | POLICY | LEDGER_FACT | REPO_EVIDENCE | TEST_EVIDENCE | AGENT_CLAIM_UNVERIFIED | EXTERNAL_OBSERVATION_UNTRUSTED`.
- Invalidation: Pack ungültig bei Änderung von facts_version, Base-/HEAD-Commit, Diff-Hash, Worktree-Identity, Policy-Version, Rollenplan, akzeptierten Findings, Testplan/CI-Target, Gate-Scope. Stale Pack wird fail-closed abgewiesen.
- Checkpoints nach Rollenabschluss (bounded): bewiesen/geändert/getestet, offene Findings, Artifact-Refs, nächster sicherer Schritt. Keine CoT, keine Rohprompts.

## 13. Teststrategie (Phase F)

Stufen: **targeted → relevanter Integration/CI-Shard → größere Regression → Full Regression nur am Closing Gate** (bevorzugt externe CI bei ungeeigneter lokaler Hardware). Root-local PASS ersetzt keinen Extension-/Ziel-Shard-PASS.

- Pflichtartefakt `test_plan`: geänderte Komponenten, gezielte Tests, relevanter CI-Kontext, Resource Class, max. lokale Laufzeit, Closing Regression, autoritative externe Checks (taskgebundenes Repo, allowlistete Checks/Refs, nur read-only).
- Resultatklassen: `PASS | CODE_FAILURE | TEST_INFRA_FAILURE | RESOURCE_LIMIT | EXTERNAL_PENDING | EXTERNAL_FAILURE | SECURITY_BLOCK`. Nur `CODE_FAILURE` autorisiert fachliches Rework.
- Recovery-/Architektur-Testmatrix (Pflicht): Lease Expiry/Fencing, Dual-Supervisor-Claim, Supervisor-Tod bei read-only Agent und bei Writer, Boot-ID-Wechsel, External Wait über Restart, CI-Event-Duplikat, OOM/ENOSPC/Timeout, Worktree-Divergenz, stale Context Pack, DONE-Stickiness, Gate-/Notification-Dedup, Visualizer read-only.

## 14. Worktree-/Git Lifecycle (Phase B minimal, Phase I voll)

- Invariante: 1 Job = 1 Writer-Worktree = max. 1 gültige Writer-Lease. **Global bis Phase I maximal ein Writer.**
- Naming: `argent/<short-job-id>/<sanitized-purpose>`, Pfad `/home/pc/projects/argent-worktrees/<short-job-id>`; Repo-Identity + Base Commit kanonisch aufgelöst und persistiert.
- Erstellung nur durch Supervisor/Worktree Manager; expliziter Base Commit; unbekannter Dirty State → kein Claim.
- Writer-Lease gebunden an job_id, worktree_id, dispatch_id, lease_epoch, owner_instance_id; WorkspaceBroker prüft Writer-Bindung.
- Commit-Policy: **lokale Commits autonom im genehmigten Job-Scope erlaubt**, wenn Tests/Policy erfüllt; Commit-Message/-Scope aus Supervisorvertrag; Push/Merge/Stable/extern nur nach vorhandener Policy bzw. Owner-Gate.
- Abandoned-Recovery: Prozess/Lease/`git status`/HEAD/Base/Journal-Hash read-only prüfen; konsistent-terminal → `CLEANUP_PENDING`; dirty aber eindeutig job-owned → behalten; divergent/mehrdeutig → `BLOCKED`, nichts überschreiben. **Dirty/ambiguous Worktrees niemals automatisch löschen.**
- Merge Queue erst Phase I: seriell, Rebase/Merge in isoliertem Integrations-Worktree, relevante Tests erneut, Konflikt zurück an denselben Job, **anfangs Owner-gated**.

## 15. Background Betrieb (Phase G)

- Ein unprivilegierter **systemd-user-Service**; Installation/Aktivierung benötigt zu diesem Zeitpunkt ein **explizites Owner-Gate**.
- Singleton-Scheduler-Lease; kurze bounded Reconcile-Passes; Agenten detached starten + Prozessregistrierung; TUI/Telegram/Visualizer-Ausfall wirkungsneutral; nach Reboot boot_id-Reconciliation; kein Blind-Respawn.

## 16. Trust Boundaries

- UNTRUSTED-DATA-Regel gilt für alle neuen Pfade: QUEUED nur durch authentifizierten lokalen Create/Recovery; RUNNING nur Scheduler-Lease+Policy+Admission (Agent-Text darf keinen eigenen Spawn/Modellwechsel auslösen); WAITING_EXTERNAL-Payload liefert Status, nie Shell/Writes/Approval/Scope; OWNER_GATE nur Approval-Ledger (Wait-/CI-/Agent-Events erzeugen/approven/consumen nie Gates); LOST fail-closed; DONE nur Core-DONE + Closing-Invarianten (CI-grün/Telegram-Callback/Agent-Behauptung/Visualizerstatus allein reichen nie).
- Resource Governor: Limits aus lokaler Policy; Agent kann cgroups nicht erhöhen; Tooldiagnostik ist Daten, nicht Autorisierung.
- Worktree Manager: Agent bestimmt Pfad/Repo/Branch nicht frei; kanonische Pfade aus Jobvertrag; keine Symlink-/Path-Traversal-Erweiterung.
- Context Router: untrusted Inhalte behalten Herkunftsklasse; keine externen Texte als Policy; Secrets/verbotene Felder ausgeschlossen.
- External Watcher: enges read-only Provider-Interface, keine Webhook-to-command-Brücke, Event-Dedup + SHA-Bindung, kein zweiter Telegram-Poller.
- Background Service: keine neuen Privilegien, kein Root, keine freie Shell-/Secret-Schnittstelle.
- **Routing-Policy (`routing.py` wird Policy-Code): versioniert, im Dispatch persistiert** — sicherheitsrelevant, da Modell-/Rollenrechte die Dispatch-Provenance beeinflussen.

## 17. Storage-Konvention

```text
Worktrees : /home/pc/projects/argent-worktrees/<short-job-id>
State     : ~/.local/state/argent/
Artifacts : ~/.local/share/argent/
Cache     : ~/.cache/argent/
/tmp      : nur kleine bounded Dateien (kein Repo, kein node_modules, kein Store)
```

## 18. Notification Policy

- Telegram nur für: **GATE, ERROR, DONE**. Normales Queuing/Running/Retry/CI-pending/Recovery-Fortschritt erzeugt **keine** Meldung. BLOCKED/FAILED/PERSISTENT_ERROR werden unter ERROR mit allowlisted reason code differenziert.
- Feste Plaintext-Templates (SPEC V3A) bleiben; keine Links, kein parse_mode.
- **Telegram B2B:** erst nach autoritativ verfügbarer Upstream-Unterstützung (`updateId`/`messageDate`), Compatibility Guard und separatem Live-Test-/Aktivierungs-Gate; kein zweiter Poller; Approval bleibt Permission, nicht Execution.
- **Visualizer:** Lease-, Resource-, Wait- und Cost-Felder dürfen später read-only ergänzt werden; Closed Allowlist und Secret Canaries bleiben Pflicht; kein Control-Pfad.

## 19. Durable Queue / Schema-Entscheidung

- **Phase B prüft zuerst, ob die bestehende `supervisor_jobs`-Struktur additiv zur Durable Queue erweitert werden kann** (Job-Felder aus §5 direkt am Job). Eine separate `job_queue`-Tabelle wird **nur** eingeführt, wenn ein konkreter Schema-/Compatibility-Grund dies verhindert (dann mit dokumentiertem Befund).
- **Keine separate `job_leases`-Tabelle in V1.** Lease-Felder direkt am Job.
- Action-Journal bleibt unverändert als Audit-/Crash-Evidence.
- Tabellen-Plan (phasenweise, keine vorzeitige Enterprise-Infrastruktur):
  - **Phase B:** Durable Job-/Queue-Felder, minimales `process_registry`, `external_waits`, minimale Worktree-/Writer-Bindung.
  - **Phase C:** Resource-Messdaten, nur bounded und nur soweit nötig.
  - **Phase D:** `context_packs`.
  - **Phase I:** vollständige Worktree Registry + Merge-Queue-Funktionen.
- Alle Schemaänderungen transaktional, additiv, mit Migration-/Rollback-Crash-Tests.

## 20. Implementierungsphasen A–L (verbindliche Reihenfolge)

| Phase | Scope | Exit-Kriterien (Kurzform) |
|---|---|---|
| **A Architecture Freeze** | Diese Spec als Vertrag; State-/Transition-Tabelle; Schema-/Mapping-Plan; Failure-/Crash-Matrix | kein Code begonnen; V2C/V3-Invarianten gemappt; unabhängiger Review abgeschlossen |
| **B Durable Supervisor Core** | Durable Queue/Lease/Fencing auf `supervisor_jobs` (additiv), External Wait Core, minimales `process_registry`, minimale Worktree-Ownership, Error Taxonomy | nie 2 gültige Owner; alter Lease-Holder kann nicht committen; Einzeljob über Stunden mit simulierten Crashes; Waits blockieren keinen LLM-Agenten |
| **C Resource Governor** | Klassen, Preflight, Admission, cgroup/systemd-scope-Ceilings, RESOURCE-Klassifikation, bounded Samples | Heavy-Test kann Host nicht erschöpfen; RESOURCE nie als CODE_FAILURE; `/tmp`-Policy erzwungen; verwaiste Scopes erkannt |
| **D Context Engineering** | immutable Context Packs, Budgets, Retrieval, Invalidation, Artifact-Handoff | kein History-Dump; stale Pack fail-closed; Security-/Privacy-Tests; messbare Context-Reduktion |
| **E Adaptive Roles + Model Routing** | persistierte Rollenpläne, Complexity/Risk-Classifier, Flash-first, begründete Eskalation | triviale Jobs ohne 5-Agenten-Pflicht; Routine Lead/QA = Flash; Sol nur allowlisted; Closing Reviewer unabhängig |
| **F Test Economy** | `test_plan`-Artefakt, Stufenmodell, CI-Kontext-Bindung, Resultatklassen | Root-PASS ersetzt Shard nicht; Evidence SHA-gebunden; Full Regression nur Closing Gate |
| **G Background Operation / Reboot Recovery** | systemd-user-Service (Owner-Gate!), Boot-ID-Reconciliation, Singleton-Lease | TUI schließbar, Job läuft weiter; Reboot → deterministische Reconciliation; kein Blind-Respawn |
| **H Telegram Owner Approval B2B** | erst bei autoritativem Upstream, Compatibility Guard, Live-Test-Gate | Metadaten unverfälscht; Callback außerhalb Agent-Textpfad; exactly-once; Approval ≠ Execution |
| **I Controlled Parallelization + Worktrees + Merge Queue** | volle Worktree Registry, Writer-Lease, wenige read-only Jobs, serielle owner-gated Merge Queue | nie 2 Writer/Worktree; global max. 1 Writer; Konflikte fail-closed; Stabilitätsziele B–H nachgewiesen |
| **J Productive Autonomous E2E** | reale kleine/normale Tasks, Lauf-/Wait-/Crash-/Reboot-Szenarien, Kosten-/Ressourcenmessung | mehrere Tasks bis DONE ohne Babysitting; kein Ghost Writer; kein teurer Agent im Wait; kein WSL-Stall |
| **K Optional Specialists** | evidenzbasiert, z. B. Security/Performance/Dependency Specialist | belegte Verbesserung, kein Rollen-Sprawl, keine gefährlichen Tools ohne Owner-Gate |
| **L Owner-gated Self-Improvement** | Proposal → isolated implementation → tests → independent review → Owner Gate → adoption | **standardmäßig DISABLED**; nur nach separater expliziter Owner-Aktivierung; Stable-Tags/Policies nie automatisch überschrieben |

## 21. Failure/Crash Matrix

| Ereignis | Klassifikation | Recovery | Retry |
|---|---|---|---|
| OOM/cgroup-limit/ENOSPC/Pressure | `RESOURCE` | Job → QUEUED (nach Preflight), Messdaten persistieren | kein identischer Sofort-Retry; max. 1 kontrollierte Alternative; sonst BLOCKED/LOCAL_CAPACITY_INSUFFICIENT |
| Test-Assertion/Compiler-Fehler (normaler Exit) | `DETERMINISTIC` | Findings + ggf. Rework | nach Policy, attempt_no begrenzt |
| Timeout ohne Pressure | `TRANSIENT`/`DETERMINISTIC_STALL` | Job → QUEUED, Toolprofil prüfen | mit Backoff, begrenzt |
| CI/Upstream nicht verfügbar | `EXTERNAL` | WAITING_EXTERNAL (wait_kind=CI/UPSTREAM), Watcher | Event-/Deadline-basiert, kein LLM-Polling |
| Agent-Crash | – | Prozess terminal belegen; Writer-Worktree sichern; Dispatch binden | nach Fehlerklasse + attempt_no |
| WSL-Reboot/PC-Reboot | – | boot_id-Wechsel ⇒ Prozesse tot, Leases verfallen, Writer → LOST → bounded reconcile | nach globaler Reconciliation, erst dann Admission |
| Writer-/Ownership-Ambiguität | `OWNER_REQUIRED` | LOST → bounded reconcile → BLOCKED/AMBIGUOUS_WRITER | nur Owner-/Policy-Autorisierung |
| Security-Verdacht | `SECURITY` | fail-closed, kein Retry, kein Rework | nur Owner-Entscheidung |
| DONE-Stickiness verletzt | – | unmöglich (invariant); Verstoß = Bug | – |

## 22. Mapping bestehender V1–V3D-Invarianten

| Bestehende Invariante (Quelle) | Mapping in V1-Final |
|---|---|
| SQLite-Ledger, `BEGIN IMMEDIATE`, CAS, Unique Constraints (V1/V2) | KEEP unverändert; additiv erweitert |
| TaskState-Workflow fail-closed (V1/V2) | KEEP; Job-State-Machine ist Betriebsprojektion, keine zweite fachliche Maschine |
| Trust Boundary, UNTRUSTED DATA (V2B) | KEEP; auf alle neuen Zustände/Komponenten ausgeweitet (§16) |
| Write-Broker als einziger Write-Pfad, Rollenrechte (V2B) | KEEP; zusätzlich Writer-Lease-Prüfung (§6/§14) |
| bwrap-Test-Runner read-only (V2B) | KEEP; Resource-Governor umschließt Ausführung (§9) |
| SupervisorJobStatus `ACTIVE/W AITING_RUN/WAITING_GATE/BACKOFF/RECOVERING/ERROR/TERMINAL` (V2C) | ACTIVE→RUNNING; WAITING_RUN→QUEUED; WAITING_GATE→OWNER_GATE; BACKOFF→QUEUED+RETRY_BACKOFF; RECOVERING→QUEUED+recovery_phase (bzw. LOST); ERROR→BLOCKED/FAILED nach Klassifikation; TERMINAL→DONE/FAILED |
| Action-Journal, Runtime-Provenance, Dispatch-CAS (V2C) | KEEP als Audit-/Crash-Evidence + Fencing-Grundlage (§6) |
| `PRESENT_OWNER_GATE`/`WAITING_GATE`-Semantik, Gate-Closure atomar (V2C) | OWNER_GATE + §4-Transitionen; Approval bleibt Permission ≠ Execution |
| DONE-Stickiness, No-Background-Wake (V2C) | KEEP; DONE immutable (§3); Waits reaktivieren über QUEUED, nie direkt DONE (V3A/§8) |
| Worker-Erfolgsmeldungen ≠ Abnahme (V2C §11) | KEEP; DONE nur Core-DONE + Closing-Invarianten (§16) |
| Telegram Outbox: outbound-only, dedup, feste Templates, non-blocking (V3A) | KEEP; Notification-Policy GATE/ERROR/DONE (§18) |
| Telegram Approval: Inline-Buttons, Challenge, kein Text-Parser (V3C) | KEEP; B2B erst mit Upstream-Metadaten + Guard + Gate (Phase H) |
| Visualizer read-only, Closed Allowlist, Secret Canaries (3D) | KEEP; Feld-Erweiterung nur allowlist-basiert, read-only (§18) |
| `/tmp`-Disziplin, Ressourcen-Learnings (Workspace-Memory) | Verbindlich im Resource Governor (§9) |

## 23. Echte offene Punkte

1. **`supervisor_jobs`-Upgrade:** konkreter additiver Migrationsplan (Felder, Indexe, Unique-Constraints) wird in Phase B zuerst gegen die reale Schema-Struktur geprüft; separate `job_queue` nur bei dokumentiertem Schema-Grund.
2. **`external_waits`-Allowlist:** konkrete erlaubte Repos/Checks pro Task werden mit dem `test_plan` je Projekt festgelegt (Owner-/Policy-Freigabe, read-only).
3. **Phase-G-Service-Detail:** exakte systemd-user-Unit (Restart-Policy, Environment, Pfade) wird erst bei Phase G mit Owner-Gate erstellt.
4. **Outbox-Template-Erweiterung:** falls neue reason codes in ERROR-Templates nötig werden, ist das eine kleine, separat reviewte Template-Erweiterung (Phase F/E); kein freier Text.
5. **Memory-Index des OpenClaw-Workspace** ist derzeit nicht verfügbar (Index-Metadaten fehlen) — betrifft nicht die Architektur, aber die Betriebs-Observability; Reindex ist Owner-/Ops-Aufgabe.

---

*Ende ARGENT ARCHITECTURE V1 FINAL. Verantwortlich: Supervisor (Argent). Kein Commit/Push; Datei untracked.*
