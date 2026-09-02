"""Phase 3C-A owner-approval challenge core tests (SPEC V3C §7/§8, A1/A2).

Offline and deterministic: temp DB, fake clock, no network, no agents, no
Telegram.  Verifies the V6 schema, token generation, challenge creation,
the terminal challenge state machine, the strict callback parser, the
update-id dedup log and the restart-fixed inbound cursor.

No realistic secret strings are used anywhere: token material is produced by
``secrets.token_urlsafe`` at runtime and is never asserted against a fixed
value; every fixed 64-char hash here is an obviously-fake placeholder.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from argent_core import Core, OWNER_SOURCE, Role, ApprovalError, ApprovalStatus, role_source
from argent_core.models import OwnerApproval, SourceClass
from argent_core.approval_core import (
    CHALLENGE_TTL_SECONDS,
    CallbackAction,
    Challenge,
    ChallengeStatus,
    challenge_id,
    challenge_from_row,
    consume_approved,
    consume_rejected,
    create_challenge,
    expire_challenge,
    generate_challenge_token,
    invalidate_challenge,
    parse_callback,
    token_hash,
)
from argent_core.gates import binding_hash
from argent_core.store import SCHEMA_VERSION

OWNER = OWNER_SOURCE
LEAD = role_source(Role.LEAD)

V6_TABLES = {"approval_challenges", "telegram_update_log", "telegram_inbound_state"}
V6_INDEXES = {
    "idx_approval_challenges_token_hash",
    "idx_approval_challenges_active_approval",
    "idx_approval_challenges_due",
    "idx_approval_challenges_approval",
    "idx_telegram_update_log_outcome",
    "idx_telegram_update_log_challenge",
}


class Clock:
    """Controllable deterministic clock (no sleep, no real time)."""

    def __init__(self, start=None):
        self.t = start or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


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


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    core = Core(str(tmp_path / "env.db"), clock=clock)
    project = core.create_project("demo", OWNER)
    task = core.create_task(project.id, "demo-task", OWNER)
    core.start_role(task.id, Role.LEAD, LEAD)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    approval = res.approval
    job_id = "job-1"
    core._store._insert_supervisor_job(_job_row(job_id, task.id, core._store.now_iso()))
    yield SimpleNamespace(core=core, task=task, approval=approval, job_id=job_id,
                          clock=clock)
    core.close()


def _table_names(core):
    return {
        r["name"] for r in core._store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _column_names(core, table):
    return {
        r[1] for r in core._store._conn.execute(f"PRAGMA table_info({table})")
    }


# ---------------------------------------------------------------------------
# Schema V6
# ---------------------------------------------------------------------------

def test_schema_version_is_15():
    # E3 (Phase E): schema 15 -> 16 (additive routing_decisions provenance).
    assert SCHEMA_VERSION == "16"


def test_fresh_db_has_v6_tables_and_version(db_path, core):
    row = core._store._conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    assert row["value"] == SCHEMA_VERSION
    assert V6_TABLES <= _table_names(core)


def test_v6_indexes_exist(db_path, core):
    idx = {
        r["name"] for r in core._store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    }
    assert V6_INDEXES <= idx


def test_foreign_key_check_empty(db_path, env):
    # A freshly-populated challenge graph must satisfy all FK constraints.
    create_challenge(env.core._store, approval=env.approval,
                     supervisor_job_id=env.job_id, now=env.clock.t)
    rows = env.core._store._conn.execute("PRAGMA foreign_key_check").fetchall()
    assert rows == []


def test_no_secret_columns(db_path, core):
    banned = (
        "secret", "credential", "password", "key", "chat_id", "chatid",
        "sender_id", "senderid", "user_id", "userid", "from_id", "chat_owner",
        "owner_chat", "owner_id", "owner_user", "api", "bearer", "bot",
    )
    for table in V6_TABLES:
        for name in _column_names(core, table):
            lc = name.lower()
            if "token" in lc:
                # Only the sha256 hash of a token may be persisted (A2).
                assert lc == "token_hash", f"raw token column {name!r} in {table}"
            for b in banned:
                assert b not in lc, f"secret-like column {name!r} in {table}"


def test_v5_to_v6_preserves_v5_data(db_path):
    # Build a V5 DB: drop the V6 tables, stamp version 5, keep V5 rows.
    core = Core(db_path)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    core.start_role(task.id, Role.LEAD, LEAD)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)
    conn = core._store._conn
    conn.execute("DROP TABLE telegram_update_log")
    conn.execute("DROP TABLE telegram_inbound_state")
    conn.execute("DROP TABLE approval_challenges")
    conn.execute("UPDATE schema_meta SET value = '5' WHERE key = 'schema_version'")
    core.close()

    core2 = Core(db_path)
    try:
        row = core2._store._conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        assert row["value"] == SCHEMA_VERSION
        assert V6_TABLES <= _table_names(core2)
        # V5 data intact.
        assert core2.queries.get_task(task.id) is not None
        assert core2.queries.get_project(project.id) is not None
        assert core2.queries.get_approval(res.approval.id) is not None
        # No challenge / update-log backfill for the pre-existing gate.
        assert core2._store.list_challenges() == []
        assert core2._store.get_update_log(0) is None
    finally:
        core2.close()


def test_v6_migration_rollback_on_error(db_path, monkeypatch):
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
        assert "approval_challenges" not in tables
        assert "telegram_update_log" not in tables
        assert "telegram_inbound_state" not in tables
        assert "projects" not in tables  # _SCHEMA DDL rolled back with migration
    finally:
        conn.close()

    core = Core(db_path)
    try:
        row = core._store._conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        assert row["value"] == SCHEMA_VERSION
        assert V6_TABLES <= _table_names(core)
    finally:
        core.close()


# ---------------------------------------------------------------------------
# CHECK constraints
# ---------------------------------------------------------------------------

def test_approval_challenges_status_check(env):
    conn = env.core._store._conn
    args = (env.approval.id, env.task.id, env.job_id, "b" * 64, "c" * 64)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO approval_challenges (id, approval_id, task_id, "
            "supervisor_job_id, binding_hash, token_hash, status, created_at, "
            "expires_at) VALUES ('c-x', ?, ?, ?, ?, ?, 'BOGUS', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T01:00:00+00:00')",
            args,
        )


def test_approval_challenges_expires_after_created_check(env):
    conn = env.core._store._conn
    args = (env.approval.id, env.task.id, env.job_id, "b" * 64, "c" * 64)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO approval_challenges (id, approval_id, task_id, "
            "supervisor_job_id, binding_hash, token_hash, status, created_at, "
            "expires_at) VALUES ('c-x', ?, ?, ?, ?, ?, 'ISSUED', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            args,
        )


def test_approval_challenges_status_consistency_check(env):
    conn = env.core._store._conn
    args = (env.approval.id, env.task.id, env.job_id, "b" * 64, "c" * 64)
    # ISSUED must have no consumed_at.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO approval_challenges (id, approval_id, task_id, "
            "supervisor_job_id, binding_hash, token_hash, status, created_at, "
            "expires_at, consumed_at) VALUES ('c-x', ?, ?, ?, ?, ?, 'ISSUED', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T01:00:00+00:00', "
            "'2026-01-01T00:30:00+00:00')",
            args,
        )
    # CONSUMED_APPROVED must have consumed_at + consumed_update_id.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO approval_challenges (id, approval_id, task_id, "
            "supervisor_job_id, binding_hash, token_hash, status, created_at, "
            "expires_at) VALUES ('c-y', ?, ?, ?, ?, ?, 'CONSUMED_APPROVED', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T01:00:00+00:00')",
            args,
        )


def test_telegram_update_log_checks(env):
    conn = env.core._store._conn
    now = env.core._store.now_iso()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO telegram_update_log (update_id, chat_authorized, "
            "sender_authorized, outcome, received_at, processed_at) "
            "VALUES (1, 1, 1, 'BOGUS', ?, ?)", (now, now),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO telegram_update_log (update_id, chat_authorized, "
            "sender_authorized, outcome, received_at, processed_at) "
            "VALUES (-1, 1, 1, 'APPROVED', ?, ?)", (now, now),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO telegram_update_log (update_id, chat_authorized, "
            "sender_authorized, outcome, received_at, processed_at) "
            "VALUES (2, 2, 1, 'APPROVED', ?, ?)", (now, now),
        )


# ---------------------------------------------------------------------------
# Token generation (A2)
# ---------------------------------------------------------------------------

def test_token_length_and_charset():
    token = generate_challenge_token()
    assert len(token) == 43
    urlsafe = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
    assert set(token) <= urlsafe


def test_token_hash_is_64_hex():
    token = generate_challenge_token()
    h = token_hash(token)
    assert len(h) == 64
    assert set(h) <= set("0123456789abcdef")


def test_tokens_differ_and_not_derivable_from_task(env):
    t1 = generate_challenge_token()
    t2 = generate_challenge_token()
    assert t1 != t2
    assert token_hash(t1) != token_hash(t2)
    # Two challenges for the SAME task produce different tokens/hashes.
    ap2 = OwnerApproval(
        id="approval-2", task_id=env.task.id, action="deploy_production",
        scope="staging", status=ApprovalStatus.PENDING, requested_by="lead",
        source_class=SourceClass.TRUSTED,
        created_at=env.core._store.now_iso(), decided_at=None, consumed_at=None,
        expires_at=env.core._store.expiry_iso(CHALLENGE_TTL_SECONDS),
    )
    env.core._store._insert_approval(ap2)
    ap2 = env.core._store.get_approval("approval-2")
    c1, raw1 = create_challenge(env.core._store, approval=env.approval,
                                supervisor_job_id=env.job_id, now=env.clock.t)
    c2, raw2 = create_challenge(env.core._store, approval=ap2,
                                supervisor_job_id=env.job_id, now=env.clock.t)
    assert raw1 != raw2
    assert c1.token_hash != c2.token_hash


# ---------------------------------------------------------------------------
# create_challenge
# ---------------------------------------------------------------------------

def test_create_challenge_happy_path(env):
    challenge, raw = create_challenge(env.core._store, approval=env.approval,
                                      supervisor_job_id=env.job_id,
                                      now=env.clock.t)
    assert challenge.status == ChallengeStatus.ISSUED.value
    assert challenge.id.startswith("challenge:")
    assert challenge.approval_id == env.approval.id
    assert challenge.task_id == env.task.id
    assert challenge.supervisor_job_id == env.job_id
    assert challenge.binding_hash == binding_hash(
        env.task.id, "deploy_production", "prod")
    assert challenge.token_hash == token_hash(raw)
    # raw token never persisted: stored row carries only the hash.
    row = env.core._store.get_challenge(challenge.id)
    assert row["token_hash"] == token_hash(raw)
    assert raw not in row.values()
    assert challenge.expires_at != challenge.created_at
    assert challenge.expires_at > challenge.created_at
    # persisted row round-trips
    assert challenge_from_row(row) == challenge


def test_create_challenge_expiry_min(env):
    now = env.clock.t
    challenge, _ = create_challenge(env.core._store, approval=env.approval,
                                    supervisor_job_id=env.job_id, now=now)
    # approval.expires_at == now + 3600 (default), so min == now + 3600.
    expected = (now + timedelta(seconds=CHALLENGE_TTL_SECONDS)).isoformat()
    assert challenge.expires_at == expected


def test_create_challenge_shorter_approval_expiry(env):
    now = env.clock.t
    shorter = (now + timedelta(seconds=1800)).isoformat()
    env.core._store._conn.execute(
        "UPDATE owner_approvals SET expires_at = ? WHERE id = ?",
        (shorter, env.approval.id),
    )
    approval = env.core._store.get_approval(env.approval.id)
    challenge, _ = create_challenge(env.core._store, approval=approval,
                                    supervisor_job_id=env.job_id, now=now)
    # challenge expires with the (shorter) approval, never beyond it.
    assert challenge.expires_at == shorter


def test_create_challenge_requires_pending(env):
    env.core.approve(env.approval.id, OWNER, task_id=env.task.id,
                     action="deploy_production", scope="prod")
    approval = env.core._store.get_approval(env.approval.id)
    assert approval.status is ApprovalStatus.APPROVED
    with pytest.raises(ApprovalError):
        create_challenge(env.core._store, approval=approval,
                         supervisor_job_id=env.job_id, now=env.clock.t)


def test_create_challenge_binding_mismatch(env):
    env.core._store._conn.execute(
        "UPDATE owner_approvals SET binding_hash = ? WHERE id = ?",
        ("0" * 64, env.approval.id),
    )
    approval = env.core._store.get_approval(env.approval.id)
    with pytest.raises(ApprovalError):
        create_challenge(env.core._store, approval=approval,
                         supervisor_job_id=env.job_id, now=env.clock.t)


def test_create_challenge_expired_approval(env):
    env.clock.advance(3601)  # past the 1h approval TTL
    with pytest.raises(ApprovalError):
        create_challenge(env.core._store, approval=env.approval,
                         supervisor_job_id=env.job_id, now=env.clock.t)
    assert env.core._store.list_challenges() == []


def test_create_challenge_duplicate_active_fails(env):
    create_challenge(env.core._store, approval=env.approval,
                     supervisor_job_id=env.job_id, now=env.clock.t)
    with pytest.raises(ApprovalError):
        create_challenge(env.core._store, approval=env.approval,
                         supervisor_job_id=env.job_id, now=env.clock.t)
    assert len(env.core._store.list_challenges()) == 1


# ---------------------------------------------------------------------------
# State machine (terminal, single-use)
# ---------------------------------------------------------------------------

def _fresh_challenge(env):
    challenge, raw = create_challenge(env.core._store, approval=env.approval,
                                      supervisor_job_id=env.job_id,
                                      now=env.clock.t)
    return challenge, raw


def test_consume_approved_transition(env):
    challenge, _ = _fresh_challenge(env)
    now = env.core._store.now_iso()
    assert consume_approved(env.core._store, challenge.id, consumed_at=now,
                            consumed_update_id=11) is True
    row = env.core._store.get_challenge(challenge.id)
    assert row["status"] == ChallengeStatus.CONSUMED_APPROVED.value
    assert row["consumed_at"] == now
    assert row["consumed_update_id"] == 11


def test_consume_rejected_transition(env):
    challenge, _ = _fresh_challenge(env)
    now = env.core._store.now_iso()
    assert consume_rejected(env.core._store, challenge.id, consumed_at=now,
                            consumed_update_id=12) is True
    assert env.core._store.get_challenge(challenge.id)["status"] == \
        ChallengeStatus.CONSUMED_REJECTED.value


def test_expire_transition(env):
    challenge, _ = _fresh_challenge(env)
    now = env.core._store.now_iso()
    assert expire_challenge(env.core._store, challenge.id, now_iso=now) is True
    row = env.core._store.get_challenge(challenge.id)
    assert row["status"] == ChallengeStatus.EXPIRED.value
    assert row["invalidated_at"] == now


def test_invalidate_transition(env):
    challenge, _ = _fresh_challenge(env)
    now = env.core._store.now_iso()
    assert invalidate_challenge(env.core._store, challenge.id, now_iso=now) is True
    row = env.core._store.get_challenge(challenge.id)
    assert row["status"] == ChallengeStatus.INVALIDATED.value
    assert row["invalidated_at"] == now


def test_double_consume_fails(env):
    challenge, _ = _fresh_challenge(env)
    now = env.core._store.now_iso()
    assert consume_approved(env.core._store, challenge.id, consumed_at=now,
                            consumed_update_id=21) is True
    assert consume_approved(env.core._store, challenge.id, consumed_at=now,
                            consumed_update_id=22) is False
    assert consume_rejected(env.core._store, challenge.id, consumed_at=now,
                            consumed_update_id=23) is False
    assert env.core._store.get_challenge(challenge.id)["consumed_update_id"] == 21


def test_terminal_states_cannot_reopen(env):
    # For every terminal target, no other transition can move it again.
    now = env.core._store.now_iso()
    for fn, terminal in (
        (lambda s, c: consume_approved(s, c, consumed_at=now, consumed_update_id=1),
         ChallengeStatus.CONSUMED_APPROVED.value),
        (lambda s, c: consume_rejected(s, c, consumed_at=now, consumed_update_id=2),
         ChallengeStatus.CONSUMED_REJECTED.value),
        (lambda s, c: expire_challenge(s, c, now_iso=now),
         ChallengeStatus.EXPIRED.value),
        (lambda s, c: invalidate_challenge(s, c, now_iso=now),
         ChallengeStatus.INVALIDATED.value),
    ):
        challenge, _ = _fresh_challenge(env)
        assert fn(env.core._store, challenge.id) is True
        assert env.core._store.get_challenge(challenge.id)["status"] == terminal
        # every transition away from a terminal state fails (fail-closed)
        assert consume_approved(env.core._store, challenge.id, consumed_at=now,
                                consumed_update_id=50) is False
        assert consume_rejected(env.core._store, challenge.id, consumed_at=now,
                                consumed_update_id=51) is False
        assert expire_challenge(env.core._store, challenge.id, now_iso=now) is False
        assert invalidate_challenge(env.core._store, challenge.id, now_iso=now) is False
        assert env.core._store.get_challenge(challenge.id)["status"] == terminal


# ---------------------------------------------------------------------------
# parse_callback
# ---------------------------------------------------------------------------

CH = "a" * 43  # a valid 43-char opaque challenge reference


def test_parse_callback_valid():
    assert parse_callback("A:" + CH) == (CallbackAction.APPROVE, CH)
    assert parse_callback("R:" + CH) == (CallbackAction.REJECT, CH)
    assert parse_callback("D:" + CH) == (CallbackAction.DETAILS, CH)


def test_parse_callback_malformed():
    cases = [
        "",                        # empty
        "X:" + CH,                 # wrong action
        "A:" + "a" * 42,           # too short
        "A:" + "a" * 44,           # too long
        "A:" + CH + "x",           # extra chars
        "A:" + CH + "\n",          # newline
        "a:" + CH,                 # lowercase action
        "A:" + "\u00e4" * 43,      # unicode
        "APPROVE " + CH,           # free-text command
        "/APPROVE " + CH,          # slash-prefixed
        "/A:" + CH,                # slash-prefixed action
        "A:" + "a" * 42 + "/",     # slash not in url-safe alphabet
        "A:" + "a" * 42 + "+",     # plus not in url-safe alphabet
        "A:" + "a" * 42 + "=",     # padding not allowed
        "A",                       # no colon
        "A:" + CH + ":extra",      # extra separator
        "A::" + CH,                # double colon
        None,                      # non-string
    ]
    for data in cases:
        assert parse_callback(data) is None, f"expected fail-closed for {data!r}"


# ---------------------------------------------------------------------------
# Update log dedup + inbound cursor
# ---------------------------------------------------------------------------

def test_update_log_dedup(db_path, core):
    now = core._store.now_iso()
    row = {
        "update_id": 7, "message_date": 123, "chat_authorized": 1,
        "sender_authorized": 1, "decision": None, "challenge_id": None,
        "approval_id": None, "outcome": "APPROVED", "received_at": now,
        "processed_at": now,
    }
    assert core._store._insert_update_log(row) is True
    assert core._store._insert_update_log(row) is False  # duplicate -> no-op
    got = core._store.get_update_log(7)
    assert got is not None and got["outcome"] == "APPROVED"
    assert got["update_id"] == 7


def test_cursor_persists_across_reopen(db_path):
    core = Core(db_path)
    s0 = core._store.get_inbound_state()
    assert s0 is not None and s0["stream_id"] == "telegram-owner-approval-v1"
    assert s0["next_update_id"] == 0
    core._store._set_inbound_state(41, core._store.now_iso())
    core.close()

    core2 = Core(db_path)
    try:
        s1 = core2._store.get_inbound_state()
        assert s1["next_update_id"] == 41
    finally:
        core2.close()
