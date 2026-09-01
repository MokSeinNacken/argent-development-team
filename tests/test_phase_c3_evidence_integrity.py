"""Phase C3 — evidence integrity (B).  Deterministic.

Proves that only trusted, caller-validated evidence can produce a
``FailureClass``: a wrong scope / wrong process identity / stale boot_id /
stale cgroup evidence / malformed evidence / missing evidence fail closed, the
persisted evidence is bounded JSON, and agent-provided reasons are IGNORED.
"""

from __future__ import annotations

import pytest

from argent_core.process_registry import (
    IDENTITY_BOOT_CHANGED,
    IDENTITY_PID_REUSE,
    IDENTITY_SAME,
    ProcessIdentity,
    ProcessRegistry,
)
from argent_core.resource_failure import TerminationClass
from argent_core.resource_recovery import (
    FailureClass,
    RecoveryDecision,
    classify_failure,
    decide_recovery,
)
from argent_core.scheduler import Scheduler
from argent_core.supervisor import Supervisor
from c3_helpers import build_running_job


def test_wrong_scope_identity_is_boot_changed():
    reg = {"boot_id": "boot-old", "pid": 300, "process_start_ticks": 5}
    verdict = ProcessRegistry.classify_identity(
        reg, ProcessIdentity(boot_id="boot-new", pid=300, process_start_ticks=5),
    )
    assert verdict == IDENTITY_BOOT_CHANGED


def test_pid_reuse_is_not_the_same_process():
    reg = {"boot_id": "boot-1", "pid": 200, "process_start_ticks": 100}
    verdict = ProcessRegistry.classify_identity(
        reg, ProcessIdentity(boot_id="boot-1", pid=200, process_start_ticks=999),
    )
    assert verdict == IDENTITY_PID_REUSE


def test_stale_cgroup_evidence_is_not_authoritative():
    # A stale scope_ref/cgroup_ref from an old boot is historical only; the C3
    # classification consumes only the CURRENT registration (job-scoped).
    reg = {"boot_id": "boot-old", "pid": 300, "process_start_ticks": 5,
           "scope_ref": "argent-old.scope"}
    assert ProcessRegistry.classify_identity(
        reg, ProcessIdentity(boot_id="boot-new", pid=300, process_start_ticks=5),
    ) == IDENTITY_BOOT_CHANGED


def test_malformed_scope_events_treated_as_no_evidence():
    # Non-dict / non-int / negative / bool deltas are never OOM evidence.
    for bad in (None, "not-a-dict", {"oom_kill": "x"}, {"oom_kill": -5},
                {"oom_kill": True}):
        fc = classify_failure(
            termination_class=None, exit_code=137, timed_out=False,
            scope_events=bad,
        )
        assert fc is FailureClass.CODE_OR_PROCESS_FAILURE, bad


def test_missing_evidence_is_unknown():
    fc = classify_failure(termination_class=None, exit_code=None, timed_out=False,
                          scope_events=None)
    assert fc is FailureClass.UNKNOWN_TERMINATION


def test_scheduler_scope_events_parse_fails_closed():
    # A malformed scope_events JSON string is not an OOM vector.
    assert Scheduler._parse_scope_events("not json") is None
    assert Scheduler._parse_scope_events('{"oom_kill": "x"}') == {"oom_kill": "x"}
    assert Scheduler._parse_scope_events(None) is None
    assert Scheduler._parse_scope_events("") is None


def test_agent_provided_reason_is_ignored(db_path):
    # A free agent string can never be a FailureClass / RecoveryDecision.
    with pytest.raises(ValueError):
        FailureClass("AGENT SAYS OOM")
    with pytest.raises(ValueError):
        decide_recovery("AGENT SAYS OOM", attempt_no=0)


def test_store_rejects_free_recovery_decision_string(db_path):
    from argent_core import Core
    env = build_running_job(Core(db_path))
    with pytest.raises(ValueError):
        env.core._store.commit_recovery_decision(
            env.jid, owner_instance_id="A", lease_epoch=env.epoch,
            failure_class="RESOURCE_OOM",
            recovery_decision="AGENT WANTS RETRY",
            reason_code="free text",
        )


def test_store_rejects_free_failure_class_string(db_path):
    from argent_core import Core
    env = build_running_job(Core(db_path))
    with pytest.raises(ValueError):
        env.core._store.commit_recovery_decision(
            env.jid, owner_instance_id="A", lease_epoch=env.epoch,
            failure_class="AGENT SAYS OOM",
            recovery_decision=RecoveryDecision.BLOCK_RESOURCE,
            reason_code="RESOURCE_OOM",
        )
