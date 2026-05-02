from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "eval_agent.sqlite3"
os.environ.setdefault("AGENT_DB_PATH", str(DEFAULT_DB))
sys.path.insert(0, str(ROOT))

from app.db import get_connection, reset_database  # noqa: E402
from app.services.agent import run_workflow  # noqa: E402
from app.utils import json_dumps, new_id, utc_now  # noqa: E402


def load_cases(path: Path) -> list[dict]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            cases.append(json.loads(line))
    return cases


def score_case(case: dict, run: dict) -> dict:
    tools = [step["tool_name"] for step in run["steps"] if step.get("tool_name")]
    retry_attempts = sum(max(0, int(step.get("attempt_count") or 1) - 1) for step in run["steps"])
    retry_step_count = sum(1 for step in run["steps"] if int(step.get("attempt_count") or 1) > 1)
    failed_step_count = sum(1 for step in run["steps"] if step.get("status") == "failed")
    expected_tools = case.get("expected_tools", [])
    missing_tools = [tool for tool in expected_tools if tool not in tools]
    rag_step = next((step for step in run["steps"] if step.get("tool_name") == "query_enterprise_rag"), None)
    rag_output = (rag_step or {}).get("tool_output", {})
    expected_status = case.get("expected_status")
    category_ok = run.get("category") == case.get("expected_category")
    approval_ok = bool(run.get("needs_approval")) == bool(case.get("expected_approval"))
    tools_ok = not missing_tools
    rag_tool_called = rag_step is not None
    status_ok = run.get("status") == expected_status if expected_status else True
    task_completed = run.get("status") in {"completed", "waiting_approval"}
    passed = category_ok and approval_ok and tools_ok and rag_tool_called and status_ok
    return {
        "id": case["id"],
        "passed": passed,
        "category_ok": category_ok,
        "approval_ok": approval_ok,
        "tools_ok": tools_ok,
        "status_ok": status_ok,
        "task_completed": task_completed,
        "rag_tool_called": rag_tool_called,
        "rag_available": bool(rag_output.get("available")),
        "rag_can_answer": bool(rag_output.get("can_answer")),
        "rag_citation_count": len(rag_output.get("citations", [])),
        "rag_retrieved_chunk_count": len(rag_output.get("retrieved_chunks", [])),
        "retry_attempts": retry_attempts,
        "retry_step_count": retry_step_count,
        "failed_step_count": failed_step_count,
        "missing_tools": missing_tools,
        "actual_category": run.get("category"),
        "actual_status": run.get("status"),
        "actual_tools": tools,
        "latency_ms": run.get("latency_ms", 0),
        "cost_estimate": run.get("cost_estimate", 0),
        "run_id": run["id"],
    }


def save_report(results: list[dict], prefix: str) -> dict:
    total = len(results)
    passed = sum(1 for item in results if item["passed"])
    tool_ok = sum(1 for item in results if item["tools_ok"])
    approval_ok = sum(1 for item in results if item["approval_ok"])
    task_completed = sum(1 for item in results if item["task_completed"])
    rag_tool_called = sum(1 for item in results if item["rag_tool_called"])
    rag_available = sum(1 for item in results if item["rag_available"])
    rag_citation_present = sum(1 for item in results if item["rag_citation_count"] > 0)
    retry_steps = sum(item["retry_step_count"] for item in results)
    retry_attempts = sum(item["retry_attempts"] for item in results)
    failed_steps = sum(item["failed_step_count"] for item in results)
    avg_latency = sum(item["latency_ms"] for item in results) / total if total else 0
    avg_cost = sum(float(item["cost_estimate"] or 0) for item in results) / total if total else 0
    summary = {
        "id": new_id("eval"),
        "total_count": total,
        "passed_count": passed,
        "pass_rate": round(passed / total, 4) if total else 0,
        "tool_accuracy": round(tool_ok / total, 4) if total else 0,
        "approval_accuracy": round(approval_ok / total, 4) if total else 0,
        "task_completion_rate": round(task_completed / total, 4) if total else 0,
        "rag_tool_call_rate": round(rag_tool_called / total, 4) if total else 0,
        "rag_available_rate": round(rag_available / total, 4) if total else 0,
        "rag_citation_present_rate": round(rag_citation_present / total, 4) if total else 0,
        "total_retry_steps": retry_steps,
        "total_retry_attempts": retry_attempts,
        "total_failed_steps": failed_steps,
        "avg_latency_ms": round(avg_latency, 2),
        "avg_cost_estimate": round(avg_cost, 6),
        "created_at": utc_now(),
    }

    report_dir = ROOT / "data" / "eval_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / f"{prefix}_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (report_dir / f"{prefix}_results.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in results) + "\n",
        encoding="utf-8",
    )

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO eval_reports
            (id, total_count, passed_count, pass_rate, tool_accuracy, approval_accuracy,
             avg_latency_ms, report_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                summary["id"],
                summary["total_count"],
                summary["passed_count"],
                summary["pass_rate"],
                summary["tool_accuracy"],
                summary["approval_accuracy"],
                summary["avg_latency_ms"],
                json_dumps({"summary": summary, "results": results}),
                summary["created_at"],
            ),
        )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval-file", default=str(ROOT / "sample_data" / "eval" / "workflow_eval.jsonl"))
    parser.add_argument("--report-prefix", default="latest")
    parser.add_argument("--keep-db", action="store_true")
    args = parser.parse_args()

    if not args.keep_db:
        reset_database(seed=True)

    cases = load_cases(Path(args.eval_file))
    results = []
    for case in cases:
        run = run_workflow(case["objective"], requester_user_id="eval", requester_department="QA")
        results.append(score_case(case, run))

    summary = save_report(results, args.report_prefix)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
