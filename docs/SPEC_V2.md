# ARGENT DEVELOPMENT TEAM — Phase 2A: Orchestrierung & Provenance (SPEC V2)

Basis: Core V1 (SPEC V1–V1.3, CORE_GREEN, Commit 83d91c2, 595 Tests grün).
Ziel: Verbindung des deterministischen Cores mit echten, isolierten OpenClaw-Agent-Runs
und harte Run-/Session-/Handoff-Provenance.

**Leitprinzip: Der bestehende deterministische Core bleibt die EINZIGE Autoritätsinstanz.**
LLM-Agenten liefern Empfehlungen/Ergebnisse (DATA). Sie können State Machine, Owner Gates,
Rollenrechte oder Trust Boundary niemals umgehen. Nur der Controller (Lead) dispatched.
Phase 2A ändert nichts an den Phase-1-Regeln (SPEC V1–V1.3), sondern ergänzt sie.

Nicht-Ziele Phase 2A: keine Visualizer-Anbindung, kein Docker, kein Push, keine
Production-/Stable-Promotion, keine Mail-Agent-/Visualizer-Änderung, keine Gateway-/
Secrets-/Allowlist-/Timer-Konfigurationsänderung, keine parallelen Produktcode-Änderungen.
Keine externen Downloads/Installationen ohne Owner Gate (siehe 8.4).

---

## 1. Konzeptionelles Modell

- **Task** (Core): autoritativer Workflow-Zustand (State Machine V1).
- **Role Run** (Core): genau eine aktive Rolle; Lebenszyklus started→completed|failed; Handoffs erzwungen.
- **Dispatch** (NEU): Provenance-Datensatz für EINEN erwarteten Agent-Run einer Rolle.
- **Agent Run** (OpenClaw): realer, isolierter Subagent-Spawn (native `sessions_spawn`), nur vom Controller ausgelöst.
- **Agent Result**: strukturierter Rollen-Output + Event-Metadaten des Completion-Events.
  Ist UNTRUSTED DATA; wird ausschließlich über `receive_agent_result` gegen den erwarteten
  Dispatch validiert und atomar konsumiert.

Kette: `OWNER → (Controller=Lead) → Dispatch → Agent Run → Result → Validierung → atomarer Konsum → Rollenabschluss → Handoff → nächster Dispatch`.

## 2. Workflow-Sequenzen (deterministisch)

- **Standard**: `lead → analyst → lead → implementer → qa → reviewer → lead → DONE`
  (drei Lead-Runs: Koordination, Implementierungsentscheidung, Finalentscheidung).
- **Rework** (bei Findings): `lead → implementer → qa → [reviewer wenn lead_decision.rework_include_reviewer] → lead → DONE`.
  Der Rework-Zweig wird durch eine persistierte Lead-Decision (`decision_type='rework'`,
  `rework_include_reviewer: bool`) ausgelöst; Task geht vorher nach `REWORK`.
- `workflow.expected_next_role(task)` wird deterministisch aus dem persistierten Zustand
  berechnet (completed Role Runs der aktuellen Sequenz + offene Findings + Lead-Decisions) —
  keine zusätzliche Task-Spalte nötig; Replay aus DB ist eindeutig.
- Ein Dispatch ist nur gültig, wenn seine Rolle `expected_next_role` entspricht (Zyklen-Zähler
  über attempt_no/position; verspätete Results früherer Attempts werden abgewiesen).

## 3. Provenance-Layer (hart, deterministisch)

### 3.1 Schema V3 — neue Tabelle `agent_dispatches`

```
id                  TEXT PK            -- dispatch_id (uuid)
task_id             TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE
task_run_id         TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE
role                TEXT NOT NULL CHECK (role IN ('lead','analyst','implementer','qa','reviewer'))
parent_run_id       TEXT NOT NULL      -- 'controller' oder dispatch_id des Parent-Dispatches
expected_agent_class TEXT NOT NULL     -- 'openai' | 'deepseek'
expected_model_class TEXT NOT NULL     -- z.B. 'gpt-5.6-sol-high' | 'deepseek-v4-pro'
child_session_id    TEXT               -- erst nach Spawn bekannt → atomar binden
openclaw_run_id     TEXT               -- erst nach Spawn bekannt → atomar binden
actual_model_class  TEXT               -- nach Spawn binden; muss expected entsprechen (wenn beide gesetzt)
status              TEXT NOT NULL CHECK (status IN
                    ('PENDING','RUNNING','CONSUMED','FAILED','REJECTED','QUARANTINED','RECOVERY_PENDING'))
attempt_no          INTEGER NOT NULL DEFAULT 1
handoff_id          TEXT               -- Handoff, den dieser Run erfüllt (erwarteter Handoff)
result_json         TEXT               -- strukturierter Output (erst bei Konsum)
created_at          TEXT NOT NULL
started_at          TEXT
consumed_at         TEXT
UNIQUE (task_id, role, attempt_no)
```

Neue Tabelle `agent_result_quarantine` (immutables Log):
`id PK, task_id, dispatch_id, reason, event_meta_json (SANITIZED: nur Metadaten, nie Prompts/Code), created_at`.

Weitere Schema-Änderungen: `tasks.risk_class TEXT NOT NULL DEFAULT 'NORMAL' CHECK (risk_class IN ('LOW','NORMAL','HIGH'))`;
`tasks.external_actions_policy TEXT NOT NULL DEFAULT 'ALLOWED_WITH_GATE' CHECK (external_actions_policy IN ('ALLOWED_WITH_GATE','FORBIDDEN'))`.

### 3.2 Dispatch-Lebenszyklus

1. `create_dispatch(task_id, task_run_id, role, parent_run_id, model_choice, source)` —
   NUR mit `source='role:lead'` (Controller). Validierung:
   - aktiver Role-Run für `role` existiert (Lead hat die Rolle bereits gestartet),
   - `role == workflow.expected_next_role(task)` (Sequenzkonformität),
   - kein anderer Dispatch des Tasks in `PENDING|RUNNING|RECOVERY_PENDING` (genau ein aktiver Run),
   - `routing.validate_model_choice(role, model_choice, task.risk_class)` (sonst `policy.role_violation`),
   - Task nicht terminal/pausiert.
   Status `PENDING`; Events `agent.dispatch_created`, `handoff.expected`.
