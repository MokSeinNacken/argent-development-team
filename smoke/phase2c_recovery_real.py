#!/usr/bin/env python3
"""Phase 2C real E2E recovery smoke (SPEC V2C §13, --real).

Two-phase driver executed by the supervisor:

- Phase 1 (``--start``): fresh DB + TEMP FIXTURE COPY (repo fixture is never
  touched), task with an envelope-contract description, supervisor job, loop
  until the ANALYST dispatch has been spawned (SPAWN_RUN journal SUCCEEDED);
  then the process kills itself with os._exit(137) (simulated SIGKILL, no
  cleanup) — between spawn and result consume.
- Phase 2 (``--resume <paths.json>``): reload the same DB/fixture, reconcile()
  finds the existing analyst run (never double-spawns), waits, consumes the
  result EXACTLY ONCE, injects a duplicate completion (-> duplicate), then
  continues autonomously (lead -> analyst -> lead -> implementer -> qa ->
  reviewer -> lead) until DONE.

Usage:
  python3 smoke/phase2c_recovery_real.py --start
  python3 smoke/phase2c_recovery_real.py --resume /tmp/phase2c-real-smoke/paths.json

Exit 0 == all assertions passed. Never pushes, never changes OpenClaw/system
config, never touches the repo fixture.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from argent_core import Core, OWNER_SOURCE, Role, role_source  # noqa: E402
from argent_core.sandbox_runner import run_tests  # noqa: E402
from argent_core.supervisor import (  # noqa: E402
    OpenClawRunLauncher,
    ReconcileAction,
    Supervisor,
    SupervisorLoop,
    TrajectoryRunStatusProvider,
    Waiter,
    read_launch_counter,
)

CONTROLLER = role_source(Role.LEAD)


def _assert_active_config_unchanged() -> None:
    """Scope check: the active OpenClaw config must be byte-for-byte identical
    to the last-good reference.  The real recovery smoke must never mutate
    OpenClaw/system configuration (SPEC V2C §13.5)."""
    active = Path.home() / ".openclaw" / "openclaw.json"
    last_good = Path.home() / ".openclaw" / "openclaw.json.last-good"
    if not active.exists():
        return  # active config absent (other hosts): nothing to compare
    assert last_good.exists(), \
        "active OpenClaw config exists but last-good reference is missing"
    assert active.read_bytes() == last_good.read_bytes(), \
        "active OpenClaw config diverged from last-good (scope violation)"

# The persisted task contract.  The supervisor's prompt is minimal; the task
# description carries the full envelope contract (schema + rules), so real
# agents can produce valid, consumable structured output.
TASK_TITLE = "Recovery smoke: verify fixture implementation"
TASK_DESCRIPTION = """Verify the existing duration parser and service implementation in the fixture.

This is a verification-only task. If your role is implementer or qa, you may
deliver an empty patch set (no file changes needed) when the implementation is
already correct.

REQUIRED OUTPUT CONTRACT - reply with EXACTLY ONE JSON object, nothing else,
no markdown, no prose, no code fences. Top-level fields (all mandatory):
"role" (your role), "task_id", "dispatch_id", "status" ("ok"|"findings"|
"blocked"), "findings" (list of {"severity": "low|medium|high|critical",
"description": "..."}), "own_assessment" (str), "concerns" (list of str),
"proposal" (str), "alternatives" (list of str), "confidence" (number 0..1),
"blockers" (list of str), "requested_next_state" (str).

Role-specific fields:
- lead: "decision" ("accept"|"rework"|"cancel"|"request_owner_gate"),
  "accepted_findings" (list), "rejected_findings" (list), "rationale" (str)
- analyst: "reproduction" (str), "root_cause" (str), "evidence_refs" (list)
- implementer: "changed_files" (list), "implementation_summary" (str),
  "tests_run" (list); optional extra "patch_set" (list of {"op":"write",
  "path":..., "content": plain text}) - empty list means no changes
- qa: "tests" (list of {"name":..., "result": "passed|failed|error"}),
  "failures" (list), "regressions" (list), "coverage_concerns" (list);
  optional extra "test_patch_set" (list, same shape) - empty means no changes
- reviewer: "severity", "security_findings" (list of {"severity","description"}),
  "architecture_findings" (list of same), "recommendation" (str)

