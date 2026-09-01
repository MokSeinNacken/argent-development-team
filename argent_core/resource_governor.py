"""Phase C1 — Resource Governor: safe admission decision basis ONLY.

This module decides **whether a job may be locally admitted** given a host
snapshot and the local policy.  It is the *decision basis* of the Resource
Governor (ARGENT ARCHITECTURE V1 FINAL §9) — **not** execution enforcement.
cgroup/systemd-run/``prlimit`` process limits, kill and OOM-recovery are all
Phase C2 and are deliberately NOT implemented here.

Key properties:

* :class:`ResourceReasonCode` — the exact, bounded reason codes.
* :class:`AdmissionDecision` — frozen decision (verdict + reason + bounded
  proposal for C2 limits + timestamp + snapshot ref).
* :class:`ResourceGovernor.decide` — pure function of (resource_class,
  snapshot, policy, now_iso, prefer_external_ci, estimated_temp_bytes,
  tmp_paths).  All parameters come from trusted local policy / snapshot /
  supervisor context; agent output can NEVER determine class/limits/reason.

Decision logic (order matters; first match wins):

1. evidence-unknown (fail-closed),
2. concurrency limits (incl. global single-writer),
3. swap pressure,
4. disk low / large-temp factor-2,
5. tmpfs policy violation,
6. host-reserve (the core rule),
7. load pressure,
8. PREFER_EXTERNAL (routing hint only, no external action),
9. otherwise ALLOW + OK.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional, Sequence

from .host_snapshot import FIELD_ACTIVE_JOBS, HostResourceSnapshot
from .resource_policy import (
    ResourceClass,
    ResourcePolicy,
    effective_memory_max,
    required_host_reserve,
)


class ResourceReasonCode(str, Enum):
    """Bounded, exact reason codes for admission decisions."""

    OK = "OK"
    INSUFFICIENT_MEMORY_RESERVE = "INSUFFICIENT_MEMORY_RESERVE"
    SWAP_PRESSURE = "SWAP_PRESSURE"
    DISK_LOW = "DISK_LOW"
    TMPFS_POLICY_VIOLATION = "TMPFS_POLICY_VIOLATION"
    LOAD_PRESSURE = "LOAD_PRESSURE"
    CONCURRENCY_LIMIT = "CONCURRENCY_LIMIT"
    RESOURCE_EVIDENCE_UNKNOWN = "RESOURCE_EVIDENCE_UNKNOWN"
    LOCAL_CAPACITY_INSUFFICIENT = "LOCAL_CAPACITY_INSUFFICIENT"
    EXTERNAL_CI_PREFERRED = "EXTERNAL_CI_PREFERRED"
    # C2: execution enforcement (systemd-run --scope / cgroup) was unavailable
    # or could not be proven.  Fail-closed RESOURCE outcome — never CODE_FAILURE.
    RESOURCE_ENFORCEMENT_UNAVAILABLE = "RESOURCE_ENFORCEMENT_UNAVAILABLE"


class AdmissionVerdict(str, Enum):
    """The four admission outcomes.  PREFER_EXTERNAL is a routing hint only."""

    ALLOW = "ALLOW"
    DEFER = "DEFER"
    DENY_LOCAL = "DENY_LOCAL"
    PREFER_EXTERNAL = "PREFER_EXTERNAL"


@dataclass(frozen=True)
class AdmissionDecision:
    """A single admission decision (bounded; no secrets)."""

    resource_class: str
    policy_version: str
    snapshot_ref: str
    decision: str  # AdmissionVerdict value
    reason_code: str  # ResourceReasonCode value
    next_eligible_at: Optional[str] = None  # only for DEFER
    effective_limits: dict = None  # C2 proposal (memory_high/max, swap, cpu, timeout)
    timestamp: str = ""

    def __post_init__(self) -> None:
        if self.effective_limits is None:
            object.__setattr__(self, "effective_limits", {})


def _now_iso_default() -> str:
    return datetime.now(timezone.utc).isoformat()


def _add_seconds_iso(now_iso: Optional[str], seconds: int) -> str:
    """Add ``seconds`` to an ISO timestamp, returning a deterministic ISO string."""
    if seconds <= 0:
        return now_iso or _now_iso_default()
    try:
        dt = datetime.fromisoformat(now_iso)
    except (ValueError, TypeError):
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def _is_tmp_path_forbidden(path: str, policy: ResourcePolicy) -> bool:
    """True when ``path`` is under ``/tmp`` and carries a forbidden marker."""
    if not isinstance(path, str):
        return False
    norm = path.strip().replace("\\", "/").rstrip("/")
    if not norm:
        return False
    if norm == "/tmp" or norm.startswith("/tmp/"):
        for part in norm.split("/"):
            if part.lower() in policy.tmp_forbidden_markers:
                return True
    return False


class ResourceGovernor:
    """Decides local admission from trusted local policy + snapshot evidence."""

    def __init__(self, policy: Optional[ResourcePolicy] = None):
        self._policy = policy or ResourcePolicy()

    @property
    def policy(self) -> ResourcePolicy:
        return self._policy

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _active_counts(active_jobs) -> dict:
        counts = {c.value: 0 for c in ResourceClass}
        for item in active_jobs or ():
            try:
                cls = item[1] if isinstance(item, (tuple, list)) else None
            except (IndexError, TypeError):
                continue
            if cls in counts:
                counts[cls] += 1
        return counts

    def _missing_critical(self, snapshot: HostResourceSnapshot,
                          resource_class: ResourceClass) -> list:
        missing = []
        if snapshot.mem_total_bytes is None:
            missing.append("mem_total_bytes")
        if snapshot.mem_available_bytes is None:
            missing.append("mem_available_bytes")
        if snapshot.swap_total_bytes is None:
            missing.append("swap_total_bytes")
        if snapshot.swap_free_bytes is None:
            missing.append("swap_free_bytes")
        if snapshot.root_free_bytes is None:
            missing.append("root_free")
        if snapshot.tmp_fs_type is None:
            missing.append("tmp_fs_type")
        if FIELD_ACTIVE_JOBS in snapshot.unknown_fields:
            missing.append(FIELD_ACTIVE_JOBS)
        if resource_class in (ResourceClass.MEDIUM, ResourceClass.HEAVY,
                              ResourceClass.EXCLUSIVE):
            if snapshot.load_5min is None:
                missing.append("load")
            if snapshot.cpu_count is None:
                missing.append("cpu_count")
        return missing

    def _effective_limits(
        self, resource_class: ResourceClass, snapshot: HostResourceSnapshot,
        reserve: Optional[int],
    ) -> dict:
        limits = self._policy.limits_for(resource_class)
        memory_max = limits.memory_max_bytes
        if (
            reserve is not None
            and snapshot.mem_available_bytes is not None
        ):
            memory_max = effective_memory_max(
                limits.memory_max_bytes, snapshot.mem_available_bytes, reserve,
            )
        return {
            "memory_high_bytes": limits.memory_high_bytes,
            "memory_max_bytes": memory_max,
            "swap_max_bytes": limits.swap_max_bytes,
            "cpu_quota_percent": limits.cpu_quota_percent,
            "timeout_seconds": limits.timeout_seconds,
        }

    def _decide(
        self,
        *,
        resource_class: ResourceClass,
        snapshot: HostResourceSnapshot,
        policy: ResourcePolicy,
        now_iso: Optional[str],
        prefer_external_ci: bool,
        estimated_temp_bytes: Optional[int],
        tmp_paths: Optional[Sequence[str]],
    ) -> AdmissionDecision:
        verdict = AdmissionVerdict.DENY_LOCAL
        reason = ResourceReasonCode.LOCAL_CAPACITY_INSUFFICIENT
        next_eligible_at: Optional[str] = None
        effective_limits: dict = {}

        # 1. evidence-unknown (fail-closed).
        missing = self._missing_critical(snapshot, resource_class)
        if missing:
            reason = ResourceReasonCode.RESOURCE_EVIDENCE_UNKNOWN
            if resource_class in (ResourceClass.MEDIUM, ResourceClass.HEAVY,
                                  ResourceClass.EXCLUSIVE):
                verdict = AdmissionVerdict.DENY_LOCAL
            else:
                # LIGHT: conservative defer (reserve not provable -> no start).
                verdict = AdmissionVerdict.DEFER
                next_eligible_at = _add_seconds_iso(
                    now_iso, policy.defer_retry_seconds,
                )
            return AdmissionDecision(
                resource_class=resource_class.value,
                policy_version=policy.policy_version,
                snapshot_ref=snapshot.snapshot_hash,
                decision=verdict.value,
                reason_code=reason.value,
                next_eligible_at=next_eligible_at,
                effective_limits={},
                timestamp=now_iso or _now_iso_default(),
            )

        counts = self._active_counts(snapshot.active_jobs)

        # 2. concurrency limits (C1 §9 "Host exklusiv"):
        #   * EXCLUSIVE is host-exclusive: blocked by ANY active job.
        #   * any candidate (incl. LIGHT) is blocked by an active EXCLUSIVE.
        #   * MEDIUM/HEAVY/EXCLUSIVE are writers: blocked by any active writer.
        #   * LIGHT is bounded by ``max_light`` (active LIGHT count).
        writer_active = (
            counts[ResourceClass.MEDIUM.value]
            + counts[ResourceClass.HEAVY.value]
            + counts[ResourceClass.EXCLUSIVE.value]
        )
        is_writer = policy.is_writer_class(resource_class)

        def _defer_concurrency() -> AdmissionDecision:
            return AdmissionDecision(
                resource_class=resource_class.value,
                policy_version=policy.policy_version,
                snapshot_ref=snapshot.snapshot_hash,
                decision=AdmissionVerdict.DEFER.value,
                reason_code=ResourceReasonCode.CONCURRENCY_LIMIT.value,
                next_eligible_at=_add_seconds_iso(now_iso, policy.defer_retry_seconds),
                effective_limits={},
                timestamp=now_iso or _now_iso_default(),
            )

        if resource_class == ResourceClass.EXCLUSIVE:
            if writer_active + counts[ResourceClass.LIGHT.value] > 0:
                return _defer_concurrency()
        if counts[ResourceClass.EXCLUSIVE.value] > 0:
            return _defer_concurrency()
        if is_writer and writer_active >= policy.max_writers_global:
            return _defer_concurrency()
        if resource_class == ResourceClass.LIGHT and \
                counts[ResourceClass.LIGHT.value] >= policy.max_light:
            return _defer_concurrency()

        # 3. swap pressure.
        swap_ratio = None
        if snapshot.swap_total_bytes is not None and snapshot.swap_total_bytes > 0:
            swap_used = snapshot.swap_total_bytes - (snapshot.swap_free_bytes or 0)
            swap_ratio = swap_used / snapshot.swap_total_bytes
        if swap_ratio is not None:
            if swap_ratio >= policy.swap_block_ratio and is_writer:
                verdict, reason = AdmissionVerdict.DEFER, ResourceReasonCode.SWAP_PRESSURE
                next_eligible_at = _add_seconds_iso(now_iso, policy.defer_retry_seconds)
                return AdmissionDecision(
                    resource_class=resource_class.value,
                    policy_version=policy.policy_version,
                    snapshot_ref=snapshot.snapshot_hash,
                    decision=verdict.value, reason_code=reason.value,
                    next_eligible_at=next_eligible_at, effective_limits={},
                    timestamp=now_iso or _now_iso_default(),
                )
            if swap_ratio >= policy.swap_warning_ratio and resource_class in (
                ResourceClass.MEDIUM, ResourceClass.HEAVY, ResourceClass.EXCLUSIVE,
            ):
                verdict, reason = AdmissionVerdict.DEFER, ResourceReasonCode.SWAP_PRESSURE
                next_eligible_at = _add_seconds_iso(now_iso, policy.defer_retry_seconds)
                return AdmissionDecision(
                    resource_class=resource_class.value,
                    policy_version=policy.policy_version,
                    snapshot_ref=snapshot.snapshot_hash,
                    decision=verdict.value, reason_code=reason.value,
                    next_eligible_at=next_eligible_at, effective_limits={},
                    timestamp=now_iso or _now_iso_default(),
                )

        # 4. disk low + large-temp factor-2.
        # The persistent-storage threshold (10 GiB / 15%) applies to the ROOT
        # filesystem (the durable workspace lives on it in production).  The
        # workspace free-space is only used for the factor-2 large-temp rule
        # below — NOT for the threshold, because a test/sandbox workspace may
        # legitimately live under /tmp (tmpfs), which is governed by the tmpfs
        # policy (step 5), not the persistent-disk policy.
        disk_threshold = policy.minimum_disk_free_bytes
        disk_low = False
        if snapshot.root_free_bytes is not None:
            if snapshot.root_free_bytes < disk_threshold:
                disk_low = True
            if snapshot.root_free_ratio is not None and \
                    snapshot.root_free_ratio < policy.minimum_disk_free_ratio:
                disk_low = True
        if estimated_temp_bytes is not None:
            required_persistent = int(int(estimated_temp_bytes) * policy.large_temp_factor)
            if snapshot.workspace_free_bytes is not None and \
                    snapshot.workspace_free_bytes < required_persistent:
                disk_low = True
        if disk_low:
            verdict, reason = AdmissionVerdict.DENY_LOCAL, ResourceReasonCode.DISK_LOW
            return AdmissionDecision(
                resource_class=resource_class.value,
                policy_version=policy.policy_version,
                snapshot_ref=snapshot.snapshot_hash,
                decision=verdict.value, reason_code=reason.value,
                next_eligible_at=None, effective_limits={},
                timestamp=now_iso or _now_iso_default(),
            )

        # 5. tmpfs policy violation.
        if snapshot.tmp_fs_type == "tmpfs":
            for p in tmp_paths or ():
                if _is_tmp_path_forbidden(p, policy):
                    verdict, reason = (
                        AdmissionVerdict.DENY_LOCAL,
                        ResourceReasonCode.TMPFS_POLICY_VIOLATION,
                    )
                    return AdmissionDecision(
                        resource_class=resource_class.value,
                        policy_version=policy.policy_version,
                        snapshot_ref=snapshot.snapshot_hash,
                        decision=verdict.value, reason_code=reason.value,
                        next_eligible_at=None, effective_limits={},
                        timestamp=now_iso or _now_iso_default(),
                    )

        # 6. host reserve (core rule).
        reserve = required_host_reserve(
            snapshot.mem_total_bytes,
            minimum_host_reserve_bytes=policy.minimum_host_reserve_bytes,
            host_reserve_ram_ratio=policy.host_reserve_ram_ratio,
        )
        limits = policy.limits_for(resource_class)
        if snapshot.mem_available_bytes is not None:
            if snapshot.mem_available_bytes - limits.memory_max_bytes < reserve:
                verdict, reason = (
                    AdmissionVerdict.DEFER,
                    ResourceReasonCode.INSUFFICIENT_MEMORY_RESERVE,
                )
                next_eligible_at = _add_seconds_iso(now_iso, policy.defer_retry_seconds)
                return AdmissionDecision(
                    resource_class=resource_class.value,
                    policy_version=policy.policy_version,
                    snapshot_ref=snapshot.snapshot_hash,
                    decision=verdict.value, reason_code=reason.value,
                    next_eligible_at=next_eligible_at, effective_limits={},
                    timestamp=now_iso or _now_iso_default(),
                )

        # 7. load pressure (MEDIUM+ only).
        if resource_class in (ResourceClass.MEDIUM, ResourceClass.HEAVY,
                              ResourceClass.EXCLUSIVE):
            if (
                snapshot.load_5min is not None
                and snapshot.cpu_count is not None
                and snapshot.cpu_count > 0
                and snapshot.load_5min > snapshot.cpu_count * policy.load_multiplier_5min
            ):
                verdict, reason = AdmissionVerdict.DEFER, ResourceReasonCode.LOAD_PRESSURE
                next_eligible_at = _add_seconds_iso(now_iso, policy.defer_retry_seconds)
                return AdmissionDecision(
                    resource_class=resource_class.value,
                    policy_version=policy.policy_version,
                    snapshot_ref=snapshot.snapshot_hash,
                    decision=verdict.value, reason_code=reason.value,
                    next_eligible_at=next_eligible_at, effective_limits={},
                    timestamp=now_iso or _now_iso_default(),
                )

        # 8. PREFER_EXTERNAL (routing hint only; no external action).
        if prefer_external_ci:
            return AdmissionDecision(
                resource_class=resource_class.value,
                policy_version=policy.policy_version,
                snapshot_ref=snapshot.snapshot_hash,
                decision=AdmissionVerdict.PREFER_EXTERNAL.value,
                reason_code=ResourceReasonCode.EXTERNAL_CI_PREFERRED.value,
                next_eligible_at=None,
                effective_limits=self._effective_limits(resource_class, snapshot, reserve),
                timestamp=now_iso or _now_iso_default(),
            )

        # 9. ALLOW.
        return AdmissionDecision(
            resource_class=resource_class.value,
            policy_version=policy.policy_version,
            snapshot_ref=snapshot.snapshot_hash,
            decision=AdmissionVerdict.ALLOW.value,
            reason_code=ResourceReasonCode.OK.value,
            next_eligible_at=None,
            effective_limits=self._effective_limits(resource_class, snapshot, reserve),
            timestamp=now_iso or _now_iso_default(),
        )

    def decide(
        self,
        *,
        resource_class,
        snapshot: HostResourceSnapshot,
        policy: Optional[ResourcePolicy] = None,
        now_iso: Optional[str] = None,
        prefer_external_ci: bool = False,
        estimated_temp_bytes: Optional[int] = None,
        tmp_paths: Optional[Sequence[str]] = None,
    ) -> AdmissionDecision:
        """Decide admission for ``resource_class`` against ``snapshot``.

        ``resource_class`` may be a :class:`ResourceClass` or its string value;
        an unknown value raises ``ValueError`` (validation, never guessed).
        """
        if isinstance(resource_class, ResourceClass):
            rc = resource_class
        else:
            rc = ResourceClass(resource_class)  # raises ValueError on unknown
        pol = policy or self._policy
        return self._decide(
            resource_class=rc,
            snapshot=snapshot,
            policy=pol,
            now_iso=now_iso,
            prefer_external_ci=prefer_external_ci,
            estimated_temp_bytes=estimated_temp_bytes,
            tmp_paths=tmp_paths,
        )
