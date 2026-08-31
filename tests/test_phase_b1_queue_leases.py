"""Phase B1 durable-queue / job-lease / epoch-fencing tests (offline, deterministic).

Covers (A) queue ordering/eligibility/terminal rules, (B) atomic claim + the
dual-supervisor exactly-one-winner guarantee, (C) epoch + safe takeover,
(D) renewal rules, (E) fencing a stale owner on the real reconcile commit path,
(F) sticky-terminal invariants, (G) restart persistence, (H) additive migration
(+ idempotence, row preservation) and the retry/backoff metadata base.

All time is controlled via ``FakeClock``; there is no sleep, no network and no
real OpenClaw runtime.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from argent_core import Core, OWNER_SOURCE, Role, role_source
from argent_core.job_state import PrimaryState
from argent_core.models import LeaseError, LeaseFencedError
from argent_core.supervisor import Supervisor
from argent_core.store import MAX_LEASE_TTL_SECONDS, SCHEMA_VERSION, _format_dt
from mock_supervisor_runtime import FakeClock, FakeRunStatusProvider

OWNER = OWNER_SOURCE
LEAD = role_source(Role.LEAD)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_env(db_path, clock=None):
    """A Core + Supervisor over a temp DB with one project (no job yet)."""
    clock = clock or FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    prov = FakeRunStatusProvider()
    sup = Supervisor(core, prov, clock=clock)
    return SimpleNamespace(core=core, project=project, prov=prov, sup=sup,
                           clock=clock)


def add_queued_job(env, title, *, priority=0, next_eligible_at=None,
                   queue_reason="NEW", idem=None):
    """Create a task + supervisor job and enqueue it as QUEUED."""
    task = env.core.create_task(env.project.id, title, OWNER)
    job = env.sup.store.create_job(
        task.id, idempotency_key=idem or f"job-{task.id}"
    )
    env.core._store.enqueue_job(
        job.supervisor_job_id,
        queue_reason=queue_reason,
        priority=priority,
        next_eligible_at=next_eligible_at,
    )
    return job.supervisor_job_id


def job_row(core, job_id):
    return core._store.get_supervisor_job(job_id)


def set_terminal(core, job_id, terminal):
    """Close a job to a sticky terminal state (DONE/FAILED/BLOCKED)."""
    core._store._update_supervisor_job(
        job_id, status="TERMINAL", terminal=terminal, next_action="NONE"
    )
    return job_row(core, job_id)


# ---------------------------------------------------------------------------
# A. Queue
# ---------------------------------------------------------------------------

def test_queue_fifo_claim_order(db_path):
    env = make_env(db_path)
    ids = [add_queued_job(env, f"t{i}") for i in range(3)]
    claimed = []
    for _ in range(3):
        j = env.core._store.claim_next_job(owner_instance_id="A", ttl_seconds=60)
        claimed.append(j["id"])
    assert claimed == ids
    # Nothing left to claim.
    assert env.core._store.claim_next_job(owner_instance_id="A", ttl_seconds=60) is None
    env.core.close()


def test_queue_priority_order(db_path):
    env = make_env(db_path)
    low = add_queued_job(env, "low", priority=0)
    high = add_queued_job(env, "high", priority=10)
    mid = add_queued_job(env, "mid", priority=5)
    claimed = []
    for _ in range(3):
        claimed.append(
            env.core._store.claim_next_job(owner_instance_id="A", ttl_seconds=60)["id"]
        )
    assert claimed == [high, mid, low]
    env.core.close()


def test_queue_respects_next_eligible_at(db_path):
    env = make_env(db_path)
    future = _format_dt(env.clock() + timedelta(seconds=100))
    jid = add_queued_job(env, "deferred", next_eligible_at=future)
    # Not eligible yet -> no claim.
    assert env.core._store.claim_next_job(owner_instance_id="A", ttl_seconds=60) is None
    with pytest.raises(LeaseError):
        env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=60)
    # After the eligibility time passes -> claimable.
    env.clock.advance(101)
    claimed = env.core._store.claim_next_job(owner_instance_id="A", ttl_seconds=60)
    assert claimed["id"] == jid
    env.core.close()


def test_terminal_jobs_not_claimable(db_path):
    env = make_env(db_path)
    done = add_queued_job(env, "done")
    failed = add_queued_job(env, "failed")
    blocked = add_queued_job(env, "blocked")
    set_terminal(env.core, done, "DONE")
    set_terminal(env.core, failed, "FAILED")
    set_terminal(env.core, blocked, "BLOCKED")
    for jid in (done, failed, blocked):
        assert env.core._store.get_supervisor_job(jid)["primary_state"] in (
            "DONE", "FAILED", "BLOCKED"
        )
        with pytest.raises(LeaseError):
            env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=60)
    assert env.core._store.claim_next_job(owner_instance_id="A", ttl_seconds=60) is None
    env.core.close()


# ---------------------------------------------------------------------------
# B. Claim (atomic + dual supervisor)
# ---------------------------------------------------------------------------

def test_claim_is_atomic_and_sets_lease(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env, "job")
    claimed = env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=100)
    assert claimed["primary_state"] == "RUNNING"
    assert claimed["status"] == "ACTIVE"
    assert claimed["owner_instance_id"] == "A"
    assert claimed["lease_epoch"] == 1
    assert claimed["lease_expires_at"] is not None
    # Second claim while lease is active fails.
    with pytest.raises(LeaseError):
        env.core._store.claim_job(jid, owner_instance_id="B", ttl_seconds=100)
    env.core.close()


def test_dual_supervisor_exactly_one_wins(db_path):
    """Two supervisor instances (two connections) on one DB: exactly one claim."""
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    jid = add_queued_job(env, "job")
    # Second, independent supervisor instance over the same DB file.
    core2 = Core(db_path, clock=clock)
    try:
        winner = env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=60)
        assert winner["owner_instance_id"] == "A"
        with pytest.raises(LeaseError):
            core2._store.claim_job(jid, owner_instance_id="B", ttl_seconds=60)
        final = env.core._store.get_supervisor_job(jid)
        assert final["owner_instance_id"] == "A"
        assert final["lease_epoch"] == 1
    finally:
        core2.close()
        env.core.close()


def test_dual_supervisor_concurrent_claim(db_path):
    """Genuine concurrency: two threads, two connections, one job -> one winner."""
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    jid = add_queued_job(env, "job")
    env.core.close()  # release the writer connection

    results: list[tuple] = []
    barrier = threading.Barrier(2, timeout=10)
    lock = threading.Lock()

    def worker(name):
        c = Core(db_path, clock=FakeClock())
        barrier.wait()
        try:
            c._store.claim_job(jid, owner_instance_id=name, ttl_seconds=60)
            outcome = ("ok", name)
        except LeaseError:
            outcome = ("fail", name)
        finally:
            c.close()
        with lock:
            results.append(outcome)

    t1 = threading.Thread(target=worker, args=("instance-A",))
    t2 = threading.Thread(target=worker, args=("instance-B",))
    t1.start(); t2.start(); t1.join(); t2.join()

    oks = [r for r in results if r[0] == "ok"]
    fails = [r for r in results if r[0] == "fail"]
    assert len(oks) == 1, f"expected exactly one winner, got {results}"
    assert len(fails) == 1, f"expected exactly one loser, got {results}"
    # The single winner holds epoch 1 and is the only owner.
    c = Core(db_path, clock=clock)
    try:
        final = c._store.get_supervisor_job(jid)
        assert final["owner_instance_id"] == oks[0][1]
        assert final["lease_epoch"] == 1
    finally:
        c.close()


# ---------------------------------------------------------------------------
# C. Epoch + safe takeover
# ---------------------------------------------------------------------------

def test_epoch_and_safe_takeover(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env, "job")
    first = env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=30)
    assert first["lease_epoch"] == 1
    # Active lease blocks a rival claim.
    with pytest.raises(LeaseError):
        env.core._store.claim_job(jid, owner_instance_id="B", ttl_seconds=30)
    # Expire the lease.
    env.clock.advance(31)
    # F1: a RUNNING job is NEVER claimed directly; takeover goes through the
    # evidence-bound recovery path (no process evidence + no worktree binding
    # -> the takeover proceeds, epoch+1).
    second = env.core._store.recover_takeover_job(
        jid, expected=job_row(env.core, jid), owner_instance_id="B",
        ttl_seconds=30, process_alive=False, worktree_verdict=None,
    )
    assert second["lease_epoch"] == 2
    assert second["owner_instance_id"] == "B"
    # The old (owner, epoch) is fenced.
    with pytest.raises(LeaseFencedError):
        env.core._store.assert_lease_current(jid, "A", 1)
    assert env.core._store.lease_is_current(jid, "B", 2) is True
    assert env.core._store.lease_is_current(jid, "A", 1) is False
    env.core.close()


# ---------------------------------------------------------------------------
# D. Renewal
# ---------------------------------------------------------------------------

def test_renewal_rules(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env, "job")
    env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=30)
    before = job_row(env.core, jid)["lease_expires_at"]
    env.clock.advance(10)
    # Valid owner+epoch renews and extends expiry.
    renewed = env.core._store.renew_lease(
        jid, owner_instance_id="A", lease_epoch=1, ttl_seconds=60
    )
    assert renewed["lease_expires_at"] > before
    # Wrong owner cannot renew.
    with pytest.raises(LeaseError):
        env.core._store.renew_lease(
            jid, owner_instance_id="B", lease_epoch=1, ttl_seconds=60
        )
    # Stale epoch cannot renew.
    with pytest.raises(LeaseError):
        env.core._store.renew_lease(
            jid, owner_instance_id="A", lease_epoch=0, ttl_seconds=60
        )
    # Expired lease cannot be silently extended.
    env.clock.advance(3600)
    with pytest.raises(LeaseError):
        env.core._store.renew_lease(
            jid, owner_instance_id="A", lease_epoch=1, ttl_seconds=60
        )
    env.core.close()


# ---------------------------------------------------------------------------
# E. Fencing on the real mutating supervisor path
# ---------------------------------------------------------------------------

def test_fencing_stale_owner_cannot_commit(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env, "job")
    env.core._store.claim_job(jid, owner_instance_id="instance-A", ttl_seconds=30)
    env.clock.advance(31)
    # Expire the lease.
    env.clock.advance(31)
    # F1: takeover of RUNNING goes through the recovery path (never claim_job).
    env.core._store.recover_takeover_job(
        jid, expected=job_row(env.core, jid), owner_instance_id="instance-B",
        ttl_seconds=30, process_alive=False, worktree_verdict=None,
    )
    assert job_row(env.core, jid)["lease_epoch"] == 2

    # The stale owner A (epoch 1) attempts to commit through the reconcile path.
    env.sup.set_lease_owner("instance-A", 1)
    with pytest.raises(LeaseFencedError):
        env.sup.reconcile(jid)
    # No mutation was committed.  ``facts_version`` was already bumped by the
    # initial enqueue + the two claims (F1 invalidates on every claim/requeue):
    # 0 -> 1 (enqueue) -> 2 (claim A) -> 3 (claim B).
    assert job_row(env.core, jid)["facts_version"] == 3

    # The current owner B (epoch 2) can commit.
    env.sup.set_lease_owner("instance-B", 2)
    decision = env.sup.reconcile(jid)
    assert decision is not None
    assert job_row(env.core, jid)["facts_version"] == 4
    env.core.close()


def test_fencing_expired_lease_blocks_commit(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env, "job")
    env.core._store.claim_job(jid, owner_instance_id="instance-A", ttl_seconds=30)
    env.clock.advance(31)
    # Owner still matches + epoch still matches, but the lease has expired.
    env.sup.set_lease_owner("instance-A", 1)
    with pytest.raises(LeaseFencedError):
        env.sup.reconcile(jid)
    # No mutation: facts_version was bumped by the enqueue + claim (0->1->2).
    assert job_row(env.core, jid)["facts_version"] == 2
    env.core.close()


# ---------------------------------------------------------------------------
# F. Terminal invariants
# ---------------------------------------------------------------------------

def test_done_is_sticky_never_claimable(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env, "job")
    set_terminal(env.core, jid, "DONE")
    with pytest.raises(LeaseError):
        env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=60)
    # Even after time passes, DONE is never claimable again.
    env.clock.advance(100000)
    with pytest.raises(LeaseError):
        env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=60)
    assert env.core._store.claim_next_job(owner_instance_id="A", ttl_seconds=60) is None
    env.core.close()


def test_failed_is_not_autonomously_reopened(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env, "job")
    set_terminal(env.core, jid, "FAILED")
    with pytest.raises(LeaseError):
        env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=60)
    env.core.close()


def test_blocked_not_normally_claimable(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env, "job")
    set_terminal(env.core, jid, "BLOCKED")
    with pytest.raises(LeaseError) as exc:
        env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=60)
    assert "not_claimable:BLOCKED" in str(exc.value)
    env.core.close()


# ---------------------------------------------------------------------------
# G. Restart persistence
# ---------------------------------------------------------------------------

def test_restart_preserves_queue_and_lease(db_path):
    clock = FakeClock()
    env = make_env(db_path, clock=clock)
    jid1 = add_queued_job(env, "one")
    jid2 = add_queued_job(env, "two")
    env.core._store.claim_job(jid1, owner_instance_id="A", ttl_seconds=100)
    env.core.close()

    # Reopen the SAME DB: no in-memory cache authority.
    core2 = Core(db_path, clock=clock)
    try:
        j1 = core2._store.get_supervisor_job(jid1)
        assert j1["primary_state"] == "RUNNING"
        assert j1["owner_instance_id"] == "A"
        assert j1["lease_epoch"] == 1
        j2 = core2._store.get_supervisor_job(jid2)
        assert j2["primary_state"] == "QUEUED"
        # The queued job is still claimable after restart.
        claimed = core2._store.claim_next_job(owner_instance_id="B", ttl_seconds=60)
        assert claimed["id"] == jid2
    finally:
        core2.close()


# ---------------------------------------------------------------------------
# H. Schema / migration
# ---------------------------------------------------------------------------

_OLD_SUPERVISOR_JOBS_DDL = """
CREATE TABLE supervisor_jobs (
    id                    TEXT PRIMARY KEY,
    task_id               TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    status                TEXT NOT NULL CHECK (status IN
                          ('ACTIVE','WAITING_RUN','WAITING_GATE','BACKOFF',
                           'RECOVERING','ERROR','TERMINAL')),
    workflow_state        TEXT NOT NULL,
    expected_role         TEXT,
    expected_dispatch_id  TEXT REFERENCES agent_dispatches(id),
    agent_id              TEXT,
    session_id            TEXT,
    run_id                TEXT,
    attempt_no            INTEGER NOT NULL DEFAULT 0,
    dispatch_status       TEXT,
    result_status         TEXT NOT NULL DEFAULT 'NOT_OBSERVED',
    result_consumed       INTEGER NOT NULL DEFAULT 0,
    current_handoff_id    TEXT,
    open_findings_count   INTEGER NOT NULL DEFAULT 0,
    rework_cycle          INTEGER NOT NULL DEFAULT 1,
    recovery_state        TEXT NOT NULL DEFAULT 'NONE',
    owner_gate_id         TEXT REFERENCES owner_approvals(id),
    gate_status           TEXT,
    gate_scope            TEXT,
    gate_closed           INTEGER NOT NULL DEFAULT 0,
    owner_prompted_at     TEXT,
    owner_prompted_gate_id TEXT,
    next_action           TEXT NOT NULL DEFAULT 'NONE',
    next_wake_at          TEXT,
    retry_count           INTEGER NOT NULL DEFAULT 0,
    missing_confirmations INTEGER NOT NULL DEFAULT 0,
    last_error_code       TEXT,
    last_progress_at      TEXT NOT NULL,
    terminal              TEXT CHECK (terminal IS NULL OR terminal IN
                          ('DONE','FAILED','BLOCKED')),
    facts_version         INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
)
"""


def _build_pre_b1_db(path: str) -> None:
    """Build a minimal V2C-style DB (no B1 columns) with two job rows."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('schema_version', '6')"
    )
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
        "title TEXT NOT NULL, description TEXT, state TEXT NOT NULL, "
        "resume_state TEXT, source TEXT NOT NULL, source_class TEXT NOT NULL, "
        "risk_class TEXT NOT NULL DEFAULT 'NORMAL', "
        "external_actions_policy TEXT NOT NULL DEFAULT 'ALLOWED_WITH_GATE', "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "idempotency_key TEXT UNIQUE)"
    )
    conn.execute(_OLD_SUPERVISOR_JOBS_DDL)
    conn.execute(
        "INSERT INTO tasks (id, project_id, title, state, source, source_class, "
        "created_at, updated_at) VALUES ('t1', 'p1', 'x', 'NEW', 'owner', "
        "'OWNER', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    # Two pre-B1 rows: one ACTIVE (-> RUNNING) and one terminal DONE.
    conn.execute(
        "INSERT INTO supervisor_jobs (id, task_id, status, workflow_state, "
        "result_status, result_consumed, recovery_state, next_action, "
        "last_progress_at, facts_version, created_at, updated_at) VALUES "
        "('j1', 't1', 'ACTIVE', 'NEW', 'NOT_OBSERVED', 0, 'NONE', 'NONE', "
        "'2026-01-01T00:00:00+00:00', 0, '2026-01-01T00:00:00+00:00', "
        "'2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO supervisor_jobs (id, task_id, status, workflow_state, "
        "result_status, result_consumed, recovery_state, next_action, "
        "last_progress_at, terminal, facts_version, created_at, updated_at) VALUES "
        "('j2', 't1', 'TERMINAL', 'NEW', 'NOT_OBSERVED', 0, 'NONE', 'NONE', "
        "'2026-01-01T00:00:00+00:00', 'DONE', 0, "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    conn.close()


def test_migration_adds_columns_backfills_and_preserves_rows(tmp_path):
    db = str(tmp_path / "pre_b1.db")
    _build_pre_b1_db(db)

    core = Core(db)
    try:
        cols = {
            r[1] for r in core._store._conn.execute(
                "PRAGMA table_info(supervisor_jobs)"
            )
        }
        for c in (
            "primary_state", "queue_reason", "priority", "owner_instance_id",
            "lease_epoch", "lease_expires_at", "next_eligible_at",
            "error_class", "wait_kind",
        ):
            assert c in cols, f"missing migrated column {c}"

        j1 = core._store.get_supervisor_job("j1")
        j2 = core._store.get_supervisor_job("j2")
        # Backfill derived primary_state from the projection fields.  F3: a
        # legacy pre-B1 ACTIVE row has no lease, so it bootstraps to QUEUED
        # (never a lease-less RUNNING).
        assert j1["primary_state"] == "QUEUED"   # legacy ACTIVE -> QUEUED bootstrap
        assert j2["primary_state"] == "DONE"      # terminal=DONE
        # Existing rows preserved (id/task_id untouched).
        assert j1["id"] == "j1" and j1["task_id"] == "t1"
        assert j2["id"] == "j2" and j2["task_id"] == "t1"

        row = core._store._conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        assert row["value"] == SCHEMA_VERSION
    finally:
        core.close()


def test_migration_reopen_is_idempotent(tmp_path):
    db = str(tmp_path / "pre_b1.db")
    _build_pre_b1_db(db)

    c1 = Core(db)
    c1.close()
    # Reopening the already-migrated DB must be a no-op (no error, same data).
    c2 = Core(db)
    try:
        assert c2._store.get_supervisor_job("j1")["primary_state"] == "QUEUED"
        row = c2._store._conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        assert row["value"] == SCHEMA_VERSION
    finally:
        c2.close()


# ---------------------------------------------------------------------------
# Retry/backoff metadata base (Phase B1 §5/§6/§9)
# ---------------------------------------------------------------------------

def test_retry_backoff_metadata(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env, "job")
    # Simulate a classified retryable failure -> QUEUED with backoff metadata.
    env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=30)
    future = _format_dt(env.clock() + timedelta(seconds=30))
    row = env.core._store.enqueue_job(
        jid,
        queue_reason="RETRY_BACKOFF",
        next_eligible_at=future,
        error_class="TRANSIENT",
        error_code="timeout",
        bump_attempt=True,
        # F2: holder-requeue from RUNNING requires the current (owner, epoch)
        # CAS — a foreign lease must never be silently removed.
        owner_instance_id="A",
        lease_epoch=1,
    )
    assert row["primary_state"] == "QUEUED"
    assert row["status"] == "BACKOFF"
    assert row["queue_reason"] == "RETRY_BACKOFF"
    assert row["next_eligible_at"] == future
    assert row["error_class"] == "TRANSIENT"
    assert row["last_error_code"] == "timeout"
    assert row["attempt_no"] == 1
    assert row["owner_instance_id"] is None
    # Still not claimable until eligible again.
    with pytest.raises(LeaseError):
        env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=30)
    env.clock.advance(31)
    claimed = env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=30)
    assert claimed["primary_state"] == "RUNNING"
    assert claimed["lease_epoch"] == 2
    env.core.close()


