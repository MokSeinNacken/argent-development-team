"""Phase D2 — trusted, bounded local retrieval (ARGENT V1 FINAL §12/§16).

This module is the **only** deterministic retrieval surface for building a
Context Pack.  It performs **no provider calls, no vector DB, no embeddings,
no shell commands and no external search** — it is a pure function of bounded
local inputs (authorized filesystem roots + the SQLite Store).

Design invariants (verbindlich, §12/§16):

* **Root allow-list only.**  Every filesystem read is confined to a canonical
  ``authorized_root`` that must be inside the policy's ``allowed_roots``
  (whitelist).  ``~/.ssh``, ``~/.config``, ``/etc``, ``/proc``, ``/sys`` and
  any other home-scope path outside the allowed roots are **denied fail-closed**.
* **No path traversal / symlink escape.**  Every target is ``os.path.realpath``
  canonicalised and prefix-checked against the authorized root.  ``..``,
  absolute-path injection and symlink escapes are rejected before any read.
* **Bounded reads.**  Every read is capped by ``max_excerpt_bytes``; every result
  set is capped by ``max_results`` and ``max_bytes``.  Oversized files produce a
  bounded excerpt with a truncation marker + excerpt hash, never a whole-file
  dump.  There is **no** "read entire repo" path.
* **Deterministic order.**  Results are sorted by a deterministic local key
  (lexicographic by ``(source_type, ref, content)``); equal scores are NOT
  LLM-ranked.  Same state → same selection → same content hash.
* **Fail-closed.**  A refused/denied/failed request raises
  :class:`RetrievalError` with a bounded code.  Never a silent substitution by a
  "similar" file.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

from .models import ArgentError
from .context_pack import ContextError

# ---------------------------------------------------------------------------
# Version / limits
# ---------------------------------------------------------------------------

RETRIEVAL_POLICY_VERSION = "1"

# Hard ceilings for a single request (a request may only LOWER these).
MAX_RESULTS_HARD = 500
MAX_BYTES_HARD = 1 << 22          # 4 MiB total per request
MAX_EXCERPT_BYTES_HARD = 1 << 17  # 128 KiB per excerpt
MAX_FILES_SCANNED = 200           # bounded file scan during symbol search
MAX_MATCH_RESULTS_PER_FILE = 16   # bounded matches returned per file

#: Truncation marker appended when a file excerpt is cut short.
TRUNCATION_MARKER = "\n[TRUNCATED]"

#: Bounded reference/metadata limits (mirrors context_pack F4).
MAX_REF_LEN = 512
MAX_METADATA_ENTRIES = 16
MAX_METADATA_KEY_LEN = 128
MAX_METADATA_VALUE_LEN = 1024


# ---------------------------------------------------------------------------
# Enums / errors
# ---------------------------------------------------------------------------


class RetrievalType(str, Enum):
    """Bounded retrieval kinds (no vector DB, no embeddings)."""

    EXACT_REF = "EXACT_REF"
    FILE_EXCERPT = "FILE_EXCERPT"
    SYMBOL_OR_TEXT_MATCH = "SYMBOL_OR_TEXT_MATCH"
    ARTIFACT_LOOKUP = "ARTIFACT_LOOKUP"
    FACT_LOOKUP = "FACT_LOOKUP"
    HANDOFF_LOOKUP = "HANDOFF_LOOKUP"
    CHECKPOINT_LOOKUP = "CHECKPOINT_LOOKUP"


#: Filesystem-bound kinds (require an authorized_root within the allow-list).
_FILESYSTEM_TYPES = frozenset({
    RetrievalType.EXACT_REF,
    RetrievalType.FILE_EXCERPT,
    RetrievalType.SYMBOL_OR_TEXT_MATCH,
    RetrievalType.ARTIFACT_LOOKUP,
})

#: Store-bound kinds (query the SQLite ledger, no filesystem root).
_STORE_TYPES = frozenset({
    RetrievalType.FACT_LOOKUP,
    RetrievalType.HANDOFF_LOOKUP,
    RetrievalType.CHECKPOINT_LOOKUP,
})


class RetrievalError(ContextError):
    """A retrieval request was refused or failed (bounded code, fail-closed).

    ``code`` is one of the bounded ``RETRIEVAL_*`` reason codes below.  This is
    NEVER a shell fallback, never an unbounded read, never a re-ranking.
    """

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code, detail)


#: Canonical retrieval reason codes (bounded).
RETRIEVAL_ROOT_DENIED = "RETRIEVAL_ROOT_DENIED"
RETRIEVAL_PATH_TRAVERSAL = "RETRIEVAL_PATH_TRAVERSAL"
RETRIEVAL_SYMLINK_ESCAPE = "RETRIEVAL_SYMLINK_ESCAPE"
RETRIEVAL_INVALID_REQUEST = "RETRIEVAL_INVALID_REQUEST"
RETRIEVAL_FORBIDDEN_PATTERN = "RETRIEVAL_FORBIDDEN_PATTERN"
RETRIEVAL_LIMIT_EXCEEDED = "RETRIEVAL_LIMIT_EXCEEDED"
RETRIEVAL_NOT_FOUND = "RETRIEVAL_NOT_FOUND"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalItem:
    """One bounded retrieval result item."""

    ref: str
    source_type: str = ""
    location: str = ""
    content: str = ""            # bounded excerpt (may be empty for pure refs)
    content_hash: str = ""
    truncated: bool = False
    metadata: tuple = ()         # sorted (key, value) string pairs


@dataclass(frozen=True)
class RetrievalResult:
    """A bounded, deterministically ordered retrieval result set."""

    items: tuple                # tuple[RetrievalItem, ...]
    truncated: bool
    total_bytes: int
    provenance: tuple           # tuple[str, ...] deterministic source descriptors


@dataclass(frozen=True)
class RetrievalRequest:
    """A single bounded retrieval request (frozen)."""

    job_id: str
    dispatch_id: str
    source_type: RetrievalType
    authorized_root: str = ""
    task_id: str = ""
    reference: str = ""
    query: str = ""
    revision: str = ""           # trusted revision binding (e.g. git HEAD)
    excerpt_offset: int = 0
    excerpt_length: int = 0      # 0 => up to max_excerpt_bytes
    max_results: Optional[int] = None
    max_bytes: Optional[int] = None
    max_excerpt_bytes: Optional[int] = None


@dataclass(frozen=True)
class RetrievalPolicy:
    """Versioned, frozen retrieval policy (limits + root allow/deny)."""

    policy_version: str = RETRIEVAL_POLICY_VERSION
    max_results_default: int = 50
    max_bytes_default: int = 262144       # 256 KiB
    max_excerpt_bytes_default: int = 16384  # 16 KiB
    allowed_roots: tuple = ()             # canonical absolute roots (whitelist)
    denied_roots: tuple = ()              # canonical absolute roots (always denied)
    forbidden_patterns: tuple = ()        # regex source strings; a match fails closed


def default_denied_roots() -> tuple:
    """Canonical, always-denied roots (home secrets/config/system pseudo-fs)."""
    home = os.path.realpath(os.path.expanduser("~"))
    return tuple(sorted({
        os.path.realpath(os.path.join(home, ".ssh")),
        os.path.realpath(os.path.join(home, ".config")),
        os.path.realpath(os.path.join(home, ".gnupg")),
        os.path.realpath(os.path.join(home, ".local", "share", "keyrings")),
        os.path.realpath("/etc"),
        os.path.realpath("/proc"),
        os.path.realpath("/sys"),
        os.path.realpath("/dev"),
    }))


def make_default_policy(allowed_roots: Sequence[str] = ()) -> RetrievalPolicy:
    """Build a default policy with the given allowed roots (whitelist)."""
    return RetrievalPolicy(
        allowed_roots=tuple(sorted(os.path.realpath(r) for r in allowed_roots)),
        denied_roots=default_denied_roots(),
        forbidden_patterns=(
            r"\.\.",                      # any '..' traversal
            r"IMPORTANT\s+SYSTEM\s+POLICY",
            r"OWNER_INSTRUCTION",
            r"TRUSTED_POLICY",
            r"SYSTEM\s*:",
        ),
    )


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------


def _within(root: str, path: str) -> bool:
    if path == root:
        return True
    return path.startswith(root + os.sep)


def _stable_json(value) -> str:
    import json
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _content_hash(content: str) -> str:
    return _sha256(content.encode("utf-8"))


def _canonical_metadata(mapping: Optional[dict]) -> tuple:
    if not mapping:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in mapping.items()))


def _sort_key(item: RetrievalItem):
    return (item.source_type, item.ref, item.content, item.content_hash)


def _sorted_items(items: Sequence[RetrievalItem]) -> tuple:
    return tuple(sorted(items, key=_sort_key))


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class RetrievalEngine:
    """Trusted, bounded, deterministic local retrieval engine.

    ``policy`` supplies the allow-list + limits; ``store`` (optional) supplies
    FACT/HANDOFF/CHECKPOINT lookups.  Filesystem reads use only
    ``os``/``pathlib`` and are always realpath-prefix-checked against the
    authorized root.
    """

    def __init__(self, policy: Optional[RetrievalPolicy] = None, store=None):
        self._policy = policy or make_default_policy()
        self._store = store

    # -- request validation ------------------------------------------------

    def validate_request(self, request: RetrievalRequest) -> None:
        """Validate a request against the policy (fail-closed)."""
        if not isinstance(request.source_type, RetrievalType):
            raise RetrievalError(RETRIEVAL_INVALID_REQUEST,
                                 f"unknown source_type {request.source_type!r}")
        for field in ("job_id", "dispatch_id"):
            value = getattr(request, field)
            if not isinstance(value, str) or not value:
                raise RetrievalError(RETRIEVAL_INVALID_REQUEST,
                                     f"empty {field}")
        self._check_forbidden(request.reference)
        self._check_forbidden(request.query)
        if request.source_type in _FILESYSTEM_TYPES:
            self._validate_root(request.authorized_root)

    def _check_forbidden(self, text: str) -> None:
        for pattern in self._policy.forbidden_patterns:
            if re.search(pattern, text or ""):
                raise RetrievalError(RETRIEVAL_FORBIDDEN_PATTERN,
                                     f"matched forbidden pattern {pattern!r}")

    def _validate_root(self, root) -> str:
        try:
            root = os.fspath(root)
        except TypeError:
            root = ""
        if not isinstance(root, str) or not root.strip():
            raise RetrievalError(RETRIEVAL_ROOT_DENIED, "empty authorized_root")
        real = os.path.realpath(os.path.abspath(os.fspath(root)))
        for denied in self._policy.denied_roots:
            if _within(denied, real):
                raise RetrievalError(RETRIEVAL_ROOT_DENIED,
                                     f"root {real!r} is inside a denied root")
        if not self._policy.allowed_roots:
            raise RetrievalError(RETRIEVAL_ROOT_DENIED,
                                 "no allowed roots configured (fail-closed)")
        if not any(_within(a, real) for a in self._policy.allowed_roots):
            raise RetrievalError(RETRIEVAL_ROOT_DENIED,
                                 f"root {real!r} not in the allow-list")
        return real

    def _resolve_within(self, root_real: str, relpath: str) -> str:
        """Resolve ``relpath`` against ``root_real``, rejecting escapes."""
        if not isinstance(relpath, str) or not relpath.strip():
            raise RetrievalError(RETRIEVAL_PATH_TRAVERSAL, "empty reference")
        if os.path.isabs(relpath):
            raise RetrievalError(RETRIEVAL_PATH_TRAVERSAL,
                                 "reference must be relative to the root")
        norm = os.path.normpath(relpath)
        if norm == ".." or norm.startswith(".." + os.sep):
            raise RetrievalError(RETRIEVAL_PATH_TRAVERSAL,
                                 "reference may not escape via '..'")
        joined = os.path.realpath(os.path.join(root_real, norm))
        if not _within(root_real, joined):
            raise RetrievalError(RETRIEVAL_SYMLINK_ESCAPE,
                                 f"reference {relpath!r} escapes the root")
        return joined

    def _resolve_limit(self, value, default: int, name: str) -> int:
        """Resolve a request limit strictly (None -> policy default).

        ``0`` / negative / non-int values are refused fail-closed with
        ``RETRIEVAL_INVALID_REQUEST`` (a request may only LOWER a limit to a
        positive integer, never disable or corrupt it).
        """
        if value is None:
            value = default
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RetrievalError(
                RETRIEVAL_INVALID_REQUEST,
                f"{name} must be a positive integer (got {value!r})",
            )
        return value

    def _limits(self, request: RetrievalRequest):
        max_results = min(
            self._resolve_limit(request.max_results,
                                self._policy.max_results_default, "max_results"),
            MAX_RESULTS_HARD,
        )
        max_bytes = min(
            self._resolve_limit(request.max_bytes,
                                self._policy.max_bytes_default, "max_bytes"),
            MAX_BYTES_HARD,
        )
        max_excerpt = min(
            self._resolve_limit(request.max_excerpt_bytes,
                                self._policy.max_excerpt_bytes_default,
                                "max_excerpt_bytes"),
            MAX_EXCERPT_BYTES_HARD,
        )
        return max_results, max_bytes, max_excerpt

    @staticmethod
    def _bounded_excerpt(content: str, max_excerpt: int) -> str:
        """Bound a rendered store item to ``max_excerpt`` bytes (marker on cut)."""
        data = content.encode("utf-8")
        if len(data) <= max_excerpt:
            return content
        cut = data[:max_excerpt].decode("utf-8", errors="replace")
        return cut + TRUNCATION_MARKER

    # -- bounded file read --------------------------------------------------

    def _bounded_read(self, path: str, max_excerpt: int, offset: int,
                      length: int) -> RetrievalItem:
        """Read a bounded excerpt of ``path`` (truncation marker + hash)."""
        size = os.path.getsize(path)
        if offset < 0:
            offset = 0
        if length and length > 0:
            limit = min(length, max_excerpt)
        else:
            limit = max_excerpt
        truncated = (size - offset) > limit
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                raw = fh.read(limit)
        except OSError as exc:
            raise RetrievalError(RETRIEVAL_NOT_FOUND,
                                 f"read {path!r} failed: {exc}")
        text = raw.decode("utf-8", errors="replace")
        content = text + (TRUNCATION_MARKER if truncated else "")
        return RetrievalItem(
            ref=path, source_type=RetrievalType.FILE_EXCERPT.value,
            location=path, content=content,
            content_hash=_content_hash(content),
            truncated=truncated,
        )

    # -- filesystem retrievers ---------------------------------------------

    def _retrieve_exact_or_excerpt(self, request: RetrievalRequest,
                                   max_results, max_bytes, max_excerpt) -> RetrievalResult:
        root = self._validate_root(request.authorized_root)
        target = self._resolve_within(root, request.reference)
        if not os.path.isfile(target):
            raise RetrievalError(RETRIEVAL_NOT_FOUND,
                                 f"{request.reference!r} is not a file")
        item = self._bounded_read(target, max_excerpt, request.excerpt_offset,
                                  request.excerpt_length)
        meta = {"revision": request.revision} if request.revision else {}
        item = RetrievalItem(
            ref=item.ref, source_type=request.source_type.value,
            location=item.location, content=item.content,
            content_hash=item.content_hash, truncated=item.truncated,
            metadata=_canonical_metadata(meta),
        )
        return RetrievalResult(
            items=(item,), truncated=item.truncated,
            total_bytes=len(item.content.encode("utf-8")),
            provenance=(request.source_type.value,),
        )

    def _retrieve_symbol(self, request: RetrievalRequest, max_results,
                         max_bytes, max_excerpt) -> RetrievalResult:
        root = self._validate_root(request.authorized_root)
        query = request.query
        if not query:
            raise RetrievalError(RETRIEVAL_INVALID_REQUEST, "empty query")
        items: list = []
        total = 0
        scanned = 0
        truncated = False
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            filenames.sort()
            for name in filenames:
                if scanned >= MAX_FILES_SCANNED:
                    truncated = True
                    break
                scanned += 1
                path = os.path.realpath(os.path.join(dirpath, name))
                if not _within(root, path):
                    continue
                try:
                    os.path.getsize(path)
                except OSError:
                    continue
                # Oversized files use the bounded FILE_EXCERPT path.
                item = self._bounded_read(path, max_excerpt, 0, 0)
                if query in item.content or re.search(query, item.content):
                    matches = _extract_matches(item.content, query)
                    excerpt = "\n".join(matches[:MAX_MATCH_RESULTS_PER_FILE])
                    if excerpt:
                        sz = len(excerpt.encode("utf-8"))
                        if len(items) >= max_results or total + sz > max_bytes:
                            truncated = True
                            break
                        items.append(RetrievalItem(
                            ref=path,
                            source_type=RetrievalType.SYMBOL_OR_TEXT_MATCH.value,
                            location=path, content=excerpt,
                            content_hash=_content_hash(excerpt),
                            truncated=item.truncated,
                            metadata=_canonical_metadata(
                                {"revision": request.revision} if request.revision else {}),
                        ))
                        total += sz
            if truncated:
                break
        return RetrievalResult(
            items=_sorted_items(items),
            truncated=truncated, total_bytes=total,
            provenance=(RetrievalType.SYMBOL_OR_TEXT_MATCH.value,),
        )

    def _retrieve_artifact(self, request: RetrievalRequest, max_results,
                           max_bytes, max_excerpt) -> RetrievalResult:
        root = self._validate_root(request.authorized_root)
        ref = request.reference
        items: list = []
        total = 0
        truncated = False
        names = sorted(os.listdir(root)) if os.path.isdir(root) else []
        for name in names:
            path = os.path.realpath(os.path.join(root, name))
            if not _within(root, path):
                continue
            if ref and ref not in name and ref not in path:
                continue
            if not os.path.isfile(path):
                continue
            item = self._bounded_read(path, max_excerpt, 0, 0)
            sz = len(item.content.encode("utf-8"))
            if len(items) >= max_results or total + sz > max_bytes:
                truncated = True
                break
            items.append(RetrievalItem(
                ref=path, source_type=RetrievalType.ARTIFACT_LOOKUP.value,
                location=path, content=item.content,
                content_hash=item.content_hash, truncated=item.truncated,
            ))
            total += sz
        return RetrievalResult(
            items=_sorted_items(items),
            truncated=truncated, total_bytes=total,
            provenance=(RetrievalType.ARTIFACT_LOOKUP.value,),
        )

    # -- store retrievers ---------------------------------------------------

    def _task_id_for(self, request: RetrievalRequest) -> str:
        if request.task_id:
            return request.task_id
        if self._store is not None:
            job = self._store.get_supervisor_job(request.job_id)
            if job:
                return job.get("task_id") or ""
        return ""

    def _retrieve_facts(self, request: RetrievalRequest, max_results,
                        max_bytes, max_excerpt) -> RetrievalResult:
        if self._store is None:
            return RetrievalResult(items=(), truncated=False, total_bytes=0,
                                   provenance=(RetrievalType.FACT_LOOKUP.value,))
        task_id = self._task_id_for(request)
        items: list = []
        total = 0
        truncated = False

        def _add(ref, content) -> bool:
            nonlocal total, truncated
            content = self._bounded_excerpt(content, max_excerpt)
            sz = len(content.encode("utf-8"))
            if len(items) >= max_results or total + sz > max_bytes:
                truncated = True
                return False
            items.append(RetrievalItem(
                ref=ref, source_type=RetrievalType.FACT_LOOKUP.value,
                content=content, content_hash=_content_hash(content),
            ))
            total += sz
            return True

        for f in self._store.list_findings(task_id or None):
            if not _add(f.id, f"{f.severity}: {f.description} ({f.status.value})"):
                break
        for tr in self._store.list_test_runs(task_id or None):
            if not _add(tr.id, f"test {tr.result.value}: {tr.detail or ''}"):
                break
        for dec in self._store.list_decisions(task_id or None):
            if not _add(dec.id, f"decision {dec.decision}: {dec.detail or ''}"):
                break
        for rev in self._store.list_reviews(task_id or None):
            if not _add(rev.id, f"review {rev.verdict}: {rev.detail or ''}"):
                break

        return RetrievalResult(
            items=_sorted_items(items), truncated=truncated,
            total_bytes=total, provenance=(RetrievalType.FACT_LOOKUP.value,),
        )

    def _retrieve_handoffs(self, request: RetrievalRequest, max_results,
                           max_bytes, max_excerpt) -> RetrievalResult:
        if self._store is None:
            return RetrievalResult(items=(), truncated=False, total_bytes=0,
                                   provenance=(RetrievalType.HANDOFF_LOOKUP.value,))
        rows = self._store.list_handoffs_v2(request.job_id)
        items: list = []
        total = 0
        truncated = False
        for row in rows:
            content = self._bounded_excerpt(_render_handoff_row(row), max_excerpt)
            sz = len(content.encode("utf-8"))
            if len(items) >= max_results or total + sz > max_bytes:
                truncated = True
                break
            items.append(RetrievalItem(
                ref=row["handoff_id"], source_type=RetrievalType.HANDOFF_LOOKUP.value,
                content=content, content_hash=_content_hash(content),
            ))
            total += sz
        return RetrievalResult(
            items=_sorted_items(items), truncated=truncated,
            total_bytes=total, provenance=(RetrievalType.HANDOFF_LOOKUP.value,),
        )

    def _retrieve_checkpoints(self, request: RetrievalRequest, max_results,
                              max_bytes, max_excerpt) -> RetrievalResult:
        if self._store is None:
            return RetrievalResult(items=(), truncated=False, total_bytes=0,
                                   provenance=(RetrievalType.CHECKPOINT_LOOKUP.value,))
        rows = self._store.list_checkpoints(request.job_id)
        items: list = []
        total = 0
        truncated = False
        for row in rows:
            content = self._bounded_excerpt(_render_checkpoint_row(row), max_excerpt)
            sz = len(content.encode("utf-8"))
            if len(items) >= max_results or total + sz > max_bytes:
                truncated = True
                break
            items.append(RetrievalItem(
                ref=row["checkpoint_id"],
                source_type=RetrievalType.CHECKPOINT_LOOKUP.value,
                content=content, content_hash=_content_hash(content),
            ))
            total += sz
        return RetrievalResult(
            items=_sorted_items(items), truncated=truncated,
            total_bytes=total, provenance=(RetrievalType.CHECKPOINT_LOOKUP.value,),
        )

    # -- execute ------------------------------------------------------------

    def execute(self, request: RetrievalRequest) -> RetrievalResult:
        """Execute a validated request (bounded, deterministic)."""
        self.validate_request(request)
        max_results, max_bytes, max_excerpt = self._limits(request)
        if request.source_type in (RetrievalType.EXACT_REF,
                                   RetrievalType.FILE_EXCERPT):
            return self._retrieve_exact_or_excerpt(
                request, max_results, max_bytes, max_excerpt)
        if request.source_type is RetrievalType.SYMBOL_OR_TEXT_MATCH:
            return self._retrieve_symbol(
                request, max_results, max_bytes, max_excerpt)
        if request.source_type is RetrievalType.ARTIFACT_LOOKUP:
            return self._retrieve_artifact(
                request, max_results, max_bytes, max_excerpt)
        if request.source_type is RetrievalType.FACT_LOOKUP:
            return self._retrieve_facts(
                request, max_results, max_bytes, max_excerpt)
        if request.source_type is RetrievalType.HANDOFF_LOOKUP:
            return self._retrieve_handoffs(
                request, max_results, max_bytes, max_excerpt)
        if request.source_type is RetrievalType.CHECKPOINT_LOOKUP:
            return self._retrieve_checkpoints(
                request, max_results, max_bytes, max_excerpt)
        raise RetrievalError(RETRIEVAL_INVALID_REQUEST,
                             f"unsupported source_type {request.source_type!r}")


# ---------------------------------------------------------------------------
# Row renderers (store lookups → bounded text)
# ---------------------------------------------------------------------------


def _render_handoff_row(row: dict) -> str:
    """Render a bounded handoff row into a single deterministic text excerpt."""
    import json
    parts = [
        f"handoff {row['handoff_id']}",
        f"role: {row['source_role']}",
        f"outcome: {_json_field(row, 'result_json', 'outcome')}",
        f"observations: {_json_field(row, 'result_json', 'key_observations')}",
        f"next: {_json_field(row, 'next_step_json', 'proposed_capability')}",
    ]
    return "\n".join(parts)


def _render_checkpoint_row(row: dict) -> str:
    import json
    parts = [
        f"checkpoint {row['checkpoint_id']} (#{row['checkpoint_no']})",
        f"state: {_json_field(row, 'workflow_json', 'primary_state')}",
        f"head: {_json_field(row, 'code_json', 'head_commit')}",
    ]
    return "\n".join(parts)


def _json_field(row: dict, col: str, key: str) -> str:
    import json
    try:
        data = json.loads(row.get(col) or "{}")
    except Exception:
        return ""
    value = data.get(key)
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value)
    return str(value)


def _extract_matches(content: str, query: str) -> list:
    """Extract bounded line-based matches for a substring/regex query."""
    lines = content.splitlines()
    matches: list = []
    try:
        rx = re.compile(query)
        for line in lines:
            if rx.search(line):
                matches.append(line)
    except re.error:
        for line in lines:
            if query in line:
                matches.append(line)
    return matches
