"""Phase B3 — minimal Worktree/Writer ownership (evidence, no auto-cleanup).

Implements the minimal Phase-B worktree/writer binding (ARGENT ARCHITECTURE V1
FINAL §14/§6.5): 1 job = 1 writer worktree = at most 1 valid writer lease.
This module provides:

* :class:`WorktreeBinding` — the persisted ownership fields (canonical path,
  repo identity, base commit, branch identity, writer dispatch/lease binding,
  expected/current HEAD).
* :func:`resolve_canonical_worktree_path` — canonicalisation that rejects
  path injection / ``..`` / absolute-path / symlink escape.
* :func:`classify_worktree_recovery` — read-only recovery EVIDENCE only (never
  deletes, rebases or merges anything).
* :func:`writer_guard_for` — a broker writer-binding guard factory.

No full Worktree Registry / Merge Queue here (Phase I).  Dirty/ambiguous
worktrees are NEVER auto-deleted.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from .models import PermissionDenied

#: Recovery verdicts.
V_KEEP_DIRTY = "KEEP_DIRTY"
V_CLEANUP_PENDING = "CLEANUP_PENDING"
V_BLOCKED_DIVERGED = "BLOCKED_DIVERGED"
V_LOST = "LOST"
V_AMBIGUOUS_WRITER = "AMBIGUOUS_WRITER"

#: Bounded field lengths.
MAX_REPO_IDENTITY_LEN = 256
MAX_BRANCH_LEN = 256
MAX_COMMIT_LEN = 64

#: Writer binding modes persisted on ``supervisor_jobs.writer_binding_mode``.
#: ``BOUND`` marks a job whose writer/worktree binding is established via the
#: supervisor-authorized ``bind_writer_worktree`` primitive; the writer guard
#: enforces the full fencing token for such jobs (fail-closed).  ``None``/
#: absent marks a legacy unbound job (no binding to enforce).
WRITER_BINDING_BOUND = "BOUND"

#: Operational states the guard requires for a BOUND job (kept as string
#: literals to avoid a circular import with ``job_state``).
_PRIMARY_RUNNING = "RUNNING"
_STATUS_ACTIVE = "ACTIVE"

#: A 40/64-char hex commit SHA shape (defensive, not a proof of existence).
_HEX_SHA_CHARS = frozenset("0123456789abcdefABCDEF")


@dataclass(frozen=True)
class WorktreeBinding:
    """Persisted minimal writer/worktree ownership fields."""

    job_id: str
    canonical_worktree_path: str
    repo_identity: Optional[str] = None
    base_commit: Optional[str] = None
    branch_identity: Optional[str] = None
    writer_dispatch_id: Optional[str] = None
    writer_owner_instance_id: Optional[str] = None
    writer_lease_epoch: int = 0
    expected_head: Optional[str] = None
    current_head: Optional[str] = None

    def to_fields(self) -> dict:
        return {
            "canonical_worktree_path": self.canonical_worktree_path,
            "repo_identity": self.repo_identity,
            "base_commit": self.base_commit,
            "branch_identity": self.branch_identity,
            "writer_dispatch_id": self.writer_dispatch_id,
            "writer_owner_instance_id": self.writer_owner_instance_id,
            "writer_lease_epoch": self.writer_lease_epoch,
            "expected_head": self.expected_head,
            "current_head": self.current_head,
        }


def _git(args: list, cwd: str) -> Optional[str]:
    """Run a read-only ``git -C cwd ...`` command; ``None`` on any failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace").strip() or None