# ---------------------------------------------------------------------------
# Fix-round tests (F1-F7, Sol REJECT findings)
# ---------------------------------------------------------------------------

# --- F1: fencing ends the stale holder's action AFTER takeover -------------

def test_f1_race_takeover_fences_stale_action(db_path):
    """A reconciles (decision authored) -> lease expires -> B takes over
    (epoch+1, facts_version+1) -> A executes its decision -> fenced, nothing
    written."""
    env = make_env(db_path)
    jid = add_queued_job(env, "job")
    # A claims (epoch 1) and authors a decision under that lease.
    env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=30)
    env.sup.set_lease_owner("A", 1)
    decision = env.sup.reconcile(jid)
    assert decision is not None
    assert decision.owner_instance_id == "A"
    assert decision.lease_epoch == 1
    # Lease expires; B takes over (epoch 2, facts_version bumped by the claim).
    env.clock.advance(31)
    env.core._store.recover_takeover_job(
        jid, expected=job_row(env.core, jid), owner_instance_id="B",
        ttl_seconds=30, process_alive=False, worktree_verdict=None,
    )
    assert job_row(env.core, jid)["lease_epoch"] == 2
    facts_after_takeover = job_row(env.core, jid)["facts_version"]
    # A (stale holder, epoch 1) tries to execute its decision -> fenced.
    with pytest.raises(LeaseFencedError):
        env.sup.perform_next_safe_action_if_required(decision)
    # NOTHING was written by A.
    assert job_row(env.core, jid)["facts_version"] == facts_after_takeover
    assert job_row(env.core, jid)["owner_instance_id"] == "B"
    env.core.close()


