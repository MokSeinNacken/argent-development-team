"""Phase 2A event tests (SPEC V2 10, 12 test 33)."""

import pytest

from argent_core import (
    DispatchStatus,
    Role,
    RolePolicyViolation,
    SequenceKind,
    OWNER_SOURCE,
)
from argent_core.events import AGENT_EVENT_TYPES, PRIVACY_DENYLIST

from conftest import LEAD
from mock_runtime import MockRuntime
from phase2a_helpers import (
    orchestrated_task,
    receive_valid,
    run_role,
    start_and_dispatch,
)

OWNER = OWNER_SOURCE


def _all_agent_event_types_emitted(core, task, task_run, runtime):
    # Dispatch + bind + consume (lead): dispatch_created, started,
    # result_received, result_accepted, completed, handoff.expected,
    # handoff.accepted.
    run_role(core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD)
    # Forged rejection -> agent.result_rejected.
    core.start_role(task.id, Role.ANALYST, LEAD)
    d = core.create_dispatch(
        task.id, task_run.id, Role.ANALYST, 1, 1, SequenceKind.STANDARD, None, LEAD
    )
    session, run = runtime.spawn()
    core.bind_spawn_result(
        d.id, session, run, d.expected_agent_class, d.expected_model_class, "medium", LEAD
    )
    em = runtime.completion_event(task.id, "forged", run)
    core.receive_agent_result(d.id, em, {"role": "analyst"}, LEAD)
    # Duplicate -> agent.result_duplicate (re-deliver valid result twice).
    res = receive_valid(core, runtime, d, session, run, task.id, Role.ANALYST)
    assert res.status == "consumed"
    receive_valid(core, runtime, d, session, run, task.id, Role.ANALYST)
    # Model violation -> policy.role_violation.
    run_role(core, runtime, task, task_run, Role.LEAD, 1, 2, SequenceKind.STANDARD)
    core.start_role(task.id, Role.IMPLEMENTER, LEAD)
    with pytest.raises(RolePolicyViolation):
        core.create_dispatch(
            task.id, task_run.id, Role.IMPLEMENTER, 3, 1, SequenceKind.STANDARD,
            {"provider": "openai", "model": "gpt-5.6-sol", "thinking_tier": "high"},
            LEAD,
        )
    # agent.failed.
    d2 = core.create_dispatch(
        task.id, task_run.id, Role.IMPLEMENTER, 3, 1, SequenceKind.STANDARD, None, LEAD
    )
    s2, r2 = runtime.spawn()
    core.bind_spawn_result(
        d2.id, s2, r2, d2.expected_agent_class, d2.expected_model_class, "medium", LEAD
    )
    core.mark_agent_failed(d2.id, "timeout", LEAD)
    # Recovery -> agent.recovery_pending.
    core.start_role(task.id, Role.IMPLEMENTER, LEAD)
    d3 = core.create_dispatch(
        task.id, task_run.id, Role.IMPLEMENTER, 3, 1, SequenceKind.STANDARD, None, LEAD
    )
    s3, r3 = runtime.spawn()
    core.bind_spawn_result(
        d3.id, s3, r3, d3.expected_agent_class, d3.expected_model_class, "medium", LEAD
    )
    core.recover(OWNER)

    seen = {e.type for e in core.list_events(OWNER, task_id=task.id)}
    return seen


def test_all_agent_event_types_emitted(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    seen = _all_agent_event_types_emitted(core, task, task_run, runtime)
    missing = AGENT_EVENT_TYPES - seen
    assert not missing, f"missing agent event types: {missing}"


def test_new_events_privacy_safe(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    _all_agent_event_types_emitted(core, task, task_run, runtime)
    for e in core.list_events(OWNER, task_id=task.id):
        if not e.type.startswith(("agent.", "handoff.", "policy.")):
            continue
        blob = _flatten(e.payload) + " " + e.type + " " + str(e.role) + " " + str(e.state)
        low = blob.lower()
        for word in PRIVACY_DENYLIST:
            assert word not in low, f"event {e.type} leaks {word!r}: {e.payload}"


def _flatten(obj):
    parts = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            parts.append(str(k))
            parts.append(_flatten(v))
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            parts.append(_flatten(item))
    else:
        parts.append(str(obj))
    return " ".join(parts)
