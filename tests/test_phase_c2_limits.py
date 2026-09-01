"""Phase C2 — limit validation (deterministic, pure, fail-closed).

Proves ``validate_effective_limits`` enforces the C2 invariants: every limit is
a strictly positive finite int, ``MemoryHigh <= MemoryMax <= ceiling``,
``SwapMax <= ceiling``, ``CPUQuota <= ceiling``, and the wall-clock timeout is
bounded by the policy maximum.  Invalid / negative / None / inf / bool values
must raise ``ValueError`` (fail-closed — never a silently adjusted limit).
"""

from __future__ import annotations

import math

import pytest

from argent_core.execution_scope import (
    validate_effective_limits,
    translate_limits_to_properties,
)
from argent_core.resource_policy import ResourceClass, ResourcePolicy


def _limits(policy=None, **over):
    pol = policy or ResourcePolicy()
    base = pol.limits_for(ResourceClass.HEAVY)
    d = {
        "memory_high_bytes": base.memory_high_bytes,
        "memory_max_bytes": base.memory_max_bytes,
        "swap_max_bytes": base.swap_max_bytes,
        "cpu_quota_percent": base.cpu_quota_percent,
        "timeout_seconds": base.timeout_seconds,
    }
    d.update(over)
    return d


def test_valid_heavy_limits_pass_and_are_canonicalised():
    out = validate_effective_limits(
        _limits(), resource_class=ResourceClass.HEAVY, policy=ResourcePolicy(),
    )
    assert set(out) == {
        "memory_high_bytes", "memory_max_bytes", "swap_max_bytes",
        "cpu_quota_percent", "timeout_seconds",
    }


def test_memory_high_greater_than_memory_max_rejected():
    with pytest.raises(ValueError):
        validate_effective_limits(
            _limits(memory_high_bytes=5 * 1024 ** 3, memory_max_bytes=1024 ** 3),
            resource_class=ResourceClass.HEAVY, policy=ResourcePolicy(),
        )


def test_memory_max_above_class_ceiling_rejected():
    with pytest.raises(ValueError):
        validate_effective_limits(
            _limits(memory_max_bytes=100 * 1024 ** 3),
            resource_class=ResourceClass.HEAVY, policy=ResourcePolicy(),
        )


def test_swap_max_above_ceiling_rejected():
    with pytest.raises(ValueError):
        validate_effective_limits(
            _limits(swap_max_bytes=100 * 1024 ** 3),
            resource_class=ResourceClass.HEAVY, policy=ResourcePolicy(),
        )


def test_cpu_quota_above_ceiling_rejected():
    with pytest.raises(ValueError):
        validate_effective_limits(
            _limits(cpu_quota_percent=500),
            resource_class=ResourceClass.HEAVY, policy=ResourcePolicy(),
        )


def test_timeout_above_policy_maximum_rejected():
    with pytest.raises(ValueError):
        validate_effective_limits(
            _limits(timeout_seconds=24 * 3600),
            resource_class=ResourceClass.HEAVY, policy=ResourcePolicy(),
        )


@pytest.mark.parametrize("field", [
    "memory_high_bytes", "memory_max_bytes", "swap_max_bytes",
    "cpu_quota_percent", "timeout_seconds",
])
@pytest.mark.parametrize("bad", [None, -1, 0, math.inf, float("nan"), 1.5, True, "123"])
def test_invalid_and_nonpositive_and_nonint_values_rejected(field, bad):
    with pytest.raises(ValueError):
        validate_effective_limits(
            _limits(**{field: bad}),
            resource_class=ResourceClass.HEAVY, policy=ResourcePolicy(),
        )


def test_exclusive_none_timeout_rejected():
    # EXCLUSIVE has a step-specific (None) default timeout; C2 has no per-step
    # timeout, so None is fail-closed (enforcement cannot be proven).
    pol = ResourcePolicy()
    excl = pol.limits_for(ResourceClass.EXCLUSIVE)
    with pytest.raises(ValueError):
        validate_effective_limits(
            {
                "memory_high_bytes": excl.memory_high_bytes,
                "memory_max_bytes": excl.memory_max_bytes,
                "swap_max_bytes": excl.swap_max_bytes,
                "cpu_quota_percent": excl.cpu_quota_percent,
                "timeout_seconds": None,
            },
            resource_class=ResourceClass.EXCLUSIVE, policy=pol,
        )


def test_unknown_resource_class_rejected():
    with pytest.raises(ValueError):
        validate_effective_limits(
            _limits(), resource_class="NOPE", policy=ResourcePolicy(),
        )


def test_translate_limits_to_properties():
    props = translate_limits_to_properties(_limits())
    assert props["MemoryMax"] == str(4 * 1024 ** 3)
    assert props["MemoryHigh"] == str(3 * 1024 ** 3)
    assert props["MemorySwapMax"] == str(1024 ** 3)
    assert props["CPUQuota"] == "300%"
    assert props["TasksMax"] == "64"
    # Timeout is deliberately NOT a systemd property (wall-clock wrapper).
    assert "timeout" not in {k.lower() for k in props}
