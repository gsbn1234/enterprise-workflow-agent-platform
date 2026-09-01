from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "multi_agent_smoke_test.sqlite3")
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
sys.path.insert(0, str(ROOT))

from app.db import reset_database  # noqa: E402
from app.services.agent import decide_approval_and_resume, get_run_detail  # noqa: E402
from app.services.multi_agent import resume_multi_agent_for_workflow, run_multi_agent  # noqa: E402


def main() -> None:
    reset_database(seed=True)

    result = run_multi_agent(
        "Customer Orbit Retail reports a billing dispute last month and requests a refund of 800 RMB. "
        "Create an auditable ticket, check policy evidence, and prepare a reply to support@orbit.example.",
        requester_user_id="smoke",
        requester_department="Customer Success",
        requester_role="manager",
    )

    assert result["status"] == "waiting_approval", result
    assert result["workflow_run_id"], result

    agent_names = {message["agent_name"] for message in result["messages"]}
    expected_agents = {
        "memory",
        "supervisor",
        "enterprise_rag_research",
        "local_policy_research",
        "rag_research",
        "compliance_risk",
        "operational_risk",
        "risk_approval",
        "tool_execution",
    }
    assert expected_agents.issubset(agent_names), agent_names
    completed_tasks = {task["task_key"] for task in result["tasks"] if task["status"] == "completed"}
    assert {"research.enterprise_rag", "research.local_policy", "research.synthesis", "risk.consensus", "action.execute"}.issubset(completed_tasks), result["tasks"]
    assert len(result["handoffs"]) >= 8, result["handoffs"]

    workflow = get_run_detail(result["workflow_run_id"])
    assert workflow and workflow["status"] == "waiting_approval", workflow
    assert workflow["category"] == "refund", workflow
    assert workflow["needs_approval"] == 1, workflow
    assert any(step["tool_name"] == "query_enterprise_rag" for step in workflow["steps"]), workflow
    assert any(step["tool_name"] == "request_approval" for step in workflow["steps"]), workflow

    approval_id = next(
        message["content"].get("approval_id")
        for message in result["messages"]
        if message["agent_name"] == "tool_execution"
    )
    resumed_workflow = decide_approval_and_resume(approval_id, True, "manager", "multi-agent smoke approval")
    assert resumed_workflow and resumed_workflow["status"] == "completed", resumed_workflow
    result = resume_multi_agent_for_workflow(resumed_workflow)
    assert result and result["status"] == "completed", result
    assert result["critic_score"] >= 80, result
    assert result["memory_item_id"], result
    assert result["critic_report"]["passed"], result
    assert "human_approval" in {message["agent_name"] for message in result["messages"]}, result

    denied_result = run_multi_agent(
        "Customer Acme asks for a refund of 900 RMB. Check policy and prepare the request for approval.",
        requester_user_id="smoke",
        requester_department="Customer Success",
        requester_role="manager",
    )
    assert denied_result["status"] == "waiting_approval", denied_result
    denied_approval_id = next(
        message["content"].get("approval_id")
        for message in denied_result["messages"]
        if message["agent_name"] == "tool_execution"
    )
    denied_workflow = decide_approval_and_resume(
        denied_approval_id,
        False,
        "manager",
        "Policy owner declined the request.",
    )
    assert denied_workflow and denied_workflow["status"] == "cancelled", denied_workflow
    assert not denied_workflow.get("ticket_id"), denied_workflow
    denied_result = resume_multi_agent_for_workflow(denied_workflow)
    assert denied_result and denied_result["status"] == "cancelled", denied_result
    assert denied_result["critic_report"]["passed"], denied_result
    assert denied_result["critic_report"]["approval_denied_safely"], denied_result

    print("multi_agent_smoke_test passed")
    print(f"multi_agent_run={result['id']}")
    print(f"workflow_run={result['workflow_run_id']}")
    print(f"critic_score={result['critic_score']}")
    print("approval_denial_terminal=cancelled")


if __name__ == "__main__":
    main()
