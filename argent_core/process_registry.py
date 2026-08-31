"""Phase B3 — minimal Process Registry (evidence only, no generic kill).

The PID alone is not a process identity (PID reuse, boot changes).  The
canonical identity is the tuple ``(boot_id, pid, process_start_ticks)``
(ARCHITECTURE REVIEW V1 §6.1 / ARGENT ARCHITECTURE V1 FINAL §6).

This module provides:

* :class:`ProcessIdentity` — the immutable identity tuple.
* :class:`ProcessIdentityProvider` — reads ``boot_id`` from
  ``/proc/sys/kernel/random/boot_id`` and ``process_start_ticks`` from
  ``/proc/<pid>/stat`` field 22; both sources are injectable for tests.
* :class:`ProcessRegistry` — persists/classifies registrations through the
  store.  Registration happens ONLY at the trusted local spawn path; no agent
  sets these values.

Recovery evidence rules (implemented in :meth:`ProcessRegistry.classify`):

* same ``(boot_id, pid, start_ticks)`` -> the SAME known process;
* same ``pid`` but different ``start_ticks`` -> NOT the same process (PID reuse);
* different ``boot_id`` -> the old registration is surely not alive;
* unknown status -> fail-closed (never "surely dead").

Agent prose (e.g. "process completed") is NEVER an authority here — only the
persisted registry facts and the live identity tuple are.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

#: Process registry statuses (closed set).
PROCESS_STATUS_RUNNING = "RUNNING"
PROCESS_STATUS_TERMINAL = "TERMINAL"
PROCESS_STATUS_UNKNOWN = "UNKNOWN"
ALLOWED_PROCESS_STATUSES: frozenset[str] = frozenset({
    PROCESS_STATUS_RUNNING, PROCESS_STATUS_TERMINAL, PROCESS_STATUS_UNKNOWN,
})

#: Classification verdicts (recovery evidence).
IDENTITY_SAME = "same"
IDENTITY_PID_REUSE = "pid_reuse"
IDENTITY_BOOT_CHANGED = "boot_changed"


@dataclass(frozen=True)
class ProcessIdentity:
    """The canonical process identity tuple (§6).

    ``boot_id`` and ``process_start_ticks`` are ``None`` when UNKNOWN/unreadable
    (F2: never ``""``/``0`` as a concrete identity).  ``is_known`` is True only
    when the full tuple is concrete.
    """

    boot_id: Optional[str] = None
    pid: int = 0
    process_start_ticks: Optional[int] = None

    @property
    def is_known(self) -> bool:
        return self.boot_id is not None and self.process_start_ticks is not None


class ProcessIdentityProvider:
    """Reads the live process identity; injectable for tests.

    ``boot_id_path`` defaults to ``/proc/sys/kernel/random/boot_id`` (a stable,
    readable boot identifier that changes on every WSL/PC reboot).  ``proc_root``
    points at the ``/proc`` mount used to read ``<pid>/stat``.  Both can be
    replaced with test doubles by passing ``boot_id_reader`` / ``stat_reader``.
    """

    def __init__(
        self,
        *,
        boot_id_path: str = "/proc/sys/kernel/random/boot_id",
        proc_root: str = "/proc",
        boot_id_reader: Optional[callable] = None,
        stat_reader: Optional[callable] = None,
    ):
        self._boot_id_path = boot_id_path
        self._proc_root = proc_root
        self._boot_id_reader = boot_id_reader
        self._stat_reader = stat_reader

    def boot_id(self) -> Optional[str]:
        if self._boot_id_reader is not None:
            value = self._boot_id_reader()
        else:
            try:
                with open(self._boot_id_path, "r", encoding="utf-8") as fh:
                    value = fh.read()
            except OSError:
                return None
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip()

    def _stat_line(self, pid: int) -> Optional[str]:
        if self._stat_reader is not None:
            return self._stat_reader(pid)
        try:
            with open(os.path.join(self._proc_root, str(pid), "stat"),
                      "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None

    def process_start_ticks(self, pid: int) -> Optional[int]:
        """Field 22 of ``/proc/<pid>/stat`` (starttime in clock ticks)."""
        line = self._stat_line(pid)
        if line is None:
            return None
        return parse_start_ticks(line)

    def current(self, pid: int) -> ProcessIdentity:
        """The live identity for ``pid`` (boot_id + pid + start_ticks).

        F2: unreadable identity parts are ``None`` (UNKNOWN), never ``""``/``0``
        masquerading as a concrete identity.
        """
        ticks = self.process_start_ticks(pid)
        return ProcessIdentity(
            boot_id=self.boot_id(),
            pid=pid,
            process_start_ticks=ticks,
        )


def parse_start_ticks(stat_line: str) -> int:
    """Parse starttime (field 22) from a ``/proc/<pid>/stat`` line.

    The comm field ``(name)`` may contain spaces and parentheses, so we split
    at the LAST ``)``; the remaining fields start at field 3 (state).  Starttime
    is therefore index ``22 - 3 == 19`` of the remaining fields.
    """
    rest = stat_line.rsplit(")", 1)[-1].split()
    if len(rest) < 20:
        raise ValueError("stat line too short to contain field 22")
    return int(rest[19])


class ProcessRegistry:
    """Persists and classifies process registrations through the store."""

    def __init__(self, store):
        self._store = store

    def register(
        self,
        *,
        process_id: Optional[str] = None,
        job_id: str,
        dispatch_id: Optional[str],
        identity: ProcessIdentity,
        status: str = PROCESS_STATUS_RUNNING,
        cgroup_ref: Optional[str] = None,
    ) -> dict:
        """Insert a registration (trusted spawn path only).

        ``status`` must be a closed-set value; ``identity`` is the observed
        (boot_id, pid, start_ticks) tuple — never agent-supplied.  F2: an
        UNKNOWN identity (missing boot_id or start_ticks) is persisted with
        ``status=UNKNOWN`` and ``NULL`` identity parts — NEVER a concrete
        ``""``/``0`` identity.
        """
        if status not in ALLOWED_PROCESS_STATUSES:
            raise ValueError(f"invalid process status {status!r}")
        if not identity.is_known:
            status = PROCESS_STATUS_UNKNOWN
        now = self._store.now_iso()
        row = {
            "process_id": process_id or "proc:" + _uuid_hex(),
            "job_id": job_id,
            "dispatch_id": dispatch_id,
            "pid": identity.pid,
            "boot_id": identity.boot_id,
            "process_start_ticks": identity.process_start_ticks,
            "cgroup_ref": cgroup_ref,
            "status": status,
            "created_at": now,
            "last_observed_at": now,
            "terminal_at": None,
            "exit_code": None,
        }
        self._store._insert_process_registration(row)
        return row

    # -- classification (recovery evidence) ---------------------------------

    @staticmethod
    def classify_identity(
        registered: dict, observed: ProcessIdentity,
    ) -> str:
        """Classify an observed identity against a persisted registration.

        Returns ``IDENTITY_SAME``, ``IDENTITY_PID_REUSE`` or
        ``IDENTITY_BOOT_CHANGED``.  A different boot_id always wins (the old
        registration cannot be alive after a boot change); within the same boot
        a different ``start_ticks`` for the same pid means PID reuse.
        """
        if registered["boot_id"] != observed.boot_id:
            return IDENTITY_BOOT_CHANGED
        if registered["pid"] == observed.pid \
                and registered["process_start_ticks"] == observed.process_start_ticks:
            return IDENTITY_SAME
        if registered["pid"] == observed.pid:
            return IDENTITY_PID_REUSE
        return IDENTITY_BOOT_CHANGED  # same boot, unknown pid -> not this process

    @staticmethod
    def is_terminally_dead(registration: dict) -> bool:
        """True ONLY when the registry carries authoritative terminal evidence.

        Requires ``status == TERMINAL`` AND a concrete ``terminal_at`` timestamp.
        Anything else (RUNNING, UNKNOWN, missing terminal_at) is NOT "surely
        dead" and must be treated fail-closed by recovery.  Agent prose never
        contributes to this.
        """
        return (
            registration.get("status") == PROCESS_STATUS_TERMINAL
            and registration.get("terminal_at") is not None
        )


def _uuid_hex() -> str:
    from uuid import uuid4
    return uuid4().hex
