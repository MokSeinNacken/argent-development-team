"""Phase 3C-A approval callback processor tests (SPEC V3C §5/§6.2/§6.3/§10/
§11/§12/§14, owner amendments A1/A3/A6/A7).

Offline and deterministic: temp DB, fake clock, no network, no agents, no
Telegram.  Exercises the transport-neutral :class:`ApprovalProcessor` end to
end against the ``owner_approvals`` ledger, the challenge CAS, update-id dedup,
the restart-fixed cursor, exactly-once concurrency, expiry, binding-mismatch
invalidation, post-decision UX and the no-secrets audit invariant.

Dummy identities only: ``OWNER_USER_ID`` / ``OWNER_CHAT_ID`` are obviously
fake and — per A3/§5 (single canonical owner, F3) — deliberately the SAME
string.  Token material comes from ``secrets.token_urlsafe`` at runtime and is
never asserted against a fixed value.
"""

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from argent_core import (
    OWNER_SOURCE,
    ApprovalError,
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
    invalidate_challenge,
    token_hash,
)
from argent_core.approval_processor import (
    FUTURE_SKEW_BOUND,
    ApprovalProcessor,
    CallbackOutcome,
    DeterministicMockPostDecisionUx,
)
from argent_core.store import Store

LEAD = role_source(Role.LEAD)
# Single canonical owner identity (A3/§5, F3): the user AND chat identities
# must resolve to this ONE string.
OWNER_USER_ID = "owner-42"
OWNER_CHAT_ID = "owner-42"
REF = "cbq:ref-1"

# The fixed fake clock starts at 2026-01-01 12:00:00 UTC; every challenge in
# the ``chal`` fixture is created at that instant, so this timestamp is a
# valid in-window ``message_date`` for all non-expiry tests.
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
    """An environment with one ISSUED challenge; returns (challenge, raw)."""
    challenge, raw = create_challenge(env.core._store, approval=env.approval,
                                      supervisor_job_id=env.job_id, now=env.clock.t)
    return challenge, raw


def _processor(env, ux=None):
    return ApprovalProcessor(
        env.core, identity_source=StaticIdentity(OWNER_USER_ID, OWNER_CHAT_ID), ux=ux
    )


def _call(processor, *, action, challenge, update_id=1,
          message_date=MSG_DATE, chat=OWNER_CHAT_ID, user=OWNER_USER_ID, ref=REF):
    return processor.process_callback(
        action=action, challenge=challenge, update_id=update_id,
        message_date=message_date,
        private_chat_identity=chat, sender_identity=user, ref=ref,
    )


def _thread_safe_cores(db_path, clock):
    """Two Core instances over two independent connections to the SAME DB file
    (thread-safe, with busy_timeout so a second BEGIN IMMEDIATE waits instead
    of raising "database is locked")."""
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


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_happy_approve(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=11)
    assert outcome is CallbackOutcome.APPROVED
    row = env.core._store.get_challenge(challenge.id)
    assert row["status"] == ChallengeStatus.CONSUMED_APPROVED.value
    assert row["consumed_update_id"] == 11
    ap = env.core._store.get_approval(env.approval.id)
    assert ap.status is ApprovalStatus.APPROVED
    # Approval is NOT execution: task stays at the gate.
    assert env.core.queries.get_task(env.task.id).state is TaskState.OWNER_APPROVAL_REQUIRED
    assert len(_owner_approved_events(env.core, env.task.id)) == 1
    log = env.core._store.get_update_log(11)
    assert log["outcome"] == "APPROVED"
    assert log["decision"] == "APPROVE"
    assert env.core._store.get_inbound_state()["next_update_id"] == 12


def test_happy_reject(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    outcome = _call(processor, action=CallbackAction.REJECT, challenge=raw,
                    update_id=12)
    assert outcome is CallbackOutcome.REJECTED
    assert env.core._store.get_challenge(challenge.id)["status"] == \
        ChallengeStatus.CONSUMED_REJECTED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.REJECTED
    assert env.core.queries.get_task(env.task.id).state is TaskState.BLOCKED
    assert len(_owner_rejected_events(env.core, env.task.id)) == 1
    log = env.core._store.get_update_log(12)
    assert log["outcome"] == "REJECTED"
    assert log["decision"] == "REJECT"


def test_details_read_only(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    expiry_before = challenge.expires_at
    outcome = _call(processor, action=CallbackAction.DETAILS, challenge=raw,
                    update_id=13)
    assert outcome is CallbackOutcome.DETAILS
    row = env.core._store.get_challenge(challenge.id)
    assert row["status"] == ChallengeStatus.ISSUED.value
    assert row["expires_at"] == expiry_before  # no expiry extension
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
    assert env.core._store.get_update_log(13) is None  # read-only: no audit row
    # Safe payload: internal ids + scope ref only, no secrets.
    details = processor.safe_details(raw)
    assert details["job_id"] == env.job_id
    assert details["task_id"] == env.task.id
    assert details["gate_id"] == env.approval.id
    assert details["valid_until"] == expiry_before
    assert details["scope_ref"] == "sha256:" + challenge.binding_hash[:16]
    blob = json.dumps(details)
    assert raw not in blob
    assert OWNER_USER_ID not in blob
    assert OWNER_CHAT_ID not in blob
    assert "prod" not in blob  # raw scope never leaked


# ---------------------------------------------------------------------------
# Dedup / single-use
# ---------------------------------------------------------------------------

def test_duplicate_update_id_single_decision(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    assert _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                 update_id=21) is CallbackOutcome.APPROVED
    assert _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                 update_id=21) is CallbackOutcome.DUPLICATE_UPDATE
    assert len(_owner_approved_events(env.core, env.task.id)) == 1
    assert env.core._store.get_challenge(challenge.id)["consumed_update_id"] == 21


def test_same_challenge_twice_used_token(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    assert _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                 update_id=31) is CallbackOutcome.APPROVED
    # Same button pressed again -> a NEW update_id, same consumed challenge.
    assert _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                 update_id=32) is CallbackOutcome.USED_TOKEN
    assert len(_owner_approved_events(env.core, env.task.id)) == 1
    log = env.core._store.get_update_log(32)
    assert log["outcome"] == "USED_TOKEN"


