from __future__ import annotations

import logging
import re

from app.config import settings
from app.services.agent.state import PlanDecision
from app.services.agent.ticket_commands import parse_ticket_command
from app.services.llm import LlmOutcome, call_json, llm_ready
from app.services.llm_telemetry import record_llm_call


EMAIL_RE = re.compile(r"[\w.\-+]+@[\w.\-]+\.\w+")
ALLOWED_CATEGORIES = {
    "refund",
    "complaint",
    "communication",
    "security",
    "access_request",
    "remote_work",
    "procurement",
    "incident",
    "general",
}
ALLOWED_PRIORITIES = {"low", "normal", "high", "critical"}

logger = logging.getLogger("agent_platform.planner")
AMOUNT_RE = re.compile(r"(?:￥|¥|\$)?\s*(\d+(?:\.\d+)?)\s*(?:元|rmb|RMB|usd|USD|美元)?")


def guard_objective(objective: str) -> dict:
    lowered = objective.lower()
    blocked_terms = [
        "忽略之前",
        "绕过审批",
        "不要记录日志",
        "删除数据库",
        "ignore previous",
        "bypass approval",
        "without audit",
        "drop table",
    ]
    for term in blocked_terms:
        if term in lowered:
            return {
                "allowed": False,
                "reason": "prompt_injection_or_unsafe_instruction",
                "message": "该请求包含绕过审批、审计或破坏性操作的风险，Agent 已拒绝执行。",
            }
    return {"allowed": True, "reason": None, "message": "Request passed guard checks."}


def plan_workflow(objective: str) -> PlanDecision:
    ticket_command = parse_ticket_command(objective)
    if ticket_command:
        category = "ticket_update" if ticket_command.intent == "update" else "ticket_query"
        risk_level = "medium" if ticket_command.intent == "update" else "low"
        owner = ticket_command.owner_department or "Business Ops"
        return PlanDecision(
            category=category,
            priority=ticket_command.priority or "normal",
            risk_level=risk_level,
            needs_approval=False,
            amount=None,
            recipient_email=None,
            recommended_owner=owner,
            reason=f"Recognized a natural-language existing-ticket {ticket_command.intent} request. Scope and permissions are enforced by the ticket tool.",
            proposed_tools=["update_ticket" if ticket_command.intent == "update" else "query_tickets"],
            workflow_type="existing_ticket_update" if ticket_command.intent == "update" else "existing_ticket_query",
            approval_chain=[],
            blocked_actions=["modify_tickets_outside_role_scope", "change_audit_history"],
            auto_actions=[
                "parse_ticket_command",
                "apply_role_scope",
                "update_existing_ticket" if ticket_command.intent == "update" else "query_existing_tickets",
            ],
            final_ticket_status=ticket_command.status or "unchanged",
            approval_action="none",
        )
    llm_plan, llm_outcome = _llm_plan(objective)
    if llm_plan:
        return llm_plan
    category = _classify_category(objective)
    amount = _extract_amount(objective)
    recipient = _extract_email(objective)
    priority = _priority(objective, category)
    owner = _owner_department(category, priority)
    risk_level = _risk_level(category, amount, objective)
    needs_approval = _needs_approval(category, amount, recipient, risk_level)
    policy = _workflow_policy(category, amount, recipient, risk_level)
    proposed_tools = ["query_enterprise_rag", "search_knowledge", "create_ticket"]
    if category in {"refund", "complaint", "communication"} and (
        recipient or any(term in objective.lower() for term in ["客户", "customer", "退款", "投诉"])
    ):
        proposed_tools.insert(0, "lookup_customer")
        proposed_tools.append("draft_email")
    if category in {"security", "access_request", "incident"}:
        proposed_tools.append("notify_internal_team")
    if needs_approval:
        proposed_tools.append("request_approval")
    elif recipient:
        proposed_tools.append("send_email")

    return PlanDecision(
        category=category,
        priority=priority,
        risk_level=risk_level,
        needs_approval=needs_approval,
        amount=amount,
        recipient_email=recipient,
        recommended_owner=owner,
        reason=_reason(
            category, amount, risk_level, needs_approval, policy["workflow_type"],
            policy["approval_chain"], llm_note=_llm_failure_note(llm_outcome),
        ),
        proposed_tools=proposed_tools,
        workflow_type=policy["workflow_type"],
        approval_chain=policy["approval_chain"],
        blocked_actions=policy["blocked_actions"],
        auto_actions=policy["auto_actions"],
        final_ticket_status=policy["final_ticket_status"],
        approval_action=policy["approval_action"],
    )


