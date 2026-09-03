"""Shared helpers for Phase I3-B GitHub live-acceptance tests.

Deterministic: real ``gh``/``git`` replaced by scripted fake executables in a
tmp dir (argv only, no network, no real GitHub writes), real Store, real
broker/fencing.  Reuses the I3-A provenance foundation (``i3a_helpers``) for
controller-authoritative request creation, and drives the REAL
``GitHubProviderAdapter`` through the broker for argv/classification/idempotency
tests.

The fake executable reads a scenario JSON (env ``FAKE_SCENARIO``) keyed by a
substring of the full argv (or the first argv token) and writes its scripted
stdout/stderr/exit-code; it appends every invocation argv to ``FAKE_LOG`` (env).
Credentials are NEVER placed in argv — the fake asserts this on every call by
refusing to run if any argv token looks like a GitHub token.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

_FAKE_EXECUTABLE = r'''#!/usr/bin/env python3
import json, os, re, sys

argv = sys.argv[1:]
full = " ".join(argv)

# Fail closed if a credential ever reaches argv (CASE 3/21/22).
_tok = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b|github_pat_[A-Za-z0-9_]{20,}")
for a in argv:
    if _tok.search(a):
        sys.stderr.write("FAKE_REFUSED_CREDENTIAL_IN_ARGV")
        sys.exit(97)

log = os.environ.get("FAKE_LOG")
if log:
    try:
        with open(log, "a") as f:
            f.write(json.dumps(argv) + "\n")
    except OSError:
        pass

scenario = {}
spath = os.environ.get("FAKE_SCENARIO")
if spath and os.path.exists(spath):
    try:
        scenario = json.load(open(spath))
    except (ValueError, OSError):
        scenario = {}

resp = None
for key in scenario:
    if key == argv[0] or key in full:
        resp = scenario[key]
        break
if resp is None:
    resp = scenario.get("*")
if resp is None:
    resp = {"code": 0, "stdout": "", "stderr": ""}
sys.stdout.write(resp.get("stdout", "") or "")
sys.stderr.write(resp.get("stderr", "") or "")
sys.exit(resp.get("code", 0))
'''


def write_fake_executable(tmp_path, name: str) -> str:
    """Write the scripted fake ``gh``/``git`` executable and return its path."""
    exe = Path(tmp_path) / name
    exe.write_text(_FAKE_EXECUTABLE)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(exe)


def write_scenario(tmp_path, scenario: dict) -> str:
    """Write a scenario JSON file (returns its path)."""
    p = Path(tmp_path) / "scenario.json"
    p.write_text(json.dumps(scenario))
    return str(p)


def read_log(tmp_path) -> list:
    """Read and parse the fake executable's argv log (list of argv lists)."""
    p = Path(tmp_path) / "log.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def env_for(tmp_path, scenario: dict = None, *, extra=None) -> dict:
    """Build the subprocess env for the fake executable.

    Always writes ``scenario.json`` and points ``FAKE_SCENARIO`` at it, so a
    test can :func:`write_scenario` later to update the scripted responses
    (the fake re-reads the file on every invocation).
    """
    spath = write_scenario(tmp_path, scenario if scenario is not None else {})
    env = {
        "FAKE_LOG": str(Path(tmp_path) / "log.jsonl"),
        "FAKE_SCENARIO": spath,
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    if extra:
        env.update(extra)
    return env


# ---------------------------------------------------------------------------
# GitHub-identity integrated source (I3-B: the acceptance repository identity
# is the GitHub ``owner/repo``, distinct from the LOCAL worktree path).
# ---------------------------------------------------------------------------

from argent_core import Core, OWNER_SOURCE  # noqa: E402
from argent_core.github_provider_adapter import GITHUB_ACCEPTANCE_REPOSITORY  # noqa: E402
from argent_core.supervisor import Supervisor  # noqa: E402
from i3a_helpers import (  # noqa: E402
    commit_all,
    git_sha,
    init_repo,
    make_env,
    make_provenance,
    run_git,
)
from mock_supervisor_runtime import FakeRunLauncher, FakeRunStatusProvider  # noqa: E402


def make_gh_integrated_source(core, project, sup, repo, *,
                              repository=GITHUB_ACCEPTANCE_REPOSITORY,
                              branch="main"):
    """Create a terminal-DONE job + INTEGRATED candidate bound to the GitHub
    ``repository`` identity (local ``repo`` path is the worktree only).

    Returns ``(job_id, candidate_id, head_sha, task_id)``.
    """
    import uuid

    task = core.create_task(project.id, f"t-{uuid.uuid4().hex[:6]}", OWNER_SOURCE)
    core.start_task_run(task.id, OWNER_SOURCE)
    job = sup.store.create_job(task.id, idempotency_key=f"job-{uuid.uuid4().hex[:8]}")
    jid = job.supervisor_job_id
    claimed = sup.store.claim_job(jid, owner_instance_id="OWNER", ttl_seconds=3600)
    epoch = claimed["lease_epoch"]
    head = git_sha(repo)
    if run_git(repo, "rev-parse", "--verify", f"refs/heads/{branch}").returncode != 0:
        run_git(repo, "branch", branch, head)
    sup.store.set_job_metadata(
        jid, owner_instance_id="OWNER", lease_epoch=epoch,
        repo_identity=repository, canonical_worktree_path=repo,
        branch_identity=branch, mutation_path_roots=[f"src_{task.id}.py"],
    )
    core._store._update_supervisor_job(
        jid, base_commit=head, expected_head=head, current_head=head,
        status="TERMINAL", terminal="DONE", next_action="NONE",
    )
    cid_row = core._store.create_integration_candidate(
        repository=repository, integration_target=branch, source_job_id=jid,
        base_commit=head, source_head=head, source_branch=branch,
    )
    cid = cid_row["id"]
    core._store.transition_integration_candidate(
        cid, from_state="PENDING", to_state="INTEGRATED",
        expected_revision=0, integrated_head=head,
    )
    return jid, cid, head, task.id


def make_gh_provenance(job_id, candidate_id, head, *,
                       repository=GITHUB_ACCEPTANCE_REPOSITORY,
                       branch="main", scope="push", mac_key=None):
    """Keyed provenance bound to the GitHub ``repository`` identity."""
    return make_provenance(job_id, candidate_id, repository, head,
                           branch=branch, scope=scope, mac_key=mac_key)
