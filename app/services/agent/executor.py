from __future__ import annotations

import time
import logging
from typing import Any, Callable

from app.db import get_connection, row_to_dict, rows_to_dicts
from app.services.agent.planner import guard_objective, plan_workflow
from app.services.agent.retry import RetryPolicy, classify_error, default_retry_policy
from app.services.agent.state import WorkflowContext
from app.services.agent.ticket_commands import execute_ticket_query, execute_ticket_update, parse_ticket_command
from app.services.audit import record_audit
from app.services.it.execution import (
    NOT_EXECUTED_ALREADY_EXECUTED,
    NOT_EXECUTED_TOOL_ERROR,
    execute_it_action,
)
from app.services.it.risk_gate import DECISION_DENY, DECISION_REQUIRE_APPROVAL
from app.services.llm import polish_agent_answer
from app.services.observability import start_span
from app.services.requests import get_business_request, update_business_request_status
from app.services.tenancy import effective_tenant_id
from app.services.tools.approvals import create_approval, get_approval, mark_approval
from app.services.tools.crm import lookup_customer
from app.services.tools.email import draft_email, send_email
from app.services.tools.knowledge import query_enterprise_rag, search_knowledge
from app.services.tools.notifications import notify_internal_team
from app.services.tools.ticketing import create_ticket, update_ticket
from app.utils import compact_text, estimate_token_cost, json_dumps, json_loads, new_id, utc_now


logger = logging.getLogger("agent_platform.workflow")


