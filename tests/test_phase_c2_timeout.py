"""Phase C2 — wall-clock timeout model (deterministic).

The step timeout is wall-clock, so it is enforced by an external ``timeout``
wrapper (NOT systemd ``TimeoutStopSec``).  Proves the wrapper argv, the
TIMEOUT classification and evidence persistence, and that there is NO automatic
retry with a longer timeout (the timeout value is a bounded policy value).
"""

from __future__ import annotations

import pytest

from argent_core.process_registry import ProcessIdentity, ProcessRegistry
from argent_core.resource_failure import TerminationClass, classify_termination
from argent_core.resource_policy import ResourceClass, ResourcePolicy
from argent_core.scope_enforcer import ExecutionEnforcer, TimeoutRunner
from c2_helpers import FakeScopeBackend, verified_properties


def _limits(timeout=60):
    pol = ResourcePolicy()
    base = pol.limits_for(ResourceClass.HEAVY)
    return {
        "memory_high_bytes": base.memory_high_bytes,
        "memory_max_bytes": base.memory_max_bytes,
        "swap_max_bytes": base.swap_max_bytes,
        "cpu_quota_percent": base.cpu_quota_percent,
        "timeout_seconds": timeout,
    }


def test_timeout_runner_wraps_command():
    runner = TimeoutRunner(kill_after_seconds=10)
    argv = runner.wrap(["openclaw", "agent", "--json"], 60)
    assert argv == ["timeout", "-k", "10", "60", "openclaw", "agent", "--json"]


def test_timeout_runner_rejects_invalid_timeout():
    runner = TimeoutRunner()
    with pytest.raises(ValueError):
        runner.wrap(["openclaw"], 0)
    with pytest.raises(ValueError):
        runner.wrap(["openclaw"], -5)


def test_timeout_runner_rejects_invalid_grace():
    with pytest.raises(ValueError):
        TimeoutRunner(kill_after_seconds=0)


def test_timed_out_classifies_as_timeout():
    assert classify_termination(exit_code=124, timed_out=True) == \
        TerminationClass.TIMEOUT
    # Timeout wins over a non-zero exit code.
    assert classify_termination(exit_code=1, timed_out=True) == \
        TerminationClass.TIMEOUT


def test_timeout_evidence_persists():
    reg = ProcessRegistry(_FakeStore())
    row = reg.register(
        job_id="job-1", dispatch_id=None,
        identity=ProcessIdentity(boot_id="b", pid=1, process_start_ticks=1),
        timed_out=True, termination_class=TerminationClass.TIMEOUT.value,
    )
    assert row["timed_out"] == 1
    assert row["termination_class"] == "TIMEOUT"


def test_no_automatic_longer_retry():
    """The enforcer always uses the SAME policy timeout (never escalates)."""
    backend = FakeScopeBackend(verify_properties=verified_properties(_limits()))
    enforcer = ExecutionEnforcer(backend)
    common = dict(
        command=["openclaw", "agent"],
        effective_limits=_limits(timeout=60),
        resource_class=ResourceClass.HEAVY,
        policy_version="1",
        job_id="job-1",
        dispatch_id="dispatch-1",
    )
    enforcer.enforce_and_spawn(**common)
    enforcer.enforce_and_spawn(**common)
    # Both attempts wrapped with the same 60s timeout (no escalation).
    assert backend.started[0]["command"][3] == "60"
    assert backend.started[1]["command"][3] == "60"


class _FakeStore:
    """Minimal store double for ProcessRegistry (records rows only)."""

    def __init__(self):
        self.rows = []

    def now_iso(self):
        return "2026-09-01T00:00:00+00:00"

    def _insert_process_registration(self, row):
        self.rows.append(dict(row))
