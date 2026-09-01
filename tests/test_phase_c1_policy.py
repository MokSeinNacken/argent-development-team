"""Phase C1 — resource policy unit tests (pure, no host I/O).

Covers the exact resource-class set, ceiling/default values, unknown-class
rejection, ``required_host_reserve`` boundary cases and
``effective_memory_max``.
"""

from __future__ import annotations

import pytest

from argent_core.resource_policy import (
    ResourceClass,
    ResourceLimits,
    ResourcePolicy,
    effective_memory_max,
    gib,
    mib,
    required_host_reserve,
)


def test_resource_classes_are_exactly_the_four():
    assert [c.value for c in ResourceClass] == [
        "LIGHT", "MEDIUM", "HEAVY", "EXCLUSIVE",
    ]


def test_unknown_resource_class_rejected():
    with pytest.raises(ValueError):
        ResourceClass("BOGUS")
    with pytest.raises(ValueError):
        ResourceClass("")


def test_policy_default_ceilings():
    p = ResourcePolicy()
    light = p.limits_for(ResourceClass.LIGHT)
    assert light.memory_high_bytes == mib(768)
    assert light.memory_max_bytes == gib(1)
    assert light.swap_max_bytes == mib(256)
    assert light.cpu_quota_percent == 100
    assert light.timeout_seconds == 15 * 60

    medium = p.limits_for(ResourceClass.MEDIUM)
    assert medium.memory_high_bytes == gib(2)
    assert medium.memory_max_bytes == gib(2.5)
    assert medium.swap_max_bytes == mib(512)
    assert medium.cpu_quota_percent == 200
    assert medium.timeout_seconds == 45 * 60

    heavy = p.limits_for(ResourceClass.HEAVY)
    assert heavy.memory_high_bytes == gib(3)
    assert heavy.memory_max_bytes == gib(4)
    assert heavy.swap_max_bytes == gib(1)
    assert heavy.cpu_quota_percent == 300
    assert heavy.timeout_seconds == 120 * 60

    excl = p.limits_for(ResourceClass.EXCLUSIVE)
    assert excl.memory_high_bytes == gib(4.5)
    assert excl.memory_max_bytes == gib(5.5)
    assert excl.swap_max_bytes == gib(1.5)
    assert excl.cpu_quota_percent == 400
    assert excl.timeout_seconds is None


def test_policy_is_frozen_and_versioned():
    p = ResourcePolicy()
    assert p.policy_version == "1"
    with pytest.raises(Exception):
        p.minimum_host_reserve_bytes = 0  # frozen dataclass


def test_required_host_reserve_small_ram_uses_floor():
    # 4 GiB total -> 20% = 0.8 GiB < 1.5 GiB floor -> floor wins.
    assert required_host_reserve(gib(4)) == gib(1.5)


def test_required_host_reserve_large_ram_uses_ratio():
    # 32 GiB total -> 20% = 6.4 GiB > 1.5 GiB floor -> ratio wins.
    assert required_host_reserve(gib(32)) == int(0.20 * gib(32))


def test_required_host_reserve_boundary_exact_floor():
    # Exactly 7.5 GiB total -> 20% == 1.5 GiB exactly.
    total = gib(7.5)
    assert int(0.20 * total) == gib(1.5)
    assert required_host_reserve(total) == gib(1.5)


def test_required_host_reserve_boundary_exact_ratio():
    # A total where 20% exactly equals the floor: 7.5 GiB.
    assert required_host_reserve(gib(7.5)) == gib(1.5)
    # Just above -> ratio wins.
    assert required_host_reserve(gib(8)) == int(0.20 * gib(8))


def test_required_host_reserve_nonpositive_falls_back_to_floor():
    assert required_host_reserve(0) == gib(1.5)
    assert required_host_reserve(-100) == gib(1.5)


def test_effective_memory_max():
    # ceiling wins when plenty of headroom.
    assert effective_memory_max(gib(4), gib(6), gib(1.5)) == gib(4)
    # headroom wins when tight.
    assert effective_memory_max(gib(4), gib(5), gib(1.5)) == gib(3.5)
    # floored at 0.
    assert effective_memory_max(gib(4), gib(1), gib(1.5)) == 0


def test_policy_concurrency_defaults():
    p = ResourcePolicy()
    assert p.max_writers_global == 1
    assert p.max_light == 2
    assert p.max_medium == 1
    assert p.max_heavy == 1
    assert p.is_writer_class(ResourceClass.MEDIUM)
    assert p.is_writer_class(ResourceClass.HEAVY)
    assert p.is_writer_class(ResourceClass.EXCLUSIVE)
    assert not p.is_writer_class(ResourceClass.LIGHT)
