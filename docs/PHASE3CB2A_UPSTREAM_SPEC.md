# Phase 3C-B2A — Upstream-Spezifikation: updateId/messageDate an TelegramInteractiveHandlerContext

Entscheidung: **Alternative A** (saubere Upstream-Lösung) + operativ C bis zur
Verfügbarkeit. Kein lokaler Patch des minifizierten dist-Artefakts, kein
Live-Inbound, keine Live-Approvals, kein zweiter Poller, kein Webhook.
Basis: HEAD `b94fb24` (Argent), OpenClaw 2026.7.1-2, Upstream-Clone
`/tmp/openclaw-upstream` (github.com/openclaw/openclaw, main, depth 1).

## 1. Problem

Der OpenClaw Telegram-Plugin-Interactive-Handler kann `callback_query`
exklusiv vor dem Agenten-Textpfad beanspruchen (namespace match →
`dispatchTelegramPluginInteractiveHandler` → `handled` → return). Der
`TelegramInteractiveHandlerContext` enthält aber NICHT die beiden
autoritativen Telegram-Metadaten, die der Argent Phase-3C-A-Approval-Core
fail-closed verlangt:

- `update_id` (Update-Dedup/Cursor, SPEC V3C §6.2)
- `message_date` (Challenge-Zeitfenster, SPEC V3C §10/§16.6)

Der Argent-B1-Adapter verlangt beide Werte absichtlich zwingend und
fail-closed (HOST_CONTRACT_VIOLATION, keine Synthese). Ohne Upstream-
Unterstützung kann der Adapter die Werte nicht erhalten → Live-Owner-
Approvals bleiben deaktiviert.

## 2. Genaue benötigte Upstream-Änderung

`TelegramInteractiveHandlerContext` um zwei neue Pflichtfelder erweitern:

```ts
updateId: number | null;  // Telegram-Update-Identität, exakt aus ctx.update.update_id
                          // (null bei synthetischen oder anderweitig fehlenden/ungültigen
                          // update_id-Werten; valide Bot-API-Updates liefern eine Zahl)
messageDate: number;      // Telegram-Message-Zeitstempel, exakt aus callback.message.date
```

- `updateId` ist bewusst `number | null` (kein Fake-Default):
  `resolveTelegramUpdateId` liefert `null` bei synthetischen Updates oder
  jedem fehlenden/ungültigen Wert — Nicht-Objekt, fehlendes Feld,
  Nichtzahl, negativer oder nicht sicher darstellbarer Integer
  (`isValidUpdateId`, `telegram-ingress-spool.ts:18-20/34-40`).
  Konsumenten behandeln `null` fail-closed (keine Synthese, kein
  Ersatzwert). Typdefinition und Runtime stimmen damit überein
  (Upstream-`tsconfig` ist `strict`).

- Minimal: nur Weitergabe bereits vorhandener autoritativer Telegram-
  Metadaten; KEINE Semantikänderung (Namespace-Matching, handled-Fallback,
  Commands, Auth, Approval/Gate-Semantik unverändert).
- Keine neuen Secrets, kein Poller, keine Agentenlogik.
- Runtime und Typdefinition müssen übereinstimmen; keine Fake-Defaults.

## 3. Betroffene Upstream-Dateien/Funktionen

| Datei | Funktion/Stelle | Änderung |
|---|---|---|
| `extensions/telegram/src/interactive-dispatch.ts` | `type TelegramInteractiveHandlerContext` (~Z. 14) | + `updateId: number | null; messageDate: number;` im Context-Objekt |
| `extensions/telegram/src/bot-handlers.callback-router-controls.ts` | `handleTelegramInteractiveCallback` (~Z. 434, Dispatch-Aufruf ~Z. 564) | `updateId`/`messageDate` aus Parametern in den `ctx`-Aufruf übernehmen |
| `extensions/telegram/src/bot-handlers.callback-router.ts` | Aufruf von `handleTelegramInteractiveCallback` (~Z. 321) | `updateId` aus `resolveTelegramUpdateId(ctx.update)` und `messageDate` aus `callback.message.date` übergeben |
| `extensions/telegram/src/telegram-ingress-spool.ts` | `resolveTelegramUpdateId(update)` (~Z. 34) — Quelle `update.update_id` | (bereits vorhanden; ggf. Import/Reuse) |
| `extensions/telegram/src/bot-handlers.types.ts` | `TelegramCallbackRouter`/`RegisterTelegramHandlerParams` (falls Typpflicht) | Parametertypen um `updateId`/`messageDate` ergänzen |
| `extensions/telegram/api.ts`, `extensions/telegram/contract-api.ts` | Re-Export von `TelegramInteractiveHandlerContext` | unverändert (Typ wird automatisch mitgezogen) |
| `src/plugins/interactive-contract.test-helpers.ts` | Test-Helper-Typ `TelegramInteractiveHandlerContext` (~Z. 18) | + `updateId`/`messageDate` (Testkonsistenz) |