def test_wrong_token_unknown(env, chal):
    _, raw = chal
    processor = _processor(env)
    wrong = "x" * 43  # valid format, unknown token
    assert wrong != raw
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=wrong,
                    update_id=41)
    assert outcome is CallbackOutcome.UNKNOWN_CHALLENGE
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
    log = env.core._store.get_update_log(41)
    assert log["outcome"] == "UNKNOWN_TOKEN"


# ---------------------------------------------------------------------------
# Malformed / identity
# ---------------------------------------------------------------------------

def test_malformed_callback_never_crashes(env, chal):
    _, raw = chal
    processor = _processor(env)
    malformed_cases = [
        dict(action="X", challenge=raw, update_id=51),           # bad action
        dict(action=CallbackAction.APPROVE, challenge="short", update_id=52),
        dict(action=CallbackAction.APPROVE, challenge="a" * 44, update_id=53),
        dict(action=CallbackAction.APPROVE, challenge="A" * 42 + "!", update_id=54),
        dict(action=CallbackAction.APPROVE, challenge=raw, update_id="not-int"),
        dict(action=CallbackAction.APPROVE, challenge=raw, update_id=-1),
        dict(action=None, challenge=raw, update_id=55),
        dict(action=CallbackAction.APPROVE, challenge=None, update_id=56),
    ]
    for kw in malformed_cases:
        outcome = _call(processor, **kw)
        assert outcome is CallbackOutcome.MALFORMED, kw
    # No challenge consumption, no gate mutation.
    assert env.core._store.get_challenge(chal[0].id)["status"] == ChallengeStatus.ISSUED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING


def test_identity_fail_closed(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    cases = [
        # wrong chat -> WRONG_CHAT (checked before sender, SPEC V3C §14)
        (dict(chat="WRONG-CHAT", user=OWNER_USER_ID), CallbackOutcome.WRONG_CHAT),
        # right chat, wrong sender -> SPOOFED_SENDER
        (dict(chat=OWNER_CHAT_ID, user="WRONG-USER"), CallbackOutcome.SPOOFED_SENDER),
        # both wrong -> WRONG_CHAT (chat fails first)
        (dict(chat="WRONG-CHAT", user="WRONG-USER"), CallbackOutcome.WRONG_CHAT),
    ]
    for idx, (kw, expected) in enumerate(cases):
        outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                        update_id=61 + idx, **kw)
        assert outcome is expected, (kw, outcome)
    # No challenge consumption, no gate mutation.
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
    # Persistent allowlisted outcomes with ONLY authorization flags: no
    # message_date, no token/approval references, no identities (§8.2/§14).
    for uid, expected in ((61, "WRONG_CHAT"), (62, "SPOOFED_SENDER"), (63, "WRONG_CHAT")):
        row = env.core._store.get_update_log(uid)
        assert row is not None and row["outcome"] == expected, uid
        assert row["message_date"] is None
        assert row["challenge_id"] is None
        assert row["approval_id"] is None
        assert row["decision"] is None
    dump = _dump_db(env.core)
    assert OWNER_USER_ID not in dump
    assert OWNER_CHAT_ID not in dump
    assert env.core._store.get_inbound_state()["next_update_id"] == 64


# ---------------------------------------------------------------------------
# Expiry / binding / decided / invalidated
# ---------------------------------------------------------------------------

def test_expired_challenge(env):
    # Give the approval a longer life so ONLY the challenge expires.
    longer = (env.clock.t + timedelta(seconds=7200)).isoformat()
    env.core._store._conn.execute(
        "UPDATE owner_approvals SET expires_at = ? WHERE id = ?",
        (longer, env.approval.id),
    )
    approval = env.core._store.get_approval(env.approval.id)
    challenge, raw = create_challenge(env.core._store, approval=approval,
                                      supervisor_job_id=env.job_id, now=env.clock.t)
    processor = _processor(env)
    env.clock.advance(3601)
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=71)
    assert outcome is CallbackOutcome.EXPIRED
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.EXPIRED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
    assert env.core._store.get_update_log(71)["outcome"] == "EXPIRED_TOKEN"


