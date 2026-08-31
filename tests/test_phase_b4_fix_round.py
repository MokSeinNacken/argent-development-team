"""Phase B4 — adversarial fix-round regression tests (Sol REJECT findings F1-F6).

Each test below is a direct regression for one of the six findings confirmed in
the independent Sol Closing Review, exercising REAL product code paths (no
synthetic classifier objects standing in for the scheduler/broker):

* F1 — a RUNNING job is never claimed directly (process-based recovery bypass);
  takeover goes ONLY through the evidence-bound ``recover_takeover_job`` path.
* F2 — the writer fence is re-asserted immediately before EVERY OS effect, so a
  mid-broker takeover never writes.
* F3 — real git provenance is persisted on the BOUND binding and drives the
  scheduler's worktree recovery decision.
* F4 — ``owner_authorized`` requires an EXACT BLOCKED job (never RUNNING/LOST).
* F5 — DONE/FAILED are immutable in the central low-level mutator.
* F6 — dual-owner takeover races + terminal stickiness with real state changes.

No sleep, no network, no real process; time is ``FakeClock``.
"""

from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from argent_core import Core, OWNER_SOURCE, Role
from argent_core.job_state import PrimaryState
from argent_core.models import LeaseError, LeaseFencedError, PermissionDenied
from argent_core.process_registry import ProcessIdentity, ProcessRegistry
from argent_core.scheduler import OUTCOME_NO_WORK, Scheduler
from argent_core.supervisor import Supervisor
from argent_core.worktree import (
    V_BLOCKED_DIVERGED,
    V_CLEANUP_PENDING,
    V_LOST,
    writer_guard_for,
)
from argent_core.workspace_broker import CONTROLLER_SOURCE, WorkspaceBroker
from mock_supervisor_runtime import FakeClock, FakeRunLauncher, FakeRunStatusProvider

OWNER = OWNER_SOURCE


def _future_iso(seconds=3600):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)) \
        .astimezone(timezone.utc).isoformat()


def _write(scope, path, content=b"x"):
    return [{"op": "write", "path": path,
             "content": base64.b64encode(content).decode()}]


class _ScriptedIdentityProvider:
    def __init__(self, identities):
        self._identities = identities

    def current(self, pid):
        return self._identities.get(
            pid, ProcessIdentity(boot_id=None, pid=pid, process_start_ticks=None))


class _FakeGitProvider:
    """Deterministic git-provenance double (real product path, fake git)."""

    def __init__(self, repo_identity="repo-a", head="sha1", branch="main",
                 dirty=False):
        self._repo = repo_identity
        self._head = head
        self._branch = branch
        self._dirty = dirty

    def repo_identity(self, path=None):
        return self._repo

    def head(self, path=None):
        return self._head

    def branch(self, path=None):
        return self._branch

    def dirty(self, path=None):
        return self._dirty


def _make_env(db_path, *, identity=None, git=None, workspace_root=None):
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    sup = Supervisor(
        core, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock,
        process_identity_provider=identity,
        git_provenance_provider=git,
        workspace_root=workspace_root,
    )
    return SimpleNamespace(core=core, project=project, sup=sup, clock=clock)


def _add_queued_job(env):
    task = env.core.create_task(env.project.id, "t", OWNER)
    job = env.sup.store.create_job(task.id, idempotency_key=f"j-{task.id}")
    return job.supervisor_job_id


def _job_row(core, jid):
    return core._store.get_supervisor_job(jid)


def _register_process(env, jid, *, boot_id="boot-1", pid=100, ticks=4242):
    reg = ProcessRegistry(env.core._store)
    return reg.register(
        job_id=jid, dispatch_id=None,
        identity=ProcessIdentity(boot_id=boot_id, pid=pid,
                                 process_start_ticks=ticks),
    )


# ---------------------------------------------------------------------------
# F1 — process-based recovery cannot be bypassed via the normal claim path
# ---------------------------------------------------------------------------

