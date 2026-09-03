"""Phase I2 — merge-queue authority, ordering, isolation (CASE 8/9/10/11/18/
19/20/24/25/36/37/38).

Deterministic: real git fixtures, real store, no network, no LLM.
"""

from __future__ import annotations

import pytest

from argent_core import job_state
from argent_core.integration_candidate import CandidateState
from argent_core.models import LeaseFencedError
from argent_core.merge_queue import MergeQueue
from i2_helpers import (
    git_sha,
    init_repo,
    make_env,
    make_holder,
    make_mq,
    make_source,
    make_test_evidence,
    new_branch,
    run_git,
    write_commit,
)


def _mk(tmp_path):
    core, project, sup = make_env(str(tmp_path / "t.db"))
    repo = init_repo(str(tmp_path / "git"))
    base = git_sha(repo)
    return core, project, sup, repo, base


def _job_count(core):
    return core._store._conn.execute(
        "SELECT COUNT(*) AS n FROM supervisor_jobs").fetchone()["n"]


# ---------------------------------------------------------------------------
# CASE 8/9/36/37 — single integration authority (action lock)
# ---------------------------------------------------------------------------

def test_case8_single_holder_per_target(tmp_path):
    core, project, sup, repo, base = _mk(tmp_path)
    h1, e1 = make_holder(core, project, sup)
    h2, e2 = make_holder(core, project, sup)
    mq = MergeQueue(core._store, worktrees_root=str(tmp_path / "wts"))
    lock = mq.integration_lock_name(repo, "main")
    assert mq.store.try_acquire_action_lock(lock, job_id=h1, lease_epoch=e1) is True
    # A different valid holder is refused (exactly ONE holder per target).
    assert mq.store.try_acquire_action_lock(lock, job_id=h2, lease_epoch=e2) is False
    # Releasing allows the other holder.
    assert mq.store.release_action_lock(lock, job_id=h1, lease_epoch=e1) is True
    assert mq.store.try_acquire_action_lock(lock, job_id=h2, lease_epoch=e2) is True
    core.close()


def test_case9_stale_holder_reclaimed(tmp_path):
    from mock_supervisor_runtime import FakeClock
    core = __import__("argent_core").Core
    core2 = None
    # Use a fake clock to expire a holder lease deterministically.
    from argent_core import Core
    clock = FakeClock()
    core = Core(str(tmp_path / "t.db"), clock=clock)
    project = core.create_project("p", "owner:authenticated")
    from argent_core.supervisor import Supervisor
    from mock_supervisor_runtime import FakeRunLauncher, FakeRunStatusProvider
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock)
    task1 = core.create_task(project.id, "h1", "owner:authenticated")
    core.start_task_run(task1.id, "owner:authenticated")
    j1 = sup.store.create_job(task1.id, idempotency_key="h1")
    h1 = j1.supervisor_job_id
    e1 = sup.store.claim_job(h1, owner_instance_id="OWNER", ttl_seconds=1)["lease_epoch"]
    mq = MergeQueue(core._store, worktrees_root=str(tmp_path / "wts"))
    lock = mq.integration_lock_name("repo", "main")
    assert mq.store.try_acquire_action_lock(lock, job_id=h1, lease_epoch=e1) is True
    clock.advance(60)  # expire h1's lease
    task2 = core.create_task(project.id, "h2", "owner:authenticated")
    core.start_task_run(task2.id, "owner:authenticated")
    j2 = sup.store.create_job(task2.id, idempotency_key="h2")
    h2 = j2.supervisor_job_id
    e2 = sup.store.claim_job(h2, owner_instance_id="OWNER", ttl_seconds=3600)["lease_epoch"]
    # h1's lock is stale -> h2 reclaims atomically (restart-safe, not PID-only).
    assert mq.store.try_acquire_action_lock(lock, job_id=h2, lease_epoch=e2) is True
    core.close()


def test_case36_stale_holder_cannot_drive_integration(tmp_path):
    core, project, sup, repo, base = _mk(tmp_path)
    hid, epoch = make_holder(core, project, sup)
    mq = make_mq(core, str(tmp_path / "wts"))
    lock = mq.integration_lock_name(repo, "main")
    # A stale epoch cannot acquire the integration lock.
    with pytest.raises(LeaseFencedError):
        mq.store.try_acquire_action_lock(lock, job_id=hid, lease_epoch=epoch + 999)
    core.close()