def test_gate_changed_binding_mismatch(env, chal):
    challenge, raw = chal
    # Tamper the persisted approval binding hash (gate content changed).
    env.core._store._conn.execute(
        "UPDATE owner_approvals SET binding_hash = ? WHERE id = ?",
        ("0" * 64, env.approval.id),
    )
    processor = _processor(env)
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=72)
    assert outcome is CallbackOutcome.GATE_CHANGED
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.INVALIDATED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
    assert env.core._store.get_update_log(72)["outcome"] == "BINDING_MISMATCH"


def test_gate_already_decided(env, chal):
    challenge, raw = chal
    # Decide the gate out-of-band (owner approves directly), challenge remains.
    env.core.approve(env.approval.id, OWNER_SOURCE, task_id=env.task.id,
                     action="deploy_production", scope="prod")
    processor = _processor(env)
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=73)
    assert outcome is CallbackOutcome.GATE_DECIDED
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value
    assert env.core._store.get_update_log(73)["outcome"] == "APPROVAL_NOT_PENDING"


def test_gate_already_rejected(env, chal):
    challenge, raw = chal
    # Decide the gate out-of-band (owner rejects directly), challenge remains.
    env.core.reject(env.approval.id, OWNER_SOURCE, task_id=env.task.id,
                    action="deploy_production", scope="prod")
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.REJECTED
    processor = _processor(env)
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=76)
    assert outcome is CallbackOutcome.GATE_DECIDED
    # Challenge stays ISSUED (no consumption); an adversarial APPROVE cannot
    # reopen the rejected gate; the approval stays REJECTED.
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value
    assert env.core._store.get_challenge(challenge.id)["consumed_update_id"] is None
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.REJECTED
    assert env.core._store.get_update_log(76)["outcome"] == "APPROVAL_NOT_PENDING"
    assert len(_owner_rejected_events(env.core, env.task.id)) == 1
    assert len(_owner_approved_events(env.core, env.task.id)) == 0


def test_gate_already_consumed(env, chal):
    challenge, raw = chal
    # Consume the gate out-of-band (owner approves, then executes), challenge
    # remains ISSUED.
    env.core.approve(env.approval.id, OWNER_SOURCE, task_id=env.task.id,
                     action="deploy_production", scope="prod")
    env.core.execute_approved(env.approval.id, OWNER_SOURCE, task_id=env.task.id,
                              action="deploy_production", scope="prod")
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.CONSUMED
    processor = _processor(env)
    outcome = _call(processor, action=CallbackAction.REJECT, challenge=raw,
                    update_id=77)
    assert outcome is CallbackOutcome.GATE_DECIDED
    # Challenge stays ISSUED (no consumption); an already-executed approval can
    # be neither re-approved nor rejected via a leftover challenge.
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value
    assert env.core._store.get_challenge(challenge.id)["consumed_update_id"] is None
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.CONSUMED
    assert env.core._store.get_update_log(77)["outcome"] == "APPROVAL_NOT_PENDING"
    assert len(_owner_approved_events(env.core, env.task.id)) == 1
    assert len(_owner_rejected_events(env.core, env.task.id)) == 0


def test_invalidated_challenge(env, chal):
    challenge, raw = chal
    invalidate_challenge(env.core._store, challenge.id, now_iso=env.core._store.now_iso())
    processor = _processor(env)
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=74)
    assert outcome is CallbackOutcome.UNKNOWN_CHALLENGE
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
    assert env.core._store.get_update_log(74)["outcome"] == "UNKNOWN_TOKEN"


def test_approval_expired_release_no_decision(env, chal):
    challenge, raw = chal
    # Force approval expiry while the challenge remains nominally valid
    # (SPEC V3C §12: approval expiry is checked independently of challenge expiry).
    past = (env.clock.t - timedelta(seconds=10)).isoformat()
    env.core._store._conn.execute(
        "UPDATE owner_approvals SET expires_at = ? WHERE id = ?",
        (past, env.approval.id),
    )
    processor = _processor(env)
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=75)
    assert outcome is CallbackOutcome.EXPIRED
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.EXPIRED
    assert env.core.queries.get_task(env.task.id).state is not TaskState.OWNER_APPROVAL_REQUIRED
    assert env.core._store.get_update_log(75)["outcome"] == "EXPIRED_APPROVAL"


# ---------------------------------------------------------------------------
# Core bridge: reject expiry guard (A7 / §12)
# ---------------------------------------------------------------------------

def test_core_reject_expired_approval_fails_closed(env):
    env.clock.advance(3601)  # past the 1h approval TTL (status still 'pending')
    with pytest.raises(ApprovalError):
        env.core.reject(env.approval.id, OWNER_SOURCE, task_id=env.task.id,
                        action="deploy_production", scope="prod")
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.EXPIRED
    assert env.core.queries.get_task(env.task.id).state is not TaskState.OWNER_APPROVAL_REQUIRED


# ---------------------------------------------------------------------------
# Restart / crash / exactly-once
# ---------------------------------------------------------------------------

