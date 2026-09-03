"""Phase I2 — real-git integration behavior (CASE 12/13/14/15/16/17/21/22/23).

Uses real git in tmp_path fixture repos (argv only, no shell).  Verifies the
dedicated integration worktree, target-branch immutability, authoritative
conflict detection, and the fresh-snapshot TestPlan binding.
"""

from __future__ import annotations

import os

from argent_core.integration_candidate import CandidateState, MergeClassification
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


def _source(core, project, sup, repo, base, title, head, branch, paths=None):
    return make_source(core, project, sup, title, repo=repo, branch=branch,
                       head=head, base=base, mutation_paths=paths)


# ---------------------------------------------------------------------------
# CASE 12/13/14 — dedicated integration worktree, target never mutated
# ---------------------------------------------------------------------------

def test_case12_integration_uses_dedicated_worktree_not_writer(tmp_path):
    core, project, sup, repo, base = _mk(tmp_path)
    new_branch(repo, "feature-a")
    head_a = write_commit(repo, "feature_a.py", "def fa():\n    return 1\n", "a")
    run_git(repo, "checkout", "-q", "main")
    jid, _ = _source(core, project, sup, repo, base, "a", head_a, "feature-a")

    wt_root = str(tmp_path / "wts")
    mq = make_mq(core, wt_root)
    c = mq.enqueue_candidate(jid, "main")
    c = mq.evaluate_candidate(c["id"])
    hid, hepoch = make_holder(core, project, sup)
    out = mq.integrate_candidate(c["id"], holder_job_id=hid, holder_lease_epoch=hepoch)
    assert out.state == CandidateState.INTEGRATED.value

    row = core._store.get_integration_candidate(c["id"])
    # The integration worktree is under the worktrees root, NOT the writer
    # worktree (repo), and its branch is integration/<target>.
    assert row["integration_worktree_path"] != repo
    assert row["integration_worktree_path"].startswith(wt_root)
    assert row["integration_branch"] == "integration/main"
    # The integration branch never equals the target (CASE 14).
    assert row["integration_branch"] != "main"
    # A real git worktree + branch exist.
    assert os.path.isdir(row["integration_worktree_path"])
    branches = run_git(repo, "branch", "--list", "integration/main").stdout
    assert "integration/main" in branches
    core.close()


def test_case13_target_branch_never_mutated(tmp_path):
    core, project, sup, repo, base = _mk(tmp_path)
    new_branch(repo, "feature-a")
    head_a = write_commit(repo, "feature_a.py", "x = 1\n", "a")
    run_git(repo, "checkout", "-q", "main")
    jid, _ = _source(core, project, sup, repo, base, "a", head_a, "feature-a")
    mq = make_mq(core, str(tmp_path / "wts"))
    c = mq.enqueue_candidate(jid, "main")
    mq.evaluate_candidate(c["id"])
    hid, hepoch = make_holder(core, project, sup)
    out = mq.integrate_candidate(c["id"], holder_job_id=hid, holder_lease_epoch=hepoch)
    assert out.state == CandidateState.INTEGRATED.value
    # The target branch (main) HEAD is unchanged (still base).
    assert git_sha(repo, "main") == base
    core.close()


def test_case14_integration_branch_never_equals_target(tmp_path):
    from argent_core.merge_queue import MergeQueue
    core, project, sup, repo, base = _mk(tmp_path)
    mq = MergeQueue(core._store, worktrees_root=str(tmp_path / "wts"))
    for target in ("main", "master", "stable", "release/v2", "production"):
        assert mq._integration_branch(target) != target
        assert mq._integration_branch(target).startswith("integration/")
    core.close()


# ---------------------------------------------------------------------------
# CASE 15/16/17 — authoritative conflict detection, no partial INTEGRATED
# ---------------------------------------------------------------------------

