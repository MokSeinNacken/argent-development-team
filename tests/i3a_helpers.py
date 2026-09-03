"""Shared helpers for Phase I3-A external-action-broker tests.

Deterministic: real git in tmp fixture repos (argv only), real Store, no
network, no LLM, no real provider writes (FakeGitHubAdapter only).  Builds the
I2 provenance foundation (terminal-DONE source job + INTEGRATED candidate) that
the broker requires for controller-authoritative request creation.

The terminal job + INTEGRATED candidate are manufactured through the
AUTHORITATIVE store paths (``create_integration_candidate`` +
``transition_integration_candidate``) — NOT direct SQL — so the HIGH-2
provenance binding (candidate.source_job_id == provenance.source_job_id) is
tested against the real store object model.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

from argent_core import Core, OWNER_SOURCE
from argent_core.external_action_broker import (
    AllowlistEntry,
    ExternalActionAllowlist,
    ExternalActionBroker,
    StandingPolicy,
    compute_provenance_mac,
)
from argent_core.external_provider_adapter import FakeGitHubAdapter
from argent_core.supervisor import Supervisor
from mock_supervisor_runtime import FakeRunLauncher, FakeRunStatusProvider

#: Deterministic evidence MAC key for I3-A tests (>= 16 bytes, reuses the
#: Phase-F ``_resolve_mac_key`` contract).  The broker fails closed without it.
TEST_MAC_KEY = b"i3a-test-evidence-mac-key-0123456789abcdef"


def run_git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args],
                          capture_output=True, text=True)


def commit_all(repo, msg):
    run_git(repo, "add", "-A")
    run_git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-m", msg)


def git_sha(repo, ref="HEAD"):
    return run_git(repo, "rev-parse", ref).stdout.strip()


def init_repo(root):
    """Create a git repo with branch ``main`` and a base commit."""
    repo = str(Path(root) / "repo")
    Path(repo).mkdir(parents=True, exist_ok=True)
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "t@t")
    run_git(repo, "config", "user.name", "t")
    Path(repo, "app.py").write_text("def add(a, b):\n    return a + b\n")
    Path(repo, "tests").mkdir(exist_ok=True)
    Path(repo, "tests", "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    commit_all(repo, "base")
    return repo


def make_env(db_path):
    core = Core(db_path)
    project = core.create_project("p", OWNER_SOURCE)
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher())
    return core, project, sup


def make_integrated_source(core, project, sup, repo, *, branch="main"):
    """Create a terminal-DONE source job + INTEGRATED candidate for ``repo``.

    Returns ``(job_id, candidate_id, head_sha, task_id)``.
    """
    task = core.create_task(project.id, f"t-{uuid.uuid4().hex[:6]}", OWNER_SOURCE)
    core.start_task_run(task.id, OWNER_SOURCE)
    job = sup.store.create_job(
        task.id, idempotency_key=f"job-{uuid.uuid4().hex[:8]}")
    jid = job.supervisor_job_id
    claimed = sup.store.claim_job(jid, owner_instance_id="OWNER", ttl_seconds=3600)
    epoch = claimed["lease_epoch"]
    head = git_sha(repo)
    if run_git(repo, "rev-parse", "--verify", f"refs/heads/{branch}").returncode != 0:
        run_git(repo, "branch", branch, head)
    sup.store.set_job_metadata(
        jid, owner_instance_id="OWNER", lease_epoch=epoch,
        repo_identity=repo, canonical_worktree_path=repo,
        branch_identity=branch, mutation_path_roots=[f"src_{task.id}.py"],
    )
    core._store._update_supervisor_job(
        jid, base_commit=head, expected_head=head, current_head=head,
        status="TERMINAL", terminal="DONE", next_action="NONE",
    )
    # Authoritative candidate creation + transition to INTEGRATED (HIGH-2
    # provenance binding: candidate.source_job_id is bound to ``jid`` and
    # ``integrated_head`` is set through the store, never raw SQL).
    cid_row = core._store.create_integration_candidate(
        repository=repo, integration_target=branch, source_job_id=jid,
        base_commit=head, source_head=head, source_branch=branch,
    )
    cid = cid_row["id"]
    core._store.transition_integration_candidate(
        cid, from_state="PENDING", to_state="INTEGRATED",
        expected_revision=0, integrated_head=head,
    )
    return jid, cid, head, task.id


def make_provenance(job_id, candidate_id, repo, head, *, branch="main",
                    scope="push", mac_key=None):
    """Build a keyed (MAC) provenance dict (I3-A HIGH-2 contract)."""
    key = mac_key if mac_key is not None else TEST_MAC_KEY
    prov = dict(
        version=1, source_job_id=job_id, source_candidate_id=candidate_id,
        repository=repo, source_head=head, integrated_head=head,
        branch=branch, scope=scope,
    )
    prov["provenance_hash"] = compute_provenance_mac(prov, key)
    return prov


def default_allowlist(repo, *, actions=None, pr_targets=("main",),
                      branch_namespaces=("argent/",)):
    actions = actions if actions is not None else frozenset({
        "read_repository", "read_ref", "read_pull_request", "read_checks",
        "push_feature_branch", "create_pull_request", "update_pull_request",
    })
    return ExternalActionAllowlist(entries=(AllowlistEntry(
        provider="github", account="MokSeinNacken",
        repositories=frozenset({repo}),
        permitted_actions=frozenset(actions),
        branch_namespaces=frozenset(branch_namespaces),
        pr_targets=frozenset(pr_targets),
    ),))


def default_standing_policy():
    """Standing policy that grants the I3-A autonomous BOUNDED_WRITE actions.

    (HIGH-3: an autonomous write requires the standing policy to grant the
    action — empty default ⇒ no autonomous writes.)
    """
    return StandingPolicy(autonomous_actions=frozenset({
        "push_feature_branch", "create_pull_request", "update_pull_request",
    }))


def make_broker(store, *, repo, adapter=None, allowlist=None,
                standing_policy=None, mac_key=None):
    adapter = adapter if adapter is not None else FakeGitHubAdapter(
        provider_name="github")
    allowlist = allowlist if allowlist is not None else default_allowlist(repo)
    standing_policy = (standing_policy if standing_policy is not None
                       else default_standing_policy())
    return ExternalActionBroker(
        store, adapter=adapter, allowlist=allowlist,
        standing_policy=standing_policy,
        mac_key=mac_key if mac_key is not None else TEST_MAC_KEY,
    )


def make_holder(core, project, sup):
    """Create + claim a holder (external-action authority) job."""
    task = core.create_task(project.id, f"holder-{uuid.uuid4().hex[:6]}",
                            OWNER_SOURCE)
    core.start_task_run(task.id, OWNER_SOURCE)
    job = sup.store.create_job(task.id, idempotency_key=f"holder-{uuid.uuid4().hex[:8]}")
    hid = job.supervisor_job_id
    claimed = sup.store.claim_job(hid, owner_instance_id="OWNER", ttl_seconds=3600)
    return hid, claimed["lease_epoch"]