2. `bind_child_session(dispatch_id, child_session_id, actual_model_class, source)` — atomar;
   nur `PENDING→RUNNING`; Session darf noch nicht an einen anderen Dispatch gebunden sein
   (fail-closed bei Konflikt); Event `agent.started`. IDs werden NIE geraten — nur gebunden,
   was der Spawn tatsächlich zurückgibt; ist die Bindung nicht eindeutig → fail-closed.
3. `bind_run_id(dispatch_id, openclaw_run_id, source)` — analog atomar (falls separat geliefert).
4. `receive_agent_result(dispatch_id, event_meta, result, source)` — siehe 3.3.
5. `mark_agent_failed(dispatch_id, reason, source)` — nur Controller; `RUNNING|RECOVERY_PENDING → FAILED`;
   Role-Run → failed; Handoff bleibt offen; Events `agent.failed`, `role.failed`.
6. `quarantine(...)` intern: `QUARANTINED` + Log-Zeile; KEIN State-Change, KEINE Folgeaktion.

### 3.3 Result-Validierung (alle Bedingungen, fail-closed)

Ein Ergebnis wird NUR verarbeitet, wenn ALLE gelten:

- `dispatch_id` existiert (sonst UNEXPECTED_AGENT_RESULT → Quarantäne `dispatch_unknown`),
- `event_meta.task_id` (falls vorhanden) == `dispatch.task_id` (sonst Quarantäne `task_mismatch`),
- `dispatch.task_run_id` == aktueller Task-Run des Tasks (`run_mismatch`/`stale_run`),
- `dispatch.role` == aktive Rolle des Role-Runs des Tasks (`role_mismatch`),
- `event_meta.child_session_id` (falls vorhanden) == `dispatch.child_session_id` (`session_mismatch`),
- `event_meta.run_id` (falls vorhanden) == `dispatch.openclaw_run_id` (`run_id_mismatch`),
- `dispatch.parent_run_id` == erwarteter Controller-Parent (`parent_mismatch`),
- `dispatch.status in (PENDING, RUNNING, RECOVERY_PENDING)`:
  - `CONSUMED` → Duplikat: `agent.result_duplicate`, idempotent ignorieren (kein State-Change),
  - `FAILED|REJECTED|QUARANTINED` → verspätet/alt → Quarantäne `stale_dispatch`,
- `dispatch.handoff_id` == aktuell erwarteter offener Handoff des Tasks (`handoff_mismatch`),
- strukturierter Output valid (Schema je Rolle, siehe 5; sonst `REJECTED`, Grund `malformed_output`,
  KEIN State-Change, KEINE Folge-Rolle — Controller muss neu dispatchen),
- Task nicht terminal (`task_ended` → Quarantäne).

Bei jeder Abweichung: UNEXPECTED_AGENT_RESULT → Quarantäne/`rejected_result`, Event
`agent.result_rejected`, **kein** State-Change, **keine** neue Rolle, **keine** Codeänderung,
**kein** Owner-Approval, **keine** Policy-Änderung.

### 3.4 Atomarer Konsum

Bei vollständiger Validierung in EINER `BEGIN IMMEDIATE`-Transaktion:
`status → CONSUMED`, `result_json`, `consumed_at`; Events `agent.result_received`,
`agent.result_accepted`, `agent.completed`; Rollen-Effekte anwenden:
- findings[] → `findings`-Tabelle (`finding.created`),
- lead: decision → `decisions` (`lead.decision`), accepted/rejected findings,
- qa: tests/failures → `test_runs` (`test.started`/`test.completed`),
- reviewer: verdict → `reviews` (`review.started`/`review.completed`),
- Role-Run `completed` (`role.completed`), Handoff zum nächsten Sequenz-Rollenstart
  (`handoff.created` + `handoff.accepted`).
Doppelte Zustellung desselben gültigen Runs nach Konsum → idempotent (`agent.result_duplicate`),
weil `CONSUMED`-Check + UNIQUE(task_id, role, attempt_no) jede Doppelanwendung ausschließen.

## 4. Context-Isolation

`context.build_agent_context(task, role, position)` liefert die Sicht je Rolle
(deterministisch aus Core-Daten: Findings, Decisions, TestRuns, Reviews, Diff-Summary-Metadaten;
die Prompt-Formulierung liegt beim Controller, die Datenauswahl ist Core-Aufgabe):

- **lead**: Owner-Anforderung (task.title + freigegebene Beschreibung), relevante Projektregeln,
  aktueller sicherer Task-Zustand, strukturierte Ergebnisse vorheriger Rollen soweit nötig.
- **analyst**: Originalanforderung, relevante Dateien/Repo-Zustand (Namen/Status), Problem-/
  Reproduktionskontext. NIE die gewünschte Lösung des Implementers (existiert in Std.-Reihenfolge noch nicht;
  Test: Analyst-Kontext enthält keine Implementer-Felder).
- **implementer**: freigegebene Lead-Entscheidung, bestätigte Analyst-Findings, exakter Scope, Write-Policy.
- **qa**: Originalanforderung, akzeptierte Lead-Entscheidung, aktueller Diff (Metadaten: changed_files),
  Testanforderungen; darf eigene Gegenbeispiele entwickeln.
- **reviewer**: Originalanforderung, Sicherheits-/Architekturregeln, aktueller Diff (Metadaten),
  finaler Repo-Zustand (Zusammenfassung), Testergebnisse. **KEINE** Implementer-Selbsteinschätzung
  (`own_assessment`/`proposal` des Implementers wird NICHT in den Reviewer-Kontext übernommen),
  möglichst keine unnötigen vorherigen Agentenschlussfolgerungen. (Test: Context-Minimization.)

## 5. Strukturierte Rollen-Outputs (fail-closed)

