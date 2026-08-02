from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from skyledger.adsb import normalize_aircraft
from skyledger.config import AppConfig
from skyledger.db import Database
from skyledger.discord import DiscordNotifier
from skyledger.enrichment import EnrichmentProvider, EnrichmentResult
from skyledger.tracker import SkyLedgerTracker
from tests.test_core import FakeReader


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_adsbdb_response_uses_iata_codes() -> None:
    provider = EnrichmentProvider(True)
    response = {
        "response": {
            "flightroute": {
                "origin": {"iata_code": "ORD", "icao_code": "KORD"},
                "destination": {"iata_code": "ATL", "icao_code": "KATL"},
            }
        }
    }
    with patch("skyledger.enrichment.urllib.request.urlopen", return_value=FakeResponse(response)):
        result, cacheable = provider._fetch("UAL123")

    assert cacheable is True
    assert result is not None
    assert result.route_from == "ORD"
    assert result.route_to == "ATL"


def test_cached_route_avoids_network_lookup() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database = Database(str(Path(tmp) / "ledger.db"))
        database.init()
        database.cache_flight_route("UAL123", "ORD", "ATL", "adsbdb")
        provider = EnrichmentProvider(True, database)

        with patch("skyledger.enrichment.urllib.request.urlopen") as urlopen:
            result = asyncio.run(provider.lookup(" ual123 "))

        urlopen.assert_not_called()
        assert result is not None
        assert (result.route_from, result.route_to) == ("ORD", "ATL")


def test_closest_aircraft_payload_contains_route() -> None:
    class FakeEnricher:
        async def lookup(self, callsign):
            assert callsign == "UAL123"
            return EnrichmentResult(route_from="ORD", route_to="ATL", source="test")

    with tempfile.TemporaryDirectory() as tmp:
        database = Database(str(Path(tmp) / "ledger.db"))
        database.init()
        snapshot = normalize_aircraft(
            {
                "hex": "ABC123",
                "flight": "UAL123",
                "lat": 41.6,
                "lon": -88.0,
                "alt_baro": 5000,
            },
            41.5733224,
            -87.98208,
            "2026-08-02T12:00:00Z",
        )
        assert snapshot is not None
        tracker = SkyLedgerTracker(
            AppConfig(),
            database,
            FakeReader([snapshot]),
            DiscordNotifier("", False),
            enricher=FakeEnricher(),
        )

        payload = asyncio.run(tracker.tick())

        assert payload["closest_aircraft"]["route_from"] == "ORD"
        assert payload["closest_aircraft"]["route_to"] == "ATL"