def run_workflow(
    objective: str,
    *,
    request_id: str | None = None,
    requester_user_id: str | None = None,
    requester_department: str | None = None,
    requester_role: str | None = None,
    tenant_id: str | None = None,
    plan_override: dict | None = None,
    knowledge_override: dict | None = None,
    risk_override: dict | None = None,
    it_action: dict | None = None,
) -> dict:
    started = time.perf_counter()
    run_id = new_id("run")
    tenant = effective_tenant_id(tenant_id)
    logger.info(
        "workflow.run_started",
        extra={
            "event": "workflow.run_started",
            "run_id": run_id,
            "request_id": request_id,
            "tenant_id": tenant,
            "requester_user_id": requester_user_id,
        },
    )
    context = WorkflowContext(
        run_id=run_id,
        objective=objective,
        request_id=request_id,
        requester_user_id=requester_user_id,
        requester_department=requester_department,
    )
    now = utc_now()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO workflow_runs
            (id, request_id, objective, tenant_id, status, created_at)
            VALUES (?, ?, ?, ?, 'running', ?)
            """,
            (run_id, request_id, objective, tenant, now),
        )
    record_audit("workflow.start", "workflow_run", run_id, {"request_id": request_id}, actor=requester_user_id or "agent", tenant_id=tenant)

    with start_span("workflow.run", {"workflow.run_id": run_id, "tenant.id": tenant, "workflow.request_id": request_id}):
        guard = _run_step(
            context,
            "guard",
            "policy_check",
            None,
            {"objective": objective},
            lambda: guard_objective(objective),
            "Check for prompt injection, approval bypass attempts, and destructive instructions.",
        )
    if not guard["allowed"]:
        answer = guard["message"]
        _complete_run(
            run_id,
            status="refused",
            final_answer=answer,
            started=started,
            refusal_reason=guard["reason"],
        )
        return get_run_detail(run_id)

    coordinated_plan = _coordinated_plan(plan_override, risk_override)
    coordination = {
        "supervisor_plan_consumed": bool(plan_override),
        "shared_evidence_consumed": bool(knowledge_override),
        "synthesized_evidence_consumed": isinstance(knowledge_override, dict) and "evidence" in knowledge_override,
        "risk_consensus_consumed": bool(risk_override),
    }
    ticket_command = parse_ticket_command(objective)
    if ticket_command:
        ticket_coordination = {
            **coordination,
            "shared_evidence_consumed": False,
            "synthesized_evidence_consumed": False,
            "evidence_not_required": "scoped_existing_ticket_command",
        }
        plan = _run_step(
            context,
            "plan",
            "planner",
            None,
            {
                "objective": objective,
                "source": "supervisor_handoff" if coordinated_plan else "workflow_planner",
                "coordination": ticket_coordination,
            },
            lambda: coordinated_plan or plan_workflow(objective).__dict__,
            "Consume the supervisor plan before executing a scoped existing-ticket command.",
        )
        context.artifacts["plan"] = plan
        _update_run_plan(run_id, plan)
        command_payload = _run_step(
            context,
            "interpret_ticket_command",
            "planner",
            None,
            {"objective": objective},
            lambda: ticket_command.to_dict(),
            "Recognize natural-language ticket query or update intent before creating any new work item.",
        )
        category = "ticket_update" if ticket_command.intent == "update" else "ticket_query"
        if request_id:
            update_business_request_status(request_id, "running", category)

        if ticket_command.intent == "query":
            query_result = _run_step(
                context,
                "query_existing_tickets",
                "tool_call",
                "query_tickets",
                command_payload,
                lambda: execute_ticket_query(
                    ticket_command,
                    requester_role=requester_role,
                    requester_department=requester_department,
                    tenant_id=tenant,
                ),
                "Query existing tickets with role and department visibility applied.",
            )
            if _has_step_error(query_result):
                return _fail_workflow_from_tool_error(run_id, started, request_id, {"category": category}, "query_tickets", query_result)
            _complete_run(
                run_id,
                status="completed",
                final_answer=query_result.get("message") or "Ticket query completed.",
                started=started,
                needs_approval=False,
            )
            if request_id:
                update_business_request_status(request_id, "completed", category)
            return get_run_detail(run_id)

        update_result = _run_step(
            context,
            "update_existing_ticket",
            "tool_call",
            "update_ticket",
            command_payload,
            lambda: execute_ticket_update(
                ticket_command,
                requester_user_id=requester_user_id,
                requester_role=requester_role,
                requester_department=requester_department,
                tenant_id=tenant,
            ),
            "Update an existing ticket from natural-language instructions after permission checks.",
        )
        if _has_step_error(update_result):
            return _fail_workflow_from_tool_error(run_id, started, request_id, {"category": category}, "update_ticket", update_result)
        final_status = "refused" if update_result.get("error_code") == "permission_denied" else "completed"
        _complete_run(
            run_id,
            status=final_status,
            final_answer=update_result.get("message") or "Ticket update command finished.",
            started=started,
            needs_approval=False,
            refusal_reason=update_result.get("error_code") if final_status == "refused" else None,
        )
        if request_id:
            update_business_request_status(request_id, final_status, category)
        return get_run_detail(run_id)

    plan = _run_step(
        context,
        "plan",
        "planner",
        None,
        {
            "objective": objective,
            "source": "supervisor_handoff" if coordinated_plan else "workflow_planner",
            "coordination": coordination,
        },
        lambda: coordinated_plan or plan_workflow(objective).__dict__,
        "Consume the supervisor plan when present; otherwise classify the request, estimate risk, and choose the tool path.",
    )
    context.artifacts["plan"] = plan
    _update_run_plan(run_id, plan)
    if request_id:
        update_business_request_status(request_id, "running", plan["category"])

    shared_rag = (knowledge_override or {}).get("enterprise_rag") if knowledge_override else None
    rag_knowledge = _run_step(
        context,
        "retrieve_enterprise_rag",
        "tool_call",
        "query_enterprise_rag",
        {
            "question": objective,
            "top_k": 3,
            "user_context": {
                "user_id": requester_user_id,
                "user_department": requester_department,
                "user_role": requester_role,
            },
            "source": "research_agent_handoff" if shared_rag is not None else "direct_tool_call",
        },
        lambda: shared_rag
        if shared_rag is not None
        else query_enterprise_rag(
            objective,
            top_k=3,
            user_id=requester_user_id,
            user_department=requester_department,
            user_role=requester_role,
        ),
        "Consume ACL-filtered evidence from the research agent, or query enterprise RAG directly when no handoff exists.",
    )
    context.artifacts["rag_knowledge"] = rag_knowledge

    shared_local = (knowledge_override or {}).get("local_policy") if knowledge_override else None
    local_knowledge = _run_step(
        context,
        "retrieve_policy",
        "tool_call",
        "search_knowledge",
        {
            "query": objective,
            "limit": 3,
            "source": "research_agent_handoff" if shared_local is not None else "direct_tool_call",
        },
        lambda: shared_local if shared_local is not None else search_knowledge(objective, limit=3),
        "Consume the local-policy research handoff, or retrieve fallback evidence directly when no handoff exists.",
    )
    knowledge = _coordinated_knowledge(knowledge_override, rag_knowledge, local_knowledge)
    context.artifacts["knowledge"] = knowledge

    customer = {"customer": None, "matched_by": None}
    if _requires_customer_context(plan, objective):
        customer = _run_step(
            context,
            "collect_customer_context",
            "tool_call",
            "lookup_customer",
            {"query": objective, "tenant_id": tenant},
            lambda: lookup_customer(objective, tenant_id=tenant),
            "Look up customer context only for customer-facing workflows such as refunds and complaints.",
        )
    context.artifacts["customer"] = customer.get("customer")

    # An IT action takes over the run here, before the business approval branch
    # below: the IT ticket already exists (created by the intake path), so this
    # run must never create a second one, and whether anything executes was
    # already decided by the deterministic risk gate. ``it_action`` is None for
    # every non-IT run, which is what keeps all of the above untouched.
    if it_action:
        return _run_it_action_branch(
            context,
            run_id,
            started,
            request_id,
            tenant,
            plan,
            coordination,
            it_action,
        )

    if plan["needs_approval"]:
        proposed_ticket = _proposed_ticket_payload(plan, objective, knowledge, customer.get("customer"), run_id)
        approval_payload = {
            "objective": objective,
            "plan": plan,
            "proposed_ticket": proposed_ticket,
            "customer": customer.get("customer"),
            "knowledge": knowledge.get("results", []),
            "rag_citations": rag_knowledge.get("citations", []),
            "requester_user_id": requester_user_id,
            "requester_department": requester_department,
            "multi_agent_coordination": coordination,
        }
        approval = _run_step(
            context,
            "request_approval",
            "approval",
            "request_approval",
            approval_payload,
            lambda: create_approval(
                run_id,
                plan.get("approval_action") or "business_action",
                # The IT path names the tool it actually wants approved
                # (``restart_service``); nothing else sets this key, so every
                # pre-existing caller still records ``create_ticket``.
                plan.get("approval_tool") or "create_ticket",
                approval_payload,
                requested_by="agent",
                tenant_id=tenant,
            ),
            "Pause before creating external business artifacts; only approved requests create tickets.",
        )
        final_answer = _pending_approval_without_ticket_answer(plan, approval)
        _complete_run(
            run_id,
            status="waiting_approval",
            final_answer=final_answer,
            started=started,
            needs_approval=True,
        )
        if request_id:
            update_business_request_status(request_id, "waiting_approval", plan["category"])
        return get_run_detail(run_id)

    ticket = _run_step(
        context,
        "execute_ticket",
        "tool_call",
        "create_ticket",
        {
            "title": _ticket_title(plan, objective),
            "description": objective,
            "priority": plan["priority"],
            "owner_department": plan["recommended_owner"],
            "workflow_type": plan.get("workflow_type"),
            "category": plan.get("category"),
            "risk_level": plan.get("risk_level"),
            "approval_chain": plan.get("approval_chain") or [],
            "auto_actions": plan.get("auto_actions") or [],
            "blocked_actions": plan.get("blocked_actions") or [],
            "agent_run_id": run_id,
        },
        lambda: create_ticket(
            _ticket_title(plan, objective),
            _ticket_description(objective, plan, knowledge),
            customer_id=(customer.get("customer") or {}).get("id"),
            priority=plan["priority"],
            owner_department=plan["recommended_owner"],
            workflow_type=plan.get("workflow_type"),
            category=plan.get("category"),
            risk_level=plan.get("risk_level"),
            approval_chain=plan.get("approval_chain") or [],
            auto_actions=plan.get("auto_actions") or [],
            blocked_actions=plan.get("blocked_actions") or [],
            evidence=knowledge.get("results", [])[:10],
            agent_run_id=run_id,
            tenant_id=tenant,
        ),
        "Create an auditable work item so the agent action is connected to business operations.",
    )
    context.artifacts["ticket"] = ticket
    if _has_step_error(ticket) or not ticket.get("id"):
        return _fail_workflow_from_tool_error(run_id, started, request_id, plan, "create_ticket", ticket)
    _set_run_ticket(run_id, ticket["id"])

    if _requires_internal_notification(plan):
        notification = _run_step(
            context,
            "notify_internal_owner",
            "tool_call",
            "notify_internal_team",
            {
                "team": plan["recommended_owner"],
                "message": _internal_notification_message(plan, ticket, objective),
                "severity": plan["priority"],
                "ticket_id": ticket["id"],
            },
            lambda: notify_internal_team(
                plan["recommended_owner"],
                _internal_notification_message(plan, ticket, objective),
                severity=plan["priority"],
                ticket_id=ticket["id"],
                tenant_id=tenant,
            ),
            "Notify the responsible internal team while keeping an auditable notification record.",
        )
        context.artifacts["notification"] = notification
        _run_step(
            context,
            "sync_ticket_notification",
            "state_update",
            "update_ticket",
            {
                "ticket_id": ticket["id"],
                "comment": f"Internal notification sent to {plan['recommended_owner']}.",
                "agent_run_id": run_id,
            },
            lambda: update_ticket(
                ticket["id"],
                comment=f"Internal notification sent to {plan['recommended_owner']}.",
                agent_run_id=run_id,
                tenant_id=tenant,
            )
            or {"error": f"Ticket {ticket['id']} was not found."},
            "Append the internal notification outcome to the external ticket timeline.",
        )

    email_payload = _build_email_payload(objective, plan, customer.get("customer"), ticket, knowledge)
    if email_payload:
        draft = _run_step(
            context,
            "draft_response",
            "tool_call",
            "draft_email",
            email_payload,
            lambda: draft_email(**email_payload),
            "Prepare a customer-safe message but do not send high-risk content without approval.",
        )
        context.artifacts["draft_email"] = draft

    sent_email = None
    if context.artifacts.get("draft_email"):
        sent_email = _run_step(
            context,
            "send_response",
            "tool_call",
            "send_email",
            context.artifacts["draft_email"],
            lambda: send_email(**context.artifacts["draft_email"], tenant_id=tenant),
            "Send low-risk customer communication and keep an audit trail.",
        )
        context.artifacts["sent_email"] = sent_email
        if _has_step_error(sent_email) or not sent_email.get("id"):
            return _fail_workflow_from_tool_error(run_id, started, request_id, plan, "send_email", sent_email)

    ticket_update = _run_step(
        context,
        "finalize",
        "state_update",
        "update_ticket",
        {"ticket_id": ticket["id"], "status": _final_ticket_status(plan, sent_email)},
        lambda: update_ticket(
            ticket["id"],
            status=_final_ticket_status(plan, sent_email),
            comment=f"Workflow completed with final status {_final_ticket_status(plan, sent_email)}.",
            agent_run_id=run_id,
            tenant_id=tenant,
        )
        or {"error": f"Ticket {ticket['id']} was not found."},
        "Move the ticket to the correct operational state after agent execution.",
    )
    if _has_step_error(ticket_update):
        return _fail_workflow_from_tool_error(run_id, started, request_id, plan, "update_ticket", ticket_update)
    final_answer = _completed_answer(plan, ticket, sent_email)
    _complete_run(run_id, status="completed", final_answer=final_answer, started=started)
    if request_id:
        update_business_request_status(request_id, "completed", plan["category"])
    return get_run_detail(run_id)


def decide_approval_and_resume(approval_id: str, approved: bool, decided_by: str, reason: str | None = None) -> dict | None:
    approval_before = get_approval(approval_id)
    if not approval_before:
        return None
    approval = mark_approval(approval_id, approved, decided_by, reason)
    if not approval:
        return None
    run_id = approval["run_id"]
    run = _get_run_row(run_id)
    if not run:
        return None
    requested_status = "approved" if approved else "denied"
    if approval.get("status") != requested_status or not approval.get("decision_applied"):
        return get_run_detail(run_id)
    tenant = effective_tenant_id(run.get("tenant_id"))
    context = WorkflowContext(run_id=run_id, objective=run["objective"])
    payload = approval["payload"] or {}
    plan = payload.get("plan") or {}
    proposed_ticket = payload.get("proposed_ticket")
    legacy_ticket_id = payload.get("ticket_id")
    if not approved:
        _run_step(
            context,
            "approval_denied",
            "approval",
            None,
            {"approval_id": approval_id, "reason": reason},
            lambda: {"status": "denied", "reason": reason},
            "Human reviewer denied the action; the workflow is cancelled safely.",
        )
        denied_ticket_id = legacy_ticket_id
        if denied_ticket_id:
            _set_run_ticket(run_id, denied_ticket_id)
            _run_step(
                context,
                "approval_reject_ticket",
                "state_update",
                "update_ticket",
                {"ticket_id": denied_ticket_id, "status": "rejected", "approval_id": approval_id},
                lambda: update_ticket(
                    denied_ticket_id,
                    status="rejected",
                    comment=f"Approval denied by {decided_by}: {reason or 'no reason provided'}.",
                    actor=decided_by,
                    approval_id=approval_id,
                    agent_run_id=run_id,
                    tenant_id=tenant,
                )
                or {"error": f"Ticket {denied_ticket_id} was not found."},
                "Mark the operational ticket as rejected when the human reviewer denies the action.",
            )
        _complete_run(
            run_id,
            status="cancelled",
            final_answer=_approval_denied_answer(reason, bool(denied_ticket_id)),
            started=None,
        )
        return get_run_detail(run_id)

    if proposed_ticket and not legacy_ticket_id:
        ticket = _run_step(
            context,
            "approval_create_ticket",
            "tool_call",
            "create_ticket",
            {**proposed_ticket, "approval_id": approval_id},
            lambda: create_ticket(
                proposed_ticket["title"],
                proposed_ticket["description"],
                customer_id=proposed_ticket.get("customer_id"),
                priority=proposed_ticket.get("priority") or "normal",
                owner_department=proposed_ticket.get("owner_department") or "Business Ops",
                workflow_type=proposed_ticket.get("workflow_type"),
                category=proposed_ticket.get("category"),
                risk_level=proposed_ticket.get("risk_level"),
                approval_chain=proposed_ticket.get("approval_chain") or [],
                auto_actions=proposed_ticket.get("auto_actions") or [],
                blocked_actions=proposed_ticket.get("blocked_actions") or [],
                evidence=proposed_ticket.get("evidence") or [],
                agent_run_id=run_id,
                approval_id=approval_id,
                tenant_id=tenant,
            ),
            "Human approval was granted; create the external operational ticket now.",
        )
        if _has_step_error(ticket) or not ticket.get("id"):
            _complete_run(
                run_id,
                status="failed",
                final_answer=_tool_failure_message("create_ticket", ticket),
                started=None,
            )
            return get_run_detail(run_id)
        _set_run_ticket(run_id, ticket["id"])

        if _requires_internal_notification(plan):
            notification = _run_step(
                context,
                "approval_notify_internal_owner",
                "tool_call",
                "notify_internal_team",
                {
                    "team": plan.get("recommended_owner"),
                    "message": _internal_notification_message(plan, ticket, run["objective"]),
                    "severity": plan.get("priority") or ticket.get("priority") or "normal",
                    "ticket_id": ticket["id"],
                    "approval_id": approval_id,
                },
                lambda: notify_internal_team(
                    plan.get("recommended_owner") or "Operations",
                    _internal_notification_message(plan, ticket, run["objective"]),
                    severity=plan.get("priority") or ticket.get("priority") or "normal",
                    ticket_id=ticket["id"],
                    tenant_id=tenant,
                ),
                "Notify the responsible internal team after approval creates the ticket.",
            )
            _run_step(
                context,
                "approval_sync_ticket_notification",
                "state_update",
                "update_ticket",
                {
                    "ticket_id": ticket["id"],
                    "comment": f"Internal notification sent after approval {approval_id}.",
                    "approval_id": approval_id,
                },
                lambda: update_ticket(
                    ticket["id"],
                    comment=f"Internal notification sent after approval {approval_id}.",
                    actor=decided_by,
                    approval_id=approval_id,
                    agent_run_id=run_id,
                    evidence=[notification] if isinstance(notification, dict) else None,
                    tenant_id=tenant,
                )
                or {"error": f"Ticket {ticket['id']} was not found."},
                "Append the internal notification outcome to the ticket timeline.",
            )

        knowledge = {"results": payload.get("knowledge") or []}
        email_payload_after_approval = _build_email_payload(run["objective"], plan, payload.get("customer"), ticket, knowledge)
        sent_email_after_approval = None
        if email_payload_after_approval:
            draft = _run_step(
                context,
                "approval_draft_email",
                "tool_call",
                "draft_email",
                email_payload_after_approval,
                lambda: draft_email(**email_payload_after_approval),
                "Prepare the customer response after the human approval gate.",
            )
            if _has_step_error(draft):
                _complete_run(
                    run_id,
                    status="failed",
                    final_answer=_tool_failure_message("draft_email", draft),
                    started=None,
                )
                return get_run_detail(run_id)
            sent_email_after_approval = _run_step(
                context,
                "approval_execute_email",
                "tool_call",
                "send_email",
                {**draft, "approval_id": approval_id},
                lambda: send_email(
                    draft["to_address"],
                    draft["subject"],
                    draft["body"],
                    approval_id=approval_id,
                    tenant_id=tenant,
                ),
                "Human approval was granted; send the customer email.",
            )
            if _has_step_error(sent_email_after_approval) or not sent_email_after_approval.get("id"):
                _complete_run(
                    run_id,
                    status="failed",
                    final_answer=_tool_failure_message("send_email", sent_email_after_approval),
                    started=None,
                )
                return get_run_detail(run_id)

        ticket_update = _run_step(
            context,
            "approval_finalize_ticket",
            "state_update",
            "update_ticket",
            {"ticket_id": ticket["id"], "status": _final_ticket_status(plan, sent_email_after_approval), "approval_id": approval_id},
            lambda: update_ticket(
                ticket["id"],
                status=_final_ticket_status(plan, sent_email_after_approval),
                comment=f"Approval granted by {decided_by}; ticket created and workflow resumed.",
                actor=decided_by,
                approval_id=approval_id,
                agent_run_id=run_id,
                tenant_id=tenant,
            )
            or {"error": f"Ticket {ticket['id']} was not found."},
            "Finalize the newly created ticket after approved execution.",
        )
        if _has_step_error(ticket_update):
            _complete_run(
                run_id,
                status="failed",
                final_answer=_tool_failure_message("update_ticket", ticket_update),
                started=None,
            )
            return get_run_detail(run_id)

        _complete_run(
            run_id,
            status="completed",
            final_answer=_approval_created_ticket_answer(ticket, sent_email_after_approval),
            started=None,
        )
        return get_run_detail(run_id)

    email_payload = payload.get("email")
    sent_email = None
    if approval["tool_name"] == "send_email" and email_payload:
        sent_email = _run_step(
            context,
            "approval_execute_email",
            "tool_call",
            "send_email",
            {**email_payload, "approval_id": approval_id},
            lambda: send_email(
                email_payload["to_address"],
                email_payload["subject"],
                email_payload["body"],
                approval_id=approval_id,
                tenant_id=tenant,
            ),
            "Human approval was granted; execute the previously paused email action.",
        )
        if _has_step_error(sent_email) or not sent_email.get("id"):
            _complete_run(
                run_id,
                status="failed",
                final_answer=_tool_failure_message("send_email", sent_email),
                started=None,
            )
            return get_run_detail(run_id)

    ticket_id = payload.get("ticket_id")
    if ticket_id:
        _set_run_ticket(run_id, ticket_id)
        plan = payload.get("plan") or {}
        ticket_update = _run_step(
            context,
            "approval_update_ticket",
            "state_update",
            "update_ticket",
            {"ticket_id": ticket_id, "status": _final_ticket_status(plan, sent_email), "approval_id": approval_id},
            lambda: update_ticket(
                ticket_id,
                status=_final_ticket_status(plan, sent_email),
                comment=f"Approval granted by {decided_by}; workflow resumed.",
                actor=decided_by,
                approval_id=approval_id,
                agent_run_id=run_id,
                tenant_id=tenant,
            )
            or {"error": f"Ticket {ticket_id} was not found."},
            "Update the operational ticket after approval execution.",
        )
        if _has_step_error(ticket_update):
            _complete_run(
                run_id,
                status="failed",
                final_answer=_tool_failure_message("update_ticket", ticket_update),
                started=None,
            )
            return get_run_detail(run_id)

    answer = _approval_legacy_completed_answer(sent_email)
    _complete_run(run_id, status="completed", final_answer=answer, started=None)
    return get_run_detail(run_id)


def list_runs(limit: int = 100, tenant_id: str | None = None) -> list[dict]:
    with get_connection() as conn:
        if tenant_id:
            rows = conn.execute(
                """
                SELECT * FROM workflow_runs
                WHERE tenant_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM workflow_runs
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
    return rows_to_dicts(rows)


