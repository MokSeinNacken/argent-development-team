"""Phase I3-A — provider adapter boundary + reconciliation (deterministic,
FakeGitHubAdapter, no network, no real writes).

CASE 50 (no real write path), adapter determinism, duplicate-PR detection,
non-fast-forward conflict, and crash-after-provider-success reconciliation.
"""

from __future__ import annotations

import pytest

from argent_core.external_action_broker import (
    ExternalActionBroker,
    PolicyDecision,
    RequestState,
)
from argent_core.external_provider_adapter import (
    OUTCOME_CONFLICT,
    OUTCOME_RATE_LIMITED,
    OUTCOME_SUCCESS,
    ExternalProviderAdapter,
    FakeGitHubAdapter,
    NoWriteExternalProviderAdapter,
    ProviderWriteDisabled,
)

from i3a_helpers import (
    make_broker,
    make_env,
    make_holder,
    make_integrated_source,
    make_provenance,
    init_repo,
)


def test_adapter_abc_mutations_structurally_disabled():
    # The base protocol's mutation methods are concrete (raising); a
    # write-disabled concrete adapter inherits them (no real write path).
    adapter = NoWriteExternalProviderAdapter()
    req = _dummy_request()
    with pytest.raises(ProviderWriteDisabled):
        adapter.push_feature_branch(req)
    with pytest.raises(ProviderWriteDisabled):
        adapter.create_pull_request(req)
    with pytest.raises(ProviderWriteDisabled):
        adapter.update_pull_request(req)
    assert adapter.write_enabled is False


def test_no_write_adapter_write_disabled():
    adapter = NoWriteExternalProviderAdapter()
    assert adapter.write_enabled is False
    with pytest.raises(ProviderWriteDisabled):
        adapter.push_feature_branch(_dummy_request())


def test_fake_adapter_push_and_pr_roundtrip(tmp_path):
    adapter = FakeGitHubAdapter(provider_name="github")
    req = _dummy_request(parameters={"branch": "argent/t1-x",
                                     "sha": "a" * 40})
    r = adapter.push_feature_branch(req)
    assert r.outcome == OUTCOME_SUCCESS
    assert adapter.branches["argent/t1-x"] == "a" * 40
    # Non-fast-forward push conflicts.
    r = adapter.push_feature_branch(_dummy_request(
        parameters={"branch": "argent/t1-x", "sha": "b" * 40}))
    assert r.outcome == OUTCOME_CONFLICT


def test_fake_adapter_duplicate_pr_detection(tmp_path):
    adapter = FakeGitHubAdapter(provider_name="github")
    p = {"head_branch": "argent/t1-x", "base_branch": "main",
         "head_sha": "a" * 40, "title": "t", "body": ""}
    r1 = adapter.create_pull_request(_dummy_request(parameters=p))
    r2 = adapter.create_pull_request(_dummy_request(parameters=p))
    assert r1.outcome == OUTCOME_SUCCESS
    # The same head yields the SAME PR (no duplicate).
    assert r2.object_id == r1.object_id
    assert len(adapter.pull_requests) == 1


def test_fake_adapter_observe_reconcile_semantics(tmp_path):
    adapter = FakeGitHubAdapter(provider_name="github")
    # push: observe found iff remote ref == expected sha.
    adapter.set_branch("argent/t1-x", "a" * 40)
    obs = adapter.observe(_dummy_request(action="push_feature_branch",
        parameters={"branch": "argent/t1-x", "sha": "a" * 40}))
    assert obs.found is True
    obs = adapter.observe(_dummy_request(action="push_feature_branch",
        parameters={"branch": "argent/t1-x", "sha": "b" * 40}))
    assert obs.found is False


def test_execute_conflict_terminal_failure(tmp_path):
    core, project, sup = make_env(str(tmp_path / "a.db"))
    repo = init_repo(str(tmp_path / "r"))
    jid, cid, head, tid = make_integrated_source(core, project, sup, repo)
    prov = make_provenance(jid, cid, repo, head)
    adapter = FakeGitHubAdapter(provider_name="github")
    adapter.set_branch(f"argent/{tid}-feature", "0" * 40)  # pre-existing
    b = make_broker(core._store, repo=repo, adapter=adapter)
    branch = f"argent/{tid}-feature"
    req = b.create_request(
        provider="github", account="MokSeinNacken",
        action="push_feature_branch", repository=repo, resource_ref=branch,
        requested_scope="write", parameters={"branch": branch, "sha": head},
        idempotency_key="ik", provenance=prov)
    req = b.authorize_autonomous(req["request_id"])
    hid, hep = make_holder(core, project, sup)
    req = b.execute(req["request_id"], holder_job_id=hid, holder_lease_epoch=hep)
    assert req["state"] == "FAILED"
    assert req["last_failure_class"] == "CONFLICT"


