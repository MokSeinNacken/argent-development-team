"""Phase I2 — restart/crash-safety recovery (CASE 26/27/28/29/30/31).

Conservative: recovery never infers INTEGRATED from process disappearance; it
resets in-flight (INTEGRATING) candidates to PENDING and re-drives integration
idempotently (the merge is deterministic and the integration worktree is
recreated deterministically).
"""

from __future__ import annotations

import pytest

from argent_core import Core
from argent_core.integration_candidate import CandidateState
from argent_core.merge_queue import MergeQueue
from i2_helpers import (
    git_sha,
    init_repo,
    make_env,
    make_holder,
    make_mq,
    make_source,
    new_branch,
    write_commit,
)


def _ready_candidate(tmp_path):
    """Return (core, repo, base, candidate_id, holder) with one READY candidate."""
    core, project, sup = make_env(str(tmp_path / "t.db"))
    repo = init_repo(str(tmp_path / "git"))
    base = git_sha(repo)
    new_branch(repo, "feature-a")
    head_a = write_commit(repo, "feature_a.py", "a = 1\n", "a")
    from i2_helpers import run_git
    run_git(repo, "checkout", "-q", "main")
    jid, _ = make_source(core, project, sup, "a", repo=repo, branch="feature-a",
                         head=head_a, base=base)
    mq = make_mq(core, str(tmp_path / "wts"))
    c = mq.enqueue_candidate(jid, "main")
    c = mq.evaluate_candidate(c["id"])
    hid, hepoch = make_holder(core, project, sup)
    return core, repo, base, c, (hid, hepoch), mq


# ---------------------------------------------------------------------------
# CASE 26 — queue survives restart before integration begins
# ---------------------------------------------------------------------------

def test_case26_queue_survives_restart_before_integration(tmp_path):
    core, repo, base, c, holder, mq = _ready_candidate(tmp_path)
    assert c["state"] == CandidateState.READY.value
    core.close()

    # Reopen (simulated supervisor restart) — the candidate is persisted.
    core2 = Core(str(tmp_path / "t.db"))
    mq2 = make_mq(core2, str(tmp_path / "wts"))
    row = core2._store.get_integration_candidate(c["id"])
    assert row is not None
    assert row["state"] == CandidateState.READY.value
    core2.close()


def test_case26_restart_and_process(tmp_path):
    core, repo, base, c, (hid, hepoch), mq = _ready_candidate(tmp_path)
    core.close()
    core2 = Core(str(tmp_path / "t.db"))
    mq2 = make_mq(core2, str(tmp_path / "wts"))
    from argent_core.supervisor import Supervisor
    from mock_supervisor_runtime import FakeRunLauncher, FakeRunStatusProvider
    sup2 = Supervisor(core2, FakeRunStatusProvider(), FakeRunLauncher())
    # The old holder job/lease survive in the store; use them directly.
    out = mq2.process_target(repo, "main", holder_job_id=hid, holder_lease_epoch=hepoch)
    assert out.integrated == [c["id"]]
    assert core2._store.get_integration_candidate(c["id"])["state"] == CandidateState.INTEGRATED.value
    core2.close()


# ---------------------------------------------------------------------------
# CASE 27/28/29/30 — crash windows reset conservatively, never infer INTEGRATED
# ---------------------------------------------------------------------------

def _force_integrating(core, cid, **fields):
    row = core._store.get_integration_candidate(cid)
    core._store.transition_integration_candidate(
        cid, from_state=row["state"], to_state=CandidateState.INTEGRATING.value,
        expected_revision=row["revision"], **fields)