def get_run_detail(run_id: str) -> dict | None:
    run = _get_run_row(run_id)
    if not run:
        return None
    with get_connection() as conn:
        step_rows = conn.execute(
            """
            SELECT * FROM workflow_steps
            WHERE run_id = ?
            ORDER BY step_index ASC
            """,
            (run_id,),
        ).fetchall()
    steps = rows_to_dicts(step_rows)
    for step in steps:
        step["tool_input"] = json_loads(step.pop("tool_input_json"), {})
        step["tool_output"] = json_loads(step.pop("tool_output_json"), {})
        step["attempts"] = json_loads(step.pop("attempts_json", None), [])
    run["steps"] = steps
    return run


def _run_step(
    context: WorkflowContext,
    node_name: str,
    action_type: str,
    tool_name: str | None,
    tool_input: dict[str, Any],
    fn: Callable[[], dict],
    reasoning_summary: str,
    retry_policy: RetryPolicy | None = None,
) -> dict:
    started = time.perf_counter()
    policy = retry_policy or default_retry_policy(tool_name, action_type)
    max_attempts = max(1, policy.max_attempts if policy.retryable else 1)
    status = "completed"
    output: dict[str, Any] = {}
    error_type: str | None = None
    attempts: list[dict[str, Any]] = []
    for attempt_index in range(1, max_attempts + 1):
        attempt_started = time.perf_counter()
        try:
            with start_span(
                "workflow.step",
                {
                    "workflow.run_id": context.run_id,
                    "workflow.node_name": node_name,
                    "workflow.action_type": action_type,
                    "workflow.tool_name": tool_name or "",
                    "workflow.attempt": attempt_index,
                },
            ):
                output = fn()
            attempts.append(
                {
                    "attempt": attempt_index,
                    "status": "completed",
                    "latency_ms": _elapsed_ms(attempt_started),
                }
            )
            status = "completed"
            error_type = None
            break
        except Exception as exc:
            error_type = classify_error(exc)
            logger.warning(
                "workflow.step_failed",
                extra={
                    "event": "workflow.step_failed",
                    "run_id": context.run_id,
                    "node_name": node_name,
                    "action_type": action_type,
                    "tool_name": tool_name,
                    "attempt": attempt_index,
                    "error_type": error_type,
                    "error": str(exc),
                },
            )
            is_last_attempt = attempt_index >= max_attempts
            attempts.append(
                {
                    "attempt": attempt_index,
                    "status": "failed",
                    "error_type": error_type,
                    "error": str(exc),
                    "latency_ms": _elapsed_ms(attempt_started),
                    "will_retry": policy.retryable and not is_last_attempt,
                }
            )
            if is_last_attempt or not policy.retryable:
                status = "failed"
                output = {"error": str(exc), "error_type": error_type}
                break
            time.sleep(policy.backoff_seconds * (2 ** (attempt_index - 1)))
    latency_ms = int((time.perf_counter() - started) * 1000)
    context.step_index = _next_step_index(context.run_id)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO workflow_steps
            (id, run_id, step_index, node_name, action_type, tool_name,
             tool_input_json, tool_output_json, status, attempt_count, max_attempts,
             retryable, error_type, attempts_json, reasoning_summary, latency_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("step"),
                context.run_id,
                context.step_index,
                node_name,
                action_type,
                tool_name,
                json_dumps(tool_input),
                json_dumps(output),
                status,
                len(attempts),
                max_attempts,
                int(policy.retryable),
                error_type,
                json_dumps(attempts),
                reasoning_summary,
                latency_ms,
                utc_now(),
            ),
        )
    return output


