from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from app.db import get_connection, row_to_dict, rows_to_dicts
from app.services.tenancy import effective_tenant_id
from app.utils import json_dumps, json_loads, new_id


def record_audit(
    event_type: str,
    target_type: str,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
    *,
    actor: str = "system",
    tenant_id: str | None = None,
) -> dict:
    audit_id = new_id("audit")
    created_at = _audit_now()
    detail_json = json_dumps(detail or {})
    tenant = effective_tenant_id(tenant_id)
    with get_connection() as conn:
        previous = conn.execute(
            """
            SELECT row_hash
            FROM audit_logs
            WHERE row_hash IS NOT NULL
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        previous_hash = (row_to_dict(previous) or {}).get("row_hash")
        row_hash = audit_row_hash(
            {
                "id": audit_id,
                "actor": actor,
                "event_type": event_type,
                "target_type": target_type,
                "target_id": target_id,
                "tenant_id": tenant,
                "detail_json": detail_json,
                "previous_hash": previous_hash,
                "created_at": created_at,
            }
        )
        conn.execute(
            """
            INSERT INTO audit_logs
            (id, actor, event_type, target_type, target_id, tenant_id, detail_json, previous_hash, row_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (audit_id, actor, event_type, target_type, target_id, tenant, detail_json, previous_hash, row_hash, created_at),
        )
    return {
        "id": audit_id,
        "actor": actor,
        "event_type": event_type,
        "target_type": target_type,
        "target_id": target_id,
        "detail": detail or {},
        "tenant_id": tenant,
        "previous_hash": previous_hash,
        "row_hash": row_hash,
    }


def list_audit_logs(limit: int = 100, tenant_id: str | None = None) -> list[dict]:
    with get_connection() as conn:
        if tenant_id:
            rows = conn.execute(
                """
                SELECT * FROM audit_logs
                WHERE tenant_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM audit_logs
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
    return [hydrate_audit_log(item) for item in rows_to_dicts(rows)]


def list_it_audit_chain(ticket_id: str, tenant_id: str | None = None) -> list[dict]:
    """Every ``it.*`` audit row recorded against one ticket, oldest first.

    Ordered by ``rowid`` rather than ``created_at``: ``audit_logs.id`` is random
    hex and ``created_at`` resolves only to the microsecond, so two events
    written back to back can land on the same timestamp and a sort on it would
    be free to swap them. Insertion order is the only ordering actually
    guaranteed, and it is what makes the result read as the reasoning sequence
    the chain view presents it as.

    The ``it.*`` filter is what keeps this about the IT loop: the ``ticket.*``
    lifecycle rows interleaved with them are the generic ticketing trail and are
    returned separately by ``list_ticket_events``.
    """
    clauses = ["target_type = 'ticket'", "target_id = ?", "event_type LIKE 'it.%'"]
    params: list[Any] = [ticket_id]
    if tenant_id:
        clauses.append("tenant_id = ?")
        params.append(tenant_id)
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM audit_logs WHERE {' AND '.join(clauses)} ORDER BY rowid ASC",
            params,
        ).fetchall()
    return [hydrate_audit_log(item) for item in rows_to_dicts(rows)]


def verify_audit_log_integrity(limit: int | None = None) -> dict:
    sql_limit = "" if limit is None else "LIMIT ?"
    params: tuple[Any, ...] = () if limit is None else (max(1, min(limit, 10000)),)
    with get_connection() as conn:
        rows = rows_to_dicts(
            conn.execute(
                f"""
                SELECT *
                FROM audit_logs
                ORDER BY created_at ASC, id ASC
                {sql_limit}
                """,
                params,
            ).fetchall()
        )

    checked = 0
    legacy = 0
    tampered: list[dict[str, Any]] = []
    broken_links: list[dict[str, Any]] = []
    unhashed: list[dict[str, Any]] = []
    orphaned_prefix = False
    previous_hash: str | None = None
    seen_hashed = False

    for row in rows:
        expected_hash = row.get("row_hash")
        if not expected_hash:
            # A row without a hash is only benign *before* the hash chain starts:
            # those were written by versions that predate row hashing. A gap
            # *after* a hashed row means the chain was interrupted -- by a bug or
            # by someone clearing hashes to hide an edit. Treating every gap as
            # "legacy" is what made `UPDATE audit_logs SET row_hash = NULL`
            # a complete bypass of this verifier.
            if seen_hashed:
                unhashed.append({"id": row.get("id"), "reason": "missing_row_hash_after_hashed_row"})
            else:
                legacy += 1
            continue
        checked += 1
        actual_hash = audit_row_hash(row)
        legacy_hash = audit_row_hash(row, include_tenant=False)
        if actual_hash != expected_hash and legacy_hash != expected_hash:
            tampered.append({"id": row.get("id"), "expected": expected_hash, "actual": actual_hash})
        stored_previous = row.get("previous_hash")
        if not seen_hashed:
            orphaned_prefix = bool(stored_previous)
        elif stored_previous != previous_hash:
            broken_links.append(
                {
                    "id": row.get("id"),
                    "expected_previous_hash": previous_hash,
                    "actual_previous_hash": stored_previous,
                }
            )
        previous_hash = expected_hash
        seen_hashed = True

    return {
        # `checked > 0` matters: an empty log, a log whose rows were all deleted,
        # or one whose hashes were all cleared has nothing to verify, and
        # "nothing to verify" must not read as "verified". Same for `unhashed`:
        # a hole in the chain is not a pass.
        "valid": not tampered and not broken_links and not unhashed and checked > 0,
        "checked_count": checked,
        "legacy_count": legacy,
        "unhashed_count": len(unhashed),
        "total_count": len(rows),
        "tampered_count": len(tampered),
        "broken_link_count": len(broken_links),
        "orphaned_prefix": orphaned_prefix,
        # True when verification succeeded but part of the log predates hashing.
        # Callers that need "the whole log is provably intact" must check this.
        "degraded": legacy > 0,
        "latest_hash": previous_hash,
        "tampered": tampered[:20],
        "broken_links": broken_links[:20],
        "unhashed": unhashed[:20],
    }


def hydrate_audit_log(row: dict) -> dict:
    row["detail"] = json_loads(row.get("detail_json"), {})
    return row


def audit_row_hash(row: dict[str, Any], *, include_tenant: bool = True) -> str:
    payload = {
        "id": row.get("id"),
        "actor": row.get("actor"),
        "event_type": row.get("event_type"),
        "target_type": row.get("target_type"),
        "target_id": row.get("target_id"),
        "detail_json": row.get("detail_json") or "{}",
        "previous_hash": row.get("previous_hash"),
        "created_at": row.get("created_at"),
    }
    if include_tenant:
        payload["tenant_id"] = row.get("tenant_id") or effective_tenant_id()
    encoded = json_dumps(payload).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _audit_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
