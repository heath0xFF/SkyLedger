from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from skyledger.adsb import normalize_aircraft
from skyledger.db import SCHEMA_VERSION, Database


def test_init_versions_existing_database() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.db"
        database = Database(str(path))
        database.init()
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_online_backup_contains_committed_wal_data_and_passes_integrity_check() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source_path = Path(tmp) / "ledger.db"
        backup_path = Path(tmp) / "backup" / "ledger.db"
        database = Database(str(source_path))
        database.init()
        snapshot = normalize_aircraft(
            {"hex": "ABC123", "lat": 41.001, "lon": -87.0, "alt_baro": 2200},
            41.0,
            -87.0,
            "2026-08-02T12:00:00Z",
        )
        assert snapshot is not None
        database.record_flyover(snapshot, "reveal", True, True)

        assert database.backup(backup_path) == backup_path.resolve()
        restored = Database(str(backup_path))
        assert restored.quick_check() == "ok"
        assert len(restored.get_history()) == 1


def test_backup_refuses_to_replace_live_database() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database = Database(str(Path(tmp) / "ledger.db"))
        database.init()
        with pytest.raises(ValueError, match="must differ"):
            database.backup(database.path)
