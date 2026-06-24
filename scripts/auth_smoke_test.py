from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_AUTH_REQUIRED"] = "true"
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "auth_smoke_test.sqlite3")
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import reset_database  # noqa: E402
from app.main import app  # noqa: E402
from app.services.auth import ensure_demo_users  # noqa: E402


def login(client: TestClient, user_id: str, password: str) -> str:
    response = client.post("/api/auth/login", json={"user_id": user_id, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def main() -> None:
    reset_database(seed=True)
    ensure_demo_users()

    with TestClient(app) as client:
        readiness = client.get("/api/readiness")
        assert readiness.status_code == 200, readiness.text
        assert readiness.json()["database"]["status"] == "ok", readiness.text

        blocked = client.post("/api/workflow/run", json={"objective": "未登录请求"})
        assert blocked.status_code == 401, blocked.text

        alice = login(client, "alice", "AlicePass123")
        manager = login(client, "cs_manager", "ManagerPass123")
        admin = login(client, "admin", "AdminPass123")

        run_response = client.post(
            "/api/workflow/run",
            headers=auth_header(alice),
            json={
                "objective": "客户 Orbit Retail 投诉服务中断，要求退费 800 元，请创建工单并准备回复 support@orbit.example"
            },
        )
        assert run_response.status_code == 200, run_response.text
        run = run_response.json()
        assert run["status"] == "waiting_approval", run

        employee_approvals = client.get("/api/approvals", headers=auth_header(alice))
        assert employee_approvals.status_code == 403, employee_approvals.text

        approvals = client.get("/api/approvals?status=pending", headers=auth_header(manager))
        assert approvals.status_code == 200, approvals.text
        approval_id = approvals.json()[0]["id"]

        decided = client.post(
            f"/api/approvals/{approval_id}/decide",
            headers=auth_header(manager),
            json={"approved": True, "reason": "auth smoke approve"},
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["status"] == "completed", decided.text

        employee_users = client.get("/api/users", headers=auth_header(alice))
        assert employee_users.status_code == 403, employee_users.text

        admin_users = client.get("/api/users", headers=auth_header(admin))
        assert admin_users.status_code == 200, admin_users.text
        assert len(admin_users.json()) >= 3, admin_users.text

        employee_preflight = client.get("/api/admin/preflight", headers=auth_header(alice))
        assert employee_preflight.status_code == 403, employee_preflight.text

        admin_preflight = client.get("/api/admin/preflight", headers=auth_header(admin))
        assert admin_preflight.status_code == 200, admin_preflight.text
        assert admin_preflight.json()["database"]["status"] == "ok", admin_preflight.text

        admin_dashboard = client.get("/api/admin/operations-dashboard", headers=auth_header(admin))
        assert admin_dashboard.status_code == 200, admin_dashboard.text
        assert "workflow_runs" in admin_dashboard.json()["statuses"], admin_dashboard.text
        assert "external_outbox" in admin_dashboard.json()["statuses"], admin_dashboard.text

        employee_outbox = client.get("/api/admin/external-outbox", headers=auth_header(alice))
        assert employee_outbox.status_code == 403, employee_outbox.text

        admin_outbox = client.get("/api/admin/external-outbox", headers=auth_header(admin))
        assert admin_outbox.status_code == 200, admin_outbox.text
        assert any(item["action_type"] in {"ticket.create", "email.send"} for item in admin_outbox.json()), admin_outbox.text

        logout_response = client.post("/api/auth/logout", headers=auth_header(alice))
        assert logout_response.status_code == 200, logout_response.text
        revoked_me = client.get("/api/auth/me", headers=auth_header(alice))
        assert revoked_me.status_code == 401, revoked_me.text

    print("auth_smoke_test passed")


if __name__ == "__main__":
    main()
