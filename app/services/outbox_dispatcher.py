from __future__ import annotations

import asyncio
import logging

from app.config import settings
from app.services.outbox import dispatch_due_outbox_events


logger = logging.getLogger("agent_platform.outbox_dispatcher")


async def outbox_dispatcher_loop() -> None:
    interval = max(1.0, settings.outbox_dispatch_interval_seconds)
    batch_size = max(1, min(settings.outbox_dispatch_batch_size, 100))
    logger.info("Outbox dispatcher started interval=%ss batch_size=%s", interval, batch_size)
    try:
        while True:
            try:
                result = await asyncio.to_thread(dispatch_due_outbox_events, batch_size)
                if result["attempted"]:
                    logger.info(
                        "Outbox dispatcher attempted=%s completed=%s failed=%s skipped=%s",
                        result["attempted"],
                        result["completed"],
                        result["failed"],
                        result["skipped"],
                    )
            except Exception:
                logger.exception("Outbox dispatcher iteration failed")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("Outbox dispatcher stopped")
        raise
