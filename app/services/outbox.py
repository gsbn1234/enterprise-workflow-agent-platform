from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.db import get_connection, row_to_dict, rows_to_dicts
from app.services.tenancy import effective_tenant_id, rls_system_context, tenant_context
from app.utils import json_dumps, json_loads, new_id, utc_now


def create_outbox_event(
    action_type: str,
    provider: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    tenant_id: str | None = None,
) -> dict:
    tenant = effective_tenant_id(tenant_id or payload.get("tenant_id"))
    if idempotency_key:
        existing = get_outbox_by_idempotency_key(idempotency_key)
        if existing:
            return existing

    outbox_id = new_id("outbox")
    now = utc_now()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO external_outbox
            (id, action_type, provider, target_type, target_id, tenant_id, idempotency_key, status,
             attempt_count, payload_json, response_json, error_message, created_at, updated_at,
             next_attempt_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, '{}', NULL, ?, ?, NULL, NULL)
            """,
            (
                outbox_id,
                action_type,
                provider,
                target_type,
                target_id,
                tenant,
                idempotency_key,
                json_dumps(payload),
                now,
                now,
            ),
        )
    return get_outbox_event(outbox_id) or {}


def mark_outbox_running(outbox_id: str) -> dict | None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE external_outbox
            SET status = 'running',
                attempt_count = attempt_count + 1,
                error_message = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (utc_now(), outbox_id),
        )
    return get_outbox_event(outbox_id)


def mark_outbox_completed(
    outbox_id: str,
    *,
    response: dict[str, Any] | None = None,
    target_id: str | None = None,
) -> dict | None:
    now = utc_now()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE external_outbox
            SET status = 'completed',
                target_id = COALESCE(?, target_id),
                response_json = ?,
                error_message = NULL,
                updated_at = ?,
                completed_at = ?
            WHERE id = ?
            """,
            (target_id, json_dumps(response or {}), now, now, outbox_id),
        )
    return get_outbox_event(outbox_id)


def mark_outbox_failed(
    outbox_id: str,
    error_message: str,
    *,
    response: dict[str, Any] | None = None,
    target_id: str | None = None,
    next_attempt_at: str | None = None,
) -> dict | None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE external_outbox
            SET status = 'failed',
                target_id = COALESCE(?, target_id),
                response_json = ?,
                error_message = ?,
                updated_at = ?,
                next_attempt_at = ?
            WHERE id = ?
            """,
            (target_id, json_dumps(response or {}), error_message, utc_now(), next_attempt_at, outbox_id),
        )
    return get_outbox_event(outbox_id)


def get_outbox_event(outbox_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM external_outbox WHERE id = ?", (outbox_id,)).fetchone()
    return hydrate_outbox_event(row_to_dict(row))


def get_outbox_by_idempotency_key(idempotency_key: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM external_outbox WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
    return hydrate_outbox_event(row_to_dict(row))


def list_outbox_events(status: str | None = None, limit: int = 100, tenant_id: str | None = None) -> list[dict]:
    with get_connection() as conn:
        if status and tenant_id:
            rows = conn.execute(
                """
                SELECT * FROM external_outbox
                WHERE status = ? AND tenant_id = ?
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT ?
                """,
                (status, tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        elif status:
            rows = conn.execute(
                """
                SELECT * FROM external_outbox
                WHERE status = ?
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT ?
                """,
                (status, max(1, min(limit, 500))),
            ).fetchall()
        elif tenant_id:
            rows = conn.execute(
                """
                SELECT * FROM external_outbox
                WHERE tenant_id = ?
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT ?
                """,
                (tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM external_outbox
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
    return [event for event in (hydrate_outbox_event(item) for item in rows_to_dicts(rows)) if event]


def list_due_outbox_events(limit: int = 20, tenant_id: str | None = None) -> list[dict]:
    now = utc_now()
    max_attempts = max(1, settings.outbox_max_attempts)
    with get_connection() as conn:
        if tenant_id:
            rows = conn.execute(
                """
                SELECT * FROM external_outbox
                WHERE status IN ('pending', 'failed')
                  AND tenant_id = ?
                  AND attempt_count < ?
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (tenant_id, max_attempts, now, max(1, min(limit, 100))),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM external_outbox
                WHERE status IN ('pending', 'failed')
                  AND attempt_count < ?
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (max_attempts, now, max(1, min(limit, 100))),
            ).fetchall()
    return [event for event in (hydrate_outbox_event(item) for item in rows_to_dicts(rows)) if event]


def retry_outbox_event(outbox_id: str, tenant_id: str | None = None) -> dict:
    with rls_system_context() if tenant_id is None else tenant_context(tenant_id):
        event = get_outbox_event(outbox_id)
        if not event:
            raise ValueError(f"Outbox event not found: {outbox_id}")
        if tenant_id and event.get("tenant_id") != tenant_id:
            raise ValueError(f"Outbox event not found: {outbox_id}")
        if event["status"] == "completed":
            return {"outbox_id": outbox_id, "status": "skipped", "event": event, "reason": "already_completed"}

        try:
            result = _dispatch_outbox_event(event)
        except Exception as exc:
            refreshed = _schedule_outbox_retry(outbox_id, str(exc))
            return {
                "outbox_id": outbox_id,
                "status": "failed",
                "event": refreshed or event,
                "error": str(exc),
            }

        refreshed = get_outbox_event(outbox_id)
        return {
            "outbox_id": outbox_id,
            "status": "completed" if refreshed and refreshed.get("status") == "completed" else "attempted",
            "event": refreshed or event,
            "result": result,
        }


