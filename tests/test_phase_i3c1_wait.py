"""Phase I3-C1 — CI wait lifecycle core (trusted, non-LLM).

Covers wait-identity binding, head-SHA binding, wake-once, backoff, PR
lifecycle, provider outage/rate-limit, cross-repo/PR isolation, and crash/
restart recovery — all deterministic through the real Store + FakeCiAdapter +
FakeClock.  No network, no LLM, no real process.
"""

from __future__ import annotations

import json

import pytest

from argent_core import Core
from argent_core.ci_external_wait import (
    PR_CLOSED,
    PR_MERGED,
    PR_OPEN,
    PR_UNKNOWN,
    PROVIDER_ERROR_RATE_LIMITED,
    PROVIDER_ERROR_UNAVAILABLE,
    CiState,
    CiWaitManager,
    FakeCiAdapter,
    ci_ref,
    load_policy,
    make_ci_check,
    make_ci_read,
    parse_ci_ref,
)
from argent_core.job_state import PrimaryState, QueueReason
from argent_core.models import LeaseError
from i3c1_helpers import (
    REPO,
    REPO_B,
    SHA_A,
    SHA_B,
    ci_spec,
    make_ci_manager,
    make_env,
    make_running_job,
)


def _enter(mgr, env, jid, job, **spec_kw):
    return mgr.enter_ci_wait(
        jid, spec=ci_spec(**spec_kw),
        owner_instance_id="A", lease_epoch=job["lease_epoch"],
    )


# ---------------------------------------------------------------------------
# ref identity
# ---------------------------------------------------------------------------

def test_ci_ref_roundtrip_and_malformed():
    # Case 18a: canonical bounded ref + fail-closed parsing.
    assert ci_ref(REPO, 1) == f"{REPO}#1"
    assert parse_ci_ref(ci_ref(REPO, 1)) == (REPO, 1)
    for bad in ("", "no-number", "a/b#", "a/b#0", "a/b#x", "a#b#1"):
        with pytest.raises(ValueError):
            parse_ci_ref(bad)


# ---------------------------------------------------------------------------
# Wait identity binding + enter
# ---------------------------------------------------------------------------

def test_enter_ci_wait_atomic_and_binds_identity(db_path):
    # Case 18: atomic RUNNING→WAITING_EXTERNAL + full identity persistence.
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    mgr = make_ci_manager(env)
    updated = _enter(mgr, env, jid, job)
    assert updated["primary_state"] == PrimaryState.WAITING_EXTERNAL.value
    assert updated["wait_kind"] == "CI"
    assert updated["owner_instance_id"] is None

    w = env.core._store.list_external_waits(jid)[0]
    assert w["kind"] == "CI"
    assert w["provider"] == "github"
    assert w["ref"] == f"{REPO}#1"
    assert w["expected_subject"] == SHA_A
    assert w["terminal_observed_at"] is None
    policy = load_policy(w)
    assert policy["expected_base"] == "main"
    assert policy["required_checks"] == ["ci"]
    assert policy["candidate_id"] == "cand:1"
    assert policy["expected_head_sha"] == SHA_A
    env.core.close()


def test_enter_ci_wait_rejects_unallowlisted_provider(db_path):
    # Case 19: a provider outside the allowlist registry can never be entered.
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    mgr = make_ci_manager(env)
    with pytest.raises(ValueError):
        _enter(mgr, env, jid, job, provider="evil")
    assert env.core._store.list_external_waits(jid) == []
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == "RUNNING"
    env.core.close()


def test_enter_ci_wait_rejects_bad_identity_fields(db_path):
    # Case 20: bad head SHA / base / required checks are rejected fail-closed.
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    mgr = make_ci_manager(env)
    for kw in (dict(expected_head_sha=""), dict(expected_base=""),
               dict(required_checks=("",)), dict(pr_number=0)):
        with pytest.raises(ValueError):
            _enter(mgr, env, jid, job, **kw)
    assert env.core._store.list_external_waits(jid) == []
    env.core.close()


