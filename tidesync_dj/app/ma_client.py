"""Music Assistant WebSocket client.

Talks to the Music Assistant add-on over its WebSocket API
(ws://<ma_host>:<ma_port>/ws). We use raw `websockets` rather than the
`music-assistant-client` package so the command surface is explicit and the
reconnect/event behaviour is fully under our control.

Protocol (see https://music-assistant.io/integration/websocket/):
  * On connect, the server sends a `server_info` message.
  * Commands are JSON: {"command": "<cmd>", "message_id": "<id>", "args": {...}}
  * Responses echo the message_id: {"message_id": "<id>", "result": ...}
    or {"message_id": "<id>", "error_code": ..., "details": ...}
  * Events are pushed unsolicited: {"event": "<type>", "object_id": ...,
    "data": ...}

Command names follow MA's documented schema; if your MA version differs,
adjust the COMMAND_* constants below.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
from typing import Any, Awaitable, Callable

import websockets

_LOGGER = logging.getLogger(__name__)

# --- MA command names (centralised so they're easy to tweak per MA version) ---
CMD_PLAYERS_ALL = "players/all"
CMD_QUEUES_ALL = "player_queues/all"
CMD_QUEUE_ITEMS = "player_queues/items"
CMD_PLAY_MEDIA = "player_queues/play_media"
CMD_NEXT = "players/cmd/next"
CMD_PLAY = "players/cmd/play"
CMD_PAUSE = "players/cmd/pause"
CMD_PLAY_PAUSE = "players/cmd/play_pause"
CMD_STOP = "players/cmd/stop"
CMD_SEARCH = "music/search"
CMD_LIBRARY_TRACKS = "music/tracks/library_items"
# Queue editing (remove + reorder upcoming items from the dashboard).
# NOTE: verify these two strings against the running MA version if remove/move
# ever stops working — the player_queues API has shifted historically.
CMD_QUEUE_DELETE = "player_queues/delete_item"
CMD_QUEUE_MOVE = "player_queues/move_item"
# Favorites + playlist management (used for "like" and "save session").
CMD_QUEUE_CLEAR = "player_queues/clear"
CMD_FAVORITE_ADD = "music/favorites/add_item"
CMD_PLAYLIST_CREATE = "music/playlists/create_playlist"
CMD_PLAYLIST_ADD_TRACKS = "music/playlists/add_playlist_tracks"

# Event types we care about.
EVENT_QUEUE_UPDATED = "queue_updated"
EVENT_QUEUE_TIME_UPDATED = "queue_time_updated"

EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
ReconnectCallback = Callable[[], Awaitable[None]]


class MusicAssistantClient:
    def __init__(
        self,
        ws_url: str,
        username: str = "",
        password: str = "",
        token: str = "",
    ) -> None:
        self._ws_url = ws_url
        self._username = username
        self._password = password
        self._token = token
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._msg_ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future] = {}
        self._event_cb: EventCallback | None = None
        self._reconnect_cb: ReconnectCallback | None = None
        self._recv_task: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self._authenticated = asyncio.Event()
        self._closing = False
        self._is_open = False
        self._authed = False
        self.last_error: str | None = None
        self.server_info: dict[str, Any] = {}
        # Best-guess active queue, refreshed from players/queues.
        self.active_queue_id: str | None = None
        # Explicitly selected player (overrides auto-pick). In MA a player's own
        # queue_id equals its player_id.
        self.selected_player_id: str | None = None
        # Last successful player list — returned when MA is temporarily disconnected
        # so the UI dropdown doesn't flicker/disappear during brief reconnects.
        self._cached_players: list[dict[str, Any]] = []

    @property
    def is_connected(self) -> bool:
        """True only when the socket is open AND we're authenticated."""
        return self._is_open and self._authed

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #
    def on_event(self, cb: EventCallback) -> None:
        self._event_cb = cb

    def on_reconnect(self, cb: ReconnectCallback) -> None:
        self._reconnect_cb = cb

    async def _resolve_url(self) -> str:
        """Return the best WebSocket URL for this run.

        When the configured host is a private LAN IP (192.168.x, 10.x, etc.)
        we're almost certainly running inside a HA add-on container and the
        connection goes through Docker hairpin NAT, which HA OS evicts after
        ~30 s regardless of activity.  The Music Assistant add-on uses
        host-networking, so it is also reachable via the Docker bridge gateway
        (172.30.32.1 on stock HA OS).  Try that first; fall back to the
        configured URL if it doesn't answer.
        """
        import re
        import socket

        lan_re = re.compile(r"^(192\.168\.|10\.|172\.(1[6-9]|2[0-9]|3[01])\.)")
        if not lan_re.match(self._ws_url.split("//")[-1].split(":")[0]):
            return self._ws_url  # not a LAN IP — use as-is

        # Extract port from original URL so the gateway candidate uses the same.
        try:
            port = int(self._ws_url.split(":")[-1].split("/")[0])
        except ValueError:
            port = 8095

        gateway = "172.30.32.1"
        candidate = f"ws://{gateway}:{port}/ws"
        try:
            sock = socket.create_connection((gateway, port), timeout=2)
            sock.close()
            _LOGGER.info(
                "Using internal Docker gateway %s (avoids hairpin NAT on %s)",
                candidate,
                self._ws_url,
            )
            return candidate
        except OSError:
            return self._ws_url

    async def connect(self) -> None:
        """Connect, handshake, and authenticate. Raises on failure."""
        url = await self._resolve_url()
        _LOGGER.info("Connecting to Music Assistant at %s", url)
        self._ws = await websockets.connect(url, max_size=None)
        self._closing = False
        self._is_open = True
        self._recv_task = asyncio.create_task(self._receive_loop())
        # Wait for the initial server_info handshake.
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=10)
        except asyncio.TimeoutError:
            _LOGGER.warning("No server_info received; continuing anyway")
        await self._authenticate()

    async def _authenticate(self) -> None:
        """Authenticate the session (MA 2.8+ requires this before any command).

        Order of preference: an explicit MA token, then username/password login.
        With no credentials we probe and assume an older, auth-less MA.
        """
        # 1) Explicit token.
        if self._token:
            try:
                await self._command("auth", token=self._token)
            except Exception as err:  # noqa: BLE001
                self.last_error = f"MA token rejected: {err}"
                raise ConnectionError(self.last_error)
            self._authed = True
            self._authenticated.set()
            self.last_error = None
            _LOGGER.info("Authenticated to Music Assistant with token")
            return

        # 2) Username / password (builtin provider).
        if self._username and self._password:
            res = await self._command(
                "auth/login", username=self._username, password=self._password
            )
            if not (isinstance(res, dict) and res.get("success")):
                msg = res.get("error") if isinstance(res, dict) else res
                self.last_error = f"MA login failed: {msg}"
                raise ConnectionError(self.last_error)
            login_token = res.get("token") if isinstance(res, dict) else None
            # Bind the session if login alone didn't authenticate it.
            if not await self._authed_probe() and login_token:
                try:
                    await self._command("auth", token=login_token)
                except Exception:  # noqa: BLE001
                    pass
            if not await self._authed_probe():
                self.last_error = "MA login succeeded but the session is unauthorized"
                raise ConnectionError(self.last_error)
            self._authed = True
            self._authenticated.set()
            self.last_error = None
            _LOGGER.info("Authenticated to Music Assistant as %s", self._username)
            return

        # 3) No credentials — works only on older MA without auth.
        if await self._authed_probe():
            self._authed = True
            self._authenticated.set()
            self.last_error = None
            return
        self.last_error = (
            "Music Assistant requires a login. Set ma_username + ma_password "
            "(or ma_token) in the add-on configuration."
        )
        raise ConnectionError(self.last_error)

    async def _authed_probe(self) -> bool:
        """Return True if a trivial command succeeds (i.e. we're authenticated)."""
        try:
            await self._command("players/all")
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _keepalive_loop(self, interval: int = 15) -> None:
        """Send a cheap command periodically so the socket never sits idle.

        Idle authenticated connections through the HA add-on/Docker hairpin get
        reset after ~30s; a lightweight `info` every 15s keeps them alive.
        """
        while not self._closing:
            await asyncio.sleep(interval)
            if self.is_connected:
                try:
                    await self._command("info")
                except Exception:  # noqa: BLE001 - the receive loop handles drops
                    pass

    async def run_forever(self, retry_delay: int = 2) -> None:
        """Maintain the connection, reconnecting with backoff on drops."""
        keepalive = asyncio.create_task(self._keepalive_loop())
        try:
            await self._run_forever_inner(retry_delay)
        finally:
            keepalive.cancel()

    async def _run_forever_inner(self, retry_delay: int) -> None:
        while not self._closing:
            try:
                # Open + handshake + authenticate.
                await self.connect()
            except OSError as err:
                # Socket couldn't be opened at all — real config problem.
                self.last_error = (
                    f"Can't reach Music Assistant at {self._ws_url} — check the "
                    f"host and port. ({err})"
                )
                _LOGGER.warning("MA connection error: %s", self.last_error)
            except ConnectionError as err:
                # Auth/handshake error — _authenticate already set last_error.
                _LOGGER.warning("MA auth error: %s", err)
            except Exception as err:  # noqa: BLE001
                self.last_error = f"Music Assistant connection error: {err}"
                _LOGGER.warning("MA connection error: %s", err)
            else:
                # Connected and authenticated.
                # Run post-connect helpers independently so a Python-level bug
                # in our own code doesn't get misread as a socket drop and
                # trigger an instant reconnect loop.
                try:
                    await self.refresh_active_queue()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("refresh_active_queue error (socket still open): %s", err)

                if self._reconnect_cb:
                    try:
                        await self._reconnect_cb()
                    except Exception as cb_err:  # noqa: BLE001
                        _LOGGER.debug("Reconnect callback error: %s", cb_err)

                # Block here until the socket actually closes.
                if self._recv_task:
                    try:
                        await self._recv_task
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.info("MA connection dropped, reconnecting: %s", err)

            if self._closing:
                break
            self._connected.clear()
            self._authenticated.clear()
            self._authed = False
            self._is_open = False
            await asyncio.sleep(retry_delay)

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                await self._handle_message(raw)
        except websockets.ConnectionClosed as exc:
            _LOGGER.info(
                "MA websocket closed (code=%s reason=%r)", exc.code, exc.reason
            )
        finally:
            self._is_open = False
            self._authed = False
            self._authenticated.clear()
            self._fail_pending(ConnectionError("websocket closed"))

    async def _handle_message(self, raw: str | bytes) -> None:
        import json

        try:
            msg = json.loads(raw)
        except ValueError:
            _LOGGER.debug("Non-JSON MA message ignored")
            return

        # Handshake.
        if "server_version" in msg or msg.get("server_id"):
            self.server_info = msg
            self._connected.set()
            return

        # Command response.
        if "message_id" in msg:
            fut = self._pending.pop(str(msg["message_id"]), None)
            if fut and not fut.done():
                if "error_code" in msg or "error" in msg:
                    fut.set_exception(
                        RuntimeError(msg.get("details") or msg.get("error"))
                    )
                else:
                    fut.set_result(msg.get("result"))
            return

        # Pushed event.
        if "event" in msg and self._event_cb:
            try:
                await self._event_cb(msg["event"], msg.get("data") or msg)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error(
                    "Event callback raised for event %r: %s",
                    msg.get("event"), err, exc_info=True,
                )

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def close(self) -> None:
        self._closing = True
        if self._recv_task:
            self._recv_task.cancel()
        if self._ws:
            await self._ws.close()

    # ------------------------------------------------------------------ #
    # Command helper
    # ------------------------------------------------------------------ #
    async def _command(self, command: str, **args: Any) -> Any:
        if not self._is_open:
            # Socket is closed — wait briefly for a reconnect before failing.
            # (Don't gate on is_connected here: _authenticate calls _command
            # before _authed is set, so using is_connected would deadlock auth.)
            try:
                await asyncio.wait_for(self._authenticated.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                raise ConnectionError("Music Assistant not connected")
        if self._ws is None:
            raise ConnectionError("not connected to Music Assistant")
        import json

        message_id = str(next(self._msg_ids))
        payload = {"command": command, "message_id": message_id}
        if args:
            payload["args"] = args
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[message_id] = fut
        try:
            await self._ws.send(json.dumps(payload))
        except Exception as send_err:
            # Socket died between the is_open check and the send.  Remove the
            # future from pending and cancel it so Python never logs
            # "Future exception was never retrieved" when _fail_pending runs.
            self._pending.pop(message_id, None)
            fut.cancel()
            raise ConnectionError(f"websocket send failed: {send_err}") from send_err
        return await asyncio.wait_for(fut, timeout=30)

    # ------------------------------------------------------------------ #
    # Players
    # ------------------------------------------------------------------ #
    @staticmethod
    def _player_view(player: dict[str, Any]) -> dict[str, Any]:
        return {
            "player_id": player.get("player_id") or player.get("id"),
            "name": player.get("display_name") or player.get("name"),
            "available": player.get("available", True),
            "powered": player.get("powered"),
            "state": player.get("state"),
            "provider": player.get("provider"),
        }

    @staticmethod
    def _image_path(obj: dict[str, Any]) -> str | None:
        """Best-effort album-art URL from a queue item, media item, or track."""
        if not isinstance(obj, dict):
            return None
        img = obj.get("image")
        if isinstance(img, dict) and img.get("path"):
            return img.get("path")
        images = (obj.get("metadata") or {}).get("images")
        if isinstance(images, list) and images and isinstance(images[0], dict):
            return images[0].get("path") or images[0].get("url")
        return None

    @classmethod
    def _track_view(cls, item: dict[str, Any]) -> dict[str, Any]:
        """Normalise a queue item / search result / media item to one shape.

        Shared by search results, the Up Next panel, and now-playing so the UI
        only ever deals with `{name, artist, uri, image, duration, item_id}`.
        `item_id` (the queue_item_id) is only present on queue items.
        """
        media = item.get("media_item") or item
        artists = media.get("artists") or []
        first = artists[0] if artists else None
        artist = (
            (first.get("name") if isinstance(first, dict) else first)
            if first
            else media.get("artist")
        )
        return {
            "name": media.get("name") or item.get("name"),
            "artist": artist,
            "uri": media.get("uri") or item.get("uri"),
            "image": cls._image_path(item) or cls._image_path(media),
            "duration": media.get("duration") or item.get("duration"),
            "item_id": item.get("queue_item_id"),
        }

    async def get_players(self) -> list[dict[str, Any]]:
        """List players known to Music Assistant (AirPlay zones, etc.)."""
        try:
            players = await self._command(CMD_PLAYERS_ALL) or []
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not list players: %s — returning cached list (%d players)", err, len(self._cached_players))
            return list(self._cached_players)
        _LOGGER.debug(
            "MA players/all returned %d entries: %s",
            len(players),
            [(p.get("player_id"), p.get("display_name") or p.get("name"), p.get("type"), p.get("available")) for p in players],
        )
        views = [self._player_view(p) for p in players]
        # Keep any player with an id — idle MA players (AirPlay zones, etc.)
        # report available=false until woken, but we still want to list/target
        # them (Start Radio will power them on).
        result = [v for v in views if v["player_id"]]
        if result:
            self._cached_players = result
        return result

    def set_player(self, player_id: str) -> None:
        """Explicitly target a player. Its queue_id == player_id in MA."""
        self.selected_player_id = player_id
        self.active_queue_id = player_id
        _LOGGER.info("Selected MA player: %s", player_id)

    # ------------------------------------------------------------------ #
    # High-level operations
    # ------------------------------------------------------------------ #
    @staticmethod
    def _queue_id_of(q: Any) -> str | None:
        """Extract a queue id from either a dict or a bare string."""
        if isinstance(q, str):
            return q or None
        if isinstance(q, dict):
            return q.get("queue_id") or q.get("id") or None
        return None

    async def refresh_active_queue(self) -> str | None:
        """Resolve the DJ target queue.

        Prefers an explicitly selected player; otherwise picks the queue that's
        currently playing, falling back to the first available one.

        MA 2.8.9 changed player_queues/all to return plain queue-id strings
        instead of full dicts, so we handle both shapes.
        """
        if self.selected_player_id:
            self.active_queue_id = self.selected_player_id
            return self.active_queue_id
        try:
            queues = await self._command(CMD_QUEUES_ALL) or []
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not list queues: %s", err)
            return self.active_queue_id

        _LOGGER.debug("player_queues/all sample: %s", queues[:2] if queues else [])

        # Prefer a playing queue; fall back to first.
        playing = [q for q in queues if isinstance(q, dict) and q.get("state") == "playing"]
        chosen = playing[0] if playing else (queues[0] if queues else None)
        qid = self._queue_id_of(chosen)
        if qid:
            self.active_queue_id = qid
        return self.active_queue_id

    async def get_queue(self, queue_id: str | None = None) -> dict[str, Any]:
        """Return the active queue plus its upcoming items."""
        queue_id = queue_id or self.active_queue_id
        if not queue_id:
            await self.refresh_active_queue()
            queue_id = self.active_queue_id
        if not queue_id:
            return {"queue_id": None, "items": [], "items_remaining": 0}

        queues = await self._command(CMD_QUEUES_ALL) or []
        meta = next(
            (q for q in queues if isinstance(q, dict) and (q.get("queue_id") or q.get("id")) == queue_id),
            {},
        )
        items = await self._command(CMD_QUEUE_ITEMS, queue_id=queue_id) or []

        current_index = meta.get("current_index") or 0
        items_remaining = max(len(items) - current_index - 1, 0)
        return {
            "queue_id": queue_id,
            "current_item": meta.get("current_item"),
            "current_index": current_index,
            "items": items,
            "items_remaining": items_remaining,
            "state": meta.get("state"),
        }

    async def get_history(self, n: int = 20) -> list[dict[str, Any]]:
        """Return the last `n` played items from the active queue."""
        queue = await self.get_queue()
        items = queue.get("items") or []
        idx = queue.get("current_index") or 0
        history = items[max(0, idx - n):idx]
        return history

    async def search_track(self, query: str) -> dict[str, Any] | None:
        """Search MA for a track matching a free-text query."""
        try:
            result = await self._command(
                CMD_SEARCH,
                search_query=query,
                media_types=["track"],
                limit=3,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("MA search failed for %r: %s", query, err)
            return None
        if not result:
            return None
        tracks = result.get("tracks") if isinstance(result, dict) else result
        return tracks[0] if tracks else None

    async def search_tracks(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Search MA for tracks and return normalised view dicts for the UI."""
        try:
            result = await self._command(
                CMD_SEARCH,
                search_query=query,
                media_types=["track"],
                limit=limit,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("MA search failed for %r: %s", query, err)
            return []
        if not result:
            return []
        tracks = result.get("tracks") if isinstance(result, dict) else result
        return [self._track_view(t) for t in (tracks or []) if t.get("uri")]

    async def enqueue_uri(self, uri: str, option: str = "next") -> bool:
        """Queue an already-resolved track URI on the active queue.

        `option="next"` inserts it right after the current track without
        interrupting playback (the dashboard's search → "Play Next"). `option`
        maps straight onto MA's play_media options (also "add" for end-of-queue).
        """
        queue_id = self.active_queue_id or await self.refresh_active_queue()
        if not queue_id:
            _LOGGER.warning("No active queue to enqueue into")
            return False
        try:
            await self._command(
                CMD_PLAY_MEDIA, queue_id=queue_id, media=uri, option=option
            )
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Failed to enqueue %r (%s): %s", uri, option, err)
            return False

    async def remove_queue_item(self, item_id: str) -> bool:
        """Remove an upcoming item from the active queue by its queue_item_id."""
        queue_id = self.active_queue_id or await self.refresh_active_queue()
        if not queue_id:
            return False
        try:
            await self._command(
                CMD_QUEUE_DELETE, queue_id=queue_id, item_id_or_index=item_id
            )
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Failed to remove queue item %s: %s", item_id, err)
            return False

    async def move_queue_item(self, item_id: str, to_index: int) -> bool:
        """Move an upcoming item to an absolute index in the active queue.

        MA's move_item takes a relative `pos_shift`, so resolve the item's
        current index from the queue and shift by the delta.
        """
        queue = await self.get_queue()
        queue_id = queue.get("queue_id")
        if not queue_id:
            return False
        items = queue.get("items") or []
        cur = next(
            (i for i, it in enumerate(items) if it.get("queue_item_id") == item_id),
            None,
        )
        if cur is None:
            _LOGGER.warning("Queue item %s not found for move", item_id)
            return False
        pos_shift = to_index - cur
        if pos_shift == 0:
            return True
        try:
            await self._command(
                CMD_QUEUE_MOVE,
                queue_id=queue_id,
                queue_item_id=item_id,
                pos_shift=pos_shift,
            )
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Failed to move queue item %s: %s", item_id, err)
            return False

    async def clear_queue(self) -> bool:
        """Clear all items from the active queue."""
        queue_id = self.active_queue_id or await self.refresh_active_queue()
        if not queue_id:
            return False
        try:
            await self._command(CMD_QUEUE_CLEAR, queue_id=queue_id)
            _LOGGER.info("Queue %s cleared", queue_id)
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Failed to clear queue %s: %s", queue_id, err)
            return False

    async def enqueue_queries(
        self,
        queries: list[str],
        option: str = "add",
        blocked_uris: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Resolve queries to tracks and queue them on the active queue.

        `option` maps to MA's play_media options:
          * "add"     — append without interrupting playback (normal DJ flow)
          * "play"    — start playing this now (used by Start Radio); the first
                        track plays immediately, the rest are appended.

        `blocked_uris` are dropped after resolution so a temporarily-blocked
        track can never make it back into the queue.

        Returns the list of tracks that were successfully queued.
        """
        queue_id = self.active_queue_id or await self.refresh_active_queue()
        if not queue_id:
            _LOGGER.warning("No active queue to enqueue into")
            return []

        blocked_uris = blocked_uris or set()
        enqueued: list[dict[str, Any]] = []
        seen: set[str] = set()  # dedupe within this batch (two queries → same track)
        first = True
        for query in queries:
            track = await self.search_track(query)
            if not track:
                continue
            uri = track.get("uri")
            if not uri:
                continue
            if uri in blocked_uris or uri in seen:
                _LOGGER.debug("Skipping blocked/duplicate track %s", uri)
                continue
            # Only the first track of a "play" batch interrupts; the rest append.
            this_option = option if (first and option == "play") else "add"
            try:
                await self._command(
                    CMD_PLAY_MEDIA,
                    queue_id=queue_id,
                    media=uri,
                    option=this_option,
                )
                enqueued.append(track)
                seen.add(uri)
                first = False
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Failed to queue %r: %s", query, err)
        return enqueued

    async def ensure_playing(self, player_id: str | None = None) -> bool:
        """Send a play command so the selected player actually starts."""
        player_id = player_id or self.active_queue_id or self.selected_player_id
        if not player_id:
            return False
        try:
            await self._command(CMD_PLAY, player_id=player_id)
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Play command failed: %s", err)
            return False

    async def pause(self) -> bool:
        player_id = self.active_queue_id or self.selected_player_id
        if not player_id:
            return False
        try:
            await self._command(CMD_PAUSE, player_id=player_id)
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Pause failed: %s", err)
            return False

    async def stop(self) -> bool:
        """Stop playback on the active player (does not clear the queue)."""
        player_id = self.active_queue_id or self.selected_player_id
        if not player_id:
            return False
        try:
            await self._command(CMD_STOP, player_id=player_id)
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Stop failed: %s", err)
            return False

    async def play_pause(self) -> bool:
        """Atomically toggle play/pause on the active player.

        Preferred over a read-then-decide toggle: MA flips based on its own
        authoritative state in a single call, avoiding races against the
        eventually-consistent player state (a stale read could send the wrong
        command), and it resumes a paused queue more reliably than a bare play.
        """
        player_id = self.active_queue_id or self.selected_player_id
        if not player_id:
            return False
        try:
            await self._command(CMD_PLAY_PAUSE, player_id=player_id)
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Play/pause toggle failed: %s", err)
            return False

    async def get_play_state(self) -> str | None:
        """Return the active player's playback state ("playing"|"paused"|"idle").

        The player object carries the authoritative playback state; the queue
        meta `state` doesn't always reflect a pause, so prefer the player and
        fall back to the queue.
        """
        target = self.selected_player_id or self.active_queue_id
        if target:
            try:
                for player in await self.get_players():
                    if player.get("player_id") == target:
                        state = player.get("state")
                        if state:
                            return str(state).lower()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("get_play_state via players failed: %s", err)
        try:
            state = (await self.get_queue()).get("state")
            return str(state).lower() if state else None
        except Exception:  # noqa: BLE001
            return None

    async def skip(self) -> bool:
        queue = await self.get_queue()
        player_id = queue.get("queue_id")
        if not player_id:
            return False
        try:
            await self._command(CMD_NEXT, player_id=player_id)
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Skip failed: %s", err)
            return False

    async def get_library_tracks(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            return await self._command(CMD_LIBRARY_TRACKS, limit=limit) or []
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("get_library_tracks failed: %s", err)
            return []

    # ------------------------------------------------------------------ #
    # Favorites + playlists (synced to the track's source provider via MA)
    # ------------------------------------------------------------------ #
    async def add_favorite(self, uri: str) -> bool:
        """Favorite a track in MA, which syncs to the track's source provider.

        The URI scheme (e.g. tidal://, spotify://) determines which provider MA
        routes the favorite to — this works with any configured music source."""
        if not uri:
            return False
        try:
            await self._command(CMD_FAVORITE_ADD, item=uri)
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("add_favorite failed for %s: %s", uri, err)
            raise

    async def create_playlist(
        self, name: str, provider: str | None = None
    ) -> dict[str, Any]:
        """Create a playlist on the given music provider (e.g. 'tidal', 'spotify').

        Returns the created playlist object (carries item_id / uri).
        """
        args: dict[str, Any] = {"name": name}
        if provider:
            args["provider_instance_or_domain"] = provider
        result = await self._command(CMD_PLAYLIST_CREATE, **args)
        return result or {}

    async def add_playlist_tracks(self, playlist_id: str, uris: list[str]) -> None:
        """Append tracks (by uri) to an existing library playlist."""
        if not playlist_id or not uris:
            return
        await self._command(
            CMD_PLAYLIST_ADD_TRACKS, db_playlist_id=playlist_id, uris=uris
        )
