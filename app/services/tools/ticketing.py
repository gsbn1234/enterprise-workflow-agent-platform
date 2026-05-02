from __future__ import annotations

from app.db import get_connection, row_to_dict, rows_to_dicts
from app.services.audit import record_audit
from app.utils import new_id, utc_now


def create_ticket(
    title: str,
    description: str,
    *,
    customer_id: str | None = None,
    priority: str = "normal",
    owner_department: str = "Customer Success",
) -> dict:
    ticket_id = new_id("ticket")
    now = utc_now()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO tickets
            (id, title, description, customer_id, status, priority, owner_department, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?)
            """,
            (ticket_id, title, description, customer_id, priority, owner_department, now, now),
        )
    record_audit("ticket.create", "ticket", ticket_id, {"title": title, "priority": priority})
    return get_ticket(ticket_id)


def update_ticket(ticket_id: str, status: str | None = None, owner_department: str | None = None) -> dict | None:
    with get_connection() as conn:
        current = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not current:
            return None
        conn.execute(
            """
            UPDATE tickets
            SET status = COALESCE(?, status),
                owner_department = COALESCE(?, owner_department),
                updated_at = ?
            WHERE id = ?
            """,
            (status, owner_department, utc_now(), ticket_id),
        )
    record_audit("ticket.update", "ticket", ticket_id, {"status": status, "owner_department": owner_department})
    return get_ticket(ticket_id)


def get_ticket(ticket_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    return row_to_dict(row)


def list_tickets(limit: int = 100) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM tickets
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 500)),),
        ).fetchall()
    return rows_to_dicts(rows)