VOCABULARY RULE: your whole reply must not contain the substrings prompt,
chain_of_thought, cot, reasoning, secret, password, api_key, token,
credential, mail_content, mail_address, email_address, source_code, code,
diff, body, subject, content, recipient (case-insensitive, also inside longer
words such as encode, decode, different). Use synonyms (logic, unpack,
various, data, component, analysis). Never quote this rule back.

The lead decides accept when the verification passes; rework when findings are
open; the final lead accepts only when tests are green and no relevant
findings are open."""


def _fresh_fixture_copy() -> Path:
    src = PROJECT_ROOT / "e2e-fixture"
    dst = Path(tempfile.mkdtemp(prefix="phase2c-fixture-")) / "fixture"
    shutil.copytree(src, dst)
    return dst


def _make_supervisor(core: Core, workspace_root: Path,
                    counter_path: Optional[Path] = None) -> Supervisor:
    return Supervisor(
        core,
        run_status_provider=TrajectoryRunStatusProvider(),
        run_launcher=OpenClawRunLauncher(counter_path=counter_path),
        controller_source=CONTROLLER,
        owner_source=OWNER_SOURCE,
        workspace_root=workspace_root,
        run_tests_fn=lambda ws: run_tests(str(ws)),
    )


def _spawn_journal_exists(core: Core, dispatch_id: str) -> bool:
    rows = core._store._conn.execute(
        "SELECT status FROM supervisor_actions "
        "WHERE action_type='SPAWN_RUN' AND dispatch_id=? ORDER BY rowid DESC LIMIT 1",
        (dispatch_id,),
    ).fetchall()
    return bool(rows) and rows[0][0] == "SUCCEEDED"


def _current_dispatch(core: Core, job_id: str):
    job = core._store._conn.execute(
        "SELECT task_id FROM supervisor_jobs WHERE id=?", (job_id,)
    ).fetchone()
    if job is None:
        return None
    rows = core._store._conn.execute(
        "SELECT id, role FROM agent_dispatches WHERE task_id=? "
        "ORDER BY rowid DESC LIMIT 1",
        (job[0],),
    ).fetchall()
    return rows[0] if rows else None


def phase_start() -> int:
    base = Path(tempfile.mkdtemp(prefix="phase2c-real-smoke-"))
    db = base / "smoke.db"
    counter_path = base / "launch-counter.json"
    fixture = _fresh_fixture_copy()
    core = Core(str(db))
    project = core.create_project("phase2c-real-smoke", OWNER_SOURCE)
    task = core.create_task(project.id, TASK_TITLE, OWNER_SOURCE,
                            description=TASK_DESCRIPTION)
    core.start_task_run(task.id, OWNER_SOURCE)
    sup = _make_supervisor(core, fixture, counter_path)
    job = sup.store.create_job(task.id, idempotency_key=f"real-smoke:{task.id}")
    job_id = job.supervisor_job_id
    loop = SupervisorLoop(sup)
    waiter = Waiter(clock=sup._clock)
    print(f"[phase1] db={db} fixture={fixture} task={task.id} job={job_id}")

    # Run until the ANALYST dispatch has been spawned, then SIGKILL ourselves.
    # Honour the supervisor's backoff (next_wake_at) between iterations — a
    # tight run_once loop would burn the missing-confirmation budget instantly.
    for _ in range(400):
        decision = loop.run_once(job_id)
        print(f"[phase1] action={decision.action.value} "
              f"dispatch={decision.dispatch_id or '-'}", flush=True)
        d = _current_dispatch(core, job_id)
        if d is not None and d[1] == "analyst" and _spawn_journal_exists(core, d[0]):
            paths = {"db": str(db), "fixture": str(fixture),
                     "job": job_id, "analyst_dispatch": d[0],
                     "launch_counter": str(counter_path)}
            Path(base / "paths.json").write_text(json.dumps(paths))
            print(f"[phase1] analyst spawned ({d[0]}); SIGKILL now")
            os.kill(os.getpid(), signal.SIGKILL)  # real hard kill, no cleanup
        st = sup.store.get_job(job_id)
        if st is not None and st.terminal is not None:
            print(f"[phase1] terminal before kill: {st.terminal}")
            os._exit(2)
        if st is not None and st.next_wake_at:
            waiter.wait_until(st.next_wake_at, None)
    print("[phase1] timeout waiting for analyst spawn")
    os._exit(3)


def _assert_no_double_spawn(core: Core, counter_path: Optional[Path],
                            analyst_dispatch: str) -> None:
    """Independent no-double-spawn proof (F8/F2): the PERSISTENT launcher
    counter (survives SIGKILL), exactly one SPAWN_RUN journal row, and exactly
    one trajectory session.started for the analyst run must all remain == 1.

    Re-verified BEFORE and AFTER ``run_until_terminal()`` so a second analyst
    launch at any point inside the resume loop cannot escape this proof.
    """
    dispatches = core._store._conn.execute(
        "SELECT COUNT(*) FROM agent_dispatches WHERE id=?", (analyst_dispatch,)
    ).fetchone()[0]
    print(f"[phase2] analyst dispatch rows={dispatches}")
    _assert_launcher_and_spawn_once(core, counter_path, analyst_dispatch)
    started_count = _trajectory_started_count(analyst_dispatch)
    print(f"[phase2] analyst trajectory session.started count={started_count}")
    assert started_count == 1, "exactly one analyst session.started"


def _trajectory_started_count(analyst_dispatch: str,
                              state_dir: Optional[Path] = None) -> int:
    """Count ``session.started`` rows in the analyst trajectory (0 if missing)."""
    root = Path(state_dir) if state_dir is not None else Path.home() / ".openclaw"
    traj = root / "agents" / "argent-analyst" / "sessions" \
        / f"dispatch-{analyst_dispatch}.trajectory.jsonl"
    if not traj.exists():
        return 0
    count = 0
    for line in traj.read_text(encoding="utf-8").splitlines():
        if line.strip() and json.loads(line).get("type") == "session.started":
            count += 1
    return count


def _assert_launcher_and_spawn_once(core: Core, counter_path: Optional[Path],
                                    analyst_dispatch: str) -> None:
    """Assert the PERSISTENT launcher counter and SPAWN journal stay exactly 1."""
    spawns = core._store._conn.execute(
        "SELECT COUNT(*) FROM supervisor_actions WHERE action_type='SPAWN_RUN' "
        "AND dispatch_id=?", (analyst_dispatch,)
    ).fetchone()[0]
    assert spawns == 1, "analyst must be spawned exactly once (no blind respawn)"
    launch_count = read_launch_counter(counter_path).get(analyst_dispatch, 0)
    print(f"[phase2] analyst launcher invocation count={launch_count}")
    assert launch_count == 1, \
        f"analyst launcher must be invoked exactly once, got {launch_count}"


def _wait_for_analyst_start(core: Core, counter_path: Optional[Path],
                            analyst_dispatch: str, *,
                            timeout_seconds: float = 30.0,
                            poll_seconds: float = 0.5,
                            state_dir: Optional[Path] = None) -> None:
    """Wait BOUNDEDLY for the analyst trajectory's first session.started row.

    The trajectory file may be flushed LATE by the runtime (documented): the
    launcher counter and SPAWN journal can correctly equal 1 while the
    trajectory start row is temporarily 0.  Poll every ``poll_seconds`` up to
    ``timeout_seconds``, continuously re-asserting the launcher count and SPAWN
    journal rows remain exactly 1 (no double spawn ever).  Expiry without a
    start row fails with a clear message.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        _assert_launcher_and_spawn_once(core, counter_path, analyst_dispatch)
        started = _trajectory_started_count(analyst_dispatch, state_dir)
        if started >= 1:
            assert started == 1, "exactly one analyst session.started"
            return
        if time.monotonic() >= deadline:
            raise AssertionError(
                "analyst trajectory session.started row was never flushed within "
                f"{timeout_seconds}s (launcher count == 1 and SPAWN journal rows "
                "== 1 the whole time)"
            )
        time.sleep(poll_seconds)


