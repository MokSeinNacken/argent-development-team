"""Phase D3 — flow tests (A Full Dev Flow, B No Raw History, C Restart).

Deterministic; real Supervisor/Store integration with fakes (no providers).
"""

from __future__ import annotations

from argent_core import Core, OWNER_SOURCE, Role, role_source
from argent_core.checkpoint import (
    CheckpointCode,
    CheckpointContext,
    CheckpointStore,
    build_checkpoint_record,
)
from argent_core.context_handoff_integration import build_pack_with_retrieval
from argent_core.context_pack import (
    ContextBuilder,
    render_pack,
)
from argent_core.handoff import (
    HandoffArtifact,
    HandoffResult,
    build_handoff_record,
    handoff_to_store_json,
)
from argent_core.retrieval import (
    RetrievalEngine,
    RetrievalRequest,
    RetrievalType,
    make_default_policy,
)
from d3_helpers import make_d3_e2e_env, drive_to_terminal

OWNER = OWNER_SOURCE
LEAD = role_source(Role.LEAD)

#: Fields that must NEVER appear in a pack/render (raw session-transcript data).
_FORBIDDEN_TRANSCRIPT_FIELDS = (
    "child_session_id", "run_id", "session_key", "transcript",
    "trajectory", "session_history",
)


def _make_job(core):
    from argent_core.supervisor import Supervisor
    from mock_supervisor_runtime import FakeRunStatusProvider, FakeClock
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER, description="fix the bug")
    sup = Supervisor(core, FakeRunStatusProvider(), clock=FakeClock())
    job = sup.store.create_job(task.id, idempotency_key="job-1")
    return task, job.supervisor_job_id


# ---------------------------------------------------------------------------
# A — FULL DEV FLOW (Owner → Implementer → QA → Reviewer → DONE)
# ---------------------------------------------------------------------------

def test_full_dev_flow_no_raw_history(tmp_path):
    env = make_d3_e2e_env(tmp_path)
    final, row = drive_to_terminal(env)
    assert row is not None and row["terminal"] == "DONE"

    dispatches = env.core._store.list_dispatches(env.task.id)
    roles = {d.role.value for d in dispatches}
    assert Role.IMPLEMENTER.value in roles
    assert Role.QA.value in roles
    assert Role.REVIEWER.value in roles

    # Implementer handoff carries hashed artifact refs + git revision.
    handoffs = env.core._store.list_handoffs_v2(env.jid)
    impl = [h for h in handoffs if h["source_role"] == Role.IMPLEMENTER.value]
    assert impl
    import json
    arts = json.loads(impl[0]["artifacts_json"])
    assert arts and arts[0]["content_hash"] and arts[0]["revision"]

    # Checkpoints are sequential + immutable (INSERT-only).
    cps = env.core._store.list_checkpoints(env.jid)
    assert [c["checkpoint_no"] for c in cps] == list(range(1, len(cps) + 1))

    # Every pack is under its hard budget; every rendered message file carries
    # no session-transcript data.
    for p in env.core._store.list_context_packs(env.jid):
        rec = env.core._store.get_context_pack_by_id(p["context_pack_id"])
        assert rec.token_count <= rec.hard_budget
    for cmd in env.backend.started:
        if "--message-file" in cmd["command"]:
            path = cmd["command"][cmd["command"].index("--message-file") + 1]
            text = open(path, encoding="utf-8").read()
            for field in _FORBIDDEN_TRANSCRIPT_FIELDS:
                assert field not in text
    env.core.close()


# ---------------------------------------------------------------------------
# B — NO RAW HISTORY (empty prior session history; no transcript fields)
# ---------------------------------------------------------------------------