def test_enter_ci_wait_rollback_on_bad_epoch(db_path):
    # Case 47: a failed transition rolls back the whole wait (no half-wait).
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    mgr = make_ci_manager(env)
    with pytest.raises(LeaseError):
        mgr.enter_ci_wait(jid, spec=ci_spec(), owner_instance_id="A",
                          lease_epoch=9999)
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == "RUNNING"
    assert env.core._store.list_external_waits(jid) == []
    env.core.close()


# ---------------------------------------------------------------------------
# No active LLM while waiting
# ---------------------------------------------------------------------------

def test_waiting_job_not_claimable_and_no_agent_dispatch(db_path):
    # Case 22: WAITING_EXTERNAL holds no LLM/role-run/scope; polling is
    # deterministic provider work only.
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=SHA_A, pr_state=PR_OPEN,
        checks=[make_ci_check("ci", conclusion=None, status="IN_PROGRESS")]))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job)

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "pending"
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.WAITING_EXTERNAL.value
    # No agent dispatch / role run / scope was created by polling.
    assert env.core._store.list_dispatches(task_id) == []
    env.core.close()


# ---------------------------------------------------------------------------
# Pending / backoff
# ---------------------------------------------------------------------------

def test_pending_backoff_no_wake(db_path):
    # Case 23: unchanged pending poll → backoff, no wake, no LLM.
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=SHA_A, pr_state=PR_OPEN,
        checks=[make_ci_check("ci", conclusion=None, status="IN_PROGRESS")]))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job)
    before = env.core._store.list_external_waits(jid)[0]

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert len(results) == 1
    assert results[0].outcome == "pending"

    after = env.core._store.list_external_waits(jid)[0]
    assert after["check_attempt"] == 1
    assert after["next_check_at"] > before["next_check_at"]
    assert after["terminal_observed_at"] is None
    assert after["last_observed_state"] == CiState.PENDING.value
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.WAITING_EXTERNAL.value
    env.core.close()


def test_unknown_aggregate_backoff_no_wake(db_path):
    # Case 44: UNKNOWN aggregate (missing required check) → backoff, no wake,
    # never SUCCESS.
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=SHA_A, pr_state=PR_OPEN,
        checks=[make_ci_check("ci", conclusion="SUCCESS")]))  # 'test' missing
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job, required_checks=("ci", "test"))

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "pending"
    assert results[0].reason == "unknown"
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.WAITING_EXTERNAL.value
    env.core.close()


def test_no_checks_configured_backoff_not_success(db_path):
    # Case 43: NO_CHECKS_CONFIGURED → backoff, no wake, persisted as such
    # (never a fabricated SUCCESS).
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(head_sha=SHA_A, pr_state=PR_OPEN))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job)

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "pending"
    assert results[0].reason == "no_checks_configured"
    w = env.core._store.list_external_waits(jid)[0]
    assert w["last_observed_state"] == CiState.NO_CHECKS_CONFIGURED.value
    assert w["terminal_observed_at"] is None
    env.core.close()


# ---------------------------------------------------------------------------
# Wake on terminal CI result (exactly once)
# ---------------------------------------------------------------------------

def test_ci_success_wakes_exactly_once(db_path):
    # Case 25/39: CI SUCCESS wakes exactly once; duplicate response deduped.
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=SHA_A, pr_state=PR_OPEN,
        checks=[make_ci_check("ci", conclusion="SUCCESS", check_id=7)],
        event_version=7))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job)

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert len(results) == 1
    assert results[0].outcome == "woke"
    assert results[0].queue_reason == QueueReason.WAIT_EVENT.value
    assert results[0].aggregate_state == CiState.SUCCESS.value
    assert results[0].error_class == "NONE"

    row = env.core._store.get_supervisor_job(jid)
    assert row["primary_state"] == PrimaryState.QUEUED.value
    assert row["wait_kind"] == "NONE"
    w = env.core._store.list_external_waits(jid)[0]
    assert w["terminal_observed_at"] is not None

    # Second poll: no due waits → no second wake.
    env.clock.advance(120)
    assert mgr.check_due_ci_waits() == []
    env.core.close()


