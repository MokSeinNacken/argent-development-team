"""Role-scoped agent context building (SPEC V2 chapter 4 / V2.1 15.8).

``build_agent_context`` returns a deterministic, role-specific view over the
Core data.  This module is pure: the caller (``Core``) gathers the data from the
store and passes it in.  The role allow-list guarantees context isolation:

- analyst never sees the implementer's solution fields (``changed_files``,
  ``implementation_summary``, ``own_assessment``, ``proposal``),
- reviewer never sees the implementer's ``own_assessment``/``proposal``.

The returned dict is suitable for both the live prompt (controller's job to
phrase it) and the persisted ``agent_context_snapshots`` (hash + summary).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from . import events
from .models import DispatchError, PrivacyViolation, Role, Task

# Safe, static rule strings (privacy-safe; no deny-listed terms).
PROJECT_RULES: tuple[str, ...] = (
    "Core bleibt die einzige Autoritaetsinstanz",
    "Agenten liefern Empfehlungen, keine Authoritaet",
    "Owner Gates und Trust Boundary sind nicht umgehbar",
)

SECURITY_ARCH_RULES: tuple[str, ...] = (
    "keine Secrets oder Zugangsdaten preisgeben",
    "keine externen Aktionen ohne Owner Gate",
    "Rollengrenzen technisch erzwungen",
)

# Strict field allow-list for ``repo_summary`` (SPEC V2.2 16.5).  Only
# metadata is permitted; full diffs / source code / secrets are excluded.
REPO_SUMMARY_ALLOWLIST: frozenset[str] = frozenset(
    {"ref", "commit", "branch", "status", "changed_files", "summary"}
)

MAX_REPO_SUMMARY_BYTES = 256 * 1024
MAX_REPO_SUMMARY_DEPTH = 12
MAX_REPO_SUMMARY_STRING = 8192


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


def filter_repo_summary(repo_summary: Optional[dict]) -> dict:
    """Validate and filter a ``repo_summary`` for a context snapshot (16.5).

    Enforces a strict field allow-list, size/depth/string limits and the
    privacy deny-list.  Raises :class:`DispatchError` on unknown fields or
    limit violations and :class:`PrivacyViolation` on deny-listed content.
    Returns the (unmodified) dict on success.
    """
    if repo_summary is None:
        return {}
    if not isinstance(repo_summary, dict):
        raise DispatchError("repo_summary must be a dict")
    unknown = [k for k in repo_summary if k not in REPO_SUMMARY_ALLOWLIST]
    if unknown:
        raise DispatchError(
            f"repo_summary contains unknown field(s): {', '.join(sorted(map(str, unknown)))}"
        )
    try:
        serialized = json.dumps(repo_summary, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise DispatchError(f"repo_summary is not JSON-serializable: {exc}") from exc
    if len(serialized.encode("utf-8")) > MAX_REPO_SUMMARY_BYTES:
        raise DispatchError("repo_summary exceeds 256 KB limit")
    if _depth(repo_summary) > MAX_REPO_SUMMARY_DEPTH:
        raise DispatchError("repo_summary exceeds maximum nesting depth")
    if _max_string_len(repo_summary) > MAX_REPO_SUMMARY_STRING:
        raise DispatchError("repo_summary contains a string longer than 8 KB")
    hit = events.scan_value_for_denylist(repo_summary)
    if hit is not None:
        raise PrivacyViolation(
            f"repo_summary contains deny-listed term {hit!r}"
        )
    return repo_summary


def _summary_items(items) -> list[dict]:
    """Normalize structured Core rows into deterministic dict summaries."""
    out: list[dict] = []
    for it in items:
        if isinstance(it, dict):
            out.append(it)
        else:
            # Dataclass row -> known field subset (safe fields only).
            d = {}
            for key in ("id", "severity", "status", "result", "verdict", "decision"):
                if hasattr(it, key):
                    d[key] = getattr(it, key)
            out.append(d)
    return out


def build_agent_context(
    task: Task,
    role: Role,
    position: int,
    repo_summary: Optional[dict],
    *,
    findings: tuple = (),
    decisions: tuple = (),
    test_runs: tuple = (),
    reviews: tuple = (),
    changed_files: tuple = (),
) -> dict[str, Any]:
    """Build the role-scoped context dict (deterministic)."""
    owner_request = {
        "title": task.title,
        "description": task.description or "",
        "risk_class": task.risk_class.value,
    }

    sections: dict[str, Any] = {"role": role.value, "position": position}

    if role is Role.LEAD:
        sections.update(
            {
                "owner_request": owner_request,
                "project_rules": list(PROJECT_RULES),
                "safe_state": task.state.value,
                "findings": _summary_items(findings),
                "decisions": _summary_items(decisions),
                "test_runs": _summary_items(test_runs),
                "reviews": _summary_items(reviews),
            }
        )
    elif role is Role.ANALYST:
        # No implementer fields; problem context only.
        sections.update(
            {
                "owner_request": owner_request,
                "repo_state": repo_summary or {},
                "findings": _summary_items(findings),
            }
        )
    elif role is Role.IMPLEMENTER:
        decisions_sum = _summary_items(decisions)
        last_decision = decisions_sum[-1] if decisions_sum else {}
        sections.update(
            {
                "owner_request": owner_request,
                "lead_decision": last_decision,
                "confirmed_findings": _summary_items(findings),
                "scope": {
                    "task_id": task.id,
                    "risk_class": task.risk_class.value,
                    "external_actions_policy": task.external_actions_policy.value,
                },
                "write_policy": {
                    "product_code": "implementer-only write",
                    "test_code": "implementer write",
                },
            }
        )
    elif role is Role.QA:
        decisions_sum = _summary_items(decisions)
        last_decision = decisions_sum[-1] if decisions_sum else {}
        sections.update(
            {
                "owner_request": owner_request,
                "lead_decision": last_decision,
                "changed_files": list(changed_files),
                "test_requirements": ["alle Pflichtfaelle deterministisch abdecken"],
            }
        )
    elif role is Role.REVIEWER:
        # No implementer own_assessment/proposal; test results only.
        sections.update(
            {
                "owner_request": owner_request,
                "security_arch_rules": list(SECURITY_ARCH_RULES),
                "changed_files": list(changed_files),
                "repo_state": repo_summary or {},
                "test_runs": _summary_items(test_runs),
                "reviews": _summary_items(reviews),
            }
        )
    else:
        raise ValueError(f"unknown role {role!r}")

    return sections


def context_hash(sections: dict[str, Any]) -> str:
    """SHA-256 of the structured sections (canonical JSON)."""
    canonical = json.dumps(sections, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def context_summary_json(sections: dict[str, Any]) -> str:
    """Serialize the sections (already summary-only, no full diffs/secrets)."""
    return json.dumps(sections, sort_keys=True, default=str)
