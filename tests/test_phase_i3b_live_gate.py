"""Phase I3-B — live-write activation gate (CASE 1) + credential-mask fix
presence (CASE 2).

Deterministic, no network, no real writes.  Proves that a real GitHub write can
NEVER activate without BOTH an explicit controller flag AND the I3-A
credential-mask fix (marker + resolver) in the running code.
"""

from __future__ import annotations

from pathlib import Path

from argent_core import execution_scope
from argent_core.github_provider_adapter import (
    LIVE_WRITE_REQUIRED_COMMIT,
    GitHubProviderAdapter,
    credential_mask_fix_present,
    live_write_gate,
)

_PRE_I3A = "0000000000000000000000000000000000000000"


# ---------------------------------------------------------------------------
# CASE 1 — live mode cannot activate without the I3-A marker/commit
# ---------------------------------------------------------------------------

def test_case1_gate_requires_explicit_flag():
    assert live_write_gate(False) is False
    assert live_write_gate(True) is True  # real code carries the fix


def test_case1_gate_requires_credential_mask_resolver():
    # Even with the flag, a missing resolver fails closed.
    assert live_write_gate(True, resolver_present=False) is False


def test_case1_gate_fails_on_pre_i3a_marker(monkeypatch):
    # Simulate a pre-I3-A deployment: the required-commit marker is pinned to a
    # value predating the credential-mask fix -> live write can never activate.
    assert LIVE_WRITE_REQUIRED_COMMIT != _PRE_I3A
    monkeypatch.setattr(
        "argent_core.github_provider_adapter.LIVE_WRITE_REQUIRED_COMMIT",
        _PRE_I3A)
    assert live_write_gate(True) is False


def test_case1_adapter_write_disabled_without_flag():
    a = GitHubProviderAdapter()
    assert a.write_enabled is False
    assert a.provider_name == "github"


def test_case1_adapter_write_disabled_on_pre_i3a_marker(monkeypatch):
    monkeypatch.setattr(
        "argent_core.github_provider_adapter.LIVE_WRITE_REQUIRED_COMMIT",
        _PRE_I3A)
    a = GitHubProviderAdapter(live_write=True)
    assert a.write_enabled is False


def test_case1_adapter_live_enabled_only_with_gate(monkeypatch):
    # With the fix present, an explicit flag enables the write path.
    assert credential_mask_fix_present() is True
    a = GitHubProviderAdapter(live_write=True)
    assert a.write_enabled is True
    # A patched-out resolver disables it again (fail closed).
    monkeypatch.setattr(
        "argent_core.github_provider_adapter.credential_mask_fix_present",
        lambda: False)
    b = GitHubProviderAdapter(live_write=True)
    assert b.write_enabled is False


# ---------------------------------------------------------------------------
# CASE 2 — the deployment must carry the I3-A credential-mask fix
# ---------------------------------------------------------------------------

def test_case2_credential_mask_resolver_present():
    assert credential_mask_fix_present() is True
    assert callable(execution_scope.resolve_credential_mask_paths)


def test_case2_resolved_mask_covers_gh_config_dir():
    paths = execution_scope.resolve_credential_mask_paths()
    assert Path.home() / ".config" / "gh" in paths


def test_case2_resolved_mask_covers_gh_dir_explicit_home(tmp_path):
    home = tmp_path / "home"
    paths = execution_scope.resolve_credential_mask_paths(home=home, env={})
    assert Path(home, ".config", "gh") in paths
