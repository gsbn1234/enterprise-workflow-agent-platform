from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "self_correction_smoke_test.sqlite3")
sys.path.insert(0, str(ROOT))

from app.db import reset_database  # noqa: E402
from app.services.multi_agent import list_agent_checkpoints, run_multi_agent  # noqa: E402


def main() -> None:
    reset_database(seed=True)

    result = run_multi_agent(
        "Create a normal business operations follow-up ticket for weekly report archiving improvements. No customer response is needed.",
        requester_user_id="smoke",
        requester_department="QA",
        enable_self_correction=True,
        max_correction_attempts=1,
        diagnostic_force_critic_failure=True,
    )

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

    print("self_correction_smoke_test passed")
    print(f"multi_agent_run={result['id']}")
    print(f"workflow_run={result['workflow_run_id']}")
    print(f"correction_count={result['correction_count']}")
    print(f"critic_score={result['critic_score']}")


if __name__ == "__main__":
    main()
