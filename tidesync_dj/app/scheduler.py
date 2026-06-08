"""Background DJ engine.

Drives decision cycles from two triggers:
  * Primary: a Music Assistant `queue_updated` event when items_remaining < 2.
  * Secondary: a polling fallback every `dj_tick_interval` seconds.

Also performs skip detection (a track change within `skip_penalty_seconds` of
the track starting is treated as a skip) and assembles the structured context
payload handed to Claude.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime
from typing import Any

from claude_brain import ClaudeBrain
from config import Config
from ha_client import HAClient
from ma_client import MusicAssistantClient, EVENT_QUEUE_UPDATED
from taste_profile import TasteProfile

_LOGGER = logging.getLogger(__name__)

ENQUEUE_THRESHOLD = 2  # tick when fewer than this many tracks remain


def _time_of_day(now: datetime | None = None) -> str:
    hour = (now or datetime.now()).hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    if 21 <= hour < 24:
        return "night"
    return "late_night"  # 00:00 - 04:59


def _track_label(item: dict[str, Any] | None) -> str | None:
    if not item:
        return None
    media = item.get("media_item") or item
    name = media.get("name") or item.get("name")
    artists = media.get("artists") or []
    artist = artists[0].get("name") if artists else media.get("artist")
    if name and artist:
        return f"{artist} - {name}"
    return name


class DJEngine:
    def __init__(
        self,
        config: Config,
        ma: MusicAssistantClient,
        ha: HAClient,
        brain: ClaudeBrain,
        taste: TasteProfile,
    ) -> None:
        self._config = config
        self._ma = ma
        self._ha = ha
        self._brain = brain
        self._taste = taste

        self.vibe_prompt: str = ""
        self.decision_log: deque[dict[str, Any]] = deque(maxlen=50)
        self.recent_skips: list[dict[str, Any]] = []
        self.session_started = time.monotonic()
        self.stats = {"tracks_played": 0, "skips": 0, "discoveries": 0}

        self._current_track_id: str | None = None
        self._current_track_started = time.monotonic()
        self._last_current: dict[str, Any] | None = None
        self._tick_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._stopping = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self._taste.start_session()
        self._ma.on_event(self._on_ma_event)
        self._tasks.append(asyncio.create_task(self._ma.run_forever()))
        self._tasks.append(asyncio.create_task(self._poll_loop()))
        if self._config.vibe_input_text_entity:
            self._tasks.append(asyncio.create_task(self._vibe_poll_loop()))
        await self._bootstrap_taste()

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        await self._ma.close()
        await self._ha.close()

    async def _bootstrap_taste(self) -> None:
        if self._taste.is_bootstrapped:
            return
        try:
            library = await self._ma.get_library_tracks(limit=150)
            await self._taste.bootstrap(self._brain, library)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Taste bootstrap skipped: %s", err)

    # ------------------------------------------------------------------ #
    # Triggers
    # ------------------------------------------------------------------ #
    async def _on_ma_event(self, event: str, data: dict[str, Any]) -> None:
        if event != EVENT_QUEUE_UPDATED:
            return
        self._detect_skip(data)
        remaining = data.get("items_remaining")
        if remaining is None:
            queue = await self._ma.get_queue()
            remaining = queue.get("items_remaining", 0)
        if remaining < ENQUEUE_THRESHOLD:
            await self.tick(reason="queue_low")

    async def _poll_loop(self) -> None:
        interval = max(self._config.dj_tick_interval, 10)
        while not self._stopping:
            await asyncio.sleep(interval)
            try:
                queue = await self._ma.get_queue()
                self._detect_skip(queue)
                if queue.get("items_remaining", 0) < ENQUEUE_THRESHOLD:
                    await self.tick(reason="poll")
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Poll tick error: %s", err)

    async def _vibe_poll_loop(self) -> None:
        """Mirror an HA input_text helper into the active vibe (stretch goal)."""
        entity = self._config.vibe_input_text_entity
        while not self._stopping:
            value = await self._ha.get_state_value(entity)  # type: ignore[arg-type]
            if value and value != self.vibe_prompt:
                _LOGGER.info("Vibe updated from %s: %s", entity, value)
                self.vibe_prompt = value
            await asyncio.sleep(15)

    # ------------------------------------------------------------------ #
    # Skip detection
    # ------------------------------------------------------------------ #
    def _detect_skip(self, queue_or_event: dict[str, Any]) -> None:
        current = queue_or_event.get("current_item")
        track_id = None
        if current:
            track_id = current.get("queue_item_id") or current.get("uri")
        if track_id == self._current_track_id:
            return

        # Track changed — was the previous one skipped?
        elapsed = time.monotonic() - self._current_track_started
        if (
            self._current_track_id is not None
            and elapsed < self._config.skip_penalty_seconds
        ):
            label = _track_label(self._last_current)
            self.stats["skips"] += 1
            skip = {"track": label, "elapsed_seconds": round(elapsed, 1)}
            self.recent_skips.append(skip)
            self.recent_skips = self.recent_skips[-10:]
            artist = label.split(" - ")[0] if label else None
            self._taste.note_skip(artist)
            _LOGGER.info("Detected skip: %s after %.1fs", label, elapsed)
        elif self._current_track_id is not None:
            self.stats["tracks_played"] += 1

        self._current_track_id = track_id
        self._current_track_started = time.monotonic()
        self._last_current = current

    # ------------------------------------------------------------------ #
    # Decision cycle
    # ------------------------------------------------------------------ #
    async def build_context(self) -> dict[str, Any]:
        queue = await self._ma.get_queue()
        history = await self._ma.get_history(n=20)
        duration_mins = round((time.monotonic() - self.session_started) / 60)
        return {
            "taste_profile": self._taste.summary,
            "recent_history": [_track_label(i) for i in history],
            "current_track": _track_label(queue.get("current_item")),
            "queue": [_track_label(i) for i in queue.get("items", [])],
            "recent_skips": self.recent_skips,
            "avoid_artists": self._taste.avoid_artists,
            "vibe_prompt": self.vibe_prompt or None,
            "time_of_day": _time_of_day(),
            "listening_duration_mins": duration_mins,
        }

    async def tick(self, reason: str = "manual") -> dict[str, Any]:
        # Serialize ticks so overlapping triggers don't double-enqueue.
        async with self._tick_lock:
            context = await self.build_context()
            try:
                decision = await self._brain.decide(context)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("DJ decision failed: %s", err)
                return {"ok": False, "error": str(err)}

            queries = [t.query for t in decision.next_tracks]
            enqueued = await self._ma.enqueue_queries(queries)
            self.stats["discoveries"] += len(enqueued)

            entry = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "reason": reason,
                "dj_note": decision.dj_note,
                "mood_shift": decision.mood_shift,
                "mood_shift_reason": decision.mood_shift_reason,
                "tracks": [
                    {"query": t.query, "reason": t.reason}
                    for t in decision.next_tracks
                ],
                "enqueued": len(enqueued),
            }
            self.decision_log.appendleft(entry)

            # Emit an HA event so users can trigger automations off DJ decisions.
            await self._ha.fire_event(
                "tidesync_dj_decision",
                {
                    "dj_note": decision.dj_note,
                    "mood_shift": decision.mood_shift,
                    "enqueued": len(enqueued),
                    "vibe": self.vibe_prompt,
                },
            )

            # Periodically refine the taste profile.
            if self._taste.record_decision():
                history = await self._ma.get_history(n=30)
                await self._taste.maybe_update(self._brain, history)

            return {"ok": True, "decision": entry}

    # ------------------------------------------------------------------ #
    # Read-only views for the API/UI
    # ------------------------------------------------------------------ #
    async def status(self) -> dict[str, Any]:
        # Stay resilient during onboarding / before MA is reachable.
        try:
            queue = await self._ma.get_queue()
        except Exception:  # noqa: BLE001
            queue = {}
        return {
            "configured": bool(self._config.anthropic_api_key),
            "now_playing": _track_label(queue.get("current_item")),
            "vibe": self.vibe_prompt or None,
            "time_of_day": _time_of_day(),
            "items_remaining": queue.get("items_remaining", 0),
            "ma_connected": self._ma.active_queue_id is not None,
            "ma_host": self._config.ma_host,
            "session_minutes": round((time.monotonic() - self.session_started) / 60),
            "stats": self.stats,
            "model": self._config.claude_model,
        }
