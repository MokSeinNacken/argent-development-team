"""Core data model: enums, dataclasses and typed exceptions.

Everything in this module is deterministic. Enums carry the canonical string
values that are persisted to SQLite. Dataclasses are plain value objects used
at the API boundary and inside the store layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class RiskClass(str, Enum):
    """Risk classification of a task (SPEC V2 3.1)."""

    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"


class ExternalActionsPolicy(str, Enum):
    """Whether external actions are allowed (gated) or forbidden (SPEC V2 8.4)."""

    ALLOWED_WITH_GATE = "ALLOWED_WITH_GATE"
    FORBIDDEN = "FORBIDDEN"


class DispatchStatus(str, Enum):
    """Lifecycle status of an agent dispatch (SPEC V2 3.1)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    CONSUMED = "CONSUMED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    QUARANTINED = "QUARANTINED"
    RECOVERY_PENDING = "RECOVERY_PENDING"


class SequenceKind(str, Enum):
    """Workflow sequence kind of a dispatch (SPEC V2 15.4)."""

    STANDARD = "STANDARD"
    REWORK = "REWORK"


class TaskState(str, Enum):
    """All 16 task states defined in SPEC V1 chapter 1.

    Main path (9): NEW, PLANNING, ANALYZING, LEAD_DECISION, IMPLEMENTING,
    TESTING, REVIEWING, FINAL_DECISION, DONE.
    Additional (7): REWORK, BLOCKED, OWNER_APPROVAL_REQUIRED, PAUSED, FAILED,
    RECOVERING, CANCELLED.
    """

    NEW = "NEW"
    PLANNING = "PLANNING"
    ANALYZING = "ANALYZING"
    LEAD_DECISION = "LEAD_DECISION"
    IMPLEMENTING = "IMPLEMENTING"
    TESTING = "TESTING"
    REVIEWING = "REVIEWING"
    FINAL_DECISION = "FINAL_DECISION"
    DONE = "DONE"

    REWORK = "REWORK"
    BLOCKED = "BLOCKED"
    OWNER_APPROVAL_REQUIRED = "OWNER_APPROVAL_REQUIRED"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    RECOVERING = "RECOVERING"
    CANCELLED = "CANCELLED"


class Role(str, Enum):
    """The five fixed roles of the team (SPEC V1 chapter 2)."""

    LEAD = "lead"
    ANALYST = "analyst"
    IMPLEMENTER = "implementer"
    QA = "qa"
    REVIEWER = "reviewer"


class SourceClass(str, Enum):
    """Trust classification of an input source (SPEC V1 chapter 4)."""

    TRUSTED = "TRUSTED"
    UNTRUSTED = "UNTRUSTED"


class ArtifactCategory(str, Enum):
    """Artifact categories used for the permission matrix."""

    PRODUCT_CODE = "PRODUCT_CODE"
    TEST_CODE = "TEST_CODE"
    OTHER = "OTHER"


class Permission(str, Enum):
    """Artifact access mode."""

    READ = "read"
    WRITE = "write"


