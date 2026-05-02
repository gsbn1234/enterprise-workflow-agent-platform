from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "trace_replay_smoke_test.sqlite3")
sys.path.insert(0, str(ROOT))

from app.db import reset_database  # noqa: E402
from app.services.multi_agent import (  # noqa: E402
    diff_trace,
    export_multi_agent_trace,
    replay_multi_agent_run,
    run_multi_agent,
    save_golden_trace,
)


def main() -> None:
    reset_database(seed=True)

    source = run_multi_agent(
        "Customer Orbit Retail reports a billing dispute last month and requests a refund of 800 RMB. "
        "Create an auditable ticket, check policy evidence, and prepare a reply to support@orbit.example.",
        requester_user_id="smoke",
        requester_department="Customer Success",
        requester_role="manager",
    )
    trace = export_multi_agent_trace(source["id"])
    assert trace and trace["tool_sequence"], trace

    golden = save_golden_trace(source["id"], "refund_high_golden")
    golden_diff = diff_trace(source["id"], golden_id=golden["id"])
    assert golden_diff["passed"], golden_diff

    replay = replay_multi_agent_run(source["id"])
    assert replay["status"] == "passed", replay
    assert replay["diff_report"]["passed"], replay

    print("trace_replay_smoke_test passed")
    print(f"source_run={source['id']}")
    print(f"golden_trace={golden['id']}")
    print(f"replay_id={replay['id']}")
    print(f"replay_run={replay['replay_run_id']}")


if __name__ == "__main__":
    main()
