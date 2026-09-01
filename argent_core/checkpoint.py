"""Phase D2 — immutable Checkpoints + resume (ARGENT V1 FINAL §12/§14/§16).

A **CheckpointRecord** is the bounded, reproducible state needed to rebuild a
fresh Context Pack after a restart — NOT a raw prompt/session-history dump.

Hard invariants (verbindlich):

* **Immutable (INSERT-only).**  A checkpoint is never overwritten; a new state
  increments ``checkpoint_no``.  Only the mutable "latest" pointer moves (CAS /
  lease fencing).
* **Fenced creation.**  Only the current lease holder (``owner_instance_id`` +
  ``lease_epoch``) may create the next checkpoint; a stale holder is refused
  (``LeaseFencedError``), never silently written.
* **Integrity.**  ``content_hash`` is recomputed canonically on load; any drift
  fails closed (``CONTEXT_CHECKPOINT_INVALID``).
* **Stale detection.**  ``checkpoint_references_valid`` verifies artifact
  existence + hash, worktree HEAD, handoff refs and pack refs against current
  trusted facts.  A changed file/hash/HEAD ⇒ ``STALE_CONTEXT_REFERENCE``
  (fail-closed, no silent "similar file" substitution).
* **Resume builds a NEW pack.**  ``resume_context`` runs the D1 ``ContextBuilder``
  over trusted facts + checkpoint refs — never reuses the old prompt.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from .models import ArgentError, LeaseFencedError, NotFound
from .context_pack import (
    ArtifactRef,
    CapabilityTier,
    ContextBuilder,
    ContextError,
    ContextPack,
    FactInput,
    ResultInput,
)

CHECKPOINT_VERSION = "1"

# Bounded field limits.
MAX_CHECKPOINT_ID_LEN = 128
MAX_STATE_LEN = 64
MAX_STEP_LEN = 128
MAX_QUEUE_META = 16
MAX_SOURCE_REFS = 128
MAX_REF_LEN = 512
MAX_MILESTONES = 128
MAX_MILESTONE_LEN = 512
MAX_QUESTIONS = 64
MAX_QUESTION_LEN = 1024
MAX_CODE_PATH_LEN = 1024     # worktree_path / repo_identity (paths)
MAX_COMMIT_LEN = 64          # base_commit / head_commit (git SHA)

_HEX_CHARS = frozenset("0123456789abcdef")


def _is_sha256_hex(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        c in _HEX_CHARS for c in value)


def _is_checkpoint_id(value) -> bool:
    return (isinstance(value, str) and value.startswith("ck_")
            and len(value) == len("ck_") + 24
            and all(c in _HEX_CHARS for c in value[len("ck_"):]))


class CheckpointError(ContextError):
    """A checkpoint operation failed (bounded code, fail-closed)."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code, detail)


#: Canonical checkpoint failure codes.
CONTEXT_CHECKPOINT_INVALID = "CONTEXT_CHECKPOINT_INVALID"
STALE_CONTEXT_REFERENCE = "STALE_CONTEXT_REFERENCE"


@dataclass(frozen=True)
class CheckpointIdentity:
    checkpoint_id: str
    job_id: str
    checkpoint_no: int
    created_at: str


@dataclass(frozen=True)
class CheckpointWorkflow:
    primary_state: str = ""
    logical_step: str = ""
    attempt_no: int = 0
    queue_meta: tuple = ()   # sorted (key, value) string pairs, bounded


@dataclass(frozen=True)
class CheckpointContext:
    last_context_pack_id: str = ""
    last_context_pack_hash: str = ""
    required_trusted_source_refs: tuple = ()
    selected_artifact_refs: tuple = ()   # (ref, content_hash) pairs
    latest_handoff_refs: tuple = ()      # handoff_id strings


@dataclass(frozen=True)
class CheckpointCode:
    worktree_path: str = ""
    repo_identity: str = ""
    base_commit: str = ""
    head_commit: str = ""


@dataclass(frozen=True)
class CheckpointProgress:
    completed_milestones: tuple = ()
    remaining_milestones: tuple = ()
    unresolved_questions: tuple = ()


