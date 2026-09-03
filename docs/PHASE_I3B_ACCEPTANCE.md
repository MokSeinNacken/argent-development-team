# PHASE I3-B ACCEPTANCE — GitHub Feature-Branch Push + PR Live Acceptance

**Branch:** `phase-i3b-github-live-acceptance` (Base `ffc266421ca0d53d1a5a7c2d078194f88e65868b` = Phase I3-A GREEN).
**Datum:** 2026-09-03.
**Scope:** code + deterministic tests + docs only. **No commit, no push, no
live service/systemd/state-dir/gh-config mutation, no network writes, no real
GitHub writes, no credential/token changes or printing, no LLM agents.**

**STATUS: NOT GREEN (Writer-Ergebnis; GREEN nur durch Main nach unabhängiger
Verifikation + Sol-Review).**

---

## 1. Deliverables

- **NEW** `argent_core/github_provider_adapter.py` — real `GitHubProviderAdapter`
  (argv subprocesses), live-write activation gate, acceptance identity
  constants, repository-identity canonicalization, `classify_gh_failure`,
  task-scoped `github_acceptance_allowlist()` / `github_acceptance_standing_policy()`.
- **EXTENDED** `argent_core/external_provider_adapter.py` — additive
  `ProviderNetworkError(ProviderUnavailable)` (no behavioral change to existing
  classes).
- **NEW** `tests/i3b_helpers.py` — scripted fake `gh`/`git` executable harness
  (tmp dir, no network, fails-closed on any credential in argv).
- **Tests** `tests/test_phase_i3b_live_gate.py` (9), `_adapter.py` (18),
  `_allowlist.py` (8), `_broker_live.py` (8) — **43 I3-B tests**.
- **Docs** `docs/PHASE_I3B_NOTES.md` + this file.
- **Deployment-tracking defaults** updated: `tests/test_phase_g2_unit_static.py`
  + `g2-systemd/install-check.sh` (G3 → I3-A worktree/`PHASE_I3A_ACCEPTANCE.md`).

## 2. Adapter contract

- **argv-only** subprocesses (NO `shell=True`/`eval`/`exec`); executables
  injectable (default `gh` for reads/PR ops, `git` for push).
- **NO-WRITE default**: mutation methods raise `ProviderWriteDisabled` unless
  the adapter is live-enabled AND the live-write gate passes (CASE 1).
- **Trusted push URL** from `trusted_repo_urls` (canonical repo identity → URL);
  an agent-supplied URL is never accepted (CASE 4/5/6).
- **Failure classification** → closed I3-A taxonomy (401/403→credential, 403
  policy→validation, 409/non-fast-forward→conflict, 400/422→validation,
  429→rate-limit, ≥500→unavailable, transport→`ProviderNetworkError`).
- **Credential handling controller-side**; tokens never embedded/echoed/logged.

## 3. Live-write activation gate (CASE 1/2)

`live_write_gate(activation_flag)` = explicit flag AND (credential-mask
resolver present AND `LIVE_WRITE_REQUIRED_COMMIT ==
"ffc266421ca0d53d1a5a7c2d078194f88e65868b"`). Fails closed otherwise.

## 4. Live-flow design (exact repo / allowlist / branch / PR plan)

- **Provider/account/repo:** `github` / `MokSeinNacken` /
  `MokSeinNacken/argent-development-team` (owner-controlled, NOT a fork,
  default branch `main`, remote `main` SHA == `ffc2664…`).
- **Allowlist:** autonomous reads + `push_feature_branch` + `create_pull_request`;
  SENSITIVE (`merge_pull_request`/`create_release`/`deploy_production`)
  PERMITTED-but-owner-gated; branch namespace `argent/`, PR base `main`.
- **Branch plan:** push `argent/<task-id>-<slug>` (validated namespace, never a
  protected ref), SHA must equal the candidate's integrated HEAD.
- **PR plan:** `--head argent/<task-id>-<slug> --base main`, bounded/sanitized
  title+body (secret-rejected title, secret-redacted body), own-PR updates only.
- **Credential/helper:** `gh` auth via `~/.config/gh` (no git credential.helper);
  credential transport controller-side (never in argv/logs).

## 5. Case → test mapping (30 cases)

