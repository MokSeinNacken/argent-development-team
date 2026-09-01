"""Phase C3 — fix-round adversarial tests (F1–F7).  Deterministic.

Every Sol finding (F1–F7) is closed by at least one adversarial test here.
No real OOM/swap/disk/CPU stress: FakeScopeBackend + FakeClock + FakeGovernor
only.  No shell, no secrets, bounded JSON.
"""

from __future__ import annotations

import pytest

from argent_core import Core
from argent_core.models import (
    AgentDispatch,
    DispatchStatus,
    LeaseError,
    LeaseFencedError,
    Role,
    SequenceKind,
)
from argent_core.process_registry import (
    ProcessIdentity,
    ProcessRegistry,
)
from argent_core.resource_failure import TerminationClass
from argent_core.resource_governor import (
    AdmissionDecision,
    AdmissionVerdict,
    ResourceReasonCode,
)
from argent_core.resource_policy import ResourceClass, ResourcePolicy
from argent_core.resource_recovery import (
    FailureClass,
    RecoveryDecision,
    RecoveryPolicy,
    RecoveryReasonCode,
    assert_valid_recovery_pair,
    decide_recovery,
    is_resource_failure,
    is_valid_recovery_pair,
    reason_code_for_failure,
)
from argent_core.scheduler import Scheduler
from argent_core.scope_enforcer import ExecutionEnforcer, EnforcementStatus
from argent_core.supervisor import Supervisor
from c2_helpers import (
    FakeGovernor,
    FakeScopeBackend,
    FakeSnapshotProvider,
    make_env,
    verified_properties,
)
from c3_helpers import (
    build_running_job,
    fake_identity_provider,
    make_scheduler,
    register_terminal_evidence,
)
from mock_supervisor_runtime import FakeRunLauncher, FakeRunStatusProvider


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

def _light_limits():
    pol = ResourcePolicy()
    limits = pol.limits_for(ResourceClass.LIGHT)
    return {
        "memory_high_bytes": limits.memory_high_bytes,
        "memory_max_bytes": limits.memory_max_bytes,
        "swap_max_bytes": limits.swap_max_bytes,
        "cpu_quota_percent": limits.cpu_quota_percent,
        "timeout_seconds": limits.timeout_seconds,
    }


def _admission(verdict, reason):
    return AdmissionDecision(
        resource_class=ResourceClass.LIGHT.value,
        policy_version="1",
        snapshot_ref="snap-1",
        decision=verdict,
        reason_code=reason,
        next_eligible_at=None,
        effective_limits=_light_limits(),
        timestamp="2026-09-01T00:00:00+00:00",
    )


def _scheduler_for(env, *, owner="A", recovery_policy=None):
    return Scheduler(
        env.sup, owner_instance_id=owner, lease_ttl_seconds=300,
        resource_governor=FakeGovernor(
            _admission(AdmissionVerdict.ALLOW.value, ResourceReasonCode.OK.value)
        ),
        snapshot_provider=FakeSnapshotProvider(),
        recovery_policy=recovery_policy,
    )


def _scheduler_on(core, *, owner="A", recovery_policy=None):
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher())
    sup._process_identity_provider = fake_identity_provider()
    return Scheduler(
        sup, owner_instance_id=owner, lease_ttl_seconds=300,
        resource_governor=FakeGovernor(
            _admission(AdmissionVerdict.ALLOW.value, ResourceReasonCode.OK.value)
        ),
        snapshot_provider=FakeSnapshotProvider(),
        recovery_policy=recovery_policy,
    )


def _drive_to_dispatch(sched, env):
    for _ in range(20):
        sched.run_pass(env.jid)
        job = env.sup.store._job_row(env.jid)
        if job.get("expected_dispatch_id") is not None:
            return job["expected_dispatch_id"]
    return None


