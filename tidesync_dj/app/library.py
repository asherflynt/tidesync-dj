"""Local mirror of the Music Assistant library (v2).

The DJ only ever picks tracks that exist in this mirror, so every candidate is
a real, playable library track with a known URI. The mirror also holds the
measured features the sequencer needs (BPM, key, energy) and remembers which
tracks Music Assistant has analysed so far.

Sync runs in three independent passes, each safe to repeat:

1. ``sync_listing``   - page through the library (fast: ~500 tracks per call).
2. ``sync_analysed``  - ask the sonic index which tracks are analysed and
                        derive an energy value for each.
3. ``sync_features``  - fetch BPM/key (and MA's own energy level on 2.11+)
                        for analysed tracks that do not have them yet.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from ma import MAClient, MAError
from sequencer import Track, camelot

_LOGGER = logging.getLogger("tidesync.library")

PAGE = 500
# Phrases scored against every analysed track to place it on an energy scale.
ENERGY_HIGH = "high energy, loud, intense, driving, fast"
ENERGY_LOW = "calm, quiet, soft, gentle, slow"

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
CREATE TABLE IF NOT EXISTS mappings (
    provider TEXT NOT NULL,
    item_id TEXT NOT NULL,
    track_id INTEGER NOT NULL,
    PRIMARY KEY (provider, item_id)
);
CREATE INDEX IF NOT EXISTS idx_mappings_track ON mappings(track_id);
CREATE INDEX IF NOT EXISTS idx_tracks_analysed ON tracks(analysed);
CREATE TABLE IF NOT EXISTS feedback (
    track_id INTEGER PRIMARY KEY,
    plays INTEGER NOT NULL DEFAULT 0,
    skips INTEGER NOT NULL DEFAULT 0,
    last_skip INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _artist_names(item: dict[str, Any]) -> str:
    return ", ".join(a.get("name", "") for a in item.get("artists") or [] if a.get("name"))


def _genres(item: dict[str, Any]) -> list[str]:
    out = []
    for g in (item.get("metadata") or {}).get("genres") or []:
        name = g.get("name") if isinstance(g, dict) else g
        if name:
            out.append(str(name))
    return out


class Library:
    """SQLite-backed mirror. All methods are synchronous and quick; sync passes are async."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # -- meta ---------------------------------------------------------------

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, str(value)))

    def stats(self) -> dict[str, int]:
        row = self.db.execute(
            "SELECT COUNT(*) total, COALESCE(SUM(analysed),0) analysed,"
            " COALESCE(SUM(bpm IS NOT NULL),0) with_bpm, COALESCE(SUM(energy IS NOT NULL),0) with_energy"
            " FROM tracks"
        ).fetchone()
        return dict(row)

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
        return count

    def _apply_audio_metadata(self, tid: int, meta: dict[str, Any] | None) -> bool:
        if not meta:
            return False
        bpm, key, level = meta.get("bpm"), meta.get("musical_key"), meta.get("energy_level")
        if bpm or key:
            self.db.execute(
                "UPDATE tracks SET bpm=COALESCE(?, bpm), musical_key=COALESCE(?, musical_key),"
                " camelot=COALESCE(?, camelot), analysed=1, features_at=? WHERE id=?",
                (float(bpm) if bpm else None, key, camelot(key), int(time.time()), tid),
            )
        if level:
            # MA's own measurement (2.11+) always wins over our derived estimate
            self.db.execute(
                "UPDATE tracks SET energy=?, energy_source='ma' WHERE id=?", (float(level), tid)
            )
        return bool(bpm or key or level)

    def prune(self, seen_before: int) -> int:
        """Drop tracks that were not seen in the latest full listing."""
        cur = self.db.execute("DELETE FROM tracks WHERE seen_at < ?", (seen_before,))
        self.db.execute("DELETE FROM mappings WHERE track_id NOT IN (SELECT id FROM tracks)")
        return cur.rowcount

    # -- reads --------------------------------------------------------------

    def resolve(self, provider: str, item_id: str) -> int | None:
        """Map a (provider, item id) pair from a sonic result to a library track id."""
        if provider == "library":
            row = self.db.execute("SELECT id FROM tracks WHERE id=?", (item_id,)).fetchone()
            return row["id"] if row else None
        row = self.db.execute(
            "SELECT track_id FROM mappings WHERE provider=? AND item_id=?", (provider, str(item_id))
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

    def sonic_seeds(self, track_ids: Iterable[int]) -> list[str]:
        """Sonic-index item ids for the given tracks; unanalysed tracks are dropped."""
        return [r["sonic_item_id"] for r in self.rows(track_ids).values() if r["sonic_item_id"]]

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
        """Mark analysed tracks and derive an energy value (1-10) for each.

        Scores every indexed track against a high-energy and a low-energy phrase
        and ranks the difference across the library, so the scale stays spread
        out whatever the collection sounds like. Skipped for tracks that already
        carry Music Assistant's own energy level.
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
        _LOGGER.info("Sonic index: %d analysed tracks (%d not in mirror)", n, unresolved)
        return {"analysed": n, "unresolved": unresolved}

    async def sync_features(self, ma: MAClient, limit: int = 2000) -> int:
        """Fetch BPM/key for analysed tracks that lack them. Returns how many were filled."""
        rows = self.db.execute(
            "SELECT id FROM tracks WHERE analysed=1 AND bpm IS NULL"
            " AND (features_at IS NULL OR features_at < ?) ORDER BY id LIMIT ?",
            (int(time.time()) - 86400, limit),
        ).fetchall()
        filled = 0
        for n, row in enumerate(rows, 1):
            tid = row["id"]
            item = await ma.try_cmd(
                "music/tracks/get", item_id=str(tid), provider_instance_id_or_domain="library"
            )
            if item and self._apply_audio_metadata(tid, item.get("audio_metadata")):
                filled += 1
            else:
                # remember the attempt so a track without tempo data is not re-fetched every pass
                self.db.execute("UPDATE tracks SET features_at=? WHERE id=?", (int(time.time()), tid))
            if n % 200 == 0:
                self.db.commit()
        self.db.commit()
        return filled

    async def sync_all(self, ma: MAClient) -> dict[str, Any]:
        listing = await self.sync_listing(ma)
        analysed = await self.sync_analysed(ma)
        features = await self.sync_features(ma)
        return {**listing, **analysed, "features_filled": features, **self.stats()}
