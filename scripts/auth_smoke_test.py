from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_AUTH_REQUIRED"] = "true"
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "auth_smoke_test.sqlite3")
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
        blocked = client.post("/api/workflow/run", json={"objective": "未登录请求"})
        assert blocked.status_code == 401, blocked.text

        alice = login(client, "alice", "AlicePass123")
        manager = login(client, "manager", "ManagerPass123")
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

    print("auth_smoke_test passed")


if __name__ == "__main__":
    main()
