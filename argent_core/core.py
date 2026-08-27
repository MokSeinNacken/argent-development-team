"""Public API of the deterministic team-control core (SPEC V1 + V1.1).

Every public entry point validates the source trust class first (fail-closed),
then runs its work inside a single ``BEGIN IMMEDIATE`` transaction.  Commands
accept an optional ``idempotency_key``: a repeat with the same key and the same
canonical arguments produces no second state change and no duplicate event; a
repeat with the same key but different arguments raises ``IdempotencyError``.

SPEC V1.1 hardenings (R1–R15) are implemented here: owner-authority gates, role
authority bound to the active role run + source, strict handoff enforcement,
gate entry into the transition table, persisted action executions, conservative
recovery V2 and store encapsulation via the read-only ``Core.queries`` facade.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional
from uuid import uuid4

from . import gates, recovery, roles, state_machine, trust
from .models import (
    ActionClass,
    ActionExecution,
    ActionExecutionStatus,
    ApprovalError,
    ApprovalStatus,
    ArgentError,
    Decision,
    Finding,
    FindingStatus,
    ForbiddenAction,
    IdempotencyError,
    InvalidTransition,
    NotFound,
    OwnerApproval,
    PermissionDenied,
    Project,
    Role,
    RoleConflict,
    RoleRun,
    RoleRunStatus,
    SourceClass,
    Task,
    TaskRun,
    TaskRunStatus,
    TaskState,
    TestResult,
    TestRun,
    UntrustedSource,
    Event,
    Review,
)
from .roles import DEFAULT_NEXT_ROLE
from .state_machine import PAUSE_STATES, TERMINAL_STATES, is_actionable, is_valid_resume_target
from .store import Queries, Store, utcnow
from .trust import role_source

APPROVAL_TTL_SECONDS = 3600

# Map command name -> refetch kind (idempotent replay).
_REPLAY_KIND: dict[str, str] = {
    "create_project": "project",
    "create_task": "task",
    "transition": "task",
    "resume": "task",
    "start_role": "role_run",
    "complete_role": "role_run",
    "fail_role": "role_run",
    "start_task_run": "task_run",
    "complete_task_run": "task_run",
    "fail_task_run": "task_run",
    "approve": "approval",
    "reject": "approval",
    "execute_approved": "approval",
    "add_finding": "finding",
    "resolve_finding": "finding",
    "record_test_run": "test_run",
    "record_review": "review",
    "record_decision": "decision",
}


@dataclass(frozen=True)
class ActionRequestResult:
    action: str
    scope: str
    action_class: ActionClass
    allowed: bool
    approval: Optional[OwnerApproval] = None
    execution_id: Optional[str] = None


@dataclass(frozen=True)
class RecoveryReport:
    interrupted_role_runs: int
    interrupted_task_runs: int
    rolled_back: tuple = field(default_factory=tuple)


class _ApprovalExpired(Exception):
    """Internal signal: approval is expired and should be marked expired."""

    def __init__(self, approval_id: str):
        self.approval_id = approval_id
        super().__init__(approval_id)


def _hash_args(args: dict) -> str:
    """Canonical SHA-256 of a command's arguments (R9)."""
    canonical = json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Core:
    """Deterministic team-control core over a SQLite store."""

    def __init__(self, db_path: str, clock: Optional[Callable[[], datetime]] = None):
        self._clock = clock or utcnow
        self._store = Store(db_path, clock=self._clock)
        self._queries = Queries(self._store)

    # ------------------------------------------------------------------ utils

    @property
    def queries(self) -> Queries:
        """Read-only ``get_*``/``list_*`` query facade (SPEC V1.1 11.5, R8)."""
        return self._queries

    def close(self) -> None:
        self._store.close()

    def _check_source(self, source: str) -> SourceClass:
        cls = trust.classify_source(source)
        if cls is not SourceClass.TRUSTED:
            raise UntrustedSource(f"untrusted source rejected: {source!r}")
        return cls

    def _require_owner(self, source: str) -> None:
        trust.require_owner(source)

    def _coerce_state(self, value) -> TaskState:
        if isinstance(value, TaskState):
            return value
        try:
            return TaskState(value)
        except (ValueError, TypeError):
            raise InvalidTransition(f"unknown state: {value!r}") from None

    def _coerce_role(self, value) -> Role:
        return value if isinstance(value, Role) else Role(value)

    def _coerce_result(self, value) -> TestResult:
        return value if isinstance(value, TestResult) else TestResult(value)

    # ---------------------------------------------------- role authorization

    def _require_role_actor(self, task_id: str, actor_role: Role, source: str) -> None:
        """Require an active ``actor_role`` run and ``source == role:<actor_role>``."""
        expected = role_source(actor_role)
        if source != expected:
            raise PermissionDenied(
                f"operation requires source {expected!r}, got {source!r}"
            )
        active = self._store.get_active_role_run(task_id)
        if active is None or active.role is not actor_role:
            raise PermissionDenied(
                f"task {task_id!r} has no active {actor_role.value} role run"
            )

    def _require_active_role_in(
        self, task_id: str, allowed_roles: tuple[Role, ...], source: str
    ) -> None:
        """Require an active role run whose role is in ``allowed_roles`` and whose
        source matches the active role exactly."""
        active = self._store.get_active_role_run(task_id)
        if active is None:
            raise PermissionDenied(f"task {task_id!r} has no active role run")
        if active.role not in allowed_roles:
            raise PermissionDenied(
                f"role {active.role.value!r} is not allowed for this operation"
            )
        if source != role_source(active.role):
            raise PermissionDenied(
                f"operation requires source {role_source(active.role)!r}, got {source!r}"
            )

    def _require_actionable_state(self, task: Task) -> None:
        """Reject gated actions requested from terminal or pause states.

        A task in ``DONE``/``CANCELLED`` (terminal) or in any pause state
        (``OWNER_APPROVAL_REQUIRED``/``PAUSED``/``RECOVERING``) cannot request
        an action (SPEC V1.2 12.1/12.2): fail-closed, no side effect.
        """
        if not is_actionable(task.state):
            raise InvalidTransition(
                f"cannot request action from state {task.state.value}"
            )

    # ---------------------------------------------------------------- events

    def _emit(
        self,
        type_: str,
        task_id: Optional[str] = None,
        role: Optional[str] = None,
        state: Optional[str] = None,
        payload: Optional[dict] = None,
    ) -> Event:
        ev = Event(
            id=str(uuid4()),
            type=type_,
            task_id=task_id,
            role=role,
            state=state,
            payload=payload or {},
            created_at=self._store.now_iso(),
        )
        self._store._insert_event(ev)
        return ev

    def _apply_transition(
        self,
        task: Task,
        to_state: TaskState,
        new_resume: Optional[TaskState],
    ) -> None:
        self._store._update_task_state(
            task.id, to_state, new_resume, self._store.now_iso()
        )
        self._emit(
            "task.state_changed",
            task_id=task.id,
            state=to_state.value,
            payload={"from_state": task.state.value, "to_state": to_state.value},
        )
        if to_state is TaskState.DONE:
            self._emit(
                "task.completed",
                task_id=task.id,
                state=TaskState.DONE.value,
                payload={},
            )

    def _check_full_binding(
        self, approval: OwnerApproval, task_id: str, action: str, scope: str
    ) -> None:
        if (
            approval.task_id != task_id
            or approval.action != action
            or approval.scope != scope
        ):
            raise ApprovalError(
                "approval binding mismatch: task_id/action/scope must all match "
                f"(approval={approval.task_id!r}/{approval.action!r}/{approval.scope!r}, "
                f"requested={task_id!r}/{action!r}/{scope!r})"
            )

    # ------------------------------------------------------------- idempotency

    def _idempotent(self, key, command, args, work, refetch):
        args_hash = _hash_args(args)
        if key is None:
            with self._store._transaction():
                entity, _rid = work()
            return entity
        with self._store._transaction():
            existing = self._store.get_command_idempotency(key, command)
            if existing is not None:
                result_id, stored_hash = existing
                if stored_hash != args_hash:
                    raise IdempotencyError(
                        f"idempotency key {key!r} reused for {command!r} with "
                        "different arguments"
                    )
                return refetch(result_id)
            entity, rid = work()
            self._store._set_command_idempotency(
                key, command, rid, args_hash, self._store.now_iso()
            )
            return entity

    def _refetch(self, command: str, result_id: str):
        kind = _REPLAY_KIND.get(command)
        if kind == "project":
            obj = self._store.get_project(result_id)
        elif kind == "task":
            obj = self._store.get_task(result_id)
        elif kind == "role_run":
            obj = self._store.get_role_run(result_id)
        elif kind == "task_run":
            obj = self._store.get_task_run(result_id)
        elif kind == "approval":
            return self._approval_result_refetch(result_id)
        elif kind == "finding":
            obj = self._store.get_finding(result_id)
        elif kind == "test_run":
            obj = self._store.get_test_run(result_id)
        elif kind == "review":
            obj = self._store.get_review(result_id)
        elif kind == "decision":
            obj = self._store.get_decision(result_id)
        else:
            raise IdempotencyError(f"unknown command {command!r}")
        if obj is None:
            raise IdempotencyError(f"idempotent replay: entity {result_id!r} missing")
        return obj

    def _approval_result_refetch(self, result_id: str):
        ap = self._store.get_approval(result_id)
        if ap is None:
            raise IdempotencyError(f"idempotent replay: approval {result_id!r} missing")
        return ap

    def _request_action_refetch(self, result_id: str):
        if result_id.startswith("approval:"):
            ap = self._store.get_approval(result_id[len("approval:"):])
            if ap is None:
                raise IdempotencyError(
                    f"idempotent replay: approval {result_id!r} missing"
                )
            return ActionRequestResult(
                action=ap.action,
                scope=ap.scope,
                action_class=ActionClass.OWNER_APPROVAL_REQUIRED,
                allowed=False,
                approval=ap,
            )
        if result_id.startswith("execution:"):
            ex = self._store.get_action_execution(result_id[len("execution:"):])
            if ex is None:
                raise IdempotencyError(
                    f"idempotent replay: execution {result_id!r} missing"
                )
            cls = gates.classify_action(ex.action)
            return ActionRequestResult(
                action=ex.action,
                scope=ex.scope,
                action_class=cls,
                allowed=(ex.status is ActionExecutionStatus.EXECUTED),
                execution_id=ex.id,
            )
        if result_id.startswith("blocked:"):
            return None
        raise IdempotencyError(f"unknown request_action result {result_id!r}")

    # ------------------------------------------------------------- projects

    def create_project(
        self, name: str, source: str, idempotency_key: Optional[str] = None
    ) -> Project:
        self._require_owner(source)
        args = {"name": name, "source": source}

        def work():
            pid = str(uuid4())
            p = Project(id=pid, name=name, created_at=self._store.now_iso(),
                        idempotency_key=idempotency_key)
            self._store._insert_project(p)
            return p, pid

        return self._idempotent(idempotency_key, "create_project", args, work,
                                lambda rid: self._refetch("create_project", rid))

    # ----------------------------------------------------------------- tasks

    def create_task(
        self,
        project_id: str,
        title: str,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> Task:
        self._require_owner(source)
        args = {"project_id": project_id, "title": title, "source": source}

        def work():
            if self._store.get_project(project_id) is None:
                raise NotFound(f"project {project_id!r} not found")
            tid = str(uuid4())
            now = self._store.now_iso()
            t = Task(
                id=tid,
                project_id=project_id,
                title=title,
                state=TaskState.NEW,
                resume_state=None,
                source=source,
                source_class=SourceClass.TRUSTED,
                created_at=now,
                updated_at=now,
                idempotency_key=idempotency_key,
            )
            self._store._insert_task(t)
            self._emit("task.created", task_id=tid, state=TaskState.NEW.value,
                       payload={"project_id": project_id})
            return t, tid

        return self._idempotent(idempotency_key, "create_task", args, work,
                                lambda rid: self._refetch("create_task", rid))

    def transition(
        self,
        task_id: str,
        to_state,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> Task:
        self._check_source(source)
        to_state = self._coerce_state(to_state)
        args = {"task_id": task_id, "to_state": to_state.value, "source": source}

        def work():
            task = self._store.get_task(task_id)
            if task is None:
                raise NotFound(f"task {task_id!r} not found")
            self._require_role_actor(task_id, Role.LEAD, source)
            if task.state in PAUSE_STATES or to_state in PAUSE_STATES:
                raise InvalidTransition(
                    "pause states are reserved for the dedicated gate/resume/"
                    "recover commands"
                )
            state_machine.validate_transition(task.state, to_state, task.resume_state)
            self._apply_transition(task, to_state, None)
            return self._store.get_task(task_id), task_id

        return self._idempotent(idempotency_key, "transition", args, work,
                                lambda rid: self._refetch("transition", rid))

    def resume(
        self,
        task_id: str,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> Task:
        self._check_source(source)
        args = {"task_id": task_id, "source": source}

        def work():
            task = self._store.get_task(task_id)
            if task is None:
                raise NotFound(f"task {task_id!r} not found")
            self._require_role_actor(task_id, Role.LEAD, source)
            if task.state is not TaskState.PAUSED:
                raise InvalidTransition(
                    f"resume requires state PAUSED, got {task.state.value}"
                )
            rs = task.resume_state
            if rs is None:
                raise InvalidTransition("task is PAUSED without a resume_state")
            state_machine.validate_transition(task.state, rs, rs)
            self._apply_transition(task, rs, None)
            return self._store.get_task(task_id), task_id

        return self._idempotent(idempotency_key, "resume", args, work,
                                lambda rid: self._refetch("resume", rid))

    # ------------------------------------------------------------- role runs

    def start_role(
        self,
        task_id: str,
        role,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> RoleRun:
        self._check_source(source)
        role = self._coerce_role(role)
        args = {"task_id": task_id, "role": role.value, "source": source}

        def work():
            task = self._store.get_task(task_id)
            if task is None:
                raise NotFound(f"task {task_id!r} not found")
            if source != role_source(Role.LEAD):
                raise PermissionDenied("start_role is lead-only")
            if (
                task.state in TERMINAL_STATES
                or task.state in PAUSE_STATES
                or task.state is TaskState.BLOCKED
            ):
                raise InvalidTransition(
                    f"cannot start a role run from state {task.state.value}"
                )
            if self._store.get_active_role_run(task_id) is not None:
                raise RoleConflict(f"task {task_id!r} already has an active role run")
            latest = self._store.get_latest_handoff(task_id)
            if latest is None:
                expected = Role.LEAD
            else:
                expected = latest.to_role
            if role is not expected:
                raise RoleConflict(
                    f"next role must be {expected.value!r} (handoff required), "
                    f"got {role.value!r}"
                )
            rid = str(uuid4())
            rr = RoleRun(id=rid, task_id=task_id, role=role,
                         status=RoleRunStatus.STARTED, started_at=self._store.now_iso())
            self._store._insert_role_run(rr)
            self._emit("role.started", task_id=task_id, role=role.value,
                       payload={"role": role.value})
            return rr, rid

        return self._idempotent(idempotency_key, "start_role", args, work,
                                lambda rid: self._refetch("start_role", rid))

    def complete_role(
        self,
        role_run_id: str,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> RoleRun:
        self._check_source(source)
        args = {"role_run_id": role_run_id, "source": source}

        def work():
            rr = self._store.get_role_run(role_run_id)
            if rr is None:
                raise NotFound(f"role run {role_run_id!r} not found")
            if rr.status is not RoleRunStatus.STARTED:
                raise IdempotencyError(
                    f"role run {role_run_id!r} is not started ({rr.status.value})"
                )
            if source != role_source(rr.role):
                raise PermissionDenied(
                    f"complete_role requires source {role_source(rr.role)!r}, "
                    f"got {source!r}"
                )
            now = self._store.now_iso()
            self._store._update_role_run_status(role_run_id, RoleRunStatus.COMPLETED, now)
            to_role = DEFAULT_NEXT_ROLE[rr.role]
            self._store._insert_handoff(
                models_handoff(rr.task_id, rr.role, to_role, now)
            )
            self._emit("role.completed", task_id=rr.task_id, role=rr.role.value,
                       payload={"role": rr.role.value})
            self._emit("handoff.created", task_id=rr.task_id,
                       payload={"from_role": rr.role.value, "to_role": to_role.value})
            return self._store.get_role_run(role_run_id), role_run_id

        return self._idempotent(idempotency_key, "complete_role", args, work,
                                lambda rid: self._refetch("complete_role", rid))

    def fail_role(
        self,
        role_run_id: str,
        source: str,
        detail: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> RoleRun:
        self._check_source(source)
        args = {"role_run_id": role_run_id, "source": source, "detail": detail}

        def work():
            rr = self._store.get_role_run(role_run_id)
            if rr is None:
                raise NotFound(f"role run {role_run_id!r} not found")
            if rr.status is not RoleRunStatus.STARTED:
                raise IdempotencyError(
                    f"role run {role_run_id!r} is not started ({rr.status.value})"
                )
            if source != role_source(rr.role):
                raise PermissionDenied(
                    f"fail_role requires source {role_source(rr.role)!r}, got {source!r}"
                )
            now = self._store.now_iso()
            self._store._update_role_run_status(role_run_id, RoleRunStatus.FAILED, now)
            # Deterministic handoff after failure (SPEC V1.3 13.3): like
            # complete_role, record the next role so the pipeline can continue.
            to_role = DEFAULT_NEXT_ROLE[rr.role]
            self._store._insert_handoff(
                models_handoff(rr.task_id, rr.role, to_role, now)
            )
            payload = {"role": rr.role.value}
            if detail is not None:
                payload["detail"] = detail
            self._emit("role.failed", task_id=rr.task_id, role=rr.role.value,
                       payload=payload)
            self._emit("handoff.created", task_id=rr.task_id,
                       payload={"from_role": rr.role.value, "to_role": to_role.value})
            return self._store.get_role_run(role_run_id), role_run_id

        return self._idempotent(idempotency_key, "fail_role", args, work,
                                lambda rid: self._refetch("fail_role", rid))

    # ------------------------------------------------------------- task runs

    def start_task_run(
        self,
        task_id: str,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> TaskRun:
        self._check_source(source)
        args = {"task_id": task_id, "source": source}

        def work():
            if self._store.get_task(task_id) is None:
                raise NotFound(f"task {task_id!r} not found")
            rid = str(uuid4())
            tr = TaskRun(id=rid, task_id=task_id, status=TaskRunStatus.STARTED,
                         started_at=self._store.now_iso())
            self._store._insert_task_run(tr)
            return tr, rid

        return self._idempotent(idempotency_key, "start_task_run", args, work,
                                lambda rid: self._refetch("start_task_run", rid))

    def complete_task_run(
        self,
        task_run_id: str,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> TaskRun:
        self._check_source(source)
        args = {"task_run_id": task_run_id, "source": source}

        def work():
            tr = self._store.get_task_run(task_run_id)
            if tr is None:
                raise NotFound(f"task run {task_run_id!r} not found")
            if tr.status is not TaskRunStatus.STARTED:
                raise IdempotencyError(f"task run {task_run_id!r} is not started")
            self._store._update_task_run_status(
                task_run_id, TaskRunStatus.COMPLETED, self._store.now_iso()
            )
            return self._store.get_task_run(task_run_id), task_run_id

        return self._idempotent(idempotency_key, "complete_task_run", args, work,
                                lambda rid: self._refetch("complete_task_run", rid))

    def fail_task_run(
        self,
        task_run_id: str,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> TaskRun:
        self._check_source(source)
        args = {"task_run_id": task_run_id, "source": source}

        def work():
            tr = self._store.get_task_run(task_run_id)
            if tr is None:
                raise NotFound(f"task run {task_run_id!r} not found")
            if tr.status is not TaskRunStatus.STARTED:
                raise IdempotencyError(f"task run {task_run_id!r} is not started")
            self._store._update_task_run_status(
                task_run_id, TaskRunStatus.FAILED, self._store.now_iso()
            )
            return self._store.get_task_run(task_run_id), task_run_id

        return self._idempotent(idempotency_key, "fail_task_run", args, work,
                                lambda rid: self._refetch("fail_task_run", rid))

    # ----------------------------------------------------------- gated actions

    def request_action(
        self,
        task_id: str,
        action: str,
        scope: str,
        actor_role,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> ActionRequestResult:
        self._check_source(source)
        actor_role = self._coerce_role(actor_role)
        cls = gates.classify_action(action)
        args = {
            "task_id": task_id,
            "action": action,
            "scope": scope,
            "actor_role": actor_role.value,
            "source": source,
        }

        if cls is ActionClass.FORBIDDEN:
            def work():
                task = self._store.get_task(task_id)
                if task is None:
                    raise NotFound(f"task {task_id!r} not found")
                self._require_role_actor(task_id, actor_role, source)
                self._require_actionable_state(task)
                eid = str(uuid4())
                self._store._insert_action_execution(
                    ActionExecution(
                        id=eid, task_id=task_id, approval_id=None, action=action,
                        scope=scope, actor_role=actor_role.value,
                        status=ActionExecutionStatus.BLOCKED,
                        created_at=self._store.now_iso(),
                    )
                )
                self._emit("lead.decision", task_id=task_id,
                           payload={"blocked": True})
                return None, f"blocked:{eid}"

            self._idempotent(idempotency_key, "request_action", args, work,
                             lambda rid: None)
            raise ForbiddenAction(f"action {action!r} is forbidden")

        if cls is ActionClass.OWNER_APPROVAL_REQUIRED:
            def work():
                task = self._store.get_task(task_id)
                if task is None:
                    raise NotFound(f"task {task_id!r} not found")
                self._require_role_actor(task_id, actor_role, source)
                self._require_actionable_state(task)
                for a in self._store.list_approvals(task_id):
                    if (a.action == action and a.scope == scope
                            and a.status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)):
                        raise ApprovalError(
                            f"duplicate pending/approved approval for {action!r}/{scope!r}"
                        )
                aid = str(uuid4())
                now = self._store.now_iso()
                ap = OwnerApproval(
                    id=aid, task_id=task_id, action=action, scope=scope,
                    status=ApprovalStatus.PENDING, requested_by=actor_role.value,
                    source_class=SourceClass.TRUSTED, created_at=now,
                    decided_at=None, consumed_at=None,
                    expires_at=self._store.expiry_iso(APPROVAL_TTL_SECONDS),
                )
                self._store._insert_approval(ap)
                task = self._store.get_task(task_id)
                state_machine.validate_transition(
                    task.state, TaskState.OWNER_APPROVAL_REQUIRED, task.resume_state
                )
                self._apply_transition(task, TaskState.OWNER_APPROVAL_REQUIRED, task.state)
                self._emit("gate.owner_required", task_id=task_id,
                           payload={"approval_id": aid})
                return (ActionRequestResult(action, scope, cls, allowed=False,
                                            approval=ap), f"approval:{aid}")

            return self._idempotent(idempotency_key, "request_action", args, work,
                                    self._request_action_refetch)

        # AUTONOMOUS: enforce the role permission (chapter 2).
        def work():
            task = self._store.get_task(task_id)
            if task is None:
                raise NotFound(f"task {task_id!r} not found")
            self._require_role_actor(task_id, actor_role, source)
            self._require_actionable_state(task)
            category, mode = gates.permission_for(action)
            roles.check_permission(actor_role, category, mode)
            eid = str(uuid4())
            self._store._insert_action_execution(
                ActionExecution(
                    id=eid, task_id=task_id, approval_id=None, action=action,
                    scope=scope, actor_role=actor_role.value,
                    status=ActionExecutionStatus.EXECUTED,
                    created_at=self._store.now_iso(),
                )
            )
            return (ActionRequestResult(action, scope, cls, allowed=True,
                                        execution_id=eid), f"execution:{eid}")

        return self._idempotent(idempotency_key, "request_action", args, work,
                                self._request_action_refetch)

    # ------------------------------------------------------------ owner gates

    def approve(
        self,
        approval_id: str,
        source: str,
        *,
        task_id: str,
        action: str,
        scope: str,
        idempotency_key: Optional[str] = None,
    ) -> OwnerApproval:
        self._require_owner(source)
        args = {
            "approval_id": approval_id, "task_id": task_id, "action": action,
            "scope": scope, "source": source,
        }

        def work():
            ap = self._store.get_approval(approval_id)
            if ap is None:
                raise ApprovalError(f"approval {approval_id!r} not found")
            self._check_full_binding(ap, task_id, action, scope)
            now = self._store.now_iso()
            if ap.status is not ApprovalStatus.PENDING:
                raise ApprovalError(
                    f"approval {approval_id!r} is not pending ({ap.status.value})"
                )
            if ap.expires_at <= now:
                raise _ApprovalExpired(approval_id)
            rc = self._store._mark_approved(approval_id, now)
            if rc == 0:
                raise ApprovalError(f"approval {approval_id!r} could not be approved")
            self._emit("gate.owner_approved", task_id=ap.task_id,
                       payload={"approval_id": approval_id})
            return self._store.get_approval(approval_id), approval_id

        try:
            return self._idempotent(idempotency_key, "approve", args, work,
                                    lambda rid: self._refetch("approve", rid))
        except _ApprovalExpired as exc:
            with self._store._transaction():
                self._expire_and_release(exc.approval_id)
            raise ApprovalError(f"approval {exc.approval_id!r} expired") from None

    def reject(
        self,
        approval_id: str,
        source: str,
        *,
        task_id: str,
        action: str,
        scope: str,
        idempotency_key: Optional[str] = None,
    ) -> OwnerApproval:
        self._require_owner(source)
        args = {
            "approval_id": approval_id, "task_id": task_id, "action": action,
            "scope": scope, "source": source,
        }

        def work():
            ap = self._store.get_approval(approval_id)
            if ap is None:
                raise ApprovalError(f"approval {approval_id!r} not found")
            self._check_full_binding(ap, task_id, action, scope)
            now = self._store.now_iso()
            rc = self._store._mark_rejected(approval_id, now)
            if rc == 0:
                ap2 = self._store.get_approval(approval_id)
                raise ApprovalError(
                    f"approval {approval_id!r} is not pending ({ap2.status.value})"
                )
            task = self._store.get_task(ap.task_id)
            if task is not None and task.state is TaskState.OWNER_APPROVAL_REQUIRED:
                state_machine.validate_transition(
                    task.state, TaskState.BLOCKED, task.resume_state
                )
                self._apply_transition(task, TaskState.BLOCKED, None)
            self._emit("gate.owner_rejected", task_id=ap.task_id,
                       payload={"approval_id": approval_id})
            return self._store.get_approval(approval_id), approval_id

        return self._idempotent(idempotency_key, "reject", args, work,
                                lambda rid: self._refetch("reject", rid))

    def _expire_and_release(self, approval_id: str) -> None:
        """Expire an approval and release its task from the gate (SPEC V1.3 13.2).

        Runs inside a single transaction.  The approval is marked ``expired``
        and, if the task is still parked in ``OWNER_APPROVAL_REQUIRED``, it is
        moved to its validated ``resume_state`` (non-terminal, non-pause) or to
        ``BLOCKED`` otherwise.  No consumption, no execution.
        """
        now = self._store.now_iso()
        self._store._mark_expired(approval_id, now)
        ap = self._store.get_approval(approval_id)
        if ap is None:
            return
        task = self._store.get_task(ap.task_id)
        if task is None or task.state is not TaskState.OWNER_APPROVAL_REQUIRED:
            return
        resume = task.resume_state
        if resume is not None and is_valid_resume_target(resume):
            state_machine.validate_transition(task.state, resume, task.resume_state)
            self._apply_transition(task, resume, None)
        else:
            state_machine.validate_transition(
                task.state, TaskState.BLOCKED, task.resume_state
            )
            self._apply_transition(task, TaskState.BLOCKED, None)

    def execute_approved(
        self,
        approval_id: str,
        source: str,
        *,
        task_id: str,
        action: str,
        scope: str,
        idempotency_key: Optional[str] = None,
    ) -> OwnerApproval:
        self._require_owner(source)
        args = {
            "approval_id": approval_id, "task_id": task_id, "action": action,
            "scope": scope, "source": source,
        }

        def work():
            ap = self._store.get_approval(approval_id)
            if ap is None:
                raise ApprovalError(f"approval {approval_id!r} not found")
            self._check_full_binding(ap, task_id, action, scope)
            now = self._store.now_iso()
            if ap.status is not ApprovalStatus.APPROVED:
                raise ApprovalError(
                    f"approval {approval_id!r} is not approved ({ap.status.value})"
                )
            if ap.expires_at <= now:
                # Expired 'approved' approval: release it (mark 'expired' and
                # free the task from the gate) so a fresh request can be made
                # (SPEC V1.3 13.2).  Handled atomically in _expire_and_release.
                raise _ApprovalExpired(approval_id)
            # Re-check action class and source class before consuming (R7).
            cls = gates.classify_action(ap.action)
            if cls is ActionClass.FORBIDDEN:
                raise ForbiddenAction(f"action {ap.action!r} is forbidden")
            if ap.source_class is not SourceClass.TRUSTED:
                raise ApprovalError(
                    f"approval {approval_id!r} has an untrusted source class"
                )
            rc = self._store._consume_approval(approval_id, now)
            if rc == 0:
                ap2 = self._store.get_approval(approval_id)
                raise ApprovalError(
                    f"approval {approval_id!r} not consumable ({ap2.status.value})"
                )
            self._store._insert_action_execution(
                ActionExecution(
                    id=str(uuid4()), task_id=ap.task_id, approval_id=approval_id,
                    action=ap.action, scope=ap.scope, actor_role=ap.requested_by,
                    status=ActionExecutionStatus.EXECUTED, created_at=self._store.now_iso(),
                )
            )
            task = self._store.get_task(ap.task_id)
            if task is not None and task.state is TaskState.OWNER_APPROVAL_REQUIRED:
                resume = task.resume_state
                if resume is not None:
                    state_machine.validate_transition(task.state, resume, task.resume_state)
                    self._apply_transition(task, resume, None)
            return self._store.get_approval(approval_id), approval_id

        try:
            return self._idempotent(idempotency_key, "execute_approved", args, work,
                                    lambda rid: self._refetch("execute_approved", rid))
        except _ApprovalExpired as exc:
            with self._store._transaction():
                self._expire_and_release(exc.approval_id)
            raise ApprovalError(f"approval {exc.approval_id!r} expired") from None

    # -------------------------------------------------------- findings / more

    def add_finding(
        self,
        task_id: str,
        severity: str,
        description: str,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> Finding:
        self._check_source(source)
        args = {
            "task_id": task_id, "severity": severity, "description": description,
            "source": source,
        }

        def work():
            if self._store.get_task(task_id) is None:
                raise NotFound(f"task {task_id!r} not found")
            self._require_active_role_in(task_id, (Role.REVIEWER, Role.QA), source)
            fid = str(uuid4())
            f = Finding(id=fid, task_id=task_id, severity=severity,
                        description=description, status=FindingStatus.OPEN,
                        created_at=self._store.now_iso())
            self._store._insert_finding(f)
            self._emit("finding.created", task_id=task_id,
                       payload={"finding_id": fid, "severity": severity})
            return f, fid

        return self._idempotent(idempotency_key, "add_finding", args, work,
                                lambda rid: self._refetch("add_finding", rid))

    def resolve_finding(
        self,
        finding_id: str,
        source: str,
        idempotency_key: Optional[str] = None,
    ) -> Finding:
        self._check_source(source)
        args = {"finding_id": finding_id, "source": source}

        def work():
            f = self._store.get_finding(finding_id)
            if f is None:
                raise NotFound(f"finding {finding_id!r} not found")
            self._require_active_role_in(f.task_id, (Role.REVIEWER, Role.QA), source)
            if f.status is not FindingStatus.OPEN:
                raise IdempotencyError(
                    f"finding {finding_id!r} is not open ({f.status.value})"
                )
            now = self._store.now_iso()
            self._store._update_finding_status(finding_id, FindingStatus.RESOLVED, now)
            self._emit("finding.resolved", task_id=f.task_id,
                       payload={"finding_id": finding_id})
            return self._store.get_finding(finding_id), finding_id

        return self._idempotent(idempotency_key, "resolve_finding", args, work,
                                lambda rid: self._refetch("resolve_finding", rid))

    def record_test_run(
        self,
        task_id: str,
        result,
        source: str,
        detail: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> TestRun:
        self._check_source(source)
        result = self._coerce_result(result)
        args = {"task_id": task_id, "result": result.value, "source": source,
                "detail": detail}

        def work():
            if self._store.get_task(task_id) is None:
                raise NotFound(f"task {task_id!r} not found")
            self._require_active_role_in(task_id, (Role.QA, Role.IMPLEMENTER), source)
            rid = str(uuid4())
            tr = TestRun(id=rid, task_id=task_id, result=result, detail=detail,
                         created_at=self._store.now_iso())
            self._store._insert_test_run(tr)
            self._emit("test.started", task_id=task_id,
                       payload={"test_run_id": rid})
            self._emit("test.completed", task_id=task_id,
                       payload={"test_run_id": rid, "result": result.value})
            return tr, rid

        return self._idempotent(idempotency_key, "record_test_run", args, work,
                                lambda rid: self._refetch("record_test_run", rid))

    def record_review(
        self,
        task_id: str,
        verdict: str,
        source: str,
        detail: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Review:
        self._check_source(source)
        args = {"task_id": task_id, "verdict": verdict, "source": source,
                "detail": detail}

        def work():
            if self._store.get_task(task_id) is None:
                raise NotFound(f"task {task_id!r} not found")
            self._require_active_role_in(task_id, (Role.REVIEWER,), source)
            rid = str(uuid4())
            rv = Review(id=rid, task_id=task_id, verdict=verdict, detail=detail,
                        created_at=self._store.now_iso())
            self._store._insert_review(rv)
            self._emit("review.started", task_id=task_id,
                       payload={"review_id": rid})
            self._emit("review.completed", task_id=task_id,
                       payload={"review_id": rid, "verdict": verdict})
            return rv, rid

        return self._idempotent(idempotency_key, "record_review", args, work,
                                lambda rid: self._refetch("record_review", rid))

    def record_decision(
        self,
        task_id: str,
        decision: str,
        source: str,
        detail: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Decision:
        self._check_source(source)
        args = {"task_id": task_id, "decision": decision, "source": source,
                "detail": detail}

        def work():
            if self._store.get_task(task_id) is None:
                raise NotFound(f"task {task_id!r} not found")
            self._require_role_actor(task_id, Role.LEAD, source)
            did = str(uuid4())
            d = Decision(id=did, task_id=task_id, decision=decision, detail=detail,
                         created_at=self._store.now_iso())
            self._store._insert_decision(d)
            self._emit("lead.decision", task_id=task_id,
                       payload={"decision_id": did})
            return d, did

        return self._idempotent(idempotency_key, "record_decision", args, work,
                                lambda rid: self._refetch("record_decision", rid))

    # --------------------------------------------------------------- recovery

    def recover(self, source: str, idempotency_key: Optional[str] = None) -> RecoveryReport:
        self._require_owner(source)
        args = {"source": source}

        def work():
            report = self._recover_work()
            return report, "recovered"

        return self._idempotent(
            idempotency_key, "recover", args, work,
            lambda rid: RecoveryReport(0, 0, ()),
        )

    def _recover_work(self) -> RecoveryReport:
        now = self._store.now_iso()
        self._emit("system.recovery_started", payload={})

        interrupted_role_runs = self._store.list_role_runs(status=RoleRunStatus.STARTED)
        for rr in interrupted_role_runs:
            self._store._update_role_run_status(rr.id, RoleRunStatus.FAILED, now)
            self._emit("role.failed", task_id=rr.task_id, role=rr.role.value,
                       payload={"role": rr.role.value, "detail": "interrupted"})

        interrupted_task_runs = self._store.list_task_runs(status=TaskRunStatus.STARTED)
        for tr in interrupted_task_runs:
            self._store._update_task_run_status(tr.id, TaskRunStatus.FAILED, now)

        rolled_back = []
        for task, valid in self._store.list_tasks_for_recovery():
            if not valid:
                # Defensive (SPEC V1.2 12.4): unknown resume_state -> BLOCKED,
                # the rest of the recovery continues.
                self._store._update_task_state(task.id, TaskState.BLOCKED, None, now)
                self._emit("task.state_changed", task_id=task.id,
                           state=TaskState.BLOCKED.value,
                           payload={"from_state": task.state.value,
                                    "to_state": TaskState.BLOCKED.value})
                rolled_back.append(
                    (task.id, task.state.value, TaskState.BLOCKED.value)
                )
                continue
            target = recovery.recovery_target(task)
            if target is not task.state:
                # Defense in depth (SPEC V1.3 13.1): reuse the state-machine
                # validation before applying the recovery target; on failure,
                # fall back to BLOCKED.
                try:
                    state_machine.validate_transition(
                        TaskState.RECOVERING, target, task.resume_state
                    )
                except InvalidTransition:
                    target = TaskState.BLOCKED
                if target is not task.state:
                    self._store._update_task_state(task.id, target, None, now)
                    self._emit("task.state_changed", task_id=task.id, state=target.value,
                               payload={"from_state": task.state.value, "to_state": target.value})
                    rolled_back.append((task.id, task.state.value, target.value))

        self._emit("system.recovery_completed",
                   payload={"interrupted_role_runs": len(interrupted_role_runs),
                            "interrupted_task_runs": len(interrupted_task_runs)})
        return RecoveryReport(
            interrupted_role_runs=len(interrupted_role_runs),
            interrupted_task_runs=len(interrupted_task_runs),
            rolled_back=tuple(rolled_back),
        )

    # ----------------------------------------------------------------- events

    def list_events(self, source: str, task_id: Optional[str] = None):
        self._check_source(source)
        return self._store.list_events(task_id)


def models_handoff(task_id: str, from_role: Role, to_role: Role, created_at: str):
    from .models import Handoff

    return Handoff(id=str(uuid4()), task_id=task_id, from_role=from_role,
                   to_role=to_role, created_at=created_at)
