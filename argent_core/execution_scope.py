"""Phase C2 — bounded execution scopes (systemd-run --user --scope + cgroup v2).

This is the execution core of the Resource Governor (ARGENT ARCHITECTURE
V1 FINAL §9).  It wraps a resource-relevant process in a transient,
unprivileged systemd **scope** (cgroup v2) so the ceilings computed by the C1
admission decision become *enforced* memory/CPU limits — not just a decision
basis.

Key pieces:

* :class:`ExecutionScope` — frozen record of one scope (name, unit, cgroup path,
  effective limits, process identity, verification state).
* scope **naming** helpers — locally generated, strictly validated
  ``[a-z0-9-]`` names (``<= 64`` chars); agent text can NEVER name a scope.
* pure **property translation** and **limit validation** (fail-closed).
* :class:`ExecutionScopeBackend` — the scope lifecycle protocol
  (create / verify / start / cleanup / terminate) with the **Start-Barrier**
  ordering (F2).
* :class:`SystemdRunScopeBackend` — the real implementation via
  ``systemd-run --user --scope`` + ``systemctl --user show`` + cgroupfs
  read-back.  No root, no shell, no system-wide change; only transient scopes.

Live host measurements (verified 2026-09-01, read-only) that this backend is
built on:

* ``systemd-run --user --scope`` works UNPRIVILEGED; the scope is created, the
  properties are applied, and systemd auto-cleans the transient scope when the
  scoped command exits (0 loaded units afterwards).
* ``systemd-run --scope`` EXECs the command: the ``Popen.pid`` of the
  ``systemd-run`` subprocess becomes the scoped command process, whose
  ``/proc/<pid>/cgroup`` shows the scope's cgroup path and whose
  ``/proc/<pid>/stat`` field 22 yields the start ticks.
* Properties read back via ``systemctl --user show <scope>`` (MemoryMax,
  MemoryHigh, MemorySwapMax, CPUQuotaPerSecUSec, TasksMax, ControlGroup) AND
  directly from ``/sys/fs/cgroup/<ControlGroup>/memory.max`` /
  ``memory.high`` / ``memory.swap.max`` / ``cpu.max`` / ``pids.max``.
* A process can be moved into the scope's cgroup with an unprivileged
  ``cgroup.procs`` write (the scope belongs to the user) — the Start-Barrier
  mechanism that keeps the scope alive while the agent is bound and verified.
* CPUQuota 100% -> ``CPUQuotaPerSecUSec=1s`` and ``cpu.max="100000 100000"``.
"""

from __future__ import annotations

import os
import re
import secrets
import signal
import subprocess
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Mapping, Optional, Sequence

from .resource_policy import ResourceClass, ResourcePolicy

# ---------------------------------------------------------------------------
# Agent-spawn environment allowlist (G1 F4)
# ---------------------------------------------------------------------------

#: A fixed allowlist of environment variable NAMES that a spawned agent/command
#: may inherit.  Everything else — crucially ``ARGENT_EVIDENCE_MAC_KEY``,
#: ``ARGENT_EVIDENCE_MAC_KEY_FILE`` and any evidence-key-file path — is
#: STRIPPED so a child process can never see the supervisor's secrets.
#: ``XDG_RUNTIME_DIR``/``DBUS_SESSION_BUS_ADDRESS`` are retained for
#: ``systemd-run --user`` (scope creation); they carry no secrets.
_AGENT_ENV_ALLOWLIST: frozenset[str] = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL",
    "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "LC_NUMERIC", "LC_TIME",
    "TZ", "TERM", "NO_COLOR", "FORCE_COLOR", "CLICOLOR",
    "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONUNBUFFERED",
    "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
    "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
})


