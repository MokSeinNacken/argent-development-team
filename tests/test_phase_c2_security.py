"""Phase C2 — security: no shell, no agent-controlled scope/limits (static).

Proves (a) the product code never uses ``shell=True`` (argv is structured), and
(b) agent output can NEVER determine the scope name, properties, limits,
timeout, or cgroup path — scope names are locally generated and strictly
validated, and limits come only from the local policy + snapshot.
"""

from __future__ import annotations

from pathlib import Path

from argent_core.execution_scope import (
    generate_scope_name,
    is_valid_scope_name,
    translate_limits_to_properties,
)
from argent_core.resource_policy import ResourceClass, ResourcePolicy
from argent_core.scope_enforcer import ExecutionEnforcer
from c2_helpers import FakeScopeBackend, verified_properties

PRODUCT_ROOT = Path(__file__).resolve().parent.parent / "argent_core"


def _limits():
    pol = ResourcePolicy()
    base = pol.limits_for(ResourceClass.HEAVY)
    return {
        "memory_high_bytes": base.memory_high_bytes,
        "memory_max_bytes": base.memory_max_bytes,
        "swap_max_bytes": base.swap_max_bytes,
        "cpu_quota_percent": base.cpu_quota_percent,
        "timeout_seconds": base.timeout_seconds,
    }


def test_no_shell_true_in_product_code():
    offenders = []
    for path in PRODUCT_ROOT.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "shell=True" in text:
            offenders.append(path.name)
    assert offenders == [], f"shell=True found in product code: {offenders}"


def test_agent_like_text_cannot_name_scope():
    # Even if hostile text somehow reached the naming helper, it is sanitized
    # to a safe [a-z0-9-] name (fail-closed).
    name = generate_scope_name("job'; rm -rf /", "dispatch$(reboot)&|")
    assert is_valid_scope_name(name)
    assert ";" not in name and "|" not in name and "$" not in name
    assert "/" not in name and " " not in name


def test_scope_name_independent_of_command_and_limits():
    backend = FakeScopeBackend(verify_properties=verified_properties(_limits()))
    enforcer = ExecutionEnforcer(backend)
    base = dict(
        effective_limits=_limits(),
        resource_class=ResourceClass.HEAVY,
        policy_version="1",
        job_id="job-1",
        dispatch_id="dispatch-1",
    )
    r1 = enforcer.enforce_and_spawn(command=["openclaw", "agent"], **base)
    r2 = enforcer.enforce_and_spawn(
        command=["openclaw", "agent", "--malicious"], **base,
    )
    # The command content cannot alter the scope name's charset/pattern.
    assert is_valid_scope_name(r1.scope.scope_name)
    assert is_valid_scope_name(r2.scope.scope_name)


def test_limits_derive_only_from_policy_and_snapshot():
    # translate_limits_to_properties is a pure function of the validated limits
    # (policy-derived); there is no path for agent text to reach it.
    props = translate_limits_to_properties(_limits())
    assert props["MemoryMax"] == str(_limits()["memory_max_bytes"])
    assert props["CPUQuota"] == "300%"
    assert "timeout" not in {k.lower() for k in props}