# --- F2: enqueue_job guards (foreign lease / BLOCKED / terminal) -----------

def test_f2_foreign_lease_cannot_be_requeued(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env, "job")
    env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=30)
    # No holder CAS -> refused (a foreign lease is never silently removed).
    with pytest.raises(LeaseError):
        env.core._store.enqueue_job(jid, queue_reason="RETRY_BACKOFF")
    # Wrong owner CAS -> refused.
    with pytest.raises(LeaseError):
        env.core._store.enqueue_job(
            jid, queue_reason="RETRY_BACKOFF",
            owner_instance_id="B", lease_epoch=1,
        )
    # Lease still intact.
    j = job_row(env.core, jid)
    assert j["owner_instance_id"] == "A"
    assert j["primary_state"] == "RUNNING"
    env.core.close()


def test_f2_blocked_requeue_requires_authorization(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env, "job")
    set_terminal(env.core, jid, "BLOCKED")
    # Default -> refused (no automatic BLOCKED reopen).
    with pytest.raises(LeaseError):
        env.core._store.enqueue_job(jid, queue_reason="RECOVERY")
    # owner_authorized without a policy_ref -> refused.
    with pytest.raises(LeaseError):
        env.core._store.enqueue_job(
            jid, queue_reason="RECOVERY", owner_authorized=True,
        )
    # Authorized with policy_ref -> reopens to QUEUED (terminal cleared).
    row = env.core._store.enqueue_job(
        jid, queue_reason="RECOVERY", owner_authorized=True,
        policy_ref="owner:approved:reopen-123",
    )
    assert row["primary_state"] == "QUEUED"
    assert row["terminal"] is None
    assert job_row(env.core, jid)["primary_state"] == "QUEUED"
    env.core.close()


