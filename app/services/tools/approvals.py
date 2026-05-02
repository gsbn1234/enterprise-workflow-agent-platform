from __future__ import annotations

from app.db import get_connection, row_to_dict, rows_to_dicts
from app.services.audit import record_audit
from app.utils import json_dumps, json_loads, new_id, utc_now


def create_approval(
    run_id: str,
    action_type: str,
    tool_name: str,
    payload: dict,
    *,
    requested_by: str = "agent",
) -> dict:
    approval_id = new_id("approval")
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO approvals
            (id, run_id, action_type, tool_name, payload_json, status, requested_by, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (approval_id, run_id, action_type, tool_name, json_dumps(payload), requested_by, utc_now()),
        )
    record_audit("approval.request", "approval", approval_id, {"run_id": run_id, "tool_name": tool_name})
    return get_approval(approval_id)


def get_approval(approval_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    approval = row_to_dict(row)
    if approval:
        approval["payload"] = json_loads(approval.pop("payload_json"), {})
    return approval


def list_approvals(status: str | None = None, limit: int = 100) -> list[dict]:
    with get_connection() as conn:
        if status:
            rows = conn.execute(
                """
                SELECT * FROM approvals
                WHERE status = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (status, max(1, min(limit, 500))),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM approvals
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
    approvals = rows_to_dicts(rows)
    for approval in approvals:
        approval["payload"] = json_loads(approval.pop("payload_json"), {})
    return approvals


def mark_approval(approval_id: str, approved: bool, decided_by: str, reason: str | None = None) -> dict | None:
    status = "approved" if approved else "denied"
    with get_connection() as conn:
        current = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if not current:
            return None
        if current["status"] != "pending":
            return get_approval(approval_id)
        conn.execute(
            """
            UPDATE approvals
            SET status = ?, decided_by = ?, decision_reason = ?, decided_at = ?
            WHERE id = ?
            """,
            (status, decided_by, reason, utc_now(), approval_id),
        )
    record_audit("approval.decide", "approval", approval_id, {"approved": approved, "reason": reason}, actor=decided_by)
    return get_approval(approval_id)
