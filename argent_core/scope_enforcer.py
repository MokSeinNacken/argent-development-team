"""Phase C2 — execution enforcer (valid admission -> valid scope -> binding).

The central enforcement flow that turns a validated admission into a bounded,
verifiable execution scope and binds it into the process registry:

1. validate the effective limits (fail-closed);
2. generate a safe, local scope name;
3. translate limits to ``systemd-run`` properties;
4. wrap the command in a wall-clock ``timeout`` (the step timeout is wallclock,
   so ``TimeoutStopSec`` is NOT used);
5. **Start-Barrier (F2)**: create the scope with a harmless PLACEHOLDER (NOT the
   agent), verify the scope + properties + cgroup path BEFORE the agent starts,
   then start the real agent detached and move it into the verified scope's
   cgroup, verify the process binding exactly, and terminate the placeholder;
6. return a bounded :class:`EnforcementResult` with the scope + evidence.

Every failure path returns a bounded result WITHOUT starting an unbounded
process, and every failure BEFORE the agent start must prove inactivity
(``SCOPE_CLEANUP_UNVERIFIED`` otherwise — never a silent requeue).  The enforcer
never reads the host beyond the backend, never uses a shell, and never lets
agent output influence scope name / properties / limits / timeout / cgroup path.

``enforce_and_run`` is the SYNCHRONOUS variant (used by the sandbox test path):
it runs a bounded command to completion inside the scope and captures the exit
code, the wall-clock timeout flag and the cgroup ``memory.events`` delta, then
classifies the termination (F5).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Optional, Sequence

from .execution_scope import (
    ExecutionScope,
    ExecutionScopeBackend,
    PLACEHOLDER_COMMAND,
    ScopeCreateError,
    ScopeVerificationError,
    VERIFICATION_VERIFIED,
    generate_scope_name,
    is_valid_scope_name,
    translate_limits_to_properties,
    validate_effective_limits,
    _now_iso,
)
from .resource_failure import (
    TerminationClass,
    classify_termination,
    memory_events_delta,
)
from .resource_policy import ResourceClass, ResourcePolicy


class EnforcementStatus(str, Enum):
    """Bounded spawn/enforcement outcome (the spawn-time subset)."""

    SCOPE_OK = "SCOPE_OK"
    SCOPE_CREATION_FAILED = "SCOPE_CREATION_FAILED"
    SCOPE_VERIFICATION_FAILED = "SCOPE_VERIFICATION_FAILED"
    ENFORCEMENT_UNAVAILABLE = "ENFORCEMENT_UNAVAILABLE"
    # F2: cleanup could not be proven inactive -> LOST quarantine (no requeue).
    SCOPE_CLEANUP_UNVERIFIED = "SCOPE_CLEANUP_UNVERIFIED"
    # Termination classes (post-process evidence; see resource_failure).
    TIMEOUT = "TIMEOUT"
    OOM_MEMORY_LIMIT = "OOM_MEMORY_LIMIT"
    NORMAL_EXIT = "NORMAL_EXIT"
    NONZERO_EXIT = "NONZERO_EXIT"
    UNKNOWN_TERMINATION = "UNKNOWN_TERMINATION"


@dataclass(frozen=True)
class EnforcementResult:
    """Bounded result of one enforcement attempt."""

    status: str  # EnforcementStatus value
    scope: Optional[ExecutionScope] = None
    evidence: dict = field(default_factory=dict)
    exit_code: Optional[int] = None
    terminal_at: str = ""
    # F5: post-termination evidence (synchronous ``enforce_and_run`` only).
    timed_out: bool = False
    scope_events: Optional[dict] = None
    termination_class: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.terminal_at:
            object.__setattr__(self, "terminal_at", _now_iso())

    @property
    def ok(self) -> bool:
        return self.status == EnforcementStatus.SCOPE_OK.value


class TimeoutRunner:
    """Wall-clock timeout wrapper around the child command.

    ``timeout -k <grace> <seconds> <command...>``: after ``seconds`` the child
    gets SIGTERM; if it does not exit within ``grace`` more seconds, SIGKILL.
    The timeout value is a bounded policy value — the agent can never set or
    remove it, and there is no automatic retry with a longer timeout.
    """

    def __init__(
        self,
        *,
        kill_after_seconds: int = 10,
        timeout_cmd: str = "timeout",
    ):
        if not isinstance(kill_after_seconds, int) or kill_after_seconds <= 0:
            raise ValueError("kill_after_seconds must be a positive int")
        self._kill_after = kill_after_seconds
        self._timeout_cmd = timeout_cmd

    @property
    def kill_after_seconds(self) -> int:
        return self._kill_after

    def wrap(self, command: Sequence[str], timeout_seconds: int) -> list:
        """Return ``[timeout, -k, grace, seconds, *command]``."""
        if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive int")
        return [
            self._timeout_cmd, "-k", str(self._kill_after),
            str(timeout_seconds), *[str(c) for c in command],
        ]


class ExecutionEnforcer:
    """Runs the Start-Barrier create -> verify -> start flow, fail-closed."""

    def __init__(
        self,
        backend: ExecutionScopeBackend,
        *,
        policy: Optional[ResourcePolicy] = None,
        timeout_runner: Optional[TimeoutRunner] = None,
    ):
        if backend is None:
            raise ValueError("backend is required")
        self._backend = backend
        self._policy = policy or ResourcePolicy()
        self._timeout_runner = timeout_runner or TimeoutRunner()

    @property
    def policy(self) -> ResourcePolicy:
        return self._policy

    @property
    def backend(self) -> ExecutionScopeBackend:
        return self._backend

    # -- shared preparation ------------------------------------------------

    def _prepare(
        self, *, command, effective_limits, resource_class, policy_version,
        job_id, dispatch_id, scope_name,
    ):
        """Validate limits + name + wrap command; returns (full_command, scope, err)."""
        try:
            limits = validate_effective_limits(
                effective_limits,
                resource_class=resource_class,
                policy=self._policy,
            )
        except ValueError as exc:
            return None, None, EnforcementResult(
                status=EnforcementStatus.ENFORCEMENT_UNAVAILABLE.value,
                evidence={"reason": f"invalid_limits:{exc}"},
            )

        name = scope_name or generate_scope_name(job_id, dispatch_id)
        if not is_valid_scope_name(name):
            return None, None, EnforcementResult(
                status=EnforcementStatus.ENFORCEMENT_UNAVAILABLE.value,
                evidence={"reason": "invalid_scope_name"},
            )

        properties = translate_limits_to_properties(limits)
        try:
            full_command = self._timeout_runner.wrap(command, limits["timeout_seconds"])
        except ValueError as exc:
            return None, None, EnforcementResult(
                status=EnforcementStatus.ENFORCEMENT_UNAVAILABLE.value,
                evidence={"reason": f"invalid_timeout:{exc}"},
            )

        rc_value = resource_class.value if isinstance(resource_class, ResourceClass) \
            else ResourceClass(resource_class).value
        scope = ExecutionScope(
            scope_name=name,
            unit_name=name if name.endswith(".scope") else name + ".scope",
            cgroup_path="",
            job_id=job_id,
            dispatch_id=dispatch_id,
            resource_class=rc_value,
            policy_version=policy_version,
            effective_limits=limits,
            process_id=None,
            created_at=_now_iso(),
        )
        return full_command, scope, None

    # -- Start-Barrier ------------------------------------------------------

    def _cleanup_and_prove(self, scope: ExecutionScope) -> bool:
        """Best-effort cleanup + prove inactivity (F2.6).  True iff provable."""
        try:
            self._backend.cleanup_scope(scope)
        except Exception:
            pass
        try:
            return self._backend.prove_inactive(scope)
        except Exception:
            return False

    def _terminate_and_prove(self, scope: ExecutionScope) -> bool:
        """Best-effort terminate (agent already started) + prove inactivity."""
        try:
            self._backend.terminate_scope(scope)
        except Exception:
            pass
        try:
            self._backend.cleanup_scope(scope)
        except Exception:
            pass
        try:
            return self._backend.prove_inactive(scope)
        except Exception:
            return False

    def _barrier_result(
        self, status: str, scope: ExecutionScope, reason: str,
        *, started: bool,
    ) -> EnforcementResult:
        """Build a failure result; if cleanup is unproven -> SCOPE_CLEANUP_UNVERIFIED."""
        inactive = self._terminate_and_prove(scope) if started else self._cleanup_and_prove(scope)
        final_status = status
        if not inactive:
            final_status = EnforcementStatus.SCOPE_CLEANUP_UNVERIFIED.value
        return EnforcementResult(
            status=final_status,
            scope=scope,
            evidence={"reason": reason, "cleanup_proven": inactive},
        )

    # -- detached spawn -----------------------------------------------------

    def enforce_and_spawn(
        self,
        *,
        command: Sequence[str],
        effective_limits: dict,
        resource_class,
        policy_version: str,
        job_id: str,
        dispatch_id: str,
        scope_name: Optional[str] = None,
    ) -> EnforcementResult:
        """Start-Barrier: create placeholder scope -> verify -> start agent.

        Returns ``SCOPE_OK`` with the verified scope on success, or a bounded
        failure result.  The agent is started only AFTER the scope is verified;
        every failure before that proves inactivity (fail-closed).
        """
        full_command, scope, err = self._prepare(
            command=command, effective_limits=effective_limits,
            resource_class=resource_class, policy_version=policy_version,
            job_id=job_id, dispatch_id=dispatch_id, scope_name=scope_name,
        )
        if err is not None:
            return err

        # 1. create the scope with the placeholder (NOT the agent).
        try:
            scope = self._backend.create_scope(
                scope=scope, placeholder_command=PLACEHOLDER_COMMAND,
                properties=translate_limits_to_properties(scope.effective_limits),
            )
        except ScopeCreateError as exc:
            return EnforcementResult(
                status=EnforcementStatus.SCOPE_CREATION_FAILED.value,
                evidence={"reason": str(exc)},
            )
        except Exception as exc:  # backend bug -> bounded failure, never a crash
            return EnforcementResult(
                status=EnforcementStatus.SCOPE_CREATION_FAILED.value,
                evidence={"reason": f"create_error:{type(exc).__name__}"},
            )

        # 2. verify scope + properties + cgroup (F4) BEFORE agent start.
        try:
            verified = self._backend.verify_scope(scope)
        except ScopeVerificationError as exc:
            return self._barrier_result(
                EnforcementStatus.SCOPE_VERIFICATION_FAILED.value,
                scope, str(exc), started=False,
            )
        except Exception as exc:
            return self._barrier_result(
                EnforcementStatus.SCOPE_VERIFICATION_FAILED.value,
                scope, f"verify_error:{type(exc).__name__}", started=False,
            )

        # 3. read the bounded memory.events baseline (F5) at scope build.
        baseline = self._read_baseline(scope)

        # 4. start the agent inside the verified scope.
        try:
            scope = self._backend.start_in_scope(scope=scope, command=full_command)
        except ScopeCreateError as exc:
            return self._barrier_result(
                EnforcementStatus.SCOPE_CREATION_FAILED.value,
                scope, str(exc), started=True,
            )
        except Exception as exc:
            return self._barrier_result(
                EnforcementStatus.SCOPE_CREATION_FAILED.value,
                scope, f"start_error:{type(exc).__name__}", started=True,
            )

        # 5. verify the process binding exactly (F4.3).
        try:
            bound = self._backend.verify_process_binding(scope)
        except Exception:
            bound = False
        if not bound:
            return self._barrier_result(
                EnforcementStatus.SCOPE_VERIFICATION_FAILED.value,
                scope, "process_binding_unverified", started=True,
            )

        # 6. terminate the placeholder (scope stays alive via the agent).
        try:
            self._backend.stop_placeholder(scope)
        except Exception:
            pass  # best-effort; the scope is kept alive by the agent process

        scope = replace(
            scope,
            memory_events_baseline=baseline,
            verified_properties=verified,
            verification_status=VERIFICATION_VERIFIED,
        )
        return EnforcementResult(
            status=EnforcementStatus.SCOPE_OK.value,
            scope=scope,
            evidence={
                "scope_name": scope.scope_name,
                "unit_name": scope.unit_name,
                "cgroup_path": scope.cgroup_path,
                "process_id": scope.process_id,
                "resource_class": scope.resource_class,
                "policy_version": policy_version,
                "effective_limits": scope.effective_limits,
                "verified_properties": verified,
                "memory_events_baseline": baseline,
            },
        )

    def _read_baseline(self, scope: ExecutionScope) -> dict:
        try:
            return self._backend.read_memory_events(scope)
        except Exception:
            return {}

    # -- synchronous bounded run (sandbox) -----------------------------------

    def enforce_and_run(
        self,
        *,
        command: Sequence[str],
        effective_limits: dict,
        resource_class,
        policy_version: str,
        job_id: str,
        dispatch_id: str,
        scope_name: Optional[str] = None,
    ) -> EnforcementResult:
        """Start-Barrier + synchronous run + termination evidence (F3/F5).

        Creates the scope with a placeholder, verifies it, runs ``command`` to
        completion INSIDE the scope (bounded output capture), reads the cgroup
        ``memory.events`` delta while the placeholder still keeps the scope
        alive, classifies the termination, then terminates the placeholder and
        proves inactivity.
        """
        full_command, scope, err = self._prepare(
            command=command, effective_limits=effective_limits,
            resource_class=resource_class, policy_version=policy_version,
            job_id=job_id, dispatch_id=dispatch_id, scope_name=scope_name,
        )
        if err is not None:
            return err

        try:
            scope = self._backend.create_scope(
                scope=scope, placeholder_command=PLACEHOLDER_COMMAND,
                properties=translate_limits_to_properties(scope.effective_limits),
            )
        except ScopeCreateError as exc:
            return EnforcementResult(
                status=EnforcementStatus.SCOPE_CREATION_FAILED.value,
                evidence={"reason": str(exc)},
            )
        except Exception as exc:
            return EnforcementResult(
                status=EnforcementStatus.SCOPE_CREATION_FAILED.value,
                evidence={"reason": f"create_error:{type(exc).__name__}"},
            )

        try:
            verified = self._backend.verify_scope(scope)
        except ScopeVerificationError as exc:
            return self._barrier_result(
                EnforcementStatus.SCOPE_VERIFICATION_FAILED.value,
                scope, str(exc), started=False,
            )
        except Exception as exc:
            return self._barrier_result(
                EnforcementStatus.SCOPE_VERIFICATION_FAILED.value,
                scope, f"verify_error:{type(exc).__name__}", started=False,
            )

        baseline = self._read_baseline(scope)

        try:
            run = self._backend.run_in_scope(
                scope=scope, command=full_command,
                timeout=scope.effective_limits.get("timeout_seconds"),
            )
        except ScopeCreateError as exc:
            return self._barrier_result(
                EnforcementStatus.SCOPE_CREATION_FAILED.value,
                scope, str(exc), started=True,
            )
        except Exception as exc:
            return self._barrier_result(
                EnforcementStatus.SCOPE_CREATION_FAILED.value,
                scope, f"run_error:{type(exc).__name__}", started=True,
            )

        # Read the delta while the placeholder still keeps the scope alive.
        current = self._read_baseline(scope)
        delta = memory_events_delta(baseline, current)
        exit_code = run.get("exit_code")
        timed_out = bool(run.get("timed_out"))
        termination_class = classify_termination(
            exit_code=exit_code, scope_events=delta, timed_out=timed_out,
        ).value

        # Terminate the placeholder and prove inactivity.
        try:
            self._backend.stop_placeholder(scope)
        except Exception:
            pass
        inactive = self._cleanup_and_prove(scope)

        status = EnforcementStatus.SCOPE_OK.value if inactive \
            else EnforcementStatus.SCOPE_CLEANUP_UNVERIFIED.value
        scope = replace(
            scope,
            process_id=run.get("pid"),
            memory_events_baseline=baseline,
            verified_properties=verified,
            verification_status=VERIFICATION_VERIFIED,
        )
        return EnforcementResult(
            status=status,
            scope=scope,
            exit_code=exit_code,
            timed_out=timed_out,
            scope_events=delta,
            termination_class=termination_class,
            evidence={
                "scope_name": scope.scope_name,
                "unit_name": scope.unit_name,
                "cgroup_path": scope.cgroup_path,
                "process_id": scope.process_id,
                "resource_class": scope.resource_class,
                "policy_version": policy_version,
                "effective_limits": scope.effective_limits,
                "verified_properties": verified,
                "stdout_bounded": run.get("stdout_bounded", ""),
                "stderr_bounded": run.get("stderr_bounded", ""),
                "cleanup_proven": inactive,
            },
        )

    def _safe_cleanup(self, scope: ExecutionScope) -> None:
        try:
            self._backend.cleanup_scope(scope)
        except Exception:
            pass
