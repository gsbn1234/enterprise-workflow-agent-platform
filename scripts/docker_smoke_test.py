from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request


REFUND_OBJECTIVE = (
    "\u5ba2\u6237 Orbit Retail \u6295\u8bc9\u4e0a\u6708\u670d\u52a1\u4e2d\u65ad\uff0c"
    "\u8981\u6c42\u9000\u8d39 800 \u5143\uff0c\u8bf7\u521b\u5efa\u5de5\u5355\u5e76"
    "\u51c6\u5907\u56de\u590d support@orbit.example"
)
LOW_RISK_OBJECTIVE = (
    "\u8bf7\u4e3a\u8fd0\u8425\u56e2\u961f\u521b\u5efa\u4e00\u4e2a"
    "\u666e\u901a\u6d41\u7a0b\u4f18\u5316\u5de5\u5355\uff1a\u6bcf\u5468"
    "\u62a5\u8868\u81ea\u52a8\u5f52\u6863\uff0c\u8bb0\u5f55\u4e3a"
    "\u4f4e\u4f18\u5148\u7ea7\u3002"
)


def request_json(url: str, payload: dict | None = None, token: str | None = None) -> dict | list:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if payload else "GET")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_health(base_url: str, timeout_seconds: int) -> dict:
    deadline = time.time() + timeout_seconds
    last_error = None
    while time.time() < deadline:
        try:
            payload = request_json(f"{base_url}/api/readiness")
            if isinstance(payload, dict) and payload.get("status") == "ok" and payload.get("database", {}).get("status") == "ok":
                return payload
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
        time.sleep(1)
    raise RuntimeError(f"Service did not become healthy: {last_error}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--user-id")
    parser.add_argument("--password")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    health = wait_for_health(base_url, args.timeout)
    token = None
    if args.user_id and args.password:
        login = request_json(f"{base_url}/api/auth/login", {"user_id": args.user_id, "password": args.password})
        if not isinstance(login, dict):
            raise AssertionError(login)
        token = login["access_token"]

    run = request_json(
        f"{base_url}/api/workflow/run",
        {"objective": REFUND_OBJECTIVE, "requester_user_id": "docker-smoke", "requester_department": "QA"},
        token=token,
    )
    if not isinstance(run, dict):
        raise AssertionError(run)
    if run.get("status") != "waiting_approval":
        raise AssertionError(run)
    if run.get("category") != "refund":
        raise AssertionError(run)
    if run.get("ticket_id"):
        raise AssertionError(run)

    job = request_json(
        f"{base_url}/api/workflow/jobs",
        {
            "objective": LOW_RISK_OBJECTIVE,
            "requester_user_id": "docker-smoke",
            "requester_department": "QA",
            "max_attempts": 1,
        },
        token=token,
    )
    if not isinstance(job, dict):
        raise AssertionError(job)
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        loaded = request_json(f"{base_url}/api/jobs/{job['id']}", token=token)
        if not isinstance(loaded, dict):
            raise AssertionError(loaded)
        if loaded.get("status") in {"completed", "failed"}:
            job = loaded
            break
        time.sleep(1)
    if job.get("status") != "completed":
        raise AssertionError(job)

    async_run = request_json(f"{base_url}/api/runs/{job['run_id']}", token=token)
    if not isinstance(async_run, dict):
        raise AssertionError(async_run)
    if async_run.get("status") != "completed":
        raise AssertionError(async_run)
    if not async_run.get("ticket_id"):
        raise AssertionError(async_run)

    print(
        json.dumps(
            {
                "health": health,
                "sync_run_id": run["id"],
                "sync_status": run["status"],
                "async_job_id": job["id"],
                "async_run_id": job["run_id"],
                "async_ticket_id": async_run["ticket_id"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
