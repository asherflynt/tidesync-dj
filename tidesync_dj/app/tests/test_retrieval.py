"""Retrieval: sound neighbours lead, Traits is a fallback, filler never dilutes a mix."""
import pytest

from library import Library
from ma import MAError
from plan import Plan
from retrieval import SAME_RECORDING, gather, rank_score

FS = "filesystem_smb--eDoxPtVW"


def _path(i):
    return f"Music/{i:03d}.flac"


def _track(i, artist=None):
    return {
        "item_id": str(i), "uri": f"library://track/{i}", "name": f"Song {i}",
        "artists": [{"name": artist or f"Artist {i % 9}"}], "album": {"name": "Album"}, "duration": 200,
        "provider_mappings": [{"provider_instance": FS, "item_id": _path(i)}],
        # tempo spread 66..186 BPM -> tempo-based energy across the scale
        "audio_metadata": {"bpm": 60.0 + (i * 13) % 126, "musical_key": "C major"},
    }


class SoundMA:
    """Tracks 1-100 are analysed; ``sound`` says which of them the sound (CLAP) index holds.

    Sound neighbours are the indexed tracks nearest by number (closer number = more alike).
    The Traits index knows every analysed file and answers with tracks within 15.
    """

    def __init__(self, sound=range(1, 101), clap=True, duplicates=None):
        self.sound = set(sound)
        self.clap = clap
        self.duplicates = duplicates or {}  # seed -> a track that is the same recording
        self.calls = []

    async def cmd(self, command, timeout=60, **args):
        args = {k: v for k, v in args.items() if v is not None}
        self.calls.append((command, args))
        if command == "sonic_similarity/rebuild_index":
            return {"status": "rebuilt", "index_size": len(self.sound)}
        if command == "sonic_similarity/text_search":
            items = [{"provider": FS, "item_id": _path(i), "distance": i / 200} for i in sorted(self.sound)]
            return {"analyzed": True, "items": items[: args["limit"]]}
        if command == "sonic_similarity/similar_clap":
            if not self.clap:
                raise MAError(command, 12, "Invalid command: sonic_similarity/similar_clap")
            seed = int(args["item_id"].split("/")[1][:3])
            if seed not in self.sound:
                return {"analyzed": False, "reason": "seed_not_in_index", "items": []}
            near = sorted((i for i in self.sound if i != seed), key=lambda i: (abs(i - seed), i))
            items = [{"provider": FS, "item_id": _path(i), "distance": 0.05 + abs(i - seed) / 200}
                     for i in near]
            if seed in self.duplicates:
                items.insert(0, {"provider": FS, "item_id": _path(self.duplicates[seed]),
                                 "distance": SAME_RECORDING / 2})
            return {"analyzed": True, "items": items[: args["limit"]]}
        if command == "sonic_similarity/similar":
            seeds = {int(s.split("/")[1][:3]) for s in args["item_ids"]}
            near = [i for i in range(1, 101) if i not in seeds and min(abs(i - s) for s in seeds) <= 15]
            if not seeds & set(range(1, 101)):
                return {"analyzed": False, "reason": "seed_not_in_index", "items": []}
            return {"analyzed": True, "items": [{"provider": FS, "item_id": _path(i),
                                                 "distance": 0.2} for i in near[: args["limit"]]]}
        raise AssertionError(command)

    async def try_cmd(self, command, default=None, **args):
        return await self.cmd(command, **args)

    def args_of(self, command):
        return [a for c, a in self.calls if c == command]


async def _library(tmp_path, ma, extra=()):
    lib = Library(tmp_path / "lib.db")
    lib.upsert_tracks([_track(i) for i in range(1, 101)])
    lib.upsert_tracks([{**_track(200), "audio_metadata": None}])  # not analysed yet
    lib.upsert_tracks(list(extra))
    await lib.sync_analysed(ma)  # marks the tracks the sound index holds
    ma.calls.clear()
    return lib


@pytest.fixture
async def pool(tmp_path):
    """A sound index that holds only tracks 1-10 (it lags the analysis)."""
    ma = SoundMA(sound=range(1, 11))
    lib = await _library(tmp_path, ma)
    yield lib, ma
    lib.close()


@pytest.fixture
async def full(tmp_path):
    """Every analysed track is in the sound index."""
    ma = SoundMA()
    lib = await _library(tmp_path, ma)
    yield lib, ma
    lib.close()


def test_rank_score_declines_gently():
    assert rank_score(1) == 1.0
    assert 0.49 < rank_score(40) < 0.52
    assert rank_score(1) > rank_score(2) > rank_score(300) > 0.1


async def test_vibe_reaches_analysed_tracks_outside_the_sound_index(pool, monkeypatch):
    monkeypatch.setattr("retrieval.REDISCOVER_LIMIT", 0)
    lib, ma = pool
    assert lib.stats()["analysed"] == 100 and lib.stats()["in_sound_index"] == 10
    plan = Plan(vibe="warm jazz", queries=["warm jazz"], base_energy=5.5, span=1.0)
    scores, src = await gather(ma, lib, plan, targets=[5.0, 5.5, 6.0])
    assert src.text == 10 and src.vibe_neighbours > 0 and src.energy > 0
    outside = set(scores) - set(range(1, 11))
    assert outside and 200 not in scores  # unanalysed tracks are never candidates
    # the best sound matches were each asked for songs that sound like them
    calls = ma.args_of("sonic_similarity/similar_clap")
    assert [c["item_id"] for c in calls] == [_path(1), _path(2), _path(3)]
    assert all(c["seed_provider"] == FS for c in calls)
    assert not ma.args_of("sonic_similarity/similar")  # no Traits needed
    # energy top-up: only tracks near the set's energy, ranked below the sound matches
    rows = lib.rows(scores)
    assert len(outside) == src.energy
    assert all(4.0 <= rows[t]["energy"] <= 7.0 for t in outside)
    assert min(scores[t] for t in range(1, 11)) > max(scores[t] for t in outside)
    assert any("10 of the 100 ready tracks" in n for n in src.notes)


