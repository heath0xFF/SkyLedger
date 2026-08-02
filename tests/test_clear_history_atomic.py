from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from skyledger.config import AppConfig
from skyledger.db import Database
from skyledger.discord import DiscordNotifier
from skyledger.tracker import SkyLedgerTracker
from tests.test_core import FakeReader


def test_tracker_clear_history_serializes_against_ticks() -> None:
    async def exercise() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "skyledger.db"))
            db.init()
            tracker = SkyLedgerTracker(
                AppConfig(), db, FakeReader([]), DiscordNotifier("", False)
            )

            await asyncio.gather(tracker.tick(), tracker.clear_history())

            assert db.get_history() == []
            assert tracker.active == {}
            assert tracker.alert_cooldowns == {}

    asyncio.run(exercise())