def test_case37_lock_boundary_is_per_repo_target(tmp_path):
    core, project, sup, repo, base = _mk(tmp_path)
    h1, e1 = make_holder(core, project, sup)
    mq = MergeQueue(core._store, worktrees_root=str(tmp_path / "wts"))
    lock_a = mq.integration_lock_name(repo, "main")
    lock_b = mq.integration_lock_name(repo, "release/v2")
    assert lock_a != lock_b
    assert mq.store.try_acquire_action_lock(lock_a, job_id=h1, lease_epoch=e1) is True
    # A different target in the same repo is an independent lock (different
    # targets may progress independently — conservative §25).
    assert mq.store.try_acquire_action_lock(lock_b, job_id=h1, lease_epoch=e1) is True
    core.close()


# ---------------------------------------------------------------------------
# CASE 10/11 — no second scheduler / no second source of truth
# ---------------------------------------------------------------------------

def test_case10_integration_is_not_a_second_scheduler(tmp_path):
    core, project, sup, repo, base = _mk(tmp_path)
    new_branch(repo, "feature-a")
    head_a = write_commit(repo, "feature_a.py", "a = 1\n", "a")
    run_git(repo, "checkout", "-q", "main")
    jid, _ = make_source(core, project, sup, "a", repo=repo, branch="feature-a",
                         head=head_a, base=base)
    mq = make_mq(core, str(tmp_path / "wts"))
    c = mq.enqueue_candidate(jid, "main")
    mq.evaluate_candidate(c["id"])
    hid, hepoch = make_holder(core, project, sup)
    before = _job_count(core)
    out = mq.process_target(repo, "main", holder_job_id=hid, holder_lease_epoch=hepoch)
    assert out.integrated == [c["id"]]
    # No new supervisor job was created by the queue (it never schedules).
    assert _job_count(core) == before
    core.close()


def test_case11_store_is_single_source_of_truth(tmp_path):
    core, project, sup, repo, base = _mk(tmp_path)
    new_branch(repo, "feature-a")
    head_a = write_commit(repo, "feature_a.py", "a = 1\n", "a")
    run_git(repo, "checkout", "-q", "main")
    jid, _ = make_source(core, project, sup, "a", repo=repo, branch="feature-a",
                         head=head_a, base=base)
    mq = make_mq(core, str(tmp_path / "wts"))
    c = mq.enqueue_candidate(jid, "main")
    mq.evaluate_candidate(c["id"])
    hid, hepoch = make_holder(core, project, sup)
    mq.process_target(repo, "main", holder_job_id=hid, holder_lease_epoch=hepoch)
    row = core._store.get_integration_candidate(c["id"])
    # The authoritative state lives in the store; git only supplies the head.
    assert row["state"] == CandidateState.INTEGRATED.value
    assert row["integrated_head"] is not None
    # The source job's primary state is untouched (still DONE).
    assert core._store.get_supervisor_job(jid)["terminal"] == "DONE"
    core.close()


# ---------------------------------------------------------------------------
# CASE 18/24 — sequential multi-candidate + combined snapshot
# ---------------------------------------------------------------------------

def test_case18_sequential_a_then_b(tmp_path):
    core, project, sup, repo, base = _mk(tmp_path)
    new_branch(repo, "feature-a")
    head_a = write_commit(repo, "feature_a.py", "def fa():\n    return 1\n", "a")
    run_git(repo, "checkout", "-q", "main")
    new_branch(repo, "feature-b")
    head_b = write_commit(repo, "feature_b.py", "def fb():\n    return 2\n", "b")
    run_git(repo, "checkout", "-q", "main")
    ja, _ = make_source(core, project, sup, "a", repo=repo, branch="feature-a",
                        head=head_a, base=base)
    jb, _ = make_source(core, project, sup, "b", repo=repo, branch="feature-b",
                        head=head_b, base=base)
    order = []
    mq = make_mq(core, str(tmp_path / "wts"))

    def runner(candidate, worktree_path, plan, changed):
        order.append(candidate["id"])
        return make_test_evidence("DONE", plan, worktree_path)

    mq._test_runner = runner
    ca = mq.enqueue_candidate(ja, "main")
    cb = mq.enqueue_candidate(jb, "main")
    mq.evaluate_candidate(ca["id"])
    mq.evaluate_candidate(cb["id"])
    hid, hepoch = make_holder(core, project, sup)
    out = mq.process_target(repo, "main", holder_job_id=hid, holder_lease_epoch=hepoch)
    # FIFO: A integrated before B.
    assert order == [ca["id"], cb["id"]]
    assert out.integrated == [ca["id"], cb["id"]]
    core.close()


