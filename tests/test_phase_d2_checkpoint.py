"""Phase D2 — checkpoint tests (D/E/H).  Deterministic, no providers.

Proves: immutability (INSERT-only, sequential checkpoint_no), integrity (hash
recomputation), reload across reopen, latest selection, fencing (stale lease
epoch / stale owner), stale-reference detection, and resume building a NEW pack.
"""

from __future__ import annotations

import pytest

from argent_core import Core, OWNER_SOURCE, Role, role_source
from argent_core.checkpoint import (
    CheckpointCode,
    CheckpointContext,
    CheckpointProgress,
    CheckpointStore,
    CheckpointWorkflow,
    STALE_CONTEXT_REFERENCE,
    build_checkpoint_record,
    checkpoint_references_valid,
    resume_context,
)
from argent_core.context_pack import ContextBuilder
from argent_core.models import LeaseFencedError

OWNER = OWNER_SOURCE
LEAD = role_source(Role.LEAD)


@pytest.fixture
def env(db_path):
    core = Core(db_path)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER, description="fix the bug")
    yield core, task
    core.close()


def _job_id(core, task):
    from mock_supervisor_runtime import FakeRunStatusProvider, FakeClock
    from argent_core.supervisor import Supervisor
    sup = Supervisor(core, FakeRunStatusProvider(), clock=FakeClock())
    job = sup.store.create_job(task.id, idempotency_key="job-1")
    return job.supervisor_job_id


def _cp(job_id, no, **kw):
    base = dict(
        job_id=job_id, checkpoint_no=no,
        workflow=CheckpointWorkflow(primary_state="RUNNING",
                                    logical_step="implement"),
        code=CheckpointCode(head_commit=""),
    )
    base.update(kw)
    return build_checkpoint_record(**base)


def _claim(core, jid, owner="A", epoch=1, expires="2099-01-01T00:00:00+00:00"):
    core._store._update_supervisor_job(
        jid, owner_instance_id=owner, lease_epoch=epoch,
        lease_expires_at=expires,
    )


