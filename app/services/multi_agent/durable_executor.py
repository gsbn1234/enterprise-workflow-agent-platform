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
from app.services.agent import get_run_detail
from app.services.audit import record_audit
from app.services.multi_agent.agents import (
    ComplianceRiskAgent,
    CorrectionAgent,
    CriticAgent,
    EnterpriseRagResearchAgent,
    HistoricalTicketResearchAgent,
    LocalPolicyResearchAgent,
    MemoryAgent,
    OperationalRiskAgent,
    RagResearchAgent,
    ResolutionAgent,
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
from app.services.it.execution import (
    execute_it_action,
    find_approved_approval_id,
    find_denied_approval_id,
    it_action_arguments,
    record_it_action_skipped,
)
from app.services.auth import AuthContext
from app.services.it.risk_gate import ACTION_CLASSES as IT_ACTION_CLASSES
from app.services.it.risk_gate import DECISION_DENY, DECISION_REQUIRE_APPROVAL
from app.services.it.risk_gate import UNKNOWN_CRITICALITY, UNKNOWN_ENVIRONMENT
from app.services.it.risk_gate import evaluate as evaluate_it_risk
from app.services.it.risk_gate import merge_criticality, merge_environment
from app.services.it.triage import ENVIRONMENTS, INTENTS, RESOURCE_CODES, SERVICE_CODES
from app.services.it.triage import SOFTWARE_CODES, TriageResult
from app.services.it.triage import classify as classify_it_request
from app.services.it.triage import llm_fallback as triage_llm_fallback
from app.services.it.triage import needs_llm_fallback
from app.services.llm import call_json, llm_ready
from app.services.llm_schemas import IntentFallbackResult
from app.services.llm_telemetry import llm_context, record_llm_call
from app.services.tenancy import effective_tenant_id
from app.services.tools.registry import call_tool
from app.services.tools.ticketing import update_ticket
from app.utils import compact_text, json_dumps, json_loads, new_id, utc_now


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
    # IT service loop (Phase 2). ``it_ticket_id`` is both the ticket linkage and
    # the mode switch: it is only present on runs started for an IT ticket, and
    # the IT nodes return an empty update without it, so a non-IT run's
    # state dictionary is byte-for-byte what it was before.
    it_ticket_id: str
    it_triage: dict
    it_research_query: str
    # Phase 3's second evidence channel. A sibling of ``research_output``, never
    # folded into it: the resolver takes both and may only act on one.
    it_history: dict
    it_resolution: dict
    it_risk_decision: dict
    it_execution: dict


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
    it_ticket_id: str | None = None,
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
             executor_type, thread_id, correction_count, replay_of_run_id, it_ticket_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'running', 'durable_langgraph', ?, 0, ?, ?, ?)
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
                it_ticket_id,
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
    if it_ticket_id:
        # Inserted only when set, so every pre-existing run keeps the exact
        # state dictionary — and therefore the exact checkpoint payload — it
        # had before this key existed.
        initial_state["it_ticket_id"] = it_ticket_id

    try:
        # Every model call made inside the graph is attributed to this run.
        # The nodes themselves were never given a run id -- they read it from
        # here -- so this one line is what makes ``multi_agent_run_id`` on
        # ``llm_calls`` mean anything.
        with llm_context(multi_agent_run_id=run_id, ticket_id=it_ticket_id):
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
            SELECT id, thread_id, requester_user_id, tenant_id, it_ticket_id
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
        # A resumed run asks the model just as often as a fresh one -- the
        # triage fallback and the expert agents all run again on this pass --
        # so it announces itself the same way, keeping the run's own ticket.
        with llm_context(multi_agent_run_id=run["id"], ticket_id=run.get("it_ticket_id")):
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
    builder.add_node("it_triage", lambda state: _it_triage_node(state, context))
    builder.add_node("supervisor", lambda state: _supervisor_node(state, context))
    builder.add_node("research_dispatch", lambda state: _research_dispatch_node(state, context))
    builder.add_node("research_bypass", lambda state: _research_bypass_node(state, context))
    builder.add_node("enterprise_rag_research", lambda state: _enterprise_rag_research_node(state, context))
    builder.add_node("local_policy_research", lambda state: _local_policy_research_node(state, context))
    builder.add_node("evidence_synthesis", lambda state: _evidence_synthesis_node(state, context))
    builder.add_node("it_history", lambda state: _it_history_node(state, context))
    builder.add_node("it_resolution", lambda state: _it_resolution_node(state, context))
    builder.add_node("risk_dispatch", lambda state: _risk_dispatch_node(state, context))
    builder.add_node("compliance_risk", lambda state: _compliance_risk_node(state, context))
    builder.add_node("operational_risk", lambda state: _operational_risk_node(state, context))
    builder.add_node("risk_consensus", lambda state: _risk_consensus_node(state, context))
    builder.add_node("risk_gate", lambda state: _risk_gate_node(state, context))
    builder.add_node("tool_execution", lambda state: _tool_execution_node(state, context))
    builder.add_node("human_approval", lambda state: _human_approval_node(state, context))
    builder.add_node("critic", lambda state: _critic_node(state, context))
    builder.add_node("self_correction", lambda state: _self_correction_node(state, context))
    builder.add_node("memory_write", lambda state: _memory_write_node(state, context))
    builder.add_node("finalize", lambda state: _finalize_node(state, context))

    builder.add_edge(START, "memory_retrieve")
    # IT triage sits between memory and planning: it is the only place the IT
    # ticket is read, and it hands the planner an objective enriched with the
    # classified entities so the retrieval nodes below can match the corpus.
    builder.add_edge("memory_retrieve", "it_triage")
    builder.add_edge("it_triage", "supervisor")
    builder.add_conditional_edges(
        "supervisor",
        _route_research,
        {"research_parallel": "research_dispatch", "research_bypass": "research_bypass"},
    )
    builder.add_edge("research_dispatch", "enterprise_rag_research")
    builder.add_edge("research_dispatch", "local_policy_research")
    builder.add_edge(["enterprise_rag_research", "local_policy_research"], "evidence_synthesis")
    builder.add_edge("evidence_synthesis", "it_history")
    builder.add_edge("research_bypass", "it_history")
    builder.add_edge("it_history", "it_resolution")
    builder.add_edge("it_resolution", "risk_dispatch")
    builder.add_edge("risk_dispatch", "compliance_risk")
    builder.add_edge("risk_dispatch", "operational_risk")
    builder.add_edge(["compliance_risk", "operational_risk"], "risk_consensus")
    # The deterministic gate runs after the LLM risk agents, never instead of
    # them: the votes stay exactly as they were and the gate is what the
    # executor actually obeys.
    builder.add_edge("risk_consensus", "risk_gate")
    builder.add_edge("risk_gate", "tool_execution")
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


