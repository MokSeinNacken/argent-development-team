"""Gated autonomy (SPEC V1 chapter 3 + V2 8.4 / V2.1 15.10).

Every action is classified into exactly one of three classes.  Anything not
explicitly listed as AUTONOMOUS or OWNER_APPROVAL_REQUIRED is classified as
FORBIDDEN (fail-closed).

Phase 2A adds a closed set of external actions (``EXTERNAL_ACTIONS``): when a
task's ``external_actions_policy`` is ``FORBIDDEN`` every external action is
classified ``FORBIDDEN`` (not approvable).  Unknown action names remain
``FORBIDDEN`` (V1 rule).
"""

from __future__ import annotations

from .models import (
    ActionClass,
    ArtifactCategory,
    ExternalActionsPolicy,
    Permission,
)

AUTONOMOUS_ACTIONS: frozenset[str] = frozenset(
    {
        "analyze",
        "implement",
        "run_tests",
        "review",
        "rework",
        "create_local_artifact",
        "git_local_commit",
        "create_handoff",
    }
)

OWNER_APPROVAL_ACTIONS: frozenset[str] = frozenset(
    {
        "deploy_production",
        "change_secrets",
        "expose_gateway",
        "modify_allowlist",
        "promote_stable",
        "modify_policy",
        "external_send",
        "install_software",
        "raise_privileges",
        "enable_self_improvement",
        "production_write",
    }
)

FORBIDDEN_ACTIONS: frozenset[str] = frozenset(
    {
        "bypass_owner_approval",
        "forge_owner_approval",
        "treat_untrusted_as_owner_approval",
        "disclose_secrets",
        "disable_security_boundary",
        "exfiltrate_data",
    }
)

# Closed set of external action names (SPEC V2 15.10).  Under a FORBIDDEN
# external-actions policy every one of these is classified FORBIDDEN.
EXTERNAL_ACTIONS: frozenset[str] = frozenset(
    {
        "install_software",
        "download_dependency",
        "system_install",
        "network_fetch",
        "external_send",
        "deploy_production",
        "change_secrets",
        "expose_gateway",
        "modify_allowlist",
        "promote_stable",
        "modify_policy",
        "raise_privileges",
        "enable_self_improvement",
        "production_write",
    }
)

# Permission requirement for each AUTONOMOUS action.  ``review`` maps to reading
# the product code (every role may read); the write-gated actions map to the
# artifact category they modify.
ACTION_PERMISSIONS: dict[str, tuple[ArtifactCategory, Permission]] = {
    "analyze": (ArtifactCategory.OTHER, Permission.WRITE),
    "implement": (ArtifactCategory.PRODUCT_CODE, Permission.WRITE),
    "run_tests": (ArtifactCategory.TEST_CODE, Permission.WRITE),
    "review": (ArtifactCategory.PRODUCT_CODE, Permission.READ),
    "rework": (ArtifactCategory.PRODUCT_CODE, Permission.WRITE),
    "create_local_artifact": (ArtifactCategory.OTHER, Permission.WRITE),
    "git_local_commit": (ArtifactCategory.PRODUCT_CODE, Permission.WRITE),
    "create_handoff": (ArtifactCategory.OTHER, Permission.WRITE),
}


def classify_action(
    action: str,
    external_actions_policy: ExternalActionsPolicy = ExternalActionsPolicy.ALLOWED_WITH_GATE,
) -> ActionClass:
    """Classify an action name deterministically (fail-closed on unknown).

    When ``external_actions_policy`` is ``FORBIDDEN``, every action in
    ``EXTERNAL_ACTIONS`` is classified ``FORBIDDEN`` (SPEC V2 8.4 / 15.10).
    With ``ALLOWED_WITH_GATE`` they remain ``OWNER_APPROVAL_REQUIRED``.
    """
    if (
        external_actions_policy is ExternalActionsPolicy.FORBIDDEN
        and action in EXTERNAL_ACTIONS
    ):
        return ActionClass.FORBIDDEN
    if action in AUTONOMOUS_ACTIONS:
        return ActionClass.AUTONOMOUS
    if action in OWNER_APPROVAL_ACTIONS:
        return ActionClass.OWNER_APPROVAL_REQUIRED
    if action in EXTERNAL_ACTIONS:
        return ActionClass.OWNER_APPROVAL_REQUIRED
    # FORBIDDEN_ACTIONS and anything unlisted are forbidden.
    return ActionClass.FORBIDDEN


def permission_for(action: str) -> tuple[ArtifactCategory, Permission]:
    """Return the required (category, mode) for an AUTONOMOUS action."""
    return ACTION_PERMISSIONS[action]
