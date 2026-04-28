"""SimpleX Chat platform adapter.

Connects to a user-run ``simplex-chat`` daemon over its WebSocket JSON-RPC
protocol. The daemon handles SimpleX's onion routing and SMP server I/O;
this adapter just translates between Hermes and the daemon.

Requires:
  - A running ``simplex-chat`` instance reachable at SIMPLEX_WS_URL
    (default port 5225). The daemon's WS is unauthenticated, so the
    network path between Hermes and the daemon must be trusted (LAN or
    behind a TLS+auth reverse proxy).
  - ``SIMPLEX_GROUP_IDS`` listing the numeric group IDs Hermes should
    listen in. Discover them with ``hermes simplex join <invite-link>``
    or ``hermes simplex list``.

v1 scope (text + groups only):
  - Inbound text messages from configured groups → MessageEvent.
  - Outbound text via ``/_send #<groupId> json [...]``.
  - Self-echo filtered via ``chatDir.type == 'groupRcv'``.
  - Allowlist enforced on the sender's display name
    (SIMPLEX_ALLOWED_USERS), with the same shape as Signal/WhatsApp.

Known v1 gaps (tracked for v1.1):
  - No images / files / voice / typing indicators / streaming edits.
  - No missed-message replay across Hermes restarts. simplex-chat
    persists messages received during downtime in its own DB but does
    not re-emit them to a re-connecting WS client. The user sees a
    delivered message but Hermes doesn't process it. To close this gap
    we'd need to call ``/_get_chat #<gid> count=N`` after reconnect and
    filter by a persisted ``last_seen_item_id`` per group — deferred
    until we can validate the response shape against a real daemon.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Any, Dict, List, Optional
from uuid import uuid4

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.platforms.simplex_client import (
    SimplexChatClient,
    SimplexConnectionClosed,
    SimplexProtocolError,
)

logger = logging.getLogger(__name__)


MAX_MESSAGE_LENGTH = 4000


def check_simplex_requirements() -> bool:
    """Return True iff the env has enough config for the SimpleX adapter."""
    return bool(os.getenv("SIMPLEX_WS_URL") and os.getenv("SIMPLEX_GROUP_IDS"))


class SimplexAdapter(BasePlatformAdapter):
    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.SIMPLEX)

        extra = config.extra or {}
        self._ws_url: str = extra.get("ws_url", "")
        # group_ids stored as strings to match Hermes chat_id convention,
        # parsed to int at the wire boundary.
        self._allowed_group_ids: set[str] = set(extra.get("group_ids") or [])
        self._max_reconnect_delay: float = float(
            extra.get("max_reconnect_delay_s", 60)
        )
        self._initial_reconnect_delay: float = 5.0

        self._client: Optional[SimplexChatClient] = None
        self._supervisor_task: Optional[asyncio.Task] = None
        self._stopping = False
        self._send_lock = asyncio.Lock()

        # Cached on connect, refreshed when /groups changes.
        self._known_groups: Dict[int, str] = {}
        self._self_display_name: str = ""

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        if not self._ws_url:
            self._set_fatal_error(
                "MISSING_CONFIG",
                "SIMPLEX_WS_URL is not configured",
                retryable=False,
            )
            return False
        if not self._allowed_group_ids:
            self._set_fatal_error(
                "MISSING_CONFIG",
                "SIMPLEX_GROUP_IDS is empty — set it via 'hermes simplex join' or env",
                retryable=False,
            )
            return False

        self._stopping = False
        if not await self._connect_once():
            return False
        self._supervisor_task = asyncio.create_task(
            self._supervisor(), name="simplex-supervisor"
        )
        self._mark_connected()
        logger.info(
            "simplex: connected to %s (bot=%s, groups=%s)",
            self._ws_url,
            self._self_display_name or "?",
            sorted(self._allowed_group_ids),
        )
        return True

    async def disconnect(self) -> None:
        self._stopping = True
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._supervisor_task = None
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
        self._mark_disconnected()

    # ── Outbound ────────────────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> SendResult:
        try:
            group_id = int(chat_id)
        except (TypeError, ValueError):
            return SendResult(
                success=False,
                error=f"simplex: chat_id must be a numeric group id, got {chat_id!r}",
            )

        if self._client is None:
            return SendResult(
                success=False,
                error="simplex: not connected",
                retryable=True,
            )

        text = content if len(content) <= MAX_MESSAGE_LENGTH else content[:MAX_MESSAGE_LENGTH]
        async with self._send_lock:
            try:
                resp = await self._client.api_send_text_message_to_group(
                    group_id, text
                )
            except SimplexConnectionClosed as e:
                return SendResult(
                    success=False, error=f"simplex: {e}", retryable=True
                )
            except Exception as e:
                logger.warning("simplex send failed: %r", e)
                return SendResult(success=False, error=str(e))

        return SendResult(
            success=True,
            message_id=str(resp.item_id) if resp.item_id is not None else None,
            raw_response=resp.raw,
        )

    async def send_typing(
        self, chat_id: str, metadata: Optional[Dict] = None
    ) -> None:
        # simplex-chat has no typing API — silently drop.
        logger.debug("simplex: send_typing is a no-op (chat_id=%s)", chat_id)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
    ) -> SendResult:
        # v1: text only.  simplex-chat supports images via /_send with
        # msgContent.type == "image" but the protocol is bigger and out
        # of scope.
        return SendResult(
            success=False,
            error="simplex: image send not implemented in v1",
        )

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        try:
            gid = int(chat_id)
        except (TypeError, ValueError):
            return {"name": chat_id, "type": "group", "chat_id": chat_id}
        name = self._known_groups.get(gid)
        if name is None and self._client is not None:
            # Lazy refresh — group may have been joined since connect.
            try:
                groups = await self._client.api_get_groups()
                self._known_groups = {g.group_id: g.display_name for g in groups}
                name = self._known_groups.get(gid)
            except Exception as e:
                logger.debug("simplex: get_chat_info refresh failed: %r", e)
        return {"name": name or str(gid), "type": "group", "chat_id": chat_id}

    # ── Internals ───────────────────────────────────────────────────────

    async def _connect_once(self) -> bool:
        """Open WS, health-check, verify groups. Returns True on success."""
        self._client = SimplexChatClient(self._ws_url)
        try:
            await self._client.connect()
        except Exception as e:
            logger.warning(
                "simplex: cannot reach daemon at %s: %r", self._ws_url, e
            )
            self._set_fatal_error(
                "DAEMON_UNREACHABLE",
                f"simplex-chat daemon at {self._ws_url} is not reachable: {e}",
                retryable=True,
            )
            await self._safe_close_client()
            return False

        try:
            user = await self._client.api_get_active_user()
            self._self_display_name = user.display_name or ""
        except (SimplexProtocolError, SimplexConnectionClosed) as e:
            logger.warning("simplex: no active user on daemon: %r", e)
            self._set_fatal_error(
                "DAEMON_NO_USER",
                "simplex-chat daemon has no active user — start it with --create-bot-display-name",
                retryable=False,
            )
            await self._safe_close_client()
            return False
        except Exception as e:
            logger.warning("simplex: handshake error: %r", e)
            self._set_fatal_error(
                "DAEMON_HANDSHAKE",
                f"handshake failed: {e}",
                retryable=True,
            )
            await self._safe_close_client()
            return False

        await self._refresh_known_groups()
        return True

    async def _safe_close_client(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.close()
        except Exception:
            pass
        self._client = None

    async def _refresh_known_groups(self) -> None:
        if self._client is None:
            return
        try:
            groups = await self._client.api_get_groups()
        except Exception as e:
            logger.warning("simplex: failed to list groups: %r", e)
            return
        self._known_groups = {g.group_id: g.display_name for g in groups}
        present = {str(gid) for gid in self._known_groups}
        missing = self._allowed_group_ids - present
        if missing:
            logger.warning(
                "simplex: configured groups not present on daemon: %s "
                "(rejoin with 'hermes simplex join' or remove from SIMPLEX_GROUP_IDS)",
                sorted(missing),
            )

    async def _supervisor(self) -> None:
        delay = self._initial_reconnect_delay
        while not self._stopping:
            try:
                await self._event_loop()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("simplex: event loop crashed: %r", e)

            if self._stopping:
                return

            await self._safe_close_client()
            self._mark_disconnected()

            jittered = delay + random.uniform(0, delay * 0.3)
            logger.info("simplex: reconnecting in %.1fs", jittered)
            try:
                await asyncio.sleep(jittered)
            except asyncio.CancelledError:
                return
            delay = min(delay * 2, self._max_reconnect_delay)

            if await self._connect_once():
                delay = self._initial_reconnect_delay
                self._mark_connected()
                logger.info("simplex: reconnected to %s", self._ws_url)
            else:
                # _connect_once already set a fatal error; loop until
                # disconnect() is called externally.  We still keep trying
                # because the error was marked retryable.
                if not self._fatal_error_retryable:
                    self._stopping = True
                    return

    async def _event_loop(self) -> None:
        """Iterate the client's event stream until it ends."""
        if self._client is None:
            return
        async for event in self._client.events():
            if not isinstance(event, dict):
                continue
            if event.get("type") != "newChatItems":
                continue
            for item in event.get("chatItems") or []:
                await self._dispatch_chat_item(item)

    async def _dispatch_chat_item(self, item: Dict[str, Any]) -> None:
        chat_info = item.get("chatInfo") or {}
        chat_item = item.get("chatItem") or {}

        if chat_info.get("type") != "group":
            return

        chat_dir = chat_item.get("chatDir") or {}
        # Self-echo filter: outbound messages arrive as groupSnd.
        if chat_dir.get("type") != "groupRcv":
            return

        content = chat_item.get("content") or {}
        if content.get("type") != "rcvMsgContent":
            return

        msg_content = content.get("msgContent") or {}
        if msg_content.get("type") != "text":
            return  # v1: text only

        group_info = chat_info.get("groupInfo") or {}
        group_id = group_info.get("groupId")
        if group_id is None:
            return

        gid_str = str(group_id)
        if gid_str not in self._allowed_group_ids:
            logger.debug("simplex: dropping message from unsubscribed group %s", gid_str)
            return

        # Cache the friendly name for get_chat_info().
        if isinstance(group_id, int):
            self._known_groups[group_id] = group_info.get(
                "displayName", self._known_groups.get(group_id, gid_str)
            )

        member = chat_dir.get("groupMember") or {}
        sender = (
            member.get("localDisplayName")
            or (member.get("memberProfile") or {}).get("displayName")
            or "unknown"
        )
        text = msg_content.get("text") or ""
        meta = chat_item.get("meta") or {}
        item_id = meta.get("itemId")
        message_id = str(item_id) if isinstance(item_id, int) else uuid4().hex

        source = self.build_source(
            chat_id=gid_str,
            chat_name=group_info.get("displayName", gid_str),
            chat_type="group",
            user_id=sender,
            user_name=sender,
        )
        event_obj = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=message_id,
            raw_message=item,
        )
        try:
            await self.handle_message(event_obj)
        except Exception as e:
            logger.exception("simplex: handle_message raised: %r", e)
