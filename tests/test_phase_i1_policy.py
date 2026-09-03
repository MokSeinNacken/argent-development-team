"""Phase I1 — pure concurrency-policy unit tests (structural decisions).

These test :mod:`argent_core.concurrency_policy` in isolation: deterministic,
I/O-free, LLM-free.  Every decision is a pure function of (candidate facts,
active-job facts), so each assertion is fully explicit.
"""

from __future__ import annotations

import pytest

from argent_core.concurrency_policy import (
    ACTION_GLOBAL,
    ACTION_REPO_GLOBAL,
    DEPENDENCY_MISSING,
    ConcurrencyReasonCode,
    ConcurrencyVerdict,
    JobFacts,
    MutationFootprint,
    action_lock_name,
    decide,
    footprint_overlap,
    path_roots_conflict,
    serialize_footprint_paths,
)


def fp(*, repo="repo-A", wt="/wt/A", branch="main", roots=("src/a",),
        integration=None):
    return MutationFootprint(
        repo_identity=repo,
        canonical_worktree_path=wt,
        branch_identity=branch,
        path_roots=tuple(roots),
        integration_target=integration,
    )


def facts(job_id, *, role="implementer", rc="MEDIUM", footprint=None,
          depends_on=None, dep_terminal=None, action_class=None):
    return JobFacts(
        job_id=job_id, role=role, resource_class=rc,
        footprint=footprint or fp(),
        depends_on=depends_on, dependency_terminal=dep_terminal,
        action_class=action_class,
    )


def ro_facts(job_id, *, role="analyst", rc="LIGHT"):
    return facts(job_id, role=role, rc=rc)


# ---------------------------------------------------------------------------
# Case 1–6: structural overlap decisions
# ---------------------------------------------------------------------------

def test_case1_two_readonly_jobs_allowed():
    d = decide(ro_facts("b"), [ro_facts("a")])
    assert d.verdict == ConcurrencyVerdict.ALLOW_PARALLEL.value
    assert d.reason_code == ConcurrencyReasonCode.READONLY_SAFE.value


def test_case2_two_jobs_different_repos_allowed():
    a = facts("a", footprint=fp(repo="repo-A", wt="/wt/A", roots=("src/a",)))
    b = facts("b", footprint=fp(repo="repo-B", wt="/wt/B", roots=("src/b",)))
    d = decide(b, [a])
    assert d.verdict == ConcurrencyVerdict.ALLOW_PARALLEL.value
    assert d.reason_code in (
        ConcurrencyReasonCode.DISTINCT_REPO.value,
        ConcurrencyReasonCode.DISTINCT_WORKTREE.value,
    )


def test_case3_two_writers_distinct_worktrees_disjoint_footprints_eligible():
    a = facts("a", footprint=fp(repo="repo-A", wt="/wt/A", branch="feat-a",
                                roots=("src/feature_a",)))
    b = facts("b", footprint=fp(repo="repo-A", wt="/wt/B", branch="feat-b",
                                roots=("src/feature_b",)))
    d = decide(b, [a])
    assert d.verdict == ConcurrencyVerdict.ALLOW_PARALLEL.value
    assert d.reason_code == ConcurrencyReasonCode.DISTINCT_WORKTREE.value


def test_case4_same_worktree_two_writers_serialize():
    a = facts("a", footprint=fp(repo="repo-A", wt="/wt/A", roots=("src/x",)))
    b = facts("b", footprint=fp(repo="repo-A", wt="/wt/A", roots=("src/y",)))
    d = decide(b, [a])
    assert d.verdict == ConcurrencyVerdict.SERIALIZE.value
    assert d.reason_code == ConcurrencyReasonCode.WORKTREE_CONFLICT.value
    assert d.conflict_job_id == "a"


