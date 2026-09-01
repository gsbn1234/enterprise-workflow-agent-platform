from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_AUTH_REQUIRED"] = "true"
os.environ["AGENT_TENANT_ISOLATION_ENABLED"] = "true"
os.environ["AGENT_AUTO_SEED"] = "false"
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "crm_ticket_smoke_test.sqlite3")
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import reset_database  # noqa: E402
from app.main import app  # noqa: E402
from app.services.auth import create_user  # noqa: E402
from app.services.tools.ticketing import create_ticket  # noqa: E402


def login(client: TestClient, user_id: str) -> dict[str, str]:
    response = client.post("/api/auth/login", json={"user_id": user_id, "password": "TestPass123"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def main() -> None:
    reset_database(seed=False)
    create_user("admin_a", "Tenant A Admin", "Platform", "admin", "TestPass123", tenant_id="tenant-a")
    create_user("admin_b", "Tenant B Admin", "Platform", "admin", "TestPass123", tenant_id="tenant-b")
    create_user("cs_manager", "CS Manager", "Customer Success", "manager", "TestPass123", tenant_id="tenant-a")
    create_user("cs_agent", "CS Agent", "Customer Success", "employee", "TestPass123", tenant_id="tenant-a")
    create_user("ops_agent", "Ops Agent", "Operations", "employee", "TestPass123", tenant_id="tenant-a")

    ticket = create_ticket(
        "CRM customer escalation",
        "Validate ticket state and timeline operations.",
        owner_department="Customer Success",
        priority="high",
        tenant_id="tenant-a",
    )
    assert ticket["due_at"] and ticket["sla"]["state"] == "active", ticket

    with TestClient(app) as client:
        admin_a = login(client, "admin_a")
        admin_b = login(client, "admin_b")
        manager = login(client, "cs_manager")
        agent = login(client, "cs_agent")
        ops = login(client, "ops_agent")

        customer_payload = {
            "name": "Shared Email Tenant A",
            "email": "shared@example.com",
            "tier": "enterprise",
            "tags": ["renewal", "priority"],
        }
        created_a = client.post("/api/customers", headers=manager, json=customer_payload)
        assert created_a.status_code == 201, created_a.text
        created_b = client.post(
            "/api/customers",
            headers=admin_b,
            json={**customer_payload, "name": "Shared Email Tenant B"},
        )
        assert created_b.status_code == 201, created_b.text
        assert created_a.json()["id"] != created_b.json()["id"]

        customers_a = client.get("/api/customers?q=shared@example.com", headers=admin_a)
        customers_b = client.get("/api/customers?q=shared@example.com", headers=admin_b)
        assert [item["id"] for item in customers_a.json()] == [created_a.json()["id"]]
        assert [item["id"] for item in customers_b.json()] == [created_b.json()["id"]]
        assert client.get(f"/api/customers/{created_b.json()['id']}", headers=admin_a).status_code == 404
        assert client.get("/api/customers", headers=ops).status_code == 403
        assert client.get("/api/metrics/summary", headers=admin_a).json()["totals"]["customers"] == 1
        assert client.get("/api/metrics/summary", headers=admin_b).json()["totals"]["customers"] == 1

        updated = client.patch(
            f"/api/customers/{created_a.json()['id']}",
            headers=manager,
            json={"status": "at_risk", "health_score": 38},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["status"] == "at_risk"
        interaction = client.post(
            f"/api/customers/{created_a.json()['id']}/interactions",
            headers=agent,
            json={"summary": "Customer requested an escalation call.", "interaction_type": "call", "channel": "phone"},
        )
        assert interaction.status_code == 201, interaction.text
        timeline = client.get(f"/api/customers/{created_a.json()['id']}/interactions", headers=agent)
        assert len(timeline.json()) == 1

        ticket_list = client.get("/api/tickets", headers=agent)
        assert ticket_list.status_code == 200, ticket_list.text
        assert ticket["id"] in {item["id"] for item in ticket_list.json()}
        investigating = client.patch(
            f"/api/tickets/{ticket['id']}/ops",
            headers=agent,
            json={"status": "investigating", "comment": "Agent accepted the escalation."},
        )
        assert investigating.status_code == 200, investigating.text
        approved = client.patch(
            f"/api/tickets/{ticket['id']}/ops",
            headers=manager,
            json={"status": "approved", "comment": "Manager approved remediation."},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["resolved_at"] is None
        invalid = client.patch(f"/api/tickets/{ticket['id']}/ops", headers=manager, json={"status": "rejected"})
        assert invalid.status_code == 400, invalid.text
        comment = client.post(
            f"/api/tickets/{ticket['id']}/comments",
            headers=agent,
            json={"body": "Customer was informed of the approved plan."},
        )
        assert comment.status_code == 201, comment.text
        resolved = client.patch(
            f"/api/tickets/{ticket['id']}/ops",
            headers=agent,
            json={"status": "resolved", "comment": "Customer confirmed resolution."},
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["resolved_at"] and resolved.json()["sla"]["state"] == "met"
        events = client.get(f"/api/tickets/{ticket['id']}/events", headers=agent)
        assert events.status_code == 200, events.text
        assert {event["event_type"] for event in events.json()} >= {"created", "status_changed", "comment"}
        assert client.get(f"/api/tickets/{ticket['id']}", headers=ops).status_code == 403

    print("crm_ticket_smoke_test passed")


if __name__ == "__main__":
    main()