def test_case24_combined_snapshot_failure_detected(tmp_path):
    # Two individually-green candidates are NOT assumed jointly green: B is
    # tested against the COMBINED (A+B) snapshot, and a genuine test failure in
    # that combined snapshot must be detected (I2 HIGH-6) — not merely that
    # both files exist.
    import subprocess
    import sys

    core, project, sup, repo, base = _mk(tmp_path)
    new_branch(repo, "feature-a")
    head_a = write_commit(repo, "feature_a.py", "MARKER = 'a'\n", "a")
    run_git(repo, "checkout", "-q", "main")
    new_branch(repo, "feature-b")
    write_commit(repo, "feature_b.py", "MARKER = 'b'\n", "b")
    # B adds a test asserting it is the ONLY feature file (green alone; red
    # once A's feature_a.py is also present in the combined snapshot).
    write_commit(
        repo, "tests/test_combined.py",
        "import pathlib\n\n\ndef test_single_feature():\n"
        "    features = sorted(p.name for p in pathlib.Path('.').glob('feature_*.py'))\n"
        "    assert features == ['feature_b.py']\n",
        "b-test",
    )
    head_b = git_sha(repo)
    run_git(repo, "checkout", "-q", "main")
    ja, _ = make_source(core, project, sup, "a", repo=repo, branch="feature-a",
                        head=head_a, base=base)
    jb, _ = make_source(core, project, sup, "b", repo=repo, branch="feature-b",
                        head=head_b, base=base)
    mq = make_mq(core, str(tmp_path / "wts"))

    def runner(candidate, worktree_path, plan, changed):
        # Run the REAL pytest suite on the combined integration snapshot.
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "."],
            cwd=worktree_path, capture_output=True, text=True,
        )
        verdict = "DONE" if proc.returncode == 0 else "FAILED"
        return make_test_evidence(
            verdict, plan, worktree_path,
            summary=(proc.stdout or proc.stderr)[-200:] or "pytest",
        )

    mq._test_runner = runner
    ca = mq.enqueue_candidate(ja, "main")
    cb = mq.enqueue_candidate(jb, "main")
    mq.evaluate_candidate(ca["id"])
    mq.evaluate_candidate(cb["id"])
    hid, hepoch = make_holder(core, project, sup)
    out = mq.process_target(repo, "main", holder_job_id=hid, holder_lease_epoch=hepoch)
    # A is green alone; B's combined snapshot (A+B) fails its combined test.
    assert ca["id"] in out.integrated
    assert cb["id"] in out.failed
    brow = core._store.get_integration_candidate(cb["id"])
    assert brow["state"] == CandidateState.FAILED.value
    # The failure is a genuine test failure (authenticated, not a bypass).
    assert brow["last_error_code"] == "tests_failed"
    # The combined snapshot truly contained BOTH files (A leaked into B's run).
    wt = brow["integration_worktree_path"]
    ls = run_git(wt, "ls-tree", "-r", "--name-only", "HEAD").stdout
    assert "feature_a.py" in ls and "feature_b.py" in ls
    core.close()


# ---------------------------------------------------------------------------
# CASE 19/25/20 — failed-candidate isolation, no global rollback
# ---------------------------------------------------------------------------

