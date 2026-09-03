"""Phase I3-C1 — CI External-Wait / PR Lifecycle Core (GitHub READ-ONLY).

Provider-neutral CI-wait lifecycle built ON TOP of the Phase-B/G external-wait
core (``external_wait.py``) and the existing ``external_waits`` table + the
``WAITING_EXTERNAL`` primary job state.  This module is ADDITIVE: it introduces
a richer normalized CI state model, deterministic check aggregation, and a
trusted, non-LLM polling controller for CI waits — but it reuses the SAME store
primitives (atomic ``transition_to_waiting_external`` / idempotent
``complete_wait_and_requeue``), the SAME backoff ladder, and the SAME
``WAITING_EXTERNAL`` state machine.  There is NO second scheduler and NO second
source of truth.

Trust boundaries (mirrors §8/§16 of the wait core, tightened for CI):

* A CI wait can only be CREATED from authoritative local evidence (a resolved
  :class:`CiWaitSpec` carrying provider/account/repository/PR number/expected
  head SHA/expected base/required-check policy/source job + candidate).  Agent
  text can never create wait authority.
* The provider is read through an allowlisted :class:`CiWaitAdapter`; its
  output is UNTRUSTED DATA, normalized into :class:`CiRead`, strictly
  validated, and reduced to a bounded :class:`CiSnapshot`.
* No LLM occupies a slot while WAITING_EXTERNAL; polling is deterministic
  provider work only.  A model is activated only AFTER a wake, and only by the
  parent workflow's normal admission/claim path.
* There is NO write path to the provider in this phase: the GitHub adapter's
  mutation surface stays structurally disabled (ProviderWriteDisabled).

Hard rules (code-enforced, tested): NO_CHECKS_CONFIGURED != SUCCESS;
UNKNOWN != SUCCESS; PROVIDER_UNAVAILABLE != CODE_FAILURE; RATE_LIMITED !=
CODE_FAILURE; CANCELLED != SUCCESS; no LLM interprets "looks green" — only
structured provider evidence is aggregated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Optional, Protocol, Sequence
from uuid import uuid4

from . import job_state
from .models import DispatchStatus, RoleRunStatus
from .external_wait import (
    MAX_PROVIDER_LEN,
    MAX_REASON_LEN,
    MAX_REF_LEN,
    MAX_SUBJECT_LEN,
    _iso,
    _parse_iso,
    _bounded_reason,
    next_check_delay_seconds,
)

# ---------------------------------------------------------------------------
# Bounded evidence budget (matches store.MAX_JSON_COLUMN_BYTES).
# ---------------------------------------------------------------------------

MAX_CI_EVIDENCE_BYTES = 64 * 1024
MAX_CI_CHECKS = 256            # bounded number of normalized checks per read
MAX_CI_CHECK_NAME_LEN = 256
MAX_CI_RUN_REF_LEN = 512


# ---------------------------------------------------------------------------
# Normalized CI state model (closed set)
# ---------------------------------------------------------------------------

class CiState(str, Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    NEUTRAL = "NEUTRAL"
    SKIPPED = "SKIPPED"
    NO_CHECKS_CONFIGURED = "NO_CHECKS_CONFIGURED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    UNKNOWN = "UNKNOWN"


ALL_CI_STATES: frozenset[str] = frozenset(s.value for s in CiState)

#: Aggregate states that represent a TERMINAL CI result and therefore wake the
#: waiting job (SUCCESS/FAILURE/CANCELLED/TIMED_OUT/ACTION_REQUIRED).  PENDING,
#: UNKNOWN, NO_CHECKS_CONFIGURED, NEUTRAL and SKIPPED do NOT wake.
CI_TERMINAL_STATES: frozenset[str] = frozenset({
    CiState.SUCCESS.value,
    CiState.FAILURE.value,
    CiState.CANCELLED.value,
    CiState.TIMED_OUT.value,
    CiState.ACTION_REQUIRED.value,
})

#: Normalized individual-check conclusions (closed set; ``None`` = not
#: completed yet).  ``STALE`` / ``STARTUP_FAILURE`` are GitHub's own
#: conclusions, preserved verbatim so no "looks green" reinterpretation occurs.
CHECK_CONCLUSIONS: frozenset[str] = frozenset({
    "SUCCESS", "FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED",
    "NEUTRAL", "SKIPPED", "STALE", "STARTUP_FAILURE",
})

#: Normalized individual-check status (closed set).
CHECK_STATUSES: frozenset[str] = frozenset({
    "QUEUED", "IN_PROGRESS", "COMPLETED", "PENDING", "UNKNOWN",
})

#: Normalized PR lifecycle state (closed set, read-only).
PR_OPEN = "OPEN"
PR_CLOSED = "CLOSED"
PR_MERGED = "MERGED"
PR_UNKNOWN = "UNKNOWN"
PR_STATES: frozenset[str] = frozenset({PR_OPEN, PR_CLOSED, PR_MERGED, PR_UNKNOWN})

#: Provider error classes observed on a read (closed set).
PROVIDER_ERROR_NONE = None
PROVIDER_ERROR_UNAVAILABLE = "unavailable"
PROVIDER_ERROR_RATE_LIMITED = "rate_limited"
PROVIDER_ERROR_NETWORK = "network"
PROVIDER_ERROR_UNKNOWN = "unknown"
PROVIDER_ERRORS: frozenset[str] = frozenset({
    PROVIDER_ERROR_UNAVAILABLE, PROVIDER_ERROR_RATE_LIMITED,
    PROVIDER_ERROR_NETWORK, PROVIDER_ERROR_UNKNOWN,
})

#: Deterministic partial CI failure classification (closed set).
CI_FAIL_CODE = "CODE_FAILURE"          # deterministic code/test failure
CI_FAIL_INFRA = "INFRASTRUCTURE_FAILURE"
CI_FAIL_CANCELLED = "CANCELLED"
CI_FAIL_TIMEOUT = "TIMEOUT"
CI_FAIL_PROVIDER = "PROVIDER"
CI_FAIL_UNKNOWN = "UNKNOWN"
ALL_CI_FAILURE_CLASSES: frozenset[str] = frozenset({
    CI_FAIL_CODE, CI_FAIL_INFRA, CI_FAIL_CANCELLED, CI_FAIL_TIMEOUT,
    CI_FAIL_PROVIDER, CI_FAIL_UNKNOWN,
})

#: Deterministic name markers for the partial CODE vs INFRA classification
#: (bounded, closed; everything else classifies as UNKNOWN — never fabricated).
_INFRA_MARKERS: tuple[str, ...] = (
    "deploy", "preview", "pages", "netlify", "vercel", "release",
    "infrastructure", "infra", "terraform", "provision",
)
_CODE_MARKERS: tuple[str, ...] = (
    "test", "lint", "build", "check", "ci", "type", "unit", "integration",
    "e2e", "format", "coverage", "mypy", "pytest",
)


# ---------------------------------------------------------------------------
# CI identity ref ("owner/repo#<pr_number>") — bounded, canonical
# ---------------------------------------------------------------------------

def ci_ref(repository: str, pr_number: int) -> str:
    """Canonical CI wait ref: ``owner/repo#<pr_number>`` (bounded)."""
    if not isinstance(repository, str) or not repository.strip():
        raise ValueError("repository must be a non-empty string")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) \
            or pr_number <= 0:
        raise ValueError("pr_number must be a positive integer")
    ref = f"{repository}#{pr_number}"
    if len(ref) > MAX_REF_LEN:
        raise ValueError(f"ref exceeds {MAX_REF_LEN} chars")
    return ref


