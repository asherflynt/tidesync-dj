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
    assert (await lib.sync_features(ma))["filled"] == 1
    assert lib.rows([1])[1]["camelot"] == "11A"
    ma.calls.clear()
    assert (await lib.sync_features(ma))["filled"] == 0
    assert "music/tracks/get" not in ma.calls  # the miss is remembered for a day


def test_candidates_carry_relevance(lib):
    lib.upsert_tracks([_track(1, audio_metadata={"bpm": 90, "musical_key": "C major"})])
    (cand,) = lib.candidates({1: 0.8})
    assert (cand.id, cand.key, cand.relevance, cand.artist) == ("1", "8B", 0.8, "Artist 1")


# -- finding analysed tracks without the sound index -------------------------

FS = "filesystem_smb--eDoxPtVW"
N = 1000


def _path_track(p, **extra):
    """Track at position ``p`` in the provider's path order; ids are shuffled against it."""
    tid = (p * 37) % N + 1
    return {
        "item_id": str(tid), "uri": f"library://track/{tid}", "name": f"Song {tid}",
        "artists": [{"name": f"Artist {tid % 7}"}], "album": {"name": "Album"}, "duration": 200,
        "provider_mappings": [{"provider_instance": FS, "item_id": f"Music/{p:05d}.flac"}],
        **extra,
    }


class ScanMA:
    """Music Assistant whose background scan has analysed some files, by path position."""

    def __init__(self):
        self.analysed: set[int] = set()
        self.position = {(p * 37) % N + 1: p for p in range(N)}
        self.looked_up: list[int] = []

    async def try_cmd(self, command, default=None, **args):
        assert command == "music/tracks/get", command
        assert args["allow_update_metadata"] is False and args["recursive"] is False
        tid = int(args["item_id"])
        self.looked_up.append(tid)
        if self.position[tid] in self.analysed:
            return {"item_id": str(tid), "audio_metadata": {"bpm": 124.0, "musical_key": "A minor"}}
        return {"item_id": str(tid), "audio_metadata": None}

    def measured(self, lib):
        return {self.position[tid] for tid, row in lib.rows(self.position).items() if row["bpm"]}


@pytest.fixture
def scan(lib, monkeypatch):
    monkeypatch.setattr("library.FRONTIER_MISS_RUN", 20)
    lib.upsert_tracks([_path_track(p) for p in range(N)])
    lib.db.commit()
    return ScanMA()


async def test_frontier_follows_the_scan_in_path_order_and_resumes(lib, scan):
    scan.analysed = set(range(300))
    first = await lib.sync_features(scan, expected=300)
    assert (first["filled"], first["checked"], first["more"], first["caught_up"]) == (300, 320, False, True)
    assert scan.measured(lib) == set(range(300))
    # the scan moves on: the next pass starts after the last hit, not from the top
    scan.analysed |= set(range(300, 500))
    scan.looked_up.clear()
    second = await lib.sync_features(scan, expected=500)
    assert (second["filled"], second["checked"]) == (200, 220)
    scan.looked_up.clear()
    assert (await lib.sync_features(scan, expected=500))["checked"] == 20  # just the miss run


async def test_frontier_walk_stops_after_a_run_of_misses(lib, scan, monkeypatch):
    monkeypatch.setattr("library.PROBE_COUNT", 0)
    monkeypatch.setattr("library.SWEEP_BUDGET", 0)
    scan.analysed = set(range(100)) | set(range(150, 200))  # a 50-file gap > the miss run
    result = await lib.sync_features(scan)
    assert (result["filled"], result["checked"]) == (100, 120)
    assert scan.measured(lib) == set(range(100))


