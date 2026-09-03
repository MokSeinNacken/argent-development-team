"""Phase I3-A — External Action Broker core (provider-neutral write boundary).

This is the single authority that turns a *validated* I2 integration result
into a bounded, deterministic external write request, evaluates its policy,
and drives it through a durable, fenced lifecycle against a provider adapter.
It is the trust boundary between "code we have authoritatively integrated"
and "the outside world".

Provider-neutral design: the broker knows nothing about GitHub beyond the
closed :class:`ActionTaxonomy` / action registry and the generic
``provider``/``account``/``repository``/``resource`` fields; a specific provider
is reached only through :class:`~argent_core.external_provider_adapter.ExternalProviderAdapter`.
In I3-A there is **no real provider write path** — every mutation runs against a
fixture adapter or an explicit no-write mode (CASE 50).

Hard guarantees (code-enforced, tested):

* **Controller-authoritative**: a request is created ONLY from trusted store
  facts (a valid I2 integration result).  Agent prose is never sufficient
  (CASE 11–15).
* **Deterministic policy**: :class:`PolicyEngine` is a pure function over the
  request + allowlist + standing policy.  No LLM in any decision.  Unknown
  provider/action/repository → DENY (no string-prefix authz).
* **Bounded states**: the request lifecycle is a SEPARATE 8-state machine
  (PENDING/AUTHORIZED/EXECUTING/WAITING_EXTERNAL/SUCCEEDED/FAILED/BLOCKED/
  DENIED) — NOT new job states; the 8-state ``job_state.PrimaryState`` model is
  untouched.  Terminal states are clearly defined and nothing disappears on
  restart.
* **Fenced**: every transition is a revision CAS; the authoritative
  transitions re-verify the holder's live job lease AND the named action lock
  atomically (a stale holder can never finalize).
* **Idempotent / reconcilable**: at-most-one logical Argent action (unique
  idempotency key); provider reconciliation via provider-visible state
  (push: remote ref == expected SHA; create PR: existing Argent-owned PR for
  the same head).  Never claims exactly-once beyond provider semantics.
* **Secret-free audit**: REQUESTED/AUTHORIZED/EXECUTED/RECONCILED events with a
  bounded failure-class taxonomy; no secret is ever logged.
* **Publication safety**: PR title/body are bounded and secret-redacted; no
  system prompts / credentials can be smuggled into a publication.
* **External-wait integration**: WAITING_EXTERNAL semantics with
  ``next_check_at``/attempt metadata; no LLM occupies a slot while waiting.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Dict, Optional, Tuple

from .external_provider_adapter import (
    ALLOWED_OUTCOMES,
    OUTCOME_CONFLICT,
    OUTCOME_CREDENTIAL_ERROR,
    OUTCOME_RATE_LIMITED,
    OUTCOME_SUCCESS,
    OUTCOME_UNAVAILABLE,
    OUTCOME_VALIDATION_FAILED,
    OUTCOME_WAITING,
    NoWriteExternalProviderAdapter,
    ProviderConflict,
    ProviderCredentialError,
    ProviderObservation,
    ProviderRateLimited,
    ProviderResult,
    ProviderUnavailable,
    ProviderValidationError,
    ProviderWriteDisabled,
)
from .external_wait import next_check_delay_seconds
from .models import LeaseFencedError
from .worktree import is_sha_like, validate_branch_identity, validate_repo_identity

# ---------------------------------------------------------------------------
# Bounded constants
# ---------------------------------------------------------------------------

REQUEST_ID_PREFIX = "xr_"
REQUEST_ID_HEX = 24
MAX_PROVIDER_LEN = 64
MAX_ACCOUNT_LEN = 128
MAX_REPO_LEN = 512
MAX_RESOURCE_REF_LEN = 256
MAX_SCOPE_LEN = 256
MAX_PARAMETERS_JSON_BYTES = 64 * 1024
MAX_PRECONDITIONS_JSON_BYTES = 64 * 1024
MAX_PROVIDER_STATE_JSON_BYTES = 64 * 1024
MAX_TITLE_LEN = 256
MAX_BODY_LEN = 64 * 1024
MAX_REASON_CODE_LEN = 64
MAX_AUDIT_DETAIL_LEN = 512

#: Bounded retry budget — no retry storms (mirrors external_wait's ladder).
MAX_RETRY_ATTEMPTS = 8
BACKOFF_BASE_MINUTES = (1, 2, 5, 10, 30)

#: Upper bound for a request expiry TTL (fail-closed on misconfiguration).
MAX_EXPIRY_TTL_SECONDS = 7 * 24 * 3600

#: Provenance version supported by this broker (I3-A contract).
PROVENANCE_VERSION = 1


# ---------------------------------------------------------------------------
# Taxonomy (3 classes) + closed action registry (GitHub-oriented initial set)
# ---------------------------------------------------------------------------

class ActionTaxonomy(str, Enum):
    """The three bounded action classes (brief §6)."""

    READ = "READ"
    BOUNDED_WRITE = "BOUNDED_WRITE"
    SENSITIVE = "SENSITIVE"


#: Closed action registry: action name -> taxonomy class.  Unknown names are
#: DENY (never a string-prefix match, never a fallback).
ACTIONS: Dict[str, ActionTaxonomy] = {
    # READ
    "read_repository": ActionTaxonomy.READ,
    "read_ref": ActionTaxonomy.READ,
    "read_pull_request": ActionTaxonomy.READ,
    "read_checks": ActionTaxonomy.READ,
    # BOUNDED_WRITE
    "push_feature_branch": ActionTaxonomy.BOUNDED_WRITE,
    "create_pull_request": ActionTaxonomy.BOUNDED_WRITE,
    "update_pull_request": ActionTaxonomy.BOUNDED_WRITE,
    # SENSITIVE (merge/release/deploy -> OWNER_GATE_REQUIRED, CASE 48/49)
    "merge_pull_request": ActionTaxonomy.SENSITIVE,
    "create_release": ActionTaxonomy.SENSITIVE,
    "deploy_production": ActionTaxonomy.SENSITIVE,
}

#: Actions that are provider MUTATIONS (structurally disabled in I3-A).
MUTATION_ACTIONS: frozenset[str] = frozenset({
    "push_feature_branch", "create_pull_request", "update_pull_request",
    "merge_pull_request", "create_release", "deploy_production",
})

#: Actions that are provider READS (side-effect free).
READ_ACTIONS: frozenset[str] = frozenset({
    "read_repository", "read_ref", "read_pull_request", "read_checks",
})


def action_taxonomy(action: str) -> Optional[ActionTaxonomy]:
    """Deterministic taxonomy lookup (``None`` => unknown => DENY)."""
    return ACTIONS.get(action)


# ---------------------------------------------------------------------------
# Request lifecycle (broker states only — NOT job states)
# ---------------------------------------------------------------------------

class RequestState(str, Enum):
    PENDING = "PENDING"
    AUTHORIZED = "AUTHORIZED"
    EXECUTING = "EXECUTING"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    DENIED = "DENIED"


REQUEST_STATE_VALUES: Tuple[str, ...] = tuple(s.value for s in RequestState)

#: Terminal broker states (no further automatic transition; nothing disappears).
TERMINAL_REQUEST_STATES: frozenset = frozenset({
    RequestState.SUCCEEDED, RequestState.FAILED,
    RequestState.BLOCKED, RequestState.DENIED,
})

#: States from which an (authorized) execution may be re-driven after a
#: bounded retry backoff.
RETRYABLE_STATES: frozenset = frozenset({
    RequestState.EXECUTING, RequestState.WAITING_EXTERNAL,
})

#: Closed request-state edge map (I3-A HIGH-4).  Terminal states have NO
#: outgoing edges — a terminal request can never be reopened.  The store's
#: ``transition_external_action_request(_authoritative)`` enforces this map.
REQUEST_TRANSITIONS: Dict[str, frozenset] = {
    RequestState.PENDING.value: frozenset({
        RequestState.AUTHORIZED.value, RequestState.DENIED.value,
        RequestState.BLOCKED.value,
    }),
    RequestState.AUTHORIZED.value: frozenset({
        RequestState.EXECUTING.value, RequestState.DENIED.value,
        RequestState.BLOCKED.value,
    }),
    RequestState.EXECUTING.value: frozenset({
        RequestState.WAITING_EXTERNAL.value, RequestState.SUCCEEDED.value,
        RequestState.FAILED.value, RequestState.BLOCKED.value,
    }),
    RequestState.WAITING_EXTERNAL.value: frozenset({
        RequestState.EXECUTING.value, RequestState.SUCCEEDED.value,
        RequestState.FAILED.value, RequestState.BLOCKED.value,
    }),
    RequestState.SUCCEEDED.value: frozenset(),
    RequestState.FAILED.value: frozenset(),
    RequestState.BLOCKED.value: frozenset(),
    RequestState.DENIED.value: frozenset(),
}


# ---------------------------------------------------------------------------
# Policy decisions + reason codes (closed sets)
# ---------------------------------------------------------------------------

class PolicyDecision(str, Enum):
    ALLOW_AUTONOMOUS = "ALLOW_AUTONOMOUS"
    OWNER_GATE_REQUIRED = "OWNER_GATE_REQUIRED"
    DENY = "DENY"
    DEFER = "DEFER"


#: Bounded reason codes (closed set).
RC_ALLOWED = "ALLOWED"
RC_UNKNOWN_PROVIDER = "UNKNOWN_PROVIDER"
RC_UNKNOWN_ACTION = "UNKNOWN_ACTION"
RC_UNKNOWN_REPO = "UNKNOWN_REPO"
RC_UNKNOWN_ACCOUNT = "UNKNOWN_ACCOUNT"
RC_NOT_ALLOWLISTED = "NOT_ALLOWLISTED"
RC_CLASS_NOT_PERMITTED = "CLASS_NOT_PERMITTED"
RC_SENSITIVE = "SENSITIVE_ACTION"
RC_PROTECTED_REF = "PROTECTED_REF"
RC_BRANCH_NOT_IN_NAMESPACE = "BRANCH_NOT_IN_NAMESPACE"
RC_MISSING_PROVENANCE = "MISSING_PROVENANCE"
RC_EXPIRED = "EXPIRED"
RC_DEFERRED = "DEFERRED_PRECONDITION"
RC_NOT_AUTONOMOUS = "NOT_AUTONOMOUS"
RC_IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"

ALL_REASON_CODES: frozenset[str] = frozenset({
    RC_ALLOWED, RC_UNKNOWN_PROVIDER, RC_UNKNOWN_ACTION, RC_UNKNOWN_REPO,
    RC_UNKNOWN_ACCOUNT, RC_NOT_ALLOWLISTED, RC_CLASS_NOT_PERMITTED,
    RC_SENSITIVE, RC_PROTECTED_REF, RC_BRANCH_NOT_IN_NAMESPACE,
    RC_MISSING_PROVENANCE, RC_EXPIRED, RC_DEFERRED,
    RC_NOT_AUTONOMOUS, RC_IDEMPOTENCY_CONFLICT,
})


# ---------------------------------------------------------------------------
# Failure classes (audit; provider outage != code failure, rate limit != model)
# ---------------------------------------------------------------------------

FAILURE_AUTHORIZATION = "AUTHORIZATION"
FAILURE_POLICY_DENIED = "POLICY_DENIED"
FAILURE_RATE_LIMIT = "RATE_LIMIT"
FAILURE_PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
FAILURE_NETWORK = "NETWORK"
FAILURE_PRECONDITION_FAILED = "PRECONDITION_FAILED"
FAILURE_CONFLICT = "CONFLICT"
FAILURE_CREDENTIAL = "CREDENTIAL"
FAILURE_REMOTE_VALIDATION = "REMOTE_VALIDATION"
FAILURE_LOCAL_CODE_ERROR = "LOCAL_CODE_ERROR"
FAILURE_UNKNOWN = "UNKNOWN"

ALL_FAILURE_CLASSES: frozenset[str] = frozenset({
    FAILURE_AUTHORIZATION, FAILURE_POLICY_DENIED, FAILURE_RATE_LIMIT,
    FAILURE_PROVIDER_UNAVAILABLE, FAILURE_NETWORK, FAILURE_PRECONDITION_FAILED,
    FAILURE_CONFLICT, FAILURE_CREDENTIAL, FAILURE_REMOTE_VALIDATION,
    FAILURE_LOCAL_CODE_ERROR, FAILURE_UNKNOWN,
})

#: Map a provider outcome to a bounded failure class (UNTRUSTED outcome →
#: closed set; anything unmapped is UNKNOWN).
_OUTCOME_TO_FAILURE_CLASS: Dict[str, str] = {
    OUTCOME_RATE_LIMITED: FAILURE_RATE_LIMIT,
    OUTCOME_UNAVAILABLE: FAILURE_PROVIDER_UNAVAILABLE,
    OUTCOME_CONFLICT: FAILURE_CONFLICT,
    OUTCOME_VALIDATION_FAILED: FAILURE_REMOTE_VALIDATION,
    OUTCOME_CREDENTIAL_ERROR: FAILURE_CREDENTIAL,
}


# ---------------------------------------------------------------------------
# Audit event types (closed set)
# ---------------------------------------------------------------------------

AUDIT_REQUESTED = "REQUESTED"
AUDIT_AUTHORIZED = "AUTHORIZED"
AUDIT_EXECUTED = "EXECUTED"
AUDIT_RECONCILED = "RECONCILED"
ALL_AUDIT_EVENT_TYPES: frozenset[str] = frozenset({
    AUDIT_REQUESTED, AUDIT_AUTHORIZED, AUDIT_EXECUTED, AUDIT_RECONCILED,
})


# ---------------------------------------------------------------------------
# Branch safety model
# ---------------------------------------------------------------------------

#: Protected refs NEVER eligible for autonomous writes (push/merge/…).
PROTECTED_REF_EXACT: frozenset[str] = frozenset({"main", "master", "stable"})
PROTECTED_REF_PREFIXES: Tuple[str, ...] = ("release", "production")

#: Autonomous feature-branch namespace (validated; CASE: pushes eventually
#: restricted to ``argent/<task-id>-<slug>``).
AUTONOMOUS_BRANCH_PREFIX = "argent/"


def _strip_ref_prefix(ref: str) -> str:
    """Strip ``refs/heads/`` / ``refs/tags/`` for the safety check."""
    for p in ("refs/heads/", "refs/tags/"):
        if ref.startswith(p):
            return ref[len(p):]
    return ref


def is_protected_ref(ref: str) -> bool:
    """True iff ``ref`` is a protected ref (main/master/stable/release*/production*).

    Fail-closed on non-string input (returns True so a malformed ref can never
    be treated as an autonomous target)."""
    if not isinstance(ref, str):
        return True
    name = _strip_ref_prefix(ref)
    if name in PROTECTED_REF_EXACT:
        return True
    low = name.lower()
    return any(low.startswith(p) for p in PROTECTED_REF_PREFIXES)


def autonomous_branch_ok(branch: str, task_id: str,
                        namespaces=("argent/",)) -> bool:
    """True iff ``branch`` is a valid autonomous feature-branch for ``task_id``.

    Requires a validated namespace ``<ns><task-id>-<slug>`` within one of
    ``namespaces`` (default ``argent/``) — never a protected ref, never an
    arbitrary branch.  A missing/unsafe ``task_id`` or a branch outside every
    namespace returns False (fail-closed).
    """
    if not isinstance(branch, str) or not isinstance(task_id, str):
        return False
    try:
        validate_branch_identity(branch)
    except ValueError:
        return False
    if is_protected_ref(branch):
        return False
    for ns in namespaces:
        if not isinstance(ns, str) or not ns:
            continue
        if branch.startswith(ns):
            # Must be ``<ns><task-id>-<slug>`` — the segment after the
            # namespace starts with the exact ``task_id`` + ``-`` separator.
            rest = branch[len(ns):]
            if rest and rest.startswith(task_id + "-"):
                return True
    return False


# ---------------------------------------------------------------------------
# Publication safety (bounded/sanitized PR title/body)
# ---------------------------------------------------------------------------

#: Secret-like token patterns (best-effort redaction of credential material —
#: a title that matches is REJECTED; a body that matches is redacted).  Never a
#: security boundary by itself — the authoritative guard is that the broker
#: never logs/secrets and the sandbox masks credentials.
_SECRET_PATTERNS: Tuple[re.Pattern, ...] = tuple(re.compile(p) for p in (
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",          # GitHub tokens
    r"github_pat_[A-Za-z0-9_]{20,}",            # fine-grained PAT
    r"\bAKIA[0-9A-Z]{16}\b",                    # AWS access key
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",      # PEM private keys
    r"\bsk-[A-Za-z0-9]{20,}\b",                 # OpenAI-style keys
))

#: Prompt-injection markers that a publication body must never carry (bounded
#: heuristic; the authoritative boundary is provenance, not this list).
_PROMPT_INJECTION_MARKERS: Tuple[str, ...] = (
    "ignore previous instructions",
    "ignore all previous",
    "system prompt",
    "you are now",
)

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _has_secret_like(text: str) -> bool:
    return any(p.search(text) for p in _SECRET_PATTERNS)


def _redact_secrets(text: str) -> str:
    for p in _SECRET_PATTERNS:
        text = p.sub("[REDACTED]", text)
    return text


#: Additional token-like markers that a provider detail must never carry
#: verbatim (best-effort; the authoritative guard is that the broker never
#: logs secrets and the sandbox masks credentials).
_TOKEN_MARKERS: Tuple[re.Pattern, ...] = tuple(re.compile(p) for p in (
    r"\bgh[pousr]_[A-Za-z0-9]{16,}\b",
    r"\btoken[=: ]+[A-Za-z0-9_-]{16,}",
    r"\bBearer\s+[A-Za-z0-9._-]{16,}",
))


def sanitize_provider_detail(detail) -> str:
    """Bounded, secret-redacted provider detail (I3-A HIGH-7).

    Provider ``detail`` is UNTRUSTED DATA.  Before ANY persistence (request
    ``last_error_code``, audit ``reason_code``) it is: coerced to ``str``,
    stripped of control characters, secret/token markers redacted, and
    truncated to :data:`MAX_PROVIDER_DETAIL_LEN`.  A secret-like token (ghp_/
    gho_/ghs_/token/Bearer) can never reach a request or audit row.
    """
    from .external_provider_adapter import MAX_PROVIDER_DETAIL_LEN

    if detail is None:
        return ""
    if not isinstance(detail, str):
        detail = str(detail)
    detail = _CONTROL_CHARS_RE.sub("", detail)
    detail = _redact_secrets(detail)
    for p in _TOKEN_MARKERS:
        detail = p.sub("[REDACTED]", detail)
    return detail[:MAX_PROVIDER_DETAIL_LEN]


def sanitize_publication_text(text, *, field: str, max_len: int,
                              reject_on_secret: bool = False) -> str:
    """Bounded, secret-safe publication text (CASE: bounded/sanitized PR).

    Strips control characters, truncates to ``max_len``, and either REJECTS
    (``reject_on_secret=True``, used for the title) or REDACTS secret-like
    content (body).  A non-string is coerced; an empty result is allowed only
    for the body.
    """
    if not isinstance(text, str):
        text = str(text)
    text = _CONTROL_CHARS_RE.sub("", text)
    if reject_on_secret and _has_secret_like(text):
        raise ValueError(f"{field} contains secret-like content (rejected)")
    text = _redact_secrets(text)
    return text[:max_len]


def validate_pr_title(title: str) -> str:
    """Validate a PR title (bounded, non-empty, no secrets)."""
    if not isinstance(title, str) or not title.strip():
        raise ValueError("PR title must be a non-empty string")
    title = sanitize_publication_text(
        title, field="PR title", max_len=MAX_TITLE_LEN, reject_on_secret=True)
    if not title.strip():
        raise ValueError("PR title must be non-empty after sanitization")
    return title


def validate_pr_body(body: Optional[str]) -> str:
    """Validate a PR body (bounded, secret-redacted, no injection markers)."""
    if body is None:
        return ""
    if not isinstance(body, str):
        body = str(body)
    low = body.lower()
    for marker in _PROMPT_INJECTION_MARKERS:
        if marker in low:
            raise ValueError("PR body contains a forbidden prompt-injection marker")
    return sanitize_publication_text(
        body, field="PR body", max_len=MAX_BODY_LEN, reject_on_secret=False)


# ---------------------------------------------------------------------------
# Allowlist + standing policy (trusted controller config; agents cannot modify)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AllowlistEntry:
    """A trusted allowlist entry for one (provider, account).

    ``repositories`` is a frozenset of EXACT repo identities (``*`` is not
    supported — UNKNOWN → DENY).  ``permitted_actions`` is a frozenset of
    action names.  ``branch_namespaces`` bounds autonomous pushes (default
    ``argent/``); ``pr_targets`` bounds the PR base branches (empty => the
    allowlist does not authorize PR creation).
    """

    provider: str
    account: str
    repositories: frozenset
    permitted_actions: frozenset
    branch_namespaces: frozenset = frozenset({"argent/"})
    pr_targets: frozenset = frozenset()


@dataclass(frozen=True)
class ExternalActionAllowlist:
    """An immutable allowlist of :class:`AllowlistEntry` (fail-closed).

    A lookup that does not EXACTLY match provider+account+repository+action
    returns DENY.  No wildcard matching, no prefix authz.
    """

    entries: Tuple[AllowlistEntry, ...] = ()

    def entry_for(self, provider: str, account: str) -> Optional[AllowlistEntry]:
        for e in self.entries:
            if e.provider == provider and e.account == account:
                return e
        return None

    def permits(self, provider: str, account: str, repository: str,
                action: str) -> bool:
        e = self.entry_for(provider, account)
        if e is None:
            return False
        if repository not in e.repositories:
            return False
        if action not in e.permitted_actions:
            return False
        return True


@dataclass(frozen=True)
class StandingPolicy:
    """Standing high-autonomy policy (trusted controller config, NOT agent-editable).

    In I3-A this is represented but NOT activated beyond fixture config
    (``autonomous_actions`` is empty by default).  Changing the allowlist or
    standing policy itself is OWNER_GATED (not implemented here — the broker
    exposes only read access; there is no mutation path on the standing policy).
    """

    autonomous_actions: frozenset = frozenset()
    owner_gate_actions: frozenset = frozenset()


# ---------------------------------------------------------------------------
# Request model + provenance
# ---------------------------------------------------------------------------

def request_id_for(provider: str, account: str, repository: str,
                   action: str, idempotency_key: str) -> str:
    """Deterministic, bounded request id (idempotent creation)."""
    digest = hashlib.sha256(
        f"{provider}\x00{account}\x00{repository}\x00{action}\x00"
        f"{idempotency_key}".encode("utf-8")).hexdigest()[:REQUEST_ID_HEX]
    return REQUEST_ID_PREFIX + digest


def compute_provenance_mac(provenance: dict, mac_key: bytes) -> str:
    """Keyed HMAC-SHA256 over the provenance fields (I3-A HIGH-2).

    Reuses the Phase-F evidence MAC pattern: the same deterministic
    ``canonical_bytes`` canonicalization (from ``test_planning``) and the
    controller-held evidence MAC key (``_resolve_mac_key``) — **no new secret
    is invented**.  A plain unkeyed SHA-256 over the fields is forgeable by an
    agent that can recompute it; only a provenance minted under the evidence
    key verifies.  The MAC covers every field EXCEPT the MAC itself.
    """
    from .test_planning import canonical_bytes

    fields = {k: v for k, v in provenance.items() if k != "provenance_hash"}
    payload = canonical_bytes(fields)
    return hmac.new(bytes(mac_key), payload, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class ExternalActionRequest:
    """Versioned, controller-authoritative external action request."""

    request_id: str
    provider: str
    account: str
    action: str
    policy_class: str  # READ / BOUNDED_WRITE / SENSITIVE
    repository: str
    resource_ref: str
    source_job_id: str
    source_candidate_id: str
    requested_scope: str
    parameters: dict
    expected_preconditions: dict
    idempotency_key: str
    provenance_version: int
    provenance_hash: str
    state: str = RequestState.PENDING.value
    authorization_state: Optional[str] = None
    revision: int = 0
    holder_owner_instance_id: Optional[str] = None
    holder_lease_epoch: int = 0
    action_lock_name: Optional[str] = None
    provider_state: Optional[dict] = None
    provider_object_id: Optional[str] = None
    attempt_count: int = 0
    next_attempt_at: Optional[str] = None
    last_failure_class: Optional[str] = None
    last_error_code: Optional[str] = None
    expires_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def is_terminal(self) -> bool:
        return RequestState(self.state) in TERMINAL_REQUEST_STATES

    def is_mutation(self) -> bool:
        return self.action in MUTATION_ACTIONS

    @classmethod
    def from_row(cls, row: dict) -> "ExternalActionRequest":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        decoded = _decode_json_field
        kwargs = {k: row[k] for k in row.keys() if k in known}
        kwargs["parameters"] = decoded(row.get("parameters"), {})
        kwargs["expected_preconditions"] = decoded(
            row.get("expected_preconditions"), {})
        kwargs["provider_state"] = decoded(row.get("provider_state"), None)
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# Broker errors
# ---------------------------------------------------------------------------

class BrokerError(Exception):
    """Base error for the external action broker."""


class RequestRevisionError(BrokerError):
    """A request transition failed its revision CAS fence."""


class RequestNotFound(BrokerError):
    """A referenced request does not exist."""


class ProvenanceError(BrokerError):
    """A request's provenance failed verification (fail-closed)."""


