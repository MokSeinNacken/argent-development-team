# ARGENT DEVELOPMENT TEAM — Phase 2B: Rollen-Isolation & E2E (SPEC V2B)

Basis: Phase 2A (ORCHESTRATION_GREEN, 676b4ee, 727 Tests). Owner-freigegeben am 2026-08-27 20:16
(exakt Variante A; Approval nicht übertragbar). Config-Änderung bereits aktiv (siehe
docs/PHASE2B_CONFIG_CHANGE.md): 5 Rollen-Agenten mit `tools.profile="minimal"` →
zero dangerous or mutating tools; one harmless status capability (session_status)
auf direkten Turns; Subagent-Spawns fail closed (belegt: minimal-Profil = nur
session_status; session_status wird Subagenten nativ entzogen).

## 1. Sicherheitsarchitektur (final)

- **Layer 1 (nativ):** Rollen-Agenten haben zero dangerous or mutating tools; one harmless
  status capability (session_status) on direct turns; subagent spawns fail closed — kein read
  (host-weit!), kein exec, kein write/edit/apply_patch, kein web, kein memory, keine Sessions.
- **Layer 2 (Core):** Provenance, Owner Gates, Rollenrechte, Workflow (unverändert Phase 2A).
- **Datenfluss:** Controller erzeugt scope-validierte **Context-Snapshots** (Denylist-gescannt,
  größenbegrenzt) → Agent liefert strukturiertes JSON → Controller validiert (Provenance) →
  **Write-Broker** wendet Patch-Set an (Pfadvalidierung) → **bwrap-Test-Runner** führt Tests aus
  (Namespace, Limits) → Ergebnisse → nächster Handoff.
- Agent-authored Code = UNTRUSTED DATA, läuft NIE direkt auf dem Host (nur im bwrap-Namespace).

## 2. Write-Broker (`argent_core/workspace_broker.py`)

API: `broker.apply_patch_set(scope_root, patch_set, role, source) -> BrokerResult`
`patch_set` = Liste von `{op: "write"|"delete", path: str, content: str (base64 für write)}`
`BrokerResult` = {applied: [...], skipped: [...], errors: [...]}

Härtungsregeln (alle verpflichtend, jeweils mit Test):
1. **Absolute-Pfad-Escape:** `path` wird relativ zum scope_root interpretiert; absolute Pfade
   oder Pfade, deren kanonisiertes Ziel außerhalb liegt → Ablehnung.
2. **`..`:** Normalisierung via `os.path.normpath` + `realpath`-Vergleich gegen `realpath(scope_root)`
   (Prefix-Check mit Separator-Grenze); jede Abweichung → Ablehnung.
3. **Symlink-Escape:** Alle existierenden Ziel-Komponenten werden via `realpath` aufgelöst und
   müssen innerhalb des Scope liegen; das Ziel selbst wird mit `O_NOFOLLOW` geöffnet (bzw. `lstat`
   vor `os.replace`); Symlink im Ziel → Ablehnung.
4. **Hardlinks:** Nach dem Write `lstat`: `st_nlink == 1`, sonst Ablehnung + Rollback.
5. **Special Files:** Ziel/Ergebnis muss reguläre Datei sein (`stat.S_ISREG`), kein setuid/setgid
   (`st_mode & 0o6000 == 0`), kein Device/FIFO/Socket.
6. **Scope Escape:** Alle Writes nur unterhalb von `realpath(scope_root)`; `delete` nur für
   reguläre Dateien im Scope.
7. **Config-/Secret-Pfade:** Zusätzliche Deny-Liste (unabhängig vom Scope): `~/.openclaw`,
   `/etc`, `~/.ssh`, `~/.config`, `~/.npm-global`, `/mnt/*`, `/proc`, `/sys`, `/dev`, `/run`,
   sowie jede Datei, deren Inhalt einen Privacy-High-Signal-Marker der (eingeengten) Content-
   Deny-Liste enthält: `secret`, `password`, `api_key`, `credential`, `mail_content`,
   `mail_address`, `email_address`, `recipient` → Ablehnung.
