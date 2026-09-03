"""Shared helpers for Phase I2 integration / merge-queue tests.

Deterministic: real git in tmp_path fixture repos (argv only, no shell), real
Store, no network, no LLM.  Provides the small fixture machinery to build a
base commit, source (writer) jobs with proven provenance, a holder job, and a
MergeQueue with a deterministic PASS test runner.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

from argent_core import Core, OWNER_SOURCE
from argent_core.merge_queue import MergeQueue, make_integration_evidence
from argent_core.supervisor import Supervisor
from argent_core.test_execution import compute_snapshot_identity
from mock_supervisor_runtime import FakeRunLauncher, FakeRunStatusProvider

#: Shared deterministic evidence MAC key (tests + fake runners sign with it;
#: production holds its own secret key).  >= 16 bytes (Phase F F7).
TEST_MAC_KEY = b"k" * 32


def run_git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args],
                          capture_output=True, text=True)


def commit_all(repo, msg):
    run_git(repo, "add", "-A")
    run_git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-m", msg)


def git_sha(repo, ref="HEAD"):
    return run_git(repo, "rev-parse", ref).stdout.strip()


def init_repo(root: str) -> str:
    """Create a git repo with branch ``main`` and a base commit. Returns path."""
    repo = os.path.join(root, "repo")
    os.makedirs(repo, exist_ok=True)
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "t@t")
    run_git(repo, "config", "user.name", "t")
    Path(repo, "app.py").write_text("def add(a, b):\n    return a + b\n")
    os.makedirs(os.path.join(repo, "tests"), exist_ok=True)
    Path(repo, "tests", "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    commit_all(repo, "base")
    return repo


def write_commit(repo, relpath, content, msg):
    """Write ``relpath`` and commit on the CURRENT branch; returns new sha."""
    p = Path(repo) / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    commit_all(repo, msg)
    return git_sha(repo)


def new_branch(repo, name, from_ref="main"):
    run_git(repo, "checkout", "-q", "-b", name, from_ref)
    return name


def checkout(repo, ref):
    run_git(repo, "checkout", "-q", ref)


class PassPlan:
    plan_hash = "plan-hash"
    verdict = "DONE"


def pass_plan_builder(candidate, changed, base_sha):
    return PassPlan()


def make_test_evidence(verdict, plan, worktree_path, *, mac_key=TEST_MAC_KEY,
                       summary="deterministic PASS", test_count=1):
    """Build authenticated integration evidence (I2 HIGH-7) bound to the real
    snapshot identity of the integration worktree, signed with ``mac_key``."""
    snap = compute_snapshot_identity(worktree_path)
    return make_integration_evidence(
        verdict, plan.plan_hash, snap.source_hash, snap.test_definition_hash,
        summary=summary, test_count=test_count, mac_key=mac_key,
    )


def pass_test_runner(candidate, worktree_path, plan, changed):
    return make_test_evidence("DONE", plan, worktree_path)


def make_env(db_path):
    core = Core(db_path)
    project = core.create_project("p", OWNER_SOURCE)
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher())
    return core, project, sup


def make_source(
    core, project, sup, title, *, repo, branch, head, base,
    risk="NORMAL", open_findings=0, depends_on=None, mutation_paths=None,
):
    """Create a terminal-DONE source job with proven provenance.

    Returns the job id.  The job is claimed, given trusted metadata + git
    provenance, then closed DONE (immutable terminal).
    """
    task = core.create_task(project.id, title, OWNER_SOURCE)
    core.start_task_run(task.id, OWNER_SOURCE)
    if risk != "NORMAL":
        core._store._conn.execute(
            "UPDATE tasks SET risk_class = ? WHERE id = ?", (risk, task.id))
    job = sup.store.create_job(task.id, idempotency_key=f"job-{title}-{uuid.uuid4().hex[:8]}")
    jid = job.supervisor_job_id
    claimed = sup.store.claim_job(jid, owner_instance_id="OWNER", ttl_seconds=3600)
    epoch = claimed["lease_epoch"]
    if mutation_paths is None:
        mutation_paths = [f"src_{title}.py"]
    # Truthful fixture (I2 HIGH-3): ensure the recorded source branch exists at
    # the recorded head so admission's authoritative git-evidence verification
    # (branch tip == source head, base ancestry, top-level == repo identity)
    # can actually pass.
    if run_git(repo, "rev-parse", "--verify", f"refs/heads/{branch}").returncode != 0:
        run_git(repo, "branch", branch, head)
    sup.store.set_job_metadata(
        jid, owner_instance_id="OWNER", lease_epoch=epoch,
        repo_identity=repo, canonical_worktree_path=repo,
        branch_identity=branch, mutation_path_roots=mutation_paths,
        depends_on=depends_on,
    )
    core._store._update_supervisor_job(
        jid, base_commit=base, expected_head=head, current_head=head,
        status="TERMINAL", terminal="DONE", next_action="NONE",
        open_findings_count=open_findings,
    )
    return jid, task.id


def make_holder(core, project, sup):
    """Create + claim a holder (integration authority) job. Returns (id, epoch)."""
    task = core.create_task(project.id, f"holder-{uuid.uuid4().hex[:6]}", OWNER_SOURCE)
    core.start_task_run(task.id, OWNER_SOURCE)
    job = sup.store.create_job(task.id, idempotency_key=f"holder-{uuid.uuid4().hex[:8]}")
    hid = job.supervisor_job_id
    claimed = sup.store.claim_job(hid, owner_instance_id="OWNER", ttl_seconds=3600)
    return hid, claimed["lease_epoch"]


def make_mq(core, worktrees_root):
    mq = MergeQueue(core._store, worktrees_root=worktrees_root, mac_key=TEST_MAC_KEY)
    mq._plan_builder = pass_plan_builder
    mq._test_runner = pass_test_runner
    return mq


def add_review(core, task_id, verdict, source_class="controller"):
    rid = "review-" + uuid.uuid4().hex
    core._store._conn.execute(
        "INSERT INTO reviews (id, task_id, verdict, detail, created_at, "
        "source_class, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (rid, task_id, verdict, "integration test review", core._store.now_iso(),
         source_class, rid),
    )


def add_finding(core, task_id, severity, status="open"):
    fid = "finding-" + uuid.uuid4().hex
    core._store._conn.execute(
        "INSERT INTO findings (id, task_id, severity, description, status, "
        "created_at, source_class, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (fid, task_id, severity, "integration test finding", status,
         core._store.now_iso(), "controller", fid),
    )
