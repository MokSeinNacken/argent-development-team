"""Phase C1 — host-reserve formula tests (§13 / required_host_reserve)."""

from __future__ import annotations

from argent_core.resource_policy import gib, required_host_reserve


def test_floor_1_5_gib_minimum():
    # Any total at or below the crossover yields exactly the 1.5 GiB floor.
    assert required_host_reserve(gib(1)) == gib(1.5)
    assert required_host_reserve(gib(4)) == gib(1.5)


def test_ratio_20_percent_wins_for_large_ram():
    assert required_host_reserve(gib(32)) == int(0.20 * gib(32))
    assert required_host_reserve(gib(64)) == int(0.20 * gib(64))


def test_larger_of_floor_and_ratio_wins():
    # crossover at 7.5 GiB (20% == 1.5 GiB)
    below = required_host_reserve(gib(7))     # 20% = 1.4 GiB < floor
    above = required_host_reserve(gib(8))     # 20% = 1.6 GiB > floor
    assert below == gib(1.5)
    assert above == int(0.20 * gib(8))
    assert below < above


def test_boundary_exactly_1_5_gib():
    assert required_host_reserve(gib(7.5)) == gib(1.5)


def test_boundary_exactly_20_percent():
    total = gib(7.5)
    assert int(0.20 * total) == gib(1.5)
    assert required_host_reserve(total) == gib(1.5)


def test_nonpositive_total_falls_back_to_floor():
    assert required_host_reserve(0) == gib(1.5)
    assert required_host_reserve(-5) == gib(1.5)
