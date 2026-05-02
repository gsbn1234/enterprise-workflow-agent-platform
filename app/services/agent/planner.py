from __future__ import annotations

import re

from app.config import settings
from app.services.agent.state import PlanDecision


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
    category = _classify_category(objective)
    amount = _extract_amount(objective)
    recipient = _extract_email(objective)
    priority = _priority(objective, category)
    owner = _owner_department(category, priority)
    risk_level = _risk_level(category, amount, objective)
    needs_approval = _needs_approval(category, amount, recipient, risk_level)
    proposed_tools = ["search_knowledge", "create_ticket"]
    if recipient or any(term in objective.lower() for term in ["客户", "customer", "退款", "投诉"]):
        proposed_tools.insert(0, "lookup_customer")
        proposed_tools.append("draft_email")
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
        reason=_reason(category, amount, risk_level, needs_approval),
        proposed_tools=proposed_tools,
    )


def _classify_category(text: str) -> str:
    lowered = text.lower()
    if _contains(text, ["安全", "权限", "泄露", "异常登录", "账号", "security", "access leak"]):
        return "security"
    if _contains(text, ["故障", "宕机", "无法访问", "p1", "p0", "incident", "outage", "down"]):
        return "incident"
    if _contains(text, ["采购", "购买", "订阅", "预算", "云资源", "procurement", "purchase"]):
        return "procurement"
    if _contains(text, ["退款", "退费", "赔偿", "refund", "compensation"]):
        return "refund"
    if _contains(text, ["投诉", "差评", "不满", "complaint"]):
        return "complaint"
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
    if category == "incident":
        return "SRE"
    if category == "procurement":
        return "Procurement"
    if category in {"refund", "complaint", "communication"}:
        return "Customer Success"
    if priority == "high":
        return "Operations"
    return "Business Ops"


def _risk_level(category: str, amount: float | None, text: str) -> str:
    if category == "security":
        return "high"
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
    if category == "procurement" and amount is not None and amount >= 1000:
        return True
    if category in {"refund", "security"}:
        return True
    if recipient and settings.external_email_requires_approval and category in {"refund", "security", "procurement"}:
        return True
    return False


def _reason(category: str, amount: float | None, risk_level: str, needs_approval: bool) -> str:
    amount_text = f"，识别金额 {amount:g}" if amount is not None else ""
    approval_text = "需要人工审批" if needs_approval else "可自动执行低风险动作"
    return f"分类为 {category}{amount_text}，风险等级 {risk_level}，{approval_text}。"
