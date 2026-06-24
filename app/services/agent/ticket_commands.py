from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from app.services.tools.ticketing import find_ticket, query_tickets, update_ticket
from app.utils import compact_text


TICKET_REF_RE = re.compile(r"\b(?:ticket_[a-z0-9]+|EXT-\d+)\b", re.IGNORECASE)

QUERY_TERMS = [
    "query",
    "search",
    "find",
    "show",
    "list",
    "\u67e5\u8be2",
    "\u67e5\u627e",
    "\u67e5\u770b",
    "\u641c\u7d22",
]
UPDATE_TERMS = [
    "update",
    "change",
    "set",
    "assign",
    "close",
    "resolve",
    "comment",
    "note",
    "\u4fee\u6539",
    "\u66f4\u65b0",
    "\u6539\u6210",
    "\u8c03\u6574",
    "\u6307\u6d3e",
    "\u5206\u6d3e",
    "\u5173\u95ed",
    "\u89e3\u51b3",
    "\u5907\u6ce8",
    "\u8bf4\u660e",
]
CREATE_TERMS = ["create", "new ticket", "\u521b\u5efa", "\u65b0\u5efa", "\u767b\u8bb0"]
LATEST_TERMS = ["latest", "recent", "last", "\u6700\u8fd1", "\u6700\u65b0", "\u4e0a\u4e00\u4e2a"]
ASSIGN_TERMS = ["assign", "owner", "route to", "transfer to", "\u6307\u6d3e", "\u5206\u6d3e", "\u8f6c\u7ed9", "\u4ea4\u7ed9"]

OWNER_KEYWORDS = [
    ("Customer Success", ["customer success", "cs", "\u5ba2\u670d", "\u5ba2\u6237\u6210\u529f", "\u5ba2\u6237"]),
    ("Security", ["security", "\u5b89\u5168"]),
    ("IT Access", ["it access", "access", "\u6743\u9650", "\u8bbf\u95ee"]),
    ("SRE", ["sre", "incident", "\u8fd0\u7ef4", "\u503c\u73ed", "\u6545\u969c"]),
    ("Procurement", ["procurement", "purchase", "\u91c7\u8d2d"]),
    ("People Ops", ["people ops", "hr", "\u4eba\u4e8b", "\u8fdc\u7a0b\u529e\u516c"]),
    ("Finance", ["finance", "\u8d22\u52a1"]),
    ("Operations", ["operations", "\u8fd0\u8425"]),
    ("Business Ops", ["business ops", "\u4e1a\u52a1"]),
]


@dataclass(frozen=True)
class TicketCommand:
    intent: str
    ticket_ref: str | None = None
    status: str | None = None
    priority: str | None = None
    owner_department: str | None = None
    change_owner: bool = False
    comment: str | None = None
    q: str | None = None
    latest: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_ticket_command(objective: str) -> TicketCommand | None:
    text = " ".join(str(objective or "").split())
    if not text:
        return None
    lowered = text.lower()
    ticket_ref = _ticket_ref(text)
    mentions_ticket = bool(ticket_ref) or "ticket" in lowered or "\u5de5\u5355" in text
    if not mentions_ticket:
        return None

    wants_update = _has_any(text, lowered, UPDATE_TERMS)
    wants_query = _has_any(text, lowered, QUERY_TERMS)
    wants_create = _has_any(text, lowered, CREATE_TERMS)
    if wants_create and not wants_update and not _explicit_ticket_query(text, lowered):
        return None
    if not wants_update and not wants_query and not ticket_ref:
        return None

    command = TicketCommand(
        intent="update" if wants_update else "query",
        ticket_ref=ticket_ref,
        status=_parse_status(text, lowered),
        priority=_parse_priority(text, lowered),
        owner_department=_parse_owner(text, lowered),
        change_owner=_has_any(text, lowered, ASSIGN_TERMS),
        comment=_parse_comment(text),
        q=_parse_free_text_query(text, lowered, ticket_ref),
        latest=_has_any(text, lowered, LATEST_TERMS),
    )
    if command.intent == "update" and not any([command.status, command.priority, command.owner_department, command.comment]):
        return None
    return command


def execute_ticket_query(
    command: TicketCommand,
    *,
    requester_role: str | None,
    requester_department: str | None,
    tenant_id: str | None = None,
) -> dict:
    raw = query_tickets(
        ticket_ref=command.ticket_ref,
        status=command.status,
        priority=command.priority,
        owner_department=command.owner_department,
        q=command.q,
        tenant_id=tenant_id,
        limit=30,
    )
    visible = [ticket for ticket in raw["tickets"] if _can_view(ticket, requester_role, requester_department)]
    message = _query_message(visible, len(raw["tickets"]), requester_role, requester_department)
    return {
        "intent": "query",
        "ok": True,
        "tickets": visible,
        "count": len(visible),
        "total_matched": len(raw["tickets"]),
        "filters": raw.get("filters", {}),
        "message": message,
    }


