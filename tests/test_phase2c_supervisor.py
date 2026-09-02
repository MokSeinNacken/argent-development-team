"""Phase 2C supervisor tests — deterministic, offline (SPEC V2C §12).

Covers persistent reload, restart recovery, duplicate/stale completion,
exactly-once consumption, bounded retry, owner-gate memory, DONE stickiness,
crash points and the §7.2 decision table.  Uses the FakeClock/FakeRunStatus
runtime; a "restart" is a new Core/Supervisor over the same DB plus a freshly
scripted provider (simulating the persistent trajectory state).
"""

from __future__ import annotations

import base64
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argent_core import Core, OWNER_SOURCE, Role, role_source  # noqa: E402
from argent_core.models import (  # noqa: E402
    ApprovalStatus,
    DispatchStatus,
    NotFound,
    RoleRun,
    RoleRunStatus,
    TaskState,
)
from argent_core.sandbox_runner import SandboxResult  # noqa: E402
from argent_core.store import SCHEMA_VERSION  # noqa: E402
from argent_core.supervisor import (  # noqa: E402
    AGENT_IDS,
    MAX_DISPATCH_ATTEMPTS_PER_STEP,
    MAX_ACTION_RETRIES,
    MAX_RUNTIME_UNKNOWN,
    OpenClawRunLauncher,
    ReconcileAction,
    RunStatus,
    Supervisor,
    SupervisorJobStatus,
    SupervisorLoop,
    TrajectoryRunStatusProvider,
    WorkspaceHashProvider,
    _canonical_json,
    _parse_iso,
    _sha256,
    _write_envelope,
    backoff_seconds,
    extract_balanced_json,
    read_launch_counter,
    session_key_for,
)
from argent_core.workspace_broker import WorkspaceBroker  # noqa: E402
from argent_core.gates import binding_hash  # noqa: E402
from argent_core.resource_governor import (  # noqa: E402
    AdmissionDecision,
    AdmissionVerdict,
    ResourceReasonCode,
)
from argent_core.resource_policy import ResourceClass, ResourcePolicy  # noqa: E402
from argent_core.scope_enforcer import ExecutionEnforcer  # noqa: E402
from c2_helpers import (  # noqa: E402
    FakeGovernor,
    FakeScopeBackend,
    FakeSnapshotProvider,
)
from mock_runtime import build_output  # noqa: E402
from mock_supervisor_runtime import (  # noqa: E402
    FakeClock,
    FakeRunLauncher,
    FakeRunStatusProvider,
    FakeWaiter,
    canonical_binding,
    make_run_observation,
)

OWNER = OWNER_SOURCE
LEAD = role_source(Role.LEAD)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_workspace(tmp_path):
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "src" / "module.py").write_text("# stub\n")
    return root


def fake_run_tests(workspace, pytest_args=None, limits=None):
    return SandboxResult(
        exit_code=0, stdout_bounded="", stderr_bounded="", timed_out=False,
        wall_seconds=0.0,
    )


def _light_limits():
    pol = ResourcePolicy()
    b = pol.limits_for(ResourceClass.LIGHT)
    return {
        "memory_high_bytes": b.memory_high_bytes,
        "memory_max_bytes": b.memory_max_bytes,
        "swap_max_bytes": b.swap_max_bytes,
        "cpu_quota_percent": b.cpu_quota_percent,
        "timeout_seconds": b.timeout_seconds,
    }


def _allow_admission():
    return AdmissionDecision(
        resource_class=ResourceClass.LIGHT.value,
        policy_version="1",
        snapshot_ref="snap-1",
        decision=AdmissionVerdict.ALLOW.value,
        reason_code=ResourceReasonCode.OK.value,
        next_eligible_at=None,
        effective_limits=_light_limits(),
        timestamp="2026-09-01T00:00:00+00:00",
    )


def make_env(db_path, clock=None, *, workspace=None, run_tests_fn=None,
             idempotency_key="job-1", enforcer=None,
             resource_governor=None, snapshot_provider=None):
    clock = clock or FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    task_run = core.start_task_run(task.id, OWNER)
    prov = FakeRunStatusProvider()
    launch = FakeRunLauncher()
    # C2: a deterministic fake enforcement path (spawn now goes through the
    # enforcer, never the legacy launcher).  A fake governor + snapshot
    # provider keep the fresh C1 admission at the enforcement point fully
    # deterministic (no real host reads in these offline tests).
    backend = FakeScopeBackend()
    enforcer = enforcer or ExecutionEnforcer(backend)
    resource_governor = resource_governor or FakeGovernor(_allow_admission())
    snapshot_provider = snapshot_provider or FakeSnapshotProvider()
    sup = Supervisor(
        core, prov, launch, clock=clock,
        workspace_root=workspace, run_tests_fn=run_tests_fn,
        enforcer=enforcer, resource_governor=resource_governor,
        snapshot_provider=snapshot_provider,
    )
    job = sup.store.create_job(task.id, idempotency_key=idempotency_key)
    return SimpleNamespace(
        core=core, task=task, task_run=task_run, prov=prov, launch=launch,
        sup=sup, job=job, clock=clock, backend=backend, enforcer=enforcer,
    )


def step(env):
    d = env.sup.reconcile(env.job.supervisor_job_id)
    env.sup.perform_next_safe_action_if_required(d)
    return d


def advance(env, action, max_steps=30):
    seen = []
    for _ in range(max_steps):
        d = step(env)
        seen.append(d.action)
        if d.action == action:
            return d
    raise AssertionError(f"never reached {action}; saw {seen}")


def _bind_and_succeed(env, dispatch_id, role, result):
    d = env.core.queries.get_dispatch(dispatch_id)
    provider, model, thinking, session = canonical_binding(d)
    run_id = f"run-{d.id[:8]}"
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    advance(env, ReconcileAction.BIND_RUN)
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.SUCCEEDED,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking, result=result,
    ))
    return session, run_id


def bind_running(env, role):
    """Advance to a bound RUNNING dispatch (not consumed) and return it."""
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    assert d.role is role
    provider, model, thinking, session = canonical_binding(d)
    run_id = f"run-{d.id[:8]}"
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    advance(env, ReconcileAction.BIND_RUN)
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING
    return d, session


def _bind_writer(env):
    """Bind the job's writer to the last implementer dispatch (test infra).

    E2 F1: the closing review is ALWAYS writer-independent.  Real writer binding
    is an external (Phase B3/I) concern; the offline test harness binds the last
    implementer dispatch so the happy-path reviewer can dispatch a DIFFERENT
    model.  Tests that exercise the no-writer fail-closed path do NOT call this.
    """
    writers = [
        d for d in env.core.queries.list_dispatches(env.task.id)
        if d.role is Role.IMPLEMENTER
    ]
    if writers:
        env.core._store._conn.execute(
            "UPDATE supervisor_jobs SET writer_dispatch_id = ? WHERE id = ?",
            (writers[-1].id, env.job.supervisor_job_id),
        )


def drive_frontier(env, role, result_fn=None):
    """Drive START_ROLE -> CREATE_DISPATCH -> BIND -> (write preconds) -> CONSUME."""
    if role is Role.REVIEWER:
        _bind_writer(env)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    dispatch = env.core.queries.list_dispatches(env.task.id)[-1]
    assert dispatch.role is role
    if result_fn is None:
        result = build_output(role, env.task.id, dispatch.id)
    else:
        result = result_fn(dispatch.id)
    _bind_and_succeed(env, dispatch.id, role, result)
    if role in (Role.IMPLEMENTER, Role.QA):
        advance(env, ReconcileAction.APPLY_PATCH_SET)
        advance(env, ReconcileAction.RUN_SANDBOX_TESTS)
        advance(env, ReconcileAction.RECORD_TEST_RESULT)
    advance(env, ReconcileAction.CONSUME_RESULT)
    return dispatch


def _write_result(role, task_id, dispatch_id, patch_field, patch):
    r = dict(build_output(role, task_id, dispatch_id))
    r[patch_field] = patch
    return r


def drive_to_done(env):
    """Run the full standard workflow to DONE using offline fakes."""
    t = env.task
    drive_frontier(env, Role.LEAD)
    drive_frontier(env, Role.ANALYST)
    drive_frontier(env, Role.LEAD)
    drive_frontier(env, Role.IMPLEMENTER, lambda did: _write_result(
        Role.IMPLEMENTER, t.id, did, "patch_set",
        [{"op": "write", "path": "src/module.py",
          "content": base64.b64encode(b"def parse_duration(s):\n    return None\n").decode()}],
    ))
    drive_frontier(env, Role.QA, lambda did: _write_result(
        Role.QA, t.id, did, "test_patch_set",
        [{"op": "write", "path": "tests/test_parser.py",
          "content": base64.b64encode(b"def test_x():\n    assert True\n").decode()}],
    ))
    drive_frontier(env, Role.REVIEWER)
    drive_frontier(env, Role.LEAD)
    return env.core.queries.get_task(t.id)


# ---------------------------------------------------------------------------
# Schema / wrapper / idempotency
# ---------------------------------------------------------------------------

