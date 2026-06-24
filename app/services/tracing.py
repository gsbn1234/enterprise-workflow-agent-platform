from __future__ import annotations

import re
import secrets
from dataclasses import dataclass


TRACEPARENT_RE = re.compile(r"^(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$")


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    trace_flags: str
    source: str

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags}"


def build_trace_context(traceparent: str | None = None) -> TraceContext:
    parsed = parse_traceparent(traceparent)
    if parsed:
        return TraceContext(
            trace_id=parsed["trace_id"],
            span_id=_random_span_id(),
            parent_span_id=parsed["span_id"],
            trace_flags=parsed["flags"],
            source="incoming",
        )
    return TraceContext(
        trace_id=_random_trace_id(),
        span_id=_random_span_id(),
        parent_span_id=None,
        trace_flags="01",
        source="generated",
    )


def parse_traceparent(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    match = TRACEPARENT_RE.match(value.strip().lower())
    if not match:
        return None
    groups = match.groupdict()
    if groups["version"] == "ff":
        return None
    if _is_all_zero(groups["trace_id"]) or _is_all_zero(groups["span_id"]):
        return None
    return groups


def _random_trace_id() -> str:
    while True:
        value = secrets.token_hex(16)
        if not _is_all_zero(value):
            return value


def _random_span_id() -> str:
    while True:
        value = secrets.token_hex(8)
        if not _is_all_zero(value):
            return value


def _is_all_zero(value: str) -> bool:
    return set(value) <= {"0"}