def execute_ticket_update(
    command: TicketCommand,
    *,
    requester_user_id: str | None,
    requester_role: str | None,
    requester_department: str | None,
    tenant_id: str | None = None,
) -> dict:
    ticket = _resolve_update_target(command, requester_role, requester_department, tenant_id=tenant_id)
    if not ticket:
        return {
            "intent": "update",
            "ok": False,
            "error_code": "ticket_not_found",
            "message": "\u6ca1\u6709\u627e\u5230\u7b26\u5408\u6761\u4ef6\u7684\u5de5\u5355\uff0c\u8bf7\u6307\u5b9a\u5de5\u5355\u53f7\uff08\u4f8b\u5982 EXT-000001\uff09\u6216\u66f4\u660e\u786e\u7684\u6761\u4ef6\u3002",
        }
    if not _can_view(ticket, requester_role, requester_department):
        return _permission_denied(ticket, requester_role, requester_department)

    field_changes = {
        "status": command.status,
        "priority": command.priority,
        "owner_department": command.owner_department if command.change_owner else None,
    }
    field_changes = {key: value for key, value in field_changes.items() if value}
    if not _can_change_fields(field_changes, requester_role, ticket, requester_department):
        return _permission_denied(ticket, requester_role, requester_department)

    actor = requester_user_id or "agent"
    comment = command.comment
    if not comment:
        comment = f"Natural-language update by {actor}: {compact_text(command.q or ticket['title'], 160)}"
    updated = update_ticket(ticket["id"], actor=actor, comment=comment, **field_changes)
    if not updated:
        return {
            "intent": "update",
            "ok": False,
            "error_code": "ticket_not_found",
            "message": "\u5de5\u5355\u5728\u66f4\u65b0\u65f6\u6ca1\u6709\u627e\u5230\uff0c\u672a\u505a\u4efb\u4f55\u4fee\u6539\u3002",
        }
    return {
        "intent": "update",
        "ok": True,
        "ticket": updated,
        "tickets": [updated],
        "changes": {**field_changes, "comment": comment},
        "message": _update_message(updated, field_changes, comment),
    }


def _resolve_update_target(
    command: TicketCommand,
    requester_role: str | None,
    requester_department: str | None,
    *,
    tenant_id: str | None = None,
) -> dict | None:
    if command.ticket_ref:
        ticket = find_ticket(command.ticket_ref)
        if tenant_id and ticket and ticket.get("tenant_id") != tenant_id:
            return None
        return ticket
    raw = query_tickets(
        status=None,
        priority=None,
        owner_department=command.owner_department,
        q=None if command.latest else command.q,
        tenant_id=tenant_id,
        limit=20,
    )
    visible = [ticket for ticket in raw["tickets"] if _can_view(ticket, requester_role, requester_department)]
    if not visible:
        return None
    if len(visible) == 1 or command.latest:
        return visible[0]
    return None


def _can_view(ticket: dict, role: str | None, department: str | None) -> bool:
    normalized_role = (role or "employee").lower()
    if normalized_role == "admin":
        return True
    owner = (ticket.get("owner_department") or "").lower()
    dept = (department or "").lower()
    return bool(dept and owner == dept)


def _can_change_fields(field_changes: dict[str, str], role: str | None, ticket: dict, department: str | None) -> bool:
    normalized_role = (role or "employee").lower()
    if normalized_role == "admin":
        return True
    return _can_view(ticket, role, department)


def _permission_denied(ticket: dict, role: str | None, department: str | None) -> dict:
    return {
        "intent": "update",
        "ok": False,
        "error_code": "permission_denied",
        "ticket": ticket,
        "message": (
            "\u6743\u9650\u4e0d\u8db3\uff1a\u5f53\u524d\u7528\u6237\u53ea\u80fd\u67e5\u770b\u6216\u4fee\u6539\u81ea\u5df1\u90e8\u95e8\u7684\u5de5\u5355\u3002"
            f" role={role or 'employee'}, department={department or '-'}"
        ),
    }


def _query_message(tickets: list[dict], total_matched: int, role: str | None, department: str | None) -> str:
    if not tickets and total_matched:
        return (
            "\u627e\u5230\u4e86\u5de5\u5355\uff0c\u4f46\u5df2\u6309\u6743\u9650\u8fc7\u6ee4\uff1a"
            f"\u5f53\u524d role={role or 'employee'}, department={department or '-'} \u65e0\u6cd5\u67e5\u770b\u3002"
        )
    if not tickets:
        return "\u672a\u627e\u5230\u7b26\u5408\u6761\u4ef6\u7684\u5de5\u5355\u3002"
    lines = [
        f"{ticket.get('external_id') or ticket['id']} {ticket.get('status')} {ticket.get('priority')} {ticket.get('owner_department')} - {compact_text(ticket.get('title'), 72)}"
        for ticket in tickets[:5]
    ]
    return f"\u627e\u5230 {len(tickets)} \u4e2a\u53ef\u89c1\u5de5\u5355\uff1a" + " | ".join(lines)


def _update_message(ticket: dict, field_changes: dict[str, str], comment: str) -> str:
    changes = ", ".join(f"{key}={value}" for key, value in field_changes.items()) or "\u4ec5\u8ffd\u52a0\u5907\u6ce8"
    ref = ticket.get("external_id") or ticket["id"]
    return f"\u5df2\u66f4\u65b0\u5de5\u5355 {ref}\uff1a{changes}\uff1b\u5907\u6ce8\uff1a{compact_text(comment, 120)}"


