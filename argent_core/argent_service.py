"""Phase G1 — non-interactive background supervisor entrypoint (SPEC G1 §B).

A single ``main()`` that boots the durable background supervisor WITHOUT any
TUI and WITHOUT activating anything (no systemd enable/start/restart, no
systemd-manager reload for activation, no user lingering, no
``.wslconfig``/Windows startup, no cron/timer).  Activation is Phase G2 and is
owned by the operator.

Startup sequence (fail-closed; any unrecoverable init error exits non-zero):

1. Load the trusted service config (optional JSON file + strict env overrides).
2. Validate + create the canonical durable state directory.
3. Acquire a non-blocking advisory lock (defense-in-depth against two live
   supervisors on the same host).
4. Open/migrate the durable SQLite store (``Store`` migrations are idempotent).
5. Establish the single-active instance identity (SPEC G1 §C/§D) — a live or
   ambiguous owner refuses activation (no split-brain).
6. Reconcile previous state (Phase B ``Scheduler.reconcile_after_restart``).
7. Install SIGTERM/SIGINT handlers -> graceful shutdown (SPEC G1 §G).
8. Run the bounded background loop (SPEC G1 §I), then release the instance.

The evidence MAC key (Phase F) is resolved ONLY from the process environment
(``ARGENT_EVIDENCE_MAC_KEY_FILE`` / ``ARGENT_EVIDENCE_MAC_KEY``); it is never
embedded in this module, in the config file, or in the systemd unit, and the
agent can never choose it (SPEC G1 §M).
"""

from __future__ import annotations

import json
import math
import os
import signal
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional
from uuid import uuid4

from . import runtime_paths
from .background_runtime import (
    DEFAULT_INSTANCE_TTL_SECONDS,
    InstanceVerdict,
    ServiceHealth,
    SupervisorInstance,
    SupervisorRuntime,
)
from .core import Core
from .external_wait import ExternalWaitManager
from .scheduler import Scheduler
from .supervisor import (
    OpenClawRunLauncher,
    Supervisor,
    TrajectoryRunStatusProvider,
)

#: Process exit codes (SPEC G1 §B: non-zero on unrecoverable init error).
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INIT_ERROR = 2
EXIT_OWNER_CONFLICT = 3

#: Env var overrides for the canonical durable locations (never /tmp).
_ENV_STATE_DIR = "ARGENT_STATE_DIR"
_ENV_SHARE_DIR = "ARGENT_SHARE_DIR"
_ENV_CACHE_DIR = "ARGENT_CACHE_DIR"
_ENV_DB_PATH = "ARGENT_DB_PATH"

#: Ephemeral (tmpfs) prefixes that durable data must never live under.
_EPHEMERAL_ROOTS: tuple[str, ...] = ("/tmp", "/dev/shm", "/run")

#: Bounded stop_reason length persisted on release.
_MAX_STOP_REASON = 256


@dataclass(frozen=True)
class ServiceConfig:
    """Validated trusted service configuration (SPEC G1 §B)."""

    state_dir: Path
    share_dir: Path
    cache_dir: Path
    db_path: Path
    lease_ttl_seconds: int = DEFAULT_INSTANCE_TTL_SECONDS
    idle_sleep_seconds: float = 5.0
    active_sleep_seconds: float = 1.0
    heartbeat_every_passes: int = 10


