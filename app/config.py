from __future__ import annotations

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(BASE_DIR / ".env")


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _list_env(name: str) -> tuple[str, ...]:
    value = os.getenv(name, "")
    return tuple(item.strip() for item in value.split(",") if item.strip())


class Settings:
    app_env: str = os.getenv("AGENT_ENV", "development").strip().lower()
    app_name: str = os.getenv("AGENT_APP_NAME", "Enterprise Workflow Agent Platform")
    public_base_url: str = os.getenv("AGENT_PUBLIC_BASE_URL", "").rstrip("/")
    cors_origins: tuple[str, ...] = _list_env("AGENT_CORS_ORIGINS")
    trusted_hosts: tuple[str, ...] = _list_env("AGENT_TRUSTED_HOSTS")
    enable_security_headers: bool = _bool_env("AGENT_ENABLE_SECURITY_HEADERS", True)
    traceparent_enabled: bool = _bool_env("AGENT_TRACEPARENT_ENABLED", True)
    metrics_enabled: bool = _bool_env("AGENT_METRICS_ENABLED", True)
    metrics_auth_required: bool = _bool_env("AGENT_METRICS_AUTH_REQUIRED", False)
    service_name: str = os.getenv("AGENT_SERVICE_NAME", "enterprise-workflow-agent")
    service_version: str = os.getenv("AGENT_SERVICE_VERSION", "local")
    log_level: str = os.getenv("AGENT_LOG_LEVEL", "INFO").strip().upper() or "INFO"
    log_format: str = os.getenv("AGENT_LOG_FORMAT", "plain").strip().lower()
    otel_enabled: bool = _bool_env("AGENT_OTEL_ENABLED", False)
    otel_service_name: str = os.getenv("AGENT_OTEL_SERVICE_NAME", service_name)
    otel_exporter_otlp_endpoint: str = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").rstrip("/")
    otel_exporter_otlp_headers: str = os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")
    otel_export_console: bool = _bool_env("AGENT_OTEL_EXPORT_CONSOLE", False)
    data_dir: Path = BASE_DIR / "data"
    db_backend: str = os.getenv("AGENT_DB_BACKEND", "sqlite").strip().lower()
    db_path: Path = Path(os.getenv("AGENT_DB_PATH", str(data_dir / "agent_platform.sqlite3")))
    database_url: str = os.getenv("AGENT_DATABASE_URL", "")
    migration_database_url: str = os.getenv("AGENT_MIGRATION_DATABASE_URL", "").strip()
    worker_database_url: str = os.getenv("AGENT_WORKER_DATABASE_URL", "").strip()
    readonly_database_url: str = os.getenv("AGENT_READONLY_DATABASE_URL", "").strip()
    postgres_schema: str = os.getenv("AGENT_POSTGRES_SCHEMA", "agent_app").strip() or "agent_app"
    postgres_rls_enabled: bool = _bool_env("AGENT_POSTGRES_RLS_ENABLED", False)
    postgres_rls_bypass_role: str = os.getenv("AGENT_POSTGRES_RLS_BYPASS_ROLE", "").strip()
    tenant_isolation_enabled: bool = _bool_env("AGENT_TENANT_ISOLATION_ENABLED", False)
    default_tenant_id: str = os.getenv("AGENT_DEFAULT_TENANT_ID", "default")
    auto_migrate: bool = _bool_env("AGENT_AUTO_MIGRATE", True)
    auto_seed: bool = _bool_env("AGENT_AUTO_SEED", True)
    max_steps: int = int(os.getenv("AGENT_MAX_STEPS", "8"))
    approval_amount_threshold: float = float(os.getenv("AGENT_APPROVAL_AMOUNT_THRESHOLD", "500"))
    external_email_requires_approval: bool = _bool_env("AGENT_EXTERNAL_EMAIL_REQUIRES_APPROVAL", True)
    job_poll_interval_seconds: float = float(os.getenv("AGENT_JOB_POLL_INTERVAL_SECONDS", "2"))
    job_max_attempts: int = int(os.getenv("AGENT_JOB_MAX_ATTEMPTS", "3"))
    queue_backend: str = os.getenv("AGENT_QUEUE_BACKEND", "db").strip().lower()
    redis_url: str = os.getenv("AGENT_REDIS_URL", "").strip()
    redis_queue_name: str = os.getenv("AGENT_REDIS_QUEUE_NAME", "agent:workflow_jobs").strip() or "agent:workflow_jobs"
    redis_block_timeout_seconds: int = int(os.getenv("AGENT_REDIS_BLOCK_TIMEOUT_SECONDS", "5"))
    tool_retry_max_attempts: int = int(os.getenv("AGENT_TOOL_RETRY_MAX_ATTEMPTS", "2"))
    tool_retry_backoff_seconds: float = float(os.getenv("AGENT_TOOL_RETRY_BACKOFF_SECONDS", "0.2"))
    outbox_dispatcher_enabled: bool = _bool_env("AGENT_OUTBOX_DISPATCHER_ENABLED", False)
    embedded_outbox_dispatcher_enabled: bool = _bool_env("AGENT_EMBEDDED_OUTBOX_DISPATCHER_ENABLED", False)
    outbox_dispatch_interval_seconds: float = float(os.getenv("AGENT_OUTBOX_DISPATCH_INTERVAL_SECONDS", "5"))
    outbox_dispatch_batch_size: int = int(os.getenv("AGENT_OUTBOX_DISPATCH_BATCH_SIZE", "20"))
    outbox_max_attempts: int = int(os.getenv("AGENT_OUTBOX_MAX_ATTEMPTS", "5"))
    outbox_retry_backoff_seconds: float = float(os.getenv("AGENT_OUTBOX_RETRY_BACKOFF_SECONDS", "30"))
    auth_required: bool = _bool_env("AGENT_AUTH_REQUIRED", False)
    auth_token_secret: str = os.getenv("AGENT_AUTH_TOKEN_SECRET", "change-this-local-secret")
    access_token_expire_minutes: int = int(os.getenv("AGENT_ACCESS_TOKEN_EXPIRE_MINUTES", "480"))
    password_hash_iterations: int = int(os.getenv("AGENT_PASSWORD_HASH_ITERATIONS", "260000"))
    oidc_enabled: bool = _bool_env("AGENT_OIDC_ENABLED", False)
    oidc_issuer: str = os.getenv("AGENT_OIDC_ISSUER", "").rstrip("/")
    oidc_audience: str = os.getenv("AGENT_OIDC_AUDIENCE", "")
    oidc_jwks_url: str = os.getenv("AGENT_OIDC_JWKS_URL", "")
    oidc_browser_login_enabled: bool = _bool_env("AGENT_OIDC_BROWSER_LOGIN_ENABLED", False)
    oidc_authorization_url: str = os.getenv("AGENT_OIDC_AUTHORIZATION_URL", "").strip()
    oidc_token_url: str = os.getenv("AGENT_OIDC_TOKEN_URL", "").strip()
    oidc_client_id: str = os.getenv("AGENT_OIDC_CLIENT_ID", "").strip()
    oidc_client_secret: str = os.getenv("AGENT_OIDC_CLIENT_SECRET", "").strip()
    oidc_redirect_uri: str = os.getenv("AGENT_OIDC_REDIRECT_URI", "").strip()
    oidc_scopes: tuple[str, ...] = _list_env("AGENT_OIDC_SCOPES") or ("openid", "profile", "email")
    oidc_algorithms: tuple[str, ...] = _list_env("AGENT_OIDC_ALGORITHMS") or ("RS256",)
    oidc_hs256_secret: str = os.getenv("AGENT_OIDC_HS256_SECRET", "")
    oidc_sub_claim: str = os.getenv("AGENT_OIDC_SUB_CLAIM", "sub")
    oidc_display_name_claim: str = os.getenv("AGENT_OIDC_DISPLAY_NAME_CLAIM", "name")
    oidc_email_claim: str = os.getenv("AGENT_OIDC_EMAIL_CLAIM", "email")
    oidc_department_claim: str = os.getenv("AGENT_OIDC_DEPARTMENT_CLAIM", "department")
    oidc_role_claim: str = os.getenv("AGENT_OIDC_ROLE_CLAIM", "role")
    oidc_groups_claim: str = os.getenv("AGENT_OIDC_GROUPS_CLAIM", "groups")
    oidc_tenant_claim: str = os.getenv("AGENT_OIDC_TENANT_CLAIM", "tenant_id")
    oidc_admin_groups: tuple[str, ...] = _list_env("AGENT_OIDC_ADMIN_GROUPS")
    oidc_manager_groups: tuple[str, ...] = _list_env("AGENT_OIDC_MANAGER_GROUPS")
    oidc_default_role: str = os.getenv("AGENT_OIDC_DEFAULT_ROLE", "employee")
    oidc_default_department: str = os.getenv("AGENT_OIDC_DEFAULT_DEPARTMENT", "Operations")
    scim_enabled: bool = _bool_env("AGENT_SCIM_ENABLED", False)
    scim_token: str = os.getenv("AGENT_SCIM_TOKEN", "").strip()
    login_max_failures: int = int(os.getenv("AGENT_LOGIN_MAX_FAILURES", "5"))
    login_window_minutes: int = int(os.getenv("AGENT_LOGIN_WINDOW_MINUTES", "15"))
    login_lockout_minutes: int = int(os.getenv("AGENT_LOGIN_LOCKOUT_MINUTES", "15"))
    audit_retention_days: int = int(os.getenv("AGENT_AUDIT_RETENTION_DAYS", "0"))
    eval_report_retention_days: int = int(os.getenv("AGENT_EVAL_REPORT_RETENTION_DAYS", "0"))
    external_outbox_retention_days: int = int(os.getenv("AGENT_EXTERNAL_OUTBOX_RETENTION_DAYS", "0"))
    email_retention_days: int = int(os.getenv("AGENT_EMAIL_RETENTION_DAYS", "0"))
    workflow_job_retention_days: int = int(os.getenv("AGENT_WORKFLOW_JOB_RETENTION_DAYS", "0"))
    login_attempt_retention_days: int = int(os.getenv("AGENT_LOGIN_ATTEMPT_RETENTION_DAYS", "0"))
    rag_base_url: str = os.getenv("KNOWLEDGE_RAG_BASE_URL", "").rstrip("/")
    rag_service_token: str = os.getenv("KNOWLEDGE_RAG_SERVICE_TOKEN", "").strip()
    rag_timeout_seconds: float = float(os.getenv("KNOWLEDGE_RAG_TIMEOUT_SECONDS", "8"))

    llm_enabled: bool = _bool_env("AGENT_LLM_ENABLED", False)
    llm_provider: str = os.getenv("AGENT_LLM_PROVIDER", "qwen").strip().lower()
    llm_base_url: str = os.getenv("AGENT_LLM_BASE_URL", "").rstrip("/")
    llm_api_key: str = os.getenv("AGENT_LLM_API_KEY", os.getenv("DASHSCOPE_API_KEY", "")).strip()
    llm_model: str = os.getenv("AGENT_LLM_MODEL", "qwen-plus").strip()
    llm_timeout_seconds: float = float(os.getenv("AGENT_LLM_TIMEOUT_SECONDS", "30"))
    llm_temperature: float = float(os.getenv("AGENT_LLM_TEMPERATURE", "0.2"))
    llm_max_tokens: int = int(os.getenv("AGENT_LLM_MAX_TOKENS", "900"))
    llm_planner_enabled: bool = _bool_env("AGENT_LLM_PLANNER_ENABLED", llm_enabled)
    llm_final_answer_enabled: bool = _bool_env("AGENT_LLM_FINAL_ANSWER_ENABLED", llm_enabled)
    llm_multi_agent_reasoning_enabled: bool = _bool_env(
        "AGENT_LLM_MULTI_AGENT_REASONING_ENABLED",
        llm_enabled,
    )
    llm_planner_min_confidence: float = float(os.getenv("AGENT_LLM_PLANNER_MIN_CONFIDENCE", "0.55"))

    tool_mode: str = os.getenv("AGENT_TOOL_MODE", "mock").strip().lower()

    email_provider: str = os.getenv("AGENT_EMAIL_PROVIDER", "").strip().lower()
    email_allowlist: tuple[str, ...] = _list_env("AGENT_EMAIL_ALLOWLIST")
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_username: str = os.getenv("SMTP_USERNAME", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_from_email: str = os.getenv("SMTP_FROM_EMAIL", smtp_username)
    smtp_use_tls: bool = _bool_env("SMTP_USE_TLS", True)
    smtp_use_ssl: bool = _bool_env("SMTP_USE_SSL", False)
    smtp_timeout_seconds: float = float(os.getenv("SMTP_TIMEOUT_SECONDS", "15"))

    ticket_provider: str = os.getenv("AGENT_TICKET_PROVIDER", "").strip().lower()
    ticket_api_url: str = os.getenv("TICKET_API_URL", "")
    ticket_api_token: str = os.getenv("TICKET_API_TOKEN", "")
    ticket_api_timeout_seconds: float = float(os.getenv("TICKET_API_TIMEOUT_SECONDS", "15"))
    ticket_http_id_field: str = os.getenv("TICKET_HTTP_ID_FIELD", "id")
    ticket_http_url_field: str = os.getenv("TICKET_HTTP_URL_FIELD", "url")
    jira_base_url: str = os.getenv("JIRA_BASE_URL", "").rstrip("/")
    jira_email: str = os.getenv("JIRA_EMAIL", "")
    jira_api_token: str = os.getenv("JIRA_API_TOKEN", "")
    jira_project_key: str = os.getenv("JIRA_PROJECT_KEY", "")
    jira_issue_type: str = os.getenv("JIRA_ISSUE_TYPE", "Task")


settings = Settings()
if settings.app_env not in {"development", "test", "staging", "production"}:
    raise ValueError("AGENT_ENV must be one of: development, test, staging, production.")
if settings.db_backend not in {"sqlite", "postgres"}:
    raise ValueError("AGENT_DB_BACKEND must be 'sqlite' or 'postgres'.")
if settings.log_format not in {"plain", "json"}:
    raise ValueError("AGENT_LOG_FORMAT must be 'plain' or 'json'.")
if settings.queue_backend not in {"db", "redis"}:
    raise ValueError("AGENT_QUEUE_BACKEND must be 'db' or 'redis'.")
if settings.llm_provider not in {"qwen", "vllm", "openai_compatible", "openai-compatible"}:
    raise ValueError("AGENT_LLM_PROVIDER must be 'qwen', 'vllm', or 'openai_compatible'.")
if not settings.db_path.is_absolute():
    settings.db_path = BASE_DIR / settings.db_path
settings.db_path.parent.mkdir(parents=True, exist_ok=True)
