"""Phase 3C-B1 — OpenClaw Telegram approval adapter tests (SPEC V3C §4/§5/
§6.2/§6.3/§10/§11/§14/§15, owner amendments A1/A3/A6/A7).

Offline and deterministic: temp DB, fake clock, no network, no real Telegram,
no agents.  Exercises the transport-neutral :class:`TelegramApprovalAdapter`
against the VERIFIED Phase 3C-A :class:`ApprovalProcessor`: structured-callback
grammar (fail-closed before the core), exact field mapping, total (never-raise)
delivery, exactly-once concurrency, post-decision UX (A6), and the no-secrets /
no-network audit invariants.

Dummy identities only: ``OWNER_USER_ID`` / ``OWNER_CHAT_ID`` are obviously fake
and — per A3/§5 (single canonical owner, F3) — deliberately the SAME string.
"""

import ast
import inspect
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from argent_core import (
    OWNER_SOURCE,
    ApprovalStatus,
    Core,
    Role,
    TaskState,
    role_source,
)
from argent_core.approval_core import (
    CallbackAction,
    ChallengeStatus,
    create_challenge,
    token_hash,
)
from argent_core.approval_processor import ApprovalProcessor, CallbackOutcome
from argent_core.telegram_approval_adapter import (
    CALLBACK_NAMESPACE,
    AdapterOutcome,
    FakePostDecisionUx,
    TelegramApprovalAdapter,
    dispatch_callback,
    split_namespace,
)

LEAD = role_source(Role.LEAD)
OWNER_USER_ID = "owner-42"
OWNER_CHAT_ID = "owner-42"
REF = "cbq:ref-1"
MESSAGE_REF = "msg:ref-1"

MSG_DATE = int(datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp())


def _unix_from_iso(iso: str) -> int:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


class Clock:
    def __init__(self, start=None):
        self.t = start or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


class StaticIdentity:
    def __init__(self, user_id, chat_id):
        self._user = user_id
        self._chat = chat_id

    def expected_owner_user_id(self):
        return self._user

    def expected_owner_chat_id(self):
        return self._chat


def _job_row(job_id, task_id, now):
    return {
        "id": job_id,
        "task_id": task_id,
        "status": "WAITING_GATE",
        "workflow_state": "gated",
        "expected_role": None,
        "expected_dispatch_id": None,
        "agent_id": None,
        "session_id": None,
        "run_id": None,
        "attempt_no": 0,
        "dispatch_status": None,
        "result_status": "NOT_OBSERVED",
        "result_consumed": 0,
        "current_handoff_id": None,
        "open_findings_count": 0,
        "rework_cycle": 1,
        "recovery_state": "NONE",
        "owner_gate_id": None,
        "gate_status": None,
        "gate_scope": None,
        "gate_closed": 0,
        "owner_prompted_at": None,
        "owner_prompted_gate_id": None,
        "next_action": "NONE",
        "next_wake_at": None,
        "retry_count": 0,
        "missing_confirmations": 0,
        "last_error_code": None,
        "last_progress_at": now,
        "terminal": None,
        "facts_version": 0,
        "primary_state": "OWNER_GATE",
        "queue_reason": "NEW",
        "priority": 0,
        "owner_instance_id": None,
        "lease_epoch": 0,
        "lease_expires_at": None,
        "next_eligible_at": None,
        "error_class": "NONE",
        "wait_kind": "NONE",
        "created_at": now,
        "updated_at": now,
    }


def _setup_env(db_path):
    clock = Clock()
    core = Core(db_path, clock=clock)
    project = core.create_project("demo", OWNER_SOURCE)
    task = core.create_task(project.id, "demo-task", OWNER_SOURCE)
    core.start_role(task.id, Role.LEAD, LEAD)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    approval = res.approval
    job_id = "job-1"
    core._store._insert_supervisor_job(_job_row(job_id, task.id, core._store.now_iso()))
    return SimpleNamespace(core=core, task=task, approval=approval, job_id=job_id,
                           clock=clock, db_path=db_path)


