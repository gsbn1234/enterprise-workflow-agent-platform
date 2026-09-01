from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db import get_connection, row_to_dict, rows_to_dicts
from app.services.audit import record_audit
from app.services.outbox import create_outbox_event, mark_outbox_completed, mark_outbox_failed, mark_outbox_running
from app.services.tenancy import effective_tenant_id
from app.utils import json_dumps, json_loads, new_id, utc_now


TICKET_STATUSES = {
    "open",
    "investigating",
    "waiting_approval",
    "approved",
    "rejected",
    "waiting_customer",
    "resolved",
    "closed",
}
TICKET_PRIORITIES = {"low", "normal", "high", "urgent"}
ALLOWED_STATUS_TRANSITIONS = {
    "open": {"investigating", "waiting_approval", "approved", "rejected", "waiting_customer", "resolved", "closed"},
    "investigating": {"open", "waiting_approval", "approved", "rejected", "waiting_customer", "resolved", "closed"},
    "waiting_approval": {"open", "investigating", "approved", "rejected", "closed"},
    "approved": {"open", "investigating", "waiting_customer", "resolved", "closed"},
    "rejected": {"open", "closed"},
    "waiting_customer": {"open", "investigating", "resolved", "closed"},
    "resolved": {"open", "investigating", "closed"},
    "closed": {"open"},
}


def create_ticket(
    title: str,
    description: str,
    *,
    customer_id: str | None = None,
    priority: str = "normal",
    owner_department: str = "Customer Success",
    workflow_type: str | None = None,
    category: str | None = None,
    risk_level: str | None = None,
    approval_chain: list[str] | None = None,
    auto_actions: list[str] | None = None,
    blocked_actions: list[str] | None = None,
    evidence: list[dict] | None = None,
    agent_run_id: str | None = None,
    approval_id: str | None = None,
    tenant_id: str | None = None,
) -> dict:
    provider = _ticket_provider()
    tenant = effective_tenant_id(tenant_id)
    priority = normalize_ticket_priority(priority)
    title = str(title or "").strip()
    owner_department = str(owner_department or "").strip()
    if not title:
        raise ValueError("Ticket title is required.")
    if not owner_department:
        raise ValueError("Ticket owner_department is required.")
    if customer_id:
        with get_connection() as conn:
            customer = conn.execute(
                "SELECT id FROM customers WHERE id = ? AND tenant_id = ? LIMIT 1",
                (customer_id, tenant),
            ).fetchone()
        if not customer:
            raise ValueError("Ticket customer_id was not found in the tenant.")
    idempotency_key = _ticket_idempotency_key(
        title=title,
        description=description,
        customer_id=customer_id,
        priority=priority,
        owner_department=owner_department,
        provider=provider,
        tenant_id=tenant,
        agent_run_id=agent_run_id,
        approval_id=approval_id,
    )
    existing = _find_ticket_by_idempotency_key(idempotency_key)
    if existing:
        record_audit(
            "ticket.idempotent_reuse",
            "ticket",
            existing["id"],
            {"idempotency_key": idempotency_key, "provider": provider},
            tenant_id=tenant,
        )
        return existing

    outbox_payload = {
        "title": title,
        "description": description,
        "customer_id": customer_id,
        "priority": priority,
        "owner_department": owner_department,
        "workflow_type": workflow_type,
        "category": category,
        "risk_level": risk_level,
        "approval_chain": approval_chain or [],
        "auto_actions": auto_actions or [],
        "blocked_actions": blocked_actions or [],
        "evidence": evidence or [],
        "agent_run_id": agent_run_id,
        "approval_id": approval_id,
        "tenant_id": tenant,
    }
    outbox = create_outbox_event(
        "ticket.create",
        provider,
        outbox_payload,
        idempotency_key=idempotency_key,
        target_type="ticket",
    )
    mark_outbox_running(outbox["id"])

    external = {"external_id": None, "external_url": None, "payload": {}}
    try:
        if provider == "http":
            external = _create_http_ticket(
                title,
                description,
                customer_id,
                priority,
                owner_department,
                workflow_type=workflow_type,
                category=category,
                risk_level=risk_level,
                approval_chain=approval_chain,
                auto_actions=auto_actions,
                blocked_actions=blocked_actions,
                evidence=evidence,
                agent_run_id=agent_run_id,
                approval_id=approval_id,
                idempotency_key=idempotency_key,
                tenant_id=tenant,
            )
        elif provider == "jira":
            external = _create_jira_ticket(title, description, priority, owner_department)
        elif provider != "mock":
            raise ValueError(f"Unsupported ticket provider: {provider}")
    except Exception as exc:
        mark_outbox_failed(outbox["id"], str(exc), response=external["payload"])
        raise

    ticket_id = new_id("ticket")
    now = utc_now()
    due_at = ticket_due_at(priority, now)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO tickets
            (id, title, description, customer_id, status, priority, owner_department, tenant_id, provider,
             external_id, external_url, idempotency_key, external_payload_json, due_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticket_id,
                title,
                description,
                customer_id,
                priority,
                owner_department,
                tenant,
                provider,
                external["external_id"],
                external["external_url"],
                idempotency_key,
                json_dumps(external["payload"]),
                due_at,
                now,
                now,
            ),
        )
        _insert_ticket_event(
            conn,
            ticket_id,
            tenant,
            "created",
            agent_run_id or "agent",
            "Ticket created by Agent workflow.",
            to_status="open",
            payload={"priority": priority, "owner_department": owner_department, "provider": provider},
            created_at=now,
        )
    mark_outbox_completed(
        outbox["id"],
        target_id=ticket_id,
        response={
            "ticket_id": ticket_id,
            "external_id": external["external_id"],
            "external_url": external["external_url"],
            "provider": provider,
        },
    )
    record_audit(
        "ticket.create",
        "ticket",
        ticket_id,
        {
            "title": title,
            "priority": priority,
            "provider": provider,
            "external_id": external["external_id"],
            "external_url": external["external_url"],
            "idempotency_key": idempotency_key,
        },
        tenant_id=tenant,
    )
    return get_ticket(ticket_id)