def test_crash_during_cas_rolls_back_and_reprocessable(env, chal, monkeypatch):
    challenge, raw = chal
    processor = _processor(env)
    original = Store._consume_challenge

    def boom(self, *a, **k):
        raise RuntimeError("injected CAS crash")

    monkeypatch.setattr(Store, "_consume_challenge", boom)
    assert _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                 update_id=81) is CallbackOutcome.ERROR
    # Full rollback: nothing persisted.
    assert env.core._store.get_update_log(81) is None
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
    assert env.core._store.get_inbound_state()["next_update_id"] == 0

    monkeypatch.setattr(Store, "_consume_challenge", original)
    assert _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                 update_id=81) is CallbackOutcome.APPROVED
    assert len(_owner_approved_events(env.core, env.task.id)) == 1


def test_restart_before_decision_reprocesses_exactly_once(tmp_path, monkeypatch):
    db = str(tmp_path / "restart-before.db")
    e = _setup_env(db)
    challenge, raw = create_challenge(e.core._store, approval=e.approval,
                                      supervisor_job_id=e.job_id, now=e.clock.t)
    processor = _processor(e)
    original = Store._consume_challenge

    def boom(self, *a, **k):
        raise RuntimeError("injected crash before commit")

    monkeypatch.setattr(Store, "_consume_challenge", boom)
    assert _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                 update_id=91) is CallbackOutcome.ERROR
    e.core.close()  # simulate a process restart
    monkeypatch.setattr(Store, "_consume_challenge", original)

    e2 = _reopen(db, e.clock)
    try:
        p2 = _processor(e2)
        assert _call(p2, action=CallbackAction.APPROVE, challenge=raw,
                     update_id=91) is CallbackOutcome.APPROVED
        assert len(_owner_approved_events(e2.core, e.task.id)) == 1
        # A replayed (now-duplicate) update is a no-op.
        assert _call(p2, action=CallbackAction.APPROVE, challenge=raw,
                     update_id=91) is CallbackOutcome.DUPLICATE_UPDATE
    finally:
        e2.core.close()


def _reopen(db_path, clock):
    """Reopen the SAME DB file with the SAME clock (persisted state intact)."""
    return SimpleNamespace(core=Core(db_path, clock=clock), clock=clock)


def test_restart_after_decision_no_double(tmp_path):
    db = str(tmp_path / "restart-after.db")
    e = _setup_env(db)
    challenge, raw = create_challenge(e.core._store, approval=e.approval,
                                      supervisor_job_id=e.job_id, now=e.clock.t)
    processor = _processor(e)
    assert _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                 update_id=92) is CallbackOutcome.APPROVED
    e.core.close()

    e2 = _reopen(db, e.clock)
    try:
        p2 = _processor(e2)
        assert _call(p2, action=CallbackAction.APPROVE, challenge=raw,
                     update_id=92) is CallbackOutcome.DUPLICATE_UPDATE
        assert len(_owner_approved_events(e2.core, e.task.id)) == 1
        assert e2.core._store.get_approval(e.approval.id).status is ApprovalStatus.APPROVED
    finally:
        e2.core.close()


def test_replay_after_restart_duplicate(tmp_path):
    db = str(tmp_path / "replay.db")
    e = _setup_env(db)
    challenge, raw = create_challenge(e.core._store, approval=e.approval,
                                      supervisor_job_id=e.job_id, now=e.clock.t)
    processor = _processor(e)
    assert _call(processor, action=CallbackAction.REJECT, challenge=raw,
                 update_id=93) is CallbackOutcome.REJECTED
    e.core.close()

    e2 = _reopen(db, e.clock)
    try:
        p2 = _processor(e2)
        assert _call(p2, action=CallbackAction.REJECT, challenge=raw,
                     update_id=93) is CallbackOutcome.DUPLICATE_UPDATE
        assert len(_owner_rejected_events(e2.core, e.task.id)) == 1
    finally:
        e2.core.close()


# ---------------------------------------------------------------------------
# Concurrency (two controllers)
# ---------------------------------------------------------------------------

def test_two_controllers_same_update_one_wins(tmp_path):
    db = str(tmp_path / "conc1.db")
    e = _setup_env(db)
    challenge, raw = create_challenge(e.core._store, approval=e.approval,
                                      supervisor_job_id=e.job_id, now=e.clock.t)
    approval_id = e.approval.id
    task_id = e.task.id
    e.core.close()

    core_a, core_b = _thread_safe_cores(db, e.clock)
    try:
        pa = _processor_for(core_a)
        pb = _processor_for(core_b)
        results = {}
        barrier = threading.Barrier(2)

        def run_a():
            barrier.wait()
            results["a"] = pa.process_callback(
                action=CallbackAction.APPROVE, challenge=raw, update_id=101,
                message_date=MSG_DATE,
                private_chat_identity=OWNER_CHAT_ID, sender_identity=OWNER_USER_ID,
                ref="a")

        def run_b():
            barrier.wait()
            results["b"] = pb.process_callback(
                action=CallbackAction.APPROVE, challenge=raw, update_id=101,
                message_date=MSG_DATE,
                private_chat_identity=OWNER_CHAT_ID, sender_identity=OWNER_USER_ID,
                ref="b")

        ta = threading.Thread(target=run_a)
        tb = threading.Thread(target=run_b)
        ta.start(); tb.start()
        ta.join(); tb.join()

        outcomes = sorted([results["a"].value, results["b"].value])
        # Exactly one controller wins the decision (SPEC V3C §6.3).  With the
        # dedicated busy_timeout=0 inbound connection the loser aborts
        # immediately on the DB lock (§14) — or, if the winner already
        # committed, is a DUPLICATE_UPDATE no-op.
        assert outcomes.count("APPROVED") == 1, results
        assert set(outcomes) <= {"APPROVED", "DUPLICATE_UPDATE", "LOCKED"}, results
        assert core_a._store.get_approval(approval_id).status is ApprovalStatus.APPROVED
        assert len(_owner_approved_events(core_a, task_id)) == 1
        assert core_a._store.get_challenge(challenge.id)["consumed_update_id"] == 101
    finally:
        core_a.close(); core_b.close()


