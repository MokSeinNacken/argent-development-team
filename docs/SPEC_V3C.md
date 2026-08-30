# A. VERDICT: SPEC_COMPLETE

# B. ARGENT DEVELOPMENT TEAM — Phase 3C: Strict Telegram Owner-Gate Confirmation

Basis: Branch `phase-2c-persistent-supervisor`, Commit  
`adc39bcc3947705f9773332d3df2ca2f999db56e`, Marker `PHASE3B_GREEN`.

Phase 3C ergänzt den bestehenden persistenten Owner-Gate-Pfad ausschließlich um eine eng begrenzte Telegram-Bestätigung für ein bereits persistiertes `OWNER_APPROVAL_REQUIRED`-Gate. Telegram wird kein allgemeiner Steuerungs-, Prompt- oder Command-Kanal.

SPEC V2C bleibt für Supervisor-Lifecycle, Reconciliation, Owner-Gates, `WAITING_GATE`, `PRESENT_OWNER_GATE`, Gate-Closure und No-Background-Wake bindend. SPEC V3A bleibt für die persistente Outbox, begrenzte Outbound-Zustellung und Secret-Quellen bindend, soweit Phase 3C sie nicht ausdrücklich und owner-gated um einen Challenge- und Bestätigungstyp erweitert.

---

## 0.1 VERBINDLICHE OWNER-AMENDMENTS (2026-08-30, Planungsrunde)

Diese Amendments sind owner-verbindlich und überschreiben alle
widersprechenden Abschnitte dieser Spec. Phase 3C bleibt in der Planung;
es wird NICHTS implementiert, kein Inbound aktiviert, kein Live-Approval
getestet, keine Config-/Credential-Änderung vorgenommen.

### A1 — APPROVAL-UX: ausschließlich Telegram Inline-Buttons

- Phase 3C verwendet ausschließlich Telegram Inline-Buttons:
  `[ Genehmigen ] [ Ablehnen ] [ Details ]`.
- Approval/Reject über normale Telegram-Textnachrichten ist NICHT erlaubt.
- Es gibt KEINEN Parser für `APPROVE <token>` / `REJECT <token>`.
- Normale Telegram-Texte dürfen NIE `owner_approvals` verändern.
- Zulässige strukturierte Callback-Aktionen (callback_data):
  `A:<challenge>` (APPROVE_EXISTING_GATE),
  `R:<challenge>` (REJECT_EXISTING_GATE),
  `D:<challenge>` (SHOW_GATE_DETAILS).
- Das Callback-Format muss innerhalb der Telegram-Limits bleiben
  (callback_data <= 64 Bytes; `A:`/`R:`/`D:` + 43-Zeichen-Challenge ist ok).
- `D:` (Details) verbraucht die Challenge nicht und verändert keinen Gate.

### A2 — Challenge-Modell vereinfacht (CSPRNG, kein HMAC-Key)

- KEIN neuer persistenter HMAC-/Challenge-Secret-Key (Sol hat keinen
  zwingenden Sicherheitsgrund nachgewiesen; HMAC-Key-Modell aus §7 wird
  VERWORFEN).
- Token-Erzeugung: `secrets.token_urlsafe(32)` (CSPRNG, 32 zufällige Bytes,
  Base64URL ohne Padding, ca. 43 Zeichen opaque).
- In der DB wird AUSSCHLIESSLICH `sha256(token)` gespeichert.
- Zusätzlich persistent gebunden: `gate_id`, `task_id`, `binding_hash`,
  `expires_at`, `status`.
- Der Token selbst darf nicht in Repo, DB, allgemeinen Logs oder
  Supervisor-Events persistiert werden.
- Standard-TTL: 60 Minuten (3600 Sekunden).
- Single-use; jeder Zielzustand terminal.

### A3 — Owner-Authentifizierung: User- UND Chat-ID

- Nicht nur `chat.id` prüfen.
- Bei Private-Chat-Approval müssen BEIDE konsistent verifiziert werden:
  - erwarteter Owner `user/from.id`,
  - erwarteter Owner `chat.id`,
  gegen die bestehende einzelne Owner-Allowlist.
- Falscher User ODER falscher Chat => fail-closed (keine Aktion, kein
  Verbrauch, kein Rendern von Details).

### A4 — KEIN zweiter Telegram-Poller

- Vor jeder Inbound-Implementierung ist technisch festzustellen, wer aktuell
  den Telegram-Update-Stream für den bestehenden Bot besitzt (siehe
  §6.0 Untersuchungsergebnis).
- Solange unklar: KEIN eigener zweiter `getUpdates`-Poller, KEIN zweiter
  Consumer desselben Bot-Update-Streams.
- Bevorzugte Architektur (falls technisch möglich):
  `Telegram → bestehender OpenClaw Telegram Inbound → strikt begrenzter
  Approval-Callback-Dispatcher → TelegramApprovalInbox → persistenter
  owner_approvals-Ledger`.
- Falls das bestehende OpenClaw-Telegram-System Callback-Queries nicht
  sicher bereitstellen kann: STOP und Alternativen bewerten — NICHT
  improvisieren.

### A5 — Hosting / Wake

- SPEC_V3C definiert explizit, wie ein Approval empfangen wird, wenn keine
  TUI aktiv arbeitet: Wer empfängt dauerhaft? Wie gelangt der Callback zum
  Approval-Consumer? Wie wird danach Supervisor-Reconciliation ausgelöst?
  Was passiert bei Restart? Was bei Telegram-Ausfall?
- KEINE neue systemd-/Cron-Konfiguration in dieser Planungsrunde.

### A6 — Post-Decision-UX (best-effort, nicht autoritativ)

- KEIN neuer `OWNER_APPROVAL_DECIDED`-Outbox-Typ einführen (Abschnitt
  §9.4 wird verworfen).
- Nach APPROVE/REJECT darf der Telegram-Adapter best-effort:
  - `answerCallbackQuery` senden,
  - die ursprüngliche Approval-Nachricht auf „Genehmigt“ bzw.
    „Abgelehnt“ aktualisieren (`editMessageText`),
  - Buttons entfernen (`reply_markup` entfernen).
- Diese UI-Aktionen sind NICHT autoritativ.
- Fehlschlag beim Telegram-Edit darf NIE das bereits persistierte
  Approval/Reject zurückrollen.

### A7 — Existierende Sicherheitsregeln bleiben

- `owner_approvals` bleibt einzige Authority.
- `binding_hash` muss unverändert geprüft werden.
- CAS/Exactly-once, Replay-Schutz, persistentes Update-Dedup, Expiry auch
  bei REJECT, keine Scope-Erweiterung, keine Agenten-Selbstgenehmigung,
  keine freien Telegram-Kommandos, keine Shell-/Exec-Steuerung, keine
  Secrets, kein Live-Inbound, kein Live-Approval, keine Config-/Credential-
  Änderung, kein Push.

---

Read-only verifiziert:

- Repository: `/home/pc/projects/argent-development-team`
- Branch: `phase-2c-persistent-supervisor`
- HEAD: `adc39bcc3947705f9773332d3df2ca2f999db56e`
- Commit-Betreff: `Argent Phase 3B: Secure Live Activation of Telegram Owner Notifications (PHASE3B_GREEN)`
- Working Tree clean:
  - `git status --porcelain=v1 --untracked-files=all` ohne Ausgabe
  - `git diff --quiet` erfolgreich
  - `git diff --cached --quiet` erfolgreich
- keine Git-Remotes konfiguriert
- `/home/pc/.openclaw/openclaw.json` ist byte-identisch zu `/home/pc/.openclaw/openclaw.json.last-good`
- SHA-256 beider Config-Dateien:
  `34984a47cd0ab40bb808de1380b0abe48cef6832de69acc05525159f4103a110`
- `smoke/phase2b.db`:
  - mtime `2026-08-28 17:31:44.418212812 +0200`
  - Größe 872448 Bytes
- bestehendes Schema: V5
- bestehende Notification-Typen: `DONE`, `FAILED`, `BLOCKED`, `OWNER_APPROVAL_REQUIRED`
- bestehender Approval-TTL: 3600 Sekunden
- Code nennt die Gate-Statusklasse `ApprovalStatus`, nicht `GateStatus`; persistierte Werte sind lowercase:
  `pending`, `approved`, `rejected`, `consumed`, `expired`
