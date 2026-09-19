"""Mock IT remediation tools: diagnose, flush cache, restart, grant access.

Every function here is registered in ``app.services.tools.registry`` and is
reached through ``call_tool``, never imported and called directly by an agent.
That is what keeps ``Agent -> Tool Registry -> RBAC -> Tool`` true for actions
as well as for lookups: the role gate in ``TOOL_SPECS`` and the ``mcp.tool_*``
audit rows happen on every call, including the ones the graph makes on its own.

Two independent layers, copied deliberately from ``app.services.it.tools``:

* ``required_role`` (in ``TOOL_SPECS``) answers "may this role use this tool at
  all" — all four are ``it_support``.
* the checks *inside* each function answer "may this caller touch **this
  row**" — restarting a production service, or granting read-write, needs
  ``it_admin``. A tool with only the first layer would let any ``it_support``
  user restart production.

What these functions deliberately do **not** write is ``it.action_executed``.
That row belongs to the caller in ``app.services.it.execution``, which is the
only place that knows which risk rule cleared the action and which approval
authorised it; a second row written here would carry neither and would make
"how many times did this run" ambiguous. The tool's own success is already on
the record as ``mcp.tool_call``, written by ``call_tool``. What a tool *does*
own is ``it.action_failed``, because only the tool knows why its preconditions
were not met.

Nothing here touches a real host. Every function returns ``mode: "mock"`` and
``simulated: True``, and no SSH, container runtime, HTTP client or new
dependency is involved. Precondition failures raise :class:`ITOperationError`,
which ``call_tool`` already turns into ``mcp.tool_error`` + ``{"error": ...}``
— the same failure contract every other tool in the platform uses.
"""

from __future__ import annotations

from typing import Any

from app.db import get_connection
from app.services.audit import record_audit
from app.services.it.rbac import (
    ToolAuthorization,
    denied_payload,
    is_it_admin_or_above,
    is_it_support_or_above,
    normalize_role,
)
from app.services.tenancy import effective_tenant_id


# The platform's own IT operations identity, deliberately distinct from the
# person who reported the incident.
#
# The requester's identity drives every requester-scoped read (their profile,
# their assets, the RAG ACL) and is recorded as ``requested_by``. This account
# is the identity that performs privileged operations, and it is used only
# after the deterministic risk gate has cleared the action and, where the gate
# demanded one, a human has approved. ``it.action_executed`` records both
# identities side by side so the audit trail never confuses "who asked" with
# "who ran".
IT_SERVICE_ACCOUNT: dict[str, str] = {
    "user_id": "E003",
    "role": "it_admin",
    "department": "IT",
    "tenant_id": "default",
}

MODE = "mock"

# Distinguishes the two ways an IT action can be refused, so one audit query
# can separate "the gate said no" from "the tool's own resource check said no".
LAYER_RISK_GATE = "risk_gate"
LAYER_TOOL_RESOURCE = "tool_resource"

DIAGNOSTIC_CHECKS: tuple[str, ...] = (
    "reachability",
    "memory_pressure",
    "connection_saturation",
    "recent_errors",
)

ACCESS_LEVELS: tuple[str, ...] = ("read_only", "read_write")

SIMULATED_DOWNTIME_SECONDS = 12

_TOOL_BY_ACTION: dict[str, str] = {
    "diagnostic_read": "diagnose_service",
    "cache_flush": "flush_cache",
    "service_restart": "restart_service",
    "permission_grant": "grant_permission",
}


class ITOperationError(RuntimeError):
    """A mock operation refused to run because its preconditions were not met."""


def diagnose_service(asset_id: str, *, ticket_id: str | None = None, auth_context: Any = None) -> dict[str, Any]:
    """Run read-only diagnostics against one asset.

    Read-only, so it is the one action that is automatic even in production.
    A missing asset is a legitimate diagnostic answer rather than an error:
    "that host is not in the directory" is useful information to return.
    """
    _require_actor(diagnose_service.__name__, auth_context)
    if not is_it_support_or_above(auth_context):
        return denied_payload(
            diagnose_service.__name__,
            ToolAuthorization(False, "it_support", _actor_role(auth_context), "insufficient_role"),
        )

    target = str(asset_id or "").strip()
    if not target:
        raise ITOperationError("asset_id is required.")

    asset = _fetch_asset(target)
    if not asset:
        return {
            "found": False,
            "asset_id": target,
            "reason": "asset_not_found",
            "mode": MODE,
            "simulated": True,
        }

    environment = _environment(asset)
    return {
        "found": True,
        "asset_id": target,
        "asset_type": asset.get("asset_type"),
        "environment": environment,
        "status": asset.get("status"),
        "criticality": asset.get("criticality") or "normal",
        "checks": [
            {"name": name, "result": "degraded" if name == "recent_errors" else "ok"}
            for name in DIAGNOSTIC_CHECKS
        ],
        "summary": f"{target} ({asset.get('asset_type')}, {environment}) responded to read-only diagnostics.",
        "mode": MODE,
        "simulated": True,
    }


