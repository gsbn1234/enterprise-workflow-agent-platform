from __future__ import annotations

import time
from typing import Any, Callable

from app.db import get_connection, row_to_dict, rows_to_dicts
from app.services.agent.planner import guard_objective, plan_workflow
from app.services.agent.retry import RetryPolicy, classify_error, default_retry_policy
from app.services.agent.state import WorkflowContext
from app.services.audit import record_audit
from app.services.requests import get_business_request, update_business_request_status
from app.services.tools.approvals import create_approval, get_approval, mark_approval
from app.services.tools.crm import lookup_customer
from app.services.tools.email import draft_email, send_email
from app.services.tools.knowledge import query_enterprise_rag, search_knowledge
from app.services.tools.ticketing import create_ticket, update_ticket
from app.utils import compact_text, estimate_token_cost, json_dumps, json_loads, new_id, utc_now


def run_workflow(
    objective: str,
    *,
    request_id: str | None = None,
    requester_user_id: str | None = None,
    requester_department: str | None = None,
) -> dict:
    started = time.perf_counter()
    run_id = new_id("run")
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
            (id, request_id, objective, status, created_at)
            VALUES (?, ?, ?, 'running', ?)
            """,
            (run_id, request_id, objective, now),
        )
    record_audit("workflow.start", "workflow_run", run_id, {"request_id": request_id}, actor=requester_user_id or "agent")

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

    plan = _run_step(
        context,
        "plan",
        "planner",
        None,
        {"objective": objective},
        lambda: plan_workflow(objective).__dict__,
        "Classify the request, estimate risk, and choose the tool path.",
    )
    context.artifacts["plan"] = plan
    _update_run_plan(run_id, plan)
    if request_id:
        update_business_request_status(request_id, "running", plan["category"])

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
            },
        },
        lambda: query_enterprise_rag(
            objective,
            top_k=3,
            user_id=requester_user_id,
            user_department=requester_department,
        ),
        "Query the enterprise RAG system as a first-class knowledge tool with user context.",
    )
    context.artifacts["rag_knowledge"] = rag_knowledge

    local_knowledge = _run_step(
        context,
        "retrieve_policy",
        "tool_call",
        "search_knowledge",
        {"query": objective, "limit": 3},
        lambda: search_knowledge(objective, limit=3),
        "Retrieve local policy evidence as fallback and comparison context for the workflow.",
    )
    knowledge = _effective_knowledge(rag_knowledge, local_knowledge)
    context.artifacts["knowledge"] = knowledge

    customer = _run_step(
        context,
        "collect_context",
        "tool_call",
        "lookup_customer",
        {"query": objective},
        lambda: lookup_customer(objective),
        "Look up customer context when the request may involve an external user or account.",
    )
    context.artifacts["customer"] = customer.get("customer")

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
        },
        lambda: create_ticket(
            _ticket_title(plan, objective),
            _ticket_description(objective, knowledge),
            customer_id=(customer.get("customer") or {}).get("id"),
            priority=plan["priority"],
            owner_department=plan["recommended_owner"],
        ),
        "Create an auditable work item so the agent action is connected to business operations.",
    )
    context.artifacts["ticket"] = ticket

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

    if plan["needs_approval"]:
        approval_payload = {
            "ticket_id": ticket["id"],
            "plan": plan,
            "email": context.artifacts.get("draft_email"),
            "knowledge": knowledge.get("results", []),
            "rag_citations": rag_knowledge.get("citations", []),
        }
        approval = _run_step(
            context,
            "request_approval",
            "approval",
            "request_approval",
            approval_payload,
            lambda: create_approval(
                run_id,
                "business_action",
                "send_email" if context.artifacts.get("draft_email") else "update_ticket",
                approval_payload,
                requested_by="agent",
            ),
            "Pause before risky execution and ask a human to approve the proposed action.",
        )
        final_answer = _pending_answer(plan, ticket, approval)
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

    sent_email = None
    if context.artifacts.get("draft_email"):
        sent_email = _run_step(
            context,
            "send_response",
            "tool_call",
            "send_email",
            context.artifacts["draft_email"],
            lambda: send_email(**context.artifacts["draft_email"]),
            "Send low-risk customer communication and keep an audit trail.",
        )
        context.artifacts["sent_email"] = sent_email

    _run_step(
        context,
        "finalize",
        "state_update",
        "update_ticket",
        {"ticket_id": ticket["id"], "status": "waiting_customer" if sent_email else "open"},
        lambda: update_ticket(ticket["id"], status="waiting_customer" if sent_email else "open"),
        "Move the ticket to the correct operational state after agent execution.",
    )
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
    context = WorkflowContext(run_id=run_id, objective=run["objective"])
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
        _complete_run(
            run_id,
            status="cancelled",
            final_answer=f"审批已拒绝，Agent 未执行风险动作。原因：{reason or '未填写'}",
            started=None,
        )
        return get_run_detail(run_id)

    payload = approval["payload"]
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
            ),
            "Human approval was granted; execute the previously paused email action.",
        )

    ticket_id = payload.get("ticket_id")
    if ticket_id:
        _run_step(
            context,
            "approval_update_ticket",
            "state_update",
            "update_ticket",
            {"ticket_id": ticket_id, "status": "waiting_customer" if sent_email else "approved"},
            lambda: update_ticket(ticket_id, status="waiting_customer" if sent_email else "approved"),
            "Update the operational ticket after approval execution.",
        )

    answer = "审批已通过，Agent 已继续执行暂停的业务动作。"
    if sent_email:
        answer += f" 已发送邮件 {sent_email['id']}。"
    _complete_run(run_id, status="completed", final_answer=answer, started=None)
    return get_run_detail(run_id)


def list_runs(limit: int = 100) -> list[dict]:
    with get_connection() as conn:
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


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _next_step_index(run_id: str) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(step_index), 0) + 1 AS next_index FROM workflow_steps WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    return int(row["next_index"])


def _update_run_plan(run_id: str, plan: dict) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE workflow_runs
            SET category = ?, risk_level = ?, needs_approval = ?
            WHERE id = ?
            """,
            (plan["category"], plan["risk_level"], int(plan["needs_approval"]), run_id),
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
    record_audit("workflow.complete", "workflow_run", run_id, {"status": status})


