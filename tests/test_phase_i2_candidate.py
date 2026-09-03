"""Phase I2 — integration candidate model + deterministic ordering (pure).

Covers the candidate state model (CASE 2/3), deterministic merge ordering
(CASE 4/5/6/7) and the argv-only git-client safety contract (CASE 32/33/34).
Pure and deterministic — no live DB, no network, no LLM.
"""

from __future__ import annotations

import pytest

from argent_core.integration_candidate import (
    CANDIDATE_STATE_VALUES,
    CandidateState,
    GitClient,
    IntegrationCandidate,
    MergeClassification,
    candidate_id_for,
    deterministic_order,
    serialize_candidate_result,
)
from argent_core.worktree import is_sha_like


def C(cid, *, state="READY", pos=0, prio=0, depends_on=None):
    return IntegrationCandidate(
        id=cid, repository="repo", integration_target="main",
        source_job_id=f"job:{cid}", state=state, queue_position=pos,
        priority=prio, depends_on=depends_on,
    )


# ---------------------------------------------------------------------------
# CASE 2 — bounded candidate states, separate from primary job states
# ---------------------------------------------------------------------------

def test_case2_candidate_states_are_bounded_and_separate():
    # The candidate state model is an independent, bounded enum — it does not
    # touch job_state.PrimaryState (8 states).  Assert the exact state set.
    expected = {"PENDING", "READY", "INTEGRATING", "CONFLICTED", "STALE",
                "BLOCKED", "INTEGRATED", "FAILED"}
    assert set(CANDIDATE_STATE_VALUES) == expected
    # The primary job-state model is untouched (still the exact 8 states).
    from argent_core.job_state import PRIMARY_STATE_VALUES
    assert PRIMARY_STATE_VALUES == (
        "QUEUED", "RUNNING", "WAITING_EXTERNAL", "OWNER_GATE",
        "BLOCKED", "FAILED", "LOST", "DONE")
    # Terminal candidate states are bounded.
    from argent_core.integration_candidate import TERMINAL_CANDIDATE_STATES
    assert {s.value for s in TERMINAL_CANDIDATE_STATES} == {
        "INTEGRATED", "CONFLICTED", "STALE", "BLOCKED", "FAILED"}


# ---------------------------------------------------------------------------
# CASE 3 — deterministic candidate id + bounded serialization
# ---------------------------------------------------------------------------

def test_case3_candidate_id_deterministic():
    a = candidate_id_for("repo", "main", "job:a")
    b = candidate_id_for("repo", "main", "job:a")
    c = candidate_id_for("repo", "main", "job:b")
    assert a == b
    assert a != c
    assert a.startswith("ic_")


def test_serialize_candidate_result_bounded():
    assert serialize_candidate_result(None) is None
    out = serialize_candidate_result({"verdict": "DONE", "plan_hash": "h"})
    import json
    assert json.loads(out)["verdict"] == "DONE"
    with pytest.raises(Exception):
        serialize_candidate_result({"blob": "x" * (70 * 1024)})


# ---------------------------------------------------------------------------
# CASE 4/5/6/7 — deterministic ordering
# ---------------------------------------------------------------------------

def test_case4_dependency_orders_before_dependent():
    a = C("a", pos=2)
    b = C("b", pos=1, depends_on="a")
    order = deterministic_order([b, a], integrated_ids=set())
    assert [c.id for c in order.ordered] == ["a", "b"]
    assert order.deferred == []
    assert order.blocked == []


def test_case5_fifo_queue_position_when_no_dependency():
    a = C("a", pos=0)
    b = C("b", pos=1)
    c = C("c", pos=2)
    order = deterministic_order([c, a, b], integrated_ids=set())
    assert [x.id for x in order.ordered] == ["a", "b", "c"]


def test_case6_priority_tiebreak_after_fifo():
    # FIFO dominates priority (§4): a lower queue_position wins even at lower
    # priority; priority only breaks a FIFO tie.
    a = C("a", pos=0, prio=0)
    b = C("b", pos=1, prio=100)
    order = deterministic_order([b, a], integrated_ids=set())
    assert [x.id for x in order.ordered] == ["a", "b"]
    # Same position -> priority desc.
    a2 = C("a2", pos=0, prio=1)
    b2 = C("b2", pos=0, prio=5)
    order2 = deterministic_order([a2, b2], integrated_ids=set())
    assert [x.id for x in order2.ordered] == ["b2", "a2"]


def test_case7_unknown_dependency_defers_no_llm_ordering():
    a = C("a", pos=0)
    b = C("b", pos=1, depends_on="missing")  # unknown id -> defer
    order = deterministic_order([a, b], integrated_ids=set())
    assert [x.id for x in order.ordered] == ["a"]
    assert [x.id for x in order.deferred] == ["b"]
    assert order.blocked == []


def test_case7_cycle_is_blocked_conservatively():
    a = C("a", pos=0, depends_on="b")
    b = C("b", pos=1, depends_on="a")
    order = deterministic_order([a, b], integrated_ids=set())
    assert order.ordered == []
    assert {x.id for x in order.blocked} == {"a", "b"}


def test_order_ignores_non_ready_states():
    a = C("a", pos=0)
    b = C("b", pos=1, state="PENDING")
    c = C("c", pos=2, state="INTEGRATED")
    order = deterministic_order([a, b, c], integrated_ids=set())
    assert [x.id for x in order.ordered] == ["a"]


def test_order_skips_already_integrated_dependency():
    a = C("a", pos=0, state="INTEGRATED")
    b = C("b", pos=1, depends_on="a")
    order = deterministic_order([b], integrated_ids={"a"})
    assert [x.id for x in order.ordered] == ["b"]


# ---------------------------------------------------------------------------
# CASE 32/33/34 — argv-only git safety (no shell / eval / exec)
# ---------------------------------------------------------------------------

def test_case32_git_client_builds_argv_only():
    captured = {}

    def runner(argv):
        captured["argv"] = argv
        return (0, "", "")

    git = GitClient(runner=runner)
    git._run(["rev-parse", "HEAD"], cwd="/tmp/r")
    argv = captured["argv"]
    assert argv[0] == "git"
    assert argv[1] == "-C"
    assert argv[2] == "/tmp/r"
    # No shell: the argv is a list of discrete tokens, never a single string.
    assert all(" " not in tok for tok in argv[3:])


def test_case33_ref_validation_fail_closed():
    git = GitClient()
    # A malicious ref (shell metacharacters) is rejected before reaching git.
    assert git.resolve_sha("/tmp", "; rm -rf /") is None
    assert git.resolve_sha("/tmp", "a b") is None
    assert is_sha_like("deadbeef") is True
    assert is_sha_like("; rm -rf /") is False
    assert is_sha_like("ZZZZ") is False
    assert is_sha_like("") is False


def test_case34_merge_tree_rejects_invalid_sha():
    git = GitClient()
    assert git.merge_tree_conflicts("/tmp", "not-a-sha", "also-not") is None
    assert git.merge_tree_conflicts("/tmp", "a" * 40, "b; rm -rf /") is None


def test_merge_classification_values_bounded():
    vals = {m.value for m in MergeClassification}
    assert vals == {"CLEAN_APPLY", "DIVERGED_CLEAN", "CONFLICT", "STALE_BASE",
                    "DEPENDENCY_NOT_INTEGRATED", "UNKNOWN"}


def test_case33_path_injection_fail_closed(tmp_path):
    import os

    root = str(tmp_path / "root")
    os.makedirs(root)
    git = GitClient(allowed_root=root)
    # A missing path is fail-closed (never reaches git).
    assert git.head(str(tmp_path / "nope")) is None
    # An absolute path outside the allowed root is fail-closed.
    outside = str(tmp_path / "outside")
    os.makedirs(outside)
    assert git.head(outside) is None
    # A relative path is fail-closed.
    assert git.head("relative") is None
    # Branch/ref tokens that look like options or revision/glob syntax are
    # rejected before reaching git.
    assert git.resolve_sha(root, "-f") is None
    assert git.resolve_sha(root, "a..b") is None
    assert git.resolve_sha(root, "a~1") is None
    assert git.resolve_sha(root, "a^1") is None
    assert git.resolve_sha(root, "a@{") is None