@pytest.fixture
def env(tmp_path):
    e = _setup_env(str(tmp_path / "env.db"))
    yield e
    e.core.close()


@pytest.fixture
def chal(env):
    challenge, raw = create_challenge(env.core._store, approval=env.approval,
                                      supervisor_job_id=env.job_id, now=env.clock.t)
    return challenge, raw


def _processor(env, ux=None):
    return ApprovalProcessor(
        env.core, identity_source=StaticIdentity(OWNER_USER_ID, OWNER_CHAT_ID), ux=ux
    )


def _adapter(env, ux=None):
    return TelegramApprovalAdapter(_processor(env), ux=ux)


def _call(adapter, *, payload, update_id=1, message_date=MSG_DATE,
          chat=OWNER_CHAT_ID, user=OWNER_USER_ID, callback_ref=REF,
          message_ref=MESSAGE_REF):
    return adapter.handle_callback(
        payload=payload, sender_identity=user, private_chat_identity=chat,
        update_id=update_id, callback_ref=callback_ref, message_ref=message_ref,
        message_date=message_date,
    )


class SpyProcessor:
    """Wraps the real processor to record exactly what the adapter forwards."""

    def __init__(self, real):
        self._real = real
        self.calls = []

    def process_callback(self, **kwargs):
        self.calls.append(kwargs)
        return self._real.process_callback(**kwargs)


class RaisingProcessor:
    """A processor that always raises — simulates a core/host failure."""

    def process_callback(self, **kwargs):
        raise RuntimeError("injected core explosion")


