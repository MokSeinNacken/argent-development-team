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
    init_repo,
    make_gh_integrated_source,
    make_gh_provenance,
    read_log,
    write_fake_executable,
    write_scenario,
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
    sha = "a" * 40
    scenario = {
        "repo view": {"code": 0, "stdout": "{}", "stderr": ""},
        "rev-parse": {"code": 0, "stdout": sha + "\n", "stderr": ""},
        "push": {"code": 0, "stdout": "", "stderr": ""},
        "ls-remote": {"code": 0,
                      "stdout": sha + "\trefs/heads/argent/t1-x\n",
                      "stderr": ""},
    }
    adapter = _adapter(tmp_path, scenario=scenario,
                       env_extra={"GH_TOKEN": fake_token})
    # A read AND a write both succeed WITHOUT the token in argv (the fake
    # would exit 97 if a token reached argv).
    r = adapter.read_repository(_req(action="read_repository"))
    assert r.outcome == OUTCOME_SUCCESS
    p = adapter.push_feature_branch(
        _req(action="push_feature_branch",
             parameters={"branch": "argent/t1-x", "sha": sha}))
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
    sha = "a" * 40
    scenario = {
        "rev-parse": {"code": 0, "stdout": sha + "\n", "stderr": ""},
        "push": {"code": 1, "stdout": "",
                 "stderr": "! [rejected] argent/t1-x -> argent/t1-x "
                           "(non-fast-forward)"},
    }
    adapter = _adapter(tmp_path, scenario=scenario)
    with pytest.raises(ProviderConflict):
        adapter.push_feature_branch(_req(
            parameters={"branch": "argent/t1-x", "sha": sha}))
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


def test_live_gh_pr_create_url_parsing(tmp_path, monkeypatch):
    """Real-gh behavior: ``gh pr create`` prints a URL (no --json) — the
    adapter must extract the PR number from it (live-flow discovery fix)."""
    from i3b_helpers import env_for, read_log, write_fake_executable, write_scenario
    from argent_core.github_provider_adapter import (
        GitHubProviderAdapter, github_acceptance_allowlist,
        github_acceptance_standing_policy,
    )
    from argent_core.external_action_broker import ExternalActionBroker, RequestState
    from i3a_helpers import TEST_MAC_KEY, make_env, make_holder

    gh = write_fake_executable(tmp_path, "gh")
    git = write_fake_executable(tmp_path, "git")
    adapter = GitHubProviderAdapter(
        live_write=True, gh_executable=gh, git_executable=git,
        trusted_repo_urls={"MokSeinNacken/argent-development-team":
                           "https://github.com/MokSeinNacken/"
                           "argent-development-team.git"},
        env=env_for(tmp_path, None),
    )
    core, project, sup = make_env(str(tmp_path / "a.db"))
    repo = init_repo(str(tmp_path / "r"))
    jid, cid, head, tid = make_gh_integrated_source(core, project, sup, repo)
    prov = make_gh_provenance(jid, cid, head)
    b = ExternalActionBroker(
        core._store, adapter=adapter,
        allowlist=github_acceptance_allowlist(),
        standing_policy=github_acceptance_standing_policy(),
        mac_key=TEST_MAC_KEY)
    hid, hep = make_holder(core, project, sup)
    branch = f"argent/{tid}-feature"
    # Remote head ref must equal head_sha (HIGH-6c pre-check) before the PR
    # create runs, and the created URL must bind to the expected repository.
    write_scenario(tmp_path, {
        "ls-remote": {"code": 0,
                      "stdout": f"{head}\trefs/heads/{branch}\n",
                      "stderr": ""},
        "pr create": {"code": 0,
                      "stdout": "https://github.com/MokSeinNacken/"
                                "argent-development-team/pull/7\n"},
    })
    req = b.create_request(
        provider="github", account="MokSeinNacken",
        action="create_pull_request",
        repository="MokSeinNacken/argent-development-team",
        resource_ref=branch, requested_scope="write",
        parameters={"head_branch": branch, "base_branch": "main",
                    "head_sha": head, "title": "Add feature", "body": ""},
        idempotency_key="ik-pr-url", provenance=prov)
    req = b.authorize_autonomous(req["request_id"])
    req = b.execute(req["request_id"], holder_job_id=hid, holder_lease_epoch=hep)
    assert req["state"] == RequestState.SUCCEEDED.value
    assert req["provider_object_id"] == "7"


# ---------------------------------------------------------------------------
# HIGH-6 — bound SHA enforced at the mutation boundary
# ---------------------------------------------------------------------------

