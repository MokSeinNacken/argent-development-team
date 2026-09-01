"""Phase C1 — disk + tmpfs policy admission tests."""

from __future__ import annotations

from argent_core.resource_governor import (
    AdmissionVerdict,
    ResourceGovernor,
    ResourceReasonCode,
)
from argent_core.resource_policy import ResourceClass, gib
from c1_helpers import make_snapshot


def _governor():
    return ResourceGovernor()


def test_enough_disk_is_ok():
    snap = make_snapshot(root_free=gib(100), root_free_ratio=0.5)
    d = _governor().decide(
        resource_class=ResourceClass.MEDIUM, snapshot=snap,
        now_iso="2026-09-01T00:00:00+00:00",
    )
    assert d.decision == AdmissionVerdict.ALLOW.value


def test_less_than_10_gib_denies_local():
    snap = make_snapshot(root_free=gib(5), root_free_ratio=0.5)
    d = _governor().decide(
        resource_class=ResourceClass.MEDIUM, snapshot=snap,
        now_iso="2026-09-01T00:00:00+00:00",
    )
    assert d.decision == AdmissionVerdict.DENY_LOCAL.value
    assert d.reason_code == ResourceReasonCode.DISK_LOW.value


def test_less_than_15_percent_denies_local():
    snap = make_snapshot(root_free=gib(100), root_free_ratio=0.05)
    d = _governor().decide(
        resource_class=ResourceClass.MEDIUM, snapshot=snap,
        now_iso="2026-09-01T00:00:00+00:00",
    )
    assert d.decision == AdmissionVerdict.DENY_LOCAL.value
    assert d.reason_code == ResourceReasonCode.DISK_LOW.value


def test_tmpfs_policy_violation_for_repo_in_tmp():
    snap = make_snapshot(tmp_fs_type="tmpfs")
    d = _governor().decide(
        resource_class=ResourceClass.HEAVY, snapshot=snap,
        tmp_paths=["/tmp/repo", "/tmp/node_modules"],
        now_iso="2026-09-01T00:00:00+00:00",
    )
    assert d.decision == AdmissionVerdict.DENY_LOCAL.value
    assert d.reason_code == ResourceReasonCode.TMPFS_POLICY_VIOLATION.value


def test_tmpfs_small_bounded_files_allowed():
    snap = make_snapshot(tmp_fs_type="tmpfs")
    d = _governor().decide(
        resource_class=ResourceClass.HEAVY, snapshot=snap,
        tmp_paths=["/tmp/small.log"],
        now_iso="2026-09-01T00:00:00+00:00",
    )
    assert d.decision == AdmissionVerdict.ALLOW.value


def test_non_tmpfs_no_tmpfs_violation():
    snap = make_snapshot(tmp_fs_type="ext4")
    d = _governor().decide(
        resource_class=ResourceClass.HEAVY, snapshot=snap,
        tmp_paths=["/tmp/my-repo"],
        now_iso="2026-09-01T00:00:00+00:00",
    )
    assert d.decision == AdmissionVerdict.ALLOW.value


def test_factor_two_rule_for_large_temp_data():
    # 60 GiB of estimated temp needs >= 120 GiB persistent free.
    snap = make_snapshot(workspace_free=gib(100), root_free=gib(200), root_free_ratio=0.5)
    d = _governor().decide(
        resource_class=ResourceClass.HEAVY, snapshot=snap,
        estimated_temp_bytes=gib(60),
        now_iso="2026-09-01T00:00:00+00:00",
    )
    assert d.decision == AdmissionVerdict.DENY_LOCAL.value
    assert d.reason_code == ResourceReasonCode.DISK_LOW.value

    # Enough persistent space satisfies the factor-2 rule.
    snap2 = make_snapshot(workspace_free=gib(150), root_free=gib(200), root_free_ratio=0.5)
    d2 = _governor().decide(
        resource_class=ResourceClass.HEAVY, snapshot=snap2,
        estimated_temp_bytes=gib(60),
        now_iso="2026-09-01T00:00:00+00:00",
    )
    assert d2.decision == AdmissionVerdict.ALLOW.value
