from __future__ import annotations

import re

from app.config import settings
from app.services.agent.state import PlanDecision
from app.services.agent.ticket_commands import parse_ticket_command


EMAIL_RE = re.compile(r"[\w.\-+]+@[\w.\-]+\.\w+")
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
        reason=_reason(category, amount, risk_level, needs_approval, policy["workflow_type"], policy["approval_chain"]),
        proposed_tools=proposed_tools,
        workflow_type=policy["workflow_type"],
        approval_chain=policy["approval_chain"],
        blocked_actions=policy["blocked_actions"],
        auto_actions=policy["auto_actions"],
        final_ticket_status=policy["final_ticket_status"],
        approval_action=policy["approval_action"],
    )


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


def _reason(category: str, amount: float | None, risk_level: str, needs_approval: bool, workflow_type: str, approval_chain: list[str]) -> str:
    amount_text = f"，识别金额 {amount:g}" if amount is not None else ""
    approval_text = f"需要 {' -> '.join(approval_chain)} 审批" if needs_approval and approval_chain else "可自动执行低风险动作"
    return f"分类为 {category}，流程为 {workflow_type}{amount_text}，风险等级 {risk_level}，{approval_text}。"