def test_ci_failure_wakes_with_failing_evidence(db_path):
    # Case 26: CI FAILURE persists failing check identity + classification
    # BEFORE waking; error class EXTERNAL (not a Writer/Argent code failure).
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=SHA_A, pr_state=PR_OPEN,
        checks=[make_ci_check("unit-tests", conclusion="FAILURE",
                              run_ref="run/42", check_id=11)],
        event_version=11))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job, required_checks=("unit-tests",))

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "woke"
    assert results[0].aggregate_state == CiState.FAILURE.value
    assert results[0].error_class == "EXTERNAL"

    w = env.core._store.list_external_waits(jid)[0]
    evidence = json.loads(w["ci_evidence"])
    assert evidence["aggregate_state"] == "FAILURE"
    assert evidence["classification"] == "CODE_FAILURE"
    assert evidence["failing_checks"][0]["name"] == "unit-tests"
    assert evidence["failing_checks"][0]["run_ref"] == "run/42"
    assert w["terminal_observed_at"] is not None
    # The job is never set DONE/FAILED directly — it wakes to QUEUED.
    row = env.core._store.get_supervisor_job(jid)
    assert row["primary_state"] == PrimaryState.QUEUED.value
    assert row["terminal"] is None
    env.core.close()


def test_ci_cancelled_wakes_not_success(db_path):
    # Case 27: CANCELLED wakes (never SUCCESS) with EXTERNAL error class.
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=SHA_A, pr_state=PR_OPEN,
        checks=[make_ci_check("ci", conclusion="CANCELLED", check_id=3)],
        event_version=3))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job)

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "woke"
    assert results[0].aggregate_state == CiState.CANCELLED.value
    assert results[0].error_class == "EXTERNAL"
    w = env.core._store.list_external_waits(jid)[0]
    assert json.loads(w["ci_evidence"])["classification"] == "CANCELLED"
    env.core.close()


def test_ci_action_required_wakes_with_owner_required(db_path):
    # Case 28: ACTION_REQUIRED (provider requires Owner action) wakes with
    # OWNER_REQUIRED error class.
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=SHA_A, pr_state=PR_OPEN,
        checks=[make_ci_check("ci", conclusion="ACTION_REQUIRED", check_id=9)],
        event_version=9))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job)

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "woke"
    assert results[0].aggregate_state == CiState.ACTION_REQUIRED.value
    assert results[0].error_class == "OWNER_REQUIRED"
    env.core.close()


# ---------------------------------------------------------------------------
# Head-SHA binding
# ---------------------------------------------------------------------------

def test_head_sha_change_wakes_stale_and_invalidates_prior(db_path):
    # Case 29/50: PR #1 @ SHA X must never silently become PR #1 @ SHA Y —
    # a head change wakes STALE, persists stale evidence, and invalidates any
    # prior success evidence (no stale PASS reuse).
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    # A read that reports the PR's CURRENT head has moved to SHA_B.
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=SHA_B, pr_state=PR_OPEN,
        checks=[make_ci_check("ci", conclusion="SUCCESS", check_id=5)],
        event_version=5))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job)

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "woke"
    assert results[0].reason == "stale_head_change"
    assert results[0].aggregate_state == "STALE"

    w = env.core._store.list_external_waits(jid)[0]
    evidence = json.loads(w["ci_evidence"])
    assert evidence["transition"] == "stale_head_change"
    assert evidence["expected_head_sha"] == SHA_A
    assert evidence["head_sha"] == SHA_B
    assert w["terminal_observed_at"] is not None
    row = env.core._store.get_supervisor_job(jid)
    assert row["primary_state"] == PrimaryState.QUEUED.value
    env.core.close()