def _it_triage_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    """Classify the IT request and derive the query the research nodes will use.

    The classifier is the Phase 1 deterministic one, reused rather than
    rewritten, and the ticket wins over a fresh classification: Phase 1 already
    classified this request when the reporter filed it, and a re-worded
    objective must not silently reclassify a live incident.

    The entity-augmented query built here is the only reason the retrieval
    nodes below can match an IT corpus at all — the reporter's own sentence
    ("我的生产 Redis 连不上了") carries the words, but the *codes* the corpus is
    tagged with (``REDIS``, ``production``) come from the classifier.
    """
    ticket_id = str(state.get("it_ticket_id") or "")
    if not ticket_id:
        # Not an IT run: no message, no task, no checkpoint, no audit. The
        # state stays exactly as it was, which is what keeps every pre-existing
        # trace and checkpoint payload identical.
        return {}
    objective = state.get("active_objective") or state["objective"]
    attempt = int(state.get("correction_attempts", 0))
    tenant_id = state.get("tenant_id")
    actor = state.get("requester_user_id") or "agent"

    def _triage() -> dict:
        fresh_result = classify_it_request(objective)
        fresh = fresh_result.to_dict()
        ticket = _it_ticket(state, ticket_id)
        stored = dict(ticket.get("triage") or {})
        source = "phase1_stored" if stored else "recomputed"
        if stored:
            # The reporter's own classification stands, and the objective here
            # may have been re-worded since — a model must not get to silently
            # reclassify a live incident off the back of that.
            #
            # It stands *unless it was itself unsure*. A stored triage below the
            # confidence bar is exactly the case the fallback exists for, and
            # the stored reading is what the model is given as its base: every
            # stored value wins, the model may only fill gaps, and priority and
            # needs_approval stay escalate-only. So the run can proceed on a
            # refined reading but never on a laxer one. The ticket row itself is
            # not rewritten — the refinement belongs to this run, and the audit
            # below records it as such.
            triage = {**fresh, **stored}
            base = _triage_result_from(triage)
            fallback_trace = None
            if needs_llm_fallback(base, settings.llm_triage_min_confidence):
                merged, fallback_trace = _it_triage_fallback(
                    objective, base, ticket_id=ticket_id, actor=actor, tenant_id=tenant_id
                )
                # ``_it_triage_fallback`` hands back the base unchanged whenever
                # the call failed or the answer was unusable, so only a merge
                # that actually happened is allowed to replace the reading.
                if merged.get("mode") == "llm_assisted":
                    triage = merged
                    source = "phase1_stored_llm_assisted"
        else:
            triage, fallback_trace = _it_triage_fallback(
                objective, fresh_result, ticket_id=ticket_id, actor=actor, tenant_id=tenant_id
            )
        entities = dict(triage.get("entities") or {})
        query = _it_research_query(objective, entities)
        record_audit(
            "it.triage_classified",
            "ticket",
            ticket_id,
            {
                "intent": triage.get("intent"),
                "category": triage.get("category"),
                "priority": triage.get("priority"),
                "entities": entities,
                "needs_approval": bool(triage.get("needs_approval")),
                "missing_information": list(triage.get("missing_information") or []),
                "confidence": triage.get("confidence"),
                "mode": triage.get("mode"),
                "source": source,
                "llm": fallback_trace,
            },
            actor=actor,
            tenant_id=tenant_id,
        )
        record_audit(
            "it.research_query_built",
            "ticket",
            ticket_id,
            {
                "query": query,
                "entities": entities,
                "objective_length": len(objective),
            },
            actor=actor,
            tenant_id=tenant_id,
        )
        return {
            **triage,
            "ticket_id": ticket_id,
            "ticket_status": ticket.get("status"),
            "asset_id": ticket.get("asset_id"),
            "it_category": ticket.get("it_category"),
            "environment": ticket.get("environment") or entities.get("environment"),
            "research_query": query,
            "source": source,
            "producer": "it_triage",
            "llm": fallback_trace,
        }

    content = _run_agent_message(
        context.run_id,
        "it_triage",
        "it_intake",
        _triage,
        task_key="it.triage",
        task_attempt=attempt,
        task_input={"ticket_id": ticket_id, "objective": objective},
    )
    update = {"it_triage": content, "it_research_query": content.get("research_query") or objective}
    _record_checkpoint(context, "it_triage", {**state, **update})
    return update


