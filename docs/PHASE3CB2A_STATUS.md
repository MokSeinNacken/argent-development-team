# Phase 3C-B2A — Operativer Status (Alternative A gewählt)

Datum: 2026-08-30. Owner-Entscheid: **Alternative A** (Upstream-Lösung),
operativ **Alternative C** bis zur Verfügbarkeit.

## Live-Approval bleibt absichtlich deaktiviert

- Kein lokaler Patch des minifizierten OpenClaw-dist-Artefakts.
- Kein Live-Telegram-Inbound, keine Live-Owner-Approvals, kein zweiter
  Poller, kein Webhook, keine Umgehung der fehlenden Felder.
- Der Phase-3C-B1-Adapter bleibt fail-closed: `update_id`/`message_date`
  sind Pflichtfelder; fehlen sie, liefert der Adapter
  `HOST_CONTRACT_VIOLATION` ohne Ledger-Eingriff (keine Synthese).

## Benötigte Upstream-Fähigkeit

OpenClaw muss `TelegramInteractiveHandlerContext` um zwei Pflichtfelder
erweitern (durchgereicht aus dem bereits vorhandenen Telegram-Ingress):

- `updateId: number | null` — exakt `ctx.update.update_id`
  (Helper `resolveTelegramUpdateId` existiert bereits in
  `extensions/telegram/src/telegram-ingress-spool.ts:34`; `null` bei
  synthetischen oder anderweitig fehlenden/ungültigen `update_id`-Werten
  — Nicht-Objekt, fehlendes Feld, Nichtzahl, negativer/unsicherer
  Integer; valide Bot-API-Updates liefern eine Zahl; Argent-Adapter
  fail-closed)
- `messageDate: number` — exakt `callbackQuery.message.date`

Details, betroffene Dateien, PR-Entwurf und Testplan:
`docs/PHASE3CB2A_UPSTREAM_SPEC.md` + `docs/PHASE3CB2A_UPSTREAM_PR_DRAFT.md`.

## Lokaler Upstream-PR-Entwurf (nur /tmp, nicht in npm-Installation)

- Re-Creierbarer Checkout: `/tmp/openclaw-upstream` (main-Tip, z. B.
  `6b8a9310`), Branch `feat/telegram-interactive-context-update-metadata`,
  lokaler Commit (zuletzt `6f16e3e0`) — 5 Dateien, +29 Zeilen (Typ,
  Router-Params, Dispatch-ctx, Test-Helper, Test-Fixtures/Assertions;
  `updateId: number | null` wegen Upstream-`strict`-tsconfig, `null` bei
  synthetischen oder fehlenden/ungültigen `update_id`-Werten).
  **Nicht gepusht.** Die produktive npm-Installation
  (`/home/pc/.npm-global/.../openclaw`) ist unverändert. Da `/tmp`
  tmpfs ist, gilt als dauerhaftes Artefakt der Patch
  `docs/PHASE3CB2A_UPSTREAM_PATCH.patch` (Prozedur siehe
  `docs/PHASE3CB2A_UPSTREAM_PATCH.md`).
- Re-Creation-Vermerk: `/tmp` ist tmpfs und wurde durch zwei Host-Neustarts
  (2026-08-30) geleert. Der ursprüngliche Entwurf (`8bb56e8b` auf Basis
  `781431e2`) und die erste Re-Creation (`6a1837c1` auf Basis main-Tip
  `8b7d685b`, inhaltlich identisch: 5 Dateien, +25 Zeilen) gingen damit
  verloren. **Dauerhaftes Artefakt ist daher der Patch
  `docs/PHASE3CB2A_UPSTREAM_PATCH.patch`** (mit Re-Creation-Prozedur in
  `docs/PHASE3CB2A_UPSTREAM_PATCH.md`); der Clone kann jederzeit
  reproduziert werden. Basis `781431e2` ist upstream nicht mehr von einem
  Ref aus erreichbar; die Re-Creation erfolgt auf dem jeweils aktuellen
  main-Tip (Patch ist additiv und kontextstabil).

### Upstream-Tests: lokal nicht ausführbar (ehrliche Einschränkung)

Die Upstream-seitigen Testpunkte der 22-Punkte-Testliste des Auftrags
(siehe Anhang unten; konkret Punkte 1, 2, 9–13 und 17 — OpenClaw-
Verhalten/Tests) sind in dieser Umgebung **nicht ausführbar**: Das System
hat kein pnpm/corepack und keine node_modules; eine vollständige
OpenClaw-Monorepo-Installation wäre eine Systemänderung außerhalb der
Owner-Freigabe (und `/tmp` ist ephemer). Verifiziert wurde der Patch
daher read-only:

- Wertequellen im Quelltext verifiziert (`resolveTelegramUpdateId` →
  `ctx.update.update_id`; `callbackMessage.date`),
