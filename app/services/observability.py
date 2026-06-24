from __future__ import annotations

import json
import logging
import sys
from contextlib import nullcontext
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from app.config import settings


_log_context: ContextVar[dict[str, Any]] = ContextVar("agent_log_context", default={})
_otel_status: dict[str, Any] = {
    "enabled": settings.otel_enabled,
    "configured": False,
    "exporter": None,
    "error": None,
}


STANDARD_LOG_RECORD_ATTRS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
}


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": settings.service_name,
            "service_version": settings.service_version,
            "environment": settings.app_env,
        }
        payload.update(current_log_context())
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in STANDARD_LOG_RECORD_ATTRS or key in payload:
                continue
            payload[key] = _json_safe(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def configure_observability() -> dict[str, Any]:
    configure_logging()
    configure_opentelemetry()
    return observability_status()


def configure_logging() -> None:
    level = getattr(logging, settings.log_level, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        root.addHandler(logging.StreamHandler(sys.stdout))
    formatter: logging.Formatter
    if settings.log_format == "json":
        formatter = JsonLogFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    for handler in root.handlers:
        handler.setLevel(level)
        handler.setFormatter(formatter)
    for logger_name in ("agent_platform", "uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(logger_name).setLevel(level)


def configure_opentelemetry() -> None:
    global _otel_status
    _otel_status = {
        "enabled": settings.otel_enabled,
        "configured": False,
        "exporter": None,
        "error": None,
    }
    if not settings.otel_enabled:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    except Exception as exc:  # pragma: no cover - optional dependency path
        _otel_status["error"] = f"{type(exc).__name__}: {exc}"
        return

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": settings.service_version,
            "deployment.environment": settings.app_env,
        }
    )
    provider = TracerProvider(resource=resource)
    exporters: list[str] = []
    if settings.otel_exporter_otlp_endpoint:
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=settings.otel_exporter_otlp_endpoint,
                    headers=_parse_headers(settings.otel_exporter_otlp_headers),
                )
            )
        )
        exporters.append("otlp_http")
    if settings.otel_export_console:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        exporters.append("console")
    if not exporters:
        _otel_status["error"] = "AGENT_OTEL_ENABLED=true requires OTEL_EXPORTER_OTLP_ENDPOINT or AGENT_OTEL_EXPORT_CONSOLE=true."
        return
    trace.set_tracer_provider(provider)
    _otel_status.update({"configured": True, "exporter": ",".join(exporters), "error": None})


def observability_status() -> dict[str, Any]:
    return {
        "service_name": settings.service_name,
        "service_version": settings.service_version,
        "log_level": settings.log_level,
        "log_format": settings.log_format,
        "traceparent_enabled": settings.traceparent_enabled,
        "metrics_enabled": settings.metrics_enabled,
        "otel": dict(_otel_status),
    }


def set_log_context(**fields: Any):
    current = dict(_log_context.get())
    current.update({key: value for key, value in fields.items() if value is not None})
    return _log_context.set(current)


def reset_log_context(token) -> None:
    _log_context.reset(token)


def current_log_context() -> dict[str, Any]:
    return dict(_log_context.get())


def start_span(name: str, attributes: dict[str, Any] | None = None):
    if not _otel_status.get("configured"):
        return nullcontext()
    try:
        from opentelemetry import trace
    except Exception:  # pragma: no cover - optional dependency path
        return nullcontext()
    tracer = trace.get_tracer(settings.service_name)
    safe_attributes = {key: _json_safe(value) for key, value in (attributes or {}).items() if value is not None}
    return tracer.start_as_current_span(name, attributes=safe_attributes)


def _parse_headers(raw: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in raw.split(","):
        if not item.strip() or "=" not in item:
            continue
        key, value = item.split("=", 1)
        headers[key.strip()] = value.strip()
    return headers


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)
