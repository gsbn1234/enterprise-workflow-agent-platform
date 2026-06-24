from __future__ import annotations

import base64
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import jwt
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jwt.utils import base64url_encode

try:
    from cryptography.hazmat.primitives.asymmetric import rsa
except ModuleNotFoundError as exc:  # pragma: no cover - dependency is installed through PyJWT[crypto]
    raise RuntimeError("cryptography is required for the local OIDC provider.") from exc


ISSUER = os.getenv("LOCAL_IDP_ISSUER", "http://127.0.0.1:8030").rstrip("/")
AUDIENCE = os.getenv("LOCAL_IDP_AUDIENCE", "agent-platform")
CLIENT_ID = os.getenv("LOCAL_IDP_CLIENT_ID", "agent-platform")
CLIENT_SECRET = os.getenv("LOCAL_IDP_CLIENT_SECRET", "local-oidc-demo-client-secret")
REDIRECT_URI = os.getenv("LOCAL_IDP_REDIRECT_URI", "http://127.0.0.1:8010/api/auth/oidc/callback")
KEY_ID = os.getenv("LOCAL_IDP_KEY_ID", "local-demo-key")
CODE_TTL_SECONDS = int(os.getenv("LOCAL_IDP_CODE_TTL_SECONDS", "180"))


@dataclass
class AuthorizationCode:
    code: str
    subject: str
    nonce: str | None
    redirect_uri: str
    issued_at: int
    expires_at: int


app = FastAPI(title="Local Demo OIDC Provider")
_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_codes: dict[str, AuthorizationCode] = {}


DEMO_USERS: dict[str, dict[str, Any]] = {
    "admin": {
        "sub": "local-sso-admin",
        "name": "Local SSO Admin",
        "email": "admin.sso@example.test",
        "department": "Platform",
        "groups": ["agent-admins"],
        "tenant_id": "hr-demo",
    },
    "manager": {
        "sub": "local-sso-manager",
        "name": "Local SSO Manager",
        "email": "manager.sso@example.test",
        "department": "Operations",
        "groups": ["agent-approvers"],
        "tenant_id": "hr-demo",
    },
    "alice": {
        "sub": "local-sso-alice",
        "name": "Local SSO Alice",
        "email": "alice.sso@example.test",
        "department": "Business Ops",
        "groups": [],
        "tenant_id": "hr-demo",
    },
}


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "jwks_uri": f"{ISSUER}/oauth2/v1/jwks",
        "user_count": len(DEMO_USERS),
    }


@app.get("/.well-known/openid-configuration")
def openid_configuration() -> dict[str, Any]:
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/oauth2/v1/authorize",
        "token_endpoint": f"{ISSUER}/oauth2/v1/token",
        "jwks_uri": f"{ISSUER}/oauth2/v1/jwks",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid", "profile", "email"],
        "claims_supported": ["sub", "iss", "aud", "exp", "iat", "nonce", "name", "email", "department", "groups", "tenant_id"],
    }


@app.get("/oauth2/v1/jwks")
def jwks() -> dict[str, Any]:
    public_numbers = _private_key.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "kid": KEY_ID,
                "alg": "RS256",
                "n": _int_to_b64(public_numbers.n),
                "e": _int_to_b64(public_numbers.e),
            }
        ]
    }


@app.get("/oauth2/v1/authorize")
def authorize(
    request: Request,
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    scope: str = Query("openid"),
    state: str = Query(...),
    nonce: str | None = Query(default=None),
    user: str | None = Query(default=None),
):
    _validate_authorize_request(response_type, client_id, redirect_uri, scope)
    if user:
        return _issue_code_redirect(user, redirect_uri, state, nonce)
    return HTMLResponse(_authorize_page(request.url.path, dict(request.query_params)))


@app.post("/oauth2/v1/authorize")
def authorize_form(
    response_type: str = Form(...),
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    scope: str = Form("openid"),
    state: str = Form(...),
    nonce: str | None = Form(default=None),
    user: str = Form(...),
) -> RedirectResponse:
    _validate_authorize_request(response_type, client_id, redirect_uri, scope)
    return _issue_code_redirect(user, redirect_uri, state, nonce)


