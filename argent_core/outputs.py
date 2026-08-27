"""Structured role-output validation (SPEC V2 chapter 5, fail-closed).

Agent outputs are UNTRUSTED DATA.  ``validate_role_output`` enforces, for each
role, a strict allow-list of top-level fields plus per-field types, value
enums and privacy/limits.  Any violation raises :class:`OutputValidationError`
(malformed -> the dispatch is rejected, never consumed).

Limits (SPEC V2 15.11): result <= 256 KB serialized, nesting depth <= 12,
any string <= 8 KB.  A deny-list scan (``events.PRIVACY_DENYLIST``) runs over
all fields (keys and values, recursively); a hit is a malformed result.
"""

from __future__ import annotations

import json
from typing import Any

from . import events
from .models import OutputValidationError, Role

MAX_RESULT_BYTES = 256 * 1024
MAX_DEPTH = 12
MAX_STRING_LEN = 8192

_STATUS_VALUES = frozenset({"ok", "findings", "blocked"})
_DECISION_VALUES = frozenset({"accept", "rework", "cancel", "request_owner_gate"})
_SEVERITY_VALUES = frozenset({"low", "medium", "high", "critical"})
_TEST_RESULT_VALUES = frozenset({"passed", "failed", "error"})
_FINDING_ALLOWLIST = frozenset({"severity", "description", "title", "id"})
_TEST_DICT_ALLOWLIST = frozenset({"name", "result"})
_SEC_ARCH_DICT_ALLOWLIST = frozenset({"severity", "description"})

# Nested fields whose elements must all be plain strings (SPEC V2.2 16.6).
_STRING_LIST_FIELDS: frozenset[str] = frozenset(
    {
        "accepted_findings",
        "rejected_findings",
        "alternatives",
        "concerns",
        "blockers",
        "evidence_refs",
        "failures",
        "regressions",
        "tests_run",
    }
)

# Common mandatory fields for every role.
_COMMON_FIELDS: dict[str, Any] = {
    "role": str,
    "task_id": str,
    "dispatch_id": str,
    "status": str,
    "findings": list,
    "own_assessment": str,
    "concerns": list,
    "proposal": str,
    "alternatives": list,
    "confidence": (int, float),
    "blockers": list,
    "requested_next_state": str,
}

_ROLE_FIELDS: dict[Role, dict[str, Any]] = {
    Role.LEAD: {
        "decision": str,
        "accepted_findings": list,
        "rejected_findings": list,
        "rationale": str,
    },
    Role.ANALYST: {
        "reproduction": str,
        "root_cause": str,
        "evidence_refs": list,
    },
    Role.IMPLEMENTER: {
        "changed_files": list,
        "implementation_summary": str,
        "tests_run": list,
    },
    Role.QA: {
        "tests": list,
        "failures": list,
        "regressions": list,
        "coverage_concerns": list,
    },
    Role.REVIEWER: {
        "severity": str,
        "security_findings": list,
        "architecture_findings": list,
        "recommendation": str,
    },
}

# Optional allow-listed extras (not required, but accepted if present).
_OPTIONAL_FIELDS: dict[Role, dict[str, Any]] = {
    Role.LEAD: {"rework_include_reviewer": bool},
    Role.ANALYST: {},
    Role.IMPLEMENTER: {},
    Role.QA: {},
    Role.REVIEWER: {},
}