def retry_outbox_events(status: str = "failed", limit: int = 20, tenant_id: str | None = None) -> dict:
    with rls_system_context() if tenant_id is None else tenant_context(tenant_id):
        events = list_outbox_events(status=status, limit=max(1, min(limit, 100)), tenant_id=tenant_id)
        results = [retry_outbox_event(event["id"], tenant_id=tenant_id or event.get("tenant_id")) for event in events]
    return {
        "requested_status": status,
        "attempted": len(results),
        "completed": sum(1 for item in results if item["status"] == "completed"),
        "failed": sum(1 for item in results if item["status"] == "failed"),
        "skipped": sum(1 for item in results if item["status"] == "skipped"),
        "results": results,
    }


def dispatch_due_outbox_events(limit: int | None = None, tenant_id: str | None = None) -> dict:
    with rls_system_context() if tenant_id is None else tenant_context(tenant_id):
        events = list_due_outbox_events(limit=limit or settings.outbox_dispatch_batch_size, tenant_id=tenant_id)
        results = [retry_outbox_event(event["id"], tenant_id=tenant_id or event.get("tenant_id")) for event in events]
    return {
        "attempted": len(results),
        "completed": sum(1 for item in results if item["status"] == "completed"),
        "failed": sum(1 for item in results if item["status"] == "failed"),
        "skipped": sum(1 for item in results if item["status"] == "skipped"),
        "results": results,
    }


def outbox_dispatcher_status() -> dict[str, Any]:
    return {
        "enabled": settings.outbox_dispatcher_enabled,
        "embedded_enabled": settings.embedded_outbox_dispatcher_enabled,
        "interval_seconds": settings.outbox_dispatch_interval_seconds,
        "batch_size": settings.outbox_dispatch_batch_size,
        "max_attempts": settings.outbox_max_attempts,
        "retry_backoff_seconds": settings.outbox_retry_backoff_seconds,
    }


def outbox_status_counts() -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM external_outbox
            GROUP BY status
            ORDER BY status ASC
            """
        ).fetchall()
    return rows_to_dicts(rows)


def hydrate_outbox_event(event: dict | None) -> dict | None:
    if not event:
        return None
    event["payload"] = json_loads(event.pop("payload_json", None), {})
    event["response"] = json_loads(event.pop("response_json", None), {})
    return event


def _dispatch_outbox_event(event: dict) -> dict:
    payload = event.get("payload") or {}
    action_type = event.get("action_type")
    if action_type == "ticket.create":
        from app.services.tools.ticketing import create_ticket

        return create_ticket(
            title=payload.get("title") or "Recovered ticket",
            description=payload.get("description") or "",
            customer_id=payload.get("customer_id"),
            priority=payload.get("priority") or "normal",
            owner_department=payload.get("owner_department") or "Customer Success",
            workflow_type=payload.get("workflow_type"),
            category=payload.get("category"),
            risk_level=payload.get("risk_level"),
            approval_chain=payload.get("approval_chain") or [],
            auto_actions=payload.get("auto_actions") or [],
            blocked_actions=payload.get("blocked_actions") or [],
            evidence=payload.get("evidence") or [],
            agent_run_id=payload.get("agent_run_id"),
            approval_id=payload.get("approval_id"),
            tenant_id=payload.get("tenant_id") or event.get("tenant_id"),
        )
    if action_type == "ticket.update":
        from app.services.tools.ticketing import replay_ticket_update

        return replay_ticket_update(event["id"], payload)
    if action_type == "email.send":
        from app.services.tools.email import send_email

        return send_email(
            to_address=payload.get("to_address") or "",
            subject=payload.get("subject") or "",
            body=payload.get("body") or "",
            approval_id=payload.get("approval_id"),
            tenant_id=payload.get("tenant_id") or event.get("tenant_id"),
        )

    mark_outbox_running(event["id"])
    mark_outbox_failed(event["id"], f"Unsupported outbox action type: {action_type}")
    raise ValueError(f"Unsupported outbox action type: {action_type}")


def _schedule_outbox_retry(outbox_id: str, error_message: str) -> dict | None:
    refreshed = get_outbox_event(outbox_id)
    if not refreshed:
        return None
    attempt_count = int(refreshed.get("attempt_count") or 0)
    if attempt_count >= max(1, settings.outbox_max_attempts):
        mark_outbox_failed(outbox_id, error_message, next_attempt_at=None)
        return get_outbox_event(outbox_id)
    delay_seconds = max(1.0, settings.outbox_retry_backoff_seconds) * (2 ** max(0, attempt_count - 1))
    next_attempt_at = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).replace(microsecond=0).isoformat()
    mark_outbox_failed(outbox_id, error_message, next_attempt_at=next_attempt_at)
    return get_outbox_event(outbox_id)