8. **TOCTOU-nahe Fälle:** Staging-Datei im Zielverzeichnis schreiben (`O_CREAT|O_EXCL|O_NOFOLLOW`),
   unmittelbar vor `os.replace` erneute Kanonisierung des Ziels, nach dem Replace `lstat`-Verifikation
   (4+5). Bei jeder Verletzung: bereits geschriebene Dateien des Patch-Sets zurücksetzen
   (Backup vor Replace; Rollback im Fehlerfall), kein Teilzustand.
9. **Atomisches Schreiben:** Jeder einzelne Write atomar via `os.replace`; das gesamte Patch-Set
   all-or-nothing (Backup + Rollback).
10. **Keine Shell/eval/exec:** Patch-Inhalte werden NIE als Kommando ausgeführt; reine
    Datei-Operationen.

Rollen-Scope: implementer → `scope_root = <proj>/e2e-fixture` (alles darunter erlaubt);
qa → `<proj>/e2e-fixture/tests/**` (Produktcode-Pfade werden abgelehnt → `PermissionDenied` +
`policy.role_violation`-Event). Broker-Aufruf nur durch Controller (source=role:lead).

## 3. bwrap-Test-Runner (`argent_core/sandbox_runner.py`)

API: `sandbox_runner.run_tests(workspace_path, pytest_args=None, limits=None) -> SandboxResult`
(exit_code, stdout_bounded, stderr_bounded, timed_out, wall_seconds)

Exakte Isolation (freigegeben; verifiziert durch Selftests):
```
bwrap --ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /lib64 /lib64 \
      --ro-bind /bin /bin --ro-bind /sbin /sbin --ro-bind /etc /etc \
      --proc /proc --dev /dev --tmpfs /tmp --tmpfs /home --tmpfs /root \
      --ro-bind <workspace> /workspace \
      --unshare-net --unshare-pid --die-with-parent --new-session \
      --clearenv --setenv PATH /usr/local/bin:/usr/bin:/bin \
      --setenv HOME /tmp --setenv LANG C.UTF-8 \
      --setenv PYTHONDONTWRITEBYTECODE 1 \
      prlimit --nproc=64 --as=536870912 --cpu=30 --fsize=10485760 -- \
      timeout 120 python3 -m pytest /workspace/tests -q -p no:cacheprovider
```
- `/home/pc`, `/mnt/c`, `~/.ssh`, `/run/user` unsichtbar (tmpfs-Overlays; verifiziert).
- Kein Netz (--unshare-net; verifiziert). PID-Isolation (--unshare-pid --die-with-parent),
  --new-session, clearenv, Limits via prlimit (nproc=64, as=512MB, cpu=30s, fsize=10MB) + timeout 120.
- Alle Parameter konfigurierbar (Defaults wie oben); Ergebnis wird begrenzt zurückgegeben
  (stdout/stderr je max 64 KB).

## 4. Context-Snapshots (Controller-Pflicht)

`context.build_agent_context` (Phase 2A) wird erweitert um `fixture_snapshot`:
Controller liest relevante Fixture-Dateien (nur Dateien im Fixture-Scope, max. 64 KB/Datei,
max. 20 Dateien), scannt mit Denylist (keine Secrets), und bindet sie als Text-Sektion in den
Agenten-Prompt. Rollen-Agenten haben zero dangerous or mutating tools (nur die harmlose
Status-Fähigkeit session_status auf direkten Turns) → sie können nur sehen, was der Snapshot enthält.
Snapshot wird als `agent_context_snapshots` persistiert (Hash + Summary; keine vollen Inhalte in
Events/Handoffs).

## 5. E2E-Task-Specs (verbindlich, vor Workflow-Start persistiert; Änderung nur durch Owner)

Fixture: `e2e-fixture/` — stdlib-only: `parser.py`, `service.py`, `tests/`. Kein Netz, keine Downloads.

