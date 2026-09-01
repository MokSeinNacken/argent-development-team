"""Phase C1 — concurrency-limit admission tests (incl. global single-writer)."""

from __future__ import annotations

from argent_core.resource_governor import (
    AdmissionVerdict,
    ResourceGovernor,
    ResourceReasonCode,
)
from argent_core.resource_policy import ResourceClass
from c1_helpers import make_snapshot


def _governor():
    return ResourceGovernor()


def _snap(active=()):
    return make_snapshot(active_jobs=active)


def test_light_up_to_two_concurrent():
    snap = _snap([("j1", "LIGHT"), ("j2", "LIGHT")])
    d = _governor().decide(
        resource_class=ResourceClass.LIGHT, snapshot=snap,
        now_iso="2026-09-01T00:00:00+00:00",
    )
    assert d.decision == AdmissionVerdict.DEFER.value
    assert d.reason_code == ResourceReasonCode.CONCURRENCY_LIMIT.value


def test_light_allowed_when_under_limit():
    snap = _snap([("j1", "LIGHT")])
    d = _governor().decide(
        resource_class=ResourceClass.LIGHT, snapshot=snap,
        now_iso="2026-09-01T00:00:00+00:00",
    )
    assert d.decision == AdmissionVerdict.ALLOW.value


def test_medium_at_most_one():
    snap = _snap([("j1", "MEDIUM")])
    d = _governor().decide(
        resource_class=ResourceClass.MEDIUM, snapshot=snap,
        now_iso="2026-09-01T00:00:00+00:00",
    )
    assert d.decision == AdmissionVerdict.DEFER.value
    assert d.reason_code == ResourceReasonCode.CONCURRENCY_LIMIT.value


def test_heavy_excludes_other_medium_heavy():
    for active in ([("j1", "MEDIUM")], [("j1", "HEAVY")]):
        d = _governor().decide(
            resource_class=ResourceClass.HEAVY, snapshot=_snap(active),
            now_iso="2026-09-01T00:00:00+00:00",
        )
        assert d.decision == AdmissionVerdict.DEFER.value
        assert d.reason_code == ResourceReasonCode.CONCURRENCY_LIMIT.value


def test_exclusive_excludes_medium_heavy_exclusive():
    for active in ([("j1", "MEDIUM")], [("j1", "HEAVY")], [("j1", "EXCLUSIVE")]):
        d = _governor().decide(
            resource_class=ResourceClass.EXCLUSIVE, snapshot=_snap(active),
            now_iso="2026-09-01T00:00:00+00:00",
        )
        assert d.decision == AdmissionVerdict.DEFER.value
        assert d.reason_code == ResourceReasonCode.CONCURRENCY_LIMIT.value


def test_global_single_writer():
    # Any writer active blocks a new writer (MEDIUM candidate here).
    snap = _snap([("j1", "EXCLUSIVE")])
    d = _governor().decide(
        resource_class=ResourceClass.MEDIUM, snapshot=snap,
        now_iso="2026-09-01T00:00:00+00:00",
    )
    assert d.decision == AdmissionVerdict.DEFER.value
    assert d.reason_code == ResourceReasonCode.CONCURRENCY_LIMIT.value


def test_light_ignored_by_writer_limit():
    # LIGHT jobs do not count against the single-writer limit.
    snap = _snap([("j1", "LIGHT"), ("j2", "LIGHT")])
    # but a MEDIUM candidate is only blocked by the writer limit if a writer is
    # active; LIGHT-only active does not block a writer.
    d = _governor().decide(
        resource_class=ResourceClass.MEDIUM, snapshot=make_snapshot(
            active_jobs=[("j1", "LIGHT")],
        ),
        now_iso="2026-09-01T00:00:00+00:00",
    )
    assert d.decision == AdmissionVerdict.ALLOW.value