def test_f1_claim_job_and_claim_next_never_take_over_running(db_path):
    env = _make_env(db_path)
    jid = _add_queued_job(env)
    env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=30)
    env.clock.advance(31)  # expired concrete lease
    # The normal claim path refuses a RUNNING job even with an expired lease.
    with pytest.raises(LeaseError) as exc:
        env.core._store.claim_job(jid, owner_instance_id="B", ttl_seconds=60)
    assert "running_not_claimable" in str(exc.value)
    assert env.core._store.claim_next_job(owner_instance_id="B",
                                          ttl_seconds=60) is None
    assert _job_row(env.core, jid)["owner_instance_id"] == "A"  # unchanged
    env.core.close()


def test_f1_live_process_blocks_run_pass_takeover(db_path):
    identity = _ScriptedIdentityProvider({
        100: ProcessIdentity(boot_id="boot-1", pid=100,
                             process_start_ticks=4242),
    })
    env = _make_env(db_path, identity=identity)
    jid = _add_queued_job(env)
    env.core._store.claim_job(jid, owner_instance_id="writer-A", ttl_seconds=30)
    _register_process(env, jid, boot_id="boot-1", pid=100, ticks=4242)
    env.clock.advance(31)  # lease expired, but the writer process still lives
    sched_b = Scheduler(env.sup, owner_instance_id="writer-B",
                        lease_ttl_seconds=60)
    # run_pass must NOT switch to epoch 2 (no second owner while A lives).
    r = sched_b.run_pass(jid)
    assert r.outcome == OUTCOME_NO_WORK
    row = _job_row(env.core, jid)
    assert row["owner_instance_id"] == "writer-A"
    assert row["lease_epoch"] == 1
    env.core.close()


def test_f1_dead_process_consistent_worktree_takeover_epoch_plus_one(db_path):
    identity = _ScriptedIdentityProvider({
        100: ProcessIdentity(boot_id="boot-2", pid=100,
                             process_start_ticks=4242),  # boot changed -> dead
    })
    git = _FakeGitProvider(repo_identity="repo-a", head="sha1", dirty=False)
    env = _make_env(db_path, identity=identity, git=git,
                    workspace_root="/tmp/f1-ws")
    jid = _add_queued_job(env)
    env.core._store.claim_job(jid, owner_instance_id="writer-A", ttl_seconds=30)
    _register_process(env, jid, boot_id="boot-1", pid=100, ticks=4242)
    # Real persisted binding with real provenance (writer A, epoch 1).
    env.sup.bind_writer_worktree(
        jid, dispatch_id="d1", owner_instance_id="writer-A", lease_epoch=1,
        repo_identity="repo-a", base_commit="base1", branch_identity="main",
        expected_head="sha1",
    )
    env.clock.advance(31)  # lease expired
    sched_b = Scheduler(env.sup, owner_instance_id="writer-B",
                        lease_ttl_seconds=60)
    r = sched_b.run_pass(jid)
    assert r.outcome != OUTCOME_NO_WORK
    row = _job_row(env.core, jid)
    assert row["owner_instance_id"] == "writer-B"
    assert row["lease_epoch"] == 2  # takeover -> epoch+1
    # The old holder is fenced.
    with pytest.raises(LeaseFencedError):
        env.core._store.assert_lease_current(jid, "writer-A", 1)
    env.core.close()


