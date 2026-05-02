from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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


def estimate_token_cost(text: str) -> float:
    # A deterministic local estimate for portfolio demos. Replace with provider usage
    # when a real LLM gateway is wired in.
    approx_tokens = max(1, len(text) // 4)
    return round(approx_tokens * 0.000002, 6)
