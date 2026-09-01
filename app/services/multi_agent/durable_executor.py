from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.config import settings
from app.db import get_connection, row_to_dict, rows_to_dicts
from app.services.audit import record_audit
from app.services.multi_agent.agents import (
    ComplianceRiskAgent,
    CorrectionAgent,
    CriticAgent,
    EnterpriseRagResearchAgent,
    LocalPolicyResearchAgent,
    MemoryAgent,
    OperationalRiskAgent,
    RagResearchAgent,
    RiskApprovalAgent,
    SupervisorAgent,
    ToolExecutionAgent,
)
from app.services.multi_agent.coordination import (
    finish_task,
    list_handoffs,
    list_tasks,
    record_handoff,
    register_task_graph,
    start_task,
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
    enterprise_research: dict
    local_research: dict
    research_output: dict
    compliance_risk_vote: dict
    operational_risk_vote: dict
    risk_output: dict
    execution_output: dict
    critic_report: dict
    self_correction_report: dict
    memory_item: dict
    final_summary: str


_checkpoint_lock = threading.Lock()


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
            (id, objective, requester_user_id, requester_department, requester_role, tenant_id, status,
             executor_type, thread_id, correction_count, replay_of_run_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'running', 'durable_langgraph', ?, 0, ?, ?)
            """,
            (
                run_id,
                objective,
                requester_user_id,
                requester_department,
                requester_role,
                tenant,
                thread_id,
                replay_of_run_id,
                utc_now(),
            ),
        )
    record_audit(
        "multi_agent.start",
        "multi_agent_run",
        run_id,
        {"requester": requester_user_id, "executor": "durable_langgraph", "replay_of_run_id": replay_of_run_id},
        actor=requester_user_id or "agent",
        tenant_id=tenant,
    )

    checkpoint_conn = _open_checkpoint_connection()
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
        if final_state.get("__interrupt__"):
            _pause_run(run_id, started, final_state)
            record_audit(
                "multi_agent.interrupted",
                "multi_agent_run",
                run_id,
                {
                    "thread_id": thread_id,
                    "workflow_run_id": final_state.get("execution_output", {}).get("workflow_run_id"),
                    "approval_id": final_state.get("execution_output", {}).get("approval_id"),
                },
                actor=requester_user_id or "agent",
                tenant_id=tenant,
            )
        else:
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


def resume_multi_agent_for_workflow(workflow: dict) -> dict | None:
    workflow_run_id = str(workflow.get("id") or "")
    if not workflow_run_id:
        return None
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, thread_id, requester_user_id, tenant_id
            FROM multi_agent_runs
            WHERE workflow_run_id = ? AND status = 'waiting_approval'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (workflow_run_id,),
        ).fetchone()
    run = row_to_dict(row)
    if not run or not run.get("thread_id"):
        return None

    started = time.perf_counter()
    context = DurableRunContext(run_id=run["id"], thread_id=run["thread_id"], started=started)
    checkpoint_conn = _open_checkpoint_connection()
    checkpointer = SqliteSaver(checkpoint_conn)
    checkpointer.setup()
    graph = _build_graph(context).compile(checkpointer=checkpointer)
    try:
        final_state = graph.invoke(
            Command(resume={"workflow": workflow}),
            {"configurable": {"thread_id": run["thread_id"]}},
        )
        if final_state.get("__interrupt__"):
            _pause_run(run["id"], started, final_state)
        else:
            _complete_run(run["id"], started, final_state)
            record_audit(
                "multi_agent.resumed",
                "multi_agent_run",
                run["id"],
                {
                    "thread_id": run["thread_id"],
                    "workflow_run_id": workflow_run_id,
                    "workflow_status": workflow.get("status"),
                },
                actor=run.get("requester_user_id") or "agent",
                tenant_id=run.get("tenant_id"),
            )
    except Exception as exc:
        _fail_run(run["id"], started, exc)
        record_audit(
            "multi_agent.resume_failed",
            "multi_agent_run",
            run["id"],
            {"error": str(exc), "workflow_run_id": workflow_run_id},
            actor=run.get("requester_user_id") or "agent",
            tenant_id=run.get("tenant_id"),
        )
    finally:
        checkpoint_conn.close()
    return _get_multi_agent_run(run["id"])


def _build_graph(context: DurableRunContext):
    builder = StateGraph(MultiAgentState)
    builder.add_node("memory_retrieve", lambda state: _memory_retrieve_node(state, context))
    builder.add_node("supervisor", lambda state: _supervisor_node(state, context))
    builder.add_node("research_dispatch", lambda state: _research_dispatch_node(state, context))
    builder.add_node("research_bypass", lambda state: _research_bypass_node(state, context))
    builder.add_node("enterprise_rag_research", lambda state: _enterprise_rag_research_node(state, context))
    builder.add_node("local_policy_research", lambda state: _local_policy_research_node(state, context))
    builder.add_node("evidence_synthesis", lambda state: _evidence_synthesis_node(state, context))
    builder.add_node("risk_dispatch", lambda state: _risk_dispatch_node(state, context))
    builder.add_node("compliance_risk", lambda state: _compliance_risk_node(state, context))
    builder.add_node("operational_risk", lambda state: _operational_risk_node(state, context))
    builder.add_node("risk_consensus", lambda state: _risk_consensus_node(state, context))
    builder.add_node("tool_execution", lambda state: _tool_execution_node(state, context))
    builder.add_node("human_approval", lambda state: _human_approval_node(state, context))
    builder.add_node("critic", lambda state: _critic_node(state, context))
    builder.add_node("self_correction", lambda state: _self_correction_node(state, context))
    builder.add_node("memory_write", lambda state: _memory_write_node(state, context))
    builder.add_node("finalize", lambda state: _finalize_node(state, context))

    builder.add_edge(START, "memory_retrieve")
    builder.add_edge("memory_retrieve", "supervisor")
    builder.add_conditional_edges(
        "supervisor",
        _route_research,
        {"research_parallel": "research_dispatch", "research_bypass": "research_bypass"},
    )
    builder.add_edge("research_dispatch", "enterprise_rag_research")
    builder.add_edge("research_dispatch", "local_policy_research")
    builder.add_edge(["enterprise_rag_research", "local_policy_research"], "evidence_synthesis")
    builder.add_edge("evidence_synthesis", "risk_dispatch")
    builder.add_edge("research_bypass", "risk_dispatch")
    builder.add_edge("risk_dispatch", "compliance_risk")
    builder.add_edge("risk_dispatch", "operational_risk")
    builder.add_edge(["compliance_risk", "operational_risk"], "risk_consensus")
    builder.add_edge("risk_consensus", "tool_execution")
    builder.add_conditional_edges(
        "tool_execution",
        _route_after_execution,
        {"human_approval": "human_approval", "critic": "critic"},
    )
    builder.add_edge("human_approval", "critic")
    builder.add_conditional_edges(
        "critic",
        _route_after_critic,
        {"self_correction": "self_correction", "memory_write": "memory_write"},
    )
    builder.add_conditional_edges(
        "self_correction",
        _route_after_correction,
        {"retry": "supervisor", "stop": "memory_write"},
    )
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
        task_key="memory.retrieve",
        task_attempt=0,
        task_input={"objective": state["objective"], "tenant_id": state.get("tenant_id")},
    )
    update = {"memory_context": content}
    _record_checkpoint(context, "memory_retrieve", {**state, **update})
    return update


def _supervisor_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    supervisor = SupervisorAgent()
    attempt = int(state.get("correction_attempts", 0))
    active_objective = state.get("active_objective") or state["objective"]
    content = _run_agent_message(
        context.run_id,
        supervisor.name,
        "planner",
        lambda: supervisor.run(active_objective, state.get("memory_context")),
        task_key="plan.supervisor",
        task_attempt=attempt,
        task_input={"objective": active_objective, "memory_context": state.get("memory_context", {})},
    )
    register_task_graph(context.run_id, content.get("task_graph", []), attempt=attempt)
    update = {"supervisor_output": content}
    _record_checkpoint(context, "supervisor", {**state, **update})
    return update


def _research_dispatch_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    active_objective = state.get("active_objective") or state["objective"]
    plan = state["supervisor_output"].get("plan", {})
    record_handoff(
        context.run_id,
        "supervisor",
        "enterprise_rag_research",
        "research.enterprise_rag",
        {"objective": active_objective, "plan": plan},
    )
    record_handoff(
        context.run_id,
        "supervisor",
        "local_policy_research",
        "research.local_policy",
        {"objective": active_objective, "plan": plan},
    )
    return {}


def _research_bypass_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    content = {
        "enterprise_rag": {"available": False, "results": [], "citations": [], "skipped": True},
        "local_policy": {"available": False, "results": [], "skipped": True},
        "selected_source": "not_required",
        "evidence": [],
        "evidence_count": 0,
        "citation_count": 0,
        "can_answer": True,
        "warnings": [],
        "conflicts": [],
        "reasoning_mode": "supervisor_research_bypass",
        "producer": "supervisor",
        "bypass_reason": "Existing-ticket commands use scoped ticket data and do not require policy retrieval.",
    }
    update = {"research_output": content}
    _record_checkpoint(context, "research_bypass", {**state, **update})
    return update


def _enterprise_rag_research_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    agent = EnterpriseRagResearchAgent()
    attempt = int(state.get("correction_attempts", 0))
    active_objective = state.get("active_objective") or state["objective"]
    content = _run_agent_message(
        context.run_id,
        agent.name,
        "researcher",
        lambda: agent.run(
            active_objective,
            user_id=state.get("requester_user_id"),
            user_department=state.get("requester_department"),
            user_role=state.get("requester_role"),
        ),
        task_key="research.enterprise_rag",
        task_attempt=attempt,
        task_input={"objective": active_objective, "acl_context": _acl_context(state)},
    )
    record_handoff(
        context.run_id,
        agent.name,
        "rag_research",
        "research.synthesis",
        {"available": content.get("available"), "result_count": len(content.get("results") or [])},
    )
    update = {"enterprise_research": content}
    _record_checkpoint(context, "enterprise_rag_research", {**state, **update})
    return update


def _local_policy_research_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    agent = LocalPolicyResearchAgent()
    attempt = int(state.get("correction_attempts", 0))
    active_objective = state.get("active_objective") or state["objective"]
    content = _run_agent_message(
        context.run_id,
        agent.name,
        "researcher",
        lambda: agent.run(active_objective),
        task_key="research.local_policy",
        task_attempt=attempt,
        task_input={"objective": active_objective},
    )
    record_handoff(
        context.run_id,
        agent.name,
        "rag_research",
        "research.synthesis",
        {"available": content.get("available"), "result_count": len(content.get("results") or [])},
    )
    update = {"local_research": content}
    _record_checkpoint(context, "local_policy_research", {**state, **update})
    return update


def _evidence_synthesis_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    agent = RagResearchAgent()
    attempt = int(state.get("correction_attempts", 0))
    active_objective = state.get("active_objective") or state["objective"]
    content = _run_agent_message(
        context.run_id,
        agent.name,
        "evidence_synthesizer",
        lambda: agent.run(active_objective, state["enterprise_research"], state["local_research"]),
        task_key="research.synthesis",
        task_attempt=attempt,
        task_input={
            "enterprise_result_count": len(state["enterprise_research"].get("results") or []),
            "local_result_count": len(state["local_research"].get("results") or []),
        },
    )
    record_handoff(
        context.run_id,
        agent.name,
        "risk_approval",
        "risk.consensus",
        {"evidence_count": content.get("evidence_count"), "selected_source": content.get("selected_source")},
    )
    update = {"research_output": content}
    _record_checkpoint(context, "evidence_synthesis", {**state, **update})
    return update


def _risk_dispatch_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    source_agent = str(state.get("research_output", {}).get("producer") or "rag_research")
    record_handoff(
        context.run_id,
        source_agent,
        "compliance_risk",
        "risk.compliance_vote",
        {"plan": state["supervisor_output"]["plan"], "evidence_count": state["research_output"].get("evidence_count")},
    )
    record_handoff(
        context.run_id,
        source_agent,
        "operational_risk",
        "risk.operational_vote",
        {"plan": state["supervisor_output"]["plan"], "evidence_count": state["research_output"].get("evidence_count")},
    )
    return {}


def _compliance_risk_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    agent = ComplianceRiskAgent()
    attempt = int(state.get("correction_attempts", 0))
    content = _run_agent_message(
        context.run_id,
        agent.name,
        "risk_voter",
        lambda: agent.run(state["supervisor_output"], state["research_output"]),
        task_key="risk.compliance_vote",
        task_attempt=attempt,
        task_input={"plan": state["supervisor_output"]["plan"], "research": state["research_output"]},
    )
    record_handoff(context.run_id, agent.name, "risk_approval", "risk.consensus", content)
    return {"compliance_risk_vote": content}


def _operational_risk_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    agent = OperationalRiskAgent()
    attempt = int(state.get("correction_attempts", 0))
    content = _run_agent_message(
        context.run_id,
        agent.name,
        "risk_voter",
        lambda: agent.run(state["supervisor_output"], state["research_output"]),
        task_key="risk.operational_vote",
        task_attempt=attempt,
        task_input={"plan": state["supervisor_output"]["plan"], "research": state["research_output"]},
    )
    record_handoff(context.run_id, agent.name, "risk_approval", "risk.consensus", content)
    return {"operational_risk_vote": content}


def _risk_consensus_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    agent = RiskApprovalAgent()
    attempt = int(state.get("correction_attempts", 0))
    content = _run_agent_message(
        context.run_id,
        agent.name,
        "risk_consensus",
        lambda: agent.run(
            state["supervisor_output"],
            state["research_output"],
            state.get("compliance_risk_vote"),
            state.get("operational_risk_vote"),
        ),
        task_key="risk.consensus",
        task_attempt=attempt,
        task_input={
            "compliance_vote": state.get("compliance_risk_vote"),
            "operational_vote": state.get("operational_risk_vote"),
        },
    )
    record_handoff(
        context.run_id,
        agent.name,
        "tool_execution",
        "action.execute",
        {"decision": content.get("decision"), "risk_level": content.get("risk_level"), "votes": content.get("votes")},
    )
    update = {"risk_output": content}
    _record_checkpoint(context, "risk_consensus", {**state, **update})
    return update


def _tool_execution_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    tool = ToolExecutionAgent()
    attempt = int(state.get("correction_attempts", 0))
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
            supervisor_output=state.get("supervisor_output"),
            research_output=state.get("research_output"),
            risk_output=state.get("risk_output"),
        ),
        task_key="action.execute",
        task_attempt=attempt,
        task_input={
            "plan": state.get("supervisor_output", {}).get("plan"),
            "research_handoff": state.get("research_output"),
            "risk_consensus": state.get("risk_output"),
        },
    )
    next_agent = "human_approval" if content.get("workflow_status") == "waiting_approval" else "critic"
    record_handoff(
        context.run_id,
        tool.name,
        next_agent,
        "quality.critic",
        {
            "workflow_run_id": content.get("workflow_run_id"),
            "workflow_status": content.get("workflow_status"),
            "approval_id": content.get("approval_id"),
        },
    )
    update = {"execution_output": content}
    _record_checkpoint(context, "tool_execution", {**state, **update})
    return update


def _human_approval_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    execution = state["execution_output"]
    resume_payload = interrupt(
        {
            "type": "human_approval",
            "multi_agent_run_id": context.run_id,
            "thread_id": context.thread_id,
            "workflow_run_id": execution.get("workflow_run_id"),
            "approval_id": execution.get("approval_id"),
            "risk_decision": state.get("risk_output", {}),
        }
    )
    workflow = (resume_payload or {}).get("workflow")
    if not isinstance(workflow, dict):
        raise RuntimeError("A resumed multi-agent run requires the completed workflow payload.")
    tool = ToolExecutionAgent()
    content = _run_agent_message(
        context.run_id,
        "human_approval",
        "approval_resume",
        lambda: tool.summarize_workflow(workflow),
        task_key="approval.resume",
        task_attempt=int(state.get("correction_attempts", 0)),
        task_input={"workflow_run_id": workflow.get("id"), "workflow_status": workflow.get("status")},
    )
    record_handoff(
        context.run_id,
        "human_approval",
        "critic",
        "quality.critic",
        {"workflow_run_id": workflow.get("id"), "workflow_status": workflow.get("status")},
    )
    update = {"execution_output": content}
    _record_checkpoint(context, "human_approval", {**state, **update})
    return update


def _critic_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    critic = CriticAgent()
    attempt = int(state.get("correction_attempts", 0))
    content = _run_agent_message(
        context.run_id,
        critic.name,
        "critic",
        lambda: critic.run(
            state["execution_output"]["workflow_run_id"],
            state["supervisor_output"],
            state.get("risk_output"),
        ),
        task_key="quality.critic",
        task_attempt=attempt,
        task_input={
            "workflow_run_id": state["execution_output"]["workflow_run_id"],
            "supervisor": state.get("supervisor_output"),
            "risk": state.get("risk_output"),
        },
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
        task_key="quality.self_correction",
        task_attempt=int(state.get("correction_attempts", 0)),
        task_input={"critic_report": state["critic_report"]},
    )
    correction_attempts = int(state.get("correction_attempts", 0)) + (1 if content.get("should_retry") else 0)
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
        task_key="memory.write",
        task_attempt=int(state.get("correction_attempts", 0)),
        task_input={
            "critic_report": state["critic_report"],
            "workflow_run_id": state["execution_output"]["workflow_run_id"],
        },
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


def _route_after_execution(state: MultiAgentState) -> str:
    status = state.get("execution_output", {}).get("workflow_status")
    return "human_approval" if status == "waiting_approval" else "critic"


def _route_research(state: MultiAgentState) -> str:
    return "research_parallel" if state.get("supervisor_output", {}).get("research_required", True) else "research_bypass"


def _route_after_critic(state: MultiAgentState) -> str:
    if not state.get("enable_self_correction", True):
        return "memory_write"
    critic_report = state.get("critic_report", {})
    if critic_report.get("passed", False):
        return "memory_write"
    if int(state.get("correction_attempts", 0)) >= int(state.get("max_correction_attempts", 1)):
        return "memory_write"
    return "self_correction"


def _route_after_correction(state: MultiAgentState) -> str:
    return "retry" if state.get("self_correction_report", {}).get("should_retry") else "stop"


def _acl_context(state: MultiAgentState) -> dict:
    return {
        "user_id": state.get("requester_user_id"),
        "user_department": state.get("requester_department"),
        "user_role": state.get("requester_role"),
        "tenant_id": state.get("tenant_id"),
    }


def _run_agent_message(
    run_id: str,
    agent_name: str,
    role: str,
    fn: Callable[[], dict],
    *,
    task_key: str | None = None,
    task_attempt: int = 0,
    task_input: dict | None = None,
) -> dict:
    started = time.perf_counter()
    status = "completed"
    if task_key:
        start_task(
            run_id,
            task_key,
            agent_name,
            attempt=task_attempt,
            task_input=task_input,
        )
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
    if task_key:
        finish_task(
            run_id,
            task_key,
            attempt=task_attempt,
            output=content,
            status=status,
            duration_ms=latency_ms,
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
    with _checkpoint_lock:
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
        "enterprise_research",
        "local_research",
        "research_output",
        "compliance_risk_vote",
        "operational_risk_vote",
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
    execution_status = state.get("execution_output", {}).get("workflow_status")
    if critic_report.get("passed") and execution_status == "cancelled":
        final_status = "cancelled"
    elif critic_report.get("passed") and execution_status != "failed":
        final_status = "completed"
    else:
        final_status = "failed"
    latency_ms = int((time.perf_counter() - started) * 1000)
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE multi_agent_runs
            SET status = ?,
                workflow_run_id = ?,
                final_summary = ?,
                critic_score = ?,
                critic_report_json = ?,
                memory_item_id = ?,
                correction_count = ?,
                latency_ms = COALESCE(latency_ms, 0) + ?,
                completed_at = ?
            WHERE id = ?
            """,
            (
                final_status,
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


def _pause_run(run_id: str, started: float, state: dict) -> None:
    execution = state.get("execution_output", {})
    latency_ms = int((time.perf_counter() - started) * 1000)
    summary = (
        f"interrupted_for_human_approval; workflow={execution.get('workflow_run_id')}; "
        f"approval={execution.get('approval_id')}; thread_id={state.get('thread_id')}"
    )
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE multi_agent_runs
            SET status = 'waiting_approval',
                workflow_run_id = ?,
                final_summary = ?,
                latency_ms = COALESCE(latency_ms, 0) + ?
            WHERE id = ?
            """,
            (execution.get("workflow_run_id"), summary, latency_ms, run_id),
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
                latency_ms = COALESCE(latency_ms, 0) + ?,
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
    run["tasks"] = list_tasks(run_id)
    run["handoffs"] = list_handoffs(run_id)
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


def _open_checkpoint_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(_langgraph_checkpoint_path()),
        check_same_thread=False,
        timeout=30,
    )
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection
