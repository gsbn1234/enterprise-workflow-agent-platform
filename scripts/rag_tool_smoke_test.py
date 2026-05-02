from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "rag_tool_smoke_test.sqlite3")
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
sys.path.insert(0, str(ROOT))

from app.db import reset_database  # noqa: E402
from app.services.agent import run_workflow  # noqa: E402


def main() -> None:
    reset_database(seed=True)
    run = run_workflow(
        "RAG tool smoke test：客户 Orbit Retail 投诉服务中断，要求退费 800 元，请创建工单并准备回复 support@orbit.example",
        requester_user_id="rag-smoke",
        requester_department="QA",
    )
    rag_steps = [step for step in run["steps"] if step.get("tool_name") == "query_enterprise_rag"]
    local_steps = [step for step in run["steps"] if step.get("tool_name") == "search_knowledge"]
    assert len(rag_steps) == 1, run
    assert len(local_steps) == 1, run
    assert rag_steps[0]["tool_output"]["available"] is False, rag_steps[0]
    assert "KNOWLEDGE_RAG_BASE_URL" in rag_steps[0]["tool_output"]["reason"], rag_steps[0]
    assert run["status"] == "waiting_approval", run
    print("rag_tool_smoke_test passed")
    print(f"run_id={run['id']}")


if __name__ == "__main__":
    main()