- `Core.approve()` prüft Status, Binding-Hash und Expiry
- `Core.reject()` prüft Status und Binding, derzeit jedoch nicht `expires_at`; der Telegram-Pfad muss deshalb eine zusätzliche Expiry-Prüfung vor Reject erzwingen, ohne die bestehende öffentliche Reject-Semantik stillschweigend zu ändern
- Tests oder Smokes wurden in dieser read-only Spezifikationsphase nicht ausgeführt
- keine Datei, DB, Konfiguration oder Runtime wurde verändert oder aktiviert
- die referenzierte Datei `/home/pc/.openclaw/workspace/ARGENT_SUPERVISOR.md` war am angegebenen Pfad nicht vorhanden

---

## 1. Ziel, Scope und Nicht-Ziele

### 1.1 Verbindliches Ziel (A1)

Der Owner darf ein bereits existierendes, offenes, exakt gebundenes
`owner_approvals`-Gate aus dem einzigen bestehenden Telegram-Owner-
Privatchat durch genau EINE strukturierte Inline-Button-Aktion
deciden:

```text
[ Genehmigen ]  -> callback_data: A:<challenge>
[ Ablehnen ]    -> callback_data: R:<challenge>
[ Details ]     -> callback_data: D:<challenge>
```

Normale Telegram-Textnachrichten (insbesondere `APPROVE <token>` /
`REJECT <token>`) werden NIE akzeptiert und verändern NIE
`owner_approvals` (A1).

Die Entscheidung betrifft ausschließlich die durch die Challenge
gebundene bestehende Approval-Zeile.

`APPROVE` bewirkt ausschließlich:

```text
owner_approvals.status: pending -> approved
```

`REJECT` bewirkt ausschließlich den bestehenden Core-Reject-Pfad:

```text
owner_approvals.status: pending -> rejected
task: OWNER_APPROVAL_REQUIRED -> BLOCKED, soweit der bestehende Core dies verlangt
```

Telegram ruft niemals `execute_approved()` auf. Ein Telegram-Approve führt die freigegebene Aktion nicht aus und konsumiert das Gate nicht in den Status `consumed`.

### 1.2 Scope

- V5→V6-Migration im bestehenden SQLite-Ledger;
- persistente Approval-Challenges;
- persistente Telegram-Update-Deduplizierung und Offset-Verfolgung;
- exakt ein Owner-Privatchat, gebunden an die bestehende `allowFrom`-Quelle (A3: User- UND Chat-ID);
- ausschließlich strukturierte Inline-Button-Callbacks `A:/R:/D:` (A1), KEIN Text-Parser;
- task-, approval-, action-, scope- und binding-spezifische Challenge (CSPRNG, A2);
- gehashte Token-Persistenz; niemals Roh-Token in SQLite;
- ein atomarer Core-Bridge-Pfad für Update-Dedup, Challenge-CAS und Approval-Entscheidung;
- begrenzter, lokal ausgelöster Single-Pass-Consume über den BESTEHENDEN OpenClaw Inbound-Dispatcher (A4: KEIN eigener `getUpdates`-Abruf);
- deterministic Offline-Tests mit Fake Clock, Fake Callback Source und Mock-Transport;
- best-effort Post-Decision-UX via `answerCallbackQuery` + `editMessageText` (A6), ohne neuen Outbox-Typ.

### 1.3 Nicht-Ziele

- keine allgemeine Telegram-Command-Schnittstelle;
- keine freien Prompts oder Konversationen;
- kein Inbound außer der einen strukturierten Approval-Aktion;
- keine `/commands`, Buttons, Callback-Queries, Reactions, Webhooks oder Commands in Captions;
- keine Shell-, Exec-, Prozess-, Tool- oder Codeausführung;
- keine Code-, Patch-, Test- oder Repository-Änderungen über Telegram;
- keine Task-Erzeugung oder Änderung von Task-Zielen;
- keine Agent-Steuerung, kein Spawn, Stop, Retry oder Modellwechsel;
- keine Scope-, Permission-, Policy- oder Rollenänderung;
- keine Auswahl von Task, Gate, Action oder Scope aus Telegram-Freitext;
- keine automatische Approval- oder Reject-Entscheidung;
- keine Ausführung eines approved Gate;
- keine Approvals aus Gruppen, Channels, Threads oder anderen Chats;
- keine Mail-Agent- oder `mail-agent-v2-stable-canary`-Änderung;
- keine Visualizer-, Gateway-, Allowlist- oder OpenClaw-Konfigurationsänderung;
- keine neuen Agenten;
- kein Background-Wake, kein dauerhafter Poller, kein Webhook;
- kein systemd, cron oder Gateway-Autostart;
- kein Push, Deployment oder Stable-/Production-Promotion;
- keine Aktivierung von Inbound ohne neues Owner-Gate;
- kein Live-Approval-Test ohne separate ausdrückliche Autorisierung.

---

## 2. Sicherheitsinvarianten

1. `owner_approvals` bleibt die einzige Approval-Autorität.
2. Telegram authentisiert und bestätigt nur eine bereits persistierte Approval-Zeile.
3. Kein Telegram-Text bestimmt `task_id`, `approval_id`, `action`, `scope` oder `binding_hash`.
4. Ein Token kann genau eine von zwei Entscheidungen bestätigen: `APPROVE` oder `REJECT`.
5. Ein Token ist task- und gate-spezifisch und nicht auf andere Approvals übertragbar.
6. Jeder Token ist single-use und nach Entscheidung permanent ungültig.
7. Challenge-Expiry kann nie über `owner_approvals.expires_at` hinausreichen.
8. Eine Approval-Entscheidung und der Challenge-Konsum sind eine SQLite-Transaktion.
9. Telegram ruft nie `execute_approved()` oder einen Action-Handler auf.
10. Ungültige Daten erzeugen keine externe Antwort und keine fachliche Mutation.
11. Telegram-Ausfälle verändern keinen Supervisor-, Task-, Gate- oder Workflowzustand.
12. Inbound-Verarbeitung läuft nie synchron in `reconcile()` oder einer sicheren Supervisor-Aktion.
13. Roh-Token erscheinen nur im einen autorisierten `OWNER_APPROVAL_REQUIRED`-Nachrichtentext.
14. Bot-Credential, Challenge-Key, Owner-Chat-ID und Roh-Token erscheinen nie in DB, Git, Logs oder Supervisor-Events.

---

## 3. Trust Boundary und Threat Model

### 3.1 Vertraute Elemente

Vertraut werden ausschließlich:

- das SQLite-Core-Ledger;
- die persistierte `owner_approvals`-Zeile;
- der neu berechnete `gates.binding_hash(task_id, action, scope)`;
- die Challenge-Zeile und ihr Challenge-Binding;
- die persistente Update-ID-Deduplizierung;
- der exakt eine Owner-Privatchat aus der bestehenden `allowFrom`-Quelle;
- der injizierte Runtime-Secret-Provider;
- die lokal gestartete, eng begrenzte Consumer-Komponente;
- Telegram Bot API nur als authentisierter Transport, nicht als fachliche Autorität.

### 3.2 Untrusted Data

Ausnahmslos untrusted sind:

- der gesamte Update-Body;
- `message.text`;
- Chat-, User-, Sender-, Thread- und Forward-Metadaten;
- Telegram-Update-Zeitstempel;
- Update-Reihenfolge;
- Callback-, Reaction-, Edit-, Channel- und Media-Daten;
- Telegram-Fehlertexte und HTTP-Response-Bodies;
- weitergeleitete Nachrichten;
- jeder behauptete Task-, Gate-, Scope- oder Approval-Bezug in Telegram-Text.

### 3.3 Bedrohungen und Mitigations

