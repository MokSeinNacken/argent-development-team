"""Phase I3-B — real GitHub adapter unit tests (argv / classification).

Deterministic, no network, no real GitHub writes: the real
``GitHubProviderAdapter`` runs against a scripted fake ``gh``/``git``
executable (tmp dir).  Covers the argv-level safety surface: no credential in
argv, trusted push URL only, no direct write path, conservative PR guards,
secret redaction, and provider failure classification (403/429/5xx/network).
"""

from __future__ import annotations

import pytest

from argent_core.external_action_broker import (
    ExternalActionRequest,
    sanitize_provider_detail,
    validate_pr_body,
    validate_pr_title,
)
from argent_core.external_provider_adapter import (
    OUTCOME_SUCCESS,
    ProviderConflict,
    ProviderCredentialError,
    ProviderNetworkError,
    ProviderRateLimited,
    ProviderUnavailable,
    ProviderValidationError,
    ProviderWriteDisabled,
)
from argent_core.github_provider_adapter import (
    GITHUB_ACCEPTANCE_CANONICAL_URL,
    GITHUB_ACCEPTANCE_REPOSITORY,
    GitHubProviderAdapter,
    canonicalize_repo_identity,
    classify_gh_failure,
)

from i3b_helpers import (
    env_for,
    read_log,
    write_fake_executable,
)


def _req(**kw):
    defaults = dict(
        request_id="xr_" + "0" * 24, provider="github", account="MokSeinNacken",
        action="push_feature_branch", policy_class="BOUNDED_WRITE",
        repository=GITHUB_ACCEPTANCE_REPOSITORY, resource_ref="argent/t1-x",
        source_job_id="j", source_candidate_id="c", requested_scope="write",
        parameters={}, expected_preconditions={}, idempotency_key="ik",
        provenance_version=1, provenance_hash="h" * 64,
    )
    defaults.update(kw)
    return ExternalActionRequest(**defaults)


def _adapter(tmp_path, *, live_write=True, trusted_repo_urls=None,
             scenario=None, env_extra=None):
    gh = write_fake_executable(tmp_path, "gh")
    git = write_fake_executable(tmp_path, "git")
    env = env_for(tmp_path, scenario)
    if env_extra:
        env.update(env_extra)
    return GitHubProviderAdapter(
        live_write=live_write, gh_executable=gh, git_executable=git,
        trusted_repo_urls=(trusted_repo_urls if trusted_repo_urls is not None
                           else {GITHUB_ACCEPTANCE_REPOSITORY:
                                 GITHUB_ACCEPTANCE_CANONICAL_URL}),
        env=env,
    )


# ---------------------------------------------------------------------------
# CASE 3 / 21 — credential never in argv (sandbox probe fail-closed)
# ---------------------------------------------------------------------------

def test_case3_fake_executable_refuses_credential_in_argv(tmp_path):
    # The fake probe itself fails closed: a token-shaped argv exits 97.
    gh = write_fake_executable(tmp_path, "gh")
    import subprocess
    token = "ghp_" + "A" * 36
    proc = subprocess.run([gh, "repo", "view", token], capture_output=True,
                          text=True, env=env_for(tmp_path, {}))
    assert proc.returncode == 97


def test_case21_adapter_never_places_credential_in_argv(tmp_path):
    fake_token = "ghp_" + "A" * 36
    scenario = {"*": {"code": 0, "stdout": "{}", "stderr": ""}}
    adapter = _adapter(tmp_path, scenario=scenario,
                       env_extra={"GH_TOKEN": fake_token})
    # A read AND a write both succeed WITHOUT the token in argv (the fake
    # would exit 97 if a token reached argv).
    r = adapter.read_repository(_req(action="read_repository"))
    assert r.outcome == OUTCOME_SUCCESS
    p = adapter.push_feature_branch(
        _req(action="push_feature_branch",
             parameters={"branch": "argent/t1-x", "sha": "a" * 40}))
    assert p.outcome == OUTCOME_SUCCESS
    for argv in read_log(tmp_path):
        assert all("ghp_" not in a for a in argv)


def test_case3_adapter_invocations_record_has_no_credential(tmp_path):
    adapter = _adapter(tmp_path, scenario={"*": {"code": 0, "stdout": "{}",
                                                 "stderr": ""}},
                       env_extra={"GH_TOKEN": "ghp_" + "A" * 36})
    adapter.read_repository(_req(action="read_repository"))
    assert adapter.invocations
    for argv in adapter.invocations:
        assert all("ghp_" not in a for a in argv)
        assert all("token" not in a.lower() for a in argv)