def test_supervisor_tables_exist(db_path):
    core = Core(db_path)
    try:
        names = {
            r["name"]
            for r in core._store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        core.close()
    assert {"supervisor_jobs", "supervisor_actions"} <= names


def test_workflow_frontier_public_wrapper(db_path):
    core = Core(db_path)
    try:
        project = core.create_project("p", OWNER)
        task = core.create_task(project.id, "t", OWNER)
        f = core.workflow_frontier(task.id, LEAD)
        assert f.expected_role is Role.LEAD
        assert (f.cycle_no, f.position) == (1, 0)
        with pytest.raises(Exception):
            core.workflow_frontier(task.id, "role:analyst")
    finally:
        core.close()


def test_create_job_idempotent(db_path):
    env = make_env(db_path)
    job2 = env.sup.store.create_job(env.task.id, idempotency_key="job-1")
    assert job2.supervisor_job_id == env.job.supervisor_job_id == \
        "supervisor:" + env.task.id
    # Same task, different idempotency key, still resolves to the same job.
    job3 = env.sup.store.create_job(env.task.id, idempotency_key="job-2")
    assert job3.supervisor_job_id == env.job.supervisor_job_id
    # A different key with a different (conflicting) task would be an error,
    # but the same task id is deterministic.


def test_extract_balanced_json_shared():
    assert extract_balanced_json("noise {\"a\": [1,2]} tail") == {"a": [1, 2]}
    with pytest.raises(ValueError):
        extract_balanced_json("no brace here")


# ---------------------------------------------------------------------------
# Persistence / reload
# ---------------------------------------------------------------------------

def test_persistent_state_reload(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    dispatch = env.core.queries.list_dispatches(env.task.id)[-1]
    state_before = env.sup.store.get_job(env.job.supervisor_job_id)
    env.core.close()

    clock2 = FakeClock()
    core2 = Core(db_path, clock=clock2)
    prov2 = FakeRunStatusProvider()
    sup2 = Supervisor(core2, prov2, FakeRunLauncher(), clock=clock2)
    job2 = sup2.store.get_job(env.job.supervisor_job_id)
    assert job2 is not None
    assert job2.task_id == env.task.id
    assert job2.expected_role == "lead"
    assert job2.expected_dispatch_id == dispatch.id
    assert job2.dispatch_status == "PENDING"
    assert job2.facts_version == state_before.facts_version


def test_reload_recomputes_from_core_ledger(db_path):
    env = make_env(db_path)
    drive_frontier(env, Role.LEAD)
    # Cache (job) now shows lead consumed; task PLANNING; frontier analyst.
    state = env.sup.store.get_job(env.job.supervisor_job_id)
    assert state.workflow_state == TaskState.PLANNING.value
    assert state.expected_role == "analyst"
    env.core.close()

    core2 = Core(db_path)
    prov2 = FakeRunStatusProvider()
    sup2 = Supervisor(core2, prov2, FakeRunLauncher())
    job2 = sup2.store.get_job(env.job.supervisor_job_id)
    assert job2.expected_role == "analyst"
    assert job2.workflow_state == TaskState.PLANNING.value


# ---------------------------------------------------------------------------
# Decision table: running -> wait; succeeded -> consume once
# ---------------------------------------------------------------------------

def test_running_run_wait_no_double_dispatch(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    provider, model, thinking, session = canonical_binding(d)
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id="r1", session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    advance(env, ReconcileAction.BIND_RUN)
    # Bound + RUNNING -> WAIT, no new dispatch, no spawn.
    d2 = step(env)
    assert d2.action is ReconcileAction.WAIT
    dispatches = env.core.queries.list_dispatches(env.task.id)
    assert len(dispatches) == 1
    assert len(env.launch.spawns) == 0


def test_succeeded_unconsumed_consume_once(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    result = build_output(d.role, env.task.id, d.id)
    session, run = _bind_and_succeed(env, d.id, d.role, result)
    # F8: the run SUCCEEDED but is still unconsumed; reload BEFORE consumption
    # (a fresh Core/Supervisor over the same DB) and consume exactly once.
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING
    env.core.close()

    core2 = Core(db_path)
    prov2 = FakeRunStatusProvider()
    sup2 = Supervisor(core2, prov2, FakeRunLauncher())
    d2 = core2.queries.get_dispatch(d.id)
    provider, model, thinking, session2 = canonical_binding(d2)
    prov2.set_current(d2.id, make_run_observation(
        dispatch_id=d2.id, role=d2.role, status=RunStatus.SUCCEEDED,
        run_id=run, session_id=session, provider=provider, model=model,
        thinking_tier=thinking, result=result,
    ))
    for _ in range(10):
        dec = sup2.reconcile(env.job.supervisor_job_id)
        sup2.perform_next_safe_action_if_required(dec)
        if core2.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED:
            break
    assert core2.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED
    # No re-apply: the frontier advanced and a further reconcile never consumes
    # the same dispatch again.
    d4 = sup2.reconcile(env.job.supervisor_job_id)
    assert d4.action is not ReconcileAction.CONSUME_RESULT


# ---------------------------------------------------------------------------
# Exactly-once / duplicates / stale
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("count", [2, 5, 20])
def test_duplicate_completion_exactly_once(db_path, count):
    env = make_env(db_path)
    d = drive_frontier(env, Role.LEAD)
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED
    result = build_output(Role.LEAD, env.task.id, d.id)
    event_meta = {
        "task_id": env.task.id,
        "child_session_id": env.core.queries.get_dispatch(d.id).child_session_id,
        "run_id": env.core.queries.get_dispatch(d.id).openclaw_run_id,
        "parent_dispatch_id": None,
        "event_type": "agent.completed",
        "status": "completed",
    }
    for _ in range(count):
        res = env.sup.receive_completion_hint(d.id, event_meta, result)
        assert res.status == "duplicate", res
    # Still exactly one consumed, one handoff, one decision.
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED
    assert len(env.core.queries.list_handoffs(env.task.id)) == 1
    assert len(env.core.queries.list_decisions(env.task.id)) == 1


def test_stale_attempt_during_next_attempt(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d1 = env.core.queries.list_dispatches(env.task.id)[-1]
    provider, model, thinking, session = canonical_binding(d1)
    env.prov.set_current(d1.id, make_run_observation(
        dispatch_id=d1.id, role=d1.role, status=RunStatus.FAILED,
        run_id="r1", session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    advance(env, ReconcileAction.MARK_RUN_FAILED)
    assert env.core.queries.get_dispatch(d1.id).status is DispatchStatus.FAILED

    # Now attempt 2 (same frontier).
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d2 = env.core.queries.list_dispatches(env.task.id)[-1]
    assert d2.id != d1.id and d2.attempt_no == 2

    # Stale completion from attempt 1 -> rejected, state unchanged.
    result = build_output(d1.role, env.task.id, d1.id)
    em = {
        "task_id": env.task.id, "child_session_id": session, "run_id": "r1",
        "parent_dispatch_id": None, "event_type": "agent.completed",
        "status": "completed",
    }
    res = env.sup.receive_completion_hint(d1.id, em, result)
    assert res.status == "rejected", res
    assert env.core.queries.get_dispatch(d2.id).status is DispatchStatus.PENDING


def test_wrong_run_id_rejected(db_path):
    env = make_env(db_path)
    d, session = bind_running(env, Role.LEAD)
    result = build_output(Role.LEAD, env.task.id, d.id)
    dd = env.core.queries.get_dispatch(d.id)
    em = {
        "task_id": env.task.id, "child_session_id": dd.child_session_id,
        "run_id": "wrong-run-id", "parent_dispatch_id": None,
        "event_type": "agent.completed", "status": "completed",
    }
    res = env.sup.receive_completion_hint(d.id, em, result)
    assert res.status == "rejected"
    assert res.reason == "run_id_mismatch"


def test_wrong_session_id_rejected(db_path):
    env = make_env(db_path)
    d, session = bind_running(env, Role.LEAD)
    result = build_output(Role.LEAD, env.task.id, d.id)
    dd = env.core.queries.get_dispatch(d.id)
    em = {
        "task_id": env.task.id, "child_session_id": "wrong-session",
        "run_id": dd.openclaw_run_id, "parent_dispatch_id": None,
        "event_type": "agent.completed", "status": "completed",
    }
    res = env.sup.receive_completion_hint(d.id, em, result)
    assert res.status == "rejected"
    assert res.reason == "session_mismatch"


def test_wrong_role_envelope_rejected(db_path):
    env = make_env(db_path)
    drive_frontier(env, Role.LEAD)  # consume lead so frontier reaches analyst
    d, session = bind_running(env, Role.ANALYST)
    # A lead envelope delivered against an analyst dispatch -> role mismatch.
    result = build_output(Role.LEAD, env.task.id, d.id)
    dd = env.core.queries.get_dispatch(d.id)
    em = {
        "task_id": env.task.id, "child_session_id": dd.child_session_id,
        "run_id": dd.openclaw_run_id, "parent_dispatch_id": None,
        "event_type": "agent.completed", "status": "completed",
    }
    res = env.sup.receive_completion_hint(d.id, em, result)
    assert res.status == "rejected"
    assert res.reason == "role_mismatch"


def test_lost_completion_found_via_trajectory_no_respawn(db_path):
    # Dispatch bound RUNNING; no completion hint arrives.  The provider reports
    # SUCCEEDED (trajectory shows the run finished) -> consume once, no spawn.
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    result = build_output(d.role, env.task.id, d.id)
    session, run = _bind_and_succeed(env, d.id, d.role, result)
    advance(env, ReconcileAction.CONSUME_RESULT)
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED
    assert len(env.launch.spawns) == 0  # never even spawned via launcher (fake)


# ---------------------------------------------------------------------------
# Failure / retry / no blind respawn
# ---------------------------------------------------------------------------

def test_failed_run_bounded_retry(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    provider, model, thinking, session = canonical_binding(d)
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id="r1", session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    advance(env, ReconcileAction.BIND_RUN)
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.FAILED,
        run_id="r1", session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    advance(env, ReconcileAction.MARK_RUN_FAILED)
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.FAILED
    # Next: retry -> START_ROLE (not CLOSE_FAILED).
    d2 = step(env)
    assert d2.action is ReconcileAction.START_ROLE


def test_no_blind_respawn_spawn_journal_exists(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    # Simulate a prior SPAWN_RUN journal (crash after spawn intent) and a
    # NOT_FOUND runtime: reconcile must WAIT/DISCOVER, never spawn again.
    env.sup.store._store._insert_supervisor_action({
        "id": "spawn-action-1",
        "supervisor_job_id": env.job.supervisor_job_id,
        "dispatch_id": d.id,
        "action_type": "SPAWN_RUN",
        "action_key": f"supervisor:dispatch:{d.id}:spawn",
        "args_hash": "h", "input_hash": None, "precondition_hash": None,
        "effect_hash": None, "status": "RUNNING", "attempt_count": 1,
        "next_attempt_at": None, "started_at": "t", "finished_at": None,
        "last_error_code": None, "created_at": "t", "updated_at": "t",
    })
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.NOT_FOUND,
        authoritative_not_found=True,
    ))
    d2 = step(env)
    assert d2.action is ReconcileAction.WAIT
    assert len(env.launch.spawns) == 0


def test_spawn_plan_then_launch_once(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.NOT_FOUND,
        authoritative_not_found=True,
    ))
    advance(env, ReconcileAction.SPAWN_RUN)
    assert len(env.backend.created) == 1  # exactly one scope created
    # A second reconcile while still NOT_FOUND must not re-spawn.
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.NOT_FOUND,
        authoritative_not_found=True,
    ))
    d2 = step(env)
    assert d2.action is ReconcileAction.WAIT
    assert len(env.backend.created) == 1


def test_max_attempts_terminal_failed(db_path):
    env = make_env(db_path)
    # Fail the read-only lead role MAX_DISPATCH_ATTEMPTS_PER_STEP times.
    for _ in range(MAX_DISPATCH_ATTEMPTS_PER_STEP):
        advance(env, ReconcileAction.START_ROLE)
        advance(env, ReconcileAction.CREATE_DISPATCH)
        d = env.core.queries.list_dispatches(env.task.id)[-1]
        provider, model, thinking, session = canonical_binding(d)
        env.prov.set_current(d.id, make_run_observation(
            dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
            run_id=f"r{d.attempt_no}", session_id=session, provider=provider,
            model=model, thinking_tier=thinking,
        ))
        advance(env, ReconcileAction.BIND_RUN)
        env.prov.set_current(d.id, make_run_observation(
            dispatch_id=d.id, role=d.role, status=RunStatus.FAILED,
            run_id=f"r{d.attempt_no}", session_id=session, provider=provider,
            model=model, thinking_tier=thinking,
        ))
        advance(env, ReconcileAction.MARK_RUN_FAILED)
    # After 3 failed attempts, the next reconcile must CLOSE_FAILED.
    d = advance(env, ReconcileAction.CLOSE_FAILED, max_steps=5)
    state = env.sup.store.get_job(env.job.supervisor_job_id)
    assert state.terminal == "FAILED"


# ---------------------------------------------------------------------------
# Owner gate memory
# ---------------------------------------------------------------------------

def test_approval_persistence_closed_after_reload(db_path):
    env = make_env(db_path)
    core = env.core
    core.start_role(env.task.id, Role.LEAD, LEAD)
    ar = core.request_action(
        env.task.id, "deploy_production", "prod", Role.LEAD, LEAD,
    )
    ap = ar.approval
    core.approve(ap.id, OWNER, task_id=env.task.id, action="deploy_production",
                 scope="prod")
    core.execute_approved(ap.id, OWNER, task_id=env.task.id,
                          action="deploy_production", scope="prod")
    # gate consumed + closed
    ap2 = core.queries.get_approval(ap.id)
    assert ap2.status is ApprovalStatus.CONSUMED
    assert ap2.closed_at is not None
    assert ap2.execution_id is not None

    # Reload supervisor: gate reflects CLOSED, no new prompt, frontier continues.
    core.close()
    core2 = Core(db_path)
    prov2 = FakeRunStatusProvider()
    sup2 = Supervisor(core2, prov2, FakeRunLauncher())
    job2 = sup2.store.get_job(env.job.supervisor_job_id)
    assert job2.gate_closed is True
    # reconcile: not PRESENT_OWNER_GATE (task released from gate state).
    d = sup2.reconcile(env.job.supervisor_job_id)
    assert d.action is not ReconcileAction.PRESENT_OWNER_GATE


def test_pending_gate_presented_once(db_path):
    env = make_env(db_path)
    core = env.core
    core.start_role(env.task.id, Role.LEAD, LEAD)
    core.request_action(env.task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    # reconcile -> PRESENT_OWNER_GATE exactly once.
    d = step(env)
    assert d.action is ReconcileAction.PRESENT_OWNER_GATE
    d2 = step(env)
    assert d2.action is ReconcileAction.WAIT
    assert d2.reason == "waiting_gate"


def test_binding_hash_present_and_not_null(db_path):
    core = Core(db_path)
    try:
        project = core.create_project("p", OWNER)
        task = core.create_task(project.id, "t", OWNER)
        core.start_role(task.id, Role.LEAD, LEAD)
        ar = core.request_action(task.id, "deploy_production", "prod",
                                 Role.LEAD, LEAD)
        from argent_core.gates import binding_hash
        assert ar.approval.binding_hash == binding_hash(
            task.id, "deploy_production", "prod")
    finally:
        core.close()


# ---------------------------------------------------------------------------
# DONE persistence
# ---------------------------------------------------------------------------

def test_done_stays_done_after_reload(db_path, tmp_path):
    env = make_env(db_path, workspace=make_workspace(tmp_path),
                   run_tests_fn=fake_run_tests)
    task = drive_to_done(env)
    assert task.state is TaskState.DONE
    # reconcile -> CLOSE_DONE (terminal).
    d = advance(env, ReconcileAction.CLOSE_DONE, max_steps=3)
    state = env.sup.store.get_job(env.job.supervisor_job_id)
    assert state.terminal == "DONE"
    env.core.close()

    core2 = Core(db_path)
    prov2 = FakeRunStatusProvider()
    sup2 = Supervisor(core2, prov2, FakeRunLauncher())
    job2 = sup2.store.get_job(env.job.supervisor_job_id)
    assert job2.terminal == "DONE"
    # A stale completion can never reopen DONE.
    d2 = sup2.reconcile(env.job.supervisor_job_id)
    assert d2.action is ReconcileAction.NONE
    assert d2.reason == "job_terminal"


# ---------------------------------------------------------------------------
# Crash / restart points (reload-from-SQLite simulation)
# ---------------------------------------------------------------------------

def test_crash_between_dispatch_create_and_spawn_binding(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    assert d.status is DispatchStatus.PENDING
    env.core.close()

    # Restart: dispatch PENDING, no spawn journal -> SPAWN_RUN (exactly one).
    core2 = Core(db_path)
    prov2 = FakeRunStatusProvider()
    launch2 = FakeRunLauncher()
    sup2 = Supervisor(core2, prov2, launch2)
    job2 = sup2.store.get_job(env.job.supervisor_job_id)
    d2 = sup2.reconcile(env.job.supervisor_job_id)
    assert d2.action is ReconcileAction.SPAWN_RUN


def test_crash_after_spawn_binding_no_double_spawn(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    provider, model, thinking, session = canonical_binding(d)
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id="r1", session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    advance(env, ReconcileAction.BIND_RUN)
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING
    env.core.close()

    # Restart: RUNNING dispatch found -> WAIT, no second spawn.
    core2 = Core(db_path)
    prov2 = FakeRunStatusProvider()
    launch2 = FakeRunLauncher()
    sup2 = Supervisor(core2, prov2, launch2)
    prov2.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id="r1", session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    d2 = sup2.reconcile(env.job.supervisor_job_id)
    assert d2.action is ReconcileAction.WAIT
    assert len(launch2.spawns) == 0


def test_crash_during_rework_reconstructs(db_path):
    env = make_env(db_path)
    # Complete lead/analyst/lead so frontier reaches implementer, then add an
    # open finding (the reviewer would create it) and simulate rework lead.
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    _bind_and_succeed(env, d.id, Role.LEAD, build_output(Role.LEAD, env.task.id, d.id))
    advance(env, ReconcileAction.CONSUME_RESULT)
    env.core.close()

    core2 = Core(db_path)
    prov2 = FakeRunStatusProvider()
    sup2 = Supervisor(core2, prov2, FakeRunLauncher())
    job2 = sup2.store.get_job(env.job.supervisor_job_id)
    assert job2.expected_role == "analyst"
    assert job2.rework_cycle >= 1


# ---------------------------------------------------------------------------
# Adapter UNKNOWN / persistent error
# ---------------------------------------------------------------------------

def test_unknown_runtime_persistent_error(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    provider, model, thinking, session = canonical_binding(d)
    # Bind the dispatch, then report UNKNOWN repeatedly.
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id="r1", session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    advance(env, ReconcileAction.BIND_RUN)
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.UNKNOWN,
    ))
    # 5 UNKNOWN observations -> PERSISTENT_ERROR.
    for _ in range(10):
        dd = step(env)
        if dd.action is ReconcileAction.PERSISTENT_ERROR:
            break
    else:
        raise AssertionError("never hit PERSISTENT_ERROR")
    state = env.sup.store.get_job(env.job.supervisor_job_id)
    assert state.last_error_code is not None


# ---------------------------------------------------------------------------
# Loop (run_until_terminal with FakeWaiter, no busy-loop)
# ---------------------------------------------------------------------------

def test_loop_run_until_terminal(db_path, tmp_path):
    env = make_env(db_path, workspace=make_workspace(tmp_path),
                   run_tests_fn=fake_run_tests)
    from argent_core.supervisor import SupervisorLoop
    from mock_supervisor_runtime import AutoRunStatusProvider
    auto = AutoRunStatusProvider(env.core)
    env.sup = Supervisor(env.core, auto, env.launch, clock=env.clock,
                         workspace_root=make_workspace(tmp_path),
                         run_tests_fn=fake_run_tests)
    waiter = FakeWaiter(env.clock)
    loop = SupervisorLoop(env.sup, waiter=waiter)
    state = loop.run_until_terminal(env.job.supervisor_job_id)
    assert state.terminal == "DONE"
    assert env.core.queries.get_task(env.task.id).state is TaskState.DONE


# ---------------------------------------------------------------------------
# Additional restart / stale-gate invariants
# ---------------------------------------------------------------------------

def test_crash_during_implementer_run_no_second_writer(db_path):
    env = make_env(db_path)
    # Consume lead(0), analyst(1), lead(2) so the frontier reaches implementer.
    drive_frontier(env, Role.LEAD)
    drive_frontier(env, Role.ANALYST)
    drive_frontier(env, Role.LEAD)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    assert d.role is Role.IMPLEMENTER
    provider, model, thinking, session = canonical_binding(d)
    run_id = f"run-{d.id[:8]}"
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    advance(env, ReconcileAction.BIND_RUN)
    env.core.close()

    core2 = Core(db_path)
    prov2 = FakeRunStatusProvider()
    sup2 = Supervisor(core2, prov2, FakeRunLauncher())
    prov2.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    d2 = sup2.reconcile(env.job.supervisor_job_id)
    # Found the running writer -> wait, never a second implementer.
    assert d2.action is ReconcileAction.WAIT
    impl = [x for x in core2.queries.list_dispatches(env.task.id)
            if x.role is Role.IMPLEMENTER]
    assert len(impl) == 1


def test_stale_approval_cannot_reopen(db_path):
    env = make_env(db_path)
    core = env.core
    core.start_role(env.task.id, Role.LEAD, LEAD)
    ar = core.request_action(env.task.id, "deploy_production", "prod",
                             Role.LEAD, LEAD)
    ap = ar.approval
    core.approve(ap.id, OWNER, task_id=env.task.id, action="deploy_production",
                 scope="prod")
    core.execute_approved(ap.id, OWNER, task_id=env.task.id,
                          action="deploy_production", scope="prod")
    # A stale re-approve / re-execute must fail closed (never reopens).
    with pytest.raises(Exception):
        core.approve(ap.id, OWNER, task_id=env.task.id,
                     action="deploy_production", scope="prod")
    with pytest.raises(Exception):
        core.execute_approved(ap.id, OWNER, task_id=env.task.id,
                              action="deploy_production", scope="prod")
    # Supervisor: no new prompt, no reopen.
    d = step(env)
    assert d.action is not ReconcileAction.PRESENT_OWNER_GATE
    state = env.sup.store.get_job(env.job.supervisor_job_id)
    assert state.gate_closed is True


def test_missing_confirmation_budgets_sufficient_for_real_runs():
    """Real E2E finding: Sol/Codex runs can take 40-90s to create trajectory
    files after spawn.  The missing-confirmation budgets must cover agent
    startup latency, not just a few seconds (SPEC V2C §9)."""
    from argent_core.supervisor import (
        MISSING_BOUND_RUN_CONFIRMATIONS,
        MISSING_UNBOUND_SPAWN_CONFIRMATIONS,
        BACKOFF_MAX_SECONDS,
    )
    # unbound-spawn window ≈ 1+2+4+8+16 + (N-5)*30 seconds
    unbound_window = 31 + (MISSING_UNBOUND_SPAWN_CONFIRMATIONS - 5) * BACKOFF_MAX_SECONDS
    assert unbound_window >= 240, \
        f"unbound spawn window too short: {unbound_window}s"
    assert MISSING_BOUND_RUN_CONFIRMATIONS >= 5



def test_active_run_without_binding_values_waits_not_loops(db_path):
    """Real E2E finding: an active session whose trajectory is not flushed yet
    has no run_id -> the supervisor must WAIT (bounded poll), never tight-loop
    on BIND_RUN."""
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    # Active session (session file exists) but trajectory not flushed: no
    # bindable values yet.
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id=None, session_id=None, provider=None, model=None,
        thinking_tier=None,
    ))
    dec = step(env)
    assert dec.action is ReconcileAction.WAIT, dec.action
    assert dec.reason == "run_active_no_binding_values", dec.reason
    state = env.sup.store.get_job(env.job.supervisor_job_id)
    assert state.missing_confirmations == 0, "active run must not burn budget"
    assert len(env.launch.spawns) == 0, "no respawn for an active run"
    # Once the trajectory appears (bindable values present) -> BIND_RUN.
    session = f"agent:argent-analyst:explicit:dispatch-{d.id}"
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id="run-1234", session_id=session,
        provider="deepseek", model="deepseek-v4-pro", thinking_tier="medium",
    ))
    dec2 = step(env)
    assert dec2.action is ReconcileAction.BIND_RUN, dec2.action


def test_consume_rejected_is_bounded_not_infinite(db_path):
    """Real E2E finding: a rejected consume (provenance mismatch) must fail
    the action so the bounded retry policy applies - never a silent infinite
    CONSUME_RESULT re-plan loop."""
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    advance(env, ReconcileAction.SPAWN_RUN)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    session, run = None, None
    provider, model, thinking, session = canonical_binding(d)
    run = f"run-{d.id[:8]}"
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.SUCCEEDED,
        run_id=run, session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    advance(env, ReconcileAction.BIND_RUN)
    # Deliver a result whose task_id does NOT belong to this dispatch:
    # the core rejects it (task_mismatch) -> the consume action must FAIL.
    bad = build_output(Role.LEAD, "foreign-task", d.id)
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.SUCCEEDED,
        run_id=run, session_id=session, provider=provider, model=model,
        thinking_tier=thinking, result=bad,
    ))
    for _ in range(MAX_ACTION_RETRIES + 3):
        dec = step(env)
        if dec.action not in (ReconcileAction.CONSUME_RESULT,):
            break
    else:
        raise AssertionError("consume looped beyond the retry budget")
    # The supervisor must have stopped re-planning CONSUME_RESULT.
    assert dec.action is not ReconcileAction.CONSUME_RESULT, dec.action


def test_missing_counter_resets_per_dispatch(db_path):
    """Real E2E finding: missing_confirmations accumulated across dispatches
    and could CLOSE_BLOCKED a fresh spawn.  Each new dispatch (CREATE_DISPATCH)
    and each bind (BIND_RUN) must reset the budget."""
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    # Pump the missing counter close to the budget (spawn journal + NOT_FOUND).
    env.sup.store._store._insert_supervisor_action({
        "id": "spawn-action-x", "supervisor_job_id": env.job.supervisor_job_id,
        "dispatch_id": d.id, "action_type": "SPAWN_RUN",
        "action_key": f"supervisor:dispatch:{d.id}:spawn",
        "args_hash": "h", "input_hash": None, "precondition_hash": None,
        "effect_hash": None, "status": "SUCCEEDED", "attempt_count": 1,
        "next_attempt_at": None, "started_at": "t", "finished_at": "t",
        "last_error_code": None, "created_at": "t", "updated_at": "t",
    })
    for _ in range(12):
        env.prov.set_current(d.id, make_run_observation(
            dispatch_id=d.id, role=d.role, status=RunStatus.NOT_FOUND,
            authoritative_not_found=True,
        ))
        dec = step(env)
        assert dec.action is ReconcileAction.WAIT
    state = env.sup.store.get_job(env.job.supervisor_job_id)
    assert state.missing_confirmations >= 12, state.missing_confirmations
    # Simulate the run appearing -> bind resets the budget.
    session = f"agent:argent-{d.role.value}:explicit:dispatch-{d.id}"
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id="run-1", session_id=session,
        provider="deepseek", model="deepseek-v4-pro", thinking_tier="medium",
    ))
    advance(env, ReconcileAction.BIND_RUN)
    state = env.sup.store.get_job(env.job.supervisor_job_id)
    assert state.missing_confirmations == 0, state.missing_confirmations


# ---------------------------------------------------------------------------
# Closing-review fixes F1–F8 (regression tests)
# ---------------------------------------------------------------------------

def _make_write_env(db_path, tmp_path):
    """Env reaching the implementer frontier with a counting broker."""
    ws = make_workspace(tmp_path)
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    core.start_task_run(task.id, OWNER)
    prov = FakeRunStatusProvider()
    launch = FakeRunLauncher()
    calls = {"n": 0}

    class CountingBroker(WorkspaceBroker):
        def apply_patch_set(self, *a, **k):
            calls["n"] += 1
            return super().apply_patch_set(*a, **k)

    sup = Supervisor(core, prov, launch, clock=clock, workspace_root=ws,
                     run_tests_fn=fake_run_tests,
                     broker_factory=lambda: CountingBroker())
    job = sup.store.create_job(task.id, idempotency_key="job-1")
    env = SimpleNamespace(core=core, task=task, prov=prov, launch=launch,
                          sup=sup, job=job, clock=clock, ws=ws, calls=calls)
    # Reach the implementer frontier.
    drive_frontier(env, Role.LEAD)
    drive_frontier(env, Role.ANALYST)
    drive_frontier(env, Role.LEAD)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    assert d.role is Role.IMPLEMENTER
    return env, d


def _bind_implementer_succeeded(env, d, result):
    provider, model, thinking, session = canonical_binding(d)
    run_id = f"run-{d.id[:8]}"
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    advance(env, ReconcileAction.BIND_RUN)
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.SUCCEEDED,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking, result=result,
    ))
    return session, run_id


# --- F1: unvalidated agent result must never mutate the workspace -----------

def test_f1_write_role_mismatched_task_no_broker_apply(db_path, tmp_path):
    env, d = _make_write_env(db_path, tmp_path)
    bad = _write_result(Role.IMPLEMENTER, "foreign-task", d.id, "patch_set",
                        [{"op": "write", "path": "src/module.py",
                          "content": base64.b64encode(b"X").decode()}])
    _bind_implementer_succeeded(env, d, bad)
    for _ in range(MAX_ACTION_RETRIES + 2):
        dec = step(env)
        if dec.action in (ReconcileAction.PERSISTENT_ERROR,
                          ReconcileAction.MARK_RUN_FAILED):
            break
    else:
        raise AssertionError("never reached a terminal/error action")
    assert env.calls["n"] == 0, "broker must never be called for a foreign task_id"
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING
    assert (env.ws / "src" / "module.py").read_text() == "# stub\n"


# --- F2: crash-safe action journal + reconciliation -------------------------

def test_f2_crash_after_apply_reconciles_exactly_once(db_path, tmp_path):
    env, d = _make_write_env(db_path, tmp_path)
    patch = [{"op": "write", "path": "src/module.py",
              "content": base64.b64encode(b"def parse_duration(s):\n    return None\n").decode()}]
    result = _write_result(Role.IMPLEMENTER, env.task.id, d.id, "patch_set", patch)
    _bind_implementer_succeeded(env, d, result)

    wsp = WorkspaceHashProvider()
    precondition = wsp.scoped_hash(env.ws)
    effect = wsp.predicted_hash(env.ws, patch)
    result_hash = _sha256(_canonical_json(result))
    key = f"supervisor:dispatch:{d.id}:apply:{result_hash}"
    args_hash = _sha256(_canonical_json(
        {"dispatch_id": d.id, "patch_set": patch,
         "workspace_root": str(env.ws.resolve())}))

    # Simulate a crash AFTER the broker applied the patch but BEFORE the
    # journal SUCCEEDED: apply via a separate broker, then leave a RUNNING
    # journal row carrying the persisted precondition/effect hashes.
    WorkspaceBroker().apply_patch_set(env.ws, patch, Role.IMPLEMENTER, LEAD)
    env.sup.store._store._insert_supervisor_action({
        "id": "apply-crash", "supervisor_job_id": env.job.supervisor_job_id,
        "dispatch_id": d.id, "action_type": "APPLY_PATCH_SET",
        "action_key": key, "args_hash": args_hash,
        "input_hash": result_hash, "precondition_hash": precondition,
        "effect_hash": effect, "status": "RUNNING", "attempt_count": 1,
        "next_attempt_at": None, "started_at": "t", "finished_at": None,
        "last_error_code": None, "created_at": "t", "updated_at": "t",
    })

    # Recovery reconciles from the persisted effect hash -> SUCCEEDED, and the
    # counting broker is never invoked again (exactly-once effect).
    advance(env, ReconcileAction.RUN_SANDBOX_TESTS)
    assert env.calls["n"] == 0, "no re-apply after crash reconciliation"
    act = env.sup._latest_action(d.id, "APPLY_PATCH_SET")
    assert act["status"] == "SUCCEEDED"
    assert (env.ws / "src" / "module.py").read_text() == \
        "def parse_duration(s):\n    return None\n"


def test_f2_stuck_running_apply_reruns_when_not_applied(db_path, tmp_path):
    env, d = _make_write_env(db_path, tmp_path)
    patch = [{"op": "write", "path": "src/module.py",
              "content": base64.b64encode(b"def x():\n    pass\n").decode()}]
    result = _write_result(Role.IMPLEMENTER, env.task.id, d.id, "patch_set", patch)
    _bind_implementer_succeeded(env, d, result)

    wsp = WorkspaceHashProvider()
    precondition = wsp.scoped_hash(env.ws)  # still the pre-apply stub
    effect = wsp.predicted_hash(env.ws, patch)
    result_hash = _sha256(_canonical_json(result))
    key = f"supervisor:dispatch:{d.id}:apply:{result_hash}"
    args_hash = _sha256(_canonical_json(
        {"dispatch_id": d.id, "patch_set": patch,
         "workspace_root": str(env.ws.resolve())}))
    env.sup.store._store._insert_supervisor_action({
        "id": "apply-stuck", "supervisor_job_id": env.job.supervisor_job_id,
        "dispatch_id": d.id, "action_type": "APPLY_PATCH_SET",
        "action_key": key, "args_hash": args_hash,
        "input_hash": result_hash, "precondition_hash": precondition,
        "effect_hash": effect, "patch_set_json": _canonical_json(patch),
        "status": "RUNNING", "attempt_count": 1,
        "next_attempt_at": None, "started_at": "t", "finished_at": None,
        "last_error_code": None, "created_at": "t", "updated_at": "t",
    })
    # Workspace still at precondition -> reconcile re-applies exactly once.
    advance(env, ReconcileAction.RUN_SANDBOX_TESTS)
    assert env.calls["n"] == 1, "a not-applied RUNNING apply must be re-applied once"
    act = env.sup._latest_action(d.id, "APPLY_PATCH_SET")
    assert act["status"] == "SUCCEEDED"
    assert (env.ws / "src" / "module.py").read_text() == "def x():\n    pass\n"


