from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_AUTH_REQUIRED"] = "true"
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "oidc_browser_smoke_test.sqlite3")
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
os.environ["AGENT_OIDC_ENABLED"] = "true"
os.environ["AGENT_OIDC_BROWSER_LOGIN_ENABLED"] = "true"
os.environ["AGENT_OIDC_ISSUER"] = "https://idp.example.test"
os.environ["AGENT_OIDC_AUDIENCE"] = "agent-platform"
os.environ["AGENT_OIDC_AUTHORIZATION_URL"] = "https://idp.example.test/oauth2/v1/authorize"
os.environ["AGENT_OIDC_TOKEN_URL"] = "https://idp.example.test/oauth2/v1/token"
os.environ["AGENT_OIDC_CLIENT_ID"] = "agent-client"
os.environ["AGENT_OIDC_CLIENT_SECRET"] = "oidc-browser-smoke-client-secret"
os.environ["AGENT_OIDC_ALGORITHMS"] = "HS256"
os.environ["AGENT_OIDC_HS256_SECRET"] = "oidc-browser-smoke-shared-secret-at-least-32-bytes"
os.environ["AGENT_OIDC_ADMIN_GROUPS"] = "agent-admins"
os.environ["AGENT_OIDC_MANAGER_GROUPS"] = "agent-approvers"
os.environ["AGENT_OIDC_DEFAULT_DEPARTMENT"] = "Operations"
sys.path.insert(0, str(ROOT))

import jwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.services.oidc as oidc_module  # noqa: E402
from app.db import reset_database  # noqa: E402
from app.main import app  # noqa: E402


ISSUER = "https://idp.example.test"
AUDIENCE = "agent-platform"
SECRET = "oidc-browser-smoke-shared-secret-at-least-32-bytes"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_id_token() -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "browser-sso-admin",
            "iat": now,
            "exp": now + 600,
            "name": "Browser SSO Admin",
            "email": "browser.sso.admin@example.test",
            "department": "Platform",
            "groups": ["agent-admins"],
            "tenant_id": "tenant-sso",
        },
        SECRET,
        algorithm="HS256",
    )


def extract_access_token(html: str) -> str:
    match = re.search(r'sessionStorage\.setItem\("agent_access_token",\s*(".*?")\);', html)
    assert match, html
    return json.loads(match.group(1))


def main() -> None:
    reset_database(seed=True)
    oidc_module.exchange_authorization_code = lambda code, redirect_uri: {"id_token": make_id_token()}

    with TestClient(app) as client:
        start = client.get("/api/auth/oidc/start?next=/admin", follow_redirects=False)
        assert start.status_code == 302, start.text
        location = start.headers["location"]
        parsed = urlparse(location)
        assert parsed.scheme == "https", location
        assert parsed.netloc == "idp.example.test", location
        params = parse_qs(parsed.query)
        assert params["client_id"] == ["agent-client"], params
        assert params["response_type"] == ["code"], params
        assert "openid" in params["scope"][0], params
        state = params["state"][0]

        callback = client.get(f"/api/auth/oidc/callback?code=smoke-code&state={state}")
        assert callback.status_code == 200, callback.text
        platform_token = extract_access_token(callback.text)
        me = client.get("/api/auth/me", headers=bearer(platform_token))
        assert me.status_code == 200, me.text
        user = me.json()
        assert user["display_name"] == "Browser SSO Admin", user
        assert user["role"] == "admin", user
        assert user["tenant_id"] == "tenant-sso", user

        mismatch = client.get(f"/api/auth/oidc/callback?code=smoke-code&state={state}")
        assert mismatch.status_code == 400, mismatch.text

    print("oidc_browser_smoke_test passed")


if __name__ == "__main__":
    main()