def update_ticket(
    ticket_id: str,
    status: str | None = None,
    owner_department: str | None = None,
    priority: str | None = None,
    comment: str | None = None,
    actor: str = "agent",
    approval_id: str | None = None,
    agent_run_id: str | None = None,
    evidence: list[dict] | None = None,
    tenant_id: str | None = None,
) -> dict | None:
    tenant = effective_tenant_id(tenant_id) if tenant_id is not None else None
    with get_connection() as conn:
        if tenant:
            current = conn.execute("SELECT * FROM tickets WHERE id = ? AND tenant_id = ?", (ticket_id, tenant)).fetchone()
        else:
            current = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if not current:
        return None
    current_ticket = row_to_dict(current) or {}
    normalized_status = normalize_ticket_status(status) if status is not None else None
    normalized_priority = normalize_ticket_priority(priority) if priority is not None else None
    normalized_owner = str(owner_department).strip() if owner_department is not None else None
    if owner_department is not None and not normalized_owner:
        raise ValueError("Ticket owner_department cannot be empty.")
    validate_ticket_transition(str(current_ticket.get("status") or "open"), normalized_status)

    payload = {
        "ticket_id": ticket_id,
        "status": normalized_status,
        "owner_department": normalized_owner,
        "priority": normalized_priority,
        "comment": comment,
        "actor": actor,
        "approval_id": approval_id,
        "agent_run_id": agent_run_id,
        "evidence": evidence,
        "tenant_id": current_ticket.get("tenant_id"),
    }
    provider = str(current_ticket.get("provider") or "mock")
    if provider in {"http", "jira"}:
        idempotency_key = _ticket_update_idempotency_key(current_ticket, payload)
        outbox = create_outbox_event(
            "ticket.update",
            provider,
            payload,
            idempotency_key=idempotency_key,
            target_type="ticket",
            target_id=ticket_id,
            tenant_id=current_ticket.get("tenant_id"),
        )
        if outbox.get("status") == "completed":
            return get_ticket(ticket_id)
        return replay_ticket_update(outbox["id"], payload)
    return _apply_local_ticket_update(current_ticket, payload, external_payload=None)


