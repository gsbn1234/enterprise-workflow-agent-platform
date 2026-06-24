from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from collections.abc import Iterator

from app.config import settings


_CURRENT_TENANT_ID: ContextVar[str | None] = ContextVar("agent_current_tenant_id", default=None)
_RLS_BYPASS: ContextVar[bool] = ContextVar("agent_rls_bypass", default=False)


def normalize_tenant_id(value: str | None = None) -> str:
    raw = (value or settings.default_tenant_id or "default").strip()
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "-" for ch in raw)
    return cleaned.strip("-_") or "default"


def tenant_filter_enabled() -> bool:
    return settings.tenant_isolation_enabled


def effective_tenant_id(value: str | None = None) -> str:
    return normalize_tenant_id(value)


def current_tenant_id() -> str:
    return effective_tenant_id(_CURRENT_TENANT_ID.get())


def current_rls_bypass() -> bool:
    return bool(_RLS_BYPASS.get())


def set_current_tenant_id(value: str | None) -> Token[str | None]:
    return _CURRENT_TENANT_ID.set(effective_tenant_id(value))


def reset_current_tenant_id(token: Token[str | None]) -> None:
    _CURRENT_TENANT_ID.reset(token)


def set_rls_bypass(enabled: bool) -> Token[bool]:
    return _RLS_BYPASS.set(bool(enabled))


def reset_rls_bypass(token: Token[bool]) -> None:
    _RLS_BYPASS.reset(token)


@contextmanager
def tenant_context(tenant_id: str | None) -> Iterator[str]:
    token = set_current_tenant_id(tenant_id)
    try:
        yield current_tenant_id()
    finally:
        reset_current_tenant_id(token)


@contextmanager
def rls_system_context() -> Iterator[None]:
    tenant_token = set_current_tenant_id(settings.default_tenant_id)
    bypass_token = set_rls_bypass(True)
    try:
        yield
    finally:
        reset_rls_bypass(bypass_token)
        reset_current_tenant_id(tenant_token)