def parse_ci_ref(ref: str) -> tuple[str, int]:
    """Parse a canonical CI ref back into ``(repository, pr_number)``.

    Raises :class:`ValueError` on any malformed ref (untrusted provider data is
    never used to reconstruct an identity).
    """
    if not isinstance(ref, str) or not ref:
        raise ValueError("ref must be a non-empty string")
    if ref.count("#") != 1:
        raise ValueError("malformed CI ref")
    repository, _, num = ref.partition("#")
    if not repository or "/" not in repository or not num.isdigit():
        raise ValueError("malformed CI ref")
    pr_number = int(num)
    if pr_number <= 0:
        raise ValueError("malformed CI ref")
    return repository, pr_number


# ---------------------------------------------------------------------------
# Normalized types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CiCheck:
    """One normalized CI check/status context (bounded, untrusted→validated)."""

    name: str
    conclusion: Optional[str]      # None while not completed
    status: str                    # QUEUED / IN_PROGRESS / COMPLETED / ...
    run_ref: Optional[str] = None
    details_url: Optional[str] = None
    check_id: int = 0              # provider-issued monotonic id (for event_version)


@dataclass(frozen=True)
class CiRead:
    """Normalized, validated provider read for one CI wait check.

    ``pr_head_sha`` is the PR's CURRENT head SHA observed from PR state (may
    differ from the bound ``expected_subject`` — the controller treats that as
    STALE).  ``checks`` are the check runs/statuses for the BOUND head SHA.
    ``provider_error`` is ``None`` on a clean read, else one of the closed
    provider-error classes.
    """

    repository: str
    pr_number: int
    pr_head_sha: Optional[str]
    base_ref: Optional[str]
    pr_state: str
    checks: tuple
    provider_error: Optional[str] = None
    rate_limit_reset_at: Optional[str] = None
    event_version: int = 0


@dataclass(frozen=True)
class CiSnapshot:
    """The controller's bounded aggregation of a :class:`CiRead`.

    ``aggregate_state`` is the normalized aggregate CI state;
    ``failing_checks``/``missing_required``/``required_checks`` are the
    structured evidence used for persistence and post-wake reasoning;
    ``classification`` is the deterministic partial failure classification.
    """

    read: CiRead
    aggregate_state: str
    required_checks: tuple
    optional_checks: tuple
    failing_checks: tuple
    missing_required: tuple
    classification: str = CI_FAIL_UNKNOWN


# ---------------------------------------------------------------------------
# Deterministic aggregation (pure, no LLM)
# ---------------------------------------------------------------------------

def _norm_check_name(name) -> str:
    if not isinstance(name, str):
        return ""
    return name.strip()[:MAX_CI_CHECK_NAME_LEN]


#: Non-success check conclusions (terminal non-success or neutral/skipped/stale)
#: used for evidence persistence — a conclusion that is NOT a clean SUCCESS and
#: is never masked by a partial/empty required policy.
_FAILING_CHECK_CONCLUSIONS: frozenset[str] = frozenset({
    "FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED",
    "STARTUP_FAILURE", "NEUTRAL", "SKIPPED", "STALE",
})


def aggregate_ci_state(
    checks: Sequence[CiCheck],
    *,
    required: Optional[Sequence[str]],
    optional: Sequence[str] = (),
) -> CiState:
    """Deterministic normalized CI aggregate (pure; no LLM).

    ``required`` is the TRUSTED required-check name set:

    * ``None`` = unknown requirement set ⇒ conservative UNKNOWN.
    * an explicit EMPTY set = "no named required checks".  Because this phase
      retrieves a SINGLE unpaginated check-runs request with no
      branch-protection/ruleset completeness proof, an empty required set can
      NEVER be aggregated to SUCCESS from a partial universe.  A terminal
      non-success IS still reported (a check we observed failed is a safe
      positive signal); otherwise the aggregate is conservatively UNKNOWN.

    For a NON-empty required set the documented priority applies:

    1. any required check with a terminal non-success conclusion
       (FAILURE/STARTUP_FAILURE > CANCELLED > TIMED_OUT > ACTION_REQUIRED)
       ⇒ that conclusion (wins over missing/pending — HIGH-4);
    2. any required check with NEUTRAL/SKIPPED/STALE ⇒ UNKNOWN;
    3. a required check name absent from the observed set ⇒ UNKNOWN
       (incomplete; never SUCCESS);
    4. any required check still pending (no conclusion) ⇒ PENDING;
    5. otherwise (all required present and SUCCESS) ⇒ SUCCESS.

    ``optional`` is informational and never decides the aggregate.
    """
    req = None if required is None else frozenset(_norm_check_name(n)
                                                  for n in required)
    if req is None:
        return CiState.UNKNOWN
    if not checks:
        return CiState.NO_CHECKS_CONFIGURED
    by_name: dict[str, CiCheck] = {}
    for c in checks:
        by_name.setdefault(c.name, c)
    observed_names = set(by_name)

    def _concl(name: str) -> Optional[str]:
        c = by_name.get(name)
        return c.conclusion if c is not None else None

    if not req:
        # Empty required set + no completeness proof (HIGH-3): never SUCCESS
        # from an arbitrary subset.  A terminal non-success is a safe positive
        # signal (we observed a real failure); otherwise conservative UNKNOWN.
        for name in sorted(observed_names):
            if _concl(name) in ("FAILURE", "STARTUP_FAILURE"):
                return CiState.FAILURE
        for name in sorted(observed_names):
            if _concl(name) == "CANCELLED":
                return CiState.CANCELLED
        for name in sorted(observed_names):
            if _concl(name) == "TIMED_OUT":
                return CiState.TIMED_OUT
        for name in sorted(observed_names):
            if _concl(name) == "ACTION_REQUIRED":
                return CiState.ACTION_REQUIRED
        return CiState.UNKNOWN

    # Non-empty required set: terminal non-success wins before missing/pending.
    for name in sorted(req):
        if _concl(name) in ("FAILURE", "STARTUP_FAILURE"):
            return CiState.FAILURE
    for name in sorted(req):
        if _concl(name) == "CANCELLED":
            return CiState.CANCELLED
    for name in sorted(req):
        if _concl(name) == "TIMED_OUT":
            return CiState.TIMED_OUT
    for name in sorted(req):
        if _concl(name) == "ACTION_REQUIRED":
            return CiState.ACTION_REQUIRED
    for name in sorted(req):
        if _concl(name) in ("NEUTRAL", "SKIPPED", "STALE"):
            return CiState.UNKNOWN
    missing = [n for n in sorted(req) if n not in observed_names]
    if missing:
        return CiState.UNKNOWN
    for name in sorted(req):
        if by_name[name].conclusion is None:
            return CiState.PENDING
    return CiState.SUCCESS


def failing_required_checks(checks: Sequence[CiCheck],
                            required: Sequence[str]) -> tuple:
    """Return the required checks whose conclusion is a failure/neutral/stale
    terminal non-success (bounded, for evidence persistence)."""
    req = frozenset(_norm_check_name(n) for n in required)
    out = []
    for c in checks:
        if c.name in req and c.conclusion in _FAILING_CHECK_CONCLUSIONS:
            out.append(c)
    return tuple(out)


def failing_observed_checks(checks: Sequence[CiCheck],
                            required: Optional[Sequence[str]]) -> tuple:
    """Return observed checks with a non-success conclusion (evidence).

    Derived from OBSERVED conclusions, never gated solely by ``required``:
    when ``required`` is a non-empty set, restrict to required check names;
    when it is empty or ``None``, include ALL observed checks with a
    non-success conclusion so a failure is never masked by an empty required
    policy (HIGH-3).
    """
    req = None if required is None else frozenset(_norm_check_name(n)
                                                  for n in required)
    out = []
    for c in checks:
        if c.conclusion in _FAILING_CHECK_CONCLUSIONS:
            if req is None or not req or c.name in req:
                out.append(c)
    return tuple(out)


