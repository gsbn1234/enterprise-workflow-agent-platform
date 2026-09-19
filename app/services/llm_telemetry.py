"""Where LLM calls get remembered.

Before Phase 5-1 nothing recorded that a model had been consulted at all: the
only cost figure in the platform was ``estimate_token_cost``, which multiplies
the length of the *final answer text* by four and a made-up per-token price.
That is a demonstration number, and its own docstring says so. This module is
what replaces it with the provider's own counts.

Three rules, all of them about not lying in a report:

* **A disabled call is not a failure and is not recorded.** When the LLM is
  switched off every run would otherwise write a row saying so, and the table
  would fill with the one fact already available from ``llm_status()``. The
  outcome is still returned to the caller, which can record it in its own
  payload — it just does not get a telemetry row.
* **Missing usage is recorded as missing.** ``usage_available`` is written
  explicitly, so a provider that reports nothing is distinguishable from one
  that reports zero. A cost dashboard that treats NULL as 0 will understate
  spend, and it will do so silently.
* **Recording never raises.** Telemetry is an observer. A failure to write a
  usage row must not be able to fail the workflow that produced it, so every
  write is wrapped and downgraded to a log line.
* **A call knows which run it belongs to.** A row with tokens and no run id
  answers "how much did we spend" but not "on what", and the second question is
  the one an operator asks when a bill is wrong. See :func:`llm_context`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from app.config import settings
from app.db import get_connection, row_to_dict, rows_to_dicts
from app.services.llm import LLM_STATUSES, STATUS_DISABLED, LlmOutcome
from app.services.tenancy import effective_tenant_id
from app.utils import new_id, utc_now


logger = logging.getLogger("agent_platform.llm")

# Bounded on purpose: the message is a diagnostic aid, not an archive.
MAX_ERROR_MESSAGE = 500

# The run an LLM call belongs to, when nobody told ``record_llm_call`` directly.
#
# Three of the four write sites pass these ids explicitly, but the sites that
# matter most are the ones that cannot: ``_expert_call`` is reached from six
# expert agents and ``_llm_plan`` from a planner whose signature predates
# multi-agent runs. Neither was given a run id, and threading one down through
# every intermediate would change signatures that have nothing to do with
# telemetry. So the run announces itself once, at its own boundary, and the deep
# call sites inherit it — the same ambient-context shape the platform already
# uses for tenancy (``app/services/tenancy.py``) and for the connection purpose
# (``app/db.py``).
#
# Explicit arguments win over the ambient value, because a workflow step runs
# *inside* a multi-agent run: the nested caller knows more than its host.
_LLM_CONTEXT: ContextVar[dict[str, str]] = ContextVar("agent_llm_context", default={})


def current_llm_context() -> dict[str, str]:
    """The run context in force here, as a copy callers may keep."""
    return dict(_LLM_CONTEXT.get())


@contextmanager
def llm_context(
    *,
    multi_agent_run_id: str | None = None,
    workflow_run_id: str | None = None,
    ticket_id: str | None = None,
) -> Iterator[dict[str, str]]:
    """Announce the run that calls made inside this block belong to.

    Values accumulate rather than replace, and ``None`` means "not known here"
    — it never clears an id an outer block already set. That is what lets the
    two levels coexist: a workflow step running inside a multi-agent run
    records both ids, and neither has to know the other exists.
    """
    merged = current_llm_context()
    for key, value in (
        ("multi_agent_run_id", multi_agent_run_id),
        ("workflow_run_id", workflow_run_id),
        ("ticket_id", ticket_id),
    ):
        if value:
            merged[key] = value
    token = _LLM_CONTEXT.set(merged)
    try:
        yield merged
    finally:
        _LLM_CONTEXT.reset(token)


def record_llm_call(
    outcome: LlmOutcome,
    *,
    tenant_id: str | None = None,
    multi_agent_run_id: str | None = None,
    workflow_run_id: str | None = None,
    ticket_id: str | None = None,
    enabled: bool | None = None,
) -> dict | None:
    """Persist one attempted call. Returns the row, or ``None`` if not recorded.

    An id the caller passed explicitly wins; anything it left out is filled from
    :func:`llm_context`, so a call site that has never heard of a run id still
    lands in the right run.
    """
    if enabled is None:
        enabled = settings.llm_telemetry_enabled
    if not enabled or outcome.status == STATUS_DISABLED:
        return None

    ambient = current_llm_context()
    multi_agent_run_id = multi_agent_run_id or ambient.get("multi_agent_run_id")
    workflow_run_id = workflow_run_id or ambient.get("workflow_run_id")
    ticket_id = ticket_id or ambient.get("ticket_id")

    row = {
        "id": new_id("llm"),
        "created_at": utc_now(),
        "tenant_id": effective_tenant_id(tenant_id),
        "operation": outcome.operation,
        "provider": outcome.provider,
        "model": outcome.model,
        "status": outcome.status,
        "error_type": outcome.error_type,
        "error_message": (outcome.error_message or "")[:MAX_ERROR_MESSAGE] or None,
        "latency_ms": int(outcome.latency_ms),
        "retry_count": int(outcome.retry_count),
        "fallback_used": int(bool(outcome.fallback_used)),
        "usage_available": int(bool(outcome.usage_available)),
        "prompt_tokens": outcome.prompt_tokens,
        "completion_tokens": outcome.completion_tokens,
        "total_tokens": outcome.total_tokens,
        "schema_name": outcome.schema,
        "multi_agent_run_id": multi_agent_run_id,
        "workflow_run_id": workflow_run_id,
        "ticket_id": ticket_id,
    }
    try:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO llm_calls
                (id, created_at, tenant_id, operation, provider, model, status, error_type,
                 error_message, latency_ms, retry_count, fallback_used, usage_available,
                 prompt_tokens, completion_tokens, total_tokens, schema_name,
                 multi_agent_run_id, workflow_run_id, ticket_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"], row["created_at"], row["tenant_id"], row["operation"],
                    row["provider"], row["model"], row["status"], row["error_type"],
                    row["error_message"], row["latency_ms"], row["retry_count"],
                    row["fallback_used"], row["usage_available"], row["prompt_tokens"],
                    row["completion_tokens"], row["total_tokens"], row["schema_name"],
                    row["multi_agent_run_id"], row["workflow_run_id"], row["ticket_id"],
                ),
            )
    except Exception as exc:  # telemetry must never fail the thing it observes
        logger.warning(
            "llm.telemetry_write_failed",
            extra={
                "event": "llm.telemetry_write_failed",
                "operation": outcome.operation,
                "error": str(exc),
            },
        )
        return None
    return row


def llm_usage_summary(tenant_id: str | None = None) -> dict[str, Any]:
    """Aggregate what the models have cost so far, and be honest about gaps.

    ``usage_available_calls`` sits beside ``calls`` for the same reason the
    column exists: a token total computed from a subset of calls is only
    meaningful next to the size of that subset.

    Every count is coalesced to zero. ``SUM`` over no rows is NULL, not 0, so
    without this a platform that has not yet called a model reports
    ``"failed_calls": null`` — and a dashboard that adds that to a running
    total gets a TypeError rather than the zero that is actually true. The
    distinction this function cares about is *unknown usage*, which is what
    ``usage_available_calls`` records; "no calls happened" is not unknown, it
    is empty.
    """
    with get_connection() as conn:
        totals = row_to_dict(
            conn.execute(
                """
                SELECT
                    COUNT(*) AS calls,
                    COALESCE(SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END), 0) AS successful_calls,
                    COALESCE(SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END), 0) AS failed_calls,
                    COALESCE(SUM(CASE WHEN fallback_used = 1 THEN 1 ELSE 0 END), 0) AS fallback_calls,
                    COALESCE(SUM(CASE WHEN usage_available = 1 THEN 1 ELSE 0 END), 0) AS usage_available_calls,
                    COALESCE(SUM(retry_count), 0) AS retries,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                    COALESCE(MAX(latency_ms), 0) AS max_latency_ms
                FROM llm_calls
                WHERE (? IS NULL OR tenant_id = ?)
                """,
                (tenant_id, tenant_id),
            ).fetchone()
        ) or {}
        by_status = rows_to_dicts(
            conn.execute(
                """
                SELECT status, error_type, COUNT(*) AS count, COALESCE(AVG(latency_ms), 0) AS avg_latency_ms
                FROM llm_calls
                WHERE (? IS NULL OR tenant_id = ?)
                GROUP BY status, error_type
                ORDER BY count DESC
                """,
                (tenant_id, tenant_id),
            ).fetchall()
        )
        by_operation = rows_to_dicts(
            conn.execute(
                """
                SELECT operation, model, COUNT(*) AS count,
                       SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END) AS failed_calls,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(AVG(latency_ms), 0) AS avg_latency_ms
                FROM llm_calls
                WHERE (? IS NULL OR tenant_id = ?)
                GROUP BY operation, model
                ORDER BY count DESC
                """,
                (tenant_id, tenant_id),
            ).fetchall()
        )
        # The full cross of the four columns that describe *why* a call ended the
        # way it did. ``by_status`` and ``by_operation`` are the two slices the
        # per-call audit is read with; this is the cross, and it exists so the
        # Prometheus exporter can render its labels from this one definition
        # instead of carrying a second copy of the same aggregation. Two copies
        # are two things to drift.
        #
        # ``error_type`` is coalesced because it doubles as a Prometheus label
        # value, where NULL has no representation.
        by_operation_status = rows_to_dicts(
            conn.execute(
                """
                SELECT operation, model, status, COALESCE(error_type, '') AS error_type,
                       COUNT(*) AS count,
                       COALESCE(AVG(latency_ms), 0) AS avg_latency_ms
                FROM llm_calls
                WHERE (? IS NULL OR tenant_id = ?)
                GROUP BY operation, model, status, COALESCE(error_type, '')
                ORDER BY count DESC
                """,
                (tenant_id, tenant_id),
            ).fetchall()
        )
    return {
        "totals": totals,
        "by_status": by_status,
        "by_operation": by_operation,
        "by_operation_status": by_operation_status,
    }


def llm_call_statuses() -> tuple[str, ...]:
    """Exposed so a caller can validate a status filter without importing llm."""
    return LLM_STATUSES