@pytest.mark.parametrize("window_fields", [
    {},                                                       # before prep
    {"integration_worktree_path": "/x/wt", "integration_branch": "integration/main"},  # after checkout
    {"integration_worktree_path": "/x/wt", "integrated_head": "a" * 40},  # mid-merge
    {"integration_worktree_path": "/x/wt", "integrated_head": "a" * 40,
     "result_json": '{"verdict": "DONE"}'},                   # after tests
])
def test_crash_window_resets_conservatively(tmp_path, window_fields):
    core, repo, base, c, (hid, hepoch), mq = _ready_candidate(tmp_path)
    _force_integrating(core, c["id"], **window_fields)
    # Reconcile: the in-flight candidate is reset to PENDING, never INTEGRATED.
    result = mq.reconcile_target(repo, "main")
    assert c["id"] in result.reset_to_pending
    row = core._store.get_integration_candidate(c["id"])
    assert row["state"] == CandidateState.PENDING.value
    assert row["last_error_code"] == "recovered_after_restart"
    assert row["state"] != CandidateState.INTEGRATED.value
    core.close()


# ---------------------------------------------------------------------------
# CASE 31 — idempotent duplicate processing
# ---------------------------------------------------------------------------

def test_case31_idempotent_duplicate_processing(tmp_path):
    core, repo, base, c, (hid, hepoch), mq = _ready_candidate(tmp_path)
    out1 = mq.process_target(repo, "main", holder_job_id=hid, holder_lease_epoch=hepoch)
    assert out1.integrated == [c["id"]]
    head1 = core._store.get_integration_candidate(c["id"])["integrated_head"]
    # A second pass must not re-integrate (already INTEGRATED) or change the head.
    out2 = mq.process_target(repo, "main", holder_job_id=hid, holder_lease_epoch=hepoch)
    assert out2.integrated == []
    head2 = core._store.get_integration_candidate(c["id"])["integrated_head"]
    assert head1 == head2
    core.close()


def test_reconcile_clears_in_flight_then_reintegrate(tmp_path):
    core, repo, base, c, (hid, hepoch), mq = _ready_candidate(tmp_path)
    _force_integrating(core, c["id"], integration_worktree_path="/x/wt")
    mq.reconcile_target(repo, "main")
    # After recovery, re-evaluate + re-integrate succeeds idempotently.
    mq.evaluate_candidate(c["id"])
    out = mq.process_target(repo, "main", holder_job_id=hid, holder_lease_epoch=hepoch)
    assert out.integrated == [c["id"]]
    core.close()


# ---------------------------------------------------------------------------
# I2 fix-round regressions (HIGH-2 — per-candidate reconcile with real evidence)
# ---------------------------------------------------------------------------

def _fclock_env(tmp_path):
    from argent_core import Core, OWNER_SOURCE
    from argent_core.supervisor import Supervisor
    from mock_supervisor_runtime import (
        FakeClock, FakeRunLauncher, FakeRunStatusProvider,
    )
    clock = FakeClock()
    core = Core(str(tmp_path / "t.db"), clock=clock)
    project = core.create_project("p", OWNER_SOURCE)
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock)
    return clock, core, project, sup


