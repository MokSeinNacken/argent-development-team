"""Phase D3 — regression (J): D3 must not regress D1/D2/C/B invariants.

A focused, deterministic regression gate for the contracts the D3 changes
touch (handoff schema, checkpoint stale detection, context-pack integrity,
retrieval bounds, schema version).  The full D2/D1/C/B suites are re-run
separately in verification (see PHASE_D3_NOTES.md).
"""

from __future__ import annotations

import pytest

from argent_core import Core, OWNER_SOURCE, Role, role_source
from argent_core.checkpoint import (
    CheckpointStore,
    build_checkpoint_record,
)
from argent_core.context_pack import (
    ContextBuilder,
    FactInput,
    content_hash,
    validate_context_pack,
)
from argent_core.handoff import (
    HandoffArtifact,
    HandoffResult,
    build_handoff_record,
    handoff_from_store_row,
    handoff_to_store_json,
    validate_handoff_record,
)
from argent_core.retrieval import (
    RetrievalEngine,
    RetrievalError,
    RetrievalRequest,
    RetrievalType,
    make_default_policy,
)
from argent_core.store import SCHEMA_VERSION

OWNER = OWNER_SOURCE
LEAD = role_source(Role.LEAD)


def _make_job(core):
    from argent_core.supervisor import Supervisor
    from mock_supervisor_runtime import FakeRunStatusProvider, FakeClock
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    sup = Supervisor(core, FakeRunStatusProvider(), clock=FakeClock())
    job = sup.store.create_job(task.id, idempotency_key="job-1")
    return task, job.supervisor_job_id


# D2: handoff contract unchanged (trust forced, deterministic hash, roundtrip)
def test_regression_handoff_contract():
    rec = build_handoff_record(
        job_id="j1", source_dispatch_id="d1", source_role="implementer",
        result=HandoffResult(outcome="done", key_observations=("fixed",)),
        artifacts=(HandoffArtifact(ref="src/f.py", content_hash="a" * 64),),
    )
    assert rec.provenance.trust_class == "AGENT_RESULT"
    assert rec.handoff_id.startswith("ho_")
    validate_handoff_record(rec)
    # Determinism + volatile metadata excluded.
    rec2 = build_handoff_record(
        job_id="j1", source_dispatch_id="d1", source_role="implementer",
        result=HandoffResult(outcome="done", key_observations=("fixed",)),
        artifacts=(HandoffArtifact(ref="src/f.py", content_hash="a" * 64),),
    )
    assert rec.content_hash == rec2.content_hash


def test_regression_handoff_roundtrip(db_path):
    core = Core(db_path)
    rec = build_handoff_record(
        job_id="j1", source_dispatch_id="d1", source_role="qa",
        result=HandoffResult(outcome="done"))
    core._store._insert_handoff_v2(**handoff_to_store_json(rec))
    row = core._store.get_handoff_v2(rec.handoff_id)
    rec2 = handoff_from_store_row(row)
    assert rec2.handoff_id == rec.handoff_id
    assert rec2.content_hash == rec.content_hash
    core.close()


# D2: checkpoint immutable + fencing + deterministic hash
def test_regression_checkpoint_contract(db_path):
    from argent_core.models import LeaseFencedError
    core = Core(db_path)
    task, jid = _make_job(core)
    core._store._update_supervisor_job(jid, owner_instance_id="A", lease_epoch=1)
    cs = CheckpointStore(core._store)
    r1 = cs.create_checkpoint(build_checkpoint_record(job_id=jid, checkpoint_no=1),
                              owner_instance_id="A", lease_epoch=1)
    r2 = cs.create_checkpoint(build_checkpoint_record(job_id=jid, checkpoint_no=1),
                              owner_instance_id="A", lease_epoch=1)
    assert r1.identity.checkpoint_no == 1 and r2.identity.checkpoint_no == 2
    with pytest.raises(LeaseFencedError):
        cs.create_checkpoint(build_checkpoint_record(job_id=jid, checkpoint_no=1),
                             owner_instance_id="B", lease_epoch=1)
    core.close()


# D1: context-pack integrity + determinism + hash excludes instance metadata
def test_regression_context_pack_contract():
    b = ContextBuilder()
    p1 = b.build(job_id="j", dispatch_id="d", role="qa", objective="o",
                 now_iso="2026-01-01T00:00:00+00:00")
    p2 = b.build(job_id="j", dispatch_id="d", role="qa", objective="o",
                 now_iso="2026-02-02T00:00:00+00:00")
    assert p1.content_hash == p2.content_hash
    assert p1.context_pack_id == p2.context_pack_id
    validate_context_pack(p1)
    assert content_hash(p1) == p1.content_hash


# D1: dedup (identical items collapse)
def test_regression_context_pack_dedup():
    b = ContextBuilder()
    p = b.build(job_id="j", dispatch_id="d", role="qa", objective="o",
                facts=[FactInput("f", source_ref="r"),
                       FactInput("f", source_ref="r")])
    assert len([it for it in p.items if it.source_type == "fact"]) == 1


# D2: retrieval bounds + fail-closed
def test_regression_retrieval_bounds(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "f.txt").write_text("hello world\n")
    engine = RetrievalEngine(policy=make_default_policy(allowed_roots=[str(root)]))
    r = engine.execute(RetrievalRequest(
        job_id="j", dispatch_id="d", source_type=RetrievalType.FILE_EXCERPT,
        authorized_root=str(root), reference="f.txt", max_excerpt_bytes=5))
    assert r.items[0].truncated
    with pytest.raises(RetrievalError):
        engine.execute(RetrievalRequest(
            job_id="j", dispatch_id="d", source_type=RetrievalType.EXACT_REF,
            authorized_root=str(root), reference="../etc/passwd"))


# Schema: still version 13; additive tables present
def test_regression_schema_version(db_path):
    core = Core(db_path)
    assert SCHEMA_VERSION == "13"
    tables = {r[0] for r in core._store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("context_packs", "handoffs_v2", "checkpoints"):
        assert t in tables
    core.close()
