"""Shared deterministic helpers for the Phase C2 execution-enforcement tests.

Provides a scriptable :class:`FakeScopeBackend` (no systemd, no cgroup, no host
I/O) plus a small env builder for the Scheduler-integration tests.  Every value
is explicit so the enforcement outcome is fully deterministic.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from argent_core import Core, OWNER_SOURCE, Role, role_source
from argent_core.execution_scope import (
    ScopeCreateError,
    ScopeVerificationError,
)
from argent_core.resource_policy import ResourceClass, ResourcePolicy
from argent_core.scope_enforcer import ExecutionEnforcer
from argent_core.supervisor import Supervisor
from c1_helpers import make_snapshot
from mock_supervisor_runtime import FakeClock, FakeRunLauncher, FakeRunStatusProvider

OWNER = OWNER_SOURCE
LEAD = role_source(Role.LEAD)


class FakeScopeBackend:
    """Deterministic scope backend (no systemd / cgroup / host I/O).

    Implements the Start-Barrier backend protocol:

    * ``create_scope`` records (scope + placeholder_command + properties) and
      returns the scope with a fixed ``placeholder_pid`` + ``cgroup_path``;
    * ``verify_scope`` returns the configured ``verify_properties`` or raises
      the configured failure;
    * ``start_in_scope`` records the REAL (timeout-wrapped) command and returns
      the scope with the fixed ``process_id``;
    * ``verify_process_binding`` returns ``not fail_bind``;
    * ``prove_inactive`` returns the configured flag (True by default);
    * ``run_in_scope`` returns a canned synchronous run result (for
      ``enforce_and_run``);
    * records cleanup/terminate/stop-placeholder calls.
    """

    def __init__(
        self,
        *,
        process_id: int = 424242,
        placeholder_pid: int = 777777,
        cgroup_path: str = "/user.slice/test/app.slice/argent-c2-test.scope",
        verify_properties=None,
        fail_create=None,
        fail_verify=None,
        fail_bind: bool = False,
        prove_inactive: bool = True,
        memory_events=None,
        run_result=None,
    ):
        self.process_id = process_id
        self.placeholder_pid = placeholder_pid
        self.cgroup_path = cgroup_path
        self.verify_properties = dict(verify_properties or {})
        self.fail_create = fail_create
        self.fail_verify = fail_verify
        self.fail_bind = fail_bind
        self._prove_inactive = prove_inactive
        self.memory_events = dict(memory_events or {})
        self.run_result = run_result or {
            "exit_code": 0, "stdout_bounded": "", "stderr_bounded": "",
            "timed_out": False, "pid": process_id,
        }
        self.created: list = []
        self.started: list = []
        self.cleanup_calls: list = []
        self.terminate_calls: list = []
        self.stop_placeholder_calls: list = []

    def create_scope(self, *, scope, placeholder_command, properties):
        self.created.append({
            "scope": scope,
            "placeholder_command": list(placeholder_command),
            "properties": dict(properties),
        })
        if self.fail_create is not None:
            exc = self.fail_create
            self.fail_create = None
            if isinstance(exc, BaseException):
                raise exc
            raise ScopeCreateError(str(exc))
        return replace(
            scope,
            placeholder_pid=self.placeholder_pid,
            cgroup_path=self.cgroup_path,
        )

    def verify_scope(self, scope):
        if self.fail_verify is not None:
            exc = self.fail_verify
            self.fail_verify = None
            if isinstance(exc, BaseException):
                raise exc
            raise ScopeVerificationError(str(exc))
        return dict(self.verify_properties)

    def start_in_scope(self, *, scope, command, workdir=None):
        self.started.append({"scope": scope, "command": list(command),
                             "workdir": workdir})
        return replace(scope, process_id=self.process_id)

    def verify_process_binding(self, scope):
        return not self.fail_bind

    def stop_placeholder(self, scope):
        self.stop_placeholder_calls.append(scope.scope_name)

    def prove_inactive(self, scope):
        return self._prove_inactive

    def read_memory_events(self, scope):
        return dict(self.memory_events)

    def run_in_scope(self, *, scope, command, timeout=None):
        return dict(self.run_result)

    def cleanup_scope(self, scope):
        self.cleanup_calls.append(scope.scope_name)

    def terminate_scope(self, scope):
        self.terminate_calls.append(scope.scope_name)


def verified_properties(effective_limits, *, cgroup_path=None, process_id=424242):
    """A realistic verified-properties dict matching ``effective_limits``."""
    cpu = int(effective_limits["cpu_quota_percent"])
    return {
        "MemoryMax": str(effective_limits["memory_max_bytes"]),
        "MemoryHigh": str(effective_limits["memory_high_bytes"]),
        "MemorySwapMax": str(effective_limits["swap_max_bytes"]),
        "CPUQuotaPerSecUSec": f"{cpu // 100}s",
        "TasksMax": "64",
        "ControlGroup": cgroup_path or "/user.slice/test/argent-c2-test.scope",
        "ActiveState": "active",
        "memory.max": str(effective_limits["memory_max_bytes"]),
        "memory.high": str(effective_limits["memory_high_bytes"]),
        "memory.swap.max": str(effective_limits["swap_max_bytes"]),
        "cpu.max": f"{cpu * 1000} 100000",
        "pids.max": "64",
    }


class FakeGovernor:
    """Scriptable governor with a real policy (for limit recomputation)."""

    def __init__(self, decision=None, policy=None):
        self.decision = decision
        self.policy = policy or ResourcePolicy()
        self.calls = []

    def decide(self, **kwargs):
        self.calls.append(kwargs)
        return self.decision


class FakeSnapshotProvider:
    """Scriptable snapshot provider (never touches the real host)."""

    def __init__(self, snapshot=None):
        self.snapshot = snapshot or make_snapshot()
        self.captures = []

    def capture(self, workspace_path=None):
        self.captures.append(workspace_path)
        return self.snapshot


def make_env(
    db_path,
    *,
    clock=None,
    resource_class=ResourceClass.HEAVY.value,
    enforcer=None,
    backend=None,
    governor=None,
    snapshot_provider=None,
):
    """Build a Core+Supervisor+job env for C2 Scheduler-integration tests."""
    clock = clock or FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    core.start_task_run(task.id, OWNER)
    launch = FakeRunLauncher()
    sup = Supervisor(core, FakeRunStatusProvider(), launch, clock=clock,
                     enforcer=enforcer,
                     prompts_dir=Path(db_path).parent / "prompts")
    job = sup.store.create_job(
        task.id, idempotency_key="job-main", resource_class=resource_class,
    )
    jid = job.supervisor_job_id
    return SimpleNamespace(
        core=core, project=project, task=task, launch=launch, sup=sup,
        clock=clock, jid=jid, backend=backend,
    )


def make_enforcer(*, backend=None, **kwargs):
    """Build an :class:`ExecutionEnforcer` over a fake backend."""
    return ExecutionEnforcer(backend=backend or FakeScopeBackend(), **kwargs)
