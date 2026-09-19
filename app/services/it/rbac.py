"""Role model and tool-level authorization for the IT service domain.

The important property here is that this module is **fail-closed**:

* an unrecognised role has level ``-1``, which is below every requirement, so it
  can never satisfy a check;
* an unrecognised or missing ``required_role`` falls back to ``it_admin`` rather
  than to the weakest role.

The role check is deliberately independent of ``settings.auth_required``. That
setting decides whether a caller must log in at all; it must never decide
whether a logged-in role is strong enough. Conflating the two is what previously
made the platform's RBAC decorative.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


KNOWN_ROLES: tuple[str, ...] = ("employee", "manager", "it_support", "it_admin", "admin")

# ``manager`` deliberately sits below ``it_support``: a line manager approves
# requests, but must not gain IT diagnostic or asset-visibility powers.
# Approval permissions are additive and belong to Phase 2, not to this ladder.
ROLE_LEVEL: dict[str, int] = {
    "employee": 10,
    "manager": 20,
    "it_support": 30,
    "it_admin": 40,
    "admin": 50,
}

DEFAULT_REQUIRED_ROLE = "it_admin"

IT_SUPPORT_LEVEL = ROLE_LEVEL["it_support"]
IT_ADMIN_LEVEL = ROLE_LEVEL["it_admin"]


@dataclass(frozen=True)
class ToolAuthorization:
    allowed: bool
    required_role: str
    actor_role: str | None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "required_role": self.required_role,
            "actor_role": self.actor_role,
            "reason": self.reason,
        }


def normalize_role(role: Any) -> str | None:
    """Return the canonical role name, or ``None`` when the role is not recognised."""
    normalized = str(role or "").strip().lower()
    return normalized if normalized in KNOWN_ROLES else None


def is_known_role(role: Any) -> bool:
    return normalize_role(role) is not None


def role_level(role: Any) -> int:
    """Numeric level for a role. Unknown roles get ``-1`` so every check denies."""
    normalized = normalize_role(role)
    return ROLE_LEVEL[normalized] if normalized else -1


def role_satisfies(role: Any, required_role: Any) -> bool:
    required = normalize_role(required_role) or DEFAULT_REQUIRED_ROLE
    return role_level(role) >= ROLE_LEVEL[required]


def is_it_support_or_above(auth_context: Any) -> bool:
    return role_level(_context_role(auth_context)) >= IT_SUPPORT_LEVEL


def is_it_admin_or_above(auth_context: Any) -> bool:
    return role_level(_context_role(auth_context)) >= IT_ADMIN_LEVEL


def authorize_tool_call(
    tool_name: str,
    *,
    required_role: Any = None,
    require_auth_context: bool = False,
    auth_context: Any = None,
) -> ToolAuthorization:
    """Decide whether ``auth_context`` may invoke ``tool_name``.

    ``require_auth_context`` marks the IT tools, which never accept an anonymous
    caller. Legacy tools leave it ``False``: telemetry from callers that present
    no identity at all (internal service paths, the stdio bridge) keeps working
    exactly as before, but any caller that *does* present a role is now checked.
    """
    required = normalize_role(required_role) or DEFAULT_REQUIRED_ROLE

    if auth_context is None:
        if require_auth_context:
            return ToolAuthorization(False, required, None, "auth_context_required")
        return ToolAuthorization(True, required, None, None)

    actor_role = normalize_role(_context_role(auth_context))
    if actor_role is None:
        return ToolAuthorization(False, required, None, "unknown_role")
    if ROLE_LEVEL[actor_role] < ROLE_LEVEL[required]:
        return ToolAuthorization(False, required, actor_role, "insufficient_role")
    return ToolAuthorization(True, required, actor_role, None)


def denied_payload(tool_name: str, decision: ToolAuthorization) -> dict[str, Any]:
    """Standard shape for a refused tool call, matching ``call_tool`` error style."""
    return {
        "error": "forbidden",
        "tool_name": tool_name,
        "required_role": decision.required_role,
        "actor_role": decision.actor_role,
        "reason": decision.reason,
    }


def _context_role(auth_context: Any) -> str | None:
    if auth_context is None:
        return None
    role = getattr(auth_context, "role", None) if not isinstance(auth_context, dict) else auth_context.get("role")
    return role
