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
from app.services.tenancy import effective_tenant_id
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
    tenant_id: str

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
    tenant_id: str | None = None,
) -> dict:
    now = utc_now()
    tenant = effective_tenant_id(tenant_id)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users
            (id, display_name, department, role, tenant_id, password_hash, disabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                display_name = excluded.display_name,
                department = excluded.department,
                role = excluded.role,
                tenant_id = excluded.tenant_id,
                password_hash = excluded.password_hash,
                disabled = 0,
                updated_at = excluded.updated_at
            """,
            (user_id, display_name, department, role, tenant, hash_password(password), now, now),
        )
    record_audit("user.upsert", "user", user_id, {"role": role, "department": department}, actor="system", tenant_id=tenant)
    return public_user(get_user(user_id))


def upsert_external_user(
    user_id: str,
    display_name: str,
    department: str,
    role: str,
    *,
    tenant_id: str | None = None,
    disabled: bool = False,
    source: str = "external",
    actor: str = "system",
) -> dict:
    now = utc_now()
    tenant = effective_tenant_id(tenant_id)
    normalized_role = role if role in {"admin", "manager", "employee"} else "employee"
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users
            (id, display_name, department, role, tenant_id, password_hash, disabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                display_name = excluded.display_name,
                department = excluded.department,
                role = excluded.role,
                tenant_id = excluded.tenant_id,
                disabled = excluded.disabled,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                display_name,
                department,
                normalized_role,
                tenant,
                _external_password_hash(source, user_id),
                1 if disabled else 0,
                now,
                now,
            ),
        )
    record_audit(
        "user.external_upsert",
        "user",
        user_id,
        {"source": source, "role": normalized_role, "department": department, "disabled": bool(disabled)},
        actor=actor,
        tenant_id=tenant,
    )
    return public_user(get_user(user_id))


def set_user_disabled(user_id: str, disabled: bool, *, actor: str = "system") -> dict | None:
    now = utc_now()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE users
            SET disabled = ?, updated_at = ?
            WHERE id = ?
            """,
            (1 if disabled else 0, now, user_id),
        )
    user = get_user(user_id)
    if user:
        record_audit(
            "user.disable" if disabled else "user.enable",
            "user",
            user_id,
            {"disabled": bool(disabled)},
            actor=actor,
            tenant_id=user.get("tenant_id"),
        )
    return public_user(user)


def ensure_demo_users() -> None:
    defaults = [
        ("admin", "Admin", "Platform", "admin", "AdminPass123"),
        ("manager", "Ops Manager", "Operations", "manager", "ManagerPass123"),
        ("alice", "Alice", "Customer Success", "employee", "AlicePass123"),
        ("cs_manager", "Customer Success Manager", "Customer Success", "manager", "ManagerPass123"),
        ("finance_manager", "Finance Manager", "Finance", "manager", "ManagerPass123"),
        ("security_manager", "Security Lead", "Security", "manager", "ManagerPass123"),
        ("it_manager", "IT Access Manager", "IT Access", "manager", "ManagerPass123"),
        ("people_manager", "People Ops Manager", "People Ops", "manager", "ManagerPass123"),
        ("procurement_manager", "Procurement Manager", "Procurement", "manager", "ManagerPass123"),
        ("sre_manager", "Incident Commander", "SRE", "manager", "ManagerPass123"),
    ]
    for user_id, display_name, department, role, password in defaults:
        if not get_user(user_id):
            create_user(user_id, display_name, department, role, password)


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
        "tenant_id": user.get("tenant_id") or effective_tenant_id(),
        "disabled": bool(user["disabled"]),
        "created_at": user["created_at"],
        "updated_at": user["updated_at"],
    }


