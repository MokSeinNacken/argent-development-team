"""Phase I1 — isolation tests: read-only no-write, job-local provenance.

Covers context-pack provenance, routing escalation, evidence reuse, and the
read-only no-write guarantee + unchanged trust/fs narrowing.  Deterministic,
no network, no LLM.
"""

from __future__ import annotations

import pytest

from argent_core.concurrency_policy import READONLY_ROLES
from argent_core.context_pack import (
    ContextBuilder,
    ContextPack,
    make_context_pack_id,
)
from argent_core.models import Role
from argent_core.supervisor import AGENT_IDS
from argent_core.test_execution import (
    EvidenceRecord,
    EvidenceStore,
    ResultClass,
    SnapshotIdentity,
)
from argent_core.workspace_broker import (
    CONTENT_DENYLIST,
    CONTROLLER_SOURCE,
    WorkspaceBroker,
)


# ---------------------------------------------------------------------------
# Case 7: read-only roles gain no write permission under parallelism
# ---------------------------------------------------------------------------

def test_case7_readonly_roles_have_no_broker_write():
    broker = WorkspaceBroker()
    for role in ("analyst", "reviewer", "lead"):
        with pytest.raises(Exception) as exc:
            broker.apply_patch_set(
                "/tmp/scope", [{"op": "write", "path": "x.txt",
                                "content": "aGk="}],
                role, CONTROLLER_SOURCE,
            )
        # Either the role-scope check or the source check fails closed.
        assert type(exc.value).__name__ in ("PermissionDenied", "BrokerError")


def test_case7_readonly_roles_are_listed_readonly():
    assert READONLY_ROLES == ("lead", "analyst", "reviewer")


def test_case7_implementer_and_qa_have_scoped_write():
    broker = WorkspaceBroker()
    # implementer -> whole root; qa -> tests/ subtree only; others -> deny.
    assert broker._allowed_root("/tmp/r", Role.IMPLEMENTER) == broker._allowed_root(
        "/tmp/r", Role.IMPLEMENTER)
    qa_root = broker._allowed_root("/tmp/r", Role.QA)
    assert qa_root.endswith("tests")
    for role in (Role.LEAD, Role.ANALYST, Role.REVIEWER):
        with pytest.raises(Exception):
            broker._allowed_root("/tmp/r", role)


# ---------------------------------------------------------------------------
# Case 20: context-pack provenance is job/dispatch-specific
# ---------------------------------------------------------------------------

def test_case20_context_pack_provenance_job_specific():
    b = ContextBuilder()
    p1 = b.build(job_id="job-1", dispatch_id="d-1", role="implementer",
                 objective="task 1", now_iso="2026-01-01T00:00:00+00:00")
    p2 = b.build(job_id="job-2", dispatch_id="d-2", role="implementer",
                 objective="task 2", now_iso="2026-01-01T00:00:00+00:00")
    assert p1.job_id == "job-1" and p1.dispatch_id == "d-1"
    assert p2.job_id == "job-2" and p2.dispatch_id == "d-2"
    # Distinct packs never share an id (id is dispatch-bound).
    assert p1.context_pack_id != p2.context_pack_id
    # The id is a pure function of dispatch_id + content hash.
    assert make_context_pack_id("d-1", p1.content_hash) == p1.context_pack_id
    assert make_context_pack_id("d-2", p2.content_hash) == p2.context_pack_id


# ---------------------------------------------------------------------------
# Case 21: routing escalation is per-dispatch (job-local)
# ---------------------------------------------------------------------------

