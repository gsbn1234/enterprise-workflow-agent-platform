from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from app.config import settings
from app.utils import json_dumps, new_id, utc_now


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [row_to_dict(row) for row in rows if row is not None]


def init_db(seed: bool | None = None) -> None:
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS business_requests (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                requester_user_id TEXT,
                requester_department TEXT,
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
                password_hash TEXT NOT NULL,
                disabled INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS workflow_runs (
                id TEXT PRIMARY KEY,
                request_id TEXT,
                objective TEXT NOT NULL,
                status TEXT NOT NULL,
                category TEXT,
                risk_level TEXT,
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

            CREATE TABLE IF NOT EXISTS agent_memory (
                id TEXT PRIMARY KEY,
                memory_type TEXT NOT NULL,
                memory_key TEXT NOT NULL,
                summary TEXT NOT NULL,
                detail_json TEXT NOT NULL,
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
                trace_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (source_run_id) REFERENCES multi_agent_runs(id)
            );

            CREATE TABLE IF NOT EXISTS trace_replays (
                id TEXT PRIMARY KEY,
                source_run_id TEXT NOT NULL,
                replay_run_id TEXT NOT NULL,
                mode TEXT NOT NULL,
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
                tier TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                health_score INTEGER NOT NULL,
                notes TEXT NOT NULL,
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
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (customer_id) REFERENCES customers(id)
            );

            CREATE TABLE IF NOT EXISTS emails (
                id TEXT PRIMARY KEY,
                to_address TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL,
                approval_id TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT
            );

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
                detail_json TEXT NOT NULL,
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
            """
        )
        _ensure_column(conn, "workflow_steps", "attempt_count", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "workflow_steps", "max_attempts", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "workflow_steps", "retryable", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "workflow_steps", "error_type", "TEXT")
        _ensure_column(conn, "workflow_steps", "attempts_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "multi_agent_runs", "executor_type", "TEXT NOT NULL DEFAULT 'durable_langgraph'")
        _ensure_column(conn, "multi_agent_runs", "thread_id", "TEXT")
        _ensure_column(conn, "multi_agent_runs", "correction_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "multi_agent_runs", "replay_of_run_id", "TEXT")
    if settings.auto_seed if seed is None else seed:
        seed_demo_data()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def reset_database(seed: bool = True) -> None:
    if settings.db_path.exists():
        settings.db_path.unlink()
    init_db(seed=seed)


def seed_demo_data() -> None:
    with get_connection() as conn:
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
                (id, name, tier, email, health_score, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*customer, now, now),
            )
