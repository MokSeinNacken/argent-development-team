"""Phase I3-A — external-credential isolation fix (I3-A §31).

Closes the live defect where the agent-dispatch sandbox left ``~/.config/gh``
(the authenticated GitHub credential home — ``hosts.yml`` mode 0600 with the
account token, plus ``config.yml``) VISIBLE read-only inside the sandbox because
only ``~/.config/argent`` and ``~/.local/state/argent`` were masked.

Tests:

(a) deterministic: the mask list is an explicit parameter; the default
    production set covers ``~/.config/gh`` (+ ``~/.git-credentials`` /
    ``~/.netrc`` when present) WITHOUT weakening the G3 narrowing;
(b) live real-bwrap probe (skip if bwrap unavailable): with the FIXED argv
    against the real host, ``~/.config/gh/hosts.yml`` + ``config.yml`` are
    ABSENT inside the sandbox while the supervisor-side (outside) read still
    works.  The before-fix exposure is documented (Main already live-probed it).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from argent_core.execution_scope import (
    build_agent_sandbox_argv,
    resolve_credential_mask_paths,
)

_HAS_BWRAP = shutil.which("bwrap") is not None


# ---------------------------------------------------------------------------
# (a) deterministic argv construction
# ---------------------------------------------------------------------------

def _contains(argv, *seq):
    for i in range(len(argv) - len(seq) + 1):
        if list(argv[i:i + len(seq)]) == list(seq):
            return True
    return False


def test_resolve_credential_mask_paths_includes_gh_dir(tmp_path):
    home = tmp_path / "home"
    paths = resolve_credential_mask_paths(home=home, env={})
    assert Path(home, ".config", "gh") in paths
    # git-credentials / netrc are absent under this fresh home -> not included.
    assert Path(home, ".git-credentials") not in paths
    assert Path(home, ".netrc") not in paths


def test_resolve_credential_mask_paths_includes_present_files(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".git-credentials").write_text("https://u:t@example.com")
    (home / ".netrc").write_text("machine x login y password z")
    (home / ".ssh").mkdir()
    paths = resolve_credential_mask_paths(home=home, env={})
    assert Path(home, ".config", "gh") in paths
    assert Path(home, ".git-credentials") in paths
    assert Path(home, ".netrc") in paths
    assert Path(home, ".ssh") in paths


def test_resolve_credential_mask_paths_ssh_conditional(tmp_path):
    # A future ~/.ssh is covered ONLY when present (LOW-9), like .netrc.
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    paths = resolve_credential_mask_paths(home=home, env={})
    assert Path(home, ".ssh") not in paths
    (home / ".ssh").mkdir()
    paths = resolve_credential_mask_paths(home=home, env={})
    assert Path(home, ".ssh") in paths


def test_sandbox_argv_masks_credential_dirs(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".config" / "argent"
    state_dir = home / ".local" / "state" / "argent"
    openclaw_dir = home / ".openclaw"
    gh_dir = home / ".config" / "gh"
    credential_dirs = [gh_dir, home / ".git-credentials", home / ".netrc"]

    argv = build_agent_sandbox_argv(
        ["true"],
        config_dir=config_dir, state_dir=state_dir,
        openclaw_dir=openclaw_dir,
        credential_dirs=credential_dirs,
    )
    # The trusted dirs are still masked (G3 narrowing preserved).
    assert _contains(argv, "--tmpfs", str(config_dir))
    assert _contains(argv, "--tmpfs", str(state_dir))
    # The external-credential homes are ADDITIONALLY masked.
    assert _contains(argv, "--tmpfs", str(gh_dir))
    assert _contains(argv, "--tmpfs", str(home / ".git-credentials"))
    assert _contains(argv, "--tmpfs", str(home / ".netrc"))
    # Read-only root still present (never weakened).
    assert _contains(argv, "--ro-bind", "/", "/")


def test_sandbox_argv_no_credential_dirs_by_default(tmp_path):
    # Without explicit credential_dirs the builder emits NO credential masks
    # (the production backend resolves and passes them explicitly).
    argv = build_agent_sandbox_argv(
        ["true"], config_dir="/cfg", state_dir="/st", openclaw_dir="/oc")
    assert "--tmpfs" in argv  # trusted dirs only (+ /tmp)
    assert _contains(argv, "--tmpfs", "/cfg")
    assert _contains(argv, "--tmpfs", "/st")


# ---------------------------------------------------------------------------
# (b) live real-bwrap probe against the real host
# ---------------------------------------------------------------------------

_PROBE = """\
import os, json
gh = os.environ["GH_DIR"]
hosts = os.path.join(gh, "hosts.yml")
cfg = os.path.join(gh, "config.yml")
r = {}
def rd(p):
    try:
        os.close(os.open(p, os.O_RDONLY))
        return "OPENED"
    except FileNotFoundError:
        return "ENOENT"
    except Exception as e:  # noqa: BLE001
        return type(e).__name__