def _has_step_error(output: dict | None) -> bool:
    return not isinstance(output, dict) or bool(output.get("error"))


def _tool_failure_message(tool_name: str, output: dict | None) -> str:
    if isinstance(output, dict):
        reason = output.get("error") or output.get("message") or "Tool returned an invalid payload."
    else:
        reason = "Tool returned an invalid payload."
    return f"{tool_name} failed: {reason}"


def _fail_workflow_from_tool_error(
    run_id: str,
    started: float,
    request_id: str | None,
    plan: dict | None,
    tool_name: str,
    output: dict | None,
) -> dict:
    final_answer = _tool_failure_message(tool_name, output)
    _complete_run(run_id, status="failed", final_answer=final_answer, started=started)
    if request_id:
        update_business_request_status(request_id, "failed", (plan or {}).get("category"))
    return get_run_detail(run_id)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _next_step_index(run_id: str) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(step_index), 0) + 1 AS next_index FROM workflow_steps WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    return int(row["next_index"])


def _coordinated_plan(plan_override: dict | None, risk_override: dict | None) -> dict | None:
    if not isinstance(plan_override, dict):
        return None
    required_fields = {
        "category",
        "priority",
        "risk_level",
        "needs_approval",
        "recommended_owner",
        "proposed_tools",
    }
    if not required_fields.issubset(plan_override):
        return None

    plan = dict(plan_override)
    if not isinstance(risk_override, dict):
        return plan

    risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    plan_risk = str(plan.get("risk_level") or "low")
    consensus_risk = str(risk_override.get("risk_level") or plan_risk)
    if risk_order.get(consensus_risk, 0) > risk_order.get(plan_risk, 0):
        plan["risk_level"] = consensus_risk
    if risk_override.get("needs_approval"):
        plan["needs_approval"] = True
        if "request_approval" not in plan["proposed_tools"]:
            plan["proposed_tools"] = [*plan["proposed_tools"], "request_approval"]
    plan["risk_consensus"] = {
        "decision": risk_override.get("decision"),
        "warnings": list(risk_override.get("warnings") or []),
        "consensus_rule": risk_override.get("consensus_rule"),
        "votes": list(risk_override.get("votes") or []),
    }
    return plan


