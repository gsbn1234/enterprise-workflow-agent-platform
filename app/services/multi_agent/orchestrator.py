from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from app.db import get_connection, row_to_dict, rows_to_dicts
from app.services.audit import record_audit
from app.services.multi_agent.agents import (
    CriticAgent,
    MemoryAgent,
    RagResearchAgent,
    RiskApprovalAgent,
    SupervisorAgent,
    ToolExecutionAgent,
)
from app.services.tenancy import effective_tenant_id
from app.utils import json_dumps, json_loads, new_id, utc_now


def run_multi_agent(
    objective: str,
    *,
    requester_user_id: str | None = None,
    requester_department: str | None = None,
    requester_role: str | None = None,
    tenant_id: str | None = None,
    enable_self_correction: bool = True,
    max_correction_attempts: int = 1,
    replay_of_run_id: str | None = None,
    diagnostic_force_critic_failure: bool = False,
) -> dict:
    from app.services.multi_agent.durable_executor import run_multi_agent_durable

    return run_multi_agent_durable(
        objective,
        requester_user_id=requester_user_id,
        requester_department=requester_department,
        requester_role=requester_role,
        tenant_id=tenant_id,
        enable_self_correction=enable_self_correction,
        max_correction_attempts=max_correction_attempts,
        replay_of_run_id=replay_of_run_id,
        diagnostic_force_critic_failure=diagnostic_force_critic_failure,
    )