def _facts(jid, **kw):
    base = {
        "job_id": jid,
        "worktree_path": "",
        "repo_identity": "",
        "base_commit": "",
        "head_commit": "",
        "artifact_hashes": {},
        "known_handoff_ids": frozenset(),
        "known_packs": {},
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# D. Immutability + sequence
# ---------------------------------------------------------------------------

def test_insert_only_sequential_no(env):
    core, task = env
    jid = _job_id(core, task)
    _claim(core, jid)
    cs = CheckpointStore(core._store)
    c1 = _cp(jid, 1)
    c2 = _cp(jid, 2)
    cs.create_checkpoint(c1, owner_instance_id="A", lease_epoch=1)
    cs.create_checkpoint(c2, owner_instance_id="A", lease_epoch=1)
    rows = cs.list_checkpoints(jid)
    assert [r.identity.checkpoint_no for r in rows] == [1, 2]
    # Immutable INSERT-only: a third write derives #3 — never overwrites #1/#2
    # and never honours the caller's (arbitrary) checkpoint_no=99.
    cs.create_checkpoint(_cp(jid, 99), owner_instance_id="A", lease_epoch=1)
    assert [r.identity.checkpoint_no for r in cs.list_checkpoints(jid)] == [1, 2, 3]
    # Latest pointer points at #3.
    latest = cs.latest_checkpoint(jid)
    assert latest.identity.checkpoint_no == 3


def test_duplicate_checkpoint_id_refused(env):
    core, task = env
    jid = _job_id(core, task)
    _claim(core, jid)
    cs = CheckpointStore(core._store)
    c1 = _cp(jid, 1)
    r1 = cs.create_checkpoint(c1, owner_instance_id="A", lease_epoch=1)
    # The store derives the sequential number; re-writing the SAME record yields
    # a NEW id (#2) — an id is never reused (INSERT-only, no overwrite).
    r2 = cs.create_checkpoint(c1, owner_instance_id="A", lease_epoch=1)
    assert r2.identity.checkpoint_no == 2
    assert r2.identity.checkpoint_id != r1.identity.checkpoint_id


def test_next_checkpoint_no(env):
    core, task = env
    jid = _job_id(core, task)
    _claim(core, jid)
    cs = CheckpointStore(core._store)
    assert cs.next_checkpoint_no(jid) == 1
    cs.create_checkpoint(_cp(jid, 1), owner_instance_id="A", lease_epoch=1)
    assert cs.next_checkpoint_no(jid) == 2


# ---------------------------------------------------------------------------
# D. Integrity + reload
# ---------------------------------------------------------------------------

def test_integrity_hash_tamper(env):
    core, task = env
    jid = _job_id(core, task)
    _claim(core, jid)
    cs = CheckpointStore(core._store)
    cs.create_checkpoint(_cp(jid, 1), owner_instance_id="A", lease_epoch=1)
    latest = cs.latest_checkpoint(jid)
    # Directly corrupt the stored hash and confirm load fails closed.
    core._store._conn.execute(
        "UPDATE checkpoints SET content_hash = ? WHERE checkpoint_id = ?",
        ("0" * 64, latest.identity.checkpoint_id),
    )
    with pytest.raises(Exception):
        cs.latest_checkpoint(jid)


def test_reload_across_reopen(db_path):
    core = Core(db_path)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    jid = _job_id(core, task)
    _claim(core, jid)
    cs = CheckpointStore(core._store)
    cp = _cp(jid, 1, code=CheckpointCode(head_commit="abc123"))
    cs.create_checkpoint(cp, owner_instance_id="A", lease_epoch=1)
    core.close()

    core2 = Core(db_path)
    try:
        cs2 = CheckpointStore(core2._store)
        latest = cs2.latest_checkpoint(jid)
        assert latest.identity.checkpoint_no == 1
        assert latest.code.head_commit == "abc123"
    finally:
        core2.close()


# ---------------------------------------------------------------------------
# H. Fencing
# ---------------------------------------------------------------------------

def test_stale_lease_epoch_fenced(env):
    core, task = env
    jid = _job_id(core, task)
    # Holder A claims the job (epoch 1).
    core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=60)
    cs = CheckpointStore(core._store)
    # Correct holder + epoch succeeds.
    cs.create_checkpoint(_cp(jid, 1), owner_instance_id="A", lease_epoch=1)
    # A stale epoch (0) is fenced — no write.
    with pytest.raises(LeaseFencedError):
        cs.create_checkpoint(_cp(jid, 2), owner_instance_id="A", lease_epoch=0)
    assert cs.next_checkpoint_no(jid) == 2  # nothing was written


def test_stale_owner_fenced(env):
    core, task = env
    jid = _job_id(core, task)
    core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=60)
    cs = CheckpointStore(core._store)
    with pytest.raises(LeaseFencedError):
        cs.create_checkpoint(_cp(jid, 1), owner_instance_id="B", lease_epoch=1)
    assert cs.latest_checkpoint(jid) is None


def test_unleased_write_refused(env):
    core, task = env
    jid = _job_id(core, task)
    cs = CheckpointStore(core._store)
    # No lease holder => the fence refuses the write fail-closed (the legacy
    # unfenced path is removed).
    with pytest.raises(LeaseFencedError):
        cs.create_checkpoint(_cp(jid, 1), owner_instance_id="A", lease_epoch=1)
    assert cs.latest_checkpoint(jid) is None


# ---------------------------------------------------------------------------
# E. Stale detection
# ---------------------------------------------------------------------------

def test_head_mismatch_is_stale(env):
    core, task = env
    jid = _job_id(core, task)
    cp = _cp(jid, 1, code=CheckpointCode(head_commit="abc123"))
    ok, reason = checkpoint_references_valid(cp, _facts(jid, head_commit="def456"))
    assert not ok
    assert reason == STALE_CONTEXT_REFERENCE


def test_artifact_hash_changed_is_stale(env):
    core, task = env
    jid = _job_id(core, task)
    cp = _cp(jid, 1, context=CheckpointContext(
        selected_artifact_refs=(("src/f.py", "a" * 64),)))
    ok, reason = checkpoint_references_valid(
        cp, _facts(jid, artifact_hashes={"src/f.py": "b" * 64}))
    assert not ok
    assert reason == STALE_CONTEXT_REFERENCE


def test_missing_artifact_is_stale(env):
    core, task = env
    jid = _job_id(core, task)
    cp = _cp(jid, 1, context=CheckpointContext(
        selected_artifact_refs=(("src/f.py", "a" * 64),)))
    ok, reason = checkpoint_references_valid(cp, _facts(jid, artifact_hashes={}))
    assert not ok
    assert reason == STALE_CONTEXT_REFERENCE


