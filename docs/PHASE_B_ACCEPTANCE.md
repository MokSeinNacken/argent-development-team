# PHASE B ACCEPTANCE — Durable Supervisor Core (B1 + B2 + B3 integriert)

**Status:** Acceptance-Beweis abgeschlossen; **Fix-Runde nach Sol-Closing-Review
(REJECT) umgesetzt** (F1-F6 behoben, adversariale Regressionstests ergänzt).
**Nicht committet** (Supervisor committet nach dem erneuten unabhängigen Review).

---

## 1. Commit-Chain (Basis)

| Commit | Phase | Inhalt |
|---|---|---|
| `e43e785` | B1 | Durable Queue / Job-Lease / Epoch-Fencing (`job_state.py`, `store.py` lease primitives, `supervisor.py` fencing) |
| `aa0189c` | B2 | Durable Scheduler-Passes + Restart-Reconciliation (`scheduler.py`, Action-Fence `_fence_action`, `clear_lease`, `quarantine_lost`) |
| `c7654ac` | B3 | External Wait Manager + minimal Process Registry + Worktree/Writer-Binding (`external_wait.py`, `process_registry.py`, `worktree.py`, `store.py` Tabellen/Spalten) |

Base dieses Worktrees: `c7654ac` (Working Tree war clean; B4 ergänzt **nur** Tests + dieses
Dokument, keine Produktcode-Änderung).

---

## 2. Finale relevante Architektur (Ist-Stand nach B1–B3)

- **8 Primary States** unverändert: `QUEUED, RUNNING, WAITING_EXTERNAL, OWNER_GATE, BLOCKED, FAILED, LOST, DONE`.
  `primary_state` ist die autoritative Betriebsprojektion; die V2C-`status`-Spalte bleibt synchron.
- **Lease direkt am Job** (`owner_instance_id`, `lease_epoch`, `lease_expires_at`); keine `job_leases`-Tabelle.
  `lease_epoch` ist monotones Fencing-Token; `facts_version` invalidisiert alte Entscheidungen.
- **Fencing-Token** für jeden mutierenden Commit: `job_id + owner_instance_id + lease_epoch + lease_expires_at > now + facts_version`.
- **Action-Journal** (`supervisor_actions`) bleibt Audit-/Crash-Evidence; `action_key` UNIQUE erzwingt
  exactly-once pro Effekt (get-or-create + Fence im `BEGIN IMMEDIATE`).
- **Scheduler** (`Scheduler.run_pass` / `reconcile_after_restart`): ein sicherer Schritt pro Pass, kein
  gehaltener Agent; Renewal nur bei noch-RUNNING + aktueller gültiger Lease; Restart-Reconcile ist
  deterministisch + idempotent (nur Quarantäne-`LOST` als einziger Write).
- **External Wait** (`ExternalWaitManager`): atomarer `RUNNING → WAITING_EXTERNAL` (Wait-Row + Lease-Release
  in einer Transaktion), bounded non-LLM Checker (Backoff 1/2/5/10/30 min), Event-Dedup + SHA-/Subject-Bindung,
  Deadline → `QUEUED` (nie direkt `DONE`/`FAILED`).
- **Process Registry** (`ProcessRegistry`): Identität = `(boot_id, pid, process_start_ticks)`, nur am
  trusted Spawn-Pfad registriert; unbekannte Evidenz fail-closed `UNKNOWN`, nie „sicher tot".
- **Worktree/Writer-Binding** (`WorktreeBinding`, `writer_guard_for`, `bind_writer_worktree`):
  1 Job = 1 Writer-Worktree = max. 1 gültige Writer-Lease; Guard ans volle Fencing-Token gebunden;
  Dirty/mehrdeutig wird nie gelöscht/überschrieben.

---

## 3. Failure/Crash Matrix (27 Fälle) — Testnachweis

Alle Fälle in `tests/test_phase_b4_crash_matrix.py` (deterministisch, FakeClock, keine echte Zeit/Netz/LLM).

