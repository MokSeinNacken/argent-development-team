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
