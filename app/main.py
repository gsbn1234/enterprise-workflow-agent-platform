from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import BASE_DIR, settings
from app.db import clear_run_history, database_status, init_db, list_schema_migrations, reset_database, seed_it_data
from app.schemas import (
    ApprovalDecisionRequest,
    AuthLoginRequest,
    AuthTokenOut,
    BusinessRequestCreate,
    CustomerCreate,
    CustomerInteractionCreate,
    CustomerUpdate,
    GoldenTraceCreate,
    GoldenTraceDiffRequest,
    ITRequestCreate,
    ITResolveRequest,
    KnowledgeArticleCreate,
    MultiAgentRunRequest,
    TicketCommentCreate,
    TicketOpsRequest,
    ToolCallRequest,
    TraceReplayRequest,
    UserCreate,
    WorkflowJobCreate,
    WorkflowRunRequest,
)
from app.services.agent import decide_approval_and_resume, get_run_detail, list_runs, run_workflow
from app.services.audit import (
    list_audit_logs,
    list_it_audit_chain,
    record_audit,
    verify_audit_log_integrity,
)
from app.services.auth import (
    AuthContext,
    AuthError,
    auth_context_from_authorization,
    authenticate_user,
    create_user,
    ensure_demo_users,
    list_login_attempts,
    list_users,
    revoke_authorization_token,
)
from app.services.eval_reports import list_eval_reports
from app.services.jobs import create_workflow_job, get_workflow_job, list_workflow_jobs, process_next_job, retry_workflow_job
from app.services.llm import llm_status
from app.services.llm_telemetry import llm_usage_summary
from app.services.metrics import metrics_summary, prometheus_metrics
from app.services.multi_agent import (
    diff_trace,
    export_multi_agent_trace,
    find_multi_agent_run_by_it_ticket,
    get_multi_agent_run,
    list_agent_checkpoints,
    list_golden_traces,
    list_multi_agent_runs,
    list_trace_replays,
    replay_multi_agent_run,
    resume_multi_agent_for_workflow,
    run_multi_agent,
    save_golden_trace,
)
from app.services.multi_agent.memory import list_memories
from app.services.oidc import (
    OIDC_STATE_COOKIE,
    build_authorization_redirect,
    complete_authorization_code_login,
    oidc_status,
)
from app.services.observability import (
    configure_observability,
    observability_status,
    reset_log_context,
    set_log_context,
    start_span,
)
from app.services.operations import operations_dashboard, operations_status
from app.services.outbox import dispatch_due_outbox_events, list_outbox_events, retry_outbox_event, retry_outbox_events
from app.services.outbox_dispatcher import outbox_dispatcher_loop
from app.services.queue import queue_status
from app.services.retention import apply_retention, retention_plan
from app.services.requests import create_business_request, get_business_request, list_business_requests
from app.services.scim import (
    create_scim_user,
    disable_scim_user,
    get_scim_user,
    list_scim_users,
    patch_scim_user,
    replace_scim_user,
    scim_resource_types,
    scim_schemas,
    scim_service_provider_config,
)
from app.services.demo import list_demo_scenarios
from app.services.tools.approvals import get_approval, list_approvals
from app.services.tools.crm import (
    add_customer_interaction,
    create_customer,
    get_customer,
    list_customer_interactions,
    list_customers,
    update_customer,
)
from app.services.tools.email import list_emails
from app.services.tools.knowledge import create_article, list_articles, search_knowledge
from app.services.tools.registry import call_tool, list_tool_specs
from app.services.it.demo_scenarios import list_it_demo_scenarios
from app.services.it.intake import original_request_text, submit_it_request
from app.services.it.triage import classify
from app.services.tools.ticketing import (
    add_ticket_comment,
    get_ticket,
    list_ticket_events,
    list_tickets,
    query_tickets,
    update_ticket,
)
from app.services.tracing import build_trace_context
from app.services.tenancy import (
    effective_tenant_id,
    reset_current_tenant_id,
    reset_rls_bypass,
    set_current_tenant_id,
    set_rls_bypass,
    tenant_filter_enabled,
)
from app.utils import utc_now


app = FastAPI(title=settings.app_name)
configure_observability()
logger = logging.getLogger("agent_platform.http")

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "traceparent", "tracestate", "X-Trace-ID"],
    )

if settings.trusted_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.trusted_hosts))

static_dir = BASE_DIR / "app" / "static"
react_static_dir = static_dir / "react"
app.mount("/static", StaticFiles(directory=static_dir), name="static")
app.mount("/assets", StaticFiles(directory=react_static_dir / "assets"), name="react-assets")


@app.middleware("http")
async def operational_headers(request: Request, call_next):
    started = time.perf_counter()
    tenant_token = set_current_tenant_id(None)
    bypass_token = set_rls_bypass(False)
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    trace_context = build_trace_context(request.headers.get("traceparent")) if settings.traceparent_enabled else None
    log_token = set_log_context(
        request_id=request_id,
        trace_id=trace_context.trace_id if trace_context else None,
        span_id=trace_context.span_id if trace_context else None,
        parent_span_id=trace_context.parent_span_id if trace_context else None,
        http_method=request.method,
        http_path=request.url.path,
    )
    if trace_context:
        request.state.trace_id = trace_context.trace_id
        request.state.span_id = trace_context.span_id
        request.state.parent_span_id = trace_context.parent_span_id
    response = None
    try:
        with start_span(
            f"HTTP {request.method} {request.url.path}",
            {
                "http.method": request.method,
                "http.route": request.url.path,
                "http.request_id": request_id,
                "trace.source": trace_context.source if trace_context else "disabled",
            },
        ):
            response = await call_next(request)
    except Exception:
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.exception(
            "http.request_failed",
            extra={
                "event": "http.request_failed",
                "duration_ms": duration_ms,
                "http_status": 500,
                "trace_source": trace_context.source if trace_context else "disabled",
            },
        )
        raise
    else:
        duration_ms = int((time.perf_counter() - started) * 1000)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-Ms"] = str(duration_ms)
        if trace_context:
            response.headers["traceparent"] = trace_context.traceparent
            response.headers["X-Trace-ID"] = trace_context.trace_id
            response.headers["X-Span-ID"] = trace_context.span_id
        if settings.enable_security_headers:
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
            response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        logger.info(
            "http.request_completed",
            extra={
                "event": "http.request_completed",
                "duration_ms": duration_ms,
                "http_status": response.status_code,
                "trace_source": trace_context.source if trace_context else "disabled",
            },
        )
        return response
    finally:
        reset_rls_bypass(bypass_token)
        reset_current_tenant_id(tenant_token)
        reset_log_context(log_token)