class RoleRunStatus(str, Enum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskRunStatus(str, Enum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONSUMED = "consumed"
    EXPIRED = "expired"


class ActionExecutionStatus(str, Enum):
    """Persisted execution outcome of a gated action (SPEC V1.1 11.3, R13)."""

    EXECUTED = "executed"
    BLOCKED = "blocked"


class ActionClass(str, Enum):
    AUTONOMOUS = "AUTONOMOUS"
    OWNER_APPROVAL_REQUIRED = "OWNER_APPROVAL_REQUIRED"
    FORBIDDEN = "FORBIDDEN"


class FindingStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"


class TestResult(str, Enum):
    PASSED = "passed"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------

class ArgentError(Exception):
    """Base class for all errors raised by the deterministic core."""


class InvalidTransition(ArgentError):
    """A state transition that is not in the allowed transition table."""


class RoleConflict(ArgentError):
    """More than one active role run, or a role switch without completion."""


class PermissionDenied(ArgentError):
    """The acting role lacks the required artifact permission."""


class ForbiddenAction(ArgentError):
    """The requested action is permanently forbidden (no approval possible)."""


class ApprovalError(ArgentError):
    """An approval operation failed (wrong status/binding/expiry/reuse)."""


class UntrustedSource(ArgentError):
    """An UNTRUSTED source tried to enter a public API entry point."""


class OwnerAuthorityRequired(ArgentError):
    """An owner-authority operation was attempted by a non-owner source (R3)."""


class PrivacyViolation(ArgentError):
    """An event payload contains a deny-listed key or value."""


class IdempotencyError(ArgentError):
    """A command or event that must be idempotent was re-applied incorrectly."""


class NotFound(ArgentError):
    """A referenced entity does not exist."""


class RolePolicyViolation(ArgentError):
    """A role/model policy violation (SPEC V2 6/7; policy.role_violation)."""


class OutputValidationError(ArgentError):
    """A structured agent output failed validation (SPEC V2 5, fail-closed)."""


class DispatchError(ArgentError):
    """An agent dispatch operation failed provenance/state validation."""


class LeaseError(ArgentError):
    """A durable-queue lease claim/renew/release failed its guard (Phase B1).

    Raised when a job is not claimable (wrong primary_state, not yet eligible,
    terminal/LOST/BLOCKED, or a still-valid foreign lease) or when a
    renew/release call does not hold the current owner+epoch.
    """


class LeaseFencedError(ArgentError):
    """A stale lease holder attempted a mutation after lease takeover (Phase B1).

    Fencing token mismatch: the caller's (owner_instance_id, lease_epoch) is not
    the job's current lease holder, so the mutation was refused and nothing was
    written.
    """


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Project:
    id: str
    name: str
    created_at: str
    idempotency_key: Optional[str] = None


@dataclass(frozen=True)
class Task:
    id: str
    project_id: str
    title: str
    state: TaskState
    resume_state: Optional[TaskState]
    source: str
    source_class: SourceClass
    created_at: str
    updated_at: str
    idempotency_key: Optional[str] = None
    description: Optional[str] = None
    risk_class: RiskClass = RiskClass.NORMAL
    external_actions_policy: ExternalActionsPolicy = ExternalActionsPolicy.ALLOWED_WITH_GATE


@dataclass(frozen=True)
class TaskRun:
    id: str
    task_id: str
    status: TaskRunStatus
    started_at: str
    finished_at: Optional[str] = None


@dataclass(frozen=True)
class RoleRun:
    id: str
    task_id: str
    role: Role
    status: RoleRunStatus
    started_at: str
    finished_at: Optional[str] = None


@dataclass(frozen=True)
class Handoff:
    id: str
    task_id: str
    from_role: Role
    to_role: Role
    created_at: str


@dataclass(frozen=True)
class Finding:
    id: str
    task_id: str
    severity: str
    description: str
    status: FindingStatus
    created_at: str
    resolved_at: Optional[str] = None


@dataclass(frozen=True)
class TestRun:
    id: str
    task_id: str
    result: TestResult
    detail: Optional[str]
    created_at: str


@dataclass(frozen=True)
class Review:
    id: str
    task_id: str
    verdict: str
    detail: Optional[str]
    created_at: str


@dataclass(frozen=True)
class Decision:
    id: str
    task_id: str
    decision: str
    detail: Optional[str]
    created_at: str


@dataclass(frozen=True)
class OwnerApproval:
    id: str
    task_id: str
    action: str
    scope: str
    status: ApprovalStatus
    requested_by: str
    source_class: SourceClass
    created_at: str
    decided_at: Optional[str]
    consumed_at: Optional[str]
    expires_at: str
    # V4 gate-memory closure fields (SPEC V2C §4.3 / §10).
    binding_hash: Optional[str] = None
    approved_at: Optional[str] = None
    execution_id: Optional[str] = None
    executed_at: Optional[str] = None
    closed_at: Optional[str] = None


@dataclass(frozen=True)
class ActionExecution:
    """Persisted record of a gated-action execution (SPEC V1.1 11.3, R13)."""

    id: str
    task_id: str
    approval_id: Optional[str]
    action: str
    scope: str
    actor_role: str
    status: ActionExecutionStatus
    created_at: str


@dataclass(frozen=True)
class Event:
    """Immutable event record (SPEC V1 chapter 6)."""

    id: str
    type: str
    task_id: Optional[str]
    role: Optional[str]
    state: Optional[str]
    payload: dict
    created_at: str


@dataclass(frozen=True)
class AgentDispatch:
    """Provenance record for one expected agent run (SPEC V2 3.1 / V2.1 15.4)."""

    id: str
    task_id: str
    task_run_id: str
    role: Role
    parent_dispatch_id: Optional[str]
    expected_agent_class: str
    expected_model_class: str
    expected_thinking_tier: str
    child_session_id: Optional[str]
    openclaw_run_id: Optional[str]
    actual_provider: Optional[str]
    actual_model: Optional[str]
    thinking_tier: Optional[str]
    status: DispatchStatus
    cycle_no: int
    position: int
    sequence_kind: SequenceKind
    attempt_no: int
    handoff_id: Optional[str]
    result_json: Optional[str]
    created_at: str
    started_at: Optional[str]
    consumed_at: Optional[str]


@dataclass(frozen=True)
class AgentResultQuarantine:
    """Immutable quarantine log entry (SPEC V2 3.1 / V2.1 15.11)."""

    id: str
    task_id: str
    dispatch_id: Optional[str]
    reason: str
    event_meta_json: str
    created_at: str


@dataclass(frozen=True)
class AgentContextSnapshot:
    """Immutable context snapshot for a dispatch (SPEC V2 15.8)."""

    dispatch_id: str
    role: Role
    position: int
    context_hash: str
    context_summary_json: str
    created_at: str