def _triage_result_from(stored: dict) -> TriageResult:
    """Rehydrate a persisted triage dict so the fallback gate can read it.

    The gate takes a :class:`TriageResult` because that is what ``classify``
    returns, but on the ticket-backed path the reading under consideration is
    the one stored at intake — a plain dict that has been through JSON. Every
    field is read defensively: an unrecognised intent or category is kept as
    the string it is rather than blanked, since the fallback treats the base
    as the reading to refine and a blank one would invite the model to fill in
    more than it should.
    """
    entities = stored.get("entities")
    missing = stored.get("missing_information")
    return TriageResult(
        intent=str(stored.get("intent") or ""),
        category=str(stored.get("category") or ""),
        priority=str(stored.get("priority") or ""),
        entities=dict(entities) if isinstance(entities, dict) else {},
        needs_approval=bool(stored.get("needs_approval")),
        missing_information=list(missing) if isinstance(missing, list) else [],
        confidence=float(stored.get("confidence") or 0.0),
        mode=str(stored.get("mode") or "deterministic"),
    )


def _it_triage_fallback(
    objective: str,
    fresh: TriageResult,
    *,
    ticket_id: str,
    actor: str,
    tenant_id: str | None,
) -> tuple[dict, dict | None]:
    """Ask a model to re-read a request the keyword rules could not place.

    This is the only place in the triage path where a model is consulted, and
    it is a *fallback* in the literal sense: ``classify`` has already run and
    already returned an answer. Nothing here can leave the run without a
    triage, and the two ways this can go wrong — the call failing, and the call
    answering in a vocabulary the platform does not have — both end with the
    deterministic result being used unchanged.

    Three conditions gate the call, and each is checked in code rather than
    asked of the model:

    * the fallback is switched on and the provider is reachable;
    * the reading handed in came back below ``llm_triage_min_confidence``;
    * that reading is ``deterministic`` rather than already ``llm_assisted`` —
      one fallback per request, so a model cannot ask itself for a second
      opinion.

    ``fresh`` is whatever reading is under consideration, which is not always a
    fresh ``classify`` call: the caller passes the *stored* triage when the
    ticket already carries one, so the model refines the reading the reporter's
    ticket was filed under rather than a re-reading of a possibly re-worded
    objective. It is a :class:`TriageResult` either way, and the merge that
    follows does not care which of the two it was handed.

    Both outcomes are audited, including the rejection. A model that was asked
    and whose answer was thrown away is exactly the event somebody reading the
    trail later needs to see, and the rejection reason distinguishes "the call
    failed" from "the call answered something that is not an intent".
    """
    if not settings.llm_triage_fallback_enabled or not llm_ready():
        return fresh.to_dict(), None
    if not needs_llm_fallback(fresh, settings.llm_triage_min_confidence):
        return fresh.to_dict(), None

    outcome = call_json(
        [
            {
                "role": "system",
                "content": (
                    "You classify enterprise IT service requests. Read the request and return JSON with "
                    "intent, and where the request supports it service, resource, environment, confidence "
                    "and reason. intent must be exactly one of the supplied allowed values. service and "
                    "environment must come from the supplied allowed values; omit a field rather than "
                    "guessing. Do not invent details the request does not contain."
                ),
            },
            {
                "role": "user",
                "content": json_dumps(
                    {
                        "request": objective,
                        "allowed_intents": list(INTENTS),
                        "allowed_services": list(SERVICE_CODES),
                        "allowed_resources": list(RESOURCE_CODES),
                        "allowed_software": list(SOFTWARE_CODES),
                        "allowed_environments": list(ENVIRONMENTS),
                        "deterministic_reading": fresh.to_dict(),
                    }
                ),
            },
        ],
        operation="it_triage_fallback",
        schema=IntentFallbackResult,
        temperature=0,
        max_tokens=400,
    )
    record_llm_call(outcome, ticket_id=ticket_id)

    if not outcome.ok:
        record_audit(
            "it.triage_llm_fallback_rejected",
            "ticket",
            ticket_id,
            {
                "reason": outcome.status,
                "error_type": outcome.error_type,
                "error": outcome.error_message,
                "base_confidence": fresh.confidence,
                "threshold": settings.llm_triage_min_confidence,
            },
            actor=actor,
            tenant_id=tenant_id,
        )
        return fresh.to_dict(), _llm_trace(outcome)

    merged = triage_llm_fallback(objective, fresh, outcome)
    if merged is None:
        record_audit(
            "it.triage_llm_fallback_rejected",
            "ticket",
            ticket_id,
            {
                "reason": "intent_not_in_vocabulary",
                "proposed_intent": str((outcome.value or {}).get("intent") or "")[:60],
                "base_confidence": fresh.confidence,
            },
            actor=actor,
            tenant_id=tenant_id,
        )
        return fresh.to_dict(), _llm_trace(outcome)

    record_audit(
        "it.triage_llm_fallback_accepted",
        "ticket",
        ticket_id,
        {
            "base": fresh.to_dict(),
            "merged": merged.to_dict(),
            "model_confidence": (outcome.value or {}).get("confidence"),
            "reason": str((outcome.value or {}).get("reason") or "")[:300],
        },
        actor=actor,
        tenant_id=tenant_id,
    )
    return merged.to_dict(), _llm_trace(outcome)