def _ticket_ref(text: str) -> str | None:
    match = TICKET_REF_RE.search(text)
    return match.group(0) if match else None


def _parse_status(text: str, lowered: str) -> str | None:
    status_terms = [
        ("waiting_approval", ["waiting_approval", "waiting approval", "\u7b49\u5f85\u5ba1\u6279"]),
        ("waiting_customer", ["waiting_customer", "waiting customer", "\u7b49\u5f85\u5ba2\u6237"]),
        ("investigating", ["investigating", "in progress", "\u8c03\u67e5", "\u5904\u7406\u4e2d"]),
        ("approved", ["approved", "\u5df2\u901a\u8fc7", "\u901a\u8fc7\u5ba1\u6279"]),
        ("rejected", ["rejected", "\u5df2\u62d2\u7edd", "\u62d2\u7edd"]),
        ("resolved", ["resolved", "done", "\u5df2\u89e3\u51b3", "\u89e3\u51b3", "\u5b8c\u6210"]),
        ("closed", ["closed", "close", "\u5df2\u5173\u95ed", "\u5173\u95ed", "\u5173\u5355"]),
        ("open", ["open", "\u6253\u5f00", "\u91cd\u65b0\u6253\u5f00"]),
    ]
    return _first_value(text, lowered, status_terms)


def _parse_priority(text: str, lowered: str) -> str | None:
    priority_terms = [
        ("urgent", ["urgent", "p0", "\u7d27\u6025", "\u6700\u9ad8"]),
        ("high", ["high", "p1", "\u9ad8\u4f18\u5148\u7ea7", "\u9ad8\u4f18", "\u9ad8"]),
        ("low", ["low", "\u4f4e\u4f18\u5148\u7ea7", "\u4f4e\u4f18", "\u4f4e"]),
        ("normal", ["normal", "medium", "\u666e\u901a", "\u4e2d\u7b49"]),
    ]
    return _first_value(text, lowered, priority_terms)


def _parse_owner(text: str, lowered: str) -> str | None:
    for owner, terms in OWNER_KEYWORDS:
        if _has_any(text, lowered, terms):
            return owner
    return None


def _parse_comment(text: str) -> str | None:
    patterns = [
        r"(?:comment|note)\s*[:：]\s*(.+)$",
        r"(?:\u5907\u6ce8|\u8bf4\u660e)\s*[:：]?\s*(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return compact_text(match.group(1), 500)
    return None


def _parse_free_text_query(text: str, lowered: str, ticket_ref: str | None) -> str | None:
    if ticket_ref:
        return None
    cleaned = text
    status_terms = [
        "waiting_approval",
        "waiting approval",
        "waiting_customer",
        "waiting customer",
        "investigating",
        "in progress",
        "approved",
        "rejected",
        "resolved",
        "closed",
        "close",
        "open",
        "\u7b49\u5f85\u5ba1\u6279",
        "\u7b49\u5f85\u5ba2\u6237",
        "\u8c03\u67e5",
        "\u5904\u7406\u4e2d",
        "\u5df2\u901a\u8fc7",
        "\u62d2\u7edd",
        "\u5df2\u89e3\u51b3",
        "\u89e3\u51b3",
        "\u5b8c\u6210",
        "\u5df2\u5173\u95ed",
        "\u5173\u95ed",
    ]
    priority_terms = ["urgent", "high", "normal", "medium", "low", "p0", "p1", "\u7d27\u6025", "\u9ad8", "\u666e\u901a", "\u4f4e"]
    owner_terms = [term for _, terms in OWNER_KEYWORDS for term in terms]
    filler_terms = ["department", "priority", "\u90e8\u95e8", "\u4f18\u5148\u7ea7", "\u628a", "\u5e76", "\u4e3a"]
    for term in QUERY_TERMS + UPDATE_TERMS + CREATE_TERMS + LATEST_TERMS + status_terms + priority_terms + owner_terms + filler_terms:
        cleaned = re.sub(re.escape(term), " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(?:ticket|tickets)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace("\u5de5\u5355", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return compact_text(cleaned, 120) if cleaned else None


def _first_value(text: str, lowered: str, mapping: list[tuple[str, list[str]]]) -> str | None:
    for value, terms in mapping:
        if _has_any(text, lowered, terms):
            return value
    return None


def _explicit_ticket_query(text: str, lowered: str) -> bool:
    return bool(re.search(r"(?:query|search|find|show|list)\s+(?:a\s+)?tickets?", lowered)) or any(
        term in text for term in ["\u67e5\u8be2\u5de5\u5355", "\u67e5\u770b\u5de5\u5355", "\u67e5\u627e\u5de5\u5355"]
    )


def _has_any(text: str, lowered: str, terms: list[str]) -> bool:
    for term in terms:
        if not term:
            continue
        if term.isascii():
            if term.lower() in lowered:
                return True
        elif term in text:
            return True
    return False