def _update_run_plan(run_id: str, plan: dict) -> None:
    _set_run_classification(run_id, plan["category"], plan["risk_level"], bool(plan["needs_approval"]))


def _set_run_ticket(run_id: str, ticket_id: str | None) -> None:
    if not ticket_id:
        return
    with get_connection() as conn:
        conn.execute("UPDATE workflow_runs SET ticket_id = COALESCE(ticket_id, ?) WHERE id = ?", (ticket_id, run_id))


def _set_run_classification(run_id: str, category: str, risk_level: str, needs_approval: bool) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE workflow_runs
            SET category = ?, risk_level = ?, needs_approval = ?
            WHERE id = ?
            """,
            (category, risk_level, int(needs_approval), run_id),
        )


def _complete_run(
    run_id: str,
    *,
    status: str,
    final_answer: str,
    started: float | None,
    refusal_reason: str | None = None,
    needs_approval: bool | None = None,
) -> None:
    latency_ms = int((time.perf_counter() - started) * 1000) if started is not None else 0
    run_before = _get_run_row(run_id) or {}
    polished_answer = polish_agent_answer(
        final_answer,
        status=status,
        category=run_before.get("category"),
        risk_level=run_before.get("risk_level"),
    )
    if polished_answer:
        final_answer = polished_answer
    cost = estimate_token_cost(final_answer)
    completed_at = None if status == "waiting_approval" else utc_now()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE workflow_runs
            SET status = ?,
                final_answer = ?,
                refusal_reason = ?,
                needs_approval = COALESCE(?, needs_approval),
                cost_estimate = cost_estimate + ?,
                latency_ms = CASE WHEN ? > 0 THEN ? ELSE latency_ms END,
                completed_at = COALESCE(?, completed_at)
            WHERE id = ?
            """,
            (
                status,
                final_answer,
                refusal_reason,
                int(needs_approval) if needs_approval is not None else None,
                cost,
                latency_ms,
                latency_ms,
                completed_at,
                run_id,
            ),
        )
    run = _get_run_row(run_id) or run_before
    logger.info(
        "workflow.run_completed",
        extra={
            "event": "workflow.run_completed",
            "run_id": run_id,
            "tenant_id": (run or {}).get("tenant_id"),
            "status": status,
            "latency_ms": latency_ms,
            "estimated_cost": cost,
        },
    )
    record_audit(
        "workflow.complete",
        "workflow_run",
        run_id,
        {"status": status},
        tenant_id=(run or {}).get("tenant_id"),
    )


