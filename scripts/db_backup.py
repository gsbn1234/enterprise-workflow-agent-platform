from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.db import database_url_for_purpose  # noqa: E402


def create_backup(output_dir: Path | None = None, name: str | None = None) -> dict:
    output_dir = output_dir or (ROOT / "data" / "backups")
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base_name = name or f"agent-{settings.db_backend}-{stamp}"

    if settings.db_backend == "postgres":
        backup_path = output_dir / f"{base_name}.dump"
        _create_postgres_backup(backup_path)
    else:
        backup_path = output_dir / f"{base_name}.sqlite3"
        _create_sqlite_backup(backup_path)

    manifest = {
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "backend": settings.db_backend,
        "postgres_schema": settings.postgres_schema if settings.db_backend == "postgres" else None,
        "source": _safe_source_label(),
        "backup_path": str(backup_path),
        "size_bytes": backup_path.stat().st_size,
        "sha256": _sha256(backup_path),
    }
    manifest_path = output_dir / f"{base_name}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def _create_sqlite_backup(backup_path: Path) -> None:
    if not settings.db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {settings.db_path}")
    if backup_path.exists():
        backup_path.unlink()
    with closing(sqlite3.connect(settings.db_path)) as source:
        with closing(sqlite3.connect(backup_path)) as target:
            source.backup(target)


def _create_postgres_backup(backup_path: Path) -> None:
    database_url = database_url_for_purpose("readonly")
    if not database_url:
        raise RuntimeError("AGENT_DATABASE_URL is required for PostgreSQL backups.")
    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        raise RuntimeError("pg_dump was not found on PATH. Install PostgreSQL client tools before backing up PostgreSQL.")
    command = [
        pg_dump,
        "--dbname",
        database_url,
        "--format",
        "custom",
        "--file",
        str(backup_path),
    ]
    if settings.postgres_schema:
        command.extend(["--schema", settings.postgres_schema])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode:
        raise RuntimeError(f"pg_dump failed: {completed.stderr.strip() or completed.stdout.strip()}")


def _safe_source_label() -> str:
    if settings.db_backend == "postgres":
        return f"postgres schema={settings.postgres_schema}"
    return str(settings.db_path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an Agent platform database backup.")
    parser.add_argument("--output-dir", type=Path, default=None, help="Directory where backup files will be written.")
    parser.add_argument("--name", default=None, help="Backup base filename without extension.")
    args = parser.parse_args()
    manifest = create_backup(output_dir=args.output_dir, name=args.name)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
