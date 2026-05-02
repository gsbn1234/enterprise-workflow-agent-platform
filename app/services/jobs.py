from __future__ import annotations

import time
from typing import Any

from app.config import settings
from app.db import get_connection, row_to_dict, rows_to_dicts
from app.services.agent import run_workflow
from app.services.audit import record_audit
from app.utils import new_id, utc_now


def create_workflow_job(
    objective: str,
    *,
    request_id: str | None = None,
    requester_user_id: str | None = None,
    requester_department: str | None = None,
    max_attempts: int | None = None,
) -> dict:
    job_id = new_id("job")
    now = utc_now()
    attempts_limit = max_attempts or settings.job_max_attempts
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO workflow_jobs
            (id, objective, request_id, requester_user_id, requester_department,
             status, attempts, max_attempts, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?)
            """,
            (job_id, objective, request_id, requester_user_id, requester_department, attempts_limit, now, now),
        )
    record_audit(
        "workflow_job.create",
        "workflow_job",
        job_id,
        {"request_id": request_id, "max_attempts": attempts_limit},
        actor=requester_user_id or "agent",
    )
    return get_workflow_job(job_id)


def get_workflow_job(job_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM workflow_jobs WHERE id = ?", (job_id,)).fetchone()
    return row_to_dict(row)


def list_workflow_jobs(status: str | None = None, limit: int = 100) -> list[dict]:
    with get_connection() as conn:
        if status:
            rows = conn.execute(
                """
                SELECT * FROM workflow_jobs
                WHERE status = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (status, max(1, min(limit, 500))),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM workflow_jobs
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
    return rows_to_dicts(rows)


def retry_workflow_job(job_id: str) -> dict | None:
    job = get_workflow_job(job_id)
    if not job:
        return None
    if job["status"] not in {"failed", "queued"}:
        return job
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE workflow_jobs
            SET status = 'queued', error_message = NULL, updated_at = ?, started_at = NULL, completed_at = NULL
            WHERE id = ?
            """,
            (utc_now(), job_id),
        )
    record_audit("workflow_job.retry", "workflow_job", job_id, {"previous_status": job["status"]})
    return get_workflow_job(job_id)


def claim_next_job(worker_id: str = "worker") -> dict | None:
    now = utc_now()
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM workflow_jobs
            WHERE status = 'queued'
              AND attempts < max_attempts
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            conn.commit()
            return None
        job = row_to_dict(row)
        conn.execute(
            """
            UPDATE workflow_jobs
            SET status = 'running',
                attempts = attempts + 1,
                error_message = NULL,
                started_at = COALESCE(started_at, ?),
                updated_at = ?
            WHERE id = ? AND status = 'queued'
            """,
            (now, now, job["id"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    record_audit("workflow_job.claim", "workflow_job", job["id"], {"worker_id": worker_id})
    return get_workflow_job(job["id"])


def process_next_job(worker_id: str = "worker") -> dict | None:
    job = claim_next_job(worker_id)
    if not job:
        return None
    try:
        run = run_workflow(
            job["objective"],
            request_id=job.get("request_id"),
            requester_user_id=job.get("requester_user_id"),
            requester_department=job.get("requester_department"),
        )
    except Exception as exc:
        return mark_job_failed(job["id"], str(exc))
    return mark_job_completed(job["id"], run["id"])


def mark_job_completed(job_id: str, run_id: str) -> dict:
    now = utc_now()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE workflow_jobs
            SET status = 'completed',
                run_id = ?,
                error_message = NULL,
                updated_at = ?,
                completed_at = ?
            WHERE id = ?
            """,
            (run_id, now, now, job_id),
        )
    record_audit("workflow_job.complete", "workflow_job", job_id, {"run_id": run_id})
    return get_workflow_job(job_id)


def mark_job_failed(job_id: str, error_message: str) -> dict:
    job = get_workflow_job(job_id)
    if not job:
        raise ValueError(f"Workflow job not found: {job_id}")
    should_retry = int(job["attempts"]) < int(job["max_attempts"])
    status = "queued" if should_retry else "failed"
    now = utc_now()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE workflow_jobs
            SET status = ?,
                error_message = ?,
                updated_at = ?,
                completed_at = CASE WHEN ? = 'failed' THEN ? ELSE completed_at END
            WHERE id = ?
            """,
            (status, error_message, now, status, now, job_id),
        )
    record_audit("workflow_job.fail", "workflow_job", job_id, {"status": status, "error": error_message})
    return get_workflow_job(job_id)


def run_worker_loop(
    *,
    worker_id: str = "worker",
    poll_interval_seconds: float | None = None,
    stop_after_idle: int | None = None,
) -> None:
    idle_count = 0
    interval = settings.job_poll_interval_seconds if poll_interval_seconds is None else poll_interval_seconds
    while True:
        job = process_next_job(worker_id)
        if job:
            idle_count = 0
            continue
        idle_count += 1
        if stop_after_idle is not None and idle_count >= stop_after_idle:
            return
        time.sleep(interval)


def job_metrics() -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM workflow_jobs
            GROUP BY status
            """
        ).fetchall()
    return rows_to_dicts(rows)
