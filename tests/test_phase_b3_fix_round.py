"""Phase B3 fix-round regression tests (Sol-Review verdict REJECT, 6 findings).

Offline and deterministic.  Covers, per finding:

* F1 — writer guard is bound to the full fencing token (stale owner after
  takeover denied; guard-installation failure denied fail-closed);
* F2 — restart reconciliation uses live process identity (4 cases: same /
  boot-change / PID-reuse / unreadable);
* F3 — writer/worktree binding primitive + guard fail-closed for bound jobs;
* F4 — CI wait requires an expected subject; READY with missing subject
  never wakes;
* F5 — a malformed observation backoffs one wait without aborting the pass;
* F6 — worktree classification is fail-closed (CLEANUP_PENDING only on exact
  binding + authoritatively terminal writer).

No network, no LLM, no real process.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from argent_core import Core, OWNER_SOURCE, Role
from argent_core.external_wait import (
    ExternalWaitManager,
    FakeExternalWaitAdapter,
    OBS_READY,
    WaitObservation,
    WaitSpec,
)
from argent_core.job_state import PrimaryState
from argent_core.models import LeaseFencedError, PermissionDenied
from argent_core.process_registry import (
    ProcessIdentity,
    ProcessRegistry,
)
from argent_core.supervisor import Supervisor, _canonical_json, _sha256
from argent_core.worktree import (
    V_AMBIGUOUS_WRITER,
    V_CLEANUP_PENDING,
    V_LOST,
    WorktreeBinding,
    WorktreeEvidence,
    classify_worktree_recovery,
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


# ---------------------------------------------------------------------------
# F1 — writer guard bound to the full fencing token
# ---------------------------------------------------------------------------

def test_f1_stale_writer_after_takeover_rejects(tmp_path):
    scope = os.path.realpath(str(tmp_path))
    state = {"job": {
        "id": "j1",
        "writer_binding_mode": "BOUND",
        "primary_state": "RUNNING",
        "status": "ACTIVE",
        "owner_instance_id": "A",
        "lease_epoch": 1,
        "facts_version": 10,
        "lease_expires_at": _future_iso(),
        "writer_dispatch_id": "d1",
        "writer_owner_instance_id": "A",
        "writer_lease_epoch": 1,
        "canonical_worktree_path": scope,
    }}
    guard = writer_guard_for(
        lambda: state["job"], job_id="j1", dispatch_id="d1",
        owner_instance_id="A", lease_epoch=1, facts_version=10,
    )
    # Takeover: owner B, epoch 2.  Dispatch + path still "match", but the lease
    # token no longer does — the guard must fail closed.
    state["job"] = {**state["job"], "owner_instance_id": "B", "lease_epoch": 2}
    broker = WorkspaceBroker(writer_guard=guard)
    with pytest.raises(PermissionDenied):
        broker.apply_patch_set(scope, _write(scope, "f.txt"), Role.IMPLEMENTER,
                               CONTROLLER_SOURCE)
    assert not os.path.exists(os.path.join(scope, "f.txt"))


def test_f1_guard_install_failure_denies_write(db_path, tmp_path):
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher(),
                     clock=clock, workspace_root=str(tmp_path))
    job = sup.store.create_job(task.id, idempotency_key="j")
    sup.store.claim_job(job.supervisor_job_id, owner_instance_id="A",
                        ttl_seconds=600)
    sup.set_lease_owner("A", 1)
    jid = job.supervisor_job_id
    scope = sup._workspace_root
    # Make the job BOUND so the binding step is a no-op and we exercise only
    # the guard-installation fail-closed path.
    core._store._update_supervisor_job(
        jid, writer_dispatch_id="d1", writer_owner_instance_id="A",
        writer_lease_epoch=1, canonical_worktree_path=scope,
        writer_binding_mode="BOUND",
    )
    patch_set = _write(scope, "f.txt")
    args_hash = _sha256(_canonical_json({
        "dispatch_id": "d1", "patch_set": patch_set,
        "workspace_root": scope,
    }))
    row = {"id": "action-1", "dispatch_id": "d1",
           "patch_set_json": json.dumps(patch_set), "args_hash": args_hash,
           "precondition_hash": None, "effect_hash": None}
    d = SimpleNamespace(id="d1", role=Role.IMPLEMENTER, task_id=task.id)

    calls = []

    class RecordingBroker(WorkspaceBroker):
        def apply_patch_set(self, *a, **k):
            calls.append(1)
            return super().apply_patch_set(*a, **k)

    sup._broker_factory = lambda: RecordingBroker()

    def _raise(*a, **k):
        raise RuntimeError("guard-install-boom")

    sup._writer_guard_for = _raise
    outcome = sup._invoke_broker_locked(
        core._store.get_supervisor_job(jid), "d1", d, row)
    assert outcome.action == "APPLY_PATCH_SET"
    assert outcome.status == "failed"
    assert outcome.detail == "writer_guard_install_failed"
    assert calls == []  # the broker was never invoked
    assert not os.path.exists(os.path.join(scope, "f.txt"))
    core.close()


# ---------------------------------------------------------------------------
# F2 — live process identity in restart reconciliation
# ---------------------------------------------------------------------------

class _ScriptedIdentityProvider:
    def __init__(self, identities):
        self._identities = identities

    def current(self, pid):
        return self._identities.get(
            pid, ProcessIdentity(boot_id=None, pid=pid, process_start_ticks=None))


def _f2_env(db_path, identity):
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher(),
                     clock=clock, process_identity_provider=identity)
    job = sup.store.create_job(task.id, idempotency_key="j")
    sup.store.claim_job(job.supervisor_job_id, owner_instance_id="A",
                        ttl_seconds=30)
    jid = job.supervisor_job_id
    reg = ProcessRegistry(core._store)
    reg.register(
        job_id=jid, dispatch_id=None,
        identity=ProcessIdentity(boot_id="boot-1", pid=100,
                                 process_start_ticks=4242),
    )
    clock.advance(31)  # expire A's lease
    return SimpleNamespace(core=core, sup=sup, clock=clock, jid=jid,
                           job=job, reg=reg)


def test_f2_same_identity_no_takeover(db_path):
    from argent_core.scheduler import Scheduler
    prov = _ScriptedIdentityProvider({
        100: ProcessIdentity(boot_id="boot-1", pid=100,
                             process_start_ticks=4242),
    })
    env = _f2_env(db_path, prov)
    sched = Scheduler(env.sup, owner_instance_id="B", lease_ttl_seconds=60)
    summary = sched.reconcile_after_restart()
    assert summary.takeover_candidates == 0
    assert summary.process_alive == 1
    row = env.core._store.get_supervisor_job(env.jid)
    assert row["owner_instance_id"] == "A"  # holder unchanged, no takeover
    assert row["primary_state"] == PrimaryState.RUNNING.value
    env.core.close()


def test_f2_boot_change_allows_takeover(db_path):
    from argent_core.scheduler import Scheduler
    prov = _ScriptedIdentityProvider({
        100: ProcessIdentity(boot_id="boot-2", pid=100,
                             process_start_ticks=4242),
    })
    env = _f2_env(db_path, prov)
    sched = Scheduler(env.sup, owner_instance_id="B", lease_ttl_seconds=60)
    summary = sched.reconcile_after_restart()
    assert summary.takeover_candidates == 1
    assert summary.process_alive == 0
    assert summary.quarantined_lost == 0
    env.core.close()


def test_f2_pid_reuse_is_not_same_process(db_path):
    from argent_core.scheduler import Scheduler
    prov = _ScriptedIdentityProvider({
        100: ProcessIdentity(boot_id="boot-1", pid=100,
                             process_start_ticks=9999),
    })
    env = _f2_env(db_path, prov)
    sched = Scheduler(env.sup, owner_instance_id="B", lease_ttl_seconds=60)
    summary = sched.reconcile_after_restart()
    assert summary.takeover_candidates == 1
    assert summary.process_alive == 0
    env.core.close()


def test_f2_unreadable_identity_lost_no_claim(db_path):
    from argent_core.scheduler import Scheduler
    prov = _ScriptedIdentityProvider({})  # unreadable -> UNKNOWN identity
    env = _f2_env(db_path, prov)
    sched = Scheduler(env.sup, owner_instance_id="B", lease_ttl_seconds=60)
    summary = sched.reconcile_after_restart()
    assert summary.quarantined_lost == 1
    assert summary.takeover_candidates == 0
    row = env.core._store.get_supervisor_job(env.jid)
    assert row["primary_state"] == PrimaryState.LOST.value
    env.core.close()


# ---------------------------------------------------------------------------
# F3 — writer/worktree binding primitive + guard fail-closed
# ---------------------------------------------------------------------------

def test_f3_bind_primitive_and_guard_enforce_bound_path(db_path, tmp_path):
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher(),
                     clock=clock, workspace_root=str(tmp_path))
    job = sup.store.create_job(task.id, idempotency_key="j")
    sup.store.claim_job(job.supervisor_job_id, owner_instance_id="A",
                        ttl_seconds=600)
    jid = job.supervisor_job_id
    scope = sup._workspace_root

    bound = sup.bind_writer_worktree(
        jid, dispatch_id="d1", owner_instance_id="A", lease_epoch=1,
        repo_identity="repo-a", base_commit="base1", branch_identity="main",
    )
    assert bound["writer_binding_mode"] == "BOUND"
    assert bound["writer_dispatch_id"] == "d1"
    assert bound["writer_owner_instance_id"] == "A"
    assert bound["writer_lease_epoch"] == 1
    assert bound["canonical_worktree_path"] == scope

    guard = writer_guard_for(
        lambda: core._store.get_supervisor_job(jid),
        job_id=jid, dispatch_id="d1", owner_instance_id="A", lease_epoch=1,
        facts_version=bound["facts_version"], now_iso=sup._now_iso,
    )
    broker = WorkspaceBroker(writer_guard=guard)
    res = broker.apply_patch_set(scope, _write(scope, "f.txt"),
                                 Role.IMPLEMENTER, CONTROLLER_SOURCE)
    assert not res.errors
    assert os.path.exists(os.path.join(scope, "f.txt"))

    # A foreign worktree path is denied.
    other = os.path.realpath(os.path.join(str(tmp_path), "..", "other"))
    os.makedirs(other, exist_ok=True)
    with pytest.raises(PermissionDenied):
        broker.apply_patch_set(other, _write(other, "g.txt"),
                               Role.IMPLEMENTER, CONTROLLER_SOURCE)
    assert not os.path.exists(os.path.join(other, "g.txt"))
    core.close()


def test_f3_bound_job_missing_binding_fails_closed(tmp_path):
    scope = os.path.realpath(str(tmp_path))
    job = {
        "id": "j1", "writer_binding_mode": "BOUND",
        "primary_state": "RUNNING", "status": "ACTIVE",
        "owner_instance_id": "A", "lease_epoch": 1, "facts_version": 1,
        "lease_expires_at": _future_iso(),
        "writer_dispatch_id": None, "writer_owner_instance_id": None,
        "writer_lease_epoch": 0, "canonical_worktree_path": None,
    }
    guard = writer_guard_for(
        lambda: job, job_id="j1", dispatch_id="d1", owner_instance_id="A",
        lease_epoch=1, facts_version=1,
    )
    with pytest.raises(PermissionDenied) as exc:
        guard(scope, Role.IMPLEMENTER, CONTROLLER_SOURCE)
    assert "binding_incomplete" in str(exc.value)


def test_f3_bind_stale_epoch_rejected(db_path, tmp_path):
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher(),
                     clock=clock, workspace_root=str(tmp_path))
    job = sup.store.create_job(task.id, idempotency_key="j")
    sup.store.claim_job(job.supervisor_job_id, owner_instance_id="A",
                        ttl_seconds=600)
    jid = job.supervisor_job_id
    with pytest.raises(LeaseFencedError):
        sup.bind_writer_worktree(
            jid, dispatch_id="d1", owner_instance_id="A", lease_epoch=9999,
        )
    row = core._store.get_supervisor_job(jid)
    assert row["writer_binding_mode"] is None  # no partial binding persisted
    core.close()


# ---------------------------------------------------------------------------
# F4 — CI wait requires expected subject; missing subject never wakes
# ---------------------------------------------------------------------------

def _f4_env(db_path):
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    sup = Supervisor(core, FakeRunStatusProvider(), clock=clock)
    task = core.create_task(project.id, "t", OWNER)
    job = sup.store.create_job(task.id, idempotency_key="j")
    claimed = core._store.claim_job(job.supervisor_job_id,
                                    owner_instance_id="A", ttl_seconds=600)
    mgr = ExternalWaitManager(
        core._store, adapters={"ci": FakeExternalWaitAdapter()}, clock=clock)
    return SimpleNamespace(core=core, sup=sup, clock=clock, mgr=mgr,
                           job=claimed, jid=claimed["id"])


def test_f4_ci_wait_requires_expected_subject(db_path):
    env = _f4_env(db_path)
    with pytest.raises(ValueError):
        env.mgr.enter_waiting_external(
            env.jid, spec=WaitSpec(kind="CI", provider="ci", ref="org/repo#run",
                                   expected_subject=None),
            owner_instance_id="A", lease_epoch=env.job["lease_epoch"],
        )
    assert env.core._store.list_external_waits(env.jid) == []
    env.core.close()


def test_f4_ready_with_missing_subject_no_wake(db_path):
    env = _f4_env(db_path)
    adapter = FakeExternalWaitAdapter()
    adapter.set_sticky("ci", "org/repo#run", WaitObservation(
        provider="ci", ref="org/repo#run", state=OBS_READY, subject=None,
        event_version=1))
    mgr = ExternalWaitManager(
        env.core._store, adapters={"ci": adapter}, clock=env.clock)
    mgr.enter_waiting_external(
        env.jid, spec=WaitSpec(kind="CI", provider="ci", ref="org/repo#run",
                               expected_subject="abc123"),
        owner_instance_id="A", lease_epoch=env.job["lease_epoch"],
    )
    env.clock.advance(61)
    results = mgr.check_due_waits()
    assert len(results) == 1
    assert results[0].outcome == "ignored"
    assert results[0].reason == "missing_subject"
    assert env.core._store.get_supervisor_job(env.jid)["primary_state"] == \
        PrimaryState.WAITING_EXTERNAL.value
    env.core.close()


# ---------------------------------------------------------------------------
# F5 — malformed observation isolated per wait, pass not aborted
# ---------------------------------------------------------------------------

def _f5_env(db_path):
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    sup = Supervisor(core, FakeRunStatusProvider(), clock=clock)
    task = core.create_task(project.id, "t", OWNER)
    job = sup.store.create_job(task.id, idempotency_key="j")
    claimed = core._store.claim_job(job.supervisor_job_id,
                                    owner_instance_id="A", ttl_seconds=600)
    return SimpleNamespace(core=core, sup=sup, clock=clock, job=claimed,
                           jid=claimed["id"])


def test_f5_malformed_event_version_does_not_abort_pass(db_path):
    env = _f5_env(db_path)
    adapter = FakeExternalWaitAdapter()
    adapter.script("ci", "ref-bad", [
        WaitObservation(provider="ci", ref="ref-bad", state=OBS_READY,
                        subject="abc123", event_version="malformed"),
    ])
    adapter.script("ci", "ref-good", [
        WaitObservation(provider="ci", ref="ref-good", state=OBS_READY,
                        subject="abc123", event_version=1),
    ])
    mgr = ExternalWaitManager(
        env.core._store, adapters={"ci": adapter}, clock=env.clock)
    mgr.enter_waiting_external(
        env.jid, spec=WaitSpec(kind="CI", provider="ci", ref="ref-bad",
                               expected_subject="abc123"),
        owner_instance_id="A", lease_epoch=env.job["lease_epoch"],
    )
    # Second job + wait (the one that should still wake despite the malformed
    # first observation).
    task2 = env.core.create_task(env.core.create_project("p2", OWNER).id, "t2",
                                 OWNER)
    job2 = env.sup.store.create_job(task2.id, idempotency_key="j2")
    claimed2 = env.core._store.claim_job(job2.supervisor_job_id,
                                         owner_instance_id="A", ttl_seconds=600)
    mgr.enter_waiting_external(
        claimed2["id"], spec=WaitSpec(kind="CI", provider="ci", ref="ref-good",
                                      expected_subject="abc123"),
        owner_instance_id="A", lease_epoch=claimed2["lease_epoch"],
    )
    env.clock.advance(61)

    results = mgr.check_due_waits()
    by_ref = {r.wait_id: r for r in results}
    outcomes = {r.reason for r in results}
    assert len(results) == 2
    assert "bad_event_version" in outcomes  # the malformed wait backoffs
    assert any(r.outcome == "woke" for r in results)  # the good wait still woke
    # The malformed wait's job stayed WAITING_EXTERNAL (no wake, no abort).
    bad_job = env.core._store.get_supervisor_job(env.jid)
    assert bad_job["primary_state"] == PrimaryState.WAITING_EXTERNAL.value
    good_job = env.core._store.get_supervisor_job(claimed2["id"])
    assert good_job["primary_state"] == PrimaryState.QUEUED.value
    env.core.close()


def test_f5_wrong_provider_observation_ignored(db_path):
    env = _f5_env(db_path)
    adapter = FakeExternalWaitAdapter()
    adapter.set_sticky("ci", "org/repo#run", WaitObservation(
        provider="evil", ref="org/repo#run", state=OBS_READY, subject="abc123",
        event_version=1))
    mgr = ExternalWaitManager(
        env.core._store, adapters={"ci": adapter}, clock=env.clock)
    mgr.enter_waiting_external(
        env.jid, spec=WaitSpec(kind="CI", provider="ci", ref="org/repo#run",
                               expected_subject="abc123"),
        owner_instance_id="A", lease_epoch=env.job["lease_epoch"],
    )
    env.clock.advance(61)
    results = mgr.check_due_waits()
    assert len(results) == 1
    assert results[0].outcome == "ignored"
    assert results[0].reason == "wrong_provider"
    assert env.core._store.get_supervisor_job(env.jid)["primary_state"] == \
        PrimaryState.WAITING_EXTERNAL.value
    env.core.close()


# ---------------------------------------------------------------------------
# F6 — worktree classification fail-closed
# ---------------------------------------------------------------------------

def test_f6_missing_repo_head_not_cleanup_pending():
    binding = WorktreeBinding(job_id="j1", canonical_worktree_path="/x")
    v = classify_worktree_recovery(
        binding, WorktreeEvidence(repo_identity=None, head=None, dirty=False))
    assert v.verdict == V_LOST
    assert v.verdict != V_CLEANUP_PENDING


def test_f6_correct_binding_terminal_cleanup_pending():
    binding = WorktreeBinding(
        job_id="j1", canonical_worktree_path="/x", repo_identity="repo-a",
        base_commit="base1", expected_head="sha1",
        writer_dispatch_id="d1", writer_owner_instance_id="A",
        writer_lease_epoch=1,
    )
    v = classify_worktree_recovery(
        binding, WorktreeEvidence(repo_identity="repo-a", head="sha1",
                                  dirty=False),
        writer_terminal=True)
    assert v.verdict == V_CLEANUP_PENDING
    # Without an authoritatively terminal writer -> ambiguous, never cleanup.
    v2 = classify_worktree_recovery(
        binding, WorktreeEvidence(repo_identity="repo-a", head="sha1",
                                  dirty=False),
        writer_terminal=None)
    assert v2.verdict == V_AMBIGUOUS_WRITER


def test_f6_dirty_without_ownership_ambiguous():
    binding = WorktreeBinding(
        job_id="j1", canonical_worktree_path="/x", repo_identity="repo-a",
        base_commit="base1", expected_head="sha1",
        # No writer ownership proof.
    )
    v = classify_worktree_recovery(
        binding, WorktreeEvidence(repo_identity="repo-a", dirty=True))
    assert v.verdict == V_AMBIGUOUS_WRITER
