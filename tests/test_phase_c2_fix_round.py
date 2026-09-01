"""Phase C2 — fix-round acceptance tests (F1–F5, deterministic, no host stress).

Covers the Sol closing-review findings that rejected the C2 candidate:

* F1 — no spawn without an enforcer (fail-closed ``resource_enforcement_failed``
  / ``RESOURCE_ENFORCEMENT_UNAVAILABLE``, never a legacy ``launcher.spawn``
  fallback); default wiring creates a real enforcer; a FRESH C1 admission at
  the enforcement point blocks DEFER/DENY_LOCAL despite an earlier ALLOW.
* F2 — Start-Barrier: a verification failure AFTER the process started must
  terminate + prove inactivity (never requeue as "no process started"); an
  unprovable cleanup maps to ``SCOPE_CLEANUP_UNVERIFIED`` -> LOST quarantine
  (never a 300s DEFER requeue); no double/overlapping agent.
* F3 — the production sandbox test path (``RUN_SANDBOX_TESTS``) runs through
  the same C1-admission / C2-enforcement path (scoped bwrap + registry
  terminal evidence); the ``run_tests_fn`` seam is a test-only substitute.
* F4 — ``verify_scope`` is fail-closed: a missing ``ControlGroup`` -> UNKNOWN
  (never a fallback to the old value); ``CPUQuotaPerSecUSec`` / ``TasksMax`` /
  ``pids.max`` are actually compared; a deviating property ->
  ``SCOPE_VERIFICATION_FAILED``.
* F5 — ``persist_resource_decision`` / scope evidence are bounded (JSON <= 4KB),
  holder-CAS, and never accept a foreign write (closed termination enum).
"""

from __future__ import annotations

import sqlite3

import pytest

from argent_core import Core, OWNER_SOURCE, Role, role_source
from argent_core.execution_scope import (
    SCOPE_NAME_PREFIX,
    ExecutionScope,
    ScopeVerificationError,
    SystemdRunScopeBackend,
    VERIFICATION_VERIFIED,
)
from argent_core.process_registry import (
    ProcessIdentity,
    ProcessRegistry,
    _bounded_json,
)
from argent_core.resource_failure import TerminationClass
from argent_core.resource_governor import (
    AdmissionDecision,
    AdmissionVerdict,
    ResourceGovernor,
    ResourceReasonCode,
)
from argent_core.resource_policy import ResourceClass, ResourcePolicy
from argent_core.scheduler import Scheduler
from argent_core.scope_enforcer import EnforcementStatus, ExecutionEnforcer
from c2_helpers import (
    FakeScopeBackend,
    FakeGovernor,
    FakeSnapshotProvider,
    make_env,
    verified_properties,
)

OWNER = OWNER_SOURCE


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _limits(timeout=None):
    pol = ResourcePolicy()
    base = pol.limits_for(ResourceClass.HEAVY)
    return {
        "memory_high_bytes": base.memory_high_bytes,
        "memory_max_bytes": base.memory_max_bytes,
        "swap_max_bytes": base.swap_max_bytes,
        "cpu_quota_percent": base.cpu_quota_percent,
        "timeout_seconds": timeout if timeout is not None else base.timeout_seconds,
    }


def _light_limits():
    pol = ResourcePolicy()
    base = pol.limits_for(ResourceClass.LIGHT)
    return {
        "memory_high_bytes": base.memory_high_bytes,
        "memory_max_bytes": base.memory_max_bytes,
        "swap_max_bytes": base.swap_max_bytes,
        "cpu_quota_percent": base.cpu_quota_percent,
        "timeout_seconds": base.timeout_seconds,
    }


def _admission(verdict, reason, *, next_eligible_at=None):
    return AdmissionDecision(
        resource_class=ResourceClass.HEAVY.value,
        policy_version="1",
        snapshot_ref="snap-1",
        decision=verdict,
        reason_code=reason,
        next_eligible_at=next_eligible_at,
        effective_limits=_limits(),
        timestamp="2026-09-01T00:00:00+00:00",
    )


