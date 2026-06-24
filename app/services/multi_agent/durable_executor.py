from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from app.config import settings
from app.db import get_connection, row_to_dict, rows_to_dicts
from app.services.audit import record_audit
from app.services.multi_agent.agents import (
    CorrectionAgent,
    CriticAgent,
    MemoryAgent,
    RagResearchAgent,
    RiskApprovalAgent,
    SupervisorAgent,
    ToolExecutionAgent,
)
from app.services.tenancy import effective_tenant_id
from app.utils import json_dumps, json_loads, new_id, utc_now


class MultiAgentState(TypedDict, total=False):
    run_id: str
    thread_id: str
    objective: str
    active_objective: str
    requester_user_id: str | None
    requester_department: str | None
    requester_role: str | None
    tenant_id: str
    max_correction_attempts: int
    correction_attempts: int
    enable_self_correction: bool
    diagnostic_force_critic_failure: bool
    memory_context: dict
    supervisor_output: dict
    research_output: dict
    risk_output: dict
    execution_output: dict
    critic_report: dict
    self_correction_report: dict
    memory_item: dict
    final_summary: str


@dataclass
class DurableRunContext:
    run_id: str
    thread_id: str
    started: float


def run_multi_agent_durable(
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
    started = time.perf_counter()
    run_id = new_id("ma")
    thread_id = f"multi-agent:{run_id}"
    tenant = effective_tenant_id(tenant_id)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO multi_agent_runs
            (id, objective, requester_user_id, requester_department, tenant_id, status,
             executor_type, thread_id, correction_count, replay_of_run_id, created_at)
            VALUES (?, ?, ?, ?, ?, 'running', 'durable_langgraph', ?, 0, ?, ?)
            """,
            (run_id, objective, requester_user_id, requester_department, tenant, thread_id, replay_of_run_id, utc_now()),
        )
    record_audit(
        "multi_agent.start",
        "multi_agent_run",
        run_id,
        {"requester": requester_user_id, "executor": "durable_langgraph", "replay_of_run_id": replay_of_run_id},
        actor=requester_user_id or "agent",
        tenant_id=tenant,
    )

    checkpoint_conn = sqlite3.connect(str(_langgraph_checkpoint_path()), check_same_thread=False)
    checkpointer = SqliteSaver(checkpoint_conn)
    checkpointer.setup()
    graph = _build_graph(DurableRunContext(run_id=run_id, thread_id=thread_id, started=started)).compile(checkpointer=checkpointer)
    initial_state: MultiAgentState = {
        "run_id": run_id,
        "thread_id": thread_id,
        "objective": objective,
        "active_objective": objective,
        "requester_user_id": requester_user_id,
        "requester_department": requester_department,
        "requester_role": requester_role,
        "tenant_id": tenant,
        "max_correction_attempts": max(0, min(max_correction_attempts, 3)),
        "correction_attempts": 0,
        "enable_self_correction": enable_self_correction,
        "diagnostic_force_critic_failure": diagnostic_force_critic_failure,
    }

    try:
        final_state = graph.invoke(initial_state, {"configurable": {"thread_id": thread_id}})
        _complete_run(run_id, started, final_state)
        record_audit(
            "multi_agent.complete",
            "multi_agent_run",
            run_id,
            {"score": final_state.get("critic_report", {}).get("score"), "corrections": final_state.get("correction_attempts", 0)},
            actor=requester_user_id or "agent",
            tenant_id=tenant,
        )
    except Exception as exc:
        _fail_run(run_id, started, exc)
        record_audit(
            "multi_agent.failed",
            "multi_agent_run",
            run_id,
            {"error": str(exc)},
            actor=requester_user_id or "agent",
            tenant_id=tenant,
        )
    finally:
        checkpoint_conn.close()
    return _get_multi_agent_run(run_id)


def _build_graph(context: DurableRunContext):
    builder = StateGraph(MultiAgentState)
    builder.add_node("memory_retrieve", lambda state: _memory_retrieve_node(state, context))
    builder.add_node("supervisor", lambda state: _supervisor_node(state, context))
    builder.add_node("rag_research", lambda state: _rag_research_node(state, context))
    builder.add_node("risk_approval", lambda state: _risk_approval_node(state, context))
    builder.add_node("risk_bypass", lambda state: _risk_bypass_node(state, context))
    builder.add_node("tool_execution", lambda state: _tool_execution_node(state, context))
    builder.add_node("critic", lambda state: _critic_node(state, context))
    builder.add_node("self_correction", lambda state: _self_correction_node(state, context))
    builder.add_node("memory_write", lambda state: _memory_write_node(state, context))
    builder.add_node("finalize", lambda state: _finalize_node(state, context))

    builder.add_edge(START, "memory_retrieve")
    builder.add_edge("memory_retrieve", "supervisor")
    builder.add_edge("supervisor", "rag_research")
    builder.add_conditional_edges(
        "rag_research",
        _route_risk,
        {"risk_approval": "risk_approval", "risk_bypass": "risk_bypass"},
    )
    builder.add_edge("risk_approval", "tool_execution")
    builder.add_edge("risk_bypass", "tool_execution")
    builder.add_edge("tool_execution", "critic")
    builder.add_conditional_edges(
        "critic",
        _route_after_critic,
        {"self_correction": "self_correction", "memory_write": "memory_write"},
    )
    builder.add_edge("self_correction", "tool_execution")
    builder.add_edge("memory_write", "finalize")
    builder.add_edge("finalize", END)
    return builder


def _memory_retrieve_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    memory = MemoryAgent()
    content = _run_agent_message(
        context.run_id,
        memory.name,
        "context",
        lambda: memory.retrieve(state["objective"], tenant_id=state.get("tenant_id")),
    )
    update = {"memory_context": content}
    _record_checkpoint(context, "memory_retrieve", {**state, **update})
    return update


def _supervisor_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    supervisor = SupervisorAgent()
    content = _run_agent_message(context.run_id, supervisor.name, "planner", lambda: supervisor.run(state["objective"]))
    update = {"supervisor_output": content}
    _record_checkpoint(context, "supervisor", {**state, **update})
    return update


def _rag_research_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    rag = RagResearchAgent()
    content = _run_agent_message(
        context.run_id,
        rag.name,
        "researcher",
        lambda: rag.run(
            state["objective"],
            user_id=state.get("requester_user_id"),
            user_department=state.get("requester_department"),
            user_role=state.get("requester_role"),
        ),
    )
    update = {"research_output": content}
    _record_checkpoint(context, "rag_research", {**state, **update})
    return update


def _risk_approval_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    risk = RiskApprovalAgent()
    content = _run_agent_message(
        context.run_id,
        risk.name,
        "risk",
        lambda: risk.run(state["supervisor_output"], state["research_output"]),
    )
    update = {"risk_output": content}
    _record_checkpoint(context, "risk_approval", {**state, **update})
    return update


def _risk_bypass_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    plan = state["supervisor_output"]["plan"]
    content = {
        "risk_level": plan["risk_level"],
        "needs_approval": False,
        "category": plan["category"],
        "warnings": [],
        "decision": "auto_execute",
        "skipped_by_supervisor": True,
    }
    update = {"risk_output": content}
    _record_checkpoint(context, "risk_bypass", {**state, **update})
    return update


def _tool_execution_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    tool = ToolExecutionAgent()
    role = "executor" if int(state.get("correction_attempts", 0)) == 0 else "corrected_executor"
    content = _run_agent_message(
        context.run_id,
        tool.name,
        role,
        lambda: tool.run(
            state.get("active_objective") or state["objective"],
            requester_user_id=state.get("requester_user_id"),
            requester_department=state.get("requester_department"),
            requester_role=state.get("requester_role"),
            tenant_id=state.get("tenant_id"),
        ),
    )
    update = {"execution_output": content}
    _record_checkpoint(context, "tool_execution", {**state, **update})
    return update


def _critic_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    critic = CriticAgent()
    content = _run_agent_message(
        context.run_id,
        critic.name,
        "critic",
        lambda: critic.run(
            state["execution_output"]["workflow_run_id"],
            state["supervisor_output"],
            state.get("risk_output"),
        ),
    )
    if state.get("diagnostic_force_critic_failure") and int(state.get("correction_attempts", 0)) == 0:
        content = {
            **content,
            "score": 50,
            "passed": False,
            "findings": [
                *content.get("findings", []),
                {
                    "severity": "high",
                    "code": "missing_ticket",
                    "message": "Diagnostic mode forced the first critic pass to fail so self-correction can be exercised.",
                },
            ],
            "diagnostic_forced_failure": True,
        }
        _overwrite_latest_agent_message(context.run_id, critic.name, content)
    update = {"critic_report": content}
    _record_checkpoint(context, "critic", {**state, **update})
    return update


def _self_correction_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    correction = CorrectionAgent()
    content = _run_agent_message(
        context.run_id,
        correction.name,
        "critic_repair",
        lambda: correction.run(state["objective"], state["critic_report"], state.get("supervisor_output")),
    )
    correction_attempts = int(state.get("correction_attempts", 0)) + 1
    active_objective = content["corrected_objective"] if content.get("should_retry") else state.get("active_objective", state["objective"])
    update = {
        "self_correction_report": content,
        "active_objective": active_objective,
        "correction_attempts": correction_attempts,
    }
    _record_checkpoint(context, "self_correction", {**state, **update})
    return update


def _memory_write_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    memory = MemoryAgent()
    content = _run_agent_message(
        context.run_id,
        memory.name,
        "memory_writer",
        lambda: memory.write(
            context.run_id,
            state.get("active_objective") or state["objective"],
            state["critic_report"],
            state["execution_output"]["workflow_run_id"],
            tenant_id=state.get("tenant_id"),
        ),
    )
    update = {"memory_item": content}
    _record_checkpoint(context, "memory_write", {**state, **update})
    return update


def _finalize_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    summary = _final_summary(
        state["supervisor_output"],
        state["risk_output"],
        state["execution_output"],
        state["critic_report"],
        state["memory_context"],
        int(state.get("correction_attempts", 0)),
    )
    update = {"final_summary": summary}
    _record_checkpoint(context, "finalize", {**state, **update})
    return update


def _route_risk(state: MultiAgentState) -> str:
    required = state.get("supervisor_output", {}).get("required_agents", [])
    return "risk_approval" if "risk_approval" in required else "risk_bypass"


def _route_after_critic(state: MultiAgentState) -> str:
    if not state.get("enable_self_correction", True):
        return "memory_write"
    critic_report = state.get("critic_report", {})
    if critic_report.get("passed", False):
        return "memory_write"
    if int(state.get("correction_attempts", 0)) >= int(state.get("max_correction_attempts", 1)):
        return "memory_write"
    return "self_correction"


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


def _overwrite_latest_agent_message(run_id: str, agent_name: str, content: dict) -> None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id FROM multi_agent_messages
            WHERE run_id = ? AND agent_name = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (run_id, agent_name),
        ).fetchone()
        if row:
            conn.execute("UPDATE multi_agent_messages SET content_json = ? WHERE id = ?", (json_dumps(content), row["id"]))


def _record_checkpoint(context: DurableRunContext, node_name: str, state: dict, status: str = "completed") -> None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(checkpoint_index), 0) + 1 AS next_index FROM agent_checkpoints WHERE run_id = ?",
            (context.run_id,),
        ).fetchone()
        checkpoint_index = int(row["next_index"])
        conn.execute(
            """
            INSERT INTO agent_checkpoints
            (id, run_id, thread_id, checkpoint_index, node_name, status, state_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("ckpt"),
                context.run_id,
                context.thread_id,
                checkpoint_index,
                node_name,
                status,
                json_dumps(_checkpoint_state(state)),
                utc_now(),
            ),
        )