# ---------------------------------------------------------------------------
# Cross-repo / cross-PR isolation
# ---------------------------------------------------------------------------

def test_cross_repo_never_wakes(db_path):
    # Case 30: a read for Repo A can never bind to a wait for Repo B.
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        repository=REPO_B, pr_number=1, head_sha=SHA_A, pr_state=PR_OPEN,
        checks=[make_ci_check("ci", conclusion="SUCCESS")], event_version=2))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job)

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "malformed"
    assert results[0].reason == "wrong_identity"
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.WAITING_EXTERNAL.value
    env.core.close()


def test_cross_pr_never_wakes(db_path):
    # Case 31: a read for PR #1 can never bind to a wait for PR #2.
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        repository=REPO, pr_number=2, head_sha=SHA_A, pr_state=PR_OPEN,
        checks=[make_ci_check("ci", conclusion="SUCCESS")], event_version=2))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job)

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "malformed"
    assert results[0].reason == "wrong_identity"
    env.core.close()


# ---------------------------------------------------------------------------
# PR lifecycle
# ---------------------------------------------------------------------------

def test_pr_closed_wakes_unexpected_mutation(db_path):
    # Case 32: unexpected PR CLOSED wakes conservatively (no new action authz).
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=SHA_A, pr_state=PR_CLOSED, event_version=4))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job)

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "woke"
    assert results[0].reason == "pr_closed"
    assert results[0].aggregate_state == PR_CLOSED
    w = env.core._store.list_external_waits(jid)[0]
    assert json.loads(w["ci_evidence"])["transition"] == "pr_closed"
    env.core.close()


def test_pr_merged_wakes_distinct_unexpected(db_path):
    # Case 33: unexpected PR MERGED wakes with a DISTINCT reason (merged ≠
    # Argent-authorized merge).
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=SHA_A, pr_state=PR_MERGED, event_version=6))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job)

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "woke"
    assert results[0].reason == "pr_merged_unexpected"
    assert results[0].aggregate_state == PR_MERGED
    env.core.close()


# ---------------------------------------------------------------------------
# Required-check set materially changed
# ---------------------------------------------------------------------------

def test_required_check_set_materially_changed_wakes(db_path):
    # Case 34: a previously-observed required check vanishing wakes
    # conservatively (REQUIRED_CHECK_SET_CHANGED).
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    # First poll: both required checks present but 'test' still pending.
    adapter.script(REPO, 1, [
        make_ci_read(head_sha=SHA_A, pr_state=PR_OPEN, checks=[
            make_ci_check("ci", conclusion="SUCCESS", check_id=1),
            make_ci_check("test", conclusion=None, status="IN_PROGRESS",
                          check_id=2)], event_version=2),
        # Second poll: 'test' vanished entirely (material change).
        make_ci_read(head_sha=SHA_A, pr_state=PR_OPEN, checks=[
            make_ci_check("ci", conclusion="SUCCESS", check_id=1)],
            event_version=3),
    ])
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job, required_checks=("ci", "test"))

    env.clock.advance(61)
    r1 = mgr.check_due_ci_waits()
    assert r1[0].outcome == "pending"  # test pending → backoff

    env.clock.advance(2000)
    r2 = mgr.check_due_ci_waits()
    assert r2[0].outcome == "woke"
    assert r2[0].reason == "required_check_set_changed"
    env.core.close()


# ---------------------------------------------------------------------------
# Provider outage / rate-limit
# ---------------------------------------------------------------------------

def test_provider_unavailable_keeps_waiting(db_path):
    # Case 35: provider outage → keep WAITING_EXTERNAL, classify PROVIDER, no
    # wake, no LLM, no Writer failure.
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=None, pr_state=PR_OPEN,
        provider_error=PROVIDER_ERROR_UNAVAILABLE))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job)

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "provider_unavailable"
    row = env.core._store.get_supervisor_job(jid)
    assert row["primary_state"] == PrimaryState.WAITING_EXTERNAL.value
    assert row["terminal"] is None
    w = env.core._store.list_external_waits(jid)[0]
    assert w["last_observed_state"] == CiState.PROVIDER_UNAVAILABLE.value
    assert json.loads(w["ci_evidence"])["classification"] == "PROVIDER"
    env.core.close()