`outputs.validate_role_output(role, result: dict)` — Pflichtfelder + Typen; unbekannte Rollen
oder fehlende/falsch typisierte Pflichtfelder → `OutputValidationError` (malformed → rejected).
Gemeinsame Pflichtfelder: `role, task_id, dispatch_id, status, findings[], own_assessment,
concerns[], proposal, alternatives[], confidence, blockers[], requested_next_state`.
Zusätzlich je Rolle:
- **lead**: `decision` (accept|rework|cancel|request_owner_gate), `accepted_findings[]`, `rejected_findings[]`, `rationale`
- **analyst**: `reproduction`, `root_cause`, `evidence_refs[]`
- **implementer**: `changed_files[]`, `implementation_summary`, `tests_run[]`
- **qa**: `tests[]`, `failures[]`, `regressions[]`, `coverage_concerns[]`
- **reviewer**: `severity`, `security_findings[]`, `architecture_findings[]`, `recommendation`

Privacy: kein Chain-of-Thought, keine Secrets, kein vollständiger Sourcecode; Denylist-Scan
(Prompt/CoT/Secrets/Mail/Code-Begriffe) über alle Output-Felder → Verstoß = malformed/rejected.
`confidence` als Zahl 0..1; `status` in (`ok`,`findings`,`blocked`).

## 6. Modellrouting (deterministisch)

`routing.py` (Stand der read-only Modellprüfung, siehe §13.7 — KEINE Konfigänderung):
- **lead**: `openai` / `gpt-5.6-sol` (höchste verfügbare Sol-Stufe, „Sol High“).
- **analyst**: `deepseek` / `deepseek-v4-pro`.
- **implementer**: `deepseek` / `deepseek-v4-pro`; `deepseek-v4-flash` NUR für `risk_class='LOW'`
  (klar kleine, risikoarme Änderungen).
- **qa**: `deepseek` / `deepseek-v4-pro`; `deepseek-v4-flash` NUR für einfache deterministische Regressionen.
- **reviewer**: `openai` / `gpt-5.6-sol` (höchste verfügbare Stufe; „Sol Max“ existiert in Doku/Konfiguration
  nicht → Owner-Vorgabe: Sol High verwenden und im Abschlussbericht explizit dokumentieren).
- `validate_model_choice(role, model, risk_class)` → bei Verstoß `policy.role_violation`-Event + Ablehnung.
- Lead und Reviewer MÜSSEN unterschiedliche Sessions haben: jede Bindung einer bereits verwendeten
  `child_session_id` an einen anderen Dispatch → fail-closed.

## 7. Rollenrechte technisch erzwungen

Bestehende Phase-1-Matrix bleibt; technische Durchsetzung für Agent-Runs:
- **lead**: Produktcode read-only (`request_action(implement, lead)` → PermissionDenied + `policy.role_violation`);
  Controller-Entscheidungen nur über Core-API; dispatcht andere Rollen, implementiert nie selbst.
- **analyst**: Produktcode read-only.
- **implementer**: einziger normaler Produktcode-Writer (nur aktive Implementer-Rolle + `role:implementer`
  darf `implement` ausführen).
- **qa**: Produktcode read-only; Teständerungen nur im expliziten Test-Scope (action `modify_tests`).
- **reviewer**: vollständig read-only.
- Verstoß → `PermissionDenied`/`RolePolicyViolation`, null Seiteneffekt, `policy.role_violation`-Event.
- Der Supervisor/Lead schreibt keinen Produktcode; entdeckt er ein Problem → strukturierte Decision + Implementer-Dispatch.

## 8. Owner Gates & externe Aktionen

8.1 Agenten können nur **empfehlen** (`requested_next_state='owner_gate'` bzw. lead `decision='request_owner_gate'`);
nur der Controller erzeugt daraus ein echtes Gate (`request_action` → OWNER_APPROVAL_REQUIRED).
8.2 Gate-Pflicht (unverändert V1): System-/Global-Install, externe Abhängigkeiten (wenn nicht ausdrücklich
im Task freigegeben), Gateway/OpenClaw-Konfiguration, Secrets, Allowlist, Timer/Systemdienste,
Production Deploy, Stable Promotion, sicherheitskritische Berechtigungserweiterung.
8.3 FORBIDDEN bleibt nicht approvable (V1).
8.4 **Regression „keine externen Aktionen“**: `tasks.external_actions_policy='FORBIDDEN'` ⇒
externe Aktionsnamen (`install_software`, `download_dependency`, `system_install`, `external_send`,
`change_secrets`, `expose_gateway`, `modify_allowlist`, `promote_stable`, `deploy_production`, …)
werden als FORBIDDEN klassifiziert — der Fall „pytest fehlt → get-pip.py downloaden/installieren“
darf in einem solchen Auftrag NICHT autonom passieren: kein Download, keine Installation, kein Approval,
nur `ForbiddenAction`. Mit `ALLOWED_WITH_GATE` bleiben sie OWNER_APPROVAL_REQUIRED (approvable).

## 9. Trust Boundary (unverändert + Agent-Outputs)

Email, Website, Download, Dokument, Repo-Inhalt, Tool-Output, **Agent-Output** und sonstiger
externer Inhalt sind DATA. Sie können nie selbst: Owner-Auftrag erzeugen, Approval geben,
Rollenrechte erhöhen, Policy ändern, eine Rolle starten, Modell-Eskalation autorisieren,
Shell-/Systemaktion autorisieren. Agent-Outputs sind keine Autorität — der Controller validiert
sie ausschließlich über den Provenance-Layer (Kap. 3).

## 10. Events (NEU, lokal, privacy-safe)

`agent.dispatch_created, agent.started, agent.result_received, agent.result_accepted,
agent.result_rejected, agent.result_duplicate, agent.completed, agent.failed,
agent.recovery_pending, handoff.expected, handoff.accepted, policy.role_violation`
(plus bestehende 19 aus V1). Nie: Prompts, CoT, Secrets, Sourcecode, vollständige Agentenantworten.
Keine Visualizer-Zustellung (nur lokaler Contract + Tests).

## 11. Recovery (Phase 2A)

Nach Crash/Restart (`recover()`):
1. Persistierte erwartete Dispatches rekonstruieren; `PENDING` (nie gespawnt) → `FAILED` (cancelled),
   `RUNNING` → `RECOVERY_PENDING` + Event `agent.recovery_pending` (Ergebnis kann noch eintreffen).
