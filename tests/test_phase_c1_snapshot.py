"""Phase C1 — host snapshot parsing/provider tests (deterministic, fake readers).

No real host reads: every reader is a fake.  Proves strict parsing (never
guess), UNKNOWN fail-closed behaviour, and stable snapshot hashes.
"""

from __future__ import annotations

from argent_core.host_snapshot import (
    HostSnapshotProvider,
    parse_loadavg,
    parse_meminfo,
    parse_mounts,
    statvfs_free,
)


def test_parse_meminfo_good():
    text = (
        "MemTotal:        8056920 kB\n"
        "MemFree:         1000000 kB\n"
        "MemAvailable:    6291456 kB\n"
        "Buffers:          1000 kB\n"
        "SwapTotal:       2097152 kB\n"
        "SwapFree:        2097152 kB\n"
    )
    d = parse_meminfo(text)
    assert d["MemTotal"] == 8056920 * 1024
    assert d["MemAvailable"] == 6291456 * 1024
    assert d["SwapTotal"] == 2097152 * 1024
    assert d["SwapFree"] == 2097152 * 1024


def test_parse_meminfo_malformed_value_is_unknown():
    d = parse_meminfo("MemTotal: notanumber kB\nMemAvailable: 100 kB\n")
    assert d["MemTotal"] is None
    assert d["MemAvailable"] == 100 * 1024


def test_parse_meminfo_empty_is_none():
    assert parse_meminfo("") is None
    assert parse_meminfo("   \n") is None
    assert parse_meminfo(None) is None


def test_parse_loadavg_good_and_bad():
    assert parse_loadavg("0.10 0.20 0.30 1/100 1234") == (0.10, 0.20, 0.30)
    assert parse_loadavg("0.10 0.20") is None
    assert parse_loadavg("x y z") is None
    assert parse_loadavg(None) is None


def test_parse_mounts_returns_mountpoint_to_fstype():
    text = (
        "/dev/sdd / ext4 rw,relatime 0 0\n"
        "tmpfs /tmp tmpfs rw,nosuid,nodev 0 0\n"
        "proc /proc proc rw 0 0\n"
    )
    m = parse_mounts(text)
    assert m["/"] == "ext4"
    assert m["/tmp"] == "tmpfs"
    assert m["/proc"] == "proc"


def test_parse_mounts_empty_is_none():
    assert parse_mounts("") is None
    assert parse_mounts(None) is None


def test_provider_captures_with_fake_readers_and_unknown_fields():
    def meminfo():
        return "MemTotal: 1000 kB\nMemAvailable: 500 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n"

    def loadavg():
        return "0.1 0.2 0.3 1/1 1\n"

    def mounts():
        return "tmpfs /tmp tmpfs rw 0 0\n"

    def svf(path):
        if path == "/":
            return (1000, 0.5)
        if path == "/ws":
            return (500, 0.25)
        return None

    provider = HostSnapshotProvider(
        meminfo_reader=meminfo,
        loadavg_reader=loadavg,
        mounts_reader=mounts,
        statvfs_reader=svf,
        cpu_count_reader=lambda: 8,
        active_jobs_reader=lambda: [("j1", "LIGHT")],
    )
    snap = provider.capture("/ws")
    assert snap.mem_total_bytes == 1000 * 1024
    assert snap.mem_available_bytes == 500 * 1024
    assert snap.swap_total_bytes == 0
    assert snap.swap_free_bytes == 0
    assert snap.tmp_fs_type == "tmpfs"
    assert snap.root_free_bytes == 1000
    assert snap.workspace_free_bytes == 500
    assert snap.cpu_count == 8
    assert snap.load_1min == 0.1
    assert snap.active_jobs == (("j1", "LIGHT"),)
    assert snap.unknown_fields == frozenset()


def test_provider_fail_closed_on_missing_critical_evidence():
    def meminfo():
        return None  # unreadable

    def loadavg():
        return None

    def mounts():
        return None

    def svf(path):
        return None

    provider = HostSnapshotProvider(
        meminfo_reader=meminfo,
        loadavg_reader=loadavg,
        mounts_reader=mounts,
        statvfs_reader=svf,
        cpu_count_reader=lambda: None,
        active_jobs_reader=lambda: None,
    )
    snap = provider.capture(None)
    # Critical fields all None + recorded unknown; provider did not raise.
    assert snap.mem_total_bytes is None
    assert snap.mem_available_bytes is None
    assert snap.swap_total_bytes is None
    assert snap.tmp_fs_type is None
    assert snap.root_free_bytes is None
    assert snap.cpu_count is None
    assert "mem_total_bytes" in snap.unknown_fields
    assert "tmp_fs_type" in snap.unknown_fields
    assert "root_free" in snap.unknown_fields


def test_snapshot_hash_stable_across_identical_facts():
    from c1_helpers import make_snapshot

    a = make_snapshot()
    b = make_snapshot()
    assert a.snapshot_hash == b.snapshot_hash
    c = make_snapshot(mem_available=0)
    assert c.snapshot_hash != a.snapshot_hash


def test_statvfs_free_real_os_call_does_not_crash():
    # Only exercised against a path that exists (never asserts a specific value).
    result = statvfs_free("/")
    assert result is None or (isinstance(result[0], int) and len(result) == 2)
