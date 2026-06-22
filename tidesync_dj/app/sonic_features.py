"""Sonic feature lookup (BPM + musical key → Camelot) for queued tracks.

Tidal / Music Assistant don't expose tempo or key, and we never download or
analyse the audio. Instead we look features up by *metadata* from FREE sources
and cache them on disk so any track is fetched at most once:

  * AcousticBrainz (primary): Essentia-derived BPM + key, keyed by a MusicBrainz
    recording id (MBID). No API key. Coverage is strong on catalogue, thin on
    recent releases (it stopped ingesting submissions ~2022).
  * MusicBrainz (resolver): when we only have an ISRC, resolve it to an MBID.
    Rate-limited to ~1 req/s and requires a descriptive User-Agent.
  * GetSongBPM (optional fallback): BPM + key by artist/title. Needs a free API
    key (config) plus an attribution backlink, so it's off unless configured.

Everything here is best-effort and non-fatal: any network/parse error yields no
features and the DJ carries on. Misses are cached as ``None`` so a track with no
data is never re-fetched.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

_LOGGER = logging.getLogger(__name__)

_USER_AGENT = "TideSyncDJ/1.3 (https://github.com/asherflynt/tidesync-dj)"
_TIMEOUT = httpx.Timeout(6.0)
_MB_MIN_INTERVAL = 1.1  # MusicBrainz asks for <= 1 request/second.

# --- Camelot wheel ---------------------------------------------------------
# Normalise flats to sharps so AcousticBrainz keys (either spelling) map cleanly.
_ENHARMONIC = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}
_CAMELOT_MAJOR = {
    "B": "1B", "F#": "2B", "C#": "3B", "G#": "4B", "D#": "5B", "A#": "6B",
    "F": "7B", "C": "8B", "G": "9B", "D": "10B", "A": "11B", "E": "12B",
}
_CAMELOT_MINOR = {
    "G#": "1A", "D#": "2A", "A#": "3A", "F": "4A", "C": "5A", "G": "6A",
    "D": "7A", "A": "8A", "E": "9A", "B": "10A", "F#": "11A", "C#": "12A",
}


def to_camelot(key: str | None, scale: str | None) -> str | None:
    """Map a musical key + scale (e.g. "A", "minor") to a Camelot code ("8A")."""
    if not key:
        return None
    note = key.strip().capitalize()
    note = _ENHARMONIC.get(note, note)
    table = _CAMELOT_MINOR if (scale or "").strip().lower() == "minor" else _CAMELOT_MAJOR
    return table.get(note)


def _parse_camelot(code: str) -> tuple[int, str] | None:
    code = (code or "").strip().upper()
    if len(code) < 2 or code[-1] not in ("A", "B"):
        return None
    try:
        return int(code[:-1]), code[-1]
    except ValueError:
        return None


def camelot_adjacent(a: str | None, b: str | None) -> bool:
    """True if two Camelot codes mix harmonically.

    Compatible = identical, same number (relative major/minor A↔B), or same
    letter with the number ±1 around the 12-hour wheel (1 and 12 are adjacent).
    """
    pa, pb = _parse_camelot(a or ""), _parse_camelot(b or "")
    if not pa or not pb:
        return False
    (na, la), (nb, lb) = pa, pb
    if na == nb and la == lb:
        return True
    if na == nb:  # relative major/minor
        return True
    if la == lb:
        diff = abs(na - nb)
        return diff in (1, 11)  # 11 == wrap-around (12↔1)
    return False


def tempo_close(a: float | None, b: float | None, tol: float = 0.06) -> bool:
    """True if two BPMs are within ``tol`` (default ±6%), allowing half/double-time."""
    if not a or not b:
        return False
    for cand in (b, b * 2, b / 2):
        if abs(a - cand) <= a * tol:
            return True
    return False


# --- Feature store ---------------------------------------------------------
class SonicFeatures:
    """Disk-cached BPM/key lookups, keyed by ISRC (falling back to MBID/name)."""

    def __init__(
        self,
        data_dir: Path,
        *,
        enabled: bool = True,
        getsongbpm_key: str = "",
    ) -> None:
        self._enabled = enabled
        self._gsb_key = (getsongbpm_key or "").strip()
        self._path = Path(data_dir) / "sonic_cache.json"
        self._cache: dict[str, dict[str, Any] | None] = self._load()
        self._write_lock = asyncio.Lock()
        self._mb_last = 0.0

    @property
    def enabled(self) -> bool:
        return self._enabled

    # -- cache io ----------------------------------------------------------
    def _load(self) -> dict[str, dict[str, Any] | None]:
        try:
            if self._path.exists():
                with self._path.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    if isinstance(data, dict):
                        return data
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not read sonic cache %s: %s", self._path, err)
        return {}

    async def _persist(self) -> None:
        async with self._write_lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(".json.tmp")
                with tmp.open("w", encoding="utf-8") as fh:
                    json.dump(self._cache, fh)
                tmp.replace(self._path)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Could not write sonic cache: %s", err)

    @staticmethod
    def _keys(isrc: str | None, mbid: str | None, artist: str, title: str) -> list[str]:
        keys: list[str] = []
        if isrc:
            keys.append(f"isrc:{isrc.upper()}")
        if mbid:
            keys.append(f"mbid:{mbid}")
        if artist and title:
            keys.append(f"name:{artist.lower()}|{title.lower()}")
        return keys

    # -- public ------------------------------------------------------------
    def get(
        self,
        isrc: str | None = None,
        mbid: str | None = None,
        artist: str = "",
        title: str = "",
    ) -> dict[str, Any] | None:
        """Cache-only read (no network) for hot paths like build_context."""
        for key in self._keys(isrc, mbid, artist, title):
            if key in self._cache:
                return self._cache[key]
        return None

    async def ensure(
        self,
        isrc: str | None = None,
        mbid: str | None = None,
        artist: str = "",
        title: str = "",
    ) -> dict[str, Any] | None:
        """Return features, fetching + caching on a miss. Always non-fatal."""
        if not self._enabled:
            return None
        keys = self._keys(isrc, mbid, artist, title)
        if not keys:
            return None
        for key in keys:
            if key in self._cache:  # hit (including a cached miss == None)
                return self._cache[key]

        features = await self._fetch(isrc, mbid, artist, title)
        for key in keys:  # cache under every identifier we know (miss too)
            self._cache[key] = features
        await self._persist()
        return features

    # -- fetchers ----------------------------------------------------------
    async def _fetch(
        self, isrc: str | None, mbid: str | None, artist: str, title: str
    ) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT}
            ) as client:
                rmbid = mbid or await self._mbid_from_isrc(client, isrc)
                if rmbid:
                    feats = await self._acousticbrainz(client, rmbid)
                    if feats:
                        return feats
                if self._gsb_key and artist and title:
                    return await self._getsongbpm(client, artist, title)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Sonic lookup failed (%s - %s): %s", artist, title, err)
        return None

    async def _mbid_from_isrc(
        self, client: httpx.AsyncClient, isrc: str | None
    ) -> str | None:
        if not isrc:
            return None
        # Respect MusicBrainz's ~1 req/s courtesy limit.
        wait = _MB_MIN_INTERVAL - (time.monotonic() - self._mb_last)
        if wait > 0:
            await asyncio.sleep(wait)
        self._mb_last = time.monotonic()
        resp = await client.get(
            "https://musicbrainz.org/ws/2/recording",
            params={"query": f"isrc:{isrc}", "fmt": "json", "limit": 1},
        )
        if resp.status_code != 200:
            return None
        recs = resp.json().get("recordings") or []
        return recs[0].get("id") if recs else None

    async def _acousticbrainz(
        self, client: httpx.AsyncClient, mbid: str
    ) -> dict[str, Any] | None:
        resp = await client.get(
            f"https://acousticbrainz.org/api/v1/{mbid}/low-level"
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        rhythm = data.get("rhythm") or {}
        tonal = data.get("tonal") or {}
        bpm = rhythm.get("bpm")
        key = tonal.get("key_key")
        scale = tonal.get("key_scale")
        if not bpm and not key:
            return None
        feats: dict[str, Any] = {"source": "acousticbrainz"}
        if bpm:
            feats["bpm"] = round(float(bpm))
        if key:
            feats["key"] = key
            feats["scale"] = scale
            feats["camelot"] = to_camelot(key, scale)
        # Best-effort danceability/mood from the high-level endpoint.
        try:
            hi = await client.get(
                f"https://acousticbrainz.org/api/v1/{mbid}/high-level"
            )
            if hi.status_code == 200:
                hl = (hi.json().get("highlevel") or {})
                dance = (hl.get("danceability") or {}).get("all", {}).get("danceable")
                if dance is not None:
                    feats["danceability"] = round(float(dance), 2)
        except Exception:  # noqa: BLE001
            pass
        return feats

    async def _getsongbpm(
        self, client: httpx.AsyncClient, artist: str, title: str
    ) -> dict[str, Any] | None:
        resp = await client.get(
            "https://api.getsong.co/search/",
            params={
                "api_key": self._gsb_key,
                "type": "both",
                "lookup": f"song:{title} artist:{artist}",
            },
        )
        if resp.status_code != 200:
            return None
        results = resp.json().get("search") or []
        if not isinstance(results, list) or not results:
            return None
        first = results[0]
        bpm = first.get("tempo")
        key = first.get("key_of")
        if not bpm and not key:
            return None
        feats: dict[str, Any] = {"source": "getsongbpm"}
        if bpm:
            try:
                feats["bpm"] = round(float(bpm))
            except (TypeError, ValueError):
                pass
        if key:
            feats["camelot"] = key  # GetSongBPM already returns Camelot codes.
        return feats
