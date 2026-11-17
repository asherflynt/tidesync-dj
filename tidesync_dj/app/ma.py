"""Thin Music Assistant WebSocket client (v2).

Deliberately small: one connection, request/response by message id, chunked
("partial") results reassembled, server events fanned out to listeners, and
automatic reconnect. It knows nothing about DJ logic — callers pass command
names, so a renamed MA command is a one-line change at the call site.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

_LOGGER = logging.getLogger("tidesync.ma")

EventListener = Callable[[str, dict[str, Any]], Awaitable[None] | None]


class MAError(RuntimeError):
    """A command was rejected by Music Assistant."""

    def __init__(self, command: str, code: Any, details: str) -> None:
        super().__init__(f"{command}: {details}")
        self.command = command
        self.code = code
        self.details = details

    @property
    def unsupported(self) -> bool:
        """True when the server does not know the command (provider off / older MA).

        Music Assistant 2.10 answers "Invalid command: <name>"; older servers
        said "unsupported command".
        """
        details = self.details.lower()
        return "unsupported command" in details or "invalid command" in details

    @property
    def not_permitted(self) -> bool:
        """True when the token lacks the scope the command needs (e.g. admin-only)."""
        details = self.details.lower()
        return ("requires the" in details and "scope" in details) or "authentication required" in details


class MAUnavailable(RuntimeError):
    """No usable connection to Music Assistant."""


class MAClient:
    """One authenticated WebSocket session to Music Assistant."""

    def __init__(
        self,
        url: str,
        *,
        token: str = "",
        username: str = "",
        password: str = "",
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self.url = url
        self._token = token
        self._username = username
        self._password = password
        self._connect = connect or websockets.connect
        self._ws: Any = None
        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._partials: dict[str, list[Any]] = {}
        self._listeners: list[EventListener] = []
        self._reader: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.server_info: dict[str, Any] = {}

    # -- connection ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._reader is not None and not self._reader.done()

    @property
    def server_version(self) -> str:
        return str(self.server_info.get("server_version") or "")

    async def ensure_connected(self) -> None:
        if self.connected:
            return
        async with self._lock:
            if self.connected:
                return
            await self._open()

    async def _open(self) -> None:
        await self._teardown()
        try:
            ws = await self._connect(self.url, max_size=None)
            self.server_info = json.loads(await asyncio.wait_for(ws.recv(), 15))
        except Exception as err:
            raise MAUnavailable(f"Cannot reach Music Assistant at {self.url}: {err}") from err
        self._ws = ws
        self._reader = asyncio.create_task(self._read_loop(ws))
        try:
            if self._token:
                await self._send("auth", {"token": self._token})
            elif self._username:
                res = await self._send(
                    "auth/login", {"username": self._username, "password": self._password}
                )
                token = (res or {}).get("token") or (res or {}).get("access_token")
                if token:
                    await self._send("auth", {"token": token})
        except MAError as err:
            await self._teardown()
            raise MAUnavailable(f"Music Assistant rejected the credentials: {err.details}") from err
        _LOGGER.info("Connected to Music Assistant %s", self.server_version)

    async def _teardown(self) -> None:
        reader, ws = self._reader, self._ws
        self._reader = self._ws = None
        if reader and reader is not asyncio.current_task():
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        self._fail_pending(MAUnavailable("Connection to Music Assistant closed"))

    async def close(self) -> None:
        async with self._lock:
            await self._teardown()

    def _fail_pending(self, err: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()
        self._partials.clear()

    # -- wire ---------------------------------------------------------------

    async def _read_loop(self, ws: Any) -> None:
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if "message_id" in msg:
                    self._on_reply(msg)
                elif "event" in msg:
                    await self._on_event(msg)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - connection died; pending calls must fail
            _LOGGER.warning("Music Assistant connection lost: %s", err)
        finally:
            if self._ws is ws:
                self._ws = None
            self._fail_pending(MAUnavailable("Connection to Music Assistant lost"))

    def _on_reply(self, msg: dict[str, Any]) -> None:
        mid = str(msg["message_id"])
        fut = self._pending.get(mid)
        if fut is None or fut.done():
            return
        if "error_code" in msg:
            self._pending.pop(mid, None)
            self._partials.pop(mid, None)
            fut.set_exception(
                MAError(str(msg.get("command", "")), msg["error_code"], str(msg.get("details") or ""))
            )
            return
        result = msg.get("result")
        if msg.get("partial"):
            self._partials.setdefault(mid, []).extend(result or [])
            return
        parts = self._partials.pop(mid, None)
        if parts is not None:
            result = parts + list(result or [])
        self._pending.pop(mid, None)
        fut.set_result(result)

    async def _on_event(self, msg: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            try:
                res = listener(str(msg.get("event")), msg)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:  # noqa: BLE001 - a bad listener must not kill the reader
                _LOGGER.exception("Event listener failed")

    async def _send(self, command: str, args: dict[str, Any] | None, timeout: float = 60) -> Any:
        ws = self._ws
        if ws is None:
            raise MAUnavailable("Not connected to Music Assistant")
        mid = str(next(self._ids))
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        payload: dict[str, Any] = {"command": command, "message_id": mid}
        if args:
            payload["args"] = args
        try:
            await ws.send(json.dumps(payload))
            return await asyncio.wait_for(fut, timeout)
        except MAError as err:
            err.command = command
            raise MAError(command, err.code, err.details) from None
        except asyncio.TimeoutError:
            raise MAUnavailable(f"{command} timed out after {timeout:.0f}s") from None
        except websockets.ConnectionClosed as err:
            raise MAUnavailable(f"Connection closed during {command}") from err
        finally:
            self._pending.pop(mid, None)
            self._partials.pop(mid, None)

    # -- public -------------------------------------------------------------

    async def cmd(self, command: str, *, timeout: float = 60, **args: Any) -> Any:
        """Run a command and return its result. Reconnects once if the link dropped.

        ``None``-valued arguments are omitted so callers can pass optionals freely.
        """
        clean = {k: v for k, v in args.items() if v is not None}
        await self.ensure_connected()
        try:
            return await self._send(command, clean, timeout)
        except MAUnavailable:
            await self.ensure_connected()
            return await self._send(command, clean, timeout)

    async def try_cmd(self, command: str, *, default: Any = None, timeout: float = 60, **args: Any) -> Any:
        """Like :meth:`cmd` but returns ``default`` when MA rejects the command."""
        try:
            return await self.cmd(command, timeout=timeout, **args)
        except MAError as err:
            _LOGGER.debug("%s rejected: %s", command, err.details)
            return default

    def subscribe(self, listener: EventListener) -> Callable[[], None]:
        """Register a listener for server events; returns an unsubscribe function."""
        self._listeners.append(listener)

        def _remove() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return _remove
