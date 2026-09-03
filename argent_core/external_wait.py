"""Phase B3 — External Wait Manager (persistent, bounded, non-LLM).

Implements the ARGENT ARCHITECTURE V1 FINAL §8 external-wait lifecycle in its
Phase-B scope:

* ``enter_waiting_external`` — the single trusted local path that atomically
  moves a leased ``RUNNING`` job to ``WAITING_EXTERNAL`` while persisting a
  validated ``external_waits`` row and releasing the agent/compute lease.
* ``check_due_waits`` — a bounded, deterministic, NON-LLM checker that only
  reads due waits, calls an allowlisted adapter, persists a bounded observation
  and applies bounded backoff (1/2/5/10/30 min + jitter, deadline).

Trust boundaries (§8 / §16):

* The wait's ``provider``/``ref``/``expected_subject``/``deadline_at`` are
  resolved from LOCAL POLICY / bound task facts — NEVER from agent output.  The
  agent may only *recommend* a wait via :class:`WaitRequest` (``kind`` +
  a bounded ``reason`` string); it can never choose a provider, repo, SHA, URL,
  checker, credential, poll/shell command or wake action.
* There are no secrets, no free shell/command/poll fields and no agent prompts
  anywhere in this module or in the ``external_waits`` table.
* An external observation is UNTRUSTED DATA.  Only the allowlisted adapter
  translates it into the narrow :class:`WaitObservation` type, and only a
  strictly bounded, validated subset of that observation ever takes effect.

No background daemon, no live GitHub/network in Phase B3 (a fake adapter is
used in tests).  A relevant event / deadline wakes the job to ``QUEUED``; it is
NEVER set directly to ``DONE``/``FAILED``/``RUNNING``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Protocol
from uuid import uuid4

from . import job_state

# ---------------------------------------------------------------------------
# Allowlists (closed sets; an observation/provider outside these never takes
# effect).
# ---------------------------------------------------------------------------

#: External wait kinds allowed to be persisted (mirrors ``job_state.WaitKind``,
#: minus ``NONE`` which is not an external wait).
ALLOWED_WAIT_KINDS: frozenset[str] = frozenset({
    job_state.WaitKind.CI.value,
    job_state.WaitKind.UPSTREAM.value,
    job_state.WaitKind.RATE_LIMIT.value,
    job_state.WaitKind.NETWORK.value,
    job_state.WaitKind.TIMER.value,
})

#: Allowlisted observation states.
OBS_PENDING = "PENDING"
OBS_READY = "READY"
OBS_FAILED = "FAILED"
OBS_UNKNOWN = "UNKNOWN"
ALLOWED_OBS_STATES: frozenset[str] = frozenset({
    OBS_PENDING, OBS_READY, OBS_FAILED, OBS_UNKNOWN,
})

#: Bounded provider/ref/subject string length (defense against unbounded
#: foreign data entering the ledger).
MAX_PROVIDER_LEN = 64
MAX_REF_LEN = 256
MAX_SUBJECT_LEN = 128

#: Bounded event_version (a non-negative integer is required; anything larger
#: is treated as malformed untrusted data).
MAX_EVENT_VERSION = 2 ** 31 - 1

#: Bounded reason string length for backoff results (defense against
#: unbounded foreign exception text entering the ledger).
MAX_REASON_LEN = 256


def _bounded_reason(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    return text[:MAX_REASON_LEN]

#: Backoff ladder (§8): 1/2/5/10/30 minutes.
BACKOFF_MINUTES: tuple[int, ...] = (1, 2, 5, 10, 30)


def next_check_delay_seconds(attempt: int, *, jitter: float = 0.0) -> int:
    """Bounded backoff delay for the next check (1/2/5/10/30 min + jitter).

    ``attempt`` is the NEW ``check_attempt`` value (>= 1).  ``jitter`` is an
    injectable relative factor in ``[-1, 1)``; it is added multiplicatively and
    then clamped to a floor of 1 second.  Default 0.0 (deterministic tests);
    production injects a real bounded jitter source.
    """
    n = max(1, int(attempt))
    minutes = BACKOFF_MINUTES[min(n - 1, len(BACKOFF_MINUTES) - 1)]
    base = minutes * 60
    if jitter:
        try:
            j = float(jitter)
        except (TypeError, ValueError):
            j = 0.0
        j = max(-1.0, min(0.99, j))
        base = int(round(base * (1.0 + j)))
    return max(1, base)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WaitRequest:
    """The agent's *recommendation* that a job should wait externally.

    Carries ONLY the wait ``kind`` and a bounded free-text ``reason``.  It
    deliberately has no provider/ref/subject/URL/command/credential/poll field:
    those are resolved from local policy / bound task facts by the supervisor.
    """

    kind: str
    reason: str = ""


@dataclass(frozen=True)
class WaitSpec:
    """A fully resolved, trusted wait specification (local policy output)."""

    kind: str
    provider: str
    ref: str
    expected_subject: Optional[str] = None
    deadline_at: Optional[str] = None
    next_check_at: Optional[str] = None


@dataclass(frozen=True)
class WaitObservation:
    """Narrow, bounded observation produced by an allowlisted adapter.

    UNTRUSTED DATA translated through the adapter into this closed type.  Only
    ``state``, ``subject`` and ``event_version`` are ever interpreted; nothing
    here can write code, approve, set DONE, expand scope, escalate a model or
    read credentials.
    """

    provider: str
    ref: str
    state: str
    subject: Optional[str] = None
    event_version: int = 0


class ExternalWaitAdapter(Protocol):
    """Allowlisted read-only provider interface (no webhook-to-command bridge).

    ``validate_ref`` gates a ref before it is persisted; ``check`` returns a
    bounded :class:`WaitObservation` for a due wait.  Adapters must be
    deterministic, bounded and side-effect free (except recording).  A real
    adapter (Phase J) must never expose credentials, poll/shell commands or
    write authority.
    """

    def validate_ref(self, ref: str) -> bool: ...

    def check(self, wait: dict) -> WaitObservation: ...


class FakeExternalWaitAdapter:
    """Deterministic, scriptable adapter (offline Phase B3 tests).

    Scripts are keyed by ``(provider, ref)``; each ``check`` pops the next
    observation (sticky last one once exhausted).  An unscripted key returns a
    benign ``PENDING`` observation so a missing provider can be distinguished
    from a pending state at the manager level (the manager itself never calls an
    adapter whose provider is not in its allowlist registry).
    """

    def __init__(self):
        self._queues: dict[tuple, list] = {}
        self._sticky: dict[tuple, WaitObservation] = {}
        self.checks: list[dict] = []
        self.fail_next: Optional[BaseException] = None

    def validate_ref(self, ref: str) -> bool:
        return isinstance(ref, str) and 0 < len(ref) <= MAX_REF_LEN

    def script(self, provider: str, ref: str, observations) -> None:
        self._queues[(provider, ref)] = list(observations)

    def set_sticky(self, provider: str, ref: str, obs: WaitObservation) -> None:
        self._queues[(provider, ref)] = []
        self._sticky[(provider, ref)] = obs

    def check(self, wait: dict) -> WaitObservation:
        self.checks.append(dict(wait))
        if self.fail_next is not None:
            exc = self.fail_next
            self.fail_next = None
            raise exc
        key = (wait["provider"], wait["ref"])
        q = self._queues.get(key)
        if q:
            obs = q.pop(0)
            self._sticky[key] = obs
            return obs
        obs = self._sticky.get(key)
        if obs is not None:
            return obs
        return WaitObservation(
            provider=wait["provider"], ref=wait["ref"], state=OBS_PENDING,
            event_version=0,
        )


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WaitCheckResult:
    """Bounded outcome of processing one due wait."""

    wait_id: str
    outcome: str  # 'pending' | 'woke' | 'ignored' | 'adapter_error' | 'malformed' | 'unknown_provider'
    reason: Optional[str] = None
    job_id: Optional[str] = None
    queue_reason: Optional[str] = None
    next_check_at: Optional[str] = None


class ExternalWaitManager:
    """Bounded non-LLM wait checker + the trusted wait-entry path.

    ``adapters`` is an allowlist registry ``{provider_key: adapter}``; a
    provider key absent from the registry can NEVER be entered or checked
    (fail-closed).  ``clock`` (``FakeClock``-style callable -> datetime) and
    ``jitter`` (callable -> float) are injectable for determinism.
    """

    def __init__(
        self,
        store,
        *,
        adapters: Optional[dict] = None,
        clock: Optional[Callable[[], datetime]] = None,
        jitter: Optional[Callable[[], float]] = None,
        kinds: Optional[frozenset] = None,
    ):
        self._store = store
        self._adapters = dict(adapters or {})
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._jitter = jitter or (lambda: 0.0)
        #: Optional wait-kind allowlist.  When set, only due waits whose
        #: ``kind`` is in the set are processed (I3-C1 uses this to let the
        #: CI wait manager own ``CI`` waits exclusively).  Default None = all.
        self._kinds = frozenset(kinds) if kinds is not None else None

    # -- helpers -----------------------------------------------------------

    def _now_iso(self) -> str:
        return _iso(self._clock())

    def _validate_kind(self, kind: str) -> None:
        if kind not in ALLOWED_WAIT_KINDS:
            raise ValueError(
                f"invalid wait kind {kind!r}; expected one of "
                f"{sorted(ALLOWED_WAIT_KINDS)}"
            )

    def _validate_provider(self, provider: str) -> None:
        if not isinstance(provider, str) or not provider:
            raise ValueError("provider must be a non-empty string")
        if len(provider) > MAX_PROVIDER_LEN:
            raise ValueError(f"provider exceeds {MAX_PROVIDER_LEN} chars")
        if provider not in self._adapters:
            raise ValueError(f"provider {provider!r} is not allowlisted")

    def _validate_ref(self, provider: str, ref: str) -> None:
        adapter = self._adapters[provider]
        if not isinstance(ref, str) or not ref:
            raise ValueError("ref must be a non-empty string")
        if len(ref) > MAX_REF_LEN:
            raise ValueError(f"ref exceeds {MAX_REF_LEN} chars")
        if not adapter.validate_ref(ref):
            raise ValueError(f"ref {ref!r} rejected by provider {provider!r}")

    def _validate_subject(self, subject: Optional[str]) -> Optional[str]:
        if subject is None:
            return None
        if not isinstance(subject, str) or len(subject) > MAX_SUBJECT_LEN:
            raise ValueError(f"expected_subject exceeds {MAX_SUBJECT_LEN} chars")
        return subject

    def _build_wait_row(self, job_id: str, spec: WaitSpec, now_iso: str) -> dict:
        self._validate_kind(spec.kind)
        self._validate_provider(spec.provider)
        self._validate_ref(spec.provider, spec.ref)
        subject = self._validate_subject(spec.expected_subject)
        # F4: a CI wait REQUIRES a validated non-empty (SHA-like) expected
        # subject at creation time; a CI wait without one is rejected.
        if spec.kind == job_state.WaitKind.CI.value \
                and (subject is None or not subject.strip()):
            raise ValueError("CI wait requires a non-empty expected_subject")
        deadline = spec.deadline_at
        if deadline is not None:
            # A deadline must be a parseable future timestamp (bounded).
            _parse_iso(deadline)
        next_check_at = spec.next_check_at
        if next_check_at is None:
            next_check_at = _iso(_parse_iso(now_iso) + timedelta(seconds=60))
        else:
            _parse_iso(next_check_at)
        return {
            "wait_id": "wait:" + uuid4().hex,
            "job_id": job_id,
            "kind": spec.kind,
            "provider": spec.provider,
            "ref": spec.ref,
            "expected_subject": subject,
            "last_observed_state": None,
            "next_check_at": next_check_at,
            "deadline_at": deadline,
            "check_attempt": 0,
            "event_version": 0,
            "terminal_observed_at": None,
            "ci_policy": None,
            "ci_evidence": None,
            "created_at": now_iso,
            "updated_at": now_iso,
        }

    # -- trusted wait entry (the ONLY path to WAITING_EXTERNAL) -------------

    def enter_waiting_external(
        self,
        job_id: str,
        *,
        spec: WaitSpec,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> dict:
        """Atomically move a leased RUNNING job to WAITING_EXTERNAL.

        Resolves/validates the spec (allowlist provider/ref/subject), then calls
        the store's single-transaction primitive that persists the wait row AND
        transitions the job AND releases the lease in one ``BEGIN IMMEDIATE``.
        A failed transition rolls back the whole transaction (no wait row, job
        stays RUNNING).
        """
        now_iso = self._now_iso()
        wait_row = self._build_wait_row(job_id, spec, now_iso)
        return self._store.transition_to_waiting_external(
            job_id,
            wait_row=wait_row,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
        )

    # -- bounded checker ----------------------------------------------------

    def check_due_waits(self, max_items: int = 10) -> list[WaitCheckResult]:
        """Read only DUE waits and process them (bounded, deterministic, no LLM).

        A wait is due when it is not terminal and either ``next_check_at`` or
        ``deadline_at`` has passed.  Exactly one outcome per wait; a relevant
        event/deadline wakes the job exactly once (dedup by ``event_version``
        and terminal-idempotence).
        """
        if max_items <= 0:
            return []
        now = self._now_iso()
        due = self._store.list_due_external_waits(now, limit=max_items)
        if self._kinds is not None:
            due = [w for w in due if w["kind"] in self._kinds]
        results: list[WaitCheckResult] = []
        # F5: process each due wait in isolation — an uncaught failure on one
        # untrusted observation must NEVER abort the rest of the bounded pass.
        for w in due:
            try:
                results.append(self._process_wait(w, now))
            except Exception as exc:  # noqa: BLE001 - fail-closed per wait
                results.append(self._backoff(
                    w, now, observed_state=None,
                    outcome="adapter_error",
                    reason=_bounded_reason(type(exc).__name__),
                ))
        return results

    def _validate_observation(
        self, obs: WaitObservation, wait: dict,
    ) -> Optional[tuple]:
        """Strictly validate an UNTRUSTED observation (F5).

        Returns ``(outcome, reason)`` for a malformed/mismatched observation,
        or ``None`` when it passes.  provider/ref/subject must be bounded
        non-empty strings (or ``None`` for subject); event_version must be a
        non-negative bounded integer; state must be in the allowlist.  The
        provider and ref must EXACTLY match the wait.
        """
        provider = obs.provider
        if not isinstance(provider, str) or not provider \
                or len(provider) > MAX_PROVIDER_LEN:
            return ("malformed", "bad_provider")
        if provider != wait["provider"]:
            return ("ignored", "wrong_provider")
        ref = obs.ref
        if not isinstance(ref, str) or not ref or len(ref) > MAX_REF_LEN:
            return ("malformed", "bad_ref")
        if ref != wait["ref"]:
            return ("ignored", "wrong_ref")
        state = obs.state
        if state not in ALLOWED_OBS_STATES:
            return ("malformed", _bounded_reason(f"bad_state:{state!r}"))
        ev = obs.event_version
        if not isinstance(ev, int) or isinstance(ev, bool) \
                or ev < 0 or ev > MAX_EVENT_VERSION:
            return ("malformed", "bad_event_version")
        subject = obs.subject
        if subject is not None:
            if not isinstance(subject, str) or len(subject) > MAX_SUBJECT_LEN:
                return ("malformed", "bad_subject")
        return None

    def _process_wait(self, wait: dict, now: str) -> WaitCheckResult:
        # Deadline is a pure local fact and wins over any observation.
        deadline = wait["deadline_at"]
        if deadline is not None and deadline <= now:
            return self._wake(
                wait, None, now,
                queue_reason=job_state.QueueReason.WAIT_DEADLINE.value,
                error_class=job_state.ErrorClass.EXTERNAL.value,
                reason="external_deadline",
            )

        adapter = self._adapters.get(wait["provider"])
        if adapter is None:
            return self._backoff(wait, now, observed_state=None,
                                 outcome="unknown_provider",
                                 reason="provider_not_allowlisted")

        try:
            obs = adapter.check(wait)
        except BaseException as exc:
            return self._backoff(wait, now, observed_state=None,
                                 outcome="adapter_error",
                                 reason=_bounded_reason(type(exc).__name__))

        if not isinstance(obs, WaitObservation):
            return self._backoff(wait, now, observed_state=None,
                                 outcome="adapter_error",
                                 reason="bad_observation_type")

        # F5: strict, bounded validation of the UNTRUSTED observation fields.
        error = self._validate_observation(obs, wait)
        if error is not None:
            outcome, reason = error
            safe_state = obs.state if obs.state in ALLOWED_OBS_STATES else None
            return self._backoff(wait, now, observed_state=safe_state,
                                 outcome=outcome, reason=reason)

        # Pending / unknown is NOT an event: bounded backoff, no wake.  This
        # is checked BEFORE event_version dedup, because a pending observation
        # carries no meaningful version.
        if obs.state in (OBS_PENDING, OBS_UNKNOWN):
            return self._backoff(wait, now, observed_state=obs.state,
                                 outcome="pending", reason="pending")

        # F4: a required expected_subject (CI) wakes ONLY on an exact subject
        # match.  A READY/FAILED-style terminal event WITHOUT a subject never
        # wakes (missing_subject); a present-but-different subject is stale
        # evidence (stale_subject).
        expected = wait["expected_subject"]
        if expected is not None:
            if obs.subject is None:
                return self._backoff(wait, now, observed_state=obs.state,
                                     outcome="ignored",
                                     reason="missing_subject")
            if obs.subject != expected:
                return self._backoff(wait, now, observed_state=obs.state,
                                     outcome="ignored", reason="stale_subject")

        # Event dedup: an older/equal event version is stale and has no effect
        # (applies to RELEVANT terminal/change events: READY/FAILED).
        if obs.event_version <= wait["event_version"]:
            return self._backoff(wait, now, observed_state=obs.state,
                                 outcome="ignored", reason="stale_version")

        # A relevant terminal/change event wakes the job exactly once.
        return self._wake(
            wait, obs, now,
            queue_reason=job_state.QueueReason.WAIT_EVENT.value,
            error_class=job_state.ErrorClass.NONE.value,
            reason="wait_event",
        )

    def _backoff(
        self, wait: dict, now: str, *, observed_state, outcome: str, reason: str,
    ) -> WaitCheckResult:
        attempt = wait["check_attempt"] + 1
        delay = next_check_delay_seconds(attempt, jitter=self._jitter())
        next_check_at = _iso(_parse_iso(now) + timedelta(seconds=delay))
        updates = {
            "check_attempt": attempt,
            "next_check_at": next_check_at,
            "updated_at": now,
        }
        if observed_state is not None:
            updates["last_observed_state"] = observed_state
        self._store._update_external_wait(wait["wait_id"], **updates)
        return WaitCheckResult(
            wait_id=wait["wait_id"], outcome=outcome, reason=reason,
            job_id=wait["job_id"], next_check_at=next_check_at,
        )

    def _wake(
        self, wait: dict, obs: Optional[WaitObservation], now: str, *,
        queue_reason: str, error_class: str, reason: str,
    ) -> WaitCheckResult:
        updated = self._store.complete_wait_and_requeue(
            wait["wait_id"],
            queue_reason=queue_reason,
            error_class=error_class,
            observed_state=(obs.state if obs is not None else None),
            event_version=(obs.event_version if obs is not None
                           else wait["event_version"]),
            now_iso=now,
        )
        if updated is None:
            # Already terminal / already left WAITING_EXTERNAL (dedup).
            return WaitCheckResult(
                wait_id=wait["wait_id"], outcome="ignored", reason="already_handled",
                job_id=wait["job_id"],
            )
        return WaitCheckResult(
            wait_id=wait["wait_id"], outcome="woke", reason=reason,
            job_id=wait["job_id"], queue_reason=queue_reason,
        )
