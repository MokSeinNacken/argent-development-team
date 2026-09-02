"""Phase E1 — Provider/Model abstraction + Capability Registry.

This module is the single, provider-neutral authority for the **static** model
and provider identity that Phase E (adaptive roles / model routing, E2) will
build on.  It performs **no I/O at runtime, no provider calls, no credential
handling and no dynamic routing**: it is a pure, deterministic, fail-closed
description of *what models exist and what capabilities they claim*.

Fundamental invariant (verbindlich): **roles are capabilities; models are
interchangeable implementations.**  There is NO ``if provider == "deepseek"``
branch in the core registry code — providers are data.  The core works with
abstract ``ProviderDescriptor`` / ``ModelDescriptor`` / ``CapabilityRequirements``
objects.

What E1 does NOT implement (by explicit scope): dynamic model selection, any
role→model *decision*, escalation, automatic fallback *execution*, new providers
(Claude/GLM/Gemini/Qwen), credentials, benchmarks, a background service,
parallelisation, DB-schema changes, or a secret-handling architecture.

Security invariants (verbindlich):

* **No secrets.**  ``credential_ref`` / ``auth_mode`` are opaque references,
  never a key/token/password.  Nothing in the registry can hold a secret.
* **Claims, not facts.**  Every capability tag / reasoning level / tool claim /
  cost / latency / reliability class carries a ``ClaimProvenance(source=...,
  benchmarked=False)``.  Nothing here asserts "X is better than Y" without
  benchmark/policy evidence (E3 benchmarks).
* **No agent mutation.**  There is no API by which an agent (or a prompt) can
  authorise a model, enable a provider, raise a capability claim, lower a
  quality floor, force a fallback, or change an independence constraint.
  ``ModelRegistry`` and its descriptors are immutable.
* **Fail-closed loading.**  A malformed registry is refused in full — never
  partially loaded.

Persistence: the registry is a **versioned local file set** (``argent_core/
registry/*.json``) checked into the repo, with strict load validation.  Runtime
availability is never polled (there is no live polling); ``UNKNOWN`` is never
invented as ``AVAILABLE``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, Optional, Sequence, Tuple
from urllib.parse import urlparse

from .job_state import ErrorClass
from .models import ArgentError

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

REGISTRY_VERSION = "1"

#: Policy version for registry claims (same bounded value in E1).  Document
#: ``policy_version`` and every entry ``policy_version`` must equal this.
POLICY_VERSION = "1"

# ---------------------------------------------------------------------------
# Bounded enums (taxonomy)
# ---------------------------------------------------------------------------


class ProviderType(str, Enum):
    """How a provider is integrated (bounded; a provider is NOT agent-defined)."""

    OPENAI_COMPLETIONS_API = "openai-completions"
    OAUTH_PLUGIN = "oauth-plugin"


class AvailabilityState(str, Enum):
    """Bounded availability of a provider/model (no live polling in E1)."""

    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    DISABLED = "DISABLED"
    UNKNOWN = "UNKNOWN"


class LifecycleState(str, Enum):
    """Bounded lifecycle of a model descriptor."""

    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    RETIRED = "RETIRED"


class ReasoningLevel(str, Enum):
    """Bounded reasoning levels (mapping of real, existing levels).

    ``LOW``/``MEDIUM``/``HIGH`` are a model *capability* and a dispatch *policy*
    axis — never a separate model identity.  In the current configuration only
    ``HIGH`` (sol) and ``MEDIUM`` (pro/flash) are mapped; ``LOW`` is reserved.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


_REASONING_RANK: Dict[ReasoningLevel, int] = {
    ReasoningLevel.LOW: 0,
    ReasoningLevel.MEDIUM: 1,
    ReasoningLevel.HIGH: 2,
}


class Capability(str, Enum):
    """Bounded capability taxonomy (versioned, extensible, NOT agent-driven).

    Deliberately coarse — no micro-capabilities.  This is the single vocabulary
    for capability requirements, model capability tags and provider capability
    support in Phase E.
    """

    COORDINATION = "COORDINATION"
    SIMPLE_ANALYSIS = "SIMPLE_ANALYSIS"
    CODE_IMPLEMENTATION = "CODE_IMPLEMENTATION"
    COMPLEX_CODE_IMPLEMENTATION = "COMPLEX_CODE_IMPLEMENTATION"
    DEBUGGING = "DEBUGGING"
    REPOSITORY_REASONING = "REPOSITORY_REASONING"
    ARCHITECTURE = "ARCHITECTURE"
    SECURITY_REVIEW = "SECURITY_REVIEW"
    CODE_REVIEW = "CODE_REVIEW"
    ROOT_CAUSE_ANALYSIS = "ROOT_CAUSE_ANALYSIS"
    TOOL_USE = "TOOL_USE"
    LONG_CONTEXT = "LONG_CONTEXT"
    VISION = "VISION"
    STRUCTURED_OUTPUT = "STRUCTURED_OUTPUT"


