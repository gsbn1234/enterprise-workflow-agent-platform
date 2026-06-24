from __future__ import annotations

import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import set_connection_purpose  # noqa: E402
from app.services.observability import configure_observability  # noqa: E402
from app.services.outbox_dispatcher import outbox_dispatcher_loop  # noqa: E402


def main() -> None:
    set_connection_purpose("worker")
    configure_observability()
    asyncio.run(outbox_dispatcher_loop())


if __name__ == "__main__":
    main()
