"""Event system tests (SPEC V1 chapter 6, test points 16, 17 & 18)."""

import pytest

from argent_core import (
    Event,
    PrivacyViolation,
    Role,
    TaskState,
    role_source,
    OWNER_SOURCE,
)
from argent_core.events import EVENT_TYPES, PRIVACY_DENYLIST

from conftest import LEAD, events_of, pipeline_to, start_lead

OWNER = OWNER_SOURCE


def _complete_active(core, task_id):
    active = core.queries.get_active_role_run(task_id)
    core.complete_role(active.id, role_source(active.role))
    return core.queries.list_handoffs(task_id)[-1].to_role


# -- 18: all 19 mandatory event types are emitted at the right places ---------

def test_all_19_event_types_emitted(core, project, task):
    # task.created (fixture), then role/state/decision events.
    start_lead(core, task.id)                                             # role.started
    core.transition(task.id, TaskState.PLANNING, LEAD)                    # state_changed
    core.record_decision(task.id, "proceed", LEAD)                        # lead.decision
    _complete_active(core, task.id)                                       # role.completed + handoff.created

    # analyst
    core.start_role(task.id, Role.ANALYST, LEAD)
    _complete_active(core, task.id)

    # implementer -> test events
    core.start_role(task.id, Role.IMPLEMENTER, LEAD)
    core.record_test_run(task.id, "passed", role_source(Role.IMPLEMENTER))  # test.started + completed
    _complete_active(core, task.id)

    # qa -> finding events + role.failed
    core.start_role(task.id, Role.QA, LEAD)
    core.add_finding(task.id, "high", "a bug", role_source(Role.QA))       # finding.created
    core.resolve_finding(core.queries.list_findings(task.id)[0].id,
                         role_source(Role.QA))                              # finding.resolved
    qa_run = core.queries.get_active_role_run(task.id)
    core.fail_role(qa_run.id, role_source(Role.QA))                         # role.failed + handoff.created (qa->reviewer)

    # reviewer -> review events (fail_role hands qa -> reviewer, SPEC V1.3 13.3)
    core.start_role(task.id, Role.REVIEWER, LEAD)
    core.record_review(task.id, "ok", role_source(Role.REVIEWER))           # review.started + completed
    _complete_active(core, task.id)

    # lead again -> owner gate events
    core.start_role(task.id, Role.LEAD, LEAD)
    res = core.request_action(task.id, "deploy_production", "prod", Role.LEAD, LEAD)  # gate.owner_required
    core.approve(res.approval.id, OWNER, task_id=task.id,
                 action="deploy_production", scope="prod")                  # gate.owner_approved
    core.execute_approved(res.approval.id, OWNER, task_id=task.id,
                          action="deploy_production", scope="prod")
    res2 = core.request_action(task.id, "promote_stable", "prod", Role.LEAD, LEAD)
    core.reject(res2.approval.id, OWNER, task_id=task.id,
                action="promote_stable", scope="prod")                      # gate.owner_rejected
    core.recover(OWNER)                                                     # recovery_started/completed

    # second task to DONE for task.completed.
    t2 = core.create_task(project.id, "t2", OWNER)
    start_lead(core, t2.id)
    for s in [TaskState.PLANNING, TaskState.ANALYZING, TaskState.LEAD_DECISION,
              TaskState.IMPLEMENTING, TaskState.TESTING, TaskState.REVIEWING,
              TaskState.FINAL_DECISION, TaskState.DONE]:
        core.transition(t2.id, s, LEAD)

    seen = {e.type for e in core.list_events(OWNER)}
    assert EVENT_TYPES <= seen, f"missing event types: {EVENT_TYPES - seen}"


def test_event_format_fields(core, task):
    start_lead(core, task.id)
    core.transition(task.id, TaskState.PLANNING, LEAD)
    evs = core.list_events(OWNER)
    assert evs
    for e in evs:
        assert isinstance(e.id, str) and e.id
        assert isinstance(e.type, str)
        assert isinstance(e.created_at, str)
        assert isinstance(e.payload, dict)


