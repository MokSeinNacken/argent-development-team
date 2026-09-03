"""Phase I3-B — broker-driven live GitHub flow with the REAL adapter.

Deterministic, no network, no real GitHub writes: the real
``GitHubProviderAdapter`` (live-write enabled) runs against scripted fake
``gh``/``git`` executables.  Covers the broker path (CASE 13), network-outage
safety (CASE 25), stale-holder fencing (CASE 26), restart/reconcile idempotency
(CASE 17/27/28), and single-logical-action guarantees (CASE 29/30).
"""

from __future__ import annotations

import json

import pytest

from argent_core.external_action_broker import ExternalActionBroker, RequestState
from argent_core.github_provider_adapter import (
    GITHUB_ACCEPTANCE_ACCOUNT,
    GITHUB_ACCEPTANCE_CANONICAL_URL,
    GITHUB_ACCEPTANCE_REPOSITORY,
    GitHubProviderAdapter,
    github_acceptance_allowlist,
    github_acceptance_standing_policy,
)

from i3a_helpers import TEST_MAC_KEY, make_env, make_holder
from i3b_helpers import (
    env_for,
    init_repo,
    make_gh_integrated_source,
    make_gh_provenance,
    read_log,
    write_fake_executable,
    write_scenario,
)

_TRUSTED = {GITHUB_ACCEPTANCE_REPOSITORY: GITHUB_ACCEPTANCE_CANONICAL_URL}


def _build(tmp_path, *, scenario=None, adapter=None, adapter_kwargs=None):
    core, project, sup = make_env(str(tmp_path / "a.db"))
    repo = init_repo(str(tmp_path / "r"))
    jid, cid, head, tid = make_gh_integrated_source(core, project, sup, repo)
    prov = make_gh_provenance(jid, cid, head)
    if adapter is None:
        gh = write_fake_executable(tmp_path, "gh")
        git = write_fake_executable(tmp_path, "git")
        kw = dict(adapter_kwargs or {})
        adapter = GitHubProviderAdapter(
            live_write=kw.pop("live_write", True),
            gh_executable=kw.pop("gh_executable", gh),
            git_executable=kw.pop("git_executable", git),
            trusted_repo_urls=kw.pop("trusted_repo_urls", _TRUSTED),
            env=env_for(tmp_path, scenario),
            **kw,
        )
    b = ExternalActionBroker(
        core._store, adapter=adapter,
        allowlist=github_acceptance_allowlist(),
        standing_policy=github_acceptance_standing_policy(),
        mac_key=TEST_MAC_KEY,
    )
    return core, project, sup, repo, jid, cid, head, tid, prov, b


def _push_request(b, prov, *, tid, head, branch=None, idem="ik"):
    branch = branch if branch is not None else f"argent/{tid}-feature"
    return b.create_request(
        provider="github", account=GITHUB_ACCEPTANCE_ACCOUNT,
        action="push_feature_branch", repository=GITHUB_ACCEPTANCE_REPOSITORY,
        resource_ref=branch, requested_scope="write",
        parameters={"branch": branch, "sha": head},
        idempotency_key=idem, provenance=prov)


def _pr_request(b, prov, *, branch, head, idem="ik-pr"):
    return b.create_request(
        provider="github", account=GITHUB_ACCEPTANCE_ACCOUNT,
        action="create_pull_request", repository=GITHUB_ACCEPTANCE_REPOSITORY,
        resource_ref=branch, requested_scope="write",
        parameters={"head_branch": branch, "base_branch": "main",
                    "head_sha": head, "title": "Add feature", "body": ""},
        idempotency_key=idem, provenance=prov)


# ---------------------------------------------------------------------------
# CASE 13 — push occurs only via the broker path
# ---------------------------------------------------------------------------