def _get_run_row(run_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
    return row_to_dict(row)


def _ticket_title(plan: dict, objective: str) -> str:
    return f"[{plan['category']}/{plan['risk_level']}] {compact_text(objective, 72)}"


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


def _ticket_description(objective: str, knowledge: dict) -> str:
    evidence = "\n".join(f"- {item['title']}: {item['snippet']}" for item in knowledge.get("results", [])[:3])
    return f"用户请求：{objective}\n\n检索到的政策依据：\n{evidence or '- 未命中明确政策'}"


def _build_email_payload(
    objective: str,
    plan: dict,
    customer: dict | None,
    ticket: dict,
    knowledge: dict,
) -> dict | None:
    recipient = plan.get("recipient_email") or (customer or {}).get("email")
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


def _pending_answer(plan: dict, ticket: dict, approval: dict) -> str:
    return (
        f"已创建工单 {ticket['id']}，分类 {plan['category']}，风险等级 {plan['risk_level']}。"
        f"由于该动作需要人工审批，已创建审批单 {approval['id']}，当前等待负责人确认。"
    )


def _completed_answer(plan: dict, ticket: dict, sent_email: dict | None) -> str:
    answer = f"已完成低风险自动化处理：创建工单 {ticket['id']}，分类 {plan['category']}。"
    if sent_email:
        answer += f" 已发送客户邮件 {sent_email['id']}。"
    else:
        answer += " 未检测到可直接发送的外部收件人，因此只保留工单和处理建议。"
    return answer