def test_f2_terminal_requeue_is_domain_error(db_path):
    env = make_env(db_path)
    done = add_queued_job(env, "done")
    failed = add_queued_job(env, "failed")
    set_terminal(env.core, done, "DONE")
    set_terminal(env.core, failed, "FAILED")
    for jid in (done, failed):
        with pytest.raises(LeaseError):
            env.core._store.enqueue_job(jid, queue_reason="NEW")
        # Even explicitly authorized, DONE/FAILED are sticky (domain error).
        with pytest.raises(LeaseError):
            env.core._store.enqueue_job(
                jid, queue_reason="NEW", owner_authorized=True, policy_ref="x",
            )
    env.core.close()


# --- F3: canonical job path respects queue/lease ---------------------------

def test_f3_create_job_starts_queued(db_path):
    env = make_env(db_path)
    task = env.core.create_task(env.project.id, "job", OWNER)
    job = env.sup.store.create_job(task.id, idempotency_key="job-1")
    row = job_row(env.core, job.supervisor_job_id)
    assert row["primary_state"] == "QUEUED"
    assert row["status"] == "WAITING_RUN"
    assert row["queue_reason"] == "NEW"
    assert row["owner_instance_id"] is None
    assert row["lease_epoch"] == 0
    assert row["lease_expires_at"] is None
    env.core.close()


