from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db import Database


@dataclass(slots=True)
class EnrichmentResult:
    registration: str | None = None
    aircraft_type: str | None = None
    operator: str | None = None
    photo_url: str | None = None
    route_from: str | None = None
    route_to: str | None = None
    source: str | None = None


class EnrichmentProvider:
    """Cache-first flight route enrichment using ADSBDB's public API."""

    POSITIVE_CACHE_TTL = timedelta(days=7)
    NEGATIVE_CACHE_TTL = timedelta(hours=6)

    def __init__(
        self,
        enabled: bool,
        db: Database | None = None,
        base_url: str = "https://api.adsbdb.com/v0/callsign",
    ) -> None:
        self.enabled = enabled
        self.db = db
        self.base_url = base_url.rstrip("/")
        self._lookup_lock = asyncio.Lock()

    async def lookup(self, callsign: str | None) -> EnrichmentResult | None:
        normalized = _normalize_callsign(callsign)
        if not self.enabled or not normalized:
            return None
        cached = await self._cached(normalized)
        if cached is not None:
            return cached

        async with self._lookup_lock:
            cached = await self._cached(normalized)
            if cached is not None:
                return cached
            result, cacheable = await asyncio.to_thread(self._fetch, normalized)
            if cacheable and self.db:
                await asyncio.to_thread(
                    self.db.cache_flight_route,
                    normalized,
                    result.route_from if result else None,
                    result.route_to if result else None,
                    "adsbdb",
                )
            return result

    async def _cached(self, callsign: str) -> EnrichmentResult | None:
        if not self.db:
            return None
        row = await asyncio.to_thread(self.db.get_cached_flight_route, callsign)
        if not row:
            return None
        updated = _parse_utc(row.get("last_updated"))
        has_route = bool(row.get("route_from") and row.get("route_to"))
        ttl = self.POSITIVE_CACHE_TTL if has_route else self.NEGATIVE_CACHE_TTL
        if updated is None or datetime.now(timezone.utc) - updated >= ttl:
            return None
        return EnrichmentResult(
            route_from=row.get("route_from"),
            route_to=row.get("route_to"),
            source=row.get("source"),
        )

    def _fetch(self, callsign: str) -> tuple[EnrichmentResult | None, bool]:
        url = f"{self.base_url}/{urllib.parse.quote(callsign, safe='')}"
        request = urllib.request.Request(url, headers={"User-Agent": "SkyLedger/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return (None, True) if exc.code == 404 else (None, False)
        except (OSError, ValueError, json.JSONDecodeError):
            return None, False

        route = payload.get("response", {}).get("flightroute")
        if not isinstance(route, dict):
            return None, True
        route_from = _airport_code(route.get("origin"))
        route_to = _airport_code(route.get("destination"))
        if not route_from or not route_to:
            return None, True
        return EnrichmentResult(
            route_from=route_from,
            route_to=route_to,
            source="adsbdb",
        ), True


def _normalize_callsign(value: str | None) -> str | None:
    text = "".join((value or "").upper().split())
    return text if text and text.isalnum() else None


def _airport_code(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    code = value.get("iata_code") or value.get("icao_code")
    text = str(code or "").strip().upper()
    return text or None


def _parse_utc(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