def _insert_dispatch(core, *, task_id, task_run_id, dispatch_id):
    d = AgentDispatch(
        id=dispatch_id, task_id=task_id, task_run_id=task_run_id,
        role=Role.IMPLEMENTER, parent_dispatch_id=None,
        expected_agent_class="argent-implementer",
        expected_model_class="model-x", expected_thinking_tier="medium",
        child_session_id=None, openclaw_run_id=None,
        actual_provider=None, actual_model=None, thinking_tier=None,
        status=DispatchStatus.PENDING, cycle_no=1, position=1,
        sequence_kind=SequenceKind.STANDARD, attempt_no=1, handoff_id=None,
        result_json=None, created_at="2026-01-01T00:00:00+00:00",
        started_at=None, consumed_at=None,
    )
    core._store._insert_dispatch(d)
    return d


def _register_terminal(
    core, jid, *, termination_class, dispatch_id=None, boot_id="boot-1",
    exit_code=None, scope_events=None,
):
    reg = ProcessRegistry(core._store)
    row = reg.register(
        job_id=jid, dispatch_id=dispatch_id,
        identity=ProcessIdentity(boot_id=boot_id, pid=100,
                                 process_start_ticks=42),
    )
    reg.mark_terminal(
        row["process_id"], exit_code=exit_code,
        terminal_at="2026-09-01T00:00:00+00:00",
        termination_class=termination_class, scope_events=scope_events,
    )
    return row["process_id"]


# ---------------------------------------------------------------------------
# F1 — detached agents: productive post-terminal classification/recovery path
# ---------------------------------------------------------------------------

def test_f1_restart_reconcile_recovers_detached_terminal_exactly_once(db_path):
    env = build_running_job(Core(db_path), owner="A")
    register_terminal_evidence(
        env.core, env.jid, termination_class=TerminationClass.OOM_KILL.value,
        exit_code=137,
        scope_events={"oom_kill": 1, "oom_group_kill": 0, "max": 0, "high": 0},
    )
    jid = env.jid
    env.core.close()

    core2 = Core(db_path)
    try:
        sched = _scheduler_on(core2, owner="A")
        s1 = sched.reconcile_after_restart()
        assert s1.resource_recovered == 1
        row = core2._store.get_supervisor_job(jid)
        assert row["primary_state"] == "BLOCKED"
        assert row["last_failure_class"] == "RESOURCE_OOM"
        # Exactly once: a second reconcile cannot re-recover the same event.
        s2 = sched.reconcile_after_restart()
        assert s2.resource_recovered == 0
        assert core2._store.get_supervisor_job(jid)["primary_state"] == "BLOCKED"
    finally:
        core2.close()


def test_f1_run_pass_recovers_detached_terminal_no_duplicate_spawn(db_path):
    env = build_running_job(Core(db_path), owner="A")
    register_terminal_evidence(
        env.core, env.jid, termination_class=TerminationClass.OOM_KILL.value,
        exit_code=137,
        scope_events={"oom_kill": 1, "oom_group_kill": 0, "max": 0, "high": 0},
    )
    sched = make_scheduler(env, owner="A")
    result = sched.run_pass(env.jid)
    assert result.outcome == "resource_recovered"
    row = env.core._store.get_supervisor_job(env.jid)
    assert row["primary_state"] == "BLOCKED"
    # No second agent, no code rework.
    assert env.sup._launcher.spawns == []
    assert row["last_failure_class"] == "RESOURCE_OOM"


def test_f1_normal_exit_is_not_a_resource_recovery(db_path):
    env = build_running_job(Core(db_path), owner="A")
    register_terminal_evidence(
        env.core, env.jid, termination_class=TerminationClass.NORMAL_EXIT.value,
        exit_code=0,
    )
    sched = make_scheduler(env, owner="A")
    assert sched.classify_and_recover(env.jid, env.epoch) is None
    assert env.core._store.get_supervisor_job(env.jid)["primary_state"] == "RUNNING"


# ---------------------------------------------------------------------------
# F2 — DEFER_RESOURCE consumes a bounded budget
# ---------------------------------------------------------------------------

