from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "multi_agent_coordination_smoke_test.sqlite3")
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["AGENT_LLM_ENABLED"] = "false"
sys.path.insert(0, str(ROOT))

from app.db import reset_database  # noqa: E402
from app.services.multi_agent import list_agent_checkpoints, run_multi_agent  # noqa: E402
from app.services.multi_agent.agents import (  # noqa: E402
    ComplianceRiskAgent,
    EnterpriseRagResearchAgent,
    LocalPolicyResearchAgent,
    OperationalRiskAgent,
)


def _delayed(original):
    def run(self, *args, **kwargs):
        time.sleep(0.25)
        return original(self, *args, **kwargs)

    return run


def _overlap(first: dict, second: dict) -> bool:
    first_start = datetime.fromisoformat(first["started_at"])
    first_end = datetime.fromisoformat(first["completed_at"])
    second_start = datetime.fromisoformat(second["started_at"])
    second_end = datetime.fromisoformat(second["completed_at"])
    return max(first_start, second_start) < min(first_end, second_end)


def main() -> None:
    reset_database(seed=True)
    patched = [
        (EnterpriseRagResearchAgent, EnterpriseRagResearchAgent.run),
        (LocalPolicyResearchAgent, LocalPolicyResearchAgent.run),
        (ComplianceRiskAgent, ComplianceRiskAgent.run),
        (OperationalRiskAgent, OperationalRiskAgent.run),
    ]
    for agent_class, original in patched:
        agent_class.run = _delayed(original)
    try:
        result = run_multi_agent(
            "Create a low-risk internal operations ticket for weekly archive quality checks.",
            requester_user_id="coordination-smoke",
            requester_department="Operations",
            requester_role="employee",
        )
    finally:
        for agent_class, original in patched:
            agent_class.run = original

    assert result["status"] == "completed", result
    tasks = {task["task_key"]: task for task in result["tasks"]}
    research_pair = (tasks["research.enterprise_rag"], tasks["research.local_policy"])
    risk_pair = (tasks["risk.compliance_vote"], tasks["risk.operational_vote"])
    assert _overlap(*research_pair), research_pair
    assert _overlap(*risk_pair), risk_pair
    assert all(task["duration_ms"] >= 250 for task in [*research_pair, *risk_pair]), tasks

    synthesis = next(
        message["content"] for message in result["messages"] if message["agent_name"] == "rag_research"
    )
    consensus = next(
        message["content"] for message in result["messages"] if message["agent_name"] == "risk_approval"
    )
    assert "evidence" in synthesis and synthesis["handoff_contract"]["reuse_without_retrieval"], synthesis
    assert len(consensus["votes"]) == 2, consensus

    ticket_query = run_multi_agent(
        "查询当前 open 工单",
        requester_user_id="coordination-smoke",
        requester_department="Operations",
        requester_role="manager",
    )
    assert ticket_query["status"] == "completed", ticket_query
    ticket_agents = {message["agent_name"] for message in ticket_query["messages"]}
    assert "enterprise_rag_research" not in ticket_agents, ticket_agents
    assert "local_policy_research" not in ticket_agents, ticket_agents
    assert {"compliance_risk", "operational_risk", "risk_approval"}.issubset(ticket_agents), ticket_agents
    checkpoint_nodes = {item["node_name"] for item in list_agent_checkpoints(ticket_query["id"])}
    assert "research_bypass" in checkpoint_nodes, checkpoint_nodes

    print("multi_agent_coordination_smoke_test passed")
    print(f"multi_agent_run={result['id']}")
    print("research_parallel=true")
    print("risk_parallel=true")
    print("ticket_command_research_bypass=true")


if __name__ == "__main__":
    main()
