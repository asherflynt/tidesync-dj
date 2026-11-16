"""Engine behaviour against a fake Music Assistant: picking, refills, feedback, likes."""
from types import SimpleNamespace

import pytest

from engine import AHEAD_TARGET, OPENING, DJEngine, track_view
from library import Library
from ma import MAError
from plan import ClaudePlanner, heuristic_plan
from user_memory import UserStore

KEYS = ["A minor", "E minor", "C major", "G major", "D minor", "F major"]


def _track(i):
    return {
        "item_id": str(i), "uri": f"library://track/{i}", "name": f"Song {i}",
        "artists": [{"name": f"Artist {i % 15}"}], "album": {"name": "Album"}, "duration": 200,
        "provider_mappings": [{"provider_instance": "fs--1", "item_id": f"f{i}"}],
        "audio_metadata": {"bpm": 90 + (i * 7) % 60, "musical_key": KEYS[i % len(KEYS)]},
    }


class FakeMA:
    """Just enough of Music Assistant: a library, a sonic index and one queue."""

    connected = True

    def __init__(self, n=80, text_search=True):
        self.tracks = [_track(i) for i in range(1, n + 1)]
        self.text_search = text_search
        self.queue: list[dict] = []
        self.index = None
        self.state = "idle"
        self.calls: list[tuple[str, dict]] = []
        self.listeners = []

    def subscribe(self, listener):
        self.listeners.append(listener)
        return lambda: None

    async def close(self):
        pass

    async def try_cmd(self, command, default=None, **args):
        try:
            return await self.cmd(command, **args)
        except MAError:
            return default

    async def cmd(self, command, timeout=60, **args):
        args = {k: v for k, v in args.items() if v is not None}
        self.calls.append((command, args))
        if command == "music/tracks/library_items":
            return self.tracks[args.get("offset", 0): args.get("offset", 0) + args["limit"]]
        if command == "sonic_similarity/text_search":
            if not self.text_search:
                raise MAError(command, 999, "Invalid or unsupported command.")
            n = len(self.tracks)
            flip = "calm" in args["query"]
            items = [{"provider": "fs--1", "item_id": f"f{i}",
                      "distance": ((n - i) if flip else i) / n} for i in range(1, n + 1)]
            return {"analyzed": True, "items": sorted(items, key=lambda x: x["distance"])[: args["limit"]]}
        if command == "sonic_similarity/similar":
            seed = int(args["item_ids"][0][1:])
            near = [i for i in range(1, len(self.tracks) + 1) if i != seed]
            near.sort(key=lambda i: abs(i - seed))
            return {"analyzed": True, "items": [{"provider": "fs--1", "item_id": f"f{i}", "distance": 0.1}
                                                for i in near[: args["limit"]]]}
        if command == "music/tracks/get":
            return None
        if command == "players/all":
            return [{"player_id": "p1", "display_name": "Den", "available": True, "enabled": True},
                    {"player_id": "p2", "display_name": "Off", "available": False, "enabled": True}]
        if command == "players/get":
            return {"volume_level": 30, "playback_state": self.state}
        if command == "player_queues/get":
            current = self.queue[self.index] if self.index is not None and self.queue else None
            return {"queue_id": "p1", "items": len(self.queue), "current_index": self.index,
                    "state": self.state, "current_item": current}
        if command == "player_queues/items":
            return self.queue[args.get("offset", 0): args.get("offset", 0) + args.get("limit", 500)]
        if command == "player_queues/play_media":
            media = args["media"] if isinstance(args["media"], list) else [args["media"]]
            by_uri = {t["uri"]: t for t in self.tracks}
            items = [{"queue_item_id": f"q{len(self.calls)}-{n}", "media_item": by_uri[u]}
                     for n, u in enumerate(media)]
            if args["option"] == "replace":
                self.queue, self.index, self.state = items, 0, "playing"
            elif args["option"] == "replace_next":
                self.queue = self.queue[: (self.index or 0) + 1] + items
            else:
                self.queue += items
            return None
        if command == "player_queues/next":
            self.index = min((self.index or 0) + 1, len(self.queue) - 1)
            return None
        if command in ("player_queues/play", "player_queues/resume"):
            self.state = "playing"
            self.index = self.index or 0
            return None
        if command == "player_queues/pause":
            self.state = "paused"
            return None
        if command in ("player_queues/stop", "player_queues/clear"):
            self.queue, self.index, self.state = [], None, "idle"
            return None
        if command == "music/playlists/create_playlist":
            return {"item_id": 7, "name": args["name"]}
        return None

    def upcoming_uris(self):
        return [it["media_item"]["uri"] for it in self.queue[(self.index or 0) + 1:]]