def _checkpoint_state(state: dict) -> dict:
    keys = [
        "objective",
        "active_objective",
        "requester_user_id",
        "requester_department",
        "requester_role",
        "tenant_id",
        "memory_context",
        "supervisor_output",
        "research_output",
        "risk_output",
        "execution_output",
        "critic_report",
        "self_correction_report",
        "memory_item",
        "final_summary",
        "correction_attempts",
        "max_correction_attempts",
    ]
    return {key: state.get(key) for key in keys if key in state}


def _complete_run(run_id: str, started: float, state: dict) -> None:
    critic_report = state.get("critic_report", {})
    memory_item = state.get("memory_item", {})
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
                correction_count = ?,
                latency_ms = ?,
                completed_at = ?
            WHERE id = ?
            """,
            (
                state.get("execution_output", {}).get("workflow_run_id"),
                state.get("final_summary"),
                float(critic_report.get("score") or 0),
                json_dumps(critic_report),
                memory_item.get("id"),
                int(state.get("correction_attempts", 0)),
                latency_ms,
                utc_now(),
                run_id,
            ),
        )


def _fail_run(run_id: str, started: float, exc: Exception) -> None:
    latency_ms = int((time.perf_counter() - started) * 1000)
    failure_report = {
        "score": 0,
        "passed": False,
        "findings": [{"severity": "critical", "code": "multi_agent_failed", "message": str(exc)}],
    }
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


def _get_multi_agent_run(run_id: str) -> dict:
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
    run = row_to_dict(run_row) or {}
    run["critic_report"] = json_loads(run.pop("critic_report_json", None), {})
    messages = rows_to_dicts(message_rows)
    for message in messages:
        message["content"] = json_loads(message.pop("content_json"), {})
    run["messages"] = messages
    return run


def _final_summary(
    supervisor_output: dict,
    risk_output: dict,
    execution_output: dict,
    critic_report: dict,
    memory_context: dict,
    correction_count: int,
) -> str:
    return (
        f"category={supervisor_output['plan']['category']}; "
        f"risk={risk_output['risk_level']}; "
        f"decision={risk_output['decision']}; "
        f"workflow={execution_output['workflow_run_id']}:{execution_output['workflow_status']}; "
        f"critic_score={critic_report['score']}; "
        f"corrections={correction_count}; "
        f"similar_cases={len(memory_context.get('similar_cases', []))}"
    )


def _langgraph_checkpoint_path():
    path = settings.db_path.parent / "langgraph_checkpoints.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
