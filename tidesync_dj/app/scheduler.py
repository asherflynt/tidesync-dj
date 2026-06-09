"""Background DJ engine.

Drives decision cycles from two triggers:
  * Primary: a Music Assistant `queue_updated` event when items_remaining < 2.
  * Secondary: a polling fallback every `dj_tick_interval` seconds.

Tracks the currently-playing song for stats and the like/block path, and
assembles the structured context payload handed to Claude. Skips are recorded
only when the user presses the TideSync skip button — there is no time-based
skip detection (it produced false skips on queue clears / rebuilds).
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
from ma_client import MusicAssistantClient, EVENT_QUEUE_UPDATED, EVENT_QUEUE_TIME_UPDATED
from taste_profile import TasteProfile
from user_memory import UserStore

_LOGGER = logging.getLogger(__name__)

ENQUEUE_THRESHOLD = 5   # tick when fewer than this many tracks remain
QUEUE_TARGET = 30       # how many tracks to request from Claude per decision


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
    first = artists[0] if artists else None
    artist = (first.get("name") if isinstance(first, dict) else first) if first else media.get("artist")
    if name and artist:
        return f"{artist} - {name}"
    return name


def _track_uri(item: dict[str, Any] | None) -> str | None:
    if not item:
        return None
    media = item.get("media_item") or item
    return media.get("uri") or item.get("uri")


class DJEngine:
    def __init__(
        self,
        config: Config,
        ma: MusicAssistantClient,
        ha: HAClient,
        brain: ClaudeBrain,
        taste: TasteProfile,
        users: UserStore,
    ) -> None:
        self._config = config
        self._ma = ma
        self._ha = ha
        self._brain = brain
        self._taste = taste
        self._users = users

        self.vibe_prompt: str = ""
        self.decision_log: deque[dict[str, Any]] = deque(maxlen=50)
        # Soft, session-only skip signal. Populated ONLY by the UI skip button —
        # never by automatic track-change detection — and never persisted.
        self.recent_skips: list[dict[str, Any]] = []
        self.session_started = time.monotonic()
        self.stats = {"tracks_played": 0, "skips": 0, "discoveries": 0}
        # Ordered, de-duplicated track URIs seen this session (for "save as
        # playlist"). dict preserves insertion order and dedupes by key.
        self._session_uris: dict[str, None] = {}

        self._current_track_id: str | None = None
        self._current_track_started = time.monotonic()
        self._last_current: dict[str, Any] | None = None
        self._tick_lock = asyncio.Lock()
        self._pending_tick: asyncio.Task | None = None  # debounce queue_updated flood
        self._enqueuing = False  # True while _run_decision is adding tracks to MA
        self._tasks: list[asyncio.Task] = []
        self._stopping = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self._taste.start_session()
        self._ma.on_event(self._on_ma_event)
        self._ma.on_reconnect(self._on_ma_reconnect)
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

    async def _on_ma_reconnect(self) -> None:
        """Called each time the MA WebSocket reconnects.

        Clears the tracked current-track state so the next event re-establishes
        the baseline cleanly.
        """
        _LOGGER.info("MA reconnected — resetting track-tracking state")
        self._current_track_id = None
        self._current_track_started = time.monotonic()

    # ------------------------------------------------------------------ #
    # Triggers
    # ------------------------------------------------------------------ #
    async def _on_ma_event(self, event: str, data: dict[str, Any]) -> None:
        _LOGGER.debug("MA event received: %s", event)

        if event == "media_item_played":
            # MA fires this when a new track starts — use it for track bookkeeping.
            self._note_track_change({"current_item": data})
            return

        if event != EVENT_QUEUE_UPDATED:
            return

        self._note_track_change(data)

        # Compute items_remaining directly from the event payload.
        # The queue_updated event has 'items' (total count) and 'current_index'.
        # Use `or 0` rather than a default so null JSON values also become 0.
        items_total = data.get("items")
        current_index = data.get("current_index") or 0
        if isinstance(items_total, int):
            remaining = max(items_total - current_index - 1, 0)
        else:
            # Fallback: ask MA (only if socket is healthy)
            if self._ma.is_connected:
                try:
                    queue = await self._ma.get_queue()
                    remaining = queue.get("items_remaining", 0)
                except Exception:  # noqa: BLE001
                    return
            else:
                return

        _LOGGER.debug("queue_updated: items_remaining=%s enqueuing=%s", remaining, self._enqueuing)
        if self._enqueuing:
            return  # suppress ticks during our own enqueue batch
        if remaining < ENQUEUE_THRESHOLD:
            # Debounce: MA fires queue_updated for every track added during a
            # batch enqueue.  Collapse the burst into a single pending tick so
            # we don't pile up dozens of lock-waiting coroutines.
            if self._pending_tick and not self._pending_tick.done():
                return
            self._pending_tick = asyncio.create_task(self.tick(reason="queue_low"))

    async def _poll_loop(self) -> None:
        interval = max(self._config.dj_tick_interval, 10)
        while not self._stopping:
            await asyncio.sleep(interval)
            try:
                queue = await self._ma.get_queue()
                self._note_track_change(queue)
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
                # Same behavior as the Set Vibe button: rebuild the queue now.
                asyncio.create_task(self.set_vibe(value))
            await asyncio.sleep(15)

    # ------------------------------------------------------------------ #
    # Track-change bookkeeping
    # ------------------------------------------------------------------ #
    def _note_track_change(self, queue_or_event: dict[str, Any]) -> None:
        """Track which song is current — for stats, art, and the like/block path.

        Deliberately does NOT record skips. Time-based "did the track change
        quickly?" detection is gone: clearing the queue or a vibe rebuild would
        register false skips, and skips no longer carry any persistent penalty.
        A skip is only ever recorded when the user presses the TideSync skip
        button (see :meth:`skip`).
        """
        current = queue_or_event.get("current_item")
        track_id = current.get("queue_item_id") or current.get("uri") if current else None
        # No current item usually means a reconnect gap — ignore it.
        if track_id is None or track_id == self._current_track_id:
            return

        # A genuine advance to a new track.
        if self._current_track_id is not None:
            self.stats["tracks_played"] += 1
        self._current_track_id = track_id
        self._current_track_started = time.monotonic()
        self._last_current = current
        self._remember_uri(_track_uri(current))

    def _remember_uri(self, uri: str | None) -> None:
        if uri:
            self._session_uris[uri] = None

    @property
    def current_uri(self) -> str | None:
        return _track_uri(self._last_current)

    # ------------------------------------------------------------------ #
    # Decision cycle
    # ------------------------------------------------------------------ #
    async def build_context(self, fresh_start: bool = False) -> dict[str, Any]:
        queue = await self._ma.get_queue()
        history = await self._ma.get_history(n=20)
        duration_mins = round((time.monotonic() - self.session_started) / 60)
        person = self._users.active
        tod = _time_of_day()
        ctx = {
            "taste_profile": self._taste.summary,
            "listener": person.name,
            "recent_history": [_track_label(i) for i in history],
            "current_track": _track_label(queue.get("current_item")),
            "queue": [_track_label(i) for i in queue.get("items", [])],
            "recent_skips": self.recent_skips,
            "recent_likes": person.recent_likes(),
            "blocked_tracks": person.blocked_labels(),
            "moods_this_time_of_day": person.moods_for(tod),
            "vibe_prompt": self.vibe_prompt or None,
            "time_of_day": tod,
            "listening_duration_mins": duration_mins,
            "tracks_to_add": QUEUE_TARGET,
        }
        if fresh_start:
            ctx["fresh_start"] = True
            if self.vibe_prompt:
                ctx["instruction"] = (
                    f"Starting a brand new radio session for {person.name} — select exactly "
                    f"{QUEUE_TARGET} tracks that set the mood from their taste profile, the "
                    "current vibe, and the time of day."
                )
            else:
                ctx["instruction"] = (
                    f"Starting a brand new radio session for {person.name} with NO explicit "
                    f"vibe. Infer the mood from 'moods_this_time_of_day' (what they usually "
                    f"ask for at this time) and 'recent_likes', then select exactly "
                    f"{QUEUE_TARGET} tracks. Never queue anything in 'blocked_tracks'."
                )
        return ctx

    async def _run_decision(
        self, reason: str, play_option: str = "add", fresh_start: bool = False
    ) -> dict[str, Any]:
        """Shared decision path used by tick() and start_radio()."""
        async with self._tick_lock:
            # Re-check queue depth once we hold the lock.  queue_updated events
            # fire for every track added during an enqueue batch, so multiple
            # "queue_low" ticks can pile up while a previous decision is still
            # running.  By the time each one acquires the lock the queue is
            # already healthy — skip the Claude call entirely.
            if play_option == "add":
                try:
                    live_queue = await self._ma.get_queue()
                    live_remaining = live_queue.get("items_remaining", 0)
                    if live_remaining >= QUEUE_TARGET // 2:
                        _LOGGER.debug(
                            "tick(%s) skipped — queue already has %d tracks remaining",
                            reason, live_remaining,
                        )
                        return {"ok": True, "skipped": True, "items_remaining": live_remaining}
                except Exception:  # noqa: BLE001
                    pass  # if we can't check, proceed with the decision

            context = await self.build_context(fresh_start=fresh_start)
            try:
                decision = await self._brain.decide(context)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("DJ decision failed: %s", err)
                return {"ok": False, "error": str(err)}

            queries = [t.query for t in decision.next_tracks]
            blocked = self._users.active.blocked_uris()
            self._enqueuing = True
            try:
                enqueued = await self._ma.enqueue_queries(
                    queries, option=play_option, blocked_uris=blocked
                )
            finally:
                self._enqueuing = False
            if play_option == "play":
                await self._ma.ensure_playing()
            for track in enqueued:
                self._remember_uri(track.get("uri"))
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
                    "reason": reason,
                },
            )

            # Periodically refine the taste profile.
            if self._taste.record_decision():
                history = await self._ma.get_history(n=30)
                await self._taste.maybe_update(self._brain, history)

            if play_option == "play" and not enqueued:
                return {
                    "ok": False,
                    "error": "Claude picked tracks but none could be found/"
                    "resolved in Music Assistant.",
                    "decision": entry,
                }
            return {"ok": True, "decision": entry}

    async def tick(self, reason: str = "manual") -> dict[str, Any]:
        return await self._run_decision(reason=reason, play_option="add")

    async def _rebuild(self, reason: str) -> dict[str, Any]:
        """Clear the queue and start a brand-new set, cutting to it immediately.

        Shared by Start Radio, Nudge DJ, a vibe change, and a person switch — all
        of which should make the change audible right away rather than waiting
        for the existing queue to drain.
        """
        # If MA just dropped and is reconnecting, wait up to 15s rather than
        # immediately failing with a misleading "tracks not found" error.
        if not self._ma.is_connected:
            _LOGGER.info("%s: MA not connected, waiting up to 15s for reconnect", reason)
            for _ in range(15):
                await asyncio.sleep(1)
                if self._ma.is_connected:
                    break
            if not self._ma.is_connected:
                return {
                    "ok": False,
                    "error": "Music Assistant is not connected. Check the MA add-on and try again.",
                }
        player = await self._ensure_player()
        if not player:
            return {
                "ok": False,
                "error": "No Music Assistant player available to start radio on.",
            }
        _LOGGER.info("Rebuilding queue (%s) on player %s — clearing queue", reason, player)
        await self._ma.clear_queue()
        # Re-baseline track tracking so the fresh first track isn't treated as a
        # continuation of whatever was playing before the clear.
        self._current_track_id = None
        self._last_current = None
        return await self._run_decision(
            reason=reason, play_option="play", fresh_start=True
        )

    async def start_radio(self) -> dict[str, Any]:
        """Pick a player if needed, then start playback with a fresh set."""
        return await self._rebuild("start_radio")

    async def nudge(self) -> dict[str, Any]:
        """Tear down the current queue and rebuild a fresh set with the live vibe."""
        return await self._rebuild("nudge")

    async def set_vibe(self, prompt: str) -> dict[str, Any]:
        """Set the active vibe, remember it for this person + time of day, rebuild."""
        self.vibe_prompt = prompt.strip()
        _LOGGER.info("Vibe set to: %s", self.vibe_prompt)
        if self.vibe_prompt:
            person = self._users.active
            person.record_mood(_time_of_day(), self.vibe_prompt)
            self._users.save(person)
        return await self._rebuild("vibe_change")

    # ------------------------------------------------------------------ #
    # People
    # ------------------------------------------------------------------ #
    def list_users(self) -> list[dict[str, Any]]:
        return self._users.people()

    async def select_user(self, slug: str) -> dict[str, Any]:
        if not self._users.select(slug):
            return {"ok": False, "error": f"Unknown person: {slug}"}
        _LOGGER.info("Switched active person to %s", slug)
        # Rebuild immediately so playback reflects the new person's preferences.
        result = await self._rebuild("person_switch")
        result["active"] = self._users.active_slug
        return result

    def add_user(self, name: str) -> dict[str, Any]:
        if not name.strip():
            return {"ok": False, "error": "Name is required."}
        person = self._users.add_person(name)
        return {"ok": True, "slug": person.slug, "name": person.name}

    # ------------------------------------------------------------------ #
    # Players
    # ------------------------------------------------------------------ #
    async def list_players(self) -> list[dict[str, Any]]:
        players = await self._ma.get_players()
        for p in players:
            p["selected"] = p["player_id"] == self._ma.selected_player_id
        return players

    def select_player(self, player_id: str) -> None:
        self._ma.set_player(player_id)

    async def pause(self) -> dict[str, Any]:
        ok = await self._ma.pause()
        return {"ok": ok}

    async def skip(self) -> dict[str, Any]:
        """Skip the current track (the ONLY thing that records a skip).

        Recorded as a soft, session-only signal so Claude steers away from this
        track/artist for the rest of the session. It is not persisted and never
        blocks the track from future sessions — use Block for that.
        """
        label = _track_label(self._last_current)
        if label:
            self.stats["skips"] += 1
            self.recent_skips.append({"track": label})
            self.recent_skips = self.recent_skips[-10:]
            _LOGGER.info("UI skip recorded: %s", label)
        ok = await self._ma.skip()
        return {"ok": ok}

    async def _ensure_player(self) -> str | None:
        """Return a target player id, auto-selecting the first if none chosen.

        Also validates the stored player_id still exists in MA — the Web Player
        (ma_* prefix) disappears when the MA browser tab is closed, and other
        players may go unavailable. Using a stale id causes "Queue not
        available" warnings in MA until the add-on is restarted.
        """
        players = await self._ma.get_players()
        player_ids = {p["player_id"] for p in players}

        if self._ma.selected_player_id:
            if self._ma.selected_player_id in player_ids:
                return self._ma.selected_player_id
            _LOGGER.warning(
                "Previously selected player %s is no longer in MA player list — clearing selection",
                self._ma.selected_player_id,
            )
            self._ma.selected_player_id = None
            self._ma.active_queue_id = None

        # Prefer a player that is actively powered/available over a cold one.
        available = [p for p in players if p.get("available") and p.get("powered")]
        chosen = (available or players or [None])[0]
        if chosen:
            self._ma.set_player(chosen["player_id"])
            return self._ma.selected_player_id
        return None

    # ------------------------------------------------------------------ #
    # Taste seeding
    # ------------------------------------------------------------------ #
    async def seed_from_playlist(self, playlist: str) -> dict[str, Any]:
        """Seed the taste profile from a public YouTube Music playlist."""
        from yt_seed import fetch_playlist_tracks

        try:
            tracks = await asyncio.to_thread(fetch_playlist_tracks, playlist)
        except (ValueError, RuntimeError) as err:
            return {"ok": False, "error": str(err)}

        try:
            summary = await self._taste.seed_from_tracks(
                self._brain, tracks, source="youtube_music"
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Seed taste summary failed: %s", err)
            return {
                "ok": False,
                "error": f"Fetched {len(tracks)} tracks, but Claude analysis "
                f"failed: {err}",
            }
        return {"ok": True, "track_count": len(tracks), "summary": summary}

    # ------------------------------------------------------------------ #
    # Tidal: like a track / save the session as a playlist
    # ------------------------------------------------------------------ #
    async def _resolve_current(self) -> tuple[str | None, str | None]:
        """Return (uri, label) for the currently-playing track, querying MA if needed."""
        uri = self.current_uri
        label = _track_label(self._last_current)
        if not uri:
            try:
                queue = await self._ma.get_queue()
                current = queue.get("current_item")
                uri = _track_uri(current)
                label = label or _track_label(current)
            except Exception:  # noqa: BLE001
                pass
        return uri, label

    async def like_current(self) -> dict[str, Any]:
        """Favorite the currently playing track (syncs to Tidal via MA) and log it
        to the active person's memory."""
        uri, label = await self._resolve_current()
        if not uri:
            return {"ok": False, "error": "Nothing is playing to like."}
        try:
            await self._ma.add_favorite(uri)
        except Exception as err:  # noqa: BLE001
            return {"ok": False, "error": f"Music Assistant rejected the like: {err}"}
        person = self._users.active
        person.add_like(label, uri)
        self._users.save(person)
        return {"ok": True, "liked": label or uri, "person": person.name}

    async def block_current(self) -> dict[str, Any]:
        """Block the current track for 30 days for the active person and skip it now."""
        uri, label = await self._resolve_current()
        if not uri:
            return {"ok": False, "error": "Nothing is playing to block."}
        person = self._users.active
        person.add_block(label, uri)
        self._users.save(person)
        _LOGGER.info("Blocked %s for %s (30 days)", label or uri, person.name)
        # Stop the song immediately — that's why you blocked it.
        await self._ma.skip()
        return {"ok": True, "blocked": label or uri, "person": person.name}

    async def save_session_playlist(self, name: str) -> dict[str, Any]:
        """Create a Tidal playlist from the tracks heard this session."""
        uris = list(self._session_uris.keys())
        if not uris:
            return {"ok": False, "error": "No tracks have played yet this session."}
        name = name.strip() or datetime.now().strftime("TideSync %Y-%m-%d %H:%M")
        try:
            playlist = await self._ma.create_playlist(
                name, provider=self._config.tidal_provider
            )
        except Exception as err:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"Could not create the playlist in Music Assistant: {err}",
            }
        playlist_id = playlist.get("item_id") or playlist.get("uri")
        if not playlist_id:
            return {"ok": False, "error": "Playlist was created but has no usable id."}
        try:
            await self._ma.add_playlist_tracks(playlist_id, uris)
        except Exception as err:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"Playlist '{name}' created, but adding tracks failed: {err}",
                "playlist": name,
            }
        _LOGGER.info("Saved session playlist '%s' with %d tracks", name, len(uris))
        return {"ok": True, "playlist": name, "track_count": len(uris)}

    # ------------------------------------------------------------------ #
    # Read-only views for the API/UI
    # ------------------------------------------------------------------ #
    async def status(self) -> dict[str, Any]:
        # Stay resilient during onboarding / before MA is reachable.
        try:
            queue = await self._ma.get_queue()
        except Exception:  # noqa: BLE001
            queue = {}
        current_item = queue.get("current_item")
        album_art = None
        if current_item:
            img = current_item.get("image") or {}
            album_art = img.get("path") or None

        can_act = bool(_track_uri(current_item) or self.current_uri)
        return {
            "configured": bool(self._config.anthropic_api_key),
            "now_playing": _track_label(current_item),
            "album_art": album_art,
            "player_state": queue.get("state"),  # "playing" | "paused" | "idle" | None
            "vibe": self.vibe_prompt or None,
            "time_of_day": _time_of_day(),
            "items_remaining": queue.get("items_remaining", 0),
            "ma_connected": self._ma.is_connected,
            "ma_error": self._ma.last_error,
            "ma_host": self._config.ma_host,
            "selected_player_id": self._ma.selected_player_id,
            "taste_seeded": self._taste.is_bootstrapped,
            "session_track_count": len(self._session_uris),
            "can_like": can_act,
            "can_block": can_act,
            "people": self._users.people(),
            "active_person": self._users.active.name,
            "session_minutes": round((time.monotonic() - self.session_started) / 60),
            "stats": self.stats,
            "model": self._config.claude_model,
        }