@pytest.fixture
async def rig(tmp_path):
    ma = FakeMA()
    lib = Library(tmp_path / "lib.db")
    await lib.sync_all(ma)
    config = SimpleNamespace(data_dir=tmp_path, ma_host="ma.local", ma_port=8095, dj_tick_interval=30,
                             vibe_input_text_entity=None, ha_action_entity="")
    engine = DJEngine(config, ma, ha=None, planner=ClaudePlanner("", "x"), users=UserStore(tmp_path),
                      library=lib)
    yield engine, ma, lib
    lib.close()


async def test_start_radio_queues_real_library_tracks_and_plays(rig):
    engine, ma, lib = rig
    result = await engine.start_radio()
    assert result["ok"], result
    assert len(ma.queue) == OPENING and ma.state == "playing"
    uris = [it["media_item"]["uri"] for it in ma.queue]
    assert len(set(uris)) == OPENING and all(lib.id_for_uri(u) for u in uris)
    # one call for the whole block, not one per track
    assert sum(1 for c, _ in ma.calls if c == "player_queues/play_media") == 1
    assert engine.player_id == "p1"  # the unavailable player was not chosen


async def test_top_up_only_when_low_and_never_repeats(rig):
    engine, ma, _ = rig
    await engine.start_radio()
    assert (await engine.tick())["enqueued"] == 0  # plenty queued
    ma.index = len(ma.queue) - 2  # one track left
    before = {it["media_item"]["uri"] for it in ma.queue}
    result = await engine.tick()
    assert result["ok"] and len(ma.upcoming_uris()) == AHEAD_TARGET
    assert not before & set(ma.upcoming_uris()[1:])


async def test_set_vibe_replaces_upcoming_but_keeps_current_song(rig):
    engine, ma, _ = rig
    await engine.start_radio()
    playing = ma.queue[0]["media_item"]["uri"]
    result = await engine.set_vibe("calm evening")
    assert result["ok"] and engine.plan.base_energy < 5
    assert ma.queue[0]["media_item"]["uri"] == playing and ma.state == "playing"
    assert len(ma.upcoming_uris()) == AHEAD_TARGET
    assert engine._users.active.moods  # remembered for this listener


async def test_energy_nudge_shifts_targets_without_cutting_playback(rig):
    engine, ma, _ = rig
    await engine.start_radio()
    base = engine.plan.base_energy
    playing = ma.queue[0]["media_item"]["uri"]
    result = await engine.nudge_energy("up")
    assert result["ok"] and result["energy_bias"] == 1 and engine.plan.base_energy == base + 1
    assert ma.queue[0]["media_item"]["uri"] == playing
    assert not any(c == "player_queues/next" for c, _ in ma.calls)


async def test_seed_radio_starts_with_seed_and_stays_near_it(rig):
    engine, ma, lib = rig
    result = await engine.start_radio_from_seed("library://track/40", "Artist - Song 40")
    assert result["ok"], result
    assert ma.queue[0]["media_item"]["uri"] == "library://track/40"
    picked = [lib.id_for_uri(u) for u in ma.upcoming_uris()]
    assert len(picked) == OPENING - 1 and 40 not in picked
    assert sum(abs(i - 40) for i in picked) / len(picked) < 25  # neighbours, not random


async def test_blocked_tracks_are_never_picked(rig):
    engine, ma, _ = rig
    person = engine._users.active
    for i in range(1, 41):
        person.add_block(f"Song {i}", f"library://track/{i}")
    await engine.start_radio()
    ids = {int(it["media_item"]["item_id"]) for it in ma.queue}
    assert ids and min(ids) > 40


async def test_skip_records_feedback_once_and_lowers_ranking(rig):
    engine, ma, lib = rig
    await engine.start_radio()
    skipped = int(ma.queue[0]["media_item"]["item_id"])
    assert (await engine.skip())["ok"]
    assert engine.stats["skips"] == 1
    # the playback report for the same track must not count a second skip
    await engine._on_event("media_item_played", {"data": {
        "uri": f"library://track/{skipped}", "is_playing": False, "fully_played": False,
        "seconds_played": 10, "duration": 200}})
    engine._record_feedback({"uri": f"library://track/{skipped}"}, skipped=True)
    assert engine.stats["skips"] == 1
    assert lib.skip_penalties([skipped])[skipped] > 0


async def test_full_play_event_counts_as_played(rig):
    engine, ma, lib = rig
    await engine.start_radio()
    await engine._on_event("media_item_played", {"data": {
        "uri": "library://track/5", "media_type": "track", "is_playing": False, "fully_played": True,
        "seconds_played": 200, "duration": 200}})
    assert engine.stats["tracks_played"] == 1
    assert lib.rows([5])[5]["last_played"] > 0