def _crash_apply_intent(env, d, patch, *, applied):
    """Bind a valid write result and insert a RUNNING APPLY_PATCH_SET intent.

    ``applied=True`` simulates a crash AFTER the broker mutation but before the
    journal SUCCEEDED (the effect is on disk, the row is RUNNING).
    ``applied=False`` simulates a crash BEFORE the broker ran (the workspace is
    still at the persisted precondition).  Returns
    ``(result, result_hash, args_hash)`` for the persisted intent.
    """
    result = _write_result(Role.IMPLEMENTER, env.task.id, d.id, "patch_set", patch)
    _bind_implementer_succeeded(env, d, result)
    wsp = WorkspaceHashProvider()
    precondition = wsp.scoped_hash(env.ws)
    effect = wsp.predicted_hash(env.ws, patch)
    result_hash = _sha256(_canonical_json(result))
    key = f"supervisor:dispatch:{d.id}:apply:{result_hash}"
    args_hash = _sha256(_canonical_json(
        {"dispatch_id": d.id, "patch_set": patch,
         "workspace_root": str(env.ws.resolve())}))
    if applied:
        WorkspaceBroker().apply_patch_set(env.ws, patch, Role.IMPLEMENTER, LEAD)
    env.sup.store._store._insert_supervisor_action({
        "id": "apply-crash-r10", "supervisor_job_id": env.job.supervisor_job_id,
        "dispatch_id": d.id, "action_type": "APPLY_PATCH_SET",
        "action_key": key, "args_hash": args_hash,
        "input_hash": result_hash, "precondition_hash": precondition,
        "effect_hash": effect, "patch_set_json": _canonical_json(patch),
        "status": "RUNNING", "attempt_count": 1,
        "next_attempt_at": None, "started_at": "t", "finished_at": None,
        "last_error_code": None, "created_at": "t", "updated_at": "t",
    })
    return result, result_hash, args_hash


def _apply_rows(env, d):
    return [a for a in env.sup.store._store.list_supervisor_actions()
            if a["dispatch_id"] == d.id
            and a["action_type"] == "APPLY_PATCH_SET"]


def test_f2_crash_after_apply_changed_result_no_second_apply(db_path, tmp_path):
    """R7-F1 intersection: crash AFTER the broker applied A but BEFORE journal
    success, then recovery observes a DIFFERENT result B (same valid envelope,
    non-overlapping patch set).  Reconcile must NOT mint a second apply intent:
    exactly ONE apply intent total, A's row reconciles SUCCEEDED from the
    persisted effect hash (broker never re-invoked), B is never applied, and a
    hash-mismatch backoff is produced.  The workspace keeps ONLY A's files and
    the dispatch never advances to tests/record/consume with B."""
    env, d = _make_write_env(db_path, tmp_path)
    patch_a = [{"op": "write", "path": "src/module.py",
                "content": base64.b64encode(b"def a():\n    pass\n").decode()}]
    patch_b = [{"op": "write", "path": "src/other.py",
                "content": base64.b64encode(b"def b():\n    pass\n").decode()}]
    result_a, result_hash_a, _ = _crash_apply_intent(
        env, d, patch_a, applied=True)
    result_b = _write_result(Role.IMPLEMENTER, env.task.id, d.id,
                             "patch_set", patch_b)
    assert _write_envelope(Role.IMPLEMENTER, result_a) == \
        _write_envelope(Role.IMPLEMENTER, result_b)
    assert _sha256(_canonical_json(result_a)) != _sha256(_canonical_json(result_b))
    _reset_succeeded_result(env, d, result_b)

    dec = step(env)
    assert dec.action is ReconcileAction.APPLY_PATCH_SET, dec.action
    assert len(_apply_rows(env, d)) == 1, "must not mint a second apply intent"
    act = env.sup._latest_action(d.id, "APPLY_PATCH_SET")
    assert act["status"] == "SUCCEEDED"
    assert env.calls["n"] == 0, "broker must never be re-invoked for a changed result"
    # Workspace keeps ONLY A's files; B was never applied.
    assert (env.ws / "src" / "module.py").read_text() == "def a():\n    pass\n"
    assert not (env.ws / "src" / "other.py").exists()
    # The changed observation is failed closed: hash-mismatch backoff, never
    # tests/record/consume with B.
    dec2 = step(env)
    assert dec2.action is ReconcileAction.WAIT, dec2.action
    assert dec2.reason == "write_result_hash_mismatch", dec2.reason
    assert env.sup._latest_action(d.id, "RUN_SANDBOX_TESTS") is None
    assert env.sup._latest_action(d.id, "RECORD_TEST_RESULT") is None
    assert env.sup._latest_action(d.id, "CONSUME_RESULT") is None
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING


def test_f2_stuck_running_changed_result_reruns_persisted_intent(db_path, tmp_path):
    """R7-F1 stuck-RUNNING-with-B variant: a RUNNING apply intent where the
    workspace is still at the persisted precondition (A never applied) and
    recovery observes B.  The persisted A intent is re-applied exactly once
    (same key/args_hash, broker count == 1) and SUCCEEDED; B is not applied;
    a hash-mismatch backoff is produced."""
    env, d = _make_write_env(db_path, tmp_path)
    patch_a = [{"op": "write", "path": "src/module.py",
                "content": base64.b64encode(b"def a():\n    pass\n").decode()}]
    patch_b = [{"op": "write", "path": "src/other.py",
                "content": base64.b64encode(b"def b():\n    pass\n").decode()}]
    _crash_apply_intent(env, d, patch_a, applied=False)
    result_b = _write_result(Role.IMPLEMENTER, env.task.id, d.id,
                             "patch_set", patch_b)
    _reset_succeeded_result(env, d, result_b)

    dec = step(env)
    assert dec.action is ReconcileAction.APPLY_PATCH_SET, dec.action
    assert len(_apply_rows(env, d)) == 1
    assert env.calls["n"] == 1, "the persisted A intent must be re-applied once"
    act = env.sup._latest_action(d.id, "APPLY_PATCH_SET")
    assert act["status"] == "SUCCEEDED"
    # B was never applied: workspace has A's files only.
    assert (env.ws / "src" / "module.py").read_text() == "def a():\n    pass\n"
    assert not (env.ws / "src" / "other.py").exists()
    dec2 = step(env)
    assert dec2.action is ReconcileAction.WAIT, dec2.action
    assert dec2.reason == "write_result_hash_mismatch", dec2.reason


def test_f2_fresh_dispatch_no_intent_applies_result(db_path, tmp_path):
    """R7-F1 fresh-dispatch guard: with no apply intent yet, an observation B
    is applied normally (no false-positive block)."""
    env, d = _make_write_env(db_path, tmp_path)
    patch_b = [{"op": "write", "path": "src/other.py",
                "content": base64.b64encode(b"def b():\n    pass\n").decode()}]
    result_b = _write_result(Role.IMPLEMENTER, env.task.id, d.id,
                             "patch_set", patch_b)
    _bind_implementer_succeeded(env, d, result_b)
    advance(env, ReconcileAction.APPLY_PATCH_SET)
    assert env.calls["n"] == 1
    assert (env.ws / "src" / "other.py").read_text() == "def b():\n    pass\n"
    assert len(_apply_rows(env, d)) == 1
    assert env.sup._frozen_write_result_hash(d.id) == \
        _sha256(_canonical_json(result_b))


def test_f2_frozen_hash_is_first_persisted_intent(db_path, tmp_path):
    """R7-F1 frozen-hash-source: the frozen write-result hash binds at the
    FIRST persisted apply intent — even while the row is RUNNING (before any
    SUCCEEDED), and unchanged after the crash-window reconcile."""
    env, d = _make_write_env(db_path, tmp_path)
    patch_a = [{"op": "write", "path": "src/module.py",
                "content": base64.b64encode(b"def a():\n    pass\n").decode()}]
    _, result_hash_a, _ = _crash_apply_intent(env, d, patch_a, applied=True)
    # Freeze at the FIRST persisted intent regardless of RUNNING status.
    assert env.sup._frozen_write_result_hash(d.id) == result_hash_a
    dec = step(env)
    assert dec.action is ReconcileAction.APPLY_PATCH_SET
    act = env.sup._latest_action(d.id, "APPLY_PATCH_SET")
    assert act["status"] == "SUCCEEDED"
    assert env.calls["n"] == 0
    assert env.sup._frozen_write_result_hash(d.id) == result_hash_a


def _partial_apply_patch():
    """A two-file patch whose FIRST file can be applied alone to simulate a
    hard kill DURING a multi-file broker loop (partial workspace state)."""
    return [
        {"op": "write", "path": "src/a.py",
         "content": base64.b64encode(b"def a():\n    pass\n").decode()},
        {"op": "write", "path": "src/b.py",
         "content": base64.b64encode(b"def b():\n    pass\n").decode()},
    ]


def test_f1_partial_effect_crash_diverged_no_second_apply(db_path, tmp_path):
    """F1: a hard kill DURING a multi-file broker loop leaves a PARTIAL
    workspace (matches NEITHER the persisted precondition nor the persisted
    effect).  Recovery must keep the canonical intent as the immutable binding
    (status UNCERTAIN, NOT FAILED), never invoke the broker, never mint a
    second APPLY intent, keep ``_frozen_write_result_hash`` returning A's hash,
    and never advance the dispatch."""
    env, d = _make_write_env(db_path, tmp_path)
    patch_a = _partial_apply_patch()
    patch_b = [{"op": "write", "path": "src/other.py",
                "content": base64.b64encode(b"def b():\n    pass\n").decode()}]
    result_a, result_hash_a, _ = _crash_apply_intent(env, d, patch_a, applied=False)
    # Partial apply: write ONLY the first file (a SIGKILL after the first
    # os.replace), leaving the workspace at neither the precondition nor the
    # full effect hash.
    WorkspaceBroker().apply_patch_set(env.ws, patch_a[:1], Role.IMPLEMENTER, LEAD)
    result_b = _write_result(Role.IMPLEMENTER, env.task.id, d.id,
                             "patch_set", patch_b)
    _reset_succeeded_result(env, d, result_b)

    dec = step(env)
    assert dec.action is ReconcileAction.APPLY_PATCH_SET, dec.action
    act = env.sup._latest_action(d.id, "APPLY_PATCH_SET")
    assert act["status"] == "UNCERTAIN", act  # NOT FAILED: canonical kept
    assert act["last_error_code"] == "workspace_diverged"
    assert len(_apply_rows(env, d)) == 1, "must never mint a second apply intent"
    assert env.calls["n"] == 0, "broker must never be invoked on divergence"
    assert env.sup._frozen_write_result_hash(d.id) == result_hash_a

    # Bounded: WAIT (retry_count grows) then sticky PERSISTENT_ERROR; never
    # tests/record/consume, dispatch never advances.
    for _ in range(MAX_RUNTIME_UNKNOWN + 2):
        dec = step(env)
        if dec.action is ReconcileAction.PERSISTENT_ERROR:
            break
        assert dec.action is ReconcileAction.WAIT, dec.action
    else:
        raise AssertionError("never reached PERSISTENT_ERROR")
    assert dec.action is ReconcileAction.PERSISTENT_ERROR
    assert dec.reason == "workspace_diverged"
    assert len(_apply_rows(env, d)) == 1
    assert env.calls["n"] == 0
    assert env.sup._latest_action(d.id, "RUN_SANDBOX_TESTS") is None
    assert env.sup._latest_action(d.id, "RECORD_TEST_RESULT") is None
    assert env.sup._latest_action(d.id, "CONSUME_RESULT") is None
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING
    assert env.sup._frozen_write_result_hash(d.id) == result_hash_a


def test_f1_partial_effect_crash_same_observation_diverged(db_path, tmp_path):
    """F1: partial broker-effect crash with the observation UNCHANGED (still A).
    An indeterminate workspace is never silently re-applied — it is
    PERSISTENT_ERROR-bounded exactly like the changed-result case."""
    env, d = _make_write_env(db_path, tmp_path)
    patch_a = _partial_apply_patch()
    result_a, result_hash_a, _ = _crash_apply_intent(env, d, patch_a, applied=False)
    WorkspaceBroker().apply_patch_set(env.ws, patch_a[:1], Role.IMPLEMENTER, LEAD)

    dec = step(env)
    assert dec.action is ReconcileAction.APPLY_PATCH_SET, dec.action
    act = env.sup._latest_action(d.id, "APPLY_PATCH_SET")
    assert act["status"] == "UNCERTAIN", act
    assert act["last_error_code"] == "workspace_diverged"
    assert env.calls["n"] == 0
    assert len(_apply_rows(env, d)) == 1
    assert env.sup._frozen_write_result_hash(d.id) == result_hash_a

    for _ in range(MAX_RUNTIME_UNKNOWN + 2):
        dec = step(env)
        if dec.action is ReconcileAction.PERSISTENT_ERROR:
            break
        assert dec.action is ReconcileAction.WAIT, dec.action
    else:
        raise AssertionError("never reached PERSISTENT_ERROR")
    assert dec.action is ReconcileAction.PERSISTENT_ERROR
    assert env.calls["n"] == 0
    assert len(_apply_rows(env, d)) == 1
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING
    assert env.sup._frozen_write_result_hash(d.id) == result_hash_a


def _diverging_broker_factory(env, stray_relpath="src/stray.py"):
    """A broker that applies the patch set then writes a STRAY file so the
    workspace hash diverges from the predicted effect hash (a post-broker
    mismatch, NOT a pre-commit rejection)."""

    class DivergingBroker(WorkspaceBroker):
        def apply_patch_set(self, workspace, patch_set, role, source):
            env.calls["n"] += 1
            result = super().apply_patch_set(workspace, patch_set, role, source)
            (Path(workspace) / stray_relpath).write_text("# stray\n")
            return result

    return DivergingBroker


def test_f1_post_broker_divergence_reapply_uncertain_no_second_apply(
        db_path, tmp_path):
    """F1/R10-F1/R9/R7-F1: a canonical RUNNING apply intent A is re-entered at
    its exact precondition; the broker re-applies A, then a STRAY change makes
    the workspace diverge from the persisted effect.  The row must become
    UNCERTAIN/workspace_diverged (NEVER FAILED — FAILED would drop the frozen
    binding and permit a second result-keyed intent), ``_frozen_write_result_hash``
    keeps returning A's hash, the broker is invoked at most once, and a later
    observation B never mints a second APPLY intent (B's file absent)."""
    env, d = _make_write_env(db_path, tmp_path)
    patch_a = [{"op": "write", "path": "src/module.py",
                "content": base64.b64encode(b"def a():\n    pass\n").decode()}]
    patch_b = [{"op": "write", "path": "src/other.py",
                "content": base64.b64encode(b"def b():\n    pass\n").decode()}]
    result_a, result_hash_a, _ = _crash_apply_intent(env, d, patch_a, applied=False)
    env.sup._broker_factory = lambda: _diverging_broker_factory(env)()
    result_b = _write_result(Role.IMPLEMENTER, env.task.id, d.id,
                             "patch_set", patch_b)
    _reset_succeeded_result(env, d, result_b)

    dec = step(env)
    assert dec.action is ReconcileAction.APPLY_PATCH_SET, dec.action
    act = env.sup._latest_action(d.id, "APPLY_PATCH_SET")
    assert act["status"] == "UNCERTAIN", act  # NOT FAILED: canonical kept
    assert act["last_error_code"] == "workspace_diverged"
    assert len(_apply_rows(env, d)) == 1, "must never mint a second apply intent"
    assert env.calls["n"] == 1, "broker invoked exactly once (the re-apply)"
    assert env.sup._frozen_write_result_hash(d.id) == result_hash_a
    assert (env.ws / "src" / "module.py").read_text() == "def a():\n    pass\n"
    assert not (env.ws / "src" / "other.py").exists(), "B must never be applied"

    # Bounded sticky: WAIT (retry_count grows) then PERSISTENT_ERROR; never
    # tests/record/consume, dispatch never advances, B never mints an intent.
    for _ in range(MAX_RUNTIME_UNKNOWN + 2):
        dec = step(env)
        if dec.action is ReconcileAction.PERSISTENT_ERROR:
            break
        assert dec.action is ReconcileAction.WAIT, dec.action
    else:
        raise AssertionError("never reached PERSISTENT_ERROR")
    assert dec.action is ReconcileAction.PERSISTENT_ERROR
    assert dec.reason == "workspace_diverged"
    assert env.calls["n"] == 1, "no further broker invocation"
    assert len(_apply_rows(env, d)) == 1
    assert not (env.ws / "src" / "other.py").exists()
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING
    assert env.sup._frozen_write_result_hash(d.id) == result_hash_a


def test_f1_post_broker_divergence_fresh_uncertain_no_second_apply(
        db_path, tmp_path):
    """F1: no prior apply row; the broker applies A then a STRAY change
    diverges the workspace from the predicted effect.  The freshly committed
    row must become UNCERTAIN/workspace_diverged (NOT FAILED), the frozen
    binding is A's hash, and a later observation B never mints a second APPLY
    intent (B's file absent)."""
    env, d = _make_write_env(db_path, tmp_path)
    patch_a = [{"op": "write", "path": "src/module.py",
                "content": base64.b64encode(b"def a():\n    pass\n").decode()}]
    patch_b = [{"op": "write", "path": "src/other.py",
                "content": base64.b64encode(b"def b():\n    pass\n").decode()}]
    env.sup._broker_factory = lambda: _diverging_broker_factory(env)()
    result_a = _write_result(Role.IMPLEMENTER, env.task.id, d.id,
                             "patch_set", patch_a)
    result_hash_a = _sha256(_canonical_json(result_a))
    _bind_implementer_succeeded(env, d, result_a)

    advance(env, ReconcileAction.APPLY_PATCH_SET)
    act = env.sup._latest_action(d.id, "APPLY_PATCH_SET")
    assert act["status"] == "UNCERTAIN", act  # NOT FAILED
    assert act["last_error_code"] == "workspace_diverged"
    assert len(_apply_rows(env, d)) == 1
    assert env.calls["n"] == 1
    assert env.sup._frozen_write_result_hash(d.id) == result_hash_a

    # A later observation B must never mint a second APPLY intent.
    result_b = _write_result(Role.IMPLEMENTER, env.task.id, d.id,
                             "patch_set", patch_b)
    _reset_succeeded_result(env, d, result_b)
    for _ in range(MAX_RUNTIME_UNKNOWN + 2):
        dec = step(env)
        if dec.action is ReconcileAction.PERSISTENT_ERROR:
            break
        assert dec.action is ReconcileAction.WAIT, dec.action
    else:
        raise AssertionError("never reached PERSISTENT_ERROR")
    assert dec.action is ReconcileAction.PERSISTENT_ERROR
    assert dec.reason == "workspace_diverged"
    assert len(_apply_rows(env, d)) == 1
    assert env.calls["n"] == 1
    assert not (env.ws / "src" / "other.py").exists()
    assert env.sup._frozen_write_result_hash(d.id) == result_hash_a


def test_f1_missing_persisted_patch_set_uncertain_not_failed(db_path, tmp_path):
    """F1: a committed RUNNING apply intent whose persisted patch set is
    missing (the payload was lost AFTER the intent committed) is a
    divergence/ambiguity — UNCERTAIN/workspace_diverged, never FAILED (FAILED
    would drop the frozen binding and let a second result-keyed intent be
    minted)."""
    env, d = _make_write_env(db_path, tmp_path)
    patch_a = [{"op": "write", "path": "src/module.py",
                "content": base64.b64encode(b"def a():\n    pass\n").decode()}]
    result_a, result_hash_a, _ = _crash_apply_intent(env, d, patch_a, applied=False)
    rows = _apply_rows(env, d)
    assert len(rows) == 1
    # Strip the persisted patch set from the committed row.
    env.sup.store._store._update_supervisor_action(rows[0]["id"], patch_set_json=None)

    dec = step(env)
    assert dec.action is ReconcileAction.APPLY_PATCH_SET, dec.action
    act = env.sup._latest_action(d.id, "APPLY_PATCH_SET")
    assert act["status"] == "UNCERTAIN", act  # NOT FAILED
    assert act["last_error_code"] == "workspace_diverged"
    assert env.calls["n"] == 0, "broker must never be invoked on missing payload"
    assert len(_apply_rows(env, d)) == 1
    assert env.sup._frozen_write_result_hash(d.id) == result_hash_a


# --- R13-F1: atomic canonical write-intent claim (concurrent APPLY race) ----

