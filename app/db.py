from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

from app.config import settings
from app.services.tenancy import current_rls_bypass, current_tenant_id, rls_system_context
from app.utils import json_dumps, new_id, utc_now

try:
    import psycopg
    from psycopg.rows import dict_row
except ModuleNotFoundError:  # pragma: no cover - optional until postgres is enabled
    psycopg = None
    dict_row = None


BASELINE_MIGRATION_ID = "0001_enterprise_workflow_baseline"
BASELINE_MIGRATION_DESCRIPTION = "Enterprise workflow agent baseline schema and indexes"
IDEMPOTENCY_MIGRATION_ID = "0002_side_effect_idempotency"
IDEMPOTENCY_MIGRATION_DESCRIPTION = "Add idempotency keys for tickets and emails"
AUTH_SESSION_MIGRATION_ID = "0003_revocable_auth_sessions"
AUTH_SESSION_MIGRATION_DESCRIPTION = "Add revocable access-token sessions"
OUTBOX_MIGRATION_ID = "0004_external_outbox"
OUTBOX_MIGRATION_DESCRIPTION = "Add external side-effect outbox"
AUTH_LOGIN_PROTECTION_MIGRATION_ID = "0005_auth_login_protection"
AUTH_LOGIN_PROTECTION_MIGRATION_DESCRIPTION = "Add login attempt audit and lockout controls"
AUDIT_HASH_CHAIN_MIGRATION_ID = "0006_audit_hash_chain"
AUDIT_HASH_CHAIN_MIGRATION_DESCRIPTION = "Add tamper-evident audit hash chain"
TENANT_ISOLATION_MIGRATION_ID = "0007_tenant_isolation_foundation"
TENANT_ISOLATION_MIGRATION_DESCRIPTION = "Add tenant identifiers to core business records"
POSTGRES_RLS_MIGRATION_ID = "0008_postgres_tenant_rls"
POSTGRES_RLS_MIGRATION_DESCRIPTION = "Add optional PostgreSQL row-level security for tenant-scoped records"
POSTGRES_RLS_BYPASS_ROLE_MIGRATION_ID = "0009_postgres_rls_bypass_role_gate"
POSTGRES_RLS_BYPASS_ROLE_MIGRATION_DESCRIPTION = "Gate PostgreSQL RLS bypass on an optional database role"
WORKFLOW_RUN_TICKET_LINK_MIGRATION_ID = "0010_workflow_run_ticket_link"
WORKFLOW_RUN_TICKET_LINK_MIGRATION_DESCRIPTION = "Store the primary ticket created by each workflow run"
CRM_FOUNDATION_MIGRATION_ID = "0011_tenant_crm_foundation"
CRM_FOUNDATION_MIGRATION_DESCRIPTION = "Add tenant-scoped CRM profiles and customer interaction history"
TICKET_TIMELINE_MIGRATION_ID = "0012_ticket_timeline"
TICKET_TIMELINE_MIGRATION_DESCRIPTION = "Add tenant-scoped operational ticket history"
TICKET_SLA_MIGRATION_ID = "0013_ticket_sla"
TICKET_SLA_MIGRATION_DESCRIPTION = "Add ticket due dates and terminal lifecycle timestamps"
IT_SERVICE_FOUNDATION_MIGRATION_ID = "0014_it_service_foundation"
IT_SERVICE_FOUNDATION_MIGRATION_DESCRIPTION = "Add the IT service directory (departments, employees, assets) and IT ticket intake fields"
IT_RUN_TICKET_LINK_MIGRATION_ID = "0015_it_run_ticket_link"
IT_RUN_TICKET_LINK_MIGRATION_DESCRIPTION = "Link a multi-agent run to the IT ticket it resolves, so an IT run replays as an IT run"
IT_HISTORICAL_TICKETS_MIGRATION_ID = "0016_it_historical_tickets"
IT_HISTORICAL_TICKETS_MIGRATION_DESCRIPTION = (
    "Add the historical IT ticket reference corpus, kept separate from knowledge articles"
)


TENANT_RLS_TABLES = (
    "business_requests",
    "workflow_runs",
    "workflow_jobs",
    "multi_agent_runs",
    "agent_memory",
    "golden_traces",
    "trace_replays",
    "approvals",
    "customers",
    "tickets",
    "emails",
    "external_outbox",
    "audit_logs",
    "departments",
    "employees",
    "assets",
    "llm_calls",
)

RELATED_RLS_TABLES = (
    ("workflow_steps", "workflow_runs", "run_id"),
    ("multi_agent_messages", "multi_agent_runs", "run_id"),
    ("multi_agent_tasks", "multi_agent_runs", "run_id"),
    ("multi_agent_handoffs", "multi_agent_runs", "run_id"),
    ("agent_checkpoints", "multi_agent_runs", "run_id"),
    ("customer_interactions", "customers", "customer_id"),
    ("ticket_events", "tickets", "ticket_id"),
)


CONNECTION_PURPOSES = {"runtime", "migration", "worker", "readonly"}
_connection_purpose: ContextVar[str] = ContextVar("database_connection_purpose", default="runtime")


def normalize_connection_purpose(purpose: str | None) -> str:
    normalized = (purpose or current_connection_purpose()).strip().lower()
    if normalized not in CONNECTION_PURPOSES:
        raise ValueError(f"Unknown database connection purpose: {purpose!r}.")
    return normalized


def current_connection_purpose() -> str:
    return _connection_purpose.get()


def set_connection_purpose(purpose: str):
    return _connection_purpose.set(normalize_connection_purpose(purpose))


def reset_connection_purpose(token) -> None:
    _connection_purpose.reset(token)


@contextmanager
def database_connection_purpose(purpose: str) -> Iterator[None]:
    token = set_connection_purpose(purpose)
    try:
        yield
    finally:
        reset_connection_purpose(token)


def database_url_for_purpose(purpose: str | None = None) -> str:
    normalized = normalize_connection_purpose(purpose)
    urls = {
        "migration": settings.migration_database_url,
        "worker": settings.worker_database_url,
        "readonly": settings.readonly_database_url,
        "runtime": settings.database_url,
    }
    return urls.get(normalized) or settings.database_url


def database_connection_profiles() -> dict[str, Any]:
    return {
        "runtime_configured": bool(settings.database_url),
        "migration_configured": bool(settings.migration_database_url),
        "worker_configured": bool(settings.worker_database_url),
        "readonly_configured": bool(settings.readonly_database_url),
        "current_purpose": current_connection_purpose(),
    }


