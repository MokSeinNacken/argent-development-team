"""Phase I3-C1 — GitHub CI adapter normalization + classification (READ-ONLY).

Deterministic: a scripted ``run`` callable replaces ``gh`` (argv only, no
network, no real GitHub).  Verifies normalization of PR state + check-runs +
commit status into the bounded :class:`CiRead` model, provider failure
classification, and the structural absence of any write path.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from argent_core.ci_external_wait import (
    PR_MERGED,
    PR_OPEN,
    PROVIDER_ERROR_RATE_LIMITED,
    PROVIDER_ERROR_UNAVAILABLE,
    PROVIDER_ERROR_UNKNOWN,
    GitHubCiAdapter,
    normalize_check_runs,
    normalize_statuses,
)


def _proc(stdout="", stderr="", code=0):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def _adapter(responses, calls):
    def run(argv, **kw):
        calls.append(list(argv))
        key = " ".join(argv)
        for sig, resp in responses.items():
            if sig in key:
                return _proc(**resp)
        return _proc()
    return GitHubCiAdapter(run=run)


def test_normalize_check_runs_maps_conclusion_and_event_version():
    # Case 32a: GitHub check-runs normalize to bounded CiCheck; event_version
    # is the max check-run id (monotonic transition identity).
    runs = [
        {"id": 10, "name": "ci", "status": "completed",
         "conclusion": "success", "details_url": "https://x/runs/10"},
        {"id": 11, "name": "unit-tests", "status": "completed",
         "conclusion": "failure", "html_url": "https://x/runs/11"},
    ]
    checks, ev = normalize_check_runs(runs)
    assert ev == 11
    assert {c.name: c.conclusion for c in checks} == {
        "ci": "SUCCESS", "unit-tests": "FAILURE"}
    assert checks[0].status == "COMPLETED"
    assert checks[0].details_url == "https://x/runs/10"


def test_normalize_statuses_maps_commit_status():
    # Case 32b: commit status contexts normalize (success/failure/pending).
    statuses = [
        {"context": "ci/build", "state": "success", "target_url": "https://x"},
        {"context": "ci/lint", "state": "pending"},
        {"context": "ci/deploy", "state": "error"},
    ]
    checks = normalize_statuses(statuses)
    by = {c.name: c for c in checks}
    assert by["ci/build"].conclusion == "SUCCESS"
    assert by["ci/lint"].conclusion is None
    assert by["ci/deploy"].conclusion == "FAILURE"


def test_read_ci_state_open_with_checks(capsys=None):
    calls = []
    adapter = _adapter({
        "pr view": {"stdout": json.dumps({
            "number": 1, "state": "OPEN",
            "headRefOid": "a" * 40, "baseRefName": "main",
            "mergedAt": None, "closedAt": None})},
        "check-runs": {"stdout": json.dumps({"total_count": 1, "check_runs": [
            {"id": 5, "name": "ci", "status": "completed",
             "conclusion": "success", "details_url": "https://x/runs/5"}]})},
        "/status": {"stdout": json.dumps({"state": "success", "statuses": []})},
    }, calls)
    read = adapter.read_ci_state("MokSeinNacken/argent-development-team", 1,
                                 "a" * 40)
    assert read.pr_state == PR_OPEN
    assert read.pr_head_sha == "a" * 40
    assert read.base_ref == "main"
    assert read.provider_error is None
    assert read.event_version == 5
    assert read.checks[0].name == "ci"
    assert read.checks[0].conclusion == "SUCCESS"


def test_read_ci_state_merged_pr():
    calls = []
    adapter = _adapter({
        "pr view": {"stdout": json.dumps({
            "number": 1, "state": "OPEN",
            "headRefOid": "a" * 40, "baseRefName": "main",
            "mergedAt": "2026-09-03T00:00:00Z", "closedAt": None})},
    }, calls)
    read = adapter.read_ci_state("MokSeinNacken/argent-development-team", 1,
                                 "a" * 40)
    assert read.pr_state == PR_MERGED


def test_read_ci_state_reveals_head_movement():
    # The adapter returns the PR's CURRENT head from PR state so the controller
    # can detect a head change vs the bound SHA.
    calls = []
    adapter = _adapter({
        "pr view": {"stdout": json.dumps({
            "number": 1, "state": "OPEN",
            "headRefOid": "b" * 40, "baseRefName": "main"})},
    }, calls)
    read = adapter.read_ci_state("MokSeinNacken/argent-development-team", 1,
                                 "a" * 40)
    assert read.pr_head_sha == "b" * 40


def test_read_ci_state_rate_limited():
    calls = []
    adapter = _adapter({
        "pr view": {"code": 1, "stderr": "API rate limit exceeded (429)"},
    }, calls)
    read = adapter.read_ci_state("MokSeinNacken/argent-development-team", 1,
                                 "a" * 40)
    assert read.provider_error == PROVIDER_ERROR_RATE_LIMITED


def test_read_ci_state_provider_unavailable():
    calls = []
    adapter = _adapter({
        "pr view": {"code": 503, "stderr": "service unavailable"},
    }, calls)
    read = adapter.read_ci_state("MokSeinNacken/argent-development-team", 1,
                                 "a" * 40)
    assert read.provider_error == PROVIDER_ERROR_UNAVAILABLE


def test_github_ci_adapter_has_no_write_path():
    # Case 44: the GitHub CI adapter is structurally READ-ONLY — no mutation
    # methods exist at all (no push/merge/close/comment/rerun path).
    adapter = GitHubCiAdapter()
    for method in ("push_feature_branch", "create_pull_request",
                   "update_pull_request", "merge_pull_request",
                   "create_release", "deploy_production"):
        assert not hasattr(adapter, method), f"unexpected write path {method}"
    assert adapter.write_enabled is False


# ---------------------------------------------------------------------------
# LOW-6: malformed/non-dict/empty provider success responses fail closed
# ---------------------------------------------------------------------------

def test_malformed_check_runs_json_yields_provider_error():
    # LOW-6: a successful gh response whose check-runs JSON is malformed is
    # classified as provider_error (UNKNOWN), never silently an empty check set.
    calls = []
    adapter = _adapter({
        "pr view": {"stdout": json.dumps({
            "number": 1, "state": "OPEN", "headRefOid": "a" * 40,
            "baseRefName": "main", "mergedAt": None, "closedAt": None})},
        "check-runs": {"stdout": "not-json{"},
    }, calls)
    read = adapter.read_ci_state("MokSeinNacken/argent-development-team", 1,
                                 "a" * 40)
    assert read.provider_error == PROVIDER_ERROR_UNKNOWN


def test_non_dict_check_runs_yields_provider_error():
    # LOW-6: a check-runs response that is valid JSON but not an object is
    # rejected (fail-closed), never normalized to an empty check set.
    calls = []
    adapter = _adapter({
        "pr view": {"stdout": json.dumps({
            "number": 1, "state": "OPEN", "headRefOid": "a" * 40,
            "baseRefName": "main"})},
        "check-runs": {"stdout": json.dumps([1, 2, 3])},
    }, calls)
    read = adapter.read_ci_state("MokSeinNacken/argent-development-team", 1,
                                 "a" * 40)
    assert read.provider_error == PROVIDER_ERROR_UNKNOWN


def test_malformed_pr_view_json_yields_provider_error():
    # LOW-6: a malformed PR-view response is classified provider_error (never a
    # silent PR_UNKNOWN + empty-check read).
    calls = []
    adapter = _adapter({
        "pr view": {"stdout": ""},
    }, calls)
    read = adapter.read_ci_state("MokSeinNacken/argent-development-team", 1,
                                 "a" * 40)
    assert read.provider_error == PROVIDER_ERROR_UNKNOWN


def test_contradictory_check_normalizes_then_is_rejected_by_validation():
    # LOW-6: a check-run with status in_progress AND conclusion success
    # normalizes to a contradictory CiCheck (IN_PROGRESS + SUCCESS), which the
    # manager's _validate_read then rejects (fail-closed).
    runs = [{"id": 7, "name": "ci", "status": "in_progress",
             "conclusion": "success"}]
    checks, _ = normalize_check_runs(runs)
    assert checks[0].status == "IN_PROGRESS"
    assert checks[0].conclusion == "SUCCESS"
