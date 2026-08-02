from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .adsb import AircraftSnapshot, utc_now_iso


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS aircraft (
    hex TEXT PRIMARY KEY,
    registration TEXT,
    aircraft_type TEXT,
    operator TEXT,
    photo_url TEXT,
    first_seen TEXT,
    last_seen TEXT,
    total_sightings INTEGER DEFAULT 0,
    lowest_altitude_ft INTEGER,
    lowest_distance_mi REAL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS flyover_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seen_at TEXT,
    hex TEXT,
    callsign TEXT,
    altitude_ft INTEGER,
    speed_kt INTEGER,
    heading REAL,
    vertical_rate_fpm INTEGER,
    distance_mi REAL,
    closest_lat REAL,
    closest_lon REAL,
    event_type TEXT,
    was_alerted INTEGER,
    was_revealed INTEGER,
    is_new_aircraft INTEGER,
    is_new_lowest INTEGER
);

CREATE INDEX IF NOT EXISTS idx_flyover_events_seen_at ON flyover_events(seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_flyover_events_hex ON flyover_events(hex, seen_at DESC);

CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    total_flyovers INTEGER DEFAULT 0,
    new_aircraft INTEGER DEFAULT 0,
    repeat_aircraft INTEGER DEFAULT 0,
    low_flyovers INTEGER DEFAULT 0,
    lowest_altitude_ft INTEGER
);

CREATE TABLE IF NOT EXISTS aircraft_enrichment_cache (
    hex TEXT PRIMARY KEY,
    registration TEXT,
    aircraft_type TEXT,
    operator TEXT,
    photo_url TEXT,
    route_from TEXT,
    route_to TEXT,
    source TEXT,
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS raw_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seen_at TEXT,
    hex TEXT,
    lat REAL,
    lon REAL,
    altitude_ft INTEGER,
    speed_kt INTEGER,
    heading REAL
);