def flush_cache(asset_id: str, *, ticket_id: str | None = None, auth_context: Any = None) -> dict[str, Any]:
    """Evict the cache on one asset. Reversible, so automatic outside production."""
    return _side_effect(
        action_type="cache_flush",
        tool_name=flush_cache.__name__,
        target=asset_id,
        ticket_id=ticket_id,
        auth_context=auth_context,
        result={"keys_evicted": 1024, "memory_freed_mb": 128},
    )


def restart_service(asset_id: str, *, ticket_id: str | None = None, auth_context: Any = None) -> dict[str, Any]:
    """Restart the service on one asset.

    Production restarts need ``it_admin`` here as well as the approval the risk
    gate demands. The two are not redundant: the gate decides whether a human
    must sign off, this decides whether *this caller* is allowed to hold the
    button at all.
    """
    return _side_effect(
        action_type="service_restart",
        tool_name=restart_service.__name__,
        target=asset_id,
        ticket_id=ticket_id,
        auth_context=auth_context,
        result={"downtime_seconds": SIMULATED_DOWNTIME_SECONDS, "health": "healthy"},
    )


def grant_permission(
    employee_id: str,
    resource: str,
    access_level: str = "read_only",
    *,
    ticket_id: str | None = None,
    auth_context: Any = None,
) -> dict[str, Any]:
    """Grant one employee access to one resource.

    Read-write grants, and any grant on a resource that maps to a production
    asset, need ``it_admin``. Access is never revoked here: revoking is a
    ``permission_revoke`` action class, and the risk gate denies that class
    outright.
    """
    tool_name = grant_permission.__name__
    _require_actor(tool_name, auth_context)
    if not is_it_support_or_above(auth_context):
        return denied_payload(
            tool_name, ToolAuthorization(False, "it_support", _actor_role(auth_context), "insufficient_role")
        )

    target = str(employee_id or "").strip()
    if not target:
        raise ITOperationError("employee_id is required.")
    resource_name = str(resource or "").strip()
    if not resource_name:
        raise ITOperationError("resource is required.")

    level = str(access_level or "read_only").strip().lower()
    if level not in ACCESS_LEVELS:
        raise ITOperationError(f"access_level must be one of {', '.join(ACCESS_LEVELS)}.")

    if not _fetch_employee(target):
        message = f"employee_not_found: {target}"
        _record_action_failure("permission_grant", target, ticket_id, message, auth_context, target_type="employee")
        raise ITOperationError(message)

    production_resource = _resource_is_production(resource_name)
    if (level == "read_write" or production_resource) and not is_it_admin_or_above(auth_context):
        return _resource_denial(
            tool_name,
            "permission_grant",
            resource_name,
            ticket_id,
            "production" if production_resource else None,
            auth_context,
            reason="read_write_requires_it_admin" if level == "read_write" else "production_requires_it_admin",
        )

    return {
        "granted": True,
        "employee_id": target,
        "resource": resource_name,
        "access_level": level,
        "production_resource": production_resource,
        "ticket_id": ticket_id,
        "mode": MODE,
        "simulated": True,
    }


