"""Phase D3 — bounded artifact-ref hashing (best-effort, never authority).

Shared, bounded filesystem helpers used by (a) the handoff artifact-ref builder
(:func:`argent_core.handoff.build_bounded_artifact_refs`) and (b)
:meth:`argent_core.checkpoint.CheckpointStore.current_facts` so a checkpoint's
declared artifact refs can be hash-verified for stale detection.

Hard invariants (verbindlich, ARGENT V1 FINAL §12/§16):

* **Bounded.**  Every operation caps the number of refs, the per-file hash size
  and the excerpt bytes — there is no unbounded file read, no whole-repo scan.
* **Best-effort, never block.**  A missing/unreadable/oversized/foreign file
  yields ``None`` (no hash) — never a guess, never a fallback to "similar".
* **No shell, no provider, no network.**  Pure ``os``/``hashlib``.
* **Fail-closed.**  The absence of a hash is exactly the signal that
  stale detection uses to refuse a reference (``STALE_CONTEXT_REFERENCE``).
"""

from __future__ import annotations

import hashlib
import os
import stat
from typing import Optional

#: Maximum number of artifact refs processed in one operation.
MAX_ARTIFACT_REFS = 32

#: Maximum bytes read when computing a full-file content hash.  A file larger
#: than this is treated as unhashable (fail-closed: no hash), so a huge file
#: can never trigger unbounded I/O.
MAX_HASH_BYTES = 4 * 1024 * 1024  # 4 MiB

#: Maximum excerpt bytes embedded in a handoff artifact ref.
MAX_EXCERPT_BYTES = 4096

#: Read chunk size for streaming hashes (constant memory).
_HASH_CHUNK = 65536

#: Truncation marker appended to a bounded excerpt that was cut short.
TRUNCATION_MARKER = "\n[TRUNCATED]"

#: Secret/forbidden basename PREFIXES (a declared artifact ref whose final
#: path component starts with one of these is dropped, never hashed/excerpted).
#: ``token*`` is intentionally broad (fail-closed toward dropping).
_FORBIDDEN_BASENAME_PREFIXES = (
    "credentials", "token", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
)

#: Secret/forbidden basename SUFFIXES (``*.pem`` / ``*.key`` / PKCS12 / PKCS#8).
_FORBIDDEN_BASENAME_SUFFIXES = (".pem", ".key", ".p12", ".pfx")

#: Sensitive directory components (matched case-insensitively on any segment).
#: ``.ssh``/``.gnupg``/``.config`` are already covered by the hidden-dot rule
#: below; they are listed here explicitly for policy clarity.
_FORBIDDEN_DIR_NAMES = frozenset({
    ".ssh", ".gnupg", ".config", "secrets", "keyrings",
})


def _clamp_int(value, default: int, hard_max: int) -> int:
    """Clamp a public size/count parameter to ``[1, hard_max]`` (never unbounded).

    A ``bool`` is treated as invalid (it is an ``int`` subclass but never a
    legitimate size/count).  Any non-int/non-positive value falls back to
    ``default``; otherwise ``min(value, hard_max)``.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return default
    return min(value, hard_max)


def is_forbidden_ref(relpath: str) -> bool:
    """Return True if ``relpath`` must never be embedded as an artifact ref.

    Bounded, LOCAL secret/forbidden-path deny-list (same policy as Retrieval's
    root deny-list, applied to declared artifact refs): secret basenames
    (``.env``, ``*.pem``, ``*.key``, ``credentials*``, ``id_rsa``/``id_dsa``/…,
    ``token*``), sensitive directories (``.ssh``, ``.gnupg``, ``.config``,
    ``secrets``, ``keyrings``) and any hidden dot-file/dot-directory component.
    Pure string matching — no shell, no catastrophic regex.
    """
    if not isinstance(relpath, str) or not relpath.strip():
        return True
    norm = relpath.replace("\\", "/")
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if not parts:
        return True
    lower = [p.lower() for p in parts]
    base = lower[-1]
    # Any hidden dot-file/dot-directory component is sensitive (fail-closed).
    for part in lower:
        if part.startswith("."):
            return True
    if base == ".env":
        return True
    if base == "secrets" or base.startswith("secrets."):
        return True
    if base.startswith(_FORBIDDEN_BASENAME_PREFIXES):
        return True
    if base.endswith(_FORBIDDEN_BASENAME_SUFFIXES):
        return True
    for part in lower:
        if part in _FORBIDDEN_DIR_NAMES:
            return True
    return False


def resolve_ref_within(root: Optional[str], relpath: str) -> Optional[str]:
    """Resolve ``relpath`` inside ``root``; ``None`` on escape/foreign path.

    Rejects absolute paths, ``..`` traversal and symlink escapes (realpath
    prefix check).  Returns the canonical absolute path or ``None``.
    """
    if not root or not isinstance(relpath, str) or not relpath.strip():
        return None
    if os.path.isabs(relpath):
        return None
    norm = os.path.normpath(relpath)
    if norm == ".." or norm.startswith(".." + os.sep):
        return None
    try:
        root_real = os.path.realpath(root)
    except (OSError, ValueError):
        return None
    joined = os.path.realpath(os.path.join(root_real, norm))
    if joined == root_real or joined.startswith(root_real + os.sep):
        return joined
    return None


def sha256_file(path: str, *, max_bytes: int = MAX_HASH_BYTES) -> Optional[str]:
    """Stream the sha256 of ``path``; ``None`` if unreadable/oversized/growing.

    Hard-bounded (F2): ``max_bytes`` is clamped to ``MAX_HASH_BYTES``; only
    regular files (``stat.S_ISREG``) are hashed; a byte counter aborts at
    ``max_bytes + 1`` during the read, so a file that GROWS mid-read still
    returns ``None`` (never unbounded I/O).  The hash covers the FULL file
    content (streamed in constant memory), so a changed file yields a different
    hash (the stale-detection basis).
    """
    max_bytes = _clamp_int(max_bytes, MAX_HASH_BYTES, MAX_HASH_BYTES)
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    try:
        if st.st_size > max_bytes:
            return None
    except OSError:
        return None
    h = hashlib.sha256()
    total = 0
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(_HASH_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    # File grew past the cap while we were reading.
                    return None
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def bounded_excerpt(path: str, *, max_bytes: int = MAX_EXCERPT_BYTES) -> str:
    """Read a bounded prefix of ``path`` as text; ``""`` on any failure.

    Hard-bounded (F2): ``max_bytes`` is clamped to ``MAX_EXCERPT_BYTES``; only
    regular files are read; at most ``max_bytes + 1`` bytes are ever read (one
    extra byte only to DETECT truncation), so the read is capped regardless of
    file growth.  A cut-off excerpt is suffixed with ``TRUNCATION_MARKER``.
    Never raises.
    """
    max_bytes = _clamp_int(max_bytes, MAX_EXCERPT_BYTES, MAX_EXCERPT_BYTES)
    try:
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode):
            return ""
    except OSError:
        return ""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(max_bytes + 1)
    except OSError:
        return ""
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    text = raw.decode("utf-8", errors="replace")
    if truncated:
        text += TRUNCATION_MARKER
    return text