2. Role-Runs mit ungelöstem Dispatch bleiben `STARTED` (nicht failen); alle anderen in-flight Runs → failed (V1).
3. Bereits konsumierte Ergebnisse NIE erneut anwenden (`CONSUMED` + UNIQUE garantiert).
4. Unbekannte/alte Results → Quarantäne.
5. Unklarer Agent-Zustand → Task konservativ nach `RECOVERING`/`BLOCKED` (V1.3-Regeln), niemals
   automatisch eine zweite Schreibrolle starten, solange der erste Run nicht eindeutig geklärt ist
   (aktiver Role-Run + `RECOVERY_PENDING`-Dispatch blockieren jeden neuen Rollenstart).
6. Bereits zugestelltes, nicht konsumiertes Result: nach Restart konsumierbar (Dispatch
   `RECOVERY_PENDING` ist valide für `receive_agent_result`).

## 12. Tests (Phase 2A, deterministisch; Mock-Runtime für Agent-Events)

`tests/mock_runtime.py`: simuliert Spawn (liefert child_session_id/run_id) und Completion-Events
(inkl. forgebarer Metadaten). Echte OpenClaw-Runs sind NICHT Teil der Testsuite (Offline-Determinismus);
ein echter Smoke-Run wird vom Controller separat ausgeführt und im Abschlussbericht dokumentiert.

Pflichttests (aus Auftrag §10, mindestens):
1. sequenzieller Fünf-Rollen-Workflow (Standard + Rework) end-to-end über Mock-Runtime
2. Controller ist einziger Dispatcher (`create_dispatch` mit Nicht-Lead-Source → PermissionDenied)
3. Rolle kann keine andere Rolle starten (start_role/complete_role-Pfad + Dispatch-Validierung)
4. genau eine aktive Rolle; 5. genau ein Produktcode-Writer (nur implementer)
6. Lead kann Produktcode nicht schreiben; 7. Analyst read-only; 8. QA nur Tests; 9. Reviewer read-only
10. getrennte Lead-/Reviewer-Kontexte (Context-Builder) + unterschiedliche child_session_ids erzwungen
11. erwarteter Run akzeptiert; 12.–17. falsche task_id/role/dispatch_id/session/parent/run-id → rejected+Quarantäne
18. alter Run rejected; 19. doppelte Completion idempotent; 20. Completion nach Task-Ende rejected
21. verspätetes Ergebnis eines vorherigen Attempts rejected; 22. unerwarteter Sol-Completion-Event → kein State-Change
23. Agent-Output kann kein Approval erzeugen; 24. Agent-Output kann keine Policy ändern
25. strukturierte Output-Validierung fail-closed; 26. malformed output rejected
27. Context-Minimization für Reviewer; 28. Owner Gate funktioniert (empfehlen → Controller erzeugt Gate → approve → execute)
29. „keine externen Aktionen“ blockiert Dependency-Download/Install (get-pip-Regression, ohne echten Download)
30. Recovery mit in-flight Agent-Run; 31. Neustart mit zugestelltem, nicht konsumiertem Result → konsumierbar
32. atomare Result-Consumption (Crash zwischen Validierung und Konsum → kein Teilzustand)
33. Privacy der neuen Events/Handoffs (Denylist-Scan inkl. Envelope; keine Agentenantworten)
34. Modellrouting: Flash-Limitierung (implementer LOW erlaubt Flash, NORMAL nicht; qa-Regel), role_violation-Event

## 13. Native OpenClaw-Mechanismen (read-only untersucht, Ergebnis)

Quellen: docs/ (tools/subagents.md, automation/tasks.md, gateway/config-agents.md,
concepts/managed-worktrees.md, gateway/sandbox-vs-tool-policy-vs-elevated.md,
cli/approvals.md, cli/audit.md, cli/models.md, providers/openai.md). Nichts verändert.

13.1 **Subagent-Sessions** (`sessions_spawn`): liefert SOFORT `{status, runId, childSessionKey}`;
`taskId` existiert im Spawn-Resultat nicht. Completion kommt als interner Push mit stabilem
Idempotenz-Key. → Provenance-Bindungen = `childSessionKey` + `runId` (genau die zwei Felder des
Dispatch-Records); IDs werden nie geraten, sondern nach Spawn atomar gebunden (SPEC §3.2).
13.2 **Tasks** (nativer Activity-Ledger): task_id/runId/childSessionKey, Status-Lebenszyklus;
`openclaw tasks show` read-only. → Komplementär zu unserem `openclaw_run_id`; keine Abhängigkeit.
13.3 **Agents**: persistente Agenten mit Modell/Thinking-Overrides; pro Run überschreibbar via
`sessions_spawn.model`/`thinking`. → Modellrouting wird beim Spawn erzwungen (Controller wählt Modell
aus `routing.resolve_model`; Dispatch speichert expected/actual).
13.4 **Worktrees**: native Managed Worktrees existieren (isolierte Repo-Checkouts). Phase 2A: NICHT
nötig („noch keine parallelen Produktcode-Änderungen“) → als Phase-2B-Kandidat dokumentiert.
13.5 **Sandbox/Tool-Permissions**: native Sandbox-Modi + Tool-Policy + Exec-Approvals existieren.
Phase 2A: keine Konfigänderung; Rollenrechte werden im Core technisch erzwungen (Kap. 7).
13.6 **Approvals/Audit**: nativer /approve-Flow + metadata-only Audit-Ledger. → Komplementär;
Owner Gates bleiben Domänen-Autorität des Cores.
13.7 **Modelle**: read-only `openclaw models list`; **„Sol Max“ existiert nicht** in Doku/Konfiguration;
höchste verfügbare Stufe = `openai/gpt-5.6-sol` (Flagship, xhigh/max Reasoning; weitere OpenAI-Modelle
`-terra`/`-luna`). → Reviewer/Lead = `openai/gpt-5.6-sol`, dokumentiert im Abschlussbericht.
13.8 **Idempotenz**: nativ nur punktuell (Completion-Idempotenz-Key, Media-Job-Dedup, Protocol-Keys).
→ Core-eigene Idempotenz (args_hash, UNIQUE-Constraints, CONSUMED-Check) bleibt nötig und maßgeblich.

## 14. Abnahmekriterien Phase 2A

