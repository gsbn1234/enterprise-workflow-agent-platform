from __future__ import annotations

import time
from typing import Any, Callable

from app.services.audit import record_audit
from app.services.tools.approvals import create_approval
from app.services.tools.crm import lookup_customer
from app.services.tools.email import draft_email
from app.services.tools.knowledge import query_enterprise_rag, search_knowledge
from app.services.tools.ticketing import create_ticket, update_ticket
from app.utils import compact_text


ToolFn = Callable[..., dict]


def request_approval(
    run_id: str,
    action_type: str,
    tool_name: str,
    payload: dict,
    requested_by: str = "mcp",
) -> dict:
    return create_approval(
        run_id=run_id,
        action_type=action_type,
        tool_name=tool_name,
        payload=payload,
        requested_by=requested_by,
    )


TOOL_REGISTRY: dict[str, ToolFn] = {
    "query_enterprise_rag": query_enterprise_rag,
    "search_knowledge": search_knowledge,
    "lookup_customer": lookup_customer,
    "create_ticket": create_ticket,
    "update_ticket": update_ticket,
    "draft_email": draft_email,
    "request_approval": request_approval,
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
        "name": "lookup_customer",
        "description": "Find a customer by email, name, or request text.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1}},
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
            },
            "required": ["title", "description"],
        },
        "output_shape": {"id": "string", "status": "string", "priority": "string"},
        "side_effect": True,
        "requires_approval": False,
        "required_role": "employee",
    },
    {
        "name": "update_ticket",
        "description": "Update ticket status or owner department.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string", "minLength": 1},
                "status": {"type": ["string", "null"]},
                "owner_department": {"type": ["string", "null"]},
            },
            "required": ["ticket_id"],
        },
        "output_shape": {"id": "string", "status": "string", "owner_department": "string"},
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
            },
            "required": ["run_id", "action_type", "tool_name", "payload"],
        },
        "output_shape": {"id": "string", "status": "string", "payload": "object"},
        "side_effect": True,
        "requires_approval": False,
        "required_role": "manager",
    },
]


def list_tool_specs() -> list[dict[str, Any]]:
    return TOOL_SPECS


def get_tool_spec(tool_name: str) -> dict[str, Any] | None:
    return next((spec for spec in TOOL_SPECS if spec["name"] == tool_name), None)


def call_tool(tool_name: str, arguments: dict[str, Any], *, actor: str = "mcp", source: str = "mcp") -> dict:
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
    try:
        result = tool(**arguments)
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
    record_audit(
        "mcp.tool_call",
        "mcp_tool",
        tool_name,
        {
            "source": source,
            "arguments": arguments,
            "result_summary": _summarize_result(result),
            "latency_ms": latency_ms,
        },
        actor=actor,
    )
    return result


def _summarize_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "keys": sorted(result.keys())[:20],
        "status": result.get("status"),
        "source": result.get("source"),
        "available": result.get("available"),
        "result_count": len(result.get("results", [])) if isinstance(result.get("results"), list) else None,
        "text": compact_text(str(result), 240),
    }
