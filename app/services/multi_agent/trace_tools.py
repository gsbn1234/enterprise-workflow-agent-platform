from __future__ import annotations

from app.db import get_connection, row_to_dict, rows_to_dicts
from app.services.agent import decide_approval_and_resume, get_run_detail
from app.services.multi_agent.durable_executor import resume_multi_agent_for_workflow
from app.services.multi_agent.orchestrator import get_multi_agent_run, run_multi_agent
from app.services.tenancy import effective_tenant_id
from app.utils import json_dumps, json_loads, new_id, utc_now


TASK_TRACE_ORDER = {
    "memory.retrieve": 0,
    "plan.supervisor": 10,
    "research.enterprise_rag": 20,
    "research.local_policy": 21,
    "research.synthesis": 30,
    "risk.compliance_vote": 40,
    "risk.operational_vote": 41,
    "risk.consensus": 50,
    "action.execute": 60,
    "approval.resume": 70,
    "quality.critic": 80,
    "quality.self_correction": 90,
    "memory.write": 100,
}


def list_agent_checkpoints(run_id: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM agent_checkpoints
            WHERE run_id = ?
            ORDER BY checkpoint_index ASC
            """,
            (run_id,),
        ).fetchall()
    checkpoints = rows_to_dicts(rows)
    for checkpoint in checkpoints:
        checkpoint["state"] = json_loads(checkpoint.pop("state_json"), {})
    return checkpoints


def export_multi_agent_trace(run_id: str, *, include_payloads: bool = False) -> dict | None:
    run = get_multi_agent_run(run_id)
    if not run:
        return None
    workflow = get_run_detail(run["workflow_run_id"]) if run.get("workflow_run_id") else None
    checkpoints = list_agent_checkpoints(run_id)
    messages = run.get("messages", [])
    tasks = sorted(run.get("tasks", []), key=_task_trace_order)
    handoffs = sorted(
        run.get("handoffs", []),
        key=lambda item: (item.get("task_key", ""), item.get("from_agent", ""), item.get("to_agent", "")),
    )
    workflow_steps = (workflow or {}).get("steps", [])
    trace = {
        "run_id": run["id"],
        "tenant_id": run.get("tenant_id"),
        "objective": run["objective"],
        "status": run["status"],
        "executor_type": run.get("executor_type"),
        "thread_id": run.get("thread_id"),
        "correction_count": run.get("correction_count", 0),
        "replay_of_run_id": run.get("replay_of_run_id"),
        "critic_score": run.get("critic_score", 0),
        "critic_passed": bool(run.get("critic_report", {}).get("passed")),
        "critic_findings": run.get("critic_report", {}).get("findings", []),
        "workflow": {
            "run_id": (workflow or {}).get("id"),
            "status": (workflow or {}).get("status"),
            "category": (workflow or {}).get("category"),
            "risk_level": (workflow or {}).get("risk_level"),
            "needs_approval": bool((workflow or {}).get("needs_approval")),
        },
        "agent_sequence": [task["assigned_agent"] for task in tasks],
        "agent_roles": [message["role"] for message in messages],
        "agent_statuses": [message["status"] for message in messages],
        "task_sequence": [f"{task['attempt']}:{task['task_key']}:{task['status']}" for task in tasks],
        "handoff_edges": [
            f"{handoff['from_agent']}->{handoff['to_agent']}:{handoff['task_key']}" for handoff in handoffs
        ],
        "checkpoint_sequence": [checkpoint["node_name"] for checkpoint in checkpoints],
        "tool_sequence": [step.get("tool_name") for step in workflow_steps if step.get("tool_name")],
        "workflow_nodes": [
            {
                "node_name": step.get("node_name"),
                "action_type": step.get("action_type"),
                "tool_name": step.get("tool_name"),
                "status": step.get("status"),
                "attempt_count": step.get("attempt_count"),
                "retryable": bool(step.get("retryable")),
            }
            for step in workflow_steps
        ],
    }
    if include_payloads:
        trace["messages"] = messages
        trace["tasks"] = tasks
        trace["handoffs"] = handoffs
        trace["checkpoints"] = checkpoints
        trace["workflow_steps"] = workflow_steps
    return trace


def save_golden_trace(run_id: str, name: str) -> dict:
    trace = export_multi_agent_trace(run_id, include_payloads=False)
    if not trace:
        raise ValueError(f"Multi-agent run not found: {run_id}")
    tenant = effective_tenant_id(trace.get("tenant_id"))
    now = utc_now()
    with get_connection() as conn:
        existing = conn.execute("SELECT * FROM golden_traces WHERE name = ?", (name,)).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE golden_traces
                SET source_run_id = ?, tenant_id = ?, trace_json = ?, created_at = ?
                WHERE id = ?
                """,
                (run_id, tenant, json_dumps(trace), now, existing["id"]),
            )
            golden_id = existing["id"]
        else:
            golden_id = new_id("golden")
            conn.execute(
                """
                INSERT INTO golden_traces
                (id, name, source_run_id, tenant_id, trace_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (golden_id, name, run_id, tenant, json_dumps(trace), now),
            )
    return get_golden_trace(golden_id)


def list_golden_traces(limit: int = 100, tenant_id: str | None = None) -> list[dict]:
    with get_connection() as conn:
        if tenant_id:
            rows = conn.execute(
                """
                SELECT * FROM golden_traces
                WHERE tenant_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM golden_traces
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
    items = rows_to_dicts(rows)
    for item in items:
        item["trace"] = json_loads(item.pop("trace_json"), {})
    return items


def get_golden_trace(golden_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM golden_traces WHERE id = ?", (golden_id,)).fetchone()
    item = row_to_dict(row)
    if not item:
        return None
    item["trace"] = json_loads(item.pop("trace_json"), {})
    return item


def diff_trace(run_id: str, *, golden_id: str | None = None, baseline_run_id: str | None = None) -> dict:
    current = export_multi_agent_trace(run_id)
    if not current:
        raise ValueError(f"Multi-agent run not found: {run_id}")
    if golden_id:
        golden = get_golden_trace(golden_id)
        if not golden:
            raise ValueError(f"Golden trace not found: {golden_id}")
        baseline = golden["trace"]
        baseline_ref = {"type": "golden", "id": golden_id, "name": golden["name"]}
    elif baseline_run_id:
        baseline = export_multi_agent_trace(baseline_run_id)
        if not baseline:
            raise ValueError(f"Baseline multi-agent run not found: {baseline_run_id}")
        baseline_ref = {"type": "run", "id": baseline_run_id}
    else:
        raise ValueError("Either golden_id or baseline_run_id is required.")
    return _diff_normalized_traces(baseline, current, baseline_ref=baseline_ref)


def replay_multi_agent_run(source_run_id: str) -> dict:
    source = get_multi_agent_run(source_run_id)
    if not source:
        raise ValueError(f"Multi-agent run not found: {source_run_id}")
    replay = run_multi_agent(
        source["objective"],
        requester_user_id=source.get("requester_user_id"),
        requester_department=source.get("requester_department"),
        requester_role=source.get("requester_role"),
        tenant_id=source.get("tenant_id"),
        enable_self_correction=True,
        max_correction_attempts=max(1, int(source.get("correction_count") or 0)),
        replay_of_run_id=source_run_id,
    )
    source_workflow = get_run_detail(source["workflow_run_id"]) if source.get("workflow_run_id") else None
    if replay.get("status") == "waiting_approval" and (source_workflow or {}).get("status") != "waiting_approval":
        approval_id = next(
            (
                message["content"].get("approval_id")
                for message in replay.get("messages", [])
                if message.get("agent_name") == "tool_execution"
            ),
            None,
        )
        if approval_id:
            workflow = decide_approval_and_resume(
                approval_id,
                approved=(source_workflow or {}).get("status") != "refused",
                decided_by="trace-replay",
                reason="Replay the source run's human-approval outcome.",
            )
            resumed = resume_multi_agent_for_workflow(workflow or {})
            if resumed:
                replay = resumed
    diff = diff_trace(replay["id"], baseline_run_id=source_run_id)
    replay_id = new_id("replay")
    status = "passed" if diff["passed"] else "failed"
    now = utc_now()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO trace_replays
            (id, source_run_id, replay_run_id, mode, tenant_id, status, diff_report_json, created_at, completed_at)
            VALUES (?, ?, ?, 'full_replay', ?, ?, ?, ?, ?)
            """,
            (replay_id, source_run_id, replay["id"], effective_tenant_id(source.get("tenant_id")), status, json_dumps(diff), now, now),
        )
    return get_trace_replay(replay_id)


def get_trace_replay(replay_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM trace_replays WHERE id = ?", (replay_id,)).fetchone()
    replay = row_to_dict(row)
    if not replay:
        return None
    replay["diff_report"] = json_loads(replay.pop("diff_report_json"), {})
    return replay


def list_trace_replays(limit: int = 100, tenant_id: str | None = None) -> list[dict]:
    with get_connection() as conn:
        if tenant_id:
            rows = conn.execute(
                """
                SELECT * FROM trace_replays
                WHERE tenant_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM trace_replays
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
    replays = rows_to_dicts(rows)
    for replay in replays:
        replay["diff_report"] = json_loads(replay.pop("diff_report_json"), {})
    return replays


def _diff_normalized_traces(baseline: dict, current: dict, *, baseline_ref: dict) -> dict:
    differences = []
    _compare_value(differences, "workflow.status", baseline.get("workflow", {}).get("status"), current.get("workflow", {}).get("status"))
    _compare_value(differences, "workflow.category", baseline.get("workflow", {}).get("category"), current.get("workflow", {}).get("category"))
    _compare_value(differences, "workflow.needs_approval", baseline.get("workflow", {}).get("needs_approval"), current.get("workflow", {}).get("needs_approval"))
    _compare_value(differences, "critic_passed", baseline.get("critic_passed"), current.get("critic_passed"))
    _compare_sequence(differences, "agent_sequence", baseline.get("agent_sequence", []), current.get("agent_sequence", []))
    _compare_sequence(differences, "task_sequence", baseline.get("task_sequence", []), current.get("task_sequence", []))
    _compare_sequence(differences, "handoff_edges", baseline.get("handoff_edges", []), current.get("handoff_edges", []))
    _compare_sequence(differences, "tool_sequence", baseline.get("tool_sequence", []), current.get("tool_sequence", []))
    _compare_sequence(differences, "checkpoint_sequence", baseline.get("checkpoint_sequence", []), current.get("checkpoint_sequence", []))
    high = [item for item in differences if item["severity"] == "high"]
    return {
        "passed": not high,
        "baseline_ref": baseline_ref,
        "current_ref": {"type": "run", "id": current["run_id"]},
        "summary": {
            "difference_count": len(differences),
            "high_difference_count": len(high),
            "baseline_critic_score": baseline.get("critic_score"),
            "current_critic_score": current.get("critic_score"),
            "baseline_correction_count": baseline.get("correction_count", 0),
            "current_correction_count": current.get("correction_count", 0),
        },
        "differences": differences,
    }


def _task_trace_order(task: dict) -> tuple[int, int, str]:
    task_key = str(task.get("task_key") or "")
    return (
        int(task.get("attempt") or 0),
        TASK_TRACE_ORDER.get(task_key, 999),
        task_key,
    )


def _compare_value(differences: list[dict], field: str, expected, actual) -> None:
    if expected != actual:
        differences.append({"severity": "high", "field": field, "expected": expected, "actual": actual})


def _compare_sequence(differences: list[dict], field: str, expected: list, actual: list) -> None:
    if expected == actual:
        return
    missing = [item for item in expected if item not in actual]
    extra = [item for item in actual if item not in expected]
    severity = "high" if missing else "medium"
    differences.append(
        {
            "severity": severity,
            "field": field,
            "expected": expected,
            "actual": actual,
            "missing": missing,
            "extra": extra,
        }
    )