def _make_two_controller_env(db_path, tmp_path):
    """Two Supervisor controllers sharing ONE Core/DB (R13 race setup).

    Returns ``(env_a, env_b, d)`` where ``d`` is the bound-RUNNING implementer
    dispatch and each env carries its own scriptable run-status provider (so A
    and B can observe different results for the SAME dispatch).
    """
    ws = make_workspace(tmp_path)
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    core.start_task_run(task.id, OWNER)
    calls = {"n": 0}

    class CountingBroker(WorkspaceBroker):
        def apply_patch_set(self, *a, **k):
            calls["n"] += 1
            return super().apply_patch_set(*a, **k)

    sup_a = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher(),
                       clock=clock, workspace_root=ws,
                       run_tests_fn=fake_run_tests,
                       broker_factory=lambda: CountingBroker())
    sup_b = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher(),
                       clock=clock, workspace_root=ws,
                       run_tests_fn=fake_run_tests,
                       broker_factory=lambda: CountingBroker())
    job = sup_a.store.create_job(task.id, idempotency_key="job-1")
    env_a = SimpleNamespace(core=core, task=task, prov=sup_a._run_status,
                            sup=sup_a, job=job, clock=clock, ws=ws, calls=calls)
    # Drive to the implementer frontier with controller A.
    drive_frontier(env_a, Role.LEAD)
    drive_frontier(env_a, Role.ANALYST)
    drive_frontier(env_a, Role.LEAD)
    advance(env_a, ReconcileAction.START_ROLE)
    advance(env_a, ReconcileAction.CREATE_DISPATCH)
    d = core.queries.list_dispatches(task.id)[-1]
    assert d.role is Role.IMPLEMENTER
    provider, model, thinking, session = canonical_binding(d)
    run_id = f"run-{d.id[:8]}"
    env_a.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    advance(env_a, ReconcileAction.BIND_RUN)
    env_b = SimpleNamespace(core=core, task=task, prov=sup_b._run_status,
                            sup=sup_b, job=job, clock=clock, ws=ws, calls=calls)
    return env_a, env_b, d


def test_r13_concurrent_apply_claim_single_intent(db_path, tmp_path, monkeypatch):
    """R13-F1 (HIGH): deterministic two-controller APPLY claim race.

    A and B observe DIFFERENT valid results.  B reconciles second (fresh
    decision) and executes first, winning the canonical write-intent claim; A's
    decision is now stale, but A's committed-intents read happened BEFORE B
    executed.  Reproducing that pre-B read deterministically forces A's atomic
    claim (not the status-filtered SELECT) to be the enforcement: A must fail
    closed to B's winner.  Exactly ONE APPLY intent row, ONE broker invocation,
    B's files only, frozen hash == B's hash, and A never advances to
    tests/record/consume.
    """
    env_a, env_b, d = _make_two_controller_env(db_path, tmp_path)
    patch_a = [{"op": "write", "path": "src/a.py",
                "content": base64.b64encode(b"AAA").decode()}]
    patch_b = [{"op": "write", "path": "src/b.py",
                "content": base64.b64encode(b"BBB").decode()}]
    result_a = _write_result(Role.IMPLEMENTER, env_a.task.id, d.id,
                             "patch_set", patch_a)
    result_b = _write_result(Role.IMPLEMENTER, env_b.task.id, d.id,
                             "patch_set", patch_b)
    assert _sha256(_canonical_json(result_a)) != _sha256(_canonical_json(result_b))
    _reset_succeeded_result(env_a, d, result_a)
    _reset_succeeded_result(env_b, d, result_b)

    # A reconciles first (its decision becomes stale once B reconciles).
    decision_a = env_a.sup.reconcile(env_a.job.supervisor_job_id)
    assert decision_a.action is ReconcileAction.APPLY_PATCH_SET
    # B reconciles second (fresh) and executes first -> B wins the claim.
    decision_b = env_b.sup.reconcile(env_b.job.supervisor_job_id)
    assert decision_b.action is ReconcileAction.APPLY_PATCH_SET
    outcome_b = env_b.sup.perform_next_safe_action_if_required(decision_b)
    assert outcome_b.status == "executed", outcome_b

    # A resumes with its pre-B committed-intents read (empty): the ATOMIC
    # claim must detect B's winner and fail closed (never apply A).
    monkeypatch.setattr(env_a.sup, "_committed_apply_intents", lambda did: [])
    job_dict = env_a.core._store.get_supervisor_job(env_a.job.supervisor_job_id)
    outcome_a = env_a.sup._perform_apply_patch_set(decision_a, job_dict)

    assert outcome_a.status == "failed", outcome_a
    assert outcome_a.detail == "write_result_hash_mismatch", outcome_a
    assert len(_apply_rows(env_a, d)) == 1, "exactly ONE APPLY intent row (the winner)"
    assert env_a.calls["n"] == 1, "exactly ONE broker invocation"
    # Winner's files only: B applied, A never did.
    assert (env_a.ws / "src" / "b.py").read_text() == "BBB"
    assert not (env_a.ws / "src" / "a.py").exists()
    # Frozen hash == winner's hash; canonical intent table has exactly one row.
    b_hash = _sha256(_canonical_json(result_b))
    assert env_a.sup._frozen_write_result_hash(d.id) == b_hash
    winner = env_a.core._store.get_dispatch_write_intent(d.id)
    assert winner is not None
    assert winner["canonical_input_hash"] == b_hash
    # A's observation never advances: hash-mismatch backoff, no downstream rows.
    dec = env_a.sup.reconcile(env_a.job.supervisor_job_id)
    assert dec.action is ReconcileAction.WAIT, dec.action
    assert dec.reason == "write_result_hash_mismatch", dec.reason
    assert env_a.sup._latest_action(d.id, "RUN_SANDBOX_TESTS") is None
    assert env_a.sup._latest_action(d.id, "RECORD_TEST_RESULT") is None
    assert env_a.sup._latest_action(d.id, "CONSUME_RESULT") is None


def test_r13_concurrent_apply_claim_reversed_order(db_path, tmp_path, monkeypatch):
    """R13-F1 reversed order: A reconciles second (fresh) and executes first,
    winning; B (whose committed-intents read predates A's execution) reaches the
    atomic claim and fails closed.  Same single-intent invariant regardless of
    which controller executes first."""
    env_a, env_b, d = _make_two_controller_env(db_path, tmp_path)
    patch_a = [{"op": "write", "path": "src/a.py",
                "content": base64.b64encode(b"AAA").decode()}]
    patch_b = [{"op": "write", "path": "src/b.py",
                "content": base64.b64encode(b"BBB").decode()}]
    result_a = _write_result(Role.IMPLEMENTER, env_a.task.id, d.id,
                             "patch_set", patch_a)
    result_b = _write_result(Role.IMPLEMENTER, env_b.task.id, d.id,
                             "patch_set", patch_b)
    _reset_succeeded_result(env_a, d, result_a)
    _reset_succeeded_result(env_b, d, result_b)

    # B reconciles first (stale); A reconciles second (fresh) and executes first.
    decision_b = env_b.sup.reconcile(env_b.job.supervisor_job_id)
    assert decision_b.action is ReconcileAction.APPLY_PATCH_SET
    decision_a = env_a.sup.reconcile(env_a.job.supervisor_job_id)
    assert decision_a.action is ReconcileAction.APPLY_PATCH_SET
    outcome_a = env_a.sup.perform_next_safe_action_if_required(decision_a)
    assert outcome_a.status == "executed", outcome_a

    monkeypatch.setattr(env_b.sup, "_committed_apply_intents", lambda did: [])
    job_dict = env_b.core._store.get_supervisor_job(env_b.job.supervisor_job_id)
    outcome_b = env_b.sup._perform_apply_patch_set(decision_b, job_dict)

    assert outcome_b.status == "failed", outcome_b
    assert outcome_b.detail == "write_result_hash_mismatch", outcome_b
    assert len(_apply_rows(env_b, d)) == 1
    assert env_b.calls["n"] == 1
    assert (env_b.ws / "src" / "a.py").read_text() == "AAA"
    assert not (env_b.ws / "src" / "b.py").exists()
    a_hash = _sha256(_canonical_json(result_a))
    assert env_b.sup._frozen_write_result_hash(d.id) == a_hash
    winner = env_b.core._store.get_dispatch_write_intent(d.id)
    assert winner["canonical_input_hash"] == a_hash
    dec = env_b.sup.reconcile(env_b.job.supervisor_job_id)
    assert dec.action is ReconcileAction.WAIT, dec.action
    assert dec.reason == "write_result_hash_mismatch", dec.reason


def test_r13_normal_single_controller_single_intent(db_path, tmp_path):
    """R13-F1 guard: a normal single-controller apply still creates exactly ONE
    canonical intent (one APPLY row + one dispatch_write_intents row) and
    applies normally."""
    env, d = _make_write_env(db_path, tmp_path)
    patch = [{"op": "write", "path": "src/module.py",
              "content": base64.b64encode(b"XXX").decode()}]
    result = _write_result(Role.IMPLEMENTER, env.task.id, d.id, "patch_set", patch)
    _bind_implementer_succeeded(env, d, result)
    advance(env, ReconcileAction.APPLY_PATCH_SET)
    assert env.calls["n"] == 1
    assert len(_apply_rows(env, d)) == 1
    winner = env.core._store.get_dispatch_write_intent(d.id)
    assert winner is not None
    assert winner["canonical_input_hash"] == _sha256(_canonical_json(result))
    assert env.sup._frozen_write_result_hash(d.id) == winner["canonical_input_hash"]
    assert (env.ws / "src" / "module.py").read_text() == "XXX"