def test_case5_same_repo_overlapping_footprint_serialize():
    a = facts("a", footprint=fp(repo="repo-A", wt="/wt/A", roots=("src/shared",)))
    b = facts("b", footprint=fp(repo="repo-A", wt="/wt/B", roots=("src/shared",)))
    d = decide(b, [a])
    assert d.verdict == ConcurrencyVerdict.SERIALIZE.value
    assert d.reason_code == ConcurrencyReasonCode.REPO_OVERLAP.value


def test_case5b_same_branch_serialize():
    a = facts("a", footprint=fp(repo="repo-A", wt="/wt/A", branch="main",
                                roots=("src/a",)))
    b = facts("b", footprint=fp(repo="repo-A", wt="/wt/B", branch="main",
                                roots=("src/b",)))
    d = decide(b, [a])
    assert d.verdict == ConcurrencyVerdict.SERIALIZE.value
    assert d.reason_code == ConcurrencyReasonCode.REPO_OVERLAP.value


def test_case5c_shared_integration_target_serialize():
    a = facts("a", footprint=fp(repo="repo-A", wt="/wt/A", branch="f1",
                                roots=("src/a",), integration="main"))
    b = facts("b", footprint=fp(repo="repo-A", wt="/wt/B", branch="f2",
                                roots=("src/b",), integration="main"))
    d = decide(b, [a])
    assert d.verdict == ConcurrencyVerdict.SERIALIZE.value
    assert d.reason_code == ConcurrencyReasonCode.REPO_OVERLAP.value


def test_case6_unknown_overlap_serialize():
    # No repo identity -> cannot prove disjointness.
    a = facts("a", footprint=MutationFootprint(canonical_worktree_path="/wt/A"))
    b = facts("b", footprint=MutationFootprint(canonical_worktree_path="/wt/B"))
    d = decide(b, [a])
    assert d.verdict == ConcurrencyVerdict.SERIALIZE.value
    assert d.reason_code == ConcurrencyReasonCode.UNKNOWN_OVERLAP.value


def test_case6b_missing_path_roots_same_repo_unknown():
    a = facts("a", footprint=fp(repo="repo-A", wt="/wt/A", branch="f1",
                                roots=()))
    b = facts("b", footprint=fp(repo="repo-A", wt="/wt/B", branch="f2",
                                roots=()))
    d = decide(b, [a])
    assert d.verdict == ConcurrencyVerdict.SERIALIZE.value
    assert d.reason_code == ConcurrencyReasonCode.UNKNOWN_OVERLAP.value


# ---------------------------------------------------------------------------
# Case 12/13: dependencies (policy level)
# ---------------------------------------------------------------------------

def test_case12_dependency_not_met_defers():
    b = facts("b", depends_on="a", dep_terminal=None)  # a still running
    d = decide(b, [])
    assert d.verdict == ConcurrencyVerdict.DEFER.value
    assert d.reason_code == ConcurrencyReasonCode.DEPENDENCY_NOT_MET.value


def test_case12b_dependency_failed_defers():
    b = facts("b", depends_on="a", dep_terminal="FAILED")
    d = decide(b, [])
    assert d.verdict == ConcurrencyVerdict.DEFER.value
    assert d.reason_code == ConcurrencyReasonCode.DEPENDENCY_NOT_MET.value


def test_dependency_missing_blocks():
    b = facts("b", depends_on="a", dep_terminal=DEPENDENCY_MISSING)
    d = decide(b, [])
    assert d.verdict == ConcurrencyVerdict.BLOCK.value
    assert d.reason_code == ConcurrencyReasonCode.DEPENDENCY_UNKNOWN.value


def test_dependency_satisfied_proceeds():
    b = facts("b", depends_on="a", dep_terminal="DONE")
    d = decide(b, [ro_facts("a")])
    # dependency satisfied -> falls through to structural (read-only active)
    assert d.verdict == ConcurrencyVerdict.ALLOW_PARALLEL.value


# ---------------------------------------------------------------------------
# Case 23: action-class serialization boundary
# ---------------------------------------------------------------------------