def run_multi_agent_legacy(
    objective: str,
    *,
    requester_user_id: str | None = None,
    requester_department: str | None = None,
    requester_role: str | None = None,
    tenant_id: str | None = None,
) -> dict:
    started = time.perf_counter()
    run_id = new_id("ma")
    tenant = effective_tenant_id(tenant_id)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO multi_agent_runs
            (id, objective, requester_user_id, requester_department, tenant_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'running', ?)
            """,
            (run_id, objective, requester_user_id, requester_department, tenant, utc_now()),
        )
    record_audit(
        "multi_agent.start",
        "multi_agent_run",
        run_id,
        {"requester": requester_user_id},
        actor=requester_user_id or "agent",
        tenant_id=tenant,
    )

    supervisor = SupervisorAgent()
    rag = RagResearchAgent()
    risk = RiskApprovalAgent()
    tool = ToolExecutionAgent()
    critic = CriticAgent()
    memory = MemoryAgent()

    try:
        memory_context = _run_agent_message(run_id, memory.name, "context", lambda: memory.retrieve(objective, tenant_id=tenant))
        supervisor_output = _run_agent_message(run_id, supervisor.name, "planner", lambda: supervisor.run(objective))
        research_output = _run_agent_message(
            run_id,
            rag.name,
            "researcher",
            lambda: rag.run(
                objective,
                user_id=requester_user_id,
                user_department=requester_department,
                user_role=requester_role,
            ),
        )
        if "risk_approval" in supervisor_output.get("required_agents", []):
            risk_output = _run_agent_message(run_id, risk.name, "risk", lambda: risk.run(supervisor_output, research_output))
        else:
            risk_output = {
                "risk_level": supervisor_output["plan"]["risk_level"],
                "needs_approval": False,
                "category": supervisor_output["plan"]["category"],
                "warnings": [],
                "decision": "auto_execute",
                "skipped_by_supervisor": True,
            }
        execution_output = _run_agent_message(
            run_id,
            tool.name,
            "executor",
            lambda: tool.run(
                objective,
                requester_user_id=requester_user_id,
                requester_department=requester_department,
                requester_role=requester_role,
                tenant_id=tenant,
            ),
        )
        workflow_run_id = execution_output["workflow_run_id"]
        critic_report = _run_agent_message(
            run_id,
            critic.name,
            "critic",
            lambda: critic.run(workflow_run_id, supervisor_output, risk_output),
        )
        memory_item = _run_agent_message(
            run_id,
            memory.name,
            "memory_writer",
            lambda: memory.write(run_id, objective, critic_report, workflow_run_id, tenant_id=tenant),
        )
        final_summary = _final_summary(supervisor_output, risk_output, execution_output, critic_report, memory_context)
        latency_ms = int((time.perf_counter() - started) * 1000)
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE multi_agent_runs
                SET status = 'completed',
                    workflow_run_id = ?,
                    final_summary = ?,
                    critic_score = ?,
                    critic_report_json = ?,
                    memory_item_id = ?,
                    latency_ms = ?,
                    completed_at = ?
                WHERE id = ?
                """,
                (
                    workflow_run_id,
                    final_summary,
                    float(critic_report.get("score") or 0),
                    json_dumps(critic_report),
                    memory_item["id"],
                    latency_ms,
                    utc_now(),
                    run_id,
                ),
            )
        record_audit(
            "multi_agent.complete",
            "multi_agent_run",
            run_id,
            {"score": critic_report.get("score")},
            actor=requester_user_id or "agent",
            tenant_id=tenant,
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        failure_report = {"score": 0, "passed": False, "findings": [{"severity": "critical", "code": "multi_agent_failed", "message": str(exc)}]}
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE multi_agent_runs
                SET status = 'failed',
                    final_summary = ?,
                    critic_score = 0,
                    critic_report_json = ?,
                    latency_ms = ?,
                    completed_at = ?
                WHERE id = ?
                """,
                (str(exc), json_dumps(failure_report), latency_ms, utc_now(), run_id),
            )
        record_audit(
            "multi_agent.failed",
            "multi_agent_run",
            run_id,
            {"error": str(exc)},
            actor=requester_user_id or "agent",
            tenant_id=tenant,
        )
    return get_multi_agent_run(run_id)


def list_multi_agent_runs(limit: int = 100, tenant_id: str | None = None) -> list[dict]:
    with get_connection() as conn:
        if tenant_id:
            rows = conn.execute(
                """
                SELECT * FROM multi_agent_runs
                WHERE tenant_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM multi_agent_runs
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
    runs = rows_to_dicts(rows)
    for run in runs:
        run["critic_report"] = json_loads(run.pop("critic_report_json"), {})
    return runs


def get_multi_agent_run(run_id: str) -> dict | None:
    with get_connection() as conn:
        run_row = conn.execute("SELECT * FROM multi_agent_runs WHERE id = ?", (run_id,)).fetchone()
        message_rows = conn.execute(
            """
            SELECT * FROM multi_agent_messages
            WHERE run_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (run_id,),
        ).fetchall()
    run = row_to_dict(run_row)
    if not run:
        return None
    run["critic_report"] = json_loads(run.pop("critic_report_json"), {})
    messages = rows_to_dicts(message_rows)
    for message in messages:
        message["content"] = json_loads(message.pop("content_json"), {})
    run["messages"] = messages
    return run


def _run_agent_message(run_id: str, agent_name: str, role: str, fn: Callable[[], dict]) -> dict:
    started = time.perf_counter()
    status = "completed"
    try:
        content = fn()
    except Exception as exc:
        status = "failed"
        content = {"error": str(exc), "error_type": exc.__class__.__name__}
    latency_ms = int((time.perf_counter() - started) * 1000)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO multi_agent_messages
            (id, run_id, agent_name, role, content_json, status, latency_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id("msg"), run_id, agent_name, role, json_dumps(content), status, latency_ms, utc_now()),
        )
    if status == "failed":
        raise RuntimeError(f"{agent_name} failed: {content['error']}")
    return content


def _final_summary(
    supervisor_output: dict,
    risk_output: dict,
    execution_output: dict,
    critic_report: dict,
    memory_context: dict,
) -> str:
    return (
        f"category={supervisor_output['plan']['category']}; "
        f"risk={risk_output['risk_level']}; "
        f"decision={risk_output['decision']}; "
        f"workflow={execution_output['workflow_run_id']}:{execution_output['workflow_status']}; "
        f"critic_score={critic_report['score']}; "
        f"similar_cases={len(memory_context.get('similar_cases', []))}"
    )
