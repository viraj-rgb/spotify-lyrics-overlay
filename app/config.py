"""Persistent settings, stored as JSON next to the lyric cache.

Everything here is optional. The app runs with no configuration at all: LRCLIB
needs no API key and SMTC needs no credentials, so a fresh install works on
first launch. The Spotify fields below only enable the optional queue-based
preloading described in the README.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_DIR = Path.home() / ".lyric-overlay"
CONFIG_PATH = CONFIG_DIR / "config.json"
CACHE_DIR = CONFIG_DIR / "cache"


@dataclass
class Settings:
    # Window geometry, written whenever the user finishes a move or resize.
    x: Optional[int] = None
    y: Optional[int] = None
    w: int = 660
    h: int = 230

    # Appearance. `plate_core`/`plate_edge` are the card's opacity at its centre
    # and its edges; text is never dimmed with alpha, only the plate.
    plate_core: float = 0.75
    plate_edge: float = 0.64
    text_scale: float = 1.0

    # Tint the card from the album cover. Only the hue is taken; lightness is
    # forced dark so legibility never depends on the artwork.
    theme_from_album: bool = True

    always_on_top: bool = True
    start_with_windows: bool = False

    # Whether the "it's still running in the tray" notice has been shown. Once
    # is informative; every time is nagging.
    tray_hint_shown: bool = False

    # Optional Spotify Web API credentials. Only a client ID is needed -- the
    # app uses the PKCE flow, which is designed for apps that cannot keep a
    # secret. Leave blank to run entirely on SMTC.
    spotify_client_id: str = ""
    spotify_refresh_token: str = ""
    preload_from_queue: bool = True

    def clamped(self) -> "Settings":
        """Pull values into supported ranges, in case the file was hand-edited."""
        self.plate_core = min(max(self.plate_core, 0.10), 1.0)
        self.plate_edge = min(max(self.plate_edge, 0.05), 1.0)
        self.text_scale = min(max(self.text_scale, 0.6), 2.0)
        self.w = min(max(int(self.w), 340), 1800)
        self.h = min(max(int(self.h), 130), 900)
        return self


class ConfigStore:
    """Loads and saves `Settings`, tolerating a missing or corrupt file.

    Writes go through a temporary file and a replace, so an interrupted save
    cannot leave a half-written config that fails to parse next launch.
    """

    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self._lock = threading.Lock()
        self.settings = self._load()

    def _load(self) -> Settings:
        try:
            raw: Dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return Settings()

        known = {f for f in Settings.__dataclass_fields__}
        return Settings(**{k: v for k, v in raw.items() if k in known}).clamped()

    def save(self) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.path.with_suffix(".json.tmp")
                temp.write_text(json.dumps(asdict(self.settings), indent=2), encoding="utf-8")
                temp.replace(self.path)
            except OSError:
                pass

    def update(self, **changes: Any) -> None:
        for key, value in changes.items():
            if hasattr(self.settings, key):
                setattr(self.settings, key, value)
        self.settings.clamped()
        self.save()
