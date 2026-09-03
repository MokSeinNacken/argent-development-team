"""Phase I2 — controller-authoritative candidate admission (CASE 1/35/40).

Candidates are created ONLY from trusted store facts: a source job in a valid
integration-ready terminal state with proven provenance and no unresolved
HIGH findings.  Agent prose is never sufficient (CASE 1).  Review independence
(CASE 35) and immutable terminal source (CASE 40) are covered here too.
"""

from __future__ import annotations

import pytest

from argent_core.integration_candidate import CandidateState, IntegrationError
from argent_core.merge_queue import MergeQueue
from i2_helpers import (
    add_finding,
    add_review,
    git_sha,
    init_repo,
    make_env,
    make_holder,
    make_mq,
    make_source,
    new_branch,
    write_commit,
)


@pytest.fixture
def env(tmp_path):
    core, project, sup = make_env(str(tmp_path / "t.db"))
    repo = init_repo(str(tmp_path / "git"))
    base = git_sha(repo)
    yield core, project, sup, repo, base
    core.close()


def _mq(core, tmp_path):
    return make_mq(core, str(tmp_path / "wts"))


# ---------------------------------------------------------------------------
# CASE 1 — candidate admission requires a valid integration-ready source
# ---------------------------------------------------------------------------

def test_case1_admission_rejects_non_done_source(env, tmp_path):
    core, project, sup, repo, base = env
    # A source job that is NOT terminal DONE is not admissible.
    task = core.create_task(project.id, "running", "owner:authenticated")
    core.start_task_run(task.id, "owner:authenticated")
    job = sup.store.create_job(task.id, idempotency_key="j-run")
    jid = job.supervisor_job_id
    mq = _mq(core, tmp_path)
    errors = mq.admission_errors(jid)
    assert "source_not_terminal_done" in errors
    with pytest.raises(IntegrationError):
        mq.enqueue_candidate(jid, "main")


def test_case1_admission_requires_provenance(env, tmp_path):
    core, project, sup, repo, base = env
    # A DONE source with no repo identity / base commit / head is not admissible.
    jid, _ = make_source(core, project, sup, "s", repo=repo, branch="f",
                         head=git_sha(repo), base=base)
    core._store._update_supervisor_job(jid, repo_identity=None, base_commit=None,
                                       expected_head=None, current_head=None)
    mq = _mq(core, tmp_path)
    errors = mq.admission_errors(jid)
    assert "repo_identity_missing" in errors
    assert "base_commit_not_proven" in errors
    assert "source_head_not_proven" in errors


def test_case1_admission_rejects_open_high_finding(env, tmp_path):
    core, project, sup, repo, base = env
    jid, task_id = make_source(core, project, sup, "s", repo=repo, branch="f",
                               head=git_sha(repo), base=base)
    add_finding(core, task_id, "HIGH", status="open")
    mq = _mq(core, tmp_path)
    assert "open_high_finding" in mq.admission_errors(jid)


def test_admission_rejects_lowercase_open_high_finding(env, tmp_path):
    # HIGH-4: validated severities are lowercase ``high``/``critical``
    # (outputs.py); admission must canonicalise case-insensitively.
    core, project, sup, repo, base = env
    jid, task_id = make_source(core, project, sup, "s", repo=repo, branch="f",
                               head=git_sha(repo), base=base)
    add_finding(core, task_id, "high", status="open")
    mq = _mq(core, tmp_path)
    assert "open_high_finding" in mq.admission_errors(jid)


def test_admission_rejects_open_critical_finding(env, tmp_path):
    # HIGH-4: an open CRITICAL finding also blocks admission.
    core, project, sup, repo, base = env
    jid, task_id = make_source(core, project, sup, "s", repo=repo, branch="f",
                               head=git_sha(repo), base=base)
    add_finding(core, task_id, "critical", status="open")
    mq = _mq(core, tmp_path)
    assert "open_critical_finding" in mq.admission_errors(jid)


def test_case1_agent_prose_is_never_sufficient(env, tmp_path):
    core, project, sup, repo, base = env
    # A job that is NOT terminal DONE, even with complete provenance and a
    # plausible "ready to merge" prose marker, is never admissible — the
    # controller reads only trusted facts (terminal + provenance + findings),
    # never agent prose.
    task = core.create_task(project.id, "prose", "owner:authenticated")
    core.start_task_run(task.id, "owner:authenticated")
    job = sup.store.create_job(task.id, idempotency_key="j-prose")
    jid = job.supervisor_job_id
    sup.store.claim_job(jid, owner_instance_id="OWNER", ttl_seconds=3600)
    core._store._update_supervisor_job(
        jid, repo_identity=repo, base_commit=base, expected_head=git_sha(repo),
        current_head=git_sha(repo), branch_identity="f",
        last_error_code="agent prose: ready to merge",
    )
    mq = _mq(core, tmp_path)
    assert "source_not_terminal_done" in mq.admission_errors(jid)
    with pytest.raises(IntegrationError):
        mq.enqueue_candidate(jid, "main")