| Bedrohung | Mitigation |
|---|---|
| Token-Guessing/Brute Force | 256-Bit pseudorandom Capability; maximal 10 Updates je manuellen Pass; keine Antwort als Oracle; keine Präfixsuche |
| Token-Leak in einen anderen Chat | Chat- und Sender-ID müssen beide exakt dem einzigen Owner entsprechen; falscher Chat wird vor Token-Lookup abgelehnt |
| Kompromittierter Owner-Account plus Token-Leak | Außerhalb der abdeckbaren Grenze eines Telegram-Bearer-Faktors; TTL und Single-Use begrenzen das Fenster |
| Replay desselben Updates | persistenter `update_id`-Primary-Key und monotoner Offset |
| Derselbe Token in neuer Update-ID | Challenge-CAS lässt nur eine Entscheidung gewinnen |
| Update-Reordering | gültige Batch-Einträge werden nach `update_id` sortiert; IDs unter dem Cursor sind stale/no-op |
| Duplicate Delivery | Update-PK und Challenge-CAS |
| Alte Nachricht | `message.date >= challenge.created_at`, `message.date < expires_at`, lokale Zeit ebenfalls vor Expiry |
| Falscher Chat | exakte private `chat.id`-Prüfung |
| Gespoofter Sender | `message.from.id == allowFrom == message.chat.id`, `is_bot != true`, `chat.type == private` |
| Scope Confusion | Task/Gate/Action/Scope ausschließlich aus Challenge→Approval-FK; voller Binding-Hash wird neu berechnet |
| Zwei Controller | SQLite `BEGIN IMMEDIATE`, Update-PK, Challenge-CAS und Approval-CAS |
| Crashfenster | ein gemeinsamer DB-Transakt; vor Commit vollständiger Rollback, nach Commit vollständige Entscheidung |
| Outbox-/Inbound-Race | Challenge muss `ISSUED` sein; gebrauchte/abgelaufene Challenge-bearing Outbox-Zeile wird vor erneutem Rendern verworfen |
| Malformed-Input-DoS | feste Response-/Batch-/Text-Limits; totaler Parser; keine Rekursion; keine Antwort; ein Pass ohne Retry-Schleife |
| Telegram-Outage | harter Request-Timeout, strukturierter Fehler, keine Supervisor-Auswirkung |
| DB locked | `timeout=0`; Pass endet sofort, Offset und Gate unverändert |
| Token aus anderem Gate | eindeutiger Token-Hash plus Challenge-Binding plus Approval-Binding |
| Manipulierte DB-Bindings | Challenge wird `INVALIDATED`; keine Entscheidung; kein Scope wird erraten |
| Unterschiedliche Payloads mit derselben Update-ID | erste committed Update-ID gewinnt; ein gefälschter Konflikt kann höchstens DoS, nie Approval-Erweiterung erzeugen |

---

## 4. Architektur

Neues enges Modul:

```text
argent_core/telegram_approvals.py
```

Komponenten (gemäß A1/A2/A4):

```python
class TelegramCallbackDispatcher(Protocol):
    """Strikt begrenzter Dispatcher: bekommt NUR strukturierte
    Callback-Queries vom bestehenden OpenClaw Telegram Inbound und
    liefert ausschließlich A:/R:/D:-Aktionen an die Inbox."""
    def deliver_callback(self, *, chat_id, from_id, callback_data) -> None: ...

class TelegramOwnerIdentitySource(Protocol):
    def telegram_bot_token(self) -> str | None: ...
    def telegram_owner_chat_id(self) -> str | None: ...
    def telegram_owner_user_id(self) -> str | None: ...

class TelegramApprovalInbox:
    def process_callbacks_once(self) -> InboundPassResult: ...
    def kick(self) -> None: ...
```

KEIN `ApprovalChallengeKeySource` (A2: kein HMAC-Key). KEIN eigener
`getUpdates`-Poller (A4): Inbound kommt ausschließlich über den
bestehenden OpenClaw Telegram Inbound / einen strikt begrenzten
Callback-Dispatcher. `TelegramApprovalInbox` besitzt keine API für
Prompts, Tasks, Agenten, Shell oder beliebige Core-Kommandos.

Konstanten (A2: TTL 60 Minuten):

```python
INBOUND_FETCH_LIMIT = 10
INBOUND_REQUEST_TIMEOUT_SECONDS = 5
INBOUND_RESPONSE_MAX_BYTES = 65536
INBOUND_TEXT_MAX_BYTES = 128
CHALLENGE_TOKEN_BYTES = 32
CHALLENGE_TTL_SECONDS = 3600   # A2: 60 Minuten
CALLBACK_ACTION_APPROVE = "A"
CALLBACK_ACTION_REJECT = "R"
CALLBACK_ACTION_DETAILS = "D"
```

`kick()` ist optional, O(1), catch-all und startet höchstens einen
daemonisierten prozesslokalen Worker für genau einen Pass. Phase 3C
verdrahtet diesen Kick standardmäßig nicht mit dem Supervisor-Loop. Eine
solche Verdrahtung benötigt ein neues Owner-Gate (A5).

---

## 5. Strikt begrenzte Callback-Validierung (A1/A3)

Akzeptiert wird AUSSCHLIESSLICH eine strukturierte Telegram
`callback_query` auf eine von Argent gesendete Inline-Button-Nachricht
(`[ Genehmigen ] [ Ablehnen ] [ Details ]`). Normale Textnachrichten
werden NIE akzeptiert: Es gibt KEINEN Parser für `APPROVE <token>` /
`REJECT <token>`; normale Telegram-Texte verändern NIE `owner_approvals`.

Identitätsbedingungen (A3 — beide IDs gegen die einzelne Owner-Allowlist):

```text
callback_query.message.chat.type == "private"
canonical(callback_query.message.chat.id) == configured_owner_chat_id
canonical(callback_query.from.id) == configured_owner_user_id
callback_query.from.is_bot is not true
canonical(chat.id) == canonical(from.id)   # Private-Chat-Konsistenz
```

Falscher User ODER falscher Chat => fail-closed: keine Aktion, kein
Challenge-Verbrauch, kein Rendern von Details, best-effort
`answerCallbackQuery` mit neutraler Ablehnung.

Callback-Daten-Grammatik (A1):

```regex
\A([ARD]):([A-Za-z0-9_-]{43})\Z
```

Zusätzliche Regeln:

- `A` = APPROVE_EXISTING_GATE, `R` = REJECT_EXISTING_GATE,
  `D` = SHOW_GATE_DETAILS;
- ASCII-only, exakt ein `:` Trennzeichen, keine weitere Prosa;
- Challenge = die opaque 43-Zeichen-CSPRNG-Challenge (A2);
- `D` verbraucht die Challenge NICHT und verändert keinen Gate-Zustand;
- unbekannte Aktion/Form => fail-closed, kein Verbrauch;
- Telegram-Limit: callback_data <= 64 Bytes (Format passt).

Abgelehnt werden alle anderen Update-Arten und Formen (edited_message,
channel_post, inline_query, poll, message_reaction, Caption-/Media-
/Dateiinhalte, Updates ohne nichtnegativen Integer-`update_id`, beliebige
Text-/Prompt-/Command-Nachrichten).

`reply_to_message` wird ignoriert; ausschließlich die strukturierte
Callback-Aktion wird ausgewertet. Gruppen-Threads werden nie akzeptiert.

---

## 6. Bounded Telegram Update Flow

### 6.0 Untersuchungsergebnis Update-Stream-Besitz (read-only, 2026-08-30)

A4-Pflichtprüfung — wer besitzt den Telegram-Update-Stream für den
bestehenden Bot? Ergebnis der read-only Untersuchung der OpenClaw-Doku
(`docs/channels/telegram.md`, `docs/plugins/sdk-channel-plugins.md`,
`docs/tools/exec-approvals-advanced.md`):

- **Der OpenClaw-Gateway besitzt den Stream**: Telegram läuft im
  Gateway-Prozess; Long-Polling ist der Default-Transport (grammY
  Runner); „Each gateway process guards long polling so only one active
  poller can use a bot token at a time“; persistente `getUpdates`-409-
  Konflikte zeigen einen zweiten Poller. Ein eigener Argent-`getUpdates`-
  Poller würde den Stream teilen/konfligieren → VERBOTEN (A4).
- **Inline-Buttons/Callback-Queries werden unterstützt**:
  `channels.telegram.capabilities.inlineButtons` (Scopes `off|dm|group|
  all|allowlist`, Default `allowlist`); Nachrichten-Aktionen mit
  `buttons`/`callback_data`; „Callback clicks not claimed by a registered
  plugin interactive handler are passed to the agent as text:
  `callback_data: <value>`“.
- **Es existiert ein Plugin-Interactive-Handler-Mechanismus**
  (`interactions`-Hooks in Channel-Plugins) und ein etablierter
  Telegram-Native-Approval-Präzedenzfall (Exec-Approvals mit Inline-
  Buttons, opaque Callback-Payloads, `/approve`-Fallback).
- **Konsequenz**: Die bevorzugte Architektur ist technisch darstellbar
  über den bestehenden OpenClaw Telegram Inbound (Gateway-Long-Poll)
  mit einem strikt begrenzten Approval-Callback-Dispatcher (Plugin-
  Interactive-Handler oder gleichwertige Callback-Weiterleitung), der
  ausschließlich `A:/R:/D:`-Callback-Queries an
  `TelegramApprovalInbox` reicht. Ein zweiter Poller wird NICHT gebaut.
