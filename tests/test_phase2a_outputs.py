"""Phase 2A output-validation tests (SPEC V2 5, V2.1 15.11/15.12)."""

import pytest

from argent_core import OutputValidationError, Role
from argent_core.outputs import validate_role_output

from mock_runtime import (
    analyst_output,
    build_output,
    implementer_output,
    lead_output,
    qa_output,
    reviewer_output,
)


def test_all_roles_valid_outputs_pass():
    builders = {
        Role.LEAD: lead_output,
        Role.ANALYST: analyst_output,
        Role.IMPLEMENTER: implementer_output,
        Role.QA: qa_output,
        Role.REVIEWER: reviewer_output,
    }
    for role, builder in builders.items():
        out = builder("t", "d")
        assert validate_role_output(role, out) is out


def test_missing_required_field_fails():
    out = lead_output("t", "d")
    del out["decision"]
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.LEAD, out)


def test_wrong_type_fails():
    out = lead_output("t", "d")
    out["findings"] = "not-a-list"
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.LEAD, out)


def test_unknown_top_level_field_fails():
    out = lead_output("t", "d")
    out["sneaky_extra"] = "nope"
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.LEAD, out)


def test_bad_status_fails():
    out = lead_output("t", "d", status="weird")
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.LEAD, out)


def test_confidence_out_of_range_fails():
    out = lead_output("t", "d", confidence=1.5)
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.LEAD, out)
    out = lead_output("t", "d", confidence=-0.1)
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.LEAD, out)


def test_bad_decision_fails():
    out = lead_output("t", "d", decision="nope")
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.LEAD, out)


def test_role_envelope_mismatch_fails():
    out = lead_output("t", "d", role="analyst")
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.LEAD, out)


def test_unknown_role_fails():
    with pytest.raises(OutputValidationError):
        validate_role_output("bogus", lead_output("t", "d"))


def test_non_dict_fails():
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.LEAD, ["not", "a", "dict"])


def test_oversized_output_rejected():
    out = lead_output("t", "d")
    out["own_assessment"] = "x" * (256 * 1024 + 1)
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.LEAD, out)


def test_deep_output_rejected():
    out = lead_output("t", "d")
    nested = {}
    cur = nested
    for _ in range(20):
        cur["child"] = {}
        cur = cur["child"]
    out["findings"] = [nested]
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.LEAD, out)


def test_long_string_rejected():
    out = lead_output("t", "d")
    out["proposal"] = "y" * 9000
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.LEAD, out)


def test_denylisted_term_rejected():
    out = lead_output("t", "d")
    out["own_assessment"] = "the reasoning is secret"
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.LEAD, out)


def test_denylisted_key_rejected():
    out = lead_output("t", "d")
    out["findings"] = [{"severity": "low", "password": "x"}]
    with pytest.raises(OutputValidationError):
        validate_role_output(Role.LEAD, out)
