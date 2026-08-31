"""Phase B3 external-wait tests (A–G): offline, deterministic.

Covers the external-wait core: atomic RUNNING→WAITING_EXTERNAL transition and
rollback (A), no active agent in wait (B), pending (C), exactly-once wake (D),
dedup/stale (E), deadline (F), restart persistence (G).

All time via ``FakeClock``; the provider is a ``FakeExternalWaitAdapter``; there
is no LLM, no network and no real process.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from argent_core import Core, OWNER_SOURCE
from argent_core.external_wait import (
    ExternalWaitManager,
    FakeExternalWaitAdapter,
    OBS_PENDING,
    OBS_READY,
    WaitObservation,
    WaitSpec,
    next_check_delay_seconds,
)
from argent_core.job_state import PrimaryState, QueueReason
from argent_core.models import LeaseError
from argent_core.store import Store
from argent_core.supervisor import Supervisor
from mock_supervisor_runtime import FakeClock, FakeRunStatusProvider

OWNER = OWNER_SOURCE


def make_env(db_path, clock=None):
    clock = clock or FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    sup = Supervisor(core, FakeRunStatusProvider(), clock=clock)
    return SimpleNamespace(core=core, project=project, sup=sup, clock=clock)


def make_running_job(env, owner="A", ttl=600):
    task = env.core.create_task(env.project.id, "t", OWNER)
    job = env.sup.store.create_job(task.id, idempotency_key=f"job-{task.id}")
    claimed = env.core._store.claim_job(
        job.supervisor_job_id, owner_instance_id=owner, ttl_seconds=ttl
    )
    return claimed, task.id


def make_manager(env, adapter=None):
    return ExternalWaitManager(
        env.core._store,
        adapters={"ci": adapter or FakeExternalWaitAdapter()},
        clock=env.clock,
    )


def wait_spec(**kw):
    base = dict(kind="CI", provider="ci", ref="org/repo#run",
                expected_subject="abc123")
    base.update(kw)
    return WaitSpec(**base)


# ---------------------------------------------------------------------------
# A. Atomic wait transition + rollback
# ---------------------------------------------------------------------------

def test_wait_transition_atomic_releases_lease(db_path):
    env = make_env(db_path)
    job, task_id = make_running_job(env, owner="A", ttl=600)
    jid = job["id"]
    mgr = make_manager(env)

    updated = mgr.enter_waiting_external(
        jid, spec=wait_spec(), owner_instance_id="A", lease_epoch=job["lease_epoch"]
    )
    assert updated["primary_state"] == PrimaryState.WAITING_EXTERNAL.value
    assert updated["status"] == "WAITING_RUN"
    assert updated["owner_instance_id"] is None
    assert updated["lease_expires_at"] is None
    assert updated["wait_kind"] == "CI"

    waits = env.core._store.list_external_waits(jid)
    assert len(waits) == 1
    assert waits[0]["kind"] == "CI"
    assert waits[0]["provider"] == "ci"
    assert waits[0]["ref"] == "org/repo#run"
    assert waits[0]["expected_subject"] == "abc123"
    assert waits[0]["terminal_observed_at"] is None
    env.core.close()


def test_wait_transition_rollback_on_bad_epoch(db_path):
    env = make_env(db_path)
    job, task_id = make_running_job(env, owner="A", ttl=600)
    jid = job["id"]
    mgr = make_manager(env)

    with pytest.raises(LeaseError):
        mgr.enter_waiting_external(
            jid, spec=wait_spec(), owner_instance_id="A", lease_epoch=9999
        )
    row = env.core._store.get_supervisor_job(jid)
    assert row["primary_state"] == PrimaryState.RUNNING.value
    assert row["owner_instance_id"] == "A"
    assert row["lease_expires_at"] is not None
    # No half-wait state: the wait insert rolled back with the transition.
    assert env.core._store.list_external_waits(jid) == []
    env.core.close()


def test_wait_transition_rejects_wrong_owner(db_path):
    env = make_env(db_path)
    job, task_id = make_running_job(env, owner="A", ttl=600)
    jid = job["id"]
    mgr = make_manager(env)
    with pytest.raises(LeaseError):
        mgr.enter_waiting_external(
            jid, spec=wait_spec(), owner_instance_id="B", lease_epoch=job["lease_epoch"]
        )
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == "RUNNING"
    assert env.core._store.list_external_waits(jid) == []
    env.core.close()


def test_wait_transition_rejects_unallowlisted_provider_and_kind(db_path):
    env = make_env(db_path)
    job, task_id = make_running_job(env, owner="A", ttl=600)
    jid = job["id"]
    mgr = make_manager(env)
    # Provider not in the allowlist registry.
    with pytest.raises(ValueError):
        mgr.enter_waiting_external(
            jid, spec=wait_spec(provider="evil"), owner_instance_id="A",
            lease_epoch=job["lease_epoch"],
        )
    # Kind outside the closed set.
    with pytest.raises(ValueError):
        mgr.enter_waiting_external(
            jid, spec=wait_spec(kind="SHELL"), owner_instance_id="A",
            lease_epoch=job["lease_epoch"],
        )
    # Ref rejected by the adapter's validator.
    with pytest.raises(ValueError):
        mgr.enter_waiting_external(
            jid, spec=wait_spec(ref=""), owner_instance_id="A",
            lease_epoch=job["lease_epoch"],
        )
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == "RUNNING"
    assert env.core._store.list_external_waits(jid) == []
    env.core.close()


# ---------------------------------------------------------------------------
# B. No active agent in wait
# ---------------------------------------------------------------------------

def test_waiting_job_not_claimable_and_no_agent(db_path):
    env = make_env(db_path)
    job, task_id = make_running_job(env, owner="A", ttl=600)
    jid = job["id"]
    adapter = FakeExternalWaitAdapter()
    adapter.script("ci", "org/repo#run", [
        WaitObservation(provider="ci", ref="org/repo#run", state=OBS_PENDING,
                        event_version=0),
    ])
    mgr = make_manager(env, adapter)
    mgr.enter_waiting_external(
        jid, spec=wait_spec(), owner_instance_id="A", lease_epoch=job["lease_epoch"]
    )

    row = env.core._store.get_supervisor_job(jid)
    claimable, reason = Store._job_is_claimable(row, env.core._store.now_iso())
    assert not claimable
    assert reason == "not_claimable:WAITING_EXTERNAL"
    assert env.core._store.claim_next_job(owner_instance_id="B", ttl_seconds=60) is None

    # Polling the wait never creates an agent dispatch.
    env.clock.advance(61)
    mgr.check_due_waits()
    assert env.core._store.list_dispatches(task_id) == []
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.WAITING_EXTERNAL.value
    env.core.close()


# ---------------------------------------------------------------------------
# C. Pending
# ---------------------------------------------------------------------------

def test_pending_stays_waiting_backoff_no_wake(db_path):
    env = make_env(db_path)
    job, task_id = make_running_job(env, owner="A", ttl=600)
    jid = job["id"]
    adapter = FakeExternalWaitAdapter()
    adapter.set_sticky("ci", "org/repo#run", WaitObservation(
        provider="ci", ref="org/repo#run", state=OBS_PENDING, event_version=0))
    mgr = make_manager(env, adapter)
    mgr.enter_waiting_external(
        jid, spec=wait_spec(), owner_instance_id="A", lease_epoch=job["lease_epoch"]
    )
    before = env.core._store.list_external_waits(jid)[0]

    env.clock.advance(61)
    results = mgr.check_due_waits()
    assert len(results) == 1
    assert results[0].outcome == "pending"

    after = env.core._store.list_external_waits(jid)[0]
    assert after["last_observed_state"] == OBS_PENDING
    assert after["check_attempt"] == 1
    assert after["next_check_at"] > before["next_check_at"]
    assert after["terminal_observed_at"] is None
    # No wake, no LLM.
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.WAITING_EXTERNAL.value
    assert env.core._store.list_dispatches(task_id) == []
    env.core.close()


def test_backoff_schedule_ladder():
    assert next_check_delay_seconds(1) == 60
    assert next_check_delay_seconds(2) == 120
    assert next_check_delay_seconds(3) == 300
    assert next_check_delay_seconds(4) == 600
    assert next_check_delay_seconds(5) == 1800
    assert next_check_delay_seconds(100) == 1800  # capped at 30 min


# ---------------------------------------------------------------------------
# D. Wake (exactly once)
# ---------------------------------------------------------------------------

def test_relevant_event_wakes_exactly_once_then_claim(db_path):
    env = make_env(db_path)
    job, task_id = make_running_job(env, owner="A", ttl=600)
    jid = job["id"]
    adapter = FakeExternalWaitAdapter()
    adapter.script("ci", "org/repo#run", [
        WaitObservation(provider="ci", ref="org/repo#run", state=OBS_READY,
                        subject="abc123", event_version=1),
    ])
    mgr = make_manager(env, adapter)
    mgr.enter_waiting_external(
        jid, spec=wait_spec(), owner_instance_id="A", lease_epoch=job["lease_epoch"]
    )
    env.clock.advance(61)

    results = mgr.check_due_waits()
    assert len(results) == 1
    assert results[0].outcome == "woke"
    assert results[0].queue_reason == QueueReason.WAIT_EVENT.value

    row = env.core._store.get_supervisor_job(jid)
    assert row["primary_state"] == PrimaryState.QUEUED.value
    assert row["queue_reason"] == QueueReason.WAIT_EVENT.value
    assert row["wait_kind"] == "NONE"

    wait = env.core._store.list_external_waits(jid)[0]
    assert wait["terminal_observed_at"] is not None
    assert wait["event_version"] == 1

    # A second check has no further effect (terminal wait is no longer due).
    env.clock.advance(120)
    results2 = mgr.check_due_waits()
    assert results2 == []
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.QUEUED.value

    # Now a NEW scheduler admission/claim works.
    claimed = env.core._store.claim_job(jid, owner_instance_id="B", ttl_seconds=60)
    assert claimed["primary_state"] == PrimaryState.RUNNING.value
    env.core.close()


# ---------------------------------------------------------------------------
# E. Dedup / stale
# ---------------------------------------------------------------------------

def test_stale_version_and_wrong_ref_and_wrong_sha_have_no_effect(db_path):
    env = make_env(db_path)
    job, task_id = make_running_job(env, owner="A", ttl=600)
    jid = job["id"]
    mgr = make_manager(env)
    mgr.enter_waiting_external(
        jid, spec=wait_spec(), owner_instance_id="A", lease_epoch=job["lease_epoch"]
    )

    # Stale event version (0 <= 0): a READY event is ignored, no wake.
    stale = FakeExternalWaitAdapter()
    stale.set_sticky("ci", "org/repo#run", WaitObservation(
        provider="ci", ref="org/repo#run", state=OBS_READY, subject="abc123",
        event_version=0))
    mgr2 = make_manager(env, stale)
    env.clock.advance(61)
    r = mgr2.check_due_waits()
    assert r[0].outcome == "ignored"
    assert r[0].reason == "stale_version"
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.WAITING_EXTERNAL.value

    # Wrong ref -> no effect.
    wrong_ref = FakeExternalWaitAdapter()
    wrong_ref.set_sticky("ci", "org/repo#run", WaitObservation(
        provider="ci", ref="other/repo#run", state=OBS_READY, subject="abc123",
        event_version=1))
    mgr3 = make_manager(env, wrong_ref)
    env.clock.advance(2000)
    r = mgr3.check_due_waits()
    assert r[0].outcome == "ignored"
    assert r[0].reason == "wrong_ref"

    # Wrong SHA-like subject (stale CI evidence) -> no effect.
    wrong_sha = FakeExternalWaitAdapter()
    wrong_sha.set_sticky("ci", "org/repo#run", WaitObservation(
        provider="ci", ref="org/repo#run", state=OBS_READY, subject="deadbeef",
        event_version=2))
    mgr4 = make_manager(env, wrong_sha)
    env.clock.advance(2000)
    r = mgr4.check_due_waits()
    assert r[0].outcome == "ignored"
    assert r[0].reason == "stale_subject"

    # Still WAITING_EXTERNAL throughout.
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.WAITING_EXTERNAL.value
    env.core.close()


def test_duplicate_event_max_one_wake(db_path):
    env = make_env(db_path)
    job, task_id = make_running_job(env, owner="A", ttl=600)
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
        jid, spec=wait_spec(), owner_instance_id="A", lease_epoch=job["lease_epoch"]
    )
    env.clock.advance(61)
    r1 = mgr.check_due_waits()
    wakes = [x for x in r1 if x.outcome == "woke"]
    assert len(wakes) == 1
    # The second identical observation is never processed (wait now terminal).
    r2 = mgr.check_due_waits()
    assert [x for x in r2 if x.outcome == "woke"] == []
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.QUEUED.value
    env.core.close()


# ---------------------------------------------------------------------------
# F. Deadline
# ---------------------------------------------------------------------------

def test_deadline_requeues_with_external_error_class(db_path):
    env = make_env(db_path)
    job, task_id = make_running_job(env, owner="A", ttl=600)
    jid = job["id"]
    mgr = make_manager(env)
    deadline = env.clock.now_iso()
    # Advance past a generous deadline.
    from datetime import timedelta as _td
    dl = env.clock() + _td(seconds=500)
    mgr.enter_waiting_external(
        jid, spec=wait_spec(deadline_at=dl.isoformat()),
        owner_instance_id="A", lease_epoch=job["lease_epoch"],
    )
    env.clock.advance(600)

    results = mgr.check_due_waits()
    assert len(results) == 1
    assert results[0].outcome == "woke"
    assert results[0].queue_reason == QueueReason.WAIT_DEADLINE.value

    row = env.core._store.get_supervisor_job(jid)
    assert row["primary_state"] == PrimaryState.QUEUED.value
    assert row["queue_reason"] == QueueReason.WAIT_DEADLINE.value
    assert row["error_class"] == "EXTERNAL"
    # Never set to DONE/FAILED directly.
    assert row["terminal"] is None
    assert row["status"] == "WAITING_RUN"
    env.core.close()


# ---------------------------------------------------------------------------
# G. Restart persistence
# ---------------------------------------------------------------------------

def test_wait_survives_restart_and_later_check_works(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    job, task_id = make_running_job(env, owner="A", ttl=600)
    jid = job["id"]
    adapter = FakeExternalWaitAdapter()
    adapter.set_sticky("ci", "org/repo#run", WaitObservation(
        provider="ci", ref="org/repo#run", state=OBS_PENDING, event_version=0))
    mgr = make_manager(env, adapter)
    mgr.enter_waiting_external(
        jid, spec=wait_spec(), owner_instance_id="A", lease_epoch=job["lease_epoch"]
    )
    before = env.core._store.list_external_waits(jid)[0]
    env.core.close()

    # Reopen the DB: no in-memory cache, no agent needed.
    core2 = Core(db_path, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), clock=clock)
    try:
        wait = core2._store.list_external_waits(jid)[0]
        assert wait["next_check_at"] == before["next_check_at"]
        assert wait["deadline_at"] == before["deadline_at"]
        assert wait["terminal_observed_at"] is None
        assert core2._store.get_supervisor_job(jid)["primary_state"] == \
            PrimaryState.WAITING_EXTERNAL.value

        # A later check still works (pending -> backoff, no wake).
        clock.advance(61)
        mgr2 = ExternalWaitManager(
            core2._store, adapters={"ci": adapter}, clock=clock)
        results = mgr2.check_due_waits()
        assert results[0].outcome == "pending"
        assert core2._store.get_supervisor_job(jid)["primary_state"] == \
            PrimaryState.WAITING_EXTERNAL.value
    finally:
        core2.close()
