"""Phase F1 — deterministic test inventory, change impact, risk + test plan.

This module is the pure, deterministic authority for the Test Economy
(ARGENT ARCHITECTURE V1 FINAL §13).  It performs **no LLM calls, no shell, no
network, no I/O except loading the two versioned metadata files**:

* ``registry/test_inventory_v1.json`` — which tests exist, which module owns
  which subsystem, and which tests target which module/subsystem.
* ``registry/test_policy_v1.json`` — how risk is derived from trusted change
  evidence, which hard safety regressions can never be dropped, and when the
  full suite stays mandatory.

Fundamental invariants (Owner-Spec F1 + Sol fix-round F1-F9, verbindlich):

* **Test Economy is *staged sufficiency*, not "avoid the full suite".**  The
  full suite is ~36 s; it is only *reduced* for provably low-risk, isolated
  local iterations, and it stays mandatory at closing and for every
  HIGH/CRITICAL/uncertain/security/test-infra/schema/multi-subsystem change.
* **Risk is derived from trusted evidence, never from agent prose.**  A
  :class:`ChangeEvidence` carries only bounded facts (changed paths, base ref,
  a schema-migration flag, a phase-closing flag, a security-review flag).
  There is no field by which an agent can claim "this is small" to lower the
  plan.
* **UNKNOWN means broader, never narrower.**  A path that cannot be mapped to
  a known module raises plan breadth and never reduces it.
* **Test-infrastructure changes are self-referential risk.**  A change to the
  planner/inventory/policy/fixtures/conftest can never be proven safe by a test
  set it just weakened, so it forces broad regression + full suite.
* **Hard safety regressions cannot be cost-ranked away.**  The hard invariants
  in the policy are always included regardless of any efficiency ranking, and
  every mandatory selector keeps an explicit mandatory/hard-invariant marking
  even if it was first placed in an earlier stage.
* **Safety floors are in the CODE, not in the mutable policy.**  The policy
  cannot delete mandatory risk tags, drop the core hard-invariant subsystems,
  disable UNKNOWN/test-infra full-suite, or shrink the mandatory full-suite
  condition set (fix F1).
* **Fail-closed metadata.**  A malformed inventory/policy (duplicate keys,
  unknown subsystem/tag/risk, missing fields, unsafe/zero-match selectors,
  ambiguous basenames) is refused in full (fix F2/F9).
* **No agent mutation.**  Metadata is deeply read-only; no API raises a
  status, drops a mandatory selector, or mints a lower risk (fix F6).
"""

from __future__ import annotations

import glob
import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

INVENTORY_VERSION = "1"
POLICY_VERSION = "1"

#: Repository root (argent_core's parent) used for glob resolution against the
#: real filesystem (zero-match rejection of selectors).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Bounded enums
# ---------------------------------------------------------------------------