def test_no_raw_history_migrated_flow(tmp_path):
    core = Core(str(tmp_path / "t.db"))
    task, jid = _make_job(core)
    # Store a structured handoff + checkpoint (no transcript data at all).
    rec = build_handoff_record(
        job_id=jid, source_dispatch_id="d-impl", source_role="implementer",
        result=HandoffResult(outcome="done", key_observations=("fixed",)),
        artifacts=(HandoffArtifact(ref="src/f.py",
                                   content_hash="a" * 64, excerpt="def f()"),),
    )
    core._store._insert_handoff_v2(**handoff_to_store_json(rec))
    root = tmp_path / "root"
    root.mkdir()
    (root / "f.py").write_text("def f():\n    return 1\n")
    engine = RetrievalEngine(policy=make_default_policy(allowed_roots=[str(root)]),
                             store=core._store)
    pack = build_pack_with_retrieval(
        context_builder=ContextBuilder(), job_id=jid, dispatch_id="d-qa",
        role="qa", objective="verify", retriever=engine,
        retrieval_requests=[RetrievalRequest(
            job_id=jid, dispatch_id="d-qa",
            source_type=RetrievalType.HANDOFF_LOOKUP, task_id=task.id)],
    )
    rendered = render_pack(pack)
    # No session-transcript fields anywhere in the rendered prompt.
    for field in _FORBIDDEN_TRANSCRIPT_FIELDS:
        assert field not in rendered
    # No OPTIONAL_HISTORY / raw-history items (only handoff = AGENT_RESULT).
    assert all(it.source_type != "history" for it in pack.items)
    core.close()


# ---------------------------------------------------------------------------
# C — RESTART (checkpoint → reopen → NEW pack via checkpoint + retrieval)
# ---------------------------------------------------------------------------

def test_restart_rebuild_checkpoint_and_retrieval(db_path):
    core = Core(db_path)
    task, jid = _make_job(core)
    core._store._update_supervisor_job(jid, owner_instance_id="A", lease_epoch=1)
    cs = CheckpointStore(core._store)
    cp = build_checkpoint_record(
        job_id=jid, checkpoint_no=1,
        code=CheckpointCode(head_commit="abc123"),
        context=CheckpointContext(
            selected_artifact_refs=(("src/f.py", "a" * 64),),
            latest_handoff_refs=("ho_1",),
        ),
    )
    cs.create_checkpoint(cp, owner_instance_id="A", lease_epoch=1)
    # Also store the referenced handoff so HANDOFF_LOOKUP has data.
    ho = build_handoff_record(
        job_id=jid, source_dispatch_id="d-impl", source_role="implementer",
        result=HandoffResult(outcome="done", key_observations=("fixed",)),
    )
    core._store._insert_handoff_v2(**handoff_to_store_json(ho))
    core.close()

    # Reopen: fresh Core + CheckpointStore over the same DB (no session history).
    core2 = Core(db_path)
    try:
        cs2 = CheckpointStore(core2._store)
        latest = cs2.latest_checkpoint(jid)
        engine = RetrievalEngine(policy=make_default_policy(), store=core2._store)
        pack = build_pack_with_retrieval(
            context_builder=ContextBuilder(), job_id=jid, dispatch_id="d-new",
            role="qa", objective="continue",
            retriever=engine,
            retrieval_requests=[RetrievalRequest(
                job_id=jid, dispatch_id="d-new",
                source_type=RetrievalType.HANDOFF_LOOKUP, task_id=task.id)],
            checkpoint=latest,
            checkpoint_current_facts={
                "job_id": jid, "worktree_path": "", "repo_identity": "",
                "base_commit": "", "head_commit": "abc123",
                "artifact_hashes": {"src/f.py": "a" * 64},
                "known_handoff_ids": frozenset({"ho_1", ho.handoff_id}),
                "known_packs": {},
            },
        )
        # Objective comes from the trusted caller (never the checkpoint).
        assert pack.objective == "continue"
        # Checkpoint artifact ref is present as a bounded reference.
        assert any(a.ref == "src/f.py" for a in pack.artifacts)
        # No raw history fallback.
        assert all(it.source_type != "history" for it in pack.items)
    finally:
        core2.close()