def replay_ticket_update(outbox_id: str, payload: dict) -> dict:
    ticket_id = str(payload.get("ticket_id") or "")
    current_ticket = get_ticket(ticket_id)
    if not current_ticket:
        mark_outbox_failed(outbox_id, f"Ticket not found: {ticket_id}", target_id=ticket_id or None)
        raise ValueError(f"Ticket not found: {ticket_id}")
    expected_tenant = effective_tenant_id(payload.get("tenant_id"))
    if current_ticket.get("tenant_id") != expected_tenant:
        mark_outbox_failed(outbox_id, f"Ticket not found in tenant: {expected_tenant}", target_id=ticket_id)
        raise ValueError(f"Ticket not found in tenant: {expected_tenant}")
    normalized_status = normalize_ticket_status(payload.get("status")) if payload.get("status") is not None else None
    normalized_priority = normalize_ticket_priority(payload.get("priority")) if payload.get("priority") is not None else None
    payload = {**payload, "status": normalized_status, "priority": normalized_priority}
    validate_ticket_transition(str(current_ticket.get("status") or "open"), normalized_status)
    mark_outbox_running(outbox_id)
    external_payload: dict | None = None
    try:
        provider = str(current_ticket.get("provider") or "mock")
        if provider == "http":
            external_payload = _update_http_ticket(current_ticket, **_external_update_arguments(payload))
        elif provider == "jira":
            external_payload = _update_jira_ticket(current_ticket, **_external_update_arguments(payload))
        elif provider != "mock":
            raise ValueError(f"Unsupported ticket provider: {provider}")
        updated = _apply_local_ticket_update(current_ticket, payload, external_payload=external_payload)
    except Exception as exc:
        mark_outbox_failed(outbox_id, str(exc), response=external_payload or {}, target_id=ticket_id)
        raise
    mark_outbox_completed(
        outbox_id,
        target_id=ticket_id,
        response={"ticket_id": ticket_id, "provider": current_ticket.get("provider"), "external": external_payload or {}},
    )
    return updated


def _apply_local_ticket_update(current_ticket: dict, payload: dict, *, external_payload: dict | None) -> dict:
    ticket_id = str(current_ticket["id"])
    updated_at = utc_now()
    next_status = payload.get("status") or current_ticket.get("status") or "open"
    resolved_at = current_ticket.get("resolved_at")
    closed_at = current_ticket.get("closed_at")
    if next_status in {"resolved", "rejected"}:
        resolved_at = resolved_at or updated_at
        closed_at = None
    elif next_status == "closed":
        resolved_at = resolved_at or updated_at
        closed_at = closed_at or updated_at
    elif next_status in {"open", "investigating", "waiting_approval", "approved", "waiting_customer"} and payload.get("status"):
        resolved_at = None
        closed_at = None
    due_at = (
        ticket_due_at(payload["priority"], str(current_ticket.get("created_at") or updated_at))
        if payload.get("priority")
        else current_ticket.get("due_at")
    )
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE tickets
            SET status = COALESCE(?, status),
                owner_department = COALESCE(?, owner_department),
                priority = COALESCE(?, priority),
                external_payload_json = COALESCE(?, external_payload_json),
                due_at = ?,
                resolved_at = ?,
                closed_at = ?,
                updated_at = ?
            WHERE id = ? AND tenant_id = ?
            """,
            (
                payload.get("status"),
                payload.get("owner_department"),
                payload.get("priority"),
                json_dumps(external_payload) if external_payload else None,
                due_at,
                resolved_at,
                closed_at,
                updated_at,
                ticket_id,
                current_ticket.get("tenant_id"),
            ),
        )
        changed = {
            key: payload.get(key)
            for key in ("status", "owner_department", "priority", "approval_id", "agent_run_id")
            if payload.get(key) is not None
        }
        event_type = "status_changed" if payload.get("status") and payload.get("status") != current_ticket.get("status") else "comment"
        if event_type == "comment" and any(payload.get(key) is not None for key in ("owner_department", "priority")):
            event_type = "fields_updated"
        _insert_ticket_event(
            conn,
            ticket_id,
            str(current_ticket.get("tenant_id") or "default"),
            event_type,
            str(payload.get("actor") or "agent"),
            str(payload.get("comment") or "Ticket fields updated.").strip(),
            from_status=str(current_ticket.get("status")) if event_type == "status_changed" else None,
            to_status=payload.get("status") if event_type == "status_changed" else None,
            payload=changed,
            created_at=updated_at,
        )
    record_audit(
        "ticket.update",
        "ticket",
        ticket_id,
        {
            "from_status": current_ticket.get("status"),
            "status": payload.get("status"),
            "owner_department": payload.get("owner_department"),
            "priority": payload.get("priority"),
            "comment": payload.get("comment"),
            "approval_id": payload.get("approval_id"),
            "provider_synced": current_ticket.get("provider") in {"http", "jira"},
        },
        actor=payload.get("actor") or "agent",
        tenant_id=current_ticket.get("tenant_id"),
    )
    return get_ticket(ticket_id) or {}


def get_ticket(ticket_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    return _hydrate_ticket(row_to_dict(row))


def list_ticket_events(
    ticket_id: str,
    *,
    tenant_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    tenant = effective_tenant_id(tenant_id) if tenant_id is not None else None
    clauses = ["ticket_id = ?"]
    params: list[object] = [ticket_id]
    if tenant:
        clauses.append("tenant_id = ?")
        params.append(tenant)
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM ticket_events
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, max(1, min(int(limit), 500)), max(0, int(offset))),
        ).fetchall()
    events = rows_to_dicts(rows)
    for event in events:
        event["payload"] = json_loads(event.pop("payload_json", "{}"), {})
    return events


def add_ticket_comment(
    ticket_id: str,
    body: str,
    *,
    actor: str = "user",
    tenant_id: str | None = None,
) -> dict:
    comment = str(body or "").strip()
    if not comment:
        raise ValueError("Ticket comment is required.")
    ticket = update_ticket(ticket_id, comment=comment, actor=actor, tenant_id=tenant_id)
    if not ticket:
        raise ValueError("Ticket not found.")
    events = list_ticket_events(ticket_id, tenant_id=tenant_id, limit=1)
    return events[0] if events else {"ticket_id": ticket_id, "body": comment, "actor": actor}


def _insert_ticket_event(
    conn,
    ticket_id: str,
    tenant_id: str,
    event_type: str,
    actor: str,
    body: str,
    *,
    from_status: str | None = None,
    to_status: str | None = None,
    payload: dict | None = None,
    created_at: str | None = None,
) -> str:
    event_id = new_id("ticket_event")
    conn.execute(
        """
        INSERT INTO ticket_events
        (id, ticket_id, tenant_id, event_type, actor, body, from_status, to_status, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            ticket_id,
            tenant_id,
            event_type,
            actor,
            body,
            from_status,
            to_status,
            json_dumps(payload or {}),
            created_at or utc_now(),
        ),
    )
    return event_id


