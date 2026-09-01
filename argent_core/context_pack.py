"""Phase D1 — immutable Context Pack core (ARGENT ARCHITECTURE V1 FINAL §12).

This module is the pure, deterministic authority for building an **immutable
Context Pack** per agent dispatch.  It performs **no I/O, no provider calls,
no vendor tokenization and no shell commands** — it is a pure function of its
inputs, so it is trivially testable and cannot be influenced by untrusted
agent output.

Design invariants (verbindlich, see §12 / §16 / §19):

* **Trust is local.**  ``TrustClass`` is assigned by this builder from the
  *input slot* (objective, constraint, fact, artifact, history, prior result),
  never from agent text.  An agent string that *claims* to be policy can never
  raise its own trust class.
* **Budgets are trusted policy.**  The budget (soft/hard) and any expansion
  come from the trusted ``ContextBudgetPolicy`` / capability field — never from
  an agent.  Phase E (adaptive roles/routing) does not exist yet, so the
  capability tier is passed in explicitly.
* **Budget enforcement is fail-closed.**  If the REQUIRED context alone exceeds
  the hard budget, the build raises ``CONTEXT_BUDGET_EXCEEDED`` (no silent
  truncation, no dispatch).  If the pack exceeds the soft budget but fits in
  the hard budget, it may only be expanded with a bounded, persisted reason.
* **Canonical hash.**  ``content_hash`` is a deterministic hash of the semantic
  content (role + items).  ``context_pack_id`` / ``created_at`` are *instance*
  metadata and are NOT part of the content hash (§19: same semantic inputs →
  same hash; reordering non-semantic inputs → same hash).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

from .models import ArgentError

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

CONTEXT_PACK_VERSION = "1"

# ---------------------------------------------------------------------------
# Bounded reference/metadata limits (F4).  These fields are deterministic
# render overhead or in-memory manifest metadata that is NOT counted as item
# ``content``, so they MUST be bounded — otherwise an unbounded metadata field
# could bypass the hard budget (render overhead) or bloat the in-memory
# manifest beyond what the budget expresses.
# ---------------------------------------------------------------------------

MAX_ITEMS = 256                   # maximum number of items in one pack
MAX_PROVENANCE_ENTRIES = 256      # maximum provenance triples
MAX_SOURCE_REF_LEN = 512          # maximum source_ref length
MAX_ARTIFACT_REF_LEN = 512        # maximum artifact ref length
MAX_ARTIFACT_LOCATION_LEN = 1024  # maximum artifact location length
MAX_METADATA_ENTRIES = 16         # maximum (key, value) metadata pairs per item
MAX_METADATA_KEY_LEN = 128        # maximum metadata key length
MAX_METADATA_VALUE_LEN = 1024     # maximum metadata value length

# ---------------------------------------------------------------------------
# Trust / importance / expansion enums (canonical persisted values)
# ---------------------------------------------------------------------------


class TrustClass(str, Enum):
    """Trust classification of a context item (determined LOCALLY by the builder).

    Ordering is irrelevant to security; the value is a canonical persisted
    string.  ``AGENT_RESULT`` / ``EXTERNAL_UNTRUSTED`` / ``OPTIONAL_HISTORY``
    are never authority and can never raise themselves.
    """

    OWNER_INSTRUCTION = "OWNER_INSTRUCTION"
    TRUSTED_POLICY = "TRUSTED_POLICY"
    TRUSTED_LOCAL_FACT = "TRUSTED_LOCAL_FACT"
    TRUSTED_ARTIFACT = "TRUSTED_ARTIFACT"
    AGENT_RESULT = "AGENT_RESULT"
    EXTERNAL_UNTRUSTED = "EXTERNAL_UNTRUSTED"
    OPTIONAL_HISTORY = "OPTIONAL_HISTORY"


class Importance(str, Enum):
    """Relative importance for trimming.  ``REQUIRED`` is never trimmed."""

    REQUIRED = "REQUIRED"
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    OPTIONAL = "OPTIONAL"


#: Trimming rank (higher = more important; REQUIRED is never removable).
_IMPORTANCE_RANK: dict[str, int] = {
    Importance.REQUIRED.value: 4,
    Importance.HIGH.value: 3,
    Importance.NORMAL.value: 2,
    Importance.OPTIONAL.value: 1,
}


class ExpansionReason(str, Enum):
    """Bounded, persisted reason for a soft-budget expansion (§12)."""

    REQUIRED_CONTEXT = "REQUIRED_CONTEXT"
    LARGE_CODE_EVIDENCE = "LARGE_CODE_EVIDENCE"
    SECURITY_REVIEW = "SECURITY_REVIEW"
    ROOT_CAUSE_ANALYSIS = "ROOT_CAUSE_ANALYSIS"
    INTEGRATED_REVIEW = "INTEGRATED_REVIEW"


class CapabilityTier(str, Enum):
    """Trusted capability tier selecting the budget (pre-Phase-E mapping)."""

    FLASH = "FLASH"
    PRO = "PRO"
    SOL = "SOL"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

#: Canonical error code for a hard-budget violation (never CODE_FAILURE,
#: never a resource failure, never a model failure — a pure ORCHESTRATION error).
CONTEXT_BUDGET_EXCEEDED = "CONTEXT_BUDGET_EXCEEDED"


class ContextBuildError(ArgentError):
    """A Context Pack build failed (orchestration error, fail-closed).

    ``code`` is a bounded reason code (``CONTEXT_BUDGET_EXCEEDED`` or a
    validation code).  This is NEVER a ``CODE_FAILURE``, never a
    ``RESOURCE`` failure and never a model failure — it carries no escalation
    semantics.
    """

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


# ---------------------------------------------------------------------------
# Context failure-code classification (F6)
# ---------------------------------------------------------------------------

#: Deterministic, permanent Context-Pack failure codes — a bounded retry can
#: NEVER fix them (budget exceeded, an invalid/foreign pack, a stale pack, a
#: missing task).  They must fail-closed to BLOCKED quarantine, never re-queue.
_PERMANENT_CONTEXT_CODES = frozenset({
    CONTEXT_BUDGET_EXCEEDED,
    "CONTEXT_INVALID_VERSION",
    "CONTEXT_MALFORMED_ID",
    "CONTEXT_INVALID_TRUST_CLASS",
    "CONTEXT_INVALID_IMPORTANCE",
    "CONTEXT_INVALID_BUDGET",
    "CONTEXT_INVALID_EXPANSION_REASON",
    "CONTEXT_INVALID_REFERENCE",
    "CONTEXT_HASH_MISMATCH",
    "CONTEXT_STALE_PACK",
    "CONTEXT_MISSING_TASK",
})

#: Provably transient Context-Pack failure codes — a bounded re-queue is safe
#: (e.g. a persist/artifact-write I/O error that may succeed on a retry).
_TRANSIENT_CONTEXT_CODES = frozenset({
    "CONTEXT_PERSIST_IO_ERROR",
    "CONTEXT_ARTIFACT_WRITE_ERROR",
})


def is_permanent_context_code(code: str) -> bool:
    """Return ``True`` for a deterministic, permanent Context-Pack failure code.

    A permanent code can never be fixed by a bounded retry and must fail-closed
    to BLOCKED (quarantine).  Any ``CONTEXT_INVALID_*`` code is permanent by
    construction.  A code that is neither in the explicit permanent set nor the
    transient set is treated as permanent (fail-closed): an unknown context
    code must never enter a retry loop.
    """
    if code in _TRANSIENT_CONTEXT_CODES:
        return False
    if code in _PERMANENT_CONTEXT_CODES or code.startswith("CONTEXT_INVALID_"):
        return True
    return True  # unknown context code → fail-closed (never a retry loop)


# ---------------------------------------------------------------------------
# Token estimator
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Provider-neutral deterministic token approximation.

    ``max(1, len(text) // 4)`` — 4 chars ≈ 1 token, rounded down, minimum 1.
    This is intentionally a conservative, deterministic approximation rather
    than a vendor tokenizer: it has zero provider coupling, is trivially
    reproducible and cannot drift between providers.  Budget enforcement stays
    conservative (we round DOWN the character count, so a pack that is "within
    budget" under this estimate is safe against the 4-chars-per-token rule).
    """
    if not isinstance(text, str):
        text = str(text)
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Item / reference value objects
# ---------------------------------------------------------------------------