def _llm_trace(outcome) -> dict:
    """The same fixed call record the agent payloads carry, for the audit row."""
    return {
        "status": outcome.status,
        "operation": outcome.operation,
        "provider": outcome.provider,
        "model": outcome.model,
        "latency_ms": round(outcome.latency_ms, 2),
        "retry_count": outcome.retry_count,
        "fallback_used": outcome.fallback_used,
        "error_type": outcome.error_type,
        "usage": {
            "available": outcome.usage_available,
            "prompt_tokens": outcome.prompt_tokens,
            "completion_tokens": outcome.completion_tokens,
            "total_tokens": outcome.total_tokens,
        },
    }


def _it_research_query(objective: str, entities: dict) -> str:
    """Append the classified entity codes the objective does not already say.

    Order is preserved and the objective always leads, so a human reading the
    audit row sees the reporter's words first and the added codes after.
    """
    parts = [objective]
    for key in ("service", "resource", "software", "environment", "access_level"):
        value = str(entities.get(key) or "").strip()
        if value and value.lower() not in objective.lower():
            parts.append(value)
    return " ".join(dict.fromkeys(parts))[:400]


def _it_ticket(state: MultiAgentState, ticket_id: str) -> dict:
    """Read the IT ticket through the tool registry, never with a raw query.

    ``query_tickets`` takes no ``auth_context``, so ``call_tool`` does not
    inject one and the tenant scope has to be passed explicitly — without it
    this read would cross tenants.
    """
    result = call_tool(
        "query_tickets",
        {"ticket_ref": ticket_id, "limit": 1, "tenant_id": state.get("tenant_id")},
        actor=state.get("requester_user_id") or "agent",
        source="multi_agent_it",
    )
    tickets = result.get("tickets") if isinstance(result, dict) else None
    if isinstance(tickets, list) and tickets:
        return dict(tickets[0])
    return {}


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


IT_HISTORY_LIMIT = 3
"""§五's K: how many past tickets the resolution context is shown. Three keeps
the historical channel a reference rather than a second corpus — past the top
few, matches stop being comparable and start being noise."""