def _find_ticket_by_idempotency_key(idempotency_key: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
    return _hydrate_ticket(row_to_dict(row))


def find_ticket(ticket_ref: str) -> dict | None:
    ref = str(ticket_ref or "").strip()
    if not ref:
        return None
    lowered = ref.lower()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM tickets
            WHERE lower(id) = ?
               OR lower(external_id) = ?
               OR lower(external_url) = ?
            LIMIT 1
            """,
            (lowered, lowered, lowered),
        ).fetchone()
    return _hydrate_ticket(row_to_dict(row))


def query_tickets(
    *,
    ticket_ref: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    owner_department: str | None = None,
    q: str | None = None,
    tenant_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    if ticket_ref:
        ticket = find_ticket(ticket_ref)
        if tenant_id and ticket and ticket.get("tenant_id") != tenant_id:
            ticket = None
        tickets = [ticket] if ticket else []
        return {"tickets": tickets, "count": len(tickets), "filters": {"ticket_ref": ticket_ref}}

    clauses = []
    params: list[object] = []
    if status:
        clauses.append("lower(status) = ?")
        params.append(normalize_ticket_status(status))
    if priority:
        clauses.append("lower(priority) = ?")
        params.append(normalize_ticket_priority(priority))
    if owner_department:
        clauses.append("lower(owner_department) = ?")
        params.append(owner_department.lower())
    if q:
        like = f"%{q.lower()}%"
        clauses.append(
            "(lower(title) LIKE ? OR lower(description) LIKE ? OR lower(id) LIKE ? OR lower(COALESCE(external_id, '')) LIKE ?)"
        )
        params.extend([like, like, like, like])
    if tenant_id:
        clauses.append("tenant_id = ?")
        params.append(tenant_id)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    safe_limit = max(1, min(int(limit), 500))
    safe_offset = max(0, int(offset))
    with get_connection() as conn:
        count_row = conn.execute(f"SELECT COUNT(*) AS count FROM tickets {where}", params).fetchone()
        rows = conn.execute(
            f"""
            SELECT * FROM tickets
            {where}
            ORDER BY updated_at DESC, created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, safe_limit, safe_offset),
        ).fetchall()
    tickets = [_hydrate_ticket(ticket) for ticket in rows_to_dicts(rows)]
    return {
        "tickets": [ticket for ticket in tickets if ticket],
        "count": int((row_to_dict(count_row) or {}).get("count", 0)),
        "filters": {
            "status": status,
            "priority": priority,
            "owner_department": owner_department,
            "q": q,
            "limit": safe_limit,
            "offset": safe_offset,
        },
    }


