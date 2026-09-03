"""Phase I2 — candidate evidence isolation + hard invariants (CASE 37/39).

Per-candidate context/routing/result evidence is stored in separate candidate
rows and written only through revision-fenced transitions, so one candidate can
never overwrite another (CASE 39).  The one-INTEGRATING-per-(repository,target)
partial unique index is a defensive second layer for the single-holder
boundary (CASE 37).
"""

from __future__ import annotations

import sqlite3

import pytest

from argent_core.integration_candidate import CandidateRevisionError, CandidateState
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


def _two_sources(tmp_path):
    core, project, sup = make_env(str(tmp_path / "t.db"))
    repo = init_repo(str(tmp_path / "git"))
    base = git_sha(repo)
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
    return core, project, sup, repo, base, ja, jb


def test_case39_candidate_evidence_isolated(tmp_path):
    core, project, sup, repo, base, ja, jb = _two_sources(tmp_path)
    mq = make_mq(core, str(tmp_path / "wts"))
    ca = mq.enqueue_candidate(ja, "main")
    cb = mq.enqueue_candidate(jb, "main")
    mq.evaluate_candidate(ca["id"])
    mq.evaluate_candidate(cb["id"])

    def runner(candidate, worktree_path, plan, changed):
        return make_test_evidence(
            "DONE", plan, worktree_path, summary=f"candidate {candidate['id'][-4:]}")

    mq._test_runner = runner
    hid, hepoch = make_holder(core, project, sup)
    out = mq.process_target(repo, "main", holder_job_id=hid, holder_lease_epoch=hepoch)
    assert len(out.integrated) == 2
    # Each candidate's evidence (result_json) is its own, never overwritten.
    import json
    ra = json.loads(core._store.get_integration_candidate(ca["id"])["result_json"])
    rb = json.loads(core._store.get_integration_candidate(cb["id"])["result_json"])
    # Per-candidate evidence is stored independently (distinct summaries) and
    # each carries its own authenticated evidence MAC.
    assert ra["summary"] != rb["summary"]
    assert ra["evidence_mac"] and rb["evidence_mac"]
    core.close()


def test_case39_revision_fence_prevents_stale_overwrite(tmp_path):
    core, project, sup, repo, base, ja, jb = _two_sources(tmp_path)
    mq = make_mq(core, str(tmp_path / "wts"))
    ca = mq.enqueue_candidate(ja, "main")
    mq.evaluate_candidate(ca["id"])
    row = core._store.get_integration_candidate(ca["id"])
    rev = row["revision"]
    # A stale caller (old revision) cannot commit a transition.
    with pytest.raises(CandidateRevisionError):
        core._store.transition_integration_candidate(
            ca["id"], from_state=row["state"],
            to_state=CandidateState.INTEGRATING.value,
            expected_revision=rev + 999)
    # A wrong from_state is also refused (fail closed).
    with pytest.raises(CandidateRevisionError):
        core._store.transition_integration_candidate(
            ca["id"], from_state=CandidateState.INTEGRATED.value,
            to_state=CandidateState.READY.value, expected_revision=rev)
    # The candidate is unchanged.
    after = core._store.get_integration_candidate(ca["id"])
    assert after["state"] == row["state"]
    assert after["revision"] == rev
    core.close()


def test_case37_one_integrating_per_target_unique_index(tmp_path):
    core, project, sup, repo, base, ja, jb = _two_sources(tmp_path)
    mq = make_mq(core, str(tmp_path / "wts"))
    ca = mq.enqueue_candidate(ja, "main")
    cb = mq.enqueue_candidate(jb, "main")
    mq.evaluate_candidate(ca["id"])
    mq.evaluate_candidate(cb["id"])
    # Force both into INTEGRATING directly — the partial unique index rejects
    # the second (defensive layer beneath the action lock).
    row_a = core._store.get_integration_candidate(ca["id"])
    row_b = core._store.get_integration_candidate(cb["id"])
    core._store.transition_integration_candidate(
        ca["id"], from_state=row_a["state"], to_state=CandidateState.INTEGRATING.value,
        expected_revision=row_a["revision"])
    with pytest.raises(sqlite3.IntegrityError):
        core._store.transition_integration_candidate(
            cb["id"], from_state=row_b["state"], to_state=CandidateState.INTEGRATING.value,
            expected_revision=row_b["revision"])
    core.close()