- alle Tests grün, keine Deaktivierungen/Skips
- keine offenen HIGH/CRITICAL, keine unakzeptierten relevanten MEDIUM
- echte getrennte Agent-Runs funktionieren (Smoke-Run dokumentiert)
- Controller dispatched allein; Provenance technisch erzwungen
- unerwartete/duplizierte Completion-Events fail-closed
- Rollenrechte, Context-Isolation, Owner Gates, Trust Boundary, Recovery verifiziert
- keine Änderung an Mail-Agent/System-Visualizer; keine ungefragte System-/OpenClaw-Konfiguration
- lokaler Commit auf Branch `phase-2a-orchestration`, kein Push

Abschlussausgabe: `ARGENT DEVELOPMENT TEAM PHASE 2A — ORCHESTRATION_GREEN` mit Architektur,
reale Agentenrollen, Modellrouting, Provenance-Mechanismus, Rollenrechte, Context-Isolation,
Tests, Review-Ergebnis, offene LOW-Punkte, Git-Commit, empfohlener nächster Schritt.

---

## 15. SPEC V2.1 — Härtung nach unabhängigem Sol-Architektur-/Security-Review

V2.1 überschreibt V2 bei Widersprüchen. Schließt die 12 verifizierten Review-Findings (F1–F12).

### 15.1 Controller als einzige Schnittstelle; kein Agent-Zugriff auf Core (F1, CRITICAL)

- Der Core ist ausschließlich über den Controller (Lead-Session) erreichbar. Agenten erhalten NIE
  Core-API-Zugriff, NIE den DB-Pfad, NIE Core-Innereien. Der Controller ist die einzige Prozess-Schnittstelle.
- Die Core-DB ist Controller-eigen (exklusiver Handle). Rollenrechte gelten an der Core-API-Grenze;
  OS-Level-Durchsetzung (Datei-/Shell-/Tool-Zugriff der Agenten) ist in Phase 2A NICHT konfigurierbar
  (keine OpenClaw-Konfigänderung) und wird als Phase-2B-Punkt dokumentiert: native Sandbox-/Tool-Policy,
  nur Implementer erhält eng begrenzten Produktcode-Write; dafür wäre OWNER_APPROVAL_REQUIRED nötig.
- `source`-Strings sind nur innerhalb der Controller-Schnittstelle gültig (der Controller konstruiert sie);
  sie sind kein Authentisierungsmechanismus gegen externe Angreifer — dokumentierte Grenze von 2A.
- In Phase 2A laufen Agenten im Projekt-Repo; ihr Schreibverhalten wird durch Controller-mediierten
  Prompt-/Tool-Kontext begrenzt (Write-Policy im Kontext, siehe §4) und durch den Core nachträglich
  verifiziert (changed_files[] vs. Rollenrecht bei Konsum: Implementer-only Produktcode-Writes).

### 15.2 Ghost-Writer-Ausschluss: Spawn-Intent & Recovery-Regel (F2, CRITICAL)

- `create_dispatch` IST der persistente Spawn-Intent (vor `sessions_spawn`). Korrelationsschlüssel:
  der Controller spawnt mit stabilem Label `argent-dispatch-<dispatch_id>` (taskName), damit nach
  Crash der Spawn-Zustand per nativer Suche rekonziliert werden kann (Controller-Verfahren, dokumentiert).
- **Recovery-Regel (write-role-sicher)**: Dispatches mit `status=PENDING|RUNNING` einer SCHREIBROLLE
  (implementer) werden bei Recovery NIE automatisch auf `FAILED` gesetzt → sie werden `RECOVERY_PENDING`
  und der Task konservativ nach `RECOVERING`; eine zweite Schreibrolle ist dadurch unmöglich
  (aktiver Dispatch + aktiver Role-Run). Read-only-Rollen (`PENDING`) dürfen bei Recovery auf `FAILED`
  (harmlos, erneut dispatchbar).
- Partieller Unique-Index: genau EIN aktiver Dispatch pro Task
  `CREATE UNIQUE INDEX ... ON agent_dispatches(task_id) WHERE status IN ('PENDING','RUNNING','RECOVERY_PENDING')`.
- `RECOVERY_PENDING` wird nur durch den Controller aufgelöst: (a) Result trifft ein → validieren/konsumieren,
  (b) `mark_agent_failed` mit Beleg (z.B. native `openclaw tasks show` = failed/lost) → FAILED +
  Retry derselben Position (gleiche Rolle, `attempt_no+1`). Ohne Beleg bleibt der Task BLOCKED/RECOVERING.

### 15.3 Strikte Result-Provenance (F3, HIGH)

- Konsum NUR aus vollständig gebundenem `RUNNING|RECOVERY_PENDING`-Dispatch (`child_session_id` UND
  `openclaw_run_id` NOT NULL). `PENDING` ist NIE konsumierbar (Result ohne Spawn = unmöglich).
- ALLE ID-Vergleiche sind verpflichtend (kein „falls vorhanden“): `event_meta.task_id`,
  `event_meta.child_session_id`, `event_meta.run_id` müssen exakt dem Dispatch entsprechen;
  Envelope des strukturierten Outputs (`result.task_id`, `result.dispatch_id`, `result.role`) ebenfalls.
- `expected_agent_class`/`expected_model_class` werden gegen die gebundenen Ist-Werte
  (`actual_provider`, `actual_model`, `thinking_tier`) verglichen — Mismatch → rejected.

### 15.4 Persistierte Workflow-Position (F4, HIGH)

- `agent_dispatches` erhält `cycle_no INTEGER NOT NULL DEFAULT 1`, `position INTEGER NOT NULL`,
  `sequence_kind TEXT NOT NULL CHECK (sequence_kind IN ('STANDARD','REWORK'))`;
  UNIQUE wechselt auf `(task_id, cycle_no, position, attempt_no)`.
- `tasks` erhält `description TEXT` (Owner-Anforderung; Kontext-Basis).
- Handoff-Ziele orchestrierter Tasks werden aus der Sequenz berechnet
  (`workflow.next_role(task) = sequence[position]`); `DEFAULT_NEXT_ROLE` gilt nur für Nicht-Orchestrierungs-Tasks.
  (Hinweis: V1 `DEFAULT_NEXT_ROLE[analyst]=lead` deckt sich mit V2; Widerspruch bestand nicht.)
