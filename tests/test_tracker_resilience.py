from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from skyledger.adsb import SourceStatus
from skyledger.config import AppConfig
from skyledger.db import Database
from skyledger.discord import DiscordNotifier
from skyledger.main import ConnectionManager
from skyledger.tracker import SkyLedgerTracker


class EmptyReader:
    def __init__(self) -> None:
        self.status = SourceStatus(source="test")

    async def read(self) -> list[object]:
        return []


class FakeWebSocket:
    def __init__(self, error: Exception | None = None, delay: float = 0) -> None:
        self.error = error
        self.delay = delay
        self.payloads: list[dict[str, object]] = []

    async def send_json(self, payload: dict[str, object]) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        self.payloads.append(payload)


class TrackerResilienceTests(unittest.IsolatedAsyncioTestCase):
    def make_tracker(self, database_path: str) -> SkyLedgerTracker:
        return SkyLedgerTracker(
            AppConfig(database_path=database_path, poll_interval_seconds=0),
            Database(database_path),
            EmptyReader(),  # type: ignore[arg-type]
            DiscordNotifier("", False),
        )

    async def test_run_recovers_after_tick_error_and_reports_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tracker = self.make_tracker(str(Path(tmp) / "tracker.db"))
            attempts = 0

            async def flaky_tick() -> dict[str, object]:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError("receiver unavailable")
                tracker.stop()
                return {}

            tracker.tick = flaky_tick  # type: ignore[method-assign]
            await tracker.run()

            self.assertEqual(attempts, 2)
            self.assertFalse(tracker.tracker_running)
            self.assertIsNotNone(tracker.tracker_last_success_at)
            self.assertIsNone(tracker.tracker_last_error)
            self.assertEqual(tracker.tracker_consecutive_errors, 0)

            status = tracker.status_payload()
            self.assertFalse(status["tracker_running"])
            self.assertEqual(status["tracker_last_success_at"], tracker.tracker_last_success_at)

    async def test_cancellation_marks_tracker_not_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tracker = self.make_tracker(str(Path(tmp) / "tracker.db"))

            async def blocked_tick() -> dict[str, object]:
                await asyncio.Event().wait()
                return {}

            tracker.tick = blocked_tick  # type: ignore[method-assign]
            task = asyncio.create_task(tracker.run())
            await asyncio.sleep(0)
            self.assertTrue(tracker.tracker_running)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(tracker.tracker_running)

    async def test_broadcast_removes_failed_client_without_blocking_healthy_client(self) -> None:
        manager = ConnectionManager()
        healthy = FakeWebSocket()
        failed = FakeWebSocket(error=ValueError("closed"))
        manager.connections.update({healthy, failed})  # type: ignore[arg-type]

        await manager.broadcast({"ok": True})

        self.assertEqual(healthy.payloads, [{"ok": True}])
        self.assertIn(healthy, manager.connections)
        self.assertNotIn(failed, manager.connections)

    async def test_broadcast_times_out_and_removes_slow_client(self) -> None:
        manager = ConnectionManager()
        manager.SEND_TIMEOUT_SECONDS = 0.01
        slow = FakeWebSocket(delay=1)
        manager.connections.add(slow)  # type: ignore[arg-type]

        await manager.broadcast({"ok": True})

        self.assertNotIn(slow, manager.connections)


if __name__ == "__main__":
    unittest.main()
