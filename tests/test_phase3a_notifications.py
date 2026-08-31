"""Phase 3A owner-notification tests — deterministic, offline (SPEC V3A §11).

Covers the V5 schema/migration, the ``notification_outbox`` store CRUD, the
dedup-guarded enqueue helper, the four trigger classes (§7 + Amendment 2
mapping), idempotency and atomicity.  No network, no sleeps, no agents, no
delivery worker (round B).  Uses the FakeClock / FakeRunStatus runtime; a
"restart" is a new Core/Supervisor over the same DB.
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
from argent_core.gates import binding_hash  # noqa: E402
from argent_core.models import (  # noqa: E402
    ApprovalStatus,
    DispatchStatus,
    TaskState,
)
from argent_core.notifications import (  # noqa: E402
    ALLOWED_REASON_CODES,
    TEMPLATE_VERSION,
    NotificationStatus,
    NotificationType,
    build_payload,
    canonical_payload_json,
    event_ref_close,
    event_ref_gate,
    event_ref_persistent_error,
    gate_dedup_key,
    normal_dedup_key,
    outbox_id,
    payload_hash,
    render_message,
    resolve_close_outcome,
    scope_ref,
)
from argent_core.sandbox_runner import SandboxResult  # noqa: E402
from argent_core.store import SCHEMA_VERSION  # noqa: E402
from argent_core.supervisor import (  # noqa: E402
    MAX_DISPATCH_ATTEMPTS_PER_STEP,
    MISSING_BOUND_RUN_CONFIRMATIONS,
    MISSING_UNBOUND_SPAWN_CONFIRMATIONS,
    ReconcileAction,
    RunStatus,
    Supervisor,
    _canonical_json,
    _sha256,
)
from mock_runtime import build_output  # noqa: E402
from mock_supervisor_runtime import (  # noqa: E402
    FakeClock,
    FakeRunLauncher,
    FakeRunStatusProvider,
    canonical_binding,
    make_run_observation,
)

OWNER = OWNER_SOURCE
LEAD = role_source(Role.LEAD)


# ---------------------------------------------------------------------------
# Helpers (mirror test_phase2c_supervisor so this module stays self-contained)
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


def make_env(db_path, clock=None, *, workspace=None, run_tests_fn=None,
             idempotency_key="job-1"):
    clock = clock or FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    task_run = core.start_task_run(task.id, OWNER)
    prov = FakeRunStatusProvider()
    launch = FakeRunLauncher()
    sup = Supervisor(
        core, prov, launch, clock=clock,
        workspace_root=workspace, run_tests_fn=run_tests_fn,
    )
    job = sup.store.create_job(task.id, idempotency_key=idempotency_key)
    return SimpleNamespace(
        core=core, task=task, task_run=task_run, prov=prov, launch=launch,
        sup=sup, job=job, clock=clock,
    )


def step(env):
    d = env.sup.reconcile(env.job.supervisor_job_id)
    env.sup.perform_next_safe_action_if_required(d)
    return d


def advance(env, action, max_steps=40):
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


def drive_frontier(env, role, result_fn=None):
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


def rows_for(env):
    return env.core._store.list_notifications(env.job.supervisor_job_id)


def set_task_state(env, state):
    env.core._store._update_task_state(
        env.task.id, state, None, env.core._store.now_iso(),
    )


# ---------------------------------------------------------------------------
# notifications module: templates / mapping / hashes
# ---------------------------------------------------------------------------

def test_reason_code_allowlist_exact():
    assert ALLOWED_REASON_CODES == {
        "TASK_DONE", "TASK_FAILED", "TASK_CANCELLED", "MAX_ATTEMPTS",
        "PERSISTENT_ERROR", "TASK_BLOCKED", "GATE_REJECTED",
        "SPAWN_UNRESOLVABLE", "AMBIGUOUS_WRITER", "WAITING_GATE",
    }


def test_render_done_template():
    msg = render_message(
        NotificationType.DONE, supervisor_job_id="sj", task_id="t",
        event_at="2026-01-01T00:00:00+00:00", reason_code="TASK_DONE",
        dedup_key="a" * 64,
    )
    assert msg == (
        "ARGENT · DONE\n"
        "Job: sj\n"
        "Task: t\n"
        "Time: 2026-01-01T00:00:00+00:00\n"
        f"Ref: {'a' * 16}"
    )


def test_render_failed_and_blocked_show_reason():
    for ntype in (NotificationType.FAILED, NotificationType.BLOCKED):
        msg = render_message(
            ntype, supervisor_job_id="sj", task_id="t", event_at="e",
            reason_code="TASK_FAILED", dedup_key="b" * 64,
        )
        assert f"Reason: TASK_FAILED" in msg
        assert "ARGENT · " in msg


def test_render_owner_approval_template():
    msg = render_message(
        NotificationType.OWNER_APPROVAL_REQUIRED, supervisor_job_id="sj",
        task_id="t", event_at="e", reason_code="WAITING_GATE",
        dedup_key="c" * 64, gate_id="g1", scope_ref="sha256:abcd1234",
    )
    assert "ARGENT · OWNER APPROVAL REQUIRED" in msg
    assert "Gate: g1" in msg
    assert "Scope ref: sha256:abcd1234" in msg
    assert "Informational only. Use the authenticated owner-control path." in msg


def test_resolve_close_outcome_mapping():
    assert resolve_close_outcome("DONE", "task_done") == (
        NotificationType.DONE, "TASK_DONE")
    assert resolve_close_outcome("FAILED", "task_failed_cancelled",
                                 task_state="FAILED") == (
        NotificationType.FAILED, "TASK_FAILED")
    assert resolve_close_outcome("FAILED", "task_failed_cancelled",
                                 task_state="CANCELLED") == (
        NotificationType.FAILED, "TASK_CANCELLED")
    assert resolve_close_outcome("FAILED", "max_attempts") == (
        NotificationType.FAILED, "MAX_ATTEMPTS")
    assert resolve_close_outcome("BLOCKED", "task_blocked") == (
        NotificationType.BLOCKED, "TASK_BLOCKED")
    assert resolve_close_outcome("BLOCKED", "task_blocked",
                                 gate_rejected=True) == (
        NotificationType.BLOCKED, "GATE_REJECTED")
    assert resolve_close_outcome("BLOCKED", "spawn_unresolvable") == (
        NotificationType.BLOCKED, "SPAWN_UNRESOLVABLE")
    assert resolve_close_outcome("BLOCKED", "ambiguous_writer") == (
        NotificationType.BLOCKED, "AMBIGUOUS_WRITER")


def test_resolve_close_outcome_gate_rejected_priority():
    """F3 (SPEC §7 BLOCKED priority): a rejected gate wins over every other
    BLOCKED signal; the others only apply when the gate is not rejected."""
    assert resolve_close_outcome(
        "BLOCKED", "spawn_unresolvable", gate_rejected=True) == (
        NotificationType.BLOCKED, "GATE_REJECTED")
    assert resolve_close_outcome(
        "BLOCKED", "ambiguous_writer", gate_rejected=True) == (
        NotificationType.BLOCKED, "GATE_REJECTED")
    assert resolve_close_outcome(
        "BLOCKED", "task_blocked", gate_rejected=True) == (
        NotificationType.BLOCKED, "GATE_REJECTED")
    # Not gate-rejected -> the specific reason applies.
    assert resolve_close_outcome("BLOCKED", "spawn_unresolvable") == (
        NotificationType.BLOCKED, "SPAWN_UNRESOLVABLE")
    assert resolve_close_outcome("BLOCKED", "ambiguous_writer") == (
        NotificationType.BLOCKED, "AMBIGUOUS_WRITER")
    assert resolve_close_outcome("BLOCKED", "task_blocked") == (
        NotificationType.BLOCKED, "TASK_BLOCKED")


def test_hashes_use_existing_canonical_json():
    # Amendment 1: notification hashes must equal _sha256(_canonical_json([...])).
    job_id, ntype, eref = "sj", NotificationType.FAILED, "supervisor:sj:close:FAILED"
    expected = _sha256(_canonical_json(
        ["argent-notification-v1", job_id, "FAILED", eref, 1]))
    assert normal_dedup_key(job_id, ntype, eref, 1) == expected
    assert outbox_id(expected) == "notification:" + expected
    assert len(payload_hash({"a": 1})) == 64


def test_scope_ref_truncates_binding_hash():
    assert scope_ref("a" * 64) == "sha256:" + ("a" * 16)


def test_payload_allowed_keys_only():
    payload = build_payload(
        notification_type="FAILED", supervisor_job_id="sj", task_id="t",
        event_ref="supervisor:sj:close:FAILED", event_at="e",
        reason_code="TASK_FAILED",
    )
    assert set(payload) == {
        "template_version", "notification_type", "supervisor_job_id",
        "task_id", "event_ref", "event_at", "reason_code",
    }
    assert payload["template_version"] == TEMPLATE_VERSION
    assert payload["reason_code"] == "TASK_FAILED"
    # A non-allowlisted reason is rejected fail-closed.
    with pytest.raises(ValueError):
        build_payload(
            notification_type="FAILED", supervisor_job_id="s", task_id="t",
            event_ref="r", event_at="e", reason_code="frontier_exhausted",
        )


# ---------------------------------------------------------------------------
# Schema / migration (SPEC V3A §11.1)
# ---------------------------------------------------------------------------

def test_fresh_db_version5_and_table_indexes(db_path):
    core = Core(db_path)
    try:
        row = core._store._conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        assert row["value"] == SCHEMA_VERSION
        names = {r["name"] for r in core._store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "notification_outbox" in names
        indexes = {r["name"] for r in core._store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        assert {
            "idx_notification_outbox_dedup",
            "idx_notification_outbox_due",
            "idx_notification_outbox_job",
        } <= indexes
    finally:
        core.close()


def test_v4_to_v5_preserves_data_and_recreates_table(db_path):
    core = Core(db_path)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    # Simulate a V4 DB: drop the V5 table + indexes and stamp the version 4.
    conn = core._store._conn
    conn.execute("DROP TABLE notification_outbox")
    conn.execute(
        "UPDATE schema_meta SET value = '4' WHERE key = 'schema_version'"
    )
    core.close()

    core2 = Core(db_path)
    try:
        row = core2._store._conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        assert row["value"] == SCHEMA_VERSION
        names = {r["name"] for r in core2._store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "notification_outbox" in names
        # V4 data intact.
        assert core2.queries.get_task(task.id) is not None
        assert core2.queries.get_project(project.id) is not None
    finally:
        core2.close()


def test_no_backfill_on_upgrade(db_path):
    core = Core(db_path)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    conn = core._store._conn
    conn.execute("DROP TABLE notification_outbox")
    conn.execute(
        "UPDATE schema_meta SET value = '4' WHERE key = 'schema_version'"
    )
    core.close()

    # Reopen (V4 -> V6): no historical rows are backfilled.
    core2 = Core(db_path)
    try:
        assert core2._store.list_notifications() == []
    finally:
        core2.close()


def test_migration_rollback_on_error(db_path, monkeypatch):
    def failing_migrate(self):
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr("argent_core.store.Store._migrate", failing_migrate)
    with pytest.raises(RuntimeError):
        Core(db_path)
    monkeypatch.undo()

    conn = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "notification_outbox" not in tables
        assert "projects" not in tables  # _SCHEMA DDL rolled back with migration
    finally:
        conn.close()

    # A subsequent open succeeds (V6, table present).
    core = Core(db_path)
    try:
        row = core._store._conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        assert row["value"] == SCHEMA_VERSION
        names = {r["name"] for r in core._store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "notification_outbox" in names
    finally:
        core.close()


def _base_row(env, **over):
    row = {
        "id": "n1", "supervisor_job_id": env.job.supervisor_job_id,
        "task_id": env.task.id, "dispatch_id": None, "gate_id": None,
        "notification_type": "DONE", "event_ref": "r", "event_version": 1,
        "dedup_key": "same", "payload_json": "{}", "payload_hash": "0" * 64,
        "status": "PENDING", "attempt_count": 0, "next_attempt_at": None,
        "claimed_at": None, "claim_token": None, "last_attempt_at": None,
        "sent_at": None, "last_error_code": None, "created_at": "c",
        "updated_at": "c",
    }
    row.update(over)
    return row


def test_notification_outbox_check_constraints(db_path):
    env = make_env(db_path)

    def insert(**over):
        return env.core._store._insert_notification(_base_row(env, **over))

    # invalid notification_type
    with pytest.raises(sqlite3.IntegrityError):
        insert(notification_type="INVALID")
    # invalid status
    with pytest.raises(sqlite3.IntegrityError):
        insert(status="INVALID")
    # payload_hash not 64 chars
    with pytest.raises(sqlite3.IntegrityError):
        insert(payload_hash="short")
    # SENDING requires claim_token + claimed_at
    with pytest.raises(sqlite3.IntegrityError):
        insert(status="SENDING", claim_token="tok", claimed_at=None)
    # non-SENDING must not carry a claim_token
    with pytest.raises(sqlite3.IntegrityError):
        insert(status="PENDING", claim_token="tok")
    # SENT requires sent_at
    with pytest.raises(sqlite3.IntegrityError):
        insert(status="SENT", sent_at=None)
    # a fully valid row inserts
    assert insert(status="PENDING", dedup_key="ok") is True


def test_notification_outbox_no_secret_columns(db_path):
    core = Core(db_path)
    try:
        cols = {r[1] for r in core._store._conn.execute(
            "PRAGMA table_info(notification_outbox)").fetchall()}
        assert cols == {
            "id", "supervisor_job_id", "task_id", "dispatch_id", "gate_id",
            "notification_type", "event_ref", "event_version", "dedup_key",
            "payload_json", "payload_hash", "status", "attempt_count",
            "next_attempt_at", "claimed_at", "claim_token", "last_attempt_at",
            "sent_at", "last_error_code", "created_at", "updated_at",
        }
        forbidden = {"credential", "secret", "bot_token", "api_key",
                     "password", "chat_id", "url", "header", "response",
                     "body", "inbound", "usertext", "target_id"}
        for col in cols:
            assert not any(f in col.lower() for f in forbidden), col
    finally:
        core.close()


def test_unique_dedup_index_and_insert_noop(db_path):
    env = make_env(db_path)
    assert env.core._store._insert_notification(_base_row(env)) is True
    # Same dedup_key -> silent no-op (returns False, no exception).
    assert env.core._store._insert_notification(
        _base_row(env, id="n2")) is False
    assert len(env.core._store.list_notifications()) == 1
    assert env.core._store.get_notification("n1")["dedup_key"] == "same"


def test_store_claim_and_completion_cas(db_path):
    env = make_env(db_path)
    env.core._store._insert_notification(_base_row(env))
    # due selection (PENDING) with a 30s lease.
    due = env.core._store._select_due_notification("c", 30)
    assert due is not None and due["id"] == "n1"
    # claim it
    assert env.core._store._claim_notification("n1", "tok1", "c", 30) is True
    n = env.core._store.get_notification("n1")
    assert n["status"] == "SENDING" and n["claim_token"] == "tok1"
    assert n["attempt_count"] == 1
    # completion CAS with the right token
    assert env.core._store._complete_notification_sent("n1", "tok1", "c") is True
    n = env.core._store.get_notification("n1")
    assert n["status"] == "SENT" and n["sent_at"] == "c"
    assert n["claim_token"] is None and n["claimed_at"] is None


# ---------------------------------------------------------------------------
# Triggers (SPEC V3A §7 + Amendment 2 mapping)
# ---------------------------------------------------------------------------

def test_trigger_done(db_path, tmp_path):
    env = make_env(db_path, workspace=make_workspace(tmp_path),
                   run_tests_fn=fake_run_tests)
    drive_to_done(env)
    advance(env, ReconcileAction.CLOSE_DONE, max_steps=3)
    rows = rows_for(env)
    assert len(rows) == 1
    r = rows[0]
    assert r["notification_type"] == "DONE"
    assert r["status"] == "PENDING"
    payload = json.loads(r["payload_json"])
    assert payload["reason_code"] == "TASK_DONE"
    assert r["event_ref"] == event_ref_close(env.job.supervisor_job_id, "DONE")
    # No secret content: payload is exactly the allowed key set.
    assert set(payload) == {
        "template_version", "notification_type", "supervisor_job_id",
        "task_id", "event_ref", "event_at", "reason_code",
    }


def test_trigger_failed_task_failed(db_path):
    env = make_env(db_path)
    set_task_state(env, TaskState.FAILED)
    d = step(env)
    assert d.action is ReconcileAction.CLOSE_FAILED
    rows = rows_for(env)
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"])["reason_code"] == "TASK_FAILED"
    assert rows[0]["notification_type"] == "FAILED"


def test_trigger_failed_task_cancelled(db_path):
    env = make_env(db_path)
    set_task_state(env, TaskState.CANCELLED)
    d = step(env)
    assert d.action is ReconcileAction.CLOSE_FAILED
    rows = rows_for(env)
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"])["reason_code"] == "TASK_CANCELLED"


def test_trigger_failed_max_attempts(db_path):
    env = make_env(db_path)
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
    d = advance(env, ReconcileAction.CLOSE_FAILED, max_steps=5)
    assert d.reason == "max_attempts"
    rows = rows_for(env)
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"])["reason_code"] == "MAX_ATTEMPTS"


def test_trigger_persistent_error_sticky(db_path):
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
        dispatch_id=d.id, role=d.role, status=RunStatus.UNKNOWN,
    ))
    for _ in range(12):
        dd = step(env)
        if dd.action is ReconcileAction.PERSISTENT_ERROR:
            break
    else:
        raise AssertionError("never hit PERSISTENT_ERROR")
    rows = rows_for(env)
    assert len(rows) == 1
    assert rows[0]["notification_type"] == "FAILED"
    assert json.loads(rows[0]["payload_json"])["reason_code"] == "PERSISTENT_ERROR"
    assert rows[0]["event_ref"] == event_ref_persistent_error(
        env.job.supervisor_job_id)


def test_trigger_blocked_task_blocked(db_path):
    env = make_env(db_path)
    set_task_state(env, TaskState.BLOCKED)
    d = step(env)
    assert d.action is ReconcileAction.CLOSE_BLOCKED
    rows = rows_for(env)
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"])["reason_code"] == "TASK_BLOCKED"
    assert rows[0]["notification_type"] == "BLOCKED"


def test_trigger_blocked_gate_rejected(db_path):
    env = make_env(db_path)
    env.core.start_role(env.task.id, Role.LEAD, LEAD)
    ar = env.core.request_action(
        env.task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    ap = ar.approval
    env.core.reject(ap.id, OWNER, task_id=env.task.id,
                    action="deploy_production", scope="prod")
    d = step(env)
    assert d.action is ReconcileAction.CLOSE_BLOCKED
    rows = rows_for(env)
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["reason_code"] == "GATE_REJECTED"
    assert rows[0]["gate_id"] is None  # close notification carries no gate ref


def test_trigger_blocked_spawn_unresolvable(db_path):
    env = make_env(db_path)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    d = env.core.queries.list_dispatches(env.task.id)[-1]
    env.sup.store._store._insert_supervisor_action({
        "id": "spawn-x", "supervisor_job_id": env.job.supervisor_job_id,
        "dispatch_id": d.id, "action_type": "SPAWN_RUN",
        "action_key": f"supervisor:dispatch:{d.id}:spawn",
        "args_hash": "h", "input_hash": None, "precondition_hash": None,
        "effect_hash": None, "status": "SUCCEEDED", "attempt_count": 1,
        "next_attempt_at": None, "started_at": "t", "finished_at": "t",
        "last_error_code": None, "created_at": "t", "updated_at": "t",
    })
    for _ in range(MISSING_UNBOUND_SPAWN_CONFIRMATIONS + 2):
        env.prov.set_current(d.id, make_run_observation(
            dispatch_id=d.id, role=d.role, status=RunStatus.NOT_FOUND,
            authoritative_not_found=True,
        ))
        dec = step(env)
        if dec.action is ReconcileAction.CLOSE_BLOCKED:
            break
    else:
        raise AssertionError("never reached CLOSE_BLOCKED")
    assert dec.reason == "spawn_unresolvable"
    rows = rows_for(env)
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"])["reason_code"] == "SPAWN_UNRESOLVABLE"


def test_trigger_blocked_ambiguous_writer(db_path):
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
    for _ in range(MISSING_BOUND_RUN_CONFIRMATIONS + 2):
        env.prov.set_current(d.id, make_run_observation(
            dispatch_id=d.id, role=d.role, status=RunStatus.NOT_FOUND,
            authoritative_not_found=True,
        ))
        dec = step(env)
        if dec.action is ReconcileAction.CLOSE_BLOCKED:
            break
    else:
        raise AssertionError("never reached CLOSE_BLOCKED")
    assert dec.reason == "ambiguous_writer"
    rows = rows_for(env)
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"])["reason_code"] == "AMBIGUOUS_WRITER"


def test_trigger_owner_approval_required_per_gate(db_path):
    env = make_env(db_path)
    env.core.start_role(env.task.id, Role.LEAD, LEAD)
    ar = env.core.request_action(
        env.task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    ap = ar.approval
    d = step(env)
    assert d.action is ReconcileAction.PRESENT_OWNER_GATE
    rows = rows_for(env)
    assert len(rows) == 1
    r = rows[0]
    assert r["notification_type"] == "OWNER_APPROVAL_REQUIRED"
    assert r["gate_id"] == ap.id
    payload = json.loads(r["payload_json"])
    assert payload["reason_code"] == "WAITING_GATE"
    assert payload["gate_id"] == ap.id
    assert payload["scope_ref"] == scope_ref(ap.binding_hash)
    assert r["event_ref"] == event_ref_gate(env.job.supervisor_job_id, ap.id)


# ---------------------------------------------------------------------------
# Negatives (SPEC V3A §7): no notification for non-terminal states
# ---------------------------------------------------------------------------

def test_no_notification_for_running_wait(db_path):
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
    d2 = step(env)
    assert d2.action is ReconcileAction.WAIT
    assert rows_for(env) == []


def test_no_notification_for_backoff(db_path):
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
        dispatch_id=d.id, role=d.role, status=RunStatus.UNKNOWN,
    ))
    d2 = step(env)
    assert d2.action is ReconcileAction.WAIT  # bounded backoff, not yet error
    assert rows_for(env) == []


def test_no_notification_for_retryable_run_failure(db_path):
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
    # Retry (START_ROLE), not terminal -> no notification.
    d2 = step(env)
    assert d2.action is ReconcileAction.START_ROLE
    assert rows_for(env) == []


def test_no_notification_for_closed_approved_gate(db_path):
    env = make_env(db_path)
    env.core.start_role(env.task.id, Role.LEAD, LEAD)
    ar = env.core.request_action(
        env.task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    ap = ar.approval
    env.core.approve(ap.id, OWNER, task_id=env.task.id,
                     action="deploy_production", scope="prod")
    env.core.execute_approved(ap.id, OWNER, task_id=env.task.id,
                              action="deploy_production", scope="prod")
    # Gate consumed/closed: reconcile must NOT present again.
    d = step(env)
    assert d.action is not ReconcileAction.PRESENT_OWNER_GATE
    assert rows_for(env) == []


# ---------------------------------------------------------------------------
# Idempotency (SPEC V3A §11.3)
# ---------------------------------------------------------------------------

def test_idempotent_terminal_reconcile_repeated(db_path, tmp_path):
    env = make_env(db_path, workspace=make_workspace(tmp_path),
                   run_tests_fn=fake_run_tests)
    drive_to_done(env)
    advance(env, ReconcileAction.CLOSE_DONE, max_steps=3)
    for _ in range(20):
        d = step(env)
        assert d.action is ReconcileAction.NONE
    rows = rows_for(env)
    assert len(rows) == 1


def test_idempotent_same_transition_direct_calls(db_path):
    env = make_env(db_path)
    set_task_state(env, TaskState.FAILED)
    job = {"id": env.job.supervisor_job_id, "task_id": env.task.id}
    for _ in range(5):
        env.sup._close_job(job, "FAILED", reason="task_failed_cancelled")
    rows = rows_for(env)
    assert len(rows) == 1


def test_idempotent_same_gate_one_new_gate_new(db_path):
    env = make_env(db_path)
    env.core.start_role(env.task.id, Role.LEAD, LEAD)
    ar1 = env.core.request_action(
        env.task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    d = step(env)
    assert d.action is ReconcileAction.PRESENT_OWNER_GATE
    # Re-presenting the SAME gate (reconcile + restart) -> still one row.
    for _ in range(5):
        d2 = step(env)
        assert d2.action is not ReconcileAction.PRESENT_OWNER_GATE
    rows = rows_for(env)
    assert len(rows) == 1
    assert rows[0]["gate_id"] == ar1.approval.id

    # Release gate 1 (approve + execute) so a NEW gate can be requested, then
    # request a second, different gate -> a new notification row.
    env.core.approve(ar1.approval.id, OWNER, task_id=env.task.id,
                     action="deploy_production", scope="prod")
    env.core.execute_approved(ar1.approval.id, OWNER, task_id=env.task.id,
                              action="deploy_production", scope="prod")
    ar2 = env.core.request_action(
        env.task.id, "change_secrets", "prod", Role.LEAD, LEAD)
    d3 = step(env)
    assert d3.action is ReconcileAction.PRESENT_OWNER_GATE
    rows = rows_for(env)
    assert len(rows) == 2
    gate_ids = {r["gate_id"] for r in rows}
    assert gate_ids == {ar1.approval.id, ar2.approval.id}


def test_idempotent_persistent_error_reconcile_repeated(db_path):
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
        dispatch_id=d.id, role=d.role, status=RunStatus.UNKNOWN,
    ))
    for _ in range(12):
        if step(env).action is ReconcileAction.PERSISTENT_ERROR:
            break
    for _ in range(20):
        d = step(env)
        assert d.action is ReconcileAction.NONE
    rows = rows_for(env)
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"])["reason_code"] == "PERSISTENT_ERROR"


def test_hashes_stable_across_restart(db_path, tmp_path):
    env = make_env(db_path, workspace=make_workspace(tmp_path),
                   run_tests_fn=fake_run_tests)
    drive_to_done(env)
    advance(env, ReconcileAction.CLOSE_DONE, max_steps=3)
    before = rows_for(env)[0]
    env.core.close()

    core2 = Core(db_path)
    prov2 = FakeRunStatusProvider()
    sup2 = Supervisor(core2, prov2, FakeRunLauncher())
    after = core2._store.list_notifications(env.job.supervisor_job_id)[0]
    assert after["dedup_key"] == before["dedup_key"]
    assert after["payload_hash"] == before["payload_hash"]
    assert after["id"] == before["id"]
    # Reconcile on restart: no new row.
    d = sup2.reconcile(env.job.supervisor_job_id)
    assert d.action is ReconcileAction.NONE
    assert len(core2._store.list_notifications(env.job.supervisor_job_id)) == 1


# ---------------------------------------------------------------------------
# Atomicity (SPEC V3A §11.3): row appears iff the transition commits
# ---------------------------------------------------------------------------

def test_atomicity_row_iff_transition_commits(db_path, monkeypatch):
    env = make_env(db_path)
    set_task_state(env, TaskState.FAILED)

    # Inject a failure in the enqueue so the CLOSE_JOB transaction rolls back:
    # neither the terminal transition NOR the notification row may persist.
    def boom(job, terminal, reason):
        raise RuntimeError("injected enqueue failure")

    monkeypatch.setattr(env.sup, "_enqueue_close_notification", boom)
    with pytest.raises(RuntimeError):
        step(env)
    monkeypatch.undo()

    job = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    assert job["terminal"] is None
    assert rows_for(env) == []

    # After removing the injection, the transition commits and the row appears.
    d = step(env)
    assert d.action is ReconcileAction.CLOSE_FAILED
    job = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    assert job["terminal"] == "FAILED"
    assert len(rows_for(env)) == 1


def test_atomicity_gate_row_iff_waiting_gate_commits(db_path, monkeypatch):
    env = make_env(db_path)
    env.core.start_role(env.task.id, Role.LEAD, LEAD)
    env.core.request_action(
        env.task.id, "deploy_production", "prod", Role.LEAD, LEAD)

    def boom(job, gate):
        raise RuntimeError("injected gate enqueue failure")

    monkeypatch.setattr(env.sup, "_enqueue_waiting_gate_notification", boom)
    with pytest.raises(RuntimeError):
        step(env)
    monkeypatch.undo()

    job = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    assert job["status"] != "WAITING_GATE"  # rollback: not committed
    assert rows_for(env) == []
