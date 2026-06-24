from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import database_connection_purpose, database_status, init_db, list_schema_migrations  # noqa: E402
from app.services.auth import ensure_demo_users  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize or migrate the Agent platform database.")
    parser.add_argument("--seed", action="store_true", help="Seed built-in knowledge/customer demo data.")
    parser.add_argument("--demo-users", action="store_true", help="Create demo admin/manager/employee users.")
    args = parser.parse_args()

    with database_connection_purpose("migration"):
        init_db(seed=args.seed)
        if args.demo_users:
            ensure_demo_users()

    status = database_status(purpose="migration")
    if status["status"] != "ok":
        raise SystemExit(f"database_status=error\nreason={status.get('error')}")

    print("database_status=ok")
    print(f"backend={status['backend']}")
    if status.get("postgres_schema"):
        print(f"postgres_schema={status['postgres_schema']}")
    if status.get("sqlite_path"):
        print(f"sqlite_path={status['sqlite_path']}")
    print(f"migration_count={status['migration_count']}")
    for migration in list_schema_migrations(purpose="migration"):
        print(f"migration={migration['id']} applied_at={migration['applied_at']}")


if __name__ == "__main__":
    main()