def _refuse_ephemeral(path: Path, name: str) -> Path:
    """Fail-closed: refuse durable data under a tmpfs/ephemeral root.

    G1 (F5): resolves the symlink chain with ``Path.resolve()`` (NOT
    ``abspath``) so a symlink that ultimately lands under ``/tmp`` is refused,
    and returns the canonical (absolute) path.
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
                    f"{name} must not live under ephemeral location {root!r}"
                )
        except ValueError:
            raise
    return resolved


#: Closed set of allowed trusted-config keys (G1 F5: unknown keys fail-closed).
_ALLOWED_CONFIG_KEYS = frozenset({
    "state_dir", "share_dir", "cache_dir", "db_path",
    "lease_ttl_seconds", "idle_sleep_seconds", "active_sleep_seconds",
    "heartbeat_every_passes",
})


def _enforce_db_path_invariant(db_path: Path, state_dir: Path) -> None:
    """G1 (F5b): the durable DB must live under the state directory.

    A symlinked ``db_path`` pointing outside ``state_dir`` (e.g. into the agent
    write area) is refused.  Resolves both paths before comparing.
    """
    resolved_db = Path(os.path.expanduser(str(db_path))).resolve()
    resolved_state = Path(os.path.expanduser(str(state_dir))).resolve()
    if not (resolved_db == resolved_state
            or resolved_db.is_relative_to(resolved_state)):
        raise ValueError("db_path must live under the state directory")


def _coerce_dir(value: Optional[str], fallback: Path, name: str,
                reject_ephemeral: bool = True) -> Path:
    if value is None or not str(value).strip():
        return fallback
    if reject_ephemeral:
        return _refuse_ephemeral(Path(str(value)), name)
    return Path(str(value))


def _pos_int(value, default: int, name: str) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _pos_float(value, default: float, name: str) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive number")
    f = float(value)
    # G1 (F5): reject NaN/Infinity — a NaN idle/active sleep would busy-spin.
    if not math.isfinite(f) or f <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return f


def load_service_config(
    config_path: Optional[str] = None,
    *,
    home: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    reject_ephemeral: bool = True,
) -> ServiceConfig:
    """Load + strictly validate the trusted service config (SPEC G1 §B/§26).

    Precedence: explicit config JSON file -> env overrides -> canonical
    defaults (``runtime_paths``).  Any malformed/unknown/wrong-typed input
    fails closed with ``ValueError`` (never a silently-guessed default).

    ``reject_ephemeral`` (default True) refuses durable data under tmpfs roots
    (``/tmp``, ``/dev/shm``, ``/run``); deterministic tests that inject a
    ``tmp_path``-derived location pass ``False``.
    """
    env = env if env is not None else os.environ
    data: dict = {}
    if config_path:
        try:
            raw = Path(config_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot read service config: {exc}") from exc
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("malformed service config JSON") from exc
        if not isinstance(loaded, dict):
            raise ValueError("service config must be a JSON object")
        data = loaded
        # G1 (F5c): unknown config keys fail-closed (allowlist).
        unknown = set(data) - _ALLOWED_CONFIG_KEYS
        if unknown:
            raise ValueError(
                f"unknown service config keys: {sorted(unknown)}")

    def _cfg_str(key: str) -> Optional[str]:
        if key not in data:
            return None
        value = data[key]
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        return value

    state_dir = _coerce_dir(
        _cfg_str("state_dir"),
        runtime_paths.resolve_state_dir(home=home, env=env,
                                        reject_ephemeral=reject_ephemeral),
        "state_dir",
        reject_ephemeral=reject_ephemeral,
    )
    share_dir = _coerce_dir(
        _cfg_str("share_dir"),
        runtime_paths.resolve_share_dir(home=home, env=env,
                                        reject_ephemeral=reject_ephemeral),
        "share_dir",
        reject_ephemeral=reject_ephemeral,
    )
    cache_dir = _coerce_dir(
        _cfg_str("cache_dir"),
        runtime_paths.resolve_cache_dir(home=home, env=env,
                                        reject_ephemeral=reject_ephemeral),
        "cache_dir",
        reject_ephemeral=reject_ephemeral,
    )
    # Env overrides win over file config for the DB/state location (operator
    # escape hatch); both are still validated against ephemeral roots.
    state_dir = _coerce_dir(env.get(_ENV_STATE_DIR), state_dir, "state_dir",
                            reject_ephemeral=reject_ephemeral)
    share_dir = _coerce_dir(env.get(_ENV_SHARE_DIR), share_dir, "share_dir",
                            reject_ephemeral=reject_ephemeral)
    cache_dir = _coerce_dir(env.get(_ENV_CACHE_DIR), cache_dir, "cache_dir",
                            reject_ephemeral=reject_ephemeral)
    db_path = _coerce_dir(
        env.get(_ENV_DB_PATH) or _cfg_str("db_path"),
        state_dir / "argent.db",
        "db_path",
        reject_ephemeral=reject_ephemeral,
    )
    # G1 (F5b): the durable DB must live under the canonical state directory
    # (a symlinked / foreign ``ARGENT_DB_PATH`` — e.g. into the agent worktree —
    # is refused).
    _enforce_db_path_invariant(db_path, state_dir)

    lease_ttl = _pos_int(data.get("lease_ttl_seconds"), DEFAULT_INSTANCE_TTL_SECONDS,
                         "lease_ttl_seconds")
    idle_sleep = _pos_float(data.get("idle_sleep_seconds"), 5.0, "idle_sleep_seconds")
    active_sleep = _pos_float(data.get("active_sleep_seconds"), 1.0,
                              "active_sleep_seconds")
    heartbeat = _pos_int(data.get("heartbeat_every_passes"), 10,
                         "heartbeat_every_passes")

    return ServiceConfig(
        state_dir=state_dir,
        share_dir=share_dir,
        cache_dir=cache_dir,
        db_path=db_path,
        lease_ttl_seconds=lease_ttl,
        idle_sleep_seconds=idle_sleep,
        active_sleep_seconds=active_sleep,
        heartbeat_every_passes=heartbeat,
    )


@dataclass
class ServiceRuntime:
    """Fully assembled (but NOT yet loop-running) background service."""

    config: ServiceConfig
    core: Core
    instance: SupervisorInstance
    runtime: SupervisorRuntime
    acquire_result: "object"


def _acquire_lock(state_dir: Path):
    """Best-effort non-blocking advisory lock (defense-in-depth, SPEC G1 §C).

    Returns an open file handle holding ``LOCK_EX | LOCK_NB``, or ``None`` when
    another supervisor already holds it.  On platforms without ``fcntl`` this
    returns a sentinel (the persisted CAS remains the authority).
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX fallback
        return object()
    lock_path = Path(state_dir) / "supervisor.lock"
    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def build_service(
    config: ServiceConfig,
    *,
    clock: Optional[Callable[[], datetime]] = None,
    identity_provider=None,
    pid_alive: Optional[Callable[[int], Optional[bool]]] = None,
    supervisor=None,
    scheduler=None,
    external_wait_manager=None,
    instance=None,
    sleep_fn: Optional[Callable[[float], None]] = None,
    max_passes: Optional[int] = None,
) -> ServiceRuntime:
    """Assemble the service (store + supervisor + instance + runtime).

    All real objects may be overridden with fakes for deterministic offline
    tests; the defaults build the production wiring (which never activates a
    systemd unit or network listener on construction).
    """
    runtime_paths.validate_state_dir(config.state_dir)
    core = Core(str(config.db_path), clock=clock)
    clock = clock or core._store._clock
    supervisor = supervisor or Supervisor(
        core,
        TrajectoryRunStatusProvider(),
        run_launcher=OpenClawRunLauncher(),
        clock=clock,
    )
    instance_id = "instance:" + uuid4().hex
    scheduler = scheduler or Scheduler(
        supervisor,
        owner_instance_id=instance_id,
        lease_ttl_seconds=config.lease_ttl_seconds,
    )
    external_wait_manager = external_wait_manager or ExternalWaitManager(
        core._store, adapters={}, clock=clock,
    )
    instance = instance or SupervisorInstance(
        core._store,
        identity_provider=identity_provider,
        instance_id=instance_id,
        pid_alive=pid_alive,
        clock=clock,
        ttl_seconds=config.lease_ttl_seconds,
    )
    runtime = SupervisorRuntime(
        scheduler=scheduler,
        external_wait_manager=external_wait_manager,
        instance=instance,
        store=core._store,
        clock=clock,
        sleep_fn=sleep_fn,
        idle_sleep_seconds=config.idle_sleep_seconds,
        active_sleep_seconds=config.active_sleep_seconds,
        heartbeat_every_passes=config.heartbeat_every_passes,
        max_passes=max_passes,
    )
    return ServiceRuntime(
        config=config, core=core, instance=instance,
        runtime=runtime, acquire_result=None,
    )


