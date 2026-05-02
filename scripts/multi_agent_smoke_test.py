from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "multi_agent_smoke_test.sqlite3")
sys.path.insert(0, str(ROOT))

from app.db import reset_database  # noqa: E402
from app.services.agent import get_run_detail  # noqa: E402
from app.services.multi_agent import run_multi_agent  # noqa: E402


def main() -> None:
    reset_database(seed=True)

    result = run_multi_agent(
        "Customer Orbit Retail reports a billing dispute last month and requests a refund of 800 RMB. "
        "Create an auditable ticket, check policy evidence, and prepare a reply to support@orbit.example.",
        requester_user_id="smoke",
        requester_department="Customer Success",
        requester_role="manager",
    )

    assert result["status"] == "completed", result
    assert result["workflow_run_id"], result
    assert result["critic_score"] >= 80, result
    assert result["memory_item_id"], result
    assert result["critic_report"]["passed"], result

    agent_names = {message["agent_name"] for message in result["messages"]}
    expected_agents = {"memory", "supervisor", "rag_research", "risk_approval", "tool_execution", "critic"}
    assert expected_agents.issubset(agent_names), agent_names

    workflow = get_run_detail(result["workflow_run_id"])
    assert workflow and workflow["status"] == "waiting_approval", workflow
    assert workflow["category"] == "refund", workflow
    assert workflow["needs_approval"] == 1, workflow
    assert any(step["tool_name"] == "query_enterprise_rag" for step in workflow["steps"]), workflow
    assert any(step["tool_name"] == "request_approval" for step in workflow["steps"]), workflow

    print("multi_agent_smoke_test passed")
    print(f"multi_agent_run={result['id']}")
    print(f"workflow_run={result['workflow_run_id']}")
    print(f"critic_score={result['critic_score']}")


if __name__ == "__main__":
    main()
