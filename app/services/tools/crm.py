from __future__ import annotations

import re
from typing import Any

from app.db import get_connection, row_to_dict, rows_to_dicts
from app.services.audit import record_audit
from app.services.tenancy import effective_tenant_id
from app.utils import json_dumps, json_loads, new_id, utc_now


EMAIL_RE = re.compile(r"[\w.\-+]+@[\w.\-]+\.\w+")
CUSTOMER_STATUSES = {"active", "onboarding", "at_risk", "inactive", "churned"}
CUSTOMER_TIERS = {"starter", "growth", "enterprise", "strategic"}
INTERACTION_TYPES = {"note", "email", "call", "meeting", "ticket", "health_update"}


def create_customer(
    name: str,
    email: str,
    *,
    tier: str = "starter",
    status: str = "active",
    phone: str | None = None,
    health_score: int = 100,
    owner_department: str = "Customer Success",
    owner_user_id: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    notes: str = "",
    tenant_id: str | None = None,
    actor: str = "crm",
) -> dict:
    tenant = effective_tenant_id(tenant_id)
    normalized_email = _normalize_email(email)
    normalized_tier = _normalize_choice(tier, CUSTOMER_TIERS, "tier")
    normalized_status = _normalize_choice(status, CUSTOMER_STATUSES, "status")
    score = _normalize_health_score(health_score)
    customer_id = new_id("cust")
    now = utc_now()
    with get_connection() as conn:
        duplicate = conn.execute(
            "SELECT id FROM customers WHERE tenant_id = ? AND lower(email) = lower(?) LIMIT 1",
            (tenant, normalized_email),
        ).fetchone()
        if duplicate:
            raise ValueError("A customer with this email already exists in the tenant.")
        conn.execute(
            """
            INSERT INTO customers
            (id, name, tenant_id, tier, status, email, phone, health_score, owner_department,
             owner_user_id, tags_json, metadata_json, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                customer_id,
                _required_text(name, "name", 200),
                tenant,
                normalized_tier,
                normalized_status,
                normalized_email,
                _optional_text(phone, 80),
                score,
                _required_text(owner_department, "owner_department", 120),
                _optional_text(owner_user_id, 120),
                json_dumps(_normalize_tags(tags)),
                json_dumps(metadata or {}),
                str(notes or "").strip(),
                now,
                now,
            ),
        )
    record_audit(
        "customer.create",
        "customer",
        customer_id,
        {"email": normalized_email, "tier": normalized_tier, "status": normalized_status},
        actor=actor,
        tenant_id=tenant,
    )
    return get_customer(customer_id, tenant_id=tenant) or {}


def get_customer(customer_id: str, *, tenant_id: str | None = None) -> dict | None:
    tenant = effective_tenant_id(tenant_id)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM customers WHERE id = ? AND tenant_id = ? LIMIT 1",
            (customer_id, tenant),
        ).fetchone()
    return _hydrate_customer(row_to_dict(row))


def update_customer(
    customer_id: str,
    *,
    name: str | None = None,
    email: str | None = None,
    tier: str | None = None,
    status: str | None = None,
    phone: str | None = None,
    health_score: int | None = None,
    owner_department: str | None = None,
    owner_user_id: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    notes: str | None = None,
    tenant_id: str | None = None,
    actor: str = "crm",
) -> dict | None:
    tenant = effective_tenant_id(tenant_id)
    current = get_customer(customer_id, tenant_id=tenant)
    if not current:
        return None

    updates: dict[str, Any] = {}
    if name is not None:
        updates["name"] = _required_text(name, "name", 200)
    if email is not None:
        normalized_email = _normalize_email(email)
        with get_connection() as conn:
            duplicate = conn.execute(
                "SELECT id FROM customers WHERE tenant_id = ? AND lower(email) = lower(?) AND id <> ? LIMIT 1",
                (tenant, normalized_email, customer_id),
            ).fetchone()
        if duplicate:
            raise ValueError("A customer with this email already exists in the tenant.")
        updates["email"] = normalized_email
    if tier is not None:
        updates["tier"] = _normalize_choice(tier, CUSTOMER_TIERS, "tier")
    if status is not None:
        updates["status"] = _normalize_choice(status, CUSTOMER_STATUSES, "status")
    if phone is not None:
        updates["phone"] = _optional_text(phone, 80)
    if health_score is not None:
        updates["health_score"] = _normalize_health_score(health_score)
    if owner_department is not None:
        updates["owner_department"] = _required_text(owner_department, "owner_department", 120)
    if owner_user_id is not None:
        updates["owner_user_id"] = _optional_text(owner_user_id, 120)
    if tags is not None:
        updates["tags_json"] = json_dumps(_normalize_tags(tags))
    if metadata is not None:
        updates["metadata_json"] = json_dumps(metadata)
    if notes is not None:
        updates["notes"] = str(notes).strip()
    if not updates:
        return current

    updates["updated_at"] = utc_now()
    assignments = ", ".join(f"{column} = ?" for column in updates)
    with get_connection() as conn:
        conn.execute(
            f"UPDATE customers SET {assignments} WHERE id = ? AND tenant_id = ?",
            (*updates.values(), customer_id, tenant),
        )
    record_audit(
        "customer.update",
        "customer",
        customer_id,
        {"changed_fields": sorted(column.removesuffix("_json") for column in updates if column != "updated_at")},
        actor=actor,
        tenant_id=tenant,
    )
    return get_customer(customer_id, tenant_id=tenant)


def query_customers(
    *,
    q: str | None = None,
    status: str | None = None,
    tier: str | None = None,
    owner_department: str | None = None,
    tenant_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    tenant = effective_tenant_id(tenant_id)
    clauses = ["tenant_id = ?"]
    params: list[Any] = [tenant]
    if q:
        needle = f"%{q.strip().lower()}%"
        clauses.append("(lower(name) LIKE ? OR lower(email) LIKE ? OR lower(COALESCE(phone, '')) LIKE ? OR lower(notes) LIKE ? OR lower(tags_json) LIKE ?)")
        params.extend([needle, needle, needle, needle, needle])
    if status:
        clauses.append("lower(status) = ?")
        params.append(_normalize_choice(status, CUSTOMER_STATUSES, "status"))
    if tier:
        clauses.append("lower(tier) = ?")
        params.append(_normalize_choice(tier, CUSTOMER_TIERS, "tier"))
    if owner_department:
        clauses.append("lower(owner_department) = lower(?)")
        params.append(owner_department.strip())
    safe_limit = max(1, min(int(limit), 500))
    safe_offset = max(0, int(offset))
    where = " AND ".join(clauses)
    with get_connection() as conn:
        count_row = conn.execute(f"SELECT COUNT(*) AS count FROM customers WHERE {where}", params).fetchone()
        rows = conn.execute(
            f"""
            SELECT * FROM customers
            WHERE {where}
            ORDER BY updated_at DESC, name ASC
            LIMIT ? OFFSET ?
            """,
            (*params, safe_limit, safe_offset),
        ).fetchall()
    return {
        "customers": [_hydrate_customer(row) for row in rows_to_dicts(rows)],
        "count": int((row_to_dict(count_row) or {}).get("count", 0)),
        "limit": safe_limit,
        "offset": safe_offset,
    }


def list_customers(
    limit: int = 100,
    *,
    tenant_id: str | None = None,
    q: str | None = None,
    status: str | None = None,
    tier: str | None = None,
    owner_department: str | None = None,
    offset: int = 0,
) -> list[dict]:
    return query_customers(
        q=q,
        status=status,
        tier=tier,
        owner_department=owner_department,
        tenant_id=tenant_id,
        limit=limit,
        offset=offset,
    )["customers"]


def lookup_customer(query: str, tenant_id: str | None = None) -> dict:
    tenant = effective_tenant_id(tenant_id)
    email_match = EMAIL_RE.search(query or "")
    with get_connection() as conn:
        if email_match:
            row = conn.execute(
                "SELECT * FROM customers WHERE tenant_id = ? AND lower(email) = lower(?) LIMIT 1",
                (tenant, email_match.group(0)),
            ).fetchone()
            return {"customer": _hydrate_customer(row_to_dict(row)), "matched_by": "email" if row else None}

        rows = conn.execute("SELECT * FROM customers WHERE tenant_id = ?", (tenant,)).fetchall()
    lowered = str(query or "").lower()
    for row in rows:
        customer = _hydrate_customer(row_to_dict(row)) or {}
        if str(customer.get("name", "")).lower() in lowered or str(customer.get("email", "")).lower() in lowered:
            return {"customer": customer, "matched_by": "name"}
    return {"customer": None, "matched_by": None}


def add_customer_interaction(
    customer_id: str,
    summary: str,
    *,
    interaction_type: str = "note",
    channel: str = "internal",
    detail: dict[str, Any] | None = None,
    actor: str = "crm",
    tenant_id: str | None = None,
) -> dict:
    tenant = effective_tenant_id(tenant_id)
    customer = get_customer(customer_id, tenant_id=tenant)
    if not customer:
        raise ValueError("Customer not found in the tenant.")
    normalized_type = _normalize_choice(interaction_type, INTERACTION_TYPES, "interaction_type")
    interaction_id = new_id("crm_event")
    created_at = utc_now()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO customer_interactions
            (id, customer_id, tenant_id, interaction_type, channel, summary, detail_json, actor, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interaction_id,
                customer_id,
                tenant,
                normalized_type,
                _required_text(channel, "channel", 80),
                _required_text(summary, "summary", 1000),
                json_dumps(detail or {}),
                _required_text(actor, "actor", 120),
                created_at,
            ),
        )
        conn.execute("UPDATE customers SET updated_at = ? WHERE id = ? AND tenant_id = ?", (created_at, customer_id, tenant))
    record_audit(
        "customer.interaction.create",
        "customer",
        customer_id,
        {"interaction_id": interaction_id, "interaction_type": normalized_type, "channel": channel},
        actor=actor,
        tenant_id=tenant,
    )
    return get_customer_interaction(interaction_id, tenant_id=tenant) or {}


def get_customer_interaction(interaction_id: str, *, tenant_id: str | None = None) -> dict | None:
    tenant = effective_tenant_id(tenant_id)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM customer_interactions WHERE id = ? AND tenant_id = ? LIMIT 1",
            (interaction_id, tenant),
        ).fetchone()
    return _hydrate_interaction(row_to_dict(row))


def list_customer_interactions(
    customer_id: str,
    *,
    tenant_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    tenant = effective_tenant_id(tenant_id)
    if not get_customer(customer_id, tenant_id=tenant):
        return []
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM customer_interactions
            WHERE customer_id = ? AND tenant_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (customer_id, tenant, max(1, min(int(limit), 500)), max(0, int(offset))),
        ).fetchall()
    return [_hydrate_interaction(row) for row in rows_to_dicts(rows)]


def _hydrate_customer(customer: dict | None) -> dict | None:
    if not customer:
        return None
    hydrated = dict(customer)
    hydrated["tags"] = json_loads(hydrated.pop("tags_json", "[]"), [])
    hydrated["metadata"] = json_loads(hydrated.pop("metadata_json", "{}"), {})
    return hydrated


def _hydrate_interaction(interaction: dict | None) -> dict | None:
    if not interaction:
        return None
    hydrated = dict(interaction)
    hydrated["detail"] = json_loads(hydrated.pop("detail_json", "{}"), {})
    return hydrated


def _normalize_email(value: str) -> str:
    email = str(value or "").strip().lower()
    if not EMAIL_RE.fullmatch(email):
        raise ValueError("A valid customer email is required.")
    return email


def _normalize_choice(value: str, choices: set[str], field: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in choices:
        raise ValueError(f"Unsupported {field}: {value!r}. Allowed values: {', '.join(sorted(choices))}.")
    return normalized


def _normalize_health_score(value: int) -> int:
    score = int(value)
    if not 0 <= score <= 100:
        raise ValueError("health_score must be between 0 and 100.")
    return score


def _normalize_tags(tags: list[str] | None) -> list[str]:
    return list(dict.fromkeys(str(tag).strip() for tag in (tags or []) if str(tag).strip()))[:50]


def _required_text(value: str, field: str, max_length: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required.")
    if len(text) > max_length:
        raise ValueError(f"{field} must not exceed {max_length} characters.")
    return text


def _optional_text(value: str | None, max_length: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if len(text) > max_length:
        raise ValueError(f"Value must not exceed {max_length} characters.")
    return text or None
