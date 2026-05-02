from __future__ import annotations

from dataclasses import dataclass

from app.config import settings


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    backoff_seconds: float
    retryable: bool = True


NON_RETRYABLE_TOOLS = {
    "create_ticket",
    "send_email",
    "request_approval",
}

RETRYABLE_TOOLS = {
    "query_enterprise_rag",
    "search_knowledge",
    "lookup_customer",
    "draft_email",
    "update_ticket",
    "get_document_detail",
    "list_document_versions",
    "search_eval_reports",
    "get_vector_backend_status",
}


def default_retry_policy(tool_name: str | None, action_type: str) -> RetryPolicy:
    if tool_name in NON_RETRYABLE_TOOLS:
        return RetryPolicy(max_attempts=1, backoff_seconds=0, retryable=False)
    if tool_name in RETRYABLE_TOOLS:
        return RetryPolicy(
            max_attempts=max(1, settings.tool_retry_max_attempts),
            backoff_seconds=max(0, settings.tool_retry_backoff_seconds),
            retryable=True,
        )
    if action_type in {"tool_call", "state_update"}:
        return RetryPolicy(
            max_attempts=max(1, settings.tool_retry_max_attempts),
            backoff_seconds=max(0, settings.tool_retry_backoff_seconds),
            retryable=True,
        )
    return RetryPolicy(max_attempts=1, backoff_seconds=0, retryable=False)


def classify_error(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    name = exc.__class__.__name__.lower()
    if "timeout" in name:
        return "timeout"
    if "connection" in name or "network" in name:
        return "network"
    if "permission" in name or "auth" in name:
        return "permission"
    if "value" in name or "validation" in name:
        return "validation"
    return "unexpected"
