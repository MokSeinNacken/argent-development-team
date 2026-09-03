"""Phase I1 §22 bounded real demo — TWO concurrent harmless scoped executions.

No LLM, no network, no production repos, no stress.  Runs two LIGHT read-only
style scope runs through the REAL ``SystemdRunScopeBackend`` with separate job
ids, separate fixture worktrees (temp dirs), separate process evidence, and
demonstrates aggregate admission via the REAL ResourceGovernor + host snapshot.

Skipped gracefully (exit 0 with a NOTE) when ``systemd-run --user --scope`` is
unavailable.
"""

import os
import subprocess
import sys
import tempfile
import threading
import time

# Allow execution as ``python3 docs/i1_demo.py`` from anywhere (repo root on
# sys.path so ``argent_core`` imports resolve).
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_MIB = 1024 * 1024

_LIMITS = {
    "memory_high_bytes": 32 * _MIB,
    "memory_max_bytes": 64 * _MIB,
    "swap_max_bytes": 16 * _MIB,
    "cpu_quota_percent": 100,
    "timeout_seconds": 30,
}


def _scope_available() -> bool:
    unit = "argent-i1-demo-avail"
    try:
        proc = subprocess.run(
            ["systemd-run", "--user", "--scope", f"--unit={unit}", "--", "true"],
            capture_output=True, text=True, timeout=10,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _run_one(job_id, dispatch_id, worktree, results):
    from argent_core.execution_scope import (
        ExecutionScope,
        SystemdRunScopeBackend,
        generate_scope_name,
        translate_limits_to_properties,
    )

    def _state(backend, unit_name):
        try:
            return backend._show(unit_name, ["ActiveState"]).get("ActiveState")
        except Exception as exc:
            return f"err:{type(exc).__name__}"

    backend = SystemdRunScopeBackend(sandbox_wrap=False)
    name = generate_scope_name("i1demo", job_id)
    scope = ExecutionScope(
        scope_name=name, unit_name=name + ".scope", cgroup_path="",
        job_id=job_id, dispatch_id=dispatch_id, resource_class="LIGHT",
        policy_version="1", effective_limits=_LIMITS, process_id=None,
        created_at="",
    )
    created = backend.create_scope(
        scope=scope, placeholder_command=["sleep", "30"],
        properties=translate_limits_to_properties(_LIMITS),
    )
    verified = backend.verify_scope(created)
    # Benign bounded work: write a marker into OUR OWN temp worktree, sleep.
    marker = os.path.join(worktree, "marker.txt")
    started = backend.start_in_scope(
        scope=created,
        command=["sh", "-c", f"echo ok > {marker} && sleep 2"],
    )
    bound = backend.verify_process_binding(started)
    backend.stop_placeholder(started)
    entry = {
        "job_id": job_id, "dispatch_id": dispatch_id,
        "scope_name": name, "cgroup_path": created.cgroup_path,
        "process_id": started.process_id,
        "verified_memory_max": verified.get("memory.max"),
        "bound": bound,
    }
    results.append(entry)
    # Wait for the transient scope to self-clean (best-effort, bounded).
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        st = _state(backend, started.unit_name)
        if st in ("inactive", "failed", ""):
            break
        time.sleep(0.2)
    entry["final_active_state"] = _state(backend, started.unit_name)


def main() -> int:
    if not _scope_available():
        print("NOTE: systemd-run --user --scope unavailable; demo skipped.")
        return 0

    print("== I1 bounded real demo: two concurrent scoped executions ==")

    # 1. Aggregate admission via the REAL governor + host snapshot.
    from argent_core.host_snapshot import HostSnapshotProvider
    from argent_core.resource_governor import ResourceGovernor
    from argent_core.resource_policy import ResourceClass

    provider = HostSnapshotProvider(
        active_jobs_reader=lambda: [("demo-A", "LIGHT"), ("demo-B", "LIGHT")],
    )
    snap = provider.capture(os.getcwd())
    gov = ResourceGovernor()
    d_light3 = gov.decide(resource_class=ResourceClass.LIGHT, snapshot=snap,
                          now_iso="2026-09-03T15:33:00+00:00")
    d_medium = gov.decide(resource_class=ResourceClass.MEDIUM, snapshot=snap,
                          now_iso="2026-09-03T15:33:00+00:00")
    print(f"host mem_total={snap.mem_total_bytes} avail={snap.mem_available_bytes}")
    print(f"active=LIGHT x2 -> LIGHT3 {d_light3.decision}/{d_light3.reason_code}, "
          f"MEDIUM {d_medium.decision}/{d_medium.reason_code}")

    # 2. Two concurrent scoped executions.
    tmp = tempfile.mkdtemp(prefix="argent-i1-demo-")
    wt_a = os.path.join(tmp, "wt-A"); os.makedirs(wt_a)
    wt_b = os.path.join(tmp, "wt-B"); os.makedirs(wt_b)

    results = []
    t_a = threading.Thread(target=_run_one, args=("demo-job-A", "d-A", wt_a, results))
    t_b = threading.Thread(target=_run_one, args=("demo-job-B", "d-B", wt_b, results))
    t_a.start(); t_b.start()
    # Observe a mid-point where BOTH scopes should be concurrently active.
    time.sleep(0.5)
    t_a.join(timeout=60); t_b.join(timeout=60)

    for r in sorted(results, key=lambda x: x["job_id"]):
        print(f"{r['job_id']}: pid={r['process_id']} cgroup={r['cgroup_path']} "
              f"memory.max={r['verified_memory_max']} bound={r['bound']} "
              f"final_state={r['final_active_state']}")

    assert len(results) == 2
    a, b = results[0], results[1]
    assert a["process_id"] != b["process_id"]
    assert a["cgroup_path"] != b["cgroup_path"]
    assert a["job_id"] != b["job_id"]
    assert os.path.exists(os.path.join(wt_a, "marker.txt"))
    assert os.path.exists(os.path.join(wt_b, "marker.txt"))
    for r in results:
        assert r["bound"] is True
        assert r["final_active_state"] in ("inactive", "failed", ""), r
    print("== DEMO OK: two scopes ran concurrently, distinct evidence, clean end ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
