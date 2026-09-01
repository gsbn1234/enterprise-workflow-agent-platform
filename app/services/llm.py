from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.config import settings
from app.utils import compact_text


logger = logging.getLogger("agent_platform.llm")

QWEN_OPENAI_COMPATIBLE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class LLMError(RuntimeError):
    pass


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
    }


def llm_ready() -> bool:
    credentials_ready = bool(settings.llm_api_key) or not _api_key_required()
    return bool(settings.llm_enabled and settings.llm_model and credentials_ready and _base_url())


def complete_chat(
    messages: list[dict[str, str]],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict | None = None,
) -> dict:
    if not llm_ready():
        raise LLMError("LLM is disabled or is missing its base URL, model, or required API key.")

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
        raise LLMError("The httpx package is required for LLM calls. Run pip install -r requirements.txt.") from exc

    try:
        with httpx.Client(timeout=settings.llm_timeout_seconds) as client:
            headers = {"Content-Type": "application/json"}
            if settings.llm_api_key:
                headers["Authorization"] = f"Bearer {settings.llm_api_key}"
            response = client.post(f"{_base_url()}/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        detail = compact_text(exc.response.text, 500)
        raise LLMError(f"LLM provider returned HTTP {exc.response.status_code}: {detail}") from exc
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise LLMError(f"LLM provider request failed: {exc}") from exc

    return data


def complete_text(
    messages: list[dict[str, str]],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    data = complete_chat(messages, temperature=temperature, max_tokens=max_tokens)
    try:
        return str(data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("LLM response did not contain choices[0].message.content.") from exc


def complete_json(
    messages: list[dict[str, str]],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict:
    text = complete_text(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    parsed = _parse_json_object(text)
    if not isinstance(parsed, dict):
        raise LLMError("LLM response was not a JSON object.")
    return parsed


def polish_agent_answer(
    answer: str,
    *,
    status: str,
    category: str | None = None,
    risk_level: str | None = None,
) -> str | None:
    if not settings.llm_final_answer_enabled or not llm_ready() or not answer.strip():
        return None
    if status in {"failed"}:
        return None

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
    try:
        polished = complete_text(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            temperature=0.1,
            max_tokens=500,
        )
    except LLMError as exc:
        logger.warning("llm.final_answer_polish_failed", extra={"event": "llm.final_answer_polish_failed", "error": str(exc)})
        return None

    return polished.strip() or None


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
            raise
        return json.loads(match.group(0))
