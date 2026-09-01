from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "self_correction_smoke_test.sqlite3")
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
sys.path.insert(0, str(ROOT))

from app.db import reset_database  # noqa: E402
from app.config import settings  # noqa: E402
from app.services.multi_agent.agents import ToolExecutionAgent  # noqa: E402
from app.services.multi_agent import list_agent_checkpoints, run_multi_agent  # noqa: E402
from app.services.tools.ticketing import list_tickets  # noqa: E402


def main() -> None:
    reset_database(seed=True)

    objective = (
        "Create a normal business operations follow-up ticket for weekly report archiving improvements. "
        "No customer response is needed."
    )
    original_run = ToolExecutionAgent.run
    execution_count = 0

    def fail_first_execution(self, *args, **kwargs):
        nonlocal execution_count
        execution_count += 1
        if execution_count != 1:
            return original_run(self, *args, **kwargs)
        original_provider = settings.ticket_provider
        settings.ticket_provider = "unsupported-for-self-correction-test"
        try:
            return original_run(self, *args, **kwargs)
        finally:
            settings.ticket_provider = original_provider

    ToolExecutionAgent.run = fail_first_execution
    try:
        result = run_multi_agent(
            objective,
            requester_user_id="smoke",
            requester_department="QA",
            enable_self_correction=True,
            max_correction_attempts=1,
        )
    finally:
        ToolExecutionAgent.run = original_run

    assert result["status"] == "completed", result
    assert result["correction_count"] == 1, result
    assert result["critic_score"] >= 80, result
    roles = [message["role"] for message in result["messages"]]
    assert "critic_repair" in roles, roles
    assert "corrected_executor" in roles, roles

    checkpoints = list_agent_checkpoints(result["id"])
    checkpoint_nodes = [checkpoint["node_name"] for checkpoint in checkpoints]
    assert "self_correction" in checkpoint_nodes, checkpoint_nodes
    assert checkpoint_nodes.count("tool_execution") == 2, checkpoint_nodes
    assert checkpoint_nodes.count("critic") == 2, checkpoint_nodes

    ticket_count_before_guard_test = len(list_tickets(limit=100))
    guarded = run_multi_agent(
        "Create a separate low-risk audit ticket for the side-effect replay guard test.",
        requester_user_id="smoke",
        requester_department="QA",
        enable_self_correction=True,
        max_correction_attempts=1,
        diagnostic_force_critic_failure=True,
    )
    assert guarded["status"] == "failed", guarded
    assert guarded["correction_count"] == 0, guarded
    correction = next(
        message["content"] for message in guarded["messages"] if message["agent_name"] == "self_correction"
    )
    assert correction["should_retry"] is False, correction
    assert correction["blocked_reason"] == "successful_side_effects_require_human_review", correction
    assert len(list_tickets(limit=100)) == ticket_count_before_guard_test + 1

    print("self_correction_smoke_test passed")
    print(f"multi_agent_run={result['id']}")
    print(f"workflow_run={result['workflow_run_id']}")
    print(f"correction_count={result['correction_count']}")
    print(f"critic_score={result['critic_score']}")
    print("side_effect_replay_guard=passed")


if __name__ == "__main__":
    main()
