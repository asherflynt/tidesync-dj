"""Double-start re-entrancy guard on _rebuild, and the dj_status() snapshot.

Both build a bare DJEngine (object.__new__) wired with only the attributes the
method under test touches, mirroring test_stats.py / test_grounding.py.
"""
import asyncio
import time
from collections import deque

import sonic_features
from claude_brain import SetPhase, SetPlan
from scheduler import DJEngine


# --------------------------------------------------------------------------- #
# Part A: a second rebuild while one is in flight must bail "busy", not overlap
# --------------------------------------------------------------------------- #
async def test_concurrent_rebuild_bails_busy():
    e = object.__new__(DJEngine)
    e._rebuild_lock = asyncio.Lock()
    calls = []
    holding = asyncio.Event()

    async def slow_locked(reason, **kw):
        calls.append(reason)        # the guarded body — the real clear+enqueue
        holding.set()               # signal that we now hold the lock
        await asyncio.sleep(0.05)   # keep it held so the 2nd call overlaps
        return {"ok": True, "reason": reason}

    e._rebuild_locked = slow_locked

    async def second():
        await holding.wait()        # ensure the first call holds the lock first
        return await e._rebuild("start_radio")

    first_res, second_res = await asyncio.gather(e._rebuild("start_radio"), second())

    assert first_res["ok"] is True
    assert second_res["ok"] is False and second_res.get("busy") is True
    # The guarded body (queue clear + enqueue) ran exactly once.
    assert calls == ["start_radio"]


async def test_rebuild_runs_again_once_lock_is_free():
    """The guard is transient — a later rebuild proceeds normally."""
    e = object.__new__(DJEngine)
    e._rebuild_lock = asyncio.Lock()
    calls = []

    async def locked(reason, **kw):
        calls.append(reason)
        return {"ok": True}

    e._rebuild_locked = locked
    await e._rebuild("start_radio")
    await e._rebuild("start_radio")
    assert calls == ["start_radio", "start_radio"]


# --------------------------------------------------------------------------- #
# Part B: dj_status() returns the full UI shape
# --------------------------------------------------------------------------- #
def _item(uri, artist, name):
    return {"uri": uri, "name": name, "artists": [{"name": artist}]}


class _FakeMA:
    async def get_queue(self):
        return {
            "current_item": _item("tidal://now", "X", "Now"),
            "items_remaining": 12,
            "state": "playing",
        }

    async def get_history(self, n=20):
        return [_item("tidal://h1", "Y", "H1")]

    def external_ids(self, item):
        return {}


def _engine(tmp_path):
    e = object.__new__(DJEngine)
    e._ma = _FakeMA()
    e._sonic = sonic_features.SonicFeatures(tmp_path, enabled=False)
    e._dj_activity = {"phase": "enqueuing", "detail": "Queueing tracks", "target": 30}
    e._track_energy = {"tidal://now": 7, "tidal://h1": 4}
    e._set_plan = SetPlan(
        phases=[
            SetPhase(name="Gentle Entry", target_energy=3, approx_minutes=20),
            SetPhase(name="Peak", target_energy=8, approx_minutes=20),
        ],
        arc_note="a gentle rise",
    )
    e._set_plan_started = time.monotonic()
    e.vibe_prompt = "lofi"
    e._dj_stopped = False
    e._energy_bias = 0
    e.decision_log = deque([{
        "timestamp": "2026-06-28T20:14:11",
        "reason": "start_radio",
        "dj_note": "opening soft",
        "vibe_reading": {"energy_target": 3, "mood": "calm", "interpretation": "easing in"},
        "mood_shift": False,
        "mood_shift_reason": None,
        "tracks": [{"query": "A - B", "reason": "sets the tone", "energy": 3}],
        "enqueued": 30,
    }])
    return e


async def test_dj_status_shape(tmp_path):
    s = await _engine(tmp_path).dj_status()

    # Activity + live loading-bar progress (items_remaining drives "enqueued").
    assert s["activity"]["phase"] == "enqueuing"
    assert s["activity"]["target"] == 30
    assert s["activity"]["enqueued"] == 12

    # Set plan + where we are in the arc.
    assert s["set_plan"]["arc_note"] == "a gentle rise"
    assert len(s["set_plan"]["phases"]) == 2
    assert s["arc_position"]["current_phase"] in ("Gentle Entry", "Peak")
    assert 0 <= s["arc_position"]["pct_through"] <= 100

    # Energy curve: history + the now-playing point flagged.
    assert isinstance(s["energy_arc"], list) and s["energy_arc"]
    assert s["energy_arc"][-1]["now_playing"] is True

    # Latest decision reasoning surfaced for the feed.
    assert s["latest_decision"]["dj_note"] == "opening soft"
    assert s["latest_decision"]["tracks"][0]["query"] == "A - B"
    assert s["now_playing"]


async def test_dj_status_idle_is_safe(tmp_path):
    """No plan / no decisions yet → empty-but-valid shape, no crash."""
    e = _engine(tmp_path)
    e._dj_activity = {"phase": "idle"}
    e._set_plan = None
    e._set_plan_started = None
    e.decision_log = deque()

    s = await e.dj_status()
    assert s["activity"]["phase"] == "idle"
    assert "enqueued" not in s["activity"]   # only added while enqueuing
    assert s["set_plan"] is None
    assert s["arc_position"] is None
    assert s["latest_decision"] is None