async def test_time_cap_ends_a_pass_and_the_next_one_continues(lib, scan):
    scan.analysed = set(range(500))
    clock = lambda: float(len(scan.looked_up))  # noqa: E731 - one second per lookup
    first = await lib.sync_features(scan, budget=120, clock=clock, expected=500)
    assert (first["checked"], first["filled"], first["more"]) == (120, 120, True)
    second = await lib.sync_features(scan, budget=10_000, clock=clock, expected=500)
    assert second["filled"] == 380 and not second["more"]
    # a slow server that runs out of time on misses alone does not trigger catch-up passes
    slow = await lib.sync_features(scan, budget=5, clock=clock, expected=500)
    assert slow["checked"] == 5 and slow["filled"] == 0 and not slow["more"]
    hits = [t for t in scan.looked_up if scan.position[t] < 500]
    assert len(hits) == len(set(hits)) == 500  # no analysed file was looked up twice


async def test_probes_find_analysed_files_away_from_the_anchor(lib, scan):
    # e.g. rows bulk-copied from another Music Assistant for a later stretch of the library
    scan.analysed = set(range(600, 800))
    first = await lib.sync_features(scan, expected=200)
    assert first["filled"] == 200 and first["more"]  # walked both ways from the probe hit
    assert scan.measured(lib) == set(range(600, 800))
    # the new anchor is followed from then on, without probing
    scan.analysed |= set(range(800, 900))
    scan.looked_up.clear()
    second = await lib.sync_features(scan, expected=300)
    assert second["filled"] == 100 and second["caught_up"]
    assert second["checked"] == 20 + 100 + 20  # top-of-list miss run + new hits + miss run


async def test_caught_up_skips_the_wide_searches(lib, scan):
    scan.analysed = set(range(50))
    assert (await lib.sync_features(scan, expected=50))["checked"] == 70
    scan.looked_up.clear()
    unknown = await lib.sync_features(scan)  # no count from MA: probes and sweep run too
    assert unknown["checked"] > 20 and not unknown["caught_up"]


async def test_played_tracks_are_checked_once_per_play(lib, scan, monkeypatch):
    monkeypatch.setattr("library.PROBE_COUNT", 0)
    monkeypatch.setattr("library.SWEEP_BUDGET", 0)
    analysed_tid, other_tid = (900 * 37) % N + 1, (950 * 37) % N + 1
    lib.upsert_tracks([_path_track(900, last_played=1000), _path_track(950, last_played=1000)])
    scan.analysed = {900}
    assert (await lib.sync_features(scan))["filled"] == 1
    assert {analysed_tid, other_tid} <= set(scan.looked_up)
    scan.looked_up.clear()
    await lib.sync_features(scan)
    assert other_tid not in scan.looked_up  # its miss is remembered until it is played again


def test_tempo_energy_fills_in_until_a_better_source_arrives(lib):
    lib.upsert_tracks([_track(1, audio_metadata={"bpm": 120.0})])
    row = lib.rows([1])[1]
    assert (row["energy"], row["energy_source"], row["analysed"]) == (5.5, "tempo", 1)
    lib._apply_audio_metadata(1, {"bpm": 150.0})  # re-analysed: the estimate follows the tempo
    assert lib.rows([1])[1]["energy"] == 7.75
    lib.db.execute("UPDATE tracks SET energy=3.0, energy_source='derived' WHERE id=1")
    lib._apply_audio_metadata(1, {"bpm": 170.0})
    assert (lib.rows([1])[1]["energy"], lib.rows([1])[1]["energy_source"]) == (3.0, "derived")
    lib._apply_audio_metadata(1, {"energy_level": 8})
    assert (lib.rows([1])[1]["energy"], lib.rows([1])[1]["energy_source"]) == (8.0, "ma")


async def test_sound_based_energy_replaces_the_tempo_estimate(lib):
    lib.upsert_tracks([_track(i, audio_metadata={"bpm": 80.0}) for i in range(1, 4)])
    assert lib.stats()["energy_from_tempo"] == 3
    await lib.sync_analysed(FakeMA([]))
    rows = lib.rows([1, 2, 3])
    assert {r["energy_source"] for r in rows.values()} == {"derived"}
    assert rows[3]["energy"] == 10.0


