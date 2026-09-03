"""Shared helpers for Phase I3-C1 CI external-wait tests (offline, deterministic).

Reuses the Phase-B3 ``make_env`` / ``make_running_job`` pattern plus a real
Store (in-memory file DB), a controllable ``FakeClock``, and the deterministic
``FakeCiAdapter``.  No network, no LLM, no real process.
"""

from __future__ import annotations

from types import SimpleNamespace

from argent_core import Core, OWNER_SOURCE
from argent_core.ci_external_wait import (
    CiWaitManager,
    CiWaitSpec,
    FakeCiAdapter,
)
from argent_core.supervisor import Supervisor
from mock_supervisor_runtime import FakeClock, FakeRunStatusProvider

OWNER = OWNER_SOURCE

#: Deterministic, obviously-fake 40-hex SHAs.
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40

REPO = "MokSeinNacken/argent-development-team"
REPO_B = "MokSeinNacken/other-repo"


def make_env(db_path, clock=None):
    clock = clock or FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER)
    sup = Supervisor(core, FakeRunStatusProvider(), clock=clock)
    return SimpleNamespace(core=core, project=project, sup=sup, clock=clock)


def make_running_job(env, owner="A", ttl=600):
    task = env.core.create_task(env.project.id, "t", OWNER)
    job = env.sup.store.create_job(task.id, idempotency_key=f"job-{task.id}")
    claimed = env.core._store.claim_job(
        job.supervisor_job_id, owner_instance_id=owner, ttl_seconds=ttl
    )
    return claimed, task.id


def make_ci_manager(env, adapter=None):
    return CiWaitManager(
        env.core._store,
        adapters={"github": adapter or FakeCiAdapter()},
        clock=env.clock,
    )


def ci_spec(**kw):
    base = dict(
        provider="github",
        repository=REPO,
        pr_number=1,
        expected_head_sha=SHA_A,
        expected_base="main",
        required_checks=("ci",),
        optional_checks=(),
        candidate_id="cand:1",
    )
    base.update(kw)
    return CiWaitSpec(**base)
