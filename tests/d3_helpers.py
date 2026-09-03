"""Shared deterministic helpers for the Phase D3 acceptance + hardening tests.

Builds deterministic Core/Supervisor/store environments (fake enforcer/governor/
clock, no providers, no host I/O) and provides provider-neutral pack metrics.
"""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from argent_core import Core, OWNER_SOURCE, Role, role_source
from argent_core.resource_governor import (
    AdmissionDecision,
    AdmissionVerdict,
    ResourceReasonCode,
)
from argent_core.resource_policy import ResourceClass, ResourcePolicy
from argent_core.scope_enforcer import ExecutionEnforcer
from argent_core.supervisor import Supervisor
from c2_helpers import (
    FakeGovernor,
    FakeScopeBackend,
    FakeSnapshotProvider,
    verified_properties,
)
from mock_runtime import build_output
from mock_supervisor_runtime import (
    FakeClock,
    FakeRunLauncher,
    FakeRunStatusProvider,
)

OWNER = OWNER_SOURCE
LEAD = role_source(Role.LEAD)


# ---------------------------------------------------------------------------
# Provider-neutral pack metrics (Context-Economy / acceptance evidence)
# ---------------------------------------------------------------------------

def pack_metrics(pack) -> dict:
    """Provider-neutral, deterministic acceptance/audit metrics for a pack."""
    return {
        "token_count": pack.token_count,
        "budget_estimated": pack.budget_estimated,
        "budget_soft": pack.budget_soft,
        "budget_hard": pack.budget_hard,
        "item_count": len(pack.items),
        "artifact_refs": len(pack.artifacts),
        "excerpts": sum(1 for a in pack.artifacts if a.excerpt),
        "history_items": sum(1 for it in pack.items if it.source_type == "history"),
        "prior_result_items": sum(
            1 for it in pack.items if it.source_type == "prior_result"),
        "required_items": sum(
            1 for it in pack.items if it.importance == "REQUIRED"),
        "expansion_reason": pack.expansion_reason,
    }


def _limits():
    pol = ResourcePolicy()
    base = pol.limits_for(ResourceClass.HEAVY)
    return {
        "memory_high_bytes": base.memory_high_bytes,
        "memory_max_bytes": base.memory_max_bytes,
        "swap_max_bytes": base.swap_max_bytes,
        "cpu_quota_percent": base.cpu_quota_percent,
        "timeout_seconds": base.timeout_seconds,
    }


def d3_admission():
    return AdmissionDecision(
        resource_class=ResourceClass.HEAVY.value, policy_version="1",
        snapshot_ref="snap-1", decision=AdmissionVerdict.ALLOW.value,
        reason_code=ResourceReasonCode.OK.value, next_eligible_at=None,
        effective_limits=_limits(), timestamp="2026-09-01T00:00:00+00:00",
    )


def make_d3_env(db_path, *, context_builder=None, retriever=None,
                checkpoint_store=None, resource_class=ResourceClass.HEAVY.value,
                description="fix the bug"):
    """Core+Supervisor+job env with fake enforcer/governor + optional D2 wiring."""
    clock = FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER, description=description)
    core.start_task_run(task.id, OWNER)
    launch = FakeRunLauncher()
    backend = FakeScopeBackend(verify_properties=verified_properties(_limits()))
    enforcer = ExecutionEnforcer(backend)
    sup = Supervisor(
        core, FakeRunStatusProvider(), launch, clock=clock,
        enforcer=enforcer, context_builder=context_builder,
        retriever=retriever, checkpoint_store=checkpoint_store,
        prompts_dir=Path(db_path).parent / "prompts",
    )
    job = sup.store.create_job(task.id, idempotency_key="job-main",
                               resource_class=resource_class)
    jid = job.supervisor_job_id
    return SimpleNamespace(
        core=core, project=project, task=task, launch=launch, sup=sup,
        clock=clock, jid=jid, backend=backend,
    )


# ---------------------------------------------------------------------------
# Full-flow E2E (real git worktree + fake enforcer/governor, no providers)
# ---------------------------------------------------------------------------

def _git_init(ws) -> None:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    for args in (("init", "-q", "-b", "main"), ("add", "-A"),
                 ("commit", "-q", "-m", "init")):
        subprocess.run(["git", "-C", str(ws), *args], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)


def _d3_implementer(role, task_id, dispatch_id, content=None):
    """Implementer result: declares a changed file AND writes a real patch."""
    out = build_output(role, task_id, dispatch_id)
    if role is Role.IMPLEMENTER:
        out["changed_files"] = ["src/module.py"]
        content = content if content is not None else b"def fixed():\n    return True\n"
        out["patch_set"] = [{
            "op": "write",
            "path": "src/module.py",
            "content": base64.b64encode(content).decode(),
        }]
    return out


def _fake_run_tests(workspace, pytest_args=None, limits=None):
    from argent_core.sandbox_runner import SandboxResult
    return SandboxResult(exit_code=0, stdout_bounded="", stderr_bounded="",
                         timed_out=False, wall_seconds=0.0)


