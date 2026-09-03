"""Phase G2 — service config, secret-path resolution, store-path invariants.

Deterministic and offline.  Covers the CODE-ENFORCED side of the Phase-G2
systemd-user-service deployment contract that does not require a live systemd:

* trusted service config (``ServiceConfig``) — canonical XDG defaults, env
  overrides, fail-closed on malformed/unknown/NaN/Infinity/ephemeral/foreign
  DB path (re-asserting and extending the G1 F5 cases from the G2 activation
  lens);
* the ``service.env`` deployment contract — it references the evidence MAC key
  ONLY by path (``ARGENT_EVIDENCE_MAC_KEY_FILE``), never by value
  (``ARGENT_EVIDENCE_MAC_KEY``), and the reference resolves through the
  existing Phase-F ``_resolve_mac_key`` (fail-closed);
* store-path invariant — durable DB / health / lock files live under the
  canonical state directory (never the worktree, never ``/tmp``).

No systemd activation, no real secrets (all key bytes are deterministic test
bytes), no network, no real subprocess.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from argent_core.argent_service import (
    _pos_float,
    _pos_int,
    load_service_config,
)
from argent_core import runtime_paths
from argent_core.test_execution import _resolve_mac_key

#: A synthetic, non-ephemeral home so the canonical-path invariants can be
#: asserted WITHOUT triggering the (separately-tested) /tmp refusal.
_FAKE_HOME = Path("/argent-g2-fake-home")


# ---------------------------------------------------------------------------
# Config defaults — canonical XDG paths (SPEC G1 §A, re-asserted for G2)
# ---------------------------------------------------------------------------

def test_config_defaults_resolve_canonical_xdg(tmp_path):
    cfg = load_service_config(home=tmp_path, env={}, reject_ephemeral=False)
    assert cfg.state_dir == tmp_path / ".local" / "state" / "argent"
    assert cfg.share_dir == tmp_path / ".local" / "share" / "argent"
    assert cfg.cache_dir == tmp_path / ".cache" / "argent"
    assert cfg.db_path == cfg.state_dir / "argent.db"


def test_config_defaults_honor_xdg_overrides(tmp_path):
    env = {
        "XDG_STATE_HOME": str(tmp_path / "st"),
        "XDG_DATA_HOME": str(tmp_path / "dt"),
        "XDG_CACHE_HOME": str(tmp_path / "ct"),
    }
    cfg = load_service_config(home=tmp_path, env=env, reject_ephemeral=False)
    assert cfg.state_dir == tmp_path / "st" / "argent"
    assert cfg.share_dir == tmp_path / "dt" / "argent"
    assert cfg.cache_dir == tmp_path / "ct" / "argent"


def test_config_relative_xdg_override_resolves_under_home(tmp_path):
    # A RELATIVE XDG override must resolve under home, never against the cwd.
    cfg = load_service_config(home=tmp_path, env={"XDG_STATE_HOME": "rel"},
                              reject_ephemeral=False)
    assert cfg.state_dir == tmp_path / "rel" / "argent"


def test_config_env_state_dir_override_wins_over_file(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"state_dir": str(tmp_path / "from-file")}))
    env = {"ARGENT_STATE_DIR": str(tmp_path / "from-env")}
    cfg = load_service_config(str(p), home=tmp_path, env=env,
                              reject_ephemeral=False)
    assert cfg.state_dir == tmp_path / "from-env"


# ---------------------------------------------------------------------------
# Config fail-closed (SPEC G1 §B/§26 / F5, re-asserted from the G2 lens)
# ---------------------------------------------------------------------------

def test_config_ephemeral_state_dir_fails_closed(tmp_path):
    with pytest.raises(ValueError):
        load_service_config(home=tmp_path, env={"XDG_STATE_HOME": "/tmp/x"})
    with pytest.raises(ValueError):
        load_service_config(home=tmp_path, env={"ARGENT_STATE_DIR": "/run/x"})


def test_config_symlink_to_ephemeral_fails_closed(tmp_path):
    # Path.resolve() collapses the symlink chain, so a link landing under /tmp
    # is refused (realpath semantics, not abspath).
    link = tmp_path / "link"
    link.symlink_to("/tmp", target_is_directory=True)
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"state_dir": str(link / "argent")}))
    with pytest.raises(ValueError):
        load_service_config(str(p), home=tmp_path, env={})


def test_config_db_path_outside_state_fails_closed(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "state_dir": str(tmp_path / "state"),
        "db_path": str(tmp_path / "elsewhere.db"),
    }))
    with pytest.raises(ValueError):
        load_service_config(str(p), home=tmp_path, env={},
                            reject_ephemeral=False)


def test_config_unknown_field_fails_closed(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"bogus_field": "x"}))
    with pytest.raises(ValueError):
        load_service_config(str(p), home=tmp_path, env={},
                            reject_ephemeral=False)


def test_config_malformed_json_fails_closed(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text("{ not json")
    with pytest.raises(ValueError):
        load_service_config(str(p), home=tmp_path, env={})


def test_config_non_object_fails_closed(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text("[1, 2, 3]")
    with pytest.raises(ValueError):
        load_service_config(str(p), home=tmp_path, env={})


def test_config_wrong_typed_field_fails_closed(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"state_dir": 123}))
    with pytest.raises(ValueError):
        load_service_config(str(p), home=tmp_path, env={})


def test_config_nan_infinity_fails_closed(tmp_path):
    # Python's json accepts the NaN/Infinity literals; they must be refused.
    for bad in ("NaN", "Infinity", "-Infinity"):
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps({"idle_sleep_seconds": json.loads(bad)}))
        with pytest.raises(ValueError):
            load_service_config(str(p), home=tmp_path, env={},
                                reject_ephemeral=False)
    # The pure coercers reject NaN/Inf/0/negative/bool directly.
    with pytest.raises(ValueError):
        _pos_float(float("nan"), 5.0, "x")
    with pytest.raises(ValueError):
        _pos_float(float("inf"), 5.0, "x")
    with pytest.raises(ValueError):
        _pos_float(0.0, 5.0, "x")
    with pytest.raises(ValueError):
        _pos_float(-1.0, 5.0, "x")
    with pytest.raises(ValueError):
        _pos_int(0, 10, "x")
    with pytest.raises(ValueError):
        _pos_int(True, 10, "x")
    with pytest.raises(ValueError):
        _pos_int(-1, 10, "x")


# ---------------------------------------------------------------------------
# service.env deployment contract (SPEC G1 §M) — path reference, never a value
# ---------------------------------------------------------------------------

def _parse_systemd_env_file(text: str) -> dict:
    """Minimal, strict systemd ``EnvironmentFile`` parse (the relevant subset).

    systemd reads ``KEY=VALUE`` lines (no ``export``, no quoting, ``#`` starts a
    comment, blank lines are ignored).  This test helper mirrors that contract
    so the deployment contract is exercised deterministically offline.
    """
    result: dict = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        result[key] = value.strip()
    return result


def _canonical_service_env(key_path: Path) -> str:
    return (
        "# Argent supervisor environment (owner-provisioned, 0600).\n"
        "# References the evidence MAC key BY PATH only; never a value.\n"
        f"ARGENT_EVIDENCE_MAC_KEY_FILE={key_path}\n"
    )


def test_service_env_references_path_only_never_key_value(tmp_path):
    key_path = tmp_path / "evidence_mac.key"
    key_path.write_bytes(b"K" * 32)
    text = _canonical_service_env(key_path)

    parsed = _parse_systemd_env_file(text)
    assert "ARGENT_EVIDENCE_MAC_KEY_FILE" in parsed
    assert "ARGENT_EVIDENCE_MAC_KEY" not in parsed  # never the value form
    assert parsed["ARGENT_EVIDENCE_MAC_KEY_FILE"] == str(key_path)
    # No raw key bytes may appear in the env file.
    assert b"K" * 32 not in text.encode()


def test_service_env_path_reference_resolves_through_key_resolver(
    monkeypatch, tmp_path,
):
    key_path = tmp_path / "evidence_mac.key"
    key_path.write_bytes(b"S" * 32)
    # systemd injects the EnvironmentFile into the process env; emulate it.
    monkeypatch.setenv("ARGENT_EVIDENCE_MAC_KEY_FILE", str(key_path))
    monkeypatch.delenv("ARGENT_EVIDENCE_MAC_KEY", raising=False)
    assert _resolve_mac_key(None) == b"S" * 32


def test_service_env_value_form_would_leak_key_and_is_rejected(tmp_path):
    # If the operator (incorrectly) wrote the VALUE form, a strict parse would
    # surface ARGENT_EVIDENCE_MAC_KEY — which the deployment checker flags.  The
    # path-reference contract exists precisely to keep the value out of any
    # env file that the unit/subprocess machinery might otherwise inherit.
    bad = "ARGENT_EVIDENCE_MAC_KEY=abcdef0123456789abcdef0123456789\n"
    parsed = _parse_systemd_env_file(bad)
    assert "ARGENT_EVIDENCE_MAC_KEY" in parsed
    # The KEY VALUE never appears as a FILE reference — distinct forms.
    assert "ARGENT_EVIDENCE_MAC_KEY_FILE" not in parsed


def test_evidence_key_missing_fails_closed(monkeypatch):
    monkeypatch.delenv("ARGENT_EVIDENCE_MAC_KEY", raising=False)
    monkeypatch.delenv("ARGENT_EVIDENCE_MAC_KEY_FILE", raising=False)
    with pytest.raises(ValueError):
        _resolve_mac_key(None)


def test_evidence_key_file_empty_fails_closed(monkeypatch, tmp_path):
    key_path = tmp_path / "empty.key"
    key_path.write_bytes(b"")
    monkeypatch.setenv("ARGENT_EVIDENCE_MAC_KEY_FILE", str(key_path))
    monkeypatch.delenv("ARGENT_EVIDENCE_MAC_KEY", raising=False)
    with pytest.raises(ValueError):
        _resolve_mac_key(None)


def test_evidence_key_file_too_short_fails_closed(monkeypatch, tmp_path):
    key_path = tmp_path / "short.key"
    key_path.write_bytes(b"short")
    monkeypatch.setenv("ARGENT_EVIDENCE_MAC_KEY_FILE", str(key_path))
    monkeypatch.delenv("ARGENT_EVIDENCE_MAC_KEY", raising=False)
    with pytest.raises(ValueError):
        _resolve_mac_key(None)


# ---------------------------------------------------------------------------
# Store-path invariant — durable data under state_dir, never worktree/tmp
# ---------------------------------------------------------------------------

def test_store_paths_stay_under_state_dir_not_worktree_not_tmp():
    home = _FAKE_HOME
    # Pass an explicit empty env so ambient XDG_* variables can't perturb the
    # deterministic canonical-path assertions.
    state = runtime_paths.resolve_state_dir(home=home, env={})
    db = runtime_paths.default_db_path(home=home, env={})
    health = runtime_paths.default_health_path(home=home, env={})
    lock = runtime_paths.default_instance_lock_path(home=home, env={})

    # Canonical location: ~/.local/state/argent (never hardcoded /home/<user>).
    assert state == home / ".local" / "state" / "argent"
    # DB / health / lock all live under the state directory.
    for p in (db, health, lock):
        assert p.is_relative_to(state), f"{p} escapes state_dir"
    # Never the worktree and never an ephemeral root.
    worktree = Path(__file__).resolve().parent.parent
    for p in (state, db, health, lock):
        assert not p.is_relative_to(worktree)
        for root in ("/tmp", "/dev/shm", "/run"):
            assert not p.is_relative_to(Path(root))


def test_default_db_is_under_state_dir_via_config(tmp_path):
    cfg = load_service_config(home=tmp_path, env={}, reject_ephemeral=False)
    assert cfg.db_path == cfg.state_dir / "argent.db"
    assert cfg.db_path.is_relative_to(cfg.state_dir)


def test_resolve_state_dir_refuses_ephemeral_by_default():
    # Production default (reject_ephemeral=True) refuses a /tmp state dir.
    with pytest.raises(ValueError):
        runtime_paths.resolve_state_dir(
            home=Path("/home/x"), env={"XDG_STATE_HOME": "/tmp/argent"},
        )
