from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from skyledger.config import load_config, update_config_file


def test_concurrent_config_updates_do_not_lose_changes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.yaml"
        path.write_text("home_name: Original\n", encoding="utf-8")

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(update_config_file, str(path), {"map_zoom_level": 16}),
                executor.submit(update_config_file, str(path), {"home_name": "Hangar"}),
            ]
            for future in futures:
                future.result()

        config = load_config(str(path))
        assert config.map_zoom_level == 16
        assert config.home_name == "Hangar"


def test_failed_config_serialization_preserves_original_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.yaml"
        original = "home_name: Original\n"
        path.write_text(original, encoding="utf-8")

        with patch("skyledger.config.yaml.safe_dump", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                update_config_file(str(path), {"home_name": "Hangar"})

        assert path.read_text(encoding="utf-8") == original
        assert list(path.parent.glob(f".{path.name}.*.tmp")) == []