def missing_required_checks(checks: Sequence[CiCheck],
                            required: Sequence[str]) -> tuple:
    req = frozenset(_norm_check_name(n) for n in required)
    observed = {c.name for c in checks}
    return tuple(sorted(n for n in req if n not in observed))


def classify_ci_failure(
    aggregate_state: str,
    *,
    failing: Sequence[CiCheck] = (),
    provider_error: Optional[str] = None,
) -> str:
    """Deterministic partial failure classification (closed set).

    Provider conditions are NEVER code failures; a cancelled/timed-out CI run
    is NEVER a code failure.  For a concrete check failure, a bounded name
    marker decides CODE vs INFRASTRUCTURE; everything else is UNKNOWN (partial
    classification — post-wake reasoning refines it per the Phase-E capability
    policy; this function never fabricates).
    """
    if provider_error in (PROVIDER_ERROR_RATE_LIMITED,
                          PROVIDER_ERROR_UNAVAILABLE,
                          PROVIDER_ERROR_NETWORK,
                          PROVIDER_ERROR_UNKNOWN):
        return CI_FAIL_PROVIDER
    if aggregate_state == CiState.RATE_LIMITED.value:
        return CI_FAIL_PROVIDER
    if aggregate_state == CiState.PROVIDER_UNAVAILABLE.value:
        return CI_FAIL_PROVIDER
    if aggregate_state == CiState.TIMED_OUT.value:
        return CI_FAIL_TIMEOUT
    if aggregate_state == CiState.CANCELLED.value:
        return CI_FAIL_CANCELLED
    if aggregate_state == CiState.ACTION_REQUIRED.value:
        # Owner-action signal — never a code failure.
        return CI_FAIL_UNKNOWN
    conclusions = {c.conclusion for c in failing}
    if "TIMED_OUT" in conclusions:
        return CI_FAIL_TIMEOUT
    if "CANCELLED" in conclusions:
        return CI_FAIL_CANCELLED
    names = " ".join(c.name.lower() for c in failing)
    if any(m in names for m in _INFRA_MARKERS):
        return CI_FAIL_INFRA
    if any(m in names for m in _CODE_MARKERS):
        return CI_FAIL_CODE
    return CI_FAIL_UNKNOWN


# ---------------------------------------------------------------------------
# Adapter protocol + deterministic fixture
# ---------------------------------------------------------------------------

class CiWaitAdapter(Protocol):
    """Allowlisted READ-ONLY CI provider interface (no write authority).

    ``validate_ref`` gates a ref before it is persisted; ``read_ci_state``
    returns a bounded, normalized :class:`CiRead` for a due CI wait.  Adapters
    must be deterministic, bounded and side-effect free; a real adapter must
    never expose credentials, poll/shell commands or write authority.
    """

    provider_name: str

    def validate_ref(self, ref: str) -> bool: ...

    def read_ci_state(self, repository: str, pr_number: int,
                      head_sha: str) -> CiRead: ...


class FakeCiAdapter:
    """Deterministic, scriptable CI provider fixture (offline tests).

    Scripts are keyed by ``(repository, pr_number)``; each ``read_ci_state``
    pops the next :class:`CiRead` (sticky last one once exhausted).  An
    unscripted key returns a benign PENDING read (empty checks, OPEN, no head
    change) so a missing provider can be distinguished from a pending state at
    the manager level (the manager never calls an adapter whose provider is not
    allowlisted).
    """

    def __init__(self, provider_name: str = "github"):
        self.provider_name = provider_name
        self._queues: dict[tuple, list] = {}
        self._sticky: dict[tuple, CiRead] = {}
        self.reads: list[dict] = []
        self.fail_next: Optional[BaseException] = None

    def validate_ref(self, ref: str) -> bool:
        try:
            parse_ci_ref(ref)
            return True
        except ValueError:
            return False

    def script(self, repository: str, pr_number: int, reads) -> None:
        self._queues[(repository, pr_number)] = list(reads)

    def set_sticky(self, repository: str, pr_number: int, read: CiRead) -> None:
        self._queues[(repository, pr_number)] = []
        self._sticky[(repository, pr_number)] = read

    def read_ci_state(self, repository: str, pr_number: int,
                      head_sha: str) -> CiRead:
        self.reads.append({"repository": repository, "pr_number": pr_number,
                           "head_sha": head_sha})
        if self.fail_next is not None:
            exc = self.fail_next
            self.fail_next = None
            raise exc
        key = (repository, pr_number)
        q = self._queues.get(key)
        if q:
            read = q.pop(0)
            self._sticky[key] = read
            return read
        read = self._sticky.get(key)
        if read is not None:
            return read
        return CiRead(
            repository=repository, pr_number=pr_number,
            pr_head_sha=head_sha, base_ref=None, pr_state=PR_OPEN,
            checks=(), provider_error=None, event_version=0,
        )


def make_ci_read(
    repository: str = "MokSeinNacken/argent-development-team",
    pr_number: int = 1,
    *,
    head_sha: str = "a" * 40,
    base_ref: str = "main",
    pr_state: str = PR_OPEN,
    checks: Sequence[CiCheck] = (),
    provider_error: Optional[str] = None,
    rate_limit_reset_at: Optional[str] = None,
    event_version: int = 0,
) -> CiRead:
    """Convenience constructor for deterministic :class:`CiRead` fixtures."""
    return CiRead(
        repository=repository, pr_number=pr_number, pr_head_sha=head_sha,
        base_ref=base_ref, pr_state=pr_state, checks=tuple(checks),
        provider_error=provider_error,
        rate_limit_reset_at=rate_limit_reset_at, event_version=event_version,
    )


def make_ci_check(
    name: str,
    *,
    conclusion: Optional[str] = None,
    status: str = "COMPLETED",
    run_ref: Optional[str] = None,
    details_url: Optional[str] = None,
    check_id: int = 0,
) -> CiCheck:
    """Convenience constructor for deterministic :class:`CiCheck` fixtures."""
    return CiCheck(name=name, conclusion=conclusion, status=status,
                   run_ref=run_ref, details_url=details_url, check_id=check_id)


# ---------------------------------------------------------------------------
# Trusted CI wait spec (the ONLY authority for wait creation)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CiWaitSpec:
    """A fully resolved, TRUSTED CI wait specification (local policy output).

    Carries the full CI wait identity.  ``required_checks`` is ``None`` when
    the requirement set is UNKNOWN (conservative); an explicit empty tuple
    means "no required checks".  Nothing here can write code, approve, set
    DONE, escalate a model or read credentials — and it is never derived from
    agent prose.
    """

    provider: str
    repository: str                       # canonical owner/repo
    pr_number: int
    expected_head_sha: str
    expected_base: str
    required_checks: Optional[tuple] = None
    optional_checks: tuple = ()
    candidate_id: Optional[str] = None
    deadline_at: Optional[str] = None
    next_check_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Bounded evidence serialization
# ---------------------------------------------------------------------------

def _check_to_evidence(c: CiCheck) -> dict:
    return {
        "name": _norm_check_name(c.name),
        "conclusion": c.conclusion,
        "status": c.status,
        "run_ref": (c.run_ref or "")[:MAX_CI_RUN_REF_LEN],
        "details_url": (c.details_url or "")[:MAX_CI_RUN_REF_LEN],
    }


def dump_evidence(evidence: dict) -> str:
    """Serialize bounded CI evidence to JSON (raises on over-budget payloads).

    Defense against unbounded foreign data entering the ledger: the serialized
    payload must fit ``MAX_CI_EVIDENCE_BYTES``; otherwise the caller fails
    closed (never writes an oversized evidence blob).
    """
    text = json.dumps(evidence, sort_keys=True, separators=(",", ":"),
                      default=str)
    if len(text.encode("utf-8")) > MAX_CI_EVIDENCE_BYTES:
        raise ValueError("CI evidence exceeds the bounded budget")
    return text


