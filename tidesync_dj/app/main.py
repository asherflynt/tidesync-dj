"""TideSync DJ — FastAPI application.

Exposes the ingress dashboard and the control/observability API. The DJ engine
runs as a background task started in the lifespan handler.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from claude_brain import ClaudeBrain
from config import load_config
from ha_client import HAClient
from ma_client import MusicAssistantClient
from scheduler import DJEngine
from taste_profile import TasteProfile
from user_memory import UserStore

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# Keep the app's own modules at DEBUG, but silence third-party libraries whose
# DEBUG output is noisy AND sensitive: the anthropic SDK + httpx/httpcore log
# full request payloads (the listener context we send Claude) and can surface
# request headers — i.e. the API key. Quieting them to WARNING keeps that data
# out of the add-on log, matching the "never logged" promise in the README.
for _noisy in ("websockets", "anthropic", "httpx", "httpcore", "urllib3", "ytmusicapi"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
_LOGGER = logging.getLogger("tidesync")

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    if not config.anthropic_api_key:
        _LOGGER.warning("No anthropic_api_key configured — DJ decisions will fail.")

    ma = MusicAssistantClient(
        config.ma_ws_url,
        username=config.ma_username,
        password=config.ma_password,
        token=config.ma_token,
    )
    ha = HAClient()
    brain = ClaudeBrain(config.anthropic_api_key, config.claude_model)
    taste = TasteProfile(config.data_dir)
    users = UserStore(config.data_dir)
    engine = DJEngine(config, ma, ha, brain, taste, users)

    app.state.engine = engine
    app.state.ma = ma
    app.state.config = config

    await engine.start()
    _LOGGER.info("TideSync DJ started (model=%s)", config.claude_model)
    try:
        yield
    finally:
        await engine.shutdown()
        _LOGGER.info("TideSync DJ stopped")


app = FastAPI(title="TideSync DJ", lifespan=lifespan)


@app.exception_handler(Exception)
async def _json_errors(request: Request, exc: Exception):
    """Always return JSON, never Starlette's plain-text 500.

    The UI parses every response as JSON; a plain-text 500 would surface to the
    user as a useless generic failure (this is what hid the real "like" error).
    """
    _LOGGER.exception("Unhandled error on %s", request.url.path)
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


class VibeBody(BaseModel):
    prompt: str


class EnergyBody(BaseModel):
    direction: str  # "up" | "down"


class VolumeBody(BaseModel):
    level: int  # 0-100


class PlayerBody(BaseModel):
    player_id: str


class UserSelectBody(BaseModel):
    slug: str


class UserAddBody(BaseModel):
    name: str


class UserRenameBody(BaseModel):
    slug: str
    name: str


class SeedBody(BaseModel):
    playlist: str


class SeedRadioBody(BaseModel):
    uri: str
    label: str = ""


class SavePlaylistBody(BaseModel):
    name: str = ""


class PlayNextBody(BaseModel):
    uri: str
    option: str = "next"


class QueueRemoveBody(BaseModel):
    item_id: str


class QueueMoveBody(BaseModel):
    item_id: str
    to_index: int


def _engine(request: Request) -> DJEngine:
    return request.app.state.engine


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return TEMPLATES.TemplateResponse("index.html", {"request": request})


@app.get("/status")
async def status(request: Request):
    return await _engine(request).status()


@app.post("/vibe")
async def set_vibe(request: Request, body: VibeBody):
    result = await _engine(request).set_vibe(body.prompt)
    return JSONResponse(result, status_code=200 if result.get("ok") else 502)


@app.get("/queue")
async def queue(request: Request):
    ma: MusicAssistantClient = request.app.state.ma
    q = await ma.get_queue()
    return {
        "queue_id": q.get("queue_id"),
        "items_remaining": q.get("items_remaining", 0),
        "current_index": q.get("current_index", 0),
        "items": [ma._track_view(it) for it in q.get("items", [])],
    }


@app.get("/search")
async def search(request: Request, q: str = ""):
    ma: MusicAssistantClient = request.app.state.ma
    query = q.strip()
    if not query:
        return {"tracks": []}
    return {"tracks": await ma.search_tracks(query)}


@app.post("/queue/play_next")
async def queue_play_next(request: Request, body: PlayNextBody):
    ma: MusicAssistantClient = request.app.state.ma
    # Only the two options the UI offers; anything else falls back to "next".
    option = body.option if body.option in ("next", "add") else "next"
    ok = await ma.enqueue_uri(body.uri, option)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 502)


@app.post("/queue/remove")
async def queue_remove(request: Request, body: QueueRemoveBody):
    ma: MusicAssistantClient = request.app.state.ma
    ok = await ma.remove_queue_item(body.item_id)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 502)


@app.post("/queue/move")
async def queue_move(request: Request, body: QueueMoveBody):
    ma: MusicAssistantClient = request.app.state.ma
    ok = await ma.move_queue_item(body.item_id, body.to_index)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 502)


@app.post("/tick")
async def tick(request: Request):
    result = await _engine(request).tick(reason="manual")
    status_code = 200 if result.get("ok") else 502
    return JSONResponse(result, status_code=status_code)


@app.get("/players")
async def players(request: Request):
    return {"players": await _engine(request).list_players()}


@app.post("/players/select")
async def select_player(request: Request, body: PlayerBody):
    result = await _engine(request).select_player(body.player_id)
    return JSONResponse(result, status_code=200 if result.get("ok") else 502)


@app.post("/volume")
async def volume(request: Request, body: VolumeBody):
    result = await _engine(request).set_volume(body.level)
    return JSONResponse(result, status_code=200 if result.get("ok") else 502)


@app.post("/previous")
async def previous(request: Request):
    result = await _engine(request).previous_track()
    return JSONResponse(result, status_code=200 if result.get("ok") else 502)


@app.get("/users/profiles")
async def user_profiles(request: Request):
    return {"people": _engine(request).user_taste_profiles()}


@app.post("/start_radio")
async def start_radio(request: Request):
    result = await _engine(request).start_radio()
    return JSONResponse(result, status_code=200 if result.get("ok") else 502)


@app.post("/start_radio_seed")
async def start_radio_seed(request: Request, body: SeedRadioBody):
    result = await _engine(request).start_radio_from_seed(body.uri, body.label)
    return JSONResponse(result, status_code=200 if result.get("ok") else 502)


@app.post("/seed")
async def seed(request: Request, body: SeedBody):
    result = await _engine(request).seed_from_playlist(body.playlist.strip())
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/playpause")
async def playpause(request: Request):
    """Toggle play/pause based on the live player state."""
    result = await _engine(request).toggle_playback()
    return JSONResponse(result)


@app.post("/stop")
async def stop(request: Request):
    """Stop playback, clear the queue, and park the auto-DJ until restarted."""
    result = await _engine(request).stop()
    return JSONResponse(result)


@app.post("/pause")
async def pause(request: Request):
    # Kept for back-compat; delegates to the toggle so a paused player resumes.
    result = await _engine(request).toggle_playback()
    return JSONResponse(result)


@app.post("/skip")
async def skip(request: Request):
    result = await _engine(request).skip()
    return JSONResponse(result)


@app.post("/like")
async def like(request: Request):
    result = await _engine(request).like_current()
    return JSONResponse(result, status_code=200 if result.get("ok") else 502)


@app.post("/block")
async def block(request: Request):
    result = await _engine(request).block_current()
    return JSONResponse(result, status_code=200 if result.get("ok") else 502)


@app.post("/nudge")
async def nudge(request: Request):
    result = await _engine(request).nudge()
    return JSONResponse(result, status_code=200 if result.get("ok") else 502)


@app.post("/energy")
async def energy(request: Request, body: EnergyBody):
    result = await _engine(request).nudge_energy(body.direction)
    return JSONResponse(result, status_code=200 if result.get("ok") else 502)


@app.get("/users")
async def users(request: Request):
    return {"people": _engine(request).list_users()}


@app.post("/users/select")
async def select_user(request: Request, body: UserSelectBody):
    result = await _engine(request).select_user(body.slug)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/users/add")
async def add_user(request: Request, body: UserAddBody):
    result = _engine(request).add_user(body.name)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/users/rename")
async def rename_user(request: Request, body: UserRenameBody):
    result = _engine(request).rename_user(body.slug, body.name)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/users/delete")
async def delete_user(request: Request, body: UserSelectBody):
    result = _engine(request).delete_user(body.slug)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/save_playlist")
async def save_playlist(request: Request, body: SavePlaylistBody):
    result = await _engine(request).save_session_playlist(body.name)
    return JSONResponse(result, status_code=200 if result.get("ok") else 502)


@app.get("/history")
async def history(request: Request):
    return {"decisions": list(_engine(request).decision_log)}