def _llm_failure_note(outcome: LlmOutcome) -> str:
    """One clause saying the planner's model was asked and did not answer.

    Before Phase 5-1 a failed planner call left no trace in the plan at all:
    ``except LLMError: return None`` meant "the LLM is switched off" and "the
    LLM is timing out" produced byte-identical plans, and the operator reading
    one had no way to tell a configuration choice from an incident.

    Only the failure case adds text. The ``disabled`` case is the platform's
    default configuration and is already visible in ``llm_status()``; writing a
    note into every deterministic plan would change the reason string of every
    run that never wanted a model in the first place.
    """
    if not outcome.failed:
        return ""
    detail = f" ({outcome.error_type})" if outcome.error_type else ""
    return f"LLM planner attempted but returned {outcome.status}{detail}，已改用确定性分类。"


def _llm_plan(objective: str) -> tuple[PlanDecision | None, LlmOutcome]:
    """Ask the model to classify the request. Returns ``(plan, outcome)`` always.

    **Every** path returns the two-tuple, including the success path — the
    caller unpacks unconditionally, so a bare ``PlanDecision`` here would take
    the whole run down the moment a provider answered well enough to be
    trusted. That is precisely the failure this function's docstring exists to
    prevent, because it is invisible to every test that only makes the model
    fail: an unavailable provider exercises the ``return None, outcome`` lines
    and never reaches the end of this function.

    The outcome is returned even on success so ``plan_workflow`` can report
    what happened, and is returned on every failure path so the deterministic
    plan that follows can say *why* it is deterministic.

    Everything the model proposes is re-derived through the same safety
    functions the deterministic path uses: ``risk_level``, ``needs_approval``
    and the whole workflow policy are computed from the (validated) category,
    amount and recipient and never read from the answer.
    """
    if not settings.llm_planner_enabled or not llm_ready():
        return None, LlmOutcome(
            status="disabled",
            operation="planner_classification",
            provider=settings.llm_provider,
            model=settings.llm_model,
        )
    outcome = call_json(
        [
            {
                "role": "system",
                "content": (
                    "You classify user requests for a safe enterprise AI agent. "
                    "Return JSON only. Do not execute tools or approve actions. "
                    "Allowed categories: refund, complaint, communication, security, "
                    "access_request, remote_work, procurement, incident, general. "
                    "Allowed priorities: low, normal, high, critical. "
                    "Use null when amount or recipient_email is absent."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Request: {objective}\n\n"
                    "Return JSON with keys: category, priority, amount, recipient_email, "
                    "recommended_owner, confidence, explanation."
                ),
            },
        ],
        operation="planner_classification",
        temperature=0,
        max_tokens=450,
    )
    if outcome.failed:
        record_llm_call(outcome)
        logger.warning(
            "llm.planner_failed",
            extra={
                "event": "llm.planner_failed",
                "status": outcome.status,
                "error_type": outcome.error_type,
                "error": outcome.error_message,
                "latency_ms": outcome.latency_ms,
                "retry_count": outcome.retry_count,
            },
        )
        return None, outcome
    suggestion = outcome.value or {}

    confidence = _safe_float(suggestion.get("confidence"), 0.0)
    if confidence < settings.llm_planner_min_confidence:
        # A low-confidence answer is a refusal, not an error: the model was
        # asked, it answered, and it said it was not sure. Calling that a
        # failure would put an ``invalid_output`` on a call that worked.
        #
        # It is still a call whose answer was discarded, though, and that is
        # worth a row. ``fallback_used`` is what records it, and the row is
        # written from *this* outcome rather than the raw one so the table and
        # the plan tell the same story.
        withheld = LlmOutcome(
            status="success",
            operation=outcome.operation,
            provider=outcome.provider,
            model=outcome.model,
            value=suggestion,
            latency_ms=outcome.latency_ms,
            prompt_tokens=outcome.prompt_tokens,
            completion_tokens=outcome.completion_tokens,
            total_tokens=outcome.total_tokens,
            usage_available=outcome.usage_available,
            retry_count=outcome.retry_count,
            fallback_used=True,
            error_type="retained_deterministic_plan",
            error_message=(
                f"planner confidence {confidence:.2f} is below the "
                f"{settings.llm_planner_min_confidence:.2f} bar; deterministic plan used"
            ),
        )
        record_llm_call(withheld)
        return None, withheld

    record_llm_call(outcome)
    category = str(suggestion.get("category") or "").strip().lower()
    if category not in ALLOWED_CATEGORIES:
        category = _classify_category(objective)
    priority = str(suggestion.get("priority") or "").strip().lower()
    if priority not in ALLOWED_PRIORITIES:
        priority = _priority(objective, category)
    if priority == "critical":
        priority = "high"

    amount = _safe_amount(suggestion.get("amount"), _extract_amount(objective))
    recipient = _safe_email(suggestion.get("recipient_email")) or _extract_email(objective)
    owner = str(suggestion.get("recommended_owner") or "").strip() or _owner_department(category, priority)
    if len(owner) > 80:
        owner = _owner_department(category, priority)

    risk_level = _risk_level(category, amount, objective)
    needs_approval = _needs_approval(category, amount, recipient, risk_level)
    policy = _workflow_policy(category, amount, recipient, risk_level)
    proposed_tools = _proposed_tools(category, recipient, needs_approval)
    explanation = str(suggestion.get("explanation") or "").strip()
    deterministic_reason = _reason(category, amount, risk_level, needs_approval, policy["workflow_type"], policy["approval_chain"])
    reason = (
        f"LLM planner ({settings.llm_provider}/{settings.llm_model}, confidence={confidence:.2f}) "
        f"suggested {category}. {explanation} Safety policy: {deterministic_reason}"
    )

    return (
        PlanDecision(
            category=category,
            priority=priority,
            risk_level=risk_level,
            needs_approval=needs_approval,
            amount=amount,
            recipient_email=recipient,
            recommended_owner=owner,
            reason=reason,
            proposed_tools=proposed_tools,
            workflow_type=policy["workflow_type"],
            approval_chain=policy["approval_chain"],
            blocked_actions=policy["blocked_actions"],
            auto_actions=policy["auto_actions"],
            final_ticket_status=policy["final_ticket_status"],
            approval_action=policy["approval_action"],
        ),
        outcome,
    )