def test_case19_failed_candidate_isolated(tmp_path):
    core, project, sup, repo, base = _mk(tmp_path)
    new_branch(repo, "feature-a")
    head_a = write_commit(repo, "feature_a.py", "a = 1\n", "a")
    run_git(repo, "checkout", "-q", "main")
    new_branch(repo, "feature-b")
    head_b = write_commit(repo, "feature_b.py", "b = 2\n", "b")
    run_git(repo, "checkout", "-q", "main")
    ja, _ = make_source(core, project, sup, "a", repo=repo, branch="feature-a",
                        head=head_a, base=base)
    jb, _ = make_source(core, project, sup, "b", repo=repo, branch="feature-b",
                        head=head_b, base=base)
    mq = make_mq(core, str(tmp_path / "wts"))
    ca = mq.enqueue_candidate(ja, "main")
    cb = mq.enqueue_candidate(jb, "main")
    mq.evaluate_candidate(ca["id"])
    mq.evaluate_candidate(cb["id"])
    # B's tests fail.
    def runner(candidate, worktree_path, plan, changed):
        if candidate["id"] == cb["id"]:
            return make_test_evidence("FAILED", plan, worktree_path, summary="boom")
        return make_test_evidence("DONE", plan, worktree_path)
    mq._test_runner = runner
    hid, hepoch = make_holder(core, project, sup)
    out = mq.process_target(repo, "main", holder_job_id=hid, holder_lease_epoch=hepoch)
    assert ca["id"] in out.integrated
    assert cb["id"] in out.failed
    # CASE 20/25: A's already-integrated evidence is intact (never rolled back).
    arow = core._store.get_integration_candidate(ca["id"])
    assert arow["state"] == CandidateState.INTEGRATED.value
    assert arow["integrated_head"] is not None
    brow = core._store.get_integration_candidate(cb["id"])
    assert brow["state"] == CandidateState.FAILED.value
    core.close()


# ---------------------------------------------------------------------------
# CASE 38 — Resource Governor binding during integration tests
# ---------------------------------------------------------------------------

def test_case38_resource_governor_gate_used(tmp_path, monkeypatch):
    import argent_core.test_execution as te

    core, project, sup, repo, base = _mk(tmp_path)
    new_branch(repo, "feature-a")
    head_a = write_commit(repo, "feature_a.py", "a = 1\n", "a")
    run_git(repo, "checkout", "-q", "main")
    jid, _ = make_source(core, project, sup, "a", repo=repo, branch="feature-a",
                         head=head_a, base=base)

    captured = {}

    def fake_execute(plan, runner, *, snapshot, resource_gate=None, store=None,
                     project_root=None, mac_key=None):
        captured["gate"] = resource_gate
        class _V:
            value = "DONE"
        class _R:
            verdict = _V()
            stages = ()
        return _R()

    monkeypatch.setattr(te, "execute_plan", fake_execute)

    # A proper zero-arg Resource Governor gate is wired by the caller and MUST
    # be passed through to execute_plan (Resource Governor binding, CASE 38).
    from argent_core.resource_governor import (
        AdmissionDecision, AdmissionVerdict, ResourceReasonCode,
    )
    from argent_core.test_execution import ResourceGovernorGate
    allow = AdmissionDecision(
        resource_class="LIGHT", policy_version="1", snapshot_ref="s",
        decision=AdmissionVerdict.ALLOW.value,
        reason_code=ResourceReasonCode.OK.value, effective_limits={},
        timestamp="2026-01-01T00:00:00+00:00",
    )
    gate = ResourceGovernorGate(lambda: allow)

    mq = MergeQueue(core._store, worktrees_root=str(tmp_path / "wts"),
                    mac_key=b"k" * 32, resource_gate=gate)
    c = mq.enqueue_candidate(jid, "main")
    mq.evaluate_candidate(c["id"])
    hid, hepoch = make_holder(core, project, sup)
    out = mq.integrate_candidate(c["id"], holder_job_id=hid, holder_lease_epoch=hepoch)
    assert out.state == CandidateState.INTEGRATED.value
    assert captured["gate"] is gate
    core.close()


# ---------------------------------------------------------------------------
# I2 fix-round regressions (HIGH-1 / HIGH-5 / HIGH-7)
# ---------------------------------------------------------------------------

