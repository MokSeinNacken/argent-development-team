"""Phase C1 — swap-pressure admission tests (thresholds from policy)."""

from __future__ import annotations

from argent_core.resource_governor import (
    AdmissionVerdict,
    ResourceGovernor,
    ResourceReasonCode,
)
from argent_core.resource_policy import ResourceClass, gib
from c1_helpers import make_snapshot


def _governor():
    return ResourceGovernor()


def _swap_snapshot(used_ratio: float):
    """A healthy host except swap ``used_ratio`` of 2 GiB is used."""
    total = gib(2)
    used = int(total * used_ratio)
    return make_snapshot(swap_total=total, swap_free=total - used)


def test_swap_below_70_is_normal():
    snap = _swap_snapshot(0.5)
    d = _governor().decide(
        resource_class=ResourceClass.HEAVY, snapshot=snap, now_iso="2026-09-01T00:00:00+00:00",
    )
    assert d.decision == AdmissionVerdict.ALLOW.value
    assert d.reason_code == ResourceReasonCode.OK.value


def test_swap_70_to_85_defers_medium_heavy_exclusive():
    for cls in (ResourceClass.MEDIUM, ResourceClass.HEAVY, ResourceClass.EXCLUSIVE):
        d = _governor().decide(
            resource_class=cls, snapshot=_swap_snapshot(0.75),
            now_iso="2026-09-01T00:00:00+00:00",
        )
        assert d.decision == AdmissionVerdict.DEFER.value
        assert d.reason_code == ResourceReasonCode.SWAP_PRESSURE.value


def test_swap_85_or_more_blocks_medium_heavy_exclusive():
    for cls in (ResourceClass.MEDIUM, ResourceClass.HEAVY, ResourceClass.EXCLUSIVE):
        d = _governor().decide(
            resource_class=cls, snapshot=_swap_snapshot(0.9),
            now_iso="2026-09-01T00:00:00+00:00",
        )
        assert d.decision == AdmissionVerdict.DEFER.value
        assert d.reason_code == ResourceReasonCode.SWAP_PRESSURE.value


def test_light_allowed_under_warning_swap_when_reserve_safe():
    # LIGHT is not deferred at 70-85% when the rest of the host is healthy.
    d = _governor().decide(
        resource_class=ResourceClass.LIGHT, snapshot=_swap_snapshot(0.75),
        now_iso="2026-09-01T00:00:00+00:00",
    )
    assert d.decision == AdmissionVerdict.ALLOW.value


def test_light_still_allowed_at_high_swap_when_reserve_safe():
    d = _governor().decide(
        resource_class=ResourceClass.LIGHT, snapshot=_swap_snapshot(0.9),
        now_iso="2026-09-01T00:00:00+00:00",
    )
    assert d.decision == AdmissionVerdict.ALLOW.value