def list_tickets(limit: int = 100, tenant_id: str | None = None, *, offset: int = 0) -> list[dict]:
    return query_tickets(limit=limit, offset=offset, tenant_id=tenant_id)["tickets"]


def _hydrate_ticket(ticket: dict | None) -> dict | None:
    if ticket and ticket.get("external_payload_json"):
        ticket["external_payload"] = json_loads(ticket.pop("external_payload_json"), {})
    if ticket:
        ticket["sla"] = ticket_sla_state(ticket)
    return ticket


def ticket_due_at(priority: str, created_at: str | None = None) -> str:
    hours = {"urgent": 1, "high": 4, "normal": 24, "low": 72}[normalize_ticket_priority(priority)]
    base = _parse_datetime(created_at) if created_at else datetime.now(timezone.utc)
    return (base + timedelta(hours=hours)).isoformat(timespec="seconds")


def ticket_sla_state(ticket: dict) -> dict[str, object]:
    status = str(ticket.get("status") or "open")
    due_at = ticket.get("due_at")
    if status in {"resolved", "closed", "rejected"}:
        completed_at = ticket.get("closed_at") or ticket.get("resolved_at") or ticket.get("updated_at")
        breached = bool(due_at and completed_at and _parse_datetime(str(completed_at)) > _parse_datetime(str(due_at)))
        return {"state": "breached" if breached else "met", "breached": breached, "due_at": due_at, "completed_at": completed_at}
    breached = bool(due_at and datetime.now(timezone.utc) > _parse_datetime(str(due_at)))
    return {"state": "breached" if breached else "active", "breached": breached, "due_at": due_at, "completed_at": None}


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _ticket_provider() -> str:
    if settings.ticket_provider:
        return settings.ticket_provider
    if settings.tool_mode != "real":
        return "mock"
    if settings.jira_base_url:
        return "jira"
    return "http"


