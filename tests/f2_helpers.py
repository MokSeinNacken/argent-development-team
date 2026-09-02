"""Shared deterministic helpers for the Phase F2 test suite.

Offline, no network/shell/real subprocess.  Imports the F2 executor and F1
planner, and provides fake runner/gate/store builders plus a synthetic
``TestPlan`` constructor for precise control over stage shape.

Fix-Round F1–F9 additions:

- ``TEST_MAC_KEY`` — injected deterministic HMAC key for offline tests.
- ``mk_plan`` now derives an authentic ``plan_hash`` (so F3 integrity holds).
- ``snap`` / ``pass_record`` / ``fail_record`` bind ``root``/``config_hash``.
- ``store`` injects ``TEST_MAC_KEY``.
- ``exec_plan`` wraps :func:`execute_plan` with a default *allowed* gate so the
  product-path fail-closed no-gate semantics (F6) are tested explicitly in the
  fix-round file rather than silently opted-into everywhere.
"""

from __future__ import annotations

from types import MappingProxyType

from argent_core import test_planning as tp
from argent_core.test_planning import PlanStage, RiskLevel, TestPlan
from argent_core import test_execution as te
from argent_core.test_execution import (
    EvidenceRecord,
    EvidenceStore,
    ResourceAdmission,
    ResultClass,
    RunnerOutcome,
    SnapshotIdentity,
)

#: Injected deterministic HMAC key (F4) for offline tests.  The product path
#: resolves its key from the environment, never from the agent write area.
TEST_MAC_KEY: bytes = b"argent-f2-offline-test-mac-key-0000000000000000"


class FakeRunner:
    """Deterministic selector -> outcome runner with a call log."""

    def __init__(self, outcomes=None, default: ResultClass = ResultClass.TEST_PASS):
        self.calls: list = []
        self.outcomes = dict(outcomes or {})
        self.default = default

    def run(self, selector: str) -> RunnerOutcome:
        self.calls.append(selector)
        if selector in self.outcomes:
            oc = self.outcomes[selector]
            if isinstance(oc, RunnerOutcome):
                return oc
            return RunnerOutcome(oc)
        return RunnerOutcome(self.default, test_count=1)


class FakeGate:
    def __init__(self, allowed: bool = True, reason: str = ""):
        self.allowed = allowed
        self.reason = reason
        self.calls = 0

    def admit(self) -> ResourceAdmission:
        self.calls += 1
        return ResourceAdmission(self.allowed, self.reason)


class FakeProc:
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def snap(
    source="s1",
    testdef="t1",
    executor=te.EXECUTOR_ID,
    root="",
    config_hash="",
) -> SnapshotIdentity:
    return SnapshotIdentity(source, testdef, executor, root, config_hash)


def mk_plan(
    stages,
    *,
    full_suite_required=False,
    plan_hash=None,
    inventory_hash="ih1",
    policy_hash="poh1",
) -> TestPlan:
    plan = TestPlan(
        risk_level=RiskLevel.LOW,
        documentation_only=False,
        full_suite_required=full_suite_required,
        stages=tuple(stages),
        policy_version="1",
        policy_hash=policy_hash,
        inventory_version="1",
        inventory_hash=inventory_hash,
        change_set_hash="cs1",
        escalation_reasons=(),
        plan_hash=plan_hash or "",
    )
    if plan_hash is None:
        plan = te.replace(plan, plan_hash=te.recompute_plan_hash(plan))
    return plan


def stage(name, selectors, mandatory=()):
    return PlanStage(
        name=name,
        selectors=tuple(selectors),
        reasons=MappingProxyType({s: ("reason",) for s in selectors}),
        mandatory=tuple(mandatory),
    )


def real_plan(*changed, **kw) -> TestPlan:
    inv = tp.load_inventory()
    pol = tp.load_policy()
    return tp.build_test_plan(tp.ChangeEvidence(changed), pol, inv)


def pass_record(selector, snapshot, plan) -> EvidenceRecord:
    return EvidenceRecord(
        selector=selector,
        source_hash=snapshot.source_hash,
        test_definition_hash=snapshot.test_definition_hash,
        plan_hash=plan.plan_hash,
        inventory_hash=plan.inventory_hash,
        policy_hash=plan.policy_hash,
        executor_id=snapshot.executor_id,
        classification=ResultClass.TEST_PASS,
        timestamp="2026-09-02T18:00:00Z",
        artifact_ref="abc",
        summary="",
        test_count=1,
        root=snapshot.root,
        config_hash=snapshot.config_hash,
    )


def fail_record(selector, snapshot, plan, cls=ResultClass.TEST_FAILURE) -> EvidenceRecord:
    base = pass_record(selector, snapshot, plan)
    r = te.replace(base, classification=cls)
    return te.replace(r, evidence_hash=te.compute_evidence_mac(r, TEST_MAC_KEY))


def store(path=None, records=(), mac_key: bytes = TEST_MAC_KEY):
    s = EvidenceStore(path=path, mac_key=mac_key)
    for r in records:
        s.add(r)
    return s


def exec_plan(
    plan,
    runner,
    *,
    snapshot,
    resource_gate=None,
    store=None,
    project_root=None,
):
    """``execute_plan`` with a default *allowed* gate (test convenience).

    The product path fails closed when no gate is given (F6); offline tests opt
    into an allowed gate explicitly through this helper.
    """
    if resource_gate is None:
        resource_gate = FakeGate(allowed=True)
    return te.execute_plan(
        plan,
        runner,
        snapshot=snapshot,
        resource_gate=resource_gate,
        store=store,
        project_root=project_root,
    )
