from __future__ import annotations

import logging
from typing import Any

from app.config import settings

try:
    import redis
except ModuleNotFoundError:  # pragma: no cover - optional dependency path
    redis = None


logger = logging.getLogger("agent_platform.queue")


def queue_enabled() -> bool:
    return settings.queue_backend == "redis"


def enqueue_workflow_job(job_id: str) -> bool:
    if not queue_enabled():
        return False
    client = _redis_client()
    if not client:
        logger.warning(
            "queue.enqueue_skipped",
            extra={"event": "queue.enqueue_skipped", "job_id": job_id, "reason": "redis_not_configured"},
        )
        return False
    try:
        client.rpush(settings.redis_queue_name, job_id)
    except Exception as exc:
        logger.warning(
            "queue.enqueue_failed",
            extra={"event": "queue.enqueue_failed", "job_id": job_id, "error": str(exc)},
        )
        return False
    logger.info(
        "queue.job_enqueued",
        extra={"event": "queue.job_enqueued", "job_id": job_id, "queue_name": settings.redis_queue_name},
    )
    return True


def dequeue_workflow_job(timeout_seconds: int | None = None) -> str | None:
    if not queue_enabled():
        return None
    client = _redis_client()
    if not client:
        return None
    timeout = settings.redis_block_timeout_seconds if timeout_seconds is None else max(0, int(timeout_seconds))
    try:
        item = client.blpop(settings.redis_queue_name, timeout=timeout)
    except Exception as exc:
        if _is_empty_queue_timeout(exc):
            return None
        logger.warning("queue.dequeue_failed", extra={"event": "queue.dequeue_failed", "error": str(exc)})
        return None
    if not item:
        return None
    _, raw_job_id = item
    if isinstance(raw_job_id, bytes):
        return raw_job_id.decode("utf-8")
    return str(raw_job_id)


def queue_status(check_connection: bool = False) -> dict[str, Any]:
    status: dict[str, Any] = {
        "backend": settings.queue_backend,
        "enabled": queue_enabled(),
        "redis_url_configured": bool(settings.redis_url),
        "redis_queue_name": settings.redis_queue_name,
        "redis_dependency_available": redis is not None,
    }
    if not queue_enabled():
        return status
    if not settings.redis_url:
        status.update({"reachable": False, "error": "AGENT_REDIS_URL is required when AGENT_QUEUE_BACKEND=redis."})
        return status
    if redis is None:
        status.update({"reachable": False, "error": "redis package is not installed."})
        return status
    if check_connection:
        try:
            client = _redis_client()
            client.ping()
            status["reachable"] = True
            try:
                status["queued_signal_count"] = int(client.llen(settings.redis_queue_name))
            except Exception:
                status["queued_signal_count"] = None
        except Exception as exc:
            status.update({"reachable": False, "error": str(exc)})
    return status


def _redis_client():
    if redis is None or not settings.redis_url:
        return None
    socket_timeout = max(settings.redis_block_timeout_seconds + 5, 10)
    return redis.Redis.from_url(
        settings.redis_url,
        decode_responses=False,
        health_check_interval=30,
        socket_connect_timeout=5,
        socket_timeout=socket_timeout,
    )


def _is_empty_queue_timeout(exc: Exception) -> bool:
    if exc.__class__.__name__ != "TimeoutError":
        return False
    message = str(exc).lower()
    return "timeout reading from socket" in message or "timed out" in message