r["hosts_read"] = rd(hosts)
r["config_read"] = rd(cfg)
r["hosts_exists"] = os.path.exists(hosts)
r["config_exists"] = os.path.exists(cfg)
r["gh_dir_exists"] = os.path.isdir(gh)
print(json.dumps(r))
"""


@pytest.mark.skipif(not _HAS_BWRAP, reason="bwrap unavailable on this host")
def test_live_probe_gh_credentials_absent_inside_sandbox():
    real_gh = Path.home() / ".config" / "gh"
    hosts = real_gh / "hosts.yml"
    cfg = real_gh / "config.yml"
    if not hosts.exists() or not cfg.exists():
        pytest.skip("real ~/.config/gh/hosts.yml + config.yml not present")

    # Supervisor-side (outside the sandbox) MUST still read the credential dir.
    assert hosts.exists() and os.access(str(hosts), os.R_OK)
    assert cfg.exists() and os.access(str(cfg), os.R_OK)

    cred_dirs = list(resolve_credential_mask_paths())
    assert real_gh in cred_dirs  # the fixed mask set covers ~/.config/gh

    argv = build_agent_sandbox_argv(
        ["/usr/bin/python3", "-c", _PROBE],
        config_dir=Path.home() / ".config" / "argent",
        state_dir=Path.home() / ".local" / "state" / "argent",
        openclaw_dir=Path.home() / ".openclaw",
        credential_dirs=cred_dirs,
    )
    env = {"GH_DIR": str(real_gh),
           "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30,
                          env=env)
    assert proc.returncode == 0, proc.stderr
    r = json.loads(proc.stdout)

    # INSIDE the fixed sandbox: the credential files are ABSENT.
    assert r["hosts_read"] == "ENOENT", r
    assert r["config_read"] == "ENOENT", r
    assert r["hosts_exists"] is False, r
    assert r["config_exists"] is False, r
    # The masked dir is an EMPTY tmpfs mount (present but empty).
    assert r["gh_dir_exists"] is True, r

    # Supervisor-side (outside) is untouched and still readable.
    assert hosts.exists() and os.access(str(hosts), os.R_OK)
    assert cfg.exists() and os.access(str(cfg), os.R_OK)


@pytest.mark.skipif(not _HAS_BWRAP, reason="bwrap unavailable on this host")
def test_live_probe_before_fix_exposure_documented():
    # Document (not re-run) the before-fix exposure: the ORIGINAL sandbox argv
    # (no credential_dirs) left ~/.config/gh visible read-only.  Re-prove the
    # contrast deterministically: without the credential mask, the ro-root
    # still exposes the file (this is exactly the pre-fix behavior).
    real_gh = Path.home() / ".config" / "gh"
    hosts = real_gh / "hosts.yml"
    if not hosts.exists():
        pytest.skip("real ~/.config/gh/hosts.yml not present")
    argv = build_agent_sandbox_argv(
        ["/usr/bin/python3", "-c", _PROBE],
        config_dir=Path.home() / ".config" / "argent",
        state_dir=Path.home() / ".local" / "state" / "argent",
        openclaw_dir=Path.home() / ".openclaw",
        credential_dirs=(),  # the OLD (buggy) mask set
    )
    env = {"GH_DIR": str(real_gh),
           "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30,
                          env=env)
    assert proc.returncode == 0, proc.stderr
    r = json.loads(proc.stdout)
    # Before-fix: hosts.yml is VISIBLE (OPENED) inside the sandbox.
    assert r["hosts_read"] == "OPENED", r