def load_policy(wait: dict) -> dict:
    """Parse the trusted ``ci_policy`` JSON off a wait row (fail-closed)."""
    raw = wait.get("ci_policy")
    if not raw:
        return {"required_checks": None, "optional_checks": (),
                "expected_base": None, "candidate_id": None,
                "repository": None, "pr_number": None,
                "expected_head_sha": None}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {"required_checks": None, "optional_checks": (),
                "expected_base": None, "candidate_id": None,
                "repository": None, "pr_number": None,
                "expected_head_sha": None}
    req = data.get("required_checks")
    if req is not None and not isinstance(req, list):
        req = None
    opt = data.get("optional_checks")
    if not isinstance(opt, list):
        opt = []
    return {
        "required_checks": req,
        "optional_checks": tuple(opt),
        "expected_base": data.get("expected_base"),
        "candidate_id": data.get("candidate_id"),
        "repository": data.get("repository"),
        "pr_number": data.get("pr_number"),
        "expected_head_sha": data.get("expected_head_sha"),
    }


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CiWaitCheckResult:
    """Bounded outcome of processing one due CI wait."""

    wait_id: str
    outcome: str  # 'pending' | 'woke' | 'ignored' | 'provider_unavailable' |
                  # 'rate_limited' | 'adapter_error' | 'malformed' |
                  # 'unknown_provider'
    reason: Optional[str] = None
    job_id: Optional[str] = None
    queue_reason: Optional[str] = None
    error_class: Optional[str] = None
    aggregate_state: Optional[str] = None
    next_check_at: Optional[str] = None