# ---------------------------------------------------------------------------
# CASE 3 (admission half) — idempotent creation + PENDING start
# ---------------------------------------------------------------------------

def test_enqueue_idempotent_and_starts_pending(env, tmp_path):
    core, project, sup, repo, base = env
    jid, _ = make_source(core, project, sup, "s", repo=repo, branch="f",
                         head=git_sha(repo), base=base)
    mq = _mq(core, tmp_path)
    c1 = mq.enqueue_candidate(jid, "main")
    c2 = mq.enqueue_candidate(jid, "main")
    assert c1["id"] == c2["id"]
    assert c1["state"] == CandidateState.PENDING.value
    # A candidate never starts READY.
    assert c1["state"] != CandidateState.READY.value


# ---------------------------------------------------------------------------
# CASE 35 — review independence (writer cannot approve its own integration)
# ---------------------------------------------------------------------------

def test_case35_high_risk_requires_independent_review(env, tmp_path):
    core, project, sup, repo, base = env
    jid, task_id = make_source(core, project, sup, "s", repo=repo, branch="f",
                               head=git_sha(repo), base=base, risk="HIGH")
    mq = _mq(core, tmp_path)
    c = mq.enqueue_candidate(jid, "main")
    # No independent review yet -> stays PENDING with REVIEW_REQUIRED.
    c = mq.evaluate_candidate(c["id"])
    assert c["state"] == CandidateState.PENDING.value
    assert c["last_error_code"] == "REVIEW_REQUIRED"
    # The writer's own agent review is NOT sufficient (CASE 35).
    add_review(core, task_id, "approved", source_class="agent")
    c = mq.evaluate_candidate(c["id"])
    assert c["state"] == CandidateState.PENDING.value
    assert c["last_error_code"] == "REVIEW_REQUIRED"
    # An independent controller review unblocks promotion to READY.
    add_review(core, task_id, "approved", source_class="controller")
    c = mq.evaluate_candidate(c["id"])
    assert c["state"] == CandidateState.READY.value


def test_case35_normal_risk_no_review_required(env, tmp_path):
    core, project, sup, repo, base = env
    jid, _ = make_source(core, project, sup, "s", repo=repo, branch="f",
                         head=git_sha(repo), base=base, risk="NORMAL")
    mq = _mq(core, tmp_path)
    c = mq.enqueue_candidate(jid, "main")
    c = mq.evaluate_candidate(c["id"])
    assert c["state"] == CandidateState.READY.value


# ---------------------------------------------------------------------------
# CASE 40 — terminal source job immutable after integration failure
# ---------------------------------------------------------------------------

def test_case40_source_job_terminal_immutable_after_failure(env, tmp_path):
    core, project, sup, repo, base = env
    jid, _ = make_source(core, project, sup, "s", repo=repo, branch="f",
                         head=git_sha(repo), base=base)
    mq = _mq(core, tmp_path)
    c = mq.enqueue_candidate(jid, "does-not-exist")
    c = mq.evaluate_candidate(c["id"])
    assert c["state"] == CandidateState.READY.value
    hid, hepoch = make_holder(core, project, sup)
    # Integrate against a NON-EXISTENT target branch -> FAILED (unreadable).
    out = mq.integrate_candidate(c["id"], holder_job_id=hid,
                                 holder_lease_epoch=hepoch)
    assert out.state == CandidateState.FAILED.value
    # The source job's terminal state is immutable and untouched by the failure.
    assert core._store.get_supervisor_job(jid)["terminal"] == "DONE"


def test_case40_source_head_change_invalidates_candidate(env, tmp_path):
    core, project, sup, repo, base = env
    jid, _ = make_source(core, project, sup, "s", repo=repo, branch="f",
                         head=git_sha(repo), base=base)
    mq = _mq(core, tmp_path)
    c = mq.enqueue_candidate(jid, "main")
    c = mq.evaluate_candidate(c["id"])
    # Mutate the source head AFTER candidate creation -> the candidate becomes
    # STALE at integration time (source_head_changed).
    new_branch(repo, "f2")
    new_head = write_commit(repo, "other.py", "x = 1\n", "other")
    core._store._update_supervisor_job(jid, current_head=new_head, expected_head=new_head)
    hid, hepoch = make_holder(core, project, sup)
    out = mq.integrate_candidate(c["id"], holder_job_id=hid, holder_lease_epoch=hepoch)
    assert out.state == CandidateState.STALE.value
    assert out.detail == "source_head_changed"
