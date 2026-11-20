"""Deterministic set sequencer (v2).

Pure functions, no I/O and no AI: given candidate tracks with measured features
(BPM, key, energy) this picks and orders the next N tracks so that

* the set follows a target energy curve,
* each transition is musically close (tempo, key, energy step),
* artists are spaced out, and
* tracks that matched the request best are preferred.

Missing features are tolerated: an unknown value costs a fixed, moderate
penalty instead of being treated as a perfect or impossible match.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from typing import Iterable, Sequence

# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------

_PITCH = {
    "c": 0, "b#": 0, "c#": 1, "db": 1, "d": 2, "d#": 3, "eb": 3, "e": 4, "fb": 4,
    "f": 5, "e#": 5, "f#": 6, "gb": 6, "g": 7, "g#": 8, "ab": 8, "a": 9, "a#": 10,
    "bb": 10, "b": 11, "cb": 11,
}
# Camelot number for each major-key pitch class (C major = 8B, G = 9B, ...).
_MAJOR_NUM = {0: 8, 7: 9, 2: 10, 9: 11, 4: 12, 11: 1, 6: 2, 1: 3, 8: 4, 3: 5, 10: 6, 5: 7}
_CAMELOT_RE = re.compile(r"^(1[0-2]|[1-9])([ab])$", re.I)
_KEY_RE = re.compile(r"^([a-g](?:#|b|♯|♭)?)\s*(major|minor|maj|min|m)?$", re.I)


def camelot(key: str | None) -> str | None:
    """Convert a key name ("A minor", "F# major", "Am", "8A") to Camelot notation."""
    if not key:
        return None
    text = key.strip()
    m = _CAMELOT_RE.match(text)
    if m:
        return f"{int(m.group(1))}{m.group(2).upper()}"
    m = _KEY_RE.match(text)
    if not m:
        return None
    note = m.group(1).lower().replace("♯", "#").replace("♭", "b")
    mode = m.group(2) or "major"
    # a bare trailing "m" means minor ("Am"); "M"/"maj"/"major" mean major
    minor = mode == "m" or mode.lower() in ("minor", "min")
    pitch = _PITCH.get(note)
    if pitch is None:
        return None
    if minor:
        # relative major is three semitones up; same Camelot number, letter A
        return f"{_MAJOR_NUM[(pitch + 3) % 12]}A"
    return f"{_MAJOR_NUM[pitch]}B"


def key_cost(a: str | None, b: str | None) -> float:
    """Harmonic distance between two Camelot keys, 0 (same) to 1 (clash)."""
    if not a or not b:
        return 0.5
    na, la = int(a[:-1]), a[-1]
    nb, lb = int(b[:-1]), b[-1]
    steps = min((na - nb) % 12, (nb - na) % 12)
    if la == lb:
        return {0: 0.0, 1: 0.15, 2: 0.45}.get(steps, 1.0)
    # relative major/minor shares all notes; a diagonal move is still workable
    return {0: 0.15, 1: 0.5}.get(steps, 1.0)


# --------------------------------------------------------------------------
# Tempo
# --------------------------------------------------------------------------


def bpm_cost(a: float | None, b: float | None) -> float:
    """Tempo distance 0..1, treating half and double time as compatible."""
    if not a or not b or a <= 0 or b <= 0:
        return 0.5
    best = min(abs(math.log2(b * mult / a)) for mult in (0.5, 1.0, 2.0))
    # ~6% apart (a comfortable beat-match) costs 0.25; ~25% apart or more costs 1
    return min(1.0, best / 0.32)


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

# "(Acoustic Version)", "[Live]", "- Remastered 2011": the same song in another version
_VERSION_RE = re.compile(r"\s*[\(\[][^)\]]*[\)\]]|\s+-\s+.*$")
# "Calvin Harris feat. Rihanna", "Simon & Garfunkel": spacing goes by the lead artist
_FEATURING_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|with|x|&|and|vs\.?)\s+.*$", re.IGNORECASE)


@dataclass(frozen=True)
class Track:
    """A candidate track. ``relevance`` is 0..1, higher = better fit to the request."""

    id: str
    artist: str = ""
    bpm: float | None = None
    key: str | None = None  # Camelot
    energy: float | None = None  # 1..10
    relevance: float = 0.5
    title: str = ""

    @property
    def artist_key(self) -> str:
        """The lead artist, so "Ashnikko" and "Ashnikko, Princess Nokia" are spaced as one."""
        lead = self.artist.split(", ")[0].strip()
        return (_FEATURING_RE.sub("", lead).strip() or lead).casefold()

    @property
    def song_key(self) -> tuple[str, str]:
        """Artist and title without version notes: two versions of one song share it."""
        base = _VERSION_RE.sub("", self.title).strip().casefold()
        return self.artist_key, base or self.title.strip().casefold()


@dataclass(frozen=True)
class Weights:
    """Relative importance of each term in the placement score.

    The defaults suit a vibe set, where every candidate already matches the
    request and the energy arc gives the set its shape. Key is the weakest term:
    for listening (not beat-matched mixing) a pleasant key move matters far less
    than staying in the requested sound, and a strong key term made the DJ stack
    songs in one key while ignoring how well they fit.
    """

    energy_fit: float = 1.0
    bpm: float = 0.6
    key: float = 0.2
    energy_step: float = 0.6
    relevance: float = 1.5
    artist_gap: int = 6  # minimum tracks between two songs by the same artist
    artist_penalty: float = 3.0
    max_energy_step: float = 2.5  # steps larger than this are penalised hard


# A mix from a song (or following what plays): sounding like the anchors is the
# point, so relevance dominates. A top match in a clashing key and a 25% tempo
# gap costs 2.5*0.05 + 0.1 + 0.3 = 0.53; a half-good match in the same key at the
# same tempo costs 2.5*0.5 = 1.25, so the close match wins.
# A mix has hundreds of close songs to choose from, so artists can be spaced
# much further apart than in a set built from a narrower vibe.
RADIO = Weights(energy_fit=0.5, bpm=0.3, key=0.1, energy_step=0.5, relevance=2.5, artist_gap=12)


ARCS = ("flat", "build", "peak", "wind_down", "wave")


def energy_curve(arc: str, length: int, *, base: float = 5.5, span: float = 2.5, start: int = 0,
                 total: int | None = None) -> list[float]:
    """Target energy (1..10) for ``length`` slots of a set.

    ``start``/``total`` place this block inside a longer set so a rolling refill
    continues the same curve instead of restarting it.
    """
    total = max(total or (start + length), 1)
    out: list[float] = []
    for i in range(start, start + length):
        t = min(i / max(total - 1, 1), 1.0)
        if arc == "build":
            v = base - span + 2 * span * t
        elif arc == "wind_down":
            v = base + span - 2 * span * t
        elif arc == "peak":
            v = base - span + 2 * span * math.sin(math.pi * min(t / 0.7, 1.0) / 2) \
                if t <= 0.7 else base + span - 2 * span * ((t - 0.7) / 0.3) * 0.75
        elif arc == "wave":
            v = base + span * 0.7 * math.sin(2 * math.pi * i / 8)
        else:
            v = base
        out.append(max(1.0, min(10.0, v)))
    return out


def transition_cost(prev: Track, nxt: Track, w: Weights = Weights()) -> float:
    """Cost of playing ``nxt`` directly after ``prev`` (lower is smoother)."""
    cost = w.bpm * bpm_cost(prev.bpm, nxt.bpm) + w.key * key_cost(prev.key, nxt.key)
    if prev.energy is not None and nxt.energy is not None:
        step = abs(nxt.energy - prev.energy)
        cost += w.energy_step * (step / 9.0)
        if step > w.max_energy_step:
            cost += w.energy_step * (step - w.max_energy_step) / 3.0
    else:
        cost += w.energy_step * 0.25
    return cost


def _slot_cost(track: Track, target: float, w: Weights) -> float:
    fit = 0.35 if track.energy is None else min(abs(track.energy - target) / 4.0, 1.5)
    return w.energy_fit * fit + w.relevance * (1.0 - max(0.0, min(1.0, track.relevance)))


@dataclass
class _Path:
    cost: float
    picks: list[Track] = field(default_factory=list)
    used: frozenset[str] = frozenset()


def sequence(
    candidates: Iterable[Track],
    targets: Sequence[float],
    *,
    previous: Sequence[Track] = (),
    weights: Weights = Weights(),
    beam_width: int = 12,
) -> list[Track]:
    """Choose and order ``len(targets)`` tracks from ``candidates``.

    :param candidates: Pool to draw from; duplicates by id are ignored.
    :param targets: Target energy for each slot, in play order.
    :param previous: Tracks already played/queued, oldest first. The last one
        anchors the first transition and all of them count for artist spacing.
    :param beam_width: Number of partial orderings kept per slot.
    :returns: Up to ``len(targets)`` tracks; fewer when the pool is too small.
    """
    pool: dict[str, Track] = {}
    seen = {t.id for t in previous}
    heard = {t.song_key for t in previous if t.title}
    versions: dict[tuple[str, str], Track] = {}
    for cand in candidates:
        if cand.id in seen or cand.id in pool:
            continue
        if cand.title:
            song = cand.song_key
            if song in heard:
                continue  # another version of a song already played or queued
            other = versions.get(song)
            if other is not None:
                if (other.relevance, other.id) >= (cand.relevance, cand.id):
                    continue  # keep only the best-matching version of each song
                del pool[other.id]
            versions[song] = cand
        pool[cand.id] = cand
    tracks = sorted(pool.values(), key=lambda t: t.id)  # stable regardless of input order
    if not tracks or not targets:
        return []
    history = list(previous)
    beams = [_Path(0.0)]
    for target in targets:
        grown: list[_Path] = []
        for path in beams:
            line = history + path.picks
            last = line[-1] if line else None
            recent = [t.artist_key for t in line[-weights.artist_gap:]] if weights.artist_gap else []
            for cand in tracks:
                if cand.id in path.used:
                    continue
                cost = path.cost + _slot_cost(cand, target, weights)
                if last is not None:
                    cost += transition_cost(last, cand, weights)
                if cand.artist_key and cand.artist_key in recent:
                    # nearer repeats cost more, so spacing degrades gracefully
                    distance = len(recent) - max(i for i, a in enumerate(recent) if a == cand.artist_key)
                    cost += weights.artist_penalty * (1.0 - (distance - 1) / max(weights.artist_gap, 1))
                grown.append(_Path(cost, path.picks + [cand], path.used | {cand.id}))
        if not grown:
            break
        grown.sort(key=lambda p: (p.cost, [t.id for t in p.picks]))
        # keep the best few, but never two beams ending in the same track set
        beams, kept = [], set()
        for path in grown:
            sig = (path.used, path.picks[-1].id)
            if sig in kept:
                continue
            kept.add(sig)
            beams.append(path)
            if len(beams) >= beam_width:
                break
    return beams[0].picks


def nudge(targets: Sequence[float], delta: float) -> list[float]:
    """Shift a target curve up or down, clamped to the 1..10 scale."""
    return [max(1.0, min(10.0, t + delta)) for t in targets]


def with_relevance(track: Track, relevance: float) -> Track:
    return replace(track, relevance=relevance)
