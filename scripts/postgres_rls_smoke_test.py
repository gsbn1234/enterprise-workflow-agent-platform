from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("AGENT_DB_BACKEND", "postgres")
os.environ.setdefault("AGENT_DATABASE_URL", "postgresql://agent:agent_password@127.0.0.1:5433/agent")
os.environ.setdefault("AGENT_POSTGRES_SCHEMA", "agent_rls_smoke")
os.environ["AGENT_POSTGRES_RLS_ENABLED"] = "true"
os.environ["AGENT_POSTGRES_RLS_BYPASS_ROLE"] = "agent_rls_bypass_smoke"
os.environ["AGENT_TENANT_ISOLATION_ENABLED"] = "true"
os.environ["AGENT_DEFAULT_TENANT_ID"] = "default"
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
sys.path.insert(0, str(ROOT))


def main() -> None:
    try:
        import psycopg
    except ModuleNotFoundError:
        print("agent_postgres_rls_smoke=skipped")
        print("reason=psycopg is not installed. Run: pip install -r requirements.txt")
        return

    try:
        with psycopg.connect(os.environ["AGENT_DATABASE_URL"], connect_timeout=3):
            pass
    except Exception as exc:
        print("agent_postgres_rls_smoke=skipped")
        print(f"reason=PostgreSQL is not reachable: {exc}")
        return

    from app.db import get_connection, postgres_rls_status, reset_database
    from app.services.tenancy import rls_system_context, tenant_context
    from app.services.tools.email import send_email
    from app.services.tools.ticketing import create_ticket

    reset_database(seed=False)

    with tenant_context("tenant-a"):
        ticket_a = create_ticket("Tenant A RLS ticket", "Only tenant-a can see this.", tenant_id="tenant-a")
        email_a = send_email("tenant-a@example.com", "Tenant A RLS email", "Only tenant-a can see this.", tenant_id="tenant-a")

    with tenant_context("tenant-b"):
        ticket_b = create_ticket("Tenant B RLS ticket", "Only tenant-b can see this.", tenant_id="tenant-b")
        email_b = send_email("tenant-b@example.com", "Tenant B RLS email", "Only tenant-b can see this.", tenant_id="tenant-b")

    with tenant_context("tenant-a"):
        with get_connection() as conn:
            tickets = conn.execute("SELECT id, tenant_id FROM tickets ORDER BY id").fetchall()
            emails = conn.execute("SELECT id, tenant_id FROM emails ORDER BY id").fetchall()
        assert [row["id"] for row in tickets] == [ticket_a["id"]], tickets
        assert [row["id"] for row in emails] == [email_a["id"]], emails

    with tenant_context("tenant-b"):
        with get_connection() as conn:
            tickets = conn.execute("SELECT id, tenant_id FROM tickets ORDER BY id").fetchall()
            emails = conn.execute("SELECT id, tenant_id FROM emails ORDER BY id").fetchall()
        assert [row["id"] for row in tickets] == [ticket_b["id"]], tickets
        assert [row["id"] for row in emails] == [email_b["id"]], emails

    with tenant_context("tenant-a"):
        with get_connection() as conn:
            conn.execute("SELECT set_config('app.rls_bypass', 'on', false)")
            tickets = conn.execute("SELECT id, tenant_id FROM tickets ORDER BY id").fetchall()
        assert [row["id"] for row in tickets] == [ticket_a["id"]], tickets

    try:
        with tenant_context("tenant-a"):
            with get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO business_requests
                    (id, title, description, requester_user_id, requester_department, tenant_id, priority, status, created_at, updated_at)
                    VALUES ('req_cross_tenant_rls', 'Cross tenant', 'Should be rejected', NULL, NULL, 'tenant-b',
                            'normal', 'new', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
                    """
                )
    except Exception:
        cross_tenant_insert_blocked = True
    else:
        cross_tenant_insert_blocked = False
    assert cross_tenant_insert_blocked, "RLS did not block a cross-tenant INSERT."

    rls = postgres_rls_status()
    with rls_system_context():
        with get_connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM tickets").fetchone()
    expected_count = 2 if rls.get("current_user_has_bypass_role") else 0
    assert int(row["count"]) == expected_count, {"row": row, "rls": rls}

    print("agent_postgres_rls_smoke=ok")
    print(f"schema={os.environ['AGENT_POSTGRES_SCHEMA']}")
    print(f"bypass_role_exists={str(bool(rls.get('bypass_role_exists'))).lower()}")
    print(f"current_user_has_bypass_role={str(bool(rls.get('current_user_has_bypass_role'))).lower()}")
    print(f"ticket_a={ticket_a['id']}")
    print(f"ticket_b={ticket_b['id']}")


if __name__ == "__main__":
    main()
