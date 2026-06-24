from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode
from typing import Any

from app.config import settings
from app.db import get_connection
from app.services.audit import record_audit
from app.services.auth import AuthContext, AuthError, create_access_token, public_user
from app.services.tenancy import effective_tenant_id
from app.utils import utc_now


OIDC_STATE_COOKIE = "agent_oidc_state"
OIDC_STATE_MAX_AGE_SECONDS = 600


def oidc_status() -> dict[str, Any]:
    algorithms = list(settings.oidc_algorithms)
    uses_hs256 = any(algorithm.upper().startswith("HS") for algorithm in algorithms)
    missing = []
    if settings.oidc_enabled:
        if not settings.oidc_issuer:
            missing.append("AGENT_OIDC_ISSUER")
        if not settings.oidc_audience:
            missing.append("AGENT_OIDC_AUDIENCE")
        if uses_hs256:
            if not settings.oidc_hs256_secret:
                missing.append("AGENT_OIDC_HS256_SECRET")
        elif not settings.oidc_jwks_url:
            missing.append("AGENT_OIDC_JWKS_URL")
        if settings.oidc_browser_login_enabled:
            if not settings.oidc_authorization_url:
                missing.append("AGENT_OIDC_AUTHORIZATION_URL")
            if not settings.oidc_token_url:
                missing.append("AGENT_OIDC_TOKEN_URL")
            if not settings.oidc_client_id:
                missing.append("AGENT_OIDC_CLIENT_ID")
    return {
        "enabled": settings.oidc_enabled,
        "browser_login_enabled": settings.oidc_browser_login_enabled,
        "issuer_configured": bool(settings.oidc_issuer),
        "audience_configured": bool(settings.oidc_audience),
        "jwks_url_configured": bool(settings.oidc_jwks_url),
        "authorization_url_configured": bool(settings.oidc_authorization_url),
        "token_url_configured": bool(settings.oidc_token_url),
        "client_id_configured": bool(settings.oidc_client_id),
        "redirect_uri_configured": bool(settings.oidc_redirect_uri),
        "algorithms": algorithms,
        "uses_hs256": uses_hs256,
        "missing": missing,
        "default_role": settings.oidc_default_role,
        "default_department": settings.oidc_default_department,
    }


def build_authorization_redirect(next_path: str | None, request_base_url: str) -> dict[str, Any]:
    _ensure_browser_login_configured()
    nonce = secrets.token_urlsafe(24)
    redirect_uri = _redirect_uri(request_base_url)
    sanitized_next = _sanitize_next_path(next_path)
    state = _encode_state({"nonce": nonce, "next": sanitized_next, "iat": int(time.time())})
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(settings.oidc_scopes),
        "state": state,
        "nonce": nonce,
    }
    return {
        "authorization_url": f"{settings.oidc_authorization_url}?{urlencode(params)}",
        "state_cookie": nonce,
        "state_max_age_seconds": OIDC_STATE_MAX_AGE_SECONDS,
    }


def complete_authorization_code_login(
    *,
    code: str,
    state: str,
    state_cookie: str | None,
    request_base_url: str,
    token_response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _ensure_browser_login_configured()
    if not code:
        raise AuthError("OIDC authorization code is required.")
    state_payload = _decode_state(state)
    if not state_cookie or state_cookie != state_payload.get("nonce"):
        raise AuthError("OIDC state cookie mismatch.")
    issued_at = int(state_payload.get("iat") or 0)
    if issued_at <= 0 or int(time.time()) - issued_at > OIDC_STATE_MAX_AGE_SECONDS:
        raise AuthError("OIDC state expired.")
    tokens = token_response or exchange_authorization_code(code, _redirect_uri(request_base_url))
    id_token = tokens.get("id_token")
    if not id_token:
        raise AuthError("OIDC token response did not include id_token.", status_code=502)
    claims = verify_oidc_token(str(id_token))
    user = sync_oidc_user(claims)
    access_token, expires_at = create_access_token(user["id"])
    record_audit(
        "auth.oidc_browser_login",
        "user",
        user["id"],
        {"issuer": claims.get("iss"), "client_id": settings.oidc_client_id},
        actor=user["id"],
        tenant_id=user.get("tenant_id"),
    )
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_at": expires_at,
        "user": user,
        "next": _sanitize_next_path(str(state_payload.get("next") or "/")),
    }


def exchange_authorization_code(code: str, redirect_uri: str) -> dict[str, Any]:
    try:
        import httpx
    except ModuleNotFoundError as exc:
        raise AuthError("httpx is required for OIDC authorization-code login.", status_code=500) from exc
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": settings.oidc_client_id,
    }
    if settings.oidc_client_secret:
        payload["client_secret"] = settings.oidc_client_secret
    try:
        response = httpx.post(settings.oidc_token_url, data=payload, timeout=10)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        raise AuthError(f"OIDC token exchange failed: {exc}", status_code=502) from exc
    if not isinstance(data, dict):
        raise AuthError("OIDC token endpoint returned an invalid payload.", status_code=502)
    return data


def auth_context_from_oidc_token(token: str) -> AuthContext:
    claims = verify_oidc_token(token)
    user = sync_oidc_user(claims)
    return AuthContext(
        user_id=user["id"],
        display_name=user["display_name"],
        department=user["department"],
        role=user["role"],
        tenant_id=user["tenant_id"],
    )


