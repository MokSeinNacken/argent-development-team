"""Phase I3-B — task-scoped acceptance allowlist + policy (CASE 7/8/9/10/20)
and broker input validation (CASE 11/12).

Deterministic, no network, no real writes.  Uses the real broker with the
acceptance allowlist/standing-policy builders and a no-write GitHub adapter for
policy evaluation (no dispatch).
"""

from __future__ import annotations

import pytest

from argent_core.external_action_broker import (
    ExternalActionBroker,
    PolicyDecision,
    ProvenanceError,
)
from argent_core.external_provider_adapter import FakeGitHubAdapter
from argent_core.github_provider_adapter import (
    GITHUB_ACCEPTANCE_ACCOUNT,
    GITHUB_ACCEPTANCE_PROVIDER,
    GITHUB_ACCEPTANCE_REPOSITORY,
    GitHubProviderAdapter,
    github_acceptance_allowlist,
    github_acceptance_standing_policy,
)

from i3a_helpers import TEST_MAC_KEY, make_env, make_holder
from i3b_helpers import (
    make_gh_integrated_source,
    make_gh_provenance,
    init_repo,
)


def _broker(core, sup, repo, *, allowlist=None, standing_policy=None,
            adapter=None):
    adapter = adapter if adapter is not None else FakeGitHubAdapter(
        provider_name="github")
    return ExternalActionBroker(
        core._store, adapter=adapter,
        allowlist=allowlist if allowlist is not None
        else github_acceptance_allowlist(),
        standing_policy=standing_policy if standing_policy is not None
        else github_acceptance_standing_policy(),
        mac_key=TEST_MAC_KEY,
    )


def _env(tmp_path, *, repository=GITHUB_ACCEPTANCE_REPOSITORY):
    core, project, sup = make_env(str(tmp_path / "a.db"))
    repo = init_repo(str(tmp_path / "r"))
    jid, cid, head, tid = make_gh_integrated_source(
        core, project, sup, repo, repository=repository)
    prov = make_gh_provenance(jid, cid, head, repository=repository)
    b = _broker(core, sup, repo)
    return core, project, sup, repo, jid, cid, head, tid, prov, b


# ---------------------------------------------------------------------------
# CASE 7 / 8 — allowlist permits EXACT repo only
# ---------------------------------------------------------------------------

def test_case7_allowlist_permits_exact_repo():
    al = github_acceptance_allowlist()
    assert al.permits(GITHUB_ACCEPTANCE_PROVIDER, GITHUB_ACCEPTANCE_ACCOUNT,
                      GITHUB_ACCEPTANCE_REPOSITORY, "push_feature_branch")
    assert al.permits(GITHUB_ACCEPTANCE_PROVIDER, GITHUB_ACCEPTANCE_ACCOUNT,
                      GITHUB_ACCEPTANCE_REPOSITORY, "read_repository")
    assert al.permits(GITHUB_ACCEPTANCE_PROVIDER, GITHUB_ACCEPTANCE_ACCOUNT,
                      GITHUB_ACCEPTANCE_REPOSITORY, "create_pull_request")


def test_case8_allowlist_denies_different_repo_or_account():
    al = github_acceptance_allowlist()
    assert not al.permits(GITHUB_ACCEPTANCE_PROVIDER, GITHUB_ACCEPTANCE_ACCOUNT,
                          "someone-else/other", "push_feature_branch")
    assert not al.permits(GITHUB_ACCEPTANCE_PROVIDER, "other-account",
                          GITHUB_ACCEPTANCE_REPOSITORY, "push_feature_branch")
    # SENSITIVE merge is PERMITTED (owner-gateable), never autonomous.
    assert al.permits(GITHUB_ACCEPTANCE_PROVIDER, GITHUB_ACCEPTANCE_ACCOUNT,
                      GITHUB_ACCEPTANCE_REPOSITORY, "merge_pull_request")
    # unknown action denied.
    assert not al.permits(GITHUB_ACCEPTANCE_PROVIDER, GITHUB_ACCEPTANCE_ACCOUNT,
                          GITHUB_ACCEPTANCE_REPOSITORY, "update_pull_request")


# ---------------------------------------------------------------------------
# CASE 9 / 10 — branch namespace + protected ref (broker policy)
# ---------------------------------------------------------------------------

def test_case9_branch_namespace_enforced(tmp_path):
    core, project, sup, repo, jid, cid, head, tid, prov, b = _env(tmp_path)
    good = f"argent/{tid}-feature"
    bad = f"random/{tid}-feature"
    for branch in (good, bad):
        req = b.create_request(
            provider="github", account=GITHUB_ACCEPTANCE_ACCOUNT,
            action="push_feature_branch", repository=GITHUB_ACCEPTANCE_REPOSITORY,
            resource_ref=branch, requested_scope="write",
            parameters={"branch": branch, "sha": head},
            idempotency_key=f"ik-{branch}", provenance=prov)
        dec, reason = b.evaluate_policy(req)
        if branch == good:
            assert dec is PolicyDecision.ALLOW_AUTONOMOUS, reason
        else:
            assert dec is PolicyDecision.OWNER_GATE_REQUIRED
            assert reason == "BRANCH_NOT_IN_NAMESPACE"