class PolicyDeniedError(BrokerError):
    """The policy engine denied the request (bounded reason code attached)."""

    def __init__(self, reason_code: str, detail: str = ""):
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code


class AuthorizationError(BrokerError):
    """An authorization operation failed its binding (owner approval mismatch)."""


class IllegalRequestTransition(BrokerError):
    """A request transition violated the closed edge map (terminal immutability)."""


class IdempotencyConflictError(BrokerError):
    """An idempotency key was reused with a non-equivalent request (fail-closed)."""


# ---------------------------------------------------------------------------
# Broker
# ---------------------------------------------------------------------------

def _now_iso(clock: Optional[Callable[[], datetime]] = None) -> str:
    dt = (clock() if clock else datetime.now(timezone.utc))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class ExternalActionBroker:
    """Single authority for external actions (provider-neutral)."""

    def __init__(
        self,
        store,
        *,
        adapter=None,
        allowlist: Optional[ExternalActionAllowlist] = None,
        standing_policy: Optional[StandingPolicy] = None,
        clock: Optional[Callable[[], datetime]] = None,
        mac_key: Optional[bytes] = None,
    ):
        from .test_execution import _resolve_mac_key

        self.store = store
        self._adapter = adapter if adapter is not None else NoWriteExternalProviderAdapter()
        self._allowlist = allowlist if allowlist is not None else ExternalActionAllowlist()
        self._standing_policy = standing_policy if standing_policy is not None else StandingPolicy()
        self._clock = clock
        # Keyed provenance MAC (I3-A HIGH-2): reuse the Phase-F evidence MAC
        # key (fail-closed when absent — an unkeyed provenance never verifies).
        self._mac_key = _resolve_mac_key(mac_key)

    # -- provenance verification --------------------------------------------

    def verify_provenance(self, provenance: dict) -> dict:
        """Fail-closed verification that a request's provenance is a valid I2
        integration result (CASE 11–15).

        Returns the normalized provenance dict (with its recomputed hash) on
        success; raises :class:`ProvenanceError` on any missing/invalid fact:

        * the source integration candidate exists and is INTEGRATED with a
          sha-like ``integrated_head`` AND its ``source_job_id`` equals the
          provenance's ``source_job_id``;
        * the source job is terminal-DONE with a sha-like source head;
        * ``repository`` matches the candidate's repository AND the
          provenance ``branch`` matches the candidate's integration target;
        * no unresolved HIGH/CRITICAL findings on the source task;
        * ``branch``/``ref`` is a validated branch identity;
        * ``provenance_hash`` equals :func:`compute_provenance_mac` under the
          controller-held evidence MAC key (keyed — a plain SHA is forged).

        Agent statements are never sufficient — only these trusted store facts.
        """
        if not isinstance(provenance, dict):
            raise ProvenanceError("provenance must be a mapping")
        req = {
            "version", "source_job_id", "source_candidate_id", "repository",
            "source_head", "integrated_head", "branch", "scope",
        }
        missing = req - set(provenance.keys())
        if missing:
            raise ProvenanceError(f"provenance missing fields: {sorted(missing)}")
        if provenance.get("version") != PROVENANCE_VERSION:
            raise ProvenanceError(
                f"unsupported provenance version {provenance.get('version')!r}")

        stored_hash = provenance.get("provenance_hash")
        if not isinstance(stored_hash, str) or len(stored_hash) != 64:
            raise ProvenanceError("provenance_hash must be a sha256 hex")
        expected_mac = compute_provenance_mac(provenance, self._mac_key)
        if not hmac.compare_digest(stored_hash, expected_mac):
            raise ProvenanceError("provenance_hash mismatch (forged/stale)")

        candidate = self.store.get_integration_candidate(
            provenance["source_candidate_id"])
        if candidate is None:
            raise ProvenanceError("source_candidate missing")
        from .integration_candidate import CandidateState
        if candidate["state"] != CandidateState.INTEGRATED.value:
            raise ProvenanceError("source_candidate not INTEGRATED")
        # (HIGH-2a) the candidate must be bound to the provenance's source job
        # (and therefore to the request's source_job_id).
        if candidate["source_job_id"] != provenance["source_job_id"]:
            raise ProvenanceError("source_job_id mismatch vs candidate")
        integrated_head = candidate.get("integrated_head")
        if not is_sha_like(integrated_head or ""):
            raise ProvenanceError("integrated_head not proven")
        if integrated_head != provenance["integrated_head"]:
            raise ProvenanceError("integrated_head mismatch vs candidate")

        job = self.store.get_supervisor_job(provenance["source_job_id"])
        if job is None:
            raise ProvenanceError("source_job missing")
        if job.get("terminal") != "DONE":
            raise ProvenanceError("source_job not terminal DONE")
        source_head = job.get("expected_head") or job.get("current_head")
        if not is_sha_like(source_head or ""):
            raise ProvenanceError("source_head not proven")
        if source_head != provenance["source_head"]:
            raise ProvenanceError("source_head mismatch vs job")

        repo = provenance["repository"]
        try:
            if validate_repo_identity(repo) is None:
                raise ProvenanceError("repository empty")
        except ValueError:
            raise ProvenanceError("repository invalid")
        if repo != candidate["repository"]:
            raise ProvenanceError("repository mismatch vs candidate")
        # (HIGH-2b) bind the integration target/branch to the candidate.
        if candidate.get("integration_target") != provenance["branch"]:
            raise ProvenanceError("integration target mismatch vs candidate")

        # No unresolved HIGH/CRITICAL findings on the source task.
        for f in self.store.list_findings(job.get("task_id")):
            if f.status.value != "open":
                continue
            sev = (f.severity or "").strip().upper()
            if sev in ("HIGH", "CRITICAL"):
                raise ProvenanceError(f"unresolved {sev} finding blocks external action")

        try:
            if validate_branch_identity(provenance["branch"]) is None:
                raise ProvenanceError("branch invalid")
        except ValueError:
            raise ProvenanceError("branch invalid")

        return provenance

    # -- request creation ----------------------------------------------------

    def create_request(
        self,
        *,
        provider: str,
        account: str,
        action: str,
        repository: str,
        resource_ref: str,
        requested_scope: str,
        parameters: dict,
        expected_preconditions: Optional[dict] = None,
        idempotency_key: str,
        provenance: dict,
        expiry_ttl_seconds: int = 3600,
    ) -> dict:
        """Create (idempotently) a PENDING external action request.

        Controller-authoritative: the provenance must be a verified I2
        integration result (:meth:`verify_provenance`); the parameters are
        bounded-validated (no command substitution / ref injection).  The
        policy class is derived deterministically from the action taxonomy.
        Raises :class:`ProvenanceError` / :class:`ValueError` on any invalid
        input (nothing is persisted on failure).
        """
        taxonomy = action_taxonomy(action)
        if taxonomy is None:
            raise ValueError(f"unknown action {action!r}")
        if not isinstance(provider, str) or not provider or len(provider) > MAX_PROVIDER_LEN:
            raise ValueError("provider must be a bounded non-empty string")
        if not isinstance(account, str) or not account or len(account) > MAX_ACCOUNT_LEN:
            raise ValueError("account must be a bounded non-empty string")
        try:
            if validate_repo_identity(repository) is None:
                raise ValueError("repository must be a non-empty string")
        except ValueError as exc:
            raise ValueError(f"invalid repository: {exc}")
        if not isinstance(requested_scope, str) or len(requested_scope) > MAX_SCOPE_LEN:
            raise ValueError("requested_scope must be a bounded string")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must be a non-empty string")
        if not isinstance(expiry_ttl_seconds, int) or not (1 <= expiry_ttl_seconds <= MAX_EXPIRY_TTL_SECONDS):
            raise ValueError("expiry_ttl_seconds out of range")

        # Validate provenance FIRST (fail-closed before any bounded encoding).
        verified = self.verify_provenance(provenance)

        # Bounded, safe parameter/precondition encoding (validated per action).
        parameters = self._validate_parameters(action, parameters)
        preconditions = self._validate_preconditions(
            action, expected_preconditions or {})

        # (HIGH-2b) bind the mutation parameters to the integrated result.
        self._bind_mutation_to_integrated(action, parameters, verified)

        params_json = _bounded_json(parameters, MAX_PARAMETERS_JSON_BYTES,
                                    "parameters")
        pre_json = _bounded_json(preconditions, MAX_PRECONDITIONS_JSON_BYTES,
                                 "expected_preconditions")

        rid = request_id_for(provider, account, repository, action, idempotency_key)
        now_iso = _now_iso(self._clock)
        expires_at = _iso_add(now_iso, expiry_ttl_seconds)

        row, created = self.store.create_external_action_request(
            request_id=rid,
            provider=provider,
            account=account,
            action=action,
            policy_class=taxonomy.value,
            repository=repository,
            resource_ref=resource_ref[:MAX_RESOURCE_REF_LEN],
            source_job_id=verified["source_job_id"],
            source_candidate_id=verified["source_candidate_id"],
            requested_scope=requested_scope,
            parameters=params_json,
            expected_preconditions=pre_json,
            idempotency_key=idempotency_key,
            provenance_version=PROVENANCE_VERSION,
            provenance_hash=verified["provenance_hash"],
            state=RequestState.PENDING.value,
            expires_at=expires_at,
        )
        # (HIGH-6) audit the ACTUAL returned row id, and only on creation (no
        # duplicate REQUESTED rows on idempotent reuse).
        if created:
            self.store.append_external_action_audit(
                row["request_id"], AUDIT_REQUESTED, failure_class=None,
                reason_code=RC_ALLOWED, detail=None,
            )
        return row

    def _bind_mutation_to_integrated(self, action: str, parameters: dict,
                                     verified: dict) -> None:
        """Bind mutation parameters to the integrated result (I3-A HIGH-2b).

        A push must reference the candidate's integrated HEAD SHA (never an
        arbitrary SHA-shaped value); a PR must reference the integrated HEAD
        as its head SHA.  Fail-closed (ValueError) on any mismatch.
        """
        integrated_head = verified["integrated_head"]
        if action == "push_feature_branch":
            if parameters.get("sha") != integrated_head:
                raise ValueError(
                    "push sha must equal the candidate's integrated HEAD")
        elif action == "create_pull_request":
            if parameters.get("head_sha") != integrated_head:
                raise ValueError(
                    "PR head_sha must equal the candidate's integrated HEAD")

    # -- parameter validation (no command substitution / ref injection) ------

    def _validate_parameters(self, action: str, parameters) -> dict:
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, dict):
            raise ValueError("parameters must be a mapping")
        out = {}
        for k, v in parameters.items():
            if not isinstance(k, str) or not k or len(k) > MAX_RESOURCE_REF_LEN:
                raise ValueError("parameter key must be a bounded string")
            out[k] = v
        if action == "push_feature_branch":
            self._require(out, "branch", "sha")
            branch = out["branch"]
            try:
                validate_branch_identity(branch)
            except ValueError as exc:
                raise ValueError(f"invalid branch: {exc}")
            if any(tok in branch for tok in ("..", "~", "^", ":", "@{", " ")):
                raise ValueError("branch contains a reserved revision token")
            if not is_sha_like(out["sha"]):
                raise ValueError("push sha must be a full sha")
        elif action == "create_pull_request":
            self._require(out, "head_branch", "base_branch", "head_sha")
            for bkey in ("head_branch", "base_branch"):
                try:
                    validate_branch_identity(out[bkey])
                except ValueError as exc:
                    raise ValueError(f"invalid {bkey}: {exc}")
            if not is_sha_like(out["head_sha"]):
                raise ValueError("PR head_sha must be a full sha")
            if "title" in out:
                out["title"] = validate_pr_title(out["title"])
            if "body" in out:
                out["body"] = validate_pr_body(out["body"])
        elif action == "update_pull_request":
            self._require(out, "number")
            if not isinstance(out["number"], int) or out["number"] <= 0:
                raise ValueError("PR number must be a positive integer")
            if "title" in out:
                out["title"] = validate_pr_title(out["title"])
            if "body" in out:
                out["body"] = validate_pr_body(out["body"])
        return out

    @staticmethod
    def _require(params: dict, *keys: str) -> None:
        for k in keys:
            if k not in params:
                raise ValueError(f"missing required parameter {k!r}")

    @staticmethod
    def _validate_preconditions(action: str, preconditions) -> dict:
        if not isinstance(preconditions, dict):
            raise ValueError("expected_preconditions must be a mapping")
        return preconditions

    # -- policy evaluation ---------------------------------------------------

    def evaluate_policy(self, request: dict) -> Tuple[PolicyDecision, str]:
        """Deterministic policy decision + bounded reason code (no LLM).

        Order (fail-closed, HIGH-3):

        1. action unknown → DENY/UNKNOWN_ACTION;
        2. provider unknown → DENY/UNKNOWN_PROVIDER (no adapter, no allowlist
           entry → the provider is simply not known);
        3. allowlist: provider/account/repository/action must all EXACTLY match
           → else DENY (NOT_ALLOWLISTED / UNKNOWN_ACCOUNT / UNKNOWN_REPO) —
           evaluated BEFORE the SENSITIVE gate so a DENY-class condition can
           never be bypassed by an owner approval;
        4. SENSITIVE class → OWNER_GATE_REQUIRED/SENSITIVE_ACTION (CASE 48/49);
        5. standing policy: an autonomous BOUNDED_WRITE requires the standing
           policy to grant the action (empty default ⇒ NOT autonomous →
           OWNER_GATE_REQUIRED);
        6. branch safety: protected refs and off-namespace autonomous pushes
           are never autonomous (namespace from the allowlist entry);
        7. READ → ALLOW_AUTONOMOUS; BOUNDED_WRITE → ALLOW_AUTONOMOUS when
           branch-safe AND standing-policy-granted, else OWNER_GATE_REQUIRED /
           DENY.
        """
        provider = request["provider"]
        action = request["action"]
        repository = request["repository"]
        account = request["account"]
        request = decode_row(request)
        taxonomy = action_taxonomy(action)
        if taxonomy is None:
            return PolicyDecision.DENY, RC_UNKNOWN_ACTION
        if self._adapter.provider_name != provider:
            return PolicyDecision.DENY, RC_UNKNOWN_PROVIDER
        # FULL allowlist checks BEFORE the SENSITIVE gate (DENY can never be
        # bypassed by an owner approval).
        entry = self._allowlist.entry_for(provider, account)
        if entry is None:
            return PolicyDecision.DENY, RC_UNKNOWN_ACCOUNT
        if repository not in entry.repositories:
            return PolicyDecision.DENY, RC_UNKNOWN_REPO
        if action not in entry.permitted_actions:
            return PolicyDecision.DENY, RC_CLASS_NOT_PERMITTED
        if taxonomy is ActionTaxonomy.SENSITIVE:
            return PolicyDecision.OWNER_GATE_REQUIRED, RC_SENSITIVE

        # Standing policy (HIGH-3): an autonomous BOUNDED_WRITE requires the
        # standing policy to grant the action.  READ is always autonomous.
        if taxonomy is ActionTaxonomy.BOUNDED_WRITE:
            if action not in self._standing_policy.autonomous_actions:
                return PolicyDecision.OWNER_GATE_REQUIRED, RC_NOT_AUTONOMOUS

        # Branch safety (writes only; reads are always autonomous).
        if action == "push_feature_branch":
            branch = request["parameters"].get("branch", "")
            if is_protected_ref(branch):
                return PolicyDecision.OWNER_GATE_REQUIRED, RC_PROTECTED_REF
            task_id = self._task_id_for(request)
            namespaces = tuple(entry.branch_namespaces) if entry.branch_namespaces \
                else (AUTONOMOUS_BRANCH_PREFIX,)
            if not autonomous_branch_ok(branch, task_id, namespaces=namespaces):
                return PolicyDecision.OWNER_GATE_REQUIRED, RC_BRANCH_NOT_IN_NAMESPACE
        elif action == "create_pull_request":
            base = request["parameters"].get("base_branch", "")
            if base not in entry.pr_targets:
                return PolicyDecision.DENY, RC_NOT_ALLOWLISTED
        return PolicyDecision.ALLOW_AUTONOMOUS, RC_ALLOWED

    def _task_id_for(self, request: dict) -> str:
        """The source task id (for the autonomous branch namespace check)."""
        job = self.store.get_supervisor_job(request["source_job_id"])
        return job.get("task_id", "") if job else ""

    # -- authorization -------------------------------------------------------

    def authorize_autonomous(self, request_id: str) -> dict:
        """PENDING → AUTHORIZED for an ALLOW_AUTONOMOUS request (CASE: no owner).

        Re-runs the policy engine (never trusts a cached decision) and fails
        closed when the decision is not ALLOW_AUTONOMOUS.
        """
        row = self.store.get_external_action_request(request_id)
        if row is None:
            raise RequestNotFound(request_id)
        if row["state"] in TERMINAL_REQUEST_STATES:
            return row
        # (HIGH-5) expiry is enforced at every authorization entry.
        if self._check_expired(row):
            return self._expire(row)
        decision, reason = self.evaluate_policy(row)
        if decision is not PolicyDecision.ALLOW_AUTONOMOUS:
            return self._deny(row, reason)
        return self.store.transition_external_action_request(
            request_id, from_state=RequestState.PENDING.value,
            to_state=RequestState.AUTHORIZED.value,
            expected_revision=row["revision"],
            authorization_state=decision.value,
            last_error_code=None,
        )

    def authorize_owner(self, request_id: str, *, approval_id: str) -> dict:
        """PENDING → AUTHORIZED via a verified single-use owner approval
        loaded from the authoritative store (HIGH-1).

        The approval must be APPROVED, unexpired, TRUSTED-source, and bound to
        EXACTLY (task, action, provider, account, repository, resource,
        requested_scope, provenance, idempotency key, parameters/preconditions)
        — reuse the ``gates.binding_hash`` contract.  The policy engine is
        re-run first: a DENY-class condition can never be bypassed by an owner
        approval.  The approval is atomically consumed single-use via the
        existing ``store._consume_approval`` API.  Raises
        :class:`AuthorizationError` on any binding/reuse/expiry mismatch.
        """
        from .gates import binding_hash
        from .models import ApprovalStatus, SourceClass

        row = self.store.get_external_action_request(request_id)
        if row is None:
            raise RequestNotFound(request_id)
        if row["state"] in TERMINAL_REQUEST_STATES:
            raise AuthorizationError(f"request {request_id!r} is terminal")
        if row["state"] != RequestState.PENDING.value:
            raise AuthorizationError(f"request {request_id!r} is not PENDING")
        if self._check_expired(row):
            self._expire(row)
            raise AuthorizationError("request expired")
        # (HIGH-3) re-run the full policy: DENY cannot be owner-authorized.
        decision, reason = self.evaluate_policy(row)
        if decision is PolicyDecision.DENY:
            raise AuthorizationError(f"policy denies owner authorization: {reason}")

        approval = self.store.get_approval(approval_id)
        if approval is None:
            raise AuthorizationError("approval not found in authoritative store")
        if approval.status is not ApprovalStatus.APPROVED:
            raise AuthorizationError("approval is not approved")
        now_iso = _now_iso(self._clock)
        if approval.expires_at <= now_iso:
            raise AuthorizationError("approval expired")
        if approval.source_class is not SourceClass.TRUSTED:
            raise AuthorizationError("approval source_class is not trusted")

        job = self.store.get_supervisor_job(row["source_job_id"])
        task_id = job.get("task_id") if job else None
        scope = self._approval_scope(row)
        expected = binding_hash(task_id, approval.action, scope)
        if (approval.task_id != task_id
                or approval.action != row["action"]
                or approval.scope != scope
                or approval.binding_hash != expected):
            raise AuthorizationError("owner approval binding mismatch")

        # Single-use atomic consumption (HIGH-1): a consumed/expired approval
        # can never authorize a second request.
        rc = self.store._consume_approval(approval.id, now_iso)
        if rc != 1:
            raise AuthorizationError("approval already consumed or expired")
        return self.store.transition_external_action_request(
            request_id, from_state=RequestState.PENDING.value,
            to_state=RequestState.AUTHORIZED.value,
            expected_revision=row["revision"],
            authorization_state=PolicyDecision.OWNER_GATE_REQUIRED.value,
            last_error_code=None,
        )

    def _approval_scope(self, request: dict) -> str:
        """The deterministic, fully-binding approval scope (HIGH-1).

        Binds provider + account + repository + resource_ref + requested_scope
        + provenance (hash + source job/candidate) + idempotency key +
        parameters/preconditions content hashes, so an approval authorizes
        EXACTLY one request (no scope substitution across account/params/
        provenance).
        """
        req = decode_row(request)
        params_hash = hashlib.sha256(
            json.dumps(req["parameters"], sort_keys=True, default=str)
            .encode("utf-8")).hexdigest()
        pre_hash = hashlib.sha256(
            json.dumps(req["expected_preconditions"], sort_keys=True, default=str)
            .encode("utf-8")).hexdigest()
        return json.dumps([
            request["provider"], request["account"], request["repository"],
            request["resource_ref"], request["requested_scope"],
            request["provenance_hash"], request["source_job_id"],
            request["source_candidate_id"], request["idempotency_key"],
            params_hash, pre_hash,
        ], sort_keys=True)

    # -- expiry / redrive helpers -------------------------------------------

    def _check_expired(self, row: dict) -> bool:
        expires = row.get("expires_at")
        if not expires:
            return False
        return expires <= _now_iso(self._clock)

    def _expire(self, row: dict) -> dict:
        """Terminal BLOCKED with RC_EXPIRED (HIGH-5)."""
        try:
            updated = self.store.transition_external_action_request(
                row["request_id"], from_state=row["state"],
                to_state=RequestState.BLOCKED.value,
                expected_revision=row["revision"],
                last_failure_class=FAILURE_PRECONDITION_FAILED,
                last_error_code=RC_EXPIRED)
        except (RequestRevisionError, IllegalRequestTransition):
            updated = self.store.get_external_action_request(
                row["request_id"]) or row
        self.store.append_external_action_audit(
            row["request_id"], AUDIT_EXECUTED,
            failure_class=FAILURE_PRECONDITION_FAILED,
            reason_code=RC_EXPIRED, detail=None)
        return updated

    # -- execution -----------------------------------------------------------

    def _lock_name(self, request: dict) -> str:
        from .concurrency_policy import ACTION_REPO_GLOBAL, action_lock_name
        return action_lock_name(
            ACTION_REPO_GLOBAL, repo_identity=request["repository"],
            name=f"external-action:{request['request_id']}",
        )

    def execute(
        self,
        request_id: str,
        *,
        holder_job_id: str,
        holder_lease_epoch: int,
    ) -> dict:
        """Drive an AUTHORIZED request through EXECUTING → terminal.

        Fenced: re-checks expiry + policy currency, acquires the named action
        lock (lease-verified), transitions AUTHORIZED → EXECUTING via the
        holder-verified authoritative transition, dispatches to the adapter,
        and finalizes.  A stale holder can never finalize (the authoritative
        transition re-verifies lease + lock).  A provider mutation is
        structurally refused when the adapter is not write-enabled (CASE 50).
        """
        row = self.store.get_external_action_request(request_id)
        if row is None:
            raise RequestNotFound(request_id)
        if row["state"] in TERMINAL_REQUEST_STATES:
            return row
        if row["state"] != RequestState.AUTHORIZED.value:
            raise BrokerError(f"request {request_id!r} is not AUTHORIZED")
        # (HIGH-5) expiry at execution entry.
        if self._check_expired(row):
            return self._expire(row)
        # (HIGH-3) re-check policy currency: a revoked/DENY policy refuses
        # execution even after a prior authorization.
        decision, reason = self.evaluate_policy(row)
        if decision is PolicyDecision.DENY:
            return self._deny(row, reason)
        lock = self._lock_name(row)
        if not self.store.try_acquire_action_lock(
            lock, job_id=holder_job_id, lease_epoch=holder_lease_epoch):
            return row  # another holder owns the lock
        try:
            return self._drive(row, holder_job_id, holder_lease_epoch, lock)
        finally:
            self.store.release_action_lock(
                lock, job_id=holder_job_id, lease_epoch=holder_lease_epoch)

    def redrive_waiting(
        self,
        request_id: str,
        *,
        holder_job_id: str,
        holder_lease_epoch: int,
    ) -> dict:
        """Deterministic redrive path for a WAITING_EXTERNAL request (HIGH-5).

        This is the broker method the I3-B scheduler/background runtime calls
        to re-drive a request that entered WAITING_EXTERNAL (no LLM occupies a
        slot while waiting).  Holder-verified + action-lock fenced exactly like
        :meth:`execute`; a stale holder aborts.  I3-A implements the method +
        tests only — the real scheduler wiring is I3-B (marked hook).
        """
        row = self.store.get_external_action_request(request_id)
        if row is None:
            raise RequestNotFound(request_id)
        if row["state"] in TERMINAL_REQUEST_STATES:
            return row
        if row["state"] != RequestState.WAITING_EXTERNAL.value:
            raise BrokerError(f"request {request_id!r} is not WAITING_EXTERNAL")
        if self._check_expired(row):
            return self._expire(row)
        decision, reason = self.evaluate_policy(row)
        if decision is PolicyDecision.DENY:
            return self._deny(row, reason)
        lock = self._lock_name(row)
        if not self.store.try_acquire_action_lock(
            lock, job_id=holder_job_id, lease_epoch=holder_lease_epoch):
            return row
        try:
            return self._drive(row, holder_job_id, holder_lease_epoch, lock)
        finally:
            self.store.release_action_lock(
                lock, job_id=holder_job_id, lease_epoch=holder_lease_epoch)

    def _drive(self, row: dict, holder_job_id: str,
               holder_lease_epoch: int, lock: str) -> dict:
        """Shared execution body for execute/redrive: (from row state) →
        EXECUTING (holder-verified) → dispatch → settle."""
        try:
            row = self.store.transition_external_action_request_authoritative(
                row["request_id"], lock_name=lock, holder_job_id=holder_job_id,
                holder_lease_epoch=holder_lease_epoch,
                from_state=row["state"],
                to_state=RequestState.EXECUTING.value,
                expected_revision=row["revision"],
                holder_owner_instance_id=holder_job_id)
        except (LeaseFencedError, RequestRevisionError):
            return self.store.get_external_action_request(
                row["request_id"]) or row

        # Structurally refuse a mutation against a read-only adapter (CASE 50).
        if row["action"] in MUTATION_ACTIONS and not self._adapter.write_enabled:
            return self._finalize(
                row, RequestState.FAILED, failure_class=FAILURE_POLICY_DENIED,
                error_code="PROVIDER_WRITE_DISABLED",
                holder_job_id=holder_job_id, holder_lease_epoch=holder_lease_epoch,
                lock=lock, audit=AUDIT_EXECUTED)

        # Preconditions: bounded, deterministic gate before dispatch.
        pre_error = self._check_preconditions(row)
        if pre_error is not None:
            return self._finalize(
                row, RequestState.BLOCKED, failure_class=FAILURE_PRECONDITION_FAILED,
                error_code=pre_error, holder_job_id=holder_job_id,
                holder_lease_epoch=holder_lease_epoch, lock=lock,
                audit=AUDIT_EXECUTED)

        try:
            result = self._dispatch(row)
        except ProviderWriteDisabled:
            return self._finalize(
                row, RequestState.FAILED, failure_class=FAILURE_POLICY_DENIED,
                error_code="PROVIDER_WRITE_DISABLED", holder_job_id=holder_job_id,
                holder_lease_epoch=holder_lease_epoch, lock=lock,
                audit=AUDIT_EXECUTED)
        except ProviderRateLimited:
            result = ProviderResult(OUTCOME_RATE_LIMITED, detail="rate_limited")
        except ProviderConflict:
            result = ProviderResult(OUTCOME_CONFLICT, detail="conflict")
        except ProviderCredentialError:
            result = ProviderResult(OUTCOME_CREDENTIAL_ERROR, detail="credential_error")
        except ProviderValidationError:
            result = ProviderResult(OUTCOME_VALIDATION_FAILED, detail="validation_failed")
        except ProviderUnavailable:
            result = ProviderResult(OUTCOME_UNAVAILABLE, detail="unavailable")
        except Exception as exc:  # noqa: BLE001 - provider surface is untrusted
            result = ProviderResult(
                OUTCOME_UNAVAILABLE, detail=type(exc).__name__)

        return self._settle(row, result, holder_job_id, holder_lease_epoch, lock)

    def _check_preconditions(self, row: dict) -> Optional[str]:
        """Bounded precondition gate (deterministic; never provider/agent text)."""
        # The source job must still be terminal-DONE (immutable provenance).
        job = self.store.get_supervisor_job(row["source_job_id"])
        if job is None or job.get("terminal") != "DONE":
            return "source_terminal_mutated"
        return None

    def _dispatch(self, row: dict) -> ProviderResult:
        request = ExternalActionRequest.from_row(row)
        action = request.action
        if action == "read_repository":
            return self._adapter.read_repository(request)
        if action == "read_ref":
            return self._adapter.read_ref(request)
        if action == "read_pull_request":
            return self._adapter.read_pull_request(request)
        if action == "read_checks":
            return self._adapter.read_checks(request)
        if action == "push_feature_branch":
            return self._adapter.push_feature_branch(request)
        if action == "create_pull_request":
            return self._adapter.create_pull_request(request)
        if action == "update_pull_request":
            return self._adapter.update_pull_request(request)
        # merge/release/deploy are SENSITIVE (owner-gated) and structurally
        # absent from I3-A's no-write adapter set — fail closed.
        return ProviderResult(OUTCOME_VALIDATION_FAILED,
                              detail=f"unsupported action {action!r}")

    def _settle(self, row: dict, result: ProviderResult, holder_job_id: str,
                holder_lease_epoch: int, lock: str) -> dict:
        if not isinstance(result, ProviderResult):
            return self._finalize(
                row, RequestState.FAILED, failure_class=FAILURE_LOCAL_CODE_ERROR,
                error_code="BAD_PROVIDER_RESULT", holder_job_id=holder_job_id,
                holder_lease_epoch=holder_lease_epoch, lock=lock,
                audit=AUDIT_EXECUTED)
        outcome = result.outcome
        if outcome not in ALLOWED_OUTCOMES:
            return self._finalize(
                row, RequestState.FAILED, failure_class=FAILURE_LOCAL_CODE_ERROR,
                error_code="BAD_OUTCOME", holder_job_id=holder_job_id,
                holder_lease_epoch=holder_lease_epoch, lock=lock,
                audit=AUDIT_EXECUTED)
        if outcome == OUTCOME_SUCCESS:
            return self._finalize(
                row, RequestState.SUCCEEDED, failure_class=None, error_code=None,
                provider_object_id=result.object_id, provider_state=result.state,
                holder_job_id=holder_job_id, holder_lease_epoch=holder_lease_epoch,
                lock=lock, audit=AUDIT_EXECUTED)
        if outcome == OUTCOME_WAITING:
            return self._bounded_wait(
                row, failure_class=None, detail=None, holder_job_id=holder_job_id,
                holder_lease_epoch=holder_lease_epoch, lock=lock)
        # Bounded retry for transient classes; terminal for permanent ones.
        failure_class = _OUTCOME_TO_FAILURE_CLASS.get(outcome, FAILURE_UNKNOWN)
        if outcome in (OUTCOME_UNAVAILABLE, OUTCOME_RATE_LIMITED):
            return self._bounded_wait(
                row, failure_class=failure_class, detail=result.detail,
                holder_job_id=holder_job_id, holder_lease_epoch=holder_lease_epoch,
                lock=lock)
        # conflict / validation / credential are terminal failures.
        return self._finalize(
            row, RequestState.FAILED, failure_class=failure_class,
            error_code=result.detail, holder_job_id=holder_job_id,
            holder_lease_epoch=holder_lease_epoch, lock=lock, audit=AUDIT_EXECUTED)

    def _finalize(self, row: dict, to_state: RequestState, *, failure_class,
                  error_code, holder_job_id, holder_lease_epoch, lock,
                  audit: str, provider_object_id=None, provider_state=None) -> dict:
        # (HIGH-7) sanitize every error/reason string before ANY persistence.
        error_code = sanitize_provider_detail(error_code)
        fields = {"last_failure_class": failure_class,
                  "last_error_code": error_code[:MAX_REASON_CODE_LEN]}
        if provider_object_id is not None:
            fields["provider_object_id"] = str(provider_object_id)[:256]
        if provider_state is not None:
            fields["provider_state"] = _bounded_json(
                provider_state, MAX_PROVIDER_STATE_JSON_BYTES, "provider_state")
        try:
            updated = self.store.transition_external_action_request_authoritative(
                row["request_id"], lock_name=lock, holder_job_id=holder_job_id,
                holder_lease_epoch=holder_lease_epoch, from_state=row["state"],
                to_state=to_state.value, expected_revision=row["revision"],
                holder_owner_instance_id=holder_job_id, clear_holder=(to_state in TERMINAL_REQUEST_STATES),
                **fields)
        except (LeaseFencedError, RequestRevisionError):
            updated = self.store.get_external_action_request(
                row["request_id"]) or row
        self.store.append_external_action_audit(
            row["request_id"], audit, failure_class=failure_class,
            reason_code=error_code[:MAX_REASON_CODE_LEN],
            detail=None)
        return updated

    def _bounded_wait(self, row: dict, *, failure_class, detail,
                      holder_job_id, holder_lease_epoch, lock) -> dict:
        """Unified EXECUTING → WAITING_EXTERNAL with a shared retry budget
        (HIGH-5): EVERY outcome that increments ``attempt_count`` (rate-limit,
        unavailable, AND ``waiting``) honors :data:`MAX_RETRY_ATTEMPTS`, then
        terminal-fails conservatively.
        """
        attempt = row["attempt_count"] + 1
        if attempt > MAX_RETRY_ATTEMPTS:
            return self._finalize(
                row, RequestState.FAILED,
                failure_class=failure_class or FAILURE_UNKNOWN,
                error_code=(detail or "RETRY_EXHAUSTED"),
                holder_job_id=holder_job_id, holder_lease_epoch=holder_lease_epoch,
                lock=lock, audit=AUDIT_EXECUTED)
        delay = next_check_delay_seconds(attempt)
        next_check_at = _iso_add(_now_iso(self._clock), delay)
        fields = {"attempt_count": attempt, "next_attempt_at": next_check_at}
        if failure_class is not None:
            fields["last_failure_class"] = failure_class
        if detail is not None:
            fields["last_error_code"] = \
                sanitize_provider_detail(detail)[:MAX_REASON_CODE_LEN]
        try:
            updated = self.store.transition_external_action_request_authoritative(
                row["request_id"], lock_name=lock, holder_job_id=holder_job_id,
                holder_lease_epoch=holder_lease_epoch, from_state=row["state"],
                to_state=RequestState.WAITING_EXTERNAL.value,
                expected_revision=row["revision"],
                holder_owner_instance_id=holder_job_id, **fields)
        except (LeaseFencedError, RequestRevisionError):
            updated = self.store.get_external_action_request(
                row["request_id"]) or row
        return updated

    # -- reconciliation (crash-after-provider-success) ------------------------

    def reconcile(self, request_id: str, *, holder_job_id: str,
                  holder_lease_epoch: int) -> dict:
        """Reconcile an in-flight request against provider-visible state.

        Holder-verified + action-lock fenced (HIGH-4): a phantom/stale holder
        can never finalize (the authoritative transition re-verifies the live
        job lease + action-lock ownership atomically).  Detects crash-after-
        provider-success: e.g. a push whose remote ref already equals the
        expected SHA, or a create-PR whose Argent-owned PR already exists — the
        request is finalized SUCCEEDED WITHOUT re-running the mutation (no
        duplicate).  Never claims exactly-once beyond provider semantics.
        Reads only (the observation is UNTRUSTED DATA).
        """
        row = self.store.get_external_action_request(request_id)
        if row is None:
            raise RequestNotFound(request_id)
        if row["state"] in TERMINAL_REQUEST_STATES:
            return row
        if row["state"] not in (RequestState.EXECUTING.value,
                                RequestState.WAITING_EXTERNAL.value):
            return row
        # (HIGH-5) expiry at reconcile entry.
        if self._check_expired(row):
            return self._expire(row)
        lock = self._lock_name(row)
        if not self.store.try_acquire_action_lock(
            lock, job_id=holder_job_id, lease_epoch=holder_lease_epoch):
            return row  # another holder owns the lock
        try:
            return self._reconcile_locked(row, holder_job_id, holder_lease_epoch, lock)
        finally:
            self.store.release_action_lock(
                lock, job_id=holder_job_id, lease_epoch=holder_lease_epoch)

    def _reconcile_locked(self, row: dict, holder_job_id: str,
                          holder_lease_epoch: int, lock: str) -> dict:
        try:
            obs = self._adapter.observe(ExternalActionRequest.from_row(row))
        except Exception:  # noqa: BLE001
            obs = ProviderObservation(found=False)
        if not isinstance(obs, ProviderObservation):
            obs = ProviderObservation(found=False)
        if obs.found:
            fields = {"provider_object_id": (obs.object_id or "")[:256]}
            if obs.state is not None:
                fields["provider_state"] = _bounded_json(
                    obs.state, MAX_PROVIDER_STATE_JSON_BYTES, "provider_state")
            try:
                updated = self.store.transition_external_action_request_authoritative(
                    row["request_id"], lock_name=lock, holder_job_id=holder_job_id,
                    holder_lease_epoch=holder_lease_epoch, from_state=row["state"],
                    to_state=RequestState.SUCCEEDED.value,
                    expected_revision=row["revision"], clear_holder=True, **fields)
            except (LeaseFencedError, RequestRevisionError):
                updated = self.store.get_external_action_request(
                    row["request_id"]) or row
            self.store.append_external_action_audit(
                row["request_id"], AUDIT_RECONCILED, failure_class=None,
                reason_code="RECONCILED_SUCCESS", detail=None)
            return updated
        # Not found provider-side: re-drive from AUTHORIZED-equivalent is unsafe
        # unless a live holder re-executes; here we record a bounded no-op.
        self.store.append_external_action_audit(
            row["request_id"], AUDIT_RECONCILED, failure_class=None,
            reason_code="RECONCILED_NOT_FOUND", detail=None)
        return self.store.get_external_action_request(row["request_id"]) or row

    # -- denial (policy) ------------------------------------------------------

    def _deny(self, row: dict, reason: str) -> dict:
        try:
            updated = self.store.transition_external_action_request(
                row["request_id"], from_state=row["state"],
                to_state=RequestState.DENIED.value,
                expected_revision=row["revision"],
                last_failure_class=FAILURE_POLICY_DENIED,
                last_error_code=reason)
        except (RequestRevisionError, IllegalRequestTransition):
            updated = self.store.get_external_action_request(
                row["request_id"]) or row
        self.store.append_external_action_audit(
            row["request_id"], AUDIT_EXECUTED, failure_class=FAILURE_POLICY_DENIED,
            reason_code=reason, detail=None)
        return updated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bounded_json(value, max_bytes: int, name: str) -> str:
    raw = json.dumps(value, sort_keys=True, default=str)
    if len(raw.encode("utf-8")) > max_bytes:
        raise ValueError(f"{name} exceeds the JSON byte budget ({max_bytes})")
    return raw


def _decode_json_field(value, default):
    """Decode a persisted JSON column to its Python value (fail-closed)."""
    if value is None:
        return default
    if isinstance(value, (dict, list, int, float, str)) and not isinstance(value, str):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return default
    return default


def decode_row(row: dict) -> dict:
    """Return a request row with its JSON columns decoded (pure)."""
    out = dict(row)
    out["parameters"] = _decode_json_field(row.get("parameters"), {})
    out["expected_preconditions"] = _decode_json_field(
        row.get("expected_preconditions"), {})
    out["provider_state"] = _decode_json_field(row.get("provider_state"), None)
    return out


def _iso_add(iso: str, seconds: int) -> str:
    from datetime import timedelta

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).astimezone(timezone.utc).isoformat()
