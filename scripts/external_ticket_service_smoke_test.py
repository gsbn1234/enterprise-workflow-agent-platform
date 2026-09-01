from __future__ import annotations

import base64
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["TICKET_SERVICE_DB_PATH"] = str(ROOT / "data" / "external_ticket_service_smoke_test.sqlite3")
os.environ["TICKET_SERVICE_TOKEN"] = "external-service-test-token"
os.environ["TICKET_DASHBOARD_USERNAME"] = "ticket-desk"
os.environ["TICKET_DASHBOARD_PASSWORD"] = "TicketDeskPass123"
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from external_ticket_service.server import DB_PATH, app  # noqa: E402


def main() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
    bearer = {"Authorization": "Bearer external-service-test-token"}
    basic_value = base64.b64encode(b"ticket-desk:TicketDeskPass123").decode("ascii")
    basic = {"Authorization": f"Basic {basic_value}"}

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/tickets").status_code == 401
        created = client.post(
            "/api/tickets",
            headers=bearer,
            json={"title": "Tenant A external ticket", "tenant_id": "tenant-a", "priority": "high"},
        )
        assert created.status_code == 200, created.text
        ticket = created.json()
        assert ticket["tenant_id"] == "tenant-a"
        assert ticket["due_at"] and ticket["sla"]["label"] != "not set"
        assert client.get("/api/tickets?tenant_id=tenant-b", headers=bearer).json() == []
        assert len(client.get("/api/tickets?tenant_id=tenant-a", headers=bearer).json()) == 1
        investigating = client.patch(
            f"/api/tickets/{ticket['id']}",
            headers=bearer,
            json={"status": "investigating", "actor": "agent"},
        )
        assert investigating.status_code == 200, investigating.text
        approved = client.patch(
            f"/api/tickets/{ticket['id']}",
            headers=bearer,
            json={"status": "approved", "actor": "manager"},
        )
        assert approved.status_code == 200, approved.text
        invalid = client.patch(f"/api/tickets/{ticket['id']}", headers=bearer, json={"status": "rejected"})
        assert invalid.status_code == 409, invalid.text
        assert client.get("/", headers=basic).status_code == 200
        comment = client.post(
            f"/api/tickets/{ticket['id']}/comments",
            headers=basic,
            json={"body": "Ticket desk comment", "actor": "ticket-desk"},
        )
        assert comment.status_code == 200, comment.text

    print("external_ticket_service_smoke_test passed")


if __name__ == "__main__":
    main()
