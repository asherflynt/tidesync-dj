"""Sonic features: Camelot mapping, adjacency, BPM lookup, and energy_arc.

The lookup tests stub httpx so no network is touched; the energy_arc test builds
a bare DJEngine (object.__new__) wired with only what the helper reads, mirroring
test_stats.py.
"""
import sonic_features
from ma_client import MusicAssistantClient
from scheduler import DJEngine
from sonic_features import (
    SonicFeatures,
    camelot_adjacent,
    tempo_close,
    to_camelot,
)


# --- pure helpers ----------------------------------------------------------
def test_to_camelot_major_minor_and_enharmonic():
    assert to_camelot("A", "minor") == "8A"
    assert to_camelot("C", "major") == "8B"
    assert to_camelot("Bb", "major") == "6B"   # flat normalised to A#
    assert to_camelot("Db", "minor") == "12A"  # flat normalised to C#
    assert to_camelot(None, "minor") is None
    assert to_camelot("H", "major") is None     # not a real note


def test_camelot_adjacent():
    assert camelot_adjacent("8A", "8A")    # identical
    assert camelot_adjacent("8A", "8B")    # relative major/minor
    assert camelot_adjacent("8A", "9A")    # +1 around the wheel
    assert camelot_adjacent("8A", "7A")    # -1
    assert camelot_adjacent("12A", "1A")   # wrap-around 12 -> 1
    assert not camelot_adjacent("8A", "3A")  # far apart
    assert not camelot_adjacent("8A", None)  # missing data


def test_tempo_close():
    assert tempo_close(120, 124)       # within 6%
    assert tempo_close(120, 60)        # double-time
    assert tempo_close(120, 240)       # half-time
    assert not tempo_close(120, 150)   # too far
    assert not tempo_close(None, 120)  # missing data


# --- lookup (stubbed httpx) ------------------------------------------------
class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """Routes GETs by URL fragment; counts calls so we can assert cache hits."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        self.calls += 1
        for fragment, resp in self.routes.items():
            if fragment in url:
                return resp
        return _Resp(404, {})


async def test_lookup_acousticbrainz_via_isrc(tmp_path, monkeypatch):
    fake = _FakeClient({
        "musicbrainz.org": _Resp(200, {"recordings": [{"id": "mbid-123"}]}),
        "/low-level": _Resp(200, {
            "rhythm": {"bpm": 122.4},
            "tonal": {"key_key": "A", "key_scale": "minor"},
        }),
        "/high-level": _Resp(200, {
            "highlevel": {"danceability": {"all": {"danceable": 0.8}}}
        }),
    })
    monkeypatch.setattr(sonic_features.httpx, "AsyncClient", lambda *a, **k: fake)

    sf = SonicFeatures(tmp_path, enabled=True)
    feats = await sf.ensure(isrc="USABC1234567", artist="X", title="Y")

    assert feats == {
        "source": "acousticbrainz",
        "bpm": 122,
        "key": "A",
        "scale": "minor",
        "camelot": "8A",
        "danceability": 0.8,
    }
    # Cached under the ISRC and served without a second network round-trip.
    calls_after_first = fake.calls
    assert sf.get(isrc="USABC1234567") == feats
    assert await sf.ensure(isrc="USABC1234567") == feats
    assert fake.calls == calls_after_first
    assert (tmp_path / "sonic_cache.json").exists()


async def test_lookup_miss_is_cached_as_none(tmp_path, monkeypatch):
    fake = _FakeClient({})  # everything 404s -> no MBID, no features
    monkeypatch.setattr(sonic_features.httpx, "AsyncClient", lambda *a, **k: fake)

    sf = SonicFeatures(tmp_path, enabled=True)
    assert await sf.ensure(isrc="USNOPE0000000", artist="X", title="Y") is None
    calls_after_first = fake.calls
    # Second call is served from the cached miss — no new network call.
    assert await sf.ensure(isrc="USNOPE0000000", artist="X", title="Y") is None
    assert fake.calls == calls_after_first


async def test_disabled_store_never_fetches(tmp_path, monkeypatch):
    def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("disabled store should not hit the network")

    monkeypatch.setattr(sonic_features.httpx, "AsyncClient", _boom)
    sf = SonicFeatures(tmp_path, enabled=False)
    assert await sf.ensure(isrc="USABC1234567") is None


# --- energy_arc (bare engine) ---------------------------------------------
def _item(uri, artist, name, isrc=None):
    media = {"uri": uri, "name": name, "artists": [{"name": artist}]}
    if isrc:
        media["external_ids"] = [["isrc", isrc]]
    return {"media_item": media}


def test_energy_arc_carries_energy_and_features(tmp_path):
    e = object.__new__(DJEngine)
    e._ma = MusicAssistantClient  # external_ids is a staticmethod
    e._track_energy = {"tidal://a": 7, "tidal://b": 4}
    e._sonic = SonicFeatures(tmp_path, enabled=True)
    # Pre-seed the cache (no network) for track b's ISRC.
    e._sonic._cache["isrc:USAAA1111111"] = {"bpm": 128, "camelot": "9A"}

    history = [
        _item("tidal://a", "A", "Song A"),
        _item("tidal://b", "B", "Song B", isrc="USAAA1111111"),
    ]
    current = _item("tidal://c", "C", "Song C")

    arc = e._energy_arc(history, current)

    assert arc[0] == {"track": "A - Song A", "energy": 7}
    assert arc[1] == {"track": "B - Song B", "energy": 4, "bpm": 128, "camelot": "9A"}
    assert arc[2] == {"track": "C - Song C", "now_playing": True}
