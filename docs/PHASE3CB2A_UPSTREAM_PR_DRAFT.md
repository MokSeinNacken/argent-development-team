# PR-/Issue-Entwurf: OpenClaw — expose updateId/messageDate in TelegramInteractiveHandlerContext

> STATUS: Lokaler Entwurf. NOCH NICHT veröffentlicht (kein Push, kein
> externer Post ohne separate Owner-Freigabe). Erstellt am 2026-08-30.

## Problem

`TelegramInteractiveHandlerContext` (Plugin-Interactive-Handler für
Telegram-Callbacks) reicht viele Callback-Metadaten durch (callbackId,
senderId, chatId, messageId, namespace, payload), aber nicht die beiden
autoritativen Telegram-Metadaten `update_id` und `message.date`. Beide sind
am Ingress bereits vorhanden (`ctx.update.update_id` via
`resolveTelegramUpdateId`, `callbackQuery.message.date`), werden aber beim
Dispatch an den Plugin-Handler verworfen.

## Sicherheitsbegründung

Integrationen, die einen Gate-/Approval-Core mit persistenter Update-Dedup
und Zeitfenster-Prüfung betreiben (z. B. fail-closed Owner-Approvals über
Telegram-Inline-Buttons), benötigen diese Werte unverändert und
synthesefrei. Ohne sie muss der Konsument fail-closed ablehnen (kein
Synthetisieren, kein Ersatz aus callbackId/Timestamps), was Live-Funktionen
blockiert. Die Weitergabe vorhandener autoritativer Werte fügt keinerlei
neue Angriffsfläche hinzu: keine neuen Secrets, keine neuen Pfade, kein
zusätzlicher Poller, keine Auth-Änderung.

## Aktueller Codepfad

```
grammY bot.on("callback_query")                     bot-handlers.runtime.ts:27
  → callbackRouter.route(ctx)                       bot-handlers.runtime.ts:28 (Aufruf);
                                                   Definition `route:` callback-router.ts:418
    → handleCallback(ctx)                           callback-router.ts:99 (Definition; Aufruf :422)
      → data = (callback.data ?? "").trim()
      → callbackMessage = callback.message
      → handleTelegramInteractiveCallback({...})    callback-router.ts:321
        → dispatchTelegramPluginInteractiveHandler({
            data: pluginCallbackData,
            callbackId: callback.id,
            ctx: { accountId, callbackId, conversationId, senderId, ...,
                   callbackMessage: { messageId, chatId, messageText } },
            respond: {...} })                       bot-handlers.callback-router-controls.ts:564
          → registration.handler({...ctx, channel, callback:{...}, respond,...})
                                                    interactive-dispatch.ts (via createChannelInteractiveDispatcher)
```

Der grammY-`ctx` (mit `update.update_id`) und `callbackMessage.date` sind an
den Zwischenstationen verfügbar, gehen aber im Dispatch-`ctx`-Objekt
verloren.

## Minimale vorgeschlagene Änderung

### Typänderung

`extensions/telegram/src/interactive-dispatch.ts` — in
`type TelegramInteractiveHandlerContext`:

```ts
export type TelegramInteractiveHandlerContext = {
  channel: "telegram";
  accountId: string;
  callbackId: string;
  // NEU: Telegram-Update-Identität (exakt ctx.update.update_id;
  // null bei synthetischen oder fehlenden/ungültigen Werten, Konsument fail-closed)
  updateId: number | null;
  // NEU: Telegram-Message-Zeitstempel (exakt callback.message.date, Unix-Sekunden)
  messageDate: number;
  conversationId: string;
  // ... übrige Felder unverändert
};
```

Zusätzlich (Typpflicht auf dem Dispatch-Pfad):

- `bot-handlers.callback-router-controls.ts`:
  `handleTelegramInteractiveCallback`-Params um `updateId: number | null;`
  `messageDate: number;` ergänzen und im `dispatchTelegramPluginInteractiveHandler`-Aufruf übergeben.
