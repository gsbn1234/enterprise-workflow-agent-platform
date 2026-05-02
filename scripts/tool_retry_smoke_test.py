from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "tool_retry_smoke_test.sqlite3")
sys.path.insert(0, str(ROOT))

from app.db import get_connection, reset_database  # noqa: E402
from app.services.agent.executor import _run_step, get_run_detail  # noqa: E402
from app.services.agent.retry import RetryPolicy  # noqa: E402
from app.services.agent.state import WorkflowContext  # noqa: E402
from app.utils import new_id, utc_now  # noqa: E402


def main() -> None:
    reset_database(seed=True)
    run_id = new_id("run")
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO workflow_runs
            (id, objective, status, created_at)
            VALUES (?, 'tool retry smoke test', 'running', ?)
            """,
            (run_id, utc_now()),
        )

    context = WorkflowContext(run_id=run_id, objective="tool retry smoke test")
    calls = {"count": 0}

    def flaky_tool() -> dict:
        calls["count"] += 1
        if calls["count"] == 1:
            raise TimeoutError("temporary upstream timeout")
        return {"ok": True, "attempt": calls["count"]}

    output = _run_step(
        context,
        "flaky_tool_step",
        "tool_call",
        "query_enterprise_rag",
        {"question": "retry smoke"},
        flaky_tool,
        "Retry a transient upstream failure and record attempt metadata.",
        retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=0, retryable=True),
    )
    assert output["ok"] is True, output
    run = get_run_detail(run_id)
    step = run["steps"][0]
    assert step["status"] == "completed", step
    assert step["attempt_count"] == 2, step
    assert step["max_attempts"] == 2, step
    assert step["retryable"] == 1, step
    assert len(step["attempts"]) == 2, step
    assert step["attempts"][0]["error_type"] == "timeout", step
    assert step["attempts"][0]["will_retry"] is True, step
    assert step["attempts"][1]["status"] == "completed", step

    print("tool_retry_smoke_test passed")
    print(f"run_id={run_id}")


if __name__ == "__main__":
    main()
