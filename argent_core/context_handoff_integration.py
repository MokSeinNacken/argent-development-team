"""Phase D2 — Context-Builder ↔ Retrieval/Handoff/Checkpoint integration.

This module is the ONLY glue between the D1 immutable Context Pack and the D2
retrieval/handoff/checkpoint modules.  It **never bypasses D1**: the D1
``ContextBuilder`` remains the single budget/integrity authority; this module
only maps bounded retrieval/handoff/checkpoint results into D1 input slots
(``FactInput`` / ``ArtifactRef`` / ``ResultInput``) with a locally-determined
TrustClass/Importance.

Trust mapping (D1 TrustClass, §16):

* FACT_LOOKUP / CHECKPOINT_LOOKUP → ``FactInput`` → ``TRUSTED_LOCAL_FACT``
* ARTIFACT_LOOKUP / EXACT_REF / FILE_EXCERPT / SYMBOL_OR_TEXT_MATCH →
  ``ArtifactRef`` → ``TRUSTED_ARTIFACT``
* HANDOFF_LOOKUP → ``ResultInput`` → ``AGENT_RESULT`` (never policy)
* Optional history is only added when explicitly requested (``OPTIONAL_HISTORY``)

Owner objective / acceptance criteria / constraints / policy references always
remain REQUIRED and are passed straight through to D1.
"""

from __future__ import annotations

from typing import Optional, Sequence

from .context_pack import (
    ArtifactRef,
    CapabilityTier,
    ContextBuilder,
    ContextPack,
    FactInput,
    ResultInput,
)
from .retrieval import (
    RetrievalRequest,
    RetrievalResult,
    RetrievalType,
)
from .checkpoint import CheckpointRecord, checkpoint_validated_inputs


def _handoff_content(items) -> str:
    """Render handoff items as a bounded, deterministic observation summary."""
    return "\n".join(
        f"[{it.source_type}] {it.ref}: {it.content}" for it in items
    )


def map_retrieval_to_inputs(
    result: RetrievalResult,
    *,
    facts: list,
    artifacts: list,
    prior_results: list,
    history: list,
) -> None:
    """Map a retrieval result into D1 builder inputs (local trust assignment)."""
    for it in result.items:
        st = it.source_type
        if st == RetrievalType.FACT_LOOKUP.value:
            facts.append(FactInput(content=it.content, source_ref=it.ref))
        elif st == RetrievalType.CHECKPOINT_LOOKUP.value:
            facts.append(FactInput(content=it.content, source_ref=it.ref))
        elif st == RetrievalType.HANDOFF_LOOKUP.value:
            prior_results.append(ResultInput(
                content=it.content or it.ref, source_ref=it.ref))
        elif st in (RetrievalType.ARTIFACT_LOOKUP.value,
                    RetrievalType.EXACT_REF.value,
                    RetrievalType.FILE_EXCERPT.value,
                    RetrievalType.SYMBOL_OR_TEXT_MATCH.value):
            artifacts.append(ArtifactRef(
                ref=it.ref, location=it.location, excerpt=it.content,
                content_hash=it.content_hash,
            ))
        else:
            # Unknown/optional source → OPTIONAL_HISTORY (never authority).
            history.append(it.content or it.ref)


def build_pack_with_retrieval(
    *,
    context_builder: ContextBuilder,
    job_id: str,
    dispatch_id: str,
    role: str,
    objective: str,
    acceptance_criteria: Sequence[str] = (),
    constraints: Sequence[str] = (),
    policy_references: Sequence[str] = (),
    facts: Sequence[FactInput] = (),
    artifacts: Sequence[ArtifactRef] = (),
    prior_results: Sequence[ResultInput] = (),
    history: Sequence[str] = (),
    retriever=None,
    retrieval_requests: Sequence[RetrievalRequest] = (),
    checkpoint: Optional[CheckpointRecord] = None,
    checkpoint_current_facts: Optional[dict] = None,
    capability: str = CapabilityTier.FLASH.value,
    expansion_reason: Optional[str] = None,
    now_iso: str = "",
) -> ContextPack:
    """Build an immutable Context Pack, enriching D1 inputs with bounded
    retrieval + optional checkpoint-resume — D1 remains the budget authority.

    * ``checkpoint`` (optional): resume from a valid checkpoint (its artifact /
      handoff / source refs are added; objective/acceptance/constraints always
      come from the caller).
    * ``retrieval_requests`` (optional): bounded retrieval results mapped into
      D1 inputs with a local TrustClass.
    """
    fact_list: list = list(facts)
    artifact_list: list = list(artifacts)
    prior_list: list = list(prior_results)
    history_list: list = list(history)

    if checkpoint is not None:
        # Resume enriches the pack with the checkpoint's validated refs — but it
        # does NOT skip retrieval.  Both checkpoint refs AND bounded retrieval
        # flow through the SAME D1 builder run (D1 remains the single
        # budget/integrity authority; no early return, no D1 bypass).
        cp_artifacts, cp_prior, cp_facts = checkpoint_validated_inputs(
            checkpoint, job_id, checkpoint_current_facts)
        artifact_list.extend(cp_artifacts)
        prior_list.extend(cp_prior)
        fact_list.extend(cp_facts)

    if retriever is not None:
        for request in retrieval_requests:
            result = retriever.execute(request)
            map_retrieval_to_inputs(
                result, facts=fact_list, artifacts=artifact_list,
                prior_results=prior_list, history=history_list,
            )

    return context_builder.build(
        job_id=job_id,
        dispatch_id=dispatch_id,
        role=role,
        objective=objective,
        acceptance_criteria=acceptance_criteria,
        constraints=constraints,
        policy_references=policy_references,
        facts=tuple(fact_list),
        artifacts=tuple(artifact_list),
        prior_results=tuple(prior_list),
        history=tuple(history_list),
        capability=capability,
        expansion_reason=expansion_reason,
        now_iso=now_iso,
    )