class ScriptedGovernor:
    """Returns canned decisions from a queue (sticky-last)."""

    def __init__(self, decisions, policy=None):
        self.decisions = list(decisions)
        self.calls = []
        self.policy = policy or ResourcePolicy()

    def decide(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.decisions) > 1:
            return self.decisions.pop(0)
        return self.decisions[0]


def _drive(sched, jid, max_passes=15):
    final = None
    for _ in range(max_passes):
        r = sched.run_pass(jid)
        final = r
        if r.outcome in ("resource_deferred", "resource_denied", "resource_lost"):
            break
    return final


def _row(env):
    return env.core._store.get_supervisor_job(env.jid)


# ---------------------------------------------------------------------------
# F1 — no spawn without enforcer; default wiring; fresh admission at the point
# ---------------------------------------------------------------------------

def test_no_enforcer_blocks_spawn_fail_closed(db_path):
    env = make_env(db_path)
    env.sup._enforcer = None  # simulate enforcement unavailable (fail-closed)
    gov = FakeGovernor(_admission(
        AdmissionVerdict.ALLOW.value, ResourceReasonCode.OK.value,
    ))
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=gov,
                      snapshot_provider=FakeSnapshotProvider())

    final = _drive(sched, env.jid)

    assert final.outcome == "resource_deferred"
    assert final.detail == ResourceReasonCode.RESOURCE_ENFORCEMENT_UNAVAILABLE.value
    assert env.launch.spawns == []  # no legacy launcher.spawn fallback
    row = _row(env)
    assert row["primary_state"] == "QUEUED"
    assert row["error_class"] == "RESOURCE"
    env.core.close()


def test_default_supervisor_wires_real_enforcer(db_path):
    env = make_env(db_path)  # no enforcer injected
    assert isinstance(env.sup._enforcer, ExecutionEnforcer)
    assert isinstance(env.sup._enforcer.backend, SystemdRunScopeBackend)
    env.core.close()


def test_scheduler_wires_injected_enforcer_onto_supervisor(db_path):
    backend = FakeScopeBackend(verify_properties=verified_properties(_limits()))
    enforcer = ExecutionEnforcer(backend)
    env = make_env(db_path)  # supervisor auto-wires a real enforcer first
    assert env.sup._enforcer is not enforcer
    Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60,
              enforcer=enforcer)
    assert env.sup._enforcer is enforcer  # scheduler overrides with the fake
    env.core.close()


def test_fresh_admission_blocks_defer_despite_earlier_allow(db_path):
    """A fresh admission at the enforcement point blocks a DEFER that arrives
    AFTER two earlier ALLOWs (claim preflight + spawn-gate preflight)."""
    backend = FakeScopeBackend(verify_properties=verified_properties(_limits()))
    enforcer = ExecutionEnforcer(backend)
    env = make_env(db_path, enforcer=enforcer)
    gov = ScriptedGovernor([
        _admission(AdmissionVerdict.ALLOW.value, ResourceReasonCode.OK.value),
        _admission(AdmissionVerdict.ALLOW.value, ResourceReasonCode.OK.value),
        _admission(
            AdmissionVerdict.DEFER.value,
            ResourceReasonCode.INSUFFICIENT_MEMORY_RESERVE.value,
            next_eligible_at="2026-09-01T00:05:00+00:00",
        ),
    ])
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=gov,
                      snapshot_provider=FakeSnapshotProvider())

    final = _drive(sched, env.jid)

    assert final.outcome == "resource_deferred"
    assert final.detail == ResourceReasonCode.INSUFFICIENT_MEMORY_RESERVE.value
    assert len(gov.calls) == 3  # claim, spawn-gate, enforcement-point
    assert len(backend.created) == 0  # no scope was ever created
    assert env.launch.spawns == []
    row = _row(env)
    assert row["primary_state"] == "QUEUED"
    assert row["error_class"] == "RESOURCE"
    env.core.close()


# ---------------------------------------------------------------------------
# F2 — Start-Barrier
# ---------------------------------------------------------------------------

