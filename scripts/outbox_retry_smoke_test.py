from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_AUTH_REQUIRED"] = "true"
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "outbox_retry_smoke_test.sqlite3")
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import get_connection, reset_database  # noqa: E402
from app.main import app  # noqa: E402
from app.services.auth import ensure_demo_users  # noqa: E402
from app.services.outbox import create_outbox_event, mark_outbox_failed  # noqa: E402
from app.services.tools.email import _email_idempotency_key  # noqa: E402
from app.services.tools.ticketing import _ticket_idempotency_key  # noqa: E402


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def count_rows(table: str) -> int:
    with get_connection() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
    return int(row["count"])


def seed_failed_outbox_events() -> None:
    ticket_payload = {
        "title": "Recovered outbox ticket",
        "description": "This ticket should be created by retrying a failed outbox event.",
        "customer_id": "cust_orbit",
        "priority": "high",
        "owner_department": "Customer Success",
        "workflow_type": "refund",
        "category": "refund",
        "risk_level": "high",
        "approval_chain": ["Customer Success Manager"],
        "auto_actions": ["create_ticket"],
        "blocked_actions": [],
        "evidence": [],
        "agent_run_id": "run_outbox_retry",
        "approval_id": "approval_outbox_retry",
    }
    ticket_key = _ticket_idempotency_key(
        title=ticket_payload["title"],
        description=ticket_payload["description"],
        customer_id=ticket_payload["customer_id"],
        priority=ticket_payload["priority"],
        owner_department=ticket_payload["owner_department"],
        provider="mock",
        tenant_id="default",
        agent_run_id=ticket_payload["agent_run_id"],
        approval_id=ticket_payload["approval_id"],
    )
    ticket_outbox = create_outbox_event(
        "ticket.create",
        "mock",
        ticket_payload,
        idempotency_key=ticket_key,
        target_type="ticket",
    )
    mark_outbox_failed(ticket_outbox["id"], "simulated ticket outage")

    email_payload = {
        "to_address": "hr@example.com",
        "subject": "Recovered outbox email",
        "body": "This email should be created by retrying a failed outbox event.",
        "approval_id": "approval_email_outbox_retry",
    }
    email_key = _email_idempotency_key(
        to_address=email_payload["to_address"],
        subject=email_payload["subject"],
        body=email_payload["body"],
        approval_id=email_payload["approval_id"],
        provider="mock",
        tenant_id="default",
    )
    email_outbox = create_outbox_event(
        "email.send",
        "mock",
        email_payload,
        idempotency_key=email_key,
        target_type="email",
    )
    mark_outbox_failed(email_outbox["id"], "simulated email outage")


def main() -> None:
    reset_database(seed=True)
    ensure_demo_users()
    seed_failed_outbox_events()
    assert count_rows("tickets") == 0
    assert count_rows("emails") == 0

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"user_id": "admin", "password": "AdminPass123"})
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]

        retry = client.post("/api/admin/external-outbox/retry-failed?limit=10", headers=auth_header(token))
        assert retry.status_code == 200, retry.text
        payload = retry.json()
        assert payload["attempted"] == 2, payload
        assert payload["completed"] == 2, payload
        assert payload["failed"] == 0, payload

        failed = client.get("/api/admin/external-outbox?status=failed&limit=10", headers=auth_header(token))
        assert failed.status_code == 200, failed.text
        assert failed.json() == [], failed.json()

    assert count_rows("tickets") == 1
    assert count_rows("emails") == 1
    print("outbox_retry_smoke_test passed")


if __name__ == "__main__":
    main()
