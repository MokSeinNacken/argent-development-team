#!/usr/bin/env python3
"""Phase I2 — LOCAL controlled integration demo (docs/i2_demo.py).

Disposable fixture repositories/worktrees in a temp dir, NO network, NO push,
NO user projects, NO stress.  Demonstrates the merge-queue core end-to-end:

    Base -> Candidate A + Candidate B -> merge queue -> dedicated integration
    worktree -> deterministic FIFO order -> clean integrated HEAD -> fresh
    TestPlan (Phase F test_planning) -> real pytest on the integrated snapshot
    -> PASS.

    Plus a conflict fixture (Candidate C + Candidate D touching the same file)
    -> authoritative git conflict -> queue marks CONFLICTED -> no partial
    authoritative INTEGRATED result.

Everything is real git (argv only, no shell) + the real store + the real
``build_test_plan``/``execute_plan`` path.  The only substitution is the
inventory (a minimal fixture inventory so the fixture's own test file is the
plan's selector); the policy is the real default policy.

Prints redacted evidence; exits 0 on success.  Gracefully notes when git is
unavailable (it should not be).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
# The demo reuses the deterministic run-status/launcher fakes from the test
# helpers (no live runtime, no systemd).
_TESTS_DIR = PROJECT_ROOT / "tests"
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))


def _git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args],
                          capture_output=True, text=True)


def _commit(cwd, msg):
    _git(cwd, "add", "-A")
    _git(cwd, "-c", "user.email=demo@argent", "-c", "user.name=argent",
          "commit", "-m", msg)


def _sha(cwd, ref="HEAD"):
    return _git(cwd, "rev-parse", ref).stdout.strip()


def _git_available() -> bool:
    try:
        return subprocess.run(["git", "--version"], capture_output=True).returncode == 0
    except OSError:
        return False


def _build_fixture_repo(root: str, branch="main", test_body=None):
    repo = os.path.join(root, "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q", "-b", branch)
    _git(repo, "config", "user.email", "demo@argent")
    _git(repo, "config", "user.name", "argent")
    Path(repo, "app.py").write_text("def add(a, b):\n    return a + b\n")
    os.makedirs(os.path.join(repo, "tests"))
    if test_body is None:
        test_body = "import app\n\n\ndef test_add():\n    assert app.add(1, 2) == 3\n"
    Path(repo, "tests", "test_app.py").write_text(test_body)
    _commit(repo, "base")
    return repo, _sha(repo)


def _make_test_runner(mac_key):
    from argent_core.test_execution import (
        PytestRunner, ResultClass, compute_snapshot_identity,
    )
    from argent_core.merge_queue import make_integration_evidence

    def test_runner(candidate, worktree_path, plan, changed):
        # Real pytest on the integrated snapshot's own test file (argv only,
        # no shell).  The fresh TestPlan (Phase F) was built from the git
        # changed paths; we print its hash/selectors as evidence and run the
        # fixture's real test.  Evidence is signed with the demo MAC key so it
        # carries authenticated provenance (I2 HIGH-7).
        runner = PytestRunner(project_root=worktree_path)
        outcome = runner.run("tests/test_app.py")
        verdict = "DONE" if outcome.classification == ResultClass.TEST_PASS else "FAILED"
        snap = compute_snapshot_identity(worktree_path)
        print(f"    plan_hash={plan.plan_hash[:12]} "
              f"selectors={list(plan.all_selectors())[:5]} changed={list(changed)}")
        return make_integration_evidence(
            verdict, plan.plan_hash, snap.source_hash, snap.test_definition_hash,
            summary=outcome.summary, test_count=outcome.test_count, mac_key=mac_key,
        )
    return test_runner


def _run_demo():
    from argent_core import Core, OWNER_SOURCE
    from argent_core.supervisor import Supervisor
    from mock_supervisor_runtime import FakeRunLauncher, FakeRunStatusProvider
    from argent_core.merge_queue import MergeQueue
    from argent_core.integration_candidate import CandidateState

    tmp = tempfile.mkdtemp(prefix="argent-i2-demo-")
    worktrees_root = os.path.join(tmp, "worktrees")
    os.makedirs(worktrees_root)
    try:
        repo, base = _build_fixture_repo(os.path.join(tmp, "happy"))

        # Candidate A (disjoint file).
        _git(repo, "checkout", "-q", "-b", "feature-a")
        Path(repo, "feature_a.py").write_text("def fa():\n    return 1\n")
        _commit(repo, "feature a")
        head_a = _sha(repo)
        _git(repo, "checkout", "-q", "main")

        # Candidate B (disjoint file).
        _git(repo, "checkout", "-q", "-b", "feature-b")
        Path(repo, "feature_b.py").write_text("def fb():\n    return 2\n")
        _commit(repo, "feature b")
        head_b = _sha(repo)
        _git(repo, "checkout", "-q", "main")

        db = os.path.join(tmp, "state.db")
        core = Core(db)
        project = core.create_project("demo", OWNER_SOURCE)
        sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher())

        def make_source(repo, title, head, base):
            task = core.create_task(project.id, title, OWNER_SOURCE)
            core.start_task_run(task.id, OWNER_SOURCE)
            job = sup.store.create_job(task.id, idempotency_key=f"job-{title}")
            jid = job.supervisor_job_id
            claimed = sup.store.claim_job(jid, owner_instance_id="OWNER",
                                         ttl_seconds=3600)
            sup.store.set_job_metadata(
                jid, owner_instance_id="OWNER", lease_epoch=claimed["lease_epoch"],
                repo_identity=repo, canonical_worktree_path=repo,
                branch_identity=f"feature-{title}",
                mutation_path_roots=[f"feature_{title}.py"])
            core._store._update_supervisor_job(
                jid, base_commit=base, expected_head=head, current_head=head,
                status="TERMINAL", terminal="DONE", next_action="NONE")
            return jid

        ja = make_source(repo, "a", head_a, base)
        jb = make_source(repo, "b", head_b, base)

        mq = MergeQueue(core._store, worktrees_root=worktrees_root,
                        mac_key=b"d" * 32, test_runner=_make_test_runner(b"d" * 32))
        ca = mq.enqueue_candidate(ja, "main")
        cb = mq.enqueue_candidate(jb, "main")
        mq.evaluate_candidate(ca["id"])
        mq.evaluate_candidate(cb["id"])

        htask = core.create_task(project.id, "holder", OWNER_SOURCE)
        core.start_task_run(htask.id, OWNER_SOURCE)
        hjob = sup.store.create_job(htask.id, idempotency_key="holder")
        hid = hjob.supervisor_job_id
        hepoch = sup.store.claim_job(hid, owner_instance_id="OWNER",
                                     ttl_seconds=3600)["lease_epoch"]

        out = mq.process_target(repo, "main", holder_job_id=hid,
                                holder_lease_epoch=hepoch)
        print("== HAPPY PATH ==")
        print(f"  integrated:  {len(out.integrated)} candidates "
              f"({[c[-8:] for c in out.integrated]})")
        print(f"  conflicted:  {out.conflicted}")
        print(f"  failed:      {out.failed}")
        print(f"  stale:       {out.stale}")
        assert len(out.integrated) == 2 and not out.conflicted and not out.failed

        for cid in (ca["id"], cb["id"]):
            row = core._store.get_integration_candidate(cid)
            print(f"  {cid[-8:]} state={row['state']} "
                  f"cls={row['merge_classification']} "
                  f"head={row['integrated_head'][:12]}")

        tip = core._store.get_integration_candidate(cb["id"])["integrated_head"]
        wt = core._store.get_integration_candidate(cb["id"])["integration_worktree_path"]
        files = _git(wt, "ls-tree", "-r", "--name-only", tip).stdout.split()
        assert "feature_a.py" in files and "feature_b.py" in files
        assert _git(repo, "rev-parse", "main").stdout.strip() == base  # target untouched
        print(f"  combined snapshot files: {sorted(files)}")
        print("  target branch 'main' untouched: OK")

        # Conflict fixture: C and D both edit app.py on the same line, while
        # the test is a trivial smoke (does not depend on app.py) so C still
        # passes and D produces a genuine git conflict.
        repo2, base2 = _build_fixture_repo(
            os.path.join(tmp, "conflict"), test_body="def test_smoke():\n    assert True\n")
        _git(repo2, "checkout", "-q", "-b", "feature-c")
        Path(repo2, "app.py").write_text("def add(a, b):\n    return a + b + 1\n")
        _commit(repo2, "feature-c")
        head_c = _sha(repo2)
        _git(repo2, "checkout", "-q", "main")
        _git(repo2, "checkout", "-q", "-b", "feature-d")
        Path(repo2, "app.py").write_text("def add(a, b):\n    return a * b\n")
        _commit(repo2, "feature-d")
        head_d = _sha(repo2)
        _git(repo2, "checkout", "-q", "main")

        jc = make_source(repo2, "c", head_c, base2)
        jd = make_source(repo2, "d", head_d, base2)
        cc = mq.enqueue_candidate(jc, "main")
        cd = mq.enqueue_candidate(jd, "main")
        mq.evaluate_candidate(cc["id"])
        mq.evaluate_candidate(cd["id"])
        out2 = mq.process_target(repo2, "main", holder_job_id=hid,
                                 holder_lease_epoch=hepoch)
        print("== CONFLICT FIXTURE ==")
        print(f"  integrated: {[c[-8:] for c in out2.integrated]}")
        print(f"  conflicted: {[c[-8:] for c in out2.conflicted]}")
        drow = core._store.get_integration_candidate(cd["id"])
        print(f"  D state={drow['state']} cls={drow['merge_classification']} "
              f"integrated_head={drow['integrated_head']}")
        assert cc["id"] in out2.integrated and cd["id"] in out2.conflicted
        assert drow["state"] == CandidateState.CONFLICTED.value
        assert drow["integrated_head"] is None  # no partial authoritative result

        core.close()
        print("I2 DEMO OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    if not _git_available():
        print("git unavailable — demo skipped (should not happen).")
        return 2
    _run_demo()
    return 0


if __name__ == "__main__":
    sys.exit(main())
