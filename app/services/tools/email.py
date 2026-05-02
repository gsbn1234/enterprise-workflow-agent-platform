from __future__ import annotations

from app.db import get_connection, row_to_dict, rows_to_dicts
from app.services.audit import record_audit
from app.utils import new_id, utc_now


def draft_email(to_address: str, subject: str, body: str) -> dict:
    return {
        "to_address": to_address,
        "subject": subject,
        "body": body,
        "status": "draft",
    }


def send_email(to_address: str, subject: str, body: str, approval_id: str | None = None) -> dict:
    email_id = new_id("email")
    now = utc_now()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO emails
            (id, to_address, subject, body, status, approval_id, created_at, sent_at)
            VALUES (?, ?, ?, ?, 'sent', ?, ?, ?)
            """,
            (email_id, to_address, subject, body, approval_id, now, now),
        )
    record_audit("email.send", "email", email_id, {"to_address": to_address, "approval_id": approval_id})
    return get_email(email_id)


def get_email(email_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    return row_to_dict(row)


def list_emails(limit: int = 100) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM emails
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 500)),),
        ).fetchall()
    return rows_to_dicts(rows)
