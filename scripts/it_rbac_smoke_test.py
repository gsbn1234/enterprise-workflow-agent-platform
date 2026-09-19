from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "it_rbac_smoke_test.sqlite3")
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
# Deliberately OFF. The whole point of this script is that authorization does
# not depend on the "must you log in at all" switch.
os.environ["AGENT_AUTH_REQUIRED"] = "false"
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.db import reset_database  # noqa: E402
from app.services.audit import list_audit_logs  # noqa: E402
from app.services.auth import AuthContext, ensure_demo_users  # noqa: E402
from app.services.it.rbac import (  # noqa: E402
    KNOWN_ROLES,
    ROLE_LEVEL,
    is_known_role,
    role_level,
    role_satisfies,
)
from app.services.tools.registry import TOOL_REGISTRY, call_tool, get_tool_spec  # noqa: E402


def _context(user_id: str, role: str, department: str) -> AuthContext:
    return AuthContext(
        user_id=user_id, display_name=user_id, department=department, role=role, tenant_id="default"
    )


ADMIN = _context("admin", "admin", "Platform")
IT_ADMIN = _context("E003", "it_admin", "IT")
IT_SUPPORT = _context("E001", "it_support", "IT")
MANAGER = _context("E004", "manager", "Engineering")
EMPLOYEE = _context("E002", "employee", "Engineering")
UNKNOWN_ROLE = _context("E002", "superuser", "Engineering")


def main() -> None:
    reset_database(seed=True)
    ensure_demo_users()

    assert settings.auth_required is False, settings.auth_required

    _role_ladder()
    _employee_cannot_read_another_employees_assets()
    _manager_sees_production_assets_only_redacted()
    _anonymous_calls_are_refused()
    _unknown_roles_fail_closed()
    _role_by_tool_matrix()
    _it_action_tools_are_role_gated()
    _legacy_tools_are_unchanged()


def _role_ladder() -> None:
    assert set(KNOWN_ROLES) == {"employee", "manager", "it_support", "it_admin", "admin"}, KNOWN_ROLES
    # A line manager approves requests; they do not gain IT diagnostic powers.
    assert ROLE_LEVEL["manager"] < ROLE_LEVEL["it_support"], ROLE_LEVEL
    assert role_satisfies("it_support", "employee") is True
    assert role_satisfies("employee", "it_support") is False
    assert role_satisfies("manager", "it_admin") is False
    assert role_satisfies("it_admin", "it_admin") is True
    assert role_satisfies("admin", "it_admin") is True
    # Fail-closed by construction: an unknown role sits below every requirement.
    assert is_known_role("superuser") is False
    assert role_level("superuser") == -1
    assert role_satisfies("superuser", "employee") is False
    # An unrecognised requirement falls back to the strictest role, not the weakest.
    assert role_satisfies("it_support", "not_a_real_role") is False


def _employee_cannot_read_another_employees_assets() -> None:
    # Requirement case 6: an employee reaching for someone else's data.
    denied = call_tool("get_user_assets", {"user_id": "E001"}, auth_context=EMPLOYEE)
    assert denied["error"] == "forbidden", denied
    assert denied["reason"] == "insufficient_role", denied
    assert denied["required_role"] == "it_support", denied
    assert denied["actor_role"] == "employee", denied

    # The refusal is audited, so an attempted escalation is visible after the fact.
    events = [entry for entry in list_audit_logs(limit=200) if entry["event_type"] == "mcp.tool_denied"]
    assert events, "expected an mcp.tool_denied audit entry"
    assert any(entry["target_id"] == "get_user_assets" for entry in events), events
    assert any((entry.get("detail") or {}).get("reason") == "insufficient_role" for entry in events), events

    # The same rule blocks reading another employee's profile.
    profile = call_tool("get_employee", {"employee_id": "E001"}, auth_context=EMPLOYEE)
    assert profile["error"] == "forbidden", profile
    assert profile["required_role"] == "it_support", profile

    # Reading your own profile and your own assets is still allowed.
    assert call_tool("get_employee", {"employee_id": "E002"}, auth_context=EMPLOYEE)["found"] is True
    assert call_tool("get_user_assets", {}, auth_context=EMPLOYEE)["count"] == 1


