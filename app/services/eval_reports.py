from __future__ import annotations

from app.db import get_connection, rows_to_dicts
from app.utils import json_loads


def list_eval_reports(limit: int = 100) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM eval_reports
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 500)),),
        ).fetchall()
    reports = rows_to_dicts(rows)
    for report in reports:
        report["report"] = json_loads(report.pop("report_json"), {})
    return reports
