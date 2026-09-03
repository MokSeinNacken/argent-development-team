"""Phase G2 — single-active instance ownership determinism (SPEC G1 §C/§D/§O).

Deterministic, offline.  Re-asserts and extends the G1 single-active fence from
the Phase-G2 controlled-live-restart lens: the persisted singleton row is fenced
by a monotonic integer ``revision`` compare-and-swap, and liveness is decided
from the canonical ``(boot_id, pid, process_start_ticks)`` + ``host_id`` tuple
(never PID alone).  Two racing supervisors can never both win (no split-brain,
no ABA under a frozen clock).

Reuses the G1 test infrastructure (``g1_helpers``).
"""

from __future__ import annotations

from argent_core import Core
from argent_core.background_runtime import (
    OWNER_LIVENESS_DEAD,
    OWNER_LIVENESS_LIVE,
    OWNER_LIVENESS_UNKNOWN,
    InstanceVerdict,
    SupervisorInstance,
)
from argent_core.store import Store
from g1_helpers import make_identity_provider, seed_owner


def _row(instance_id: str, updated_at: str, boot_id: str = "boot-1",
         host_id: str = "host-1") -> dict:
    return {
        "singleton_id": "primary",
        "instance_id": instance_id,
        "boot_id": boot_id,
        "host_id": host_id,
        "pid": 100,
        "process_start_ticks": 5,
        "status": "ACTIVE",
        "acquired_at": updated_at,
        "lease_expires_at": None,
        "last_heartbeat_at": updated_at,
        "stopped_at": None,
        "stop_reason": None,
        "last_error_code": None,
        "updated_at": updated_at,
    }


# ---------------------------------------------------------------------------
# revision CAS determinism — exactly one winner, no ABA under a frozen clock
# ---------------------------------------------------------------------------

def test_revision_cas_exactly_one_winner_frozen_clock(db_path):
    s1 = Store(db_path)
    s2 = Store(db_path)
    try:
        assert s1.cas_supervisor_instance(
            row=_row("instance:old", "T"), expected_revision=None)
        rev = s1.get_supervisor_instance()["revision"]
        assert rev == 1

        # Two candidates read the SAME revision and write the SAME timestamp
        # (frozen clock).  Exactly one CAS must win.
        a = s1.cas_supervisor_instance(row=_row("instance:A", "T"),
                                       expected_revision=rev)
        b = s2.cas_supervisor_instance(row=_row("instance:B", "T"),
                                       expected_revision=rev)
        assert a != b
        final = s1.get_supervisor_instance()
        assert final["revision"] == rev + 1
        assert final["instance_id"] == ("instance:A" if a else "instance:B")
    finally:
        s1.close()
        s2.close()


def test_revision_is_monotonic_across_heartbeat_and_release(db_path):
    core = Core(db_path)
    inst = SupervisorInstance(
        core._store,
        identity_provider=make_identity_provider("boot-1", {100: 5}),
        instance_id="instance:test", own_pid=100,
    )
    assert inst.acquire().verdict in (InstanceVerdict.ACQUIRED,
                                      InstanceVerdict.TAKEOVER)
    r0 = core._store.get_supervisor_instance()["revision"]
    assert inst.heartbeat() is True
    r1 = core._store.get_supervisor_instance()["revision"]
    assert r1 == r0 + 1
    assert inst.release(reason="shutdown") is True
    r2 = core._store.get_supervisor_instance()["revision"]
    assert r2 == r1 + 1
    core.close()


# ---------------------------------------------------------------------------
# Full acquire race — exactly one ACTIVE row, no split-brain
# ---------------------------------------------------------------------------

def test_two_instances_acquire_exactly_one_active(db_path):
    core = Core(db_path)
    a = SupervisorInstance(
        core._store,
        identity_provider=make_identity_provider("boot-1", {100: 5, 200: 9}),
        instance_id="instance:a", own_pid=100, pid_alive=lambda pid: True,
    )
    b = SupervisorInstance(
        core._store,
        identity_provider=make_identity_provider("boot-1", {100: 5, 200: 9}),
        instance_id="instance:b", own_pid=200, pid_alive=lambda pid: True,
    )
    ra = a.acquire()
    rb = b.acquire()
    # Exactly one ACTIVE winner; the loser is refused (live owner or ambiguous).
    winners = [r for r in (ra, rb)
               if r.verdict in (InstanceVerdict.ACQUIRED, InstanceVerdict.TAKEOVER)]
    assert len(winners) == 1
    row = core._store.get_supervisor_instance()
    assert row["status"] == "ACTIVE"
    assert row["instance_id"] == winners[0].instance_id
    # The persisted singleton holds exactly ONE identity (no split-brain).
    winner_instance = a if winners[0].instance_id == a.instance_id else b
    assert row["pid"] == winner_instance.identity.pid
    core.close()


