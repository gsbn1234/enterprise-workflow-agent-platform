from __future__ import annotations

import inspect
import time
from typing import Any, Callable

from app.services.audit import record_audit
from app.services.it.actions import diagnose_service, flush_cache, grant_permission, restart_service
from app.services.it.history import search_historical_tickets
from app.services.it.rbac import authorize_tool_call, denied_payload
from app.services.it.tools import find_asset_by_type, get_asset, get_department, get_employee, get_user_assets
from app.services.tools.approvals import create_approval
from app.services.tools.crm import lookup_customer
from app.services.tools.email import draft_email
from app.services.tools.knowledge import query_enterprise_rag, search_knowledge
from app.services.tools.notifications import notify_internal_team
from app.services.tools.ticketing import create_ticket, query_tickets, update_ticket
from app.utils import compact_text


ToolFn = Callable[..., dict]


def request_approval(
    run_id: str,
    action_type: str,
    tool_name: str,
    payload: dict,
    requested_by: str = "mcp",
    tenant_id: str | None = None,
) -> dict:
    return create_approval(
        run_id=run_id,
        action_type=action_type,
        tool_name=tool_name,
        payload=payload,
        requested_by=requested_by,
        tenant_id=tenant_id,
    )


TOOL_REGISTRY: dict[str, ToolFn] = {
    "query_enterprise_rag": query_enterprise_rag,
    "search_knowledge": search_knowledge,
    "search_historical_tickets": search_historical_tickets,
    "lookup_customer": lookup_customer,
    "create_ticket": create_ticket,
    "query_tickets": query_tickets,
    "update_ticket": update_ticket,
    "draft_email": draft_email,
    "notify_internal_team": notify_internal_team,
    "request_approval": request_approval,
    "get_employee": get_employee,
    "get_department": get_department,
    "get_user_assets": get_user_assets,
    "get_asset": get_asset,
    "find_asset_by_type": find_asset_by_type,
    "diagnose_service": diagnose_service,
    "flush_cache": flush_cache,
    "restart_service": restart_service,
    "grant_permission": grant_permission,
}


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "query_enterprise_rag",
        "description": "Query the connected enterprise RAG service with optional user context and return citations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "minLength": 1},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 3},
                "user_id": {"type": ["string", "null"]},
                "user_department": {"type": ["string", "null"]},
                "user_role": {"type": ["string", "null"]},
            },
            "required": ["question"],
        },
        "output_shape": {
            "available": "boolean",
            "answer": "string",
            "citations": "array",
            "retrieved_chunks": "array",
            "metrics": "object",
        },
        "side_effect": False,
        "requires_approval": False,
        "required_role": "employee",
    },
    {
        "name": "search_knowledge",
        "description": "Search local internal policy articles.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3},
            },
            "required": ["query"],
        },
        "output_shape": {"results": "array", "source": "string"},
        "side_effect": False,
        "requires_approval": False,
        "required_role": "employee",
    },
    {
        "name": "search_historical_tickets",
        "description": (
            "Search past IT tickets for how similar incidents were handled. Reference material only: "
            "it is not policy and must never be treated as one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3},
                "ticket_id": {"type": ["string", "null"]},
                "category": {"type": ["string", "null"]},
                "tenant_id": {"type": ["string", "null"]},
            },
            "required": ["query"],
        },
        "output_shape": {
            "results": "array",
            "count": "integer",
            "source": "string",
            "available": "boolean",
        },
        "side_effect": False,
        "requires_approval": False,
        "required_role": "employee",
    },
    {
        "name": "lookup_customer",
        "description": "Find a customer by email, name, or request text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "tenant_id": {"type": ["string", "null"]},
            },
            "required": ["query"],
        },
        "output_shape": {"customer": "object|null", "matched_by": "string|null"},
        "side_effect": False,
        "requires_approval": False,
        "required_role": "employee",
    },
    {
        "name": "create_ticket",
        "description": "Create an internal ticket for a workflow task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "minLength": 1},
                "description": {"type": "string", "minLength": 1},
                "customer_id": {"type": ["string", "null"]},
                "priority": {"type": "string", "default": "normal"},
                "owner_department": {"type": "string", "default": "Customer Success"},
                "workflow_type": {"type": ["string", "null"]},
                "category": {"type": ["string", "null"]},
                "risk_level": {"type": ["string", "null"]},
                "approval_chain": {"type": "array", "items": {"type": "string"}, "default": []},
                "auto_actions": {"type": "array", "items": {"type": "string"}, "default": []},
                "blocked_actions": {"type": "array", "items": {"type": "string"}, "default": []},
                "evidence": {"type": "array", "items": {"type": "object"}, "default": []},
                "agent_run_id": {"type": ["string", "null"]},
                "approval_id": {"type": ["string", "null"]},
                "tenant_id": {"type": ["string", "null"]},
            },
            "required": ["title", "description"],
        },
        "output_shape": {"id": "string", "status": "string", "priority": "string", "due_at": "string", "sla": "object", "external_url": "string|null"},
        "side_effect": True,
        "requires_approval": False,
        "required_role": "employee",
    },
    {
        "name": "query_tickets",
        "description": "Query existing tickets by id, external id, status, priority, department, or free text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_ref": {"type": ["string", "null"]},
                "status": {"type": ["string", "null"], "enum": ["open", "investigating", "waiting_approval", "approved", "rejected", "waiting_customer", "resolved", "closed", None]},
                "priority": {"type": ["string", "null"], "enum": ["low", "normal", "high", "urgent", None]},
                "owner_department": {"type": ["string", "null"]},
                "q": {"type": ["string", "null"]},
                "limit": {"type": "integer", "default": 50},
                "offset": {"type": "integer", "default": 0},
                "tenant_id": {"type": ["string", "null"]},
            },
        },
        "output_shape": {"tickets": "array", "count": "integer", "filters": "object"},
        "side_effect": False,
        "requires_approval": False,
        "required_role": "employee",
    },
    {
        "name": "update_ticket",
        "description": "Update ticket status, owner, or append an operational timeline comment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string", "minLength": 1},
                "status": {"type": ["string", "null"], "enum": ["open", "investigating", "waiting_approval", "approved", "rejected", "waiting_customer", "resolved", "closed", None]},
                "owner_department": {"type": ["string", "null"]},
                "priority": {"type": ["string", "null"], "enum": ["low", "normal", "high", "urgent", None]},
                "comment": {"type": ["string", "null"]},
                "actor": {"type": "string", "default": "agent"},
                "approval_id": {"type": ["string", "null"]},
                "agent_run_id": {"type": ["string", "null"]},
                "evidence": {"type": ["array", "null"], "items": {"type": "object"}},
                "tenant_id": {"type": ["string", "null"]},
            },
            "required": ["ticket_id"],
        },
        "output_shape": {"id": "string", "status": "string", "owner_department": "string", "due_at": "string", "sla": "object"},
        "side_effect": True,
        "requires_approval": False,
        "required_role": "employee",
    },
    {
        "name": "draft_email",
        "description": "Draft an outbound email without sending it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to_address": {"type": "string", "minLength": 3},
                "subject": {"type": "string", "minLength": 1},
                "body": {"type": "string", "minLength": 1},
            },
            "required": ["to_address", "subject", "body"],
        },
        "output_shape": {"to_address": "string", "subject": "string", "body": "string", "status": "string"},
        "side_effect": False,
        "requires_approval": False,
        "required_role": "employee",
    },
    {
        "name": "notify_internal_team",
        "description": "Notify an internal team through the simulated enterprise notification channel and preserve audit evidence.",
        "input_schema": {
            "type": "object",
            "properties": {
                "team": {"type": "string", "minLength": 1},
                "message": {"type": "string", "minLength": 1},
                "severity": {"type": "string", "default": "normal"},
                "ticket_id": {"type": ["string", "null"]},
                "channel": {"type": "string", "default": "internal_queue"},
                "tenant_id": {"type": ["string", "null"]},
            },
            "required": ["team", "message"],
        },
        "output_shape": {"id": "string", "team": "string", "status": "string", "audit_id": "string"},
        "side_effect": True,
        "requires_approval": False,
        "required_role": "employee",
    },
    {
        "name": "request_approval",
        "description": "Create a human approval request for a risky action.",
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "minLength": 1},
                "action_type": {"type": "string", "default": "business_action"},
                "tool_name": {"type": "string", "minLength": 1},
                "payload": {"type": "object"},
                "requested_by": {"type": "string", "default": "mcp"},
                "tenant_id": {"type": ["string", "null"]},
            },
            "required": ["run_id", "action_type", "tool_name", "payload"],
        },
        "output_shape": {"id": "string", "status": "string", "payload": "object"},
        "side_effect": True,
        "requires_approval": False,
        "required_role": "manager",
    },
    {
        "name": "get_employee",
        "description": "Read an IT directory employee profile. Reading another employee's profile requires it_support or above.",
        "input_schema": {
            "type": "object",
            "properties": {"employee_id": {"type": "string", "minLength": 1}},
            "required": ["employee_id"],
        },
        "output_shape": {"found": "boolean", "employee": "object|null", "reason": "string|null"},
        "side_effect": False,
        "requires_approval": False,
        "required_role": "employee",
        "require_auth_context": True,
    },
    {
        "name": "get_department",
        "description": "Read an IT directory department with its head and active headcount.",
        "input_schema": {
            "type": "object",
            "properties": {"department_id": {"type": "string", "minLength": 1}},
            "required": ["department_id"],
        },
        "output_shape": {"found": "boolean", "department": "object|null", "reason": "string|null"},
        "side_effect": False,
        "requires_approval": False,
        "required_role": "employee",
        "require_auth_context": True,
    },
    {
        "name": "get_user_assets",
        "description": "List the assets owned by a user, defaulting to the caller. Listing another user's assets requires it_support or above.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": {"type": ["string", "null"]}},
        },
        "output_shape": {"user_id": "string", "count": "integer", "assets": "array"},
        "side_effect": False,
        "requires_approval": False,
        "required_role": "employee",
        "require_auth_context": True,
    },
    {
        "name": "get_asset",
        "description": "Read an IT asset by id. Production assets are redacted for callers below it_support.",
        "input_schema": {
            "type": "object",
            "properties": {"asset_id": {"type": "string", "minLength": 1}},
            "required": ["asset_id"],
        },
        "output_shape": {"found": "boolean", "asset": "object|null", "reason": "string|null"},
        "side_effect": False,
        "requires_approval": False,
        "required_role": "employee",
        "require_auth_context": True,
    },
    {
        "name": "find_asset_by_type",
        "description": "Resolve an asset type such as redis or vpn to a single asset. Reports ambiguity instead of guessing.",
        "input_schema": {
            "type": "object",
            "properties": {"asset_type": {"type": "string", "minLength": 1}},
            "required": ["asset_type"],
        },
        "output_shape": {"found": "boolean", "asset": "object|null", "reason": "string|null"},
        "side_effect": False,
        "requires_approval": False,
        "required_role": "employee",
        "require_auth_context": True,
    },
    {
        "name": "diagnose_service",
        "description": "Run read-only diagnostics against an IT asset. The only IT action that is automatic in production.",
        "input_schema": {
            "type": "object",
            "properties": {
                "asset_id": {"type": "string", "minLength": 1},
                "ticket_id": {"type": ["string", "null"]},
            },
            "required": ["asset_id"],
        },
        "output_shape": {"found": "boolean", "asset_id": "string", "environment": "string", "checks": "array"},
        "side_effect": False,
        "requires_approval": False,
        "required_role": "it_support",
        "require_auth_context": True,
    },
    {
        "name": "flush_cache",
        "description": "Evict the cache on an IT asset. Reversible; production requires it_admin.",
        "input_schema": {
            "type": "object",
            "properties": {
                "asset_id": {"type": "string", "minLength": 1},
                "ticket_id": {"type": ["string", "null"]},
            },
            "required": ["asset_id"],
        },
        "output_shape": {"executed": "boolean", "asset_id": "string", "keys_evicted": "integer"},
        "side_effect": True,
        "requires_approval": False,
        "required_role": "it_support",
        "require_auth_context": True,
    },
    {
        "name": "restart_service",
        "description": "Restart the service on an IT asset. Always requires human approval; production requires it_admin.",
        "input_schema": {
            "type": "object",
            "properties": {
                "asset_id": {"type": "string", "minLength": 1},
                "ticket_id": {"type": ["string", "null"]},
            },
            "required": ["asset_id"],
        },
        "output_shape": {"executed": "boolean", "asset_id": "string", "downtime_seconds": "integer", "health": "string"},
        "side_effect": True,
        "requires_approval": True,
        "required_role": "it_support",
        "require_auth_context": True,
    },
    {
        "name": "grant_permission",
        "description": "Grant an employee access to a resource. Always requires human approval; read-write or production requires it_admin.",
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string", "minLength": 1},
                "resource": {"type": "string", "minLength": 1},
                "access_level": {"type": "string", "enum": ["read_only", "read_write"], "default": "read_only"},
                "ticket_id": {"type": ["string", "null"]},
            },
            "required": ["employee_id", "resource"],
        },
        "output_shape": {"granted": "boolean", "employee_id": "string", "resource": "string", "access_level": "string"},
        "side_effect": True,
        "requires_approval": True,
        "required_role": "it_support",
        "require_auth_context": True,
    },
]