**Task 1 (Standard-Workflow):** „Implementiere in `parser.py` die Funktion
`parse_duration(s: str) -> datetime.timedelta`:
- Format `XdYhZm` (Tage/Stunden/Minuten), Komponenten optional, Reihenfolge fix d→h→m,
  Case-insensitive (D/H/M), Leerzeichen zwischen Komponenten erlaubt.
- Fehler: leere/whitespace-Eingabe → ValueError('empty'), ungültiges Format/unbekannte Zeichen →
  ValueError('invalid'), negative Zahlen → ValueError('negative').
- `service.py`: `total_minutes(duration) -> int` (aufgerundet auf ganze Minuten) und
  `format_duration(duration) -> str` (kompaktes `XdYhZm`, 0-Komponenten weglassen, `0m` für Null).
- `tests/`: vollständige Tests für alle definierten Edge Cases (mindestens: leer, whitespace,
  ungültig, negativ, Groß/Klein, Reihenfolge, Teilkomponenten, 0d, Overflow >999 Tage,
  `1d2h3m` == 26h3m == 1563m).
Akzeptanz: `pytest tests/ -q` grün im bwrap-Test-Runner; nur Fixture-Dateien geändert.

**Task 2 (Rework-Workflow):** „Erweitere `parse_duration` um Dezimal-Komponenten (`1.5h`, `0.5d`,
`1.25m`) und `service.format_duration` um dezimale Ausgabe, wenn nötig. Exakte Präzisionsregel:
Summe der Komponenten muss exakt sein (keine Float-Artefakte; Rechnen in Mikrosekunden).
Zusätzlich `parse_duration` mit ISO-Stil `P1DT2H` ablehnen (ValueError('invalid')).“
Die Spezifikation enthält bewusst Fallstricke (Float-Rundung, gemischte Dezimal-/Ganz-Komponenten
wie `1.5h30m`, `0.5d12h` == genau 24h), sodass QA/Reviewer ein echtes Finding produzieren können.
Der Rework-Zyklus wird NICHT simuliert.

## 6. Isolationstests (Phase 2B §5 + Broker §9, deterministisch)

- Lead/Analyst/Reviewer Produktcode-Write → blockiert (Core: PermissionDenied + policy.role_violation)
- QA Produktcode-Write → blockiert; QA Testdatei-Write → erlaubt (Broker-Scope)
- Implementer Produktcode-Write → erlaubt; Implementer außerhalb Task-Scope → blockiert (Broker)
- Agent kann eigene Rechte nicht erhöhen / Tool Policy nicht verändern / Sandbox nicht deaktivieren
  (zero dangerous/mutating tools + Core-FORBIDDEN-Aktionen; Tests auf Core-Ebene)
- Agent kann Owner Gate nicht umgehen / andere Rolle nicht direkt starten (bestehende Provenance-Tests)
- inherited permissions → keine Eskalation (Leaf-Subagent-Restriktion dokumentiert + Core-Test)
- falsche Session/Rolle → Provenance-Reject (bestehend)
- externe Inhalte können Rechte nicht ändern (bestehend)
- Broker: absolute path escape, `..`, Symlink-Escape, Hardlinks, Special Files, Scope Escape,
  Config-/Secret-Pfade, TOCTOU-nahe Fälle, atomisches Schreiben (jeweils eigener Test)
- bwrap-Runner: minimales FS (host unsichtbar), kein Netz, Limits greifen, timeout greift,
  Exit-Code/Output korrekt zurückgegeben

## 7. Abnahmekriterien Phase 2B (unverändert aus Owner-Auftrag §14)

Alle Tests grün, keine Skips; keine offenen HIGH/CRITICAL, keine unakzeptierten relevanten MEDIUM;
Rollenrechte Core-seitig verifiziert; native Tool-Rechte verifiziert (zero dangerous/mutating tools; session_status-only, spawns fail closed);
echter vollständiger 5-Agenten-Workflow erfolgreich; echter Rework-Workflow erfolgreich;
Recovery/Reconciliation erfolgreich; Provenance mit echten Agent-IDs verifiziert;
Lead/Reviewer getrennte Sessions; nur Implementer schreibt Produktcode; QA nur Tests;
unerwartete/duplizierte Resultate fail-closed; keine Mail-Agent-/Visualizer-Änderung;
keine ungefragte OpenClaw-/Systemänderung; lokaler Commit, kein Push; Working Tree clean.