def test_case13_push_via_broker_only(tmp_path):
    core, project, sup, repo, jid, cid, head, tid, prov, b = _build(
        tmp_path, scenario={"push": {"code": 0, "stdout": "", "stderr": ""}})
    req = _push_request(b, prov, tid=tid, head=head)
    req = b.authorize_autonomous(req["request_id"])
    assert req["state"] == RequestState.AUTHORIZED.value
    hid, hep = make_holder(core, project, sup)
    req = b.execute(req["request_id"], holder_job_id=hid, holder_lease_epoch=hep)
    assert req["state"] == RequestState.SUCCEEDED.value
    assert req["provider_object_id"] == head
    # The fake git recorded EXACTLY ONE push, to the trusted URL + validated
    # refspec, never a force flag.
    pushes = [a for a in read_log(tmp_path) if len(a) >= 1 and a[0] == "push"]
    assert len(pushes) == 1
    argv = pushes[0]
    assert argv[1] == GITHUB_ACCEPTANCE_CANONICAL_URL
    assert argv[2] == (f"refs/heads/argent/{tid}-feature:"
                       f"refs/heads/argent/{tid}-feature")
    assert "--force" not in argv and "-f" not in argv


# ---------------------------------------------------------------------------
# CASE 25 — network/provider outage preserves safe action state
# ---------------------------------------------------------------------------

def test_case25_broker_network_outage_preserves_safe_state(tmp_path):
    # A transport failure (nonexistent git) must NOT crash or claim success:
    # the request lands in a retryable WAITING_EXTERNAL state.
    adapter = GitHubProviderAdapter(
        live_write=True, git_executable="/nonexistent/git",
        trusted_repo_urls=_TRUSTED)
    core, project, sup, repo, jid, cid, head, tid, prov, b = _build(
        tmp_path, adapter=adapter)
    req = _push_request(b, prov, tid=tid, head=head)
    req = b.authorize_autonomous(req["request_id"])
    hid, hep = make_holder(core, project, sup)
    req = b.execute(req["request_id"], holder_job_id=hid, holder_lease_epoch=hep)
    assert req["state"] == RequestState.WAITING_EXTERNAL.value
    assert req["last_failure_class"] == "PROVIDER_UNAVAILABLE"
    assert req["provider_object_id"] is None  # nothing was written


# ---------------------------------------------------------------------------
# CASE 26 — stale broker holder cannot finalize a live action
# ---------------------------------------------------------------------------

def test_case26_stale_holder_cannot_finalize(tmp_path):
    from argent_core.models import LeaseFencedError
    core, project, sup, repo, jid, cid, head, tid, prov, b = _build(
        tmp_path, scenario={"push": {"code": 0, "stdout": "", "stderr": ""}})
    req = _push_request(b, prov, tid=tid, head=head)
    req = b.authorize_autonomous(req["request_id"])
    hid, hep = make_holder(core, project, sup)
    lock = b._lock_name(req)
    assert core._store.try_acquire_action_lock(lock, job_id=hid, lease_epoch=hep)
    # A DIFFERENT (stale) holder can never drive the authoritative transition.
    other, oep = make_holder(core, project, sup)
    with pytest.raises(LeaseFencedError):
        core._store.transition_external_action_request_authoritative(
            req["request_id"], lock_name=lock, holder_job_id=other,
            holder_lease_epoch=oep, from_state="AUTHORIZED",
            to_state="EXECUTING", expected_revision=req["revision"])
    # No dispatch happened (the mutation was never driven).
    assert read_log(tmp_path) == []


# ---------------------------------------------------------------------------
# CASE 17 / 28 — duplicate logical PR reconciles existing PR (no duplicate)
# ---------------------------------------------------------------------------

def test_case17_duplicate_pr_reconciles_existing(tmp_path):
    core, project, sup, repo, jid, cid, head, tid, prov, b = _build(
        tmp_path)
    branch = f"argent/{tid}-feature"
    write_scenario(tmp_path, {"pr list": {
        "code": 0,
        "stdout": json.dumps([{"number": 1, "headRefName": branch,
                               "baseRefName": "main", "headRefOid": head,
                               "author": {"login": GITHUB_ACCEPTANCE_ACCOUNT}}]),
        "stderr": ""}})
    req = _pr_request(b, prov, branch=branch, head=head)
    req = b.authorize_autonomous(req["request_id"])
    core._store.transition_external_action_request(
        req["request_id"], from_state="AUTHORIZED", to_state="EXECUTING",
        expected_revision=req["revision"])
    hid, hep = make_holder(core, project, sup)
    req = b.reconcile(req["request_id"], holder_job_id=hid,
                      holder_lease_epoch=hep)
    assert req["state"] == RequestState.SUCCEEDED.value
    assert req["provider_object_id"] == "1"
    for a in read_log(tmp_path):
        assert "pr create" not in " ".join(a)


