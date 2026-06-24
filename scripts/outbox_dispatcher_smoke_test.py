from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_AUTH_REQUIRED"] = "true"
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "outbox_dispatcher_smoke_test.sqlite3")
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
os.environ["AGENT_OUTBOX_MAX_ATTEMPTS"] = "3"
os.environ["AGENT_OUTBOX_RETRY_BACKOFF_SECONDS"] = "1"
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import get_connection, reset_database  # noqa: E402
from app.main import app  # noqa: E402
from app.services.auth import ensure_demo_users  # noqa: E402
from app.services.outbox import create_outbox_event, get_outbox_event, mark_outbox_failed  # noqa: E402
from app.services.tools.email import _email_idempotency_key  # noqa: E402


OLD = "2000-01-01T00:00:00+00:00"
FUTURE = "2999-01-01T00:00:00+00:00"


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def count_rows(table: str) -> int:
    with get_connection() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
    return int(row["count"])


def set_attempt_count(outbox_id: str, count: int) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE external_outbox SET attempt_count = ? WHERE id = ?", (count, outbox_id))


def seed_events() -> dict[str, str]:
    email_payload = {
        "to_address": "hr@example.com",
        "subject": "Dispatcher due email",
        "body": "This due email should be dispatched.",
        "approval_id": "approval_dispatcher_due",
    }
    due_email = create_outbox_event(
        "email.send",
        "mock",
        email_payload,
        idempotency_key=_email_idempotency_key(provider="mock", tenant_id="default", **email_payload),
        target_type="email",
    )
    mark_outbox_failed(due_email["id"], "simulated due failure", next_attempt_at=OLD)

    future_email_payload = {
        "to_address": "hr@example.com",
        "subject": "Dispatcher future email",
        "body": "This future email should not be dispatched yet.",
        "approval_id": "approval_dispatcher_future",
    }
    future_email = create_outbox_event(
        "email.send",
        "mock",
        future_email_payload,
        idempotency_key=_email_idempotency_key(provider="mock", tenant_id="default", **future_email_payload),
        target_type="email",
    )
    mark_outbox_failed(future_email["id"], "simulated future failure", next_attempt_at=FUTURE)

    exhausted = create_outbox_event("email.unsupported", "mock", {"note": "exhausted"}, target_type="email")
    mark_outbox_failed(exhausted["id"], "too many attempts", next_attempt_at=OLD)
    set_attempt_count(exhausted["id"], 3)

    unsupported = create_outbox_event("email.unsupported", "mock", {"note": "schedule me"}, target_type="email")
    mark_outbox_failed(unsupported["id"], "unsupported action", next_attempt_at=OLD)

    return {
        "due_email": due_email["id"],
        "future_email": future_email["id"],
        "exhausted": exhausted["id"],
        "unsupported": unsupported["id"],
    }


def main() -> None:
    reset_database(seed=True)
    ensure_demo_users()
    ids = seed_events()

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"user_id": "admin", "password": "AdminPass123"})
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]

        dispatch = client.post("/api/admin/external-outbox/dispatch-due?limit=10", headers=auth_header(token))
        assert dispatch.status_code == 200, dispatch.text
        payload = dispatch.json()
        assert payload["attempted"] == 2, payload
        assert payload["completed"] == 1, payload
        assert payload["failed"] == 1, payload

    assert count_rows("emails") == 1
    due_email = get_outbox_event(ids["due_email"])
    future_email = get_outbox_event(ids["future_email"])
    exhausted = get_outbox_event(ids["exhausted"])
    unsupported = get_outbox_event(ids["unsupported"])
    assert due_email and due_email["status"] == "completed", due_email
    assert future_email and future_email["status"] == "failed" and future_email["attempt_count"] == 0, future_email
    assert exhausted and exhausted["status"] == "failed" and exhausted["attempt_count"] == 3, exhausted
    assert unsupported and unsupported["status"] == "failed", unsupported
    assert unsupported["attempt_count"] == 1, unsupported
    assert unsupported["next_attempt_at"], unsupported

    print("outbox_dispatcher_smoke_test passed")


if __name__ == "__main__":
    main()