- **Restoffene technische Klärung vor Implementierung** (Owner-Gate):
  exakte Dispatcher-Hostung (Plugin vs. Gateway-Hook) und die sichere
  Callback-Weiterleitung ohne Agenten-Textpfad; bis dahin kein Inbound.

### 6.1 Aktivierungsmodell

Phase 3C installiert keinen Listener und keinen Poller.

Nach separater Owner-Autorisierung (und geklärter Dispatcher-Hostung):

```text
argent telegram-approval-consume-once
```

Ein Aufruf:

1. liest den persistenten Verarbeitungs-Cursor;
2. konsumiert genau EINEN begrenzten Stapel strukturierter Callbacks vom
   bestehenden Inbound-Dispatcher (kein Bot-API-`getUpdates`-Aufruf);
3. verarbeitet jede Callback-Aktion in einem kurzen DB-Transakt;
4. kehrt ohne Sleep, Long Poll oder internen Retry zurück.

### 6.2 Update-ID und Offset

- `telegram_update_log.update_id` ist der restart-feste Dedup-Key für
  die vom Dispatcher gelieferten Callback-Queries.
- `telegram_inbound_state` hält den letzten verarbeiteten Cursor.
- Cursor-Advance und Update-Outcome committen in derselben Transaktion wie eine mögliche Approval-Entscheidung.
- Ein Update unter dem Cursor ist alt und no-op.
- Ein bereits persistiertes Update wird niemals nochmals fachlich verarbeitet.
- Kann ein Update wegen DB-Lock nicht committen, wird der Cursor nicht verschoben.
- Ein API-Objekt ohne brauchbare Update-ID beendet den Pass mit einem strukturierten Transport-/Formatfehler. Es gibt keine interne Schleife; daher kein Livelock im Supervisor.

*Implementierungsnotiz (Phase 3C-A, Option 1):* Die akzeptierte
`update_id`-Domäne ist exakt `0 <= update_id <= 2**63 - 2`. Der Endwert
`2**63 - 1` ist zwar als `telegram_update_log`-Primary-Key darstellbar, aber
sein Cursor-Advance (`update_id + 1` == 2**63) überläuft SQLites vorzeichenbehaftetes
64-Bit-INTEGER. Er wird deshalb als `MALFORMED` verworfen — persistiert als
`MALFORMED`-Logzeile mit genau-einmaligem Cursor-Advance, der bei `2**63 - 1`
sättigt (nie `2**63` persistiert); Replay-Dedup an diesem Endwert stützt sich auf
den Update-PK. Werte `>= 2**63` (sowie negative/nicht-integer/`bool`) sind nicht
darstellbar und bleiben ein nacktes `MALFORMED` ohne persistierte Zeile.

### 6.3 Zwei Fetcher

Zwei lokale Controller dürfen dieselbe Offset-Sicht lesen und dasselbe Update erhalten. Nur einer kann den Update-PK und Challenge-CAS committen. Der andere erhält `DUPLICATE` oder `USED_TOKEN` und verändert das Gate nicht.

---

## 7. Token-Challenge-Modell (A2: CSPRNG, kein HMAC-Key)

### 7.1 Erzeugung

Eine Challenge wird nur erzeugt, wenn alle Bedingungen im selben Supervisor-Commit erfüllt sind:

- `_decide_gate()` ergibt erstmals `PRESENT_OWNER_GATE`;
- Gate existiert;
- `owner_approvals.status == pending`;
- Task ist `OWNER_APPROVAL_REQUIRED`;
- `binding_hash` stimmt mit Task, Action und Scope überein;
- keine aktive Challenge für diese Approval-ID existiert.

Zufallswerte (A2 — CSPRNG, KEIN HMAC-Key, KEIN persistenter Secret):

```python
challenge_id = "challenge:" + uuid4().hex
token = secrets.token_urlsafe(32)   # CSPRNG, 32 Bytes, ~43 Zeichen, Base64URL o. Padding

token_hash = sha256(token.encode("ascii"))
```

Der Capability-Token wird NICHT aus der Task-ID erzeugt und ist nicht
daraus ableitbar: er ist reiner CSPRNG-Zufall. Es existiert KEIN
`ApprovalChallengeKeySource`, KEIN HMAC-Key, KEIN domain-separated
Runtime-Key, KEINE Key-Rotation (A2).

### 7.2 Speicherung

Persistiert werden ausschließlich:

- Challenge-ID;
- `gate_id` (approval_id), `task_id`, `binding_hash` (A2-Bindung);
- `token_hash` (nur SHA-256, nie der Roh-Token);
- Status und Zeitstempel;
- `expires_at`;
- zugehörige Outbox-ID.

Nicht persistiert werden:

- Roh-Token;
- Bot-Credential;
- Owner-Chat-/User-ID;
- Telegram-Nachrichtentext.

### 7.3 Statusmaschine

```text
ISSUED
  -> CONSUMED_APPROVED
  -> CONSUMED_REJECTED
  -> EXPIRED
  -> INVALIDATED
```

Alle Zielzustände sind terminal. Es gibt keinen Übergang zurück zu `ISSUED`.
`D:`-Details verbraucht die Challenge NICHT (A1).

### 7.4 Expiry (A2: 60 Minuten)

```text
challenge.expires_at =
    min(challenge.created_at + 3600 Sekunden,
        owner_approval.expires_at)
```

Wenn die Approval bereits abgelaufen ist, wird keine Challenge erzeugt.
Es gibt keine automatische Challenge-Erneuerung. Eine spätere
Reissue-Funktion müsste die alte Challenge terminal invalidieren, einen
neuen Token erzeugen und eine neue deduplizierte Notification erzeugen;
sie ist nicht Teil der Basis-Phase 3C.

---

## 8. Persistenz und Schema V6

### 8.1 Neue Tabellen

```sql
CREATE TABLE IF NOT EXISTS approval_challenges (
    id                       TEXT PRIMARY KEY,
    approval_id              TEXT NOT NULL
                             REFERENCES owner_approvals(id) ON DELETE CASCADE,
    task_id                  TEXT NOT NULL
                             REFERENCES tasks(id) ON DELETE CASCADE,
    supervisor_job_id        TEXT NOT NULL
                             REFERENCES supervisor_jobs(id) ON DELETE CASCADE,
    binding_hash             TEXT NOT NULL
                             CHECK (length(binding_hash) = 64),
    notification_outbox_id   TEXT UNIQUE
                             REFERENCES notification_outbox(id)
                             ON DELETE SET NULL,
    token_hash               TEXT NOT NULL
                             CHECK (length(token_hash) = 64),
    status                   TEXT NOT NULL CHECK (status IN
                             ('ISSUED','CONSUMED_APPROVED',
                              'CONSUMED_REJECTED','EXPIRED','INVALIDATED')),
    created_at               TEXT NOT NULL,
    expires_at               TEXT NOT NULL,
    consumed_at              TEXT,
    consumed_update_id       INTEGER UNIQUE,
    invalidated_at           TEXT,
    CHECK (expires_at > created_at),
    CHECK (
        (status = 'ISSUED'
         AND consumed_at IS NULL
         AND consumed_update_id IS NULL
         AND invalidated_at IS NULL)
        OR
        (status IN ('CONSUMED_APPROVED','CONSUMED_REJECTED')
         AND consumed_at IS NOT NULL
         AND consumed_update_id IS NOT NULL
         AND invalidated_at IS NULL)
        OR
        (status IN ('EXPIRED','INVALIDATED')
         AND consumed_at IS NULL
         AND consumed_update_id IS NULL
         AND invalidated_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_challenges_token_hash
    ON approval_challenges(token_hash);

CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_challenges_active_approval
    ON approval_challenges(approval_id)
    WHERE status = 'ISSUED';

CREATE INDEX IF NOT EXISTS idx_approval_challenges_due
    ON approval_challenges(status, expires_at);

CREATE INDEX IF NOT EXISTS idx_approval_challenges_approval
    ON approval_challenges(approval_id, created_at);
```