def agent_spawn_env(extra: Optional[Mapping[str, str]] = None) -> dict:
    """Build a minimal, allowlisted environment for a spawned agent/command.

    Copies only ``_AGENT_ENV_ALLOWLIST`` names from ``os.environ`` so secrets
    (``ARGENT_EVIDENCE_*`` and the key-file path) can never leak to a child.
    ``extra`` (if any) is merged LAST; any ``ARGENT_EVIDENCE_*`` key in it is
    refused (fail-closed).
    """
    env = {k: v for k, v in os.environ.items() if k in _AGENT_ENV_ALLOWLIST}
    if extra:
        for k, v in extra.items():
            if k.startswith("ARGENT_EVIDENCE_"):
                raise ValueError(f"refusing to inject {k!r} into agent env")
            env[k] = v
    return env


# ---------------------------------------------------------------------------
# Scope naming (locally generated, strictly validated)
# ---------------------------------------------------------------------------

#: Fixed prefix for every generated scope name (also the smoke-test prefix).
SCOPE_NAME_PREFIX = "argent-c2"

#: Upper bound for a scope name length (systemd unit names must stay short).
SCOPE_NAME_MAX_LEN = 64

#: Allowed charset: lowercase alphanumerics + hyphens, no leading/trailing hyphen.
_SCOPE_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")

#: Default TasksMax for a scope (fork-bomb guard; not a policy ceiling).
DEFAULT_TASKS_MAX = 64

#: ``timeout -k`` grace: SIGTERM, then SIGKILL after this many seconds.
TIMEOUT_KILL_AFTER_SECONDS = 10

#: Harmless placeholder process for the Start-Barrier (F2).  The scope is
#: created with this placeholder (NOT the agent), verified, then the real
#: agent is moved into the verified scope's cgroup before the placeholder is
#: terminated.  A BOUNDED sleep (600s) is used instead of ``sleep infinity`` so
#: a crash between agent-bind and placeholder-kill can never leak an immortal
#: scope; 600s is >> the sub-second verification window.
PLACEHOLDER_COMMAND: tuple[str, ...] = ("sleep", "600")

#: ``memory.events`` keys captured as bounded termination evidence (F5).
MEMORY_EVENTS_KEYS: tuple[str, ...] = ("oom_kill", "oom_group_kill", "max", "high")

#: Bounded output capture for synchronous ``run_in_scope`` (never a huge dump).
MAX_RUN_OUTPUT_BYTES = 64 * 1024

#: Verification statuses (closed set).
VERIFICATION_PENDING = "PENDING"
VERIFICATION_VERIFIED = "VERIFIED"
VERIFICATION_FAILED = "FAILED"
VERIFICATION_UNKNOWN = "UNKNOWN"


def is_valid_scope_name(name: str) -> bool:
    """True iff ``name`` is a safe scope name (fail-closed, never lenient)."""
    if not isinstance(name, str):
        return False
    if len(name) < 1 or len(name) > SCOPE_NAME_MAX_LEN:
        return False
    return bool(_SCOPE_NAME_RE.match(name))


def sanitize_scope_name(raw) -> str:
    """Defensively reduce arbitrary input to a safe ``[a-z0-9-]`` chunk.

    Never raises; returns ``""`` when nothing usable remains.  Production scope
    names are always generated by :func:`generate_scope_name` — this helper is
    a fail-closed reducer for defensive reuse, never an authority.
    """
    if not isinstance(raw, str):
        return ""
    s = raw.lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:SCOPE_NAME_MAX_LEN]


def _safe_chunk(raw: Optional[str], n: int) -> str:
    """Reduce ``raw`` to a ``[a-z0-9]`` chunk of at most ``n`` chars (never empty)."""
    s = sanitize_scope_name(raw or "").replace("-", "")
    return s[:n] if s else "0"