def test_reconcile_preserves_live_holder(tmp_path):
    # HIGH-2: an INTEGRATING candidate whose recorded holder still holds a LIVE
    # lease AND the action lock is preserved (not reset).
    from i2_helpers import TEST_MAC_KEY, pass_plan_builder, pass_test_runner

    clock, core, project, sup = _fclock_env(tmp_path)
    repo = init_repo(str(tmp_path / "git"))
    base = git_sha(repo)
    new_branch(repo, "feature-a")
    head_a = write_commit(repo, "feature_a.py", "a = 1\n", "a")
    from i2_helpers import run_git
    run_git(repo, "checkout", "-q", "main")
    jid, _ = make_source(core, project, sup, "a", repo=repo, branch="feature-a",
                         head=head_a, base=base)
    mq = MergeQueue(core._store, worktrees_root=str(tmp_path / "wts"),
                    mac_key=TEST_MAC_KEY)
    mq._plan_builder = pass_plan_builder
    mq._test_runner = pass_test_runner
    c = mq.enqueue_candidate(jid, "main")
    mq.evaluate_candidate(c["id"])

    task_h = core.create_task(project.id, "h", "owner:authenticated")
    core.start_task_run(task_h.id, "owner:authenticated")
    jh = sup.store.create_job(task_h.id, idempotency_key="h").supervisor_job_id
    eh = sup.store.claim_job(jh, owner_instance_id="OWNER", ttl_seconds=3600)["lease_epoch"]
    lock = mq.integration_lock_name(repo, "main")
    assert mq.store.try_acquire_action_lock(lock, job_id=jh, lease_epoch=eh) is True

    row = core._store.get_integration_candidate(c["id"])
    core._store.transition_integration_candidate(
        c["id"], from_state=row["state"], to_state=CandidateState.INTEGRATING.value,
        expected_revision=row["revision"], holder_owner_instance_id=jh,
        holder_lease_epoch=eh, integration_worktree_path=str(tmp_path / "wts" / "x"),
        integration_branch="integration/main", integrated_head=head_a)

    result = mq.reconcile_target(repo, "main")
    assert c["id"] not in result.reset_to_pending
    assert result.reclaimed_lock is False
    assert core._store.get_integration_candidate(c["id"])["state"] == CandidateState.INTEGRATING.value
    core.close()


def test_reconcile_resets_stale_holder_and_reclaims_lock(tmp_path):
    # HIGH-2: a stale holder (expired lease) is reset with the holder fields
    # EXPLICITLY cleared, and the stale lock is reclaimed truthfully.
    from i2_helpers import TEST_MAC_KEY, pass_plan_builder, pass_test_runner

    clock, core, project, sup = _fclock_env(tmp_path)
    repo = init_repo(str(tmp_path / "git"))
    base = git_sha(repo)
    new_branch(repo, "feature-a")
    head_a = write_commit(repo, "feature_a.py", "a = 1\n", "a")
    from i2_helpers import run_git
    run_git(repo, "checkout", "-q", "main")
    jid, _ = make_source(core, project, sup, "a", repo=repo, branch="feature-a",
                         head=head_a, base=base)
    mq = MergeQueue(core._store, worktrees_root=str(tmp_path / "wts"),
                    mac_key=TEST_MAC_KEY)
    mq._plan_builder = pass_plan_builder
    mq._test_runner = pass_test_runner
    c = mq.enqueue_candidate(jid, "main")
    mq.evaluate_candidate(c["id"])

    task_h = core.create_task(project.id, "h", "owner:authenticated")
    core.start_task_run(task_h.id, "owner:authenticated")
    jh = sup.store.create_job(task_h.id, idempotency_key="h").supervisor_job_id
    eh = sup.store.claim_job(jh, owner_instance_id="OWNER", ttl_seconds=5)["lease_epoch"]
    lock = mq.integration_lock_name(repo, "main")
    assert mq.store.try_acquire_action_lock(lock, job_id=jh, lease_epoch=eh) is True

    row = core._store.get_integration_candidate(c["id"])
    core._store.transition_integration_candidate(
        c["id"], from_state=row["state"], to_state=CandidateState.INTEGRATING.value,
        expected_revision=row["revision"], holder_owner_instance_id=jh,
        holder_lease_epoch=eh, integration_worktree_path=str(tmp_path / "wts" / "x"),
        integration_branch="integration/main", integrated_head=head_a)

    clock.advance(60)  # expire the holder's lease
    result = mq.reconcile_target(repo, "main")
    assert c["id"] in result.reset_to_pending
    assert result.reclaimed_lock is True
    row = core._store.get_integration_candidate(c["id"])
    assert row["state"] == CandidateState.PENDING.value
    # Holder fields are explicitly cleared (I2 HIGH-2), not merely left stale.
    assert row["holder_owner_instance_id"] is None
    assert row["holder_lease_epoch"] == 0
    core.close()
