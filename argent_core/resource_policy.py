"""Phase C1 — resource policy (decision basis ONLY, no enforcement).

This module is the pure, deterministic policy authority for the Resource
Governor (ARGENT ARCHITECTURE V1 FINAL §9).  It defines:

* :class:`ResourceClass` — the exact 4 resource classes (``LIGHT`` /
  ``MEDIUM`` / ``HEAVY`` / ``EXCLUSIVE``).
* :class:`ResourceLimits` — the per-class ceilings (memory high/max, swap,
  CPU quota, default timeout).  These are **ceilings** (upper bounds for the
  C2 cgroup/systemd-scope enforcement), never guarantees.
* :class:`ResourcePolicy` — the versioned, frozen policy with the host-reserve,
  swap, disk, tmpfs and concurrency defaults.  A version string
  (``policy_version``) is persisted with every admission decision so a later
  policy change is auditable.
* pure helpers ``required_host_reserve`` and ``effective_memory_max``.

This module performs **no I/O, no shell commands and no host reads** — it is a
pure function of its inputs, so it is trivially testable and cannot be
influenced by untrusted agent output.  All values are in bytes (``int``) except
ratios (``float``) and counts/percentages (``int``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

# -- byte helpers (no I/O, deterministic) ------------------------------------

_MIB = 1024 * 1024
_GIB = 1024 * _MIB


def mib(n: int) -> int:
    """Return ``n`` MiB in bytes."""
    return int(n) * _MIB


def gib(n: float) -> int:
    """Return ``n`` GiB in bytes (fractional GiB allowed, e.g. ``gib(4.5)``)."""
    return int(float(n) * _GIB)


class ResourceClass(str, Enum):
    """The exact 4 resource classes (§9).  Unknown values raise ``ValueError``."""

    LIGHT = "LIGHT"
    MEDIUM = "MEDIUM"
    HEAVY = "HEAVY"
    EXCLUSIVE = "EXCLUSIVE"


#: Canonical order for the DB CHECK constraint (deterministic DDL string).
RESOURCE_CLASS_VALUES: tuple[str, ...] = tuple(c.value for c in ResourceClass)

#: Classes that count as a "writer" for the global single-writer invariant.
_WRITER_CLASSES: frozenset[str] = frozenset({
    ResourceClass.MEDIUM.value,
    ResourceClass.HEAVY.value,
    ResourceClass.EXCLUSIVE.value,
})


@dataclass(frozen=True)
class ResourceLimits:
    """Per-class ceilings (§9).  Ceilings, never guaranteed allocations."""

    memory_high_bytes: int
    memory_max_bytes: int
    swap_max_bytes: int
    cpu_quota_percent: int
    #: Default wall-clock timeout; ``None`` means "step-specific / no default".
    timeout_seconds: Optional[int]


@dataclass(frozen=True)
class ResourcePolicy:
    """Versioned, frozen resource policy (defaults per §9).

    All ceilings/bytes are ``int``; ratios are ``float`` in ``(0, 1]``;
    counts/percentages are ``int``.  A later policy version bumps
    ``policy_version`` and is persisted with each decision.
    """

    policy_version: str = "1"

    # -- per-class ceilings (§9 table) -------------------------------------
    light_limits: ResourceLimits = ResourceLimits(
        memory_high_bytes=mib(768),
        memory_max_bytes=gib(1),
        swap_max_bytes=mib(256),
        cpu_quota_percent=100,
        timeout_seconds=15 * 60,
    )
    medium_limits: ResourceLimits = ResourceLimits(
        memory_high_bytes=gib(2),
        memory_max_bytes=gib(2.5),
        swap_max_bytes=mib(512),
        cpu_quota_percent=200,
        timeout_seconds=45 * 60,
    )
    heavy_limits: ResourceLimits = ResourceLimits(
        memory_high_bytes=gib(3),
        memory_max_bytes=gib(4),
        swap_max_bytes=gib(1),
        cpu_quota_percent=300,
        timeout_seconds=120 * 60,
    )
    exclusive_limits: ResourceLimits = ResourceLimits(
        memory_high_bytes=gib(4.5),
        memory_max_bytes=gib(5.5),
        swap_max_bytes=gib(1.5),
        cpu_quota_percent=400,
        timeout_seconds=None,  # step-specific
    )

    # -- host reserve (§9) ---------------------------------------------------
    minimum_host_reserve_bytes: int = gib(1.5)
    host_reserve_ram_ratio: float = 0.20

    # -- swap thresholds (§9) ------------------------------------------------
    swap_warning_ratio: float = 0.70
    swap_block_ratio: float = 0.85

    # -- disk thresholds (§9) ------------------------------------------------
    minimum_disk_free_bytes: int = gib(10)
    minimum_disk_free_ratio: float = 0.15
    #: persistent free storage required for large temporary data (factor 2).
    large_temp_factor: float = 2.0

    # -- /tmp policy (§9/§17) -------------------------------------------------
    #: Path segment markers that must never live in /tmp (repos, node_modules,
    #: package stores, build caches, large artifacts).  Small bounded /tmp
    #: files remain allowed.
    tmp_forbidden_markers: tuple[str, ...] = (
        "node_modules", "site-packages", ".venv", "venv", "env",
        "repo", "worktree", "build", "dist", "cache", "artifact",
        "store", ".pnpm-store", ".npm", ".cargo", ".yarn", "target",
    )

    # -- concurrency limits (§9/§14) -----------------------------------------
    max_writers_global: int = 1
    max_light: int = 2
    max_medium: int = 1
    max_heavy: int = 1

    # -- retry / load ----------------------------------------------------------
    #: bounded ``next_eligible_at`` deferral on DEFER decisions.
    defer_retry_seconds: int = 300
    #: load-5min above ``cpu_count * load_multiplier_5min`` defers MEDIUM+.
    load_multiplier_5min: float = 1.5

    # -- accessors -----------------------------------------------------------

    def limits_for(self, resource_class: ResourceClass) -> ResourceLimits:
        """Return the ceiling set for a resource class."""
        return {
            ResourceClass.LIGHT: self.light_limits,
            ResourceClass.MEDIUM: self.medium_limits,
            ResourceClass.HEAVY: self.heavy_limits,
            ResourceClass.EXCLUSIVE: self.exclusive_limits,
        }[resource_class]

    def is_writer_class(self, resource_class: ResourceClass) -> bool:
        """True when the class counts against the global single-writer limit."""
        return resource_class.value in _WRITER_CLASSES


# -- pure helpers -------------------------------------------------------------


def required_host_reserve(
    mem_total_bytes: int,
    *,
    minimum_host_reserve_bytes: int = gib(1.5),
    host_reserve_ram_ratio: float = 0.20,
) -> int:
    """The host-reserve floor: ``max(1.5 GiB, 20% of total RAM)`` (§9).

    A pure function of ``mem_total_bytes``; larger of the absolute floor and
    the ratio-based reserve wins.  ``mem_total_bytes <= 0`` falls back to the
    absolute floor (never a negative reserve).
    """
    if mem_total_bytes <= 0:
        return int(minimum_host_reserve_bytes)
    ratio_reserve = int(host_reserve_ram_ratio * mem_total_bytes)
    return max(int(minimum_host_reserve_bytes), ratio_reserve)


def effective_memory_max(
    class_memory_max_bytes: int,
    mem_available_bytes: int,
    reserve_bytes: int,
) -> int:
    """Effective MemoryMax proposal for C2: ``min(ceiling, avail - reserve)``.

    Floored at 0 (never negative).  Pure function; no enforcement here.
    """
    headroom = int(mem_available_bytes) - int(reserve_bytes)
    return max(0, min(int(class_memory_max_bytes), headroom))