def generate_scope_name(job_id: Optional[str], dispatch_id: Optional[str]) -> str:
    """Generate a safe, collision-resistant scope name from LOCAL identifiers.

    Layout: ``argent-c2-<shortjob>-<shortdispatch>-<randomhex>`` (all
    ``[a-z0-9-]``, ``<= 64``).  The inputs are local job/dispatch ids — agent
    text can never supply them.  The random hex makes the name unique per
    spawn attempt (so a retry never collides with a still-cleaning scope).
    """
    name = "-".join((
        SCOPE_NAME_PREFIX,
        _safe_chunk(job_id, 8),
        _safe_chunk(dispatch_id, 8),
        secrets.token_hex(4),
    ))
    # Guarantee the invariant even if the layout ever changes.
    if not is_valid_scope_name(name):
        name = f"{SCOPE_NAME_PREFIX}-{secrets.token_hex(8)}"
    return name


# ---------------------------------------------------------------------------
# Limit validation + property translation (pure, fail-closed)
# ---------------------------------------------------------------------------


def _is_positive_int(value) -> bool:
    """True for a strictly positive finite ``int`` (rejects bool/float/inf/None)."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate_effective_limits(
    effective_limits: Mapping,
    *,
    resource_class,
    policy: ResourcePolicy,
) -> dict:
    """Validate and canonicalise enforcement limits; raises ``ValueError``.

    Enforced invariants (all fail-closed):

    * every limit is a strictly positive finite ``int`` (no ``None``/negative/
      ``inf``/``NaN``/bool);
    * ``MemoryHigh <= MemoryMax <= class ceiling``;
    * ``SwapMax <= class ceiling``;
    * ``CPUQuota <= class ceiling``;
    * ``timeout`` is ``> 0`` and ``<=`` the policy's largest class timeout.

    Returns a canonical dict with exactly the five keys.  Any violation raises
    :class:`ValueError` (the enforcer maps it to a bounded fail-closed result —
    never a silently adjusted limit).
    """
    if isinstance(resource_class, ResourceClass):
        rc = resource_class
    else:
        rc = ResourceClass(resource_class)  # raises ValueError on unknown
    if not isinstance(effective_limits, Mapping):
        raise ValueError("effective_limits must be a mapping")

    limits = policy.limits_for(rc)

    def _get(name):
        value = effective_limits.get(name)
        if not _is_positive_int(value):
            raise ValueError(f"invalid {name}: {value!r}")
        return int(value)

    memory_high = _get("memory_high_bytes")
    memory_max = _get("memory_max_bytes")
    swap_max = _get("swap_max_bytes")
    cpu_quota = _get("cpu_quota_percent")
    timeout = _get("timeout_seconds")

    if memory_high > memory_max:
        raise ValueError(
            f"MemoryHigh {memory_high} > MemoryMax {memory_max}"
        )
    if memory_max > limits.memory_max_bytes:
        raise ValueError(
            f"MemoryMax {memory_max} > class ceiling {limits.memory_max_bytes}"
        )
    if memory_high > limits.memory_high_bytes:
        raise ValueError(
            f"MemoryHigh {memory_high} > class ceiling {limits.memory_high_bytes}"
        )
    if swap_max > limits.swap_max_bytes:
        raise ValueError(
            f"SwapMax {swap_max} > class ceiling {limits.swap_max_bytes}"
        )
    if cpu_quota > limits.cpu_quota_percent:
        raise ValueError(
            f"CPUQuota {cpu_quota} > class ceiling {limits.cpu_quota_percent}"
        )

    max_timeout = max(
        (
            lt.timeout_seconds
            for lt in (
                policy.light_limits,
                policy.medium_limits,
                policy.heavy_limits,
                policy.exclusive_limits,
            )
            if lt.timeout_seconds is not None
        ),
        default=0,
    )
    if timeout > max_timeout:
        raise ValueError(
            f"timeout {timeout} > policy maximum {max_timeout}"
        )

    return {
        "memory_high_bytes": memory_high,
        "memory_max_bytes": memory_max,
        "swap_max_bytes": swap_max,
        "cpu_quota_percent": cpu_quota,
        "timeout_seconds": timeout,
    }


def translate_limits_to_properties(effective_limits: Mapping) -> dict:
    """Translate validated limits to ``systemd-run --property=...`` values.

    Timeout is deliberately NOT translated here — the wall-clock step timeout is
    enforced by the external ``timeout`` wrapper (see
    :class:`argent_core.scope_enforcer.TimeoutRunner`), because systemd's
    ``TimeoutStopSec`` is a stop-timeout, not a wall-clock runtime limit.
    """
    props: dict = {
        "MemoryHigh": str(int(effective_limits["memory_high_bytes"])),
        "MemoryMax": str(int(effective_limits["memory_max_bytes"])),
        "MemorySwapMax": str(int(effective_limits["swap_max_bytes"])),
        "CPUQuota": f"{int(effective_limits['cpu_quota_percent'])}%",
        "TasksMax": str(DEFAULT_TASKS_MAX),
    }
    return props


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Scope record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionScope:
    """Frozen record of one bounded execution scope."""

    scope_name: str
    unit_name: str
    cgroup_path: str
    job_id: str
    dispatch_id: str
    resource_class: str
    policy_version: str
    effective_limits: dict
    process_id: Optional[int]
    created_at: str
    placeholder_pid: Optional[int] = None
    memory_events_baseline: dict = field(default_factory=dict)
    verified_properties: dict = field(default_factory=dict)
    verification_status: str = VERIFICATION_PENDING


class ScopeCreateError(Exception):
    """The scope could not be created (backend failure)."""


class ScopeVerificationError(Exception):
    """The scope's enforced properties could not be proven (fail-closed)."""


