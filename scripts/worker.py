from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.services.jobs import run_worker_loop  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id", default=os.getenv("AGENT_WORKER_ID", "worker-1"))
    parser.add_argument("--poll-interval", type=float, default=settings.job_poll_interval_seconds)
    parser.add_argument("--stop-after-idle", type=int, default=None)
    args = parser.parse_args()

    init_db(seed=settings.auto_seed)
    print(
        f"Worker {args.worker_id} started. db={settings.db_path} poll_interval={args.poll_interval}",
        flush=True,
    )
    run_worker_loop(
        worker_id=args.worker_id,
        poll_interval_seconds=args.poll_interval,
        stop_after_idle=args.stop_after_idle,
    )


if __name__ == "__main__":
    main()