def _get_run_row(run_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
    return row_to_dict(row)


def _run_it_action_branch(
    context: WorkflowContext,
    run_id: str,
    started: float,
    request_id: str | None,
    tenant: str,
    plan: dict,
    coordination: dict,
    it_action: dict,
) -> dict:
    """Carry one gate-decided IT action through to its outcome.

    The gate has already run; this does not re-decide anything. It maps the
    three possible decisions onto three run endings:

    ``deny``
        Nothing runs and the ticket is rejected. The refusal is recorded by
        ``app.services.it.execution``, which is asked to execute and declines —
        so even the refusals travel the one code path that knows the risk rule.
    ``require_approval``
        An approval row is created and the ticket parks at ``waiting_approval``;
        the run ends ``waiting_approval``, which is what makes the graph
        interrupt further up. Note the approval payload carries ``ticket_id``
        and deliberately no ``proposed_ticket``, so the resume path closes out
        this existing ticket instead of creating a second one.
    ``auto_execute``
        The action runs and the ticket is resolved.
    """
    ticket_id = str(it_action.get("ticket_id") or "")
    action_type = str(it_action.get("action_type") or "")
    tool_name = it_action.get("tool_name")
    arguments = dict(it_action.get("arguments") or {})
    decision = dict(it_action.get("risk_decision") or {})
    requested_by = it_action.get("requested_by") or context.requester_user_id
    gate = decision.get("decision")
    _set_run_ticket(run_id, ticket_id)

    if gate == DECISION_REQUIRE_APPROVAL:
        approval_payload = {
            "objective": context.objective,
            "plan": plan,
            "ticket_id": ticket_id,
            "it_action": {key: value for key, value in it_action.items() if key != "resolution"},
            "it_resolution": it_action.get("resolution") or {},
            "it_risk_decision": decision,
            "knowledge": (context.artifacts.get("knowledge") or {}).get("results", [])[:10],
            "requester_user_id": context.requester_user_id,
            "requester_department": context.requester_department,
            "multi_agent_coordination": coordination,
        }
        approval = _run_step(
            context,
            "it_request_approval",
            "approval",
            "request_approval",
            approval_payload,
            lambda: create_approval(
                run_id,
                plan.get("approval_action") or action_type or "it_action",
                plan.get("approval_tool") or tool_name or "human_handoff",
                approval_payload,
                requested_by="agent",
                tenant_id=tenant,
            ),
            "Pause a production IT action for human approval; nothing runs until a human decides.",
        )
        if _has_step_error(approval) or not approval.get("id"):
            return _fail_workflow_from_tool_error(run_id, started, request_id, plan, "request_approval", approval)
        _run_step(
            context,
            "it_approval_ticket",
            "state_update",
            "update_ticket",
            {"ticket_id": ticket_id, "status": "waiting_approval", "approval_id": approval.get("id")},
            lambda: update_ticket(
                ticket_id,
                status="waiting_approval",
                comment=(
                    f"IT action {action_type} requires human approval "
                    f"({decision.get('rule_id')}). Awaiting reviewer."
                ),
                approval_id=approval.get("id"),
                agent_run_id=run_id,
                tenant_id=tenant,
            )
            or {"error": f"Ticket {ticket_id} was not found."},
            "Park the IT ticket at waiting_approval while the reviewer decides.",
        )
        record_audit(
            "it.approval_requested",
            "ticket",
            ticket_id,
            {
                "approval_id": approval.get("id"),
                "action_type": action_type,
                "tool_name": tool_name,
                "environment": decision.get("environment"),
                "risk_rule_id": decision.get("rule_id"),
                "reasons": list(decision.get("reasons") or []),
                "workflow_run_id": run_id,
                "multi_agent_run_id": it_action.get("multi_agent_run_id"),
                "requested_by": requested_by,
            },
            actor=requested_by or "agent",
        )
        _complete_run(
            run_id,
            status="waiting_approval",
            final_answer=_it_action_answer(
                "waiting_approval", action_type, ticket_id, decision
            ),
            started=started,
            needs_approval=True,
        )
        if request_id:
            update_business_request_status(request_id, "waiting_approval", plan["category"])
        return get_run_detail(run_id)

    execution = _run_step(
        context,
        "it_action_execute" if gate != DECISION_DENY else "it_action_denied",
        "it_operation",
        tool_name,
        {**arguments, "ticket_id": ticket_id},
        lambda: execute_it_action(
            ticket_id=ticket_id,
            action_type=action_type,
            tool_name=tool_name,
            arguments=arguments,
            risk_decision=decision,
            requested_by=requested_by,
            workflow_run_id=run_id,
            multi_agent_run_id=it_action.get("multi_agent_run_id"),
            # The execution this run already completed, if the correction loop
            # routed back here. Restarting a service twice is not a retry.
            prior_execution=it_action.get("prior_execution"),
        ),
        (
            "Execute the IT action the deterministic risk gate cleared automatically."
            if gate != DECISION_DENY
            else "Ask the executor to run a denied IT action so the refusal is recorded against the risk rule."
        ),
    )

    if execution.get("reason") == NOT_EXECUTED_ALREADY_EXECUTED:
        # The correction loop brought this run back through execution and the
        # action was short-circuited because it had already run. Nothing failed
        # and nothing changed, so the ticket keeps the state that execution
        # gave it — rejecting it here would report a completed action as a
        # refusal.
        _complete_run(
            run_id,
            status="completed",
            final_answer=_it_action_answer("completed", action_type, ticket_id, decision, execution),
            started=started,
        )
        if request_id:
            update_business_request_status(request_id, "completed", plan["category"])
        return get_run_detail(run_id)

    if gate == DECISION_DENY or execution.get("executed") is not True:
        # Denied, or cleared by the gate but un-runnable / failed. Either way
        # nothing changed on the target, and the ticket says so.
        _run_step(
            context,
            "it_action_reject_ticket",
            "state_update",
            "update_ticket",
            {"ticket_id": ticket_id, "status": "rejected"},
            lambda: update_ticket(
                ticket_id,
                status="rejected",
                comment=(
                    f"IT action {action_type} was not executed "
                    f"({execution.get('reason') or 'unknown'})."
                ),
                agent_run_id=run_id,
                tenant_id=tenant,
            )
            or {"error": f"Ticket {ticket_id} was not found."},
            "Record on the ticket that no IT action ran, and why.",
        )
        # A refusal the gate intended (deny) ends the run cancelled; a tool that
        # broke when it was allowed to run is a failure, and saying so is what
        # lets the critic and the operator tell the two apart.
        outcome = "failed" if execution.get("reason") == NOT_EXECUTED_TOOL_ERROR else "cancelled"
        _complete_run(
            run_id,
            status=outcome,
            final_answer=_it_action_answer("not_executed", action_type, ticket_id, decision, execution),
            started=started,
        )
        if request_id:
            update_business_request_status(request_id, outcome, plan["category"])
        return get_run_detail(run_id)

    _run_step(
        context,
        "it_action_finalize",
        "state_update",
        "update_ticket",
        {"ticket_id": ticket_id, "status": "resolved"},
        lambda: update_ticket(
            ticket_id,
            status="resolved",
            comment=f"IT action {action_type} executed automatically; ticket resolved.",
            agent_run_id=run_id,
            tenant_id=tenant,
        )
        or {"error": f"Ticket {ticket_id} was not found."},
        "Resolve the IT ticket once the cleared action has run.",
    )
    _complete_run(
        run_id,
        status="completed",
        final_answer=_it_action_answer("completed", action_type, ticket_id, decision, execution),
        started=started,
    )
    if request_id:
        update_business_request_status(request_id, "completed", plan["category"])
    return get_run_detail(run_id)


def _it_action_answer(
    outcome: str,
    action_type: str,
    ticket_id: str,
    decision: dict,
    execution: dict | None = None,
) -> str:
    """The human-readable run answer. States what ran and what did not."""
    rule = decision.get("rule_id") or "unknown_rule"
    if outcome == "waiting_approval":
        return (
            f"IT 动作 {action_type or '-'} 需要人工审批（风险规则：{rule}），"
            f"工单 {ticket_id} 已进入等待审批；在审批通过前不会执行任何操作。"
        )
    if outcome == "not_executed":
        reason = (execution or {}).get("reason") or decision.get("decision")
        return (
            f"IT 动作 {action_type or '-'} 未执行（原因：{reason}；风险规则：{rule}），"
            f"工单 {ticket_id} 已标记为 rejected，目标资产未被改动。"
        )
    return (
        f"IT 动作 {action_type or '-'} 已自动执行（风险规则：{rule}），工单 {ticket_id} 已 resolved。"
    )


def _requires_customer_context(plan: dict, objective: str) -> bool:
    if plan.get("category") in {"refund", "complaint", "communication"}:
        return True
    return False


def _requires_internal_notification(plan: dict) -> bool:
    return plan.get("category") in {"security", "access_request", "incident"} or plan.get("workflow_type") in {
        "security_incident_response",
        "access_request_fulfillment",
        "incident_response",
    }


def _internal_notification_message(plan: dict, ticket: dict, objective: str) -> str:
    return (
        f"Agent 已创建 {plan.get('workflow_type', 'business')} 工单 {ticket['id']}。"
        f"风险等级：{plan.get('risk_level')}；负责人：{plan.get('recommended_owner')}；"
        f"禁止动作：{', '.join(plan.get('blocked_actions') or []) or '-'}。"
        f"原始请求：{compact_text(objective, 220)}"
    )


def _final_ticket_status(plan: dict, sent_email: dict | None = None) -> str:
    if sent_email:
        return "waiting_customer"
    return plan.get("final_ticket_status") or "open"


def _ticket_title(plan: dict, objective: str) -> str:
    return f"[{plan['category']}/{plan['risk_level']}] {compact_text(objective, 72)}"


def _proposed_ticket_payload(plan: dict, objective: str, knowledge: dict, customer: dict | None, run_id: str) -> dict:
    return {
        "title": _ticket_title(plan, objective),
        "description": _ticket_description(objective, plan, knowledge),
        "customer_id": (customer or {}).get("id"),
        "priority": plan["priority"],
        "owner_department": plan["recommended_owner"],
        "workflow_type": plan.get("workflow_type"),
        "category": plan.get("category"),
        "risk_level": plan.get("risk_level"),
        "approval_chain": plan.get("approval_chain") or [],
        "auto_actions": plan.get("auto_actions") or [],
        "blocked_actions": plan.get("blocked_actions") or [],
        "evidence": knowledge.get("results", [])[:10],
        "agent_run_id": run_id,
    }


def _effective_knowledge(rag_knowledge: dict, local_knowledge: dict) -> dict:
    rag_results = rag_knowledge.get("results", []) if rag_knowledge.get("available") else []
    local_results = local_knowledge.get("results", [])
    if rag_results:
        results = rag_results + local_results[: max(0, 3 - len(rag_results))]
        source = "enterprise_rag"
    else:
        results = local_results
        source = "local_policy_db"
    return {
        "source": source,
        "query": rag_knowledge.get("query") or local_knowledge.get("query"),
        "results": results,
        "rag": {
            "available": bool(rag_knowledge.get("available")),
            "can_answer": bool(rag_knowledge.get("can_answer")),
            "citation_count": len(rag_knowledge.get("citations", [])),
            "retrieved_chunk_count": len(rag_knowledge.get("retrieved_chunks", [])),
            "refusal_reason": rag_knowledge.get("refusal_reason"),
            "eval_reports_url": rag_knowledge.get("eval_reports_url"),
        },
        "local": {
            "available": bool(local_knowledge.get("available", True)),
            "result_count": len(local_results),
        },
    }


def _coordinated_knowledge(
    knowledge_override: dict | None,
    rag_knowledge: dict,
    local_knowledge: dict,
) -> dict:
    synthesized = list((knowledge_override or {}).get("evidence") or [])
    if not synthesized:
        return _effective_knowledge(rag_knowledge, local_knowledge)
    normalized = []
    for item in synthesized[:10]:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                **item,
                "title": str(item.get("title") or "Policy evidence"),
                "snippet": str(item.get("snippet") or item.get("content") or "")[:320],
                "source": str(item.get("source") or "research_agent"),
            }
        )
    effective = _effective_knowledge(rag_knowledge, local_knowledge)
    effective.update(
        {
            "source": str((knowledge_override or {}).get("selected_source") or effective["source"]),
            "results": normalized,
            "synthesis": {
                "reasoning_mode": (knowledge_override or {}).get("reasoning_mode"),
                "summary": (knowledge_override or {}).get("synthesis_summary"),
                "conflicts": list((knowledge_override or {}).get("conflicts") or []),
                "warnings": list((knowledge_override or {}).get("warnings") or []),
            },
        }
    )
    return effective