def _depth(obj: Any, d: int = 1) -> int:
    if isinstance(obj, dict):
        if not obj:
            return d
        return max(_depth(v, d + 1) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        if not obj:
            return d
        return max(_depth(v, d + 1) for v in obj)
    return d


def _max_string_len(obj: Any) -> int:
    mx = 0
    if isinstance(obj, str):
        return len(obj)
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                mx = max(mx, len(k))
            mx = max(mx, _max_string_len(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            mx = max(mx, _max_string_len(v))
    return mx


def _is_instance(value: Any, expected: Any) -> bool:
    return isinstance(value, expected)


def _check_types(result: dict, spec: dict, label: str) -> None:
    for field, expected in spec.items():
        if field not in result:
            # Optional fields may be absent; required fields are checked
            # separately (missing required -> OutputValidationError earlier).
            continue
        value = result[field]
        if not _is_instance(value, expected):
            raise OutputValidationError(
                f"{label}.{field} must be {expected!r}, got {type(value).__name__!r}"
            )


def _validate_string_list(result: dict, field: str, label: str) -> None:
    values = result.get(field)
    if values is None:
        return
    for i, v in enumerate(values):
        if not isinstance(v, str):
            raise OutputValidationError(
                f"{label}.{field}[{i}] must be a str, got {type(v).__name__!r}"
            )


def _validate_findings(result: dict, label: str) -> None:
    findings = result.get("findings")
    if findings is None:
        return
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            raise OutputValidationError(
                f"{label}.findings[{i}] must be a dict, got {type(f).__name__!r}"
            )
        unknown = [k for k in f if k not in _FINDING_ALLOWLIST]
        if unknown:
            raise OutputValidationError(
                f"{label}.findings[{i}] has unknown field(s): "
                f"{', '.join(sorted(map(str, unknown)))}"
            )
        # V2.3 (G2): severity is REQUIRED and must be a valid enum value.
        if "severity" not in f:
            raise OutputValidationError(
                f"{label}.findings[{i}].severity is required"
            )
        if f["severity"] not in _SEVERITY_VALUES:
            raise OutputValidationError(
                f"{label}.findings[{i}].severity must be one of "
                f"{sorted(_SEVERITY_VALUES)}, got {f['severity']!r}"
            )
        # V2.3 (G2): at least one of description|title (str) is REQUIRED.
        for k in ("description", "title", "id"):
            if k in f and not isinstance(f[k], str):
                raise OutputValidationError(
                    f"{label}.findings[{i}].{k} must be a str"
                )
        if "description" not in f and "title" not in f:
            raise OutputValidationError(
                f"{label}.findings[{i}] must include 'description' or 'title'"
            )


def _validate_tests(result: dict, label: str) -> None:
    tests = result.get("tests")
    if tests is None:
        return
    for i, t in enumerate(tests):
        if isinstance(t, str):
            continue
        if isinstance(t, dict):
            unknown = [k for k in t if k not in _TEST_DICT_ALLOWLIST]
            if unknown:
                raise OutputValidationError(
                    f"{label}.tests[{i}] has unknown field(s): "
                    f"{', '.join(sorted(map(str, unknown)))}"
                )
            if "name" not in t or not isinstance(t["name"], str):
                raise OutputValidationError(
                    f"{label}.tests[{i}].name must be a str"
                )
            if "result" not in t or t["result"] not in _TEST_RESULT_VALUES:
                raise OutputValidationError(
                    f"{label}.tests[{i}].result must be one of "
                    f"{sorted(_TEST_RESULT_VALUES)}, got {t.get('result')!r}"
                )
        else:
            raise OutputValidationError(
                f"{label}.tests[{i}] must be a str or dict, "
                f"got {type(t).__name__!r}"
            )


def _validate_sec_arch_findings(result: dict, field: str, label: str) -> None:
    values = result.get(field)
    if values is None:
        return
    for i, v in enumerate(values):
        if isinstance(v, str):
            continue
        if isinstance(v, dict):
            # V2.3 (G2): empty dicts are rejected.
            if not v:
                raise OutputValidationError(
                    f"{label}.{field}[{i}] must not be empty"
                )
            unknown = [k for k in v if k not in _SEC_ARCH_DICT_ALLOWLIST]
            if unknown:
                raise OutputValidationError(
                    f"{label}.{field}[{i}] has unknown field(s): "
                    f"{', '.join(sorted(map(str, unknown)))}"
                )
            # V2.3 (G2): severity AND description are REQUIRED.
            if "severity" not in v:
                raise OutputValidationError(
                    f"{label}.{field}[{i}].severity is required"
                )
            if v["severity"] not in _SEVERITY_VALUES:
                raise OutputValidationError(
                    f"{label}.{field}[{i}].severity must be one of "
                    f"{sorted(_SEVERITY_VALUES)}, got {v['severity']!r}"
                )
            if "description" not in v:
                raise OutputValidationError(
                    f"{label}.{field}[{i}].description is required"
                )
            if not isinstance(v["description"], str):
                raise OutputValidationError(
                    f"{label}.{field}[{i}].description must be a str"
                )
        else:
            raise OutputValidationError(
                f"{label}.{field}[{i}] must be a str or dict, "
                f"got {type(v).__name__!r}"
            )


def validate_role_output(role: Role, result: dict) -> dict:
    """Validate a structured output for ``role`` (fail-closed).

    Returns the (unmodified) ``result`` dict on success; raises
    :class:`OutputValidationError` otherwise.
    """
    if not isinstance(role, Role):
        raise OutputValidationError(f"unknown role {role!r}")

    if not isinstance(result, dict):
        raise OutputValidationError("output must be a dict")

    # Limits first (cheap, no schema interpretation).
    try:
        serialized = json.dumps(result, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise OutputValidationError(f"output is not JSON-serializable: {exc}") from exc
    if len(serialized.encode("utf-8")) > MAX_RESULT_BYTES:
        raise OutputValidationError("output exceeds 256 KB limit")
    if _depth(result) > MAX_DEPTH:
        raise OutputValidationError("output exceeds maximum nesting depth")
    if _max_string_len(result) > MAX_STRING_LEN:
        raise OutputValidationError("output contains a string longer than 8 KB")

    # Strict top-level allow-list (SPEC V2 15.11).
    allowed: set[str] = set(_COMMON_FIELDS) | set(_ROLE_FIELDS[role]) | set(
        _OPTIONAL_FIELDS[role]
    )
    unknown = [k for k in result if k not in allowed]
    if unknown:
        raise OutputValidationError(
            f"unknown top-level field(s): {', '.join(sorted(unknown))}"
        )

    # Required fields.
    required = set(_COMMON_FIELDS) | set(_ROLE_FIELDS[role])
    missing = [k for k in required if k not in result]
    if missing:
        raise OutputValidationError(f"missing required field(s): {', '.join(missing)}")

    _check_types(result, _COMMON_FIELDS, "output")
    _check_types(result, _ROLE_FIELDS[role], "output")
    _check_types(result, _OPTIONAL_FIELDS[role], "output")

    # Envelope identity.
    if result["role"] != role.value:
        raise OutputValidationError(
            f"role envelope mismatch: {result['role']!r} != {role.value!r}"
        )
    if result["status"] not in _STATUS_VALUES:
        raise OutputValidationError(
            f"status must be one of {sorted(_STATUS_VALUES)}, got {result['status']!r}"
        )
    conf = result["confidence"]
    if isinstance(conf, bool) or not (0 <= conf <= 1):
        raise OutputValidationError(f"confidence must be 0..1, got {conf!r}")

    if role is Role.LEAD:
        if result["decision"] not in _DECISION_VALUES:
            raise OutputValidationError(
                f"decision must be one of {sorted(_DECISION_VALUES)}, "
                f"got {result['decision']!r}"
            )

    # V2.2 (F6): validate nested element schemas fully BEFORE the CAS, so a
    # malformed nested field is a REJECTED (malformed_output) — never a crash
    # after consumption.
    _validate_findings(result, "output")
    for field in _STRING_LIST_FIELDS:
        if field in result:
            _validate_string_list(result, field, "output")
    if "tests" in result:
        _validate_tests(result, "output")
    if "security_findings" in result:
        _validate_sec_arch_findings(result, "security_findings", "output")
    if "architecture_findings" in result:
        _validate_sec_arch_findings(result, "architecture_findings", "output")
    if role is Role.REVIEWER and result["severity"] not in _SEVERITY_VALUES:
        raise OutputValidationError(
            f"severity must be one of {sorted(_SEVERITY_VALUES)}, "
            f"got {result['severity']!r}"
        )

    # Deny-list scan over the whole output (keys + values, recursive).
    hit = events.scan_value_for_denylist(result)
    if hit is not None:
        raise OutputValidationError(
            f"output contains deny-listed term {hit!r}"
        )

    return result
