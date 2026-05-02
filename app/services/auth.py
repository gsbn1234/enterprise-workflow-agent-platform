from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.db import get_connection, row_to_dict, rows_to_dicts
from app.services.audit import record_audit
from app.utils import new_id, utc_now


class AuthError(Exception):
    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    display_name: str
    department: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_approve(self) -> bool:
        return self.role in {"admin", "manager"}


def create_user(
    user_id: str,
    display_name: str,
    department: str,
    role: str,
    password: str,
) -> dict:
    now = utc_now()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users
            (id, display_name, department, role, password_hash, disabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                display_name = excluded.display_name,
                department = excluded.department,
                role = excluded.role,
                password_hash = excluded.password_hash,
                disabled = 0,
                updated_at = excluded.updated_at
            """,
            (user_id, display_name, department, role, hash_password(password), now, now),
        )
    record_audit("user.upsert", "user", user_id, {"role": role, "department": department}, actor="system")
    return public_user(get_user(user_id))


def ensure_demo_users() -> None:
    with get_connection() as conn:
        existing = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
    if existing:
        return
    create_user("admin", "Admin", "Platform", "admin", "AdminPass123")
    create_user("manager", "Ops Manager", "Operations", "manager", "ManagerPass123")
    create_user("alice", "Alice", "Customer Success", "employee", "AlicePass123")


def get_user(user_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return row_to_dict(row)


def list_users(limit: int = 100) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM users
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 500)),),
        ).fetchall()
    return [public_user(user) for user in rows_to_dicts(rows)]


def public_user(user: dict | None) -> dict | None:
    if not user:
        return None
    return {
        "id": user["id"],
        "display_name": user["display_name"],
        "department": user["department"],
        "role": user["role"],
        "disabled": bool(user["disabled"]),
        "created_at": user["created_at"],
        "updated_at": user["updated_at"],
    }


def authenticate_user(user_id: str, password: str) -> dict:
    user = get_user(user_id)
    if not user or user["disabled"]:
        raise AuthError("Invalid user id or password.")
    if not verify_password(password, user["password_hash"]):
        raise AuthError("Invalid user id or password.")
    token, expires_at = create_access_token(user["id"])
    record_audit("auth.login", "user", user["id"], {"role": user["role"]}, actor=user["id"])
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": expires_at,
        "user": public_user(user),
    }


def create_access_token(user_id: str) -> tuple[str, str]:
    expires_at_dt = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": user_id,
        "exp": int(expires_at_dt.timestamp()),
        "nonce": secrets.token_hex(8),
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signature = _sign(body)
    return f"{body}.{signature}", expires_at_dt.replace(microsecond=0).isoformat()


def auth_context_from_authorization(authorization: str | None, *, required: bool) -> AuthContext | None:
    if not authorization:
        if required:
            raise AuthError("Authorization header is required.")
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthError("Bearer token is required.")
    user_id = verify_access_token(token)
    user = get_user(user_id)
    if not user or user["disabled"]:
        raise AuthError("User is disabled or no longer exists.")
    return AuthContext(
        user_id=user["id"],
        display_name=user["display_name"],
        department=user["department"],
        role=user["role"],
    )


def verify_access_token(token: str) -> str:
    body, sep, signature = token.partition(".")
    if not sep or not body or not signature:
        raise AuthError("Invalid token.")
    expected = _sign(body)
    if not hmac.compare_digest(signature, expected):
        raise AuthError("Invalid token signature.")
    try:
        payload = json.loads(_unb64(body).decode("utf-8"))
    except Exception as exc:
        raise AuthError("Invalid token payload.") from exc
    if int(payload.get("exp", 0)) < int(datetime.now(timezone.utc).timestamp()):
        raise AuthError("Token expired.")
    user_id = payload.get("sub")
    if not user_id:
        raise AuthError("Token subject missing.")
    return str(user_id)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    iterations = settings.password_hash_iterations
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt, expected = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations).hex()
    return hmac.compare_digest(digest, expected)


def _sign(body: str) -> str:
    digest = hmac.new(settings.auth_token_secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    return _b64(digest)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