class GitProvenanceProvider:
    """Read-only git provenance for a worktree (injectable for tests).

    Reads the canonical repo identity (``git rev-parse --show-toplevel``),
    the current HEAD (``git rev-parse HEAD``), the branch identity
    (``git rev-parse --abbrev-ref HEAD``) and the dirty flag
    (``git status --porcelain``) — never mutating anything.  Every read is
    fail-closed: an unreadable fact is ``None``/dirty so recovery can never
    treat missing evidence as "clean".
    """

    def __init__(self, worktree_root: Optional[str] = None):
        self._worktree_root = worktree_root

    def _root(self, path: Optional[str] = None) -> Optional[str]:
        return path or self._worktree_root

    def repo_identity(self, path: Optional[str] = None) -> Optional[str]:
        root = self._root(path)
        if root is None:
            return None
        top = _git(["rev-parse", "--show-toplevel"], root)
        if not top:
            return None
        return os.path.realpath(top)

    def head(self, path: Optional[str] = None) -> Optional[str]:
        root = self._root(path)
        if root is None:
            return None
        return _git(["rev-parse", "HEAD"], root)

    def branch(self, path: Optional[str] = None) -> Optional[str]:
        root = self._root(path)
        if root is None:
            return None
        return _git(["rev-parse", "--abbrev-ref", "HEAD"], root)

    def dirty(self, path: Optional[str] = None) -> bool:
        root = self._root(path)
        if root is None:
            return True  # unknown -> fail-closed (never "clean")
        out = _git(["status", "--porcelain"], root)
        if out is None:
            return True  # unreadable -> fail-closed (never "clean")
        return bool(out)

    def changed_paths(self, path: Optional[str] = None,
                      max_paths: int = 32) -> tuple:
        """Return bounded changed paths vs HEAD (``git diff --name-only HEAD``).

        Read-only, fail-closed: any failure returns ``()`` (never a guess).
        The result is capped at ``max_paths`` (bounded, no whole-repo scan).
        Used as authoritative write/diff scope evidence for artifact refs (D3).
        """
        root = self._root(path)
        if root is None:
            return ()
        out = _git(["diff", "--name-only", "HEAD"], root)
        if out is None:
            return ()
        if not isinstance(max_paths, int) or max_paths <= 0:
            max_paths = 32
        lines = [ln for ln in out.splitlines() if ln.strip()]
        return tuple(lines[:max_paths])


def is_sha_like(value: str) -> bool:
    return (
        isinstance(value, str)
        and 7 <= len(value) <= MAX_COMMIT_LEN
        and all(c in _HEX_SHA_CHARS for c in value)
    )


def resolve_canonical_worktree_path(path, *, base_root: Optional[str] = None) -> str:
    """Canonically resolve a worktree path and reject escapes/injection.

    * Absolute paths and ``..`` traversal are rejected fail-closed;
    * the result is ``os.path.realpath``-canonicalised (symlinks resolved);
    * if ``base_root`` is given, the resolved path must stay within it;
    * the result must be an absolute, non-empty path.

    Returns the canonical string or raises :class:`ValueError`.
    """
    if path is None or not isinstance(path, str) or not path.strip():
        raise ValueError("worktree path must be a non-empty string")
    raw = path.strip()
    if os.path.isabs(raw):
        raise ValueError("worktree path must be relative to the worktrees root")
    norm = os.path.normpath(raw)
    if norm == ".." or norm.startswith(".." + os.sep):
        raise ValueError("worktree path may not escape via '..'")
    if base_root is not None:
        base = os.path.realpath(os.path.abspath(os.fspath(base_root)))
        joined = os.path.realpath(os.path.join(base, norm))
        if not _within(base, joined):
            raise ValueError("worktree path resolves outside the worktrees root")
        return joined
    return os.path.realpath(os.path.abspath(norm))


def _within(root: str, path: str) -> bool:
    if path == root:
        return True
    return path.startswith(root + os.sep)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone(timezone.utc).isoformat()


def validate_worktree_binding_path(path, *, base_root: Optional[str] = None) -> str:
    """Validate and realpath-canonicalise an absolute worktree binding path.

    Rejects ``None``/empty/non-string values; resolves symlinks via
    ``os.path.realpath``; if ``base_root`` is given, the resolved path must stay
    within it (no symlink escape / foreign-root worktree).  Returns the
    canonical absolute path or raises :class:`ValueError`.
    """
    if path is None or not isinstance(path, str) or not path.strip():
        raise ValueError("canonical worktree path must be a non-empty string")
    real = os.path.realpath(os.path.abspath(os.fspath(path.strip())))
    if base_root is not None:
        base = os.path.realpath(os.path.abspath(os.fspath(base_root)))
        if not _within(base, real):
            raise ValueError("canonical worktree path resolves outside the worktrees root")
    return real