```sql
CREATE TABLE IF NOT EXISTS telegram_update_log (
    update_id          INTEGER PRIMARY KEY CHECK (update_id >= 0),
    message_date       INTEGER,
    chat_authorized    INTEGER NOT NULL
                       CHECK (chat_authorized IN (0,1)),
    sender_authorized  INTEGER NOT NULL
                       CHECK (sender_authorized IN (0,1)),
    decision           TEXT CHECK
                       (decision IS NULL OR decision IN ('APPROVE','REJECT')),
    challenge_id       TEXT
                       REFERENCES approval_challenges(id) ON DELETE SET NULL,
    approval_id        TEXT
                       REFERENCES owner_approvals(id) ON DELETE SET NULL,
    outcome            TEXT NOT NULL CHECK (outcome IN (
                       'PROCESSING',
                       'APPROVED',
                       'REJECTED',
                       'WRONG_CHAT',
                       'SPOOFED_SENDER',
                       'MALFORMED',
                       'UNKNOWN_TOKEN',
                       'USED_TOKEN',
                       'EXPIRED_TOKEN',
                       'EXPIRED_APPROVAL',
                       'APPROVAL_NOT_PENDING',
                       'STALE_MESSAGE',
                       'BINDING_MISMATCH')),
    received_at        TEXT NOT NULL,
    processed_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_telegram_update_log_outcome
    ON telegram_update_log(outcome, processed_at);

CREATE INDEX IF NOT EXISTS idx_telegram_update_log_challenge
    ON telegram_update_log(challenge_id, processed_at);
```

```sql
CREATE TABLE IF NOT EXISTS telegram_inbound_state (
    stream_id       TEXT PRIMARY KEY
                    CHECK (stream_id = 'telegram-owner-approval-v1'),
    next_update_id  INTEGER NOT NULL CHECK (next_update_id >= 0),
    updated_at      TEXT NOT NULL
);

INSERT INTO telegram_inbound_state (
    stream_id, next_update_id, updated_at
)
SELECT 'telegram-owner-approval-v1', 0, CURRENT_TIMESTAMP
WHERE NOT EXISTS (
    SELECT 1 FROM telegram_inbound_state
    WHERE stream_id = 'telegram-owner-approval-v1'
);
```

`PROCESSING` darf nur innerhalb eines offenen Transakts sichtbar sein. Jede öffentliche Consumer-Rückkehr muss es auf einen terminalen Outcome aktualisiert haben. Crash vor Commit rollt die Zeile zurück. Acceptance-Tests müssen beweisen, dass keine committed `PROCESSING`-Zeile entsteht.

### 8.2 Keine Chat-ID-Spalte

`telegram_update_log` speichert bewusst nicht die echte Chat- oder Sender-ID. Persistiert werden nur `chat_authorized` und `sender_authorized`. Dies erhält die Phase-3A-Regel, nach der die Zielkennung nicht in SQLite liegt.

### 8.3 KEINE Confirmation-Notification (A6)

`DONE`, `FAILED` oder `BLOCKED` werden nicht als Approval-Bestätigung
missbraucht — aber es wird auch KEIN neuer Typ `OWNER_APPROVAL_DECIDED`
eingeführt (A6). Der frühere Abschnitt 8.3 wird VERWORFEN.

Das `notification_outbox.notification_type`-CHECK bleibt unverändert auf
den vier Phase-3A-Typen:

```sql
CHECK (notification_type IN (
    'DONE',
    'FAILED',
    'BLOCKED',
    'OWNER_APPROVAL_REQUIRED'
))
```

Ein V6-Tabellen-Rebuild der `notification_outbox` ist damit NICHT für
einen neuen Typ erforderlich. Sofern der V6-Migrationsweg aus anderen
Gründen (z. B. neue Challenge-/Update-Tabellen) einen Rebuild braucht,
gilt der weiter unten beschriebene transaktionale Ablauf unverändert —
jedoch ohne Typ-Erweiterung.
9. erst zuletzt `schema_version = 6`;
10. jeder Fehler rollt DDL, Copy und Versionswechsel vollständig zurück.

Es gibt kein Backfill von Challenges oder Update-Logs für bereits präsentierte Gates.

---

## 9. Outbound-Integration

### 9.1 Gate-Notification und Challenge atomar

Im bestehenden `_commit()`-Transakt für `PRESENT_OWNER_GATE`:

1. Gate-Binding erneut prüfen;
2. Challenge und Token in Memory erzeugen;
3. nur `token_hash` und Challenge-Binding persistieren;
4. `OWNER_APPROVAL_REQUIRED`-Outbox-Zeile erzeugen;
5. Outbox-ID in der Challenge binden;
6. WAITING_GATE-Projektion committen.

Es darf keinen Zustand geben mit:

- Challenge ohne passende Gate-Notification;
- Gate-Notification mit Challenge-ID ohne Challenge;
- WAITING_GATE mit halb persistierter Challenge.

Fehlt der Challenge-Key, bleibt der Supervisor non-blocking: WAITING_GATE und der bisherige rein informative Phase-3A-Pfad dürfen weiter committen; es wird keine erratene oder schwache Challenge erzeugt.

### 9.2 Persistierter Payload

Der Outbox-Payload enthält zusätzlich nur:

```text
challenge_id
challenge_expires_at
```

Der Roh-Token ist nicht Teil von `payload_json` oder `payload_hash`.

Der Delivery-Renderer (A2 — kein HMAC-Key, kein Nonce):

1. lädt Challenge und Approval;
2. verlangt Challenge `ISSUED` und unexpired;
3. prüft den vollen Approval-`binding_hash`;
4. prüft die A2-Bindung (gate_id/task_id/binding_hash) der Challenge;
5. lädt die gespeicherte opaque Challenge (aus der Outbox-/Challenge-
   Zeile) als Button-Callback-Referenz;
6. verifiziert die Challenge-Integrität (kein Roh-Token im Text; nur
   callback_data `A:/R:/D:<challenge>`);
7. erzeugt erst dann Nachrichtentext + Inline-Buttons.

Bei jeder Abweichung: kein Send, Outbox `DISCARDED` mit allowlisted internem Fehlercode.

### 9.3 Exaktes Approval-Template mit Inline-Buttons (A1)

```text
ARGENT · OWNER APPROVAL REQUIRED
Job: <supervisor_job_id>
Task: <task_id>
Gate: <approval_id>
Scope ref: <scope_ref>
Valid until: <challenge_expires_at>
Time: <event_at>
Ref: <dedup-ref>
```

Inline-Buttons (callback_data, je <= 64 Bytes):

```text
[ Genehmigen ]  -> callback_data: A:<challenge>
[ Ablehnen ]    -> callback_data: R:<challenge>
[ Details ]     -> callback_data: D:<challenge>
```

Der Roh-Token steht NIEMALS im Nachrichtentext (A1). Die Buttons tragen
ausschließlich die opaque Challenge als Referenz; die Autorisierung
erfolgt über die Challenge-Bindung im Ledger. Dies ist die einzige
erlaubte Offenlegung der Challenge (als Button-Callback-Referenz).

### 9.4 KEINE OWNER_APPROVAL_DECIDED-Notification (A6)

Der frühere Abschnitt „9.4 Exaktes Confirmation-Template“ mit dem neuen
Outbox-Typ `OWNER_APPROVAL_DECIDED` wird VERWORFEN. Es wird kein neuer
Outbox-/Notification-Typ eingeführt.

Nach APPROVE/REJECT darf der Telegram-Adapter best-effort (A6):

- `answerCallbackQuery` senden;
- die ursprüngliche Approval-Nachricht auf „Genehmigt“ bzw. „Abgelehnt“
  aktualisieren (`editMessageText`);
- die Buttons entfernen (`reply_markup` = leer).

Diese UI-Aktionen sind NICHT autoritativ. Ein Fehlschlag beim Telegram-
Edit (Offline, API-Fehler, Rate-Limit) rollt das bereits persistierte
Approval/Reject NIE zurück und erzeugt keine Outbox-Zeile. Der
`owner_approvals`-Ledger bleibt die einzige Quelle der Entscheidung.

### 9.5 Outbox-/Inbound-Race

Wenn eine Challenge bereits konsumiert oder abgelaufen ist, darf ein noch nicht gesendeter oder nach Crash erneut beanspruchter `OWNER_APPROVAL_REQUIRED`-Eintrag den Token nicht mehr rendern. Er wird `DISCARDED/CHALLENGE_INACTIVE`.

Ein bereits von Telegram angenommener Send kann nicht zurückgerufen werden. Sein Token ist durch Challenge-CAS bereits permanent unbrauchbar.

---

## 10. Atomarer Approve-/Reject-Pfad

### 10.1 Notwendiger Core-Bridge-Pfad

