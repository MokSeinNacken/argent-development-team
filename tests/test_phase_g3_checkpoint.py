"""Phase G3-A — pre-reboot checkpoint validation (deterministic, no live DB).

Proves the pure :mod:`argent_core.g3_checkpoint` validation/extraction/
comparison functions: the checkpoint structure is strictly validated fail-closed
(no secret VALUES, no unknown top-level keys), the G1 fencing predicate is
deterministic on ``(boot_id, host_id, instance_id)``, and the bounded
persisted-state comparison reports only real deltas.

Deterministic fixtures only; the REAL checkpoint file is validated only in a
``skip-if-missing`` integration test (it exists NOW and must keep validating).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from argent_core.g3_checkpoint import (
    CHECKPOINT_VERSION,
    boot_identity,
    compare_persisted_state,
    is_old_authority_still_live,
    post_reboot_validation_plan,
    single_active_supervisor,
    validate_checkpoint,
)

_REAL_CHECKPOINT = Path(
    "/home/pc/.local/state/argent/g3/g3-pre-reboot-checkpoint.json"
)


def _valid_checkpoint() -> dict:
    return {
        "checkpoint_version": "1.0",
        "phase": "G3-A",
        "marker_g2": "ARGENT_PHASE_G2_SYSTEMD_LIVE_GREEN",
        "timestamp": "2026-09-03T14:23:18.594109+02:00",
        "boot_id": "boot-aaaa",
        "host_id": "host-bbbb",
        "supervisor": {
            "instance_id": "instance:test",
            "pid": 1234,
            "process_start_ticks": 5678,
            "status": "ACTIVE",
            "revision": 1,
        },
        "service": {
            "enabled": "yes",
            "active": "yes",
            "health": "READY",
            "unit_sha256": "a" * 64,
        },
        "g2": {"commit_sha": "c" * 40},
        "g3_worktree": {"branch": "b", "head": "c" * 40, "path": "/tmp/x"},
        "db": {"schema_version": "18", "path": "/tmp/argent.db"},
        "job_state_counts": {"QUEUED": 0, "RUNNING": 0, "total_rows": 0},
        "external_wait_count": 0,
        "notification_outbox": {"status_counts": {}, "total_rows": 0},
        "supervisor_actions": 0,
        "process_registry_rows": 0,
        "active_supervisor_instance_rows": 1,
        "journal_secret_scan_hits": 0,
        "persistent_paths": {
            "config_dir": "/home/pc/.config/argent",
            "state_dir": "/home/pc/.local/state/argent",
            "key_file": "/home/pc/.config/argent/evidence_mac.key",
        },
        "expected_post_reboot_invariants": [
            "boot_id changed",
            "old (boot_id,pid,process_start_ticks,instance_id) is NOT live authority",
            "no linger was enabled by G3",
        ],
    }


# ---------------------------------------------------------------------------
# structural validation (fail-closed)
# ---------------------------------------------------------------------------

def test_valid_checkpoint_passes():
    assert validate_checkpoint(_valid_checkpoint()) == []


def test_malformed_non_dict_input_fails_closed():
    for bad in (None, [], "nope", 42, 3.14):
        errors = validate_checkpoint(bad)
        assert errors, f"expected errors for {bad!r}"
        assert any("must be a dict" in e for e in errors)


def test_wrong_version_rejected():
    cp = _valid_checkpoint()
    cp["checkpoint_version"] = "0.9"
    errors = validate_checkpoint(cp)
    assert any("checkpoint_version" in e for e in errors)


def test_wrong_phase_rejected():
    cp = _valid_checkpoint()
    cp["phase"] = "G2"
    errors = validate_checkpoint(cp)
    assert any("phase" in e for e in errors)


def test_missing_boot_id_rejected():
    cp = _valid_checkpoint()
    del cp["boot_id"]
    errors = validate_checkpoint(cp)
    assert any("boot_id" in e for e in errors)


def test_missing_host_id_rejected():
    cp = _valid_checkpoint()
    del cp["host_id"]
    errors = validate_checkpoint(cp)
    assert any("host_id" in e for e in errors)


def test_non_dict_supervisor_rejected():
    cp = _valid_checkpoint()
    cp["supervisor"] = "ACTIVE"
    errors = validate_checkpoint(cp)
    assert any("supervisor" in e for e in errors)


def test_non_positive_pid_rejected():
    cp = _valid_checkpoint()
    cp["supervisor"]["pid"] = 0
    errors = validate_checkpoint(cp)
    assert any("pid" in e for e in errors)


def test_negative_job_count_rejected():
    cp = _valid_checkpoint()
    cp["job_state_counts"]["RUNNING"] = -1
    errors = validate_checkpoint(cp)
    assert any("job_state_counts" in e for e in errors)


def test_unknown_top_level_key_rejected():
    cp = _valid_checkpoint()
    cp["evil_key"] = "x"
    errors = validate_checkpoint(cp)
    assert any("unknown top-level key" in e and "evil_key" in e for e in errors)


def test_secret_named_key_nested_rejected():
    cp = _valid_checkpoint()
    cp["supervisor"]["api_key"] = "do-not-carry-me"
    errors = validate_checkpoint(cp)
    assert any("api_key" in e for e in errors)


def test_secret_named_key_case_insensitive_rejected():
    cp = _valid_checkpoint()
    cp["db"]["Token"] = "x"
    errors = validate_checkpoint(cp)
    assert any("Token" in e for e in errors)


def test_secret_named_key_deep_nested_rejected():
    cp = _valid_checkpoint()
    cp["notification_outbox"]["wrapper"] = {"password": "x"}
    errors = validate_checkpoint(cp)
    assert any("password" in e for e in errors)


# ---------------------------------------------------------------------------
# bounded extraction + fencing predicate + single-active + deltas
# ---------------------------------------------------------------------------

def test_boot_identity_extraction():
    cp = _valid_checkpoint()
    ident = boot_identity(cp)
    assert ident == {
        "boot_id": "boot-aaaa",
        "host_id": "host-bbbb",
        "instance_id": "instance:test",
        "pid": 1234,
        "process_start_ticks": 5678,
    }


def test_old_authority_live_same_boot_host_instance():
    cp = _valid_checkpoint()
    row = {
        "boot_id": "boot-aaaa",
        "host_id": "host-bbbb",
        "instance_id": "instance:test",
        "pid": 9999,  # different pid is irrelevant: identity is (boot,host,instance)
    }
    assert is_old_authority_still_live(cp, row) is True


def test_old_authority_not_live_boot_changed():
    cp = _valid_checkpoint()
    row = {
        "boot_id": "boot-new",
        "host_id": "host-bbbb",
        "instance_id": "instance:test",
    }
    assert is_old_authority_still_live(cp, row) is False


def test_old_authority_not_live_instance_changed():
    cp = _valid_checkpoint()
    row = {
        "boot_id": "boot-aaaa",
        "host_id": "host-bbbb",
        "instance_id": "instance:other",
    }
    assert is_old_authority_still_live(cp, row) is False


def test_single_active_exactly_one():
    rows = [
        {"instance_id": "a", "status": "ACTIVE"},
        {"instance_id": "b", "status": "DEAD"},
    ]
    ok, reason = single_active_supervisor(rows)
    assert ok is True
    assert "exactly one" in reason


def test_single_active_zero():
    ok, reason = single_active_supervisor([{"instance_id": "a", "status": "DEAD"}])
    assert ok is False


def test_single_active_two():
    rows = [
        {"instance_id": "a", "status": "ACTIVE"},
        {"instance_id": "b", "status": "ACTIVE"},
    ]
    ok, reason = single_active_supervisor(rows)
    assert ok is False
    assert "2" in reason


def test_compare_persisted_state_identical():
    cp = _valid_checkpoint()
    live = {
        "db": {"path": "/tmp/argent.db", "schema_version": "18"},
        "g2": {"commit_sha": "c" * 40},
        "host_id": "host-bbbb",
        "service": {"enabled": "yes", "active": "yes"},
    }
    assert compare_persisted_state(cp, live) == []


def test_compare_persisted_state_reports_deltas():
    cp = _valid_checkpoint()
    live = {
        "db": {"path": "/tmp/argent.db", "schema_version": "19"},
        "g2": {"commit_sha": "c" * 40},
        "host_id": "host-changed",
        "service": {"enabled": "yes", "active": "no"},
    }
    deltas = compare_persisted_state(cp, live)
    text = "\n".join(deltas)
    assert "db.schema_version" in text
    assert "host_id" in text
    assert "service.active" in text
    # The unchanged keys are NOT reported.
    assert "db.path" not in text
    assert "service.enabled" not in text


def test_post_reboot_plan_nonempty_with_required_markers():
    cp = _valid_checkpoint()
    plan = post_reboot_validation_plan(cp)
    assert len(plan) > 0
    text = "\n".join(plan)
    assert "boot_id changed" in text
    assert "no linger was enabled by G3" in text


# ---------------------------------------------------------------------------
# real checkpoint (skip-if-missing integration; must keep validating)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _REAL_CHECKPOINT.exists(),
                    reason="real checkpoint not present")
def test_real_checkpoint_validates():
    data = json.loads(_REAL_CHECKPOINT.read_text(encoding="utf-8"))
    assert validate_checkpoint(data) == []
    assert CHECKPOINT_VERSION == "1.0"
