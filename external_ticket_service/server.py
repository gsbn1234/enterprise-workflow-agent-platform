from __future__ import annotations

import html
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(os.getenv("TICKET_SERVICE_DB_PATH", str(DATA_DIR / "external_ticket_service.sqlite3")))
PUBLIC_BASE_URL = os.getenv("TICKET_SERVICE_PUBLIC_BASE_URL", "http://127.0.0.1:8020").rstrip("/")

STATUSES = {
    "open",
    "investigating",
    "waiting_approval",
    "approved",
    "rejected",
    "waiting_customer",
    "resolved",
    "closed",
}

app = FastAPI(title="Local External Ticket Service", version="2.0.0")


class TicketCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    description: str = Field("", max_length=30000)
    customer_id: str | None = None
    priority: str = Field("normal", max_length=32)
    owner_department: str = Field("Customer Success", max_length=120)
    workflow_type: str | None = Field(None, max_length=120)
    category: str | None = Field(None, max_length=80)
    risk_level: str | None = Field(None, max_length=32)
    approval_chain: list[str] = Field(default_factory=list)
    auto_actions: list[str] = Field(default_factory=list)
    blocked_actions: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    agent_run_id: str | None = Field(None, max_length=120)
    approval_id: str | None = Field(None, max_length=120)
    idempotency_key: str | None = Field(None, max_length=160)
    source: str = Field("agent", max_length=80)


class TicketUpdate(BaseModel):
    status: str | None = Field(None, max_length=32)
    owner_department: str | None = Field(None, max_length=120)
    priority: str | None = Field(None, max_length=32)
    comment: str | None = Field(None, max_length=5000)
    actor: str = Field("agent", max_length=120)
    approval_id: str | None = Field(None, max_length=120)
    agent_run_id: str | None = Field(None, max_length=120)
    evidence: list[dict[str, Any]] | None = None


class TicketCommentCreate(BaseModel):
    body: str = Field(..., min_length=1, max_length=5000)
    actor: str = Field("user", max_length=120)
    event_type: str = Field("comment", max_length=64)
    visibility: str = Field("internal", max_length=32)


@app.on_event("startup")
def startup() -> None:
    init_db()


def require_api_token(authorization: str | None = Header(default=None)) -> None:
    expected = os.getenv("TICKET_SERVICE_TOKEN", "").strip()
    if not expected:
        return
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Invalid ticket service token.")


@app.get("/", response_class=HTMLResponse)
def dashboard(
    status: str | None = None,
    owner: str | None = None,
    priority: str | None = None,
    workflow_type: str | None = None,
    q: str | None = None,
    limit: int = Query(80, ge=1, le=300),
) -> str:
    init_db()
    filters = TicketFilters(status=status, owner=owner, priority=priority, workflow_type=workflow_type, q=q)
    tickets = list_ticket_rows(limit, filters)
    rows = "\n".join(_ticket_row(ticket) for ticket in tickets)
    stats = _ticket_stats(tickets)
    if not rows:
        rows = """
        <tr>
          <td colspan="10" class="empty">No tickets match the current filters.</td>
        </tr>
        """
    body = f"""
    <section class="hero">
      <div>
        <p class="eyebrow">HTTP Ticket API</p>
        <h1>External Ticket Desk</h1>
        <p>Independent ticket system used by the Agent through <code>{_escape(PUBLIC_BASE_URL)}</code>.</p>
      </div>
      <div class="hero-actions">
        <a class="button secondary" href="/api/tickets">JSON API</a>
        <button class="button danger" type="button" onclick="clearTickets()">Clear Demo Data</button>
      </div>
    </section>

    <section class="stats-grid">
      <article class="stat"><strong>{stats["total"]}</strong><span>Total</span></article>
      <article class="stat"><strong>{stats["waiting_approval"]}</strong><span>Waiting approval</span></article>
      <article class="stat"><strong>{stats["high_priority"]}</strong><span>High priority</span></article>
      <article class="stat"><strong>{stats["sla_watch"]}</strong><span>SLA watch</span></article>
    </section>

    <section class="filters panel">
      <form method="get">
        {_filter_input("q", "Search", q or "")}
        {_filter_input("status", "Status", status or "")}
        {_filter_input("owner", "Owner", owner or "")}
        {_filter_input("priority", "Priority", priority or "")}
        {_filter_input("workflow_type", "Workflow", workflow_type or "")}
        <button class="button" type="submit">Apply</button>
        <a class="button secondary" href="/">Reset</a>
      </form>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>Tickets</h2>
        <span>{len(tickets)} shown</span>
      </div>
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Title</th>
            <th>Workflow</th>
            <th>Risk</th>
            <th>Priority</th>
            <th>Status</th>
            <th>SLA</th>
            <th>Owner</th>
            <th>Updated</th>
            <th></th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </section>
    """
    return _page("External Ticket Desk", body)


