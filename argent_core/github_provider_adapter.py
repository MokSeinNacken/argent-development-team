"""Phase I3-B — real GitHub provider adapter (live-acceptance ready).

Implements the I3-A :class:`~argent_core.external_provider_adapter.
ExternalProviderAdapter` contract against the real GitHub via ``gh`` / ``git``
argv subprocesses.  This is the single translation point between the broker's
bounded :class:`~argent_core.external_action_broker.ExternalActionRequest` and
GitHub's object model.  It is **LOCAL-ONLY** here: every real external write is
gated behind the deterministic live-write activation gate (CASE 1) and is
performed later by Main through the broker — this module never activates live
writes on its own and performs no network write in I3-B acceptance.

Hard guarantees (code-enforced, tested):

* **argv-only**: every operation runs ``subprocess`` with an explicit argv list
  (no shell, no ``eval``/``exec``); executables are injectable for tests
  (default ``gh`` for reads/PR ops, ``git`` for push).
* **NO-WRITE default**: mutation methods raise
  :class:`ProviderWriteDisabled` unless the adapter is live-enabled AND the
  deterministic live-write activation gate passes (CASE 1).
* **Trusted push URL**: the push remote is taken from TRUSTED repo metadata
  (an injected ``trusted_repo_urls`` mapping keyed by canonical repo identity);
  the repository identity is canonicalized and validated against that map —
  an agent-supplied URL is never accepted (CASE 4/5/6).
* **Credential handling is controller-side**: the controller injects any
  credential via the ``env`` mapping (never embedded in argv); the adapter
  never reads, prints, or logs credential VALUES.
* **Closed failure classification**: provider/transport failures map to the
  bounded I3-A taxonomy (403 → credential/policy, 429 → rate-limit, 5xx →
  unavailable, transport → network), never leaking tokens into error text.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from typing import Mapping, Optional

from .external_action_broker import (
    AllowlistEntry,
    ExternalActionAllowlist,
    StandingPolicy,
    validate_pr_body,
    validate_pr_title,
)
from .external_provider_adapter import (
    OUTCOME_CREDENTIAL_ERROR,
    OUTCOME_SUCCESS,
    OUTCOME_UNAVAILABLE,
    OUTCOME_VALIDATION_FAILED,
    ExternalProviderAdapter,
    ProviderConflict,
    ProviderCredentialError,
    ProviderError,
    ProviderNetworkError,
    ProviderObservation,
    ProviderRateLimited,
    ProviderResult,
    ProviderUnavailable,
    ProviderValidationError,
    ProviderWriteDisabled,
)
from .worktree import is_sha_like, validate_branch_identity, validate_repo_identity


# ---------------------------------------------------------------------------
# Live-write activation gate (I3-B CASE 1/2)
# ---------------------------------------------------------------------------

#: The I3-A GREEN commit — the minimum code revision carrying the
#: credential-mask fix required before ANY live GitHub write may activate.
LIVE_WRITE_REQUIRED_COMMIT = "ffc266421ca0d53d1a5a7c2d078194f88e65868b"

#: The canonical I3-A marker the required-commit must equal.  Tests patch
#: :data:`LIVE_WRITE_REQUIRED_COMMIT` to a pre-I3-A value to prove the gate
#: fails closed when the deployed code predates the credential-mask fix.
_I3A_CREDENTIAL_MASK_MARKER = "ffc266421ca0d53d1a5a7c2d078194f88e65868b"


def credential_mask_fix_present() -> bool:
    """True iff the running code carries the I3-A credential-mask resolver.

    The I3-A credential-isolation fix added
    :func:`argent_core.execution_scope.resolve_credential_mask_paths` (which
    masks ``~/.config/gh`` inside the agent sandbox).  Its presence is a
    deterministic proxy for "this code cannot leak the GitHub credential to an
    untrusted same-UID role agent", which is a hard prerequisite for any real
    GitHub write.
    """
    from . import execution_scope

    return callable(getattr(execution_scope, "resolve_credential_mask_paths", None))


def live_write_gate(activation_flag: bool, *,
                    resolver_present: Optional[bool] = None) -> bool:
    """Deterministic live-write activation gate (I3-B CASE 1).

    Live write requires BOTH:

    (a) an explicit activation flag (the controller opts in), AND
    (b) the running code carries the I3-A credential-mask fix — enforced by
        (i) the credential-mask resolver being present AND (ii) the required
        minimum commit marker equalling the I3-A marker.

    Fails closed: a missing flag, a missing resolver, or a marker pinned to a
    pre-I3-A commit all return ``False`` (no live write).
    """
    if not activation_flag:
        return False
    if resolver_present is None:
        resolver_present = credential_mask_fix_present()
    if not resolver_present:
        return False
    return LIVE_WRITE_REQUIRED_COMMIT == _I3A_CREDENTIAL_MASK_MARKER


# ---------------------------------------------------------------------------
# Acceptance GitHub identity (owner-controlled, NOT a fork)
# ---------------------------------------------------------------------------

GITHUB_ACCEPTANCE_PROVIDER = "github"
GITHUB_ACCEPTANCE_ACCOUNT = "MokSeinNacken"
GITHUB_ACCEPTANCE_REPOSITORY = "MokSeinNacken/argent-development-team"
GITHUB_ACCEPTANCE_CANONICAL_URL = (
    "https://github.com/MokSeinNacken/argent-development-team.git")
GITHUB_ACCEPTANCE_BRANCH_NAMESPACE = "argent/"
GITHUB_ACCEPTANCE_PR_BASE = "main"

#: Acceptance autonomous classes (4 reads + 2 bounded writes).  SENSITIVE
#: actions (merge/release/deploy) are PERMITTED but never autonomous — they
#: reach the broker's SENSITIVE gate and return OWNER_GATE_REQUIRED.
GITHUB_ACCEPTANCE_AUTONOMOUS_ACTIONS = frozenset({
    "read_repository", "read_ref", "read_pull_request", "read_checks",
    "push_feature_branch", "create_pull_request",
})

#: Acceptance permitted actions = autonomous + SENSITIVE (owner-gateable).
GITHUB_ACCEPTANCE_PERMITTED_ACTIONS = frozenset(
    GITHUB_ACCEPTANCE_AUTONOMOUS_ACTIONS | {
        "merge_pull_request", "create_release", "deploy_production",
    })


# ---------------------------------------------------------------------------
# Repository identity canonicalization (CASE 6)
# ---------------------------------------------------------------------------

def canonicalize_repo_identity(repo: str) -> str:
    """Canonicalize a repository identity for identity comparison.

    Strips a trailing ``.git``, trailing slashes, and any ``https://github.com/``
    / ``git@github.com:`` prefix, returning the canonical ``owner/repo`` form.
    Non-string input fails closed (returns the coerced, stripped value — callers
    validate further).
    """
    if not isinstance(repo, str):
        repo = str(repo)
    r = repo.strip()
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if r.startswith(prefix):
            r = r[len(prefix):]
            break
    r = r.rstrip("/")
    if r.endswith(".git"):
        r = r[:-4]
    return r


def _validate_repo_identity(repo) -> str:
    try:
        validated = validate_repo_identity(repo)
    except ValueError as exc:
        raise ProviderValidationError(f"invalid repository identity: {exc}") from exc
    if not validated:
        raise ProviderValidationError("repository identity is empty")
    return validated


def _validate_ref_and_sha(branch, sha) -> None:
    """Defense-in-depth ref/sha validation (never weaker than the broker's)."""
    try:
        validate_branch_identity(branch)
    except ValueError as exc:
        raise ProviderValidationError(f"invalid branch: {exc}") from exc
    if any(tok in branch for tok in ("..", "~", "^", ":", "@{", " ")):
        raise ProviderValidationError("branch contains a reserved revision token")
    if not is_sha_like(sha):
        raise ProviderValidationError("sha must be a full sha")


# ---------------------------------------------------------------------------
# Provider failure classification (CASE 23/24)
# ---------------------------------------------------------------------------

def classify_gh_failure(returncode: Optional[int], stderr: str = "") -> ProviderError:
    """Map a ``gh``/``git`` subprocess failure to a bounded I3-A ProviderError.

    Deterministic, closed mapping:

    * 401 → credential (authentication failed);
    * 403 → credential (token-permission / authorization), OR validation where
      the remote's policy rejection is observable in stderr (protected branch /
      required status / policy);
    * 409 / non-fast-forward / ``fetch first`` / ``rejected`` → conflict;
    * 400 / 422 → validation;
    * 429 (or a rate-limit message) → rate-limited;
    * >= 500 → provider unavailable;
    * unknown non-zero → unavailable (retryable, fail-safe — never a crash).
    """
    low = (stderr or "").lower()
    if returncode is None:
        return ProviderUnavailable("provider operation failed")
    if returncode == 429 or "rate limit" in low or "secondary rate limit" in low:
        return ProviderRateLimited("rate limited")
    if returncode == 401:
        return ProviderCredentialError("authentication failed (401)")
    if returncode == 403:
        if "protected branch" in low or "required status" in low or "policy" in low:
            return ProviderValidationError("remote policy rejected the write (403)")
        return ProviderCredentialError("authorization/permission denied (403)")
    if (returncode == 409 or "non-fast-forward" in low or "fetch first" in low
            or "rejected" in low or "conflict" in low):
        return ProviderConflict("conflict (non-fast-forward / rejected)")
    if returncode in (400, 422):
        return ProviderValidationError(f"remote validation failed ({returncode})")
    if returncode >= 500:
        return ProviderUnavailable(f"provider unavailable ({returncode})")
    return ProviderUnavailable("provider operation failed")


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------

class GitHubProviderAdapter(ExternalProviderAdapter):
    """Real GitHub adapter (argv subprocesses; NO-WRITE by default).

    ``write_enabled`` is derived from :func:`live_write_gate` at construction:
    it is ``True`` only when the controller passes ``live_write=True`` AND the
    running code carries the I3-A credential-mask fix.  Mutation methods also
    re-assert ``write_enabled`` (defense in depth — CASE 13).
    """

    def __init__(
        self,
        provider_name: str = "github",
        *,
        live_write: bool = False,
        gh_executable: str = "gh",
        git_executable: str = "git",
        trusted_repo_urls: Optional[Mapping[str, str]] = None,
        env: Optional[Mapping[str, str]] = None,
        run=subprocess.run,
        gate=live_write_gate,
        cwd: Optional[str] = None,
    ):
        self.provider_name = provider_name
        self.gh_executable = gh_executable
        self.git_executable = git_executable
        self._trusted_repo_urls = dict(trusted_repo_urls or {})
        self._env = dict(env) if env else None
        self._run = run
        self._cwd = cwd
        self._gate = gate
        self.write_enabled = bool(self._gate(live_write))
        #: argv record for deterministic tests (NEVER env / credential values).
        self.invocations: list = []

    # -- helpers -------------------------------------------------------------

    def _assert_write_enabled(self, op: str) -> None:
        if not self.write_enabled:
            raise ProviderWriteDisabled(
                f"{type(self).__name__} is not live-write enabled for {op!r} "
                f"(live-write gate did not pass)")

    def _trusted_push_url(self, repository: str) -> str:
        """Resolve the TRUSTED push URL for a repository (never agent-supplied)."""
        canonical = canonicalize_repo_identity(repository)
        url = self._trusted_repo_urls.get(canonical)
        if url is None:
            raise ProviderValidationError(
                f"repository {canonical!r} is not an allowlisted push target")
        if not isinstance(url, str) or not url:
            raise ProviderValidationError("trusted push URL is empty")
        return url

    def _exec(self, argv, *, executable_kind: str = "gh"):
        """Run an argv subprocess (NO shell), record argv, classify failure."""
        self.invocations.append(list(argv))
        env = None
        if self._env:
            env = dict(os.environ)
            env.update(self._env)
        try:
            proc = self._run(argv, capture_output=True, text=True, env=env,
                             cwd=self._cwd, timeout=60)
        except FileNotFoundError as exc:
            raise ProviderNetworkError(
                f"{executable_kind} executable not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderNetworkError(f"{executable_kind} timed out") from exc
        except OSError as exc:  # noqa: BLE001 - transport failure surface
            raise ProviderNetworkError(
                f"transport failure: {type(exc).__name__}") from exc
        if proc.returncode != 0:
            raise classify_gh_failure(proc.returncode, proc.stderr or "")
        return proc

    def _json(self, proc, default):
        try:
            return json.loads(proc.stdout or "")
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _parse_pr_number(proc, expected_repo: Optional[str] = None) -> Optional[int]:
        """Extract a created PR number from ``gh pr create`` output.

        ``gh pr create`` has no ``--json`` flag: on success (including the
        existing-PR reconciliation case) it prints the PR URL, e.g.
        ``https://github.com/owner/repo/pull/123``.  Parse defensively: a JSON
        dict with ``number`` first, then a ``pull/<digits>`` URL in
        stdout/stderr.  The URL MUST belong to ``expected_repo`` (canonical
        ``owner/repo``) — a PR URL for any other repository is ignored (the
        caller fails closed instead of claiming an unidentified/foreign
        SUCCESS).  Returns ``None`` when no bound number is observable.
        """
        try:
            data = json.loads(proc.stdout or "")
        except (ValueError, TypeError):
            data = {}
        if isinstance(data, dict) and data.get("number") is not None:
            try:
                return int(data["number"])
            except (TypeError, ValueError):
                return None
        # ``pull/<digits>`` URLs are only trusted when they belong to the
        # EXPECTED repository (LOW-14): an agent/foreign URL can never bind.
        expected_canonical = (canonicalize_repo_identity(expected_repo)
                              if expected_repo is not None else None)
        for text in ((proc.stdout or ""), (proc.stderr or "")):
            m = re.search(
                r"(?:https?://github\.com/|git@github\.com:)"
                r"([^/\s]+)/([^/\s]+)/pull/([0-9]+)", text)
            if m:
                owner, repo, number = m.group(1), m.group(2), int(m.group(3))
                if expected_canonical is not None:
                    candidate = canonicalize_repo_identity(f"{owner}/{repo}")
                    if candidate != expected_canonical:
                        continue  # foreign repository — never bind (fail closed)
                return number
        return None

    # -- read operations -----------------------------------------------------

    def read_repository(self, request) -> ProviderResult:
        repo = _validate_repo_identity(request.repository)
        argv = [self.gh_executable, "repo", "view", repo,
                "--json", "name,defaultBranchRef"]
        proc = self._exec(argv, executable_kind="gh")
        data = self._json(proc, {})
        default_branch = ""
        dbr = (data or {}).get("defaultBranchRef") or {}
        if isinstance(dbr, dict):
            default_branch = dbr.get("name", "")
        return ProviderResult(OUTCOME_SUCCESS,
                              state={"repository": repo,
                                     "default_branch": default_branch})

    def read_ref(self, request) -> ProviderResult:
        repo = _validate_repo_identity(request.repository)
        ref = request.parameters.get("ref") or request.parameters.get("branch") \
            or request.resource_ref
        if not ref:
            raise ProviderValidationError("read_ref requires a ref/branch")
        argv = [self.gh_executable, "api", f"repos/{repo}/commits/{ref}",
                "--jq", ".sha"]
        proc = self._exec(argv, executable_kind="gh")
        sha = (proc.stdout or "").strip()
        if not is_sha_like(sha):
            raise ProviderValidationError("read_ref returned a non-sha value")
        return ProviderResult(OUTCOME_SUCCESS, object_id=sha,
                              state={"ref": ref, "sha": sha})

    def read_pull_request(self, request) -> ProviderResult:
        repo = _validate_repo_identity(request.repository)
        number = request.parameters.get("number")
        if number is not None:
            argv = [self.gh_executable, "pr", "view", str(number),
                    "--repo", repo,
                    "--json", "number,headRefName,baseRefName,headRefOid,"
                              "title,body,state"]
            proc = self._exec(argv, executable_kind="gh")
            data = self._json(proc, {})
            if not data:
                return ProviderResult(OUTCOME_VALIDATION_FAILED,
                                      detail="unknown PR")
            return ProviderResult(OUTCOME_SUCCESS, object_id=str(number),
                                  state=data)
        head_branch = request.parameters.get("head_branch")
        if head_branch:
            argv = [self.gh_executable, "pr", "list", "--repo", repo,
                    "--head", head_branch, "--state", "open", "--limit", "100",
                    "--json", "number,headRefName,baseRefName,headRefOid,"
                              "title,body,state"]
            proc = self._exec(argv, executable_kind="gh")
            data = self._json(proc, [])
            prs = data if isinstance(data, list) else []
            first = prs[0] if prs else None
            return ProviderResult(
                OUTCOME_SUCCESS,
                object_id=str(first.get("number")) if first else None,
                state={"prs": prs, "head_branch": head_branch})
        raise ProviderValidationError(
            "read_pull_request requires a number or head_branch")

    def read_checks(self, request) -> ProviderResult:
        repo = _validate_repo_identity(request.repository)
        ref = request.parameters.get("ref") or request.parameters.get("branch") \
            or request.resource_ref or ""
        argv = [self.gh_executable, "api",
                f"repos/{repo}/commits/{ref}/check-runs", "--jq", ".check_runs"]
        try:
            proc = self._exec(argv, executable_kind="gh")
        except ProviderError:
            # Best-effort: checks are advisory — degrade to empty, never fail.
            return ProviderResult(OUTCOME_SUCCESS, state={"runs": []})
        runs = self._json(proc, [])
        if not isinstance(runs, list):
            runs = []
        return ProviderResult(OUTCOME_SUCCESS, state={"runs": runs})

    # -- mutation operations (live-write gated) ------------------------------

    def _local_ref_sha(self, branch: str) -> Optional[str]:
        """Resolve the LOCAL ``refs/heads/<branch>`` to a sha (fail-closed).

        Runs ``git rev-parse --verify`` (argv, no shell).  A transport failure
        (spawn/timeout/OS) propagates as :class:`ProviderNetworkError`; a
        missing/invalid local ref (non-zero exit or non-sha output) returns
        ``None`` so the caller fails closed (the local ref is NOT at the bound
        sha).  Never treats an unverifiable local ref as correct.
        """
        argv = [self.git_executable, "rev-parse", "--verify",
                f"refs/heads/{branch}"]
        try:
            proc = self._exec(argv, executable_kind="git")
        except ProviderNetworkError:
            raise  # real transport failure — never conflated with a mismatch
        except ProviderError:
            return None  # missing/invalid local ref
        sha = (proc.stdout or "").strip()
        return sha if is_sha_like(sha) else None

    def _remote_ref_sha_strict(self, repository, branch) -> Optional[str]:
        """Read the REMOTE ``refs/heads/<branch>`` sha (strict, fail-closed).

        Runs ``git ls-remote <trusted-url> refs/heads/<branch>``.  Transport
        failures propagate as :class:`ProviderNetworkError`; an absent/unknown
        ref returns ``None``.  Used by the mutation boundary checks that MUST
        not conflate "ref differs" with "could not read the ref".
        """
        url = self._trusted_push_url(repository)
        argv = [self.git_executable, "ls-remote", url, f"refs/heads/{branch}"]
        try:
            proc = self._exec(argv, executable_kind="git")
        except ProviderNetworkError:
            raise
        except ProviderError:
            return None
        line = (proc.stdout or "").strip()
        if not line:
            return None
        sha = line.split()[0]
        return sha if is_sha_like(sha) else None

    def push_feature_branch(self, request) -> ProviderResult:
        self._assert_write_enabled("push_feature_branch")
        branch = request.parameters["branch"]
        sha = request.parameters["sha"]
        _validate_ref_and_sha(branch, sha)
        # The remote URL comes from TRUSTED repo metadata (CASE 4/5/6) — never
        # an agent-supplied URL.
        url = self._trusted_push_url(request.repository)
        # (HIGH-6a) verify the LOCAL ref actually points at the BOUND sha
        # BEFORE any push.  A mismatch/stale/missing local ref fails closed
        # (conflict) with NO push invocation.
        local = self._local_ref_sha(branch)
        if local != sha:
            raise ProviderConflict(
                f"local ref refs/heads/{branch} does not point at the bound sha")
        refspec = f"refs/heads/{branch}:refs/heads/{branch}"
        argv = [self.git_executable, "push", url, refspec]
        self._exec(argv, executable_kind="git")
        # (HIGH-6b) read the remote ref back and REQUIRE equality with the
        # bound sha before claiming success — a race/stale mirror is a conflict,
        # never a fabricated success.
        remote = self._remote_ref_sha_strict(request.repository, branch)
        if remote != sha:
            raise ProviderConflict(
                f"remote ref refs/heads/{branch} is not the bound sha "
                f"(race/stale mirror)")
        return ProviderResult(OUTCOME_SUCCESS, object_id=sha,
                              state={"remote_ref": branch, "sha": sha})

    def create_pull_request(self, request) -> ProviderResult:
        self._assert_write_enabled("create_pull_request")
        head_branch = request.parameters["head_branch"]
        base_branch = request.parameters["base_branch"]
        head_sha = request.parameters.get("head_sha")
        _validate_ref_and_sha(head_branch, head_sha or "")
        _validate_ref_and_sha(base_branch, head_sha or "")
        # Conservative head/base mismatch guard (CASE 18).
        if head_branch == base_branch:
            raise ProviderValidationError("PR head and base branches must differ")
        # Defense-in-depth publication safety (CASE 19): re-validate the
        # already broker-sanitized title/body (idempotent).  Map any
        # publication-safety ValueError to a bounded provider validation
        # error (never a raw ValueError leaking to the broker's generic path).
        try:
            title = validate_pr_title(request.parameters.get("title", ""))
            body = validate_pr_body(request.parameters.get("body", ""))
        except ValueError as exc:
            raise ProviderValidationError(str(exc)) from exc
        repo = _validate_repo_identity(request.repository)
        # (HIGH-6c) verify the REMOTE head ref equals head_sha BEFORE creating
        # the PR — a stale/mismatched mirror fails closed (never a PR against
        # an unverified head).
        remote_head = self._remote_ref_sha_strict(repo, head_branch)
        if remote_head != head_sha:
            raise ProviderConflict(
                f"remote head ref refs/heads/{head_branch} is not head_sha")
        argv = [self.gh_executable, "pr", "create", "--repo", repo,
                "--head", head_branch, "--base", base_branch,
                "--title", title]
        # Body via a bounded temp file (argv-length safe, no shell).
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                         encoding="utf-8") as tf:
            tf.write(body)
            body_path = tf.name
        try:
            argv = argv + ["--body-file", body_path]
            proc = self._exec(argv, executable_kind="gh")
        finally:
            try:
                os.unlink(body_path)
            except OSError:
                pass
        number = self._parse_pr_number(proc, expected_repo=repo)
        if number is None:
            # The provider accepted the action but Argent cannot identify the
            # created object (or the URL belongs to a foreign repo): fail
            # closed (never an unidentified/foreign SUCCESS).
            raise ProviderValidationError(
                "created PR number not observable from gh output")
        return ProviderResult(
            OUTCOME_SUCCESS, object_id=str(number),
            state={"repo": repo, "head_branch": head_branch,
                   "base_branch": base_branch, "head_sha": head_sha,
                   "title": title, "number": number})

    def update_pull_request(self, request) -> ProviderResult:
        self._assert_write_enabled("update_pull_request")
        number = request.parameters["number"]
        repo = _validate_repo_identity(request.repository)
        # Own PR only: refuse to mutate a PR not owned by the acceptance account.
        author = self._pr_author(repo, number)
        if author and author != GITHUB_ACCEPTANCE_ACCOUNT:
            raise ProviderValidationError(
                "refusing to update a PR not owned by the acceptance account")
        argv = [self.gh_executable, "pr", "edit", str(number), "--repo", repo]
        try:
            if "title" in request.parameters:
                argv += ["--title", validate_pr_title(request.parameters["title"])]
            if "body" in request.parameters:
                argv += ["--body", validate_pr_body(request.parameters["body"])]
        except ValueError as exc:
            raise ProviderValidationError(str(exc)) from exc
        self._exec(argv, executable_kind="gh")
        return ProviderResult(OUTCOME_SUCCESS, object_id=str(number))

    def _pr_author(self, repo, number) -> Optional[str]:
        argv = [self.gh_executable, "pr", "view", str(number), "--repo", repo,
                "--json", "author"]
        try:
            proc = self._exec(argv, executable_kind="gh")
        except ProviderError:
            return None
        data = self._json(proc, {})
        author = (data or {}).get("author") or {}
        if isinstance(author, dict):
            return author.get("login")
        return None

    # -- reconciliation probe ------------------------------------------------

    def observe(self, request) -> ProviderObservation:
        action = request.action
        if action == "push_feature_branch":
            branch = request.parameters.get("branch")
            sha = request.parameters.get("sha")
            remote = self._remote_ref_sha(request.repository, branch)
            if remote and remote == sha:
                return ProviderObservation(
                    found=True, object_id=sha,
                    state={"remote_ref": branch, "sha": sha})
            return ProviderObservation(found=False)
        if action == "create_pull_request":
            head_branch = request.parameters.get("head_branch")
            base_branch = request.parameters.get("base_branch")
            head_sha = request.parameters.get("head_sha")
            pr = self._find_own_pr(request.repository, head_branch,
                                   base_branch, head_sha)
            if pr is not None:
                return ProviderObservation(
                    found=True, object_id=str(pr.get("number")), state=pr)
            return ProviderObservation(found=False)
        return ProviderObservation(found=False)

    def _remote_ref_sha(self, repository, branch) -> Optional[str]:
        # Best-effort reconciliation read: any failure degrades to None (the
        # observation is "not found"), never crashes the reconcile path.
        try:
            return self._remote_ref_sha_strict(repository, branch)
        except ProviderError:
            return None

    def _find_own_pr(self, repository, head_branch, base_branch,
                     head_sha) -> Optional[dict]:
        repo = _validate_repo_identity(repository)
        argv = [self.gh_executable, "pr", "list", "--repo", repo,
                "--head", head_branch, "--state", "open", "--limit", "100",
                "--json", "number,headRefName,baseRefName,headRefOid,author"]
        try:
            proc = self._exec(argv, executable_kind="gh")
        except ProviderError:
            return None
        data = self._json(proc, [])
        prs = data if isinstance(data, list) else []
        for pr in prs:
            # (LOW-14) fail closed on a missing/non-dict author: a PR whose
            # author is unknown is NEVER treated as Argent-owned.
            author = pr.get("author") if isinstance(pr, dict) else None
            if not isinstance(author, dict) or \
                    author.get("login") != GITHUB_ACCEPTANCE_ACCOUNT:
                continue
            if (pr.get("head_branch") or pr.get("headRefName")) != head_branch:
                continue
            if (pr.get("base_branch") or pr.get("baseRefName")) != base_branch:
                continue
            if (pr.get("head_sha") or pr.get("headRefOid")) != head_sha:
                continue
            return pr
        return None


# ---------------------------------------------------------------------------
# Task-scoped acceptance allowlist / standing policy builders (CASE 7/8)
# ---------------------------------------------------------------------------

def github_acceptance_allowlist(
    *,
    repository: str = GITHUB_ACCEPTANCE_REPOSITORY,
    branch_namespace: str = GITHUB_ACCEPTANCE_BRANCH_NAMESPACE,
    pr_base: str = GITHUB_ACCEPTANCE_PR_BASE,
    actions: Optional[frozenset] = None,
) -> ExternalActionAllowlist:
    """Build the I3-B task-scoped GitHub acceptance allowlist (CASE 7/8).

    Exact-match only (no wildcards): provider ``github``, account
    ``MokSeinNacken``, the single owner-controlled repository, the autonomous
    read/bounded-write classes plus the owner-gateable SENSITIVE classes, the
    ``argent/`` branch namespace, and PR base ``main``.  Anything else — a
    different repo, a different branch namespace, a different PR target — is
    DENY.  This is a BUILDER (usable by Main's live flow and by tests); it is
    never activated as a persistent standing policy.
    """
    return ExternalActionAllowlist(entries=(AllowlistEntry(
        provider=GITHUB_ACCEPTANCE_PROVIDER,
        account=GITHUB_ACCEPTANCE_ACCOUNT,
        repositories=frozenset({repository}),
        permitted_actions=frozenset(
            actions if actions is not None else GITHUB_ACCEPTANCE_PERMITTED_ACTIONS),
        branch_namespaces=frozenset({branch_namespace}),
        pr_targets=frozenset({pr_base}),
    ),))


def github_acceptance_standing_policy() -> StandingPolicy:
    """Standing policy granting the acceptance BOUNDED_WRITE actions.

    Only ``push_feature_branch`` and ``create_pull_request`` are autonomous
    writes; reads are always autonomous; SENSITIVE (merge/release/deploy) stays
    OWNER_GATE_REQUIRED (never in ``autonomous_actions``).
    """
    return StandingPolicy(autonomous_actions=frozenset({
        "push_feature_branch", "create_pull_request",
    }))
