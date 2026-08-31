"""Phase B4 — integrated E2E lifecycle test (mandatory acceptance path).

One controlled end-to-end path (FakeClock, Fake adapter/launcher, real SQLite
DB, real Supervisor/Scheduler instances, DB reopen) proving that B1 + B2 + B3
work together:

    QUEUED -> atomic claim -> RUNNING -> writer/process evidence bound
    -> action -> WAITING_EXTERNAL -> lease released -> persistent restart
    (DB reopen + new Supervisor) -> non-LLM wait check -> wake -> QUEUED
    -> re-claim with a new valid lease/epoch -> further action -> DONE.

Proofs asserted along the way:
* no old lease has effect after wake/takeover;
* no poll agent stays active during the wait;
* the wait survives reopen;
* process/writer evidence is consistent across restart;
* DONE is sticky.
"""

from __future__ import annotations

import base64
import os
import subprocess

import pytest

from argent_core import Core, OWNER_SOURCE, Role
from argent_core.external_wait import (
    ExternalWaitManager,
    FakeExternalWaitAdapter,
    OBS_READY,
    WaitObservation,
    WaitSpec,
)
from argent_core.job_state import PrimaryState, QueueReason
from argent_core.models import LeaseError, LeaseFencedError
from argent_core.process_registry import ProcessIdentity, ProcessRegistry
from argent_core.sandbox_runner import SandboxResult
from argent_core.scheduler import Scheduler
from argent_core.supervisor import Supervisor, SupervisorLoop
from mock_runtime import build_output
from mock_supervisor_runtime import (
    AutoRunStatusProvider,
    FakeClock,
    FakeRunLauncher,
    FakeWaiter,
)

OWNER = OWNER_SOURCE


def _implementer_with_patch(role, task_id, dispatch_id):
    """Result builder: standard outputs, but the implementer writes a real
    patch so the writer/worktree binding is exercised (not a silent no-op)."""
    out = build_output(role, task_id, dispatch_id)
    if role is Role.IMPLEMENTER:
        out["patch_set"] = [{
            "op": "write",
            "path": "src/new.py",
            "content": base64.b64encode(b"x = 1\n").decode(),
        }]
    return out


def _fake_run_tests(workspace, pytest_args=None, limits=None):
    return SandboxResult(exit_code=0, stdout_bounded="", stderr_bounded="",
                         timed_out=False, wall_seconds=0.0)


def _make_supervisor(core, clock, ws):
    prov = AutoRunStatusProvider(core, result_builder=_implementer_with_patch)
    return Supervisor(core, prov, FakeRunLauncher(), clock=clock,
                      workspace_root=str(ws), run_tests_fn=_fake_run_tests)


def _git_init(ws) -> None:
    """Init a real git repo so the writer binding persists REAL provenance."""
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    for args in (("init", "-q", "-b", "main"), ("add", "-A"),
                 ("commit", "-q", "-m", "init")):
        subprocess.run(["git", "-C", str(ws), *args], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)