def _stable_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def item_content_hash(content: str) -> str:
    """Deterministic content hash for a single item (semantic content only)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def make_item_id(
    trust_class: str, source_type: str, source_ref: str, content: str,
) -> str:
    """Stable, locally generated item id (same semantic content → same id).

    Importance is intentionally NOT part of the id: importance is a priority
    hint, not identity.  Two items with identical content+source+trust collapse
    to the same id regardless of importance (dedup keeps the higher importance).
    """
    digest = hashlib.sha256(
        _stable_json(
            [trust_class, source_type, source_ref, content]
        ).encode("utf-8")
    ).hexdigest()
    return "ci_" + digest[:16]


@dataclass(frozen=True)
class ContextItem:
    """A single bounded context item.

    ``trust_class`` / ``importance`` are stored as canonical enum *string*
    values so that a malformed pack (built with raw strings) can be rejected by
    :func:`validate_context_pack`; the builder always writes valid enum values.
    """

    id: str
    trust_class: str
    importance: str
    source_type: str
    source_ref: str
    content: str
    content_hash: str
    metadata: tuple = ()  # sorted (key, value) string pairs, canonical

    def with_importance(self, importance: str) -> "ContextItem":
        """Return a copy with ``importance`` changed (dedup keeps the higher)."""
        return ContextItem(
            id=self.id, trust_class=self.trust_class, importance=importance,
            source_type=self.source_type, source_ref=self.source_ref,
            content=self.content, content_hash=self.content_hash,
            metadata=self.metadata,
        )


@dataclass(frozen=True)
class ArtifactRef:
    """A bounded artifact reference with an optional excerpt."""

    ref: str
    location: str = ""
    excerpt: str = ""
    content_hash: str = ""


@dataclass(frozen=True)
class ProvenanceEntry:
    """One provenance triple (source type + ref + trust class)."""

    source_type: str
    source_ref: str
    trust_class: str


@dataclass(frozen=True)
class FactInput:
    """A trusted local fact supplied to the builder."""

    content: str
    source_ref: str = ""
    importance: str = Importance.NORMAL.value


@dataclass(frozen=True)
class ResultInput:
    """A bounded prior agent result supplied to the builder (AGENT_RESULT)."""

    content: str
    source_ref: str = ""
    importance: str = Importance.OPTIONAL.value


# ---------------------------------------------------------------------------
# Budget policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetTier:
    """Soft range + target + hard ceiling for one capability tier (§12)."""

    soft_min: int
    soft_max: int
    soft_target: int
    hard: int


@dataclass(frozen=True)
class ContextBudgetPolicy:
    """Versioned, frozen context budget policy (defaults per §12).

    * FLASH: soft 4k–8k (target 6k), hard 16k
    * PRO:   soft 12k–24k (target 16k), hard 48k
    * SOL:   soft 24k–48k (target 32k), hard 96k

    ``allow_expansion`` governs whether a pack may exceed the soft budget (up to
    the hard budget) with a bounded persisted reason.  The tier is selected from
    the trusted capability field — never from agent output.
    """

    policy_version: str = "1"
    allow_expansion: bool = True
    flash: BudgetTier = BudgetTier(4000, 8000, 6000, 16000)
    pro: BudgetTier = BudgetTier(12000, 24000, 16000, 48000)
    sol: BudgetTier = BudgetTier(24000, 48000, 32000, 96000)

    def tier_for(self, capability: str) -> BudgetTier:
        tier = CapabilityTier(capability)
        return {
            CapabilityTier.FLASH: self.flash,
            CapabilityTier.PRO: self.pro,
            CapabilityTier.SOL: self.sol,
        }[tier]


# ---------------------------------------------------------------------------
# ContextPack (manifest)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextPack:
    """Immutable Context Pack manifest for one dispatch.

    ``items`` is the canonical ordered content (single source of truth).  The
    grouped fields (``objective``, ``acceptance_criteria``, ``constraints``,
    ``policy_references``, ``facts``, ``artifacts``, ``history``,
    ``provenance``) are projections of ``items`` for explicit schema
    compliance.  There are NO free-form agent fields treated as trusted policy.
    """

    version: str
    context_pack_id: str
    job_id: str
    dispatch_id: str
    role: str
    created_at: str
    objective: str
    acceptance_criteria: tuple
    constraints: tuple
    policy_references: tuple
    facts: tuple
    artifacts: tuple
    history: tuple
    budget_soft: int
    budget_hard: int
    budget_estimated: int
    token_count: int
    expansion_reason: Optional[str]
    provenance: tuple
    content_hash: str
    items: tuple = ()


@dataclass(frozen=True)
class ContextPackRecord:
    """Bounded persisted metadata row for a built pack (``context_packs``).

    Immutable artifact contents are stored on persistent storage
    (``~/.local/share/argent/``), referenced here by ``artifact_location``;
    only bounded metadata lives in SQLite.
    """

    context_pack_id: str
    dispatch_id: str
    job_id: str
    role: str
    version: str
    content_hash: str
    size_estimate: int
    token_count: int
    soft_budget: int
    hard_budget: int
    expansion_reason: Optional[str]
    artifact_location: Optional[str]
    created_at: str


# ---------------------------------------------------------------------------
# Canonical serialization + hashing
# ---------------------------------------------------------------------------


def _item_canonical(it: ContextItem) -> dict:
    """Canonical, hash-stable dict for one item (identity id excluded)."""
    return {
        "trust_class": it.trust_class,
        "importance": it.importance,
        "source_type": it.source_type,
        "source_ref": it.source_ref,
        "content": it.content,
        "metadata": list(it.metadata),
    }


def _item_sort_key(it: ContextItem):
    return (
        it.trust_class,
        it.importance,
        it.source_type,
        it.source_ref,
        it.content_hash,
    )


def canonical_content(items: Sequence[ContextItem], role: str, version: str) -> str:
    """Canonical JSON of the semantic content (sorted → order-independent)."""
    doc = {
        "version": version,
        "role": role,
        "items": [_item_canonical(it) for it in sorted(items, key=_item_sort_key)],
    }
    return _stable_json(doc)


def content_hash(pack: ContextPack) -> str:
    """Deterministic semantic content hash (instance metadata excluded).

    Same semantic inputs → same hash; reordering non-semantic items → same hash
    (items are sorted before hashing); content mutation → different hash.
    ``context_pack_id`` / ``created_at`` / ``job_id`` / ``dispatch_id`` are
    NOT part of this hash (§19).
    """
    return hashlib.sha256(
        canonical_content(pack.items, pack.role, pack.version).encode("utf-8")
    ).hexdigest()


def make_context_pack_id(dispatch_id: str, content_hash: str) -> str:
    """Deterministic, content-stable pack id (F2).

    Derived from ``dispatch_id`` + semantic ``content_hash`` ONLY — NOT from
    ``created_at`` — so a retry that rebuilds the same semantic content for the
    same dispatch yields the SAME pack id.  ``created_at`` remains pure
    *instance metadata* on the manifest/record and is not part of the id.
    """
    digest = hashlib.sha256(
        f"{dispatch_id}\x00{content_hash}".encode("utf-8")
    ).hexdigest()
    return "cp_" + digest[:24]


def instance_id(pack: ContextPack) -> str:
    """Return the pack's instance id (== ``context_pack_id``)."""
    return pack.context_pack_id


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_ID_PATTERN_PREFIX = "ci_"
_PACK_ID_PREFIX = "cp_"


