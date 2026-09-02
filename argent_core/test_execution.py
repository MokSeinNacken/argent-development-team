"""Phase F2 — staged deterministic test execution + safe evidence reuse.

Consumes the F1 planner (``argent_core.test_planning``) directly and does NOT
introduce a second test-selection authority.  ``execute_plan`` runs exactly the
stages present in an F1 :class:`~argent_core.test_planning.TestPlan` in order,
stops on a genuine early failure, and only ever reuses a previous result when
the evidence identity matches **exactly** (same immutable code snapshot, same
selector, same test definitions, same plan/inventory/policy hashes, same
executor) AND the evidence carries a valid keyed MAC (authenticated provenance).

Fix-Round F1–F9 (see docs/PHASE_F2_NOTES.md § Fix-Round):

- F1: glob selectors are resolved deterministically (sorted, against the real
  filesystem, allowed roots only); zero-match is fail-closed TEST_INFRA_FAILURE.
- F2: ``compute_snapshot_identity`` now really walks ``extra_roots`` (default
  ``e2e-fixture``), resolves directory/file symlinks safely, excludes
  bytecode/artifacts, and binds external pytest config as a config identity.
- F3: ``execute_plan`` fail-closes on malformed/tampered plans (authentic
  ``plan_hash`` re-verified + strict stage shape/order/uniqueness).
- F4: ``evidence_hash`` is now an HMAC-SHA256 over the canonical identity/result
  fields with a key that is *not* in the agent write area (env var / file,
  injectable).  Evidence without a valid MAC is rejected, never reused.
- F5: the runner binds a canonical project root and runs with ``cwd=root``; the
  snapshot root is validated against the execution root (fail-closed).
- F6: no resource gate => fail-closed BLOCKED (no silent AllowAll default);
  documented bridge to the real Phase-C ResourceGovernor/ExecutionEnforcer.
- F7: bounded exit-code classification sharpened (137/124 never PASS/TEST_FAILURE
  without evidence; fixture/collection errors -> TEST_INFRA_FAILURE).
- F8: non-PASS summary/artifact_ref are preserved in SelectorResult/StageExecution
  and persisted (bounded, never reusable as PASS).
- F9: snapshot scan excludes ``__pycache__``/``*.pyc``/``.pytest_cache``; store
  trims on load; summary/artifact_ref length limits enforced.

This module is intentionally execution/evidence logic only.  F3 (adversarial
acceptance + Phase-F closure) is out of scope here.
"""

from __future__ import annotations

import glob
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Tuple

from argent_core.test_planning import (
    TestPlan,
    canonical_bytes,
    sha256_hex,
    _ALLOWED_SELECTOR_ROOTS,
)

EXECUTOR_ID = "argent-test-executor-f2-v1"
EVIDENCE_STORE_VERSION = "1"

#: Environment variable that names a file holding the HMAC key (preferred).
_MAC_KEY_FILE_ENV = "ARGENT_EVIDENCE_MAC_KEY_FILE"
#: Environment variable that holds the HMAC key directly (fallback).
_MAC_KEY_ENV = "ARGENT_EVIDENCE_MAC_KEY"

#: External pytest configuration bound as a separate config identity (root
#: level only — NOT inside ``tests/``).  If none exists, config identity is ""
#: (documented as fine).
_EXTERNAL_CONFIG_FILES: Tuple[str, ...] = (
    "pytest.ini",
    "setup.cfg",
    "pyproject.toml",
    "tox.ini",
    "conftest.py",
)

#: Artifact path components and bytecode suffixes excluded from snapshot scans.
_ARTIFACT_DIRS: Tuple[str, ...] = (
    "__pycache__",
    ".pytest_cache",
    ".git",
    ".mypy_cache",
    ".tox",
    ".venv",
    "venv",
    ".eggs",
    "build",
    "dist",
)
_ARTIFACT_SUFFIXES: Tuple[str, ...] = (".pyc", ".pyo")

#: Extra roots covered by the default snapshot identity (e2e-fixture product +
#: tests must invalidate evidence on any change).
_DEFAULT_EXTRA_ROOTS: Tuple[str, ...] = ("e2e-fixture",)

#: Canonical stage order (the plan must be a subsequence of this).
_CANONICAL_STAGE_ORDER: Tuple[str, ...] = (
    "targeted",
    "module",
    "phase_regression",
    "full_suite",
)

#: Bounded length limits for evidence fields (F9).
_MAX_SUMMARY = 1000
_MAX_ARTIFACT_REF = 256

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Bounded enums
# ---------------------------------------------------------------------------


class ResultClass(str, Enum):
    """Outcome of one selector execution (never conflated)."""

    TEST_PASS = "TEST_PASS"
    TEST_FAILURE = "TEST_FAILURE"
    TEST_INFRA_FAILURE = "TEST_INFRA_FAILURE"
    RESOURCE_FAILURE = "RESOURCE_FAILURE"
    PROCESS_FAILURE = "PROCESS_FAILURE"
    TIMEOUT = "TIMEOUT"
    CANCELLED_BLOCKED = "CANCELLED_BLOCKED"
    UNKNOWN = "UNKNOWN"


