"""Phase G1 — single-active instance identity + ownership (SPEC G1 §C/§D/§O).

Acceptance cases 1–6, 37, 38 (released re-acquire).  Deterministic and offline:
process identity is injected via a fake ``ProcessIdentityProvider``; the
persisted ``supervisor_instances`` singleton row is fenced by compare-and-swap.
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
from g1_helpers import make_identity_provider, seed_owner


def _instance(store, *, boot_id, ticks_by_pid, own_pid, pid_alive=None, iid="instance:new"):
    return SupervisorInstance(
        store,
        identity_provider=make_identity_provider(boot_id, ticks_by_pid),
        instance_id=iid,
        own_pid=own_pid,
        pid_alive=pid_alive,
    )


# ---------------------------------------------------------------------------
# Case 1: fresh startup -> unique instance
# ---------------------------------------------------------------------------

def test_fresh_startup_acquires_unique_instance(db_path):
    core = Core(db_path)
    inst = _instance(core._store, boot_id="boot-1", ticks_by_pid={100: 5},
                     own_pid=100)
    res = inst.acquire()
    assert res.verdict is InstanceVerdict.ACQUIRED
    assert inst.instance_id.startswith("instance:")
    row = core._store.get_supervisor_instance()
    assert row is not None
    assert row["instance_id"] == inst.instance_id
    assert row["boot_id"] == "boot-1"
    assert row["pid"] == 100
    assert row["process_start_ticks"] == 5
    assert row["status"] == "ACTIVE"
    assert inst.identity is not None
    assert inst.identity.boot_id == "boot-1"
    # A second, distinct process has a distinct instance id.
    core.close()


def test_instance_ids_are_unique_across_stores(db_path, tmp_path):
    a = Core(db_path)
    b = Core(str(tmp_path / "other.db"))
    ia = _instance(a._store, boot_id="boot-1", ticks_by_pid={100: 5},
                   own_pid=100, iid="instance:one")
    ib = _instance(b._store, boot_id="boot-1", ticks_by_pid={200: 9},
                   own_pid=200, iid="instance:two")
    assert ia.instance_id != ib.instance_id
    assert ia.acquire().verdict is InstanceVerdict.ACQUIRED
    assert ib.acquire().verdict is InstanceVerdict.ACQUIRED
    a.close()
    b.close()


# ---------------------------------------------------------------------------
# Case 2: second supervisor vs authoritative-live first -> cannot activate
# ---------------------------------------------------------------------------

def test_live_owner_refuses_second_supervisor(db_path):
    core = Core(db_path)
    seed_owner(core._store, boot_id="boot-1", pid=100, ticks=5)
    inst = _instance(
        core._store, boot_id="boot-1", ticks_by_pid={100: 5, 200: 9},
        own_pid=200, pid_alive=lambda pid: pid == 100,
    )
    res = inst.acquire()
    assert res.verdict is InstanceVerdict.LIVE_OWNER
    # The persisted owner is unchanged (no takeover).
    row = core._store.get_supervisor_instance()
    assert row["instance_id"] == "instance:old"
    core.close()


# ---------------------------------------------------------------------------
# Case 3: stale owner -> bounded takeover
# ---------------------------------------------------------------------------

def test_dead_owner_takeover(db_path):
    core = Core(db_path)
    seed_owner(core._store, boot_id="boot-1", pid=100, ticks=5)
    inst = _instance(
        core._store, boot_id="boot-1", ticks_by_pid={200: 9},
        own_pid=200, pid_alive=lambda pid: False,  # old pid is gone
    )
    res = inst.acquire()
    assert res.verdict is InstanceVerdict.TAKEOVER
    row = core._store.get_supervisor_instance()
    assert row["instance_id"] == "instance:new"
    core.close()


def test_boot_changed_owner_takeover(db_path):
    core = Core(db_path)
    seed_owner(core._store, boot_id="boot-old", pid=100, ticks=5)
    inst = _instance(
        core._store, boot_id="boot-new", ticks_by_pid={200: 9},
        own_pid=200, pid_alive=lambda pid: True,
    )
    assert inst.acquire().verdict is InstanceVerdict.TAKEOVER
    core.close()


# ---------------------------------------------------------------------------
# Case 4/5/6: pure liveness classification (SPEC G1 §D)
# ---------------------------------------------------------------------------

def test_pid_reuse_different_ticks_is_not_same_process():
    assert SupervisorInstance.classify_owner_liveness(
        owner_boot_id="boot-1", owner_pid=100, owner_start_ticks=5,
        live_boot_id="boot-1", pid_alive=True, live_start_ticks=9,
    ) == OWNER_LIVENESS_DEAD


def test_same_pid_different_boot_is_not_same_process():
    # Same HOST, different boot -> a reboot -> the old process is provably dead.
    assert SupervisorInstance.classify_owner_liveness(
        owner_boot_id="boot-1", owner_pid=100, owner_start_ticks=5,
        live_boot_id="boot-2", pid_alive=True, live_start_ticks=5,
        owner_host_id="host-1", live_host_id="host-1",
    ) == OWNER_LIVENESS_DEAD


def test_different_boot_different_host_is_ambiguous_not_dead():
    # G1 (F3): a DIFFERENT host (shared FS/network store) with a different boot
    # must NEVER be treated as dead — the owner may be alive on its own host.
    assert SupervisorInstance.classify_owner_liveness(
        owner_boot_id="boot-1", owner_pid=100, owner_start_ticks=5,
        live_boot_id="boot-2", pid_alive=True, live_start_ticks=5,
        owner_host_id="host-1", live_host_id="host-2",
    ) == OWNER_LIVENESS_UNKNOWN


def test_different_boot_missing_host_identity_is_ambiguous():
    # Without provable same-host identity, a foreign boot is ambiguous (never dead).
    assert SupervisorInstance.classify_owner_liveness(
        owner_boot_id="boot-1", owner_pid=100, owner_start_ticks=5,
        live_boot_id="boot-2", pid_alive=True, live_start_ticks=5,
        owner_host_id=None, live_host_id="host-1",
    ) == OWNER_LIVENESS_UNKNOWN


def test_same_identity_is_live():
    assert SupervisorInstance.classify_owner_liveness(
        owner_boot_id="boot-1", owner_pid=100, owner_start_ticks=5,
        live_boot_id="boot-1", pid_alive=True, live_start_ticks=5,
    ) == OWNER_LIVENESS_LIVE


def test_ambiguous_identity_is_unknown_conservative():
    # unreadable live boot id
    assert SupervisorInstance.classify_owner_liveness(
        owner_boot_id="boot-1", owner_pid=100, owner_start_ticks=5,
        live_boot_id=None, pid_alive=True, live_start_ticks=5,
    ) == OWNER_LIVENESS_UNKNOWN
    # incomplete persisted identity
    assert SupervisorInstance.classify_owner_liveness(
        owner_boot_id=None, owner_pid=100, owner_start_ticks=5,
        live_boot_id="boot-1", pid_alive=True, live_start_ticks=5,
    ) == OWNER_LIVENESS_UNKNOWN
    # unreadable /proc (pid_alive None)
    assert SupervisorInstance.classify_owner_liveness(
        owner_boot_id="boot-1", owner_pid=100, owner_start_ticks=5,
        live_boot_id="boot-1", pid_alive=None, live_start_ticks=None,
    ) == OWNER_LIVENESS_UNKNOWN
    # pid alive but stat unreadable
    assert SupervisorInstance.classify_owner_liveness(
        owner_boot_id="boot-1", owner_pid=100, owner_start_ticks=5,
        live_boot_id="boot-1", pid_alive=True, live_start_ticks=None,
    ) == OWNER_LIVENESS_UNKNOWN


def test_ambiguous_owner_fails_closed_no_takeover(db_path):
    core = Core(db_path)
    seed_owner(core._store, boot_id="boot-1", pid=100, ticks=5)
    # Cannot read /proc -> pid_alive None -> ambiguous -> refuse takeover.
    inst = _instance(
        core._store, boot_id="boot-1", ticks_by_pid={200: 9},
        own_pid=200, pid_alive=lambda pid: None,
    )
    res = inst.acquire()
    assert res.verdict is InstanceVerdict.AMBIGUOUS
    assert core._store.get_supervisor_instance()["instance_id"] == "instance:old"
    core.close()


# ---------------------------------------------------------------------------
# Case 37: simulated reboot -> boot identity change invalidates old liveness
# ---------------------------------------------------------------------------

def test_reboot_same_pid_same_ticks_still_stale(db_path):
    core = Core(db_path)
    seed_owner(core._store, boot_id="boot-old", pid=100, ticks=5)
    # New boot, SAME pid 100 and SAME start ticks 5: PID+ticks "match" but the
    # boot_id differs, so the old process is provably dead (SPEC G1 §O).
    inst = _instance(
        core._store, boot_id="boot-new", ticks_by_pid={100: 5, 200: 9},
        own_pid=200, pid_alive=lambda pid: pid == 100,
    )
    assert inst.acquire().verdict is InstanceVerdict.TAKEOVER
    core.close()


# ---------------------------------------------------------------------------
# Case 38: duplicate startup is idempotent (released owner re-acquire)
# ---------------------------------------------------------------------------

def test_released_owner_reacquire_is_clean(db_path):
    core = Core(db_path)
    seed_owner(core._store, boot_id="boot-1", pid=100, ticks=5,
               status="RELEASED")
    inst = _instance(core._store, boot_id="boot-1", ticks_by_pid={200: 9},
                     own_pid=200, pid_alive=lambda pid: False)
    assert inst.acquire().verdict is InstanceVerdict.ACQUIRED
    core.close()