def test_f3_loop_claims_before_work(db_path):
    from argent_core.supervisor import SupervisorLoop
    env = make_env(db_path)
    jid = add_queued_job(env, "job")
    loop = SupervisorLoop(env.sup, owner_instance_id="loop-A",
                          lease_ttl_seconds=60)
    loop.run_once(jid)
    row = job_row(env.core, jid)
    assert row["primary_state"] == "RUNNING"
    assert row["owner_instance_id"] == "loop-A"
    assert row["lease_epoch"] == 1
    assert row["lease_expires_at"] is not None
    env.core.close()


def test_f3_null_expiry_run_is_not_takeoverable(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env, "job")
    # Force a lease-less RUNNING row (legacy/unleased) with NULL expiry.
    env.core._store._update_supervisor_job(
        jid, primary_state="RUNNING", status="ACTIVE",
    )
    # Fail-closed: no concrete expiry -> NO takeover.
    with pytest.raises(LeaseError) as exc:
        env.core._store.claim_job(jid, owner_instance_id="B", ttl_seconds=30)
    assert "running_not_claimable" in str(exc.value)
    env.core.close()


# --- F4: primary-state projection invariant --------------------------------

def test_f4_derive_unknown_status_fails_closed_to_blocked():
    from argent_core.job_state import derive_primary_state
    assert derive_primary_state("BOGUS_STATUS") is PrimaryState.BLOCKED


