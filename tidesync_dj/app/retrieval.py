"""Candidate retrieval: turn a plan and the listening so far into scored library tracks.

Every candidate is a track id from the local mirror, so nothing here can
suggest music the listener does not own. Similarity always comes from Music
Assistant's sonic analysis, never from names:

* sound neighbours of the station's anchors through the sound (CLAP) index,
  which compares how whole songs sound. The anchors are the song a mix was
  started from (or what is playing when no vibe is set) and, with less weight,
  the last songs that played to the end, so the station follows the listener
  without drifting away from where it started,
* the similar-track ("Traits") index, an 18-number summary of tempo, energy
  and timbre, only for an anchor the sound index does not hold yet: it cannot
  tell a folk ballad from a slow rap song, so it is a fallback, not a source,
* sound search for each plan query when a vibe is typed, plus the sound
  neighbours of the best matches,
* for a vibe or no direction at all: analysed tracks outside the sound index
  whose energy suits the set, and rediscoveries (not heard for a while).

A mix started from a song (or following what plays) is filled from sound
neighbours only. Energy and rediscovery filler joins it only when too few
neighbours are left, so random songs never slip in between close matches.

Songs that were skipped early this session push their own sound neighbours
down, likes lift a track and skip history lowers it.
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
TEXT_SEEDS_PER_QUERY = 3  # best sound matches per query whose sound neighbours are added
SOUND_LIMIT = 300  # sound (CLAP) neighbours fetched per anchor
VIBE_NEIGHBOUR_LIMIT = 100  # sound neighbours per best sound match of a vibe
RANK_DAMPING = 40  # neighbour rank -> score (k+1)/(k+rank): 1st 1.0, 40th ~0.5, 300th ~0.12
SAME_RECORDING = 0.02  # sound distance below which two files hold the same recording
TRAITS_TRUST = 0.7  # Traits neighbours count for this much of a sound neighbour
RECENT_WEIGHT = 0.25  # each song that just played to the end steers this much (seed = 1)
LIKED_ANCHORS = 10  # recent likes that steer a session with no seed, song or vibe
AVOID_LIMIT = 50  # sound neighbours of an early skip that are pushed down
AVOID_WEIGHT = 0.6  # the skipped song's closest neighbour loses 60%, the 50th nothing
WIDEN_FACTOR = 4  # a mix with fewer candidates than this x slots is widened
WIDE_SOUND_LIMIT = 1000
ENERGY_LIMIT = 150
ENERGY_MARGIN = 1.0  # energy top-up window beyond the set's target range
REDISCOVER_LIMIT = 60
REDISCOVER_AGE = 30 * 86400


@dataclass
class Sources:
    """How many candidates each source produced; shown in the DJ status view."""

    text: int = 0
    vibe_neighbours: int = 0
    similar: int = 0  # neighbours of the anchors (seed, playing song, recent plays, likes)
    traits: int = 0  # of those, how many came from the Traits fallback
    energy: int = 0
    rediscover: int = 0
    fallback: int = 0
    avoided: int = 0  # candidates pushed down for sounding like an early skip
    widened: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "vibe_neighbours": self.vibe_neighbours, "similar": self.similar,
                "traits": self.traits, "energy": self.energy, "rediscover": self.rediscover,
                "fallback": self.fallback, "avoided": self.avoided, "widened": self.widened,
                "notes": self.notes}


def _rank_scores(items: list[dict[str, Any]], top: float, floor: float) -> dict[tuple[str, str], float]:
    """Spread results evenly from ``top`` (best match) down to ``floor`` (last)."""
    n = len(items)
    return {
        (it["provider"], str(it["item_id"])): top - (top - floor) * (i / max(n - 1, 1))
        for i, it in enumerate(items)
    }


def rank_score(rank: int) -> float:
    """Score of the ``rank``-th nearest neighbour (1-based): 1.0, then a gentle decline.

    Ranks rather than raw distances, because how far apart songs are differs a lot
    between corners of a library (a dense folk corner vs. sparse free jazz), while
    "the 20th closest song" means the same everywhere.
    """
    return (RANK_DAMPING + 1) / (RANK_DAMPING + max(rank, 1))


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


class _Sonic:
    """Neighbour lookups for one gather: sound (CLAP) index first, Traits as a fallback."""

    def __init__(self, ma: MAClient, lib: Library, src: Sources) -> None:
        self._ma, self._lib, self._src = ma, lib, src
        self.sound_ok = True  # False once MA says it has no sound-neighbour command
        self.traits_ok = True
        self._cache: dict[tuple[str, str, int], tuple[list[tuple[int, float]], str]] = {}

    def resolve(self, items: Iterable[dict[str, Any]]) -> list[tuple[int, float]]:
        """``[(track_id, distance)]`` nearest first; one entry per library track."""
        out: list[tuple[int, float]] = []
        seen: set[int] = set()
        for it in items:
            tid = self._lib.resolve(it["provider"], str(it["item_id"]))
            if tid is None or tid in seen:
                continue
            seen.add(tid)
            out.append((tid, float(it.get("distance") or 0.0)))
        return out

    async def neighbours(self, pair: tuple[str, str], limit: int = SOUND_LIMIT,
                         ) -> tuple[list[tuple[int, float]], str]:
        """Closest tracks to one analysed file, and which index answered ("sound", "traits", "")."""
        key = (pair[0], pair[1], limit)
        if key not in self._cache:
            self._cache[key] = await self._lookup(pair, limit)
        return self._cache[key]

    async def _lookup(self, pair: tuple[str, str], limit: int) -> tuple[list[tuple[int, float]], str]:
        provider, item_id = pair
        if self.sound_ok:
            try:
                res = await self._ma.cmd("sonic_similarity/similar_clap", item_id=item_id,
                                         seed_provider=provider or None, limit=limit, timeout=60)
            except MAError as err:
                res = None
                if err.unsupported or err.provider_unavailable:
                    self.sound_ok = False  # sound index off, or an older MA: Traits for the rest
                else:
                    self._src.notes.append(f"Finding songs that sound alike failed: {err.details}")
            if isinstance(res, dict) and res.get("analyzed"):
                return self.resolve(res.get("items") or []), "sound"
        if not self.traits_ok:
            return [], ""
        try:
            res = await self._ma.cmd("sonic_similarity/similar", **_seed_args([pair]),
                                     limit=min(limit, SIMILAR_LIMIT), preset="vibe", diversity=0.3,
                                     timeout=120)
        except MAError as err:
            self.traits_ok = False
            self._src.notes.append("Finding similar songs is unavailable in Music Assistant."
                                   if err.unsupported else f"Finding similar songs failed: {err.details}")
            return [], ""
        if isinstance(res, dict) and res.get("analyzed"):
            found = self.resolve(res.get("items") or [])
            # Traits neighbours come from analysis rows, so they are analysed; this is how
            # tracks whose analysis is copied in (e.g. keyed to Navidrome) join the pool.
            self._lib.mark_analysed(tid for tid, _ in found)
            return found, "traits"
        return [], ""


async def gather(
    ma: MAClient,
    lib: Library,
    plan: Plan,
    *,
    seed_ids: Iterable[int] = (),
    liked_ids: Iterable[int] = (),
    exclude_ids: Iterable[int] = (),
    targets: Sequence[float] = (),
    recent_ids: Iterable[int] = (),
    avoid_ids: Iterable[int] = (),
) -> tuple[dict[int, float], Sources]:
    """Return ``{track_id: relevance}`` for the plan, plus per-source counts.

    :param seed_ids: what the station sounds like: the song a mix started from,
        or what is playing when no vibe is set. Sound neighbours of these lead.
    :param recent_ids: songs that just played to the end; they steer gently
        (each counts ``RECENT_WEIGHT`` of a seed) so a mix evolves with the listener.
    :param avoid_ids: songs skipped early this session; their sound neighbours
        are pushed down.
    :param liked_ids: the listener's recent likes. With no seed and no vibe they
        steer the session; otherwise they only lift tracks that are already candidates.
    :param targets: the energy targets of the slots being filled; the energy
        top-up looks for tracks in that range (defaults to the plan's range).
    """
    scores: dict[int, float] = {}
    src = Sources()
    sonic = _Sonic(ma, lib, src)
    seed_ids, liked_ids = list(dict.fromkeys(seed_ids)), list(liked_ids)
    exclude = set(exclude_ids)
    slots = max(len(targets), 1)

    # 1. a typed vibe: what the set should sound like (sound index only)
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
        found: dict[int, float] = {}
        for (provider, item_id), score in _rank_scores(items, top=0.95, floor=0.35).items():
            tid = lib.resolve(provider, item_id)
            if tid is not None:
                found[tid] = max(found.get(tid, 0.0), score)
        src.text += len(found)
        _merge(scores, found)
        text_seeds.extend((it["provider"], str(it["item_id"])) for it in items[:TEXT_SEEDS_PER_QUERY])

    # 1b. songs that sound like the best matches reach tracks the text search ranks lower
    vibe_near: dict[int, float] = {}
    for pair in list(dict.fromkeys(text_seeds))[:10]:
        near, _ = await sonic.neighbours(pair, VIBE_NEIGHBOUR_LIMIT)
        for rank, (tid, _) in enumerate(near, 1):
            vibe_near[tid] = max(vibe_near.get(tid, 0.0), 0.7 * rank_score(rank))
    src.vibe_neighbours = len(vibe_near)
    _merge(scores, vibe_near)

    # 2. the station's anchors: seed songs lead, songs that just played steer gently
    anchors = [(tid, 1.0) for tid in seed_ids]
    if anchors:
        anchors += [(tid, RECENT_WEIGHT) for tid in dict.fromkeys(recent_ids) if tid not in seed_ids]
    elif not plan.queries:
        anchors = [(tid, 0.6) for tid in liked_ids[-LIKED_ANCHORS:]]
    seeded = bool(seed_ids)
    near_anchor = await _anchor_neighbours(sonic, lib, anchors, src, SOUND_LIMIT)
    if seeded and len(set(near_anchor) - exclude) < WIDEN_FACTOR * slots:
        # a long session has used up the closest songs: look further out before any filler
        near_anchor = await _anchor_neighbours(sonic, lib, anchors, src, WIDE_SOUND_LIMIT)
        src.widened = True
    src.similar = len(near_anchor)
    _merge(scores, near_anchor)

    need_filler = not seeded or len(set(scores) - exclude) < WIDEN_FACTOR * slots
    if need_filler:
        _add_filler(lib, plan, scores, src, targets, rediscover=not plan.queries or seeded)

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

    for tid in exclude:
        scores.pop(tid, None)

    # less of what was just skipped: its sound neighbours are pushed down
    avoid = await _avoid_scores(sonic, lib, list(dict.fromkeys(avoid_ids)))
    for tid, penalty in avoid.items():
        if tid in scores:
            scores[tid] *= 1.0 - penalty
            src.avoided += 1

    # history: likes lift, skips push down
    liked = set(liked_ids)
    penalties = lib.skip_penalties(scores)
    for tid in list(scores):
        value = scores[tid] + (0.1 if tid in liked else 0.0) - penalties.get(tid, 0.0)
        scores[tid] = max(0.0, min(1.0, value))
    src.notes = list(dict.fromkeys(src.notes))
    return scores, src


async def _anchor_neighbours(sonic: _Sonic, lib: Library, anchors: list[tuple[int, float]],
                             src: Sources, limit: int) -> dict[int, float]:
    """Fuse the anchors' neighbour lists into one 0..1 score per track.

    Each anchor contributes its weight times the neighbour's rank score; the sum is
    divided by the total weight, so a song close to the seed and to what just played
    beats one close to only one of them. The best candidate scores 1.
    """
    if not anchors:
        return {}
    ids = [tid for tid, _ in anchors]
    pairs = dict(zip(ids, lib.similar_seed_pairs(ids)))
    expanded: list[tuple[int, float, tuple[str, str]]] = []
    for tid, weight in anchors:
        pair = pairs.get(tid)
        if pair is not None:
            expanded.append((tid, weight, pair))
            continue
        if weight < 1.0:
            continue
        # a seed that has not been analysed: other analysed songs by the same artist stand in
        stand_ins = lib.artist_stand_ins(tid, limit=3)
        if not stand_ins:
            src.notes.append("That song has not been analysed yet, so the DJ cannot look for "
                             "songs that sound like it.")
            continue
        src.notes.append("That song has not been analysed yet, so the DJ is following other "
                         "songs by the same artist that have been.")
        for sid, spair in stand_ins:
            expanded.append((sid, weight / len(stand_ins), spair))
    fused: dict[int, float] = {}
    same_recording: set[int] = set(ids)
    traits = 0
    for tid, weight, pair in expanded:
        near, index = await sonic.neighbours(pair, limit)
        if not near:
            if weight >= 1.0 and index == "":
                src.notes.append("Music Assistant has not indexed how that song sounds yet, so "
                                 "similar songs could not be looked up.")
            continue
        trust = 1.0 if index == "sound" else TRAITS_TRUST
        rank = 0
        for nid, distance in near:
            if nid == tid:
                continue
            if index == "sound" and distance < SAME_RECORDING:
                same_recording.add(nid)  # the anchor itself in another file or album
                continue
            rank += 1
            fused[nid] = fused.get(nid, 0.0) + weight * trust * rank_score(rank)
            traits += index == "traits"
    for nid in same_recording:
        fused.pop(nid, None)
    src.traits = max(src.traits, traits)
    if not fused:
        return {}
    best = max(fused.values())
    return {nid: round(score / best, 4) for nid, score in fused.items()}


async def _avoid_scores(sonic: _Sonic, lib: Library, avoid_ids: list[int]) -> dict[int, float]:
    """Penalty (0..AVOID_WEIGHT) for tracks that sound like a song skipped early."""
    out: dict[int, float] = {}
    pairs = lib.similar_seed_pairs(avoid_ids)
    for tid, pair in zip(avoid_ids, pairs):
        if pair is None:
            continue
        near, index = await sonic.neighbours(pair, AVOID_LIMIT)
        if index != "sound":
            continue  # Traits neighbours are too loose to punish anything for
        out[tid] = AVOID_WEIGHT
        for rank, (nid, _) in enumerate((n for n in near if n[0] != tid), 1):
            # steep: only what sounds most like the skip is pushed down, not its whole corner
            penalty = AVOID_WEIGHT * (1.0 - (rank - 1) / AVOID_LIMIT) ** 2
            out[nid] = max(out.get(nid, 0.0), round(penalty, 4))
    return out


def _add_filler(lib: Library, plan: Plan, scores: dict[int, float], src: Sources,
                targets: Sequence[float], *, rediscover: bool) -> None:
    """Energy top-up and rediscoveries, always ranked below real matches."""
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
    if rediscover:
        old = lib.rediscover(REDISCOVER_LIMIT, not_played_since=int(time.time()) - REDISCOVER_AGE)
        fresh = {tid: 0.25 for tid in old if tid not in scores}
        src.rediscover = len(fresh)
        _merge(scores, fresh)