@app.on_event("startup")
async def startup() -> None:
    if settings.auto_migrate:
        init_db(seed=settings.auto_seed)
        ensure_demo_users()
        # Mirrors ensure_demo_users: the mock IT directory is idempotent and is
        # guaranteed present on every boot, including when auto_seed is off.
        seed_it_data()
    if settings.embedded_outbox_dispatcher_enabled:
        app.state.outbox_dispatcher_task = asyncio.create_task(outbox_dispatcher_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    task = getattr(app.state, "outbox_dispatcher_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _frontend_entry() -> FileResponse:
    headers = {"Cache-Control": "no-cache, no-store, must-revalidate"}
    react_entry = react_static_dir / "index.html"
    if react_entry.exists():
        return FileResponse(react_entry, headers=headers)
    return FileResponse(static_dir / "index.html", headers=headers)


@app.get("/")
def index() -> FileResponse:
    return _frontend_entry()


@app.get("/admin")
def admin() -> FileResponse:
    return _frontend_entry()


@app.get("/login")
def login_page() -> FileResponse:
    return _frontend_entry()


@app.get("/showcase")
def showcase_page() -> FileResponse:
    """Phase 6 showcase. Relies on the frontend's hand-rolled router, and on the
    same ``_frontend_entry`` fallback as ``/`` and ``/admin`` -- there is no
    catch-all route, so a new client-side path needs a server-side twin."""
    return _frontend_entry()


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "app": settings.app_name,
        "db_backend": settings.db_backend,
        "db_path": str(settings.db_path),
        "postgres_schema": settings.postgres_schema if settings.db_backend == "postgres" else None,
        "auto_migrate": settings.auto_migrate,
        "auth_required": settings.auth_required,
        "tool_mode": settings.tool_mode,
        "ticket_provider": settings.ticket_provider or settings.tool_mode or "mock",
        "email_provider": settings.email_provider or settings.tool_mode or "mock",
        "rag": "connected" if settings.rag_base_url else "not_configured",
        "observability": {
            **observability_status(),
        },
        "queue": queue_status(check_connection=False),
        "oidc": oidc_status(),
        "llm": llm_status(),
    }


@app.get("/api/readiness")
def readiness() -> dict:
    db = database_status()
    status = "ok" if db.get("status") == "ok" else "error"
    return {
        "status": status,
        "app": settings.app_name,
        "database": db,
        "operations": operations_status(),
        "observability": observability_status(),
        "queue": queue_status(check_connection=True),
        "llm": llm_status(),
        "rag": "configured" if settings.rag_base_url else "not_configured",
    }


@app.get("/api/demo-scenarios")
def demo_scenarios() -> list[dict]:
    return list_demo_scenarios()


@app.get("/api/it/demo-scenarios")
def it_demo_scenarios() -> dict:
    """Scenarios for the Phase 6 showcase, projected from the committed IT
    evaluation suite rather than restated. Read-only static metadata, so it is
    anonymous like ``/api/demo-scenarios``; every call it advertises requires a
    token of its own.
    """
    return list_it_demo_scenarios()


@app.get("/api/events")
async def event_stream(
    token: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
) -> StreamingResponse:
    auth_context = _event_auth_context(authorization, token)

    async def generate():
        last_payload = ""
        while True:
            snapshot = _event_snapshot(auth_context)
            comparable_snapshot = {key: value for key, value in snapshot.items() if key != "server_time"}
            payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
            fingerprint = json.dumps(comparable_snapshot, ensure_ascii=False, separators=(",", ":"))
            if fingerprint != last_payload:
                yield f"event: snapshot\ndata: {payload}\n\n"
                last_payload = fingerprint
            else:
                yield f": heartbeat {snapshot['server_time']}\n\n"
            await asyncio.sleep(1.5)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _event_auth_context(authorization: str | None, token: str | None) -> AuthContext | None:
    if authorization:
        return _optional_auth_context(authorization)
    if token:
        return _optional_auth_context(f"Bearer {token}")
    if settings.auth_required:
        raise HTTPException(status_code=401, detail="Authorization token is required.")
    return None


def _event_snapshot(auth_context: AuthContext | None) -> dict:
    can_approve = bool(auth_context and auth_context.can_approve)
    is_admin = bool(auth_context and auth_context.is_admin)
    return {
        "server_time": utc_now(),
        "user": {
            "id": auth_context.user_id if auth_context else None,
            "role": auth_context.role if auth_context else "anonymous",
            "tenant_id": auth_context.tenant_id if auth_context else effective_tenant_id(),
            "can_approve": can_approve,
            "is_admin": is_admin,
        },
        "health": {
            "tool_mode": settings.tool_mode,
            "ticket_provider": settings.ticket_provider or settings.tool_mode or "mock",
            "email_provider": settings.email_provider or settings.tool_mode or "mock",
            "rag": "connected" if settings.rag_base_url else "not_configured",
            "llm": llm_status(),
        },
        "metrics": metrics_summary(_tenant_scope(auth_context)),
        "approvals": _compact_records(
            _scoped_approvals(status="pending", limit=10, auth_context=auth_context) if can_approve else [],
            ("id", "run_id", "tool_name", "status", "created_at"),
        ),
        "tickets": _compact_records(
            list_tickets(limit=6, tenant_id=_tenant_scope(auth_context)),
            ("id", "external_id", "status", "priority", "owner_department", "provider", "updated_at"),
        ),
        "emails": _compact_records(
            list_emails(limit=6, tenant_id=_tenant_scope(auth_context)),
            ("id", "to_address", "status", "provider", "sent_at", "created_at"),
        ),
        "runs": _compact_records(
            list_runs(limit=6, tenant_id=_tenant_scope(auth_context)),
            ("id", "status", "category", "risk_level", "needs_approval", "created_at", "completed_at"),
        ),
        "multi_agent_runs": _compact_records(
            list_multi_agent_runs(limit=6, tenant_id=_tenant_scope(auth_context)),
            ("id", "status", "workflow_run_id", "critic_score", "created_at", "completed_at"),
        ),
    }


def _compact_records(records: list[dict], fields: tuple[str, ...]) -> list[dict]:
    return [{field: record.get(field) for field in fields if field in record} for record in records]


def _optional_auth_context(authorization: str | None = Header(default=None)) -> AuthContext | None:
    try:
        context = auth_context_from_authorization(authorization, required=settings.auth_required)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if context:
        set_current_tenant_id(context.tenant_id)
    return context


def _required_auth_context(authorization: str | None = Header(default=None)) -> AuthContext:
    try:
        context = auth_context_from_authorization(authorization, required=True)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if not context:
        raise HTTPException(status_code=401, detail="Authorization header is required.")
    set_current_tenant_id(context.tenant_id)
    return context


@app.get("/metrics")
def metrics_endpoint(auth_context: AuthContext | None = Depends(_optional_auth_context)) -> Response:
    if not settings.metrics_enabled:
        raise HTTPException(status_code=404, detail="Metrics are disabled.")
    if settings.metrics_auth_required and not auth_context:
        raise HTTPException(status_code=401, detail="Metrics require authentication.")
    # Same scope helper as ``/api/metrics/summary``. The dependency above has
    # already resolved who is scraping; without this the identity was discarded
    # and an authenticated tenant read every tenant's counters.
    return Response(
        prometheus_metrics(_tenant_scope(auth_context)),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


def _ensure_admin_when_auth_required(auth_context: AuthContext | None) -> None:
    if settings.auth_required and (not auth_context or not auth_context.is_admin):
        raise HTTPException(status_code=403, detail="Admin role is required.")


def _ensure_approver_when_auth_required(auth_context: AuthContext | None) -> None:
    if settings.auth_required and (not auth_context or not auth_context.can_approve):
        raise HTTPException(status_code=403, detail="Manager or admin role is required.")


def _ensure_crm_access(auth_context: AuthContext | None, *, manage: bool = False) -> None:
    if not settings.auth_required:
        return
    if not auth_context:
        raise HTTPException(status_code=401, detail="Authentication is required.")
    if auth_context.is_admin:
        return
    if auth_context.department != "Customer Success":
        raise HTTPException(status_code=403, detail="CRM access is limited to Customer Success or administrators.")
    if manage and not auth_context.can_approve:
        raise HTTPException(status_code=403, detail="Customer Success manager or admin role is required.")


def _crm_tenant_scope(auth_context: AuthContext | None) -> str:
    return auth_context.tenant_id if auth_context else effective_tenant_id()


def _approval_visible_to_context(approval: dict, auth_context: AuthContext | None) -> bool:
    if not settings.auth_required or not auth_context:
        return True
    if not _row_visible_to_context(approval, auth_context):
        return False
    if auth_context.is_admin:
        return True
    if not auth_context.can_approve:
        return False
    return auth_context.department in _approval_departments(approval)


def _approval_departments(approval: dict) -> set[str]:
    payload = approval.get("payload") or {}
    plan = payload.get("plan") or {}
    departments: set[str] = set()
    for value in [plan.get("recommended_owner"), *(plan.get("approval_chain") or [])]:
        departments.update(_approval_label_departments(str(value or ""), payload))
    requester_department = payload.get("requester_department")
    if requester_department and "Line Manager" in (plan.get("approval_chain") or []):
        departments.add(str(requester_department))
    return departments or {"Operations"}


def _approval_label_departments(label: str, payload: dict | None = None) -> set[str]:
    mapping = {
        "Customer Success Manager": {"Customer Success"},
        "Customer Success": {"Customer Success"},
        "Finance": {"Finance"},
        "Security Lead": {"Security"},
        "Security": {"Security"},
        "Line Manager": {str((payload or {}).get("requester_department") or "Operations")},
        "IT Access": {"IT Access"},
        "Procurement Manager": {"Procurement"},
        "Procurement": {"Procurement"},
        "Incident Commander": {"SRE", "Operations"},
        "SRE": {"SRE"},
        "People Ops": {"People Ops"},
        "Operations": {"Operations"},
        "Business Ops": {"Business Ops"},
    }
    return mapping.get(label, {label} if label else set())


def _scoped_approvals(status: str | None, limit: int, auth_context: AuthContext | None) -> list[dict]:
    approvals = list_approvals(status=status, limit=max(limit, 100), tenant_id=_tenant_scope(auth_context))
    visible = [approval for approval in approvals if _approval_visible_to_context(approval, auth_context)]
    return visible[: max(1, min(limit, 500))]


def _ensure_can_decide_approval(approval_id: str, auth_context: AuthContext | None) -> dict:
    _ensure_approver_when_auth_required(auth_context)
    approval = get_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found.")
    if not _approval_visible_to_context(approval, auth_context):
        raise HTTPException(status_code=403, detail="This approval belongs to another department.")
    return approval


def _ensure_can_operate_ticket(ticket_id: str, auth_context: AuthContext | None) -> dict:
    if settings.auth_required and not auth_context:
        raise HTTPException(status_code=401, detail="Authentication is required.")
    ticket = get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    if not _row_visible_to_context(ticket, auth_context):
        raise HTTPException(status_code=404, detail="Ticket not found.")
    if settings.auth_required and auth_context and not auth_context.is_admin and ticket.get("owner_department") != auth_context.department:
        raise HTTPException(status_code=403, detail="This ticket belongs to another department.")
    return ticket


def _tenant_scope(auth_context: AuthContext | None) -> str | None:
    if tenant_filter_enabled() and auth_context:
        return auth_context.tenant_id
    return None


def _row_visible_to_context(row: dict | None, auth_context: AuthContext | None) -> bool:
    if not tenant_filter_enabled() or not auth_context or not row:
        return True
    return effective_tenant_id(row.get("tenant_id")) == auth_context.tenant_id


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


@app.post("/api/admin/clear-history")
def clear_history(auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    _ensure_admin_when_auth_required(auth_context)
    deleted = clear_run_history()
    init_db(seed=True)
    ensure_demo_users()
    return {"message": "Run history cleared.", "deleted": deleted}


@app.get("/api/admin/schema-migrations")
def schema_migrations(auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    _ensure_admin_when_auth_required(auth_context)
    return {"database": database_status(), "migrations": list_schema_migrations()}


@app.get("/api/admin/preflight")
def preflight(auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    _ensure_admin_when_auth_required(auth_context)
    return {
        "database": database_status(),
        "operations": operations_status(),
        "health": {
            "tool_mode": settings.tool_mode,
            "ticket_provider": settings.ticket_provider or settings.tool_mode or "mock",
            "email_provider": settings.email_provider or settings.tool_mode or "mock",
            "rag": "configured" if settings.rag_base_url else "not_configured",
            "oidc": oidc_status(),
        },
    }


@app.get("/api/admin/operations-dashboard")
def admin_operations_dashboard(auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    _ensure_admin_when_auth_required(auth_context)
    return operations_dashboard()


@app.get("/api/admin/external-outbox")
def external_outbox(
    status: str | None = None,
    limit: int = 100,
    auth_context: AuthContext | None = Depends(_optional_auth_context),
) -> list[dict]:
    _ensure_admin_when_auth_required(auth_context)
    return list_outbox_events(status=status, limit=limit, tenant_id=_tenant_scope(auth_context))


@app.post("/api/admin/external-outbox/{outbox_id}/retry")
def retry_external_outbox(outbox_id: str, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    _ensure_admin_when_auth_required(auth_context)
    try:
        return retry_outbox_event(outbox_id, tenant_id=_tenant_scope(auth_context))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/admin/external-outbox/retry-failed")
def retry_failed_external_outbox(
    limit: int = 20,
    auth_context: AuthContext | None = Depends(_optional_auth_context),
) -> dict:
    _ensure_admin_when_auth_required(auth_context)
    return retry_outbox_events(status="failed", limit=limit, tenant_id=_tenant_scope(auth_context))


@app.post("/api/admin/external-outbox/dispatch-due")
def dispatch_due_external_outbox(
    limit: int | None = None,
    auth_context: AuthContext | None = Depends(_optional_auth_context),
) -> dict:
    _ensure_admin_when_auth_required(auth_context)
    return dispatch_due_outbox_events(limit=limit, tenant_id=_tenant_scope(auth_context))


@app.get("/api/admin/retention/plan")
def admin_retention_plan(auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    _ensure_admin_when_auth_required(auth_context)
    return retention_plan()


@app.post("/api/admin/retention/apply")
def admin_apply_retention(auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    _ensure_admin_when_auth_required(auth_context)
    return apply_retention()


@app.get("/api/admin/security/login-attempts")
def security_login_attempts(
    user_id: str | None = None,
    limit: int = 100,
    auth_context: AuthContext | None = Depends(_optional_auth_context),
) -> list[dict]:
    _ensure_admin_when_auth_required(auth_context)
    return list_login_attempts(limit=limit, user_id=user_id)


@app.get("/api/admin/audit/integrity")
def audit_integrity(
    limit: int | None = None,
    auth_context: AuthContext | None = Depends(_optional_auth_context),
) -> dict:
    _ensure_admin_when_auth_required(auth_context)
    return verify_audit_log_integrity(limit=limit)


@app.post("/api/auth/login", response_model=AuthTokenOut)
def login(payload: AuthLoginRequest, request: Request) -> dict:
    try:
        return authenticate_user(
            payload.user_id,
            payload.password,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@app.get("/api/auth/oidc/start")
def oidc_login_start(request: Request, next: str = "/") -> RedirectResponse:
    try:
        redirect = build_authorization_redirect(next, str(request.base_url).rstrip("/"))
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    response = RedirectResponse(redirect["authorization_url"], status_code=302)
    response.set_cookie(
        OIDC_STATE_COOKIE,
        redirect["state_cookie"],
        max_age=redirect["state_max_age_seconds"],
        httponly=True,
        secure=request.url.scheme == "https" or settings.public_base_url.startswith("https://"),
        samesite="lax",
    )
    return response


@app.get("/api/auth/oidc/callback")
def oidc_login_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> HTMLResponse:
    if error:
        response = _oidc_callback_error_html(error_description or error)
        response.delete_cookie(OIDC_STATE_COOKIE)
        return response
    try:
        result = complete_authorization_code_login(
            code=code or "",
            state=state or "",
            state_cookie=request.cookies.get(OIDC_STATE_COOKIE),
            request_base_url=str(request.base_url).rstrip("/"),
        )
    except AuthError as exc:
        response = _oidc_callback_error_html(exc.message)
        response.delete_cookie(OIDC_STATE_COOKIE)
        return response
    response = _oidc_callback_success_html(result["access_token"], result["next"])
    response.delete_cookie(OIDC_STATE_COOKIE)
    return response


@app.get("/api/auth/me")
def me(auth_context: AuthContext = Depends(_required_auth_context)) -> dict:
    return {
        "id": auth_context.user_id,
        "display_name": auth_context.display_name,
        "department": auth_context.department,
        "role": auth_context.role,
        "tenant_id": auth_context.tenant_id,
    }


@app.post("/api/auth/logout")
def logout(
    authorization: str | None = Header(default=None),
    auth_context: AuthContext = Depends(_required_auth_context),
) -> dict:
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header is required.")
    _, _, token = authorization.partition(" ")
    revoke_authorization_token(token, actor=auth_context.user_id)
    return {"message": "Logged out."}


def _oidc_callback_success_html(access_token: str, next_path: str) -> HTMLResponse:
    token_json = json.dumps(access_token)
    next_json = json.dumps(next_path if next_path.startswith("/") else "/")
    return HTMLResponse(
        f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>SSO 登录完成</title></head>
<body>
<script>
sessionStorage.setItem("agent_access_token", {token_json});
window.location.replace({next_json});
</script>
<p>SSO 登录完成，正在返回系统。</p>
</body>
</html>""",
        headers={"Cache-Control": "no-store"},
    )


def _oidc_callback_error_html(message: str) -> HTMLResponse:
    message_json = json.dumps(message)
    return HTMLResponse(
        f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>SSO 登录失败</title></head>
<body>
<script>
sessionStorage.removeItem("agent_access_token");
window.location.replace("/login?oidc_error=" + encodeURIComponent({message_json}));
</script>
<p>SSO 登录失败。</p>
</body>
</html>""",
        status_code=400,
        headers={"Cache-Control": "no-store"},
    )


def _require_scim_token(authorization: str | None = Header(default=None)) -> None:
    if not settings.scim_enabled:
        raise HTTPException(status_code=404, detail="SCIM is disabled.")
    if not settings.scim_token:
        raise HTTPException(status_code=500, detail="SCIM token is not configured.")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or token != settings.scim_token:
        raise HTTPException(status_code=401, detail="SCIM bearer token is required.")


@app.get("/scim/v2/ServiceProviderConfig")
def scim_service_provider(_: None = Depends(_require_scim_token)) -> dict:
    return scim_service_provider_config()


@app.get("/scim/v2/ResourceTypes")
def scim_resource_type_list(_: None = Depends(_require_scim_token)) -> dict:
    return scim_resource_types()


@app.get("/scim/v2/Schemas")
def scim_schema_list(_: None = Depends(_require_scim_token)) -> dict:
    return scim_schemas()


@app.get("/scim/v2/Users")
def scim_user_list(
    startIndex: int = Query(default=1, ge=1),
    count: int = Query(default=100, ge=1, le=100),
    _: None = Depends(_require_scim_token),
) -> dict:
    return list_scim_users(start_index=startIndex, count=count)


@app.post("/scim/v2/Users")
def scim_user_create(payload: dict[str, Any], _: None = Depends(_require_scim_token)) -> JSONResponse:
    try:
        user = create_scim_user(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(user, status_code=201)


@app.get("/scim/v2/Users/{user_id}")
def scim_user_get(user_id: str, _: None = Depends(_require_scim_token)) -> dict:
    user = get_scim_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="SCIM user not found.")
    return user


@app.put("/scim/v2/Users/{user_id}")
def scim_user_replace(user_id: str, payload: dict[str, Any], _: None = Depends(_require_scim_token)) -> dict:
    try:
        return replace_scim_user(user_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/scim/v2/Users/{user_id}")
def scim_user_patch(user_id: str, payload: dict[str, Any], _: None = Depends(_require_scim_token)) -> dict:
    try:
        user = patch_scim_user(user_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not user:
        raise HTTPException(status_code=404, detail="SCIM user not found.")
    return user


@app.delete("/scim/v2/Users/{user_id}", status_code=204)
def scim_user_delete(user_id: str, _: None = Depends(_require_scim_token)) -> Response:
    user = disable_scim_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="SCIM user not found.")
    return Response(status_code=204)


@app.get("/api/users")
def users(limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    _ensure_admin_when_auth_required(auth_context)
    accounts = list_users(limit)
    if tenant_filter_enabled() and auth_context:
        return [account for account in accounts if effective_tenant_id(account.get("tenant_id")) == auth_context.tenant_id]
    return accounts


@app.post("/api/users")
def add_user(request: UserCreate, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    _ensure_admin_when_auth_required(auth_context)
    tenant_id = auth_context.tenant_id if tenant_filter_enabled() and auth_context else request.tenant_id
    return create_user(request.id, request.display_name, request.department, request.role, request.password, tenant_id=tenant_id)


@app.post("/api/requests")
def create_request(request: BusinessRequestCreate, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    return create_business_request(
        request.title,
        request.description,
        requester_user_id=auth_context.user_id if auth_context else request.requester_user_id,
        requester_department=auth_context.department if auth_context else request.requester_department,
        tenant_id=auth_context.tenant_id if auth_context else request.tenant_id,
        priority=request.priority,
    )


@app.get("/api/requests")
def requests(limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    return list_business_requests(limit, tenant_id=_tenant_scope(auth_context))


@app.post("/api/requests/{request_id}/run")
def run_request(request_id: str, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    request = get_business_request(request_id)
    if not request or not _row_visible_to_context(request, auth_context):
        raise HTTPException(status_code=404, detail="Business request not found.")
    objective = f"{request['title']}\n\n{request['description']}"
    return run_workflow(
        objective,
        request_id=request_id,
        requester_user_id=auth_context.user_id if auth_context else request.get("requester_user_id"),
        requester_department=auth_context.department if auth_context else request.get("requester_department"),
        requester_role=auth_context.role if auth_context else None,
        tenant_id=auth_context.tenant_id if auth_context else request.get("tenant_id"),
    )


@app.post("/api/requests/{request_id}/enqueue")
def enqueue_request(request_id: str, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    request = get_business_request(request_id)
    if not request or not _row_visible_to_context(request, auth_context):
        raise HTTPException(status_code=404, detail="Business request not found.")
    objective = f"{request['title']}\n\n{request['description']}"
    return create_workflow_job(
        objective,
        request_id=request_id,
        requester_user_id=auth_context.user_id if auth_context else request.get("requester_user_id"),
        requester_department=auth_context.department if auth_context else request.get("requester_department"),
        tenant_id=auth_context.tenant_id if auth_context else request.get("tenant_id"),
    )


@app.post("/api/workflow/run")
def run_direct(request: WorkflowRunRequest, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    return run_workflow(
        request.objective,
        request_id=request.request_id,
        requester_user_id=auth_context.user_id if auth_context else request.requester_user_id,
        requester_department=auth_context.department if auth_context else request.requester_department,
        requester_role=auth_context.role if auth_context else request.requester_role,
        tenant_id=auth_context.tenant_id if auth_context else request.tenant_id,
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
        tenant_id=auth_context.tenant_id if auth_context else request.tenant_id,
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
        tenant_id=auth_context.tenant_id if auth_context else request.tenant_id,
        max_attempts=request.max_attempts,
    )


@app.get("/api/jobs")
def jobs(status: str | None = None, limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    return list_workflow_jobs(status=status, limit=limit, tenant_id=_tenant_scope(auth_context))


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    job = get_workflow_job(job_id)
    if not job or not _row_visible_to_context(job, auth_context):
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
    return list_runs(limit, tenant_id=_tenant_scope(auth_context))


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    run = get_run_detail(run_id)
    if not run or not _row_visible_to_context(run, auth_context):
        raise HTTPException(status_code=404, detail="Workflow run not found.")
    return run


@app.get("/api/multi-agent/runs")
def multi_agent_runs(limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    return list_multi_agent_runs(limit, tenant_id=_tenant_scope(auth_context))


@app.get("/api/multi-agent/runs/{run_id}")
def multi_agent_run_detail(run_id: str, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    run = get_multi_agent_run(run_id)
    if not run or not _row_visible_to_context(run, auth_context):
        raise HTTPException(status_code=404, detail="Multi-agent run not found.")
    return run


@app.get("/api/multi-agent/runs/{run_id}/checkpoints")
def multi_agent_run_checkpoints(run_id: str, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    run = get_multi_agent_run(run_id)
    if not run or not _row_visible_to_context(run, auth_context):
        raise HTTPException(status_code=404, detail="Multi-agent run not found.")
    return list_agent_checkpoints(run_id)


@app.get("/api/multi-agent/runs/{run_id}/trace")
def multi_agent_run_trace(run_id: str, include_payloads: bool = False, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    trace = export_multi_agent_trace(run_id, include_payloads=include_payloads)
    if not trace or not _row_visible_to_context(trace, auth_context):
        raise HTTPException(status_code=404, detail="Multi-agent run not found.")
    return trace


@app.get("/api/agent-memory")
def agent_memory(limit: int = 100, memory_type: str | None = None, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    return list_memories(limit=limit, memory_type=memory_type, tenant_id=_tenant_scope(auth_context))


@app.post("/api/trace-replay")
def trace_replay(request: TraceReplayRequest, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    source = get_multi_agent_run(request.source_run_id)
    if not source or not _row_visible_to_context(source, auth_context):
        raise HTTPException(status_code=404, detail="Multi-agent run not found.")
    try:
        return replay_multi_agent_run(request.source_run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/trace-replays")
def trace_replays(limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    return list_trace_replays(limit, tenant_id=_tenant_scope(auth_context))


@app.post("/api/golden-traces")
def create_golden_trace(request: GoldenTraceCreate, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    run = get_multi_agent_run(request.run_id)
    if not run or not _row_visible_to_context(run, auth_context):
        raise HTTPException(status_code=404, detail="Multi-agent run not found.")
    try:
        return save_golden_trace(request.run_id, request.name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/golden-traces")
def golden_traces(limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    return list_golden_traces(limit, tenant_id=_tenant_scope(auth_context))


@app.post("/api/golden-traces/diff")
def golden_trace_diff(request: GoldenTraceDiffRequest, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    run = get_multi_agent_run(request.run_id)
    if not run or not _row_visible_to_context(run, auth_context):
        raise HTTPException(status_code=404, detail="Multi-agent run not found.")
    if request.baseline_run_id:
        baseline = get_multi_agent_run(request.baseline_run_id)
        if not baseline or not _row_visible_to_context(baseline, auth_context):
            raise HTTPException(status_code=404, detail="Baseline multi-agent run not found.")
    if request.golden_id and tenant_filter_enabled() and auth_context:
        allowed = {item["id"] for item in list_golden_traces(limit=500, tenant_id=auth_context.tenant_id)}
        if request.golden_id not in allowed:
            raise HTTPException(status_code=404, detail="Golden trace not found.")
    try:
        return diff_trace(request.run_id, golden_id=request.golden_id, baseline_run_id=request.baseline_run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/approvals")
def approvals(status: str | None = None, limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    _ensure_approver_when_auth_required(auth_context)
    return _scoped_approvals(status=status, limit=limit, auth_context=auth_context)


@app.post("/api/approvals/{approval_id}/decide")
def decide_approval(
    approval_id: str,
    request: ApprovalDecisionRequest,
    auth_context: AuthContext | None = Depends(_optional_auth_context),
) -> dict:
    _ensure_can_decide_approval(approval_id, auth_context)
    run = decide_approval_and_resume(
        approval_id,
        approved=request.approved,
        decided_by=auth_context.user_id if auth_context else request.decided_by,
        reason=request.reason,
    )
    if not run:
        raise HTTPException(status_code=404, detail="Approval not found.")
    resumed_multi_agent = None
    if run.get("status") != "waiting_approval":
        resumed_multi_agent = resume_multi_agent_for_workflow(run)
    if resumed_multi_agent:
        run["multi_agent_run"] = resumed_multi_agent
    return run


@app.get("/api/knowledge")
def knowledge(limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    return list_articles(limit)


@app.post("/api/knowledge")
def add_knowledge(article: KnowledgeArticleCreate, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    _ensure_admin_when_auth_required(auth_context)
    return create_article(article.title, article.category, article.content, article.tags)


@app.get("/api/knowledge/search")
def search_policy(q: str, limit: int = 3, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    return search_knowledge(q, limit)


@app.get("/api/customers")
def customers(
    q: str | None = None,
    status: str | None = None,
    tier: str | None = None,
    owner_department: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    auth_context: AuthContext | None = Depends(_optional_auth_context),
) -> list[dict]:
    _ensure_crm_access(auth_context)
    try:
        return list_customers(
            limit,
            tenant_id=_crm_tenant_scope(auth_context),
            q=q,
            status=status,
            tier=tier,
            owner_department=owner_department,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/customers", status_code=201)
def add_customer(
    request: CustomerCreate,
    auth_context: AuthContext | None = Depends(_optional_auth_context),
) -> dict:
    _ensure_crm_access(auth_context, manage=True)
    try:
        return create_customer(
            **request.model_dump(),
            tenant_id=_crm_tenant_scope(auth_context),
            actor=auth_context.user_id if auth_context else "crm-api",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/customers/{customer_id}")
def customer_detail(
    customer_id: str,
    auth_context: AuthContext | None = Depends(_optional_auth_context),
) -> dict:
    _ensure_crm_access(auth_context)
    customer = get_customer(customer_id, tenant_id=_crm_tenant_scope(auth_context))
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found.")
    customer["interactions"] = list_customer_interactions(
        customer_id,
        tenant_id=_crm_tenant_scope(auth_context),
        limit=50,
    )
    return customer


@app.patch("/api/customers/{customer_id}")
def edit_customer(
    customer_id: str,
    request: CustomerUpdate,
    auth_context: AuthContext | None = Depends(_optional_auth_context),
) -> dict:
    _ensure_crm_access(auth_context, manage=True)
    try:
        customer = update_customer(
            customer_id,
            **request.model_dump(exclude_unset=True),
            tenant_id=_crm_tenant_scope(auth_context),
            actor=auth_context.user_id if auth_context else "crm-api",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found.")
    return customer


@app.get("/api/customers/{customer_id}/interactions")
def customer_interactions(
    customer_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    auth_context: AuthContext | None = Depends(_optional_auth_context),
) -> list[dict]:
    _ensure_crm_access(auth_context)
    tenant_id = _crm_tenant_scope(auth_context)
    if not get_customer(customer_id, tenant_id=tenant_id):
        raise HTTPException(status_code=404, detail="Customer not found.")
    return list_customer_interactions(customer_id, tenant_id=tenant_id, limit=limit, offset=offset)


@app.post("/api/customers/{customer_id}/interactions", status_code=201)
def add_customer_timeline_event(
    customer_id: str,
    request: CustomerInteractionCreate,
    auth_context: AuthContext | None = Depends(_optional_auth_context),
) -> dict:
    _ensure_crm_access(auth_context)
    try:
        return add_customer_interaction(
            customer_id,
            **request.model_dump(),
            tenant_id=_crm_tenant_scope(auth_context),
            actor=auth_context.user_id if auth_context else "crm-api",
        )
    except ValueError as exc:
        message = str(exc)
        status_code = 404 if "not found" in message.lower() else 400
        raise HTTPException(status_code=status_code, detail=message) from exc


@app.get("/api/tickets")
def tickets(
    status: str | None = None,
    priority: str | None = None,
    owner_department: str | None = None,
    q: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    auth_context: AuthContext | None = Depends(_optional_auth_context),
) -> list[dict]:
    try:
        tickets = list_tickets(
            limit,
            tenant_id=_tenant_scope(auth_context),
            offset=offset,
        ) if not any([status, priority, owner_department, q]) else query_tickets(
            status=status,
            priority=priority,
            owner_department=owner_department,
            q=q,
            tenant_id=_tenant_scope(auth_context),
            limit=limit,
            offset=offset,
        )["tickets"]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if settings.auth_required and auth_context and not auth_context.is_admin:
        return [ticket for ticket in tickets if ticket.get("owner_department") == auth_context.department]
    return tickets


@app.get("/api/tickets/{ticket_id}")
def ticket_detail_api(
    ticket_id: str,
    auth_context: AuthContext | None = Depends(_optional_auth_context),
) -> dict:
    ticket = dict(_ensure_can_operate_ticket(ticket_id, auth_context))
    ticket["events"] = list_ticket_events(ticket_id, tenant_id=_tenant_scope(auth_context), limit=200)
    return ticket


@app.get("/api/tickets/{ticket_id}/events")
def ticket_timeline_api(
    ticket_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    auth_context: AuthContext | None = Depends(_optional_auth_context),
) -> list[dict]:
    _ensure_can_operate_ticket(ticket_id, auth_context)
    return list_ticket_events(ticket_id, tenant_id=_tenant_scope(auth_context), limit=limit, offset=offset)


@app.post("/api/tickets/{ticket_id}/comments", status_code=201)
def ticket_comment_api(
    ticket_id: str,
    request: TicketCommentCreate,
    auth_context: AuthContext | None = Depends(_optional_auth_context),
) -> dict:
    _ensure_can_operate_ticket(ticket_id, auth_context)
    try:
        return add_ticket_comment(
            ticket_id,
            request.body,
            actor=auth_context.user_id if auth_context else "ticket-api",
            tenant_id=_tenant_scope(auth_context),
        )
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"External ticket sync failed; the Outbox event can be retried: {exc}") from exc


@app.patch("/api/tickets/{ticket_id}/ops")
def ticket_ops(
    ticket_id: str,
    request: TicketOpsRequest,
    auth_context: AuthContext | None = Depends(_optional_auth_context),
) -> dict:
    _ensure_can_operate_ticket(ticket_id, auth_context)
    if settings.auth_required and request.status in {"approved", "rejected"} and auth_context and not auth_context.can_approve:
        raise HTTPException(status_code=403, detail="Manager or admin role is required for approval status changes.")
    try:
        ticket = update_ticket(
            ticket_id,
            status=request.status,
            owner_department=request.owner_department,
            priority=request.priority,
            comment=request.comment or "Ticket updated from Agent admin console.",
            actor=auth_context.user_id if auth_context else "admin-console",
            tenant_id=_tenant_scope(auth_context),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"External ticket sync failed; the Outbox event can be retried: {exc}") from exc
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    return ticket


@app.get("/api/emails")
def emails(limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    return list_emails(limit, tenant_id=_tenant_scope(auth_context))


@app.get("/api/audit-logs")
def audit_logs(limit: int = 100, auth_context: AuthContext | None = Depends(_optional_auth_context)) -> list[dict]:
    _ensure_admin_when_auth_required(auth_context)
    return list_audit_logs(limit, tenant_id=_tenant_scope(auth_context))


@app.get("/api/metrics/summary")
def metrics(auth_context: AuthContext | None = Depends(_optional_auth_context)) -> dict:
    tenant_id = _tenant_scope(auth_context)
    return {
        **metrics_summary(tenant_id),
        # What the models cost, next to what the workflows did. Same tenant
        # scope and same gating as the rest of this payload — LLM spend is not
        # a more sensitive number than the cost estimate already in ``totals``.
        #
        # ``usage_available_calls`` sits beside ``calls`` on purpose: a token
        # total computed from a subset of calls is only readable next to the
        # size of that subset, and a dashboard that reads NULL as 0 will
        # understate spend silently.
        "llm_usage": llm_usage_summary(tenant_id),
    }


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
    arguments = dict(request.arguments)
    tenant_id = _tenant_scope(auth_context)
    if tenant_id and request.tool_name in {"lookup_customer", "create_ticket", "query_tickets", "request_approval", "notify_internal_team"}:
        arguments.setdefault("tenant_id", tenant_id)
    if request.tool_name == "update_ticket" and tenant_id:
        arguments.setdefault("tenant_id", tenant_id)
        ticket_id = arguments.get("ticket_id")
        ticket = get_ticket(ticket_id) if ticket_id else None
        if not ticket or not _row_visible_to_context(ticket, auth_context):
            raise HTTPException(status_code=404, detail="Ticket not found.")
    result = call_tool(
        request.tool_name,
        arguments,
        actor=auth_context.user_id if auth_context else "mcp-http",
        source="http",
        auth_context=auth_context,
    )
    if "error" in result:
        status_code = 403 if result["error"] == "forbidden" else 400
        raise HTTPException(status_code=status_code, detail=result)
    return {"tool_name": request.tool_name, "result": result}


@app.post("/api/it/requests", status_code=201)
def it_submit_request(
    request: ITRequestCreate,
    auth_context: AuthContext = Depends(_required_auth_context),
) -> dict:
    """Run the Phase 1 IT intake loop for the authenticated requester.

    Authentication is mandatory here regardless of ``settings.auth_required``:
    the IT intake's authorization model is entirely built on who is asking, so
    an anonymous submission has no meaning.
    """
    try:
        return submit_it_request(request.objective, auth_context=auth_context, tenant_id=request.tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


IT_RESOLVABLE_STATUSES = frozenset({"open", "investigating"})
"""The only two ticket states from which resolution is meaningful.

``open`` is moved to ``investigating`` first — ``open -> investigating`` is a
legal transition and it is what tells a human reading the ticket that an agent
picked it up. Everything else (``waiting_approval``, ``resolved``, ...) is
already downstream of a decision, and re-entering the loop there would either
be a no-op or a second execution.
"""


def _it_loop_state(run_id: str | None) -> dict:
    """Recover the IT loop's intermediate results from the run's checkpoints.

    ``multi_agent_runs`` stores the run's own columns, not the graph state, so
    the triage / resolution / risk decision live only in ``agent_checkpoints``.
    Each checkpoint carries the whole state it observed, so replaying them in
    order and letting later ones win reconstructs the final picture.
    """
    if not run_id:
        return {}
    state: dict = {}
    for checkpoint in list_agent_checkpoints(run_id):
        snapshot = checkpoint.get("state")
        if isinstance(snapshot, dict):
            state.update(snapshot)
    return state


@app.post("/api/it/requests/{ticket_id}/resolve")
def it_resolve_request(
    ticket_id: str,
    request: ITResolveRequest,
    auth_context: AuthContext = Depends(_required_auth_context),
) -> dict:
    """Drive one IT ticket through Triage -> RAG -> Resolution -> Risk Gate.

    This is the Phase 2 entry point. It does not execute anything itself: the
    graph decides, the deterministic risk gate classifies, and a high-risk
    action stops at the ``interrupt`` with a ``waiting_approval`` ticket. Only
    ``open`` and ``investigating`` tickets can be resolved — anything else is
    already past the point where resolution would mean anything.
    """
    ticket = get_ticket(ticket_id)
    if not ticket or not _row_visible_to_context(ticket, auth_context):
        raise HTTPException(status_code=404, detail="Ticket not found.")
    if ticket.get("status") not in IT_RESOLVABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Ticket status {ticket.get('status')!r} cannot be resolved; expected one of {sorted(IT_RESOLVABLE_STATUSES)}.",
        )
    if ticket.get("status") == "open":
        moved = call_tool(
            "update_ticket",
            {
                "ticket_id": ticket_id,
                "status": "investigating",
                "actor": auth_context.user_id,
                "comment": "Phase 2 resolution loop started.",
                "tenant_id": auth_context.tenant_id,
            },
            actor=auth_context.user_id,
            source="http",
            auth_context=auth_context,
        )
        if moved.get("error"):
            raise HTTPException(status_code=403 if moved["error"] == "forbidden" else 400, detail=moved)
        ticket = get_ticket(ticket_id) or ticket

    objective = (request.objective or "").strip() or original_request_text(ticket)
    if not objective:
        raise HTTPException(status_code=400, detail="objective is required.")
    run = run_multi_agent(
        objective,
        requester_user_id=ticket.get("requester_user_id") or auth_context.user_id,
        requester_department=auth_context.department,
        requester_role=auth_context.role,
        tenant_id=request.tenant_id or ticket.get("tenant_id") or auth_context.tenant_id,
        enable_self_correction=request.enable_self_correction,
        max_correction_attempts=request.max_correction_attempts,
        it_ticket_id=ticket_id,
    )
    record_audit(
        "it.ticket_linked",
        "ticket",
        ticket_id,
        {
            "multi_agent_run_id": run.get("id"),
            "workflow_run_id": run.get("workflow_run_id"),
            "status": run.get("status"),
            "requester": ticket.get("requester_user_id"),
        },
        actor=auth_context.user_id,
        tenant_id=request.tenant_id or ticket.get("tenant_id") or auth_context.tenant_id,
    )
    loop = _it_loop_state(run.get("id"))
    return {
        "ticket_id": ticket_id,
        "multi_agent_run_id": run.get("id"),
        "workflow_run_id": run.get("workflow_run_id"),
        "status": run.get("status"),
        "ticket_status": (get_ticket(ticket_id) or {}).get("status"),
        "triage": loop.get("it_triage"),
        "resolution": loop.get("it_resolution"),
        "risk_decision": loop.get("it_risk_decision"),
        "execution": loop.get("it_execution"),
        "approval": _it_pending_approval(run.get("workflow_run_id")),
        "run": run,
    }


def _it_pending_approval(workflow_run_id: str | None) -> dict | None:
    """The approval a human still has to decide, if the run stopped for one."""
    if not workflow_run_id:
        return None
    return next(
        (item for item in list_approvals(limit=200) if item.get("run_id") == workflow_run_id and item.get("status") == "pending"),
        None,
    )


@app.get("/api/it/requests/{ticket_id}")
def it_get_request(ticket_id: str, auth_context: AuthContext = Depends(_required_auth_context)) -> dict:
    """Return one IT ticket with its triage record and event timeline."""
    ticket = get_ticket(ticket_id)
    if not ticket or not _row_visible_to_context(ticket, auth_context):
        raise HTTPException(status_code=404, detail="Ticket not found.")
    return {
        "ticket": ticket,
        "triage": ticket.get("triage") or {},
        "events": list_ticket_events(ticket_id, tenant_id=_tenant_scope(auth_context), limit=200),
    }


def _it_run_approval(workflow_run_id: str | None) -> dict | None:
    """The approval that gated this run, pending or already decided.

    ``_it_pending_approval`` answers "what does a human still have to do", which
    is the question the resolve endpoint is asked. The chain view needs the
    other question — "what did the human decide" — so after a decision it must
    still return the row, or the step that explains the execution goes blank
    exactly when it becomes interesting.
    """
    if not workflow_run_id:
        return None
    candidates = [
        item for item in list_approvals(limit=200) if item.get("run_id") == workflow_run_id
    ]
    if not candidates:
        return None
    pending = next((item for item in candidates if item.get("status") == "pending"), None)
    return pending or candidates[0]


@app.get("/api/it/requests/{ticket_id}/chain")
def it_request_chain(
    ticket_id: str, auth_context: AuthContext = Depends(_required_auth_context)
) -> dict:
    """The whole decision chain for one ticket, in one read-only response.

    This is what answers "why did the agent do that?" without replaying the run.
    Each stage of the graph left its result in a checkpoint, and each stage also
    wrote an audit row; returning both lets a reader check the recorded decision
    against the recorded inputs rather than trusting the summary.

    Read-only by construction: no tool is called, nothing is executed, and a
    ticket the caller cannot see is a 404 rather than a 403 so that the route
    does not confirm the existence of other tenants' tickets.
    """
    ticket = get_ticket(ticket_id)
    if not ticket or not _row_visible_to_context(ticket, auth_context):
        raise HTTPException(status_code=404, detail="Ticket not found.")
    run = find_multi_agent_run_by_it_ticket(ticket_id, tenant_id=_tenant_scope(auth_context))
    loop = _it_loop_state((run or {}).get("id"))
    return {
        "ticket": ticket,
        "triage": ticket.get("triage") or loop.get("it_triage") or {},
        "resolution": loop.get("it_resolution"),
        "risk_decision": loop.get("it_risk_decision"),
        "execution": loop.get("it_execution"),
        "history": loop.get("it_history"),
        "approval": _it_run_approval((run or {}).get("workflow_run_id")),
        "run": run,
        "events": list_ticket_events(ticket_id, tenant_id=_tenant_scope(auth_context), limit=200),
        "audit": list_it_audit_chain(ticket_id, tenant_id=_tenant_scope(auth_context)),
    }


@app.get("/api/it/triage/preview")
def it_triage_preview(
    objective: str = Query(min_length=1),
    auth_context: AuthContext | None = Depends(_optional_auth_context),
) -> dict:
    """Classify a request without creating a ticket. Deterministic rules only."""
    return {"objective": objective, "triage": classify(objective).to_dict()}


@app.get("/api/it/employees/{employee_id}")
def it_get_employee(employee_id: str, auth_context: AuthContext = Depends(_required_auth_context)) -> dict:
    """Directory lookup. Routed through ``call_tool`` so RBAC applies identically.

    Authentication is required at the edge even when ``settings.auth_required``
    is off, so an anonymous caller gets a clean 401 instead of a 403 from the
    tool gate.
    """
    result = call_tool("get_employee", {"employee_id": employee_id}, actor=auth_context.user_id, source="http", auth_context=auth_context)
    if "error" in result:
        raise HTTPException(status_code=403 if result["error"] == "forbidden" else 400, detail=result)
    return result


@app.get("/api/it/assets/{asset_id}")
def it_get_asset(asset_id: str, auth_context: AuthContext = Depends(_required_auth_context)) -> dict:
    """Asset lookup. Production assets are redacted for callers below it_support."""
    result = call_tool("get_asset", {"asset_id": asset_id}, actor=auth_context.user_id, source="http", auth_context=auth_context)
    if "error" in result:
        raise HTTPException(status_code=403 if result["error"] == "forbidden" else 400, detail=result)
    return result