CREATE INDEX IF NOT EXISTS idx_raw_positions_seen_at ON raw_positions(seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_positions_hex ON raw_positions(hex, seen_at DESC);

CREATE TABLE IF NOT EXISTS received_aircraft (
    hex TEXT PRIMARY KEY,
    first_seen TEXT,
    last_seen TEXT,
    last_callsign TEXT,
    total_positions INTEGER DEFAULT 0,
    max_distance_mi REAL,
    max_distance_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS flight_route_cache (
    callsign TEXT PRIMARY KEY,
    route_from TEXT,
    route_to TEXT,
    source TEXT,
    last_updated TEXT NOT NULL
);
"""

SCHEMA_VERSION = 2


class Database:
    SUMMARY_CACHE_TTL_SECONDS = 10.0

    def __init__(self, path: str) -> None:
        self.path = Path(path).expanduser()
        if self.path.parent != Path("."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._summary_cache: dict[str, Any] | None = None
        self._summary_cache_at = 0.0
        self._summary_cache_lock = threading.Lock()

    def init(self) -> None:
        with self.connect() as conn:
            current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {current_version} is newer than supported "
                    f"version {SCHEMA_VERSION}."
                )
            conn.executescript(SCHEMA)
            if current_version < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")  # 30 seconds for lock contention
            yield conn
            conn.commit()
        finally:
            conn.close()

    def execute_with_retry(self, fn, attempts: int = 4) -> Any:
        delay = 0.05
        for attempt in range(attempts):
            try:
                return fn()
            except sqlite3.OperationalError as exc:
                err = str(exc).lower()
                if not any(x in err for x in ("locked", "busy", "database is locked")) or attempt == attempts - 1:
                    raise
                time.sleep(delay)
                delay *= 2
        return None

    def record_raw_positions(self, snapshots: Iterable[AircraftSnapshot], retention_days: int) -> None:
        snapshot_list = list(snapshots)
        rows = [
            (
                item.received_at,
                item.hex,
                item.lat,
                item.lon,
                item.altitude_ft,
                item.speed_kt,
                item.heading,
            )
            for item in snapshot_list
            if item.hex and item.has_position
        ]
        received_rows = [
            (
                item.hex,
                item.received_at,
                item.received_at,
                item.callsign,
                int(item.has_position),
                item.distance_mi,
                item.received_at if item.distance_mi is not None else None,
            )
            for item in snapshot_list
            if item.hex
        ]
        if not rows and not received_rows:
            return

        def write() -> None:
            with self.connect() as conn:
                if rows:
                    conn.executemany(
                        """
                        INSERT INTO raw_positions (seen_at, hex, lat, lon, altitude_ft, speed_kt, heading)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                cutoff = datetime.now(timezone.utc) - timedelta(days=int(retention_days))
                cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
                conn.execute("DELETE FROM raw_positions WHERE seen_at < ?", (cutoff_iso,))
                if received_rows:
                    conn.executemany(
                        """
                        INSERT INTO received_aircraft (
                            hex, first_seen, last_seen, last_callsign,
                            total_positions, max_distance_mi, max_distance_seen_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(hex) DO UPDATE SET
                            last_seen = excluded.last_seen,
                            last_callsign = COALESCE(excluded.last_callsign, received_aircraft.last_callsign),
                            total_positions = received_aircraft.total_positions + excluded.total_positions,
                            max_distance_mi = CASE
                                WHEN excluded.max_distance_mi IS NULL THEN received_aircraft.max_distance_mi
                                WHEN received_aircraft.max_distance_mi IS NULL THEN excluded.max_distance_mi
                                WHEN excluded.max_distance_mi > received_aircraft.max_distance_mi THEN excluded.max_distance_mi
                                ELSE received_aircraft.max_distance_mi
                            END,
                            max_distance_seen_at = CASE
                                WHEN excluded.max_distance_mi IS NULL THEN received_aircraft.max_distance_seen_at
                                WHEN received_aircraft.max_distance_mi IS NULL THEN excluded.max_distance_seen_at
                                WHEN excluded.max_distance_mi > received_aircraft.max_distance_mi THEN excluded.max_distance_seen_at
                                ELSE received_aircraft.max_distance_seen_at
                            END
                        """,
                        received_rows,
                    )

        self.execute_with_retry(write)

    def record_flyover(
        self,
        snapshot: AircraftSnapshot,
        event_type: str,
        was_alerted: bool,
        was_revealed: bool,
    ) -> dict[str, Any]:
        seen_at = snapshot.received_at or utc_now_iso()
        date = seen_at[:10]

        if not snapshot.hex:
            return {
                "is_new_aircraft": False,
                "is_new_lowest": False,
                "previous_sightings": 0,
                "previous_lowest_altitude_ft": None,
            }

        def write() -> dict[str, Any]:
            with self.connect() as conn:
                # Serialize the read/modify/write sequence so two first
                # sightings cannot both conclude that the aircraft is new.
                conn.execute("BEGIN IMMEDIATE")
                aircraft = conn.execute(
                    "SELECT * FROM aircraft WHERE hex = ?",
                    (snapshot.hex,),
                ).fetchone()
                is_new_aircraft = aircraft is None
                previous_lowest = aircraft["lowest_altitude_ft"] if aircraft else None
                previous_sightings = int(aircraft["total_sightings"]) if aircraft else 0
                is_new_lowest = (
                    snapshot.altitude_ft is not None
                    and (previous_lowest is None or snapshot.altitude_ft < previous_lowest)
                )

                if is_new_aircraft:
                    conn.execute(
                        """
                        INSERT INTO aircraft (
                            hex, registration, first_seen, last_seen, total_sightings,
                            lowest_altitude_ft, lowest_distance_mi
                        )
                        VALUES (?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            snapshot.hex,
                            snapshot.registration,
                            seen_at,
                            seen_at,
                            snapshot.altitude_ft,
                            snapshot.distance_mi,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE aircraft
                        SET last_seen = ?,
                            registration = COALESCE(?, registration),
                            total_sightings = total_sightings + 1,
                            lowest_altitude_ft = CASE
                                WHEN ? IS NULL THEN lowest_altitude_ft
                                WHEN lowest_altitude_ft IS NULL THEN ?
                                WHEN ? < lowest_altitude_ft THEN ?
                                ELSE lowest_altitude_ft
                            END,
                            lowest_distance_mi = CASE
                                WHEN ? IS NULL THEN lowest_distance_mi
                                WHEN lowest_distance_mi IS NULL THEN ?
                                WHEN ? < lowest_distance_mi THEN ?
                                ELSE lowest_distance_mi
                            END
                        WHERE hex = ?
                        """,
                        (
                            seen_at,
                            snapshot.registration,
                            snapshot.altitude_ft,
                            snapshot.altitude_ft,
                            snapshot.altitude_ft,
                            snapshot.altitude_ft,
                            snapshot.distance_mi,
                            snapshot.distance_mi,
                            snapshot.distance_mi,
                            snapshot.distance_mi,
                            snapshot.hex,
                        ),
                    )

                conn.execute(
                    """
                    INSERT INTO flyover_events (
                        seen_at, hex, callsign, altitude_ft, speed_kt, heading,
                        vertical_rate_fpm, distance_mi, closest_lat, closest_lon,
                        event_type, was_alerted, was_revealed, is_new_aircraft, is_new_lowest
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        seen_at,
                        snapshot.hex,
                        snapshot.callsign,
                        snapshot.altitude_ft,
                        snapshot.speed_kt,
                        snapshot.heading,
                        snapshot.vertical_rate_fpm,
                        snapshot.distance_mi,
                        snapshot.lat,
                        snapshot.lon,
                        event_type,
                        int(was_alerted),
                        int(was_revealed),
                        int(is_new_aircraft),
                        int(is_new_lowest),
                    ),
                )

                conn.execute(
                    """
                    INSERT INTO daily_stats (
                        date, total_flyovers, new_aircraft, repeat_aircraft,
                        low_flyovers, lowest_altitude_ft
                    )
                    VALUES (?, 1, ?, ?, ?, ?)
                    ON CONFLICT(date) DO UPDATE SET
                        total_flyovers = total_flyovers + 1,
                        new_aircraft = new_aircraft + excluded.new_aircraft,
                        repeat_aircraft = repeat_aircraft + excluded.repeat_aircraft,
                        low_flyovers = low_flyovers + excluded.low_flyovers,
                        lowest_altitude_ft = CASE
                            WHEN excluded.lowest_altitude_ft IS NULL THEN daily_stats.lowest_altitude_ft
                            WHEN daily_stats.lowest_altitude_ft IS NULL THEN excluded.lowest_altitude_ft
                            WHEN excluded.lowest_altitude_ft < daily_stats.lowest_altitude_ft THEN excluded.lowest_altitude_ft
                            ELSE daily_stats.lowest_altitude_ft
                        END
                    """,
                    (
                        date,
                        int(is_new_aircraft),
                        int(not is_new_aircraft),
                        int(snapshot.altitude_ft is not None and snapshot.altitude_ft <= 2500),
                        snapshot.altitude_ft,
                    ),
                )

                return {
                    "is_new_aircraft": is_new_aircraft,
                    "is_new_lowest": is_new_lowest,
                    "previous_sightings": previous_sightings,
                    "previous_lowest_altitude_ft": previous_lowest,
                }

        return self.execute_with_retry(write)

    def get_aircraft_record(self, hex_value: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT a.*, e.route_from, e.route_to, e.source AS enrichment_source, e.last_updated AS enrichment_updated
                FROM aircraft a
                LEFT JOIN aircraft_enrichment_cache e ON e.hex = a.hex
                WHERE a.hex = ?
                """,
                (hex_value.lower(),),
            ).fetchone()
            return dict(row) if row else None

    def get_aircraft_history(self, hex_value: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM flyover_events
                WHERE hex = ?
                ORDER BY seen_at DESC
                LIMIT ?
                """,
                (hex_value.lower(), limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_cached_flight_route(self, callsign: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM flight_route_cache WHERE callsign = ?",
                (callsign.upper(),),
            ).fetchone()
            return dict(row) if row else None

    def cache_flight_route(
        self,
        callsign: str,
        route_from: str | None,
        route_to: str | None,
        source: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO flight_route_cache (
                    callsign, route_from, route_to, source, last_updated
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(callsign) DO UPDATE SET
                    route_from = excluded.route_from,
                    route_to = excluded.route_to,
                    source = excluded.source,
                    last_updated = excluded.last_updated
                """,
                (callsign.upper(), route_from, route_to, source, utc_now_iso()),
            )

    def get_recent_events(self, limit: int = 12) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT f.*, a.registration, a.aircraft_type, a.operator, a.photo_url
                FROM flyover_events f
                LEFT JOIN aircraft a ON a.hex = f.hex
                ORDER BY f.seen_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_history(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.get_recent_events(limit=limit)

    def get_today_stats(self, date: str | None = None) -> dict[str, Any]:
        date = date or utc_now_iso()[:10]
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM daily_stats WHERE date = ?", (date,)).fetchone()
            if not row:
                return {
                    "date": date,
                    "total_flyovers": 0,
                    "new_aircraft": 0,
                    "repeat_aircraft": 0,
                    "low_flyovers": 0,
                    "lowest_altitude_ft": None,
                }
            return dict(row)

    @staticmethod
    def _day_bounds(date: str) -> tuple[str, str]:
        start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        return (
            start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def count_distinct_aircraft_today(self, date: str | None = None) -> int:
        date = date or utc_now_iso()[:10]
        start, end = self._day_bounds(date)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT hex) AS count FROM flyover_events "
                "WHERE seen_at >= ? AND seen_at < ?",
                (start, end),
            ).fetchone()
            return int(row["count"])

    def get_summary_stats(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._summary_cache_lock:
            if (
                self._summary_cache is not None
                and now - self._summary_cache_at < self.SUMMARY_CACHE_TTL_SECONDS
            ):
                return dict(self._summary_cache)
        with self.connect() as conn:
            summary = self._get_summary_stats(conn)
        with self._summary_cache_lock:
            self._summary_cache = dict(summary)
            self._summary_cache_at = now
        return summary

    @staticmethod
    def _get_summary_stats(conn: sqlite3.Connection) -> dict[str, Any]:
        row = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM received_aircraft) AS total_aircraft,
                (SELECT COUNT(*) FROM flyover_events) AS total_flyovers,
                (SELECT MIN(altitude_ft) FROM flyover_events WHERE altitude_ft IS NOT NULL) AS lowest_flyover_ft,
                (SELECT MAX(max_distance_mi) FROM received_aircraft) AS max_distance_mi
            """
        ).fetchone()
        return dict(row)

    def get_dashboard_data(self, closest_hex: str | None = None) -> dict[str, Any]:
        """Fetch all persistent dashboard data with one SQLite connection."""
        date = utc_now_iso()[:10]
        start, end = self._day_bounds(date)
        now = time.monotonic()
        with self.connect() as conn:
            today_row = conn.execute(
                "SELECT * FROM daily_stats WHERE date = ?", (date,)
            ).fetchone()
            today = dict(today_row) if today_row else {
                "date": date,
                "total_flyovers": 0,
                "new_aircraft": 0,
                "repeat_aircraft": 0,
                "low_flyovers": 0,
                "lowest_altitude_ft": None,
            }
            distinct = conn.execute(
                "SELECT COUNT(DISTINCT hex) AS count FROM flyover_events "
                "WHERE seen_at >= ? AND seen_at < ?",
                (start, end),
            ).fetchone()
            with self._summary_cache_lock:
                cached = (
                    dict(self._summary_cache)
                    if self._summary_cache is not None
                    and now - self._summary_cache_at < self.SUMMARY_CACHE_TTL_SECONDS
                    else None
                )
            summary = cached or self._get_summary_stats(conn)
            if cached is None:
                with self._summary_cache_lock:
                    self._summary_cache = dict(summary)
                    self._summary_cache_at = now
            record = None
            if closest_hex:
                row = conn.execute(
                    """
                    SELECT a.*, e.route_from, e.route_to,
                           e.source AS enrichment_source, e.last_updated AS enrichment_updated
                    FROM aircraft a
                    LEFT JOIN aircraft_enrichment_cache e ON e.hex = a.hex
                    WHERE a.hex = ?
                    """,
                    (closest_hex.lower(),),
                ).fetchone()
                record = dict(row) if row else None
        today["aircraft_count"] = int(distinct["count"])
        return {"today": today, "summary": summary, "closest_record": record}

    def clear_history(self) -> dict[str, int]:
        def write() -> dict[str, int]:
            with self.connect() as conn:
                counts = {
                    "flyover_events": int(conn.execute("SELECT COUNT(*) FROM flyover_events").fetchone()[0]),
                    "aircraft": int(conn.execute("SELECT COUNT(*) FROM aircraft").fetchone()[0]),
                    "daily_stats": int(conn.execute("SELECT COUNT(*) FROM daily_stats").fetchone()[0]),
                    "raw_positions": int(conn.execute("SELECT COUNT(*) FROM raw_positions").fetchone()[0]),
                    "received_aircraft": int(conn.execute("SELECT COUNT(*) FROM received_aircraft").fetchone()[0]),
                }
                conn.execute("DELETE FROM flyover_events")
                conn.execute("DELETE FROM aircraft")
                conn.execute("DELETE FROM daily_stats")
                conn.execute("DELETE FROM raw_positions")
                conn.execute("DELETE FROM received_aircraft")
                conn.execute(
                    "DELETE FROM sqlite_sequence WHERE name IN ('flyover_events', 'raw_positions')"
                )
                return counts

        result = self.execute_with_retry(write)
        with self._summary_cache_lock:
            self._summary_cache = None
            self._summary_cache_at = 0.0
        return result

    def checkpoint_wal(self) -> None:
        """Checkpoint the WAL file to prevent unbounded growth and reduce lock contention."""
        with self.connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def quick_check(self) -> str:
        """Return SQLite's quick integrity-check result."""
        with self.connect() as conn:
            rows = conn.execute("PRAGMA quick_check").fetchall()
        return "\n".join(str(row[0]) for row in rows)

    def backup(self, destination: str | Path) -> Path:
        """Create a transactionally consistent online backup, including WAL data."""
        destination_path = Path(destination).expanduser().resolve()
        source_path = self.path.resolve()
        if destination_path == source_path:
            raise ValueError("Backup destination must differ from the live database path.")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = destination_path.with_name(f".{destination_path.name}.tmp")
        temporary_path.unlink(missing_ok=True)
        try:
            with sqlite3.connect(source_path, timeout=15) as source:
                source.execute("PRAGMA busy_timeout=30000")
                with sqlite3.connect(temporary_path) as target:
                    source.backup(target)
                    result = target.execute("PRAGMA quick_check").fetchone()[0]
                    if result != "ok":
                        raise sqlite3.DatabaseError(f"Backup integrity check failed: {result}")
            temporary_path.replace(destination_path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return destination_path