class CostClass(str, Enum):
    """Bounded cost class (LOW/MEDIUM/HIGH; UNKNOWN = no verified data)."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class LatencyClass(str, Enum):
    """Bounded latency class (LOW/MEDIUM/HIGH; UNKNOWN = no verified data)."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class ReliabilityClass(str, Enum):
    """Bounded reliability class (LOW/MEDIUM/HIGH; UNKNOWN = no benchmark data)."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class Independence(str, Enum):
    """Bounded independence semantics between a candidate and a reference model.

    * ``SAME_MODEL_ALLOWED``       — no constraint.
    * ``DIFFERENT_MODEL_REQUIRED`` — candidate must be a different model id.
    * ``DIFFERENT_PROVIDER_PREFERRED`` — preference (soft hint, no ordering in E1).
    * ``DIFFERENT_PROVIDER_REQUIRED``  — candidate must be a different provider.

    A writer model can therefore never serve as its own closing reviewer when a
    ``DIFFERENT_*_REQUIRED`` constraint is expressed against it.
    """

    SAME_MODEL_ALLOWED = "SAME_MODEL_ALLOWED"
    DIFFERENT_MODEL_REQUIRED = "DIFFERENT_MODEL_REQUIRED"
    DIFFERENT_PROVIDER_PREFERRED = "DIFFERENT_PROVIDER_PREFERRED"
    DIFFERENT_PROVIDER_REQUIRED = "DIFFERENT_PROVIDER_REQUIRED"


class ToolCapability(str, Enum):
    """Bounded tool-capability *claims* (NOT permissions — see spec §16).

    A tool capability describes what a model *can* drive; the actual right to
    use a tool remains with the Supervisor / Policy / Task scope / trust
    boundary (``roles.py`` permissions and tool allow-lists are unchanged).
    """

    CODE_EDIT = "code_edit"
    SHELL_EXEC = "shell_exec"
    WEB_SEARCH = "web_search"
    FILE_ACCESS = "file_access"


class CodingMode(str, Enum):
    """Bounded coding-ability axis for the vision/coding/review metadata."""

    NONE = "none"
    IMPLEMENTATION = "implementation"
    REVIEW = "review"


# ---------------------------------------------------------------------------
# Bounded error codes + error class
# ---------------------------------------------------------------------------

PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
MODEL_NOT_ALLOWED = "MODEL_NOT_ALLOWED"
CAPABILITY_FLOOR_UNMET = "CAPABILITY_FLOOR_UNMET"
MODEL_CONFIG_INVALID = "MODEL_CONFIG_INVALID"

MODEL_REGISTRY_ERROR_CODES: FrozenSet[str] = frozenset({
    PROVIDER_UNAVAILABLE,
    MODEL_UNAVAILABLE,
    MODEL_NOT_ALLOWED,
    CAPABILITY_FLOOR_UNMET,
    MODEL_CONFIG_INVALID,
})


class ModelRegistryError(ArgentError):
    """A provider/model registry violation (bounded ``code``, fail-closed).

    This is NEVER a CODE/RESOURCE/CONTEXT failure: it carries no escalation
    semantics and no automatic fallback.  Its ``error_class`` is ``PROVIDER``
    (the additive Phase-E1 class, mapped only where a registry error actually
    occurs).  Laufzeit-Providerausfälle (Netz) remain job-side EXTERNAL/
    TRANSIENT (Phase B/C).
    """

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)

    @property
    def error_class(self) -> str:
        return ErrorClass.PROVIDER.value


def registry_error_class(exc: ModelRegistryError) -> str:
    """Map a registry error to its (bounded) ``ErrorClass`` — ``PROVIDER``."""
    return ErrorClass.PROVIDER.value


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

#: Bounded id pattern: lowercase letters/digits/dot/dash, no whitespace/colon.
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
_MAX_ID_LEN = 128
_MAX_REF_LEN = 256

#: Secret markers used to reject a credential_ref that carries an actual secret.
_SECRET_MARKERS: Tuple[str, ...] = (
    "sk-", "ghp_", "gho_", "ghs_", "github_pat_", "AKIA", "xoxb-", "xoxp-",
    "Bearer ", "AIza",
)

#: Secret *key names* (case-/separator-insensitive).  A registry document that
#: carries one of these key names (even as an "unknown" field) is refused — it
#: signals an attempt to smuggle a credential into the registry.
_SECRET_KEY_NAMES: FrozenSet[str] = frozenset({
    "apikey", "token", "secret", "password", "privatekey", "authorization",
})


def _normalized_key_name(key: str) -> str:
    """Normalize a field name for secret-key detection (drop separators)."""
    return re.sub(r"[_-]", "", key.lower())


#: Bounded trusted-local authority prefixes for ``ClaimProvenance.source``.
#: Registry claims may only cite these local, read-only, checked-in authorities
#: (repo files, routing table, architecture doc, local config).  ``test`` is
#: permitted for test-only fixtures.
_TRUSTED_SOURCE_AUTHORITIES: Tuple[str, ...] = (
    "openclaw.json",
    "routing.py",
    "docs-",
    "architecture",
    "local-config",
    "test",
)

#: Agent-origin / self-report markers that MUST be rejected (fail-closed).  A
#: provenance source may never claim agent/model/prompt/handoff authorship —
#: claims must trace back to a trusted-local authority, never to an agent.
_AGENT_ORIGIN_MARKERS: Tuple[str, ...] = (
    "agent-output",
    "model-self-report",
    "self-report",
    "model-output",
    "prompt",
    "handoff",
)

#: Bounded opaque-reference grammar (credential_ref / auth_mode / profile_ref):
#: letters, digits, ``:`` ``.`` ``_`` ``/`` ``-`` only — no ``@``, no whitespace,
#: no ``://`` URL-scheme/authority (which could smuggle userinfo).
_OPAQUE_REF_PATTERN = re.compile(r"^[A-Za-z0-9:._/-]+$")


#: Exact key allow-lists (fail-closed: any other key anywhere is refused).
_DOC_KEYS: FrozenSet[str] = frozenset({
    "registry_version", "policy_version", "providers", "models",
})
_PROVIDER_KEYS: FrozenSet[str] = frozenset({
    "provider_id", "provider_type", "display_name", "enabled",
    "availability_state", "capabilities_supported", "credential_ref",
    "auth_mode", "endpoint_ref", "profile_ref", "policy_version",
})
_MODEL_KEYS: FrozenSet[str] = frozenset({
    "model_id", "provider_id", "canonical_model_name", "enabled",
    "lifecycle_state", "context_window_metadata", "output_limit_metadata",
    "reasoning_levels_supported", "tool_capabilities", "abilities",
    "latency_class", "cost_class", "reliability_class", "capability_tags",
    "policy_version", "provenance",
})
_ABILITIES_KEYS: FrozenSet[str] = frozenset({"vision", "coding", "review"})
_PROVENANCE_KEYS: FrozenSet[str] = frozenset({"source", "benchmarked"})


def _reject_unknown_keys(
    raw: Dict[str, Any], allowed: FrozenSet[str], context: str
) -> None:
    """Refuse any field outside the exact allow-list (secret key names first)."""
    for key in raw:
        if key in allowed:
            continue
        if _normalized_key_name(key) in _SECRET_KEY_NAMES:
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID,
                f"{context}: secret key name {key!r} is not allowed",
            )
        raise ModelRegistryError(
            MODEL_CONFIG_INVALID, f"{context}: unknown field {key!r}",
        )


def _validate_source(source: str) -> str:
    """Validate a ``ClaimProvenance.source`` against the bounded authority rule.

    A source must cite a trusted-local authority (prefix match) and must NOT
    claim agent/model/prompt/handoff origin.  Fail-closed on either violation.
    """
    low = source.lower()
    for marker in _AGENT_ORIGIN_MARKERS:
        if marker in low:
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID,
                f"provenance.source {source!r} claims agent origin "
                f"(marker {marker!r})",
            )
    for authority in _TRUSTED_SOURCE_AUTHORITIES:
        if low.startswith(authority):
            return source
    raise ModelRegistryError(
        MODEL_CONFIG_INVALID,
        f"provenance.source {source!r} is not a trusted-local authority "
        f"(allowed prefixes: {', '.join(_TRUSTED_SOURCE_AUTHORITIES)})",
    )


@dataclass(frozen=True)
class ClaimProvenance:
    """Where a claim came from (a local, read-only, evidence-tagged source)."""

    source: str
    benchmarked: bool = False


@dataclass(frozen=True)
class ModelAbilities:
    """Vision / coding / review capability metadata (claims, not facts)."""

    vision: Optional[bool] = None
    coding: Optional[str] = None      # CodingMode value (or None)
    review: Optional[bool] = None


@dataclass(frozen=True)
class ProviderDescriptor:
    """Versioned provider descriptor (provider is NOT agent-definable)."""

    provider_id: str
    provider_type: str
    display_name: str
    enabled: bool
    availability_state: str
    capabilities_supported: tuple = ()      # Capability values
    credential_ref: Optional[str] = None    # opaque reference, NEVER a secret
    auth_mode: Optional[str] = None         # opaque reference, NEVER a secret
    endpoint_ref: Optional[str] = None      # non-secret endpoint (baseUrl)
    profile_ref: Optional[str] = None       # non-secret profile reference
    policy_version: str = REGISTRY_VERSION


@dataclass(frozen=True)
class ModelDescriptor:
    """Versioned model descriptor (model is an interchangeable implementation)."""

    model_id: str
    provider_id: str
    canonical_model_name: str
    enabled: bool
    lifecycle_state: str
    context_window_metadata: Optional[int] = None   # tokens, None = unknown
    output_limit_metadata: Optional[int] = None     # tokens, None = unknown
    reasoning_levels_supported: tuple = ()          # ReasoningLevel values
    tool_capabilities: tuple = ()                   # ToolCapability values (claims)
    abilities: ModelAbilities = ModelAbilities()    # vision/coding/review metadata
    latency_class: str = LatencyClass.UNKNOWN.value
    cost_class: str = CostClass.UNKNOWN.value
    reliability_class: str = ReliabilityClass.UNKNOWN.value
    capability_tags: tuple = ()                     # Capability values
    policy_version: str = REGISTRY_VERSION
    provenance: ClaimProvenance = ClaimProvenance(source="", benchmarked=False)


# ---------------------------------------------------------------------------
# Capability Requirements (trusted; E1 provides candidates, never selects)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityRequirements:
    """Trusted capability/quality requirements for a (future) candidate set.

    E1 does **not** decide: it only computes the candidate set that satisfies
    these requirements (``eligible_models``).  An agent may *recommend*
    requirements as text; the controller / policy is authoritative.  There is
    no cost-based selection or "cheaper is better" logic anywhere.
    """

    required_capabilities: tuple = ()                       # Capability values
    optional_capabilities: tuple = ()                       # Capability values
    minimum_reasoning_level: str = ReasoningLevel.LOW.value
    tool_requirements: tuple = ()                           # ToolCapability values
    context_requirement: Optional[int] = None               # min context tokens
    independence_requirement: str = Independence.SAME_MODEL_ALLOWED.value
    quality_floor: str = ReliabilityClass.UNKNOWN.value     # min reliability class

    def __post_init__(self) -> None:
        # Canonicalize (and fail-closed validate) every field at construction so
        # the frozen instance is always valid.  Sequence fields must be a list or
        # tuple, are canonicalized to a frozen tuple of canonical ``.value``
        # strings, and duplicates are refused.  Enum scalars are canonicalized to
        # ``.value``.  ``context_requirement`` must be a non-negative int and is
        # NEVER a ``bool`` (a bool is an ``int`` subclass, so it is rejected
        # explicitly).  Every violation is a bounded ``ModelRegistryError``, never
        # a raw ``TypeError``/``ValueError``.
        object.__setattr__(
            self, "required_capabilities",
            _canonicalize_capability_seq(
                self.required_capabilities, Capability, "required_capabilities"),
        )
        object.__setattr__(
            self, "optional_capabilities",
            _canonicalize_capability_seq(
                self.optional_capabilities, Capability, "optional_capabilities"),
        )
        object.__setattr__(
            self, "tool_requirements",
            _canonicalize_capability_seq(
                self.tool_requirements, ToolCapability, "tool_requirements"),
        )
        object.__setattr__(
            self, "minimum_reasoning_level",
            _canonical_enum_value(
                self.minimum_reasoning_level, ReasoningLevel,
                "minimum_reasoning_level"),
        )
        object.__setattr__(
            self, "independence_requirement",
            _canonical_enum_value(
                self.independence_requirement, Independence,
                "independence_requirement"),
        )
        object.__setattr__(
            self, "quality_floor",
            _canonical_enum_value(
                self.quality_floor, ReliabilityClass, "quality_floor"),
        )
        cr = self.context_requirement
        if cr is not None:
            if isinstance(cr, bool) or not isinstance(cr, int) or cr < 0:
                raise ModelRegistryError(
                    MODEL_CONFIG_INVALID,
                    f"invalid context_requirement {cr!r} "
                    "(must be a non-negative int, never bool)",
                )

    def validate(self) -> None:
        """Fail-closed validation of every field (bounded enums + bounds).

        Idempotent: construction (``__post_init__``) already canonicalizes and
        validates, but the candidate/floor methods call this first as the
        explicit public gate.
        """
        for cap in self.required_capabilities + self.optional_capabilities:
            _require_enum(cap, Capability, "capability")
        _require_enum(self.minimum_reasoning_level, ReasoningLevel, "reasoning level")
        for tool in self.tool_requirements:
            _require_enum(tool, ToolCapability, "tool requirement")
        _require_enum(self.independence_requirement, Independence, "independence")
        _require_enum(self.quality_floor, ReliabilityClass, "quality floor")
        if self.context_requirement is not None:
            if isinstance(self.context_requirement, bool) or \
                    not isinstance(self.context_requirement, int) or \
                    self.context_requirement < 0:
                raise ModelRegistryError(
                    MODEL_CONFIG_INVALID,
                    f"invalid context_requirement {self.context_requirement!r}",
                )


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _require_enum(value: Any, enum_cls: type, label: str) -> None:
    try:
        enum_cls(value)
    except (ValueError, TypeError):
        raise ModelRegistryError(
            MODEL_CONFIG_INVALID,
            f"invalid {label} {value!r}; expected one of "
            f"{sorted(m.value for m in enum_cls)}",
        ) from None


def _canonical_enum_value(value: Any, enum_cls: type, label: str) -> str:
    """Return the canonical ``.value`` of a bounded enum (member or value string).

    Accepts an existing enum member or its ``.value`` string; raises
    ``ModelRegistryError(MODEL_CONFIG_INVALID)`` otherwise (never a raw
    ``TypeError``/``ValueError``).
    """
    try:
        return enum_cls(value).value
    except (ValueError, TypeError):
        raise ModelRegistryError(
            MODEL_CONFIG_INVALID,
            f"invalid {label} {value!r}; expected one of "
            f"{sorted(m.value for m in enum_cls)}",
        ) from None


def _canonicalize_capability_seq(value: Any, enum_cls: type, label: str) -> tuple:
    """Canonicalize a capability/tool sequence to a frozen tuple (no duplicates).

    ``list``/``tuple`` of enum members or ``.value`` strings -> frozen tuple of
    canonical ``.value`` strings.  Anything else (``str``, ``bool``, ``dict``,
    ``int``) is refused; duplicates are refused.
    """
    if not isinstance(value, (list, tuple)):
        raise ModelRegistryError(
            MODEL_CONFIG_INVALID,
            f"{label} must be a list or tuple, got {value!r}",
        )
    out = []
    for item in value:
        out.append(_canonical_enum_value(item, enum_cls, label))
    if len(out) != len(set(out)):
        raise ModelRegistryError(
            MODEL_CONFIG_INVALID, f"{label} must not contain duplicates",
        )
    return tuple(out)


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ModelRegistryError(MODEL_CONFIG_INVALID,
                                 f"{label} must be a bool, got {value!r}")
    return value


def _require_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ModelRegistryError(MODEL_CONFIG_INVALID,
                                 f"{label} must be a non-empty string")
    if len(value) > _MAX_ID_LEN:
        raise ModelRegistryError(MODEL_CONFIG_INVALID,
                                 f"{label} {value!r} exceeds {_MAX_ID_LEN} chars")
    if not _ID_PATTERN.match(value):
        raise ModelRegistryError(
            MODEL_CONFIG_INVALID,
            f"{label} {value!r} is not a valid id (lowercase [a-z0-9.-])",
        )
    return value


def _require_opaque_ref(value: Any, label: str) -> Optional[str]:
    """Validate an opaque reference (never a secret, bounded grammar).

    ``credential_ref`` / ``auth_mode`` / ``profile_ref`` are opaque references:
    bounded charset, no ``@``, no whitespace, no URL scheme with authority/
    userinfo, and no secret marker.  ``None`` is permitted.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ModelRegistryError(MODEL_CONFIG_INVALID,
                                 f"{label} must be a non-empty string or null")
    if len(value) > _MAX_REF_LEN:
        raise ModelRegistryError(MODEL_CONFIG_INVALID,
                                 f"{label} exceeds {_MAX_REF_LEN} chars")
    if "://" in value or "@" in value:
        raise ModelRegistryError(
            MODEL_CONFIG_INVALID,
            f"{label} must not carry a URL scheme/authority or userinfo",
        )
    if not _OPAQUE_REF_PATTERN.match(value):
        raise ModelRegistryError(
            MODEL_CONFIG_INVALID,
            f"{label} has an invalid character (bounded charset [A-Za-z0-9:._/-])",
        )
    low = value.lower()
    for marker in _SECRET_MARKERS:
        if marker.lower() in low:
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID,
                f"{label} looks like a secret (marker {marker!r})",
            )
    return value


