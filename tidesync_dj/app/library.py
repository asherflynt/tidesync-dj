"""Local mirror of the Music Assistant library (v2).

The DJ only ever picks tracks that exist in this mirror, so every candidate is
a real, playable library track with a known URI. The mirror also holds the
measured features the sequencer needs (BPM, key, energy) and remembers which
tracks Music Assistant has analysed so far.

A track is "analysed" (the DJ may pick it) once Music Assistant has measured
it: either it reported a tempo or key for it, or the track is in the sound
(CLAP) index. The sound index can lag far behind the analysis itself, so it is
one source of "analysed", not the definition of it.

Sync runs in three independent passes, each safe to repeat:

1. ``sync_listing``   - page through the library (fast: ~500 tracks per call).
2. ``sync_analysed``  - ask the sound (CLAP) index which tracks it holds and
                        derive an energy value for each from how they sound.
3. ``sync_features``  - find tracks Music Assistant has analysed and fetch
                        their BPM/key. This does not depend on the sound index.

How pass 3 finds analysed tracks. Music Assistant (checked against 2.10.12)
has no command that lists analysed tracks; the only API that returns a track's
stored analysis is ``music/tracks/get``, whose ``audio_metadata`` carries BPM
and key (from Smart Fades) out of the ``audio_analysis`` table. Fetching every
track on every pass would be ~280k calls for a large library, so the pass
looks where analysis is expected to be:

* tracks the sound index already reported that still lack a tempo,
* tracks played since they were last checked (MA analyses during playback),
* "anchors" per filesystem provider. MA's background scan takes its candidates
  from a ``GROUP BY provider_item_id, provider_instance`` query, so it works
  through each provider's files in path order (the item id of a file is its
  relative path), and rows bulk-copied from another Music Assistant arrive in
  the same order. Each anchor is the last analysed file found; the pass walks
  forward from it while hits keep coming and stops after a run of misses or
  when the pass's wall-clock budget is spent, so a jump of thousands of newly
  analysed tracks is picked up within a few passes,
* when Music Assistant's own count says more tracks are analysed than the
  mirror has found: random probes over the unanalysed files (a hit starts a
  walk in both directions and becomes a new anchor) and a slow rolling sweep.
"""
from __future__ import annotations

import json
import logging
import random
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from ma import MAClient, MAError
from sequencer import Track, camelot

_LOGGER = logging.getLogger("tidesync.library")

PAGE = 500
# Phrases scored against every indexed track to place it on an energy scale.
ENERGY_HIGH = "high energy, loud, intense, driving, fast"
ENERGY_LOW = "calm, quiet, soft, gentle, slow"

# Music Assistant analyses files in the background only from these providers
# (FILESYSTEM_PROVIDER_DOMAINS in its audio_analysis controller). Their instance
# ids are "<domain>--<random>", e.g. "filesystem_smb--eDoxPtVW".
SCANNED_DOMAINS = ("filesystem_local", "filesystem_smb", "filesystem_nfs")

FEATURE_PASS_SECONDS = 180.0  # wall-clock cap for one sync_features pass
FRONTIER_MISS_RUN = 200  # consecutive unanalysed files that end a walk
FRONTIER_PAGE = 250  # mappings read from the mirror per query during a walk
MAX_ANCHORS = 6  # per provider
ANCHOR_IDLE = 7 * 86400  # an extra anchor with no hit for this long is dropped
PROBE_COUNT = 60  # random lookups per provider when MA reports tracks we have not found
PROBE_WINDOW = 500  # most unmeasured tracks one random seek may look through
PLAYED_BUDGET = 100
SWEEP_BUDGET = 200  # backstop lookups per pass
KNOWN_RETRY = 86400  # sound-index tracks without a tempo are re-checked daily
STATS_TTL = 5.0  # seconds stats() may be reused; dashboards poll it every few seconds
CAUGHT_UP_SLACK = 0.005  # mirror within 0.5% (+20) of MA's count counts as caught up

# Music Assistant's analysis coverage, sampled over time for a rate and ETA.
COVERAGE_DOMAINS = ("sonic_analysis", "smart_fades")
COVERAGE_MIN_GAP = 600  # seconds between stored samples
COVERAGE_KEEP = 8 * 86400
DAY = 86400
ETA_MIN_SPAN = 20 * 3600  # MA's scan is a nightly job: no ETA before ~a day of samples
RATE_MAX_DAYS = 3  # the rate is averaged over the last 1-3 whole days
# A reading that gains far more than MA could analyse in the time since the last one
# is a bulk import of analysis results (e.g. copied in from a faster machine). It
# counts as ready but not as MA's own pace, or the ETA would look days long.
STEP_MIN_GAIN = 1000  # never a step below this many tracks in one interval
STEP_RATE_FACTOR = 10  # ...and only when faster than this many times the quickest pace seen
STEP_FLOOR_RATE = 300  # tracks/hour assumed as the quickest pace before any is seen


def is_scanned_provider(provider: str) -> bool:
    """True for provider instances whose files MA analyses in the background."""
    return str(provider).split("--", 1)[0] in SCANNED_DOMAINS