- `expected_next_role` wird deterministisch aus `(cycle_no, position, sequence_kind)` + offenen
  Findings + letzter Lead-Decision replayt; Rework wählt `REWORK`-Sequenz (Lead→Implementer→QA→
  [Reviewer wenn `rework_include_reviewer`]→Lead), Standard die Sieben-Rollen-Sequenz.

### 15.5 Ein atomarer Bind-Befehl + DB-Uniques (F5, HIGH)

- `bind_spawn_result(dispatch_id, child_session_id, openclaw_run_id, actual_provider, actual_model,
  thinking_tier, source)` — EINE atomare Operation `PENDING→RUNNING`, bindet alle Spawn-Rückgaben;
  Modell-Policy-Check (`routing.validate_model_choice`) im selben Block; Event `agent.started`.
- `bind_child_session`/`bind_run_id` separat entfallen.
- Partielle Unique-Indizes: `child_session_id` (nicht-null, einmalig über alle Dispatches),
  `openclaw_run_id` (nicht-null, einmalig), aktiver Dispatch pro Task (15.2).
- `parent_dispatch_id TEXT REFERENCES agent_dispatches(id)` (NULL = Controller-Parent);
  Validierung: `parent_dispatch_id` muss NULL sein (Controller) oder einem bekannten Dispatch entsprechen.

### 15.6 Recovery-/Failure-Semantik normiert (F6, HIGH)

- V2.1-Override (für orchestrierte Tasks): `mark_agent_failed` → Dispatch `FAILED`, Role-Run `failed`
  (`role.failed`), Handoff NACH `DEFAULT_NEXT_ROLE`-Ersatz: Handoff auf DIESELBE Position/Rolle
  (Retry, `attempt_no+1`), Task-Zustand bleibt in der aktuellen Phase. Kein automatischer Fortschritt.
- „Zugestellt, aber nicht konsumiert“ nach Restart: Dispatch ist `RECOVERY_PENDING` → nach erneuter
  Zustellung (Controller-History) konsumierbar; Konsum idempotent (`CONSUMED` → `agent.result_duplicate`).
- Kein Zeit-Timeout; Auflösung nur durch Controller/Result (15.2).

### 15.7 Atomarer Konsum — exakte Spezifikation (F7, HIGH)

- Ein Transaktionsblock (`BEGIN IMMEDIATE`): (1) Validierung komplett (15.3), (2) CAS-Update
  `UPDATE agent_dispatches SET status='CONSUMED', result_json=?, consumed_at=? WHERE id=? AND status IN
  ('RUNNING','RECOVERY_PENDING')` → `rowcount==1` sonst Abbruch, (3) Rolleneffekte NUR task-gebunden
  (agentengelieferte Finding-IDs/`accepted_findings` müssen zum selben Task gehören, sonst Abbruch),
  (4) Role-Run-Completion + Sequenz-Handoff, (5) alle Events. Jede Exception → vollständiger ROLLBACK.

### 15.8 Context-Snapshots (F8, MEDIUM)

- `tasks.description` (15.4) + neue Tabelle `agent_context_snapshots(dispatch_id PK REFERENCES
  agent_dispatches(id), role, position, context_hash, context_summary_json, created_at)`: persistierter,
  unveränderlicher Schnappschuss der strukturierten Kontext-Felder je Rolle (Allowlist je Rolle,
  Herkunft je Feld); volle Diffs liegen im Git-Repo (Referenz: commit/ref + changed_files), NIE in der DB.
- Feld-Ausschluss bleibt: Reviewer-Kontext ohne `own_assessment`/`proposal` des Implementers;
  Analyst-Kontext ohne Implementer-Lösung. Tests prüfen die Snapshots deterministisch.

### 15.9 Kanonisches Modellrouting (F9, MEDIUM)

- Kanonische Strings: `openai/gpt-5.6-sol`, `deepseek/deepseek-v4-pro`, `deepseek/deepseek-v4-flash`;
  `thinking_tier` als Pflichtfeld im Dispatch (lead/reviewer: `high`). QA-Flash NUR bei
  `risk_class='LOW'` („einfache Regressionen“ als Kriterium entfällt — deterministisch).

### 15.10 Geschlossene externe Aktionsmenge (F10, MEDIUM)

- Geschlossene Enum-Menge `EXTERNAL_ACTIONS` in `gates.py`: `install_software, download_dependency,
  system_install, network_fetch, external_send, deploy_production, change_secrets, expose_gateway,
  modify_allowlist, promote_stable, modify_policy, raise_privileges, enable_self_improvement,
  production_write`. `external_actions_policy='FORBIDDEN'` ⇒ alle EXTERNAL_ACTIONS → FORBIDDEN
  (nicht approvable); unbekannte Aktionsnamen → FORBIDDEN (V1-Regel, bleibt). Direkte OS-Toolaufrufe
  sind Sache der Sandbox-/Tool-Policy (Phase-2B-Config-Punkt, §15.1) — der Core vermittelt an seiner Grenze.

### 15.11 Privacy-/Quarantäne-Grenzen (F11, MEDIUM)

- Output-Limits: `result_json` ≤ 256 KB, Tiefe ≤ 12, Stringlänge ≤ 8 KB; Überschreitung → malformed/rejected.
- Strukturierte Outputs: strikte bekannte Top-Level-Felder (Allowlist); unbekannte Top-Level-Schlüssel → rejected.
- Quarantäne-Log: NUR Allowlist-Metadaten (`session_key`, `run_id`, `event_type`, `status`, Grund),
  nie Inhalte; gefälschtes/abweichendes Fremdereignis invalidiert einen legitimen Dispatch NICHT
  (nur Log; Dispatch bleibt `RUNNING`). Nur ein Ergebnis mit übereinstimmender Dispatch-Identität,
  aber malformed Output, setzt den Dispatch auf `REJECTED`.

### 15.12 Ergänzte Pflichttests (F12, MEDIUM)

Zusätzlich zu §12: Spawn-vor-Bind-Crash (kein Ghost-Writer, Recovery lässt Schreibrolle unangetastet),
fehlende Metadaten → rejected, `PENDING`-Injection → rejected, mehrfacher Lead (3 Positionen, eindeutige
UNIQUEs), mehrere Rework-Zyklen (cycle_no), paralleles Bind/Consume auf zwei Verbindungen (CAS),Provider-/Thinking-Mismatch → rejected, legitimer Dispatch nach forged rejection (bleibt RUNNING),
unbekannte externe Aktionen → FORBIDDEN, Oversized/Deep-Outputs → rejected, ausbleibendes Recovery-Resultat
(Task bleibt konservativ BLOCKED/RECOVERING).