def test_r13_dispatch_write_intents_table_exists(db_path):
    """R13-F1: a fresh DB carries dispatch_write_intents with a PRIMARY KEY on
    dispatch_id."""
    core = Core(db_path)
    try:
        names = {r["name"] for r in core._store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "dispatch_write_intents" in names
        sql = core._store._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='dispatch_write_intents'").fetchone()[0]
        assert "dispatch_id" in sql and "PRIMARY KEY" in sql
    finally:
        core.close()


def test_r13_dispatch_write_intents_primary_key_blocks_second_row(db_path, tmp_path):
    """R13-F1: the DB-level PRIMARY KEY enforces the single-intent invariant."""
    env, d = _make_write_env(db_path, tmp_path)
    store = env.core._store
    store._insert_dispatch_write_intent({
        "dispatch_id": d.id, "canonical_input_hash": "h1",
        "intent_action_id": "a1", "created_at": "t", "updated_at": "t",
    })
    with pytest.raises(sqlite3.IntegrityError):
        store._insert_dispatch_write_intent({
            "dispatch_id": d.id, "canonical_input_hash": "h2",
            "intent_action_id": "a2", "created_at": "t", "updated_at": "t",
        })


def test_r13_dispatch_write_intents_migration_existing_v4(tmp_path):
    """R13-F1: a pre-existing V4 DB (built before this table) gains
    dispatch_write_intents on reopen.  The schema is now V7 (additive
    notification_outbox + owner-approval-challenge + durable-queue/lease
    migration), so the version is UPSERTed to 7."""
    db = str(tmp_path / "v4.db")
    c = Core(db)
    c._store._conn.execute("DROP TABLE dispatch_write_intents")
    c.close()

    c2 = Core(db)
    try:
        names = {r["name"] for r in c2._store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "dispatch_write_intents" in names
        row = c2._store._conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        assert row["value"] == SCHEMA_VERSION
    finally:
        c2.close()


# --- R14-F1: cross-controller exclusive broker execution (overlap fence) ----

def _make_two_connection_env(db_path, tmp_path, broker, ws_b=None):
    """Two Supervisor controllers over TWO independent Core/SQLite connections
    to the SAME DB file, sharing one workspace (R14-F1 overlap setup).  ``ws_b``
    lets the second controller use a DIFFERENT spelling of the same physical
    workspace (e.g. a symlink alias) to exercise the R15 canonical-root fence."""
    ws = make_workspace(tmp_path)
    clock = FakeClock()
    core_a = Core(db_path, clock=clock)
    core_b = Core(db_path, clock=clock)
    # The two controllers run in separate threads, each using its own
    # connection.  SQLite connections are thread-bound by default, so re-open
    # both with check_same_thread=False (the schema already exists on disk) and
    # a busy_timeout so a short BEGIN IMMEDIATE waits for the other's write
    # lock instead of raising "database is locked".
    for core in (core_a, core_b):
        core._store._conn.close()
        conn = sqlite3.connect(db_path, isolation_level=None,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        core._store._conn = conn
    sup_a = Supervisor(core_a, FakeRunStatusProvider(), FakeRunLauncher(),
                       clock=clock, workspace_root=ws,
                       run_tests_fn=fake_run_tests, broker_factory=lambda: broker)
    sup_b = Supervisor(core_b, FakeRunStatusProvider(), FakeRunLauncher(),
                       clock=clock,
                       workspace_root=(ws if ws_b is None else ws_b),
                       run_tests_fn=fake_run_tests, broker_factory=lambda: broker)

    project = core_a.create_project("p", OWNER)
    task = core_a.create_task(project.id, "t", OWNER)
    core_a.start_task_run(task.id, OWNER)
    job = sup_a.store.create_job(task.id, idempotency_key="job-1")

    env_a = SimpleNamespace(core=core_a, task=task, prov=sup_a._run_status,
                            sup=sup_a, job=job, clock=clock, ws=ws)
    # Drive to the implementer frontier with controller A.
    drive_frontier(env_a, Role.LEAD)
    drive_frontier(env_a, Role.ANALYST)
    drive_frontier(env_a, Role.LEAD)
    advance(env_a, ReconcileAction.START_ROLE)
    advance(env_a, ReconcileAction.CREATE_DISPATCH)
    d = core_a.queries.list_dispatches(task.id)[-1]
    assert d.role is Role.IMPLEMENTER
    provider, model, thinking, session = canonical_binding(d)
    run_id = f"run-{d.id[:8]}"
    env_a.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    advance(env_a, ReconcileAction.BIND_RUN)
    env_b = SimpleNamespace(core=core_b, task=task, prov=sup_b._run_status,
                            sup=sup_b, job=job, clock=clock, ws=ws)
    return env_a, env_b, d


def test_r14_fenced_exclusive_broker_execution_same_result(db_path, tmp_path):
    """R14-F1 (HIGH): two controllers sharing ONE canonical RUNNING intent must
    execute the broker EXACTLY once (cross-controller exclusive-execution
    fence).  A barrier-broker holds the winner inside the critical section so
    the loser deterministically reaches the fence while the winner still holds
    the lock; only ONE broker invocation results."""
    import threading
    import time
    calls = {"n": 0}
    entered = threading.Event()
    release = threading.Event()

    class BarrierBroker(WorkspaceBroker):
        def apply_patch_set(self, *a, **k):
            calls["n"] += 1
            entered.set()
            release.wait(timeout=20)
            return super().apply_patch_set(*a, **k)

    env_a, env_b, d = _make_two_connection_env(db_path, tmp_path, BarrierBroker())
    patch = [{"op": "write", "path": "src/module.py",
              "content": base64.b64encode(b"def fenced():\n    pass\n").decode()}]
    result = _write_result(Role.IMPLEMENTER, env_a.task.id, d.id, "patch_set", patch)
    result_hash = _sha256(_canonical_json(result))
    _reset_succeeded_result(env_a, d, result)
    _reset_succeeded_result(env_b, d, result)

    decision_a = env_a.sup.reconcile(env_a.job.supervisor_job_id)
    decision_b = env_b.sup.reconcile(env_b.job.supervisor_job_id)
    assert decision_a.action is ReconcileAction.APPLY_PATCH_SET, decision_a.action
    assert decision_b.action is ReconcileAction.APPLY_PATCH_SET, decision_b.action

    job_a = env_a.core._store.get_supervisor_job(env_a.job.supervisor_job_id)
    job_b = env_b.core._store.get_supervisor_job(env_b.job.supervisor_job_id)
    outcomes = {}

    def run(sup, decision, job, name):
        try:
            outcomes[name] = sup._perform_apply_patch_set(decision, job)
        except Exception as exc:  # noqa: BLE001
            outcomes[name] = exc

    ta = threading.Thread(target=run, args=(env_a.sup, decision_a, job_a, "a"))
    ta.start()
    assert entered.wait(timeout=10), "winner never entered the broker"
    tb = threading.Thread(target=run, args=(env_b.sup, decision_b, job_b, "b"))
    tb.start()
    time.sleep(0.3)  # let the loser reach the fence (blocks on the flock)
    assert calls["n"] == 1, "loser must not invoke the broker while the winner holds the lock"
    release.set()
    ta.join(timeout=20)
    tb.join(timeout=20)
    assert not ta.is_alive() and not tb.is_alive(), "controllers did not finish"

    assert not isinstance(outcomes.get("a"), Exception), outcomes.get("a")
    assert not isinstance(outcomes.get("b"), Exception), outcomes.get("b")
    assert calls["n"] == 1, "broker invoked exactly once (fenced)"
    assert len(_apply_rows(env_a, d)) == 1, "exactly one APPLY row"
    winner = env_a.core._store.get_dispatch_write_intent(d.id)
    assert winner is not None
    assert winner["canonical_input_hash"] == result_hash
    act = env_a.sup._latest_action(d.id, "APPLY_PATCH_SET")
    assert act["status"] == "SUCCEEDED", act
    assert (env_a.ws / "src" / "module.py").read_text() == "def fenced():\n    pass\n"


def test_r14_fenced_broker_failure_no_double_execution(db_path, tmp_path):
    """R14-F1: the winner's broker returns errors (row FAILED); the loser, after
    acquiring the lock, refetches the FAILED row and does NOT invoke the broker
    a second time (broker count == 1 total).  The bounded retry machinery owns
    the next attempt on a later cycle."""
    import threading
    import time
    from argent_core.workspace_broker import BrokerResult
    calls = {"n": 0}
    entered = threading.Event()
    release = threading.Event()

    class FailingBarrierBroker(WorkspaceBroker):
        def apply_patch_set(self, *a, **k):
            calls["n"] += 1
            entered.set()
            release.wait(timeout=20)
            return BrokerResult(applied=[], skipped=[], errors=[
                {"op": "write", "path": "src/module.py", "error": "simulated_failure"},
            ])

    env_a, env_b, d = _make_two_connection_env(
        db_path, tmp_path, FailingBarrierBroker())
    patch = [{"op": "write", "path": "src/module.py",
              "content": base64.b64encode(b"def x():\n    pass\n").decode()}]
    result = _write_result(Role.IMPLEMENTER, env_a.task.id, d.id, "patch_set", patch)
    _reset_succeeded_result(env_a, d, result)
    _reset_succeeded_result(env_b, d, result)

    decision_a = env_a.sup.reconcile(env_a.job.supervisor_job_id)
    decision_b = env_b.sup.reconcile(env_b.job.supervisor_job_id)
    job_a = env_a.core._store.get_supervisor_job(env_a.job.supervisor_job_id)
    job_b = env_b.core._store.get_supervisor_job(env_b.job.supervisor_job_id)
    outcomes = {}

    def run(sup, decision, job, name):
        try:
            outcomes[name] = sup._perform_apply_patch_set(decision, job)
        except Exception as exc:  # noqa: BLE001
            outcomes[name] = exc

    ta = threading.Thread(target=run, args=(env_a.sup, decision_a, job_a, "a"))
    ta.start()
    assert entered.wait(timeout=10)
    tb = threading.Thread(target=run, args=(env_b.sup, decision_b, job_b, "b"))
    tb.start()
    time.sleep(0.3)
    assert calls["n"] == 1
    release.set()
    ta.join(timeout=20)
    tb.join(timeout=20)
    assert not ta.is_alive() and not tb.is_alive()

    assert calls["n"] == 1, "broker invoked exactly once even on failure"
    assert len(_apply_rows(env_a, d)) == 1
    winner = env_a.core._store.get_dispatch_write_intent(d.id)
    assert winner is not None
    act = env_a.sup._latest_action(d.id, "APPLY_PATCH_SET")
    assert act["status"] == "FAILED", act  # winner's broker failure
    assert act["last_error_code"] == "broker:1", act
    assert not isinstance(outcomes.get("b"), Exception), outcomes.get("b")
    assert outcomes["b"].status == "failed", outcomes["b"]
    assert (env_a.ws / "src" / "module.py").read_text() == "# stub\n"


def test_r14_waiter_after_winner_no_second_apply(db_path, tmp_path):
    """R14-F1 waiter-after-winner: controller A completes the apply fully, then
    controller B runs its (stale) APPLY decision — B observes SUCCEEDED and
    never invokes the broker (count stays 1), no second intent."""
    calls = {"n": 0}

    class CountingBroker(WorkspaceBroker):
        def apply_patch_set(self, *a, **k):
            calls["n"] += 1
            return super().apply_patch_set(*a, **k)

    env_a, env_b, d = _make_two_connection_env(db_path, tmp_path, CountingBroker())
    patch = [{"op": "write", "path": "src/module.py",
              "content": base64.b64encode(b"def fenced():\n    pass\n").decode()}]
    result = _write_result(Role.IMPLEMENTER, env_a.task.id, d.id, "patch_set", patch)
    result_hash = _sha256(_canonical_json(result))
    _reset_succeeded_result(env_a, d, result)
    _reset_succeeded_result(env_b, d, result)

    decision_a = env_a.sup.reconcile(env_a.job.supervisor_job_id)
    decision_b = env_b.sup.reconcile(env_b.job.supervisor_job_id)
    assert decision_a.action is ReconcileAction.APPLY_PATCH_SET, decision_a.action
    assert decision_b.action is ReconcileAction.APPLY_PATCH_SET, decision_b.action

    job_a = env_a.core._store.get_supervisor_job(env_a.job.supervisor_job_id)
    job_b = env_b.core._store.get_supervisor_job(env_b.job.supervisor_job_id)

    out_a = env_a.sup._perform_apply_patch_set(decision_a, job_a)
    assert out_a.status == "executed", out_a
    assert calls["n"] == 1

    out_b = env_b.sup._perform_apply_patch_set(decision_b, job_b)
    assert out_b.status == "already_succeeded", out_b
    assert calls["n"] == 1, "broker must not be invoked by the waiter"
    assert len(_apply_rows(env_a, d)) == 1
    winner = env_a.core._store.get_dispatch_write_intent(d.id)
    assert winner["canonical_input_hash"] == result_hash
    act = env_a.sup._latest_action(d.id, "APPLY_PATCH_SET")
    assert act["status"] == "SUCCEEDED"
    assert (env_a.ws / "src" / "module.py").read_text() == "def fenced():\n    pass\n"


# --- R15: F1 canonical-root fence + F2 bounded lock-failure backoff ---------

def test_r15_symlink_alias_single_fence_exactly_once(db_path, tmp_path):
    """F1 (R15, HIGH): two controllers naming the SAME physical workspace via
    two path spellings (real path vs symlink alias) must derive the SAME
    canonical lockfile and execute the broker EXACTLY once.  A barrier-broker
    holds the winner inside the critical section so the loser deterministically
    reaches the fence; exactly ONE broker invocation, exactly one APPLY row,
    exactly one dispatch_write_intents row, and the intent ends SUCCEEDED."""
    import threading
    import time
    calls = {"n": 0}
    entered = threading.Event()
    release = threading.Event()

    class BarrierBroker(WorkspaceBroker):
        def apply_patch_set(self, *a, **k):
            calls["n"] += 1
            entered.set()
            release.wait(timeout=20)
            return super().apply_patch_set(*a, **k)

    # Controller B accesses the SAME physical workspace through a symlink
    # alias (a DIFFERENT raw spelling of the same physical path).
    alias = tmp_path / "ws-alias"
    alias.symlink_to("ws", target_is_directory=True)

    env_a, env_b, d = _make_two_connection_env(
        db_path, tmp_path, BarrierBroker(), ws_b=alias)

    # Both controllers must freeze the SAME canonical root and lockfile, even
    # though B was constructed from the alias spelling.
    assert env_a.sup._workspace_root == env_b.sup._workspace_root
    assert str(env_a.sup._workspace_root) == str(env_a.ws.resolve())
    assert env_a.sup._apply_lock_path(d.id) == env_b.sup._apply_lock_path(d.id)

    patch = [{"op": "write", "path": "src/module.py",
              "content": base64.b64encode(b"def fenced():\n    pass\n").decode()}]
    result = _write_result(Role.IMPLEMENTER, env_a.task.id, d.id, "patch_set", patch)
    result_hash = _sha256(_canonical_json(result))
    _reset_succeeded_result(env_a, d, result)
    _reset_succeeded_result(env_b, d, result)

    decision_a = env_a.sup.reconcile(env_a.job.supervisor_job_id)
    decision_b = env_b.sup.reconcile(env_b.job.supervisor_job_id)
    assert decision_a.action is ReconcileAction.APPLY_PATCH_SET, decision_a.action
    assert decision_b.action is ReconcileAction.APPLY_PATCH_SET, decision_b.action

    job_a = env_a.core._store.get_supervisor_job(env_a.job.supervisor_job_id)
    job_b = env_b.core._store.get_supervisor_job(env_b.job.supervisor_job_id)
    outcomes = {}

    def run(sup, decision, job, name):
        try:
            outcomes[name] = sup._perform_apply_patch_set(decision, job)
        except Exception as exc:  # noqa: BLE001
            outcomes[name] = exc

    ta = threading.Thread(target=run, args=(env_a.sup, decision_a, job_a, "a"))
    ta.start()
    assert entered.wait(timeout=10), "winner never entered the broker"
    tb = threading.Thread(target=run, args=(env_b.sup, decision_b, job_b, "b"))
    tb.start()
    time.sleep(0.3)  # let the loser reach the fence (blocks on the flock)
    assert calls["n"] == 1, "loser must not invoke the broker while the winner holds the lock"
    release.set()
    ta.join(timeout=20)
    tb.join(timeout=20)
    assert not ta.is_alive() and not tb.is_alive(), "controllers did not finish"

    assert not isinstance(outcomes.get("a"), Exception), outcomes.get("a")
    assert not isinstance(outcomes.get("b"), Exception), outcomes.get("b")
    assert calls["n"] == 1, "broker invoked exactly once (canonical fence)"
    assert len(_apply_rows(env_a, d)) == 1, "exactly one APPLY row"
    winner = env_a.core._store.get_dispatch_write_intent(d.id)
    assert winner is not None
    assert winner["canonical_input_hash"] == result_hash
    n_intents = env_a.core._store._conn.execute(
        "SELECT COUNT(*) FROM dispatch_write_intents WHERE dispatch_id = ?",
        (d.id,)).fetchone()[0]
    assert n_intents == 1, "exactly one dispatch_write_intents row"
    act = env_a.sup._latest_action(d.id, "APPLY_PATCH_SET")
    assert act["status"] == "SUCCEEDED", act
    assert (env_a.ws / "src" / "module.py").read_text() == "def fenced():\n    pass\n"


def test_r15_lock_open_failure_bounded_error(db_path, tmp_path):
    """F2 (R15, MEDIUM): a persistent lock OPEN failure (lock root not
    creatable) must land in a bounded job-level backoff -> sticky
    PERSISTENT_ERROR, never a livelock with retry_count=0, never a broker
    invocation, and never mark the canonical RUNNING intent FAILED."""
    env, d = _make_write_env(db_path, tmp_path)
    patch = [{"op": "write", "path": "src/module.py",
              "content": base64.b64encode(b"def x():\n    pass\n").decode()}]
    result = _write_result(Role.IMPLEMENTER, env.task.id, d.id, "patch_set", patch)
    _bind_implementer_succeeded(env, d, result)

    # Point the lock root at a location that cannot be created: a regular file
    # occupies the lock-directory path, so ``Path.mkdir`` raises (OSError) and
    # ``_acquire_dispatch_lock`` fails closed on the open path.
    blocker = tmp_path / "lock-blocker"
    blocker.write_text("x")
    env.sup._apply_lock_path = lambda dispatch_id: (
        blocker / f"apply-{dispatch_id}.lock"
    )

    # First attempt: intent minted, lock open fails, retry_count persists 1.
    step(env)
    job = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    assert job["retry_count"] == 1, job["retry_count"]
    assert job["status"] == SupervisorJobStatus.BACKOFF.value, job["status"]
    assert job["next_wake_at"] is not None
    assert job["last_error_code"] == "apply_lock_unavailable"

    # Drain the budget -> sticky PERSISTENT_ERROR (no livelock, no broker).
    for _ in range(MAX_RUNTIME_UNKNOWN + 1):
        step(env)
    job = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    assert job["status"] == SupervisorJobStatus.ERROR.value, job["status"]
    assert job["recovery_state"] == "PERSISTENT_ERROR"
    assert job["retry_count"] >= MAX_RUNTIME_UNKNOWN
    assert job["last_error_code"] == "apply_lock_unavailable"
    assert job["next_action"] == "NONE"
    assert env.calls["n"] == 0, "broker must never be invoked without the lock"
    act = env.sup._latest_action(d.id, "APPLY_PATCH_SET")
    assert act["status"] == "RUNNING", act  # canonical intent NOT FAILED
    assert len(_apply_rows(env, d)) == 1


def test_r15_lock_contention_timeout_bounded_error(db_path, tmp_path, monkeypatch):
    """F2 (R15, MEDIUM): lock CONTENTION exceeding the bounded timeout (a second
    fd holds the flock) must land in a bounded job-level backoff -> sticky
    PERSISTENT_ERROR, with the broker invoked zero times and the canonical
    intent left RUNNING."""
    import fcntl
    import os

    env, d = _make_write_env(db_path, tmp_path)
    patch = [{"op": "write", "path": "src/module.py",
              "content": base64.b64encode(b"def x():\n    pass\n").decode()}]
    result = _write_result(Role.IMPLEMENTER, env.task.id, d.id, "patch_set", patch)
    _bind_implementer_succeeded(env, d, result)

    # Shrink the bounded lock timeout so the contention test is fast.
    monkeypatch.setattr(
        "argent_core.supervisor.APPLY_LOCK_TIMEOUT_SECONDS", 0.2)

    # Hold the flock from a second fd on the SAME canonical lockfile.
    lock_path = env.sup._apply_lock_path(d.id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        for _ in range(MAX_RUNTIME_UNKNOWN + 2):
            step(env)
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)

    job = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    assert job["status"] == SupervisorJobStatus.ERROR.value, job["status"]
    assert job["recovery_state"] == "PERSISTENT_ERROR"
    assert job["retry_count"] >= MAX_RUNTIME_UNKNOWN
    assert job["last_error_code"] == "apply_lock_unavailable"
    assert env.calls["n"] == 0, "broker must never be invoked while the lock is held"
    act = env.sup._latest_action(d.id, "APPLY_PATCH_SET")
    assert act["status"] == "RUNNING", act  # canonical intent NOT FAILED


def test_f2_malformed_result_journaled_bounded(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    # A structurally malformed envelope (missing all required fields).
    _bind_and_succeed(env, d.id, d.role, {"role": d.role.value})
    for _ in range(MAX_ACTION_RETRIES + 3):
        dec = step(env)
        if dec.action is not ReconcileAction.CONSUME_RESULT:
            break
    else:
        raise AssertionError("malformed consume looped beyond the retry budget")
    act = env.sup._consume_action(d.id)
    assert act is not None and act["status"] == "FAILED"
    assert dec.action is not ReconcileAction.CONSUME_RESULT


# --- F3: missing-run backoff growth (1/2/4/8/16/30) -------------------------

def _wake_delta(env):
    state = env.sup.store.get_job(env.job.supervisor_job_id)
    if state.next_wake_at is None:
        return None
    return _parse_iso(state.next_wake_at) - env.clock().timestamp()


def _insert_spawn_journal(env, dispatch_id):
    env.sup.store._store._insert_supervisor_action({
        "id": f"spawn-{dispatch_id}",
        "supervisor_job_id": env.job.supervisor_job_id,
        "dispatch_id": dispatch_id, "action_type": "SPAWN_RUN",
        "action_key": f"supervisor:dispatch:{dispatch_id}:spawn",
        "args_hash": "h", "input_hash": None, "precondition_hash": None,
        "effect_hash": None, "status": "SUCCEEDED", "attempt_count": 1,
        "next_attempt_at": None, "started_at": "t", "finished_at": "t",
        "last_error_code": None, "created_at": "t", "updated_at": "t",
    })


def test_f3_backoff_grows_unbound(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    _insert_spawn_journal(env, d.id)
    deltas = []
    for _ in range(6):
        env.prov.set_current(d.id, make_run_observation(
            dispatch_id=d.id, role=d.role, status=RunStatus.NOT_FOUND,
            authoritative_not_found=True,
        ))
        dec = step(env)
        assert dec.action is ReconcileAction.WAIT
        deltas.append(_wake_delta(env))
    assert deltas == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0], deltas


def test_f3_backoff_grows_bound(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    provider, model, thinking, session = canonical_binding(d)
    run_id = f"run-{d.id[:8]}"
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    advance(env, ReconcileAction.BIND_RUN)
    deltas = []
    for _ in range(6):
        env.prov.set_current(d.id, make_run_observation(
            dispatch_id=d.id, role=d.role, status=RunStatus.NOT_FOUND,
            authoritative_not_found=True,
        ))
        dec = step(env)
        assert dec.action is ReconcileAction.WAIT
        deltas.append(_wake_delta(env))
    assert deltas == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0], deltas


# --- F5: owner prompt memory is gate-scoped ---------------------------------

def test_f5_new_gate_presented_again_after_closure(db_path):
    env = make_env(db_path)
    core = env.core
    core.start_role(env.task.id, Role.LEAD, LEAD)
    ar1 = core.request_action(env.task.id, "deploy_production", "prod",
                              Role.LEAD, LEAD)
    ap1 = ar1.approval
    d = step(env)
    assert d.action is ReconcileAction.PRESENT_OWNER_GATE
    core.approve(ap1.id, OWNER, task_id=env.task.id, action="deploy_production",
                 scope="prod")
    core.execute_approved(ap1.id, OWNER, task_id=env.task.id,
                          action="deploy_production", scope="prod")
    # A new gate for a different scope must be presented again.
    core.request_action(env.task.id, "deploy_production", "staging",
                        Role.LEAD, LEAD)
    d2 = step(env)
    assert d2.action is ReconcileAction.PRESENT_OWNER_GATE, d2.action


# --- F6: complete snapshot/CAS (no stale gate decision) ---------------------

def test_f6_gate_transition_between_snapshot_and_commit_no_stale_presentation(
        db_path):
    env = make_env(db_path)
    core = env.core
    core.start_role(env.task.id, Role.LEAD, LEAD)
    ar = core.request_action(env.task.id, "deploy_production", "prod",
                             Role.LEAD, LEAD)

    class GateApprovingSupervisor(Supervisor):
        def _observe(self, snap):
            g = snap.gate
            if g is not None and g.status is ApprovalStatus.PENDING:
                self.core.approve(g.id, OWNER, task_id=g.task_id,
                                  action=g.action, scope=g.scope)
            return super()._observe(snap)

    sup2 = GateApprovingSupervisor(core, env.prov, env.launch, clock=env.clock)
    d = sup2.reconcile(env.job.supervisor_job_id)
    # The snapshot saw the gate pending; the owner approved it before commit.
    # The CAS must retry and observe the approved gate -> WAIT, never PRESENT.
    assert d.action is not ReconcileAction.PRESENT_OWNER_GATE, d.action
    assert d.action is ReconcileAction.WAIT


# --- F7: binding hash verified on approve/reject/execute ---------------------

def _corrupt_binding_hash(core, approval_id, bad="deadbeef" * 8):
    core._store._conn.execute(
        "UPDATE owner_approvals SET binding_hash = ? WHERE id = ?",
        (bad, approval_id),
    )


def test_f7_approve_rejects_binding_hash_mismatch(db_path):
    env = make_env(db_path)
    core = env.core
    core.start_role(env.task.id, Role.LEAD, LEAD)
    ar = core.request_action(env.task.id, "deploy_production", "prod",
                             Role.LEAD, LEAD)
    _corrupt_binding_hash(core, ar.approval.id)
    with pytest.raises(Exception):
        core.approve(ar.approval.id, OWNER, task_id=env.task.id,
                     action="deploy_production", scope="prod")


def test_f7_execute_rejects_binding_hash_mismatch(db_path):
    env = make_env(db_path)
    core = env.core
    core.start_role(env.task.id, Role.LEAD, LEAD)
    ar = core.request_action(env.task.id, "deploy_production", "prod",
                             Role.LEAD, LEAD)
    core.approve(ar.approval.id, OWNER, task_id=env.task.id,
                 action="deploy_production", scope="prod")
    _corrupt_binding_hash(core, ar.approval.id)
    with pytest.raises(Exception):
        core.execute_approved(ar.approval.id, OWNER, task_id=env.task.id,
                              action="deploy_production", scope="prod")


def test_f7_backfilled_binding_hash_verifies(db_path):
    env = make_env(db_path)
    core = env.core
    core.start_role(env.task.id, Role.LEAD, LEAD)
    ar = core.request_action(env.task.id, "deploy_production", "prod",
                             Role.LEAD, LEAD)
    ap = ar.approval
    assert ap.binding_hash == binding_hash(env.task.id, "deploy_production", "prod")
    # A stored hash equal to the canonical recomputation verifies and approves.
    core.approve(ap.id, OWNER, task_id=env.task.id, action="deploy_production",
                 scope="prod")
    assert core.queries.get_approval(ap.id).status is ApprovalStatus.APPROVED


# --- F8: crash matrix (DB reopen simulates a hard restart) ------------------

def test_f8_crash_during_analyst_run(db_path):
    env = make_env(db_path)
    drive_frontier(env, Role.LEAD)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    assert d.role is Role.ANALYST
    provider, model, thinking, session = canonical_binding(d)
    run_id = f"run-{d.id[:8]}"
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    advance(env, ReconcileAction.BIND_RUN)
    env.core.close()

    core2 = Core(db_path)
    prov2 = FakeRunStatusProvider()
    launch2 = FakeRunLauncher()
    sup2 = Supervisor(core2, prov2, launch2)
    prov2.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    d2 = sup2.reconcile(env.job.supervisor_job_id)
    assert d2.action is ReconcileAction.WAIT
    assert len(launch2.spawns) == 0
    analyst = [x for x in core2.queries.list_dispatches(env.task.id)
               if x.role is Role.ANALYST]
    assert len(analyst) == 1


def test_f8_crash_during_implementer_run(db_path):
    env = make_env(db_path)
    drive_frontier(env, Role.LEAD)
    drive_frontier(env, Role.ANALYST)
    drive_frontier(env, Role.LEAD)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    assert d.role is Role.IMPLEMENTER
    provider, model, thinking, session = canonical_binding(d)
    run_id = f"run-{d.id[:8]}"
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    advance(env, ReconcileAction.BIND_RUN)
    env.core.close()

    core2 = Core(db_path)
    prov2 = FakeRunStatusProvider()
    sup2 = Supervisor(core2, prov2, FakeRunLauncher())
    prov2.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    d2 = sup2.reconcile(env.job.supervisor_job_id)
    assert d2.action is ReconcileAction.WAIT
    impl = [x for x in core2.queries.list_dispatches(env.task.id)
            if x.role is Role.IMPLEMENTER]
    assert len(impl) == 1


def test_f8_crash_after_qa_completion_before_consume(db_path, tmp_path):
    env = make_env(db_path, workspace=make_workspace(tmp_path),
                   run_tests_fn=fake_run_tests)
    drive_frontier(env, Role.LEAD)
    drive_frontier(env, Role.ANALYST)
    drive_frontier(env, Role.LEAD)
    drive_frontier(env, Role.IMPLEMENTER, lambda did: _write_result(
        Role.IMPLEMENTER, env.task.id, did, "patch_set",
        [{"op": "write", "path": "src/module.py",
          "content": base64.b64encode(b"def x():\n    pass\n").decode()}],
    ))
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    assert d.role is Role.QA
    result = _write_result(Role.QA, env.task.id, d.id, "test_patch_set", [])
    _bind_and_succeed(env, d.id, d.role, result)
    # Simulate QA completed (provider SUCCEEDED) but consume not yet run, then
    # crash before the write preconditions/consume complete.
    env.core.close()

    core2 = Core(db_path)
    prov2 = FakeRunStatusProvider()
    sup2 = Supervisor(core2, prov2, FakeRunLauncher(),
                      workspace_root=make_workspace(tmp_path),
                      run_tests_fn=fake_run_tests)
    # Re-script the QA run as SUCCEEDED; recovery must consume exactly once.
    d2 = core2.queries.get_dispatch(d.id)
    provider, model, thinking, session = canonical_binding(d2)
    run_id = f"run-{d2.id[:8]}"
    prov2.set_current(d2.id, make_run_observation(
        dispatch_id=d2.id, role=d2.role, status=RunStatus.SUCCEEDED,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking, result=result,
    ))
    # QA is a write role: apply (noop for empty patch), tests, record, consume.
    adv = sup2.reconcile(env.job.supervisor_job_id)
    sup2.perform_next_safe_action_if_required(adv)
    # Drive to consume.
    for _ in range(40):
        dec = sup2.reconcile(env.job.supervisor_job_id)
        sup2.perform_next_safe_action_if_required(dec)
        if core2.queries.get_dispatch(d2.id).status is DispatchStatus.CONSUMED:
            break
    assert core2.queries.get_dispatch(d2.id).status is DispatchStatus.CONSUMED


def test_f8_crash_during_rework_with_open_finding(db_path, tmp_path):
    env = make_env(db_path, workspace=make_workspace(tmp_path),
                   run_tests_fn=fake_run_tests)
    # Complete lead/analyst/lead so the frontier reaches implementer, then add
    # an open finding (reviewer artefact) and a rework lead decision.
    drive_frontier(env, Role.LEAD)
    drive_frontier(env, Role.ANALYST)
    drive_frontier(env, Role.LEAD)
    # Simulate a reviewer-created open finding (direct store insert avoids the
    # reviewer role-run requirement; the supervisor must reconstruct it).
    from argent_core.models import Finding, FindingStatus
    env.core._store._insert_finding(Finding(
        id="f-rework", task_id=env.task.id, severity="high",
        description="rework finding", status=FindingStatus.OPEN,
        created_at="t", resolved_at=None,
    ))
    env.core.close()

    core2 = Core(db_path)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(),
                      workspace_root=make_workspace(tmp_path),
                      run_tests_fn=fake_run_tests)
    job2 = sup2.store.get_job(env.job.supervisor_job_id)
    assert job2 is not None
    assert job2.open_findings_count == 1
    assert job2.expected_role == "implementer"


def test_f8_crash_final_lead_before_done(db_path, tmp_path):
    env = make_env(db_path, workspace=make_workspace(tmp_path),
                   run_tests_fn=fake_run_tests)
    t = env.task
    # Consume lead(0), analyst(1), lead(2), implementer(3), qa(4), reviewer(5),
    # leaving the FINAL lead (position 6) dispatched but UNCONSUMED.
    drive_frontier(env, Role.LEAD)
    drive_frontier(env, Role.ANALYST)
    drive_frontier(env, Role.LEAD)
    drive_frontier(env, Role.IMPLEMENTER, lambda did: _write_result(
        Role.IMPLEMENTER, t.id, did, "patch_set",
        [{"op": "write", "path": "src/module.py",
          "content": base64.b64encode(b"def parse_duration(s):\n    return None\n").decode()}],
    ))
    drive_frontier(env, Role.QA, lambda did: _write_result(
        Role.QA, t.id, did, "test_patch_set",
        [{"op": "write", "path": "tests/test_parser.py",
          "content": base64.b64encode(b"def test_x():\n    assert True\n").decode()}],
    ))
    drive_frontier(env, Role.REVIEWER)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    assert d.role is Role.LEAD and d.position == 6
    result = build_output(Role.LEAD, env.task.id, d.id)
    _bind_and_succeed(env, d.id, d.role, result)
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING
    env.core.close()

    core2 = Core(db_path)
    prov2 = FakeRunStatusProvider()
    sup2 = Supervisor(core2, prov2, FakeRunLauncher())
    d2 = core2.queries.get_dispatch(d.id)
    provider, model, thinking, session = canonical_binding(d2)
    run_id = f"run-{d2.id[:8]}"
    prov2.set_current(d2.id, make_run_observation(
        dispatch_id=d2.id, role=d2.role, status=RunStatus.SUCCEEDED,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking, result=result,
    ))
    # Recover consumes the final lead exactly once -> DONE, never a new lead.
    for _ in range(10):
        dec = sup2.reconcile(env.job.supervisor_job_id)
        sup2.perform_next_safe_action_if_required(dec)
        if core2.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED:
            break
    assert core2.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED
    assert core2.queries.get_task(env.task.id).state is TaskState.DONE


def test_f8_pending_gate_restart(db_path):
    env = make_env(db_path)
    core = env.core
    core.start_role(env.task.id, Role.LEAD, LEAD)
    core.request_action(env.task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    d = step(env)
    assert d.action is ReconcileAction.PRESENT_OWNER_GATE
    env.core.close()
    core2 = Core(db_path)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher())
    # The same pending gate must not be presented a second time after restart.
    d2 = sup2.reconcile(env.job.supervisor_job_id)
    assert d2.action is ReconcileAction.WAIT
    assert d2.reason == "waiting_gate"


def test_f8_crash_after_broker_mutation_before_journal_success(db_path, tmp_path):
    env, d = _make_write_env(db_path, tmp_path)
    patch = [{"op": "write", "path": "src/module.py",
              "content": base64.b64encode(b"def parse_duration(s):\n    return None\n").decode()}]
    result = _write_result(Role.IMPLEMENTER, env.task.id, d.id, "patch_set", patch)
    _bind_implementer_succeeded(env, d, result)

    wsp = WorkspaceHashProvider()
    precondition = wsp.scoped_hash(env.ws)
    effect = wsp.predicted_hash(env.ws, patch)
    result_hash = _sha256(_canonical_json(result))
    key = f"supervisor:dispatch:{d.id}:apply:{result_hash}"
    args_hash = _sha256(_canonical_json({
        "dispatch_id": d.id, "patch_set": patch,
        "workspace_root": str(env.ws.resolve())}))

    # Broker mutation happens, then crash BEFORE journal SUCCEEDED (RUNNING row
    # with persisted precondition/effect, no SUCCEEDED).
    WorkspaceBroker().apply_patch_set(env.ws, patch, Role.IMPLEMENTER, LEAD)
    env.sup.store._store._insert_supervisor_action({
        "id": "apply-crash-db", "supervisor_job_id": env.job.supervisor_job_id,
        "dispatch_id": d.id, "action_type": "APPLY_PATCH_SET",
        "action_key": key, "args_hash": args_hash,
        "input_hash": result_hash, "precondition_hash": precondition,
        "effect_hash": effect, "status": "RUNNING", "attempt_count": 1,
        "next_attempt_at": None, "started_at": "t", "finished_at": None,
        "last_error_code": None, "created_at": "t", "updated_at": "t",
    })
    env.core.close()

    # DB reopen: recovery reconciles from the broker state (no re-apply).
    calls2 = {"n": 0}

    class CountingBroker2(WorkspaceBroker):
        def apply_patch_set(self, *a, **k):
            calls2["n"] += 1
            return super().apply_patch_set(*a, **k)

    core2 = Core(db_path)
    prov2 = FakeRunStatusProvider()
    sup2 = Supervisor(core2, prov2, FakeRunLauncher(), workspace_root=env.ws,
                      run_tests_fn=fake_run_tests,
                      broker_factory=lambda: CountingBroker2())
    d2 = core2.queries.get_dispatch(d.id)
    provider, model, thinking, session = canonical_binding(d2)
    run_id = f"run-{d2.id[:8]}"
    prov2.set_current(d2.id, make_run_observation(
        dispatch_id=d2.id, role=d2.role, status=RunStatus.SUCCEEDED,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking, result=result,
    ))
    # Drive until the write precondition apply has been reconciled.
    for _ in range(20):
        dec = sup2.reconcile(env.job.supervisor_job_id)
        sup2.perform_next_safe_action_if_required(dec)
        act = sup2._latest_action(d2.id, "APPLY_PATCH_SET")
        if act is not None and act["status"] == "SUCCEEDED":
            break
    act = sup2._latest_action(d2.id, "APPLY_PATCH_SET")
    assert act is not None and act["status"] == "SUCCEEDED"
    assert calls2["n"] == 0, "no re-apply after DB reopen reconciliation"
    assert (env.ws / "src" / "module.py").read_text() == \
        "def parse_duration(s):\n    return None\n"


# ---------------------------------------------------------------------------
# Phase-2C Fix Round regression tests (F1/F2/F3/F8/F6-new)
# ---------------------------------------------------------------------------

def _close_job_rows(core):
    return [a for a in core._store.list_supervisor_actions()
            if a["action_type"] == "CLOSE_JOB"]


# --- F1 (round 2): forbidden field / noncanonical hash fail-closed ----------

def test_f1_forbidden_field_not_brokered(db_path, tmp_path):
    env, d = _make_write_env(db_path, tmp_path)
    # A forbidden top-level field ('encoded') must be rejected fail-closed:
    # validate_role_output rejects it, the broker is never invoked and the
    # APPLY_PATCH_SET action journals FAILED.
    result = dict(build_output(Role.IMPLEMENTER, env.task.id, d.id))
    result["patch_set"] = [{"op": "write", "path": "src/module.py",
                            "content": base64.b64encode(b"X").decode()}]
    result["encoded"] = "smuggled"  # forbidden top-level field
    _bind_implementer_succeeded(env, d, result)
    for _ in range(MAX_ACTION_RETRIES + 3):
        dec = step(env)
        if dec.action in (ReconcileAction.PERSISTENT_ERROR,
                          ReconcileAction.MARK_RUN_FAILED):
            break
    else:
        raise AssertionError("forbidden field never reached a terminal/error action")
    assert env.calls["n"] == 0, "broker must never be called for a forbidden field"
    act = env.sup._latest_action(d.id, "APPLY_PATCH_SET")
    assert act is not None and act["status"] == "FAILED"
    assert (env.ws / "src" / "module.py").read_text() == "# stub\n"


def test_f1_noncanonical_hash_rejected(db_path, tmp_path):
    env, d = _make_write_env(db_path, tmp_path)
    patch = [{"op": "write", "path": "src/module.py",
              "content": base64.b64encode(b"def x():\n    pass\n").decode()}]
    result = _write_result(Role.IMPLEMENTER, env.task.id, d.id, "patch_set", patch)
    provider, model, thinking, session = canonical_binding(d)
    run_id = f"run-{d.id[:8]}"
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    advance(env, ReconcileAction.BIND_RUN)
    # SUCCEEDED with a WRONG (noncanonical) adapter-supplied result hash.
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.SUCCEEDED,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking, result=result, result_hash="0" * 64,
    ))
    for _ in range(MAX_ACTION_RETRIES + 3):
        dec = step(env)
        if dec.action in (ReconcileAction.PERSISTENT_ERROR,
                          ReconcileAction.MARK_RUN_FAILED):
            break
    else:
        raise AssertionError("noncanonical hash never reached a terminal/error action")
    assert env.calls["n"] == 0, "broker must never be called for a noncanonical hash"
    act = env.sup._latest_action(d.id, "APPLY_PATCH_SET")
    assert act is not None and act["status"] == "FAILED"
    assert act["last_error_code"] == "result_hash_mismatch"
    # The journal input_hash must carry the RECOMPUTED canonical hash, not the
    # adapter-supplied noncanonical one.
    assert act["input_hash"] == _sha256(_canonical_json(result))
    assert (env.ws / "src" / "module.py").read_text() == "# stub\n"


# --- F2 (round 2): exhausted START_ROLE persists ERROR (no livelock) --------

def test_f2_exhausted_start_role_persists_error(db_path):
    env = make_env(db_path)
    # A foreign active role run makes start_role(lead) fail with RoleConflict
    # every time, exhausting the START_ROLE journal retry budget.
    env.core._store._insert_role_run(RoleRun(
        id="rr-foreign", task_id=env.task.id, role=Role.ANALYST,
        status=RoleRunStatus.STARTED, started_at="t",
    ))
    for _ in range(MAX_ACTION_RETRIES + 3):
        dec = step(env)
        if dec.action is ReconcileAction.NONE:
            break
    else:
        raise AssertionError("exhausted START_ROLE never reached NONE/ERROR")
    state = env.sup.store.get_job(env.job.supervisor_job_id)
    assert state.status == SupervisorJobStatus.ERROR.value
    assert state.last_error_code is not None
    assert state.terminal is None
    # No infinite ACTIVE re-planning: the next reconcile is NONE.
    d2 = env.sup.reconcile(env.job.supervisor_job_id)
    assert d2.action is ReconcileAction.NONE


def test_f2_terminal_close_journaled_exactly_once(db_path, tmp_path):
    env = make_env(db_path, workspace=make_workspace(tmp_path),
                   run_tests_fn=fake_run_tests)
    drive_to_done(env)
    assert env.core.queries.get_task(env.task.id).state is TaskState.DONE
    # Crash AFTER task DONE but BEFORE CLOSE_JOB is journaled (zero CLOSE_JOB
    # rows, job still ACTIVE).
    assert _close_job_rows(env.core) == []
    assert env.sup.store.get_job(env.job.supervisor_job_id).terminal is None
    env.core.close()

    # Restart over the same DB: reconcile -> CLOSE_DONE -> CLOSE_JOB journaled
    # exactly once and terminal DONE (never a terminal job with zero CLOSE_JOB).
    core2 = Core(db_path)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(),
                      workspace_root=make_workspace(tmp_path),
                      run_tests_fn=fake_run_tests)
    loop = SupervisorLoop(sup2)
    result = loop.run_until_terminal(env.job.supervisor_job_id)
    assert result is not None and result.terminal == "DONE"
    closes = _close_job_rows(core2)
    assert len(closes) == 1
    assert closes[0]["status"] == "SUCCEEDED"
    # A further reconcile is NONE (terminal), never a second close.
    d = sup2.reconcile(env.job.supervisor_job_id)
    assert d.action is ReconcileAction.NONE


