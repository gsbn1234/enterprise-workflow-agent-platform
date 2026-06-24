from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_AUTH_REQUIRED"] = "true"
os.environ["AGENT_TRACEPARENT_ENABLED"] = "true"
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "trace_context_smoke_test.sqlite3")
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import reset_database  # noqa: E402
from app.main import app  # noqa: E402
from app.services.auth import ensure_demo_users  # noqa: E402
from app.services.tracing import parse_traceparent  # noqa: E402


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def main() -> None:
    reset_database(seed=True)
    ensure_demo_users()

    incoming_trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    incoming_span_id = "00f067aa0ba902b7"
    traceparent = f"00-{incoming_trace_id}-{incoming_span_id}-01"

    with TestClient(app) as client:
        health = client.get("/api/health", headers={"traceparent": traceparent, "X-Request-ID": "trace-smoke"})
        assert health.status_code == 200, health.text
        assert health.headers["X-Request-ID"] == "trace-smoke", health.headers
        assert health.headers["X-Trace-ID"] == incoming_trace_id, health.headers
        assert health.headers["X-Span-ID"] != incoming_span_id, health.headers
        response_traceparent = health.headers["traceparent"]
        parsed = parse_traceparent(response_traceparent)
        assert parsed, response_traceparent
        assert parsed["trace_id"] == incoming_trace_id, parsed
        assert parsed["span_id"] == health.headers["X-Span-ID"], (parsed, health.headers)
        assert health.json()["observability"]["traceparent_enabled"] is True, health.json()

        invalid = client.get("/api/health", headers={"traceparent": "00-0-0-00"})
        assert invalid.status_code == 200, invalid.text
        assert invalid.headers["X-Trace-ID"] != incoming_trace_id, invalid.headers
        assert parse_traceparent(invalid.headers["traceparent"]), invalid.headers

        login = client.post("/api/auth/login", json={"user_id": "admin", "password": "AdminPass123"})
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]
        dashboard = client.get("/api/admin/operations-dashboard", headers=auth_header(token))
        assert dashboard.status_code == 200, dashboard.text
        tracing = dashboard.json()["tracing"]
        assert tracing["traceparent_enabled"] is True, tracing
        assert "traceparent" in tracing["response_headers"], tracing

    print("trace_context_smoke_test passed")


if __name__ == "__main__":
    main()
