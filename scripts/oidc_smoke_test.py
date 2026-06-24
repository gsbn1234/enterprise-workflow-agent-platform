from __future__ import annotations

import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_AUTH_REQUIRED"] = "true"
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "oidc_smoke_test.sqlite3")
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
os.environ["AGENT_OIDC_ENABLED"] = "true"
os.environ["AGENT_OIDC_ISSUER"] = "https://idp.example.test"
os.environ["AGENT_OIDC_AUDIENCE"] = "agent-platform"
os.environ["AGENT_OIDC_ALGORITHMS"] = "HS256"
os.environ["AGENT_OIDC_HS256_SECRET"] = "oidc-smoke-shared-secret-at-least-32-bytes"
os.environ["AGENT_OIDC_ADMIN_GROUPS"] = "agent-admins"
os.environ["AGENT_OIDC_MANAGER_GROUPS"] = "agent-approvers"
os.environ["AGENT_OIDC_DEFAULT_DEPARTMENT"] = "Operations"
sys.path.insert(0, str(ROOT))

import jwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import reset_database  # noqa: E402
from app.main import app  # noqa: E402
from app.services.auth import ensure_demo_users  # noqa: E402


ISSUER = "https://idp.example.test"
AUDIENCE = "agent-platform"
SECRET = "oidc-smoke-shared-secret-at-least-32-bytes"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_token(**overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "sso-admin-001",
        "iat": now,
        "exp": now + 600,
        "name": "SSO Admin",
        "email": "sso.admin@example.test",
        "department": "Platform",
        "groups": ["agent-admins"],
    }
    claims.update(overrides)
    return jwt.encode(claims, SECRET, algorithm="HS256")


def main() -> None:
    reset_database(seed=True)
    ensure_demo_users()

    oidc_token = make_token()
    bad_audience_token = make_token(aud="wrong-audience")

    with TestClient(app) as client:
        me = client.get("/api/auth/me", headers=bearer(oidc_token))
        assert me.status_code == 200, me.text
        user = me.json()
        assert user["id"].startswith("oidc_"), user
        assert user["display_name"] == "SSO Admin", user
        assert user["department"] == "Platform", user
        assert user["role"] == "admin", user

        dashboard = client.get("/api/admin/operations-dashboard", headers=bearer(oidc_token))
        assert dashboard.status_code == 200, dashboard.text
        oidc = dashboard.json()["oidc"]
        assert oidc["enabled"] is True, oidc
        assert oidc["uses_hs256"] is True, oidc
        assert oidc["missing"] == [], oidc

        logout = client.post("/api/auth/logout", headers=bearer(oidc_token))
        assert logout.status_code == 200, logout.text

        invalid = client.get("/api/auth/me", headers=bearer(bad_audience_token))
        assert invalid.status_code == 401, invalid.text

        local_login = client.post("/api/auth/login", json={"user_id": "admin", "password": "AdminPass123"})
        assert local_login.status_code == 200, local_login.text
        local_token = local_login.json()["access_token"]
        local_me = client.get("/api/auth/me", headers=bearer(local_token))
        assert local_me.status_code == 200, local_me.text
        assert local_me.json()["id"] == "admin", local_me.json()

    print("oidc_smoke_test passed")


if __name__ == "__main__":
    main()