def _valid_hex(value: str, length: int) -> bool:
    return len(value) == length and all(c in "0123456789abcdef" for c in value)


def _validate_item_bounds(it: ContextItem) -> None:
    """Enforce bounded reference/metadata limits on one item (F4)."""
    if len(it.source_ref) > MAX_SOURCE_REF_LEN:
        raise ContextBuildError(
            "CONTEXT_INVALID_REFERENCE",
            f"source_ref length {len(it.source_ref)} > {MAX_SOURCE_REF_LEN}",
        )
    if len(it.metadata) > MAX_METADATA_ENTRIES:
        raise ContextBuildError(
            "CONTEXT_INVALID_REFERENCE",
            f"metadata entries {len(it.metadata)} > {MAX_METADATA_ENTRIES}",
        )
    for k, v in it.metadata:
        if len(k) > MAX_METADATA_KEY_LEN:
            raise ContextBuildError(
                "CONTEXT_INVALID_REFERENCE",
                f"metadata key length {len(k)} > {MAX_METADATA_KEY_LEN}",
            )
        if len(v) > MAX_METADATA_VALUE_LEN:
            raise ContextBuildError(
                "CONTEXT_INVALID_REFERENCE",
                f"metadata value length {len(v)} > {MAX_METADATA_VALUE_LEN}",
            )