class PostgresConnection:
    backend = "postgres"

    def __init__(self, dsn: str, schema: str, purpose: str = "runtime") -> None:
        if psycopg is None or dict_row is None:
            raise RuntimeError("psycopg is required for AGENT_DB_BACKEND=postgres. Run: pip install -r requirements.txt")
        self.schema = clean_identifier(schema)
        self.purpose = normalize_connection_purpose(purpose)
        self._conn = psycopg.connect(dsn, row_factory=dict_row)
        quoted_schema = quote_identifier(self.schema)
        if self.purpose == "migration":
            self._conn.execute(f"CREATE SCHEMA IF NOT EXISTS {quoted_schema}")
        self._conn.execute(f"SET search_path TO {quoted_schema}")
        if settings.postgres_rls_enabled:
            self._conn.execute("SELECT set_config('app.tenant_id', %s, false)", (current_tenant_id(),))
            self._conn.execute("SELECT set_config('app.rls_bypass', %s, false)", ("on" if current_rls_bypass() else "off",))
            self._conn.execute("SELECT set_config('app.rls_bypass_role', %s, false)", (settings.postgres_rls_bypass_role,))
        self._conn.commit()

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] | None = None):
        prepared_sql = translate_sqlite_placeholders(sql)
        if prepared_sql.strip().upper() == "BEGIN IMMEDIATE":
            prepared_sql = "BEGIN"
        return self._conn.execute(prepared_sql, params)

    def executescript(self, script: str) -> None:
        for statement in split_sql_script(script):
            self.execute(statement)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PostgresConnection":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()


