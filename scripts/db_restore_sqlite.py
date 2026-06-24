from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402


def restore_sqlite_backup(backup_path: Path, target_path: Path | None = None, *, confirmed: bool = False) -> dict:
    if not confirmed:
        raise RuntimeError("Refusing to restore without confirmed=True or --yes.")
    if settings.db_backend != "sqlite" and target_path is None:
        raise RuntimeError("Automatic restore only supports SQLite. Provide --target for a SQLite restore target.")

    backup_path = backup_path.resolve()
    target_path = (target_path or settings.db_path).resolve()
    if not backup_path.exists():
        raise FileNotFoundError(f"Backup file not found: {backup_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(backup_path)) as source:
        with closing(sqlite3.connect(target_path)) as target:
            source.backup(target)

    return {"restored": True, "backup_path": str(backup_path), "target_path": str(target_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore a SQLite Agent platform backup.")
    parser.add_argument("backup_path", type=Path)
    parser.add_argument("--target", type=Path, default=None, help="SQLite database path to restore into.")
    parser.add_argument("--yes", action="store_true", help="Confirm destructive restore of the target database.")
    args = parser.parse_args()
    result = restore_sqlite_backup(args.backup_path, args.target, confirmed=args.yes)
    print(f"restored={str(result['restored']).lower()}")
    print(f"backup_path={result['backup_path']}")
    print(f"target_path={result['target_path']}")


if __name__ == "__main__":
    main()