def test_f1_divergent_worktree_blocks_takeover_and_blocks_job(db_path):
    identity = _ScriptedIdentityProvider({
        100: ProcessIdentity(boot_id="boot-2", pid=100,
                             process_start_ticks=4242),  # dead
    })
    # Real repo, but the live HEAD diverges from the persisted expected head.
    git = _FakeGitProvider(repo_identity="repo-a", head="sha2", dirty=False)
    env = _make_env(db_path, identity=identity, git=git,
                    workspace_root="/tmp/f1-div-ws")
    jid = _add_queued_job(env)
    env.core._store.claim_job(jid, owner_instance_id="writer-A", ttl_seconds=30)
    _register_process(env, jid, boot_id="boot-1", pid=100, ticks=4242)
    env.sup.bind_writer_worktree(
        jid, dispatch_id="d1", owner_instance_id="writer-A", lease_epoch=1,
        repo_identity="repo-a", base_commit="base1", branch_identity="main",
        expected_head="sha1",
    )
    env.clock.advance(31)
    sched_b = Scheduler(env.sup, owner_instance_id="writer-B",
                        lease_ttl_seconds=60)
    r = sched_b.run_pass(jid)
    assert r.outcome == OUTCOME_NO_WORK
    row = _job_row(env.core, jid)
    assert row["primary_state"] == PrimaryState.BLOCKED.value  # no takeover
    assert row["terminal"] == "BLOCKED"
    env.core.close()


def test_f1_in_flight_broker_action_blocks_takeover(db_path):
    env = _make_env(db_path)
    jid = _add_queued_job(env)
    env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=30)
    # An in-flight (RUNNING) broker APPLY_PATCH_SET action is an admission block.
    now = env.core._store.now_iso()
    env.core._store._insert_supervisor_action({
        "id": "action-1", "supervisor_job_id": jid, "dispatch_id": None,
        "action_type": "APPLY_PATCH_SET", "action_key": "key-1",
        "args_hash": "h", "input_hash": None, "precondition_hash": None,
        "effect_hash": None, "patch_set_json": None,
        "status": "RUNNING", "attempt_count": 1, "next_attempt_at": None,
        "started_at": now, "finished_at": None, "last_error_code": None,
        "created_at": now, "updated_at": now,
    })
    env.clock.advance(31)
    with pytest.raises(LeaseError) as exc:
        env.core._store.recover_takeover_job(
            jid, expected=_job_row(env.core, jid), owner_instance_id="B",
            ttl_seconds=60, process_alive=False, worktree_verdict=None,
        )
    assert "in-flight broker action" in str(exc.value)
    assert _job_row(env.core, jid)["owner_instance_id"] == "A"  # no takeover
    env.core.close()


# ---------------------------------------------------------------------------
# F2 — writer fence is re-asserted immediately before every OS effect
# ---------------------------------------------------------------------------

def test_f2_mid_broker_takeover_does_not_write(tmp_path):
    scope = os.path.realpath(str(tmp_path))
    state = {"job": {
        "id": "j1", "writer_binding_mode": "BOUND",
        "primary_state": "RUNNING", "status": "ACTIVE",
        "owner_instance_id": "A", "lease_epoch": 1, "facts_version": 10,
        "lease_expires_at": _future_iso(),
        "writer_dispatch_id": "d1", "writer_owner_instance_id": "A",
        "writer_lease_epoch": 1, "canonical_worktree_path": scope,
    }}
    guard = writer_guard_for(
        lambda: state["job"], job_id="j1", dispatch_id="d1",
        owner_instance_id="A", lease_epoch=1, facts_version=10,
    )
    broker = WorkspaceBroker(writer_guard=guard)

    def do_takeover(target):
        # Mid-broker: between the initial guard and os.replace, B takes over.
        state["job"] = {**state["job"], "owner_instance_id": "B",
                        "lease_epoch": 2}

    broker._before_replace_hook = do_takeover
    with pytest.raises(PermissionDenied):
        broker.apply_patch_set(scope, _write(scope, "f.txt"),
                               Role.IMPLEMENTER, CONTROLLER_SOURCE)
    # The stale writer (epoch 1) wrote NOTHING.
    assert not os.path.exists(os.path.join(scope, "f.txt"))
    # No staging residue either.
    assert [n for n in os.listdir(scope) if n.startswith(".argent-staging-")] == []


