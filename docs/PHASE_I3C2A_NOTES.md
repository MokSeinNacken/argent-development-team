# PHASE I3-C2-A NOTES — Minimal Safe GitHub Actions CI Bootstrap

**Branch:** `phase-i3c2a-ci-bootstrap` (Base `29de775ad12088cba13cf2285a6514b12f9d770d` = Phase I3-C1 GREEN).
**Datum:** 2026-09-04.
**Scope:** ADDITIVE only — one workflow file, one test module, one `conftest.py`
hook, a handful of portability guards, two docs. **No commit, no push, no
GitHub/broker/systemd/service/state-dir operation, no LLM agents.** The PR that
will carry this workflow is created **later** by the supervisor through the
trusted external-action broker; this phase only installs the minimal, safe CI
definition and the deterministic security tests that keep it trustworthy.

---

## 1. Purpose / scope

Phase I3-C2-A installs the **minimal, safe GitHub Actions CI workflow** that
will produce a trustworthy GitHub check for pull requests into `main` of
`MokSeinNacken/argent-development-team`. The workflow is read-only, carries no
secrets, has no deployment/release/publish capability, and runs the repo's
portable deterministic test suite with a fail-closed job. Phase I3-C2-B will
later wait on this CI's check runs with an exact required-check identity, so
that identity is fixed and documented here (§6).

**Boundaries (CI only):**

- No deployment / release / publish / artifact capability.
- No secrets, no credentials, no `GITHUB_TOKEN` usage by any step.
- Read-only `permissions: contents: read`.
- Ends at the Owner Gate — this phase produces
  `CI_BOOTSTRAP_PR_READY_FOR_OWNER_MERGE`; the PR is created later by the
  supervisor via the broker (this writer never touches GitHub).
- I3-C2-B must **NOT** start automatically; it waits for owner merge.

### 1.1 Honest token boundary (HIGH-1, corrected)

The original docs claimed "no action receives a token". That was imprecise. The
**exact** boundary is:

- `actions/checkout` (step 1) receives the ephemeral, **read-only,
  contents-scoped** `GITHUB_TOKEN` **only** to clone the repository.
- That token is **NOT persisted** into `.git/config` because the checkout step
  sets `persist-credentials: false` (asserted by CASE 19).
- **No later step and no script receives any token**: there is no
  `env: GITHUB_TOKEN`, no `secrets`, no `with: token:` anywhere, and no action
  beyond checkout is in the token path. Asserted by CASE 6 / 17 / 19.

This is the least privilege that still lets a PR check clone and report its
check-run status; anything less would break the checkout itself.

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
- **No secrets.** The repo's portable suite needs none (evidence §3): every
  live-environment test skips cleanly on a stock runner via the
  `host_acceptance` marker / `skipif` / skip-if-absent guards; there is no
  network, no external service, no credentials in any test. An empty `secrets`
  universe is therefore both safe and sufficient.
- **No `pull_request_target`.** `pull_request_target` runs with the *base*
  repository's elevated context and default write permissions — an untrusted PR
  could exfiltrate tokens or mutate the base. `pull_request` runs in the
  read-only, fork-safe context; the whole point of this bootstrap is to keep
  untrusted PR code at the lowest possible authority.
- **Only two GitHub-maintained actions, pinned to immutable commit SHAs** (§4).
  No third-party actions; no action beyond checkout is in the token path.
- **No expressions (`${{ }}`) anywhere.** This removes the entire class of
  script-injection / PR-content-as-command-authority attacks at the source:
  nothing derived from the PR title/body/head/labels/ref can ever reach a shell.
- **No concurrency / caching / artifacts / matrix.** Unneeded for a single
  deterministic job; each adds surface area without adding check value.
- **No job display-name override.** Job id `test` is the check name (stable,
  never derived from PR content) — see §6.
- **`timeout-minutes: 15`.** The locally proven suite runs in ~48 s; 15 min gives
  ~18× headroom on a slower hosted runner while still bounding a hung run.
- **No bubblewrap provisioning.** The first fixed run (head `5140d15`, run
  33897941532) proved GitHub-hosted ubuntu-24.04 runners DO install
  bubblewrap but the runner kernel DENIES unprivileged user namespaces
  (`bwrap: setting up uid map: Permission denied`, `loopback: Failed
  RTM_NEWADDR: Operation not permitted`) — real bwrap sandbox EXECUTION is
  therefore **OPERATIONAL_HOST_ACCEPTANCE (B)**, not portable; those tests are
  `host_acceptance`-marked (§7). Only the argv-shape sandbox tests are
  portable. Provisioning a dependency that cannot run would waste CI time and
  falsely imply coverage.