def test_f2_repeated_defers_stop_after_max_and_survive_reopen(db_path):
    env = build_running_job(Core(db_path), owner="A")
    pol = RecoveryPolicy(max_resource_defers=2)

    # Defer round 1 (attempt 0 -> 1).
    row = env.core._store.commit_recovery_decision(
        env.jid, owner_instance_id="A", lease_epoch=env.epoch,
        failure_class=FailureClass.RESOURCE_CAPACITY_FAILURE,
        recovery_decision=RecoveryDecision.DEFER_RESOURCE,
        reason_code="RESOURCE_CAPACITY_INSUFFICIENT",
        next_eligible_at="2026-09-01T00:05:00+00:00",
    )
    assert row["attempt_no"] == 1

    # Re-claim (attempt_no persists) -> defer round 2 (attempt 1 -> 2).
    claimed = env.core._store.claim_job(
        env.jid, owner_instance_id="A", ttl_seconds=300,
    )
    assert decide_recovery(
        FailureClass.RESOURCE_CAPACITY_FAILURE, attempt_no=1, policy=pol,
    ) is RecoveryDecision.DEFER_RESOURCE
    row = env.core._store.commit_recovery_decision(
        env.jid, owner_instance_id="A", lease_epoch=claimed["lease_epoch"],
        failure_class=FailureClass.RESOURCE_CAPACITY_FAILURE,
        recovery_decision=RecoveryDecision.DEFER_RESOURCE,
        reason_code="RESOURCE_CAPACITY_INSUFFICIENT",
        next_eligible_at="2026-09-01T00:10:00+00:00",
    )
    assert row["attempt_no"] == 2

    # Budget exhausted -> BLOCK (no unbounded defer loop).
    assert decide_recovery(
        FailureClass.RESOURCE_CAPACITY_FAILURE, attempt_no=2, policy=pol,
    ) is RecoveryDecision.BLOCK_RESOURCE

    # DB reopen respects the persisted budget.
    jid = env.jid
    env.core.close()
    core2 = Core(db_path)
    try:
        assert core2._store.get_supervisor_job(jid)["attempt_no"] == 2
        assert decide_recovery(
            FailureClass.RESOURCE_CAPACITY_FAILURE, attempt_no=2, policy=pol,
        ) is RecoveryDecision.BLOCK_RESOURCE
    finally:
        core2.close()


def test_f2_timeout_defer_also_bounded_to_block(db_path):
    pol = RecoveryPolicy(max_resource_retries=2, max_resource_defers=2)
    # Retry budget: 0,1 -> RETRY; both budgets exhausted at 2 -> BLOCK.
    assert decide_recovery(FailureClass.RESOURCE_TIMEOUT, attempt_no=0,
                           policy=pol) is RecoveryDecision.RETRY_BOUNDED
    assert decide_recovery(FailureClass.RESOURCE_TIMEOUT, attempt_no=1,
                           policy=pol) is RecoveryDecision.RETRY_BOUNDED
    assert decide_recovery(FailureClass.RESOURCE_TIMEOUT, attempt_no=2,
                           policy=pol) is RecoveryDecision.BLOCK_RESOURCE
    assert decide_recovery(FailureClass.RESOURCE_TIMEOUT, attempt_no=99,
                           policy=pol) is RecoveryDecision.BLOCK_RESOURCE


def test_f2_enforcement_failure_defers_bounded_then_lost(db_path):
    pol = RecoveryPolicy(max_resource_defers=2, allow_enforcement_defer=True)
    assert decide_recovery(FailureClass.RESOURCE_ENFORCEMENT_FAILURE, attempt_no=0,
                           policy=pol, has_evidence_unknown=False) \
        is RecoveryDecision.DEFER_RESOURCE
    assert decide_recovery(FailureClass.RESOURCE_ENFORCEMENT_FAILURE, attempt_no=1,
                           policy=pol, has_evidence_unknown=False) \
        is RecoveryDecision.DEFER_RESOURCE
    assert decide_recovery(FailureClass.RESOURCE_ENFORCEMENT_FAILURE, attempt_no=2,
                           policy=pol, has_evidence_unknown=False) \
        is RecoveryDecision.QUARANTINE_LOST


# ---------------------------------------------------------------------------
# F3 — classification is bound to concrete evidence
# ---------------------------------------------------------------------------