def test_case23_global_action_serializes_against_any_active():
    b = facts("b", action_class=ACTION_GLOBAL)
    d = decide(b, [ro_facts("a")])
    assert d.verdict == ConcurrencyVerdict.SERIALIZE.value
    assert d.reason_code == ConcurrencyReasonCode.ACTION_GLOBAL_SERIALIZE.value


def test_repo_global_action_serializes_same_repo_only():
    a = facts("a", footprint=fp(repo="repo-A", wt="/wt/A", roots=("x",)))
    same_repo = facts("b", footprint=fp(repo="repo-A", wt="/wt/B", roots=("y",)),
                      action_class=ACTION_REPO_GLOBAL)
    d = decide(same_repo, [a])
    assert d.verdict == ConcurrencyVerdict.SERIALIZE.value

    other_repo = facts("c", footprint=fp(repo="repo-Z", wt="/wt/C", roots=("z",)),
                       action_class=ACTION_REPO_GLOBAL)
    d2 = decide(other_repo, [a])
    assert d2.verdict == ConcurrencyVerdict.ALLOW_PARALLEL.value


def test_action_lock_name_derivation():
    assert action_lock_name(ACTION_GLOBAL, name="merge") == "global:merge"
    assert action_lock_name(ACTION_REPO_GLOBAL, repo_identity="repo-A",
                            name="merge") == "repo:repo-A:merge"
    with pytest.raises(ValueError):
        action_lock_name(ACTION_REPO_GLOBAL, repo_identity=None, name="merge")
    with pytest.raises(ValueError):
        action_lock_name("BOGUS", name="merge")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_path_roots_conflict_prefix_intersection():
    assert path_roots_conflict(["src/a"], ["src/a/b"]) is True
    assert path_roots_conflict(["src/a"], ["src/b"]) is False
    assert path_roots_conflict(["src/a", "tests"], ["tests/x"]) is True
    assert path_roots_conflict([], ["src/a"]) is False


def test_footprint_overlap_classification():
    a = fp(repo="repo-A", wt="/wt/A", branch="f1", roots=("src/a",))
    b = fp(repo="repo-B", wt="/wt/B", branch="f1", roots=("src/b",))
    assert footprint_overlap(a, b) == "DISJOINT"
    unknown = MutationFootprint()
    assert footprint_overlap(a, unknown) == "UNKNOWN"


def test_serialize_footprint_paths_bounded():
    assert serialize_footprint_paths(["src/a", "src/a/b"]) is not None
    with pytest.raises(ValueError):
        serialize_footprint_paths([""])
    with pytest.raises(ValueError):
        serialize_footprint_paths([1, 2])  # non-strings


# ---------------------------------------------------------------------------
# Read-only roles never writer (Case 7 policy half)
# ---------------------------------------------------------------------------

def test_readonly_roles_are_not_writers():
    for role in ("analyst", "reviewer", "lead"):
        j = ro_facts("x", role=role, rc="LIGHT")
        assert j.is_writer is False
        assert j.is_readonly is True
    impl = facts("x", role="implementer", rc="MEDIUM")
    assert impl.is_writer is True
    assert impl.is_readonly is False


# ---------------------------------------------------------------------------
# F2: role-based writer classification (write-capable role => writer at LIGHT)
# ---------------------------------------------------------------------------

def test_write_capable_role_is_writer_regardless_of_class():
    # implementer/qa are write-capable roles: they reach broker writes + writer
    # binding even at resource class LIGHT, so they MUST be treated as writers
    # for structural overlap.
    for role in ("implementer", "qa"):
        j = JobFacts("x", role=role, resource_class="LIGHT",
                     footprint=fp(wt="/wt/A"))
        assert j.is_writer is True
        assert j.is_readonly is False
    # Read-only roles at LIGHT are NOT writers.
    for role in ("analyst", "reviewer", "lead"):
        j = JobFacts("x", role=role, resource_class="LIGHT",
                     footprint=fp(wt="/wt/A"))
        assert j.is_writer is False