class ExecutionScopeBackend:
    """Scope lifecycle protocol (Start-Barrier create/verify/start/cleanup).

    F2: the scope is created with a harmless PLACEHOLDER first, the scope +
    properties + cgroup path are verified BEFORE the agent starts, THEN the
    real agent is moved into the verified scope's cgroup.  Every failure
    before agent start must prove inactivity (``prove_inactive``), never leave
    an unverifiable scope behind.
    """

    def create_scope(
        self,
        *,
        scope: ExecutionScope,
        placeholder_command: Sequence[str],
        properties: Mapping[str, str],
    ) -> ExecutionScope:  # pragma: no cover - protocol
        raise NotImplementedError

    def verify_scope(self, scope: ExecutionScope) -> dict:  # pragma: no cover
        raise NotImplementedError

    def start_in_scope(
        self,
        *,
        scope: ExecutionScope,
        command: Sequence[str],
    ) -> ExecutionScope:  # pragma: no cover - protocol
        raise NotImplementedError

    def run_in_scope(
        self,
        *,
        scope: ExecutionScope,
        command: Sequence[str],
        timeout: Optional[int] = None,
    ) -> dict:  # pragma: no cover - protocol
        raise NotImplementedError

    def verify_process_binding(self, scope: ExecutionScope) -> bool:  # pragma: no cover
        raise NotImplementedError

    def stop_placeholder(self, scope: ExecutionScope) -> None:  # pragma: no cover
        raise NotImplementedError

    def prove_inactive(self, scope: ExecutionScope) -> bool:  # pragma: no cover
        raise NotImplementedError

    def read_memory_events(self, scope: ExecutionScope) -> dict:  # pragma: no cover
        raise NotImplementedError

    def cleanup_scope(self, scope: ExecutionScope) -> None:  # pragma: no cover
        raise NotImplementedError

    def terminate_scope(self, scope: ExecutionScope) -> None:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Real backend: systemd-run --user --scope
# ---------------------------------------------------------------------------