async def test_like_toggles_and_syncs_to_music_assistant(rig):
    engine, ma, _ = rig
    await engine.start_radio()
    current = ma.queue[0]["media_item"]
    current["provider"] = "library"
    assert (await engine.like_current())["liked"] is True
    assert ("music/favorites/add_item", {"item": current["uri"]}) in ma.calls
    assert (await engine.like_current())["liked"] is False
    assert ("music/favorites/remove_item",
            {"media_type": "track", "library_item_id": current["item_id"]}) in ma.calls


async def test_works_without_sound_search(tmp_path):
    ma = FakeMA()
    lib = Library(tmp_path / "lib.db")
    await lib.sync_all(ma)
    ma.text_search = False
    config = SimpleNamespace(data_dir=tmp_path, ma_host="x", ma_port=1, dj_tick_interval=30,
                             vibe_input_text_entity=None, ha_action_entity="")
    engine = DJEngine(config, ma, None, ClaudePlanner("", "x"), UserStore(tmp_path), lib)
    result = await engine.set_vibe("anything")
    assert result["ok"] and len(ma.queue) == OPENING
    assert any("unavailable" in n for n in result["decision"]["sources"]["notes"])
    lib.close()


async def test_clear_error_when_nothing_is_analysed(tmp_path):
    ma = FakeMA(text_search=False)
    lib = Library(tmp_path / "lib.db")
    await lib.sync_listing(ma)
    lib.db.execute("UPDATE tracks SET analysed=0")
    config = SimpleNamespace(data_dir=tmp_path, ma_host="x", ma_port=1, dj_tick_interval=30,
                             vibe_input_text_entity=None, ha_action_entity="")
    engine = DJEngine(config, ma, None, ClaudePlanner("", "x"), UserStore(tmp_path), lib)
    result = await engine.start_radio()
    assert not result["ok"] and "analys" in result["error"]
    assert engine._dj_stopped and not ma.queue
    lib.close()


async def test_status_and_queue_views_have_dashboard_fields(rig):
    engine, ma, _ = rig
    await engine.start_radio()
    status = await engine.status()
    for key in ("now_playing", "player_state", "items_remaining", "people", "stats", "library", "model"):
        assert key in status
    assert status["items_remaining"] == OPENING - 1 and status["library"]["analysed"] == 80
    view = await engine.queue_view()
    assert len(view["items"]) == OPENING and view["current_index"] == 0
    dj = await engine.dj_status()
    assert dj["latest_decision"]["enqueued"] == OPENING and len(dj["energy_arc"]) == OPENING
    assert dj["energy_arc"][0]["now_playing"] is True


async def test_save_playlist_uses_session_tracks(rig):
    engine, ma, _ = rig
    await engine.start_radio()
    result = await engine.save_session_playlist("My set")
    assert result == {"ok": True, "playlist": "My set", "track_count": OPENING}
    add = next(a for c, a in ma.calls if c == "music/playlists/add_playlist_tracks")
    assert add["db_playlist_id"] == "7" and len(add["uris"]) == OPENING


def test_track_view_builds_proxied_art_url():
    item = {"queue_item_id": "q1", "image": {"proxy_id": "abc123"},
            "media_item": {"name": "N", "uri": "library://track/1", "artists": [{"name": "A"}]}}
    assert track_view(item) == {"name": "N", "artist": "A", "uri": "library://track/1",
                                "image": "art/abc123", "duration": None, "item_id": "q1"}


def test_heuristic_plan_reads_energy_words():
    assert heuristic_plan("chill sunday morning").base_energy < 4
    assert heuristic_plan("workout").base_energy > 7
    assert heuristic_plan("pregame warm up").arc == "build"
    assert heuristic_plan("", slot="late_night").queries == []


async def test_keep_scan_running_restarts_only_a_stopped_scan_with_work_left(rig):
    engine, ma, _ = rig
    state = {"status": "success", "pending": 50}
    real = ma.cmd

    async def cmd(command, timeout=60, **args):
        if command == "audio_analysis/coverage":
            ma.calls.append((command, args))
            return {"analyzed": 5, "pending": state["pending"]}
        if command == "tasks/get":
            return {"status": state["status"]}
        return await real(command, timeout=timeout, **args)

    ma.cmd = cmd
    runs = lambda: sum(1 for c, _ in ma.calls if c == "tasks/run")  # noqa: E731
    await engine._keep_scan_running()
    assert runs() == 0  # option is off by default
    engine._config.keep_analysis_running = True
    await engine._keep_scan_running()
    assert runs() == 1
    state["status"] = "running"
    await engine._keep_scan_running()
    state.update(status="success", pending=0)
    await engine._keep_scan_running()
    assert runs() == 1
