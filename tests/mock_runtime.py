"""Offline, deterministic mock runtime for Phase 2A agent-run simulation.

SPEC V2 chapter 12 / V2.1 15.12: real OpenClaw runs are NOT part of the test
suite.  This module simulates the two things the orchestration layer sees:

- ``spawn`` returns ``(child_session_id, openclaw_run_id)`` (deterministic),
- ``completion_event`` builds the completion ``event_meta`` dict the controller
  would receive (forgeable: any field can be overridden to test provenance).

It also provides privacy-safe builders for the structured role outputs so the
tests stay deterministic and never trip the deny-list.
"""

from __future__ import annotations

import itertools

from argent_core import Role, resolve_model

DEFAULT_THINKING = {
    Role.LEAD: "high",
    Role.ANALYST: "medium",
    Role.IMPLEMENTER: "medium",
    Role.QA: "medium",
    Role.REVIEWER: "high",
}


class MockRuntime:
    """Deterministic spawn/completion simulator."""

    def __init__(self):
        self._counter = itertools.count(1)

    def spawn(self) -> tuple[str, str]:
        n = next(self._counter)
        return f"session-{n}", f"run-{n}"

    def completion_event(
        self,
        task_id: str,
        child_session_id: str,
        run_id: str,
        event_type: str = "agent.completed",
        status: str = "completed",
        parent_dispatch_id=None,
    ) -> dict:
        return {
            "task_id": task_id,
            "child_session_id": child_session_id,
            "run_id": run_id,
            "parent_dispatch_id": parent_dispatch_id,
            "event_type": event_type,
            "status": status,
        }


def default_model(role: Role, risk_class=None):
    """Canonical ``(provider, model, thinking_tier)`` for a role."""
    from argent_core import RiskClass

    risk_class = risk_class or RiskClass.NORMAL
    return resolve_model(role, risk_class)


def base_output(role: Role, task_id: str, dispatch_id: str, **overrides) -> dict:
    """Common structured output fields (privacy-safe)."""
    out = {
        "role": role.value,
        "task_id": task_id,
        "dispatch_id": dispatch_id,
        "status": "ok",
        "findings": [],
        "own_assessment": "work is complete and correct",
        "concerns": [],
        "proposal": "proceed with the agreed plan",
        "alternatives": [],
        "confidence": 0.9,
        "blockers": [],
        "requested_next_state": "PLANNING",
    }
    out.update(overrides)
    return out


def lead_output(
    task_id: str,
    dispatch_id: str,
    decision: str = "accept",
    **overrides,
) -> dict:
    out = base_output(
        Role.LEAD,
        task_id,
        dispatch_id,
        decision=decision,
        accepted_findings=[],
        rejected_findings=[],
        rationale="all checks passed",
    )
    out.update(overrides)
    return out


def analyst_output(
    task_id: str,
    dispatch_id: str,
    root_cause: str = "a missing null guard",
    **overrides,
) -> dict:
    out = base_output(
        Role.ANALYST,
        task_id,
        dispatch_id,
        reproduction="steps to reproduce are clear",
        root_cause=root_cause,
        evidence_refs=["test_a", "test_b"],
    )
    out.update(overrides)
    return out


def implementer_output(
    task_id: str,
    dispatch_id: str,
    changed_files=("src/module.py",),
    **overrides,
) -> dict:
    out = base_output(
        Role.IMPLEMENTER,
        task_id,
        dispatch_id,
        changed_files=list(changed_files),
        implementation_summary="the fix is applied",
        tests_run=["test_a", "test_b"],
    )
    out.update(overrides)
    return out


def qa_output(task_id: str, dispatch_id: str, **overrides) -> dict:
    out = base_output(
        Role.QA,
        task_id,
        dispatch_id,
        tests=[{"name": "test_a", "result": "passed"}],
        failures=[],
        regressions=[],
        coverage_concerns=[],
    )
    out.update(overrides)
    return out


def reviewer_output(
    task_id: str,
    dispatch_id: str,
    recommendation: str = "approve",
    **overrides,
) -> dict:
    out = base_output(
        Role.REVIEWER,
        task_id,
        dispatch_id,
        severity="low",
        security_findings=[],
        architecture_findings=[],
        recommendation=recommendation,
    )
    out.update(overrides)
    return out


OUTPUT_BUILDERS = {
    Role.LEAD: lead_output,
    Role.ANALYST: analyst_output,
    Role.IMPLEMENTER: implementer_output,
    Role.QA: qa_output,
    Role.REVIEWER: reviewer_output,
}


def build_output(role: Role, task_id: str, dispatch_id: str, **overrides) -> dict:
    return OUTPUT_BUILDERS[role](task_id, dispatch_id, **overrides)
