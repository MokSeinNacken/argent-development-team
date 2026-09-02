"""Phase E2 — deterministic adaptive model router + capability escalation.

Provider-neutral, pure, deterministic.  **No LLM in the router, no shell, no
network, no I/O except loading the versioned policy/registry files.**  The
router converts a trusted :class:`RoutingRequest` (task facts + bounded
evidence assembled by the controller from the Core ledger) into a
:class:`RoutingDecision` (the single provider/model/reasoning identity to
dispatch), honouring the E1 capability floor and the trusted bootstrap routing
policy.

Fundamental invariants (verbindlich, Owner-Spec E2):

* **Quality/Security/Capability floor BEFORE cost.**  A hard floor violation
  removes a candidate; the deterministic ranking (minimum sufficient) can never
  override the floor.
* **Roles are capabilities; models are interchangeable implementations** (E1).
  The router maps a task profile (role + risk class + task state + evidence) to
  *capability requirements*, then selects the minimum sufficient eligible model.
* **Trusted bootstrap routing policy only.**  E1 models are ``benchmarked:
  false``; the router never derives a *new* capability→model authorisation from
  registry claims.  A model is eligible only if the versioned policy explicitly
  lists it for the active profile (``allowed_models``).  New/unknown models are
  never auto-eligible (CASE 15 / §31 exit criterion).
* **No escalation by text** (§12).  Agent prose can never raise a level, force a
  model, enable a provider, or change a benchmark status.  Only the bounded
  structured evidence triggers escalation.
* **Bounded, monotonic escalation** (§10/§23/§24): level 0 ROUTINE → 1
  IMPLEMENTATION → 2 DEEP_REASONING → 3 MAX_APPROVED → 4 OWNER (fail-closed).
  No silent downgrade within the same logical problem.
* **The router never grants tool permissions, never expands the D budget, never
  changes resource ceilings, never executes a fallback** (§16/§25–§27).  It
  returns a *model identity only*.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, Optional, Sequence, Tuple

from .job_state import ErrorClass
from .model_registry import (
    CAPABILITY_FLOOR_UNMET,
    MODEL_CONFIG_INVALID,
    Capability,
    CapabilityRequirements,
    Independence,
    ModelDescriptor,
    ModelRegistry,
    ModelRegistryError,
    ReasoningLevel,
    ReliabilityClass,
    get_default_registry,
)
from .models import ArgentError, DispatchStatus, RiskClass, Role

# ---------------------------------------------------------------------------
# Version / bounded codes
# ---------------------------------------------------------------------------

ROUTING_POLICY_VERSION = "1"

# Bounded escalation levels (owner-spec §10).
LEVEL_ROUTINE = 0
LEVEL_IMPLEMENTATION = 1
LEVEL_DEEP_REASONING = 2
LEVEL_MAX_APPROVED = 3
LEVEL_OWNER = 4

LEVEL_NAMES: Dict[int, str] = {
    LEVEL_ROUTINE: "ROUTINE",
    LEVEL_IMPLEMENTATION: "IMPLEMENTATION",
    LEVEL_DEEP_REASONING: "DEEP_REASONING",
    LEVEL_MAX_APPROVED: "MAX_APPROVED",
    LEVEL_OWNER: "OWNER",
}

# Bounded routing error codes.
ROUTING_NO_ELIGIBLE_CANDIDATE = "ROUTING_NO_ELIGIBLE_CANDIDATE"
ROUTING_OWNER_GATE = "ROUTING_OWNER_GATE"
ROUTING_POLICY_INVALID = "ROUTING_POLICY_INVALID"
ROUTING_REQUEST_INVALID = "ROUTING_REQUEST_INVALID"

ROUTING_ERROR_CODES: FrozenSet[str] = frozenset({
    ROUTING_NO_ELIGIBLE_CANDIDATE,
    ROUTING_OWNER_GATE,
    ROUTING_POLICY_INVALID,
    ROUTING_REQUEST_INVALID,
})


class RoutingReasonCode(str, Enum):
    """Bounded decision/trigger reason codes (§11).  Agent text is never a code."""

    MINIMUM_SUFFICIENT = "MINIMUM_SUFFICIENT"
    REPEATED_FIX_FAILURE = "REPEATED_FIX_FAILURE"
    TESTS_STILL_RED = "TESTS_STILL_RED"
    ROOT_CAUSE_UNPROVEN = "ROOT_CAUSE_UNPROVEN"
    CONTRADICTORY_EVIDENCE = "CONTRADICTORY_EVIDENCE"
    REVIEWER_REJECTED_CANDIDATE = "REVIEWER_REJECTED_CANDIDATE"
    UNEXPECTED_SCOPE_GROWTH = "UNEXPECTED_SCOPE_GROWTH"
    SECURITY_COMPLEXITY = "SECURITY_COMPLEXITY"
    CONCURRENCY_COMPLEXITY = "CONCURRENCY_COMPLEXITY"
    RECOVERY_COMPLEXITY = "RECOVERY_COMPLEXITY"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    MODEL_FAILURE = "MODEL_FAILURE"
    CAPABILITY_FLOOR_UNMET = "CAPABILITY_FLOOR_UNMET"
    MISSING_REQUIRED_EXTERNAL_INFO = "MISSING_REQUIRED_EXTERNAL_INFO"
    OWNER_GATE = "OWNER_GATE"
    NO_ELIGIBLE_CANDIDATE = "NO_ELIGIBLE_CANDIDATE"


#: Reason codes that ARE capability escalations (raise the level).
_CAPABILITY_TRIGGER_CODES: FrozenSet[str] = frozenset({
    RoutingReasonCode.REPEATED_FIX_FAILURE.value,
    RoutingReasonCode.TESTS_STILL_RED.value,
    RoutingReasonCode.ROOT_CAUSE_UNPROVEN.value,
    RoutingReasonCode.CONTRADICTORY_EVIDENCE.value,
    RoutingReasonCode.REVIEWER_REJECTED_CANDIDATE.value,
    RoutingReasonCode.UNEXPECTED_SCOPE_GROWTH.value,
    RoutingReasonCode.SECURITY_COMPLEXITY.value,
    RoutingReasonCode.CONCURRENCY_COMPLEXITY.value,
    RoutingReasonCode.RECOVERY_COMPLEXITY.value,
})

#: Reason codes that are provider/model/failure-side (never a capability failure,
#: never a capability escalation — they route through the existing WAIT/backoff).
_NON_CAPABILITY_CODES: FrozenSet[str] = frozenset({
    RoutingReasonCode.PROVIDER_FAILURE.value,
    RoutingReasonCode.MODEL_FAILURE.value,
})

#: Bounded attempt outcome classes (derived by the controller from trusted DB
#: fields; NEVER from agent prose).  ``CAPABILITY`` is the only class that counts
#: as a code/capability failure for escalation purposes.
ATTEMPT_OUTCOME_CAPABILITY = "CAPABILITY"
ATTEMPT_OUTCOME_TRANSIENT = "TRANSIENT"
ATTEMPT_OUTCOME_EXTERNAL = "EXTERNAL"
ATTEMPT_OUTCOME_PROVIDER = "PROVIDER"
ATTEMPT_OUTCOME_RESOURCE = "RESOURCE"
ATTEMPT_OUTCOME_CONTEXT = "CONTEXT"
ATTEMPT_OUTCOME_SECURITY = "SECURITY"
ATTEMPT_OUTCOME_OWNER = "OWNER_REQUIRED"
ATTEMPT_OUTCOME_SUCCESS = "SUCCESS"
ATTEMPT_OUTCOME_OTHER = "OTHER"

_ATTEMPT_OUTCOMES: FrozenSet[str] = frozenset({
    ATTEMPT_OUTCOME_CAPABILITY,
    ATTEMPT_OUTCOME_TRANSIENT,
    ATTEMPT_OUTCOME_EXTERNAL,
    ATTEMPT_OUTCOME_PROVIDER,
    ATTEMPT_OUTCOME_RESOURCE,
    ATTEMPT_OUTCOME_CONTEXT,
    ATTEMPT_OUTCOME_SECURITY,
    ATTEMPT_OUTCOME_OWNER,
    ATTEMPT_OUTCOME_SUCCESS,
    ATTEMPT_OUTCOME_OTHER,
})

#: Canonical bounded reviewer verdicts (F2).  The Core persists ONLY these
#: two values into ``reviews.verdict`` (free-text recommendation goes to the
#: untrusted ``detail`` column).  The router evaluates ONLY these canonical
#: values — never arbitrary agent prose.
CANONICAL_VERDICT_APPROVE = "approve"
CANONICAL_VERDICT_REJECT = "reject"

#: Reviewer verdicts that constitute a reviewer-reject (bounded, canonical).
_REVIEWER_REJECT_VERDICTS: FrozenSet[str] = frozenset({CANONICAL_VERDICT_REJECT})

#: Reviewer verdicts that constitute an approve (bounded, canonical).
_REVIEWER_APPROVE_VERDICTS: FrozenSet[str] = frozenset({CANONICAL_VERDICT_APPROVE})

#: Dispatch thinking-tier axis -> ReasoningLevel (same three-valued capability
#: axis; the dispatch layer uses lowercase ``high``/``medium``/``low``).
_THINKING_TIER_TO_REASONING: Dict[str, str] = {
    "high": ReasoningLevel.HIGH.value,
    "medium": ReasoningLevel.MEDIUM.value,
    "low": ReasoningLevel.LOW.value,
}


# ---------------------------------------------------------------------------
# Controller-side evidence classification (trusted, deterministic)
# ---------------------------------------------------------------------------


def thinking_to_reasoning(tier: Optional[str]) -> Optional[str]:
    """Map a dispatch ``expected_thinking_tier`` to a bounded ``ReasoningLevel``.

    The dispatch layer's thinking-tier axis (``high``/``medium``/``low``) is the
    same three-valued capability axis as ``ReasoningLevel``; this maps it into
    the router's canonical vocabulary.  ``None`` or any unknown/foreign string
    maps to ``None`` (unknown) — a level is never fabricated.
    """
    if tier is None:
        return None
    if not isinstance(tier, str):
        return None
    return _THINKING_TIER_TO_REASONING.get(tier.strip().lower())


def classify_attempt(
    dispatch_status: Optional[str],
    error_class: Optional[str],
    tests_red: bool,
    reviewer_rejected: bool,
) -> str:
    """Classify one prior dispatch attempt into a bounded outcome class.

    Deterministic and provider/transport-safe (§14/§15).  The returned value is
    always one of ``_ATTEMPT_OUTCOMES``.  The single hard rule: a provider /
    transport / resource / context / security / owner signal is NEVER a
    capability gap, and only a capability gap can drive capability escalation.

    * ``error_class`` EXTERNAL / PROVIDER / TRANSIENT (and registry-side
      PROVIDER codes) are provider/transport signals → their own non-capability
      outcome (never ``CAPABILITY``).
    * RESOURCE / CONTEXT / SECURITY / OWNER_REQUIRED → their own classes.
    * DETERMINISTIC (a deterministic code failure) → ``CAPABILITY``.
    * With no error class (``NONE``), a *consumed* attempt is judged by the CODE
      follow-up signals: tests still red or a reviewer reject ⇒ ``CAPABILITY``;
      a clean consumed attempt ⇒ ``SUCCESS``.  A failed/rejected attempt with
      no transport signal is a code gap ⇒ ``CAPABILITY``.  An incomplete
      attempt (pending/running/recovery/quarantined) ⇒ ``OTHER``.
    """
    ec = (error_class or "").strip().upper()
    if ec == ErrorClass.TRANSIENT.value:
        return ATTEMPT_OUTCOME_TRANSIENT
    if ec == ErrorClass.EXTERNAL.value:
        return ATTEMPT_OUTCOME_EXTERNAL
    if ec == ErrorClass.PROVIDER.value:
        return ATTEMPT_OUTCOME_PROVIDER
    if ec == ErrorClass.RESOURCE.value:
        return ATTEMPT_OUTCOME_RESOURCE
    if ec == ErrorClass.CONTEXT.value:
        return ATTEMPT_OUTCOME_CONTEXT
    if ec == ErrorClass.SECURITY.value:
        return ATTEMPT_OUTCOME_SECURITY
    if ec == ErrorClass.OWNER_REQUIRED.value:
        return ATTEMPT_OUTCOME_OWNER
    if ec == ErrorClass.DETERMINISTIC.value:
        return ATTEMPT_OUTCOME_CAPABILITY

    status = (dispatch_status or "").strip().upper()
    if status == DispatchStatus.CONSUMED.value:
        if tests_red or reviewer_rejected:
            return ATTEMPT_OUTCOME_CAPABILITY
        return ATTEMPT_OUTCOME_SUCCESS
    if status in (DispatchStatus.FAILED.value, DispatchStatus.REJECTED.value):
        return ATTEMPT_OUTCOME_CAPABILITY
    return ATTEMPT_OUTCOME_OTHER

#: Bounded policy document / key allow-lists (fail-closed at load).
_POLICY_DOC_KEYS: FrozenSet[str] = frozenset({
    "policy_version", "bootstrap", "benchmark_required_for_new_models",
    "escalation", "reasoning", "level_min_tiers", "model_tiers", "profiles",
    "escalation_profiles", "cost_order", "latency_order",
})
_PROFILE_KEYS: FrozenSet[str] = frozenset({
    "roles", "required_capabilities", "minimum_reasoning_level", "entry_level",
    "low_risk_entry_level", "allowed_models", "independence",
})
_ESCALATION_KEYS: FrozenSet[str] = frozenset({
    "max_automatic_level", "owner_level", "level_names",
})
_REASONING_KEYS: FrozenSet[str] = frozenset({"level_defaults", "level_ceilings"})

_REASONING_RANK: Dict[str, int] = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
_TIER_RANK: Dict[int, int] = {0: 0, 1: 1, 2: 2}


class RoutingError(ArgentError):
    """A deterministic routing violation (bounded ``code``, fail-closed).

    Never a CODE/RESOURCE/CONTEXT failure; carries no automatic fallback.  Its
    ``error_class`` mirrors the registry side (``PROVIDER``) only for registry
    -originated violations; pure routing outcomes (no candidate / owner gate)
    use ``OWNER_REQUIRED`` so the supervisor routes them to its existing
    BLOCKED/OWNER mechanism rather than a provider retry.
    """

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)

    @property
    def error_class(self) -> str:
        if self.code == ROUTING_NO_ELIGIBLE_CANDIDATE:
            return ErrorClass.OWNER_REQUIRED.value
        if self.code == ROUTING_OWNER_GATE:
            return ErrorClass.OWNER_REQUIRED.value
        return ErrorClass.PROVIDER.value


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttemptEvidence:
    """Bounded evidence for one prior dispatch attempt (trusted, bounded)."""

    attempt_no: int
    model_id: Optional[str]
    reasoning_level: Optional[str]
    outcome_class: str          # one of _ATTEMPT_OUTCOMES
    status: Optional[str] = None        # DispatchStatus value
    sequence_kind: Optional[str] = None  # STANDARD/REWORK
    escalation_level: int = 0
    failure_code: Optional[str] = None


@dataclass(frozen=True)
class RoutingEvidence:
    """Bounded trusted evidence assembled by the controller (C-fields only)."""

    prior_attempts: Tuple[AttemptEvidence, ...] = ()
    test_results: Tuple[str, ...] = ()           # "passed"/"failed", chronological
    reviewer_verdicts: Tuple[str, ...] = ()      # verdict strings, chronological
    open_findings_count: int = 0
    confirmed_finding: bool = False
    security_relevant: bool = False
    concurrency_relevant: bool = False


@dataclass(frozen=True)
class RoutingRequest:
    """Trusted routing request (job/task facts + bounded evidence)."""

    job_id: str
    task_id: str
    role: str
    risk_class: str = RiskClass.NORMAL.value
    dispatch_id: Optional[str] = None
    reference_model_id: Optional[str] = None
    independence_requirement: Optional[str] = None
    evidence: RoutingEvidence = RoutingEvidence()
    current_escalation_level: int = LEVEL_ROUTINE
    policy_version: str = ROUTING_POLICY_VERSION


@dataclass(frozen=True)
class RoutingDecision:
    """The single dispatch identity produced by the router.

    ``provider``/``model``/``reasoning_level`` may be ``None`` ONLY for a
    terminal owner/no-candidate decision (``is_terminal`` True) — the caller
    must fail-closed (BLOCKED/OWNER), never dispatch a ``None`` identity.
    """

    decision_id: str
    job_id: str
    task_id: str
    role: str
    dispatch_id: Optional[str]
    provider: Optional[str]
    model: Optional[str]
    reasoning_level: Optional[str]
    escalation_level: int
    required_capabilities: Tuple[str, ...]
    matched_capabilities: Tuple[str, ...]
    requirements_hash: str
    decision_reason_code: str
    policy_version: str
    evidence_refs: Tuple[str, ...]
    reference_model_id: Optional[str]
    independence_requirement: Optional[str]
    canonical_json: str
    created_at: str
    sha256: str

    @property
    def is_terminal(self) -> bool:
        return (
            self.provider is None
            or self.model is None
            or self.escalation_level >= LEVEL_OWNER
            or self.decision_reason_code in (
                RoutingReasonCode.OWNER_GATE.value,
                RoutingReasonCode.NO_ELIGIBLE_CANDIDATE.value,
            )
        )

    @property
    def is_owner_gate(self) -> bool:
        return self.decision_reason_code == RoutingReasonCode.OWNER_GATE.value

    def thinking_tier(self) -> Optional[str]:
        """Map the reasoning level to the dispatch ``thinking_tier`` axis."""
        if self.reasoning_level is None:
            return None
        return self.reasoning_level.lower()


# ---------------------------------------------------------------------------
# Routing policy (immutable, fail-closed load)
# ---------------------------------------------------------------------------


def _require_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RoutingError(ROUTING_POLICY_INVALID, f"{label} must be a non-empty string")
    return value


def _require_int(value: Any, label: str, lo: int, hi: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RoutingError(ROUTING_POLICY_INVALID, f"{label} must be an int")
    if value < lo or value > hi:
        raise RoutingError(ROUTING_POLICY_INVALID, f"{label} out of range [{lo},{hi}]")
    return value


def _reject_unknown(raw: Dict[str, Any], allowed: FrozenSet[str], context: str) -> None:
    for key in raw:
        if key not in allowed:
            raise RoutingError(ROUTING_POLICY_INVALID, f"{context}: unknown field {key!r}")


def _validate_level_names(value: Any, owner_lvl: int) -> None:
    """F5(b): strict ``level_names`` validation (exact keys 0..owner_level)."""
    if not isinstance(value, dict) or not value:
        raise RoutingError(ROUTING_POLICY_INVALID, "escalation.level_names must be a non-empty object")
    expected = {str(i) for i in range(0, owner_lvl + 1)}
    if set(value.keys()) != expected:
        raise RoutingError(
            ROUTING_POLICY_INVALID,
            f"level_names keys {sorted(value.keys())!r} must be exactly 0..{owner_lvl}",
        )
    for k, v in value.items():
        if not isinstance(v, str) or not v:
            raise RoutingError(ROUTING_POLICY_INVALID, f"level_names[{k}] must be a non-empty string")


def _require_exact_level_keys(value: Any, max_auto: int, context: str) -> None:
    """F5(b): the inner dict's keys must be EXACTLY 0..max_auto (no extras).

    Both int and str keys are accepted (JSON objects always use string keys), but
    the *set* of integer levels must equal ``range(0, max_auto+1)``.
    """
    if not isinstance(value, dict):
        raise RoutingError(ROUTING_POLICY_INVALID, f"{context} must be an object")
    normalized = set()
    for k in value:
        try:
            lvl = int(k)
        except (TypeError, ValueError):
            raise RoutingError(ROUTING_POLICY_INVALID, f"{context} has non-integer key {k!r}")
        if lvl < 0 or lvl > max_auto:
            raise RoutingError(ROUTING_POLICY_INVALID, f"{context} has out-of-range key {k!r}")
        normalized.add(lvl)
    if normalized != set(range(0, max_auto + 1)):
        raise RoutingError(
            ROUTING_POLICY_INVALID,
            f"{context} keys must be exactly 0..{max_auto}, got {sorted(normalized)!r}",
        )


def _require_monotonic_tiers(tiers: Dict[str, int]) -> None:
    """F5(f): model_tiers must be contiguous 0..k (monotonic, no gaps)."""
    values = sorted(set(tiers.values()))
    if values != list(range(0, max(values) + 1)):
        raise RoutingError(
            ROUTING_POLICY_INVALID,
            f"model_tiers values must be contiguous 0..k, got {values!r}",
        )


class RoutingPolicy:
    """Immutable trusted bootstrap routing policy (§3/§19)."""

    def __init__(self, raw: Dict[str, Any]):
        if not isinstance(raw, dict):
            raise RoutingError(ROUTING_POLICY_INVALID, "policy must be an object")
        _reject_unknown(raw, _POLICY_DOC_KEYS, "policy document")
        pv = _require_id(raw.get("policy_version"), "policy_version")
        if pv != ROUTING_POLICY_VERSION:
            raise RoutingError(
                ROUTING_POLICY_INVALID,
                f"policy_version {pv!r} != {ROUTING_POLICY_VERSION!r}",
            )
        bootstrap = raw.get("bootstrap", False)
        if not isinstance(bootstrap, bool):
            raise RoutingError(ROUTING_POLICY_INVALID, "bootstrap must be a bool")
        bench = raw.get("benchmark_required_for_new_models", True)
        if not isinstance(bench, bool):
            raise RoutingError(ROUTING_POLICY_INVALID, "benchmark_required_for_new_models must be a bool")
        # F5(d): bootstrap routing policy version 1 MUST be bootstrap-trusted and
        # MUST require benchmarks for new models (no weak policy is accepted).
        if bootstrap is not True:
            raise RoutingError(
                ROUTING_POLICY_INVALID, "bootstrap must be true in policy version 1"
            )
        if bench is not True:
            raise RoutingError(
                ROUTING_POLICY_INVALID,
                "benchmark_required_for_new_models must be true in policy version 1",
            )

        esc = raw.get("escalation", {})
        if not isinstance(esc, dict):
            raise RoutingError(ROUTING_POLICY_INVALID, "escalation must be an object")
        _reject_unknown(esc, _ESCALATION_KEYS, "escalation")
        max_auto = _require_int(esc.get("max_automatic_level"), "max_automatic_level", 0, 4)
        owner_lvl = _require_int(esc.get("owner_level"), "owner_level", 1, 4)
        if owner_lvl <= max_auto:
            raise RoutingError(ROUTING_POLICY_INVALID, "owner_level must exceed max_automatic_level")
        # F5(b): ``level_names`` is a strictly-validated inner dict (exact keys
        # 0..owner_level, non-empty string values, no extra keys).
        _validate_level_names(esc.get("level_names"), owner_lvl)

        reasoning = raw.get("reasoning", {})
        if not isinstance(reasoning, dict):
            raise RoutingError(ROUTING_POLICY_INVALID, "reasoning must be an object")
        _reject_unknown(reasoning, _REASONING_KEYS, "reasoning")
        defaults = reasoning.get("level_defaults", {})
        ceilings = reasoning.get("level_ceilings", {})
        if not isinstance(defaults, dict) or not isinstance(ceilings, dict):
            raise RoutingError(ROUTING_POLICY_INVALID, "reasoning defaults/ceilings must be objects")
        # Reasoning defaults/ceilings are required up to the max automatic level
        # (the OWNER level is terminal and never selects a model).  F5(b): the
        # key set must be EXACTLY 0..max_auto (string or int keys) — extra keys
        # are refused.
        _require_exact_level_keys(defaults, max_auto, "reasoning.level_defaults")
        _require_exact_level_keys(ceilings, max_auto, "reasoning.level_ceilings")
        self._reasoning_defaults: Dict[int, str] = {}
        self._reasoning_ceilings: Dict[int, str] = {}
        for lvl in range(0, max_auto + 1):
            d = defaults.get(str(lvl), defaults.get(lvl))
            c = ceilings.get(str(lvl), ceilings.get(lvl))
            if d is None or c is None:
                raise RoutingError(ROUTING_POLICY_INVALID, f"reasoning default/ceiling missing for level {lvl}")
            _require_reasoning(d, f"level_defaults[{lvl}]")
            _require_reasoning(c, f"level_ceilings[{lvl}]")
            if _REASONING_RANK[c] < _REASONING_RANK[d]:
                raise RoutingError(ROUTING_POLICY_INVALID, f"ceiling below default at level {lvl}")
            self._reasoning_defaults[lvl] = d
            self._reasoning_ceilings[lvl] = c

        lmt = raw.get("level_min_tiers", {})
        if not isinstance(lmt, dict):
            raise RoutingError(ROUTING_POLICY_INVALID, "level_min_tiers must be an object")
        _require_exact_level_keys(lmt, max_auto, "level_min_tiers")
        self._level_min_tiers: Dict[int, int] = {}
        for lvl in range(0, max_auto + 1):
            t = lmt.get(str(lvl), lmt.get(lvl))
            if t is None:
                raise RoutingError(ROUTING_POLICY_INVALID, f"level_min_tiers missing for level {lvl}")
            self._level_min_tiers[lvl] = _require_int(t, f"level_min_tiers[{lvl}]", 0, 2)

        tiers = raw.get("model_tiers", {})
        if not isinstance(tiers, dict) or not tiers:
            raise RoutingError(ROUTING_POLICY_INVALID, "model_tiers must be a non-empty object")
        self._model_tiers: Dict[str, int] = {}
        for mid, t in tiers.items():
            _require_id(mid, "model_tiers key")
            self._model_tiers[mid] = _require_int(t, f"model_tiers[{mid}]", 0, 2)
        # F5(f): tiers must be monotonic and contiguous (0..k with no gaps).
        _require_monotonic_tiers(self._model_tiers)

        self._profiles: Dict[str, Dict[str, Any]] = {}
        raw_profiles = raw.get("profiles", {})
        if not isinstance(raw_profiles, dict) or not raw_profiles:
            raise RoutingError(ROUTING_POLICY_INVALID, "profiles must be a non-empty object")
        for pid, p in raw_profiles.items():
            self._profiles[_require_id(pid, "profile id")] = self._parse_profile(p, pid)

        self._escalation_profiles: Dict[str, Dict[str, Any]] = {}
        raw_esc_profiles = raw.get("escalation_profiles", {})
        if not isinstance(raw_esc_profiles, dict):
            raise RoutingError(ROUTING_POLICY_INVALID, "escalation_profiles must be an object")
        for pid, p in raw_esc_profiles.items():
            self._escalation_profiles[_require_id(pid, "escalation profile id")] = self._parse_profile(p, pid)

        self._cost_order = _require_class_order(raw.get("cost_order"), "cost_order")
        self._latency_order = _require_class_order(raw.get("latency_order"), "latency_order")

        self._bootstrap = bootstrap
        self._benchmark_required = bench
        self._max_automatic_level = max_auto
        self._owner_level = owner_lvl

    # -- parse helpers ------------------------------------------------------

    def _parse_profile(self, p: Any, pid: str) -> Dict[str, Any]:
        if not isinstance(p, dict):
            raise RoutingError(ROUTING_POLICY_INVALID, f"profile {pid} must be an object")
        _reject_unknown(p, _PROFILE_KEYS, f"profile {pid}")
        roles = p.get("roles")
        if roles is None:
            roles = ()  # escalation profiles are capability-only (no roles)
        elif not isinstance(roles, list) or not roles:
            raise RoutingError(ROUTING_POLICY_INVALID, f"profile {pid}.roles must be a non-empty list")
        # F5(c): roles must be valid Role enum values, duplicates rejected.
        roles = tuple(_require_enum_str(r, Role, f"profile {pid}.roles") for r in roles)
        if len(roles) != len(set(roles)):
            raise RoutingError(ROUTING_POLICY_INVALID, f"profile {pid} has duplicate roles")
        req = p.get("required_capabilities")
        if not isinstance(req, list) or not req:
            raise RoutingError(ROUTING_POLICY_INVALID, f"profile {pid}.required_capabilities must be a non-empty list")
        req_caps = tuple(_require_capability(c, f"profile {pid}") for c in req)
        if len(req_caps) != len(set(req_caps)):
            raise RoutingError(ROUTING_POLICY_INVALID, f"profile {pid} has duplicate required capabilities")
        min_reason = p.get("minimum_reasoning_level")
        _require_reasoning(min_reason, f"profile {pid}.minimum_reasoning_level")
        entry = _require_int(p.get("entry_level"), f"profile {pid}.entry_level", 0, 4)
        low_entry = p.get("low_risk_entry_level")
        if low_entry is not None:
            low_entry = _require_int(low_entry, f"profile {pid}.low_risk_entry_level", 0, 4)
        allowed = p.get("allowed_models")
        if not isinstance(allowed, list) or not allowed:
            raise RoutingError(ROUTING_POLICY_INVALID, f"profile {pid}.allowed_models must be a non-empty list")
        allowed_models = tuple(_require_id(m, f"profile {pid}.allowed_models") for m in allowed)
        if len(allowed_models) != len(set(allowed_models)):
            raise RoutingError(ROUTING_POLICY_INVALID, f"profile {pid} has duplicate allowed_models")
        for m in allowed_models:
            if m not in self._model_tiers:
                raise RoutingError(ROUTING_POLICY_INVALID,
                                   f"profile {pid} allows model {m!r} missing from model_tiers")
        # F5(g): a profile floor above the reachable level ceiling is a policy
        # error (reject), never a silent clamp at route time.
        for lvl in {entry} | ({low_entry} if low_entry is not None else set()):
            ceiling = self._reasoning_ceilings.get(lvl)
            if ceiling is not None and _REASONING_RANK[min_reason] > _REASONING_RANK[ceiling]:
                raise RoutingError(
                    ROUTING_POLICY_INVALID,
                    f"profile {pid}.minimum_reasoning_level {min_reason!r} "
                    f"exceeds reasoning ceiling {ceiling!r} at level {lvl}",
                )
        independence = p.get("independence")
        if independence is not None:
            _require_enum_str(independence, Independence, f"profile {pid}.independence")
        return {
            "roles": tuple(roles),
            "required_capabilities": req_caps,
            "minimum_reasoning_level": min_reason,
            "entry_level": entry,
            "low_risk_entry_level": low_entry,
            "allowed_models": allowed_models,
            "independence": independence,
        }

    # -- read-only access ---------------------------------------------------

    @property
    def version(self) -> str:
        return ROUTING_POLICY_VERSION

    @property
    def max_automatic_level(self) -> int:
        return self._max_automatic_level

    @property
    def owner_level(self) -> int:
        return self._owner_level

    @property
    def bootstrap(self) -> bool:
        return self._bootstrap

    @property
    def benchmark_required_for_new_models(self) -> bool:
        return self._benchmark_required

    def reasoning_default(self, level: int) -> str:
        return self._reasoning_defaults[level]

    def reasoning_ceiling(self, level: int) -> str:
        return self._reasoning_ceilings[level]

    def min_tier_for_level(self, level: int) -> int:
        return self._level_min_tiers[level]

    def tier_of(self, model_id: str) -> Optional[int]:
        return self._model_tiers.get(model_id)

    def profile_for_role(self, role: str) -> Dict[str, Any]:
        for pid, p in self._profiles.items():
            if role in p["roles"]:
                return p
        raise RoutingError(ROUTING_POLICY_INVALID, f"no policy profile for role {role!r}")

    def escalation_profile(self, profile_id: str) -> Optional[Dict[str, Any]]:
        return self._escalation_profiles.get(profile_id)

    def cost_rank(self, cost_class: str) -> int:
        return self._cost_order.index(cost_class) if cost_class in self._cost_order else len(self._cost_order)

    def latency_rank(self, latency_class: str) -> int:
        return self._latency_order.index(latency_class) if latency_class in self._latency_order else len(self._latency_order)

    def validate_models(self, registry: ModelRegistry) -> None:
        """F5(e): cross-validate every model reference against the E1 registry.

        Every ``model_tiers`` key and every ``allowed_models`` entry must exist
        in the registry, be enabled, and reference a known provider.  A model
        the registry does not describe (or that is disabled/unknown) can never
        be policy-authorised.
        """
        for mid in self._model_tiers:
            md = registry.get_model(mid)
            if md is None:
                raise RoutingError(
                    ROUTING_POLICY_INVALID,
                    f"model_tiers references unknown model {mid!r}",
                )
            if not md.enabled:
                raise RoutingError(
                    ROUTING_POLICY_INVALID,
                    f"model_tiers references disabled model {mid!r}",
                )
            if registry.get_provider(md.provider_id) is None:
                raise RoutingError(
                    ROUTING_POLICY_INVALID,
                    f"model_tiers model {mid!r} has unknown provider {md.provider_id!r}",
                )
        for pid, p in self._profiles.items():
            for m in p["allowed_models"]:
                self._validate_allowed_model(registry, m, f"profile {pid}")
        for pid, p in self._escalation_profiles.items():
            for m in p["allowed_models"]:
                self._validate_allowed_model(registry, m, f"escalation profile {pid}")

    @staticmethod
    def _validate_allowed_model(registry: ModelRegistry, mid: str, context: str) -> None:
        md = registry.get_model(mid)
        if md is None:
            raise RoutingError(
                ROUTING_POLICY_INVALID, f"{context} allows unknown model {mid!r}",
            )
        if not md.enabled:
            raise RoutingError(
                ROUTING_POLICY_INVALID, f"{context} allows disabled model {mid!r}",
            )
        if registry.get_provider(md.provider_id) is None:
            raise RoutingError(
                ROUTING_POLICY_INVALID,
                f"{context} model {mid!r} has unknown provider {md.provider_id!r}",
            )


def _require_capability(value: Any, label: str) -> str:
    return _require_enum_str(value, Capability, label)


def _require_reasoning(value: Any, label: str) -> str:
    return _require_enum_str(value, ReasoningLevel, label)


def _require_enum_str(value: Any, enum_cls: type, label: str) -> str:
    try:
        return enum_cls(value).value
    except (ValueError, TypeError):
        raise RoutingError(
            ROUTING_POLICY_INVALID,
            f"invalid {label} {value!r}; expected one of {sorted(m.value for m in enum_cls)}",
        ) from None


def _require_class_order(value: Any, label: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise RoutingError(ROUTING_POLICY_INVALID, f"{label} must be a non-empty list")
    out = tuple(_require_enum_str(v, _CostLatencyClass, label) for v in value)
    if len(out) != len(set(out)):
        raise RoutingError(ROUTING_POLICY_INVALID, f"{label} must not contain duplicates")
    return out


class _CostLatencyClass(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

_default_policy: Optional[RoutingPolicy] = None


def _no_duplicate_keys(pairs: list) -> dict:
    """F5(a): object_pairs_hook that refuses duplicate JSON keys.

    ``json.loads`` silently keeps the last duplicate key; the routing policy is
    a trusted bootstrap document, so a duplicate key (an ambiguity) is refused
    fail-closed rather than silently resolved.
    """
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise RoutingError(
                ROUTING_POLICY_INVALID, f"duplicate JSON key {key!r}"
            )
        out[key] = value
    return out


def load_routing_policy(base_dir: Optional[str] = None) -> RoutingPolicy:
    """Load the versioned bootstrap routing policy file (fail-closed)."""
    base = Path(base_dir) if base_dir else Path(__file__).resolve().parent / "registry"
    path = base / "routing_policy_v1.json"
    try:
        doc = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys
        )
    except RoutingError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise RoutingError(ROUTING_POLICY_INVALID, f"cannot read routing policy: {exc}") from None
    if not isinstance(doc, dict):
        raise RoutingError(ROUTING_POLICY_INVALID, "routing policy must be a top-level object")
    return RoutingPolicy(doc)


def get_default_policy() -> RoutingPolicy:
    global _default_policy
    if _default_policy is None:
        pol = load_routing_policy()
        # F5(e): the trusted bootstrap policy is cross-validated against the
        # loaded E1 registry at (default) load time — a policy referencing an
        # unknown/disabled model or unknown provider is refused fail-closed.
        pol.validate_models(get_default_registry())
        _default_policy = pol
    return _default_policy


def reset_default_policy() -> None:
    global _default_policy
    _default_policy = None


# ---------------------------------------------------------------------------
# Deterministic evidence → triggers / level
# ---------------------------------------------------------------------------


def _is_capability_failure(attempt: AttemptEvidence) -> bool:
    return attempt.outcome_class == ATTEMPT_OUTCOME_CAPABILITY


def _is_reject_verdict(verdict: str) -> bool:
    return verdict.strip().lower() in _REVIEWER_REJECT_VERDICTS


def _is_approve_verdict(verdict: str) -> bool:
    return verdict.strip().lower() in _REVIEWER_APPROVE_VERDICTS


def _tests_contradict(tests: Sequence[str]) -> bool:
    """A current-cycle test contradiction: a pass immediately followed by a
    fail (regression).  An older fail that was later fixed does NOT contradict."""
    return len(tests) >= 2 and tests[-1] == "failed" and tests[-2] == "passed"


def _verdicts_contradict(verdicts: Sequence[str]) -> bool:
    """A current-cycle verdict contradiction: a reject immediately after an
    approve (a flip within the current cycle).  An older reject superseded by a
    later approve does NOT stick."""
    return (
        len(verdicts) >= 2
        and _is_reject_verdict(verdicts[-1])
        and _is_approve_verdict(verdicts[-2])
    )


def detect_triggers(evidence: RoutingEvidence) -> FrozenSet[str]:
    """Derive the bounded escalation triggers from trusted evidence only.

    Agent prose is never an input here (the collector builds ``evidence`` from
    the Core ledger's C-fields).  Provider/transient/resource/context failures
    never produce a capability trigger (§14/§15).
    """
    triggers: set = set()

    capability_failures = [a for a in evidence.prior_attempts if _is_capability_failure(a)]
    distinct_fix_attempts = {a.attempt_no for a in capability_failures}
    if len(distinct_fix_attempts) >= 2:
        triggers.add(RoutingReasonCode.REPEATED_FIX_FAILURE.value)

    if evidence.test_results and evidence.test_results[-1] == "failed":
        triggers.add(RoutingReasonCode.TESTS_STILL_RED.value)

    if evidence.open_findings_count > 0 and not evidence.confirmed_finding:
        triggers.add(RoutingReasonCode.ROOT_CAUSE_UNPROVEN.value)

    if any(_is_reject_verdict(v) for v in evidence.reviewer_verdicts):
        triggers.add(RoutingReasonCode.REVIEWER_REJECTED_CANDIDATE.value)

    # CONTRADICTORY_EVIDENCE (F4): only from currently-valid, non-superseded
    # evidence — the LATEST verdict/test contradicting the immediately
    # preceding one (a genuine flip within the current cycle).  "Ever passed +
    # ever failed" (or "ever approve + ever reject") across the whole history
    # must NOT produce a sticky contradiction.
    tests = evidence.test_results
    verdicts = evidence.reviewer_verdicts
    if _tests_contradict(tests) or _verdicts_contradict(verdicts):
        triggers.add(RoutingReasonCode.CONTRADICTORY_EVIDENCE.value)

    if evidence.security_relevant:
        triggers.add(RoutingReasonCode.SECURITY_COMPLEXITY.value)

    if evidence.concurrency_relevant:
        triggers.add(RoutingReasonCode.CONCURRENCY_COMPLEXITY.value)

    # Provider-side failures (NEVER a capability escalation, §14/§15): any
    # prior attempt whose outcome is a provider/transport signal (EXTERNAL /
    # PROVIDER / TRANSIENT) is surfaced as a non-capability reason code only,
    # so the supervisor can route to its existing WAIT/backoff paths (§27).
    # ``MODEL_FAILURE`` remains a reserved bounded code (no distinct bootstrap
    # evidence class; a model-level code failure is already ``CAPABILITY``).
    if any(
        a.outcome_class in (
            ATTEMPT_OUTCOME_EXTERNAL,
            ATTEMPT_OUTCOME_PROVIDER,
            ATTEMPT_OUTCOME_TRANSIENT,
        )
        for a in evidence.prior_attempts
    ):
        triggers.add(RoutingReasonCode.PROVIDER_FAILURE.value)

    return frozenset(triggers)


def _apply_trigger_levels(
    level: int,
    current: int,
    triggers: FrozenSet[str],
    policy: RoutingPolicy,
    role: Optional[str] = None,
) -> int:
    """Deterministic escalation ladder from the trigger set (§10–§11).

    * SECURITY_COMPLEXITY / hard root cause (REPEATED_FIX_FAILURE +
      TESTS_STILL_RED + ROOT_CAUSE_UNPROVEN) impose a *floor* of DEEP_REASONING
      (level 2) — a direct, policy-justified tier jump ("kein blindes lineares
      Durchprobieren", §10).  F3: the SECURITY_COMPLEXITY floor is ROLE-aware —
      it raises only the roles that have a deep-reasoning security escalation
      (analyst deep analysis, reviewer security review, lead coordination).
      The implementer/qa keep their implementation capability and stay on their
      own model (the separate closing review carries the security review).
    * One-step escalators (reviewer reject, contradictory evidence, concurrency/
      recovery complexity, scope growth) raise the level by at most ONE step in
      total (§24 bounded).
    * A root cause that persists AFTER prior attempts already ran at the
      deep-reasoning tier (``current >= 2``) is a further objective escalation
      (Sol → Sol HIGH), one step, never a loop.
    * Provider/model failure codes never escalate capability (§14/§15).
    """
    hard_root_cause = {
        RoutingReasonCode.REPEATED_FIX_FAILURE.value,
        RoutingReasonCode.TESTS_STILL_RED.value,
        RoutingReasonCode.ROOT_CAUSE_UNPROVEN.value,
    }
    if hard_root_cause <= triggers:
        level = max(level, LEVEL_DEEP_REASONING)
    if RoutingReasonCode.SECURITY_COMPLEXITY.value in triggers:
        if role in _SECURITY_DEEP_REASONING_ROLES:
            level = max(level, LEVEL_DEEP_REASONING)

    step = 0
    for code in (
        RoutingReasonCode.REVIEWER_REJECTED_CANDIDATE.value,
        RoutingReasonCode.CONTRADICTORY_EVIDENCE.value,
        RoutingReasonCode.CONCURRENCY_COMPLEXITY.value,
        RoutingReasonCode.RECOVERY_COMPLEXITY.value,
        RoutingReasonCode.UNEXPECTED_SCOPE_GROWTH.value,
    ):
        if code in triggers:
            step = max(step, 1)
    # A root cause that persists AT the deep-reasoning tier (prior attempts
    # already ran at level >= 2) is a further objective escalation
    # (Sol → Sol HIGH), one step, never a loop.
    if hard_root_cause <= triggers and current >= LEVEL_DEEP_REASONING:
        step = max(step, 1)
    level += step

    return level


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------


class ModelRouter:
    """Deterministic, provider-neutral adaptive model router."""

    def __init__(
        self,
        registry: Optional[ModelRegistry] = None,
        policy: Optional[RoutingPolicy] = None,
    ):
        self._registry = registry
        self._policy = policy

    def _reg(self) -> ModelRegistry:
        return self._registry if self._registry is not None else get_default_registry()

    def _pol(self) -> RoutingPolicy:
        return self._policy if self._policy is not None else get_default_policy()

    # -- public -------------------------------------------------------------

    def route(
        self,
        request: RoutingRequest,
        now_iso: Optional[str] = None,
    ) -> RoutingDecision:
        """Compute the single dispatch identity for ``request``.

        Pure and deterministic over (registry, policy, request, now_iso).
        Returns a terminal decision (``is_terminal``) when no candidate meets
        the floor + policy + independence at the reached level.
        """
        registry = self._reg()
        policy = self._pol()
        self._validate_request(request, policy)
        created_at = now_iso or _now_iso_stub()

        # 1. Profile + capability requirements + entry level.
        profile = policy.profile_for_role(request.role)
        role_entry_level = _entry_level(profile, request.risk_class)
        entry_level = role_entry_level
        required_caps = list(profile["required_capabilities"])
        min_reason = profile["minimum_reasoning_level"]
        allowed = list(profile["allowed_models"])
        # Independence (F1): a closing review is ALWAYS writer-independent.  The
        # request may never weaken this to SAME_MODEL_ALLOWED.  Other roles use
        # the explicit request requirement, else the policy profile default,
        # else SAME_MODEL_ALLOWED.
        if request.role == Role.REVIEWER.value:
            independence = Independence.DIFFERENT_MODEL_REQUIRED.value
        else:
            independence = (
                request.independence_requirement
                or profile.get("independence")
                or Independence.SAME_MODEL_ALLOWED.value
            )

        # 2. Escalation profiles (security / architecture / root cause).
        triggers = detect_triggers(request.evidence)
        if RoutingReasonCode.SECURITY_COMPLEXITY.value in triggers:
            sec = policy.escalation_profile("security_review")
            # F3: the security-review REPLACE applies ONLY to the reviewer /
            # closing-review context (it shifts the task to "review security").
            # Other roles keep their own capability requirements and escalate
            # via their role's allowed_models (e.g. analyst -> Sol) — never an
            # empty intersection.
            if sec is not None and _profile_applies(sec, request.role):
                required_caps = list(sec["required_capabilities"])
                min_reason = _max_reason(min_reason, sec["minimum_reasoning_level"])
                entry_level = max(entry_level, sec["entry_level"])
                allowed = _intersect_models(allowed, sec["allowed_models"])
            else:
                # F3: role-appropriate analysis escalation (e.g. analyst on
                # HIGH-risk security complexity) — Sol for the analyst WITHOUT
                # granting SECURITY_REVIEW (the analyst's capabilities shift to
                # deep repository reasoning, not reviewer security review).
                da = policy.escalation_profile("deep_analysis")
                if da is not None and _profile_applies(da, request.role):
                    required_caps = list(da["required_capabilities"])
                    min_reason = _max_reason(min_reason, da["minimum_reasoning_level"])
                    entry_level = max(entry_level, da["entry_level"])
                    allowed = _intersect_models(allowed, da["allowed_models"])
        if (
            RoutingReasonCode.REPEATED_FIX_FAILURE.value in triggers
            and RoutingReasonCode.TESTS_STILL_RED.value in triggers
            and RoutingReasonCode.ROOT_CAUSE_UNPROVEN.value in triggers
        ):
            rca = policy.escalation_profile("root_cause_analysis")
            if rca is not None and _profile_applies(rca, request.role):
                # Role adaptation (§18): the hard problem shifts the task's
                # nature from "write code" to "analyse the hard root cause" —
                # the capability requirements are REPLACED, not merged.
                required_caps = list(rca["required_capabilities"])
                min_reason = _max_reason(min_reason, rca["minimum_reasoning_level"])
                entry_level = max(entry_level, rca["entry_level"])
                allowed = _intersect_models(allowed, rca["allowed_models"])

        # 3. Level computation (bounded, monotonic).
        base_level = entry_level
        current = max(LEVEL_ROUTINE, min(request.current_escalation_level, policy.owner_level))
        level = max(base_level, current)
        level = _apply_trigger_levels(level, current, triggers, policy, request.role)
        level = min(level, policy.owner_level)

        # MISSING_REQUIRED_EXTERNAL_INFO → owner gate (no capability escalation).
        if RoutingReasonCode.MISSING_REQUIRED_EXTERNAL_INFO.value in triggers:
            return self._terminal(
                request, required_caps, RoutingReasonCode.OWNER_GATE.value,
                level, independence, policy, created_at,
            )

        if level > policy.max_automatic_level:
            return self._terminal(
                request, required_caps, RoutingReasonCode.OWNER_GATE.value,
                level, independence, policy, created_at,
            )

        # 4. Reasoning selection: level default, clamped to [floor, ceiling].
        reasoning_level = policy.reasoning_default(level)
        reasoning_level = _max_reason(reasoning_level, min_reason)
        ceiling = policy.reasoning_ceiling(level)
        if _REASONING_RANK[reasoning_level] > _REASONING_RANK[ceiling]:
            reasoning_level = ceiling

        # 5. Eligibility filter (floor + policy authorisation + independence).
        requirements = CapabilityRequirements(
            required_capabilities=tuple(required_caps),
            minimum_reasoning_level=min_reason,
            independence_requirement=independence,
        )
        candidates = self._eligible_candidates(
            registry, policy, requirements, allowed,
            level, reasoning_level, request.reference_model_id,
        )

        # 6. Ranking (minimum sufficient; cost/latency only as tiebreakers).
        chosen = self._rank_minimum(candidates, policy, registry)

        if chosen is None:
            return self._terminal(
                request, tuple(required_caps), RoutingReasonCode.NO_ELIGIBLE_CANDIDATE.value,
                level, independence, policy, created_at,
            )

        reason_code = self._decision_reason(triggers, level, role_entry_level)

        return self._build_decision(
            request, chosen, level, reasoning_level,
            tuple(required_caps), reason_code, independence, policy, created_at,
        )

    # -- internal -----------------------------------------------------------

    def _validate_request(self, request: RoutingRequest, policy: RoutingPolicy) -> None:
        if not isinstance(request, RoutingRequest):
            raise RoutingError(ROUTING_REQUEST_INVALID, "request must be a RoutingRequest")
        if not request.job_id or not request.task_id:
            raise RoutingError(ROUTING_REQUEST_INVALID, "job_id and task_id are required")
        if request.role not in _ROLE_VALUES:
            raise RoutingError(ROUTING_REQUEST_INVALID, f"unknown role {request.role!r}")
        if request.risk_class not in _RISK_VALUES:
            raise RoutingError(ROUTING_REQUEST_INVALID, f"unknown risk_class {request.risk_class!r}")
        if request.current_escalation_level < 0:
            raise RoutingError(ROUTING_REQUEST_INVALID, "current_escalation_level must be >= 0")
        if request.independence_requirement is not None:
            _require_enum_str(
                request.independence_requirement, Independence,
                "request.independence_requirement",
            )
        if request.policy_version != ROUTING_POLICY_VERSION:
            raise RoutingError(ROUTING_REQUEST_INVALID, f"unsupported policy_version {request.policy_version!r}")

    def _eligible_candidates(
        self,
        registry: ModelRegistry,
        policy: RoutingPolicy,
        requirements: CapabilityRequirements,
        allowed: Sequence[str],
        level: int,
        reasoning_level: str,
        reference_model_id: Optional[str],
    ) -> Tuple[ModelDescriptor, ...]:
        """Candidate set: registry valid + policy-authorised + tier floor +
        capability floor + reasoning floor + independence."""
        min_tier = policy.min_tier_for_level(level)
        allowed_set = set(allowed)
        reference = None
        if reference_model_id is not None:
            reference = registry.get_model(reference_model_id)
        out = []
        for model in registry.list_models():
            if model.model_id not in allowed_set:
                continue  # bootstrap policy authorisation (never auto-eligible)
            tier = policy.tier_of(model.model_id)
            if tier is None or tier < min_tier:
                continue
            if not _model_enabled_available(registry, model):
                continue
            if not registry.satisfies_floor(model, requirements):
                continue
            if not _reasoning_level_supported(model, reasoning_level):
                continue
            if not _independence_ok(model, reference, requirements):
                continue
            out.append(model)
        return tuple(out)

    def _rank_minimum(
        self,
        candidates: Sequence[ModelDescriptor],
        policy: RoutingPolicy,
        registry: ModelRegistry,
    ) -> Optional[ModelDescriptor]:
        if not candidates:
            return None
        ordered = sorted(
            candidates,
            key=lambda m: (
                policy.tier_of(m.model_id) if policy.tier_of(m.model_id) is not None else 99,
                policy.cost_rank(m.cost_class),
                policy.latency_rank(m.latency_class),
                m.model_id,
            ),
        )
        return ordered[0]

    def _decision_reason(self, triggers: FrozenSet[str], level: int, base_level: int) -> str:
        if level > base_level:
            # Prefer the most specific capability trigger; fall back to the
            # lowest-ordered deterministic code present.
            priority = (
                RoutingReasonCode.REPEATED_FIX_FAILURE.value,
                RoutingReasonCode.ROOT_CAUSE_UNPROVEN.value,
                RoutingReasonCode.REVIEWER_REJECTED_CANDIDATE.value,
                RoutingReasonCode.CONTRADICTORY_EVIDENCE.value,
                RoutingReasonCode.CONCURRENCY_COMPLEXITY.value,
                RoutingReasonCode.UNEXPECTED_SCOPE_GROWTH.value,
                RoutingReasonCode.RECOVERY_COMPLEXITY.value,
                RoutingReasonCode.TESTS_STILL_RED.value,
                RoutingReasonCode.SECURITY_COMPLEXITY.value,
            )
            for code in priority:
                if code in triggers:
                    return code
        for code in _NON_CAPABILITY_CODES:
            if code in triggers:
                return code
        return RoutingReasonCode.MINIMUM_SUFFICIENT.value

    def _terminal(
        self,
        request: RoutingRequest,
        required_caps: Sequence[str],
        reason_code: str,
        level: int,
        independence: str,
        policy: RoutingPolicy,
        created_at: str,
    ) -> RoutingDecision:
        return self._build_decision(
            request, None, level, None, tuple(required_caps), reason_code,
            independence, policy, created_at,
        )

    def _build_decision(
        self,
        request: RoutingRequest,
        chosen: Optional[ModelDescriptor],
        level: int,
        reasoning_level: Optional[str],
        required_caps: Tuple[str, ...],
        reason_code: str,
        independence: str,
        policy: RoutingPolicy,
        created_at: str,
    ) -> RoutingDecision:
        if chosen is not None:
            provider = chosen.provider_id
            model = chosen.model_id
            matched = tuple(
                c for c in required_caps if c in chosen.capability_tags
            )
        else:
            provider = None
            model = None
            matched = ()
        evidence_refs = tuple(
            f"attempt:{a.attempt_no}" for a in request.evidence.prior_attempts
        )
        canonical = _canonical_json({
            "job_id": request.job_id,
            "task_id": request.task_id,
            "role": request.role,
            "risk_class": request.risk_class,
            "reference_model_id": request.reference_model_id,
            "independence_requirement": independence,
            "provider": provider,
            "model": model,
            "reasoning_level": reasoning_level,
            "escalation_level": level,
            "required_capabilities": sorted(required_caps),
            "reason_code": reason_code,
            "policy_version": policy.version,
            "evidence_refs": list(evidence_refs),
        })
        decision_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
        sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        requirements_hash = hashlib.sha256(_canonical_json({
            "role": request.role,
            "risk_class": request.risk_class,
            "required_capabilities": sorted(required_caps),
            "reasoning_level": reasoning_level,
            "independence_requirement": independence,
        }).encode("utf-8")).hexdigest()
        return RoutingDecision(
            decision_id=decision_id,
            job_id=request.job_id,
            task_id=request.task_id,
            role=request.role,
            dispatch_id=request.dispatch_id,
            provider=provider,
            model=model,
            reasoning_level=reasoning_level,
            escalation_level=level,
            required_capabilities=required_caps,
            matched_capabilities=matched,
            requirements_hash=requirements_hash,
            decision_reason_code=reason_code,
            policy_version=policy.version,
            evidence_refs=evidence_refs,
            reference_model_id=request.reference_model_id,
            independence_requirement=independence,
            canonical_json=canonical,
            created_at=created_at,
            sha256=sha,
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_ROLE_VALUES: FrozenSet[str] = frozenset(r.value for r in Role)
_RISK_VALUES: FrozenSet[str] = frozenset(r.value for r in RiskClass)

# F3: roles that carry a deep-reasoning SECURITY escalation (analyst deep
# analysis, reviewer security review, lead coordination).  implementer/qa keep
# their implementation capability and do NOT jump level on security complexity
# (the separate closing review carries the security review).
_SECURITY_DEEP_REASONING_ROLES: FrozenSet[str] = frozenset({
    Role.ANALYST.value,
    Role.REVIEWER.value,
    Role.LEAD.value,
})


def _now_iso_stub() -> str:
    # Deterministic fallback timestamp (tests always pass an explicit now_iso).
    from .store import utcnow
    return utcnow().isoformat()


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _entry_level(profile: Dict[str, Any], risk_class: str) -> int:
    low_entry = profile.get("low_risk_entry_level")
    if low_entry is not None and risk_class == RiskClass.LOW.value:
        return low_entry
    return profile["entry_level"]


def _intersect_models(base: Sequence[str], extra: Sequence[str]) -> list:
    extra_set = set(extra)
    return [m for m in base if m in extra_set]


def _profile_applies(profile: Dict[str, Any], role: str) -> bool:
    """Whether a (escalation) profile applies to a role (F3).

    A profile with no ``roles`` key applies to every role (capability-only); a
    profile with ``roles`` applies only to those roles.  This keeps the
    security-review REPLACE scoped to the reviewer context so an analyst on
    HIGH-risk escalates via its own allowed_models (Sol) instead of an empty
    intersection.
    """
    roles = profile.get("roles") or ()
    if not roles:
        return True
    return role in roles


def _max_reason(a: str, b: str) -> str:
    return a if _REASONING_RANK[a] >= _REASONING_RANK[b] else b


def _model_enabled_available(registry: ModelRegistry, model: ModelDescriptor) -> bool:
    if not model.enabled:
        return False
    if model.lifecycle_state != "ACTIVE":
        return False
    provider = registry.get_provider(model.provider_id)
    if provider is None or not provider.enabled:
        return False
    return provider.availability_state in ("AVAILABLE", "DEGRADED")


def _reasoning_level_supported(model: ModelDescriptor, level: str) -> bool:
    floor = _REASONING_RANK[level]
    return any(
        _REASONING_RANK[r] >= floor for r in model.reasoning_levels_supported
    )


def _independence_ok(
    model: ModelDescriptor,
    reference: Optional[ModelDescriptor],
    requirements: CapabilityRequirements,
) -> bool:
    constraint = requirements.independence_requirement
    if constraint == Independence.SAME_MODEL_ALLOWED.value:
        return True
    if reference is None:
        # A hard independence constraint without a reference cannot be evaluated
        # -> fail closed (no candidates), matching E1 semantics.
        return False
    if constraint == Independence.DIFFERENT_MODEL_REQUIRED.value:
        return model.model_id != reference.model_id
    if constraint == Independence.DIFFERENT_PROVIDER_REQUIRED.value:
        return model.provider_id != reference.provider_id
    if constraint == Independence.DIFFERENT_PROVIDER_PREFERRED.value:
        return True
    return True
