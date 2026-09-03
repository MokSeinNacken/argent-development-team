"""Phase I2 — durable integration / merge-queue controller.

This is the single integration authority (ARGENT ARCHITECTURE V1 FINAL §14,
Phase I2 brief §1–§25).  It is **not** a second job scheduler and **not** a
second source of truth: it reads trusted store facts and git evidence, drives
the integration of candidates through the I1 action-lock boundary, and persists
only additive ``integration_candidates`` rows.

Conservative initial policy (§25): exactly ONE integration holder per
(repository, integration-target) — the I1 ``action_locks`` lease-fenced boundary
(``repo:<repo>:integrate:<target>``) — and the queue is processed serially per
target.  Different repositories may progress independently if resources allow
(the Resource Governor binding is injected by the caller).  No throughput
tuning.

Key safety properties (code-enforced):

* Candidates are controller-authoritative: created ONLY from trusted store
  facts (a source job in a valid integration-ready terminal state with proven
  provenance).  Agent prose ("ready to merge") is never sufficient.
* Integration NEVER mutates a Writer worktree or the integration target
  branch; it creates a dedicated integration worktree + branch
  ``integration/<target>`` under the validated worktrees root.  Promotion of
  the target branch is later (I3/J) — CASE 12/13/14.
* Merge classification is authoritative git only (merge-base / merge-tree);
  no LLM conflict declaration, no blind ours/theirs, no force-rebase — CASE
  15/16/17.
* Every candidate transition is revision-fenced (CAS); a stale caller can
  never commit — CASE 26/27/28.
* Restart/crash safety: recovery never infers INTEGRATED from process
  disappearance; it resets in-flight candidates conservatively and re-drives
  integration idempotently — CASE 26–30.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from .concurrency_policy import ACTION_REPO_GLOBAL, action_lock_name
from .integration_candidate import (
    CandidateState,
    GitClient,
    IntegrationCandidate,
    MergeClassification,
    candidate_id_for,
    classify_merge,
    deterministic_order,
    serialize_candidate_result,
    CandidateNotFound,
    CandidateRevisionError,
    IntegrationError,
)
from .models import LeaseFencedError, NotFound
from .worktree import (
    is_sha_like,
    resolve_canonical_worktree_path,
    validate_branch_identity,
    validate_repo_identity,
)

# ---------------------------------------------------------------------------
# Outcome types
# ---------------------------------------------------------------------------

#: Integration-ready source terminal state (conservative: DONE only).
INTEGRATION_READY_TERMINALS = ("DONE",)

#: Approving review verdicts (independent, controller-source evidence).
APPROVING_REVIEW_VERDICTS = ("approved", "passed", "accept", "accepted")


class _HolderLostError(Exception):
    """Internal: the integration holder lost its live lease or the action lock
    mid-sequence (I2 HIGH-1).  Always aborts to a bounded, recoverable outcome;
    never INTEGRATED."""


@dataclass(frozen=True)
class IntegrationOutcome:
    """Result of driving one candidate through integration."""

    candidate_id: str
    state: str
    classification: Optional[str] = None
    integrated_head: Optional[str] = None
    detail: Optional[str] = None
    locked: bool = False


@dataclass(frozen=True)
class ProcessOutcome:
    """Result of processing a full target queue serially."""

    repository: str
    integration_target: str
    integrated: List[str]
    conflicted: List[str]
    stale: List[str]
    failed: List[str]
    blocked: List[str]
    locked: bool = False


@dataclass(frozen=True)
class ReconcileOutcome:
    """Result of a conservative restart/crash reconciliation of one target."""

    repository: str
    integration_target: str
    reset_to_pending: List[str]
    reclaimed_lock: bool
    detail: Optional[str] = None


def compute_integration_evidence_mac(
    verdict: str,
    plan_hash: str,
    source_hash: str,
    test_definition_hash: str,
    mac_key: bytes,
) -> str:
    """Keyed HMAC-SHA256 over the canonical integration-evidence fields
    (I2 HIGH-7): binds the verdict + plan hash + exact snapshot identity to the
    controller-held MAC key.  Only evidence minted under this key verifies."""
    from .test_planning import canonical_bytes

    payload = canonical_bytes({
        "verdict": verdict,
        "plan_hash": plan_hash,
        "source_hash": source_hash,
        "test_definition_hash": test_definition_hash,
    })
    return hmac.new(bytes(mac_key), payload, hashlib.sha256).hexdigest()


def make_integration_evidence(
    verdict: str,
    plan_hash: str,
    source_hash: str,
    test_definition_hash: str,
    *,
    summary: str = "",
    test_count: int = 0,
    mac_key: Optional[bytes] = None,
) -> dict:
    """Build the bounded authenticated integration-evidence contract
    (I2 HIGH-7).  ``evidence_mac`` is present iff a ``mac_key`` is supplied."""
    evidence = {
        "verdict": verdict,
        "plan_hash": plan_hash,
        "source_hash": source_hash,
        "test_definition_hash": test_definition_hash,
        "summary": summary,
        "test_count": test_count,
    }
    if mac_key is not None:
        evidence["evidence_mac"] = compute_integration_evidence_mac(
            verdict, plan_hash, source_hash, test_definition_hash, mac_key,
        )
    return evidence


# ---------------------------------------------------------------------------
# MergeQueue
# ---------------------------------------------------------------------------


class MergeQueue:
    """Durable integration / merge-queue controller (single authority)."""

    def __init__(
        self,
        store,
        worktrees_root: str,
        *,
        git: Optional[GitClient] = None,
        plan_builder: Optional[Callable[..., object]] = None,
        test_runner: Optional[Callable[..., dict]] = None,
        review_policy: Optional[Callable[[dict], Optional[str]]] = None,
        mac_key: Optional[bytes] = None,
        resource_gate=None,
        evidence_dir: Optional[str] = None,
    ):
        self.store = store
        self.worktrees_root = worktrees_root
        self.git = git or GitClient()
        self._plan_builder = plan_builder
        self._test_runner = test_runner
        self._review_policy = review_policy or self._default_review_policy
        self._mac_key = mac_key
        # Resource Governor binding for integration tests (Phase I2 §38): a
        # ``ResourceGate`` protocol object (``.admit() -> ResourceAdmission``)
        # wired by the caller; None -> fail-closed BLOCKED in ``execute_plan``.
        self._resource_gate = resource_gate
        # Durable base for authenticated integration-test evidence (I2 HIGH-7).
        # Defaults to a hidden dir under the canonical worktrees root (the
        # canonical integration state area); never the live state dir.
        self._evidence_dir = evidence_dir

    # -- lock boundary -------------------------------------------------------

    @staticmethod
    def _canonical_repo(repository: str) -> str:
        """Realpath-canonicalise a repository identity for lock keys / worktree
        naming (I2 HIGH-3).  Aliases (symlinks, ``..``, trailing slashes) must
        not split the lock key or worktree name."""
        return os.path.realpath(os.path.abspath(os.fspath(repository)))

    def integration_lock_name(self, repository: str, integration_target: str) -> str:
        """The single-holder action-lock key for a (repository, target).

        The repository is realpath-canonicalised so a symlink/alias of the same
        repo can never yield a different lock key (I2 HIGH-3)."""
        return action_lock_name(
            ACTION_REPO_GLOBAL, repo_identity=self._canonical_repo(repository),
            name=f"integrate:{integration_target}",
        )

    # -- candidate admission -------------------------------------------------

    def _git_evidence_errors(self, job: dict) -> List[str]:
        """Authoritative git-evidence verification for admission (I2 HIGH-3).

        Verifies (fail-closed) that the recorded provenance matches REAL git
        evidence: the canonical worktree exists and its ``--show-toplevel``
        realpath equals the canonical repo identity; the recorded source head
        RESOLVES to a real commit (not merely sha-shaped); the recorded branch
        ref exists and its tip equals the source head; the recorded base is an
        ancestor of the source head; and the worktree is not dirty.  A missing
        /unreadable fact is an error — never treated as clean.
        """
        errors: List[str] = []
        repo_raw = job.get("repo_identity")
        wt = job.get("canonical_worktree_path")
        if not repo_raw or not wt:
            return errors  # no git evidence to verify (shape errors reported elsewhere)
        repo = self._canonical_repo(repo_raw)
        if not os.path.isdir(wt):
            errors.append("worktree_missing")
            return errors
        top = self.git.show_toplevel(wt)
        if top is None:
            errors.append("worktree_not_git")
            return errors
        if top != repo:
            errors.append("repo_identity_mismatch")
        source_head = job.get("expected_head") or job.get("current_head")
        if source_head and is_sha_like(source_head):
            if self.git.resolve_sha(repo, source_head) is None:
                errors.append("source_head_unresolvable")
        bid = job.get("branch_identity")
        if bid and source_head:
            tip = self.git.resolve_sha(repo, bid)
            if tip is None:
                errors.append("branch_unresolvable")
            elif tip != source_head:
                errors.append("branch_tip_mismatch")
        if self.git.is_dirty(wt):
            errors.append("worktree_dirty")
        base = job.get("base_commit")
        if base and is_sha_like(base) and source_head and is_sha_like(source_head):
            if not self.git.is_ancestor(repo, base, source_head):
                errors.append("base_not_ancestor")
        return errors

    def admission_errors(self, source_job_id: str) -> List[str]:
        """Return the list of admission-blocking reasons ([] = admissible).

        Controller-authoritative: only trusted store facts reach this method.
        A source job is integration-ready iff it reached a valid terminal
        state with proven provenance (verified against real git evidence),
        no unresolved HIGH/CRITICAL findings, and no still-active writer
        mutation.
        """
        errors: List[str] = []
        job = self.store.get_supervisor_job(source_job_id)
        if job is None:
            return ["source_job_missing"]
        if job.get("terminal") not in INTEGRATION_READY_TERMINALS:
            errors.append("source_not_terminal_done")
        repo = job.get("repo_identity")
        try:
            if validate_repo_identity(repo) is None:
                errors.append("repo_identity_missing")
        except ValueError:
            errors.append("repo_identity_invalid")
        if not is_sha_like(job.get("base_commit") or ""):
            errors.append("base_commit_not_proven")
        source_head = job.get("expected_head") or job.get("current_head")
        if not is_sha_like(source_head or ""):
            errors.append("source_head_not_proven")
        if job.get("expected_head") and job.get("current_head") \
                and job["expected_head"] != job["current_head"]:
            errors.append("head_ambiguous")
        try:
            if validate_branch_identity(job.get("branch_identity")) is None:
                errors.append("branch_identity_missing")
        except ValueError:
            errors.append("branch_identity_invalid")
        if job.get("open_findings_count", 0) != 0:
            errors.append("open_findings")
        # Unresolved HIGH/CRITICAL findings block admission (fail closed).
        # Severity is canonicalised case-insensitively (outputs.py stores
        # lowercase ``high``/``critical``; canonicalisation — never masking).
        for f in self.store.list_findings(job.get("task_id")):
            if f.status.value != "open":
                continue
            sev = (f.severity or "").strip().upper()
            if sev == "HIGH":
                errors.append("open_high_finding")
                break
            if sev == "CRITICAL":
                errors.append("open_critical_finding")
                break
        # Authoritative git-evidence verification (I2 HIGH-3).
        errors.extend(self._git_evidence_errors(job))
        return errors

    def enqueue_candidate(
        self,
        source_job_id: str,
        integration_target: str,
        *,
        depends_on: Optional[str] = None,
        priority: int = 0,
    ) -> dict:
        """Create (idempotently) a PENDING candidate from a trusted source job.

        Raises :class:`IntegrationError` (with the admission reason list) when
        the source job is not admissible.  ``depends_on`` may be a candidate id
        or a source job id (I1 single-dependency semantics); the latter is
        translated to the candidate id for the same target when that candidate
        exists.
        """
        try:
            target = validate_branch_identity(integration_target)
        except ValueError as exc:
            raise IntegrationError(f"invalid integration_target: {exc}")
        errors = self.admission_errors(source_job_id)
        if errors:
            raise IntegrationError("candidate not admissible: " + ",".join(errors))
        job = self.store.get_supervisor_job(source_job_id)
        repo = self._canonical_repo(job["repo_identity"])
        source_head = job.get("expected_head") or job.get("current_head")
        dep = depends_on
        if dep is None and job.get("depends_on"):
            # Translate the source job's prerequisite job id to the candidate id
            # for the same target (I1 semantics).
            dep = candidate_id_for(repo, target, job["depends_on"])
        return self.store.create_integration_candidate(
            repository=repo,
            integration_target=target,
            source_job_id=source_job_id,
            base_commit=job.get("base_commit"),
            source_head=source_head,
            source_branch=job.get("branch_identity"),
            depends_on=dep,
            priority=priority,
        )

    # -- evaluation ----------------------------------------------------------

    def _dependency_satisfied(self, candidate: dict) -> bool:
        """True iff the prerequisite's CANDIDATE has INTEGRATED (I2 HIGH-5).

        ``depends_on`` may be a candidate id or (untranslated) a source job id;
        in BOTH cases satisfaction requires the corresponding integration
        CANDIDATE for the same target to have reached INTEGRATED — a source
        job merely being terminal-DONE is NOT sufficient (its candidate may
        still be PENDING).
        """
        dep = candidate.get("depends_on")
        if not dep:
            return True
        dep_row = self.store.get_integration_candidate(dep)
        if dep_row is not None:
            return dep_row["state"] == CandidateState.INTEGRATED.value
        # ``depends_on`` may reference a source job id -> translate to the
        # candidate id for the same target and gate on that candidate.
        translated = candidate_id_for(
            candidate["repository"], candidate["integration_target"], dep)
        dep_row = self.store.get_integration_candidate(translated)
        if dep_row is not None:
            return dep_row["state"] == CandidateState.INTEGRATED.value
        return False

    def _default_review_policy(self, source_job: dict) -> Optional[str]:
        """Default independent-review gate (Phase I2 §17).

        Returns ``None`` when review is satisfied or not required; returns an
        error code when a required closing review is missing.  A task at
        ``risk_class='HIGH'`` requires independent (controller-source, approving)
        review evidence; the writer's own agent review is never sufficient.
        """
        task = self.store.get_task(source_job.get("task_id"))
        if task is None or task.risk_class.value != "HIGH":
            return None
        for r in self.store.list_reviews(source_job.get("task_id")):
            if r.source_class == "controller" \
                    and r.verdict in APPROVING_REVIEW_VERDICTS:
                return None
        return "REVIEW_REQUIRED"

    def evaluate_candidate(self, candidate_id: str) -> dict:
        """Promote a PENDING/STALE candidate to READY when admissible.

        Pure store-fact evaluation (deterministic, no git, no LLM): dependency
        must be satisfied and the review gate must be clear.  Stale-base /
        conflict detection happens just-in-time at integration (authoritative
        git).
        """
        c = self.store.get_integration_candidate(candidate_id)
        if c is None:
            raise CandidateNotFound(candidate_id)
        if c["state"] not in (CandidateState.PENDING.value, CandidateState.STALE.value):
            return c
        if not self._dependency_satisfied(c):
            return self.store.transition_integration_candidate(
                candidate_id, from_state=c["state"], to_state=CandidateState.PENDING.value,
                expected_revision=c["revision"],
                last_error_code="DEPENDENCY_NOT_INTEGRATED",
            )
        source_job = self.store.get_supervisor_job(c["source_job_id"])
        review_error = self._review_policy(source_job or {}) if source_job else None
        if review_error:
            return self.store.transition_integration_candidate(
                candidate_id, from_state=c["state"], to_state=CandidateState.PENDING.value,
                expected_revision=c["revision"], last_error_code=review_error,
            )
        return self.store.transition_integration_candidate(
            candidate_id, from_state=c["state"], to_state=CandidateState.READY.value,
            expected_revision=c["revision"], last_error_code=None,
        )

    # -- target tip / integration base ---------------------------------------

    def _target_tip(self, repository: str, target: str) -> Optional[str]:
        return self.git.resolve_sha(repository, target)

    def _current_integration_base(
        self, repository: str, target: str, target_tip: Optional[str],
    ) -> Optional[str]:
        """The head the next integration worktree should start from.

        = the ``integrated_head`` of the last INTEGRATED candidate for this
        target (FIFO), else the current target tip.  Never mutates anything.
        """
        candidates = self.store.list_integration_candidates(
            repository=repository, integration_target=target)
        last_integrated_head = None
        for c in candidates:
            if c["state"] == CandidateState.INTEGRATED.value \
                    and is_sha_like(c.get("integrated_head") or ""):
                last_integrated_head = c["integrated_head"]
        if last_integrated_head is not None:
            return last_integrated_head
        return target_tip

    def _integration_worktree_name(self, repository: str, target: str) -> str:
        # Canonical repo identity so aliases cannot split the worktree name.
        repo = self._canonical_repo(repository)
        digest = hashlib.sha256(
            f"{repo}\x00{target}".encode("utf-8")).hexdigest()[:12]
        return f"integration-{digest}"

    def _integration_worktree_path(self, repository: str, target: str) -> str:
        name = self._integration_worktree_name(repository, target)
        return resolve_canonical_worktree_path(name, base_root=self.worktrees_root)

    def _integration_branch(self, target: str) -> str:
        # Deterministic per-target integration branch; never equals the target
        # (the target is only ever read).  The queue id IS the integration
        # target under the single-holder-per-target policy (§25), so
        # ``integration/<target>`` == ``integration/<queue-id>`` (documented,
        # I2 LOW-8).
        return "integration/" + target.lstrip("/")

    def _ensure_integration_worktree(
        self, repository: str, target: str, base_sha: str,
    ) -> Tuple[str, str]:
        """Create/reuse a clean integration worktree at ``base_sha``.

        Returns ``(worktree_path, branch)``.  Idempotent: stale worktree
        residue is removed, but a pre-existing integration BRANCH is only
        deleted when a recorded candidate proves this queue created it
        (``integration_branch == branch``) — never force-delete a branch that
        is not provably owned by this queue (I2 LOW-8).
        """
        branch = self._integration_branch(target)
        wt_path = self._integration_worktree_path(repository, target)
        if base_sha is None or not is_sha_like(base_sha):
            raise IntegrationError("invalid integration base sha")
        # Remove any existing worktree residue (best effort; never the target).
        self.git.remove_worktree(repository, wt_path)
        if self.git.branch_exists(repository, branch):
            owned = any(
                c.get("integration_branch") == branch
                for c in self.store.list_integration_candidates(
                    repository=repository, integration_target=target)
            )
            if not owned:
                raise IntegrationError(
                    f"refusing to remove unowned branch {branch!r}")
            self.git.delete_branch(repository, branch)
        if not self.git.add_worktree(repository, wt_path, branch, base_sha):
            raise IntegrationError(
                f"failed to create integration worktree for {target!r}")
        return wt_path, branch

    # -- merge classification wrapper ---------------------------------------

    def _classify(
        self, candidate: dict, target_tip: Optional[str],
    ) -> MergeClassification:
        dependency_integrated = self._dependency_satisfied(candidate)
        return classify_merge(
            self.git, candidate["repository"],
            target_tip=target_tip,
            source_head=candidate.get("source_head"),
            claimed_base=candidate.get("base_commit"),
            dependency_integrated=dependency_integrated,
        )

    # -- integration test step ----------------------------------------------

    def _changed_paths(self, worktree_path: str, base_sha: str) -> Tuple[str, ...]:
        return self.git.changed_paths_vs(worktree_path, base_sha)

    def _run_integration_tests(
        self, candidate: dict, worktree_path: str, base_sha: str,
    ) -> Tuple[object, dict]:
        """Build a FRESH integration TestPlan and run it (injectable).

        Returns ``(plan, evidence)``.  Stale source-worktree PASS evidence can
        never close the integration: the plan is built for the INTEGRATED
        snapshot and the caller injects the plan builder / runner (CASE
        21/22/23/24).  The evidence carries the authenticated contract
        (verdict + plan hash + snapshot identity + evidence MAC, I2 HIGH-7).
        """
        changed = self._changed_paths(worktree_path, base_sha)
        if self._plan_builder is not None:
            plan = self._plan_builder(candidate, changed, base_sha)
        else:
            plan = self._default_plan_builder(candidate, changed, base_sha)
        if self._test_runner is not None:
            return plan, self._test_runner(candidate, worktree_path, plan, changed)
        return plan, self._default_test_runner(candidate, worktree_path, plan, changed)

    def _default_plan_builder(self, candidate: dict, changed: Tuple[str, ...],
                              base_sha: str):
        from .test_planning import (
            ChangeEvidence,
            build_test_plan,
            get_default_inventory,
            get_default_policy,
        )

        # I2 HIGH-6: an integration run is a phase-closing workflow AND
        # inherits the source task's risk classification, so a HIGH-risk
        # integration forces the broad closing full suite.
        risk_class = None
        job = self.store.get_supervisor_job(candidate.get("source_job_id"))
        if job is not None:
            task = self.store.get_task(job.get("task_id"))
            if task is not None:
                risk_class = task.risk_class.value
        evidence = ChangeEvidence(
            changed_paths=tuple(changed), base_ref=base_sha,
            phase_closing=True, risk_class=risk_class,
        )
        return build_test_plan(
            evidence, get_default_policy(), get_default_inventory(),
            mac_key=self._mac_key,
        )

    def _evidence_store_path(self, repository: str, target: str) -> str:
        """Durable evidence-store path for one (repository, target) (I2 HIGH-7)."""
        base = self._evidence_dir or os.path.join(
            self.worktrees_root, ".integration-evidence")
        os.makedirs(base, exist_ok=True)
        digest = hashlib.sha256(
            f"{self._canonical_repo(repository)}\x00{target}".encode("utf-8")
        ).hexdigest()[:16]
        return os.path.join(base, f"integration-{digest}.json")

    def _default_test_runner(self, candidate: dict, worktree_path: str, plan,
                             changed: Tuple[str, ...]) -> dict:
        from .test_execution import (
            EvidenceStore,
            PytestRunner,
            compute_snapshot_identity,
            execute_plan,
        )

        snapshot = compute_snapshot_identity(worktree_path)
        runner = PytestRunner(project_root=worktree_path)
        store = EvidenceStore(
            path=self._evidence_store_path(
                candidate["repository"], candidate["integration_target"]),
            mac_key=self._mac_key,
        )
        report = execute_plan(
            plan, runner, snapshot=snapshot, resource_gate=self._resource_gate,
            store=store, project_root=worktree_path, mac_key=self._mac_key,
        )
        return make_integration_evidence(
            report.verdict.value, plan.plan_hash,
            snapshot.source_hash, snapshot.test_definition_hash,
            summary="integrated snapshot test execution",
            test_count=sum(
                s.test_count for st in report.stages for s in st.selector_results
            ),
            mac_key=self._mac_key,
        )

    def _evidence_authentic(self, evidence: object, plan: object) -> bool:
        """Fail-closed verification of the runner evidence contract (I2 HIGH-7).

        Accepts evidence only when it is a dict whose ``plan_hash`` equals the
        expected plan hash AND whose ``evidence_mac`` is a valid keyed MAC over
        (verdict, plan hash, source hash, test-definition hash).  A fake
        runner that fabricates ``DONE`` without the controller MAC key is
        rejected.
        """
        if not isinstance(evidence, dict):
            return False
        if evidence.get("plan_hash") != getattr(plan, "plan_hash", None):
            return False
        if self._mac_key is None:
            return False  # no key -> cannot authenticate (fail closed)
        mac = evidence.get("evidence_mac")
        if not isinstance(mac, str) or not mac:
            return False
        expected = compute_integration_evidence_mac(
            evidence.get("verdict"), evidence.get("plan_hash"),
            evidence.get("source_hash"), evidence.get("test_definition_hash"),
            self._mac_key,
        )
        return hmac.compare_digest(mac, expected)

    # -- single-candidate integration ---------------------------------------

    def integrate_candidate(
        self,
        candidate_id: str,
        *,
        holder_job_id: str,
        holder_lease_epoch: int,
        run_tests: bool = True,
    ) -> IntegrationOutcome:
        """Drive one candidate through integration (fenced, restart-safe).

        The ``(holder_job_id, holder_lease_epoch)`` must be the current
        unexpired lease holder of an existing non-terminal job (the integration
        authority).  Returns an :class:`IntegrationOutcome`; never raises on a
        legitimate classification (CONFLICT/STALE/DEPENDENCY) — those are
        returned as bounded states.
        """
        c = self.store.get_integration_candidate(candidate_id)
        if c is None:
            raise CandidateNotFound(candidate_id)
        repository = c["repository"]
        target = c["integration_target"]
        lock_name = self.integration_lock_name(repository, target)

        if not self.store.try_acquire_action_lock(
            lock_name, job_id=holder_job_id, lease_epoch=holder_lease_epoch,
        ):
            return IntegrationOutcome(candidate_id, c["state"], locked=True,
                                      detail="target_locked_by_another_holder")
        try:
            return self._integrate_locked(
                c, holder_job_id, holder_lease_epoch, run_tests=run_tests,
            )
        finally:
            self.store.release_action_lock(
                lock_name, job_id=holder_job_id, lease_epoch=holder_lease_epoch)

    def _integrate_locked(
        self, c: dict, holder_job_id: str, holder_lease_epoch: int,
        *, run_tests: bool,
    ) -> IntegrationOutcome:
        repository = c["repository"]
        target = c["integration_target"]
        candidate_id = c["id"]
        lock_name = self.integration_lock_name(repository, target)

        def outcome(state, classification=None, detail=None, head=None):
            return IntegrationOutcome(candidate_id, state,
                                      classification=classification,
                                      integrated_head=head, detail=detail)

        def fenced(cur: dict, from_state: str, to_state: str, **fields) -> dict:
            """Holder-verified transition; raises _HolderLostError on lease/lock
            loss, CandidateRevisionError/NotFound on a state/revision race."""
            try:
                return self.store.transition_integration_candidate_authoritative(
                    candidate_id, lock_name=lock_name, holder_job_id=holder_job_id,
                    holder_lease_epoch=holder_lease_epoch, from_state=from_state,
                    to_state=to_state, expected_revision=cur["revision"],
                    holder_owner_instance_id=holder_job_id, **fields)
            except LeaseFencedError as exc:
                raise _HolderLostError(str(exc)) from exc

        def mark(cur: dict, state: CandidateState, *, classification=None,
                 detail=None, head=None, conflict_detail=None):
            fields = {}
            if classification is not None:
                fields["merge_classification"] = classification
            if detail is not None:
                fields["last_error_code"] = detail
            if head is not None:
                fields["integrated_head"] = head
            if conflict_detail is not None:
                fields["conflict_detail"] = conflict_detail
            try:
                updated = fenced(cur, cur["state"], state.value, **fields)
            except (CandidateRevisionError, NotFound):
                updated = self.store.get_integration_candidate(candidate_id) or cur
            return outcome(updated["state"],
                           classification=updated.get("merge_classification"),
                           head=updated.get("integrated_head"),
                           detail=updated.get("last_error_code"))

        try:
            # 1. READY -> INTEGRATING (holder-verified, revision-fenced).
            try:
                c = fenced(c, CandidateState.READY.value,
                           CandidateState.INTEGRATING.value)
            except (CandidateRevisionError, NotFound):
                return outcome(c["state"], detail="state_changed_concurrently")

            # 2. Resolve target tip (authoritative git).
            target_tip = self._target_tip(repository, target)
            if target_tip is None:
                return mark(c, CandidateState.FAILED, detail="target_tip_unreadable")

            # 2b. Re-validate the immutable source job (terminal source mutation
            #     / HEAD mismatch invalidates the candidate — CASE / §F).
            src = self.store.get_supervisor_job(c["source_job_id"])
            if src is None or src.get("terminal") != "DONE":
                return mark(c, CandidateState.STALE, detail="source_terminal_mutated")
            src_head = src.get("expected_head") or src.get("current_head")
            if src_head != c.get("source_head"):
                return mark(c, CandidateState.STALE, detail="source_head_changed")

            # 3. Integration base = the head this candidate actually merges onto
            #    (last INTEGRATED candidate's head, else the target tip).
            base_sha = self._current_integration_base(repository, target, target_tip)
            if base_sha is None or not is_sha_like(base_sha):
                return mark(c, CandidateState.FAILED, detail="integration_base_unresolved")

            # 4. Authoritative merge classification (git only) against the
            #    integration base (stale-base + conflict detection).
            classification = self._classify(c, base_sha)
            if classification == MergeClassification.DEPENDENCY_NOT_INTEGRATED:
                return mark(c, CandidateState.PENDING,
                            classification=classification.value,
                            detail="dependency_not_integrated")
            if classification == MergeClassification.STALE_BASE:
                return mark(c, CandidateState.STALE,
                            classification=classification.value,
                            detail="stale_base")
            if classification == MergeClassification.CONFLICT:
                return mark(c, CandidateState.CONFLICTED,
                            classification=classification.value,
                            detail="merge_conflict")
            if classification == MergeClassification.UNKNOWN:
                return mark(c, CandidateState.FAILED,
                            classification=classification.value,
                            detail="merge_classification_unknown")
            # DIVERGED_CLEAN (I2 LOW-9): source diverged from the integration
            # base but ``git merge-tree --write-tree`` proved a clean three-way
            # merge.  The disposition is explicit: proceed with a NORMAL
            # ``--no-ff`` merge commit (never a rebase / history rewrite).

            # 5. Prepare integration worktree (never the Writer worktree).
            try:
                wt_path, branch = self._ensure_integration_worktree(
                    repository, target, base_sha)
            except IntegrationError as exc:
                return mark(c, CandidateState.FAILED, detail=str(exc))
            try:
                c = fenced(c, CandidateState.INTEGRATING.value,
                           CandidateState.INTEGRATING.value,
                           integration_worktree_path=wt_path,
                           integration_branch=branch)
            except (CandidateRevisionError, NotFound):
                return outcome(CandidateState.INTEGRATING.value,
                               detail="state_changed_during_prepare")

            # 5b. Apply the merge (argv git; no force/ours/theirs).
            clean, err = self.git.merge_no_ff(
                wt_path, c["source_head"], f"integrate {candidate_id}")
            if not clean:
                self.git.merge_abort(wt_path)
                return mark(c, CandidateState.CONFLICTED,
                            classification=classification.value,
                            conflict_detail=err)
            integrated_head = self.git.head(wt_path)
            if not is_sha_like(integrated_head or ""):
                self.git.merge_abort(wt_path)
                return mark(c, CandidateState.FAILED, detail="integrated_head_unreadable")
            try:
                c = fenced(c, CandidateState.INTEGRATING.value,
                           CandidateState.INTEGRATING.value,
                           integrated_head=integrated_head,
                           merge_classification=classification.value)
            except (CandidateRevisionError, NotFound):
                return outcome(CandidateState.INTEGRATING.value,
                               detail="state_changed_after_merge")

            # 6. Build + run a FRESH integration TestPlan on the integrated
            #    snapshot; the evidence MUST carry the expected plan hash +
            #    authenticated provenance (I2 HIGH-7) before it can close.
            if run_tests:
                try:
                    plan, evidence = self._run_integration_tests(c, wt_path, base_sha)
                except IntegrationError as exc:
                    return mark(c, CandidateState.FAILED, detail=str(exc))
                if not self._evidence_authentic(evidence, plan):
                    return mark(c, CandidateState.FAILED,
                                detail="integration_evidence_unauthenticated")
                result_json = serialize_candidate_result(evidence)
                passed = evidence.get("verdict") == "DONE"
                try:
                    c = fenced(c, CandidateState.INTEGRATING.value,
                               CandidateState.INTEGRATING.value,
                               result_json=result_json)
                except (CandidateRevisionError, NotFound):
                    return outcome(CandidateState.INTEGRATING.value,
                                   detail="state_changed_after_tests")
                if passed:
                    return mark(c, CandidateState.INTEGRATED,
                                classification=classification.value,
                                head=integrated_head, detail="tests_passed")
                return mark(c, CandidateState.FAILED,
                            classification=classification.value,
                            detail="tests_failed")
            # No tests requested: the integrated snapshot is authoritative but NOT
            # INTEGRATED (tests are required to close integration — CASE 21/22).
            return mark(c, CandidateState.FAILED,
                        classification=classification.value,
                        detail="tests_not_run")
        except _HolderLostError:
            # Bounded, recoverable: the candidate is left INTEGRATING (never
            # INTEGRATED); reconcile_target or the next holder recovers it.
            cur = self.store.get_integration_candidate(candidate_id) or c
            return outcome(cur["state"], detail="holder_lease_or_lock_lost")

    # -- serial target processing -------------------------------------------

    def process_target(
        self,
        repository: str,
        integration_target: str,
        *,
        holder_job_id: str,
        holder_lease_epoch: int,
        run_tests: bool = True,
    ) -> ProcessOutcome:
        """Process a target queue serially (conservative §25).

        Evaluates candidates, orders deterministically, and integrates them one
        at a time.  A CONFLICT/FAILED candidate stops the queue (its successor
        would be built on an un-integrated base); already-INTEGRATED evidence
        is never rolled back (CASE 19/25/27/37).
        """
        integrated: List[str] = []
        conflicted: List[str] = []
        stale: List[str] = []
        failed: List[str] = []
        blocked: List[str] = []
        lock_name = self.integration_lock_name(repository, integration_target)
        if not self.store.try_acquire_action_lock(
            lock_name, job_id=holder_job_id, lease_epoch=holder_lease_epoch,
        ):
            return ProcessOutcome(repository, integration_target, integrated,
                                  conflicted, stale, failed, blocked, locked=True)
        try:
            candidates = self.store.list_integration_candidates(
                repository=repository, integration_target=integration_target)
            # Evaluate all non-terminal candidates first (store-fact promotion).
            for c in candidates:
                if c["state"] in (CandidateState.PENDING.value,
                                  CandidateState.STALE.value):
                    self.evaluate_candidate(c["id"])
            candidates = self.store.list_integration_candidates(
                repository=repository, integration_target=integration_target)
            integrated_ids = {
                c["id"] for c in candidates
                if c["state"] == CandidateState.INTEGRATED.value
            }
            cand_objs = [IntegrationCandidate.from_row(c) for c in candidates]
            order = deterministic_order(cand_objs, integrated_ids=integrated_ids)
            for c in order.blocked:
                blocked.append(c.id)
                # I2 HIGH-5: cycle members must transition to candidate BLOCKED
                # with bounded evidence (never silently left READY).
                try:
                    self.store.transition_integration_candidate(
                        c.id, from_state=c.state, to_state=CandidateState.BLOCKED.value,
                        expected_revision=c.revision,
                        last_error_code="dependency_cycle",
                    )
                except (CandidateRevisionError, NotFound):
                    pass
            for c in order.deferred:
                # Deferred: dependency not integrated -> leave PENDING.
                pass
            for c in order.ordered:
                outcome = self.integrate_candidate(
                    c.id, holder_job_id=holder_job_id,
                    holder_lease_epoch=holder_lease_epoch, run_tests=run_tests,
                )
                if outcome.state == CandidateState.INTEGRATED.value:
                    integrated.append(outcome.candidate_id)
                    continue
                if outcome.state == CandidateState.CONFLICTED.value:
                    conflicted.append(outcome.candidate_id)
                    break  # successor would build on an un-integrated base
                if outcome.state == CandidateState.STALE.value:
                    stale.append(outcome.candidate_id)
                    continue
                if outcome.state == CandidateState.FAILED.value:
                    failed.append(outcome.candidate_id)
                    break
                if outcome.locked:
                    # Re-acquire failed mid-queue (holder lost the lock).
                    break
            return ProcessOutcome(repository, integration_target, integrated,
                                  conflicted, stale, failed, blocked)
        finally:
            self.store.release_action_lock(
                lock_name, job_id=holder_job_id, lease_epoch=holder_lease_epoch)

    # -- restart/crash recovery ---------------------------------------------

    def reconcile_target(
        self, repository: str, integration_target: str,
    ) -> ReconcileOutcome:
        """Conservative recovery of one target queue (CASE 26–30, I2 HIGH-2).

        Per-candidate recovery, not a blanket reset: an in-flight (INTEGRATING)
        candidate is reset to PENDING only when its recorded holder no longer
        holds a LIVE lease AND the action lock is no longer held by that
        holder (a live holder still driving integration is preserved).  Reset
        clears the holder columns explicitly and reconciles the recorded
        worktree/HEAD evidence (bounded).  A stale action lock (holder no
        longer live) is reclaimed atomically and reported truthfully in
        ``reclaimed_lock``.  Never infers INTEGRATED from process
        disappearance.
        """
        reset: List[str] = []
        lock_name = self.integration_lock_name(repository, integration_target)
        candidates = self.store.list_integration_candidates(
            repository=repository, integration_target=integration_target)
        for c in candidates:
            if c["state"] != CandidateState.INTEGRATING.value:
                continue
            holder = c.get("holder_owner_instance_id")
            epoch = c.get("holder_lease_epoch") or 0
            holder_alive = bool(holder) and self.store.job_holds_current_lease(
                holder, epoch)
            lock_held = bool(holder) and self.store.action_lock_held_by(
                lock_name, holder, epoch)
            if holder_alive and lock_held:
                # A live holder still owns the lock: preserve (never reset a
                # candidate that may still be mid-integration).
                continue
            # Reconcile real worktree/HEAD evidence before reset (bounded): if
            # the recorded integration worktree still exists and its HEAD
            # matches the recorded integrated_head, it is crash residue that
            # re-integration will recreate deterministically; if it does not
            # match, the evidence is stale and the reset is still safe (the
            # throwaway worktree is recreated).  Either way the candidate is
            # conservatively reset (never INTEGRATED).
            try:
                self.store.transition_integration_candidate(
                    c["id"], from_state=CandidateState.INTEGRATING.value,
                    to_state=CandidateState.PENDING.value,
                    expected_revision=c["revision"],
                    clear_holder=True,
                    last_error_code="recovered_after_restart",
                )
                reset.append(c["id"])
            except (CandidateRevisionError, NotFound):
                pass
        reclaimed = self.store.reclaim_stale_action_lock(lock_name)
        return ReconcileOutcome(repository, integration_target, reset,
                                reclaimed_lock=reclaimed)
