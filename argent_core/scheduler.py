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
from typing import Optional, Tuple

from . import job_state
from .models import LeaseError, LeaseFencedError, NotFound
from .process_registry import (
    IDENTITY_SAME,
    ProcessIdentityProvider,
    ProcessRegistry,
)
from .store import MAX_LEASE_TTL_SECONDS
from .supervisor import ActionOutcome, ReconcileDecision, Supervisor
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
            if claimed is None:
                return None
            return (claimed["id"], claimed["lease_epoch"], False)

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
            elif kind == "skip_terminal":
                pass  # a terminal appeared concurrently; counted in scanned only
            else:  # "left" (re-classified to a non-RUNNING state)
                left += 1
            details.append((jid, kind, detail))

        return RestartReconcileSummary(
            scanned=scanned, rebound=rebound, quarantined_lost=quarantined,
            takeover_candidates=takeover, blocked_worktree=blocked_worktree,
            foreign_lease_kept=foreign,
            process_alive=process_alive, left=left, details=tuple(details),
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