class SystemdRunScopeBackend(ExecutionScopeBackend):
    """Real scope backend: ``systemd-run --user --scope`` + cgroup v2 read-back.

    Start-Barrier (F2):

    1. ``create_scope`` launches ``systemd-run --user --scope --unit=<name>
       --property=... -- <placeholder>`` (a harmless ``sleep``, NOT the agent)
       as a detached Popen.  ``systemd-run --scope`` EXECs the placeholder, so
       ``Popen.pid`` is the placeholder PID and the scope stays alive as long
       as the placeholder runs.
    2. ``verify_scope`` proves the scope + properties + cgroup path (F4,
       fail-closed) BEFORE the agent starts.
    3. ``start_in_scope`` starts the REAL agent detached (``start_new_session``,
       NO shell) and moves it into the verified scope's cgroup via a cgroup-v2
       ``cgroup.procs`` write (unprivileged — the scope belongs to the user).
    4. ``verify_process_binding`` proves the agent's ``/proc/<pid>/cgroup``
       equals the scope's cgroup path exactly (structured parse, no substring).
    5. ``stop_placeholder`` terminates the placeholder; the scope stays alive
       via the agent process and self-cleans when the agent exits.

    ``read_memory_events`` reads the bounded cgroup ``memory.events`` counters
    (F5); ``prove_inactive`` proves a scope is gone after cleanup.
    """

    def __init__(
        self,
        *,
        systemd_run: str = "systemd-run",
        systemctl: str = "systemctl",
        cgroupfs_root: str = "/sys/fs/cgroup",
        poll_timeout_seconds: float = 5.0,
        poll_interval_seconds: float = 0.1,
        popen_fn=None,
    ):
        self._systemd_run = systemd_run
        self._systemctl = systemctl
        self._cgroupfs_root = cgroupfs_root
        self._poll_timeout = poll_timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._popen_fn = popen_fn or subprocess.Popen

    # -- helpers -------------------------------------------------------------

    def _run(self, argv: Sequence[str]) -> tuple:
        """Run a subprocess; return ``(rc, stdout)`` (never raises on OSError)."""
        try:
            proc = subprocess.run(
                list(argv), capture_output=True, text=True, timeout=10,
            )
            return proc.returncode, proc.stdout
        except (OSError, subprocess.TimeoutExpired):
            return -1, ""

    def _show(self, unit_name: str, props: Sequence[str]) -> dict:
        """Read ``systemctl --user show <unit>`` properties into a dict."""
        argv = [self._systemctl, "--user", "show", unit_name]
        for p in props:
            argv.append(f"-p{p}")
        _, out = self._run(argv)
        result: dict = {}
        for line in out.splitlines():
            if "=" in line:
                key, _, val = line.partition("=")
                result[key.strip()] = val.strip()
        return result

    def _control_group(self, unit_name: str) -> str:
        out = self._show(unit_name, ["ControlGroup"])
        return out.get("ControlGroup", "")

    def _wait_for_control_group(self, unit_name: str) -> str:
        """Bounded poll for the scope's ControlGroup path to appear."""
        deadline = time.monotonic() + self._poll_timeout
        while time.monotonic() < deadline:
            cg = self._control_group(unit_name)
            if cg:
                return cg
            time.sleep(self._poll_interval)
        return ""

    def _read_file(self, path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def _read_cgroupfs(self, cgroup_path: str) -> dict:
        base = os.path.join(self._cgroupfs_root, cgroup_path.lstrip("/"))
        return {
            "memory.max": self._read_file(os.path.join(base, "memory.max")),
            "memory.high": self._read_file(os.path.join(base, "memory.high")),
            "memory.swap.max": self._read_file(os.path.join(base, "memory.swap.max")),
            "cpu.max": self._read_file(os.path.join(base, "cpu.max")),
            "pids.max": self._read_file(os.path.join(base, "pids.max")),
        }

    def _parse_process_cgroup(self, pid: Optional[int]) -> str:
        """Strictly parse ``/proc/<pid>/cgroup`` (cgroup v2 ``0::<path>``).

        Returns the unified (cgroup v2) path for the process, or ``""`` when
        unreadable/absent.  No substring search — the caller compares the full
        path exactly.
        """
        if not isinstance(pid, int) or pid <= 0:
            return ""
        content = self._read_file(f"/proc/{pid}/cgroup")
        if not content:
            return ""
        for line in content.splitlines():
            parts = line.split(":", 2)
            if len(parts) != 3:
                continue
            # cgroup v2 unified hierarchy: "0::<path>".
            if parts[0] == "0" and parts[1] == "":
                return parts[2]
        return ""

    def _process_in_cgroup(self, pid: Optional[int], cgroup_path: str) -> bool:
        if not cgroup_path:
            return False
        return self._parse_process_cgroup(pid) == cgroup_path

    def _move_into_cgroup(self, pid: int, cgroup_path: str) -> bool:
        """Unprivileged cgroup-v2 ``cgroup.procs`` write to move ``pid`` in."""
        if not isinstance(pid, int) or pid <= 0 or not cgroup_path:
            return False
        procs = os.path.join(
            self._cgroupfs_root, cgroup_path.lstrip("/"), "cgroup.procs"
        )
        try:
            with open(procs, "w", encoding="ascii") as fh:
                fh.write(str(pid))
            return True
        except OSError:
            return False

    def _kill_pid(self, pid: Optional[int]) -> None:
        if not isinstance(pid, int) or pid <= 0:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except (OSError, ProcessLookupError):
                return

    def _parse_usec(self, raw) -> Optional[int]:
        """Parse a systemd time-span (``1s``, ``500ms``, ``2min`` ...) to usec."""
        if not isinstance(raw, str) or not raw.strip():
            return None
        total = 0
        unit_map = {
            "us": 1, "ms": 1000, "s": 1_000_000,
            "min": 60_000_000, "h": 3_600_000_000, "d": 86_400_000_000,
        }
        for part in raw.split():
            m = re.match(r"^(\d+)([a-z]+)$", part)
            if not m:
                return None
            n, unit = int(m.group(1)), m.group(2)
            if unit not in unit_map:
                return None
            total += n * unit_map[unit]
        return total

    def _cpu_quota_matches(self, raw, cpu_quota_percent: int) -> bool:
        # 100% quota == 1s == 1_000_000us == 100 * 10_000us.
        usec = self._parse_usec(raw)
        return usec == int(cpu_quota_percent) * 10_000

    def _read_memory_events_file(self, cgroup_path: str) -> dict:
        base = os.path.join(self._cgroupfs_root, cgroup_path.lstrip("/"), "memory.events")
        text = self._read_file(base)
        events: dict = {}
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] in MEMORY_EVENTS_KEYS:
                try:
                    events[parts[0]] = int(parts[1])
                except ValueError:
                    continue
        return events

    def _unit_name_from(self, scope_name: str) -> str:
        # systemd-run --scope appends ".scope" to the ``--unit`` name.
        return scope_name if scope_name.endswith(".scope") else scope_name + ".scope"

    @staticmethod
    def _bounded(data: Optional[bytes]) -> str:
        if not data:
            return ""
        text = data.decode("utf-8", errors="replace")
        return text[:MAX_RUN_OUTPUT_BYTES]

    # -- backend interface ---------------------------------------------------

    def create_scope(
        self,
        *,
        scope: ExecutionScope,
        placeholder_command: Sequence[str],
        properties: Mapping[str, str],
    ) -> ExecutionScope:
        if not is_valid_scope_name(scope.scope_name):
            raise ScopeCreateError(f"invalid scope name {scope.scope_name!r}")
        argv = [
            self._systemd_run, "--user", "--scope",
            f"--unit={scope.scope_name}",
        ]
        for key, value in properties.items():
            argv.append(f"--property={key}={value}")
        argv.append("--")
        argv.extend(str(c) for c in placeholder_command)
        try:
            popen = self._popen_fn(
                argv,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                env=agent_spawn_env(),
            )
        except (OSError, ValueError) as exc:
            raise ScopeCreateError(f"systemd-run failed to start: {exc}") from exc
        placeholder_pid = getattr(popen, "pid", None)
        cgroup_path = self._wait_for_control_group(scope.unit_name)
        if not cgroup_path:
            raise ScopeCreateError(
                f"scope {scope.unit_name!r} ControlGroup not resolvable"
            )
        return replace(
            scope,
            placeholder_pid=placeholder_pid if isinstance(placeholder_pid, int) else None,
            cgroup_path=cgroup_path,
        )

    def verify_scope(self, scope: ExecutionScope) -> dict:
        props = self._show(
            scope.unit_name,
            ["MemoryMax", "MemoryHigh", "MemorySwapMax", "CPUQuotaPerSecUSec",
             "TasksMax", "ControlGroup", "ActiveState"],
        )
        # F4.1: ControlGroup MUST be present (NO fallback to the old value) and
        # must EXACTLY equal the recorded cgroup path + end with our own unit.
        control_group = props.get("ControlGroup", "")
        fs = self._read_cgroupfs(control_group if control_group else scope.cgroup_path)

        expected = scope.effective_limits
        cpu_quota = int(expected["cpu_quota_percent"])

        errors: list = []
        if not control_group:
            errors.append("ControlGroup.missing")
        else:
            if control_group != scope.cgroup_path:
                errors.append("ControlGroup.mismatch")
            if not control_group.endswith("/" + scope.unit_name):
                errors.append("ControlGroup.unit_binding")
        if props.get("MemoryMax") != str(expected["memory_max_bytes"]):
            errors.append("MemoryMax")
        if props.get("MemoryHigh") != str(expected["memory_high_bytes"]):
            errors.append("MemoryHigh")
        if props.get("MemorySwapMax") != str(expected["swap_max_bytes"]):
            errors.append("MemorySwapMax")
        # F4.2: CPUQuotaPerSecUSec normalized ("1s" == 100%).
        if not self._cpu_quota_matches(props.get("CPUQuotaPerSecUSec", ""), cpu_quota):
            errors.append("CPUQuotaPerSecUSec")
        # F4.2: TasksMax (systemd property) + pids.max (cgroupfs) exact.
        if props.get("TasksMax") != str(DEFAULT_TASKS_MAX):
            errors.append("TasksMax")
        if props.get("ActiveState") != "active":
            errors.append("ActiveState")
        if fs.get("memory.max") != str(expected["memory_max_bytes"]):
            errors.append("fs.memory.max")
        if fs.get("memory.high") != str(expected["memory_high_bytes"]):
            errors.append("fs.memory.high")
        if fs.get("memory.swap.max") != str(expected["swap_max_bytes"]):
            errors.append("fs.memory.swap.max")
        if fs.get("cpu.max") != f"{cpu_quota * 1000} 100000":
            errors.append("fs.cpu.max")
        if fs.get("pids.max") != str(DEFAULT_TASKS_MAX):
            errors.append("fs.pids.max")
        # F2: the placeholder must be bound to the scope (exact cgroup parse).
        if not self._process_in_cgroup(scope.placeholder_pid, control_group or scope.cgroup_path):
            errors.append("placeholder_binding")

        if errors:
            raise ScopeVerificationError(
                f"scope {scope.unit_name!r} verification failed: "
                + ", ".join(errors)
            )

        return {
            "MemoryMax": props.get("MemoryMax", ""),
            "MemoryHigh": props.get("MemoryHigh", ""),
            "MemorySwapMax": props.get("MemorySwapMax", ""),
            "CPUQuotaPerSecUSec": props.get("CPUQuotaPerSecUSec", ""),
            "TasksMax": props.get("TasksMax", ""),
            "ControlGroup": control_group or scope.cgroup_path,
            "ActiveState": props.get("ActiveState", ""),
            "memory.max": fs.get("memory.max", ""),
            "memory.high": fs.get("memory.high", ""),
            "memory.swap.max": fs.get("memory.swap.max", ""),
            "cpu.max": fs.get("cpu.max", ""),
            "pids.max": fs.get("pids.max", ""),
        }

    def start_in_scope(
        self,
        *,
        scope: ExecutionScope,
        command: Sequence[str],
    ) -> ExecutionScope:
        """Start the agent detached (NO shell) and move it into the scope cgroup."""
        try:
            popen = self._popen_fn(
                list(command),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                env=agent_spawn_env(),
            )
        except (OSError, ValueError) as exc:
            raise ScopeCreateError(f"agent start failed: {exc}") from exc
        pid = getattr(popen, "pid", None)
        if not isinstance(pid, int) or pid <= 0:
            raise ScopeCreateError("agent process id not resolvable")
        if not self._move_into_cgroup(pid, scope.cgroup_path):
            self._kill_pid(pid)
            raise ScopeCreateError("could not move agent into scope cgroup")
        return replace(scope, process_id=pid)

    def run_in_scope(
        self,
        *,
        scope: ExecutionScope,
        command: Sequence[str],
        timeout: Optional[int] = None,
    ) -> dict:
        """Run a command synchronously inside the scope (bounded output capture)."""
        try:
            popen = self._popen_fn(
                list(command),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                env=agent_spawn_env(),
            )
        except (OSError, ValueError) as exc:
            raise ScopeCreateError(f"command start failed: {exc}") from exc
        pid = getattr(popen, "pid", None)
        if not isinstance(pid, int) or pid <= 0:
            raise ScopeCreateError("command process id not resolvable")
        if not self._move_into_cgroup(pid, scope.cgroup_path):
            self._kill_pid(pid)
            raise ScopeCreateError("could not move command into scope cgroup")
        try:
            out, err = popen.communicate(
                timeout=(timeout + TIMEOUT_KILL_AFTER_SECONDS + 15) if timeout else None,
            )
        except subprocess.TimeoutExpired:
            self._kill_pid(pid)
            out, err = popen.communicate()
            return {
                "exit_code": -1,
                "stdout_bounded": self._bounded(out),
                "stderr_bounded": self._bounded(err),
                "timed_out": True,
            }
        return {
            "exit_code": popen.returncode,
            "stdout_bounded": self._bounded(out),
            "stderr_bounded": self._bounded(err),
            "timed_out": popen.returncode == 124,
        }

    def verify_process_binding(self, scope: ExecutionScope) -> bool:
        """Exact process→scope binding (F4.3): full cgroup path, no substring."""
        return self._process_in_cgroup(scope.process_id, scope.cgroup_path)

    def stop_placeholder(self, scope: ExecutionScope) -> None:
        """Terminate the Start-Barrier placeholder (own unit; scope stays alive)."""
        self._kill_pid(scope.placeholder_pid)

    def prove_inactive(self, scope: ExecutionScope) -> bool:
        """Prove the scope is gone (ActiveState inactive OR cgroup path gone)."""
        if not is_valid_scope_name(scope.scope_name):
            return False
        active = self._show(scope.unit_name, ["ActiveState"]).get("ActiveState", "")
        if active in ("inactive", "failed", "dead", ""):
            return True
        cg = scope.cgroup_path or self._control_group(scope.unit_name)
        if not cg:
            return True
        cg_path = os.path.join(self._cgroupfs_root, cg.lstrip("/"))
        if not os.path.exists(cg_path):
            return True
        return False

    def read_memory_events(self, scope: ExecutionScope) -> dict:
        cg = scope.cgroup_path or self._control_group(scope.unit_name)
        if not cg:
            return {}
        return self._read_memory_events_file(cg)

    def cleanup_scope(self, scope: ExecutionScope) -> None:
        """Best-effort stop of our own transient scope (safety net)."""
        if not is_valid_scope_name(scope.scope_name):
            return
        self._run([self._systemctl, "--user", "stop", scope.unit_name])

    def terminate_scope(self, scope: ExecutionScope) -> None:
        """Best-effort force-kill of the scope's process tree (safety net)."""
        if not is_valid_scope_name(scope.scope_name):
            return
        self._run([
            self._systemctl, "--user", "kill", "--signal=SIGKILL", scope.unit_name,
        ])