# --- F3 (round 2): non-authoritative unbound NOT_FOUND backoff --------------

def test_f3_wait_missing_backoff_sequence(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    # Non-authoritative unbound NOT_FOUND -> _wait_missing (no spawn journal).
    # The wake must use the PRE-increment retry count: 1,2,4,8,16,30.
    seq = []
    for _ in range(6):
        env.prov.set_current(d.id, make_run_observation(
            dispatch_id=d.id, role=d.role, status=RunStatus.NOT_FOUND,
            authoritative_not_found=False,
        ))
        dec = step(env)
        assert dec.action is ReconcileAction.WAIT
        state = env.sup.store.get_job(env.job.supervisor_job_id)
        wake = _parse_iso(state.next_wake_at) - env.clock().timestamp()
        seq.append((round(wake), state.retry_count))
    assert seq == [(1, 1), (2, 2), (4, 3), (8, 4), (16, 5), (30, 6)], seq


# --- F8 (round 2): persistent launcher invocation counter -------------------

def test_f8_launcher_counter_increments_per_spawn(db_path, tmp_path, monkeypatch):
    import subprocess as sp
    counter = tmp_path / "launch-counter.json"
    launcher = OpenClawRunLauncher(counter_path=counter)
    popen_calls = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append(cmd)
        return None

    monkeypatch.setattr(sp, "Popen", fake_popen)
    msg = tmp_path / "msg.md"
    msg.write_text("hi")
    launcher.spawn(agent_id="argent-analyst", dispatch_id="d1",
                   message_file=msg, timeout_seconds=1)
    launcher.spawn(agent_id="argent-analyst", dispatch_id="d1",
                   message_file=msg, timeout_seconds=1)
    launcher.spawn(agent_id="argent-analyst", dispatch_id="d2",
                   message_file=msg, timeout_seconds=1)
    assert len(popen_calls) == 3
    # Simulated kill: read the counter back from the persisted file.
    assert read_launch_counter(counter) == {"d1": 2, "d2": 1}


# --- F6-new: missing-job reconcile never leaks an open transaction ----------

def test_f6_new_missing_job_reconcile_leaves_no_transaction(db_path):
    env = make_env(db_path)
    conn = env.core._store._conn
    with pytest.raises(NotFound):
        env.sup.reconcile("supervisor:no-such-job")
    assert conn.in_transaction is False
    # Subsequent Core operations work (no "cannot start a transaction within a
    # transaction").
    assert env.core.queries.get_task(env.task.id) is not None
    d = env.sup.reconcile(env.job.supervisor_job_id)
    assert d.action in (ReconcileAction.START_ROLE, ReconcileAction.NONE)


# ---------------------------------------------------------------------------
# Phase-2C Fix Round 3 regression tests (F1/F2/F3/F4)
# ---------------------------------------------------------------------------

# --- F1 (round 3): bound dispatch trajectory binding fail-closed ------------

def test_f1_foreign_tuple_bound_dispatch_no_consume(db_path, tmp_path):
    """A bound dispatch whose trajectory consistently carries a foreign
    provider/model tuple must be observed as CONFLICT by the real trajectory
    provider, so the supervisor never reaches CONSUME_RESULT (dispatch stays
    RUNNING, never CONSUMED)."""
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    # Persist the canonical binding directly (simulates the already-bound
    # dispatch from a prior run).
    provider, model, thinking, session = canonical_binding(d)
    run_id = f"run-{d.id[:8]}"
    env.core.bind_spawn_result(d.id, session, run_id, provider, model,
                               thinking, LEAD)
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING
    env.core.close()

    # Reopen with a REAL trajectory provider whose trajectory carries a
    # CONSISTENT foreign tuple (the opposite of the canonical binding).
    state_dir = tmp_path / "state"
    agent_id = AGENT_IDS[d.role]
    sess_dir = state_dir / "agents" / agent_id / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    label = f"dispatch-{d.id}"
    sess_key = session_key_for(agent_id, d.id)
    foreign = ("deepseek", "deepseek-v4-pro") if provider == "openai" \
        else ("openai", "gpt-5.6-sol")
    traj = sess_dir / f"{label}.trajectory.jsonl"
    traj.write_text(json.dumps({
        "type": "session.started", "ts": "2026-01-01T00:00:00Z",
        "sessionId": label, "sessionKey": sess_key, "runId": run_id,
        "provider": foreign[0], "modelId": foreign[1],
        "data": {"agentId": agent_id,
                 "sessionFile": str(sess_dir / f"{label}.jsonl")},
    }) + "\n" + json.dumps({
        "type": "session.ended", "ts": "2026-01-01T00:01:00Z",
        "sessionId": label, "sessionKey": sess_key, "runId": run_id,
        "provider": foreign[0], "modelId": foreign[1],
        "data": {"status": "success", "agentId": agent_id},
    }) + "\n", encoding="utf-8")

    core2 = Core(db_path)
    sup2 = Supervisor(core2, TrajectoryRunStatusProvider(state_dir=state_dir),
                      FakeRunLauncher())
    dec = sup2.reconcile(env.job.supervisor_job_id)
    assert dec.action is not ReconcileAction.CONSUME_RESULT, dec.action
    assert dec.action is ReconcileAction.WAIT, dec.action
    assert dec.reason == "adapter_conflict", dec.reason
    assert core2.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING


def test_f4_malformed_start_data_bound_dispatch_no_consume(db_path, tmp_path):
    """F-R4 liveness regression: a bound dispatch whose trajectory start row
    carries a non-object ``data`` must be observed as CONFLICT (never
    SUCCEEDED) so reconcile returns WAIT (adapter_conflict), the dispatch is
    never consumed, and reconcile never raises (the supervisor loop stays
    alive)."""
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    provider, model, thinking, session = canonical_binding(d)
    run_id = f"run-{d.id[:8]}"
    env.core.bind_spawn_result(d.id, session, run_id, provider, model,
                               thinking, LEAD)
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING
    env.core.close()

    state_dir = tmp_path / "state"
    agent_id = AGENT_IDS[d.role]
    sess_dir = state_dir / "agents" / agent_id / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    label = f"dispatch-{d.id}"
    sess_key = session_key_for(agent_id, d.id)
    traj = sess_dir / f"{label}.trajectory.jsonl"
    traj.write_text(json.dumps({
        "type": "session.started", "ts": "2026-01-01T00:00:00Z",
        "sessionId": label, "sessionKey": sess_key, "runId": run_id,
        "provider": provider, "modelId": model,
        # F-R4: scalar string ``data`` (malformed) instead of an object.
        "data": "agentId",
    }) + "\n" + json.dumps({
        "type": "session.ended", "ts": "2026-01-01T00:01:00Z",
        "sessionId": label, "sessionKey": sess_key, "runId": run_id,
        "provider": provider, "modelId": model,
        "data": {"status": "success", "agentId": agent_id},
    }) + "\n", encoding="utf-8")

    core2 = Core(db_path)
    sup2 = Supervisor(core2, TrajectoryRunStatusProvider(state_dir=state_dir),
                      FakeRunLauncher())
    dec = sup2.reconcile(env.job.supervisor_job_id)
    assert dec.action is not ReconcileAction.CONSUME_RESULT, dec.action
    assert dec.action is ReconcileAction.WAIT, dec.action
    assert dec.reason == "adapter_conflict", dec.reason
    assert core2.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING


def test_f5_metadata_null_bound_dispatch_no_consume(db_path, tmp_path):
    """F-R5 regression: a bound dispatch whose trajectory carries a PRESENT
    JSON-null ``trace.metadata`` row must be observed as CONFLICT (never
    SUCCEEDED) -> reconcile returns WAIT (adapter_conflict), the dispatch is
    never consumed, and reconcile never raises."""
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    provider, model, thinking, session = canonical_binding(d)
    run_id = f"run-{d.id[:8]}"
    env.core.bind_spawn_result(d.id, session, run_id, provider, model,
                               thinking, LEAD)
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING
    env.core.close()

    state_dir = tmp_path / "state"
    agent_id = AGENT_IDS[d.role]
    sess_dir = state_dir / "agents" / agent_id / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    label = f"dispatch-{d.id}"
    sess_key = session_key_for(agent_id, d.id)
    traj = sess_dir / f"{label}.trajectory.jsonl"
    traj.write_text(json.dumps({
        "type": "session.started", "ts": "2026-01-01T00:00:00Z",
        "sessionId": label, "sessionKey": sess_key, "runId": run_id,
        "provider": provider, "modelId": model,
        "data": {"agentId": agent_id,
                 "sessionFile": str(sess_dir / f"{label}.jsonl")},
    }) + "\n" + json.dumps({
        "type": "trace.metadata", "ts": "2026-01-01T00:00:00Z",
        "runId": run_id, "data": None,
    }) + "\n" + json.dumps({
        "type": "session.ended", "ts": "2026-01-01T00:01:00Z",
        "sessionId": label, "sessionKey": sess_key, "runId": run_id,
        "provider": provider, "modelId": model,
        "data": {"status": "success", "agentId": agent_id},
    }) + "\n", encoding="utf-8")

    core2 = Core(db_path)
    sup2 = Supervisor(core2, TrajectoryRunStatusProvider(state_dir=state_dir),
                      FakeRunLauncher())
    dec = sup2.reconcile(env.job.supervisor_job_id)
    assert dec.action is not ReconcileAction.CONSUME_RESULT, dec.action
    assert dec.action is ReconcileAction.WAIT, dec.action
    assert dec.reason == "adapter_conflict", dec.reason
    assert core2.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING


# --- F2 (round 3): binding re-verified on idempotent replay -----------------

def _pending_approval(env, action="deploy_production", scope="prod"):
    env.core.start_role(env.task.id, Role.LEAD, LEAD)
    ar = env.core.request_action(env.task.id, action, scope, Role.LEAD, LEAD)
    return ar.approval


def test_f2_replay_approve_after_corruption_raises(db_path):
    env = make_env(db_path)
    core = env.core
    ap = _pending_approval(env)
    key = "approve-replay-1"
    first = core.approve(ap.id, OWNER, task_id=env.task.id,
                         action="deploy_production", scope="prod",
                         idempotency_key=key)
    assert first.status is ApprovalStatus.APPROVED
    _corrupt_binding_hash(core, ap.id)
    with pytest.raises(Exception):
        core.approve(ap.id, OWNER, task_id=env.task.id,
                     action="deploy_production", scope="prod",
                     idempotency_key=key)


def test_f2_replay_reject_after_corruption_raises(db_path):
    env = make_env(db_path)
    core = env.core
    ap = _pending_approval(env)
    key = "reject-replay-1"
    first = core.reject(ap.id, OWNER, task_id=env.task.id,
                        action="deploy_production", scope="prod",
                        idempotency_key=key)
    assert first.status is ApprovalStatus.REJECTED
    _corrupt_binding_hash(core, ap.id)
    with pytest.raises(Exception):
        core.reject(ap.id, OWNER, task_id=env.task.id,
                    action="deploy_production", scope="prod",
                    idempotency_key=key)


def test_f2_replay_execute_approved_after_corruption_raises(db_path):
    env = make_env(db_path)
    core = env.core
    ap = _pending_approval(env)
    core.approve(ap.id, OWNER, task_id=env.task.id,
                 action="deploy_production", scope="prod")
    key = "execute-replay-1"
    first = core.execute_approved(ap.id, OWNER, task_id=env.task.id,
                                  action="deploy_production", scope="prod",
                                  idempotency_key=key)
    assert first.status is ApprovalStatus.CONSUMED
    _corrupt_binding_hash(core, ap.id)
    with pytest.raises(Exception):
        core.execute_approved(ap.id, OWNER, task_id=env.task.id,
                              action="deploy_production", scope="prod",
                              idempotency_key=key)


def test_f2_clean_replay_returns_original_approval(db_path):
    env = make_env(db_path)
    core = env.core
    ap = _pending_approval(env)
    key = "approve-replay-clean"
    first = core.approve(ap.id, OWNER, task_id=env.task.id,
                         action="deploy_production", scope="prod",
                         idempotency_key=key)
    replay = core.approve(ap.id, OWNER, task_id=env.task.id,
                          action="deploy_production", scope="prod",
                          idempotency_key=key)
    assert replay.id == first.id == ap.id
    assert replay.status is ApprovalStatus.APPROVED


# --- F3 (round 3): exhausted SPAWN_RUN persists sticky ERROR ----------------

def test_f3_exhausted_spawn_run_persists_error(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    # Persist a FAILED SPAWN_RUN row with attempt_count == MAX (exhausted).
    env.sup.store._store._insert_supervisor_action({
        "id": "spawn-exhausted",
        "supervisor_job_id": env.job.supervisor_job_id,
        "dispatch_id": d.id, "action_type": "SPAWN_RUN",
        "action_key": f"supervisor:dispatch:{d.id}:spawn",
        "args_hash": _sha256(_canonical_json({"dispatch_id": d.id})),
        "input_hash": None, "precondition_hash": None, "effect_hash": None,
        "status": "FAILED", "attempt_count": MAX_ACTION_RETRIES,
        "next_attempt_at": None, "started_at": "t", "finished_at": "t",
        "last_error_code": "x", "created_at": "t", "updated_at": "t",
    })
    env.core.close()

    core2 = Core(db_path)
    prov2 = FakeRunStatusProvider()
    launch2 = FakeRunLauncher()
    sup2 = Supervisor(core2, prov2, launch2)
    prov2.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.NOT_FOUND,
        authoritative_not_found=False,
    ))
    dec = sup2.reconcile(env.job.supervisor_job_id)
    assert dec.action is ReconcileAction.PERSISTENT_ERROR, dec.action
    assert dec.reason == "spawn_run_exhausted", dec.reason
    state = sup2.store.get_job(env.job.supervisor_job_id)
    assert state.status == SupervisorJobStatus.ERROR.value
    assert state.last_error_code == "spawn_run_exhausted"
    assert len(launch2.spawns) == 0, "launcher must never be invoked"