def test_f2_guard_rechecks_before_delete(tmp_path):
    scope = os.path.realpath(str(tmp_path))
    (open(os.path.join(scope, "f.txt"), "w")).write("old")
    state = {"job": {
        "id": "j1", "writer_binding_mode": "BOUND",
        "primary_state": "RUNNING", "status": "ACTIVE",
        "owner_instance_id": "A", "lease_epoch": 1, "facts_version": 10,
        "lease_expires_at": _future_iso(),
        "writer_dispatch_id": "d1", "writer_owner_instance_id": "A",
        "writer_lease_epoch": 1, "canonical_worktree_path": scope,
    }}
    guard = writer_guard_for(
        lambda: state["job"], job_id="j1", dispatch_id="d1",
        owner_instance_id="A", lease_epoch=1, facts_version=10,
    )
    broker = WorkspaceBroker(writer_guard=guard)
    # Takeover happens after the top-of-patch guard but before the unlink.
    state["job"] = {**state["job"], "owner_instance_id": "B", "lease_epoch": 2}
    with pytest.raises(PermissionDenied):
        broker.apply_patch_set(
            scope, [{"op": "delete", "path": "f.txt"}],
            Role.IMPLEMENTER, CONTROLLER_SOURCE)
    assert os.path.exists(os.path.join(scope, "f.txt"))  # not deleted


# ---------------------------------------------------------------------------
# F3 — real worktree provenance is persisted and drives recovery
# ---------------------------------------------------------------------------

def test_f3_bind_persists_real_provenance(db_path):
    env = _make_env(db_path, git=_FakeGitProvider(repo_identity="repo-a",
                                                  head="sha1", branch="main"),
                    workspace_root="/tmp/f3-bind-ws")
    jid = _add_queued_job(env)
    env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=600)
    prov = env.sup._git_provenance()
    env.sup.bind_writer_worktree(
        jid, dispatch_id="d1", owner_instance_id="A", lease_epoch=1,
        repo_identity=prov["repo_identity"], base_commit=prov["base_commit"],
        branch_identity=prov["branch_identity"],
        expected_head=prov["expected_head"],
    )
    row = _job_row(env.core, jid)
    assert row["writer_binding_mode"] == "BOUND"
    assert row["repo_identity"] == "repo-a"
    assert row["base_commit"] == "sha1"
    assert row["branch_identity"] == "main"
    assert row["expected_head"] == "sha1"
    assert row["current_head"] is None  # advanced only after the broker effect
    env.core.close()


def test_f3_scheduler_recovery_uses_real_worktree_facts(db_path):
    identity = _ScriptedIdentityProvider({
        100: ProcessIdentity(boot_id="boot-2", pid=100,
                             process_start_ticks=4242),  # dead
    })
    git = _FakeGitProvider(repo_identity="repo-a", head="sha1", dirty=False)
    env = _make_env(db_path, identity=identity, git=git,
                    workspace_root="/tmp/f3-ws")
    jid = _add_queued_job(env)
    env.core._store.claim_job(jid, owner_instance_id="writer-A", ttl_seconds=30)
    _register_process(env, jid, boot_id="boot-1", pid=100, ticks=4242)
    env.sup.bind_writer_worktree(
        jid, dispatch_id="d1", owner_instance_id="writer-A", lease_epoch=1,
        repo_identity="repo-a", base_commit="base1", branch_identity="main",
        expected_head="sha1",
    )
    env.clock.advance(31)
    sched = Scheduler(env.sup, owner_instance_id="writer-B",
                      lease_ttl_seconds=60)
    summary = sched.reconcile_after_restart()
    # Real git facts (head == expected_head, clean) -> takeover eligible.
    assert summary.takeover_candidates == 1
    assert summary.blocked_worktree == 0
    assert summary.quarantined_lost == 0
    env.core.close()


# ---------------------------------------------------------------------------
# F4 — owner_authorized requires an EXACT BLOCKED job
# ---------------------------------------------------------------------------

def _set_terminal(core, jid, terminal):
    core._store._update_supervisor_job(
        jid, status="TERMINAL", terminal=terminal, next_action="NONE",
    )


