"""Phase I1 — deterministic concurrency policy (decision ONLY, no enforcement).

This module is the pure, deterministic authority for the *structural*
concurrency decision (ARGENT ARCHITECTURE V1 FINAL §14, Phase I1 brief §1–§20):
**may a candidate job run in parallel with the currently active jobs?**

It is deliberately narrow: it decides WORKTREE / REPOSITORY / MUTATION-FOOTPRINT
/ DEPENDENCY / ACTION-CLASS conflicts over the trusted job set.  It does **NOT**
perform host-resource admission (memory/swap/disk/load) — that remains the
Phase C :class:`~argent_core.resource_governor.ResourceGovernor` authority,
which I1 extends with aggregate reservations.  It also does **NOT** re-implement
the class-count slot limits (`max_writers_global` / `max_light` / HEAVY-alone /
EXCLUSIVE-alone): those are already enforced by the Resource Governor and emit
``CONCURRENCY_LIMIT``.  Keeping the two authorities disjoint avoids two
competing systems deciding the same slots.

Key properties:

* :class:`ConcurrencyVerdict` — the exact, bounded verdict
  (``ALLOW_PARALLEL`` / ``SERIALIZE`` / ``DEFER`` / ``BLOCK``).
* :class:`ConcurrencyReasonCode` — the exact, bounded reason codes.
* :class:`MutationFootprint` — the bounded trusted per-job mutation metadata.
* :class:`JobFacts` — the candidate/active job evidence snapshot.
* :class:`ConcurrencyDecision` — frozen (verdict + reason + detail).
* :func:`decide` — the pure decision function.

**No I/O, no shell, no host reads, no LLM.**  Path-prefix overlap detection is a
pure lexical operation over already-normalised relative path roots (never
resolves the filesystem).  Agent prose is never an input; only trusted,
controller-originated metadata and persisted store facts reach this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ConcurrencyVerdict(str, Enum):
    """The exact, bounded concurrency verdict."""

    ALLOW_PARALLEL = "ALLOW_PARALLEL"
    SERIALIZE = "SERIALIZE"
    DEFER = "DEFER"
    BLOCK = "BLOCK"


class ConcurrencyReasonCode(str, Enum):
    """Bounded, exact reason codes for the structural concurrency decision.

    Structural codes are produced HERE.  Class-budget codes (writer slot /
    heavy/exclusive/light caps) and host-reserve are owned by Phase C and are
    listed here only as the documented contract for callers; they are emitted
    by :class:`~argent_core.resource_governor.ResourceGovernor` as
    ``CONCURRENCY_LIMIT`` / ``INSUFFICIENT_MEMORY_RESERVE``.
    """

    # -- produced by this module (structural) ------------------------------
    READONLY_SAFE = "READONLY_SAFE"
    DISTINCT_WORKTREE = "DISTINCT_WORKTREE"
    DISTINCT_REPO = "DISTINCT_REPO"
    WORKTREE_CONFLICT = "WORKTREE_CONFLICT"
    REPO_OVERLAP = "REPO_OVERLAP"
    UNKNOWN_OVERLAP = "UNKNOWN_OVERLAP"
    DEPENDENCY_NOT_MET = "DEPENDENCY_NOT_MET"
    DEPENDENCY_UNKNOWN = "DEPENDENCY_UNKNOWN"
    ACTION_GLOBAL_SERIALIZE = "ACTION_GLOBAL_SERIALIZE"
    # -- contract-only (owned by Phase C Resource Governor) ----------------
    WRITER_SLOT_FULL = "WRITER_SLOT_FULL"
    HEAVY_ALONE = "HEAVY_ALONE"
    EXCLUSIVE_ALONE = "EXCLUSIVE_ALONE"
    LIGHT_SLOT_FULL = "LIGHT_SLOT_FULL"
    RESOURCE_RESERVE = "RESOURCE_RESERVE"


# ---------------------------------------------------------------------------
# Conservative initial limits (§23 / Phase C §14)
# ---------------------------------------------------------------------------
#
# Deliberately conservative for THIS host (8 CPU / ~7.7 GiB RAM).  These are
# module constants (later configurable — but NO config system is built now).
# They mirror the existing ``ResourcePolicy`` concurrency defaults so the
# structural policy and the resource governor never disagree on slots.
# Derivation in docs/PHASE_I1_NOTES.md §3.

#: At most one writer (MEDIUM-class or heavier) active at a time.
MAX_WRITERS_CONCURRENT: int = 1
#: At most this many concurrent read-only/LIGHT jobs.
MAX_READONLY_LIGHT_CONCURRENT: int = 2
#: EXCLUSIVE jobs always run alone (blocked by / blocks any active job).
EXCLUSIVE_ALONE: bool = True
#: HEAVY is gated by the single-writer budget (it IS a writer class), NOT by a
#: dedicated "alone" slot — mirroring the existing validated Phase C
#: concurrency matrix (test_full_concurrency_matrix allows HEAVY + LIGHT).
#: On THIS host the aggregate-memory admission is the binding constraint (a
#: HEAVY 4 GiB ceiling + any LIGHT work already exceeds the host reserve).
#: ``HEAVY_ALONE=False`` records that reality; a later config could tighten it
#: without any policy change.
HEAVY_ALONE: bool = False

#: Terminal states of a prerequisite that satisfy ``depends_on``.
DEPENDENCY_SATISFIED_TERMINALS: Tuple[str, ...] = ("DONE",)

#: Roles that are structurally read-only w.r.t. the write broker.
READONLY_ROLES: Tuple[str, ...] = ("lead", "analyst", "reviewer")

#: Roles that are WRITE-CAPABLE w.r.t. the write broker / writer binding, i.e.
#: they reach broker writes + writer binding regardless of resource class.
#: Mirrors ``supervisor._is_write_role`` (Role.IMPLEMENTER / Role.QA).  A
#: write-capable role is treated as a writer for structural overlap even when
#: its resource class is LIGHT (see :attr:`JobFacts.is_writer`).
WRITE_ROLES: Tuple[str, ...] = ("implementer", "qa")

#: Action classes that require global / repository-global serialization.
ACTION_GLOBAL = "GLOBAL"
ACTION_REPO_GLOBAL = "REPO_GLOBAL"

#: Sentinel used by callers to mark a ``depends_on`` prerequisite as missing.
DEPENDENCY_MISSING = "MISSING"

#: Bounded field lengths (mirror worktree.py bounds).
MAX_PATH_ROOTS = 64
MAX_MODULES = 64
MAX_ROOT_LEN = 512
MAX_MODULE_LEN = 256


@dataclass(frozen=True)
class MutationFootprint:
    """Bounded trusted per-job mutation metadata (Phase I1 §9/§3).

    Originates ONLY from trusted controller/task analysis (never agent prose).
    All fields are Optional to represent an unbounded/unknown footprint, which
    the policy treats conservatively (UNKNOWN overlap ⇒ SERIALIZE).
    """

    repo_identity: Optional[str] = None
    canonical_worktree_path: Optional[str] = None
    branch_identity: Optional[str] = None
    #: Relative path prefixes the job is expected to mutate (bounded).
    path_roots: Tuple[str, ...] = ()
    #: Optional subsystem/module names the job touches (bounded).
    modules: Tuple[str, ...] = ()
    #: External action class (e.g. AUTONOMOUS / OWNER_APPROVAL_REQUIRED).
    external_action_class: Optional[str] = None
    #: Integration target (branch/PR/release) shared by jobs that must
    #: serialize (e.g. both targeting ``main``).
    integration_target: Optional[str] = None


@dataclass(frozen=True)
class JobFacts:
    """Concurrency-relevant evidence for a candidate or an active job."""

    job_id: str
    role: Optional[str] = None
    resource_class: str = "LIGHT"
    footprint: MutationFootprint = field(default_factory=MutationFootprint)
    #: Prerequisite job id (single ``depends_on``, no DAG).
    depends_on: Optional[str] = None
    #: Resolved terminal state of the prerequisite (caller-resolved), or
    #: ``DEPENDENCY_MISSING`` when the referenced job does not exist.
    dependency_terminal: Optional[str] = None
    #: Action class for repo-global / global serialization (None = normal).
    action_class: Optional[str] = None

    @property
    def is_writer(self) -> bool:
        """True when this job counts as a writer for structural overlap.

        A job is a writer when EITHER its role is write-capable
        (:data:`WRITE_ROLES` — implementer/qa reach broker writes + writer
        binding regardless of resource class) OR its resource class is a
        writer class (MEDIUM/HEAVY/EXCLUSIVE).  A LIGHT implementer/qa job is
        therefore still treated as a writer for same-worktree / footprint-
        overlap / UNKNOWN-overlap purposes.
        """
        if self.role in WRITE_ROLES:
            return True
        from .resource_policy import ResourceClass, ResourcePolicy

        try:
            return ResourcePolicy().is_writer_class(ResourceClass(self.resource_class))
        except ValueError:
            return False

    @property
    def is_readonly(self) -> bool:
        """True when the job is structurally read-only (non-writer role)."""
        if self.is_writer:
            return False
        return self.role in READONLY_ROLES


@dataclass(frozen=True)
class ConcurrencyDecision:
    """A single structural concurrency decision (bounded, no secrets)."""

    verdict: str  # ConcurrencyVerdict value
    reason_code: str  # ConcurrencyReasonCode value
    detail: Optional[str] = None
    #: The active job that conflicts (for observability / tests); None when N/A.
    conflict_job_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Pure path helpers (lexical only, no filesystem access)
# ---------------------------------------------------------------------------


def _norm_rel(path: str) -> str:
    """Pure lexical normalisation of a relative path (no filesystem access).

    Converts backslashes to slashes, collapses duplicate/empty segments, and
    lexically resolves ``.`` and ``..``.  Returns ``""`` for empty/invalid input.
    """
    if not isinstance(path, str):
        return ""
    p = path.replace("\\", "/").strip()
    parts: list = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts).rstrip("/")


def _validate_rel_root(path: str) -> str:
    """Validate + normalize ONE trusted relative mutation path root (fail-closed).

    Rejects (raises :class:`ValueError`) any input that is not a safe,
    repository-relative path root: non-strings, empty/whitespace-only, absolute
    POSIX paths (leading ``/``), absolute Windows paths (leading ``\\`` or a
    drive letter like ``C:``), or anything that lexically escapes the
    repository root (a leading ``..`` segment or a ``..`` that pops above the
    root).  Only such safe relative roots may be persisted, so a malformed
    footprint root can never be silently normalized into an over-broad or
    escaped scope.
    """
    if not isinstance(path, str):
        raise ValueError("path root must be a string")
    raw = path.strip()
    if not raw:
        raise ValueError("path root must be a non-empty string")
    # Reject absolute POSIX / Windows paths and drive letters up front.
    if raw.startswith("/") or raw.startswith("\\"):
        raise ValueError(f"absolute path root {path!r} is not a relative root")
    if len(raw) >= 2 and raw[0].isalpha() and raw[1] == ":":
        raise ValueError(f"drive-letter path root {path!r} is not a relative root")
    p = raw.replace("\\", "/")
    depth = 0
    out: list = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if depth == 0:
                raise ValueError(
                    f"path root {path!r} escapes the repository root")
            out.pop()
            depth -= 1
            continue
        out.append(seg)
        depth += 1
    norm = "/".join(out)
    if not norm:
        raise ValueError(f"path root {path!r} normalises to empty")
    if norm == ".." or norm.startswith("../"):
        raise ValueError(f"path root {path!r} escapes the repository root")
    return norm


def _is_prefix(prefix: str, path: str) -> bool:
    """True when normalised ``prefix`` is an ancestor-or-equal of ``path``.

    An empty prefix matches nothing (avoids over-matching a missing root).
    """
    if not prefix:
        return False
    if prefix == path:
        return True
    return path.startswith(prefix + "/")


def path_roots_conflict(roots_a, roots_b) -> bool:
    """True when any normalised root in ``roots_a`` overlaps one in ``roots_b``.

    Overlap = one root is a lexical ancestor-or-equal of the other
    (path-prefix intersection, Phase I1 §3).
    """
    ra = {_norm_rel(r) for r in (roots_a or ())}
    rb = {_norm_rel(r) for r in (roots_b or ())}
    ra.discard("")
    rb.discard("")
    for a in ra:
        for b in rb:
            if _is_prefix(a, b) or _is_prefix(b, a):
                return True
    return False


# ---------------------------------------------------------------------------
# Footprint overlap classification
# ---------------------------------------------------------------------------

#: Footprint overlap outcomes (pure string enum).
OVERLAP = "OVERLAP"
DISJOINT = "DISJOINT"
UNKNOWN = "UNKNOWN"


def footprint_overlap(a: MutationFootprint, b: MutationFootprint) -> str:
    """Classify the mutation overlap of two trusted footprints (pure).

    * different (non-null) repo identities → ``DISJOINT``;
    * missing repo identity on either side → ``UNKNOWN`` (fail closed);
    * same repo + same branch identity → ``OVERLAP``;
    * same repo + shared integration target → ``OVERLAP``;
    * same repo + path-root prefix intersection → ``OVERLAP``;
    * same repo, different branches, empty/unknown roots → ``UNKNOWN``;
    * otherwise ``DISJOINT``.
    """
    if not a.repo_identity or not b.repo_identity:
        return UNKNOWN
    if a.repo_identity != b.repo_identity:
        return DISJOINT
    # Same repository: branch / integration / path-root overlap decides.
    if a.branch_identity and b.branch_identity \
            and a.branch_identity == b.branch_identity:
        return OVERLAP
    if a.integration_target and b.integration_target \
            and a.integration_target == b.integration_target:
        return OVERLAP
    if not a.path_roots or not b.path_roots:
        return UNKNOWN
    if path_roots_conflict(a.path_roots, b.path_roots):
        return OVERLAP
    return DISJOINT


# ---------------------------------------------------------------------------
# Decision function
# ---------------------------------------------------------------------------


def _decision(verdict: ConcurrencyVerdict, reason: ConcurrencyReasonCode,
              detail: Optional[str] = None,
              conflict_job_id: Optional[str] = None) -> ConcurrencyDecision:
    return ConcurrencyDecision(
        verdict=verdict.value, reason_code=reason.value,
        detail=detail, conflict_job_id=conflict_job_id,
    )


def decide(
    candidate: JobFacts,
    active_jobs: Sequence[JobFacts],
) -> ConcurrencyDecision:
    """Decide whether ``candidate`` may run in parallel with ``active_jobs``.

    Pure and deterministic.  Order matters (first match wins):

    1. dependency (prerequisite not satisfied / missing);
    2. action-class global/repo-global serialization (symmetric: candidate-side
       and active-side);
    3. structural worktree/repo/footprint overlap (write-capable jobs only);
    4. read-only or disjoint → ALLOW_PARALLEL.

    Class-budget slots and host reserve are NOT decided here (Phase C owns
    them); this function returns ALLOW_PARALLEL for structurally-eligible
    candidates so the caller can then run the Resource Governor.
    """
    # 1. Dependency gate (resolved by the caller from the store).
    if candidate.depends_on is not None:
        dep_state = candidate.dependency_terminal
        if dep_state == DEPENDENCY_MISSING:
            return _decision(ConcurrencyVerdict.BLOCK,
                             ConcurrencyReasonCode.DEPENDENCY_UNKNOWN,
                             f"prerequisite {candidate.depends_on!r} missing")
        if dep_state not in DEPENDENCY_SATISFIED_TERMINALS:
            return _decision(ConcurrencyVerdict.DEFER,
                             ConcurrencyReasonCode.DEPENDENCY_NOT_MET,
                             f"prerequisite {candidate.depends_on!r} terminal "
                             f"state {dep_state!r} is not satisfied")

    # 2. Action-class global / repo-global serialization (SYMMETRIC).
    #    Candidate-side AND active-side checks both run before any structural
    #    overlap decision (step 3).
    # (a) candidate-side: a global/repo-global candidate serializes against the
    #     relevant active jobs.
    if candidate.action_class == ACTION_GLOBAL:
        if active_jobs:
            return _decision(ConcurrencyVerdict.SERIALIZE,
                             ConcurrencyReasonCode.ACTION_GLOBAL_SERIALIZE,
                             "global action while jobs active",
                             conflict_job_id=active_jobs[0].job_id)
    elif candidate.action_class == ACTION_REPO_GLOBAL:
        for active in active_jobs:
            if active.footprint.repo_identity is not None \
                    and active.footprint.repo_identity == candidate.footprint.repo_identity:
                return _decision(ConcurrencyVerdict.SERIALIZE,
                                 ConcurrencyReasonCode.ACTION_GLOBAL_SERIALIZE,
                                 "repo-global action while same-repo job active",
                                 conflict_job_id=active.job_id)
    # (b) active-side: an ordinary candidate serializes against ANY active
    #     ACTION_GLOBAL job, and against an active ACTION_REPO_GLOBAL job in
    #     the same repository.
    for active in active_jobs:
        if active.action_class == ACTION_GLOBAL:
            return _decision(ConcurrencyVerdict.SERIALIZE,
                             ConcurrencyReasonCode.ACTION_GLOBAL_SERIALIZE,
                             "global action active",
                             conflict_job_id=active.job_id)
        if active.action_class == ACTION_REPO_GLOBAL \
                and active.footprint.repo_identity is not None \
                and active.footprint.repo_identity == candidate.footprint.repo_identity:
            return _decision(ConcurrencyVerdict.SERIALIZE,
                             ConcurrencyReasonCode.ACTION_GLOBAL_SERIALIZE,
                             "repo-global action active in same repo",
                             conflict_job_id=active.job_id)

    # 3. Structural overlap (write-capable jobs only; read-only jobs never
    #    mutate).  A write-capable ROLE (implementer/qa) counts as a writer
    #    even at resource class LIGHT.
    if candidate.is_writer:
        for active in active_jobs:
            if not active.is_writer:
                continue
            # 3a. same canonical worktree → conflict.
            cw = candidate.footprint.canonical_worktree_path
            aw = active.footprint.canonical_worktree_path
            if cw and aw and cw == aw:
                return _decision(ConcurrencyVerdict.SERIALIZE,
                                 ConcurrencyReasonCode.WORKTREE_CONFLICT,
                                 f"same worktree {cw!r}",
                                 conflict_job_id=active.job_id)
            # 3b. footprint overlap classification.
            overlap = footprint_overlap(candidate.footprint, active.footprint)
            if overlap == OVERLAP:
                return _decision(ConcurrencyVerdict.SERIALIZE,
                                 ConcurrencyReasonCode.REPO_OVERLAP,
                                 "overlapping mutation footprints",
                                 conflict_job_id=active.job_id)
            if overlap == UNKNOWN:
                return _decision(ConcurrencyVerdict.SERIALIZE,
                                 ConcurrencyReasonCode.UNKNOWN_OVERLAP,
                                 "cannot prove disjoint footprints",
                                 conflict_job_id=active.job_id)
            # DISJOINT: keep checking remaining active writers.
        # All active writers are on distinct worktrees with disjoint footprints.
        # Emit DISTINCT_REPO when at least one active writer lives in a DIFFERENT
        # repository (the stronger isolation guarantee), else DISTINCT_WORKTREE.
        if any(
            candidate.footprint.repo_identity is not None
            and aw.footprint.repo_identity is not None
            and candidate.footprint.repo_identity != aw.footprint.repo_identity
            for aw in active_jobs if aw.is_writer
        ):
            return _decision(ConcurrencyVerdict.ALLOW_PARALLEL,
                             ConcurrencyReasonCode.DISTINCT_REPO)
        return _decision(ConcurrencyVerdict.ALLOW_PARALLEL,
                         ConcurrencyReasonCode.DISTINCT_WORKTREE)

    # 4. Read-only / non-writer: no structural conflict.
    return _decision(ConcurrencyVerdict.ALLOW_PARALLEL,
                     ConcurrencyReasonCode.READONLY_SAFE)


# ---------------------------------------------------------------------------
# Bounded footprint (de)serialization (used by the store layer)
# ---------------------------------------------------------------------------


def serialize_footprint_paths(roots: Sequence[str]) -> Optional[str]:
    """Serialize a bounded list of path roots to a JSON array (or None).

    Raises ``ValueError`` on non-string entries / over-bounded input.  Used by
    the trusted store setter; never reads agent output.
    """
    import json

    if roots is None:
        return None
    if not isinstance(roots, (list, tuple)):
        raise ValueError("path roots must be a list/tuple of strings")
    if len(roots) > MAX_PATH_ROOTS:
        raise ValueError(f"path roots exceed MAX_PATH_ROOTS ({MAX_PATH_ROOTS})")
    out = []
    for r in roots:
        if not isinstance(r, str) or not r.strip():
            raise ValueError("path root entries must be non-empty strings")
        norm = _validate_rel_root(r)
        if len(norm) > MAX_ROOT_LEN:
            raise ValueError(f"path root exceeds {MAX_ROOT_LEN} chars")
        out.append(norm)
    return json.dumps(out, sort_keys=True)


def serialize_footprint_modules(modules: Sequence[str]) -> Optional[str]:
    """Serialize a bounded list of modules to a JSON array (or None)."""
    import json

    if modules is None:
        return None
    if not isinstance(modules, (list, tuple)):
        raise ValueError("modules must be a list/tuple of strings")
    if len(modules) > MAX_MODULES:
        raise ValueError(f"modules exceed MAX_MODULES ({MAX_MODULES})")
    out = []
    for m in modules:
        if not isinstance(m, str) or not m.strip():
            raise ValueError("module entries must be non-empty strings")
        if len(m) > MAX_MODULE_LEN:
            raise ValueError(f"module exceeds {MAX_MODULE_LEN} chars")
        out.append(m.strip())
    return json.dumps(out, sort_keys=True)


def action_lock_name(action_class: str, *, repo_identity: Optional[str] = None,
                     name: str = "action") -> str:
    """Derive a deterministic named action-lock key (Phase I1 §17).

    * ``ACTION_GLOBAL`` → ``"global:<name>"`` (one machine-wide lock);
    * ``ACTION_REPO_GLOBAL`` → ``"repo:<repo_identity>:<name>"`` (one per repo);
    * anything else → raises ``ValueError`` (only the two scopes exist).

    Pure and deterministic; used by I2/I3 to acquire the serialization boundary
    via :meth:`~argent_core.store.Store.try_acquire_action_lock`.
    """
    if action_class == ACTION_GLOBAL:
        return f"global:{name}"
    if action_class == ACTION_REPO_GLOBAL:
        if not repo_identity:
            raise ValueError(
                "REPO_GLOBAL action lock requires a repo_identity")
        return f"repo:{repo_identity}:{name}"
    raise ValueError(f"unknown action_class {action_class!r}")


def parse_footprint_list(raw: Optional[str]) -> Tuple[str, ...]:
    """Parse a stored JSON array back to a tuple of strings (fail-closed).

    A malformed / non-list value returns ``()`` (the policy then treats the
    footprint as unbounded → UNKNOWN overlap).
    """
    import json

    if not raw:
        return ()
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return ()
    if not isinstance(value, list):
        return ()
    out = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item)
    return tuple(out)
