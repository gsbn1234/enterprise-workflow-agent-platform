from __future__ import annotations

from typing import Any

from app.config import settings
from app.db import get_connection, rows_to_dicts
from app.services.llm_telemetry import llm_usage_summary
from app.services.queue import queue_status
from app.utils import utc_now


def metrics_summary(tenant_id: str | None = None) -> dict:
    tenant_params = (tenant_id, tenant_id)
    with get_connection() as conn:
        run_counts = rows_to_dicts(
            conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM workflow_runs
                WHERE (? IS NULL OR tenant_id = ?)
                GROUP BY status
                """,
                tenant_params,
            ).fetchall()
        )
        tool_counts = rows_to_dicts(
            conn.execute(
                """
                SELECT steps.tool_name, COUNT(*) AS count, AVG(steps.latency_ms) AS avg_latency_ms
                FROM workflow_steps steps
                JOIN workflow_runs runs ON runs.id = steps.run_id
                WHERE steps.tool_name IS NOT NULL
                  AND (? IS NULL OR runs.tenant_id = ?)
                GROUP BY steps.tool_name
                ORDER BY count DESC
                """,
                tenant_params,
            ).fetchall()
        )
        totals = conn.execute(
            """
            WITH
              scoped_runs AS (SELECT * FROM workflow_runs WHERE (? IS NULL OR tenant_id = ?)),
              scoped_approvals AS (SELECT * FROM approvals WHERE (? IS NULL OR tenant_id = ?)),
              scoped_tickets AS (SELECT * FROM tickets WHERE (? IS NULL OR tenant_id = ?)),
              scoped_emails AS (SELECT * FROM emails WHERE (? IS NULL OR tenant_id = ?)),
              scoped_jobs AS (SELECT * FROM workflow_jobs WHERE (? IS NULL OR tenant_id = ?)),
              scoped_multi_runs AS (SELECT * FROM multi_agent_runs WHERE (? IS NULL OR tenant_id = ?)),
              scoped_customers AS (SELECT * FROM customers WHERE (? IS NULL OR tenant_id = ?)),
              scoped_interactions AS (SELECT * FROM customer_interactions WHERE (? IS NULL OR tenant_id = ?))
            SELECT
                (SELECT COUNT(*) FROM scoped_runs) AS runs,
                (SELECT COUNT(*) FROM scoped_runs WHERE status = 'completed') AS completed_runs,
                (SELECT COUNT(*) FROM scoped_runs WHERE status = 'failed') AS failed_runs,
                (SELECT COUNT(*) FROM scoped_runs WHERE status = 'waiting_approval') AS waiting_runs,
                (SELECT COUNT(*) FROM scoped_approvals WHERE status = 'pending') AS pending_approvals,
                (SELECT COUNT(*) FROM scoped_approvals WHERE status IN ('approved', 'denied')) AS decided_approvals,
                (SELECT COUNT(*) FROM scoped_tickets) AS tickets,
                (SELECT COUNT(*) FROM scoped_tickets WHERE provider != 'mock' OR external_id IS NOT NULL) AS external_tickets,
                (SELECT COUNT(*) FROM scoped_emails) AS emails,
                (SELECT COUNT(*) FROM scoped_emails WHERE status = 'sent') AS sent_emails,
                (SELECT COUNT(*) FROM scoped_emails WHERE status = 'failed') AS failed_emails,
                (SELECT COUNT(*) FROM scoped_jobs) AS jobs,
                (SELECT COUNT(*) FROM scoped_customers) AS customers,
                (SELECT COUNT(*) FROM scoped_customers WHERE status = 'at_risk') AS at_risk_customers,
                (SELECT COUNT(*) FROM scoped_interactions) AS customer_interactions,
                (SELECT COALESCE(AVG(latency_ms), 0) FROM scoped_runs) AS avg_latency_ms,
                (SELECT COALESCE(AVG(critic_score), 0) FROM scoped_multi_runs) AS avg_critic_score,
                (SELECT COALESCE(SUM(cost_estimate), 0) FROM scoped_runs) AS estimated_cost
            """,
            tenant_params * 8,
        ).fetchone()
        job_counts = rows_to_dicts(
            conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM workflow_jobs
                WHERE (? IS NULL OR tenant_id = ?)
                GROUP BY status
                """,
                tenant_params,
            ).fetchall()
        )
    return {
        "totals": dict(totals),
        "run_counts": run_counts,
        "job_counts": job_counts,
        "tool_counts": tool_counts,
        "queue": queue_status(check_connection=False),
    }