def test_rate_limit_enters_waiting_external(tmp_path):
    core, project, sup = make_env(str(tmp_path / "a.db"))
    repo = init_repo(str(tmp_path / "r"))
    jid, cid, head, tid = make_integrated_source(core, project, sup, repo)
    prov = make_provenance(jid, cid, repo, head)
    adapter = FakeGitHubAdapter(provider_name="github")
    adapter.script("push_feature_branch", [OUTCOME_RATE_LIMITED])
    b = make_broker(core._store, repo=repo, adapter=adapter)
    branch = f"argent/{tid}-feature"
    req = b.create_request(
        provider="github", account="MokSeinNacken",
        action="push_feature_branch", repository=repo, resource_ref=branch,
        requested_scope="write", parameters={"branch": branch, "sha": head},
        idempotency_key="ik", provenance=prov)
    req = b.authorize_autonomous(req["request_id"])
    hid, hep = make_holder(core, project, sup)
    req = b.execute(req["request_id"], holder_job_id=hid, holder_lease_epoch=hep)
    assert req["state"] == "WAITING_EXTERNAL"
    assert req["last_failure_class"] == "RATE_LIMIT"


def test_reconcile_not_found_is_noop(tmp_path):
    core, project, sup = make_env(str(tmp_path / "a.db"))
    repo = init_repo(str(tmp_path / "r"))
    jid, cid, head, tid = make_integrated_source(core, project, sup, repo)
    prov = make_provenance(jid, cid, repo, head)
    adapter = FakeGitHubAdapter(provider_name="github")
    b = make_broker(core._store, repo=repo, adapter=adapter)
    branch = f"argent/{tid}-feature"
    req = b.create_request(
        provider="github", account="MokSeinNacken",
        action="push_feature_branch", repository=repo, resource_ref=branch,
        requested_scope="write", parameters={"branch": branch, "sha": head},
        idempotency_key="ik", provenance=prov)
    req = b.authorize_autonomous(req["request_id"])
    core._store.transition_external_action_request(
        req["request_id"], from_state="AUTHORIZED", to_state="EXECUTING",
        expected_revision=req["revision"])
    hid, hep = make_holder(core, project, sup)
    # Nothing provider-side -> reconcile leaves it in-flight (no crash claim).
    req = b.reconcile(req["request_id"], holder_job_id=hid,
                      holder_lease_epoch=hep)
    assert req["state"] == "EXECUTING"


def test_stale_holder_aborts_before_dispatch(tmp_path):
    from argent_core.models import LeaseFencedError
    core, project, sup = make_env(str(tmp_path / "a.db"))
    repo = init_repo(str(tmp_path / "r"))
    jid, cid, head, tid = make_integrated_source(core, project, sup, repo)
    prov = make_provenance(jid, cid, repo, head)
    adapter = FakeGitHubAdapter(provider_name="github")
    b = make_broker(core._store, repo=repo, adapter=adapter)
    branch = f"argent/{tid}-feature"
    req = b.create_request(
        provider="github", account="MokSeinNacken",
        action="push_feature_branch", repository=repo, resource_ref=branch,
        requested_scope="write", parameters={"branch": branch, "sha": head},
        idempotency_key="ik", provenance=prov)
    req = b.authorize_autonomous(req["request_id"])
    # A phantom (non-lease-holding) caller cannot acquire the action lock ->
    # the mutation is NEVER dispatched.
    with pytest.raises(LeaseFencedError):
        b.execute(req["request_id"], holder_job_id="nonexistent",
                  holder_lease_epoch=1)
    assert not any(c["action"] == "push_feature_branch" for c in adapter.calls)
    assert adapter.branches.get(branch) is None


def _dummy_request(**kw):
    from argent_core.external_action_broker import ExternalActionRequest
    defaults = dict(
        request_id="xr_" + "0" * 24, provider="github", account="a",
        action="push_feature_branch", policy_class="BOUNDED_WRITE",
        repository="/r", resource_ref="argent/t1-x", source_job_id="j",
        source_candidate_id="c", requested_scope="write",
        parameters={}, expected_preconditions={}, idempotency_key="ik",
        provenance_version=1, provenance_hash="h" * 64,
    )
    defaults.update(kw)
    return ExternalActionRequest(**defaults)