def validate_context_pack(pack: ContextPack) -> None:
    """Validate a Context Pack's schema (fail-closed on any violation).

    Recomputes the declared integrity/budget fields canonically (F5): each
    item's stable id and content hash are re-derived from its content;
    ``token_count`` must equal the token estimate of the canonical render;
    ``budget_estimated`` must be a consistent pre-trim estimate; the expansion
    reason must be consistent with the soft budget.  Also enforces bounded
    reference/metadata limits (F4).
    """
    if pack.version != CONTEXT_PACK_VERSION:
        raise ContextBuildError("CONTEXT_INVALID_VERSION",
                                f"version {pack.version!r} != {CONTEXT_PACK_VERSION!r}")

    if not isinstance(pack.context_pack_id, str) or \
            not pack.context_pack_id.startswith(_PACK_ID_PREFIX) or \
            not _valid_hex(pack.context_pack_id[len(_PACK_ID_PREFIX):], 24):
        raise ContextBuildError("CONTEXT_MALFORMED_ID",
                                f"malformed context_pack_id {pack.context_pack_id!r}")

    if len(pack.items) > MAX_ITEMS:
        raise ContextBuildError(
            "CONTEXT_INVALID_REFERENCE",
            f"{len(pack.items)} items > {MAX_ITEMS}",
        )

    for it in pack.items:
        try:
            TrustClass(it.trust_class)
        except ValueError:
            raise ContextBuildError(
                "CONTEXT_INVALID_TRUST_CLASS",
                f"invalid trust_class {it.trust_class!r}",
            )
        try:
            Importance(it.importance)
        except ValueError:
            raise ContextBuildError(
                "CONTEXT_INVALID_IMPORTANCE",
                f"invalid importance {it.importance!r}",
            )
        if not isinstance(it.id, str) or not it.id.startswith(_ID_PATTERN_PREFIX) or \
                not _valid_hex(it.id[len(_ID_PATTERN_PREFIX):], 16):
            raise ContextBuildError("CONTEXT_MALFORMED_ID",
                                    f"malformed item id {it.id!r}")
        # F5: the stable item id and content hash are re-derived from content.
        if it.id != make_item_id(it.trust_class, it.source_type, it.source_ref,
                                 it.content):
            raise ContextBuildError(
                "CONTEXT_MALFORMED_ID",
                f"item id {it.id!r} does not match its content",
            )
        if it.content_hash != item_content_hash(it.content):
            raise ContextBuildError(
                "CONTEXT_HASH_MISMATCH",
                f"item content_hash {it.content_hash!r} does not match content",
            )
        _validate_item_bounds(it)

    if pack.budget_soft < 0 or pack.budget_hard < pack.budget_soft:
        raise ContextBuildError("CONTEXT_INVALID_BUDGET",
                                f"soft={pack.budget_soft} hard={pack.budget_hard}")

    # F5: token_count must equal the canonical render token estimate.
    rendered = render_token_count(pack)
    if pack.token_count != rendered:
        raise ContextBuildError(
            "CONTEXT_INVALID_BUDGET",
            f"token_count={pack.token_count} != render estimate {rendered}",
        )
    if pack.token_count < 0 or pack.token_count > pack.budget_hard:
        raise ContextBuildError("CONTEXT_INVALID_BUDGET",
                                f"token_count={pack.token_count} hard={pack.budget_hard}")
    if pack.budget_estimated < pack.token_count:
        raise ContextBuildError(
            "CONTEXT_INVALID_BUDGET",
            f"budget_estimated={pack.budget_estimated} < token_count={pack.token_count}",
        )

    # F5: expansion semantics — token_count > soft ⇔ expansion_reason set.
    if (pack.token_count > pack.budget_soft) != (pack.expansion_reason is not None):
        raise ContextBuildError(
            "CONTEXT_INVALID_EXPANSION_REASON",
            "expansion_reason must be set iff token_count exceeds the soft budget",
        )

    if pack.expansion_reason is not None:
        try:
            ExpansionReason(pack.expansion_reason)
        except ValueError:
            raise ContextBuildError(
                "CONTEXT_INVALID_EXPANSION_REASON",
                f"invalid expansion_reason {pack.expansion_reason!r}",
            )

    if len(pack.provenance) > MAX_PROVENANCE_ENTRIES:
        raise ContextBuildError(
            "CONTEXT_INVALID_REFERENCE",
            f"{len(pack.provenance)} provenance entries > {MAX_PROVENANCE_ENTRIES}",
        )
    for prov in pack.provenance:
        try:
            TrustClass(prov.trust_class)
        except ValueError:
            raise ContextBuildError(
                "CONTEXT_INVALID_TRUST_CLASS",
                f"invalid provenance trust_class {prov.trust_class!r}",
            )

    if content_hash(pack) != pack.content_hash:
        raise ContextBuildError("CONTEXT_HASH_MISMATCH",
                                "content hash does not match semantic content")


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def _canonical_metadata(mapping: Optional[dict]) -> tuple:
    """Convert a metadata dict to sorted (key, value) string pairs."""
    if not mapping:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in mapping.items()))