def test_case28_restart_reconcile_no_duplicate_pr(tmp_path):
    core, project, sup, repo, jid, cid, head, tid, prov, b = _build(
        tmp_path)
    branch = f"argent/{tid}-feature"
    write_scenario(tmp_path, {"pr list": {
        "code": 0,
        "stdout": json.dumps([{"number": 7, "headRefName": branch,
                               "baseRefName": "main", "headRefOid": head,
                               "author": {"login": GITHUB_ACCEPTANCE_ACCOUNT}}]),
        "stderr": ""}})
    req = _pr_request(b, prov, branch=branch, head=head)
    req = b.authorize_autonomous(req["request_id"])
    core._store.transition_external_action_request(
        req["request_id"], from_state="AUTHORIZED", to_state="EXECUTING",
        expected_revision=req["revision"])
    hid, hep = make_holder(core, project, sup)
    req = b.reconcile(req["request_id"], holder_job_id=hid,
                      holder_lease_epoch=hep)
    assert req["state"] == RequestState.SUCCEEDED.value
    assert req["provider_object_id"] == "7"
    for a in read_log(tmp_path):
        assert "pr create" not in " ".join(a)


# ---------------------------------------------------------------------------
# CASE 27 — restart/reconcile does not duplicate push
# ---------------------------------------------------------------------------

def test_case27_restart_reconcile_no_duplicate_push(tmp_path):
    core, project, sup, repo, jid, cid, head, tid, prov, b = _build(
        tmp_path)
    branch = f"argent/{tid}-feature"
    write_scenario(tmp_path, {
        "ls-remote": {"code": 0,
                      "stdout": f"{head}\trefs/heads/{branch}\n", "stderr": ""},
    })
    req = _push_request(b, prov, tid=tid, head=head, branch=branch)
    req = b.authorize_autonomous(req["request_id"])
    core._store.transition_external_action_request(
        req["request_id"], from_state="AUTHORIZED", to_state="EXECUTING",
        expected_revision=req["revision"])
    hid, hep = make_holder(core, project, sup)
    req = b.reconcile(req["request_id"], holder_job_id=hid,
                      holder_lease_epoch=hep)
    assert req["state"] == RequestState.SUCCEEDED.value
    assert req["provider_object_id"] == head
    # Only ls-remote ran — never a duplicate push.
    for a in read_log(tmp_path):
        assert a[0] != "push"


# ---------------------------------------------------------------------------
# CASE 29 / 30 — exactly one logical action recorded
# ---------------------------------------------------------------------------

def test_case29_exactly_one_logical_push_action_recorded(tmp_path):
    core, project, sup, repo, jid, cid, head, tid, prov, b = _build(
        tmp_path, scenario={"push": {"code": 0, "stdout": "", "stderr": ""}})
    req = _push_request(b, prov, tid=tid, head=head, idem="ik")
    again = _push_request(b, prov, tid=tid, head=head, idem="ik")
    assert again["request_id"] == req["request_id"]
    rows = core._store.list_external_action_requests()
    assert len(rows) == 1
    audit = core._store.list_external_action_audit(req["request_id"])
    requested = [e for e in audit if e["event_type"] == "REQUESTED"]
    assert len(requested) == 1


def test_case30_exactly_one_logical_pr_action_recorded(tmp_path):
    core, project, sup, repo, jid, cid, head, tid, prov, b = _build(
        tmp_path, scenario={"pr create": {"code": 0, "stdout": '{"number": 3}',
                                          "stderr": ""}})
    branch = f"argent/{tid}-feature"
    r1 = _pr_request(b, prov, branch=branch, head=head, idem="ik-pr")
    r2 = _pr_request(b, prov, branch=branch, head=head, idem="ik-pr")
    assert r2["request_id"] == r1["request_id"]
    rows = core._store.list_external_action_requests()
    assert len(rows) == 1
    audit = core._store.list_external_action_audit(r1["request_id"])
    requested = [e for e in audit if e["event_type"] == "REQUESTED"]
    assert len(requested) == 1