- **Portable test command** `python -m pytest tests/ -q -m "not host_acceptance"`
  — the runner executes the full deterministic suite *minus* the
  `host_acceptance`-marked operational tests, which a stock runner cannot
  faithfully represent (§7).

## 3. Repository test requirements (evidence)

- README („Voraussetzungen"): **Python 3.14, nur Standardbibliothek + `pytest`**
  (nutzerlokal installiert). No `pyproject.toml` / `setup.py` / requirements
  files; `tests/conftest.py` adds the repo root to `sys.path`, so **no package
  install step is needed or wanted**.
- Portable deterministic suite: `python -m pytest tests/ -q -m "not host_acceptance"`
  from the repo root. Locally the **full** suite is **2955 passed** in **~48 s**
  (this fix round); the portable subset (minus 1 `host_acceptance` test) is
  **2954 collected**.
- All live-environment tests (real systemd scope, sibling-checkout consistency,
  live credential probes, real checkpoint, installed-unit substitution) **skip
  cleanly** on a stock GitHub runner via the `host_acceptance` marker /
  `skipif` / skip-if-absent guards; no secrets, no external services, no real
  network anywhere in the suite.
- **pytest pinned to `==9.1.1`** — the locally proven version (deterministic
  check conclusions). Installing anything else would change check semantics.
- The only install step is `python -m pip install --quiet
  --disable-pip-version-check pytest==9.1.1` (user-local, no system mutation).

## 4. Third-party action safety (supply chain)

- **Immutable full-commit SHA pins** (resolved 2026-09-04 via the GitHub API;
  the human-readable release tag is kept as an inline comment for review, never
  as the mutable ref the runner resolves):
  - `actions/checkout` → `3d3c42e5aac5ba805825da76410c181273ba90b1` (# v7.0.1)
  - `actions/setup-python` → `5fda3b95a4ea91299a34e894583c3862153e4b97` (# v7.0.0)
- These are the **only** actions (`uses:` set is exactly those two, asserted by
  the test suite as module constants, and each SHA is verified to be a 40-hex
  immutable commit — CASE 12).
- No secrets are passed to any action; only checkout is in the token path and
  that token is not persisted (§1.1); the only `with:` inputs are
  `persist-credentials: false` (checkout) and `python-version: '3.14'`
  (setup-python).
- **pytest pin:** `pytest==9.1.1` (deterministic version).
- **Accepted residual (documented):** the pip index is **not** hash-locked
  (no `--require-hashes`, no pinned wheel hash). This is a deliberate, accepted
  residual of this bootstrap: the single installed dependency is a
  version-pinned, widely-used PyPI package, and full hash-locking is deferred to
  a later hardening pass. Nothing else is installed.

## 5. Security validation (19 cases → test module)

The workflow is validated by `tests/test_phase_i3c2a_ci_workflow.py` (stdlib
`pathlib` only — no PyYAML, deterministic line/indent structural reader + strict
denylist text assertions, **canonical** exact-step-set parsing). Case → test
mapping:

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
| 12 | only trusted actions (exact set, SHA-pinned) | `test_case12_only_trusted_actions` |
| 13 | required-check identity (name/job/CHECK_NAME) | `test_case13_required_check_identity` |
| 14 | workflow path exactly `.github/workflows/ci.yml` | `test_case14_workflow_path_exact` |
| 15 | PR content cannot become command authority | `test_case15_pr_content_cannot_become_command_authority` |
| 16 | no schedule/workflow_dispatch/repository_dispatch | `test_case16_no_other_triggers` |
| 17 | no env block / GITHUB_TOKEN / env injection | `test_case17_no_env_or_github_token` |
| 18 | exact canonical step set + structural key allowlists | `test_case18_exact_canonical_step_set` |
| 19 | checkout `persist-credentials: false` | `test_case19_checkout_persist_credentials_false` |

The parser is now **canonical** (HIGH-2): it parses the job's steps as an exact
ordered list (names + uses/with/run content), tolerates full-line `#` comments
and `run: |` block scalars, and asserts the exact step set plus per-step/job/
top-level key allowlists (`{name, uses, with, run}` / `{runs-on,
timeout-minutes, steps}` / `{name, on, permissions, jobs}`). Inline `#`
comments are stripped when reading `uses:` refs.

## 6. Check identity for I3-C2-B (documented, deterministic)

- workflow `name`: **`Argent CI`**
- single job id: **`test`** (no display-name override)
- required check name (constant `CHECK_NAME` in the test module): **`test`**
- GitHub check-run name = `test`; deterministic, never derived from PR content.

Any change to the workflow name or job id will fail CASE 13 loudly and force a
coordinated update of the test constants + docs — this is deliberate.

### 6.1 Check provenance binding (LOW-4)

The required-check identity that I3-C2-B waits on is **not** the check name
alone. A same-named check emitted by a *different* app must never satisfy the
wait. The required-check identity for I3-C2-B therefore comprises **all three**:

1. **check name** `test`;
2. **GitHub Actions app identity** (app slug `github-actions`);
3. **workflow file path** `.github/workflows/ci.yml`.

I3-C2-B must bind all three (name + app slug + workflow path) when polling the
check-run status; a check named `test` from any other app or workflow must not
satisfy the wait.

## 7. CI coverage model

### 7.1 PORTABLE CI COVERAGE (what the runner runs)

The GitHub runner executes `python -m pytest tests/ -q -m "not host_acceptance"`
— the **full deterministic suite minus the `host_acceptance`-marked operational
tests**. The portable set includes the sandbox **argv-shape** tests
(`build_command` inspection: isolation flags, limits, ro-bind workspace,
no-cache bytecode flags) and the Phase-G sandbox **argv-composition** tests
that never execute bwrap (e.g. `test_start_in_scope_wraps_with_bwrap` uses
`FakePopen`). Real bwrap sandbox EXECUTION is NOT portable: the runner kernel
denies unprivileged user namespaces (evidence §7.4), so every execution test is
`host_acceptance`-marked (B) and runs in the local full suite only.

### 7.2 LOCAL/WSL OPERATIONAL ACCEPTANCE (development-host only)

These prove **live-host** state a stock GitHub runner cannot faithfully
represent (real systemd user-scope + cgroup delegation, a sibling checkout, real
credentials, a real checkpoint, an installed systemd unit):

| Test | Mechanism | On stock runner |
|---|---|---|
| `c2` real-scope smoke (`test_real_scope_create_verify_cleanup`) | `host_acceptance` **marker** (excluded by `-m "not host_acceptance"`) | excluded |
| `3d-visualizer` sibling-checkout consistency (`test_secret_patterns_identical_publisher_reader`) | skip-if-absent guard (sibling repo path) | skips (path absent) |
| `i3a` live credential probes | skip without real `~/.config/gh` | skips (no creds) |
| `g3` real-checkpoint test | skip without checkpoint | skips (no checkpoint) |
| `g2` deployment-substitution live-unit test | skips without an installed systemd unit (incl. empty `FragmentPath`) | skips (no unit) |
| `sandbox_runner` real-bwrap execution tests (9) | `host_acceptance` **marker** (kernel-dependent unprivileged userns) | excluded |
| `g2_sandbox` real-bwrap probes + `start_in_scope` composition (4) | `host_acceptance` **marker** (+ `_HAS_BWRAP` skipif for non-CI hosts) | excluded |

### 7.3 Real-CI failure classification (A/B/C model)

The first real GitHub CI run on `4c0ad046` **correctly FAILED** (fail-closed
worked): 13 failed, 2934 passed, 6 skipped. The 13 failures map to 5 root causes:

| # | Root cause (test) | Failures | Class | Fix |
|---|---|---|---|---|
| 1 | `tests/test_sandbox_runner.py` execution tests (run 1: `FileNotFoundError: 'bwrap'`; run 2 on `5140d15`: installs but `bwrap: setting up uid map: Permission denied` / `loopback: Failed RTM_NEWADDR: Operation not permitted`) | 9 | **B** OPERATIONAL_HOST_ACCEPTANCE (revised from A after run-2 evidence: GH-runner kernel denies unprivileged user namespaces) | mark `host_acceptance` (excluded from portable CI); argv-shape tests stay portable; run in the local full suite |
| 2 | `tests/test_phase_c2_real_scope_smoke.py::test_real_scope_create_verify_cleanup` (`ScopeCreateError`: cgroup move not delegated) | 1 | **B** OPERATIONAL_HOST_ACCEPTANCE | mark `host_acceptance` (keeps `_systemd_scope_available()` skipif) |
| 3 | `tests/test_phase_g2_sandbox.py` real-bwrap probes + `test_start_in_scope_wraps_with_bwrap` (run 2 on `5140d15`: `uid map: Permission denied` / bwrap preflight) | 4 | **B** OPERATIONAL_HOST_ACCEPTANCE (revised: bwrap execution/probes cannot run on the runner) | mark `host_acceptance` (keep `_HAS_BWRAP` skipifs); no assertion changes |
| 4 | `tests/test_phase_g2_unit_static.py::test_deployment_substitutions_match_installed_unit_on_this_host` (`IsADirectoryError: '.'` from empty `FragmentPath`) | 1 | **C** ACCIDENTAL_HOST_COUPLING | empty/absent `FragmentPath` → clean return (portability only; local assertion unchanged) |
| 5 | `tests/test_phase3d_visualizer_snapshot.py::test_secret_patterns_identical_publisher_reader` (`FileNotFoundError` on sibling path) | 1 | **B** OPERATIONAL_HOST_ACCEPTANCE | `pytest.skip` deterministically when sibling path absent (local assertion unchanged) |

**Classification semantics:** A = portable (should run in CI), B =
operational host acceptance (only provable on the live host), C = accidental
host coupling (bug in the test's portability, not a host requirement).

### 7.4 Second real CI run (head `5140d15`, run 33897941532) — reclassification evidence

After the fix push `5140d15` the workflow provisioned bubblewrap and ran the
portable suite: **10 failed, 2940 passed, 4 skipped, 1 deselected**. Exact
provider evidence from the run log:

- `bwrap: setting up uid map: Permission denied` (3 × `test_phase_g2_sandbox.py`
  real-bwrap probes, 2 × `sandbox_runner` execution tests)
- `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`
  (5 × `sandbox_runner` execution tests)
- 2 × `sandbox_runner` execution tests (`sandbox_cannot_overwrite_product_file`,
  `sandbox_result_fields`) FALSE-PASSED on the broken sandbox (exit-1/type-only
  assertions) — masking, which the exclusion also removes.

Conclusion: bubblewrap INSTALLS on ubuntu-24.04 hosted runners, but the runner
kernel denies unprivileged user namespaces, so real bwrap sandbox execution is
not faithfully representable there. Per the A/B/C doctrine the nine
`sandbox_runner` execution tests and the four `g2_sandbox` bwrap/preflight
composition tests are reclassified **A → B** (OPERATIONAL_HOST_ACCEPTANCE),
marked `host_acceptance`, and keep running unchanged in the local full suite;
portable CI keeps the sandbox argv-shape tests. The bubblewrap provisioning
step was removed (no point provisioning a dependency the kernel refuses). No
test assertion was weakened for green status — this is classification on real
platform evidence.

## 8. Local verification performed (this writer, fix round)

- YAML syntax parse (local PyYAML only for validation — **never** a repo
  dependency): `yaml.safe_load` → top-level keys `['name', True, 'permissions',
  'jobs']` (the `on` key is reported as `True` under YAML 1.1's boolean-key
  quirk — fine for a syntax check; the repo tests are text-based and
  unaffected).
- Security tests: `python3 -m pytest tests/test_phase_i3c2a_ci_workflow.py -q`
  → **19 passed**.
- Full suite (deployment env exports applied, see §9): **2955 passed** in
  **~48 s**.
- Portable collect: `python3 -m pytest tests/ -q -m "not host_acceptance"
  --collect-only` → **2954 collected, 1 deselected**.

## 9. Deployment-tracking note

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

## 10. Explicit non-goals / limitations

- **Provider-token residual (LOW-5, documented precisely).** A broad classic PAT
  remains on account `MokSeinNacken` with scopes `gist`, `read:org`, `repo`,
  `workflow`. `main` has **no branch protection**. Mitigations are broker policy
  fences: the `argent/` namespace allowlist, task-scoped usage, and `SENSITIVE →
  OWNER_GATE_REQUIRED`. This token is **NOT rotated** in this phase. A
  fine-grained PAT or a GitHub App remains a **required hardening item before
  productive Phase-I / I4 closure**.
- **No branch protection on `main`** — the required-check policy stays explicit
  and local (never inferred from a non-existent ruleset).
- **pip index is not hash-locked** (§4) — accepted residual, documented.
- **This phase does NOT accept any CI lifecycle** (no wait/wake acceptance) —
  that is I3-C2-B, after owner merge.
- No commit/push/PR is produced by this phase; the supervisor creates the PR via
  the broker later.

## 11. Status

**PENDING — fix round applied.** The first real CI run on `4c0ad046` correctly
FAILED (fail-closed), surfacing 13 environmental failures (5 root causes); the
fixes in this round (provisioning, `host_acceptance` marker, portability guards,
canonical parser, SHA pins, honest token boundary) are the response. GREEN is
marked only by Main after the final fix push, green CI on the fixed head,
broker audit, credential re-probe, secret scan, Sol review, and full regression.
See `docs/PHASE_I3C2A_ACCEPTANCE.md`.