def _manager_sees_production_assets_only_redacted() -> None:
    # Requirement case 8: a manager is not IT staff.
    assert ROLE_LEVEL[MANAGER.role] < ROLE_LEVEL["it_support"]
    assert call_tool("get_user_assets", {"user_id": "E001"}, auth_context=MANAGER)["error"] == "forbidden"
    assert call_tool("get_employee", {"employee_id": "E001"}, auth_context=MANAGER)["error"] == "forbidden"

    # They may confirm a production asset exists, but not its connection details.
    manager_view = call_tool("get_asset", {"asset_id": "REDIS-001"}, auth_context=MANAGER)
    assert manager_view["found"] is True, manager_view
    asset = manager_view["asset"]
    assert asset["redacted"] is True, asset
    assert "serial" not in asset, asset
    assert "owner_user_id" not in asset, asset
    assert "metadata" not in asset, asset
    assert sorted(asset["redacted_fields"]) == ["metadata", "owner_user_id", "serial"], asset
    assert asset["criticality"] == "critical", asset

    # IT support and above see the full record.
    support_view = call_tool("get_asset", {"asset_id": "REDIS-001"}, auth_context=IT_SUPPORT)["asset"]
    assert support_view["redacted"] is False, support_view
    assert support_view["serial"] == "SN-RDS-001", support_view
    assert support_view["metadata"]["port"] == "6379", support_view

    it_admin_view = call_tool("get_asset", {"asset_id": "REDIS-001"}, auth_context=IT_ADMIN)["asset"]
    assert it_admin_view["redacted"] is False, it_admin_view

    # Redaction is audited.
    redacted_events = [entry for entry in list_audit_logs(limit=200) if entry["event_type"] == "it.asset_redacted"]
    assert redacted_events, "expected an it.asset_redacted audit entry"

    # A non-production asset is not redacted, even for an employee.
    dev = call_tool("get_asset", {"asset_id": "SERVER-001"}, auth_context=EMPLOYEE)["asset"]
    assert dev["redacted"] is False, dev


def _anonymous_calls_are_refused() -> None:
    # The IT tools mark ``require_auth_context``: an anonymous caller never gets in,
    # regardless of AGENT_AUTH_REQUIRED being false.
    for tool_name, arguments in (
        ("get_asset", {"asset_id": "REDIS-001"}),
        ("get_employee", {"employee_id": "E001"}),
        ("get_user_assets", {}),
        ("get_department", {"department_id": "IT"}),
        ("find_asset_by_type", {"asset_type": "redis"}),
        # The Phase 2 action tools gate at the same layer. An anonymous caller
        # cannot restart a service simply because no role was presented.
        ("diagnose_service", {"asset_id": "REDIS-001"}),
        ("flush_cache", {"asset_id": "SERVER-001"}),
        ("restart_service", {"asset_id": "REDIS-001"}),
        ("grant_permission", {"employee_id": "E002", "resource": "DATABASE"}),
    ):
        result = call_tool(tool_name, arguments)
        assert result["error"] == "forbidden", (tool_name, result)
        assert result["reason"] == "auth_context_required", (tool_name, result)


def _unknown_roles_fail_closed() -> None:
    # A role the platform does not know gets nothing, rather than silently
    # behaving like the weakest known role.
    result = call_tool("get_asset", {"asset_id": "DEV-001"}, auth_context=UNKNOWN_ROLE)
    assert result["error"] == "forbidden", result
    assert result["reason"] == "unknown_role", result
    assert result["actor_role"] is None, result