def _metadata_get(metadata: tuple, key: str) -> str:
    for k, v in metadata:
        if k == key:
            return v
    return ""


def _project_items(items: Sequence[ContextItem]) -> dict:
    """Project the canonical ordered item list into the schema fields."""
    objective = ""
    acceptance: list = []
    constraints: list = []
    policies: list = []
    facts: list = []
    artifacts: list = []
    history: list = []
    provenance: list = []
    seen_prov: set = set()

    for it in items:
        prov = (it.source_type, it.source_ref, it.trust_class)
        if prov not in seen_prov:
            seen_prov.add(prov)
            provenance.append(ProvenanceEntry(*prov))
        if it.source_type == "objective":
            objective = it.content
        elif it.source_type == "acceptance_criteria":
            acceptance.append(it.content)
        elif it.source_type == "constraint":
            constraints.append(it.content)
        elif it.source_type == "policy_reference":
            policies.append(it.content)
        elif it.source_type == "fact":
            facts.append(it.content)
        elif it.source_type == "artifact":
            artifacts.append(ArtifactRef(
                ref=it.source_ref,
                location=_metadata_get(it.metadata, "location"),
                excerpt=it.content if it.content != it.source_ref else "",
                content_hash=_metadata_get(it.metadata, "artifact_hash"),
            ))
        elif it.source_type in ("history", "prior_result"):
            history.append(it.content)

    return {
        "objective": objective,
        "acceptance_criteria": tuple(acceptance),
        "constraints": tuple(constraints),
        "policy_references": tuple(policies),
        "facts": tuple(facts),
        "artifacts": tuple(artifacts),
        "history": tuple(history),
        "provenance": tuple(provenance),
    }


