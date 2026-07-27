"""Explicit, non-destructive import of an older AI_CAM SQLite database."""
from __future__ import annotations

import argparse
from contextlib import closing
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import DB_PATH
from backend.database import db
from backend.database import ocr_v121_db
from tools.migrate_session_id import migrate as migrate_session_schema


def inspect_source(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)
    with closing(sqlite3.connect(str(path))) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"source integrity_check failed: {integrity}")
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vin_records'"
        ).fetchone()
        if not table:
            raise RuntimeError("source has no vin_records table")
        rows = int(connection.execute("SELECT COUNT(*) FROM vin_records").fetchone()[0])
    return {"source": str(path), "integrity": integrity, "vin_records": rows}


def import_database(source: Path, destination: Path, *, apply: bool = False,
                    replace_existing: bool = False) -> dict:
    source = source.resolve()
    destination = destination.resolve()
    report = inspect_source(source)
    report["destination"] = str(destination)
    report["apply"] = apply
    if not apply:
        return report
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not replace_existing:
        raise FileExistsError(
            f"destination exists: {destination}; use --replace-existing explicitly"
        )

    temp = destination.with_name(destination.name + ".importing")
    if temp.exists():
        raise FileExistsError(f"stale import temp exists; inspect/remove manually: {temp}")
    backup = None
    try:
        with (
            closing(sqlite3.connect(str(source))) as src,
            closing(sqlite3.connect(str(temp))) as dst,
        ):
            src.backup(dst)
        if migrate_session_schema(temp, dry_run=False) != 0:
            raise RuntimeError("session-integrity migration failed")
        db.set_db_path(temp)
        try:
            db.init_db()
            with closing(sqlite3.connect(str(temp))) as connection:
                ocr_v121_db.migrate(connection)
                state = ocr_v121_db.migration_state(connection)
                rows = int(
                    connection.execute("SELECT COUNT(*) FROM vin_records").fetchone()[0]
                )
        finally:
            db.set_db_path(None)
        if destination.exists():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = destination.with_name(destination.name + f".bak-{stamp}")
            shutil.copy2(destination, backup)
        os.replace(temp, destination)
        report.update(
            {
                "imported": True,
                "vin_records_after": rows,
                "migration_state": state,
                "replaced_backup": str(backup) if backup else None,
            }
        )
        return report
    finally:
        if temp.exists():
            temp.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Copy and migrate an old AI_CAM DB into runtime/data; source is never modified."
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", type=Path, default=DB_PATH)
    parser.add_argument("--yes", action="store_true", help="apply import (default is dry-run)")
    parser.add_argument("--replace-existing", action="store_true")
    args = parser.parse_args()
    try:
        report = import_database(
            args.source,
            args.destination,
            apply=args.yes,
            replace_existing=args.replace_existing,
        )
        import json
        print(json.dumps(report, indent=2, default=str))
        return 0
    except Exception as exc:
        print(f"import failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
