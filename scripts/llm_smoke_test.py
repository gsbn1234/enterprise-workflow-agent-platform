from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.llm import LLMError, complete_text, llm_ready, llm_status


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test the configured LLM provider.")
    parser.add_argument("--prompt", default="用一句中文回答：Qwen API 已经接入成功了吗？")
    parser.add_argument("--require-call", action="store_true", help="Fail when the LLM is not configured.")
    args = parser.parse_args()

    status = llm_status()
    print("LLM status:")
    for key, value in status.items():
        print(f"  {key}: {value}")

    if not llm_ready():
        message = "LLM is not ready. Set AGENT_LLM_ENABLED=true and AGENT_LLM_API_KEY or DASHSCOPE_API_KEY."
        print(message)
        return 1 if args.require_call else 0

    try:
        answer = complete_text(
            [
                {"role": "system", "content": "You are a concise smoke-test assistant."},
                {"role": "user", "content": args.prompt},
            ],
            temperature=0,
            max_tokens=120,
        )
    except LLMError as exc:
        print(f"LLM call failed: {exc}")
        return 1

    print("LLM response:")
    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