def test_verify_failure_before_start_leaves_no_agent_started():
    backend = FakeScopeBackend(fail_verify=ScopeVerificationError("MemoryMax"))
    enforcer = ExecutionEnforcer(backend)
    result = enforcer.enforce_and_spawn(
        command=["openclaw", "agent"], effective_limits=_limits(),
        resource_class=ResourceClass.HEAVY, policy_version="1",
        job_id="job-1", dispatch_id="dispatch-1",
    )
    assert result.status == EnforcementStatus.SCOPE_VERIFICATION_FAILED.value
    assert backend.started == []  # the agent was never started


def test_verify_failure_after_start_terminates_and_proves():
    backend = FakeScopeBackend(fail_bind=True)  # bind fails AFTER start
    enforcer = ExecutionEnforcer(backend)
    result = enforcer.enforce_and_spawn(
        command=["openclaw", "agent"], effective_limits=_limits(),
        resource_class=ResourceClass.HEAVY, policy_version="1",
        job_id="job-1", dispatch_id="dispatch-1",
    )
    assert result.status == EnforcementStatus.SCOPE_VERIFICATION_FAILED.value
    # The started process was terminated and cleanup proven (no double agent).
    assert len(backend.started) == 1
    assert len(backend.terminate_calls) == 1
    assert len(backend.cleanup_calls) == 1
    assert result.evidence["cleanup_proven"] is True


def test_cleanup_unverified_quarantines_lost_not_requeue(db_path):
    """Unprovable cleanup -> LOST quarantine, never a 300s DEFER requeue."""
    backend = FakeScopeBackend(fail_bind=True, prove_inactive=False)
    enforcer = ExecutionEnforcer(backend)
    env = make_env(db_path, enforcer=enforcer)
    gov = FakeGovernor(_admission(
        AdmissionVerdict.ALLOW.value, ResourceReasonCode.OK.value,
    ))
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=gov,
                      snapshot_provider=FakeSnapshotProvider())

    final = _drive(sched, env.jid)

    assert final.outcome == "resource_lost"
    assert env.launch.spawns == []
    row = _row(env)
    assert row["primary_state"] == "LOST"
    assert row["status"] == "RECOVERING"
    assert row["error_class"] == "OWNER_REQUIRED"
    assert row["last_error_code"] == "SCOPE_CLEANUP_UNVERIFIED"
    env.core.close()


def test_start_barrier_no_overlapping_agent():
    """Two attempts produce two distinct scopes; each starts the agent once and
    never uses the agent command as the placeholder."""
    backend = FakeScopeBackend(verify_properties=verified_properties(_limits()))
    enforcer = ExecutionEnforcer(backend)
    common = dict(
        command=["openclaw", "agent"], effective_limits=_limits(),
        resource_class=ResourceClass.HEAVY, policy_version="1",
        job_id="job-1", dispatch_id="dispatch-1",
    )
    r1 = enforcer.enforce_and_spawn(**common)
    r2 = enforcer.enforce_and_spawn(**common)
    assert r1.ok and r2.ok
    assert r1.scope.scope_name != r2.scope.scope_name  # no overlap
    assert r1.scope.scope_name.startswith(SCOPE_NAME_PREFIX + "-")
    assert len(backend.created) == 2
    assert len(backend.started) == 2
    # The placeholder is the harmless sleep, never the agent command.
    for created in backend.created:
        assert created["placeholder_command"][0] == "sleep"
        assert "openclaw" not in created["placeholder_command"]


# ---------------------------------------------------------------------------
# F3 — production sandbox path goes through enforcement
# ---------------------------------------------------------------------------

def test_enforce_and_run_classifies_and_captures_events():
    backend = FakeScopeBackend(
        verify_properties=verified_properties(_light_limits()),
        memory_events={"oom_kill": 0, "oom_group_kill": 0, "max": 0, "high": 0},
        run_result={"exit_code": 0, "stdout_bounded": "", "stderr_bounded": "",
                    "timed_out": False, "pid": 424242},
    )
    enforcer = ExecutionEnforcer(backend)
    result = enforcer.enforce_and_run(
        command=["bwrap", "--ro-bind", "/", "/"], effective_limits=_light_limits(),
        resource_class=ResourceClass.LIGHT, policy_version="1",
        job_id="job-1", dispatch_id="dispatch-1",
    )
    assert result.status == EnforcementStatus.SCOPE_OK.value
    assert result.exit_code == 0
    assert result.termination_class == TerminationClass.NORMAL_EXIT.value
    assert result.scope is not None
    assert len(backend.created) == 1
    # The scope + placeholder were cleaned up and inactivity proven.
    assert result.evidence["cleanup_proven"] is True
    assert len(backend.stop_placeholder_calls) == 1


