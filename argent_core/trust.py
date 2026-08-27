"""Trust boundary (SPEC V1 chapter 4).

Only explicitly authenticated sources are TRUSTED.  Everything else — including
unknown sources — is classified as UNTRUSTED (fail-closed).
"""

from __future__ import annotations

from .models import Role, SourceClass

OWNER_SOURCE = "owner:authenticated"

# Concrete UNTRUSTED source names from the spec.
UNTRUSTED_SOURCES: frozenset[str] = frozenset(
    {
        "email",
        "website",
        "download",
        "document",
        "repo_content",
        "tool_output",
        "network",
    }
)

_ROLE_VALUES = frozenset(r.value for r in Role)


def role_source(role: Role) -> str:
    """Build a deterministic TRUSTED role source string, e.g. ``role:lead``."""
    return f"role:{role.value}"


def classify_source(source: str) -> SourceClass:
    """Classify a source string.

    TRUSTED sources are ``owner:authenticated`` and ``role:<known role>``.
    Everything else (including the concrete UNTRUSTED names and any unknown
    string) is UNTRUSTED.
    """
    if source == OWNER_SOURCE:
        return SourceClass.TRUSTED
    if source.startswith("role:"):
        role_name = source[len("role:"):]
        if role_name in _ROLE_VALUES:
            return SourceClass.TRUSTED
    return SourceClass.UNTRUSTED


def is_trusted(source: str) -> bool:
    return classify_source(source) is SourceClass.TRUSTED


def require_trusted(source: str) -> SourceClass:
    """Return the source class, raising :class:`UntrustedSource` if untrusted."""
    cls = classify_source(source)
    if cls is not SourceClass.TRUSTED:
        from .models import UntrustedSource

        raise UntrustedSource(f"untrusted source rejected: {source!r}")
    return cls


def require_owner(source: str) -> None:
    """Require the source to be the authenticated owner (SPEC V1.1 11.3, R3).

    ``role:<R>`` sources are TRUSTED but never carry owner authority.

    Raises :class:`UntrustedSource` for untrusted sources and
    :class:`OwnerAuthorityRequired` for trusted-but-non-owner sources.
    """
    from .models import OwnerAuthorityRequired, UntrustedSource

    cls = classify_source(source)
    if cls is not SourceClass.TRUSTED:
        raise UntrustedSource(f"untrusted source rejected: {source!r}")
    if source != OWNER_SOURCE:
        raise OwnerAuthorityRequired(
            f"owner authority required, got {source!r}"
        )
