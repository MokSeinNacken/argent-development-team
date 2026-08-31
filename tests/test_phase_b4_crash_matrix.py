"""Phase B4 — systematic Failure/Crash Matrix (27 cases, A–F).

Integrated acceptance proof that B1 (queue/lease/fencing) + B2 (scheduler/
recovery/action-journal) + B3 (external wait / process registry / worktree)
behave consistently together.  Each test below maps 1:1 to a row of the
Failure/Crash Matrix in ``docs/PHASE_B_ACCEPTANCE.md``.

All time is controlled via ``FakeClock``; there is no sleep, no network, no
real process and no LLM.  Reopens use a fresh ``Core``/``Supervisor`` over the
same DB file (no in-memory cache authority).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from argent_core import Core, OWNER_SOURCE, Role
from argent_core.external_wait import (
    ExternalWaitManager,
    FakeExternalWaitAdapter,
    OBS_PENDING,
    OBS_READY,
    WaitObservation,
    WaitSpec,
)
from argent_core.job_state import PrimaryState, QueueReason
from argent_core.models import LeaseError, LeaseFencedError
from argent_core.process_registry import (
    IDENTITY_BOOT_CHANGED,
    IDENTITY_PID_REUSE,
    IDENTITY_SAME,
    PROCESS_STATUS_UNKNOWN,
    ProcessIdentity,
    ProcessRegistry,
)
from argent_core.scheduler import OUTCOME_NO_WORK, Scheduler
from argent_core.supervisor import ReconcileAction, Supervisor, _canonical_json, _sha256
from argent_core.worktree import (
    V_AMBIGUOUS_WRITER,
    V_BLOCKED_DIVERGED,
    V_CLEANUP_PENDING,
    V_KEEP_DIRTY,
    V_LOST,
    WorktreeBinding,
    WorktreeEvidence,
    classify_worktree_recovery,
)
from mock_supervisor_runtime import (
    FakeClock,
    FakeRunLauncher,
    FakeRunStatusProvider,
)

OWNER = OWNER_SOURCE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_env(db_path, clock=None, *, start_run=False, identity=None):
    clock = clock or FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    prov = FakeRunStatusProvider()
    sup = Supervisor(
        core, prov, FakeRunLauncher(), clock=clock,
        process_identity_provider=identity,
    )
    env = SimpleNamespace(core=core, project=project, sup=sup, prov=prov,
                          clock=clock)
    if start_run:
        env.task = core.create_task(project.id, "t", OWNER)
        core.start_task_run(env.task.id, OWNER)
    return env


def add_queued_job(env, title="job", *, idem=None, start_run=True):
    if start_run:
        task = env.core.create_task(env.project.id, title, OWNER)
        env.core.start_task_run(task.id, OWNER)
    else:
        task = env.core.create_task(env.project.id, title, OWNER)
    job = env.sup.store.create_job(task.id, idempotency_key=idem or f"job-{task.id}")
    env.core._store.enqueue_job(job.supervisor_job_id, queue_reason="NEW")
    return job.supervisor_job_id


def job_row(core, job_id):
    return core._store.get_supervisor_job(job_id)


def set_terminal(core, job_id, terminal):
    core._store._update_supervisor_job(
        job_id, status="TERMINAL", terminal=terminal, next_action="NONE"
    )


class _ScriptedIdentityProvider:
    def __init__(self, identities):
        self._identities = identities

    def current(self, pid):
        return self._identities.get(
            pid, ProcessIdentity(boot_id=None, pid=pid, process_start_ticks=None))


def _start_role_key(env, jid):
    sup = env.sup
    task_id = env.task.id
    f = env.core.workflow_frontier(task_id, sup.controller_source)
    role = f.expected_role
    cycle, pos, attempt = sup._frontier_attempt(task_id, f)
    key = (f"supervisor:{jid}:cycle:{cycle}:pos:{pos}:attempt:{attempt}:"
           f"start-role")
    args_hash = _sha256(_canonical_json({
        "task_id": task_id, "role": role.value, "source": sup.controller_source,
    }))
    return key, args_hash


def wait_spec(**kw):
    base = dict(kind="CI", provider="ci", ref="org/repo#run",
                expected_subject="abc123")
    base.update(kw)
    return WaitSpec(**base)


def make_manager(env, adapter=None):
    return ExternalWaitManager(
        env.core._store, adapters={"ci": adapter or FakeExternalWaitAdapter()},
        clock=env.clock,
    )


# ---------------------------------------------------------------------------
# A. Queue / Claim (cases 1–3)
# ---------------------------------------------------------------------------

def test_case01_crash_before_claim_stays_queued(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    jid = add_queued_job(env, "job", start_run=False)
    assert job_row(env.core, jid)["primary_state"] == PrimaryState.QUEUED.value
    env.core.close()  # crash before any claim

    core2 = Core(db_path, clock=clock)
    try:
        row = job_row(core2, jid)
        assert row["primary_state"] == PrimaryState.QUEUED.value
        assert row["owner_instance_id"] is None
        assert row["lease_epoch"] == 0
        # Still normally claimable after restart.
        claimed = core2._store.claim_job(jid, owner_instance_id="A", ttl_seconds=60)
        assert claimed["primary_state"] == PrimaryState.RUNNING.value
    finally:
        core2.close()


def test_case02_crash_after_claim_lease_persisted_reconciles(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    jid = add_queued_job(env, "job", start_run=False)
    env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=300)
    env.core.close()  # crash right after claim

    core2 = Core(db_path, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(),
                      clock=clock)
    try:
        # Lease + epoch persisted across the crash.
        row = job_row(core2, jid)
        assert row["primary_state"] == PrimaryState.RUNNING.value
        assert row["owner_instance_id"] == "A"
        assert row["lease_epoch"] == 1
        assert row["lease_expires_at"] is not None
        # Same owner rebinds; a different owner must NOT take over a valid lease.
        sched_same = Scheduler(sup2, owner_instance_id="A", lease_ttl_seconds=60)
        assert sched_same.reconcile_after_restart().rebound == 1
        sched_other = Scheduler(sup2, owner_instance_id="B", lease_ttl_seconds=60)
        summary = sched_other.reconcile_after_restart()
        assert summary.foreign_lease_kept == 1
        assert summary.takeover_candidates == 0
    finally:
        core2.close()


def test_case03_two_supervisors_exactly_one_wins(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    jid = add_queued_job(env, "job", start_run=False)

    core2 = Core(db_path, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(),
                      clock=clock)
    sched_a = Scheduler(env.sup, owner_instance_id="instance-A", lease_ttl_seconds=60)
    sched_b = Scheduler(sup2, owner_instance_id="instance-B", lease_ttl_seconds=60)
    try:
        ra = sched_a.run_pass(jid)
        rb = sched_b.run_pass(jid)
        assert ra.outcome != OUTCOME_NO_WORK
        assert rb.outcome == OUTCOME_NO_WORK
        row = job_row(env.core, jid)
        assert row["owner_instance_id"] == "instance-A"
        assert row["lease_epoch"] == 1
    finally:
        core2.close()
        env.core.close()


# ---------------------------------------------------------------------------
# B. Action Journal (cases 4–7)
# ---------------------------------------------------------------------------

def _job_for_env_task(env, idem="j-main"):
    """Create a supervisor job for ``env.task`` (which already has a run)."""
    job = env.sup.store.create_job(env.task.id, idempotency_key=idem)
    return job.supervisor_job_id


def test_case04_crash_before_effect_replays_safely(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock, start_run=True)
    jid = _job_for_env_task(env)
    env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=3600)
    env.sup.set_lease_owner("A", 1)
    key, args_hash = _start_role_key(env, jid)
    row, outcome = env.sup._begin_action(key, "START_ROLE",
                                         job_row(env.core, jid), None, args_hash)
    assert outcome == "new" and row["status"] == "RUNNING"
    assert env.core.queries.get_active_role_run(env.task.id) is None
    env.core.close()  # crash BEFORE effect

    core2 = Core(db_path, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(),
                      clock=clock)
    try:
        sup2.set_lease_owner("A", 1)
        dec = sup2.reconcile(jid)
        assert dec.action is ReconcileAction.START_ROLE
        sup2.perform_next_safe_action_if_required(dec)
        assert len(core2.queries.list_role_runs(env.task.id)) == 1
    finally:
        core2.close()


def test_case05_crash_after_effect_before_finalize_no_double(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock, start_run=True)
    jid = _job_for_env_task(env)
    env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=3600)
    env.sup.set_lease_owner("A", 1)
    key, args_hash = _start_role_key(env, jid)
    row, _ = env.sup._begin_action(key, "START_ROLE",
                                   job_row(env.core, jid), None, args_hash)
    # Effect applied, journal left RUNNING (crash before finalize).
    env.core.start_role(env.task.id, Role.LEAD, env.sup.controller_source,
                        idempotency_key=key)
    assert row["status"] == "RUNNING"
    env.core.close()

    core2 = Core(db_path, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(),
                      clock=clock)
    try:
        sup2.set_lease_owner("A", 1)
        dec = sup2.reconcile(jid)
        assert dec.action is not ReconcileAction.START_ROLE
        sup2.perform_next_safe_action_if_required(dec)
        assert len(core2.queries.list_role_runs(env.task.id)) == 1  # no double
    finally:
        core2.close()


def test_case06_crash_after_finalize_not_repeated(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock, start_run=True)
    jid = _job_for_env_task(env)
    env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=3600)
    env.sup.set_lease_owner("A", 1)
    key, args_hash = _start_role_key(env, jid)
    row, _ = env.sup._begin_action(key, "START_ROLE",
                                   job_row(env.core, jid), None, args_hash)
    env.core.start_role(env.task.id, Role.LEAD, env.sup.controller_source,
                        idempotency_key=key)
    env.sup._finish_action(row["id"], "SUCCEEDED")
    env.core.close()

    core2 = Core(db_path, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(),
                      clock=clock)
    try:
        sup2.set_lease_owner("A", 1)
        dec = sup2.reconcile(jid)
        assert dec.action is not ReconcileAction.START_ROLE
        sup2.perform_next_safe_action_if_required(dec)
        starts = [a for a in core2._store.list_supervisor_actions(jid)
                  if a["action_type"] == "START_ROLE"]
        assert len(starts) == 1 and starts[0]["status"] == "SUCCEEDED"
    finally:
        core2.close()


def test_case07_takeover_between_fence_check_and_effect_fences_stale(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env, "job", start_run=False)
    env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=30)
    env.sup.set_lease_owner("A", 1)
    decision = env.sup.reconcile(jid)
    assert decision.owner_instance_id == "A" and decision.lease_epoch == 1
    env.clock.advance(31)  # expire A
    # F1: RUNNING takeover goes through the recovery path (never claim_job).
    env.core._store.recover_takeover_job(
        jid, expected=job_row(env.core, jid), owner_instance_id="B",
        ttl_seconds=30, process_alive=False, worktree_verdict=None,
    )
    facts_after = job_row(env.core, jid)["facts_version"]
    # Stale owner A (epoch 1) executes its old decision -> fenced, no write.
    with pytest.raises(LeaseFencedError):
        env.sup.perform_next_safe_action_if_required(decision)
    assert job_row(env.core, jid)["facts_version"] == facts_after
    assert job_row(env.core, jid)["owner_instance_id"] == "B"
    env.core.close()


# ---------------------------------------------------------------------------
# C. Writer / Worktree (cases 8–12)
# ---------------------------------------------------------------------------

def _register_process(env, jid, *, boot_id, pid, ticks, dispatch_id=None,
                      status=None):
    reg = ProcessRegistry(env.core._store)
    ident = ProcessIdentity(boot_id=boot_id, pid=pid, process_start_ticks=ticks)
    return reg.register(job_id=jid, dispatch_id=dispatch_id, identity=ident)


def test_case08_writer_running_supervisor_dies_no_second_writer(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock,
                   identity=_ScriptedIdentityProvider({
                       100: ProcessIdentity(boot_id="boot-1", pid=100,
                                            process_start_ticks=4242),
                   }))
    jid = add_queued_job(env, "job", start_run=False)
    env.core._store.claim_job(jid, owner_instance_id="writer-A", ttl_seconds=30)
    _register_process(env, jid, boot_id="boot-1", pid=100, ticks=4242)
    clock.advance(31)  # lease expired, but live process identity still SAME
    sched_b = Scheduler(env.sup, owner_instance_id="writer-B", lease_ttl_seconds=60)
    summary = sched_b.reconcile_after_restart()
    # Process still alive -> NO takeover, NO second writer.
    assert summary.process_alive == 1
    assert summary.takeover_candidates == 0
    # F1: run_pass must NOT switch to epoch 2 either (no second owner).
    r = sched_b.run_pass(jid)
    assert r.outcome == OUTCOME_NO_WORK
    row = job_row(env.core, jid)
    assert row["owner_instance_id"] == "writer-A"
    assert row["lease_epoch"] == 1
    env.core.close()


def test_case09_writer_status_unknown_lost_fail_closed(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock, identity=_ScriptedIdentityProvider({}))
    jid = add_queued_job(env, "job", start_run=False)
    env.core._store.claim_job(jid, owner_instance_id="writer-A", ttl_seconds=30)
    _register_process(env, jid, boot_id="boot-1", pid=100, ticks=4242)
    clock.advance(31)  # expired + unreadable live identity -> unknown
    sched_b = Scheduler(env.sup, owner_instance_id="writer-B", lease_ttl_seconds=60)
    summary = sched_b.reconcile_after_restart()
    assert summary.quarantined_lost == 1
    assert summary.takeover_candidates == 0
    row = job_row(env.core, jid)
    assert row["primary_state"] == PrimaryState.LOST.value
    with pytest.raises(LeaseError) as exc:
        env.core._store.claim_job(jid, owner_instance_id="writer-B", ttl_seconds=60)
    assert "not_claimable:LOST" in str(exc.value)
    env.core.close()


def _owned_binding():
    return WorktreeBinding(
        job_id="j1", canonical_worktree_path="/x", repo_identity="repo-a",
        base_commit="base1", expected_head="sha1",
        writer_dispatch_id="d1", writer_owner_instance_id="A",
        writer_lease_epoch=1,
    )


def test_case10_writer_terminal_worktree_consistent_recovery(db_path):
    binding = _owned_binding()
    v = classify_worktree_recovery(
        binding,
        WorktreeEvidence(repo_identity="repo-a", head="sha1", dirty=False),
        writer_terminal=True)
    assert v.verdict == V_CLEANUP_PENDING


def test_case11_worktree_dirty_job_owned_kept(db_path):
    binding = _owned_binding()
    v = classify_worktree_recovery(
        binding,
        WorktreeEvidence(repo_identity="repo-a", head=None, dirty=True))
    assert v.verdict == V_KEEP_DIRTY


def test_case12_worktree_divergent_foreign_ambiguous_blocked(db_path):
    owned = _owned_binding()
    # Foreign repo -> LOST (never touch).
    v = classify_worktree_recovery(
        owned, WorktreeEvidence(repo_identity="repo-b", head="sha1", dirty=False))
    assert v.verdict == V_LOST
    # Divergent HEAD -> BLOCKED (never overwrite).
    v = classify_worktree_recovery(
        owned, WorktreeEvidence(repo_identity="repo-a", head="sha2", dirty=False))
    assert v.verdict == V_BLOCKED_DIVERGED
    # Dirty WITHOUT ownership proof -> ambiguous (never auto-delete).
    unowned = WorktreeBinding(job_id="j2", canonical_worktree_path="/y",
                              repo_identity="repo-a", base_commit="base1",
                              expected_head="sha1")
    v = classify_worktree_recovery(
        unowned, WorktreeEvidence(repo_identity="repo-a", dirty=True))
    assert v.verdict == V_AMBIGUOUS_WRITER


# ---------------------------------------------------------------------------
# D. Process identity (cases 13–16)
# ---------------------------------------------------------------------------

def test_case13_same_identity_same_process(db_path):
    reg = {"boot_id": "boot-1", "pid": 100, "process_start_ticks": 4242}
    same = ProcessIdentity(boot_id="boot-1", pid=100, process_start_ticks=4242)
    assert ProcessRegistry.classify_identity(reg, same) == IDENTITY_SAME


def test_case14_same_pid_other_ticks_pid_reuse(db_path):
    reg = {"boot_id": "boot-1", "pid": 100, "process_start_ticks": 4242}
    reuse = ProcessIdentity(boot_id="boot-1", pid=100, process_start_ticks=9999)
    assert ProcessRegistry.classify_identity(reg, reuse) == IDENTITY_PID_REUSE


def test_case15_other_boot_id_not_alive(db_path):
    reg = {"boot_id": "boot-1", "pid": 100, "process_start_ticks": 4242}
    boot = ProcessIdentity(boot_id="boot-2", pid=100, process_start_ticks=4242)
    assert ProcessRegistry.classify_identity(reg, boot) == IDENTITY_BOOT_CHANGED


def test_case16_unreadable_evidence_unknown_not_dead(db_path):
    env = make_env(db_path)
    task = env.core.create_task(env.project.id, "t", OWNER)
    job = env.sup.store.create_job(task.id, idempotency_key="j")
    reg = ProcessRegistry(env.core._store)
    # UNKNOWN identity (no boot_id / no ticks) -> persisted as UNKNOWN.
    ident = ProcessIdentity(boot_id=None, pid=100, process_start_ticks=None)
    row = reg.register(job_id=job.supervisor_job_id, dispatch_id=None,
                       identity=ident)
    rec = env.core._store.get_process_registration(row["process_id"])
    assert rec["status"] == PROCESS_STATUS_UNKNOWN
    assert rec["boot_id"] is None and rec["process_start_ticks"] is None
    # Never "surely dead" -> fail-closed.
    assert ProcessRegistry.is_terminally_dead(rec) is False
    env.core.close()


# ---------------------------------------------------------------------------
# E. External Wait (cases 17–23)
# ---------------------------------------------------------------------------

def _running_with_wait(env, *, owner="A", ttl=600, adapter=None):
    job = env.sup.store.create_job(
        env.core.create_task(env.project.id, "t", OWNER).id,
        idempotency_key=f"j-{len(env.core._store.list_supervisor_jobs())}")
    claimed = env.core._store.claim_job(job.supervisor_job_id,
                                        owner_instance_id=owner, ttl_seconds=ttl)
    return claimed


def test_case17_crash_before_wait_commit_no_half_state(db_path):
    env = make_env(db_path)
    job = _running_with_wait(env, owner="A", ttl=600)
    jid = job["id"]
    mgr = make_manager(env)
    # A crash/fence-loss at commit time is simulated by a stale epoch -> the
    # whole transition (wait insert + state + lease release) rolls back.
    with pytest.raises(LeaseError):
        mgr.enter_waiting_external(
            jid, spec=wait_spec(), owner_instance_id="A", lease_epoch=9999)
    row = job_row(env.core, jid)
    assert row["primary_state"] == PrimaryState.RUNNING.value
    assert row["owner_instance_id"] == "A"
    assert row["lease_expires_at"] is not None
    assert env.core._store.list_external_waits(jid) == []  # no half-wait
    env.core.close()


def test_case18_crash_after_wait_commit_wait_persists_no_llm(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    job = _running_with_wait(env, owner="A", ttl=600)
    jid = job["id"]
    mgr = make_manager(env)
    mgr.enter_waiting_external(
        jid, spec=wait_spec(), owner_instance_id="A", lease_epoch=job["lease_epoch"])
    env.core.close()  # crash right after wait commit

    core2 = Core(db_path, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), clock=clock)
    try:
        waits = core2._store.list_external_waits(jid)
        assert len(waits) == 1 and waits[0]["terminal_observed_at"] is None
        assert job_row(core2, jid)["primary_state"] == \
            PrimaryState.WAITING_EXTERNAL.value
        # No dispatch was ever created (the wait is a pure persisted fact).
        assert core2._store.list_dispatches(
            core2._store.get_supervisor_job(jid)["task_id"]) == []
    finally:
        core2.close()


def test_case19_restart_during_wait_wait_remains(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    job = _running_with_wait(env, owner="A", ttl=600)
    jid = job["id"]
    adapter = FakeExternalWaitAdapter()
    adapter.set_sticky("ci", "org/repo#run", WaitObservation(
        provider="ci", ref="org/repo#run", state=OBS_PENDING, event_version=0))
    mgr = make_manager(env, adapter)
    mgr.enter_waiting_external(
        jid, spec=wait_spec(), owner_instance_id="A", lease_epoch=job["lease_epoch"])
    before = env.core._store.list_external_waits(jid)[0]
    env.core.close()

    core2 = Core(db_path, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), clock=clock)
    try:
        wait = core2._store.list_external_waits(jid)[0]
        assert wait["next_check_at"] == before["next_check_at"]
        assert wait["deadline_at"] == before["deadline_at"]
        assert job_row(core2, jid)["primary_state"] == \
            PrimaryState.WAITING_EXTERNAL.value
        clock.advance(61)
        mgr2 = ExternalWaitManager(core2._store, adapters={"ci": adapter},
                                   clock=clock)
        assert mgr2.check_due_waits()[0].outcome == "pending"
    finally:
        core2.close()


def test_case20_duplicate_event_max_one_wake(db_path):
    env = make_env(db_path)
    job = _running_with_wait(env, owner="A", ttl=600)
    jid = job["id"]
    adapter = FakeExternalWaitAdapter()
    adapter.script("ci", "org/repo#run", [
        WaitObservation(provider="ci", ref="org/repo#run", state=OBS_READY,
                        subject="abc123", event_version=5),
        WaitObservation(provider="ci", ref="org/repo#run", state=OBS_READY,
                        subject="abc123", event_version=5),
    ])
    mgr = make_manager(env, adapter)
    mgr.enter_waiting_external(
        jid, spec=wait_spec(), owner_instance_id="A", lease_epoch=job["lease_epoch"])
    env.clock.advance(61)
    r1 = mgr.check_due_waits()
    assert len([x for x in r1 if x.outcome == "woke"]) == 1
    assert [x for x in mgr.check_due_waits() if x.outcome == "woke"] == []
    assert job_row(env.core, jid)["primary_state"] == PrimaryState.QUEUED.value
    env.core.close()


def test_case21_stale_wrong_subject_sha_no_effect(db_path):
    env = make_env(db_path)
    job = _running_with_wait(env, owner="A", ttl=600)
    jid = job["id"]
    mgr = make_manager(env)
    mgr.enter_waiting_external(
        jid, spec=wait_spec(), owner_instance_id="A", lease_epoch=job["lease_epoch"])
    # Stale version.
    s1 = FakeExternalWaitAdapter()
    s1.set_sticky("ci", "org/repo#run", WaitObservation(
        provider="ci", ref="org/repo#run", state=OBS_READY, subject="abc123",
        event_version=0))
    env.clock.advance(61)
    assert make_manager(env, s1).check_due_waits()[0].reason == "stale_version"
    # Wrong subject (stale CI SHA).
    s2 = FakeExternalWaitAdapter()
    s2.set_sticky("ci", "org/repo#run", WaitObservation(
        provider="ci", ref="org/repo#run", state=OBS_READY, subject="deadbeef",
        event_version=2))
    env.clock.advance(2000)
    assert make_manager(env, s2).check_due_waits()[0].reason == "stale_subject"
    assert job_row(env.core, jid)["primary_state"] == \
        PrimaryState.WAITING_EXTERNAL.value
    env.core.close()


def test_case22_pending_no_error_no_llm(db_path):
    env = make_env(db_path)
    job = _running_with_wait(env, owner="A", ttl=600)
    jid = job["id"]
    adapter = FakeExternalWaitAdapter()
    adapter.set_sticky("ci", "org/repo#run", WaitObservation(
        provider="ci", ref="org/repo#run", state=OBS_PENDING, event_version=0))
    mgr = make_manager(env, adapter)
    mgr.enter_waiting_external(
        jid, spec=wait_spec(), owner_instance_id="A", lease_epoch=job["lease_epoch"])
    env.clock.advance(61)
    results = mgr.check_due_waits()
    assert results[0].outcome == "pending"
    assert job_row(env.core, jid)["primary_state"] == \
        PrimaryState.WAITING_EXTERNAL.value
    task_id = job_row(env.core, jid)["task_id"]
    assert env.core._store.list_dispatches(task_id) == []
    env.core.close()


def test_case23_deadline_requeues_external_not_done(db_path):
    from datetime import timedelta
    env = make_env(db_path)
    job = _running_with_wait(env, owner="A", ttl=600)
    jid = job["id"]
    mgr = make_manager(env)
    dl = env.clock() + timedelta(seconds=500)
    mgr.enter_waiting_external(
        jid, spec=wait_spec(deadline_at=dl.isoformat()),
        owner_instance_id="A", lease_epoch=job["lease_epoch"])
    env.clock.advance(600)
    results = mgr.check_due_waits()
    assert results[0].outcome == "woke"
    assert results[0].queue_reason == QueueReason.WAIT_DEADLINE.value
    row = job_row(env.core, jid)
    assert row["primary_state"] == PrimaryState.QUEUED.value
    assert row["error_class"] == "EXTERNAL"
    assert row["terminal"] is None  # never DONE/FAILED
    env.core.close()


# ---------------------------------------------------------------------------
# F. Terminal (cases 24–27)
# ---------------------------------------------------------------------------

def test_case24_done_after_restart_sticky(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    jid = add_queued_job(env, "job", start_run=False)
    set_terminal(env.core, jid, "DONE")
    env.core.close()

    core2 = Core(db_path, clock=clock)
    try:
        row = job_row(core2, jid)
        assert row["primary_state"] == PrimaryState.DONE.value
        assert row["terminal"] == "DONE"
        with pytest.raises(LeaseError):
            core2._store.claim_job(jid, owner_instance_id="A", ttl_seconds=60)
    finally:
        core2.close()


def test_case25_failed_after_restart_sticky(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    jid = add_queued_job(env, "job", start_run=False)
    set_terminal(env.core, jid, "FAILED")
    env.core.close()

    core2 = Core(db_path, clock=clock)
    try:
        assert job_row(core2, jid)["primary_state"] == PrimaryState.FAILED.value
        with pytest.raises(LeaseError):
            core2._store.claim_job(jid, owner_instance_id="A", ttl_seconds=60)
    finally:
        core2.close()


def test_case26_blocked_not_normally_claimable(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env, "job", start_run=False)
    set_terminal(env.core, jid, "BLOCKED")
    with pytest.raises(LeaseError) as exc:
        env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=60)
    assert "not_claimable:BLOCKED" in str(exc.value)
    # Only explicit owner/policy authorization may reopen it.
    row = env.core._store.enqueue_job(
        jid, queue_reason="RECOVERY", owner_authorized=True,
        policy_ref="owner:approved:reopen-1")
    assert row["primary_state"] == PrimaryState.QUEUED.value
    env.core.close()


def test_case27_stale_owner_terminal_mutation_fenced(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env, "job", start_run=False)
    env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=30)
    env.clock.advance(31)
    # Takeover by B (recovery path, never claim_job on a RUNNING job).
    env.core._store.recover_takeover_job(
        jid, expected=job_row(env.core, jid), owner_instance_id="B",
        ttl_seconds=30, process_alive=False, worktree_verdict=None,
    )
    # Stale owner A (epoch 1) tries to mutate the job -> fenced.
    env.sup.set_lease_owner("A", 1)
    with pytest.raises(LeaseFencedError):
        env.sup.reconcile(jid)
    assert job_row(env.core, jid)["owner_instance_id"] == "B"
    env.core.close()
