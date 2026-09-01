"""Phase C2 — scope naming + creation (deterministic, no live systemd).

Proves that scope names are locally generated and strictly validated (agent text
can NEVER name a scope), that properties are translated correctly, and that a
backend create failure is fail-closed (no unbounded process).
"""

from __future__ import annotations

from argent_core.execution_scope import (
    SCOPE_NAME_MAX_LEN,
    SCOPE_NAME_PREFIX,
    VERIFICATION_VERIFIED,
    ScopeCreateError,
    ScopeVerificationError,
    generate_scope_name,
    is_valid_scope_name,
    sanitize_scope_name,
    translate_limits_to_properties,
)
from argent_core.resource_policy import ResourceClass, ResourcePolicy
from argent_core.scope_enforcer import EnforcementStatus, ExecutionEnforcer
from c2_helpers import FakeScopeBackend, verified_properties


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


# ---------------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------------

def test_generated_scope_names_are_safe_and_unique():
    n1 = generate_scope_name("job-abc", "dispatch-xyz")
    n2 = generate_scope_name("job-abc", "dispatch-xyz")
    assert is_valid_scope_name(n1)
    assert is_valid_scope_name(n2)
    assert n1 != n2  # random hex makes each spawn attempt unique
    assert n1.startswith(SCOPE_NAME_PREFIX + "-")
    assert len(n1) <= SCOPE_NAME_MAX_LEN
    assert n1 == n1.lower()


def test_is_valid_scope_name_accepts_and_rejects():
    assert is_valid_scope_name("argent-c2-abc-def-01234567")
    assert is_valid_scope_name("a")
    assert not is_valid_scope_name("")
    assert not is_valid_scope_name(None)
    assert not is_valid_scope_name(123)
    assert not is_valid_scope_name("UpperCase")
    assert not is_valid_scope_name("has space")
    assert not is_valid_scope_name("a/b")
    assert not is_valid_scope_name("-leading")
    assert not is_valid_scope_name("trailing-")
    assert not is_valid_scope_name("a" * (SCOPE_NAME_MAX_LEN + 1))
    # Injection attempts must be rejected (never a shell metacharacter path).
    assert not is_valid_scope_name("foo; rm -rf /")
    assert not is_valid_scope_name("foo|cat /etc/passwd")
    assert not is_valid_scope_name("foo$(reboot)")
    assert not is_valid_scope_name("foo\nbar")
    assert not is_valid_scope_name("foo.scope.service")


def test_sanitize_scope_name_reduces_arbitrary_input():
    assert sanitize_scope_name("ARGENT C2 Foo/Bar!") == "argent-c2-foo-bar"
    assert sanitize_scope_name(";;;") == ""
    assert sanitize_scope_name(None) == ""
    assert sanitize_scope_name("a--b") == "a-b"


# ---------------------------------------------------------------------------
# creation flow
# ---------------------------------------------------------------------------

def test_enforce_and_spawn_creates_verified_scope():
    backend = FakeScopeBackend(
        verify_properties=verified_properties(_limits()),
    )
    enforcer = ExecutionEnforcer(backend)
    result = enforcer.enforce_and_spawn(
        command=["openclaw", "agent"],
        effective_limits=_limits(),
        resource_class=ResourceClass.HEAVY,
        policy_version="1",
        job_id="job-1",
        dispatch_id="dispatch-1",
    )
    assert result.ok
    assert result.status == EnforcementStatus.SCOPE_OK.value
    assert result.scope is not None
    assert result.scope.process_id == 424242
    assert result.scope.cgroup_path != ""
    assert result.scope.resource_class == "HEAVY"
    assert result.scope.policy_version == "1"
    assert result.scope.verification_status == VERIFICATION_VERIFIED
    # The backend recorded exactly one scope creation (Start-Barrier: the scope
    # is created with the harmless PLACEHOLDER, NOT the agent).
    assert len(backend.created) == 1
    created = backend.created[0]
    assert created["placeholder_command"][0] == "sleep"
    # The agent command is started INSIDE the verified scope, wrapped in the
    # wall-clock timeout.
    assert len(backend.started) == 1
    started = backend.started[0]
    assert started["command"][0] == "timeout"
    assert "-k" in started["command"]
    assert str(_limits()["timeout_seconds"]) in started["command"]
    assert started["command"][-2:] == ["openclaw", "agent"]
    # Properties are the translated systemd values.
    assert created["properties"]["MemoryMax"] == str(_limits()["memory_max_bytes"])
    assert created["properties"]["CPUQuota"] == "300%"
    # The placeholder was terminated after the agent was bound (Start-Barrier).
    assert len(backend.stop_placeholder_calls) == 1


def test_enforce_and_spawn_validates_properties_translation():
    props = translate_limits_to_properties(_limits())
    assert props["MemoryHigh"] == str(_limits()["memory_high_bytes"])
    assert props["MemorySwapMax"] == str(_limits()["swap_max_bytes"])
    assert props["CPUQuota"] == f"{_limits()['cpu_quota_percent']}%"


def test_backend_create_failure_is_fail_closed():
    backend = FakeScopeBackend(fail_create=ScopeCreateError("boom"))
    enforcer = ExecutionEnforcer(backend)
    result = enforcer.enforce_and_spawn(
        command=["openclaw", "agent"],
        effective_limits=_limits(),
        resource_class=ResourceClass.HEAVY,
        policy_version="1",
        job_id="job-1",
        dispatch_id="dispatch-1",
    )
    assert not result.ok
    assert result.status == EnforcementStatus.SCOPE_CREATION_FAILED.value
    assert result.scope is None


def test_backend_verify_failure_cleans_up_scope():
    backend = FakeScopeBackend(fail_verify=ScopeVerificationError("MemoryMax"))
    enforcer = ExecutionEnforcer(backend)
    result = enforcer.enforce_and_spawn(
        command=["openclaw", "agent"],
        effective_limits=_limits(),
        resource_class=ResourceClass.HEAVY,
        policy_version="1",
        job_id="job-1",
        dispatch_id="dispatch-1",
    )
    assert not result.ok
    assert result.status == EnforcementStatus.SCOPE_VERIFICATION_FAILED.value
    # The failed scope was cleaned up (best-effort, no stray scope).
    assert len(backend.cleanup_calls) == 1


def test_invalid_limits_are_enforcement_unavailable():
    backend = FakeScopeBackend()
    enforcer = ExecutionEnforcer(backend)
    bad = dict(_limits(), memory_high_bytes=_limits()["memory_max_bytes"] + 1)
    result = enforcer.enforce_and_spawn(
        command=["openclaw", "agent"],
        effective_limits=bad,
        resource_class=ResourceClass.HEAVY,
        policy_version="1",
        job_id="job-1",
        dispatch_id="dispatch-1",
    )
    assert not result.ok
    assert result.status == EnforcementStatus.ENFORCEMENT_UNAVAILABLE.value
    assert len(backend.created) == 0  # nothing was started