# ---------------------------------------------------------------------------
# CASE 4 / 5 — unapproved + third-party/upstream repo rejected
# ---------------------------------------------------------------------------

def test_case4_unapproved_repo_rejected(tmp_path):
    adapter = _adapter(tmp_path, trusted_repo_urls={})  # nothing approved
    with pytest.raises(ProviderValidationError):
        adapter.push_feature_branch(_req(
            parameters={"branch": "argent/t1-x", "sha": "a" * 40}))
    assert read_log(tmp_path) == []


def test_case5_third_party_repo_rejected(tmp_path):
    adapter = _adapter(tmp_path)  # only the acceptance repo is trusted
    for repo in ("openclaw/openclaw", "MokSeinNacken/openclaw",
                 "attacker/argent-development-team"):
        with pytest.raises(ProviderValidationError):
            adapter.push_feature_branch(_req(
                repository=repo, parameters={"branch": "argent/t1-x",
                                             "sha": "a" * 40}))
    assert read_log(tmp_path) == []


# ---------------------------------------------------------------------------
# CASE 6 — repo identity canonicalization
# ---------------------------------------------------------------------------

def test_case6_repo_identity_canonicalized():
    expect = "MokSeinNacken/argent-development-team"
    assert canonicalize_repo_identity(expect) == expect
    assert canonicalize_repo_identity(expect + ".git") == expect
    assert canonicalize_repo_identity(
        "https://github.com/" + expect + ".git") == expect
    assert canonicalize_repo_identity("git@github.com:" + expect + ".git") == expect
    assert canonicalize_repo_identity(expect + "/") == expect


# ---------------------------------------------------------------------------
# CASE 13 / 16 — no direct write path (no-write adapter refuses)
# ---------------------------------------------------------------------------

def test_case13_direct_push_refused_without_live_write(tmp_path):
    adapter = _adapter(tmp_path, live_write=False)
    req = _req(parameters={"branch": "argent/t1-x", "sha": "a" * 40})
    with pytest.raises(ProviderWriteDisabled):
        adapter.push_feature_branch(req)
    assert read_log(tmp_path) == []


def test_case16_direct_pr_create_refused_without_live_write(tmp_path):
    adapter = _adapter(tmp_path, live_write=False)
    req = _req(action="create_pull_request",
               parameters={"head_branch": "argent/t1-x", "base_branch": "main",
                           "head_sha": "a" * 40, "title": "t", "body": ""})
    with pytest.raises(ProviderWriteDisabled):
        adapter.create_pull_request(req)
    assert read_log(tmp_path) == []


# ---------------------------------------------------------------------------
# CASE 14 — remote expected SHA reconciles
# ---------------------------------------------------------------------------

def test_case14_remote_expected_sha_reconciles(tmp_path):
    sha = "a" * 40
    scenario = {"ls-remote": {"code": 0,
                              "stdout": f"{sha}\trefs/heads/argent/t1-x\n",
                              "stderr": ""}}
    adapter = _adapter(tmp_path, scenario=scenario)
    obs = adapter.observe(_req(parameters={"branch": "argent/t1-x",
                                           "sha": sha}))
    assert obs.found is True
    assert obs.object_id == sha
    # A different expected sha does NOT reconcile (never claims success).
    obs2 = adapter.observe(_req(parameters={"branch": "argent/t1-x",
                                            "sha": "b" * 40}))
    assert obs2.found is False


# ---------------------------------------------------------------------------
# CASE 15 — remote different SHA never force-overwritten (conflict, no force)
# ---------------------------------------------------------------------------

def test_case15_remote_sha_conflict_no_force(tmp_path):
    scenario = {"push": {"code": 1, "stdout": "",
                         "stderr": "! [rejected] argent/t1-x -> argent/t1-x "
                                   "(non-fast-forward)"}}
    adapter = _adapter(tmp_path, scenario=scenario)
    with pytest.raises(ProviderConflict):
        adapter.push_feature_branch(_req(
            parameters={"branch": "argent/t1-x", "sha": "a" * 40}))
    for argv in read_log(tmp_path):
        assert "--force" not in argv
        assert "-f" not in argv
        assert "+" not in argv  # never a force-push refspec


# ---------------------------------------------------------------------------
# CASE 18 — PR head/base mismatch fails conservative
# ---------------------------------------------------------------------------