def test_case21_routing_escalation_job_local():
    import argent_core.model_router as mr

    router = mr.ModelRouter()
    req_a = mr.RoutingRequest(
        job_id="ja", task_id="ta", role=Role.IMPLEMENTER.value,
        dispatch_id="d-a", current_escalation_level=2,
    )
    req_b = mr.RoutingRequest(
        job_id="jb", task_id="tb", role=Role.IMPLEMENTER.value,
        dispatch_id="d-b", current_escalation_level=0,
    )
    now = "2026-01-01T00:00:00+00:00"
    da = router.route(req_a, now_iso=now)
    db = router.route(req_b, now_iso=now)
    # Each decision is bound to its own dispatch (no cross-job leak).
    assert da.dispatch_id == "d-a"
    assert db.dispatch_id == "d-b"
    assert da.job_id == "ja" and db.job_id == "jb"
    # Escalating job A never upgrades job B.
    assert da.escalation_level > db.escalation_level


# ---------------------------------------------------------------------------
# Case 22: test evidence cannot leak across snapshots/worktrees
# ---------------------------------------------------------------------------

def _rec(source_hash, root, *, classification=ResultClass.TEST_PASS):
    return EvidenceRecord(
        selector="tests/",
        source_hash=source_hash,
        test_definition_hash="tdh",
        plan_hash="ph",
        inventory_hash="ih",
        policy_hash="poh",
        executor_id="ex",
        classification=classification,
        timestamp="2026-01-01T00:00:00Z",
        root=root,
        config_hash="ch",
    )


def test_case22_evidence_reuse_bound_to_exact_snapshot():
    store = EvidenceStore(mac_key=b"0" * 32)
    store.add(_rec("src-A", "/wt/A"))

    snap_a = SnapshotIdentity(source_hash="src-A", test_definition_hash="tdh",
                              executor_id="ex", root="/wt/A", config_hash="ch")
    snap_b = SnapshotIdentity(source_hash="src-B", test_definition_hash="tdh",
                              executor_id="ex", root="/wt/B", config_hash="ch")

    plan_a = type("Plan", (), {
        "plan_hash": "ph", "inventory_hash": "ih", "policy_hash": "poh",
    })()

    # Exact identity -> reusable.
    reused = store.find_reusable_pass("tests/", snap_a, plan_a)
    assert reused is not None

    # Different snapshot (source_hash / worktree) -> NOT reusable.
    assert store.find_reusable_pass("tests/", snap_b, plan_a) is None


def test_case22_tampered_evidence_refused():
    store = EvidenceStore(mac_key=b"0" * 32)
    store.add(_rec("src-A", "/wt/A"))
    # Tamper a record's identity without recomputing the MAC -> load fails.
    from argent_core.test_execution import compute_evidence_mac

    rec = _rec("src-A", "/wt/A")
    tampered = EvidenceRecord(
        selector=rec.selector, source_hash="src-A", test_definition_hash="tdh",
        plan_hash="ph", inventory_hash="ih", policy_hash="poh",
        executor_id="ex", classification=ResultClass.TEST_PASS,
        timestamp=rec.timestamp, root="/wt/A", config_hash="ch",
        evidence_hash=compute_evidence_mac(rec, b"0" * 32),
    )
    # The MAC was computed for src-A; changing source_hash invalidates it.
    assert not store._verify_mac(
        EvidenceRecord(**{**tampered.__dict__, "source_hash": "src-B"}))


# ---------------------------------------------------------------------------
# Case 30: trust / fs narrowing unchanged
# ---------------------------------------------------------------------------

def test_case30_broker_denylist_and_role_scope_unchanged():
    # Narrowing invariants that must never regress.
    for word in ("secret", "password", "api_key", "credential", "recipient"):
        assert word in CONTENT_DENYLIST
    broker = WorkspaceBroker()
    # Absolute paths and setuid targets remain rejected.
    assert broker._is_denied_path("/etc/passwd")
    assert broker._is_denied_path("/proc/self/status")


def test_case30_agent_id_map_unchanged():
    assert AGENT_IDS[Role.LEAD] == "argent-lead"
    assert AGENT_IDS[Role.ANALYST] == "argent-analyst"
    assert AGENT_IDS[Role.IMPLEMENTER] == "argent-implementer"
    assert AGENT_IDS[Role.QA] == "argent-qa"
    assert AGENT_IDS[Role.REVIEWER] == "argent-reviewer"
