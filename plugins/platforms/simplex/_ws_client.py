"""Minimal request/response WebSocket client for the simplex-chat JSON-RPC protocol.

Used by the ``hermes simplex`` CLI for one-off discovery / join commands.
The platform adapter (``adapter.py``) maintains its own long-lived WS
connection with a full event-stream pump; this module is intentionally
narrower so the CLI doesn't pull in the adapter's runtime state.

Wire format mirrors the official ``simplex-chat`` npm client:

    outgoing  {"corrId": "1", "cmd": "/u"}
    response  {"corrId": "1", "resp": {"type": "activeUser", "user": {...}}}
    event     {"resp": {"type": "newChatItems", "chatItems": [...]}}

Events (no corrId) are silently dropped — CLI commands are request/response
only. Responses (with corrId) resolve the matching Future from
:meth:`SimplexChatClient.send_chat_cmd`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

try:
    import websockets
    from websockets.asyncio.client import ClientConnection
except ImportError as e:
    raise ImportError(
        "The 'websockets' package is required for SimpleX CLI commands. "
        "Install with: pip install -e '.[simplex]' (or '.[all]')"
    ) from e


logger = logging.getLogger(__name__)


class SimplexProtocolError(Exception):
    """The daemon returned a response that doesn't match what we asked for."""

    def __init__(self, message: str, response: Optional[dict] = None) -> None:
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
class ContactInfo:
    contact_id: int
    display_name: str
    raw: dict


ConnectFactory = Callable[[str], Awaitable["ClientConnection"]]


class SimplexChatClient:
    """Single-WS, request/response client for simplex-chat.

    Use as an async context manager:

        async with SimplexChatClient(ws_url) as client:
            user = await client.api_get_active_user()
    """

    def __init__(
        self,
        ws_url: str,
        *,
        request_timeout: float = 30.0,
        connect_factory: Optional[ConnectFactory] = None,
    ) -> None:
        self._ws_url = ws_url
        self._request_timeout = request_timeout
        self._connect_factory = connect_factory or self._default_connect
        self._ws: Optional[ClientConnection] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._corr_id = 0
        self._closed = False
        self._send_lock = asyncio.Lock()

    @staticmethod
    async def _default_connect(url: str) -> "ClientConnection":
        return await websockets.connect(url, max_size=2**24)

    async def connect(self) -> None:
        if self._ws is not None:
            return
        self._ws = await self._connect_factory(self._ws_url)
        self._closed = False
        self._recv_task = asyncio.create_task(
            self._recv_loop(), name="simplex-cli-recv"
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
        self._ws = None
        self._recv_task = None

    async def __aenter__(self) -> "SimplexChatClient":
        await self.connect()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
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
                fut,
                timeout=timeout if timeout is not None else self._request_timeout,
            )
        except (asyncio.TimeoutError, Exception):
            self._pending.pop(corr_id, None)
            raise

    async def api_get_active_user(self) -> ActiveUser:
        resp = await self.send_chat_cmd("/u")
        if resp.get("type") != "activeUser":
            raise SimplexProtocolError("expected activeUser", resp)
        user = resp.get("user") or {}
        profile = user.get("profile") or {}
        return ActiveUser(
            user_id=int(user.get("userId") or 0),
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
            gid = info.get("groupId") or info.get("id")
            if gid is None:
                continue
            profile = info.get("groupProfile") or {}
            display = (
                info.get("displayName")
                or profile.get("displayName")
                or profile.get("fullName")
                or ""
            )
            out.append(
                GroupInfo(group_id=int(gid), display_name=display, raw=entry)
            )
        return out

    async def api_get_contacts(self) -> list[ContactInfo]:
        resp = await self.send_chat_cmd("/contacts")
        if resp.get("type") != "contactsList":
            raise SimplexProtocolError("expected contactsList", resp)
        out: list[ContactInfo] = []
        for entry in resp.get("contacts") or []:
            cid = entry.get("contactId") or entry.get("id")
            if cid is None:
                continue
            profile = entry.get("profile") or {}
            display = (
                entry.get("localDisplayName")
                or profile.get("displayName")
                or entry.get("displayName")
                or ""
            )
            out.append(
                ContactInfo(contact_id=int(cid), display_name=display, raw=entry)
            )
        return out

    async def api_connect(self, invitation_link: str) -> dict:
        return await self.send_chat_cmd(f"/c {invitation_link}")

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("simplex cli: dropping non-JSON frame")
                    continue
                if not isinstance(msg, dict):
                    continue
                corr_id = msg.get("corrId")
                resp = msg.get("resp")
                if corr_id is None:
                    continue
                fut = self._pending.pop(str(corr_id), None)
                if fut is None or fut.done():
                    logger.debug("simplex cli: orphan response corrId=%s", corr_id)
                    continue
                fut.set_result(resp if resp is not None else msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.info("simplex cli: recv loop terminated: %r", e)
        finally:
            self._fail_pending(SimplexConnectionClosed("ws closed"))

    def _fail_pending(self, exc: BaseException) -> None:
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(exc)