def test_case10_protected_branch_rejected(tmp_path):
    from argent_core.external_action_broker import is_protected_ref
    assert is_protected_ref("main")
    assert is_protected_ref("master")
    assert is_protected_ref("release/1.0")
    assert is_protected_ref("production")
    core, project, sup, repo, jid, cid, head, tid, prov, b = _env(tmp_path)
    req = b.create_request(
        provider="github", account=GITHUB_ACCEPTANCE_ACCOUNT,
        action="push_feature_branch", repository=GITHUB_ACCEPTANCE_REPOSITORY,
        resource_ref="main", requested_scope="write",
        parameters={"branch": "main", "sha": head},
        idempotency_key="ik-main", provenance=prov)
    dec, reason = b.evaluate_policy(req)
    assert dec is PolicyDecision.OWNER_GATE_REQUIRED
    assert reason == "PROTECTED_REF"


# ---------------------------------------------------------------------------
# CASE 11 / 12 — pre-push HEAD mismatch + missing evidence
# ---------------------------------------------------------------------------

def test_case11_pre_push_head_mismatch_rejected(tmp_path):
    core, project, sup, repo, jid, cid, head, tid, prov, b = _env(tmp_path)
    branch = f"argent/{tid}-feature"
    with pytest.raises(ValueError):
        b.create_request(
            provider="github", account=GITHUB_ACCEPTANCE_ACCOUNT,
            action="push_feature_branch", repository=GITHUB_ACCEPTANCE_REPOSITORY,
            resource_ref=branch, requested_scope="write",
            parameters={"branch": branch, "sha": "0" * 40},  # wrong HEAD
            idempotency_key="ik", provenance=prov)


def test_case12_missing_evidence_rejects_push(tmp_path):
    core, project, sup, repo, jid, cid, head, tid, prov, b = _env(tmp_path)
    branch = f"argent/{tid}-feature"
    # Provenance missing required fields -> fail closed.
    bad_prov = dict(prov)
    bad_prov.pop("integrated_head")
    with pytest.raises(ProvenanceError):
        b.create_request(
            provider="github", account=GITHUB_ACCEPTANCE_ACCOUNT,
            action="push_feature_branch", repository=GITHUB_ACCEPTANCE_REPOSITORY,
            resource_ref=branch, requested_scope="write",
            parameters={"branch": branch, "sha": head},
            idempotency_key="ik", provenance=bad_prov)
    # A forged provenance hash (unkeyed recompute) also fails closed.
    forged = dict(prov)
    forged["provenance_hash"] = "0" * 64
    with pytest.raises(ProvenanceError):
        b.create_request(
            provider="github", account=GITHUB_ACCEPTANCE_ACCOUNT,
            action="push_feature_branch", repository=GITHUB_ACCEPTANCE_REPOSITORY,
            resource_ref=branch, requested_scope="write",
            parameters={"branch": branch, "sha": head},
            idempotency_key="ik", provenance=forged)


# ---------------------------------------------------------------------------
# CASE 20 — merge (SENSITIVE) -> OWNER_GATE_REQUIRED
# ---------------------------------------------------------------------------

def test_case20_merge_request_owner_gate_required(tmp_path):
    core, project, sup, repo, jid, cid, head, tid, prov, b = _env(tmp_path)
    req = b.create_request(
        provider="github", account=GITHUB_ACCEPTANCE_ACCOUNT,
        action="merge_pull_request", repository=GITHUB_ACCEPTANCE_REPOSITORY,
        resource_ref="1", requested_scope="merge",
        parameters={"number": 1}, idempotency_key="ik-merge", provenance=prov)
    dec, reason = b.evaluate_policy(req)
    assert dec is PolicyDecision.OWNER_GATE_REQUIRED
    assert reason == "SENSITIVE_ACTION"


def test_case20_release_and_deploy_owner_gate_required(tmp_path):
    core, project, sup, repo, jid, cid, head, tid, prov, b = _env(tmp_path)
    for action in ("create_release", "deploy_production"):
        req = b.create_request(
            provider="github", account=GITHUB_ACCEPTANCE_ACCOUNT,
            action=action, repository=GITHUB_ACCEPTANCE_REPOSITORY,
            resource_ref="x", requested_scope="sensitive",
            parameters={}, idempotency_key=f"ik-{action}", provenance=prov)
        dec, reason = b.evaluate_policy(req)
        assert dec is PolicyDecision.OWNER_GATE_REQUIRED, reason
        assert reason == "SENSITIVE_ACTION"