| # | Fall | Erwartetes Verhalten | Test |
|---|---|---|---|
| **A. Queue/Claim** | | | |
| 1 | Crash vor Claim | bleibt `QUEUED`, normal claimbar nach Restart | `test_case01_crash_before_claim_stays_queued` |
| 2 | Crash direkt nach Claim | Lease/Epoch persisted; Restart rebindet (gleicher Owner) / kein Takeover (fremder Owner) | `test_case02_crash_after_claim_lease_persisted_reconciles` |
| 3 | Zwei Supervisoren gleichzeitig | exakt einer gewinnt (epoch=1) | `test_case03_two_supervisors_exactly_one_wins` |
| **B. Action Journal** | | | |
| 4 | Crash vor Effect | spätere sichere Ausführung (get-or-create replay) | `test_case04_crash_before_effect_replays_safely` |
| 5 | Crash nach Effect vor Finalize | keine Doppelwirkung | `test_case05_crash_after_effect_before_finalize_no_double` |
| 6 | Crash nach Finalize | nicht wiederholen (genau 1 SUCCEEDED-Row) | `test_case06_crash_after_finalize_not_repeated` |
| 7 | Takeover zwischen Fence-Check und Effect | stale Owner schreibt nichts | `test_case07_takeover_between_fence_check_and_effect_fences_stale` |
| **C. Writer** | | | |
| 8 | Writer läuft, Supervisor stirbt | kein zweiter Writer ohne sichere Evidenz (live identity same → kein Takeover) | `test_case08_writer_running_supervisor_dies_no_second_writer` |
| 9 | Writer-Status unbekannt | `LOST`/fail-closed | `test_case09_writer_status_unknown_lost_fail_closed` |
| 10 | Writer terminal + Worktree konsistent | Recovery möglich (`CLEANUP_PENDING`) | `test_case10_writer_terminal_worktree_consistent_recovery` |
| 11 | Worktree dirty, job-owned | Arbeit erhalten (`KEEP_DIRTY`) | `test_case11_worktree_dirty_job_owned_kept` |
| 12 | Worktree divergent/fremd/mehrdeutig | nichts überschreiben (`BLOCKED_DIVERGED`/`LOST`/`AMBIGUOUS_WRITER`) | `test_case12_worktree_divergent_foreign_ambiguous_blocked` |
| **D. Process** | | | |
| 13 | gleiche PID+boot_id+start_ticks | gleicher Prozess | `test_case13_same_identity_same_process` |
| 14 | gleiche PID + andere start_ticks | PID-Reuse, nicht derselbe Prozess | `test_case14_same_pid_other_ticks_pid_reuse` |
| 15 | andere boot_id | alter Prozess sicher nicht alive | `test_case15_other_boot_id_not_alive` |
| 16 | Evidence nicht lesbar | `UNKNOWN`, nicht als sicher tot | `test_case16_unreadable_evidence_unknown_not_dead` |
| **E. External Wait** | | | |
| 17 | Crash unmittelbar vor Wait-Commit | kein halber Zustand (Rollback) | `test_case17_crash_before_wait_commit_no_half_state` |
| 18 | Crash unmittelbar nach Wait-Commit | Wait persistent, kein LLM nötig | `test_case18_crash_after_wait_commit_wait_persists_no_llm` |
| 19 | Restart während WAITING_EXTERNAL | Wait bleibt (next_check_at/deadline erhalten) | `test_case19_restart_during_wait_wait_remains` |
| 20 | duplicate external event | max. 1 Wake | `test_case20_duplicate_event_max_one_wake` |
| 21 | stale/wrong subject/SHA | keine Wirkung | `test_case21_stale_wrong_subject_sha_no_effect` |
| 22 | Pending | kein Fehler, kein LLM | `test_case22_pending_no_error_no_llm` |
| 23 | Deadline | `QUEUED` + EXTERNAL-Metadaten, nie `DONE`/`FAILED` | `test_case23_deadline_requeues_external_not_done` |
| **F. Terminal** | | | |
| 24 | DONE nach Restart | DONE (sticky) | `test_case24_done_after_restart_sticky` |
| 25 | FAILED nach Restart | FAILED (sticky) | `test_case25_failed_after_restart_sticky` |
| 26 | BLOCKED | nicht normal claimbar (nur autorisiert) | `test_case26_blocked_not_normally_claimable` |
| 27 | stale Owner terminale Mutation | fenced | `test_case27_stale_owner_terminal_mutation_fenced` |