def _it_history_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    """Retrieve how comparable past tickets were handled.

    This is the second evidence channel and it is deliberately a *separate node
    feeding a separate state key*. Nothing here is merged into
    ``research_output``: the resolution agent receives the two channels under
    two parameter names and only one of them can move the selected action. A
    node that appended historical rows to the knowledge evidence would quietly
    promote a past workaround to policy, which is precisely what the separation
    exists to prevent.

    Sits between synthesis and the resolution proposal, so it runs after the
    knowledge channel is settled and before anything is decided. Like the other
    three IT nodes it is a no-op with no side effects for non-IT runs.
    """
    ticket_id = str(state.get("it_ticket_id") or "")
    if not ticket_id:
        return {}
    objective = state.get("active_objective") or state["objective"]
    triage = state.get("it_triage") or {}
    # Same query builder the triage node uses, so both channels search for the
    # same thing. It is computed here rather than read from ``it_research_query``
    # because nothing in the graph consumes that key today (see the Phase 3
    # report); reusing the helper keeps the two in step regardless.
    query = _it_research_query(objective, triage.get("entities") or {})
    attempt = int(state.get("correction_attempts", 0))
    tenant_id = state.get("tenant_id")
    limit = IT_HISTORY_LIMIT

    def _history() -> dict:
        content = HistoricalTicketResearchAgent().run(
            query, tenant_id=tenant_id, limit=limit
        )
        results = list(content.get("results") or [])
        record_audit(
            "it.historical_retrieved",
            "ticket",
            ticket_id,
            {
                "ticket_id": ticket_id,
                "query": content.get("query"),
                "terms": list(content.get("terms") or []),
                "count": int(content.get("count") or 0),
                "matched_count": int(content.get("matched_count") or 0),
                "source": content.get("source"),
                "mode": content.get("mode"),
                "available": content.get("available"),
                "reason": content.get("reason"),
                # Only what an auditor needs to see *why* this ticket was cited:
                # which ones, how close, and what was done at the time. The
                # resolution text itself is in the ticket corpus, not here.
                "top": [
                    {
                        "ticket_id": item.get("ticket_id"),
                        "title": item.get("title"),
                        "category": item.get("category"),
                        "similarity": item.get("similarity"),
                        "resolution_action": item.get("resolution_action"),
                        "status": item.get("status"),
                    }
                    for item in results
                ],
            },
            actor=state.get("requester_user_id") or "agent",
            tenant_id=tenant_id,
        )
        return content

    content = _run_agent_message(
        context.run_id,
        HistoricalTicketResearchAgent.name,
        "it_historian",
        _history,
        task_key="it.history",
        task_attempt=attempt,
        task_input={"ticket_id": ticket_id, "query": query, "limit": limit},
    )
    record_handoff(
        context.run_id,
        HistoricalTicketResearchAgent.name,
        "it_history",
        "it.resolution",
        {"count": content.get("count"), "available": content.get("available")},
    )
    update = {"it_history": content}
    _record_checkpoint(context, "it_history", {**state, **update})
    return update


def _it_resolution_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    """Ask the resolution agent to propose an action from the retrieved evidence.

    The agent proposes; it never executes. Its output is the input to the
    deterministic gate two nodes later, and everything that will actually run —
    action class, tool, arguments — is read from the deterministic fields of
    that output, never from the LLM-phrased ones.
    """
    ticket_id = str(state.get("it_ticket_id") or "")
    if not ticket_id:
        return {}
    objective = state.get("active_objective") or state["objective"]
    triage = state.get("it_triage") or {}
    research_output = state.get("research_output") or {}
    historical_output = state.get("it_history") or {}
    attempt = int(state.get("correction_attempts", 0))
    tenant_id = state.get("tenant_id")
    actor = state.get("requester_user_id") or "agent"

    def _resolve() -> dict:
        ticket = _it_ticket(state, ticket_id)
        content = ResolutionAgent().run(
            objective,
            triage=triage,
            research_output=research_output,
            ticket=ticket,
            historical_output=historical_output,
        )
        content["ticket_id"] = ticket_id
        common = {
            "ticket_id": ticket_id,
            "evidence_count": int(content.get("evidence_count") or 0),
            "missing_information": list(content.get("missing_information") or []),
            "mode": content.get("mode"),
            # How much precedent was on hand is worth recording on both exits.
            # ``historical_reference`` is the one that matters: true only when
            # the history is standing in for knowledge that does not exist.
            "historical_count": int(content.get("historical_count") or 0),
            "historical_reference": bool(content.get("historical_reference")),
        }
        if content.get("status") == "NO_KNOWLEDGE":
            # The audit row that proves nothing was invented: no diagnosis, no
            # action, and the reason is the absence of evidence.
            record_audit(
                "it.resolution_no_knowledge",
                "ticket",
                ticket_id,
                {
                    **common,
                    "warnings": list(content.get("warnings") or []),
                    "route": content.get("route"),
                    "reason": content.get("reason"),
                },
                actor=actor,
                tenant_id=tenant_id,
            )
        else:
            record_audit(
                "it.resolution_proposed",
                "ticket",
                ticket_id,
                {
                    **common,
                    "action_type": content.get("action_type"),
                    "proposed_action": content.get("proposed_action"),
                    "action_arguments": content.get("action_arguments"),
                    "target": content.get("target"),
                    "environment": content.get("environment"),
                    "confidence": content.get("confidence"),
                    "reason": content.get("reason"),
                    # Knowledge evidence. Anything with a historical ``source``
                    # in this list would be a bug: the resolver builds this from
                    # the knowledge channel alone.
                    "evidence": [
                        {
                            "article_id": item.get("article_id"),
                            "title": item.get("title"),
                            "source": item.get("source"),
                            "score": item.get("score"),
                            "snippet": compact_text(str(item.get("snippet") or ""), 240),
                        }
                        for item in (content.get("evidence") or [])
                    ],
                    "historical_divergence": bool(content.get("historical_divergence")),
                    "historical_evidence": [
                        {
                            "ticket_id": item.get("ticket_id"),
                            "title": item.get("title"),
                            "resolution_action": item.get("resolution_action"),
                            "similarity": item.get("similarity"),
                            "snippet": compact_text(str(item.get("snippet") or ""), 240),
                        }
                        for item in (content.get("historical_evidence") or [])
                    ],
                },
                actor=actor,
                tenant_id=tenant_id,
            )
        return content

    content = _run_agent_message(
        context.run_id,
        ResolutionAgent.name,
        "it_resolver",
        _resolve,
        task_key="it.resolution",
        task_attempt=attempt,
        task_input={
            "ticket_id": ticket_id,
            "triage": triage,
            "evidence_count": research_output.get("evidence_count"),
        },
    )
    update = {"it_resolution": content}
    _record_checkpoint(context, "it_resolution", {**state, **update})
    return update


