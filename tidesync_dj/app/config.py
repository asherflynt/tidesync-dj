"""Add-on configuration loaded from /data/options.json.

Home Assistant writes the user's add-on options to /data/options.json at
runtime (from the `options`/`schema` in config.yaml). Persistent state lives
under /data, which survives add-on restarts.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(os.environ.get("TIDESYNC_DATA_DIR", "/data"))
OPTIONS_PATH = DATA_DIR / "options.json"


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str
    claude_model: str
    ma_host: str
    ma_port: int
    dj_tick_interval: int
    skip_penalty_seconds: int
    vibe_input_text_entity: str | None
    ma_username: str
    ma_password: str
    ma_token: str
    # MA music provider used when saving a session playlist. Blank = auto-detect
    # from the tracks that played; otherwise this forces a specific provider.
    playlist_provider: str
    # Optional HA entities whose state colours the DJ's mood (blank = ignored).
    weather_entity: str
    temperature_entity: str
    # Optional HA input_text/input_select whose value drives an action (play,
    # stop, skip, nudge, energy up/down, "vibe: …", "player: …"). Blank = off.
    ha_action_entity: str
    data_dir: Path

    @property
    def ma_ws_url(self) -> str:
        return f"ws://{self.ma_host}:{self.ma_port}/ws"


def _load_options() -> dict:
    if OPTIONS_PATH.exists():
        with OPTIONS_PATH.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    # Fallback for local development outside the add-on container.
    return {}


def _clean_host(raw: str) -> str:
    """Reduce a user-entered host to a bare hostname/IP.

    Tolerates someone pasting a full URL with scheme and/or port into the host
    field (e.g. 'http://192.168.2.6:8095' -> '192.168.2.6'). The port comes from
    the dedicated ma_port option, so any embedded port here is dropped.
    """
    raw = (raw or "").strip()
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    raw = raw.split("/", 1)[0]  # drop any path
    raw = raw.split(":", 1)[0]  # drop any embedded port
    return raw or "homeassistant.local"


def load_config() -> Config:
    opts = _load_options()

    def _get(key: str, default):
        # Environment overrides make local testing easy.
        env_key = f"TIDESYNC_{key.upper()}"
        if env_key in os.environ:
            return os.environ[env_key]
        return opts.get(key, default)

    vibe_entity = _get("vibe_input_text_entity", "") or ""

    return Config(
        anthropic_api_key=str(_get("anthropic_api_key", "")),
        claude_model=str(_get("claude_model", "claude-sonnet-4-6")),
        ma_host=_clean_host(str(_get("ma_host", "homeassistant.local"))),
        ma_port=int(_get("ma_port", 8095)),
        dj_tick_interval=int(_get("dj_tick_interval", 30)),
        skip_penalty_seconds=int(_get("skip_penalty_seconds", 30)),
        vibe_input_text_entity=vibe_entity or None,
        ma_username=str(_get("ma_username", "")),
        ma_password=str(_get("ma_password", "")),
        ma_token=str(_get("ma_token", "")),
        # New generic key, falling back to the legacy `tidal_provider` so existing
        # installs keep working. Default "tidal" preserves prior behaviour.
        playlist_provider=str(
            _get("playlist_provider", "") or _get("tidal_provider", "") or "tidal"
        ),
        weather_entity=str(_get("weather_entity", "") or ""),
        temperature_entity=str(_get("temperature_entity", "") or ""),
        ha_action_entity=str(_get("ha_action_entity", "") or ""),
        data_dir=DATA_DIR,
    )