def test_stats_separate_the_pool_from_the_sound_index(lib):
    lib.upsert_tracks([_track(1, audio_metadata={"bpm": 100.0}), _track(2), _track(3)])
    lib.db.execute("UPDATE tracks SET analysed=1, sonic_item_id='s3', sonic_provider='sub--1' WHERE id=3")
    stats = lib.stats()
    assert (stats["total"], stats["analysed"], stats["measured"], stats["in_sound_index"]) == (3, 2, 1, 1)


def test_similar_seeds_fall_back_to_the_file_id(lib):
    lib.upsert_tracks([_path_track(5, audio_metadata={"bpm": 100.0}), _path_track(6)])
    lib.db.execute("UPDATE tracks SET analysed=1, sonic_provider=?, sonic_item_id='x' WHERE id=?",
                   (FS, (6 * 37) % N + 1))
    lib.upsert_tracks([_path_track(7)])  # not analysed: dropped
    ids = [(7 * 37) % N + 1, (5 * 37) % N + 1, (6 * 37) % N + 1]
    assert lib.similar_seeds(ids) == [(FS, "Music/00005.flac"), (FS, "x")]


def test_resolve_matches_a_domain_key_to_its_instance(lib):
    lib.upsert_tracks([_track(1, provider_mappings=[{"provider_instance": "opensubsonic--HJ4", "item_id": "77"}])])
    assert lib.resolve("opensubsonic", "77") == 1
    assert lib.resolve("opensubsonic", "78") is None


# -- Music Assistant's analysis coverage ------------------------------------

DOMAIN = "sonic_analysis"
T0 = 1_763_337_600  # a local midnight; MA's nightly scan starts here


def _nightly(lib, hours, *, per_hour=87, every=600, total=270_000, run_hours=4):
    """Record samples every ``every`` s while a nightly scan analyses ``per_hour`` for ``run_hours``."""
    for step in range(int(hours * 3600 / every) + 1):
        t = T0 + step * every
        day, into = divmod(t - T0, 86400)
        analysed = 1000 + day * per_hour * run_hours + per_hour * min(into, run_hours * 3600) // 3600
        lib.record_coverage(DOMAIN, analysed, total - analysed, now=t)
    return T0 + int(hours * 3600 / every) * every


def test_no_eta_until_samples_span_about_a_day(lib):
    last = _nightly(lib, 10)
    progress = lib.coverage_progress(DOMAIN, now=last)
    assert progress["state"] == "measuring" and progress["eta_seconds"] is None
    assert progress["rate_per_hour"] is None and progress["analysed"] == 1000 + 87 * 4


def test_rate_and_eta_follow_the_nightly_pattern(lib):
    daily = 87 * 4
    for hours in (30, 47, 52, 70, 80):
        lib.set_meta(f"coverage:{DOMAIN}", "[]")
        last = _nightly(lib, hours)
        progress = lib.coverage_progress(DOMAIN, now=last)
        assert progress["state"] == "analysing", hours
        assert progress["rate_per_hour"] == pytest.approx(daily / 24, rel=0.02), hours
        assert progress["eta_seconds"] == pytest.approx(progress["pending"] / (daily / 86400), rel=0.02)
    # between 20 and 24 hours the estimate is allowed, from everything recorded
    lib.set_meta(f"coverage:{DOMAIN}", "[]")
    last = _nightly(lib, 21)
    assert lib.coverage_progress(DOMAIN, now=last)["rate_per_hour"] == pytest.approx(daily / 21, rel=0.02)


