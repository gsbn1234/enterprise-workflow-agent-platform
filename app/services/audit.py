from __future__ import annotations

from typing import Any

from app.db import get_connection, rows_to_dicts
from app.utils import json_dumps, new_id, utc_now


def record_audit(
    event_type: str,
    target_type: str,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
    *,
    actor: str = "system",
) -> dict:
    audit_id = new_id("audit")
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO audit_logs
            (id, actor, event_type, target_type, target_id, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (audit_id, actor, event_type, target_type, target_id, json_dumps(detail or {}), utc_now()),
        )
    return {
        "id": audit_id,
        "actor": actor,
        "event_type": event_type,
        "target_type": target_type,
        "target_id": target_id,
        "detail": detail or {},
    }


def list_audit_logs(limit: int = 100) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM audit_logs
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 500)),),
        ).fetchall()
    return rows_to_dicts(rows)