class ContextBuilder:
    """Pure, deterministic Context Pack builder (no provider coupling)."""

    def __init__(self, *, budget_policy: Optional[ContextBudgetPolicy] = None):
        self._budget_policy = budget_policy or ContextBudgetPolicy()

    # -- item construction (trust class fixed by slot, never by content) -----

    @staticmethod
    def _item(trust_class: TrustClass, importance: str, source_type: str,
              source_ref: str, content: str, metadata: Optional[dict] = None) -> ContextItem:
        content = "" if content is None else str(content)
        return ContextItem(
            id=make_item_id(trust_class.value, source_type, source_ref, content),
            trust_class=trust_class.value,
            importance=importance,
            source_type=source_type,
            source_ref=source_ref,
            content=content,
            content_hash=item_content_hash(content),
            metadata=_canonical_metadata(metadata),
        )

    def _collect_items(
        self,
        *,
        objective: str,
        acceptance_criteria: Sequence[str],
        constraints: Sequence[str],
        policy_references: Sequence[str],
        facts: Sequence[FactInput],
        artifacts: Sequence[ArtifactRef],
        history: Sequence[str],
        prior_results: Sequence[ResultInput],
    ) -> list:
        items: list = []
        items.append(self._item(
            TrustClass.OWNER_INSTRUCTION, Importance.REQUIRED.value,
            "objective", "task_objective", objective,
        ))
        for i, ac in enumerate(acceptance_criteria):
            items.append(self._item(
                TrustClass.OWNER_INSTRUCTION, Importance.REQUIRED.value,
                "acceptance_criteria", "", ac,
            ))
        for i, c in enumerate(constraints):
            items.append(self._item(
                TrustClass.TRUSTED_POLICY, Importance.REQUIRED.value,
                "constraint", "", c,
            ))
        for i, p in enumerate(policy_references):
            items.append(self._item(
                TrustClass.TRUSTED_POLICY, Importance.REQUIRED.value,
                "policy_reference", "", p,
            ))
        for f in facts:
            items.append(self._item(
                TrustClass.TRUSTED_LOCAL_FACT, f.importance, "fact",
                f.source_ref, f.content,
            ))
        for a in artifacts:
            if len(a.ref) > MAX_ARTIFACT_REF_LEN:
                raise ContextBuildError(
                    "CONTEXT_INVALID_REFERENCE",
                    f"artifact ref length {len(a.ref)} > {MAX_ARTIFACT_REF_LEN}",
                )
            if len(a.location) > MAX_ARTIFACT_LOCATION_LEN:
                raise ContextBuildError(
                    "CONTEXT_INVALID_REFERENCE",
                    f"artifact location length {len(a.location)} > "
                    f"{MAX_ARTIFACT_LOCATION_LEN}",
                )
            content = a.excerpt if a.excerpt else a.ref
            items.append(self._item(
                TrustClass.TRUSTED_ARTIFACT, Importance.NORMAL.value,
                "artifact", a.ref, content,
                metadata={"location": a.location, "artifact_hash": a.content_hash},
            ))
        for i, h in enumerate(history):
            items.append(self._item(
                TrustClass.OPTIONAL_HISTORY, Importance.OPTIONAL.value,
                "history", "", h,
            ))
        for r in prior_results:
            items.append(self._item(
                TrustClass.AGENT_RESULT, r.importance, "prior_result",
                r.source_ref, r.content,
            ))
        return items

    # -- dedup --------------------------------------------------------------

    @staticmethod
    def _dedup(items: Sequence[ContextItem]) -> list:
        """Collapse duplicates (same trust/source/ref/content) to one item.

        The higher importance wins on a collision (a REQUIRED fact beats an
        OPTIONAL duplicate of the same text).  Returns items in a deterministic
        canonical order.
        """
        by_key: dict = {}
        for it in items:
            key = (it.trust_class, it.source_type, it.source_ref, it.content_hash)
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = it
            elif _IMPORTANCE_RANK.get(it.importance, 0) > \
                    _IMPORTANCE_RANK.get(existing.importance, 0):
                by_key[key] = it.with_importance(it.importance)
        return sorted(
            by_key.values(),
            key=lambda it: (-_IMPORTANCE_RANK.get(it.importance, 0),
                            it.trust_class, it.id),
        )

    # -- trimming -----------------------------------------------------------

    @staticmethod
    def _trim_group(it: ContextItem) -> Optional[int]:
        """Trim priority (lower removed first); ``None`` = never removable.

        Order (§12): OPTIONAL_HISTORY → redundant AGENT_RESULT → OPTIONAL →
        NORMAL → HIGH (only if referenceable) → REQUIRED (never).
        """
        if it.importance == Importance.REQUIRED.value:
            return None
        if it.trust_class == TrustClass.OPTIONAL_HISTORY.value:
            return 0
        if it.trust_class == TrustClass.AGENT_RESULT.value:
            return 1
        if it.importance == Importance.OPTIONAL.value:
            return 2
        if it.importance == Importance.NORMAL.value:
            return 3
        if it.importance == Importance.HIGH.value:
            # HIGH is only removable when it can be re-fetched by reference.
            return 4 if it.source_ref else None
        return 3

    @classmethod
    def _trim(cls, items: Sequence[ContextItem], soft: int, render_tokens) -> list:
        """Deterministically trim removable items until ``<= soft`` rendered tokens.

        ``render_tokens`` maps a candidate item list to the token estimate of
        its canonical render (so the render overhead is counted, F4).
        """
        remaining = list(items)
        if render_tokens(remaining) <= soft:
            return remaining
        removable = [it for it in remaining if cls._trim_group(it) is not None]
        removable.sort(key=lambda it: (cls._trim_group(it), it.id))
        for it in removable:
            remaining.remove(it)
            if render_tokens(remaining) <= soft:
                break
        return remaining

    # -- projection ---------------------------------------------------------

    @staticmethod
    def _project(items: Sequence[ContextItem]) -> dict:
        """Project items into schema fields (delegates to :func:`_project_items`)."""
        return _project_items(items)

    # -- build --------------------------------------------------------------

    def build(
        self,
        *,
        job_id: str,
        dispatch_id: str,
        role: str,
        objective: str,
        acceptance_criteria: Sequence[str] = (),
        constraints: Sequence[str] = (),
        policy_references: Sequence[str] = (),
        facts: Sequence[FactInput] = (),
        artifacts: Sequence[ArtifactRef] = (),
        history: Sequence[str] = (),
        prior_results: Sequence[ResultInput] = (),
        budget_policy: Optional[ContextBudgetPolicy] = None,
        capability: str = CapabilityTier.FLASH.value,
        expansion_reason: Optional[str] = None,
        now_iso: str = "",
    ) -> ContextPack:
        """Build an immutable Context Pack (deterministic, fail-closed).

        Raises :class:`ContextBuildError` with code ``CONTEXT_BUDGET_EXCEEDED``
        when the REQUIRED context exceeds the hard budget, or when the pack
        exceeds the soft budget without a permitted bounded expansion reason.
        """
        policy = budget_policy or self._budget_policy
        tier = policy.tier_for(capability)
        soft = tier.soft_max
        hard = tier.hard

        items = self._collect_items(
            objective=objective,
            acceptance_criteria=acceptance_criteria,
            constraints=constraints,
            policy_references=policy_references,
            facts=facts,
            artifacts=artifacts,
            history=history,
            prior_results=prior_results,
        )
        items = self._dedup(items)

        if len(items) > MAX_ITEMS:
            raise ContextBuildError(
                "CONTEXT_INVALID_REFERENCE",
                f"{len(items)} items > {MAX_ITEMS}",
            )

        # Render-based token estimation (F4): the budget is enforced against
        # the FULL canonical render (identity fields + labels + reference/
        # metadata fields + closing instruction), not just ``item.content``.
        def render_tokens(its):
            return _render_tokens_for(
                its, job_id=job_id, dispatch_id=dispatch_id, role=role,
                version=CONTEXT_PACK_VERSION,
            )

        required_render = render_tokens(
            [it for it in items if it.importance == Importance.REQUIRED.value]
        )
        if required_render > hard:
            raise ContextBuildError(
                CONTEXT_BUDGET_EXCEEDED,
                f"required context ({required_render} tokens) exceeds hard "
                f"budget ({hard})",
            )

        estimated = render_tokens(items)
        final_items = items
        final_reason: Optional[str] = None

        if estimated > soft:
            final_items = self._trim(items, soft, render_tokens)
            trimmed_total = render_tokens(final_items)
            if trimmed_total > soft:
                if trimmed_total > hard:
                    raise ContextBuildError(
                        CONTEXT_BUDGET_EXCEEDED,
                        f"context ({trimmed_total} tokens) exceeds hard budget "
                        f"({hard}) after deterministic trimming",
                    )
                if not policy.allow_expansion or expansion_reason is None:
                    raise ContextBuildError(
                        CONTEXT_BUDGET_EXCEEDED,
                        "context exceeds soft budget without a permitted "
                        "expansion reason",
                    )
                final_reason = ExpansionReason(expansion_reason).value

        proj = self._project(final_items)
        content_h = hashlib.sha256(
            canonical_content(final_items, role, CONTEXT_PACK_VERSION).encode("utf-8")
        ).hexdigest()
        created_at = now_iso or ""
        pack_id = make_context_pack_id(dispatch_id, content_h)

        pack = ContextPack(
            version=CONTEXT_PACK_VERSION,
            context_pack_id=pack_id,
            job_id=job_id,
            dispatch_id=dispatch_id,
            role=role,
            created_at=created_at,
            objective=proj["objective"],
            acceptance_criteria=proj["acceptance_criteria"],
            constraints=proj["constraints"],
            policy_references=proj["policy_references"],
            facts=proj["facts"],
            artifacts=proj["artifacts"],
            history=proj["history"],
            budget_soft=soft,
            budget_hard=hard,
            budget_estimated=estimated,
            token_count=render_tokens(final_items),
            expansion_reason=final_reason,
            provenance=proj["provenance"],
            content_hash=content_h,
            items=tuple(final_items),
        )
        # Self-check before returning (cheap; catches invariant drift).
        validate_context_pack(pack)
        return pack