def validate_repo_identity(repo: Optional[str]) -> Optional[str]:
    if repo is None:
        return None
    if not isinstance(repo, str) or not repo.strip():
        raise ValueError("repo_identity must be a non-empty string when set")
    if len(repo) > MAX_REPO_IDENTITY_LEN:
        raise ValueError(f"repo_identity exceeds {MAX_REPO_IDENTITY_LEN} chars")
    return repo


def validate_branch_identity(branch: Optional[str]) -> Optional[str]:
    if branch is None:
        return None
    if not isinstance(branch, str) or not branch.strip():
        raise ValueError("branch_identity must be a non-empty string when set")
    if len(branch) > MAX_BRANCH_LEN:
        raise ValueError(f"branch_identity exceeds {MAX_BRANCH_LEN} chars")
    if any(c in branch for c in ("\n", "\r", " ")):
        raise ValueError("branch_identity contains whitespace")
    return branch


@dataclass(frozen=True)
class WorktreeRecoveryVerdict:
    """Read-only recovery EVIDENCE (never an action)."""

    verdict: str
    reason: str


@dataclass(frozen=True)
class WorktreeEvidence:
    """Read-only git/worktree facts gathered for recovery."""

    repo_identity: Optional[str] = None
    head: Optional[str] = None
    dirty: bool = False


def classify_worktree_recovery(
    binding: WorktreeBinding,
    evidence: WorktreeEvidence,
    *,
    writer_terminal: Optional[bool] = None,
) -> WorktreeRecoveryVerdict:
    """Classify a known worktree for recovery (read-only, evidence only).

    Rules (ARCHITECTURE V1 FINAL §14 / REVIEW §12), fail-closed:

    * wrong/unknown repo identity -> ``LOST`` (never touch a foreign/unknown
      repo);
    * HEAD diverges from the expected/base -> ``BLOCKED_DIVERGED`` (never
      overwrite); base vs expected comparison is conservative;
    * dirty but PROVEN job-owned (bound writer + valid binding) -> ``KEEP_DIRTY``;
      dirty WITHOUT ownership proof -> ``AMBIGUOUS_WRITER``;
    * clean, exact repo + HEAD + base binding AND an authoritatively terminal
      old writer -> ``CLEANUP_PENDING``;
    * otherwise fail-closed ``AMBIGUOUS_WRITER`` (missing/contradictory facts
      never authorise cleanup).

    ``writer_terminal`` is a NEW input fact from trusted process/dispatch
    evidence (Process Registry terminal / dispatch terminal / journal) — never
    agent prose.  ``None`` means "terminality unknown" and blocks
    ``CLEANUP_PENDING``.

    The caller must NEVER delete on ``KEEP_DIRTY``/``BLOCKED_DIVERGED`` /
    ``AMBIGUOUS_WRITER``/``LOST``.
    """
    # F6: exact repo binding is a precondition for ANY positive verdict.
    if binding.repo_identity is None or evidence.repo_identity is None:
        return WorktreeRecoveryVerdict(V_LOST, "missing_repo_identity")
    if evidence.repo_identity != binding.repo_identity:
        return WorktreeRecoveryVerdict(V_LOST, "foreign_repo")

    # F6: ownership proof requires a bound writer + a valid writer binding.
    owned = (
        binding.writer_dispatch_id is not None
        and binding.writer_owner_instance_id is not None
        and binding.writer_lease_epoch >= 1
    )

    if evidence.dirty:
        if not owned:
            return WorktreeRecoveryVerdict(V_AMBIGUOUS_WRITER, "dirty_unowned")
        return WorktreeRecoveryVerdict(V_KEEP_DIRTY, "dirty_job_owned")

    # F6: exact base/HEAD binding required for cleanup.
    if binding.expected_head is None:
        return WorktreeRecoveryVerdict(V_AMBIGUOUS_WRITER, "missing_expected_head")
    if evidence.head is None:
        return WorktreeRecoveryVerdict(V_AMBIGUOUS_WRITER, "missing_head")
    if evidence.head != binding.expected_head:
        return WorktreeRecoveryVerdict(V_BLOCKED_DIVERGED, "head_diverged")
    if binding.base_commit is None:
        return WorktreeRecoveryVerdict(V_AMBIGUOUS_WRITER, "missing_base_commit")

    # F6: cleanup only on an authoritatively terminal old writer.
    if writer_terminal is not True:
        return WorktreeRecoveryVerdict(V_AMBIGUOUS_WRITER, "writer_terminality_unknown")

    return WorktreeRecoveryVerdict(V_CLEANUP_PENDING, "clean_terminal")


