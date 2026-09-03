"""Phase G3-A closing fix — no implicit working-directory RW bind (offline).

Proves the new fail-closed contract: the bwrap agent sandbox binds RW ONLY the
per-agent runtime dirs plus an EXPLICITLY authorized workdir; it NEVER
implicitly RW-binds the supervisor's own working directory (``Path.cwd()``).
Deterministic, no real bwrap invocation.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from argent_core.execution_scope import (
    SystemdRunScopeBackend,
    build_agent_sandbox_argv,
)
from argent_core.resource_policy import ResourceClass, ResourcePolicy
from argent_core.scope_enforcer import ExecutionEnforcer
from c2_helpers import FakeScopeBackend, verified_properties


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


def _contains(argv, *seq):
    for i in range(len(argv) - len(seq) + 1):
        if list(argv[i:i + len(seq)]) == list(seq):
            return True
    return False


def _scope():
    from argent_core.execution_scope import ExecutionScope

    return ExecutionScope(
        scope_name="argent-c2-x", unit_name="argent-c2-x.scope",
        cgroup_path="/user.slice/argent-c2-x.scope", job_id="j",
        dispatch_id="d", resource_class="LIGHT", policy_version="1",
        effective_limits={}, process_id=None, created_at="t",
    )


# ---------------------------------------------------------------------------
# (i)/(ii) build_agent_sandbox_argv: cwd=None -> NO bind; explicit -> bind
# ---------------------------------------------------------------------------

def test_build_argv_no_bind_when_cwd_is_none(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".config" / "argent"
    state_dir = home / ".local" / "state" / "argent"
    openclaw_dir = home / ".openclaw"

    argv = build_agent_sandbox_argv(
        ["openclaw", "agent", "--agent", "argent-lead"],
        config_dir=config_dir, state_dir=state_dir, openclaw_dir=openclaw_dir,
        cwd=None,
    )
    # No writable runtime dirs and no explicit workdir -> NO --bind at all.
    assert "--bind" not in argv
    # Specifically the service's own working directory is NEVER implicitly bound.
    assert not _contains(argv, "--bind", str(Path.cwd()), str(Path.cwd()))


def test_build_argv_binds_explicit_cwd(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".config" / "argent"
    state_dir = home / ".local" / "state" / "argent"
    openclaw_dir = home / ".openclaw"
    cwd = tmp_path / "worktree"

    argv = build_agent_sandbox_argv(
        ["openclaw", "agent", "--agent", "argent-lead"],
        config_dir=config_dir, state_dir=state_dir, openclaw_dir=openclaw_dir,
        cwd=cwd,
    )
    assert _contains(argv, "--bind", str(cwd), str(cwd))


# ---------------------------------------------------------------------------
# (iii) _wrap_for_sandbox: no workdir bind when no explicit workdir
# ---------------------------------------------------------------------------

def test_wrap_for_sandbox_no_workdir_bind_without_explicit_workdir(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(shutil, "which", lambda *a, **k: "/usr/bin/bwrap")
    home = tmp_path / "home"
    config_dir = home / ".config" / "argent"
    state_dir = home / ".local" / "state" / "argent"
    openclaw_dir = home / ".openclaw"
    backend = SystemdRunScopeBackend(
        sandbox_dirs={
            "config_dir": config_dir,
            "state_dir": state_dir,
            "openclaw_dir": openclaw_dir,
        },
    )

    argv = backend._wrap_for_sandbox(
        ["openclaw", "agent", "--agent", "argent-lead"],
    )

    # The per-agent runtime dirs ARE rw-bound (the only ~/.openclaw surface).
    assert _contains(
        argv, "--bind",
        str(openclaw_dir / "agents" / "argent-lead"),
        str(openclaw_dir / "agents" / "argent-lead"),
    )
    assert _contains(
        argv, "--bind",
        str(openclaw_dir / "workspace" / "argent-lead"),
        str(openclaw_dir / "workspace" / "argent-lead"),
    )
    # No other --bind pairs: exactly two, and NOT the whole openclaw_dir, and
    # NOT the service's own working directory.
    assert argv.count("--bind") == 2
    assert not _contains(argv, "--bind", str(openclaw_dir), str(openclaw_dir))
    assert not _contains(argv, "--bind", str(Path.cwd()), str(Path.cwd()))


# ---------------------------------------------------------------------------
# (iv) start_in_scope cwd semantics via the popen_fn seam
# ---------------------------------------------------------------------------

def test_start_in_scope_passes_cwd_semantics(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda *a, **k: "/usr/bin/bwrap")
    captured = {}
    home = tmp_path / "home"
    config_dir = home / ".config" / "argent"
    state_dir = home / ".local" / "state" / "argent"
    openclaw_dir = home / ".openclaw"
    workdir = tmp_path / "worktree"
    for d in (config_dir, state_dir, openclaw_dir, workdir):
        d.mkdir(parents=True, exist_ok=True)

    class FakePopen:
        pid = 4242

    def fake_popen(argv, **kw):
        captured["cwd"] = kw.get("cwd")
        return FakePopen()

    backend = SystemdRunScopeBackend(
        popen_fn=fake_popen,
        sandbox_dirs={
            "config_dir": config_dir,
            "state_dir": state_dir,
            "openclaw_dir": openclaw_dir,
        },
    )
    monkeypatch.setattr(backend, "_move_into_cgroup", lambda pid, cg: True)

    # workdir None -> child starts at cwd="/" (never the deployment dir).
    backend.start_in_scope(
        scope=_scope(),
        command=["openclaw", "agent", "--agent", "argent-lead"],
    )
    assert captured["cwd"] == "/"

    # explicit workdir -> child starts at that authorized worktree.
    backend.start_in_scope(
        scope=_scope(),
        command=["openclaw", "agent", "--agent", "argent-lead"],
        workdir=str(workdir),
    )
    assert captured["cwd"] == str(workdir)


# ---------------------------------------------------------------------------
# (v) enforce_and_spawn forwards workdir to the backend
# ---------------------------------------------------------------------------

def test_enforce_and_spawn_forwards_workdir():
    backend = FakeScopeBackend(verify_properties=verified_properties(_limits()))
    enforcer = ExecutionEnforcer(backend)

    r1 = enforcer.enforce_and_spawn(
        command=["openclaw", "agent"], effective_limits=_limits(),
        resource_class=ResourceClass.HEAVY, policy_version="1",
        job_id="job-1", dispatch_id="dispatch-1",
        workdir="/authorized/worktree",
    )
    assert r1.ok
    assert backend.started[0]["workdir"] == "/authorized/worktree"

    r2 = enforcer.enforce_and_spawn(
        command=["openclaw", "agent"], effective_limits=_limits(),
        resource_class=ResourceClass.HEAVY, policy_version="1",
        job_id="job-2", dispatch_id="dispatch-2",
    )
    assert r2.ok
    assert backend.started[1]["workdir"] is None
