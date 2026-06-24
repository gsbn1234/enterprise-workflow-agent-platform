from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "queue_smoke_test.sqlite3")
os.environ["AGENT_QUEUE_BACKEND"] = "redis"
os.environ["AGENT_REDIS_URL"] = "redis://queue-smoke.local/0"
os.environ["AGENT_REDIS_QUEUE_NAME"] = "agent:test:workflow_jobs"
os.environ["AGENT_REDIS_BLOCK_TIMEOUT_SECONDS"] = "0"
sys.path.insert(0, str(ROOT))

from app.db import reset_database  # noqa: E402
from app.services import queue as queue_module  # noqa: E402
from app.services.jobs import create_workflow_job, get_workflow_job, process_next_job  # noqa: E402


class FakeRedis:
    def __init__(self) -> None:
        self.items: list[str] = []

    def rpush(self, queue_name: str, job_id: str) -> int:
        self.items.append(job_id)
        return len(self.items)

    def blpop(self, queue_name: str, timeout: int = 0):
        if not self.items:
            return None
        return queue_name, self.items.pop(0).encode("utf-8")

    def ping(self) -> bool:
        return True

    def llen(self, queue_name: str) -> int:
        return len(self.items)


def main() -> None:
    fake_redis = FakeRedis()
    queue_module.redis = object()
    queue_module._redis_client = lambda: fake_redis

    reset_database(seed=True)
    job = create_workflow_job(
        "客户 Orbit Retail 投诉上月服务中断，要求退费 800 元，请创建工单并准备回复 support@orbit.example",
        requester_user_id="queue-smoke",
        requester_department="QA",
    )
    assert job["status"] == "queued", job
    assert fake_redis.items == [job["id"]], fake_redis.items

    status = queue_module.queue_status(check_connection=True)
    assert status["backend"] == "redis", status
    assert status["reachable"] is True, status
    assert status["queued_signal_count"] == 1, status

    processed = process_next_job(worker_id="queue-smoke-worker")
    assert processed and processed["status"] == "completed", processed
    assert processed["run_id"], processed
    assert fake_redis.items == [], fake_redis.items

    loaded = get_workflow_job(job["id"])
    assert loaded and loaded["status"] == "completed", loaded
    print("queue_smoke_test passed")
    print(f"job_id={loaded['id']}")
    print(f"run_id={loaded['run_id']}")


if __name__ == "__main__":
    main()
