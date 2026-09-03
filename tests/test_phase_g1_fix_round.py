"""Phase G1 — fix-round regression tests (F1–F8).

Deterministic, offline proofs for the independently-confirmed findings:

* F1  background loop continues multi-step RUNNING jobs + periodic recovery
      of expired-lease RUNNING jobs after a restart;
* F2  monotonic-revision CAS (no ABA even under a frozen clock);
* F3  shared-store host identity (no blind cross-host takeover) + fence-loss
      stops the runtime;
* F4  minimal allowlisted spawn environment (no evidence-key leak);
* F5  path/config validation fail-closed (XDG/symlink/relative/DB-path/NaN/
      unknown-field);
* F6  SIGTERM mid-pass aborts before spawn;
* F7  FAILED/exit semantics (FAILED preserved, non-zero exit, structural
      escalation, waits still run on error passes).

No systemd activation, no real secrets, no real subprocess (except the
controlled ``git init`` in the F1 multi-step lifecycle test, mirroring the
existing B4 E2E harness).
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from argent_core import Core, OWNER_SOURCE
from argent_core.background_runtime import (
    ServiceHealth,
    SupervisorInstance,
    SupervisorRuntime,
)
from argent_core.external_wait import ExternalWaitManager
from argent_core.job_state import PrimaryState
from argent_core.resource_failure import TerminationClass
from argent_core.sandbox_runner import SandboxResult
from argent_core.scheduler import OUTCOME_NO_WORK, Scheduler
from argent_core.supervisor import RunStatus, Supervisor
from c2_helpers import FakeScopeBackend
from c3_helpers import (
    build_running_job,
    fake_identity_provider,
    register_terminal_evidence,
)
from g1_helpers import (
    add_queued_job,
    make_identity_provider,
    make_runtime_env,
    seed_owner,
)
from mock_supervisor_runtime import (
    AutoRunStatusProvider,
    FakeClock,
    FakeRunLauncher,
    FakeRunStatusProvider,
    make_run_observation,
)

OWNER = OWNER_SOURCE


# ---------------------------------------------------------------------------
# F2 — monotonic-revision CAS (no ABA under a frozen clock)
# ---------------------------------------------------------------------------

def _instance_row(instance_id: str, updated_at: str) -> dict:
    return {
        "singleton_id": "primary",
        "instance_id": instance_id,
        "boot_id": "boot-1",
        "host_id": "host-1",
        "pid": 100,
        "process_start_ticks": 5,
        "status": "ACTIVE",
        "acquired_at": updated_at,
        "lease_expires_at": None,
        "last_heartbeat_at": updated_at,
        "stopped_at": None,
        "stop_reason": None,
        "last_error_code": None,
        "updated_at": updated_at,
    }


def test_cas_revision_prevents_aba_concurrent_takeover(db_path):
    from argent_core.store import Store

    s1 = Store(db_path)
    s2 = Store(db_path)
    try:
        # Seed an existing ACTIVE row (revision -> 1) with a FIXED timestamp.
        assert s1.cas_supervisor_instance(
            row=_instance_row("instance:old", "T"), expected_revision=None)
        rev = s1.get_supervisor_instance()["revision"]
        assert rev == 1

        # Both candidates read the SAME revision and write the SAME updated_at
        # (a frozen clock -> the old updated_at-CAS would let BOTH win).
        a_ok = s1.cas_supervisor_instance(
            row=_instance_row("instance:A", "T"), expected_revision=rev)
        b_ok = s2.cas_supervisor_instance(
            row=_instance_row("instance:B", "T"), expected_revision=rev)

        assert a_ok != b_ok  # exactly ONE wins
        final = s1.get_supervisor_instance()
        assert final["revision"] == rev + 1  # monotonic, atomically bumped
        winner = "instance:A" if a_ok else "instance:B"
        assert final["instance_id"] == winner
    finally:
        s1.close()
        s2.close()


def test_heartbeat_and_release_bump_revision(db_path):
    core = Core(db_path)
    inst = SupervisorInstance(
        core._store,
        identity_provider=make_identity_provider("boot-1", {100: 5}),
        instance_id="instance:test", own_pid=100,
    )
    res = inst.acquire()
    assert res.verdict.value in ("acquired", "takeover")
    r0 = core._store.get_supervisor_instance()["revision"]
    assert inst.heartbeat() is True
    r1 = core._store.get_supervisor_instance()["revision"]
    assert r1 == r0 + 1
    assert inst.release(reason="shutdown") is True
    r2 = core._store.get_supervisor_instance()["revision"]
    assert r2 == r1 + 1
    core.close()


# ---------------------------------------------------------------------------
# F3 — shared-store host identity + fence-loss
# ---------------------------------------------------------------------------

def test_shared_store_foreign_host_is_not_taken_over(db_path):
    core = Core(db_path)
    # Owner lives on host-1 / boot-1.
    seed_owner(core._store, boot_id="boot-1", pid=100, ticks=5,
               host_id="host-1")
    # A candidate on host-2 (different machine-id) with a different boot must
    # NOT treat the owner as dead (it may be alive on its own host).
    inst = SupervisorInstance(
        core._store,
        identity_provider=make_identity_provider("boot-2", {200: 9},
                                                 machine_id="host-2"),
        instance_id="instance:new", own_pid=200,
        pid_alive=lambda pid: True,
    )
    res = inst.acquire()
    assert res.verdict.value == "ambiguous"
    assert core._store.get_supervisor_instance()["instance_id"] == "instance:old"
    core.close()


def test_fence_loss_stops_runtime_without_further_passes(db_path):
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    inst = SupervisorInstance(
        core._store,
        identity_provider=make_identity_provider("boot-1", {100: 5}),
        instance_id="instance:test", own_pid=100, clock=clock,
    )
    assert inst.acquire().verdict.value in ("acquired", "takeover")

    class RecordingScheduler:
        def __init__(self):
            self.calls = 0

        def run_pass(self, job_id=None):
            self.calls += 1
            return SimpleNamespace(outcome="no_work")

    sched = RecordingScheduler()
    ewm = ExternalWaitManager(core._store, adapters={}, clock=clock)
    rt = SupervisorRuntime(
        scheduler=sched, external_wait_manager=ewm, instance=inst,
        store=core._store, clock=clock, sleep_fn=lambda s: None, max_passes=100,
    )
    # Another instance takes over the singleton row (fence loss for us).
    row = core._store.get_supervisor_instance()
    takeover = _instance_row("instance:other", row["updated_at"])
    assert core._store.cas_supervisor_instance(
        row=takeover, expected_revision=row["revision"])

    rt.run_loop()
    assert rt.state is ServiceHealth.FAILED
    assert rt.snapshot().last_error_code in ("instance_lease_lost",
                                             "heartbeat_failed")
    # At most one pass ran before the fence loss was detected.
    assert sched.calls <= 1
    core.close()


# ---------------------------------------------------------------------------
# F4 — minimal allowlisted spawn environment
# ---------------------------------------------------------------------------

def test_agent_spawn_env_strips_evidence_key(monkeypatch):
    from argent_core.execution_scope import agent_spawn_env
    monkeypatch.setenv("ARGENT_EVIDENCE_MAC_KEY", "canary-secret")
    monkeypatch.setenv("ARGENT_EVIDENCE_MAC_KEY_FILE", "/secret/key.bin")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/t")
    env = agent_spawn_env()
    assert "ARGENT_EVIDENCE_MAC_KEY" not in env
    assert "ARGENT_EVIDENCE_MAC_KEY_FILE" not in env
    assert env.get("PATH") == "/usr/bin"
    assert env.get("HOME") == "/home/t"


def test_scope_start_in_scope_passes_sanitized_env(monkeypatch):
    from argent_core.execution_scope import ExecutionScope, SystemdRunScopeBackend
    monkeypatch.setenv("ARGENT_EVIDENCE_MAC_KEY", "canary-secret")
    monkeypatch.setenv("ARGENT_EVIDENCE_MAC_KEY_FILE", "/secret/key.bin")
    captured = {}

    class FakePopen:
        pid = 4242

    def fake_popen(argv, **kw):
        captured["env"] = kw.get("env")
        return FakePopen()

    backend = SystemdRunScopeBackend(popen_fn=fake_popen)
    monkeypatch.setattr(backend, "_move_into_cgroup", lambda pid, cg: True)
    scope = ExecutionScope(
        scope_name="argent-c2-x", unit_name="argent-c2-x.scope",
        cgroup_path="/user.slice/argent-c2-x.scope", job_id="j",
        dispatch_id="d", resource_class="LIGHT", policy_version="1",
        effective_limits={}, process_id=None, created_at="t",
    )
    backend.start_in_scope(scope=scope, command=["openclaw", "agent"])
    assert captured["env"] is not None
    assert "ARGENT_EVIDENCE_MAC_KEY" not in captured["env"]
    assert "ARGENT_EVIDENCE_MAC_KEY_FILE" not in captured["env"]


def test_launcher_spawn_passes_sanitized_env(monkeypatch, tmp_path):
    from argent_core import supervisor as supmod
    monkeypatch.setenv("ARGENT_EVIDENCE_MAC_KEY", "canary-secret")
    monkeypatch.setenv("ARGENT_EVIDENCE_MAC_KEY_FILE", "/secret/key.bin")
    captured = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            captured["env"] = kw.get("env")
            self.pid = 7777

    monkeypatch.setattr(supmod.subprocess, "Popen", FakePopen)
    launcher = supmod.OpenClawRunLauncher()
    pid = launcher.spawn(agent_id="argent-lead", dispatch_id="d1",
                         message_file=tmp_path / "m.json", timeout_seconds=60)
    assert pid == 7777
    assert "ARGENT_EVIDENCE_MAC_KEY" not in captured["env"]
    assert "ARGENT_EVIDENCE_MAC_KEY_FILE" not in captured["env"]


# ---------------------------------------------------------------------------
# F5 — path / config validation fail-closed
# ---------------------------------------------------------------------------

def test_config_xdg_tmp_fails_closed(tmp_path):
    from argent_core.argent_service import load_service_config
    with pytest.raises(ValueError):
        load_service_config(home=tmp_path, env={"XDG_STATE_HOME": "/tmp/x"})


def test_config_symlink_to_tmp_fails_closed(tmp_path):
    from argent_core.argent_service import load_service_config
    link = tmp_path / "link"
    link.symlink_to("/tmp", target_is_directory=True)
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"state_dir": str(link / "argent")}))
    with pytest.raises(ValueError):
        load_service_config(str(p), home=tmp_path, env={})


def test_config_relative_xdg_resolves_under_home(tmp_path):
    from argent_core.argent_service import load_service_config
    cfg = load_service_config(home=tmp_path, env={"XDG_STATE_HOME": "rel"},
                              reject_ephemeral=False)
    assert cfg.state_dir == tmp_path / "rel" / "argent"


def test_config_db_path_outside_state_fails_closed(tmp_path):
    from argent_core.argent_service import load_service_config
    state = tmp_path / "state"
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "state_dir": str(state),
        "db_path": str(tmp_path / "elsewhere.db"),
    }))
    with pytest.raises(ValueError):
        load_service_config(str(p), home=tmp_path, env={},
                            reject_ephemeral=False)


def test_pos_float_rejects_nan_and_inf():
    from argent_core.argent_service import _pos_float
    with pytest.raises(ValueError):
        _pos_float(float("nan"), 5.0, "x")
    with pytest.raises(ValueError):
        _pos_float(float("inf"), 5.0, "x")
    with pytest.raises(ValueError):
        _pos_float(0.0, 5.0, "x")


def test_config_unknown_field_fails_closed(tmp_path):
    from argent_core.argent_service import load_service_config
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"bogus_field": "x"}))
    with pytest.raises(ValueError):
        load_service_config(str(p), home=tmp_path, env={},
                            reject_ephemeral=False)


# ---------------------------------------------------------------------------
# F6 — SIGTERM mid-pass aborts before spawn
# ---------------------------------------------------------------------------

class _RecordingEnforcer:
    def __init__(self):
        from argent_core.scope_enforcer import ExecutionEnforcer
        self._inner = ExecutionEnforcer(FakeScopeBackend())
        self.spawn_calls = 0
        self.run_calls = 0

    def enforce_and_spawn(self, **kw):
        self.spawn_calls += 1
        return self._inner.enforce_and_spawn(**kw)

    def enforce_and_run(self, **kw):
        self.run_calls += 1
        return self._inner.enforce_and_run(**kw)


def test_sigterm_mid_pass_aborts_before_spawn(db_path):
    enforcer = _RecordingEnforcer()
    env = make_runtime_env(db_path, sleep_fn=lambda s: None, enforcer=enforcer)
    task = env.core.create_task(env.project.id, "t", OWNER)
    env.core.start_task_run(task.id, OWNER)
    job = env.sup.store.create_job(task.id, idempotency_key="job-f6")
    jid = job.supervisor_job_id

    # Pass 1: claim -> START_ROLE.  Pass 2: CREATE_DISPATCH.
    env.sched.run_pass(jid)
    env.sched.run_pass(jid)
    dispatches = env.core._store.list_dispatches(task.id)
    assert len(dispatches) == 1
    d = dispatches[0]

    # NOT_FOUND -> the next pass plans SPAWN_RUN.
    env.sup._run_status.set_current(d.id, make_run_observation(
        dispatch_id=d.id, role=d.role, status=RunStatus.NOT_FOUND,
        authoritative_not_found=True))

    # SIGTERM lands mid-pass: the stop predicate is set.
    env.runtime.request_shutdown("SIGTERM")
    r = env.sched.run_pass(jid)
    assert r.outcome == OUTCOME_NO_WORK
    assert r.detail == "stop_requested"
    # The launcher/enforcer never fired.
    assert enforcer.spawn_calls == 0
    assert enforcer.run_calls == 0
    # The job is still RUNNING under a valid lease (consistent, re-claimable).
    row = env.core._store.get_supervisor_job(jid)
    assert row["primary_state"] == PrimaryState.RUNNING.value
    env.core.close()


# ---------------------------------------------------------------------------
# F7 — FAILED / exit semantics
# ---------------------------------------------------------------------------

def test_failed_state_preserved_by_finalize(db_path):
    env = make_runtime_env(db_path, max_passes=5, sleep_fn=lambda s: None)
    env.instance.acquire()

    class FailScheduler:
        def __init__(self, runtime):
            self.runtime = runtime

        def run_pass(self, job_id=None):
            # An unrecoverable error surfaces mid-pass (mark_failed).
            self.runtime.mark_failed("db_unreachable")
            return SimpleNamespace(outcome="no_work")

    env.runtime._scheduler = FailScheduler(env.runtime)
    summary = env.runtime.run_loop()
    # F7(a): _finalize must NOT downgrade FAILED to STOPPING.
    assert env.runtime.state is ServiceHealth.FAILED
    assert summary.stop_reason == "db_unreachable"
    env.core.close()


def test_repeated_scheduler_errors_escalate_to_failed(db_path):
    class BoomScheduler:
        def run_pass(self, job_id=None):
            raise RuntimeError("boom")

    env = make_runtime_env(db_path, max_passes=50, sleep_fn=lambda s: None,
                           max_consecutive_errors=3)
    env.runtime._scheduler = BoomScheduler()
    summary = env.runtime.run_loop()
    assert env.runtime.state is ServiceHealth.FAILED
    assert summary.errors >= 3
    env.core.close()


def test_transient_error_stays_degraded(db_path):
    class FlakyScheduler:
        def __init__(self):
            self.calls = 0

        def run_pass(self, job_id=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient")
            return SimpleNamespace(outcome="no_work")

    env = make_runtime_env(db_path, max_passes=5, sleep_fn=lambda s: None,
                           max_consecutive_errors=3)
    env.runtime._scheduler = FlakyScheduler()
    env.runtime.run_loop()
    assert env.runtime.state is not ServiceHealth.FAILED
    assert env.runtime.state in (ServiceHealth.STOPPING, ServiceHealth.DEGRADED)
    env.core.close()


def test_external_waits_run_even_on_pass_error(db_path):
    class BoomScheduler:
        def run_pass(self, job_id=None):
            raise RuntimeError("boom")

    class RecordingWaitManager:
        def __init__(self):
            self.calls = 0

        def check_due_waits(self):
            self.calls += 1
            return []

    env = make_runtime_env(db_path, max_passes=3, sleep_fn=lambda s: None,
                           max_consecutive_errors=100)
    env.runtime._scheduler = BoomScheduler()
    waits = RecordingWaitManager()
    env.runtime._external_wait_manager = waits
    env.runtime.run_loop()
    assert waits.calls == 3
    env.core.close()


def test_main_returns_failed_exit_code(monkeypatch, tmp_path):
    import argent_core.argent_service as svcmod
    from argent_core.background_runtime import InstanceVerdict

    class FakeScheduler:
        def reconcile_after_restart(self):
            return SimpleNamespace()

    class FakeRuntime:
        state = ServiceHealth.FAILED
        scheduler = FakeScheduler()

        def run_loop(self):
            return None

        def set_recovery_result(self, summary):
            pass

    class FakeInstance:
        def acquire(self):
            return SimpleNamespace(verdict=InstanceVerdict.ACQUIRED)

    class FakeCore:
        def close(self):
            pass

    class FakeSvc:
        def __init__(self):
            self.instance = FakeInstance()
            self.runtime = FakeRuntime()
            self.core = FakeCore()
            self.acquire_result = None

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(
        svcmod, "load_service_config",
        lambda path=None, **kw: svcmod.ServiceConfig(
            state_dir=state, share_dir=state, cache_dir=state,
            db_path=state / "a.db"),
    )
    monkeypatch.setattr(svcmod, "build_service", lambda config, **kw: FakeSvc())
    # G2 (F1): the sandbox preflight is irrelevant on this fully-injected path
    # (no real scope backend is constructed) — disable it.
    monkeypatch.setattr(svcmod, "_SANDBOX_PREFLIGHT_ENABLED", False)
    assert svcmod.main([]) == svcmod.EXIT_FAILED


# ---------------------------------------------------------------------------
# F1 — multi-step job via run_loop + periodic recovery after restart
# ---------------------------------------------------------------------------

def _fake_run_tests(workspace, pytest_args=None, limits=None):
    return SandboxResult(exit_code=0, stdout_bounded="", stderr_bounded="",
                         timed_out=False, wall_seconds=0.0)


def test_run_loop_drives_multistep_job_to_done(tmp_path):
    clock = FakeClock()
    db = str(tmp_path / "g1.db")
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True, exist_ok=True)
    (ws / "tests").mkdir(parents=True, exist_ok=True)
    (ws / "src" / "module.py").write_text("# stub\n")

    core = Core(db, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    core.start_task_run(task.id, OWNER)
    sup = Supervisor(
        core, AutoRunStatusProvider(core), FakeRunLauncher(), clock=clock,
        workspace_root=str(ws), run_tests_fn=_fake_run_tests,
        enforcer=_RecordingEnforcer(),
    )
    job = sup.store.create_job(task.id, idempotency_key="job-1")
    jid = job.supervisor_job_id

    sched = Scheduler(sup, owner_instance_id="instance-A", lease_ttl_seconds=600)
    ewm = ExternalWaitManager(core._store, adapters={}, clock=clock)
    inst = SupervisorInstance(
        core._store,
        identity_provider=make_identity_provider("boot-1", {100: 5}),
        instance_id="instance-A", own_pid=100, clock=clock,
    )
    rt = SupervisorRuntime(
        scheduler=sched, external_wait_manager=ewm, instance=inst,
        store=core._store, clock=clock, sleep_fn=lambda s: None, max_passes=300,
    )
    assert inst.acquire().verdict.value in ("acquired", "takeover")
    rt.run_loop()
    row = core._store.get_supervisor_job(jid)
    assert row["terminal"] == "DONE"
    assert row["primary_state"] == PrimaryState.DONE.value
    core.close()


def test_loop_periodic_recovery_takes_over_expired_lease(db_path):
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    env = build_running_job(core, owner="A", ttl=60)
    # A provably-terminal (but NON-resource) process -> evidence-bound takeover
    # (a resource class would trigger the C3 recovery path instead).
    register_terminal_evidence(
        core, env.jid, termination_class=TerminationClass.NORMAL_EXIT.value,
        exit_code=0,
    )
    core.close()

    core2 = Core(db_path, clock=clock)
    sup = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher(),
                     clock=clock)
    sup._process_identity_provider = fake_identity_provider()
    sched = Scheduler(sup, owner_instance_id="B", lease_ttl_seconds=60)

    # Restart BEFORE lease expiry: reconcile leaves the (still-valid) foreign lease.
    summary = sched.reconcile_after_restart()
    assert summary.foreign_lease_kept == 1
    assert core2._store.get_supervisor_job(env.jid)["owner_instance_id"] == "A"

    # Advance past lease expiry -> the periodic LOOP (not reconcile) recovers it.
    clock.advance(61)
    inst = SupervisorInstance(
        core2._store,
        identity_provider=make_identity_provider("boot-2", {200: 9}),
        instance_id="instance:B", own_pid=200, clock=clock,
    )
    ewm = ExternalWaitManager(core2._store, adapters={}, clock=clock)
    rt = SupervisorRuntime(
        scheduler=sched, external_wait_manager=ewm, instance=inst,
        store=core2._store, clock=clock, sleep_fn=lambda s: None, max_passes=1,
    )
    assert inst.acquire().verdict.value in ("acquired", "takeover")
    rt.run_loop()
    row = core2._store.get_supervisor_job(env.jid)
    # The loop took over the expired-lease RUNNING job (no longer foreign).
    assert row["owner_instance_id"] == "B"
    assert row["lease_epoch"] >= 2
    core2.close()
