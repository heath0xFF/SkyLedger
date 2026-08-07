from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .adsb import ADSBReader, AircraftSnapshot, SourceStatus, utc_now_iso
from .config import AppConfig
from .db import Database
from .discord import DiscordNotifier
from .enrichment import EnrichmentProvider
from .geo import cardinal_direction

Broadcaster = Callable[[dict[str, Any]], Awaitable[None]]
LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ActiveFlyover:
    key: str
    first_seen_monotonic: float
    last_seen_monotonic: float
    latest: AircraftSnapshot
    closest: AircraftSnapshot
    closest_distance_mi: float
    countdown_started_monotonic: float | None = None
    reveal_started_monotonic: float | None = None
    reveal_until_monotonic: float | None = None
    event_logged: bool = False
    was_alerted: bool = False
    was_revealed: bool = False
    event_context: dict[str, Any] | None = None
    demo: bool = False

    def update(self, snapshot: AircraftSnapshot, now: float) -> None:
        self.latest = snapshot
        self.last_seen_monotonic = now
        distance = snapshot.distance_mi
        if distance is not None and distance < self.closest_distance_mi:
            self.closest = snapshot
            self.closest_distance_mi = distance


class SkyLedgerTracker:
    RAW_POSITIONS_WRITE_INTERVAL = 5
    WAL_CHECKPOINT_INTERVAL_SECONDS = 60
    MAX_ERROR_BACKOFF_SECONDS = 30.0

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        reader: ADSBReader,
        notifier: DiscordNotifier,
        broadcaster: Broadcaster | None = None,
        enricher: EnrichmentProvider | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.reader = reader
        self.notifier = notifier
        self.broadcaster = broadcaster
        self.enricher = enricher or EnrichmentProvider(False)
        self.active: dict[str, ActiveFlyover] = {}
        self.live_aircraft: list[AircraftSnapshot] = []
        self.alert_cooldowns: dict[str, float] = {}
        self.last_payload: dict[str, Any] = {}
        self.last_poll_at: str | None = None
        self.last_discord_error: str | None = None
        self.tracker_running = False
        self.tracker_last_success_at: str | None = None
        self.tracker_last_error: str | None = None
        self.tracker_consecutive_errors = 0
        self._stop_event = asyncio.Event()
        self._mutation_lock = asyncio.Lock()
        self._raw_position_tick = 0
        self._last_wal_checkpoint_at = 0.0
        self._wal_checkpoint_info: dict[str, int] | None = None

    async def run(self) -> None:
        self.tracker_running = True
        try:
            while not self._stop_event.is_set():
                try:
                    await self.tick()
                except Exception as exc:  # noqa: BLE001 - keep the tracker supervised.
                    self.tracker_consecutive_errors += 1
                    self.tracker_last_error = f"{type(exc).__name__}: {exc}"
                    LOGGER.exception("SkyLedger tracker tick failed")
                    delay = min(
                        self.MAX_ERROR_BACKOFF_SECONDS,
                        max(0.5, self.config.poll_interval_seconds)
                        * (2 ** min(self.tracker_consecutive_errors - 1, 10)),
                    )
                else:
                    self.tracker_last_success_at = utc_now_iso()
                    self.tracker_last_error = None
                    self.tracker_consecutive_errors = 0
                    delay = max(0.5, self.config.poll_interval_seconds)

                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.tracker_running = False

    def stop(self) -> None:
        self._stop_event.set()

    async def tick(self) -> dict[str, Any]:
        async with self._mutation_lock:
            return await self._tick_unlocked()

    async def _tick_unlocked(self) -> dict[str, Any]:
        snapshots = await self.reader.read()
        fresh_snapshots = [snapshot for snapshot in snapshots if self._is_snapshot_fresh(snapshot)]
        self.last_poll_at = utc_now_iso()
        self.live_aircraft = sorted(
            fresh_snapshots,
            key=lambda item: item.distance_mi if item.distance_mi is not None else 9999,
        )

        # Throttle raw_positions writes to reduce DB lock contention
        self._raw_position_tick += 1
        if self._raw_position_tick >= self.RAW_POSITIONS_WRITE_INTERVAL:
            await asyncio.to_thread(
                self.db.record_raw_positions,
                fresh_snapshots,
                self.config.raw_position_retention_days,
            )
            self._raw_position_tick = 0
        # Periodically checkpoint the WAL to prevent unbounded growth
        now = time.monotonic()
        if now - self._last_wal_checkpoint_at >= self.WAL_CHECKPOINT_INTERVAL_SECONDS:
            self._wal_checkpoint_info = await asyncio.to_thread(self.db.checkpoint_wal)
            self._last_wal_checkpoint_at = now

        await self._process_snapshots(fresh_snapshots)
        self._cleanup_active()
        payload = await self.build_payload_async()
        self.last_payload = payload
        if self.broadcaster:
            await self.broadcaster(payload)
        return payload

    async def clear_history(self) -> dict[str, int]:
        """Clear persistent and in-memory history without racing an ingestion tick."""
        async with self._mutation_lock:
            counts = await asyncio.to_thread(self.db.clear_history)
            self._wal_checkpoint_info = None
            self._last_wal_checkpoint_at = 0.0
            self.active.clear()
            self.alert_cooldowns.clear()
            payload = await self.build_payload_async()
            self.last_payload = payload
            if self.broadcaster:
                await self.broadcaster(payload)
            return counts

    async def _process_snapshots(self, snapshots: list[AircraftSnapshot]) -> None:
        now = time.monotonic()
        seen_keys: set[str] = set()
        for snapshot in snapshots:
            if snapshot.distance_mi is None:
                continue
            if (
                self.config.tracking_radius_miles > 0
                and snapshot.distance_mi > self.config.tracking_radius_miles
            ):
                continue

            seen_keys.add(snapshot.key)
            active = self.active.get(snapshot.key)
            if active is None:
                active = ActiveFlyover(
                    key=snapshot.key,
                    first_seen_monotonic=now,
                    last_seen_monotonic=now,
                    latest=snapshot,
                    closest=snapshot,
                    closest_distance_mi=snapshot.distance_mi,
                )
                self.active[snapshot.key] = active
            else:
                active.update(snapshot, now)

            if self._is_alert_candidate(snapshot):
                if active.countdown_started_monotonic is None and snapshot.distance_mi <= self.config.guess_trigger_radius_miles:
                    active.countdown_started_monotonic = now
                await self._maybe_alert(active)

            if (
                active.countdown_started_monotonic is not None
                and active.reveal_started_monotonic is None
                and now - active.countdown_started_monotonic >= self.config.countdown_seconds
            ):
                await self._start_reveal(active, now)

        for active in list(self.active.values()):
            if (
                active.countdown_started_monotonic is not None
                and active.reveal_started_monotonic is None
                and now - active.countdown_started_monotonic >= self.config.countdown_seconds
            ):
                await self._start_reveal(active, now)

        for key, active in list(self.active.items()):
            if active.demo or key in seen_keys:
                continue
            if self._active_age_seconds(active, now) > self.config.live_aircraft_timeout_seconds:
                await self._finish_if_needed(active, event_type="closest_pass")

    def _cleanup_active(self) -> None:
        now = time.monotonic()
        for key, active in list(self.active.items()):
            reveal_done = active.reveal_until_monotonic is not None and now > active.reveal_until_monotonic
            stale = (
                not active.demo
                and active.reveal_until_monotonic is None
                and self._active_age_seconds(active, now) > self.config.live_aircraft_timeout_seconds
            )
            if reveal_done or stale:
                self.active.pop(key, None)

    def _is_snapshot_fresh(self, snapshot: AircraftSnapshot) -> bool:
        timeout = self.config.live_aircraft_timeout_seconds
        if timeout <= 0:
            return True

        ages = [snapshot.seen_seconds]
        if snapshot.has_position:
            ages.append(snapshot.seen_position_seconds)

        known_ages = [age for age in ages if age is not None]
        if not known_ages:
            return True
        return max(known_ages) <= timeout

    @staticmethod
    def _active_age_seconds(active: ActiveFlyover, now: float) -> float:
        return now - active.last_seen_monotonic

    async def _start_reveal(self, active: ActiveFlyover, now: float) -> None:
        active.reveal_started_monotonic = now
        active.reveal_until_monotonic = now + self.config.reveal_duration_seconds
        active.was_revealed = True
        await self._finish_if_needed(active, event_type="reveal")

    async def _finish_if_needed(self, active: ActiveFlyover, event_type: str) -> None:
        if active.event_logged:
            return
        if active.closest_distance_mi > self.config.guess_trigger_radius_miles and not active.was_alerted:
            return

        context = await asyncio.to_thread(
            self.db.record_flyover,
            active.closest,
            event_type,
            active.was_alerted,
            active.was_revealed,
        )
        active.event_context = context
        active.event_logged = True

    async def _maybe_alert(self, active: ActiveFlyover) -> None:
        if not self.notifier.enabled:
            return

        snapshot = active.latest
        if snapshot.distance_mi is None or snapshot.distance_mi > self.config.alert_radius_miles:
            return
        cooldown_key = snapshot.hex or snapshot.callsign or snapshot.key
        now = time.monotonic()
        cooldown_seconds = self.config.alert_cooldown_minutes * 60
        if now - self.alert_cooldowns.get(cooldown_key, 0) < cooldown_seconds:
            return

        record = await asyncio.to_thread(self.db.get_aircraft_record, snapshot.hex) or {}
        ok, error = await self.notifier.send_alert(
            {
                **record,
                **snapshot.to_dict(),
                "timestamp": utc_now_iso(),
                "seen_before_count": record.get("total_sightings", 0),
            }
        )
        self.last_discord_error = error
        active.was_alerted = ok
        self.alert_cooldowns[cooldown_key] = now
        if not ok and error:
            return

    def _is_alert_candidate(self, snapshot: AircraftSnapshot) -> bool:
        if snapshot.altitude_ft is None:
            return False
        if snapshot.altitude_ft > self.config.max_alert_altitude_ft:
            return False
        if snapshot.distance_mi is None:
            return False
        return True

    def build_payload(self) -> dict[str, Any]:
        mode, focus = self._current_mode()
        closest = self.live_aircraft[0].to_dict() if self.live_aircraft else None
        dashboard_data = self.db.get_dashboard_data(closest.get("hex") if closest else None)
        today = dashboard_data["today"]
        summary = dashboard_data["summary"]
        if closest:
            record = dashboard_data["closest_record"] or {}
            closest["registration"] = closest.get("registration") or record.get("registration")
            closest["aircraft_type"] = record.get("aircraft_type")
            closest["operator"] = record.get("operator")
            closest["total_sightings"] = record.get("total_sightings", 0)
        payload = {
            "mode": mode,
            "focus": focus,
            "status": self.status_payload(),
            "config": {
                "home_name": self.config.home_name,
                "home_lat": self.config.home_lat,
                "home_lon": self.config.home_lon,
                "dashboard_title": self.config.dashboard_title,
                "tar1090_url": self.config.tar1090_url,
                "alert_radius_miles": self.config.alert_radius_miles,
                "guess_trigger_radius_miles": self.config.guess_trigger_radius_miles,
                "tracking_radius_miles": self.config.tracking_radius_miles,
                "max_alert_altitude_ft": self.config.max_alert_altitude_ft,
                "map_zoom_level": self.config.map_zoom_level,
                "map_tile_url": self.config.map_tile_url,
                "aircraft_marker_low_color": self.config.aircraft_marker_low_color,
                "aircraft_marker_default_color": self.config.aircraft_marker_default_color,
            },
            "live_aircraft": [item.to_dict() for item in self.live_aircraft[:40]],
            "closest_aircraft": closest,
            "active_count": len(self.active),
            "stats_total": summary,
            "stats_today": {
                **today,
                "helicopters": sum(1 for item in self.live_aircraft if item.is_helicopter),
            },
        }
        return payload

    async def build_payload_async(self) -> dict[str, Any]:
        """Build a payload without blocking the asyncio event loop on SQLite."""
        payload = await asyncio.to_thread(self.build_payload)
        callsigns = {
            item.get("callsign")
            for item in (payload.get("closest_aircraft"), payload.get("focus"))
            if isinstance(item, dict) and item.get("callsign")
        }
        routes = {
            callsign: await self.enricher.lookup(callsign)
            for callsign in callsigns
        }
        for item in (payload.get("closest_aircraft"), payload.get("focus")):
            if not isinstance(item, dict):
                continue
            route = routes.get(item.get("callsign"))
            if route:
                item["route_from"] = route.route_from
                item["route_to"] = route.route_to
        return payload

    def status_payload(self) -> dict[str, Any]:
        status = self.reader.status.to_dict()
        status.update(
            {
                "last_poll_at": self.last_poll_at,
                "last_discord_error": self.last_discord_error,
                "tracker_running": self.tracker_running,
                "tracker_last_success_at": self.tracker_last_success_at,
                "tracker_last_error": self.tracker_last_error,
                "tracker_consecutive_errors": self.tracker_consecutive_errors,
                "database_path": self.db.path.name,
                "wal_checkpoint_info": self._wal_checkpoint_info,
                "config": self.config.public_dict(),
            }
        )
        return status

    def _current_mode(self) -> tuple[str, dict[str, Any] | None]:
        now = time.monotonic()
        reveals = [
            item
            for item in self.active.values()
            if item.reveal_until_monotonic is not None and now <= item.reveal_until_monotonic
        ]
        if reveals:
            active = max(reveals, key=lambda item: item.reveal_started_monotonic or 0)
            return "reveal", self._reveal_payload(active, now)

        countdowns = [
            item
            for item in self.active.values()
            if item.countdown_started_monotonic is not None and item.reveal_started_monotonic is None
        ]
        if countdowns:
            active = min(countdowns, key=lambda item: item.countdown_started_monotonic or now)
            return "countdown", self._countdown_payload(active, now)

        return "live", None

    def _countdown_payload(self, active: ActiveFlyover, now: float) -> dict[str, Any]:
        started = active.countdown_started_monotonic or now
        elapsed = max(0, now - started)
        remaining = max(0, self.config.countdown_seconds - int(elapsed))
        latest = active.latest
        return {
            "seconds_remaining": remaining,
            "callsign": latest.callsign,
            "hex": latest.hex,
            "direction": cardinal_direction(latest.heading),
            "distance_mi": latest.distance_mi,
            "heading": latest.heading,
            "speed_kt": latest.speed_kt,
        }

    def _reveal_payload(self, active: ActiveFlyover, now: float) -> dict[str, Any]:
        latest = active.closest
        record = self.db.get_aircraft_record(latest.hex) or {}
        context = active.event_context or {}
        return {
            **latest.to_dict(),
            "seconds_remaining": int(max(0, (active.reveal_until_monotonic or now) - now)),
            "direction": cardinal_direction(latest.heading),
            "registration": record.get("registration"),
            "aircraft_type": record.get("aircraft_type"),
            "operator": record.get("operator"),
            "photo_url": record.get("photo_url"),
            "route_from": record.get("route_from"),
            "route_to": record.get("route_to"),
            "total_sightings": record.get("total_sightings") or (context.get("previous_sightings", 0) + 1),
            "first_seen": record.get("first_seen"),
            "last_seen": record.get("last_seen"),
            "lowest_altitude_ft": record.get("lowest_altitude_ft"),
            "lowest_distance_mi": record.get("lowest_distance_mi"),
            "is_new_aircraft": bool(context.get("is_new_aircraft")),
            "is_new_lowest": bool(context.get("is_new_lowest")),
            "previous_sightings": context.get("previous_sightings", 0),
            "previous_lowest_altitude_ft": context.get("previous_lowest_altitude_ft"),
        }

    async def trigger_test_countdown(self) -> dict[str, Any]:
        now = time.monotonic()
        snapshot = AircraftSnapshot(
            key=f"test-{int(now)}",
            hex=f"test{int(now) % 10000:04d}",
            callsign="SKY123",
            registration="N123SK",
            lat=self.config.home_lat,
            lon=self.config.home_lon,
            altitude_ft=3400,
            speed_kt=168,
            heading=182,
            vertical_rate_fpm=-256,
            category="A3",
            distance_mi=0.8,
            seen_seconds=0,
            seen_position_seconds=0,
            received_at=utc_now_iso(),
        )
        active = ActiveFlyover(
            key=snapshot.key,
            first_seen_monotonic=now,
            last_seen_monotonic=now,
            latest=snapshot,
            closest=snapshot,
            closest_distance_mi=0.8,
            countdown_started_monotonic=now,
            demo=True,
        )
        self.active[snapshot.key] = active
        payload = await self.build_payload_async()
        if self.broadcaster:
            await self.broadcaster(payload)
        return payload


def empty_status(source: str) -> SourceStatus:
    return SourceStatus(source=source)