class RiskLevel(str, Enum):
    """Bounded change-risk classes (F1 spec C)."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


_RISK_RANK: Dict[str, int] = {
    RiskLevel.LOW.value: 0,
    RiskLevel.MEDIUM.value: 1,
    RiskLevel.HIGH.value: 2,
    RiskLevel.CRITICAL.value: 3,
}


def _max_risk(*levels: RiskLevel) -> RiskLevel:
    best = RiskLevel.LOW
    for lv in levels:
        if _RISK_RANK[lv.value] > _RISK_RANK[best.value]:
            best = lv
    return best


class Subsystem(str, Enum):
    """Bounded subsystem ownership vocabulary (coarse module ownership)."""

    CORE = "CORE"
    SUPERVISOR = "SUPERVISOR"
    RESOURCE = "RESOURCE"
    CONTEXT = "CONTEXT"
    MODEL_ROUTING = "MODEL_ROUTING"
    SECURITY = "SECURITY"
    PERSISTENCE = "PERSISTENCE"
    TEST_INFRA = "TEST_INFRA"
    DOCUMENTATION = "DOCUMENTATION"


class RiskTag(str, Enum):
    """Bounded fine-grained risk categories (F1 spec C/E)."""

    SCHEMA_MIGRATION = "SCHEMA_MIGRATION"
    LEASE_FENCING_SCHEDULER = "LEASE_FENCING_SCHEDULER"
    SECURITY_TRUST_BOUNDARY = "SECURITY_TRUST_BOUNDARY"
    WRITE_BROKER_EXECUTION_BOUNDARY = "WRITE_BROKER_EXECUTION_BOUNDARY"
    CRASH_RECOVERY = "CRASH_RECOVERY"
    PROCESS_OWNERSHIP = "PROCESS_OWNERSHIP"
    RESOURCE_ENFORCEMENT = "RESOURCE_ENFORCEMENT"
    CONTEXT_INTEGRITY = "CONTEXT_INTEGRITY"
    MODEL_ROUTING_INDEPENDENCE = "MODEL_ROUTING_INDEPENDENCE"
    TERMINAL_STATE_TRANSITION = "TERMINAL_STATE_TRANSITION"
    TEST_INFRASTRUCTURE = "TEST_INFRASTRUCTURE"


# ---------------------------------------------------------------------------
# Non-negotiable safety floors (F1) — defined in CODE, not in the policy file
# ---------------------------------------------------------------------------

#: Mandatory risk tags that MUST exist in the policy with at least the given
#: minimum risk level.  A policy that deletes or weakens any of these is
#: rejected at load time.
_MANDATORY_RISK_TAGS: Dict[str, str] = {
    "SCHEMA_MIGRATION": "CRITICAL",
    "LEASE_FENCING_SCHEDULER": "HIGH",
    "SECURITY_TRUST_BOUNDARY": "HIGH",
    "WRITE_BROKER_EXECUTION_BOUNDARY": "HIGH",
    "CRASH_RECOVERY": "HIGH",
    "PROCESS_OWNERSHIP": "HIGH",
    "RESOURCE_ENFORCEMENT": "HIGH",
    "CONTEXT_INTEGRITY": "MEDIUM",
    "MODEL_ROUTING_INDEPENDENCE": "HIGH",
    "TERMINAL_STATE_TRANSITION": "HIGH",
    "TEST_INFRASTRUCTURE": "HIGH",
}

#: Core hard-invariant subsystems that MUST exist in the policy, each with
#: ``full_suite: true`` (safety-critical: persistence, security, supervisor).
_CORE_HARD_INVARIANT_SUBSYSTEMS: Tuple[str, ...] = (
    "SECURITY",
    "SUPERVISOR",
    "PERSISTENCE",
)

#: Mandatory full-suite conditions that MUST be present in the policy.
_MANDATORY_FULL_SUITE_CONDITIONS: Tuple[str, ...] = (
    "phase_closing",
    "risk_HIGH",
    "risk_CRITICAL",
    "test_infrastructure_change",
    "multiple_subsystems",
)

#: Planner-owned paths that MUST be classified as TEST_INFRA if present (they
#: may not be re-classified into e.g. CORE to dodge the test-infra risk).
_PLANNER_OWNED_PATH_PREFIXES: Tuple[str, ...] = ("argent_core/registry/test_",)
_PLANNER_OWNED_EXACT_PATHS: FrozenSet[str] = frozenset(
    {
        "argent_core/test_planning.py",
        "argent_core/test_inventory.py",
        "argent_core/change_impact.py",
    }
)


#: Bounded, known full-suite condition names referenced by the policy.  The
#: policy's ``full_suite_required_when`` list must be a subset of this set.
_FULL_SUITE_CONDITIONS: FrozenSet[str] = frozenset(
    {
        "phase_closing",
        "risk_CRITICAL",
        "risk_HIGH",
        "test_infrastructure_change",
        "schema_migration",
        "multiple_subsystems",
        "test_plan_uncertainty",
        "planner_metadata_uncertainty",
        "security_reviewed_patch",
    }
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TestPlanningError(Exception):
    """Base error for the deterministic test planner."""

    __test__ = False  # pytest: this is not a test class


class InventoryError(TestPlanningError):
    """Malformed / inconsistent test inventory."""


class PolicyError(TestPlanningError):
    """Malformed / inconsistent test-economy policy."""


# ---------------------------------------------------------------------------
# Canonical hashing helpers (stable, independent of file formatting)
# ---------------------------------------------------------------------------


def canonical_bytes(obj: Any) -> bytes:
    """Deterministic, formatting-independent serialisation of a JSON value."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _no_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    """Fail-closed ``object_pairs_hook``: refuse ambiguous duplicate keys."""
    d: Dict[str, Any] = {}
    for k, v in pairs:
        if k in d:
            raise ValueError(f"duplicate key: {k!r}")
        d[k] = v
    return d


def _load_json(path: Path) -> Dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        obj = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
    except ValueError as exc:
        raise TestPlanningError(f"malformed JSON in {path}: {exc}") from exc
    if not isinstance(obj, dict):
        raise TestPlanningError(f"{path}: expected a JSON object")
    return obj


def _normalize_path(path: str) -> str:
    p = path.strip()
    while p.startswith("./"):
        p = p[2:]
    while p.startswith("/"):
        p = p[1:]
    return p


# ---------------------------------------------------------------------------
# Selector validation (F2): allowed roots + zero-match glob rejection
# ---------------------------------------------------------------------------

#: Allowed selector roots (a selector must live under one of these).
_ALLOWED_SELECTOR_ROOTS: Tuple[str, ...] = ("tests/", "e2e-fixture/tests/")


def _selector_matches(sel: str) -> bool:
    """Deterministically resolve a selector against the real filesystem.

    Directory selectors (ending in ``/``) must name an existing, non-empty
    directory.  Glob selectors must match at least one real file.
    """
    stripped = sel.rstrip("/")
    if sel.endswith("/"):
        d = _PROJECT_ROOT / stripped
        if not d.is_dir():
            return False
        return any(d.iterdir())
    matches = glob.glob(sel, root_dir=str(_PROJECT_ROOT), recursive=True)
    return bool(matches)


def _validate_selector(sel: str, err_cls: type = InventoryError) -> None:
    """Fail-closed selector validation: safe root + non-zero matches."""
    if not isinstance(sel, str) or not sel:
        raise err_cls("selector must be a non-empty string")
    s = sel.strip()
    if s.startswith("/") or ".." in s:
        raise err_cls(f"unsafe selector {sel!r} (no absolute paths, no '..')")
    if not any(s.startswith(root) or s == root.rstrip("/") for root in _ALLOWED_SELECTOR_ROOTS):
        raise err_cls(
            f"selector {sel!r} must be under one of {_ALLOWED_SELECTOR_ROOTS!r}"
        )
    if not _selector_matches(s):
        raise err_cls(f"selector {sel!r} matches no test files")


# ---------------------------------------------------------------------------
# Change evidence (trusted facts, NOT agent prose)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangeEvidence:
    """Bounded, trusted facts about a change set.

    There is deliberately **no** free-form "this is a small change" field: the
    controller derives impact and risk from these facts only.
    """

    changed_paths: Tuple[str, ...]
    base_ref: Optional[str] = None
    schema_migration: bool = False
    phase_closing: bool = False
    security_reviewed: bool = False


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubsystemTests:
    """Deeply immutable per-subsystem test selectors."""

    phase: Optional[str]
    module_selectors: Tuple[str, ...]
    phase_selectors: Tuple[str, ...]


@dataclass(frozen=True)
class TestInventory:
    """Versioned, deeply immutable, fail-closed test inventory."""

    version: str
    module_ownership: MappingProxyType  # path -> Subsystem.value
    subsystem_tests: MappingProxyType  # Subsystem.value -> SubsystemTests
    targeted_tests: MappingProxyType  # module basename -> Tuple[selector, ...]
    manual_suites: MappingProxyType  # path -> documented reason (non-authoritative)
    full_suite_selector: str
    _hash: str

    @property
    def content_hash(self) -> str:
        return self._hash

    def subsystem_for_path(self, path: str) -> Optional[str]:
        """Resolve a repo-relative path to a subsystem value (or None).

        Basename collisions are rejected at load time (fix F9), so the
        basename fallback is deterministic.
        """
        norm = _normalize_path(path)
        direct = self.module_ownership.get(norm)
        if direct is not None:
            return direct
        basename = norm.rsplit("/", 1)[-1]
        if basename in self.module_ownership:
            return self.module_ownership[basename]
        for key, sub in self.module_ownership.items():
            if key.rsplit("/", 1)[-1] == basename:
                return sub
        return None

    def module_selectors(self, subsystem: str) -> Tuple[str, ...]:
        entry = self.subsystem_tests.get(subsystem)
        if entry is None:
            return ()
        return entry.module_selectors

    def phase_selectors(self, subsystem: str) -> Tuple[str, ...]:
        entry = self.subsystem_tests.get(subsystem)
        if entry is None:
            return ()
        return entry.phase_selectors

    def targeted_for(self, path: str) -> Tuple[str, ...]:
        basename = path.rsplit("/", 1)[-1]
        return tuple(self.targeted_tests.get(basename, ()))

    def is_documentation_path(self, path: str) -> bool:
        """A path is documentation ONLY under ``docs/`` or as a root ``*.md``.

        Markdown under ``tests/``, ``e2e-fixture/`` or product directories is
        NOT documentation (fix F3).
        """
        norm = _normalize_path(path)
        if norm.startswith("docs/"):
            return True
        if "/" not in norm and norm.endswith(".md"):
            return True
        return False

    def is_test_infra_path(self, path: str) -> bool:
        norm = _normalize_path(path)
        sub = self.subsystem_for_path(norm)
        if sub == Subsystem.TEST_INFRA.value:
            return True
        if norm.startswith("tests/") or norm == "tests":
            return True
        if norm.startswith("e2e-fixture/"):
            return True
        if norm == "conftest.py" or norm.endswith("_helpers.py"):
            return True
        if norm.endswith("/conftest.py"):
            return True
        return False

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TestInventory":
        try:
            version = data["inventory_version"]
        except KeyError as exc:
            raise InventoryError("inventory: missing 'inventory_version'") from exc
        if version != INVENTORY_VERSION:
            raise InventoryError(
                f"inventory: unsupported version {version!r} (expected {INVENTORY_VERSION!r})"
            )

        ownership_raw = data.get("module_ownership")
        if not isinstance(ownership_raw, dict) or not ownership_raw:
            raise InventoryError("inventory: 'module_ownership' must be a non-empty object")

        subsystem_tests_raw = data.get("subsystem_tests")
        if not isinstance(subsystem_tests_raw, dict):
            raise InventoryError("inventory: 'subsystem_tests' must be an object")

        known_subsystems = {s.value for s in Subsystem}
        for sub in subsystem_tests_raw:
            if sub not in known_subsystems:
                raise InventoryError(f"inventory: unknown subsystem {sub!r} in subsystem_tests")

        ownership: Dict[str, str] = {}
        for path, sub in ownership_raw.items():
            if not isinstance(path, str) or not path:
                raise InventoryError("inventory: module_ownership keys must be non-empty strings")
            if sub not in known_subsystems:
                raise InventoryError(
                    f"inventory: module_ownership[{path!r}] -> unknown subsystem {sub!r}"
                )
            ownership[path] = sub

        # F1(e): planner-owned paths must be TEST_INFRA (no self-reclass).
        for path, sub in ownership.items():
            if path in _PLANNER_OWNED_EXACT_PATHS or any(
                path.startswith(pfx) for pfx in _PLANNER_OWNED_PATH_PREFIXES
            ):
                if sub != Subsystem.TEST_INFRA.value:
                    raise InventoryError(
                        f"inventory: planner-owned path {path!r} must be TEST_INFRA, got {sub!r}"
                    )

        # F9: reject ambiguous basenames (same basename -> different subsystems).
        basename_subsys: Dict[str, set] = {}
        for path, sub in ownership.items():
            basename_subsys.setdefault(path.rsplit("/", 1)[-1], set()).add(sub)
        for bn, subs in basename_subsys.items():
            if len(subs) > 1:
                raise InventoryError(
                    f"inventory: basename {bn!r} maps to multiple subsystems "
                    f"{sorted(subs)!r} (ambiguous)"
                )

        # Subsystem tests (deeply immutable).
        norm_subsys_tests: Dict[str, SubsystemTests] = {}
        for sub, entry in subsystem_tests_raw.items():
            if not isinstance(entry, dict):
                raise InventoryError(f"inventory: subsystem_tests[{sub!r}] must be an object")
            phase = entry.get("phase")
            if phase is not None and not isinstance(phase, str):
                raise InventoryError(f"inventory: subsystem_tests[{sub!r}].phase must be string/null")
            module_sels = entry.get("module_selectors", ())
            phase_sels = entry.get("phase_selectors", ())
            for field, sels in (("module_selectors", module_sels), ("phase_selectors", phase_sels)):
                if not isinstance(sels, list) or not all(isinstance(s, str) and s for s in sels):
                    raise InventoryError(
                        f"inventory: subsystem_tests[{sub!r}].{field} must be a list of non-empty strings"
                    )
                for sel in sels:
                    _validate_selector(sel, InventoryError)
            norm_subsys_tests[sub] = SubsystemTests(
                phase=phase,
                module_selectors=tuple(module_sels),
                phase_selectors=tuple(phase_sels),
            )

        # Targeted tests.
        targeted_raw = data.get("targeted_tests")
        if not isinstance(targeted_raw, dict):
            raise InventoryError("inventory: 'targeted_tests' must be an object")
        targeted: Dict[str, Tuple[str, ...]] = {}
        for mod, sels in targeted_raw.items():
            if not isinstance(mod, str) or not mod:
                raise InventoryError("inventory: targeted_tests keys must be non-empty strings")
            if not isinstance(sels, list) or not all(isinstance(s, str) and s for s in sels):
                raise InventoryError(
                    f"inventory: targeted_tests[{mod!r}] must be a list of non-empty strings"
                )
            for sel in sels:
                _validate_selector(sel, InventoryError)
            targeted[mod] = tuple(sels)

        # Manual / non-authoritative suites (documented, no auto selectors).
        manual_raw = data.get("manual_suites", {})
        if not isinstance(manual_raw, dict):
            raise InventoryError("inventory: 'manual_suites' must be an object")
        manual: Dict[str, str] = {}
        for path, reason in manual_raw.items():
            if not isinstance(path, str) or not path:
                raise InventoryError("inventory: manual_suites keys must be non-empty strings")
            if not isinstance(reason, str) or not reason:
                raise InventoryError("inventory: manual_suites values must be non-empty strings")
            manual[path] = reason

        full_suite = data.get("full_suite_selector")
        if not isinstance(full_suite, str) or not full_suite:
            raise InventoryError("inventory: 'full_suite_selector' must be a non-empty string")
        _validate_selector(full_suite, InventoryError)

        return cls(
            version=version,
            module_ownership=MappingProxyType(dict(ownership)),
            subsystem_tests=MappingProxyType(norm_subsys_tests),
            targeted_tests=MappingProxyType(targeted),
            manual_suites=MappingProxyType(manual),
            full_suite_selector=full_suite,
            _hash=sha256_hex(canonical_bytes(data)),
        )


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HardInvariant:
    """Deeply immutable hard safety regression requirement."""

    required_regression: Tuple[str, ...]
    full_suite: bool


@dataclass(frozen=True)
class HandlingPolicy:
    """Deeply immutable handling rule (UNKNOWN / test-infra)."""

    policy: Optional[str]
    required_regression: Tuple[str, ...]
    full_suite: bool


@dataclass(frozen=True)
class TestPolicy:
    """Versioned, deeply immutable, fail-closed test-economy policy."""

    version: str
    risk_raising_changes: MappingProxyType  # RiskTag.value -> RiskLevel.value
    subsystem_risk_tags: MappingProxyType  # Subsystem.value -> Tuple[RiskTag.value, ...]
    module_tag_overrides: MappingProxyType  # path -> Tuple[RiskTag.value, ...]
    hard_invariants: MappingProxyType  # Subsystem.value -> HardInvariant
    module_hard_invariants: MappingProxyType  # path -> HardInvariant
    full_suite_required_when: FrozenSet[str]
    unknown_handling: HandlingPolicy
    test_infra_handling: HandlingPolicy
    _hash: str

    @property
    def content_hash(self) -> str:
        return self._hash

    def risk_level_for_tag(self, tag: str) -> RiskLevel:
        raw = self.risk_raising_changes.get(tag)
        if raw is None:
            return RiskLevel.LOW
        return RiskLevel(raw)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TestPolicy":
        try:
            version = data["policy_version"]
        except KeyError as exc:
            raise PolicyError("policy: missing 'policy_version'") from exc
        if version != POLICY_VERSION:
            raise PolicyError(
                f"policy: unsupported version {version!r} (expected {POLICY_VERSION!r})"
            )

        known_tags = {t.value for t in RiskTag}
        known_risks = {r.value for r in RiskLevel}
        known_subsystems = {s.value for s in Subsystem}

        # risk_raising_changes
        rrc_raw = data.get("risk_raising_changes")
        if not isinstance(rrc_raw, dict) or not rrc_raw:
            raise PolicyError("policy: 'risk_raising_changes' must be a non-empty object")
        risk_raising: Dict[str, str] = {}
        for tag, lvl in rrc_raw.items():
            if tag not in known_tags:
                raise PolicyError(f"policy: unknown risk tag {tag!r}")
            if lvl not in known_risks:
                raise PolicyError(f"policy: risk_raising_changes[{tag!r}] -> unknown level {lvl!r}")
            risk_raising[tag] = lvl

        # F1(a): mandatory risk tags must exist with at least the floor level.
        for tag, min_lvl in _MANDATORY_RISK_TAGS.items():
            got = risk_raising.get(tag)
            if got is None:
                raise PolicyError(f"policy: mandatory risk tag {tag!r} is missing")
            if _RISK_RANK[got] < _RISK_RANK[min_lvl]:
                raise PolicyError(
                    f"policy: risk tag {tag!r} level {got!r} below mandatory floor {min_lvl!r}"
                )

        # subsystem_risk_tags
        srt_raw = data.get("subsystem_risk_tags")
        if not isinstance(srt_raw, dict):
            raise PolicyError("policy: 'subsystem_risk_tags' must be an object")
        subsystem_tags: Dict[str, Tuple[str, ...]] = {}
        for sub, tags in srt_raw.items():
            if sub not in known_subsystems:
                raise PolicyError(f"policy: unknown subsystem {sub!r} in subsystem_risk_tags")
            if not isinstance(tags, list) or not all(t in known_tags for t in tags):
                raise PolicyError(
                    f"policy: subsystem_risk_tags[{sub!r}] must be a list of known risk tags"
                )
            for t in tags:
                if t not in risk_raising:
                    raise PolicyError(
                        f"policy: subsystem_risk_tags[{sub!r}] tag {t!r} has no risk level"
                    )
            subsystem_tags[sub] = tuple(tags)

        # module_tag_overrides
        mto_raw = data.get("module_tag_overrides")
        if not isinstance(mto_raw, dict):
            raise PolicyError("policy: 'module_tag_overrides' must be an object")
        overrides: Dict[str, Tuple[str, ...]] = {}
        for path, tags in mto_raw.items():
            if not isinstance(path, str) or not path:
                raise PolicyError("policy: module_tag_overrides keys must be non-empty strings")
            if not isinstance(tags, list) or not all(t in known_tags for t in tags):
                raise PolicyError(
                    f"policy: module_tag_overrides[{path!r}] must be a list of known risk tags"
                )
            for t in tags:
                if t not in risk_raising:
                    raise PolicyError(
                        f"policy: module_tag_overrides[{path!r}] tag {t!r} has no risk level"
                    )
            overrides[path] = tuple(tags)

        # hard_invariants
        hi_raw = data.get("hard_invariants")
        if not isinstance(hi_raw, dict):
            raise PolicyError("policy: 'hard_invariants' must be an object")
        invariants: Dict[str, HardInvariant] = {}
        for sub, entry in hi_raw.items():
            if sub not in known_subsystems:
                raise PolicyError(f"policy: unknown subsystem {sub!r} in hard_invariants")
            if not isinstance(entry, dict):
                raise PolicyError(f"policy: hard_invariants[{sub!r}] must be an object")
            req = entry.get("required_regression", ())
            if not isinstance(req, list) or not all(isinstance(s, str) and s for s in req):
                raise PolicyError(
                    f"policy: hard_invariants[{sub!r}].required_regression must be a list of strings"
                )
            for sel in req:
                _validate_selector(sel, PolicyError)
            fs = entry.get("full_suite", False)
            if not isinstance(fs, bool):
                raise PolicyError(f"policy: hard_invariants[{sub!r}].full_suite must be bool")
            invariants[sub] = HardInvariant(required_regression=tuple(req), full_suite=fs)

        # F1(b): core hard-invariant subsystems must exist with full_suite=True.
        for sub in _CORE_HARD_INVARIANT_SUBSYSTEMS:
            inv = invariants.get(sub)
            if inv is None:
                raise PolicyError(f"policy: hard_invariants missing core subsystem {sub!r}")
            if inv.full_suite is not True:
                raise PolicyError(
                    f"policy: hard_invariants[{sub!r}].full_suite must be true (core floor)"
                )

        # module_hard_invariants (per-module obligations, e.g. checkpoint.py)
        mhi_raw = data.get("module_hard_invariants", {})
        if not isinstance(mhi_raw, dict):
            raise PolicyError("policy: 'module_hard_invariants' must be an object")
        module_invariants: Dict[str, HardInvariant] = {}
        for path, entry in mhi_raw.items():
            if not isinstance(path, str) or not path:
                raise PolicyError("policy: module_hard_invariants keys must be non-empty strings")
            if not isinstance(entry, dict):
                raise PolicyError(f"policy: module_hard_invariants[{path!r}] must be an object")
            req = entry.get("required_regression", ())
            if not isinstance(req, list) or not all(isinstance(s, str) and s for s in req):
                raise PolicyError(
                    f"policy: module_hard_invariants[{path!r}].required_regression must be a list of strings"
                )
            for sel in req:
                _validate_selector(sel, PolicyError)
            fs = entry.get("full_suite", False)
            if not isinstance(fs, bool):
                raise PolicyError(f"policy: module_hard_invariants[{path!r}].full_suite must be bool")
            module_invariants[path] = HardInvariant(required_regression=tuple(req), full_suite=fs)

        # full_suite_required_when
        fsw_raw = data.get("full_suite_required_when")
        if not isinstance(fsw_raw, list) or not fsw_raw:
            raise PolicyError("policy: 'full_suite_required_when' must be a non-empty list")
        fsw: FrozenSet[str] = frozenset(fsw_raw)
        unknown_conds = fsw - _FULL_SUITE_CONDITIONS
        if unknown_conds:
            raise PolicyError(
                f"policy: unknown full_suite condition(s) {sorted(unknown_conds)!r}"
            )
        # F1(d): mandatory full-suite conditions must be present.
        missing_conds = [c for c in _MANDATORY_FULL_SUITE_CONDITIONS if c not in fsw]
        if missing_conds:
            raise PolicyError(
                f"policy: full_suite_required_when missing mandatory condition(s) {missing_conds!r}"
            )

        # unknown_handling / test_infra_handling
        def _parse_handling(name: str, *, require_full_suite: bool) -> HandlingPolicy:
            raw = data.get(name)
            if not isinstance(raw, dict):
                raise PolicyError(f"policy: '{name}' must be an object")
            req = raw.get("required_regression", ())
            if not isinstance(req, list) or not all(isinstance(s, str) and s for s in req):
                raise PolicyError(f"policy: {name}.required_regression must be a list of strings")
            for sel in req:
                _validate_selector(sel, PolicyError)
            fs = raw.get("full_suite", False)
            if not isinstance(fs, bool):
                raise PolicyError(f"policy: {name}.full_suite must be bool")
            if require_full_suite and fs is not True:
                raise PolicyError(f"policy: {name}.full_suite must be true (mandatory floor)")
            policy_val = raw.get("policy")
            if policy_val is not None and not isinstance(policy_val, str):
                raise PolicyError(f"policy: {name}.policy must be string/null")
            return HandlingPolicy(
                policy=policy_val,
                required_regression=tuple(req),
                full_suite=fs,
            )

        return cls(
            version=version,
            risk_raising_changes=MappingProxyType(dict(risk_raising)),
            subsystem_risk_tags=MappingProxyType(dict(subsystem_tags)),
            module_tag_overrides=MappingProxyType(dict(overrides)),
            hard_invariants=MappingProxyType(dict(invariants)),
            module_hard_invariants=MappingProxyType(dict(module_invariants)),
            full_suite_required_when=fsw,
            unknown_handling=_parse_handling("unknown_handling", require_full_suite=True),
            test_infra_handling=_parse_handling("test_infra_handling", require_full_suite=True),
            _hash=sha256_hex(canonical_bytes(data)),
        )


# ---------------------------------------------------------------------------
# Impact + risk derivation (trusted evidence -> deterministic classification)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangeImpact:
    """Deterministically derived impact of a change set."""

    subsystems: FrozenSet[str]
    risk_tags: FrozenSet[str]
    changed_modules: Tuple[str, ...]
    unknown_paths: Tuple[str, ...]
    risk_level: RiskLevel
    schema_migration: bool
    test_infrastructure: bool
    multiple_subsystems: bool
    documentation_only: bool


@dataclass(frozen=True)
class PlanStage:
    """One ordered stage of selectors with per-selector reasons + mandatory set."""

    name: str
    selectors: Tuple[str, ...]
    reasons: MappingProxyType  # selector -> Tuple[reason, ...] (sorted)
    mandatory: Tuple[str, ...]  # mandatory selectors in this stage (sorted)


@dataclass(frozen=True)
class TestPlan:
    """The deterministic, reproducible test plan + bounded provenance."""

    risk_level: RiskLevel
    documentation_only: bool
    full_suite_required: bool
    stages: Tuple[PlanStage, ...]
    policy_version: str
    policy_hash: str
    inventory_version: str
    inventory_hash: str
    change_set_hash: str
    escalation_reasons: Tuple[str, ...]
    plan_hash: str

    def all_selectors(self) -> Tuple[str, ...]:
        out: List[str] = []
        for st in self.stages:
            out.extend(st.selectors)
        return tuple(out)

    def mandatory_selectors(self) -> FrozenSet[str]:
        out: set = set()
        for st in self.stages:
            out.update(st.mandatory)
        return frozenset(out)


def derive_change_impact(
    evidence: ChangeEvidence, inventory: TestInventory, policy: TestPolicy
) -> ChangeImpact:
    """Deterministically classify a change set into impact + risk.

    Never returns a risk derived from anything but trusted evidence.
    """
    paths = evidence.changed_paths
    if not paths:
        raise TestPlanningError("change evidence: 'changed_paths' must be non-empty")

    norm_paths = tuple(_normalize_path(p) for p in paths)

    subsystems: set = set()
    tags: set = set()
    changed_modules: List[str] = []
    unknown: List[str] = []
    test_infra = False
    doc_paths = 0

    for p in norm_paths:
        # Test-infrastructure FIRST (fix F3): a .md under tests/ or
        # e2e-fixture/ is test infra, never documentation.
        if inventory.is_test_infra_path(p):
            sub = Subsystem.TEST_INFRA.value
            test_infra = True
            subsystems.add(sub)
            for tag in policy.subsystem_risk_tags.get(sub, ()):
                tags.add(tag)
            continue
        # Documentation second: only docs/ or root *.md.
        if inventory.is_documentation_path(p):
            subsystems.add(Subsystem.DOCUMENTATION.value)
            doc_paths += 1
            continue
        sub = inventory.subsystem_for_path(p)
        if sub is None:
            unknown.append(p)
            continue
        subsystems.add(sub)
        for tag in policy.subsystem_risk_tags.get(sub, ()):
            tags.add(tag)
        override = policy.module_tag_overrides.get(p)
        if override is None:
            override = policy.module_tag_overrides.get(p.rsplit("/", 1)[-1])
        if override:
            for tag in override:
                tags.add(tag)
        changed_modules.append(p)

    subsystems_frozen = frozenset(subsystems)
    tags_frozen = frozenset(tags)

    risk = RiskLevel.LOW
    for tag in tags_frozen:
        risk = _max_risk(risk, policy.risk_level_for_tag(tag))

    schema_migration = evidence.schema_migration or (
        Subsystem.PERSISTENCE.value in subsystems_frozen
    )
    if schema_migration:
        risk = _max_risk(risk, RiskLevel.CRITICAL)

    if unknown:
        risk = _max_risk(risk, RiskLevel.MEDIUM)

    substantive = {
        s
        for s in subsystems_frozen
        if s not in (Subsystem.DOCUMENTATION.value, Subsystem.TEST_INFRA.value)
    }
    multiple = len(substantive) >= 2
    if multiple:
        risk = _max_risk(risk, RiskLevel.HIGH)

    doc_only = doc_paths == len(norm_paths) and doc_paths > 0

    return ChangeImpact(
        subsystems=subsystems_frozen,
        risk_tags=tags_frozen,
        changed_modules=tuple(sorted(changed_modules)),
        unknown_paths=tuple(sorted(unknown)),
        risk_level=risk,
        schema_migration=schema_migration,
        test_infrastructure=test_infra,
        multiple_subsystems=multiple,
        documentation_only=doc_only,
    )


def _full_suite_required(
    evidence: ChangeEvidence, impact: ChangeImpact, policy: TestPolicy
) -> Tuple[bool, Tuple[str, ...]]:
    reasons: List[str] = []
    conds = policy.full_suite_required_when

    def check(cond: str, true: bool, reason: str) -> None:
        if cond in conds and true:
            reasons.append(reason)

    check("phase_closing", evidence.phase_closing, "phase closing workflow")
    check("risk_CRITICAL", impact.risk_level == RiskLevel.CRITICAL, "risk CRITICAL")
    check("risk_HIGH", impact.risk_level == RiskLevel.HIGH, "risk HIGH")
    check(
        "test_infrastructure_change",
        impact.test_infrastructure,
        "test infrastructure modified",
    )
    check("schema_migration", impact.schema_migration, "schema/migration change")
    check(
        "multiple_subsystems",
        impact.multiple_subsystems,
        "multiple subsystems changed",
    )
    check(
        "test_plan_uncertainty",
        bool(impact.unknown_paths),
        "unknown changed path(s) -> conservative breadth",
    )
    check(
        "planner_metadata_uncertainty",
        False,  # no separate signal in F1; metadata fail-closes on load instead
        "planner metadata uncertainty",
    )
    check(
        "security_reviewed_patch",
        evidence.security_reviewed,
        "independently reviewed security-sensitive patch",
    )

    # Hard invariants may force full suite even beyond the risk rule.
    for sub in impact.subsystems:
        inv = policy.hard_invariants.get(sub)
        if inv is not None and inv.full_suite:
            reasons.append(f"hard invariant {sub} requires full suite")

    return bool(reasons), tuple(sorted(set(reasons)))


# ---------------------------------------------------------------------------
# Deterministic plan generation
# ---------------------------------------------------------------------------


def build_test_plan(
    change_evidence: ChangeEvidence,
    policy: TestPolicy,
    inventory: TestInventory,
) -> TestPlan:
    """Deterministically build a staged, reproducible test plan.

    Same inputs -> same :class:`TestPlan` (including identical ``plan_hash``).

    Reasons are accumulated per selector (never lost to first-wins dedup), and
    every mandatory selector keeps an explicit mandatory marking (fix F8).
    """
    impact = derive_change_impact(change_evidence, inventory, policy)

    reasons_by_sel: Dict[str, List[str]] = {}
    mandatory_sels: set = set()
    stage_of_sel: Dict[str, str] = {}

    def add(stage: str, sel: str, reason: str, mandatory: bool = False) -> None:
        lst = reasons_by_sel.setdefault(sel, [])
        if reason not in lst:
            lst.append(reason)
        if mandatory:
            mandatory_sels.add(sel)
        if sel not in stage_of_sel:
            stage_of_sel[sel] = stage

    # Targeted: exact test files for the changed modules.
    for mod in impact.changed_modules:
        for sel in inventory.targeted_for(mod):
            add("targeted", sel, f"directly targets changed module {mod}")

    # Module regression for every impacted subsystem.
    for sub in sorted(impact.subsystems):
        if sub == Subsystem.DOCUMENTATION.value:
            continue
        for sel in inventory.module_selectors(sub):
            add("module", sel, f"module regression for subsystem {sub}")

    # Phase regression + hard invariants.
    for sub in sorted(impact.subsystems):
        if sub == Subsystem.DOCUMENTATION.value:
            continue
        inv = policy.hard_invariants.get(sub)
        hard = inv is not None
        if impact.risk_level != RiskLevel.LOW or hard:
            for sel in inventory.phase_selectors(sub):
                add(
                    "phase_regression",
                    sel,
                    f"phase regression for subsystem {sub}"
                    + (" (HARD INVARIANT)" if hard else ""),
                )
            if inv is not None:
                for sel in inv.required_regression:
                    add("phase_regression", sel, f"HARD INVARIANT {sub}: {sel}", mandatory=True)

    # Per-module hard invariants (fix F7: e.g. checkpoint.py).
    for mod in impact.changed_modules:
        mhi = policy.module_hard_invariants.get(mod)
        if mhi is None:
            mhi = policy.module_hard_invariants.get(mod.rsplit("/", 1)[-1])
        if mhi is not None:
            for sel in mhi.required_regression:
                add("phase_regression", sel, f"MODULE HARD INVARIANT {mod}: {sel}", mandatory=True)
            if mhi.full_suite:
                add("full_suite", inventory.full_suite_selector,
                    f"module hard invariant {mod} requires full suite", mandatory=True)

    # UNKNOWN paths -> conservative broaden (never narrow).
    if impact.unknown_paths:
        uh = policy.unknown_handling
        for sel in uh.required_regression:
            add("phase_regression", sel, "UNKNOWN path -> conservative broaden", mandatory=True)
        if uh.full_suite:
            add("full_suite", inventory.full_suite_selector,
                "UNKNOWN path -> full suite", mandatory=True)

    # Test infrastructure changes -> broad closing regression + full suite.
    if impact.test_infrastructure:
        th = policy.test_infra_handling
        for sel in th.required_regression:
            add("phase_regression", sel, "test infrastructure modified -> broad regression", mandatory=True)
        if th.full_suite:
            add("full_suite", inventory.full_suite_selector,
                "test infrastructure modified -> full suite", mandatory=True)

    # Full-suite policy.
    full_required, full_reasons = _full_suite_required(change_evidence, impact, policy)
    if full_required:
        add("full_suite", inventory.full_suite_selector,
            "full suite required by policy", mandatory=True)

    # Build ordered stages.
    stages: List[PlanStage] = []
    for name in ("targeted", "module", "phase_regression", "full_suite"):
        sels = sorted(s for s, st in stage_of_sel.items() if st == name)
        if not sels:
            continue
        stages.append(
            PlanStage(
                name=name,
                selectors=tuple(sels),
                reasons=MappingProxyType(
                    {s: tuple(sorted(reasons_by_sel[s])) for s in sels}
                ),
                mandatory=tuple(sorted(m for m in mandatory_sels if m in sels)),
            )
        )

    escalation = tuple(full_reasons)
    if impact.unknown_paths:
        escalation = tuple(sorted(set(escalation) | {"unknown path(s) escalated"}))
    if impact.test_infrastructure:
        escalation = tuple(sorted(set(escalation) | {"test infrastructure escalated"}))

    change_set_hash = sha256_hex(
        canonical_bytes(
            {
                "changed_paths": sorted(_normalize_path(p) for p in change_evidence.changed_paths),
                "base_ref": change_evidence.base_ref,
                "schema_migration": change_evidence.schema_migration,
                "phase_closing": change_evidence.phase_closing,
                "security_reviewed": change_evidence.security_reviewed,
            }
        )
    )

    plan_hash = sha256_hex(
        canonical_bytes(
            {
                "risk": impact.risk_level.value,
                "full_suite_required": full_required,
                "stages": [
                    {
                        "name": st.name,
                        "selectors": list(st.selectors),
                        "reasons": {s: list(r) for s, r in st.reasons.items()},
                        "mandatory": list(st.mandatory),
                    }
                    for st in stages
                ],
                "policy_hash": policy.content_hash,
                "inventory_hash": inventory.content_hash,
                "change_set_hash": change_set_hash,
            }
        )
    )

    return TestPlan(
        risk_level=impact.risk_level,
        documentation_only=impact.documentation_only,
        full_suite_required=full_required,
        stages=tuple(stages),
        policy_version=policy.version,
        policy_hash=policy.content_hash,
        inventory_version=inventory.version,
        inventory_hash=inventory.content_hash,
        change_set_hash=change_set_hash,
        escalation_reasons=escalation,
        plan_hash=plan_hash,
    )


# ---------------------------------------------------------------------------
# Default singletons (mirror the evidence_registry convention)
# ---------------------------------------------------------------------------

_REGISTRY_DIR = Path(__file__).resolve().parent / "registry"

_default_inventory: Optional[TestInventory] = None
_default_policy: Optional[TestPolicy] = None


def load_inventory(path: Optional[str] = None) -> TestInventory:
    p = Path(path) if path else _REGISTRY_DIR / "test_inventory_v1.json"
    return TestInventory.from_dict(_load_json(p))


def load_policy(path: Optional[str] = None) -> TestPolicy:
    p = Path(path) if path else _REGISTRY_DIR / "test_policy_v1.json"
    return TestPolicy.from_dict(_load_json(p))


def get_default_inventory() -> TestInventory:
    global _default_inventory
    if _default_inventory is None:
        _default_inventory = load_inventory()
    return _default_inventory


def get_default_policy() -> TestPolicy:
    global _default_policy
    if _default_policy is None:
        _default_policy = load_policy()
    return _default_policy


def reset_defaults() -> None:
    global _default_inventory, _default_policy
    _default_inventory = None
    _default_policy = None