def list_tool_specs() -> list[dict[str, Any]]:
    return TOOL_SPECS


def get_tool_spec(tool_name: str) -> dict[str, Any] | None:
    return next((spec for spec in TOOL_SPECS if spec["name"] == tool_name), None)


def call_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    actor: str = "mcp",
    source: str = "mcp",
    auth_context: Any = None,
) -> dict:
    """Invoke a registered tool after enforcing the role gate from ``TOOL_SPECS``.

    This is the single choke point for tool authorization: every caller — the
    HTTP ``/api/mcp/call`` route, the intake service, the stdio bridge — goes
    through here, so there is no path that bypasses the check.

    ``auth_context`` is injected into the target function only when that
    function declares the parameter, which keeps every pre-existing tool
    signature untouched.
    """
    started = time.perf_counter()
    tool = TOOL_REGISTRY.get(tool_name)
    if not tool:
        result = {"error": f"Unknown tool: {tool_name}", "available_tools": sorted(TOOL_REGISTRY)}
        record_audit(
            "mcp.tool_error",
            "mcp_tool",
            tool_name,
            {"source": source, "arguments": arguments, "error": result["error"]},
            actor=actor,
        )
        return result

    spec = get_tool_spec(tool_name) or {}
    decision = authorize_tool_call(
        tool_name,
        required_role=spec.get("required_role"),
        require_auth_context=bool(spec.get("require_auth_context")),
        auth_context=auth_context,
    )
    if not decision.allowed:
        result = denied_payload(tool_name, decision)
    else:
        call_arguments = dict(arguments)
        if _accepts_auth_context(tool):
            call_arguments["auth_context"] = auth_context
        try:
            result = tool(**call_arguments)
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            record_audit(
                "mcp.tool_error",
                "mcp_tool",
                tool_name,
                {
                    "source": source,
                    "arguments": arguments,
                    "error": str(exc),
                    "latency_ms": latency_ms,
                },
                actor=actor,
            )
            return {"error": str(exc), "tool_name": tool_name}

    latency_ms = int((time.perf_counter() - started) * 1000)
    # A tool may refuse on its own (resource-level check) after passing the role
    # gate above. Both refusals are recorded as denials, so the audit trail never
    # shows a rejected call as an ordinary success.
    denied = isinstance(result, dict) and result.get("error") == "forbidden"
    detail = {
        "source": source,
        "arguments": arguments,
        "result_summary": _summarize_result(result),
        "latency_ms": latency_ms,
    }
    if denied:
        detail["reason"] = result.get("reason")
        detail["required_role"] = result.get("required_role")
        detail["actor_role"] = result.get("actor_role")
    record_audit("mcp.tool_denied" if denied else "mcp.tool_call", "mcp_tool", tool_name, detail, actor=actor)
    return result


def _accepts_auth_context(tool: ToolFn) -> bool:
    try:
        return "auth_context" in inspect.signature(tool).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins and C callables
        return False


def _summarize_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "keys": sorted(result.keys())[:20],
        "status": result.get("status"),
        "source": result.get("source"),
        "available": result.get("available"),
        "result_count": len(result.get("results", [])) if isinstance(result.get("results"), list) else None,
        "text": compact_text(str(result), 240),
    }