def test_rate_limit_keeps_waiting_respects_reset(db_path):
    # Case 36/37: rate limit → keep waiting, classify PROVIDER (never a code
    # failure), respect the reset/eligible time when observable.
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    reset = (env.clock() + __import__("datetime").timedelta(seconds=3600)) \
        .astimezone(__import__("datetime").timezone.utc).isoformat()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=None, pr_state=PR_OPEN,
        provider_error=PROVIDER_ERROR_RATE_LIMITED,
        rate_limit_reset_at=reset))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job)

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "rate_limited"
    assert results[0].aggregate_state == CiState.RATE_LIMITED.value
    row = env.core._store.get_supervisor_job(jid)
    assert row["primary_state"] == PrimaryState.WAITING_EXTERNAL.value
    assert row["terminal"] is None
    w = env.core._store.list_external_waits(jid)[0]
    assert w["last_observed_state"] == CiState.RATE_LIMITED.value
    assert json.loads(w["ci_evidence"])["classification"] == "PROVIDER"
    # next_check_at must be at least ~1h out (reset respected).
    assert w["next_check_at"] >= reset
    env.core.close()


def test_adapter_exception_backs_off_and_pass_continues(db_path):
    # Case 46: an adapter exception is contained per-wait and does not abort
    # the bounded pass (a second due wait still processes).
    env = make_env(db_path)
    job1, _ = make_running_job(env)
    job2, _ = make_running_job(env)
    jid1, jid2 = job1["id"], job2["id"]
    adapter = FakeCiAdapter()
    adapter.fail_next = RuntimeError("boom")
    adapter.set_sticky(REPO, 2, make_ci_read(
        pr_number=2, head_sha=SHA_A, pr_state=PR_OPEN,
        checks=[make_ci_check("ci", conclusion="SUCCESS", check_id=1)],
        event_version=1))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid1, job1, pr_number=1)
    _enter(mgr, env, jid2, job2, pr_number=2)

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    outcomes = {r.job_id: r.outcome for r in results}
    # First due wait hits the injected exception → contained backoff; the
    # second due wait still wakes.
    assert outcomes[jid1] == "adapter_error"
    assert outcomes[jid2] == "woke"
    env.core.close()


def test_malformed_read_backs_off_no_crash(db_path):
    # Case 45: an untrusted read with an out-of-closed-set conclusion is
    # rejected (malformed) without crashing the pass.
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=SHA_A, pr_state=PR_OPEN,
        checks=[make_ci_check("ci", conclusion="NOT_A_REAL_CONCLUSION")]))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job)

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "malformed"
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.WAITING_EXTERNAL.value
    env.core.close()


# ---------------------------------------------------------------------------
# Deadline
# ---------------------------------------------------------------------------

def test_deadline_wakes_with_external_error_class(db_path):
    # Case 38: CI deadline → wake with WAIT_DEADLINE + EXTERNAL, never DONE.
    from datetime import timedelta as _td
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    mgr = make_ci_manager(env)
    dl = env.clock() + _td(seconds=500)
    _enter(mgr, env, jid, job, deadline_at=dl.isoformat())

    env.clock.advance(600)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "woke"
    assert results[0].queue_reason == QueueReason.WAIT_DEADLINE.value
    row = env.core._store.get_supervisor_job(jid)
    assert row["primary_state"] == PrimaryState.QUEUED.value
    assert row["error_class"] == "EXTERNAL"
    assert row["terminal"] is None
    env.core.close()


# ---------------------------------------------------------------------------
# Crash / restart recovery
# ---------------------------------------------------------------------------