def test_f4_e2e_lifecycle_primary_state_invariant(tmp_path):
    from argent_core.sandbox_runner import SandboxResult
    from argent_core.supervisor import SupervisorLoop
    from mock_supervisor_runtime import AutoRunStatusProvider, FakeWaiter

    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True, exist_ok=True)
    (ws / "tests").mkdir(parents=True, exist_ok=True)
    (ws / "src" / "module.py").write_text("# stub\n")

    def fake_run_tests(workspace, pytest_args=None, limits=None):
        return SandboxResult(exit_code=0, stdout_bounded="", stderr_bounded="",
                             timed_out=False, wall_seconds=0.0)

    clock = FakeClock()
    db = str(tmp_path / "lifecycle.db")
    core = Core(db, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    core.start_task_run(task.id, OWNER)
    prov = AutoRunStatusProvider(core)
    sup = Supervisor(core, prov, clock=clock, workspace_root=str(ws),
                     run_tests_fn=fake_run_tests)
    job = sup.store.create_job(task.id, idempotency_key="job-1")
    jid = job.supervisor_job_id
    assert core._store.get_supervisor_job(jid)["primary_state"] == "QUEUED"

    loop = SupervisorLoop(sup, waiter=FakeWaiter(clock),
                          owner_instance_id="loop-A", lease_ttl_seconds=3600)
    for _ in range(120):
        loop.run_once(jid)
        st = sup.store.get_job(jid)
        if st.terminal is not None:
            break
        # F4 invariant: once leased (actively worked), primary_state never QUEUED.
        if st.owner_instance_id is not None:
            assert st.primary_state != PrimaryState.QUEUED.value, \
                f"primary_state drifted to QUEUED during active execution: {st}"
    final = sup.store.get_job(jid)
    assert final.terminal == "DONE"
    core.close()


# --- F5: expiry fail-closed -------------------------------------------------

def test_f5_expired_release_refused(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env, "job")
    env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=30)
    env.clock.advance(31)
    # An expired holder must not be able to release (mutate) the lease.
    with pytest.raises(LeaseError):
        env.core._store.release_lease(jid, owner_instance_id="A", lease_epoch=1)
    j = job_row(env.core, jid)
    assert j["owner_instance_id"] == "A"
    assert j["primary_state"] == "RUNNING"
    env.core.close()


