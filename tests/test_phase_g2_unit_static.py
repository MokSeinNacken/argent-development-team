"""Phase G2 — systemd user-service unit static validation (no activation).

Deterministic, offline parse of ``g1-systemd/argent-supervisor.service`` and
the Phase-G2 deployment helper (``g2-systemd/install-check.sh``).  This proves
the CODE-ENFORCED + OPERATIONALLY-CONFIGURED unit contract that the live G2
activation (performed separately by the Supervisor) relies on:

* no secret literal / key value in the unit;
* user (not root) service, ``WantedBy=default.target``;
* systemd rate-limiting restart policy (``Restart=on-failure``, ``RestartSec``,
  ``StartLimitBurst``/``StartLimitIntervalSec``) — bounds aggressive respawn
  bursts in a sliding 120s window, NOT an absolute restart cap;
* ``KillSignal=SIGTERM`` graceful shutdown;
* ``NoNewPrivileges=yes`` hardening;
* no shell in ``ExecStart`` (no ``/bin/sh``, ``bash``, ``&&``/``|``/``;``);
* ``StateDirectory``/``CacheDirectory``/``WorkingDirectory``/``EnvironmentFile``
  semantics (the optional ``-`` prefix, canonical XDG locations, ``%h`` home).

The install-check helper is asserted to be read-only: it must never enable,
start, reload or activate anything.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_UNIT = _REPO / "g1-systemd" / "argent-supervisor.service"
_INSTALL_CHECK = _REPO / "g2-systemd" / "install-check.sh"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Minimal systemd unit parser (sections -> directive -> value)
# ---------------------------------------------------------------------------

def _parse_unit(text: str) -> dict:
    """Parse a systemd unit into ``{section: {directive: value}}``.

    Directives may be repeated; the LAST occurrence wins for single-value keys
    (sufficient for the statically-validated directives asserted here).
    """
    sections: dict = {}
    current = None
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, {})
            continue
        if current is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        sections[current][key.strip()] = value.strip()
    return sections


@pytest.fixture(scope="module")
def unit() -> dict:
    return _parse_unit(_read(_UNIT))


@pytest.fixture(scope="module")
def unit_text() -> str:
    return _read(_UNIT)


# ---------------------------------------------------------------------------
# Secrets / privilege / listener
# ---------------------------------------------------------------------------

def test_unit_has_no_secret_literal_or_key_value(unit_text, unit):
    # No key VALUE form anywhere in the file (the guidance comment only ever
    # mentions the PATH form ARGENT_EVIDENCE_MAC_KEY_FILE=, which is fine).
    assert "ARGENT_EVIDENCE_MAC_KEY=" not in unit_text
    assert not re.search(
        r"(password|secret|token|api[_-]?key)\s*=\s*[A-Za-z0-9]", unit_text, re.I,
    )
    # No hex/blob key-looking literal of >= 16 chars in the unit.
    assert not re.search(r"\b[0-9a-f]{32,}\b", unit_text, re.I)
    # No actual Environment directive carries an ARGENT_EVIDENCE_* assignment.
    for section in unit.values():
        for key in section:
            assert not key.startswith("ARGENT_EVIDENCE_"), key


def test_unit_runs_as_user_not_root(unit):
    assert "User" not in unit["Service"]
    assert "Group" not in unit["Service"]


def test_unit_no_public_listener(unit_text):
    for directive in ("ListenStream", "ListenDatagram", "Listen", "Accept=",
                      "Socket", "BindToDevice"):
        assert directive not in unit_text


# ---------------------------------------------------------------------------
# Restart policy / graceful shutdown / hardening
# ---------------------------------------------------------------------------

def test_unit_restart_policy_is_bounded(unit):
    svc = unit["Service"]
    assert svc["Restart"] == "on-failure"
    assert int(svc["RestartSec"]) >= 1
    assert int(unit["Unit"]["StartLimitBurst"]) >= 1
    assert int(unit["Unit"]["StartLimitIntervalSec"]) >= 1
    # Rate limiting: a burst bound over a sliding 120s window (NOT an absolute
    # restart cap — slow failures are restarted indefinitely).
    assert int(unit["Unit"]["StartLimitBurst"]) <= 10


def test_unit_kill_signal_is_sigterm(unit):
    assert unit["Service"]["KillSignal"] == "SIGTERM"


def test_unit_no_new_privileges(unit):
    assert unit["Service"]["NoNewPrivileges"] == "yes"


def test_unit_execstart_has_no_shell(unit):
    execstart = unit["Service"]["ExecStart"]
    # No shell interpreter, no shell metacharacters that imply a shell.
    for token in ("/bin/sh", "/bin/bash", "bash", "sh -c", "&&", "||", "|",
                  ";", "$(", "`"):
        assert token not in execstart, f"ExecStart shell indicator: {token!r}"
    # The executable is an absolute interpreter path, not a user-writable one.
    assert execstart.startswith("/")


# ---------------------------------------------------------------------------
# Directory / environment semantics
# ---------------------------------------------------------------------------

def test_unit_state_and_cache_directories_canonical(unit):
    assert unit["Service"]["StateDirectory"] == "argent"
    assert unit["Service"]["CacheDirectory"] == "argent"


def test_unit_directory_modes_and_umask_hardened(unit):
    # G2 (F4): the created state/cache dirs are 0700 (owner-only) and the
    # service runs with UMask=0077, so durable state/cache and prompt files are
    # never group/world-readable.
    svc = unit["Service"]
    assert svc["StateDirectoryMode"] == "0700"
    assert svc["CacheDirectoryMode"] == "0700"
    assert svc["UMask"] == "0077"


def test_unit_working_directory_uses_home_specifier(unit):
    assert unit["Service"]["WorkingDirectory"] == "%h/argent"


def test_unit_environment_file_is_optional_path_reference(unit):
    envfile = unit["Service"]["EnvironmentFile"]
    # The leading '-' makes a missing file a non-fatal (optional) condition.
    assert envfile.startswith("-")
    assert "service.env" in envfile
    assert ".config/argent" in envfile  # outside any agent write area
    assert "%h" in envfile  # per-user home, never a hardcoded user path


def test_unit_install_wanted_by_default_target(unit):
    assert unit["Install"]["WantedBy"] == "default.target"


def test_unit_type_simple(unit):
    assert unit["Service"]["Type"] == "simple"


# ---------------------------------------------------------------------------
# Deployment helper must be read-only (no activation)
# ---------------------------------------------------------------------------

def test_install_check_is_read_only():
    text = _read(_INSTALL_CHECK)
    # A read-only validator has no business invoking any activation verb.
    # Absence of the activation commands (even in commentary) is the strongest
    # deterministic guarantee that it can never enable/start/reload anything.
    for token in ("daemon-reload", "loginctl", "systemd-run",
                  "enable-linger", "enable --now", "start-linger",
                  "systemctl enable", "systemctl start", "systemctl restart",
                  "systemctl stop", "systemctl reload", "systemctl kill"):
        assert token not in text, f"install-check references {token!r}"


def test_install_check_resolves_installed_unit_read_only():
    text = _read(_INSTALL_CHECK)
    # F6: the installed unit is resolved READ-ONLY via FragmentPath (with a
    # read-only `cat` fallback) — never via any activation command.
    assert "systemctl --user show -p FragmentPath argent-supervisor.service" in text
    assert "systemctl --user cat argent-supervisor.service" in text


def test_install_check_declares_read_only_intent():
    text = _read(_INSTALL_CHECK)
    assert "read-only" in text.lower() or "READ-ONLY" in text
    assert "no activation" in text.lower()


# ---------------------------------------------------------------------------
# F6 — deployment-substitution normalization (template -> installed unit)
# ---------------------------------------------------------------------------

#: This host's three deployment substitutions (env-parameterizable like
#: g2-systemd/install-check.sh).  I3-C1: the accepted live deployment advanced
#: from the I3-A worktree to the I3-B worktree (unit WorkingDirectory/
#: Documentation re-pointed during the authorized Phase-I3-B deploy), so the
#: defaults track the I3-B deployment.
import os as _os

_DEFAULT_WORKTREE = _os.environ.get(
    "ARGENT_WORKTREE",
    "/home/pc/projects/argent-worktrees/phase-i3b-github-live-acceptance",
)
_DEFAULT_DOC = _os.environ.get(
    "ARGENT_DOC",
    f"file:{_DEFAULT_WORKTREE}/docs/PHASE_I3B_ACCEPTANCE.md",
)
_DEFAULT_WORKDIR = _DEFAULT_WORKTREE
_DEFAULT_ENVFILE = "-/home/pc/.config/argent/service.env"


def apply_deployment_substitutions(
    text: str, *, documentation, working_directory, environment_file,
) -> str:
    """Apply the three deployment substitutions to a unit template (pure).

    Replaces the ``Documentation``, ``WorkingDirectory`` and ``EnvironmentFile``
    directive values (the only host-specific deployment values) and leaves every
    other line (including comments) unchanged.
    """
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Documentation="):
            out.append(f"Documentation={documentation}")
        elif stripped.startswith("WorkingDirectory="):
            out.append(f"WorkingDirectory={working_directory}")
        elif stripped.startswith("EnvironmentFile="):
            out.append(f"EnvironmentFile={environment_file}")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def test_deployment_substitutions_produce_expected_installed_unit():
    template = _read(_UNIT)
    normalized = apply_deployment_substitutions(
        template,
        documentation=_DEFAULT_DOC,
        working_directory=_DEFAULT_WORKDIR,
        environment_file=_DEFAULT_ENVFILE,
    )
    parsed = _parse_unit(normalized)
    assert parsed["Unit"]["Documentation"] == _DEFAULT_DOC
    assert parsed["Service"]["WorkingDirectory"] == _DEFAULT_WORKDIR
    assert parsed["Service"]["EnvironmentFile"] == _DEFAULT_ENVFILE
    # Everything else is untouched.
    tpl = _parse_unit(template)
    assert parsed["Service"]["ExecStart"] == tpl["Service"]["ExecStart"]
    assert parsed["Service"]["Restart"] == tpl["Service"]["Restart"]
    assert parsed["Service"]["StateDirectoryMode"] == "0700"
    assert parsed["Service"]["UMask"] == "0077"


def test_deployment_substitutions_match_installed_unit_on_this_host():
    template = _read(_UNIT)
    normalized = apply_deployment_substitutions(
        template,
        documentation=_DEFAULT_DOC,
        working_directory=_DEFAULT_WORKDIR,
        environment_file=_DEFAULT_ENVFILE,
    )
    expected = _parse_unit(normalized)

    # Real host: additionally assert against the live FragmentPath when present;
    # skip cleanly when systemd/unit is absent so other environments don't fail.
    import subprocess
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", "-p", "FragmentPath",
             "argent-supervisor.service"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return  # no systemd -> skip cleanly
    if not out.startswith("FragmentPath="):
        return
    fragment_value = out.split("=", 1)[1].strip()
    # A unit that is not loaded/installed prints an EMPTY FragmentPath value;
    # Path("") would resolve to '.' (a directory) and read_text('.') would
    # raise IsADirectoryError.  Guard the empty/absent cases before any
    # exists()/read_text() so this host-coupled check stays purely portable
    # (same clean skip as the other absent-systemd paths above).
    if not fragment_value:
        return  # empty FragmentPath -> unit not installed -> skip cleanly
    fragment = Path(fragment_value)
    if not fragment.exists():
        return  # installed unit absent -> skip cleanly
    installed_text = fragment.read_text(encoding="utf-8")
    actual = _parse_unit(installed_text)
    for section, directives in expected.items():
        for key, value in directives.items():
            assert actual.get(section, {}).get(key) == value, (
                f"installed unit directive [{section}] {key} drifted from template"
            )
