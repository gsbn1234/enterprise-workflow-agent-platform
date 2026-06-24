from __future__ import annotations

import time
import logging
from typing import Any

from app.config import settings
from app.db import get_connection, is_postgres_connection, row_to_dict, rows_to_dicts
from app.services.agent import run_workflow
from app.services.audit import record_audit
from app.services.observability import reset_log_context, set_log_context, start_span
from app.services.queue import dequeue_workflow_job, enqueue_workflow_job, queue_enabled
from app.services.tenancy import effective_tenant_id, rls_system_context, tenant_context
from app.utils import new_id, utc_now


logger = logging.getLogger("agent_platform.jobs")


def create_workflow_job(
    objective: str,
    *,
    request_id: str | None = None,
    requester_user_id: str | None = None,
    requester_department: str | None = None,
    tenant_id: str | None = None,
    max_attempts: int | None = None,
) -> dict:
    job_id = new_id("job")
    now = utc_now()
    attempts_limit = max_attempts or settings.job_max_attempts
    tenant = effective_tenant_id(tenant_id)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO workflow_jobs
            (id, objective, request_id, requester_user_id, requester_department, tenant_id,
             status, attempts, max_attempts, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?)
            """,
            (job_id, objective, request_id, requester_user_id, requester_department, tenant, attempts_limit, now, now),
        )
    record_audit(
        "workflow_job.create",
        "workflow_job",
        job_id,
        {"request_id": request_id, "max_attempts": attempts_limit},
        actor=requester_user_id or "agent",
        tenant_id=tenant,
    )
    enqueue_workflow_job(job_id)
    return get_workflow_job(job_id)


def get_workflow_job(job_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM workflow_jobs WHERE id = ?", (job_id,)).fetchone()
    return row_to_dict(row)


def list_workflow_jobs(status: str | None = None, limit: int = 100, tenant_id: str | None = None) -> list[dict]:
    with get_connection() as conn:
        if status and tenant_id:
            rows = conn.execute(
                """
                SELECT * FROM workflow_jobs
                WHERE status = ? AND tenant_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (status, tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        elif status:
            rows = conn.execute(
                """
                SELECT * FROM workflow_jobs
                WHERE status = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (status, max(1, min(limit, 500))),
            ).fetchall()
        elif tenant_id:
            rows = conn.execute(
                """
                SELECT * FROM workflow_jobs
                WHERE tenant_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (tenant_id, max(1, min(limit, 500))),
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
    record_audit("workflow_job.retry", "workflow_job", job_id, {"previous_status": job["status"]}, tenant_id=job.get("tenant_id"))
    enqueue_workflow_job(job_id)
    return get_workflow_job(job_id)


def claim_job(job_id: str | None = None, worker_id: str = "worker") -> dict | None:
    now = utc_now()
    with rls_system_context():
        conn = get_connection()
        try:
            if is_postgres_connection(conn):
                if job_id:
                    row = conn.execute(
                        """
                        SELECT * FROM workflow_jobs
                        WHERE id = ?
                          AND status = 'queued'
                          AND attempts < max_attempts
                        FOR UPDATE SKIP LOCKED
                        """,
                        (job_id,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """
                        SELECT * FROM workflow_jobs
                        WHERE status = 'queued'
                          AND attempts < max_attempts
                        ORDER BY created_at ASC, id ASC
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                        """
                    ).fetchone()
            else:
                conn.execute("BEGIN IMMEDIATE")
                if job_id:
                    row = conn.execute(
                        """
                        SELECT * FROM workflow_jobs
                        WHERE id = ?
                          AND status = 'queued'
                          AND attempts < max_attempts
                        LIMIT 1
                        """,
                        (job_id,),
                    ).fetchone()
                else:
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
    with tenant_context(job.get("tenant_id")):
        record_audit("workflow_job.claim", "workflow_job", job["id"], {"worker_id": worker_id}, tenant_id=job.get("tenant_id"))
        return get_workflow_job(job["id"])


def claim_next_job(worker_id: str = "worker") -> dict | None:
    return claim_job(worker_id=worker_id)


def process_next_job(worker_id: str = "worker") -> dict | None:
    queued_job_id = dequeue_workflow_job() if queue_enabled() else None
    job = claim_job(queued_job_id, worker_id) if queued_job_id else claim_next_job(worker_id)
    if queued_job_id and not job:
        logger.info(
            "queue.job_signal_discarded",
            extra={"event": "queue.job_signal_discarded", "job_id": queued_job_id, "reason": "not_claimable"},
        )
        job = claim_next_job(worker_id)
    if not job:
        return None
    log_token = set_log_context(job_id=job["id"], tenant_id=job.get("tenant_id"), worker_id=worker_id)
    try:
        logger.info("workflow_job.processing_started", extra={"event": "workflow_job.processing_started"})
        with start_span(
            "workflow_job.process",
            {
                "workflow.job_id": job["id"],
                "workflow.worker_id": worker_id,
                "tenant.id": job.get("tenant_id"),
            },
        ):
            with tenant_context(job.get("tenant_id")):
                run = run_workflow(
                    job["objective"],
                    request_id=job.get("request_id"),
                    requester_user_id=job.get("requester_user_id"),
                    requester_department=job.get("requester_department"),
                    tenant_id=job.get("tenant_id"),
                )
    except Exception as exc:
        logger.exception("workflow_job.processing_failed", extra={"event": "workflow_job.processing_failed", "error": str(exc)})
        return mark_job_failed(job["id"], str(exc))
    finally:
        reset_log_context(log_token)
    if run.get("status") == "failed":
        logger.warning(
            "workflow_job.run_failed",
            extra={
                "event": "workflow_job.run_failed",
                "job_id": job["id"],
                "run_id": run.get("id"),
                "run_status": run.get("status"),
            },
        )
        return mark_job_failed(job["id"], run.get("final_answer") or "Workflow run failed.", run_id=run.get("id"))

    completed = mark_job_completed(job["id"], run["id"])
    logger.info(
        "workflow_job.processing_completed",
        extra={"event": "workflow_job.processing_completed", "job_id": job["id"], "run_id": run["id"]},
    )
    return completed


def mark_job_completed(job_id: str, run_id: str) -> dict:
    now = utc_now()
    with rls_system_context():
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
            job = row_to_dict(conn.execute("SELECT * FROM workflow_jobs WHERE id = ?", (job_id,)).fetchone())
    with tenant_context((job or {}).get("tenant_id")):
        record_audit("workflow_job.complete", "workflow_job", job_id, {"run_id": run_id}, tenant_id=(job or {}).get("tenant_id"))
    return job


def mark_job_failed(job_id: str, error_message: str, run_id: str | None = None) -> dict:
    with rls_system_context():
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
                    run_id = COALESCE(?, run_id),
                    error_message = ?,
                    updated_at = ?,
                    completed_at = CASE WHEN ? = 'failed' THEN ? ELSE completed_at END
                WHERE id = ?
                """,
                (status, run_id, error_message, now, status, now, job_id),
            )
    with tenant_context(job.get("tenant_id")):
        record_audit(
            "workflow_job.fail",
            "workflow_job",
            job_id,
            {"status": status, "error": error_message, "run_id": run_id},
            tenant_id=job.get("tenant_id"),
        )
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
