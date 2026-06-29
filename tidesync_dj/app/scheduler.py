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
import json
import logging
import random
import time
from collections import deque
from datetime import datetime
from typing import Any

from claude_brain import ClaudeBrain, PoolOrder, SetPlan
from config import Config
from ha_client import HAClient
from ma_client import MusicAssistantClient, EVENT_QUEUE_UPDATED, EVENT_QUEUE_TIME_UPDATED
from sonic_features import SonicFeatures, camelot_adjacent, tempo_close
from taste_profile import TasteProfile
from timectx import month_bucket, time_of_day_slot
from user_memory import Person, UserStore

_LOGGER = logging.getLogger(__name__)

ENQUEUE_THRESHOLD = 5   # tick when fewer than this many tracks remain
QUEUE_TARGET = 30       # how many tracks to request from Claude per decision
POOL_TARGET = 40        # candidates to generate when grounding (pool to pick from)
SEED_PLAYLIST_SAMPLE = 15  # max playlist tracks woven into the opening seed queue
PERSON_REFINE_EVERY = 20   # refine the active person's taste every N decisions
ENERGY_BIAS_MAX = 3        # clamp for the energy up/down nudge
STOP_SUPPRESS_SECONDS = 4  # ignore auto-refill briefly after a Stop
# Rebuild reasons that start a genuinely new session → regenerate the set plan
# and reset the energy bias. (nudge/energy keep the existing plan.)
_FRESH_PLAN_REASONS = {"start_radio", "seed_radio", "vibe_change", "person_switch"}
# Reasons that begin a new *listening session* (reset stats/clock). Starting
# radio, a seed, or a Set Vibe begins one; a person switch continues the session.
_SESSION_START_REASONS = {"start_radio", "seed_radio", "vibe_change"}
# A listening session also ends on its own after this long with no playback, so
# the next song you put on starts fresh stats instead of continuing yesterday's.
IDLE_SESSION_RESET_SECONDS = 2 * 60 * 60


def _time_of_day(now: datetime | None = None) -> str:
    # Thin wrapper so existing call sites keep working; the slot boundaries live
    # in timectx so the scheduler and user_memory bucket identically.
    return time_of_day_slot(now)


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


def _track_artist(item: dict[str, Any] | None) -> str | None:
    """Primary artist name from a queue item / media item (either shape)."""
    if not item:
        return None
    media = item.get("media_item") or item
    artists = media.get("artists") or []
    first = artists[0] if artists else None
    if first:
        return first.get("name") if isinstance(first, dict) else first
    return media.get("artist")


