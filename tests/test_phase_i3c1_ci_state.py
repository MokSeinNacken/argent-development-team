"""Phase I3-C1 — normalized CI state model + deterministic aggregation.

Covers the CiState closed set, the hard rules (NO_CHECKS_CONFIGURED != SUCCESS,
UNKNOWN != SUCCESS, PROVIDER_UNAVAILABLE != CODE_FAILURE, RATE_LIMITED !=
CODE_FAILURE, CANCELLED != SUCCESS), deterministic check aggregation over a
trusted required/optional policy, and the partial failure classification.

Pure functions only — no store, no LLM, no network.
"""

from __future__ import annotations

from argent_core.ci_external_wait import (
    ALL_CI_FAILURE_CLASSES,
    ALL_CI_STATES,
    CI_FAIL_CANCELLED,
    CI_FAIL_CODE,
    CI_FAIL_INFRA,
    CI_FAIL_PROVIDER,
    CI_FAIL_TIMEOUT,
    CI_FAIL_UNKNOWN,
    CHECK_CONCLUSIONS,
    CHECK_STATUSES,
    CiState,
    aggregate_ci_state,
    classify_ci_failure,
    make_ci_check,
)

REQ = ("ci", "test")
OPT = ("deploy",)


def agg(checks, required=REQ, optional=OPT):
    return aggregate_ci_state(checks, required=required, optional=optional)


# ---------------------------------------------------------------------------
# Closed-set model
# ---------------------------------------------------------------------------

def test_ci_state_closed_set_exact():
    # Case 1: the normalized CI state model is a closed set of exactly 12
    # states, including the terminal and conservative states.
    assert ALL_CI_STATES == {
        "PENDING", "SUCCESS", "FAILURE", "CANCELLED", "TIMED_OUT",
        "ACTION_REQUIRED", "NEUTRAL", "SKIPPED", "NO_CHECKS_CONFIGURED",
        "PROVIDER_UNAVAILABLE", "RATE_LIMITED", "UNKNOWN",
    }
    assert len(ALL_CI_STATES) == 12


def test_check_conclusion_and_status_closed_sets():
    # Case 1b: individual-check conclusions/status are closed sets.
    assert "SUCCESS" in CHECK_CONCLUSIONS
    assert "NEUTRAL" in CHECK_CONCLUSIONS
    assert "SKIPPED" in CHECK_CONCLUSIONS
    assert "STALE" in CHECK_CONCLUSIONS
    assert CHECK_STATUSES >= {"QUEUED", "IN_PROGRESS", "COMPLETED", "PENDING"}


def test_failure_classification_closed_set():
    assert ALL_CI_FAILURE_CLASSES == {
        CI_FAIL_CODE, CI_FAIL_INFRA, CI_FAIL_CANCELLED, CI_FAIL_TIMEOUT,
        CI_FAIL_PROVIDER, CI_FAIL_UNKNOWN,
    }


# ---------------------------------------------------------------------------
# Hard rules
# ---------------------------------------------------------------------------

def test_no_checks_configured_is_not_success():
    # Case 2: NO_CHECKS_CONFIGURED != SUCCESS.
    assert agg([], required=REQ) == CiState.NO_CHECKS_CONFIGURED
    assert agg([], required=()) == CiState.NO_CHECKS_CONFIGURED
    assert agg([], required=REQ) != CiState.SUCCESS


def test_unknown_requirement_set_is_not_success():
    # Case 3: UNKNOWN != SUCCESS; an unknown requirement set is conservative.
    checks = [make_ci_check("ci", conclusion="SUCCESS")]
    assert aggregate_ci_state(checks, required=None) == CiState.UNKNOWN
    assert aggregate_ci_state(checks, required=None) != CiState.SUCCESS


def test_provider_unavailable_is_not_code_failure():
    # Case 4: PROVIDER_UNAVAILABLE != CODE_FAILURE.
    cls = classify_ci_failure(CiState.PROVIDER_UNAVAILABLE.value,
                              provider_error="unavailable")
    assert cls == CI_FAIL_PROVIDER
    assert cls != CI_FAIL_CODE