def _it_asset_risk_metadata(state: MultiAgentState, resolution: dict) -> tuple[str, str, dict]:
    """Read the target asset's own environment and criticality, fail-closed.

    Phase 3's finding: the gate was told the environment by the *reporter* and
    the criticality by the triage priority, and never once looked at the asset
    the action was actually aimed at. A ticket saying "预发环境的 Redis 缓存需要
    清理" therefore auto-executed a cache flush against REDIS-001, which the
    asset table records as production and critical. The reporter's wording is a
    claim; the asset row is the fact.

    The read goes through ``call_tool`` like every other privileged access, so
    the registry's role gate and the ``mcp.tool_*`` audit rows still apply and
    an agent never reaches a Python function directly. ``get_asset`` redacts
    ``serial``/``owner_user_id``/``metadata`` on production assets but never
    ``environment`` or ``criticality`` — the two fields this needs.

    Returns the pair to merge plus the provenance for the audit row. Anything
    that stops the read — no asset id on the ticket, no such row, a refused
    call, an unexpected error — yields the fail-closed defaults, because "we
    could not verify this target" must not be the cheap way past the gate.
    """
    arguments = resolution.get("action_arguments")
    arguments = arguments if isinstance(arguments, dict) else {}
    triage = state.get("it_triage") or {}
    # The action's own target first (what will actually be mutated), then the
    # asset the ticket was filed against. A permission grant carries no asset id
    # in its arguments, so the ticket's binding is what is left.
    asset_id = next(
        (
            text
            for text in (
                str(arguments.get("asset_id") or "").strip(),
                str(triage.get("asset_id") or "").strip(),
                str(resolution.get("target") or "").strip(),
            )
            if text
        ),
        None,
    )
    provenance: dict = {"asset_id": asset_id, "asset_lookup": "no_asset_id"}
    if not asset_id:
        return UNKNOWN_ENVIRONMENT, UNKNOWN_CRITICALITY, provenance

    requester = str(state.get("requester_user_id") or "agent")
    auth_context = AuthContext(
        user_id=requester,
        display_name=requester,
        department=str(state.get("requester_department") or ""),
        role=str(state.get("requester_role") or "employee"),
        tenant_id=str(state.get("tenant_id") or effective_tenant_id(None)),
    )
    try:
        # No tenant argument: ``get_asset`` scopes itself with the ambient
        # tenancy context, exactly as intake's ``find_asset_by_type`` does.
        result = call_tool(
            "get_asset",
            {"asset_id": asset_id},
            actor=requester,
            source="multi_agent_it",
            auth_context=auth_context,
        )
    except Exception:  # pragma: no cover - a read that raises is still a read we did not get
        provenance["asset_lookup"] = "error"
        return UNKNOWN_ENVIRONMENT, UNKNOWN_CRITICALITY, provenance

    if not isinstance(result, dict) or result.get("error"):
        # "forbidden" is the registry's role gate refusing the read; anything
        # else is the tool itself failing. Both fail closed, but the audit row
        # keeps them apart so "why" stays answerable.
        error = str((result or {}).get("error") or "")
        provenance["asset_lookup"] = "denied" if error == "forbidden" else "error"
        provenance["asset_lookup_reason"] = str((result or {}).get("reason") or error)
        return UNKNOWN_ENVIRONMENT, UNKNOWN_CRITICALITY, provenance
    if not result.get("found"):
        provenance["asset_lookup"] = "not_found"
        return UNKNOWN_ENVIRONMENT, UNKNOWN_CRITICALITY, provenance

    asset = result.get("asset") or {}
    provenance["asset_lookup"] = "found"
    return (
        str(asset.get("environment") or "") or UNKNOWN_ENVIRONMENT,
        str(asset.get("criticality") or "") or UNKNOWN_CRITICALITY,
        provenance,
    )