- `bot-handlers.callback-router.ts`: beim Aufruf strikte Übergabe ohne
  Ersatzwert: `updateId: resolveTelegramUpdateId(ctx.update)`
  (`number | null`; Helper existiert bereits in
  `telegram-ingress-spool.ts:34`, Upstream-`tsconfig` ist `strict`, der
  Typ ist damit konsistent). Bei synthetischen Updates (kein `update_id`)
  liefert der Helper `null` → der Konsument behandelt das fail-closed
  (kein Dispatch von Ersatzwerten, keine Synthese); echte
  Bot-API-`callback_query`-Updates besitzen immer `update_id`.
- `messageDate: callbackMessage.date` (Pflichtfeld `Message.date`).

### Runtime-Änderung

Nur die Werte in das `ctx`-Objekt des Dispatchers aufnehmen; keine weitere
Logik.

### Testplan (Upstream)

1. `updateId` wertgleich im Handler (Update-Fixture mit fester `update_id`).
2. `messageDate` wertgleich im Handler (`callback.message.date`-Fixture).
3. TS-Typcheck + Laufzeitkonsistenz (beide Felder present;
   `updateId` ist `number | null` — bei echten Bot-API-`callback_query`-
   Updates `number` —, `messageDate` ist `number`).
4. `handled=true` weiterhin exklusiv (kein Agentenprompt).
5. Agenten-Fallback unverändert.
6. Unbekannter Namespace unverändert.
7. Normale Telegram-Nachrichten unverändert.
8. Keine doppelte Callback-Ausführung (Dedup unverändert).
9. Synthetisches Update ohne `update_id`: fail-closed auf Konsumentenseite
   (kein stiller Ersatzwert).
10. Bestehende Telegram-Tests grün.

Hinweis: Die Upstream-Testpunkte dieses Testplans sind im Argent-Umfeld
**nicht lokal ausführbar** (kein pnpm/node_modules; Monorepo-Installation
wäre Systemänderung außerhalb der Freigabe) — sie sind für die
OpenClaw-Maintainer im PR-Prozess bestimmt; der Patch wurde read-only
verifiziert (Quelltext-Wertequellen, `git apply --check` gegen main,
Typpfad unter `strict`). Siehe `docs/PHASE3CB2A_STATUS.md`.

### Rückwärtskompatibilität

- Additive Pflichtfelder nur im Telegram-Plugin-erzeugten Context; alle
  bestehenden Handler-Registrierungen kompilieren und laufen unverändert
  (sie lesen die Felder nicht).
- Keine Änderung an `dispatchTelegramInteractive`, `handled`-Semantik,
  Auth, Namespace-Matching oder Agenten-Fallback.
- OpenClaw-eigene Tests: Test-Helper `src/plugins/interactive-contract.test-helpers.ts`
  um die beiden Felder ergänzen sowie `src/plugins/interactive.test.ts`
  um Fixture-Werte (`updateId: 424242`, `messageDate: 1710000000`) und
  Wertgleich-Assertions erweitern.

## Betroffene Dateien (Zusammenfassung)

1. `extensions/telegram/src/interactive-dispatch.ts` — Typ
2. `extensions/telegram/src/bot-handlers.callback-router-controls.ts` — Params + Dispatch
3. `extensions/telegram/src/bot-handlers.callback-router.ts` — Werte-Übergabe
4. `extensions/telegram/src/bot-handlers.types.ts` — ggf. Router-Typen
   (geprüft: nicht benötigt — Parametertypen liegen inline in
   `handleTelegramInteractiveCallback`; der Patch fasst types.ts nicht an)
5. `src/plugins/interactive-contract.test-helpers.ts` — Test-Helper-Typ
6. Tests: `src/plugins/interactive.test.ts` — Fixture
   `updateId: 424242` / `messageDate: 1710000000` + Wertgleich-Assertions
   (Test-Helper `src/plugins/interactive-contract.test-helpers.ts`
   konsistent erweitert).

Kein neuer Poller, kein Webhook, keine Secrets, keine Agentenlogik.
