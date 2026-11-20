"""Sequencer: key/tempo maths and ordering behaviour."""
import random

from sequencer import (RADIO, Track, Weights, bpm_cost, camelot, energy_curve, key_cost, sequence,
                       transition_cost)


def test_camelot_conversion():
    assert camelot("A minor") == "8A"
    assert camelot("C major") == "8B"
    assert camelot("F# minor") == "11A"
    assert camelot("Bb major") == "6B"
    assert camelot("Am") == "8A"
    assert camelot("8a") == "8A"
    assert camelot("") is None
    assert camelot("H dur") is None


def test_key_cost_orders_compatibility():
    assert key_cost("8A", "8A") == 0
    assert key_cost("8A", "9A") < key_cost("8A", "10A") < key_cost("8A", "2A")
    assert key_cost("8A", "8B") < key_cost("8A", "3B")
    assert key_cost("12A", "1A") == key_cost("8A", "9A")  # wheel wraps
    assert key_cost(None, "8A") == 0.5


def test_bpm_cost_handles_half_and_double_time():
    assert bpm_cost(120, 120) == 0
    assert bpm_cost(120, 60) == 0
    assert bpm_cost(70, 140) == 0
    assert bpm_cost(120, 124) < bpm_cost(120, 135) < bpm_cost(120, 160)
    assert bpm_cost(None, 120) == 0.5


def test_energy_curves_have_expected_shape():
    build = energy_curve("build", 10)
    assert build == sorted(build) and build[0] < build[-1]
    down = energy_curve("wind_down", 10)
    assert down == sorted(down, reverse=True)
    peak = energy_curve("peak", 20)
    assert max(peak) > peak[0] and max(peak) > peak[-1]
    assert energy_curve("flat", 3, base=4) == [4, 4, 4]
    # a refill continues the curve rather than restarting it
    whole = energy_curve("build", 10)
    assert energy_curve("build", 5, start=5, total=10) == whole[5:]


def _pool(n=60, seed=1):
    rng = random.Random(seed)
    keys = [f"{k}{m}" for k in range(1, 13) for m in "AB"]
    return [
        Track(id=str(i), artist=f"artist{i % 12}", bpm=rng.uniform(80, 170), key=rng.choice(keys),
              energy=rng.uniform(1, 10), relevance=rng.random())
        for i in range(n)
    ]


def test_sequence_is_deterministic_and_order_independent():
    pool = _pool()
    targets = energy_curve("build", 8)
    first = [t.id for t in sequence(pool, targets)]
    shuffled = pool[:]
    random.Random(9).shuffle(shuffled)
    assert [t.id for t in sequence(shuffled, targets)] == first
    assert len(set(first)) == 8


def test_sequence_follows_energy_curve():
    picks = sequence(_pool(200), energy_curve("build", 10, span=3))
    energies = [t.energy for t in picks]
    assert sum(energies[-3:]) / 3 > sum(energies[:3]) / 3 + 2


def test_sequence_spaces_out_artists():
    pool = [Track(id=str(i), artist="same" if i < 10 else f"a{i}", bpm=120, key="8A", energy=5,
                  relevance=0.9 if i < 10 else 0.6) for i in range(40)]
    picks = sequence(pool, [5.0] * 8)
    positions = [i for i, t in enumerate(picks) if t.artist == "same"]
    assert all(b - a >= Weights().artist_gap for a, b in zip(positions, positions[1:]))


def test_sequence_respects_previous_tracks():
    pool = _pool(30)
    prev = [pool[0], pool[1]]
    picks = sequence(pool, [5.0] * 5, previous=prev)
    assert not {t.id for t in picks} & {"0", "1"}
    # the opener should be a smooth move from the last played track
    opener_cost = transition_cost(prev[-1], picks[0])
    worst = max(transition_cost(prev[-1], t) for t in pool[2:])
    assert opener_cost < worst


def test_sequence_beats_random_order_on_smoothness():
    pool = _pool(120, seed=4)
    picks = sequence(pool, [5.5] * 10)
    ours = sum(transition_cost(a, b) for a, b in zip(picks, picks[1:]))
    rand = pool[:10]
    baseline = sum(transition_cost(a, b) for a, b in zip(rand, rand[1:]))
    assert ours < baseline * 0.6


def test_sequence_tolerates_missing_features_and_small_pools():
    pool = [Track(id="a", artist="x"), Track(id="b", artist="y", bpm=120)]
    assert {t.id for t in sequence(pool, [5.0] * 5)} == {"a", "b"}
    assert sequence([], [5.0]) == []


def test_featured_credits_count_as_the_lead_artist():
    assert Track(id="1", artist="Ashnikko, Princess Nokia").artist_key == "ashnikko"
    assert Track(id="2", artist="Calvin Harris feat. Rihanna").artist_key == "calvin harris"
    assert Track(id="3", artist="Simon & Garfunkel").artist_key == "simon"
    pool = [Track(id=str(i), artist="Ashnikko" if i % 2 else f"Ashnikko, Guest {i}", bpm=120, key="8A",
                  energy=5, relevance=0.95) for i in range(10)]
    pool += [Track(id=str(100 + i), artist=f"Other {i}", bpm=120, key="8A", energy=5, relevance=0.6)
             for i in range(10)]
    picks = sequence(pool, [5.0] * 6)
    assert sum(t.artist_key == "ashnikko" for t in picks) == 1


def test_another_version_of_a_queued_song_is_not_picked():
    played = [Track(id="1", artist="Rose Betts", title="Irish Eyes", relevance=1.0)]
    pool = [Track(id="2", artist="Rose Betts", title="Irish Eyes (The Piano Version)", relevance=0.99),
            Track(id="3", artist="A", title="Let It Go (Acoustic Version)", relevance=0.9),
            Track(id="4", artist="A", title="Let It Go", relevance=0.8),
            Track(id="5", artist="B", title="Other", relevance=0.5)]
    picks = sequence(pool, [5.0] * 4, previous=played)
    assert [t.id for t in picks] in (["3", "5"], ["5", "3"])  # no piano version, one Let It Go


def test_radio_prefers_a_close_match_over_a_smooth_key_and_tempo():
    prev = Track(id="p", artist="seed", bpm=100, key="8A", energy=5)
    close = Track(id="c", artist="x", bpm=125, key="2B", energy=5, relevance=0.95)
    smooth = Track(id="s", artist="y", bpm=100, key="8A", energy=5, relevance=0.5)
    assert [t.id for t in sequence([close, smooth], [5.0], previous=[prev], weights=RADIO)] == ["c"]