class D3AutoProvider:
    """Per-dispatch: authoritative NOT_FOUND -> RUNNING -> SUCCEEDED.

    Unlike ``AutoRunStatusProvider`` (which pretends the run already exists and
    therefore never triggers ``SPAWN_RUN``), this provider reports an
    authoritative NOT_FOUND for a fresh unbound dispatch so the supervisor's
    real ``SPAWN_RUN`` (context-pack build + scoped spawn) is exercised, then
    RUNNING (bind), then SUCCEEDED (consume).  No provider calls.
    """

    def __init__(self, core, result_builder=None):
        from mock_supervisor_runtime import make_run_observation
        self.core = core
        self.result_builder = result_builder or (lambda r, t, d: build_output(r, t, d))
        self._make_obs = make_run_observation
        self._phase = {}
        self.observe_calls = []

    def observe(self, lookup):
        from argent_core.supervisor import RunStatus
        from mock_supervisor_runtime import canonical_binding
        self.observe_calls.append(lookup)
        d = self.core.queries.get_dispatch(lookup.dispatch_id)
        if d is None:
            return self._make_obs(
                dispatch_id=lookup.dispatch_id, role=Role.LEAD,
                status=RunStatus.NOT_FOUND, authoritative_not_found=True)
        n = self._phase.get(d.id, 0)
        self._phase[d.id] = n + 1
        provider, model, thinking, session = canonical_binding(d)
        if d.child_session_id is None:
            if n == 0:
                return self._make_obs(
                    dispatch_id=d.id, role=d.role,
                    status=RunStatus.NOT_FOUND, authoritative_not_found=True)
            return self._make_obs(
                dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
                run_id=f"run-{d.id[:8]}", session_id=session,
                provider=provider, model=model, thinking_tier=thinking)
        result = self.result_builder(d.role, d.task_id, d.id)
        return self._make_obs(
            dispatch_id=d.id, role=d.role, status=RunStatus.SUCCEEDED,
            run_id=d.openclaw_run_id, session_id=d.child_session_id,
            provider=provider, model=model, thinking_tier=thinking,
            result=result)


def make_d3_e2e_env(tmp_path, *, implementer_content=None):
    """Full deterministic E2E env: git worktree + fake enforcer/governor + D2 wiring."""
    from argent_core.checkpoint import CheckpointStore
    from argent_core.retrieval import RetrievalEngine, make_default_policy
    from argent_core.worktree import GitProvenanceProvider
    from argent_core.scheduler import Scheduler

    clock = FakeClock()
    db = str(tmp_path / "e2e.db")
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True, exist_ok=True)
    (ws / "tests").mkdir(parents=True, exist_ok=True)
    (ws / "src" / "module.py").write_text("# stub\n")
    _git_init(ws)

    core = Core(db, clock=clock)
    project = core.create_project("p", OWNER)
    task = core.create_task(project.id, "t", OWNER)
    core.start_task_run(task.id, OWNER)

    def result_builder(role, task_id, dispatch_id):
        return _d3_implementer(role, task_id, dispatch_id,
                               content=implementer_content)

    prov = D3AutoProvider(core, result_builder=result_builder)
    backend = FakeScopeBackend(verify_properties=verified_properties(_limits()))
    enforcer = ExecutionEnforcer(backend)
    git = GitProvenanceProvider(str(ws))
    retriever = RetrievalEngine(
        policy=make_default_policy(allowed_roots=[str(ws)]), store=core._store)
    checkpoint_store = CheckpointStore(core._store, git_provenance_provider=git)

    sup = Supervisor(
        core, prov, FakeRunLauncher(), clock=clock,
        workspace_root=str(ws), run_tests_fn=_fake_run_tests,
        enforcer=enforcer,
        resource_governor=FakeGovernor(d3_admission()),
        snapshot_provider=FakeSnapshotProvider(),
        retriever=retriever,
        checkpoint_store=checkpoint_store,
        git_provenance_provider=git,
        prompts_dir=tmp_path / "prompts",
    )
    job = sup.store.create_job(task.id, idempotency_key="job-main",
                               resource_class=ResourceClass.HEAVY.value)
    jid = job.supervisor_job_id
    sched = Scheduler(sup, owner_instance_id="instance-A", lease_ttl_seconds=600,
                      resource_governor=FakeGovernor(d3_admission()),
                      snapshot_provider=FakeSnapshotProvider())
    return SimpleNamespace(
        core=core, project=project, task=task, sup=sup, sched=sched,
        clock=clock, jid=jid, backend=backend, ws=ws,
    )


def drive_to_terminal(env, max_passes=400):
    """Drive the scheduler until the job reaches a terminal state (or cap)."""
    from argent_core.scheduler import Scheduler
    final = None
    for _ in range(max_passes):
        r = env.sched.run_pass(env.jid)
        final = r
        row = env.core._store.get_supervisor_job(env.jid)
        if row is not None and row["terminal"] is not None:
            break
    return final, env.core._store.get_supervisor_job(env.jid)


# re-export for convenience

