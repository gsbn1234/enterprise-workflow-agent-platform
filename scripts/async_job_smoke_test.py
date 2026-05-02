from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "async_job_smoke_test.sqlite3")
sys.path.insert(0, str(ROOT))

from app.db import reset_database  # noqa: E402
from app.services.jobs import create_workflow_job, get_workflow_job, process_next_job  # noqa: E402


def main() -> None:
    reset_database(seed=True)
    job = create_workflow_job(
        "异步烟测：客户 Orbit Retail 投诉服务中断，要求退费 800 元，请创建工单并准备回复 support@orbit.example",
        requester_user_id="async-smoke",
        requester_department="QA",
    )
    assert job["status"] == "queued", job

    processed = process_next_job(worker_id="async-smoke-worker")
    assert processed and processed["status"] == "completed", processed
    assert processed["run_id"], processed

    loaded = get_workflow_job(job["id"])
    assert loaded and loaded["status"] == "completed", loaded
    print("async_job_smoke_test passed")
    print(f"job_id={loaded['id']}")
    print(f"run_id={loaded['run_id']}")


if __name__ == "__main__":
    main()
