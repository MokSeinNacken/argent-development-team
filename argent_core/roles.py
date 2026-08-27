"""Role permission matrix (SPEC V1 chapter 2).

Five fixed roles and three artifact categories.  "write" grants read+write,
"read" grants read only.
"""

from __future__ import annotations

from .models import ArtifactCategory, Permission, PermissionDenied, Role

# Permission matrix from the spec (chapter 2 table).
#  role       | PRODUCT_CODE | TEST_CODE | OTHER
#  lead       | read         | read      | write
#  analyst    | read         | read      | write
#  implementer| write        | write     | write
#  qa         | read         | write     | write
#  reviewer   | read         | read      | read
PERMISSIONS: dict[Role, dict[ArtifactCategory, Permission]] = {
    Role.LEAD: {
        ArtifactCategory.PRODUCT_CODE: Permission.READ,
        ArtifactCategory.TEST_CODE: Permission.READ,
        ArtifactCategory.OTHER: Permission.WRITE,
    },
    Role.ANALYST: {
        ArtifactCategory.PRODUCT_CODE: Permission.READ,
        ArtifactCategory.TEST_CODE: Permission.READ,
        ArtifactCategory.OTHER: Permission.WRITE,
    },
    Role.IMPLEMENTER: {
        ArtifactCategory.PRODUCT_CODE: Permission.WRITE,
        ArtifactCategory.TEST_CODE: Permission.WRITE,
        ArtifactCategory.OTHER: Permission.WRITE,
    },
    Role.QA: {
        ArtifactCategory.PRODUCT_CODE: Permission.READ,
        ArtifactCategory.TEST_CODE: Permission.WRITE,
        ArtifactCategory.OTHER: Permission.WRITE,
    },
    Role.REVIEWER: {
        ArtifactCategory.PRODUCT_CODE: Permission.READ,
        ArtifactCategory.TEST_CODE: Permission.READ,
        ArtifactCategory.OTHER: Permission.READ,
    },
}

# Canonical order used to determine the "next" role at a handoff.
ROLE_ORDER: tuple[Role, ...] = (
    Role.LEAD,
    Role.ANALYST,
    Role.LEAD,
    Role.IMPLEMENTER,
    Role.QA,
    Role.REVIEWER,
)

# Default "next role" after a role completes.  Deterministic linear pipeline
# through all five roles (Phase-1 simplification).  This is the enforced handoff
# target: ``complete_role`` records it and ``start_role`` must match it (R12).
DEFAULT_NEXT_ROLE: dict[Role, Role] = {
    Role.LEAD: Role.ANALYST,
    Role.ANALYST: Role.IMPLEMENTER,
    Role.IMPLEMENTER: Role.QA,
    Role.QA: Role.REVIEWER,
    Role.REVIEWER: Role.LEAD,
}


def can_read(role: Role, category: ArtifactCategory) -> bool:
    """Every role may read every category (every cell is at least 'read')."""
    return role in PERMISSIONS and category in PERMISSIONS[role]


def can_write(role: Role, category: ArtifactCategory) -> bool:
    """Return whether ``role`` may modify artifacts of ``category``."""
    if role not in PERMISSIONS or category not in PERMISSIONS[role]:
        return False
    return PERMISSIONS[role][category] is Permission.WRITE


def check_permission(
    role: Role, category: ArtifactCategory, mode: Permission
) -> None:
    """Raise :class:`PermissionDenied` if the role lacks the permission."""
    if mode is Permission.WRITE:
        if not can_write(role, category):
            raise PermissionDenied(
                f"role '{role.value}' may not write {category.value}"
            )
    else:
        if not can_read(role, category):
            raise PermissionDenied(
                f"role '{role.value}' may not read {category.value}"
            )
