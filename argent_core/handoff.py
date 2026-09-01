"""Phase D2 — structured Handoff record (ARGENT V1 FINAL §12/§16).

A **HandoffRecord** is a bounded, immutable, structured summary of an agent
step's outcome, artifacts, evidence and proposed next step.  It is persisted
additively alongside the existing minimal ``Handoff`` workflow row (which
remains untouched — see ``models.Handoff``).

Hard invariants (verbindlich):

* **A handoff can NEVER carry owner/policy rules.**  ``trust_class`` is
  forced to ``AGENT_RESULT``; any attempt to construct/validate a handoff with
  a policy/owner trust class (or policy marker content) raises ``ValueError``.
* **Structured != trusted.**  The ContextBuilder still treats handoff-derived
  items as ``AGENT_RESULT`` (D1 TrustClass).  Structure adds provenance, not
  authority.
* **Bounded fields.**  Every string/list is length-capped; oversized content is
  rejected, never silently truncated into authority.
* **Deterministic hash.**  ``content_hash`` is computed over semantic fields
  only (identity/result/artifacts/evidence/next-step/provenance); volatile
  instance metadata (``handoff_id``, ``created_at``) is excluded.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional, Sequence

from .models import ArgentError, Role
from .context_pack import ContextError

HANDOFF_VERSION = "1"

# Bounded field limits.
MAX_HANDOFF_ID_LEN = 128
MAX_ROLE_LEN = 32
MAX_OUTCOME_LEN = 128
MAX_OBSERVATIONS = 64
MAX_OBSERVATION_LEN = 2048
MAX_DECISIONS = 32
MAX_DECISION_LEN = 2048
MAX_QUESTIONS = 32
MAX_QUESTION_LEN = 2048
MAX_ARTIFACTS = 128
MAX_ARTIFACT_REF_LEN = 512
MAX_ARTIFACT_EXCERPT_LEN = 8192
MAX_EVIDENCE_REFS = 128
MAX_EVIDENCE_REF_LEN = 512
MAX_FACT_LEN = 2048
MAX_NEXT_STEP_REFS = 64
MAX_NEXT_STEP_REF_LEN = 512
MAX_CAPABILITY_LEN = 128
MAX_AGENT_ID_LEN = 128

_HEX_CHARS = frozenset("0123456789abcdef")


def _is_sha256_hex(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        c in _HEX_CHARS for c in value)


def _is_handoff_id(value) -> bool:
    return (isinstance(value, str) and value.startswith("ho_")
            and len(value) == len("ho_") + 24
            and all(c in _HEX_CHARS for c in value[len("ho_"):]))


class HandoffError(ContextError):
    """A handoff record failed validation (bounded code)."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code, detail)


#: Forbidden trust classes for a handoff (a handoff is NEVER policy/owner).
_FORBIDDEN_TRUST_CLASSES = frozenset({
    "OWNER_INSTRUCTION", "TRUSTED_POLICY", "TRUSTED_LOCAL_FACT",
    "TRUSTED_ARTIFACT", "EXTERNAL_UNTRUSTED", "OPTIONAL_HISTORY",
})

#: Policy-marker substrings that a handoff payload must never contain.
_POLICY_MARKERS = (
    "IMPORTANT SYSTEM POLICY",
    "OWNER_INSTRUCTION",
    "TRUSTED_POLICY",
    "SYSTEM:",
)


@dataclass(frozen=True)
class HandoffResult:
    outcome: str = ""
    key_observations: tuple = ()
    decisions: tuple = ()
    unresolved_questions: tuple = ()


@dataclass(frozen=True)
class HandoffArtifact:
    ref: str
    content_hash: str = ""
    excerpt: str = ""


@dataclass(frozen=True)
class HandoffEvidence:
    test_refs: tuple = ()
    commit_refs: tuple = ()
    diff_refs: tuple = ()
    trusted_facts: tuple = ()   # trusted local facts (from the ledger)
    observations: tuple = ()    # agent observations (UNTRUSTED data)


@dataclass(frozen=True)
class HandoffNextStep:
    proposed_capability: str = ""
    required_context_refs: tuple = ()


@dataclass(frozen=True)
class HandoffProvenance:
    source_agent_id: str = ""
    source_dispatch_id: str = ""
    trust_class: str = "AGENT_RESULT"


@dataclass(frozen=True)
class HandoffRecord:
    handoff_version: str
    handoff_id: str
    job_id: str
    source_dispatch_id: str
    source_role: str
    created_at: str
    result: HandoffResult
    artifacts: tuple          # tuple[HandoffArtifact, ...]
    evidence: HandoffEvidence
    next_step: HandoffNextStep
    provenance: HandoffProvenance
    content_hash: str