def test_case18_pr_head_base_mismatch_fails(tmp_path):
    adapter = _adapter(tmp_path)
    with pytest.raises(ProviderValidationError):
        adapter.create_pull_request(_req(
            action="create_pull_request",
            parameters={"head_branch": "main", "base_branch": "main",
                        "head_sha": "a" * 40, "title": "t", "body": ""}))
    assert read_log(tmp_path) == []


# ---------------------------------------------------------------------------
# CASE 19 — publication secret rejected/redacted
# ---------------------------------------------------------------------------

def test_case19_secret_title_rejected_and_body_redacted():
    secret = "ghp_" + "A" * 36
    with pytest.raises(ValueError):
        validate_pr_title(f"fix {secret}")
    body = validate_pr_body(f"see {secret} for access")
    assert "ghp_" not in body
    assert "[REDACTED]" in body


def test_case19_adapter_rejects_secret_title(tmp_path):
    adapter = _adapter(tmp_path)
    with pytest.raises(ProviderValidationError):
        adapter.create_pull_request(_req(
            action="create_pull_request",
            parameters={"head_branch": "argent/t1-x", "base_branch": "main",
                        "head_sha": "a" * 40,
                        "title": "fix ghp_" + "A" * 36, "body": ""}))
    assert read_log(tmp_path) == []


# ---------------------------------------------------------------------------
# CASE 22 — credential absent from logs / detail / publication
# ---------------------------------------------------------------------------

def test_case22_credential_absent_from_detail_and_classification():
    secret = "ghp_" + "A" * 36
    out = sanitize_provider_detail(f"failed {secret} token")
    assert "ghp_" not in out
    err = classify_gh_failure(403, f"denied {secret}")
    assert "ghp_" not in str(err)


# ---------------------------------------------------------------------------
# CASE 23 / 24 — failure classification
# ---------------------------------------------------------------------------

def test_case23_403_401_classification():
    assert isinstance(classify_gh_failure(403, "permission denied"),
                      ProviderCredentialError)
    assert isinstance(classify_gh_failure(401, ""), ProviderCredentialError)
    # where the remote's POLICY rejection is observable -> validation, not auth.
    assert isinstance(classify_gh_failure(403, "protected branch"),
                      ProviderValidationError)
    assert isinstance(classify_gh_failure(403, "required status check"),
                      ProviderValidationError)


def test_case24_rate_limit_and_outage_classification():
    assert isinstance(classify_gh_failure(429, ""), ProviderRateLimited)
    assert isinstance(classify_gh_failure(200, "secondary rate limit"),
                      ProviderRateLimited)
    assert isinstance(classify_gh_failure(500, ""), ProviderUnavailable)
    assert isinstance(classify_gh_failure(503, ""), ProviderUnavailable)
    assert isinstance(classify_gh_failure(400, ""), ProviderValidationError)
    assert isinstance(classify_gh_failure(409, ""), ProviderConflict)


# ---------------------------------------------------------------------------
# CASE 25 — network/transport failure maps to a retryable outage
# ---------------------------------------------------------------------------

def test_case25_network_failure_maps_to_network_error(tmp_path):
    adapter = GitHubProviderAdapter(
        live_write=True, git_executable="/nonexistent/git",
        trusted_repo_urls={GITHUB_ACCEPTANCE_REPOSITORY:
                           GITHUB_ACCEPTANCE_CANONICAL_URL})
    req = _req(parameters={"branch": "argent/t1-x", "sha": "a" * 40})
    with pytest.raises(ProviderNetworkError):
        adapter.push_feature_branch(req)
    # ProviderNetworkError IS-A ProviderUnavailable (retryable outage class).
    with pytest.raises(ProviderUnavailable):
        adapter.push_feature_branch(req)


def test_case25_timeout_maps_to_network_error(tmp_path):
    # A fake run that always times out proves timeout -> network (retryable).
    class _TimeoutRun:
        def __call__(self, argv, **kw):
            import subprocess
            raise subprocess.TimeoutExpired(argv, 60)

    adapter = GitHubProviderAdapter(
        live_write=True, git_executable="git", run=_TimeoutRun(),
        trusted_repo_urls={GITHUB_ACCEPTANCE_REPOSITORY:
                           GITHUB_ACCEPTANCE_CANONICAL_URL})
    with pytest.raises(ProviderNetworkError):
        adapter.push_feature_branch(_req(
            parameters={"branch": "argent/t1-x", "sha": "a" * 40}))
