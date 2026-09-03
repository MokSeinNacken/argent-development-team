"""Phase I3-A — deterministic closing-fix regression tests (Sol HIGH review).

Each test maps 1:1 to a finding from the independent Sol HIGH closing review
(7 HIGH + 3 LOW) and proves the FIXED behavior, not the old flawed behavior.

* HIGH-1 owner approval (store-backed, single-use, expired, account/action bound)
* HIGH-2 candidate provenance binding (source_job_id, integrated HEAD, keyed MAC)
* HIGH-3 standing policy + allowlist escalation (empty policy, namespaces, DENY)
* HIGH-4 reconciliation fencing + terminal immutability + PR under-bound
* HIGH-5 expiry / retry / redrive
* HIGH-6 idempotency-key equivalence + orphan audit
* HIGH-7 provider-detail redaction
* LOW-8 provider exception taxonomy
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from argent_core.external_action_broker import (
    MAX_RETRY_ATTEMPTS,
    AllowlistEntry,
    AuthorizationError,
    ExternalActionAllowlist,
    ExternalActionBroker,
    IdempotencyConflictError,
    IllegalRequestTransition,
    PolicyDecision,
    ProvenanceError,
    StandingPolicy,
    TERMINAL_REQUEST_STATES,
    compute_provenance_mac,
)
from argent_core.external_provider_adapter import (
    OUTCOME_CONFLICT,
    OUTCOME_WAITING,
    FakeGitHubAdapter,
    ProviderConflict,
    ProviderCredentialError,
    ProviderRateLimited,
    ProviderResult,
    ProviderValidationError,
)
from argent_core.models import (
    ApprovalStatus,
    LeaseFencedError,
    NotFound,
    OwnerApproval,
    SourceClass,
)

from i3a_helpers import (
    TEST_MAC_KEY,
    default_allowlist,
    default_standing_policy,
    init_repo,
    make_broker,
    make_env,
    make_holder,
    make_integrated_source,
    make_provenance,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _approve_in_store(store, *, ap_id, task_id, action, scope,
                      expires_at="2999-01-01T00:00:00+00:00",
                      status=ApprovalStatus.PENDING):
    from argent_core.gates import binding_hash
    ap = OwnerApproval(
        id=ap_id, task_id=task_id, action=action, scope=scope,
        status=status, requested_by="owner",
        source_class=SourceClass.TRUSTED, created_at="t", decided_at=None,
        consumed_at=None, expires_at=expires_at,
        binding_hash=binding_hash(task_id, action, scope),
    )
    store._insert_approval(ap)
    if status is ApprovalStatus.PENDING:
        store._mark_approved(ap_id, store.now_iso())
    return ap_id


def _env(tmp_path):
    core, project, sup = make_env(str(tmp_path / "a.db"))
    repo = init_repo(str(tmp_path / "r"))
    jid, cid, head, tid = make_integrated_source(core, project, sup, repo)
    return core, project, sup, repo, jid, cid, head, tid


def _broker(core, repo, *, adapter=None, allowlist=None, standing_policy=None,
            clock=None):
    return ExternalActionBroker(
        core._store,
        adapter=adapter if adapter is not None else FakeGitHubAdapter("github"),
        allowlist=allowlist if allowlist is not None else default_allowlist(repo),
        standing_policy=(standing_policy if standing_policy is not None
                         else default_standing_policy()),
        clock=clock, mac_key=TEST_MAC_KEY,
    )


def _make(b, repo, *, jid, cid, head, action, params=None, account="MokSeinNacken",
          idempotency_key="ik", expiry_ttl_seconds=3600, requested_scope=None):
    prov = make_provenance(jid, cid, repo, head)
    if requested_scope is None:
        requested_scope = "read" if action == "read_ref" else "write"
    return b.create_request(
        provider="github", account=account, action=action, repository=repo,
        resource_ref="main", requested_scope=requested_scope,
        parameters=params if params is not None else {"ref": "main"},
        idempotency_key=idempotency_key, provenance=prov,
        expiry_ttl_seconds=expiry_ttl_seconds,
    )


# ---------------------------------------------------------------------------
# HIGH-1 — owner approval (store-backed, single-use, expiry, binding)
# ---------------------------------------------------------------------------

def test_high1_forged_approval_rejected(tmp_path):
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    b = _broker(core, repo)
    req = _make(b, repo, jid=jid, cid=cid, head=head, action="read_ref")
    with pytest.raises(AuthorizationError):
        b.authorize_owner(req["request_id"], approval_id="does-not-exist")


def test_high1_expired_approval_rejected(tmp_path):
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    b = _broker(core, repo)
    req = _make(b, repo, jid=jid, cid=cid, head=head, action="read_ref")
    scope = b._approval_scope(req)
    _approve_in_store(
        core._store, ap_id="ap-exp", task_id=tid, action="read_ref", scope=scope,
        expires_at="2000-01-01T00:00:00+00:00", status=ApprovalStatus.APPROVED,
    )
    with pytest.raises(AuthorizationError):
        b.authorize_owner(req["request_id"], approval_id="ap-exp")


def test_high1_single_use_approval(tmp_path):
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    b = _broker(core, repo)
    req = _make(b, repo, jid=jid, cid=cid, head=head, action="read_ref")
    scope = b._approval_scope(req)
    _approve_in_store(core._store, ap_id="ap-1", task_id=tid,
                      action="read_ref", scope=scope)
    req2 = _make(b, repo, jid=jid, cid=cid, head=head, action="read_ref",
                 idempotency_key="ik-2")
    r1 = b.authorize_owner(req["request_id"], approval_id="ap-1")
    assert r1["state"] == "AUTHORIZED"
    assert core._store.get_approval("ap-1").status is ApprovalStatus.CONSUMED
    with pytest.raises(AuthorizationError):
        b.authorize_owner(req2["request_id"], approval_id="ap-1")


def test_high1_account_binding(tmp_path):
    # Approval minted for account B cannot authorize account A's request.
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    b = _broker(core, repo)
    req_a = _make(b, repo, jid=jid, cid=cid, head=head, action="read_ref",
                  account="MokSeinNacken")
    allowlist = ExternalActionAllowlist(entries=tuple(
        default_allowlist(repo).entries) + (AllowlistEntry(
            provider="github", account="OTHER",
            repositories=frozenset({repo}),
            permitted_actions=frozenset({"read_ref"}),
        ),))
    b2 = _broker(core, repo, allowlist=allowlist)
    req_b = _make(b2, repo, jid=jid, cid=cid, head=head, action="read_ref",
                  account="OTHER", idempotency_key="ik-b")
    scope_b = b2._approval_scope(req_b)
    _approve_in_store(core._store, ap_id="ap-b", task_id=tid,
                      action="read_ref", scope=scope_b)
    with pytest.raises(AuthorizationError):
        b2.authorize_owner(req_a["request_id"], approval_id="ap-b")


def test_high1_action_binding(tmp_path):
    # An approval for `push_feature_branch` cannot authorize a read request.
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    b = _broker(core, repo)
    req = _make(b, repo, jid=jid, cid=cid, head=head, action="read_ref")
    _approve_in_store(core._store, ap_id="ap-push", task_id=tid,
                      action="push_feature_branch",
                      scope=b._approval_scope(req))
    with pytest.raises(AuthorizationError):
        b.authorize_owner(req["request_id"], approval_id="ap-push")


# ---------------------------------------------------------------------------
# HIGH-2 — candidate provenance binding
# ---------------------------------------------------------------------------

def test_high2_candidate_of_job_a_with_job_b_provenance_rejected(tmp_path):
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    b = _broker(core, repo)
    jid_b, cid_b, head_b, tid_b = make_integrated_source(core, project, sup, repo)
    prov = dict(
        version=1, source_job_id=jid_b,
        source_candidate_id=cid,   # candidate A
        repository=repo, source_head=head, integrated_head=head,
        branch="main", scope="push",
    )
    prov["provenance_hash"] = compute_provenance_mac(prov, TEST_MAC_KEY)
    with pytest.raises(ProvenanceError):
        b.create_request(
            provider="github", account="MokSeinNacken", action="read_ref",
            repository=repo, resource_ref="main", requested_scope="read",
            parameters={"ref": "main"}, idempotency_key="ik-x",
            provenance=prov)


def test_high2_unrelated_push_sha_rejected(tmp_path):
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    b = _broker(core, repo)
    with pytest.raises(ValueError):
        _make(b, repo, jid=jid, cid=cid, head=head,
              action="push_feature_branch",
              params={"branch": f"argent/{tid}-x", "sha": "0" * 40},
              idempotency_key="ik-sha")


def test_high2_pr_head_not_tied_to_integrated_result_rejected(tmp_path):
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    b = _broker(core, repo)
    with pytest.raises(ValueError):
        _make(b, repo, jid=jid, cid=cid, head=head,
              action="create_pull_request",
              params={"head_branch": f"argent/{tid}-x", "base_branch": "main",
                      "head_sha": "1" * 40, "title": "t"},
              idempotency_key="ik-pr")


# ---------------------------------------------------------------------------
# HIGH-3 — standing policy + allowlist escalation
# ---------------------------------------------------------------------------

def test_high3_empty_standing_policy_blocks_autonomous_write(tmp_path):
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    b = _broker(core, repo, standing_policy=StandingPolicy())
    req = _make(b, repo, jid=jid, cid=cid, head=head,
                action="push_feature_branch",
                params={"branch": f"argent/{tid}-x", "sha": head})
    dec, reason = b.evaluate_policy(req)
    assert dec is PolicyDecision.OWNER_GATE_REQUIRED
    assert reason == "NOT_AUTONOMOUS"
    assert b.authorize_autonomous(req["request_id"])["state"] == "DENIED"


def test_high3_branch_namespace_enforced(tmp_path):
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    allowlist = ExternalActionAllowlist(entries=(AllowlistEntry(
        provider="github", account="MokSeinNacken",
        repositories=frozenset({repo}),
        permitted_actions=frozenset({"push_feature_branch"}),
        branch_namespaces=frozenset({"feature/"}),
    ),))
    b = _broker(core, repo, allowlist=allowlist)
    req = _make(b, repo, jid=jid, cid=cid, head=head,
                action="push_feature_branch",
                params={"branch": f"argent/{tid}-x", "sha": head})
    dec, reason = b.evaluate_policy(req)
    assert dec is PolicyDecision.OWNER_GATE_REQUIRED
    assert reason == "BRANCH_NOT_IN_NAMESPACE"


def test_high3_deny_account_cannot_be_owner_authorized(tmp_path):
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    b = _broker(core, repo)
    req = _make(b, repo, jid=jid, cid=cid, head=head, action="read_ref",
                account="UNKNOWN_ACCOUNT")
    _approve_in_store(core._store, ap_id="ap-x", task_id=tid,
                      action="read_ref", scope=b._approval_scope(req))
    with pytest.raises(AuthorizationError):
        b.authorize_owner(req["request_id"], approval_id="ap-x")


def test_high3_revoked_policy_blocks_execution(tmp_path):
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    b = _broker(core, repo)
    req = _make(b, repo, jid=jid, cid=cid, head=head,
                action="push_feature_branch",
                params={"branch": f"argent/{tid}-x", "sha": head})
    assert b.authorize_autonomous(req["request_id"])["state"] == "AUTHORIZED"
    b._allowlist = ExternalActionAllowlist()  # simulate policy revocation
    hid, hep = make_holder(core, project, sup)
    out = b.execute(req["request_id"], holder_job_id=hid, holder_lease_epoch=hep)
    assert out["state"] == "DENIED"


# ---------------------------------------------------------------------------
# HIGH-4 — reconciliation fencing + terminal immutability + PR under-bound
# ---------------------------------------------------------------------------

def test_high4_phantom_holder_cannot_reconcile_finalize(tmp_path):
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    b = _broker(core, repo)
    req = _make(b, repo, jid=jid, cid=cid, head=head,
                action="push_feature_branch",
                params={"branch": f"argent/{tid}-x", "sha": head})
    req = b.authorize_autonomous(req["request_id"])
    core._store.transition_external_action_request(
        req["request_id"], from_state="AUTHORIZED", to_state="EXECUTING",
        expected_revision=req["revision"])
    with pytest.raises(LeaseFencedError):
        b.reconcile(req["request_id"], holder_job_id="nonexistent",
                    holder_lease_epoch=1)


def test_high4_stale_holder_cannot_finalize_after_takeover(tmp_path):
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    adapter = FakeGitHubAdapter("github")
    b = _broker(core, repo, adapter=adapter)
    branch = f"argent/{tid}-x"
    req = _make(b, repo, jid=jid, cid=cid, head=head,
                action="push_feature_branch",
                params={"branch": branch, "sha": head})
    adapter.set_branch(branch, head, repo=repo)
    req = b.authorize_autonomous(req["request_id"])
    core._store.transition_external_action_request(
        req["request_id"], from_state="AUTHORIZED", to_state="EXECUTING",
        expected_revision=req["revision"])
    hid, hep = make_holder(core, project, sup)
    other, oep = make_holder(core, project, sup)
    lock = b._lock_name(req)
    assert core._store.try_acquire_action_lock(lock, job_id=other, lease_epoch=oep)
    out = b.reconcile(req["request_id"], holder_job_id=hid, holder_lease_epoch=hep)
    assert out["state"] == "EXECUTING"  # the stale holder did NOT finalize


def test_high4_terminal_cannot_be_reopened(tmp_path):
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    b = _broker(core, repo)
    req = _make(b, repo, jid=jid, cid=cid, head=head,
                action="push_feature_branch",
                params={"branch": f"argent/{tid}-x", "sha": head})
    req = b.authorize_autonomous(req["request_id"])
    hid, hep = make_holder(core, project, sup)
    out = b.execute(req["request_id"], holder_job_id=hid, holder_lease_epoch=hep)
    assert out["state"] == "SUCCEEDED"
    with pytest.raises(IllegalRequestTransition):
        core._store.transition_external_action_request(
            req["request_id"], from_state="SUCCEEDED", to_state="PENDING",
            expected_revision=out["revision"])


def test_high4_under_bound_pr_match_not_suppressed(tmp_path):
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    adapter = FakeGitHubAdapter("github")
    b = _broker(core, repo, adapter=adapter)
    branch = f"argent/{tid}-x"
    req = _make(b, repo, jid=jid, cid=cid, head=head,
                action="create_pull_request",
                params={"head_branch": branch, "base_branch": "main",
                        "head_sha": head, "title": "t", "body": ""})
    b.authorize_autonomous(req["request_id"])
    # A PR with the SAME head_branch but a DIFFERENT head_sha exists -> must
    # NOT be matched (a legitimate new PR is not suppressed).
    adapter.pull_requests[1] = {
        "number": 1, "repo": repo, "head_branch": branch,
        "base_branch": "main", "head_sha": "f" * 40, "state": "open",
        "idempotency_key": req["idempotency_key"], "argent_owned": True,
    }
    req = core._store.get_external_action_request(req["request_id"])
    core._store.transition_external_action_request(
        req["request_id"], from_state="AUTHORIZED", to_state="EXECUTING",
        expected_revision=req["revision"])
    hid, hep = make_holder(core, project, sup)
    out = b.reconcile(req["request_id"], holder_job_id=hid, holder_lease_epoch=hep)
    assert out["state"] == "EXECUTING"


# ---------------------------------------------------------------------------
# HIGH-5 — expiry / retry / redrive
# ---------------------------------------------------------------------------

def test_high5_expired_request_cannot_authorize_or_execute(tmp_path):
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    t = [datetime.now(timezone.utc)]
    b = _broker(core, repo, clock=lambda: t[0])
    # (a) an already-expired request cannot be authorized.
    req = _make(b, repo, jid=jid, cid=cid, head=head, action="read_ref",
                expiry_ttl_seconds=60)
    t[0] = t[0] + timedelta(seconds=61)
    out = b.authorize_autonomous(req["request_id"])
    assert out["state"] == "BLOCKED"
    assert out["last_error_code"] == "EXPIRED"
    # (b) a request authorized BEFORE expiry, then expired, cannot execute.
    t[0] = datetime.now(timezone.utc)
    req2 = _make(b, repo, jid=jid, cid=cid, head=head, action="read_ref",
                 expiry_ttl_seconds=60, idempotency_key="ik-exp2")
    out2 = b.authorize_autonomous(req2["request_id"])
    assert out2["state"] == "AUTHORIZED"
    t[0] = t[0] + timedelta(seconds=61)
    hid, hep = make_holder(core, project, sup)
    out3 = b.execute(req2["request_id"], holder_job_id=hid, holder_lease_epoch=hep)
    assert out3["state"] == "BLOCKED"
    assert out3["last_error_code"] == "EXPIRED"


def test_high5_waiting_redrives_within_budget_then_terminal_fails(tmp_path):
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    adapter = FakeGitHubAdapter("github")
    b = _broker(core, repo, adapter=adapter)
    req = _make(b, repo, jid=jid, cid=cid, head=head,
                action="push_feature_branch",
                params={"branch": f"argent/{tid}-x", "sha": head})
    adapter.script("push_feature_branch", [OUTCOME_WAITING] * 100)
    b.authorize_autonomous(req["request_id"])
    hid, hep = make_holder(core, project, sup)
    out = b.execute(req["request_id"], holder_job_id=hid, holder_lease_epoch=hep)
    assert out["state"] == "WAITING_EXTERNAL"
    assert out["attempt_count"] == 1
    for _ in range(MAX_RETRY_ATTEMPTS + 2):
        out = b.redrive_waiting(req["request_id"], holder_job_id=hid,
                                holder_lease_epoch=hep)
        if out["state"] in TERMINAL_REQUEST_STATES:
            break
    assert out["state"] == "FAILED"
    assert out["attempt_count"] <= MAX_RETRY_ATTEMPTS


# ---------------------------------------------------------------------------
# HIGH-6 — idempotency equivalence + orphan audit
# ---------------------------------------------------------------------------

def test_high6_same_key_different_action_conflicts(tmp_path):
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    b = _broker(core, repo)
    _make(b, repo, jid=jid, cid=cid, head=head, action="read_ref",
          idempotency_key="ik")
    with pytest.raises(IdempotencyConflictError):
        _make(b, repo, jid=jid, cid=cid, head=head, action="read_repository",
              params={}, idempotency_key="ik")


def test_high6_same_key_equivalent_single_audit(tmp_path):
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    b = _broker(core, repo)
    r1 = _make(b, repo, jid=jid, cid=cid, head=head, action="read_ref",
               idempotency_key="ik")
    r2 = _make(b, repo, jid=jid, cid=cid, head=head, action="read_ref",
               idempotency_key="ik")
    assert r1["request_id"] == r2["request_id"]
    audit = core._store.list_external_action_audit(r1["request_id"])
    requested = [a for a in audit if a["event_type"] == "REQUESTED"]
    assert len(requested) == 1


def test_high6_no_orphan_audit(tmp_path):
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    with pytest.raises(NotFound):
        core._store.append_external_action_audit(
            "xr_" + "0" * 24, "EXECUTED", failure_class=None,
            reason_code=None, detail=None)


# ---------------------------------------------------------------------------
# HIGH-7 — provider-detail redaction
# ---------------------------------------------------------------------------

class _LeakyAdapter(FakeGitHubAdapter):
    def push_feature_branch(self, request):
        return ProviderResult(
            OUTCOME_CONFLICT,
            detail="auth failed ghp_abcdefghijklmnopqrstuvwxyz1234567890 token",
        )


def test_high7_secret_marker_never_persisted(tmp_path):
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    b = _broker(core, repo, adapter=_LeakyAdapter("github"))
    req = _make(b, repo, jid=jid, cid=cid, head=head,
                action="push_feature_branch",
                params={"branch": f"argent/{tid}-x", "sha": head})
    b.authorize_autonomous(req["request_id"])
    hid, hep = make_holder(core, project, sup)
    out = b.execute(req["request_id"], holder_job_id=hid, holder_lease_epoch=hep)
    assert out["state"] == "FAILED"
    assert "ghp_" not in (out["last_error_code"] or "")
    audit = core._store.list_external_action_audit(req["request_id"])
    for a in audit:
        for v in a.values():
            assert "ghp_" not in str(v)


# ---------------------------------------------------------------------------
# LOW-8 — provider exception taxonomy
# ---------------------------------------------------------------------------

class _RaisingAdapter(FakeGitHubAdapter):
    def __init__(self, exc):
        super().__init__(provider_name="github")
        self._exc = exc

    def push_feature_branch(self, request):
        raise self._exc


@pytest.mark.parametrize("exc,cls,state", [
    (ProviderConflict(), "CONFLICT", "FAILED"),
    (ProviderRateLimited(), "RATE_LIMIT", "WAITING_EXTERNAL"),
    (ProviderCredentialError(), "CREDENTIAL", "FAILED"),
    (ProviderValidationError(), "REMOTE_VALIDATION", "FAILED"),
])
def test_low8_raised_exception_maps_to_class(tmp_path, exc, cls, state):
    core, project, sup, repo, jid, cid, head, tid = _env(tmp_path)
    b = _broker(core, repo, adapter=_RaisingAdapter(exc))
    req = _make(b, repo, jid=jid, cid=cid, head=head,
                action="push_feature_branch",
                params={"branch": f"argent/{tid}-x", "sha": head})
    b.authorize_autonomous(req["request_id"])
    hid, hep = make_holder(core, project, sup)
    out = b.execute(req["request_id"], holder_job_id=hid, holder_lease_epoch=hep)
    assert out["state"] == state
    assert out["last_failure_class"] == cls
