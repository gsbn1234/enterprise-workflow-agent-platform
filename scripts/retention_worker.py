from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import set_connection_purpose  # noqa: E402
from app.services.observability import configure_observability  # noqa: E402
from app.services.retention import apply_retention, retention_status  # noqa: E402


logger = logging.getLogger("agent_platform.retention_worker")


def main() -> None:
    parser = argparse.ArgumentParser(description="Periodically apply configured data retention policies.")
    parser.add_argument("--interval-seconds", type=float, default=3600)
    parser.add_argument("--run-once", action="store_true")
    args = parser.parse_args()

    set_connection_purpose("worker")
    configure_observability()
    interval = max(60.0, args.interval_seconds)
    status = retention_status()
    logger.info(
        "Retention worker started interval=%ss enabled=%s enabled_policy_count=%s",
        interval,
        status["enabled"],
        status["enabled_policy_count"],
    )

    while True:
        try:
            result = apply_retention()
            if result["total_deleted"]:
                logger.info("Retention applied total_deleted=%s", result["total_deleted"])
        except Exception:
            logger.exception("Retention worker iteration failed")
        if args.run_once:
            return
        time.sleep(interval)


if __name__ == "__main__":
    main()