# ---------------------------------------------------------------------------
# Hashing / id
# ---------------------------------------------------------------------------


def _stable_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _canonical_doc(rec: HandoffRecord) -> dict:
    """Canonical semantic document (volatile metadata excluded)."""
    return {
        "handoff_version": rec.handoff_version,
        "job_id": rec.job_id,
        "source_dispatch_id": rec.source_dispatch_id,
        "source_role": rec.source_role,
        "result": {
            "outcome": rec.result.outcome,
            "key_observations": list(rec.result.key_observations),
            "decisions": list(rec.result.decisions),
            "unresolved_questions": list(rec.result.unresolved_questions),
        },
        "artifacts": [
            {"ref": a.ref, "content_hash": a.content_hash, "excerpt": a.excerpt}
            for a in rec.artifacts
        ],
        "evidence": {
            "test_refs": list(rec.evidence.test_refs),
            "commit_refs": list(rec.evidence.commit_refs),
            "diff_refs": list(rec.evidence.diff_refs),
            "trusted_facts": list(rec.evidence.trusted_facts),
            "observations": list(rec.evidence.observations),
        },
        "next_step": {
            "proposed_capability": rec.next_step.proposed_capability,
            "required_context_refs": list(rec.next_step.required_context_refs),
        },
        "provenance": {
            "source_agent_id": rec.provenance.source_agent_id,
            "source_dispatch_id": rec.provenance.source_dispatch_id,
            "trust_class": rec.provenance.trust_class,
        },
    }


def handoff_content_hash(rec: HandoffRecord) -> str:
    """Deterministic semantic content hash (instance metadata excluded)."""
    return hashlib.sha256(
        _stable_json(_canonical_doc(rec)).encode("utf-8")
    ).hexdigest()


