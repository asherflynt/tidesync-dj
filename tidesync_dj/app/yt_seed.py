"""Seed the taste profile from a (public) YouTube Music playlist.

This only reads track titles/artists from a public playlist and hands them to
Claude to build the taste-profile summary. It does NOT play YouTube Music —
playback still happens through Tidal via Music Assistant. Private playlists are
not supported (we fetch unauthenticated).
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

_LOGGER = logging.getLogger(__name__)


def parse_playlist_id(value: str) -> str | None:
    """Accept a full YT Music URL or a bare playlist id and return the id.

    Examples that resolve:
      https://music.youtube.com/playlist?list=PLxxxx
      https://www.youtube.com/playlist?list=PLxxxx
      PLxxxx  /  VLPLxxxx  /  RDCLAKxxxx
    """
    value = (value or "").strip()
    if not value:
        return None
    if "://" in value:
        qs = parse_qs(urlparse(value).query)
        if "list" in qs and qs["list"]:
            return qs["list"][0]
        return None
    # Bare id — basic sanity check (YT list ids are alphanumeric/_- and longish).
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", value):
        return value
    return None


def fetch_playlist_tracks(playlist: str, limit: int = 200) -> list[dict[str, Any]]:
    """Return [{name, artist, album}] for a public YT Music playlist.

    Raises ValueError on a bad id and RuntimeError if the playlist can't be
    fetched (e.g. it's private or YT Music is unreachable).
    """
    playlist_id = parse_playlist_id(playlist)
    if not playlist_id:
        raise ValueError("Could not parse a YouTube Music playlist id from input.")

    try:
        from ytmusicapi import YTMusic
    except ImportError as err:  # pragma: no cover - dependency missing
        raise RuntimeError("ytmusicapi is not installed") from err

    try:
        yt = YTMusic()  # unauthenticated — public browsing only
        data = yt.get_playlist(playlist_id, limit=limit)
    except Exception as err:  # noqa: BLE001
        raise RuntimeError(
            f"Could not fetch playlist (is it public?): {err}"
        ) from err

    tracks: list[dict[str, Any]] = []
    for item in data.get("tracks", []) or []:
        if not item:
            continue
        artists = item.get("artists") or []
        artist = ", ".join(a.get("name", "") for a in artists if a) or None
        album = (item.get("album") or {}).get("name") if item.get("album") else None
        name = item.get("title")
        if name:
            tracks.append({"name": name, "artist": artist, "album": album})

    if not tracks:
        raise RuntimeError("Playlist fetched but contained no readable tracks.")
    _LOGGER.info("Fetched %d tracks from YT Music playlist %s", len(tracks), playlist_id)
    return tracks
