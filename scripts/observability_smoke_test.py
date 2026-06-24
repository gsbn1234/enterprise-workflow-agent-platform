from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "observability_smoke_test.sqlite3")
os.environ["AGENT_LOG_FORMAT"] = "json"
os.environ["AGENT_SERVICE_VERSION"] = "observability-smoke"
os.environ["AGENT_OTEL_ENABLED"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import reset_database  # noqa: E402
from app.main import app  # noqa: E402
from app.services.observability import JsonLogFormatter, reset_log_context, set_log_context  # noqa: E402


def main() -> None:
    reset_database(seed=True)

    token = set_log_context(request_id="obs-request", trace_id="1" * 32, span_id="2" * 16, tenant_id="tenant-a")
    try:
        record = logging.LogRecord(
            name="agent_platform.smoke",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="observability.event",
            args=(),
            exc_info=None,
        )
        record.event = "observability.event"
        record.run_id = "run_observability_smoke"
        payload = json.loads(JsonLogFormatter().format(record))
    finally:
        reset_log_context(token)

    assert payload["message"] == "observability.event", payload
    assert payload["service"] == "enterprise-workflow-agent", payload
    assert payload["service_version"] == "observability-smoke", payload
    assert payload["request_id"] == "obs-request", payload
    assert payload["trace_id"] == "1" * 32, payload
    assert payload["span_id"] == "2" * 16, payload
    assert payload["tenant_id"] == "tenant-a", payload
    assert payload["run_id"] == "run_observability_smoke", payload

    with TestClient(app) as client:
        response = client.get("/api/health", headers={"X-Request-ID": "obs-health"})
        assert response.status_code == 200, response.text
        observability = response.json()["observability"]
        assert observability["log_format"] == "json", observability
        assert observability["otel"]["enabled"] is False, observability

    print("observability_smoke_test passed")


if __name__ == "__main__":
    main()
