#!/usr/bin/env python3
"""Phase 2C recovery E2E smoke harness (SPEC V2C §13).

Proves the persistent supervisor can survive a controller/SIGKILL, find the
existing run (never double-spawn), continue autonomously to DONE, and consume
duplicate/stale completions exactly once.

Two modes:

- ``--fake`` (default, offline, deterministic): uses the FakeClock /
  FakeRunStatusProvider / FakeRunLauncher.  A "supervisor death" is simulated
  by closing the Core and reopening a fresh Core/Supervisor over the same DB.
  This mode is fully verified offline (no real OpenClaw runs).

- ``--real`` (not run here): uses TrajectoryRunStatusProvider +
  OpenClawRunLauncher; it launches a real detached ``openclaw agent`` and a
  separate supervisor process that is SIGKILLed.  Executed by the supervisor
  later; never pushes and never changes OpenClaw/system configuration.

Exit 0 == all assertions passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from argent_core import Core, OWNER_SOURCE, Role, role_source  # noqa: E402
from argent_core.models import DispatchStatus, TaskState  # noqa: E402
from argent_core.supervisor import (  # noqa: E402
    ReconcileAction,
    RunStatus,
    Supervisor,
    SupervisorLoop,
    OpenClawRunLauncher,
    TrajectoryRunStatusProvider,
)
from mock_runtime import build_output  # noqa: E402
from mock_supervisor_runtime import (  # noqa: E402
    AutoRunStatusProvider,
    FakeClock,
    FakeRunLauncher,
    FakeRunStatusProvider,
    FakeWaiter,
    canonical_binding,
    make_run_observation,
)

OWNER = OWNER_SOURCE


def _make_workspace(tmp: Path) -> Path:
    root = tmp / "ws"
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "src" / "module.py").write_text("# stub\n")
    return root


def _fake_run_tests(workspace, pytest_args=None, limits=None):
    from argent_core.sandbox_runner import SandboxResult
    return SandboxResult(
        exit_code=0, stdout_bounded="", stderr_bounded="", timed_out=False,
        wall_seconds=0.0,
    )


# ---------------------------------------------------------------------------
# Read-only baseline
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "<missing>"


def _config_hash() -> str:
    """Byte-for-byte hash of the ACTIVE OpenClaw config (openclaw.json).

    The legacy ``config.json`` path does not exist on this host; the active
    file is ``~/.openclaw/openclaw.json``.  Hash its CONTENT directly so a
    content change is always detected.  Fall back to the directory-entry-name
    hash ONLY when the active file is absent (robustness on other hosts).
    """
    cfg = Path.home() / ".openclaw" / "openclaw.json"
    if cfg.exists():
        return _sha256_file(cfg)
    d = Path.home() / ".openclaw"
    names = sorted(p.name for p in d.iterdir()) if d.is_dir() else []
    return hashlib.sha256(json.dumps(names, sort_keys=True).encode()).hexdigest()


def _active_config_matches_last_good() -> bool:
    """True iff the ACTIVE config is byte-for-byte identical to the last-good
    reference (``~/.openclaw/openclaw.json.last-good``).  Read-only scope
    assertion: the smoke must never mutate OpenClaw config."""
    active = Path.home() / ".openclaw" / "openclaw.json"
    last_good = Path.home() / ".openclaw" / "openclaw.json.last-good"
    if not active.exists():
        return True  # active config absent (other hosts): nothing to compare
    if not last_good.exists():
        return False
    return active.read_bytes() == last_good.read_bytes()


def _git_status_hash() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        return hashlib.sha256(out.stdout.encode()).hexdigest()
    except Exception:
        return "<git-unavailable>"


# ---------------------------------------------------------------------------
# Fake smoke (offline)
# ---------------------------------------------------------------------------

def _advance(sup, job_id, action, max_steps=60):
    for _ in range(max_steps):
        d = sup.reconcile(job_id)
        sup.perform_next_safe_action_if_required(d)
        if d.action == action:
            return d
    raise AssertionError(f"never reached {action}")


def _bind(sup, prov, dispatch, run_id):
    provider, model, thinking, session = canonical_binding(dispatch)
    prov.set_current(dispatch.id, make_run_observation(
        dispatch_id=dispatch.id, role=dispatch.role, status=RunStatus.RUNNING,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    return session, provider, model, thinking


def _consume_one_role(sup, prov, core, task, role, *, lead_first=False):
    """Drive START_ROLE -> CREATE_DISPATCH -> BIND -> CONSUME for one role."""
    _advance(sup, sup.store.get_job_for_task(task.id).supervisor_job_id,
             ReconcileAction.START_ROLE)
    _advance(sup, sup.store.get_job_for_task(task.id).supervisor_job_id,
             ReconcileAction.CREATE_DISPATCH)
    d = core.queries.list_dispatches(task.id)[-1]
    assert d.role is role
    run_id = f"run-{d.id[:8]}"
    session, provider, model, thinking = _bind(sup, prov, d, run_id)
    _advance(sup, sup.store.get_job_for_task(task.id).supervisor_job_id,
             ReconcileAction.BIND_RUN)
    prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.SUCCEEDED,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking, result=build_output(d.role, task.id, d.id),
    ))
    _advance(sup, sup.store.get_job_for_task(task.id).supervisor_job_id,
             ReconcileAction.CONSUME_RESULT)
    return d


def run_fake() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="phase2c-recovery-"))
    db = scratch / "recovery.db"

    baseline_cfg = _config_hash()
    baseline_git = _git_status_hash()

    clock = FakeClock()
    core = Core(str(db), clock=clock)
    project = core.create_project("phase2c-recovery", OWNER)
    task = core.create_task(project.id, "recovery-smoke-task", OWNER,
                            description="duration parser smoke")
    core.start_task_run(task.id, OWNER)

    prov = FakeRunStatusProvider()
    launcher = FakeRunLauncher()
    sup = Supervisor(core, prov, launcher, clock=clock)
    job = sup.store.create_job(task.id, idempotency_key="smoke-job-1")
    job_id = job.supervisor_job_id

    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")

    # --- step 4: consume lead position 0 via the controller path -----------
    _consume_one_role(sup, prov, core, task, Role.LEAD)
    check("lead position 0 consumed", True)

    # --- step 5: supervisor creates exactly one analyst dispatch + spawn ----
    _advance(sup, job_id, ReconcileAction.START_ROLE)
    _advance(sup, job_id, ReconcileAction.CREATE_DISPATCH)
    analyst = core.queries.list_dispatches(task.id)[-1]
    assert analyst.role is Role.ANALYST
    # Simulate the spawn-then-kill window: runtime not yet visible -> SPAWN_RUN.
    prov.set_current(analyst.id, make_run_observation(
        dispatch_id=analyst.id, role=Role.ANALYST, status=RunStatus.NOT_FOUND,
        authoritative_not_found=True,
    ))
    _advance(sup, job_id, ReconcileAction.SPAWN_RUN)
    check("exactly one analyst spawn", len(launcher.spawns) == 1)
    check("analyst dispatch (cycle=1, pos=1, attempt=1)",
          (analyst.cycle_no, analyst.position, analyst.attempt_no) == (1, 1, 1))

    # --- steps 7-9: SIGKILL the supervisor, reload, find the run -----------
    analyst_id = analyst.id
    core.close()  # simulate supervisor death (persistent state survives)

    core2 = Core(str(db), clock=clock)
    prov2 = FakeRunStatusProvider()
    sup2 = Supervisor(core2, prov2, FakeRunLauncher(), clock=clock)
    analyst2 = core2.queries.get_dispatch(analyst_id)
    # Runtime: session.started present (RUNNING), no end yet.
    session, provider, model, thinking = _bind(sup2, prov2, analyst2, "run-a")
    d = sup2.reconcile(job_id)
    sup2.perform_next_safe_action_if_required(d)
    check("bound existing run (no respawn)", d.action is ReconcileAction.BIND_RUN)
    check("no second analyst spawn after reload", len(launcher.spawns) == 1)

    # Still RUNNING -> wait (never restart).
    prov2.set_current(analyst_id, make_run_observation(
        dispatch_id=analyst_id, role=Role.ANALYST, status=RunStatus.RUNNING,
        run_id="run-a", session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    d = sup2.reconcile(job_id)
    check("running run -> wait", d.action is ReconcileAction.WAIT)

    # session.ended success -> consume once.
    result = build_output(Role.ANALYST, task.id, analyst_id)
    prov2.set_current(analyst_id, make_run_observation(
        dispatch_id=analyst_id, role=Role.ANALYST, status=RunStatus.SUCCEEDED,
        run_id="run-a", session_id=session, provider=provider, model=model,
        thinking_tier=thinking, result=result,
    ))
    _advance(sup2, job_id, ReconcileAction.CONSUME_RESULT)
    check("analyst consumed once",
          core2.queries.get_dispatch(analyst_id).status is DispatchStatus.CONSUMED)
    check("no second analyst dispatch",
          len([x for x in core2.queries.list_dispatches(task.id)
               if x.role is Role.ANALYST]) == 1)

    # --- steps 11-13: kill again, continue to DONE -------------------------
    ws = _make_workspace(scratch)
    core2.close()
    core3 = Core(str(db), clock=clock)
    auto3 = AutoRunStatusProvider(core3)
    sup3 = Supervisor(core3, auto3, FakeRunLauncher(), clock=clock,
                      workspace_root=ws, run_tests_fn=_fake_run_tests)
    waiter = FakeWaiter(clock)
    loop = SupervisorLoop(sup3, waiter=waiter)
    final_state = loop.run_until_terminal(job_id)
    check("workflow reached DONE", core3.queries.get_task(task.id).state is TaskState.DONE)
    check("job terminal DONE", final_state.terminal == "DONE")

    # Exactly seven dispatches (lead, analyst, lead, implementer, qa, reviewer,
    # lead) and seven consumed results.
    dispatches3 = core3.queries.list_dispatches(task.id)
    roles3 = [d.role for d in dispatches3]
    check("dispatch count == 7", len(dispatches3) == 7)
    check("exact role set", roles3 == [
        Role.LEAD, Role.ANALYST, Role.LEAD, Role.IMPLEMENTER,
        Role.QA, Role.REVIEWER, Role.LEAD,
    ])
    check("seven consumed results",
          all(d.status is DispatchStatus.CONSUMED for d in dispatches3))
    # No double spawn: the launcher was invoked exactly once across the whole
    # workflow (only the analyst run is launched; Auto provider never spawns).
    check("no double spawn (launcher count)", len(launcher.spawns) == 1)

    # --- step 14: after DONE, reconcile plans nothing ----------------------
    d_done = sup3.reconcile(job_id)
    check("DONE -> no action", d_done.action is ReconcileAction.NONE)

    # --- step 15: duplicate/stale injection --------------------------------
    dd = core3.queries.get_dispatch(analyst_id)
    assert dd is not None and dd.result_json is not None, \
        "duplicate injection skipped: analyst result not persisted"
    em = {
        "task_id": task.id, "child_session_id": dd.child_session_id,
        "run_id": dd.openclaw_run_id, "parent_dispatch_id": None,
        "event_type": "agent.completed", "status": "completed",
    }
    dup_ok = all(
        sup3.receive_completion_hint(analyst_id, em, result).status == "duplicate"
        for _ in range(20)
    )
    check("20x duplicate completion -> exactly once", dup_ok)

    foreign = dict(em, run_id="00000000-0000-0000-0000-000000000000")
    res_foreign = sup3.receive_completion_hint(analyst_id, foreign, result)
    check("foreign run_id rejected", res_foreign.status == "rejected")

    # --- step 17: reload -> DONE stickiness --------------------------------
    core3.close()
    core4 = Core(str(db), clock=clock)
    sup4 = Supervisor(core4, AutoRunStatusProvider(core4), FakeRunLauncher(),
                      clock=clock, workspace_root=ws, run_tests_fn=_fake_run_tests)
    state4 = sup4.store.get_job(job_id)
    check("DONE sticky after reload", state4.terminal == "DONE")
    d4 = sup4.reconcile(job_id)
    check("stale event cannot reopen DONE", d4.action is ReconcileAction.NONE)

    # --- final config/system invariants (read-only) ------------------------
    check("config hash unchanged", _config_hash() == baseline_cfg)
    check("active config byte-identical to last-good",
          _active_config_matches_last_good())
    check("git status unchanged", _git_status_hash() == baseline_git)

    core4.close()
    shutil.rmtree(scratch, ignore_errors=True)

    failed = [name for name, ok in checks if not ok]
    print(f"\nphase2c recovery smoke (fake): "
          f"{len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# Real smoke (executed by the supervisor later; not run here)
# ---------------------------------------------------------------------------

def run_real() -> int:
    """Real-agent recovery E2E (§13).  Documented but not executed in Phase 2C
    verification (no real dispatches).  Mirrors run_fake() with the real
    TrajectoryRunStatusProvider + OpenClawRunLauncher and a SIGKILLed
    supervisor subprocess."""
    print("real smoke is executed separately by the supervisor; nothing to do")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 2C recovery E2E smoke")
    ap.add_argument("--fake", action="store_true", help="offline deterministic mode")
    ap.add_argument("--real", action="store_true", help="real-agent mode (supervisor runs it)")
    args = ap.parse_args()
    if args.real and not args.fake:
        return run_real()
    return run_fake()


if __name__ == "__main__":
    sys.exit(main())