def test_case15_conflict_detected_via_git(tmp_path):
    core, project, sup, repo, base = _mk(tmp_path)
    # C and D both modify the same file -> git conflict.
    new_branch(repo, "c")
    write_commit(repo, "app.py", "def add(a, b):\n    return a + b + 1000\n", "c")
    head_c = git_sha(repo)
    run_git(repo, "checkout", "-q", "main")
    new_branch(repo, "d")
    write_commit(repo, "app.py", "def add(a, b):\n    return a * b\n", "d")
    head_d = git_sha(repo)
    run_git(repo, "checkout", "-q", "main")

    jc, _ = _source(core, project, sup, repo, base, "c", head_c, "c", ["app.py"])
    jd, _ = _source(core, project, sup, repo, base, "d", head_d, "d", ["app.py"])
    mq = make_mq(core, str(tmp_path / "wts"))
    cc = mq.enqueue_candidate(jc, "main")
    cd = mq.enqueue_candidate(jd, "main")
    mq.evaluate_candidate(cc["id"])
    mq.evaluate_candidate(cd["id"])
    hid, hepoch = make_holder(core, project, sup)
    # C integrates cleanly (fast-forward); D conflicts.
    out = mq.process_target(repo, "main", holder_job_id=hid, holder_lease_epoch=hepoch)
    assert cc["id"] in out.integrated
    assert cd["id"] in out.conflicted
    # D is classified CONFLICT via git (not LLM).
    drow = core._store.get_integration_candidate(cd["id"])
    assert drow["state"] == CandidateState.CONFLICTED.value
    assert drow["merge_classification"] == MergeClassification.CONFLICT.value
    # CASE 17: no partial authoritative INTEGRATED result for D.
    assert drow["integrated_head"] is None
    core.close()


def test_case16_no_blind_ours_theirs_or_force_rebase(tmp_path):
    # The GitClient merge path only ever issues a plain `git merge --no-ff`
    # (no -X ours/theirs, no rebase --force, no checkout --theirs).  Verify by
    # driving a conflict and confirming the worktree is left clean (abort) and
    # the classification is CONFLICT (never silently resolved).
    core, project, sup, repo, base = _mk(tmp_path)
    new_branch(repo, "c")
    write_commit(repo, "app.py", "def add(a, b):\n    return a + b + 1\n", "c")
    head_c = git_sha(repo)
    run_git(repo, "checkout", "-q", "main")
    new_branch(repo, "d")
    write_commit(repo, "app.py", "def add(a, b):\n    return a + b + 2\n", "d")
    head_d = git_sha(repo)
    run_git(repo, "checkout", "-q", "main")
    jc, _ = _source(core, project, sup, repo, base, "c", head_c, "c", ["app.py"])
    jd, _ = _source(core, project, sup, repo, base, "d", head_d, "d", ["app.py"])
    mq = make_mq(core, str(tmp_path / "wts"))
    cc = mq.enqueue_candidate(jc, "main")
    cd = mq.enqueue_candidate(jd, "main")
    mq.evaluate_candidate(cc["id"])
    mq.evaluate_candidate(cd["id"])
    hid, hepoch = make_holder(core, project, sup)
    out = mq.process_target(repo, "main", holder_job_id=hid, holder_lease_epoch=hepoch)
    assert cd["id"] in out.conflicted
    # The integration worktree was aborted back to a clean state.
    drow = core._store.get_integration_candidate(cd["id"])
    wt = drow["integration_worktree_path"] or core._store.get_integration_candidate(cc["id"])["integration_worktree_path"]
    assert not mq.git.is_dirty(wt)
    core.close()