def authenticate_user(
    user_id: str,
    password: str,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> dict:
    lockout_until = current_lockout_until(user_id)
    if lockout_until:
        _record_login_attempt(
            user_id,
            success=False,
            failure_reason="locked",
            ip_address=ip_address,
            user_agent=user_agent,
            lockout_until=lockout_until,
        )
        raise AuthError(f"Account is temporarily locked until {lockout_until}.", status_code=423)

    user = get_user(user_id)
    if not user or user["disabled"]:
        _record_login_attempt(
            user_id,
            success=False,
            failure_reason="invalid_user",
            ip_address=ip_address,
            user_agent=user_agent,
        )
        raise AuthError("Invalid user id or password.")
    if not verify_password(password, user["password_hash"]):
        lockout_until = _record_failed_password_attempt(
            user["id"],
            ip_address=ip_address,
            user_agent=user_agent,
        )
        if lockout_until:
            raise AuthError(f"Account is temporarily locked until {lockout_until}.", status_code=423)
        raise AuthError("Invalid user id or password.")
    _record_login_attempt(user["id"], success=True, ip_address=ip_address, user_agent=user_agent)
    token, expires_at = create_access_token(user["id"])
    record_audit("auth.login", "user", user["id"], {"role": user["role"]}, actor=user["id"], tenant_id=user.get("tenant_id"))
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": expires_at,
        "user": public_user(user),
    }


def create_access_token(user_id: str) -> tuple[str, str]:
    expires_at_dt = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    token_id = secrets.token_hex(16)
    payload = {
        "sub": user_id,
        "exp": int(expires_at_dt.timestamp()),
        "jti": token_id,
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signature = _sign(body)
    expires_at = expires_at_dt.replace(microsecond=0).isoformat()
    _store_access_session(token_id, user_id, expires_at)
    return f"{body}.{signature}", expires_at


def auth_context_from_authorization(authorization: str | None, *, required: bool) -> AuthContext | None:
    if not authorization:
        if required:
            raise AuthError("Authorization header is required.")
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthError("Bearer token is required.")
    if settings.oidc_enabled and token.count(".") == 2:
        from app.services.oidc import auth_context_from_oidc_token

        return auth_context_from_oidc_token(token)
    user_id = verify_access_token(token)
    user = get_user(user_id)
    if not user or user["disabled"]:
        raise AuthError("User is disabled or no longer exists.")
    return AuthContext(
        user_id=user["id"],
        display_name=user["display_name"],
        department=user["department"],
        role=user["role"],
        tenant_id=user.get("tenant_id") or effective_tenant_id(),
    )


def revoke_authorization_token(token: str, *, actor: str = "system") -> None:
    if settings.oidc_enabled and token.count(".") == 2:
        record_audit("auth.oidc_logout", "oidc_token", None, {"revoked_locally": False}, actor=actor)
        return
    revoke_access_token(token, actor=actor)


def verify_access_token(token: str) -> str:
    payload = _verified_token_payload(token)
    _ensure_session_active(payload)
    return str(payload["sub"])


def revoke_access_token(token: str, *, actor: str = "system") -> None:
    payload = _verified_token_payload(token)
    token_id = payload.get("jti")
    if not token_id:
        raise AuthError("Token id missing.")
    now = utc_now()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE auth_sessions
            SET revoked_at = COALESCE(revoked_at, ?)
            WHERE id = ?
            """,
            (now, str(token_id)),
        )
    user = get_user(str(payload.get("sub") or ""))
    record_audit(
        "auth.logout",
        "auth_session",
        str(token_id),
        {"user_id": payload.get("sub")},
        actor=actor,
        tenant_id=(user or {}).get("tenant_id"),
    )


def _external_password_hash(source: str, user_id: str) -> str:
    digest = hashlib.sha256(f"{source}:{user_id}:{settings.auth_token_secret}".encode("utf-8")).hexdigest()
    return f"external_{source}${digest}"


def current_lockout_until(user_id: str) -> str | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT lockout_until
            FROM auth_login_attempts
            WHERE user_id = ?
              AND lockout_until IS NOT NULL
              AND lockout_until > ?
            ORDER BY lockout_until DESC
            LIMIT 1
            """,
            (user_id, utc_now()),
        ).fetchone()
    if not row:
        return None
    return str(row["lockout_until"])


