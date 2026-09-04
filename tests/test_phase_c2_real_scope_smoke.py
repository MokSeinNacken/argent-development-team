"""Phase C2 — real scope smoke test (guarded, read-only, bounded).

Runs ONLY when ``systemd-run --user --scope`` is available unprivileged (a
read-only subprocess check).  When available it exercises the REAL
:class:`SystemdRunScopeBackend` with a tiny, safe ``sleep`` command: the scope
is created, the process is bound, the enforced properties are read back from
cgroup v2, the process ends normally, and the transient scope is auto-cleaned.

Guardrails (NEVER violated): no large allocation, no swap/disk filling, no
CPU stress, a hard outer timeout, small safe limits (64 MiB / 32 MiB / 16 MiB /
100% CPU), and a bounded ``sleep``.
"""

from __future__ import annotations

import subprocess

import pytest

from argent_core.execution_scope import (
    ExecutionScope,
    SystemdRunScopeBackend,
    generate_scope_name,
    is_valid_scope_name,
    translate_limits_to_properties,
)

_MIB = 1024 * 1024

#: Tiny, safe limits (matches the supervisor's verified host measurements).
_LIMITS = {
    "memory_high_bytes": 32 * _MIB,     # 33554432
    "memory_max_bytes": 64 * _MIB,      # 67108864
    "swap_max_bytes": 16 * _MIB,        # 16777216
    "cpu_quota_percent": 100,
    "timeout_seconds": 30,
}


def _systemd_scope_available() -> bool:
    """Read-only check: can we create a transient user scope? (never mutates)."""
    unit = f"argent-c2-avail-{generate_scope_name('avail', 'probe')}"
    try:
        proc = subprocess.run(
            ["systemd-run", "--user", "--scope", f"--unit={unit}",
             "--", "true"],
            capture_output=True, text=True, timeout=10,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.host_acceptance
@pytest.mark.skipif(
    not _systemd_scope_available(),
    reason="systemd-run --user --scope not available (no root workaround)",
)
def test_real_scope_create_verify_cleanup():
    # OPERATIONAL_HOST_ACCEPTANCE: proves REAL systemd user-scope + cgroup
    # delegation on a live host.  A stock GitHub runner has a systemd user
    # session but does NOT delegate cgroup process-move, so this test is
    # excluded from portable CI via the `host_acceptance` marker (the
    # _systemd_scope_available() skipif above is unchanged).
    # The raw C2 scope backend is exercised WITHOUT the agent-dispatch sandbox
    # (the bwrap wrap is G2 F1; this test proves the scope mechanism itself).
    backend = SystemdRunScopeBackend(sandbox_wrap=False)
    scope_name = generate_scope_name("smoke", "real")
    assert is_valid_scope_name(scope_name)

    scope = ExecutionScope(
        scope_name=scope_name,
        unit_name=scope_name + ".scope",
        cgroup_path="",
        job_id="smoke",
        dispatch_id="real",
        resource_class="LIGHT",
        policy_version="1",
        effective_limits=_LIMITS,
        process_id=None,
        created_at="",
    )
    properties = translate_limits_to_properties(_LIMITS)

    # Start-Barrier (F2): create the scope with a harmless bounded PLACEHOLDER
    # (never the agent), then verify the scope + enforced properties BEFORE
    # the real command starts.
    created = backend.create_scope(
        scope=scope, placeholder_command=["sleep", "30"], properties=properties,
    )
    assert created.placeholder_pid is not None and created.placeholder_pid > 0
    assert created.cgroup_path != ""

    # While the placeholder keeps the scope alive, prove the properties are
    # enforced (systemd show + cgroupfs read-back).
    verified = backend.verify_scope(created)
    assert verified["memory.max"] == str(_LIMITS["memory_max_bytes"])  # 67108864
    assert verified["memory.high"] == str(_LIMITS["memory_high_bytes"])
    assert verified["memory.swap.max"] == str(_LIMITS["swap_max_bytes"])
    assert verified["cpu.max"] == "100000 100000"
    assert verified["ActiveState"] == "active"

    # Start the tiny safe command INSIDE the verified scope and prove the
    # exact process->scope binding.
    started = backend.start_in_scope(scope=created, command=["sleep", "3"])
    assert started.process_id is not None and started.process_id > 0
    assert backend.verify_process_binding(started)

    # Terminate the placeholder; the scope is kept alive by the scoped command.
    backend.stop_placeholder(started)

    # Process ends normally; the transient scope self-cleans.
    import time
    deadline = time.monotonic() + 30  # hard outer timeout
    while time.monotonic() < deadline:
        show = backend._show(started.unit_name, ["ActiveState"])
        if show.get("ActiveState") in ("inactive", "failed", ""):
            break
        time.sleep(0.2)
    assert backend._show(started.unit_name, ["ActiveState"]).get("ActiveState") \
        in ("inactive", "failed", ""), "transient scope was not cleaned up"
