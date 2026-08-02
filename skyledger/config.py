from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

import yaml


_CONFIG_WRITE_LOCK = threading.Lock()


DEFAULT_CONFIG_PATHS = (
    "config.local.yaml",
    "config.yaml",
    "/etc/skyledger/config.yaml",
)


@dataclass(slots=True)
class AppConfig:
    home_lat: float = 0.0
    home_lon: float = 0.0
    home_name: str = "SkyLedger"
    adsb_json_path: str = "/run/readsb/aircraft.json"
    discord_webhook_url: str = ""
    dashboard_port: int = 8000
    alert_radius_miles: float = 0.5
    guess_trigger_radius_miles: float = 1.2
    reveal_radius_miles: float = 0.5
    tracking_radius_miles: float = 0.0
    max_alert_altitude_ft: int = 10000
    countdown_seconds: int = 10
    reveal_duration_seconds: int = 25
    alert_cooldown_minutes: int = 10
    raw_position_retention_days: int = 14
    live_aircraft_timeout_seconds: float = 10.0
    enable_discord_alerts: bool = False
    enable_enrichment: bool = False
    dashboard_title: str = "SkyLedger"
    database_path: str = "skyledger.db"
    tar1090_url: str = "http://localhost/tar1090/"
    poll_interval_seconds: float = 1.0
    map_zoom_level: int = 13
    map_tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    aircraft_marker_low_color: str = "#61f4a8"
    aircraft_marker_default_color: str = "#6ee7ff"

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["discord_webhook_url"] = "configured" if self.discord_webhook_url else ""
        return data


def load_config(path: str | None = None) -> AppConfig:
    config_path = resolve_config_path(path)
    raw: dict[str, Any] = {}
    if config_path and config_path.exists():
        with config_path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"Config file must contain a YAML mapping: {config_path}")
            raw = loaded

    allowed = {field.name: field for field in fields(AppConfig)}
    unknown = set(raw) - set(allowed)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"Unknown config field(s): {names}")

    values: dict[str, Any] = {}
    for name, field in allowed.items():
        if name in raw:
            values[name] = _coerce(raw[name], field.type)

    return AppConfig(**values)


def resolve_config_path(path: str | None = None) -> Path | None:
    explicit = path or os.environ.get("SKYLEDGER_CONFIG")
    if explicit:
        return Path(explicit).expanduser()

    for candidate in DEFAULT_CONFIG_PATHS:
        resolved = Path(candidate).expanduser()
        if resolved.exists():
            return resolved
    return None


def update_config_file(path: str | None, updates: dict[str, Any]) -> AppConfig:
    config_path = resolve_config_path(path) or Path("config.yaml")
    allowed = {field.name: field for field in fields(AppConfig)}
    unknown = set(updates) - set(allowed)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"Unknown config field(s): {names}")

    with _CONFIG_WRITE_LOCK:
        raw: dict[str, Any] = {}
        existing_mode: int | None = None
        if config_path.exists():
            existing_mode = config_path.stat().st_mode & 0o777
            with config_path.open("r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
                if not isinstance(loaded, dict):
                    raise ValueError(f"Config file must contain a YAML mapping: {config_path}")
                raw = loaded

        for key, value in updates.items():
            raw[key] = _coerce(value, allowed[key].type)

        config_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=config_path.parent,
                prefix=f".{config_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                yaml.safe_dump(raw, handle, sort_keys=False)
                handle.flush()
                os.fsync(handle.fileno())
            if existing_mode is not None:
                temp_path.chmod(existing_mode)
            os.replace(temp_path, config_path)
            temp_path = None
            _fsync_directory(config_path.parent)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    return load_config(str(config_path))


def _fsync_directory(path: Path) -> None:
    """Persist an atomic rename where the platform supports directory fsync."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _coerce(value: Any, annotation: Any) -> Any:
    if isinstance(annotation, str):
        annotation = {
            "float": float,
            "int": int,
            "str": str,
            "bool": bool,
        }.get(annotation, annotation)

    if annotation is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if annotation is int:
        return int(value)
    if annotation is float:
        return float(value)
    if annotation is str:
        return "" if value is None else str(value)
    return value
