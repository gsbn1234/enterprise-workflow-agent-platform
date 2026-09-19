from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class BusinessRequestCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1)
    requester_user_id: str | None = None
    requester_department: str | None = None
    tenant_id: str | None = None
    priority: str = "normal"


class AuthLoginRequest(BaseModel):
    user_id: str = Field(min_length=1)
    password: str = Field(min_length=1)


class AuthTokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: str
    user: dict[str, Any]


class UserCreate(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=120)
    department: str = Field(min_length=1, max_length=120)
    role: str = Field(pattern="^(admin|manager|employee|it_support|it_admin)$")
    password: str = Field(min_length=8)
    tenant_id: str | None = None


class WorkflowRunRequest(BaseModel):
    objective: str = Field(min_length=1)
    request_id: str | None = None
    requester_user_id: str | None = None
    requester_department: str | None = None
    requester_role: str | None = None
    tenant_id: str | None = None


class MultiAgentRunRequest(BaseModel):
    objective: str = Field(min_length=1)
    requester_user_id: str | None = None
    requester_department: str | None = None
    requester_role: str | None = None
    tenant_id: str | None = None
    enable_self_correction: bool = True
    max_correction_attempts: int = Field(default=1, ge=0, le=3)


class TraceReplayRequest(BaseModel):
    source_run_id: str = Field(min_length=1)


class GoldenTraceCreate(BaseModel):
    run_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=120)


class GoldenTraceDiffRequest(BaseModel):
    run_id: str = Field(min_length=1)
    golden_id: str | None = None
    baseline_run_id: str | None = None


class WorkflowJobCreate(BaseModel):
    objective: str = Field(min_length=1)
    request_id: str | None = None
    requester_user_id: str | None = None
    requester_department: str | None = None
    tenant_id: str | None = None
    max_attempts: int | None = Field(default=None, ge=1, le=10)


class ApprovalDecisionRequest(BaseModel):
    approved: bool
    decided_by: str = "manager"
    reason: str | None = None


class TicketOpsRequest(BaseModel):
    status: str | None = Field(
        default=None,
        pattern="^(open|investigating|waiting_approval|approved|rejected|waiting_customer|resolved|closed)$",
    )
    owner_department: str | None = Field(default=None, min_length=1, max_length=120)
    priority: str | None = Field(default=None, pattern="^(low|normal|high|urgent)$")
    comment: str | None = Field(default=None, max_length=5000)


class TicketCommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=5000)


class CustomerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)
    tier: str = Field(default="starter", pattern="^(starter|growth|enterprise|strategic)$")
    status: str = Field(default="active", pattern="^(active|onboarding|at_risk|inactive|churned)$")
    phone: str | None = Field(default=None, max_length=80)
    health_score: int = Field(default=100, ge=0, le=100)
    owner_department: str = Field(default="Customer Success", min_length=1, max_length=120)
    owner_user_id: str | None = Field(default=None, max_length=120)
    tags: list[str] = Field(default_factory=list, max_length=50)
    metadata: dict[str, Any] = Field(default_factory=dict)
    notes: str = Field(default="", max_length=10000)


class CustomerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = Field(default=None, min_length=3, max_length=320)
    tier: str | None = Field(default=None, pattern="^(starter|growth|enterprise|strategic)$")
    status: str | None = Field(default=None, pattern="^(active|onboarding|at_risk|inactive|churned)$")
    phone: str | None = Field(default=None, max_length=80)
    health_score: int | None = Field(default=None, ge=0, le=100)
    owner_department: str | None = Field(default=None, min_length=1, max_length=120)
    owner_user_id: str | None = Field(default=None, max_length=120)
    tags: list[str] | None = Field(default=None, max_length=50)
    metadata: dict[str, Any] | None = None
    notes: str | None = Field(default=None, max_length=10000)


class CustomerInteractionCreate(BaseModel):
    summary: str = Field(min_length=1, max_length=1000)
    interaction_type: str = Field(
        default="note",
        pattern="^(note|email|call|meeting|ticket|health_update)$",
    )
    channel: str = Field(default="internal", min_length=1, max_length=80)
    detail: dict[str, Any] = Field(default_factory=dict)


class KnowledgeArticleCreate(BaseModel):
    """No ``visibility`` field on purpose -- see ``create_article``.

    Unknown fields are ignored rather than rejected, so a client that still
    sends ``visibility`` keeps working; the value is simply not stored, which is
    the honest behaviour given that nothing filters on it.
    """

    title: str
    category: str
    content: str
    tags: str = ""


class ToolCallRequest(BaseModel):
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ITRequestCreate(BaseModel):
    """A free-text IT service request submitted through the Phase 1 intake loop."""

    objective: str = Field(min_length=1, max_length=2000)
    tenant_id: str | None = None


class ITResolveRequest(BaseModel):
    """Drive an existing IT ticket through the Phase 2 resolution loop.

    ``objective`` defaults to the ticket's own recorded request. Self-correction
    defaults to *off*: an IT action is not made more correct by replanning, and
    every extra pass through the graph is another chance to reach the execution
    branch.
    """

    objective: str | None = Field(default=None, min_length=1, max_length=2000)
    enable_self_correction: bool = False
    max_correction_attempts: int = Field(default=1, ge=1, le=5)
    tenant_id: str | None = None


class ApiMessage(BaseModel):
    message: str
