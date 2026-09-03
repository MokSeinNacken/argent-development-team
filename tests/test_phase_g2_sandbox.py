"""Phase G2 — agent-dispatch filesystem sandbox (F1, real trust boundary).

Proves the REAL filesystem trust boundary at the agent-spawn boundary: the
untrusted same-UID agent child is wrapped in a read-only-root ``bwrap``
namespace that masks the two TRUSTED dirs (``~/.config/argent`` and
``~/.local/state/argent``) with empty tmpfs mounts, so the evidence MAC key and
the durable DB are ABSENT (a read raises ``FileNotFoundError``) and any write
lands on an ephemeral tmpfs that vanishes.

Deterministic argv tests are offline; the adversarial probe and the
short-timeout tree-kill test run REAL ``bwrap`` (they skip only when ``bwrap``
is unavailable on the host — they never silently pass without the sandbox).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from argent_core.execution_scope import (
    ScopeCreateError,
    SystemdRunScopeBackend,
    build_agent_sandbox_argv,
    verify_bwrap_available,
)
from argent_core.scope_enforcer import TimeoutRunner

_HAS_BWRAP = shutil.which("bwrap") is not None


# ---------------------------------------------------------------------------
# (a) deterministic argv construction (offline)
# ---------------------------------------------------------------------------

def test_sandbox_argv_has_required_elements(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".config" / "argent"
    state_dir = home / ".local" / "state" / "argent"
    openclaw_dir = home / ".openclaw"
    cwd = tmp_path / "worktree"

    argv = build_agent_sandbox_argv(
        ["openclaw", "agent", "--agent", "argent-lead"],
        config_dir=config_dir, state_dir=state_dir,
        openclaw_dir=openclaw_dir, cwd=cwd,
    )

    assert argv[0] == "bwrap"
    # Read-only root.
    assert _contains(argv, "--ro-bind", "/", "/")
    # Writable binds for the legitimate child write areas.
    assert _contains(argv, "--bind", str(openclaw_dir), str(openclaw_dir))
    assert _contains(argv, "--bind", str(cwd), str(cwd))
    # Empty tmpfs masks over the two trusted dirs.
    assert _contains(argv, "--tmpfs", str(config_dir))
    assert _contains(argv, "--tmpfs", str(state_dir))
    # Ephemeral /tmp + device/proc mounts.
    assert _contains(argv, "--tmpfs", "/tmp")
    assert _contains(argv, "--dev", "/dev")
    assert _contains(argv, "--proc", "/proc")
    # NO network unshare (the agent needs provider network).
    assert "--unshare-net" not in argv
    # The command is preserved verbatim after the ``--`` separator.
    idx = argv.index("--")
    assert argv[idx + 1:] == ["openclaw", "agent", "--agent", "argent-lead"]


def test_sandbox_argv_never_unshares_net_or_pid():
    argv = build_agent_sandbox_argv(
        ["true"],
        config_dir="/cfg", state_dir="/st", openclaw_dir="/oc", cwd="/cwd",
    )
    assert "--unshare-net" not in argv
    assert "--unshare-pid" not in argv
    assert "--unshare-user" not in argv


def test_start_in_scope_wraps_with_bwrap(tmp_path, monkeypatch):
    # The backend's ``start_in_scope`` must actually wrap (no unwrapped branch
    # on the agent-dispatch path).
    captured = {}
    home = tmp_path / "home"
    config_dir = home / ".config" / "argent"
    state_dir = home / ".local" / "state" / "argent"
    openclaw_dir = home / ".openclaw"
    cwd = tmp_path / "worktree"
    for d in (config_dir, state_dir, openclaw_dir, cwd):
        d.mkdir(parents=True, exist_ok=True)

    class FakePopen:
        pid = 4242

    def fake_popen(argv, **kw):
        captured["argv"] = list(argv)
        captured["env"] = kw.get("env")
        return FakePopen()

    backend = SystemdRunScopeBackend(
        popen_fn=fake_popen,
        sandbox_dirs={
            "config_dir": config_dir, "state_dir": state_dir,
            "openclaw_dir": openclaw_dir, "cwd": cwd,
        },
    )
    scope = _scope()
    monkeypatch.setattr(backend, "_move_into_cgroup", lambda pid, cg: True)
    backend.start_in_scope(scope=scope, command=["openclaw", "agent"])

    argv = captured["argv"]
    assert argv[0] == "bwrap"
    assert _contains(argv, "--tmpfs", str(config_dir))
    assert _contains(argv, "--tmpfs", str(state_dir))
    assert _contains(argv, "--bind", str(openclaw_dir), str(openclaw_dir))
    assert "openclaw" in argv  # the command is still present
    # The child env remains the allowlisted, secret-stripped env.
    assert captured["env"] is not None


# ---------------------------------------------------------------------------
# (b) adversarial REAL spawn probe (bwrap must actually run here)
# ---------------------------------------------------------------------------

_PROBE = """\
import os, sys, json
cfg = sys.argv[1]
st = sys.argv[2]
key = os.path.join(cfg, "evidence_mac.key")
db = os.path.join(st, "argent.db")
r = {}

def rd(p):
    try:
        os.open(p, os.O_RDONLY).close()
        return "OPENED"
    except FileNotFoundError:
        return "ENOENT"
    except Exception as e:  # noqa: BLE001
        return type(e).__name__