# ---------------------------------------------------------------------------
# Message-file rendering (provider-neutral; the caller writes it to disk)
# ---------------------------------------------------------------------------

#: Fixed-length placeholder for the context_pack_id during pre-trim token
#: estimation.  A pack id is always ``cp_`` + 24 hex chars (27 chars), so its
#: exact value cannot change the rendered length/token count — only the content
#: does.  The real id is computed after trimming (it depends on the final
#: content hash) and yields the identical token estimate.
_PACK_ID_PLACEHOLDER = "cp_" + "0" * 24


def _render_message(
    *,
    context_pack_id: str,
    version: str,
    job_id: str,
    dispatch_id: str,
    role: str,
    objective: str,
    acceptance_criteria: Sequence[str],
    constraints: Sequence[str],
    policy_references: Sequence[str],
    facts: Sequence[str],
    artifacts: Sequence[ArtifactRef],
    history: Sequence[str],
) -> str:
    """Render the deterministic, privacy-safe prompt text (single source)."""
    lines: list = [
        "You are an agent in a deterministic, isolated development team.",
        f"context_pack_id: {context_pack_id}",
        f"version: {version}",
        f"job_id: {job_id}",
        f"dispatch_id: {dispatch_id}",
        f"role: {role}",
        "objective:",
        objective,
    ]
    if acceptance_criteria:
        lines.append("acceptance_criteria:")
        lines.extend(f"- {ac}" for ac in acceptance_criteria)
    if constraints:
        lines.append("constraints:")
        lines.extend(f"- {c}" for c in constraints)
    if policy_references:
        lines.append("policy_references:")
        lines.extend(f"- {p}" for p in policy_references)
    if facts:
        lines.append("facts:")
        lines.extend(f"- {f}" for f in facts)
    if artifacts:
        lines.append("artifacts:")
        for a in artifacts:
            lines.append(f"- ref: {a.ref}")
            if a.location:
                lines.append(f"  location: {a.location}")
            if a.excerpt:
                lines.append(f"  excerpt: {a.excerpt}")
    if history:
        lines.append("history:")
        lines.extend(f"- {h}" for h in history)
    lines.append("Reply with exactly one JSON object matching your role schema.")
    return "\n".join(lines) + "\n"


