"""Phase C1 — mandatory memory-pressure scenario (§13).

Simulates the real host (7.7 GiB total) under swap/memory pressure and proves
HEAVY is never admitted without the reserve being provably preserved — and
never with an automatic limit raise (C2 territory, not implemented here).
"""

from __future__ import annotations

from argent_core.resource_governor import (
    AdmissionVerdict,
    ResourceGovernor,
    ResourceReasonCode,
)
from argent_core.resource_policy import ResourceClass, gib, required_host_reserve
from c1_helpers import make_snapshot


def _governor():
    return ResourceGovernor()


REAL_HOST_TOTAL = gib(7.7)


def test_real_host_reserve_is_1_54_gib():
    assert required_host_reserve(REAL_HOST_TOTAL) == int(0.20 * REAL_HOST_TOTAL)
    assert required_host_reserve(REAL_HOST_TOTAL) > gib(1.5)


def test_heavy_never_allowed_under_memory_and_swap_pressure():
    # 7.7 GiB total, only 1.2 GiB available, swap 90% used.
    swap_total = gib(2)
    swap_used = int(swap_total * 0.9)
    snap = make_snapshot(
        mem_total=REAL_HOST_TOTAL,
        mem_available=gib(1.2),
        swap_total=swap_total,
        swap_free=swap_total - swap_used,
    )
    d = _governor().decide(
        resource_class=ResourceClass.HEAVY, snapshot=snap,
        now_iso="2026-09-01T00:00:00+00:00",
    )
    assert d.decision != AdmissionVerdict.ALLOW.value
    assert d.decision in (
        AdmissionVerdict.DEFER.value,
        AdmissionVerdict.DENY_LOCAL.value,
        AdmissionVerdict.PREFER_EXTERNAL.value,
    )
    # Reason must be a resource reason (not OK), and no limit was raised.
    assert d.reason_code in (
        ResourceReasonCode.SWAP_PRESSURE.value,
        ResourceReasonCode.INSUFFICIENT_MEMORY_RESERVE.value,
        ResourceReasonCode.LOCAL_CAPACITY_INSUFFICIENT.value,
    )
    assert d.effective_limits == {}


def test_heavy_allowed_on_healthy_host_when_reserve_preserved():
    # 7.7 GiB total, 6 GiB available -> 6-4 = 2 GiB >= 1.54 GiB reserve.
    snap = make_snapshot(mem_total=REAL_HOST_TOTAL, mem_available=gib(6))
    d = _governor().decide(
        resource_class=ResourceClass.HEAVY, snapshot=snap,
        now_iso="2026-09-01T00:00:00+00:00",
    )
    assert d.decision == AdmissionVerdict.ALLOW.value
    assert d.reason_code == ResourceReasonCode.OK.value
    # Effective memory max never exceeds the ceiling and preserves reserve.
    assert d.effective_limits["memory_max_bytes"] <= gib(4)


def test_heavy_deferred_when_reserve_not_preserved():
    # 5.2 GiB available -> 5.2-4 = 1.2 GiB < 1.54 GiB reserve -> DEFER.
    snap = make_snapshot(mem_total=REAL_HOST_TOTAL, mem_available=gib(5.2))
    d = _governor().decide(
        resource_class=ResourceClass.HEAVY, snapshot=snap,
        now_iso="2026-09-01T00:00:00+00:00",
    )
    assert d.decision == AdmissionVerdict.DEFER.value
    assert d.reason_code == ResourceReasonCode.INSUFFICIENT_MEMORY_RESERVE.value
    assert d.next_eligible_at is not None
