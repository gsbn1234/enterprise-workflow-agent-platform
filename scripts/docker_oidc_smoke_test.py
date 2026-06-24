from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()), NoRedirectHandler())


def request(opener: urllib.request.OpenerDirector, url: str, *, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], str]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with opener.open(req, timeout=10) as response:
            return response.status, dict(response.headers), response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, dict(exc.headers), body


def wait_for_ready(base_url: str) -> dict:
    deadline = time.time() + 90
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/readiness", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if payload.get("status") == "ok" and payload.get("observability", {}).get("otel"):
                    return payload
        except Exception as exc:
            last_error = str(exc)
        time.sleep(2)
    raise RuntimeError(f"Agent readiness timed out. Last error: {last_error}")


def add_query(url: str, **params: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))


def extract_access_token(html: str) -> str:
    match = re.search(r'sessionStorage\.setItem\("agent_access_token",\s*(".*?")\);', html)
    if not match:
        raise AssertionError(f"OIDC callback did not return a platform access token:\n{html[:600]}")
    return json.loads(match.group(1))


def header(headers: dict[str, str], name: str) -> str | None:
    normalized = name.lower()
    for key, value in headers.items():
        if key.lower() == normalized:
            return value
    return None


def run(base_url: str, sso_user: str) -> dict:
    readiness = wait_for_ready(base_url)
    oidc = readiness.get("observability", {})
    opener = build_opener()

    start_status, start_headers, start_body = request(opener, f"{base_url}/api/auth/oidc/start?next=/admin")
    if start_status != 302:
        raise AssertionError(f"OIDC start expected 302, got {start_status}: {start_body}")
    authorize_url = header(start_headers, "Location")
    if not authorize_url:
        raise AssertionError("OIDC start did not return a Location header.")

    authorize_status, authorize_headers, authorize_body = request(opener, add_query(authorize_url, user=sso_user))
    if authorize_status != 302:
        raise AssertionError(f"Local IdP authorize expected 302, got {authorize_status}: {authorize_body}")
    callback_url = header(authorize_headers, "Location")
    if not callback_url:
        raise AssertionError("Local IdP authorize did not return a callback Location header.")

    callback_status, _, callback_body = request(opener, callback_url)
    if callback_status != 200:
        raise AssertionError(f"OIDC callback expected 200, got {callback_status}: {callback_body}")
    platform_token = extract_access_token(callback_body)

    me_status, _, me_body = request(opener, f"{base_url}/api/auth/me", headers={"Authorization": f"Bearer {platform_token}"})
    if me_status != 200:
        raise AssertionError(f"/api/auth/me expected 200, got {me_status}: {me_body}")
    user = json.loads(me_body)
    if not str(user.get("id", "")).startswith("oidc_"):
        raise AssertionError(f"Expected synchronized OIDC user id, got: {user}")
    expected_role = {"admin": "admin", "manager": "manager", "alice": "employee"}[sso_user]
    if user.get("role") != expected_role:
        raise AssertionError(f"Expected {expected_role} role for local SSO {sso_user}, got: {user}")

    return {
        "readiness": readiness,
        "observability": oidc,
        "sso_user": sso_user,
        "platform_user": user,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test the Docker local OIDC browser login flow.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--sso-user", default="admin", choices=["admin", "manager", "alice"])
    args = parser.parse_args()
    result = run(args.base_url.rstrip("/"), args.sso_user)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