def test_unknown_handoff_ref_is_stale(env):
    core, task = env
    jid = _job_id(core, task)
    cp = _cp(jid, 1, context=CheckpointContext(latest_handoff_refs=("ho_1",)))
    ok, reason = checkpoint_references_valid(
        cp, _facts(jid, known_handoff_ids=frozenset()))
    assert not ok
    assert reason == STALE_CONTEXT_REFERENCE


def test_missing_pack_ref_is_stale(env):
    core, task = env
    jid = _job_id(core, task)
    cp = _cp(jid, 1, context=CheckpointContext(last_context_pack_id="cp_1"))
    ok, reason = checkpoint_references_valid(cp, _facts(jid, known_packs={}))
    assert not ok
    assert reason == STALE_CONTEXT_REFERENCE


def test_valid_references_ok(env):
    core, task = env
    jid = _job_id(core, task)
    cp = _cp(jid, 1, context=CheckpointContext(
        last_context_pack_id="cp_1",
        selected_artifact_refs=(("src/f.py", "a" * 64),),
        latest_handoff_refs=("ho_1",),
    ), code=CheckpointCode(head_commit="abc123"))
    ok, reason = checkpoint_references_valid(cp, _facts(
        jid,
        head_commit="abc123",
        artifact_hashes={"src/f.py": "a" * 64},
        known_handoff_ids=frozenset({"ho_1"}),
        known_packs={"cp_1": "0" * 64},
    ))
    assert ok and reason == ""


# ---------------------------------------------------------------------------
# E. Resume
# ---------------------------------------------------------------------------

def test_resume_builds_new_pack(env):
    core, task = env
    jid = _job_id(core, task)
    builder = ContextBuilder()
    cp = _cp(jid, 1, context=CheckpointContext(
        selected_artifact_refs=(("src/f.py", "a" * 64),),
        latest_handoff_refs=("ho_1",),
    ), code=CheckpointCode(head_commit="abc123"))
    pack = resume_context(
        cp, context_builder=builder, job_id=jid, dispatch_id="d2",
        role="qa", objective="verify the fix",
        constraints=("constraint1",),
        current_facts=_facts(
            jid,
            head_commit="abc123",
            artifact_hashes={"src/f.py": "a" * 64},
            known_handoff_ids=frozenset({"ho_1"}),
        ),
    )
    assert pack.job_id == jid
    assert pack.dispatch_id == "d2"
    assert pack.role == "qa"
    assert pack.objective == "verify the fix"
    # The checkpoint artifact/handoff refs are present as bounded items.
    artifact_refs = {a.ref for a in pack.artifacts}
    assert "src/f.py" in artifact_refs


def test_resume_stale_fails_closed(env):
    core, task = env
    jid = _job_id(core, task)
    builder = ContextBuilder()
    cp = _cp(jid, 1, code=CheckpointCode(head_commit="abc123"))
    with pytest.raises(Exception) as ei:
        resume_context(cp, context_builder=builder, job_id=jid,
                       dispatch_id="d2", role="qa", objective="o",
                       current_facts=_facts(jid, head_commit="changed"))
    assert "STALE_CONTEXT_REFERENCE" in str(ei.value)


def test_resume_wrong_job_fails_closed(env):
    core, task = env
    jid = _job_id(core, task)
    builder = ContextBuilder()
    cp = _cp(jid, 1)
    with pytest.raises(Exception):
        resume_context(cp, context_builder=builder, job_id="other",
                       dispatch_id="d2", role="qa", objective="o",
                       current_facts=_facts("other"))


def test_resume_no_raw_history(env):
    """The resume pack contains objective/acceptance/constraints + refs, NOT a
    raw session-history dump."""
    core, task = env
    jid = _job_id(core, task)
    builder = ContextBuilder()
    cp = _cp(jid, 1)
    pack = resume_context(cp, context_builder=builder, job_id=jid,
                          dispatch_id="d2", role="qa", objective="o",
                          current_facts=_facts(jid))
    # Objective is REQUIRED owner instruction; no 'history' items are injected.
    assert pack.objective == "o"
    assert all(it.source_type != "history" for it in pack.items)
