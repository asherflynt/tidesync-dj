"""search_track / _rank_track: pick a usable candidate, prefer the provider."""
from ma_client import MusicAssistantClient


def _client(provider="tidal"):
    return MusicAssistantClient("ws://x", preferred_provider=provider)


def _tr(name, uri):
    return {"name": name, "uri": uri}


def test_rank_prefers_configured_provider_over_order():
    c = _client("tidal")
    cands = [_tr("Song", "ytmusic://1"), _tr("Song", "tidal://2")]
    ranked = sorted(enumerate(cands), key=lambda io: c._rank_track(io[1], io[0]))
    assert ranked[0][1]["uri"] == "tidal://2"


def test_rank_keeps_relevance_order_within_same_provider():
    c = _client("tidal")
    # No edition second-guessing: a remaster top hit on the preferred provider stays #1.
    cands = [_tr("Heartbeats (Remastered 2023)", "tidal://307127602"), _tr("Heartbeats", "tidal://111")]
    ranked = sorted(enumerate(cands), key=lambda io: c._rank_track(io[1], io[0]))
    assert ranked[0][1]["uri"] == "tidal://307127602"


async def test_search_track_skips_uriless_candidate():
    c = _client("tidal")

    async def fake_cmd(command, **kw):
        return {"tracks": [{"name": "NoUri"}, _tr("Has", "tidal://9")]}

    c._command = fake_cmd
    best = await c.search_track("anything")
    assert best is not None and (best.get("uri") == "tidal://9")


async def test_search_track_returns_none_when_no_usable_candidates():
    c = _client("tidal")

    async def fake_cmd(command, **kw):
        return {"tracks": [{"name": "NoUri"}, {"name": "AlsoNoUri"}]}

    c._command = fake_cmd
    assert await c.search_track("x") is None


async def test_search_track_prefers_provider_hit():
    c = _client("tidal")

    async def fake_cmd(command, **kw):
        return {"tracks": [_tr("S", "ytmusic://a"), _tr("S", "tidal://b")]}

    c._command = fake_cmd
    best = await c.search_track("s")
    assert best["uri"] == "tidal://b"