def _parse_args(argv):
    import argparse

    parser = argparse.ArgumentParser(
        prog="argent-supervisor", description="Argent background supervisor",
    )
    parser.add_argument(
        "--config", metavar="PATH", default=None,
        help="optional trusted service config JSON",
    )
    parser.add_argument(
        "--max-passes", metavar="N", type=int, default=None,
        help="run at most N scheduler passes then exit (testing)",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    """Non-interactive entrypoint (SPEC G1 §B)."""
    args = _parse_args(argv)
    try:
        config = load_service_config(args.config)
    except ValueError as exc:
        print(f"argent-supervisor: fatal: {exc}", file=sys.stderr)
        return EXIT_INIT_ERROR
    try:
        runtime_paths.validate_state_dir(config.state_dir)
    except OSError as exc:
        print(f"argent-supervisor: fatal: {exc}", file=sys.stderr)
        return EXIT_INIT_ERROR

    lock = _acquire_lock(config.state_dir)
    if lock is None:
        print(
            "argent-supervisor: fatal: another supervisor holds the lock",
            file=sys.stderr,
        )
        return EXIT_OWNER_CONFLICT

    try:
        try:
            svc = build_service(config, max_passes=args.max_passes)
        except Exception as exc:  # noqa: BLE001 - unrecoverable init error
            print(f"argent-supervisor: fatal: {exc}", file=sys.stderr)
            return EXIT_INIT_ERROR

        result = svc.instance.acquire()
        if result.verdict in (InstanceVerdict.LIVE_OWNER, InstanceVerdict.AMBIGUOUS):
            print(
                f"argent-supervisor: fatal: cannot become single active "
                f"supervisor ({result.verdict.value}: {result.detail})",
                file=sys.stderr,
            )
            svc.core.close()
            return EXIT_OWNER_CONFLICT
        svc.acquire_result = result

        # Startup reconciliation (SPEC G1 §E) — reuse the Phase B mechanism.
        try:
            summary = svc.runtime.scheduler.reconcile_after_restart()
            svc.runtime.set_recovery_result(summary)
        except Exception as exc:  # noqa: BLE001
            print(
                f"argent-supervisor: fatal: startup reconciliation failed: {exc}",
                file=sys.stderr,
            )
            svc.core.close()
            return EXIT_INIT_ERROR

        _install_signal_handlers(svc.runtime)
        svc.runtime.run_loop()
        # G1 (F7b): a FAILED service exits non-zero so the ``Restart=on-failure``
        # policy can react; a normal STOPPING/READY exit stays 0.
        exit_code = EXIT_FAILED if svc.runtime.state is ServiceHealth.FAILED \
            else EXIT_OK
        svc.core.close()
        return exit_code
    finally:
        # Release the advisory lock (the instance release already happened in
        # the runtime's finalize).
        if lock is not None and hasattr(lock, "close"):
            try:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except Exception:  # noqa: BLE001
                pass
            lock.close()


def _install_signal_handlers(runtime: SupervisorRuntime) -> None:
    """Install SIGTERM/SIGINT -> graceful shutdown (SPEC G1 §G)."""

    def _handler(signum, _frame):
        runtime.request_shutdown(
            "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        )

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):  # not in main thread (tests)
            pass


if __name__ == "__main__":  # pragma: no cover - entrypoint
    sys.exit(main())
