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
    role: str = Field(pattern="^(admin|manager|employee)$")
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
    title: str
    category: str
    content: str
    tags: str = ""
    visibility: str = "internal"


class ToolCallRequest(BaseModel):
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ApiMessage(BaseModel):
    message: str