def test_two_updates_same_challenge_first_wins(tmp_path):
    db = str(tmp_path / "conc2.db")
    e = _setup_env(db)
    challenge, raw = create_challenge(e.core._store, approval=e.approval,
                                      supervisor_job_id=e.job_id, now=e.clock.t)
    approval_id = e.approval.id
    task_id = e.task.id
    e.core.close()

    core_a, core_b = _thread_safe_cores(db, e.clock)
    try:
        pa = _processor_for(core_a)
        pb = _processor_for(core_b)
        results = {}
        barrier = threading.Barrier(2)

        def run_a():
            barrier.wait()
            results["a"] = pa.process_callback(
                action=CallbackAction.APPROVE, challenge=raw, update_id=201,
                message_date=MSG_DATE,
                private_chat_identity=OWNER_CHAT_ID, sender_identity=OWNER_USER_ID,
                ref="a")

        def run_b():
            barrier.wait()
            results["b"] = pb.process_callback(
                action=CallbackAction.APPROVE, challenge=raw, update_id=202,
                message_date=MSG_DATE,
                private_chat_identity=OWNER_CHAT_ID, sender_identity=OWNER_USER_ID,
                ref="b")

        ta = threading.Thread(target=run_a)
        tb = threading.Thread(target=run_b)
        ta.start(); tb.start()
        ta.join(); tb.join()

        outcomes = sorted([results["a"].value, results["b"].value])
        # Exactly one controller consumes the single-use challenge; the loser is
        # a no-op: USED_TOKEN (winner already committed) or LOCKED (§14).
        assert outcomes.count("APPROVED") == 1, results
        assert set(outcomes) <= {"APPROVED", "USED_TOKEN", "LOCKED"}, results
        assert core_a._store.get_approval(approval_id).status is ApprovalStatus.APPROVED
        assert len(_owner_approved_events(core_a, task_id)) == 1
        consumed = core_a._store.get_challenge(challenge.id)["consumed_update_id"]
        assert consumed in (201, 202)
    finally:
        core_a.close(); core_b.close()


def _processor_for(core):
    return ApprovalProcessor(
        core, identity_source=StaticIdentity(OWNER_USER_ID, OWNER_CHAT_ID)
    )


# ---------------------------------------------------------------------------
# Post-decision UX (A6)
# ---------------------------------------------------------------------------

def test_ux_invoked_only_after_commit(env, chal):
    challenge, raw = chal
    ux = DeterministicMockPostDecisionUx()
    processor = _processor(env, ux=ux)
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=111, ref=REF)
    assert outcome is CallbackOutcome.APPROVED
    assert ux.calls == [
        ("answer_callback_query", REF),
        ("edit_approval_message", REF, True),
        ("remove_buttons", REF),
    ]


def test_ux_edit_failure_keeps_decision_committed(env, chal):
    challenge, raw = chal
    ux = DeterministicMockPostDecisionUx(fail_edit=True)
    processor = _processor(env, ux=ux)
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=112, ref=REF)
    # UI failure never rolls back the decision (A6): outcome stays APPROVED.
    assert outcome is CallbackOutcome.APPROVED
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.APPROVED
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.CONSUMED_APPROVED.value
    assert len(_owner_approved_events(env.core, env.task.id)) == 1
    # The mock still recorded the attempts.
    assert ("edit_approval_message", REF, True) in ux.calls


def test_ux_all_failures_never_affect_reject(env, chal):
    challenge, raw = chal
    ux = DeterministicMockPostDecisionUx(fail_answer=True, fail_edit=True,
                                         fail_remove=True)
    processor = _processor(env, ux=ux)
    outcome = _call(processor, action=CallbackAction.REJECT, challenge=raw,
                    update_id=113, ref=REF)
    assert outcome is CallbackOutcome.REJECTED
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.REJECTED
    assert env.core.queries.get_task(env.task.id).state is TaskState.BLOCKED


# ---------------------------------------------------------------------------
# No secrets (§8.2/§15)
# ---------------------------------------------------------------------------

