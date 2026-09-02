"""Phase D3 — recovery tests (D Crash Windows, E Fencing).

Deterministic; store + supervisor integration with fakes (no providers).
Proves crash-window consistency (no duplicate authoritative state) and fencing
(stale lease/owner never writes a checkpoint/resume).
"""

from __future__ import annotations

import pytest

from argent_core import Core, OWNER_SOURCE, Role, role_source
from argent_core.checkpoint import (
    CheckpointStore,
    build_checkpoint_record,
)
from argent_core.context_pack import ContextBuildError, ContextBuilder
from argent_core.handoff import (
    HandoffArtifact,
    HandoffResult,
    build_handoff_record,
    handoff_to_store_json,
)
from argent_core.models import LeaseFencedError
from d3_helpers import make_d3_env

OWNER = OWNER_SOURCE
LEAD = role_source(Role.LEAD)


def _make_job(core):
    from argent_core.supervisor import Supervisor
    from mock_supervisor_runtime import FakeRunStatusProvider, FakeClock
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER, description="fix the bug")
    sup = Supervisor(core, FakeRunStatusProvider(), clock=FakeClock())
    job = sup.store.create_job(task.id, idempotency_key="job-1")
    return task, job.supervisor_job_id


# ---------------------------------------------------------------------------
# D — CRASH WINDOWS
# ---------------------------------------------------------------------------

def test_crash_window_handoff_no_duplicate(db_path):
    import sqlite3
    core = Core(db_path)
    task, jid = _make_job(core)
    rec = build_handoff_record(
        job_id=jid, source_dispatch_id="d1", source_role="implementer",
        result=HandoffResult(outcome="done",
                             key_observations=("fixed",)),
        artifacts=(HandoffArtifact(ref="src/f.py", content_hash="a" * 64),),
    )
    core._store._insert_handoff_v2(**handoff_to_store_json(rec))
    # Crash + re-drive the same insert: the PRIMARY KEY rejects the duplicate
    # (no duplicate authoritative state; the supervisor checks before insert).
    with pytest.raises(sqlite3.IntegrityError):
        core._store._insert_handoff_v2(**handoff_to_store_json(rec))
    rows = core._store.list_handoffs_v2(jid)
    assert len(rows) == 1
    assert rows[0]["handoff_id"] == rec.handoff_id
    core.close()


def test_crash_window_checkpoint_insert_only(db_path):
    core = Core(db_path)
    task, jid = _make_job(core)
    core._store._update_supervisor_job(jid, owner_instance_id="A", lease_epoch=1)
    cs = CheckpointStore(core._store)
    cp = build_checkpoint_record(job_id=jid, checkpoint_no=1)
    # Same semantic record written twice derives sequential #1/#2 — never an
    # overwrite of an authoritative row.
    r1 = cs.create_checkpoint(cp, owner_instance_id="A", lease_epoch=1)
    r2 = cs.create_checkpoint(cp, owner_instance_id="A", lease_epoch=1)
    assert r1.identity.checkpoint_no == 1
    assert r2.identity.checkpoint_no == 2
    assert r1.identity.checkpoint_id != r2.identity.checkpoint_id
    # Exactly one "latest" (no duplicate authoritative pointer).
    assert cs.latest_checkpoint(jid).identity.checkpoint_no == 2
    core.close()


def test_crash_window_pack_idempotent_and_stale(db_path):
    from argent_core.scheduler import Scheduler
    from c2_helpers import FakeGovernor, FakeSnapshotProvider
    from d3_helpers import d3_admission
    env = make_d3_env(db_path)
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=FakeGovernor(d3_admission()),
                      snapshot_provider=FakeSnapshotProvider())
    for _ in range(15):
        r = sched.run_pass(env.jid)
        if r.outcome in ("resource_deferred", "resource_denied",
                         "context_build_failed"):
            break
    row = env.core._store.get_supervisor_job(env.jid)
    dispatch_id = row["expected_dispatch_id"]
    existing = env.core._store.get_context_pack(dispatch_id)
    assert existing is not None
    # Rebuild the SAME pack (deterministic) and re-persist: idempotent — same
    # content hash -> same pack id, no duplicate row.
    d = env.core._store.get_dispatch(dispatch_id)
    job = env.core._store.get_supervisor_job(env.jid)
    pack = env.sup._build_context_pack(d, job)
    pid = env.sup._persist_context_pack(pack)
    assert pid == existing.context_pack_id
    assert len(env.core._store.list_context_packs(env.jid)) == 1
    # Different content for the SAME dispatch is a stale pack (fail-closed).
    other = ContextBuilder().build(job_id=env.jid, dispatch_id=dispatch_id,
                                   role="qa", objective="DIFFERENT")
    with pytest.raises(ContextBuildError) as ei:
        env.sup._persist_context_pack(other)
    assert ei.value.code == "CONTEXT_STALE_PACK"
    env.core.close()


# ---------------------------------------------------------------------------
# E — FENCING (dual / stale supervisor)
# ---------------------------------------------------------------------------

def test_dual_supervisor_stale_owner_fenced(db_path):
    core = Core(db_path)
    task, jid = _make_job(core)
    core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=60)
    cs = CheckpointStore(core._store)
    cp = build_checkpoint_record(job_id=jid, checkpoint_no=1)
    with pytest.raises(LeaseFencedError):
        cs.create_checkpoint(cp, owner_instance_id="B", lease_epoch=1)
    assert cs.latest_checkpoint(jid) is None
    core.close()


def test_dual_supervisor_stale_epoch_fenced(db_path):
    core = Core(db_path)
    task, jid = _make_job(core)
    core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=60)
    cs = CheckpointStore(core._store)
    cs.create_checkpoint(build_checkpoint_record(job_id=jid, checkpoint_no=1),
                         owner_instance_id="A", lease_epoch=1)
    # A stale epoch (0) after a takeover cannot write.
    with pytest.raises(LeaseFencedError):
        cs.create_checkpoint(build_checkpoint_record(job_id=jid, checkpoint_no=2),
                             owner_instance_id="A", lease_epoch=0)
    assert cs.next_checkpoint_no(jid) == 2  # nothing written
    core.close()


def test_unleased_write_refused_fail_closed(db_path):
    core = Core(db_path)
    task, jid = _make_job(core)
    cs = CheckpointStore(core._store)
    with pytest.raises(LeaseFencedError):
        cs.create_checkpoint(build_checkpoint_record(job_id=jid, checkpoint_no=1),
                             owner_instance_id="A", lease_epoch=1)
    assert cs.latest_checkpoint(jid) is None
    core.close()
