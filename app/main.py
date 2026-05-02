from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import BASE_DIR, settings
from app.db import init_db, reset_database
from app.schemas import (
    ApprovalDecisionRequest,
    AuthLoginRequest,
    AuthTokenOut,
    BusinessRequestCreate,
    GoldenTraceCreate,
    GoldenTraceDiffRequest,
    KnowledgeArticleCreate,
    MultiAgentRunRequest,
    ToolCallRequest,
    TraceReplayRequest,
    UserCreate,
    WorkflowJobCreate,
    WorkflowRunRequest,
)
from app.services.agent import decide_approval_and_resume, get_run_detail, list_runs, run_workflow
from app.services.audit import list_audit_logs
from app.services.auth import (
    AuthContext,
    AuthError,
    auth_context_from_authorization,
    authenticate_user,
    create_user,
    ensure_demo_users,
    list_users,
)
from app.services.eval_reports import list_eval_reports
from app.services.jobs import create_workflow_job, get_workflow_job, list_workflow_jobs, process_next_job, retry_workflow_job
from app.services.metrics import metrics_summary
from app.services.multi_agent import (
    diff_trace,
    export_multi_agent_trace,
    get_multi_agent_run,
    list_agent_checkpoints,
    list_golden_traces,
    list_multi_agent_runs,
    list_trace_replays,
    replay_multi_agent_run,
    run_multi_agent,
    save_golden_trace,
)
from app.services.multi_agent.memory import list_memories
from app.services.requests import create_business_request, get_business_request, list_business_requests
from app.services.tools.approvals import list_approvals
from app.services.tools.crm import list_customers
from app.services.tools.email import list_emails
from app.services.tools.knowledge import create_article, list_articles, search_knowledge
from app.services.tools.registry import call_tool, list_tool_specs
from app.services.tools.ticketing import list_tickets


app = FastAPI(title=settings.app_name)
static_dir = BASE_DIR / "app" / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.on_event("startup")
def startup() -> None:
    init_db(seed=settings.auto_seed)
    ensure_demo_users()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "db_path": str(settings.db_path)}


def _optional_auth_context(authorization: str | None = Header(default=None)) -> AuthContext | None:
    try:
        return auth_context_from_authorization(authorization, required=settings.auth_required)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


def _required_auth_context(authorization: str | None = Header(default=None)) -> AuthContext:
    try:
        context = auth_context_from_authorization(authorization, required=True)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if not context:
        raise HTTPException(status_code=401, detail="Authorization header is required.")
    return context


def _ensure_admin_when_auth_required(auth_context: AuthContext | None) -> None:
    if settings.auth_required and (not auth_context or not auth_context.is_admin):
        raise HTTPException(status_code=403, detail="Admin role is required.")


def _ensure_approver_when_auth_required(auth_context: AuthContext | None) -> None:
    if settings.auth_required and (not auth_context or not auth_context.can_approve):
        raise HTTPException(status_code=403, detail="Manager or admin role is required.")


