"""Per-person memory: block escalation, dislike threshold, Markdown round-trip."""
from user_memory import (
    DISLIKE_AT,
    Block,
    Like,
    Mood,
    Person,
    _block_cooldown_days,
    _parse,
    _serialize,
)


def test_block_cooldowns_escalate():
    assert _block_cooldown_days(1) == 21
    assert _block_cooldown_days(2) == 60
    assert _block_cooldown_days(3) == 180
    assert _block_cooldown_days(99) >= 3650  # >=4 == effectively permanent


def test_add_block_escalates_count_and_dislike():
    p = Person(name="Mom", slug="mom")
    uri = "tidal://1"
    p.add_block("Artist - Song", uri)
    assert len(p.blocks) == 1 and p.blocks[0].count == 1
    assert not p.blocks[0].is_disliked
    p.add_block("Artist - Song", uri)
    p.add_block("Artist - Song", uri)
    assert p.blocks[0].count == 3
    assert p.blocks[0].is_disliked  # count >= DISLIKE_AT
    assert DISLIKE_AT == 3


def test_block_dedupes_by_uri():
    p = Person(name="Mom", slug="mom")
    p.add_block("A - 1", "tidal://1")
    p.add_block("A - 1 (dup)", "tidal://1")
    assert len(p.blocks) == 1


def test_serialize_parse_round_trip():
    p = Person(
        name="Dad",
        slug="dad",
        summary="Likes mellow folk in the evening.",
        likes=[Like(ts="2026-06-01T20:00:00+00:00", label="Bon Iver - Holocene", uri="tidal://9")],
        moods=[Mood(slot="evening", ts="2026-06-01T20:00:00+00:00", vibe="chill")],
    )
    p.add_block("Skrillex - Bangarang", "tidal://x")

    parsed = _parse(_serialize(p), "dad")
    assert parsed.name == "Dad"
    assert parsed.summary == "Likes mellow folk in the evening."
    assert [lk.uri for lk in parsed.likes] == ["tidal://9"]
    assert [m.vibe for m in parsed.moods] == ["chill"]
    assert len(parsed.blocks) == 1
    assert parsed.blocks[0].uri == "tidal://x" and parsed.blocks[0].count == 1


def test_parse_legacy_block_line_without_count_or_since():
    # Old format: "- expires <iso> | <label> | <uri>" (no "x{n}" / "since {iso}").
    text = (
        "# Mom\n\n## Taste Profile\n(none yet)\n\n## Likes\n(none yet)\n\n"
        "## Blocks\n- expires 2030-01-01T00:00:00+00:00 | Artist - Song | tidal://legacy\n\n"
        "## Moods by Time of Day\n(none yet)\n"
    )
    person = _parse(text, "mom")
    assert len(person.blocks) == 1
    b = person.blocks[0]
    assert b.uri == "tidal://legacy"
    assert b.label == "Artist - Song"
    assert b.count == 1
    assert b.created  # back-filled so slot/month bucketing still works
