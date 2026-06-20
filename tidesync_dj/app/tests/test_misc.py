"""Small pure helpers: time bucketing, host cleaning, playlist-id, provider detect."""
from datetime import datetime

import scheduler
from config import _clean_host
from timectx import month_of_iso, slot_of_iso, time_of_day_slot
from yt_seed import parse_playlist_id


def test_time_of_day_slots():
    assert time_of_day_slot(datetime(2026, 1, 1, 8)) == "morning"
    assert time_of_day_slot(datetime(2026, 1, 1, 13)) == "afternoon"
    assert time_of_day_slot(datetime(2026, 1, 1, 18)) == "evening"
    assert time_of_day_slot(datetime(2026, 1, 1, 22)) == "night"
    assert time_of_day_slot(datetime(2026, 1, 1, 2)) == "late_night"


def test_iso_helpers_are_tolerant():
    assert slot_of_iso("not-a-date") is None
    assert month_of_iso("garbage") is None
    # Mid-month midday is the same calendar month in every timezone.
    assert month_of_iso("2026-03-15T12:00:00+00:00") == 3


def test_clean_host_strips_scheme_path_and_port():
    assert _clean_host("http://192.168.2.6:8095") == "192.168.2.6"
    assert _clean_host("192.168.2.6") == "192.168.2.6"
    assert _clean_host("homeassistant.local/path") == "homeassistant.local"
    assert _clean_host("") == "homeassistant.local"


def test_parse_playlist_id():
    assert parse_playlist_id("https://music.youtube.com/playlist?list=PLabc123xyz") == "PLabc123xyz"
    assert parse_playlist_id("PLabcdef1234") == "PLabcdef1234"
    assert parse_playlist_id("not a playlist") is None
    assert parse_playlist_id("") is None


def test_detect_session_provider():
    assert scheduler._detect_session_provider(["tidal://a", "tidal://b"]) == "tidal"
    assert scheduler._detect_session_provider(["tidal://a", "spotify://b"]) is None
    assert scheduler._detect_session_provider([]) is None
