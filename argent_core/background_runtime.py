"""Phase G1 — background runtime core (SPEC G1 §B–J, §L, §O).

This module is the CODE-ONLY runtime kernel for the non-interactive background
supervisor.  It provides, over the existing Phase B/C/D/E/F modules:

* :class:`SupervisorInstance` — single-active-supervisor ownership.  The
  authoritative liveness signal is the canonical ``(boot_id, pid,
  process_start_ticks)`` identity (Phase B3 ``process_registry``); a persisted
  singleton row in ``supervisor_instances`` is recovery evidence + a
  compare-and-swap fence.  **PID alone is never authority** and an ambiguous
  live owner always fails closed (no split-brain).
* :class:`ServiceHealth` / :class:`HealthSnapshot` — machine-readable SERVICE
  health (STARTING/READY/DEGRADED/STOPPING/FAILED), distinct from job states.
* :class:`SupervisorRuntime` — the bounded background loop (one scheduler step
  per pass, non-LLM external-wait checks, deterministic sleep, exception
  containment, responsive graceful shutdown, bounded instance heartbeat).

Nothing in this module enables or starts a systemd unit, enables user
lingering, edits ``.wslconfig``/Windows startup, or performs a real reboot.
G1 is strictly code + tests + a static unit template (activation is G2).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional
from uuid import uuid4

from .process_registry import ProcessIdentity, ProcessIdentityProvider
from .scheduler import OUTCOME_NO_WORK

# ---------------------------------------------------------------------------
# Constants (SPEC G1 §I/§C)
# ---------------------------------------------------------------------------

#: Bounded attempts to win the compare-and-swap before failing closed.
MAX_ACQUIRE_ATTEMPTS = 5

#: Default instance-lease TTL (heartbeat horizon).  A crashed supervisor's
#: instance row is "stale" beyond this; the authoritative decision is still
#: process identity, never the lease alone.
DEFAULT_INSTANCE_TTL_SECONDS = 60

#: Loop timing (deterministic; injectable in tests).
IDLE_SLEEP_SECONDS = 5.0      # no claimable work AND no due external wait
ACTIVE_SLEEP_SECONDS = 1.0    # there was work, or a wait was checked
SLEEP_SLICE_SECONDS = 0.25    # stop_event responsiveness granularity
HEARTBEAT_EVERY_PASSES = 10   # renew the instance lease every N passes

#: Bounded consecutive pass-error threshold before the service is classified
#: FAILED (structural failure) instead of DEGRADED (transient).  Reaching this
#: forces a non-zero exit so ``Restart=on-failure`` actually fires (SPEC G1 §L).
MAX_CONSECUTIVE_ERRORS = 10

#: Persisted instance statuses (closed set).
INSTANCE_ACTIVE = "ACTIVE"
INSTANCE_RELEASED = "RELEASED"
_ALLOWED_INSTANCE_STATUSES = frozenset({INSTANCE_ACTIVE, INSTANCE_RELEASED})

#: Owner liveness verdicts (pure classification, SPEC G1 §D).
OWNER_LIVENESS_LIVE = "live"        # same (boot_id, pid, start_ticks), alive
OWNER_LIVENESS_DEAD = "dead"        # provably gone (boot change / pid reuse / no pid)
OWNER_LIVENESS_UNKNOWN = "unknown"  # fail-closed: cannot determine safely


class ServiceHealth(str, Enum):
    """SERVICE-level health (SPEC G1 §J) — never a job state."""

    STARTING = "STARTING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


class InstanceVerdict(str, Enum):
    """Outcome of a single-active acquisition attempt (SPEC G1 §C)."""

    ACQUIRED = "acquired"        # first acquisition (no prior live owner)
    TAKEOVER = "takeover"        # a provably-dead owner was replaced (bounded)
    LIVE_OWNER = "live_owner"    # an authoritative live owner exists -> refused
    AMBIGUOUS = "ambiguous"      # liveness undeterminable -> fail closed


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class InstanceAcquireResult:
    verdict: InstanceVerdict
    instance_id: str
    detail: str
    owner: Optional[dict] = None


@dataclass(frozen=True)
class HealthSnapshot:
    """Machine-readable service health (SPEC G1 §J).  No secrets."""

    state: str
    instance_id: Optional[str]
    boot_id: Optional[str]
    pid: Optional[int]
    process_start_ticks: Optional[int]
    started_at: Optional[str]
    last_scheduler_pass_at: Optional[str]
    db_accessible: bool
    active_job_count: int
    external_wait_count: int
    recovery_result: Optional[dict]
    last_error_code: Optional[str]
    stopping_reason: Optional[str]

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "instance_id": self.instance_id,
            "boot_id": self.boot_id,
            "pid": self.pid,
            "process_start_ticks": self.process_start_ticks,
            "started_at": self.started_at,
            "last_scheduler_pass_at": self.last_scheduler_pass_at,
            "db_accessible": self.db_accessible,
            "active_job_count": self.active_job_count,
            "external_wait_count": self.external_wait_count,
            "recovery_result": self.recovery_result,
            "last_error_code": self.last_error_code,
            "stopping_reason": self.stopping_reason,
        }


@dataclass(frozen=True)
class LoopSummary:
    passes: int
    errors: int
    last_pass_outcome: Optional[str]
    stop_reason: Optional[str]


# ---------------------------------------------------------------------------
# SupervisorInstance — single-active ownership (SPEC G1 §C/§D/§O)
# ---------------------------------------------------------------------------

class SupervisorInstance:
    """Establishes and fences the single active supervisor instance.

    The persisted ``supervisor_instances`` row is NOT a PID-only authority:
    liveness of an existing owner is classified from the canonical identity
    tuple ``(boot_id, pid, process_start_ticks)`` using the injectable
    :class:`~argent_core.process_registry.ProcessIdentityProvider` and an
    injectable ``pid_alive`` probe (default: ``/proc/<pid>`` existence).
    """

    def __init__(
        self,
        store,
        *,
        identity_provider: Optional[ProcessIdentityProvider] = None,
        instance_id: Optional[str] = None,
        own_pid: Optional[int] = None,
        pid_alive: Optional[Callable[[int], Optional[bool]]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        ttl_seconds: int = DEFAULT_INSTANCE_TTL_SECONDS,
    ):
        self._store = store
        self._identity_provider = identity_provider or ProcessIdentityProvider()
        self.instance_id = instance_id or ("instance:" + uuid4().hex)
        self._own_pid = own_pid if own_pid is not None else os.getpid()
        self._pid_alive = pid_alive or self._default_pid_alive
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ttl_seconds = ttl_seconds
        #: Populated on a successful acquire (authoritative identity).
        self.identity: Optional[ProcessIdentity] = None
        #: Host identity (machine-id) captured on acquire (G1 F3 shared-store fence).
        self._host_id: Optional[str] = None

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _default_pid_alive(pid: int) -> Optional[bool]:
        proc = Path("/proc")
        if not proc.is_dir():
            return None  # /proc unavailable -> ambiguous (never "dead")
        return (proc / str(pid)).is_dir()

    def _now_iso(self) -> str:
        return _iso(self._clock())

    def _now(self) -> datetime:
        return self._clock()

    @staticmethod
    def classify_owner_liveness(
        *,
        owner_boot_id: Optional[str],
        owner_pid: Optional[int],
        owner_start_ticks: Optional[int],
        live_boot_id: Optional[str],
        pid_alive: Optional[bool],
        live_start_ticks: Optional[int],
        owner_host_id: Optional[str] = None,
        live_host_id: Optional[str] = None,
    ) -> str:
        """Pure classification of a persisted owner's liveness (SPEC G1 §D).

        Returns ``live`` (same process, provably alive), ``dead`` (provably
        gone: same-host reboot / PID reuse / no such pid) or ``unknown``
        (fail-closed: incomplete persisted identity, unreadable boot identity,
        or unreadable pid stat).  An ``unknown`` verdict must NEVER authorize a
        takeover — that is the no-split-brain guarantee.

        G1 (F3): a DIFFERENT boot identity is only provably "dead" when the
        host identity matches (same host rebooted).  A different host (shared
        FS/network store) or an unreadable/missing host identity is
        AMBIGUOUS — the owner may be alive on its own host, so a takeover must
        never be authorized without provable same-host identity.
        """
        if owner_boot_id is None or owner_pid is None or owner_start_ticks is None:
            return OWNER_LIVENESS_UNKNOWN  # incomplete persisted identity
        if live_boot_id is None:
            return OWNER_LIVENESS_UNKNOWN  # cannot read current boot identity
        if owner_boot_id != live_boot_id:
            # Different boot: a same-host REBOOT is provably dead; a different
            # host (shared store) is NOT (the owner may be alive on its host).
            if owner_host_id is None or live_host_id is None:
                return OWNER_LIVENESS_UNKNOWN  # no provable same-host identity
            if owner_host_id != live_host_id:
                return OWNER_LIVENESS_UNKNOWN  # different host -> never dead
            return OWNER_LIVENESS_DEAD  # same host, boot changed -> reboot
        if pid_alive is None:
            return OWNER_LIVENESS_UNKNOWN  # cannot read /proc
        if pid_alive is False:
            return OWNER_LIVENESS_DEAD  # no such process
        if live_start_ticks is None:
            return OWNER_LIVENESS_UNKNOWN  # pid alive but stat unreadable
        if live_start_ticks == owner_start_ticks:
            return OWNER_LIVENESS_LIVE  # same process
        return OWNER_LIVENESS_DEAD  # PID reuse -> old process gone

    def _classify_existing(self, owner: dict, live_boot: str) -> InstanceVerdict:
        op = owner.get("pid")
        alive = self._pid_alive(op) if isinstance(op, int) else None
        ticks = self._identity_provider.process_start_ticks(op) \
            if isinstance(op, int) and alive is True else None
        liveness = self.classify_owner_liveness(
            owner_boot_id=owner.get("boot_id"),
            owner_pid=owner.get("pid"),
            owner_start_ticks=owner.get("process_start_ticks"),
            live_boot_id=live_boot,
            pid_alive=alive,
            live_start_ticks=ticks,
            owner_host_id=owner.get("host_id"),
            live_host_id=self._host_id,
        )
        if liveness == OWNER_LIVENESS_LIVE:
            return InstanceVerdict.LIVE_OWNER
        if liveness == OWNER_LIVENESS_DEAD:
            return InstanceVerdict.TAKEOVER
        return InstanceVerdict.AMBIGUOUS

    def _build_row(
        self, *, acquired_at: str, live_boot: str, own_ticks: int,
        lease_expires_at: Optional[str] = None,
    ) -> dict:
        now = self._now_iso()
        return {
            "singleton_id": "primary",
            "instance_id": self.instance_id,
            "boot_id": live_boot,
            "host_id": self._host_id,
            "pid": self._own_pid,
            "process_start_ticks": own_ticks,
            "status": INSTANCE_ACTIVE,
            "acquired_at": acquired_at,
            "lease_expires_at": lease_expires_at,
            "last_heartbeat_at": acquired_at,
            "stopped_at": None,
            "stop_reason": None,
            "last_error_code": None,
            "updated_at": now,
        }

    # -- acquire -----------------------------------------------------------

    def acquire(self) -> InstanceAcquireResult:
        """Attempt to become the single active supervisor (SPEC G1 §C).

        Fail-closed outcomes: ``LIVE_OWNER`` (an authoritative live supervisor
        exists) and ``AMBIGUOUS`` (liveness undeterminable — never a takeover).
        A successful ``ACQUIRED``/``TAKEOVER`` writes our identity through the
        store's compare-and-swap so two processes can never both win.
        """
        live_boot = self._identity_provider.boot_id()
        if live_boot is None:
            return InstanceAcquireResult(
                InstanceVerdict.AMBIGUOUS, self.instance_id, "boot_id_unreadable",
            )
        # G1 (F3): host identity for the shared-store single-active fence.
        self._host_id = self._identity_provider.machine_id()
        own_ticks = self._identity_provider.process_start_ticks(self._own_pid)
        if own_ticks is None:
            return InstanceAcquireResult(
                InstanceVerdict.AMBIGUOUS, self.instance_id,
                "own_start_ticks_unreadable",
            )
        acquired_at = self._now_iso()
        expiry = self._expiry_iso()
        for _ in range(MAX_ACQUIRE_ATTEMPTS):
            owner = self._store.get_supervisor_instance()
            expected_rev = owner["revision"] if owner is not None else None
            if owner is None or owner.get("status") == INSTANCE_RELEASED:
                verdict = InstanceVerdict.ACQUIRED
            else:
                verdict = self._classify_existing(owner, live_boot)
            if verdict in (InstanceVerdict.LIVE_OWNER, InstanceVerdict.AMBIGUOUS):
                return InstanceAcquireResult(
                    verdict, self.instance_id,
                    f"refused:existing:{verdict.value}", owner=owner,
                )
            row = self._build_row(
                acquired_at=acquired_at, live_boot=live_boot,
                own_ticks=own_ticks, lease_expires_at=expiry,
            )
            if self._store.cas_supervisor_instance(
                row=row, expected_revision=expected_rev,
            ):
                self.identity = ProcessIdentity(
                    boot_id=live_boot, pid=self._own_pid,
                    process_start_ticks=own_ticks,
                )
                return InstanceAcquireResult(verdict, self.instance_id, "acquired")
        return InstanceAcquireResult(
            InstanceVerdict.AMBIGUOUS, self.instance_id, "acquire_cas_contended",
        )

    def _expiry_iso(self) -> str:
        return _iso(self._now() + timedelta(seconds=self._ttl_seconds))

    # -- lease maintenance --------------------------------------------------

    def heartbeat(self) -> bool:
        """Renew the instance lease (fenced by ``instance_id``)."""
        now = self._now_iso()
        return self._store.update_supervisor_instance(
            expected_instance_id=self.instance_id,
            fields={
                "last_heartbeat_at": now,
                "lease_expires_at": self._expiry_iso(),
                "updated_at": now,
            },
        )

    def release(self, *, reason: str) -> bool:
        """Persist graceful-release evidence (fenced by ``instance_id``).

        Only the current holder can release; a since-replaced instance's
        release is a no-op (returns False) — no split-brain bookkeeping.
        """
        now = self._now_iso()
        return self._store.update_supervisor_instance(
            expected_instance_id=self.instance_id,
            fields={
                "status": INSTANCE_RELEASED,
                "stopped_at": now,
                "stop_reason": (reason or "")[:256],
                "updated_at": now,
            },
        )


# ---------------------------------------------------------------------------
# SupervisorRuntime — bounded background loop (SPEC G1 §I/§G)
# ---------------------------------------------------------------------------

class SupervisorRuntime:
    """Bounded, interruptible background loop over the Phase B scheduler.

    Each pass performs exactly one scheduler step (already bounded by
    ``Scheduler.run_pass``) plus a bounded non-LLM external-wait check, then
    sleeps deterministically.  Idle passes cost ~one no-work claim — negligible
    compute and never a busy-spin or a held LLM.  Exceptions are contained
    (the loop degrades to ``DEGRADED`` and keeps running with a bounded error
    code) unless shutdown has been requested.
    """

    def __init__(
        self,
        *,
        scheduler,
        external_wait_manager,
        instance: SupervisorInstance,
        store,
        ci_wait_manager=None,
        clock: Optional[Callable[[], datetime]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        stop_event=None,
        idle_sleep_seconds: float = IDLE_SLEEP_SECONDS,
        active_sleep_seconds: float = ACTIVE_SLEEP_SECONDS,
        slice_seconds: float = SLEEP_SLICE_SECONDS,
        heartbeat_every_passes: int = HEARTBEAT_EVERY_PASSES,
        max_passes: Optional[int] = None,
        max_consecutive_errors: int = MAX_CONSECUTIVE_ERRORS,
    ):
        self._scheduler = scheduler
        self._external_wait_manager = external_wait_manager
        self._ci_wait_manager = ci_wait_manager
        self._instance = instance
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep_fn = sleep_fn or time.sleep
        self._stop_event = stop_event
        if self._stop_event is None:
            import threading
            self._stop_event = threading.Event()
        self._idle_sleep = idle_sleep_seconds
        self._active_sleep = active_sleep_seconds
        self._slice = slice_seconds
        self._heartbeat_every = max(1, heartbeat_every_passes)
        self._max_passes = max_passes
        self._max_consecutive_errors = max(1, max_consecutive_errors)

        # G1 (F6): wire a stop-predicate into the scheduler so a SIGTERM that
        # lands mid-pass can abort the pass BEFORE any expensive spawn/test
        # action.  Backward-compatible: schedulers without the hook are ignored.
        set_stop_check = getattr(scheduler, "set_stop_check", None)
        if set_stop_check is not None:
            set_stop_check(self._stop_event.is_set)

        self._state = ServiceHealth.STARTING
        self._passes = 0
        self._errors = 0
        self._consecutive_errors = 0
        self._last_pass_outcome: Optional[str] = None
        self._last_pass_at: Optional[str] = None
        self._last_error_code: Optional[str] = None
        self._last_work_had_activity = False
        self._stopping_reason: Optional[str] = None
        self._released = False
        self._started_at = _iso(self._clock())
        self._recovery_result: Optional[dict] = None

    # -- state accessors ---------------------------------------------------

    @property
    def state(self) -> ServiceHealth:
        return self._state

    @property
    def scheduler(self):
        """The wrapped bounded scheduler (read-only; used for reconciliation)."""
        return self._scheduler

    @property
    def passes(self) -> int:
        return self._passes

    def set_recovery_result(self, summary) -> None:
        """Persist the last startup-reconciliation summary into health."""
        self._recovery_result = {
            "scanned": getattr(summary, "scanned", 0),
            "rebound": getattr(summary, "rebound", 0),
            "quarantined_lost": getattr(summary, "quarantined_lost", 0),
            "takeover_candidates": getattr(summary, "takeover_candidates", 0),
            "process_alive": getattr(summary, "process_alive", 0),
            "left": getattr(summary, "left", 0),
        }

    def request_shutdown(self, reason: str) -> None:
        """Begin graceful shutdown (SPEC G1 §G): no new runnable work."""
        self._stopping_reason = reason
        self._state = ServiceHealth.STOPPING
        self._stop_event.set()

    def mark_failed(self, reason: str) -> None:
        """Mark the service FAILED (unrecoverable) and stop the loop.

        Distinct from DEGRADED (a contained per-pass error): FAILED means the
        service can no longer make progress and must be stopped/restarted by
        the operator.  Never mutates any job state.
        """
        self._stopping_reason = self._stopping_reason or reason
        self._last_error_code = (reason or "")[:128]
        self._state = ServiceHealth.FAILED
        self._stop_event.set()

    def release_instance(self, reason: str) -> bool:
        """Persist graceful-release evidence once (idempotent)."""
        if self._released:
            return True
        ok = self._instance.release(reason=reason)
        self._released = True
        return ok

    # -- loop ---------------------------------------------------------------

    def run_loop(self) -> LoopSummary:
        """Run the bounded background loop until shutdown or ``max_passes``."""
        # G1 (F7a): only transition STARTING -> READY; preserve an already-
        # FAILED/STOPPING state (e.g. mark_failed() before the first pass).
        if self._state is ServiceHealth.STARTING:
            self._state = ServiceHealth.READY
        try:
            while self._should_continue():
                self._run_one_iteration()
                self._passes += 1
                self._sleep()
        finally:
            self._finalize()
        return LoopSummary(
            passes=self._passes, errors=self._errors,
            last_pass_outcome=self._last_pass_outcome,
            stop_reason=self._stopping_reason or "loop_exit",
        )

    def _should_continue(self) -> bool:
        if self._stop_event.is_set():
            return False
        if self._max_passes is not None and self._passes >= self._max_passes:
            return False
        return True

    def _run_one_iteration(self) -> None:
        # Graceful-shutdown gate: no new claimable work once stopping.
        if self._stop_event.is_set():
            return
        self._last_work_had_activity = False
        outcome: Optional[str] = None
        wait_results = []
        try:
            result = self._scheduler.run_pass()
            outcome = getattr(result, "outcome", None)
            self._last_pass_at = _iso(self._clock())
        except Exception as exc:  # noqa: BLE001 - contained, bounded error code
            self._record_scheduler_error(type(exc).__name__)
        else:
            self._consecutive_errors = 0
        # F7(d): external waits + heartbeat run even in pass-error passes.
        # HIGH-2(a): re-verify the single-active singleton fence BEFORE polling
        # due waits — a stale/taken-over instance must abort with NO writes.
        fence_held = True
        if self._instance.identity is not None:
            fence_held = self._fence_held()
        wait_results = []
        ci_wait_results = []
        if not fence_held:
            self.mark_failed("instance_lease_lost")
        else:
            try:
                wait_results = self._external_wait_manager.check_due_waits()
            except Exception as exc:  # noqa: BLE001 - contained per-wait already; loop-level safety
                self._record_error(type(exc).__name__)
                wait_results = []
            if self._ci_wait_manager is not None:
                try:
                    ci_wait_results = self._ci_wait_manager.check_due_ci_waits(
                        instance_id=(self._instance.instance_id
                                     if self._instance.identity is not None
                                     else None))
                except Exception as exc:  # noqa: BLE001 - contained per-wait already
                    self._record_error(type(exc).__name__)
                    ci_wait_results = []
        self._last_pass_outcome = outcome
        self._last_work_had_activity = (
            (outcome is not None and outcome != OUTCOME_NO_WORK)
            or bool(wait_results)
            or bool(ci_wait_results)
        )
        # F3(b): heartbeat + fence checks are only meaningful once we actually
        # hold the single-active fence (a successful acquire sets ``identity``).
        # Pre-acquire loop passes (unit tests / pre-boot) skip them.
        if self._instance.identity is not None:
            # heartbeat fence — a False result or exception is a LOST fence
            # (we were taken over) -> FAILED + stop, no further passes.
            if self._passes % self._heartbeat_every == 0:
                self._heartbeat_or_fail()
            # re-check the fence after every pass (before the next claim/action).
            if not self._stop_event.is_set() and not self._fence_held():
                self.mark_failed("instance_lease_lost")

    def _heartbeat_or_fail(self) -> None:
        """Renew the instance lease; a lost fence -> FAILED + stop (F3b)."""
        try:
            ok = self._instance.heartbeat()
        except Exception as exc:  # noqa: BLE001
            self._record_error(type(exc).__name__)
            self.mark_failed("heartbeat_failed")
            return
        if not ok:
            self.mark_failed("instance_lease_lost")

    def _fence_held(self) -> bool:
        """True iff the persisted singleton row still belongs to this instance."""
        try:
            row = self._store.get_supervisor_instance()
        except Exception:  # noqa: BLE001
            return False
        return row is not None and row.get("instance_id") == self._instance.instance_id

    def _record_error(self, code: str) -> None:
        self._errors += 1
        self._last_error_code = (code or "")[:128]
        if self._state is not ServiceHealth.STOPPING:
            self._state = ServiceHealth.DEGRADED

    def _record_scheduler_error(self, code: str) -> None:
        """Record a scheduler-pass error and escalate to FAILED when structural."""
        self._record_error(code)
        self._consecutive_errors += 1
        if self._consecutive_errors >= self._max_consecutive_errors:
            self.mark_failed("repeated_scheduler_errors")

    def _sleep(self) -> None:
        duration = self._idle_sleep if not self._last_work_had_activity \
            else self._active_sleep
        # Interruptible sleep: slice into small increments and check the stop
        # event so SIGTERM is responsive even mid-sleep.
        elapsed = 0.0
        while elapsed < duration and not self._stop_event.is_set():
            step = min(self._slice, duration - elapsed)
            self._sleep_fn(step)
            elapsed += step

    def _finalize(self) -> None:
        # F7(a): preserve FAILED (a failed service must exit non-zero); only
        # downgrade to STOPPING for the normal shutdown path.
        if self._state is not ServiceHealth.FAILED:
            self._state = ServiceHealth.STOPPING
        self.release_instance(self._stopping_reason or "loop_exit")

    # -- health -------------------------------------------------------------

    def snapshot(self) -> HealthSnapshot:
        """Live machine-readable service health (SPEC G1 §J)."""
        db_accessible = True
        active_jobs = 0
        external_waits = 0
        try:
            self._store.get_supervisor_instance()
        except Exception:  # noqa: BLE001
            db_accessible = False
        try:
            active_jobs = len(self._store.list_supervisor_jobs(
                nonterminal_only=True))
        except Exception:  # noqa: BLE001
            active_jobs = 0
            db_accessible = False
        try:
            external_waits = sum(
                1 for w in self._store.list_external_waits()
                if w.get("terminal_observed_at") is None
            )
        except Exception:  # noqa: BLE001
            external_waits = 0
            db_accessible = False
        ident = self._instance.identity
        return HealthSnapshot(
            state=self._state.value,
            instance_id=self._instance.instance_id,
            boot_id=ident.boot_id if ident is not None else None,
            pid=ident.pid if ident is not None else None,
            process_start_ticks=(
                ident.process_start_ticks if ident is not None else None
            ),
            started_at=self._started_at,
            last_scheduler_pass_at=self._last_pass_at,
            db_accessible=db_accessible,
            active_job_count=active_jobs,
            external_wait_count=external_waits,
            recovery_result=self._recovery_result,
            last_error_code=self._last_error_code,
            stopping_reason=self._stopping_reason,
        )
