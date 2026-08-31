"""Phase B3 process-identity + worktree/writer-ownership tests (H–I).

Offline and deterministic: process identity tuples and worktree classifications
are pure functions; the registry round-trips through the SQLite store.  The
writer guard is tested against a real ``WorkspaceBroker`` write.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from argent_core import Core, OWNER_SOURCE, Role
from argent_core.models import PermissionDenied
from argent_core.process_registry import (
    IDENTITY_BOOT_CHANGED,
    IDENTITY_PID_REUSE,
    IDENTITY_SAME,
    PROCESS_STATUS_RUNNING,
    PROCESS_STATUS_TERMINAL,
    PROCESS_STATUS_UNKNOWN,
    ProcessIdentity,
    ProcessIdentityProvider,
    ProcessRegistry,
    parse_start_ticks,
)
from argent_core.worktree import (
    V_BLOCKED_DIVERGED,
    V_CLEANUP_PENDING,
    V_KEEP_DIRTY,
    V_LOST,
    WorktreeBinding,
    WorktreeEvidence,
    classify_worktree_recovery,
    resolve_canonical_worktree_path,
    writer_guard_for,
)
from argent_core.workspace_broker import CONTROLLER_SOURCE, WorkspaceBroker

OWNER = OWNER_SOURCE


def make_env(db_path):
    core = Core(db_path)
    project = core.create_project("p", OWNER)
    return SimpleNamespace(core=core, project=project)


def stat_line(starttime):
    """A well-formed /proc/<pid>/stat line with the given field-22 starttime."""
    fields_3_to_21 = list(range(4, 22))  # 19 values for fields 4..22 minus starttime
    tail = " ".join(str(v) for v in fields_3_to_21)
    return f"123 (myproc) S {tail} {starttime}"


# ---------------------------------------------------------------------------
# H. Process identity
# ---------------------------------------------------------------------------

def test_parse_start_ticks():
    assert parse_start_ticks(stat_line(9999)) == 9999
    assert parse_start_ticks(stat_line(0)) == 0
    with pytest.raises(ValueError):
        parse_start_ticks("too short")


def test_identity_provider_injectable():
    prov = ProcessIdentityProvider(
        boot_id_reader=lambda: "boot-1",
        stat_reader=lambda pid: stat_line(4242) if pid == 100 else None,
    )
    ident = prov.current(100)
    assert ident == ProcessIdentity(boot_id="boot-1", pid=100,
                                    process_start_ticks=4242)
    assert prov.process_start_ticks(999) is None


def test_classify_identity_tuple(db_path):
    reg = {"boot_id": "boot-1", "pid": 100, "process_start_ticks": 4242}
    same = ProcessIdentity(boot_id="boot-1", pid=100, process_start_ticks=4242)
    assert ProcessRegistry.classify_identity(reg, same) == IDENTITY_SAME
    reuse = ProcessIdentity(boot_id="boot-1", pid=100, process_start_ticks=9999)
    assert ProcessRegistry.classify_identity(reg, reuse) == IDENTITY_PID_REUSE
    boot = ProcessIdentity(boot_id="boot-2", pid=100, process_start_ticks=4242)
    assert ProcessRegistry.classify_identity(reg, boot) == IDENTITY_BOOT_CHANGED


def test_registry_roundtrip_and_fail_closed(db_path):
    env = make_env(db_path)
    task = env.core.create_task(env.project.id, "t", OWNER)
    store = env.core._store
    from argent_core.supervisor import Supervisor
    from mock_supervisor_runtime import FakeRunStatusProvider
    sup = Supervisor(env.core, FakeRunStatusProvider())
    job = sup.store.create_job(task.id, idempotency_key="j1")
    reg = ProcessRegistry(store)
    ident = ProcessIdentity(boot_id="boot-1", pid=100, process_start_ticks=4242)
    row = reg.register(job_id=job.supervisor_job_id, dispatch_id=None,
                       identity=ident)
    recs = store.list_process_registrations(job.supervisor_job_id)
    assert len(recs) == 1
    assert recs[0]["boot_id"] == "boot-1"
    assert recs[0]["pid"] == 100
    assert recs[0]["status"] == PROCESS_STATUS_RUNNING

    # RUNNING/UNKNOWN are never "surely dead" (fail-closed).
    assert ProcessRegistry.is_terminally_dead(recs[0]) is False
    store._update_process_registration(
        row["process_id"], status=PROCESS_STATUS_UNKNOWN)
    assert ProcessRegistry.is_terminally_dead(
        store.get_process_registration(row["process_id"])) is False
    # Only TERMINAL + terminal_at is authoritative terminal evidence.
    store._mark_process_terminal(row["process_id"], exit_code=0,
                                 terminal_at=store.now_iso())
    assert ProcessRegistry.is_terminally_dead(
        store.get_process_registration(row["process_id"])) is True
    env.core.close()


def test_agent_text_has_no_authority():
    # "process completed" in agent prose is not registry evidence.
    reg = {"status": PROCESS_STATUS_RUNNING, "terminal_at": None}
    assert ProcessRegistry.is_terminally_dead(reg) is False
    reg2 = {"status": PROCESS_STATUS_UNKNOWN, "terminal_at": None}
    assert ProcessRegistry.is_terminally_dead(reg2) is False


# ---------------------------------------------------------------------------
# I. Worktree / writer ownership
# ---------------------------------------------------------------------------

def test_canonical_path_rejects_escape(tmp_path):
    root = str(tmp_path)
    # Absolute path rejected.
    with pytest.raises(ValueError):
        resolve_canonical_worktree_path("/etc/passwd")
    # '..' escape rejected.
    with pytest.raises(ValueError):
        resolve_canonical_worktree_path("../outside")
    # Valid relative path resolves under root.
    p = resolve_canonical_worktree_path("job-1/repo", base_root=root)
    assert p == os.path.realpath(os.path.join(root, "job-1/repo"))
    # Escape via '..' inside a path is rejected.
    with pytest.raises(ValueError):
        resolve_canonical_worktree_path("job-1/../../outside", base_root=root)


def test_recovery_classification_read_only():
    owned = WorktreeBinding(
        job_id="j1", canonical_worktree_path="/x", repo_identity="repo-a",
        base_commit="base1", expected_head="sha1",
        writer_dispatch_id="d1", writer_owner_instance_id="A",
        writer_lease_epoch=1,
    )
    # Foreign repo -> LOST (never touch).
    v = classify_worktree_recovery(owned, WorktreeEvidence(repo_identity="repo-b"))
    assert v.verdict == V_LOST
    # Dirty but PROVEN job-owned (bound writer) -> keep, never delete.
    v = classify_worktree_recovery(owned, WorktreeEvidence(
        repo_identity="repo-a", dirty=True))
    assert v.verdict == V_KEEP_DIRTY
    # Divergent HEAD -> BLOCKED (never overwrite).
    v = classify_worktree_recovery(owned, WorktreeEvidence(
        repo_identity="repo-a", head="sha2", dirty=False))
    assert v.verdict == V_BLOCKED_DIVERGED
    # Clean, correct repo + HEAD + base + terminal writer -> cleanup pending.
    v = classify_worktree_recovery(owned, WorktreeEvidence(
        repo_identity="repo-a", head="sha1", dirty=False), writer_terminal=True)
    assert v.verdict == V_CLEANUP_PENDING


def test_writer_guard_accepts_correct_writer(tmp_path):
    scope = os.path.realpath(str(tmp_path))
    job = {"canonical_worktree_path": scope, "writer_dispatch_id": "d1"}
    guard = writer_guard_for(lambda: job, dispatch_id="d1")
    guard(scope, Role.IMPLEMENTER, CONTROLLER_SOURCE)  # no raise


def test_writer_guard_rejects_wrong_dispatch_and_path(tmp_path):
    scope = os.path.realpath(str(tmp_path))
    job = {"canonical_worktree_path": scope, "writer_dispatch_id": "d1"}
    # Guard bound to a DIFFERENT dispatch -> must reject (not the writer).
    guard = writer_guard_for(lambda: job, dispatch_id="d2")
    with pytest.raises(PermissionDenied):
        guard(scope, Role.IMPLEMENTER, CONTROLLER_SOURCE)
    # Correct dispatch but wrong worktree path -> rejected.
    guard2 = writer_guard_for(lambda: job, dispatch_id="d1")
    other = os.path.realpath(os.path.join(str(tmp_path), "..", "other"))
    with pytest.raises(PermissionDenied):
        guard2(other, Role.IMPLEMENTER, CONTROLLER_SOURCE)


def test_writer_guard_stale_binding_fails_closed(tmp_path):
    scope = os.path.realpath(str(tmp_path))
    state = {"job": {"canonical_worktree_path": scope, "writer_dispatch_id": "d1"}}
    guard = writer_guard_for(lambda: state["job"], dispatch_id="d1")
    # A takeover swapped the writer binding: fresh read sees a different writer.
    state["job"] = {"canonical_worktree_path": scope, "writer_dispatch_id": "d2"}
    with pytest.raises(PermissionDenied):
        guard(scope, Role.IMPLEMENTER, CONTROLLER_SOURCE)


def test_broker_enforces_writer_guard(tmp_path):
    scope = os.path.realpath(str(tmp_path))
    state = {"job": {"canonical_worktree_path": scope, "writer_dispatch_id": "d1"}}
    guard = writer_guard_for(lambda: state["job"], dispatch_id="d1")

    broker = WorkspaceBroker(writer_guard=guard)
    import base64
    patch_set = [{"op": "write", "path": "file.txt",
                  "content": base64.b64encode(b"hello").decode()}]
    # Correct writer -> accepted (file created).
    res = broker.apply_patch_set(scope, patch_set, Role.IMPLEMENTER,
                                 CONTROLLER_SOURCE)
    assert not res.errors
    assert os.path.exists(os.path.join(scope, "file.txt"))

    # Stale writer (binding swapped) -> rejected before any write.  The guard
    # raises PermissionDenied (the broker propagates it; it does NOT silently
    # skip or return a partial result).
    state["job"] = {"canonical_worktree_path": scope, "writer_dispatch_id": "d2"}
    with pytest.raises(PermissionDenied):
        broker.apply_patch_set(
            scope, [{"op": "write", "path": "file2.txt",
                     "content": base64.b64encode(b"x").decode()}],
            Role.IMPLEMENTER, CONTROLLER_SOURCE)
    assert not os.path.exists(os.path.join(scope, "file2.txt"))


def test_worktree_fields_persist_on_job(db_path):
    env = make_env(db_path)
    task = env.core.create_task(env.project.id, "t", OWNER)
    store = env.core._store
    from argent_core.supervisor import Supervisor
    from mock_supervisor_runtime import FakeRunStatusProvider
    sup = Supervisor(env.core, FakeRunStatusProvider())
    job = sup.store.create_job(task.id, idempotency_key="j")
    store._update_supervisor_job(
        job.supervisor_job_id,
        canonical_worktree_path="/home/pc/projects/argent-worktrees/j1",
        repo_identity="repo-a", base_commit="base1",
        branch_identity="main", writer_dispatch_id="d1",
        writer_owner_instance_id="A", writer_lease_epoch=3,
        expected_head="head1",
    )
    row = store.get_supervisor_job(job.supervisor_job_id)
    assert row["canonical_worktree_path"] == "/home/pc/projects/argent-worktrees/j1"
    assert row["repo_identity"] == "repo-a"
    assert row["writer_dispatch_id"] == "d1"
    assert row["writer_lease_epoch"] == 3
    assert row["expected_head"] == "head1"
    env.core.close()
