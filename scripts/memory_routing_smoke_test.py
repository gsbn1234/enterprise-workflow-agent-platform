from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "memory_routing_smoke_test.sqlite3")
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["AGENT_LLM_ENABLED"] = "false"
sys.path.insert(0, str(ROOT))

from app.db import reset_database  # noqa: E402
from app.services.multi_agent import run_multi_agent  # noqa: E402
from app.services.multi_agent.memory import add_memory  # noqa: E402


def main() -> None:
    reset_database(seed=True)
    objective = "Create a low-risk internal archive quality ticket for the Operations team."
    add_memory(
        "failure_pattern",
        memory_key=objective,
        summary="Prior execution failed after an incomplete operational handoff.",
        detail={
            "critic_report": {
                "score": 20,
                "findings": [
                    {
                        "severity": "critical",
                        "code": "workflow_failed",
                        "message": "Prior execution failed.",
                    }
                ],
            }
        },
        tenant_id="default",
        score=20,
    )

    result = run_multi_agent(
        objective,
        requester_user_id="memory-smoke",
        requester_department="Operations",
        requester_role="employee",
    )
    assert result["status"] == "waiting_approval", result
    supervisor = next(
        message["content"] for message in result["messages"] if message["agent_name"] == "supervisor"
    )
    consensus = next(
        message["content"] for message in result["messages"] if message["agent_name"] == "risk_approval"
    )
    assert supervisor["plan"]["risk_level"] == "low", supervisor
    assert supervisor["memory_influence"]["risk_constraints_applied"] is True, supervisor
    assert "workflow_failed" in supervisor["memory_influence"]["failure_constraints"], supervisor
    assert consensus["risk_level"] == "medium", consensus
    assert consensus["needs_approval"] is True, consensus
    assert any(
        "historical" in warning
        for vote in consensus["votes"]
        for warning in vote.get("warnings", [])
    ), consensus

    print("memory_routing_smoke_test passed")
    print(f"multi_agent_run={result['id']}")
    print("memory_changed_risk_decision=true")


if __name__ == "__main__":
    main()