## 8. V2B Amendment (2026-08-28, aus dem echten E2E)

### 8.1 F9: State-Sync für Rework-Entscheid am ersten Gate (STATE MACHINE ÄNDERUNG)

Der echte E2E (Task 2) fand eine Inkonsistenz zwischen `_workflow_frontier`
und `_state_sync_plan`: Ein `rework`-Entscheid des Lead an **Position 0**
(STANDARD, Spec-Gate) startete im Frontier einen neuen Rework-Zyklus, aber die
State-Sync hatte dafür **keine Transition** — der Task blieb in `PLANNING`,
und der Implementer des Rework-Zyklus crashte mit
`InvalidTransition: expected task state REWORK, got PLANNING` (rollback
fail-closed, kein Teilzustand; vom echten Lauf reproduziert).

Fix (minimal, symmetrisch zur existierenden `REWORK -> PLANNING`-Transition):
- `state_machine._STATIC`: `PLANNING` erlaubt jetzt zusätzlich `REWORK`
  (V2B 16.3 F9).
- `core._state_sync_plan`: STANDARD pos0 + `decision == "rework"` →
  `(PLANNING, REWORK)`; REWORK-Start-Gate (pos0) + `decision == "cancel"` →
  `(REWORK, CANCELLED)` (Cancel am Rework-Start war vorher wirkungslos).
- `REWORK`-Start-Gate (pos0) mit accept/rework bleibt No-op (Zustand bleibt
  REWORK, damit der Implementer-Sync `REWORK -> IMPLEMENTING` greift).
- Regressionstests: `tests/test_phase2b_state_sync.py` (pos0-Rework-Zyklus,
  Cancel-Escape, Rework-Wiederholung).

Dokumentierte Limitation (unverändert): `cancel` an STANDARD pos0 ist
weiterhin wirkungslos (State-Machine erlaubt NEW/PLANNING -> CANCELLED nicht);
das entspricht dem Design (Cancel erst ab Entscheidungs-Gates).

### 8.2 bwrap-Runner: cwd-unabhängige Test-Imports

`build_command` setzt jetzt `--chdir /workspace` (SPEC V2B §3): Fixture-Tests,
die `from parser import ...` nutzen, funktionieren unabhängig vom Caller-cwd
(`python -m pytest` nimmt das cwd in `sys.path`).

### 8.3 E2E-Driver (`smoke/phase2b_e2e.py`)

Deterministischer Controller: init/status/next/run/init-rework/unexpected-smoke/
recovery-smoke. Rollen-Agenten laufen als direkte `openclaw agent`-Turns
(zero dangerous/mutating tools; nur session_status); der Controller wendet Patch-Sets über den Write-Broker an, testet im
bwrap-Namespace und zeichnet Test-Runs im Core auf. Enthält: Vokabular-Guard
(Deny-List-Compliance der Agenten, dokumentierte False-Positive-Vermeidung),
Content-Normalisierung (Agenten liefern Klartext; Base64 wird erkannt),
Positions-Guidance für Lead-Gates, Controller-Clarifications für die
Exakt-Präzisionsregel (Rundung half-up am Ende; `format_duration` nie `0m`
für Nicht-Null).

### 8.4 E2E-Ergebnis (echte Agenten, 2026-08-28)

- **Task 1 (Standard)**: DONE nach 13 Dispatches; echter Rework-Trigger durch
  Reviewer-Findings (Typannotationen LOW, Float-Arithmetik MEDIUM); korruptes
  Agent-Base64 (test_parser.py) wurde vom bwrap-Runner fail-closed erkannt und
  durch QA-Tests ersetzt; finale Akzeptanz: 23 Tests grün, approve, DONE.
