# PHASE I3-C2-A NOTES — Minimal Safe GitHub Actions CI Bootstrap

**Branch:** `phase-i3c2a-ci-bootstrap` (Base `29de775ad12088cba13cf2285a6514b12f9d770d` = Phase I3-C1 GREEN).
**Datum:** 2026-09-04.
**Scope:** ADDITIVE only — one workflow file, one test module, two docs. **No
commit, no push, no GitHub/broker/systemd/service/state-dir operation, no LLM
agents.** The PR that will carry this workflow is created **later** by the
supervisor through the trusted external-action broker; this phase only installs
the minimal, safe CI definition and the deterministic security tests that keep
it trustworthy.

---

## 1. Purpose / scope

Phase I3-C2-A installs the **minimal, safe GitHub Actions CI workflow** that
will produce a trustworthy GitHub check for pull requests into `main` of
`MokSeinNacken/argent-development-team`. The workflow is read-only, carries no
secrets, has no deployment/release/publish capability, and runs the repo's
deterministic test suite with a fail-closed job. Phase I3-C2-B will later wait
on this CI's check runs with an exact required-check identity, so that identity
is fixed and documented here (§6).

**Boundaries (CI only):**

- No deployment / release / publish / artifact capability.
- No secrets, no credentials, no `GITHUB_TOKEN` usage.
- Read-only `permissions: contents: read`.
- Ends at the Owner Gate — this phase produces
  `CI_BOOTSTRAP_PR_READY_FOR_OWNER_MERGE`; the PR is created later by the
  supervisor via the broker (this writer never touches GitHub).
- I3-C2-B must **NOT** start automatically; it waits for owner merge.

## 2. Design decisions + reasons

- **`on: pull_request → branches: [main]` only.** No `push` trigger — a push
  trigger is not justified for a PR-check bootstrap (the only consumer is the
  PR check I3-C2-B waits on). No `schedule` / `workflow_dispatch` /
  `repository_dispatch` (nothing periodic or manually/externally triggered is
  needed).
- **Explicit `permissions: contents: read`.** GitHub does not require more than
  read access for an ordinary PR CI check to *report* its check-run status; the
  `contents: read` grant is the least privilege needed for `actions/checkout`.
  No `pull-requests: write`, `checks: write`, `statuses: write` etc. — the
  check-run status is reported implicitly by the workflow run itself, not via an
  explicit write permission.
- **No secrets.** The repo's deterministic suite needs none (evidence §3): every
  live-environment test skips cleanly on a stock runner via existing `skipif`
  guards; there is no network, no external service, no credentials in any test.
  An empty `secrets` universe is therefore both safe and sufficient.
- **No `pull_request_target`.** `pull_request_target` runs with the *base*
  repository's elevated context and default write permissions — an untrusted PR
  could exfiltrate tokens or mutate the base. `pull_request` runs in the
  read-only, fork-safe context; the whole point of this bootstrap is to keep
  untrusted PR code at the lowest possible authority.
- **No third-party actions.** Only GitHub-maintained `actions/checkout` and
  `actions/setup-python` (§4). No secrets are passed to any action; no action
  receives a token.
- **No expressions (`${{ }}`) anywhere.** This removes the entire class of
  script-injection / PR-content-as-command-authority attacks at the source:
  nothing derived from the PR title/body/head/labels/ref can ever reach a shell.
- **No concurrency / caching / artifacts / matrix.** Unneeded for a single
  deterministic job; each adds surface area without adding check value.
- **No job display-name override.** Job id `test` is the check name (stable,
  never derived from PR content) — see §6.
- **`timeout-minutes: 15`.** The locally proven suite runs in ~51 s; 15 min gives
  ~18× headroom on a slower hosted runner while still bounding a hung run.

## 3. Repository test requirements (evidence)

