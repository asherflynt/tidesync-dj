"""Library mirror: upsert, mapping resolution, and the sync passes against a fake MA."""
import pytest

from library import Library
from ma import MAError


def _track(i, **extra):
    return {
        "item_id": str(i), "uri": f"library://track/{i}", "name": f"Song {i}", "version": "",
        "artists": [{"name": f"Artist {i % 3}"}], "album": {"name": "Album", "year": 2001},
        "duration": 200, "favorite": i == 1, "last_played": 0,
        "metadata": {"genres": ["Rock"]},
        "provider_mappings": [{"provider_instance": "sub--1", "item_id": f"s{i}"}],
        **extra,
    }


class FakeMA:
    def __init__(self, tracks, text_search=True):
        self.tracks = {int(t["item_id"]): t for t in tracks}
        self.text_search = text_search
        self.meta = {}
        self.calls = []

    async def cmd(self, command, timeout=60, **args):
        self.calls.append(command)
        if command == "music/tracks/library_items":
            items = list(self.tracks.values())
            return items[args.get("offset", 0): args.get("offset", 0) + args["limit"]]
        if command == "sonic_similarity/rebuild_index":
            return {"status": "rebuilt"}
        if command == "sonic_similarity/text_search":
            if not self.text_search:
                raise MAError(command, 999, "Invalid or unsupported command.")
            high = "high" in args["query"]
            # track 1 is the calmest, track 3 the most energetic; track 99 is not in the mirror
            dist = {"s1": 0.9, "s2": 0.5, "s3": 0.1, "zz": 0.5}
            items = [{"provider": "sub--1", "item_id": k, "distance": d if high else 1 - d}
                     for k, d in dist.items()]
            return {"analyzed": True, "items": items}
        if command == "music/tracks/get":
            return {"audio_metadata": self.meta.get(int(args["item_id"]))}
        raise AssertionError(command)

    async def try_cmd(self, command, default=None, **args):
        try:
            return await self.cmd(command, **args)
        except MAError:
            return default


@pytest.fixture
def lib(tmp_path):
    library = Library(tmp_path / "lib.db")
    yield library
    library.close()


def test_upsert_and_resolve(lib):
    assert lib.upsert_tracks([_track(1), _track(2), {"name": "no id"}]) == 2
    assert lib.resolve("sub--1", "s2") == 2
    assert lib.resolve("library", "1") == 1
    assert lib.resolve("sub--1", "nope") is None
    row = lib.rows([1])[1]
    assert (row["artist"], row["year"], row["favorite"]) == ("Artist 1", 2001, 1)
    assert lib.mapping_for(2) == ("sub--1", "s2")


def test_upsert_keeps_features_and_replaces_mappings(lib):
    lib.upsert_tracks([_track(1, audio_metadata={"bpm": 120.4, "musical_key": "A minor"})])
    moved = _track(1, provider_mappings=[{"provider_instance": "fs--1", "item_id": "a/b.flac"}])
    lib.upsert_tracks([moved])
    row = lib.rows([1])[1]
    assert (round(row["bpm"]), row["camelot"], row["analysed"]) == (120, "8A", 1)
    assert lib.resolve("sub--1", "s1") is None
    assert lib.resolve("fs--1", "a/b.flac") == 1


async def test_sync_listing_pages_and_prunes(lib, monkeypatch):
    monkeypatch.setattr("library.PAGE", 2)
    lib.upsert_tracks([_track(50)], seen_at=1)
    result = await lib.sync_listing(FakeMA([_track(i) for i in range(1, 6)]))
    assert result == {"tracks": 5, "removed": 1}
    assert lib.stats()["total"] == 5


async def test_sync_analysed_ranks_energy(lib):
    lib.upsert_tracks([_track(i) for i in range(1, 5)])
    ma = FakeMA([])
    result = await lib.sync_analysed(ma)
    assert result == {"analysed": 3, "unresolved": 1}
    assert "sonic_similarity/rebuild_index" in ma.calls
    rows = lib.rows([1, 2, 3, 4])
    assert (rows[1]["energy"], rows[2]["energy"], rows[3]["energy"]) == (1.0, 5.5, 10.0)
    assert rows[4]["analysed"] == 0 and rows[4]["energy"] is None


async def test_ma_energy_level_wins_over_derived(lib):
    lib.upsert_tracks([_track(i) for i in range(1, 4)])
    lib.upsert_tracks([_track(3, audio_metadata={"bpm": 100, "musical_key": "C major", "energy_level": 3})])
    await lib.sync_analysed(FakeMA([]))
    row = lib.rows([3])[3]
    assert (row["energy"], row["energy_source"]) == (3.0, "ma")


async def test_sync_analysed_without_text_search_is_a_no_op(lib):
    lib.upsert_tracks([_track(1)])
    assert await lib.sync_analysed(FakeMA([], text_search=False)) == {"analysed": 0, "unresolved": 0}


async def test_sync_features_fills_bpm_once(lib):
    lib.upsert_tracks([_track(1), _track(2)])
    ma = FakeMA([])
    await lib.sync_analysed(ma)
    ma.meta = {1: {"bpm": 128.0, "musical_key": "F# minor"}}
    assert await lib.sync_features(ma) == 1
    assert lib.rows([1])[1]["camelot"] == "11A"
    ma.calls.clear()
    assert await lib.sync_features(ma) == 0
    assert "music/tracks/get" not in ma.calls  # the miss is remembered for a day


def test_candidates_carry_relevance(lib):
    lib.upsert_tracks([_track(1, audio_metadata={"bpm": 90, "musical_key": "C major"})])
    (cand,) = lib.candidates({1: 0.8})
    assert (cand.id, cand.key, cand.relevance, cand.artist) == ("1", "8B", 0.8, "Artist 1")
