# Phase 3C-B2A — Upstream-PR-Entwurf als Patch (dauerhaftes Artefakt)

> Dieses Artefakt ist der **dauerhafte** lokale PR-Entwurf für OpenClaw.
> `/tmp/openclaw-upstream` (tmpfs) wurde durch Host-Neustarts zweimal
> geleert; der Patch bleibt daher als Datei im Argent-Repo erhalten und
> kann jederzeit reproduziert werden. Kein Push, keine Übernahme in die
> produktive npm-Installation.

## Anwendung (Re-Creation-Prozedur)

```bash
cd /tmp
rm -rf openclaw-upstream
git clone --depth 1 https://github.com/openclaw/openclaw.git openclaw-upstream
cd openclaw-upstream
git checkout -b feat/telegram-interactive-context-update-metadata
git apply /home/pc/projects/argent-development-team/docs/PHASE3CB2A_UPSTREAM_PATCH.patch
git add -A
git -c user.name="Argent Dev Team" -c user.email="argent-dev-team@local" commit -m \
  "feat(telegram): expose updateId/messageDate in TelegramInteractiveHandlerContext

Forward the authoritative Telegram update_id and callback message date
into the plugin interactive handler context so consumers can implement
persistent update dedup and time-window checks without synthesizing
values. Additive only; no changes to handled/fallback semantics,
namespace matching, auth, or agent behavior."
```

Erwartetes Ergebnis: 5 Dateien, +29 Zeilen; Commit-SHA variiert (abhängig
vom jeweiligen main-Tip und Commit-Zeitpunkt). Bekannte gute SHAs:
`8bb56e8b` (Basis `781431e2`, 2026-08-30, vor dem ersten Verlust),
`6a1837c1` (Basis `8b7d685b`, 2026-08-30), `22a1cb71` (Basis
`6b8a9310`, 2026-08-30) und `6f16e3e0` (Basis `6b8a9310`, aktueller
Stand inkl. Typpräzisierung `updateId: number | null` und präzisierter
`null`-Semantik im Docstring). Basis `781431e2` ist upstream nicht mehr
von einem Ref aus erreichbar; die Re-Creation erfolgt daher auf dem
jeweils aktuellen main-Tip (der Patch ist additiv und kontextstabil).

## Inhalt (5 Dateien, +29 Zeilen)

1. `extensions/telegram/src/interactive-dispatch.ts` — Typ
   `TelegramInteractiveHandlerContext` + `updateId: number | null` +
   `messageDate: number` (Pflichtfelder, dokumentiert; `null` bei
   synthetischen oder fehlenden/ungültigen `update_id`-Werten, Konsument
   fail-closed — Upstream-`tsconfig` ist `strict`).
2. `extensions/telegram/src/bot-handlers.callback-router-controls.ts` —
   `handleTelegramInteractiveCallback`-Params + Dispatch-`ctx` reichen
   beide Werte durch.
3. `extensions/telegram/src/bot-handlers.callback-router.ts` —
   Wertübergabe: `updateId: resolveTelegramUpdateId(ctx.update)`,
   `messageDate: callbackMessage.date` (+ Import).
4. `src/plugins/interactive-contract.test-helpers.ts` — Test-Helper-Typ
   um die beiden Felder ergänzt.
5. `src/plugins/interactive.test.ts` — Fixture (424242 / 1710000000) +
   Wertgleich-Assertions.

Keine Semantikänderung: Namespace-Matching, `handled`-Fallback, Auth,
Commands, Approval-/Gate-Semantik unverändert. Keine Synthese, keine
Ersatzwerte, kein neuer Poller/Webhook/Secret.
