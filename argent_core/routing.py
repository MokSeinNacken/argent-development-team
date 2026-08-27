"""Canonical model routing for agent runs (SPEC V2 chapter 6 / V2.1 15.9).

Deterministic mapping from role (+ risk class) to the canonical provider/model/
thinking tier.  ``validate_model_choice`` is the pure policy check used by the
dispatch layer (fail-closed); it returns ``False`` on any violation so the
caller can emit ``policy.role_violation`` and reject.

Canonical strings (SPEC V2 15.9):

- lead     : openai / gpt-5.6-sol        (thinking high)   — "Sol High"
- analyst  : deepseek / deepseek-v4-pro  (thinking medium)
- implementer: deepseek / deepseek-v4-pro; deepseek-v4-flash ONLY risk LOW
- qa       : deepseek / deepseek-v4-pro; deepseek-v4-flash ONLY risk LOW
- reviewer : openai / gpt-5.6-sol        (thinking high)   — "Sol High"

Note (SPEC V2 13.7): "Sol Max" does not exist in the docs/configuration; the
highest available tier is ``openai/gpt-5.6-sol`` ("Sol High").  This is the
canonical choice for lead and reviewer and is documented in the README.
"""

from __future__ import annotations

from .models import RiskClass, Role

OPENAI = "openai"
DEEPSEEK = "deepseek"

SOL_HIGH = "gpt-5.6-sol"
PRO = "deepseek-v4-pro"
FLASH = "deepseek-v4-flash"

THINKING_HIGH = "high"
THINKING_MEDIUM = "medium"

# Base canonical (role -> provider, model, thinking tier).
_CANONICAL: dict[Role, tuple[str, str, str]] = {
    Role.LEAD: (OPENAI, SOL_HIGH, THINKING_HIGH),
    Role.ANALYST: (DEEPSEEK, PRO, THINKING_MEDIUM),
    Role.IMPLEMENTER: (DEEPSEEK, PRO, THINKING_MEDIUM),
    Role.QA: (DEEPSEEK, PRO, THINKING_MEDIUM),
    Role.REVIEWER: (OPENAI, SOL_HIGH, THINKING_HIGH),
}


def resolve_model(role: Role, risk_class: RiskClass) -> tuple[str, str, str]:
    """Return the canonical ``(provider, model, thinking_tier)`` for a role.

    For implementer/qa with ``risk_class == LOW`` the cheapest allowed model is
    ``deepseek-v4-flash``; otherwise ``deepseek-v4-pro``.
    """
    provider, model, thinking = _CANONICAL[role]
    if role in (Role.IMPLEMENTER, Role.QA) and risk_class is RiskClass.LOW:
        return (provider, FLASH, thinking)
    return (provider, model, thinking)


def _validate_choice(
    role: Role,
    provider: str,
    model: str,
    thinking_tier: str,
    risk_class: RiskClass,
) -> bool:
    if role in (Role.LEAD, Role.REVIEWER):
        if provider != OPENAI or model != SOL_HIGH:
            return False
        if thinking_tier != THINKING_HIGH:
            return False
        return True
    # analyst / implementer / qa are deepseek-only.
    if provider != DEEPSEEK:
        return False
    if role is Role.ANALYST:
        # analyst never uses flash (SPEC V2 6).
        return model == PRO
    # implementer / qa: pro always; flash only for LOW risk.
    if model == PRO:
        return True
    if model == FLASH:
        return risk_class is RiskClass.LOW
    return False


def validate_model_choice(
    role: Role,
    provider: str,
    model: str,
    thinking_tier: str,
    risk_class: RiskClass,
) -> bool:
    """Return ``True`` iff the model choice is allowed for the role/risk class.

    Pure and deterministic (SPEC V2 6 / V2.1 15.9).  A ``False`` result must be
    turned into a ``policy.role_violation`` event + rejection by the caller.
    """
    if not isinstance(role, Role):
        return False
    if not isinstance(risk_class, RiskClass):
        return False
    return _validate_choice(role, provider, model, thinking_tier, risk_class)