Eine Implementierung mit diesen separaten Schritten ist verboten:

```text
challenge consume commit
Core.approve commit
update processed commit
```

Sie hätte nicht schließbare Crashfenster.

Stattdessen wird ein enger Core-Einstieg ergänzt, beispielsweise:

```python
Core.decide_owner_approval_via_telegram(
    *,
    update_id: int,
    message_date: int,
    decision: Literal["APPROVE", "REJECT"],
    token_hash: str,
    owner_source: str,
) -> TelegramDecisionResult
```

Er akzeptiert keinen Task, keine Gate-ID, keine Action und keinen Scope aus Telegram.

Die bestehende Approval-Arbeit wird in interne „already inside transaction“-Helper extrahiert:

```text
_approve_work_in_transaction(...)
_reject_work_in_transaction(...)
```

Die öffentlichen `approve()`/`reject()`-Methoden und der Telegram-Bridge-Pfad verwenden dieselben Binding-, Status-, Event- und Task-Transition-Regeln.

### 10.2 Gemeinsamer Transakt

Für ein syntaktisch gültiges, autorisiertes Update:

```sql
BEGIN IMMEDIATE;
```

Dann:

1. `telegram_update_log` mit `outcome='PROCESSING'` einfügen.
   - PK-Konflikt: Duplicate, keine Gate-Mutation.
2. Challenge ausschließlich über den vollständigen SHA-256-Token-Hash laden.
3. Challenge muss `ISSUED` sein.
4. Lokale Zeit und `message.date` müssen im Challenge-Fenster liegen.
5. Approval über FK laden.
6. Approval muss `pending` sein.
7. `approval.expires_at > now`.
8. `approval.task_id == challenge.task_id`.
9. `gates.binding_hash(approval.task_id, approval.action, approval.scope)` muss dem gespeicherten Approval-Binding entsprechen.
10. Challenge-Binding aus sämtlichen persistierten Feldern neu berechnen.
11. Challenge-CAS:

```sql
UPDATE approval_challenges
SET status = CASE
        WHEN :decision = 'APPROVE'
        THEN 'CONSUMED_APPROVED'
        ELSE 'CONSUMED_REJECTED'
    END,
    consumed_at = :now,
    consumed_update_id = :update_id
WHERE id = :challenge_id
  AND status = 'ISSUED'
  AND token_hash = :token_hash
  AND expires_at > :now;
```

`rowcount` muss exakt 1 sein.

12. Für `APPROVE` denselben Core-CAS wie `_mark_approved()` ausführen.
13. Für `REJECT` vor dem bestehenden Reject-CAS ausdrücklich Expiry prüfen; dann dieselbe Reject-/Task-BLOCKED-Logik verwenden.
14. `command_idempotency` mit einem stabilen Key persistieren:

```text
telegram-owner-approval:<update_id>:<challenge_id>:<decision>
```

15. Confirmation-Outbox-Zeile dedup-geschützt einfügen.
16. Update-Outcome auf `APPROVED` oder `REJECTED` setzen.
17. Cursor atomar auf mindestens `update_id + 1` setzen.
18. Commit.

Jeder Fehler vor Commit rollt alle Punkte zurück.

### 10.3 Approval ist nicht Execution

`APPROVE` endet bei `owner_approvals.status='approved'`.

Nicht ausgeführt werden:

- `execute_approved`;
- Action-Handler;
- Deploy;
- External Send;
- Secret-/Allowlist-/Gateway-Änderung;
- Supervisor-Fortsetzung außerhalb der bestehenden `gate_approved_waiting_execution`-Semantik.

---

## 11. Crashfenster und Exactly-once

| Crashpunkt | Ergebnis |
|---|---|
| nach Update-Dedup-Insert, vor Challenge-CAS | gesamter Transakt rollback; Update wird nach Restart erneut versucht |
| nach Challenge-CAS, vor Core-Entscheidung | gesamter Transakt rollback; Challenge bleibt `ISSUED` |
| nach Core-Entscheidung, vor Challenge-Markierung | durch vorgeschriebene Reihenfolge nicht vorhanden; beide liegen im selben uncommitted Transakt |
| nach Core-Entscheidung, vor Update-Outcome | gesamter Transakt rollback |
| nach Confirmation-Outbox-Insert, vor Commit | gesamter Transakt rollback |
| unmittelbar nach Commit | Update, Challenge, Approval, Event, Task-Transition, Idempotency und Confirmation-Outbox sind vollständig vorhanden |
| Replay nach Commit | Update-PK-Konflikt; fachlicher no-op |
| zweiter Controller, gleiche Update-ID | genau ein Update-PK gewinnt |
| zweiter Controller, andere Update-ID, gleicher Token | genau ein Challenge-CAS gewinnt |
| zwei konkurrierende unterschiedliche Entscheidungen | erste committed Entscheidung gewinnt; zweite `USED_TOKEN`/`APPROVAL_NOT_PENDING` |

Damit ist die lokale fachliche Gate-Entscheidung exactly-once.

Die externe Bestätigungsnotification bleibt wegen des Telegram-Send-Crashfensters at-least-once, begrenzt durch die bestehende Outbox.

---

## 12. Expiry-Semantik

- Challenge-Expiry und Approval-Expiry werden unabhängig geprüft.
- Effektive Challenge-Expiry ist das frühere beider Enden.
- Abgelaufener Challenge-Token wird niemals akzeptiert.
- Abgelaufene Approval wird niemals über Telegram approved oder rejected.
- Trifft ein Update auf eine abgelaufene Approval, darf der bestehende Core-Expiry-/Release-Pfad im selben Transakt ausgeführt werden; keine Telegram-Entscheidung wird verbucht.
- Der bestehende öffentliche `Core.reject()`-Pfad wird nicht als Beleg einer Expiry-Prüfung behandelt, da der aktuelle Code keine solche Prüfung enthält.
- Ein Challenge-Ablauf verlängert niemals `owner_approvals.expires_at`.
- Kein Refresh oder neue Notification erfolgt automatisch.

---

## 13. Restart und Recovery

Nach Prozessneustart stammen aus SQLite:

- nächster Telegram-Offset;
- bereits verarbeitete Update-IDs;
- Challenge-Status;
- Token-Hash;
- Challenge-Binding;
- Expiry;
- Gate-Status;
- Confirmation-Outbox-Status.

Invarianten:

1. Processed Updates werden nicht erneut entschieden.
2. Consumed/expired/invalidated Challenges werden nie wieder `ISSUED`.
3. Ein Restart erzeugt keine neue Challenge für ein bereits präsentiertes Gate.
4. Ein Restart erzeugt keine neue Gate-Notification.
5. Ein Restart erzeugt keine zweite Confirmation-Outbox-Zeile.
6. Pending Outbound-Notifications beeinflussen die Approval-Autorität nicht.
7. Inbound-Verarbeitung blockiert keinen Supervisor-Thread.
8. Ein beschädigter oder nicht lesbarer Challenge-Key führt zu no-send, nicht zu Gate-Mutation.
9. Es gibt kein State-Scan-basiertes Challenge-Backfill.
10. Telegram öffnet kein geschlossenes Gate wieder.

---

## 14. Failure Modes

| Fehler | Verhalten |
|---|---|
| Nachricht malformed | persistenter `MALFORMED`-Outcome, Cursor vor, keine Antwort, keine Mutation |
| falscher Chat | `WRONG_CHAT`, kein Token-Lookup |
| Sender stimmt nicht | `SPOOFED_SENDER`, keine Mutation |
| unbekannter Token | `UNKNOWN_TOKEN`, keine externe Antwort |
| bereits gebrauchter Token | `USED_TOKEN`, keine Mutation |
| abgelaufener Token | Challenge atomar `EXPIRED`, keine Gate-Entscheidung |
| abgelaufene Approval | `EXPIRED_APPROVAL`, bestehender Core-Expiry-Pfad, keine Approve-/Reject-Entscheidung |
| Gate bereits approved | `APPROVAL_NOT_PENDING`; kein zweites Approve |
| Gate rejected/consumed/expired | `APPROVAL_NOT_PENDING`; kein Wiederöffnen |
| Binding mismatch | Challenge `INVALIDATED`; keine Entscheidung |
| Replay gleiche Update-ID | no-op |
| Duplicate mit neuer Update-ID | Challenge-CAS verliert |
| zwei Controller | genau ein Commit gewinnt |
| DB locked | sofortiger Pass-Abbruch; kein Cursor-Advance |
| DB-Constraint-Fehler | vollständiger Rollback |
| Telegram API down | strukturierter Dispatcher-Fehler, kein Supervisor-Effekt |
| Telegram Timeout | maximal 5 Sekunden im separaten Pass/Worker |
| Telegram malformed response | bounded fail-closed, kein fachlicher Zustand |
| Callback-Stream-Konflikt (zweiter Poller) | verboten (A4); nur bestehender OpenClaw Inbound |
| Post-Decision-Edit scheitert | best-effort (A6); Entscheidung bleibt committed |
| Approval-Notification-Send scheitert | Gate bleibt pending; keine automatische Approval |
| unbounded Test-Mock | Supervisor bleibt unbeeinflusst, da Inbound nicht synchron eingebunden ist; Test-Worker wird explizit freigegeben |
| riesiger Input | Response-/Batch-/Text-Limits; kein Hashing unbeschränkter Texte |
| wiederholt malformed | höchstens zehn Rows pro explizitem Pass; kein autonomer Livelock |

