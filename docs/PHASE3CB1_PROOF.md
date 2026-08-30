# Phase 3C-B1 — OpenClaw Telegram Approval Adapter (ohne Live-Inbound)

## Technischer Beweis: Integrationspfad A (Plugin-Interactive-Handler)

Untersuchung des tatsächlich installierten OpenClaw-Codes
(`/home/pc/.npm-global/lib/node_modules/openclaw/dist/`, Version 2026.7.1-2),
read-only. Ergebnis: **Pfad A ist technisch eindeutig möglich** — der
bestehende OpenClaw-Telegram-Inbound kann `callback_query` vor dem normalen
Agenten-Textpfad exklusiv an einen Plugin-Interactive-Handler beanspruchen.

| Anforderung | Nachweis | Quelle |
|---|---|---|
| `callback_query` strukturiert zugänglich | `bot.on("callback_query", ...)` mit `ctx.callbackQuery` | `dist/telegram-ingress-spool-*.js` Z. 3483 |
| `callback_data` verfügbar (normalisiert) | `const data = (callback.data ?? "").trim()` → an Dispatcher als `params.data`; `.trim()` wird angewendet und opaque Substitution ist möglich — `callback.data` ist ggf. nicht byte-identisch zum Original | Z. ~3500, 3612 |
| `from.id` verfügbar | `const senderId = callback.from?.id ? String(callback.from.id) : ""` → Handler-`ctx.senderId` | Z. ~3555, 3619 |
| `chat.id` verfügbar | `const chatId = callbackMessage.chat.id` → `callbackMessage.chatId` | Z. ~3533, 3626 |
| `update_id` (nur ingress-intern) | `resolveTelegramUpdateId$(ctx) = ctx.update?.update_id ?? ctx.update_id` wird ingress-intern für Dedup/Cursor aufgelöst, ist aber NICHT im Handler-`ctx` exponiert (siehe Lücken) | Z. 1540 |
| message/callback reference verfügbar | `callbackId = callback.id`, `messageId = callbackMessage.message_id` | Z. 3617, 3625 |
| Konsum vor Agenten-Promptpfad | `dispatchTelegramPluginInteractiveHandler(...)` läuft VOR dem generischen Text-Fallback; `if ((await ...).handled) return;` | Z. 3612–3662 |
| Kein zusätzlicher Agenten-Text | `handled` Default `true` (`resolved?.handled ?? true`); bei `handled: true` → `return`, der `callback_data: <value>`-Textpfad wird nie erreicht | `dist/plugin-runtime-*.js` Z. 59–90 |
| Nicht passende Nachrichten | Nur `callback_query` mit registriertem Namespace matcht (`resolvePluginInteractiveMatch`: `namespace:payload`, z. B. `argent:A:<challenge>`); normale Messages gehen durch den Message-Pfad | `dist/interactive-registry-*.js` Z. 15–34 |
| Kein zweiter Poller | Gateway (Long-Polling) besitzt den Stream; der Handler hängt im bestehenden Ingress; `claimPluginInteractiveCallbackDedupe(dedupeId=callbackId)` dedupliziert | `telegram-ingress-spool` + `plugin-runtime` |

## Lücken / Host-Boundary (Korrektur zum Integrationspfad)

Der installierte `TelegramInteractiveHandlerContext`
(`dist/interactive-dispatch-*.d.ts` Z. 98–149) liefert an den Plugin-Handler
**folgende** Felder:

- `channel`, `accountId`, `callbackId`, `conversationId`, `parentConversationId`,
  `senderId`, `senderUsername`, `threadId`, `isGroup`, `isForum`
- `auth.isAuthorizedSender`
- `callback`: `data`, `namespace`, `payload`, `messageId`, `chatId`, `messageText`
- `respond`: `reply`, `editMessage`, `editButtons`, `clearButtons`, `deleteMessage`
- Conversation-Binding: `requestConversationBinding`, `detachConversationBinding`,
  `getCurrentConversationBinding`

Der Handler-Kontext stellt **NICHT** bereit:

- Telegram `update_id` (`update.update_id`) — der Ingress löst `update_id`
  intern auf (`resolveTelegramUpdateId$`, Z. 1540), exponiert es aber NICHT im
  Handler-`ctx`.
- `message_date` / `messageDate` (Datum der Callback-Nachricht).

`ApprovalProcessor.process_callback` verlangt BEIDE Felder (`message_date=None`
→ `MALFORMED`; `update_id` ist der Dedup-/Cursor-Schlüssel). Für
Live-Verarbeitung ist daher eine **unterstützte OpenClaw-Ingress-Erweiterung**
erforderlich, die den originalen `update_id` und das Callback-Nachrichts-Datum
explizit an den Handler durchreicht. Diese Erweiterung liegt **hinter dem
separaten Phase-3C-B2-Owner-Gate** — in dieser Phase (3C-B1) wird keinerlei
OpenClaw-Code geändert; der Adapter arbeitet gegen den TARGET-Vertrag (Host
liefert `update_id` + `message_date`) und verweigert fail-closed, wenn sie
fehlen.

Registrierung: Plugin deklariert `interactiveHandlers: [{ channel: "telegram",
namespace: "argent", handler }]` (`PluginInteractiveHandlerRegistration` in
`dist/types-*.d.ts` Z. 11699). Der Handler erhält `callback: { data,
namespace, payload, messageId, chatId, messageText }`, `senderId`,
`conversationId` und `respond`-Helper (reply/editMessage/editButtons/
clearButtons/deleteMessage). Das Gateway beantwortet die Callback-Query
(`answerCallbackQuery`) VOR der Plugin-Dispatch — der Handler exponiert KEIN
`answerCallbackQuery`; die Adapter-`FakePostDecisionUx` modelliert daher die
TARGET-Post-Decision-UX für die spätere Phase-3C-B2-Verdrahtung.

## Adapter-Grenze (3C-B1)

Der Adapter akzeptiert ausschließlich strukturierte Approval-Callbacks
(`A:<challenge>` / `R:<challenge>` / `D:<challenge>` im payload) und reicht
an den Phase-3C-A-Core nur: action, opaque challenge, update_id,
sender/from identity, private chat identity, message reference, callback
reference, message_date (TARGET-Vertrag: erforderlich; im installierten
Handler-`ctx` aktuell NICHT vorhanden → Phase-3C-B2-Ingress-Erweiterung
nötig). Normale Telegram-Texte verändern
niemals `owner_approvals`. Owner-Identität in 3C-B1 nur injiziert/mock;
keine echten IDs in Tests/Logs. Post-Decision-UX nur als Mock/Fake. Kein
Netzwerkzugriff, kein zweiter Poller, keine Gateway-/Config-/Credential-
Änderung.

## Bewertung

Pfad A ist eindeutig nachweisbar → Adapter-Implementierung für Phase 3C-B1
freigegeben. Kein Gateway-Hook nötig. Die Live-Verdrahtung (Plugin-
Registration im Gateway, echter Callback-Konsum) ist Phase 3C-B2 vorbehalten
und benötigt ein separates Owner-Gate.
