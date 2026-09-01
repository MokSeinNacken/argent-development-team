"""Phase C2 — scope verification (deterministic; no live systemd).

Exercises the REAL :class:`SystemdRunScopeBackend.verify_scope` logic with
injected readers (``_show`` / ``_read_cgroupfs`` / ``_process_in_cgroup``), so
the fail-closed property checks are proven without touching the host: correct
properties PASS, any deviating / missing critical property or a wrong
process-binding FAILS.
"""

from __future__ import annotations

import pytest

from argent_core.execution_scope import (
    ExecutionScope,
    ScopeVerificationError,
    SystemdRunScopeBackend,
)
from argent_core.resource_policy import ResourceClass, ResourcePolicy
from c2_helpers import verified_properties


def _limits():
    pol = ResourcePolicy()
    base = pol.limits_for(ResourceClass.HEAVY)
    return {
        "memory_high_bytes": base.memory_high_bytes,
        "memory_max_bytes": base.memory_max_bytes,
        "swap_max_bytes": base.swap_max_bytes,
        "cpu_quota_percent": base.cpu_quota_percent,
        "timeout_seconds": base.timeout_seconds,
    }


def _scope():
    limits = _limits()
    return ExecutionScope(
        scope_name="argent-c2-job-dispat-abcdef01",
        unit_name="argent-c2-job-dispat-abcdef01.scope",
        cgroup_path="/user.slice/test/app.slice/argent-c2-job-dispat-abcdef01.scope",
        job_id="job-1",
        dispatch_id="dispatch-1",
        resource_class="HEAVY",
        policy_version="1",
        effective_limits=limits,
        process_id=424242,
        created_at="2026-09-01T00:00:00+00:00",
    )


class ScriptedSystemdBackend(SystemdRunScopeBackend):
    """Real verify logic with scripted readers (no subprocess / no host I/O)."""

    def __init__(self, show=None, cgroupfs=None, proc_binding=True):
        super().__init__()
        self._show_values = dict(show or {})
        self._cgroupfs = dict(cgroupfs or {})
        self._proc_binding = proc_binding

    def _show(self, unit_name, props):
        return dict(self._show_values)

    def _read_cgroupfs(self, cgroup_path):
        return dict(self._cgroupfs)

    def _process_in_cgroup(self, pid, cgroup_path):
        return self._proc_binding


def _fs(limits):
    cpu = limits["cpu_quota_percent"]
    return {
        "memory.max": str(limits["memory_max_bytes"]),
        "memory.high": str(limits["memory_high_bytes"]),
        "memory.swap.max": str(limits["swap_max_bytes"]),
        "cpu.max": f"{cpu * 1000} 100000",
        "pids.max": "64",
    }


def _show(limits, *, active=True, control_group=None):
    return {
        "MemoryMax": str(limits["memory_max_bytes"]),
        "MemoryHigh": str(limits["memory_high_bytes"]),
        "MemorySwapMax": str(limits["swap_max_bytes"]),
        "CPUQuotaPerSecUSec": f"{limits['cpu_quota_percent'] // 100}s",
        "TasksMax": "64",
        "ControlGroup": control_group or _scope().cgroup_path,
        "ActiveState": "active" if active else "inactive",
    }


def test_correct_properties_pass():
    limits = _limits()
    backend = ScriptedSystemdBackend(show=_show(limits), cgroupfs=_fs(limits))
    out = backend.verify_scope(_scope())
    assert out["MemoryMax"] == str(limits["memory_max_bytes"])
    assert out["cpu.max"] == f"{limits['cpu_quota_percent'] * 1000} 100000"


def test_deviating_memory_max_rejected():
    limits = _limits()
    show = _show(limits)
    show["MemoryMax"] = "1"
    backend = ScriptedSystemdBackend(show=show, cgroupfs=_fs(limits))
    with pytest.raises(ScopeVerificationError):
        backend.verify_scope(_scope())


def test_deviating_cgroupfs_memory_max_rejected():
    limits = _limits()
    fs = _fs(limits)
    fs["memory.max"] = "999"
    backend = ScriptedSystemdBackend(show=_show(limits), cgroupfs=fs)
    with pytest.raises(ScopeVerificationError):
        backend.verify_scope(_scope())


def test_missing_property_is_unknown_fail_closed():
    limits = _limits()
    show = _show(limits)
    del show["MemoryHigh"]  # critical property missing -> UNKNOWN -> fail-closed
    backend = ScriptedSystemdBackend(show=show, cgroupfs=_fs(limits))
    with pytest.raises(ScopeVerificationError):
        backend.verify_scope(_scope())


def test_inactive_scope_rejected():
    limits = _limits()
    backend = ScriptedSystemdBackend(
        show=_show(limits, active=False), cgroupfs=_fs(limits),
    )
    with pytest.raises(ScopeVerificationError):
        backend.verify_scope(_scope())


def test_wrong_process_binding_rejected():
    limits = _limits()
    backend = ScriptedSystemdBackend(
        show=_show(limits), cgroupfs=_fs(limits), proc_binding=False,
    )
    with pytest.raises(ScopeVerificationError):
        backend.verify_scope(_scope())


def test_deviating_cpu_quota_rejected():
    limits = _limits()
    fs = _fs(limits)
    fs["cpu.max"] = "999 100000"
    backend = ScriptedSystemdBackend(show=_show(limits), cgroupfs=fs)
    with pytest.raises(ScopeVerificationError):
        backend.verify_scope(_scope())