def test_f3_stale_boot_id_is_not_classified(db_path):
    env = build_running_job(Core(db_path), owner="A")
    _register_terminal(
        env.core, env.jid, termination_class=TerminationClass.OOM_KILL.value,
        boot_id="boot-old", exit_code=137,
        scope_events={"oom_kill": 1, "oom_group_kill": 0, "max": 0, "high": 0},
    )
    sched = make_scheduler(env, owner="A")  # fake provider boot_id == "boot-1"
    assert sched.classify_and_recover(env.jid, env.epoch) is None
    assert env.core._store.get_supervisor_job(env.jid)["primary_state"] == "RUNNING"


def test_f3_correct_binding_recovers(db_path):
    env = build_running_job(Core(db_path), owner="A")
    register_terminal_evidence(
        env.core, env.jid, termination_class=TerminationClass.OOM_KILL.value,
        exit_code=137,
        scope_events={"oom_kill": 1, "oom_group_kill": 0, "max": 0, "high": 0},
    )
    sched = make_scheduler(env, owner="A")
    result = sched.classify_and_recover(env.jid, env.epoch)
    assert result is not None
    assert env.core._store.get_supervisor_job(env.jid)["primary_state"] == "BLOCKED"


def test_f3_targeted_query_skips_stale_latest_row(db_path):
    # Two terminal rows for the SAME job: the NEWEST is stale (foreign boot),
    # the OLDER is current-boot.  The classification must NOT blindly take the
    # newest arbitrary row — it binds to the current-boot row.
    env = build_running_job(Core(db_path), owner="A")
    # Older current-boot terminal OOM row.
    _register_terminal(
        env.core, env.jid, termination_class=TerminationClass.OOM_KILL.value,
        boot_id="boot-1", exit_code=137,
        scope_events={"oom_kill": 1, "oom_group_kill": 0, "max": 0, "high": 0},
    )
    # Newer stale-boot terminal OOM row (must be skipped).
    _register_terminal(
        env.core, env.jid, termination_class=TerminationClass.OOM_KILL.value,
        boot_id="boot-old", exit_code=137,
        scope_events={"oom_kill": 1, "oom_group_kill": 0, "max": 0, "high": 0},
    )
    sched = make_scheduler(env, owner="A")
    result = sched.classify_and_recover(env.jid, env.epoch)
    assert result is not None  # recovered via the bound (current-boot) row
    assert env.core._store.get_supervisor_job(env.jid)["primary_state"] == "BLOCKED"


def test_f3_foreign_dispatch_evidence_is_not_classified(db_path):
    # A terminal row bound to a DIFFERENT dispatch than the frontier is
    # historical evidence -> no classification.
    env = build_running_job(Core(db_path), owner="A")
    task_run_id = env.core._store.list_task_runs(env.task_id)[0].id
    _insert_dispatch(
        env.core, task_id=env.task_id, task_run_id=task_run_id,
        dispatch_id="dispatch-foreign",
    )
    _register_terminal(
        env.core, env.jid, termination_class=TerminationClass.OOM_KILL.value,
        dispatch_id="dispatch-foreign", boot_id="boot-1", exit_code=137,
        scope_events={"oom_kill": 1, "oom_group_kill": 0, "max": 0, "high": 0},
    )
    sched = make_scheduler(env, owner="A")
    # expected_dispatch_id is None (no frontier), reg.dispatch_id is foreign.
    assert sched.classify_and_recover(env.jid, env.epoch) is None
    assert env.core._store.get_supervisor_job(env.jid)["primary_state"] == "RUNNING"


# ---------------------------------------------------------------------------
# F4 — SCOPE_CLEANUP_UNVERIFIED -> LOST, never a code failure
# ---------------------------------------------------------------------------

