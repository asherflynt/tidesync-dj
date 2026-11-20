"""Client for the optional style tagger (Discogs-EffNet), and style-vector maths.

The tagger runs on another machine with access to the music files (see
``styles_tagger/`` in the repository). For every file it measures, from the audio,
how much it sounds like each of 400 Discogs styles ("Folk", "Contemporary R&B",
"K-pop", "Hard Trance", ...), plus a style embedding it uses to find files that
sound alike. TideSync syncs the 400-style vectors into its mirror and asks the
tagger for neighbours of a mix's anchors.

Everything here is optional: with no ``styles_url`` the DJ works exactly as before.
"""
from __future__ import annotations

import base64
import logging
import math
from typing import Any

import httpx

_LOGGER = logging.getLogger("tidesync.styles")

N_STYLES = 400


class StylesUnavailable(RuntimeError):
    """The tagger could not be reached or answered with an error."""


class StylesClient:
    """Small async client for the tagger's read-only HTTP API."""

    def __init__(self, base_url: str, *, timeout: float = 30.0, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def _get(self, endpoint: str, **params: Any) -> dict[str, Any]:
        try:
            res = await self._client.get(f"{self.base_url}{endpoint}", params=params)
        except httpx.HTTPError as err:
            raise StylesUnavailable(f"style tagger at {self.base_url} unreachable: {err}") from err
        if res.status_code == 404 and endpoint == "/v1/similar":
            return {"items": None}  # that file has not been measured yet
        if res.status_code != 200:
            raise StylesUnavailable(f"style tagger {endpoint} answered {res.status_code}")
        try:
            return res.json()
        except ValueError as err:
            raise StylesUnavailable(f"style tagger {endpoint} sent invalid JSON") from err

    async def status(self) -> dict[str, Any]:
        return await self._get("/v1/status")

    async def labels(self) -> list[str]:
        return list((await self._get("/v1/labels")).get("labels") or [])

    async def tracks(self, after: int, limit: int = 2000) -> dict[str, Any]:
        """Analysed files changed after ``after`` (a sequence number), oldest first."""
        return await self._get("/v1/tracks", after=after, limit=limit)

    async def similar(self, path: str, limit: int = 300) -> list[dict[str, Any]] | None:
        """Files whose style embedding is closest to ``path``'s; ``None`` if it is not analysed."""
        return (await self._get("/v1/similar", path=path, limit=limit)).get("items")

    async def close(self) -> None:
        await self._client.aclose()


def decode_vector(encoded: str) -> bytes | None:
    """The tagger's base64 400-byte style vector, or ``None`` if malformed."""
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None
    return raw if len(raw) == N_STYLES else None


def style_similarity(a: bytes, b: bytes) -> float:
    """Cosine similarity (0..1) of two style vectors.

    Square roots of the probabilities are compared (the Hellinger affinity), so the
    many small style scores count and a couple of generic labels that most songs get
    ("Pop---Ballad", "Rock---Alternative Rock") do not decide it on their own.
    """
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        sx, sy = math.sqrt(x), math.sqrt(y)
        dot += sx * sy
        na += x
        nb += y
    if not na or not nb:
        return 0.0
    return dot / math.sqrt(na * nb)


def top_styles(vec: bytes, labels: list[str], count: int = 2) -> list[str]:
    """Short names of the strongest styles ("Folk", "Country"), strongest first."""
    order = sorted(range(min(len(vec), len(labels))), key=lambda i: -vec[i])[:count]
    return [labels[i].split("---")[-1] for i in order if vec[i] > 0]
