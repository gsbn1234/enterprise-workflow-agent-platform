from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_AUTH_REQUIRED"] = "true"
os.environ["AGENT_METRICS_ENABLED"] = "true"
os.environ["AGENT_METRICS_AUTH_REQUIRED"] = "true"
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "metrics_smoke_test.sqlite3")
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import reset_database  # noqa: E402
from app.main import app  # noqa: E402
from app.services.auth import ensure_demo_users  # noqa: E402


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def main() -> None:
    reset_database(seed=True)
    ensure_demo_users()

    with TestClient(app) as client:
        unauthenticated = client.get("/metrics")
        assert unauthenticated.status_code == 401, unauthenticated.text

        login = client.post("/api/auth/login", json={"user_id": "admin", "password": "AdminPass123"})
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]

        metrics = client.get("/metrics", headers=auth_header(token))
        assert metrics.status_code == 200, metrics.text
        body = metrics.text
        assert "agent_platform_up 1" in body, body
        assert "agent_platform_info" in body, body
        assert "agent_workflow_runs_all_total" in body, body
        assert "agent_external_outbox_total" in body, body
        assert "agent_login_attempts_total" in body, body

    print("metrics_smoke_test passed")


if __name__ == "__main__":
    main()
