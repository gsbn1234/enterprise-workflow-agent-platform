from __future__ import annotations

import re

from app.db import get_connection, row_to_dict, rows_to_dicts


EMAIL_RE = re.compile(r"[\w.\-+]+@[\w.\-]+\.\w+")


def lookup_customer(query: str) -> dict:
    email_match = EMAIL_RE.search(query)
    with get_connection() as conn:
        if email_match:
            row = conn.execute(
                "SELECT * FROM customers WHERE lower(email) = lower(?)",
                (email_match.group(0),),
            ).fetchone()
            return {"customer": row_to_dict(row), "matched_by": "email" if row else None}

        rows = conn.execute("SELECT * FROM customers").fetchall()
    lowered = query.lower()
    for row in rows:
        customer = row_to_dict(row)
        if customer["name"].lower() in lowered or customer["email"].lower() in lowered:
            return {"customer": customer, "matched_by": "name"}
    return {"customer": None, "matched_by": None}


def list_customers(limit: int = 100) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM customers ORDER BY name LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
    return rows_to_dicts(rows)
