from __future__ import annotations

import asyncio
import hmac
import ipaddress
import os
import re
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .adsb import ADSBReader
from .config import AppConfig, load_config, update_config_file
from .db import Database
from .discord import DiscordNotifier
from .receiver_control import start_windows_receiver
from .tracker import SkyLedgerTracker

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"


async def require_admin_access(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """Allow local administration, or remote administration with a bearer token."""
    host = request.client.host if request.client else ""
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        if host.lower() == "localhost":
            return

    expected = os.environ.get("SKYLEDGER_ADMIN_TOKEN", "")
    scheme, _, supplied = (authorization or "").partition(" ")
    if expected and scheme.lower() == "bearer" and hmac.compare_digest(supplied, expected):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            "Administrative actions are limited to localhost. Set SKYLEDGER_ADMIN_TOKEN "
            "and send it as a Bearer token to administer remotely."
        ),
    )


class MapZoomRequest(BaseModel):
    map_zoom_level: int = Field(ge=0, le=19)


class HomeSettingsRequest(BaseModel):
    home_name: str = Field(min_length=1, max_length=80)
    home_lat: float = Field(ge=-90, le=90)
    home_lon: float = Field(ge=-180, le=180)


class AircraftMarkerColorsRequest(BaseModel):
    aircraft_marker_low_color: str
    aircraft_marker_default_color: str


class ConnectionManager:
    SEND_TIMEOUT_SECONDS = 5.0

    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self.connections.discard(websocket)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        async def send(websocket: WebSocket) -> None:
            try:
                await asyncio.wait_for(
                    websocket.send_json(payload),
                    timeout=self.SEND_TIMEOUT_SECONDS,
                )
            except Exception:  # noqa: BLE001 - one broken client must not stop fan-out.
                self.disconnect(websocket)

        await asyncio.gather(*(send(websocket) for websocket in tuple(self.connections)))


async def save_config(config_path: str | None, updates: dict[str, Any]) -> AppConfig:
    try:
        return await asyncio.to_thread(update_config_file, config_path, updates)
    except PermissionError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Config file is not writable by SkyLedger: {exc}",
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not save config file: {exc}",
        ) from exc


