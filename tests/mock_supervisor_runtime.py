"""Deterministic fakes for the Phase 2C supervisor subsystem.

Implements ``RunStatusProvider``, ``RunLauncher`` and a controllable ``FakeClock``
with no real OpenClaw runs, no sleeps and no network.  The fake runtime state is
held in-memory; a "restart" test re-scripts a fresh provider to reflect the
(simulated) persistent runtime facts while reloading the DB-backed Core/Supervisor.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from argent_core.models import Role
from argent_core.supervisor import (
    AGENT_IDS,
    RunLookup,
    RunObservation,
    RunStatus,
    session_key_for,
)


class FakeClock:
    """Controllable monotonic-ish clock (no sleep in tests)."""

    def __init__(self, start: Optional[datetime] = None):
        self._t = start or datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += timedelta(seconds=seconds)

    def set(self, dt: datetime) -> None:
        self._t = dt

    def now_iso(self) -> str:
        return self._t.astimezone(timezone.utc).isoformat()


def make_run_observation(
    *,
    dispatch_id: str,
    role: Role,
    status: RunStatus,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    thinking_tier: Optional[str] = None,
    result: Optional[dict] = None,
    result_hash: Optional[str] = None,
    authoritative_not_found: bool = False,
    error_code: Optional[str] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
) -> RunObservation:
    agent_id = AGENT_IDS[role]
    if isinstance(status, str):
        status = RunStatus(status)
    return RunObservation(
        status=status,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        provider=provider,
        model=model,
        thinking_tier=thinking_tier,
        started_at=started_at,
        finished_at=finished_at,
        result=result,
        result_hash=result_hash,
        authoritative_not_found=authoritative_not_found,
        evidence_id=f"fake:{dispatch_id}",
        error_code=error_code,
    )


def canonical_binding(dispatch) -> tuple:
    """The canonical (provider, model, thinking, session_key) for a dispatch."""
    return (
        dispatch.expected_agent_class,
        dispatch.expected_model_class,
        dispatch.expected_thinking_tier,
        session_key_for(AGENT_IDS[dispatch.role], dispatch.id),
    )


class FakeRunStatusProvider:
    """Scriptable ``RunStatusProvider`` with per-dispatch observation queues.

    ``script()`` queues observations that are consumed one-per-``observe()``;
    the most recently returned observation is sticky (repeats once the queue is
    exhausted).  ``set_current()`` sets a single sticky observation directly.
    Unscripted dispatches return an authoritative NOT_FOUND.  Failure injection
    can raise on the next call.
    """

    def __init__(self):
        self._queue: dict[str, list] = {}
        self._sticky: dict[str, RunObservation] = {}
        self.observe_calls: list[RunLookup] = []
        self.fail_next = None  # exception to raise on the next observe()

    def script(self, dispatch_id: str, observations) -> None:
        self._queue[dispatch_id] = list(observations)

    def set_current(self, dispatch_id: str, observation: RunObservation) -> None:
        self._queue[dispatch_id] = []
        self._sticky[dispatch_id] = observation

    def append(self, dispatch_id: str, observation: RunObservation) -> None:
        self._queue.setdefault(dispatch_id, []).append(observation)

    def observe(self, lookup: RunLookup) -> RunObservation:
        self.observe_calls.append(lookup)
        if self.fail_next is not None:
            exc = self.fail_next
            self.fail_next = None
            raise exc
        q = self._queue.get(lookup.dispatch_id)
        if q:
            obs = q.pop(0)
            self._sticky[lookup.dispatch_id] = obs
            return obs
        obs = self._sticky.get(lookup.dispatch_id)
        if obs is not None:
            return obs
        return make_run_observation(
            dispatch_id=lookup.dispatch_id,
            role=Role(lookup.agent_id[len("argent-"):]),
            status=RunStatus.NOT_FOUND,
            authoritative_not_found=True,
        )

    # Convenience: script a bound RUNNING then SUCCEEDED flow for a dispatch.
    def script_run_then_succeed(self, dispatch, result: dict) -> None:
        provider, model, thinking, session = canonical_binding(dispatch)
        self.script(dispatch.id, [
            make_run_observation(
                dispatch_id=dispatch.id, role=dispatch.role, status=RunStatus.RUNNING,
                run_id="run-1", session_id=session, provider=provider, model=model,
                thinking_tier=thinking,
            ),
            make_run_observation(
                dispatch_id=dispatch.id, role=dispatch.role, status=RunStatus.SUCCEEDED,
                run_id="run-1", session_id=session, provider=provider, model=model,
                thinking_tier=thinking, result=result,
            ),
        ])


class FakeRunLauncher:
    """Records spawns; optional failure injection."""

    def __init__(self):
        self.spawns: list[dict] = []
        self.fail_next = None

    def spawn(self, *, agent_id, dispatch_id, message_file, timeout_seconds) -> None:
        if self.fail_next is not None:
            exc = self.fail_next
            self.fail_next = None
            raise exc
        self.spawns.append({
            "agent_id": agent_id,
            "dispatch_id": dispatch_id,
            "message_file": str(message_file),
            "timeout_seconds": timeout_seconds,
        })


class FakeWaiter:
    """Advances the FakeClock instead of sleeping (no busy-loop)."""

    def __init__(self, clock: FakeClock):
        self._clock = clock
        self.wait_calls = 0

    def wait_until(self, wake_at, stop_event=None) -> bool:
        self.wait_calls += 1
        if stop_event is not None and stop_event.is_set():
            return True
        if not wake_at:
            return False
        from argent_core.supervisor import _parse_iso
        try:
            target = _parse_iso(wake_at)
        except (ValueError, TypeError):
            return False
        now = self._clock().timestamp()
        if target > now:
            self._clock.advance(target - now)
        return True


class AutoRunStatusProvider:
    """Auto-advancing provider that drives a whole workflow to DONE offline.

    - unbound (PENDING) dispatch -> RUNNING with the canonical binding;
    - bound (RUNNING) dispatch -> SUCCEEDED with a role-valid result.

    ``result_builder(role, task_id, dispatch_id) -> dict`` supplies the envelope
    (defaults to ``mock_runtime.build_output``).  Write roles carry no patch_set
    by default (the supervisor treats an empty patch set as a satisfied no-op).
    """

    def __init__(self, core, result_builder=None):
        self.core = core
        self.result_builder = result_builder
        self.observe_calls: list[RunLookup] = []

    def _builder(self):
        if self.result_builder is not None:
            return self.result_builder
        from mock_runtime import build_output
        return build_output

    def observe(self, lookup: RunLookup) -> RunObservation:
        self.observe_calls.append(lookup)
        d = self.core.queries.get_dispatch(lookup.dispatch_id)
        if d is None:
            return make_run_observation(
                dispatch_id=lookup.dispatch_id, role=Role.LEAD,
                status=RunStatus.NOT_FOUND, authoritative_not_found=True,
            )
        # E2 F1: the closing review is ALWAYS writer-independent.  Once the QA
        # step completes (the last write step before the reviewer), bind the
        # job's writer to the last implementer dispatch so the happy-path
        # reviewer can dispatch a DIFFERENT model (offline test infra for the
        # external writer-binding concern).
        if d.role is Role.QA and d.child_session_id is not None:
            writers = [
                x for x in self.core.queries.list_dispatches(d.task_id)
                if x.role is Role.IMPLEMENTER
            ]
            if writers:
                self.core._store._conn.execute(
                    "UPDATE supervisor_jobs SET writer_dispatch_id = ? "
                    "WHERE task_id = ?",
                    (writers[-1].id, d.task_id),
                )
        provider, model, thinking, session = canonical_binding(d)
        if d.child_session_id is None:
            return make_run_observation(
                dispatch_id=d.id, role=d.role, status=RunStatus.RUNNING,
                run_id=f"auto-run-{d.id[:8]}", session_id=session, provider=provider,
                model=model, thinking_tier=thinking,
            )
        result = self._builder()(d.role, d.task_id, d.id)
        return make_run_observation(
            dispatch_id=d.id, role=d.role, status=RunStatus.SUCCEEDED,
            run_id=d.openclaw_run_id, session_id=d.child_session_id,
            provider=provider, model=model, thinking_tier=thinking,
            result=result,
        )


def allow_governor():
    """Deterministic always-ALLOW resource governor + no-op snapshot provider.

    Scheduler unit tests that prove scheduling/fencing/wait semantics must not
    depend on the REAL host resource governor: under machine memory pressure a
    new-claim preflight can be DEFERred, intermittently breaking full-suite
    runs (pre-existing load-sensitive race, reproduced at the I3-B base;
    production single-supervisor path unaffected).  Resource admission itself
    is exercised in the C-phase suites with scripted governors.
    """
    from argent_core.resource_governor import AdmissionDecision
    from argent_core.resource_policy import ResourceClass, ResourcePolicy

    class _AllowGovernor:
        def __init__(self):
            self.policy = ResourcePolicy()
            self.decision = AdmissionDecision(
                resource_class=ResourceClass.LIGHT.value,
                policy_version="1",
                snapshot_ref="snap-test",
                decision="ALLOW",
                reason_code="OK",
                next_eligible_at=None,
                effective_limits={},
                timestamp="2026-09-01T00:00:00+00:00",
            )
            self.calls = []

        def decide(self, **kwargs):
            self.calls.append(kwargs)
            return self.decision

    class _NoopSnapshotProvider:
        def capture(self, *args, **kwargs):
            return {}

    return _AllowGovernor(), _NoopSnapshotProvider()


def make_deterministic_scheduler(supervisor, *, owner_instance_id, lease_ttl_seconds):
    """Scheduler with canned ALLOW admission (no real-host dependency)."""
    from argent_core.scheduler import Scheduler

    governor, snapshot = allow_governor()
    return Scheduler(
        supervisor,
        owner_instance_id=owner_instance_id,
        lease_ttl_seconds=lease_ttl_seconds,
        resource_governor=governor,
        snapshot_provider=snapshot,
    )