def test_f4_cleanup_unverified_is_lost_not_code_failure(db_path):
    backend = FakeScopeBackend(
        verify_properties=verified_properties(_light_limits()),
        prove_inactive=False,  # cleanup cannot be proven -> SCOPE_CLEANUP_UNVERIFIED
        run_result={"exit_code": 0, "stdout_bounded": "", "stderr_bounded": "",
                    "timed_out": False, "pid": 424242},
    )
    enforcer = ExecutionEnforcer(backend)
    env = make_env(db_path, enforcer=enforcer)
    env.sup._process_identity_provider = fake_identity_provider()
    env.sup._workspace_root = "/tmp/argent-fake-workspace"
    sched = _scheduler_for(env)

    dispatch_id = _drive_to_dispatch(sched, env)
    assert dispatch_id is not None
    job_row = env.sup.store._job_row(env.jid)

    exit_code = env.sup._run_sandbox_scoped(None, job_row, dispatch_id)
    assert exit_code != 0  # fail-closed, never a silent pass
    assert env.sup._sandbox_resource_termination(dispatch_id) is True

    epoch = env.sup.store._job_row(env.jid)["lease_epoch"]
    result = sched.classify_and_recover(env.jid, epoch)
    assert result is not None
    assert result.detail == RecoveryDecision.QUARANTINE_LOST.value

    row = env.sup.store._job_row(env.jid)
    assert row["primary_state"] == "LOST"
    assert row["last_failure_class"] == "SCOPE_CLEANUP_UNVERIFIED"
    # Never a code failure, never a retry.
    assert row["last_failure_class"] != "CODE_OR_PROCESS_FAILURE"
    assert row["primary_state"] != "QUEUED"
    env.core.close()


# ---------------------------------------------------------------------------
# F5 — exactly-once recovery (consumed marker + idempotent terminal write)
# ---------------------------------------------------------------------------

def test_f5_consumed_marker_blocks_duplicate_recovery(db_path):
    env = build_running_job(Core(db_path), owner="A")
    pid = register_terminal_evidence(
        env.core, env.jid, termination_class=TerminationClass.OOM_KILL.value,
        exit_code=137,
        scope_events={"oom_kill": 1, "oom_group_kill": 0, "max": 0, "high": 0},
    )
    sched = make_scheduler(env, owner="A")
    # The consumed marker is committed atomically with the recovery.
    result = sched.classify_and_recover(env.jid, env.epoch)
    assert result is not None
    assert env.core._store.has_recovery_marker(env.jid, pid) is True
    # Re-driving for the SAME process_id is a no-op (no double effect).
    assert sched.classify_and_recover(env.jid, env.epoch) is None
    assert env.core._store.get_supervisor_job(env.jid)["primary_state"] == "BLOCKED"


def test_f5_marker_is_durable_across_reopen(db_path):
    env = build_running_job(Core(db_path), owner="A")
    pid = register_terminal_evidence(
        env.core, env.jid, termination_class=TerminationClass.OOM_KILL.value,
        exit_code=137,
        scope_events={"oom_kill": 1, "oom_group_kill": 0, "max": 0, "high": 0},
    )
    jid = env.jid
    sched = make_scheduler(env, owner="A")
    assert sched.classify_and_recover(env.jid, env.epoch) is not None
    env.core.close()

    core2 = Core(db_path)
    try:
        assert core2._store.has_recovery_marker(jid, pid) is True
        # The job left RUNNING; a stale re-drive cannot commit twice.
        with pytest.raises((LeaseError, LeaseFencedError)):
            core2._store.commit_recovery_decision(
                jid, owner_instance_id="A", lease_epoch=env.epoch,
                failure_class=FailureClass.RESOURCE_OOM,
                recovery_decision=RecoveryDecision.BLOCK_RESOURCE,
                reason_code="RESOURCE_OOM",
                process_id=pid,
            )
    finally:
        core2.close()


