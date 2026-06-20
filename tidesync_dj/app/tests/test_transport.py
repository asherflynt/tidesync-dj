"""get_active_queue resolution, transport wrappers, and transfer args."""
from ma_client import MusicAssistantClient


def _client():
    c = MusicAssistantClient("ws://x")
    c.selected_player_id = "sel"
    c.active_queue_id = "sel"
    return c


def _with_queues(c, queues):
    async def fake_cmd(command, **kw):
        return queues
    c._command = fake_cmd
    return c


async def test_active_queue_selected_playing():
    c = _with_queues(_client(), [{"queue_id": "sel", "state": "playing"},
                                 {"queue_id": "other", "state": "idle"}])
    assert await c.get_active_queue() == ("sel", "playing")


async def test_active_queue_follows_other_playing():
    c = _with_queues(_client(), [{"queue_id": "sel", "state": "idle"},
                                 {"queue_id": "other", "state": "playing"}])
    assert await c.get_active_queue() == ("other", "playing")


async def test_active_queue_paused_selected():
    c = _with_queues(_client(), [{"queue_id": "sel", "state": "paused"}])
    assert await c.get_active_queue() == ("sel", "paused")


async def test_active_queue_nothing_active():
    c = _with_queues(_client(), [{"queue_id": "sel", "state": "idle"}])
    assert await c.get_active_queue() == ("sel", "idle")


async def test_active_queue_string_shaped_list_falls_back_to_player_state():
    # MA 2.8.9 returns plain queue-id strings: state can't be read from the list,
    # so we must fall back to player-level state (else toggle would resume/restart).
    c = _with_queues(_client(), ["sel", "other"])

    async def fake_play_state():
        return "playing"

    c.get_play_state = fake_play_state
    assert await c.get_active_queue() == ("sel", "playing")


async def test_queue_wrappers_send_expected_commands():
    c = _client()
    sent = {}

    async def cap(command, **kw):
        sent.clear()
        sent.update({"command": command, **kw})
    c._command = cap

    assert await c.queue_resume("q1") and sent == {"command": "player_queues/resume", "queue_id": "q1"}
    assert await c.queue_pause("q2") and sent == {"command": "player_queues/pause", "queue_id": "q2"}
    assert await c.queue_play("q3") and sent == {"command": "player_queues/play", "queue_id": "q3"}


async def test_transfer_queue_uses_source_queue_id():
    c = _client()
    sent = {}

    async def cap(command, **kw):
        sent.update({"command": command, **kw})
    c._command = cap

    ok = await c.transfer_queue("B")
    assert ok
    assert sent == {
        "command": "player_queues/transfer",
        "source_queue_id": "sel",
        "target_queue_id": "B",
        "auto_play": True,
    }