def test_e2e_durable_lifecycle_queue_wait_restart_done(tmp_path):
    clock = FakeClock()
    db = str(tmp_path / "e2e.db")
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True, exist_ok=True)
    (ws / "tests").mkdir(parents=True, exist_ok=True)
    (ws / "src" / "module.py").write_text("# stub\n")
    _git_init(ws)

    # ---- Phase 1: QUEUED -> atomic claim -> RUNNING ----------------------
    core = Core(db, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    core.start_task_run(task.id, OWNER)
    sup = _make_supervisor(core, clock, ws)
    job = sup.store.create_job(task.id, idempotency_key="job-1")
    jid = job.supervisor_job_id
    assert core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.QUEUED.value

    sched = Scheduler(sup, owner_instance_id="instance-A", lease_ttl_seconds=600)
    r = sched.run_pass(jid)
    assert r.job_id == jid
    row = core._store.get_supervisor_job(jid)
    assert row["primary_state"] == PrimaryState.RUNNING.value
    assert row["status"] == "ACTIVE"
    assert row["owner_instance_id"] == "instance-A"
    assert row["lease_epoch"] == 1
    assert row["lease_expires_at"] is not None

    # ---- Writer / process evidence bound ---------------------------------
    reg = ProcessRegistry(core._store)
    reg.register(job_id=jid, dispatch_id=None,
                 identity=ProcessIdentity(boot_id="boot-1", pid=100,
                                          process_start_ticks=4242))
    assert len(core._store.list_process_registrations(jid)) == 1

    # ---- Action (START_ROLE) ---------------------------------------------
    sup.set_lease_owner("instance-A", 1)
    decision = sup.reconcile(jid)
    sup.perform_next_safe_action_if_required(decision)
    assert core.queries.get_active_role_run(task.id) is not None

    # ---- WAITING_EXTERNAL (lease + agent released) -----------------------
    adapter = FakeExternalWaitAdapter()
    adapter.script("ci", "org/repo#run", [
        WaitObservation(provider="ci", ref="org/repo#run", state=OBS_READY,
                        subject="abc123", event_version=1),
    ])
    mgr = ExternalWaitManager(core._store, adapters={"ci": adapter}, clock=clock)
    updated = mgr.enter_waiting_external(
        jid,
        spec=WaitSpec(kind="CI", provider="ci", ref="org/repo#run",
                      expected_subject="abc123"),
        owner_instance_id="instance-A", lease_epoch=1,
    )
    assert updated["primary_state"] == PrimaryState.WAITING_EXTERNAL.value
    assert updated["owner_instance_id"] is None
    assert updated["lease_expires_at"] is None
    assert len(core._store.list_external_waits(jid)) == 1
    dispatches_before_wait = len(core._store.list_dispatches(task.id))

    # ---- Persistent restart (DB reopen + new Supervisor instance) --------
    core.close()
    core2 = Core(db, clock=clock)
    sup2 = _make_supervisor(core2, clock, ws)
    # Wait survives reopen; old lease is gone.
    row2 = core2._store.get_supervisor_job(jid)
    assert row2["primary_state"] == PrimaryState.WAITING_EXTERNAL.value
    assert row2["owner_instance_id"] is None
    assert row2["lease_expires_at"] is None
    assert len(core2._store.list_external_waits(jid)) == 1
    # Process evidence still consistent across restart.
    assert len(core2._store.list_process_registrations(jid)) == 1

    # ---- Non-LLM wait check -> wake -> QUEUED ----------------------------
    clock.advance(61)
    mgr2 = ExternalWaitManager(core2._store, adapters={"ci": adapter}, clock=clock)
    results = mgr2.check_due_waits()
    assert len(results) == 1 and results[0].outcome == "woke"
    assert results[0].queue_reason == QueueReason.WAIT_EVENT.value
    row3 = core2._store.get_supervisor_job(jid)
    assert row3["primary_state"] == PrimaryState.QUEUED.value
    assert row3["queue_reason"] == QueueReason.WAIT_EVENT.value
    # No poll agent: no NEW dispatch was created during the wait.
    assert len(core2._store.list_dispatches(task.id)) == dispatches_before_wait

    # ---- Re-claim with a new valid lease/epoch ---------------------------
    sched2 = Scheduler(sup2, owner_instance_id="instance-A", lease_ttl_seconds=600)
    r2 = sched2.run_pass(jid)
    assert r2.job_id == jid
    row4 = core2._store.get_supervisor_job(jid)
    assert row4["primary_state"] == PrimaryState.RUNNING.value
    assert row4["lease_epoch"] == 2  # new epoch (never the old lease)
    # The old (instance-A, epoch 1) holder is fenced.
    with pytest.raises(LeaseFencedError):
        core2._store.assert_lease_current(jid, "instance-A", 1)
    assert core2._store.lease_is_current(jid, "instance-A", 2) is True

    # ---- Further action -> DONE ------------------------------------------
    loop = SupervisorLoop(sup2, waiter=FakeWaiter(clock),
                          owner_instance_id="instance-A",
                          lease_ttl_seconds=600)
    for _ in range(200):
        loop.run_once(jid)
        st = sup2.store.get_job(jid)
        if st.terminal is not None:
            break
    final = sup2.store.get_job(jid)
    assert final.terminal == "DONE"
    frow = core2._store.get_supervisor_job(jid)
    assert frow["primary_state"] == PrimaryState.DONE.value

    # Writer evidence was bound by the real implementer step and is consistent.
    assert frow["writer_binding_mode"] == "BOUND"
    assert frow["canonical_worktree_path"] == str(ws.resolve())
    assert frow["writer_dispatch_id"] is not None
    assert frow["writer_lease_epoch"] >= 2
    # F3: the BOUND row carries REAL git provenance, not NULL.
    assert frow["repo_identity"] == str(ws.resolve())
    assert frow["base_commit"] is not None and len(frow["base_commit"]) == 40
    assert frow["branch_identity"] == "main"
    assert frow["expected_head"] == frow["base_commit"]
    assert frow["current_head"] == frow["base_commit"]
    assert (ws / "src" / "new.py").exists()  # the patch was actually applied

    # ---- DONE sticky ------------------------------------------------------
    with pytest.raises(LeaseError):
        core2._store.claim_job(jid, owner_instance_id="instance-B", ttl_seconds=60)
    assert core2._store.claim_next_job(owner_instance_id="B", ttl_seconds=60) is None
    assert core2._store.get_supervisor_job(jid)["terminal"] == "DONE"
    core2.close()