def test_high1_stale_holder_cannot_finalize_after_takeover(tmp_path):
    # HIGH-1: holder A's lease expires mid-integration, holder B reclaims the
    # action lock, and A must NOT reach INTEGRATED (every authoritative
    # transition re-verifies the live lease + lock).
    from argent_core import Core, OWNER_SOURCE
    from argent_core.supervisor import Supervisor
    from i2_helpers import TEST_MAC_KEY, pass_plan_builder
    from mock_supervisor_runtime import (
        FakeClock, FakeRunLauncher, FakeRunStatusProvider,
    )

    clock = FakeClock()
    core = Core(str(tmp_path / "t.db"), clock=clock)
    project = core.create_project("p", OWNER_SOURCE)
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock)
    repo = init_repo(str(tmp_path / "git"))
    base = git_sha(repo)
    new_branch(repo, "feature-a")
    head_a = write_commit(repo, "feature_a.py", "a = 1\n", "a")
    run_git(repo, "checkout", "-q", "main")
    jid, _ = make_source(core, project, sup, "a", repo=repo, branch="feature-a",
                         head=head_a, base=base)

    ta = core.create_task(project.id, "hA", OWNER_SOURCE)
    core.start_task_run(ta.id, OWNER_SOURCE)
    ja = sup.store.create_job(ta.id, idempotency_key="hA").supervisor_job_id
    ea = sup.store.claim_job(ja, owner_instance_id="OWNER", ttl_seconds=5)["lease_epoch"]
    tb = core.create_task(project.id, "hB", OWNER_SOURCE)
    core.start_task_run(tb.id, OWNER_SOURCE)
    jb = sup.store.create_job(tb.id, idempotency_key="hB").supervisor_job_id
    eb = sup.store.claim_job(jb, owner_instance_id="OWNER", ttl_seconds=3600)["lease_epoch"]

    mq = MergeQueue(core._store, worktrees_root=str(tmp_path / "wts"),
                    mac_key=TEST_MAC_KEY)
    mq._plan_builder = pass_plan_builder
    lock = mq.integration_lock_name(repo, "main")

    def runner(candidate, worktree_path, plan, changed):
        # Mid-integration: expire A's lease, then B reclaims the lock.
        clock.advance(60)
        assert mq.store.try_acquire_action_lock(lock, job_id=jb, lease_epoch=eb) is True
        return make_test_evidence("DONE", plan, worktree_path)

    mq._test_runner = runner
    c = mq.enqueue_candidate(jid, "main")
    mq.evaluate_candidate(c["id"])
    out = mq.integrate_candidate(c["id"], holder_job_id=ja, holder_lease_epoch=ea)
    # A lost its lease/lock mid-integration -> never INTEGRATED.
    assert out.state != CandidateState.INTEGRATED.value
    assert out.detail == "holder_lease_or_lock_lost"
    assert core._store.get_integration_candidate(c["id"])["state"] != CandidateState.INTEGRATED.value
    core.close()


def test_high5_dependency_requires_prerequisite_candidate_integrated(tmp_path):
    # HIGH-5: candidate B depending on source job A must NOT be promoted READY
    # while A's candidate is still PENDING; only after A's candidate is
    # INTEGRATED may B promote.
    core, project, sup, repo, base = _mk(tmp_path)
    new_branch(repo, "feature-a")
    head_a = write_commit(repo, "feature_a.py", "a = 1\n", "a")
    run_git(repo, "checkout", "-q", "main")
    new_branch(repo, "feature-b")
    head_b = write_commit(repo, "feature_b.py", "b = 2\n", "b")
    run_git(repo, "checkout", "-q", "main")
    ja, _ = make_source(core, project, sup, "a", repo=repo, branch="feature-a",
                        head=head_a, base=base)
    jb, _ = make_source(core, project, sup, "b", repo=repo, branch="feature-b",
                        head=head_b, base=base, depends_on=ja)
    mq = make_mq(core, str(tmp_path / "wts"))
    ca = mq.enqueue_candidate(ja, "main")
    cb = mq.enqueue_candidate(jb, "main")
    # The source-job dependency is translated to the candidate id.
    assert core._store.get_integration_candidate(cb["id"])["depends_on"] == ca["id"]
    mq.evaluate_candidate(ca["id"])
    cbe = mq.evaluate_candidate(cb["id"])
    assert cbe["state"] == CandidateState.PENDING.value
    assert cbe["last_error_code"] == "DEPENDENCY_NOT_INTEGRATED"
    # A's source job IS terminal-DONE, yet B must still wait (no DONE shortcut).
    assert core._store.get_supervisor_job(ja)["terminal"] == "DONE"
    hid, hepoch = make_holder(core, project, sup)
    out = mq.integrate_candidate(ca["id"], holder_job_id=hid, holder_lease_epoch=hepoch)
    assert out.state == CandidateState.INTEGRATED.value
    cbe = mq.evaluate_candidate(cb["id"])
    assert cbe["state"] == CandidateState.READY.value
    core.close()