| Case | Meaning | Test(s) |
|---|---|---|
| 1 | Live mode cannot activate without I3-A marker/commit | `test_case1_gate_requires_explicit_flag`, `test_case1_gate_requires_credential_mask_resolver`, `test_case1_gate_fails_on_pre_i3a_marker`, `test_case1_adapter_write_disabled_without_flag`, `test_case1_adapter_write_disabled_on_pre_i3a_marker`, `test_case1_adapter_live_enabled_only_with_gate` |
| 2 | Deployment must contain credential-mask fix (resolves `~/.config/gh`) | `test_case2_credential_mask_resolver_present`, `test_case2_resolved_mask_covers_gh_config_dir`, `test_case2_resolved_mask_covers_gh_dir_explicit_home` |
| 3 | Sandbox credential probe fail-closed (argv) | `test_case3_fake_executable_refuses_credential_in_argv`, `test_case3_adapter_invocations_record_has_no_credential` |
| 4 | Unapproved repo rejected | `test_case4_unapproved_repo_rejected` |
| 5 | Third-party/upstream repo rejected (openclaw/openclaw + fork) | `test_case5_third_party_repo_rejected` |
| 6 | Repo identity canonicalized | `test_case6_repo_identity_canonicalized` |
| 7 | Task-scoped allowlist permits exact repo | `test_case7_allowlist_permits_exact_repo` |
| 8 | Same policy denies different repo | `test_case8_allowlist_denies_different_repo_or_account` |
| 9 | Branch namespace enforced | `test_case9_branch_namespace_enforced` |
| 10 | Protected branch rejected | `test_case10_protected_branch_rejected` |
| 11 | Pre-push HEAD mismatch rejected | `test_case11_pre_push_head_mismatch_rejected` |
| 12 | Missing evidence rejects push | `test_case12_missing_evidence_rejects_push` |
| 13 | Push occurs only via broker path | `test_case13_direct_push_refused_without_live_write`, `test_case13_push_via_broker_only` |
| 14 | Remote expected SHA reconciles success | `test_case14_remote_expected_sha_reconciles` |
| 15 | Remote different SHA never force-overwritten | `test_case15_remote_sha_conflict_no_force` |
| 16 | PR create only via broker | `test_case16_direct_pr_create_refused_without_live_write` |
| 17 | Duplicate logical PR reconciles existing PR | `test_case17_duplicate_pr_reconciles_existing` |
| 18 | PR head/base mismatch fails conservative | `test_case18_pr_head_base_mismatch_fails` |
| 19 | Publication secret rejected/redacted (ghp_) | `test_case19_secret_title_rejected_and_body_redacted`, `test_case19_adapter_rejects_secret_title` |
| 20 | Merge request OWNER_GATE_REQUIRED | `test_case20_merge_request_owner_gate_required`, `test_case20_release_and_deploy_owner_gate_required` |
| 21 | Credential absent from agent sandbox (argv) | `test_case21_adapter_never_places_credential_in_argv` |
| 22 | Credential absent from logs/audit/publication | `test_case22_credential_absent_from_detail_and_classification` |
| 23 | Provider 403 classification | `test_case23_403_401_classification` |
| 24 | Rate-limit classification | `test_case24_rate_limit_and_outage_classification` |
| 25 | Network/provider outage preserves safe state | `test_case25_network_failure_maps_to_network_error`, `test_case25_timeout_maps_to_network_error`, `test_case25_broker_network_outage_preserves_safe_state` |
| 26 | Stale broker holder cannot finalize live action | `test_case26_stale_holder_cannot_finalize` |
| 27 | Restart/reconcile does not duplicate push | `test_case27_restart_reconcile_no_duplicate_push` |
| 28 | Restart/reconcile does not duplicate PR | `test_case28_restart_reconcile_no_duplicate_pr` |
| 29 | Exactly one logical push action recorded | `test_case29_exactly_one_logical_push_action_recorded` |
| 30 | Exactly one logical PR-create action recorded | `test_case30_exactly_one_logical_pr_action_recorded` |

## 6. Deployment-tracking change

The live unit now points at the I3-A worktree; `test_phase_g2_unit_static.py`
defaults + `g2-systemd/install-check.sh` defaults updated accordingly
(G3 → I3-A), env-parameterizable.

## 7. Boundary to I3-C

Real GitHub writes are performed later by Main through the broker; scheduler
wiring of `redrive_waiting` and the credential transport for `git push` are
controller-side (I3-C / live activation), not part of I3-B code.

## 8. GREEN (only Main marks this)

Pending Main independent verification + Sol review. Marker (only after Main):
`ARGENT_PHASE_I3B_GITHUB_LIVE_ACCEPTANCE_GREEN`.