def test_no_token_or_identity_persisted(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    _call(processor, action=CallbackAction.APPROVE, challenge=raw, update_id=121)
    dump = _dump_db(env.core)
    assert raw not in dump
    assert OWNER_USER_ID not in dump
    assert OWNER_CHAT_ID not in dump
    # Only the sha256 token hash is persisted, never the raw token.
    assert token_hash(raw) in dump
    # No committed PROCESSING row may exist (SPEC V3C §8.1).
    for row in env.core._store._conn.execute(
            "SELECT outcome FROM telegram_update_log").fetchall():
        assert row["outcome"] != "PROCESSING"


def test_details_payload_has_no_secrets(env, chal):
    _, raw = chal
    processor = _processor(env)
    details = processor.safe_details(raw)
    blob = json.dumps(details)
    assert raw not in blob
    assert token_hash(raw) not in blob
    assert OWNER_USER_ID not in blob
    assert OWNER_CHAT_ID not in blob
    assert "prod" not in blob


# ---------------------------------------------------------------------------
# F1 — stale updates below the cursor are no-ops (§6.2)
# ---------------------------------------------------------------------------

def test_stale_update_below_cursor_is_noop(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    # Advance the cursor past 99 with an unrelated, higher update (a wrong
    # token persists UNKNOWN_TOKEN and advances the cursor to 101).
    wrong = "x" * 43
    assert wrong != raw
    assert _call(processor, action=CallbackAction.APPROVE, challenge=wrong,
                 update_id=100) is CallbackOutcome.UNKNOWN_CHALLENGE
    assert env.core._store.get_inbound_state()["next_update_id"] == 101
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value

    # A lower (stale) but otherwise-valid approve must be a no-op: no gate
    # mutation, no challenge consumption, cursor unchanged, persisted stale row.
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=99)
    assert outcome is CallbackOutcome.STALE_UPDATE
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value
    assert env.core._store.get_inbound_state()["next_update_id"] == 101
    log = env.core._store.get_update_log(99)
    assert log is not None and log["outcome"] == "STALE_UPDATE"
    assert len(_owner_approved_events(env.core, env.task.id)) == 0


# ---------------------------------------------------------------------------
# F2 — message_date window validation (§10/§16.6)
# ---------------------------------------------------------------------------

def test_message_date_valid_in_window_persisted(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    created = _unix_from_iso(challenge.created_at)
    message_date = created + 60  # in-window, not future
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=171, message_date=message_date)
    assert outcome is CallbackOutcome.APPROVED
    assert env.core._store.get_update_log(171)["message_date"] == message_date


def test_message_date_pre_challenge_is_stale(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    created = _unix_from_iso(challenge.created_at)
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=172, message_date=created - 1)
    assert outcome is CallbackOutcome.STALE_MESSAGE
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
    assert env.core._store.get_update_log(172)["outcome"] == "STALE_MESSAGE"
    assert env.core._store.get_inbound_state()["next_update_id"] == 173


def test_message_date_post_expiry_expires_challenge(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    created = _unix_from_iso(challenge.created_at)
    expires = _unix_from_iso(challenge.expires_at)
    # Local time just before expiry; message dated just after expiry (within
    # the future-skew bound, so it is a genuine post-expiry click, not skew).
    env.clock.advance(expires - created - 100)
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=173, message_date=expires + 50)
    assert outcome is CallbackOutcome.EXPIRED
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.EXPIRED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
    assert env.core._store.get_update_log(173)["outcome"] == "EXPIRED_TOKEN"


def test_message_date_future_skew_is_stale(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    created = _unix_from_iso(challenge.created_at)
    # Within the challenge window but more than FUTURE_SKEW_BOUND ahead of now.
    message_date = created + FUTURE_SKEW_BOUND + 100
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=174, message_date=message_date)
    assert outcome is CallbackOutcome.STALE_MESSAGE
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
    assert env.core._store.get_update_log(174)["outcome"] == "STALE_MESSAGE"


def test_message_date_missing_or_invalid_is_malformed(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    for i, bad in enumerate((None, -1, -5, "123", 1.5, True)):
        outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                        update_id=180 + i, message_date=bad)
        assert outcome is CallbackOutcome.MALFORMED, (bad, outcome)
        row = env.core._store.get_update_log(180 + i)
        assert row is not None and row["outcome"] == "MALFORMED"
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING


# ---------------------------------------------------------------------------
# F3 — persistent dedup for malformed/unauthorized updates (§14)
# ---------------------------------------------------------------------------

def test_replayed_malformed_update_is_noop(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    assert _call(processor, action="X", challenge=raw,
                 update_id=190) is CallbackOutcome.MALFORMED
    # Replay: exactly one log row, no second processing, cursor advanced once.
    assert _call(processor, action="X", challenge=raw,
                 update_id=190) is CallbackOutcome.DUPLICATE_UPDATE
    rows = [dict(r) for r in env.core._store._conn.execute(
        "SELECT * FROM telegram_update_log WHERE update_id = 190").fetchall()]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "MALFORMED"
    assert env.core._store.get_inbound_state()["next_update_id"] == 191


def test_replayed_unauthorized_update_is_noop(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    assert _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                 update_id=191, chat="WRONG-CHAT",
                 user=OWNER_USER_ID) is CallbackOutcome.WRONG_CHAT
    # Replay: one log row, no second processing, cursor advanced exactly once.
    assert _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                 update_id=191, chat="WRONG-CHAT",
                 user=OWNER_USER_ID) is CallbackOutcome.DUPLICATE_UPDATE
    rows = [dict(r) for r in env.core._store._conn.execute(
        "SELECT * FROM telegram_update_log WHERE update_id = 191").fetchall()]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "WRONG_CHAT"
    assert env.core._store.get_inbound_state()["next_update_id"] == 192
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING


# ---------------------------------------------------------------------------
# F4 — immediate termination on DB lock (§14)
# ---------------------------------------------------------------------------

def test_db_locked_aborts_immediately(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    lock_conn = sqlite3.connect(env.db_path, isolation_level=None)
    try:
        lock_conn.execute("BEGIN IMMEDIATE")  # hold the write lock
        start = time.monotonic()
        outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                        update_id=195)
        elapsed = time.monotonic() - start
        assert outcome is CallbackOutcome.LOCKED
        assert elapsed < 1.0  # no 5s default busy_timeout block
        # No cursor/gate mutation.
        assert env.core._store.get_inbound_state()["next_update_id"] == 0
        assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
        assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value
        assert env.core._store.get_update_log(195) is None
    finally:
        lock_conn.rollback()
        lock_conn.close()


# ---------------------------------------------------------------------------
# F1 — dedicated inbound connection isolation (Store._conn never swapped)
# ---------------------------------------------------------------------------

def test_inbound_pass_never_swaps_main_connection(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    store = env.core._store
    original = store._conn
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=301)
    assert outcome is CallbackOutcome.APPROVED
    # The shared connection attribute is still the original open connection.
    assert store._conn is original
    # A subsequent normal query on the main connection still works.
    assert store.get_approval(env.approval.id).status is ApprovalStatus.APPROVED
    assert store.get_inbound_state()["next_update_id"] == 302


def test_overlapping_store_probe_during_inbound_transaction(env):
    store = env.core._store
    original = store._conn
    with store._inbound_transaction() as bound:
        # The shared connection is NEVER swapped to the dedicated inbound conn.
        assert store._conn is original
        assert bound._conn is not original
        # A normal read on the shared connection stays valid mid-transaction.
        assert store.get_inbound_state() is not None
        assert store.get_approval(env.approval.id) is not None
    # After the pass the shared connection is still original and usable.
    assert store._conn is original
    assert store.get_approval(env.approval.id) is not None


def test_supervisor_access_from_other_thread_during_inbound(tmp_path):
    e = _setup_env(str(tmp_path / "thread.db"))
    try:
        store = e.core._store
        # Rebind the main connection so the worker thread may use it.
        store._conn.close()
        store._conn = sqlite3.connect(e.db_path, isolation_level=None,
                                      check_same_thread=False)
        store._conn.row_factory = sqlite3.Row
        store._conn.execute("PRAGMA foreign_keys = ON")
        original = store._conn
        result = {}

        def probe():
            try:
                result["state"] = store.get_inbound_state()
                result["approval"] = store.get_approval(e.approval.id)
                result["err"] = None
            except Exception as exc:  # pragma: no cover - diagnostic
                result["err"] = exc

        with store._inbound_transaction() as bound:
            assert store._conn is original
            assert bound._conn is not original
            t = threading.Thread(target=probe)
            t.start()
            t.join()
            assert result["err"] is None, result["err"]
            assert result["state"] is not None
            assert result["approval"] is not None
        # The shared connection remains the original, open, and usable.
        assert store._conn is original
        assert store.get_approval(e.approval.id) is not None
    finally:
        e.core.close()


# ---------------------------------------------------------------------------
# F2 — expiry boundary + precedence (message_date >= expires_at -> EXPIRED)
# ---------------------------------------------------------------------------

def test_message_date_exact_expiry_boundary_expires(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    expires = _unix_from_iso(challenge.expires_at)
    # Keep local time well before expiry so the wall-clock challenge-expiry
    # guard does not pre-empt; message_date sits ON the expiry boundary.
    env.clock.advance(100)
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=176, message_date=expires)
    assert outcome is CallbackOutcome.EXPIRED
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.EXPIRED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
    assert env.core._store.get_update_log(176)["outcome"] == "EXPIRED_TOKEN"


def test_message_date_post_expiry_plus_skew_expires(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    expires = _unix_from_iso(challenge.expires_at)
    # message_date is BOTH past expiry AND far in the future (> now + skew).
    # Post-expiry precedence must expire the challenge, never leave it ISSUED.
    env.clock.advance(100)
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=177, message_date=expires + FUTURE_SKEW_BOUND + 1000)
    assert outcome is CallbackOutcome.EXPIRED
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.EXPIRED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
    assert env.core._store.get_update_log(177)["outcome"] == "EXPIRED_TOKEN"


def test_message_date_just_before_expiry_still_approves(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    expires = _unix_from_iso(challenge.expires_at)
    # Local time near (but before) expiry, message just inside the window:
    # a valid in-window message still approves (no false positive).
    env.clock.advance(3500)
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=178, message_date=expires - 1)
    assert outcome is CallbackOutcome.APPROVED
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.APPROVED


# ---------------------------------------------------------------------------
# F3 — single canonical owner identity (A3/§5)
# ---------------------------------------------------------------------------

def test_identity_mismatched_expected_values_fail_closed(env, chal):
    challenge, raw = chal
    # Misconfigured provider: the two expected identities differ.
    processor = ApprovalProcessor(
        env.core, identity_source=StaticIdentity("owner-a", "owner-b")
    )
    outcome = processor.process_callback(
        action=CallbackAction.APPROVE, challenge=raw, update_id=64,
        message_date=MSG_DATE, private_chat_identity="owner-a",
        sender_identity="owner-a", ref=REF,
    )
    assert outcome is CallbackOutcome.WRONG_CHAT  # fail-closed (0, 0)
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
    log = env.core._store.get_update_log(64)
    assert log is not None and log["outcome"] == "WRONG_CHAT"


def test_identity_presented_pair_inconsistent_fail_closed(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    # Presented sender != presented chat: right chat, wrong sender -> SPOOFED.
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=65, chat=OWNER_CHAT_ID, user="someone-else")
    assert outcome is CallbackOutcome.SPOOFED_SENDER
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
    # Presented sender != presented chat: wrong chat, right sender -> WRONG_CHAT.
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=66, chat="other-chat", user=OWNER_USER_ID)
    assert outcome is CallbackOutcome.WRONG_CHAT
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value