def test_f5_null_expiry_leased_job_is_fenced(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env, "job")
    # Force a leased RUNNING row with a NULL lease_expires_at (inconsistent).
    env.core._store._update_supervisor_job(
        jid, primary_state="RUNNING", status="ACTIVE",
        owner_instance_id="A", lease_epoch=1,
    )
    env.sup.set_lease_owner("A", 1)
    with pytest.raises(LeaseFencedError):
        env.sup.reconcile(jid)
    assert job_row(env.core, jid)["facts_version"] == 1  # unchanged
    env.core.close()


# --- F6: state/metadata validation -----------------------------------------

def test_f6_enum_validation_rejects_invalid_values(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env, "job")
    with pytest.raises(ValueError):
        env.core._store.enqueue_job(jid, queue_reason="BOGUS")
    with pytest.raises(ValueError):
        env.core._store.enqueue_job(jid, wait_kind="BOGUS")
    with pytest.raises(ValueError):
        env.core._store.enqueue_job(jid, error_class="BOGUS")
    # Still QUEUED and intact after all rejected writes.
    assert job_row(env.core, jid)["primary_state"] == "QUEUED"
    env.core.close()


def test_f6_terminal_stickiness_blocks_reopen(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env, "job")
    set_terminal(env.core, jid, "DONE")
    with pytest.raises(LeaseError):
        env.core._store._update_supervisor_job(
            jid, terminal=None, status="WAITING_RUN",
        )
    assert job_row(env.core, jid)["terminal"] == "DONE"
    env.core.close()


