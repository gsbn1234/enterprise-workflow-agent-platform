from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("AGENT_DB_PATH", str(ROOT / "data" / "agent_platform.sqlite3"))
sys.path.insert(0, str(ROOT))

from app.db import init_db  # noqa: E402
from app.services.multi_agent import diff_trace, export_multi_agent_trace, replay_multi_agent_run, save_golden_trace  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Trace replay and golden trace diff utilities.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--run-id", required=True)
    export_parser.add_argument("--include-payloads", action="store_true")

    golden_parser = subparsers.add_parser("save-golden")
    golden_parser.add_argument("--run-id", required=True)
    golden_parser.add_argument("--name", required=True)

    replay_parser = subparsers.add_parser("replay")
    replay_parser.add_argument("--run-id", required=True)

    diff_parser = subparsers.add_parser("diff")
    diff_parser.add_argument("--run-id", required=True)
    diff_group = diff_parser.add_mutually_exclusive_group(required=True)
    diff_group.add_argument("--golden-id")
    diff_group.add_argument("--baseline-run-id")

    args = parser.parse_args()
    init_db(seed=False)

    if args.command == "export":
        result = export_multi_agent_trace(args.run_id, include_payloads=args.include_payloads)
    elif args.command == "save-golden":
        result = save_golden_trace(args.run_id, args.name)
    elif args.command == "replay":
        result = replay_multi_agent_run(args.run_id)
    else:
        result = diff_trace(args.run_id, golden_id=args.golden_id, baseline_run_id=args.baseline_run_id)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