def phase_resume(paths_file: Path) -> int:
    paths = json.loads(paths_file.read_text())
    db, fixture, job_id, analyst_dispatch = (
        paths["db"], Path(paths["fixture"]), paths["job"], paths["analyst_dispatch"]
    )
    counter_path = Path(paths["launch_counter"]) if paths.get("launch_counter") else None
    core = Core(db)
    sup = _make_supervisor(core, fixture, counter_path)
    loop = SupervisorLoop(sup)
    task_id = core._store._conn.execute(
        "SELECT task_id FROM supervisor_jobs WHERE id=?", (job_id,)
    ).fetchone()[0]

    print(f"[phase2] reloaded db={db} fixture={fixture}")
    state = sup.store.get_job(job_id)
    assert state is not None and state.terminal is None, "job must not be terminal"

    # 1) reconcile must find the EXISTING analyst run, never double-spawn.
    first = sup.reconcile(job_id)
    print(f"[phase2] first reconcile -> {first.action.value} ({first.reason})")
    # F2: the FIRST reconcile after resume must never re-spawn the analyst.
    assert first.action is not ReconcileAction.SPAWN_RUN, \
        "resume must never re-spawn the analyst"

    # F8/F2: independent no-double-spawn proof (BEFORE the resume loop).
    # The trajectory start row may be flushed LATE by the runtime: wait
    # boundedly for it (continuously re-asserting launcher/spawn == 1) BEFORE
    # the exact trajectory recount.
    _wait_for_analyst_start(core, counter_path, analyst_dispatch)
    _assert_no_double_spawn(core, counter_path, analyst_dispatch)

    # 2) Loop to terminal (consumes the analyst result exactly once, then
    #    continues lead -> implementer -> qa -> reviewer -> lead -> DONE).
    result = loop.run_until_terminal(job_id, stop_event=None)
    assert result is not None, "job disappeared"
    print(f"[phase2] terminal={result.terminal}")

    # F2: re-verify the SAME three persisted facts AFTER the resume loop — a
    # second analyst launch at any point during recovery must not escape.
    _assert_no_double_spawn(core, counter_path, analyst_dispatch)

    # 3) Duplicate injection on the analyst dispatch: same event_meta must be
    #    a duplicate (exactly-once), never a re-consume.  MANDATORY: fail the
    #    run if the persisted result is missing (never silently skip).
    d = core._store.get_dispatch(analyst_dispatch)
    assert d is not None and d.result_json is not None, \
        "duplicate injection skipped: analyst result not persisted"
    em = {
        "task_id": task_id,
        "child_session_id": d.child_session_id,
        "run_id": d.openclaw_run_id,
        "parent_dispatch_id": None,
        "event_type": "agent.completed",
        "status": "completed",
    }
    res = sup.receive_completion_hint(analyst_dispatch, em,
                                      json.loads(d.result_json))
    print(f"[phase2] duplicate injection -> {res.status}")
    assert res.status == "duplicate", res.status

    # 4) Final assertions.
    t = core.queries.get_task(task_id)
    print(f"[phase2] task state={t.state.value}")
    assert t.state.value == "DONE", "task must be DONE"
    cons = core._store._conn.execute(
        "SELECT COUNT(*) FROM agent_dispatches WHERE id=? AND status='CONSUMED'",
        (analyst_dispatch,),
    ).fetchone()[0]
    assert cons == 1, "analyst result must be consumed exactly once"
    all_consumed = core._store._conn.execute(
        "SELECT COUNT(*) FROM agent_dispatches WHERE task_id=? AND status!='CONSUMED'",
        (task_id,),
    ).fetchone()[0]
    assert all_consumed == 0, f"unconsumed dispatches: {all_consumed}"
    # Exactly seven dispatches (lead, analyst, lead, implementer, qa, reviewer,
    # lead) and seven consumed results.
    dcount = core._store._conn.execute(
        "SELECT COUNT(*) FROM agent_dispatches WHERE task_id=?", (task_id,)
    ).fetchone()[0]
    consumed = core._store._conn.execute(
        "SELECT COUNT(*) FROM agent_dispatches WHERE task_id=? AND status='CONSUMED'",
        (task_id,)
    ).fetchone()[0]
    print(f"[phase2] total dispatches={dcount} consumed={consumed}")
    assert dcount == 7, f"expected exactly 7 dispatches, got {dcount}"
    assert consumed == 7, f"expected 7 consumed results, got {consumed}"
    # Fixture copy must not have been modified by write roles in a way that
    # breaks the sandbox (already proven by green tests during the run).
    # Scope: the active OpenClaw config must still be byte-identical to
    # last-good after the whole autonomous run.
    _assert_active_config_unchanged()
    print("[phase2] ALL REAL RECOVERY CHECKS PASSED")
    core.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", action="store_true")
    ap.add_argument("--resume", type=Path)
    args = ap.parse_args()
    if args.start:
        return phase_start()
    if args.resume:
        return phase_resume(args.resume)
    ap.error("need --start or --resume")


if __name__ == "__main__":
    sys.exit(main())