@dataclass(frozen=True)
class CheckpointRecord:
    checkpoint_version: str
    identity: CheckpointIdentity
    workflow: CheckpointWorkflow
    context: CheckpointContext
    code: CheckpointCode
    progress: CheckpointProgress
    content_hash: str


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def _stable_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _canonical_doc(rec: CheckpointRecord) -> dict:
    """Canonical semantic document (volatile metadata excluded)."""
    return {
        "checkpoint_version": rec.checkpoint_version,
        "job_id": rec.identity.job_id,
        "checkpoint_no": rec.identity.checkpoint_no,
        "workflow": {
            "primary_state": rec.workflow.primary_state,
            "logical_step": rec.workflow.logical_step,
            "attempt_no": rec.workflow.attempt_no,
            "queue_meta": list(rec.workflow.queue_meta),
        },
        "context": {
            "last_context_pack_id": rec.context.last_context_pack_id,
            "last_context_pack_hash": rec.context.last_context_pack_hash,
            "required_trusted_source_refs": list(rec.context.required_trusted_source_refs),
            "selected_artifact_refs": list(rec.context.selected_artifact_refs),
            "latest_handoff_refs": list(rec.context.latest_handoff_refs),
        },
        "code": {
            "worktree_path": rec.code.worktree_path,
            "repo_identity": rec.code.repo_identity,
            "base_commit": rec.code.base_commit,
            "head_commit": rec.code.head_commit,
        },
        "progress": {
            "completed_milestones": list(rec.progress.completed_milestones),
            "remaining_milestones": list(rec.progress.remaining_milestones),
            "unresolved_questions": list(rec.progress.unresolved_questions),
        },
    }


def checkpoint_content_hash(rec: CheckpointRecord) -> str:
    """Deterministic semantic content hash (checkpoint_id/created_at excluded)."""
    return hashlib.sha256(
        _stable_json(_canonical_doc(rec)).encode("utf-8")
    ).hexdigest()


def make_checkpoint_id(job_id: str, checkpoint_no: int, content_hash: str) -> str:
    digest = hashlib.sha256(
        f"{job_id}\x00{checkpoint_no}\x00{content_hash}".encode("utf-8")
    ).hexdigest()
    return "ck_" + digest[:24]


# ---------------------------------------------------------------------------
# Validation / integrity
# ---------------------------------------------------------------------------