def test_wait_survives_restart_and_later_check_works(db_path):
    # Case 40/48: the wait (incl. ci_policy) survives a supervisor restart and
    # a later deterministic check still works after DB reopen.
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    mgr = make_ci_manager(env)
    _enter(mgr, env, jid, job)
    before = env.core._store.list_external_waits(jid)[0]
    env.core.close()

    core2 = Core(db_path, clock=env.clock)
    try:
        w = core2._store.list_external_waits(jid)[0]
        assert w["next_check_at"] == before["next_check_at"]
        assert load_policy(w)["required_checks"] == ["ci"]
        assert core2._store.get_supervisor_job(jid)["primary_state"] == \
            PrimaryState.WAITING_EXTERNAL.value

        adapter = FakeCiAdapter()
        adapter.set_sticky(REPO, 1, make_ci_read(
            head_sha=SHA_A, pr_state=PR_OPEN,
            checks=[make_ci_check("ci", conclusion=None, status="IN_PROGRESS")]))
        mgr2 = CiWaitManager(core2._store, adapters={"github": adapter},
                             clock=env.clock)
        env.clock.advance(61)
        results = mgr2.check_due_ci_waits()
        assert results[0].outcome == "pending"
    finally:
        core2.close()


def test_reopen_before_first_check_keeps_wait_due(db_path):
    # Case 41: crash BEFORE the first check — nothing was persisted from the
    # provider read, so the wait remains due and a later check re-reads
    # (side-effect-free read; no lost wake, no fabricated SUCCESS).
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    mgr = make_ci_manager(env)
    _enter(mgr, env, jid, job)
    env.core.close()

    core2 = Core(db_path, clock=env.clock)
    try:
        adapter = FakeCiAdapter()
        adapter.set_sticky(REPO, 1, make_ci_read(
            head_sha=SHA_A, pr_state=PR_OPEN,
            checks=[make_ci_check("ci", conclusion="SUCCESS", check_id=1)],
            event_version=1))
        mgr2 = CiWaitManager(core2._store, adapters={"github": adapter},
                             clock=env.clock)
        env.clock.advance(61)
        results = mgr2.check_due_ci_waits()
        assert results[0].outcome == "woke"
    finally:
        core2.close()


def test_wake_is_idempotent_no_duplicate_task(db_path):
    # Case 42: after persistence-before-wake, a repeated wake is a no-op (the
    # terminal flag + idempotent complete_wait_and_requeue prevent a second
    # logical wake / duplicate task).
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=SHA_A, pr_state=PR_OPEN,
        checks=[make_ci_check("ci", conclusion="SUCCESS", check_id=1)],
        event_version=1))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job)

    env.clock.advance(61)
    assert mgr.check_due_ci_waits()[0].outcome == "woke"
    env.clock.advance(120)
    assert mgr.check_due_ci_waits() == []
    # The job was queued exactly once (single wait row, terminal).
    waits = env.core._store.list_external_waits(jid)
    assert len(waits) == 1
    assert waits[0]["terminal_observed_at"] is not None
    env.core.close()


# ---------------------------------------------------------------------------
# Unknown requirement set ⇒ conservative
# ---------------------------------------------------------------------------

def test_unknown_requirement_set_conservative_through_manager(db_path):
    # Case 49: an UNKNOWN requirement set is conservative through the manager
    # (never a fabricated SUCCESS wake) — backoff UNKNOWN.
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=SHA_A, pr_state=PR_OPEN,
        checks=[make_ci_check("ci", conclusion="SUCCESS", check_id=1)],
        event_version=1))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job, required_checks=None)

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "pending"
    assert results[0].reason == "unknown"
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.WAITING_EXTERNAL.value
    env.core.close()


# ---------------------------------------------------------------------------
# HIGH-1: positive identity binding (head SHA / base ref / OPEN lifecycle)
# ---------------------------------------------------------------------------