- `git apply --check` + `git apply` gegen frischen Upstream-Checkout
grün (zuletzt main-Tip `8cf4351`),
- struktureller Typpfad konsistent (`number | null` unter strict).

Die Ausführung der Upstream-Tests (tsc/Test-Runner) obliegt den
OpenClaw-Maintainern im PR-Prozess; bis dahin bleibt der
Argent-Adapter fail-closed.

### Anhang: 22-Punkte-Testliste des Phase-3C-B2A-Auftrags (Traceability)

Original-Liste aus dem Owner-Auftrag („TESTS — Mindestens offline
testen“); Zuordnung der Ausführbarkeit im Argent-Umfeld:

| # | Punkt | Ausführbar wo | Status |
|---|-------|---------------|--------|
| 1 | `update_id` erreicht Interactive Handler byte-/wertgleich | Upstream | nicht ausführbar (kein pnpm); read-only verifiziert |
| 2 | `message_date` erreicht Interactive Handler wertgleich | Upstream | nicht ausführbar; read-only verifiziert |
| 3 | beide erreichen Argent Adapter korrekt | Argent (B1-Fixture/offline) | grün (Mapping-Logik offline getestet) |
| 4 | fehlende `update_id` → `HOST_CONTRACT_VIOLATION` | Argent (B1) | grün |
| 5 | fehlende `message_date` → `HOST_CONTRACT_VIOLATION` | Argent (B1) | grün |
| 6 | malformed `update_id` | Argent (B1) | grün |
| 7 | malformed `message_date` | Argent (B1) | grün |
| 8 | Overflow-Grenzen | Argent (B1) | grün |
| 9 | Callback wird weiterhin exklusiv konsumiert | Upstream | nicht ausführbar; unverändertes Verhalten (Patch rein additiv) |
| 10 | `handled=true` verhindert Agenten-Fallback | Upstream | nicht ausführbar; unverändertes Verhalten |
| 11 | unbekannter Namespace bleibt unverändert | Upstream | nicht ausführbar; unverändertes Verhalten |
| 12 | normale Telegram-Nachrichten unverändert | Upstream | nicht ausführbar; unverändertes Verhalten |
| 13 | keine doppelte Callback-Ausführung | Upstream | nicht ausführbar; unverändertes Verhalten (Dedup unangetastet) |
| 14 | kein zusätzlicher Poller | Argent (Scope) | grün (Scope-Scan) |
| 15 | kein Netzwerkzugriff in Tests | Argent (bwrap) | grün (read-only, offline) |
| 16 | keine Secrets/echten Owner-IDs in Fixtures | Argent (Scan) | grün (Secret-Scan clean) |
| 17 | bestehende OpenClaw-Telegram-Tests unverändert grün | Upstream | nicht ausführbar (kein pnpm) |
| 18 | Phase-3C-B1-Adapter-Tests grün | Argent | grün (44 passed) |
| 19 | Phase-3C-A-Core-Tests grün | Argent | grün (80 passed) |
| 20 | volle Argent-Regression grün | Argent | grün (1227 passed) |
| 21 | bwrap grün | Argent | grün (90 Tests, exit 0, read-only) |
| 22 | Fake-Smoke grün | Argent | grün (22/22) |

## Compatibility-Guard (Argent, B1-Fixture)

`tests/test_phase3cb1_adapter.py::test_installed_handler_context_host_boundary_contract`
+ `tests/fixtures/interactive_dispatch_handler_context.d.ts.snippet`:

- Aktuell: assertiert die **Abwesenheit** von `updateId`/`messageDate`/
  `answerCallbackQuery` im installierten Handler-Context (Kanarienvogel;
  prüft das eingecheckte statische Fixture, das byte-exakt aus der
  installierten `dist`-d.ts extrahiert ist — kein automatisches
  Live-Detection).
- Nach einem OpenClaw-Update mit den neuen Feldern: sobald die B1-Fixture
  auf die neue installierte Kontraktform aktualisiert wird, schlägt der
  Abwesenheits-Assert fehl → Signal, dass die Fixture umgestellt und die
  Live-Bridge gebaut werden kann.
- Kein stilles Weiterlaufen ohne die Felder: fehlen sie, bleibt der
  Adapter fail-closed.

## Nächste Schritte (nach Upstream-Release)

1. OpenClaw auf ein Release mit `updateId`/`messageDate` im Context.
2. B1-Fixture/Guard auf Anwesenheit umstellen.
3. Phase-3C-B2B (Plugin-Adapter im Gateway, Namespace `argent`) mit
   Owner-Gate.
4. Live-Test mit Owner-Gate (genau ein Approval-Button-Klick, A:/R:/D:).
5. Live-Betrieb.
