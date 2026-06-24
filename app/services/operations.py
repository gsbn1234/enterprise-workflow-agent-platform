from __future__ import annotations

from app.config import settings
from app.db import database_status, get_connection, rows_to_dicts
from app.services.audit import verify_audit_log_integrity
from app.services.auth import auth_security_summary
from app.services.metrics import metrics_summary
from app.services.oidc import oidc_status
from app.services.observability import observability_status
from app.services.outbox import outbox_dispatcher_status
from app.services.queue import queue_status
from app.services.retention import retention_status


DEFAULT_LOCAL_SECRET = "change-this-local-secret"


def production_warnings() -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    production_like = settings.app_env in {"staging", "production"}

    if production_like and not settings.auth_required:
        warnings.append(
            {
                "code": "auth_disabled",
                "severity": "critical",
                "message": "AGENT_AUTH_REQUIRED should be true outside local demos.",
            }
        )
    token_secret = settings.auth_token_secret.strip()
    if production_like and (
        token_secret == DEFAULT_LOCAL_SECRET or len(token_secret) < 32 or "replace" in token_secret.lower()
    ):
        warnings.append(
            {
                "code": "weak_auth_secret",
                "severity": "critical",
                "message": "AGENT_AUTH_TOKEN_SECRET should be a long random secret.",
            }
        )
    if production_like and settings.db_backend == "sqlite":
        warnings.append(
            {
                "code": "sqlite_primary_db",
                "severity": "high",
                "message": "Use AGENT_DB_BACKEND=postgres for multi-user production deployments.",
            }
        )
    if production_like and settings.auto_migrate:
        warnings.append(
            {
                "code": "runtime_auto_migrate_enabled",
                "severity": "high",
                "message": "Run migrations as a separate release step and set AGENT_AUTO_MIGRATE=false on web/worker processes.",
            }
        )
    if production_like and settings.db_backend == "postgres":
        if not settings.migration_database_url:
            warnings.append(
                {
                    "code": "migration_db_role_not_split",
                    "severity": "medium",
                    "message": "Set AGENT_MIGRATION_DATABASE_URL for a dedicated migration database role.",
                }
            )
        if not settings.worker_database_url:
            warnings.append(
                {
                    "code": "worker_db_role_not_split",
                    "severity": "medium",
                    "message": "Set AGENT_WORKER_DATABASE_URL for a dedicated worker database role.",
                }
            )
        if not settings.readonly_database_url:
            warnings.append(
                {
                    "code": "readonly_db_role_not_split",
                    "severity": "medium",
                    "message": "Set AGENT_READONLY_DATABASE_URL for backups, diagnostics, and read-only operational checks.",
                }
            )
    if production_like and settings.tenant_isolation_enabled and settings.db_backend == "postgres" and not settings.postgres_rls_enabled:
        warnings.append(
            {
                "code": "postgres_rls_disabled",
                "severity": "medium",
                "message": "Enable AGENT_POSTGRES_RLS_ENABLED=true for defense-in-depth tenant isolation.",
            }
        )
    if production_like and settings.postgres_rls_enabled and not settings.postgres_rls_bypass_role:
        warnings.append(
            {
                "code": "postgres_rls_bypass_role_missing",
                "severity": "medium",
                "message": "Set AGENT_POSTGRES_RLS_BYPASS_ROLE to require a database role for system-level RLS bypass.",
            }
        )
    if settings.postgres_rls_enabled and settings.db_backend != "postgres":
        warnings.append(
            {
                "code": "postgres_rls_without_postgres",
                "severity": "medium",
                "message": "AGENT_POSTGRES_RLS_ENABLED only takes effect when AGENT_DB_BACKEND=postgres.",
            }
        )
    if production_like and not settings.cors_origins:
        warnings.append(
            {
                "code": "cors_not_pinned",
                "severity": "medium",
                "message": "Set AGENT_CORS_ORIGINS to the approved frontend origin list.",
            }
        )
    if production_like and settings.metrics_enabled and not settings.metrics_auth_required:
        warnings.append(
            {
                "code": "metrics_unprotected",
                "severity": "medium",
                "message": "Set AGENT_METRICS_AUTH_REQUIRED=true or expose /metrics only on a private network.",
            }
        )
    if production_like and not settings.traceparent_enabled:
        warnings.append(
            {
                "code": "trace_context_disabled",
                "severity": "medium",
                "message": "Enable AGENT_TRACEPARENT_ENABLED=true for request correlation across services.",
            }
        )
    if production_like and settings.log_format != "json":
        warnings.append(
            {
                "code": "structured_logs_disabled",
                "severity": "medium",
                "message": "Set AGENT_LOG_FORMAT=json so application logs can be indexed by log platforms.",
            }
        )
    if production_like and not settings.otel_enabled:
        warnings.append(
            {
                "code": "otel_disabled",
                "severity": "medium",
                "message": "Set AGENT_OTEL_ENABLED=true and configure OTLP export for distributed tracing.",
            }
        )
    if settings.otel_enabled and not (settings.otel_exporter_otlp_endpoint or settings.otel_export_console):
        warnings.append(
            {
                "code": "otel_exporter_missing",
                "severity": "high",
                "message": "AGENT_OTEL_ENABLED=true requires OTEL_EXPORTER_OTLP_ENDPOINT or AGENT_OTEL_EXPORT_CONSOLE=true.",
            }
        )
    observability = observability_status()
    if settings.otel_enabled and observability["otel"].get("error"):
        warnings.append(
            {
                "code": "otel_not_configured",
                "severity": "high",
                "message": f"OpenTelemetry failed to initialize: {observability['otel']['error']}",
            }
        )
    queue = queue_status(check_connection=production_like and settings.queue_backend == "redis")
    if production_like and settings.queue_backend == "db":
        warnings.append(
            {
                "code": "external_queue_disabled",
                "severity": "medium",
                "message": "Set AGENT_QUEUE_BACKEND=redis for horizontally scalable worker dispatch.",
            }
        )
    if settings.queue_backend == "redis" and not settings.redis_url:
        warnings.append(
            {
                "code": "redis_queue_url_missing",
                "severity": "high",
                "message": "AGENT_QUEUE_BACKEND=redis requires AGENT_REDIS_URL.",
            }
        )
    if settings.queue_backend == "redis" and not queue.get("redis_dependency_available"):
        warnings.append(
            {
                "code": "redis_dependency_missing",
                "severity": "high",
                "message": "Install the redis Python package before enabling AGENT_QUEUE_BACKEND=redis.",
            }
        )
    if (
        production_like
        and settings.queue_backend == "redis"
        and settings.redis_url
        and queue.get("redis_dependency_available")
        and queue.get("reachable") is False
    ):
        warnings.append(
            {
                "code": "redis_queue_unreachable",
                "severity": "high",
                "message": f"Redis queue is configured but not reachable: {queue.get('error') or 'unknown error'}.",
            }
        )
    oidc = oidc_status()
    if production_like and not settings.oidc_enabled:
        warnings.append(
            {
                "code": "sso_not_configured",
                "severity": "medium",
                "message": "Configure AGENT_OIDC_ENABLED=true with an enterprise identity provider before production launch.",
            }
        )
    if production_like and settings.oidc_enabled and not settings.oidc_browser_login_enabled:
        warnings.append(
            {
                "code": "browser_sso_disabled",
                "severity": "medium",
                "message": "Enable AGENT_OIDC_BROWSER_LOGIN_ENABLED=true for browser-based enterprise SSO.",
            }
        )
    if production_like and settings.oidc_browser_login_enabled and not settings.oidc_client_secret:
        warnings.append(
            {
                "code": "oidc_client_secret_missing",
                "severity": "medium",
                "message": "Configure AGENT_OIDC_CLIENT_SECRET for confidential browser SSO clients unless your IdP explicitly uses a public client.",
            }
        )
    if settings.oidc_enabled and oidc["missing"]:
        warnings.append(
            {
                "code": "oidc_incomplete",
                "severity": "high",
                "message": f"OIDC is enabled but missing: {', '.join(oidc['missing'])}.",
            }
        )
    if production_like and settings.oidc_enabled and oidc["uses_hs256"]:
        warnings.append(
            {
                "code": "oidc_hs256_in_production",
                "severity": "high",
                "message": "Use RS256/JWKS for production OIDC instead of shared-secret HS256.",
            }
        )
    if production_like and not settings.scim_enabled:
        warnings.append(
            {
                "code": "scim_not_configured",
                "severity": "medium",
                "message": "Enable AGENT_SCIM_ENABLED=true and configure AGENT_SCIM_TOKEN for enterprise identity lifecycle sync.",
            }
        )
    if settings.scim_enabled and (
        not settings.scim_token or len(settings.scim_token) < 32 or "replace" in settings.scim_token.lower()
    ):
        warnings.append(
            {
                "code": "scim_token_weak",
                "severity": "high",
                "message": "AGENT_SCIM_TOKEN should be a long random bearer token.",
            }
        )
    if production_like and not settings.outbox_dispatcher_enabled:
        warnings.append(
            {
                "code": "outbox_dispatcher_disabled",
                "severity": "medium",
                "message": "Enable AGENT_OUTBOX_DISPATCHER_ENABLED=true or run an external outbox dispatcher worker.",
            }
        )
    retention_days = [
        settings.audit_retention_days,
        settings.eval_report_retention_days,
        settings.external_outbox_retention_days,
        settings.email_retention_days,
        settings.workflow_job_retention_days,
        settings.login_attempt_retention_days,
    ]
    if production_like and not any(days > 0 for days in retention_days):
        warnings.append(
            {
                "code": "retention_disabled",
                "severity": "medium",
                "message": "Configure retention windows for audit, outbox, email, job, and login-attempt history.",
            }
        )
    if settings.tool_mode == "real" and settings.email_provider == "smtp" and not settings.email_allowlist:
        warnings.append(
            {
                "code": "email_allowlist_missing",
                "severity": "critical",
                "message": "Real SMTP sending should require AGENT_EMAIL_ALLOWLIST.",
            }
        )
    if settings.email_provider == "smtp":
        missing = [
            name
            for name, value in {
                "SMTP_HOST": settings.smtp_host,
                "SMTP_USERNAME": settings.smtp_username,
                "SMTP_PASSWORD": settings.smtp_password,
                "SMTP_FROM_EMAIL": settings.smtp_from_email,
            }.items()
            if not value
        ]
        if missing:
            warnings.append(
                {
                    "code": "smtp_incomplete",
                    "severity": "high",
                    "message": f"SMTP is selected but missing: {', '.join(missing)}.",
                }
            )
    if settings.ticket_provider == "http" and not settings.ticket_api_url:
        warnings.append(
            {
                "code": "ticket_http_incomplete",
                "severity": "high",
                "message": "AGENT_TICKET_PROVIDER=http requires TICKET_API_URL.",
            }
        )
    if production_like and settings.ticket_provider == "http" and "replace" in settings.ticket_api_token.lower():
        warnings.append(
            {
                "code": "ticket_token_placeholder",
                "severity": "high",
                "message": "TICKET_API_TOKEN still looks like a placeholder value.",
            }
        )
    if settings.ticket_provider == "jira":
        missing = [
            name
            for name, value in {
                "JIRA_BASE_URL": settings.jira_base_url,
                "JIRA_EMAIL": settings.jira_email,
                "JIRA_API_TOKEN": settings.jira_api_token,
                "JIRA_PROJECT_KEY": settings.jira_project_key,
            }.items()
            if not value
        ]
        if missing:
            warnings.append(
                {
                    "code": "jira_incomplete",
                    "severity": "high",
                    "message": f"Jira provider is selected but missing: {', '.join(missing)}.",
                }
            )

    return warnings