def _ticket_description(objective: str, plan: dict, knowledge: dict) -> str:
    evidence = "\n".join(f"- {item['title']}: {item['snippet']}" for item in knowledge.get("results", [])[:3])
    approval_chain = " -> ".join(plan.get("approval_chain") or []) or "无需人工审批"
    auto_actions = "\n".join(f"- {item}" for item in plan.get("auto_actions") or []) or "- 无"
    blocked_actions = "\n".join(f"- {item}" for item in plan.get("blocked_actions") or []) or "- 无"
    return (
        f"用户请求：{objective}\n\n"
        f"流程类型：{plan.get('workflow_type')}\n"
        f"分类/风险：{plan.get('category')} / {plan.get('risk_level')}\n"
        f"审批链：{approval_chain}\n\n"
        f"Agent 可自动执行：\n{auto_actions}\n\n"
        f"Agent 不会直接执行：\n{blocked_actions}\n\n"
        f"检索到的政策依据：\n{evidence or '- 未命中明确政策'}"
    )


def _build_email_payload(
    objective: str,
    plan: dict,
    customer: dict | None,
    ticket: dict,
    knowledge: dict,
) -> dict | None:
    if plan.get("category") not in {"refund", "complaint", "communication"}:
        return None
    recipient = plan.get("recipient_email")
    if not recipient and plan.get("category") in {"refund", "complaint", "communication"}:
        recipient = (customer or {}).get("email")
    if not recipient:
        return None
    policy_titles = "、".join(item["title"] for item in knowledge.get("results", [])[:2]) or "内部处理规范"
    subject = f"关于您的请求：{ticket['id']}"
    body = (
        f"您好，\n\n我们已收到并登记您的请求，工单编号为 {ticket['id']}。\n"
        f"当前系统将该事项识别为 {plan['category']}，风险等级为 {plan['risk_level']}。\n"
        f"处理依据包括：{policy_titles}。\n\n"
        "我们会继续跟进，并在需要人工审批时由负责人确认后再执行后续动作。\n\n"
        "谢谢。"
    )
    return {"to_address": recipient, "subject": subject, "body": body}


