"""Session-stats bookkeeping in DJEngine._note_track_change / _reset_session.

Builds a bare engine (object.__new__) wired with only the attributes these two
methods touch, so we don't need the full MA/brain/users/config dependency graph.
"""
import time

import scheduler
from scheduler import DJEngine, IDLE_SESSION_RESET_SECONDS


def _engine():
    e = object.__new__(DJEngine)
    e._current_track_id = None
    e._current_track_started = time.monotonic()
    e._last_current = None
    e._last_active = time.monotonic()
    e.session_started = time.monotonic()
    e.stats = {"tracks_played": 0, "skips": 0, "added": 0, "new_artists": 0}
    e._session_uris = {}
    e._played_uris = {}
    e._played_artists = set()
    return e


def _item(uri, artist=None):
    media = {"uri": uri}
    if artist:
        media["artists"] = [{"name": artist}]
    return {"current_item": media}


def test_vibe_change_starts_a_new_session():
    assert "vibe_change" in scheduler._SESSION_START_REASONS


def test_tracks_played_counts_advances_not_the_opener():
    e = _engine()
    e._note_track_change(_item("tidal://a", "A"))  # opener — not an advance
    assert e.stats["tracks_played"] == 0
    e._note_track_change(_item("tidal://b", "B"))
    e._note_track_change(_item("tidal://b", "B"))  # same track repeated → no count
    e._note_track_change(_item("tidal://c", "C"))
    assert e.stats["tracks_played"] == 2


def test_added_and_artists_count_unique_plays():
    e = _engine()
    for uri, artist in [("tidal://a", "A"), ("tidal://b", "A"), ("tidal://a", "A"), ("tidal://c", "C")]:
        e._note_track_change(_item(uri, artist))
    # 3 distinct URIs heard, 2 distinct artists.
    assert e.stats["added"] == 3
    assert list(e._played_uris) == ["tidal://a", "tidal://b", "tidal://c"]
    assert e.stats["new_artists"] == 2


def test_unplayable_queue_does_not_inflate_stats():
    # Tracks that never become current_item never touch the stats — only plays do.
    e = _engine()
    e._note_track_change(_item("tidal://a", "A"))
    assert e.stats["added"] == 1 and e.stats["new_artists"] == 1


def test_idle_gap_resets_session_before_counting():
    e = _engine()
    # Seed a "previous session" then jump _last_active past the idle threshold.
    e._current_track_id = "tidal://old"
    e.stats["tracks_played"] = 5
    e._played_uris.update({"tidal://old": None, "tidal://old2": None})
    e._last_active = time.monotonic() - (IDLE_SESSION_RESET_SECONDS + 10)

    e._note_track_change(_item("tidal://new", "New"))

    assert e.stats["tracks_played"] == 0          # reset, and opener doesn't advance
    assert list(e._played_uris) == ["tidal://new"]  # cleared then recorded fresh
    assert e.stats["added"] == 1