r["key_read"] = rd(key)
r["db_read"] = rd(db)
r["key_exists"] = os.path.exists(key)
r["db_exists"] = os.path.exists(db)
r["cfg_dir_exists"] = os.path.isdir(cfg)
r["st_dir_exists"] = os.path.isdir(st)
for name, p in (("key", key), ("db", db)):
    try:
        os.close(os.open(p, os.O_CREAT | os.O_WRONLY, 0o600))
        r[name + "_write"] = "CREATED"
    except Exception as e:  # noqa: BLE001
        r[name + "_write"] = type(e).__name__
print(json.dumps(r))
"""


@pytest.mark.skipif(not _HAS_BWRAP, reason="bwrap unavailable on this host")
def test_adversarial_probe_cannot_read_or_persist_trusted_dirs(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".config" / "argent"
    state_dir = home / ".local" / "state" / "argent"
    openclaw_dir = home / ".openclaw"
    cwd = tmp_path / "worktree"
    for d in (config_dir, state_dir, openclaw_dir, cwd):
        d.mkdir(parents=True, exist_ok=True)
    # Seed the REAL trusted files (deterministic test bytes, never the live key).
    (config_dir / "evidence_mac.key").write_bytes(b"K" * 32)
    (state_dir / "argent.db").write_bytes(b"D" * 32)

    probe = cwd / "probe.py"
    probe.write_text(_PROBE, encoding="utf-8")

    argv = build_agent_sandbox_argv(
        ["python3", str(probe), str(config_dir), str(state_dir)],
        config_dir=config_dir, state_dir=state_dir,
        openclaw_dir=openclaw_dir, cwd=cwd,
    )
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30,
                          cwd=str(cwd))
    assert proc.returncode == 0, proc.stderr
    r = json.loads(proc.stdout)

    # The real key/DB are ABSENT inside the sandbox (read -> ENOENT).
    assert r["key_read"] == "ENOENT", r
    assert r["db_read"] == "ENOENT", r
    assert r["key_exists"] is False, r
    assert r["db_exists"] is False, r
    # The masked dirs exist but are EMPTY (tmpfs mount).
    assert r["cfg_dir_exists"] is True, r
    assert r["st_dir_exists"] is True, r
    # Writes go to the ephemeral tmpfs and succeed (but vanish on exit).
    assert r["key_write"] == "CREATED", r
    assert r["db_write"] == "CREATED", r

    # The REAL dirs are untouched: content unchanged, no probe file persisted.
    assert (config_dir / "evidence_mac.key").read_bytes() == b"K" * 32
    assert (state_dir / "argent.db").read_bytes() == b"D" * 32
    assert sorted(p.name for p in config_dir.iterdir()) == ["evidence_mac.key"]
    assert sorted(p.name for p in state_dir.iterdir()) == ["argent.db"]


# ---------------------------------------------------------------------------
# (c) fail-closed: missing bwrap -> no process started
# ---------------------------------------------------------------------------

def test_fail_closed_missing_bwrap_starts_no_process(tmp_path, monkeypatch):
    called = {}

    class FakePopen:
        pid = 1

    def fake_popen(argv, **kw):
        called["argv"] = argv
        return FakePopen()

    backend = SystemdRunScopeBackend(
        popen_fn=fake_popen,
        sandbox_bwrap="/nonexistent/bwrap",  # simulate bwrap missing
        sandbox_dirs={
            "config_dir": tmp_path / "cfg",
            "state_dir": tmp_path / "st",
            "openclaw_dir": tmp_path / "oc",
            "cwd": tmp_path / "cwd",
        },
    )
    monkeypatch.setattr(backend, "_move_into_cgroup", lambda pid, cg: True)
    with pytest.raises(ScopeCreateError):
        backend.start_in_scope(scope=_scope(), command=["openclaw", "agent"])
    # No unwrapped spawn: Popen must never have been invoked.
    assert "argv" not in called


# ---------------------------------------------------------------------------
# (d) short-timeout sandboxed spawn -> the whole tree dies (rc 124 == TIMEOUT)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_BWRAP, reason="bwrap unavailable on this host")
def test_short_timeout_sandboxed_spawn_kills_tree(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".config" / "argent"
    state_dir = home / ".local" / "state" / "argent"
    openclaw_dir = home / ".openclaw"
    cwd = tmp_path / "worktree"
    for d in (config_dir, state_dir, openclaw_dir, cwd):
        d.mkdir(parents=True, exist_ok=True)

    # Compose exactly as the enforcer does: timeout wrapper + bwrap sandbox.
    timeout_cmd = TimeoutRunner(kill_after_seconds=1).wrap(
        ["python3", "-c", "import time; time.sleep(30)"], 2,
    )
    argv = build_agent_sandbox_argv(
        timeout_cmd, config_dir=config_dir, state_dir=state_dir,
        openclaw_dir=openclaw_dir, cwd=cwd,
    )
    start = time.monotonic()
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30,
                          cwd=str(cwd))
    elapsed = time.monotonic() - start
    # timeout is authoritative INSIDE the sandbox: the child tree is killed and
    # the exit status is the timeout wrapper's 124 (TIMEOUT).
    assert proc.returncode == 124, (proc.returncode, proc.stderr)
    assert elapsed < 10, f"tree not killed within the timeout window: {elapsed}s"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _contains(argv, *seq):
    """True iff ``seq`` appears as a consecutive subsequence of ``argv``."""
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