def _pending_approval_without_ticket_answer(plan: dict, approval: dict) -> str:
    chain = " -> ".join(plan.get("approval_chain") or []) or "负责人"
    return (
        f"已生成审批草案 {approval['id']}，当前等待 {chain} 确认。"
        "审批通过前不会创建外部工单、不会发送邮件，也不会执行高风险业务动作。"
    )


def _approval_denied_answer(reason: str | None, legacy_ticket_rejected: bool = False) -> str:
    if legacy_ticket_rejected:
        return f"审批已拒绝，旧流程中已创建的工单已标记为 rejected；未继续发送邮件或执行后续动作。原因：{reason or '未填写'}"
    return f"审批已拒绝，未创建外部工单、未发送邮件，也未执行高风险业务动作。原因：{reason or '未填写'}"


def _approval_created_ticket_answer(ticket: dict, sent_email: dict | None) -> str:
    ticket_ref = ticket.get("external_id") or ticket.get("id")
    answer = f"审批已通过，已创建外部工单 {ticket_ref} 并完成状态同步。"
    if sent_email:
        answer += f" 已发送客户邮件 {sent_email['id']}。"
    else:
        answer += " 本次流程不需要发送客户邮件，已保留工单、知识依据和审计记录。"
    return answer


def _approval_legacy_completed_answer(sent_email: dict | None) -> str:
    answer = "审批已通过，Agent 已继续执行暂停的业务动作。"
    if sent_email:
        answer += f" 已发送邮件 {sent_email['id']}。"
    return answer


def _completed_answer(plan: dict, ticket: dict, sent_email: dict | None) -> str:
    answer = (
        f"已完成 {plan.get('workflow_type')} 流程：创建工单 {ticket['id']}，"
        f"分类 {plan['category']}，最终状态 {_final_ticket_status(plan, sent_email)}。"
    )
    if sent_email:
        answer += f" 已发送客户邮件 {sent_email['id']}。"
    else:
        answer += " 本场景不需要直接发送客户邮件，已保留工单、知识依据和审计记录。"
    return answer