def get_connection(db_path: Path | None = None, *, purpose: str | None = None) -> sqlite3.Connection | PostgresConnection:
    if db_path is None and settings.db_backend == "postgres":
        normalized_purpose = normalize_connection_purpose(purpose)
        dsn = database_url_for_purpose(purpose)
        if not dsn:
            raise RuntimeError("AGENT_DATABASE_URL is required when AGENT_DB_BACKEND=postgres.")
        return PostgresConnection(dsn, settings.postgres_schema, purpose=normalized_purpose)
    path = db_path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def row_to_dict(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def rows_to_dicts(rows: list[sqlite3.Row] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row_to_dict(row) for row in rows if row is not None]


def init_db(seed: bool | None = None) -> None:
    with get_connection(purpose="migration") as conn:
        _ensure_schema_migrations(conn)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS business_requests (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                requester_user_id TEXT,
                requester_department TEXT,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                category TEXT,
                priority TEXT NOT NULL DEFAULT 'normal',
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                department TEXT NOT NULL,
                role TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                password_hash TEXT NOT NULL,
                disabled INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auth_sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS auth_login_attempts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                success INTEGER NOT NULL DEFAULT 0,
                failure_reason TEXT,
                ip_address TEXT,
                user_agent TEXT,
                lockout_until TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS workflow_runs (
                id TEXT PRIMARY KEY,
                request_id TEXT,
                objective TEXT NOT NULL,
                status TEXT NOT NULL,
                category TEXT,
                risk_level TEXT,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                needs_approval INTEGER NOT NULL DEFAULT 0,
                final_answer TEXT,
                refusal_reason TEXT,
                cost_estimate REAL NOT NULL DEFAULT 0,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY (request_id) REFERENCES business_requests(id)
            );

            CREATE TABLE IF NOT EXISTS workflow_jobs (
                id TEXT PRIMARY KEY,
                objective TEXT NOT NULL,
                request_id TEXT,
                requester_user_id TEXT,
                requester_department TEXT,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                run_id TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                FOREIGN KEY (request_id) REFERENCES business_requests(id),
                FOREIGN KEY (run_id) REFERENCES workflow_runs(id)
            );

            CREATE TABLE IF NOT EXISTS workflow_steps (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                node_name TEXT NOT NULL,
                action_type TEXT NOT NULL,
                tool_name TEXT,
                tool_input_json TEXT NOT NULL,
                tool_output_json TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 1,
                max_attempts INTEGER NOT NULL DEFAULT 1,
                retryable INTEGER NOT NULL DEFAULT 0,
                error_type TEXT,
                attempts_json TEXT NOT NULL DEFAULT '[]',
                reasoning_summary TEXT NOT NULL,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES workflow_runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS multi_agent_runs (
                id TEXT PRIMARY KEY,
                objective TEXT NOT NULL,
                requester_user_id TEXT,
                requester_department TEXT,
                requester_role TEXT,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                status TEXT NOT NULL,
                workflow_run_id TEXT,
                final_summary TEXT,
                critic_score REAL NOT NULL DEFAULT 0,
                critic_report_json TEXT NOT NULL DEFAULT '{}',
                memory_item_id TEXT,
                executor_type TEXT NOT NULL DEFAULT 'durable_langgraph',
                thread_id TEXT,
                correction_count INTEGER NOT NULL DEFAULT 0,
                replay_of_run_id TEXT,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY (workflow_run_id) REFERENCES workflow_runs(id)
            );

            CREATE TABLE IF NOT EXISTS multi_agent_messages (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                role TEXT NOT NULL,
                content_json TEXT NOT NULL,
                status TEXT NOT NULL,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES multi_agent_runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS multi_agent_tasks (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                task_key TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0,
                assigned_agent TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                dependencies_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL,
                input_json TEXT NOT NULL DEFAULT '{}',
                output_json TEXT NOT NULL DEFAULT '{}',
                duration_ms INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                completed_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (run_id, task_key, attempt),
                FOREIGN KEY (run_id) REFERENCES multi_agent_runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS multi_agent_handoffs (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                from_agent TEXT NOT NULL,
                to_agent TEXT NOT NULL,
                task_key TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES multi_agent_runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS agent_memory (
                id TEXT PRIMARY KEY,
                memory_type TEXT NOT NULL,
                memory_key TEXT NOT NULL,
                summary TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                source_run_id TEXT,
                score REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_checkpoints (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                checkpoint_index INTEGER NOT NULL,
                node_name TEXT NOT NULL,
                status TEXT NOT NULL,
                state_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES multi_agent_runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS golden_traces (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                source_run_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                trace_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (source_run_id) REFERENCES multi_agent_runs(id)
            );

            CREATE TABLE IF NOT EXISTS trace_replays (
                id TEXT PRIMARY KEY,
                source_run_id TEXT NOT NULL,
                replay_run_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                status TEXT NOT NULL,
                diff_report_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY (source_run_id) REFERENCES multi_agent_runs(id),
                FOREIGN KEY (replay_run_id) REFERENCES multi_agent_runs(id)
            );

            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                status TEXT NOT NULL,
                requested_by TEXT,
                decided_by TEXT,
                decision_reason TEXT,
                created_at TEXT NOT NULL,
                decided_at TEXT,
                FOREIGN KEY (run_id) REFERENCES workflow_runs(id)
            );

            CREATE TABLE IF NOT EXISTS customers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                tier TEXT NOT NULL DEFAULT 'starter',
                status TEXT NOT NULL DEFAULT 'active',
                email TEXT NOT NULL,
                phone TEXT,
                health_score INTEGER NOT NULL DEFAULT 100,
                owner_department TEXT NOT NULL DEFAULT 'Customer Success',
                owner_user_id TEXT,
                tags_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS customer_interactions (
                id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                interaction_type TEXT NOT NULL,
                channel TEXT NOT NULL DEFAULT 'internal',
                summary TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}',
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS departments (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                cost_center TEXT,
                head_user_id TEXT,
                parent_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS employees (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                email TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                department_id TEXT NOT NULL,
                manager_id TEXT,
                title TEXT,
                employment_status TEXT NOT NULL DEFAULT 'active',
                location TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS assets (
                id TEXT PRIMARY KEY,
                hostname TEXT,
                asset_type TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                environment TEXT NOT NULL DEFAULT 'dev',
                owner_user_id TEXT,
                department_id TEXT,
                model TEXT,
                serial TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                criticality TEXT NOT NULL DEFAULT 'normal',
                patch_level TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tickets (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                customer_id TEXT,
                status TEXT NOT NULL,
                priority TEXT NOT NULL,
                owner_department TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                provider TEXT NOT NULL DEFAULT 'mock',
                external_id TEXT,
                external_url TEXT,
                idempotency_key TEXT,
                external_payload_json TEXT NOT NULL DEFAULT '{}',
                due_at TEXT,
                resolved_at TEXT,
                closed_at TEXT,
                requester_user_id TEXT,
                it_category TEXT,
                service TEXT,
                asset_id TEXT,
                environment TEXT,
                triage_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (customer_id) REFERENCES customers(id)
            );

            CREATE TABLE IF NOT EXISTS ticket_events (
                id TEXT PRIMARY KEY,
                ticket_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                body TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (ticket_id) REFERENCES tickets(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS emails (
                id TEXT PRIMARY KEY,
                to_address TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                status TEXT NOT NULL,
                approval_id TEXT,
                provider TEXT NOT NULL DEFAULT 'mock',
                external_message_id TEXT,
                error_message TEXT,
                idempotency_key TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT
            );

            CREATE TABLE IF NOT EXISTS external_outbox (
                id TEXT PRIMARY KEY,
                action_type TEXT NOT NULL,
                provider TEXT NOT NULL,
                target_type TEXT,
                target_id TEXT,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                idempotency_key TEXT,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}',
                response_json TEXT NOT NULL DEFAULT '{}',
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                next_attempt_at TEXT,
                completed_at TEXT
            );

            -- ``visibility`` is inert. Nothing in this codebase filters on it:
            -- ``get_article``, ``list_articles`` and ``search_knowledge`` are
            -- all unconditional, so an article marked ``restricted`` is
            -- returned to every caller exactly like an ``internal`` one. The
            -- column is kept because removing it is a migration this version
            -- does not need, and every writer sets it to 'internal'. It is not
            -- an access-control field; see ``tools/knowledge.py:create_article``.
            CREATE TABLE IF NOT EXISTS knowledge_articles (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT NOT NULL,
                visibility TEXT NOT NULL DEFAULT 'internal',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id TEXT PRIMARY KEY,
                actor TEXT NOT NULL,
                event_type TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                detail_json TEXT NOT NULL,
                previous_hash TEXT,
                row_hash TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS eval_reports (
                id TEXT PRIMARY KEY,
                total_count INTEGER NOT NULL,
                passed_count INTEGER NOT NULL,
                pass_rate REAL NOT NULL,
                tool_accuracy REAL NOT NULL,
                approval_accuracy REAL NOT NULL,
                avg_latency_ms REAL NOT NULL,
                report_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            -- How past IT incidents were actually handled. Deliberately its own
            -- table rather than rows in ``knowledge_articles``: an article is an
            -- approved policy or runbook and a historical ticket is one
            -- engineer's past improvisation, and a resolution must be able to
            -- follow the first while merely noting the second. Keeping them
            -- apart is what makes that distinction checkable rather than a
            -- matter of prompt wording.
            CREATE TABLE IF NOT EXISTS it_historical_tickets (
                id TEXT PRIMARY KEY,
                ticket_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                resolution TEXT NOT NULL,
                resolution_action TEXT,
                environment TEXT,
                asset_id TEXT,
                status TEXT NOT NULL,
                resolved_at TEXT,
                created_at TEXT NOT NULL
            );

            -- One row per LLM call that was actually attempted. Its own table
            -- rather than columns on ``workflow_runs`` or ``multi_agent_runs``
            -- because a single run makes several calls with different outcomes,
            -- and an average hides exactly the thing worth seeing: which call
            -- failed, how, and how long the retry ladder took.
            --
            -- ``usage_available`` is stored rather than inferred from a NULL
            -- token count, so "the provider told us nothing" stays tellable
            -- apart from "the provider told us zero".
            CREATE TABLE IF NOT EXISTS llm_calls (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                operation TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                error_type TEXT,
                error_message TEXT,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0,
                fallback_used INTEGER NOT NULL DEFAULT 0,
                usage_available INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                schema_name TEXT,
                multi_agent_run_id TEXT,
                workflow_run_id TEXT,
                ticket_id TEXT
            );
            """
        )
        _ensure_column(conn, "workflow_steps", "attempt_count", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "business_requests", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
        _ensure_column(conn, "users", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
        _ensure_column(conn, "workflow_runs", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
        _ensure_column(conn, "workflow_runs", "ticket_id", "TEXT")
        _ensure_column(conn, "workflow_jobs", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
        _ensure_column(conn, "approvals", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
        _ensure_column(conn, "customers", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
        _ensure_column(conn, "customers", "status", "TEXT NOT NULL DEFAULT 'active'")
        _ensure_column(conn, "customers", "phone", "TEXT")
        _ensure_column(conn, "customers", "owner_department", "TEXT NOT NULL DEFAULT 'Customer Success'")
        _ensure_column(conn, "customers", "owner_user_id", "TEXT")
        _ensure_column(conn, "customers", "tags_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "customers", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "tickets", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
        _ensure_column(conn, "emails", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
        _ensure_column(conn, "audit_logs", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
        _ensure_column(conn, "workflow_steps", "max_attempts", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "workflow_steps", "retryable", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "workflow_steps", "error_type", "TEXT")
        _ensure_column(conn, "workflow_steps", "attempts_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "multi_agent_runs", "executor_type", "TEXT NOT NULL DEFAULT 'durable_langgraph'")
        _ensure_column(conn, "multi_agent_runs", "thread_id", "TEXT")
        _ensure_column(conn, "multi_agent_runs", "correction_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "multi_agent_runs", "replay_of_run_id", "TEXT")
        _ensure_column(conn, "multi_agent_runs", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
        _ensure_column(conn, "multi_agent_runs", "requester_role", "TEXT")
        # Nullable, no default: a non-IT run keeps NULL and its stored state is
        # byte-identical to before. Without this the trace replayer would re-run
        # an IT run in non-IT mode and emit a structurally different trace.
        _ensure_column(conn, "multi_agent_runs", "it_ticket_id", "TEXT")
        _ensure_column(conn, "multi_agent_tasks", "duration_ms", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "agent_memory", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
        _ensure_column(conn, "golden_traces", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
        _ensure_column(conn, "trace_replays", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
        _ensure_column(conn, "tickets", "provider", "TEXT NOT NULL DEFAULT 'mock'")
        _ensure_column(conn, "tickets", "external_id", "TEXT")
        _ensure_column(conn, "tickets", "external_url", "TEXT")
        _ensure_column(conn, "tickets", "idempotency_key", "TEXT")
        _ensure_column(conn, "tickets", "external_payload_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "tickets", "due_at", "TEXT")
        _ensure_column(conn, "tickets", "resolved_at", "TEXT")
        _ensure_column(conn, "tickets", "closed_at", "TEXT")
        _ensure_column(conn, "tickets", "requester_user_id", "TEXT")
        _ensure_column(conn, "tickets", "it_category", "TEXT")
        _ensure_column(conn, "tickets", "service", "TEXT")
        _ensure_column(conn, "tickets", "asset_id", "TEXT")
        _ensure_column(conn, "tickets", "environment", "TEXT")
        _ensure_column(conn, "tickets", "triage_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "emails", "provider", "TEXT NOT NULL DEFAULT 'mock'")
        _ensure_column(conn, "emails", "external_message_id", "TEXT")
        _ensure_column(conn, "emails", "error_message", "TEXT")
        _ensure_column(conn, "emails", "idempotency_key", "TEXT")
        _ensure_column(conn, "external_outbox", "target_type", "TEXT")
        _ensure_column(conn, "external_outbox", "target_id", "TEXT")
        _ensure_column(conn, "external_outbox", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
        _ensure_column(conn, "external_outbox", "idempotency_key", "TEXT")
        _ensure_column(conn, "external_outbox", "next_attempt_at", "TEXT")
        _ensure_column(conn, "external_outbox", "completed_at", "TEXT")
        _ensure_column(conn, "auth_login_attempts", "ip_address", "TEXT")
        _ensure_column(conn, "auth_login_attempts", "user_agent", "TEXT")
        _ensure_column(conn, "auth_login_attempts", "lockout_until", "TEXT")
        _ensure_column(conn, "audit_logs", "previous_hash", "TEXT")
        _ensure_column(conn, "audit_logs", "row_hash", "TEXT")
        _ensure_customer_email_tenant_scope(conn)
        _ensure_indexes(conn)
        _ensure_postgres_rls(conn)
        _record_schema_migration(conn, BASELINE_MIGRATION_ID, BASELINE_MIGRATION_DESCRIPTION)
        _record_schema_migration(conn, IDEMPOTENCY_MIGRATION_ID, IDEMPOTENCY_MIGRATION_DESCRIPTION)
        _record_schema_migration(conn, AUTH_SESSION_MIGRATION_ID, AUTH_SESSION_MIGRATION_DESCRIPTION)
        _record_schema_migration(conn, OUTBOX_MIGRATION_ID, OUTBOX_MIGRATION_DESCRIPTION)
        _record_schema_migration(conn, AUTH_LOGIN_PROTECTION_MIGRATION_ID, AUTH_LOGIN_PROTECTION_MIGRATION_DESCRIPTION)
        _record_schema_migration(conn, AUDIT_HASH_CHAIN_MIGRATION_ID, AUDIT_HASH_CHAIN_MIGRATION_DESCRIPTION)
        _record_schema_migration(conn, TENANT_ISOLATION_MIGRATION_ID, TENANT_ISOLATION_MIGRATION_DESCRIPTION)
        _record_schema_migration(conn, POSTGRES_RLS_MIGRATION_ID, POSTGRES_RLS_MIGRATION_DESCRIPTION)
        _record_schema_migration(conn, POSTGRES_RLS_BYPASS_ROLE_MIGRATION_ID, POSTGRES_RLS_BYPASS_ROLE_MIGRATION_DESCRIPTION)
        _record_schema_migration(conn, WORKFLOW_RUN_TICKET_LINK_MIGRATION_ID, WORKFLOW_RUN_TICKET_LINK_MIGRATION_DESCRIPTION)
        _record_schema_migration(conn, CRM_FOUNDATION_MIGRATION_ID, CRM_FOUNDATION_MIGRATION_DESCRIPTION)
        _record_schema_migration(conn, TICKET_TIMELINE_MIGRATION_ID, TICKET_TIMELINE_MIGRATION_DESCRIPTION)
        _record_schema_migration(conn, TICKET_SLA_MIGRATION_ID, TICKET_SLA_MIGRATION_DESCRIPTION)
        _record_schema_migration(conn, IT_SERVICE_FOUNDATION_MIGRATION_ID, IT_SERVICE_FOUNDATION_MIGRATION_DESCRIPTION)
        _record_schema_migration(conn, IT_RUN_TICKET_LINK_MIGRATION_ID, IT_RUN_TICKET_LINK_MIGRATION_DESCRIPTION)
        _record_schema_migration(
            conn, IT_HISTORICAL_TICKETS_MIGRATION_ID, IT_HISTORICAL_TICKETS_MIGRATION_DESCRIPTION
        )
    if settings.auto_seed if seed is None else seed:
        seed_demo_data(purpose="migration")
        seed_it_data(purpose="migration")


def database_status(*, purpose: str = "readonly") -> dict[str, Any]:
    try:
        with get_connection(purpose=purpose) as conn:
            if settings.auto_migrate:
                _ensure_schema_migrations(conn)
            latest = conn.execute(
                """
                SELECT id, description, checksum, applied_at
                FROM schema_migrations
                ORDER BY applied_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            migrations = conn.execute("SELECT COUNT(*) AS count FROM schema_migrations").fetchone()
            rls = postgres_rls_status(conn)
        return {
            "status": "ok",
            "backend": settings.db_backend,
            "sqlite_path": str(settings.db_path) if settings.db_backend == "sqlite" else None,
            "postgres_schema": settings.postgres_schema if settings.db_backend == "postgres" else None,
            "auto_migrate": settings.auto_migrate,
            "connection_profiles": database_connection_profiles(),
            "postgres_rls": rls,
            "migration_count": int(migrations["count"]) if migrations else 0,
            "latest_migration": row_to_dict(latest),
        }
    except Exception as exc:
        return {
            "status": "error",
            "backend": settings.db_backend,
            "sqlite_path": str(settings.db_path) if settings.db_backend == "sqlite" else None,
            "postgres_schema": settings.postgres_schema if settings.db_backend == "postgres" else None,
            "auto_migrate": settings.auto_migrate,
            "connection_profiles": database_connection_profiles(),
            "postgres_rls": {
                "enabled": settings.postgres_rls_enabled,
                "bypass_role": settings.postgres_rls_bypass_role or None,
            },
            "error": str(exc),
        }


def list_schema_migrations(*, purpose: str = "readonly") -> list[dict[str, Any]]:
    with get_connection(purpose=purpose) as conn:
        if settings.auto_migrate:
            _ensure_schema_migrations(conn)
        rows = conn.execute(
            """
            SELECT *
            FROM schema_migrations
            ORDER BY applied_at DESC, id DESC
            """
        ).fetchall()
    return rows_to_dicts(rows)


def postgres_rls_status(conn: sqlite3.Connection | PostgresConnection | None = None) -> dict[str, Any]:
    status = {
        "enabled": settings.postgres_rls_enabled,
        "bypass_role": settings.postgres_rls_bypass_role or None,
        "role_gate_configured": bool(settings.postgres_rls_bypass_role),
    }
    if settings.db_backend != "postgres":
        return status

    owns_connection = conn is None
    connection = conn or get_connection(purpose="readonly")
    try:
        role = settings.postgres_rls_bypass_role
        if role:
            role_row = connection.execute(
                """
                SELECT
                    current_user AS current_user,
                    to_regrole(?) IS NOT NULL AS bypass_role_exists,
                    CASE
                        WHEN to_regrole(?) IS NULL THEN false
                        ELSE pg_has_role(current_user, to_regrole(?), 'member')
                    END AS current_user_has_bypass_role
                """,
                (role, role, role),
            ).fetchone()
            status.update(row_to_dict(role_row) or {})
        policy_row = connection.execute(
            """
            SELECT COUNT(*) AS tenant_policy_count
            FROM pg_policies
            WHERE schemaname = ? AND policyname = 'tenant_isolation'
            """,
            (connection.schema,),
        ).fetchone()
        function_row = connection.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = ? AND p.proname = 'agent_rls_has_bypass_role'
            ) AS bypass_function_exists
            """,
            (connection.schema,),
        ).fetchone()
        status.update(row_to_dict(policy_row) or {})
        status.update(row_to_dict(function_row) or {})
    finally:
        if owns_connection:
            connection.close()
    return status


def _ensure_schema_migrations(conn: sqlite3.Connection | PostgresConnection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def _record_schema_migration(
    conn: sqlite3.Connection | PostgresConnection,
    migration_id: str,
    description: str,
) -> None:
    checksum = hashlib.sha256(f"{migration_id}:{description}".encode("utf-8")).hexdigest()
    existing = conn.execute("SELECT id FROM schema_migrations WHERE id = ?", (migration_id,)).fetchone()
    if existing:
        return
    conn.execute(
        """
        INSERT INTO schema_migrations (id, description, checksum, applied_at)
        VALUES (?, ?, ?, ?)
        """,
        (migration_id, description, checksum, utc_now()),
    )


def _ensure_indexes(conn: sqlite3.Connection | PostgresConnection) -> None:
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_business_requests_status_created
            ON business_requests(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_business_requests_tenant_created
            ON business_requests(tenant_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_created
            ON auth_sessions(user_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires
            ON auth_sessions(expires_at);
        CREATE INDEX IF NOT EXISTS idx_auth_login_attempts_user_created
            ON auth_login_attempts(user_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_auth_login_attempts_lockout
            ON auth_login_attempts(user_id, lockout_until);
        CREATE INDEX IF NOT EXISTS idx_workflow_runs_status_created
            ON workflow_runs(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_workflow_runs_tenant_created
            ON workflow_runs(tenant_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_workflow_runs_request_created
            ON workflow_runs(request_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_workflow_jobs_status_created
            ON workflow_jobs(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_workflow_jobs_tenant_created
            ON workflow_jobs(tenant_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_workflow_steps_run_index
            ON workflow_steps(run_id, step_index);
        CREATE INDEX IF NOT EXISTS idx_multi_agent_runs_status_created
            ON multi_agent_runs(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_multi_agent_runs_tenant_created
            ON multi_agent_runs(tenant_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_multi_agent_messages_run_created
            ON multi_agent_messages(run_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_multi_agent_tasks_run_status
            ON multi_agent_tasks(run_id, status, created_at);
        CREATE INDEX IF NOT EXISTS idx_multi_agent_handoffs_run_created
            ON multi_agent_handoffs(run_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_agent_memory_tenant_created
            ON agent_memory(tenant_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_agent_checkpoints_run_index
            ON agent_checkpoints(run_id, checkpoint_index);
        CREATE INDEX IF NOT EXISTS idx_golden_traces_tenant_created
            ON golden_traces(tenant_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_trace_replays_tenant_created
            ON trace_replays(tenant_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_approvals_status_created
            ON approvals(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_approvals_tenant_status_created
            ON approvals(tenant_id, status, created_at);
        CREATE INDEX IF NOT EXISTS idx_customers_tenant_updated
            ON customers(tenant_id, updated_at);
        CREATE INDEX IF NOT EXISTS idx_customers_tenant_status_owner
            ON customers(tenant_id, status, owner_department);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_customers_tenant_email
            ON customers(tenant_id, email);
        CREATE INDEX IF NOT EXISTS idx_customer_interactions_customer_created
            ON customer_interactions(customer_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_customer_interactions_tenant_created
            ON customer_interactions(tenant_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_tickets_status_updated
            ON tickets(status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_tickets_tenant_status_updated
            ON tickets(tenant_id, status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_tickets_external_id
            ON tickets(external_id);
        CREATE INDEX IF NOT EXISTS idx_tickets_tenant_due
            ON tickets(tenant_id, due_at);
        CREATE INDEX IF NOT EXISTS idx_ticket_events_ticket_created
            ON ticket_events(ticket_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_ticket_events_tenant_created
            ON ticket_events(tenant_id, created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_idempotency_key
            ON tickets(idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_emails_status_created
            ON emails(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_emails_tenant_created
            ON emails(tenant_id, created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_emails_idempotency_key
            ON emails(idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_external_outbox_status_created
            ON external_outbox(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_external_outbox_tenant_status_created
            ON external_outbox(tenant_id, status, created_at);
        CREATE INDEX IF NOT EXISTS idx_external_outbox_target
            ON external_outbox(target_type, target_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_external_outbox_idempotency_key
            ON external_outbox(idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_knowledge_articles_category
            ON knowledge_articles(category);
        CREATE INDEX IF NOT EXISTS idx_audit_logs_created
            ON audit_logs(created_at);
        CREATE INDEX IF NOT EXISTS idx_audit_logs_tenant_created
            ON audit_logs(tenant_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_audit_logs_event_target
            ON audit_logs(event_type, target_type, target_id);
        CREATE INDEX IF NOT EXISTS idx_llm_calls_created
            ON llm_calls(created_at);
        CREATE INDEX IF NOT EXISTS idx_llm_calls_tenant_created
            ON llm_calls(tenant_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_llm_calls_operation_status
            ON llm_calls(operation, status);
        CREATE INDEX IF NOT EXISTS idx_llm_calls_multi_agent_run
            ON llm_calls(multi_agent_run_id);
        CREATE INDEX IF NOT EXISTS idx_audit_logs_row_hash
            ON audit_logs(row_hash);
        CREATE INDEX IF NOT EXISTS idx_employees_tenant_dept
            ON employees(tenant_id, department_id);
        CREATE INDEX IF NOT EXISTS idx_assets_tenant_env
            ON assets(tenant_id, environment);
        CREATE INDEX IF NOT EXISTS idx_assets_owner
            ON assets(owner_user_id);
        CREATE INDEX IF NOT EXISTS idx_tickets_tenant_it_category
            ON tickets(tenant_id, it_category);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_it_history_ticket_id
            ON it_historical_tickets(ticket_id);
        CREATE INDEX IF NOT EXISTS idx_it_history_tenant_category
            ON it_historical_tickets(tenant_id, category);
        """
    )


def _ensure_postgres_rls(conn: sqlite3.Connection | PostgresConnection) -> None:
    if not is_postgres_connection(conn) or not settings.postgres_rls_enabled:
        return
    conn.execute(
        """
        CREATE OR REPLACE FUNCTION agent_rls_has_bypass_role()
        RETURNS boolean
        LANGUAGE plpgsql
        STABLE
        AS $$
        DECLARE
            configured_role text := NULLIF(current_setting('app.rls_bypass_role', true), '');
            configured_role_oid oid;
        BEGIN
            IF configured_role IS NULL THEN
                RETURN true;
            END IF;
            configured_role_oid := to_regrole(configured_role);
            IF configured_role_oid IS NULL THEN
                RETURN false;
            END IF;
            RETURN pg_has_role(current_user, configured_role_oid, 'member');
        END;
        $$;
        """
    )
    tenant_expr = "COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), 'default')"
    bypass_expr = "(current_setting('app.rls_bypass', true) = 'on' AND agent_rls_has_bypass_role())"
    for table in TENANT_RLS_TABLES:
        quoted_table = quote_identifier(table)
        predicate = f"({bypass_expr} OR tenant_id = {tenant_expr})"
        conn.execute(f"ALTER TABLE {quoted_table} ENABLE ROW LEVEL SECURITY")
        conn.execute(f"ALTER TABLE {quoted_table} FORCE ROW LEVEL SECURITY")
        conn.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {quoted_table}")
        conn.execute(
            f"""
            CREATE POLICY tenant_isolation ON {quoted_table}
            USING ({predicate})
            WITH CHECK ({predicate})
            """
        )

    for table, parent_table, foreign_key in RELATED_RLS_TABLES:
        quoted_table = quote_identifier(table)
        quoted_parent = quote_identifier(parent_table)
        quoted_fk = quote_identifier(foreign_key)
        predicate = (
            f"{bypass_expr} OR EXISTS ("
            f"SELECT 1 FROM {quoted_parent} parent "
            f"WHERE parent.id = {quoted_table}.{quoted_fk} "
            f"AND (parent.tenant_id = {tenant_expr} OR {bypass_expr})"
            f")"
        )
        conn.execute(f"ALTER TABLE {quoted_table} ENABLE ROW LEVEL SECURITY")
        conn.execute(f"ALTER TABLE {quoted_table} FORCE ROW LEVEL SECURITY")
        conn.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {quoted_table}")
        conn.execute(
            f"""
            CREATE POLICY tenant_isolation ON {quoted_table}
            USING ({predicate})
            WITH CHECK ({predicate})
            """
        )


def _ensure_column(conn: sqlite3.Connection | PostgresConnection, table: str, column: str, definition: str) -> None:
    if is_postgres_connection(conn):
        row = conn.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = ? AND table_name = ? AND column_name = ?
            LIMIT 1
            """,
            (conn.schema, table, column),
        ).fetchone()
        if not row:
            conn.execute(f"ALTER TABLE {quote_identifier(table)} ADD COLUMN {quote_identifier(column)} {definition}")
        return
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _ensure_customer_email_tenant_scope(conn: sqlite3.Connection | PostgresConnection) -> None:
    """Upgrade the legacy global email uniqueness to tenant + email uniqueness."""
    if is_postgres_connection(conn):
        conn.execute("ALTER TABLE customers DROP CONSTRAINT IF EXISTS customers_email_key")
        return
    unique_email_constraint = False
    for index in conn.execute("PRAGMA index_list(customers)").fetchall():
        if not bool(index["unique"]):
            continue
        columns = [row["name"] for row in conn.execute(f"PRAGMA index_info({quote_identifier(index['name'])})").fetchall()]
        if columns == ["email"]:
            unique_email_constraint = True
            break
    if not unique_email_constraint:
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    conn.executescript(
        """
        ALTER TABLE customers RENAME TO customers_legacy_global_email;
        CREATE TABLE customers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            tier TEXT NOT NULL DEFAULT 'starter',
            status TEXT NOT NULL DEFAULT 'active',
            email TEXT NOT NULL,
            phone TEXT,
            health_score INTEGER NOT NULL DEFAULT 100,
            owner_department TEXT NOT NULL DEFAULT 'Customer Success',
            owner_user_id TEXT,
            tags_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO customers
        (id, name, tenant_id, tier, status, email, phone, health_score, owner_department,
         owner_user_id, tags_json, metadata_json, notes, created_at, updated_at)
        SELECT id, name, tenant_id, tier, status, email, phone, health_score, owner_department,
               owner_user_id, tags_json, metadata_json, notes, created_at, updated_at
        FROM customers_legacy_global_email;
        DROP TABLE customers_legacy_global_email;
        """
    )
    conn.execute("PRAGMA legacy_alter_table = OFF")
    conn.execute("PRAGMA foreign_keys = ON")


def reset_database(seed: bool = True) -> None:
    if settings.db_backend == "postgres":
        quoted_schema = quote_identifier(settings.postgres_schema)
        with get_connection(purpose="migration") as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE")
            conn.execute(f"CREATE SCHEMA {quoted_schema}")
            conn.execute(f"SET search_path TO {quoted_schema}")
        init_db(seed=seed)
        return
    if settings.db_path.exists():
        settings.db_path.unlink()
    init_db(seed=seed)


def clear_run_history() -> dict[str, int]:
    tables = [
        "trace_replays",
        "golden_traces",
        "agent_checkpoints",
        "multi_agent_handoffs",
        "multi_agent_tasks",
        "multi_agent_messages",
        "multi_agent_runs",
        "agent_memory",
        "workflow_steps",
        "approvals",
        "emails",
        "ticket_events",
        "tickets",
        "external_outbox",
        "workflow_jobs",
        "workflow_runs",
        "business_requests",
        "eval_reports",
        "audit_logs",
        "auth_login_attempts",
    ]
    deleted: dict[str, int] = {}
    with get_connection() as conn:
        for table in tables:
            cursor = conn.execute(f"DELETE FROM {table}")
            deleted[table] = cursor.rowcount if cursor.rowcount != -1 else 0

    checkpoint_path = settings.db_path.parent / "langgraph_checkpoints.sqlite3"
    if checkpoint_path.exists():
        checkpoint_path.unlink()
        deleted["langgraph_checkpoints"] = 1
    else:
        deleted["langgraph_checkpoints"] = 0

    return deleted


def is_postgres_connection(conn: Any) -> bool:
    return getattr(conn, "backend", "") == "postgres"


def clean_identifier(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in value.strip())
    if not cleaned:
        raise ValueError("PostgreSQL schema/table identifier cannot be empty.")
    if cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    return cleaned


def quote_identifier(value: str) -> str:
    return '"' + clean_identifier(value).replace('"', '""') + '"'


def translate_sqlite_placeholders(sql: str) -> str:
    return sql.replace("?", "%s")


def split_sql_script(script: str) -> list[str]:
    return [statement.strip() for statement in script.split(";") if statement.strip()]


def seed_demo_data(purpose: str | None = None) -> None:
    with get_connection(purpose=purpose) as conn:
        existing = conn.execute("SELECT COUNT(*) AS count FROM knowledge_articles").fetchone()["count"]
        if existing:
            return
        now = utc_now()
        articles = [
            (
                "客户退款处理政策",
                "customer_success",
                "客户申请退款时，先确认客户等级、合同状态和退款金额。金额低于 500 元且无合规风险时可由客服主管直接处理；金额达到或超过 500 元时必须进入人工审批。所有退款沟通都需要创建工单并记录处理依据。",
                "refund,approval,customer,ticket",
            ),
            (
                "生产故障响应 SOP",
                "incident",
                "P1 故障需要 15 分钟内创建工单并通知值班负责人。Agent 可以自动创建故障工单、整理影响范围和建议动作，但不能自动关闭 P1 工单。涉及客户通知时需要保留邮件草稿和审计记录。",
                "incident,p1,oncall,sla",
            ),
            (
                "采购审批规则",
                "procurement",
                "软件订阅和云资源采购需要记录预算部门、金额和业务理由。金额超过 1000 元需要部门负责人审批，金额超过 5000 元还需要财务复核。Agent 可以准备审批材料，但不能绕过审批链。",
                "procurement,budget,approval",
            ),
            (
                "外部邮件发送规范",
                "compliance",
                "对外发送邮件前需要检查是否包含敏感信息、客户隐私或承诺性表述。包含退款、赔偿、合同、账号安全等内容时，必须先由人工批准。",
                "email,compliance,privacy,approval",
            ),
            (
                "账号安全事件处理",
                "security",
                "发现账号异常登录、权限泄露或疑似数据暴露时，需要创建安全工单，标记 high 优先级，并通知安全团队。Agent 不允许直接重置权限或删除数据，只能提出建议和触发审批。",
                "security,access,approval",
            ),
        ]
        for title, category, content, tags in articles:
            conn.execute(
                """
                INSERT INTO knowledge_articles
                (id, title, category, content, tags, visibility, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'internal', ?, ?)
                """,
                (new_id("kb"), title, category, content, tags, now, now),
            )

        customers = [
            ("cust_acme", "Acme China", "enterprise", "ops@acme.example", 72, "年度合同客户，关注 SLA 和响应速度。"),
            ("cust_orbit", "Orbit Retail", "growth", "support@orbit.example", 64, "近期有两次退款沟通，适合优先安抚。"),
            ("cust_nova", "Nova Studio", "starter", "hello@nova.example", 88, "小团队客户，通常通过邮件沟通。"),
        ]
        for customer in customers:
            conn.execute(
                """
                INSERT INTO customers
                (id, name, tenant_id, tier, status, email, health_score, owner_department, notes, created_at, updated_at)
                VALUES (?, ?, 'default', ?, 'active', ?, ?, 'Customer Success', ?, ?, ?)
                """,
                (*customer, now, now),
            )


def seed_it_data(purpose: str | None = None) -> None:
    """Seed the mock IT directory, its logins, and the IT knowledge base.

    Fully idempotent and entirely local: every row comes from
    ``app.services.it.mock_data`` and no external system is contacted.
    """
    _seed_it_directory(purpose=purpose)
    _ensure_it_employee_accounts()
    seed_it_knowledge(purpose=purpose)
    seed_it_history(purpose=purpose)


# The IT knowledge base the resolution loop reasons over. Content is generic
# operational guidance, written for this mock platform: no real company, runbook
# or credential appears here.
#
# Categories are deliberately ``it_service`` / ``it_policy`` rather than reusing
# ``incident`` / ``security`` / ``compliance`` from ``seed_demo_data``.
# ``search_knowledge`` awards a point when an article's category appears in the
# query text, so sharing a category with the business corpus would let business
# articles bleed into IT retrieval (and vice versa) for no benefit.
IT_KNOWLEDGE_ARTICLES: tuple[tuple[str, str, str, str], ...] = (
    (
        "Redis 生产故障排查手册",
        "it_service",
        "Redis 连接不可用时，按只读诊断优先的顺序处理：先确认实例是否存在且状态为 active，再检查内存压力、连接数饱和与近期错误，"
        "这些都可以通过 diagnose_service 只读完成，不需要授权。确认是内存碎片或热点 key 导致响应变慢时，可以清理缓存 flush_cache，"
        "该操作可逆，非生产环境可直接执行，生产环境必须先取得人工审批。确认实例已经无响应时，标准处置是重启服务 restart_service，"
        "重启会造成秒级中断，因此无论环境都必须先经过人工审批，生产环境还需要 it_admin 权限。"
        "任何情况下都不允许删除数据、回收权限或停用账号，这些动作在风险门禁中被直接拒绝。",
        "redis,incident,restart,cache,production",
    ),
    (
        "IT 生产变更与权限管理规范",
        "it_policy",
        "所有生产环境的变更都必须留下审批记录，Agent 可以准备方案和影响范围，但不能自行执行。"
        "权限申请需要记录申请人、目标资源、权限级别和业务理由；只读权限由资源负责人审批，读写权限必须由 IT 管理员审批。"
        "生产资源的任何权限授予都需要 IT 管理员审批，不论级别。"
        "权限回收、数据删除和账号停用属于高危动作，不在自动化范围内，必须由人工在平台之外处理。",
        "permission,approval,access,production,policy",
    ),
    (
        "IT 事件分级与响应时限",
        "it_policy",
        "生产环境中核心服务不可用属于紧急事件，包括 redis、数据库、vpn 网关和网络设备，需要在 15 分钟内响应并创建工单。"
        "非生产环境的故障默认按普通优先级处理。"
        "当请求中缺少服务名称或环境信息时，应当先向报障人补充信息，而不是推测一个答案后继续处理。",
        "incident,priority,sla,urgent",
    ),
    (
        "员工设备与软件申请指引",
        "it_service",
        "设备申请需要直属主管确认，资产从 IT 库存中分配并登记归属人。"
        "免费软件可以自助安装，付费或需要授权的软件需要走采购审批，并在工单中记录预算部门和业务理由。"
        "软件申请本身不涉及生产变更，通常只需要分派给对应处理团队。",
        "asset,software,laptop,docker",
    ),
)


def seed_it_knowledge(purpose: str | None = None) -> None:
    """Seed the deterministic IT knowledge base used by the resolution loop.

    Idempotent **per document**, matched on ``title``. This is deliberately not
    ``seed_demo_data``'s ``SELECT COUNT(*) FROM knowledge_articles`` guard: that
    one returns early if *any* article exists, which would silently skip the IT
    corpus whenever the business corpus was seeded first. Matching on title means
    the two seeders compose in either order and either one can be re-run.
    """
    with get_connection(purpose=purpose) as conn:
        existing = {
            row["title"]
            for row in conn.execute("SELECT title FROM knowledge_articles").fetchall()
        }
        now = utc_now()
        for title, category, content, tags in IT_KNOWLEDGE_ARTICLES:
            if title in existing:
                continue
            conn.execute(
                """
                INSERT INTO knowledge_articles
                (id, title, category, content, tags, visibility, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'internal', ?, ?)
                """,
                (new_id("kb"), title, category, content, tags, now, now),
            )


def seed_it_history(purpose: str | None = None) -> None:
    """Seed the historical IT ticket corpus the resolution agent reads from.

    Idempotent **per ticket**, matched on ``ticket_id``, for the same reason as
    ``seed_it_knowledge``: this runs on every ``init_db(seed=True)`` and on the
    admin seed endpoint, so it has to be safe to re-run and safe to combine with
    either of the other seeders in any order.

    The corpus is reference material only. It is never joined against ``tickets``
    and never drives a state transition.
    """
    from app.services.it.mock_data import MOCK_HISTORICAL_TICKETS

    with get_connection(purpose=purpose) as conn:
        existing = {
            row["ticket_id"]
            for row in conn.execute("SELECT ticket_id FROM it_historical_tickets").fetchall()
        }
        now = utc_now()
        for ticket in MOCK_HISTORICAL_TICKETS:
            ticket_id = ticket["ticket_id"]
            if ticket_id in existing:
                continue
            conn.execute(
                """
                INSERT INTO it_historical_tickets
                (id, ticket_id, tenant_id, category, title, description, resolution,
                 resolution_action, environment, asset_id, status, resolved_at, created_at)
                VALUES (?, ?, 'default', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("ithist"),
                    ticket_id,
                    ticket["category"],
                    ticket["title"],
                    ticket["description"],
                    ticket["resolution"],
                    ticket.get("resolution_action"),
                    ticket.get("environment"),
                    ticket.get("asset_id"),
                    ticket["status"],
                    now,
                    now,
                ),
            )


def _seed_it_directory(purpose: str | None = None) -> None:
    # Imported lazily: ``mock_data`` deliberately imports nothing from ``app.*``
    # so this stays a one-way dependency.
    from app.services.it.mock_data import MOCK_ASSETS, MOCK_DEPARTMENTS, MOCK_EMPLOYEES

    with get_connection(purpose=purpose) as conn:
        existing = conn.execute("SELECT COUNT(*) AS count FROM departments").fetchone()["count"]
        if existing:
            return
        now = utc_now()
        for department in MOCK_DEPARTMENTS:
            conn.execute(
                """
                INSERT INTO departments
                (id, name, tenant_id, cost_center, head_user_id, parent_id, created_at, updated_at)
                VALUES (?, ?, 'default', ?, ?, ?, ?, ?)
                """,
                (
                    department["id"],
                    department["name"],
                    department.get("cost_center"),
                    department.get("head_user_id"),
                    department.get("parent_id"),
                    now,
                    now,
                ),
            )
        for employee in MOCK_EMPLOYEES:
            conn.execute(
                """
                INSERT INTO employees
                (id, display_name, email, tenant_id, department_id, manager_id, title,
                 employment_status, location, created_at, updated_at)
                VALUES (?, ?, ?, 'default', ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    employee["id"],
                    employee["display_name"],
                    employee["email"],
                    employee["department_id"],
                    employee.get("manager_id"),
                    employee.get("title"),
                    employee.get("location"),
                    now,
                    now,
                ),
            )
        for asset in MOCK_ASSETS:
            conn.execute(
                """
                INSERT INTO assets
                (id, hostname, asset_type, tenant_id, environment, owner_user_id, department_id,
                 model, serial, status, criticality, patch_level, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, 'default', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset["id"],
                    asset.get("hostname"),
                    asset["asset_type"],
                    asset["environment"],
                    asset.get("owner_user_id"),
                    asset.get("department_id"),
                    asset.get("model"),
                    asset.get("serial"),
                    asset.get("status", "active"),
                    asset.get("criticality", "normal"),
                    asset.get("patch_level"),
                    json_dumps(asset.get("metadata") or {}),
                    now,
                    now,
                ),
            )


def _ensure_it_employee_accounts() -> None:
    """Give every mock employee a login.

    ``AuthContext.user_id`` is the same value as ``employees.id``, so an
    authenticated caller resolves against the directory without a mapping table.
    Uses the same ``get_user`` guard as ``ensure_demo_users`` so an existing
    password is never rewritten.
    """
    from app.services.auth import create_user, get_user
    from app.services.it.mock_data import MOCK_EMPLOYEES

    for employee in MOCK_EMPLOYEES:
        if not get_user(employee["id"]):
            create_user(
                employee["id"],
                employee["display_name"],
                employee["department_id"],
                employee["role"],
                employee["password"],
            )
