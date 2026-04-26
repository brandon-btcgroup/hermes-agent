"""Minimal async WebSocket client for the simplex-chat JSON-RPC protocol.

Wire format mirrors the official `simplex-chat` npm package:

  outgoing:  {"corrId": "1", "cmd": "/u"}
  response:  {"corrId": "1", "resp": {"type": "activeUser", "user": {...}}}
  event:     {"resp": {"type": "newChatItems", "chatItems": [...]}}

Events (no corrId) are pushed to an async queue exposed via `events()`.
Responses (with corrId) resolve the matching Future from `send_chat_cmd`.

Reconnect is the caller's job; this client treats a closed WS as terminal.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

try:
    import websockets
    from websockets.asyncio.client import ClientConnection
except ImportError as e:
    raise ImportError(
        "The 'websockets' package is required for SimpleX support. "
        "Install with: pip install -e '.[simplex]' (or '.[all]')"
    ) from e


logger = logging.getLogger(__name__)


class SimplexProtocolError(Exception):
    """The daemon returned a response that doesn't match what we asked for."""

    def __init__(self, message: str, response: Optional[dict] = None):
        super().__init__(message)
        self.response = response


class SimplexConnectionClosed(Exception):
    """The WS connection closed while we still had work to do."""


@dataclass
class ActiveUser:
    user_id: int
    display_name: str
    raw: dict


@dataclass
class GroupInfo:
    group_id: int
    display_name: str
    raw: dict


@dataclass
class SendResponse:
    item_id: Optional[int]
    raw: dict


ConnectFactory = Callable[[str], Awaitable[ClientConnection]]


class SimplexChatClient:
    """Single-WS, request/response + event-stream client for simplex-chat."""

    def __init__(
        self,
        ws_url: str,
        *,
        request_timeout: float = 30.0,
        connect_factory: Optional[ConnectFactory] = None,
        event_queue_size: int = 1024,
    ):
        self._ws_url = ws_url
        self._request_timeout = request_timeout
        self._connect_factory = connect_factory or self._default_connect
        self._ws: Optional[ClientConnection] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._events: asyncio.Queue = asyncio.Queue(maxsize=event_queue_size)
        self._corr_id = 0
        self._closed = False
        self._send_lock = asyncio.Lock()

    @staticmethod
    async def _default_connect(url: str) -> ClientConnection:
        return await websockets.connect(url, max_size=2**24)

    async def connect(self) -> None:
        if self._ws is not None:
            return
        self._ws = await self._connect_factory(self._ws_url)
        self._closed = False
        self._recv_task = asyncio.create_task(
            self._recv_loop(), name="simplex-recv"
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._recv_task is not None:
            self._recv_task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._recv_task is not None:
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
        self._fail_pending(SimplexConnectionClosed("client closed"))
        # Sentinel so consumers of events() unblock and exit cleanly.
        try:
            self._events.put_nowait(None)
        except asyncio.QueueFull:
            pass
        self._ws = None
        self._recv_task = None

    async def __aenter__(self) -> "SimplexChatClient":
        await self.connect()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def send_chat_cmd(
        self, cmd: str, *, timeout: Optional[float] = None
    ) -> dict:
        if self._ws is None or self._closed:
            raise SimplexConnectionClosed("not connected")
        self._corr_id += 1
        corr_id = str(self._corr_id)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[corr_id] = fut
        payload = json.dumps({"corrId": corr_id, "cmd": cmd})
        try:
            async with self._send_lock:
                await self._ws.send(payload)
            return await asyncio.wait_for(
                fut, timeout=timeout if timeout is not None else self._request_timeout
            )
        except asyncio.TimeoutError:
            self._pending.pop(corr_id, None)
            raise
        except Exception:
            self._pending.pop(corr_id, None)
            raise

    def events(self) -> AsyncIterator[dict]:
        return _EventIterator(self._events)

    # ── high-level helpers ──────────────────────────────────────────────

    async def api_get_active_user(self) -> ActiveUser:
        resp = await self.send_chat_cmd("/u")
        if resp.get("type") != "activeUser":
            raise SimplexProtocolError("expected activeUser", resp)
        user = resp.get("user") or {}
        profile = user.get("profile") or {}
        return ActiveUser(
            user_id=user.get("userId"),
            display_name=profile.get("displayName", ""),
            raw=user,
        )

    async def api_get_groups(self) -> list[GroupInfo]:
        resp = await self.send_chat_cmd("/groups")
        if resp.get("type") != "groupsList":
            raise SimplexProtocolError("expected groupsList", resp)
        out: list[GroupInfo] = []
        for entry in resp.get("groups") or []:
            info = entry.get("groupInfo") or {}
            gid = info.get("groupId")
            if gid is None:
                continue
            out.append(
                GroupInfo(
                    group_id=int(gid),
                    display_name=info.get("displayName", ""),
                    raw=entry,
                )
            )
        return out

    async def api_connect(self, invitation_link: str) -> dict:
        return await self.send_chat_cmd(f"/c {invitation_link}")

    async def api_send_text_message_to_group(
        self, group_id: int, text: str
    ) -> SendResponse:
        body = json.dumps(
            [{"msgContent": {"type": "text", "text": text}, "mentions": {}}]
        )
        cmd = f"/_send #{group_id} json {body}"
        resp = await self.send_chat_cmd(cmd)
        if resp.get("type") != "newChatItems":
            raise SimplexProtocolError("expected newChatItems", resp)
        items = resp.get("chatItems") or []
        item_id: Optional[int] = None
        if items:
            meta = (items[0].get("chatItem") or {}).get("meta") or {}
            raw_id = meta.get("itemId")
            if isinstance(raw_id, int):
                item_id = raw_id
        return SendResponse(item_id=item_id, raw=resp)

    # ── internals ───────────────────────────────────────────────────────

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("simplex: dropping non-JSON frame")
                    continue
                if not isinstance(msg, dict):
                    continue
                corr_id = msg.get("corrId")
                resp = msg.get("resp")
                if corr_id is not None:
                    fut = self._pending.pop(str(corr_id), None)
                    if fut is None or fut.done():
                        logger.debug("simplex: orphan response corrId=%s", corr_id)
                        continue
                    fut.set_result(resp if resp is not None else msg)
                else:
                    payload = resp if resp is not None else msg
                    try:
                        self._events.put_nowait(payload)
                    except asyncio.QueueFull:
                        logger.warning("simplex: event queue full, dropping event")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.info("simplex: recv loop terminated: %r", e)
        finally:
            self._fail_pending(SimplexConnectionClosed("ws closed"))
            try:
                self._events.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def _fail_pending(self, exc: BaseException) -> None:
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(exc)


class _EventIterator:
    """Async iterator yielding events until a None sentinel is dequeued."""

    def __init__(self, queue: asyncio.Queue):
        self._queue = queue

    def __aiter__(self) -> "_EventIterator":
        return self

    async def __anext__(self) -> dict:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item
