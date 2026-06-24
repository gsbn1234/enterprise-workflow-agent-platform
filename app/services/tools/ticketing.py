from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.request

from app.config import settings
from app.db import get_connection, row_to_dict, rows_to_dicts
from app.services.audit import record_audit
from app.services.outbox import create_outbox_event, mark_outbox_completed, mark_outbox_failed, mark_outbox_running
from app.services.tenancy import effective_tenant_id
from app.utils import json_dumps, json_loads, new_id, utc_now


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
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO tickets
            (id, title, description, customer_id, status, priority, owner_department, tenant_id, provider,
             external_id, external_url, idempotency_key, external_payload_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                now,
                now,
            ),
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
) -> dict | None:
    with get_connection() as conn:
        current = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not current:
            return None
        current_ticket = row_to_dict(current) or {}
        external_payload = None
        if current_ticket.get("provider") == "http":
            external_payload = _update_http_ticket(
                current_ticket,
                status=status,
                owner_department=owner_department,
                priority=priority,
                comment=comment,
                actor=actor,
                approval_id=approval_id,
                agent_run_id=agent_run_id,
                evidence=evidence,
            )
        conn.execute(
            """
            UPDATE tickets
            SET status = COALESCE(?, status),
                owner_department = COALESCE(?, owner_department),
                priority = COALESCE(?, priority),
                external_payload_json = COALESCE(?, external_payload_json),
                updated_at = ?
            WHERE id = ?
            """,
            (status, owner_department, priority, json_dumps(external_payload) if external_payload else None, utc_now(), ticket_id),
        )
    record_audit(
        "ticket.update",
        "ticket",
        ticket_id,
        {
            "status": status,
            "owner_department": owner_department,
            "priority": priority,
            "comment": comment,
            "approval_id": approval_id,
        },
        actor=actor,
        tenant_id=current_ticket.get("tenant_id"),
    )
    return get_ticket(ticket_id)


def get_ticket(ticket_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    return _hydrate_ticket(row_to_dict(row))


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
) -> dict:
    if ticket_ref:
        ticket = find_ticket(ticket_ref)
        if tenant_id and ticket and ticket.get("tenant_id") != tenant_id:
            ticket = None
        tickets = [ticket] if ticket else []
        return {"tickets": tickets, "count": len(tickets), "filters": {"ticket_ref": ticket_ref}}

    clauses = []
    params: list[str] = []
    if status:
        clauses.append("lower(status) = ?")
        params.append(status.lower())
    if priority:
        clauses.append("lower(priority) = ?")
        params.append(priority.lower())
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
    params.append(str(max(1, min(limit, 500))))
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM tickets
            {where}
            ORDER BY updated_at DESC, created_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    tickets = [_hydrate_ticket(ticket) for ticket in rows_to_dicts(rows)]
    return {
        "tickets": [ticket for ticket in tickets if ticket],
        "count": len(tickets),
        "filters": {
            "status": status,
            "priority": priority,
            "owner_department": owner_department,
            "q": q,
            "limit": limit,
        },
    }


def list_tickets(limit: int = 100, tenant_id: str | None = None) -> list[dict]:
    return query_tickets(limit=limit, tenant_id=tenant_id)["tickets"]


def _hydrate_ticket(ticket: dict | None) -> dict | None:
    if ticket and ticket.get("external_payload_json"):
        ticket["external_payload"] = json_loads(ticket.pop("external_payload_json"), {})
    return ticket


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
    external_payload = json_loads(ticket.get("external_payload_json"), {})
    external_ref = ticket.get("external_id") or external_payload.get("id") or external_payload.get("key") or external_payload.get("ticket_id")
    update_url = f"{settings.ticket_api_url.rstrip('/')}/{external_ref}" if external_ref else external_payload.get("self")
    if not update_url:
        return
    headers = {"Content-Type": "application/json"}
    if settings.ticket_api_token:
        headers["Authorization"] = f"Bearer {settings.ticket_api_token}"
    return _patch_json(update_url, payload, headers)


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