def prometheus_metrics(tenant_id: str | None = None) -> str:
    """Render the Prometheus text exposition.

    ``tenant_id`` scopes **every** series on the endpoint. It is one argument
    rather than per-family handling because a scrape that scopes some families
    and not others can print two contradictory numbers for one fact:
    ``agent_pending_approvals_total`` comes out of ``metrics_summary``, which
    honours the scope, while ``agent_approvals_total{status="pending"}`` used to
    be a bare ``GROUP BY`` over the whole table. Under tenant isolation those
    two lines described different sets of rows, in the same response, with
    nothing to say so. Callers pass the same scope they pass to
    ``/api/metrics/summary``; ``None`` means platform-wide, which is what every
    caller got before this parameter existed and still gets whenever
    ``AGENT_TENANT_ISOLATION_ENABLED`` is off (its default).
    """
    tenant_params = (tenant_id, tenant_id)
    summary = metrics_summary(tenant_id)
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
        "agent_customers_total": totals.get("customers", 0),
        "agent_customers_at_risk_total": totals.get("at_risk_customers", 0),
        "agent_customer_interactions_total": totals.get("customer_interactions", 0),
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
        # Every statement here that *can* be scoped carries the same predicate
        # the queries behind ``metrics_summary`` use, so a family rendered from
        # either side counts the same rows.
        #
        # The login-attempt families are the exception and are left platform-wide
        # on purpose: ``auth_login_attempts`` has no ``tenant_id`` column, so
        # there is no scope to apply. A tenant-scoped scrape therefore reports a
        # partitioned view of everything except these two, which count the whole
        # table. That is the honest reading of the schema, and the alternative --
        # joining through ``users`` to invent a tenant -- would silently drop
        # attempts against user ids that no longer resolve.
        grouped_specs = [
            (
                "agent_approvals_total",
                "Approval count by status.",
                "SELECT status, COUNT(*) AS count FROM approvals WHERE (? IS NULL OR tenant_id = ?) GROUP BY status",
                "status",
                tenant_params,
            ),
            (
                "agent_tickets_by_status_total",
                "Ticket count by status.",
                "SELECT status, COUNT(*) AS count FROM tickets WHERE (? IS NULL OR tenant_id = ?) GROUP BY status",
                "status",
                tenant_params,
            ),
            (
                "agent_emails_by_status_total",
                "Email count by status.",
                "SELECT status, COUNT(*) AS count FROM emails WHERE (? IS NULL OR tenant_id = ?) GROUP BY status",
                "status",
                tenant_params,
            ),
            (
                "agent_external_outbox_total",
                "External outbox count by status.",
                "SELECT status, COUNT(*) AS count FROM external_outbox WHERE (? IS NULL OR tenant_id = ?) GROUP BY status",
                "status",
                tenant_params,
            ),
            (
                "agent_login_attempts_total",
                "Login attempt count by outcome, across all tenants (the table has no tenant column).",
                """
                SELECT CASE WHEN success = 1 THEN 'success' ELSE COALESCE(failure_reason, 'failed') END AS outcome,
                       COUNT(*) AS count
                FROM auth_login_attempts
                GROUP BY outcome
                """,
                "outcome",
                (),
            ),
        ]
        for metric_name, help_text, sql, label_name, params in grouped_specs:
            _metric(lines, metric_name, help_text, "gauge")
            rows = rows_to_dicts(conn.execute(sql, params).fetchall())
            for row in rows:
                label_value = _label(row[label_name])
                lines.append(f'{metric_name}{{{label_name}="{label_value}"}} {_number(row["count"])}')

        # Platform-wide for the same reason as ``agent_login_attempts_total``.
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

    # LLM telemetry, read from the same table the per-call audit writes to. The
    # project already exports Prometheus, so this joins the existing endpoint
    # rather than introducing a second monitoring path.
    #
    # The aggregation itself is ``llm_usage_summary`` -- the same function
    # ``/api/metrics/summary`` reports ``llm_usage`` from, under the same tenant
    # scope. It used to be written out a second time here, in SQL that ignored
    # the tenant and named the usage counters from the opposite side. Two
    # aggregations over one table are two numbers that are free to disagree
    # without anyone noticing, so there is now one.
    #
    # ``agent_llm_usage_unavailable_calls_total`` is the honest counter beside
    # ``agent_llm_tokens_total``: a gateway that reports no usage would
    # otherwise make the token totals look like a decrease in spend rather than
    # a gap in the data. Its complement is exported too, so
    # ``calls == available + unavailable`` is checkable from the scrape alone
    # instead of being something a reader has to know.
    llm_usage = llm_usage_summary(tenant_id)
    llm_totals = llm_usage["totals"]
    llm_calls = int(llm_totals.get("calls") or 0)
    llm_usage_available = int(llm_totals.get("usage_available_calls") or 0)

    _metric(lines, "agent_llm_calls_total", "LLM call count by operation, model, status and error type.", "gauge")
    _metric(lines, "agent_llm_latency_ms_avg", "Average LLM call latency in milliseconds.", "gauge")
    for row in llm_usage["by_operation_status"]:
        selector = (
            f'operation="{_label(row["operation"])}",model="{_label(row["model"])}",'
            f'status="{_label(row["status"])}",error_type="{_label(row["error_type"])}"'
        )
        lines.append(f"agent_llm_calls_total{{{selector}}} {_number(row['count'])}")
        lines.append(f"agent_llm_latency_ms_avg{{{selector}}} {_number(row['avg_latency_ms'])}")
    _metric(lines, "agent_llm_tokens_total", "LLM tokens reported by the provider, by kind.", "gauge")
    for kind in ("prompt", "completion", "total"):
        lines.append(f'agent_llm_tokens_total{{kind="{kind}"}} {_number(llm_totals.get(f"{kind}_tokens"))}')
    for name, value in (
        ("agent_llm_retries_total", llm_totals.get("retries")),
        ("agent_llm_usage_available_calls_total", llm_usage_available),
        ("agent_llm_usage_unavailable_calls_total", llm_calls - llm_usage_available),
        ("agent_llm_fallback_total", llm_totals.get("fallback_calls")),
    ):
        _metric(lines, name, name.replace("_", " ").capitalize() + ".", "gauge")
        lines.append(f"{name} {_number(value)}")

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
