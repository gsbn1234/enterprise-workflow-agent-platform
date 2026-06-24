from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_AUTH_REQUIRED"] = "true"
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "retention_smoke_test.sqlite3")
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
os.environ["AGENT_AUDIT_RETENTION_DAYS"] = "1"
os.environ["AGENT_EVAL_REPORT_RETENTION_DAYS"] = "1"
os.environ["AGENT_EXTERNAL_OUTBOX_RETENTION_DAYS"] = "1"
os.environ["AGENT_EMAIL_RETENTION_DAYS"] = "1"
os.environ["AGENT_WORKFLOW_JOB_RETENTION_DAYS"] = "1"
os.environ["AGENT_LOGIN_ATTEMPT_RETENTION_DAYS"] = "1"
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import get_connection, reset_database  # noqa: E402
from app.main import app  # noqa: E402
from app.services.auth import ensure_demo_users  # noqa: E402
from app.utils import utc_now  # noqa: E402


OLD = "2000-01-01T00:00:00+00:00"


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def seed_expired_records() -> None:
    now = utc_now()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO audit_logs (id, actor, event_type, target_type, target_id, detail_json, created_at)
            VALUES ('old_audit', 'tester', 'retention.old', 'test', 'old', '{}', ?)
            """,
            (OLD,),
        )
        conn.execute(
            """
            INSERT INTO eval_reports
            (id, total_count, passed_count, pass_rate, tool_accuracy, approval_accuracy, avg_latency_ms, report_json, created_at)
            VALUES ('old_eval', 1, 1, 1.0, 1.0, 1.0, 1.0, '{}', ?)
            """,
            (OLD,),
        )
        conn.execute(
            """
            INSERT INTO external_outbox
            (id, action_type, provider, target_type, target_id, idempotency_key, status,
             attempt_count, payload_json, response_json, error_message, created_at, updated_at,
             next_attempt_at, completed_at)
            VALUES ('old_outbox_done', 'ticket.create', 'mock', 'ticket', 'ticket_old', 'old-done',
                    'completed', 1, '{}', '{}', NULL, ?, ?, NULL, ?)
            """,
            (OLD, OLD, OLD),
        )
        conn.execute(
            """
            INSERT INTO external_outbox
            (id, action_type, provider, target_type, target_id, idempotency_key, status,
             attempt_count, payload_json, response_json, error_message, created_at, updated_at,
             next_attempt_at, completed_at)
            VALUES ('old_outbox_pending', 'ticket.create', 'mock', NULL, NULL, 'old-pending',
                    'pending', 0, '{}', '{}', NULL, ?, ?, NULL, NULL)
            """,
            (OLD, OLD),
        )
        conn.execute(
            """
            INSERT INTO emails
            (id, to_address, subject, body, status, approval_id, provider, external_message_id,
             error_message, idempotency_key, created_at, sent_at)
            VALUES ('old_email', 'customer@example.com', 'Old email', 'Body', 'sent', NULL,
                    'mock', 'msg-old', NULL, 'email-old', ?, ?)
            """,
            (OLD, OLD),
        )
        conn.execute(
            """
            INSERT INTO workflow_jobs
            (id, objective, request_id, requester_user_id, requester_department, status, attempts,
             max_attempts, run_id, error_message, created_at, updated_at, started_at, completed_at)
            VALUES ('old_job_done', 'Old completed job', NULL, 'alice', 'Operations', 'completed',
                    1, 3, NULL, NULL, ?, ?, ?, ?)
            """,
            (OLD, OLD, OLD, OLD),
        )
        conn.execute(
            """
            INSERT INTO auth_login_attempts
            (id, user_id, success, failure_reason, ip_address, user_agent, lockout_until, created_at)
            VALUES ('old_login_attempt', 'alice', 0, 'bad_password', '127.0.0.1', 'smoke', NULL, ?)
            """,
            (OLD,),
        )
        conn.execute(
            """
            INSERT INTO audit_logs (id, actor, event_type, target_type, target_id, detail_json, created_at)
            VALUES ('fresh_audit', 'tester', 'retention.fresh', 'test', 'fresh', '{}', ?)
            """,
            (now,),
        )


def fetch_count(table: str, row_id: str) -> int:
    with get_connection() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE id = ?", (row_id,)).fetchone()
    return int(row["count"])


def policy_map(payload: dict) -> dict[str, dict]:
    return {policy["name"]: policy for policy in payload["policies"]}


def main() -> None:
    reset_database(seed=True)
    ensure_demo_users()
    seed_expired_records()

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"user_id": "admin", "password": "AdminPass123"})
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]

        plan = client.get("/api/admin/retention/plan", headers=auth_header(token))
        assert plan.status_code == 200, plan.text
        planned = policy_map(plan.json())
        for name in [
            "audit_logs",
            "eval_reports",
            "external_outbox",
            "emails",
            "workflow_jobs",
            "auth_login_attempts",
        ]:
            assert planned[name]["matched_count"] >= 1, planned[name]

        applied = client.post("/api/admin/retention/apply", headers=auth_header(token))
        assert applied.status_code == 200, applied.text
        payload = applied.json()
        assert payload["total_deleted"] >= 6, payload

        assert fetch_count("audit_logs", "old_audit") == 0
        assert fetch_count("eval_reports", "old_eval") == 0
        assert fetch_count("external_outbox", "old_outbox_done") == 0
        assert fetch_count("emails", "old_email") == 0
        assert fetch_count("workflow_jobs", "old_job_done") == 0
        assert fetch_count("auth_login_attempts", "old_login_attempt") == 0
        assert fetch_count("external_outbox", "old_outbox_pending") == 1
        assert fetch_count("audit_logs", "fresh_audit") == 1

        dashboard = client.get("/api/admin/operations-dashboard", headers=auth_header(token))
        assert dashboard.status_code == 200, dashboard.text
        retention = dashboard.json()["retention"]
        assert retention["enabled"] is True, retention
        assert retention["enabled_policy_count"] == 6, retention

    print("retention_smoke_test passed")


if __name__ == "__main__":
    main()
