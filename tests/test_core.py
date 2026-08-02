from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from skyledger.adsb import SourceStatus, normalize_aircraft
from skyledger.config import AppConfig, update_config_file
from skyledger.db import Database
from skyledger.discord import DiscordNotifier
from skyledger.geo import haversine_miles
from skyledger.main import AircraftMarkerColorsRequest, HomeSettingsRequest, create_app
from skyledger.receiver_control import start_windows_receiver
from skyledger.tracker import SkyLedgerTracker


class FakeReader:
    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.status = SourceStatus(source="fake")

    async def read(self):
        self.status.receiver_online = True
        self.status.raw_aircraft_count = len(self.snapshots)
        self.status.normalized_aircraft_count = len(self.snapshots)
        return self.snapshots


class CoreTests(unittest.TestCase):
    @staticmethod
    def current_utc_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_haversine_distance(self) -> None:
        distance = haversine_miles(41.0, -87.0, 41.01, -87.0)
        self.assertGreater(distance, 0.65)
        self.assertLess(distance, 0.75)

    def test_normalize_aircraft(self) -> None:
        snapshot = normalize_aircraft(
            {
                "hex": "A1B2C3",
                "flight": " SKY123 ",
                "r": "N123AB",
                "lat": 41.01,
                "lon": -87.0,
                "alt_baro": "4200",
                "gs": 180.4,
                "track": 92,
                "baro_rate": -128,
                "category": "A3",
            },
            41.0,
            -87.0,
            "2026-06-01T12:00:00Z",
        )
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.hex, "a1b2c3")
        self.assertEqual(snapshot.callsign, "SKY123")
        self.assertEqual(snapshot.registration, "N123AB")
        self.assertEqual(snapshot.altitude_ft, 4200)
        self.assertIsNotNone(snapshot.distance_mi)

    def test_normalize_keeps_zero_values(self) -> None:
        snapshot = normalize_aircraft(
            {
                "hex": "A1B2C4",
                "lat": 41.0,
                "lon": -87.0,
                "alt_baro": 0,
                "gs": 0,
                "track": 0,
            },
            41.0,
            -87.0,
            "2026-06-01T12:00:00Z",
        )
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.altitude_ft, 0)
        self.assertEqual(snapshot.speed_kt, 0)
        self.assertEqual(snapshot.heading, 0)

    def test_normalize_dump1090_windows_fields(self) -> None:
        snapshot = normalize_aircraft(
            {
                "hex": "AC5066",
                "flight": "SWA3688",
                "lat": 10.25,
                "lon": 20.75,
                "altitude": 15625,
                "speed": 415,
                "track": 195,
                "vert_rate": 2432,
            },
            10.0,
            20.0,
            "2026-06-01T12:00:00Z",
        )
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.altitude_ft, 15625)
        self.assertEqual(snapshot.speed_kt, 415)
        self.assertEqual(snapshot.heading, 195)
        self.assertEqual(snapshot.vertical_rate_fpm, 2432)

    def test_tracker_filters_stale_live_aircraft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "skyledger.db"))
            db.init()
            fresh = normalize_aircraft(
                {"hex": "ABC001", "lat": 41.001, "lon": -87.0, "seen": 1, "seen_pos": 1},
                41.0,
                -87.0,
                "2026-06-01T12:00:00Z",
            )
            stale_message = normalize_aircraft(
                {"hex": "ABC002", "lat": 41.002, "lon": -87.0, "seen": 11, "seen_pos": 1},
                41.0,
                -87.0,
                "2026-06-01T12:00:00Z",
            )
            stale_position = normalize_aircraft(
                {"hex": "ABC003", "lat": 41.003, "lon": -87.0, "seen": 1, "seen_pos": 11},
                41.0,
                -87.0,
                "2026-06-01T12:00:00Z",
            )
            assert fresh is not None
            assert stale_message is not None
            assert stale_position is not None

            tracker = SkyLedgerTracker(
                AppConfig(live_aircraft_timeout_seconds=10),
                db,
                FakeReader([fresh, stale_message, stale_position]),
                DiscordNotifier("", False),
            )

            payload = asyncio.run(tracker.tick())

            self.assertEqual([item["hex"] for item in payload["live_aircraft"]], ["abc001"])

    def test_tracker_payload_contains_sightings_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "skyledger.db"))
            db.init()
            snapshot = normalize_aircraft(
                {
                    "hex": "ABC456",
                    "flight": " N456AB ",
                    "lat": 41.001,
                    "lon": -87.0,
                    "alt_baro": 2200,
                    "gs": 120,
                    "track": 180,
                },
                41.0,
                -87.0,
                "2026-06-01T12:00:00Z",
            )
            assert snapshot is not None

            # Setup tracker
            tracker = SkyLedgerTracker(
                AppConfig(live_aircraft_timeout_seconds=10),
                db,
                FakeReader([snapshot]),
                DiscordNotifier("", False),
            )

            # 1. Test payload before logging (0 sightings expected)
            payload = asyncio.run(tracker.tick())
            closest = payload["closest_aircraft"]
            self.assertIsNotNone(closest)
            self.assertEqual(closest["total_sightings"], 0)

            # 2. Log a flyover to make sightings > 0
            db.record_flyover(snapshot, "reveal", True, True)

            # 3. Request payload again (1 sighting expected now)
            payload = asyncio.run(tracker.tick())
            closest = payload["closest_aircraft"]
            self.assertIsNotNone(closest)
            self.assertEqual(closest["total_sightings"], 1)

    def test_record_flyover_updates_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "skyledger.db"))
            db.init()
            snapshot = normalize_aircraft(
                {
                    "hex": "ABC123",
                    "flight": " N123AB ",
                    "lat": 41.001,
                    "lon": -87.0,
                    "alt_baro": 2200,
                    "gs": 120,
                    "track": 180,
                },
                41.0,
                -87.0,
                "2026-06-01T12:00:00Z",
            )
            assert snapshot is not None
            context = db.record_flyover(snapshot, "reveal", True, True)
            stats = db.get_today_stats("2026-06-01")
            record = db.get_aircraft_record("abc123")
            self.assertTrue(context["is_new_aircraft"])
            self.assertEqual(stats["total_flyovers"], 1)
            self.assertEqual(stats["new_aircraft"], 1)
            self.assertEqual(record["total_sightings"], 1)

    def test_clear_history_removes_logged_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "skyledger.db"))
            db.init()
            snapshot = normalize_aircraft(
                {
                    "hex": "ABC123",
                    "flight": " N123AB ",
                    "lat": 41.001,
                    "lon": -87.0,
                    "alt_baro": 2200,
                },
                41.0,
                -87.0,
                self.current_utc_iso(),
            )
            assert snapshot is not None
            db.record_raw_positions([snapshot], retention_days=14)
            db.record_flyover(snapshot, "reveal", True, True)

            deleted = db.clear_history()

            self.assertEqual(deleted["flyover_events"], 1)
            self.assertEqual(deleted["aircraft"], 1)
            self.assertEqual(deleted["daily_stats"], 1)
            self.assertEqual(deleted["raw_positions"], 1)
            self.assertEqual(deleted["received_aircraft"], 1)
            self.assertEqual(db.get_history(), [])
            self.assertIsNone(db.get_aircraft_record("abc123"))
            self.assertEqual(db.get_today_stats("2026-06-01")["total_flyovers"], 0)

    def test_received_aircraft_summary_tracks_unique_and_max_distance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "skyledger.db"))
            db.init()
            first = normalize_aircraft(
                {"hex": "ABC123", "lat": 41.001, "lon": -87.0, "alt_baro": 2200},
                41.0,
                -87.0,
                "2026-06-01T12:00:00Z",
            )
            second = normalize_aircraft(
                {"hex": "DEF456", "lat": 41.01, "lon": -87.0, "alt_baro": 3500},
                41.0,
                -87.0,
                "2026-06-01T12:01:00Z",
            )
            assert first is not None
            assert second is not None

            db.record_raw_positions([first, second], retention_days=14)
            summary = db.get_summary_stats()

            self.assertEqual(summary["total_aircraft"], 2)
            self.assertEqual(summary["total_flyovers"], 0)
            self.assertGreater(summary["max_distance_mi"], first.distance_mi)

    def test_update_config_file_persists_map_zoom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("home_lat: 41.0\nhome_lon: -87.0\n", encoding="utf-8")

            config = update_config_file(str(path), {"map_zoom_level": 15})

            self.assertEqual(config.map_zoom_level, 15)
            self.assertIn("map_zoom_level: 15", path.read_text(encoding="utf-8"))

    def test_update_config_file_persists_home_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("home_lat: 41.0\nhome_lon: -87.0\nhome_name: Old\n", encoding="utf-8")

            config = update_config_file(
                str(path),
                {"home_name": "Hangar", "home_lat": 41.5, "home_lon": -87.5},
            )

            self.assertEqual(config.home_name, "Hangar")
            self.assertEqual(config.home_lat, 41.5)
            self.assertEqual(config.home_lon, -87.5)

    def test_home_settings_endpoint_updates_runtime_config_and_reader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            db_path = Path(tmp) / "skyledger.db"
            aircraft_path = Path(tmp) / "aircraft.json"
            config_path.write_text(
                "\n".join(
                    [
                        "home_lat: 41.0",
                        "home_lon: -87.0",
                        "home_name: Old",
                        f"database_path: '{db_path}'",
                        f"adsb_json_path: '{aircraft_path}'",
                    ]
                ),
                encoding="utf-8",
            )
            app = create_app(str(config_path))
            app.state.db.init()
            route = next(
                route
                for route in app.routes
                if getattr(route, "path", None) == "/api/settings/home"
            )

            response = asyncio.run(
                route.endpoint(HomeSettingsRequest(home_name="Hangar", home_lat=41.5, home_lon=-87.5))
            )

            self.assertEqual(
                response,
                {"ok": True, "home_name": "Hangar", "home_lat": 41.5, "home_lon": -87.5},
            )
            self.assertEqual(app.state.config.home_name, "Hangar")
            self.assertEqual(app.state.config.home_lat, 41.5)
            self.assertEqual(app.state.config.home_lon, -87.5)
            self.assertEqual(app.state.tracker.reader.home_lat, 41.5)
            self.assertEqual(app.state.tracker.reader.home_lon, -87.5)

    def test_home_settings_endpoint_reports_config_write_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            db_path = Path(tmp) / "skyledger.db"
            aircraft_path = Path(tmp) / "aircraft.json"
            config_path.write_text(
                "\n".join(
                    [
                        "home_lat: 41.0",
                        "home_lon: -87.0",
                        "home_name: Old",
                        f"database_path: '{db_path}'",
                        f"adsb_json_path: '{aircraft_path}'",
                    ]
                ),
                encoding="utf-8",
            )
            app = create_app(str(config_path))
            route = next(
                route
                for route in app.routes
                if getattr(route, "path", None) == "/api/settings/home"
            )

            with patch("skyledger.main.update_config_file", side_effect=PermissionError("denied")):
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(
                        route.endpoint(
                            HomeSettingsRequest(home_name="Hangar", home_lat=41.5, home_lon=-87.5)
                        )
                    )

            self.assertEqual(raised.exception.status_code, 500)
            self.assertIn("not writable", raised.exception.detail)

    def test_update_config_file_persists_aircraft_marker_colors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("home_lat: 41.0\nhome_lon: -87.0\n", encoding="utf-8")

            config = update_config_file(
                str(path),
                {
                    "aircraft_marker_low_color": "#ffcc00",
                    "aircraft_marker_default_color": "#3366ff",
                },
            )

            self.assertEqual(config.aircraft_marker_low_color, "#ffcc00")
            self.assertEqual(config.aircraft_marker_default_color, "#3366ff")

    def test_aircraft_marker_colors_endpoint_updates_runtime_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            db_path = Path(tmp) / "skyledger.db"
            aircraft_path = Path(tmp) / "aircraft.json"
            config_path.write_text(
                "\n".join(
                    [
                        "home_lat: 41.0",
                        "home_lon: -87.0",
                        f"database_path: '{db_path}'",
                        f"adsb_json_path: '{aircraft_path}'",
                    ]
                ),
                encoding="utf-8",
            )
            app = create_app(str(config_path))
            app.state.db.init()
            route = next(
                route
                for route in app.routes
                if getattr(route, "path", None) == "/api/settings/aircraft-marker-colors"
            )

            response = asyncio.run(
                route.endpoint(
                    AircraftMarkerColorsRequest(
                        aircraft_marker_low_color="#FFCC00",
                        aircraft_marker_default_color="#3366FF",
                    )
                )
            )

            self.assertEqual(
                response,
                {
                    "ok": True,
                    "aircraft_marker_low_color": "#ffcc00",
                    "aircraft_marker_default_color": "#3366ff",
                },
            )
            self.assertEqual(app.state.config.aircraft_marker_low_color, "#ffcc00")
            self.assertEqual(app.state.config.aircraft_marker_default_color, "#3366ff")

    def test_receiver_start_rejects_non_local_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("skyledger.receiver_control.platform.system", return_value="Windows"):
                result = start_windows_receiver("http://192.168.1.50:8080/data/aircraft.json", Path(tmp))

        self.assertFalse(result["ok"])
        self.assertIn("local HTTP", result["error"])

    def test_receiver_start_reports_already_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("skyledger.receiver_control.platform.system", return_value="Windows"),
                patch("skyledger.receiver_control._is_port_open", return_value=True),
            ):
                result = start_windows_receiver("http://127.0.0.1:8080/data/aircraft.json", Path(tmp))

        self.assertTrue(result["ok"])
        self.assertTrue(result["already_running"])
        self.assertEqual(result["port"], 8080)

    def test_update_config_file_rejects_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("home_lat: 41.0\nhome_lon: -87.0\n", encoding="utf-8")

            with self.assertRaises(ValueError) as context:
                update_config_file(str(path), {"unknown_field": "value"})

            self.assertIn("unknown_field", str(context.exception))

    def test_update_config_file_rejects_multiple_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("home_lat: 41.0\nhome_lon: -87.0\n", encoding="utf-8")

            with self.assertRaises(ValueError) as context:
                update_config_file(
                    str(path),
                    {"unknown_field1": "value1", "unknown_field2": "value2"},
                )

            self.assertIn("unknown_field1", str(context.exception))
            self.assertIn("unknown_field2", str(context.exception))

    def test_execute_with_retry_retries_on_busy_and_database_locked_errors(self) -> None:
        """Test that execute_with_retry retries on 'busy' and 'database is locked' errors."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "skyledger.db"))
            db.init()

            call_count = 0

            def fail_once_with_busy():
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise sqlite3.OperationalError("database is locked")
                return "success"

            result = db.execute_with_retry(fail_once_with_busy)
            self.assertEqual(result, "success")
            self.assertEqual(call_count, 2)

            call_count = 0

            def fail_once_with_busy2():
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise sqlite3.OperationalError("busy")
                return "success"

            result = db.execute_with_retry(fail_once_with_busy2)
            self.assertEqual(result, "success")
            self.assertEqual(call_count, 2)

    def test_execute_with_retry_gives_up_after_max_attempts(self) -> None:
        """Test that execute_with_retry gives up after max attempts."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "skyledger.db"))
            db.init()

            def always_fail():
                raise sqlite3.OperationalError("database is locked")

            with self.assertRaises(sqlite3.OperationalError):
                db.execute_with_retry(always_fail, attempts=3)

    def test_checkpoint_wal(self) -> None:
        """Test that checkpoint_wal executes the PRAGMA."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "skyledger.db"))
            db.init()

            snapshot = normalize_aircraft(
                {"hex": "ABC123", "lat": 41.001, "lon": -87.0, "alt_baro": 2200},
                41.0,
                -87.0,
                self.current_utc_iso(),
            )
            assert snapshot is not None
            db.record_raw_positions([snapshot], retention_days=14)

            # This should not raise
            db.checkpoint_wal()

            # Verify WAL was checkpointed (wal file should be truncated or removed)
            wal_path = Path(tmp) / "skyledger.db-wal"
            if wal_path.exists():
                # WAL file exists but should be small after TRUNCATE checkpoint
                self.assertLess(wal_path.stat().st_size, 1000)

    def test_raw_position_retention_boundary(self) -> None:
        """Positions at the cutoff survive while older positions are deleted."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "skyledger.db"))
            db.init()
            now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
            cutoff = now - timedelta(days=14)

            at_cutoff = normalize_aircraft(
                {"hex": "ABC123", "lat": 41.001, "lon": -87.0},
                41.0,
                -87.0,
                cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            before_cutoff = normalize_aircraft(
                {"hex": "DEF456", "lat": 41.002, "lon": -87.0},
                41.0,
                -87.0,
                (cutoff - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            assert at_cutoff is not None
            assert before_cutoff is not None

            class FrozenDateTime(datetime):
                @classmethod
                def now(cls, tz=None):
                    return now if tz is not None else now.replace(tzinfo=None)

            with patch("skyledger.db.datetime", FrozenDateTime):
                db.record_raw_positions([at_cutoff, before_cutoff], retention_days=14)

            with db.connect() as conn:
                retained = conn.execute(
                    "SELECT hex FROM raw_positions ORDER BY hex"
                ).fetchall()
            self.assertEqual([row["hex"] for row in retained], ["abc123"])

    def test_tracker_throttles_raw_positions_writes(self) -> None:
        """Test that tracker only writes raw_positions every RAW_POSITIONS_WRITE_INTERVAL ticks."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "skyledger.db"))
            db.init()

            snapshot = normalize_aircraft(
                {"hex": "ABC123", "lat": 41.001, "lon": -87.0, "alt_baro": 2200, "seen": 1, "seen_pos": 1},
                41.0,
                -87.0,
                self.current_utc_iso(),
            )
            assert snapshot is not None

            tracker = SkyLedgerTracker(
                AppConfig(live_aircraft_timeout_seconds=10),
                db,
                FakeReader([snapshot]),
                DiscordNotifier("", False),
            )

            # Run 4 ticks - should NOT write raw_positions (interval is 5)
            for _ in range(4):
                asyncio.run(tracker.tick())

            # Check raw_positions count - should be 0
            with db.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM raw_positions").fetchone()[0]
            self.assertEqual(count, 0)

            # Run 1 more tick (5th) - should write raw_positions
            asyncio.run(tracker.tick())

            with db.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM raw_positions").fetchone()[0]
            self.assertEqual(count, 1)

            # Run 4 more ticks - should not write again
            for _ in range(4):
                asyncio.run(tracker.tick())

            with db.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM raw_positions").fetchone()[0]
            self.assertEqual(count, 1)

            # Run 1 more tick (10th) - should write again
            asyncio.run(tracker.tick())

            with db.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM raw_positions").fetchone()[0]
            self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
