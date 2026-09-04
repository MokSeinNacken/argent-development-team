"""Phase I3-A — External provider adapter boundary (I3-B-ready).

This module defines the provider-adapter protocol the External Action Broker
dispatches through, plus the ONLY adapter that may run in I3-A acceptance mode:
:class:`FakeGitHubAdapter` (a deterministic in-memory fixture).  There is **no
real provider write path** here: every mutation operation on the base protocol
is structurally disabled (raises :class:`ProviderWriteDisabled`) unless an
explicitly constructed fixture adapter overrides it.

Trust boundary: a provider adapter is the single translation point between the
broker's bounded :class:`~argent_core.external_action_broker.ExternalActionRequest`
and a provider's object model.  Everything a provider returns is UNTRUSTED
DATA — it is translated into the narrow :class:`ProviderResult` /
:class:`ProviderObservation` types, strictly validated by the broker, and can
never self-authorize an action.

No network, no shell, no credential access, no LLM in this module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Provider-side exception taxonomy (mapped by the broker to bounded failure
# classes — provider outage != code failure, rate limit != model failure).
# ---------------------------------------------------------------------------

class ProviderError(Exception):
    """Base class for provider-adapter failures."""


class ProviderWriteDisabled(ProviderError):
    """A mutation was attempted against a structurally read-only adapter."""


class ProviderUnavailable(ProviderError):
    """The provider is unreachable / down (a provider outage, not a code bug)."""


class ProviderNetworkError(ProviderUnavailable):
    """A transport/network failure reaching the provider (I3-B additive).

    A distinct subclass of :class:`ProviderUnavailable` so the adapter and its
    tests can tell a local transport failure (subprocess spawn failure, DNS,
    timeout, connection reset) apart from a provider-reported 5xx outage, while
    the broker's existing ``OUTCOME_UNAVAILABLE`` mapping (retryable outage)
    still applies unchanged — a network failure never invents a new outcome.
    """


class ProviderRateLimited(ProviderError):
    """The provider rejected the operation with a rate-limit (429 / quota)."""


class ProviderConflict(ProviderError):
    """The operation conflicted at the provider (e.g. non-fast-forward push)."""


class ProviderValidationError(ProviderError):
    """The provider rejected the operation as invalid (remote validation)."""


class ProviderCredentialError(ProviderError):
    """The provider rejected the operation for credential/auth reasons."""


# ---------------------------------------------------------------------------
# Bounded result types (all provider output is reduced to these)
# ---------------------------------------------------------------------------

#: Provider operation outcomes (closed set).  The broker maps these to its own
#: bounded request states / failure classes — never the reverse.
OUTCOME_SUCCESS = "success"
OUTCOME_WAITING = "waiting"
OUTCOME_RATE_LIMITED = "rate_limited"
OUTCOME_UNAVAILABLE = "unavailable"
OUTCOME_CONFLICT = "conflict"
OUTCOME_VALIDATION_FAILED = "validation_failed"
OUTCOME_CREDENTIAL_ERROR = "credential_error"

ALLOWED_OUTCOMES: frozenset[str] = frozenset({
    OUTCOME_SUCCESS, OUTCOME_WAITING, OUTCOME_RATE_LIMITED,
    OUTCOME_UNAVAILABLE, OUTCOME_CONFLICT, OUTCOME_VALIDATION_FAILED,
    OUTCOME_CREDENTIAL_ERROR,
})

#: Bounded provider-provided strings (defense against unbounded foreign data).
MAX_PROVIDER_OBJECT_ID_LEN = 256
MAX_PROVIDER_DETAIL_LEN = 512


def _bounded_detail(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    return text[:MAX_PROVIDER_DETAIL_LEN]


@dataclass(frozen=True)
class ProviderResult:
    """Bounded, validated result of a single provider operation.

    ``outcome`` is a closed-set string; ``object_id`` is a bounded opaque
    provider object id (PR number, pushed SHA, ref, check-run id); ``state`` is
    a bounded, JSON-serializable provider-visible snapshot used for
    reconciliation (e.g. ``{"remote_ref": "<sha>"}`` for a push, ``{"head":
    "<sha>", "title": ...}`` for a PR).  Never carries credentials or free
    command/poll fields.
    """

    outcome: str
    object_id: Optional[str] = None
    state: Optional[dict] = None
    detail: Optional[str] = None


@dataclass(frozen=True)
class ProviderObservation:
    """Provider-visible state read back for reconciliation (UNTRUSTED DATA).

    ``found`` is True iff the provider still shows an object matching the
    request's idempotency key (e.g. the pushed ref equals the expected SHA, or
    an Argent-owned PR for the same head already exists).  ``object_id`` /
    ``state`` carry the provider's view of that object (bounded).
    """

    found: bool
    object_id: Optional[str] = None
    state: Optional[dict] = None


# ---------------------------------------------------------------------------
# Provider adapter protocol / ABC
# ---------------------------------------------------------------------------

class ExternalProviderAdapter(ABC):
    """I3-B-ready provider-adapter protocol.

    The broker dispatches each external action to exactly one of the operations
    below.  Read operations are side-effect free.  Mutation operations are
    structurally disabled in the base class (I3-A: no real write path); a
    concrete fixture adapter (:class:`FakeGitHubAdapter`) enables them for
    deterministic tests.  ``write_enabled`` must be True before the broker will
    dispatch any mutation (CASE 50).
    """

    provider_name: str = "external"
    write_enabled: bool = False

    # -- read operations ----------------------------------------------------

    @abstractmethod
    def read_repository(self, request) -> ProviderResult: ...

    @abstractmethod
    def read_ref(self, request) -> ProviderResult: ...

    @abstractmethod
    def read_pull_request(self, request) -> ProviderResult: ...

    @abstractmethod
    def read_checks(self, request) -> ProviderResult: ...

    # -- mutation operations (structurally disabled in I3-A) -----------------

    def push_feature_branch(self, request) -> ProviderResult:
        raise ProviderWriteDisabled(
            f"{type(self).__name__} is read-only (no real write path in I3-A)")

    def create_pull_request(self, request) -> ProviderResult:
        raise ProviderWriteDisabled(
            f"{type(self).__name__} is read-only (no real write path in I3-A)")

    def update_pull_request(self, request) -> ProviderResult:
        raise ProviderWriteDisabled(
            f"{type(self).__name__} is read-only (no real write path in I3-A)")

    # -- reconciliation probe ------------------------------------------------

    @abstractmethod
    def observe(self, request) -> ProviderObservation: ...


class NoWriteExternalProviderAdapter(ExternalProviderAdapter):
    """A concrete read-capable-but-write-disabled adapter (I3-A acceptance).

    Every mutation raises :class:`ProviderWriteDisabled`; read operations return
    a bounded ``unavailable`` result (there is no real provider).  This is the
    default adapter type the broker falls back to when no fixture is supplied,
    so a request can never silently reach a real provider in acceptance mode.
    """

    provider_name = "external"
    write_enabled = False

    def read_repository(self, request) -> ProviderResult:
        return ProviderResult(OUTCOME_UNAVAILABLE, detail="no provider")

    def read_ref(self, request) -> ProviderResult:
        return ProviderResult(OUTCOME_UNAVAILABLE, detail="no provider")

    def read_pull_request(self, request) -> ProviderResult:
        return ProviderResult(OUTCOME_UNAVAILABLE, detail="no provider")

    def read_checks(self, request) -> ProviderResult:
        return ProviderResult(OUTCOME_UNAVAILABLE, detail="no provider")

    def observe(self, request) -> ProviderObservation:
        return ProviderObservation(found=False)


# ---------------------------------------------------------------------------
# FakeGitHubAdapter — deterministic in-memory fixture (offline I3-A tests)
# ---------------------------------------------------------------------------

class FakeGitHubAdapter(ExternalProviderAdapter):
    """Deterministic, scriptable in-memory GitHub fixture.

    Maintains a tiny object model: branches (name -> SHA), pull requests (by
    number), and check runs.  Mutation operations are ENABLED here (fixture)
    and simulate GitHub semantics (fast-forward push only; duplicate PR
    detection keyed by ``(head_sha, repo, head_branch)`` with an Argent-owner
    marker; PR state transitions).  ``observe`` implements the reconciliation
    probes the broker uses to detect crash-after-provider-success:

    * push: ``found`` iff the remote ref now equals the expected SHA;
    * create PR: ``found`` iff an Argent-owned PR for the same head exists.

    Scripting hooks (``script_outcomes`` / ``fail_next``) let tests inject
    deterministic failures (rate-limit, conflict, unavailable, …).  No network.
    """

    def __init__(self, provider_name: str = "github", *, write_enabled: bool = True):
        self.provider_name = provider_name
        self.write_enabled = write_enabled
        self.branches: dict = {}          # branch -> sha
        self.branch_repos: dict = {}      # branch -> repository (reconcil bind)
        self.pull_requests: dict = {}     # pr_number -> dict
        self._next_pr_number = 1
        self.check_runs: dict = {}        # (repo, ref) -> list[dict]
        self.script_outcomes: dict = {}   # action -> [outcome, ...]
        self.fail_next: Optional[BaseException] = None
        self.calls: list = []             # record of every dispatched action

    # -- scripting helpers ---------------------------------------------------

    def set_branch(self, branch: str, sha: str, *, repo: Optional[str] = None) -> None:
        self.branches[branch] = sha
        if repo is not None:
            self.branch_repos[branch] = repo

    def script(self, action: str, outcomes) -> None:
        self.script_outcomes[action] = list(outcomes)

    def _next_outcome(self, action: str) -> Optional[str]:
        q = self.script_outcomes.get(action)
        if q:
            return q.pop(0)
        return None

    def _record(self, action: str, request) -> None:
        self.calls.append({"action": action, "request_id": request.request_id})

    def _raise_if_fail(self) -> None:
        if self.fail_next is not None:
            exc = self.fail_next
            self.fail_next = None
            raise exc

    def _outcome_or(self, action: str, default: str) -> str:
        self._raise_if_fail()
        return self._next_outcome(action) or default

    # -- read operations -----------------------------------------------------

    def read_repository(self, request) -> ProviderResult:
        self._record("read_repository", request)
        return ProviderResult(OUTCOME_SUCCESS, state={"repository": request.repository})

    def read_ref(self, request) -> ProviderResult:
        self._record("read_ref", request)
        ref = request.parameters.get("ref", "")
        sha = self.branches.get(ref)
        if sha is None:
            return ProviderResult(OUTCOME_VALIDATION_FAILED, detail="unknown ref")
        return ProviderResult(OUTCOME_SUCCESS, object_id=sha,
                              state={"ref": ref, "sha": sha})

    def read_pull_request(self, request) -> ProviderResult:
        self._record("read_pull_request", request)
        n = request.parameters.get("number")
        pr = self.pull_requests.get(n)
        if pr is None:
            return ProviderResult(OUTCOME_VALIDATION_FAILED, detail="unknown PR")
        return ProviderResult(OUTCOME_SUCCESS, object_id=str(n), state=dict(pr))

    def read_checks(self, request) -> ProviderResult:
        self._record("read_checks", request)
        ref = request.parameters.get("ref", "")
        runs = list(self.check_runs.get((request.repository, ref), []))
        return ProviderResult(OUTCOME_SUCCESS, state={"runs": runs})

    # -- mutation operations (fixture: enabled) ------------------------------

    def push_feature_branch(self, request) -> ProviderResult:
        self._record("push_feature_branch", request)
        outcome = self._outcome_or("push_feature_branch", OUTCOME_SUCCESS)
        if outcome != OUTCOME_SUCCESS:
            return ProviderResult(outcome, detail=f"push {outcome}")
        branch = request.parameters["branch"]
        sha = request.parameters["sha"]
        if not self._is_sha(sha):
            return ProviderResult(OUTCOME_VALIDATION_FAILED, detail="invalid sha")
        existing = self.branches.get(branch)
        if existing is not None and existing != sha:
            return ProviderResult(OUTCOME_CONFLICT, detail="non-fast-forward")
        self.branches[branch] = sha
        self.branch_repos[branch] = request.repository
        return ProviderResult(OUTCOME_SUCCESS, object_id=sha,
                              state={"remote_ref": branch, "sha": sha})

    def create_pull_request(self, request) -> ProviderResult:
        self._record("create_pull_request", request)
        outcome = self._outcome_or("create_pull_request", OUTCOME_SUCCESS)
        if outcome != OUTCOME_SUCCESS:
            return ProviderResult(outcome, detail=f"create_pr {outcome}")
        head_branch = request.parameters["head_branch"]
        base_branch = request.parameters["base_branch"]
        head_sha = request.parameters.get("head_sha")
        # Duplicate detection: an Argent-owned PR for the same (repo, head
        # branch, head SHA, base, idempotency key) already exists -> return it
        # (no duplicate).
        for n, pr in self.pull_requests.items():
            if (pr.get("head_branch") == head_branch
                    and pr.get("base_branch") == base_branch
                    and pr.get("head_sha") == head_sha
                    and pr.get("repo") == request.repository
                    and pr.get("idempotency_key") == request.idempotency_key
                    and pr.get("argent_owned") is True):
                return ProviderResult(OUTCOME_SUCCESS, object_id=str(n),
                                      state=dict(pr))
        n = self._next_pr_number
        self._next_pr_number += 1
        pr = {
            "number": n,
            "repo": request.repository,
            "head_branch": head_branch,
            "base_branch": base_branch,
            "head_sha": head_sha,
            "idempotency_key": request.idempotency_key,
            "title": request.parameters.get("title", ""),
            "body": request.parameters.get("body", ""),
            "state": "open",
            "argent_owned": True,
        }
        self.pull_requests[n] = pr
        return ProviderResult(OUTCOME_SUCCESS, object_id=str(n), state=dict(pr))

    def update_pull_request(self, request) -> ProviderResult:
        self._record("update_pull_request", request)
        outcome = self._outcome_or("update_pull_request", OUTCOME_SUCCESS)
        if outcome != OUTCOME_SUCCESS:
            return ProviderResult(outcome, detail=f"update_pr {outcome}")
        n = request.parameters.get("number")
        pr = self.pull_requests.get(n)
        if pr is None:
            return ProviderResult(OUTCOME_VALIDATION_FAILED, detail="unknown PR")
        for key in ("title", "body"):
            if key in request.parameters:
                pr[key] = request.parameters[key]
        return ProviderResult(OUTCOME_SUCCESS, object_id=str(n), state=dict(pr))

    # -- reconciliation probe ------------------------------------------------

    def observe(self, request) -> ProviderObservation:
        action = request.action
        if action == "push_feature_branch":
            branch = request.parameters.get("branch")
            sha = request.parameters.get("sha")
            if self.branches.get(branch) == sha:
                # (HIGH-4) match repository + ref + expected SHA.  When a repo
                # was recorded (push/set_branch), it must match the request's
                # repository.
                repo = self.branch_repos.get(branch)
                if repo is None or repo == request.repository:
                    return ProviderObservation(
                        found=True, object_id=sha,
                        state={"remote_ref": branch, "sha": sha})
            return ProviderObservation(found=False)
        if action == "create_pull_request":
            head_branch = request.parameters.get("head_branch")
            base_branch = request.parameters.get("base_branch")
            head_sha = request.parameters.get("head_sha")
            for n, pr in self.pull_requests.items():
                if (pr.get("head_branch") == head_branch
                        and pr.get("base_branch") == base_branch
                        and pr.get("head_sha") == head_sha
                        and pr.get("repo") == request.repository
                        and pr.get("idempotency_key") == request.idempotency_key
                        and pr.get("argent_owned") is True):
                    return ProviderObservation(found=True, object_id=str(n),
                                               state=dict(pr))
            return ProviderObservation(found=False)
        # Reads / other actions have no persistent provider object to reconcile.
        return ProviderObservation(found=False)

    @staticmethod
    def _is_sha(sha: str) -> bool:
        return (isinstance(sha, str) and len(sha) == 40
                and all(c in "0123456789abcdef" for c in sha))
