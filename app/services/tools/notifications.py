from __future__ import annotations

from app.services.audit import record_audit
from app.utils import compact_text, new_id, utc_now


def notify_internal_team(
    team: str,
    message: str,
    *,
    severity: str = "normal",
    ticket_id: str | None = None,
    channel: str = "internal_queue",
    tenant_id: str | None = None,
) -> dict:
    notification_id = new_id("notify")
    audit = record_audit(
        "notification.internal",
        "notification",
        notification_id,
        {
            "team": team,
            "severity": severity,
            "ticket_id": ticket_id,
            "channel": channel,
            "message": compact_text(message, 500),
        },
        actor="agent",
        tenant_id=tenant_id,
    )
    return {
        "id": notification_id,
        "team": team,
        "severity": severity,
        "ticket_id": ticket_id,
        "channel": channel,
        "status": "delivered",
        "audit_id": audit["id"],
        "created_at": utc_now(),
    }
