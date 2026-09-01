from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "multi_agent_eval.sqlite3"
os.environ.setdefault("AGENT_DB_PATH", str(DEFAULT_DB))
os.environ.setdefault("KNOWLEDGE_RAG_BASE_URL", "")
os.environ.setdefault("AGENT_TOOL_MODE", "mock")
os.environ.setdefault("AGENT_TICKET_PROVIDER", "mock")
os.environ.setdefault("AGENT_EMAIL_PROVIDER", "mock")
sys.path.insert(0, str(ROOT))

from app.db import get_connection, reset_database  # noqa: E402
from app.services.agent import decide_approval_and_resume, get_run_detail  # noqa: E402
from app.services.multi_agent import resume_multi_agent_for_workflow, run_multi_agent  # noqa: E402
from app.utils import json_dumps, new_id, utc_now  # noqa: E402


def load_cases(path: Path) -> list[dict]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            cases.append(json.loads(line))
    return cases


def score_case(case: dict, run: dict, *, pre_approval_workflow_status: str | None = None) -> dict:
    messages = run.get("messages", [])
    agent_names = {message["agent_name"] for message in messages}
    expected_agents = set(case.get("expected_agents", []))
    missing_agents = sorted(expected_agents - agent_names)
    workflow = get_run_detail(run["workflow_run_id"]) if run.get("workflow_run_id") else None
    critic_report = run.get("critic_report", {})
    final_workflow_status = (workflow or {}).get("status") or critic_report.get("workflow_status")
    workflow_status = pre_approval_workflow_status or final_workflow_status
    category = (workflow or {}).get("category")
    needs_approval = bool((workflow or {}).get("needs_approval"))

    agent_coverage = (len(expected_agents) - len(missing_agents)) / len(expected_agents) if expected_agents else 1
    agent_ok = not missing_agents
    critic_ok = float(run.get("critic_score") or 0) >= float(case.get("min_critic_score", 0))
    status_ok = workflow_status == case.get("expected_workflow_status") if case.get("expected_workflow_status") else True
    category_ok = category == case.get("expected_category") if case.get("expected_category") else True
    approval_ok = needs_approval == bool(case.get("expected_approval")) if "expected_approval" in case else True
    passed = run.get("status") == "completed" and agent_ok and critic_ok and status_ok and category_ok and approval_ok

    return {
        "id": case["id"],
        "passed": passed,
        "agent_ok": agent_ok,
        "critic_ok": critic_ok,
        "status_ok": status_ok,
        "category_ok": category_ok,
        "approval_ok": approval_ok,
        "agent_coverage": round(agent_coverage, 4),
        "missing_agents": missing_agents,
        "actual_agents": sorted(agent_names),
        "critic_score": float(run.get("critic_score") or 0),
        "critic_findings": critic_report.get("findings", []),
        "workflow_status": workflow_status,
        "final_workflow_status": final_workflow_status,
        "workflow_category": category,
        "workflow_needs_approval": needs_approval,
        "multi_agent_status": run.get("status"),
        "multi_agent_run_id": run["id"],
        "workflow_run_id": run.get("workflow_run_id"),
        "latency_ms": run.get("latency_ms", 0),
    }


def save_report(results: list[dict], prefix: str) -> dict:
    total = len(results)
    passed = sum(1 for item in results if item["passed"])
    agent_ok = sum(1 for item in results if item["agent_ok"])
    approval_ok = sum(1 for item in results if item["approval_ok"])
    status_ok = sum(1 for item in results if item["status_ok"])
    avg_critic = sum(item["critic_score"] for item in results) / total if total else 0
    avg_agent_coverage = sum(item["agent_coverage"] for item in results) / total if total else 0
    avg_latency = sum(item["latency_ms"] for item in results) / total if total else 0
    summary = {
        "id": new_id("ma_eval"),
        "total_count": total,
        "passed_count": passed,
        "pass_rate": round(passed / total, 4) if total else 0,
        "agent_accuracy": round(agent_ok / total, 4) if total else 0,
        "expected_agent_coverage": round(avg_agent_coverage, 4),
        "workflow_status_accuracy": round(status_ok / total, 4) if total else 0,
        "approval_accuracy": round(approval_ok / total, 4) if total else 0,
        "avg_critic_score": round(avg_critic, 2),
        "avg_latency_ms": round(avg_latency, 2),
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
                summary["agent_accuracy"],
                summary["approval_accuracy"],
                summary["avg_latency_ms"],
                json_dumps({"summary": summary, "results": results}),
                summary["created_at"],
            ),
        )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval-file", default=str(ROOT / "sample_data" / "eval" / "multi_agent_scenarios.jsonl"))
    parser.add_argument("--report-prefix", default="multi_agent_latest")
    parser.add_argument("--keep-db", action="store_true")
    args = parser.parse_args()

    if not args.keep_db:
        reset_database(seed=True)

    results = []
    for case in load_cases(Path(args.eval_file)):
        run = run_multi_agent(
            case["objective"],
            requester_user_id="harness",
            requester_department="QA",
            requester_role="manager",
        )
        workflow = get_run_detail(run["workflow_run_id"]) if run.get("workflow_run_id") else None
        pre_approval_status = (workflow or {}).get("status")
        if run.get("status") == "waiting_approval":
            approval_id = next(
                (
                    message["content"].get("approval_id")
                    for message in run.get("messages", [])
                    if message.get("agent_name") == "tool_execution"
                ),
                None,
            )
            if approval_id:
                resumed_workflow = decide_approval_and_resume(
                    approval_id,
                    True,
                    "harness-manager",
                    "Approved by the multi-agent evaluation harness.",
                )
                resumed_run = resume_multi_agent_for_workflow(resumed_workflow or {})
                if resumed_run:
                    run = resumed_run
        results.append(score_case(case, run, pre_approval_workflow_status=pre_approval_status))

    summary = save_report(results, args.report_prefix)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