def _require_endpoint_ref(value: Any, label: str = "endpoint_ref") -> Optional[str]:
    """Validate an endpoint reference: null OR an http(s) URL with NO userinfo.

    ``endpoint_ref`` may hold a non-secret base URL (e.g.
    ``https://api.deepseek.com``).  Any userinfo (username/password) or a
    non-http(s) scheme is refused fail-closed.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ModelRegistryError(MODEL_CONFIG_INVALID,
                                 f"{label} must be a non-empty string or null")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ModelRegistryError(
            MODEL_CONFIG_INVALID,
            f"{label} must be an http(s) URL, got {value!r}",
        )
    if parsed.username is not None or parsed.password is not None:
        raise ModelRegistryError(
            MODEL_CONFIG_INVALID,
            f"{label} must not carry userinfo (username/password)",
        )
    if not parsed.hostname:
        raise ModelRegistryError(
            MODEL_CONFIG_INVALID, f"{label} must have a host, got {value!r}",
        )
    return value


def _require_bounded_int(value: Any, label: str, maximum: int) -> Optional[int]:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ModelRegistryError(MODEL_CONFIG_INVALID,
                                 f"{label} must be an int or null, got {value!r}")
    if value < 0 or value > maximum:
        raise ModelRegistryError(
            MODEL_CONFIG_INVALID,
            f"{label} {value!r} out of bounds [0, {maximum}]",
        )
    return value


#: Upper bound for context/output metadata (a conservative ceiling that keeps a
#: malformed registry from minting an absurd context window).
_MAX_CONTEXT_TOKENS = 10_000_000
_MAX_OUTPUT_TOKENS = 1_000_000


def _reasoning_satisfies(supported: tuple, minimum: ReasoningLevel) -> bool:
    """``True`` iff the model supports a level >= ``minimum``."""
    floor = _REASONING_RANK[minimum]
    for level in supported:
        if _REASONING_RANK[ReasoningLevel(level)] >= floor:
            return True
    return False


_CLASS_RANK: Dict[str, int] = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def _class_satisfies(actual: str, minimum: str) -> bool:
    """Reliability-class floor check; ``UNKNOWN`` never satisfies a concrete floor."""
    if minimum == ReliabilityClass.UNKNOWN.value:
        return True  # no reliability floor required
    if actual == ReliabilityClass.UNKNOWN.value:
        return False  # fail-closed: unknown reliability cannot meet a floor
    return _CLASS_RANK[actual] >= _CLASS_RANK[minimum]


# ---------------------------------------------------------------------------
# Descriptor construction from raw dicts (strict, fail-closed)
# ---------------------------------------------------------------------------


def _parse_provider(raw: Dict[str, Any], policy_version: str = POLICY_VERSION) -> ProviderDescriptor:
    if not isinstance(raw, dict):
        raise ModelRegistryError(MODEL_CONFIG_INVALID,
                                 "provider entry must be an object")
    _reject_unknown_keys(raw, _PROVIDER_KEYS, "provider entry")
    provider_id = _require_id(raw.get("provider_id"), "provider_id")
    provider_type = raw.get("provider_type")
    _require_enum(provider_type, ProviderType, "provider_type")
    display_name = raw.get("display_name")
    if not isinstance(display_name, str) or not display_name:
        raise ModelRegistryError(MODEL_CONFIG_INVALID, "display_name must be non-empty")
    enabled = _require_bool(raw.get("enabled"), "enabled")
    availability = raw.get("availability_state")
    _require_enum(availability, AvailabilityState, "availability_state")
    caps = tuple(_parse_enum_list(raw.get("capabilities_supported"),
                                  Capability, "capabilities_supported"))
    credential_ref = _require_opaque_ref(raw.get("credential_ref"), "credential_ref")
    auth_mode = _require_opaque_ref(raw.get("auth_mode"), "auth_mode")
    endpoint_ref = _require_endpoint_ref(raw.get("endpoint_ref"), "endpoint_ref")
    profile_ref = _require_opaque_ref(raw.get("profile_ref"), "profile_ref")
    entry_policy = _require_id(raw.get("policy_version", policy_version),
                               "policy_version")
    if entry_policy != policy_version:
        raise ModelRegistryError(
            MODEL_CONFIG_INVALID,
            f"provider {provider_id!r} policy_version {entry_policy!r} "
            f"!= document policy_version {policy_version!r}",
        )
    return ProviderDescriptor(
        provider_id=provider_id,
        provider_type=ProviderType(provider_type).value,
        display_name=display_name,
        enabled=enabled,
        availability_state=AvailabilityState(availability).value,
        capabilities_supported=caps,
        credential_ref=credential_ref,
        auth_mode=auth_mode,
        endpoint_ref=endpoint_ref,
        profile_ref=profile_ref,
        policy_version=entry_policy,
    )


def _parse_enum_list(value: Any, enum_cls: type, label: str) -> tuple:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ModelRegistryError(MODEL_CONFIG_INVALID, f"{label} must be a list")
    out = []
    for item in value:
        _require_enum(item, enum_cls, label)
        out.append(enum_cls(item).value)
    return tuple(out)


def _parse_model(raw: Dict[str, Any], policy_version: str = POLICY_VERSION) -> ModelDescriptor:
    if not isinstance(raw, dict):
        raise ModelRegistryError(MODEL_CONFIG_INVALID, "model entry must be an object")
    _reject_unknown_keys(raw, _MODEL_KEYS, "model entry")
    model_id = _require_id(raw.get("model_id"), "model_id")
    provider_id = _require_id(raw.get("provider_id"), "model.provider_id")
    name = raw.get("canonical_model_name")
    if not isinstance(name, str) or not name:
        raise ModelRegistryError(MODEL_CONFIG_INVALID,
                                 "canonical_model_name must be non-empty")
    enabled = _require_bool(raw.get("enabled"), "enabled")
    lifecycle = raw.get("lifecycle_state")
    _require_enum(lifecycle, LifecycleState, "lifecycle_state")
    ctx = _require_bounded_int(raw.get("context_window_metadata"),
                               "context_window_metadata", _MAX_CONTEXT_TOKENS)
    out = _require_bounded_int(raw.get("output_limit_metadata"),
                               "output_limit_metadata", _MAX_OUTPUT_TOKENS)
    reasoning = tuple(_parse_enum_list(raw.get("reasoning_levels_supported"),
                                       ReasoningLevel, "reasoning_levels_supported"))
    tools = tuple(_parse_enum_list(raw.get("tool_capabilities"),
                                   ToolCapability, "tool_capabilities"))
    latency = raw.get("latency_class", LatencyClass.UNKNOWN.value)
    _require_enum(latency, LatencyClass, "latency_class")
    cost = raw.get("cost_class", CostClass.UNKNOWN.value)
    _require_enum(cost, CostClass, "cost_class")
    reliability = raw.get("reliability_class", ReliabilityClass.UNKNOWN.value)
    _require_enum(reliability, ReliabilityClass, "reliability_class")
    tags = tuple(_parse_enum_list(raw.get("capability_tags"),
                                  Capability, "capability_tags"))
    entry_policy = _require_id(raw.get("policy_version", policy_version),
                               "policy_version")
    if entry_policy != policy_version:
        raise ModelRegistryError(
            MODEL_CONFIG_INVALID,
            f"model {model_id!r} policy_version {entry_policy!r} "
            f"!= document policy_version {policy_version!r}",
        )

    abilities = _parse_abilities(raw.get("abilities"))
    provenance = _parse_provenance(raw.get("provenance"))

    return ModelDescriptor(
        model_id=model_id,
        provider_id=provider_id,
        canonical_model_name=name,
        enabled=enabled,
        lifecycle_state=LifecycleState(lifecycle).value,
        context_window_metadata=ctx,
        output_limit_metadata=out,
        reasoning_levels_supported=reasoning,
        tool_capabilities=tools,
        abilities=abilities,
        latency_class=LatencyClass(latency).value,
        cost_class=CostClass(cost).value,
        reliability_class=ReliabilityClass(reliability).value,
        capability_tags=tags,
        policy_version=entry_policy,
        provenance=provenance,
    )


def _parse_abilities(raw: Any) -> ModelAbilities:
    """Parse the ``abilities`` sub-object (fail-closed; no truthiness coercion).

    Missing -> default ``ModelAbilities()``; present must be a dict; unknown
    keys are refused.  ``[]``/``""``/``False`` are NOT treated as ``{}``.
    """
    if raw is None:
        return ModelAbilities()
    if not isinstance(raw, dict):
        raise ModelRegistryError(MODEL_CONFIG_INVALID,
                                 "abilities must be an object (not a list/string/bool)")
    _reject_unknown_keys(raw, _ABILITIES_KEYS, "abilities")
    vision = raw.get("vision")
    if vision is not None and not isinstance(vision, bool):
        raise ModelRegistryError(MODEL_CONFIG_INVALID, "abilities.vision must be bool/null")
    coding = raw.get("coding")
    if coding is not None:
        _require_enum(coding, CodingMode, "abilities.coding")
        coding = CodingMode(coding).value
    review = raw.get("review")
    if review is not None and not isinstance(review, bool):
        raise ModelRegistryError(MODEL_CONFIG_INVALID, "abilities.review must be bool/null")
    return ModelAbilities(vision=vision, coding=coding, review=review)


def _parse_provenance(raw: Any) -> ClaimProvenance:
    """Parse the ``provenance`` sub-object (fail-closed).

    Missing -> ``ClaimProvenance(source="", benchmarked=False)``; present must
    be a dict; unknown keys are refused.  Registry version 1 requires
    ``benchmarked is False`` (claims, not benchmark facts — E3 benchmarks).  The
    source must be a trusted-local authority (bounded allowlist).
    """
    if raw is None:
        return ClaimProvenance(source="", benchmarked=False)
    if not isinstance(raw, dict):
        raise ModelRegistryError(MODEL_CONFIG_INVALID,
                                 "provenance must be an object")
    _reject_unknown_keys(raw, _PROVENANCE_KEYS, "provenance")
    source = raw.get("source")
    if not isinstance(source, str) or not source:
        raise ModelRegistryError(MODEL_CONFIG_INVALID,
                                 "provenance.source must be a non-empty string")
    _validate_source(source)
    benchmarked = raw.get("benchmarked", False)
    if not isinstance(benchmarked, bool):
        raise ModelRegistryError(MODEL_CONFIG_INVALID,
                                 "provenance.benchmarked must be a bool")
    if benchmarked is not False:
        raise ModelRegistryError(
            MODEL_CONFIG_INVALID,
            "provenance.benchmarked must be False in registry version 1 "
            "(claims, not benchmark facts)",
        )
    return ClaimProvenance(source=source, benchmarked=benchmarked)


# ---------------------------------------------------------------------------
# ModelRegistry
# ---------------------------------------------------------------------------


class ModelRegistry:
    """Immutable, fail-closed registry of providers + models.

    Construction is via :meth:`from_payload` (validates raw dicts) or
    :meth:`load_files` (reads + validates the versioned repo files).  The
    resulting object exposes **read-only** access only — there is no mutation
    API (no agent can add a model, enable a provider, or raise a claim).
    """

    def __init__(
        self,
        providers: Dict[str, ProviderDescriptor],
        models: Dict[str, ModelDescriptor],
        version: str = REGISTRY_VERSION,
    ):
        # Version consistency: the registry version is bounded (REGISTRY_VERSION).
        if version != REGISTRY_VERSION:
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID,
                f"registry version {version!r} != {REGISTRY_VERSION!r}",
            )
        if not isinstance(providers, dict) or not isinstance(models, dict):
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID,
                "providers and models must be dicts keyed by id",
            )
        # Key <-> descriptor id equality + frozen descriptor instances + entry
        # policy_version consistency.  This makes construction effectively
        # factory-only: only ``from_payload``/``load_files`` can produce a
        # consistent registry; direct construction with inconsistent data fails
        # closed.
        for key, pd in providers.items():
            if not isinstance(pd, ProviderDescriptor):
                raise ModelRegistryError(
                    MODEL_CONFIG_INVALID,
                    f"provider {key!r} is not a ProviderDescriptor",
                )
            if key != pd.provider_id:
                raise ModelRegistryError(
                    MODEL_CONFIG_INVALID,
                    f"provider key {key!r} != descriptor id {pd.provider_id!r}",
                )
            if pd.policy_version != POLICY_VERSION:
                raise ModelRegistryError(
                    MODEL_CONFIG_INVALID,
                    f"provider {key!r} policy_version {pd.policy_version!r} "
                    f"!= {POLICY_VERSION!r}",
                )
        for key, md in models.items():
            if not isinstance(md, ModelDescriptor):
                raise ModelRegistryError(
                    MODEL_CONFIG_INVALID,
                    f"model {key!r} is not a ModelDescriptor",
                )
            if key != md.model_id:
                raise ModelRegistryError(
                    MODEL_CONFIG_INVALID,
                    f"model key {key!r} != descriptor id {md.model_id!r}",
                )
            if md.policy_version != POLICY_VERSION:
                raise ModelRegistryError(
                    MODEL_CONFIG_INVALID,
                    f"model {key!r} policy_version {md.policy_version!r} "
                    f"!= {POLICY_VERSION!r}",
                )
        self._version = version
        self._providers = MappingProxyType(dict(providers))
        self._models = MappingProxyType(dict(models))
        self._validate_cross_references()

    # -- construction -------------------------------------------------------

    @classmethod
    def from_payload(
        cls,
        providers: Sequence[Dict[str, Any]],
        models: Sequence[Dict[str, Any]],
        version: str = REGISTRY_VERSION,
        policy_version: str = POLICY_VERSION,
    ) -> "ModelRegistry":
        """Build a registry from raw payload dicts, failing closed on any error.

        Detects duplicate ids, unknown providers, and every malformed field
        before returning.  A malformed registry is refused in full.
        """
        if providers is None:
            providers = ()
        if models is None:
            models = ()
        if not isinstance(providers, (list, tuple)):
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID, "providers must be a list of objects",
            )
        if not isinstance(models, (list, tuple)):
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID, "models must be a list of objects",
            )
        parsed_providers: Dict[str, ProviderDescriptor] = {}
        for raw in providers:
            pd = _parse_provider(raw, policy_version=policy_version)
            if pd.provider_id in parsed_providers:
                raise ModelRegistryError(
                    MODEL_CONFIG_INVALID,
                    f"duplicate provider_id {pd.provider_id!r}",
                )
            parsed_providers[pd.provider_id] = pd

        parsed_models: Dict[str, ModelDescriptor] = {}
        for raw in models:
            md = _parse_model(raw, policy_version=policy_version)
            if md.model_id in parsed_models:
                raise ModelRegistryError(
                    MODEL_CONFIG_INVALID, f"duplicate model_id {md.model_id!r}"
                )
            parsed_models[md.model_id] = md

        return cls(parsed_providers, parsed_models, version=version)

    @classmethod
    def load_files(cls, base_dir: Optional[str] = None) -> "ModelRegistry":
        """Load the versioned repo registry files (providers.json + models.json).

        Strict, fail-closed: any malformed entry, duplicate id, unknown provider
        reference or invalid enum value raises ``ModelRegistryError`` — never a
        partial load.
        """
        base = Path(base_dir) if base_dir else Path(__file__).resolve().parent / "registry"
        providers_path = base / "providers.json"
        models_path = base / "models.json"
        try:
            providers_doc = json.loads(providers_path.read_text(encoding="utf-8"))
            models_doc = json.loads(models_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID, f"cannot read registry files: {exc}"
            ) from None

        # Top-level type checks MUST run before any ``.get()`` (a JSON list/
        # string/number top-level is refused fail-closed, never a raw error).
        if not isinstance(providers_doc, dict):
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID,
                "providers.json must be a top-level object",
            )
        if not isinstance(models_doc, dict):
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID,
                "models.json must be a top-level object",
            )
        _reject_unknown_keys(providers_doc, _DOC_KEYS, "providers document")
        _reject_unknown_keys(models_doc, _DOC_KEYS, "models document")

        if providers_doc.get("registry_version") != REGISTRY_VERSION:
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID,
                f"providers registry_version "
                f"{providers_doc.get('registry_version')!r} != {REGISTRY_VERSION!r}",
            )
        if providers_doc.get("policy_version") != POLICY_VERSION:
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID,
                f"providers policy_version "
                f"{providers_doc.get('policy_version')!r} != {POLICY_VERSION!r}",
            )
        if models_doc.get("registry_version") != REGISTRY_VERSION:
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID,
                "providers/models registry_version mismatch",
            )
        if models_doc.get("policy_version") != POLICY_VERSION:
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID,
                "providers/models policy_version mismatch",
            )
        providers = providers_doc.get("providers")
        models = models_doc.get("models")
        if not isinstance(providers, list) or not isinstance(models, list):
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID,
                "providers/models must be arrays",
            )
        return cls.from_payload(
            providers,
            models,
            version=REGISTRY_VERSION,
            policy_version=POLICY_VERSION,
        )

    # -- validation ---------------------------------------------------------

    def _validate_cross_references(self) -> None:
        """Fail-closed cross-reference validation (model → provider + capability bound).

        Every model must reference a known provider, and every model capability
        tag must be ⊆ its provider's ``capabilities_supported`` (the provider's
        declared upper bound over its models).
        """
        for model_id, md in self._models.items():
            provider = self._providers.get(md.provider_id)
            if provider is None:
                raise ModelRegistryError(
                    MODEL_CONFIG_INVALID,
                    f"model {model_id!r} references unknown provider "
                    f"{md.provider_id!r}",
                )
            extra = set(md.capability_tags) - set(provider.capabilities_supported)
            if extra:
                raise ModelRegistryError(
                    MODEL_CONFIG_INVALID,
                    f"model {model_id!r} claims capabilities {sorted(extra)} "
                    f"not supported by provider {md.provider_id!r}",
                )

    # -- read-only access ---------------------------------------------------

    @property
    def version(self) -> str:
        return self._version

    def get_provider(self, provider_id: str) -> Optional[ProviderDescriptor]:
        return self._providers.get(provider_id)

    def get_model(self, model_id: str) -> Optional[ModelDescriptor]:
        return self._models.get(model_id)

    def list_providers(self) -> Tuple[ProviderDescriptor, ...]:
        return tuple(self._providers[p] for p in sorted(self._providers))

    def list_models(self) -> Tuple[ModelDescriptor, ...]:
        return tuple(self._models[m] for m in sorted(self._models))

    def has_provider(self, provider_id: str) -> bool:
        return provider_id in self._providers

    def has_model(self, model_id: str) -> bool:
        return model_id in self._models

    # -- identity validation (single dispatch gate) -------------------------

    def validate_identity(
        self,
        provider_id: Optional[str],
        model_id: Optional[str],
        thinking_tier: Optional[str] = None,
    ) -> ModelDescriptor:
        """Fail-closed validation of a resolved dispatch identity.

        Checks (in order): the provider exists + is enabled + is available;
        the model exists + is enabled + is not retired + its ``provider_id``
        matches.  Returns the :class:`ModelDescriptor` on success; raises
        :class:`ModelRegistryError` with a bounded code otherwise.

        ``thinking_tier`` is accepted for call-site symmetry but is NOT
        validated here (reasoning-tier policy is owned by ``routing``).
        """
        if provider_id is None or not self.has_provider(provider_id):
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID, f"unknown provider {provider_id!r}"
            )
        provider = self._providers[provider_id]
        if not provider.enabled:
            raise ModelRegistryError(
                PROVIDER_UNAVAILABLE, f"provider {provider_id!r} is disabled"
            )
        if provider.availability_state not in (
            AvailabilityState.AVAILABLE.value,
            AvailabilityState.DEGRADED.value,
        ):
            raise ModelRegistryError(
                PROVIDER_UNAVAILABLE,
                f"provider {provider_id!r} is "
                f"{provider.availability_state!r} (not available)",
            )
        if model_id is None or not self.has_model(model_id):
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID, f"unknown model {model_id!r}"
            )
        model = self._models[model_id]
        if model.provider_id != provider_id:
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID,
                f"model {model_id!r} belongs to provider {model.provider_id!r}, "
                f"not {provider_id!r}",
            )
        if not model.enabled:
            raise ModelRegistryError(
                MODEL_NOT_ALLOWED, f"model {model_id!r} is disabled"
            )
        if model.lifecycle_state == LifecycleState.RETIRED.value:
            raise ModelRegistryError(
                MODEL_UNAVAILABLE, f"model {model_id!r} is retired"
            )
        return model

    # -- capability floor / candidates / fallback eligibility ---------------

    def satisfies_floor(
        self, model: ModelDescriptor, requirements: CapabilityRequirements
    ) -> bool:
        """``True`` iff ``model`` meets the capability/quality floor.

        Floor = required capabilities + reasoning floor + tool requirements +
        context requirement + reliability quality floor.  There is **no** cost
        comparison and no "cheaper is better" logic.
        """
        requirements.validate()
        model_tags = set(model.capability_tags)
        if not set(requirements.required_capabilities) <= model_tags:
            return False
        if requirements.optional_capabilities:
            # Optional capabilities are desired but never gate a candidate.
            pass
        if not _reasoning_satisfies(
            model.reasoning_levels_supported,
            ReasoningLevel(requirements.minimum_reasoning_level),
        ):
            return False
        model_tools = set(model.tool_capabilities)
        if not set(requirements.tool_requirements) <= model_tools:
            return False
        if requirements.context_requirement is not None:
            if model.context_window_metadata is None:
                return False  # fail-closed: unknown context cannot satisfy a floor
            if model.context_window_metadata < requirements.context_requirement:
                return False
        if not _class_satisfies(
            model.reliability_class, requirements.quality_floor
        ):
            return False
        return True

    def eligible_models(
        self,
        requirements: CapabilityRequirements,
        reference_model_id: Optional[str] = None,
    ) -> Tuple[ModelDescriptor, ...]:
        """Candidate set for ``requirements`` (deterministic, no selection).

        Returns every enabled model that (a) meets the capability/quality floor
        and (b) satisfies the independence constraint relative to
        ``reference_model_id`` (when the requirement is a hard constraint).
        Ordering is by ``model_id`` (stable) — never by cost.  ``DIFFERENT_PROVIDER
        _PREFERRED`` is a soft hint and does not reorder or filter in E1.
        """
        requirements.validate()
        reference = self._resolve_reference(reference_model_id)

        candidates = []
        for model in self.list_models():
            if self._candidate_eligibility(model, requirements, reference, True):
                candidates.append(model)
        return tuple(sorted(candidates, key=lambda m: m.model_id))

    def _resolve_reference(
        self, reference_model_id: Optional[str]
    ) -> Optional[ModelDescriptor]:
        """Resolve an optional reference model id, fail-closed on unknown."""
        if reference_model_id is None:
            return None
        reference = self.get_model(reference_model_id)
        if reference is None:
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID,
                f"unknown reference model {reference_model_id!r}",
            )
        return reference

    def _candidate_eligibility(
        self,
        model: ModelDescriptor,
        requirements: CapabilityRequirements,
        reference: Optional[ModelDescriptor],
        policy_allows_fallback: bool,
    ) -> bool:
        """Single canonical eligibility predicate (shared by ``eligible_models``
        and ``is_fallback_eligible``).

        ``enabled ∧ provider enabled+available ∧ lifecycle ACTIVE ∧ floor ∧
        independence ∧ policy_allows_fallback``.  No cost comparison, no
        selection.
        """
        if not policy_allows_fallback:
            return False
        if not model.enabled:
            return False
        if model.lifecycle_state != LifecycleState.ACTIVE.value:
            return False
        provider = self._providers.get(model.provider_id)
        if provider is None or not provider.enabled:
            return False
        if provider.availability_state not in (
            AvailabilityState.AVAILABLE.value,
            AvailabilityState.DEGRADED.value,
        ):
            return False
        if not self.satisfies_floor(model, requirements):
            return False
        return self._independence_ok(model, reference, requirements)

    def _independence_ok(
        self,
        model: ModelDescriptor,
        reference: Optional[ModelDescriptor],
        requirements: CapabilityRequirements,
    ) -> bool:
        constraint = requirements.independence_requirement
        if constraint == Independence.SAME_MODEL_ALLOWED.value:
            return True
        if reference is None:
            # Without a reference model, a hard independence constraint cannot
            # be evaluated -> fail closed (no candidates).
            return constraint == Independence.DIFFERENT_PROVIDER_PREFERRED.value
        if constraint == Independence.DIFFERENT_MODEL_REQUIRED.value:
            return model.model_id != reference.model_id
        if constraint == Independence.DIFFERENT_PROVIDER_PREFERRED.value:
            return True  # soft hint — does not filter in E1
        if constraint == Independence.DIFFERENT_PROVIDER_REQUIRED.value:
            return model.provider_id != reference.provider_id
        return True

    def is_fallback_eligible(
        self,
        model_id: str,
        requirements: CapabilityRequirements,
        reference_model_id: Optional[str] = None,
        policy_allows_fallback: bool = True,
    ) -> bool:
        """Metadata-only fallback eligibility check (NEVER executes a fallback).

        Uses the single canonical eligibility predicate:
        ``enabled ∧ provider enabled+available ∧ lifecycle ACTIVE ∧ floor ∧
        independence ∧ policy_allows_fallback``.  Returns ``bool`` only — E1
        performs no fallback.
        """
        requirements.validate()
        model = self.get_model(model_id)
        if model is None:
            return False
        reference = self._resolve_reference(reference_model_id)
        return self._candidate_eligibility(
            model, requirements, reference, policy_allows_fallback
        )


# ---------------------------------------------------------------------------
# Default (singleton) registry
# ---------------------------------------------------------------------------

_default_registry: Optional[ModelRegistry] = None


def get_default_registry() -> ModelRegistry:
    """Return the process-wide default registry (lazy, loaded from repo files)."""
    global _default_registry
    if _default_registry is None:
        _default_registry = ModelRegistry.load_files()
    return _default_registry


def reset_default_registry() -> None:
    """Reset the cached default registry (test helper only)."""
    global _default_registry
    _default_registry = None
