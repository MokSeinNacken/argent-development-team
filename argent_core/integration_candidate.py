"""Phase I2 — integration candidate model + deterministic merge logic.

This module is the pure, deterministic core of the integration / merge-queue
layer (ARGENT ARCHITECTURE V1 FINAL §14/§6.5, Phase I2 brief §1–§22).  It is
deliberately narrow and holds:

* :class:`CandidateState` — the bounded per-candidate merge-queue state (NOT a
  new primary job state; the 8-state ``job_state.PrimaryState`` model is
  untouched).
* :class:`MergeClassification` — the bounded, git-derived classification of a
  candidate's merge against the current integration target.
* :class:`IntegrationCandidate` — the versioned, frozen candidate record.
* :func:`deterministic_order` — the deterministic dependency→FIFO→priority→
  stale ordering (no LLM, no prose).
* :class:`GitClient` — a controller-constructed **argv-only** git runner
  (never a shell, never ``eval``/``exec``; every ref/SHA/path/branch is
  validated before it reaches git).
* :func:`classify_merge` — the authoritative conflict/stale-base detection via
  ``git merge-base`` / ``git merge-tree`` (no LLM conflict declaration).

**No I/O beyond git argv subprocess calls; no LLM; no shell; no host reads
beyond the explicitly validated repository/worktree paths.**
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .worktree import is_sha_like, validate_branch_identity

# ---------------------------------------------------------------------------
# Bounded constants
# ---------------------------------------------------------------------------

MAX_CANDIDATE_ID_LEN = 64
MAX_TARGET_LEN = 256
MAX_INTEGRATION_BRANCH_LEN = 256
MAX_CONFLICT_DETAIL_LEN = 4096
MAX_RESULT_JSON_BYTES = 64 * 1024

#: Candidate id prefix (mirrors the ``<prefix>+24 hex`` id convention used by
#: the rest of the store layer).
CANDIDATE_ID_PREFIX = "ic_"

#: Integration branches are always named ``integration/<queue-id>`` and MUST
#: never equal an integration target (the target is only ever read, never
#: written — promotion is a later phase I3/J).
INTEGRATION_BRANCH_PREFIX = "integration/"


class CandidateState(str, Enum):
    """Bounded per-candidate merge-queue state (Phase I2 §1)."""

    PENDING = "PENDING"
    READY = "READY"
    INTEGRATING = "INTEGRATING"
    CONFLICTED = "CONFLICTED"
    STALE = "STALE"
    BLOCKED = "BLOCKED"
    INTEGRATED = "INTEGRATED"
    FAILED = "FAILED"


#: Canonical order for the CHECK constraint (deterministic DDL string).
CANDIDATE_STATE_VALUES: Tuple[str, ...] = tuple(s.value for s in CandidateState)

#: Terminal candidate states (no further automatic transition).
TERMINAL_CANDIDATE_STATES: frozenset = frozenset(
    {CandidateState.INTEGRATED, CandidateState.CONFLICTED, CandidateState.STALE,
     CandidateState.BLOCKED, CandidateState.FAILED}
)

#: States from which a candidate may be (re-)evaluated into READY.
RE_EVALUABLE_STATES: frozenset = frozenset(
    {CandidateState.PENDING, CandidateState.STALE}
)


class MergeClassification(str, Enum):
    """Bounded git-derived classification of a candidate merge (Phase I2 §5)."""

    CLEAN_APPLY = "CLEAN_APPLY"
    DIVERGED_CLEAN = "DIVERGED_CLEAN"
    CONFLICT = "CONFLICT"
    STALE_BASE = "STALE_BASE"
    DEPENDENCY_NOT_INTEGRATED = "DEPENDENCY_NOT_INTEGRATED"
    UNKNOWN = "UNKNOWN"


class IntegrationError(Exception):
    """Base error for integration/merge-queue failures."""


class CandidateRevisionError(IntegrationError):
    """A candidate transition failed its revision CAS fence (Phase I2 §1/§9)."""


class CandidateNotFound(IntegrationError):
    """A referenced integration candidate does not exist."""


class GitCommandError(IntegrationError):
    """A git argv command failed or was refused fail-closed."""


@dataclass(frozen=True)
class IntegrationCandidate:
    """Versioned, frozen integration candidate record."""

    id: str
    repository: str
    integration_target: str
    source_job_id: str
    state: str
    queue_position: int
    priority: int = 0
    depends_on: Optional[str] = None
    base_commit: Optional[str] = None
    source_head: Optional[str] = None
    source_branch: Optional[str] = None
    integration_worktree_path: Optional[str] = None
    integration_branch: Optional[str] = None
    integrated_head: Optional[str] = None
    merge_classification: Optional[str] = None
    conflict_detail: Optional[str] = None
    revision: int = 0
    holder_owner_instance_id: Optional[str] = None
    holder_lease_epoch: int = 0
    result_json: Optional[str] = None
    last_error_code: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def is_terminal(self) -> bool:
        return CandidateState(self.state) in TERMINAL_CANDIDATE_STATES

    @classmethod
    def from_row(cls, row: dict) -> "IntegrationCandidate":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: row[k] for k in row.keys() if k in known})


# ---------------------------------------------------------------------------
# Deterministic candidate id
# ---------------------------------------------------------------------------


def candidate_id_for(repository: str, integration_target: str,
                     source_job_id: str) -> str:
    """Deterministic, bounded candidate id (idempotent creation)."""
    digest = hashlib.sha256(
        f"{repository}\x00{integration_target}\x00{source_job_id}".encode("utf-8")
    ).hexdigest()[:24]
    return CANDIDATE_ID_PREFIX + digest


# ---------------------------------------------------------------------------
# GitClient (argv-only, controller-constructed, no shell)
# ---------------------------------------------------------------------------

def _default_git_runner(argv: List[str]) -> Tuple[int, str, str]:
    """Run ``argv`` (a full ``git ...`` argument list) without a shell."""
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return (-1, "", "git invocation failed")
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace"),
    )


class GitClient:
    """Controller-constructed argv-only git client.

    Every public method validates its refs/SHAs/branch/path inputs before
    building a ``git -C <cwd> ...`` argv list; it never interpolates into a
    shell string and never spawns a shell or calls ``eval``/``exec``.  A
    failure to validate fails closed (``None`` / ``GitCommandError``) — a
    missing fact is never treated as a clean merge.
    """

    def __init__(
        self,
        runner: Optional[Callable[[List[str]], Tuple[int, str, str]]] = None,
        *,
        allowed_root: Optional[str] = None,
    ):
        self._runner = runner or _default_git_runner
        self._allowed_root = (
            os.path.realpath(os.path.abspath(os.fspath(allowed_root)))
            if allowed_root else None
        )

    def _canon_dir(self, path: Optional[str]) -> Optional[str]:
        """Canonicalise a repository/worktree path and fail closed (I2 LOW-8).

        Rejects ``None``/empty/non-string/relative paths; realpath-canonicalises
        (symlinks + ``..`` resolved); rejects a path that does not exist as a
        directory; and (when an ``allowed_root`` is configured) rejects a path
        resolving outside that root.  Returns the canonical absolute path or
        ``None`` (the caller then fails closed).
        """
        if path is None or not isinstance(path, str) or not path.strip():
            return None
        if not os.path.isabs(path):
            return None
        real = os.path.realpath(path)
        if not os.path.isdir(real):
            return None
        if self._allowed_root is not None:
            root = self._allowed_root
            if real != root and not real.startswith(root + os.sep):
                return None
        return real

    def _run(self, args: List[str], cwd: Optional[str] = None) -> Tuple[int, str, str]:
        argv = ["git"]
        if cwd:
            argv += ["-C", cwd]
        argv += args
        return self._runner(argv)

    # -- read-only provenance ------------------------------------------------

    def show_toplevel(self, path: str) -> Optional[str]:
        cwd = self._canon_dir(path)
        if cwd is None:
            return None
        rc, out, _ = self._run(["rev-parse", "--show-toplevel"], cwd=cwd)
        if rc != 0 or not out.strip():
            return None
        return os.path.realpath(out.strip())

    def head(self, path: str) -> Optional[str]:
        cwd = self._canon_dir(path)
        if cwd is None:
            return None
        rc, out, _ = self._run(["rev-parse", "HEAD"], cwd=cwd)
        if rc != 0 or not out.strip():
            return None
        return out.strip()

    def branch(self, path: str) -> Optional[str]:
        cwd = self._canon_dir(path)
        if cwd is None:
            return None
        rc, out, _ = self._run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
        if rc != 0 or not out.strip():
            return None
        return out.strip()

    def is_dirty(self, path: str) -> bool:
        cwd = self._canon_dir(path)
        if cwd is None:
            return True  # unreadable -> fail-closed (never "clean")
        rc, out, _ = self._run(["status", "--porcelain"], cwd=cwd)
        if rc != 0:
            return True  # unreadable -> fail-closed (never "clean")
        return bool(out.strip())

    def resolve_sha(self, path: str, ref: str) -> Optional[str]:
        cwd = self._canon_dir(path)
        if cwd is None:
            return None
        if not is_sha_like(ref):
            # Allow branch names too (validated as branch identities).
            try:
                validate_branch_identity(ref)
            except ValueError:
                return None
        rc, out, _ = self._run(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=cwd)
        if rc != 0 or not out.strip():
            return None
        return out.strip()

    def merge_base(self, path: str, a: str, b: str) -> Optional[str]:
        cwd = self._canon_dir(path)
        if cwd is None:
            return None
        if not is_sha_like(a) or not is_sha_like(b):
            return None
        rc, out, _ = self._run(["merge-base", a, b], cwd=cwd)
        if rc != 0 or not out.strip():
            return None
        return out.strip().splitlines()[0]

    def is_ancestor(self, path: str, ancestor: str, descendant: str) -> bool:
        cwd = self._canon_dir(path)
        if cwd is None:
            return False
        if not is_sha_like(ancestor) or not is_sha_like(descendant):
            return False
        rc, _, _ = self._run(["merge-base", "--is-ancestor", ancestor, descendant],
                             cwd=cwd)
        return rc == 0

    def merge_tree_conflicts(self, path: str, ours: str,
                             theirs: str) -> Optional[bool]:
        """Return True when the three-way merge conflicts, False when clean,
        or None when the operation could not be performed (fail-closed).

        Uses ``git merge-tree --write-tree <ours> <theirs>`` (git >= 2.38) which
        computes the merge base itself and exits 0 on a clean merge and 1 when
        there are conflicts.
        """
        cwd = self._canon_dir(path)
        if cwd is None:
            return None
        if not (is_sha_like(ours) and is_sha_like(theirs)):
            return None
        rc, out, err = self._run(
            ["merge-tree", "--write-tree", ours, theirs], cwd=cwd,
        )
        if rc == 0:
            return False
        if rc == 1:
            return True
        return None

    # -- branch helpers ------------------------------------------------------

    def branch_exists(self, repo: str, branch: str) -> bool:
        """True iff ``refs/heads/<branch>`` exists (validated, argv-only)."""
        cwd = self._canon_dir(repo)
        if cwd is None:
            return False
        try:
            validate_branch_identity(branch)
        except ValueError:
            return False
        rc, _, _ = self._run(
            ["rev-parse", "--verify", f"refs/heads/{branch}"], cwd=cwd)
        return rc == 0

    def delete_branch(self, repo: str, branch: str) -> bool:
        """Delete a branch by name (caller MUST have proven ownership first)."""
        cwd = self._canon_dir(repo)
        if cwd is None:
            return False
        try:
            validate_branch_identity(branch)
        except ValueError:
            return False
        rc, _, _ = self._run(["branch", "-D", branch], cwd=cwd)
        return rc == 0

    # -- mutating operations (integration worktree only) ---------------------

    def add_worktree(self, repo: str, path: str, branch: str, start_sha: str) -> bool:
        """Create a dedicated integration worktree + branch at ``start_sha``."""
        if not is_sha_like(start_sha):
            return False
        if not branch.startswith(INTEGRATION_BRANCH_PREFIX):
            return False
        try:
            validate_branch_identity(branch)
        except ValueError:
            return False
        cwd = self._canon_dir(repo)
        if cwd is None:
            return False
        # The worktree path must be an allowed (non-existent-yet or ours) path
        # under the allowed root; here we only require it be an absolute,
        # non-existing path (git worktree creates it).
        if path is None or not isinstance(path, str) or not os.path.isabs(path):
            return False
        rc, _, _ = self._run(
            ["worktree", "add", "-b", branch, path, start_sha], cwd=cwd,
        )
        return rc == 0

    def remove_worktree(self, repo: str, path: str) -> bool:
        cwd = self._canon_dir(repo)
        if cwd is None:
            return False
        if path is None or not isinstance(path, str) or not os.path.isabs(path):
            return False
        rc, _, _ = self._run(["worktree", "remove", "--force", path], cwd=cwd)
        return rc == 0

    def checkout_detached(self, repo: str, sha: str) -> bool:
        cwd = self._canon_dir(repo)
        if cwd is None:
            return False
        if not is_sha_like(sha):
            return False
        rc, _, _ = self._run(["checkout", "--detach", sha], cwd=cwd)
        return rc == 0

    def merge_no_ff(self, path: str, source_sha: str, message: str) -> Tuple[bool, Optional[str]]:
        """Merge ``source_sha`` into the current HEAD (no force flags).

        Returns ``(clean, error_detail)``.  On conflict the caller MUST abort
        with :meth:`merge_abort` so the integration worktree is left clean.
        """
        cwd = self._canon_dir(path)
        if cwd is None:
            return False, "invalid worktree path"
        if not is_sha_like(source_sha):
            return False, "invalid source_sha"
        rc, _, err = self._run(
            ["merge", "--no-ff", "--no-edit", "-m", message, source_sha], cwd=cwd,
        )
        if rc == 0:
            return True, None
        return False, (err.strip()[:MAX_CONFLICT_DETAIL_LEN] or "merge failed")

    def merge_abort(self, path: str) -> bool:
        cwd = self._canon_dir(path)
        if cwd is None:
            return False
        rc, _, _ = self._run(["merge", "--abort"], cwd=cwd)
        return rc == 0

    def reset_hard(self, path: str, sha: str) -> bool:
        cwd = self._canon_dir(path)
        if cwd is None:
            return False
        if not is_sha_like(sha):
            return False
        rc, _, _ = self._run(["reset", "--hard", sha], cwd=cwd)
        return rc == 0

    def changed_paths_vs(self, path: str, base_sha: str,
                         max_paths: int = 256) -> Tuple[str, ...]:
        """Authoritative changed paths ``base_sha..HEAD`` (bounded)."""
        cwd = self._canon_dir(path)
        if cwd is None:
            return ()
        if not is_sha_like(base_sha):
            return ()
        rc, out, _ = self._run(
            ["diff", "--name-only", base_sha, "HEAD"], cwd=cwd,
        )
        if rc != 0:
            return ()
        lines = [ln for ln in out.splitlines() if ln.strip()]
        return tuple(lines[:max_paths])


# ---------------------------------------------------------------------------
# Deterministic ordering (dependency -> FIFO -> priority -> stale)
# ---------------------------------------------------------------------------


class OrderOutcome:
    """Result of deterministic candidate ordering for one target."""

    def __init__(self, ordered: List[IntegrationCandidate],
                 deferred: List[IntegrationCandidate],
                 blocked: List[IntegrationCandidate]):
        self.ordered = ordered
        self.deferred = deferred
        self.blocked = blocked


def deterministic_order(
    candidates: Sequence[IntegrationCandidate],
    *,
    integrated_ids: Optional[set] = None,
) -> OrderOutcome:
    """Deterministically order READY candidates (Phase I2 §4).

    Order of precedence (brief §4, no LLM):

    1. explicit dependencies (a candidate is only orderable after every
       ``depends_on`` prerequisite is either already INTEGRATED or earlier in
       the ordered output);
    2. queue position / FIFO (ascending ``queue_position``);
    3. trusted priority (descending ``priority``);
    4. stale status (non-STALE before STALE — a STALE candidate is
       de-prioritised, it must be rebased, never silently integrated).

    Fail-closed on unknown structure:

    * a dependency that references an id not present in ``candidates`` and not
      already INTEGRATED → the dependent is ``deferred``
      (``DEPENDENCY_NOT_INTEGRATED``);
    * a dependency cycle → the members are ``blocked`` (BLOCK, no partial
      order).

    Only READY candidates are ordered; PENDING/INTEGRATING/terminal candidates
    are ignored (their promotion is a separate, fenced step).
    """
    integrated = set(integrated_ids or ())
    ready = [c for c in candidates if CandidateState(c.state) == CandidateState.READY]
    by_id = {c.id: c for c in ready}
    id_to_src = {c.id: c for c in candidates}  # full set for dependency lookup

    # Dependency references (candidate id) -> set of candidates that depend on it.
    dependents: Dict[str, List[str]] = {}
    indegree: Dict[str, int] = {c.id: 0 for c in ready}
    for c in ready:
        dep = c.depends_on
        if dep is None:
            continue
        if dep in integrated:
            continue  # already integrated -> satisfied
        dep_obj = id_to_src.get(dep)
        if dep_obj is not None:
            if CandidateState(dep_obj.state) == CandidateState.INTEGRATED:
                continue  # already integrated -> satisfied
            if dep in by_id:
                # dependency is READY -> real ordering edge.
                dependents.setdefault(dep, []).append(c.id)
                indegree[c.id] += 1
            else:
                # dependency exists but not READY and not INTEGRATED -> defer.
                indegree[c.id] = 1
                dependents.setdefault(dep, []).append(c.id)
        else:
            # dependency references an unknown id -> defer (DEPENDENCY_NOT_INTEGRATED).
            indegree[c.id] = 1
            dependents.setdefault(dep, []).append(c.id)

    deferred: List[IntegrationCandidate] = []
    blocked: List[IntegrationCandidate] = []

    # Kahn's algorithm with the FIFO/priority/stale tie-break as the heap key.
    import heapq

    def sort_key(c: IntegrationCandidate) -> Tuple:
        stale = 1 if CandidateState(c.state) == CandidateState.STALE else 0
        return (stale, c.queue_position, -c.priority)

    # Ready-with-zero-indegree heap (only ids that exist in by_id).
    heap = []
    for cid, deg in indegree.items():
        if deg == 0:
            heapq.heappush(heap, (sort_key(by_id[cid]), cid))

    ordered: List[IntegrationCandidate] = []
    processed = set()
    while heap:
        _, cid = heapq.heappop(heap)
        if cid in processed:
            continue
        processed.add(cid)
        ordered.append(by_id[cid])
        for dep_id in dependents.get(cid, []):
            if dep_id not in by_id:
                continue
            indegree[dep_id] -= 1
            if indegree[dep_id] == 0:
                heapq.heappush(heap, (sort_key(by_id[dep_id]), dep_id))

    # Anything remaining with indegree > 0: unknown dependency (defer) or a
    # cycle among READY candidates (block).
    for cid, deg in indegree.items():
        if cid in processed:
            continue
        c = by_id[cid]
        dep = c.depends_on
        if dep is not None and dep in by_id:
            # dependency is READY but never became free -> cycle.
            blocked.append(c)
        else:
            # unknown / not-ready dependency -> defer (DEPENDENCY_NOT_INTEGRATED).
            deferred.append(c)

    # Stable: deferred in FIFO order.
    deferred.sort(key=lambda c: c.queue_position)
    blocked.sort(key=lambda c: c.queue_position)
    return OrderOutcome(ordered=ordered, deferred=deferred, blocked=blocked)


# ---------------------------------------------------------------------------
# Merge classification (authoritative git)
# ---------------------------------------------------------------------------


def classify_merge(
    git: GitClient,
    repo: str,
    *,
    target_tip: Optional[str],
    source_head: Optional[str],
    claimed_base: Optional[str],
    dependency_integrated: bool = True,
) -> MergeClassification:
    """Authoritatively classify a candidate merge (Phase I2 §5).

    Order of checks (fail-closed; never an LLM conflict declaration):

    1. unresolved dependency → ``DEPENDENCY_NOT_INTEGRATED``;
    2. unreadable / non-SHA target or source head → ``UNKNOWN``;
    3. no common merge base → ``STALE_BASE`` (foreign/unrelated history);
    4. claimed base validation (BEFORE fast-forward) — the recorded base must
       still be an ancestor of both the current target tip AND the source head;
       otherwise ``STALE_BASE`` (the target advanced, or the source's recorded
       provenance is inconsistent);
    5. target tip is an ancestor of source head → ``CLEAN_APPLY`` (fast-forward
       eligible — the target did not advance past the source base);
    6. source is an ancestor of target (nothing new) → ``STALE_BASE``;
    7. ``git merge-tree --write-tree`` conflict test → ``CONFLICT`` or
       ``DIVERGED_CLEAN`` (diverged but clean — a normal ``--no-ff`` merge is
       safe, never a rebase).
    """
    if not dependency_integrated:
        return MergeClassification.DEPENDENCY_NOT_INTEGRATED
    if not is_sha_like(target_tip or "") or not is_sha_like(source_head or ""):
        return MergeClassification.UNKNOWN
    assert target_tip and source_head  # is_sha_like guarantees truthy
    mb = git.merge_base(repo, target_tip, source_head)
    if mb is None:
        return MergeClassification.STALE_BASE
    # Claimed base validation BEFORE any fast-forward classification: the
    # source's recorded base must still be an ancestor of the target tip AND
    # of the source head (I2 HIGH-3); otherwise the provenance is stale or
    # inconsistent and a rebase is required (never silently CLEAN_APPLY).
    if claimed_base and is_sha_like(claimed_base):
        if not git.is_ancestor(repo, claimed_base, target_tip):
            return MergeClassification.STALE_BASE
        if not git.is_ancestor(repo, claimed_base, source_head):
            return MergeClassification.STALE_BASE
    if target_tip == mb:
        return MergeClassification.CLEAN_APPLY
    if source_head == mb:
        return MergeClassification.STALE_BASE
    conflicts = git.merge_tree_conflicts(repo, target_tip, source_head)
    if conflicts is None:
        return MergeClassification.UNKNOWN
    if conflicts:
        return MergeClassification.CONFLICT
    return MergeClassification.DIVERGED_CLEAN


# ---------------------------------------------------------------------------
# Bounded result serialization
# ---------------------------------------------------------------------------

def serialize_candidate_result(result: Optional[dict]) -> Optional[str]:
    """Bounded JSON serialization of a candidate result payload (fail-closed)."""
    if result is None:
        return None
    raw = json.dumps(result, sort_keys=True, default=str)
    if len(raw.encode("utf-8")) > MAX_RESULT_JSON_BYTES:
        raise IntegrationError("candidate result exceeds the JSON byte budget")
    return raw
