from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_AUTH_REQUIRED"] = "true"
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "audit_integrity_smoke_test.sqlite3")
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import get_connection, reset_database  # noqa: E402
from app.main import app  # noqa: E402
from app.services.audit import record_audit, verify_audit_log_integrity  # noqa: E402
from app.services.auth import ensure_demo_users  # noqa: E402


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def tamper_audit_row(audit_id: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE audit_logs
            SET detail_json = '{"tampered":true}'
            WHERE id = ?
            """,
            (audit_id,),
        )


def main() -> None:
    reset_database(seed=True)
    ensure_demo_users()

    first = record_audit("audit.integrity.first", "smoke", "first", {"step": 1}, actor="smoke")
    second = record_audit("audit.integrity.second", "smoke", "second", {"step": 2}, actor="smoke")
    assert first["row_hash"], first
    assert second["previous_hash"] == first["row_hash"], (first, second)

    initial = verify_audit_log_integrity()
    assert initial["valid"] is True, initial
    assert initial["checked_count"] >= 2, initial
    assert initial["tampered_count"] == 0, initial
    assert initial["broken_link_count"] == 0, initial

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"user_id": "admin", "password": "AdminPass123"})
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]

        api_initial = client.get("/api/admin/audit/integrity", headers=auth_header(token))
        assert api_initial.status_code == 200, api_initial.text
        assert api_initial.json()["valid"] is True, api_initial.json()

        tamper_audit_row(first["id"])
        api_tampered = client.get("/api/admin/audit/integrity", headers=auth_header(token))
        assert api_tampered.status_code == 200, api_tampered.text
        tampered_payload = api_tampered.json()
        assert tampered_payload["valid"] is False, tampered_payload
        assert tampered_payload["tampered_count"] >= 1, tampered_payload
        assert any(item["id"] == first["id"] for item in tampered_payload["tampered"]), tampered_payload

    print("audit_integrity_smoke_test passed")


if __name__ == "__main__":
    main()