def make_handoff_id(source_dispatch_id: str, content_hash: str) -> str:
    """Deterministic, content-stable handoff id."""
    digest = hashlib.sha256(
        f"{source_dispatch_id}\x00{content_hash}".encode("utf-8")
    ).hexdigest()
    return "ho_" + digest[:24]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _bounded_str(value, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a str")
    if len(value) > limit:
        raise ValueError(f"{field} exceeds {limit} chars")
    return value


def _bounded_tuple(values, field: str, count: int, limit: int) -> tuple:
    if not isinstance(values, (tuple, list)):
        raise ValueError(f"{field} must be a tuple/list")
    out = []
    for v in values:
        if not isinstance(v, str):
            raise ValueError(f"{field} entries must be str")
        if len(v) > limit:
            raise ValueError(f"{field} entry exceeds {limit} chars")
        out.append(v)
    if len(out) > count:
        raise ValueError(f"{field} has {len(out)} entries > {count}")
    return tuple(out)


def _check_no_policy_markers(*values: str) -> None:
    for value in values:
        for marker in _POLICY_MARKERS:
            if marker in (value or ""):
                raise ValueError(
                    f"handoff content contains a forbidden policy marker "
                    f"{marker!r}")


def validate_handoff_record(rec: HandoffRecord) -> None:
    """Validate a HandoffRecord's schema (fail-closed; raises ``ValueError``).

    Enforces: version, bounded fields, closed status semantics, ``trust_class``
    forced to ``AGENT_RESULT`` (a handoff can NEVER carry policy/owner rules),
    and a canonical recomputation of ``content_hash``.
    """
    if rec.handoff_version != HANDOFF_VERSION:
        raise ValueError(f"handoff_version {rec.handoff_version!r} != {HANDOFF_VERSION!r}")
    _bounded_str(rec.handoff_id, "handoff_id", MAX_HANDOFF_ID_LEN)
    if not _is_handoff_id(rec.handoff_id):
        raise ValueError(f"malformed handoff_id {rec.handoff_id!r}")
    if not _is_sha256_hex(rec.content_hash):
        raise ValueError("handoff content_hash must be sha256 hex")
    _bounded_str(rec.job_id, "job_id", MAX_HANDOFF_ID_LEN)
    _bounded_str(rec.source_dispatch_id, "source_dispatch_id", MAX_HANDOFF_ID_LEN)
    role = _bounded_str(rec.source_role, "source_role", MAX_ROLE_LEN)
    try:
        Role(role)
    except ValueError:
        raise ValueError(f"source_role {role!r} is not a valid Role")

    # Trust class is FORCED to AGENT_RESULT — never policy/owner.
    tc = rec.provenance.trust_class
    if tc != "AGENT_RESULT":
        raise ValueError(
            f"handoff trust_class {tc!r} is not AGENT_RESULT; a handoff can "
            "never carry policy/owner trust")
    if tc in _FORBIDDEN_TRUST_CLASSES:
        raise ValueError(f"handoff trust_class {tc!r} is forbidden")

    _bounded_str(rec.result.outcome, "outcome", MAX_OUTCOME_LEN)
    _validate_result(rec.result)

    if len(rec.artifacts) > MAX_ARTIFACTS:
        raise ValueError(f"artifacts has {len(rec.artifacts)} entries > {MAX_ARTIFACTS}")
    for a in rec.artifacts:
        if len(a.ref) > MAX_ARTIFACT_REF_LEN:
            raise ValueError(f"artifact ref exceeds {MAX_ARTIFACT_REF_LEN} chars")
        if len(a.excerpt) > MAX_ARTIFACT_EXCERPT_LEN:
            raise ValueError(f"artifact excerpt exceeds {MAX_ARTIFACT_EXCERPT_LEN} chars")
        if a.content_hash and not _is_sha256_hex(a.content_hash):
            raise ValueError("artifact content_hash must be sha256 hex")

    ev = rec.evidence
    _bounded_tuple(ev.test_refs, "test_refs", MAX_EVIDENCE_REFS, MAX_EVIDENCE_REF_LEN)
    _bounded_tuple(ev.commit_refs, "commit_refs", MAX_EVIDENCE_REFS, MAX_EVIDENCE_REF_LEN)
    _bounded_tuple(ev.diff_refs, "diff_refs", MAX_EVIDENCE_REFS, MAX_EVIDENCE_REF_LEN)
    _bounded_tuple(ev.trusted_facts, "trusted_facts", MAX_EVIDENCE_REFS, MAX_FACT_LEN)
    _bounded_tuple(ev.observations, "observations", MAX_EVIDENCE_REFS, MAX_FACT_LEN)

    ns = rec.next_step
    _bounded_str(ns.proposed_capability, "proposed_capability", MAX_CAPABILITY_LEN)
    _bounded_tuple(ns.required_context_refs, "required_context_refs",
                   MAX_NEXT_STEP_REFS, MAX_NEXT_STEP_REF_LEN)

    _bounded_str(rec.provenance.source_agent_id, "source_agent_id", MAX_AGENT_ID_LEN)

    # Policy-marker rejection (never carry owner/policy rules as text either).
    _check_no_policy_markers(
        rec.result.outcome,
        *rec.result.key_observations,
        *rec.result.decisions,
        *rec.result.unresolved_questions,
        *[a.excerpt for a in rec.artifacts],
        *ev.observations,
        *ev.trusted_facts,
        ns.proposed_capability,
    )

    if handoff_content_hash(rec) != rec.content_hash:
        raise ValueError("handoff content_hash does not match semantic content")


def _validate_result(result: HandoffResult) -> None:
    """Validate the bounded result sub-record (no mutation)."""
    _bounded_tuple(result.key_observations, "key_observations",
                   MAX_OBSERVATIONS, MAX_OBSERVATION_LEN)
    _bounded_tuple(result.decisions, "decisions", MAX_DECISIONS, MAX_DECISION_LEN)
    _bounded_tuple(result.unresolved_questions, "unresolved_questions",
                   MAX_QUESTIONS, MAX_QUESTION_LEN)


# ---------------------------------------------------------------------------
# Construction helper (build + hash + id in one deterministic step)
# ---------------------------------------------------------------------------


def build_handoff_record(
    *,
    job_id: str,
    source_dispatch_id: str,
    source_role: str,
    created_at: str = "",
    result: Optional[HandoffResult] = None,
    artifacts: Sequence[HandoffArtifact] = (),
    evidence: Optional[HandoffEvidence] = None,
    next_step: Optional[HandoffNextStep] = None,
    provenance: Optional[HandoffProvenance] = None,
) -> HandoffRecord:
    """Build, hash and validate a HandoffRecord (deterministic).

    ``content_hash`` and ``handoff_id`` are derived; ``created_at`` is pure
    instance metadata (excluded from the hash).  Raises ``ValueError`` on any
    validation violation.
    """
    result = result or HandoffResult()
    evidence = evidence or HandoffEvidence()
    next_step = next_step or HandoffNextStep()
    provenance = provenance or HandoffProvenance()
    rec = HandoffRecord(
        handoff_version=HANDOFF_VERSION,
        handoff_id="",  # derived below
        job_id=job_id,
        source_dispatch_id=source_dispatch_id,
        source_role=source_role,
        created_at=created_at,
        result=result,
        artifacts=tuple(artifacts),
        evidence=evidence,
        next_step=next_step,
        provenance=provenance,
        content_hash="",
    )
    content_h = handoff_content_hash(rec)
    handoff_id = make_handoff_id(source_dispatch_id, content_h)
    rec = HandoffRecord(
        handoff_version=HANDOFF_VERSION,
        handoff_id=handoff_id,
        job_id=job_id,
        source_dispatch_id=source_dispatch_id,
        source_role=source_role,
        created_at=created_at,
        result=result,
        artifacts=tuple(artifacts),
        evidence=evidence,
        next_step=next_step,
        provenance=provenance,
        content_hash=content_h,
    )
    validate_handoff_record(rec)
    return rec


# ---------------------------------------------------------------------------
# (De)serialization for the store (bounded JSON)
# ---------------------------------------------------------------------------


def handoff_to_store_json(rec: HandoffRecord) -> dict:
    """Serialize a validated HandoffRecord to bounded store columns."""
    return {
        "record_version": rec.handoff_version,
        "handoff_id": rec.handoff_id,
        "job_id": rec.job_id,
        "source_dispatch_id": rec.source_dispatch_id,
        "source_role": rec.source_role,
        "result_json": _stable_json({
            "outcome": rec.result.outcome,
            "key_observations": list(rec.result.key_observations),
            "decisions": list(rec.result.decisions),
            "unresolved_questions": list(rec.result.unresolved_questions),
        }),
        "artifacts_json": _stable_json([
            {"ref": a.ref, "content_hash": a.content_hash, "excerpt": a.excerpt}
            for a in rec.artifacts
        ]),
        "evidence_json": _stable_json({
            "test_refs": list(rec.evidence.test_refs),
            "commit_refs": list(rec.evidence.commit_refs),
            "diff_refs": list(rec.evidence.diff_refs),
            "trusted_facts": list(rec.evidence.trusted_facts),
            "observations": list(rec.evidence.observations),
        }),
        "next_step_json": _stable_json({
            "proposed_capability": rec.next_step.proposed_capability,
            "required_context_refs": list(rec.next_step.required_context_refs),
        }),
        "provenance_json": _stable_json({
            "source_agent_id": rec.provenance.source_agent_id,
            "source_dispatch_id": rec.provenance.source_dispatch_id,
            "trust_class": rec.provenance.trust_class,
        }),
        "content_hash": rec.content_hash,
        "created_at": rec.created_at,
    }


def handoff_from_store_row(row: dict) -> HandoffRecord:
    """Deserialize a store row back into a HandoffRecord."""
    record_version = row.get("record_version")
    if record_version != HANDOFF_VERSION:
        raise ValueError(
            f"persisted record_version {record_version!r} != {HANDOFF_VERSION!r}")

    def _load(col: str, default):
        try:
            return json.loads(row.get(col) or "{}")
        except Exception:
            return default

    result = _load("result_json", {})
    evidence = _load("evidence_json", {})
    next_step = _load("next_step_json", {})
    provenance = _load("provenance_json", {})
    artifacts = _load("artifacts_json", [])
    return HandoffRecord(
        handoff_version=record_version,
        handoff_id=row["handoff_id"],
        job_id=row["job_id"],
        source_dispatch_id=row["source_dispatch_id"],
        source_role=row["source_role"],
        created_at=row["created_at"],
        result=HandoffResult(
            outcome=result.get("outcome", ""),
            key_observations=tuple(result.get("key_observations", [])),
            decisions=tuple(result.get("decisions", [])),
            unresolved_questions=tuple(result.get("unresolved_questions", [])),
        ),
        artifacts=tuple(
            HandoffArtifact(ref=a.get("ref", ""),
                            content_hash=a.get("content_hash", ""),
                            excerpt=a.get("excerpt", ""))
            for a in artifacts
        ),
        evidence=HandoffEvidence(
            test_refs=tuple(evidence.get("test_refs", [])),
            commit_refs=tuple(evidence.get("commit_refs", [])),
            diff_refs=tuple(evidence.get("diff_refs", [])),
            trusted_facts=tuple(evidence.get("trusted_facts", [])),
            observations=tuple(evidence.get("observations", [])),
        ),
        next_step=HandoffNextStep(
            proposed_capability=next_step.get("proposed_capability", ""),
            required_context_refs=tuple(next_step.get("required_context_refs", [])),
        ),
        provenance=HandoffProvenance(
            source_agent_id=provenance.get("source_agent_id", ""),
            source_dispatch_id=provenance.get("source_dispatch_id", ""),
            trust_class=provenance.get("trust_class", "AGENT_RESULT"),
        ),
        content_hash=row["content_hash"],
    )
