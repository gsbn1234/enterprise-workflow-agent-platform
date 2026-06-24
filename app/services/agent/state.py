from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkflowContext:
    run_id: str
    objective: str
    request_id: str | None = None
    requester_user_id: str | None = None
    requester_department: str | None = None
    step_index: int = 0
    artifacts: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlanDecision:
    category: str
    priority: str
    risk_level: str
    needs_approval: bool
    amount: float | None
    recipient_email: str | None
    recommended_owner: str
    reason: str
    proposed_tools: list[str]
    workflow_type: str
    approval_chain: list[str]
    blocked_actions: list[str]
    auto_actions: list[str]
    final_ticket_status: str
    approval_action: str
