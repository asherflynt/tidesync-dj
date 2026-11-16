"""Candidate retrieval (v2): turn a plan into scored library tracks.

Every candidate is a track id from the local mirror, so nothing here can
suggest music the listener does not own. Sources, each contributing a
relevance between 0 and 1:

* sonic text search for each plan query (what the set should sound like),
* sonic neighbours of seed tracks (a chosen song, recent likes, what just played),
* rediscovery: analysed tracks not heard for a while, for variety.

The scores are merged (a track found by several sources ranks higher), then
adjusted by the listener's history: likes lift a track, skips push it down.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from library import Library
from ma import MAClient, MAError
from plan import Plan

_LOGGER = logging.getLogger("tidesync.retrieval")

TEXT_LIMIT = 250
SIMILAR_LIMIT = 100
REDISCOVER_LIMIT = 60
REDISCOVER_AGE = 30 * 86400


@dataclass
class Sources:
    """How many candidates each source produced; shown in the DJ status view."""

    text: int = 0
    similar: int = 0
    rediscover: int = 0
    fallback: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "similar": self.similar, "rediscover": self.rediscover,
                "fallback": self.fallback, "notes": self.notes}


def _rank_scores(items: list[dict[str, Any]], top: float, floor: float) -> dict[tuple[str, str], float]:
    """Spread results evenly from ``top`` (best match) down to ``floor`` (last)."""
    n = len(items)
    return {
        (it["provider"], str(it["item_id"])): top - (top - floor) * (i / max(n - 1, 1))
        for i, it in enumerate(items)
    }


def _merge(into: dict[int, float], scores: dict[int, float]) -> None:
    """Combine evidence: agreeing sources reinforce each other without exceeding 1."""
    for tid, score in scores.items():
        prev = into.get(tid, 0.0)
        into[tid] = 1.0 - (1.0 - prev) * (1.0 - score)


async def gather(
    ma: MAClient,
    lib: Library,
    plan: Plan,
    *,
    seed_ids: Iterable[int] = (),
    liked_ids: Iterable[int] = (),
    exclude_ids: Iterable[int] = (),
) -> tuple[dict[int, float], Sources]:
    """Return ``{track_id: relevance}`` for the plan, plus per-source counts."""
    scores: dict[int, float] = {}
    src = Sources()
    seed_ids, liked_ids = list(seed_ids), list(liked_ids)

    def resolve(pairs: dict[tuple[str, str], float]) -> dict[int, float]:
        out: dict[int, float] = {}
        for (provider, item_id), score in pairs.items():
            tid = lib.resolve(provider, item_id)
            if tid is not None:
                out[tid] = max(out.get(tid, 0.0), score)
        return out

    # 1. what the set should sound like
    for query in plan.queries:
        try:
            res = await ma.cmd(
                "sonic_similarity/text_search", query=query, limit=TEXT_LIMIT,
                exclude=plan.avoid or None, timeout=120,
            )
        except MAError as err:
            src.notes.append("Sound search is unavailable in Music Assistant."
                             if err.unsupported else f"Sound search failed: {err.details}")
            break
        if not (res or {}).get("analyzed"):
            src.notes.append(f"Sound search returned nothing ({(res or {}).get('reason') or 'no reason'}).")
            break
        found = resolve(_rank_scores(res.get("items") or [], top=0.95, floor=0.35))
        src.text += len(found)
        _merge(scores, found)

    # 2. neighbours of seeds (strong) and of recent likes (gentler)
    for ids, top, label in ((seed_ids, 0.95, "seed"), (liked_ids, 0.6, "likes")):
        sonic_ids = lib.sonic_seeds(ids)[:10]
        if not sonic_ids:
            if ids and label == "seed":
                src.notes.append("The seed track has not been analysed yet, so it cannot steer the set.")
            continue
        try:
            res = await ma.cmd(
                "sonic_similarity/similar", item_ids=sonic_ids, limit=SIMILAR_LIMIT,
                preset="vibe", diversity=0.3, timeout=120,
            )
        except MAError as err:
            src.notes.append(f"Similar-track search failed: {err.details}")
            continue
        if not (res or {}).get("analyzed"):
            continue
        found = resolve(_rank_scores(res.get("items") or [], top=top, floor=0.3))
        src.similar += len(found)
        _merge(scores, found)

    # 3. variety: things not heard in a while, kept below real matches
    old = lib.rediscover(REDISCOVER_LIMIT, not_played_since=int(time.time()) - REDISCOVER_AGE)
    fresh = {tid: 0.25 for tid in old if tid not in scores}
    src.rediscover = len(fresh)
    _merge(scores, fresh)

    # 4. nothing matched at all: fall back to any analysed tracks so music still plays
    if not scores:
        fallback = {tid: 0.3 for tid in lib.random_analysed(200)}
        src.fallback = len(fallback)
        scores.update(fallback)

    for tid in exclude_ids:
        scores.pop(tid, None)

    # history: likes lift, skips push down
    liked = set(liked_ids)
    penalties = lib.skip_penalties(scores)
    for tid in list(scores):
        value = scores[tid] + (0.1 if tid in liked else 0.0) - penalties.get(tid, 0.0)
        scores[tid] = max(0.0, min(1.0, value))
    return scores, src
