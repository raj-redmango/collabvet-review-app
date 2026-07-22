"""Verify that migrated review state matches its backup and uses local inputs."""

from __future__ import annotations

import sqlite3
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "data" / "review_app" / "review.sqlite3"
BACKUP = ROOT / "data" / "review_app" / "review.sqlite3.pre-migration-backup"
CLINICAL_DATA_ROOT = Path(
    os.environ.get("REVIEW_CLINICAL_DATA_ROOT", ROOT.parent / "collabvet-clinical-data")
).resolve()
INPUT_ROOT = (CLINICAL_DATA_ROOT / "cases").resolve()


def counts(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            table: connection.execute(
                f'SELECT COUNT(*) FROM "{table.replace(chr(34), chr(34) * 2)}"'
            ).fetchone()[0]
            for table in tables
        }
    finally:
        connection.close()


def main() -> None:
    live_counts = counts(LIVE)
    backup_counts = counts(BACKUP)
    if live_counts != backup_counts:
        raise SystemExit(f"Database row counts changed: live={live_counts}, backup={backup_counts}")

    connection = sqlite3.connect(f"file:{LIVE.resolve()}?mode=ro", uri=True)
    try:
        paths = [
            Path(row[0]).resolve()
            for row in connection.execute("SELECT source_path FROM case_record")
        ]
    finally:
        connection.close()

    outside = [path for path in paths if not path.is_relative_to(INPUT_ROOT)]
    missing = [path for path in paths if not path.is_file()]
    if outside or missing:
        raise SystemExit(
            f"Invalid case paths: outside_input_root={len(outside)}, missing={len(missing)}"
        )
    print(
        f"verified tables={len(live_counts)} cases={len(paths)} "
        f"input_root={INPUT_ROOT}"
    )


if __name__ == "__main__":
    main()