def test_classify_merge_real_git_classifications(tmp_path):
    from argent_core.integration_candidate import GitClient, classify_merge

    core, project, sup, repo, base = _mk(tmp_path)
    git = GitClient()
    # CLEAN_APPLY: source is a descendant of the target tip.
    assert classify_merge(git, repo, target_tip=base, source_head=base,
                          claimed_base=base) == MergeClassification.CLEAN_APPLY
    new_branch(repo, "fa")
    head_a = write_commit(repo, "feature_a.py", "a = 1\n", "a")
    run_git(repo, "checkout", "-q", "main")
    # DIVERGED_CLEAN: diverged but clean (head_a vs a later commit).
    new_branch(repo, "later")
    later = write_commit(repo, "other.py", "o = 1\n", "later")
    run_git(repo, "checkout", "-q", "main")
    cls = classify_merge(git, repo, target_tip=later, source_head=head_a,
                         claimed_base=base)
    assert cls in (MergeClassification.DIVERGED_CLEAN,
                   MergeClassification.CLEAN_APPLY)
    # STALE_BASE: unrelated history (a non-existent-but-SHA target has no
    # common merge base).
    assert classify_merge(git, repo, target_tip="0" * 40, source_head=head_a,
                          claimed_base=base) == MergeClassification.STALE_BASE
    # UNKNOWN: a non-SHA target/source is fail-closed.
    assert classify_merge(git, repo, target_tip="not-a-sha", source_head=head_a,
                          claimed_base=base) == MergeClassification.UNKNOWN
    # DEPENDENCY_NOT_INTEGRATED short-circuits.
    assert classify_merge(git, repo, target_tip=later, source_head=head_a,
                          claimed_base=base,
                          dependency_integrated=False) == MergeClassification.DEPENDENCY_NOT_INTEGRATED
    core.close()


# ---------------------------------------------------------------------------
# CASE 21/22/23 — fresh integration TestPlan on the integrated snapshot
# ---------------------------------------------------------------------------

def test_case21_integration_plan_is_fresh_snapshot(tmp_path):
    core, project, sup, repo, base = _mk(tmp_path)
    new_branch(repo, "feature-a")
    head_a = write_commit(repo, "feature_a.py", "def fa():\n    return 1\n", "a")
    run_git(repo, "checkout", "-q", "main")
    jid, _ = _source(core, project, sup, repo, base, "a", head_a, "feature-a")
    captured = {}

    def plan_builder(candidate, changed, base_sha):
        captured["changed"] = changed
        captured["base_sha"] = base_sha
        return type("P", (), {"plan_hash": "fresh", "verdict": "DONE"})()

    def runner(candidate, worktree_path, plan, changed):
        captured["worktree"] = worktree_path
        return make_test_evidence("DONE", plan, worktree_path)

    mq = make_mq(core, str(tmp_path / "wts"))
    mq._plan_builder = plan_builder
    mq._test_runner = runner
    c = mq.enqueue_candidate(jid, "main")
    mq.evaluate_candidate(c["id"])
    hid, hepoch = make_holder(core, project, sup)
    out = mq.integrate_candidate(c["id"], holder_job_id=hid, holder_lease_epoch=hepoch)
    assert out.state == CandidateState.INTEGRATED.value
    # The plan was built against the integrated snapshot's changed paths
    # (feature_a.py is present), from the integration base.
    assert "feature_a.py" in captured["changed"]
    assert captured["base_sha"] == base
    # The runner was pointed at the integration worktree, NOT the writer repo.
    assert captured["worktree"] != repo
    core.close()


def test_case23_stale_source_pass_cannot_close_integration(tmp_path):
    # The test runner receives ONLY the integration worktree; a stale PASS on
    # the writer worktree can never close integration.  Verify the runner is
    # never handed the source/writer repo path.
    core, project, sup, repo, base = _mk(tmp_path)
    new_branch(repo, "feature-a")
    head_a = write_commit(repo, "feature_a.py", "x = 1\n", "a")
    run_git(repo, "checkout", "-q", "main")
    jid, _ = _source(core, project, sup, repo, base, "a", head_a, "feature-a")
    seen = []

    def runner(candidate, worktree_path, plan, changed):
        seen.append(worktree_path)
        return make_test_evidence("DONE", plan, worktree_path)

    mq = make_mq(core, str(tmp_path / "wts"))
    mq._test_runner = runner
    c = mq.enqueue_candidate(jid, "main")
    mq.evaluate_candidate(c["id"])
    hid, hepoch = make_holder(core, project, sup)
    mq.integrate_candidate(c["id"], holder_job_id=hid, holder_lease_epoch=hepoch)
    assert seen and all(p != repo for p in seen)
    core.close()