*Implementierungsnotiz (Phase 3C-A, Runde E):* Der atomare Inbound-Pass läuft
auf einer dedizierten SQLite-Verbindung (`busy_timeout=0`). Der Inbound-
Transaktions-Kontext liefert dafür eine verbindungs-gebundene `Store`-Sicht
und tauscht NIEMALS `Store._conn` aus; die Supervisor-Hauptverbindung bleibt
während und nach dem Pass unverändert offen und nebenläufig nutzbar. Der
Approve-/Reject-Entscheidungspfad führt Cursor-Lesen, Update-Log-Insert,
Challenge-Lookup, Challenge-CAS, Approval-CAS und Cursor-Advance in EINEM
`BEGIN IMMEDIATE` auf genau dieser Verbindung aus.

---

## 15. Audit ohne Secrets

Persistiert oder geloggt werden dürfen:

- Challenge-ID;
- Approval-ID;
- Task-ID;
- Supervisor-Job-ID;
- Update-ID;
- Challenge-Status;
- Entscheidung `APPROVE`/`REJECT`;
- allowlisted Outcome-/Error-Code;
- Zeitstempel;
- Outbox-ID;
- Binding-Hash und Token-Hash ausschließlich als DB-Integritätswerte.

Nicht in Logs ausgeben:

- Roh-Token;
- Token-Hash oder dessen Präfix;
- Nonce;
- Roh-Nachricht;
- Chat-ID;
- Sender-ID;
- Bot-Credential;
- Challenge-Key;
- URL;
- HTTP-Header oder Body;
- Scope;
- Exception-Text;
- Notification-Text.

Die bestehende Auditspur bleibt autoritativ:

- `owner_approvals`;
- `events` (`gate.owner_approved`/`gate.owner_rejected`);
- Task-State-Transition;
- `command_idempotency`;
- optional spätere `action_executions` erst durch den getrennten Executor.

`telegram_update_log` ist nur Auth-/Dedup-Audit, keine Approval-Autorität.

---

## 16. Deterministische Acceptance- und Adversarial-Tests

Alle Tests offline, temporäre DB, Fake Clock, Fake Secret Source, Fake Update Source und deterministischer Outbound-Transport. Kein Netz, keine Sleeps, keine Agenten.

### 16.1 Schema/Migration

- frische V6-DB mit Tabellen, CHECKs, FKs und Indizes;
- V5→V6 erhält jede Outbox-Zeile und alle Statusfelder;
- gemeinsame `BEGIN IMMEDIATE`-Migration;
- Failure-Injection an jedem Rebuild-Schritt → vollständiger Rollback;
- `schema_version` erst zuletzt 6;
- kein Challenge-/Update-Backfill;
- keine Secret-, Chat-ID- oder Roh-Token-Spalte;
- `PRAGMA foreign_key_check` leer.

### 16.2 Token-Erzeugung (A2)

- `secrets.token_urlsafe(32)` (CSPRNG), 32 Bytes, 43 URL-safe Zeichen;
- zwei Challenges desselben Tasks erzeugen unterschiedliche Tokens;
- Token nicht aus Task-ID ableitbar;
- anderer Task, Gate, Action, Scope oder Approval → andere Challenge;
- Token nie in DB/Logs; nur `sha256(token)` persistiert;
- DB enthält Roh-Token nicht;
- gespeicherter Hash entspricht dem einmalig gerenderten Token;
- fehlender Key → keine Challenge, Supervisor commitfähig.

### 16.3 Happy Paths

- gültiges `APPROVE`:
  - Challenge `CONSUMED_APPROVED`;
  - Approval `approved`;
  - Task bleibt am Gate;
  - keine Execution;
  - genau ein Approval-Event;
  - genau eine Confirmation-Outbox-Zeile.
- gültiges `REJECT`:
  - Challenge `CONSUMED_REJECTED`;
  - Approval `rejected`;
  - bestehende Core-BLOCKED-Transition;
  - genau ein Reject-Event;
  - genau eine Confirmation-Outbox-Zeile.

### 16.4 Identität und Parser

Separate Tests für:

- falscher Chat;
- Gruppe;
- Channel;
- richtiger Chat, falscher Sender;
- Bot-Sender;
- edited message;
- callback query;
- reaction;
- Caption;
- leer;
- Whitespace;
- doppelte Spaces;
- Tab;
- Newline;
- lowercase;
- mixed case;
- `/APPROVE`;
- zusätzlicher Text;
- fehlender Token;
- zwei Tokens;
- ungültiges Alphabet;
- Padding;
- zu kurzer/langer Token;
- Unicode-Homoglyphen;
- Text größer als Limit.

Alle: fail-closed, kein Crash, keine Gate-Mutation.

### 16.5 Replay und Konkurrenz

- gleiche Update-ID 2x, 5x, 20x → eine Entscheidung;
- identischer Token mit mehreren Update-IDs → eine Entscheidung;
- Approve und Reject gleichzeitig → exakt ein Gewinner;
- zwei Threads mit getrennten DB-Verbindungen;
- zwei Prozesse/Controller;
- Challenge-CAS `rowcount == 1`;
- loser Controller erzeugt kein zweites Event und keine zweite Confirmation;
- alter Token nach Restart bleibt ungültig.

### 16.6 Expiry und stale Updates

- Challenge exakt vor, auf und nach Expiry;
- Approval vor Challenge abgelaufen;
- Approval läuft vor Challenge-TTL ab;
- `message.date < challenge.created_at`;
- `message.date >= expires_at`;
- future timestamp außerhalb erlaubter kleiner Clock-Skew-Grenze;
- alte/out-of-order Update-ID;
- höhere IDs vor niedriger ID im Response-Batch;
- keine automatische TTL-Verlängerung oder Reissue.

*Implementierungsnotiz (Phase 3C-A):* Die erlaubte Zukunfts-Clock-Skew-Grenze
für `message_date` ist als Konstante `FUTURE_SKEW_BOUND = 300` (Sekunden) in
`argent_core/approval_processor.py` definiert. Ein `message_date` darf höchstens
diesen Wert vor der lokalen Zeit liegen; ein darüber hinaus in der Zukunft
liegender Zeitstempel wird fail-closed als `STALE_MESSAGE` verworfen.

### 16.7 Scope- und Binding-Sicherheit

- gespeicherte Action manipuliert;
- gespeicherter Scope manipuliert;
- Approval-Binding manipuliert;
- Challenge-Binding manipuliert;
- Challenge auf andere Approval-ID umgebogen;
- Task-ID-Konflikt;
- Supervisor-Job-Konflikt.

Alle müssen Challenge invalidieren oder Entscheidung ablehnen. Kein Telegram-Text darf als Ersatzbindung verwendet werden.

### 16.8 Crash-Matrix

Failure-Injection und echter DB-Reopen nach:

- Update-Insert;
- Challenge-Lookup;
- Challenge-CAS;
- Approval-CAS;
- Task-Transition;
- Event-Insert;
- `command_idempotency`-Insert;
- Confirmation-Outbox-Insert;
- Cursor-Update;
- Commit.

Vor Commit: alles rollback und nach Restart einmal erfolgreich ausführbar.  
Nach Commit: Replay no-op.

### 16.9 Outbound-Interplay

