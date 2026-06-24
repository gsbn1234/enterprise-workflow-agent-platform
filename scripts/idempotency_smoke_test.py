from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "idempotency_smoke_test.sqlite3")
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
os.environ["TICKET_SERVICE_DB_PATH"] = str(ROOT / "data" / "external_ticket_idempotency_smoke_test.sqlite3")
os.environ["TICKET_SERVICE_TOKEN"] = "idempotency-smoke-token"
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import reset_database  # noqa: E402
from app.services.outbox import list_outbox_events  # noqa: E402
from app.services.tools.email import send_email  # noqa: E402
from app.services.tools.ticketing import create_ticket  # noqa: E402
from external_ticket_service.server import app as ticket_service_app  # noqa: E402
from external_ticket_service.server import DB_PATH as TICKET_DB_PATH  # noqa: E402
from external_ticket_service.server import init_db as init_ticket_db  # noqa: E402


def main() -> None:
    reset_database(seed=True)

    ticket_a = create_ticket(
        "Idempotency smoke ticket",
        "Create this ticket once even if the workflow retries.",
        customer_id="cust_orbit",
        priority="high",
        owner_department="Customer Success",
        agent_run_id="run_idempotency_smoke",
        approval_id="approval_idempotency_smoke",
    )
    ticket_b = create_ticket(
        "Idempotency smoke ticket",
        "Create this ticket once even if the workflow retries.",
        customer_id="cust_orbit",
        priority="high",
        owner_department="Customer Success",
        agent_run_id="run_idempotency_smoke",
        approval_id="approval_idempotency_smoke",
    )
    assert ticket_a["id"] == ticket_b["id"], (ticket_a, ticket_b)

    email_a = send_email("hr@example.com", "Idempotency smoke", "Send this once.", approval_id="approval_email_smoke")
    email_b = send_email("hr@example.com", "Idempotency smoke", "Send this once.", approval_id="approval_email_smoke")
    assert email_a["id"] == email_b["id"], (email_a, email_b)
    outbox = list_outbox_events(limit=20)
    completed = [item for item in outbox if item["status"] == "completed"]
    assert len(completed) == 2, outbox
    assert {item["action_type"] for item in completed} == {"ticket.create", "email.send"}, outbox
    assert {item["target_id"] for item in completed} == {ticket_a["id"], email_a["id"]}, outbox

    if TICKET_DB_PATH.exists():
        TICKET_DB_PATH.unlink()
    init_ticket_db()
    with TestClient(ticket_service_app) as client:
        headers = {"Authorization": "Bearer idempotency-smoke-token", "Idempotency-Key": "external-ticket-once"}
        payload = {
            "title": "External idempotency smoke",
            "description": "The external ticket service should return the existing ticket.",
            "customer_id": "cust_orbit",
            "priority": "high",
            "owner_department": "Customer Success",
        }
        first = client.post("/api/tickets", headers=headers, json=payload)
        second = client.post("/api/tickets", headers=headers, json=payload)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["id"] == second.json()["id"], (first.json(), second.json())

    print("idempotency_smoke_test passed")
    print(f"ticket_id={ticket_a['id']}")
    print(f"email_id={email_a['id']}")


if __name__ == "__main__":
    main()