def test_run_sandbox_scoped_binds_and_marks_terminal(db_path):
    backend = FakeScopeBackend(
        verify_properties=verified_properties(_light_limits()),
        memory_events={"oom_kill": 0, "oom_group_kill": 0, "max": 0, "high": 0},
        run_result={"exit_code": 0, "stdout_bounded": "", "stderr_bounded": "",
                    "timed_out": False, "pid": 424242},
    )
    enforcer = ExecutionEnforcer(backend)
    env = make_env(db_path, enforcer=enforcer)
    env.sup._workspace_root = "/tmp/argent-fake-workspace"
    gov = FakeGovernor(_admission(
        AdmissionVerdict.ALLOW.value, ResourceReasonCode.OK.value,
    ))
    sched = Scheduler(env.sup, owner_instance_id="A", lease_ttl_seconds=60,
                      resource_governor=gov,
                      snapshot_provider=FakeSnapshotProvider())
    # Drive until a real dispatch exists (the process_registry.dispatch_id FK
    # requires a real agent dispatch row).
    dispatch_id = None
    for _ in range(10):
        sched.run_pass(env.jid)
        disps = env.core._store.list_dispatches(env.task.id)
        if disps:
            dispatch_id = disps[0].id
            break
    assert dispatch_id is not None

    job_row = env.sup.store._job_row(env.jid)
    exit_code = env.sup._run_sandbox_scoped(None, job_row, dispatch_id)

    assert exit_code == 0
    regs = env.core._store.list_process_registrations(env.jid)
    assert len(regs) == 1
    reg = regs[0]
    assert reg["status"] == "TERMINAL"
    assert reg["termination_class"] == TerminationClass.NORMAL_EXIT.value
    assert reg["timed_out"] == 0
    assert reg["scope_ref"] == backend.created[0]["scope"].unit_name
    env.core.close()


# ---------------------------------------------------------------------------
# F4 — verify_scope fail-closed
# ---------------------------------------------------------------------------

class _ScriptedBackend(SystemdRunScopeBackend):
    """Real ``verify_scope`` logic with scripted readers (no host I/O)."""

    def __init__(self, show=None, cgroupfs=None):
        super().__init__()
        self._show_values = dict(show or {})
        self._cgroupfs = dict(cgroupfs or {})

    def _show(self, unit_name, props):
        return dict(self._show_values)

    def _read_cgroupfs(self, cgroup_path):
        return dict(self._cgroupfs)


def _f4_scope():
    return ExecutionScope(
        scope_name="argent-c2-job-dispat-abcdef01",
        unit_name="argent-c2-job-dispat-abcdef01.scope",
        cgroup_path="/user.slice/test/app.slice/argent-c2-job-dispat-abcdef01.scope",
        job_id="job-1",
        dispatch_id="dispatch-1",
        resource_class="HEAVY",
        policy_version="1",
        effective_limits=_limits(),
        process_id=424242,
        created_at="2026-09-01T00:00:00+00:00",
    )


def _f4_fs(limits):
    cpu = limits["cpu_quota_percent"]
    return {
        "memory.max": str(limits["memory_max_bytes"]),
        "memory.high": str(limits["memory_high_bytes"]),
        "memory.swap.max": str(limits["swap_max_bytes"]),
        "cpu.max": f"{cpu * 1000} 100000",
        "pids.max": "64",
    }


