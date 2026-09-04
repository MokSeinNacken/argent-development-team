# PHASE I3-B ACCEPTANCE — GitHub Feature-Branch Push + PR Live Acceptance

**Branch:** `phase-i3b-github-live-acceptance` (Base `ffc266421ca0d53d1a5a7c2d078194f88e65868b` = Phase I3-A GREEN).
**Datum:** 2026-09-03.
**Closing state:** commit `e825111b79cd59eef2bd80dbbe84bfda0ac21ff3` is the historical
pushed acceptance snapshot (C1). The live acceptance branch + PR (see §3) already
exist on GitHub; **they are DO-NOT-MERGE** and must be left exactly as-is.

**STATUS: I3-B GREEN — ARGENT_PHASE_I3B_GITHUB_LIVE_GREEN (Main-verifiziert nach Fix-Round + unabhängiger Verifikation; kein I3-/I-GREEN).** This is the closing-fix round (Sol HIGH 6/13/17 + LOW
14/16/18). GREEN is marked ONLY by Main after this fix round + full regression
run. Marker (only after Main): `ARGENT_PHASE_I3B_GITHUB_LIVE_ACCEPTANCE_GREEN`.

---

## 1. What I3-B delivers (final local code state)

- **NEW** `argent_core/github_provider_adapter.py` — real `GitHubProviderAdapter`
  (argv subprocesses, NO shell), live-write activation gate, acceptance identity
  constants, repository-identity canonicalization, `classify_gh_failure`,
  task-scoped `github_acceptance_allowlist()` / `github_acceptance_standing_policy()`.
  Closing fixes add the **bound-SHA mutation boundary** (HIGH-6) and fail-closed
  PR-number parsing + own-PR author binding (LOW-14/LOW-16).
- **EXTENDED** `argent_core/external_provider_adapter.py` — additive
  `ProviderNetworkError(ProviderUnavailable)` (no behavioral change to existing
  classes).
- **HARDENED** `argent_core/external_action_broker.py` — authoritative audit
  history (HIGH-13): `AUTHORIZED` is now recorded on every successful
  authorization (autonomous AND owner paths); `EXECUTED`/`RECONCILED_SUCCESS`
  rows are strictly conditional on the authoritative state transition actually
  succeeding (a failed lease/revision CAS never fabricates an execution record).
- **NEW** `tests/i3b_helpers.py` — scripted fake `gh`/`git` executable harness
  (tmp dir, no network, fails-closed on any credential in argv).
- **Tests** `tests/test_phase_i3b_live_gate.py` (9), `_adapter.py` (25),
  `_allowlist.py` (8), `_broker_live.py` (8) — **50 I3-B tests**, including the
  new adversarial regression tests for HIGH-6/HIGH-13/LOW-14. Full suite:
  **2853 passed** (Python 3.14 / pytest 9.1.1).
- **Docs** `docs/PHASE_I3B_NOTES.md` + this file.

## 2. Sol closing review findings → fixes

The independent read-only Sol HIGH review found **3 HIGH + 4 LOW** (no CRITICAL).
Main spot-verified each — **all CONFIRMED** — and all six enumerated findings are
fixed here:

| Finding | Severity | Fix |
|---|---|---|
| **HIGH-6** | HIGH | Bound SHA is now enforced at the mutation boundary. `push_feature_branch` verifies the **local** `refs/heads/<branch>` resolves to the request's bound SHA *before* the push (mismatch/missing ⇒ `ProviderConflict`, no push invocation), reads the **remote** ref back after the push and requires equality with the bound SHA before returning success (race/stale mirror ⇒ conflict, never fabricated success). `create_pull_request` reads the remote head ref and requires `remote sha == head_sha` *before* creating the PR (fail-closed otherwise). |
| **HIGH-13** | HIGH | Audit history is now authoritative. `AUTHORIZED` is appended exactly once on every successful authorization (autonomous + owner, with authorization reference / policy decision). `_finalize` and `_reconcile_locked` append their audit rows **only on the success branch** of the authoritative transition; a failed lease/revision CAS returns a bounded outcome with **no** `EXECUTED`/`RECONCILED_SUCCESS` row. |
| **HIGH-17** | HIGH | Documentation rewritten truthfully for the closing state (this file + NOTES): real live-flow evidence, real credential architecture, Sol findings + fixes, code-enforced vs operationally-required vs observed-live distinctions, unprotected `main`, and the C1-as-historical-artifact distinction. |
| **LOW-14** | LOW | `_parse_pr_number` now binds the `pull/<digits>` URL to the **expected** `owner/repo` (a foreign-repository URL fails closed); `_find_own_pr` fails closed on a missing/non-dict author (an author-unknown PR is never treated as Argent-owned). |
| **LOW-16** | LOW | Removed unreachable leftover code after the `return None` in `_parse_pr_number`; the URL-parser regression test still passes and now covers expected-repository binding. |
| **LOW-18** | LOW | Documentation staleness (credential helper, test count, pushed state, unprotected main, PAT scopes) — resolved by HIGH-17's doc rewrite. |