def test_two_implementer_light_same_worktree_serialize():
    a = JobFacts("a", role="implementer", resource_class="LIGHT",
                 footprint=fp(repo="repo-A", wt="/wt/A", branch="f1",
                              roots=("src/a",)))
    b = JobFacts("b", role="implementer", resource_class="LIGHT",
                 footprint=fp(repo="repo-A", wt="/wt/A", branch="f2",
                              roots=("src/b",)))
    d = decide(b, [a])
    assert d.verdict == ConcurrencyVerdict.SERIALIZE.value
    assert d.reason_code == ConcurrencyReasonCode.WORKTREE_CONFLICT.value


def test_implementer_light_and_analyst_light_allowed():
    a = JobFacts("a", role="implementer", resource_class="LIGHT",
                 footprint=fp(repo="repo-A", wt="/wt/A", roots=("src/a",)))
    b = JobFacts("b", role="analyst", resource_class="LIGHT",
                 footprint=fp(repo="repo-A", wt="/wt/A", roots=("src/a",)))
    # analyst is read-only -> not a writer -> no structural conflict.
    d = decide(b, [a])
    assert d.verdict == ConcurrencyVerdict.ALLOW_PARALLEL.value
    assert d.reason_code == ConcurrencyReasonCode.READONLY_SAFE.value


def test_distinct_repo_emitted_when_repos_differ():
    a = facts("a", footprint=fp(repo="repo-A", wt="/wt/A", roots=("src/a",)))
    b = facts("b", footprint=fp(repo="repo-B", wt="/wt/B", roots=("src/b",)))
    d = decide(b, [a])
    assert d.verdict == ConcurrencyVerdict.ALLOW_PARALLEL.value
    assert d.reason_code == ConcurrencyReasonCode.DISTINCT_REPO.value


# ---------------------------------------------------------------------------
# F4: symmetric global / repo-global serialization
# ---------------------------------------------------------------------------

def test_active_global_blocks_ordinary_candidate():
    active = facts("g", action_class=ACTION_GLOBAL)
    ordinary = facts("b")  # no action class
    d = decide(ordinary, [active])
    assert d.verdict == ConcurrencyVerdict.SERIALIZE.value
    assert d.reason_code == ConcurrencyReasonCode.ACTION_GLOBAL_SERIALIZE.value


def test_active_repo_global_blocks_same_repo_candidate_only():
    active = facts("g", footprint=fp(repo="repo-A", wt="/wt/A", roots=("x",)),
                   action_class=ACTION_REPO_GLOBAL)
    same = facts("b", footprint=fp(repo="repo-A", wt="/wt/B", roots=("y",)))
    other = facts("c", footprint=fp(repo="repo-Z", wt="/wt/C", roots=("z",)))
    assert decide(same, [active]).verdict == ConcurrencyVerdict.SERIALIZE.value
    assert decide(other, [active]).verdict == ConcurrencyVerdict.ALLOW_PARALLEL.value


def test_candidate_global_serializes_against_any_active_job():
    b = facts("b", action_class=ACTION_GLOBAL)
    d = decide(b, [ro_facts("a")])
    assert d.verdict == ConcurrencyVerdict.SERIALIZE.value
    assert d.reason_code == ConcurrencyReasonCode.ACTION_GLOBAL_SERIALIZE.value


# ---------------------------------------------------------------------------
# Fail-closed root validation (absolute / drive / escape rejected)
# ---------------------------------------------------------------------------

def test_serialize_footprint_paths_rejects_unsafe_roots():
    for bad in ("/etc/passwd", "\\windows\\path", "C:\\src", "../escape",
                "a/../../escape", "..", "", "   "):
        with pytest.raises(ValueError):
            serialize_footprint_paths([bad])


def test_serialize_footprint_paths_accepts_safe_relative_roots():
    out = serialize_footprint_paths(["src/a", "src/a/../b", "tests/x"])
    assert out is not None
    import json
    roots = json.loads(out)
    # ``src/a/../b`` normalizes to ``src/b`` (still within the root).
    assert "src/b" in roots