# --- F4 (round 3): exact crash-matrix boundaries ----------------------------

def test_f4_crash_after_start_role_before_dispatch(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    # Pre-step state persisted: START_ROLE journaled SUCCEEDED, role run
    # STARTED, no dispatch (crash after START_ROLE, before CREATE_DISPATCH).
    assert env.core.queries.list_dispatches(env.task.id) == []
    starts = [a for a in env.core._store.list_supervisor_actions()
              if a["action_type"] == "START_ROLE"]
    assert len(starts) == 1 and starts[0]["status"] == "SUCCEEDED"
    env.core.close()

    core2 = Core(db_path)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher())
    dec = sup2.reconcile(env.job.supervisor_job_id)
    assert dec.action is ReconcileAction.CREATE_DISPATCH, dec.action
    sup2.perform_next_safe_action_if_required(dec)
    assert len(core2.queries.list_dispatches(env.task.id)) == 1
    assert len(core2.queries.list_role_runs(env.task.id)) == 1
    starts2 = [a for a in core2._store.list_supervisor_actions()
               if a["action_type"] == "START_ROLE"]
    assert len(starts2) == 1


def test_f4_crash_after_spawn_journal_before_launcher(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    # Persist a RUNNING SPAWN_RUN journal row (spawn intent journaled) but the
    # launcher was never invoked (crash between journal and launcher).
    env.sup.store._store._insert_supervisor_action({
        "id": "spawn-running",
        "supervisor_job_id": env.job.supervisor_job_id,
        "dispatch_id": d.id, "action_type": "SPAWN_RUN",
        "action_key": f"supervisor:dispatch:{d.id}:spawn",
        "args_hash": "h", "input_hash": None, "precondition_hash": None,
        "effect_hash": None, "status": "RUNNING", "attempt_count": 1,
        "next_attempt_at": None, "started_at": "t", "finished_at": None,
        "last_error_code": None, "created_at": "t", "updated_at": "t",
    })
    env.core.close()

    core2 = Core(db_path)
    prov2 = FakeRunStatusProvider()
    launch2 = FakeRunLauncher()
    sup2 = Supervisor(core2, prov2, launch2)
    prov2.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.NOT_FOUND,
        authoritative_not_found=True,
    ))
    dec = sup2.reconcile(env.job.supervisor_job_id)
    assert dec.action is ReconcileAction.WAIT, dec.action
    assert len(launch2.spawns) == 0, "no launcher invocation after crash"


def test_f4_crash_after_spawn_before_bind(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    # Realized spawn: SPAWN_RUN journal SUCCEEDED, launcher invoked, run
    # observed RUNNING, but crash BEFORE BIND_RUN (dispatch still PENDING).
    _insert_spawn_journal(env, d.id)
    env.launch.spawns.append({"agent_id": AGENT_IDS[d.role], "dispatch_id": d.id,
                              "message_file": "x", "timeout_seconds": 1})
    env.core.close()

    core2 = Core(db_path)
    prov2 = FakeRunStatusProvider()
    launch2 = FakeRunLauncher()
    sup2 = Supervisor(core2, prov2, launch2)
    provider, model, thinking, session = canonical_binding(d)
    prov2.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id="run-1", session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    dec = sup2.reconcile(env.job.supervisor_job_id)
    assert dec.action is ReconcileAction.BIND_RUN, dec.action
    assert len(launch2.spawns) == 0, "no re-spawn after realized spawn"


def test_f4_crash_after_core_consume_before_journal(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    result = build_output(d.role, env.task.id, d.id)
    session, run = _bind_and_succeed(env, d.id, d.role, result)
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING

    # Crash between the Core consume CAS and the supervisor journal success:
    # the Core CAS consumes the dispatch, but the CONSUME_RESULT journal row is
    # left RUNNING (never SUCCEEDED).
    envelope = _write_envelope(d.role, result)
    canonical = _canonical_json(envelope)
    key = f"supervisor:consume:{d.id}:{run}:{_sha256(canonical)}"
    env.sup.store._store._insert_supervisor_action({
        "id": "consume-crash",
        "supervisor_job_id": env.job.supervisor_job_id,
        "dispatch_id": d.id, "action_type": "CONSUME_RESULT",
        "action_key": key, "args_hash": _sha256(canonical),
        "input_hash": None, "precondition_hash": None, "effect_hash": None,
        "status": "RUNNING", "attempt_count": 1,
        "next_attempt_at": None, "started_at": "t", "finished_at": None,
        "last_error_code": None, "created_at": "t", "updated_at": "t",
    })
    dd = env.core.queries.get_dispatch(d.id)
    event_meta = {
        "task_id": env.task.id,
        "child_session_id": dd.child_session_id,
        "run_id": dd.openclaw_run_id,
        "parent_dispatch_id": dd.parent_dispatch_id,
        "event_type": "agent.completed",
        "status": "completed",
    }
    res = env.core.receive_agent_result(d.id, event_meta, envelope, LEAD)
    assert res.status == "consumed", res
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED
    env.core.close()

    core2 = Core(db_path)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher())
    dec = sup2.reconcile(env.job.supervisor_job_id)
    assert dec.action is not ReconcileAction.CONSUME_RESULT, dec.action
    assert dec.action is ReconcileAction.START_ROLE, dec.action
    assert core2.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED
    assert len(core2.queries.list_handoffs(env.task.id)) == 1
    assert len(core2.queries.list_decisions(env.task.id)) == 1


def test_f4_crash_after_completion_hint_before_reconcile(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    result = build_output(d.role, env.task.id, d.id)
    session, run = _bind_and_succeed(env, d.id, d.role, result)
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING
    dd = env.core.queries.get_dispatch(d.id)
    event_meta = {
        "task_id": env.task.id,
        "child_session_id": dd.child_session_id,
        "run_id": dd.openclaw_run_id,
        "parent_dispatch_id": dd.parent_dispatch_id,
        "event_type": "agent.completed",
        "status": "completed",
    }
    # The completion hint consumes the result via Core BEFORE any reconcile.
    res = env.sup.receive_completion_hint(d.id, event_meta, result)
    assert res.status == "consumed", res
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED
    env.core.close()

    core2 = Core(db_path)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher())
    dec = sup2.reconcile(env.job.supervisor_job_id)
    assert dec.action is not ReconcileAction.CONSUME_RESULT, dec.action
    assert dec.action is ReconcileAction.START_ROLE, dec.action
    assert core2.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED
    assert len(core2.queries.list_handoffs(env.task.id)) == 1
    assert len(core2.queries.list_decisions(env.task.id)) == 1


# ---------------------------------------------------------------------------
# Phase-2C Fix Round 6 regression tests (consume selective schema-strip bypass)
# ---------------------------------------------------------------------------

_ROLE_ORDER = (Role.LEAD, Role.ANALYST, Role.LEAD, Role.IMPLEMENTER,
               Role.QA, Role.REVIEWER, Role.LEAD)
_NON_WRITE_ROLES = (Role.LEAD, Role.ANALYST, Role.REVIEWER)
_FORBIDDEN_FIELDS = ("patch_set", "test_patch_set", "encoded")


def _drive_to_role_dispatch(env, role):
    """Consume every prior frontier role, then reach CREATE_DISPATCH for `role`."""
    for r in _ROLE_ORDER:
        if r is role:
            break
        drive_frontier(env, r)
    if role is Role.REVIEWER:
        _bind_writer(env)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    assert d.role is role
    return d


def _reset_succeeded_result(env, d, result):
    """Re-point the (already bound) sticky run observation at a new result."""
    provider, model, thinking, session = canonical_binding(d)
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.SUCCEEDED,
        run_id=f"run-{d.id[:8]}", session_id=session, provider=provider,
        model=model, thinking_tier=thinking, result=result,
    ))


def _valid_patch_for(role):
    """A role-scoped, broker-legal write patch (implementer -> src, qa -> tests)."""
    if role is Role.QA:
        return [{"op": "write", "path": "tests/test_parser.py",
                 "content": base64.b64encode(b"def test_x():\n    pass\n").decode()}]
    return [{"op": "write", "path": "src/module.py",
             "content": base64.b64encode(b"def x():\n    pass\n").decode()}]


@pytest.mark.parametrize("role", _NON_WRITE_ROLES, ids=lambda r: r.value)
@pytest.mark.parametrize("forbidden", _FORBIDDEN_FIELDS)
def test_consume_non_write_forbidden_field_fail_closed(
        role, forbidden, db_path, tmp_path):
    """F6: a forbidden top-level field on a non-write result must be REJECTED
    at consume (never silently stripped): the CONSUME_RESULT journal row is
    FAILED, the dispatch stays RUNNING, and no decision/handoff/workflow-state
    mutation is produced."""
    env = make_env(db_path, workspace=make_workspace(tmp_path),
                   run_tests_fn=fake_run_tests)
    d = _drive_to_role_dispatch(env, role)
    result = dict(build_output(role, env.task.id, d.id))
    result[forbidden] = "smuggled"
    _bind_and_succeed(env, d.id, role, result)

    decisions_before = len(env.core.queries.list_decisions(env.task.id))
    handoffs_before = len(env.core.queries.list_handoffs(env.task.id))
    state_before = env.core.queries.get_task(env.task.id).state

    dec = step(env)
    assert dec.action is ReconcileAction.CONSUME_RESULT, dec.action

    act = env.sup._latest_action(d.id, "CONSUME_RESULT")
    assert act is not None, "consume must be journaled"
    assert act["status"] == "FAILED", (role, forbidden, act)
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING
    assert len(env.core.queries.list_decisions(env.task.id)) == decisions_before
    assert len(env.core.queries.list_handoffs(env.task.id)) == handoffs_before
    assert env.core.queries.get_task(env.task.id).state is state_before


def test_consume_unknown_field_on_lead_fail_closed(db_path, tmp_path):
    """Guard: a generic unknown top-level field on a LEAD result is rejected
    fail-closed at consume (the strict top-level allow-list)."""
    env = make_env(db_path, workspace=make_workspace(tmp_path),
                   run_tests_fn=fake_run_tests)
    d = _drive_to_role_dispatch(env, Role.LEAD)
    result = dict(build_output(Role.LEAD, env.task.id, d.id))
    result["unexpected_field"] = 1
    _bind_and_succeed(env, d.id, Role.LEAD, result)

    dec = step(env)
    assert dec.action is ReconcileAction.CONSUME_RESULT, dec.action
    act = env.sup._latest_action(d.id, "CONSUME_RESULT")
    assert act is not None and act["status"] == "FAILED"
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING


@pytest.mark.parametrize("role,forbidden", [
    (Role.IMPLEMENTER, "test_patch_set"),
    (Role.IMPLEMENTER, "encoded"),
    (Role.QA, "patch_set"),
    (Role.QA, "encoded"),
])
def test_consume_write_role_forbidden_field_fail_closed(
        role, forbidden, db_path, tmp_path):
    """F1/F6: a write role whose (post-apply) observation carries a forbidden
    field is caught by the frozen write-result hash binding BEFORE consume — the
    result swap changes the full-result hash -> ``write_result_hash_mismatch``
    (WAIT), the dispatch stays RUNNING, and no decision/handoff/workflow-state
    mutation is produced (never consumed for the foreign observation)."""
    env = make_env(db_path, workspace=make_workspace(tmp_path),
                   run_tests_fn=fake_run_tests)
    d = _drive_to_role_dispatch(env, role)
    patch_field = "patch_set" if role is Role.IMPLEMENTER else "test_patch_set"
    valid_result = _write_result(role, env.task.id, d.id, patch_field,
                                 _valid_patch_for(role))
    _bind_and_succeed(env, d.id, role, valid_result)
    advance(env, ReconcileAction.APPLY_PATCH_SET)
    advance(env, ReconcileAction.RUN_SANDBOX_TESTS)
    advance(env, ReconcileAction.RECORD_TEST_RESULT)

    # Swap the result to carry the forbidden field AFTER the write pre-effects
    # succeeded, so the frozen write-result hash binding must reject it
    # fail-closed (never reach CONSUME for the foreign observation).
    forbidden_result = dict(valid_result)
    forbidden_result[forbidden] = "smuggled"
    _reset_succeeded_result(env, d, forbidden_result)

    decisions_before = len(env.core.queries.list_decisions(env.task.id))
    handoffs_before = len(env.core.queries.list_handoffs(env.task.id))

    dec = step(env)
    assert dec.action is ReconcileAction.WAIT, dec.action
    assert dec.reason == "write_result_hash_mismatch", dec.reason
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING
    assert len(env.core.queries.list_decisions(env.task.id)) == decisions_before
    assert len(env.core.queries.list_handoffs(env.task.id)) == handoffs_before
    # No consume journal row was ever produced for the foreign observation.
    assert env.sup._latest_action(d.id, "CONSUME_RESULT") is None


@pytest.mark.parametrize("role,patch_field", [
    (Role.IMPLEMENTER, "patch_set"),
    (Role.QA, "test_patch_set"),
])
def test_consume_write_role_valid_patch_still_consumed(
        role, patch_field, db_path, tmp_path):
    """No regression: a write role carrying ONLY its legitimate patch extension
    is still applied and consumed exactly as before."""
    env = make_env(db_path, workspace=make_workspace(tmp_path),
                   run_tests_fn=fake_run_tests)
    d = _drive_to_role_dispatch(env, role)
    result = _write_result(role, env.task.id, d.id, patch_field,
                           _valid_patch_for(role))
    _bind_and_succeed(env, d.id, role, result)
    advance(env, ReconcileAction.APPLY_PATCH_SET)
    advance(env, ReconcileAction.RUN_SANDBOX_TESTS)
    advance(env, ReconcileAction.RECORD_TEST_RESULT)
    advance(env, ReconcileAction.CONSUME_RESULT)
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED
    act = env.sup._latest_action(d.id, "CONSUME_RESULT")
    assert act is not None and act["status"] == "SUCCEEDED"


# ---------------------------------------------------------------------------
# Fix Round: SupervisorLoop.run_once containment (malformed runtime data)
# ---------------------------------------------------------------------------
# An adapter that raises a structural exception while interpreting untrusted
# runtime data must never terminate the loop: the exception is converted into a
# fail-closed CONFLICT observation -> bounded adapter backoff (WAIT), no raise,
# job stays non-terminal, no consumption.

@pytest.mark.parametrize("exc", [
    TypeError("unhashable type: 'list'"),
    AttributeError("'NoneType' object has no attribute 'get'"),
    ValueError("invalid timestamp"),
    KeyError("runId"),
])
def test_loop_run_once_malformed_runtime_no_raise(db_path, exc):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    assert d.status is DispatchStatus.PENDING
    # The next provider observation raises (simulating a malformed trajectory
    # that the adapter could not structurally guard).
    env.prov.fail_next = exc
    loop = SupervisorLoop(env.sup)
    dec = loop.run_once(env.job.supervisor_job_id)  # must NOT raise
    assert dec.action is ReconcileAction.WAIT, dec.action
    assert dec.reason == "adapter_conflict", dec.reason
    state = env.sup.store.get_job(env.job.supervisor_job_id)
    assert state.terminal is None
    assert state.status == SupervisorJobStatus.BACKOFF.value, state.status
    assert state.recovery_state == "RUNTIME_UNKNOWN", state.recovery_state
    # No consumption happened and the dispatch is still unbound.
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.PENDING


def test_loop_malformed_runtime_bounded_sticky_error(db_path):
    """A persistently CONFLICT-observing adapter (e.g. structurally malformed
    trajectory rows) is bounded: after MAX_RUNTIME_UNKNOWN backoffs the loop
    lands in a sticky PERSISTENT_ERROR with a journaled last_error_code, never
    an infinite ACTIVE re-plan and never a raise."""
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.CONFLICT,
    ))
    loop = SupervisorLoop(env.sup)
    for _ in range(10):
        dec = loop.run_once(env.job.supervisor_job_id)
        if dec.action is ReconcileAction.PERSISTENT_ERROR:
            break
    else:
        raise AssertionError("never reached PERSISTENT_ERROR")
    state = env.sup.store.get_job(env.job.supervisor_job_id)
    assert state.status == SupervisorJobStatus.ERROR.value, state.status
    assert state.last_error_code == "adapter_conflict", state.last_error_code
    assert state.terminal is None


# ---------------------------------------------------------------------------
# Phase-2C Fix Round 8 regression tests (F1/F2/F3/F4)
# ---------------------------------------------------------------------------

# --- F1: one immutable canonical result hash across the whole write pipeline

def test_f1_write_result_hash_binding_blocks_swapped_result(db_path, tmp_path):
    """F1: apply patch set A, then swap the observation to patch set B carrying
    the SAME envelope.  The frozen write-result hash must block the rest of the
    pipeline: tests/record/consume never run, the workspace keeps A, and the
    dispatch is never CONSUMED for the later observation (B)."""
    env, d = _make_write_env(db_path, tmp_path)
    patch_a = [{"op": "write", "path": "src/module.py",
                "content": base64.b64encode(b"# A\n").decode()}]
    patch_b = [{"op": "write", "path": "src/module.py",
                "content": base64.b64encode(b"# B\n").decode()}]
    result_a = _write_result(Role.IMPLEMENTER, env.task.id, d.id,
                             "patch_set", patch_a)
    _bind_implementer_succeeded(env, d, result_a)
    advance(env, ReconcileAction.APPLY_PATCH_SET)
    assert (env.ws / "src" / "module.py").read_text() == "# A\n"
    frozen = env.sup._frozen_write_result_hash(d.id)
    assert frozen == _sha256(_canonical_json(result_a))

    result_b = _write_result(Role.IMPLEMENTER, env.task.id, d.id,
                             "patch_set", patch_b)
    # Same stripped envelope, different full result.
    assert _write_envelope(Role.IMPLEMENTER, result_a) == \
        _write_envelope(Role.IMPLEMENTER, result_b)
    _reset_succeeded_result(env, d, result_b)

    dec = step(env)
    assert dec.action is ReconcileAction.WAIT, dec.action
    assert dec.reason == "write_result_hash_mismatch", dec.reason
    # Workspace still A; no sandbox tests/record ever ran for B.
    assert (env.ws / "src" / "module.py").read_text() == "# A\n"
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING
    assert env.sup._latest_action(d.id, "RUN_SANDBOX_TESTS") is None
    assert env.sup._latest_action(d.id, "RECORD_TEST_RESULT") is None
    assert env.sup._latest_action(d.id, "CONSUME_RESULT") is None
    # Bounded backoff -> sticky PERSISTENT_ERROR.
    for _ in range(MAX_RUNTIME_UNKNOWN + 2):
        dec = step(env)
        if dec.action is ReconcileAction.PERSISTENT_ERROR:
            break
    else:
        raise AssertionError("never reached PERSISTENT_ERROR")
    assert env.sup.store.get_job(
        env.job.supervisor_job_id).last_error_code == "write_result_hash_mismatch"


