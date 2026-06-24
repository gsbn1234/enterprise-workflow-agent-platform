from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_AUTH_REQUIRED"] = "true"
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "scim_smoke_test.sqlite3")
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
os.environ["AGENT_SCIM_ENABLED"] = "true"
os.environ["AGENT_SCIM_TOKEN"] = "scim-smoke-token-with-more-than-32-bytes"
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import reset_database  # noqa: E402
from app.main import app  # noqa: E402
from app.services.auth import get_user  # noqa: E402
from app.services.scim import SCIM_AGENT_SCHEMA, SCIM_ENTERPRISE_SCHEMA  # noqa: E402


TOKEN = os.environ["AGENT_SCIM_TOKEN"]


def bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def main() -> None:
    reset_database(seed=True)
    with TestClient(app) as client:
        denied = client.get("/scim/v2/ServiceProviderConfig")
        assert denied.status_code == 401, denied.text

        service_provider = client.get("/scim/v2/ServiceProviderConfig", headers=bearer())
        assert service_provider.status_code == 200, service_provider.text
        assert service_provider.json()["patch"]["supported"] is True

        create = client.post(
            "/scim/v2/Users",
            headers=bearer(),
            json={
                "userName": "jane.scim@example.test",
                "externalId": "directory-jane-scim",
                "displayName": "Jane SCIM",
                "active": True,
                SCIM_ENTERPRISE_SCHEMA: {"department": "Security"},
                SCIM_AGENT_SCHEMA: {"role": "manager", "tenant_id": "tenant-scim"},
            },
        )
        assert create.status_code == 201, create.text
        created = create.json()
        assert created["id"].startswith("scim_"), created
        assert created[SCIM_AGENT_SCHEMA]["role"] == "manager", created
        assert created[SCIM_ENTERPRISE_SCHEMA]["department"] == "Security", created

        patch = client.patch(
            f"/scim/v2/Users/{created['id']}",
            headers=bearer(),
            json={"Operations": [{"op": "replace", "path": "active", "value": False}]},
        )
        assert patch.status_code == 200, patch.text
        assert patch.json()["active"] is False
        assert get_user(created["id"])["disabled"] == 1

        delete = client.delete(f"/scim/v2/Users/{created['id']}", headers=bearer())
        assert delete.status_code == 204, delete.text
        assert get_user(created["id"])["disabled"] == 1

    print("scim_smoke_test passed")


if __name__ == "__main__":
    main()