def test_rate_limited_is_not_code_failure():
    # Case 5: RATE_LIMITED != CODE_FAILURE.
    cls = classify_ci_failure(CiState.RATE_LIMITED.value,
                              provider_error="rate_limited")
    assert cls == CI_FAIL_PROVIDER
    assert cls != CI_FAIL_CODE


def test_cancelled_is_not_success():
    # Case 6: CANCELLED != SUCCESS (a cancelled required check is a terminal
    # non-success, distinct from FAILURE).
    checks = [make_ci_check("ci", conclusion="CANCELLED"),
              make_ci_check("test", conclusion="SUCCESS")]
    assert agg(checks) == CiState.CANCELLED
    assert agg(checks) != CiState.SUCCESS


# ---------------------------------------------------------------------------
# Aggregation over a trusted required/optional policy
# ---------------------------------------------------------------------------

def test_all_required_success_is_success():
    # Case 7.
    checks = [make_ci_check("ci", conclusion="SUCCESS"),
              make_ci_check("test", conclusion="SUCCESS"),
              make_ci_check("deploy", conclusion="FAILURE")]  # optional
    assert agg(checks) == CiState.SUCCESS


def test_any_required_failure_is_failure():
    # Case 8.
    checks = [make_ci_check("ci", conclusion="FAILURE"),
              make_ci_check("test", conclusion="SUCCESS")]
    assert agg(checks) == CiState.FAILURE


def test_any_required_pending_is_pending():
    # Case 9: a required check still running ⇒ PENDING (never SUCCESS).
    checks = [make_ci_check("ci", conclusion=None, status="IN_PROGRESS"),
              make_ci_check("test", conclusion="SUCCESS")]
    assert agg(checks) == CiState.PENDING
    assert agg(checks) != CiState.SUCCESS


def test_missing_required_is_unknown():
    # Case 10: a required check absent ⇒ conservative UNKNOWN (not success).
    checks = [make_ci_check("ci", conclusion="SUCCESS")]  # 'test' missing
    assert agg(checks) == CiState.UNKNOWN
    assert agg(checks) != CiState.SUCCESS


def test_required_neutral_or_skipped_is_unknown():
    # Case 11: a required check reporting NEUTRAL/SKIPPED is not a clean
    # success and not a clean failure ⇒ conservative UNKNOWN.
    for concl in ("NEUTRAL", "SKIPPED"):
        checks = [make_ci_check("ci", conclusion=concl),
                  make_ci_check("test", conclusion="SUCCESS")]
        assert agg(checks) == CiState.UNKNOWN
        assert agg(checks) != CiState.SUCCESS


def test_optional_failure_never_fails_aggregate():
    # Case 12: optional/skipped checks follow explicit policy (informational).
    checks = [make_ci_check("ci", conclusion="SUCCESS"),
              make_ci_check("test", conclusion="SUCCESS"),
              make_ci_check("deploy", conclusion="FAILURE")]
    assert agg(checks) == CiState.SUCCESS


def test_required_empty_and_no_checks():
    # Case 13: explicit empty required set + zero checks ⇒ NO_CHECKS_CONFIGURED.
    assert aggregate_ci_state([], required=()) == CiState.NO_CHECKS_CONFIGURED


def test_required_empty_and_all_success():
    # Case 14 (HIGH-3 FIX): explicit empty required set + observed successful
    # checks ⇒ conservative UNKNOWN — the check universe is not provably
    # complete (single unpaginated request, no branch-protection metadata), so
    # a partial set of passing checks can NEVER be aggregated to SUCCESS.
    checks = [make_ci_check("ci", conclusion="SUCCESS")]
    assert aggregate_ci_state(checks, required=()) == CiState.UNKNOWN
    assert aggregate_ci_state(checks, required=()) != CiState.SUCCESS


