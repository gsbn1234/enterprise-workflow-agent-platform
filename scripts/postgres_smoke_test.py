from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("AGENT_DB_BACKEND", "postgres")
os.environ.setdefault("AGENT_DATABASE_URL", "postgresql://agent:agent_password@127.0.0.1:5433/agent")
os.environ.setdefault("AGENT_POSTGRES_SCHEMA", "agent_postgres_smoke")
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
sys.path.insert(0, str(ROOT))


def main() -> None:
    try:
        import psycopg
    except ModuleNotFoundError:
        print("agent_postgres_smoke=skipped")
        print("reason=psycopg is not installed. Run: pip install -r requirements.txt")
        return

    try:
        with psycopg.connect(os.environ["AGENT_DATABASE_URL"], connect_timeout=3):
            pass
    except Exception as exc:
        print("agent_postgres_smoke=skipped")
        print(f"reason=PostgreSQL is not reachable: {exc}")
        return

    from app.db import get_connection, reset_database
    from app.services.agent import run_workflow
    from app.services.auth import ensure_demo_users, get_user
    from app.services.jobs import claim_next_job, create_workflow_job

    reset_database(seed=True)
    ensure_demo_users()

    alice = get_user("alice")
    if not alice:
        raise SystemExit("Demo user alice was not seeded into PostgreSQL.")

    run = run_workflow(
        "员工 Alice 申请下周三远程办公一天，请根据企业政策判断是否可自动处理并创建记录",
        requester_user_id="alice",
        requester_department="Customer Success",
        requester_role="employee",
    )
    if run["status"] != "completed":
        raise SystemExit(f"Expected completed remote-work run, got: {run['status']}")

    job = create_workflow_job(
        "员工 Alice 申请明天远程办公一天，请创建记录",
        requester_user_id="alice",
        requester_department="Customer Success",
    )
    claimed = claim_next_job("postgres-smoke-worker")
    if not claimed or claimed["id"] != job["id"] or claimed["status"] != "running":
        raise SystemExit(f"PostgreSQL queue claim failed: {claimed}")

    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM workflow_runs").fetchone()
    if int(row["count"]) < 1:
        raise SystemExit("PostgreSQL workflow_runs table did not persist the smoke run.")

    print("agent_postgres_smoke=ok")
    print(f"schema={os.environ['AGENT_POSTGRES_SCHEMA']}")
    print(f"run_id={run['id']}")
    print(f"job_id={job['id']}")


if __name__ == "__main__":
    main()