def test_high6_local_ref_mismatch_refuses_push_no_push_invocation(tmp_path):
    bound = "a" * 40
    other = "b" * 40
    scenario = {"rev-parse": {"code": 0, "stdout": other + "\n", "stderr": ""}}
    adapter = _adapter(tmp_path, scenario=scenario)
    with pytest.raises(ProviderConflict):
        adapter.push_feature_branch(_req(
            parameters={"branch": "argent/t1-x", "sha": bound}))
    argv_log = read_log(tmp_path)
    assert argv_log != []  # the local ref check DID run
    assert all(a[0] != "push" for a in argv_log)  # NO git push invocation


def test_high6_local_ref_missing_refuses_push(tmp_path):
    bound = "a" * 40
    scenario = {"rev-parse": {"code": 128, "stdout": "", "stderr": "fatal"}}
    adapter = _adapter(tmp_path, scenario=scenario)
    with pytest.raises(ProviderConflict):
        adapter.push_feature_branch(_req(
            parameters={"branch": "argent/t1-x", "sha": bound}))
    assert all(a[0] != "push" for a in read_log(tmp_path))


def test_high6_remote_ref_differs_after_push_conflict(tmp_path):
    bound = "a" * 40
    other = "b" * 40
    scenario = {
        "rev-parse": {"code": 0, "stdout": bound + "\n", "stderr": ""},
        "push": {"code": 0, "stdout": "", "stderr": ""},
        "ls-remote": {"code": 0,
                      "stdout": other + "\trefs/heads/argent/t1-x\n",
                      "stderr": ""},
    }
    adapter = _adapter(tmp_path, scenario=scenario)
    with pytest.raises(ProviderConflict):
        adapter.push_feature_branch(_req(
            parameters={"branch": "argent/t1-x", "sha": bound}))


def test_high6_remote_head_differs_before_pr_create_refused(tmp_path):
    head_sha = "a" * 40
    other = "b" * 40
    scenario = {"ls-remote": {"code": 0,
                              "stdout": other + "\trefs/heads/argent/t1-x\n",
                              "stderr": ""}}
    adapter = _adapter(tmp_path, scenario=scenario)
    with pytest.raises(ProviderConflict):
        adapter.create_pull_request(_req(
            action="create_pull_request",
            parameters={"head_branch": "argent/t1-x", "base_branch": "main",
                        "head_sha": head_sha, "title": "t", "body": ""}))
    argv_log = read_log(tmp_path)
    assert all("pr create" not in " ".join(a) for a in argv_log)


# ---------------------------------------------------------------------------
# LOW-14 — PR parsing/author binding fail-closed
# ---------------------------------------------------------------------------

class _Proc:
    def __init__(self, stdout="", stderr=""):
        self.stdout = stdout
        self.stderr = stderr


def test_low14_parse_pr_number_binds_expected_repo():
    # A foreign-repository URL must never bind (fail closed).
    foreign = _Proc(
        stdout="https://github.com/attacker/other/pull/42\n")
    assert GitHubProviderAdapter._parse_pr_number(
        foreign, expected_repo="MokSeinNacken/argent-development-team") is None
    # The expected-repository URL binds.
    own = _Proc(
        stdout="https://github.com/MokSeinNacken/"
               "argent-development-team/pull/7\n")
    assert GitHubProviderAdapter._parse_pr_number(
        own, expected_repo="MokSeinNacken/argent-development-team") == 7
    # No expected_repo still parses (backward-compatible, no binding check).
    assert GitHubProviderAdapter._parse_pr_number(own) == 7


def test_low14_find_own_pr_fails_closed_on_unknown_author(tmp_path):
    import json
    branch = "argent/t1-x"
    head = "a" * 40
    # Missing author -> never treated as own.
    scenario = {"pr list": {
        "code": 0,
        "stdout": json.dumps([{"number": 1, "headRefName": branch,
                               "baseRefName": "main", "headRefOid": head}]),
        "stderr": ""}}
    adapter = _adapter(tmp_path, scenario=scenario)
    assert adapter._find_own_pr(
        GITHUB_ACCEPTANCE_REPOSITORY, branch, "main", head) is None
    # Non-dict author (a bare string) -> never treated as own.
    write_scenario(tmp_path, {"pr list": {
        "code": 0,
        "stdout": json.dumps([{"number": 1, "headRefName": branch,
                               "baseRefName": "main", "headRefOid": head,
                               "author": "MokSeinNacken"}]),
        "stderr": ""}})
    assert adapter._find_own_pr(
        GITHUB_ACCEPTANCE_REPOSITORY, branch, "main", head) is None
    # A matching author dict -> recognized as own.
    write_scenario(tmp_path, {"pr list": {
        "code": 0,
        "stdout": json.dumps([{"number": 1, "headRefName": branch,
                               "baseRefName": "main", "headRefOid": head,
                               "author": {"login": "MokSeinNacken"}}]),
        "stderr": ""}})
    pr = adapter._find_own_pr(
        GITHUB_ACCEPTANCE_REPOSITORY, branch, "main", head)
    assert pr is not None and pr["number"] == 1
