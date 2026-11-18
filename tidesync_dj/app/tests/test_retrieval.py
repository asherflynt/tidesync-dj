"""Retrieval reaches the whole analysed pool, not just the sound (CLAP) index."""
import pytest

from library import Library
from plan import Plan
from retrieval import gather

FS = "filesystem_smb--eDoxPtVW"
INDEXED = range(1, 11)  # the only tracks the sound index holds so far


def _track(i):
    return {
        "item_id": str(i), "uri": f"library://track/{i}", "name": f"Song {i}",
        "artists": [{"name": f"Artist {i % 9}"}], "album": {"name": "Album"}, "duration": 200,
        "provider_mappings": [{"provider_instance": FS, "item_id": f"Music/{i:03d}.flac"}],
        # tempo spread 66..186 BPM -> tempo-based energy across the scale
        "audio_metadata": {"bpm": 60.0 + (i * 13) % 126, "musical_key": "C major"},
    }


class SoundMA:
    """Sound index with 10 tracks; the similar-track index knows every analysed file."""

    def __init__(self):
        self.calls = []

    async def cmd(self, command, timeout=60, **args):
        args = {k: v for k, v in args.items() if v is not None}
        self.calls.append((command, args))
        if command == "sonic_similarity/rebuild_index":
            return {"status": "rebuilt", "index_size": len(INDEXED)}
        if command == "sonic_similarity/text_search":
            items = [{"provider": FS, "item_id": f"Music/{i:03d}.flac", "distance": i / 20} for i in INDEXED]
            return {"analyzed": True, "items": items[: args["limit"]]}
        if command == "sonic_similarity/similar":
            seeds = {int(s.split("/")[1][:3]) for s in args["item_ids"]}
            near = [i for i in range(1, 101) if i not in seeds and min(abs(i - s) for s in seeds) <= 15]
            if not seeds & set(range(1, 101)):
                return {"analyzed": False, "reason": "seed_not_in_index", "items": []}
            return {"analyzed": True, "items": [{"provider": FS, "item_id": f"Music/{i:03d}.flac",
                                                 "distance": 0.2} for i in near[: args["limit"]]]}
        raise AssertionError(command)

    async def try_cmd(self, command, default=None, **args):
        return await self.cmd(command, **args)

    def similar_calls(self):
        return [a for c, a in self.calls if c == "sonic_similarity/similar"]


@pytest.fixture
async def pool(tmp_path):
    lib = Library(tmp_path / "lib.db")
    lib.upsert_tracks([_track(i) for i in range(1, 101)])
    lib.upsert_tracks([{**_track(200), "audio_metadata": None}])  # not analysed yet
    ma = SoundMA()
    await lib.sync_analysed(ma)  # marks the 10 indexed tracks
    ma.calls.clear()
    yield lib, ma
    lib.close()


async def test_vibe_reaches_analysed_tracks_outside_the_sound_index(pool, monkeypatch):
    monkeypatch.setattr("retrieval.REDISCOVER_LIMIT", 0)
    lib, ma = pool
    assert lib.stats()["analysed"] == 100 and lib.stats()["in_sound_index"] == 10
    plan = Plan(vibe="warm jazz", queries=["warm jazz"], base_energy=5.5, span=1.0)
    scores, src = await gather(ma, lib, plan, targets=[5.0, 5.5, 6.0])
    assert src.text == 10 and src.vibe_neighbours > 0 and src.energy > 0
    outside = set(scores) - set(INDEXED)
    assert outside and 200 not in scores  # unanalysed tracks are never candidates
    # neighbours of the best sound matches were looked up in the similar-track index
    (call,) = ma.similar_calls()
    assert call["item_ids"][:3] == ["Music/001.flac", "Music/002.flac", "Music/003.flac"]
    assert call["seed_provider"] == FS and call["blend_mode"] == "union" and call["limit"] == 100
    # energy top-up: only tracks near the set's energy, ranked below the sound matches
    rows = lib.rows(scores)
    energy_only = outside - set(range(1, 19))  # tracks 4-18 are neighbours of the seeds 1-3
    assert len(energy_only) == src.energy
    assert energy_only and all(4.0 <= rows[t]["energy"] <= 7.0 for t in energy_only)
    assert min(scores[t] for t in INDEXED) > max(scores[t] for t in energy_only)
    assert any("10 of the 100 ready tracks" in n for n in src.notes)


async def test_seed_outside_the_sound_index_still_finds_neighbours(pool):
    lib, ma = pool
    scores, src = await gather(ma, lib, Plan(), seed_ids=[50])
    (call,) = ma.similar_calls()
    assert call["item_ids"] == ["Music/050.flac"] and call["seed_provider"] == FS
    assert src.similar > 0 and 45 in scores and scores[45] > 0.5


async def test_unanalysed_seed_is_explained(pool):
    lib, ma = pool
    _, src = await gather(ma, lib, Plan(), seed_ids=[200])
    assert not ma.similar_calls()
    assert any("not been analysed yet" in n for n in src.notes)


async def test_no_energy_top_up_when_the_sound_index_has_everything(pool):
    lib, ma = pool
    lib.db.execute("UPDATE tracks SET sonic_item_id='x', sonic_provider=? WHERE analysed=1", (FS,))
    _, src = await gather(ma, lib, Plan(queries=["anything"]))
    assert src.energy == 0 and not any("matched by sound" in n for n in src.notes)
