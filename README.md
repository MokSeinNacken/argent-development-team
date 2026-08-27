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
