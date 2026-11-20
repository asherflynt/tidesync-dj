"""Style tagger: syncing style vectors, the second sonic view, and the style gate."""
import base64

import pytest

from plan import Plan
from retrieval import STYLE_GATE_FLOOR, gather
from styles import StylesUnavailable, decode_vector, style_similarity, top_styles
from test_retrieval import FS, SoundMA, _library, _path, _track

LABELS = [f"Genre---Style {i}" for i in range(400)]


def vec(*hot, base=2):
    """A style vector with a few strong styles (indices -> 200) and a low floor elsewhere."""
    raw = bytearray([base] * 400)
    for i in hot:
        raw[i] = 200
    return bytes(raw)


FOLK, RNB = vec(10, 11), vec(300, 301)


class FakeStyles:
    """The tagger's API over a fixed set of files."""

    def __init__(self, files, neighbours=None, fail=False):
        self.files = files  # path -> (vector, navidrome ids)
        self.neighbours = neighbours or {}
        self.fail = fail
        self.similar_calls = []

    async def labels(self):
        return LABELS

    async def status(self):
        return {"files": len(self.files), "done": len(self.files), "pending": 0, "eta_hours": None}

    async def tracks(self, after, limit=2000):
        if self.fail:
            raise StylesUnavailable("down")
        rows = [{"seq": n, "path": p, "navidrome_ids": ids, "styles": base64.b64encode(v).decode()}
                for n, (p, (v, ids)) in enumerate(self.files.items(), 1) if n > after][:limit]
        return {"items": rows, "next": rows[-1]["seq"] if rows else after}

    async def similar(self, path, limit=300):
        self.similar_calls.append(path)
        if self.fail:
            raise StylesUnavailable("down")
        if path not in self.files:
            return None
        return [{"path": p, "navidrome_ids": [], "score": 0.9} for p in self.neighbours.get(path, [])][:limit]

    async def close(self):
        pass


def test_style_similarity_and_names():
    assert style_similarity(FOLK, FOLK) == pytest.approx(1.0)
    assert style_similarity(vec(10, base=0), vec(300, base=0)) == 0.0
    assert style_similarity(FOLK, RNB) < style_similarity(FOLK, vec(10, 12)) < 1.0
    assert style_similarity(FOLK, RNB) == pytest.approx(style_similarity(RNB, FOLK))
    assert style_similarity(bytes(400), FOLK) == 0.0
    assert top_styles(FOLK, LABELS) == ["Style 10", "Style 11"]
    assert decode_vector(base64.b64encode(FOLK).decode()) == FOLK
    assert decode_vector("not base64!") is None and decode_vector(base64.b64encode(b"short").decode()) is None


async def test_sync_matches_files_by_navidrome_id_then_path(tmp_path):
    ma = SoundMA()
    navidrome_only = {**_track(150), "provider_mappings": [{"provider_instance": "opensubsonic--HJ4", "item_id": "nd-150"}]}
    lib = await _library(tmp_path, ma, extra=[navidrome_only])
    styles = FakeStyles({
        _path(5): (FOLK, []),  # matched by its file path (filesystem mapping)
        "Renamed/150.flac": (RNB, ["nd-150"]),  # matched by Navidrome song id
        "Unknown/1.flac": (FOLK, ["nd-404"]),  # not in the library
    })
    first = await lib.sync_styles(styles, page=2)
    assert first == {"synced": 2, "unmatched": 1, "more": False}
    assert lib.style_vectors([5, 150, 6]) == {5: FOLK, 150: RNB}
    assert lib.style_path(150) == "Renamed/150.flac" and lib.style_labels() == LABELS
    assert lib.stats()["with_styles"] == 2
    again = await lib.sync_styles(styles)  # resumes after the last row it saw
    assert again["synced"] == 0 and again["unmatched"] == 0
    lib.close()