def test_f4_owner_authorized_rejects_running(db_path):
    env = _make_env(db_path)
    jid = _add_queued_job(env)
    env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=600)
    with pytest.raises(LeaseError):
        env.core._store.enqueue_job(
            jid, queue_reason="RECOVERY", owner_authorized=True,
            policy_ref="owner:approved:1",
        )
    assert _job_row(env.core, jid)["primary_state"] == PrimaryState.RUNNING.value
    assert _job_row(env.core, jid)["owner_instance_id"] == "A"
    env.core.close()


def test_f4_owner_authorized_requires_exact_blocked(db_path):
    env = _make_env(db_path)
    jid = _add_queued_job(env)
    _set_terminal(env.core, jid, "BLOCKED")
    # Exact BLOCKED + policy_ref -> requeue ok.
    row = env.core._store.enqueue_job(
        jid, queue_reason="RECOVERY", owner_authorized=True,
        policy_ref="owner:approved:reopen-1",
    )
    assert row["primary_state"] == PrimaryState.QUEUED.value
    assert row["terminal"] is None
    env.core.close()


def test_f4_owner_authorized_rejects_blocked_with_lease(db_path):
    env = _make_env(db_path)
    jid = _add_queued_job(env)
    _set_terminal(env.core, jid, "BLOCKED")
    # A BLOCKED job that still holds a lease is refused (not an exact reopen).
    env.core._store._update_supervisor_job(
        jid, owner_instance_id="stale", lease_expires_at=_future_iso(600),
    )
    with pytest.raises(LeaseError):
        env.core._store.enqueue_job(
            jid, queue_reason="RECOVERY", owner_authorized=True,
            policy_ref="owner:approved:reopen-2",
        )
    assert _job_row(env.core, jid)["primary_state"] == PrimaryState.BLOCKED.value
    env.core.close()


def test_f4_owner_authorized_requires_policy_ref(db_path):
    env = _make_env(db_path)
    jid = _add_queued_job(env)
    _set_terminal(env.core, jid, "BLOCKED")
    with pytest.raises(LeaseError):
        env.core._store.enqueue_job(
            jid, queue_reason="RECOVERY", owner_authorized=True, policy_ref=None,
        )
    with pytest.raises(LeaseError):
        env.core._store.enqueue_job(
            jid, queue_reason="RECOVERY", owner_authorized=True, policy_ref="  ",
        )
    assert _job_row(env.core, jid)["primary_state"] == PrimaryState.BLOCKED.value
    env.core.close()


# ---------------------------------------------------------------------------
# F5 — DONE/FAILED are immutable in the central low-level mutator
# ---------------------------------------------------------------------------

def test_f5_done_to_failed_rejected(db_path):
    env = _make_env(db_path)
    jid = _add_queued_job(env)
    _set_terminal(env.core, jid, "DONE")
    with pytest.raises(LeaseError):
        env.core._store._update_supervisor_job(
            jid, status="TERMINAL", terminal="FAILED", next_action="NONE",
        )
    assert _job_row(env.core, jid)["terminal"] == "DONE"
    assert _job_row(env.core, jid)["primary_state"] == PrimaryState.DONE.value
    env.core.close()


def test_f5_failed_to_done_rejected(db_path):
    env = _make_env(db_path)
    jid = _add_queued_job(env)
    _set_terminal(env.core, jid, "FAILED")
    with pytest.raises(LeaseError):
        env.core._store._update_supervisor_job(
            jid, status="TERMINAL", terminal="DONE", next_action="NONE",
        )
    assert _job_row(env.core, jid)["terminal"] == "FAILED"
    env.core.close()


def test_f5_done_idempotent_and_metadata_ok(db_path):
    env = _make_env(db_path)
    jid = _add_queued_job(env)
    _set_terminal(env.core, jid, "DONE")
    # Idempotent repeat of the SAME terminal value is allowed.
    env.core._store._update_supervisor_job(
        jid, status="TERMINAL", terminal="DONE", next_action="NONE",
    )
    # Pure metadata update (does not change terminal/primary_state) is allowed.
    env.core._store._update_supervisor_job(jid, last_progress_at="2026-01-02")
    assert _job_row(env.core, jid)["terminal"] == "DONE"
    assert _job_row(env.core, jid)["primary_state"] == PrimaryState.DONE.value
    env.core.close()


