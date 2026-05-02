from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class BusinessRequestCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1)
    requester_user_id: str | None = None
    requester_department: str | None = None
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


class WorkflowRunRequest(BaseModel):
    objective: str = Field(min_length=1)
    request_id: str | None = None
    requester_user_id: str | None = None
    requester_department: str | None = None


class MultiAgentRunRequest(BaseModel):
    objective: str = Field(min_length=1)
    requester_user_id: str | None = None
    requester_department: str | None = None
    requester_role: str | None = None
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
    max_attempts: int | None = Field(default=None, ge=1, le=10)


class ApprovalDecisionRequest(BaseModel):
    approved: bool
    decided_by: str = "manager"
    reason: str | None = None


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