def _proposed_tools(category: str, recipient: str | None, needs_approval: bool) -> list[str]:
    proposed_tools = ["query_enterprise_rag", "search_knowledge", "create_ticket"]
    if category in {"refund", "complaint", "communication"}:
        proposed_tools.insert(0, "lookup_customer")
        proposed_tools.append("draft_email")
    if category in {"security", "access_request", "incident"}:
        proposed_tools.append("notify_internal_team")
    if needs_approval:
        proposed_tools.append("request_approval")
    elif recipient:
        proposed_tools.append("send_email")
    return proposed_tools


def _safe_float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_amount(value: object, default: float | None) -> float | None:
    if value is None or value == "":
        return default
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return default
    if amount < 0 or amount > 100000000:
        return default
    return amount


def _safe_email(value: object) -> str | None:
    if not value:
        return None
    match = EMAIL_RE.search(str(value))
    return match.group(0) if match else None


def _classify_category(text: str) -> str:
    lowered = text.lower()
    if _contains(text, ["泄露", "异常登录", "数据暴露", "安全事件", "security", "access leak", "data breach"]):
        return "security"
    if _contains(text, ["权限申请", "开通权限", "申请权限", "访问权限", "access request", "permission request", "grant access"]):
        return "access_request"
    if _contains(text, ["远程办公", "居家办公", "remote work", "work from home", "wfh"]):
        return "remote_work"
    if _contains(text, ["采购", "购买", "订阅", "预算", "云资源", "procurement", "purchase"]):
        return "procurement"
    if _contains(text, ["退款", "退费", "赔偿", "refund", "compensation"]):
        return "refund"
    if _contains(text, ["投诉", "差评", "不满", "complaint"]):
        return "complaint"
    if _contains(text, ["故障", "宕机", "无法访问", "p1", "p0", "incident", "outage", "down"]):
        return "incident"
    if "email" in lowered or "邮件" in text:
        return "communication"
    return "general"


