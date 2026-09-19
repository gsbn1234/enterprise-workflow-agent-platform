"""Read-only IT directory tools: employee, department and asset lookups.

Every function here is registered in ``app.services.tools.registry`` and is
reached through ``call_tool``, which already enforces the role gate declared in
``TOOL_SPECS``. The checks *inside* these functions are a second, independent
layer: they constrain **which record** an otherwise-permitted caller may see
(your own profile vs. someone else's, a production asset vs. a dev laptop).

Both layers exist because they answer different questions. ``required_role``
answers "may this role use this tool at all"; the resource check answers "may
this role see *this row*". A tool that only had the first would let any
employee read every colleague's profile.

All data is local mock data. Nothing here contacts a real directory service.
"""

from __future__ import annotations

from typing import Any

from app.db import get_connection
from app.services.audit import record_audit
from app.services.it.rbac import (
    ToolAuthorization,
    denied_payload,
    is_it_support_or_above,
    normalize_role,
)
from app.services.tenancy import effective_tenant_id
from app.utils import json_loads


# Fields that must not reach a caller below ``it_support`` on a production asset.
# ``metadata`` is where ``mock_data`` keeps connection details (IP, port,
# credential reference), so it is dropped wholesale rather than filtered.
PRODUCTION_REDACTED_FIELDS: tuple[str, ...] = ("serial", "owner_user_id", "metadata")


def get_employee(employee_id: str, *, auth_context: Any = None) -> dict[str, Any]:
    """Return one employee profile.

    Any authenticated caller may read their own profile; reading someone else's
    requires ``it_support`` or above.
    """
    actor_id = _actor_id(auth_context)
    actor_role = _actor_role(auth_context)
    if actor_id is None:
        return denied_payload("get_employee", ToolAuthorization(False, "employee", None, "auth_context_required"))

    target = str(employee_id or "").strip()
    if not target:
        raise ValueError("employee_id is required.")

    is_self = target == actor_id
    if not is_self and not is_it_support_or_above(auth_context):
        return denied_payload("get_employee", ToolAuthorization(False, "it_support", actor_role, "insufficient_role"))

    row = _fetch_employee(target)
    if not row:
        return {"found": False, "employee_id": target, "reason": "employee_not_found"}

    if not is_self:
        record_audit(
            "it.employee_read",
            "employee",
            target,
            {"actor_id": actor_id, "actor_role": normalize_role(actor_role)},
            actor=actor_id,
        )
    return {"found": True, "employee": _employee_payload(row)}


def get_department(department_id: str, *, auth_context: Any = None) -> dict[str, Any]:
    """Return one department and a headcount of its active employees."""
    actor_id = _actor_id(auth_context)
    if actor_id is None:
        return denied_payload("get_department", ToolAuthorization(False, "employee", None, "auth_context_required"))

    target = str(department_id or "").strip()
    if not target:
        raise ValueError("department_id is required.")

    tenant = effective_tenant_id()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM departments WHERE id = ? AND tenant_id = ? LIMIT 1",
            (target, tenant),
        ).fetchone()
        if not row:
            return {"found": False, "department_id": target, "reason": "department_not_found"}
        headcount = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM employees
            WHERE department_id = ? AND tenant_id = ? AND employment_status = 'active'
            """,
            (target, tenant),
        ).fetchone()["count"]

    department = dict(row)
    head_user_id = department.get("head_user_id")
    head = _fetch_employee(head_user_id) if head_user_id else None
    return {
        "found": True,
        "department": {
            "department_id": department["id"],
            "name": department["name"],
            "cost_center": department.get("cost_center"),
            "head_user_id": head_user_id,
            "head_name": head["display_name"] if head else None,
            "employee_count": int(headcount),
        },
    }


def get_user_assets(user_id: str | None = None, *, auth_context: Any = None) -> dict[str, Any]:
    """List the assets owned by a user, defaulting to the caller.

    Listing someone else's assets requires ``it_support`` or above. Production
    assets are redacted on the same terms as :func:`get_asset`.
    """
    actor_id = _actor_id(auth_context)
    actor_role = _actor_role(auth_context)
    if actor_id is None:
        return denied_payload("get_user_assets", ToolAuthorization(False, "employee", None, "auth_context_required"))

    target = str(user_id or "").strip() or actor_id
    is_self = target == actor_id
    elevated = is_it_support_or_above(auth_context)
    if not is_self and not elevated:
        return denied_payload("get_user_assets", ToolAuthorization(False, "it_support", actor_role, "insufficient_role"))

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM assets
            WHERE owner_user_id = ? AND tenant_id = ?
            ORDER BY id
            """,
            (target, effective_tenant_id()),
        ).fetchall()

    assets = [_asset_payload(row, elevated=elevated) for row in rows]
    if not is_self:
        record_audit(
            "it.assets_read",
            "employee",
            target,
            {"actor_id": actor_id, "actor_role": normalize_role(actor_role), "count": len(assets)},
            actor=actor_id,
        )
    return {"user_id": target, "count": len(assets), "assets": assets}


