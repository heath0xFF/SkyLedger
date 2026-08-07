from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config
from .db import Database


def main() -> None:
    parser = argparse.ArgumentParser(description="Maintain a SkyLedger SQLite database.")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("backup", help="Create a verified online backup")
    backup_parser.add_argument("destination", nargs="?", help="Output .db path")
    subparsers.add_parser("check", help="Run SQLite quick_check")
    subparsers.add_parser("checkpoint", help="Checkpoint and truncate the WAL file")
    args = parser.parse_args()

    config = load_config(args.config)
    database = Database(config.database_path)

    if args.command == "checkpoint":
        info = database.checkpoint_wal()
        print(f"WAL checkpoint: {info}")
        return

    if args.command == "check":
        result = database.quick_check()
        print(result)
        if result != "ok":
            raise SystemExit(1)
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = args.destination or str(Path("backups") / f"skyledger-{timestamp}.db")
    path = database.backup(destination)
    print(f"Backup created and verified: {path}")


if __name__ == "__main__":
    main()
