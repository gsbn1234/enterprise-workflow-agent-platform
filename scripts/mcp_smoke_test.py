from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "mcp_smoke_test.sqlite3"


def send(proc: subprocess.Popen, message: dict[str, Any]) -> dict[str, Any] | None:
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    if message.get("id") is None:
        return None
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("MCP server closed stdout.")
    response = json.loads(line)
    if "error" in response:
        raise AssertionError(response)
    return response["result"]


def main() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
    env = os.environ.copy()
    env["AGENT_DB_PATH"] = str(DB_PATH)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "mcp_stdio_server.py")],
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )
    try:
        initialized = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            },
        )
        assert initialized["serverInfo"]["name"] == "enterprise-workflow-agent-tools", initialized
        assert "resources" in initialized["capabilities"], initialized
        send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        tools = send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["tools"]
        tool_names = {tool["name"] for tool in tools}
        assert "query_enterprise_rag" in tool_names, tools
        assert "draft_email" in tool_names, tools
        assert "request_approval" in tool_names, tools

        knowledge = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "search_knowledge", "arguments": {"query": "refund approval", "limit": 3}},
            },
        )
        assert knowledge["isError"] is False, knowledge
        knowledge_payload = json.loads(knowledge["content"][0]["text"])
        assert knowledge_payload["source"] == "local_policy_db", knowledge_payload

        customer = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "lookup_customer", "arguments": {"query": "support@orbit.example"}},
            },
        )
        customer_payload = json.loads(customer["content"][0]["text"])
        assert customer_payload["customer"]["id"] == "cust_orbit", customer_payload

        resources = send(proc, {"jsonrpc": "2.0", "id": 5, "method": "resources/list"})["resources"]
        resource_uris = {item["uri"] for item in resources}
        assert "workflow://metrics" in resource_uris, resources

        metrics = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "resources/read",
                "params": {"uri": "workflow://metrics"},
            },
        )
        metrics_payload = json.loads(metrics["contents"][0]["text"])
        assert "totals" in metrics_payload, metrics_payload

        prompts = send(proc, {"jsonrpc": "2.0", "id": 7, "method": "prompts/list"})["prompts"]
        prompt_names = {prompt["name"] for prompt in prompts}
        assert "triage_business_request" in prompt_names, prompts

        prompt = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "prompts/get",
                "params": {
                    "name": "triage_business_request",
                    "arguments": {"request": "Customer requests refund."},
                },
            },
        )
        assert prompt["messages"][0]["content"]["type"] == "text", prompt

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("mcp_smoke_test passed")


if __name__ == "__main__":
    main()
