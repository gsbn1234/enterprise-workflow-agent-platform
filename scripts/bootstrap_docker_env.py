from __future__ import annotations

import argparse
import secrets
from pathlib import Path


DEFAULT_ENV_FILE = ".env"
WEAK_VALUES = {
    "",
    "change-this-local-secret",
    "replace-this-hr-demo-secret",
    "replace-this-rag-demo-secret",
    "replace-this-ticket-token",
}


def _is_weak_secret(value: str, *, min_length: int = 32) -> bool:
    normalized = value.strip()
    return normalized in WEAK_VALUES or "replace" in normalized.lower() or len(normalized) < min_length


def _random_secret() -> str:
    return secrets.token_urlsafe(48)


def _parse_env(lines: list[str]) -> dict[str, tuple[int, str]]:
    values: dict[str, tuple[int, str]] = {}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = (index, value.strip())
    return values


def _set_key(lines: list[str], values: dict[str, tuple[int, str]], key: str, value: str) -> None:
    if key in values:
        index, _ = values[key]
        lines[index] = f"{key}={value}"
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{key}={value}")
    values[key] = (len(lines) - 1, value)


def bootstrap_env(env_file: Path, *, force: bool = False) -> list[str]:
    if env_file.exists():
        text = env_file.read_text(encoding="utf-8")
        lines = text.splitlines()
    else:
        lines = []

    values = _parse_env(lines)
    changed: list[str] = []

    secret_keys = [
        "AGENT_AUTH_TOKEN_SECRET",
        "AGENT_OIDC_CLIENT_SECRET",
        "AGENT_SCIM_TOKEN",
        "TICKET_SERVICE_TOKEN",
        "RAG_AUTH_TOKEN_SECRET",
    ]
    for key in secret_keys:
        current = values.get(key, (-1, ""))[1]
        if force or _is_weak_secret(current):
            _set_key(lines, values, key, _random_secret())
            changed.append(key)

    role_key = "AGENT_POSTGRES_RLS_BYPASS_ROLE"
    if force or not values.get(role_key, (-1, ""))[1].strip():
        _set_key(lines, values, role_key, "agent_rls_bypass")
        changed.append(role_key)

    if changed:
        env_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap local Docker production-like secrets for the Agent stack.")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE, help="Path to the .env file used by docker compose.")
    parser.add_argument("--force", action="store_true", help="Regenerate managed secrets even if strong values already exist.")
    args = parser.parse_args()

    changed = bootstrap_env(Path(args.env_file), force=args.force)
    if changed:
        print("updated_keys=" + ",".join(changed))
    else:
        print("updated_keys=")
        print("env already contains strong Docker demo secrets")


if __name__ == "__main__":
    main()
