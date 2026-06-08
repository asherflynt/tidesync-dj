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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_LOGGER = logging.getLogger("tidesync")

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    if not config.anthropic_api_key:
        _LOGGER.warning("No anthropic_api_key configured — DJ decisions will fail.")

    ma = MusicAssistantClient(config.ma_ws_url)
    ha = HAClient()
    brain = ClaudeBrain(config.anthropic_api_key, config.claude_model)
    taste = TasteProfile(config.data_dir)
    engine = DJEngine(config, ma, ha, brain, taste)

    app.state.engine = engine
    app.state.ma = ma
    app.state.config = config

    await engine.start()
    _LOGGER.info("TideSync DJ started (model=%s)", config.claude_model)
    try:
        yield
    finally:
        await engine.stop()
        _LOGGER.info("TideSync DJ stopped")


app = FastAPI(title="TideSync DJ", lifespan=lifespan)


class VibeBody(BaseModel):
    prompt: str


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
    engine = _engine(request)
    engine.vibe_prompt = body.prompt.strip()
    _LOGGER.info("Vibe set to: %s", engine.vibe_prompt)
    return {"ok": True, "vibe": engine.vibe_prompt}


@app.get("/queue")
async def queue(request: Request):
    ma: MusicAssistantClient = request.app.state.ma
    q = await ma.get_queue()
    return {
        "queue_id": q.get("queue_id"),
        "items_remaining": q.get("items_remaining", 0),
        "current_index": q.get("current_index", 0),
        "items": [
            {
                "name": (it.get("media_item") or it).get("name"),
                "uri": (it.get("media_item") or it).get("uri"),
            }
            for it in q.get("items", [])
        ],
    }


@app.post("/tick")
async def tick(request: Request):
    result = await _engine(request).tick(reason="manual")
    status_code = 200 if result.get("ok") else 502
    return JSONResponse(result, status_code=status_code)


@app.get("/history")
async def history(request: Request):
    return {"decisions": list(_engine(request).decision_log)}