def test_case22_default_plan_builder_uses_test_planning(tmp_path, monkeypatch):
    # The default integration plan builder goes through the Phase F
    # ``build_test_plan`` with the union of git changed paths + base ref.
    import argent_core.test_planning as tp

    core, project, sup, repo, base = _mk(tmp_path)
    captured = {}

    def fake_build(evidence, policy, inventory, mac_key=None):
        captured["changed"] = evidence.changed_paths
        captured["base"] = evidence.base_ref
        captured["inventory"] = inventory is not None
        return object()

    monkeypatch.setattr(tp, "build_test_plan", fake_build)
    mq = MergeQueue(core._store, worktrees_root=str(tmp_path / "wts"))
    mq._default_plan_builder({"id": "x"}, ("feature_a.py", "feature_b.py"), base)
    assert captured["changed"] == ("feature_a.py", "feature_b.py")
    assert captured["base"] == base
    assert captured["inventory"] is True
    core.close()


def test_high6_plan_is_phase_closing_and_inherits_risk(tmp_path, monkeypatch):
    # HIGH-6: the integration plan is built phase_closing=True and inherits the
    # source task's risk classification so HIGH-risk integration forces the
    # broad closing full suite.
    import argent_core.test_planning as tp
    from i2_helpers import TEST_MAC_KEY

    core, project, sup, repo, base = _mk(tmp_path)
    new_branch(repo, "feature-a")
    head_a = write_commit(repo, "feature_a.py", "a = 1\n", "a")
    from i2_helpers import run_git as _rg
    _rg(repo, "checkout", "-q", "main")
    jid, _ = make_source(core, project, sup, "a", repo=repo, branch="feature-a",
                         head=head_a, base=base, risk="HIGH")
    captured = {}

    def fake_build(evidence, policy, inventory, mac_key=None):
        captured["phase_closing"] = evidence.phase_closing
        captured["risk_class"] = evidence.risk_class
        return object()

    monkeypatch.setattr(tp, "build_test_plan", fake_build)
    mq = MergeQueue(core._store, worktrees_root=str(tmp_path / "wts"),
                    mac_key=TEST_MAC_KEY)
    mq._default_plan_builder(
        {"id": "x", "source_job_id": jid}, ("feature_a.py",), base)
    assert captured["phase_closing"] is True
    assert captured["risk_class"] == "HIGH"
    core.close()


def test_low8_unowned_integration_branch_not_force_deleted(tmp_path):
    # LOW-8: an existing integration branch that the queue cannot prove it owns
    # is never force-deleted (fail closed).
    core, project, sup, repo, base = _mk(tmp_path)
    new_branch(repo, "feature-a")
    head_a = write_commit(repo, "feature_a.py", "a = 1\n", "a")
    run_git(repo, "checkout", "-q", "main")
    jid, _ = make_source(core, project, sup, "a", repo=repo, branch="feature-a",
                         head=head_a, base=base)
    mq = make_mq(core, str(tmp_path / "wts"))
    c = mq.enqueue_candidate(jid, "main")
    mq.evaluate_candidate(c["id"])
    branch = mq._integration_branch("main")
    # Pre-create an integration branch the queue has never recorded.
    run_git(repo, "branch", branch)
    hid, hepoch = make_holder(core, project, sup)
    out = mq.integrate_candidate(c["id"], holder_job_id=hid, holder_lease_epoch=hepoch)
    assert out.state == CandidateState.FAILED.value
    assert "unowned" in (out.detail or "")
    # The unowned branch is untouched.
    assert run_git(repo, "rev-parse", "--verify", f"refs/heads/{branch}").returncode == 0
    core.close()


def test_high9_diverged_clean_uses_normal_merge_not_rebase(tmp_path):
    # LOW-9: DIVERGED_CLEAN (source diverged from the integration base but a
    # clean three-way merge) proceeds as a NORMAL --no-ff merge commit (two
    # parents), never a rebase / history rewrite.
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
    hid, hepoch = make_holder(core, project, sup)
    out = mq.process_target(repo, "main", holder_job_id=hid, holder_lease_epoch=hepoch)
    assert ca["id"] in out.integrated and cb["id"] in out.integrated
    head = core._store.get_integration_candidate(cb["id"])["integrated_head"]
    # A normal merge commit has exactly two parents (rebase would be linear).
    parents = run_git(repo, "rev-list", "--parents", "-n", "1", head).stdout.split()
    assert len(parents) == 3  # <commit> <parent1> <parent2>
    core.close()