## 3. Live flow summary (OBSERVED LIVE — provider-side state is final)

Performed earlier by Main **through the broker** (the adapter never activates
live writes on its own). External objects created on `github.com/MokSeinNacken/argent-development-team`:

- **1 branch** pushed: `argent/efe311ca-7647-4915-bf9a-d63bca966c1b-i3b-live-acceptance`
  at `e825111b` (C1).
- **1 PR** opened: **PR #1**, head `argent/efe311ca-…-i3b-live-acceptance` @
  `e825111b`, base `main` @ `ffc26642` — **OPEN, DO-NOT-MERGE** (left as-is).
- **0 merges** (remote `main` unchanged at `ffc26642`; nothing was merged or
  force-updated).
- **Broker-only writes**: every provider mutation went through the broker's
  bounded request lifecycle; no direct/adapter-side write, no shell, no LLM.

**Ledger evidence (split-store caveat).** The live run used two store instances:

1. The **original** store: `create_request` **SUCCEEDED** but recorded
   `provider_object_id = NULL`; the subsequent readback **FAILED** (the object id
   was not persisted/readable in that ledger).
2. A **fresh reconciliation store**: reconciliation against provider-visible
   state recorded **`RECONCILED_SUCCESS`** (the pushed ref / open PR were
   observed provider-side and the request finalized SUCCEEDED).

The durable success evidence therefore lives across two store ledgers — this is
a known, documented limitation of the live acceptance run (no single-store
end-to-end ledger), not a hidden write.

## 4. Credential architecture (OBSERVED LIVE — not code-enforced)

- GitHub account `MokSeinNacken` authenticates via **`gh`**: a **classic PAT**
  stored in `~/.config/gh/hosts.yml` (mode 0600). The PAT carries **`repo` +
  `workflow`** scopes (the minimum set needed for the acceptance writes).
- **`git push` authenticates through `gh`'s git-credential helper** — i.e.
  `gh auth` is configured as the `git credential.helper` (the earlier "no git
  credential.helper" claim was WRONG and is corrected here). The adapter itself
  never reads, prints, or logs credential VALUES and never places them in argv
  (this part **is** code-enforced and tested: CASE 3/21/22).
- **Residual provider authority (OPERATIONALLY REQUIRED, not code-enforced):**
  GitHub `main` is **unprotected** (no branch-protection rulesets on this repo).
  The classic PAT (`repo` scope) therefore has direct push authority to `main`.
  The broker's *code-enforced* protected-ref policy (no autonomous push to
  `main`/`master`/`stable`/`release*`/`production*`) and the task-scoped
  `argent/` branch namespace are the mitigations; they are policy fences, not a
  provider-side protection. Treating `main` as protected is **OPERATIONALLY
  REQUIRED** until a real ruleset exists.

## 5. Code-enforced vs OPERATIONALLY REQUIRED vs OBSERVED LIVE

- **Code-enforced (deterministic, tested):** argv-only subprocesses; no-write
  default + live-write gate; trusted push URL only; credential never in
  argv/logs/audit; closed failure classification; bound-SHA mutation boundary
  (HIGH-6); authoritative audit (HIGH-13); fail-closed PR parsing/author binding
  (LOW-14/16); protected-ref + branch-namespace policy; publication safety.
- **OPERATIONALLY REQUIRED (policy/process, NOT enforced by this code):** leaving
  PR #1 DO-NOT-MERGE; not pushing to `main` directly; GitHub `main` being
  protected (absent a ruleset, this is a human/process obligation); the PAT's
  residual `main`-push authority being unused.
- **OBSERVED LIVE (facts about the completed run, recorded in docs):** the exact
  branch/PR/remote-main state (§3), the credential helper + scopes (§4), and the
  split-store ledger caveat (§3).

## 6. Tests (deterministic, pre-live)

- **50 I3-B tests** across `test_phase_i3b_live_gate.py` (9),
  `test_phase_i3b_adapter.py` (25), `test_phase_i3b_allowlist.py` (8),
  `test_phase_i3b_broker_live.py` (8), plus the new HIGH-13 broker audit tests in
  `test_phase_i3a_broker.py`.
- **Full suite: 2853 passed** (`/usr/bin/python3 -m pytest tests/ -q`).
- Regression coverage added this round: local-ref mismatch ⇒ push refused (no
  push invocation); missing local ref ⇒ refused; remote ref differs after push ⇒
  conflict; remote head differs before PR create ⇒ refused; stale-CAS finalize ⇒
  no `EXECUTED` row; reconcile after failed transition ⇒ no `RECONCILED_SUCCESS`
  row; authorize (autonomous + owner) ⇒ exactly one `AUTHORIZED` row; foreign-repo
  PR URL ⇒ no bind; author-unknown PR ⇒ never own.