def validate_checkpoint_integrity(rec: CheckpointRecord) -> None:
    """Validate checkpoint schema + recompute the canonical hash (fail-closed).

    Enforces bounded IDs/hashes, closed field bounds, and a canonical
    recomputation of ``content_hash``.  Any drift fails closed with
    ``CONTEXT_CHECKPOINT_INVALID`` (never a silent repair).
    """
    if rec.checkpoint_version != CHECKPOINT_VERSION:
        raise CheckpointError(
            CONTEXT_CHECKPOINT_INVALID,
            f"checkpoint_version {rec.checkpoint_version!r} != {CHECKPOINT_VERSION!r}",
        )
    if not isinstance(rec.identity.job_id, str) or not rec.identity.job_id:
        raise CheckpointError(CONTEXT_CHECKPOINT_INVALID, "empty job_id")
    if len(rec.identity.job_id) > MAX_CHECKPOINT_ID_LEN:
        raise CheckpointError(CONTEXT_CHECKPOINT_INVALID, "job_id too long")
    if not isinstance(rec.identity.checkpoint_no, int) or rec.identity.checkpoint_no < 1:
        raise CheckpointError(CONTEXT_CHECKPOINT_INVALID, "checkpoint_no < 1")
    if not _is_checkpoint_id(rec.identity.checkpoint_id):
        raise CheckpointError(CONTEXT_CHECKPOINT_INVALID, "malformed checkpoint_id")
    if not _is_sha256_hex(rec.content_hash):
        raise CheckpointError(CONTEXT_CHECKPOINT_INVALID,
                              "content_hash must be sha256 hex")

    # -- workflow bounds ---------------------------------------------------
    wf = rec.workflow
    if not isinstance(wf.primary_state, str) or len(wf.primary_state) > MAX_STATE_LEN:
        raise CheckpointError(CONTEXT_CHECKPOINT_INVALID, "primary_state too long")
    if not isinstance(wf.logical_step, str) or len(wf.logical_step) > MAX_STEP_LEN:
        raise CheckpointError(CONTEXT_CHECKPOINT_INVALID, "logical_step too long")
    if not isinstance(wf.attempt_no, int) or wf.attempt_no < 0:
        raise CheckpointError(CONTEXT_CHECKPOINT_INVALID, "attempt_no < 0")
    if len(wf.queue_meta) > MAX_QUEUE_META:
        raise CheckpointError(CONTEXT_CHECKPOINT_INVALID, "queue_meta too many entries")
    for k, v in wf.queue_meta:
        if not isinstance(k, str) or not isinstance(v, str):
            raise CheckpointError(CONTEXT_CHECKPOINT_INVALID,
                                  "queue_meta entries must be str")
        if len(k) > MAX_REF_LEN or len(v) > MAX_REF_LEN:
            raise CheckpointError(CONTEXT_CHECKPOINT_INVALID,
                                  "queue_meta entry too long")

    # -- context bounds ----------------------------------------------------
    ctx = rec.context
    if len(ctx.required_trusted_source_refs) > MAX_SOURCE_REFS:
        raise CheckpointError(CONTEXT_CHECKPOINT_INVALID, "too many source refs")
    for ref in ctx.required_trusted_source_refs:
        if not isinstance(ref, str) or len(ref) > MAX_REF_LEN:
            raise CheckpointError(CONTEXT_CHECKPOINT_INVALID, "source ref too long")
    if len(ctx.selected_artifact_refs) > MAX_SOURCE_REFS:
        raise CheckpointError(CONTEXT_CHECKPOINT_INVALID, "too many artifact refs")
    for ref, h in ctx.selected_artifact_refs:
        if not isinstance(ref, str) or len(ref) > MAX_REF_LEN:
            raise CheckpointError(CONTEXT_CHECKPOINT_INVALID, "artifact ref too long")
        if h and not _is_sha256_hex(h):
            raise CheckpointError(CONTEXT_CHECKPOINT_INVALID,
                                  "artifact hash must be sha256 hex")
    if len(ctx.latest_handoff_refs) > MAX_SOURCE_REFS:
        raise CheckpointError(CONTEXT_CHECKPOINT_INVALID, "too many handoff refs")
    for hid in ctx.latest_handoff_refs:
        if not isinstance(hid, str) or len(hid) > MAX_REF_LEN:
            raise CheckpointError(CONTEXT_CHECKPOINT_INVALID, "handoff ref too long")
    if len(ctx.last_context_pack_id) > MAX_REF_LEN \
            or len(ctx.last_context_pack_hash) > MAX_REF_LEN:
        raise CheckpointError(CONTEXT_CHECKPOINT_INVALID, "pack id/hash too long")

    # -- code bounds -------------------------------------------------------
    for field, limit in (("worktree_path", MAX_CODE_PATH_LEN),
                         ("repo_identity", MAX_CODE_PATH_LEN),
                         ("base_commit", MAX_COMMIT_LEN),
                         ("head_commit", MAX_COMMIT_LEN)):
        val = getattr(rec.code, field)
        if not isinstance(val, str):
            raise CheckpointError(CONTEXT_CHECKPOINT_INVALID, f"{field} must be str")
        if len(val) > limit:
            raise CheckpointError(CONTEXT_CHECKPOINT_INVALID, f"{field} too long")

    # -- progress bounds ---------------------------------------------------
    prog = rec.progress
    if len(prog.completed_milestones) > MAX_MILESTONES \
            or len(prog.remaining_milestones) > MAX_MILESTONES:
        raise CheckpointError(CONTEXT_CHECKPOINT_INVALID, "too many milestones")
    for m in (*prog.completed_milestones, *prog.remaining_milestones):
        if not isinstance(m, str) or len(m) > MAX_MILESTONE_LEN:
            raise CheckpointError(CONTEXT_CHECKPOINT_INVALID, "milestone too long")
    if len(prog.unresolved_questions) > MAX_QUESTIONS:
        raise CheckpointError(CONTEXT_CHECKPOINT_INVALID, "too many questions")
    for q in prog.unresolved_questions:
        if not isinstance(q, str) or len(q) > MAX_QUESTION_LEN:
            raise CheckpointError(CONTEXT_CHECKPOINT_INVALID, "question too long")

    if checkpoint_content_hash(rec) != rec.content_hash:
        raise CheckpointError(
            CONTEXT_CHECKPOINT_INVALID,
            "checkpoint content_hash does not match semantic content",
        )


