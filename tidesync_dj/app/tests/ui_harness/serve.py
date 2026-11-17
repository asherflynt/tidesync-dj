"""Serve the real dashboard and API against an in-memory Music Assistant, for UI checks.

    cd tidesync_dj/app
    python tests/ui_harness/serve.py --port 8765

Nothing here touches the network: Music Assistant is the fake from
tests/test_engine.py with a few more commands, the library mirror and people
live in a throwaway directory, and Claude is off. ``POST /__harness/scenario``
with ``{"library": NAME}`` switches the library readiness shown in /status:

    eta          1,606 of 272,826 ready, Music Assistant's ETA known
    measuring    same counts, under a day of samples (no ETA yet)
    catching_up  as eta, while the DJ is still adding newly finished songs
    unavailable  the token is not an admin token (no MA progress at all)
    complete     everything analysed (the readiness card should not show)
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

APP = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(APP), str(APP / "tests")]

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import Request  # noqa: E402

import main  # noqa: E402
from engine import DJEngine  # noqa: E402
from library import COVERAGE_DOMAINS, Library  # noqa: E402
from plan import ClaudePlanner  # noqa: E402
from test_engine import FakeMA  # noqa: E402
from user_memory import UserStore  # noqa: E402

WORDS = ["Night", "Harbour", "Paper", "Golden", "Slow", "Neon", "Quiet", "Summer", "River", "Velvet",
         "Echo", "Northern", "Glass", "Morning", "Silver", "Wild"]
NOUNS = ["Drive", "Lights", "Hearts", "Rain", "Garden", "Signals", "Waves", "Avenue", "Fever", "Skies"]
ARTISTS = ["The Harbours", "Mina Vale", "Low Tide Club", "Orla Finch", "Kite Season", "Juniper Row",
           "Delta Moth", "The Quiet Hours", "Sol & Wren", "Paper Planes", "Marlow", "Ivy Lane",
           "North Arcade", "Glasshouse", "Ronan Ash"]

READY, TOTAL = 1_606, 272_826


class HarnessMA(FakeMA):
    """FakeMA with named players and the queue commands the dashboard uses."""

    def __init__(self) -> None:
        super().__init__(n=120)
        for i, track in enumerate(self.tracks):
            track["name"] = f"{WORDS[i % len(WORDS)]} {NOUNS[(i * 7) % len(NOUNS)]}"
            track["artists"] = [{"name": ARTISTS[i % len(ARTISTS)]}]
        self.volume = 35

    async def cmd(self, command, timeout=60, **args):
        args = {k: v for k, v in args.items() if v is not None}
        if command == "players/all":
            return [{"player_id": "p1", "display_name": "Kitchen", "available": True, "enabled": True},
                    {"player_id": "p2", "display_name": "Living Room", "available": True, "enabled": True}]
        if command == "players/get":
            return {"volume_level": self.volume, "playback_state": self.state}
        if command == "players/cmd/volume_set":
            self.volume = int(args["volume_level"])
            return None
        if command == "player_queues/play_media" and args.get("option") == "next":
            by_uri = {t["uri"]: t for t in self.tracks}
            media = args["media"] if isinstance(args["media"], list) else [args["media"]]
            items = [{"queue_item_id": f"n{len(self.calls)}-{k}", "media_item": by_uri[u]}
                     for k, u in enumerate(media)]
            self.calls.append((command, args))
            at = (self.index or 0) + 1
            self.queue[at:at] = items
            return None
        if command == "player_queues/previous":
            self.index = max((self.index or 0) - 1, 0)
            return None
        if command == "player_queues/delete_item":
            self.queue = [it for it in self.queue if it["queue_item_id"] != args["item_id_or_index"]]
            return None
        if command == "player_queues/move_item":
            ids = [it["queue_item_id"] for it in self.queue]
            at = ids.index(args["queue_item_id"])
            item = self.queue.pop(at)
            self.queue.insert(at + int(args["pos_shift"]), item)
            return None
        return await super().cmd(command, timeout=timeout, **args)


def set_library(engine: DJEngine, lib: Library, name: str) -> None:
    """Make /status report one readiness scenario (real coverage code, faked counts)."""
    now = int(time.time())
    for domain in COVERAGE_DOMAINS:
        lib.set_meta(f"coverage:{domain}", "[]")
    engine._coverage_errors.clear()
    engine._sync_status = {"state": "idle", "features": {"more": False, "caught_up": True}}
    ready, total = (TOTAL, TOTAL) if name == "complete" else (READY, TOTAL)
    if name == "unavailable":
        engine._coverage_errors.update({d: "needs_admin" for d in COVERAGE_DOMAINS})
    else:
        hours = 2 if name == "measuring" else 30
        for domain in COVERAGE_DOMAINS:
            for h in range(hours, -1, -1):
                done = total if name == "complete" else ready - 26 * h
                lib.record_coverage(domain, done, total - done, now=now - h * 3600)
    if name == "catching_up":
        engine._sync_status["features"]["more"] = True
    real_stats = Library.stats.__get__(lib)
    lib.stats = lambda: {**real_stats(), "total": total, "analysed": ready, "measured": ready}


def build(data: Path):
    ma = HarnessMA()
    lib = Library(data / "library.db")
    users = UserStore(data)
    users.add_person("Listener Profile")
    config = SimpleNamespace(data_dir=data, ma_host="127.0.0.1", ma_port=9, dj_tick_interval=30,
                             vibe_input_text_entity=None, ha_action_entity="", keep_analysis_running=False)
    engine = DJEngine(config, ma, ha=None, planner=ClaudePlanner("", "claude-opus-5-5"), users=users,
                      library=lib)
    return ma, lib, engine, config


@asynccontextmanager
async def lifespan(app):
    data = Path(tempfile.mkdtemp(prefix="tidesync-ui-harness-"))
    ma, lib, engine, config = build(data)
    await lib.sync_all(ma)
    ma.subscribe(engine._on_event)
    set_library(engine, lib, app.state.scenario)
    app.state.engine, app.state.config, app.state.lib = engine, config, lib
    app.state.http = httpx.AsyncClient(timeout=1.0)
    yield
    await app.state.http.aclose()
    lib.close()
    shutil.rmtree(data, ignore_errors=True)


@main.app.post("/__harness/scenario")
async def scenario(request: Request):
    body = await request.json()
    set_library(request.app.state.engine, request.app.state.lib, body.get("library", "eta"))
    return {"ok": True}


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--library", default="eta", help="starting readiness scenario")
    args = parser.parse_args()
    main.app.state.scenario = args.library
    main.app.router.lifespan_context = lifespan
    uvicorn.run(main.app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    run()