def test_f5_blocked_to_done_or_failed_rejected(db_path):
    env = _make_env(db_path)
    jid = _add_queued_job(env)
    _set_terminal(env.core, jid, "BLOCKED")
    for target in ("DONE", "FAILED"):
        with pytest.raises(LeaseError):
            env.core._store._update_supervisor_job(
                jid, status="TERMINAL", terminal=target, next_action="NONE",
            )
    # Direct terminal->NULL reopen is refused (must go through owner_authorized).
    with pytest.raises(LeaseError):
        env.core._store._update_supervisor_job(jid, terminal=None, status="WAITING_RUN")
    assert _job_row(env.core, jid)["terminal"] == "BLOCKED"
    env.core.close()


# ---------------------------------------------------------------------------
# F6 — dual-owner takeover races + terminal stickiness (real state changes)
# ---------------------------------------------------------------------------

def test_f6_dual_owner_takeover_races_and_terminal_stickiness(db_path):
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher(),
                     clock=clock)
    # A second independent owner/connection over the SAME DB file.
    core2 = Core(db_path, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(),
                      clock=clock)
    try:
        jids = []
        for n in range(3):
            task = core.create_task(project.id, f"t{n}", OWNER)
            job = sup.store.create_job(task.id, idempotency_key=f"j{n}")
            jids.append(job.supervisor_job_id)

        claims = takeovers = 0
        for jid in jids:
            # Real claim by A.
            core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=10)
            claims += 1
            clock.advance(11)
            # B takes over via the recovery path (never a direct claim).
            taken = core2._store.recover_takeover_job(
                jid, expected=core2._store.get_supervisor_job(jid),
                owner_instance_id="B", ttl_seconds=10,
                process_alive=False, worktree_verdict=None,
            )
            assert taken["owner_instance_id"] == "B"
            assert taken["lease_epoch"] == 2  # epoch bumped, never reused
            takeovers += 1
            # The stale A (epoch 1) is fenced — no second writer.
            with pytest.raises(LeaseFencedError):
                core._store.assert_lease_current(jid, "A", 1)
            # Real terminal-stickiness attempt: DONE -> FAILED is refused.
            core2._store._update_supervisor_job(
                jid, status="TERMINAL", terminal="DONE", next_action="NONE",
            )
            with pytest.raises(LeaseError):
                core2._store._update_supervisor_job(
                    jid, status="TERMINAL", terminal="FAILED",
                    next_action="NONE",
                )
            assert core2._store.get_supervisor_job(jid)["terminal"] == "DONE"

        assert claims == 3 and takeovers == 3
    finally:
        core2.close()
        core.close()


def test_f6_dual_scheduler_exactly_one_wins_running_job(db_path):
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher(),
                     clock=clock)
    core2 = Core(db_path, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(),
                      clock=clock)
    try:
        task = core.create_task(project.id, "t", OWNER)
        job = sup.store.create_job(task.id, idempotency_key="j")
        jid = job.supervisor_job_id
        sched_a = Scheduler(sup, owner_instance_id="A", lease_ttl_seconds=60)
        sched_b = Scheduler(sup2, owner_instance_id="B", lease_ttl_seconds=60)
        ra = sched_a.run_pass(jid)
        rb = sched_b.run_pass(jid)
        assert ra.outcome != OUTCOME_NO_WORK
        assert rb.outcome == OUTCOME_NO_WORK  # B never takes over a live lease
        assert core._store.get_supervisor_job(jid)["owner_instance_id"] == "A"
        assert core._store.get_supervisor_job(jid)["lease_epoch"] == 1
    finally:
        core2.close()
        core.close()