def tempo_energy(bpm: float) -> float:
    """Rough energy (1-10) from tempo alone: 60 BPM -> 1, 120 -> 5.5, 180 -> 10.

    Used only for tracks that have a tempo but no energy from how they sound
    yet; a sound-based value replaces it when one arrives.
    """
    return round(max(1.0, min(10.0, 1.0 + 9.0 * (float(bpm) - 60.0) / 120.0)), 2)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY,
    uri TEXT NOT NULL,
    name TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    artist TEXT NOT NULL DEFAULT '',
    album TEXT NOT NULL DEFAULT '',
    year INTEGER,
    duration INTEGER,
    favorite INTEGER NOT NULL DEFAULT 0,
    last_played INTEGER NOT NULL DEFAULT 0,
    genres TEXT NOT NULL DEFAULT '[]',
    bpm REAL,
    musical_key TEXT,
    camelot TEXT,
    energy REAL,
    energy_source TEXT,
    analysed INTEGER NOT NULL DEFAULT 0,
    sonic_provider TEXT,
    sonic_item_id TEXT,
    features_at INTEGER,
    seen_at INTEGER NOT NULL DEFAULT 0
);
-- The primary key's index serves the frontier walk (provider = ? AND item_id > ? ORDER BY item_id).
CREATE TABLE IF NOT EXISTS mappings (
    provider TEXT NOT NULL,
    item_id TEXT NOT NULL,
    track_id INTEGER NOT NULL,
    PRIMARY KEY (provider, item_id)
);
CREATE INDEX IF NOT EXISTS idx_mappings_track ON mappings(track_id);
CREATE INDEX IF NOT EXISTS idx_tracks_analysed ON tracks(analysed);
CREATE INDEX IF NOT EXISTS idx_tracks_energy ON tracks(analysed, energy);
CREATE INDEX IF NOT EXISTS idx_tracks_played ON tracks(last_played);
CREATE TABLE IF NOT EXISTS feedback (
    track_id INTEGER PRIMARY KEY,
    plays INTEGER NOT NULL DEFAULT 0,
    skips INTEGER NOT NULL DEFAULT 0,
    last_skip INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Tracks Music Assistant has not reported a tempo or key for yet.
_UNMEASURED = "t.bpm IS NULL AND t.musical_key IS NULL"


def _artist_names(item: dict[str, Any]) -> str:
    return ", ".join(a.get("name", "") for a in item.get("artists") or [] if a.get("name"))


def _genres(item: dict[str, Any]) -> list[str]:
    out = []
    for g in (item.get("metadata") or {}).get("genres") or []:
        name = g.get("name") if isinstance(g, dict) else g
        if name:
            out.append(str(name))
    return out


class _FeaturePass:
    """Book-keeping for one ``sync_features`` run: lookups, hits and the time cap."""

    def __init__(self, lib: Library, ma: MAClient, clock: Callable[[], float], budget: float) -> None:
        self.lib = lib
        self.ma = ma
        self.clock = clock
        self.deadline = clock() + budget
        self.filled = 0
        self.checked = 0
        self.cut = False  # the time cap stopped a step that still had work
        self.new_region = False  # a probe found analysed tracks away from every anchor

    def expired(self) -> bool:
        """Call before each lookup; True (and remembered) once the budget is spent."""
        if self.clock() >= self.deadline:
            self.cut = True
            return True
        return False

    async def lookup(self, tid: int) -> bool:
        self.checked += 1
        hit = await self.lib._fetch_features(self.ma, tid)
        if hit:
            self.filled += 1
        if self.checked % 200 == 0:
            self.lib.db.commit()
        return hit


class Library:
    """SQLite-backed mirror. All methods are synchronous and quick; sync passes are async."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self.db.commit()
        self._stats: tuple[float, dict[str, int]] | None = None

    def _changed(self) -> None:
        """Forget cached counts after a write that can change them."""
        self._stats = None

    def close(self) -> None:
        self.db.close()

    # -- meta ---------------------------------------------------------------

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, str(value)))

    def _json_meta(self, key: str) -> Any:
        try:
            return json.loads(self.get_meta(key, "null"))
        except ValueError:
            return None

    def stats(self) -> dict[str, int]:
        """Counts for the status views.

        ``analysed`` is the DJ's pool: tracks it can pick. ``in_sound_index`` is
        the part of it a vibe can match by sound. ``measured`` have a tempo or
        key from Music Assistant; ``energy_from_tempo`` only have a rough energy
        estimated from their tempo. A full scan, so the result is reused for a
        few seconds unless the mirror changed.
        """
        if self._stats and time.monotonic() - self._stats[0] < STATS_TTL:
            return dict(self._stats[1])
        row = self.db.execute(
            "SELECT COUNT(*) total, COALESCE(SUM(analysed),0) analysed,"
            " COALESCE(SUM(bpm IS NOT NULL OR musical_key IS NOT NULL),0) measured,"
            " COALESCE(SUM(bpm IS NOT NULL),0) with_bpm, COALESCE(SUM(energy IS NOT NULL),0) with_energy,"
            " COALESCE(SUM(energy_source='tempo'),0) energy_from_tempo,"
            " COALESCE(SUM(sonic_item_id IS NOT NULL),0) in_sound_index"
            " FROM tracks"
        ).fetchone()
        self._stats = (time.monotonic(), dict(row))
        return dict(row)

    # -- analysis coverage reported by Music Assistant -----------------------

    def coverage_samples(self, domain: str) -> list[dict[str, int]]:
        samples = self._json_meta(f"coverage:{domain}")
        if not isinstance(samples, list):
            return []
        return [s for s in samples if isinstance(s, dict) and {"t", "analysed", "pending"} <= s.keys()]

    @staticmethod
    def _peak_rate(samples: list[dict[str, int]]) -> float:
        """Quickest pace (tracks/hour) MA itself showed between stored samples."""
        peak = 0.0
        for a, b in zip(samples, samples[1:]):
            dt = b["t"] - a["t"]
            if dt >= COVERAGE_MIN_GAP:
                peak = max(peak, (b["analysed"] - a["analysed"] - b.get("step", 0)) * 3600 / dt)
        return peak

    def record_coverage(self, domain: str, analysed: int, pending: int, now: int | None = None) -> None:
        """Remember one reading of MA's analysed/pending counts (kept across restarts).

        A gain too large for the time since the previous reading is stored as
        that sample's ``step`` (a bulk import), which the rate leaves out.
        """
        now = int(now if now is not None else time.time())
        analysed, pending = int(analysed or 0), int(pending or 0)
        samples = [s for s in self.coverage_samples(domain) if now - COVERAGE_KEEP < s["t"] <= now]
        if samples and analysed < samples[-1]["analysed"] * 0.9 - 10:
            samples = []  # MA's analysis data was reset: earlier readings no longer apply
        step = 0
        if samples:
            prev = samples[-1]
            gain = analysed - prev["analysed"]
            per_hour = gain * 3600 / max(1, now - prev["t"])
            limit = STEP_RATE_FACTOR * max(STEP_FLOOR_RATE, self._peak_rate(samples))
            if gain >= STEP_MIN_GAIN and per_hour > limit:
                step = gain
                _LOGGER.info("Analysis count for %s jumped by %d at once; treating it as an import, "
                             "not as Music Assistant's own pace", domain, gain)
        sample = {"t": now, "analysed": analysed, "pending": pending}
        if len(samples) >= 2 and now - samples[-2]["t"] < COVERAGE_MIN_GAP:
            step += samples[-1].get("step", 0)  # the reading it replaces may hold a step too
            if step:
                sample["step"] = step
            samples[-1] = sample  # keep the newest reading without storing one per poll
        else:
            if step:
                sample["step"] = step
            samples.append(sample)
        self.set_meta(f"coverage:{domain}", json.dumps(samples, separators=(",", ":")))
        self.db.commit()

    def coverage_progress(self, domain: str, now: int | None = None) -> dict[str, Any] | None:
        """Latest MA counts for ``domain`` with a rate and ETA, or ``None`` if never sampled.

        Music Assistant analyses in a nightly job (a few hours from local
        midnight, plus any manual or keep-running restarts), so the rate is the
        average over the last one to three whole days, which always contain the
        same share of nightly runs. With less than a day of samples the rate
        comes from everything recorded, and before ``ETA_MIN_SPAN`` there is no
        rate or ETA at all: ``state`` is ``"measuring"``.

        States: ``measuring`` (not enough history), ``analysing`` (progress
        seen; ``eta_seconds`` set), ``stalled`` (a whole day without
        progress), ``complete`` (nothing pending).
        """
        samples = self.coverage_samples(domain)
        if not samples:
            return None
        now = int(now if now is not None else time.time())
        latest, first = samples[-1], samples[0]
        span = latest["t"] - first["t"]
        out: dict[str, Any] = {
            "analysed": latest["analysed"], "pending": latest["pending"], "sampled_at": latest["t"],
            "measured_hours": round(span / 3600, 1), "state": "measuring",
            "rate_per_hour": None, "eta_seconds": None,
        }
        base = None
        if span >= DAY:
            cutoff = latest["t"] - min(RATE_MAX_DAYS, span // DAY) * DAY
            base = [s for s in samples if s["t"] <= cutoff][-1]
        elif span >= ETA_MIN_SPAN:
            base = first
        if base is not None:
            imported = sum(s.get("step", 0) for s in samples if base["t"] < s["t"] <= latest["t"])
            gained = latest["analysed"] - base["analysed"] - imported
            if gained > 0:
                per_second = gained / (latest["t"] - base["t"])
                out["rate_per_hour"] = round(per_second * 3600, 1)
                out["state"] = "analysing"
                if latest["pending"] > 0:
                    age = max(0, now - latest["t"])
                    out["eta_seconds"] = max(0, int(latest["pending"] / per_second) - age)
            elif span >= DAY:
                out["state"] = "stalled"
        if latest["pending"] <= 0:
            out.update(state="complete", eta_seconds=0)
        return out

    # -- writes -------------------------------------------------------------

    def upsert_tracks(self, items: Iterable[dict[str, Any]], seen_at: int | None = None) -> int:
        """Insert or refresh library tracks from MA track dicts. Features are left untouched."""
        seen_at = seen_at or int(time.time())
        count = 0
        for item in items:
            try:
                tid = int(item["item_id"])
            except (KeyError, TypeError, ValueError):
                continue
            album = item.get("album") or {}
            year = item.get("year") or album.get("year")
            self.db.execute(
                "INSERT INTO tracks(id, uri, name, version, artist, album, year, duration, favorite,"
                " last_played, genres, seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET uri=excluded.uri, name=excluded.name,"
                " version=excluded.version, artist=excluded.artist, album=excluded.album,"
                " year=excluded.year, duration=excluded.duration, favorite=excluded.favorite,"
                " last_played=excluded.last_played, genres=excluded.genres, seen_at=excluded.seen_at",
                (
                    tid,
                    item.get("uri") or f"library://track/{tid}",
                    item.get("name") or "",
                    item.get("version") or "",
                    _artist_names(item),
                    album.get("name") or "",
                    int(year) if year else None,
                    int(item["duration"]) if item.get("duration") else None,
                    1 if item.get("favorite") else 0,
                    int(item.get("last_played") or 0),
                    json.dumps(_genres(item)),
                    seen_at,
                ),
            )
            self.db.execute("DELETE FROM mappings WHERE track_id=?", (tid,))
            self.db.executemany(
                "INSERT OR REPLACE INTO mappings(provider, item_id, track_id) VALUES(?,?,?)",
                [
                    (str(m.get("provider_instance")), str(m.get("item_id")), tid)
                    for m in item.get("provider_mappings") or []
                    if m.get("provider_instance") and m.get("item_id") is not None
                ],
            )
            self._apply_audio_metadata(tid, item.get("audio_metadata"))
            count += 1
        self._changed()
        return count

    def _apply_audio_metadata(self, tid: int, meta: dict[str, Any] | None) -> bool:
        """Store MA's measured tempo/key (and energy level on newer MA). True if it had any."""
        if not meta:
            return False
        try:
            bpm = float(meta.get("bpm") or 0) or None
        except (TypeError, ValueError):
            bpm = None
        key, level = meta.get("musical_key") or None, meta.get("energy_level")
        if not (bpm or key or level):
            return False
        self.db.execute(
            "UPDATE tracks SET bpm=COALESCE(?, bpm), musical_key=COALESCE(?, musical_key),"
            " camelot=COALESCE(?, camelot), analysed=1, features_at=? WHERE id=?",
            (bpm, key, camelot(key), int(time.time()), tid),
        )
        if level:
            # MA's own measurement (2.11+) always wins over our estimates
            self.db.execute(
                "UPDATE tracks SET energy=?, energy_source='ma' WHERE id=?", (float(level), tid)
            )
        elif bpm:
            # no energy from sound yet: estimate one from tempo so the track can be sequenced
            self.db.execute(
                "UPDATE tracks SET energy=?, energy_source='tempo'"
                " WHERE id=? AND (energy IS NULL OR energy_source='tempo')",
                (tempo_energy(bpm), tid),
            )
        self._changed()
        return True

    def prune(self, seen_before: int) -> int:
        """Drop tracks that were not seen in the latest full listing."""
        cur = self.db.execute("DELETE FROM tracks WHERE seen_at < ?", (seen_before,))
        self.db.execute("DELETE FROM mappings WHERE track_id NOT IN (SELECT id FROM tracks)")
        self._changed()
        return cur.rowcount

    # -- reads --------------------------------------------------------------

    def resolve(self, provider: str, item_id: str) -> int | None:
        """Map a (provider, item id) pair from a sonic result to a library track id.

        MA keys analysis of streaming providers by domain ("opensubsonic") but
        files by instance ("filesystem_smb--eDoxPtVW"), so a bare domain also
        matches that domain's instances.
        """
        if provider == "library":
            row = self.db.execute("SELECT id FROM tracks WHERE id=?", (item_id,)).fetchone()
            return row["id"] if row else None
        row = self.db.execute(
            "SELECT track_id FROM mappings WHERE provider=? AND item_id=?", (provider, str(item_id))
        ).fetchone()
        if row is None and "--" not in provider:
            row = self.db.execute(
                "SELECT track_id FROM mappings WHERE provider GLOB ? AND item_id=?",
                (f"{provider}--*", str(item_id)),
            ).fetchone()
        return row["track_id"] if row else None

    def rows(self, ids: Iterable[int]) -> dict[int, sqlite3.Row]:
        ids = list(dict.fromkeys(int(i) for i in ids))
        out: dict[int, sqlite3.Row] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            for row in self.db.execute(f"SELECT * FROM tracks WHERE id IN ({marks})", chunk):  # noqa: S608
                out[row["id"]] = row
        return out

    def candidates(self, relevance: dict[int, float]) -> list[Track]:
        """Build sequencer tracks for the given ids, carrying each one's relevance."""
        rows = self.rows(relevance)
        return [
            Track(
                id=str(tid),
                artist=row["artist"],
                title=row["name"],
                bpm=row["bpm"],
                key=row["camelot"],
                energy=row["energy"],
                relevance=relevance[tid],
            )
            for tid, row in rows.items()
        ]

    def mapping_for(self, track_id: int) -> tuple[str, str] | None:
        row = self.db.execute(
            "SELECT provider, item_id FROM mappings WHERE track_id=? ORDER BY provider", (track_id,)
        ).fetchone()
        return (row["provider"], row["item_id"]) if row else None

    def rediscover(self, limit: int, *, not_played_since: int, analysed_only: bool = True) -> list[int]:
        """Analysed tracks not heard since ``not_played_since`` (epoch), random order."""
        cond = "analysed=1 AND " if analysed_only else ""
        rows = self.db.execute(
            f"SELECT id FROM tracks WHERE {cond}last_played < ? ORDER BY RANDOM() LIMIT ?",  # noqa: S608
            (not_played_since, limit),
        )
        return [r["id"] for r in rows]

    def id_for_uri(self, uri: str | None) -> int | None:
        """Library track id for a ``library://track/<id>`` URI, if it is in the mirror."""
        if not uri or not uri.startswith("library://track/"):
            return None
        tail = uri.rsplit("/", 1)[-1]
        if not tail.isdigit():
            return None
        row = self.db.execute("SELECT id FROM tracks WHERE id=?", (int(tail),)).fetchone()
        return row["id"] if row else None

    def similar_seeds(self, track_ids: Iterable[int]) -> list[tuple[str, str]]:
        """``(provider, item_id)`` pairs to seed a similar-track search, in input order.

        Music Assistant's similar-track search uses its feature ("Traits")
        index, which is built from the analysis rows and keyed by the file's
        provider item id (its relative path), not by the sound (CLAP) index. So
        an analysed file can seed it before the sound index has it. Tracks that
        have not been analysed are dropped.
        """
        ids = list(dict.fromkeys(int(i) for i in track_ids))
        rows = self.rows(ids)
        out: list[tuple[str, str]] = []
        for tid in ids:
            row = rows.get(tid)
            if row is None or not row["analysed"]:
                continue
            if row["sonic_item_id"]:
                out.append((row["sonic_provider"] or "", row["sonic_item_id"]))
                continue
            for m in self.db.execute(
                "SELECT provider, item_id FROM mappings WHERE track_id=? ORDER BY provider", (tid,)
            ):
                if is_scanned_provider(m["provider"]):
                    out.append((m["provider"], m["item_id"]))
                    break
        return out

    def near_energy(self, low: float, high: float, center: float, limit: int) -> dict[int, float]:
        """Analysed tracks outside the sound index with energy in ``[low, high]``.

        Returns ``{track_id: distance from center}`` for a random sample, for
        sets where the sound search cannot reach most of the pool yet.
        """
        rows = self.db.execute(
            "SELECT id, ABS(energy - ?) AS off FROM tracks WHERE analysed=1 AND energy BETWEEN ? AND ?"
            " AND sonic_item_id IS NULL ORDER BY RANDOM() LIMIT ?",
            (center, low, high, limit),
        )
        return {r["id"]: float(r["off"]) for r in rows}

    def random_analysed(self, limit: int) -> list[int]:
        rows = self.db.execute("SELECT id FROM tracks WHERE analysed=1 ORDER BY RANDOM() LIMIT ?", (limit,))
        return [r["id"] for r in rows]

    def search(self, text: str, limit: int = 12) -> list[sqlite3.Row]:
        """Simple title/artist search over the mirror, analysed tracks first."""
        words = [w for w in text.split() if w]
        if not words:
            return []
        cond = " AND ".join("(name LIKE ? OR artist LIKE ?)" for _ in words)
        args: list[Any] = [f"%{w}%" for w in words for _ in (0, 1)]
        return self.db.execute(
            f"SELECT * FROM tracks WHERE {cond} ORDER BY analysed DESC, favorite DESC, name LIMIT ?",  # noqa: S608
            (*args, limit),
        ).fetchall()

    # -- feedback -----------------------------------------------------------

    def record_play(self, track_id: int, *, skipped: bool) -> None:
        """Remember that a track finished or was skipped; feeds later ranking."""
        now = int(time.time())
        self.db.execute(
            "INSERT INTO feedback(track_id, plays, skips, last_skip) VALUES(?,?,?,?)"
            " ON CONFLICT(track_id) DO UPDATE SET plays=plays+excluded.plays,"
            " skips=skips+excluded.skips, last_skip=MAX(last_skip, excluded.last_skip)",
            (track_id, 0 if skipped else 1, 1 if skipped else 0, now if skipped else 0),
        )
        if not skipped:
            self.db.execute("UPDATE tracks SET last_played=? WHERE id=?", (now, track_id))
        self.db.commit()

    def feedback(self, track_id: int) -> dict[str, int] | None:
        """A track's play/skip counts, or ``None`` if it was never played or skipped."""
        row = self.db.execute("SELECT plays, skips, last_skip FROM feedback WHERE track_id=?",
                              (track_id,)).fetchone()
        return dict(row) if row else None

    def restore_feedback(self, track_id: int, saved: dict[str, int] | None) -> None:
        """Put back counts read earlier with :meth:`feedback` (undoing a recorded skip)."""
        if saved is None:
            self.db.execute("DELETE FROM feedback WHERE track_id=?", (track_id,))
        else:
            self.db.execute(
                "INSERT OR REPLACE INTO feedback(track_id, plays, skips, last_skip) VALUES(?,?,?,?)",
                (track_id, saved["plays"], saved["skips"], saved["last_skip"]),
            )
        self.db.commit()

    def skip_penalties(self, ids: Iterable[int]) -> dict[int, float]:
        """Relevance penalty (0..0.6) per track from its skip history.

        A skip counts for less as full plays accumulate and fades over ~60 days.
        """
        ids = list(ids)
        out: dict[int, float] = {}
        now = time.time()
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            for r in self.db.execute(
                f"SELECT * FROM feedback WHERE skips > 0 AND track_id IN ({marks})", chunk  # noqa: S608
            ):
                ratio = r["skips"] / (r["skips"] + r["plays"] + 1)
                fade = max(0.0, 1.0 - (now - r["last_skip"]) / (60 * 86400))
                out[r["track_id"]] = round(0.6 * ratio * fade, 3)
        return out

    # -- sync passes --------------------------------------------------------

    async def sync_listing(self, ma: MAClient) -> dict[str, int]:
        """Mirror the full track listing, then drop tracks that have left the library."""
        started = int(time.time())
        offset = total = 0
        while True:
            page = await ma.cmd("music/tracks/library_items", limit=PAGE, offset=offset, timeout=120)
            if not page:
                break
            total += self.upsert_tracks(page, seen_at=started)
            self.db.commit()
            if len(page) < PAGE:
                break
            offset += PAGE
        removed = self.prune(started) if total else 0
        self.set_meta("listing_synced_at", started)
        self.db.commit()
        _LOGGER.info("Library listing synced: %d tracks, %d removed", total, removed)
        return {"tracks": total, "removed": removed}

    async def sync_analysed(self, ma: MAClient) -> dict[str, int]:
        """Mark tracks in the sound (CLAP) index and derive an energy value (1-10) for each.

        Scores every indexed track against a high-energy and a low-energy phrase
        and ranks the difference across the library, so the scale stays spread
        out whatever the collection sounds like. Skipped for tracks that already
        carry Music Assistant's own energy level; replaces tempo estimates.
        """
        # Refresh MA's in-memory USearch/CLAP indexes from SQLite first so newly
        # analysed tracks are visible even when MA's background task queue is busy.
        await ma.try_cmd("sonic_similarity/rebuild_index", timeout=120)
        try:
            high = await ma.cmd("sonic_similarity/text_search", query=ENERGY_HIGH, limit=1_000_000, timeout=300)
            low = await ma.cmd("sonic_similarity/text_search", query=ENERGY_LOW, limit=1_000_000, timeout=300)
        except MAError as err:
            _LOGGER.warning("Sonic text search unavailable (%s); energy not derived", err.details)
            return {"analysed": 0, "unresolved": 0}
        if not (high or {}).get("analyzed") or not (low or {}).get("analyzed"):
            return {"analysed": 0, "unresolved": 0}
        low_by = {(i["provider"], str(i["item_id"])): float(i["distance"]) for i in low.get("items", [])}
        scores: dict[int, float] = {}
        pairs: dict[int, tuple[str, str]] = {}
        unresolved = 0
        for item in high.get("items", []):
            pair = (item["provider"], str(item["item_id"]))
            if pair not in low_by:
                continue
            tid = self.resolve(*pair)
            if tid is None:
                unresolved += 1
                continue
            # closer to "high" and further from "low" -> larger score
            scores[tid] = low_by[pair] - float(item["distance"])
            pairs[tid] = pair
        ranked = sorted(scores, key=lambda t: (scores[t], t))
        n = len(ranked)
        for pos, tid in enumerate(ranked):
            energy = 5.5 if n == 1 else round(1.0 + 9.0 * pos / (n - 1), 2)
            self.db.execute(
                "UPDATE tracks SET analysed=1, sonic_provider=?, sonic_item_id=?,"
                " energy=CASE WHEN energy_source='ma' THEN energy ELSE ? END,"
                " energy_source=CASE WHEN energy_source='ma' THEN 'ma' ELSE 'derived' END WHERE id=?",
                (*pairs[tid], energy, tid),
            )
        self.set_meta("analysed_synced_at", int(time.time()))
        self.db.commit()
        self._changed()
        _LOGGER.info("Sound index: %d tracks (%d not in mirror)", n, unresolved)
        return {"analysed": n, "unresolved": unresolved}

    async def _fetch_features(self, ma: MAClient, tid: int) -> bool:
        """Look one track up in MA and store its BPM/key if it has been analysed."""
        item = await ma.try_cmd(
            "music/tracks/get", item_id=str(tid), provider_instance_id_or_domain="library",
            # a bulk read: do not make MA refresh online metadata or load the album
            allow_update_metadata=False, recursive=False,
        )
        return bool(item and self._apply_audio_metadata(tid, item.get("audio_metadata")))

    async def _fetch_many(self, run: _FeaturePass, ids: Iterable[int], *, remember_miss: bool) -> None:
        for tid in ids:
            if run.expired():
                return
            if not await run.lookup(tid) and remember_miss:
                # remember the attempt so a track without data is not re-fetched every pass
                self.db.execute("UPDATE tracks SET features_at=? WHERE id=?", (int(time.time()), tid))

    def _scanned_providers(self) -> list[str]:
        rows = self.db.execute("SELECT DISTINCT provider FROM mappings ORDER BY provider").fetchall()
        return [r["provider"] for r in rows if is_scanned_provider(r["provider"])]

    def _is_measured(self, tid: int) -> bool:
        row = self.db.execute(
            f"SELECT 1 FROM tracks t WHERE t.id=? AND NOT ({_UNMEASURED})", (tid,)  # noqa: S608
        ).fetchone()
        return row is not None

    async def _walk(
        self, run: _FeaturePass, provider: str, start: str, *, backward: bool = False
    ) -> tuple[str | None, str]:
        """Check unmeasured files of ``provider`` in path order from ``start``.

        Keeps going while analysed files keep turning up; stops after
        ``FRONTIER_MISS_RUN`` misses in a row, at the end of the list, or when
        the pass's time budget is spent. Returns ``(last hit, last file checked)``.
        """
        op, order = ("<", "DESC") if backward else (">", "ASC")
        cursor, last_hit, misses = start, None, 0
        while True:
            rows = self.db.execute(
                f"SELECT m.item_id, m.track_id FROM mappings m JOIN tracks t ON t.id = m.track_id"  # noqa: S608
                f" WHERE m.provider=? AND m.item_id {op} ? AND {_UNMEASURED}"
                f" ORDER BY m.item_id {order} LIMIT ?",
                (provider, cursor, FRONTIER_PAGE),
            ).fetchall()
            if not rows:
                return last_hit, cursor
            for row in rows:
                if run.expired():
                    return last_hit, cursor
                cursor = row["item_id"]
                if await run.lookup(row["track_id"]):
                    last_hit, misses = cursor, 0
                else:
                    misses += 1
                    if misses >= FRONTIER_MISS_RUN:
                        return last_hit, cursor

    def _anchors(self, provider: str) -> list[dict[str, Any]]:
        data = self._json_meta(f"frontier:{provider}")
        anchors = [a for a in data if isinstance(a, dict) and isinstance(a.get("at"), str)] \
            if isinstance(data, list) else []
        return sorted(anchors, key=lambda a: a["at"]) or [{"at": "", "hit": 0}]

    def _save_anchors(self, provider: str, anchors: list[dict[str, Any]]) -> None:
        anchors = sorted({a["at"]: a for a in anchors}.values(), key=lambda a: a["at"])
        if len(anchors) > 1:
            # the first anchor follows MA's own scan; extra ones expire when idle
            now = int(time.time())
            first, extra = anchors[0], [a for a in anchors[1:] if now - int(a.get("hit") or 0) < ANCHOR_IDLE]
            extra = sorted(extra, key=lambda a: -int(a.get("hit") or 0))[: MAX_ANCHORS - 1]
            anchors = [first, *sorted(extra, key=lambda a: a["at"])]
        self.set_meta(f"frontier:{provider}", json.dumps(anchors))

    async def _walk_anchors(self, run: _FeaturePass, provider: str) -> None:
        """Walk forward from every anchor; a walk that passes the next anchor absorbs it."""
        kept: list[dict[str, Any]] = []
        reach: str | None = None
        for anchor in self._anchors(provider):
            if reach is not None and anchor["at"] < reach:
                continue  # the previous walk already went past this point
            if not run.expired():
                last_hit, reach = await self._walk(run, provider, anchor["at"])
                if last_hit is not None:
                    anchor = {"at": last_hit, "hit": int(time.time())}
            kept.append(anchor)
        self._save_anchors(provider, kept)
        self.db.commit()

    def _probe_sample(self, provider: str, count: int) -> list[sqlite3.Row]:
        """About ``count`` random unmeasured files of ``provider``.

        Seeks from random track ids instead of ``ORDER BY RANDOM()``, which
        would sort the whole provider (~0.7 s for 280k files) on every pass.
        """
        bounds = self.db.execute("SELECT MIN(id) lo, MAX(id) hi FROM tracks").fetchone()
        if bounds["lo"] is None:
            return []
        picked: dict[int, sqlite3.Row] = {}
        for _ in range(count):
            # Each seek looks at no more than PROBE_WINDOW unmeasured tracks, so a provider
            # with few unmeasured files (or none) cannot make it scan the whole table on the
            # event loop. Such providers are still covered by the walks and the sweep.
            # CROSS JOIN pins the join order: seek tracks by id, then look up their mapping.
            row = self.db.execute(
                "SELECT m.item_id, m.track_id FROM"  # noqa: S608
                f" (SELECT t.id FROM tracks t WHERE t.id >= ? AND {_UNMEASURED} ORDER BY t.id LIMIT ?) u"
                " CROSS JOIN mappings m ON m.track_id = u.id WHERE m.provider=? ORDER BY u.id LIMIT 1",
                (random.randint(bounds["lo"], bounds["hi"]), PROBE_WINDOW, provider),  # noqa: S311
            ).fetchone()
            if row is not None:
                picked[row["track_id"]] = row
        return list(picked.values())

    async def _probe(self, run: _FeaturePass, provider: str) -> None:
        """Random lookups over unmeasured files; a hit is walked both ways and anchored."""
        for row in self._probe_sample(provider, PROBE_COUNT):
            if run.expired():
                return
            if self._is_measured(row["track_id"]) or not await run.lookup(row["track_id"]):
                continue  # already found by an earlier probe's walk, or not analysed
            run.new_region = True
            await self._walk(run, provider, row["item_id"], backward=True)
            last_hit, _ = await self._walk(run, provider, row["item_id"])
            self._save_anchors(provider, [*self._anchors(provider),
                                          {"at": last_hit or row["item_id"], "hit": int(time.time())}])
            self.db.commit()

    async def _sweep(self, run: _FeaturePass) -> None:
        """Backstop: a slow rolling pass over every track MA has not measured yet."""
        cursor = int(self.get_meta("sweep_cursor", "0") or 0)
        rows = self.db.execute(
            f"SELECT t.id FROM tracks t WHERE t.id > ? AND t.analysed=0 AND {_UNMEASURED}"  # noqa: S608
            " ORDER BY t.id LIMIT ?",
            (cursor, SWEEP_BUDGET),
        ).fetchall()
        done = 0
        for row in rows:
            if run.expired():
                break
            await run.lookup(row["id"])
            done += 1
            cursor = row["id"]
        self.set_meta("sweep_cursor", cursor if rows and (done < len(rows) or len(rows) == SWEEP_BUDGET) else 0)

    async def sync_features(
        self,
        ma: MAClient,
        limit: int = 2000,
        *,
        expected: int | None = None,
        budget: float = FEATURE_PASS_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any]:
        """Find tracks MA has analysed and store their BPM/key.

        :param expected: MA's own count of analysed tracks, when known. Once the
            mirror holds about that many, the wide searches (probes, sweep) are
            skipped; the anchor walks still run, they are cheap.
        :param budget: wall-clock seconds this pass may spend on lookups.
        :param clock: monotonic clock (injectable for tests).
        :returns: ``filled`` (tracks newly measured), ``checked`` (lookups made),
            ``more`` (stopped by the budget while still finding tracks, or found
            a new region: run again soon), ``caught_up`` and ``seconds``.
        """
        run = _FeaturePass(self, ma, clock, budget)
        started = clock()
        now = int(time.time())
        # 1. sound-index tracks that still lack a tempo (retried daily)
        known = self.db.execute(
            f"SELECT t.id FROM tracks t WHERE t.analysed=1 AND {_UNMEASURED}"  # noqa: S608
            " AND (t.features_at IS NULL OR t.features_at < ?) ORDER BY t.id LIMIT ?",
            (now - KNOWN_RETRY, limit),
        ).fetchall()
        await self._fetch_many(run, [r["id"] for r in known], remember_miss=True)
        # 2. tracks played since they were last checked: MA analyses while it plays
        played = self.db.execute(
            # "+analysed" keeps SQLite on the last_played index: few tracks were ever played
            "SELECT id FROM tracks WHERE last_played > 0 AND +analysed=0"
            " AND last_played > COALESCE(features_at, 0) ORDER BY last_played DESC LIMIT ?",
            (PLAYED_BUDGET,),
        ).fetchall()
        await self._fetch_many(run, [r["id"] for r in played], remember_miss=True)
        # 3. follow the background scan through each filesystem provider
        providers = self._scanned_providers()
        for provider in providers:
            await self._walk_anchors(run, provider)
        # 4. wide searches, only while MA reports analysed tracks the mirror has not found
        measured = self.db.execute(
            f"SELECT COUNT(*) n FROM tracks t WHERE NOT ({_UNMEASURED})"  # noqa: S608
        ).fetchone()["n"]
        caught_up = expected is not None and measured >= expected - max(20, expected * CAUGHT_UP_SLACK)
        if not caught_up:
            for provider in providers:
                await self._probe(run, provider)
            await self._sweep(run)
        self.db.commit()
        result = {
            # run again soon only while it pays: cut short while finding tracks, or a new region
            "filled": run.filled, "checked": run.checked,
            "more": (run.cut and run.filled > 0) or run.new_region,
            "caught_up": caught_up, "seconds": round(clock() - started, 1),
        }
        if run.filled or run.cut:
            _LOGGER.info("Features: %d more tracks have tempo/key (%d lookups, %.0fs%s)", run.filled,
                         run.checked, result["seconds"], ", time cap reached" if run.cut else "")
        return result

    async def sync_all(self, ma: MAClient) -> dict[str, Any]:
        listing = await self.sync_listing(ma)
        analysed = await self.sync_analysed(ma)
        features = await self.sync_features(ma)
        return {**listing, **analysed, "features_filled": features["filled"], **self.stats()}
