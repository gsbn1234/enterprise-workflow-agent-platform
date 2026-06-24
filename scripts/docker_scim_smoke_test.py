from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCIM_AGENT_SCHEMA = "urn:agent:params:scim:schemas:extension:workflow:2.0:User"
SCIM_ENTERPRISE_SCHEMA = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"


def read_env_value(key: str) -> str:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def request(method: str, url: str, token: str, payload: dict | None = None) -> tuple[int, dict | str]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else ""
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else ""
        except json.JSONDecodeError:
            payload = raw
        return exc.code, payload


def wait_for_ready(base_url: str) -> dict:
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/readiness", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if payload.get("status") == "ok":
                    return payload
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError("Agent readiness timed out.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test Docker SCIM provisioning.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--token", default="")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    token = args.token or read_env_value("AGENT_SCIM_TOKEN")
    if not token:
        raise SystemExit("AGENT_SCIM_TOKEN is required. Run scripts\\bootstrap_docker_env.py first or pass --token.")

    readiness = wait_for_ready(base_url)
    if readiness.get("operations", {}).get("warnings"):
        raise AssertionError(f"Expected no readiness warnings, got: {readiness['operations']['warnings']}")

    suffix = str(int(time.time()))
    create_status, created = request(
        "POST",
        f"{base_url}/scim/v2/Users",
        token,
        {
            "userName": f"docker.scim.{suffix}@example.test",
            "externalId": f"docker-scim-{suffix}",
            "displayName": "Docker SCIM User",
            "active": True,
            SCIM_ENTERPRISE_SCHEMA: {"department": "IT Access"},
            SCIM_AGENT_SCHEMA: {"role": "manager", "tenant_id": "hr-demo"},
        },
    )
    if create_status != 201:
        raise AssertionError(f"SCIM create failed: {create_status} {created}")
    user_id = created["id"]

    patch_status, patched = request(
        "PATCH",
        f"{base_url}/scim/v2/Users/{user_id}",
        token,
        {"Operations": [{"op": "replace", "path": "active", "value": False}]},
    )
    if patch_status != 200 or patched.get("active") is not False:
        raise AssertionError(f"SCIM patch failed: {patch_status} {patched}")

    delete_status, deleted = request("DELETE", f"{base_url}/scim/v2/Users/{user_id}", token)
    if delete_status != 204:
        raise AssertionError(f"SCIM delete failed: {delete_status} {deleted}")

    print(
        json.dumps(
            {
                "readiness": readiness,
                "scim_user_id": user_id,
                "created_role": created[SCIM_AGENT_SCHEMA]["role"],
                "patched_active": patched["active"],
                "delete_status": delete_status,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
