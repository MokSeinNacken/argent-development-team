"""Phase I3-C1 — runtime integration (single scheduler, no second daemon).

Verifies the CI wait manager integrates into the SAME bounded background loop
and scheduler pass (no second source of truth), and that the existing
ExternalWaitManager excludes CI waits (so CI is owned exclusively by the CI
manager — no double-processing).
"""

from __future__ import annotations

from argent_core.background_runtime import InstanceVerdict, SupervisorRuntime
from argent_core.ci_external_wait import (
    CiWaitManager,
    FakeCiAdapter,
    make_ci_check,
    make_ci_read,
)
from argent_core.external_wait import ExternalWaitManager
from argent_core.job_state import PrimaryState, QueueReason, WaitKind
from argent_core.models import RoleRunStatus
from g1_helpers import add_queued_job, make_runtime_env
from i3c1_helpers import REPO, SHA_A, ci_spec
from mock_supervisor_runtime import FakeClock

_NON_CI_KINDS = frozenset({
    WaitKind.UPSTREAM.value, WaitKind.RATE_LIMIT.value,
    WaitKind.NETWORK.value, WaitKind.TIMER.value,
})


def _finish_active_role_run(env, task_id):
    """Complete the started role-run left by ``run_pass`` (the model finished
    and recommended the wait) so CI wait entry is legal under HIGH-5."""
    store = env.core._store
    for r in store.list_role_runs(task_id, status=RoleRunStatus.STARTED):
        store._update_role_run_status(
            r.id, RoleRunStatus.COMPLETED, env.clock().isoformat())



def test_external_wait_manager_excludes_ci_kinds(db_path):
    # The existing ExternalWaitManager can be scoped to exclude CI waits; with
    # the exclusion it never touches a due kind='ci' row.
    env = make_runtime_env(db_path)
    jid = add_queued_job(env)
    env.sched.run_pass(jid)
    row = env.core._store.get_supervisor_job(jid)
    _finish_active_role_run(env, row["task_id"])  # model finished → no active LLM
    cim = CiWaitManager(env.core._store, adapters={"github": FakeCiAdapter()},
                        clock=env.clock)
    cim.enter_ci_wait(jid, spec=ci_spec(),
                      owner_instance_id=env.instance.instance_id,
                      lease_epoch=row["lease_epoch"])
    env.clock.advance(61)

    # Scoped (excludes CI): no CI wait is processed.
    scoped = ExternalWaitManager(
        env.core._store, adapters={}, clock=env.clock, kinds=_NON_CI_KINDS)
    assert scoped.check_due_waits() == []

    # Unscoped (default, all kinds) WOULD observe it (and back it off as an
    # unknown provider) — proving the kind filter is what keeps them separate.
    unscoped = ExternalWaitManager(env.core._store, adapters={}, clock=env.clock)
    results = unscoped.check_due_waits()
    assert results and results[0].outcome == "unknown_provider"
    env.core.close()


def test_ci_wait_integrated_into_runtime_loop(db_path):
    # The CI wait manager runs in the SAME bounded loop as the scheduler: a due
    # CI wait is checked deterministically (no LLM) and wakes the job exactly
    # once, which the scheduler then admits on a later pass.
    clock = FakeClock()
    env = make_runtime_env(db_path, clock=clock)
    jid = add_queued_job(env)
    env.sched.run_pass(jid)  # claim → RUNNING
    row = env.core._store.get_supervisor_job(jid)
    _finish_active_role_run(env, row["task_id"])  # model finished → no active LLM
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=SHA_A, pr_state="OPEN",
        checks=[make_ci_check("ci", conclusion="SUCCESS", check_id=1)],
        event_version=1))
    cim = CiWaitManager(env.core._store, adapters={"github": adapter},
                        clock=clock)
    cim.enter_ci_wait(jid, spec=ci_spec(),
                      owner_instance_id=env.instance.instance_id,
                      lease_epoch=row["lease_epoch"])

    # A CI-scoped external wait manager so the two managers never double-touch
    # a CI row (the production build_service wires the same split).
    ewm_scoped = ExternalWaitManager(
        env.core._store, adapters={}, clock=clock, kinds=_NON_CI_KINDS)

    rt = SupervisorRuntime(
        scheduler=env.sched, external_wait_manager=ewm_scoped,
        ci_wait_manager=cim, instance=env.instance, store=env.core._store,
        clock=clock, sleep_fn=lambda s: None, max_passes=1,
    )
    clock.advance(61)
    rt.run_loop()

    row2 = env.core._store.get_supervisor_job(jid)
    assert row2["primary_state"] == PrimaryState.QUEUED.value
    assert row2["queue_reason"] == QueueReason.WAIT_EVENT.value
    assert row2["wait_kind"] == "NONE"
    env.core.close()


def test_singleton_loss_before_poll_stale_runtime_cannot_requeue(db_path):
    # HIGH-2(a): a runtime that lost the single-active fence must NOT poll due
    # waits (no requeue / backoff writes) — it aborts with no writes.
    clock = FakeClock()
    env = make_runtime_env(db_path, clock=clock, instance_id="instance:A")
    jid = add_queued_job(env)
    env.sched.run_pass(jid)
    row = env.core._store.get_supervisor_job(jid)
    _finish_active_role_run(env, row["task_id"])

    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=SHA_A, pr_state="OPEN",
        checks=[make_ci_check("ci", conclusion="SUCCESS", check_id=1)],
        event_version=1))
    cim = CiWaitManager(env.core._store, adapters={"github": adapter},
                        clock=clock)
    cim.enter_ci_wait(jid, spec=ci_spec(),
                      owner_instance_id=env.instance.instance_id,
                      lease_epoch=row["lease_epoch"])
    ewm_scoped = ExternalWaitManager(
        env.core._store, adapters={}, clock=clock, kinds=_NON_CI_KINDS)

    rt = SupervisorRuntime(
        scheduler=env.sched, external_wait_manager=ewm_scoped,
        ci_wait_manager=cim, instance=env.instance, store=env.core._store,
        clock=clock, sleep_fn=lambda s: None, max_passes=1,
    )
    # Instance A acquires the single-active fence (so it is a real holder).
    acq = env.instance.acquire()
    assert acq.verdict in (InstanceVerdict.ACQUIRED, InstanceVerdict.TAKEOVER)
    clock.advance(61)  # the CI wait is now due

    # A takeover happens BEFORE the poll: instance B replaces A.
    cur = env.core._store.get_supervisor_instance()
    new_row = dict(cur)
    new_row["instance_id"] = "instance:B"
    assert env.core._store.cas_supervisor_instance(
        row=new_row, expected_revision=cur["revision"])

    rt.run_loop()

    # The stale runtime A never requeued the job (still WAITING_EXTERNAL).
    row2 = env.core._store.get_supervisor_job(jid)
    assert row2["primary_state"] == PrimaryState.WAITING_EXTERNAL.value
    env.core.close()
