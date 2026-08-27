"""Deterministic workflow sequences (SPEC V2 chapter 2 / V2.1 15.4).

The orchestration layer drives roles through a fixed sequence.  The sequence
for a task is derived from its persisted ``(cycle_no, position, sequence_kind)``
plus the last lead decision and any open findings — no extra task column is
needed; replay from the database is unambiguous.

- Standard: ``lead -> analyst -> lead -> implementer -> qa -> reviewer -> lead -> DONE``
- Rework:   ``lead -> implementer -> qa -> [reviewer] -> lead -> DONE``
  (the reviewer entry is conditional on ``lead_decision.rework_include_reviewer``,
  defaulting to ``True`` when reviewer findings are open).

This module is pure (no database access).  The ``Core`` layer computes the
current frontier from the store and delegates here for the sequence math.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import Role, SequenceKind

STANDARD_SEQUENCE: tuple[Role, ...] = (
    Role.LEAD,
    Role.ANALYST,
    Role.LEAD,
    Role.IMPLEMENTER,
    Role.QA,
    Role.REVIEWER,
    Role.LEAD,
)

# Base rework sequence; the reviewer entry is conditional.
REWORK_SEQUENCE: tuple[Role, ...] = (
    Role.LEAD,
    Role.IMPLEMENTER,
    Role.QA,
    Role.REVIEWER,
    Role.LEAD,
)


def rework_sequence(include_reviewer: bool) -> tuple[Role, ...]:
    """Return the effective rework sequence for a boolean reviewer toggle."""
    if include_reviewer:
        return REWORK_SEQUENCE
    return (Role.LEAD, Role.IMPLEMENTER, Role.QA, Role.LEAD)


def effective_sequence(
    sequence_kind: SequenceKind, include_reviewer: bool = True
) -> tuple[Role, ...]:
    """Return the concrete role tuple for a sequence kind."""
    if sequence_kind is SequenceKind.REWORK:
        return rework_sequence(include_reviewer)
    return STANDARD_SEQUENCE


def next_role(
    sequence_kind: SequenceKind, position: int, include_reviewer: bool = True
) -> Optional[Role]:
    """Return the role at ``position`` of the effective sequence, or ``None``.

    ``None`` means the sequence is exhausted (workflow complete).
    """
    seq = effective_sequence(sequence_kind, include_reviewer)
    if position < 0 or position >= len(seq):
        return None
    return seq[position]


def rework_include_reviewer(
    decision_detail: Optional[dict], open_findings_exist: bool
) -> bool:
    """Resolve the reviewer toggle for a rework cycle (SPEC V2 2).

    An explicit non-None ``rework_include_reviewer`` in the lead decision wins;
    otherwise it defaults to ``True`` when reviewer findings are open.
    """
    if decision_detail and decision_detail.get("rework_include_reviewer") is not None:
        return bool(decision_detail["rework_include_reviewer"])
    return bool(open_findings_exist)


@dataclass(frozen=True)
class WorkflowFrontier:
    """The next dispatch position computed from the persisted workflow state."""

    cycle_no: int
    position: int
    sequence_kind: SequenceKind
    include_reviewer: bool
    expected_role: Optional[Role]