def _side_effect(
    *,
    action_type: str,
    tool_name: str,
    target: str,
    ticket_id: str | None,
    auth_context: Any,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Shared body of the three asset-scoped actions.

    Factored out so the role gate, the existence/status check and the
    production escalation exist once, rather than three times with three
    chances to drift.
    """
    _require_actor(tool_name, auth_context)
    if not is_it_support_or_above(auth_context):
        return denied_payload(
            tool_name, ToolAuthorization(False, "it_support", _actor_role(auth_context), "insufficient_role")
        )

    asset_id = str(target or "").strip()
    if not asset_id:
        raise ITOperationError("asset_id is required.")

    asset = _fetch_asset(asset_id)
    if not asset:
        message = f"asset_not_found: {asset_id}"
        _record_action_failure(action_type, asset_id, ticket_id, message, auth_context)
        raise ITOperationError(message)
    status = str(asset.get("status") or "").strip().lower()
    if status != "active":
        message = f"asset_not_active: {asset_id} (status={status or 'unknown'})"
        _record_action_failure(action_type, asset_id, ticket_id, message, auth_context)
        raise ITOperationError(message)

    environment = _environment(asset)
    if environment == "production" and not is_it_admin_or_above(auth_context):
        return _resource_denial(
            tool_name, action_type, asset_id, ticket_id, environment, auth_context,
            reason="production_requires_it_admin",
        )

    return {
        "executed": True,
        "asset_id": asset_id,
        "action": action_type,
        "environment": environment,
        **result,
        "mode": MODE,
        "simulated": True,
    }


def _resource_denial(
    tool_name: str,
    action_type: str,
    target: str,
    ticket_id: str | None,
    environment: str | None,
    auth_context: Any,
    *,
    reason: str,
) -> dict[str, Any]:
    """Refuse at the resource layer, recording it as a denial rather than an error.

    ``call_tool`` treats ``error == "forbidden"`` as a denial, so this reaches
    the audit trail as ``mcp.tool_denied`` — the same shape as the role gate's
    refusal, which is what lets one test assert on both layers.
    """
    record_audit(
        "it.action_denied",
        "asset",
        target,
        {
            "layer": LAYER_TOOL_RESOURCE,
            "tool_name": tool_name,
            "action_type": action_type,
            "ticket_id": ticket_id,
            "environment": environment,
            "reason": reason,
            "actor_id": _actor_id(auth_context),
            "actor_role": normalize_role(_actor_role(auth_context)),
        },
        actor=_actor_id(auth_context) or "system",
    )
    return {
        "error": "forbidden",
        "tool_name": tool_name,
        "required_role": "it_admin",
        "actor_role": normalize_role(_actor_role(auth_context)),
        "reason": reason,
    }


def _record_action_failure(
    action_type: str,
    target: str,
    ticket_id: str | None,
    error: str,
    auth_context: Any,
    *,
    target_type: str = "asset",
) -> None:
    record_audit(
        "it.action_failed",
        target_type,
        target,
        {
            "tool_name": _TOOL_BY_ACTION.get(action_type),
            "action_type": action_type,
            "ticket_id": ticket_id,
            "error": error,
            "error_type": ITOperationError.__name__,
            "executed_by": _actor_id(auth_context),
            "mode": MODE,
        },
        actor=_actor_id(auth_context) or "system",
    )


def _fetch_asset(asset_id: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM assets WHERE id = ? AND tenant_id = ? LIMIT 1",
            (asset_id, effective_tenant_id()),
        ).fetchone()
    return dict(row) if row else None


def _fetch_employee(employee_id: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM employees WHERE id = ? AND tenant_id = ? LIMIT 1",
            (employee_id, effective_tenant_id()),
        ).fetchone()
    return dict(row) if row else None


def _resource_is_production(resource: str) -> bool:
    """Whether a permission resource names an asset that lives in production.

    Uses the same uppercase-code convention as ``app.services.it.triage``: the
    triage ``resource`` code is the uppercase form of ``assets.asset_type``.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT environment FROM assets
            WHERE lower(asset_type) = ? AND tenant_id = ?
            ORDER BY id LIMIT 1
            """,
            (resource.strip().lower(), effective_tenant_id()),
        ).fetchone()
    return bool(row) and str(row["environment"]).strip().lower() == "production"


def _environment(asset: dict[str, Any]) -> str:
    return str(asset.get("environment") or "dev").strip().lower()


def _require_actor(tool_name: str, auth_context: Any) -> None:
    if _actor_id(auth_context) is None:
        raise ITOperationError(f"{tool_name} requires an authenticated actor.")


def _actor_id(auth_context: Any) -> str | None:
    if auth_context is None:
        return None
    user_id = getattr(auth_context, "user_id", None)
    if user_id is None and isinstance(auth_context, dict):
        user_id = auth_context.get("user_id")
    actor = str(user_id or "").strip()
    return actor or None


def _actor_role(auth_context: Any) -> str | None:
    if auth_context is None:
        return None
    role = getattr(auth_context, "role", None)
    if role is None and isinstance(auth_context, dict):
        role = auth_context.get("role")
    return role