def render_pack(pack: ContextPack, *, context_pack_id: Optional[str] = None) -> str:
    """Render a Context Pack to a deterministic, privacy-safe prompt text.

    This is the single source for the agent message file in a D1-migrated
    dispatch.  Only the bounded, trusted pack content is rendered — never an
    unbounded legacy history dump.  ``context_pack_id`` overrides the id when
    the caller must render the EXACT persisted id (F2).
    """
    return _render_message(
        context_pack_id=context_pack_id if context_pack_id is not None else pack.context_pack_id,
        version=pack.version,
        job_id=pack.job_id,
        dispatch_id=pack.dispatch_id,
        role=pack.role,
        objective=pack.objective,
        acceptance_criteria=pack.acceptance_criteria,
        constraints=pack.constraints,
        policy_references=pack.policy_references,
        facts=pack.facts,
        artifacts=pack.artifacts,
        history=pack.history,
    )


def render_token_count(pack: ContextPack) -> int:
    """Token estimate of the FULL canonical render (F4/F5).

    This is the single source of truth for a pack's token budget: it counts
    every deterministic byte of the rendered message (identity fields, labels,
    reference/metadata fields, and the closing instruction), not just
    ``item.content``.
    """
    return estimate_tokens(render_pack(pack))


def _render_tokens_for(
    items: Sequence[ContextItem],
    *,
    job_id: str,
    dispatch_id: str,
    role: str,
    version: str,
) -> int:
    """Token estimate of the canonical render for a candidate item set."""
    proj = _project_items(items)
    return estimate_tokens(_render_message(
        context_pack_id=_PACK_ID_PLACEHOLDER,
        version=version,
        job_id=job_id,
        dispatch_id=dispatch_id,
        role=role,
        objective=proj["objective"],
        acceptance_criteria=proj["acceptance_criteria"],
        constraints=proj["constraints"],
        policy_references=proj["policy_references"],
        facts=proj["facts"],
        artifacts=proj["artifacts"],
        history=proj["history"],
    ))