def test_identity_consistent_canonical_authorized(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    # One canonical identity for both user and chat -> authorized.
    assert processor._authorization_flags(OWNER_USER_ID, OWNER_CHAT_ID) == (1, 1)
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=67)
    assert outcome is CallbackOutcome.APPROVED


# ---------------------------------------------------------------------------
# F4 — SQLite-unrepresentable integers evade persistent dedup
# ---------------------------------------------------------------------------

def test_message_date_oversized_sqlite_int_is_malformed_persisted(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    oversized = 2**63
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=200, message_date=oversized)
    assert outcome is CallbackOutcome.MALFORMED
    row = env.core._store.get_update_log(200)
    assert row is not None and row["outcome"] == "MALFORMED"
    assert row["message_date"] is None  # no oversized value persisted
    assert env.core._store.get_inbound_state()["next_update_id"] == 201
    # No token lookup, no gate change.
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
    # Replay: exactly one log row, DUPLICATE no-op.
    assert _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                 update_id=200, message_date=oversized) is CallbackOutcome.DUPLICATE_UPDATE
    rows = [dict(r) for r in env.core._store._conn.execute(
        "SELECT * FROM telegram_update_log WHERE update_id = 200").fetchall()]
    assert len(rows) == 1


def test_update_id_oversized_sqlite_int_is_malformed_noop(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=2**63, message_date=MSG_DATE)
    assert outcome is CallbackOutcome.MALFORMED
    # No update-log row (unrepresentable PK), no cursor change, no gate change.
    assert env.core._store._conn.execute(
        "SELECT COUNT(*) AS n FROM telegram_update_log").fetchone()["n"] == 0
    assert env.core._store.get_inbound_state()["next_update_id"] == 0
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING


