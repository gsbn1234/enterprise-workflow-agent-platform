from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def utc_now_precise() -> str:
    """Return a sortable UTC timestamp precise enough for concurrent task traces."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def json_loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def compact_text(text: str, limit: int = 300) -> str:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


# USD per token, applied to whatever count is being priced. Kept in one place
# so the estimated figure and the provider-reported figure stay comparable:
# Phase 5-1 changed where the *count* comes from, not what a token costs.
COST_PER_TOKEN = 0.000002


def estimate_token_cost(text: str) -> float:
    # A deterministic local estimate for portfolio demos. Replace with provider usage
    # when a real LLM gateway is wired in.
    approx_tokens = max(1, len(text) // 4)
    return round(approx_tokens * COST_PER_TOKEN, 6)


def token_cost(total_tokens: int | None) -> float | None:
    """Price a token count the provider actually reported.

    Returns ``None`` rather than ``0.0`` when the count is missing, so a caller
    that wants to fall back can tell "the provider said zero tokens" from "the
    provider said nothing" — the same distinction ``llm_calls.usage_available``
    exists to preserve.
    """
    if total_tokens is None:
        return None
    return round(max(0, int(total_tokens)) * COST_PER_TOKEN, 6)