def test_wrong_base_never_wakes_success(db_path):
    # HIGH-1: observed base != persisted expected base ⇒ STALE wake (never a
    # SUCCESS wake even when the bound-SHA check passes).
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=SHA_A, base_ref="release", pr_state=PR_OPEN,
        checks=[make_ci_check("ci", conclusion="SUCCESS", check_id=1)],
        event_version=1))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job)

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "woke"
    assert results[0].reason == "base_ref_changed"
    assert results[0].aggregate_state == "STALE"
    assert results[0].aggregate_state != CiState.SUCCESS.value
    w = env.core._store.list_external_waits(jid)[0]
    assert json.loads(w["ci_evidence"])["transition"] == "base_ref_changed"
    env.core.close()


def test_null_head_never_wakes_success(db_path):
    # HIGH-1: a clean read with a NULL head ⇒ conservative backoff
    # (missing_head), never a SUCCESS wake.
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=None, pr_state=PR_OPEN,
        checks=[make_ci_check("ci", conclusion="SUCCESS", check_id=1)],
        event_version=1))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job)

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "pending"
    assert results[0].reason == "missing_head"
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.WAITING_EXTERNAL.value
    env.core.close()


def test_unknown_pr_state_never_wakes_success(db_path):
    # HIGH-1: PR_UNKNOWN lifecycle ⇒ conservative backoff, never aggregation.
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=SHA_A, pr_state=PR_UNKNOWN,
        checks=[make_ci_check("ci", conclusion="SUCCESS", check_id=1)],
        event_version=1))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job)

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "pending"
    assert results[0].reason == "unknown_pr_state"
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.WAITING_EXTERNAL.value
    env.core.close()


def test_missing_base_never_wakes_success(db_path):
    # HIGH-1: a clean read with a missing base ⇒ base_ref_changed STALE wake
    # (never SUCCESS).
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=SHA_A, base_ref=None, pr_state=PR_OPEN,
        checks=[make_ci_check("ci", conclusion="SUCCESS", check_id=1)],
        event_version=1))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job)

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "woke"
    assert results[0].reason == "base_ref_changed"
    assert results[0].aggregate_state == "STALE"
    env.core.close()


# ---------------------------------------------------------------------------
# HIGH-3: empty-required conservative semantics (manager-level)
# ---------------------------------------------------------------------------

def test_empty_required_fast_success_not_success(db_path):
    # HIGH-3: an empty required set + a single fast SUCCESS check ⇒ NOT a
    # SUCCESS wake (conservative UNKNOWN; partial universe).
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=SHA_A, pr_state=PR_OPEN,
        checks=[make_ci_check("ci", conclusion="SUCCESS", check_id=1)],
        event_version=1))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job, required_checks=())

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "pending"
    assert results[0].aggregate_state == CiState.UNKNOWN.value
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.WAITING_EXTERNAL.value
    env.core.close()


def test_failure_with_empty_required_stores_failing_check(db_path):
    # HIGH-3: with an empty required policy a real failure is still detected and
    # its failing-check identity persisted (derived from observed conclusions).
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=SHA_A, pr_state=PR_OPEN,
        checks=[make_ci_check("unit-tests", conclusion="FAILURE", check_id=3)],
        event_version=3))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job, required_checks=())

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "woke"
    assert results[0].aggregate_state == CiState.FAILURE.value
    w = env.core._store.list_external_waits(jid)[0]
    ev = json.loads(w["ci_evidence"])
    assert ev["failing_checks"][0]["name"] == "unit-tests"
    env.core.close()


# ---------------------------------------------------------------------------
# LOW-6: contradictory check conclusion/status
# ---------------------------------------------------------------------------

def test_contradictory_in_progress_success_is_malformed(db_path):
    # LOW-6: a check reporting IN_PROGRESS and SUCCESS simultaneously is
    # contradictory ⇒ malformed, never aggregated to SUCCESS.
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=SHA_A, pr_state=PR_OPEN,
        checks=[make_ci_check("ci", conclusion="SUCCESS", status="IN_PROGRESS")]))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job)

    env.clock.advance(61)
    results = mgr.check_due_ci_waits()
    assert results[0].outcome == "malformed"
    assert results[0].reason == "bad_check_contradictory"
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.WAITING_EXTERNAL.value
    env.core.close()