def test_live_owner_refuses_second_supervisor(db_path):
    core = Core(db_path)
    seed_owner(core._store, boot_id="boot-1", pid=100, ticks=5)
    b = SupervisorInstance(
        core._store,
        identity_provider=make_identity_provider("boot-1", {100: 5, 200: 9}),
        instance_id="instance:new", own_pid=200, pid_alive=lambda pid: pid == 100,
    )
    assert b.acquire().verdict is InstanceVerdict.LIVE_OWNER
    assert core._store.get_supervisor_instance()["instance_id"] == "instance:old"
    core.close()


def test_dead_owner_bounded_takeover(db_path):
    core = Core(db_path)
    seed_owner(core._store, boot_id="boot-1", pid=100, ticks=5)
    b = SupervisorInstance(
        core._store,
        identity_provider=make_identity_provider("boot-1", {200: 9}),
        instance_id="instance:new", own_pid=200, pid_alive=lambda pid: False,
    )
    assert b.acquire().verdict is InstanceVerdict.TAKEOVER
    assert core._store.get_supervisor_instance()["instance_id"] == "instance:new"
    core.close()


# ---------------------------------------------------------------------------
# Liveness classification — the canonical (boot_id, pid, ticks, host_id) tuple
# ---------------------------------------------------------------------------

def test_liveness_table_is_deterministic():
    # (boot_id, pid, ticks, host_id) -> exact verdict, no PID-only authority.
    cases = [
        # same process, provably alive -> LIVE
        (dict(owner_boot_id="b", owner_pid=1, owner_start_ticks=5,
              live_boot_id="b", pid_alive=True, live_start_ticks=5),
         OWNER_LIVENESS_LIVE),
        # pid gone -> DEAD
        (dict(owner_boot_id="b", owner_pid=1, owner_start_ticks=5,
              live_boot_id="b", pid_alive=False, live_start_ticks=None),
         OWNER_LIVENESS_DEAD),
        # pid reuse (same pid, different ticks) -> DEAD
        (dict(owner_boot_id="b", owner_pid=1, owner_start_ticks=5,
              live_boot_id="b", pid_alive=True, live_start_ticks=9),
         OWNER_LIVENESS_DEAD),
        # same host reboot (different boot, same host) -> DEAD
        (dict(owner_boot_id="b1", owner_pid=1, owner_start_ticks=5,
              live_boot_id="b2", pid_alive=True, live_start_ticks=5,
              owner_host_id="h", live_host_id="h"),
         OWNER_LIVENESS_DEAD),
        # different host (shared store) -> UNKNOWN (never dead)
        (dict(owner_boot_id="b1", owner_pid=1, owner_start_ticks=5,
              live_boot_id="b2", pid_alive=True, live_start_ticks=5,
              owner_host_id="h1", live_host_id="h2"),
         OWNER_LIVENESS_UNKNOWN),
        # missing host identity -> UNKNOWN (never dead)
        (dict(owner_boot_id="b1", owner_pid=1, owner_start_ticks=5,
              live_boot_id="b2", pid_alive=True, live_start_ticks=5,
              owner_host_id=None, live_host_id="h"),
         OWNER_LIVENESS_UNKNOWN),
        # unreadable live boot -> UNKNOWN
        (dict(owner_boot_id="b", owner_pid=1, owner_start_ticks=5,
              live_boot_id=None, pid_alive=True, live_start_ticks=5),
         OWNER_LIVENESS_UNKNOWN),
        # incomplete persisted identity -> UNKNOWN
        (dict(owner_boot_id=None, owner_pid=1, owner_start_ticks=5,
              live_boot_id="b", pid_alive=True, live_start_ticks=5),
         OWNER_LIVENESS_UNKNOWN),
    ]
    for kwargs, expected in cases:
        got = SupervisorInstance.classify_owner_liveness(**kwargs)
        assert got == expected, f"{kwargs} -> {got}, expected {expected}"


def test_shared_store_foreign_host_is_not_taken_over(db_path):
    core = Core(db_path)
    seed_owner(core._store, boot_id="boot-1", pid=100, ticks=5, host_id="host-1")
    candidate = SupervisorInstance(
        core._store,
        identity_provider=make_identity_provider("boot-2", {200: 9},
                                                 machine_id="host-2"),
        instance_id="instance:new", own_pid=200, pid_alive=lambda pid: True,
    )
    res = candidate.acquire()
    assert res.verdict is InstanceVerdict.AMBIGUOUS
    assert core._store.get_supervisor_instance()["instance_id"] == "instance:old"
    core.close()


def test_reboot_same_pid_same_ticks_still_takeover(db_path):
    core = Core(db_path)
    seed_owner(core._store, boot_id="boot-old", pid=100, ticks=5)
    candidate = SupervisorInstance(
        core._store,
        identity_provider=make_identity_provider("boot-new", {100: 5, 200: 9}),
        instance_id="instance:new", own_pid=200, pid_alive=lambda pid: pid == 100,
    )
    assert candidate.acquire().verdict is InstanceVerdict.TAKEOVER
    core.close()