def test_required_empty_but_observed_failure_is_failure():
    # Case 15: even with an empty required set, an observed failing check is
    # not reported as a clean success (conservative — a real failure IS a safe
    # positive signal).
    checks = [make_ci_check("ci", conclusion="FAILURE")]
    assert aggregate_ci_state(checks, required=()) == CiState.FAILURE


def test_required_empty_multiple_success_not_success():
    # HIGH-3: several observed SUCCESS checks with an empty required set still
    # cannot prove green (partial universe) ⇒ UNKNOWN, never SUCCESS.
    checks = [make_ci_check("ci", conclusion="SUCCESS"),
              make_ci_check("test", conclusion="SUCCESS")]
    assert aggregate_ci_state(checks, required=()) == CiState.UNKNOWN


def test_failure_beats_missing_required():
    # HIGH-4: a required check with a terminal non-success conclusion wins over
    # a missing required check (FAILURE, not UNKNOWN).
    checks = [make_ci_check("ci", conclusion="FAILURE")]  # 'test' missing
    assert aggregate_ci_state(checks, required=("ci", "test")) == CiState.FAILURE


def test_cancelled_beats_missing_required():
    checks = [make_ci_check("ci", conclusion="CANCELLED")]
    assert aggregate_ci_state(checks, required=("ci", "test")) == CiState.CANCELLED


def test_timed_out_beats_missing_required():
    checks = [make_ci_check("ci", conclusion="TIMED_OUT")]
    assert aggregate_ci_state(checks, required=("ci", "test")) == CiState.TIMED_OUT


def test_action_required_beats_missing_required():
    checks = [make_ci_check("ci", conclusion="ACTION_REQUIRED")]
    assert aggregate_ci_state(
        checks, required=("ci", "test")) == CiState.ACTION_REQUIRED


def test_startup_failure_beats_missing_required():
    checks = [make_ci_check("ci", conclusion="STARTUP_FAILURE")]
    assert aggregate_ci_state(checks, required=("ci", "test")) == CiState.FAILURE


# ---------------------------------------------------------------------------
# Partial failure classification
# ---------------------------------------------------------------------------

def test_classify_timed_out_and_cancelled():
    # Case 16: TIMED_OUT → TIMEOUT; CANCELLED → CANCELLED.
    assert classify_ci_failure(CiState.TIMED_OUT.value) == CI_FAIL_TIMEOUT
    assert classify_ci_failure(CiState.CANCELLED.value) == CI_FAIL_CANCELLED
    assert classify_ci_failure(
        CiState.FAILURE.value,
        failing=[make_ci_check("ci", conclusion="TIMED_OUT")],
    ) == CI_FAIL_TIMEOUT
    assert classify_ci_failure(
        CiState.FAILURE.value,
        failing=[make_ci_check("ci", conclusion="CANCELLED")],
    ) == CI_FAIL_CANCELLED


def test_classify_infra_vs_code_vs_unknown():
    # Case 17: deterministic partial classification by bounded name marker.
    assert classify_ci_failure(
        CiState.FAILURE.value,
        failing=[make_ci_check("deploy-preview", conclusion="FAILURE")],
    ) == CI_FAIL_INFRA
    assert classify_ci_failure(
        CiState.FAILURE.value,
        failing=[make_ci_check("unit-tests", conclusion="FAILURE")],
    ) == CI_FAIL_CODE
    assert classify_ci_failure(
        CiState.FAILURE.value,
        failing=[make_ci_check("snyk-scan", conclusion="FAILURE")],
    ) == CI_FAIL_UNKNOWN


def test_classify_provider_error_precedence():
    # A provider error dominates any check-level signal (never a code failure).
    assert classify_ci_failure(
        CiState.FAILURE.value,
        failing=[make_ci_check("unit-tests", conclusion="FAILURE")],
        provider_error="unavailable",
    ) == CI_FAIL_PROVIDER
