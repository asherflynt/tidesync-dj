"""Candidate retrieval (v2): turn a plan into scored library tracks.

Every candidate is a track id from the local mirror, so nothing here can
suggest music the listener does not own. Sources, each contributing a
relevance between 0 and 1:

* sound search for each plan query (what the set should sound like). It can
  only return tracks in Music Assistant's sound (CLAP) index, which may hold
  far fewer tracks than have been analysed,
* neighbours of the best sound matches, through the similar-track ("Traits")
  index, which reaches analysed tracks the sound index does not hold yet,
* neighbours of seed tracks (a chosen song, recent likes, what just played),
* analysed tracks outside the sound index whose energy suits the set, so the
  whole analysed pool is in play while the sound index catches up,
* rediscovery: analysed tracks not heard for a while, for variety.

The scores are merged (a track found by several sources ranks higher), then
adjusted by the listener's history: likes lift a track, skips push it down.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from library import Library
from ma import MAClient, MAError
from plan import Plan

_LOGGER = logging.getLogger("tidesync.retrieval")

TEXT_LIMIT = 250
SIMILAR_LIMIT = 100  # Music Assistant clamps sonic_similarity/similar to 100
TEXT_SEEDS_PER_QUERY = 3  # best sound matches per query that seed the neighbour search
ENERGY_LIMIT = 150
ENERGY_MARGIN = 1.0  # energy top-up window beyond the set's target range
REDISCOVER_LIMIT = 60
REDISCOVER_AGE = 30 * 86400


@dataclass
class Sources:
    """How many candidates each source produced; shown in the DJ status view."""

    text: int = 0
    vibe_neighbours: int = 0
    similar: int = 0
    energy: int = 0
    rediscover: int = 0
    fallback: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "vibe_neighbours": self.vibe_neighbours, "similar": self.similar,
                "energy": self.energy, "rediscover": self.rediscover, "fallback": self.fallback,
                "notes": self.notes}


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


def _seed_args(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """Arguments for sonic_similarity/similar from ``(provider, item_id)`` seeds.

    ``seed_provider`` disambiguates ids that exist under several providers; MA
    takes one value for all seeds, so it is only sent when they agree.
    """
    providers = {p for p, _ in pairs if p}
    args: dict[str, Any] = {"item_ids": [item_id for _, item_id in pairs]}
    if len(providers) == 1:
        args["seed_provider"] = providers.pop()
    return args


async def gather(
    ma: MAClient,
    lib: Library,
    plan: Plan,
    *,
    seed_ids: Iterable[int] = (),
    liked_ids: Iterable[int] = (),
    exclude_ids: Iterable[int] = (),
    targets: Sequence[float] = (),
) -> tuple[dict[int, float], Sources]:
    """Return ``{track_id: relevance}`` for the plan, plus per-source counts.

    :param targets: the energy targets of the slots being filled; the energy
        top-up looks for tracks in that range (defaults to the plan's range).
    """
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

    async def similar(pairs: list[tuple[str, str]], *, union: bool) -> dict[str, Any] | None:
        try:
            return await ma.cmd(
                "sonic_similarity/similar", **_seed_args(pairs), limit=SIMILAR_LIMIT,
                preset="vibe", diversity=0.3, blend_mode="union" if union else None, timeout=120,
            )
        except MAError as err:
            src.notes.append("Finding similar songs is unavailable in Music Assistant."
                             if err.unsupported else f"Finding similar songs failed: {err.details}")
            return None

    # 1. what the set should sound like (sound index only)
    text_seeds: list[tuple[str, str]] = []
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
        items = res.get("items") or []
        found = resolve(_rank_scores(items, top=0.95, floor=0.35))
        src.text += len(found)
        _merge(scores, found)
        text_seeds.extend((it["provider"], str(it["item_id"])) for it in items[:TEXT_SEEDS_PER_QUERY])

    # 1b. reach past the sound index: tracks that resemble the best sound matches
    if text_seeds:
        res = await similar(list(dict.fromkeys(text_seeds))[:10], union=True)
        if (res or {}).get("analyzed"):
            found = resolve(_rank_scores(res.get("items") or [], top=0.7, floor=0.3))
            src.vibe_neighbours = len(found)
            _merge(scores, found)

    # 2. neighbours of seeds (strong) and of recent likes (gentler)
    for ids, top, label in ((seed_ids, 0.95, "seed"), (liked_ids, 0.6, "likes")):
        pairs = lib.similar_seeds(ids)[-10:] if label == "likes" else lib.similar_seeds(ids)[:10]
        if not pairs:
            if ids and label == "seed":
                src.notes.append("That song has not been analysed yet, so the DJ cannot look for "
                                 "songs that sound like it.")
            continue
        res = await similar(pairs, union=False)
        if res is None:
            continue
        if not res.get("analyzed"):
            if label == "seed" and res.get("reason") == "seed_not_in_index":
                src.notes.append("Music Assistant has not indexed how that song sounds yet, so "
                                 "similar songs could not be looked up.")
            continue
        found = resolve(_rank_scores(res.get("items") or [], top=top, floor=0.3))
        src.similar += len(found)
        _merge(scores, found)

    # 3. the rest of the analysed pool, by energy: the sound search cannot reach it yet
    if targets:
        low, high = min(targets) - ENERGY_MARGIN, max(targets) + ENERGY_MARGIN
        center = sum(targets) / len(targets)
    else:
        low, high = plan.base_energy - plan.span - ENERGY_MARGIN, plan.base_energy + plan.span + ENERGY_MARGIN
        center = plan.base_energy
    near = lib.near_energy(low, high, center, ENERGY_LIMIT)
    width = max(high - center, center - low, 0.5)
    fit = {tid: round(0.3 - 0.15 * min(off / width, 1.0), 3) for tid, off in near.items() if tid not in scores}
    src.energy = len(fit)
    _merge(scores, fit)

    # 4. variety: things not heard in a while, kept below real matches
    old = lib.rediscover(REDISCOVER_LIMIT, not_played_since=int(time.time()) - REDISCOVER_AGE)
    fresh = {tid: 0.25 for tid in old if tid not in scores}
    src.rediscover = len(fresh)
    _merge(scores, fresh)

    # 5. nothing matched at all: fall back to any analysed tracks so music still plays
    if not scores:
        fallback = {tid: 0.3 for tid in lib.random_analysed(200)}
        src.fallback = len(fallback)
        scores.update(fallback)

    if plan.queries and src.energy:
        counts = lib.stats()
        if counts["in_sound_index"] < counts["analysed"]:
            src.notes.append(
                f"A vibe can be matched by sound against {counts['in_sound_index']:,} of the "
                f"{counts['analysed']:,} ready tracks so far; others were added by energy.")

    src.notes = list(dict.fromkeys(src.notes))
    for tid in exclude_ids:
        scores.pop(tid, None)

    # history: likes lift, skips push down
    liked = set(liked_ids)
    penalties = lib.skip_penalties(scores)
    for tid in list(scores):
        value = scores[tid] + (0.1 if tid in liked else 0.0) - penalties.get(tid, 0.0)
        scores[tid] = max(0.0, min(1.0, value))
    return scores, src
