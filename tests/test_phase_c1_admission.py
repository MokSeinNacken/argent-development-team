"""Phase C1 — admission decision tests (all verdicts + reason codes + fields)."""

from __future__ import annotations

from argent_core.resource_governor import (
    AdmissionDecision,
    AdmissionVerdict,
    ResourceGovernor,
    ResourceReasonCode,
)
from argent_core.resource_policy import ResourceClass, gib
from c1_helpers import make_snapshot


def _governor():
    return ResourceGovernor()


def _now():
    return "2026-09-01T00:00:00+00:00"


def test_all_reason_codes_defined():
    assert [c.value for c in ResourceReasonCode] == [
        "OK", "INSUFFICIENT_MEMORY_RESERVE", "SWAP_PRESSURE", "DISK_LOW",
        "TMPFS_POLICY_VIOLATION", "LOAD_PRESSURE", "CONCURRENCY_LIMIT",
        "RESOURCE_EVIDENCE_UNKNOWN", "LOCAL_CAPACITY_INSUFFICIENT",
        "EXTERNAL_CI_PREFERRED",
    ]


def test_allow_decision_has_full_fields():
    snap = make_snapshot()
    d = _governor().decide(
        resource_class=ResourceClass.HEAVY, snapshot=snap, now_iso=_now(),
    )
    assert isinstance(d, AdmissionDecision)
    assert d.decision == AdmissionVerdict.ALLOW.value
    assert d.reason_code == ResourceReasonCode.OK.value
    assert d.resource_class == "HEAVY"
    assert d.policy_version == "1"
    assert d.snapshot_ref == snap.snapshot_hash
    assert d.timestamp == _now()
    assert d.next_eligible_at is None
    # C2 proposal present with an effective memory max <= ceiling.
    assert d.effective_limits["memory_max_bytes"] <= gib(4)
    assert d.effective_limits["memory_high_bytes"] == gib(3)
    assert d.effective_limits["cpu_quota_percent"] == 300
    assert d.effective_limits["timeout_seconds"] == 120 * 60


def test_defer_has_bounded_next_eligible_at():
    # Tight memory -> INSUFFICIENT_MEMORY_RESERVE defer with +300s horizon.
    snap = make_snapshot(mem_total=gib(8), mem_available=gib(4))
    d = _governor().decide(
        resource_class=ResourceClass.HEAVY, snapshot=snap, now_iso=_now(),
    )
    assert d.decision == AdmissionVerdict.DEFER.value
    assert d.reason_code == ResourceReasonCode.INSUFFICIENT_MEMORY_RESERVE.value
    assert d.next_eligible_at == "2026-09-01T00:05:00+00:00"  # +300s


def test_deny_local_decision():
    snap = make_snapshot(root_free=gib(5), root_free_ratio=0.5)
    d = _governor().decide(
        resource_class=ResourceClass.HEAVY, snapshot=snap, now_iso=_now(),
    )
    assert d.decision == AdmissionVerdict.DENY_LOCAL.value
    assert d.reason_code == ResourceReasonCode.DISK_LOW.value
    assert d.next_eligible_at is None


def test_prefer_external_is_routing_hint_only():
    snap = make_snapshot(mem_total=gib(32), mem_available=gib(24))
    d = _governor().decide(
        resource_class=ResourceClass.EXCLUSIVE, snapshot=snap, now_iso=_now(),
        prefer_external_ci=True,
    )
    assert d.decision == AdmissionVerdict.PREFER_EXTERNAL.value
    assert d.reason_code == ResourceReasonCode.EXTERNAL_CI_PREFERRED.value
    # Still carries a C2 proposal (routing hint only, no external action).
    assert "memory_max_bytes" in d.effective_limits


def test_evidence_unknown_denies_medium_heavy_exclusive():
    snap = make_snapshot(mem_total=None, mem_available=None)
    for cls in (ResourceClass.MEDIUM, ResourceClass.HEAVY, ResourceClass.EXCLUSIVE):
        d = _governor().decide(resource_class=cls, snapshot=snap, now_iso=_now())
        assert d.decision == AdmissionVerdict.DENY_LOCAL.value
        assert d.reason_code == ResourceReasonCode.RESOURCE_EVIDENCE_UNKNOWN.value


def test_evidence_unknown_light_is_conservative_defer():
    snap = make_snapshot(mem_total=None, mem_available=None)
    d = _governor().decide(
        resource_class=ResourceClass.LIGHT, snapshot=snap, now_iso=_now(),
    )
    assert d.decision == AdmissionVerdict.DEFER.value
    assert d.reason_code == ResourceReasonCode.RESOURCE_EVIDENCE_UNKNOWN.value


def test_load_pressure_defers_medium_plus():
    snap = make_snapshot(load5=20.0, cpu_count=8)  # 20 > 8*1.5
    d = _governor().decide(
        resource_class=ResourceClass.HEAVY, snapshot=snap, now_iso=_now(),
    )
    assert d.decision == AdmissionVerdict.DEFER.value
    assert d.reason_code == ResourceReasonCode.LOAD_PRESSURE.value


def test_unknown_resource_class_raises():
    with __import__("pytest").raises(ValueError):
        _governor().decide(resource_class="NOPE", snapshot=make_snapshot())