def test_state_changed_event_carries_state(core, task):
    start_lead(core, task.id)
    core.transition(task.id, TaskState.PLANNING, LEAD)
    ev = events_of(core, "task.state_changed", task.id)[0]
    assert ev.state == "PLANNING"
    assert ev.payload["from_state"] == "NEW"
    assert ev.payload["to_state"] == "PLANNING"


# -- 17: privacy deny list is fail-closed ------------------------------------

def _insert_event(core, ev):
    return core._store._insert_event(ev)


def _all_events(core):
    return core._store.list_events()


@pytest.mark.parametrize("word", PRIVACY_DENYLIST)
def test_privacy_value_denied(core, word):
    ev = Event(id="ev-value-" + word, type="lead.decision", task_id=None,
               role=None, state=None, payload={"note": f"prefix {word} suffix"},
               created_at="t")
    with pytest.raises(PrivacyViolation):
        _insert_event(core, ev)
    assert all(e.id != ev.id for e in _all_events(core))


@pytest.mark.parametrize("word", PRIVACY_DENYLIST)
def test_privacy_key_denied(core, word):
    ev = Event(id="ev-key-" + word, type="lead.decision", task_id=None,
               role=None, state=None, payload={word: "value"}, created_at="t")
    with pytest.raises(PrivacyViolation):
        _insert_event(core, ev)
    assert all(e.id != ev.id for e in _all_events(core))


def test_privacy_nested_value_denied(core):
    ev = Event(id="ev-nested", type="lead.decision", task_id=None, role=None,
               state=None, payload={"outer": {"inner": ["x", {"deep": "my secret value"}]}},
               created_at="t")
    with pytest.raises(PrivacyViolation):
        _insert_event(core, ev)


def test_privacy_safe_payload_passes(core):
    ev = Event(id="ev-safe", type="lead.decision", task_id=None, role=None,
               state=None, payload={"decision_id": "abc", "state": "NEW"}, created_at="t")
    assert _insert_event(core, ev) is True


def test_scan_all_events_for_forbidden_content(core, project, task):
    start_lead(core, task.id)
    core.transition(task.id, TaskState.PLANNING, LEAD)
    core.record_decision(task.id, "proceed", LEAD)
    _complete_active(core, task.id)
    core.start_role(task.id, Role.ANALYST, LEAD)
    _complete_active(core, task.id)
    core.start_role(task.id, Role.IMPLEMENTER, LEAD)
    core.record_test_run(task.id, "passed", role_source(Role.IMPLEMENTER))
    _complete_active(core, task.id)
    core.start_role(task.id, Role.QA, LEAD)
    core.add_finding(task.id, "high", "a finding", role_source(Role.QA))
    core.resolve_finding(core.queries.list_findings(task.id)[0].id, role_source(Role.QA))
    res = core.request_action(task.id, "deploy_production", "prod", Role.QA,
                              role_source(Role.QA))
    core.approve(res.approval.id, OWNER, task_id=task.id,
                 action="deploy_production", scope="prod")
    core.execute_approved(res.approval.id, OWNER, task_id=task.id,
                          action="deploy_production", scope="prod")
    core.recover(OWNER)

    for e in core.list_events(OWNER):
        blob = _flatten(e.payload)
        low = blob.lower()
        for word in PRIVACY_DENYLIST:
            assert word not in low, f"event {e.type} contains deny-listed term {word!r}: {e.payload}"


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


# -- 16: event idempotency (double insert -> single row) ---------------------

def test_event_insert_or_ignore_idempotent(core):
    ev = Event(id="fixed-id", type="lead.decision", task_id=None, role=None,
               state=None, payload={"blocked": True}, created_at="t")
    assert _insert_event(core, ev) is True
    assert _insert_event(core, ev) is False
    matches = [e for e in _all_events(core) if e.id == "fixed-id"]
    assert len(matches) == 1