class CiWaitManager:
    """Trusted, non-LLM CI wait controller (reuses the external-wait core).

    ``adapters`` is an allowlist registry ``{provider_key: adapter}``; a
    provider key absent from the registry can NEVER be entered or checked
    (fail-closed).  ``clock`` / ``jitter`` are injectable for determinism.

    Reuses :meth:`Store.transition_to_waiting_external` (atomic enter),
    :meth:`Store.complete_wait_and_requeue` (idempotent wake) and the bounded
    backoff ladder — it never reimplements the WAITING_EXTERNAL state machine.
    """

    def __init__(
        self,
        store,
        *,
        adapters: Optional[dict] = None,
        clock: Optional[Callable[[], datetime]] = None,
        jitter: Optional[Callable[[], float]] = None,
    ):
        self._store = store
        self._adapters = dict(adapters or {})
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._jitter = jitter or (lambda: 0.0)

    # -- helpers -----------------------------------------------------------

    def _now_iso(self) -> str:
        return _iso(self._clock())

    def _validate_provider(self, provider: str) -> None:
        if not isinstance(provider, str) or not provider:
            raise ValueError("provider must be a non-empty string")
        if len(provider) > MAX_PROVIDER_LEN:
            raise ValueError(f"provider exceeds {MAX_PROVIDER_LEN} chars")
        if provider not in self._adapters:
            raise ValueError(f"provider {provider!r} is not allowlisted")

    def _validate_spec(self, spec: CiWaitSpec) -> dict:
        if not isinstance(spec, CiWaitSpec):
            raise ValueError("spec must be a CiWaitSpec")
        self._validate_provider(spec.provider)
        ref = ci_ref(spec.repository, spec.pr_number)
        adapter = self._adapters[spec.provider]
        if not adapter.validate_ref(ref):
            raise ValueError(f"ref {ref!r} rejected by provider {spec.provider!r}")
        sha = spec.expected_head_sha
        if not isinstance(sha, str) or not sha.strip():
            raise ValueError("expected_head_sha must be a non-empty string")
        if len(sha) > MAX_SUBJECT_LEN:
            raise ValueError(f"expected_head_sha exceeds {MAX_SUBJECT_LEN} chars")
        base = spec.expected_base
        if not isinstance(base, str) or not base.strip():
            raise ValueError("expected_base must be a non-empty string")
        req = spec.required_checks
        if req is not None:
            if not isinstance(req, tuple):
                raise ValueError("required_checks must be a tuple or None")
            for n in req:
                if not isinstance(n, str) or not n.strip():
                    raise ValueError("required check names must be non-empty strings")
        if spec.deadline_at is not None:
            _parse_iso(spec.deadline_at)
        if spec.next_check_at is not None:
            _parse_iso(spec.next_check_at)
        return {"ref": ref}

    def _build_policy_json(self, spec: CiWaitSpec) -> str:
        policy = {
            "provider": spec.provider,
            "repository": spec.repository,
            "pr_number": spec.pr_number,
            "expected_head_sha": spec.expected_head_sha,
            "expected_base": spec.expected_base,
            "required_checks": (None if spec.required_checks is None
                                else list(spec.required_checks)),
            "optional_checks": list(spec.optional_checks),
            "candidate_id": spec.candidate_id,
        }
        return dump_evidence(policy)

    def _build_wait_row(self, job_id: str, spec: CiWaitSpec,
                        now_iso: str) -> dict:
        info = self._validate_spec(spec)
        next_check_at = spec.next_check_at
        if next_check_at is None:
            next_check_at = _iso(_parse_iso(now_iso) + timedelta(seconds=60))
        return {
            "wait_id": "wait:" + uuid4().hex,
            "job_id": job_id,
            "kind": job_state.WaitKind.CI.value,
            "provider": spec.provider,
            "ref": info["ref"],
            "expected_subject": spec.expected_head_sha,
            "last_observed_state": None,
            "next_check_at": next_check_at,
            "deadline_at": spec.deadline_at,
            "check_attempt": 0,
            "event_version": 0,
            "terminal_observed_at": None,
            "ci_policy": self._build_policy_json(spec),
            "ci_evidence": None,
            "created_at": now_iso,
            "updated_at": now_iso,
        }

    # -- trusted wait entry ------------------------------------------------

    _ACTIVE_DISPATCH_STATUSES: frozenset[str] = frozenset({
        "PENDING", "RUNNING", "RECOVERY_PENDING",
    })

    def _ensure_no_active_process(self, job_id: str) -> None:
        """HIGH-5: refuse CI wait entry while the job holds active model/process
        evidence (agent dispatch / started role-run / RUNNING process
        registration / execution scope).  Fail-closed — a job with a live model
        or process must never be silently parked in WAITING_EXTERNAL."""
        store = self._store
        job = store.get_supervisor_job(job_id)
        task_id = job["task_id"] if job is not None else None
        if task_id is not None:
            for d in store.list_dispatches(task_id):
                if d.status.value in self._ACTIVE_DISPATCH_STATUSES:
                    raise ValueError(
                        f"job {job_id!r} has an active agent dispatch "
                        f"({d.status.value}); refusing CI wait entry")
            if store.list_role_runs(task_id, status=RoleRunStatus.STARTED):
                raise ValueError(
                    f"job {job_id!r} has an active role run; "
                    f"refusing CI wait entry")
        for p in store.list_process_registrations(job_id):
            if p.get("status") == "RUNNING":
                raise ValueError(
                    f"job {job_id!r} has an active process; "
                    f"refusing CI wait entry")

    def enter_ci_wait(
        self,
        job_id: str,
        *,
        spec: CiWaitSpec,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> dict:
        """Atomically move a leased RUNNING job to WAITING_EXTERNAL for CI.

        Reuses :meth:`Store.transition_to_waiting_external` (single transaction:
        wait row + job transition + lease release; a failed transition rolls the
        whole thing back — no half-wait state).

        HIGH-5: wait entry is fail-closed — a job with active dispatch/role-run/
        process evidence is refused before the transition (no silent parking of
        a live model/process).
        """
        now_iso = self._now_iso()
        self._ensure_no_active_process(job_id)
        wait_row = self._build_wait_row(job_id, spec, now_iso)
        return self._store.transition_to_waiting_external(
            job_id,
            wait_row=wait_row,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
        )

    # -- bounded checker ---------------------------------------------------

    def check_due_ci_waits(
        self, max_items: int = 10, *, instance_id: Optional[str] = None,
    ) -> list[CiWaitCheckResult]:
        """Process only DUE ``kind='ci'`` waits (bounded, deterministic, no LLM).

        Exactly one outcome per wait; a relevant CI transition wakes the job
        exactly once (idempotent terminal flag).  An uncaught failure on one
        untrusted read never aborts the rest of the bounded pass.

        ``instance_id`` (the current singleton holder) fences every write: a
        wake/backoff is skipped when the runtime has lost the single-active
        fence (HIGH-2).  ``None`` disables the fence (unit tests / pre-acquire).
        """
        if max_items <= 0:
            return []
        now = self._now_iso()
        due = self._store.list_due_external_waits(
            now, limit=max_items, kind=job_state.WaitKind.CI.value)
        results: list[CiWaitCheckResult] = []
        for w in due:
            try:
                results.append(self._process_ci_wait(w, now,
                                                     instance_id=instance_id))
            except Exception as exc:  # noqa: BLE001 - fail-closed per wait
                results.append(self._backoff(
                    w, now, state=None, outcome="adapter_error",
                    reason=_bounded_reason(type(exc).__name__),
                    evidence=None, instance_id=instance_id))
        return results

    # -- read + normalize --------------------------------------------------

    def _validate_read(self, read: CiRead, wait: dict) -> Optional[str]:
        if not isinstance(read, CiRead):
            return "bad_read_type"
        try:
            repo, pr = parse_ci_ref(wait["ref"])
        except ValueError:
            return "bad_ref"
        # Cross-repo / cross-PR isolation: a read for Repo A can never bind to
        # a wait for Repo B (CASE 30/31).
        if read.repository != repo or read.pr_number != pr:
            return "wrong_identity"
        if read.pr_state not in PR_STATES:
            return "bad_pr_state"
        if read.provider_error is not None \
                and read.provider_error not in PROVIDER_ERRORS:
            return "bad_provider_error"
        if not isinstance(read.checks, (tuple, list)):
            return "bad_checks"
        if len(read.checks) > MAX_CI_CHECKS:
            return "too_many_checks"
        for c in read.checks:
            if not isinstance(c, CiCheck):
                return "bad_check_type"
            if c.conclusion is not None and c.conclusion not in CHECK_CONCLUSIONS:
                return "bad_check_conclusion"
            if c.status not in CHECK_STATUSES:
                return "bad_check_status"
            # LOW-6: a check that reports a terminal conclusion while still
            # QUEUED/IN_PROGRESS/PENDING is contradictory (never both running
            # and concluded) — reject fail-closed rather than aggregate SUCCESS.
            if c.conclusion is not None and c.status in (
                    "QUEUED", "IN_PROGRESS", "PENDING"):
                return "bad_check_contradictory"
            if len(c.name) > MAX_CI_CHECK_NAME_LEN:
                return "bad_check_name"
        ev = read.event_version
        if not isinstance(ev, int) or isinstance(ev, bool) or ev < 0 \
                or ev > 2 ** 31 - 1:
            return "bad_event_version"
        return None

    def _evidence(self, *, aggregate_state: Optional[str], classification: str,
                  read: Optional[CiRead], transition: str, now: str,
                  expected_head_sha: Optional[str],
                  required_checks: Optional[tuple],
                  optional_checks: tuple, observed_check_names: Optional[tuple],
                  extra: Optional[dict] = None) -> str:
        evidence = {
            "aggregate_state": aggregate_state,
            "classification": classification,
            "transition": transition,
            "observed_at": now,
            "expected_head_sha": expected_head_sha,
            "head_sha": (read.pr_head_sha if read is not None else None),
            "base_ref": (read.base_ref if read is not None else None),
            "pr_state": (read.pr_state if read is not None else None),
            "provider_error": (read.provider_error if read is not None else None),
            "rate_limit_reset_at": (read.rate_limit_reset_at if read is not None
                                    else None),
            "event_version": (read.event_version if read is not None else 0),
            "required_checks": (list(required_checks)
                                if required_checks is not None else None),
            "optional_checks": list(optional_checks),
            "observed_check_names": (list(observed_check_names)
                                     if observed_check_names is not None
                                     else None),
        }
        if extra:
            evidence.update(extra)
        return dump_evidence(evidence)

    def _snapshot(self, read: CiRead, wait: dict, policy: dict) -> CiSnapshot:
        req = policy["required_checks"]
        opt = policy["optional_checks"]
        agg = aggregate_ci_state(read.checks, required=req, optional=opt)
        # HIGH-3: derive the failing set from OBSERVED conclusions (not from
        # ``req``) so an empty required policy never masks a real failure.
        failing = failing_observed_checks(read.checks, req)
        missing = missing_required_checks(read.checks, req or ())
        cls = classify_ci_failure(agg.value, failing=failing,
                                  provider_error=read.provider_error)
        return CiSnapshot(
            read=read, aggregate_state=agg.value,
            required_checks=tuple(req or ()), optional_checks=tuple(opt or ()),
            failing_checks=failing, missing_required=missing,
            classification=cls,
        )

    # -- process one due wait ----------------------------------------------

    def _process_ci_wait(self, wait: dict, now: str,
                         *, instance_id: Optional[str] = None) -> CiWaitCheckResult:
        deadline = wait["deadline_at"]
        if deadline is not None and deadline <= now:
            return self._wake(
                wait, None, now, queue_reason=job_state.QueueReason.WAIT_DEADLINE.value,
                error_class=job_state.ErrorClass.EXTERNAL.value,
                reason="ci_deadline", state=None,
                evidence=self._evidence(
                    aggregate_state=None, classification=CI_FAIL_UNKNOWN,
                    read=None, transition="ci_deadline", now=now,
                    expected_head_sha=wait["expected_subject"],
                    required_checks=None, optional_checks=(),
                    observed_check_names=None),
                event_version=None, instance_id=instance_id)

        adapter = self._adapters.get(wait["provider"])
        if adapter is None:
            return self._backoff(wait, now, state=None,
                                 outcome="unknown_provider",
                                 reason="provider_not_allowlisted",
                                 evidence=None, instance_id=instance_id)

        policy = load_policy(wait)
        try:
            repo, pr = parse_ci_ref(wait["ref"])
        except ValueError:
            return self._backoff(wait, now, state=None, outcome="malformed",
                                 reason="bad_ref", evidence=None,
                                 instance_id=instance_id)

        try:
            read = adapter.read_ci_state(repo, pr, wait["expected_subject"])
        except BaseException as exc:
            return self._backoff(
                wait, now, state=CiState.PROVIDER_UNAVAILABLE.value,
                outcome="adapter_error",
                reason=_bounded_reason(type(exc).__name__),
                evidence=self._evidence(
                    aggregate_state=CiState.PROVIDER_UNAVAILABLE.value,
                    classification=CI_FAIL_PROVIDER, read=None,
                    transition="provider_unavailable", now=now,
                    expected_head_sha=wait["expected_subject"],
                    required_checks=policy["required_checks"],
                    optional_checks=policy["optional_checks"],
                    observed_check_names=None),
                instance_id=instance_id)

        err = self._validate_read(read, wait)
        if err is not None:
            return self._backoff(wait, now, state=None, outcome="malformed",
                                 reason=err, evidence=None,
                                 instance_id=instance_id)

        # Provider error → keep waiting, classify, bounded backoff, no LLM.
        if read.provider_error == PROVIDER_ERROR_RATE_LIMITED:
            return self._backoff_rate_limited(wait, now, read, policy,
                                              instance_id=instance_id)
        if read.provider_error in (PROVIDER_ERROR_UNAVAILABLE,
                                   PROVIDER_ERROR_NETWORK,
                                   PROVIDER_ERROR_UNKNOWN):
            return self._backoff(
                wait, now, state=CiState.PROVIDER_UNAVAILABLE.value,
                outcome="provider_unavailable",
                reason="provider_unavailable",
                evidence=self._evidence(
                    aggregate_state=CiState.PROVIDER_UNAVAILABLE.value,
                    classification=CI_FAIL_PROVIDER, read=read,
                    transition="provider_unavailable", now=now,
                    expected_head_sha=wait["expected_subject"],
                    required_checks=policy["required_checks"],
                    optional_checks=policy["optional_checks"],
                    observed_check_names=tuple(c.name for c in read.checks)),
                instance_id=instance_id)
        # ------------------------------------------------------------------
        # Positive identity binding (HIGH-1): a CI read must be POSITIVELY
        # bound to the persisted wait identity BEFORE any aggregation — head
        # SHA, base ref and an OPEN PR lifecycle.  Any missing/mismatched field
        # fails closed (bounded stale/unknown outcome, never a SUCCESS wake).
        # ------------------------------------------------------------------
        if read.pr_head_sha is None:
            return self._backoff(
                wait, now, state=CiState.UNKNOWN.value, outcome="pending",
                reason="missing_head",
                evidence=self._evidence(
                    aggregate_state=CiState.UNKNOWN.value,
                    classification=CI_FAIL_UNKNOWN, read=read,
                    transition="missing_head", now=now,
                    expected_head_sha=wait["expected_subject"],
                    required_checks=policy["required_checks"],
                    optional_checks=policy["optional_checks"],
                    observed_check_names=tuple(c.name for c in read.checks)),
                instance_id=instance_id)

        # Head-SHA binding: a wait for PR @ SHA X must never become PR @ SHA Y.
        if read.pr_head_sha != wait["expected_subject"]:
            return self._wake(
                wait, read, now,
                queue_reason=job_state.QueueReason.WAIT_EVENT.value,
                error_class=job_state.ErrorClass.EXTERNAL.value,
                reason="stale_head_change", state="STALE",
                evidence=self._evidence(
                    aggregate_state="STALE", classification=CI_FAIL_UNKNOWN,
                    read=read, transition="stale_head_change", now=now,
                    expected_head_sha=wait["expected_subject"],
                    required_checks=policy["required_checks"],
                    optional_checks=policy["optional_checks"],
                    observed_check_names=tuple(c.name for c in read.checks)),
                event_version=read.event_version, instance_id=instance_id)

        # Base-ref binding: the PR must still target the persisted expected
        # base; a base change (or a missing base) invalidates the identity.
        if read.base_ref != policy["expected_base"]:
            return self._wake(
                wait, read, now,
                queue_reason=job_state.QueueReason.WAIT_EVENT.value,
                error_class=job_state.ErrorClass.EXTERNAL.value,
                reason="base_ref_changed", state="STALE",
                evidence=self._evidence(
                    aggregate_state="STALE", classification=CI_FAIL_UNKNOWN,
                    read=read, transition="base_ref_changed", now=now,
                    expected_head_sha=wait["expected_subject"],
                    required_checks=policy["required_checks"],
                    optional_checks=policy["optional_checks"],
                    observed_check_names=tuple(c.name for c in read.checks)),
                event_version=read.event_version, instance_id=instance_id)

        # PR lifecycle must be positively OPEN for aggregation; UNKNOWN is
        # conservative (never SUCCESS from an unverifiable lifecycle).
        if read.pr_state == PR_UNKNOWN:
            return self._backoff(
                wait, now, state=CiState.UNKNOWN.value, outcome="pending",
                reason="unknown_pr_state",
                evidence=self._evidence(
                    aggregate_state=CiState.UNKNOWN.value,
                    classification=CI_FAIL_UNKNOWN, read=read,
                    transition="unknown_pr_state", now=now,
                    expected_head_sha=wait["expected_subject"],
                    required_checks=policy["required_checks"],
                    optional_checks=policy["optional_checks"],
                    observed_check_names=tuple(c.name for c in read.checks)),
                instance_id=instance_id)

        # PR lifecycle: unexpected CLOSED / MERGED wakes conservatively (merged
        # ≠ Argent-authorized merge); no new action authorization.
        if read.pr_state in (PR_CLOSED, PR_MERGED):
            transition = ("pr_closed" if read.pr_state == PR_CLOSED
                          else "pr_merged_unexpected")
            return self._wake(
                wait, read, now,
                queue_reason=job_state.QueueReason.WAIT_EVENT.value,
                error_class=job_state.ErrorClass.EXTERNAL.value,
                reason=("pr_closed" if read.pr_state == PR_CLOSED
                        else "pr_merged_unexpected"),
                state=(PR_CLOSED if read.pr_state == PR_CLOSED else PR_MERGED),
                evidence=self._evidence(
                    aggregate_state=(PR_CLOSED if read.pr_state == PR_CLOSED
                                     else PR_MERGED),
                    classification=CI_FAIL_UNKNOWN, read=read,
                    transition=transition, now=now,
                    expected_head_sha=wait["expected_subject"],
                    required_checks=policy["required_checks"],
                    optional_checks=policy["optional_checks"],
                    observed_check_names=tuple(c.name for c in read.checks)),
                event_version=read.event_version, instance_id=instance_id)

        snap = self._snapshot(read, wait, policy)

        # Required-check set materially changed (a previously-observed required
        # check vanished) → wake conservatively.
        if self._required_set_materially_changed(wait, read, policy):
            return self._wake(
                wait, read, now,
                queue_reason=job_state.QueueReason.WAIT_EVENT.value,
                error_class=job_state.ErrorClass.EXTERNAL.value,
                reason="required_check_set_changed", state=snap.aggregate_state,
                evidence=self._evidence(
                    aggregate_state=snap.aggregate_state,
                    classification=snap.classification, read=read,
                    transition="required_check_set_changed", now=now,
                    expected_head_sha=wait["expected_subject"],
                    required_checks=policy["required_checks"],
                    optional_checks=policy["optional_checks"],
                    observed_check_names=tuple(c.name for c in read.checks)),
                event_version=read.event_version, instance_id=instance_id)

        # Terminal aggregate → persist evidence FIRST, then wake exactly once.
        if snap.aggregate_state in CI_TERMINAL_STATES:
            return self._wake(
                wait, read, now,
                queue_reason=job_state.QueueReason.WAIT_EVENT.value,
                error_class=self._error_class_for(snap.aggregate_state),
                reason=f"ci_{snap.aggregate_state.lower()}",
                state=snap.aggregate_state,
                evidence=self._evidence(
                    aggregate_state=snap.aggregate_state,
                    classification=snap.classification, read=read,
                    transition=f"ci_{snap.aggregate_state.lower()}", now=now,
                    expected_head_sha=wait["expected_subject"],
                    required_checks=policy["required_checks"],
                    optional_checks=policy["optional_checks"],
                    observed_check_names=tuple(c.name for c in read.checks),
                    extra={
                        "failing_checks": [
                            _check_to_evidence(c) for c in snap.failing_checks],
                        "missing_required": list(snap.missing_required),
                    }),
                event_version=read.event_version, instance_id=instance_id)

        # Pending / unknown / no-checks / neutral / skipped → backoff, no wake.
        reason = "pending"
        if snap.aggregate_state == CiState.NO_CHECKS_CONFIGURED.value:
            reason = "no_checks_configured"
        elif snap.aggregate_state == CiState.UNKNOWN.value:
            reason = "unknown"
        return self._backoff(
            wait, now, state=snap.aggregate_state, outcome="pending",
            reason=reason,
            evidence=self._evidence(
                aggregate_state=snap.aggregate_state,
                classification=snap.classification, read=read,
                transition=f"ci_{snap.aggregate_state.lower()}", now=now,
                expected_head_sha=wait["expected_subject"],
                required_checks=policy["required_checks"],
                optional_checks=policy["optional_checks"],
                observed_check_names=tuple(c.name for c in read.checks)),
            instance_id=instance_id)

    def _error_class_for(self, aggregate_state: str) -> str:
        if aggregate_state == CiState.SUCCESS.value:
            return job_state.ErrorClass.NONE.value
        if aggregate_state == CiState.ACTION_REQUIRED.value:
            return job_state.ErrorClass.OWNER_REQUIRED.value
        return job_state.ErrorClass.EXTERNAL.value

    def _required_set_materially_changed(self, wait, read, policy) -> bool:
        req = policy["required_checks"]
        if not req:
            return False
        req_set = set(req)
        observed = {c.name for c in read.checks}
        prior = self._prior_observed_check_names(wait)
        if prior is None:
            return False
        # A required check that was observed before and is now absent.
        return bool((prior & req_set) - observed)

    def _prior_observed_check_names(self, wait) -> Optional[set]:
        raw = wait.get("ci_evidence")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        names = data.get("observed_check_names")
        if not isinstance(names, list):
            return None
        return {n for n in names if isinstance(n, str)}

    # -- persistence helpers -----------------------------------------------

    def _backoff(self, wait, now, *, state, outcome, reason,
                 evidence: Optional[str],
                 instance_id: Optional[str] = None) -> CiWaitCheckResult:
        attempt = wait["check_attempt"] + 1
        delay = next_check_delay_seconds(attempt, jitter=self._jitter())
        next_check_at = _iso(_parse_iso(now) + timedelta(seconds=delay))
        updates = {
            "check_attempt": attempt,
            "next_check_at": next_check_at,
            "updated_at": now,
        }
        if state is not None:
            updates["last_observed_state"] = state
        if evidence is not None:
            updates["ci_evidence"] = evidence
        # HIGH-2(b): fenced + terminal-immutable write — a late response can
        # never mutate a terminal wait row; a stale instance is skipped.
        self._store.update_external_wait_fenced(
            wait["wait_id"], expected_instance_id=instance_id, **updates)
        return CiWaitCheckResult(
            wait_id=wait["wait_id"], outcome=outcome, reason=reason,
            job_id=wait["job_id"], next_check_at=next_check_at,
            aggregate_state=state,
        )

    def _backoff_rate_limited(self, wait, now, read, policy,
                              *, instance_id: Optional[str] = None) -> CiWaitCheckResult:
        # Rate limit: keep waiting, respect reset/eligible time when observable
        # (bounded backoff otherwise); never a Writer failure, never an LLM.
        attempt = wait["check_attempt"] + 1
        delay = next_check_delay_seconds(attempt, jitter=self._jitter())
        reset_at = read.rate_limit_reset_at
        if reset_at is not None:
            try:
                delay = max(delay, int((_parse_iso(reset_at) -
                                        _parse_iso(now)).total_seconds()))
            except (ValueError, TypeError):
                pass
        next_check_at = _iso(_parse_iso(now) + timedelta(seconds=delay))
        evidence = self._evidence(
            aggregate_state=CiState.RATE_LIMITED.value,
            classification=CI_FAIL_PROVIDER, read=read,
            transition="rate_limited", now=now,
            expected_head_sha=wait["expected_subject"],
            required_checks=policy["required_checks"],
            optional_checks=policy["optional_checks"],
            observed_check_names=tuple(c.name for c in read.checks))
        self._store.update_external_wait_fenced(
            wait["wait_id"], expected_instance_id=instance_id,
            check_attempt=attempt, next_check_at=next_check_at,
            updated_at=now, last_observed_state=CiState.RATE_LIMITED.value,
            ci_evidence=evidence)
        return CiWaitCheckResult(
            wait_id=wait["wait_id"], outcome="rate_limited",
            reason="rate_limited", job_id=wait["job_id"],
            next_check_at=next_check_at,
            aggregate_state=CiState.RATE_LIMITED.value,
        )

    def _wake(self, wait, read, now, *, queue_reason, error_class, reason,
              state, evidence, event_version,
              instance_id: Optional[str] = None) -> CiWaitCheckResult:
        updated = self._store.complete_wait_and_requeue(
            wait["wait_id"],
            queue_reason=queue_reason,
            error_class=error_class,
            observed_state=state,
            event_version=(event_version if event_version is not None
                           else wait["event_version"]),
            ci_evidence=evidence,
            expected_instance_id=instance_id,
            now_iso=now,
        )
        if updated is None:
            # Already terminal / already left WAITING_EXTERNAL (dedup).
            return CiWaitCheckResult(
                wait_id=wait["wait_id"], outcome="ignored",
                reason="already_handled", job_id=wait["job_id"],
            )
        return CiWaitCheckResult(
            wait_id=wait["wait_id"], outcome="woke", reason=reason,
            job_id=wait["job_id"], queue_reason=queue_reason,
            error_class=error_class, aggregate_state=state,
        )


# ---------------------------------------------------------------------------
# GitHub CI adapter (READ-ONLY; no write path)
# ---------------------------------------------------------------------------

class GitHubCiAdapter:
    """Real GitHub CI adapter (argv ``gh`` subprocesses; NO write path).

    Reads PR state + check-runs + commit status via ``gh`` and normalizes them
    into :class:`CiRead`.  Provider failures are classified with the existing
    :func:`~argent_core.github_provider_adapter.classify_gh_failure` taxonomy
    (outage/rate-limit/network/credential) — never leaked as raw token text.
    Mutation methods are structurally absent: this adapter has NO write path in
    I3-C1 (CASE 44).
    """

    provider_name = "github"
    write_enabled = False  # READ-ONLY: no mutation path exists in I3-C1

    def __init__(self, *, gh_executable: str = "gh",
                 run=__import__("subprocess").run, env=None, cwd=None):
        self.gh_executable = gh_executable
        self._run = run
        self._env = env
        self._cwd = cwd
        self.invocations: list = []

    def validate_ref(self, ref: str) -> bool:
        try:
            parse_ci_ref(ref)
            return True
        except ValueError:
            return False

    def _exec(self, argv) -> "subprocess.CompletedProcess":
        import subprocess as _sp

        from .github_provider_adapter import classify_gh_failure
        from .external_provider_adapter import (
            ProviderNetworkError, ProviderRateLimited,
        )
        self.invocations.append(list(argv))
        env = None
        if self._env:
            env = dict(_sp.os.environ)
            env.update(self._env)
        try:
            proc = self._run(argv, capture_output=True, text=True, env=env,
                             cwd=self._cwd, timeout=60)
        except FileNotFoundError as exc:
            raise ProviderNetworkError("gh executable not found") from exc
        except _sp.TimeoutExpired as exc:
            raise ProviderNetworkError("gh timed out") from exc
        except OSError as exc:  # noqa: BLE001
            raise ProviderNetworkError(
                f"transport failure: {type(exc).__name__}") from exc
        if proc.returncode != 0:
            raise classify_gh_failure(proc.returncode, proc.stderr or "")
        return proc

    def _json(self, proc, default=None):
        """Parse a provider success response body (LOW-6, fail-closed).

        Returns the parsed JSON value, or ``None`` when the body is empty or
        malformed (an empty/malformed success response is never silently
        treated as valid empty data — callers translate ``None`` to
        ``provider_error``).
        """
        import json as _json
        raw = (proc.stdout or "").strip()
        if not raw:
            return None
        try:
            return _json.loads(raw)
        except (ValueError, TypeError):
            return None

    def read_ci_state(self, repository, pr_number, head_sha) -> CiRead:
        from .external_provider_adapter import (
            ProviderRateLimited, ProviderUnavailable,
        )

        # 1) PR state (current head/base/state) — reveals head movement.
        pr_state = PR_UNKNOWN
        pr_head_sha: Optional[str] = None
        base_ref: Optional[str] = None
        checks: list = []
        event_version = 0
        provider_error: Optional[str] = None
        rate_limit_reset_at: Optional[str] = None

        try:
            proc = self._exec([
                self.gh_executable, "pr", "view", str(pr_number),
                "--repo", repository,
                "--json", "number,state,headRefOid,baseRefName,mergedAt,closedAt",
            ])
            data = self._json(proc, {})
            if not isinstance(data, dict):
                # LOW-6: malformed/non-dict/empty-when-expecting-data ⇒
                # provider_error (fail-closed), never a silent empty read.
                provider_error = PROVIDER_ERROR_UNKNOWN
            else:
                pr_head_sha = data.get("headRefOid")
                base_ref = data.get("baseRefName")
                st = data.get("state")
                if data.get("mergedAt"):
                    pr_state = PR_MERGED
                elif st == "OPEN":
                    pr_state = PR_OPEN
                elif st == "CLOSED":
                    pr_state = PR_CLOSED
                elif st == "MERGED":
                    pr_state = PR_MERGED
                else:
                    pr_state = PR_UNKNOWN
        except ProviderRateLimited as exc:
            provider_error = PROVIDER_ERROR_RATE_LIMITED
            rate_limit_reset_at = self._extract_reset(exc)
        except ProviderUnavailable as exc:
            provider_error = PROVIDER_ERROR_UNAVAILABLE
        except Exception as exc:  # noqa: BLE001
            provider_error = PROVIDER_ERROR_UNKNOWN

        if provider_error is None:
            # 2) Check-runs for the BOUND head SHA.
            try:
                proc = self._exec([
                    self.gh_executable, "api",
                    f"repos/{repository}/commits/{head_sha}/check-runs",
                ])
                data = self._json(proc, {})
                runs = data.get("check_runs") if isinstance(data, dict) else None
                if not isinstance(runs, list):
                    # LOW-6: malformed/non-dict/empty check-runs response ⇒
                    # provider_error (fail-closed), never a fabricated no-check.
                    provider_error = PROVIDER_ERROR_UNKNOWN
                else:
                    checks, event_version = normalize_check_runs(runs)
            except ProviderRateLimited as exc:
                provider_error = PROVIDER_ERROR_RATE_LIMITED
                rate_limit_reset_at = self._extract_reset(exc)
            except ProviderUnavailable as exc:
                provider_error = PROVIDER_ERROR_UNAVAILABLE
            except Exception as exc:  # noqa: BLE001
                provider_error = PROVIDER_ERROR_UNKNOWN

        if provider_error is None:
            # 3) Commit status contexts (advisory).
            try:
                proc = self._exec([
                    self.gh_executable, "api",
                    f"repos/{repository}/commits/{head_sha}/status",
                ])
                data = self._json(proc, {})
                statuses = data.get("statuses") if isinstance(data, dict) else None
                if not isinstance(statuses, list):
                    provider_error = PROVIDER_ERROR_UNKNOWN
                else:
                    extra = normalize_statuses(statuses)
                    checks = _merge_checks(checks, extra)
                    event_version = max(event_version,
                                        max((c.check_id for c in extra),
                                            default=0))
            except ProviderRateLimited as exc:
                provider_error = PROVIDER_ERROR_RATE_LIMITED
                rate_limit_reset_at = self._extract_reset(exc)
            except ProviderUnavailable as exc:
                provider_error = PROVIDER_ERROR_UNAVAILABLE
            except Exception as exc:  # noqa: BLE001
                provider_error = PROVIDER_ERROR_UNKNOWN

        return CiRead(
            repository=repository, pr_number=pr_number,
            pr_head_sha=pr_head_sha, base_ref=base_ref, pr_state=pr_state,
            checks=tuple(checks), provider_error=provider_error,
            rate_limit_reset_at=rate_limit_reset_at,
            event_version=event_version,
        )

    @staticmethod
    def _extract_reset(exc) -> Optional[str]:
        # Best-effort: parse a rate-limit reset timestamp from the error text
        # (the provider error never leaks tokens; reset metadata is advisory).
        import re
        text = str(exc)
        m = re.search(r"resets? (?:in|at)\s+([0-9T:+\-Z]+)", text)
        return m.group(1) if m else None


def normalize_check_runs(runs: list) -> tuple[list, int]:
    """Normalize GitHub check-runs into bounded :class:`CiCheck` objects.

    Returns ``(checks, event_version)`` where ``event_version`` is the max
    check-run ``id`` (monotonic provider transition identity).  Untrusted
    fields are bounded and mapped to the closed conclusion/status sets.
    """
    checks: list = []
    event_version = 0
    for r in runs[:MAX_CI_CHECKS]:
        if not isinstance(r, dict):
            continue
        name = _norm_check_name(r.get("name"))
        if not name:
            continue
        status = _norm_status(r.get("status"))
        conclusion = _norm_conclusion(r.get("conclusion"))
        check_id = r.get("id")
        if not isinstance(check_id, int) or isinstance(check_id, bool) \
                or check_id < 0:
            check_id = 0
        event_version = max(event_version, check_id)
        checks.append(CiCheck(
            name=name, conclusion=conclusion, status=status,
            run_ref=(str(r.get("html_url") or r.get("details_url") or "")
                     [:MAX_CI_RUN_REF_LEN] or None),
            details_url=(str(r.get("details_url") or r.get("html_url") or "")
                         [:MAX_CI_RUN_REF_LEN] or None),
            check_id=check_id,
        ))
    return checks, event_version


def normalize_statuses(statuses: list) -> list:
    """Normalize GitHub commit-status contexts into bounded :class:`CiCheck`."""
    checks: list = []
    for s in statuses[:MAX_CI_CHECKS]:
        if not isinstance(s, dict):
            continue
        name = _norm_check_name(s.get("context") or s.get("name"))
        if not name:
            continue
        state = s.get("state")
        conclusion = _status_state_to_conclusion(state)
        checks.append(CiCheck(
            name=name, conclusion=conclusion,
            status="COMPLETED" if conclusion is not None else "PENDING",
            run_ref=(str(s.get("target_url") or "")[:MAX_CI_RUN_REF_LEN]
                     or None),
            details_url=(str(s.get("target_url") or "")[:MAX_CI_RUN_REF_LEN]
                         or None),
            check_id=0,
        ))
    return checks


def _norm_status(status) -> str:
    if not isinstance(status, str):
        return "UNKNOWN"
    s = status.strip().upper()
    mapping = {
        "QUEUED": "QUEUED", "IN_PROGRESS": "IN_PROGRESS",
        "COMPLETED": "COMPLETED", "PENDING": "PENDING",
    }
    return mapping.get(s, "UNKNOWN")


def _norm_conclusion(conclusion) -> Optional[str]:
    if conclusion is None or not isinstance(conclusion, str):
        return None
    c = conclusion.strip().upper()
    if c in CHECK_CONCLUSIONS:
        return c
    return None


def _status_state_to_conclusion(state) -> Optional[str]:
    if not isinstance(state, str):
        return None
    s = state.strip().lower()
    mapping = {
        "success": "SUCCESS", "failure": "FAILURE", "error": "FAILURE",
        "pending": None,
    }
    return mapping.get(s, "UNKNOWN" if s not in ("pending",) else None)


def _merge_checks(existing: list, extra: list) -> list:
    """Merge status contexts into check-runs (dedup by name, check-runs win)."""
    by_name = {c.name: c for c in existing}
    for c in extra:
        if c.name not in by_name:
            by_name[c.name] = c
            existing.append(c)
    return existing
