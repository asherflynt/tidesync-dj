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
        claude_model=str(_get("claude_model", "claude-opus-4-8")),
        ma_host=str(_get("ma_host", "homeassistant.local")),
        ma_port=int(_get("ma_port", 8095)),
        dj_tick_interval=int(_get("dj_tick_interval", 30)),
        skip_penalty_seconds=int(_get("skip_penalty_seconds", 30)),
        vibe_input_text_entity=vibe_entity or None,
        data_dir=DATA_DIR,
    )
