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
import random
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
SEED_PLAYLIST_SAMPLE = 15  # max playlist tracks woven into the opening seed queue


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


def _provider_of(uri: str) -> str | None:
    """Provider id from an MA URI ("<provider>://<type>/<id>" -> "<provider>")."""
    if uri and "://" in uri:
        return uri.split("://", 1)[0] or None
    return None


def _detect_session_provider(uris: list[str]) -> str | None:
    """Return the provider shared by every session URI, or None if mixed/empty.

    A streaming playlist can only hold tracks from its own provider, so we save
    on the provider the session actually played. When the session mixed sources
    (or none could be parsed) we return None and the caller falls back to the
    configured provider.
    """
    providers = {p for p in (_provider_of(u) for u in uris) if p}
    return next(iter(providers)) if len(providers) == 1 else None


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
        # When the listener starts radio from a hand-picked song we keep that
        # seed around so every auto-refill keeps building the same station,
        # instead of reverting to the stored taste profile after the first batch.
        # Cleared by any other rebuild (vibe change, nudge, person switch, plain
        # start radio).
        self._radio_seed_label: str | None = None
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
        # User pressed Stop: playback halted, queue cleared, and the auto-DJ is
        # parked until the user explicitly restarts it (Start Radio / Set Vibe /
        # Nudge / person switch / seed all clear this). Distinct from _stopping,
        # which is app shutdown.
        self._dj_stopped = False
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
        if self._dj_stopped:
            return  # user pressed Stop — don't auto-refill until they restart
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
                # Don't poll-fill while idle/stopped — only top up an active
                # session. (_run_decision enforces the same gate, but skipping
                # here avoids waking the decision lock every interval for nothing.)
                if queue.get("state") not in ("playing", "paused"):
                    continue
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
    async def build_context(
        self, fresh_start: bool = False, seed_label: str | None = None
    ) -> dict[str, Any]:
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
            if seed_label:
                ctx["seed_track"] = seed_label
                ctx["instruction"] = (
                    f"The listener hand-picked '{seed_label}' as a seed and it is now "
                    f"playing. Build a radio station around it: pick exactly {QUEUE_TARGET} "
                    "tracks that flow from this seed — matching its genre, energy and era — "
                    "while honouring the taste profile and never queuing anything in "
                    "'blocked_tracks'. Do NOT repeat the seed track."
                )
            elif self.vibe_prompt:
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
        elif seed_label:
            # Ongoing auto-refill of a station seeded from a hand-picked song.
            # Keep it on-theme with the seed and what has actually played rather
            # than drifting back to the broad taste profile.
            ctx["seed_track"] = seed_label
            ctx["instruction"] = (
                f"This is an ongoing radio station seeded from '{seed_label}'. Keep it "
                f"flowing: pick exactly {QUEUE_TARGET} tracks that stay on-theme with the "
                f"seed and the tracks already played this session (see 'recent_history'), "
                "evolving naturally rather than reverting to the broader taste profile. "
                "Never queue anything in 'blocked_tracks' and don't repeat recent tracks."
            )
        return ctx

    @staticmethod
    def _interleave(primary: list[str], secondary: list[str]) -> list[str]:
        """Merge two lists, spreading each evenly across the result.

        Items are pulled by fractional position so neither list clumps: with 3
        primary and 9 secondary, the 3 are spaced ~every 4th slot. `primary`
        leads on ties, so the first emitted item is primary[0].
        """
        primary, secondary = list(primary), list(secondary)
        if not primary:
            return secondary
        if not secondary:
            return primary
        merged: list[str] = []
        pi = si = 0
        while pi < len(primary) or si < len(secondary):
            pf = pi / len(primary) if pi < len(primary) else 2.0
            sf = si / len(secondary) if si < len(secondary) else 2.0
            if pf <= sf:
                merged.append(primary[pi]); pi += 1
            else:
                merged.append(secondary[si]); si += 1
        return merged

    async def _run_decision(
        self,
        reason: str,
        play_option: str = "add",
        fresh_start: bool = False,
        seed_label: str | None = None,
        seed_queries: list[str] | None = None,
    ) -> dict[str, Any]:
        """Shared decision path used by tick() and start_radio().

        `seed_queries` are concrete tracks (e.g. from a YouTube Music playlist)
        woven in among Claude's discovery picks so the listener's own songs
        actually play alongside the new ones.
        """
        async with self._tick_lock:
            # Re-check queue depth once we hold the lock.  queue_updated events
            # fire for every track added during an enqueue batch, so multiple
            # "queue_low" ticks can pile up while a previous decision is still
            # running.  By the time each one acquires the lock the queue is
            # already healthy — skip the Claude call entirely.
            if play_option == "add":
                try:
                    live_queue = await self._ma.get_queue()
                    # Auto-fill only makes sense while the listener is actually
                    # playing. When the player is idle/stopped (or the selected
                    # player is gone, so there's no matching queue and state is
                    # None) an empty queue reads as items_remaining=0, which would
                    # otherwise trigger a Claude decision every poll forever even
                    # though nobody is listening. Starting playback from cold is
                    # the job of Start Radio / Set Vibe (the rebuild path), not the
                    # background top-up.
                    state = live_queue.get("state")
                    if state not in ("playing", "paused"):
                        _LOGGER.debug(
                            "tick(%s) skipped — player is %s, not actively "
                            "listening; auto-fill only runs during playback",
                            reason, state or "idle",
                        )
                        return {"ok": True, "skipped": True, "reason": "not_playing"}
                    live_remaining = live_queue.get("items_remaining", 0)
                    if live_remaining >= QUEUE_TARGET // 2:
                        _LOGGER.debug(
                            "tick(%s) skipped — queue already has %d tracks remaining",
                            reason, live_remaining,
                        )
                        return {"ok": True, "skipped": True, "items_remaining": live_remaining}
                except Exception:  # noqa: BLE001
                    pass  # if we can't check, proceed with the decision

            context = await self.build_context(fresh_start=fresh_start, seed_label=seed_label)
            try:
                decision = await self._brain.decide(context)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("DJ decision failed: %s", err)
                return {"ok": False, "error": str(err)}

            queries = [t.query for t in decision.next_tracks]
            if seed_queries:
                # Weave the listener's playlist tracks evenly through the
                # discovery picks; seed tracks lead so a familiar song starts.
                queries = self._interleave(seed_queries, queries)
            # Never repeat a track already heard/queued this session: drop the
            # active person's blocks plus everything seen so far this session.
            blocked = self._users.active.blocked_uris() | set(self._session_uris)
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
        if self._dj_stopped:
            # Parked by the user's Stop — no auto-fill until they restart.
            return {"ok": True, "skipped": True, "reason": "dj_stopped"}
        # Carry the active radio seed (if any) into every auto-refill so the
        # station keeps building around the hand-picked song.
        return await self._run_decision(
            reason=reason, play_option="add", seed_label=self._radio_seed_label
        )

    async def stop(self) -> dict[str, Any]:
        """Stop playback, clear the queue, and park the auto-DJ.

        Nothing will be queued again until the user explicitly restarts playback
        (Start Radio / Set Vibe / Nudge / person switch / a seed) — each of those
        goes through `_rebuild`, which lifts the parked flag.
        """
        self._dj_stopped = True
        self._radio_seed_label = None
        # Cancel any in-flight auto-fill so a debounced tick can't refill after
        # we clear the queue.
        if self._pending_tick and not self._pending_tick.done():
            self._pending_tick.cancel()
        stopped = await self._ma.stop()
        await self._ma.clear_queue()
        # Re-baseline track tracking so a later restart starts clean.
        self._current_track_id = None
        self._last_current = None
        _LOGGER.info("DJ stopped by user — playback halted and queue cleared")
        return {"ok": True, "stopped": stopped}

    async def _rebuild(
        self,
        reason: str,
        seed_uri: str | None = None,
        seed_label: str | None = None,
        seed_queries: list[str] | None = None,
    ) -> dict[str, Any]:
        """Clear the queue and start a brand-new set, cutting to it immediately.

        Shared by Start Radio, Nudge DJ, a vibe change, and a person switch — all
        of which should make the change audible right away rather than waiting
        for the existing queue to drain.

        When `seed_uri` is given the listener hand-picked the opening track: it
        plays immediately and Claude appends a station around it, rather than
        Claude choosing the opener itself. `seed_queries` (e.g. a seeded
        playlist) are instead woven among Claude's picks in the opening set.
        """
        # Any rebuild is a user-initiated restart — lift a prior Stop so the
        # auto-DJ resumes topping up the queue.
        self._dj_stopped = False
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
        if seed_uri:
            # Play the hand-picked seed now; Claude appends the station after it.
            # Remember the seed so later auto-refills keep the station on-theme.
            self._radio_seed_label = seed_label
            if not await self._ma.enqueue_uri(seed_uri, option="play"):
                return {"ok": False, "error": "Couldn't start the seed track."}
            await self._ma.ensure_playing()
            return await self._run_decision(
                reason=reason, play_option="add", fresh_start=True, seed_label=seed_label
            )
        # Any non-seed rebuild (vibe change, nudge, person switch, plain start
        # radio) ends the seeded station — release the seed.
        self._radio_seed_label = None
        return await self._run_decision(
            reason=reason, play_option="play", fresh_start=True,
            seed_queries=seed_queries,
        )

    async def start_radio(self) -> dict[str, Any]:
        """Pick a player if needed, then start playback with a fresh set."""
        return await self._rebuild("start_radio")

    async def start_radio_from_seed(self, uri: str, label: str = "") -> dict[str, Any]:
        """Clear the queue, play the hand-picked seed now, and let Claude build a station around it."""
        if not uri:
            return {"ok": False, "error": "No seed track provided."}
        return await self._rebuild("seed_radio", seed_uri=uri, seed_label=label.strip() or None)

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

    async def toggle_playback(self) -> dict[str, Any]:
        """Toggle play/pause using MA's atomic play_pause command.

        The previous read-then-decide approach raced MA's eventually-consistent
        player state: a stale "playing" read just after a pause would send pause
        again (or vice versa), so resume appeared to do nothing. MA's play_pause
        flips based on its own authoritative state in one call, which also
        resumes a paused queue more reliably than a bare play. The UI updates its
        icon optimistically, so we don't need to return the resulting state.
        """
        ok = await self._ma.play_pause()
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
    async def seed_from_playlist(
        self, playlist: str, start: bool = True
    ) -> dict[str, Any]:
        """Seed the taste profile from a public YouTube Music playlist.

        When `start` is set (the default), immediately kick off a fresh queue
        built from the just-updated taste profile, so one button both learns the
        playlist's taste and starts playing music that reflects it. The taste
        seed is reported as successful even if playback can't start (e.g. no MA
        player) — the profile update still happened.
        """
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

        result = {"ok": True, "track_count": len(tracks), "summary": summary}
        if start:
            # Start a fresh set from the just-refreshed taste profile, with a
            # shuffled sample of the actual playlist tracks woven in among
            # Claude's discoveries so the listener's own songs play too.
            seed_queries = self._playlist_seed_queries(tracks)
            radio = await self._rebuild("seed_taste", seed_queries=seed_queries)
            result["radio_started"] = bool(radio.get("ok"))
            if radio.get("ok"):
                result["enqueued"] = (radio.get("decision") or {}).get("enqueued", 0)
            else:
                result["radio_error"] = radio.get("error")
        return result

    @staticmethod
    def _playlist_seed_queries(tracks: list[dict[str, Any]]) -> list[str]:
        """Pick a shuffled sample of playlist tracks as MA search queries.

        Capped at SEED_PLAYLIST_SAMPLE so a 200-track playlist doesn't swamp the
        opening set; formatted "Artist Title" for a reliable Music Assistant
        search match.
        """
        sample = list(tracks)
        random.shuffle(sample)
        queries: list[str] = []
        for t in sample[:SEED_PLAYLIST_SAMPLE]:
            name = (t.get("name") or "").strip()
            if not name:
                continue
            artist = (t.get("artist") or "").strip()
            queries.append(f"{artist} {name}".strip())
        return queries

    # ------------------------------------------------------------------ #
    # Like a track / save the session as a playlist (any MA music source)
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
        """Favorite the currently playing track (MA syncs it to the track's source
        provider) and log it to the active person's memory."""
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
        """Create a playlist (on whatever music source the session played) from the
        tracks heard this session."""
        uris = list(self._session_uris.keys())
        if not uris:
            return {"ok": False, "error": "No tracks have played yet this session."}
        name = name.strip() or datetime.now().strftime("TideSync %Y-%m-%d %H:%M")
        # Save on the provider the session actually played (so the URIs are
        # accepted); for a mixed/unknown session, use the configured override.
        provider = _detect_session_provider(uris) or self._config.playlist_provider
        try:
            playlist = await self._ma.create_playlist(name, provider=provider)
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
        # Prefer the player's authoritative state so the play/pause icon flips on
        # pause; the queue meta state doesn't always report a pause.
        try:
            player_state = await self._ma.get_play_state()
        except Exception:  # noqa: BLE001
            player_state = queue.get("state")
        return {
            "configured": bool(self._config.anthropic_api_key),
            "now_playing": _track_label(current_item),
            "album_art": album_art,
            "player_state": player_state,  # "playing" | "paused" | "idle" | None
            "dj_stopped": self._dj_stopped,  # user pressed Stop; auto-DJ parked
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
