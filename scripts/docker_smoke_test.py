from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request


def request_json(url: str, payload: dict | None = None, token: str | None = None) -> dict:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
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
            payload = request_json(f"{base_url}/api/health")
            if payload.get("status") == "ok":
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

    health = wait_for_health(args.base_url.rstrip("/"), args.timeout)
    token = None
    if args.user_id and args.password:
        login = request_json(
            f"{args.base_url.rstrip('/')}/api/auth/login",
            {"user_id": args.user_id, "password": args.password},
        )
        token = login["access_token"]
    run = request_json(
        f"{args.base_url.rstrip('/')}/api/workflow/run",
        {
            "objective": "Docker smoke test：客户 Orbit Retail 要求退费 800 元，请创建工单并准备回复 support@orbit.example",
            "requester_user_id": "docker-smoke",
            "requester_department": "QA",
        },
        token=token,
    )
    if run.get("status") != "waiting_approval":
        raise AssertionError(run)
    if run.get("category") != "refund":
        raise AssertionError(run)
    job = request_json(
        f"{args.base_url.rstrip('/')}/api/workflow/jobs",
        {
            "objective": "Docker async smoke test：运营团队想购买一个 300 元的数据清洗插件，请登记采购需求并给出处理建议",
            "requester_user_id": "docker-smoke",
            "requester_department": "QA",
        },
        token=token,
    )
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        loaded = request_json(f"{args.base_url.rstrip('/')}/api/jobs/{job['id']}", token=token)
        if loaded.get("status") == "completed":
            job = loaded
            break
        time.sleep(1)
    if job.get("status") != "completed":
        raise AssertionError(job)
    print(
        json.dumps(
            {
                "health": health,
                "sync_run_id": run["id"],
                "sync_status": run["status"],
                "async_job_id": job["id"],
                "async_run_id": job["run_id"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
