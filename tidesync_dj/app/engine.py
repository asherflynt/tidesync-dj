"""DJ engine (v2): plans, retrieves, sequences and keeps the queue topped up.

The engine owns one Music Assistant queue (the selected player). It keeps a
few tracks queued ahead and re-plans on every refill, so skips, likes and
energy changes take effect within a song or two instead of after a 30-track
block. All picks come from the local library mirror.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from library import COVERAGE_DOMAINS, Library
from ma import MAClient, MAError, MAUnavailable
from plan import SET_LENGTH, ClaudePlanner, Plan, apply_bias, heuristic_plan
from retrieval import gather
from sequencer import Track, energy_curve, sequence
from timectx import time_of_day_slot

_LOGGER = logging.getLogger("tidesync.engine")

AHEAD_TARGET = 8  # upcoming tracks the DJ keeps queued
LOW_WATER = 4  # refill when fewer than this remain
OPENING = 10  # tracks queued when a session starts
CANDIDATE_CAP = 400  # best candidates handed to the sequencer
LISTING_SYNC_EVERY = 6 * 3600
ANALYSIS_SYNC_EVERY = 30 * 60
CATCH_UP_EVERY = 90  # next sync pass when the last one was cut short with tracks still turning up
COVERAGE_RETRY = 6 * 3600  # re-ask for MA's coverage this often after it was refused
SCAN_TASK_ID = "audio_analysis_background_scan"
COVERAGE_MESSAGES = {
    "needs_admin": "Music Assistant only shares its analysis progress with admin users, "
                   "and TideSync's Music Assistant token is not an admin token.",
    "not_enabled": "This kind of analysis is not turned on in Music Assistant.",
    "unsupported": "This Music Assistant version does not report its analysis progress.",
    "error": "Music Assistant did not report its analysis progress.",
}
SKIP_FRACTION = 0.5  # stopped before this share of the track = a skip


def _artist(media: dict[str, Any]) -> str:
    return ", ".join(a.get("name", "") for a in media.get("artists") or [] if a.get("name"))


def _image(obj: dict[str, Any] | None) -> str | None:
    """Album-art URL served through this app (works inside HA ingress)."""
    if not isinstance(obj, dict):
        return None
    candidates = [obj.get("image")]
    media = obj.get("media_item") or {}
    candidates.append(media.get("image"))
    for holder in (obj, media, media.get("album") or {}):
        candidates.extend((holder.get("metadata") or {}).get("images") or [])
    for img in candidates:
        if not isinstance(img, dict):
            continue
        if img.get("proxy_id"):
            return f"art/{img['proxy_id']}"
        if img.get("remotely_accessible") and str(img.get("path", "")).startswith("http"):
            return img["path"]
    return None


def track_view(item: dict[str, Any]) -> dict[str, Any]:
    """One shape for queue items and search results: what the dashboard renders."""
    media = item.get("media_item") or item
    return {
        "name": media.get("name") or item.get("name"),
        "artist": _artist(media) or None,
        "uri": media.get("uri") or item.get("uri"),
        "image": _image(item),
        "duration": media.get("duration") or item.get("duration"),
        "item_id": item.get("queue_item_id"),
    }


def coarse_duration(seconds: float) -> str:
    """Plain, deliberately rough wording for a long wait ("about 3 weeks")."""
    days = seconds / 86400
    if days < 1:
        return "less than a day"
    if days < 1.5:
        return "about a day"
    if days < 13:
        return f"about {round(days)} days"
    if days < 60:
        return f"about {round(days / 7)} weeks"
    if days < 548:
        return f"about {round(days / 30.4)} months"
    return f"about {round(days / 365)} years"


def _label(item: dict[str, Any] | None) -> str | None:
    if not item:
        return None
    view = track_view(item)
    if not view["name"]:
        return None
    return f"{view['artist']} - {view['name']}" if view["artist"] else view["name"]


class DJEngine:
    """Background DJ. Public methods return ``{"ok": bool, ...}`` dicts for the API."""

    def __init__(self, config: Any, ma: MAClient, ha: Any, planner: ClaudePlanner, users: Any,
                 library: Library) -> None:
        self._config = config
        self._ma = ma
        self._ha = ha
        self._planner = planner
        self._users = users
        self._lib = library
        self._state_file = Path(config.data_dir) / "engine_state.json"
        state = self._load_state()
        self.player_id: str | None = state.get("player_id")
        self.vibe_prompt: str = ""
        self.plan: Plan = heuristic_plan("", slot=time_of_day_slot())
        self._energy_bias = 0
        self._dj_stopped = True
        self._seed_ids: list[int] = []
        self._position = 0  # slots handed out this session (drives the energy curve)
        self._played: list[Track] = []  # session history incl. queued picks, oldest first
        self._picked_ids: set[int] = set()
        self._arc_log: deque[dict[str, Any]] = deque(maxlen=60)
        self.session_started = time.monotonic()
        self.decision_log: deque[dict[str, Any]] = deque(maxlen=20)
        self.stats = {"tracks_played": 0, "skips": 0, "added": 0, "new_artists": 0}
        self._artists_seen: set[str] = set()
        self._activity: dict[str, Any] = {"phase": "idle"}
        self._refill_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[Any]] = []
        self._sync_status: dict[str, Any] = {"state": "pending"}
        self._coverage_reading: dict[str, dict[str, Any]] = {}  # this pass's raw MA replies
        self._coverage_errors: dict[str, str] = {}  # domain -> reason code
        self._coverage_logged: set[tuple[str, str]] = set()
        self._coverage_skip_until: dict[str, float] = {}  # domain -> monotonic time to ask again
        self._ma_index: dict[str, Any] | None = None
        self._last_error: str | None = None
        self._last_feedback: tuple[str, float] = ("", 0.0)
        self._ha_vibe_seen: str | None = None

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self._ma.subscribe(self._on_event)
        self._tasks.append(asyncio.create_task(self._sync_loop(), name="sync"))
        self._tasks.append(asyncio.create_task(self._tick_loop(), name="tick"))
        if getattr(self._config, "vibe_input_text_entity", None):
            self._tasks.append(asyncio.create_task(self._ha_vibe_loop(), name="ha-vibe"))
        if getattr(self._config, "ha_action_entity", ""):
            self._tasks.append(asyncio.create_task(self._ha_action_loop(), name="ha-action"))

    async def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._ma.close()
        self._lib.close()

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self._state_file.read_text())
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        try:
            self._state_file.write_text(json.dumps({"player_id": self.player_id}))
        except OSError as err:
            _LOGGER.warning("Could not save engine state: %s", err)

    # -- background loops ---------------------------------------------------

    async def _sync_loop(self) -> None:
        """Keep the library mirror current.

        The listing is re-read every few hours and the sound index every half
        hour. Analysed tracks are looked for every half hour too, or within a
        couple of minutes while a pass keeps finding more than it had time to
        check (e.g. after thousands of analysis rows arrived at once).
        """
        last_listing = last_index = float("-inf")
        while True:
            more = False
            try:
                if time.monotonic() - last_listing > LISTING_SYNC_EVERY or not self._lib.stats()["total"]:
                    self._sync_status = {"state": "listing"}
                    await self._lib.sync_listing(self._ma)
                    last_listing = time.monotonic()
                if time.monotonic() - last_index >= ANALYSIS_SYNC_EVERY:
                    self._sync_status = {"state": "sound index"}
                    await self._lib.sync_analysed(self._ma)
                    self._ma_index = await self._read_ma_index()
                    last_index = time.monotonic()
                self._sync_status = {"state": "analysis"}
                await self._read_coverage()
                features = await self._lib.sync_features(self._ma, expected=self._expected_analysed())
                more = bool(features.get("more"))
                await self._keep_scan_running(self._pending(), read=False)
                self._sync_status = {**self._sync_status, "state": "idle", "features": features,
                                     "at": datetime.now().isoformat(timespec="seconds")}
            except (MAUnavailable, MAError) as err:
                self._sync_status = {"state": "error", "error": str(err)}
                _LOGGER.warning("Library sync failed: %s", err)
            except Exception:  # noqa: BLE001 - keep the loop alive
                _LOGGER.exception("Library sync crashed")
                self._sync_status = {"state": "error", "error": "unexpected error (see log)"}
            await asyncio.sleep(CATCH_UP_EVERY if more else ANALYSIS_SYNC_EVERY)

    async def _read_ma_index(self) -> dict[str, Any] | None:
        """Sizes of MA's two sound indexes (sonic_similarity/status, any user may read it)."""
        status = await self._ma.try_cmd("sonic_similarity/status")
        if not isinstance(status, dict):
            return None
        clap = status.get("clap_index_size")
        return {
            # tracks MA can find similar songs for / return as similar (feature "Traits" index)
            "similar_index_size": int(status.get("index_size") or 0),
            # tracks a vibe can match by sound (CLAP "Character" index); None when it is off
            "sound_index_size": int(clap) if clap is not None else None,
            "text_search": bool(status.get("text_search_enabled")),
        }

    async def _read_coverage(self) -> None:
        """Ask MA how much of the library it has analysed, and keep the reading.

        ``audio_analysis/coverage`` needs an admin token. Without one (or on a
        server that lacks it, or with that analysis provider turned off) the
        reason is logged once and the question is asked again only every few
        hours, so neither log is flooded.
        """
        self._coverage_reading = {}
        for domain in COVERAGE_DOMAINS:
            if time.monotonic() < self._coverage_skip_until.get(domain, 0.0):
                continue
            try:
                reply = await self._ma.cmd("audio_analysis/coverage", aa_domain=domain)
            except MAUnavailable as err:
                self._coverage_failed(domain, "error", str(err))
                return
            except MAError as err:
                if err.not_permitted or err.unsupported:
                    # the same answer for every domain; ask again later (the token may change)
                    reason = "needs_admin" if err.not_permitted else "unsupported"
                    for each in COVERAGE_DOMAINS:
                        self._coverage_failed(each, reason, err.details, quiet=each != domain)
                        self._coverage_skip_until[each] = time.monotonic() + COVERAGE_RETRY
                    return
                reason = "not_enabled" if "not available" in err.details.lower() else "error"
                self._coverage_failed(domain, reason, err.details)
                if reason == "not_enabled":
                    self._coverage_skip_until[domain] = time.monotonic() + COVERAGE_RETRY
                continue
            if not isinstance(reply, dict) or reply.get("analyzed") is None:
                self._coverage_failed(domain, "error", f"unexpected reply {reply!r:.80}")
                continue
            self._coverage_errors.pop(domain, None)
            self._coverage_logged = {k for k in self._coverage_logged if k[0] != domain}
            self._coverage_reading[domain] = reply
            self._lib.record_coverage(domain, int(reply["analyzed"]), int(reply.get("pending") or 0))

    def _coverage_failed(self, domain: str, reason: str, details: str, *, quiet: bool = False) -> None:
        """Remember why a domain's coverage is missing; log each new reason once."""
        self._coverage_errors[domain] = reason
        if (domain, reason) not in self._coverage_logged:
            self._coverage_logged.add((domain, reason))
            if not quiet:
                _LOGGER.info("Music Assistant analysis progress (%s) is not available: %s. %s",
                             domain, details, COVERAGE_MESSAGES.get(reason, ""))

    def _expected_analysed(self) -> int | None:
        """MA's count of analysed tracks from this pass, for the feature sync's catch-up check.

        Tempo and key come from Smart Fades, so its count is preferred.
        """
        for domain in ("smart_fades", "sonic_analysis"):
            if domain in self._coverage_reading:
                return int(self._coverage_reading[domain]["analyzed"])
        return None

    def _pending(self) -> int | None:
        pending = [int(r.get("pending") or 0) for r in self._coverage_reading.values()]
        return max(pending) if pending else None

    async def _keep_scan_running(self, pending: int | None = None, *, read: bool = True) -> None:
        """Restart Music Assistant's analysis scan when it has stopped with work left.

        MA runs the scan nightly for a few hours; with this option on, a large library
        is analysed around the clock instead. Needs an admin token; silently skipped otherwise.

        :param pending: tracks MA still has to analyse, from this pass's coverage
            reading; read here when not given (unless ``read`` is False).
        """
        if not getattr(self._config, "keep_analysis_running", False):
            return
        if pending is None and read:
            await self._read_coverage()
            pending = self._pending()
        if pending is None:
            return
        task = await self._ma.try_cmd("tasks/get", task_id=SCAN_TASK_ID)
        if not task:
            return
        self._sync_status["scan"] = {"pending": pending, "status": task.get("status")}
        if pending and task.get("status") not in ("running", "pending"):
            started = await self._ma.try_cmd("tasks/run", task_id=SCAN_TASK_ID)
            _LOGGER.info("Analysis scan was %s with %s tracks pending; restarted: %s",
                         task.get("status"), pending, bool(started))

    async def _tick_loop(self) -> None:
        interval = max(10, int(getattr(self._config, "dj_tick_interval", 30)))
        while True:
            await asyncio.sleep(interval)
            if self._dj_stopped:
                continue
            try:
                await self._top_up("auto")
            except (MAUnavailable, MAError) as err:
                self._last_error = str(err)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Tick failed")

    async def _ha_vibe_loop(self) -> None:
        """Follow an HA text helper. Acts only when the helper itself changes, so a vibe
        set in the dashboard is not overwritten by the helper's old value."""
        entity = self._config.vibe_input_text_entity
        while True:
            with contextlib.suppress(Exception):
                value = (await self._ha.get_state_value(entity) or "").strip()
                if self._ha_vibe_seen is None:
                    self._ha_vibe_seen = value
                elif value != self._ha_vibe_seen:
                    self._ha_vibe_seen = value
                    if value:
                        await self.set_vibe(value)
            await asyncio.sleep(15)

    async def _ha_action_loop(self) -> None:
        entity = self._config.ha_action_entity
        last: str | None = None
        while True:
            with contextlib.suppress(Exception):
                value = (await self._ha.get_state_value(entity) or "").strip()
                if last is None:
                    last = value
                elif value and value != last:
                    last = value
                    await self._dispatch_action(value)
            await asyncio.sleep(5)

    async def _dispatch_action(self, value: str) -> None:
        low = value.lower().strip()
        head, _, tail = value.partition(":")
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
        elif low in ("energy up", "energy_up", "more energy"):
            await self.nudge_energy("up")
        elif low in ("energy down", "energy_down", "calmer", "less energy"):
            await self.nudge_energy("down")
        elif tail and head.strip().lower() in ("player", "play_on", "speaker"):
            await self._select_player_by_name(tail.strip())
        elif tail and head.strip().lower() in ("vibe", "mood"):
            await self.set_vibe(tail.strip())
        else:
            await self.set_vibe(value)

    # -- Music Assistant helpers --------------------------------------------

    async def _queue(self) -> dict[str, Any]:
        if not self.player_id:
            return {}
        return await self._ma.cmd("player_queues/get", queue_id=self.player_id) or {}

    async def _upcoming(self, queue: dict[str, Any]) -> list[dict[str, Any]]:
        total = int(queue.get("items") or 0)
        index = queue.get("current_index")
        start = 0 if index is None else int(index) + 1
        if not self.player_id or start >= total:
            return []
        return await self._ma.cmd(
            "player_queues/items", queue_id=self.player_id, limit=200, offset=start
        ) or []

    async def list_players(self) -> list[dict[str, Any]]:
        players = await self._ma.cmd("players/all") or []
        out = [
            {"player_id": p["player_id"], "name": p.get("display_name") or p.get("name"),
             "selected": p["player_id"] == self.player_id}
            for p in players
            if p.get("available") and p.get("enabled", True) and not p.get("hide_in_ui")
        ]
        return sorted(out, key=lambda p: str(p["name"]).lower())

    async def select_player(self, player_id: str) -> dict[str, Any]:
        """Switch speakers. An active session moves with its queue to the new player."""
        if player_id == self.player_id:
            return {"ok": True, "player_id": player_id}
        old = self.player_id
        moved = False
        if old and not self._dj_stopped:
            try:
                await self._ma.cmd("player_queues/transfer", source_queue_id=old, target_queue_id=player_id)
                moved = True
            except (MAError, MAUnavailable) as err:
                _LOGGER.warning("Queue transfer failed: %s", err)
        self.player_id = player_id
        self._save_state()
        return {"ok": True, "player_id": player_id, "moved": moved}

    async def _select_player_by_name(self, name: str) -> None:
        for player in await self.list_players():
            if name.lower() in (str(player["name"]).lower(), player["player_id"].lower()):
                await self.select_player(player["player_id"])
                return

    async def _ensure_player(self) -> str | None:
        if self.player_id:
            return self.player_id
        players = await self.list_players()
        if players:
            self.player_id = players[0]["player_id"]
            self._save_state()
        return self.player_id

    # -- planning + refill --------------------------------------------------

    def _person_ids(self) -> tuple[list[int], set[int]]:
        """(recently liked track ids, blocked track ids) for the active listener."""
        person = self._users.active
        liked = [self._lib.id_for_uri(lk.uri) for lk in person.likes[-25:]]
        blocked = {self._lib.id_for_uri(uri) for uri in person.blocked_uris()}
        return [i for i in liked if i is not None], {i for i in blocked if i is not None}

    async def _make_plan(self) -> Plan:
        slot = time_of_day_slot()
        plan: Plan | None = None
        if self.vibe_prompt and self._planner.enabled:
            self._activity = {"phase": "planning"}
            plan = await self._planner.plan(
                self.vibe_prompt, slot=slot, energy_bias=0,
                context={"listener": self._users.active.name,
                         "recent_moods": self._users.active.moods_for(slot)},
            )
            if plan:
                apply_bias(plan, self._energy_bias)
        return plan or heuristic_plan(self.vibe_prompt, slot=slot, energy_bias=self._energy_bias)

    def _targets(self, count: int) -> list[float]:
        position = self._position
        if self.plan.arc in ("flat", "wave"):
            position %= SET_LENGTH
        return energy_curve(self.plan.arc, count, base=self.plan.base_energy, span=self.plan.span,
                            start=position, total=SET_LENGTH)

    async def _refill(self, reason: str, count: int, *, replace: bool) -> dict[str, Any]:
        """Pick ``count`` tracks and queue them. ``replace`` swaps out everything upcoming."""
        async with self._refill_lock:
            try:
                return await self._refill_locked(reason, count, replace=replace)
            except (MAUnavailable, MAError) as err:
                self._last_error = str(err)
                return {"ok": False, "error": str(err)}
            finally:
                self._activity = {"phase": "idle"}

    async def _refill_locked(self, reason: str, count: int, *, replace: bool) -> dict[str, Any]:
        player = await self._ensure_player()
        if not player:
            return {"ok": False, "error": "No available player found in Music Assistant."}
        counts = self._lib.stats()
        if not counts["analysed"]:
            return {"ok": False, "error": self._not_ready_message(counts)}
        self._activity = {"phase": "picking"}
        liked, blocked = self._person_ids()
        queue = await self._queue()
        upcoming = [] if replace else await self._upcoming(queue)
        queued_ids = {tid for it in upcoming
                      if (tid := self._lib.id_for_uri((it.get("media_item") or {}).get("uri"))) is not None}
        current = queue.get("current_item") or {}
        current_id = self._lib.id_for_uri((current.get("media_item") or {}).get("uri"))
        seeds = list(self._seed_ids)
        if not seeds and not self.plan.queries and current_id is not None:
            seeds = [current_id]  # no vibe, no seed: stay near what is playing
        exclude = self._picked_ids | blocked | queued_ids
        if current_id is not None:
            exclude.add(current_id)
        targets = self._targets(count)
        scores, sources = await gather(self._ma, self._lib, self.plan, seed_ids=seeds,
                                       liked_ids=liked, exclude_ids=exclude, targets=targets)
        best = dict(sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:CANDIDATE_CAP])
        candidates = self._lib.candidates(best)
        previous = self._history(current_id, [] if replace else sorted(queued_ids))
        picks = sequence(candidates, targets, previous=previous)
        if not picks:
            if counts["analysed"] < counts["total"]:
                return {"ok": False, "error": f"Nothing left to play that fits among the "
                        f"{counts['analysed']:,} songs ready so far. Try a different vibe."}
            return {"ok": False, "error": "Nothing left to play that fits. Try a different vibe."}
        rows = self._lib.rows(int(t.id) for t in picks)
        uris = [rows[int(t.id)]["uri"] for t in picks]
        self._activity = {"phase": "enqueuing", "target": len(uris), "enqueued": 0}
        playing = str(queue.get("state") or "").lower() == "playing"
        option = "replace" if replace and not playing else ("replace_next" if replace else "add")
        await self._ma.cmd("player_queues/play_media", queue_id=player, media=uris, option=option,
                           timeout=120)
        if not replace and not playing and reason != "auto":
            await self._ma.try_cmd("player_queues/play", queue_id=player)
        self._note_picks(picks, targets)
        decision = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "reason": reason,
            "dj_note": self.plan.note,
            "vibe_reading": {"interpretation": "; ".join(self.plan.queries) or None,
                             "energy_target": round(self.plan.base_energy),
                             "mood": self.plan.arc.replace("_", " ")},
            "tracks": [
                {"query": f"{t.artist} - {t.title}" if t.artist else t.title,
                 "energy": round(t.energy) if t.energy is not None else None,
                 "reason": self._why(t, target)}
                for t, target in zip(picks, targets)
            ],
            "enqueued": len(uris),
            "sources": sources.as_dict(),
            "planner": self.plan.source,
        }
        self.decision_log.appendleft(decision)
        self.stats["added"] += len(uris)
        self._last_error = None
        return {"ok": True, "decision": decision}

    def _not_ready_message(self, counts: dict[str, int]) -> str:
        """Why the DJ cannot start yet, in plain words."""
        total = counts.get("total") or 0
        songs = f"None of your {total:,} songs are" if total else "No songs are"
        message = ("Your library is still being prepared: Music Assistant is analysing your songs "
                   f"so the DJ can read their tempo and energy. {songs} ready yet. The DJ can start "
                   "as soon as the first ones are done.")
        eta = self._coverage_view().get("eta_seconds")
        if eta:
            message += f" At the current pace the whole library takes {coarse_duration(eta)}."
        return message

    def _coverage_view(self) -> dict[str, Any]:
        """Music Assistant's own analysis progress, for /status (see coverage_progress)."""
        domains = {d: p for d in COVERAGE_DOMAINS if (p := self._lib.coverage_progress(d))}
        errors = {d: self._coverage_errors[d] for d in COVERAGE_DOMAINS if d in self._coverage_errors}
        live = {d: p for d, p in domains.items() if d not in errors}
        out: dict[str, Any] = {
            "available": bool(live), "reason": None, "message": None, "state": "unknown",
            "analysed": None, "pending": None, "rate_per_hour": None, "eta_seconds": None,
            "measured_hours": None, "domains": {d: {**p, "error": errors.get(d)} for d, p in domains.items()},
        }
        if not live:
            if errors:
                reason = next(iter(errors.values()))
                out.update(state="unavailable", reason=reason, message=COVERAGE_MESSAGES.get(reason))
            return out
        # the domain furthest behind decides when the library is done
        slowest = max(live.values(), key=lambda p: p["pending"])
        states = {p["state"] for p in live.values()}
        state = next(s for s in ("measuring", "stalled", "analysing", "complete") if s in states)
        etas = [p["eta_seconds"] for p in live.values() if p["eta_seconds"] is not None]
        out.update(state=state, analysed=slowest["analysed"], pending=slowest["pending"],
                   rate_per_hour=slowest["rate_per_hour"], measured_hours=slowest["measured_hours"],
                   eta_seconds=max(etas) if state in ("analysing", "complete") and etas else None)
        return out

    def _library_view(self) -> dict[str, Any]:
        """The mirror's counts next to Music Assistant's own (see Library.stats)."""
        return {**self._lib.stats(), "ma_index": self._ma_index, "ma_coverage": self._coverage_view(),
                "sync": self._sync_status}

    def _history(self, current_id: int | None, queued_ids: list[int]) -> list[Track]:
        """Recent session tracks + what is playing/queued, for transitions and artist spacing."""
        tail = list(self._played[-12:])
        known = {t.id for t in tail}
        extra = [i for i in ([current_id] if current_id is not None else []) + queued_ids
                 if str(i) not in known]
        tail.extend(self._lib.candidates({i: 0.5 for i in extra}))
        return tail

    @staticmethod
    def _why(track: Track, target: float) -> str:
        bits = []
        if track.bpm:
            bits.append(f"{round(track.bpm)} BPM")
        if track.key:
            bits.append(track.key)
        bits.append(f"aiming energy {target:.0f}")
        return " · ".join(bits)

    def _note_picks(self, picks: list[Track], targets: list[float]) -> None:
        for track in picks:
            self._played.append(track)
            self._picked_ids.add(int(track.id))
            self._arc_log.append({
                "track": f"{track.artist} - {track.title}" if track.artist else track.title,
                "id": track.id,
                "energy": round(track.energy, 1) if track.energy is not None else None,
                "bpm": round(track.bpm) if track.bpm else None,
                "camelot": track.key,
            })
            artist = track.artist_key
            if artist and artist not in self._artists_seen:
                self._artists_seen.add(artist)
                self.stats["new_artists"] += 1
        self._position += len(picks)
        del self._played[:-200]

    async def _top_up(self, reason: str) -> dict[str, Any]:
        queue = await self._queue()
        remaining = len(await self._upcoming(queue))
        if remaining >= LOW_WATER:
            return {"ok": True, "enqueued": 0}
        return await self._refill(reason, AHEAD_TARGET - remaining, replace=False)

    def _reset_session(self) -> None:
        self._position = 0
        self._played.clear()
        self._picked_ids.clear()
        self._arc_log.clear()
        self._artists_seen.clear()
        self.session_started = time.monotonic()
        self.stats = {"tracks_played": 0, "skips": 0, "added": 0, "new_artists": 0}

    # -- public actions -----------------------------------------------------

    async def start_radio(self) -> dict[str, Any]:
        if self._refill_lock.locked():
            return {"ok": False, "busy": True, "error": "A session is already starting."}
        self._seed_ids = []
        return await self._start("start")

    async def start_radio_from_seed(self, uri: str, label: str = "") -> dict[str, Any]:
        seed = self._lib.id_for_uri(uri)
        if seed is None:
            return {"ok": False, "error": "That track is not in the library mirror yet."}
        self._seed_ids = [seed]
        self.vibe_prompt = ""
        result = await self._start(f"radio from {label or 'track'}", first_uri=uri)
        return result

    async def _start(self, reason: str, first_uri: str | None = None) -> dict[str, Any]:
        self._reset_session()
        self._dj_stopped = False
        self.plan = await self._make_plan()
        if first_uri:
            player = await self._ensure_player()
            if player:
                await self._ma.cmd("player_queues/play_media", queue_id=player, media=first_uri,
                                   option="replace")
                result = await self._refill(reason, OPENING - 1, replace=False)
            else:
                result = {"ok": False, "error": "No available player found in Music Assistant."}
        else:
            result = await self._refill(reason, OPENING, replace=True)
            if result.get("ok") and self.player_id:
                await self._ma.try_cmd("player_queues/play", queue_id=self.player_id)
        if not result.get("ok"):
            self._dj_stopped = True
        return result

    async def set_vibe(self, prompt: str) -> dict[str, Any]:
        """Change direction: re-plan and replace what is queued after the current song."""
        self.vibe_prompt = prompt.strip()
        self._seed_ids = []
        self._energy_bias = 0
        if self.vibe_prompt:
            person = self._users.active
            person.record_mood(time_of_day_slot(), self.vibe_prompt)
            self._users.save(person)
        if self._dj_stopped:
            return await self._start("vibe")
        self.plan = await self._make_plan()
        self._position = 0
        return await self._refill("vibe", AHEAD_TARGET, replace=True)

    async def nudge(self) -> dict[str, Any]:
        """Re-plan the upcoming tracks without changing the vibe."""
        if self._dj_stopped:
            return await self.start_radio()
        return await self._refill("nudge", AHEAD_TARGET, replace=True)

    async def nudge_energy(self, direction: str) -> dict[str, Any]:
        """Raise or lower the set's energy. The current song keeps playing."""
        step = 1 if direction == "up" else -1
        self._energy_bias = max(-4, min(4, self._energy_bias + step))
        self.plan.base_energy = max(1.5, min(9.5, self.plan.base_energy + step))
        if self._dj_stopped:
            return {"ok": True, "energy_bias": self._energy_bias}
        result = await self._refill(f"energy {direction}", AHEAD_TARGET, replace=True)
        result["energy_bias"] = self._energy_bias
        return result

    async def tick(self, reason: str = "manual") -> dict[str, Any]:
        if self._dj_stopped:
            return await self.start_radio()
        return await self._top_up(reason)

    async def stop(self) -> dict[str, Any]:
        self._dj_stopped = True
        if not self.player_id:
            return {"ok": True}
        try:
            await self._ma.cmd("player_queues/stop", queue_id=self.player_id)
            await self._ma.cmd("player_queues/clear", queue_id=self.player_id)
        except (MAError, MAUnavailable) as err:
            return {"ok": False, "error": str(err)}
        return {"ok": True}

    async def toggle_playback(self) -> dict[str, Any]:
        if not self.player_id:
            return {"ok": False, "error": "No player selected."}
        queue = await self._queue()
        playing = str(queue.get("state") or "").lower() == "playing"
        command = "player_queues/pause" if playing else "player_queues/resume"
        try:
            await self._ma.cmd(command, queue_id=self.player_id)
        except (MAError, MAUnavailable) as err:
            return {"ok": False, "error": str(err)}
        return {"ok": True, "state": "paused" if playing else "playing"}

    async def skip(self) -> dict[str, Any]:
        if not self.player_id:
            return {"ok": False, "error": "No player selected."}
        queue = await self._queue()
        self._record_feedback((queue.get("current_item") or {}).get("media_item") or {}, skipped=True)
        try:
            await self._ma.cmd("player_queues/next", queue_id=self.player_id)
        except (MAError, MAUnavailable) as err:
            return {"ok": False, "error": str(err)}
        if not self._dj_stopped:
            asyncio.create_task(self._safe_top_up())
        return {"ok": True}

    async def _safe_top_up(self) -> None:
        with contextlib.suppress(Exception):
            await self._top_up("auto")

    async def previous_track(self) -> dict[str, Any]:
        if not self.player_id:
            return {"ok": False, "error": "No player selected."}
        try:
            await self._ma.cmd("player_queues/previous", queue_id=self.player_id)
        except (MAError, MAUnavailable) as err:
            return {"ok": False, "error": str(err)}
        return {"ok": True}

    async def set_volume(self, level: int) -> dict[str, Any]:
        if not self.player_id:
            return {"ok": False, "error": "No player selected."}
        try:
            await self._ma.cmd("players/cmd/volume_set", player_id=self.player_id,
                               volume_level=max(0, min(100, int(level))))
        except (MAError, MAUnavailable) as err:
            return {"ok": False, "error": str(err)}
        return {"ok": True}

    # -- queue editing (dashboard) ------------------------------------------

    async def queue_view(self) -> dict[str, Any]:
        queue = await self._queue()
        index = queue.get("current_index")
        start = int(index) if index is not None else 0
        items = []
        if self.player_id and queue.get("items"):
            items = await self._ma.cmd("player_queues/items", queue_id=self.player_id, limit=200,
                                       offset=start) or []
        # the dashboard expects the current item first, then what is upcoming
        return {"queue_id": self.player_id, "items_remaining": max(len(items) - 1, 0),
                "current_index": 0, "items": [track_view(it) for it in items]}

    async def search(self, text: str) -> list[dict[str, Any]]:
        return [
            {"name": r["name"], "artist": r["artist"], "uri": r["uri"], "image": None,
             "duration": r["duration"], "item_id": None, "analysed": bool(r["analysed"])}
            for r in self._lib.search(text)
        ]

    async def enqueue_uri(self, uri: str, option: str = "next") -> bool:
        player = await self._ensure_player()
        if not player:
            return False
        await self._ma.cmd("player_queues/play_media", queue_id=player, media=uri, option=option)
        return True

    async def remove_queue_item(self, item_id: str) -> bool:
        if not self.player_id:
            return False
        await self._ma.cmd("player_queues/delete_item", queue_id=self.player_id, item_id_or_index=item_id)
        return True

    async def move_queue_item(self, item_id: str, to_index: int) -> bool:
        """Move an upcoming item to ``to_index`` (1 = next up) in the dashboard's list."""
        if not self.player_id:
            return False
        upcoming = await self._upcoming(await self._queue())
        ids = [it.get("queue_item_id") for it in upcoming]
        if item_id not in ids:
            return False
        shift = (to_index - 1) - ids.index(item_id)
        if shift:
            await self._ma.cmd("player_queues/move_item", queue_id=self.player_id,
                               queue_item_id=item_id, pos_shift=shift)
        return True

    # -- likes, blocks, people ----------------------------------------------

    async def _current(self) -> tuple[str | None, str | None, dict[str, Any]]:
        queue = await self._queue()
        item = queue.get("current_item") or {}
        media = item.get("media_item") or {}
        return media.get("uri"), _label(item), media

    async def like_current(self) -> dict[str, Any]:
        """Toggle the heart on the current track, here and in Music Assistant."""
        uri, label, media = await self._current()
        if not uri:
            return {"ok": False, "error": "Nothing is playing."}
        person = self._users.active
        if person.is_liked(uri):
            person.remove_like(uri)
            self._users.save(person)
            if media.get("provider") == "library" and media.get("item_id"):
                await self._ma.try_cmd("music/favorites/remove_item", media_type="track",
                                       library_item_id=str(media["item_id"]))
            return {"ok": True, "liked": False, "track": label}
        person.add_like(label, uri)
        self._users.save(person)
        await self._ma.try_cmd("music/favorites/add_item", item=uri)
        return {"ok": True, "liked": True, "track": label}

    async def block_current(self) -> dict[str, Any]:
        uri, label, media = await self._current()
        if not uri:
            return {"ok": False, "error": "Nothing is playing."}
        person = self._users.active
        person.add_block(label, uri)
        self._users.save(person)
        self._record_feedback(media, skipped=True)
        await self._ma.try_cmd("player_queues/next", queue_id=self.player_id)
        return {"ok": True, "blocked": label}

    def list_users(self) -> list[dict[str, Any]]:
        return self._users.people()

    def user_taste_profiles(self) -> list[dict[str, Any]]:
        out = []
        for entry in self._users.people():
            person = self._users.get(str(entry["slug"]))
            if not person:
                continue
            likes = ", ".join(person.recent_likes(8)) or "no likes yet"
            summary = person.summary or f"Recent likes: {likes}. {len(person.active_blocks())} track(s) on cooldown."
            out.append({"name": person.name, "active": bool(entry.get("active")), "summary": summary})
        return out

    async def select_user(self, slug: str) -> dict[str, Any]:
        if not self._users.select(slug):
            return {"ok": False, "error": "Unknown person."}
        if not self._dj_stopped:
            asyncio.create_task(self._refill("listener changed", AHEAD_TARGET, replace=True))
        return {"ok": True}

    def add_user(self, name: str) -> dict[str, Any]:
        if not name.strip():
            return {"ok": False, "error": "Name is required."}
        person = self._users.add_person(name.strip())
        return {"ok": True, "slug": person.slug}

    def rename_user(self, slug: str, name: str) -> dict[str, Any]:
        return {"ok": bool(name.strip()) and self._users.rename(slug, name.strip())}

    def delete_user(self, slug: str) -> dict[str, Any]:
        ok = self._users.remove(slug)
        return {"ok": ok} if ok else {"ok": False, "error": "Can't remove that person."}

    async def save_session_playlist(self, name: str) -> dict[str, Any]:
        """Save this session's tracks as a Music Assistant playlist."""
        ids = [int(t.id) for t in self._played]
        if not ids:
            return {"ok": False, "error": "No tracks in this session yet."}
        rows = self._lib.rows(ids)
        uris = [rows[i]["uri"] for i in ids if i in rows]
        title = name.strip() or f"TideSync {datetime.now():%Y-%m-%d %H:%M}"
        try:
            playlist = await self._ma.cmd("music/playlists/create_playlist", name=title)
            await self._ma.cmd("music/playlists/add_playlist_tracks",
                               db_playlist_id=str(playlist["item_id"]), uris=uris, timeout=120)
        except (MAError, MAUnavailable) as err:
            return {"ok": False, "error": str(err)}
        return {"ok": True, "playlist": title, "track_count": len(uris)}

    # -- feedback from playback ---------------------------------------------

    def _record_feedback(self, media: dict[str, Any], *, skipped: bool) -> None:
        uri = media.get("uri")
        tid = self._lib.id_for_uri(uri)
        if tid is None:
            return
        last_uri, at = self._last_feedback
        if last_uri == uri and time.monotonic() - at < 30:
            return  # the skip button and the playback report describe the same event
        self._last_feedback = (uri, time.monotonic())
        self._lib.record_play(tid, skipped=skipped)
        self.stats["skips" if skipped else "tracks_played"] += 1

    async def _on_event(self, event: str, msg: dict[str, Any]) -> None:
        if event != "media_item_played" or self._dj_stopped:
            return
        data = msg.get("data") or {}
        if data.get("media_type") not in (None, "track") or data.get("is_playing"):
            return
        player = data.get("player_id")
        if player and self.player_id and player != self.player_id:
            return
        if data.get("fully_played"):
            self._record_feedback(data, skipped=False)
        else:
            duration = float(data.get("duration") or 0)
            played = float(data.get("seconds_played") or 0)
            if duration and 3 <= played < duration * SKIP_FRACTION:
                asyncio.create_task(self._confirm_skip(data))
        asyncio.create_task(self._safe_top_up())

    async def _confirm_skip(self, data: dict[str, Any]) -> None:
        """A track that stopped early is a skip only if playback moved on to another track."""
        await asyncio.sleep(4)
        with contextlib.suppress(Exception):
            queue = await self._queue()
            now_uri = ((queue.get("current_item") or {}).get("media_item") or {}).get("uri")
            if now_uri and now_uri != data.get("uri") and str(queue.get("state")).lower() == "playing":
                self._record_feedback(data, skipped=True)

    # -- status -------------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        queue: dict[str, Any] = {}
        player: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            queue = await self._queue()
            if self.player_id:
                player = await self._ma.cmd("players/get", player_id=self.player_id) or {}
        item = queue.get("current_item")
        uri = ((item or {}).get("media_item") or {}).get("uri")
        upcoming = max(int(queue.get("items") or 0) - int(queue.get("current_index") or 0) - 1, 0)
        return {
            "configured": True,
            "claude_enabled": self._planner.enabled,
            "now_playing": _label(item),
            "album_art": _image(item),
            "player_state": (queue.get("state") or player.get("playback_state") or None),
            "volume": player.get("volume_level"),
            "dj_stopped": self._dj_stopped,
            "vibe": self.vibe_prompt or None,
            "current_liked": self._users.active.is_liked(uri),
            "energy_bias": self._energy_bias,
            "set_phase": self.plan.arc,
            "time_of_day": time_of_day_slot().replace("_", " "),
            "items_remaining": upcoming if queue else 0,
            "ma_connected": self._ma.connected,
            "ma_error": self._last_error,
            "ma_host": self._config.ma_host,
            "selected_player_id": self.player_id,
            "taste_seeded": True,
            "session_track_count": len(self._played),
            "can_like": bool(uri),
            "can_block": bool(uri),
            "people": self._users.people(),
            "active_person": self._users.active.name,
            "session_minutes": round((time.monotonic() - self.session_started) / 60),
            "stats": self.stats,
            "model": self._planner.model if self._planner.enabled else "no AI key — rules only",
            "library": self._library_view(),
        }

    async def dj_status(self) -> dict[str, Any]:
        queue: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            queue = await self._queue()
        item = queue.get("current_item")
        current_id = self._lib.id_for_uri(((item or {}).get("media_item") or {}).get("uri"))
        arc = [{**entry, "now_playing": entry["id"] == str(current_id)} for entry in self._arc_log]
        position = self._position % SET_LENGTH if self.plan.arc in ("flat", "wave") else self._position
        library = self._library_view()
        plan_note = f"{self.plan.note} {self._pool_note(library)}".strip()
        return {
            "activity": dict(self._activity),
            "set_plan": {"arc_note": plan_note, "phases": []},
            "arc_position": None if self._dj_stopped else {
                "elapsed_minutes": round((time.monotonic() - self.session_started) / 60),
                "pct_through": min(100, round(100 * position / SET_LENGTH)),
                "current_phase": self.plan.arc,
            },
            "energy_arc": arc,
            "latest_decision": self.decision_log[0] if self.decision_log else None,
            "now_playing": _label(item),
            "items_remaining": max(int(queue.get("items") or 0) - int(queue.get("current_index") or 0) - 1, 0),
            "dj_stopped": self._dj_stopped,
            "vibe": self.vibe_prompt or None,
            "energy_bias": self._energy_bias,
            "plan": self.plan.as_dict(),
            "library": library,
        }

    @staticmethod
    def _pool_note(lib: dict[str, Any]) -> str:
        """How much of the library the DJ can pick from, and how much a vibe can match by sound."""
        total, ready, by_sound = lib["total"], lib["analysed"], lib["in_sound_index"]
        if not total:
            return ""
        if not ready:
            return f"None of your {total:,} songs are ready yet: Music Assistant is still analysing them."
        note = f"Choosing from {ready:,} ready songs of {total:,}."
        if by_sound < ready:
            note += (f" A vibe can be matched by sound against {by_sound:,} of them so far; "
                     "the others are chosen by tempo and energy.")
        if ready < total:
            note += " The rest of your library is still being prepared."
        return note