---

## 4. Integrierter E2E-Lifecycle (Pflicht)

`tests/test_phase_b4_e2e_lifecycle.py::test_e2e_durable_lifecycle_queue_wait_restart_done`

Verlauf (Schritt für Schritt, alle Assertions grün):

1. `QUEUED` (create_job).
2. **Atomarer Claim** via `Scheduler.run_pass` → `RUNNING` (`owner=instance-A`, `lease_epoch=1`, `lease_expires_at` gesetzt).
3. **Process-Evidence gebunden** (`ProcessRegistry.register`, boot-1/pid/ticks persistiert).
4. **Action** (`reconcile` → `START_ROLE`) — aktive Role-Run existiert.
5. **`WAITING_EXTERNAL`** via `ExternalWaitManager.enter_waiting_external` → Lease freigegeben (`owner=None`, `lease_expires_at=None`), Wait-Row persistiert.
6. **Persistenter Restart** (`core.close()` + `Core`/`Supervisor`/`Scheduler` neu über derselben DB): Wait überlebt, alte Lease weg, Process-Evidence konsistent.
7. **Non-LLM Wait-Check** (`check_due_waits`, +61s) → `woke`, `QUEUED` mit `queue_reason=WAIT_EVENT`; **kein** neuer Dispatch (kein Poll-Agent).
8. **Neuer Claim** (`Scheduler.run_pass`) → `RUNNING` mit `lease_epoch=2`; alte `(instance-A, 1)` gefenced (`assert_lease_current` → `LeaseFencedError`).
9. **Weitere Action → `DONE`** (SupervisorLoop, echter Implementer-Schritt schreibt Patch und etabliert Writer-Binding).
10. **DONE sticky**: nicht claimbar, `terminal=DONE`, `primary_state=DONE`.

Beweise: alte Lease wirkt nicht nach Wake/Takeover (Fence), kein Poll-Agent, Wait überlebt Reopen,
Writer-/Process-Evidence konsistent (`writer_binding_mode=BOUND`, `canonical_worktree_path==ws`,
`writer_lease_epoch>=2`, Patch-Datei existiert), DONE sticky.

---

## 5. Soak-/Stress-Simulation (bounded, deterministisch)

`tests/test_phase_b4_soak.py::test_soak_durable_transitions_no_invariant_violation`

