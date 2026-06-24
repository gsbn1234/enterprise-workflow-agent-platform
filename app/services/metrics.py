from __future__ import annotations

from typing import Any

from app.config import settings
from app.db import get_connection, rows_to_dicts
from app.services.queue import queue_status
from app.utils import utc_now


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
                (SELECT COUNT(*) FROM workflow_runs WHERE status = 'completed') AS completed_runs,
                (SELECT COUNT(*) FROM workflow_runs WHERE status = 'failed') AS failed_runs,
                (SELECT COUNT(*) FROM workflow_runs WHERE status = 'waiting_approval') AS waiting_runs,
                (SELECT COUNT(*) FROM approvals WHERE status = 'pending') AS pending_approvals,
                (SELECT COUNT(*) FROM approvals WHERE status IN ('approved', 'denied')) AS decided_approvals,
                (SELECT COUNT(*) FROM tickets) AS tickets,
                (SELECT COUNT(*) FROM tickets WHERE provider != 'mock' OR external_id IS NOT NULL) AS external_tickets,
                (SELECT COUNT(*) FROM emails) AS emails,
                (SELECT COUNT(*) FROM emails WHERE status = 'sent') AS sent_emails,
                (SELECT COUNT(*) FROM emails WHERE status = 'failed') AS failed_emails,
                (SELECT COUNT(*) FROM workflow_jobs) AS jobs,
                (SELECT COALESCE(AVG(latency_ms), 0) FROM workflow_runs) AS avg_latency_ms,
                (SELECT COALESCE(AVG(critic_score), 0) FROM multi_agent_runs) AS avg_critic_score,
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
        "queue": queue_status(check_connection=False),
    }


def prometheus_metrics() -> str:
    summary = metrics_summary()
    totals = summary["totals"]
    lines: list[str] = [
        "# HELP agent_platform_up Agent platform process and database are reachable.",
        "# TYPE agent_platform_up gauge",
        "agent_platform_up 1",
        "# HELP agent_platform_info Static Agent platform runtime info.",
        "# TYPE agent_platform_info gauge",
        f'agent_platform_info{{db_backend="{_label(settings.db_backend)}",env="{_label(settings.app_env)}"}} 1',
        "# HELP agent_queue_info Static Agent queue backend info.",
        "# TYPE agent_queue_info gauge",
        f'agent_queue_info{{backend="{_label(summary["queue"]["backend"])}",queue_name="{_label(summary["queue"].get("redis_queue_name") or "")}"}} 1',
    ]

    _metric(lines, "agent_workflow_runs_total", "Workflow run count by status.", "gauge")
    for row in summary["run_counts"]:
        lines.append(f'agent_workflow_runs_total{{status="{_label(row["status"])}"}} {_number(row["count"])}')

    _metric(lines, "agent_workflow_jobs_total", "Workflow job count by status.", "gauge")
    for row in summary["job_counts"]:
        lines.append(f'agent_workflow_jobs_total{{status="{_label(row["status"])}"}} {_number(row["count"])}')

    _metric(lines, "agent_tool_calls_total", "Workflow tool call count by tool name.", "gauge")
    _metric(lines, "agent_tool_latency_ms_avg", "Average workflow tool latency by tool name.", "gauge")
    for row in summary["tool_counts"]:
        tool_name = _label(row.get("tool_name") or "unknown")
        lines.append(f'agent_tool_calls_total{{tool="{tool_name}"}} {_number(row["count"])}')
        lines.append(f'agent_tool_latency_ms_avg{{tool="{tool_name}"}} {_number(row.get("avg_latency_ms", 0))}')

    scalar_metrics = {
        "agent_workflow_runs_all_total": totals.get("runs", 0),
        "agent_workflow_runs_completed_total": totals.get("completed_runs", 0),
        "agent_workflow_runs_failed_total": totals.get("failed_runs", 0),
        "agent_workflow_runs_waiting_total": totals.get("waiting_runs", 0),
        "agent_pending_approvals_total": totals.get("pending_approvals", 0),
        "agent_decided_approvals_total": totals.get("decided_approvals", 0),
        "agent_tickets_total": totals.get("tickets", 0),
        "agent_external_tickets_total": totals.get("external_tickets", 0),
        "agent_emails_total": totals.get("emails", 0),
        "agent_emails_sent_total": totals.get("sent_emails", 0),
        "agent_emails_failed_total": totals.get("failed_emails", 0),
        "agent_jobs_total": totals.get("jobs", 0),
        "agent_workflow_latency_ms_avg": totals.get("avg_latency_ms", 0),
        "agent_multi_agent_critic_score_avg": totals.get("avg_critic_score", 0),
        "agent_estimated_cost_total": totals.get("estimated_cost", 0),
    }
    for name, value in scalar_metrics.items():
        _metric(lines, name, name.replace("_", " ").capitalize() + ".", "gauge")
        lines.append(f"{name} {_number(value)}")

    with get_connection() as conn:
        grouped_specs = [
            (
                "agent_approvals_total",
                "Approval count by status.",
                "SELECT status, COUNT(*) AS count FROM approvals GROUP BY status",
                "status",
            ),
            (
                "agent_tickets_by_status_total",
                "Ticket count by status.",
                "SELECT status, COUNT(*) AS count FROM tickets GROUP BY status",
                "status",
            ),
            (
                "agent_emails_by_status_total",
                "Email count by status.",
                "SELECT status, COUNT(*) AS count FROM emails GROUP BY status",
                "status",
            ),
            (
                "agent_external_outbox_total",
                "External outbox count by status.",
                "SELECT status, COUNT(*) AS count FROM external_outbox GROUP BY status",
                "status",
            ),
            (
                "agent_login_attempts_total",
                "Login attempt count by outcome.",
                """
                SELECT CASE WHEN success = 1 THEN 'success' ELSE COALESCE(failure_reason, 'failed') END AS outcome,
                       COUNT(*) AS count
                FROM auth_login_attempts
                GROUP BY outcome
                """,
                "outcome",
            ),
        ]
        for metric_name, help_text, sql, label_name in grouped_specs:
            _metric(lines, metric_name, help_text, "gauge")
            rows = rows_to_dicts(conn.execute(sql).fetchall())
            for row in rows:
                label_value = _label(row[label_name])
                lines.append(f'{metric_name}{{{label_name}="{label_value}"}} {_number(row["count"])}')

        active_lockouts = conn.execute(
            """
            SELECT COUNT(DISTINCT user_id) AS count
            FROM auth_login_attempts
            WHERE lockout_until IS NOT NULL AND lockout_until > ?
            """,
            (utc_now(),),
        ).fetchone()
    _metric(lines, "agent_active_lockouts_total", "Currently active account lockouts.", "gauge")
    lines.append(f"agent_active_lockouts_total {_number(active_lockouts['count'] if active_lockouts else 0)}")

    return "\n".join(lines) + "\n"


def _metric(lines: list[str], name: str, help_text: str, metric_type: str) -> None:
    lines.append(f"# HELP {name} {_escape_help(help_text)}")
    lines.append(f"# TYPE {name} {metric_type}")


def _number(value: Any) -> str:
    try:
        numeric = float(value or 0)
    except (TypeError, ValueError):
        numeric = 0.0
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:.6f}"


def _label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _escape_help(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")