def _f4_show(limits, *, control_group=None, cpu_usec=None, tasks_max="64"):
    return {
        "MemoryMax": str(limits["memory_max_bytes"]),
        "MemoryHigh": str(limits["memory_high_bytes"]),
        "MemorySwapMax": str(limits["swap_max_bytes"]),
        "CPUQuotaPerSecUSec": cpu_usec or f"{limits['cpu_quota_percent'] // 100}s",
        "TasksMax": tasks_max,
        "ControlGroup": control_group if control_group is not None
        else _f4_scope().cgroup_path,
        "ActiveState": "active",
    }


def test_missing_control_group_fails_closed_no_fallback():
    limits = _limits()
    # ControlGroup key omitted -> must FAIL (no fallback to scope.cgroup_path).
    show = _f4_show(limits)
    del show["ControlGroup"]
    backend = _ScriptedBackend(show=show, cgroupfs=_f4_fs(limits))
    with pytest.raises(ScopeVerificationError) as exc:
        backend.verify_scope(_f4_scope())
    assert "ControlGroup.missing" in str(exc.value)


def test_deviating_cpu_quota_per_sec_usec_rejected():
    limits = _limits()
    show = _f4_show(limits, cpu_usec="7s")  # 700% != 300%
    backend = _ScriptedBackend(show=show, cgroupfs=_f4_fs(limits))
    with pytest.raises(ScopeVerificationError) as exc:
        backend.verify_scope(_f4_scope())
    assert "CPUQuotaPerSecUSec" in str(exc.value)


def test_deviating_tasksmax_rejected():
    limits = _limits()
    show = _f4_show(limits, tasks_max="128")
    backend = _ScriptedBackend(show=show, cgroupfs=_f4_fs(limits))
    with pytest.raises(ScopeVerificationError) as exc:
        backend.verify_scope(_f4_scope())
    assert "TasksMax" in str(exc.value)


def test_deviating_pids_max_rejected():
    limits = _limits()
    fs = _f4_fs(limits)
    fs["pids.max"] = "128"
    backend = _ScriptedBackend(show=_f4_show(limits), cgroupfs=fs)
    with pytest.raises(ScopeVerificationError) as exc:
        backend.verify_scope(_f4_scope())
    assert "fs.pids.max" in str(exc.value)


# ---------------------------------------------------------------------------
# F5 — bounded scope evidence, holder-CAS, no foreign write
# ---------------------------------------------------------------------------

def test_bounded_json_refuses_oversized_evidence():
    # A huge evidence dict is refused fail-closed (never a dump).
    assert _bounded_json({"k": "x" * 5000}) is None
    assert _bounded_json({"k": "small"}) == '{"k":"small"}'


def test_termination_class_closed_enum_rejects_foreign_value(db_path):
    env = make_env(db_path)
    reg = ProcessRegistry(env.core._store)
    with pytest.raises(sqlite3.IntegrityError):
        reg.register(
            job_id=env.jid, dispatch_id=None,
            identity=ProcessIdentity(boot_id="boot-1", pid=1,
                                     process_start_ticks=1),
            termination_class="BOGUS_FOREIGN_VALUE",
        )
    env.core.close()


def test_mark_terminal_persists_bounded_evidence(db_path):
    env = make_env(db_path)
    reg = ProcessRegistry(env.core._store)
    row = reg.register(
        job_id=env.jid, dispatch_id=None,
        identity=ProcessIdentity(boot_id="boot-1", pid=1, process_start_ticks=1),
        scope_ref="argent-c2-x.scope", resource_class=ResourceClass.LIGHT.value,
        effective_limits=_light_limits(),
        scope_events={"oom_kill": 0, "max": 0, "high": 0},
    )
    pid = row["process_id"]
    reg.mark_terminal(
        pid,
        exit_code=0,
        terminal_at="2026-09-01T00:01:00+00:00",
        termination_class=TerminationClass.NORMAL_EXIT.value,
        timed_out=False,
        scope_events={"oom_kill": 0, "max": 0, "high": 0},
    )
    final = env.core._store.get_process_registration(pid)
    assert final["status"] == "TERMINAL"
    assert final["termination_class"] == TerminationClass.NORMAL_EXIT.value
    assert final["timed_out"] == 0
    assert final["terminal_at"] == "2026-09-01T00:01:00+00:00"
    env.core.close()
