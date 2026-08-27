"""Phase 2A context-isolation tests (SPEC V2 4, V2.1 15.8)."""

from argent_core import Role, SequenceKind

from conftest import LEAD
from phase2a_helpers import orchestrated_task, run_role
from mock_runtime import MockRuntime


def _ctx(core, task, role, position, repo_summary=None):
    return core.build_agent_context(task.id, role, position, repo_summary or {}, LEAD)


def test_lead_and_reviewer_contexts_differ(core):
    task, _ = orchestrated_task(core)
    lead_ctx = _ctx(core, task, Role.LEAD, 0)
    reviewer_ctx = _ctx(core, task, Role.REVIEWER, 5)
    assert lead_ctx["role"] == "lead"
    assert reviewer_ctx["role"] == "reviewer"
    # Lead sees project rules; reviewer sees security/arch rules.
    assert "project_rules" in lead_ctx
    assert "security_arch_rules" in reviewer_ctx
    assert "project_rules" not in reviewer_ctx


def test_reviewer_context_minimization(core):
    runtime = MockRuntime()
    task, task_run = orchestrated_task(core)
    # Produce an implementer result (own_assessment/proposal exist in output).
    run_role(core, runtime, task, task_run, Role.LEAD, 1, 0, SequenceKind.STANDARD)
    run_role(core, runtime, task, task_run, Role.ANALYST, 1, 1, SequenceKind.STANDARD)
    run_role(core, runtime, task, task_run, Role.LEAD, 1, 2, SequenceKind.STANDARD)
    run_role(core, runtime, task, task_run, Role.IMPLEMENTER, 1, 3, SequenceKind.STANDARD)
    run_role(core, runtime, task, task_run, Role.QA, 1, 4, SequenceKind.STANDARD)
    ctx = _ctx(core, task, Role.REVIEWER, 5)
    blob = str(ctx)
    assert "own_assessment" not in blob
    assert "proposal" not in blob
    assert "implementation_summary" not in blob


def test_analyst_context_has_no_implementer_fields(core):
    task, _ = orchestrated_task(core)
    ctx = _ctx(core, task, Role.ANALYST, 1)
    blob = str(ctx)
    assert "changed_files" not in blob
    assert "implementation_summary" not in blob
    assert "own_assessment" not in blob
    assert "proposal" not in blob


def test_implementer_context_has_write_policy_and_scope(core):
    task, _ = orchestrated_task(core)
    ctx = _ctx(core, task, Role.IMPLEMENTER, 3)
    assert "write_policy" in ctx
    assert ctx["scope"]["task_id"] == task.id


def test_context_snapshot_persisted_and_deterministic(core):
    task, _ = orchestrated_task(core)
    core.start_role(task.id, Role.LEAD, LEAD)
    # Create a dispatch so we can snapshot against it.
    task_run = core.queries.get_latest_task_run(task.id)
    d = core.create_dispatch(
        task.id, task_run.id, Role.LEAD, 0, 1, SequenceKind.STANDARD, None, LEAD
    )
    snap1 = core.snapshot_agent_context(d.id, Role.LEAD, 0, {}, LEAD)
    snap2 = core.snapshot_agent_context(d.id, Role.LEAD, 0, {}, LEAD)
    assert snap1.context_hash == snap2.context_hash
    got = core.queries.get_context_snapshot(d.id)
    assert got is not None
    assert got.context_hash == snap1.context_hash
