from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_AUTH_REQUIRED"] = "true"
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "security_smoke_test.sqlite3")
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
os.environ["AGENT_LOGIN_MAX_FAILURES"] = "2"
os.environ["AGENT_LOGIN_WINDOW_MINUTES"] = "15"
os.environ["AGENT_LOGIN_LOCKOUT_MINUTES"] = "15"
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import reset_database  # noqa: E402
from app.main import app  # noqa: E402
from app.services.auth import ensure_demo_users  # noqa: E402


def login(client: TestClient, user_id: str, password: str):
    return client.post("/api/auth/login", json={"user_id": user_id, "password": password})


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def main() -> None:
    reset_database(seed=True)
    ensure_demo_users()

    with TestClient(app) as client:
        first = login(client, "alice", "wrong-password")
        assert first.status_code == 401, first.text
        second = login(client, "alice", "wrong-password-again")
        assert second.status_code == 423, second.text
        locked = login(client, "alice", "AlicePass123")
        assert locked.status_code == 423, locked.text

        admin = login(client, "admin", "AdminPass123")
        assert admin.status_code == 200, admin.text
        token = admin.json()["access_token"]

        attempts = client.get("/api/admin/security/login-attempts?user_id=alice", headers=auth_header(token))
        assert attempts.status_code == 200, attempts.text
        payload = attempts.json()
        assert len(payload) >= 3, payload
        assert any(item["failure_reason"] == "bad_password" for item in payload), payload
        assert any(item["failure_reason"] == "locked" for item in payload), payload

        dashboard = client.get("/api/admin/operations-dashboard", headers=auth_header(token))
        assert dashboard.status_code == 200, dashboard.text
        security = dashboard.json()["security"]
        assert security["active_lockout_count"] >= 1, security
        assert security["failed_login_count"] >= 3, security

    print("security_smoke_test passed")


if __name__ == "__main__":
    main()
