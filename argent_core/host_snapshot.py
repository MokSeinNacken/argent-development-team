"""Phase C1 — host resource snapshot (read-only, injectable, never crashes).

Reads a bounded, deterministic picture of the local host for the Resource
Governor preflight (ARGENT ARCHITECTURE V1 FINAL §9).  This is **evidence
collection only** — it never changes anything and never raises out of a
critical read.

Design (mirrors :class:`argent_core.process_registry.ProcessIdentityProvider`):

* :class:`HostResourceSnapshot` — frozen dataclass of bounded facts.  Any
  critical field that could not be read/parsed is ``None`` AND its name is
  recorded in ``unknown_fields`` (fail-closed UNKNOWN, never guessed).
* :class:`HostSnapshotProvider` — all readers are injectable for tests
  (meminfo / loadavg / mounts / statvfs / cpu_count / active_jobs / psi).
  Defaults read ``/proc/meminfo``, ``/proc/loadavg``, ``/proc/mounts``,
  ``os.statvfs`` for ``/`` and the workspace path, and ``os.cpu_count()``.
* ``snapshot_hash`` — sha256 over a bounded, sorted-JSON canonicalisation of
  the concrete facts, so a decision can be audited against a stable ref.

No secrets, no shell commands, no agent input ever reaches this module.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

#: Critical field names tracked in ``unknown_fields`` when unreadable.
FIELD_MEM_TOTAL = "mem_total_bytes"
FIELD_MEM_AVAILABLE = "mem_available_bytes"
FIELD_SWAP_TOTAL = "swap_total_bytes"
FIELD_SWAP_FREE = "swap_free_bytes"
FIELD_ROOT_FREE = "root_free"
FIELD_WORKSPACE_FREE = "workspace_free"
FIELD_TMP_FS_TYPE = "tmp_fs_type"
FIELD_LOAD = "load"
FIELD_CPU_COUNT = "cpu_count"
FIELD_ACTIVE_JOBS = "active_jobs"

#: The critical field names whose absence forces fail-closed admission.
CRITICAL_FIELDS: frozenset[str] = frozenset({
    FIELD_MEM_TOTAL, FIELD_MEM_AVAILABLE, FIELD_SWAP_TOTAL, FIELD_SWAP_FREE,
    FIELD_ROOT_FREE, FIELD_TMP_FS_TYPE,
})


@dataclass(frozen=True)
class HostResourceSnapshot:
    """Bounded, deterministic host-resource evidence (all-or-None facts)."""

    timestamp: str
    mem_total_bytes: Optional[int]
    mem_available_bytes: Optional[int]
    swap_total_bytes: Optional[int]
    swap_free_bytes: Optional[int]
    root_free_bytes: Optional[int]
    root_free_ratio: Optional[float]
    workspace_free_bytes: Optional[int]
    workspace_free_ratio: Optional[float]
    tmp_fs_type: Optional[str]
    tmp_free_bytes: Optional[int]
    load_1min: Optional[float]
    load_5min: Optional[float]
    load_15min: Optional[float]
    cpu_count: Optional[int]
    #: active jobs as ``(job_id, resource_class)`` tuples (bounded, no secrets).
    active_jobs: tuple = ()
    psi_memory_avg10: Optional[float] = None
    psi_cpu_avg10: Optional[float] = None
    unknown_fields: frozenset = frozenset()
    snapshot_hash: str = ""

    @property
    def is_mem_known(self) -> bool:
        return (
            self.mem_total_bytes is not None
            and self.mem_available_bytes is not None
            and self.swap_total_bytes is not None
            and self.swap_free_bytes is not None
        )


# ---------------------------------------------------------------------------
# Strict parsers (never guess; unparsable -> None)
# ---------------------------------------------------------------------------


def parse_meminfo(text: str) -> Optional[dict]:
    """Parse ``/proc/meminfo``; returns bytes for the tracked keys or ``None``.

    ``None`` is returned only when the WHOLE text is unreadable/empty.  A
    missing tracked key is recorded as ``None`` in the returned mapping (so the
    caller can mark it UNKNOWN) — never guessed.  Values are in kB and are
    converted to bytes.
    """
    if text is None:
        return None
    try:
        text = str(text)
    except Exception:
        return None
    if not text.strip():
        return None
    result: dict = {}
    seen = 0
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            parts = rest.split()
            if not parts:
                result[key] = None
                continue
            try:
                kb = int(parts[0])
            except ValueError:
                result[key] = None
                continue
            result[key] = kb * 1024
            seen += 1
    if seen == 0:
        return None
    result.setdefault("MemTotal", None)
    result.setdefault("MemAvailable", None)
    result.setdefault("SwapTotal", None)
    result.setdefault("SwapFree", None)
    return result


def parse_loadavg(text: str) -> Optional[tuple]:
    """Parse ``/proc/loadavg`` -> ``(load1, load5, load15)`` or ``None``."""
    if text is None:
        return None
    try:
        text = str(text)
    except Exception:
        return None
    parts = text.split()
    if len(parts) < 3:
        return None
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except ValueError:
        return None


def parse_mounts(text: str) -> Optional[dict]:
    """Parse ``/proc/mounts`` -> ``{mountpoint: fstype}`` (or ``None``)."""
    if text is None:
        return None
    try:
        text = str(text)
    except Exception:
        return None
    if not text.strip():
        return None
    mounts: dict = {}
    seen = 0
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        # fields: device, mountpoint, fstype, options...
        device, mountpoint, fstype = fields[0], fields[1], fields[2]
        # Decode octal escapes (e.g. "\040" for space) in the mountpoint.
        try:
            mountpoint = bytes(mountpoint, "utf-8").decode("unicode_escape")
        except Exception:
            pass
        mounts[mountpoint] = fstype
        seen += 1
    if seen == 0:
        return None
    return mounts


def statvfs_free(path: str) -> Optional[tuple]:
    """Return ``(free_bytes, free_ratio)`` for a path via ``os.statvfs``.

    Returns ``None`` on any failure (missing path, permission, not a real fs).
    """
    try:
        st = os.statvfs(path)
    except (OSError, ValueError, TypeError):
        return None
    free_bytes = st.f_bavail * st.f_frsize
    total = st.f_blocks * st.f_frsize
    if total <= 0:
        return (int(free_bytes), None)
    ratio = free_bytes / total
    return (int(free_bytes), float(ratio))


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


def _canonical_snapshot_facts(facts: dict) -> str:
    """Bounded, deterministic canonical JSON of the snapshot facts."""
    return json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str)


def _snapshot_hash(facts: dict) -> str:
    return hashlib.sha256(_canonical_snapshot_facts(facts).encode("utf-8")).hexdigest()


class HostSnapshotProvider:
    """Captures host-resource evidence; every reader is injectable for tests.

    Critical read/parse failures record the field as UNKNOWN (``None`` +
    ``unknown_fields``) — the provider never raises out of a critical read and
    never guesses a value.  ``psi`` is optional: failures are silently dropped
    (field stays ``None``).
    """

    def __init__(
        self,
        *,
        meminfo_reader: Optional[Callable[[], Optional[str]]] = None,
        loadavg_reader: Optional[Callable[[], Optional[str]]] = None,
        mounts_reader: Optional[Callable[[], Optional[str]]] = None,
        statvfs_reader: Optional[Callable[[str], Optional[tuple]]] = None,
        cpu_count_reader: Optional[Callable[[], Optional[int]]] = None,
        active_jobs_reader: Optional[Callable[[], Optional[Sequence]]] = None,
        psi_reader: Optional[Callable[[], Optional[dict]]] = None,
        meminfo_path: str = "/proc/meminfo",
        loadavg_path: str = "/proc/loadavg",
        mounts_path: str = "/proc/mounts",
        tmp_mountpoint: str = "/tmp",
        root_mountpoint: str = "/",
    ):
        self._meminfo_reader = meminfo_reader
        self._loadavg_reader = loadavg_reader
        self._mounts_reader = mounts_reader
        self._statvfs_reader = statvfs_reader
        self._cpu_count_reader = cpu_count_reader
        self._active_jobs_reader = active_jobs_reader
        self._psi_reader = psi_reader
        self._meminfo_path = meminfo_path
        self._loadavg_path = loadavg_path
        self._mounts_path = mounts_path
        self._tmp_mountpoint = tmp_mountpoint
        self._root_mountpoint = root_mountpoint

    # -- default readers ----------------------------------------------------

    def _read_file(self, path: str) -> Optional[str]:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except (OSError, ValueError):
            return None

    def _default_meminfo(self) -> Optional[str]:
        return self._read_file(self._meminfo_path)

    def _default_loadavg(self) -> Optional[str]:
        return self._read_file(self._loadavg_path)

    def _default_mounts(self) -> Optional[str]:
        return self._read_file(self._mounts_path)

    def _default_statvfs(self, path: str) -> Optional[tuple]:
        return statvfs_free(path)

    def _default_cpu_count(self) -> Optional[int]:
        try:
            n = os.cpu_count()
        except Exception:
            return None
        return n if isinstance(n, int) and n > 0 else None

    def _default_active_jobs(self) -> Optional[Sequence]:
        # Unreadable active-job evidence -> UNKNOWN (fail-closed), never an
        # empty set (an empty set would wrongly authorise concurrency).
        return None

    def _default_psi(self) -> Optional[dict]:
        return None

    @staticmethod
    def _enclosing_fs_type(mounts: dict, path: str) -> Optional[str]:
        """Resolve the filesystem type of the longest mountpoint enclosing
        ``path`` (F4).

        A host without a dedicated ``/tmp`` mount (``/tmp`` lives on the root
        filesystem) must resolve to the root filesystem's type (e.g. ``ext4``)
        rather than ``None``, so it is not misclassified as UNKNOWN.  Exact
        match wins, otherwise the longest path-prefix mountpoint (``/tmp``,
        then ``/``).  Nested mounts (e.g. ``/tmp/sub``) are honoured by prefix
        length.
        """
        if not isinstance(path, str) or not path:
            return None
        norm = path.rstrip("/") or "/"
        best_len = -1
        best = None
        for mp, fstype in (mounts or {}).items():
            if not isinstance(mp, str):
                continue
            m = mp.rstrip("/") or "/"
            if m == norm:
                return fstype
            # ``m`` encloses ``norm`` when equal or when ``norm`` starts with
            # ``m + "/"``; the root mountpoint ``/`` encloses every absolute path.
            if m == "/":
                enclosing = norm.startswith("/")
            else:
                enclosing = norm.startswith(m + "/")
            if enclosing and len(m) > best_len:
                best_len = len(m)
                best = fstype
        return best

    # -- capture -------------------------------------------------------------

    def capture(self, workspace_path: Optional[str] = None) -> HostResourceSnapshot:
        """Capture a bounded host snapshot.  Never raises (critical reads fail
        closed to UNKNOWN)."""
        unknown: set = set()

        # meminfo
        meminfo = None
        try:
            raw = self._meminfo_reader() if self._meminfo_reader else self._default_meminfo()
            meminfo = parse_meminfo(raw)
        except Exception:
            meminfo = None
        mem_total = mem_available = swap_total = swap_free = None
        if meminfo is not None:
            mem_total = meminfo.get("MemTotal")
            mem_available = meminfo.get("MemAvailable")
            swap_total = meminfo.get("SwapTotal")
            swap_free = meminfo.get("SwapFree")
        if mem_total is None:
            unknown.add(FIELD_MEM_TOTAL)
        if mem_available is None:
            unknown.add(FIELD_MEM_AVAILABLE)
        if swap_total is None:
            unknown.add(FIELD_SWAP_TOTAL)
        if swap_free is None:
            unknown.add(FIELD_SWAP_FREE)

        # loadavg
        load1 = load5 = load15 = None
        try:
            raw = self._loadavg_reader() if self._loadavg_reader else self._default_loadavg()
            parsed = parse_loadavg(raw)
        except Exception:
            parsed = None
        if parsed is not None:
            load1, load5, load15 = parsed
        if load5 is None:
            unknown.add(FIELD_LOAD)

        # mounts -> tmp fstype (longest enclosing mountpoint for /tmp).
        tmp_fs_type = None
        try:
            raw = self._mounts_reader() if self._mounts_reader else self._default_mounts()
            mounts = parse_mounts(raw)
        except Exception:
            mounts = None
        if mounts is not None:
            tmp_fs_type = self._enclosing_fs_type(mounts, self._tmp_mountpoint)
        if tmp_fs_type is None:
            unknown.add(FIELD_TMP_FS_TYPE)

        # statvfs for root + workspace (injectable reader or default)
        svf = self._statvfs_reader or self._default_statvfs
        root_free_bytes = root_free_ratio = None
        try:
            pair = svf(self._root_mountpoint)
        except Exception:
            pair = None
        if pair is not None:
            root_free_bytes, root_free_ratio = pair
        if root_free_bytes is None:
            unknown.add(FIELD_ROOT_FREE)

        workspace_free_bytes = workspace_free_ratio = None
        if workspace_path:
            try:
                pair = svf(workspace_path)
            except Exception:
                pair = None
            if pair is not None:
                workspace_free_bytes, workspace_free_ratio = pair
        if workspace_free_bytes is None and workspace_path:
            unknown.add(FIELD_WORKSPACE_FREE)

        # tmp free (best effort, optional — not critical on its own)
        tmp_free_bytes = None
        try:
            pair = svf(self._tmp_mountpoint)
        except Exception:
            pair = None
        if pair is not None:
            tmp_free_bytes = pair[0]

        # cpu_count
        cpu_count = None
        try:
            cpu_count = (
                self._cpu_count_reader()
                if self._cpu_count_reader else self._default_cpu_count()
            )
        except Exception:
            cpu_count = None
        if cpu_count is None:
            unknown.add(FIELD_CPU_COUNT)

        # active jobs
        active_jobs: tuple = ()
        aj = None
        try:
            aj = (
                self._active_jobs_reader()
                if self._active_jobs_reader else self._default_active_jobs()
            )
        except Exception:
            aj = None
        if aj is None:
            # Unreadable active-job evidence -> UNKNOWN (fail-closed), never an
            # empty set (an empty set would wrongly authorise concurrency).
            unknown.add(FIELD_ACTIVE_JOBS)
        else:
            active_jobs = tuple(tuple(item) for item in aj)

        # psi (optional)
        psi_mem = psi_cpu = None
        if self._psi_reader is not None:
            try:
                psi = self._psi_reader()
            except Exception:
                psi = None
            if psi:
                try:
                    psi_mem = psi.get("memory_avg10")
                    psi_cpu = psi.get("cpu_avg10")
                except Exception:
                    psi_mem = psi_cpu = None

        timestamp = datetime.now(timezone.utc).isoformat()
        facts = {
            "mem_total_bytes": mem_total,
            "mem_available_bytes": mem_available,
            "swap_total_bytes": swap_total,
            "swap_free_bytes": swap_free,
            "root_free_bytes": root_free_bytes,
            "workspace_free_bytes": workspace_free_bytes,
            "tmp_fs_type": tmp_fs_type,
            "load_1min": load1,
            "load_5min": load5,
            "cpu_count": cpu_count,
            "active_jobs": active_jobs,
        }
        snapshot_hash = _snapshot_hash(facts)

        return HostResourceSnapshot(
            timestamp=timestamp,
            mem_total_bytes=mem_total,
            mem_available_bytes=mem_available,
            swap_total_bytes=swap_total,
            swap_free_bytes=swap_free,
            root_free_bytes=root_free_bytes,
            root_free_ratio=root_free_ratio,
            workspace_free_bytes=workspace_free_bytes,
            workspace_free_ratio=workspace_free_ratio,
            tmp_fs_type=tmp_fs_type,
            tmp_free_bytes=tmp_free_bytes,
            load_1min=load1,
            load_5min=load5,
            load_15min=load15,
            cpu_count=cpu_count,
            active_jobs=active_jobs,
            psi_memory_avg10=psi_mem,
            psi_cpu_avg10=psi_cpu,
            unknown_fields=frozenset(unknown),
            snapshot_hash=snapshot_hash,
        )
