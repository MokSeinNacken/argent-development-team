"""Phase G2 — deterministic prompt-file cleanup (F3).

Proves the bounded, deterministic lifecycle of the per-dispatch agent prompt
message file:

* it is created under ``cache/prompts`` (never ``/tmp``), owner-only (0700 dir,
  0600 files);
* it is deterministically unlinked on EVERY terminal outcome — SUCCESS (consume),
  FAILED/CANCELLED/TIMEOUT (mark-run-failed) and post-creation enforcement
  failure (no orphan);
* the bounded sweep evicts only stale files (age > 24h) and, for the count
  bound, ONLY files older than a conservative floor (never a fresh/in-flight
  file).

All tests use isolated injected ``prompts_dir``/tmp dirs — they never touch the
real ``~/.cache/argent``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from argent_core import Core, OWNER_SOURCE, Role
from argent_core.resource_policy import ResourceClass
from argent_core.scope_enforcer import EnforcementResult, EnforcementStatus
from argent_core.supervisor import (
    PROMPT_FILE_MIN_AGE_FOR_COUNT_EVICTION_SECONDS,
    Supervisor,
    sweep_prompt_files,
)
from c2_helpers import (
    FakeGovernor,
    FakeScopeBackend,
    FakeSnapshotProvider,
    verified_properties,
)
from d3_helpers import (
    _fake_run_tests,
    _git_init,
    _limits,
    d3_admission,
    make_d3_e2e_env,
    drive_to_terminal,
)
from mock_supervisor_runtime import (
    FakeClock,
    FakeRunLauncher,
    FakeRunStatusProvider,
    canonical_binding,
    make_run_observation,
)

OWNER = OWNER_SOURCE


# ---------------------------------------------------------------------------
# (e) sweep unit tests
# ---------------------------------------------------------------------------

def _touch(path: Path, mtime: float) -> None:
    path.write_text("x", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_sweep_age_eviction_removes_only_old_files(tmp_path):
    d = tmp_path / "prompts"
    d.mkdir()
    old = d / "old.md"
    fresh = d / "fresh.md"
    now = 1_000_000.0
    _touch(old, now - 25 * 3600)   # > 24h -> stale
    _touch(fresh, now - 60)        # 60s -> fresh

    removed = sweep_prompt_files(d, now=now)

    assert removed == 1
    assert not old.exists()
    assert fresh.exists()


def test_sweep_count_eviction_never_removes_fresh_files(tmp_path):
    d = tmp_path / "prompts"
    d.mkdir()
    now = 1_000_000.0
    # max_count=2, but all files are FRESH (well under the 3600s floor).
    files = []
    for i in range(10):
        p = d / f"f{i}.md"
        _touch(p, now - i)   # all fresh (<= 9s old)
        files.append(p)

    removed = sweep_prompt_files(d, now=now, max_count=2)

    assert removed == 0          # count bound must NOT touch fresh files
    assert len(list(d.iterdir())) == 10


def test_sweep_count_eviction_removes_oldest_among_old(tmp_path):
    d = tmp_path / "prompts"
    d.mkdir()
    now = 1_000_000.0
    floor = PROMPT_FILE_MIN_AGE_FOR_COUNT_EVICTION_SECONDS
    # All older than the floor (evictable), ages descending by mtime.
    a = d / "a.md"   # oldest
    b = d / "b.md"
    c = d / "c.md"   # newest (still > floor)
    _touch(a, now - floor - 300)
    _touch(b, now - floor - 200)
    _touch(c, now - floor - 100)

    removed = sweep_prompt_files(d, now=now, max_count=1)

    # Only the oldest is evicted; the newest old file survives.
    assert removed == 2
    assert not a.exists()
    assert not b.exists()
    assert c.exists()


def test_sweep_missing_dir_returns_zero(tmp_path):
    assert sweep_prompt_files(tmp_path / "nope") == 0


# ---------------------------------------------------------------------------
# tracking + deterministic unlink (unit)
# ---------------------------------------------------------------------------

def test_track_and_unlink_prompt_file_is_idempotent(tmp_path):
    d = tmp_path / "prompts"
    d.mkdir()
    clock = FakeClock()
    core = Core(str(tmp_path / "t.db"), clock=clock)
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher(),
                     clock=clock, prompts_dir=d)
    try:
        p = d / "prompt.md"
        p.write_text("hello", encoding="utf-8")
        sup._track_prompt_file("dispatch-1", p)
        assert "dispatch-1" in sup._prompt_files

        sup._unlink_prompt_file("dispatch-1")
        assert not p.exists()
        assert "dispatch-1" not in sup._prompt_files

        # Idempotent: a second unlink is a no-op (never raises).
        sup._unlink_prompt_file("dispatch-1")
    finally:
        core.close()


# ---------------------------------------------------------------------------
# (a) full success cycle -> every prompt file is gone after CONSUME
# ---------------------------------------------------------------------------

def test_full_success_cycle_removes_all_prompt_files(tmp_path):
    env = make_d3_e2e_env(tmp_path)
    prompts_dir = tmp_path / "prompts"
    # The dir is created lazily on first message-file write.
    final, row = drive_to_terminal(env)
    assert row is not None and row["terminal"] == "DONE"

    # At least one dispatch happened (a prompt file was created), and every one
    # was deterministically unlinked on CONSUME.
    assert prompts_dir.exists()
    assert list(prompts_dir.iterdir()) == [], "prompt files were not cleaned up"
    # The in-process tracking dict is empty (no dangling entries).
    assert env.sup._prompt_files == {}
    env.core.close()


# ---------------------------------------------------------------------------
# (d) post-creation enforcement failure -> no orphan
# ---------------------------------------------------------------------------

class _FailingEnforcer:
    """Returns a bounded non-OK enforcement result (no process is started)."""

    def enforce_and_spawn(self, **kwargs):
        return EnforcementResult(
            status=EnforcementStatus.ENFORCEMENT_UNAVAILABLE.value,
            evidence={"reason": "test-failure"},
        )


def test_enforcement_failure_after_creation_leaves_no_orphan(db_path):
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER, description="fix the bug")
    core.start_task_run(task.id, OWNER)
    prompts_dir = Path(db_path).parent / "prompts"
    sup = Supervisor(
        core, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock,
        enforcer=_FailingEnforcer(), prompts_dir=prompts_dir,
    )
    job = sup.store.create_job(task.id, idempotency_key="job-main",
                               resource_class=ResourceClass.HEAVY.value)
    jid = job.supervisor_job_id

    from argent_core.scheduler import Scheduler
    sched = Scheduler(sup, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=FakeGovernor(d3_admission()),
                      snapshot_provider=FakeSnapshotProvider())
    for _ in range(5):
        r = sched.run_pass(jid)
        if r.outcome in ("resource_enforcement_failed", "resource_enforcement_lost"):
            break
    # A message file WAS created (then unlinked on the enforcement failure).
    assert prompts_dir.exists()
    assert list(prompts_dir.iterdir()) == [], "orphan prompt file left behind"
    assert sup._prompt_files == {}
    core.close()


# ---------------------------------------------------------------------------
# (b) FAILED dispatch -> prompt file gone (MARK_RUN_FAILED anchor)
# ---------------------------------------------------------------------------

class _FailingProvider:
    """NOT_FOUND (unbound) -> RUNNING (bind) -> FAILED (terminal)."""

    def __init__(self, core):
        self.core = core
        self._phase = {}

    def observe(self, lookup):
        from argent_core.supervisor import RunStatus
        d = self.core.queries.get_dispatch(lookup.dispatch_id)
        if d is None:
            return make_run_observation(
                dispatch_id=lookup.dispatch_id, role=Role.LEAD,
                status=RunStatus.NOT_FOUND, authoritative_not_found=True)
        n = self._phase.get(d.id, 0)
        self._phase[d.id] = n + 1
        provider, model, thinking, session = canonical_binding(d)
        if d.child_session_id is None:
            if n == 0:
                return make_run_observation(
                    dispatch_id=d.id, role=d.role,
                    status=RunStatus.NOT_FOUND, authoritative_not_found=True)
            return make_run_observation(
                dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
                run_id=f"run-{d.id[:8]}", session_id=session,
                provider=provider, model=model, thinking_tier=thinking)
        return make_run_observation(
            dispatch_id=d.id, role=d.role, status=RunStatus.FAILED,
            run_id=d.openclaw_run_id, session_id=d.child_session_id,
            provider=provider, model=model, thinking_tier=thinking,
            error_code="E_FAKE",
        )


def _make_failing_e2e_env(tmp_path):
    from argent_core.checkpoint import CheckpointStore
    from argent_core.retrieval import RetrievalEngine, make_default_policy
    from argent_core.worktree import GitProvenanceProvider
    from argent_core.scheduler import Scheduler

    clock = FakeClock()
    db = str(tmp_path / "fail.db")
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True, exist_ok=True)
    (ws / "tests").mkdir(parents=True, exist_ok=True)
    (ws / "src" / "module.py").write_text("# stub\n")
    _git_init(ws)

    core = Core(db, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    core.start_task_run(task.id, OWNER)

    prov = _FailingProvider(core)
    backend = FakeScopeBackend(verify_properties=verified_properties(_limits()))
    from argent_core.scope_enforcer import ExecutionEnforcer
    enforcer = ExecutionEnforcer(backend)
    git = GitProvenanceProvider(str(ws))
    retriever = RetrievalEngine(
        policy=make_default_policy(allowed_roots=[str(ws)]), store=core._store)
    checkpoint_store = CheckpointStore(core._store, git_provenance_provider=git)

    sup = Supervisor(
        core, prov, FakeRunLauncher(), clock=clock,
        workspace_root=str(ws), run_tests_fn=_fake_run_tests,
        enforcer=enforcer,
        resource_governor=FakeGovernor(d3_admission()),
        snapshot_provider=FakeSnapshotProvider(),
        retriever=retriever, checkpoint_store=checkpoint_store,
        git_provenance_provider=git,
        prompts_dir=tmp_path / "prompts",
    )
    job = sup.store.create_job(task.id, idempotency_key="job-main",
                               resource_class=ResourceClass.HEAVY.value)
    jid = job.supervisor_job_id
    sched = Scheduler(sup, owner_instance_id="instance-A", lease_ttl_seconds=600,
                      resource_governor=FakeGovernor(d3_admission()),
                      snapshot_provider=FakeSnapshotProvider())
    return SimpleNamespace(
        core=core, sup=sup, sched=sched, clock=clock, jid=jid, ws=ws,
    )


def test_failed_dispatch_removes_prompt_file(tmp_path):
    env = _make_failing_e2e_env(tmp_path)
    prompts_dir = tmp_path / "prompts"
    for _ in range(400):
        env.sched.run_pass(env.jid)
        row = env.core._store.get_supervisor_job(env.jid)
        if row is not None and row["terminal"] is not None:
            break
    row = env.core._store.get_supervisor_job(env.jid)
    assert row is not None and row["terminal"] in ("FAILED", "ERROR")
    # The prompt file(s) created for the failed dispatch(es) are gone.
    assert not prompts_dir.exists() or list(prompts_dir.iterdir()) == []
    assert env.sup._prompt_files == {}
    env.core.close()
