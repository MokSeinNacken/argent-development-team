"""Phase B4 — integrated security acceptance (UNTRUSTED-DATA boundary).

Proves, at the integration level, that agent output / external observations /
process / worktree evidence are DATA and can never become AUTHORITY: they
cannot set owner/epoch, extend a lease, change a writer binding, freely choose
a worktree path, extend an external provider/ref, produce shell commands, grant
approval, change security policy, set DONE, or expand scope.

These are structural (closed dataclass/table shapes) + behavioural (fencing /
allowlist / fail-closed) proofs.  No sleep, no network, no real process.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from argent_core import Core, OWNER_SOURCE, Role
from argent_core.external_wait import (
    ExternalWaitManager,
    FakeExternalWaitAdapter,
    OBS_READY,
    WaitObservation,
    WaitRequest,
    WaitSpec,
)
from argent_core.job_state import PrimaryState
from argent_core.models import LeaseError, LeaseFencedError, PermissionDenied
from argent_core.process_registry import ProcessIdentity, ProcessRegistry
from argent_core.supervisor import Supervisor
from argent_core.worktree import (
    resolve_canonical_worktree_path,
    writer_guard_for,
)
from argent_core.workspace_broker import CONTROLLER_SOURCE, WorkspaceBroker
from mock_supervisor_runtime import FakeClock, FakeRunStatusProvider

OWNER = OWNER_SOURCE


# ---------------------------------------------------------------------------
# Structural: closed shapes carry no authority/command/secret fields.
# ---------------------------------------------------------------------------

def test_wait_request_carries_only_kind_and_reason():
    # The agent's recommendation has NO provider/ref/subject/url/command field.
    fields = {f.name for f in dataclasses.fields(WaitRequest)}
    assert fields == {"kind", "reason"}


def test_wait_spec_and_observation_have_no_command_or_approval_field():
    spec_fields = {f.name for f in dataclasses.fields(WaitSpec)}
    obs_fields = {f.name for f in dataclasses.fields(WaitObservation)}
    for fields in (spec_fields, obs_fields):
        for forbidden in ("command", "shell", "poll", "url", "credential",
                          "token", "secret", "prompt", "approval", "owner",
                          "epoch", "scope", "patch"):
            assert forbidden not in fields, f"forbidden field {forbidden!r}"


def test_external_wait_table_has_no_command_or_secret_column(db_path):
    core = Core(db_path)
    try:
        cols = {r[1] for r in core._store._conn.execute(
            "PRAGMA table_info(external_waits)")}
        for forbidden in ("command", "shell", "poll", "url", "credential",
                          "token", "secret", "prompt"):
            assert forbidden not in cols
    finally:
        core.close()


def test_worktree_path_injection_rejected(tmp_path):
    root = str(tmp_path)
    with pytest.raises(ValueError):
        resolve_canonical_worktree_path("/etc/passwd")
    with pytest.raises(ValueError):
        resolve_canonical_worktree_path("../outside")
    with pytest.raises(ValueError):
        resolve_canonical_worktree_path("a/../../outside", base_root=root)


# ---------------------------------------------------------------------------
# Behavioural: untrusted data has no authority over ownership/lease/binding.
# ---------------------------------------------------------------------------

def _env(db_path):
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    sup = Supervisor(core, FakeRunStatusProvider(), clock=clock)
    task = core.create_task(project.id, "t", OWNER)
    job = sup.store.create_job(task.id, idempotency_key="j")
    claimed = core._store.claim_job(job.supervisor_job_id,
                                    owner_instance_id="A", ttl_seconds=600)
    return SimpleNamespace(core=core, sup=sup, clock=clock, job=claimed,
                           jid=claimed["id"])


def test_external_provider_and_ref_are_allowlisted_only(db_path):
    env = _env(db_path)
    adapter = FakeExternalWaitAdapter()
    mgr = ExternalWaitManager(env.core._store, adapters={"ci": adapter},
                              clock=env.clock)
    # A provider outside the allowlist registry can never be entered.
    with pytest.raises(ValueError):
        mgr.enter_waiting_external(
            env.jid,
            spec=WaitSpec(kind="CI", provider="evil", ref="org/repo#run",
                          expected_subject="abc123"),
            owner_instance_id="A", lease_epoch=env.job["lease_epoch"])
    # The job is unchanged (no half-state, no scope expansion).
    assert env.core._store.get_supervisor_job(env.jid)["primary_state"] == \
        PrimaryState.RUNNING.value
    env.core.close()


def test_external_observation_cannot_set_done_or_extend_lease(db_path):
    env = _env(db_path)
    adapter = FakeExternalWaitAdapter()
    adapter.set_sticky("ci", "org/repo#run", WaitObservation(
        provider="ci", ref="org/repo#run", state=OBS_READY, subject="abc123",
        event_version=1))
    mgr = ExternalWaitManager(env.core._store, adapters={"ci": adapter},
                              clock=env.clock)
    mgr.enter_waiting_external(
        env.jid,
        spec=WaitSpec(kind="CI", provider="ci", ref="org/repo#run",
                      expected_subject="abc123"),
        owner_instance_id="A", lease_epoch=env.job["lease_epoch"])
    # Lease released on wait entry (an observation never re-creates it).
    assert env.core._store.get_supervisor_job(env.jid)["lease_expires_at"] is None
    env.clock.advance(61)
    results = mgr.check_due_waits()
    assert results[0].outcome == "woke"
    row = env.core._store.get_supervisor_job(env.jid)
    # A relevant external event wakes to QUEUED ONLY — never DONE/FAILED, and
    # never re-mints a lease (owner/epoch/expiry stay clear until a real claim).
    assert row["primary_state"] == PrimaryState.QUEUED.value
    assert row["terminal"] is None
    assert row["owner_instance_id"] is None
    assert row["lease_epoch"] >= 0 and row["lease_expires_at"] is None
    env.core.close()


def test_stale_owner_cannot_change_writer_binding(db_path):
    env = _env(db_path)
    # The writer binding primitive is CAS-fenced to the current holder; a stale
    # epoch is rejected and writes nothing (no partial binding).
    with pytest.raises(LeaseFencedError):
        env.core._store.bind_writer_worktree(
            env.jid, dispatch_id="d1", owner_instance_id="A", lease_epoch=9999,
            repo_identity="repo-a", base_commit=None, branch_identity=None,
            canonical_worktree_path="/some/path")
    row = env.core._store.get_supervisor_job(env.jid)
    assert row["writer_binding_mode"] is None
    assert row["writer_dispatch_id"] is None
    env.core.close()


def test_writer_guard_denies_foreign_scope_and_dispatch(db_path, tmp_path):
    scope = str(tmp_path)
    state = {"job": {"canonical_worktree_path": scope, "writer_dispatch_id": "d1"}}
    guard = writer_guard_for(lambda: state["job"], dispatch_id="d1")
    broker = WorkspaceBroker(writer_guard=guard)
    # Foreign worktree path is denied (fail-closed, no write).
    with pytest.raises(PermissionDenied):
        guard(scope + "/../other", Role.IMPLEMENTER, CONTROLLER_SOURCE)
    # A different dispatch (stale writer) is denied.
    guard2 = writer_guard_for(lambda: state["job"], dispatch_id="d2")
    with pytest.raises(PermissionDenied):
        guard2(scope, Role.IMPLEMENTER, CONTROLLER_SOURCE)


def test_process_registry_identity_is_local_not_agent_supplied(db_path):
    env = _env(db_path)
    reg = ProcessRegistry(env.core._store)
    # An UNKNOWN identity (unreadable evidence) is persisted as UNKNOWN with
    # NULL identity parts — never a concrete (boot_id, ticks) fabricated tuple.
    row = reg.register(job_id=env.jid, dispatch_id=None,
                       identity=ProcessIdentity(boot_id=None, pid=100,
                                                process_start_ticks=None))
    rec = env.core._store.get_process_registration(row["process_id"])
    assert rec["status"] == "UNKNOWN"
    assert rec["boot_id"] is None and rec["process_start_ticks"] is None
    # "Process completed" prose is not registry authority.
    assert ProcessRegistry.is_terminally_dead(rec) is False
    env.core.close()


def test_agent_output_cannot_reopen_terminal_or_grant_approval(db_path):
    env = _env(db_path)
    env.core._store._update_supervisor_job(
        env.jid, status="TERMINAL", terminal="DONE", next_action="NONE")
    # No agent/output-driven path can reopen a DONE job: the enqueue primitive
    # refuses even an explicit (non-owner) attempt.
    with pytest.raises(LeaseError):
        env.core._store.enqueue_job(env.jid, queue_reason="WAIT_EVENT")
    # An external wait can never be entered on a terminal job.
    adapter = FakeExternalWaitAdapter()
    mgr = ExternalWaitManager(env.core._store, adapters={"ci": adapter},
                              clock=env.clock)
    with pytest.raises(LeaseError):
        mgr.enter_waiting_external(
            env.jid,
            spec=WaitSpec(kind="CI", provider="ci", ref="org/repo#run",
                          expected_subject="abc123"),
            owner_instance_id="A", lease_epoch=999)
    assert env.core._store.get_supervisor_job(env.jid)["terminal"] == "DONE"
    env.core.close()