@app.get("/tickets/{ticket_id}", response_class=HTMLResponse)
def ticket_detail(ticket_id: str) -> str:
    init_db()
    ticket = get_ticket_row(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    events = list_ticket_events(ticket_id, limit=200)
    body = f"""
    <section class="hero compact">
      <div>
        <p class="eyebrow">Ticket</p>
        <h1>{_escape(ticket["id"])}</h1>
        <p>{_escape(ticket["title"])}</p>
      </div>
      <div class="hero-actions">
        <a class="button secondary" href="/">Back</a>
        <a class="button secondary" href="/api/tickets/{_escape(ticket["id"])}">JSON</a>
      </div>
    </section>

    <section class="detail-grid">
      <article class="panel padded">
        <h2>Summary</h2>
        <dl>
          <dt>Status</dt><dd>{_status_pill(ticket["status"])}</dd>
          <dt>Priority</dt><dd>{_escape(ticket["priority"])}</dd>
          <dt>Workflow</dt><dd>{_escape(ticket["workflow_type"] or "-")}</dd>
          <dt>Category</dt><dd>{_escape(ticket["category"] or "-")}</dd>
          <dt>Risk</dt><dd>{_escape(ticket["risk_level"] or "-")}</dd>
          <dt>Owner</dt><dd>{_escape(ticket["owner_department"])}</dd>
          <dt>Customer ID</dt><dd>{_escape(ticket["customer_id"] or "-")}</dd>
          <dt>Agent Run</dt><dd>{_escape(ticket["agent_run_id"] or "-")}</dd>
          <dt>Approval</dt><dd>{_escape(ticket["approval_id"] or "-")}</dd>
          <dt>Created</dt><dd>{_escape(ticket["created_at"])}</dd>
          <dt>Updated</dt><dd>{_escape(ticket["updated_at"])}</dd>
        </dl>
      </article>

      <article class="panel padded">
        <h2>Operations</h2>
        {_ticket_actions_form(ticket)}
      </article>

      <article class="panel padded">
        <h2>Agent Policy</h2>
        {_list_block("Approval Chain", _json_list(ticket["approval_chain_json"]))}
        {_list_block("Auto Actions", _json_list(ticket["auto_actions_json"]))}
        {_list_block("Blocked Actions", _json_list(ticket["blocked_actions_json"]))}
      </article>

      <article class="panel padded span-2">
        <h2>Description</h2>
        <pre>{_escape(ticket["description"])}</pre>
      </article>

      <article class="panel padded span-2">
        <h2>Knowledge Evidence</h2>
        {_evidence_html(_json_list(ticket["evidence_json"]))}
      </article>

      <article class="panel padded span-2">
        <div class="panel-title-row">
          <h2>Timeline</h2>
          <span>{len(events)} events</span>
        </div>
        <form class="comment-form" onsubmit="return addComment(event, '{_escape(ticket["id"])}')">
          <input name="body" placeholder="Add an internal comment" />
          <button class="button" type="submit">Comment</button>
        </form>
        <div class="timeline">{''.join(_event_row(event) for event in events) or '<p class="muted">No events yet.</p>'}</div>
      </article>
    </section>
    """
    return _page(ticket["id"], body)


@app.get("/api/health")
def health() -> dict[str, str]:
    init_db()
    return {"status": "ok", "service": "local_external_ticket_service", "version": "2.0.0", "db_path": str(DB_PATH)}


@app.post("/api/tickets", dependencies=[Depends(require_api_token)])
def create_ticket(payload: TicketCreate, idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    init_db()
    now = utc_now()
    idempotency_key = payload.idempotency_key or idempotency_key_header
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if idempotency_key:
            existing = conn.execute("SELECT * FROM tickets WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if existing:
                return ticket_response(dict(existing), include_events=True)
        sequence = int(conn.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM tickets").fetchone()[0])
        ticket_id = f"EXT-{sequence:06d}"
        conn.execute(
            """
            INSERT INTO tickets
            (id, sequence, title, description, customer_id, priority, status, owner_department,
             workflow_type, category, risk_level, approval_chain_json, auto_actions_json,
             blocked_actions_json, evidence_json, agent_run_id, approval_id, idempotency_key, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticket_id,
                sequence,
                payload.title.strip(),
                payload.description,
                payload.customer_id,
                payload.priority.strip() or "normal",
                payload.owner_department.strip() or "Customer Success",
                payload.workflow_type,
                payload.category,
                payload.risk_level,
                json_dumps(payload.approval_chain),
                json_dumps(payload.auto_actions),
                json_dumps(payload.blocked_actions),
                json_dumps(payload.evidence[:10]),
                payload.agent_run_id,
                payload.approval_id,
                idempotency_key,
                payload.source,
                now,
                now,
            ),
        )
        insert_event(
            conn,
            ticket_id,
            "created",
            payload.source or "agent",
            "Ticket created by Agent.",
            to_status="open",
            payload={"priority": payload.priority, "owner_department": payload.owner_department},
            created_at=now,
        )
    ticket = get_ticket_row(ticket_id)
    if not ticket:
        raise HTTPException(status_code=500, detail="Ticket was not persisted.")
    return ticket_response(ticket, include_events=True)


@app.get("/api/tickets")
def list_tickets(
    status: str | None = None,
    owner: str | None = None,
    priority: str | None = None,
    workflow_type: str | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    init_db()
    filters = TicketFilters(status=status, owner=owner, priority=priority, workflow_type=workflow_type, q=q)
    return [ticket_response(ticket) for ticket in list_ticket_rows(limit, filters)]


@app.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: str) -> dict[str, Any]:
    init_db()
    ticket = get_ticket_row(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    return ticket_response(ticket, include_events=True)


@app.patch("/api/tickets/{ticket_id}", dependencies=[Depends(require_api_token)])
def update_ticket(ticket_id: str, payload: TicketUpdate) -> dict[str, Any]:
    init_db()
    ticket = get_ticket_row(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found.")

    next_status = _normalize_status(payload.status) if payload.status else None
    now = utc_now()
    with get_connection() as conn:
        if next_status and next_status != ticket["status"]:
            insert_event(
                conn,
                ticket_id,
                "status_changed",
                payload.actor,
                payload.comment or f"Status changed from {ticket['status']} to {next_status}.",
                from_status=ticket["status"],
                to_status=next_status,
                payload={"approval_id": payload.approval_id, "agent_run_id": payload.agent_run_id},
                created_at=now,
            )
        if payload.owner_department and payload.owner_department != ticket["owner_department"]:
            insert_event(
                conn,
                ticket_id,
                "owner_changed",
                payload.actor,
                f"Owner changed from {ticket['owner_department']} to {payload.owner_department}.",
                payload={"from": ticket["owner_department"], "to": payload.owner_department},
                created_at=now,
            )
        if payload.priority and payload.priority != ticket["priority"]:
            insert_event(
                conn,
                ticket_id,
                "priority_changed",
                payload.actor,
                payload.comment or f"Priority changed from {ticket['priority']} to {payload.priority}.",
                payload={"from": ticket["priority"], "to": payload.priority},
                created_at=now,
            )
        if payload.comment and not next_status:
            insert_event(conn, ticket_id, "agent_sync", payload.actor, payload.comment, payload={"agent_run_id": payload.agent_run_id}, created_at=now)

        evidence_json = json_dumps(payload.evidence[:10]) if payload.evidence is not None else None
        conn.execute(
            """
            UPDATE tickets
            SET status = COALESCE(?, status),
                owner_department = COALESCE(?, owner_department),
                priority = COALESCE(?, priority),
                approval_id = COALESCE(?, approval_id),
                agent_run_id = COALESCE(?, agent_run_id),
                evidence_json = COALESCE(?, evidence_json),
                updated_at = ?
            WHERE id = ?
            """,
            (
                next_status,
                payload.owner_department,
                payload.priority.strip().lower() if payload.priority else None,
                payload.approval_id,
                payload.agent_run_id,
                evidence_json,
                now,
                ticket_id,
            ),
        )
    updated = get_ticket_row(ticket_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    return ticket_response(updated, include_events=True)


@app.post("/api/tickets/{ticket_id}/comments", dependencies=[Depends(require_api_token)])
def add_comment(ticket_id: str, payload: TicketCommentCreate) -> dict[str, Any]:
    init_db()
    if not get_ticket_row(ticket_id):
        raise HTTPException(status_code=404, detail="Ticket not found.")
    with get_connection() as conn:
        event = insert_event(
            conn,
            ticket_id,
            payload.event_type,
            payload.actor,
            payload.body,
            payload={"visibility": payload.visibility},
            created_at=utc_now(),
        )
        conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (event["created_at"], ticket_id))
    return event


@app.post("/api/admin/clear", dependencies=[Depends(require_api_token)])
def clear_demo_data() -> dict[str, int]:
    init_db()
    with get_connection() as conn:
        event_count = conn.execute("DELETE FROM ticket_events").rowcount
        ticket_count = conn.execute("DELETE FROM tickets").rowcount
    return {"tickets": max(ticket_count, 0), "events": max(event_count, 0)}


class TicketFilters(BaseModel):
    status: str | None = None
    owner: str | None = None
    priority: str | None = None
    workflow_type: str | None = None
    q: str | None = None


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id TEXT PRIMARY KEY,
                sequence INTEGER NOT NULL UNIQUE,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                customer_id TEXT,
                priority TEXT NOT NULL,
                status TEXT NOT NULL,
                owner_department TEXT NOT NULL,
                workflow_type TEXT,
                category TEXT,
                risk_level TEXT,
                approval_chain_json TEXT NOT NULL DEFAULT '[]',
                auto_actions_json TEXT NOT NULL DEFAULT '[]',
                blocked_actions_json TEXT NOT NULL DEFAULT '[]',
                evidence_json TEXT NOT NULL DEFAULT '[]',
                agent_run_id TEXT,
                approval_id TEXT,
                idempotency_key TEXT,
                source TEXT NOT NULL DEFAULT 'agent',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_events (
                id TEXT PRIMARY KEY,
                ticket_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                body TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (ticket_id) REFERENCES tickets(id) ON DELETE CASCADE
            )
            """
        )
        _ensure_column(conn, "tickets", "workflow_type", "TEXT")
        _ensure_column(conn, "tickets", "category", "TEXT")
        _ensure_column(conn, "tickets", "risk_level", "TEXT")
        _ensure_column(conn, "tickets", "approval_chain_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "tickets", "auto_actions_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "tickets", "blocked_actions_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "tickets", "evidence_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "tickets", "agent_run_id", "TEXT")
        _ensure_column(conn, "tickets", "approval_id", "TEXT")
        _ensure_column(conn, "tickets", "idempotency_key", "TEXT")
        _ensure_column(conn, "tickets", "source", "TEXT NOT NULL DEFAULT 'agent'")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_external_tickets_idempotency_key
            ON tickets(idempotency_key)
            WHERE idempotency_key IS NOT NULL
            """
        )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_ticket_row(ticket_id: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    return dict(row) if row else None


def list_ticket_rows(limit: int, filters: TicketFilters | None = None) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    filters = filters or TicketFilters()
    if filters.status:
        where.append("lower(status) = lower(?)")
        params.append(filters.status.strip())
    if filters.owner:
        where.append("lower(owner_department) LIKE lower(?)")
        params.append(f"%{filters.owner.strip()}%")
    if filters.priority:
        where.append("lower(priority) = lower(?)")
        params.append(filters.priority.strip())
    if filters.workflow_type:
        where.append("lower(workflow_type) LIKE lower(?)")
        params.append(f"%{filters.workflow_type.strip()}%")
    if filters.q:
        where.append("(lower(title) LIKE lower(?) OR lower(description) LIKE lower(?) OR lower(customer_id) LIKE lower(?))")
        needle = f"%{filters.q.strip()}%"
        params.extend([needle, needle, needle])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM tickets
            {where_sql}
            ORDER BY sequence DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def list_ticket_events(ticket_id: str, limit: int = 100) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM ticket_events
            WHERE ticket_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (ticket_id, max(1, min(limit, 500))),
        ).fetchall()
    events = [dict(row) for row in rows]
    for event in events:
        event["payload"] = json_loads(event.pop("payload_json"), {})
    return events


def insert_event(
    conn: sqlite3.Connection,
    ticket_id: str,
    event_type: str,
    actor: str,
    body: str,
    *,
    from_status: str | None = None,
    to_status: str | None = None,
    payload: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    event_id = f"ev_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}_{abs(hash((ticket_id, body))) % 10000:04d}"
    created = created_at or utc_now()
    conn.execute(
        """
        INSERT INTO ticket_events
        (id, ticket_id, event_type, actor, body, from_status, to_status, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (event_id, ticket_id, event_type, actor, body, from_status, to_status, json_dumps(payload or {}), created),
    )
    return {
        "id": event_id,
        "ticket_id": ticket_id,
        "event_type": event_type,
        "actor": actor,
        "body": body,
        "from_status": from_status,
        "to_status": to_status,
        "payload": payload or {},
        "created_at": created,
    }


def ticket_response(ticket: dict[str, Any], *, include_events: bool = False) -> dict[str, Any]:
    url = f"{PUBLIC_BASE_URL}/tickets/{ticket['id']}"
    api_url = f"{PUBLIC_BASE_URL}/api/tickets/{ticket['id']}"
    response = {
        **ticket,
        "key": ticket["id"],
        "ticket_id": ticket["id"],
        "url": url,
        "html_url": url,
        "self": api_url,
        "source": "local_external_ticket_service",
        "approval_chain": _json_list(ticket.get("approval_chain_json")),
        "auto_actions": _json_list(ticket.get("auto_actions_json")),
        "blocked_actions": _json_list(ticket.get("blocked_actions_json")),
        "evidence": _json_list(ticket.get("evidence_json")),
    }
    for key in ("approval_chain_json", "auto_actions_json", "blocked_actions_json", "evidence_json"):
        response.pop(key, None)
    if include_events:
        response["events"] = list_ticket_events(ticket["id"], limit=200)
    return response


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return [] if default is None else default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return [] if default is None else default


def _json_list(value: str | None) -> list[Any]:
    decoded = json_loads(value, [])
    return decoded if isinstance(decoded, list) else []


def _normalize_status(status: str) -> str:
    value = status.strip().lower()
    if value not in STATUSES:
        return value
    return value


def _ticket_row(ticket: dict[str, Any]) -> str:
    sla = _sla_state(ticket)
    return f"""
    <tr>
      <td><code>{_escape(ticket["id"])}</code></td>
      <td>{_escape(ticket["title"])}</td>
      <td>{_escape(ticket["workflow_type"] or "-")}</td>
      <td>{_escape(ticket["risk_level"] or "-")}</td>
      <td>{_escape(ticket["priority"])}</td>
      <td>{_status_pill(ticket["status"])}</td>
      <td><span class="sla {sla["class"]}">{_escape(sla["label"])}</span></td>
      <td>{_escape(ticket["owner_department"])}</td>
      <td>{_escape(ticket["updated_at"])}</td>
      <td><a href="/tickets/{_escape(ticket["id"])}">Open</a></td>
    </tr>
    """


def _filter_input(name: str, label: str, value: str) -> str:
    return f"""
    <label>
      <span>{_escape(label)}</span>
      <input name="{_escape(name)}" value="{_escape(value)}" />
    </label>
    """


def _list_block(title: str, items: list[Any]) -> str:
    if not items:
        return f"<div class=\"list-block\"><h3>{_escape(title)}</h3><p class=\"muted\">-</p></div>"
    rows = "".join(f"<li>{_escape(item)}</li>" for item in items)
    return f"<div class=\"list-block\"><h3>{_escape(title)}</h3><ul>{rows}</ul></div>"


def _ticket_actions_form(ticket: dict[str, Any]) -> str:
    statuses = ["open", "investigating", "waiting_approval", "approved", "rejected", "waiting_customer", "resolved", "closed"]
    priorities = ["low", "normal", "high", "urgent"]
    owners = ["Customer Success", "Security", "IT Access", "SRE", "Procurement", "People Ops", "Finance", "Operations", "Business Ops"]
    return f"""
    <form class="ops-form" onsubmit="return updateTicket(event, '{_escape(ticket["id"])}')">
      <label><span>Status</span>{_select("status", statuses, ticket["status"])}</label>
      <label><span>Priority</span>{_select("priority", priorities, ticket["priority"])}</label>
      <label><span>Owner</span>{_select("owner_department", owners, ticket["owner_department"])}</label>
      <label class="span-all"><span>Internal note</span><input name="comment" placeholder="What changed and why" /></label>
      <button class="button" type="submit">Update Ticket</button>
    </form>
    <p class="muted ops-hint">Every update is recorded in the timeline and returned through the JSON API.</p>
    """


def _select(name: str, options: list[str], selected: str | None) -> str:
    rows = "".join(
        f"<option value=\"{_escape(option)}\" {'selected' if option == selected else ''}>{_escape(option)}</option>"
        for option in options
    )
    return f"<select name=\"{_escape(name)}\">{rows}</select>"


def _ticket_stats(tickets: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(tickets),
        "waiting_approval": sum(1 for ticket in tickets if ticket.get("status") == "waiting_approval"),
        "high_priority": sum(1 for ticket in tickets if ticket.get("priority") in {"high", "urgent"}),
        "sla_watch": sum(1 for ticket in tickets if _sla_state(ticket)["class"] in {"warn", "danger"}),
    }


def _sla_state(ticket: dict[str, Any]) -> dict[str, str]:
    priority = (ticket.get("priority") or "normal").lower()
    status = (ticket.get("status") or "open").lower()
    if status in {"approved", "resolved", "closed", "rejected"}:
        return {"label": "done", "class": "ok"}
    if priority in {"urgent", "high"} or ticket.get("risk_level") == "high":
        return {"label": "4h watch", "class": "danger" if status == "open" else "warn"}
    if status == "waiting_approval":
        return {"label": "approval", "class": "warn"}
    return {"label": "normal", "class": "ok"}


def _evidence_html(items: list[Any]) -> str:
    if not items:
        return "<p class=\"muted\">No evidence attached.</p>"
    rows = []
    for item in items[:10]:
        if isinstance(item, dict):
            title = item.get("title") or item.get("document_name") or "Evidence"
            snippet = item.get("snippet") or item.get("evidence_snippet") or item.get("content") or ""
            source = item.get("source") or item.get("category") or "policy"
            rows.append(f"<article class=\"evidence\"><strong>{_escape(source)} · {_escape(title)}</strong><p>{_escape(snippet)}</p></article>")
        else:
            rows.append(f"<article class=\"evidence\"><p>{_escape(item)}</p></article>")
    return "".join(rows)


def _event_row(event: dict[str, Any]) -> str:
    transition = ""
    if event.get("from_status") or event.get("to_status"):
        transition = f"<span>{_escape(event.get('from_status') or '-')} -> {_escape(event.get('to_status') or '-')}</span>"
    return f"""
    <article class="timeline-item">
      <div>
        <strong>{_escape(event["event_type"])}</strong>
        {transition}
      </div>
      <p>{_escape(event["body"])}</p>
      <small>{_escape(event["actor"])} · {_escape(event["created_at"])}</small>
    </article>
    """


def _status_pill(status: str) -> str:
    css = "status-" + "".join(ch if ch.isalnum() else "-" for ch in status.lower())
    return f'<span class="pill {css}">{_escape(status)}</span>'


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{_escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #12243a;
      --muted: #5d6b7e;
      --line: #d7e1ec;
      --surface: #ffffff;
      --surface-soft: #f8fafc;
      --page: #f4f7fb;
      --accent: #00786f;
      --accent-soft: #e4f5f2;
      --blue: #2563eb;
      --blue-soft: #eff6ff;
      --ok: #027a48;
      --ok-soft: #ecfdf3;
      --warn: #b54708;
      --warn-soft: #fffaeb;
      --danger: #b42318;
      --danger-soft: #fef3f2;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: var(--page);
      font-size: 14px;
      line-height: 1.5;
    }}
    main {{ width: min(1380px, calc(100% - 32px)); margin: 0 auto; padding: 24px 0 40px; }}
    .hero {{
      min-height: 152px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 14px;
      padding: 22px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: 0 8px 24px rgba(18, 36, 58, 0.06);
    }}
    .hero.compact {{ min-height: 116px; }}
    .hero-actions {{ display: flex; gap: 9px; flex-wrap: wrap; justify-content: flex-end; }}
    .eyebrow {{ margin: 0 0 6px; font-size: 12px; font-weight: 800; color: var(--accent); text-transform: uppercase; letter-spacing: 0; }}
    h1, h2, h3, p {{ margin-top: 0; }}
    h1 {{ margin-bottom: 8px; font-size: 30px; line-height: 1.12; }}
    h2 {{ margin-bottom: 0; font-size: 17px; }}
    h3 {{ margin-bottom: 8px; font-size: 13px; }}
    p {{ color: var(--muted); }}
    code {{ padding: 2px 5px; border-radius: 5px; background: #eef3f8; color: #17324f; }}
    .button {{
      min-height: 38px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0 13px;
      border: 1px solid var(--accent);
      border-radius: 7px;
      background: var(--accent);
      color: #fff;
      text-decoration: none;
      font-weight: 750;
      white-space: nowrap;
      cursor: pointer;
    }}
    .button.secondary {{ color: var(--accent); background: var(--accent-soft); }}
    .button.danger {{ border-color: var(--danger); background: var(--danger); }}
    .panel {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: 0 6px 18px rgba(18, 36, 58, 0.05);
      overflow: hidden;
    }}
    .padded {{ padding: 16px; }}
    .panel-head, .panel-title-row {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; }}
    .panel-head {{ padding: 15px 17px; border-bottom: 1px solid var(--line); }}
    .panel-head span, .panel-title-row span, .muted, dd, dt {{ color: var(--muted); }}
    .filters {{ margin-bottom: 14px; padding: 14px; }}
    .filters form {{ display: grid; grid-template-columns: 1.2fr repeat(4, minmax(130px, 1fr)) auto auto; gap: 10px; align-items: end; }}
    .stats-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 14px; }}
    .stat {{ border: 1px solid var(--line); border-radius: 8px; background: var(--surface); padding: 14px; box-shadow: 0 6px 18px rgba(18, 36, 58, 0.05); }}
    .stat strong {{ display: block; font-size: 24px; line-height: 1.1; }}
    .stat span {{ color: var(--muted); font-size: 12px; }}
    label {{ display: grid; gap: 5px; color: var(--muted); font-size: 12px; font-weight: 700; }}
    input, select {{ min-height: 38px; width: 100%; border: 1px solid var(--line); border-radius: 7px; padding: 8px 10px; font: inherit; background: #fff; color: var(--ink); }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 11px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-size: 12px; background: #fbfdff; }}
    td a {{ color: var(--accent); font-weight: 750; text-decoration: none; }}
    .pill {{
      min-height: 24px;
      display: inline-flex;
      align-items: center;
      padding: 0 9px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #fff;
      color: var(--muted);
      font-weight: 750;
      font-size: 12px;
      white-space: nowrap;
    }}
    .status-approved, .status-resolved, .status-closed {{ border-color: #abefc6; color: var(--ok); background: var(--ok-soft); }}
    .status-waiting-approval, .status-waiting-customer, .status-investigating {{ border-color: #fedf89; color: var(--warn); background: var(--warn-soft); }}
    .status-rejected {{ border-color: #fecdca; color: var(--danger); background: var(--danger-soft); }}
    .status-open {{ border-color: #bfdbfe; color: var(--blue); background: var(--blue-soft); }}
    .sla {{ display: inline-flex; align-items: center; min-height: 24px; border-radius: 999px; padding: 0 9px; font-size: 12px; font-weight: 750; border: 1px solid var(--line); }}
    .sla.ok {{ color: var(--ok); background: var(--ok-soft); border-color: #abefc6; }}
    .sla.warn {{ color: var(--warn); background: var(--warn-soft); border-color: #fedf89; }}
    .sla.danger {{ color: var(--danger); background: var(--danger-soft); border-color: #fecdca; }}
    .empty {{ height: 150px; color: var(--muted); text-align: center; vertical-align: middle; }}
    .detail-grid {{ display: grid; grid-template-columns: minmax(280px, 0.85fr) minmax(0, 1.15fr); gap: 14px; }}
    .span-2 {{ grid-column: span 2; }}
    dl {{ display: grid; grid-template-columns: 120px minmax(0, 1fr); gap: 10px; margin: 14px 0 0; }}
    dt {{ font-weight: 750; }}
    dd {{ margin: 0; overflow-wrap: anywhere; }}
    pre {{
      max-height: 420px;
      overflow: auto;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      line-height: 1.55;
      margin: 14px 0 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--surface-soft);
      color: var(--ink);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
    }}
    .list-block {{ margin-top: 14px; }}
    ul {{ margin: 0; padding-left: 18px; color: #334155; }}
    li {{ margin: 4px 0; }}
    .evidence {{ border-top: 1px solid var(--line); padding-top: 10px; margin-top: 10px; }}
    .evidence strong {{ display: block; margin-bottom: 4px; }}
    .evidence p {{ margin: 0; }}
    .comment-form {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 9px; margin: 14px 0; }}
    .ops-form {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 14px; }}
    .ops-form .span-all, .ops-form .button {{ grid-column: 1 / -1; }}
    .ops-hint {{ margin-top: 10px; font-size: 12px; }}
    .timeline {{ display: grid; gap: 10px; }}
    .timeline-item {{ border: 1px solid var(--line); border-radius: 8px; background: var(--surface-soft); padding: 11px; }}
    .timeline-item div {{ display: flex; justify-content: space-between; gap: 10px; }}
    .timeline-item p {{ margin: 7px 0; color: #334155; overflow-wrap: anywhere; }}
    .timeline-item small {{ color: var(--muted); }}
    @media (max-width: 980px) {{
      .filters form, .detail-grid, .stats-grid, .ops-form {{ grid-template-columns: 1fr; }}
      .span-2 {{ grid-column: auto; }}
    }}
    @media (max-width: 760px) {{
      main {{ width: min(100% - 20px, 1380px); padding-top: 12px; }}
      .hero {{ align-items: flex-start; flex-direction: column; padding: 16px; }}
      .hero-actions {{ justify-content: flex-start; }}
      h1 {{ font-size: 26px; }}
      .panel {{ overflow-x: auto; }}
    }}
  </style>
  <script>
    async function clearTickets() {{
      if (!window.confirm("Clear all ticket demo data?")) return;
      await fetch("/api/admin/clear", {{ method: "POST" }});
      window.location.reload();
    }}
    async function addComment(event, ticketId) {{
      event.preventDefault();
      const input = event.currentTarget.elements.body;
      const body = input.value.trim();
      if (!body) return false;
      await fetch(`/api/tickets/${{ticketId}}/comments`, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ body, actor: "ticket-desk" }})
      }});
      input.value = "";
      window.location.reload();
      return false;
    }}
    async function updateTicket(event, ticketId) {{
      event.preventDefault();
      const form = event.currentTarget;
      const payload = {{
        status: form.elements.status.value,
        priority: form.elements.priority.value,
        owner_department: form.elements.owner_department.value,
        comment: form.elements.comment.value.trim() || "Ticket updated from ticket desk.",
        actor: "ticket-desk"
      }};
      await fetch(`/api/tickets/${{ticketId}}`, {{
        method: "PATCH",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(payload)
      }});
      window.location.reload();
      return false;
    }}
    window.setInterval(() => {{
      if (document.visibilityState === "visible" && !document.querySelector("input:focus")) {{
        window.location.reload();
      }}
    }}, 8000);
  </script>
</head>
<body>
  <main>{body}</main>
</body>
</html>"""


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)
