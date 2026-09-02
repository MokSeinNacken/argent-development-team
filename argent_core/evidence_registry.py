"""Phase E3 — versioned, bounded benchmark/capability evidence registry.

Phase E1 described *what models exist and what capabilities they claim*
(``benchmarked: false`` — claims, not facts).  Phase E3 adds the **versioned,
machine-readable evidence** that backs those claims: a bounded, fail-closed
``benchmarks_v1.json`` checked into the repo, keyed per (model, category) with
a bounded ``EvidenceStatus`` and an immutable evidence reference/version.

Fundamental invariants (verbindlich, Owner-Spec E3):

* **``benchmarked`` does NOT mean "authorised"** and **"policy-allowed" does
  NOT mean "capable enough"**.  Evidence is an *independent* filter: a model is
  eligible only if it is (1) policy-authorised AND (2) meets the capability
  floor AND (3) meets the minimum evidence status for every required
  capability's task-relevant category.  None of the three is sufficient alone.
* **Honest, bounded status.**  No scores are invented.  The only statuses are
  ``VERIFIED`` / ``PROVISIONAL`` / ``UNKNOWN`` / ``REJECTED``.  Without real
  benchmarks, the three bootstrap models are at most ``PROVISIONAL`` (claims
  from local config/architecture, ``benchmarked:false`` documented) — nothing
  is asserted ``VERIFIED``.
* **Task-relevant categories, not a single global score.**  Evidence is tracked
  per category (coordination/basic-reasoning, repository-coding, debugging/
  root-cause, architecture, security-review, tool-agent, long-context), mapped
  deterministically from the bounded ``Capability`` vocabulary.
* **Fail-closed loading.**  A malformed registry (duplicate ids, unknown model
  refs, invalid status/category, unknown fields) is refused in full.
* **No agent mutation.**  The registry is immutable read-only; no API lets an
  agent raise a status or mint a ``VERIFIED`` claim.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, Optional, Sequence, Tuple

from .model_registry import (
    MODEL_CONFIG_INVALID,
    Capability,
    ModelRegistryError,
)

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

EVIDENCE_REGISTRY_VERSION = "1"

# ---------------------------------------------------------------------------
# Bounded enums
# ---------------------------------------------------------------------------


class EvidenceStatus(str, Enum):
    """Bounded evidence status (bounded: no scores, no invented benchmarks)."""

    VERIFIED = "VERIFIED"
    PROVISIONAL = "PROVISIONAL"
    UNKNOWN = "UNKNOWN"
    REJECTED = "REJECTED"


#: Rank used ONLY for the "meets a minimum status" comparison.  REJECTED and
#: UNKNOWN are non-positive (never satisfy a PROVISIONAL/VERIFIED minimum).
_EVIDENCE_STATUS_RANK: Dict[str, int] = {
    EvidenceStatus.REJECTED.value: -1,
    EvidenceStatus.UNKNOWN.value: 0,
    EvidenceStatus.PROVISIONAL.value: 1,
    EvidenceStatus.VERIFIED.value: 2,
}


class EvidenceCategory(str, Enum):
    """Bounded, task-relevant evidence categories (coarse, NOT a global score)."""

    COORDINATION_BASIC_REASONING = "coordination_basic_reasoning"
    REPOSITORY_CODING = "repository_coding"
    DEBUGGING_ROOT_CAUSE = "debugging_root_cause"
    ARCHITECTURE = "architecture"
    SECURITY_REVIEW = "security_review"
    TOOL_AGENT = "tool_agent"
    LONG_CONTEXT = "long_context"


#: Deterministic capability -> evidence-category mapping.  A required capability
#: gates on the evidence status of its mapped category.  This is the single
#: structural link between the E1 capability vocabulary and the E3 evidence
#: vocabulary — a hardcoded, reviewed mapping, never agent-derived.
_CAPABILITY_TO_CATEGORY: Dict[str, str] = {
    Capability.COORDINATION.value: EvidenceCategory.COORDINATION_BASIC_REASONING.value,
    Capability.SIMPLE_ANALYSIS.value: EvidenceCategory.COORDINATION_BASIC_REASONING.value,
    Capability.CODE_IMPLEMENTATION.value: EvidenceCategory.REPOSITORY_CODING.value,
    Capability.COMPLEX_CODE_IMPLEMENTATION.value: EvidenceCategory.REPOSITORY_CODING.value,
    Capability.DEBUGGING.value: EvidenceCategory.DEBUGGING_ROOT_CAUSE.value,
    Capability.REPOSITORY_REASONING.value: EvidenceCategory.REPOSITORY_CODING.value,
    Capability.ARCHITECTURE.value: EvidenceCategory.ARCHITECTURE.value,
    Capability.SECURITY_REVIEW.value: EvidenceCategory.SECURITY_REVIEW.value,
    Capability.CODE_REVIEW.value: EvidenceCategory.SECURITY_REVIEW.value,
    Capability.ROOT_CAUSE_ANALYSIS.value: EvidenceCategory.DEBUGGING_ROOT_CAUSE.value,
    Capability.TOOL_USE.value: EvidenceCategory.TOOL_AGENT.value,
    Capability.LONG_CONTEXT.value: EvidenceCategory.LONG_CONTEXT.value,
    Capability.VISION.value: EvidenceCategory.TOOL_AGENT.value,
    Capability.STRUCTURED_OUTPUT.value: EvidenceCategory.TOOL_AGENT.value,
}


def capability_category(capability: str) -> str:
    """Map a bounded capability value to its evidence category (deterministic)."""
    return _CAPABILITY_TO_CATEGORY.get(capability, EvidenceCategory.TOOL_AGENT.value)


def evidence_status_rank(status: str) -> int:
    """Bounded rank of an evidence status (only for minimum-status comparison)."""
    return _EVIDENCE_STATUS_RANK.get(status, _EVIDENCE_STATUS_RANK[EvidenceStatus.UNKNOWN.value])


# ---------------------------------------------------------------------------
# Strict allow-lists (fail-closed)
# ---------------------------------------------------------------------------

_DOC_KEYS: FrozenSet[str] = frozenset({
    "evidence_version", "models",
})
_MODEL_KEYS: FrozenSet[str] = frozenset({
    "model_id", "categories",
})
_ENTRY_KEYS: FrozenSet[str] = frozenset({
    "status", "evidence_ref", "version", "benchmarked",
})

#: Bounded evidence-ref authority prefixes (trusted local, checked-in sources).
#: Mirrors the E1 provenance authority rule; ``test`` is for test fixtures.
_TRUSTED_EVIDENCE_AUTHORITIES: Tuple[str, ...] = (
    "openclaw.json",
    "routing.py",
    "docs-",
    "architecture",
    "local-config",
    "test",
)

_AGENT_ORIGIN_MARKERS: Tuple[str, ...] = (
    "agent-output", "model-self-report", "self-report", "model-output",
    "prompt", "handoff",
)


def _no_duplicate_keys(pairs: list) -> dict:
    """object_pairs_hook that refuses duplicate JSON keys (F4, fail-closed).

    ``json.loads`` silently keeps the last duplicate key; a versioned, trusted
    evidence registry must refuse an ambiguous duplicate rather than silently
    resolve it.  Mirrors the routing-policy loader's ``_no_duplicate_keys``
    (which raises ``RoutingError``); this raises ``ModelRegistryError``.
    """
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID, f"duplicate JSON key {key!r}",
            )
        out[key] = value
    return out


def _reject_unknown_keys(raw: Dict[str, Any], allowed: FrozenSet[str], context: str) -> None:
    for key in raw:
        if key not in allowed:
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID, f"{context}: unknown field {key!r}",
            )


def _require_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ModelRegistryError(MODEL_CONFIG_INVALID, f"{label} must be a non-empty string")
    return value


def _validate_evidence_ref(ref: str, label: str) -> str:
    if not isinstance(ref, str) or not ref:
        raise ModelRegistryError(MODEL_CONFIG_INVALID, f"{label} must be a non-empty string")
    low = ref.lower()
    for marker in _AGENT_ORIGIN_MARKERS:
        if marker in low:
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID,
                f"{label} {ref!r} claims agent origin (marker {marker!r})",
            )
    for authority in _TRUSTED_EVIDENCE_AUTHORITIES:
        if low.startswith(authority):
            return ref
    raise ModelRegistryError(
        MODEL_CONFIG_INVALID,
        f"{label} {ref!r} is not a trusted-local authority "
        f"(allowed prefixes: {', '.join(_TRUSTED_EVIDENCE_AUTHORITIES)})",
    )


def _evidence_content_hash(models: Sequence[Dict[str, Any]], version: str) -> str:
    """sha256 of the canonical evidence document content (F3 provenance).

    Deterministic over the raw models list + version: a change to any entry
    changes the evidence content digest.
    """
    canonical = json.dumps(
        {"evidence_version": version, "models": list(models)},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EvidenceEntry:
    """One (model, category) evidence claim (immutable, versioned)."""

    model_id: str
    category: str
    status: str
    evidence_ref: str
    version: str
    benchmarked: bool = False


class EvidenceRegistry:
    """Immutable, fail-closed registry of benchmark/capability evidence.

    Construction is via :meth:`from_payload` / :meth:`load_files` only.  A
    missing (model, category) entry means ``UNKNOWN`` — which never satisfies a
    ``PROVISIONAL``/``VERIFIED`` minimum (fail-closed on absence).
    """

    def __init__(self, entries: Dict[Tuple[str, str], EvidenceEntry], version: str, content_hash: str = ""):
        if version != EVIDENCE_REGISTRY_VERSION:
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID,
                f"evidence version {version!r} != {EVIDENCE_REGISTRY_VERSION!r}",
            )
        self._version = version
        self._entries = MappingProxyType(dict(entries))
        # F3: sha256 of the canonical evidence document content.
        self._content_hash = content_hash

    @classmethod
    def from_payload(cls, models: Sequence[Dict[str, Any]], version: str = EVIDENCE_REGISTRY_VERSION) -> "EvidenceRegistry":
        if models is None:
            models = ()
        if not isinstance(models, (list, tuple)):
            raise ModelRegistryError(MODEL_CONFIG_INVALID, "models must be a list of objects")
        entries: Dict[Tuple[str, str], EvidenceEntry] = {}
        seen_models: set = set()
        for raw in models:
            if not isinstance(raw, dict):
                raise ModelRegistryError(MODEL_CONFIG_INVALID, "evidence model entry must be an object")
            _reject_unknown_keys(raw, _MODEL_KEYS, "evidence model entry")
            model_id = _require_id(raw.get("model_id"), "model_id")
            if model_id in seen_models:
                raise ModelRegistryError(MODEL_CONFIG_INVALID, f"duplicate evidence model_id {model_id!r}")
            seen_models.add(model_id)
            cats = raw.get("categories")
            if not isinstance(cats, dict) or not cats:
                raise ModelRegistryError(MODEL_CONFIG_INVALID, f"model {model_id!r} categories must be a non-empty object")
            for cat, entry in cats.items():
                _require_enum(cat, EvidenceCategory, f"model {model_id!r} category")
                if not isinstance(entry, dict):
                    raise ModelRegistryError(MODEL_CONFIG_INVALID, f"model {model_id!r} category {cat!r} entry must be an object")
                _reject_unknown_keys(entry, _ENTRY_KEYS, f"model {model_id!r} category {cat!r}")
                status = entry.get("status")
                _require_enum(status, EvidenceStatus, f"model {model_id!r} category {cat!r} status")
                if EvidenceStatus(status).value == EvidenceStatus.VERIFIED.value:
                    raise ModelRegistryError(
                        MODEL_CONFIG_INVALID,
                        f"model {model_id!r} category {cat!r} status must not be VERIFIED "
                        "(no real benchmarks exist yet)",
                    )
                ref = _validate_evidence_ref(entry.get("evidence_ref"), f"model {model_id!r} category {cat!r} evidence_ref")
                ver = _require_id(entry.get("version"), f"model {model_id!r} category {cat!r} version")
                # F4: an entry version must equal the document evidence_version
                # (bounded format).  A mismatched entry version is refused
                # fail-closed ("banana" or any foreign version is never accepted).
                if ver != version:
                    raise ModelRegistryError(
                        MODEL_CONFIG_INVALID,
                        f"model {model_id!r} category {cat!r} version {ver!r} "
                        f"!= document evidence_version {version!r}",
                    )
                benchmarked = entry.get("benchmarked", False)
                if not isinstance(benchmarked, bool):
                    raise ModelRegistryError(MODEL_CONFIG_INVALID, f"model {model_id!r} category {cat!r} benchmarked must be a bool")
                if benchmarked is True:
                    raise ModelRegistryError(
                        MODEL_CONFIG_INVALID,
                        f"model {model_id!r} category {cat!r} benchmarked must be False "
                        "(no real benchmarks exist yet — nothing may be asserted VERIFIED)",
                    )
                key = (model_id, EvidenceCategory(cat).value)
                if key in entries:
                    raise ModelRegistryError(MODEL_CONFIG_INVALID, f"duplicate evidence entry for model {model_id!r} category {cat!r}")
                entries[key] = EvidenceEntry(
                    model_id=model_id, category=EvidenceCategory(cat).value,
                    status=EvidenceStatus(status).value, evidence_ref=ref,
                    version=ver, benchmarked=benchmarked,
                )
        return cls(entries, version=version,
                   content_hash=_evidence_content_hash(models, version))

    @classmethod
    def load_files(cls, base_dir: Optional[str] = None) -> "EvidenceRegistry":
        base = Path(base_dir) if base_dir else Path(__file__).resolve().parent / "registry"
        path = base / "benchmarks_v1.json"
        try:
            doc = json.loads(
                path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys
            )
        except ModelRegistryError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise ModelRegistryError(MODEL_CONFIG_INVALID, f"cannot read benchmarks_v1.json: {exc}") from None
        if not isinstance(doc, dict):
            raise ModelRegistryError(MODEL_CONFIG_INVALID, "benchmarks_v1.json must be a top-level object")
        _reject_unknown_keys(doc, _DOC_KEYS, "benchmarks document")
        if doc.get("evidence_version") != EVIDENCE_REGISTRY_VERSION:
            raise ModelRegistryError(
                MODEL_CONFIG_INVALID,
                f"evidence_version {doc.get('evidence_version')!r} != {EVIDENCE_REGISTRY_VERSION!r}",
            )
        models = doc.get("models")
        if not isinstance(models, list):
            raise ModelRegistryError(MODEL_CONFIG_INVALID, "models must be an array")
        return cls.from_payload(models, version=EVIDENCE_REGISTRY_VERSION)

    # -- read-only access ---------------------------------------------------

    @property
    def version(self) -> str:
        return self._version

    @property
    def content_hash(self) -> str:
        """sha256 of the canonical evidence document content (F3 provenance)."""
        return self._content_hash

    def get_status(self, model_id: str, category: str) -> str:
        entry = self._entries.get((model_id, category))
        return entry.status if entry is not None else EvidenceStatus.UNKNOWN.value

    def get_entry(self, model_id: str, category: str) -> Optional[EvidenceEntry]:
        return self._entries.get((model_id, category))

    def list_models(self) -> Tuple[str, ...]:
        return tuple(sorted({m for (m, _c) in self._entries}))

    def validate_model_refs(self, registry: "Any") -> None:
        """Cross-validate every model ref against the E1 model registry.

        An evidence entry referencing a model the E1 registry does not describe
        (unknown/disabled) is refused fail-closed.
        """
        for model_id in self.list_models():
            md = registry.get_model(model_id)
            if md is None:
                raise ModelRegistryError(
                    MODEL_CONFIG_INVALID,
                    f"evidence references unknown model {model_id!r}",
                )
            if not md.enabled:
                raise ModelRegistryError(
                    MODEL_CONFIG_INVALID,
                    f"evidence references disabled model {model_id!r}",
                )


def _require_enum(value: Any, enum_cls: type, label: str) -> None:
    try:
        enum_cls(value)
    except (ValueError, TypeError):
        raise ModelRegistryError(
            MODEL_CONFIG_INVALID,
            f"invalid {label} {value!r}; expected one of {sorted(m.value for m in enum_cls)}",
        ) from None


# ---------------------------------------------------------------------------
# Evidence gate (the independent E3 filter)
# ---------------------------------------------------------------------------


def evidence_status_for_capabilities(
    registry: EvidenceRegistry,
    model_id: str,
    required_capabilities: Sequence[str],
) -> Tuple[str, ...]:
    """Return the (deduplicated) evidence statuses for the required capabilities.

    Each required capability maps to its category; the status is looked up per
    (model, category).  A category without an entry is ``UNKNOWN``.
    """
    categories = {capability_category(c) for c in required_capabilities}
    return tuple(registry.get_status(model_id, c) for c in sorted(categories))


def satisfies_evidence(
    registry: EvidenceRegistry,
    model_id: str,
    required_capabilities: Sequence[str],
    minimum_status: str,
) -> bool:
    """``True`` iff every required capability's category meets ``minimum_status``.

    A model with no evidence for a required category (``UNKNOWN``) fails a
    ``PROVISIONAL``/``VERIFIED`` minimum — fail-closed on absence.  This is the
    independent evidence filter; it is entirely separate from the capability
    floor (a model can be floor-capable but under-evidenced, and vice versa).
    """
    minimum_rank = evidence_status_rank(minimum_status)
    if minimum_rank <= 0:
        # A minimum of UNKNOWN/REJECTED is meaningless for an eligibility gate;
        # policy validation forbids it, but guard deterministically anyway.
        return True
    for status in evidence_status_for_capabilities(registry, model_id, required_capabilities):
        if evidence_status_rank(status) < minimum_rank:
            return False
    return True


# ---------------------------------------------------------------------------
# Default (singleton)
# ---------------------------------------------------------------------------

_default_evidence: Optional[EvidenceRegistry] = None


def get_default_evidence_registry() -> EvidenceRegistry:
    global _default_evidence
    if _default_evidence is None:
        ev = EvidenceRegistry.load_files()
        ev.validate_model_refs(_default_model_registry())
        _default_evidence = ev
    return _default_evidence


def _default_model_registry():
    from .model_registry import get_default_registry
    return get_default_registry()


def reset_default_evidence_registry() -> None:
    global _default_evidence
    _default_evidence = None
