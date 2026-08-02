from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from skyledger.adsb import SourceStatus
from skyledger.config import AppConfig
from skyledger.db import Database
from skyledger.discord import DiscordNotifier
from skyledger.tracker import SkyLedgerTracker


class _Reader:
    status = SourceStatus(source="test")

    async def read(self):
        return []


class _SlowDatabase(Database):
    def get_dashboard_data(self, closest_hex=None):
        time.sleep(0.08)
        return super().get_dashboard_data(closest_hex)


class AsyncDatabaseTests(unittest.TestCase):
    def test_distinct_today_query_uses_timestamp_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "ledger.db"))
            db.init()
            start, end = db._day_bounds("2026-08-02")
            with db.connect() as conn:
                plan = conn.execute(
                    "EXPLAIN QUERY PLAN SELECT COUNT(DISTINCT hex) FROM flyover_events "
                    "WHERE seen_at >= ? AND seen_at < ?",
                    (start, end),
                ).fetchall()

        description = " ".join(str(column) for row in plan for column in row).lower()
        self.assertIn("idx_flyover_events_seen_at", description)
        self.assertNotIn("scan flyover_events", description)

    def test_async_payload_builder_does_not_block_event_loop(self) -> None:
        async def exercise() -> int:
            with tempfile.TemporaryDirectory() as tmp:
                db = _SlowDatabase(str(Path(tmp) / "ledger.db"))
                db.init()
                tracker = SkyLedgerTracker(
                    AppConfig(database_path=str(db.path)),
                    db,
                    _Reader(),
                    DiscordNotifier("", False),
                )
                heartbeats = 0

                async def heartbeat() -> None:
                    nonlocal heartbeats
                    while heartbeats < 4:
                        await asyncio.sleep(0.01)
                        heartbeats += 1

                await asyncio.gather(tracker.build_payload_async(), heartbeat())
                return heartbeats

        self.assertEqual(asyncio.run(exercise()), 4)

    def test_summary_cache_is_reused_within_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "ledger.db"))
            db.init()
            first = db.get_summary_stats()
            with db.connect() as conn:
                conn.execute(
                    "INSERT INTO received_aircraft (hex, first_seen, last_seen) VALUES (?, ?, ?)",
                    ("abc123", "2026-08-02T00:00:00Z", "2026-08-02T00:00:00Z"),
                )
            cached = db.get_summary_stats()
            db._summary_cache_at -= db.SUMMARY_CACHE_TTL_SECONDS + 1
            refreshed = db.get_summary_stats()

        self.assertEqual(first["total_aircraft"], 0)
        self.assertEqual(cached["total_aircraft"], 0)
        self.assertEqual(refreshed["total_aircraft"], 1)


if __name__ == "__main__":
    unittest.main()