# ---------------------------------------------------------------------------
# HIGH-5: wait entry refuses a job with active process evidence
# ---------------------------------------------------------------------------

def test_enter_ci_wait_refuses_with_active_process(db_path):
    # HIGH-5: a job WITH an active process must be refused CI wait entry (never
    # silently parked in WAITING_EXTERNAL with a live model/process).
    from argent_core.process_registry import ProcessIdentity, ProcessRegistry
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    ProcessRegistry(env.core._store).register(
        job_id=jid, dispatch_id=None,
        identity=ProcessIdentity(boot_id="boot-1", pid=500,
                                 process_start_ticks=9),
    )
    mgr = make_ci_manager(env)
    with pytest.raises(ValueError):
        _enter(mgr, env, jid, job)
    assert env.core._store.list_external_waits(jid) == []
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == "RUNNING"
    env.core.close()


# ---------------------------------------------------------------------------
# HIGH-2(b): terminal immutability + stale finalizer
# ---------------------------------------------------------------------------

def test_late_pending_response_cannot_corrupt_terminal_evidence(db_path):
    # HIGH-2(b): terminal evidence is immutable — a late pending response
    # (stale backoff) cannot overwrite a terminal SUCCESS wake's evidence.
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    adapter = FakeCiAdapter()
    adapter.set_sticky(REPO, 1, make_ci_read(
        head_sha=SHA_A, pr_state=PR_OPEN,
        checks=[make_ci_check("ci", conclusion="SUCCESS", check_id=1)],
        event_version=1))
    mgr = make_ci_manager(env, adapter)
    _enter(mgr, env, jid, job)

    env.clock.advance(61)
    assert mgr.check_due_ci_waits()[0].outcome == "woke"
    w = env.core._store.list_external_waits(jid)[0]
    before_ev = w["ci_evidence"]
    before_state = w["last_observed_state"]
    assert before_state == CiState.SUCCESS.value

    late_evidence = json.dumps({"aggregate_state": "PENDING",
                                "transition": "ci_pending"})
    mgr._backoff(w, mgr._now_iso(), state=CiState.PENDING.value,
                 outcome="pending", reason="pending", evidence=late_evidence)
    w2 = env.core._store.list_external_waits(jid)[0]
    assert w2["ci_evidence"] == before_ev
    assert w2["last_observed_state"] == before_state
    env.core.close()


def test_stale_finalizer_cannot_win(db_path):
    # HIGH-2(b): a stale instance (lost the singleton fence) cannot finalize
    # (requeue) a CI wait — only the current fence holder can.
    from g1_helpers import seed_owner
    env = make_env(db_path)
    job, task_id = make_running_job(env)
    jid = job["id"]
    mgr = make_ci_manager(env)
    _enter(mgr, env, jid, job)
    w = env.core._store.list_external_waits(jid)[0]
    seed_owner(env.core._store, instance_id="current")

    # A stale instance's finalizer (wrong instance_id) must NOT win.
    res = env.core._store.complete_wait_and_requeue(
        w["wait_id"], queue_reason=QueueReason.WAIT_EVENT.value,
        error_class="NONE", observed_state="SUCCESS",
        expected_instance_id="stale")
    assert res is None
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.WAITING_EXTERNAL.value

    # The current holder CAN finalize.
    res2 = env.core._store.complete_wait_and_requeue(
        w["wait_id"], queue_reason=QueueReason.WAIT_EVENT.value,
        error_class="NONE", observed_state="SUCCESS",
        expected_instance_id="current")
    assert res2 is not None
    assert env.core._store.get_supervisor_job(jid)["primary_state"] == \
        PrimaryState.QUEUED.value
    env.core.close()