def _thread_safe_cores(db_path, clock):
    core_a = Core(db_path, clock=clock)
    core_b = Core(db_path, clock=clock)
    for core in (core_a, core_b):
        core._store._conn.close()
        conn = sqlite3.connect(db_path, isolation_level=None,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        core._store._conn = conn
    return core_a, core_b


def _adapter_for(core):
    return TelegramApprovalAdapter(
        ApprovalProcessor(core, identity_source=StaticIdentity(OWNER_USER_ID, OWNER_CHAT_ID))
    )


def _owner_approved_events(core, task_id):
    return [e for e in core.list_events(OWNER_SOURCE, task_id=task_id)
            if e.type == "gate.owner_approved"]


def _owner_rejected_events(core, task_id):
    return [e for e in core.list_events(OWNER_SOURCE, task_id=task_id)
            if e.type == "gate.owner_rejected"]


def _dump_db(core):
    conn = core._store._conn
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    parts = []
    for t in tables:
        try:
            for row in conn.execute(f"SELECT * FROM {t}"):
                parts.append(str(dict(row)))
        except sqlite3.Error:
            pass
    return "\n".join(parts)


def _module_ast():
    import argent_core.telegram_approval_adapter as mod
    return ast.parse(inspect.getsource(mod))


def _imported_top_modules(tree):
    top = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top.add(node.module.split(".")[0])
    return top


def _all_identifiers(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


# ---------------------------------------------------------------------------
# Namespace split (argent:<payload> — proof / PHASE3CB1_PROOF.md)
# ---------------------------------------------------------------------------

def test_namespace_split():
    challenge = "b" * 43
    payload = f"A:{challenge}"
    assert split_namespace(f"{CALLBACK_NAMESPACE}:{payload}") == payload
    assert split_namespace(f"{CALLBACK_NAMESPACE}:") == ""
    # Non-argent namespaces, free text, non-strings -> None (never forwarded).
    assert split_namespace(f"other:{payload}") is None
    assert split_namespace(payload) is None
    assert split_namespace("hello") is None
    assert split_namespace("") is None
    assert split_namespace(None) is None
    assert split_namespace(123) is None


# ---------------------------------------------------------------------------
# Happy paths (exact field mapping, exactly-once to the core)
# ---------------------------------------------------------------------------

def test_approve_passed_exactly_once_and_recorded(env, chal):
    challenge, raw = chal
    spy = SpyProcessor(_processor(env))
    adapter = TelegramApprovalAdapter(spy)
    outcome = _call(adapter, payload=f"A:{raw}", update_id=11)
    assert outcome is CallbackOutcome.APPROVED
    # Exactly one core call with the documented field mapping.
    assert spy.calls == [dict(
        action=CallbackAction.APPROVE, challenge=raw, update_id=11,
        message_date=MSG_DATE, private_chat_identity=OWNER_CHAT_ID,
        sender_identity=OWNER_USER_ID, ref=REF,
    )]
    assert env.core._store.get_challenge(challenge.id)["status"] == \
        ChallengeStatus.CONSUMED_APPROVED.value
    assert env.core._store.get_challenge(challenge.id)["consumed_update_id"] == 11
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.APPROVED
    assert env.core.queries.get_task(env.task.id).state is TaskState.OWNER_APPROVAL_REQUIRED
    assert len(_owner_approved_events(env.core, env.task.id)) == 1


def test_reject_passed_exactly_once_and_recorded(env, chal):
    challenge, raw = chal
    spy = SpyProcessor(_processor(env))
    adapter = TelegramApprovalAdapter(spy)
    outcome = _call(adapter, payload=f"R:{raw}", update_id=12)
    assert outcome is CallbackOutcome.REJECTED
    assert spy.calls == [dict(
        action=CallbackAction.REJECT, challenge=raw, update_id=12,
        message_date=MSG_DATE, private_chat_identity=OWNER_CHAT_ID,
        sender_identity=OWNER_USER_ID, ref=REF,
    )]
    assert env.core._store.get_challenge(challenge.id)["status"] == \
        ChallengeStatus.CONSUMED_REJECTED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.REJECTED
    assert env.core.queries.get_task(env.task.id).state is TaskState.BLOCKED
    assert len(_owner_rejected_events(env.core, env.task.id)) == 1


def test_details_read_only_no_consumption(env, chal):
    challenge, raw = chal
    spy = SpyProcessor(_processor(env))
    adapter = TelegramApprovalAdapter(spy)
    expiry_before = challenge.expires_at
    outcome = _call(adapter, payload=f"D:{raw}", update_id=13)
    assert outcome is CallbackOutcome.DETAILS
    assert spy.calls == [dict(
        action=CallbackAction.DETAILS, challenge=raw, update_id=13,
        message_date=MSG_DATE, private_chat_identity=OWNER_CHAT_ID,
        sender_identity=OWNER_USER_ID, ref=REF,
    )]
    row = env.core._store.get_challenge(challenge.id)
    assert row["status"] == ChallengeStatus.ISSUED.value
    assert row["expires_at"] == expiry_before  # no expiry extension
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
    assert env.core._store.get_update_log(13) is None  # read-only: no audit row


# ---------------------------------------------------------------------------
# Fail-closed grammar: free text / unknown action / malformed payloads
# (rejected BEFORE the core — no core call, no ledger change)
# ---------------------------------------------------------------------------

def test_normal_text_never_approval_fail_closed_before_core(env, chal):
    challenge, raw = chal
    spy = SpyProcessor(_processor(env))
    adapter = TelegramApprovalAdapter(spy)
    texts = [
        "hello",
        "APPROVE " + raw,
        "REJECT " + raw,
        "A:" + raw + " please",
        "approve",
        "/APPROVE",
        "genehmigen",
        raw,                     # bare token, no action prefix
        "A",                      # no token
        "",
    ]
    for i, text in enumerate(texts):
        outcome = _call(adapter, payload=text, update_id=100 + i)
        assert outcome is CallbackOutcome.MALFORMED, (text, outcome)
    # None of them reached the core, and nothing changed in the ledger.
    assert spy.calls == []
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
    assert env.core._store.get_inbound_state()["next_update_id"] == 0
    for i in range(len(texts)):
        assert env.core._store.get_update_log(100 + i) is None


def test_unknown_callback_data_fail_closed(env, chal):
    challenge, raw = chal
    spy = SpyProcessor(_processor(env))
    adapter = TelegramApprovalAdapter(spy)
    outcome = _call(adapter, payload="X:" + "b" * 43, update_id=14)
    assert outcome is CallbackOutcome.MALFORMED
    assert spy.calls == []
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value
    assert env.core._store.get_update_log(14) is None


def test_malformed_callback_fail_closed(env, chal):
    challenge, raw = chal
    spy = SpyProcessor(_processor(env))
    adapter = TelegramApprovalAdapter(spy)
    malformed = [
        "A:" + "b" * 42,          # too short
        "A:" + "b" * 44,          # too long
        "A:" + "b" * 42 + "!",    # wrong charset (padding / non-URL-safe)
        "A:" + "b" * 43 + "x",    # extra chars after valid challenge
        "A:",                      # empty challenge
        "a:" + "b" * 43,          # lowercase action
        "A::" + "b" * 43,         # extra colon
        "AR:" + "b" * 43,         # two actions
    ]
    for i, payload in enumerate(malformed):
        outcome = _call(adapter, payload=payload, update_id=200 + i)
        assert outcome is CallbackOutcome.MALFORMED, (payload, outcome)
    assert spy.calls == []
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
    assert env.core._store.get_inbound_state()["next_update_id"] == 0


# ---------------------------------------------------------------------------
# Identity / dedup / replay (core path, delegated)
# ---------------------------------------------------------------------------

def test_wrong_user_and_wrong_chat_unauthorized(env, chal):
    challenge, raw = chal
    adapter = _adapter(env)
    # wrong chat -> WRONG_CHAT (core authorization)
    assert _call(adapter, payload=f"A:{raw}", update_id=21,
                 chat="WRONG-CHAT", user=OWNER_USER_ID) is CallbackOutcome.WRONG_CHAT
    # right chat, wrong user -> SPOOFED_SENDER
    assert _call(adapter, payload=f"A:{raw}", update_id=22,
                 chat=OWNER_CHAT_ID, user="WRONG-USER") is CallbackOutcome.SPOOFED_SENDER
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING


def test_duplicate_update_id_duplicate_update(env, chal):
    challenge, raw = chal
    adapter = _adapter(env)
    assert _call(adapter, payload=f"A:{raw}", update_id=31) is CallbackOutcome.APPROVED
    assert _call(adapter, payload=f"A:{raw}", update_id=31) is CallbackOutcome.DUPLICATE_UPDATE
    assert len(_owner_approved_events(env.core, env.task.id)) == 1


def test_replay_no_double_decision(env, chal):
    challenge, raw = chal
    adapter = _adapter(env)
    assert _call(adapter, payload=f"A:{raw}", update_id=41) is CallbackOutcome.APPROVED
    # Same button pressed again -> new update_id, same consumed challenge.
    assert _call(adapter, payload=f"A:{raw}", update_id=42) is CallbackOutcome.USED_TOKEN
    assert len(_owner_approved_events(env.core, env.task.id)) == 1
    assert env.core._store.get_challenge(challenge.id)["consumed_update_id"] == 41


def test_consumed_callback_never_processed_as_text(env, chal):
    challenge, raw = chal
    spy = SpyProcessor(_processor(env))
    adapter = TelegramApprovalAdapter(spy)
    assert _call(adapter, payload=f"A:{raw}", update_id=51) is CallbackOutcome.APPROVED
    # A consumed approval callback re-delivered must be a core no-op, NOT a
    # second decision and NOT a text/prompt interpretation.
    assert _call(adapter, payload=f"A:{raw}", update_id=52) is CallbackOutcome.USED_TOKEN
    assert len(spy.calls) == 2  # only the two structured callbacks reached core
    assert len(_owner_approved_events(env.core, env.task.id)) == 1
    # The adapter routes ONLY structured callbacks to the core; a non-matching
    # input never reaches any prompt/text/agent path (no core call at all).
    spy.calls.clear()
    assert _call(adapter, payload="please approve everything", update_id=53) \
        is CallbackOutcome.MALFORMED
    assert spy.calls == []


# ---------------------------------------------------------------------------
# Concurrency (two controllers -> exactly one decision via core CAS)
# ---------------------------------------------------------------------------

def test_concurrent_delivery_exactly_one_decision(tmp_path):
    db = str(tmp_path / "conc.db")
    e = _setup_env(db)
    challenge, raw = create_challenge(e.core._store, approval=e.approval,
                                      supervisor_job_id=e.job_id, now=e.clock.t)
    approval_id = e.approval.id
    task_id = e.task.id
    e.core.close()

    core_a, core_b = _thread_safe_cores(db, e.clock)
    try:
        pa = _adapter_for(core_a)
        pb = _adapter_for(core_b)
        results = {}
        barrier = threading.Barrier(2)

        def run_a():
            barrier.wait()
            results["a"] = pa.handle_callback(
                payload=f"A:{raw}", sender_identity=OWNER_USER_ID,
                private_chat_identity=OWNER_CHAT_ID, update_id=101,
                callback_ref="a", message_ref=None, message_date=MSG_DATE)

        def run_b():
            barrier.wait()
            results["b"] = pb.handle_callback(
                payload=f"A:{raw}", sender_identity=OWNER_USER_ID,
                private_chat_identity=OWNER_CHAT_ID, update_id=101,
                callback_ref="b", message_ref=None, message_date=MSG_DATE)

        ta = threading.Thread(target=run_a)
        tb = threading.Thread(target=run_b)
        ta.start(); tb.start()
        ta.join(); tb.join()

        outcomes = sorted([results["a"].value, results["b"].value])
        assert outcomes.count("APPROVED") == 1, results
        assert set(outcomes) <= {"APPROVED", "DUPLICATE_UPDATE", "LOCKED"}, results
        assert core_a._store.get_approval(approval_id).status is ApprovalStatus.APPROVED
        assert len(_owner_approved_events(core_a, task_id)) == 1
        assert core_a._store.get_challenge(challenge.id)["consumed_update_id"] == 101
    finally:
        core_a.close(); core_b.close()


# ---------------------------------------------------------------------------
# Total delivery (adapter never raises; supervisor loop untouched)
# ---------------------------------------------------------------------------

def test_core_error_fail_closed_no_fallback(env, chal):
    challenge, raw = chal
    adapter = TelegramApprovalAdapter(RaisingProcessor())
    # A core/host error never escapes and never becomes free Telegram text
    # processing: the outcome is ERROR and nothing else happens.
    outcome = _call(adapter, payload=f"A:{raw}", update_id=61)
    assert outcome is CallbackOutcome.ERROR
    # No decision, no ledger change (the raising processor has no store).
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
    assert env.core._store.get_inbound_state()["next_update_id"] == 0


def test_host_exception_fail_closed_supervisor_loop_untouched(env, chal):
    challenge, raw = chal
    adapter = TelegramApprovalAdapter(RaisingProcessor())
    # Simulate a host-side exception surfacing on every delivery: the adapter
    # is TOTAL, so a supervisor loop keeps running and never sees an exception.
    outcomes = []
    for i in range(5):
        outcomes.append(_call(adapter, payload=f"A:{raw}", update_id=70 + i))
    assert outcomes == [CallbackOutcome.ERROR] * 5
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value


def test_dispatch_callback_maps_host_fields(env, chal):
    challenge, raw = chal
    spy = SpyProcessor(_processor(env))
    ux = FakePostDecisionUx()
    adapter = TelegramApprovalAdapter(spy, ux=ux)
    outcome = dispatch_callback(
        adapter, payload=f"A:{raw}", sender_id=OWNER_USER_ID, chat_id=OWNER_CHAT_ID,
        update_id=71, callback_ref=REF, message_ref=MESSAGE_REF, message_date=MSG_DATE,
    )
    assert outcome is CallbackOutcome.APPROVED
    # Core receives callback_ref as its dedupe `ref`.
    assert spy.calls == [dict(
        action=CallbackAction.APPROVE, challenge=raw, update_id=71,
        message_date=MSG_DATE, private_chat_identity=OWNER_CHAT_ID,
        sender_identity=OWNER_USER_ID, ref=REF,
    )]
    # F2/F4.3: BOTH references flow observably to the correct destinations —
    # callback_ref -> answer_callback_query; message_ref -> edit/remove.
    assert ux.calls == [
        ("answer_callback_query", REF),
        ("edit_approval_message", MESSAGE_REF, True),
        ("remove_buttons", MESSAGE_REF),
    ]


def test_dispatch_callback_message_ref_none_skips_edit_remove(env, chal):
    challenge, raw = chal
    spy = SpyProcessor(_processor(env))
    ux = FakePostDecisionUx()
    adapter = TelegramApprovalAdapter(spy, ux=ux)
    outcome = dispatch_callback(
        adapter, payload=f"A:{raw}", sender_id=OWNER_USER_ID, chat_id=OWNER_CHAT_ID,
        update_id=72, callback_ref=REF, message_ref=None, message_date=MSG_DATE,
    )
    assert outcome is CallbackOutcome.APPROVED
    # callback_ref -> answer_callback_query; no message target -> edit/remove
    # are SKIPPED (never called with a fake/None target).
    assert ux.calls == [("answer_callback_query", REF)]


# ---------------------------------------------------------------------------
# Post-decision UX (A6): after commit, best-effort, never rollback
# ---------------------------------------------------------------------------

def test_ux_triggered_after_commit(env, chal):
    challenge, raw = chal
    ux = FakePostDecisionUx()
    adapter = _adapter(env, ux=ux)
    outcome = _call(adapter, payload=f"A:{raw}", update_id=81, callback_ref=REF)
    assert outcome is CallbackOutcome.APPROVED
    # F2 reference mapping: callback_ref answers the query; message_ref
    # targets the originating message for edit/remove.
    assert ux.calls == [
        ("answer_callback_query", REF),
        ("edit_approval_message", MESSAGE_REF, True),
        ("remove_buttons", MESSAGE_REF),
    ]


def test_ux_skips_edit_remove_when_message_ref_none(env, chal):
    challenge, raw = chal
    ux = FakePostDecisionUx()
    adapter = _adapter(env, ux=ux)
    outcome = adapter.handle_callback(
        payload=f"A:{raw}", sender_identity=OWNER_USER_ID,
        private_chat_identity=OWNER_CHAT_ID, update_id=86,
        callback_ref=REF, message_ref=None, message_date=MSG_DATE,
    )
    assert outcome is CallbackOutcome.APPROVED
    # Reference mapping: answer_callback_query runs with callback_ref; with no
    # message_ref, edit/remove are SKIPPED (no fake/None target).
    assert ux.calls == [("answer_callback_query", REF)]


def test_ux_reject_records_decided_false(env, chal):
    challenge, raw = chal
    ux = FakePostDecisionUx()
    adapter = _adapter(env, ux=ux)
    outcome = _call(adapter, payload=f"R:{raw}", update_id=82, callback_ref=REF)
    assert outcome is CallbackOutcome.REJECTED
    assert ("edit_approval_message", MESSAGE_REF, False) in ux.calls


def test_ux_failure_never_rolls_back_decision(env, chal):
    challenge, raw = chal
    ux = FakePostDecisionUx(fail_answer=True, fail_edit=True, fail_remove=True)
    adapter = _adapter(env, ux=ux)
    outcome = _call(adapter, payload=f"A:{raw}", update_id=83, callback_ref=REF)
    # UI failure never rolls back the decision (A6): outcome stays APPROVED.
    assert outcome is CallbackOutcome.APPROVED
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.APPROVED
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.CONSUMED_APPROVED.value
    assert len(_owner_approved_events(env.core, env.task.id)) == 1
    # All three attempts were still recorded.
    assert len(ux.calls) == 3


def test_ux_not_called_on_non_decision(env, chal):
    challenge, raw = chal
    ux = FakePostDecisionUx()
    adapter = _adapter(env, ux=ux)
    # DETAILS is read-only: no post-decision UX.
    assert _call(adapter, payload=f"D:{raw}", update_id=84) is CallbackOutcome.DETAILS
    assert ux.calls == []
    # A rejected-before-core text never triggers UX.
    assert _call(adapter, payload="hello", update_id=85) is CallbackOutcome.MALFORMED
    assert ux.calls == []


# ---------------------------------------------------------------------------
# No secrets / no network (SPEC V3C §15, A4)
# ---------------------------------------------------------------------------

def test_no_token_or_identity_persisted(env, chal):
    challenge, raw = chal
    adapter = _adapter(env)
    _call(adapter, payload=f"A:{raw}", update_id=91)
    dump = _dump_db(env.core)
    assert raw not in dump
    assert OWNER_USER_ID not in dump
    assert OWNER_CHAT_ID not in dump
    # Only the sha256 token hash is persisted, never the raw token.
    assert token_hash(raw) in dump
    for row in env.core._store._conn.execute(
            "SELECT outcome FROM telegram_update_log").fetchall():
        assert row["outcome"] != "PROCESSING"


def test_fake_ux_records_no_secrets(env, chal):
    challenge, raw = chal
    ux = FakePostDecisionUx()
    adapter = _adapter(env, ux=ux)
    _call(adapter, payload=f"A:{raw}", update_id=92, callback_ref=REF)
    # The fake UX records only method names + the callback ref — never the
    # raw token, token hash, identities or scope.
    blob = json.dumps(ux.calls)
    assert raw not in blob
    assert token_hash(raw) not in blob
    assert OWNER_USER_ID not in blob
    assert OWNER_CHAT_ID not in blob
    assert "prod" not in blob


def test_module_has_no_network_imports(env):
    tree = _module_ast()
    imported = _imported_top_modules(tree)
    # No socket/requests/urllib/http/subprocess/os import anywhere.
    assert imported & {"socket", "requests", "urllib", "http", "subprocess",
                        "os", "sys", "aiohttp", "httpx"} == set()


def test_module_has_no_network_call_or_second_poller(env):
    tree = _module_ast()
    names = _all_identifiers(tree)
    # No Telegram Bot API primitive, no HTTP primitive, no poller/webhook.
    for forbidden in (
        "getUpdates", "sendMessage", "editMessageText", "answerCallbackQuery",
        "urlopen", "webhook", "long_poll", "socket",
    ):
        assert forbidden not in names, forbidden
    # The adapter exposes only the structured-callback surface: no prompt,
    # text, agent, shell or execution routing method exists on the adapter.
    adapter_public = {n for n in dir(TelegramApprovalAdapter) if not n.startswith("__")}
    assert adapter_public == {"handle_callback", "deliver_callback",
                             "_run_post_decision_ux"}


# ---------------------------------------------------------------------------
# Host-boundary contract gap (F1/F2): the INSTALLED OpenClaw handler context
# lacks update_id / message_date and exposes no answerCallbackQuery responder.
# ---------------------------------------------------------------------------

def _installed_handler_context_snippet() -> str:
    """Read the checked-in excerpt of the INSTALLED OpenClaw declaration file
    (fixture) and strip the header comment lines so they cannot falsify the
    absent-field assertions below."""
    path = Path(__file__).parent / "fixtures" / \
        "interactive_dispatch_handler_context.d.ts.snippet"
    return "\n".join(
        ln for ln in path.read_text().splitlines()
        if not ln.strip().startswith("//")
    )


def test_installed_handler_context_host_boundary_contract():
    src = _installed_handler_context_snippet()
    # Present: the fields the installed handler context actually provides.
    for present in (
        "callbackId", "senderId", "chatId", "messageId",
        "payload", "data", "namespace",
        "editMessage", "editButtons", "clearButtons", "deleteMessage",
    ):
        assert present in src, present
    # Absent: the fields the installed handler context does NOT provide. The
    # test reads the checked-in static fixture only (no automatic live
    # detection): after an OpenClaw upgrade the fixture must be re-extracted
    # from the installed dist d.ts (manual step) before this assert can fail
    # for newly added fields. See docs/PHASE3CB2A_STATUS.md.
    for absent in (
        "update_id", "updateId", "message_date", "messageDate",
        "answerCallbackQuery",
    ):
        assert absent not in src, absent


# ---------------------------------------------------------------------------
# Host-contract omission fail-closed (F1 regression): omitted / unusable
# update_id / message_date -> HOST_CONTRACT_VIOLATION (NOT TypeError), with
# zero core calls and no ledger change, for BOTH entry points.
# ---------------------------------------------------------------------------

_OMIT = object()

_BAD_HOST_FIELD_CASES = [
    ("omitted_update_id", _OMIT, MSG_DATE),
    ("omitted_message_date", 1, _OMIT),
    ("both_omitted", _OMIT, _OMIT),
    ("update_id_none", None, MSG_DATE),
    ("message_date_none", 1, None),
    ("update_id_bool", True, MSG_DATE),
    ("update_id_negative", -1, MSG_DATE),
    ("update_id_string", "5", MSG_DATE),
    ("update_id_float", 5.0, MSG_DATE),
]


def _host_boundary_call(entry, adapter, payload, *, update_id, message_date):
    """Invoke ``handle_callback`` or ``dispatch_callback`` with the given host
    fields; ``_OMIT`` means the argument is NOT passed at all (the exact F1
    omission bug)."""
    if entry == "handle":
        kwargs = dict(
            payload=payload, sender_identity=OWNER_USER_ID,
            private_chat_identity=OWNER_CHAT_ID,
            callback_ref=REF, message_ref=MESSAGE_REF,
        )
        fn = adapter.handle_callback
    else:
        kwargs = dict(
            payload=payload, sender_id=OWNER_USER_ID, chat_id=OWNER_CHAT_ID,
            callback_ref=REF, message_ref=MESSAGE_REF,
        )
        fn = lambda **kw: dispatch_callback(adapter, **kw)
    if update_id is not _OMIT:
        kwargs["update_id"] = update_id
    if message_date is not _OMIT:
        kwargs["message_date"] = message_date
    return fn(**kwargs)


@pytest.mark.parametrize("entry", ["handle", "dispatch"])
@pytest.mark.parametrize(
    "case_id,update_id,message_date", _BAD_HOST_FIELD_CASES,
    ids=[c[0] for c in _BAD_HOST_FIELD_CASES],
)
def test_host_contract_omission_fail_closed(
    env, chal, entry, case_id, update_id, message_date
):
    challenge, raw = chal
    spy = SpyProcessor(_processor(env))
    adapter = TelegramApprovalAdapter(spy)
    ledger_before = _dump_db(env.core)
    # TOTAL (F1): a host that omits or unusably supplies update_id/message_date
    # must NOT raise TypeError — it must fail closed at the boundary.
    outcome = _host_boundary_call(
        entry, adapter, payload=f"A:{raw}",
        update_id=update_id, message_date=message_date,
    )
    assert outcome is AdapterOutcome.HOST_CONTRACT_VIOLATION
    # Zero processor calls — the core is never invoked.
    assert spy.calls == []
    # No ledger change: no new outbox/challenge/update-log rows.
    assert _dump_db(env.core) == ledger_before
    assert env.core._store.get_challenge(challenge.id)["status"] == \
        ChallengeStatus.ISSUED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
    assert env.core._store.get_inbound_state()["next_update_id"] == 0