def _risk_gate_node(state: MultiAgentState, context: DurableRunContext) -> dict:
    """Apply the deterministic risk gate and attach the plan the executor obeys.

    This node is the boundary between "the agents think" and "the platform
    does". Everything downstream — the approval, the tool call — reads the
    decision recorded here.

    Three things are deliberately *not* taken from the model:

    * ``environment`` and ``criticality``. Both are merged from the target
      asset's own metadata and from what the ticket says, and the **most
      severe** of the two wins (see :func:`_it_asset_risk_metadata`). The merge
      is ordinary Python that runs before the gate is called, so nothing a model
      produces can reach it — and because it takes the maximum, no source can
      argue the decision downwards, only upwards.
    * ``confidence`` is passed to the gate only so the gate can echo it into
      the audit row beside ``llm_confidence_used: False``. No branch of the gate
      reads it.
    """
    ticket_id = str(state.get("it_ticket_id") or "")
    if not ticket_id:
        return {}
    resolution = state.get("it_resolution") or {}
    triage = state.get("it_triage") or {}
    asset_environment, asset_criticality, provenance = _it_asset_risk_metadata(state, resolution)
    text_environment = resolution.get("environment")
    text_criticality = "critical" if triage.get("priority") == "urgent" else None
    decision = evaluate_it_risk(
        action_type=resolution.get("action_type"),
        environment=merge_environment(asset_environment, text_environment),
        criticality=merge_criticality(asset_criticality, text_criticality),
        triage_needs_approval=bool(triage.get("needs_approval")),
        evidence_count=int(resolution.get("evidence_count") or 0),
        missing_information=resolution.get("missing_information") or (),
        llm_confidence=resolution.get("confidence"),
        actor_role=state.get("requester_role"),
    ).to_dict()
    action_class = IT_ACTION_CLASSES.get(str(decision["action_type"] or ""))
    record_audit(
        "it.risk_gate_decided",
        "ticket",
        ticket_id,
        {
            "action_type": decision["action_type"],
            "risk_class": decision["risk_class"],
            "decision": decision["decision"],
            "executable": decision["executable"],
            "rule_id": decision["rule_id"],
            "reasons": decision["reasons"],
            "inputs": decision["inputs"],
            "llm_confidence": decision["llm_confidence"],
            "llm_confidence_used": decision["llm_confidence_used"],
            "mode": decision["mode"],
            "resolution_status": resolution.get("status"),
            # Requirement 十二: "why did the gate ask for approval?" answered
            # from this row alone — which asset, how bad it is, what kind of
            # action, and where each of the two facts came from.
            **provenance,
            "environment": decision["environment"],
            "criticality": decision["inputs"]["criticality"],
            "tool_name": decision["tool_name"],
            "action_class": action_class.to_dict() if action_class else None,
            "asset_environment": asset_environment,
            "asset_criticality": asset_criticality,
            "text_environment": text_environment,
            "text_criticality": text_criticality,
        },
        actor=state.get("requester_user_id") or "agent",
        tenant_id=state.get("tenant_id"),
    )
    # The gate, not the resolution node, owns the plan the executor reads: the
    # resolution ran before the decision existed and cannot know it.
    supervisor_output = dict(state.get("supervisor_output") or {})
    plan = dict(supervisor_output.get("plan") or {})
    plan.update(
        {
            "approval_action": resolution.get("action_type"),
            "approval_tool": decision["tool_name"] or "human_handoff",
            "final_ticket_status": "approved",
            "needs_approval": decision["decision"] == DECISION_REQUIRE_APPROVAL,
            "it_action_type": resolution.get("action_type"),
            "it_risk_rule_id": decision["rule_id"],
        }
    )
    supervisor_output["plan"] = plan
    update = {
        "it_risk_decision": decision,
        "supervisor_output": supervisor_output,
    }
    _record_checkpoint(context, "risk_gate", {**state, **update})
    return update