def _create_http_ticket(
    title: str,
    description: str,
    customer_id: str | None,
    priority: str,
    owner_department: str,
    *,
    workflow_type: str | None = None,
    category: str | None = None,
    risk_level: str | None = None,
    approval_chain: list[str] | None = None,
    auto_actions: list[str] | None = None,
    blocked_actions: list[str] | None = None,
    evidence: list[dict] | None = None,
    agent_run_id: str | None = None,
    approval_id: str | None = None,
    idempotency_key: str | None = None,
    tenant_id: str | None = None,
) -> dict:
    if not settings.ticket_api_url:
        raise ValueError("TICKET_API_URL is required when AGENT_TICKET_PROVIDER=http or AGENT_TOOL_MODE=real.")
    payload = {
        "title": title,
        "description": description,
        "customer_id": customer_id,
        "priority": priority,
        "owner_department": owner_department,
        "workflow_type": workflow_type,
        "category": category,
        "risk_level": risk_level,
        "approval_chain": approval_chain or [],
        "auto_actions": auto_actions or [],
        "blocked_actions": blocked_actions or [],
        "evidence": evidence or [],
        "agent_run_id": agent_run_id,
        "approval_id": approval_id,
        "idempotency_key": idempotency_key,
        "tenant_id": effective_tenant_id(tenant_id),
        "source": "agent",
    }
    headers = {"Content-Type": "application/json"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    if settings.ticket_api_token:
        headers["Authorization"] = f"Bearer {settings.ticket_api_token}"
    data = _request_json(settings.ticket_api_url, payload, headers)
    external_id = _pick_field(data, settings.ticket_http_id_field) or data.get("key") or data.get("ticket_id")
    external_url = _pick_field(data, settings.ticket_http_url_field) or data.get("self") or data.get("html_url")
    return {"external_id": str(external_id) if external_id else None, "external_url": external_url, "payload": data}


def _create_jira_ticket(title: str, description: str, priority: str, owner_department: str) -> dict:
    missing = [
        name
        for name, value in {
            "JIRA_BASE_URL": settings.jira_base_url,
            "JIRA_EMAIL": settings.jira_email,
            "JIRA_API_TOKEN": settings.jira_api_token,
            "JIRA_PROJECT_KEY": settings.jira_project_key,
        }.items()
        if not value
    ]
    if missing:
        raise ValueError(f"Missing Jira settings: {', '.join(missing)}")
    url = f"{settings.jira_base_url}/rest/api/3/issue"
    auth = base64.b64encode(f"{settings.jira_email}:{settings.jira_api_token}".encode("utf-8")).decode("ascii")
    payload = {
        "fields": {
            "project": {"key": settings.jira_project_key},
            "summary": title,
            "description": _jira_doc(f"{description}\n\nOwner department: {owner_department}\nPriority: {priority}"),
            "issuetype": {"name": settings.jira_issue_type},
        }
    }
    data = _request_json(url, payload, {"Content-Type": "application/json", "Authorization": f"Basic {auth}"})
    key = data.get("key") or data.get("id")
    external_url = f"{settings.jira_base_url}/browse/{key}" if key else data.get("self")
    return {"external_id": str(key) if key else None, "external_url": external_url, "payload": data}


def _request_json(url: str, payload: dict, headers: dict[str, str]) -> dict:
    return _request_json_with_method("POST", url, payload, headers)


def _patch_json(url: str, payload: dict, headers: dict[str, str]) -> dict:
    return _request_json_with_method("PATCH", url, payload, headers)


def _put_json(url: str, payload: dict, headers: dict[str, str]) -> dict:
    return _request_json_with_method("PUT", url, payload, headers)


def _get_json(url: str, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=settings.ticket_api_timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ticket API returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ticket API request failed: {exc.reason}") from exc
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Ticket API returned non-JSON response: {raw[:300]}") from exc


def _request_json_with_method(method: str, url: str, payload: dict, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.ticket_api_timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ticket API returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ticket API request failed: {exc.reason}") from exc
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Ticket API returned non-JSON response: {raw[:300]}") from exc


def _update_http_ticket(
    ticket: dict,
    *,
    status: str | None,
    owner_department: str | None,
    priority: str | None,
    comment: str | None,
    actor: str,
    approval_id: str | None,
    agent_run_id: str | None,
    evidence: list[dict] | None,
) -> dict | None:
    payload = {
        key: value
        for key, value in {
            "status": status,
            "owner_department": owner_department,
            "priority": priority,
            "comment": comment,
            "actor": actor,
            "approval_id": approval_id,
            "agent_run_id": agent_run_id,
            "evidence": evidence,
        }.items()
        if value is not None
    }
    if not payload:
        return
    external_payload = ticket.get("external_payload") or json_loads(ticket.get("external_payload_json"), {})
    external_ref = ticket.get("external_id") or external_payload.get("id") or external_payload.get("key") or external_payload.get("ticket_id")
    update_url = f"{settings.ticket_api_url.rstrip('/')}/{external_ref}" if external_ref else external_payload.get("self")
    if not update_url:
        return
    headers = {"Content-Type": "application/json"}
    if settings.ticket_api_token:
        headers["Authorization"] = f"Bearer {settings.ticket_api_token}"
    return _patch_json(update_url, payload, headers)


def _update_jira_ticket(
    ticket: dict,
    *,
    status: str | None,
    owner_department: str | None,
    priority: str | None,
    comment: str | None,
    actor: str,
    approval_id: str | None,
    agent_run_id: str | None,
    evidence: list[dict] | None,
) -> dict | None:
    external_ref = ticket.get("external_id")
    if not external_ref:
        raise ValueError(f"Jira ticket {ticket.get('id')} has no external issue key.")
    if not settings.jira_base_url or not settings.jira_email or not settings.jira_api_token:
        raise ValueError("JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN are required for Jira updates.")
    auth = base64.b64encode(f"{settings.jira_email}:{settings.jira_api_token}".encode("utf-8")).decode("ascii")
    headers = {"Content-Type": "application/json", "Authorization": f"Basic {auth}"}
    issue_url = f"{settings.jira_base_url}/rest/api/3/issue/{external_ref}"
    result: dict[str, object] = {}
    fields: dict[str, object] = {}
    if priority:
        fields["priority"] = {"name": priority.capitalize()}
    if fields:
        result["fields"] = _put_json(issue_url, {"fields": fields}, headers)
    note_parts = [part for part in [comment, f"Owner department: {owner_department}" if owner_department else None] if part]
    if approval_id:
        note_parts.append(f"Approval: {approval_id}")
    if agent_run_id:
        note_parts.append(f"Agent run: {agent_run_id}")
    if evidence:
        note_parts.append(f"Evidence items: {len(evidence)}")
    if note_parts:
        result["comment"] = _request_json(
            f"{issue_url}/comment",
            {"body": _jira_doc("\n".join(note_parts) + f"\nActor: {actor}")},
            headers,
        )
    if status and status != ticket.get("status"):
        transitions = _get_json(f"{issue_url}/transitions", headers).get("transitions") or []
        wanted = _status_key(status)
        transition = next(
            (
                item
                for item in transitions
                if _status_key(str((item.get("to") or {}).get("name") or item.get("name") or "")) == wanted
            ),
            None,
        )
        if not transition:
            available = [str((item.get("to") or {}).get("name") or item.get("name") or "") for item in transitions]
            raise RuntimeError(f"Jira has no transition to {status!r}. Available transitions: {available}.")
        result["transition"] = _request_json(
            f"{issue_url}/transitions",
            {"transition": {"id": str(transition["id"])}},
            headers,
        )
    return result or None


def _external_update_arguments(payload: dict) -> dict:
    return {
        "status": payload.get("status"),
        "owner_department": payload.get("owner_department"),
        "priority": payload.get("priority"),
        "comment": payload.get("comment"),
        "actor": payload.get("actor") or "agent",
        "approval_id": payload.get("approval_id"),
        "agent_run_id": payload.get("agent_run_id"),
        "evidence": payload.get("evidence"),
    }


def normalize_ticket_status(status: str) -> str:
    value = str(status or "").strip().lower()
    if value not in TICKET_STATUSES:
        raise ValueError(f"Unsupported ticket status: {status!r}. Allowed values: {', '.join(sorted(TICKET_STATUSES))}.")
    return value


def normalize_ticket_priority(priority: str) -> str:
    value = str(priority or "").strip().lower()
    if value not in TICKET_PRIORITIES:
        raise ValueError(f"Unsupported ticket priority: {priority!r}. Allowed values: {', '.join(sorted(TICKET_PRIORITIES))}.")
    return value


def validate_ticket_transition(current_status: str, next_status: str | None) -> None:
    if next_status is None:
        return
    current = normalize_ticket_status(current_status)
    if next_status == current:
        return
    if next_status not in ALLOWED_STATUS_TRANSITIONS[current]:
        raise ValueError(f"Invalid ticket status transition: {current} -> {next_status}.")


def _ticket_update_idempotency_key(ticket: dict, payload: dict) -> str:
    digest = hashlib.sha256(
        json_dumps(
            {
                "ticket_id": ticket.get("id"),
                "provider": ticket.get("provider"),
                "base_updated_at": ticket.get("updated_at"),
                "status": payload.get("status"),
                "owner_department": payload.get("owner_department"),
                "priority": payload.get("priority"),
                "comment": payload.get("comment"),
                "approval_id": payload.get("approval_id"),
                "agent_run_id": payload.get("agent_run_id"),
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"ticket_update_{digest}"


def _status_key(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _pick_field(data: dict, path: str) -> str | None:
    current: object = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    if current is None:
        return None
    return str(current)


def _ticket_idempotency_key(
    *,
    title: str,
    description: str,
    customer_id: str | None,
    priority: str,
    owner_department: str,
    provider: str,
    tenant_id: str,
    agent_run_id: str | None,
    approval_id: str | None,
) -> str:
    payload = {
        "provider": provider,
        "tenant_id": tenant_id,
        "agent_run_id": agent_run_id,
        "approval_id": approval_id,
        "title": title,
        "description": description,
        "customer_id": customer_id,
        "priority": priority,
        "owner_department": owner_department,
    }
    digest = hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()
    return f"ticket_{digest}"


def _jira_doc(text: str) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text[:30000]}],
            }
        ],
    }