def test_high5_cycle_members_transition_to_blocked(tmp_path):
    # HIGH-5: a dependency cycle among READY candidates must transition its
    # members to candidate BLOCKED with bounded evidence.
    core, project, sup, repo, base = _mk(tmp_path)
    new_branch(repo, "feature-a")
    head_a = write_commit(repo, "feature_a.py", "a = 1\n", "a")
    run_git(repo, "checkout", "-q", "main")
    new_branch(repo, "feature-b")
    head_b = write_commit(repo, "feature_b.py", "b = 2\n", "b")
    run_git(repo, "checkout", "-q", "main")
    ja, _ = make_source(core, project, sup, "a", repo=repo, branch="feature-a",
                        head=head_a, base=base)
    jb, _ = make_source(core, project, sup, "b", repo=repo, branch="feature-b",
                        head=head_b, base=base)
    mq = make_mq(core, str(tmp_path / "wts"))
    ca = mq.enqueue_candidate(ja, "main")
    cb = mq.enqueue_candidate(jb, "main")
    # Directly promote both to READY with a mutual dependency cycle.
    for cid, other in ((ca["id"], cb["id"]), (cb["id"], ca["id"])):
        row = core._store.get_integration_candidate(cid)
        core._store.transition_integration_candidate(
            cid, from_state=row["state"], to_state=CandidateState.READY.value,
            expected_revision=row["revision"], depends_on=other)
    hid, hepoch = make_holder(core, project, sup)
    out = mq.process_target(repo, "main", holder_job_id=hid, holder_lease_epoch=hepoch)
    assert ca["id"] in out.blocked and cb["id"] in out.blocked
    for cid in (ca["id"], cb["id"]):
        row = core._store.get_integration_candidate(cid)
        assert row["state"] == CandidateState.BLOCKED.value
        assert row["last_error_code"] == "dependency_cycle"
    core.close()


def test_high7_unauthenticated_evidence_rejected(tmp_path):
    # HIGH-7: a fake runner that fabricates DONE without a valid evidence MAC
    # (or with the wrong plan hash) is rejected fail-closed.
    core, project, sup, repo, base = _mk(tmp_path)
    new_branch(repo, "feature-a")
    head_a = write_commit(repo, "feature_a.py", "a = 1\n", "a")
    run_git(repo, "checkout", "-q", "main")
    jid, _ = make_source(core, project, sup, "a", repo=repo, branch="feature-a",
                         head=head_a, base=base)
    mq = make_mq(core, str(tmp_path / "wts"))
    c = mq.enqueue_candidate(jid, "main")
    mq.evaluate_candidate(c["id"])
    mq._test_runner = lambda candidate, wt, plan, changed: {
        "verdict": "DONE", "plan_hash": plan.plan_hash,
        "source_hash": "x", "test_definition_hash": "y",
    }
    hid, hepoch = make_holder(core, project, sup)
    out = mq.integrate_candidate(c["id"], holder_job_id=hid, holder_lease_epoch=hepoch)
    assert out.state == CandidateState.FAILED.value
    assert out.detail == "integration_evidence_unauthenticated"
    core.close()


def test_high7_wrong_plan_hash_rejected(tmp_path):
    # HIGH-7: evidence with a valid-looking MAC but the WRONG plan hash is
    # rejected (the plan hash must equal the freshly-built integration plan).
    from i2_helpers import TEST_MAC_KEY

    core, project, sup, repo, base = _mk(tmp_path)
    new_branch(repo, "feature-a")
    head_a = write_commit(repo, "feature_a.py", "a = 1\n", "a")
    run_git(repo, "checkout", "-q", "main")
    jid, _ = make_source(core, project, sup, "a", repo=repo, branch="feature-a",
                         head=head_a, base=base)
    mq = make_mq(core, str(tmp_path / "wts"))
    c = mq.enqueue_candidate(jid, "main")
    mq.evaluate_candidate(c["id"])

    def runner(candidate, worktree_path, plan, changed):
        ev = make_test_evidence("DONE", plan, worktree_path, mac_key=TEST_MAC_KEY)
        ev["plan_hash"] = "wrong-plan"
        return ev

    mq._test_runner = runner
    hid, hepoch = make_holder(core, project, sup)
    out = mq.integrate_candidate(c["id"], holder_job_id=hid, holder_lease_epoch=hepoch)
    assert out.state == CandidateState.FAILED.value
    assert out.detail == "integration_evidence_unauthenticated"
    core.close()