def checkpoint_references_valid(
    rec: CheckpointRecord, current_facts: dict,
) -> tuple:
    """Return ``(ok, reason)`` for the checkpoint's referenced state.

    ``current_facts`` is a REQUIRED trusted mapping (built by the caller from
    the Store + ``GitProvenanceProvider``).  Its keys are:

    * ``job_id``: the current job id (str) — checked against the checkpoint's
      job id (lineage/job binding, never silently resumed for another job).
    * ``worktree_path``: current canonical worktree path (str)
    * ``repo_identity``: current repo identity (str)
    * ``base_commit``: current base commit (str)
    * ``head_commit``: current git HEAD (str)
    * ``artifact_hashes``: ``{ref: sha256_hex}`` for referenced artifacts
    * ``known_handoff_ids``: a set/frozenset of known handoff ids
    * ``known_packs``: ``{context_pack_id: content_hash}`` (BOTH id + hash)

    Missing ``current_facts`` (``None``) and any missing comparison fact for a
    field the checkpoint declares fail closed — never a silent substitution.
    A mismatch returns ``(False, reason)`` with a bounded reason code.
    """
    if not isinstance(current_facts, dict):
        return (False, CONTEXT_CHECKPOINT_INVALID)

    # (a) job / lineage: the checkpoint must belong to the current job.
    current_job = current_facts.get("job_id")
    if not isinstance(current_job, str) or not current_job:
        return (False, CONTEXT_CHECKPOINT_INVALID)
    if rec.identity.job_id != current_job:
        return (False, CONTEXT_CHECKPOINT_INVALID)

    # (b) worktree / repo identity / base / head — fail-closed when the
    # checkpoint declares the field but the trusted comparison fact is missing
    # or mismatched.
    code = rec.code
    for field, key in (
        ("worktree_path", "worktree_path"),
        ("repo_identity", "repo_identity"),
        ("base_commit", "base_commit"),
        ("head_commit", "head_commit"),
    ):
        declared = getattr(code, field)
        if not declared:
            continue
        actual = current_facts.get(key)
        if not isinstance(actual, str) or not actual:
            return (False, STALE_CONTEXT_REFERENCE)
        if declared != actual:
            return (False, STALE_CONTEXT_REFERENCE)

    # (d) Artifact existence + hash.
    artifact_hashes = current_facts.get("artifact_hashes") or {}
    for ref, expected_hash in rec.context.selected_artifact_refs:
        actual = artifact_hashes.get(ref)
        if actual is None:
            return (False, STALE_CONTEXT_REFERENCE)
        if expected_hash and actual != expected_hash:
            return (False, STALE_CONTEXT_REFERENCE)

    # (e) Handoff refs must be known.
    known_handoffs = current_facts.get("known_handoff_ids") or frozenset()
    for hid in rec.context.latest_handoff_refs:
        if hid not in known_handoffs:
            return (False, STALE_CONTEXT_REFERENCE)

    # (c) Pack ref must exist AND its content hash must match (both!).
    if rec.context.last_context_pack_id:
        known_packs = current_facts.get("known_packs") or {}
        pack_hash = known_packs.get(rec.context.last_context_pack_id)
        if pack_hash is None:
            return (False, STALE_CONTEXT_REFERENCE)
        if rec.context.last_context_pack_hash and \
                rec.context.last_context_pack_hash != pack_hash:
            return (False, STALE_CONTEXT_REFERENCE)

    return (True, "")


def build_checkpoint_record(
    *,
    job_id: str,
    checkpoint_no: int,
    created_at: str = "",
    workflow: Optional[CheckpointWorkflow] = None,
    context: Optional[CheckpointContext] = None,
    code: Optional[CheckpointCode] = None,
    progress: Optional[CheckpointProgress] = None,
) -> CheckpointRecord:
    """Build, hash and validate a CheckpointRecord (deterministic).

    ``content_hash`` and ``checkpoint_id`` are derived; ``created_at`` and
    ``checkpoint_id`` are volatile instance metadata (excluded from the hash).
    """
    workflow = workflow or CheckpointWorkflow()
    context = context or CheckpointContext()
    code = code or CheckpointCode()
    progress = progress or CheckpointProgress()
    rec = CheckpointRecord(
        checkpoint_version=CHECKPOINT_VERSION,
        identity=CheckpointIdentity(checkpoint_id="", job_id=job_id,
                                    checkpoint_no=checkpoint_no,
                                    created_at=created_at),
        workflow=workflow, context=context, code=code, progress=progress,
        content_hash="",
    )
    content_h = checkpoint_content_hash(rec)
    checkpoint_id = make_checkpoint_id(job_id, checkpoint_no, content_h)
    rec = CheckpointRecord(
        checkpoint_version=CHECKPOINT_VERSION,
        identity=CheckpointIdentity(checkpoint_id=checkpoint_id, job_id=job_id,
                                    checkpoint_no=checkpoint_no,
                                    created_at=created_at),
        workflow=workflow, context=context, code=code, progress=progress,
        content_hash=content_h,
    )
    validate_checkpoint_integrity(rec)
    return rec


