"""Shared helpers for the Phase 2A orchestration test suite."""

from __future__ import annotations

from argent_core import (
    Core,
    OWNER_SOURCE,
    Role,
    SequenceKind,
    role_source,
)

from mock_runtime import MockRuntime, build_output

OWNER = OWNER_SOURCE
LEAD = role_source(Role.LEAD)


def orchestrated_task(core: Core, description="fix the reported issue"):
    """Create a project, an orchestrated task and a task run."""
    project = core.create_project("demo", OWNER)
    task = core.create_task(project.id, "demo-task", OWNER, description=description)
    task_run = core.start_task_run(task.id, OWNER)
    return task, task_run


def start_and_dispatch(core, runtime, task, task_run, role, cycle_no, position, kind):
    """Start the role run, create and bind a dispatch; return the bound dispatch."""
    core.start_role(task.id, role, LEAD)
    d = core.create_dispatch(
        task.id, task_run.id, role, position, cycle_no, kind, None, LEAD
    )
    session, run = runtime.spawn()
    thinking = "high" if role in (Role.LEAD, Role.REVIEWER) else "medium"
    d = core.bind_spawn_result(
        d.id, session, run, d.expected_agent_class, d.expected_model_class, thinking, LEAD
    )
    return d, session, run


def receive_valid(core, runtime, d, session, run, task_id, role, **overrides):
    """Deliver a valid structured output for the bound dispatch."""
    out = build_output(role, task_id, d.id, **overrides)
    em = runtime.completion_event(task_id, session, run)
    return core.receive_agent_result(d.id, em, out, LEAD)


def run_role(core, runtime, task, task_run, role, cycle_no, position, kind, **overrides):
    """Run one full role (start + dispatch + bind + valid result)."""
    d, session, run = start_and_dispatch(
        core, runtime, task, task_run, role, cycle_no, position, kind
    )
    result = receive_valid(core, runtime, d, session, run, task.id, role, **overrides)
    return d, result