# ---------------------------------------------------------------------------
# F6 — exact-maximum update_id cannot be cursor-advanced (2**63-1 rejected)
# ---------------------------------------------------------------------------
#
# ``update_id == 2**63-1`` is representable as the update-log PK, but its cursor
# advance (``update_id + 1`` == 2**63) overflows SQLite's signed 64-bit INTEGER.
# Option 1: reject it for processing, persist a MALFORMED row with an
# exactly-once cursor advance saturated at _SQLITE_MAX_INT (never 2**63).

def test_update_id_exact_sqlite_max_is_malformed_persisted(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    boundary = 2**63 - 1
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=boundary, message_date=MSG_DATE)
    assert outcome is CallbackOutcome.MALFORMED
    # Persisted MALFORMED row with the representable PK; no message_date, no
    # token/approval references; cursor advanced exactly once, saturated at the
    # max (never 2**63).
    row = env.core._store.get_update_log(boundary)
    assert row is not None and row["outcome"] == "MALFORMED"
    assert row["message_date"] is None
    assert row["challenge_id"] is None
    assert row["approval_id"] is None
    assert row["decision"] is None
    assert env.core._store.get_inbound_state()["next_update_id"] == boundary
    # No token lookup, no gate change.
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.ISSUED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.PENDING
    # Replay: exactly one log row, DUPLICATE no-op.
    assert _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                 update_id=boundary,
                 message_date=MSG_DATE) is CallbackOutcome.DUPLICATE_UPDATE
    rows = [dict(r) for r in env.core._store._conn.execute(
        "SELECT * FROM telegram_update_log WHERE update_id = ?",
        (boundary,)).fetchall()]
    assert len(rows) == 1


def test_update_id_sqlite_max_minus_one_processes_normally(env, chal):
    challenge, raw = chal
    processor = _processor(env)
    uid = 2**63 - 2
    outcome = _call(processor, action=CallbackAction.APPROVE, challenge=raw,
                    update_id=uid, message_date=MSG_DATE)
    assert outcome is CallbackOutcome.APPROVED
    log = env.core._store.get_update_log(uid)
    assert log is not None and log["outcome"] == "APPROVED"
    assert env.core._store.get_challenge(challenge.id)["status"] == ChallengeStatus.CONSUMED_APPROVED.value
    assert env.core._store.get_approval(env.approval.id).status is ApprovalStatus.APPROVED
    assert env.core._store.get_inbound_state()["next_update_id"] == 2**63 - 1
