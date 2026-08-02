from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skyledger.adsb import normalize_aircraft
from skyledger.db import Database


class DatabaseIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tempdir.name) / "skyledger.db"))
        self.db.init()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def snapshot(self, raw: dict, received_at: str = "2026-08-02T12:00:00Z"):
        result = normalize_aircraft(raw, 41.0, -87.0, received_at)
        self.assertIsNotNone(result)
        return result

    def test_callsign_only_snapshot_has_transient_key_and_is_not_persisted(self) -> None:
        snapshot = self.snapshot({"flight": " SKY123 ", "lat": 41.01, "lon": -87.0})
        self.assertEqual(snapshot.key, "sky123")
        self.assertEqual(snapshot.hex, "")

        self.db.record_raw_positions([snapshot], retention_days=7)
        context = self.db.record_flyover(snapshot, "reveal", False, True)

        self.assertFalse(context["is_new_aircraft"])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM aircraft").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM received_aircraft").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM raw_positions").fetchone()[0], 0)

    def test_registration_is_inserted_updated_and_not_erased(self) -> None:
        first = self.snapshot({"hex": "ABC123", "r": "N123AB"})
        self.db.record_flyover(first, "reveal", False, True)
        self.assertEqual(self.db.get_aircraft_record("abc123")["registration"], "N123AB")

        updated = self.snapshot({"hex": "ABC123", "r": "N999ZZ"})
        self.db.record_flyover(updated, "reveal", False, True)
        missing = self.snapshot({"hex": "ABC123"})
        self.db.record_flyover(missing, "reveal", False, True)
        self.assertEqual(self.db.get_aircraft_record("abc123")["registration"], "N999ZZ")

    def test_total_positions_only_counts_actual_positions(self) -> None:
        without = self.snapshot({"hex": "ABC123", "flight": "SKY1"})
        with_position = self.snapshot({"hex": "ABC123", "lat": 41.01, "lon": -87.0})
        self.db.record_raw_positions([without, with_position], retention_days=7)
        with self.db.connect() as conn:
            row = conn.execute("SELECT total_positions FROM received_aircraft").fetchone()
        self.assertEqual(row["total_positions"], 1)

    def test_retention_uses_iso_utc_boundary(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        expired = self.snapshot(
            {"hex": "ABC001", "lat": 41.01, "lon": -87.0},
            (now - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        current = self.snapshot(
            {"hex": "ABC002", "lat": 41.01, "lon": -87.0},
            (now - timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        self.db.record_raw_positions([expired, current], retention_days=7)
        with self.db.connect() as conn:
            rows = conn.execute("SELECT hex FROM raw_positions").fetchall()
        self.assertEqual([row["hex"] for row in rows], ["abc002"])

    def test_concurrent_first_sightings_are_serialized(self) -> None:
        snapshot = self.snapshot({"hex": "ABC123", "r": "N123AB"})
        barrier = threading.Barrier(3)
        results: list[dict] = []
        errors: list[BaseException] = []

        def record() -> None:
            barrier.wait()
            try:
                results.append(self.db.record_flyover(snapshot, "reveal", False, True))
            except BaseException as exc:  # pragma: no cover - assertion captures worker failures
                errors.append(exc)

        threads = [threading.Thread(target=record) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(sum(result["is_new_aircraft"] for result in results), 1)
        record = self.db.get_aircraft_record("abc123")
        self.assertEqual(record["total_sightings"], 2)


if __name__ == "__main__":
    unittest.main()
