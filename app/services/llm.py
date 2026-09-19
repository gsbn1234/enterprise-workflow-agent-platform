"""The LLM client, and the outcome type that keeps its failures tellable apart.

Phase 5-1 changed one thing about this module and left everything else alone:
it stopped answering ``None`` to every question. Before, a caller could not tell
"the feature is switched off" from "the provider timed out" from "the model
answered in prose instead of JSON" — all three arrived as the same ``None``, and
audit rows written from that ``None`` described a deterministic decision that had
never been made. :class:`LlmOutcome` carries six statuses instead of one, and
:func:`call` is the single entry point that produces them.

Three deliberate choices:

* **``except LLMError`` still catches everything.** The new subclasses narrow the
  failure; they do not replace the contract, so ``scripts/llm_smoke_test.py`` and
  every existing caller keep working untouched.
* **Only transport failures are retried.** A timeout or a 5xx is worth one more
  round trip; a malformed answer at ``temperature=0`` usually reproduces itself,
  so retrying it buys a second bill rather than a different answer. The fallback
  is the deterministic path, which is free.
* **Token counts are reported, never invented.** A provider that returns no
  ``usage`` block yields ``usage_available=False`` and ``None`` totals. A zero
  would be a lie that a cost report would later believe.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.config import settings
from app.utils import compact_text


logger = logging.getLogger("agent_platform.llm")

QWEN_OPENAI_COMPATIBLE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# --- The six outcomes --------------------------------------------------------
STATUS_DISABLED = "disabled"
STATUS_SUCCESS = "success"
STATUS_TIMEOUT = "timeout"
STATUS_PROVIDER_ERROR = "provider_error"
STATUS_INVALID_OUTPUT = "invalid_output"
STATUS_PARSE_ERROR = "parse_error"

LLM_STATUSES: tuple[str, ...] = (
    STATUS_DISABLED,
    STATUS_SUCCESS,
    STATUS_TIMEOUT,
    STATUS_PROVIDER_ERROR,
    STATUS_INVALID_OUTPUT,
    STATUS_PARSE_ERROR,
)

# Everything that is not a success. ``disabled`` is deliberately excluded: an
# LLM that is switched off has not failed at anything.
FAILURE_STATUSES: tuple[str, ...] = tuple(
    status for status in LLM_STATUSES if status not in {STATUS_DISABLED, STATUS_SUCCESS}
)


class LLMError(RuntimeError):
    """Base class for every LLM failure, so ``except LLMError`` still catches all.

    ``status`` and ``retryable`` are read by :func:`call`. ``error_type`` is the
    coarse machine-readable label that lands in the telemetry row — a ``status``
    says *what kind* of failure it was, an ``error_type`` says *which one*.
    """

    status: str = STATUS_PROVIDER_ERROR
    retryable: bool = False
    error_type: str = "provider_error"

    def __init__(
        self,
        message: str,
        *,
        error_type: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        if error_type is not None:
            self.error_type = error_type
        if retryable is not None:
            self.retryable = retryable


class LLMDisabledError(LLMError):
    status = STATUS_DISABLED
    error_type = "disabled"


class LLMTimeoutError(LLMError):
    status = STATUS_TIMEOUT
    error_type = "timeout"
    retryable = True


class LLMProviderError(LLMError):
    status = STATUS_PROVIDER_ERROR
    error_type = "provider_error"


class LLMInvalidOutputError(LLMError):
    """The provider answered, but not in the shape the caller asked for."""

    status = STATUS_INVALID_OUTPUT
    error_type = "schema_violation"


class LLMParseError(LLMError):
    """The provider answered with something that is not JSON at all."""

    status = STATUS_PARSE_ERROR
    error_type = "not_json"


@dataclass(frozen=True)
class LlmOutcome:
    """What one LLM call produced, whether or not it worked.

    ``value`` is ``None`` on every failure status, which is the only place the
    old behaviour survives — but it is now readable *next to* the reason rather
    than instead of it.
    """

    status: str
    operation: str
    provider: str = ""
    model: str = ""
    value: Any = None
    latency_ms: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    usage_available: bool = False
    error_type: str | None = None
    error_message: str | None = None
    retry_count: int = 0
    fallback_used: bool = False
    schema: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS

    @property
    def disabled(self) -> bool:
        return self.status == STATUS_DISABLED

    @property
    def failed(self) -> bool:
        return self.status in FAILURE_STATUSES

    def to_audit(self) -> dict[str, Any]:
        """The shape that goes into a node payload or an audit detail block."""
        return {
            "llm_operation": self.operation,
            "llm_status": self.status,
            "llm_provider": self.provider,
            "llm_model": self.model,
            "llm_latency_ms": self.latency_ms,
            "llm_retry_count": self.retry_count,
            "llm_fallback_used": self.fallback_used,
            "llm_error_type": self.error_type,
            "llm_error_message": self.error_message,
            "llm_usage_available": self.usage_available,
            "llm_prompt_tokens": self.prompt_tokens,
            "llm_completion_tokens": self.completion_tokens,
            "llm_total_tokens": self.total_tokens,
            "llm_schema": self.schema,
        }


def llm_status() -> dict:
    base_url = _base_url()
    return {
        "enabled": settings.llm_enabled,
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "base_url_configured": bool(base_url),
        "api_key_configured": bool(settings.llm_api_key),
        "api_key_required": _api_key_required(),
        "planner_enabled": settings.llm_planner_enabled,
        "final_answer_enabled": settings.llm_final_answer_enabled,
        "multi_agent_reasoning_enabled": settings.llm_multi_agent_reasoning_enabled,
        "triage_fallback_enabled": settings.llm_triage_fallback_enabled,
        "triage_min_confidence": settings.llm_triage_min_confidence,
        "runbook_candidates_enabled": settings.llm_runbook_candidates_enabled,
        "max_retries": settings.llm_max_retries,
        "retry_backoff_seconds": settings.llm_retry_backoff_seconds,
        "telemetry_enabled": settings.llm_telemetry_enabled,
        "timeout_seconds": settings.llm_timeout_seconds,
    }


def llm_ready() -> bool:
    credentials_ready = bool(settings.llm_api_key) or not _api_key_required()
    return bool(settings.llm_enabled and settings.llm_model and credentials_ready and _base_url())


def call(
    messages: list[dict[str, str]],
    *,
    operation: str,
    schema: Any = None,
    expect_json: bool = False,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> LlmOutcome:
    """Make one provider call and describe what happened, never raising.

    "Never raising" is about the provider: a call that is switched off, times
    out, is rejected, or answers something unusable all come back as an outcome
    with the reason attached. The single thing that can still propagate is a bug
    in this function itself — see the guard at the end of the retry loop, which
    asserts rather than returning a value a caller would go on to unpack.

    ``schema`` is an optional Pydantic model. When given, the parsed JSON is
    validated against it and a violation comes back as ``invalid_output`` with
    the validator's own message attached — the caller falls back to deterministic
    behaviour instead of crashing mid-graph.
    """
    if not llm_ready():
        return _outcome(
            STATUS_DISABLED,
            operation,
            error_type="disabled",
            error_message=(
                "LLM is disabled or is missing its base URL, model, or required API key."
            ),
            fallback_used=True,
            schema=schema,
        )

    # ``response_format`` is what turns "please answer in JSON" from a request
    # into a constraint. It stays behind a switch because "OpenAI compatible"
    # gateways vary in whether they honour it, and a rejected request would read
    # as a provider outage rather than a configuration mismatch.
    wants_json = bool(expect_json or schema is not None)
    response_format = (
        {"type": "json_object"} if (wants_json and settings.llm_json_mode) else None
    )

    attempts = 1 + max(0, settings.llm_max_retries)
    for attempt in range(attempts):
        started = time.perf_counter()
        try:
            data = _post(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
            )
        except LLMError as exc:
            latency_ms = _elapsed_ms(started)
            if exc.retryable and attempt + 1 < attempts:
                logger.warning(
                    "llm.call_retrying",
                    extra={
                        "event": "llm.call_retrying",
                        "operation": operation,
                        "error_type": exc.error_type,
                        "attempt": attempt + 1,
                    },
                )
                time.sleep(settings.llm_retry_backoff_seconds * (attempt + 1))
                continue
            logger.warning(
                "llm.call_failed",
                extra={
                    "event": "llm.call_failed",
                    "operation": operation,
                    "status": exc.status,
                    "error_type": exc.error_type,
                    "error": str(exc),
                },
            )
            return _outcome(
                exc.status,
                operation,
                latency_ms=latency_ms,
                error_type=exc.error_type,
                error_message=str(exc),
                retry_count=attempt,
                fallback_used=True,
                schema=schema,
            )

        latency_ms = _elapsed_ms(started)
        usage = _usage(data)
        try:
            value = _extract_value(data, schema=schema, expect_json=expect_json or schema is not None)
        except LLMError as exc:
            # Not retried: see the module docstring. The caller's deterministic
            # path is the answer, and it costs nothing.
            logger.warning(
                "llm.call_unusable",
                extra={
                    "event": "llm.call_unusable",
                    "operation": operation,
                    "status": exc.status,
                    "error_type": exc.error_type,
                    "error": str(exc),
                },
            )
            return _outcome(
                exc.status,
                operation,
                latency_ms=latency_ms,
                usage=usage,
                error_type=exc.error_type,
                error_message=str(exc),
                retry_count=attempt,
                fallback_used=True,
                schema=schema,
            )

        return _outcome(
            STATUS_SUCCESS,
            operation,
            value=value,
            latency_ms=latency_ms,
            usage=usage,
            retry_count=attempt,
            schema=schema,
        )

    # Unreachable by construction, and deliberately not a seventh status.
    #
    # A retryable failure on the final iteration cannot ``continue``, so it
    # always returns from inside the loop above, carrying the *underlying*
    # status (``timeout``, ``provider_error``) and ``retry_count == attempts - 1``.
    # Exhaustion is therefore already recorded, as the one thing that
    # distinguishes "we retried and it still failed" from "retrying would not
    # have helped": a non-retryable 4xx, a schema violation and a parse error
    # all leave ``retry_count`` at 0. A separate ``retry_exhausted`` status
    # would restate that fact in a second place, where it could drift.
    #
    # Kept as an explicit raise rather than a bare fall-off so that, if the
    # loop above is ever restructured, this fails loudly instead of returning
    # ``None`` to a caller that unpacks a two-tuple.
    raise AssertionError(  # pragma: no cover
        "llm.call fell out of its retry loop; this is a bug in llm.call, not a provider failure."
    )


def complete_chat(
    messages: list[dict[str, str]],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict | None = None,
) -> dict:
    """One round trip, raising :class:`LLMError` on every failure.

    Kept as the documented low-level entry point. New callers want :func:`call`,
    which returns an outcome instead of raising and can therefore tell the six
    statuses apart.
    """
    return _post(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format=response_format,
    )


def complete_text(
    messages: list[dict[str, str]],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    data = complete_chat(messages, temperature=temperature, max_tokens=max_tokens)
    return _text_from(data)


def complete_json(
    messages: list[dict[str, str]],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    schema: Any = None,
) -> dict:
    """Parse the answer as a JSON object, optionally validating it against ``schema``.

    Without ``schema`` this behaves exactly as it did before Phase 5-1. With one,
    a violation raises :class:`LLMInvalidOutputError` carrying the validator's
    field-level message, and the returned dict is the *validated* model dump.
    """
    text = complete_text(messages, temperature=temperature, max_tokens=max_tokens)
    parsed = _parse_json_object(text)
    if not isinstance(parsed, dict):
        raise LLMParseError("LLM response was not a JSON object.", error_type="not_an_object")
    if schema is None:
        return parsed
    return _validate(parsed, schema)


def call_json(
    messages: list[dict[str, str]],
    *,
    operation: str,
    schema: Any = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> LlmOutcome:
    """The non-raising JSON variant of :func:`call`, for callers that want both."""
    return call(
        messages,
        operation=operation,
        schema=schema,
        expect_json=True,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def polish_agent_answer(
    answer: str,
    *,
    status: str,
    category: str | None = None,
    risk_level: str | None = None,
) -> str | None:
    """Reword a finished answer. Kept for callers that only want the text."""
    outcome = polish_agent_answer_outcome(
        answer, status=status, category=category, risk_level=risk_level
    )
    if not outcome.ok:
        return None
    return str(outcome.value).strip() or None


def polish_agent_answer_outcome(
    answer: str,
    *,
    status: str,
    category: str | None = None,
    risk_level: str | None = None,
) -> LlmOutcome:
    """:func:`polish_agent_answer` with the outcome attached.

    The polish step may only rephrase. Every identifier, status word, amount and
    recipient has to survive verbatim, and the prompt says so; what the code
    enforces is that a failure here costs nothing — ``value`` is ``None`` and the
    caller keeps the deterministic answer it already had.
    """
    operation = "final_answer_polish"
    if not settings.llm_final_answer_enabled or not answer.strip() or status in {"failed"}:
        return _outcome(
            STATUS_DISABLED,
            operation,
            error_type="not_applicable",
            error_message="Final-answer polish is disabled or not applicable to this run.",
            fallback_used=True,
        )

    prompt = (
        "You are formatting the final response of an enterprise AI agent.\n"
        "Return concise Chinese Markdown with clear headings, bullet points, and no invented facts.\n"
        "Preserve every ticket id, approval id, email id, external id, status, amount, and recipient exactly as given.\n"
        "Do not claim an action was executed unless the original answer says it was executed."
    )
    user = {
        "status": status,
        "category": category,
        "risk_level": risk_level,
        "raw_answer": answer,
    }
    return call(
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ],
        operation=operation,
        temperature=0.1,
        max_tokens=500,
    )


def _post(
    messages: list[dict[str, str]],
    *,
    temperature: float | None,
    max_tokens: int | None,
    response_format: dict | None,
) -> dict:
    if not llm_ready():
        raise LLMDisabledError(
            "LLM is disabled or is missing its base URL, model, or required API key."
        )

    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": settings.llm_temperature if temperature is None else temperature,
        "max_tokens": settings.llm_max_tokens if max_tokens is None else max_tokens,
    }
    if response_format:
        payload["response_format"] = response_format

    try:
        import httpx
    except ModuleNotFoundError as exc:
        raise LLMProviderError(
            "The httpx package is required for LLM calls. Run pip install -r requirements.txt.",
            error_type="missing_dependency",
        ) from exc

    try:
        with httpx.Client(timeout=settings.llm_timeout_seconds) as client:
            headers = {"Content-Type": "application/json"}
            if settings.llm_api_key:
                headers["Authorization"] = f"Bearer {settings.llm_api_key}"
            response = client.post(f"{_base_url()}/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
    except httpx.TimeoutException as exc:
        raise LLMTimeoutError(
            f"LLM provider timed out after {settings.llm_timeout_seconds}s: {exc}"
        ) from exc
    except httpx.HTTPStatusError as exc:
        detail = compact_text(exc.response.text, 500)
        code = exc.response.status_code
        raise LLMProviderError(
            f"LLM provider returned HTTP {code}: {detail}",
            error_type=f"http_{code}",
            # 5xx and 429 are the provider's problem and may clear; a 4xx is our
            # own malformed request and will not.
            retryable=code >= 500 or code == 429,
        ) from exc
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise LLMProviderError(
            f"LLM provider request failed: {exc}", error_type="transport", retryable=True
        ) from exc

    if not isinstance(data, dict):
        raise LLMInvalidOutputError("LLM provider returned a non-object response.")
    return data


def _extract_value(data: dict, *, schema: Any, expect_json: bool) -> Any:
    text = _text_from(data)
    if not expect_json:
        return text
    parsed = _parse_json_object(text)
    if not isinstance(parsed, dict):
        raise LLMParseError("LLM response was not a JSON object.", error_type="not_an_object")
    if schema is None:
        return parsed
    return _validate(parsed, schema)


def _validate(parsed: dict, schema: Any) -> dict:
    try:
        model = schema.model_validate(parsed)
    except ValidationError as exc:
        raise LLMInvalidOutputError(
            f"LLM output did not satisfy {getattr(schema, '__name__', 'the schema')}: "
            f"{compact_text(str(exc), 400)}"
        ) from exc
    return model.model_dump()


def _text_from(data: dict) -> str:
    try:
        return str(data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMInvalidOutputError(
            "LLM response did not contain choices[0].message.content.",
            error_type="missing_content",
        ) from exc


def _usage(data: dict) -> dict[str, Any]:
    """Read the provider's ``usage`` block, or report honestly that there is none.

    Both spellings are accepted because "OpenAI compatible" is a family, not a
    spec: most gateways send ``prompt_tokens``/``completion_tokens``, some send
    ``input_tokens``/``output_tokens``.
    """
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return {"usage_available": False}
    prompt = _int_or_none(usage.get("prompt_tokens", usage.get("input_tokens")))
    completion = _int_or_none(usage.get("completion_tokens", usage.get("output_tokens")))
    total = _int_or_none(usage.get("total_tokens"))
    if total is None and (prompt is not None or completion is not None):
        total = (prompt or 0) + (completion or 0)
    available = prompt is not None or completion is not None or total is not None
    return {
        "usage_available": available,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _outcome(
    status: str,
    operation: str,
    *,
    value: Any = None,
    latency_ms: int = 0,
    usage: dict | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
    retry_count: int = 0,
    fallback_used: bool = False,
    schema: Any = None,
) -> LlmOutcome:
    usage = usage or {}
    return LlmOutcome(
        status=status,
        operation=operation,
        provider=settings.llm_provider,
        model=settings.llm_model,
        value=value,
        latency_ms=latency_ms,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        total_tokens=usage.get("total_tokens"),
        usage_available=bool(usage.get("usage_available")),
        error_type=error_type,
        error_message=compact_text(error_message, 500) if error_message else None,
        retry_count=retry_count,
        fallback_used=fallback_used,
        schema=getattr(schema, "__name__", None) if schema is not None else None,
    )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _base_url() -> str:
    if settings.llm_base_url:
        return settings.llm_base_url.rstrip("/")
    if settings.llm_provider == "qwen":
        return QWEN_OPENAI_COMPATIBLE_BASE_URL
    return ""


def _api_key_required() -> bool:
    return settings.llm_provider != "vllm"


def _parse_json_object(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise LLMParseError("LLM response was not parseable as JSON.") from None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            raise LLMParseError("LLM response was not parseable as JSON.") from None