@app.post("/api/admin/seed")
def seed(reset: bool = Query(default=False), auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    _ensure_admin_when_auth_required(auth_context)
    if reset:
        reset_database(seed=True)
        ensure_demo_users()
        return {"message": "Database reset and demo data seeded."}
    init_db(seed=True)
    ensure_demo_users()
    return {"message": "Demo data is ready."}


@app.post("/api/auth/login", response_model=AuthTokenOut)
def login(request: AuthLoginRequest) -> dict:
    try:
        return authenticate_user(request.user_id, request.password)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@app.get("/api/auth/me")
def me(auth_context: AuthContext = Depends(_required_auth_context)) -> dict:
    return {
        "id": auth_context.user_id,
        "display_name": auth_context.display_name,
        "department": auth_context.department,
        "role": auth_context.role,
    }


@app.get("/api/users")
def users(limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    _ensure_admin_when_auth_required(auth_context)
    return list_users(limit)


@app.post("/api/users")
def add_user(request: UserCreate, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    _ensure_admin_when_auth_required(auth_context)
    return create_user(request.id, request.display_name, request.department, request.role, request.password)


@app.post("/api/requests")
def create_request(request: BusinessRequestCreate, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    return create_business_request(
        request.title,
        request.description,
        requester_user_id=auth_context.user_id if auth_context else request.requester_user_id,
        requester_department=auth_context.department if auth_context else request.requester_department,
        priority=request.priority,
    )


@app.get("/api/requests")
def requests(limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    return list_business_requests(limit)


@app.post("/api/requests/{request_id}/run")
def run_request(request_id: str, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    request = get_business_request(request_id)
    if not request:
        raise HTTPException(status_code=404, detail="Business request not found.")
    objective = f"{request['title']}\n\n{request['description']}"
    return run_workflow(
        objective,
        request_id=request_id,
        requester_user_id=auth_context.user_id if auth_context else request.get("requester_user_id"),
        requester_department=auth_context.department if auth_context else request.get("requester_department"),
    )


@app.post("/api/requests/{request_id}/enqueue")
def enqueue_request(request_id: str, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    request = get_business_request(request_id)
    if not request:
        raise HTTPException(status_code=404, detail="Business request not found.")
    objective = f"{request['title']}\n\n{request['description']}"
    return create_workflow_job(
        objective,
        request_id=request_id,
        requester_user_id=auth_context.user_id if auth_context else request.get("requester_user_id"),
        requester_department=auth_context.department if auth_context else request.get("requester_department"),
    )


@app.post("/api/workflow/run")
def run_direct(request: WorkflowRunRequest, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    return run_workflow(
        request.objective,
        request_id=request.request_id,
        requester_user_id=auth_context.user_id if auth_context else request.requester_user_id,
        requester_department=auth_context.department if auth_context else request.requester_department,
    )


@app.post("/api/multi-agent/run")
def run_multi_agent_direct(
    request: MultiAgentRunRequest,
    auth_context: AuthContext | None = Depends(_optional_auth_context),
) -> dict:
    return run_multi_agent(
        request.objective,
        requester_user_id=auth_context.user_id if auth_context else request.requester_user_id,
        requester_department=auth_context.department if auth_context else request.requester_department,
        requester_role=auth_context.role if auth_context else request.requester_role,
        enable_self_correction=request.enable_self_correction,
        max_correction_attempts=request.max_correction_attempts,
    )


@app.post("/api/workflow/jobs")
def enqueue_direct(request: WorkflowJobCreate, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    return create_workflow_job(
        request.objective,
        request_id=request.request_id,
        requester_user_id=auth_context.user_id if auth_context else request.requester_user_id,
        requester_department=auth_context.department if auth_context else request.requester_department,
        max_attempts=request.max_attempts,
    )


@app.get("/api/jobs")
def jobs(status: str | None = None, limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    return list_workflow_jobs(status=status, limit=limit)


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    job = get_workflow_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Workflow job not found.")
    return job


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    _ensure_admin_when_auth_required(auth_context)
    job = retry_workflow_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Workflow job not found.")
    return job


@app.post("/api/jobs/run-next")
def run_next_job(auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    _ensure_admin_when_auth_required(auth_context)
    job = process_next_job(worker_id="api-manual")
    if not job:
        return {"message": "No queued job."}
    return job


@app.get("/api/runs")
def runs(limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    return list_runs(limit)


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    run = get_run_detail(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Workflow run not found.")
    return run


@app.get("/api/multi-agent/runs")
def multi_agent_runs(limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    return list_multi_agent_runs(limit)


@app.get("/api/multi-agent/runs/{run_id}")
def multi_agent_run_detail(run_id: str, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    run = get_multi_agent_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Multi-agent run not found.")
    return run


@app.get("/api/multi-agent/runs/{run_id}/checkpoints")
def multi_agent_run_checkpoints(run_id: str, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    run = get_multi_agent_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Multi-agent run not found.")
    return list_agent_checkpoints(run_id)


@app.get("/api/multi-agent/runs/{run_id}/trace")
def multi_agent_run_trace(run_id: str, include_payloads: bool = False, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    trace = export_multi_agent_trace(run_id, include_payloads=include_payloads)
    if not trace:
        raise HTTPException(status_code=404, detail="Multi-agent run not found.")
    return trace


@app.get("/api/agent-memory")
def agent_memory(limit: int = 100, memory_type: str | None = None, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    return list_memories(limit=limit, memory_type=memory_type)


@app.post("/api/trace-replay")
def trace_replay(request: TraceReplayRequest, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    try:
        return replay_multi_agent_run(request.source_run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/trace-replays")
def trace_replays(limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    return list_trace_replays(limit)


@app.post("/api/golden-traces")
def create_golden_trace(request: GoldenTraceCreate, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    try:
        return save_golden_trace(request.run_id, request.name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/golden-traces")
def golden_traces(limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    return list_golden_traces(limit)


@app.post("/api/golden-traces/diff")
def golden_trace_diff(request: GoldenTraceDiffRequest, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    try:
        return diff_trace(request.run_id, golden_id=request.golden_id, baseline_run_id=request.baseline_run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/approvals")
def approvals(status: str | None = None, limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    _ensure_approver_when_auth_required(auth_context)
    return list_approvals(status=status, limit=limit)


@app.post("/api/approvals/{approval_id}/decide")
def decide_approval(
    approval_id: str,
    request: ApprovalDecisionRequest,
    auth_context: AuthContext | None = Depends(_optional_auth_context),
) -> dict:
    _ensure_approver_when_auth_required(auth_context)
    run = decide_approval_and_resume(
        approval_id,
        approved=request.approved,
        decided_by=auth_context.user_id if auth_context else request.decided_by,
        reason=request.reason,
    )
    if not run:
        raise HTTPException(status_code=404, detail="Approval not found.")
    return run


@app.get("/api/knowledge")
def knowledge(limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    return list_articles(limit)


@app.post("/api/knowledge")
def add_knowledge(article: KnowledgeArticleCreate, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    _ensure_admin_when_auth_required(auth_context)
    return create_article(article.title, article.category, article.content, article.tags, article.visibility)


@app.get("/api/knowledge/search")
def search_policy(q: str, limit: int = 3, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    return search_knowledge(q, limit)


@app.get("/api/customers")
def customers(limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    return list_customers(limit)


@app.get("/api/tickets")
def tickets(limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    return list_tickets(limit)


@app.get("/api/emails")
def emails(limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    return list_emails(limit)


@app.get("/api/audit-logs")
def audit_logs(limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    _ensure_admin_when_auth_required(auth_context)
    return list_audit_logs(limit)


@app.get("/api/metrics/summary")
def metrics(auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    return metrics_summary()


@app.get("/api/eval-reports")
def eval_reports(limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    _ensure_admin_when_auth_required(auth_context)
    return list_eval_reports(limit)


@app.get("/api/mcp/tools")
def mcp_tools(auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    return {
        "note": "MCP-style HTTP manifest for demos. Use scripts/mcp_stdio_server.py for a stdio JSON-RPC bridge.",
        "tools": list_tool_specs(),
    }


@app.post("/api/mcp/call")
def mcp_call(request: ToolCallRequest, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    _ensure_admin_when_auth_required(auth_context)
    result = call_tool(
        request.tool_name,
        request.arguments,
        actor=auth_context.user_id if auth_context else "mcp-http",
        source="http",
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result)
    return {"tool_name": request.tool_name, "result": result}