def test_f6_cross_consistency_enforced(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env, "job")
    # The central transition primitive rejects a status that does not project
    # to the target primary_state (WAITING_RUN does not project to RUNNING).
    with pytest.raises(ValueError):
        env.core._store._transition_job(
            jid, to_primary_state="RUNNING", to_status="WAITING_RUN",
        )
    env.core.close()


# --- F7: defensive CAS / input validation ----------------------------------

def test_f7_ttl_bounds_and_owner_validation(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env, "job")
    for bad_ttl in (0, -5, MAX_LEASE_TTL_SECONDS + 1):
        with pytest.raises(ValueError):
            env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=bad_ttl)
    with pytest.raises(ValueError):
        env.core._store.claim_job(jid, owner_instance_id="", ttl_seconds=30)
    # Nothing was claimed by any invalid attempt.
    assert job_row(env.core, jid)["owner_instance_id"] is None
    env.core.close()


def test_f7_cas_precondition_failures(db_path):
    env = make_env(db_path)
    jid = add_queued_job(env, "job")
    env.core._store.claim_job(jid, owner_instance_id="A", ttl_seconds=30)
    # Release with a stale epoch -> LeaseError (CAS precondition).
    with pytest.raises(LeaseError):
        env.core._store.release_lease(jid, owner_instance_id="A", lease_epoch=99)
    # Renew with a foreign owner -> LeaseError.
    with pytest.raises(LeaseError):
        env.core._store.renew_lease(
            jid, owner_instance_id="B", lease_epoch=1, ttl_seconds=30,
        )
    # Lease still intact and held by A.
    assert job_row(env.core, jid)["owner_instance_id"] == "A"
    env.core.close()