async def test_a_mix_from_a_song_is_only_sound_neighbours(full):
    lib, ma = full
    scores, src = await gather(ma, lib, Plan(), seed_ids=[50], targets=[5.0] * 8)
    (call,) = ma.args_of("sonic_similarity/similar_clap")
    assert call["item_id"] == _path(50) and call["seed_provider"] == FS
    assert src.similar == 99 and src.traits == 0 and not ma.args_of("sonic_similarity/similar")
    # no random filler between close matches
    assert src.energy == 0 and src.rediscover == 0 and src.fallback == 0
    assert 50 not in scores
    # closer in sound ranks higher; the nearest is the best match
    assert scores[49] == 1.0 and scores[51] > 0.95  # equally close: the tie goes by id
    assert scores[51] > scores[60] > scores[90]


async def test_recent_plays_steer_while_the_seed_leads(full):
    lib, ma = full
    alone, _ = await gather(ma, lib, Plan(), seed_ids=[50])
    steered, _ = await gather(ma, lib, Plan(), seed_ids=[50], recent_ids=[70])
    # songs between the seed and what just played gain on songs on the far side of the seed
    assert steered[60] / steered[40] > alone[60] / alone[40]
    # but the seed still leads: its close neighbours beat the recent song's close neighbours
    assert steered[51] > steered[71]


async def test_an_early_skip_pushes_down_songs_that_sound_like_it(full):
    lib, ma = full
    before, _ = await gather(ma, lib, Plan(), seed_ids=[50])
    after, src = await gather(ma, lib, Plan(), seed_ids=[50], avoid_ids=[56])
    assert src.avoided > 0
    assert after[57] < before[57] * 0.6 and after[50 - 25] == before[50 - 25]
    assert after[44] > after[57]  # the far side of the seed now beats the skip's side


async def test_the_same_recording_is_never_a_candidate(tmp_path):
    ma = SoundMA(duplicates={50: 90})
    lib = await _library(tmp_path, ma)
    scores, _ = await gather(ma, lib, Plan(), seed_ids=[50])
    assert 90 not in scores and 51 in scores
    lib.close()


async def test_a_used_up_mix_looks_further_before_adding_filler(full, monkeypatch):
    monkeypatch.setattr("retrieval.SOUND_LIMIT", 20)
    monkeypatch.setattr("retrieval.WIDE_SOUND_LIMIT", 60)
    lib, ma = full
    used = set(range(30, 71))  # the closest songs were all played this session
    scores, src = await gather(ma, lib, Plan(), seed_ids=[50], exclude_ids=used, targets=[5.0] * 4)
    assert src.widened and [c["limit"] for c in ma.args_of("sonic_similarity/similar_clap")] == [20, 60]
    assert not used & set(scores) and len(scores) >= 16
    assert src.energy == 0  # widening was enough


async def test_seed_outside_the_sound_index_falls_back_to_traits(pool):
    lib, ma = pool
    scores, src = await gather(ma, lib, Plan(), seed_ids=[50])
    (traits,) = ma.args_of("sonic_similarity/similar")
    assert traits["item_ids"] == [_path(50)] and traits["seed_provider"] == FS
    assert src.similar > 0 and src.traits > 0 and 45 in scores and scores[45] > 0.5


async def test_without_the_sound_neighbour_command_traits_is_used(tmp_path):
    ma = SoundMA(clap=False)
    lib = await _library(tmp_path, ma)
    scores, src = await gather(ma, lib, Plan(), seed_ids=[50], recent_ids=[20])
    # asked once, then MA's answer (unknown command) is remembered for the other anchor
    assert len(ma.args_of("sonic_similarity/similar_clap")) == 1
    assert len(ma.args_of("sonic_similarity/similar")) == 2
    assert src.traits > 0 and scores[51] > 0.5
    lib.close()


async def test_unanalysed_seed_follows_the_artists_analysed_songs(tmp_path):
    ma = SoundMA()
    lib = await _library(tmp_path, ma, extra=[{**_track(300, artist="Artist 2"), "audio_metadata": None}])
    scores, src = await gather(ma, lib, Plan(), seed_ids=[300])
    asked = {c["item_id"] for c in ma.args_of("sonic_similarity/similar_clap")}
    assert asked and asked <= {_path(i) for i in range(1, 101) if i % 9 == 2}
    assert scores and any("other songs by the same artist" in n for n in src.notes)
    lib.close()


async def test_unanalysed_seed_without_stand_ins_is_explained(full):
    lib, ma = full
    _, src = await gather(ma, lib, Plan(), seed_ids=[200])  # Artist 2 ... but 200 % 9 == 2
    lib.db.execute("UPDATE tracks SET artist='Nobody Else' WHERE id=200")
    ma.calls.clear()
    _, src = await gather(ma, lib, Plan(), seed_ids=[200])
    assert not ma.args_of("sonic_similarity/similar_clap")
    assert any("not been analysed yet" in n for n in src.notes)


async def test_no_energy_top_up_when_the_sound_index_has_everything(full):
    lib, ma = full
    _, src = await gather(ma, lib, Plan(queries=["anything"]))
    assert src.energy == 0 and not any("matched by sound" in n for n in src.notes)
    assert src.rediscover == 0  # a typed vibe gets no random rediscoveries either