- **Task 2 (Rework)**: DONE nach 7 Zyklen; entworfene Fallstricke (Dezimal-
  Präzision, `_fraction_decimal`-Denominator-Bug, Per-Komponenten-Truncation,
  Sub-µs-Rundung) wurden von QA und Reviewer gefunden und in Rework-Zyklen
  behoben; finale Akzeptanz: 72 Tests grün, approve, DONE. Enthält den
  F9-Core-Fix (8.1) als Live-Befund.
- **Unexpected-Event-Smoke**: Duplicate (gleiche run_id) → `duplicate`
  (idempotent, State unverändert); fremde run_id → `rejected` +
  Quarantäne (`run_id_mismatch`); Task DONE bleibt DONE.
- **Recovery-Smoke**: PENDING read-only → FAILED; RUNNING implementer →
  RECOVERY_PENDING (Ghost-Writer-Regel); Task → RECOVERING; Rollback ohne
  Teilzustand.
- Verifikation: 794 Tests grün (inkl. 3 neuer F9-Regressionstests); Visualizer
  und Mail-Agent unverändert; OpenClaw-Config identisch zum last-good-Stand
  (keine Config-Änderung durch Phase 2B); lokaler Commit, kein Push.

## 8.5 Closing-Review-Fixes (2026-08-28)

Umsetzung der bestätigten, supervisor-verifizierten Findings der unabhängigen
Sol-Review. Nur die bestätigten Findings wurden geändert; kein Re-Run des E2E,
keine Dispatch-/DB-/Config-Änderung, kein Push.

### 8.5.1 F1 (CRITICAL) — Sandbox-Escape: Workspace war read-write gemountet

`argent_core/sandbox_runner.py` `build_command` mountete den Workspace mit
`--bind <workspace> /workspace` (read-write); QA-verfasste Tests hätten
Produktdateien (parser.py/service.py) auf dem Host überschreiben und damit den
Broker-Scope umgehen können. Fix: `--ro-bind <workspace> /workspace`,
`--setenv PYTHONDONTWRITEBYTECODE 1`, und `-p no:cacheprovider` in der
Default-pytest-Invokation (pytest braucht keinen Schreibzugriff mehr).
Regressionstest `test_sandbox_cannot_overwrite_product_file` (Write-Versuch →
Exit != 0, Host-Datei unverändert) + `test_build_command_workspace_ro_bind`.
Verifikation: Suite grün; e2e-fixture läuft im Sandbox (90 Tests, exit 0).

### 8.5.2 F2 (HIGH) — Dropped Finding + Finding-Lifecycle

(a) `e2e-fixture/service.py` `format_duration`: eine Nicht-Null-Dauer konnte
als `0m` rendern (`timedelta(microseconds=6)` / `=1` → `0m`), weil
`_fraction_decimal` auf 6 Nachkommastellen kappte und `_rounded_fraction` auf
6 Stellen rundete. Fix: Cap auf 12 Stellen, exakter Terminierungs-Pfad bleibt,
`_rounded_fraction` rundet auf 12 Stellen und emittiert NIE eine zu Null
rundende Fraktion (Minimum = kleinster darstellbarer Wert). Verifikation:
`format_duration(6µs) == '0.0000001m'`, `(60µs) == '0.000001m'`,
`(1µs) == '0.000000016667m'`; alle round-trip-parsen zu Nicht-Null (1/6/60 µs).
Fixture-Tests `e2e-fixture/tests/test_duration_microseconds.py`.

(b) `argent_core/core.py` `_apply_role_effects`: akzeptierte Findings wurden
unabhängig von der Lead-Entscheidung als RESOLVED markiert — ein `rework`
markierte nie behobene Findings als erledigt (cycle-6-HIGH-Fall). Fix:
akzeptierte Findings nur bei `decision == "accept"` auflösen; bei
`rework`/`cancel`/`request_owner_gate` bleiben sie OPEN. Regressionstest
`test_rework_keeps_accepted_findings_open_then_accept_resolves`.