def _role_by_tool_matrix() -> None:
    expects = {
        "get_employee": {"admin": True, "it_admin": True, "it_support": True, "manager": True, "employee": True},
        "get_department": {"admin": True, "it_admin": True, "it_support": True, "manager": True, "employee": True},
        "get_user_assets": {"admin": True, "it_admin": True, "it_support": True, "manager": True, "employee": True},
        "get_asset": {"admin": True, "it_admin": True, "it_support": True, "manager": True, "employee": True},
    }
    contexts = {
        "admin": ADMIN,
        "it_admin": IT_ADMIN,
        "it_support": IT_SUPPORT,
        "manager": MANAGER,
        "employee": EMPLOYEE,
    }
    # The tool gate admits every known role; the resource check inside each tool
    # is what separates them, which the cases above exercise directly.
    for tool_name, per_role in expects.items():
        assert tool_name in TOOL_REGISTRY, tool_name
        spec = get_tool_spec(tool_name)
        assert spec["require_auth_context"] is True, spec
        assert spec["required_role"] == "employee", spec
        for role, expected in per_role.items():
            call = {
                "get_employee": lambda ctx: call_tool("get_employee", {"employee_id": ctx.user_id}, auth_context=ctx),
                "get_department": lambda ctx: call_tool("get_department", {"department_id": "IT"}, auth_context=ctx),
                "get_user_assets": lambda ctx: call_tool("get_user_assets", {}, auth_context=ctx),
                "get_asset": lambda ctx: call_tool("get_asset", {"asset_id": "DEV-001"}, auth_context=ctx),
            }[tool_name]
            result = call(contexts[role])
            assert ("error" not in result) is expected, (tool_name, role, result)

    # request_approval stays manager-gated and is unaffected by this change.
    assert get_tool_spec("request_approval")["required_role"] == "manager"
    assert get_tool_spec("request_approval").get("require_auth_context") is not True
    denied = call_tool(
        "request_approval",
        {"run_id": "run_rbac", "action_type": "business_action", "tool_name": "create_ticket", "payload": {}},
        auth_context=EMPLOYEE,
    )
    assert denied["error"] == "forbidden", denied
    assert denied["required_role"] == "manager", denied


