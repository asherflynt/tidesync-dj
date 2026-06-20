"""Persistent taste-memory manager.

Stored at /data/taste_profile.json so it survives add-on restarts. Bootstrapped
on first run from a library sample, then refreshed incrementally every N DJ
decisions.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

UPDATE_EVERY_N_DECISIONS = 20


class TasteProfile:
    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "taste_profile.json"
        self._data: dict[str, Any] = self._default()
        self._load()

    @staticmethod
    def _default() -> dict[str, Any]:
        return {
            "summary": "",
            "favorite_artists": [],
            "session_count": 0,
            "decision_count": 0,
            "last_updated": None,
        }

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = {**self._default(), **json.loads(self._path.read_text())}
            except (ValueError, OSError) as err:
                _LOGGER.warning("Could not load taste profile: %s", err)

    def _save(self) -> None:
        self._data["last_updated"] = datetime.now(timezone.utc).isoformat()
        try:
            self._path.write_text(json.dumps(self._data, indent=2))
        except OSError as err:
            _LOGGER.warning("Could not save taste profile: %s", err)

    # ------------------------------------------------------------------ #
    @property
    def summary(self) -> str:
        return self._data.get("summary", "")

    @property
    def is_bootstrapped(self) -> bool:
        return bool(self._data.get("summary"))

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)

    def start_session(self) -> None:
        self._data["session_count"] = self._data.get("session_count", 0) + 1
        self._save()

    def record_decision(self) -> bool:
        """Increment the decision counter; return True when an update is due."""
        self._data["decision_count"] = self._data.get("decision_count", 0) + 1
        self._save()
        return self._data["decision_count"] % UPDATE_EVERY_N_DECISIONS == 0

    def set_summary(self, summary: str) -> None:
        if summary:
            self._data["summary"] = summary
            self._save()

    async def bootstrap(self, brain, library_sample: list[dict[str, Any]]) -> None:
        """One-time deep analysis on first run."""
        if self.is_bootstrapped or not library_sample:
            return
        _LOGGER.info("Bootstrapping taste profile from %d tracks", len(library_sample))
        summary = await brain.summarize_taste(library_sample)
        self.set_summary(summary)

    async def maybe_update(self, brain, recent: list[dict[str, Any]]) -> None:
        """Refine the summary using recent listening, called every N decisions."""
        if not recent:
            return
        _LOGGER.info("Refreshing taste profile summary")
        summary = await brain.summarize_taste(recent, previous=self.summary)
        self.set_summary(summary)

    async def seed_from_tracks(
        self, brain, tracks: list[dict[str, Any]], source: str = "playlist"
    ) -> str:
        """Explicit user-driven seed (e.g. a YouTube Music playlist).

        Refines the existing summary with the seed tracks so prior context isn't
        lost, then persists. Returns the new summary.
        """
        if not tracks:
            return self.summary
        _LOGGER.info("Seeding taste profile from %d %s tracks", len(tracks), source)
        summary = await brain.summarize_taste(tracks, previous=self.summary)
        self.set_summary(summary)
        self._data["seeded_from"] = source
        self._save()
        return summary
