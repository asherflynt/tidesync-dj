"""Candidate-grounding: _resolve_and_order picks/orders REAL tracks by id.

Builds a bare DJEngine (object.__new__) wired with stub MA/brain, mirroring
test_stats.py. Sonic features are disabled so _features_for is a no-op and we
don't need an external_ids stub.
"""
import sonic_features
from claude_brain import PoolOrder, PoolPick
from scheduler import DJEngine


def _track(uri, artist, name):
    return {"uri": uri, "name": name, "artists": [{"name": artist}]}


class _FakeMA:
    def __init__(self, pool):
        self._pool = pool
        self.enqueued = None

    async def resolve_queries(self, queries, blocked_uris=None):
        return list(self._pool)

    async def enqueue_tracks(self, tracks, option="add"):
        self.enqueued = tracks
        return tracks


class _FakeBrain:
    def __init__(self, order):
        self._order = order
        self.received = None

    async def order_pool(self, ctx, pool, count):
        self.received = {"ctx": ctx, "pool": pool, "count": count}
        return self._order


def _engine(pool, order, tmp_path):
    e = object.__new__(DJEngine)
    e._ma = _FakeMA(pool)
    e._brain = _FakeBrain(order)
    e._sonic = sonic_features.SonicFeatures(tmp_path, enabled=False)
    return e


async def test_resolve_and_order_selects_and_orders_by_id(tmp_path):
    pool = [
        _track("tidal://a", "A", "Song A"),
        _track("tidal://b", "B", "Song B"),
        _track("tidal://c", "C", "Song C"),
    ]
    order = PoolOrder(
        selection=[PoolPick(id=2, energy=6), PoolPick(id=0, energy=3)],
        dj_note="grounded note",
    )
    e = _engine(pool, order, tmp_path)

    enqueued, energy, note = await e._resolve_and_order(
        context={"taste_profile": "x"}, queries=["q1", "q2", "q3"],
        blocked=set(), play_option="add", count=30,
    )

    # Picked id 2 then id 0, in that order — real tracks from the pool.
    assert [t["uri"] for t in enqueued] == ["tidal://c", "tidal://a"]
    assert energy == {"tidal://c": 6, "tidal://a": 3}
    assert note == "grounded note"
    # The pool handed to Claude carried ids + labels for every resolved track.
    assert e._brain.received["pool"][0] == {"id": 0, "track": "A - Song A"}
    assert e._brain.received["count"] == 30


async def test_resolve_and_order_skips_bad_ids_and_dupes(tmp_path):
    pool = [_track("tidal://a", "A", "Song A"), _track("tidal://b", "B", "Song B")]
    order = PoolOrder(selection=[
        PoolPick(id=99, energy=5),   # out of range — skipped
        PoolPick(id=1, energy=7),
        PoolPick(id=1, energy=4),    # duplicate uri — skipped
    ])
    e = _engine(pool, order, tmp_path)

    enqueued, energy, _ = await e._resolve_and_order(
        context={}, queries=["q"], blocked=set(), play_option="add", count=30,
    )
    assert [t["uri"] for t in enqueued] == ["tidal://b"]
    assert energy == {"tidal://b": 7}


async def test_resolve_and_order_falls_back_to_pool_head_when_empty(tmp_path):
    pool = [_track(f"tidal://{i}", "A", f"S{i}") for i in range(5)]
    e = _engine(pool, PoolOrder(selection=[]), tmp_path)

    enqueued, energy, _ = await e._resolve_and_order(
        context={}, queries=["q"], blocked=set(), play_option="add", count=3,
    )
    # No usable order → keep music flowing with the first `count` pool tracks.
    assert [t["uri"] for t in enqueued] == ["tidal://0", "tidal://1", "tidal://2"]
    assert energy == {}


async def test_resolve_and_order_empty_pool_returns_nothing(tmp_path):
    e = _engine([], PoolOrder(selection=[PoolPick(id=0, energy=5)]), tmp_path)
    enqueued, energy, note = await e._resolve_and_order(
        context={}, queries=["q"], blocked=set(), play_option="add", count=30,
    )
    assert enqueued == [] and energy == {} and note == ""
