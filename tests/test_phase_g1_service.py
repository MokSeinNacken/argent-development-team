"""Phase G1 — service config, entrypoint, systemd unit, trust boundaries.

Acceptance cases 26–36, 40.  Deterministic and offline: config loading is
pure, the HMAC boundary is the existing Phase-F ``_resolve_mac_key``, and the
systemd unit + new source files are validated statically (no activation).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from argent_core import Core, OWNER_SOURCE
from argent_core.argent_service import (
    EXIT_INIT_ERROR,
    build_service,
    load_service_config,
    main,
)
from argent_core.resource_governor import AdmissionVerdict, ResourceReasonCode
from argent_core.resource_policy import ResourceClass
from argent_core.scheduler import Scheduler
from argent_core.supervisor import Supervisor
from mock_supervisor_runtime import FakeClock, FakeRunLauncher, FakeRunStatusProvider

_REPO = Path(__file__).resolve().parent.parent
_UNIT = _REPO / "g1-systemd" / "argent-supervisor.service"


# ---------------------------------------------------------------------------
# Case 26: malformed trusted config fail-closed
# ---------------------------------------------------------------------------

def test_config_defaults_resolve_canonical_paths(tmp_path):
    # ``tmp_path`` is under /tmp, so pass reject_ephemeral=False to assert the
    # canonical path RESOLUTION (the ephemeral refusal is tested separately).
    cfg = load_service_config(home=tmp_path, env={}, reject_ephemeral=False)
    assert cfg.state_dir == tmp_path / ".local" / "state" / "argent"
    assert cfg.share_dir == tmp_path / ".local" / "share" / "argent"
    assert cfg.cache_dir == tmp_path / ".cache" / "argent"
    assert cfg.db_path == cfg.state_dir / "argent.db"


def test_config_env_overrides_xdg(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path / "s")}
    cfg = load_service_config(home=tmp_path, env=env, reject_ephemeral=False)
    assert cfg.state_dir == tmp_path / "s" / "argent"


def test_config_malformed_json_fails_closed(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text("{ not json")
    with pytest.raises(ValueError):
        load_service_config(str(p), home=tmp_path, env={})


def test_config_non_object_fails_closed(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text("[1,2,3]")
    with pytest.raises(ValueError):
        load_service_config(str(p), home=tmp_path, env={})


def test_config_wrong_typed_field_fails_closed(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"state_dir": 123}))
    with pytest.raises(ValueError):
        load_service_config(str(p), home=tmp_path, env={})


def test_config_ephemeral_path_fails_closed(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"state_dir": "/tmp/argent"}))
    with pytest.raises(ValueError):
        load_service_config(str(p), home=tmp_path, env={})


def test_config_valid_file(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "state_dir": str(tmp_path / "mystate"),
        "lease_ttl_seconds": 30,
    }))
    cfg = load_service_config(str(p), home=tmp_path, env={},
                              reject_ephemeral=False)
    assert cfg.state_dir == tmp_path / "mystate"
    assert cfg.lease_ttl_seconds == 30


# ---------------------------------------------------------------------------
# Case 27: unavailable persistent state store fail-closed
# ---------------------------------------------------------------------------

def test_main_malformed_config_exits_nonzero(tmp_path, capsys):
    p = tmp_path / "cfg.json"
    p.write_text("{ not json")
    assert main(["--config", str(p)]) == EXIT_INIT_ERROR
    assert "fatal" in capsys.readouterr().err


def test_build_service_unavailable_store_raises(tmp_path):
    # db_path points at a DIRECTORY (unopenable as SQLite) -> fail-closed raise.
    state = tmp_path / "state"
    state.mkdir()
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"state_dir": str(state), "db_path": str(state)}))
    cfg = load_service_config(str(p), home=tmp_path, env={},
                              reject_ephemeral=False)
    with pytest.raises(Exception):
        build_service(cfg)


# ---------------------------------------------------------------------------
# Cases 28/29: evidence MAC key boundary (Phase F carried forward)
# ---------------------------------------------------------------------------

def test_evidence_mac_key_missing_fails_closed(monkeypatch):
    from argent_core.test_execution import _resolve_mac_key
    monkeypatch.delenv("ARGENT_EVIDENCE_MAC_KEY", raising=False)
    monkeypatch.delenv("ARGENT_EVIDENCE_MAC_KEY_FILE", raising=False)
    with pytest.raises(ValueError):
        _resolve_mac_key(None)


def test_evidence_mac_key_from_env_file(monkeypatch, tmp_path):
    from argent_core.test_execution import _resolve_mac_key
    keyfile = tmp_path / "key.bin"
    keyfile.write_bytes(b"k" * 32)
    monkeypatch.setenv("ARGENT_EVIDENCE_MAC_KEY_FILE", str(keyfile))
    monkeypatch.delenv("ARGENT_EVIDENCE_MAC_KEY", raising=False)
    assert _resolve_mac_key(None) == b"k" * 32


def test_writer_cannot_control_key_source(monkeypatch):
    # The MAC key authority is the process environment (or an explicit trusted
    # constructor arg) ONLY.  It is never read from the store or from agent
    # output: ``_resolve_mac_key`` accepts no store/db and no agent data.
    import inspect
    from argent_core.test_execution import _resolve_mac_key
    assert list(inspect.signature(_resolve_mac_key).parameters) == ["mac_key"]
    monkeypatch.delenv("ARGENT_EVIDENCE_MAC_KEY", raising=False)
    monkeypatch.delenv("ARGENT_EVIDENCE_MAC_KEY_FILE", raising=False)
    with pytest.raises(ValueError):
        _resolve_mac_key(None)


# ---------------------------------------------------------------------------
# Cases 30/31/32/40: unit file + source static validation
# ---------------------------------------------------------------------------

def _read(p):
    return Path(p).read_text(encoding="utf-8")


def test_unit_file_has_no_embedded_secret():
    text = _read(_UNIT)
    assert "ARGENT_EVIDENCE_MAC_KEY=" not in text
    assert not re.search(
        r"(password|secret|token|api[_-]?key)\s*=\s*[A-Za-z0-9]", text, re.I,
    )
    assert "EnvironmentFile=-" in text


def test_unit_file_runs_as_user_not_root():
    text = _read(_UNIT)
    assert "User=root" not in text
    assert "User=" not in text  # user service -> invoking user by default
    assert "[Install]" in text
    assert "WantedBy=default.target" in text


def test_unit_file_no_public_listener():
    text = _read(_UNIT)
    for directive in ("ListenStream", "ListenDatagram", "Listen", "Accept="):
        assert directive not in text


def test_new_modules_open_no_network_listener():
    for mod in ("background_runtime", "argent_service", "runtime_paths"):
        text = _read(_REPO / "argent_core" / f"{mod}.py")
        for token in ("socket(", ".bind(", ".listen(", ".accept(", "http.server"):
            assert token not in text, f"{mod} references {token}"


def test_g1_does_not_activate_systemd():
    for mod in ("background_runtime", "argent_service", "runtime_paths"):
        text = _read(_REPO / "argent_core" / f"{mod}.py")
        for token in (
            "systemctl", "daemon-reload", "enable-linger", "loginctl",
            "systemd-run", "subprocess.run", "Popen(",
        ):
            assert token not in text, f"{mod} references {token}"


# ---------------------------------------------------------------------------
# Case 33: resource governor stays binding after background dispatch
# ---------------------------------------------------------------------------

def test_background_loop_honors_resource_governor(db_path):
    from argent_core.background_runtime import SupervisorInstance, SupervisorRuntime
    from argent_core.external_wait import ExternalWaitManager
    from c1_helpers import make_snapshot
    from g1_helpers import make_identity_provider

    clock = FakeClock()
    core = Core(db_path, clock=clock)
    project = core.create_project("p", OWNER_SOURCE)
    task = core.create_task(project.id, "t", OWNER_SOURCE)
    core.start_task_run(task.id, OWNER_SOURCE)
    sup = Supervisor(core, FakeRunStatusProvider(), FakeRunLauncher(), clock=clock)
    job = sup.store.create_job(task.id, idempotency_key="job-main",
                               resource_class=ResourceClass.HEAVY.value)
    jid = job.supervisor_job_id

    class DenyGovernor:
        def decide(self, **kwargs):
            from argent_core.resource_governor import AdmissionDecision
            return AdmissionDecision(
                resource_class=ResourceClass.HEAVY.value, policy_version="1",
                snapshot_ref="snap-1", decision=AdmissionVerdict.DEFER.value,
                reason_code=ResourceReasonCode.INSUFFICIENT_MEMORY_RESERVE.value,
                next_eligible_at="2026-09-01T00:05:00+00:00",
                effective_limits={}, timestamp="2026-09-01T00:00:00+00:00",
            )

    class SnapshotProvider:
        def capture(self, workspace_path=None):
            return make_snapshot()

    sched = Scheduler(sup, owner_instance_id="instance:test", lease_ttl_seconds=60,
                      resource_governor=DenyGovernor(),
                      snapshot_provider=SnapshotProvider())
    inst = SupervisorInstance(
        core._store,
        identity_provider=make_identity_provider("boot-1", {100: 5}),
        instance_id="instance:test", own_pid=100, clock=clock,
    )
    rt = SupervisorRuntime(
        scheduler=sched, external_wait_manager=ExternalWaitManager(core._store),
        instance=inst, store=core._store, clock=clock,
        sleep_fn=lambda s: None, max_passes=1,
    )
    rt.run_loop()
    row = core._store.get_supervisor_job(jid)
    assert row["primary_state"] == "QUEUED"
    assert row["owner_instance_id"] is None
    assert row["last_resource_decision"] == AdmissionVerdict.DEFER.value
    assert row["error_class"] == "RESOURCE"
    core.close()


# ---------------------------------------------------------------------------
# Cases 34/35/36: Phase D/E/F interfaces unchanged
# ---------------------------------------------------------------------------

def test_phase_d_context_rules_unchanged():
    from argent_core.context import PROJECT_RULES, SECURITY_ARCH_RULES
    from argent_core.context_pack import ContextBuilder, validate_context_pack
    assert isinstance(PROJECT_RULES, (list, tuple)) and len(PROJECT_RULES) > 0
    assert isinstance(SECURITY_ARCH_RULES, (list, tuple))
    assert callable(validate_context_pack)
    assert ContextBuilder is not None


def test_phase_e_routing_unchanged():
    from argent_core.routing import resolve_model, validate_model_choice
    from argent_core.model_registry import ModelRegistry, get_default_registry
    assert callable(resolve_model)
    assert callable(validate_model_choice)
    assert isinstance(get_default_registry(), ModelRegistry)


def test_phase_f_test_assurance_unchanged():
    from argent_core.test_execution import compute_evidence_mac, _resolve_mac_key
    from argent_core.test_planning import compute_plan_mac
    assert callable(compute_evidence_mac)
    assert callable(compute_plan_mac)
    assert callable(_resolve_mac_key)


# ---------------------------------------------------------------------------
# Case 2/40: single-active advisory lock (defense-in-depth, no activation)
# ---------------------------------------------------------------------------

def test_acquire_lock_blocks_second_holder(tmp_path):
    from argent_core.argent_service import _acquire_lock
    state = tmp_path / "state"
    state.mkdir()
    first = _acquire_lock(state)
    assert first is not None
    # A second supervisor cannot hold the lock -> refused (fail-closed).
    second = _acquire_lock(state)
    assert second is None
    # Releasing the first allows a subsequent acquisition.
    import fcntl
    fcntl.flock(first.fileno(), fcntl.LOCK_UN)
    first.close()
    third = _acquire_lock(state)
    assert third is not None
    fcntl.flock(third.fileno(), fcntl.LOCK_UN)
    third.close()
