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
CMD_SEARCH = "music/search"
CMD_LIBRARY_TRACKS = "music/tracks/library_items"
# Favorites + playlist management (used for "like" and "save session").
CMD_FAVORITE_ADD = "music/favorites/add_item"
CMD_PLAYLIST_CREATE = "music/playlists/create_playlist"
CMD_PLAYLIST_ADD_TRACKS = "music/playlists/add_playlist_tracks"

# Event types we care about.
EVENT_QUEUE_UPDATED = "queue_updated"
EVENT_QUEUE_TIME_UPDATED = "queue_time_updated"

EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


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

    @property
    def is_connected(self) -> bool:
        """True only when the socket is open AND we're authenticated."""
        return self._is_open and self._authed

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #
    def on_event(self, cb: EventCallback) -> None:
        self._event_cb = cb

    async def connect(self) -> None:
        """Connect, handshake, and authenticate. Raises on failure."""
        _LOGGER.info("Connecting to Music Assistant at %s", self._ws_url)
        self._ws = await websockets.connect(self._ws_url, max_size=None)
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
                # Connected and authenticated. Hold until the socket drops; a
                # drop here is transient (e.g. idle reset) and recovers quietly.
                try:
                    await self.refresh_active_queue()
                    if self._recv_task:
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
            await self._event_cb(msg["event"], msg.get("data") or msg)

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
        await self._ws.send(json.dumps(payload))
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

    async def get_players(self) -> list[dict[str, Any]]:
        """List players known to Music Assistant (AirPlay zones, etc.)."""
        try:
            players = await self._command(CMD_PLAYERS_ALL) or []
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not list players: %s", err)
            return []
        views = [self._player_view(p) for p in players]
        # Keep any player with an id — idle MA players (AirPlay zones, etc.)
        # report available=false until woken, but we still want to list/target
        # them (Start Radio will power them on).
        return [v for v in views if v["player_id"]]

    def set_player(self, player_id: str) -> None:
        """Explicitly target a player. Its queue_id == player_id in MA."""
        self.selected_player_id = player_id
        self.active_queue_id = player_id
        _LOGGER.info("Selected MA player: %s", player_id)

    # ------------------------------------------------------------------ #
    # High-level operations
    # ------------------------------------------------------------------ #
    async def refresh_active_queue(self) -> str | None:
        """Resolve the DJ target queue.

        Prefers an explicitly selected player; otherwise picks the queue that's
        currently playing, falling back to the first available one.
        """
        if self.selected_player_id:
            self.active_queue_id = self.selected_player_id
            return self.active_queue_id
        try:
            queues = await self._command(CMD_QUEUES_ALL) or []
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not list queues: %s", err)
            return self.active_queue_id

        playing = [q for q in queues if q.get("state") == "playing"]
        chosen = playing[0] if playing else (queues[0] if queues else None)
        if chosen:
            self.active_queue_id = chosen.get("queue_id") or chosen.get("id")
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
            (q for q in queues if (q.get("queue_id") or q.get("id")) == queue_id),
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

    async def enqueue_queries(
        self, queries: list[str], option: str = "add"
    ) -> list[dict[str, Any]]:
        """Resolve queries to tracks and queue them on the active queue.

        `option` maps to MA's play_media options:
          * "add"     — append without interrupting playback (normal DJ flow)
          * "play"    — start playing this now (used by Start Radio); the first
                        track plays immediately, the rest are appended.

        Returns the list of tracks that were successfully queued.
        """
        queue_id = self.active_queue_id or await self.refresh_active_queue()
        if not queue_id:
            _LOGGER.warning("No active queue to enqueue into")
            return []

        enqueued: list[dict[str, Any]] = []
        first = True
        for query in queries:
            track = await self.search_track(query)
            if not track:
                continue
            uri = track.get("uri")
            if not uri:
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
    # Favorites + playlists (Tidal sync via MA)
    # ------------------------------------------------------------------ #
    async def add_favorite(self, uri: str) -> bool:
        """Favorite a track in MA, which syncs to the source provider (Tidal)."""
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
        """Create a playlist on the given provider (e.g. 'tidal').

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
