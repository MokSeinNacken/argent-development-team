"""bwrap test runner (SPEC V2B chapter 3).

Runs agent-authored test code inside a hardened bubblewrap namespace.  The
isolation is exactly the released command line of SPEC V2B §3 (ro-binds for
``/usr``, ``/lib``, ``/lib64``, ``/bin``, ``/sbin``, ``/etc``; ``--proc``;
``--dev``; tmpfs for ``/tmp``, ``/home``, ``/root``; ``--bind`` of the
workspace to ``/workspace``; ``--unshare-net``; ``--unshare-pid``;
``--die-with-parent``; ``--new-session``; ``--clearenv`` with a minimal env;
``prlimit`` resource caps; ``timeout``; ``python3 -m pytest``).

All parameters are configurable via ``limits`` / ``pytest_args``.  Output is
bounded to 64 KB per stream.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

MAX_OUTPUT_BYTES = 64 * 1024

# Defaults (SPEC V2B §3).  ``as`` is the address-space cap in bytes,
# ``fsize`` the max file size in bytes, ``cpu`` the CPU-seconds cap.
DEFAULT_LIMITS: dict = {
    "nproc": 64,
    "as": 536870912,
    "cpu": 30,
    "fsize": 10485760,
    "timeout": 120,
}


@dataclass
class SandboxResult:
    """Result of :func:`run_tests` (SPEC V2B §3)."""

    exit_code: int
    stdout_bounded: str
    stderr_bounded: str
    timed_out: bool
    wall_seconds: float


def _pytest_site_packages() -> Optional[str]:
    """Locate the host pytest site-packages directory.

    pytest is installed user-locally (SPEC V0); inside the namespace ``/home``
    is a tmpfs, so the interpreter cannot see it.  We ro-bind the site-packages
    directory read-only and expose it via ``PYTHONPATH``.  Returns ``None`` if
    pytest cannot be located (the run then simply fails to import pytest).
    """
    try:
        import pytest  # noqa: PLC0415

        pkg_dir = os.path.dirname(os.path.abspath(pytest.__file__))  # .../pytest
        return os.path.dirname(pkg_dir)  # .../site-packages
    except Exception:
        return None


def build_command(
    workspace_path, pytest_args: Optional[list] = None, limits: Optional[dict] = None
) -> list[str]:
    """Build the exact bwrap command line (SPEC V2B §3)."""
    merged = {**DEFAULT_LIMITS, **(limits or {})}
    site = _pytest_site_packages()

    cmd: list[str] = [
        "bwrap",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/sbin", "/sbin",
        "--ro-bind", "/etc", "/etc",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", "/home",
        "--tmpfs", "/root",
        "--bind", os.fspath(workspace_path), "/workspace",
        "--unshare-net",
        "--unshare-pid",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
        "--setenv", "HOME", "/tmp",
        "--setenv", "LANG", "C.UTF-8",
    ]
    if site is not None:
        cmd += [
            "--ro-bind", site, "/pytest-lib",
            "--setenv", "PYTHONPATH", "/pytest-lib",
        ]
    cmd += [
        "--chdir", "/workspace",
        "prlimit",
        f"--nproc={merged['nproc']}",
        f"--as={merged['as']}",
        f"--cpu={merged['cpu']}",
        f"--fsize={merged['fsize']}",
        "--",
        "timeout", str(merged["timeout"]),
        "python3", "-m", "pytest", "/workspace/tests", "-q",
    ]
    if pytest_args:
        cmd.extend(pytest_args)
    return cmd


def _bounded(data: Optional[bytes]) -> str:
    if not data:
        return ""
    text = data.decode("utf-8", errors="replace")
    if len(text) > MAX_OUTPUT_BYTES:
        return text[:MAX_OUTPUT_BYTES]
    return text


def run_tests(
    workspace_path, pytest_args: Optional[list] = None, limits: Optional[dict] = None
) -> SandboxResult:
    """Run the test suite in the bwrap namespace (SPEC V2B §3)."""
    merged = {**DEFAULT_LIMITS, **(limits or {})}
    cmd = build_command(workspace_path, pytest_args, limits)
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=merged["timeout"] + 15,
        )
    except subprocess.TimeoutExpired as exc:
        wall = time.monotonic() - start
        return SandboxResult(
            exit_code=-1,
            stdout_bounded=_bounded(exc.stdout),
            stderr_bounded=_bounded(exc.stderr),
            timed_out=True,
            wall_seconds=wall,
        )
    wall = time.monotonic() - start
    # timeout(1) returns 124 when it kills its child.
    return SandboxResult(
        exit_code=proc.returncode,
        stdout_bounded=_bounded(proc.stdout),
        stderr_bounded=_bounded(proc.stderr),
        timed_out=proc.returncode == 124,
        wall_seconds=wall,
    )
