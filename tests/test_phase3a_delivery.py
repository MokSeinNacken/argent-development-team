"""Phase 3A delivery-worker tests — offline, deterministic (SPEC V3A §11.4-11.11).

Covers the bounded claim/retry/lease delivery worker (``NotificationDelivery``),
the outbound-only ``TelegramNotificationTransport`` adapter, the non-blocking
loop kick, and the no-secrets / owner-gate / restart-flood / crash-delivery
invariants.  No network, no sleeps in the delivery path, no agents, no real
Telegram requests.  Uses the FakeClock runtime and a scriptable
``DeterministicNotificationTransport`` (test-side, SPEC V3A §3.2).
"""

from __future__ import annotations

import base64
import json
import socket
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argent_core import Core, OWNER_SOURCE, Role, role_source  # noqa: E402
from argent_core.models import TaskState  # noqa: E402
from argent_core.notifications import (  # noqa: E402
    ERROR_ATTEMPTS_EXHAUSTED,
    ERROR_AUTH,
    ERROR_CONFIG,
    ERROR_HTTP_4XX,
    ERROR_HTTP_5XX,
    ERROR_NETWORK,
    ERROR_PAYLOAD_HASH_MISMATCH,
    ERROR_POLICY,
    ERROR_RATE_LIMITED,
    ERROR_SQLITE_LOCKED,
    ERROR_TIMEOUT,
    ERROR_TRANSPORT,
    NOTIFICATION_CLAIM_LEASE_SECONDS,
    NOTIFICATION_MAX_ATTEMPTS,
    NOTIFICATION_REQUEST_TIMEOUT_SECONDS,
    NotificationConfigError,
    NotificationDelivery,
    NotificationEnvelope,
    NotificationType,
    TelegramNotificationTransport,
    TransportReceipt,
    backoff_seconds,
    build_payload,
    canonical_payload_json,
    event_ref_close,
    gate_dedup_key,
    normal_dedup_key,
    outbox_id,
    payload_hash,
    render_message,
    scope_ref,
    telegram_transport_factory,
)
from argent_core.sandbox_runner import SandboxResult  # noqa: E402
from argent_core.supervisor import (  # noqa: E402
    ReconcileAction,
    RunStatus,
    Supervisor,
    SupervisorJobStatus,
    SupervisorLoop,
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

_BASE = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def iso(seconds: float = 0.0) -> str:
    return (_BASE + timedelta(seconds=seconds)).astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Test-side deterministic transport (SPEC V3A §3.2)
# ---------------------------------------------------------------------------

class DeterministicNotificationTransport:
    """Scriptable outbound transport with per-(dedup_key, payload_hash)
    idempotency and per-dedup-key send counts.  ``hang`` blocks for
    ``timeout_seconds`` (on an optional event); ``block`` blocks unbounded
    until the event is released (for the non-blocking loop test)."""

    def __init__(self, outcome="success", *, retry_after=None, error_code=None,
                 idempotent=False, block_event=None, raise_exc=None):
        self._outcome = outcome
        self._retry_after = retry_after
        self._error_code = error_code
        self._idempotent = idempotent
        self._block_event = block_event
        self._raise_exc = raise_exc
        self.send_count: dict[str, int] = {}
        self.external_sends: list[NotificationEnvelope] = []
        self.seen: dict[tuple[str, str], int] = {}
        self.last_timeout: float | None = None

    def send(self, envelope, *, timeout_seconds):
        key = (envelope.dedup_key, envelope.payload_hash)
        self.send_count[envelope.dedup_key] = self.send_count.get(envelope.dedup_key, 0) + 1
        self.seen[key] = self.seen.get(key, 0) + 1
        self.last_timeout = timeout_seconds
        if self._outcome == "hang":
            if self._block_event is not None:
                self._block_event.wait(timeout_seconds)
            return TransportReceipt(False, True, ERROR_TIMEOUT)
        if self._outcome == "block":
            if self._block_event is not None:
                self._block_event.wait()
            return TransportReceipt(True, False)
        if self._raise_exc is not None:
            raise self._raise_exc
        if self._idempotent and self.seen[key] > 1:
            # transport-side idempotency: a duplicate (dedup_key, payload_hash)
            # is acknowledged but NOT transmitted externally.
            return TransportReceipt(True, False)
        self.external_sends.append(envelope)
        return self._receipt()

    def _receipt(self):
        o = self._outcome
        if o == "success":
            return TransportReceipt(True, False)
        if o == "network_error":
            return TransportReceipt(False, True, ERROR_NETWORK)
        if o == "timeout":
            return TransportReceipt(False, True, ERROR_TIMEOUT)
        if o == "rate_limit":
            return TransportReceipt(False, True, ERROR_RATE_LIMITED, self._retry_after)
        if o == "http_5xx":
            return TransportReceipt(False, True, ERROR_HTTP_5XX)
        if o == "http_4xx":
            return TransportReceipt(False, False, ERROR_HTTP_4XX)
        if o == "auth_error":
            return TransportReceipt(False, False, ERROR_AUTH)
        if o == "policy_error":
            return TransportReceipt(False, False, ERROR_POLICY)
        return TransportReceipt(False, False, self._error_code or ERROR_TRANSPORT)


class BlockingTransport:
    """Signals entry into ``send`` and blocks until released (thread-safe)."""

    def __init__(self, receipt=TransportReceipt(True, False)):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.receipt = receipt
        self.sends: list[NotificationEnvelope] = []

    def send(self, envelope, *, timeout_seconds):
        self.sends.append(envelope)
        self.entered.set()
        self.release.wait()
        return self.receipt


# ---------------------------------------------------------------------------
# Helpers (mirror test_phase3a_notifications so this module is self-contained)
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
        sup=sup, job=job, clock=clock, db_path=db_path,
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


def _bind_writer(env):
    """Bind the job's writer to the last implementer dispatch (test infra)."""
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
    if role is Role.REVIEWER:
        _bind_writer(env)
    advance(env, ReconcileAction.START_ROLE)
    advance(env, ReconcileAction.CREATE_DISPATCH)
    dispatch = env.core.queries.list_dispatches(env.task.id)[-1]
    assert dispatch.role is role
    result = result_fn(dispatch.id) if result_fn else build_output(role, env.task.id, dispatch.id)
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


def insert_outbox(env, *, ntype=NotificationType.DONE, reason_code="TASK_DONE",
                  status="PENDING", attempt_count=0, next_attempt_at=None,
                  claimed_at=None, claim_token=None, sent_at=None,
                  last_error_code=None, dedup_key=None, gate_id=None,
                  binding_hash=None, event_ref=None, event_at=iso(0),
                  created_at=iso(0), payload_hash_override=None,
                  job_id=None, task_id=None):
    job_id = job_id or env.job.supervisor_job_id
    task_id = task_id or env.task.id
    if ntype is NotificationType.OWNER_APPROVAL_REQUIRED:
        event_ref = event_ref or f"supervisor:{job_id}:present-gate:{gate_id}"
        dedup_key = dedup_key or gate_dedup_key(job_id, gate_id, binding_hash)
    else:
        event_ref = event_ref or event_ref_close(job_id, ntype.value)
        dedup_key = dedup_key or normal_dedup_key(job_id, ntype, event_ref)
    payload = build_payload(
        notification_type=ntype.value, supervisor_job_id=job_id,
        task_id=task_id, event_ref=event_ref, event_at=event_at,
        reason_code=reason_code, gate_id=gate_id,
        scope_ref=(("sha256:" + binding_hash[:16]) if binding_hash else None),
    )
    ph = payload_hash(payload) if payload_hash_override is None else payload_hash_override
    row = {
        "id": outbox_id(dedup_key), "supervisor_job_id": job_id,
        "task_id": task_id, "dispatch_id": None, "gate_id": gate_id,
        "notification_type": ntype.value, "event_ref": event_ref,
        "event_version": 1, "dedup_key": dedup_key,
        "payload_json": canonical_payload_json(payload), "payload_hash": ph,
        "status": status, "attempt_count": attempt_count,
        "next_attempt_at": next_attempt_at, "claimed_at": claimed_at,
        "claim_token": claim_token, "last_attempt_at": None,
        "sent_at": sent_at, "last_error_code": last_error_code,
        "created_at": created_at, "updated_at": created_at,
    }
    assert env.core._store._insert_notification(row)
    return row


def make_delivery(env, factory, clock=None):
    return NotificationDelivery(env.db_path, factory, clock=clock or FakeClock())


def _get(env, notification_id):
    return env.core._store.get_notification(notification_id)


def _drain(delivery, event):
    """Release a blocking worker and join it (no busy-poll)."""
    if event is not None:
        event.set()
    if delivery._worker is not None:
        delivery._worker.join(timeout=5)


# ---------------------------------------------------------------------------
# §11.4 — Delivery failure matrix + backoff
# ---------------------------------------------------------------------------

def test_backoff_sequence_and_cap():
    assert backoff_seconds(1) == 5
    assert backoff_seconds(2) == 10
    assert backoff_seconds(3) == 20
    assert backoff_seconds(4) == 40
    assert backoff_seconds(5) == 80
    assert backoff_seconds(7) == 300
    # Retry-After clamp: [5, 300], and it can only extend the default backoff.
    assert backoff_seconds(1, retry_after_seconds=1000) == 300
    assert backoff_seconds(1, retry_after_seconds=2) == 5
    assert backoff_seconds(3, retry_after_seconds=2) == 20
    assert backoff_seconds(1, retry_after_seconds=0) == 5
    assert backoff_seconds(1, retry_after_seconds=None) == 5


def test_delivery_success(db_path):
    env = make_env(db_path)
    row = insert_outbox(env)
    transport = DeterministicNotificationTransport(outcome="success")
    d = make_delivery(env, lambda: transport)
    r = d.send_due_once()
    assert r.claimed is True and r.outcome == "SENT"
    n = _get(env, row["id"])
    assert n["status"] == "SENT" and n["sent_at"] is not None
    assert n["claim_token"] is None and n["attempt_count"] == 1
    assert transport.send_count[row["dedup_key"]] == 1
    assert len(transport.external_sends) == 1


def test_delivery_network_error_retryable(db_path):
    env = make_env(db_path)
    row = insert_outbox(env)
    d = make_delivery(env, lambda: DeterministicNotificationTransport(outcome="network_error"))
    r = d.send_due_once()
    assert r.outcome == "FAILED" and r.error_code == ERROR_NETWORK
    n = _get(env, row["id"])
    assert n["status"] == "FAILED"
    assert n["last_error_code"] == ERROR_NETWORK
    assert n["attempt_count"] == 1
    assert n["next_attempt_at"] == iso(5)  # backoff 5s for attempt 1


def test_delivery_timeout_and_hang_retryable(db_path):
    env = make_env(db_path)
    # "timeout" outcome returns a timeout receipt immediately.
    row = insert_outbox(env, dedup_key="k1")
    d = make_delivery(env, lambda: DeterministicNotificationTransport(outcome="timeout"))
    r = d.send_due_once()
    assert r.outcome == "FAILED" and r.error_code == ERROR_TIMEOUT
    assert _get(env, row["id"])["last_error_code"] == ERROR_TIMEOUT

    # "hang" blocks for timeout_seconds (here an already-set event -> returns
    # instantly, but documents that the worker passes the hard timeout).
    row2 = insert_outbox(env, dedup_key="k2")
    evt = threading.Event(); evt.set()
    transport = DeterministicNotificationTransport(outcome="hang", block_event=evt)
    d2 = make_delivery(env, lambda: transport)
    r2 = d2.send_due_once()
    assert r2.outcome == "FAILED" and r2.error_code == ERROR_TIMEOUT
    assert transport.last_timeout == NOTIFICATION_REQUEST_TIMEOUT_SECONDS
    assert _get(env, row2["id"])["last_error_code"] == ERROR_TIMEOUT


def test_delivery_rate_limit_clamped(db_path):
    env = make_env(db_path)
    row = insert_outbox(env)
    transport = DeterministicNotificationTransport(outcome="rate_limit", retry_after=1000)
    d = make_delivery(env, lambda: transport)
    r = d.send_due_once()
    assert r.outcome == "FAILED" and r.error_code == ERROR_RATE_LIMITED
    assert r.retry_after_seconds == 300  # clamped to 300
    n = _get(env, row["id"])
    assert n["next_attempt_at"] == iso(300)


def test_delivery_rate_limit_lower_clamp(db_path):
    env = make_env(db_path)
    row = insert_outbox(env)
    transport = DeterministicNotificationTransport(outcome="rate_limit", retry_after=2)
    d = make_delivery(env, lambda: transport)
    r = d.send_due_once()
    assert r.retry_after_seconds == 5  # clamped up to 5
    assert _get(env, row["id"])["next_attempt_at"] == iso(5)


def test_delivery_http_5xx_retryable(db_path):
    env = make_env(db_path)
    row = insert_outbox(env)
    d = make_delivery(env, lambda: DeterministicNotificationTransport(outcome="http_5xx"))
    r = d.send_due_once()
    assert r.outcome == "FAILED" and r.error_code == ERROR_HTTP_5XX
    assert _get(env, row["id"])["status"] == "FAILED"


def test_delivery_constructor_failure_retryable(db_path):
    env = make_env(db_path)
    row = insert_outbox(env)

    def factory():
        raise RuntimeError("transient transport init failure")

    d = make_delivery(env, factory)
    r = d.send_due_once()
    # Temporary constructor failure is retryable (SPEC V3A §9.3/§9.4).
    assert r.outcome == "FAILED" and r.error_code == ERROR_TRANSPORT
    n = _get(env, row["id"])
    assert n["status"] == "FAILED" and n["last_error_code"] == ERROR_TRANSPORT
    # No error text is persisted: the RuntimeError message must not leak.
    assert "transient transport init failure" not in json.dumps(n)


def test_delivery_missing_config_nonretryable(db_path):
    env = make_env(db_path)
    row = insert_outbox(env)

    def factory():
        raise NotificationConfigError("telegram disabled")

    d = make_delivery(env, factory)
    r = d.send_due_once()
    assert r.outcome == "DISCARDED" and r.error_code == ERROR_CONFIG
    n = _get(env, row["id"])
    assert n["status"] == "DISCARDED" and n["last_error_code"] == ERROR_CONFIG


def test_delivery_http_4xx_nonretryable(db_path):
    env = make_env(db_path)
    row = insert_outbox(env)
    d = make_delivery(env, lambda: DeterministicNotificationTransport(outcome="http_4xx"))
    r = d.send_due_once()
    assert r.outcome == "DISCARDED" and r.error_code == ERROR_HTTP_4XX
    assert _get(env, row["id"])["status"] == "DISCARDED"


def test_delivery_auth_policy_nonretryable(db_path):
    env = make_env(db_path)
    for outcome, code in (("auth_error", ERROR_AUTH), ("policy_error", ERROR_POLICY)):
        row = insert_outbox(env, dedup_key=f"k-{outcome}")
        d = make_delivery(env, lambda: DeterministicNotificationTransport(outcome=outcome))
        r = d.send_due_once()
        assert r.outcome == "DISCARDED" and r.error_code == code
        assert _get(env, row["id"])["status"] == "DISCARDED"


def test_delivery_hash_mismatch_no_send(db_path):
    env = make_env(db_path)
    row = insert_outbox(env, payload_hash_override="0" * 64)
    transport = DeterministicNotificationTransport(outcome="success")
    d = make_delivery(env, lambda: transport)
    r = d.send_due_once()
    assert r.outcome == "DISCARDED" and r.error_code == ERROR_PAYLOAD_HASH_MISMATCH
    n = _get(env, row["id"])
    assert n["status"] == "DISCARDED"
    assert n["last_error_code"] == ERROR_PAYLOAD_HASH_MISMATCH
    # No send happened at all.
    assert transport.send_count.get(row["dedup_key"], 0) == 0
    assert len(transport.external_sends) == 0


def test_delivery_sqlite_locked_row_unchanged(db_path):
    env = make_env(db_path)
    row = insert_outbox(env)
    blocker = sqlite3.connect(db_path, timeout=0, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")  # hold the write lock
    try:
        d = make_delivery(env, lambda: DeterministicNotificationTransport(outcome="success"))
        r = d.send_due_once()
        assert r.claimed is False and r.outcome == "LOCKED"
        assert r.error_code == ERROR_SQLITE_LOCKED
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    # Row untouched: still PENDING, no attempt consumed.
    n = _get(env, row["id"])
    assert n["status"] == "PENDING" and n["attempt_count"] == 0


def test_delivery_five_attempts_then_discarded(db_path):
    env = make_env(db_path)
    row = insert_outbox(env)
    clock = FakeClock()
    transport = DeterministicNotificationTransport(outcome="network_error")
    d = make_delivery(env, lambda: transport, clock=clock)
    delays = []
    for i in range(NOTIFICATION_MAX_ATTEMPTS):
        r = d.send_due_once()
        if i < NOTIFICATION_MAX_ATTEMPTS - 1:
            assert r.outcome == "FAILED"
            delays.append(r.retry_after_seconds)
            clock.advance(r.retry_after_seconds)
        else:
            assert r.outcome == "DISCARDED"
            assert r.error_code == ERROR_ATTEMPTS_EXHAUSTED
    assert delays == [5, 10, 20, 40]
    n = _get(env, row["id"])
    assert n["status"] == "DISCARDED"
    assert n["attempt_count"] == NOTIFICATION_MAX_ATTEMPTS
    assert n["last_error_code"] == ERROR_ATTEMPTS_EXHAUSTED
    assert transport.send_count[row["dedup_key"]] == NOTIFICATION_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# §11.5 — Crash delivery
# ---------------------------------------------------------------------------

def test_crash_after_insert_pending_no_dup(db_path):
    env = make_env(db_path)
    row = insert_outbox(env)  # "crash" right after insert -> row stays PENDING
    # A second enqueue of the same transition is a silent no-op (dedup).
    dup = dict(row)
    dup["id"] = "notification:dup"
    assert env.core._store._insert_notification(dup) is False
    assert len(rows_for(env)) == 1
    assert rows_for(env)[0]["status"] == "PENDING"


def test_crash_after_claim_sending_until_lease(db_path):
    env = make_env(db_path)
    row = insert_outbox(env)
    clock = FakeClock()
    # Claim the row but "crash" (never complete) via a blocking transport.
    bt = BlockingTransport()
    d = make_delivery(env, lambda: bt, clock=clock)
    t = threading.Thread(target=d.send_due_once)
    t.start()
    assert bt.entered.wait(2)
    n = _get(env, row["id"])
    assert n["status"] == "SENDING" and n["claim_token"] is not None

    # Before lease expiry: not due -> a fresh delivery sends nothing.
    fresh = make_delivery(env, lambda: DeterministicNotificationTransport(outcome="success"), clock=clock)
    assert fresh.send_due_once().outcome == "NOT_DUE"

    # After lease expiry: the SENDING row is reclaimable.
    clock.advance(NOTIFICATION_CLAIM_LEASE_SECONDS + 1)
    transport = DeterministicNotificationTransport(outcome="success")
    reclaim = make_delivery(env, lambda: transport, clock=clock)
    r = reclaim.send_due_once()
    assert r.claimed is True and r.outcome == "SENT"
    assert _get(env, row["id"])["status"] == "SENT"
    bt.release.set()
    t.join(timeout=5)


def test_crash_after_accept_before_sent_idempotent(db_path):
    env = make_env(db_path)
    row = insert_outbox(env)
    clock = FakeClock()

    idem = DeterministicNotificationTransport(outcome="success", idempotent=True)
    d = make_delivery(env, lambda: idem, clock=clock)
    orig_complete = d._complete_sent

    def crash(*a, **k):
        raise RuntimeError("crash before SENT")

    d._complete_sent = crash
    with pytest.raises(RuntimeError):
        d.send_due_once()
    d._complete_sent = orig_complete
    # The idempotent transport transmitted externally exactly once.
    assert len(idem.external_sends) == 1
    assert _get(env, row["id"])["status"] == "SENDING"  # claimed, not completed

    # Lease expires; the SAME idempotent transport sees the retry.
    clock.advance(NOTIFICATION_CLAIM_LEASE_SECONDS + 1)
    r = d.send_due_once()
    assert r.outcome == "SENT"
    # transport-side idempotency suppressed the external duplicate.
    assert idem.send_count[row["dedup_key"]] == 2
    assert len(idem.external_sends) == 1
    assert len(rows_for(env)) == 1  # still exactly one outbox row


def test_crash_after_accept_before_sent_at_least_once(db_path):
    env = make_env(db_path)
    row = insert_outbox(env)
    clock = FakeClock()

    non_idem = DeterministicNotificationTransport(outcome="success")
    d = make_delivery(env, lambda: non_idem, clock=clock)
    orig_complete = d._complete_sent

    def crash(*a, **k):
        raise RuntimeError("crash before SENT")

    d._complete_sent = crash
    with pytest.raises(RuntimeError):
        d.send_due_once()
    d._complete_sent = orig_complete
    assert len(non_idem.external_sends) == 1

    clock.advance(NOTIFICATION_CLAIM_LEASE_SECONDS + 1)
    r = d.send_due_once()
    assert r.outcome == "SENT"
    # Non-idempotent transport re-transmits the SAME payload (at-least-once).
    assert len(non_idem.external_sends) == 2
    first, second = non_idem.external_sends[0], non_idem.external_sends[1]
    assert first.dedup_key == second.dedup_key == row["dedup_key"]
    assert first.payload_hash == second.payload_hash == row["payload_hash"]
    assert first.message_text == second.message_text
    assert len(rows_for(env)) == 1


def test_crash_after_sent_no_resend(db_path):
    env = make_env(db_path)
    row = insert_outbox(env)
    d = make_delivery(env, lambda: DeterministicNotificationTransport(outcome="success"))
    assert d.send_due_once().outcome == "SENT"
    # Restart (new delivery) -> SENT is sticky, never re-sent.
    transport = DeterministicNotificationTransport(outcome="success")
    d2 = make_delivery(env, lambda: transport)
    assert d2.send_due_once().outcome == "NOT_DUE"
    assert transport.send_count.get(row["dedup_key"], 0) == 0
    assert _get(env, row["id"])["status"] == "SENT"


def test_lease_expired_vs_not(db_path):
    env = make_env(db_path)
    row = insert_outbox(env, status="SENDING", claim_token="tok",
                        claimed_at=iso(0), attempt_count=1)
    clock = FakeClock()
    # Not expired (now == claimed_at): not due.
    d = make_delivery(env, lambda: DeterministicNotificationTransport(outcome="success"), clock=clock)
    assert d.send_due_once().outcome == "NOT_DUE"
    # Expired: reclaimable.
    clock.advance(NOTIFICATION_CLAIM_LEASE_SECONDS + 1)
    transport = DeterministicNotificationTransport(outcome="success")
    d2 = make_delivery(env, lambda: transport, clock=clock)
    r = d2.send_due_once()
    assert r.claimed is True and r.outcome == "SENT"
    n = _get(env, row["id"])
    assert n["status"] == "SENT" and n["attempt_count"] == 2


# ---------------------------------------------------------------------------
# §11.6 — Restart flood
# ---------------------------------------------------------------------------

def test_restart_flood_sent_and_discarded_zero_sends(db_path):
    env = make_env(db_path)
    for i in range(100):
        insert_outbox(env, dedup_key=f"s{i:03d}", status="SENT",
                      sent_at=iso(0), created_at=iso(i))
    for i in range(100):
        insert_outbox(env, dedup_key=f"d{i:03d}", status="DISCARDED",
                      last_error_code=ERROR_HTTP_4XX, created_at=iso(i + 1000))
    transport = DeterministicNotificationTransport(outcome="success")
    d = make_delivery(env, lambda: transport)
    r = d.send_due_once()
    assert r.claimed is False and r.outcome == "NOT_DUE"
    assert transport.send_count == {} and transport.external_sends == []


def test_restart_flood_non_due_failed_zero(db_path):
    env = make_env(db_path)
    for i in range(10):
        insert_outbox(env, dedup_key=f"f{i:03d}", status="FAILED",
                      attempt_count=1, next_attempt_at=iso(3600),
                      last_error_code=ERROR_NETWORK)
    transport = DeterministicNotificationTransport(outcome="success")
    d = make_delivery(env, lambda: transport)
    assert d.send_due_once().outcome == "NOT_DUE"
    assert transport.send_count == {}


def test_restart_flood_multiple_due_max_one_per_kick(db_path):
    env = make_env(db_path)
    rows = []
    for i in range(3):
        rows.append(insert_outbox(env, dedup_key=f"due{i:03d}", created_at=iso(i)))
    transport = DeterministicNotificationTransport(outcome="success")
    d = make_delivery(env, lambda: transport)
    r = d.send_due_once()
    assert r.claimed is True and r.outcome == "SENT"
    # Exactly ONE row per kick; the OLDEST (created_at smallest) is chosen.
    assert sum(transport.send_count.values()) == 1
    assert transport.send_count[rows[0]["dedup_key"]] == 1
    assert _get(env, rows[0]["id"])["status"] == "SENT"
    assert _get(env, rows[1]["id"])["status"] == "PENDING"


def test_terminal_jobs_zero_new_rows(db_path, tmp_path):
    env = make_env(db_path, workspace=make_workspace(tmp_path),
                   run_tests_fn=fake_run_tests)
    drive_to_done(env)
    advance(env, ReconcileAction.CLOSE_DONE, max_steps=3)
    assert len(rows_for(env)) == 1
    for _ in range(5):
        assert step(env).action is ReconcileAction.NONE
    assert len(rows_for(env)) == 1


def test_upgrade_historical_zero_backfill_sends(db_path):
    env = make_env(db_path)
    # Simulate historical (already-terminal) data: drop the outbox table and
    # stamp V4, then reopen -> migration recreates an EMPTY table (no backfill).
    conn = env.core._store._conn
    conn.execute("DROP TABLE notification_outbox")
    conn.execute("UPDATE schema_meta SET value='4' WHERE key='schema_version'")
    env.core.close()
    core2 = Core(db_path)
    try:
        assert core2._store.list_notifications() == []
    finally:
        core2.close()
    transport = DeterministicNotificationTransport(outcome="success")
    d = make_delivery(env, lambda: transport)
    # Nothing due -> no backfill sends.
    assert d.send_due_once().outcome == "NOT_DUE"
    assert transport.send_count == {}


# ---------------------------------------------------------------------------
# §11.7 — Non-blocking loop kick
# ---------------------------------------------------------------------------

def _loop_env(db_path):
    return make_env(db_path, workspace=make_workspace(Path(db_path).parent),
                   run_tests_fn=fake_run_tests)


def test_nonblocking_loop_reaches_done(db_path, tmp_path):
    env = make_env(db_path, workspace=make_workspace(tmp_path),
                   run_tests_fn=fake_run_tests)
    # Drive the workflow to task DONE with the plain supervisor (no loop -> no
    # kick), then a single loop.run_once performs the CLOSE_DONE transition
    # (enqueue) and kicks the delivery worker.  The worker blocks in send() but
    # the loop call returns immediately.
    drive_to_done(env)
    bt = BlockingTransport()
    delivery = NotificationDelivery(env.db_path, lambda: bt, clock=FakeClock())
    loop = SupervisorLoop(env.sup, notification_delivery=delivery)
    decision = loop.run_once(env.job.supervisor_job_id)
    assert decision.action is ReconcileAction.CLOSE_DONE
    # The worker claimed the DONE row and is stuck in send(); the loop did not
    # block on it.
    assert bt.entered.wait(2)
    assert delivery.worker_running is True
    rows = rows_for(env)
    assert len(rows) == 1
    assert rows[0]["status"] == "SENDING"
    job = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    assert job["terminal"] == "DONE"
    assert job["next_action"] == "NONE"
    assert "telegram" not in json.dumps(job).lower()
    _drain(delivery, bt.release)
    assert rows_for(env)[0]["status"] == "SENT"


def test_nonblocking_loop_reaches_failed(db_path):
    env = make_env(db_path)
    set_task_state(env, TaskState.FAILED)
    bt = BlockingTransport()
    delivery = NotificationDelivery(env.db_path, lambda: bt, clock=FakeClock())
    loop = SupervisorLoop(env.sup, notification_delivery=delivery)
    decision = loop.run_once(env.job.supervisor_job_id)
    assert decision.action is ReconcileAction.CLOSE_FAILED
    job = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    assert job["terminal"] == "FAILED"
    assert bt.entered.wait(2)
    assert delivery.worker_running is True
    _drain(delivery, bt.release)
    assert rows_for(env)[0]["status"] == "SENT"


def test_nonblocking_loop_reaches_blocked(db_path):
    env = make_env(db_path)
    set_task_state(env, TaskState.BLOCKED)
    bt = BlockingTransport()
    delivery = NotificationDelivery(env.db_path, lambda: bt, clock=FakeClock())
    loop = SupervisorLoop(env.sup, notification_delivery=delivery)
    decision = loop.run_once(env.job.supervisor_job_id)
    assert decision.action is ReconcileAction.CLOSE_BLOCKED
    job = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    assert job["terminal"] == "BLOCKED"
    assert bt.entered.wait(2)
    assert delivery.worker_running is True
    _drain(delivery, bt.release)
    assert rows_for(env)[0]["status"] == "SENT"


def test_nonblocking_loop_reaches_waiting_gate(db_path):
    env = make_env(db_path)
    env.core.start_role(env.task.id, Role.LEAD, LEAD)
    env.core.request_action(env.task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    bt = BlockingTransport()
    delivery = NotificationDelivery(env.db_path, lambda: bt, clock=FakeClock())
    loop = SupervisorLoop(env.sup, notification_delivery=delivery)
    decision = loop.run_once(env.job.supervisor_job_id)
    assert decision.action is ReconcileAction.PRESENT_OWNER_GATE
    job = env.core._store.get_supervisor_job(env.job.supervisor_job_id)
    assert job["status"] == "WAITING_GATE"
    assert job["gate_status"] == "pending"
    assert "telegram" not in json.dumps(job).lower()
    assert bt.entered.wait(2)
    assert delivery.worker_running is True
    rows = rows_for(env)
    assert len(rows) == 1
    assert rows[0]["notification_type"] == "OWNER_APPROVAL_REQUIRED"
    _drain(delivery, bt.release)
    assert rows_for(env)[0]["status"] == "SENT"


def test_kick_at_most_one_worker(db_path):
    env = make_env(db_path)
    row = insert_outbox(env)
    bt = BlockingTransport()
    delivery = NotificationDelivery(env.db_path, lambda: bt, clock=FakeClock())
    for _ in range(10):
        delivery.kick()
    # The single worker enters send() and blocks; no second worker is started.
    assert bt.entered.wait(2)
    assert delivery.worker_running is True
    before = delivery._worker
    delivery.kick()  # a blocked worker still running -> this kick is a no-op
    assert delivery._worker is before
    _drain(delivery, bt.release)
    assert _get(env, row["id"])["status"] == "SENT"


def test_kick_never_propagates(db_path):
    env = make_env(db_path)
    insert_outbox(env)
    # A factory that always raises: kick() must swallow it (catch-all).
    def factory():
        raise RuntimeError("boom")

    delivery = NotificationDelivery(env.db_path, factory, clock=FakeClock())
    delivery.kick()  # must not raise
    # The worker ran, caught the error, and the row is still PENDING (no claim
    # could complete -> actually the claim DID happen; the factory raise maps
    # to a retryable FAILED, so the row is FAILED, not PENDING).
    delivery._worker.join(timeout=5)
    n = _get(env, rows_for(env)[0]["id"])
    assert n["status"] == "FAILED"
    assert n["last_error_code"] == ERROR_TRANSPORT


# ---------------------------------------------------------------------------
# §11.8 — Concurrent senders
# ---------------------------------------------------------------------------

def test_concurrent_exactly_one_claim_at_most_one_send(db_path):
    env = make_env(db_path)
    row = insert_outbox(env)
    bt = BlockingTransport()
    d1 = make_delivery(env, lambda: bt)
    t = threading.Thread(target=d1.send_due_once)
    t.start()
    assert bt.entered.wait(2)
    # Second instance: the row is SENDING (not expired) -> no due row.
    transport2 = DeterministicNotificationTransport(outcome="success")
    d2 = make_delivery(env, lambda: transport2)
    r2 = d2.send_due_once()
    assert r2.claimed is False and r2.outcome == "NOT_DUE"
    assert len(bt.sends) == 1 and transport2.send_count == {}
    bt.release.set()
    t.join(timeout=5)
    assert _get(env, row["id"])["status"] == "SENT"


def test_concurrent_stale_completion_loses_cas(db_path):
    env = make_env(db_path)
    row = insert_outbox(env)
    bt = BlockingTransport()
    d1 = make_delivery(env, lambda: bt)
    t = threading.Thread(target=d1.send_due_once)
    t.start()
    assert bt.entered.wait(2)
    n = _get(env, row["id"])
    assert n["status"] == "SENDING" and n["claim_token"] is not None
    token = n["claim_token"]
    # A stale completion with the WRONG token must lose the CAS.
    raw = sqlite3.connect(db_path, timeout=0, isolation_level=None)
    try:
        cur = raw.execute(
            "UPDATE notification_outbox SET status='SENT', sent_at=?, "
            "claim_token=NULL, claimed_at=NULL, next_attempt_at=NULL "
            "WHERE id=? AND status='SENDING' AND claim_token='stale-token'",
            (iso(1), row["id"]),
        )
        assert cur.rowcount == 0
    finally:
        raw.close()
    assert _get(env, row["id"])["status"] == "SENDING"
    assert _get(env, row["id"])["claim_token"] == token
    bt.release.set()
    t.join(timeout=5)
    assert _get(env, row["id"])["status"] == "SENT"


def test_concurrent_lease_reclaimable_no_lost_attempt(db_path):
    env = make_env(db_path)
    row = insert_outbox(env)
    clock = FakeClock()
    bt = BlockingTransport()
    d1 = make_delivery(env, lambda: bt, clock=clock)
    t = threading.Thread(target=d1.send_due_once)
    t.start()
    assert bt.entered.wait(2)
    # d1 claimed (attempt 1) and is blocked; lease expires.
    clock.advance(NOTIFICATION_CLAIM_LEASE_SECONDS + 1)
    transport2 = DeterministicNotificationTransport(outcome="success")
    d2 = make_delivery(env, lambda: transport2, clock=clock)
    r2 = d2.send_due_once()
    assert r2.claimed is True and r2.outcome == "SENT"
    assert len(transport2.external_sends) == 1
    # Now d1 finishes; its stale completion must lose the CAS (no double-send).
    bt.release.set()
    t.join(timeout=5)
    n = _get(env, row["id"])
    assert n["status"] == "SENT"
    assert n["attempt_count"] == 2  # d1's claim + d2's reclaim; no lost attempt
    assert len(rows_for(env)) == 1


# ---------------------------------------------------------------------------
# §11.9 — No secrets
# ---------------------------------------------------------------------------

def test_no_secrets_canary_not_in_outbox_message_or_sqlite(db_path, tmp_path):
    CANARY = "TOPSECRET-CANARY-8f3a"
    TOKEN = "123456:DUMMY-BOT-TOKEN"
    CHAT = "-1009999999"
    env = make_env(db_path, workspace=make_workspace(tmp_path),
                   run_tests_fn=fake_run_tests)
    # Inject canaries into the task prose.
    env.core._store._conn.execute(
        "UPDATE tasks SET title=?, description=? WHERE id=?",
        (CANARY + "-title", CANARY + "-desc", env.task.id),
    )
    drive_to_done(env)
    advance(env, ReconcileAction.CLOSE_DONE, max_steps=3)
    rows = rows_for(env)
    assert len(rows) == 1
    # Deliver via a transport that raises a canary exception AND a remote error.
    class LeakyTransport:
        def send(self, envelope, *, timeout_seconds):
            raise RuntimeError(CANARY + "-transport-exception")

    d = make_delivery(env, lambda: LeakyTransport())
    r = d.send_due_once()
    assert r.outcome == "FAILED"  # transport exception -> retryable, no text leak

    n = rows_for(env)[0]
    payload = json.loads(n["payload_json"])
    # Canaries must NOT be in the payload or the fixed message text.
    assert CANARY not in n["payload_json"]
    assert CANARY not in json.dumps(n)
    msg = render_message(
        NotificationType(n["notification_type"]),
        supervisor_job_id=payload["supervisor_job_id"],
        task_id=payload["task_id"], event_at=payload["event_at"],
        reason_code=payload["reason_code"], dedup_key=n["dedup_key"],
    )
    assert CANARY not in msg
    assert "TOPSECRET" not in msg
    # The message is exactly the fixed DONE template (no free prose).
    expected = (
        "ARGENT · DONE\n"
        f"Job: {payload['supervisor_job_id']}\n"
        f"Task: {payload['task_id']}\n"
        f"Time: {payload['event_at']}\n"
        f"Ref: {n['dedup_key'][:16]}"
    )
    assert msg == expected

    # Credentials/target never reach SQLite (dummy values used only here).
    raw_bytes = Path(db_path).read_bytes()
    assert TOKEN.encode() not in raw_bytes
    assert CHAT.encode() not in raw_bytes
    # The raw gate scope (a canary) is never stored: only a sha256: prefix.
    # (No gate here; verify the scope_ref helper directly.)


def test_raw_scope_never_in_payload(db_path):
    env = make_env(db_path)
    raw_scope = "prod/TOPSECRET-SCOPE"
    env.core.start_role(env.task.id, Role.LEAD, LEAD)
    ar = env.core.request_action(
        env.task.id, "deploy_production", raw_scope, Role.LEAD, LEAD)
    step(env)
    rows = rows_for(env)
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert "scope_ref" in payload
    assert payload["scope_ref"] == scope_ref(ar.approval.binding_hash)
    assert raw_scope not in rows[0]["payload_json"]
    assert "TOPSECRET-SCOPE" not in rows[0]["payload_json"]


# ---------------------------------------------------------------------------
# §11.10 — Owner gate delivery semantics
# ---------------------------------------------------------------------------

def test_owner_gate_pending_one_informational_row(db_path):
    env = make_env(db_path)
    env.core.start_role(env.task.id, Role.LEAD, LEAD)
    ar = env.core.request_action(
        env.task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    decision = step(env)
    assert decision.action is ReconcileAction.PRESENT_OWNER_GATE
    rows = rows_for(env)
    assert len(rows) == 1
    assert rows[0]["notification_type"] == "OWNER_APPROVAL_REQUIRED"

    # Delivery (success) changes nothing about the gate.
    transport = DeterministicNotificationTransport(outcome="success")
    d = make_delivery(env, lambda: transport)
    r = d.send_due_once()
    assert r.outcome == "SENT"
    ap = env.core.queries.get_approval(ar.approval.id)
    assert ap.status.value == "pending"
    assert ap.closed_at is None
    # Informational only: no owner source is embedded in the message.
    payload = json.loads(rows_for(env)[0]["payload_json"])
    msg = render_message(
        NotificationType(payload["notification_type"]),
        supervisor_job_id=payload["supervisor_job_id"],
        task_id=payload["task_id"], event_at=payload["event_at"],
        reason_code=payload["reason_code"], dedup_key=rows_for(env)[0]["dedup_key"],
        gate_id=payload["gate_id"], scope_ref=payload["scope_ref"],
    )
    assert "owner:" not in msg
    assert "Informational only. Use the authenticated owner-control path." in msg


def test_owner_gate_outage_does_not_block_gate(db_path):
    env = make_env(db_path)
    env.core.start_role(env.task.id, Role.LEAD, LEAD)
    ar = env.core.request_action(
        env.task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    step(env)
    # Delivery fails (network error): the gate remains pending and usable.
    d = make_delivery(env, lambda: DeterministicNotificationTransport(outcome="network_error"))
    assert d.send_due_once().outcome == "FAILED"
    ap = env.core.queries.get_approval(ar.approval.id)
    assert ap.status.value == "pending"
    # The owner can still approve + execute through the authenticated path.
    env.core.approve(ap.id, OWNER, task_id=env.task.id,
                     action="deploy_production", scope="prod")
    env.core.execute_approved(ap.id, OWNER, task_id=env.task.id,
                              action="deploy_production", scope="prod")
    assert env.core.queries.get_approval(ap.id).status.value == "consumed"


def test_owner_gate_new_gate_separate_and_closed_not_renotified(db_path):
    env = make_env(db_path)
    env.core.start_role(env.task.id, Role.LEAD, LEAD)
    ar1 = env.core.request_action(
        env.task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    step(env)
    assert len(rows_for(env)) == 1

    # Approve + execute gate 1 (closes it), then request a NEW gate.
    env.core.approve(ar1.approval.id, OWNER, task_id=env.task.id,
                     action="deploy_production", scope="prod")
    env.core.execute_approved(ar1.approval.id, OWNER, task_id=env.task.id,
                              action="deploy_production", scope="prod")
    ar2 = env.core.request_action(
        env.task.id, "change_secrets", "prod", Role.LEAD, LEAD)
    decision = step(env)
    assert decision.action is ReconcileAction.PRESENT_OWNER_GATE
    rows = rows_for(env)
    assert len(rows) == 2
    gate_ids = {r["gate_id"] for r in rows}
    assert gate_ids == {ar1.approval.id, ar2.approval.id}

    # "Restart": a new Core/Supervisor over the same DB must NOT re-notify the
    # still-pending gate 2, and the closed gate 1 is never re-presented.
    env.core.close()
    core2 = Core(db_path)
    prov2 = FakeRunStatusProvider()
    sup2 = Supervisor(core2, prov2, FakeRunLauncher())
    d2 = sup2.reconcile(env.job.supervisor_job_id)
    assert d2.action is not ReconcileAction.PRESENT_OWNER_GATE
    assert len(core2._store.list_notifications(env.job.supervisor_job_id)) == 2
    core2.close()


# ---------------------------------------------------------------------------
# Telegram adapter (offline, injected HTTP)
# ---------------------------------------------------------------------------

def _telegram(bot_token="123456:DUMMY", chat_id="999999", **kw):
    return TelegramNotificationTransport(bot_token, chat_id, **kw)


def _envelope():
    return NotificationEnvelope(
        outbox_id="notification:x", dedup_key="d" * 64, payload_hash="p" * 64,
        notification_type=NotificationType.DONE, message_text="ARGENT · DONE\n...",
    )


def test_telegram_missing_config_raises():
    with pytest.raises(NotificationConfigError):
        TelegramNotificationTransport("", "123")
    with pytest.raises(NotificationConfigError):
        TelegramNotificationTransport("123", "")


def test_telegram_success_and_timeout():
    env = _envelope()

    def _raise(exc):
        raise exc

    # Success: injected request_fn returns HTTP 200 ok:true.
    t = _telegram(request_fn=lambda url, body, timeout: (200, '{"ok": true}'))
    assert t.send(env, timeout_seconds=5) == TransportReceipt(True, False)
    # Timeout: injected request_fn raises socket.timeout.
    t2 = _telegram(request_fn=lambda url, body, timeout: _raise(socket.timeout()))
    assert t2.send(env, timeout_seconds=5) == TransportReceipt(False, True, ERROR_TIMEOUT)
    # Network error: injected request_fn raises a generic OSError.
    t3 = _telegram(request_fn=lambda url, body, timeout: _raise(OSError("conn refused")))
    assert t3.send(env, timeout_seconds=5) == TransportReceipt(False, True, ERROR_NETWORK)


def test_telegram_rate_limit_retry_after():
    env = _envelope()
    t = _telegram(request_fn=lambda url, body, timeout: (
        429, '{"ok": false, "error_code": 429, "parameters": {"retry_after": 17}}'))
    r = t.send(env, timeout_seconds=5)
    assert r == TransportReceipt(False, True, ERROR_RATE_LIMITED, 17)


def test_telegram_5xx_retryable_and_4xx_auth_nonretryable():
    env = _envelope()
    t5 = _telegram(request_fn=lambda url, body, timeout: (502, '{}'))
    assert t5.send(env, timeout_seconds=5) == TransportReceipt(False, True, ERROR_HTTP_5XX)
    t4 = _telegram(request_fn=lambda url, body, timeout: (400, '{"ok": false}'))
    assert t4.send(env, timeout_seconds=5) == TransportReceipt(False, False, ERROR_HTTP_4XX)
    tauth = _telegram(request_fn=lambda url, body, timeout: (403, '{"ok": false}'))
    assert tauth.send(env, timeout_seconds=5) == TransportReceipt(False, False, ERROR_AUTH)
    # 200 ok:false with error_code 401 -> auth.
    t401 = _telegram(request_fn=lambda url, body, timeout: (200, '{"ok": false, "error_code": 401}'))
    assert t401.send(env, timeout_seconds=5) == TransportReceipt(False, False, ERROR_AUTH)


def test_telegram_transport_no_inbound_method():
    t = _telegram()
    assert not hasattr(t, "getUpdates")
    assert not hasattr(t, "setWebhook")
    assert not hasattr(t, "receive")
    assert not hasattr(t, "poll")


def test_telegram_factory_disabled_when_no_config():
    class EmptySource:
        def telegram_bot_token(self):
            return None

        def telegram_chat_id(self):
            return None

    factory = telegram_transport_factory(EmptySource())
    with pytest.raises(NotificationConfigError):
        factory()


def test_telegram_factory_provides_credentials_only_via_source():
    class Source:
        def telegram_bot_token(self):
            return "123456:DUMMY"

        def telegram_chat_id(self):
            return "999999"

    factory = telegram_transport_factory(Source())
    t = factory()
    assert isinstance(t, TelegramNotificationTransport)
    # Credentials are held in-memory only (never persisted/logged here).


# ---------------------------------------------------------------------------
# Fix Round C — F1: attempt ceiling is absolute (never exceeded via lease
# reclaim), F2: sticky-ERROR loop kick, F4: malformed retry_after never raises.
# ---------------------------------------------------------------------------

def test_expired_sending_at_ceiling_discarded_not_sent(db_path):
    """F1: an expired SENDING row already at attempt_count == MAX_ATTEMPTS is
    terminally DISCARDED (ATTEMPTS_EXHAUSTED) — never claimed/sent again and
    never incremented to 6."""
    env = make_env(db_path)
    clock = FakeClock()
    clock.advance(NOTIFICATION_CLAIM_LEASE_SECONDS + 1)
    row = insert_outbox(env, status="SENDING", claim_token="tok",
                        claimed_at=iso(0),
                        attempt_count=NOTIFICATION_MAX_ATTEMPTS)
    transport = DeterministicNotificationTransport(outcome="success")
    d = make_delivery(env, lambda: transport, clock=clock)
    r = d.send_due_once()
    assert r.claimed is True
    assert r.outcome == "DISCARDED"
    assert r.error_code == ERROR_ATTEMPTS_EXHAUSTED
    n = _get(env, row["id"])
    assert n["status"] == "DISCARDED"
    assert n["attempt_count"] == NOTIFICATION_MAX_ATTEMPTS  # never incremented
    assert n["last_error_code"] == ERROR_ATTEMPTS_EXHAUSTED
    # broker/transport NOT invoked.
    assert transport.send_count.get(row["dedup_key"], 0) == 0
    assert transport.external_sends == []
    assert len(rows_for(env)) == 1
    # A further pass must not re-claim or re-send the discarded row.
    assert d.send_due_once().outcome == "NOT_DUE"
    assert transport.send_count.get(row["dedup_key"], 0) == 0


def test_repeated_lease_reclaim_never_exceeds_max_attempts(db_path):
    """F1: repeated claim -> crash -> lease-expiry cycles never push
    attempt_count past NOTIFICATION_MAX_ATTEMPTS and never send after the
    fifth attempt; the ceiling reclaim is a terminal discard."""
    env = make_env(db_path)
    row = insert_outbox(env)
    clock = FakeClock()
    transport = DeterministicNotificationTransport(outcome="success")
    d = make_delivery(env, lambda: transport, clock=clock)
    # Keep the clock past the claim-lease cutoff so every stamped claimed_at
    # (iso(0)) is lease-expired.
    clock.advance(NOTIFICATION_CLAIM_LEASE_SECONDS + 1)
    outcomes = []
    for claim_no in range(1, NOTIFICATION_MAX_ATTEMPTS + 1):
        # Simulate a worker that claimed the row (attempt `claim_no`) then
        # crashed before completing: the row is left SENDING with an expired
        # lease.
        raw = sqlite3.connect(db_path, timeout=0, isolation_level=None)
        raw.execute(
            "UPDATE notification_outbox SET status='SENDING', claim_token=?, "
            "claimed_at=?, last_attempt_at=?, attempt_count=?, "
            "next_attempt_at=NULL, last_error_code=NULL WHERE id=?",
            (f"tok{claim_no}", iso(0), iso(0), claim_no, row["id"]),
        )
        raw.close()
        r = d.send_due_once()
        outcomes.append(r.outcome)
        n = _get(env, row["id"])
        # The attempt ceiling is absolute across cycles.
        assert n["attempt_count"] <= NOTIFICATION_MAX_ATTEMPTS, \
            (claim_no, n["attempt_count"])
    assert outcomes == ["SENT", "SENT", "SENT", "SENT", "DISCARDED"]
    n = _get(env, row["id"])
    assert n["status"] == "DISCARDED"
    assert n["attempt_count"] == NOTIFICATION_MAX_ATTEMPTS
    # Only the 4 sub-ceiling reclaims actually transmitted; the ceiling
    # reclaim never reached the transport.
    assert transport.send_count.get(row["dedup_key"], 0) == \
        NOTIFICATION_MAX_ATTEMPTS - 1


def test_loop_run_until_terminal_sticky_error_kicks_and_returns(db_path):
    """F2: a sticky ERROR job returns immediately from run_until_terminal after
    one final delivery kick — run_once is not re-entered and the loop does not
    spin."""
    env = make_env(db_path)
    env.sup._persist_error(env.job.supervisor_job_id, "bounded_sticky_error")
    state = env.sup.store.get_job(env.job.supervisor_job_id)
    assert state.status == SupervisorJobStatus.ERROR.value
    assert state.terminal is None

    class SpyDelivery:
        def __init__(self):
            self.kicks = 0

        def kick(self):
            self.kicks += 1

    spy = SpyDelivery()
    loop = SupervisorLoop(env.sup, notification_delivery=spy)
    calls = {"n": 0}
    orig_run_once = loop.run_once

    def counting_run_once(job_id):
        calls["n"] += 1
        return orig_run_once(job_id)

    loop.run_once = counting_run_once
    result = loop.run_until_terminal(env.job.supervisor_job_id)
    assert result is not None
    assert result.status == SupervisorJobStatus.ERROR.value
    assert spy.kicks == 1  # exactly one final delivery kick
    assert calls["n"] == 0  # run_once never re-entered for sticky ERROR


def test_loop_run_until_terminal_invokes_run_once_for_non_error(db_path):
    """F2 (unchanged behavior): a non-sticky-ERROR job still enters run_once."""
    env = make_env(db_path)
    set_task_state(env, TaskState.FAILED)
    loop = SupervisorLoop(env.sup)
    calls = {"n": 0}
    orig_run_once = loop.run_once

    def counting_run_once(job_id):
        calls["n"] += 1
        return orig_run_once(job_id)

    loop.run_once = counting_run_once
    state = loop.run_until_terminal(env.job.supervisor_job_id)
    assert state.terminal == "FAILED"
    assert calls["n"] >= 1


def test_telegram_malformed_retry_after_nonfinite_no_raise():
    """F4: non-finite retry_after (NaN/inf/-inf/1e400) must never raise and
    must map to a safe retry_after (None)."""
    env = _envelope()
    bodies = [
        '{"ok": false, "error_code": 429, "parameters": {"retry_after": NaN}}',
        '{"ok": false, "error_code": 429, "parameters": {"retry_after": Infinity}}',
        '{"ok": false, "error_code": 429, "parameters": {"retry_after": -Infinity}}',
        '{"ok": false, "error_code": 429, "parameters": {"retry_after": 1e400}}',
    ]
    for body in bodies:
        t = _telegram(request_fn=lambda url, b, to, _body=body: (429, _body))
        r = t.send(env, timeout_seconds=5)
        assert isinstance(r, TransportReceipt)
        assert r.error_code == ERROR_RATE_LIMITED
        assert r.retry_after_seconds is None


def test_telegram_malformed_retry_after_scalar_no_raise():
    """F4: string/null/negative retry_after -> None; a huge finite float is
    converted to a finite int (bounded downstream by backoff_seconds), never
    an exception."""
    env = _envelope()
    cases = [
        ('"17"', None),
        ("null", None),
        ("-5", None),
        ("true", None),
    ]
    for ra_json, expected in cases:
        body = '{"ok": false, "error_code": 429, '
        body += '"parameters": {"retry_after": %s}}' % ra_json
        t = _telegram(request_fn=lambda url, b, to, _body=body: (429, _body))
        r = t.send(env, timeout_seconds=5)
        assert isinstance(r, TransportReceipt)
        assert r.error_code == ERROR_RATE_LIMITED
        assert r.retry_after_seconds == expected
    # Huge but finite float -> finite int, no exception.
    body = '{"ok": false, "error_code": 429, '
    body += '"parameters": {"retry_after": 1e300}}'
    t = _telegram(request_fn=lambda url, b, to, _body=body: (429, _body))
    r = t.send(env, timeout_seconds=5)
    assert isinstance(r, TransportReceipt)
    assert r.error_code == ERROR_RATE_LIMITED
    assert isinstance(r.retry_after_seconds, int)
    assert r.retry_after_seconds > 0


def test_telegram_malformed_retry_after_200_code_429_no_raise():
    """F4: a 200 response with error_code 429 and a malformed retry_after must
    also never raise (the second RATE_LIMITED classification path)."""
    env = _envelope()
    bodies = [
        '{"ok": false, "error_code": 429, "parameters": {"retry_after": Infinity}}',
        '{"ok": false, "error_code": 429, "parameters": {"retry_after": NaN}}',
    ]
    for body in bodies:
        t = _telegram(request_fn=lambda url, b, to, _body=body: (200, _body))
        r = t.send(env, timeout_seconds=5)
        assert isinstance(r, TransportReceipt)
        assert r.error_code == ERROR_RATE_LIMITED
        assert r.retry_after_seconds is None