def get_asset(asset_id: str, *, auth_context: Any = None) -> dict[str, Any]:
    """Return one asset.

    Production assets (``environment == "production"``) are returned with
    ``serial``, ``owner_user_id`` and ``metadata`` stripped unless the caller is
    ``it_support`` or above. The asset still resolves, so a service-desk agent
    can confirm it exists without handing out connection details.
    """
    actor_id = _actor_id(auth_context)
    actor_role = _actor_role(auth_context)
    if actor_id is None:
        return denied_payload("get_asset", ToolAuthorization(False, "employee", None, "auth_context_required"))

    target = str(asset_id or "").strip()
    if not target:
        raise ValueError("asset_id is required.")

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM assets WHERE id = ? AND tenant_id = ? LIMIT 1",
            (target, effective_tenant_id()),
        ).fetchone()
    if not row:
        return {"found": False, "asset_id": target, "reason": "asset_not_found"}

    asset = _asset_payload(row, elevated=is_it_support_or_above(auth_context))
    if asset.get("redacted"):
        record_audit(
            "it.asset_redacted",
            "asset",
            target,
            {
                "actor_id": actor_id,
                "actor_role": normalize_role(actor_role),
                "redacted_fields": list(PRODUCTION_REDACTED_FIELDS),
            },
            actor=actor_id,
        )
    return {"found": True, "asset": asset}


def find_asset_by_type(asset_type: str, *, auth_context: Any = None) -> dict[str, Any]:
    """Resolve an asset type (``redis``, ``vpn``, ...) to a single asset.

    The intake loop uses this to turn a triage ``service`` code into a concrete
    asset. Ambiguity is reported rather than guessed at, and the result carries
    the same production redaction as :func:`get_asset`.
    """
    actor_id = _actor_id(auth_context)
    if actor_id is None:
        return denied_payload(
            "find_asset_by_type", ToolAuthorization(False, "employee", None, "auth_context_required")
        )

    normalized = str(asset_type or "").strip().lower()
    if not normalized:
        return {"found": False, "asset_type": normalized, "reason": "asset_type_required"}

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM assets
            WHERE lower(asset_type) = ? AND tenant_id = ?
            ORDER BY id
            """,
            (normalized, effective_tenant_id()),
        ).fetchall()
    if not rows:
        return {"found": False, "asset_type": normalized, "reason": "asset_not_found"}
    if len(rows) > 1:
        return {
            "found": False,
            "asset_type": normalized,
            "reason": "asset_ambiguous",
            "candidates": [row["id"] for row in rows],
        }
    return {"found": True, "asset": _asset_payload(rows[0], elevated=is_it_support_or_above(auth_context))}


def _fetch_employee(employee_id: str | None) -> dict[str, Any] | None:
    if not employee_id:
        return None
    # LEFT JOIN: ``employees`` is the directory, ``users`` carries the login role.
    # A directory entry without a login is still a valid employee.
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT e.*, u.role AS user_role, d.name AS department_name
            FROM employees e
            LEFT JOIN users u ON u.id = e.id
            LEFT JOIN departments d ON d.id = e.department_id
            WHERE e.id = ? AND e.tenant_id = ?
            LIMIT 1
            """,
            (employee_id, effective_tenant_id()),
        ).fetchone()
    return dict(row) if row else None


def _employee_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "employee_id": row["id"],
        "name": row["display_name"],
        "email": row.get("email"),
        "department_id": row.get("department_id"),
        "department_name": row.get("department_name") or row.get("department_id"),
        "role": row.get("user_role"),
        "title": row.get("title"),
        "manager_id": row.get("manager_id"),
        "location": row.get("location"),
        "employment_status": row.get("employment_status"),
    }


def _asset_payload(row: Any, *, elevated: bool) -> dict[str, Any]:
    asset = dict(row)
    asset["metadata"] = _json_or_empty(asset.pop("metadata_json", None))
    asset["criticality"] = asset.get("criticality") or "normal"
    asset["environment"] = asset.get("environment") or "dev"
    if elevated or asset["environment"] != "production":
        asset["redacted"] = False
        return asset

    for field_name in PRODUCTION_REDACTED_FIELDS:
        asset.pop(field_name, None)
    asset["redacted"] = True
    asset["redacted_fields"] = list(PRODUCTION_REDACTED_FIELDS)
    return asset


def _json_or_empty(value: Any) -> dict[str, Any]:
    parsed = json_loads(value, {}) if value else {}
    return parsed if isinstance(parsed, dict) else {}


def _actor_id(auth_context: Any) -> str | None:
    user_id = getattr(auth_context, "user_id", None)
    if user_id is None and isinstance(auth_context, dict):
        user_id = auth_context.get("user_id")
    actor = str(user_id or "").strip()
    return actor or None


def _actor_role(auth_context: Any) -> str | None:
    role = getattr(auth_context, "role", None)
    if role is None and isinstance(auth_context, dict):
        role = auth_context.get("role")
    return role