### 8.5.3 F3 (MEDIUM) — Encoding-Bypass

`smoke/phase2b_e2e.py` `_normalize_content`: doppelt-kodiertes
`base64(base64(x))` passierte den Round-Trip unverändert → der Broker dekodierte
nur einmal, der Deny-Scan sah nur Base64-Text, der innere Inhalt wurde nie
gescannt. Fix: vollständiges Entpacken verschachtelter Kodierungen (gültiges
Base64 + gültiges UTF-8 + verändert → dekodieren; Depth-Cap 4), dann entweder
als Base64 behalten (konsistenter Round-Trip) oder Klartext neu kodieren.
Unit-Tests `tests/test_phase2b_driver_normalize.py` (doppelt/plain/single/
invalides Base64/Whitespace) + Broker-Reject-Nachweis (entpackter Deny-Inhalt
wird abgelehnt). Verifikation: doppelt-kodiertes `"deploy with password
hunter2"` wird jetzt vollständig entpackt und vom Broker mit `content_denylist`
abgelehnt.

### 8.5.4 F4 (MEDIUM) — Überbreite Content-Deny-Liste

`argent_core/workspace_broker.py` `CONTENT_DENYLIST` war identisch zur vollen
`events.PRIVACY_DENYLIST` und lehnte legitimen Code ab (`token = lexer.next()`,
`data.decode()`, `request.body`, `different`). Fix: eingeengt auf Privacy-High-
Signal-Marker `{secret, password, api_key, credential, mail_content,
mail_address, email_address, recipient}`; `events.PRIVACY_DENYLIST` bleibt
unverändert (Envelope-Validierung in outputs.py/events). Vokabular-Guard in
`_build_prompt` auf zwei Tiers: Envelope/Findings → volle Deny-Liste;
Patch-DATEI-Inhalt → nur die eingeengte Content-Liste. Tests aktualisiert
(`test_content_denylist_token_now_accepted` + High-Signal-Reject/
Ordinary-Code-Accept-Matrix).

### 8.5.5 F5 (LOW) — No-op-Erkennung

`smoke/phase2b_e2e.py`: Ziel-Dateien werden vor/nach dem Broker-Apply gehasht;
ein byte-identischer Write wird als `no-op` geloggt (bei Rework-Implementer-
Zyklen zusätzlich als Hinweis in der Schritt-Ausgabe). Reines Reporting, keine
Verhaltensänderung.

### 8.5.6 F6 (LOW) — Docs: "0 Tools"-Behauptungen korrigiert

`docs/SPEC_V2B.md`: wörtliche "0 Tools"/"0 direkte Tools"-Behauptungen (§1, §5,
§7 sowie Header, §6, §8.3) ersetzt durch "zero dangerous or mutating tools; one
harmless status capability (session_status) on direct turns; subagent spawns
fail closed". §2-Regel 7 und der §3-Kommandoblock an F1/F4 angeglichen.

### 8.5.7 F7 — Mail-Agent (kein Code)

Keine Änderung im Repo. Der Abschlussbericht dokumentiert die engere, verifizierte
Aussage: während Phase 2B wurde kein Mail-Agent-Code geändert; der v2-Code
stammt aus der Zeit vor Phase 2B.

### Verifikation (Closing-Review-Fixes)

- Suite: `python3 -m pytest tests/ -q` → **805 passed** (0 skips, 0 xfails).
- Sandbox-Fixture: `run_tests('e2e-fixture')` → **exit_code 0, timed_out False,
  90 passed**.
- F2-Mikrosekunden: 1µs/6µs/60µs rendern nie `0m` und round-trip-parsen zu
  Nicht-Null.
- F3-Bypass: doppelt-kodierter Deny-Inhalt jetzt abgelehnt (`content_denylist`).
- F4-Matrix: reject secret/password/api_key/credential/recipient; accept
  token/code/diff/content/subject/body/api_token.