def _contains(text: str, terms: list[str]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def _extract_email(text: str) -> str | None:
    match = EMAIL_RE.search(text)
    return match.group(0) if match else None


def _extract_amount(text: str) -> float | None:
    values = []
    for match in AMOUNT_RE.finditer(text):
        try:
            values.append(float(match.group(1)))
        except ValueError:
            continue
    if not values:
        return None
    return max(values)


def _priority(text: str, category: str) -> str:
    lowered = text.lower()
    if any(term in lowered for term in ["p0", "p1", "紧急", "urgent", "立刻", "马上"]):
        return "high"
    if category in {"security", "incident"}:
        return "high"
    return "normal"


def _owner_department(category: str, priority: str) -> str:
    if category == "security":
        return "Security"
    if category == "access_request":
        return "IT Access"
    if category == "incident":
        return "SRE"
    if category == "procurement":
        return "Procurement"
    if category == "remote_work":
        return "People Ops"
    if category in {"refund", "complaint", "communication"}:
        return "Customer Success"
    if priority == "high":
        return "Operations"
    return "Business Ops"


def _risk_level(category: str, amount: float | None, text: str) -> str:
    if category == "security":
        return "high"
    if category == "access_request":
        return "medium"
    if category == "incident" and _priority(text, category) == "high":
        return "high"
    if amount is not None and amount >= settings.approval_amount_threshold:
        return "high"
    if category in {"refund", "procurement", "complaint"}:
        return "medium"
    return "low"


def _needs_approval(category: str, amount: float | None, recipient: str | None, risk_level: str) -> bool:
    if risk_level == "high":
        return True
    if category == "access_request":
        return True
    if category == "procurement" and amount is not None and amount >= 1000:
        return True
    if category == "refund" and (amount is None or amount >= settings.approval_amount_threshold):
        return True
    if recipient and settings.external_email_requires_approval and category in {"refund", "security", "procurement"}:
        return True
    return False


def _workflow_policy(category: str, amount: float | None, recipient: str | None, risk_level: str) -> dict:
    if category == "security":
        return {
            "workflow_type": "security_incident_response",
            "approval_chain": ["Security Lead"],
            "blocked_actions": ["delete_data", "reset_permissions_without_owner", "close_audit"],
            "auto_actions": ["create_security_ticket", "notify_security_team", "preserve_audit"],
            "final_ticket_status": "investigating",
            "approval_action": "security_review",
        }
    if category == "access_request":
        return {
            "workflow_type": "access_request_fulfillment",
            "approval_chain": ["Line Manager", "IT Access"],
            "blocked_actions": ["grant_privileged_access_without_approval", "skip_identity_check"],
            "auto_actions": ["create_access_ticket", "notify_it_access", "preserve_audit"],
            "final_ticket_status": "approved",
            "approval_action": "access_approval",
        }
    if category == "procurement":
        chain = ["Procurement Manager"]
        if amount is not None and amount >= 5000:
            chain.append("Finance")
        return {
            "workflow_type": "procurement_request",
            "approval_chain": chain,
            "blocked_actions": ["place_order", "commit_budget", "skip_vendor_review"],
            "auto_actions": ["create_procurement_ticket", "attach_policy_evidence"],
            "final_ticket_status": "approved" if risk_level == "high" else "open",
            "approval_action": "procurement_approval",
        }
    if category == "refund":
        return {
            "workflow_type": "customer_refund_case",
            "approval_chain": ["Customer Success Manager", "Finance"] if amount and amount >= settings.approval_amount_threshold else ["Customer Success Manager"],
            "blocked_actions": ["promise_refund_without_approval", "send_external_email_before_approval"],
            "auto_actions": ["lookup_customer", "create_refund_ticket", "draft_customer_reply"],
            "final_ticket_status": "waiting_customer" if recipient else "approved",
            "approval_action": "refund_approval",
        }
    if category == "remote_work":
        return {
            "workflow_type": "remote_work_request",
            "approval_chain": [],
            "blocked_actions": ["change_payroll", "grant_system_access"],
            "auto_actions": ["check_remote_work_policy", "create_hr_record", "mark_policy_compliant"],
            "final_ticket_status": "approved",
            "approval_action": "none",
        }
    if category == "incident":
        return {
            "workflow_type": "incident_response",
            "approval_chain": ["Incident Commander"] if risk_level == "high" else [],
            "blocked_actions": ["close_p1_without_review", "suppress_customer_impact"],
            "auto_actions": ["create_incident_ticket", "attach_sla_evidence"],
            "final_ticket_status": "investigating",
            "approval_action": "incident_review",
        }
    return {
        "workflow_type": "general_business_request",
        "approval_chain": [],
        "blocked_actions": ["perform_irreversible_action_without_review"],
        "auto_actions": ["create_business_ticket", "attach_policy_evidence"],
        "final_ticket_status": "open",
        "approval_action": "none",
    }


def _reason(
    category: str,
    amount: float | None,
    risk_level: str,
    needs_approval: bool,
    workflow_type: str,
    approval_chain: list[str],
    *,
    llm_note: str = "",
) -> str:
    amount_text = f"，识别金额 {amount:g}" if amount is not None else ""
    approval_text = f"需要 {' -> '.join(approval_chain)} 审批" if needs_approval and approval_chain else "可自动执行低风险动作"
    return (
        f"分类为 {category}，流程为 {workflow_type}{amount_text}，"
        f"风险等级 {risk_level}，{approval_text}。{llm_note}"
    )
