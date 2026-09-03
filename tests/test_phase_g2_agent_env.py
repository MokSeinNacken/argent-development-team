"""Phase G2 — agent/child spawn-environment boundary (SPEC G1 §M / F4).

Deterministic, offline.  Re-asserts and extends the Phase-F/G1 spawn
environment allowlist contract: every agent/command spawn inherits a minimal
allowlisted environment that can NEVER carry the supervisor's evidence MAC
secret — not ``ARGENT_EVIDENCE_MAC_KEY`` (the value), not
``ARGENT_EVIDENCE_MAC_KEY_FILE`` (the path), and not the resolved key bytes
under any other name.  ``PATH`` and other benign variables survive.

The G2 lens: the live service runs as a systemd user unit whose environment
(including ``EnvironmentFile``-injected ``ARGENT_EVIDENCE_MAC_KEY_FILE``) must
be cut off at the spawn boundary so a child agent can never read the key.
"""

from __future__ import annotations

import pytest

from argent_core.execution_scope import agent_spawn_env


def _seed_secret_env(monkeypatch):
    monkeypatch.setenv("ARGENT_EVIDENCE_MAC_KEY", "deadbeef" * 8)
    monkeypatch.setenv("ARGENT_EVIDENCE_MAC_KEY_FILE",
                       "/home/u/.config/argent/evidence_mac.key")
    monkeypatch.setenv("SOME_SECRET_TOKEN", "top-secret-value")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-canary")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/u")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")


# ---------------------------------------------------------------------------
# Stripping: the supervisor secret can never reach a child
# ---------------------------------------------------------------------------

def test_agent_env_strips_evidence_key_and_file(monkeypatch):
    _seed_secret_env(monkeypatch)
    env = agent_spawn_env()
    assert "ARGENT_EVIDENCE_MAC_KEY" not in env
    assert "ARGENT_EVIDENCE_MAC_KEY_FILE" not in env


def test_agent_env_strips_key_file_path_under_any_name(monkeypatch):
    # The key-file path (value) must not survive, whatever the carrier name.
    _seed_secret_env(monkeypatch)
    monkeypatch.setenv("ARGENT_KEYFILE", "/home/u/.config/argent/evidence_mac.key")
    env = agent_spawn_env()
    assert "ARGENT_KEYFILE" not in env
    # The resolved path string appears nowhere in the child environment.
    for value in env.values():
        assert "evidence_mac.key" not in str(value)


def test_agent_env_strips_all_non_allowlisted_vars(monkeypatch):
    _seed_secret_env(monkeypatch)
    monkeypatch.setenv("CUSTOM_VAR", "custom-value")
    env = agent_spawn_env()
    assert "SOME_SECRET_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "CUSTOM_VAR" not in env


def test_agent_env_keeps_allowlisted_benign_vars(monkeypatch):
    _seed_secret_env(monkeypatch)
    env = agent_spawn_env()
    assert env.get("PATH") == "/usr/bin:/bin"
    assert env.get("HOME") == "/home/u"
    assert env.get("XDG_RUNTIME_DIR") == "/run/user/1000"


def test_agent_env_empty_environment_is_safe():
    # With no secrets in the parent env, the child env is minimal and clean.
    env = agent_spawn_env()
    assert not any(k.startswith("ARGENT_EVIDENCE_") for k in env)
    # Every surviving key must be a member of the fixed allowlist.
    from argent_core.execution_scope import _AGENT_ENV_ALLOWLIST
    assert set(env) <= set(_AGENT_ENV_ALLOWLIST)


# ---------------------------------------------------------------------------
# ``extra`` injection is fail-closed on evidence keys
# ---------------------------------------------------------------------------

def test_agent_env_extra_refuses_evidence_key(monkeypatch):
    _seed_secret_env(monkeypatch)
    with pytest.raises(ValueError):
        agent_spawn_env(extra={"ARGENT_EVIDENCE_MAC_KEY": "injected"})
    with pytest.raises(ValueError):
        agent_spawn_env(extra={"ARGENT_EVIDENCE_MAC_KEY_FILE": "/injected/path"})


def test_agent_env_extra_allows_benign_override(monkeypatch):
    _seed_secret_env(monkeypatch)
    env = agent_spawn_env(extra={"PATH": "/opt/bin:/usr/bin"})
    assert env["PATH"] == "/opt/bin:/usr/bin"
    assert "ARGENT_EVIDENCE_MAC_KEY" not in env