def verify_oidc_token(token: str) -> dict[str, Any]:
    if not settings.oidc_enabled:
        raise AuthError("OIDC authentication is not enabled.")
    try:
        import jwt
        from jwt import PyJWKClient
        from jwt.exceptions import PyJWTError
    except ModuleNotFoundError as exc:
        raise AuthError("PyJWT[crypto] is required for OIDC authentication.", status_code=500) from exc

    algorithms = [algorithm.upper() for algorithm in settings.oidc_algorithms]
    if not settings.oidc_issuer or not settings.oidc_audience:
        raise AuthError("OIDC issuer and audience must be configured.", status_code=500)

    try:
        if any(algorithm.startswith("HS") for algorithm in algorithms):
            if not settings.oidc_hs256_secret:
                raise AuthError("AGENT_OIDC_HS256_SECRET is required for HS256 OIDC validation.", status_code=500)
            key = settings.oidc_hs256_secret
        else:
            if not settings.oidc_jwks_url:
                raise AuthError("AGENT_OIDC_JWKS_URL is required for asymmetric OIDC validation.", status_code=500)
            key = PyJWKClient(settings.oidc_jwks_url).get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            key=key,
            algorithms=algorithms,
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            options={"require": ["exp", "iat"]},
        )
    except AuthError:
        raise
    except PyJWTError as exc:
        raise AuthError(f"Invalid OIDC token: {exc}") from exc

    subject = _claim(claims, settings.oidc_sub_claim)
    if not subject:
        raise AuthError(f"OIDC token missing subject claim: {settings.oidc_sub_claim}")
    return claims


def sync_oidc_user(claims: dict[str, Any]) -> dict:
    subject = str(_claim(claims, settings.oidc_sub_claim))
    issuer = str(claims.get("iss") or settings.oidc_issuer)
    user_id = _external_user_id(issuer, subject)
    display_name = str(
        _claim(claims, settings.oidc_display_name_claim)
        or _claim(claims, settings.oidc_email_claim)
        or subject
    )
    department = str(_claim(claims, settings.oidc_department_claim) or settings.oidc_default_department or "Operations")
    tenant_id = effective_tenant_id(_claim(claims, settings.oidc_tenant_claim))
    role = _role_from_claims(claims)
    now = utc_now()
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
                disabled = 0,
                updated_at = excluded.updated_at
            """,
            (user_id, display_name, department, role, tenant_id, _external_password_hash(issuer, subject), now, now),
        )
    record_audit(
        "auth.oidc_login",
        "user",
        user_id,
        {
            "issuer": issuer,
            "subject_hash": hashlib.sha256(subject.encode("utf-8")).hexdigest(),
            "role": role,
            "department": department,
            "tenant_id": tenant_id,
        },
        actor=user_id,
        tenant_id=tenant_id,
    )
    user = _get_user(user_id)
    return public_user(user) or {
        "id": user_id,
        "display_name": display_name,
        "department": department,
        "role": role,
        "tenant_id": tenant_id,
        "disabled": False,
        "created_at": now,
        "updated_at": now,
    }


def _get_user(user_id: str) -> dict | None:
    from app.db import row_to_dict

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return row_to_dict(row)


def _ensure_browser_login_configured() -> None:
    if not settings.oidc_enabled:
        raise AuthError("OIDC authentication is not enabled.", status_code=404)
    if not settings.oidc_browser_login_enabled:
        raise AuthError("OIDC browser login is not enabled.", status_code=404)
    missing = oidc_status()["missing"]
    if missing:
        raise AuthError(f"OIDC browser login is not fully configured: {', '.join(missing)}.", status_code=500)


def _redirect_uri(request_base_url: str) -> str:
    if settings.oidc_redirect_uri:
        return settings.oidc_redirect_uri
    base = (settings.public_base_url or request_base_url).rstrip("/")
    return f"{base}/api/auth/oidc/callback"


def _sanitize_next_path(value: str | None) -> str:
    candidate = (value or "/").strip()
    if not candidate.startswith("/") or candidate.startswith("//") or "://" in candidate:
        return "/"
    return candidate


def _encode_state(payload: dict[str, Any]) -> str:
    body = _b64(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    signature = _sign_state(body)
    return f"{body}.{signature}"


def _decode_state(state: str) -> dict[str, Any]:
    body, sep, signature = (state or "").partition(".")
    if not sep or not body or not signature:
        raise AuthError("Invalid OIDC state.")
    if not hmac.compare_digest(signature, _sign_state(body)):
        raise AuthError("Invalid OIDC state signature.")
    try:
        payload = json.loads(_unb64(body).decode("utf-8"))
    except Exception as exc:
        raise AuthError("Invalid OIDC state payload.") from exc
    if not isinstance(payload, dict):
        raise AuthError("Invalid OIDC state payload.")
    return payload


def _sign_state(body: str) -> str:
    digest = hmac.new(settings.auth_token_secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    return _b64(digest)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _claim(claims: dict[str, Any], claim_name: str) -> Any:
    current: Any = claims
    for part in claim_name.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _role_from_claims(claims: dict[str, Any]) -> str:
    explicit = str(_claim(claims, settings.oidc_role_claim) or "").strip().lower()
    if explicit in {"admin", "manager", "employee"}:
        return explicit
    groups = _claim(claims, settings.oidc_groups_claim) or []
    if isinstance(groups, str):
        groups = [groups]
    normalized = {str(group).strip().lower() for group in groups}
    if normalized.intersection({group.lower() for group in settings.oidc_admin_groups}):
        return "admin"
    if normalized.intersection({group.lower() for group in settings.oidc_manager_groups}):
        return "manager"
    default = settings.oidc_default_role.strip().lower()
    return default if default in {"admin", "manager", "employee"} else "employee"


def _external_user_id(issuer: str, subject: str) -> str:
    digest = hashlib.sha256(f"{issuer}:{subject}".encode("utf-8")).hexdigest()[:24]
    return f"oidc_{digest}"


def _external_password_hash(issuer: str, subject: str) -> str:
    digest = hashlib.sha256(f"external:{issuer}:{subject}".encode("utf-8")).hexdigest()
    return f"external_oidc${digest}"