## 7. C1-as-historical-artifact vs final local code

- **C1 (`e825111b`)** is the **historical pushed acceptance snapshot**: the exact
  code that produced the live branch/PR. It is preserved untouched on the
  acceptance branch.
- The **final local code** (this worktree, UNCOMMITTED) is C1 **plus** the closing
  parser fix and this fix round (HIGH-6/HIGH-13/LOW-14/16 + regression tests +
  these docs). Main will commit the final closing commit after verification;
  **the pushed C1 snapshot is NOT amended/force-updated** — the distinction
  between "what was pushed live" and "what the final local code is" is deliberate
  and must remain auditable.

## 8. Boundary to I3-C

- Real GitHub writes were performed by Main through the broker; scheduler wiring
  of `redrive_waiting`, any persistent live activation, and (if ever wanted) a
  real branch-protection ruleset are **I3-C / later**, not part of I3-B.
- No provider-side changes are part of this work; no schema change (the audit
  fix reused existing store APIs only).

## 9. GREEN (only Main marks this)

Pending Main independent verification of this fix round + full regression run.
Marker (only after Main): `ARGENT_PHASE_I3B_GITHUB_LIVE_ACCEPTANCE_GREEN`.

## Main Independent Verification + GREEN (2026-09-03 22:25)

| Prüfung | Ergebnis |
|---|---|
| I3-A + I3-B targeted | **145 passed** (eigener Lauf) |
| **FULL SUITE** | **2853 passed (52.98 s, eigener Lauf nach Fix-Round)** |
| Fix-Round-Code-Review | HIGH-6 (Bound-SHA vor Push + Remote-Readback nach Push + Remote-Head vor PR-Create; adversarial fake-git/gh-Tests), HIGH-13 (AUDIT_AUTHORIZED auf authorize-Pfaden; EXECUTED/RECONCILED_SUCCESS nur bei erfolgreicher autoritativer Transition; Stale-CAS ohne Audit-Fabrication), LOW-14 (PR-URL an expected Repo gebunden; _find_own_pr fail-closed bei unbekanntem Author), LOW-16 (unreachable Code entfernt), HIGH-17/LOW-18 (Docs wahrheitsgemäß: 50 I3-B-Tests/2853; Live-Branch argent/efe311ca-…-i3b-live-acceptance @ e825111b; PR #1 OPEN DO-NOT-MERGE; main unverändert ffc26642; Split-Store-Ledger-Caveat; klassisches PAT repo+workflow via gh git-credential helper; main ohne Rulesets; C1 als historisches Artefakt vs. finaler lokaler Code) |
| Provider-State | unverändert: main ffc26642, PR #1 OPEN @ e825111b, 0 Merges, 1 Branch, 1 PR |
| Sol-HIGH-Review | 1× (nach Live-Proof + deterministischen Tests): 3 HIGH (6/13/17) + 4 LOW (14/16/18 + 3-residual) — alle geschlossen in genau EINER Fix-Round |

Exit-Kriterien: Live-Service mit I3-A-Hardening ✓ · enabled/active/READY ✓ · Agent-Credential-Isolation ✓ · Broker-Credential-Zugriff ✓ · Account MokSeinNacken bewiesen ✓ · Token-Authority assessed (klassisches PAT; Residualrisiko dokumentiert) ✓ · Owner-Repo MokSeinNacken/argent-development-team (kein Fork, main==ffc26642) ✓ · task-scoped Allowlist ✓ · Branch-Namespace argent/* ✓ · Provenance/HEAD verifiziert ✓ · Broker-Push + Remote-SHA-Readback == erwartet ✓ · Push-Reconcile idempotent ✓ · genau 1 PR via Broker ✓ · PR-Head/Base/Readback korrekt ✓ · Duplikat-PR-Reconcile ✓ · Merge OWNER_GATE_REQUIRED, nicht ausgeführt ✓ · Credential absent aus Agent-Env/Context/Audit/Logs/Publication ✓ · Publication-Safety ✓ · External Content = Daten ✓ · Audit bounded ✓ · 1 Branch + 1 PR, 0 sonstige Writes ✓ · I3-B/I3-A/I2/I1/G/F/E/D/C/B grün ✓ · Full Suite grün ✓ · genau 1 Sol-HIGH-Review ✓ · alle Findings geschlossen, 0 ungelöst HIGH/CRITICAL ✓ · Docs komplett ✓.

Marker: `ARGENT_PHASE_I3B_GITHUB_LIVE_GREEN`. KEIN `ARGENT_PHASE_I3_GREEN`, KEIN `ARGENT_PHASE_I_GREEN`.
