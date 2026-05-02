from __future__ import annotations

from app.db import get_connection, row_to_dict, rows_to_dicts
from app.services.audit import record_audit
from app.utils import new_id, utc_now


def create_business_request(
    title: str,
    description: str,
    requester_user_id: str | None = None,
    requester_department: str | None = None,
    priority: str = "normal",
) -> dict:
    request_id = new_id("req")
    now = utc_now()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO business_requests
            (id, title, description, requester_user_id, requester_department, priority, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?)
            """,
            (request_id, title, description, requester_user_id, requester_department, priority, now, now),
        )
    record_audit(
        "business_request.create",
        "business_request",
        request_id,
        {"title": title, "priority": priority},
        actor=requester_user_id or "system",
    )
    return get_business_request(request_id)


def get_business_request(request_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM business_requests WHERE id = ?", (request_id,)).fetchone()
    return row_to_dict(row)


def list_business_requests(limit: int = 100) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM business_requests
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 500)),),
        ).fetchall()
    return rows_to_dicts(rows)


def update_business_request_status(request_id: str, status: str, category: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE business_requests
            SET status = ?, category = COALESCE(?, category), updated_at = ?
            WHERE id = ?
            """,
            (status, category, utc_now(), request_id),
        )
