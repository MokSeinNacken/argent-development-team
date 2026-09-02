"""Phase B2 — bounded durable scheduler passes + restart reconciliation.

B1 introduced the durable queue/lease primitives (``claim_job`` /
``claim_next_job`` / ``renew_lease`` / ``release_lease`` /
``assert_lease_current``) and the fencing token (``job_id`` +
``owner_instance_id`` + ``lease_epoch`` + ``lease_expires_at`` +
``facts_version``).  This module adds the two *scheduler-level* pieces that make
the supervisor durable across process restarts WITHOUT holding an agent in the
supervisor process:

* :class:`Scheduler.run_pass` — a bounded pass that performs EXACTLY ONE safe
  step of exactly one job and then returns.  There is no
  ``while-not-terminal`` loop holding an agent: the agent is always spawned
  detached (the existing ``RunLauncher``), and a pass merely advances the
  persisted ledger by one step and then renews/releases the job lease.

* :class:`Scheduler.reconcile_after_restart` — a deterministic, idempotent scan
  of persisted facts (a reopened DB / fresh ``Supervisor`` instance; no
  in-memory cache is authoritative) that applies the D-rules: everything that
  is decidable without a process registry.

The job lease (``owner_instance_id`` + ``lease_epoch`` + ``lease_expires_at``)
remains the single fencing authority.  A *scheduler singleton lease* is
deliberately NOT introduced here: the job-level atomic claim already gives the
exactly-one-claim-winner guarantee needed for dual-supervisor protection, so a
separate scheduler lease would add a second authority with no provable benefit
(the job lease/epoch is the real fencing token).  Deferred to Phase G — see the
phase report for the full rationale.

Renewal policy (Design-Vorgabe 3):

    A pass renews the job lease **only** when, immediately after its safe step,
    the job is still ``RUNNING`` (``primary_state=RUNNING`` / ``status=ACTIVE``)
    AND this scheduler still holds the current, unexpired lease
    (``lease_is_current``).  "The agent process still exists" is NOT evidence
    (there is no process registry): only a persisted RUNNING job that this
    scheduler is actively driving authorises renewal.  The TTL is caller
    supplied local policy, bounded by ``store.MAX_LEASE_TTL_SECONDS``; an
    expired lease is never silently extended (``renew_lease`` CAS); progress is
    never treated as liveness.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from . import job_state
from .context_pack import is_permanent_context_code
from .host_snapshot import HostSnapshotProvider
from .models import LeaseError, LeaseFencedError, NotFound
from .process_registry import (
    IDENTITY_SAME,
    ProcessIdentityProvider,
    ProcessRegistry,
)
from .resource_governor import AdmissionVerdict, ResourceGovernor, ResourceReasonCode
from .resource_policy import ResourceClass
from .resource_recovery import (
    FailureClass,
    RecoveryDecision,
    RecoveryPolicy,
    classify_failure,
    decide_recovery,
    is_resource_failure,
    next_eligible_at_after,
    reason_code_for_failure,
)
from .store import MAX_LEASE_TTL_SECONDS
from .supervisor import (
    ActionOutcome,
    ReconcileAction,
    ReconcileDecision,
    Supervisor,
)
from .worktree import (
    V_AMBIGUOUS_WRITER,
    V_BLOCKED_DIVERGED,
    V_CLEANUP_PENDING,
    V_KEEP_DIRTY,
    V_LOST,
    GitProvenanceProvider,
    WorktreeBinding,
    WorktreeEvidence,
    classify_worktree_recovery,
)

#: Default lease TTL (seconds) used by a Scheduler when the caller does not
#: override it.  Local policy; bounded by ``store.MAX_LEASE_TTL_SECONDS``.
DEFAULT_LEASE_TTL_SECONDS = 300

#: Scheduler outcome strings (public, stable for callers/tests).
OUTCOME_NO_WORK = "no_work"
OUTCOME_STEPPED = "stepped"
OUTCOME_RENEWED = "renewed"
OUTCOME_RELEASED = "released"
OUTCOME_FENCED = "fenced"
OUTCOME_RESOURCE_DEFERRED = "resource_deferred"
OUTCOME_RESOURCE_DENIED = "resource_denied"
OUTCOME_RESOURCE_LOST = "resource_lost"
OUTCOME_RESOURCE_RECOVERED = "resource_recovered"
# D1 (Phase D): a Context-Pack build failure (fail-closed, no dispatch).
OUTCOME_CONTEXT_FAILED = "context_build_failed"

#: Far-future ``next_eligible_at`` delay (seconds) for DENY_LOCAL: prevents an
#: identical automatic retry without inventing a new primary state.  The job
#: stays QUEUED but will not be re-claimed until this bounded horizon elapses.
DENY_LOCAL_RETRY_SECONDS = 24 * 3600


@dataclass(frozen=True)
class SchedulerPassResult:
    """Outcome of a single bounded scheduler pass (Phase B2)."""

    outcome: str
    job_id: Optional[str] = None
    decision: Optional[ReconcileDecision] = None
    action_outcome: Optional[ActionOutcome] = None
    lease_epoch: Optional[int] = None
    detail: Optional[str] = None


@dataclass(frozen=True)
class RestartReconcileSummary:
    """Deterministic summary of :meth:`Scheduler.reconcile_after_restart`."""

    scanned: int = 0
    rebound: int = 0
    quarantined_lost: int = 0
    takeover_candidates: int = 0
    blocked_worktree: int = 0
    foreign_lease_kept: int = 0
    process_alive: int = 0
    resource_recovered: int = 0
    left: int = 0
    details: Tuple[tuple, ...] = ()


class Scheduler:
    """Bounded durable scheduler: one safe step per pass, no held agent.

    The scheduler is the Phase B2 replacement for the tight
    ``while-not-terminal`` driver.  It claims a claimable job (atomic, epoch+1),
    performs exactly one reconcile→perform step under the fencing token, then
    either renews the lease (still actively RUNNING and held) or clears it
    (job left RUNNING via backoff/requeue).  Every pass returns; it never holds
    an agent in-process.

    ``run_until_terminal`` on :class:`~argent_core.supervisor.SupervisorLoop`
    remains available as the documented single-job compatibility entry point.
    """

    def __init__(
        self,
        supervisor: Supervisor,
        *,
        owner_instance_id: str,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        renew_ttl_seconds: Optional[int] = None,
        resource_governor: Optional[ResourceGovernor] = None,
        snapshot_provider: Optional[HostSnapshotProvider] = None,
        # C2: execution enforcement (opt-in).  ``enforcer``/``scope_backend`` are
        # resolved from the injected value -> the supervisor's value -> None (no
        # auto-creation, so existing deterministic C1/B tests stay unaffected).
        enforcer=None,
        scope_backend=None,
        # C3: bounded recovery policy (bounded retry only, never escalation).
        recovery_policy=None,
        # G1 (F6): optional stop-predicate checked immediately before expensive
        # spawn/test actions so a SIGTERM mid-pass aborts the pass (no spawn).
        stop_check=None,
    ):
        if not isinstance(owner_instance_id, str) or not owner_instance_id.strip():
            raise ValueError("owner_instance_id must be a non-empty string")
        if not isinstance(lease_ttl_seconds, int) or lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be a positive integer")
        if lease_ttl_seconds > MAX_LEASE_TTL_SECONDS:
            raise ValueError(
                f"lease_ttl_seconds {lease_ttl_seconds} exceeds "
                f"MAX_LEASE_TTL_SECONDS ({MAX_LEASE_TTL_SECONDS})"
            )
        renew_ttl_seconds = (
            lease_ttl_seconds if renew_ttl_seconds is None else renew_ttl_seconds
        )
        if not isinstance(renew_ttl_seconds, int) or renew_ttl_seconds <= 0:
            raise ValueError("renew_ttl_seconds must be a positive integer")
        if renew_ttl_seconds > MAX_LEASE_TTL_SECONDS:
            raise ValueError(
                f"renew_ttl_seconds {renew_ttl_seconds} exceeds "
                f"MAX_LEASE_TTL_SECONDS ({MAX_LEASE_TTL_SECONDS})"
            )
        self._supervisor = supervisor
        self.owner_instance_id = owner_instance_id
        self._lease_ttl_seconds = lease_ttl_seconds
        self._renew_ttl_seconds = renew_ttl_seconds
        # C1: resource preflight.  A test/fake governor or snapshot provider may
        # be injected; otherwise fall back to the supervisor's injected one, then
        # to the real (read-only) defaults.
        self._governor = (
            resource_governor
            or getattr(supervisor, "_resource_governor", None)
            or ResourceGovernor()
        )
        self._snapshot_provider = (
            snapshot_provider
            or getattr(supervisor, "_snapshot_provider", None)
        )
        # C1/F1: when neither a test fake nor the supervisor's injected provider
        # is present, wire the real host provider WITH a trusted store-backed
        # active-jobs reader so concurrency rules actually see sibling RUNNING
        # jobs (dual-supervisor protection).  The reader returns None (UNKNOWN)
        # on a store read error — never an empty set.
        if self._snapshot_provider is None:
            self._snapshot_provider = HostSnapshotProvider(
                active_jobs_reader=self._store_active_jobs_reader(),
            )
        # Job currently being preflighted (excluded from the active-jobs view so
        # a job never concurrency-blocks itself).  Set by ``_resource_preflight``.
        self._preflight_job_id: Optional[str] = None

        # C2: wire the resolved governor/provider back onto the supervisor so
        # the supervisor's ``_perform_spawn_run`` performs its own FRESH C1
        # admission from the SAME policy + snapshot provider this scheduler
        # uses for its admission gate (single source — never from agent output).
        if getattr(supervisor, "_resource_governor", None) is None:
            supervisor._resource_governor = self._governor
        if getattr(supervisor, "_snapshot_provider", None) is None:
            supervisor._snapshot_provider = self._snapshot_provider
        # Resolve and wire the enforcement path.  F1: the Supervisor already
        # wires a real ``ExecutionEnforcer(SystemdRunScopeBackend())`` by
        # default, so ``supervisor._enforcer`` is never ``None`` in production;
        # an explicitly injected fake overrides it here.
        enforcer = enforcer or getattr(supervisor, "_enforcer", None)
        if enforcer is not None:
            supervisor._enforcer = enforcer
            supervisor._scope_backend = (
                scope_backend or getattr(supervisor, "_scope_backend", None)
            )
        # C3: bounded recovery policy (injectable for tests).
        self._recovery_policy = recovery_policy or RecoveryPolicy()
        # G1 (F6): stop-predicate for mid-pass shutdown abort.
        self._stop_check = stop_check

    # -- G1 (F6): mid-pass shutdown gate -----------------------------------

    def set_stop_check(self, stop_check) -> None:
        """Wire a stop-predicate (e.g. the runtime's ``stop_event.is_set``)."""
        self._stop_check = stop_check

    def _stop_requested(self) -> bool:
        """True when a shutdown has been requested (abort before spawn)."""
        return bool(self._stop_check is not None and self._stop_check())

    # ------------------------------------------------------------------ pass

    def _resolve_target(
        self, job_id: Optional[str]
    ) -> Optional[Tuple[str, int, bool]]:
        """Resolve the job this pass will step.

        Returns ``(job_id, lease_epoch, held)`` where ``held`` is True when the
        lease was already ours (no new claim), or None when there is nothing
        claimable/continuable.  QUEUED jobs are claimed via the B1
        ``claim_job``/``claim_next_job`` predicates; a RUNNING job is NEVER
        directly claimed (F1) — an expired RUNNING job is taken over ONLY via
        the evidence-bound :meth:`_try_recover_takeover` recovery path.
        """
        if job_id is None:
            claimed = self._supervisor.store.claim_next_job(
                owner_instance_id=self.owner_instance_id,
                ttl_seconds=self._lease_ttl_seconds,
            )
            if claimed is not None:
                return (claimed["id"], claimed["lease_epoch"], False)
            # G1 (F1): the background loop must ALSO steer continuation-capable
            # own RUNNING jobs and evidence-bound expired-lease recovery — not
            # only fresh QUEUED claims (otherwise a multi-step job stalls at
            # RUNNING forever in background operation).
            return self._resolve_loop_continuation()

        return self._resolve_explicit_target(job_id)

    def _resolve_explicit_target(
        self, job_id: str
    ) -> Optional[Tuple[str, int, bool]]:
        row = self._supervisor.store._job_row(job_id)
        if row is None:
            return None
        # Already held with a still-valid lease -> continue (no re-claim).  F2:
        # only continue when the job is still RUNNING (status=ACTIVE) AND no
        # future wake/eligibility deadline blocks continuation — a persisted
        # BACKOFF must NOT be run immediately via its lingering lease.
        if row["owner_instance_id"] == self.owner_instance_id and \
                row.get("primary_state") == job_state.PrimaryState.RUNNING.value and \
                self._supervisor.store.lease_is_current(
                    job_id, self.owner_instance_id, row["lease_epoch"],
                ):
            eligible = row.get("next_eligible_at")
            if eligible is None or eligible <= self._supervisor._now_iso():
                return (job_id, row["lease_epoch"], True)
        # F1: a RUNNING job with an EXPIRED concrete lease is a takeover
        # candidate — route it through the evidence-bound recovery path ONLY.
        # (A still-valid foreign lease is never touched here.)
        if row.get("primary_state") == job_state.PrimaryState.RUNNING.value \
                and row.get("lease_expires_at") is not None \
                and row.get("lease_expires_at") <= self._supervisor._now_iso():
            taken = self._try_recover_takeover(job_id, row)
            if taken is not None:
                return (job_id, taken["lease_epoch"], False)
            return None
        # Otherwise attempt a normal claim (QUEUED only; RUNNING is excluded).
        try:
            claimed = self._supervisor.store.claim_job(
                job_id,
                owner_instance_id=self.owner_instance_id,
                ttl_seconds=self._lease_ttl_seconds,
            )
        except LeaseError:
            return None
        return (claimed["id"], claimed["lease_epoch"], False)

    def _resolve_loop_continuation(
        self,
    ) -> Optional[Tuple[str, int, bool]]:
        """G1 (F1): pick a continuation/recovery target for the background loop.

        After ``claim_next_job`` returns no QUEUED work, scan the nonterminal
        RUNNING jobs: continue our own still-leased job (with no future
        eligibility block), or evidence-bound takeover of an expired-lease
        RUNNING job.  Exactly one target is stepped per pass; a job we cannot
        safely touch is skipped (never a blind claim / duplicate spawn).
        """
        now_iso = self._supervisor._now_iso()
        try:
            rows = self._supervisor.core._store.list_supervisor_jobs(
                nonterminal_only=True)
        except Exception:  # noqa: BLE001 - store read error -> no work this pass
            return None
        for row in rows:
            jid = row["id"]
            if row.get("primary_state") != job_state.PrimaryState.RUNNING.value:
                continue
            if row.get("owner_instance_id") == self.owner_instance_id and \
                    self._supervisor.store.lease_is_current(
                        jid, self.owner_instance_id, row["lease_epoch"]):
                eligible = row.get("next_eligible_at")
                if eligible is None or eligible <= now_iso:
                    return (jid, row["lease_epoch"], True)
                continue
            expires = row.get("lease_expires_at")
            if expires is not None and expires <= now_iso:
                taken = self._try_recover_takeover(jid, row)
                if taken is not None:
                    return (jid, taken["lease_epoch"], False)
        return None

    def _worktree_recovery_verdict(self, job_id: str) -> Optional[str]:
        """Worktree recovery verdict for a RUNNING job (F1/F3).

        Returns ``None`` when the job has no worktree binding (nothing to
        protect — the takeover proceeds without a worktree check), otherwise one
        of the :mod:`argent_core.worktree` recovery verdicts computed from REAL
        git facts (repo identity / HEAD / dirty) against the persisted binding.
        """
        ev = self.worktree_evidence(job_id)
        if ev.get("writer_binding_mode") != "BOUND":
            return None
        # A BOUND job must carry complete real provenance, else fail-closed.
        if not ev.get("canonical_worktree_path") or ev.get("repo_identity") is None:
            return V_AMBIGUOUS_WRITER
        binding = WorktreeBinding(
            job_id=job_id,
            canonical_worktree_path=ev.get("canonical_worktree_path") or "",
            repo_identity=ev.get("repo_identity"),
            base_commit=ev.get("base_commit"),
            branch_identity=ev.get("branch_identity"),
            writer_dispatch_id=ev.get("writer_dispatch_id"),
            writer_owner_instance_id=ev.get("writer_owner_instance_id"),
            writer_lease_epoch=ev.get("writer_lease_epoch") or 0,
            expected_head=ev.get("expected_head"),
            current_head=ev.get("current_head"),
        )
        provider = self._supervisor._git_provenance_provider \
            or GitProvenanceProvider()
        path = ev.get("canonical_worktree_path")
        evidence = WorktreeEvidence(
            repo_identity=provider.repo_identity(path),
            head=provider.head(path),
            dirty=provider.dirty(path),
        )
        return classify_worktree_recovery(
            binding, evidence, writer_terminal=True,
        ).verdict

    def _try_recover_takeover(self, job_id: str, row: dict) -> Optional[dict]:
        """Attempt the evidence-bound RUNNING takeover (F1).

        Process evidence decides first: a live registered process refuses the
        takeover (returns None — the holder keeps the job); an unreadable
        identity fail-closes to LOST quarantine.  A provably terminal process
        then goes through :meth:`argent_core.store.Store.recover_takeover_job`
        with the real worktree verdict.  Returns the taken-over row, or None.
        """
        verdict = self._process_identity_verdict(job_id)
        if verdict == "alive":
            return None
        if verdict == "unknown":
            self._supervisor.store.quarantine_lost(
                job_id, error_code="AMBIGUOUS_WRITER", expected=row,
            )
            return None
        worktree_verdict = self._worktree_recovery_verdict(job_id)
        try:
            taken = self._supervisor.store.recover_takeover_job(
                job_id,
                expected=row,
                owner_instance_id=self.owner_instance_id,
                ttl_seconds=self._lease_ttl_seconds,
                process_alive=False,
                worktree_verdict=worktree_verdict,
            )
        except LeaseError:
            return None
        # A worktree refusal transitions the job to BLOCKED/LOST (not RUNNING);
        # only a real RUNNING takeover under our owner is a claimable target.
        if taken.get("primary_state") != job_state.PrimaryState.RUNNING.value \
                or taken.get("owner_instance_id") != self.owner_instance_id:
            return None
        return taken

    def _should_renew(self, job: dict) -> bool:
        """Renew iff the job is still RUNNING and we still hold the lease."""
        return (
            job["primary_state"] == job_state.PrimaryState.RUNNING.value
            and self._supervisor.store.lease_is_current(
                job["id"], self.owner_instance_id, job["lease_epoch"],
            )
        )

    # -- C1 resource preflight (decision basis only; no enforcement) --------

    def _store_active_jobs_reader(self):
        """Build the default active-jobs reader from the trusted job store (F1).

        Reads every non-terminal RUNNING job (with its persisted
        ``resource_class`` — trusted store data, never agent output) and returns
        ``[(job_id, resource_class), ...]``.  The job currently being preflighted
        (``self._preflight_job_id``) is excluded so a job never blocks itself.
        Any store read/parse error returns ``None`` (UNKNOWN, fail-closed) —
        never an empty list for an unreadable store.
        """
        store = self._supervisor.core._store

        def reader():
            try:
                rows = store.list_supervisor_jobs()
            except Exception:
                return None
            try:
                exclude = self._preflight_job_id
                return [
                    (row["id"], row.get("resource_class") or ResourceClass.LIGHT.value)
                    for row in rows
                    if row.get("terminal") is None
                    and row.get("primary_state") == job_state.PrimaryState.RUNNING.value
                    and row["id"] != exclude
                ]
            except Exception:
                return None

        return reader

    def _resource_preflight(self, job_id: str):
        """Run the C1 admission preflight for a job.

        Captures a bounded host snapshot (injectable provider) and asks the
        (injectable) governor.  Returns an :class:`AdmissionDecision`.
        The job's ``resource_class`` comes from the trusted persisted job row —
        never from agent output.
        """
        self._preflight_job_id = job_id
        try:
            row = self._supervisor.store._job_row(job_id)
            rc = (row or {}).get("resource_class") or ResourceClass.LIGHT.value
            snapshot = self._snapshot_provider.capture(
                self._supervisor._workspace_root
            )
            return self._governor.decide(
                resource_class=rc,
                snapshot=snapshot,
                now_iso=self._supervisor._now_iso(),
            )
        finally:
            self._preflight_job_id = None

    def _resource_gate(self, job_id: str, epoch: int, admission):
        """Apply a non-ALLOW admission verdict; returns a result or None.

        DEFER/DENY_LOCAL requeue the job (no spawn) and return the terminal
        pass result; PREFER_EXTERNAL/ALLOW persist nothing blocking and return
        ``None`` so the caller continues (PREFER_EXTERNAL is a hint only).
        """
        if admission.decision == AdmissionVerdict.DEFER.value:
            self._defer_resource_job(job_id, epoch, admission)
            self._supervisor.clear_lease_owner()
            return SchedulerPassResult(
                OUTCOME_RESOURCE_DEFERRED, job_id=job_id,
                detail=admission.reason_code,
            )
        if admission.decision == AdmissionVerdict.DENY_LOCAL.value:
            self._deny_resource_job(job_id, epoch, admission)
            self._supervisor.clear_lease_owner()
            return SchedulerPassResult(
                OUTCOME_RESOURCE_DENIED, job_id=job_id,
                detail=admission.reason_code,
            )
        if admission.decision == AdmissionVerdict.PREFER_EXTERNAL.value:
            # C1 never triggers external CI; persist the routing hint only and
            # continue as ALLOW (local admission proceeds unchanged).
            self._persist_resource_hint(job_id, epoch, admission)
        return None

    def _defer_resource_job(self, job_id: str, epoch: int, decision) -> None:
        """DEFER: requeue as QUEUED (no spawn) with a bounded retry horizon."""
        self._supervisor.store.enqueue_job(
            job_id,
            queue_reason=job_state.QueueReason.RESOURCE_DEFERRED.value,
            next_eligible_at=decision.next_eligible_at,
            error_class=job_state.ErrorClass.RESOURCE.value,
            error_code=decision.reason_code,
            owner_instance_id=self.owner_instance_id,
            lease_epoch=epoch,
            last_resource_decision=decision.decision,
            last_resource_reason_code=decision.reason_code,
            last_resource_snapshot_hash=decision.snapshot_ref,
            last_resource_at=decision.timestamp,
        )

    def _enforcement_failed_job(self, job_id: str, epoch: int, reason_code) -> None:
        """C2: requeue a scoped-spawn enforcement failure as QUEUED (RESOURCE).

        Mirrors :meth:`_defer_resource_job`: the job stays QUEUED with
        ``error_class=RESOURCE`` and a bounded ``next_eligible_at`` (no new
        primary state, no CODE_FAILURE, no rework).  The holder-CAS enqueue
        clears the lease; a stale/foreign holder is refused (LeaseError) and the
        job is left for its current holder.
        """
        policy = self._governor.policy
        now_iso = self._supervisor._now_iso()
        try:
            dt = datetime.fromisoformat(now_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            next_eligible_at = (
                dt + timedelta(seconds=policy.defer_retry_seconds)
            ).isoformat()
        except (ValueError, TypeError):
            next_eligible_at = None
        self._supervisor.store.enqueue_job(
            job_id,
            queue_reason=job_state.QueueReason.RESOURCE_DEFERRED.value,
            next_eligible_at=next_eligible_at,
            error_class=job_state.ErrorClass.RESOURCE.value,
            error_code=reason_code,
            owner_instance_id=self.owner_instance_id,
            lease_epoch=epoch,
            last_resource_decision=AdmissionVerdict.DEFER.value,
            last_resource_reason_code=reason_code,
        )

    def _enforcement_lost_job(self, job_id: str, epoch: int, reason_code) -> None:
        """C2/F2: quarantine an unprovable-cleanup enforcement failure as LOST.

        A scope whose cleanup could not be proven inactive means a process may
        still be running — the job must NEVER be silently re-admitted (no
        DEFER-requeue).  It is quarantined via the existing
        ``quarantine_lost`` path (fail-closed, no respawn, no takeover).
        """
        self._supervisor.store.quarantine_lost(
            job_id,
            error_code=reason_code or "SCOPE_CLEANUP_UNVERIFIED",
            expected=None,
        )

    def _context_failed_job(self, job_id: str, epoch: int, reason_code) -> None:
        """D1/F6: bounded re-queue for a TRANSIENT Context-Pack failure.

        Only provably transient context errors (e.g. a persist/artifact-write
        I/O error) may be re-queued.  Mirrors :meth:`_enforcement_failed_job`:
        the job stays QUEUED with ``error_class=CONTEXT`` (an ORCHESTRATION
        error — never CODE_FAILURE, never RESOURCE) and a bounded
        ``next_eligible_at``.  No new primary state, no rework, no spawn.
        Permanent context codes must go through :meth:`_context_blocked_job`.
        """
        policy = self._governor.policy if self._governor is not None else None
        defer_seconds = getattr(policy, "defer_retry_seconds", 300) \
            if policy is not None else 300
        now_iso = self._supervisor._now_iso()
        try:
            dt = datetime.fromisoformat(now_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            next_eligible_at = (
                dt + timedelta(seconds=defer_seconds)
            ).isoformat()
        except (ValueError, TypeError):
            next_eligible_at = None
        self._supervisor.store.enqueue_job(
            job_id,
            queue_reason=job_state.QueueReason.RETRY_BACKOFF.value,
            next_eligible_at=next_eligible_at,
            error_class=job_state.ErrorClass.CONTEXT.value,
            error_code=reason_code or "CONTEXT_BUDGET_EXCEEDED",
            owner_instance_id=self.owner_instance_id,
            lease_epoch=epoch,
        )

    def _context_blocked_job(self, job_id: str, epoch: int, reason_code) -> None:
        """D1/F6: fail-closed a PERMANENT Context-Pack error to BLOCKED.

        A deterministic Context failure (budget exceeded, invalid/foreign/stale
        pack) can never be fixed by a bounded retry.  Route it through the
        existing ``quarantine_blocked`` semantics (``primary_state=BLOCKED``,
        no retry, no spawn); an owner/policy requeue is the only way back.  This
        remains an ORCHESTRATION failure — never CODE_FAILURE, never RESOURCE,
        never a model failure.
        """
        self._supervisor.store.quarantine_blocked(
            job_id,
            error_code=reason_code or "CONTEXT_BUDGET_EXCEEDED",
            error_class=job_state.ErrorClass.CONTEXT.value,
        )

    def _deny_resource_job(self, job_id: str, epoch: int, decision) -> None:
        """DENY_LOCAL: requeue as QUEUED (no spawn) with a far-future horizon."""
        far = self._supervisor._now_iso()
        try:
            dt = datetime.fromisoformat(far)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            far = (dt + timedelta(seconds=DENY_LOCAL_RETRY_SECONDS)).isoformat()
        except (ValueError, TypeError):
            far = None
        self._supervisor.store.enqueue_job(
            job_id,
            queue_reason=job_state.QueueReason.RESOURCE_DENIED.value,
            next_eligible_at=far,
            error_class=job_state.ErrorClass.RESOURCE.value,
            error_code=ResourceReasonCode.LOCAL_CAPACITY_INSUFFICIENT.value,
            owner_instance_id=self.owner_instance_id,
            lease_epoch=epoch,
            last_resource_decision=decision.decision,
            last_resource_reason_code=decision.reason_code,
            last_resource_snapshot_hash=decision.snapshot_ref,
            last_resource_at=decision.timestamp,
        )

    def _persist_resource_hint(self, job_id: str, epoch: int, decision) -> None:
        """PREFER_EXTERNAL: persist the routing hint only (best-effort audit).

        Uses the transactional holder-CAS store operation (F5) — a stale/foreign
        holder cannot overwrite another owner's audit columns.  On a fence loss
        the hint is simply dropped (the local ALLOW path continues unchanged).
        """
        try:
            self._supervisor.store.persist_resource_decision(
                job_id,
                owner_instance_id=self.owner_instance_id,
                lease_epoch=epoch,
                last_resource_decision=decision.decision,
                last_resource_reason_code=decision.reason_code,
                last_resource_snapshot_hash=decision.snapshot_ref,
                last_resource_at=decision.timestamp,
            )
        except (LeaseError, LeaseFencedError):
            pass

    def run_pass(self, job_id: Optional[str] = None) -> SchedulerPassResult:
        """Run one bounded scheduler pass (exactly one safe step, then return).

        Pass outline (Design-Vorgabe 1): (a) read persisted facts,
        (b) determine a claimable job (B1 predicates), (c) atomic claim,
        (d) exactly one safe step (reconcile→perform), (e) result persisted
        (fenced), (f) renew (still RUNNING) or clear (left RUNNING) the lease,
        (g) the pass ends.  Never loops to terminal.
        """
        target = self._resolve_target(job_id)
        if target is None:
            return SchedulerPassResult(OUTCOME_NO_WORK, job_id=job_id)

        target_job_id, epoch, held = target
        self._supervisor.set_lease_owner(self.owner_instance_id, epoch)

        # C1: resource preflight runs on every NEW claim (``held=False``), after
        # the atomic claim and BEFORE reconcile/spawn — the earliest trusted
        # admission filter (avoids unnecessary dispatches).  A continuation
        # (``held=True``) is re-checked at the spawn gate below.
        if not held:
            gate = self._resource_gate(
                target_job_id, epoch, self._resource_preflight(target_job_id)
            )
            if gate is not None:
                return gate

        # C3/F1: a RUNNING job (continuation OR takeover) whose process has
        # authoritative terminal registry evidence (detached agent finished /
        # failed) is classified + recovered exactly once BEFORE any further
        # reconcile/spawn — no duplicate spawn, no second agent.  A non-resource
        # class or a live process returns None and continues normally.
        recovered = self.classify_and_recover(target_job_id, epoch)
        if recovered is not None:
            self._supervisor.clear_lease_owner()
            return recovered

        try:
            decision = self._supervisor.reconcile(target_job_id)
        except LeaseFencedError as exc:
            self._supervisor.clear_lease_owner()
            return SchedulerPassResult(
                OUTCOME_FENCED, job_id=target_job_id, detail=str(exc),
            )
        except NotFound:
            self._supervisor.clear_lease_owner()
            return SchedulerPassResult(
                OUTCOME_NO_WORK, job_id=target_job_id, detail="job_missing",
            )

        # C1/F3: spawn-adjacent preflight is the BINDING admission point.  A
        # fresh preflight runs immediately before the resource-relevant actions
        # in this path (SPAWN_RUN and RUN_SANDBOX_TESTS), regardless of
        # ``held``, so a later pass cannot spawn under a since-changed
        # memory/swap/disk state.  DEFER/DENY_LOCAL requeue without any
        # launcher call; PREFER_EXTERNAL is a hint only.
        if decision.action in (ReconcileAction.SPAWN_RUN,
                               ReconcileAction.RUN_SANDBOX_TESTS):
            # G1 (F6): a shutdown requested mid-pass aborts BEFORE any spawn or
            # test launch — the launcher/enforcer is never invoked and the job
            # stays RUNNING under a valid lease (consistent, re-claimable).
            if self._stop_requested():
                self._supervisor.clear_lease_owner()
                return SchedulerPassResult(
                    OUTCOME_NO_WORK, job_id=target_job_id, detail="stop_requested",
                )
            gate = self._resource_gate(
                target_job_id, epoch, self._resource_preflight(target_job_id)
            )
            if gate is not None:
                return gate

        try:
            action_outcome = self._supervisor.perform_next_safe_action_if_required(
                decision
            )
        except LeaseFencedError as exc:
            self._supervisor.clear_lease_owner()
            return SchedulerPassResult(
                OUTCOME_FENCED, job_id=target_job_id, decision=decision,
                detail=str(exc),
            )

        # C2: a scoped-spawn enforcement failure (no process started) must
        # requeue the job as QUEUED with error_class=RESOURCE + a bounded
        # ``next_eligible_at`` (the existing DEFER path — no new primary state,
        # no CODE_FAILURE, no rework).
        if action_outcome.status == "resource_enforcement_failed":
            self._enforcement_failed_job(target_job_id, epoch, action_outcome.detail)
            return SchedulerPassResult(
                OUTCOME_RESOURCE_DEFERRED, job_id=target_job_id,
                detail=action_outcome.detail,
            )

        # F2: a cleanup that could NOT be proven inactive must quarantine the
        # job as LOST (never a DEFER-requeue — a possibly-running process must
        # never be silently re-admitted).
        if action_outcome.status == "resource_enforcement_lost":
            self._enforcement_lost_job(target_job_id, epoch, action_outcome.detail)
            return SchedulerPassResult(
                OUTCOME_RESOURCE_LOST, job_id=target_job_id,
                detail=action_outcome.detail,
            )

        # D1: a Context-Pack build failure (CONTEXT_BUDGET_EXCEEDED / invalid)
        # must fail-closed — an ORCHESTRATION error, never CODE_FAILURE, never
        # RESOURCE, never a spawn.  F6: a permanent context code routes to
        # BLOCKED (quarantine, no retry); only a provably transient context
        # error is bounded re-queued as QUEUED with error_class=CONTEXT.
        if action_outcome.status == "context_build_failed":
            code = action_outcome.detail or "CONTEXT_BUDGET_EXCEEDED"
            if is_permanent_context_code(code):
                self._context_blocked_job(target_job_id, epoch, code)
            else:
                self._context_failed_job(target_job_id, epoch, code)
            return SchedulerPassResult(
                OUTCOME_CONTEXT_FAILED, job_id=target_job_id,
                detail=code,
            )

        # C3: a synchronous sandbox termination with resource evidence routes
        # through the central fenced recovery commit (classification happens in
        # the scheduler, exactly-once under the holder lease).
        if action_outcome.status == "resource_termination_failed":
            recovered = self.classify_and_recover(target_job_id, epoch)
            self._supervisor.clear_lease_owner()
            return recovered or SchedulerPassResult(
                OUTCOME_STEPPED, job_id=target_job_id, decision=decision,
                action_outcome=action_outcome, detail="recovery_noop",
            )

        row = self._supervisor.store._job_row(target_job_id)
        if row is None:
            self._supervisor.clear_lease_owner()
            return SchedulerPassResult(
                OUTCOME_STEPPED, job_id=target_job_id, decision=decision,
                action_outcome=action_outcome, detail="job_gone",
            )

        if row["terminal"] is not None:
            self._supervisor.clear_lease_owner()
            return SchedulerPassResult(
                OUTCOME_STEPPED, job_id=target_job_id, decision=decision,
                action_outcome=action_outcome, lease_epoch=row["lease_epoch"],
                detail=f"terminal:{row['terminal']}",
            )

        if self._should_renew(row):
            renewed = self._supervisor.store.renew_lease(
                target_job_id,
                owner_instance_id=self.owner_instance_id,
                lease_epoch=row["lease_epoch"],
                ttl_seconds=self._renew_ttl_seconds,
            )
            self._supervisor.clear_lease_owner()
            return SchedulerPassResult(
                OUTCOME_RENEWED, job_id=target_job_id, decision=decision,
                action_outcome=action_outcome, lease_epoch=renewed["lease_epoch"],
                detail=f"held={held}",
            )

        # The job left RUNNING (backoff/requeue/error/wait) while still holding
        # our lease -> clear it so the job can be re-admitted (no stale
        # "foreign lease" blocker).  A terminal job is handled above.
        cleared = False
        if row["owner_instance_id"] == self.owner_instance_id and \
                row["lease_expires_at"] is not None:
            try:
                self._supervisor.store.clear_lease(
                    target_job_id,
                    owner_instance_id=self.owner_instance_id,
                    lease_epoch=row["lease_epoch"],
                )
                cleared = True
            except LeaseError:
                cleared = False
        self._supervisor.clear_lease_owner()
        return SchedulerPassResult(
            OUTCOME_RELEASED if cleared else OUTCOME_STEPPED,
            job_id=target_job_id, decision=decision,
            action_outcome=action_outcome, lease_epoch=row["lease_epoch"],
            detail=f"primary_state={row['primary_state']}",
        )

    # ----------------------------------------------------- restart reconcile

    def reconcile_after_restart(self) -> RestartReconcileSummary:
        """Deterministic reconciliation after a Supervisor/process restart.

        Only persisted facts are authoritative (the DB is reopened / a fresh
        Supervisor instance is used; there is no in-memory cache).  For every
        nonterminal job the D-rules are applied:

        * terminal        → never touched (sticky).
        * QUEUED          → left claimable (newly admission-eligible).
        * OWNER_GATE      → left in place (gate ledger is idempotent).
        * WAITING_EXTERNAL→ left in place (handled in Phase B3).
        * RUNNING, ``lease_expires_at IS NULL`` (legacy/inconsistent) →
          fail-closed ``LOST`` quarantine (no takeover, no respawn, no second
          writer).
        * RUNNING, valid lease, held by this instance  → ``rebound`` (the lease
          context is re-established per-pass in ``run_pass``).
        * RUNNING, valid lease, held by another owner → left alone (belongs to
          the holder; no takeover while the lease is valid).
        * RUNNING, expired concrete lease → decided by live process evidence:
          alive → ``process_alive`` (no claim); provably terminal → worktree
          evidence decides ``takeover_candidate`` (clean / no binding),
          ``blocked_worktree`` (divergent) or ``quarantined_lost``
          (foreign/ambiguous); unreadable → ``quarantined_lost``.

        Idempotent: running it twice is a no-op.
        """
        rows = self._supervisor.core._store.list_supervisor_jobs()
        now_iso = self._supervisor._now_iso()
        details: list = []
        scanned = rebound = quarantined = takeover = foreign = left = 0
        process_alive = 0
        resource_recovered = 0
        blocked_worktree = 0

        for row in rows:
            scanned += 1
            jid = row["id"]
            terminal = row.get("terminal")
            if terminal is not None:
                details.append((jid, "skip_terminal", terminal))
                continue
            ps = row.get("primary_state")
            if ps != job_state.PrimaryState.RUNNING.value:
                details.append((jid, "left", ps))
                left += 1
                continue
            kind, detail = self._classify_running(row, now_iso)
            if kind == "rebound":
                rebound += 1
            elif kind == "foreign_lease_kept":
                foreign += 1
            elif kind == "takeover_candidate":
                takeover += 1
            elif kind == "blocked_worktree":
                blocked_worktree += 1
            elif kind == "quarantined_lost":
                quarantined += 1
            elif kind == "process_alive":
                process_alive += 1
            elif kind == "resource_recovered":
                resource_recovered += 1
            elif kind == "skip_terminal":
                pass  # a terminal appeared concurrently; counted in scanned only
            else:  # "left" (re-classified to a non-RUNNING state)
                left += 1
            details.append((jid, kind, detail))

        return RestartReconcileSummary(
            scanned=scanned, rebound=rebound, quarantined_lost=quarantined,
            takeover_candidates=takeover, blocked_worktree=blocked_worktree,
            foreign_lease_kept=foreign,
            process_alive=process_alive, resource_recovered=resource_recovered,
            left=left, details=tuple(details),
        )

    def _classify_running(self, row: dict, now_iso: str) -> tuple:
        """Classify a RUNNING nonterminal row under the D-rules (F3/F4).

        Returns ``(kind, detail)`` where kind is one of ``rebound``,
        ``foreign_lease_kept``, ``takeover_candidate``, ``blocked_worktree``,
        ``quarantined_lost``, ``left`` or ``skip_terminal``.

        F4: a RUNNING lease tuple is only valid when the owner is non-empty,
        the epoch is plausible (>= 1) AND the expiry is concrete.  An
        incomplete tuple is quarantined as LOST (never a foreign lease or a
        takeover candidate).

        F3: the quarantine is CAS-fenced against the scanned snapshot.  On
        CAS loss the row is re-read and re-classified (bounded) so a newer
        state (e.g. a concurrent OWNER_GATE/WAITING_GATE transition) is never
        blindly overwritten.
        """
        current = row
        for _ in range(3):  # bounded re-classification attempts
            jid = current["id"]
            # C3/F1: a RUNNING job whose process has authoritative terminal
            # registry evidence is a post-terminal resource-recovery point
            # (detached agent).  Classify + recover exactly once under the
            # holder lease; a fenced/foreign holder or a non-resource class
            # falls through to the normal D-rule classification below.
            reg = self._bound_terminal_evidence(jid)
            if reg is not None:
                recovered = self.classify_and_recover(
                    jid, current.get("lease_epoch") or 0,
                )
                if recovered is not None:
                    return ("resource_recovered", recovered.detail)
            owner = current.get("owner_instance_id")
            epoch = current.get("lease_epoch")
            expires = current.get("lease_expires_at")
            valid = (
                owner not in (None, "")
                and isinstance(epoch, int) and epoch >= 1
                and expires is not None
            )
            if not valid:
                detail = ("incomplete_lease" if expires is not None
                          else "running_no_lease")
                result = self._supervisor.store.quarantine_lost(
                    jid, error_code="AMBIGUOUS_WRITER", expected=current,
                )
                if result is not None:
                    return ("quarantined_lost", detail)
                # F3: CAS lost — the job changed since the scan.  Re-read and
                # re-classify (the newer state is authoritative).
                current = self._supervisor.store._job_row(jid)
                if current is None:
                    return ("left", "gone")
                if current.get("terminal") is not None:
                    return ("skip_terminal", current.get("terminal"))
                if current.get("primary_state") != job_state.PrimaryState.RUNNING.value:
                    return ("left", current.get("primary_state"))
                continue
            if expires > now_iso:
                if owner == self.owner_instance_id:
                    return ("rebound", f"epoch={epoch}")
                return ("foreign_lease_kept", owner)
            # B3/F2: an expired concrete lease is no longer sufficient evidence
            # to authorise a takeover.  Live process evidence decides:
            #   * persisted identity == live identity -> process still alive,
            #     keep the holder (no takeover, no second writer);
            #   * boot change / PID reuse / authoritatively terminal -> old
            #     process surely gone -> takeover candidate (with evidence);
            #   * unknown/unreadable evidence -> fail-closed LOST quarantine
            #     (no takeover without proof).
            detail = f"epoch={epoch}"
            verdict = self._process_identity_verdict(jid)
            if verdict == "alive":
                return ("process_alive", detail + ":live_process")
            if verdict == "terminal":
                # B4 (F1/F3): a provably terminal process is only a takeover
                # pre-decision once the REAL worktree evidence is consistent.
                # Divergent -> BLOCKED; foreign/ambiguous -> LOST; clean /
                # no binding -> takeover_eligible (``takeover_candidate``).
                wv = self._worktree_recovery_verdict(jid)
                if wv == V_BLOCKED_DIVERGED:
                    result = self._supervisor.store.quarantine_blocked(
                        jid, error_code="WORKTREE_DIVERGED", expected=current,
                    )
                    if result is not None:
                        return ("blocked_worktree", detail + ":worktree_diverged")
                elif wv not in (V_CLEANUP_PENDING, None):
                    result = self._supervisor.store.quarantine_lost(
                        jid, error_code="AMBIGUOUS_WRITER", expected=current,
                    )
                    if result is not None:
                        return ("quarantined_lost",
                                detail + ":worktree_ambiguous")
                else:
                    return ("takeover_candidate", detail + ":process_terminal")
                # CAS lost on a quarantine -> re-read and re-classify (bounded).
                current = self._supervisor.store._job_row(jid)
                if current is None:
                    return ("left", "gone")
                if current.get("terminal") is not None:
                    return ("skip_terminal", current.get("terminal"))
                if current.get("primary_state") != job_state.PrimaryState.RUNNING.value:
                    return ("left", current.get("primary_state"))
                continue
            result = self._supervisor.store.quarantine_lost(
                jid, error_code="AMBIGUOUS_WRITER", expected=current,
            )
            if result is not None:
                return ("quarantined_lost", detail + ":unknown_process")
            # CAS lost — re-read and re-classify (bounded).
            current = self._supervisor.store._job_row(jid)
            if current is None:
                return ("left", "gone")
            if current.get("terminal") is not None:
                return ("skip_terminal", current.get("terminal"))
            if current.get("primary_state") != job_state.PrimaryState.RUNNING.value:
                return ("left", current.get("primary_state"))
            continue
        # Exhausted the re-classification budget: leave as-is (never blind LOST).
        return ("left", current.get("primary_state"))

    # -- B3 process/worktree evidence (read-only, authority order preserved) -

    def process_evidence(self, job_id: str):
        """Latest process-registry evidence for a job (None if unregistered)."""
        rows = self._supervisor.core._store.list_process_registrations(job_id)
        return rows[-1] if rows else None

    def _process_identity_verdict(self, job_id: str) -> str:
        """Live process-identity verdict for a RUNNING job (F2).

        Returns ``alive`` (persisted identity == live identity), ``terminal``
        (boot change / PID reuse / authoritatively terminal registration), or
        ``unknown`` (no registration or unreadable live identity).
        """
        reg = self.process_evidence(job_id)
        if reg is None:
            return "unknown"
        if ProcessRegistry.is_terminally_dead(reg):
            return "terminal"
        pid = reg.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return "unknown"
        provider = self._supervisor._process_identity_provider \
            or ProcessIdentityProvider()
        try:
            live = provider.current(pid)
        except Exception:
            return "unknown"
        if not live.is_known:
            return "unknown"
        verdict = ProcessRegistry.classify_identity(reg, live)
        return "alive" if verdict == IDENTITY_SAME else "terminal"

    def worktree_evidence(self, job_id: str) -> dict:
        """Read-only minimal worktree/writer ownership evidence for a job."""
        row = self._supervisor.core._store.get_supervisor_job(job_id)
        if row is None:
            return {}
        keys = (
            "canonical_worktree_path", "repo_identity", "base_commit",
            "branch_identity", "writer_dispatch_id", "writer_owner_instance_id",
            "writer_lease_epoch", "expected_head", "current_head",
            "writer_binding_mode",
        )
        return {k: row.get(k) for k in keys}

    # -- C3 resource-failure classification + fenced recovery commit --------

    @staticmethod
    def _parse_scope_events(scope_events) -> Optional[dict]:
        """Parse the bounded ``scope_events`` JSON (fail-closed to None)."""
        if not scope_events:
            return None
        try:
            import json
            value = json.loads(scope_events)
        except (ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def _bound_terminal_evidence(self, job_id: str) -> Optional[dict]:
        """Return the terminal registry row BOUND to the job's frontier.

        C3/F3: persisted OOM_KILL/TIMEOUT facts are only authoritative when they
        belong to the CONCRETE process bound to (a) the current frontier
        dispatch, (b) the current boot identity and (c/d) the row's own
        process_id + scope_ref/cgroup binding.  A different dispatch, a
        stale/fremde boot_id or a missing frontier binding is historical
        evidence -> ``None`` (no classification).  This is a TARGETED query for
        the frontier's terminal evidence — never "the newest arbitrary row".
        """
        row = self._supervisor.store._job_row(job_id)
        if row is None:
            return None
        if row.get("primary_state") != job_state.PrimaryState.RUNNING.value:
            return None
        if row.get("terminal") is not None:
            return None
        expected_dispatch = row.get("expected_dispatch_id")
        provider = self._supervisor._process_identity_provider \
            or ProcessIdentityProvider()
        try:
            current_boot = provider.boot_id()
        except Exception:
            current_boot = None
        regs = self._supervisor.core._store.list_process_registrations(job_id)
        for reg in reversed(regs):  # newest first
            if not ProcessRegistry.is_terminally_dead(reg):
                continue
            # (b) bind to the current frontier dispatch.
            if expected_dispatch is not None:
                if reg.get("dispatch_id") != expected_dispatch:
                    continue
            elif reg.get("dispatch_id") is not None:
                # No frontier dispatch persisted yet -> no binding -> fail closed.
                continue
            # (c) boot_id consistency: a stale/fremde boot is historical only.
            if current_boot is not None and reg.get("boot_id") != current_boot:
                continue
            # (a/d) the row IS the concrete bound process (process_id + scope_ref
            # + cgroup_ref + evidence live in this single row).
            return reg
        return None

    def classify_and_recover(
        self, job_id: str, epoch: int,
    ) -> Optional[SchedulerPassResult]:
        """C3: classify terminal process evidence and commit a fenced recovery.

        The central classification point.  Reads the terminal process-registry
        evidence BOUND to the job's current frontier (trusted store data — never
        agent output), derives the C3 :class:`FailureClass` from the persisted
        ``termination_class`` / ``scope_events`` / ``timed_out`` / ``exit_code``,
        decides a bounded :class:`RecoveryDecision`, and commits it exactly-once
        under the current holder lease (stale/foreign holders write NOTHING;
        the same process_id is recovered at most once via the consumed marker).

        Returns a :class:`SchedulerPassResult` when a resource decision was
        committed, or ``None`` when there is no authoritative terminal resource
        evidence (the job continues through the normal workflow — NORMAL_EXIT /
        code failure are NOT resource recoveries).
        """
        row = self._supervisor.store._job_row(job_id)
        if row is None:
            return None
        if row.get("primary_state") != job_state.PrimaryState.RUNNING.value:
            return None
        if row.get("terminal") is not None:
            return None
        reg = self._bound_terminal_evidence(job_id)
        if reg is None:
            return None
        # C3/F5: exactly-once — an already-consumed process_id is a no-op.
        process_id = reg.get("process_id")
        if process_id and self._supervisor.store.has_recovery_marker(job_id, process_id):
            return None

        scope_events = self._parse_scope_events(reg.get("scope_events"))
        failure_class = classify_failure(
            termination_class=reg.get("termination_class"),
            exit_code=reg.get("exit_code"),
            timed_out=bool(reg.get("timed_out")),
            scope_events=scope_events,
            policy=self._recovery_policy,
        )
        if not is_resource_failure(failure_class):
            return None  # NORMAL_EXIT / code failure -> normal workflow

        attempt_no = row.get("attempt_no") or 0
        decision = decide_recovery(
            failure_class, attempt_no=attempt_no, policy=self._recovery_policy,
        )
        reason = reason_code_for_failure(failure_class)
        backoff = (
            self._recovery_policy.retry_backoff_seconds
            if decision is RecoveryDecision.RETRY_BOUNDED
            else self._recovery_policy.defer_backoff_seconds
        )
        next_eligible_at = next_eligible_at_after(
            self._supervisor._now_iso(), backoff,
        )
        try:
            self._supervisor.store.commit_recovery_decision(
                job_id,
                owner_instance_id=self.owner_instance_id,
                lease_epoch=epoch,
                failure_class=failure_class,
                recovery_decision=decision,
                reason_code=reason,
                next_eligible_at=next_eligible_at,
                process_id=process_id,
            )
        except (LeaseError, LeaseFencedError):
            # Fenced/foreign holder or already-consumed — write nothing, leave
            # the job for its current holder (exactly-once under the fence).
            return None
        return SchedulerPassResult(
            OUTCOME_RESOURCE_RECOVERED, job_id=job_id, detail=decision.value,
        )