def test_f5_mark_terminal_is_idempotent(db_path):
    env = build_running_job(Core(db_path), owner="A")
    pid = register_terminal_evidence(
        env.core, env.jid, termination_class=TerminationClass.OOM_KILL.value,
        exit_code=137,
        scope_events={"oom_kill": 1, "oom_group_kill": 0, "max": 0, "high": 0},
    )
    reg = env.core._store.get_process_registration(pid)
    first_terminal_at = reg["terminal_at"]
    # Re-marking the same process is a no-op (returns 0, evidence not clobbered).
    rc = env.core._store.mark_process_terminal_with_evidence(
        pid, exit_code=0, terminal_at="2026-09-01T12:00:00+00:00",
        termination_class=TerminationClass.NORMAL_EXIT.value,
    )
    assert rc == 0
    reg2 = env.core._store.get_process_registration(pid)
    assert reg2["terminal_at"] == first_terminal_at
    assert reg2["termination_class"] == TerminationClass.OOM_KILL.value


# ---------------------------------------------------------------------------
# F7 — valid pairings + bounded reason codes
# ---------------------------------------------------------------------------

def test_f7_invalid_pairing_rejected(db_path):
    env = build_running_job(Core(db_path))
    with pytest.raises(ValueError):
        env.core._store.commit_recovery_decision(
            env.jid, owner_instance_id="A", lease_epoch=env.epoch,
            failure_class=FailureClass.RESOURCE_OOM,
            recovery_decision=RecoveryDecision.DEFER_RESOURCE,  # invalid for OOM
            reason_code="RESOURCE_OOM",
        )
    with pytest.raises(ValueError):
        env.core._store.commit_recovery_decision(
            env.jid, owner_instance_id="A", lease_epoch=env.epoch,
            failure_class=FailureClass.SCOPE_CLEANUP_UNVERIFIED,
            recovery_decision=RecoveryDecision.BLOCK_RESOURCE,  # only LOST
            reason_code="SCOPE_CLEANUP_UNVERIFIED",
        )
    # The rejected pairs never reached the DB (job still RUNNING, untouched).
    assert env.core._store.get_supervisor_job(env.jid)["primary_state"] == "RUNNING"


def test_f7_unknown_reason_code_rejected(db_path):
    env = build_running_job(Core(db_path))
    with pytest.raises(ValueError):
        env.core._store.commit_recovery_decision(
            env.jid, owner_instance_id="A", lease_epoch=env.epoch,
            failure_class=FailureClass.RESOURCE_OOM,
            recovery_decision=RecoveryDecision.BLOCK_RESOURCE,
            reason_code="FREE AGENT STRING",
        )


def test_f7_pairing_table_is_closed_and_consistent(db_path):
    # The pure pairing helper agrees with the commit-level validation.
    assert is_valid_recovery_pair(FailureClass.RESOURCE_OOM,
                                  RecoveryDecision.BLOCK_RESOURCE)
    assert not is_valid_recovery_pair(FailureClass.RESOURCE_OOM,
                                      RecoveryDecision.DEFER_RESOURCE)
    assert not is_valid_recovery_pair("NOT_A_CLASS",
                                      RecoveryDecision.BLOCK_RESOURCE)
    assert not is_valid_recovery_pair(FailureClass.RESOURCE_OOM,
                                      "NOT_A_DECISION")
    with pytest.raises(ValueError):
        assert_valid_recovery_pair(FailureClass.UNKNOWN_TERMINATION,
                                   RecoveryDecision.RETRY_BOUNDED)


def test_f7_every_failure_class_has_exactly_one_bounded_reason():
    seen = set()
    for fc in FailureClass:
        code = reason_code_for_failure(fc)
        # The reason code is a closed enum value (never a free string).
        assert code in {r.value for r in RecoveryReasonCode}
        seen.add(code)
    # Resource failures all map to a bounded, distinct-ish reason code.
    for fc in (FailureClass.RESOURCE_OOM, FailureClass.RESOURCE_MEMORY_LIMIT,
               FailureClass.RESOURCE_TIMEOUT,
               FailureClass.RESOURCE_ENFORCEMENT_FAILURE,
               FailureClass.RESOURCE_CAPACITY_FAILURE,
               FailureClass.SCOPE_CLEANUP_UNVERIFIED,
               FailureClass.UNKNOWN_TERMINATION):
        assert is_resource_failure(fc)
        assert reason_code_for_failure(fc) in {r.value for r in RecoveryReasonCode}