def test_a_bulk_import_does_not_count_as_mas_pace(lib):
    daily = 87 * 4
    last = _nightly(lib, 30)
    calm = lib.coverage_progress(DOMAIN, now=last)
    # 50k results copied in while TideSync could not reach MA (40 minutes)
    jump_at = last + 2400
    before = lib.coverage_samples(DOMAIN)[-1]["analysed"]
    lib.record_coverage(DOMAIN, before + 50_000, 270_000 - before - 50_000, now=jump_at)
    assert lib.coverage_samples(DOMAIN)[-1]["step"] == 50_000
    progress = lib.coverage_progress(DOMAIN, now=jump_at)
    assert progress["analysed"] == before + 50_000  # it does count as ready
    assert progress["rate_per_hour"] == pytest.approx(calm["rate_per_hour"], rel=0.05)
    assert progress["rate_per_hour"] == pytest.approx(daily / 24, rel=0.05)
    # the ETA shrinks only because less is pending, not because MA looks faster
    assert progress["eta_seconds"] == pytest.approx(progress["pending"] / (daily / 86400), rel=0.05)


def test_a_step_survives_a_replaced_reading(lib):
    last = _nightly(lib, 30)
    before = lib.coverage_samples(DOMAIN)[-1]["analysed"]
    lib.record_coverage(DOMAIN, before + 20_000, 200_000, now=last + 100)
    count = len(lib.coverage_samples(DOMAIN))
    lib.record_coverage(DOMAIN, before + 20_010, 199_990, now=last + 500)  # within the gap: replaces it
    samples = lib.coverage_samples(DOMAIN)
    assert len(samples) == count
    assert samples[-1]["analysed"] == before + 20_010 and samples[-1]["step"] == 20_000


def test_a_fast_ma_is_not_mistaken_for_an_import(lib):
    # e.g. MA on a strong machine with keep_analysis_running: ~1,100 tracks/hour all day
    for i in range(0, 30 * 6 + 1):
        t = T0 + i * 600
        lib.record_coverage(DOMAIN, 1000 + i * 183, 270_000 - i * 183, now=t)
    assert not any(s.get("step") for s in lib.coverage_samples(DOMAIN))
    assert lib.coverage_progress(DOMAIN, now=T0 + 180 * 600)["rate_per_hour"] == pytest.approx(1098, rel=0.02)


def test_a_long_gap_with_normal_progress_is_not_an_import(lib):
    last = _nightly(lib, 30)
    before = lib.coverage_samples(DOMAIN)[-1]["analysed"]
    # TideSync was off for two days while MA analysed two nights' worth
    lib.record_coverage(DOMAIN, before + 2 * 87 * 4, 260_000, now=last + 2 * 86400)
    assert not lib.coverage_samples(DOMAIN)[-1].get("step")


def test_eta_counts_down_between_samples(lib):
    last = _nightly(lib, 30)
    fresh = lib.coverage_progress(DOMAIN, now=last)["eta_seconds"]
    assert lib.coverage_progress(DOMAIN, now=last + 3600)["eta_seconds"] == fresh - 3600


def test_no_progress_is_measuring_then_stalled_and_done_is_complete(lib):
    last = _nightly(lib, 22, per_hour=0)
    assert lib.coverage_progress(DOMAIN, now=last)["state"] == "measuring"
    last = _nightly(lib, 30, per_hour=0)
    progress = lib.coverage_progress(DOMAIN, now=last)
    assert progress["state"] == "stalled" and progress["eta_seconds"] is None
    lib.record_coverage(DOMAIN, 5000, 0, now=last + 600)
    assert lib.coverage_progress(DOMAIN)["state"] == "complete"


def test_samples_are_thinned_and_reset_when_ma_starts_over(lib):
    for minute in range(120):
        lib.record_coverage(DOMAIN, 100 + minute, 1000, now=T0 + minute * 60)
    samples = lib.coverage_samples(DOMAIN)
    assert len(samples) <= 16 and samples[-1]["analysed"] == 219  # ~one per 10 min, newest kept
    lib.record_coverage(DOMAIN, 5, 2000, now=T0 + 9000)
    assert [s["analysed"] for s in lib.coverage_samples(DOMAIN)] == [5]
    assert lib.coverage_progress("smart_fades") is None
