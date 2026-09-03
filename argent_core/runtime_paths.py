"""Phase G1 — canonical runtime paths (SPEC G1 §A).

The background supervisor must never write durable data under ``/tmp`` (a
tmpfs ENOSPC incident during Phase B motivated this rule).  Instead it uses the
XDG base directories (when set) or their well-known ``~/.local/...`` fallbacks,
always resolved per-``Path.home()`` — never a hardcoded ``/home/<user>``.

Canonical locations (SPEC G1 §A):

* Persistent state   -> ``$XDG_STATE_HOME/argent`` (else ``~/.local/state/argent``)
* Persistent artifacts -> ``$XDG_DATA_HOME/argent`` (else ``~/.local/share/argent``)
* Cache              -> ``$XDG_CACHE_HOME/argent`` (else ``~/.cache/argent``)

All functions accept an injectable ``home``/``env`` for deterministic offline
tests; production callers pass neither (``Path.home()`` / ``os.environ`` are
used).  No function here ever touches ``/tmp`` or a hardcoded user path.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

#: Default XDG base (the spec-mandated fallback base under ``Path.home()``).
_DEFAULT_STATE_HOME = ".local/state"
_DEFAULT_DATA_HOME = ".local/share"
_DEFAULT_CACHE_HOME = ".cache"
_DEFAULT_CONFIG_HOME = ".config"

#: Ephemeral (tmpfs) prefixes that durable data must never live under.
_EPHEMERAL_ROOTS: tuple[str, ...] = ("/tmp", "/dev/shm", "/run")


def _refuse_ephemeral(path: Path, name: str) -> Path:
    """Resolve symlinks/relative segments and refuse tmpfs/ephemeral roots.

    G1 (F5): uses ``Path.resolve()`` (NOT ``abspath``) so a symlink chain that
    ultimately lands under ``/tmp`` is refused; the returned path is canonical
    (absolute, symlinks collapsed).  Any path under an ephemeral root raises
    ``ValueError`` (fail-closed).
    """
    try:
        resolved = Path(os.path.expanduser(str(path))).resolve()
    except (OSError, ValueError):
        resolved = Path(os.path.expanduser(str(path)))
    for root in _EPHEMERAL_ROOTS:
        rroot = Path(root).resolve()
        try:
            if resolved == rroot or resolved.is_relative_to(rroot):
                raise ValueError(
                    f"{name} must not live under ephemeral location {root!r}")
        except ValueError:
            raise
    return resolved


def _base(
    env_name: str,
    fallback_rel: str,
    *,
    home: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    reject_ephemeral: bool = True,
) -> Path:
    """Resolve one XDG base directory (env override, else ``home/<rel>``).

    G1 (F5): the override (and the fallback) is resolved to a canonical path
    and refused if it lands under an ephemeral root; a relative override is
    resolved under ``home`` (never against the process cwd).  ``reject_ephemeral``
    (default True) disables the refusal for deterministic tests that inject a
    ``tmp_path``-derived ``home``/override.
    """
    if env is None:
        env = os.environ
    base_home = Path(home) if home is not None else Path.home()
    explicit = env.get(env_name)
    if explicit:
        p = Path(str(explicit))
        if not p.is_absolute():
            p = base_home / p
        if reject_ephemeral:
            return _refuse_ephemeral(p, env_name)
        return p
    if reject_ephemeral:
        return _refuse_ephemeral(base_home / fallback_rel, env_name)
    return base_home / fallback_rel


def resolve_state_dir(
    *, home: Optional[Path] = None, env: Optional[Mapping[str, str]] = None,
    reject_ephemeral: bool = True,
) -> Path:
    """Persistent state directory (``~/.local/state/argent`` or XDG)."""
    base = _base("XDG_STATE_HOME", _DEFAULT_STATE_HOME, home=home, env=env,
                 reject_ephemeral=reject_ephemeral)
    full = base / "argent"
    if reject_ephemeral:
        return _refuse_ephemeral(full, "XDG_STATE_HOME/argent")
    return full


def resolve_config_dir(
    *, home: Optional[Path] = None, env: Optional[Mapping[str, str]] = None,
    reject_ephemeral: bool = True,
) -> Path:
    """Trusted config directory (``~/.config/argent`` or XDG_CONFIG_HOME).

    G2 (F1): one of the two TRUSTED directories that untrusted agent children
    (same UID) must never reach through normal agent execution.  It holds the
    evidence MAC key and ``service.env`` (the protected operator secrets), so
    the agent-dispatch sandbox masks it with an empty tmpfs.  Production
    callers pass neither ``home`` nor ``env`` (canonical, refused under ``/tmp``).
    """
    base = _base("XDG_CONFIG_HOME", _DEFAULT_CONFIG_HOME, home=home, env=env,
                 reject_ephemeral=reject_ephemeral)
    full = base / "argent"
    if reject_ephemeral:
        return _refuse_ephemeral(full, "XDG_CONFIG_HOME/argent")
    return full


def resolve_share_dir(
    *, home: Optional[Path] = None, env: Optional[Mapping[str, str]] = None,
    reject_ephemeral: bool = True,
) -> Path:
    """Persistent artifacts directory (``~/.local/share/argent`` or XDG)."""
    base = _base("XDG_DATA_HOME", _DEFAULT_DATA_HOME, home=home, env=env,
                 reject_ephemeral=reject_ephemeral)
    full = base / "argent"
    if reject_ephemeral:
        return _refuse_ephemeral(full, "XDG_DATA_HOME/argent")
    return full


def resolve_cache_dir(
    *, home: Optional[Path] = None, env: Optional[Mapping[str, str]] = None,
    reject_ephemeral: bool = True,
) -> Path:
    """Cache directory (``~/.cache/argent`` or XDG)."""
    base = _base("XDG_CACHE_HOME", _DEFAULT_CACHE_HOME, home=home, env=env,
                 reject_ephemeral=reject_ephemeral)
    full = base / "argent"
    if reject_ephemeral:
        return _refuse_ephemeral(full, "XDG_CACHE_HOME/argent")
    return full


#: Subdirectory (under the canonical cache dir) that holds per-dispatch agent
#: prompt message files (Phase G2 F3).  These are ephemeral, bounded, and
#: cleaned up by a bounded sweep — they must NEVER live under ``/tmp``.
PROMPTS_SUBDIR = "prompts"


def resolve_prompts_dir(
    *, home: Optional[Path] = None, env: Optional[Mapping[str, str]] = None,
    reject_ephemeral: bool = True,
) -> Path:
    """Directory for ephemeral agent-prompt message files (``cache/prompts``).

    G2 (F3): prompt files previously written via ``tempfile.mkstemp()`` under
    ``/tmp`` (a tmpfs — ENOSPC risk) never cleaned up.  They now live in a
    bounded subdirectory of the canonical CACHE dir (never an ephemeral root),
    so they are outside any agent write area and are swept by a bounded
    age/count cleanup.  Production callers pass neither ``home`` nor ``env``;
    the returned path is canonical and refused when it resolves under ``/tmp``.
    """
    return resolve_cache_dir(home=home, env=env,
                             reject_ephemeral=reject_ephemeral) / PROMPTS_SUBDIR


def default_db_path(
    *, home: Optional[Path] = None, env: Optional[Mapping[str, str]] = None
) -> Path:
    """The durable SQLite store path under the state directory."""
    return resolve_state_dir(home=home, env=env) / "argent.db"


def default_instance_lock_path(
    *, home: Optional[Path] = None, env: Optional[Mapping[str, str]] = None
) -> Path:
    """Advisory lock file path (defense-in-depth against two live supervisors)."""
    return resolve_state_dir(home=home, env=env) / "supervisor.lock"


def default_health_path(
    *, home: Optional[Path] = None, env: Optional[Mapping[str, str]] = None
) -> Path:
    """Optional machine-readable service health file (SPEC G1 §J)."""
    return resolve_state_dir(home=home, env=env) / "health.json"


def validate_state_dir(state_dir: Path) -> None:
    """Ensure the persistent state directory exists and is writable.

    Raises ``OSError`` (fail-closed) when it cannot be created or is not a
    directory.  This is the earliest possible durable-location check in the
    service entrypoint; an unrecoverable failure must exit non-zero.
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    if not state_dir.is_dir():
        raise OSError(f"state dir is not a directory: {state_dir}")