- **600** kontrollierte Iterationen (eine Job-„Super-Cycle" = claim → wait → wake → reclaim → backoff),
  4 Jobs, `LEASE_TTL=30s`, FakeClock.
- **12** DB-Reopens (Crash/Restart-Interleaving) mit Fakten-Identitäts-Check vor/nach Reopen.
- Checks nach jedem Schritt + am Ende: keine doppelte Ownership (RUNNING ⇒ genau ein gültiger
  Owner+epoch+expiry), keine Epoch-Regression, keine terminale Wiederöffnung, keine duplicate
  Action-Keys, kein duplicate Wake (terminal_observed_at unveränderlich), kein orphaned Writer-Binding
  (BOUND ⇒ vollständiges Tupel), DB-Reopen konsistent.
- Ergebnis: `passes >= 300`, `wakes >= 1`, `wait_entries >= 1`, `reopens == 12` — grün.

---

## 6. Migration/Reopen-Acceptance

`tests/test_phase_b4_migration.py` (5 Tests)

- **Frische DB** → `SCHEMA_VERSION == "8"`, alle Phase-B-Tabellen/Spalten vorhanden.
- **Legacy pre-B1 (V6)** → Migration ergänzt B1 (queue/lease) + B3 (worktree/writer) Spalten,
  korrekter Backfill (`ACTIVE` ohne Lease → `QUEUED`, `DONE` bleibt `DONE`), keine Row-Verluste.
- **B1 (V7)** → Migration ergänzt B3-Spalten + `external_waits`/`process_registry`, bestehende
  B1-Felder unangetastet. *(Hinweis: B2 hat keine Schema-Änderung; es existiert keine separate
  „B2-Schema-Stufe" — Versionen: V6=pre-B1, V7=B1, V8=B3.)*
- **Wiederholtes Reopen idempotent** (gleiche Version, byte-identische Job-Fakten).
- **Nicht-destruktiv** (keine `DROP`/`DELETE` in der Migration), **Schema-Version deterministisch**
  (zwei unabhängige frische DBs ergeben exakt dieselbe Version).

`tests/fixtures/` enthält keine Legacy-DB (nur ein unrelated `.d.ts`-Snippet); Legacy-DBs werden
programmatisch mit exaktem V6/V7-Spaltenstand aufgebaut.

---

## 7. Security-Acceptance (integriert)

`tests/test_phase_b4_security.py` (10 Tests)

- **Strukturell:** `WaitRequest` trägt nur `kind`+`reason` (kein provider/ref/sha/url/command);
  `WaitSpec`/`WaitObservation` haben keine command/shell/poll/credential/approval/scope-Felder;
  `external_waits`-Tabelle hat keine command/shell/secret/token/url/prompt-Spalte.
- **Owner/Epoch:** Agent-Output kann Owner/Epoch nicht bestimmen (Lease nur über lokalen
  `owner_instance_id`-Aufruf; `WaitRequest` kennt keins).
- **Lease:** externe Observation verlängert keine Lease (Wake → `QUEUED`, `owner=None`,
  `lease_expires_at=None`); ein Wake münzt keine neue Lease.
- **Writer-Binding:** stale Owner (falscher epoch) kann Binding nicht ändern (`LeaseFencedError`,
  kein partial binding); Guard verweigert fremden Dispatch/Pfad.
- **Worktree-Pfad:** Path-Injection (`..`, absolut, Symlink-Escape) fail-closed abgelehnt.
- **External Provider/Ref:** nur allowlistete Provider/Refs; fremder Provider wird abgelehnt, kein Scope-Wachstum.
- **Shell-Commands:** kein Command-/Poll-/Shell-Feld auf irgendeinem Wait-/Observation-Pfad.
- **Approval/Security-Policy:** externe Daten erzeugen/consumen/approven keine Gates; kein
  Policy-Schreibpfad aus Agent-Output.
- **DONE/Scope:** externes Event setzt nie `DONE`/`FAILED`; terminale Jobs können durch keinen
  Agent-/Output-Pfad wiedereröffnet werden.

Fazit: External Observation / Process- / Worktree-Evidence bleiben **Daten**, keine neue Autorität.

---

## 8. Test-Evidence (exakte Zahlen)

| Suite | Tests | Ergebnis |
|---|---|---|
| Neue B4-Tests (Acceptance/Crash-Matrix/Soak/Migration/Security/Fix-Round) | **63** | passed |
| B1+B2+B3-Regression (`test_phase_b1_*`, `test_phase_b2_*`, `test_phase_b3_*`) | **103** | passed |
| Gesamte lokale Suite (`--ignore=e2e-fixture`) | **1422** | passed |

- Neue B4-Dateien: `test_phase_b4_crash_matrix.py` (27), `test_phase_b4_e2e_lifecycle.py` (1),
  `test_phase_b4_soak.py` (1), `test_phase_b4_migration.py` (5), `test_phase_b4_security.py` (10),
  `test_phase_b4_fix_round.py` (19, adversariale F1-F6-Regressionstests).
- Das bekannte, **unrelated** `e2e-fixture`-Collection-Problem (Importfehler `parser`) wurde **nicht
  angefasst** (Aufgaben-Vorgabe); die Suite wird daher mit `--ignore=e2e-fixture` gefahren.
- Laufzeit Full Suite ~24 s, ressourcensicher (kein echter Stunden-Test).

---

## 9. Dokumentierte Korrekturen / Konflikte / Alternativen

- **Fix-Runde nach Sol-Closing-Review (REJECT):** der unabhängige Review hat 6 Findings (F1-F6)
  bestätigt; diese wurden in Produktcode + Tests behoben (siehe Abschnitt 9a). Die ursprüngliche
  Annahme „Keine Produktcode-Änderung nötig" war **falsch** — B1/B2/B3 enthielten einen
  Prozess-Recovery-Bypass (F1), eine Broker-TOCTOU-Lücke (F2) und fehlende echte
  Worktree-Provenance (F3) sowie zwei Low-Level-Mutator-Schwächen (F4/F5).
- **B2-Schema-Hinweis:** B2 (Scheduler/Recovery) hat keine Schema-Änderung vorgenommen
  (`SCHEMA_VERSION` blieb 7); „B2-Schema" ist daher identisch mit „B1-Schema". Im Report
  entsprechend dokumentiert statt einer fiktiven Migrationsstufe.
- **Beobachtung (kein Scope-Change):** beim terminalen `CLOSE_DONE` bleibt der persistente
  Lease-Tupel (`owner_instance_id`/`lease_expires_at`) am Job stehen. Für die Korrektheit unschädlich
  (DONE ist sticky und nie claimbar), aber als minimale Aufräum-Inkonsistenz notiert — keine Änderung
  im B4-Scope, kann in einer späteren Phase erwogen werden.

## 9a. Fix-Runde (F1-F6, nach Sol-Closing-Review)

| # | Schwere | Finding | Behoben durch |
|---|---|---|---|
| F1 | HIGH | Prozess-Recovery über normalen Claim-Pfad umgehbar | `_job_is_claimable` schließt RUNNING aus; neuer atomarer `recover_takeover_job`-Pfad (Prozess-/Worktree-/Journal-Evidence); Scheduler `_resolve_target`/`reconcile_after_restart` nutzen nur diesen Pfad |
| F2 | HIGH | Writer-Fence TOCTOU über Komponenten-/FS-Grenze | Broker re-assertet den Guard vor JEDEM OS-Effekt (`_write_file`/`_delete_file`/`os.replace`/`unlink`) + Bestätigungs-Check danach; in-flight Apply/Broker-Action sperrt Takeover (F1d) |
| F3 | HIGH | Keine reale Worktree-Provenance erzeugt/verwendet | `GitProvenanceProvider` + `bind_writer_worktree(expected_head/current_head)` + `_git_provenance()` im Apply-Pfad; `classify_worktree_recovery` im Produktpfad (Scheduler) |
| F4 | HIGH | `owner_authorized` requeueet jeden nicht-DONE/FAILED-Zustand | exakter CAS: `primary_state=BLOCKED` + `terminal=BLOCKED` + kein Owner + keine Lease; `policy_ref` nichtleer/bounded |
| F5 | MEDIUM | DONE/FAILED im Low-Level-Mutator nicht immutable | `_update_supervisor_job`: anderer Terminalwert abgewiesen; nur idempotente Wiederholung + Metadaten erlaubt; BLOCKED→DONE/FAILED abgewiesen |
| F6 | MEDIUM | B4-Acceptance-Tests überschätzen Abdeckung | `test_phase_b4_fix_round.py` (19 adversariale Tests) + E2E mit echter Git-Provenance + Case 8 mit `run_pass` |

**Test-Evidence (ehrlich):**

- **Integriert/adversarial getestet (jetzt):** RUNNING nie direkt claimbar (F1); live-Prozess
  blockiert `run_pass`-Takeover (F1); toter Prozess + konsistente Worktree + keine in-flight Action
  → Takeover Epoch+1 (F1); divergente/fremde Worktree → BLOCKED/LOST (F1/F3); Mid-Broker-Takeover
  schreibt keine Datei (F2); echte Git-Provenance im BOUND-Binding (F3, E2E mit realem `git`);
  `owner_authorized` nur exakter BLOCKED (F4); DONE→FAILED abgewiesen, DONE idempotent (F5);
  Dual-Owner-Takeover-Race + Terminal-Stickiness (F6).
- **Nicht getestet (außerhalb Phase-B-Scope):** echter Multi-Host/Distributed-Takeover (mehrere
  Maschinen, keine shared-FS-Semantik), echtes `git`-Merge/`rebase`-Verhalten über Crash-Grenzen,
  echte `/proc`-Prozess-Liveness gegen laufende Agents (Tests nutzen deterministische
  Identity-Provider-Doubles).

## 10. Bekannte Restrisiken (Phase-B-Scope)

- Ein globaler Singleton-Scheduler-Lease existiert bewusst nicht (B2-Entscheidung): die
  Job-Lease/Epoch ist das einzige Fencing-Token; Dual-Supervisor-Schutz ist durch den atomaren Claim
  gegeben. Phase G (Background Service) reevaluiert.
- Writer-Binding wird nur bei echtem (nicht-leerem) Broker-Write etabliert; ein No-Op-Patch setzt
  kein BOUND-Binding (korrekt, da kein Writer aktiv war).
- Resource-Governor, Context Packs, Background-Service, Live-Watcher, Merge-Queue, Telegram-B2B sind
  **explizit nicht** Teil von Phase B.

## 11. Sol-Closing-Verdict

- Verdict: **REJECT** (Erst-Review des integrierten Phase-B-Candidates) → 6 Findings (F1–F6: 4 HIGH, 2 MEDIUM), alle vom Supervisor unabhängig im Code verifiziert und **bestätigt**, in der Fix-Runde gebündelt geschlossen und per adversarialer Regressionstests belegt.
- Findings (bestätigt, geschlossen):
  - F1 HIGH — Prozessbasierte Recovery konnte über den normalen Claim-Pfad umgangen werden → RUNNING nie direkt claimbar; atomarer `recover_takeover_job` mit Snapshot-CAS, Process-/Worktree-Evidence und in-flight-Action-Sperre.
  - F2 HIGH — Writer-Fence TOCTOU über Broker-/Filesystem-Grenze → Guard-Recheck unmittelbar vor jedem OS-Effekt + Bestätigungs-Check; Takeover-Sperre bei in-flight Broker-Action.
  - F3 HIGH — Reale Worktree-Provenance fehlte → `GitProvenanceProvider`, echte repo_identity/base_commit/branch/expected_head/current_head im Binding, `classify_worktree_recovery` im Produktpfad.
  - F4 HIGH — `owner_authorized` ohne Quell-CAS → exakt `BLOCKED`+`terminal=BLOCKED`+lease-frei, `policy_ref` bounded; RUNNING/LOST nie requeueet.
  - F5 MEDIUM — DONE/FAILED nicht immutable im Low-Level-Mutator → Wechsel auf anderen Terminalwert abgewiesen; BLOCKED→DONE/FAILED abgewiesen.
  - F6 MEDIUM — Testabdeckung überschätzt → 19 adversariale Integrationstests (Live-Prozess, Mid-Broker-Takeover, echte Git-Provenance, Dual-Owner-Races, Terminal-Stickiness).
- Verworfen: 0. Offene Findings: **0**.
- Verifikation nach Fix-Runde (Supervisor, unabhängig): 166 targeted PASS, Full Suite **1422 PASS** (~25 s), kein RESOURCE_LIMIT.