- README („Voraussetzungen"): **Python 3.14, nur Standardbibliothek + `pytest`**
  (nutzerlokal installiert). No `pyproject.toml` / `setup.py` / requirements
  files; `tests/conftest.py` adds the repo root to `sys.path`, so **no package
  install step is needed or wanted**.
- Authoritative full deterministic suite (proven by Phase I3-C1 / I3-C2-A local
  runs): `python -m pytest tests/ -q` from the repo root → **2936 passed** in
  **~51 s** locally (commit `29de775ad…`).
- All live-environment tests (`bwrap`/`systemd-run`/real-checkpoint/real-gh)
  **skip cleanly** on a stock GitHub runner via existing `skipif` guards; no
  secrets, no external services, no real network anywhere in the suite.
- **pytest pinned to `==9.1.1`** — the locally proven version (deterministic
  check conclusions). Installing anything else would change check semantics.
- The only install step is `python -m pip install --quiet
  --disable-pip-version-check pytest==9.1.1` (user-local, no system mutation).

## 4. Third-party action safety

- `actions/checkout@v7.0.1` and `actions/setup-python@v7.0.0` are
  GitHub-maintained, current stable releases (verified 2026-09-04 via the GitHub
  releases API).
- These are the **only** actions (`uses:` set is exactly those two, asserted by
  the test suite as module constants).
- No secrets are passed to any action; no action receives any token; no action
  has a `with:` input beyond the pinned Python version.

## 5. Security validation (17 cases → test module)

The workflow is validated by `tests/test_phase_i3c2a_ci_workflow.py` (stdlib
`pathlib` only — no PyYAML, deterministic line/indent structural reader + strict
denylist text assertions). Case → test mapping:

| Case | Meaning | Test function |
|---|---|---|
| 1 | pull_request trigger restricted to `branches: [main]` | `test_case1_pull_request_trigger_restricted_to_main` |
| 2 | no `pull_request_target` | `test_case2_no_pull_request_target` |
| 3 | no secrets referenced | `test_case3_no_secrets_referenced` |
| 4 | permissions read-only (`contents: read` only, no write scope) | `test_case4_permissions_read_only_contents_only` |
| 5 | no deploy/release/publish/artifact capability | `test_case5_no_deploy_release_publish` |
| 6 | no external write token / credentials | `test_case6_no_external_write_token_or_credentials` |
| 7 | authoritative test command fail-closed | `test_case7_test_command_fail_closed` |
| 8 | no `continue-on-error` | `test_case8_no_continue_on_error` |
| 9 | no `${{` in any run block (PR title/body/head/labels) | `test_case9_no_run_shell_interpolation_from_pr` |
| 10 | no branch/ref/event input → shell command | `test_case10_no_branch_ref_shell_command` |
| 11 | timeout bounded (5 ≤ value ≤ 30) | `test_case11_timeout_bounded` |
| 12 | only trusted actions (exact set) | `test_case12_only_trusted_actions` |
| 13 | required-check identity (name/job/CHECK_NAME) | `test_case13_required_check_identity` |
| 14 | workflow path exactly `.github/workflows/ci.yml` | `test_case14_workflow_path_exact` |
| 15 | PR content cannot become command authority | `test_case15_pr_content_cannot_become_command_authority` |
| 16 | no schedule/workflow_dispatch/repository_dispatch | `test_case16_no_other_triggers` |
| 17 | no env block / GITHUB_TOKEN / env injection | `test_case17_no_env_or_github_token` |

## 6. Check identity for I3-C2-B (documented, deterministic)

- workflow `name`: **`Argent CI`**
- single job id: **`test`** (no display-name override)
- required check name (constant `CHECK_NAME` in the test module): **`test`**
- GitHub check-run name = `test`; deterministic, never derived from PR content.

Any change to the workflow name or job id will fail CASE 13 loudly and force a
coordinated update of the test constants + docs — this is deliberate.

## 7. Local verification performed (this writer)

- YAML syntax parse (local PyYAML only for validation — **never** a repo
  dependency): `yaml.safe_load` → top-level keys `['True', 'jobs', 'name',
  'permissions']` (the `on` key is reported as `True` under YAML 1.1's
  boolean-key quirk — fine for a syntax check; the repo tests are text-based and
  unaffected).
- New security tests: `python3 -m pytest tests/test_phase_i3c2a_ci_workflow.py
  -q` → **17 passed**.
- Full suite (deployment env exports applied, see §8): **2953 passed** (2936
  baseline + 17 new).

## 8. Deployment-tracking note

`g2-systemd/install-check.sh` + `tests/test_phase_g2_unit_static.py` default
`ARGENT_WORKTREE`/`ARGENT_DOC` to the live deployment. The live deployed unit
(`~/.config/systemd/user/argent-supervisor.service`) points at the
**phase-i3c1-ci-external-wait** worktree / `docs/PHASE_I3C1_ACCEPTANCE.md`, so a
local full-suite run on this branch must export:

```
ARGENT_WORKTREE=/home/pc/projects/argent-worktrees/phase-i3c1-ci-external-wait
ARGENT_DOC=file:/home/pc/projects/argent-worktrees/phase-i3c1-ci-external-wait/docs/PHASE_I3C1_ACCEPTANCE.md
```

(The committed in-repo defaults still say `phase-i3b-github-live-acceptance`;
that is a pre-existing stale default and is **not** modified by this phase. The
tests/scripts are env-parameterizable by design; on GitHub runners the live-unit
test skips cleanly because systemd is absent.)

## 9. Explicit non-goals / limitations

- **Broad classic PAT remains** — not rotated this phase; the fine-grained PAT /
  GitHub App replacement remains a pre-I4 hardening item.
- **No branch protection on `main`** — the required-check policy stays explicit
  and local (never inferred from a non-existent ruleset).
- **This phase does NOT accept any CI lifecycle** (no wait/wake acceptance) —
  that is I3-C2-B, after owner merge.
- No commit/push/PR is produced by this phase; the supervisor creates the PR via
  the broker later.

## 10. Status

**PENDING.** Files written and verified locally; GREEN is marked only by Main
after the live push/PR flow, broker audit, credential re-probe, secret scan,
Sol review, and full regression. See `docs/PHASE_I3C2A_ACCEPTANCE.md`.
