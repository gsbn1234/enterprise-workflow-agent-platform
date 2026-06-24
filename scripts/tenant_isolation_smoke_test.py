from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_AUTH_REQUIRED"] = "true"
os.environ["AGENT_TENANT_ISOLATION_ENABLED"] = "true"
os.environ["AGENT_DEFAULT_TENANT_ID"] = "default"
os.environ["AGENT_AUTO_SEED"] = "false"
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "tenant_isolation_smoke_test.sqlite3")
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import reset_database  # noqa: E402
from app.main import app  # noqa: E402
from app.services.auth import create_user  # noqa: E402
from app.services.tools.email import send_email  # noqa: E402
from app.services.tools.ticketing import create_ticket  # noqa: E402


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def login(client: TestClient, user_id: str, password: str) -> dict[str, str]:
    response = client.post("/api/auth/login", json={"user_id": user_id, "password": password})
    assert response.status_code == 200, response.text
    payload = response.json()
    return auth_header(payload["access_token"])


def assert_only_tenant(rows: list[dict], tenant_id: str) -> None:
    assert rows, f"Expected rows for {tenant_id}"
    leaked = [row for row in rows if row.get("tenant_id") != tenant_id]
    assert not leaked, leaked


def ids(rows: list[dict]) -> set[str]:
    return {row["id"] for row in rows}


def main() -> None:
    reset_database(seed=False)
    create_user("admin_a", "Tenant A Admin", "Platform", "admin", "AdminPass123", tenant_id="tenant-a")
    create_user("admin_b", "Tenant B Admin", "Platform", "admin", "AdminPass123", tenant_id="tenant-b")

    ticket_a = create_ticket(
        "Tenant A customer issue",
        "Only tenant-a should see this ticket.",
        priority="high",
        owner_department="Customer Success",
        tenant_id="tenant-a",
    )
    ticket_b = create_ticket(
        "Tenant B customer issue",
        "Only tenant-b should see this ticket.",
        priority="normal",
        owner_department="Operations",
        tenant_id="tenant-b",
    )
    email_a = send_email("tenant-a@example.com", "Tenant A notice", "Only tenant-a should see this email.", tenant_id="tenant-a")
    email_b = send_email("tenant-b@example.com", "Tenant B notice", "Only tenant-b should see this email.", tenant_id="tenant-b")

    with TestClient(app) as client:
        headers_a = login(client, "admin_a", "AdminPass123")
        headers_b = login(client, "admin_b", "AdminPass123")

        me_a = client.get("/api/auth/me", headers=headers_a)
        me_b = client.get("/api/auth/me", headers=headers_b)
        assert me_a.json()["tenant_id"] == "tenant-a", me_a.text
        assert me_b.json()["tenant_id"] == "tenant-b", me_b.text

        request_a = client.post(
            "/api/requests",
            headers=headers_a,
            json={"title": "Tenant A request", "description": "Query tenant-a tickets."},
        )
        request_b = client.post(
            "/api/requests",
            headers=headers_b,
            json={"title": "Tenant B request", "description": "Query tenant-b tickets."},
        )
        assert request_a.status_code == 200, request_a.text
        assert request_b.status_code == 200, request_b.text

        requests_a = client.get("/api/requests", headers=headers_a).json()
        requests_b = client.get("/api/requests", headers=headers_b).json()
        assert_only_tenant(requests_a, "tenant-a")
        assert_only_tenant(requests_b, "tenant-b")
        assert request_b.json()["id"] not in ids(requests_a)
        assert request_a.json()["id"] not in ids(requests_b)

        cross_run = client.post(f"/api/requests/{request_b.json()['id']}/run", headers=headers_a)
        assert cross_run.status_code == 404, cross_run.text

        run_a = client.post("/api/workflow/run", headers=headers_a, json={"objective": "查询工单 Tenant A"})
        run_b = client.post("/api/workflow/run", headers=headers_b, json={"objective": "查询工单 Tenant B"})
        assert run_a.status_code == 200, run_a.text
        assert run_b.status_code == 200, run_b.text

        runs_a = client.get("/api/runs", headers=headers_a).json()
        runs_b = client.get("/api/runs", headers=headers_b).json()
        assert_only_tenant(runs_a, "tenant-a")
        assert_only_tenant(runs_b, "tenant-b")
        assert run_b.json()["id"] not in ids(runs_a)
        assert run_a.json()["id"] not in ids(runs_b)

        cross_detail = client.get(f"/api/runs/{run_b.json()['id']}", headers=headers_a)
        assert cross_detail.status_code == 404, cross_detail.text

        tickets_a = client.get("/api/tickets", headers=headers_a).json()
        tickets_b = client.get("/api/tickets", headers=headers_b).json()
        assert_only_tenant(tickets_a, "tenant-a")
        assert_only_tenant(tickets_b, "tenant-b")
        assert ticket_a["id"] in ids(tickets_a)
        assert ticket_b["id"] not in ids(tickets_a)
        assert ticket_b["id"] in ids(tickets_b)
        assert ticket_a["id"] not in ids(tickets_b)

        emails_a = client.get("/api/emails", headers=headers_a).json()
        emails_b = client.get("/api/emails", headers=headers_b).json()
        assert_only_tenant(emails_a, "tenant-a")
        assert_only_tenant(emails_b, "tenant-b")
        assert email_a["id"] in ids(emails_a)
        assert email_b["id"] not in ids(emails_a)
        assert email_b["id"] in ids(emails_b)
        assert email_a["id"] not in ids(emails_b)

        outbox_a = client.get("/api/admin/external-outbox", headers=headers_a).json()
        outbox_b = client.get("/api/admin/external-outbox", headers=headers_b).json()
        assert_only_tenant(outbox_a, "tenant-a")
        assert_only_tenant(outbox_b, "tenant-b")

        audit_a = client.get("/api/audit-logs", headers=headers_a).json()
        audit_b = client.get("/api/audit-logs", headers=headers_b).json()
        assert_only_tenant(audit_a, "tenant-a")
        assert_only_tenant(audit_b, "tenant-b")

    print("tenant_isolation_smoke_test passed")


if __name__ == "__main__":
    main()