class StageState(str, Enum):
    """Bounded per-stage state machine."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"  # not executed this attempt (e.g. earlier stage failed)


class Verdict(str, Enum):
    """Terminal verdict of a whole staged execution attempt."""

    DONE = "DONE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


# ---------------------------------------------------------------------------
# Snapshot identity (code + test definitions under test)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotIdentity:
    """Immutable identity of what is under test.

    ``source_hash`` binds the product code content; ``test_definition_hash``
    binds the test definitions themselves.  ``root`` is the canonicalized
    project root the snapshot was computed from ("" = unbound); ``config_hash``
    binds external pytest configuration ("" = none present).  Both are content
    hashes computed by the controller (never from agent prose).
    """

    source_hash: str
    test_definition_hash: str
    executor_id: str = EXECUTOR_ID
    root: str = ""
    config_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("source_hash", "test_definition_hash", "executor_id"):
            v = getattr(self, name)
            if not isinstance(v, str) or not v:
                raise ValueError(f"SnapshotIdentity.{name} must be a non-empty string")
        for name in ("root", "config_hash"):
            v = getattr(self, name)
            if not isinstance(v, str):
                raise ValueError(f"SnapshotIdentity.{name} must be a string")

    def identity_key(self) -> str:
        return sha256_hex(
            canonical_bytes(
                {
                    "source_hash": self.source_hash,
                    "test_definition_hash": self.test_definition_hash,
                    "executor_id": self.executor_id,
                    "root": self.root,
                    "config_hash": self.config_hash,
                }
            )
        )


def _file_sha256(path: Path) -> str:
    return sha256_hex(path.read_bytes())


def _is_artifact_path(p: Path) -> bool:
    """Exclude bytecode + cache/artifact directories from snapshot scans (F9)."""
    if p.suffix in _ARTIFACT_SUFFIXES:
        return True
    if any(part in _ARTIFACT_DIRS for part in p.parts):
        return True
    return False


def _canonical(p: Path) -> Path:
    try:
        return p.resolve()
    except OSError:
        return p.absolute()


def _scan_root(root: Path, project_root: Path) -> List[Tuple[str, str]]:
    """Deterministically hash every file under ``root`` (paths relative to
    ``project_root``), excluding artifacts.

    Directory symlinks are followed once per canonical target (cycle-safe);
    file symlinks are hashed by their resolved content so a change *behind* a
    symlink changes the identity (F2).
    """
    pairs: List[Tuple[str, str]] = []
    seen_dirs: set = set()

    def walk(directory: Path) -> None:
        canonical = _canonical(directory)
        if canonical in seen_dirs:
            return
        seen_dirs.add(canonical)
        try:
            entries = sorted(directory.iterdir(), key=lambda p: str(p))
        except OSError:
            return
        for entry in entries:
            if entry.is_symlink():
                target = _canonical(entry)
                if target.is_dir():
                    walk(target)
                elif target.is_file():
                    if not _is_artifact_path(entry):
                        rel = str(entry.relative_to(project_root))
                        pairs.append((rel, _file_sha256(target)))
                continue
            if entry.is_dir():
                if entry.name in _ARTIFACT_DIRS:
                    continue
                walk(entry)
            elif entry.is_file():
                if _is_artifact_path(entry):
                    continue
                rel = str(entry.relative_to(project_root))
                pairs.append((rel, _file_sha256(entry)))

    walk(root)
    pairs.sort(key=lambda t: t[0])
    return pairs


def _is_under_tests_dir(rel: str, extra_root: str) -> bool:
    prefix = extra_root.rstrip("/") + "/tests"
    return rel == prefix or rel.startswith(prefix + "/")


def compute_snapshot_identity(
    project_root: Optional[str] = None,
    *,
    executor_id: str = EXECUTOR_ID,
    extra_roots: Optional[Iterable[str]] = None,
) -> SnapshotIdentity:
    """Deterministic content identity over the source + test trees.

    Walks ``argent_core/`` and ``tests/`` plus the extra roots (default
    ``e2e-fixture``), hashing the sorted list of ``(relative path, sha256)``
    pairs.  ``<extra>/tests/`` files are test definitions; everything else in an
    extra root is product source.  Bytecode/artifacts are excluded (F9) and
    symlink targets are resolved safely (F2).  Any content change therefore
    changes the relevant hash, which conservatively invalidates previous
    evidence.  External pytest configuration (root-level) is bound as a
    separate config identity.
    """
    root = Path(project_root).resolve() if project_root else _PROJECT_ROOT.resolve()
    extras = list(extra_roots) if extra_roots is not None else list(_DEFAULT_EXTRA_ROOTS)

    source_pairs: List[Tuple[str, str]] = []
    test_pairs: List[Tuple[str, str]] = []

    src_root = root / "argent_core"
    if src_root.is_dir():
        source_pairs.extend(_scan_root(src_root, root))

    for r in extras:
        eroot = root / r
        if not eroot.is_dir():
            continue
        for rel, h in _scan_root(eroot, root):
            if _is_under_tests_dir(rel, r):
                test_pairs.append((rel, h))
            else:
                source_pairs.append((rel, h))

    tst_root = root / "tests"
    if tst_root.is_dir():
        test_pairs.extend(_scan_root(tst_root, root))

    config_pairs: List[Tuple[str, str]] = []
    for fname in _EXTERNAL_CONFIG_FILES:
        c = root / fname
        if c.is_file() and not _is_artifact_path(c):
            config_pairs.append((fname, _file_sha256(c)))

    source_pairs.sort(key=lambda t: t[0])
    test_pairs.sort(key=lambda t: t[0])
    config_pairs.sort(key=lambda t: t[0])

    source_hash = sha256_hex(canonical_bytes({"files": source_pairs}))
    test_definition_hash = sha256_hex(canonical_bytes({"files": test_pairs}))
    config_hash = (
        sha256_hex(canonical_bytes({"files": config_pairs})) if config_pairs else ""
    )
    return SnapshotIdentity(
        source_hash=source_hash,
        test_definition_hash=test_definition_hash,
        executor_id=executor_id,
        root=str(root),
        config_hash=config_hash,
    )


# ---------------------------------------------------------------------------
# Runner abstraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunnerOutcome:
    """Bounded result of running one selector."""

    classification: ResultClass
    summary: str = ""
    artifact_ref: str = ""  # bounded reference/hash, never raw logs
    test_count: int = 0
    duration_seconds: float = 0.0


class SelectorRunner(Protocol):
    def run(self, selector: str) -> RunnerOutcome: ...


def _assert_trusted_selector(selector: str) -> str:
    """Defense-in-depth: only trusted selectors may reach the runner.

    Agent prose must never become executable command input.  Selectors come
    from the F1 TestPlan (already validated against the inventory), but the
    runner re-checks they are under the allowed roots and contain no unsafe
    path tokens.
    """
    if not isinstance(selector, str) or not selector:
        raise ValueError("selector must be a non-empty string")
    s = selector.strip()
    if s.startswith("/") or ".." in s:
        raise ValueError(f"unsafe selector {selector!r}")
    if not any(s.startswith(root) or s == root.rstrip("/") for root in _ALLOWED_SELECTOR_ROOTS):
        raise ValueError(f"selector {selector!r} is not a trusted test selector")
    return s


def _has_glob_magic(s: str) -> bool:
    return any(ch in s for ch in "*?[")


def _resolve_selector(selector: str, project_root: Path) -> Optional[List[str]]:
    """Resolve a trusted selector to explicit pytest argv fragments (F1).

    Globs are expanded deterministically (sorted, against the real filesystem).
    Directory selectors must name an existing, non-empty directory; explicit
    file selectors must exist.  Any zero-match returns ``None`` so the caller
    can fail closed (TEST_INFRA_FAILURE) — never silently empty.
    """
    s = _assert_trusted_selector(selector)
    if _has_glob_magic(s):
        matches = sorted(
            m
            for m in glob.glob(s, root_dir=str(project_root), recursive=True)
            if (project_root / m).exists()
        )
        if not matches:
            return None
        return matches
    if s.endswith("/"):
        d = project_root / s.rstrip("/")
        if not d.is_dir() or not any(d.iterdir()):
            return None
        return [s]
    p = project_root / s
    if not p.exists():
        return None
    return [s]


class PytestRunner:
    """Real local executor: ``python -m pytest <resolved> -q --tb=line``.

    Never invokes a shell; the command is a fixed argv list and the only
    variable is a trusted selector (resolved to explicit file paths).  The
    runner binds a canonical ``project_root`` and executes with ``cwd=root``
    (F5), so execution cannot silently happen in a foreign directory.
    ``runner_fn`` is injectable for offline deterministic tests (defaults to
    :func:`subprocess.run` with ``shell=False``).
    """

    def __init__(
        self,
        runner_fn: Optional[Callable[..., Any]] = None,
        timeout_seconds: int = 300,
        python: Optional[str] = None,
        project_root: Optional[str] = None,
    ) -> None:
        self._runner_fn = runner_fn or subprocess.run
        self._timeout_seconds = timeout_seconds
        self._python = python or sys.executable
        self._project_root = (
            Path(project_root).resolve() if project_root else _PROJECT_ROOT.resolve()
        )

    @property
    def project_root(self) -> str:
        return str(self._project_root)

    def _argv(self, resolved: List[str]) -> List[str]:
        return [self._python, "-m", "pytest", *resolved, "-q", "--tb=line"]

    def run(self, selector: str) -> RunnerOutcome:
        selector = _assert_trusted_selector(selector)
        resolved = _resolve_selector(selector, self._project_root)
        if resolved is None:
            return RunnerOutcome(
                ResultClass.TEST_INFRA_FAILURE,
                summary=f"selector {selector!r} matched no test files (fail-closed)",
            )
        argv = self._argv(resolved)
        start = time.monotonic()
        try:
            proc = self._runner_fn(
                argv,
                timeout=self._timeout_seconds,
                capture_output=True,
                shell=False,
                cwd=str(self._project_root),
            )
        except subprocess.TimeoutExpired:
            return RunnerOutcome(
                ResultClass.TIMEOUT,
                summary=f"selector {selector!r} exceeded {self._timeout_seconds}s",
                duration_seconds=time.monotonic() - start,
            )
        except OSError as exc:
            return RunnerOutcome(
                ResultClass.PROCESS_FAILURE,
                summary=f"failed to start pytest for {selector!r}: {exc}",
                duration_seconds=time.monotonic() - start,
            )

        duration = time.monotonic() - start
        classification = self.classify(proc.returncode, proc.stdout or b"", proc.stderr or b"")
        summary = self._bounded_summary(proc.stdout or b"", proc.stderr or b"")
        return RunnerOutcome(
            classification=classification,
            summary=summary,
            artifact_ref=sha256_hex((proc.stdout or b"") + (proc.stderr or b"")),
            test_count=self._parse_test_count(proc.stdout or b""),
            duration_seconds=duration,
        )

    @staticmethod
    def _looks_like_setup_or_collection_error(text: str) -> bool:
        """Bounded markers for pytest setup/fixture/collection errors (F7)."""
        if "error collecting" in text or "errors during collection" in text:
            return True
        if "error at setup of" in text or "error at teardown of" in text:
            return True
        if re.search(r"\b\d+\s+errors?\b", text):
            return True
        if re.search(r"\b\d+\s+error\b", text):
            return True
        return False

    @staticmethod
    def _has_timeout_marker(text: str) -> bool:
        return "timed out" in text

    @staticmethod
    def classify(
        returncode: int,
        stdout: bytes,
        stderr: bytes,
        *,
        scope_evidence: bool = False,
    ) -> ResultClass:
        """Map a pytest exit code (plus output) to a bounded result class (F7).

        - 0 -> PASS; 1 -> TEST_FAILURE unless the output clearly shows a
          fixture/setup/collection error (then TEST_INFRA_FAILURE); 2 ->
          CANCELLED_BLOCKED; 3/4/5 -> TEST_INFRA_FAILURE.
        - 124 (timeout wrapper) -> TIMEOUT only with a trusted timeout marker,
          else UNKNOWN (never PASS, never TEST_FAILURE).
        - 137 (SIGKILL/OOM) -> RESOURCE_FAILURE only when bound scope/memory
          evidence is present; otherwise conservative UNKNOWN (never
          TEST_FAILURE).
        """
        out = (stdout or b"") + (stderr or b"")
        text = out.decode("utf-8", "replace").lower()
        if returncode == 0:
            return ResultClass.TEST_PASS
        if returncode == 1:
            if PytestRunner._looks_like_setup_or_collection_error(text):
                return ResultClass.TEST_INFRA_FAILURE
            return ResultClass.TEST_FAILURE
        if returncode == 2:
            return ResultClass.CANCELLED_BLOCKED
        if returncode in (3, 4, 5):
            return ResultClass.TEST_INFRA_FAILURE
        if returncode == 124:
            if PytestRunner._has_timeout_marker(text):
                return ResultClass.TIMEOUT
            return ResultClass.UNKNOWN
        if returncode == 137:
            if scope_evidence:
                return ResultClass.RESOURCE_FAILURE
            return ResultClass.UNKNOWN
        return ResultClass.UNKNOWN

    @staticmethod
    def _bounded_summary(stdout: bytes, stderr: bytes) -> str:
        blob = (stdout + stderr).decode("utf-8", "replace")
        lines = [ln.rstrip() for ln in blob.splitlines() if ln.strip()]
        # Keep the final summary line(s); never persist giant stdout.
        tail = lines[-3:] if len(lines) > 3 else lines
        return " | ".join(tail)[:500]

    @staticmethod
    def _parse_test_count(stdout: bytes) -> int:
        blob = stdout.decode("utf-8", "replace")
        # Last line of a pytest -q run looks like "2240 passed in 32s".
        m = re.search(r"(\d+)\s+passed", blob)
        if not m:
            return 0
        try:
            return int(m.group(1))
        except ValueError:
            return 0


# ---------------------------------------------------------------------------
# Resource gate (Phase C binding)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceAdmission:
    allowed: bool
    reason: str = ""


class ResourceGate(Protocol):
    def admit(self) -> ResourceAdmission: ...


class ResourceGovernorGate:
    """Documented, injectable bridge from the real Phase-C
    ``ResourceGovernor``/``ExecutionEnforcer`` to the F2 ``ResourceGate``
    protocol (F6).

    ``decide_fn`` is the real admission function (e.g. ``governor.decide`` or an
    ``ExecutionEnforcer`` admission path).  Only an ``ALLOW`` verdict maps to
    ``allowed=True``; DEFER / DENY_LOCAL / PREFER_EXTERNAL / unknown verdicts map
    to ``allowed=False`` (fail-closed) with the reason code preserved.
    """

    def __init__(self, decide_fn: Callable[..., Any]) -> None:
        if not callable(decide_fn):
            raise ValueError("ResourceGovernorGate requires a callable decide_fn")
        self._decide_fn = decide_fn

    def admit(self) -> ResourceAdmission:
        decision = self._decide_fn()
        verdict = getattr(decision, "decision", None)
        if verdict == "ALLOW":
            return ResourceAdmission(True)
        reason = getattr(decision, "reason_code", "") or str(decision)
        return ResourceAdmission(False, str(reason))


# ---------------------------------------------------------------------------
# Evidence records + bounded store
# ---------------------------------------------------------------------------


def compute_evidence_mac(record: "EvidenceRecord", key: bytes) -> str:
    """HMAC-SHA256 over the canonical identity/result fields (F4).

    The key must NOT live in the agent write area (env var / file outside the
    worktree, injectable for tests).  This is the authenticated provenance
    binding that a plain unkeyed SHA-256 cannot provide.
    """
    payload = canonical_bytes(
        {
            "identity": record.identity_fields(),
            "classification": record.classification.value,
            "timestamp": record.timestamp,
            "artifact_ref": record.artifact_ref,
            "summary": record.summary,
            "test_count": record.test_count,
        }
    )
    return hmac.new(bytes(key), payload, hashlib.sha256).hexdigest()


def _resolve_mac_key(mac_key: Optional[bytes]) -> bytes:
    """Resolve the evidence MAC key, fail-closed (F4).

    Precedence: explicit ``mac_key`` argument -> ``ARGENT_EVIDENCE_MAC_KEY_FILE``
    -> ``ARGENT_EVIDENCE_MAC_KEY``.  If none is available, raise ValueError —
    the store must never silently downgrade to an unkeyed hash.
    """
    if mac_key is not None:
        if isinstance(mac_key, str):
            return mac_key.encode("utf-8")
        return bytes(mac_key)
    filepath = os.environ.get(_MAC_KEY_FILE_ENV)
    if filepath:
        raw = Path(filepath).read_bytes().strip()
        if not raw:
            raise ValueError("evidence MAC key file is empty")
        return raw
    raw = os.environ.get(_MAC_KEY_ENV)
    if raw:
        if not raw.strip():
            raise ValueError("evidence MAC key is empty")
        return raw.encode("utf-8")
    raise ValueError(
        "no evidence MAC key configured (set "
        f"{_MAC_KEY_ENV} or {_MAC_KEY_FILE_ENV}); fail-closed"
    )


@dataclass(frozen=True)
class EvidenceRecord:
    """Bounded reusable test-result evidence.

    Identity fields bind the exact snapshot/selector/plan/policy/inventory/
    executor/root/config.  ``classification`` is the authoritative terminal
    result and ``evidence_hash`` is the keyed MAC.  Only a ``TEST_PASS`` record
    with a valid MAC is ever reusable.
    """

    selector: str
    source_hash: str
    test_definition_hash: str
    plan_hash: str
    inventory_hash: str
    policy_hash: str
    executor_id: str
    classification: ResultClass
    timestamp: str
    artifact_ref: str = ""
    summary: str = ""
    test_count: int = 0
    evidence_hash: str = ""
    root: str = ""
    config_hash: str = ""

    def __post_init__(self) -> None:
        if len(self.summary) > _MAX_SUMMARY:
            object.__setattr__(self, "summary", self.summary[:_MAX_SUMMARY])
        if len(self.artifact_ref) > _MAX_ARTIFACT_REF:
            object.__setattr__(self, "artifact_ref", self.artifact_ref[:_MAX_ARTIFACT_REF])

    def identity_fields(self) -> Dict[str, str]:
        return {
            "selector": self.selector,
            "source_hash": self.source_hash,
            "test_definition_hash": self.test_definition_hash,
            "plan_hash": self.plan_hash,
            "inventory_hash": self.inventory_hash,
            "policy_hash": self.policy_hash,
            "executor_id": self.executor_id,
            "root": self.root,
            "config_hash": self.config_hash,
        }


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class EvidenceStore:
    """Bounded, versioned, fail-closed, authenticated evidence store.

    Persists terminal evidence (PASS *and* non-PASS, bounded) with a keyed MAC.
    Reuse looks up only exact-identity ``TEST_PASS`` records whose MAC still
    verifies under the store key.  The store trims to ``max_records`` on both
    add and load (F9).
    """

    def __init__(
        self,
        path: Optional[str] = None,
        max_records: int = 1000,
        now_fn: Callable[[], str] = _now_iso,
        mac_key: Optional[bytes] = None,
    ) -> None:
        if max_records < 1:
            raise ValueError("max_records must be >= 1")
        self._path = Path(path) if path else None
        self._max_records = max_records
        self._now_fn = now_fn
        self._mac_key = _resolve_mac_key(mac_key)
        self._records: Dict[str, EvidenceRecord] = {}
        if self._path and self._path.exists():
            self._load()

    # -- MAC verification ------------------------------------------------
    def _verify_mac(self, record: EvidenceRecord) -> bool:
        return record.evidence_hash == compute_evidence_mac(record, self._mac_key)

    # -- storage ---------------------------------------------------------
    def add(self, record: EvidenceRecord) -> None:
        if record.evidence_hash:
            if not self._verify_mac(record):
                raise ValueError("refusing to persist an unauthenticated evidence record")
            rec = record
        else:
            rec = replace(record, evidence_hash=compute_evidence_mac(record, self._mac_key))
        self._records[rec.evidence_hash] = rec
        self._trim()
        if self._path:
            self._save()

    def _trim(self) -> None:
        while len(self._records) > self._max_records:
            oldest = min(self._records.values(), key=lambda r: r.timestamp)
            del self._records[oldest.evidence_hash]

    # -- reuse -----------------------------------------------------------
    def find_reusable_pass(
        self,
        selector: str,
        snapshot: SnapshotIdentity,
        plan: TestPlan,
    ) -> Optional[EvidenceRecord]:
        """Return a reusable PASS record only on exact identity + valid MAC.

        A record is reusable iff: classification == TEST_PASS, the identity
        fields all match, and the MAC verifies.  Anything else (FAIL, UNKNOWN,
        tampered, unknown provenance) returns ``None`` so the executor reruns.
        """
        want = {
            "selector": selector,
            "source_hash": snapshot.source_hash,
            "test_definition_hash": snapshot.test_definition_hash,
            "plan_hash": plan.plan_hash,
            "inventory_hash": plan.inventory_hash,
            "policy_hash": plan.policy_hash,
            "executor_id": snapshot.executor_id,
            "root": snapshot.root,
            "config_hash": snapshot.config_hash,
        }
        for rec in self._records.values():
            if rec.classification != ResultClass.TEST_PASS:
                continue
            if rec.identity_fields() != want:
                continue
            if not self._verify_mac(rec):
                continue
            return rec
        return None

    def records(self) -> Tuple[EvidenceRecord, ...]:
        return tuple(self._records.values())

    # -- persistence -----------------------------------------------------
    def _save(self) -> None:
        assert self._path is not None
        payload = {
            "evidence_store_version": EVIDENCE_STORE_VERSION,
            "records": [
                {
                    "selector": r.selector,
                    "source_hash": r.source_hash,
                    "test_definition_hash": r.test_definition_hash,
                    "plan_hash": r.plan_hash,
                    "inventory_hash": r.inventory_hash,
                    "policy_hash": r.policy_hash,
                    "executor_id": r.executor_id,
                    "classification": r.classification.value,
                    "timestamp": r.timestamp,
                    "artifact_ref": r.artifact_ref,
                    "summary": r.summary,
                    "test_count": r.test_count,
                    "evidence_hash": r.evidence_hash,
                    "root": r.root,
                    "config_hash": r.config_hash,
                }
                for r in self._records.values()
            ],
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True, indent=1))
        tmp.replace(self._path)

    def _load(self) -> None:
        assert self._path is not None
        try:
            raw = self._path.read_text()
            data = json.loads(
                raw,
                object_pairs_hook=self._no_duplicate_keys,
            )
        except (OSError, ValueError) as exc:
            raise ValueError(f"evidence store unreadable/corrupt: {exc}") from exc
        if data.get("evidence_store_version") != EVIDENCE_STORE_VERSION:
            raise ValueError("evidence store version mismatch (fail-closed)")
        recs = data.get("records")
        if not isinstance(recs, list):
            raise ValueError("evidence store 'records' must be a list")
        for item in recs:
            try:
                rec = EvidenceRecord(
                    selector=item["selector"],
                    source_hash=item["source_hash"],
                    test_definition_hash=item["test_definition_hash"],
                    plan_hash=item["plan_hash"],
                    inventory_hash=item["inventory_hash"],
                    policy_hash=item["policy_hash"],
                    executor_id=item["executor_id"],
                    classification=ResultClass(item["classification"]),
                    timestamp=item["timestamp"],
                    artifact_ref=item.get("artifact_ref", ""),
                    summary=item.get("summary", ""),
                    test_count=int(item.get("test_count", 0)),
                    evidence_hash=item["evidence_hash"],
                    root=item.get("root", ""),
                    config_hash=item.get("config_hash", ""),
                )
            except (KeyError, ValueError) as exc:
                raise ValueError(f"malformed evidence record: {exc}") from exc
            if not self._verify_mac(rec):
                raise ValueError("malformed evidence record (MAC mismatch)")
            self._records[rec.evidence_hash] = rec
        self._trim()

    @staticmethod
    def _no_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        d: Dict[str, Any] = {}
        for k, v in pairs:
            if k in d:
                raise ValueError(f"duplicate key {k!r} in evidence store")
            d[k] = v
        return d


# ---------------------------------------------------------------------------
# Execution model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SelectorResult:
    selector: str
    classification: ResultClass
    reused: bool = False
    evidence_hash: str = ""
    summary: str = ""
    artifact_ref: str = ""
    test_count: int = 0


@dataclass(frozen=True)
class StageExecution:
    name: str
    state: StageState
    selector_results: Tuple[SelectorResult, ...] = ()
    summary: str = ""


@dataclass(frozen=True)
class ExecutionReport:
    plan_hash: str
    snapshot: SnapshotIdentity
    verdict: Verdict
    stages: Tuple[StageExecution, ...]
    first_failure_stage: Optional[str] = None
    first_failure_selector: Optional[str] = None
    first_failure_class: Optional[ResultClass] = None
    # Economy metrics (observational, bounded)
    stages_planned: int = 0
    stages_executed: int = 0
    stages_reused: int = 0
    stages_avoided: int = 0
    full_suite_avoided: bool = False
    wall_clock_seconds: float = 0.0
    total_tests: int = 0

    def all_pass(self) -> bool:
        return self.verdict == Verdict.DONE


# ---------------------------------------------------------------------------
# Plan integrity (F3)
# ---------------------------------------------------------------------------


def _plan_hash_payload(plan: TestPlan) -> Dict[str, Any]:
    return {
        "risk": plan.risk_level.value,
        "full_suite_required": plan.full_suite_required,
        "stages": [
            {
                "name": st.name,
                "selectors": list(st.selectors),
                "reasons": {s: list(r) for s, r in st.reasons.items()},
                "mandatory": list(st.mandatory),
            }
            for st in plan.stages
        ],
        "policy_hash": plan.policy_hash,
        "inventory_hash": plan.inventory_hash,
        "change_set_hash": plan.change_set_hash,
    }


def recompute_plan_hash(plan: TestPlan) -> str:
    """Re-derive ``plan_hash`` from the authentic TestPlan content (F3)."""
    return sha256_hex(canonical_bytes(_plan_hash_payload(plan)))


def _validate_plan(plan: TestPlan) -> None:
    """Fail-closed plan integrity (F3).

    Rejects non-TestPlan (TypeError), a ``plan_hash`` that does not match the
    authentic content, duplicated/unknown stage names, out-of-order stages,
    empty stages/selectors, mandatory selectors not present in their stage, and
    a missing ``full_suite`` stage when ``full_suite_required``.
    """
    if not isinstance(plan, TestPlan):
        raise TypeError("execute_plan requires an F1 TestPlan")
    if plan.plan_hash != recompute_plan_hash(plan):
        raise ValueError("TestPlan.plan_hash does not match its content (tampered/malformed)")
    names = [st.name for st in plan.stages]
    if len(names) != len(set(names)):
        raise ValueError("TestPlan stage names must be unique")
    idx = -1
    for name in names:
        if name not in _CANONICAL_STAGE_ORDER:
            raise ValueError(f"unknown stage name {name!r}")
        pos = _CANONICAL_STAGE_ORDER.index(name)
        if pos <= idx:
            raise ValueError(
                "TestPlan stage order must be targeted->module->phase_regression->full_suite"
            )
        idx = pos
    for st in plan.stages:
        if not st.selectors:
            raise ValueError(f"stage {st.name!r} has no selectors")
        for sel in st.selectors:
            if not isinstance(sel, str) or not sel:
                raise ValueError(f"stage {st.name!r} has an empty selector")
        for m in st.mandatory:
            if m not in st.selectors:
                raise ValueError(
                    f"stage {st.name!r} has mandatory selector {m!r} outside its selectors"
                )
    if plan.full_suite_required and "full_suite" not in names:
        raise ValueError("full_suite stage missing though plan.full_suite_required")


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _blocked_no_gate(plan: TestPlan, snapshot: SnapshotIdentity) -> ExecutionReport:
    """Fail-closed BLOCKED report when no resource gate is configured (F6)."""
    stages = tuple(
        StageExecution(
            name=st.name,
            state=StageState.BLOCKED,
            selector_results=tuple(
                SelectorResult(
                    sel,
                    ResultClass.RESOURCE_FAILURE,
                    summary="no resource gate configured (fail-closed)",
                )
                for sel in st.selectors
            ),
            summary="no resource gate configured; execution refused (fail-closed)",
        )
        for st in plan.stages
    )
    return ExecutionReport(
        plan_hash=plan.plan_hash,
        snapshot=snapshot,
        verdict=Verdict.BLOCKED,
        stages=stages,
        first_failure_stage=plan.stages[0].name if plan.stages else None,
        first_failure_class=ResultClass.RESOURCE_FAILURE,
        stages_planned=len(plan.stages),
    )


def execute_plan(
    plan: TestPlan,
    runner: SelectorRunner,
    *,
    snapshot: SnapshotIdentity,
    resource_gate: Optional[ResourceGate] = None,
    store: Optional[EvidenceStore] = None,
    project_root: Optional[str] = None,
) -> ExecutionReport:
    """Deterministically run the stages of an F1 TestPlan in order.

    - Plan integrity is validated fail-closed at entry (F3).
    - A missing resource gate is fail-closed (BLOCKED, RESOURCE_FAILURE); no
      silent AllowAll default (F6).  The gate is consulted before each stage.
    - The snapshot root is validated against the execution root (F5); evidence
      is bound to the canonical root + executor + config identity.
    - Reuse: an exact-identity, MAC-verified PASS in ``store`` short-circuits.
    - A genuine TEST_FAILURE fails the stage and stops later stages (early
      failure stopping).  Other non-PASS outcomes BLOCK and stop.
    - Non-PASS summary/artifact_ref are preserved in the report and persisted
      (bounded, never reusable as PASS) so the writer gets actionable evidence
      (F8).
    - The whole attempt is DONE only when every selector of every stage has
      valid PASS evidence (executed or exactly reused).
    """
    _validate_plan(plan)

    if resource_gate is None:
        return _blocked_no_gate(plan, snapshot)
    gate = resource_gate

    # Root binding (F5): the snapshot must be computed from the same root the
    # runner executes in.  Unbound snapshots (root == "") are allowed only when
    # no execution root is declared (offline unit tests).
    declared_root = project_root or getattr(runner, "project_root", None)
    if declared_root is not None:
        canon = str(Path(declared_root).resolve())
        if snapshot.root and snapshot.root != canon:
            raise ValueError(
                "snapshot root does not match the execution root (fail-closed)"
            )

    start = time.monotonic()
    stages_exec: List[StageExecution] = []
    staged_planned = len(plan.stages)
    stages_executed = 0
    stages_reused = 0
    stages_avoided = 0
    full_suite_avoided = False
    total_tests = 0

    first_failure_stage: Optional[str] = None
    first_failure_selector: Optional[str] = None
    first_failure_class: Optional[ResultClass] = None
    verdict = Verdict.DONE
    stop = False

    for stage in plan.stages:
        if stop:
            stages_exec.append(
                StageExecution(
                    name=stage.name,
                    state=StageState.SKIPPED,
                    summary="skipped: earlier stage produced a non-PASS result",
                )
            )
            stages_avoided += 1
            if stage.name == "full_suite":
                full_suite_avoided = True
            continue

        admission = gate.admit()
        if not admission.allowed:
            stages_exec.append(
                StageExecution(
                    name=stage.name,
                    state=StageState.BLOCKED,
                    selector_results=tuple(
                        SelectorResult(
                            sel,
                            ResultClass.RESOURCE_FAILURE,
                            summary=f"resource admission denied: {admission.reason}",
                        )
                        for sel in stage.selectors
                    ),
                    summary=f"resource admission denied: {admission.reason}",
                )
            )
            verdict = Verdict.BLOCKED
            first_failure_stage = first_failure_stage or stage.name
            first_failure_class = first_failure_class or ResultClass.RESOURCE_FAILURE
            stop = True
            continue

        selector_results: List[SelectorResult] = []
        stage_reused = True
        stage_failed = False
        stage_blocked = False

        for sel in stage.selectors:
            if store is not None:
                reused_rec = store.find_reusable_pass(sel, snapshot, plan)
                if reused_rec is not None:
                    selector_results.append(
                        SelectorResult(
                            selector=sel,
                            classification=ResultClass.TEST_PASS,
                            reused=True,
                            evidence_hash=reused_rec.evidence_hash,
                            summary=reused_rec.summary,
                            artifact_ref=reused_rec.artifact_ref,
                            test_count=reused_rec.test_count,
                        )
                    )
                    total_tests += reused_rec.test_count
                    continue

            outcome = runner.run(sel)
            total_tests += outcome.test_count
            if outcome.classification == ResultClass.TEST_PASS:
                selector_results.append(
                    SelectorResult(
                        sel,
                        ResultClass.TEST_PASS,
                        summary=outcome.summary,
                        artifact_ref=outcome.artifact_ref,
                        test_count=outcome.test_count,
                    )
                )
                if store is not None:
                    store.add(
                        EvidenceRecord(
                            selector=sel,
                            source_hash=snapshot.source_hash,
                            test_definition_hash=snapshot.test_definition_hash,
                            plan_hash=plan.plan_hash,
                            inventory_hash=plan.inventory_hash,
                            policy_hash=plan.policy_hash,
                            executor_id=snapshot.executor_id,
                            classification=ResultClass.TEST_PASS,
                            timestamp=store._now_fn(),
                            artifact_ref=outcome.artifact_ref,
                            summary=outcome.summary,
                            test_count=outcome.test_count,
                            root=snapshot.root,
                            config_hash=snapshot.config_hash,
                        )
                    )
                stage_reused = False
            else:
                selector_results.append(
                    SelectorResult(
                        sel,
                        outcome.classification,
                        summary=outcome.summary,
                        artifact_ref=outcome.artifact_ref,
                        test_count=outcome.test_count,
                    )
                )
                # Persist actionable non-PASS terminal evidence (bounded, never
                # reusable as PASS) so the writer can act on it (F8).
                if store is not None:
                    store.add(
                        EvidenceRecord(
                            selector=sel,
                            source_hash=snapshot.source_hash,
                            test_definition_hash=snapshot.test_definition_hash,
                            plan_hash=plan.plan_hash,
                            inventory_hash=plan.inventory_hash,
                            policy_hash=plan.policy_hash,
                            executor_id=snapshot.executor_id,
                            classification=outcome.classification,
                            timestamp=store._now_fn(),
                            artifact_ref=outcome.artifact_ref,
                            summary=outcome.summary,
                            test_count=outcome.test_count,
                            root=snapshot.root,
                            config_hash=snapshot.config_hash,
                        )
                    )
                stage_reused = False
                if first_failure_class is None:
                    first_failure_class = outcome.classification
                    first_failure_selector = sel
                    first_failure_stage = stage.name
                if outcome.classification == ResultClass.TEST_FAILURE:
                    stage_failed = True
                    verdict = Verdict.FAILED
                else:
                    stage_blocked = True
                    verdict = Verdict.BLOCKED
                break

        if stage_failed:
            stages_exec.append(
                StageExecution(
                    name=stage.name,
                    state=StageState.FAILED,
                    selector_results=tuple(selector_results),
                    summary="genuine test failure; later stages not executed",
                )
            )
            stop = True
        elif stage_blocked:
            stages_exec.append(
                StageExecution(
                    name=stage.name,
                    state=StageState.BLOCKED,
                    selector_results=tuple(selector_results),
                    summary="non-PASS terminal result; later stages not executed",
                )
            )
            stop = True
        else:
            stages_exec.append(
                StageExecution(
                    name=stage.name,
                    state=StageState.PASSED,
                    selector_results=tuple(selector_results),
                    summary="",
                )
            )
            stages_executed += 1
            if stage_reused:
                stages_reused += 1

    return ExecutionReport(
        plan_hash=plan.plan_hash,
        snapshot=snapshot,
        verdict=verdict,
        stages=tuple(stages_exec),
        first_failure_stage=first_failure_stage,
        first_failure_selector=first_failure_selector,
        first_failure_class=first_failure_class,
        stages_planned=staged_planned,
        stages_executed=stages_executed,
        stages_reused=stages_reused,
        stages_avoided=stages_avoided,
        full_suite_avoided=full_suite_avoided,
        wall_clock_seconds=time.monotonic() - start,
        total_tests=total_tests,
    )


# ---------------------------------------------------------------------------
# Restart / crash safety
# ---------------------------------------------------------------------------


def reconcile_running(record: EvidenceRecord, *, mac_key: Optional[bytes] = None) -> EvidenceRecord:
    """Conservatively reconcile a persisted record on restart.

    A record that somehow carries a RUNNING/UNKNOWN classification must NEVER
    become PASS.  A PASS is only valid if its keyed MAC verifies; without a key
    (or on MAC mismatch) it downgrades to UNKNOWN (which the store never
    reuses).  It exists so any future persistence of RUNNING state has an
    explicit conservative reconciliation path.
    """
    if record.classification != ResultClass.TEST_PASS:
        return record
    if mac_key is None:
        return replace(record, classification=ResultClass.UNKNOWN, evidence_hash="")
    if record.evidence_hash == compute_evidence_mac(record, mac_key):
        return record
    return replace(record, classification=ResultClass.UNKNOWN, evidence_hash="")