def _with_checkpoint_no(rec: CheckpointRecord, checkpoint_no: int) -> CheckpointRecord:
    """Rebuild a record with an authoritative ``checkpoint_no`` (new id/hash).

    The store is the single authority for the sequential number; a caller's
    ``checkpoint_no`` is never trusted.  The rebuilt record derives a fresh
    ``checkpoint_id`` and ``content_hash`` from the authoritative number.
    """
    return build_checkpoint_record(
        job_id=rec.identity.job_id,
        checkpoint_no=checkpoint_no,
        created_at=rec.identity.created_at,
        workflow=rec.workflow,
        context=rec.context,
        code=rec.code,
        progress=rec.progress,
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class CheckpointStore:
    """Fenced, immutable checkpoint persistence over the shared Store."""

    def __init__(self, store, *, git_provenance_provider=None,
                 clock: Optional[Callable[[], str]] = None):
        self._store = store
        self._git = git_provenance_provider
        self._clock = clock or (lambda: store.now_iso())

    # -- create -----------------------------------------------------------

    def create_checkpoint(
        self,
        rec: CheckpointRecord,
        *,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> CheckpointRecord:
        """Persist a new checkpoint (INSERT-only, fenced + sequential).

        ``owner_instance_id`` and ``lease_epoch`` are REQUIRED: only the
        current lease holder may write the next checkpoint (a missing/stale/
        expired holder raises :class:`LeaseFencedError`).  The checkpoint
        number is ALWAYS derived as ``MAX(checkpoint_no)+1`` under the same
        ``BEGIN IMMEDIATE`` transaction — the caller's
        ``rec.identity.checkpoint_no`` is ignored (never trusted) and the
        record is rebuilt with the authoritative number.  ``_clear_latest`` +
        ``_insert`` are atomic (no gap, no overwrite, no caller-overridden no).
        """
        job_id = rec.identity.job_id
        with self._store._transaction():
            self._fence(job_id, owner_instance_id, lease_epoch)
            no = self.next_checkpoint_no(job_id)
            rec = _with_checkpoint_no(rec, no)
            validate_checkpoint_integrity(rec)
            self._store._clear_latest_checkpoint(job_id)
            self._store._insert_checkpoint(
                checkpoint_id=rec.identity.checkpoint_id,
                record_version=rec.checkpoint_version,
                job_id=job_id,
                checkpoint_no=no,
                workflow_json=_stable_json(_workflow_json(rec)),
                context_json=_stable_json(_context_json(rec)),
                code_json=_stable_json(_code_json(rec)),
                progress_json=_stable_json(_progress_json(rec)),
                content_hash=rec.content_hash,
                created_at=rec.identity.created_at,
                latest=1,
            )
        return rec

    def _fence(self, job_id: str, owner_instance_id: str,
               lease_epoch: int) -> None:
        job = self._store.get_supervisor_job(job_id)
        if job is None:
            raise NotFound(f"job {job_id!r} not found")
        holder = job.get("owner_instance_id")
        epoch = job.get("lease_epoch")
        if holder is None:
            raise LeaseFencedError(
                f"checkpoint write refused: job {job_id!r} is unleased")
        if holder != owner_instance_id or epoch != lease_epoch:
            raise LeaseFencedError(
                f"checkpoint write refused: holder={holder!r} epoch={epoch!r} "
                f"vs caller owner={owner_instance_id!r} epoch={lease_epoch!r}"
            )
        expires = job.get("lease_expires_at")
        if expires and expires <= self._clock():
            raise LeaseFencedError(
                f"checkpoint write refused: lease expired at {expires!r}"
            )

    # -- read -------------------------------------------------------------

    def latest_checkpoint(self, job_id: str) -> Optional[CheckpointRecord]:
        row = self._store.get_latest_checkpoint(job_id)
        if row is None:
            return None
        rec = _checkpoint_from_row(row)
        validate_checkpoint_integrity(rec)
        return rec

    def get_checkpoint(self, checkpoint_id: str) -> Optional[CheckpointRecord]:
        row = self._store.get_checkpoint(checkpoint_id)
        if row is None:
            return None
        rec = _checkpoint_from_row(row)
        validate_checkpoint_integrity(rec)
        return rec

    def list_checkpoints(self, job_id: str) -> list:
        rows = self._store.list_checkpoints(job_id)
        out = []
        for row in rows:
            rec = _checkpoint_from_row(row)
            validate_checkpoint_integrity(rec)
            out.append(rec)
        return out

    def next_checkpoint_no(self, job_id: str) -> int:
        rows = self._store.list_checkpoints(job_id)
        return max((r["checkpoint_no"] for r in rows), default=0) + 1

    def current_facts(self, job_id: str) -> dict:
        """Build the trusted comparison facts for a checkpoint (Store + git).

        Assembled from the supervisor job row (worktree/repo/base/head), the live
        ``GitProvenanceProvider`` (repo identity + HEAD) and the Store (known
        handoff ids + known pack id → content_hash).  Missing facts are left as
        empty defaults so :func:`checkpoint_references_valid` fails closed on
        them — never guessed.
        """
        job = self._store.get_supervisor_job(job_id) or {}
        worktree_path = job.get("canonical_worktree_path") or ""
        repo_identity = job.get("repo_identity") or ""
        base_commit = job.get("base_commit") or ""
        head_commit = job.get("current_head") or job.get("expected_head") or ""

        if self._git is not None and worktree_path:
            try:
                repo_identity = self._git.repo_identity(worktree_path) or repo_identity
                head_commit = self._git.head(worktree_path) or head_commit
            except Exception:
                pass

        known_handoff_ids = frozenset(
            r["handoff_id"] for r in self._store.list_handoffs_v2(job_id)
        )
        known_packs: dict = {}
        for row in self._store.list_context_packs(job_id):
            known_packs[row["context_pack_id"]] = row["content_hash"]

        return {
            "job_id": job_id,
            "worktree_path": worktree_path,
            "repo_identity": repo_identity,
            "base_commit": base_commit,
            "head_commit": head_commit,
            "artifact_hashes": {},
            "known_handoff_ids": known_handoff_ids,
            "known_packs": known_packs,
        }


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


def checkpoint_validated_inputs(
    rec: CheckpointRecord, job_id: str, current_facts: dict,
) -> tuple:
    """Validate a checkpoint and return ``(artifacts, prior_results, extra_facts)``.

    Raises :class:`CheckpointError` (fail-closed) on integrity/lineage/stale
    violations.  The objective/acceptance/constraints are NEVER taken from the
    checkpoint — the checkpoint only supplies bounded artifact/handoff/source
    refs.
    """
    validate_checkpoint_integrity(rec)
    if rec.identity.job_id != job_id:
        raise CheckpointError(CONTEXT_CHECKPOINT_INVALID, "checkpoint job_id mismatch")
    ok, reason = checkpoint_references_valid(rec, current_facts)
    if not ok:
        raise CheckpointError(reason, "checkpoint references are stale/invalid")

    artifacts = [ArtifactRef(ref=ref, content_hash=content_hash)
                 for ref, content_hash in rec.context.selected_artifact_refs]
    prior_results = [ResultInput(content=f"handoff {hid}", source_ref=hid)
                     for hid in rec.context.latest_handoff_refs]
    extra_facts = [FactInput(content=ref, source_ref=ref)
                   for ref in rec.context.required_trusted_source_refs]
    return artifacts, prior_results, extra_facts


def resume_context(
    rec: CheckpointRecord,
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
    current_facts: dict,
    capability: str = CapabilityTier.FLASH.value,
    expansion_reason: Optional[str] = None,
    now_iso: str = "",
) -> ContextPack:
    """Build a NEW Context Pack from a valid checkpoint (D1 builder only).

    Fails closed with :class:`CheckpointError` if the checkpoint integrity or
    its references are stale.  ``current_facts`` is REQUIRED (trusted Store +
    git provenance) — the objective/acceptance/constraints always come from the
    trusted caller (never from the checkpoint); the checkpoint only supplies
    bounded artifact/handoff/source refs.
    """
    artifacts, prior_results, extra_facts = checkpoint_validated_inputs(
        rec, job_id, current_facts)
    all_facts: list = list(facts) + extra_facts

    return context_builder.build(
        job_id=job_id,
        dispatch_id=dispatch_id,
        role=role,
        objective=objective,
        acceptance_criteria=acceptance_criteria,
        constraints=constraints,
        policy_references=policy_references,
        facts=tuple(all_facts),
        artifacts=tuple(artifacts),
        prior_results=tuple(prior_results),
        capability=capability,
        expansion_reason=expansion_reason,
        now_iso=now_iso,
    )


# ---------------------------------------------------------------------------
# (De)serialization
# ---------------------------------------------------------------------------


def _workflow_json(rec: CheckpointRecord) -> dict:
    return {
        "primary_state": rec.workflow.primary_state,
        "logical_step": rec.workflow.logical_step,
        "attempt_no": rec.workflow.attempt_no,
        "queue_meta": list(rec.workflow.queue_meta),
    }


def _context_json(rec: CheckpointRecord) -> dict:
    return {
        "last_context_pack_id": rec.context.last_context_pack_id,
        "last_context_pack_hash": rec.context.last_context_pack_hash,
        "required_trusted_source_refs": list(rec.context.required_trusted_source_refs),
        "selected_artifact_refs": [list(p) for p in rec.context.selected_artifact_refs],
        "latest_handoff_refs": list(rec.context.latest_handoff_refs),
    }


def _code_json(rec: CheckpointRecord) -> dict:
    return {
        "worktree_path": rec.code.worktree_path,
        "repo_identity": rec.code.repo_identity,
        "base_commit": rec.code.base_commit,
        "head_commit": rec.code.head_commit,
    }


def _progress_json(rec: CheckpointRecord) -> dict:
    return {
        "completed_milestones": list(rec.progress.completed_milestones),
        "remaining_milestones": list(rec.progress.remaining_milestones),
        "unresolved_questions": list(rec.progress.unresolved_questions),
    }


def _checkpoint_from_row(row: dict) -> CheckpointRecord:
    def _load(col, default):
        try:
            return json.loads(row.get(col) or "{}")
        except Exception:
            return default

    record_version = row.get("record_version")
    if record_version != CHECKPOINT_VERSION:
        raise CheckpointError(
            CONTEXT_CHECKPOINT_INVALID,
            f"persisted record_version {record_version!r} != "
            f"{CHECKPOINT_VERSION!r}",
        )

    workflow = _load("workflow_json", {})
    context = _load("context_json", {})
    code = _load("code_json", {})
    progress = _load("progress_json", {})
    return CheckpointRecord(
        checkpoint_version=record_version,
        identity=CheckpointIdentity(
            checkpoint_id=row["checkpoint_id"],
            job_id=row["job_id"],
            checkpoint_no=row["checkpoint_no"],
            created_at=row["created_at"],
        ),
        workflow=CheckpointWorkflow(
            primary_state=workflow.get("primary_state", ""),
            logical_step=workflow.get("logical_step", ""),
            attempt_no=workflow.get("attempt_no", 0),
            queue_meta=tuple(workflow.get("queue_meta", [])),
        ),
        context=CheckpointContext(
            last_context_pack_id=context.get("last_context_pack_id", ""),
            last_context_pack_hash=context.get("last_context_pack_hash", ""),
            required_trusted_source_refs=tuple(context.get("required_trusted_source_refs", [])),
            selected_artifact_refs=tuple(
                tuple(p) for p in context.get("selected_artifact_refs", [])
            ),
            latest_handoff_refs=tuple(context.get("latest_handoff_refs", [])),
        ),
        code=CheckpointCode(
            worktree_path=code.get("worktree_path", ""),
            repo_identity=code.get("repo_identity", ""),
            base_commit=code.get("base_commit", ""),
            head_commit=code.get("head_commit", ""),
        ),
        progress=CheckpointProgress(
            completed_milestones=tuple(progress.get("completed_milestones", [])),
            remaining_milestones=tuple(progress.get("remaining_milestones", [])),
            unresolved_questions=tuple(progress.get("unresolved_questions", [])),
        ),
        content_hash=row["content_hash"],
    )