def _it_action_payload(state: MultiAgentState, context: DurableRunContext) -> dict | None:
    """The envelope ``run_workflow`` needs to carry out the gate's decision.

    Rebuilt from the two checkpointed dictionaries rather than stored as a third
    copy of the same facts, so there is no way for the decision and the action it
    authorises to drift apart.
    """
    ticket_id = str(state.get("it_ticket_id") or "")
    if not ticket_id:
        return None
    resolution = state.get("it_resolution") or {}
    decision = state.get("it_risk_decision") or {}
    if not decision:
        return None
    return {
        "ticket_id": ticket_id,
        "action_type": resolution.get("action_type"),
        "tool_name": decision.get("tool_name"),
        "arguments": it_action_arguments(resolution),
        "risk_decision": decision,
        "resolution": resolution,
        "requested_by": state.get("requester_user_id"),
        "multi_agent_run_id": context.run_id,
        # Carried so a second trip through the execution branch — the
        # correction loop can route back here — short-circuits instead of
        # running the same restart twice.
        "prior_execution": dict(state.get("it_execution") or {}),
    }


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
            it_action=_it_action_payload(state, context),
        ),
        task_key="action.execute",
        task_attempt=attempt,
        task_input={
            "plan": state.get("supervisor_output", {}).get("plan"),
            "research_handoff": state.get("research_output"),
            "risk_consensus": state.get("risk_output"),
            "it_ticket_id": state.get("it_ticket_id"),
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
    if state.get("it_ticket_id"):
        # run_workflow performed the action, and its result lives only on the
        # workflow step. Lifting it into the graph state is what lets the API
        # report what happened for an automatically cleared action, and what
        # stops the correction loop from running it a second time.
        recorded = _it_execution_from_step(content.get("workflow_run_id"))
        if recorded is not None:
            update["it_execution"] = recorded
    _record_checkpoint(context, "tool_execution", {**state, **update})
    return update


def _it_execution_from_step(workflow_run_id: str | None) -> dict | None:
    """The IT execution result recorded on this workflow run, if there was one."""
    if not workflow_run_id:
        return None
    workflow = get_run_detail(workflow_run_id) or {}
    for step in workflow.get("steps", []):
        if step.get("action_type") != "it_operation":
            continue
        output = step.get("tool_output")
        if isinstance(output, dict) and "executed" in output:
            return output
    return None


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
    if state.get("it_ticket_id"):
        # summarize_workflow above replaces execution_output wholesale and
        # carries no IT action, so the approved action would otherwise never
        # run. It runs here, after the human decided, and never before.
        update["it_execution"] = _execute_it_action_after_approval(state, context, workflow)
    _record_checkpoint(context, "human_approval", {**state, **update})
    return update


def _execute_it_action_after_approval(
    state: MultiAgentState, context: DurableRunContext, workflow: dict
) -> dict:
    """Run the IT action a human just approved — once, and only if it is runnable."""
    ticket_id = str(state.get("it_ticket_id") or "")
    resolution = state.get("it_resolution") or {}
    decision = state.get("it_risk_decision") or {}
    action_type = resolution.get("action_type")
    tool_name = decision.get("tool_name")
    requested_by = state.get("requester_user_id")
    workflow_run_id = str(workflow.get("id") or "")
    tenant_id = state.get("tenant_id")

    prior = state.get("it_execution") or {}
    if prior.get("executed"):
        # A second trip through this node must not become a second outage
        # window. The correction loop can route back here; the execution cannot
        # be repeated.
        return {**prior, "reason": "already_executed"}

    if str(workflow.get("status") or "") != "completed":
        # The reviewer declined. The workflow already rejected the ticket; all
        # that is left is to record that nothing ran, and why.
        record_it_action_skipped(
            ticket_id=ticket_id,
            action_type=action_type,
            tool_name=tool_name,
            reason="approval_denied",
            risk_decision=decision,
            approval_id=find_denied_approval_id(workflow_run_id),
            requested_by=requested_by,
            workflow_run_id=workflow_run_id,
            multi_agent_run_id=context.run_id,
        )
        return {"executed": False, "reason": "approval_denied", "ticket_id": ticket_id}

    execution = execute_it_action(
        ticket_id=ticket_id,
        action_type=action_type,
        tool_name=tool_name,
        arguments=it_action_arguments(resolution),
        risk_decision=decision,
        approval_id=find_approved_approval_id(workflow_run_id),
        requested_by=requested_by,
        workflow_run_id=workflow_run_id,
        multi_agent_run_id=context.run_id,
        prior_execution=prior,
    )
    ticket_status = "resolved" if execution.get("executed") else "investigating"
    comment = (
        "IT action executed after human approval; ticket resolved."
        if execution.get("executed")
        else (
            "Approved for human handling, not for execution "
            f"({execution.get('reason')}); the ticket is back with a human owner."
        )
    )
    _sync_it_ticket(ticket_id, ticket_status, comment, tenant_id, workflow_run_id, requested_by)
    return execution


def _sync_it_ticket(
    ticket_id: str,
    status: str,
    comment: str,
    tenant_id: str | None,
    workflow_run_id: str,
    actor: str | None,
) -> None:
    if not ticket_id:
        return
    update_ticket(
        ticket_id,
        status=status,
        comment=comment,
        actor=actor or "agent",
        agent_run_id=workflow_run_id,
        tenant_id=tenant_id,
    )


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
            it_context=_it_critic_context(state),
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


def _it_critic_context(state: MultiAgentState) -> dict | None:
    """What the critic needs to judge an IT run, or None for a non-IT run."""
    if not state.get("it_ticket_id"):
        return None
    decision = state.get("it_risk_decision") or {}
    return {
        "ticket_id": state.get("it_ticket_id"),
        "decision": decision.get("decision"),
        "tool_name": decision.get("tool_name"),
        "rule_id": decision.get("rule_id"),
        "executed": bool((state.get("it_execution") or {}).get("executed")),
    }


def _route_after_execution(state: MultiAgentState) -> str:
    status = state.get("execution_output", {}).get("workflow_status")
    return "human_approval" if status == "waiting_approval" else "critic"


def _route_research(state: MultiAgentState) -> str:
    # An IT resolution without retrieved evidence is not allowed to exist — the
    # agent's own rule is "no evidence, no answer" — so an IT run always
    # retrieves. Non-IT runs are unaffected.
    if state.get("it_ticket_id"):
        return "research_parallel"
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
        "it_ticket_id",
        "it_triage",
        "it_research_query",
        "it_history",
        "it_resolution",
        "it_risk_decision",
        "it_execution",
    ]
    # ``if key in state`` keeps non-IT checkpoints byte-identical: the IT key is
    # only ever present on a run that was started for an IT ticket.
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
