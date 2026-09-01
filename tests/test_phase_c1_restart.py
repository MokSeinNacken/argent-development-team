"""Phase C1 — migration / reopen / restart acceptance (deterministic).

Proves the additive migration to the CURRENT schema version (C1 added the
resource-class + ``last_resource_*`` audit columns; C2 later bumps
``SCHEMA_VERSION`` and adds execution-scope columns), backfill of
``resource_class='LIGHT'``, idempotent reopen, and that a persisted
``last_resource_*`` decision NEVER authorises an automatic admission (a new
claim always re-runs preflight).
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from argent_core import Core, OWNER_SOURCE, Role, role_source
from argent_core.resource_governor import (
    AdmissionVerdict,
    ResourceReasonCode,
)
from argent_core.resource_policy import ResourceClass
from argent_core.scheduler import Scheduler
from argent_core.store import SCHEMA_VERSION
from argent_core.supervisor import Supervisor
from c1_helpers import make_snapshot
from mock_supervisor_runtime import FakeClock, FakeRunLauncher, FakeRunStatusProvider

OWNER = OWNER_SOURCE
LEAD = role_source(Role.LEAD)

#: V8 ``supervisor_jobs`` shape (all B1+B3 columns, NO C1 resource columns).
_V8_SUPERVISOR_JOBS_DDL = """
CREATE TABLE supervisor_jobs (
    id                    TEXT PRIMARY KEY,
    task_id               TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    status                TEXT NOT NULL CHECK (status IN
                          ('ACTIVE','WAITING_RUN','WAITING_GATE','BACKOFF',
                           'RECOVERING','ERROR','TERMINAL')),
    workflow_state        TEXT NOT NULL,
    expected_role         TEXT,
    expected_dispatch_id  TEXT,
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
    owner_gate_id         TEXT,
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
    primary_state         TEXT NOT NULL DEFAULT 'QUEUED',
    queue_reason          TEXT NOT NULL DEFAULT 'NEW',
    priority              INTEGER NOT NULL DEFAULT 0,
    owner_instance_id     TEXT,
    lease_epoch           INTEGER NOT NULL DEFAULT 0,
    lease_expires_at      TEXT,
    next_eligible_at      TEXT,
    error_class           TEXT NOT NULL DEFAULT 'NONE',
    wait_kind             TEXT NOT NULL DEFAULT 'NONE',
    canonical_worktree_path TEXT,
    repo_identity         TEXT,
    base_commit           TEXT,
    branch_identity       TEXT,
    writer_dispatch_id    TEXT,
    writer_owner_instance_id TEXT,
    writer_lease_epoch    INTEGER NOT NULL DEFAULT 0,
    writer_binding_mode   TEXT,
    expected_head         TEXT,
    current_head          TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
)
"""


def _build_v8_db(path: str) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('schema_version', '8')")
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
        "title TEXT NOT NULL, description TEXT, state TEXT NOT NULL, "
        "resume_state TEXT, source TEXT NOT NULL, source_class TEXT NOT NULL, "
        "risk_class TEXT NOT NULL DEFAULT 'NORMAL', "
        "external_actions_policy TEXT NOT NULL DEFAULT 'ALLOWED_WITH_GATE', "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "idempotency_key TEXT UNIQUE)")
    conn.execute(_V8_SUPERVISOR_JOBS_DDL)
    conn.execute(
        "INSERT INTO tasks (id, project_id, title, state, source, source_class, "
        "created_at, updated_at) VALUES ('t1', 'p1', 'x', 'NEW', 'owner', "
        "'OWNER', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')")
    conn.execute(
        "INSERT INTO supervisor_jobs (id, task_id, status, workflow_state, "
        "result_status, result_consumed, recovery_state, next_action, "
        "last_progress_at, facts_version, created_at, updated_at) VALUES "
        "('j1', 't1', 'WAITING_RUN', 'NEW', 'NOT_OBSERVED', 0, 'NONE', 'NONE', "
        "'2026-01-01T00:00:00+00:00', 0, '2026-01-01T00:00:00+00:00', "
        "'2026-01-01T00:00:00+00:00')")
    conn.close()


def _schema_version(core) -> str:
    return core._store._conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()["value"]


def _job_cols(core) -> set:
    return {r[1] for r in core._store._conn.execute(
        "PRAGMA table_info(supervisor_jobs)")}


def test_fresh_db_lands_on_current_version_with_resource_columns(tmp_path):
    db = str(tmp_path / "fresh.db")
    core = Core(db)
    try:
        assert _schema_version(core) == SCHEMA_VERSION
        cols = _job_cols(core)
        for c in ("resource_class", "last_resource_decision",
                  "last_resource_reason_code", "last_resource_snapshot_hash",
                  "last_resource_at"):
            assert c in cols, f"missing column {c}"
    finally:
        core.close()


def test_migrate_v8_to_current_adds_resource_columns_and_backfills_light(tmp_path):
    db = str(tmp_path / "v8.db")
    _build_v8_db(db)
    core = Core(db)
    try:
        assert _schema_version(core) == SCHEMA_VERSION
        cols = _job_cols(core)
        for c in ("resource_class", "last_resource_decision",
                  "last_resource_reason_code", "last_resource_snapshot_hash",
                  "last_resource_at"):
            assert c in cols, f"missing migrated column {c}"
        row = core._store.get_supervisor_job("j1")
        assert row["resource_class"] == "LIGHT"  # backfilled default
        assert row["last_resource_decision"] is None
        assert row["id"] == "j1"  # no row loss
        assert row["primary_state"] == "QUEUED"
    finally:
        core.close()


def test_reopen_is_idempotent(tmp_path):
    db = str(tmp_path / "v8.db")
    _build_v8_db(db)
    c1 = Core(db)
    v1 = _schema_version(c1)
    j1 = c1._store.get_supervisor_job("j1")
    c1.close()

    c2 = Core(db)
    try:
        assert _schema_version(c2) == v1 == SCHEMA_VERSION
        assert c2._store.get_supervisor_job("j1") == j1
        assert c2._store.get_supervisor_job("j1")["resource_class"] == "LIGHT"
    finally:
        c2.close()


class _FakeGovernor:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    def decide(self, **kwargs):
        self.calls.append(kwargs)
        return self.decision


def _deny_decision():
    from argent_core.resource_governor import AdmissionDecision

    return AdmissionDecision(
        resource_class=ResourceClass.HEAVY.value, policy_version="1",
        snapshot_ref="snap-x", decision=AdmissionVerdict.DENY_LOCAL.value,
        reason_code=ResourceReasonCode.DISK_LOW.value, next_eligible_at=None,
        effective_limits={}, timestamp="2026-09-01T00:00:00+00:00",
    )


def _allow_decision():
    from argent_core.resource_governor import AdmissionDecision

    return AdmissionDecision(
        resource_class=ResourceClass.HEAVY.value, policy_version="1",
        snapshot_ref="snap-y", decision=AdmissionVerdict.ALLOW.value,
        reason_code=ResourceReasonCode.OK.value, next_eligible_at=None,
        effective_limits={}, timestamp="2026-09-01T00:00:00+00:00",
    )


def test_persisted_decision_never_auto_admits(tmp_path):
    """A stored last_resource_* is audit only; a new claim re-runs preflight."""
    db = str(tmp_path / "restart.db")
    clock = FakeClock()
    core = Core(db, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    core.start_task_run(task.id, OWNER)
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock)
    job = sup.store.create_job(
        task.id, idempotency_key="job", resource_class=ResourceClass.HEAVY.value,
    )
    jid = job.supervisor_job_id

    # Simulate a prior ALLOW decision being persisted (audit trail).
    core._store._update_supervisor_job(
        jid, last_resource_decision=AdmissionVerdict.ALLOW.value,
        last_resource_reason_code=ResourceReasonCode.OK.value,
        last_resource_snapshot_hash="snap-y",
        last_resource_at="2026-09-01T00:00:00+00:00",
    )
    assert core._store.get_supervisor_job(jid)["last_resource_decision"] == "ALLOW"
    core.close()

    # Reopen: a fresh Scheduler with a DENY governor must still re-preflight
    # and deny — the persisted ALLOW is never trusted for admission.
    core2 = Core(db, clock=clock)
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock)
    gov = _FakeGovernor(_deny_decision())
    sched = Scheduler(sup2, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=gov)
    r = sched.run_pass(jid)
    assert r.outcome == "resource_denied"
    assert len(gov.calls) == 1
    row = core2._store.get_supervisor_job(jid)
    assert row["primary_state"] == "QUEUED"
    assert row["last_resource_decision"] == AdmissionVerdict.DENY_LOCAL.value
    assert row["last_resource_reason_code"] == ResourceReasonCode.DISK_LOW.value
    core2.close()