---

## 16. SPEC V2.2 — Härtung nach unabhängigem Sol-Implementierungs-Review (F1–F8)

V2.2 überschreibt V2–V2.1 bei Widersprüchen. Alle 8 Findings wurden vom Supervisor im Code verifiziert.

### 16.1 Orchestrierung treibt die autoritative State Machine (F1, HIGH)

Der Konsum eines Dispatches synchronisiert den Task-Zustand in DERSELBEN Transaktion über
`state_machine.validate_transition` (eine Quelle der Wahrheit; kein separater Parallel-Zustand).
Deterministische Mapping-Tabelle (sequence_kind, position, decision) → Zielzustand:

- STANDARD: pos0 lead → NEW→PLANNING; pos1 analyst → PLANNING→ANALYZING;
  pos2 lead → ANALYZING→LEAD_DECISION, bei decision=rework zusätzlich LEAD_DECISION→REWORK,
  bei decision=cancel → CANCELLED (terminal);
  pos3 implementer → LEAD_DECISION→IMPLEMENTING; pos4 qa → IMPLEMENTING→TESTING;
  pos5 reviewer → TESTING→REVIEWING;
  pos6 lead (final) → REVIEWING→FINAL_DECISION, bei decision=accept zusätzlich FINAL_DECISION→DONE
  (task.completed), bei decision=rework FINAL_DECISION→REWORK (neuer Zyklus).
- REWORK: pos0 lead → kein State-Change (Task bleibt REWORK); pos1 implementer → REWORK→IMPLEMENTING;
  pos2 qa → IMPLEMENTING→TESTING; pos3 reviewer (falls enthalten) → TESTING→REVIEWING;
  pos4/final lead → REVIEWING→FINAL_DECISION (+ accept→DONE | rework→REWORK).

Dafür erweitert die State Machine die V1-Tabelle um GENAU ZWEI Übergänge (sonst unverändert,
fail-closed): `LEAD_DECISION → REWORK` und `REWORK → IMPLEMENTING`.
Ist der Ist-Zustand nicht der erwartete Von-Zustand → `InvalidTransition` → Transaktion bricht ab
(fail-closed; der Controller rekonziliert). Workflow-Tests MÜSSEN die Task-Zustände entlang des
Pfads inkl. REWORK und finalem DONE assertieren.

### 16.2 Recovery: vorhandene RECOVERY_PENDING-Dispatches bleiben ungelöst (F2, HIGH)

- Vorhandene UND neu erzeugte `RECOVERY_PENDING`-Dispatches werden in `recovery_pending_dispatches`
  aufgenommen (und damit in `unresolved`/`unresolved_run_ids`); ihre Role-/Task-Runs bleiben STARTED.
- Test: zweimal `recover()` → Role-/Task-Run weiterhin STARTED; legitimes Result danach konsumierbar.

### 16.3 Spawn-vor-Bind-Crash rekonzilierbar (F3, HIGH)

- `bind_spawn_result` akzeptiert `PENDING` UND `RECOVERY_PENDING` (Controller-Reconciliation mit
  Beleg: native Spawn-Rückgaben via stabilem Label `argent-dispatch-<dispatch_id>`); Übergang
  `PENDING|RECOVERY_PENDING → RUNNING` atomar mit exakter Modellprüfung (16.4).
- End-to-End-Test: PENDING-Dispatch → `recover()` → RECOVERY_PENDING → Bind mit echten
  Spawn-Rückgaben → RUNNING → Result konsumierbar.

### 16.4 Provenance vollständig verpflichtend (F4, HIGH)

- `create_dispatch` persistiert zusätzlich `expected_thinking_tier` (aus `routing.resolve_model`:
  lead/reviewer=high, analyst/implementer/qa=medium) und verlangt `parent_dispatch_id`
  (None = Controller; sonst muss der Parent-Dispatch existieren).
- `bind_spawn_result`: EXAKTE Gleichheit aller Spawn-Werte mit den Erwarteten
  (`actual_provider == expected_agent_class`, `actual_model == expected_model_class`,
  `thinking_tier == expected_thinking_tier`) ZUSÄTZLICH zur Rollen-Policy
  (`validate_model_choice`); Mismatch → atomar `REJECTED` + `policy.role_violation`-Event,
  kein hängender RUNNING-Dispatch.
- `receive_agent_result`: `event_meta` MUSS `task_id`, `child_session_id`, `run_id`,
  `parent_dispatch_id` (exakter Vergleich, nicht optional), `event_type` und `status` enthalten;
  `status` muss `completed`/`succeeded` sein, sonst rejected (fehlgeschlagene Runs →
  `mark_agent_failed`). `_model_mismatch` bleibt als zweite Verteidigungsschicht beim Konsum.

### 16.5 Context-Snapshots unveränderlich, dispatchgebunden, privacy-sicher (F5, MEDIUM)

- `snapshot_agent_context`: `role`/`position` MÜSSEN dem Dispatch entsprechen; `repo_summary` wird
  über eine strikte Feld-Allowlist + Größenlimits (≤256 KB, Tiefe ≤12, String ≤8 KB) + Denylist-Scan
  gefiltert, sonst `DispatchError`/`PrivacyViolation`.
- Store: Plain `INSERT` (kein REPLACE); zweites Snapshot für denselben Dispatch mit abweichendem
  Inhalt → Fehler. Tests: Full-Diff/Secret im repo_summary → rejected; abweichendes zweites
  Snapshot → Fehler.

### 16.6 Verschachtelte Rollenoutput-Validierung VOR dem CAS (F6, MEDIUM)

- `outputs.validate_role_output` validiert Element-Schemas vollständig VOR dem Konsum:
  `findings[]` = dicts mit Allowlist {severity: low|medium|high|critical, description/title: str,
  optional id}; `accepted_findings[]`/`rejected_findings[]`/`alternatives[]`/`concerns[]`/
  `blockers[]`/`evidence_refs[]`/`failures[]`/`regressions[]`/`tests_run[]` = Strings;
  `tests[]` = dict{name: str, result: passed|failed|error} oder str;
  `security_findings[]`/`architecture_findings[]` = Strings oder dicts {severity, description};
  Reviewer-`severity` = Enum low|medium|high|critical.
