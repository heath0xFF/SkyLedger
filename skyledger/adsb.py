from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .geo import haversine_miles


@dataclass(slots=True)
class AircraftSnapshot:
    key: str
    hex: str
    callsign: str | None
    registration: str | None
    lat: float | None
    lon: float | None
    altitude_ft: int | None
    speed_kt: int | None
    heading: float | None
    vertical_rate_fpm: int | None
    category: str | None
    distance_mi: float | None
    seen_seconds: float | None
    seen_position_seconds: float | None
    received_at: str

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None and self.distance_mi is not None

    @property
    def is_helicopter(self) -> bool:
        return (self.category or "").upper() == "A7"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "hex": self.hex,
            "callsign": self.callsign,
            "registration": self.registration,
            "lat": self.lat,
            "lon": self.lon,
            "altitude_ft": self.altitude_ft,
            "speed_kt": self.speed_kt,
            "heading": self.heading,
            "vertical_rate_fpm": self.vertical_rate_fpm,
            "category": self.category,
            "distance_mi": self.distance_mi,
            "seen_seconds": self.seen_seconds,
            "seen_position_seconds": self.seen_position_seconds,
            "is_helicopter": self.is_helicopter,
            "received_at": self.received_at,
        }


@dataclass(slots=True)
class SourceStatus:
    source: str
    receiver_online: bool = False
    raw_aircraft_count: int = 0
    normalized_aircraft_count: int = 0
    last_success_at: str | None = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "receiver_online": self.receiver_online,
            "raw_aircraft_count": self.raw_aircraft_count,
            "normalized_aircraft_count": self.normalized_aircraft_count,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
        }


class ADSBReader:
    def __init__(self, source: str, home_lat: float, home_lon: float) -> None:
        self.source = source
        self.home_lat = home_lat
        self.home_lon = home_lon
        self.status = SourceStatus(source=source)

    async def read(self) -> list[AircraftSnapshot]:
        try:
            payload = await asyncio.to_thread(self._read_json)
            aircraft = payload.get("aircraft", [])
            if not isinstance(aircraft, list):
                aircraft = []

            received_at = utc_now_iso()
            snapshots = [
                snapshot
                for item in aircraft
                if isinstance(item, dict)
                for snapshot in [normalize_aircraft(item, self.home_lat, self.home_lon, received_at)]
                if snapshot is not None
            ]
            self.status.receiver_online = True
            self.status.raw_aircraft_count = len(aircraft)
            self.status.normalized_aircraft_count = len(snapshots)
            self.status.last_success_at = received_at
            self.status.last_error = None
            return snapshots
        except Exception as exc:  # noqa: BLE001 - status should carry reader failures.
            self.status.receiver_online = False
            self.status.raw_aircraft_count = 0
            self.status.normalized_aircraft_count = 0
            self.status.last_error = str(exc)
            return []

    def _read_json(self) -> dict[str, Any]:
        if self.source.startswith(("http://", "https://")):
            with urllib.request.urlopen(self.source, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))

        path = Path(self.source).expanduser()
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)


def normalize_aircraft(
    raw: dict[str, Any],
    home_lat: float,
    home_lon: float,
    received_at: str,
) -> AircraftSnapshot | None:
    hex_value = _clean_hex(raw.get("hex"))
    callsign = _clean_callsign(raw.get("flight") or raw.get("callsign"))
    registration = _clean(raw.get("r") or raw.get("registration") or raw.get("reg"))
    key = hex_value or callsign
    if not key:
        return None

    lat = _float(raw.get("lat"))
    lon = _float(raw.get("lon"))
    distance = None
    if _valid_lat_lon(lat, lon):
        distance = round(haversine_miles(home_lat, home_lon, lat, lon), 3)
    else:
        lat = None
        lon = None

    altitude = _altitude(_first_present(raw, "alt_baro", "alt_geom", "altitude", "alt"))

    speed = _int(_first_present(raw, "gs", "ias", "tas", "speed"))
    heading = _float(_first_present(raw, "track", "mag_heading", "true_heading"))
    vertical_rate = _int(_first_present(raw, "baro_rate", "geom_rate", "vert_rate"))

    return AircraftSnapshot(
        key=key.lower(),
        # Callsigns are useful as transient live-map keys, but are not stable
        # aircraft identities: they can change between flights and be reused by
        # different airframes.  Keep the persistent ICAO identity empty when a
        # receiver row does not provide one.
        hex=hex_value.lower() if hex_value else "",
        callsign=callsign,
        registration=registration,
        lat=lat,
        lon=lon,
        altitude_ft=altitude,
        speed_kt=speed,
        heading=heading,
        vertical_rate_fpm=vertical_rate,
        category=_clean(raw.get("category")),
        distance_mi=distance,
        seen_seconds=_float(raw.get("seen")),
        seen_position_seconds=_float(raw.get("seen_pos")),
        received_at=received_at,
    )


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_hex(value: Any) -> str | None:
    text = _clean(value)
    if not text:
        return None
    text = text.lower().replace("~", "")
    return text if all(char in "0123456789abcdef" for char in text) else None


def _clean_callsign(value: Any) -> str | None:
    text = _clean(value)
    if not text:
        return None
    return " ".join(text.split())


def _first_present(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if value is not None and value != "":
            return value
    return None


def _altitude(value: Any) -> int | None:
    if isinstance(value, str) and value.lower() == "ground":
        return 0
    return _int(value)


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _valid_lat_lon(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180
