from __future__ import annotations

from app.db import get_connection, rows_to_dicts


def metrics_summary() -> dict:
    with get_connection() as conn:
        run_counts = rows_to_dicts(
            conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM workflow_runs
                GROUP BY status
                """
            ).fetchall()
        )
        tool_counts = rows_to_dicts(
            conn.execute(
                """
                SELECT tool_name, COUNT(*) AS count, AVG(latency_ms) AS avg_latency_ms
                FROM workflow_steps
                WHERE tool_name IS NOT NULL
                GROUP BY tool_name
                ORDER BY count DESC
                """
            ).fetchall()
        )
        totals = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM workflow_runs) AS runs,
                (SELECT COUNT(*) FROM approvals WHERE status = 'pending') AS pending_approvals,
                (SELECT COUNT(*) FROM tickets) AS tickets,
                (SELECT COUNT(*) FROM emails) AS emails,
                (SELECT COUNT(*) FROM workflow_jobs) AS jobs,
                (SELECT COALESCE(AVG(latency_ms), 0) FROM workflow_runs) AS avg_latency_ms,
                (SELECT COALESCE(SUM(cost_estimate), 0) FROM workflow_runs) AS estimated_cost
            """
        ).fetchone()
        job_counts = rows_to_dicts(
            conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM workflow_jobs
                GROUP BY status
                """
            ).fetchall()
        )
    return {
        "totals": dict(totals),
        "run_counts": run_counts,
        "job_counts": job_counts,
        "tool_counts": tool_counts,
    }