- Verstoß → `OutputValidationError` VOR CAS → Dispatch `REJECTED` (malformed_output) — kein
  Rollback-Fall nach dem Konsum. Negative Tests je verschachteltem Feld.

### 16.7 Quarantäne-Metadaten: Werte validieren und begrenzen (F7, MEDIUM)

- `_sanitize_event_meta`: Werte werden erzwungen auf str (Typ), Länge ≤512, Denylist-Scan;
  bei Verstoß → rotierter Platzhalter `<redacted:<sha256-prefix>>`. Immer JSON-serialisierbar.

### 16.8 Migration transaktional + Versions-UPSERT + realistischer Test (F8, MEDIUM)

- `_migrate` läuft in EINEM `BEGIN IMMEDIATE`-Block; nach erfolgreicher DDL wird `schema_version`
  per UPSERT (INSERT ... ON CONFLICT DO UPDATE) auf 3 gesetzt; Fehler → vollständiger Rollback.
- Test: realistische V2-Datenbank (V2-Tabellen + `schema_meta`-Zeile = 2) → Öffnen mit V3-Store →
  neue Spalten vorhanden, Version == 3, Bestandsdaten intakt.

### 16.9 Regressionstests

Je Finding F1–F8 mindestens ein reproduzierender Test (siehe 16.1–16.8), alle bestehenden Tests
bleiben grün.

---

## 17. SPEC V2.3 — Härtung nach Sol-Recheck (G1–G4)

V2.3 überschreibt V2–V2.2 bei Widersprüchen. Alle 4 Befunde wurden vom Supervisor im Code verifiziert.

### 17.1 Bind vollständig atomar, REJECTED als CAS (G1, MEDIUM)

- `bind_spawn_result`: Status-Lesen, Exact-Equality-, Policy-Prüfung UND Statusänderung laufen in
  EINEM `BEGIN IMMEDIATE`-Block (keine Prüfung auf einem veralteten `d0` außerhalb).
- Der Mismatch-Pfad setzt `REJECTED` per CAS: `UPDATE agent_dispatches SET status='REJECTED'
  WHERE id=? AND status IN ('PENDING','RECOVERY_PENDING')`; `rowcount==1` erforderlich, sonst
  Abbruch (kein Überschreiben eines parallel gültig gebundenen RUNNING-Dispatches).
- Test: paralleler valid-vs-mismatch-Bind (zwei Verbindungen) → gültiger Bind bleibt RUNNING,
  Mismatch wird abgewiesen, kein Ghost-Writer-Retry möglich.

### 17.2 Element-Schemas vollständig erzwungen (G2, MEDIUM)

- `findings[]`: jedes Element MUSS `severity` (Enum low|medium|high|critical) UND mindestens
  eines von `description|title` (str) enthalten; sonst `OutputValidationError`.
- `security_findings[]`/`architecture_findings[]`-Dicts: `severity` UND `description` Pflicht;
  leere Dicts abgelehnt. Negative Tests für fehlende Pflichtfelder in JEDER Liste.
- `_apply_role_effects`: `title` als Fallback für `description` verwenden (title-only-Findings
  erhalten den Titel als Beschreibung).

### 17.3 Schema-Erstellung + Migration in EINER Transaktion (G3, MEDIUM)

- `_create_schema` UND `_migrate` laufen in EINEM gemeinsamen `BEGIN IMMEDIATE`-Block
  (alle `_SCHEMA`-DDL inkl. neuer Tabellen/Indizes + ALTER + Versions-UPSERT); jeder Fehler →
  vollständiger Rollback (keine persistierte Teilmigration).
- Failure-Injection-Test: Fehler während `_migrate` (z. B. defektes Schema/SQL) → V2-Struktur
  und `schema_version=2` unverändert; danach erneutes Öffnen erfolgreich.

### 17.4 Quarantäne-Werte: `None` und problematische `__str__` fail-safe (G4, LOW)

- `_sanitize_event_meta.clean`: `None` → deterministischer Platzhalter `<none>`; `str(value)`
  in try/except (fehlschlagende `__str__` → `<unprintable>`); Länge ≤512, Denylist-Scan wie gehabt.
  Ausgabe ist immer String-typisiert und JSON-serialisierbar.
- Test: `None`- und Objekt-Werte in event_meta → Strings/Platzhalter, Quarantäne-Zeile serialisierbar.

---

## 18. SPEC V2.4 — Foreign-Event auf CONSUMED-Dispatch (H1, MEDIUM, aus echtem Smoke-Run)

V2.4 überschreibt V2–V2.3 bei Widersprüchen. Finding vom Supervisor im echten Smoke-Run entdeckt und
verifiziert (Repro: gefälschtes Event mit falscher `child_session_id` auf konsumierten Dispatch wurde
stillschweigend als `duplicate` geschluckt, ohne Quarantäne).

### 18.1 Regel

- Im `CONSUMED`-Zweig von `_receive_work` wird ZUERST die Event-Identität geprüft
  (`_event_meta_mismatch`: task_id, child_session_id, run_id gegen die gespeicherten Bindings).
- Übereinstimmende IDs (legitime Wiederzustellung desselben Runs) → `agent.result_duplicate` +
  `ReceiveResult(..., "duplicate")` (idempotent, kein State-Change).
- Abweichende IDs → `_quarantine` + `_emit_rejected` + `ReceiveResult(..., "rejected",
  reason=<mismatch>)`; KEIN `agent.result_duplicate`, KEIN State-Change.
- Duplikat-Idempotenz gilt ausschließlich für dasselbe gültige Run (übereinstimmende IDs).

### 18.2 Regressionstests (tests/test_phase2a_v24_consumed_foreign.py)

Identische Wiederzustellung → duplicate + genau ein `agent.result_duplicate`-Event, keine Quarantäne;
falsche Session / falscher run_id / falsche task_id / fehlende Metadaten → rejected + Quarantäne
(`session_mismatch`/`run_id_mismatch`/`task_mismatch`/`missing_metadata`), kein State-Change.