def operations_status() -> dict:
    warnings = production_warnings()
    blocking = [item for item in warnings if item["severity"] in {"critical", "high"}]
    return {
        "environment": settings.app_env,
        "production_like": settings.app_env in {"staging", "production"},
        "production_ready": not blocking,
        "blocking_count": len(blocking),
        "warnings": warnings,
    }


def operations_dashboard() -> dict:
    with get_connection() as conn:
        workflow_statuses = rows_to_dicts(
            conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM workflow_runs
                GROUP BY status
                ORDER BY status ASC
                """
            ).fetchall()
        )
        job_statuses = rows_to_dicts(
            conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM workflow_jobs
                GROUP BY status
                ORDER BY status ASC
                """
            ).fetchall()
        )
        approval_statuses = rows_to_dicts(
            conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM approvals
                GROUP BY status
                ORDER BY status ASC
                """
            ).fetchall()
        )
        ticket_statuses = rows_to_dicts(
            conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM tickets
                GROUP BY status
                ORDER BY status ASC
                """
            ).fetchall()
        )
        outbox_statuses = rows_to_dicts(
            conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM external_outbox
                GROUP BY status
                ORDER BY status ASC
                """
            ).fetchall()
        )
        recent_failed_runs = rows_to_dicts(
            conn.execute(
                """
                SELECT id, objective, status, category, risk_level, refusal_reason, completed_at, created_at
                FROM workflow_runs
                WHERE status IN ('failed', 'rejected')
                ORDER BY created_at DESC, id DESC
                LIMIT 10
                """
            ).fetchall()
        )
        recent_failed_jobs = rows_to_dicts(
            conn.execute(
                """
                SELECT id, objective, status, attempts, max_attempts, error_message, updated_at, created_at
                FROM workflow_jobs
                WHERE status = 'failed'
                ORDER BY updated_at DESC, id DESC
                LIMIT 10
                """
            ).fetchall()
        )
        recent_failed_outbox = rows_to_dicts(
            conn.execute(
                """
                SELECT id, action_type, provider, target_type, target_id, idempotency_key,
                       attempt_count, error_message, updated_at, created_at
                FROM external_outbox
                WHERE status = 'failed'
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT 10
                """
            ).fetchall()
        )
        recent_audit = rows_to_dicts(
            conn.execute(
                """
                SELECT id, actor, event_type, target_type, target_id, created_at
                FROM audit_logs
                ORDER BY created_at DESC, id DESC
                LIMIT 10
                """
            ).fetchall()
        )

    return {
        "database": database_status(),
        "operations": operations_status(),
        "security": auth_security_summary(),
        "metrics": metrics_summary(),
        "tracing": {
            "traceparent_enabled": settings.traceparent_enabled,
            "response_headers": ["traceparent", "X-Trace-ID", "X-Span-ID", "X-Request-ID"],
        },
        "observability": observability_status(),
        "oidc": oidc_status(),
        "scim": {
            "enabled": settings.scim_enabled,
            "token_configured": bool(settings.scim_token),
        },
        "audit_integrity": verify_audit_log_integrity(),
        "retention": retention_status(),
        "outbox_dispatcher": outbox_dispatcher_status(),
        "queue": queue_status(check_connection=True),
        "statuses": {
            "workflow_runs": workflow_statuses,
            "workflow_jobs": job_statuses,
            "approvals": approval_statuses,
            "tickets": ticket_statuses,
            "external_outbox": outbox_statuses,
        },
        "recent_failed_runs": recent_failed_runs,
        "recent_failed_jobs": recent_failed_jobs,
        "recent_failed_outbox": recent_failed_outbox,
        "recent_audit": recent_audit,
    }
