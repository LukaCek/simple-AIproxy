"""Consistent, private SimpleAI production snapshots before Docker replacement.

This script intentionally NEVER restores a database while a service is running.
A SQLite backup is transaction-consistent even with concurrent WAL writes.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import uuid


def checked_database(path: Path) -> None:
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30) as db:
        result = db.execute("PRAGMA integrity_check").fetchone()
    if result != ("ok",):
        raise RuntimeError(f"SQLite integrity check failed: {result!r}")


def backup_release(directory: Path, previous_image: str) -> Path:
    """Fail closed on missing source files, corrupt DB, or invalid Docker image ID."""
    import re

    directory = directory.resolve()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", previous_image):
        raise ValueError("Previous Docker image ID must be a complete SHA256")
    for name in (".env", "config.yml", "app.db"):
        if not (directory / name).is_file():
            raise FileNotFoundError(f"Required production file is missing: {name}")

    root = directory / ".release-backups"
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot = root / (timestamp + "-" + uuid.uuid4().hex[:10])
    snapshot.mkdir(mode=0o700)

    try:
        for name in (".env", "config.yml"):
            source = directory / name
            dest = snapshot / name
            shutil.copyfile(source, dest)
            os.chmod(dest, 0o600)

        database = directory / "app.db"
        checked_database(database)
        target = snapshot / "app.db"
        # Backup API protects against torn copies and transient WAL state.
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30) as src:
            with sqlite3.connect(target, timeout=30) as dst:
                src.backup(dst, pages=128, sleep=0.1)
        os.chmod(target, 0o600)
        checked_database(target)

        manifest = snapshot / "metadata.json"
        manifest.write_text(json.dumps({
            "previous_image_id": previous_image,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "sqlite_integrity": "ok",
            "files": [".env", "config.yml", "app.db"],
        }, indent=2) + "\n", encoding="utf-8")
        os.chmod(manifest, 0o600)
        return snapshot
    except BaseException:
        shutil.rmtree(snapshot, ignore_errors=True)
        raise


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python3 backup_release.py PROD_DIRECTORY sha256:IMAGE_ID")
    print(backup_release(Path(sys.argv[1]), sys.argv[2]))
