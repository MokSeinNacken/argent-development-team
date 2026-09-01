"""Phase D2 — fix-round adversarial tests (F1–F6).

Deterministic, no providers.  Proves the six Sol-closing-review findings are
closed: stale-validation fail-closed (F1), non-bypassable fencing + sequential
checkpoint_no (F2), strict limits + byte-budget enforcement (F3), the
ContextError dispatch gate (F4), schema/bounded validation + persisted
record_version (F5), and resume-with-retrieval (F6).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from argent_core import Core, OWNER_SOURCE, Role, role_source
from argent_core.checkpoint import (
    CONTEXT_CHECKPOINT_INVALID,
    STALE_CONTEXT_REFERENCE,
    CheckpointCode,
    CheckpointContext,
    CheckpointError,
    CheckpointStore,
    build_checkpoint_record,
    checkpoint_references_valid,
)
from argent_core.context_pack import ContextBuilder
from argent_core.handoff import (
    HandoffError,
    HandoffResult,
    build_handoff_record,
    handoff_from_store_row,
    handoff_to_store_json,
)
from argent_core.models import LeaseFencedError
from argent_core.retrieval import (
    RetrievalEngine,
    RetrievalError,
    RetrievalRequest,
    RetrievalType,
    make_default_policy,
)

OWNER = OWNER_SOURCE
LEAD = role_source(Role.LEAD)


def _make_job(core):
    from mock_supervisor_runtime import FakeRunStatusProvider, FakeClock
    from argent_core.supervisor import Supervisor
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER, description="fix the bug")
    sup = Supervisor(core, FakeRunStatusProvider(), clock=FakeClock())
    job = sup.store.create_job(task.id, idempotency_key="job-1")
    return task, job.supervisor_job_id, sup


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


def _cp(jid, no=1, **kw):
    return build_checkpoint_record(
        job_id=jid, checkpoint_no=no, **kw)


# ---------------------------------------------------------------------------
# F1 — stale validation is REQUIRED + fail-closed
# ---------------------------------------------------------------------------

def test_references_valid_requires_current_facts(env_db):
    core, jid = env_db
    cp = _cp(jid)
    ok, reason = checkpoint_references_valid(cp, None)
    assert not ok
    assert reason == CONTEXT_CHECKPOINT_INVALID


def test_wrong_head_fails_closed(env_db):
    core, jid = env_db
    cp = _cp(jid, code=CheckpointCode(head_commit="abc123"))
    ok, reason = checkpoint_references_valid(cp, _facts(jid, head_commit="def456"))
    assert not ok and reason == STALE_CONTEXT_REFERENCE


def test_wrong_base_commit_fails_closed(env_db):
    core, jid = env_db
    cp = _cp(jid, code=CheckpointCode(base_commit="base1"))
    ok, reason = checkpoint_references_valid(cp, _facts(jid, base_commit="base2"))
    assert not ok and reason == STALE_CONTEXT_REFERENCE


def test_wrong_repo_identity_fails_closed(env_db):
    core, jid = env_db
    cp = _cp(jid, code=CheckpointCode(repo_identity="/repo/a"))
    ok, reason = checkpoint_references_valid(
        cp, _facts(jid, repo_identity="/repo/b"))
    assert not ok and reason == STALE_CONTEXT_REFERENCE


def test_wrong_pack_hash_fails_closed(env_db):
    core, jid = env_db
    cp = _cp(jid, context=CheckpointContext(
        last_context_pack_id="cp_1", last_context_pack_hash="a" * 64))
    ok, reason = checkpoint_references_valid(
        cp, _facts(jid, known_packs={"cp_1": "b" * 64}))
    assert not ok and reason == STALE_CONTEXT_REFERENCE


def test_wrong_job_lineage_fails_closed(env_db):
    core, jid = env_db
    cp = _cp(jid)
    ok, reason = checkpoint_references_valid(cp, _facts("other-job"))
    assert not ok and reason == CONTEXT_CHECKPOINT_INVALID


def test_missing_worktree_path_fails_closed(env_db):
    core, jid = env_db
    cp = _cp(jid, code=CheckpointCode(worktree_path="/wt/1"))
    # current_facts lacks a worktree_path -> fail closed (incomplete comparison).
    ok, reason = checkpoint_references_valid(cp, _facts(jid))
    assert not ok and reason == STALE_CONTEXT_REFERENCE


# ---------------------------------------------------------------------------
# F2 — fencing non-bypassable + sequential checkpoint_no
# ---------------------------------------------------------------------------

def test_missing_owner_fenced(env_db):
    core, jid = env_db
    cs = CheckpointStore(core._store)
    with pytest.raises(LeaseFencedError):
        cs.create_checkpoint(_cp(jid), owner_instance_id="A", lease_epoch=1)
    assert cs.latest_checkpoint(jid) is None


def test_wrong_owner_fenced(env_db):
    core, jid = env_db
    _claim(core, jid, owner="A", epoch=1)
    cs = CheckpointStore(core._store)
    with pytest.raises(LeaseFencedError):
        cs.create_checkpoint(_cp(jid), owner_instance_id="B", lease_epoch=1)
    assert cs.latest_checkpoint(jid) is None


def test_wrong_epoch_fenced(env_db):
    core, jid = env_db
    _claim(core, jid, owner="A", epoch=1)
    cs = CheckpointStore(core._store)
    with pytest.raises(LeaseFencedError):
        cs.create_checkpoint(_cp(jid), owner_instance_id="A", lease_epoch=0)
    assert cs.latest_checkpoint(jid) is None


def test_expired_lease_fenced(env_db):
    core, jid = env_db
    _claim(core, jid, owner="A", epoch=1, expires="2000-01-01T00:00:00+00:00")
    cs = CheckpointStore(core._store)
    with pytest.raises(LeaseFencedError):
        cs.create_checkpoint(_cp(jid), owner_instance_id="A", lease_epoch=1)
    assert cs.latest_checkpoint(jid) is None


def test_caller_checkpoint_no_ignored(env_db):
    core, jid = env_db
    _claim(core, jid)
    cs = CheckpointStore(core._store)
    r = cs.create_checkpoint(_cp(jid, no=99),
                             owner_instance_id="A", lease_epoch=1)
    assert r.identity.checkpoint_no == 1  # caller's 99 is ignored (MAX+1 wins)


def test_sequential_numbers_no_gap_or_dup(env_db):
    core, jid = env_db
    _claim(core, jid)
    cs = CheckpointStore(core._store)
    for _ in range(3):
        cs.create_checkpoint(_cp(jid), owner_instance_id="A", lease_epoch=1)
    nos = [r.identity.checkpoint_no for r in cs.list_checkpoints(jid)]
    assert nos == [1, 2, 3]
    # Exactly one authoritative latest pointer.
    rows = core._store.list_checkpoints(jid)
    assert sum(1 for r in rows if r["latest"] == 1) == 1


# ---------------------------------------------------------------------------
# F3 — strict limits + byte-budget enforcement
# ---------------------------------------------------------------------------

def _engine_with_store(core, root):
    return RetrievalEngine(policy=make_default_policy(allowed_roots=[root]),
                           store=core._store)


def test_invalid_limits_rejected(env_db, tmp_path):
    core, jid = env_db
    engine = _engine_with_store(core, str(tmp_path))
    for field, bad in (("max_results", 0), ("max_results", -1),
                       ("max_results", "x"), ("max_bytes", 0),
                       ("max_excerpt_bytes", -5)):
        req = RetrievalRequest(job_id=jid, dispatch_id="d1",
                               source_type=RetrievalType.HANDOFF_LOOKUP,
                               **{field: bad})
        with pytest.raises(RetrievalError) as ei:
            engine.execute(req)
        assert ei.value.code == "RETRIEVAL_INVALID_REQUEST"


def test_byte_budget_never_exceeded_handoffs(env_db):
    core, jid = env_db
    for i in range(20):
        rec = build_handoff_record(
            job_id=jid, source_dispatch_id=f"d{i}", source_role="implementer",
            result=HandoffResult(outcome="done",
                                 key_observations=(f"observation {i} " * 10,)),
        )
        core._store._insert_handoff_v2(**handoff_to_store_json(rec))
    engine = _engine_with_store(core, "/nonexistent")
    r = engine.execute(RetrievalRequest(
        job_id=jid, dispatch_id="d", source_type=RetrievalType.HANDOFF_LOOKUP,
        max_bytes=400))
    assert r.total_bytes <= 400
    assert r.total_bytes == sum(len(it.content.encode("utf-8")) for it in r.items)
    assert r.truncated


def test_byte_budget_artifact_path(env_db, tmp_path):
    core, jid = env_db
    root = tmp_path / "root"
    root.mkdir()
    for i in range(10):
        (root / f"f{i}.txt").write_text("x" * 200)
    engine = _engine_with_store(core, str(root))
    r = engine.execute(RetrievalRequest(
        job_id=jid, dispatch_id="d", source_type=RetrievalType.ARTIFACT_LOOKUP,
        authorized_root=str(root), reference=".txt", max_bytes=250))
    assert r.total_bytes <= 250
    assert r.total_bytes == sum(len(it.content.encode("utf-8")) for it in r.items)
    assert r.truncated


# ---------------------------------------------------------------------------
# F4 — ContextError dispatch gate (context_build_failed, error_class=CONTEXT)
# ---------------------------------------------------------------------------

class _RaisingBuilder:
    def __init__(self, error):
        self.error = error

    def build(self, **kwargs):
        raise self.error


def _drive_context_error(db_path, error):
    from d1_helpers import make_d1_env, make_d1_scheduler, drive_d1
    env = make_d1_env(db_path, context_builder=_RaisingBuilder(error))
    sched = make_d1_scheduler(env)
    final = drive_d1(sched, env.jid)
    return env, final


def test_retrieval_error_is_context_build_failed(db_path):
    env, final = _drive_context_error(
        db_path, RetrievalError("RETRIEVAL_INVALID_REQUEST", "bad"))
    assert final.outcome == "context_build_failed"
    row = env.core._store.get_supervisor_job(env.jid)
    assert row["error_class"] == "CONTEXT"
    assert row["primary_state"] == "BLOCKED"
    env.core.close()


def test_checkpoint_error_is_context_build_failed(db_path):
    env, final = _drive_context_error(
        db_path, CheckpointError(CONTEXT_CHECKPOINT_INVALID, "bad"))
    assert final.outcome == "context_build_failed"
    row = env.core._store.get_supervisor_job(env.jid)
    assert row["error_class"] == "CONTEXT"
    env.core.close()


def test_handoff_error_is_context_build_failed(db_path):
    env, final = _drive_context_error(
        db_path, HandoffError("HANDOFF_INVALID_RECORD", "bad"))
    assert final.outcome == "context_build_failed"
    row = env.core._store.get_supervisor_job(env.jid)
    assert row["error_class"] == "CONTEXT"
    env.core.close()


# ---------------------------------------------------------------------------
# F5 — schema/bounded validation + persisted record_version
# ---------------------------------------------------------------------------

def test_wrong_persisted_checkpoint_version_rejected(env_db):
    core, jid = env_db
    _claim(core, jid)
    cs = CheckpointStore(core._store)
    cs.create_checkpoint(_cp(jid), owner_instance_id="A", lease_epoch=1)
    core._store._conn.execute(
        "UPDATE checkpoints SET record_version = '99' WHERE job_id = ?", (jid,))
    with pytest.raises(CheckpointError):
        cs.latest_checkpoint(jid)


def test_wrong_persisted_handoff_version_rejected(env_db):
    core, jid = env_db
    rec = build_handoff_record(job_id=jid, source_dispatch_id="d1",
                               source_role="implementer")
    core._store._insert_handoff_v2(**handoff_to_store_json(rec))
    core._store._conn.execute(
        "UPDATE handoffs_v2 SET record_version = '99' WHERE handoff_id = ?",
        (rec.handoff_id,))
    row = core._store.get_handoff_v2(rec.handoff_id)
    with pytest.raises(ValueError):
        handoff_from_store_row(row)


def test_invalid_hash_format_rejected(env_db):
    core, jid = env_db
    # A non-sha256 content_hash must be rejected at the persistence gate.
    bad = build_checkpoint_record(job_id=jid, checkpoint_no=1)
    with pytest.raises(ValueError):
        core._store._insert_checkpoint(
            checkpoint_id=bad.identity.checkpoint_id,
            record_version="1", job_id=jid, checkpoint_no=1,
            workflow_json="{}", context_json="{}", code_json="{}",
            progress_json="{}", content_hash="not-a-sha256", created_at="",
            latest=1,
        )


def test_json_over_limit_rejected(env_db):
    core, jid = env_db
    _claim(core, jid)
    cs = CheckpointStore(core._store)
    # A code_json far beyond the 64KiB budget must be rejected at the gate.
    from argent_core.store import MAX_JSON_COLUMN_BYTES
    oversized = "x" * (MAX_JSON_COLUMN_BYTES + 1)
    with pytest.raises(ValueError):
        core._store._insert_checkpoint(
            checkpoint_id="ck_" + "0" * 24, record_version="1", job_id=jid,
            checkpoint_no=1, workflow_json="{}", context_json="{}",
            code_json=oversized, progress_json="{}",
            content_hash="0" * 64, created_at="", latest=1,
        )


def test_migration_12_to_13_adds_record_version(tmp_path):
    """A schema-12 DB with checkpoints/handoffs_v2 (no record_version) migrates
    forward additively: the record_version column is added (default = current)."""
    import sqlite3
    db = str(tmp_path / "v12.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('schema_version', '12')")
    conn.execute(
        "CREATE TABLE checkpoints (checkpoint_id TEXT PRIMARY KEY, job_id TEXT "
        "NOT NULL, checkpoint_no INTEGER NOT NULL, workflow_json TEXT NOT NULL, "
        "context_json TEXT NOT NULL, code_json TEXT NOT NULL, progress_json "
        "TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL, "
        "latest INTEGER NOT NULL DEFAULT 0)")
    conn.execute(
        "CREATE TABLE handoffs_v2 (handoff_id TEXT PRIMARY KEY, job_id TEXT "
        "NOT NULL, source_dispatch_id TEXT NOT NULL, source_role TEXT NOT NULL, "
        "result_json TEXT NOT NULL, artifacts_json TEXT NOT NULL, evidence_json "
        "TEXT NOT NULL, next_step_json TEXT NOT NULL, provenance_json TEXT NOT "
        "NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL)")
    conn.commit()
    conn.close()

    core = Core(db)
    try:
        cpcols = {r[1] for r in core._store._conn.execute(
            "PRAGMA table_info(checkpoints)")}
        hcols = {r[1] for r in core._store._conn.execute(
            "PRAGMA table_info(handoffs_v2)")}
        assert "record_version" in cpcols
        assert "record_version" in hcols
    finally:
        core.close()


# ---------------------------------------------------------------------------
# F6 — resume still runs retrieval (no early return, no D1 bypass)
# ---------------------------------------------------------------------------

def test_resume_with_checkpoint_still_runs_retrieval(tmp_path):
    core = Core(str(tmp_path / "t.db"))
    task, jid, sup = _make_job(core)
    rec = build_handoff_record(
        job_id=jid, source_dispatch_id="d-impl", source_role="implementer",
        result=HandoffResult(outcome="done",
                             key_observations=("changed src/f.py",)),
    )
    core._store._insert_handoff_v2(**handoff_to_store_json(rec))
    engine = _engine_with_store(core, str(tmp_path))
    cp = build_checkpoint_record(
        job_id=jid, checkpoint_no=1,
        context=CheckpointContext(latest_handoff_refs=(rec.handoff_id,)),
    )
    from argent_core.context_handoff_integration import build_pack_with_retrieval
    pack = build_pack_with_retrieval(
        context_builder=ContextBuilder(), job_id=jid, dispatch_id="d-qa",
        role="qa", objective="verify", retriever=engine,
        retrieval_requests=[RetrievalRequest(
            job_id=jid, dispatch_id="d-qa",
            source_type=RetrievalType.HANDOFF_LOOKUP, task_id=task.id)],
        checkpoint=cp,
        checkpoint_current_facts=_facts(
            jid, known_handoff_ids=frozenset({rec.handoff_id})),
    )
    prior = [it.content for it in pack.items if it.source_type == "prior_result"]
    # The RETRIEVAL-produced handoff content is present (not just the checkpoint
    # ref "handoff ho_..."), proving retrieval ran alongside the checkpoint.
    assert any("changed src/f.py" in c for c in prior)
    core.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def env_db(db_path):
    core = Core(db_path)
    task, jid, sup = _make_job(core)
    yield core, jid
    core.close()
