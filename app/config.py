from __future__ import annotations

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    app_name: str = os.getenv("AGENT_APP_NAME", "Enterprise Workflow Agent Platform")
    data_dir: Path = BASE_DIR / "data"
    db_path: Path = Path(os.getenv("AGENT_DB_PATH", str(data_dir / "agent_platform.sqlite3")))
    auto_seed: bool = _bool_env("AGENT_AUTO_SEED", True)
    max_steps: int = int(os.getenv("AGENT_MAX_STEPS", "8"))
    approval_amount_threshold: float = float(os.getenv("AGENT_APPROVAL_AMOUNT_THRESHOLD", "500"))
    external_email_requires_approval: bool = _bool_env("AGENT_EXTERNAL_EMAIL_REQUIRES_APPROVAL", True)
    job_poll_interval_seconds: float = float(os.getenv("AGENT_JOB_POLL_INTERVAL_SECONDS", "2"))
    job_max_attempts: int = int(os.getenv("AGENT_JOB_MAX_ATTEMPTS", "3"))
    tool_retry_max_attempts: int = int(os.getenv("AGENT_TOOL_RETRY_MAX_ATTEMPTS", "2"))
    tool_retry_backoff_seconds: float = float(os.getenv("AGENT_TOOL_RETRY_BACKOFF_SECONDS", "0.2"))
    auth_required: bool = _bool_env("AGENT_AUTH_REQUIRED", False)
    auth_token_secret: str = os.getenv("AGENT_AUTH_TOKEN_SECRET", "change-this-local-secret")
    access_token_expire_minutes: int = int(os.getenv("AGENT_ACCESS_TOKEN_EXPIRE_MINUTES", "480"))
    password_hash_iterations: int = int(os.getenv("AGENT_PASSWORD_HASH_ITERATIONS", "260000"))
    rag_base_url: str = os.getenv("KNOWLEDGE_RAG_BASE_URL", "").rstrip("/")


settings = Settings()
if not settings.db_path.is_absolute():
    settings.db_path = BASE_DIR / settings.db_path
settings.db_path.parent.mkdir(parents=True, exist_ok=True)