@app.post("/oauth2/v1/token")
def token(
    grant_type: str = Form(...),
    code: str = Form(...),
    redirect_uri: str = Form(...),
    client_id: str = Form(...),
    client_secret: str | None = Form(default=None),
) -> JSONResponse:
    if grant_type != "authorization_code":
        raise HTTPException(status_code=400, detail="unsupported_grant_type")
    if client_id != CLIENT_ID or client_secret != CLIENT_SECRET:
        raise HTTPException(status_code=401, detail="invalid_client")
    code_record = _codes.pop(code, None)
    if not code_record or code_record.expires_at < int(time.time()):
        raise HTTPException(status_code=400, detail="invalid_grant")
    if code_record.redirect_uri != redirect_uri:
        raise HTTPException(status_code=400, detail="redirect_uri_mismatch")
    user = DEMO_USERS.get(code_record.subject)
    if not user:
        raise HTTPException(status_code=400, detail="unknown_subject")
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": user["sub"],
        "iat": now,
        "exp": now + 3600,
        "nonce": code_record.nonce,
        "name": user["name"],
        "email": user["email"],
        "department": user["department"],
        "groups": user["groups"],
        "tenant_id": user["tenant_id"],
    }
    id_token = jwt.encode(claims, _private_key, algorithm="RS256", headers={"kid": KEY_ID})
    access_token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": "local-idp-resource",
            "sub": user["sub"],
            "iat": now,
            "exp": now + 3600,
            "scope": "openid profile email",
        },
        _private_key,
        algorithm="RS256",
        headers={"kid": KEY_ID},
    )
    return JSONResponse(
        {
            "token_type": "Bearer",
            "expires_in": 3600,
            "access_token": access_token,
            "id_token": id_token,
        }
    )


def _validate_authorize_request(response_type: str, client_id: str, redirect_uri: str, scope: str) -> None:
    if response_type != "code":
        raise HTTPException(status_code=400, detail="unsupported_response_type")
    if client_id != CLIENT_ID:
        raise HTTPException(status_code=400, detail="invalid_client_id")
    if redirect_uri != REDIRECT_URI:
        raise HTTPException(status_code=400, detail="redirect_uri_not_allowed")
    if "openid" not in scope.split():
        raise HTTPException(status_code=400, detail="openid_scope_required")


def _issue_code_redirect(user: str, redirect_uri: str, state: str, nonce: str | None) -> RedirectResponse:
    if user not in DEMO_USERS:
        raise HTTPException(status_code=400, detail="unknown_user")
    now = int(time.time())
    _prune_codes(now)
    code = secrets.token_urlsafe(32)
    _codes[code] = AuthorizationCode(
        code=code,
        subject=user,
        nonce=nonce,
        redirect_uri=redirect_uri,
        issued_at=now,
        expires_at=now + CODE_TTL_SECONDS,
    )
    return RedirectResponse(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}", status_code=302)


def _authorize_page(path: str, query: dict[str, str]) -> str:
    hidden = "\n".join(
        f'<input type="hidden" name="{_html_escape(key)}" value="{_html_escape(value)}" />'
        for key, value in query.items()
        if key != "user"
    )
    user_cards = "\n".join(
        f"""
        <button class="user-card" type="submit" name="user" value="{_html_escape(key)}">
          <span>{_html_escape(profile["name"])}</span>
          <strong>{_html_escape(profile["department"])}</strong>
          <small>{_html_escape(", ".join(profile["groups"]) or "employee")}</small>
        </button>
        """
        for key, profile in DEMO_USERS.items()
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Local Enterprise SSO</title>
  <style>
    body {{ margin: 0; font-family: Inter, Arial, sans-serif; background: #f4f7f8; color: #10232f; }}
    main {{ min-height: 100vh; display: grid; place-items: center; padding: 32px; }}
    section {{ width: min(760px, 100%); background: white; border: 1px solid #cfdde5; border-radius: 8px; padding: 28px; box-shadow: 0 16px 45px rgba(20, 55, 70, .12); }}
    h1 {{ margin: 0 0 8px; font-size: 26px; }}
    p {{ margin: 0 0 22px; color: #5b6f7d; }}
    form {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }}
    .user-card {{ min-height: 132px; text-align: left; border: 1px solid #bed1dc; background: #f9fcfd; border-radius: 8px; padding: 16px; cursor: pointer; }}
    .user-card:hover {{ border-color: #087f78; background: #eefafa; }}
    span, strong, small {{ display: block; }}
    span {{ font-size: 17px; font-weight: 700; }}
    strong {{ margin-top: 18px; font-size: 13px; color: #087f78; }}
    small {{ margin-top: 6px; color: #5b6f7d; }}
    @media (max-width: 680px) {{ form {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <main>
    <section>
      <h1>Local Enterprise SSO</h1>
      <p>Select a demo identity to continue to the Agent platform.</p>
      <form method="post" action="{_html_escape(path)}">
        {hidden}
        {user_cards}
      </form>
    </section>
  </main>
</body>
</html>"""


def _prune_codes(now: int) -> None:
    expired = [code for code, record in _codes.items() if record.expires_at < now]
    for code in expired:
        _codes.pop(code, None)


def _int_to_b64(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return base64url_encode(value.to_bytes(length, "big")).decode("ascii")


def _html_escape(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )
