from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "backup_restore_smoke_test.sqlite3")
os.environ["AGENT_DB_BACKEND"] = "sqlite"
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
sys.path.insert(0, str(ROOT))

from app.db import get_connection, reset_database  # noqa: E402
from app.services.auth import ensure_demo_users  # noqa: E402
from scripts.db_backup import create_backup  # noqa: E402
from scripts.db_restore_sqlite import restore_sqlite_backup  # noqa: E402


def insert_audit(row_id: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO audit_logs (id, actor, event_type, target_type, target_id, detail_json, created_at)
            VALUES (?, 'backup-smoke', ?, 'database', ?, '{}', '2026-06-22T00:00:00+00:00')
            """,
            (row_id, f"backup_smoke.{row_id}", row_id),
        )
        conn.commit()
    finally:
        conn.close()


def exists_audit(row_id: str) -> bool:
    conn = get_connection()
    try:
        row = conn.execute("SELECT COUNT(*) AS count FROM audit_logs WHERE id = ?", (row_id,)).fetchone()
        return int(row["count"]) == 1
    finally:
        conn.close()


def main() -> None:
    reset_database(seed=True)
    ensure_demo_users()
    insert_audit("before_backup")

    output_dir = ROOT / "data" / "backup_restore_smoke"
    manifest = create_backup(output_dir=output_dir, name="smoke")
    backup_path = Path(manifest["backup_path"])
    assert backup_path.exists(), manifest
    assert manifest["size_bytes"] > 0, manifest
    assert manifest["sha256"], manifest

    insert_audit("after_backup")
    assert exists_audit("before_backup")
    assert exists_audit("after_backup")

    restore_sqlite_backup(backup_path, confirmed=True)
    assert exists_audit("before_backup")
    assert not exists_audit("after_backup")

    print("db_backup_restore_smoke_test passed")


if __name__ == "__main__":
    main()