# --- F2: malformed falsy patch extension must fail closed -------------------

@pytest.mark.parametrize("bad_patch_set", ["", 0, False, {}])
def test_f2_malformed_falsy_patch_extension_fail_closed(
        db_path, tmp_path, bad_patch_set):
    """F2: a PRESENT but falsy/non-list patch field must be rejected fail-closed
    (``invalid_patch_set_type``), never a silent no-op APPLY."""
    env, d = _make_write_env(db_path, tmp_path)
    result = dict(build_output(Role.IMPLEMENTER, env.task.id, d.id))
    result["patch_set"] = bad_patch_set
    _bind_implementer_succeeded(env, d, result)
    for _ in range(MAX_ACTION_RETRIES + 3):
        dec = step(env)
        if dec.action in (ReconcileAction.PERSISTENT_ERROR,
                          ReconcileAction.MARK_RUN_FAILED):
            break
    else:
        raise AssertionError("malformed patch field never reached an error action")
    act = env.sup._latest_action(d.id, "APPLY_PATCH_SET")
    assert act is not None and act["status"] == "FAILED"
    assert act["last_error_code"] == "invalid_patch_set_type"
    assert env.calls["n"] == 0, "broker must never be called for a malformed patch field"
    assert (env.ws / "src" / "module.py").read_text() == "# stub\n"


@pytest.mark.parametrize("entry,error_code", [
    ("not-a-dict", "invalid_patch_entry_type"),
    ({"op": "chmod", "path": "src/module.py"}, "invalid_patch_op"),
    ({"op": "write", "path": 7, "content": "YWJj"}, "invalid_patch_path"),
    ({"op": "write", "path": "src/module.py", "content": 123}, "invalid_patch_content"),
    ({"op": "write", "path": "src/module.py", "content": "YQ==", "extra": 1},
     "invalid_patch_key"),
])
def test_f2_malformed_patch_entry_fail_closed(db_path, tmp_path, entry, error_code):
    """F2: a malformed patch entry (non-dict, bad op/path/content, or an unknown
    key) must fail-closed BEFORE the broker is invoked."""
    env, d = _make_write_env(db_path, tmp_path)
    result = dict(build_output(Role.IMPLEMENTER, env.task.id, d.id))
    result["patch_set"] = [entry]
    _bind_implementer_succeeded(env, d, result)
    for _ in range(MAX_ACTION_RETRIES + 3):
        dec = step(env)
        if dec.action in (ReconcileAction.PERSISTENT_ERROR,
                          ReconcileAction.MARK_RUN_FAILED):
            break
    else:
        raise AssertionError("malformed patch entry never reached an error action")
    act = env.sup._latest_action(d.id, "APPLY_PATCH_SET")
    assert act is not None and act["status"] == "FAILED"
    assert act["last_error_code"] == error_code
    assert env.calls["n"] == 0, "broker must never be called for a malformed patch entry"
    assert (env.ws / "src" / "module.py").read_text() == "# stub\n"


# --- F3: guarded observation + loop-level containment ----------------------

def test_loop_bind_second_observe_raises_no_escape(db_path):
    """F3.1: a provider whose FIRST observation (reconcile) is valid but whose
    SECOND observation (BIND execution) raises TypeError must not escape
    run_once(); the guarded observe turns it into a CONFLICT, BIND is skipped,
    and the dispatch stays unbound."""
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    assert d.status is DispatchStatus.PENDING
    provider, model, thinking, session = canonical_binding(d)
    run_id = f"run-{d.id[:8]}"
    running = make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking)

    class FlakyProvider:
        def __init__(self, first):
            self.first = first
            self.calls = 0

        def observe(self, lookup):
            self.calls += 1
            if self.calls == 1:
                return self.first
            raise TypeError("unhashable type: 'list'")

    env.sup._run_status = FlakyProvider(running)
    loop = SupervisorLoop(env.sup)
    dec = loop.run_once(env.job.supervisor_job_id)  # must NOT raise
    assert dec.action is ReconcileAction.BIND_RUN, dec.action
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.PENDING


def test_loop_run_once_structural_escape_bounded(db_path, monkeypatch):
    """F3.2: a structural adapter exception escaping reconcile() must be caught
    at the loop level, converted to a structured decision (reason
    ``adapter_exception:<TypeName>``), and bounded backoff -> sticky
    PERSISTENT_ERROR after MAX_RUNTIME_UNKNOWN.  The loop never dies."""
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)

    def boom(job_id):
        raise TypeError("structural escape")

    monkeypatch.setattr(env.sup, "reconcile", boom)
    loop = SupervisorLoop(env.sup)
    dec = loop.run_once(env.job.supervisor_job_id)  # must NOT raise
    assert dec.action is ReconcileAction.WAIT, dec.action
    assert dec.reason == "adapter_exception:TypeError", dec.reason
    for _ in range(MAX_RUNTIME_UNKNOWN + 2):
        dec = loop.run_once(env.job.supervisor_job_id)
        if dec.action is ReconcileAction.PERSISTENT_ERROR:
            break
    else:
        raise AssertionError("never reached PERSISTENT_ERROR")
    state = env.sup.store.get_job(env.job.supervisor_job_id)
    assert state.status == SupervisorJobStatus.ERROR.value, state.status
    assert state.last_error_code == "adapter_exception:TypeError"
    assert state.terminal is None


# --- F4: recovery smoke trajectory start wait (bounded, no double spawn) ----

def _load_recovery_real_smoke():
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "smoke" / "phase2c_recovery_real.py"
    spec = importlib.util.spec_from_file_location("phase2c_recovery_real", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _setup_analyst_spawn_fixture(env, d, tmp_path):
    """Insert a SPAWN_RUN journal row + launch counter for a dispatch, plus an
    (empty) analyst trajectory state dir."""
    env.sup.store._store._insert_supervisor_action({
        "id": "spawn-f4", "supervisor_job_id": env.job.supervisor_job_id,
        "dispatch_id": d.id, "action_type": "SPAWN_RUN",
        "action_key": f"supervisor:dispatch:{d.id}:spawn",
        "args_hash": "h", "input_hash": None, "precondition_hash": None,
        "effect_hash": None, "status": "SUCCEEDED", "attempt_count": 1,
        "next_attempt_at": None, "started_at": "t", "finished_at": "t",
        "last_error_code": None, "created_at": "t", "updated_at": "t",
    })
    counter_path = tmp_path / "launch-counter.json"
    counter_path.write_text(json.dumps({d.id: 1}))
    state_dir = tmp_path / "state"
    traj = (state_dir / "agents" / "argent-analyst" / "sessions"
            / f"dispatch-{d.id}.trajectory.jsonl")
    traj.parent.mkdir(parents=True, exist_ok=True)
    traj.write_text("")  # start row flushed LATE
    return counter_path, state_dir, traj


def test_f4_recovery_wait_for_trajectory_start_bounded(
        db_path, tmp_path, monkeypatch):
    """F4: the recovery smoke waits BOUNDEDLY for the late-flushed analyst
    trajectory start row while continuously re-asserting launcher count and
    SPAWN journal rows stay exactly 1 (no double spawn)."""
    smoke_mod = _load_recovery_real_smoke()
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    counter_path, state_dir, traj = _setup_analyst_spawn_fixture(env, d, tmp_path)

    sleeps = {"n": 0}

    class FakeTime:
        def __init__(self):
            self._t = 0.0

        def monotonic(self):
            return self._t

        def sleep(self, s):
            sleeps["n"] += 1
            if sleeps["n"] == 1:
                # The trajectory start row is flushed after the first poll.
                traj.write_text(json.dumps({"type": "session.started"}) + "\n")
            # Do not advance time (deadline never reached).

    monkeypatch.setattr(smoke_mod, "time", FakeTime())
    smoke_mod._wait_for_analyst_start(
        env.core, counter_path, d.id, state_dir=state_dir,
        timeout_seconds=1.0, poll_seconds=0.5,
    )
    assert smoke_mod._trajectory_started_count(d.id, state_dir) == 1
    assert sleeps["n"] == 1


def test_f4_recovery_wait_expires_with_clear_message(
        db_path, tmp_path, monkeypatch):
    """F4: if the trajectory start row never appears, the bounded wait expires
    and fails with a clear message (launcher/spawn remained 1 throughout)."""
    smoke_mod = _load_recovery_real_smoke()
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    counter_path, state_dir, _traj = _setup_analyst_spawn_fixture(env, d, tmp_path)

    class FakeTime:
        def __init__(self):
            self._t = 0.0

        def monotonic(self):
            return self._t

        def sleep(self, s):
            self._t += s

    monkeypatch.setattr(smoke_mod, "time", FakeTime())
    with pytest.raises(AssertionError) as exc_info:
        smoke_mod._wait_for_analyst_start(
            env.core, counter_path, d.id, state_dir=state_dir,
            timeout_seconds=1.0, poll_seconds=0.5,
        )
    assert "never flushed" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Phase-2C Fix Round 9: action-time guarded-observation adapter livelock
# ---------------------------------------------------------------------------
# A provider that returns a VALID observation on each reconcile observation but
# raises a structural TypeError on each ACTION re-observation must be bounded:
# retry_count monotonically increases, the job lands in a sticky PERSISTENT_ERROR
# after MAX_RUNTIME_UNKNOWN, and no exception escapes run_once().  This applies
# uniformly to every action handler that re-observes the runtime (BIND_RUN,
# APPLY_PATCH_SET, RUN_SANDBOX_TESTS, RECORD_TEST_RESULT, CONSUME_RESULT).


class _AlternatingAdapterProvider:
    """Returns ``valid`` on odd observe() calls (reconcile) and raises a
    structural TypeError on even calls (action re-observation)."""

    def __init__(self, valid):
        self.valid = valid
        self.calls = 0

    def observe(self, lookup):
        self.calls += 1
        if self.calls % 2 == 1:
            return self.valid
        raise TypeError("unhashable type: 'list'")


def _action_reobserve_setup(db_path, tmp_path, action):
    """Drive the job to the point where the NEXT reconcile decision is
    ``action`` and return ``(env, dispatch, valid_observation)``, where
    ``valid_observation`` is what a well-behaved reconcile would observe."""
    if action is ReconcileAction.BIND_RUN:
        env = make_env(db_path)
        advance(env, ReconcileAction.START_ROLE)
        advance(env, ReconcileAction.CREATE_DISPATCH)
        d = env.core.queries.list_dispatches(env.task.id)[-1]
        assert d.status is DispatchStatus.PENDING
        provider, model, thinking, session = canonical_binding(d)
        valid = make_run_observation(
            dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
            run_id=f"run-{d.id[:8]}", session_id=session, provider=provider,
            model=model, thinking_tier=thinking,
        )
        return env, d, valid

    if action is ReconcileAction.CONSUME_RESULT:
        env = make_env(db_path)
        advance(env, ReconcileAction.START_ROLE)
        advance(env, ReconcileAction.CREATE_DISPATCH)
        d = env.core.queries.list_dispatches(env.task.id)[-1]
        result = build_output(Role.LEAD, env.task.id, d.id)
        _bind_and_succeed(env, d.id, Role.LEAD, result)
        dd = env.core.queries.get_dispatch(d.id)
        valid = make_run_observation(
            dispatch_id=d.id, role=d.role, status=RunStatus.SUCCEEDED,
            run_id=dd.openclaw_run_id, session_id=dd.child_session_id,
            provider=dd.actual_provider, model=dd.actual_model,
            thinking_tier=dd.thinking_tier, result=result,
        )
        return env, d, valid

    # Write pipeline (APPLY_PATCH_SET / RUN_SANDBOX_TESTS / RECORD_TEST_RESULT).
    env, d = _make_write_env(db_path, tmp_path)
    result = _write_result(
        Role.IMPLEMENTER, env.task.id, d.id, "patch_set",
        [{"op": "write", "path": "src/module.py",
          "content": base64.b64encode(b"def parse_duration(s):\n    return None\n").decode()}],
    )
    _bind_implementer_succeeded(env, d, result)
    if action is ReconcileAction.RUN_SANDBOX_TESTS:
        advance(env, ReconcileAction.APPLY_PATCH_SET)
    elif action is ReconcileAction.RECORD_TEST_RESULT:
        advance(env, ReconcileAction.APPLY_PATCH_SET)
        advance(env, ReconcileAction.RUN_SANDBOX_TESTS)
    dd = env.core.queries.get_dispatch(d.id)
    valid = make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.SUCCEEDED,
        run_id=dd.openclaw_run_id, session_id=dd.child_session_id,
        provider=dd.actual_provider, model=dd.actual_model,
        thinking_tier=dd.thinking_tier, result=result,
    )
    return env, d, valid


@pytest.mark.parametrize("action", [
    ReconcileAction.BIND_RUN,
    ReconcileAction.APPLY_PATCH_SET,
    ReconcileAction.RUN_SANDBOX_TESTS,
    ReconcileAction.RECORD_TEST_RESULT,
    ReconcileAction.CONSUME_RESULT,
])
def test_action_time_adapter_failure_bounded(db_path, tmp_path, action):
    """An alternating valid-on-reconcile / TypeError-on-action-reobserve
    provider is bounded: retry_count monotonically increases and is persisted,
    the job reaches sticky PERSISTENT_ERROR after MAX_RUNTIME_UNKNOWN action
    attempts, and the action is never re-planned unboundedly (no busy-loop)."""
    env, d, valid = _action_reobserve_setup(db_path, tmp_path, action)
    env.sup._run_status = _AlternatingAdapterProvider(valid)
    loop = SupervisorLoop(env.sup)
    seen_retries = []
    action_attempts = 0
    for _ in range(MAX_RUNTIME_UNKNOWN + 3):
        dec = loop.run_once(env.job.supervisor_job_id)  # must NOT raise
        if dec.action is action:
            action_attempts += 1
        state = env.sup.store.get_job(env.job.supervisor_job_id)
        seen_retries.append(state.retry_count)
        if state.status == SupervisorJobStatus.ERROR.value:
            break
    state = env.sup.store.get_job(env.job.supervisor_job_id)
    assert state.status == SupervisorJobStatus.ERROR.value, (action, state.status)
    assert state.recovery_state == "PERSISTENT_ERROR", (action, state.recovery_state)
    assert state.last_error_code == "adapter_exception:TypeError", \
        (action, state.last_error_code)
    # retry_count grew monotonically to the cap and was persisted each time.
    assert seen_retries == list(range(1, MAX_RUNTIME_UNKNOWN + 1)), \
        (action, seen_retries)
    # Exactly MAX_RUNTIME_UNKNOWN action attempts, never more (bounded).
    assert action_attempts == MAX_RUNTIME_UNKNOWN, (action, action_attempts)
    # Sticky: a further run_once is NONE and never re-plans the action.
    dec = loop.run_once(env.job.supervisor_job_id)
    assert dec.action is ReconcileAction.NONE, (action, dec.action)


def test_action_time_adapter_failure_single_iteration_backoff(db_path, tmp_path):
    """A single action-time TypeError persists a backoff (retry_count=1,
    BACKOFF/RUNTIME_UNKNOWN, next_wake_at set) and never escapes run_once()."""
    env, d, valid = _action_reobserve_setup(
        db_path, tmp_path, ReconcileAction.BIND_RUN)
    env.sup._run_status = _AlternatingAdapterProvider(valid)
    loop = SupervisorLoop(env.sup)
    loop.run_once(env.job.supervisor_job_id)  # must NOT raise
    state = env.sup.store.get_job(env.job.supervisor_job_id)
    assert state.status == SupervisorJobStatus.BACKOFF.value, state.status
    assert state.recovery_state == "RUNTIME_UNKNOWN", state.recovery_state
    assert state.retry_count == 1, state.retry_count
    assert state.last_error_code == "adapter_exception:TypeError", \
        state.last_error_code
    assert state.next_wake_at is not None
    assert state.terminal is None
    # The bind never executed: the dispatch stays unbound.
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.PENDING


# ---------------------------------------------------------------------------
# Phase-2C Fix Round 11: write-role completion hints are advisory-only (F2)
# ---------------------------------------------------------------------------

def _bind_dispatch_running(env, d):
    """Bind a freshly created (PENDING) dispatch as RUNNING and return
    ``(session, run_id)``."""
    provider, model, thinking, session = canonical_binding(d)
    run_id = f"run-{d.id[:8]}"
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking,
    ))
    advance(env, ReconcileAction.BIND_RUN)
    return session, run_id


def _assert_write_hint_deferred(env, d, result):
    """Send a completion hint for a write-role dispatch and assert it is
    deferred (never consumes, never mutates supervisor state)."""
    dd = env.core.queries.get_dispatch(d.id)
    event_meta = {
        "task_id": env.task.id,
        "child_session_id": dd.child_session_id,
        "run_id": dd.openclaw_run_id,
        "parent_dispatch_id": dd.parent_dispatch_id,
        "event_type": "agent.completed",
        "status": "completed",
    }
    state_before = env.core.queries.get_task(env.task.id).state
    decisions_before = len(env.core.queries.list_decisions(env.task.id))
    handoffs_before = len(env.core.queries.list_handoffs(env.task.id))
    res = env.sup.receive_completion_hint(d.id, event_meta, result)
    assert res.status == "deferred", res
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.RUNNING
    assert env.core.queries.get_task(env.task.id).state is state_before
    assert len(env.core.queries.list_decisions(env.task.id)) == decisions_before
    assert len(env.core.queries.list_handoffs(env.task.id)) == handoffs_before
    assert env.sup._consume_action(d.id) is None


@pytest.mark.parametrize("with_patch", [True, False], ids=["with_patch", "no_patch"])
def test_f2_implementer_completion_hint_is_advisory(db_path, tmp_path, with_patch):
    """F2: an Implementer completion hint (valid envelope, patch extension
    present or absent) is ADVISORY ONLY — the hint is deferred (never consumes
    directly); the persisted provider observation then drives the normal
    APPLY -> RUN_SANDBOX_TESTS -> RECORD_TEST_RESULT -> CONSUME_RESULT flow."""
    env = make_env(db_path, workspace=make_workspace(tmp_path),
                   run_tests_fn=fake_run_tests)
    d = _drive_to_role_dispatch(env, Role.IMPLEMENTER)
    session, run_id = _bind_dispatch_running(env, d)
    patch = ([{"op": "write", "path": "src/module.py",
               "content": base64.b64encode(b"def x():\n    pass\n").decode()}]
             if with_patch else [])
    result = _write_result(Role.IMPLEMENTER, env.task.id, d.id, "patch_set", patch)
    _assert_write_hint_deferred(env, d, result)

    # The provider observation (not the hint) drives the persisted pipeline.
    provider, model, thinking, _ = canonical_binding(d)
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.SUCCEEDED,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking, result=result,
    ))
    advance(env, ReconcileAction.APPLY_PATCH_SET)
    advance(env, ReconcileAction.RUN_SANDBOX_TESTS)
    advance(env, ReconcileAction.RECORD_TEST_RESULT)
    advance(env, ReconcileAction.CONSUME_RESULT)
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED


def test_f2_qa_completion_hint_is_advisory(db_path, tmp_path):
    """F2: a QA completion hint is deferred exactly like Implementer; the
    provider observation drives the normal write pipeline to consume."""
    env = make_env(db_path, workspace=make_workspace(tmp_path),
                   run_tests_fn=fake_run_tests)
    d = _drive_to_role_dispatch(env, Role.QA)
    session, run_id = _bind_dispatch_running(env, d)
    result = _write_result(
        Role.QA, env.task.id, d.id, "test_patch_set",
        [{"op": "write", "path": "tests/test_parser.py",
          "content": base64.b64encode(b"def test_x():\n    pass\n").decode()}],
    )
    _assert_write_hint_deferred(env, d, result)

    provider, model, thinking, _ = canonical_binding(d)
    env.prov.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.SUCCEEDED,
        run_id=run_id, session_id=session, provider=provider, model=model,
        thinking_tier=thinking, result=result,
    ))
    advance(env, ReconcileAction.APPLY_PATCH_SET)
    advance(env, ReconcileAction.RUN_SANDBOX_TESTS)
    advance(env, ReconcileAction.RECORD_TEST_RESULT)
    advance(env, ReconcileAction.CONSUME_RESULT)
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED


@pytest.mark.parametrize("role", _NON_WRITE_ROLES, ids=lambda r: r.value)
def test_f2_non_write_completion_hint_still_consumes(role, db_path, tmp_path):
    """F2 guard: non-write roles (LEAD/ANALYST/REVIEWER) keep the direct Core
    fast path — a completion hint still consumes directly."""
    env = make_env(db_path, workspace=make_workspace(tmp_path),
                   run_tests_fn=fake_run_tests)
    d = _drive_to_role_dispatch(env, role)
    result = build_output(role, env.task.id, d.id)
    _bind_and_succeed(env, d.id, role, result)
    dd = env.core.queries.get_dispatch(d.id)
    event_meta = {
        "task_id": env.task.id,
        "child_session_id": dd.child_session_id,
        "run_id": dd.openclaw_run_id,
        "parent_dispatch_id": dd.parent_dispatch_id,
        "event_type": "agent.completed",
        "status": "completed",
    }
    res = env.sup.receive_completion_hint(d.id, event_meta, result)
    assert res.status == "consumed", res
    assert env.core.queries.get_dispatch(d.id).status is DispatchStatus.CONSUMED


