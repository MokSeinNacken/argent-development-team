"""bwrap test-runner tests (SPEC V2B §3).

Deterministic: these tests run real sandboxed pytest runs (no skips/xfails)
wherever ``bwrap`` is available.  On GitHub CI ``bwrap`` is provisioned via the
``apt-get install bubblewrap`` step of ``.github/workflows/ci.yml``; on the
development host it is already installed.  Each test builds a tiny workspace in
``tmp_path`` and runs it through :func:`run_tests`.
"""

import os
from pathlib import Path

from argent_core import SandboxResult, run_tests
from argent_core.sandbox_runner import build_command

E2E_FIXTURE = Path(__file__).resolve().parent.parent / "e2e-fixture"


def _workspace(tmp_path, files):
    """Create ``tmp_path/tests`` and write ``files`` (name -> source)."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    for name, source in files.items():
        (tests_dir / name).write_text(source)
    return tmp_path


# ------------------------------------------------------------ §3 bwrap runs


def test_bwrap_runs_pytest(tmp_path):
    ws = _workspace(tmp_path, {"test_ok.py": "def test_ok():\n    assert 1 + 1 == 2\n"})
    res = run_tests(str(ws))
    assert res.exit_code == 0
    assert res.timed_out is False
    assert "passed" in res.stdout_bounded


def test_bwrap_runs_on_empty_fixture_skeleton(tmp_path):
    # An empty tests/ dir: pytest runs and reports "no tests" (exit code 5)
    # rather than failing to launch.  Uses a tmp workspace so the shared
    # e2e-fixture (filled by the real E2E runs) is never assumed empty.
    ws = tmp_path / "ws"
    (ws / "tests").mkdir(parents=True)
    cache = ws / "tests" / ".pytest_cache"
    try:
        res = run_tests(str(ws))
        assert res.timed_out is False
        assert res.exit_code == 5
        assert "no tests ran" in (res.stdout_bounded + res.stderr_bounded).lower()
    finally:
        # The sandbox mounts the workspace read-only and runs pytest with
        # -p no:cacheprovider, so no .pytest_cache dir is written; the guard
        # below is belt-and-suspenders cleanup (a no-op in practice).
        import shutil

        if cache.exists():
            shutil.rmtree(cache)


def test_sandbox_cannot_overwrite_product_file(tmp_path):
    """FIX 1 (sandbox escape): the workspace is ro-bound, so a QA test that
    tries to write /workspace/product.py must fail and leave the host file
    unchanged."""
    ws = _workspace(
        tmp_path,
        {
            "test_write.py": (
                "def test_write_product():\n"
                "    with open('/workspace/product.py', 'w') as fh:\n"
                "        fh.write('MUTATED')\n"
            )
        },
    )
    (ws / "product.py").write_text("ORIGINAL")
    res = run_tests(str(ws))
    assert res.exit_code != 0, res.stdout_bounded + res.stderr_bounded
    assert (ws / "product.py").read_text() == "ORIGINAL"


# ---------------------------------------------------------- §3 host invisible


def test_host_paths_invisible(tmp_path):
    ws = _workspace(
        tmp_path,
        {
            "test_sandbox.py": (
                "import os\n"
                "def test_host_invisible():\n"
                "    assert not os.path.exists('/home/pc')\n"
                "    assert not os.path.exists('/mnt/c')\n"
                "    assert not os.path.exists('/root/secret')\n"
                "    assert os.path.exists('/workspace')\n"
                "    assert os.path.exists('/etc/passwd')\n"
            )
        },
    )
    res = run_tests(str(ws))
    assert res.exit_code == 0, res.stderr_bounded
    assert res.timed_out is False


# ---------------------------------------------------------------- §3 no net


def test_no_network(tmp_path):
    ws = _workspace(
        tmp_path,
        {
            "test_net.py": (
                "import socket\n"
                "def test_no_network():\n"
                "    try:\n"
                "        socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
                "        connected = True\n"
                "    except OSError:\n"
                "        connected = False\n"
                "    assert connected is False\n"
            )
        },
    )
    res = run_tests(str(ws))
    assert res.exit_code == 0, res.stderr_bounded


# --------------------------------------------------------------- §3 limits


def test_fork_bomb_hits_process_limit(tmp_path):
    ws = _workspace(
        tmp_path,
        {
            "test_fork.py": (
                "import os\n"
                "import signal\n"
                "def test_process_limit():\n"
                "    children = []\n"
                "    try:\n"
                "        while True:\n"
                "            pid = os.fork()\n"
                "            if pid == 0:\n"
                "                import time\n"
                "                time.sleep(60)\n"
                "                os._exit(0)\n"
                "            children.append(pid)\n"
                "    except OSError as e:\n"
                "        for c in children:\n"
                "            try:\n"
                "                os.kill(c, signal.SIGKILL)\n"
                "            except OSError:\n"
                "                pass\n"
                "        for c in children:\n"
                "            try:\n"
                "                os.waitpid(c, 0)\n"
                "            except OSError:\n"
                "                pass\n"
                "        assert e.errno == 11  # EAGAIN\n"
                "        return\n"
                "    assert False, 'process limit not enforced'\n"
            )
        },
    )
    res = run_tests(str(ws), limits={"timeout": 60})
    assert res.exit_code == 0, res.stderr_bounded
    assert res.timed_out is False


# ------------------------------------------------------------- §3 timeout


def test_timeout_reported(tmp_path):
    ws = _workspace(
        tmp_path,
        {"test_slow.py": "import time\n\ndef test_slow():\n    time.sleep(60)\n"},
    )
    res = run_tests(str(ws), limits={"timeout": 3})
    assert res.timed_out is True
    assert res.exit_code == 124


# ------------------------------------------------------- §3 output bounding


def test_output_bounded(tmp_path):
    ws = _workspace(
        tmp_path,
        {"test_big.py": "def test_big():\n    print('x' * 200000)\n"},
    )
    res = run_tests(str(ws), pytest_args=["-s"])
    assert 0 < len(res.stdout_bounded) <= 65536
    assert len(res.stderr_bounded) <= 65536


# ----------------------------------------------------- §3 SandboxResult fields


def test_sandbox_result_fields(tmp_path):
    ws = _workspace(tmp_path, {"test_ok.py": "def test_ok():\n    assert True\n"})
    res = run_tests(str(ws))
    assert isinstance(res, SandboxResult)
    assert isinstance(res.exit_code, int)
    assert isinstance(res.stdout_bounded, str)
    assert isinstance(res.stderr_bounded, str)
    assert isinstance(res.timed_out, bool)
    assert isinstance(res.wall_seconds, float)
    assert res.wall_seconds >= 0.0


# ------------------------------------------- command-line shape (no run)


def test_build_command_contains_isolation_flags(tmp_path):
    cmd = build_command(str(tmp_path))
    joined = " ".join(cmd)
    for flag in (
        "--ro-bind", "/usr",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", "/home",
        "--tmpfs", "/root",
        "--unshare-net",
        "--unshare-pid",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "prlimit",
        "--nproc=64",
        "--as=536870912",
        "--cpu=30",
        "--fsize=10485760",
        "timeout", "120",
        "python3", "-m", "pytest", "/workspace/tests", "-q",
    ):
        assert flag in joined


def test_build_command_limits_configurable(tmp_path):
    cmd = build_command(str(tmp_path), limits={"timeout": 7, "nproc": 11})
    joined = " ".join(cmd)
    assert "timeout 7" in joined
    assert "--nproc=11" in joined


def test_build_command_workspace_ro_bind(tmp_path):
    """FIX 1: the workspace is mounted read-only, pytest runs without cache,
    and bytecode writing is disabled (no write access needed)."""
    cmd = build_command(str(tmp_path))
    assert "--bind" not in cmd  # no read-write workspace bind remains
    i = cmd.index("/workspace")
    assert cmd[i - 2] == "--ro-bind"
    assert "PYTHONDONTWRITEBYTECODE" in cmd
    assert "-p" in cmd and "no:cacheprovider" in cmd
