"""Shared deterministic helpers for the Phase C1 resource-governor tests.

Builds :class:`HostResourceSnapshot` fakes from pure numbers — no real host
reads, no RAM/swap/disk/CPU stress.  All values are explicit so every admission
decision is fully deterministic.
"""

from __future__ import annotations

from argent_core.host_snapshot import HostResourceSnapshot, _snapshot_hash
from argent_core.resource_policy import gib

_UNSET = object()


def make_snapshot(
    *,
    mem_total: int = _UNSET,
    mem_available: int = _UNSET,
    swap_total: int = _UNSET,
    swap_free: int = _UNSET,
    root_free: int = _UNSET,
    root_free_ratio: float = 0.5,
    workspace_free: int = _UNSET,
    workspace_free_ratio: float = 0.5,
    tmp_fs_type: str = "tmpfs",
    tmp_free: int = _UNSET,
    load1: float = 1.0,
    load5: float = 1.0,
    load15: float = 1.0,
    cpu_count: int = 8,
    active_jobs: tuple = (),
    unknown_fields: frozenset = None,
    psi_mem: float = None,
    psi_cpu: float = None,
) -> HostResourceSnapshot:
    """A healthy-host snapshot with explicit overrides.

    Defaults describe a comfortable 8 GiB host (mem_available 6 GiB, swap idle,
    100 GiB free disk, tmpfs /tmp, cpu 8, no active jobs, load ~1).  Pass ``None``
    explicitly to mark a field UNKNOWN.
    """
    mem_total = gib(8) if mem_total is _UNSET else mem_total
    mem_available = gib(6) if mem_available is _UNSET else mem_available
    swap_total = gib(2) if swap_total is _UNSET else swap_total
    swap_free = gib(2) if swap_free is _UNSET else swap_free
    root_free = gib(100) if root_free is _UNSET else root_free
    workspace_free = gib(100) if workspace_free is _UNSET else workspace_free
    tmp_free = gib(1) if tmp_free is _UNSET else tmp_free

    facts = {
        "mem_total_bytes": mem_total,
        "mem_available_bytes": mem_available,
        "swap_total_bytes": swap_total,
        "swap_free_bytes": swap_free,
        "root_free_bytes": root_free,
        "workspace_free_bytes": workspace_free,
        "tmp_fs_type": tmp_fs_type,
        "load_1min": load1,
        "load_5min": load5,
        "cpu_count": cpu_count,
        "active_jobs": tuple(tuple(a) for a in active_jobs),
    }

    return HostResourceSnapshot(
        timestamp="2026-09-01T00:00:00+00:00",
        mem_total_bytes=mem_total,
        mem_available_bytes=mem_available,
        swap_total_bytes=swap_total,
        swap_free_bytes=swap_free,
        root_free_bytes=root_free,
        root_free_ratio=root_free_ratio,
        workspace_free_bytes=workspace_free,
        workspace_free_ratio=workspace_free_ratio,
        tmp_fs_type=tmp_fs_type,
        tmp_free_bytes=tmp_free,
        load_1min=load1,
        load_5min=load5,
        load_15min=load15,
        cpu_count=cpu_count,
        active_jobs=tuple(tuple(a) for a in active_jobs),
        psi_memory_avg10=psi_mem,
        psi_cpu_avg10=psi_cpu,
        unknown_fields=frozenset() if unknown_fields is None else frozenset(unknown_fields),
        snapshot_hash=_snapshot_hash(facts),
    )