def _track_name(item: dict[str, Any] | None) -> str | None:
    """Bare track title (no artist) from a queue item / media item."""
    if not item:
        return None
    media = item.get("media_item") or item
    return media.get("name") or item.get("name")


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
        # Sonic-feature store: BPM/key looked up by ISRC and cached on disk, used
        # to ground the energy arc. Disabled => every lookup is a cheap no-op.
        self._sonic = SonicFeatures(
            config.data_dir,
            enabled=config.sonic_features,
            getsongbpm_key=config.getsongbpm_api_key,
        )

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
        # Last time playback was observed active — drives the idle auto-reset so a
        # session that has been paused/idle past IDLE_SESSION_RESET_SECONDS ends
        # and the next track you play starts a clean one.
        self._last_active = time.monotonic()
        # A "session" is a listening run: it (re)starts on Start Radio / a seed /
        # Set Vibe and these counters reset then; Stop or a long idle ends it.
        # "added"/"new_artists" reflect what ACTUALLY PLAYED — see _note_track_change
        # — not what was queued, so unplayable tracks never inflate them.
        self.stats = {"tracks_played": 0, "skips": 0, "added": 0, "new_artists": 0}
        # Ordered, de-duplicated track URIs the DJ has QUEUED this session — used
        # to avoid re-queuing the same track. dict preserves order + dedupes.
        self._session_uris: dict[str, None] = {}
        # Energy the brain committed to each queued track (uri -> 1..10). Fed back
        # as "energy_arc" so the next cycle continues the curve it actually built.
        self._track_energy: dict[str, int] = {}
        # Distinct URIs / artists that ACTUALLY started playing this session.
        # These back the "songs heard" and "artists" stats and the saved playlist.
        self._played_uris: dict[str, None] = {}
        self._played_artists: set[str] = set()
        # Per-person decision counter (in-memory) that paces background taste
        # refinement. Keyed by person slug; a restart just delays the next refine.
        self._person_decision_counts: dict[str, int] = {}
        # Strong refs to in-flight background refine tasks so they aren't GC'd
        # mid-run (create_task only keeps a weak reference).
        self._refine_tasks: set[asyncio.Task] = set()
        # Long-form set plan (the arc) for the current session, generated once on
        # a fresh session and followed across refill ticks.
        self._set_plan: SetPlan | None = None
        self._set_plan_started: float | None = None
        # Energy up/down nudge, clamped to +/-ENERGY_BIAS_MAX; reset per session.
        self._energy_bias: int = 0
        # One-shot "try again, different direction" flag (Nudge DJ).
        self._reroll: bool = False

        self._current_track_id: str | None = None
        self._current_track_started = time.monotonic()
        self._last_current: dict[str, Any] | None = None
        self._tick_lock = asyncio.Lock()
        self._pending_tick: asyncio.Task | None = None  # debounce queue_updated flood
        self._enqueuing = False  # True while _run_decision is adding tracks to MA
        # Serializes user-initiated rebuilds (Start Radio / Set Vibe / Nudge /
        # person switch / seed). A second rebuild that lands while one is still
        # clearing+enqueueing used to overlap it, leaving two fill loops thrashing
        # the queue and skipping the current track — this lock makes them mutually
        # exclusive and lets a re-entrant call bail early as "busy".
        self._rebuild_lock = asyncio.Lock()
        # What the DJ is doing right now, surfaced to the UI (dj_status). Phases:
        # "idle" | "planning" (Claude arc) | "picking" (Claude track decision) |
        # "enqueuing" (adding tracks to MA). `target` is set during enqueuing so
        # the UI can render a real progress bar against the queue depth.
        self._dj_activity: dict[str, Any] = {"phase": "idle"}
        # User pressed Stop: playback halted, queue cleared, and the auto-DJ is
        # parked until the user explicitly restarts it (Start Radio / Set Vibe /
        # Nudge / person switch / seed all clear this). Distinct from _stopping,
        # which is app shutdown.
        self._dj_stopped = False
        # Monotonic time of the last Stop; for a few seconds after, ignore the
        # queue_updated/poll refill paths even if something flips _dj_stopped, so
        # MA's own "queue cleared" event can't spawn a refill tick.
        self._stopped_at = 0.0
        self._tasks: list[asyncio.Task] = []
        self._stopping = False
        # Default to the last-used player so the screen opens on it (validated
        # against availability before any actual playback in _ensure_player).
        _last = self._load_state().get("last_player")
        if _last:
            self._ma.set_player(_last)

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
        if self._config.ha_action_entity:
            self._tasks.append(asyncio.create_task(self._action_poll_loop()))
        await self._bootstrap_taste()

    async def shutdown(self) -> None:
        """App shutdown: cancel background tasks and close clients.

        Distinct from the user-facing `stop()` (the Stop button). These were both
        named `stop`, so this one was shadowed and never ran on shutdown — tasks
        leaked until the process died.
        """
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
        if self._dj_stopped or (time.monotonic() - self._stopped_at) < STOP_SUPPRESS_SECONDS:
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
                # Keep the idle-reset clock fresh while music is actually playing,
                # even across a single long track (no track-change event fires).
                if queue.get("state") == "playing":
                    self._last_active = time.monotonic()
                # Respect a user Stop (and its brief suppression window) before
                # doing anything else.
                if self._dj_stopped or (time.monotonic() - self._stopped_at) < STOP_SUPPRESS_SECONDS:
                    continue
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

    async def _action_poll_loop(self) -> None:
        """Let HA automations drive the DJ via an input_text/input_select helper.

        Set the helper to one of: play, stop, skip, next, previous, nudge,
        energy up / energy down, "vibe: <text>", or "player: <name-or-id>".
        Acting on *change* means an automation just writes the helper (e.g. an
        office button -> input_text). The value is acted on once per change.
        """
        entity = self._config.ha_action_entity
        last: str | None = None
        while not self._stopping:
            try:
                value = (await self._ha.get_state_value(entity) or "").strip()
                if value and value != last:
                    last = value
                    _LOGGER.info("HA action from %s: %s", entity, value)
                    asyncio.create_task(self._dispatch_action(value))
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Action poll error: %s", err)
            await asyncio.sleep(5)

    async def _dispatch_action(self, value: str) -> None:
        """Map an HA helper value to a DJ action."""
        v = value.strip()
        low = v.lower()
        try:
            if low in ("play", "start", "start_radio"):
                await self.start_radio()
            elif low == "stop":
                await self.stop()
            elif low in ("skip", "next"):
                await self.skip()
            elif low in ("previous", "prev"):
                await self.previous_track()
            elif low == "nudge":
                await self.nudge()
            elif low in ("energy up", "energy_up", "louder", "more energy"):
                await self.nudge_energy("up")
            elif low in ("energy down", "energy_down", "calmer", "less energy"):
                await self.nudge_energy("down")
            elif ":" in v and low.split(":", 1)[0].strip() in ("vibe", "mood"):
                await self.set_vibe(v.split(":", 1)[1].strip())
            elif ":" in v and low.split(":", 1)[0].strip() in ("player", "play_on", "speaker"):
                await self._select_player_by_name(v.split(":", 1)[1].strip())
            else:
                # Bare text → treat as a vibe.
                await self.set_vibe(v)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("HA action '%s' failed: %s", v, err)

    async def _select_player_by_name(self, name_or_id: str) -> None:
        """Resolve an HA-supplied player name/id to a real player and switch to it."""
        target = name_or_id.strip().lower()
        try:
            for p in await self._ma.get_players():
                if not p.get("available"):
                    continue
                if target in (str(p.get("player_id", "")).lower(), str(p.get("name", "")).lower()):
                    await self.select_player(p["player_id"])
                    return
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not select player '%s': %s", name_or_id, err)

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
        # Identify the track by URI — the one key that is stable across BOTH
        # event shapes that feed this method. `queue_updated` delivers a queue
        # item (which carries a queue_item_id), while `media_item_played`
        # delivers a bare media item (no queue_item_id, only a uri). Keying on
        # queue_item_id made a single physical track look like a brand-new one
        # every time those two event types interleaved during playback, which
        # inflated tracks_played (e.g. "10 played" while still on track 1).
        track_id = _track_uri(current)
        # No current item usually means a reconnect gap — ignore it.
        if track_id is None or track_id == self._current_track_id:
            return

        now = time.monotonic()
        # A long idle/paused gap ends the previous session; the song now starting
        # begins a fresh one. Re-baseline so it isn't counted as an "advance".
        if (now - self._last_active) > IDLE_SESSION_RESET_SECONDS:
            self._reset_session()
            self._current_track_id = None

        # A genuine advance to a new track.
        if self._current_track_id is not None:
            self.stats["tracks_played"] += 1
        self._current_track_id = track_id
        self._current_track_started = now
        self._last_active = now
        self._last_current = current
        self._remember_uri(track_id)
        # Stats that should reflect what was ACTUALLY HEARD (incl. this session's
        # first track) — never inflated by queued-but-unplayable tracks.
        self._played_uris[track_id] = None
        artist = _track_artist(current)
        if artist:
            self._played_artists.add(artist.strip().lower())
        self.stats["added"] = len(self._played_uris)
        self.stats["new_artists"] = len(self._played_artists)

    def _remember_uri(self, uri: str | None) -> None:
        if uri:
            self._session_uris[uri] = None

    @property
    def current_uri(self) -> str | None:
        return _track_uri(self._last_current)

    # ------------------------------------------------------------------ #
    # Set plan (long-form arc) + ambient context
    # ------------------------------------------------------------------ #
    def _set_plan_dict(self) -> dict[str, Any] | None:
        if not self._set_plan or not self._set_plan.phases:
            return None
        return {
            "arc_note": self._set_plan.arc_note,
            "phases": [p.model_dump() for p in self._set_plan.phases],
        }

    def _arc_position(self) -> dict[str, Any] | None:
        """Where we are in the set plan: elapsed, %through, current phase."""
        if not self._set_plan or not self._set_plan.phases or self._set_plan_started is None:
            return None
        elapsed = (time.monotonic() - self._set_plan_started) / 60.0
        total = sum(max(1, p.approx_minutes) for p in self._set_plan.phases) or 1
        cum = 0.0
        current = self._set_plan.phases[-1].name
        for p in self._set_plan.phases:
            cum += max(1, p.approx_minutes)
            if elapsed <= cum:
                current = p.name
                break
        return {
            "elapsed_minutes": round(elapsed),
            "pct_through": round(min(1.0, elapsed / total) * 100),
            "current_phase": current,
        }

    async def _weather_context(self) -> dict[str, Any]:
        """Optional outside weather/temperature from configured HA entities."""
        out: dict[str, Any] = {}
        we = (getattr(self._config, "weather_entity", "") or "").strip()
        te = (getattr(self._config, "temperature_entity", "") or "").strip()
        try:
            if we:
                v = await self._ha.get_state_value(we)
                if v:
                    out["weather"] = v
            if te:
                v = await self._ha.get_state_value(te)
                if v:
                    out["outside_temp"] = v
        except Exception:  # noqa: BLE001
            pass  # weather is a nice-to-have; never block a decision on it
        return out

    async def _generate_set_plan(self, reason: str, seed_label: str | None) -> None:
        """Generate the session's long-form arc once, at the start of a session."""
        person = self._users.active
        driver = (
            f"seed track: {seed_label}" if seed_label
            else (f"vibe: {self.vibe_prompt}" if self.vibe_prompt else "the listener's taste")
        )
        plan_ctx: dict[str, Any] = {
            "driver": driver,
            "vibe_prompt": self.vibe_prompt or None,
            "seed_track": seed_label,
            "listener": person.name,
            "taste_profile": (person.summary or self._taste.summary or "")[:600],
            "time_of_day": _time_of_day(),
            "month": month_bucket(),
            "reason": reason,
        }
        plan_ctx.update(await self._weather_context())
        self._dj_activity = {"phase": "planning", "detail": "Planning the set"}
        try:
            plan = await self._brain.plan_set(plan_ctx)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Set plan generation failed: %s", err)
            self._set_plan = None
            self._set_plan_started = None
            self._dj_activity = {"phase": "idle"}
            return
        self._set_plan = plan
        self._set_plan_started = time.monotonic()
        if plan and plan.phases:
            _LOGGER.info(
                "Set plan (%s): %s", reason, " -> ".join(p.name for p in plan.phases)
            )

    def _reset_session(self) -> None:
        """Start a fresh listening session: reset the clock, stats and dedupe."""
        self.session_started = time.monotonic()
        self._last_active = time.monotonic()
        self._session_uris.clear()
        self._track_energy.clear()
        self._played_uris.clear()
        self._played_artists = set()
        self.stats = {"tracks_played": 0, "skips": 0, "added": 0, "new_artists": 0}
        _LOGGER.info("New listening session started")

    # ------------------------------------------------------------------ #
    # Sonic enrichment
    # ------------------------------------------------------------------ #
    def _features_for(self, item: dict[str, Any] | None) -> dict[str, Any] | None:
        """Cache-only sonic features for a track (BPM/key/camelot), or None."""
        if not item or not self._sonic.enabled:
            return None
        ids = self._ma.external_ids(item)
        return self._sonic.get(
            isrc=ids.get("isrc"),
            mbid=ids.get("mbid"),
            artist=_track_artist(item) or "",
            title=_track_name(item) or "",
        )

    def _arc_entry(self, item: dict[str, Any] | None) -> dict[str, Any] | None:
        """One {track, energy?, bpm?, camelot?} point on the played energy curve."""
        label = _track_label(item)
        if not label:
            return None
        entry: dict[str, Any] = {"track": label}
        uri = _track_uri(item)
        energy = self._track_energy.get(uri) if uri else None
        if energy is not None:
            entry["energy"] = energy
        feats = self._features_for(item)
        if feats:
            if feats.get("bpm"):
                entry["bpm"] = feats["bpm"]
            if feats.get("camelot"):
                entry["camelot"] = feats["camelot"]
        return entry

    def _energy_arc(
        self, history: list[dict[str, Any]], current: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """The energy/tempo/key curve actually built so far (history + now playing)."""
        arc: list[dict[str, Any]] = []
        for item in history:
            entry = self._arc_entry(item)
            if entry:
                arc.append(entry)
        now = self._arc_entry(current)
        if now:
            now["now_playing"] = True
            arc.append(now)
        return arc

    def _schedule_feature_warm(self, tracks: list[dict[str, Any]]) -> None:
        """Background: warm the sonic cache for freshly enqueued tracks by ISRC."""
        if not self._sonic.enabled or not tracks:
            return

        async def _warm() -> None:
            for t in tracks:
                ids = self._ma.external_ids(t)
                try:
                    await self._sonic.ensure(
                        isrc=ids.get("isrc"),
                        mbid=ids.get("mbid"),
                        artist=_track_artist(t) or "",
                        title=_track_name(t) or "",
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Feature warm failed: %s", err)

        task = asyncio.create_task(_warm())
        self._refine_tasks.add(task)
        task.add_done_callback(self._refine_tasks.discard)

    def _harmonic_reorder(
        self, tracks: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Greedily reorder a resolved block for the smoothest Camelot/tempo flow.

        Cache-only and conservative: keeps the brain's opener, then at each step
        prefers an unplayed track that's harmonically/tempo-compatible with the
        last; falls back to the brain's original order when nothing matches or
        features are missing. A no-op until the cache has warmed for the block.
        """
        feats = [self._features_for(t) for t in tracks]
        if sum(1 for f in feats if f) < 2:
            return tracks  # not enough data to improve on the brain's order
        remaining = list(zip(tracks, feats))
        ordered = [remaining.pop(0)]
        while remaining:
            _, last_f = ordered[-1]
            last_cam = (last_f or {}).get("camelot")
            last_bpm = (last_f or {}).get("bpm")
            nxt = next(
                (
                    i
                    for i, (_, f) in enumerate(remaining)
                    if f
                    and (
                        camelot_adjacent(last_cam, f.get("camelot"))
                        or tempo_close(last_bpm, f.get("bpm"))
                    )
                ),
                0,  # fall back to the next track in the brain's order
            )
            ordered.append(remaining.pop(nxt))
        return [t for t, _ in ordered]

    # ------------------------------------------------------------------ #
    # Decision cycle
    # ------------------------------------------------------------------ #
    async def build_context(
        self,
        fresh_start: bool = False,
        seed_label: str | None = None,
        count: int = QUEUE_TARGET,
    ) -> dict[str, Any]:
        queue = await self._ma.get_queue()
        history = await self._ma.get_history(n=20)
        duration_mins = round((time.monotonic() - self.session_started) / 60)
        person = self._users.active
        tod = _time_of_day()
        month = month_bucket()
        # Per-person taste leads; fall back to the household baseline only when the
        # person has no learned/seeded taste of their own.
        person_taste = (person.summary or "").strip()
        household_taste = (self._taste.summary or "").strip()
        effective_taste = person_taste or household_taste
        ctx = {
            "taste_profile": effective_taste,
            "taste_profile_scope": "listener" if person_taste else "household",
            "listener": person.name,
            "recent_history": [_track_label(i) for i in history],
            "current_track": _track_label(queue.get("current_item")),
            "queue": [_track_label(i) for i in queue.get("items", [])],
            "recent_skips": self.recent_skips,
            "recent_likes": person.recent_likes(),
            "likes_this_time_of_day": person.likes_for_slot(tod),
            "likes_this_month": person.likes_for_month(month),
            "blocked_tracks": person.blocked_labels(),
            "moods_this_time_of_day": person.moods_for(tod),
            "moods_this_month": person.moods_for_month(month),
            "vibe_prompt": self.vibe_prompt or None,
            "time_of_day": tod,
            "time_context": {"time_of_day": tod, "month": month},
            "listening_duration_mins": duration_mins,
            "energy_bias": self._energy_bias,
            "set_plan": self._set_plan_dict(),
            "arc_position": self._arc_position(),
            "energy_arc": self._energy_arc(history, queue.get("current_item")),
            "tracks_to_add": count,
        }
        # Optional ambient signals (skipped cleanly when unconfigured).
        ctx.update(await self._weather_context())
        if self._reroll:
            ctx["reroll"] = True
        if fresh_start:
            ctx["fresh_start"] = True
            if seed_label:
                ctx["seed_track"] = seed_label
                ctx["instruction"] = (
                    f"The listener hand-picked '{seed_label}' as a seed and it is now "
                    f"playing. Build a radio station around it: pick exactly {count} "
                    "tracks that flow from this seed — matching its genre, energy and era. "
                    "The seed dominates; the taste profile is only a secondary tiebreaker. "
                    "Never queue anything in 'blocked_tracks'. Do NOT repeat the seed track."
                )
            elif self.vibe_prompt:
                ctx["instruction"] = (
                    f"The vibe '{self.vibe_prompt}' defines the genre and energy for "
                    f"{person.name} — match it even where it diverges from the taste "
                    "profile. Any songs or artists named in the vibe are must-plays: "
                    f"include them somewhere in the {count}-track set (not "
                    "necessarily first) and build around them. Use taste only as a "
                    "secondary tiebreaker, and vary from 'recent_history' so repeat "
                    "sessions differ. Never queue anything in 'blocked_tracks'."
                )
            else:
                ctx["instruction"] = (
                    f"Plain radio for {person.name} with no vibe and no seed. Lead with "
                    "what they typically ask for and like right now — see "
                    "'moods_this_time_of_day'/'moods_this_month' and "
                    "'likes_this_time_of_day'/'likes_this_month' — set the energy to suit "
                    f"'{tod}', and use the taste profile only as the backbone palette. "
                    f"Select exactly {count} tracks, varying from 'recent_history'. "
                    "Never queue anything in 'blocked_tracks'."
                )
        elif seed_label:
            # Ongoing auto-refill of a station seeded from a hand-picked song.
            # Keep it on-theme with the seed and what has actually played rather
            # than drifting back to the broad taste profile.
            ctx["seed_track"] = seed_label
            ctx["instruction"] = (
                f"This is an ongoing radio station seeded from '{seed_label}'. Keep it "
                f"flowing: pick exactly {count} tracks that stay on-theme with the "
                f"seed and the tracks already played this session (see 'recent_history'), "
                "evolving naturally rather than reverting to the broader taste profile. "
                "The seed dominates; taste is secondary. Never queue anything in "
                "'blocked_tracks' and don't repeat recent tracks."
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

    async def _resolve_and_order(
        self,
        context: dict[str, Any],
        queries: list[str],
        blocked: set[str],
        play_option: str,
        count: int,
    ) -> tuple[list[dict[str, Any]], dict[str, int], str]:
        """Candidate-grounding: resolve queries to a REAL pool, then let Claude
        select + order `count` of them by id and enqueue in that order.

        Returns (enqueued_tracks, energy_by_uri, dj_note). This eliminates
        unresolvable picks (Claude only chooses tracks that actually exist) and
        lets the ordering use measured BPM/key. Falls back to enqueuing the head
        of the pool if ordering yields nothing usable.
        """
        pool_tracks = await self._ma.resolve_queries(queries, blocked_uris=blocked)
        if not pool_tracks:
            return [], {}, ""

        pool: list[dict[str, Any]] = []
        for i, t in enumerate(pool_tracks):
            item: dict[str, Any] = {"id": i, "track": _track_label(t)}
            feats = self._features_for(t)
            if feats:
                if feats.get("bpm"):
                    item["bpm"] = feats["bpm"]
                if feats.get("camelot"):
                    item["camelot"] = feats["camelot"]
            pool.append(item)

        order_ctx = {**context, "tracks_to_add": count}
        try:
            order = await self._brain.order_pool(order_ctx, pool, count=count)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Pool ordering failed, falling back to pool head: %s", err)
            order = PoolOrder()

        energy_by_uri: dict[str, int] = {}
        chosen: list[dict[str, Any]] = []
        seen: set[str] = set()
        for pick in order.selection:
            if not (0 <= pick.id < len(pool_tracks)):
                continue
            track = pool_tracks[pick.id]
            uri = track.get("uri")
            if not uri or uri in seen:
                continue
            seen.add(uri)
            chosen.append(track)
            energy_by_uri[uri] = pick.energy
            if len(chosen) >= count:
                break

        if not chosen:  # ordering produced nothing usable — keep music flowing
            chosen = pool_tracks[:count]

        enqueued = await self._ma.enqueue_tracks(chosen, option=play_option)
        return enqueued, energy_by_uri, order.dj_note

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
            # Honour a Stop that landed while we were waiting for the lock. A
            # background top-up can clear tick()'s _dj_stopped gate, then block
            # on _tick_lock behind an in-flight enqueue; by the time it acquires
            # the lock the user may have pressed Stop (which sets the flag and
            # clears the queue). The post-lock "is the player playing?" check
            # below isn't enough — MA's reported state lags a stop by a beat, so
            # a stale tick would still see "playing" and refill. The flag is the
            # authoritative signal of user intent. Rebuilds (Start Radio / Set
            # Vibe / Nudge / person switch) lift the flag before calling, so
            # they are never blocked here.
            if self._dj_stopped:
                _LOGGER.debug("_run_decision(%s) aborted — DJ stopped by user", reason)
                return {"ok": True, "skipped": True, "reason": "dj_stopped"}
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

            # When grounding, generate a larger candidate pool to pick/order from.
            grounding = self._config.candidate_grounding
            gen_count = POOL_TARGET if grounding else QUEUE_TARGET
            context = await self.build_context(
                fresh_start=fresh_start, seed_label=seed_label, count=gen_count
            )
            self._dj_activity = {"phase": "picking", "detail": "Choosing tracks"}
            try:
                decision = await self._brain.decide(context)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("DJ decision failed: %s", err)
                self._dj_activity = {"phase": "idle"}
                return {"ok": False, "error": str(err)}

            # Stop can also land *during* the (slow) Claude call above. Bail
            # before we enqueue so a parked DJ never resurrects the queue.
            if self._dj_stopped:
                _LOGGER.debug("_run_decision(%s) aborted after decide — DJ stopped by user", reason)
                self._dj_activity = {"phase": "idle"}
                return {"ok": True, "skipped": True, "reason": "dj_stopped"}

            queries = [t.query for t in decision.next_tracks]
            if seed_queries:
                # Weave the listener's playlist tracks evenly through the
                # discovery picks; seed tracks lead so a familiar song starts.
                queries = self._interleave(seed_queries, queries)
            # Never repeat a track already heard/queued this session: drop the
            # active person's blocks plus everything seen so far this session.
            blocked = self._users.active.blocked_uris() | set(self._session_uris)
            reorder = (
                self._harmonic_reorder
                if (self._config.harmonic_sort and self._sonic.enabled)
                else None
            )
            self._enqueuing = True
            self._dj_activity = {
                "phase": "enqueuing", "detail": "Queueing tracks", "target": QUEUE_TARGET,
            }
            try:
                if grounding:
                    # Resolve to a real pool, let Claude pick/order QUEUE_TARGET
                    # by id, enqueue in that order. order_pool already sequences,
                    # so the harmonic reorder is skipped here.
                    enqueued, ground_energy, ground_note = await self._resolve_and_order(
                        context, queries, blocked, play_option, QUEUE_TARGET
                    )
                else:
                    enqueued = await self._ma.enqueue_queries(
                        queries, option=play_option, blocked_uris=blocked, reorder=reorder
                    )
                    ground_energy, ground_note = {}, ""
            finally:
                self._enqueuing = False
                self._dj_activity = {"phase": "idle"}
            if play_option == "play":
                await self._ma.ensure_playing()
            # Remember each track and the energy committed to it, then warm sonic
            # features so the next cycle's "energy_arc" carries real BPM/key.
            # Grounding maps energy by uri (from the pool order); the legacy path
            # maps it back via the query that resolved each track.
            query_energy = {t.query: t.energy for t in decision.next_tracks}
            for track in enqueued:
                uri = track.get("uri")
                self._remember_uri(uri)
                if not uri:
                    continue
                energy = ground_energy.get(uri) if grounding else query_energy.get(
                    track.get("_source_query")
                )
                if energy is not None:
                    self._track_energy[uri] = energy
            self._schedule_feature_warm(enqueued)
            dj_note = ground_note if (grounding and ground_note) else decision.dj_note
            # NOTE: the "added"/"artists" stats are NOT bumped here. They count
            # what actually PLAYS (see _note_track_change) so tracks that MA
            # accepts but then can't stream never inflate the numbers.

            if grounding:
                # Log the real tracks actually queued (with their committed energy).
                tracks_log = [
                    {"query": _track_label(t), "energy": self._track_energy.get(t.get("uri"), 5)}
                    for t in enqueued
                ]
            else:
                tracks_log = [
                    {"query": t.query, "reason": t.reason, "energy": t.energy}
                    for t in decision.next_tracks
                ]
            entry = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "reason": reason,
                "dj_note": dj_note,
                "vibe_reading": decision.vibe_reading.model_dump(),
                "mood_shift": decision.mood_shift,
                "mood_shift_reason": decision.mood_shift_reason,
                "tracks": tracks_log,
                "enqueued": len(enqueued),
            }
            self.decision_log.appendleft(entry)

            # Emit an HA event so users can trigger automations off DJ decisions.
            await self._ha.fire_event(
                "tidesync_dj_decision",
                {
                    "dj_note": dj_note,
                    "mood_shift": decision.mood_shift,
                    "enqueued": len(enqueued),
                    "vibe": self.vibe_prompt,
                    "reason": reason,
                },
            )

            # Periodically refine the household taste profile.
            if self._taste.record_decision():
                history = await self._ma.get_history(n=30)
                await self._taste.maybe_update(self._brain, history)

            # Periodically refine the ACTIVE person's taste from their own
            # signals (likes/blocks/moods), in the background so it never blocks
            # the enqueue path.
            active = self._users.active
            count = self._person_decision_counts.get(active.slug, 0) + 1
            self._person_decision_counts[active.slug] = count
            if count % PERSON_REFINE_EVERY == 0:
                self._schedule_person_refine(active)

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
        self._stopped_at = time.monotonic()  # belt-and-suspenders refill guard
        self._radio_seed_label = None
        # Cancel any in-flight auto-fill so a debounced tick can't refill after
        # we clear the queue.
        if self._pending_tick and not self._pending_tick.done():
            self._pending_tick.cancel()
        # Stop AND clear EVERY active queue, not just the selected one — the
        # player actually playing may differ from the selected one, which is why
        # Stop used to "keep playing".
        stopped = await self._ma.stop_all()
        # Re-baseline track tracking so a later restart starts clean.
        self._current_track_id = None
        self._last_current = None
        _LOGGER.info("DJ stopped by user — all active queues halted and cleared")
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
        # A rebuild already in flight: don't clear/enqueue on top of it. Two
        # overlapping rebuilds (e.g. an impatient double Start Radio, or a second
        # browser tab) are exactly what makes the current track skip — bail early
        # and let the first one finish.
        if self._rebuild_lock.locked():
            _LOGGER.info("Rebuild (%s) ignored — a session is already starting", reason)
            return {
                "ok": False,
                "busy": True,
                "error": "A radio session is already starting — hang tight.",
            }
        async with self._rebuild_lock:
            return await self._rebuild_locked(
                reason, seed_uri=seed_uri, seed_label=seed_label, seed_queries=seed_queries
            )

    async def _rebuild_locked(
        self,
        reason: str,
        seed_uri: str | None = None,
        seed_label: str | None = None,
        seed_queries: list[str] | None = None,
    ) -> dict[str, Any]:
        """The guts of a rebuild, run while holding `_rebuild_lock`."""
        # Any rebuild is a user-initiated restart — lift a prior Stop so the
        # auto-DJ resumes topping up the queue.
        self._dj_stopped = False
        # Cancel any debounced auto-fill so a queue_low tick can't fire mid-rebuild
        # and enqueue against the queue we're about to clear and refill.
        if self._pending_tick and not self._pending_tick.done():
            self._pending_tick.cancel()
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
        # Beginning a listening session (Start Radio / seed) → reset stats+clock.
        if reason in _SESSION_START_REASONS:
            self._reset_session()
        # A genuinely new session → plan the long-form arc and reset energy bias.
        if reason in _FRESH_PLAN_REASONS:
            self._energy_bias = 0
            await self._generate_set_plan(reason, seed_label=seed_label)
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
        """Re-roll: 'you got it wrong, try something else.'

        Rebuilds the queue but signals Claude to take a deliberately DIFFERENT
        direction (genre/energy/feel) than what just played — not a same-vibe
        refresh. Keeps the session's set plan.
        """
        self._reroll = True
        try:
            return await self._rebuild("nudge")
        finally:
            self._reroll = False

    async def nudge_energy(self, direction: str) -> dict[str, Any]:
        """Push the next block calmer ('down') or more energetic ('up')."""
        delta = {"up": 1, "down": -1}.get(direction)
        if delta is None:
            return {"ok": False, "error": "direction must be 'up' or 'down'"}
        self._energy_bias = max(-ENERGY_BIAS_MAX, min(ENERGY_BIAS_MAX, self._energy_bias + delta))
        _LOGGER.info("Energy bias -> %+d (%s)", self._energy_bias, direction)
        return await self._rebuild("energy")

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

    def user_taste_profiles(self) -> list[dict[str, Any]]:
        """Read-only view of each person's LEARNED taste (for the hidden viewer)."""
        out: list[dict[str, Any]] = []
        for p in self._users.people():
            person = self._users.get(p["slug"])
            out.append({
                "slug": p["slug"],
                "name": p["name"],
                "active": p["active"],
                "summary": (person.summary if person else "") or "(still learning — like/block tracks to teach it)",
            })
        return out

    async def select_user(self, slug: str) -> dict[str, Any]:
        if not self._users.select(slug):
            return {"ok": False, "error": f"Unknown person: {slug}"}
        _LOGGER.info("Switched active person to %s", slug)
        # Start the new person from their own taste — never force a past vibe as
        # the active driver. A one-off request ("put me to sleep") must not become
        # a standing vibe on switch; recent moods still inform softly via context
        # ("moods_this_time_of_day") without dominating the set.
        self.vibe_prompt = ""
        # Rebuild immediately so playback reflects the new person's preferences.
        result = await self._rebuild("person_switch")
        result["active"] = self._users.active_slug
        return result

    def add_user(self, name: str) -> dict[str, Any]:
        if not name.strip():
            return {"ok": False, "error": "Name is required."}
        person = self._users.add_person(name)
        return {"ok": True, "slug": person.slug, "name": person.name}

    def rename_user(self, slug: str, name: str) -> dict[str, Any]:
        if not name.strip():
            return {"ok": False, "error": "Name is required."}
        if not self._users.rename(slug, name):
            return {"ok": False, "error": f"Unknown person: {slug}"}
        return {"ok": True, "slug": slug, "name": name.strip()}

    def delete_user(self, slug: str) -> dict[str, Any]:
        if not self._users.remove(slug):
            return {"ok": False, "error": "Can't delete (unknown or the last person)."}
        return {"ok": True, "active": self._users.active_slug}

    # ------------------------------------------------------------------ #
    # Per-person taste learning (background)
    # ------------------------------------------------------------------ #
    def _schedule_person_refine(self, person: Person) -> None:
        """Fire-and-forget background refinement of one person's taste."""
        task = asyncio.create_task(self._refine_person_taste(person))
        self._refine_tasks.add(task)
        task.add_done_callback(self._refine_tasks.discard)

    async def _refine_person_taste(self, person: Person) -> None:
        """Refine `person.summary` from their tracked signals. Always non-fatal."""
        try:
            signals = person.learning_signals()
            if not any(signals.values()):
                return  # nothing learned yet
            new_summary = await self._brain.summarize_person_taste(
                signals, previous=person.summary
            )
            if new_summary and new_summary != person.summary:
                person.set_summary(new_summary)
                self._users.save(person)
                _LOGGER.info("Refined taste profile for %s", person.name)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Person taste refine failed for %s: %s", person.name, err)

    # ------------------------------------------------------------------ #
    # Players
    # ------------------------------------------------------------------ #
    async def list_players(self) -> list[dict[str, Any]]:
        # Only surface players that are actually available — never offer one the
        # user can't play on (which would just fail).
        players = [p for p in await self._ma.get_players() if p.get("available")]
        for p in players:
            p["selected"] = p["player_id"] == self._ma.selected_player_id
        return players

    async def select_player(self, player_id: str) -> dict[str, Any]:
        """Switch the target player. If music is playing, TRANSFER the queue to
        the new player and resume — no interruption (like MA's Transfer Queue)."""
        old = self._ma.selected_player_id
        # Snapshot the live queue (current + upcoming, by uri) if we're playing.
        snapshot: list[str] | None = None
        try:
            state = await self._ma.get_play_state()
            if old and old != player_id and state in ("playing", "paused"):
                q = await self._ma.get_queue()
                uris: list[str] = []
                cu = _track_uri(q.get("current_item"))
                if cu:
                    uris.append(cu)
                for it in (q.get("items") or []):
                    u = _track_uri(it)
                    if u:
                        uris.append(u)
                snapshot = uris
        except Exception:  # noqa: BLE001
            pass

        # Prefer MA's native transfer (preserves the exact position).
        if snapshot is not None and await self._ma.transfer_queue(player_id):
            self._ma.set_player(player_id)
            self._save_state(last_player=player_id)
            return {"ok": True, "player_id": player_id, "transferred": "native"}

        # Otherwise move the selection and re-create the queue on the new player.
        self._ma.set_player(player_id)
        self._save_state(last_player=player_id)
        if snapshot:
            if old:
                await self._ma.stop_player(old)  # don't leave two players playing
            await self._ma.clear_queue()
            for i, u in enumerate(snapshot):
                await self._ma.enqueue_uri(u, "play" if i == 0 else "add")
            await self._ma.ensure_playing()
            return {"ok": True, "player_id": player_id, "transferred": "reenqueue"}
        return {"ok": True, "player_id": player_id, "transferred": False}

    async def set_volume(self, level: int) -> dict[str, Any]:
        ok = await self._ma.set_volume(level)
        return {"ok": ok, "volume": max(0, min(100, int(level)))}

    async def previous_track(self) -> dict[str, Any]:
        return {"ok": await self._ma.previous()}

    # -- persistent engine state (last-used player, etc.) -----------------
    def _state_path(self):
        return self._config.data_dir / "state.json"

    def _load_state(self) -> dict[str, Any]:
        try:
            p = self._state_path()
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _save_state(self, **kw: Any) -> None:
        try:
            data = self._load_state()
            data.update(kw)
            self._state_path().write_text(json.dumps(data), encoding="utf-8")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not persist engine state: %s", err)

    async def pause(self) -> dict[str, Any]:
        ok = await self._ma.pause()
        return {"ok": ok}

    async def toggle_playback(self) -> dict[str, Any]:
        """Toggle play/pause using MA's queue-level transport.

        Two failure modes of the old player-level `play_pause` are handled here:
          * It targeted the *selected* player even when a different queue was the
            one actually playing (group / user-switched-in-MA). We resolve the
            queue MA reports as active first.
          * It could not resume a queue MA had let go **idle/stopped** after a
            long pause — `play_pause` from idle is a no-op. We detect idle and
            send `resume` instead.
        The playing/paused distinction is the only eventually-consistent part;
        idle vs active is stable, so reading state to branch is safe. We return
        the INTENDED state so the UI can hold the icon during MA's state lag.
        """
        try:
            queue_id, state = await self._ma.get_active_queue()
        except Exception:  # noqa: BLE001
            queue_id, state = (None, None)
        state = (state or "").lower()

        if state == "playing":
            ok = await self._ma.queue_pause(queue_id)
            intended = "paused"
        elif state == "paused":
            ok = await self._ma.queue_play(queue_id)
            intended = "playing"
        else:
            # idle / stopped / unknown — resume the queue from where it left off.
            ok = await self._ma.queue_resume(queue_id) if queue_id else False
            intended = "playing"
        return {"ok": ok, "state": intended}

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
        avail = [p for p in players if p.get("available")]
        avail_ids = {p["player_id"] for p in avail}

        # Keep the current selection only if it is still AVAILABLE (not merely
        # present) — never hand back a player that would fail to play.
        if self._ma.selected_player_id:
            if self._ma.selected_player_id in avail_ids:
                return self._ma.selected_player_id
            _LOGGER.warning(
                "Selected player %s is no longer available — reselecting",
                self._ma.selected_player_id,
            )
            self._ma.selected_player_id = None
            self._ma.active_queue_id = None

        if not avail:
            return None  # nothing playable — don't pick an unavailable player

        # Default to the LAST-USED player if it's available again.
        last = self._load_state().get("last_player")
        if last and last in avail_ids:
            self._ma.set_player(last)
            return self._ma.selected_player_id

        # Otherwise prefer a powered/active one, else any available player.
        powered = [p for p in avail if p.get("powered")]
        chosen = (powered or avail)[0]
        self._ma.set_player(chosen["player_id"])
        return self._ma.selected_player_id

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
        """Toggle the like on the current track (Tidal-style heart).

        The per-person like is the source of truth for the heart state; the MA
        favorite sync is best-effort so the toggle still works if MA rejects it.
        """
        uri, label = await self._resolve_current()
        if not uri:
            return {"ok": False, "error": "Nothing is playing to like."}
        person = self._users.active
        if person.is_liked(uri):
            try:
                await self._ma.remove_favorite(uri)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("MA un-favorite failed (keeping local un-like): %s", err)
            person.remove_like(uri)
            self._users.save(person)
            return {"ok": True, "liked": False, "label": label or uri, "person": person.name}
        try:
            await self._ma.add_favorite(uri)
        except Exception as err:  # noqa: BLE001
            return {"ok": False, "error": f"Music Assistant rejected the like: {err}"}
        person.add_like(label, uri)
        self._users.save(person)
        return {"ok": True, "liked": True, "label": label or uri, "person": person.name}

    async def block_current(self) -> dict[str, Any]:
        """Block the current track for the active person (escalating cooldown) and skip it now."""
        uri, label = await self._resolve_current()
        if not uri:
            return {"ok": False, "error": "Nothing is playing to block."}
        person = self._users.active
        person.add_block(label, uri)
        self._users.save(person)
        _LOGGER.info("Blocked %s for %s", label or uri, person.name)
        # A block is a high-value taste signal — refine this person now (background).
        self._schedule_person_refine(person)
        # Stop the song immediately — that's why you blocked it.
        await self._ma.skip()
        return {"ok": True, "blocked": label or uri, "person": person.name}

    async def save_session_playlist(self, name: str) -> dict[str, Any]:
        """Create a playlist (on whatever music source the session played) from the
        tracks heard this session."""
        uris = list(self._played_uris.keys())
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

        cur_uri = _track_uri(current_item) or self.current_uri
        can_act = bool(cur_uri)
        current_liked = self._users.active.is_liked(cur_uri)
        # Prefer the player's authoritative state so the play/pause icon flips on
        # pause; the queue meta state doesn't always report a pause.
        try:
            player_state = await self._ma.get_play_state()
        except Exception:  # noqa: BLE001
            player_state = queue.get("state")
        try:
            volume = await self._ma.get_volume()
        except Exception:  # noqa: BLE001
            volume = None
        return {
            "configured": bool(self._config.anthropic_api_key),
            "now_playing": _track_label(current_item),
            "album_art": album_art,
            "player_state": player_state,  # "playing" | "paused" | "idle" | None
            "volume": volume,              # 0-100 or null
            "dj_stopped": self._dj_stopped,  # user pressed Stop; auto-DJ parked
            "vibe": self.vibe_prompt or None,
            "current_liked": current_liked,
            "energy_bias": self._energy_bias,
            "set_phase": (self._arc_position() or {}).get("current_phase"),
            "time_of_day": _time_of_day(),
            "items_remaining": queue.get("items_remaining", 0),
            "ma_connected": self._ma.is_connected,
            "ma_error": self._ma.last_error,
            "ma_host": self._config.ma_host,
            "selected_player_id": self._ma.selected_player_id,
            "taste_seeded": self._taste.is_bootstrapped,
            "session_track_count": len(self._played_uris),
            "can_like": can_act,
            "can_block": can_act,
            "people": self._users.people(),
            "active_person": self._users.active.name,
            "session_minutes": round((time.monotonic() - self.session_started) / 60),
            "stats": self.stats,
            "model": self._config.claude_model,
        }

    async def dj_status(self) -> dict[str, Any]:
        """Rich view of what the DJ is doing now — for the UI status modal.

        Surfaces the live activity phase (planning/picking/enqueuing) for the
        start-up loading bar, the long-form set plan + where we are in it, the
        energy/tempo curve built so far, and the latest decision's reasoning.
        """
        try:
            queue = await self._ma.get_queue()
        except Exception:  # noqa: BLE001
            queue = {}
        current_item = queue.get("current_item")
        items_remaining = queue.get("items_remaining", 0)

        # Live progress for the loading bar: while enqueuing, the queue fills up
        # toward `target`, so report the live depth as the numerator.
        activity = dict(self._dj_activity)
        if activity.get("phase") == "enqueuing":
            activity["enqueued"] = items_remaining

        try:
            history = await self._ma.get_history(n=20)
        except Exception:  # noqa: BLE001
            history = []

        latest_decision = None
        if self.decision_log:
            d = self.decision_log[0]
            latest_decision = {
                "timestamp": d.get("timestamp"),
                "reason": d.get("reason"),
                "dj_note": d.get("dj_note"),
                "vibe_reading": d.get("vibe_reading"),
                "mood_shift": d.get("mood_shift"),
                "mood_shift_reason": d.get("mood_shift_reason"),
                "tracks": d.get("tracks", []),
                "enqueued": d.get("enqueued"),
            }

        return {
            "activity": activity,  # {"phase", "detail"?, "target"?, "enqueued"?}
            "set_plan": self._set_plan_dict(),  # {"arc_note", "phases":[...]} or None
            "arc_position": self._arc_position(),  # {"elapsed_minutes","pct_through","current_phase"} or None
            "energy_arc": self._energy_arc(history, current_item),  # [{track,energy?,bpm?,camelot?,now_playing?}]
            "latest_decision": latest_decision,
            "now_playing": _track_label(current_item),
            "items_remaining": items_remaining,
            "dj_stopped": self._dj_stopped,
            "vibe": self.vibe_prompt or None,
            "energy_bias": self._energy_bias,
        }