def list_login_attempts(limit: int = 100, user_id: str | None = None) -> list[dict]:
    with get_connection() as conn:
        if user_id:
            rows = conn.execute(
                """
                SELECT *
                FROM auth_login_attempts
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, max(1, min(limit, 500))),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT *
                FROM auth_login_attempts
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
    return rows_to_dicts(rows)


def auth_security_summary() -> dict[str, Any]:
    now = utc_now()
    with get_connection() as conn:
        failed = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM auth_login_attempts
            WHERE success = 0
            """
        ).fetchone()
        active_lockouts = conn.execute(
            """
            SELECT COUNT(DISTINCT user_id) AS count
            FROM auth_login_attempts
            WHERE lockout_until IS NOT NULL AND lockout_until > ?
            """,
            (now,),
        ).fetchone()
        recent = conn.execute(
            """
            SELECT *
            FROM auth_login_attempts
            ORDER BY created_at DESC, id DESC
            LIMIT 10
            """
        ).fetchall()
    return {
        "failed_login_count": int(failed["count"]) if failed else 0,
        "active_lockout_count": int(active_lockouts["count"]) if active_lockouts else 0,
        "recent_login_attempts": rows_to_dicts(recent),
    }


def _record_failed_password_attempt(
    user_id: str,
    *,
    ip_address: str | None,
    user_agent: str | None,
) -> str | None:
    window_started = (datetime.now(timezone.utc) - timedelta(minutes=settings.login_window_minutes)).isoformat()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM auth_login_attempts
            WHERE user_id = ?
              AND success = 0
              AND failure_reason = 'bad_password'
              AND created_at >= ?
            """,
            (user_id, window_started),
        ).fetchone()
    failed_count = int(row["count"]) if row else 0
    lockout_until = None
    if failed_count + 1 >= max(1, settings.login_max_failures):
        lockout_until = (
            datetime.now(timezone.utc) + timedelta(minutes=settings.login_lockout_minutes)
        ).replace(microsecond=0).isoformat()
    _record_login_attempt(
        user_id,
        success=False,
        failure_reason="bad_password",
        ip_address=ip_address,
        user_agent=user_agent,
        lockout_until=lockout_until,
    )
    if lockout_until:
        user = get_user(user_id)
        record_audit(
            "auth.lockout",
            "user",
            user_id,
            {
                "lockout_until": lockout_until,
                "window_minutes": settings.login_window_minutes,
                "max_failures": settings.login_max_failures,
            },
            actor="system",
            tenant_id=(user or {}).get("tenant_id"),
        )
    return lockout_until


def _record_login_attempt(
    user_id: str,
    *,
    success: bool,
    failure_reason: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    lockout_until: str | None = None,
) -> dict:
    attempt_id = new_id("login")
    now = utc_now()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO auth_login_attempts
            (id, user_id, success, failure_reason, ip_address, user_agent, lockout_until, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                user_id,
                1 if success else 0,
                failure_reason,
                ip_address,
                user_agent[:500] if user_agent else None,
                lockout_until,
                now,
            ),
        )
    event_type = "auth.login_success" if success else "auth.login_failed"
    user = get_user(user_id)
    record_audit(
        event_type,
        "user",
        user_id,
        {"failure_reason": failure_reason, "ip_address": ip_address, "lockout_until": lockout_until},
        actor=user_id if success else "anonymous",
        tenant_id=(user or {}).get("tenant_id"),
    )
    return {
        "id": attempt_id,
        "user_id": user_id,
        "success": success,
        "failure_reason": failure_reason,
        "ip_address": ip_address,
        "lockout_until": lockout_until,
        "created_at": now,
    }


def _verified_token_payload(token: str) -> dict[str, Any]:
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
    if not payload.get("jti"):
        raise AuthError("Token id missing.")
    return payload


def _store_access_session(token_id: str, user_id: str, expires_at: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO auth_sessions (id, user_id, expires_at, revoked_at, created_at)
            VALUES (?, ?, ?, NULL, ?)
            """,
            (token_id, user_id, expires_at, utc_now()),
        )


def _ensure_session_active(payload: dict[str, Any]) -> None:
    token_id = str(payload["jti"])
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM auth_sessions WHERE id = ?", (token_id,)).fetchone()
    session = row_to_dict(row)
    if not session:
        raise AuthError("Token session is no longer active.")
    if session.get("revoked_at"):
        raise AuthError("Token has been revoked.")


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
