"""Print row counts from a review database without exposing record contents."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "database",
        nargs="?",
        type=Path,
        default=Path("data/review_app/review.sqlite3"),
    )
    args = parser.parse_args()
    connection = sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for table in tables:
            quoted = table.replace('"', '""')
            count = connection.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0]
            print(f"{table}={count}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