async def _styled(tmp_path, neighbours, vectors=None, extra=(), **kw):
    ma = SoundMA(**kw)
    lib = await _library(tmp_path, ma, extra=extra)
    vectors = vectors or {}
    files = {_path(i): (vectors.get(i, FOLK), []) for i in range(1, 101)}
    files.update({_path(int(t["item_id"])): (vectors.get(int(t["item_id"]), FOLK), []) for t in extra})
    styles = FakeStyles(files, {_path(k): [_path(n) for n in v] for k, v in neighbours.items()})
    await lib.sync_styles(styles)
    return lib, ma, styles


async def test_both_views_agreeing_lead_and_unanalysed_songs_join(tmp_path):
    # 300 is not analysed by Music Assistant at all; only the tagger has measured it
    unanalysed = {**_track(300), "audio_metadata": None}
    unanalysed["provider_mappings"] = [{"provider_instance": FS, "item_id": _path(300)}]
    lib, ma, styles = await _styled(tmp_path, {50: [300, 60, 51]}, extra=[unanalysed])
    scores, src = await gather(ma, lib, Plan(), seed_ids=[50], styles=styles)
    assert styles.similar_calls == [_path(50)] and src.styled == 3
    assert 300 in scores  # reachable although Music Assistant never analysed it
    # 51 and 60 are liked by both views; 49 (as close as 51 to the sound index) only by one
    assert scores[51] > scores[49]
    assert scores[60] > scores[59]
    lib.close()


async def test_style_gate_pushes_down_a_different_genre(tmp_path):
    lib, ma, styles = await _styled(tmp_path, {}, vectors={49: vec(300, 301, base=0)})  # nothing in common
    plain, _ = await gather(ma, lib, Plan(), seed_ids=[50])
    gated, src = await gather(ma, lib, Plan(), seed_ids=[50], styles=styles)
    assert src.style_gated >= 1
    assert gated[49] == pytest.approx(plain[49] * STYLE_GATE_FLOOR, abs=1e-3)
    assert gated[51] == plain[51]  # same style as the seed: untouched
    lib.close()


async def test_an_unanalysed_seed_with_style_data_is_followed(tmp_path):
    unanalysed = {**_track(300), "audio_metadata": None}
    unanalysed["provider_mappings"] = [{"provider_instance": FS, "item_id": _path(300)}]
    lib, ma, styles = await _styled(tmp_path, {300: [20, 21, 22]}, extra=[unanalysed])
    scores, src = await gather(ma, lib, Plan(), seed_ids=[300], targets=[5.0] * 2, styles=styles)
    assert not ma.args_of("sonic_similarity/similar_clap")  # nothing to ask MA about
    assert scores[20] > scores[21] > scores[22] and not any("not been analysed" in n for n in src.notes)
    lib.close()


async def test_tagger_outage_falls_back_to_the_sound_index(tmp_path):
    lib, ma, styles = await _styled(tmp_path, {50: [60]})
    styles.fail = True
    scores, src = await gather(ma, lib, Plan(), seed_ids=[50], recent_ids=[40], styles=styles)
    assert scores[51] > 0.9 and any("style tagger is unavailable" in n for n in src.notes)
    assert len(styles.similar_calls) == 1  # not asked again for the second anchor
    lib.close()


async def test_client_talks_to_the_tagger_api():
    import httpx

    from styles import StylesClient

    seen = []

    def handler(request):
        seen.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/v1/similar":
            if request.url.params["path"] == "missing.flac":
                return httpx.Response(404, json={"error": "not analysed"})
            return httpx.Response(200, json={"items": [{"path": "b.flac", "navidrome_ids": [], "score": 0.9}]})
        if request.url.path == "/v1/tracks":
            return httpx.Response(200, json={"items": [], "next": 7})
        if request.url.path == "/v1/labels":
            return httpx.Response(200, json={"labels": LABELS})
        return httpx.Response(500)

    client = StylesClient("http://tagger:8791/", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await client.similar("a b/c.flac", 5) == [{"path": "b.flac", "navidrome_ids": [], "score": 0.9}]
    assert await client.similar("missing.flac") is None
    assert (await client.tracks(3, 10))["next"] == 7 and await client.labels() == LABELS
    assert seen[0] == ("/v1/similar", {"path": "a b/c.flac", "limit": "5"})
    with pytest.raises(StylesUnavailable):
        await client.status()
    await client.close()