## 4. Exakte Wertequellen (verifiziert)

- `update_id`: grammY-`ctx.update.update_id` (Telegram Update-Objekt);
  existierender Helper `resolveTelegramUpdateId(update)` in
  `extensions/telegram/src/telegram-ingress-spool.ts:34` liefert
  `number | null` mit `isValidUpdateId`-Prüfung.
- `message_date`: `callbackQuery.message.date` (grammY `Message.date`,
  Unix-Sekunden, Pflichtfeld im Telegram Bot API).

## 5. Upstream-Testplan

1. `updateId` erreicht den Interactive-Handler byte-/wertgleich
   (`ctx.update_id` → Context `updateId`).
2. `messageDate` erreicht den Handler wertgleich (`callback.message.date` →
   Context `messageDate`).
3. Runtime und Typdefinition stimmen überein (TS-Typcheck + Laufzeitwert).
4. `handled=true` bleibt exklusiv (kein `callback_data:`-Agentenprompt).
5. Agenten-Fallback unverändert (`handled: false`/nicht gematcht → alter
   Pfad).
6. Unbekannte Namespaces unverändert (kein Match → bestehendes Verhalten).
7. Normale Telegram-Nachrichten unverändert (kein Einfluss auf Messages).
8. Keine doppelte Callback-Ausführung (Dedup unverändert).
9. Fehlende Werte (synthetische Updates ohne update_id/message.date):
   fail-closed auf Aufrufer-Seite (Argent-Adapter HOST_CONTRACT_VIOLATION);
   im OpenClaw selbst: `updateId` via `resolveTelegramUpdateId` → `null`
   bei synthetischen oder fehlenden/ungültigen `update_id`-Werten; valide
   Bot-API-Updates liefern immer beide Werte.
10. Bestehende OpenClaw-Telegram-Tests grün (rückwärtskompatibel; Felder
    additiv, Pflichtfelder nur im erweiterten Context).

## 6. Rückwärtskompatibilität

- Additive Felder in einem Context, der nur vom Telegram-Plugin erzeugt
  wird → keine bestehende Registrierung bricht (TS: bestehende Handler
  lesen die neuen Felder nicht).
- Keine Änderung an `dispatchTelegramInteractive`-Signatur oder
  `handled`-Semantik.
- Upgrade-Pfad: nach OpenClaw-Update prüft der Argent-Compatibility-Guard
  (B1-Fixture) fail-closed, ob `updateId`/`messageDate` wieder fehlen
  (→ HOST_CONTRACT_VIOLATION statt stillem Weiterlaufen).

## 7. Status des lokalen Compatibility-Guards (Argent)

`tests/test_phase3cb1_adapter.py::test_installed_handler_context_host_boundary_contract`
+ Fixture `tests/fixtures/interactive_dispatch_handler_context.d.ts.snippet`:
- Assertiert aktuell die ABWESENHEIT von `update_id`/`updateId`/
  `message_date`/`messageDate`/`answerCallbackQuery` im installierten
  Handler-Context (Kanarienvogel). Der Test liest ausschließlich die
  eingecheckte statische Fixture (kein automatisches Live-Detection);
  ein Paket-Update allein ändert sein Ergebnis nicht.
- Nach einem OpenClaw-Update: zuerst die installierte `dist`-d.ts erneut
  extrahieren und die Fixture auf die neue Kontraktform aktualisieren
  (verpflichtender Post-Update-Schritt); erst dann schlägt der
  Abwesenheits-Assert fehl → Signal, dass die Fixture auf
  die neue Kontraktform aktualisiert werden muss (und die Live-Bridge
  gebaut werden kann). Kein stilles Weiterlaufen ohne die Felder.

## 8. Was nach einem zukünftigen OpenClaw-Release für den Live-Test nötig ist

1. OpenClaw-Update auf ein Release, das `updateId`/`messageDate` im
   `TelegramInteractiveHandlerContext` liefert.
2. Fixture/Guard auf die neue Kontraktform aktualisieren (Assert auf
   Anwesenheit).
3. Phase-3C-B2B (Plugin-Adapter im Gateway registrieren: Namespace
   `argent`, `interactiveHandlers`-Eintrag) mit Owner-Gate.
4. Live-Test mit Owner-Gate: exakt ein echter Approval-Button-Klick
   (A:/R:/D:), Verifikation von Outbox/CAS/Dedup, Post-Decision-UX.
5. Danach Live-Betrieb.

## 9. Kein Push, kein externer PR/Issue-Post

Der PR-/Issue-Entwurf (siehe SEPARATE Datei) ist lokal; Veröffentlichung
nur nach separater Owner-Freigabe.