def _it_action_tools_are_role_gated() -> None:
    """Requirement case 9: an ordinary employee cannot reach a privileged action tool.

    Two layers, and this exercises both. ``required_role`` in the tool spec is
    the gate — "may this role use this tool at all". The check inside each tool
    is the resource layer — "may this actor touch *this* row". A role that
    clears the first can still be refused by the second, which is the case that
    a single-layer model would get wrong.
    """
    for tool_name in ("diagnose_service", "flush_cache", "restart_service", "grant_permission"):
        assert tool_name in TOOL_REGISTRY, tool_name
        spec = get_tool_spec(tool_name)
        assert spec["require_auth_context"] is True, spec
        assert spec["required_role"] == "it_support", spec

    # --- layer 1: the gate, by role -----------------------------------------
    denied = call_tool("restart_service", {"asset_id": "DEV-001"}, auth_context=EMPLOYEE)
    assert denied["error"] == "forbidden", denied
    assert denied["reason"] == "insufficient_role", denied
    assert denied["required_role"] == "it_support", denied
    assert denied["actor_role"] == "employee", denied

    grant = call_tool(
        "grant_permission", {"employee_id": "E002", "resource": "DATABASE"}, auth_context=EMPLOYEE
    )
    assert grant["error"] == "forbidden", grant
    assert grant["reason"] == "insufficient_role", grant

    # A manager outranks an employee in the business workflow and is still not
    # IT staff, so the IT action tools stay shut.
    assert call_tool("restart_service", {"asset_id": "DEV-001"}, auth_context=MANAGER)["error"] == "forbidden"

    # --- layer 2: the resource check, inside the tool ------------------------
    # ``it_support`` clears the gate on restart_service and is then refused by
    # the resource layer for a production asset: the two answers are distinct,
    # and the reason says which one applied.
    production = call_tool("restart_service", {"asset_id": "REDIS-001"}, auth_context=IT_SUPPORT)
    assert production["error"] == "forbidden", production
    assert production["reason"] == "production_requires_it_admin", production
    assert production["required_role"] == "it_admin", production
    assert production["actor_role"] == "it_support", production

    # The resource-layer refusal names the asset it refused on, in the audit row
    # rather than the return value — and marks which of the two layers spoke.
    resource_denials = [
        entry
        for entry in list_audit_logs(limit=300)
        if entry["event_type"] == "it.action_denied"
        and (entry.get("detail") or {}).get("layer") == "tool_resource"
    ]
    assert resource_denials, "expected an it.action_denied row from the resource layer"
    assert any(
        entry["target_id"] == "REDIS-001"
        and entry["detail"]["tool_name"] == "restart_service"
        and entry["detail"]["environment"] == "production"
        and entry["detail"]["actor_role"] == "it_support"
        for entry in resource_denials
    ), resource_denials

    # A read-only diagnostic against the same production asset has no resource
    # layer to clear — the gate was the only barrier, and support passed it.
    diagnosed = call_tool("diagnose_service", {"asset_id": "REDIS-001"}, auth_context=IT_SUPPORT)
    assert "error" not in diagnosed, diagnosed
    assert diagnosed["found"] is True, diagnosed
    assert diagnosed["simulated"] is True, diagnosed

    # The same restart, on a non-production asset, is allowed by both layers.
    dev_restart = call_tool("restart_service", {"asset_id": "SERVER-001"}, auth_context=IT_SUPPORT)
    assert "error" not in dev_restart, dev_restart
    assert dev_restart["executed"] is True, dev_restart
    assert dev_restart["simulated"] is True, dev_restart

    # A read-write grant needs IT admin even outside production; a read-only one
    # does not. The access level is what separates them, not the resource name.
    read_write = call_tool(
        "grant_permission",
        {"employee_id": "E002", "resource": "SERVER-001", "access_level": "read_write"},
        auth_context=IT_SUPPORT,
    )
    assert read_write["error"] == "forbidden", read_write
    assert read_write["reason"] == "read_write_requires_it_admin", read_write

    read_only = call_tool(
        "grant_permission",
        {"employee_id": "E002", "resource": "SERVER-001", "access_level": "read_only"},
        auth_context=IT_SUPPORT,
    )
    assert "error" not in read_only, read_only
    assert read_only["granted"] is True, read_only

    # IT admin clears both layers on the production asset support could not.
    admin_restart = call_tool("restart_service", {"asset_id": "REDIS-001"}, auth_context=IT_ADMIN)
    assert "error" not in admin_restart, admin_restart
    assert admin_restart["executed"] is True, admin_restart

    # --- every refusal above is on the record -------------------------------
    denials = [entry for entry in list_audit_logs(limit=300) if entry["event_type"] == "mcp.tool_denied"]
    assert any(entry["target_id"] == "restart_service" for entry in denials), denials
    assert any(
        entry["target_id"] == "restart_service"
        and (entry.get("detail") or {}).get("reason") == "production_requires_it_admin"
        for entry in denials
    ), "the resource-layer refusal must name its own reason, not the gate's"
    assert any(
        entry["target_id"] == "grant_permission"
        and (entry.get("detail") or {}).get("reason") == "insufficient_role"
        for entry in denials
    ), denials


def _legacy_tools_are_unchanged() -> None:
    # Pre-existing tools keep their anonymous behaviour: only callers that
    # actually present a role are checked.
    assert call_tool("search_knowledge", {"query": "refund", "limit": 1})["source"] == "local_policy_db"
    assert call_tool("lookup_customer", {"query": "support@orbit.example"})["customer"]["id"] == "cust_orbit"

    print(
        "it_rbac_smoke_test passed",
        f"denials={len([e for e in list_audit_logs(limit=200) if e['event_type'] == 'mcp.tool_denied'])}",
    )


if __name__ == "__main__":
    main()