def writer_guard_for(
    job_provider: Callable[[], Optional[dict]],
    *,
    job_id: Optional[str] = None,
    dispatch_id: Optional[str] = None,
    owner_instance_id: Optional[str] = None,
    lease_epoch: Optional[int] = None,
    facts_version: Optional[int] = None,
    now_iso: Optional[Callable[[], str]] = None,
) -> Callable[[str, object, str], None]:
    """Build a broker writer-binding guard bound to a full fencing token.

    The token is ``(job_id, dispatch_id, owner_instance_id, lease_epoch,
    facts_version)`` captured when the guard is installed (F1).  Immediately
    before a mutating broker write the guard verifies, against a FRESH job
    read:

    1. a legacy unbound job (``writer_binding_mode`` unset AND no declared
       binding) passes through unchanged — nothing to enforce;
    2. a ``writer_binding_mode=BOUND`` job is RUNNING/ACTIVE with a complete
       binding and an unexpired lease;
    3. the job's current (owner, epoch, facts_version) equals the token;
    4. the writer binding (dispatch/owner/epoch) equals the token;
    5. the job's canonical worktree path equals the broker scope root.

    ANY deviation raises :class:`PermissionDenied` (fail-closed).
    """
    now_fn = now_iso or _utc_now_iso

    def guard(scope_root_arg, role, source) -> None:
        job = job_provider()
        if job is None:
            raise PermissionDenied("writer_binding: job_missing")
        mode = job.get("writer_binding_mode")
        job_path = job.get("canonical_worktree_path")
        writer_dispatch = job.get("writer_dispatch_id")

        # Legacy unbound job: no declared binding and not marked BOUND -> no-op.
        if mode != WRITER_BINDING_BOUND and job_path is None \
                and writer_dispatch is None:
            return

        bound = mode == WRITER_BINDING_BOUND

        # F3: a BOUND job must carry a COMPLETE binding, else fail closed.
        if bound:
            if job.get("primary_state") != _PRIMARY_RUNNING \
                    or job.get("status") != _STATUS_ACTIVE:
                raise PermissionDenied("writer_binding: job_not_running_active")
            if job_path is None or writer_dispatch is None \
                    or job.get("writer_owner_instance_id") is None:
                raise PermissionDenied("writer_binding: binding_incomplete")

        # F1: enforce the captured fencing token fields.
        if job_id is not None and job.get("id") != job_id:
            raise PermissionDenied("writer_binding: job_id_mismatch")
        if owner_instance_id is not None \
                and job.get("owner_instance_id") != owner_instance_id:
            raise PermissionDenied("writer_binding: owner_mismatch")
        if lease_epoch is not None and job.get("lease_epoch") != lease_epoch:
            raise PermissionDenied("writer_binding: epoch_mismatch")
        if facts_version is not None and job.get("facts_version") != facts_version:
            raise PermissionDenied("writer_binding: facts_version_mismatch")

        if bound:
            expires = job.get("lease_expires_at")
            if expires is None or not expires > now_fn():
                raise PermissionDenied("writer_binding: lease_expired")
            if job.get("writer_owner_instance_id") != owner_instance_id:
                raise PermissionDenied("writer_binding: writer_owner_mismatch")
            if job.get("writer_lease_epoch") != lease_epoch:
                raise PermissionDenied("writer_binding: writer_epoch_mismatch")

        if dispatch_id is None or writer_dispatch != dispatch_id:
            raise PermissionDenied("writer_binding: not_writer_dispatch")

        real = os.path.realpath(os.path.abspath(os.fspath(scope_root_arg)))
        if job_path is None or os.path.realpath(job_path) != real:
            raise PermissionDenied("writer_binding: worktree_path_mismatch")

    return guard
