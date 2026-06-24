from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.db import get_connection


@dataclass(frozen=True)
class RetentionPolicy:
    name: str
    table: str
    timestamp_column: str
    retention_days: int
    description: str
    extra_where: str | None = None


def retention_policies() -> list[RetentionPolicy]:
    return [
        RetentionPolicy(
            name="audit_logs",
            table="audit_logs",
            timestamp_column="created_at",
            retention_days=settings.audit_retention_days,
            description="Audit events older than the configured compliance window.",
        ),
        RetentionPolicy(
            name="eval_reports",
            table="eval_reports",
            timestamp_column="created_at",
            retention_days=settings.eval_report_retention_days,
            description="Evaluation reports that can be regenerated from golden traces.",
        ),
        RetentionPolicy(
            name="external_outbox",
            table="external_outbox",
            timestamp_column="created_at",
            retention_days=settings.external_outbox_retention_days,
            description="Completed or failed external side-effect outbox records.",
            extra_where="status IN ('completed', 'failed')",
        ),
        RetentionPolicy(
            name="emails",
            table="emails",
            timestamp_column="created_at",
            retention_days=settings.email_retention_days,
            description="Generated email records older than the configured message history window.",
            extra_where="status IN ('sent', 'failed', 'prepared')",
        ),
        RetentionPolicy(
            name="workflow_jobs",
            table="workflow_jobs",
            timestamp_column="updated_at",
            retention_days=settings.workflow_job_retention_days,
            description="Finished background workflow jobs.",
            extra_where="status IN ('completed', 'failed')",
        ),
        RetentionPolicy(
            name="auth_login_attempts",
            table="auth_login_attempts",
            timestamp_column="created_at",
            retention_days=settings.login_attempt_retention_days,
            description="Login attempt audit records outside the security investigation window.",
        ),
    ]


def retention_status() -> dict[str, Any]:
    policies = [_policy_snapshot(policy) for policy in retention_policies()]
    enabled = [policy for policy in policies if policy["enabled"]]
    return {
        "enabled": bool(enabled),
        "enabled_policy_count": len(enabled),
        "policy_count": len(policies),
        "policies": policies,
    }


def retention_plan() -> dict[str, Any]:
    return _retention_result(apply=False)


def apply_retention() -> dict[str, Any]:
    return _retention_result(apply=True)


def _retention_result(*, apply: bool) -> dict[str, Any]:
    policies = retention_policies()
    results: list[dict[str, Any]] = []
    total = 0
    with get_connection() as conn:
        for policy in policies:
            snapshot = _policy_snapshot(policy)
            if not snapshot["enabled"]:
                snapshot["matched_count"] = 0
                snapshot["deleted_count"] = 0
                results.append(snapshot)
                continue

            where_sql, params = _policy_where(policy, snapshot["cutoff_at"])
            if apply:
                cursor = conn.execute(f"DELETE FROM {policy.table} WHERE {where_sql}", params)
                deleted = int(cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else 0)
                snapshot["matched_count"] = deleted
                snapshot["deleted_count"] = deleted
            else:
                row = conn.execute(f"SELECT COUNT(*) AS count FROM {policy.table} WHERE {where_sql}", params).fetchone()
                count = int(row["count"] if isinstance(row, dict) else row[0])
                snapshot["matched_count"] = count
                snapshot["deleted_count"] = 0
                deleted = 0
            total += deleted
            results.append(snapshot)

    return {
        "mode": "apply" if apply else "plan",
        "applied": apply,
        "total_deleted": total,
        "policies": results,
    }


def _policy_snapshot(policy: RetentionPolicy) -> dict[str, Any]:
    enabled = policy.retention_days > 0
    cutoff = _cutoff_at(policy.retention_days) if enabled else None
    return {
        "name": policy.name,
        "table": policy.table,
        "timestamp_column": policy.timestamp_column,
        "retention_days": policy.retention_days,
        "enabled": enabled,
        "cutoff_at": cutoff,
        "description": policy.description,
    }


def _policy_where(policy: RetentionPolicy, cutoff_at: str | None) -> tuple[str, tuple[Any, ...]]:
    if not cutoff_at:
        raise ValueError(f"Retention policy {policy.name} is disabled.")
    parts = [f"{policy.timestamp_column} < ?"]
    if policy.extra_where:
        parts.append(policy.extra_where)
    return " AND ".join(parts), (cutoff_at,)


def _cutoff_at(retention_days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=retention_days)).replace(microsecond=0).isoformat()