- Challenge und Requirement-Outbox atomar;
- Crash vor/nach Gate-Commit;
- inactive Challenge wird nicht erneut gerendert;
- Outbound-Timeout blockiert Supervisor nicht;
- Confirmation-Send-Fehler ändert Approval nicht;
- Confirmation dedup über Restart;
- externer Send bleibt dokumentiert at-least-once;
- keine Notification-Flut.

### 16.10 No-Secrets

Canaries für:

- Bot-Credential;
- Challenge-Key;
- Roh-Token;
- Owner-Chat-ID;
- Gate-Scope;
- Telegram-Rohtext;
- Transport-Exception.

Assert:

- Roh-Token nicht in DB-Datei, Outbox-Payload, Logs, Events oder Confirmation;
- Token nur im gerenderten Requirement-Envelope;
- kein Token in Objekt-`repr`, Assertion-Fehler oder Exception;
- Token-Hash nicht geloggt;
- kein Scope in Telegram-Nachricht;
- kein Credential/Target in SQLite.

### 16.11 Non-Blocking

- unbounded-blocking Inbound-Mock wird nie synchron aus Supervisor-Code aufgerufen;
- optionaler `kick()` kehrt O(1) zurück;
- höchstens ein Inbound-Worker;
- Supervisor erreicht unabhängig DONE, FAILED, BLOCKED oder WAITING_GATE;
- DB-Lock, API-Ausfall und malformed Update propagieren nie in den Loop.

### 16.12 Regression

- bestehende 1103 Tests plus neue Phase-3C-Tests grün;
- keine Skips/Xfails;
- bestehende Phase-2C-Gate-/Restart-Tests grün;
- Phase-3A-Notification-/Delivery-Tests angepasst und grün;
- bestehende Fake-Smokes grün;
- kein echtes Telegram in pytest;
- keine Live-Smokes ohne separate Autorisierung.

---

## 17. Implementierungsreihenfolge

1. V6-Schema und Migration;
2. Challenge-Modelle und Store-Queries;
3. Key-/Owner-Identity-Interfaces;
4. Tokenableitung und Challenge-Binding;
5. atomare Challenge+Requirement-Outbox-Integration;
6. strikter Parser;
7. Fake Update Source und Consumer Single-Pass;
8. enger Core-Transaction-Bridge;
9. Update-Dedup/Cursor;
10. Approve-/Reject-Atomizität;
11. Confirmation-Notification nach expliziter Owner-Freigabe;
12. Failure-/Crash-/Concurrency-Tests;
13. No-Secrets-Tests;
14. vollständige Bestandssuite;
15. unabhängiger read-only Security-/Closing-Review;
16. lokaler Abschlusscommit erst nach Abnahme;
17. kein Push oder Promotion.

---

## 18. Klare Nicht-Implementierungs- und Aktivierungsgrenze

Diese Spezifikation autorisiert keine Implementierung oder Aktivierung.

Insbesondere:

- kein `getUpdates`-Aufruf in dieser Phase;
- kein Inbound-Kick im Supervisor;
- kein Live-Token;
- kein Live-Approval;
- keine Credential- oder Key-Provisionierung;
- keine Allowlist-/Gateway-/Config-Änderung;
- kein Background-Poller;
- kein Webhook;
- kein cron/systemd;
- keine Migration einer stabilen/produktiven DB;
- kein Push/Deployment/Promotion.

Ein bounded manueller Fetch ist erst nach einem neuen, exakt gescopten Owner-Gate zulässig.

Ein persistenter Inbound-Dienst, Loop-Kick, periodischer Abruf, Webhook oder Background-Wake benötigt jeweils eine neue explizite Owner-Autorisierung und Security-Verifikation.

---

## 19. Owner-Approval-needed Items und offene Entscheidungen

Vor Implementierung oder Live-Nutzung sind folgende Owner-Entscheidungen nötig:

1. **Challenge-TTL:** A2 bindend 60 Minuten; nie länger als Approval-TTL.
2. **Tokenlänge:** A2 bindend 32 Byte/256 Bit CSPRNG, 43 URL-safe Zeichen.
3. **Challenge-Key:** KEIN separater Key (A2); kein Gate nötig.
4. **Confirmation-Typ:** KEIN neuer Typ (A6); Post-Decision-UX best-effort via `answerCallbackQuery`/`editMessageText`.
5. **Consume-Trigger:** empfohlen ausschließlich manueller Single-Pass über den OpenClaw-Inbound-Dispatcher.
6. **Loop-Kick:** standardmäßig deaktiviert; Aktivierung benötigt separates Gate.
7. **Chat-Modell:** empfohlen ausschließlich direkter privater Owner-Chat; keine Gruppen oder Topics.
8. **Callback-Queries:** nur strukturierte `A:/R:/D:`-Aktionen; Textnachrichten werden nie akzeptiert.
9. **Challenge-Reissue:** empfohlen nicht automatisch; spätere explizite lokale Reissue benötigt eigene Spezifikation oder Gate.
10. **Live-Test:** genau ein temporäres Gate und ein harmloser Approve-/Reject-Test nur nach separater Autorisierung.
11. **Persistente Installation:** neuer Owner-Gate zwingend.
12. **Produktive/stabile DB-Migration:** neuer Owner-Gate zwingend.
13. **Push, Deployment oder Stable-Promotion:** separat und nicht durch Phase 3C autorisiert.

---

# C. EXPLICIT ANSWERS

## 1. Ist das Tokenmodell fail-closed gegen Guessing, Leak, Replay, Expiry und Mehrfachnutzung?

Ja, innerhalb der definierten Trust Boundary:

- 256-Bit Capability;
- nur Hash persistiert;
- voller Gate-/Task-/Action-/Scope-Binding;
- exakte Owner-Chat- und Senderprüfung;
- TTL;
- terminale Single-Use-CAS;
- persistente Update-Deduplizierung.

Ein kompromittierter Owner-Telegram-Account zusammen mit einem geleakten aktiven Token liegt außerhalb dessen, was ein Telegram-Bearer-Kanal kryptographisch verhindern kann.

## 2. Ist exactly-once Gate Consumption inklusive Konkurrenz und Crashfenstern garantiert?

Ja für die Telegram-Entscheidung und den Challenge-Konsum, sofern der vorgeschriebene gemeinsame `BEGIN IMMEDIATE`-Core-Bridge-Pfad umgesetzt wird.

Wichtig: Telegram setzt `pending -> approved` oder `pending -> rejected`. Es führt nicht `execute_approved()` aus und setzt das Gate bei Approve nicht auf `consumed`.

Zwei Controller oder unterschiedliche Update-IDs mit demselben Token können höchstens einen committed Gewinner erzeugen. Alle Vor-Commit-Crashes rollen vollständig zurück; alle Nach-Commit-Replays sind no-op.

## 3. Sind No-Secrets und No-Scope-Extension airtight?

Ja, mit einer bewusst engen Ausnahme: Der Roh-Token erscheint genau im autorisierten `OWNER_APPROVAL_REQUIRED`-Nachrichtentext, weil der Owner ihn sonst nicht verwenden könnte.

Er erscheint nicht in SQLite, Payload, Hash-Payload, Logs, Events, Confirmation, Git oder Supervisor-Zustand. Telegram liefert weder Task noch Gate noch Scope; alle Bindings stammen aus dem Ledger und werden vollständig neu geprüft. Telegram kann den Scope nicht erweitern.

## 4. Bleibt der Supervisor non-blocking und restart-sicher?

Ja.

Inbound ist ein separater bounded Single-Pass und standardmäßig nicht mit dem Supervisor verdrahtet. API-Ausfälle, malformed Updates, DB-Locks oder hängende Test-Transporte ändern keinen Supervisor-Zustand. Update-Cursor, Dedup, Challenge-Status und Outbox sind restart-fest in SQLite.

## 5. Welche Teile benötigen vor Implementierung oder Live-Nutzung ein neues Owner-Gate?

Mindestens:

- Umsetzung der Phase-3C-Code-/Schemaänderung nach einem separaten Implementierungsauftrag;
- Klärung der Dispatcher-Hostung (Plugin-Interactive-Handler vs. Gateway-Hook) und der sicheren Callback-Weiterleitung ohne Agenten-Textpfad (A4/§6.0);
- jeder reale Live-Approval-Test (Button-Klick);
- Verdrahtung eines Inbound-Kicks in den Supervisor-Loop;
- jede dauerhafte/periodische Inbound-Installation;
- Webhook, Gateway-, Allowlist- oder Config-Änderung;
- Migration einer stabilen/produktiven DB;
- Deployment, Push oder Stable-/Production-Promotion.