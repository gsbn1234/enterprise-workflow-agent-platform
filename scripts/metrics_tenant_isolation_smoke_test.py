from __future__ import annotations

import os
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_AUTH_REQUIRED"] = "true"
os.environ["AGENT_TENANT_ISOLATION_ENABLED"] = "true"
os.environ["AGENT_DEFAULT_TENANT_ID"] = "default"
os.environ["AGENT_AUTO_SEED"] = "false"
os.environ["AGENT_METRICS_ENABLED"] = "true"
os.environ["AGENT_METRICS_AUTH_REQUIRED"] = "true"
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "metrics_tenant_isolation_smoke_test.sqlite3")
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import reset_database  # noqa: E402
from app.main import app  # noqa: E402
from app.services.auth import create_user  # noqa: E402
from app.services.llm import STATUS_SUCCESS, LlmOutcome  # noqa: E402
from app.services.llm_telemetry import record_llm_call  # noqa: E402
from app.services.tools.ticketing import create_ticket  # noqa: E402


TENANTS = ("tenant-a", "tenant-b")


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def login(client: TestClient, user_id: str) -> dict[str, str]:
    response = client.post("/api/auth/login", json={"user_id": user_id, "password": "AdminPass123"})
    assert response.status_code == 200, response.text
    return auth_header(response.json()["access_token"])


def sample(body: str, name: str, labels: dict[str, str] | None = None) -> float:
    """The value of one series, located by metric name and required labels.

    Handles both exposition shapes: a scalar family is ``name value`` and a
    grouped one is ``name{labels} value``. Deliberately strict about the label
    set -- a series whose labels changed shape is a different series, and the
    point of this suite is that the two endpoints count the same rows, not that
    some line with the right prefix exists.
    """
    wanted = dict(labels or {})
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        if line.startswith(f"{name}{{"):
            selector, _, value = line.partition(" ")
            found = dict(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', selector))
            if all(found.get(key) == val for key, val in wanted.items()):
                return float(value)
        elif line.startswith(f"{name} ") and not wanted:
            return float(line.rpartition(" ")[2])
    return 0.0


def total_of(body: str, name: str) -> float:
    """Sum every series of one metric family. Absent family totals to zero."""
    return sum(
        float(line.rpartition(" ")[2])
        for line in body.splitlines()
        if not line.startswith("#") and line.startswith(f"{name}{{")
    )


def main() -> None:
    reset_database(seed=False)
    for tenant in TENANTS:
        suffix = tenant[-1]
        create_user(f"admin_{suffix}", f"Tenant {suffix.upper()} Admin", "Platform", "admin", "AdminPass123", tenant_id=tenant)
        # Two tickets each, one per status, so every tenant has a non-trivial
        # distribution to group.
        create_ticket(f"{tenant} open issue", "Visible only inside its tenant.", priority="high", tenant_id=tenant)
        create_ticket(f"{tenant} normal issue", "Visible only inside its tenant.", priority="normal", tenant_id=tenant)

    # One recorded LLM call per tenant. ``usage_available`` differs between them
    # so the available/unavailable split is exercised, not just the total.
    record_llm_call(
        LlmOutcome(
            status=STATUS_SUCCESS,
            operation="plan_workflow",
            provider="stub",
            model="stub-hostile",
            latency_ms=12,
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            usage_available=True,
        ),
        tenant_id="tenant-a",
    )
    record_llm_call(
        LlmOutcome(
            status=STATUS_SUCCESS,
            operation="plan_workflow",
            provider="stub",
            model="stub-hostile",
            latency_ms=30,
            usage_available=False,
        ),
        tenant_id="tenant-b",
    )

    with TestClient(app) as client:
        headers = {tenant: login(client, f"admin_{tenant[-1]}") for tenant in TENANTS}

        scrapes = {}
        summaries = {}
        for tenant in TENANTS:
            response = client.get("/metrics", headers=headers[tenant])
            assert response.status_code == 200, response.text
            scrapes[tenant] = response.text

            response = client.get("/api/metrics/summary", headers=headers[tenant])
            assert response.status_code == 200, response.text
            summaries[tenant] = response.json()

        # --- tenant isolation: a scrape is partitioned, not global ---
        for tenant in TENANTS:
            body = scrapes[tenant]
            other = TENANTS[1] if tenant == TENANTS[0] else TENANTS[0]

            # ``agent_tickets_total`` is a scalar out of ``metrics_summary``;
            # ``agent_tickets_by_status_total`` is grouped. Both are scoped, and
            # before the scope was threaded through only the first one was.
            scoped_scalar = sample(body, "agent_tickets_total")
            scoped_grouped = total_of(body, "agent_tickets_by_status_total")
            assert scoped_scalar == 2, (tenant, scoped_scalar, body)
            assert scoped_grouped == scoped_scalar, (tenant, scoped_grouped, scoped_scalar, body)

            # The other tenant's tickets must not appear anywhere in this scrape.
            # Both tenants create exactly two, so a leak doubles the count.
            assert scoped_scalar != 4, (tenant, body)

            # And the same number the API reports, which is what "one scope,
            # one definition" means at the endpoint boundary.
            assert scoped_scalar == summaries[tenant]["totals"]["tickets"], (tenant, summaries[tenant])

            # Same check on the LLM families, which is where the two endpoints
            # used to carry separate SQL.
            llm_grouped = total_of(body, "agent_llm_calls_total")
            llm_from_api = summaries[tenant]["llm_usage"]["totals"]["calls"]
            assert llm_grouped == 1, (tenant, llm_grouped, body)
            assert llm_grouped == llm_from_api, (tenant, llm_grouped, llm_from_api)

            # The exported counters must add up to the exported total on their
            # own, so a reader can reconcile the scrape without the API.
            available = sample(body, "agent_llm_usage_available_calls_total")
            unavailable = sample(body, "agent_llm_usage_unavailable_calls_total")
            assert available + unavailable == llm_grouped, (tenant, available, unavailable, llm_grouped)

            # tenant-a's provider reported usage, tenant-b's did not. That is
            # the difference the two counters exist to make visible, and it is
            # only visible if the scope is real.
            expected_available = 1 if tenant == "tenant-a" else 0
            assert available == expected_available, (tenant, available, body)

            # The other tenant's tokens must not be summed into this scrape.
            tokens = sample(body, "agent_llm_tokens_total", {"kind": "total"})
            assert tokens == (120 if tenant == "tenant-a" else 0), (tenant, tokens, body)

            assert other not in body, (tenant, other)

        # --- the two scrapes genuinely differ, so the assertions above are not
        # --- passing because everything is empty or identical ---
        assert scrapes["tenant-a"] != scrapes["tenant-b"]

    print("metrics_tenant_isolation_smoke_test passed")


if __name__ == "__main__":
    main()