def create_app(config_path: str | None = None) -> FastAPI:
    config = load_config(config_path)
    manager = ConnectionManager()
    db = Database(config.database_path)
    reader = ADSBReader(config.adsb_json_path, config.home_lat, config.home_lon)
    notifier = DiscordNotifier(config.discord_webhook_url, config.enable_discord_alerts)
    tracker = SkyLedgerTracker(config, db, reader, notifier, manager.broadcast)

    app = FastAPI(title="SkyLedger", version="0.1.0")
    app.state.config = config
    app.state.db = db
    app.state.tracker = tracker
    app.state.manager = manager
    app.state.tracker_task = None

    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.on_event("startup")
    async def startup() -> None:
        await asyncio.to_thread(db.init)
        app.state.tracker_task = asyncio.create_task(tracker.run())

    @app.on_event("shutdown")
    async def shutdown() -> None:
        tracker.stop()
        task = app.state.tracker_task
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @app.get("/")
    async def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/history")
    async def history_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "history.html")

    @app.get("/aircraft/{hex_value}")
    async def aircraft_page(hex_value: str) -> FileResponse:
        return FileResponse(STATIC_DIR / "aircraft.html")

    @app.get("/settings")
    async def settings_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "settings.html")

    @app.websocket("/ws/live")
    async def live_ws(websocket: WebSocket) -> None:
        await manager.connect(websocket)
        try:
            if tracker.last_payload:
                await websocket.send_json(tracker.last_payload)
            while True:
                await websocket.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            manager.disconnect(websocket)

    @app.get("/api/status")
    async def api_status() -> dict[str, Any]:
        return tracker.status_payload()

    @app.get("/api/aircraft/live")
    async def api_live_aircraft() -> dict[str, Any]:
        return await tracker.build_payload_async()

    @app.get("/api/events/recent")
    async def api_recent_events(limit: int = 20) -> list[dict[str, Any]]:
        return await asyncio.to_thread(db.get_recent_events, min(max(limit, 1), 100))

    @app.get("/api/stats/today")
    async def api_today_stats() -> dict[str, Any]:
        data = await asyncio.to_thread(db.get_dashboard_data)
        return data["today"]

    @app.get("/api/aircraft/{hex_value}")
    async def api_aircraft(hex_value: str) -> dict[str, Any]:
        record = await asyncio.to_thread(db.get_aircraft_record, hex_value)
        if not record:
            raise HTTPException(status_code=404, detail="Aircraft has not been logged yet.")
        return {
            "aircraft": record,
            "events": await asyncio.to_thread(db.get_aircraft_history, hex_value, 100),
        }

    @app.get("/api/history")
    async def api_history(limit: int = 200) -> list[dict[str, Any]]:
        return await asyncio.to_thread(db.get_history, min(max(limit, 1), 500))

    @app.post("/api/test-discord", dependencies=[Depends(require_admin_access)])
    async def api_test_discord() -> dict[str, Any]:
        ok, error = await notifier.send_test()
        return {"ok": ok, "error": error}

    @app.post("/api/test-countdown", dependencies=[Depends(require_admin_access)])
    async def api_test_countdown() -> dict[str, Any]:
        return await tracker.trigger_test_countdown()

    @app.post("/api/receiver/start", dependencies=[Depends(require_admin_access)])
    async def api_start_receiver() -> dict[str, Any]:
        return await asyncio.to_thread(start_windows_receiver, config.adsb_json_path, PACKAGE_DIR.parent)

    @app.post("/api/history/clear", dependencies=[Depends(require_admin_access)])
    async def api_clear_history() -> dict[str, Any]:
        counts = await tracker.clear_history()
        return {"ok": True, "deleted": counts}

    @app.post("/api/settings/map-zoom", dependencies=[Depends(require_admin_access)])
    async def api_update_map_zoom(request: MapZoomRequest) -> dict[str, Any]:
        updated = await save_config(config_path, {"map_zoom_level": request.map_zoom_level})
        config.map_zoom_level = updated.map_zoom_level
        tracker.last_payload = await tracker.build_payload_async()
        await manager.broadcast(tracker.last_payload)
        return {"ok": True, "map_zoom_level": config.map_zoom_level}

    @app.post("/api/settings/home", dependencies=[Depends(require_admin_access)])
    async def api_update_home_settings(request: HomeSettingsRequest) -> dict[str, Any]:
        home_name = request.home_name.strip()
        if not home_name:
            raise HTTPException(status_code=422, detail="Home name is required.")

        updated = await save_config(
            config_path,
            {
                "home_name": home_name,
                "home_lat": request.home_lat,
                "home_lon": request.home_lon,
            },
        )
        config.home_name = updated.home_name
        config.home_lat = updated.home_lat
        config.home_lon = updated.home_lon
        reader.home_lat = updated.home_lat
        reader.home_lon = updated.home_lon
        tracker.last_payload = await tracker.build_payload_async()
        await manager.broadcast(tracker.last_payload)
        return {
            "ok": True,
            "home_name": config.home_name,
            "home_lat": config.home_lat,
            "home_lon": config.home_lon,
        }

    @app.post("/api/settings/aircraft-marker-colors", dependencies=[Depends(require_admin_access)])
    async def api_update_aircraft_marker_colors(request: AircraftMarkerColorsRequest) -> dict[str, Any]:
        low_color = _normalize_hex_color(request.aircraft_marker_low_color, "low-altitude marker color")
        default_color = _normalize_hex_color(
            request.aircraft_marker_default_color,
            "standard aircraft marker color",
        )
        updated = await save_config(
            config_path,
            {
                "aircraft_marker_low_color": low_color,
                "aircraft_marker_default_color": default_color,
            },
        )
        config.aircraft_marker_low_color = updated.aircraft_marker_low_color
        config.aircraft_marker_default_color = updated.aircraft_marker_default_color
        tracker.last_payload = await tracker.build_payload_async()
        await manager.broadcast(tracker.last_payload)
        return {
            "ok": True,
            "aircraft_marker_low_color": config.aircraft_marker_low_color,
            "aircraft_marker_default_color": config.aircraft_marker_default_color,
        }

    return app


def _normalize_hex_color(value: str, label: str) -> str:
    color = value.strip().lower()
    if not re.fullmatch(r"#[0-9a-f]{6}", color):
        raise HTTPException(status_code=422, detail=f"Invalid {label}. Use #rrggbb.")
    return color


app = create_app()
