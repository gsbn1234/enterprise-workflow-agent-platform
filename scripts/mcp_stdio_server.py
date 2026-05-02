from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("AGENT_DB_PATH", str(ROOT / "data" / "agent_platform.sqlite3"))
sys.path.insert(0, str(ROOT))

from app.db import init_db  # noqa: E402
from app.services.eval_reports import list_eval_reports  # noqa: E402
from app.services.metrics import metrics_summary  # noqa: E402
from app.services.tools.approvals import list_approvals  # noqa: E402
from app.services.tools.registry import call_tool, list_tool_specs  # noqa: E402


def respond(message_id, result=None, error=None) -> None:
    if message_id is None:
        return
    payload = {"jsonrpc": "2.0", "id": message_id}
    if error:
        payload["error"] = error
    else:
        payload["result"] = result or {}
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def mcp_tool(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": spec["name"],
        "description": _tool_description(spec),
        "inputSchema": spec["input_schema"],
        "annotations": {
            "sideEffect": spec.get("side_effect", False),
            "requiresApproval": spec.get("requires_approval", False),
            "requiredRole": spec.get("required_role", "employee"),
        },
    }


def _tool_description(spec: dict[str, Any]) -> str:
    notes = []
    if spec.get("side_effect"):
        notes.append("side_effect=true")
    if spec.get("requires_approval"):
        notes.append("requires_approval=true")
    role = spec.get("required_role")
    if role:
        notes.append(f"required_role={role}")
    suffix = f" ({', '.join(notes)})" if notes else ""
    return f"{spec['description']}{suffix}"


def list_resources() -> list[dict[str, str]]:
    return [
        {
            "uri": "workflow://tools",
            "name": "Registered Workflow Tools",
            "description": "Tool schemas, side-effect flags, approval metadata, and expected output shapes.",
            "mimeType": "application/json",
        },
        {
            "uri": "workflow://metrics",
            "name": "Workflow Metrics",
            "description": "Runtime counters for runs, jobs, approvals, tickets, emails, and tool calls.",
            "mimeType": "application/json",
        },
        {
            "uri": "workflow://eval/latest",
            "name": "Latest Evaluation Report",
            "description": "Latest trajectory-level evaluation report, when available.",
            "mimeType": "application/json",
        },
        {
            "uri": "workflow://approvals/pending",
            "name": "Pending Approvals",
            "description": "Pending human approvals for risky actions.",
            "mimeType": "application/json",
        },
    ]


def read_resource(uri: str) -> dict[str, Any]:
    if uri == "workflow://tools":
        return {"tools": list_tool_specs()}
    if uri == "workflow://metrics":
        return metrics_summary()
    if uri == "workflow://eval/latest":
        reports = list_eval_reports(limit=1)
        return {"latest": reports[0] if reports else None}
    if uri == "workflow://approvals/pending":
        return {"approvals": list_approvals(status="pending", limit=20)}
    raise KeyError(f"Unknown resource URI: {uri}")


def list_prompts() -> list[dict[str, Any]]:
    return [
        {
            "name": "triage_business_request",
            "description": "Classify a business request and decide whether tools or approval are needed.",
            "arguments": [
                {"name": "request", "description": "Business request text.", "required": True},
            ],
        },
        {
            "name": "review_workflow_trace",
            "description": "Review a workflow trace for tool correctness, approval correctness, and auditability.",
            "arguments": [
                {"name": "run_id", "description": "Workflow run id.", "required": True},
            ],
        },
    ]


def get_prompt(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "triage_business_request":
        request = arguments.get("request", "")
        text = (
            "You are reviewing an enterprise workflow automation request. "
            "Identify the category, risk level, needed tools, and whether human approval is required.\n\n"
            f"Request:\n{request}"
        )
    elif name == "review_workflow_trace":
        run_id = arguments.get("run_id", "")
        text = (
            "Review this workflow run for correctness. Check tool order, side effects, approval gates, "
            "citations, retry behavior, and auditability.\n\n"
            f"Run id: {run_id}"
        )
    else:
        raise KeyError(f"Unknown prompt: {name}")
    return {
        "description": name,
        "messages": [
            {
                "role": "user",
                "content": {"type": "text", "text": text},
            }
        ],
    }


def main() -> None:
    init_db(seed=True)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = message.get("method")
        message_id = message.get("id")
        if method == "initialize":
            respond(
                message_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {},
                        "resources": {},
                        "prompts": {},
                    },
                    "serverInfo": {"name": "enterprise-workflow-agent-tools", "version": "0.2.0"},
                },
            )
        elif method == "tools/list":
            respond(message_id, {"tools": [mcp_tool(spec) for spec in list_tool_specs()]})
        elif method == "tools/call":
            params = message.get("params") or {}
            result = call_tool(params.get("name"), params.get("arguments") or {}, actor="mcp-stdio", source="stdio")
            respond(
                message_id,
                {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                    "isError": "error" in result,
                },
            )
        elif method == "resources/list":
            respond(message_id, {"resources": list_resources()})
        elif method == "resources/read":
            params = message.get("params") or {}
            uri = params.get("uri")
            try:
                resource = read_resource(uri)
            except KeyError as exc:
                respond(message_id, error={"code": -32602, "message": str(exc)})
                continue
            respond(
                message_id,
                {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": json.dumps(resource, ensure_ascii=False, indent=2),
                        }
                    ]
                },
            )
        elif method == "prompts/list":
            respond(message_id, {"prompts": list_prompts()})
        elif method == "prompts/get":
            params = message.get("params") or {}
            try:
                prompt = get_prompt(params.get("name"), params.get("arguments") or {})
            except KeyError as exc:
                respond(message_id, error={"code": -32602, "message": str(exc)})
                continue
            respond(message_id, prompt)
        elif method and method.startswith("notifications/"):
            continue
        else:
            respond(message_id, error={"code": -32601, "message": f"Unknown method: {method}"})


if __name__ == "__main__":
    main()
