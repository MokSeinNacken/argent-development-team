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
import tempfile
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
    agent_id = "argent-lead"
    runtime_dirs = [openclaw_dir / "agents" / agent_id,
                    openclaw_dir / "workspace" / agent_id]

    argv = build_agent_sandbox_argv(
        ["openclaw", "agent", "--agent", agent_id],
        config_dir=config_dir, state_dir=state_dir,
        openclaw_dir=openclaw_dir, cwd=cwd,
        writable_runtime_dirs=runtime_dirs,
    )

    assert argv[0] == "bwrap"
    # Read-only root.
    assert _contains(argv, "--ro-bind", "/", "/")
    # G3-A narrows the policy: the whole ~/.openclaw is NO LONGER rw-bind-mounted.
    assert not _contains(argv, "--bind", str(openclaw_dir), str(openclaw_dir))
    # The two per-agent runtime dirs ARE rw-bind-mounted (the only ~/.openclaw
    # write surface).
    assert _contains(argv, "--bind", str(runtime_dirs[0]), str(runtime_dirs[0]))
    assert _contains(argv, "--bind", str(runtime_dirs[1]), str(runtime_dirs[1]))
    # The inherited working directory is rw-bound.
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
    assert argv[idx + 1:] == ["openclaw", "agent", "--agent", agent_id]


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
    backend.start_in_scope(
        scope=scope, command=["openclaw", "agent", "--agent", "argent-lead"],
    )

    argv = captured["argv"]
    assert argv[0] == "bwrap"
    assert _contains(argv, "--tmpfs", str(config_dir))
    assert _contains(argv, "--tmpfs", str(state_dir))
    # G3-A: whole ~/.openclaw is not rw-bound; only the per-agent runtime dirs.
    assert not _contains(argv, "--bind", str(openclaw_dir), str(openclaw_dir))
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
# (b2) G3-A adversarial probe — ~/.openclaw narrowed to per-agent runtime dirs
# ---------------------------------------------------------------------------

_G3_PROBE = """\
import os, sys, json
oc_json = sys.argv[1]      # <home>/.openclaw/openclaw.json (must be read-only)
agents_dir = sys.argv[2]   # <home>/.openclaw/agents/<id> (rw)
workspace_dir = sys.argv[3]  # <home>/.openclaw/workspace/<id> (rw)
cwd_probe = sys.argv[4]    # <cwd>/probe-write.txt (rw)
r = {}

def rd(p):
    try:
        os.close(os.open(p, os.O_RDONLY))
        return "OPENED"
    except FileNotFoundError:
        return "ENOENT"
    except Exception as e:  # noqa: BLE001
        return type(e).__name__

r["oc_json_read"] = rd(oc_json)

# Writing to ~/.openclaw/openclaw.json must FAIL (read-only root).
try:
    with open(oc_json, "w") as fh:
        fh.write("pwned")
    r["oc_json_write"] = "WROTE"
except Exception as e:  # noqa: BLE001
    r["oc_json_write"] = type(e).__name__

# Writing into ~/.openclaw/agents/<id>/sessions must SUCCEED (rw bind).
session_dir = os.path.join(agents_dir, "sessions")
os.makedirs(session_dir, exist_ok=True)
sp = os.path.join(session_dir, "probe.txt")
try:
    with open(sp, "w") as fh:
        fh.write("ok")
    r["agent_session_write"] = "WROTE"
except Exception as e:  # noqa: BLE001
    r["agent_session_write"] = type(e).__name__

# Writing into the cwd worktree must SUCCEED (rw bind).
try:
    with open(cwd_probe, "w") as fh:
        fh.write("ok")
    r["cwd_write"] = "WROTE"
except Exception as e:  # noqa: BLE001
    r["cwd_write"] = type(e).__name__

print(json.dumps(r))
"""


@pytest.mark.skipif(not _HAS_BWRAP, reason="bwrap unavailable on this host")
def test_g3_sandbox_narrows_openclaw_to_per_agent_runtime_dirs():
    # The fixture "home" must live OUTSIDE /tmp: the sandbox masks /tmp with an
    # empty tmpfs (plus the two trusted dirs), so anything under pytest's
    # tmp_path (which is /tmp) would be invisible to the child.  A home under
    # the worktree is covered by the read-only root, exactly like the real
    # ~/.openclaw under /home.  Use a throwaway dir under the worktree and
    # clean it up afterwards.
    repo_root = Path(__file__).resolve().parents[1]
    base = Path(tempfile.mkdtemp(prefix=".g3-sandbox-home-", dir=str(repo_root)))
    try:
        home = base / "home"
        config_dir = home / ".config" / "argent"
        state_dir = home / ".local" / "state" / "argent"
        openclaw_dir = home / ".openclaw"
        cwd = base / "worktree"
        agent_id = "argent-lead"
        runtime_dirs = [openclaw_dir / "agents" / agent_id,
                        openclaw_dir / "workspace" / agent_id]
        for d in (config_dir, state_dir, openclaw_dir, cwd, *runtime_dirs):
            d.mkdir(parents=True, exist_ok=True)

        # Seed an authoritative config file under ~/.openclaw: the child must be
        # able to READ it (the real openclaw CLI needs it) but never WRITE it.
        oc_json = openclaw_dir / "openclaw.json"
        oc_json.write_text('{"trusted": true}\n', encoding="utf-8")

        probe = cwd / "g3_probe.py"
        probe.write_text(_G3_PROBE, encoding="utf-8")
        cwd_probe = cwd / "probe-write.txt"

        argv = build_agent_sandbox_argv(
            ["python3", str(probe), str(oc_json), str(runtime_dirs[0]),
             str(runtime_dirs[1]), str(cwd_probe)],
            config_dir=config_dir, state_dir=state_dir,
            openclaw_dir=openclaw_dir, cwd=cwd,
            writable_runtime_dirs=runtime_dirs,
        )
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30,
                              cwd=str(cwd))
        assert proc.returncode == 0, proc.stderr
        r = json.loads(proc.stdout)

        # (1) openclaw.json is READABLE (config visible read-only).
        assert r["oc_json_read"] == "OPENED", r
        # (2) writing to openclaw.json FAILS (read-only root).
        assert r["oc_json_write"] != "WROTE", r
        # (3) writing into the per-agent sessions dir SUCCEEDS (rw bind).
        assert r["agent_session_write"] == "WROTE", r
        # (5) writing into the cwd worktree SUCCEEDS (rw bind).
        assert r["cwd_write"] == "WROTE", r

        # The REAL ~/.openclaw/openclaw.json is untouched.
        assert oc_json.read_text(encoding="utf-8") == '{"trusted": true}\n'
        # The probe file persisted in the per-agent sessions dir (rw bind real).
        assert (runtime_dirs[0] / "sessions" / "probe.txt").exists()
        # The cwd write persisted.
        assert cwd_probe.exists()
    finally:
        shutil.rmtree(base, ignore_errors=True)


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
        backend.start_in_scope(
            scope=_scope(),
            command=["openclaw", "agent", "--agent", "argent-lead"],
        )
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
