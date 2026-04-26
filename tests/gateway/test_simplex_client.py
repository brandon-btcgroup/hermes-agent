"""Unit tests for SimplexChatClient — protocol layer only, no Hermes deps."""

from __future__ import annotations

import asyncio
import json

import pytest

from gateway.platforms.simplex_client import (
    ActiveUser,
    GroupInfo,
    SendResponse,
    SimplexChatClient,
    SimplexConnectionClosed,
    SimplexProtocolError,
)


class FakeWS:
    """Minimal stand-in for websockets.ClientConnection.

    Tests push frames via ``server_send`` and inspect what the client
    sent via ``outgoing``.
    """

    def __init__(self) -> None:
        self.outgoing: list[str] = []
        self._inbox: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send(self, message: str) -> None:
        if self.closed:
            raise ConnectionError("ws closed")
        self.outgoing.append(message)

    async def close(self) -> None:
        self.closed = True
        await self._inbox.put(None)

    def __aiter__(self) -> "FakeWS":
        return self

    async def __anext__(self) -> str:
        item = await self._inbox.get()
        if item is None:
            raise StopAsyncIteration
        return item

    def server_send(self, payload: dict | str) -> None:
        msg = payload if isinstance(payload, str) else json.dumps(payload)
        self._inbox.put_nowait(msg)

    def server_close(self) -> None:
        self._inbox.put_nowait(None)


def _make_client(ws: FakeWS) -> SimplexChatClient:
    async def factory(_url: str) -> FakeWS:
        return ws

    return SimplexChatClient(
        "ws://test", request_timeout=2.0, connect_factory=factory
    )


@pytest.mark.asyncio
async def test_connect_and_close_cycle():
    ws = FakeWS()
    client = _make_client(ws)
    await client.connect()
    assert client._ws is ws
    await client.close()
    assert ws.closed
    assert client._ws is None


@pytest.mark.asyncio
async def test_send_chat_cmd_corrid_roundtrip():
    ws = FakeWS()
    client = _make_client(ws)
    await client.connect()

    # Issue command, then have the "server" reply with a matching corrId.
    async def reply_when_seen():
        for _ in range(50):
            if ws.outgoing:
                req = json.loads(ws.outgoing[-1])
                ws.server_send({"corrId": req["corrId"], "resp": {"type": "ok"}})
                return
            await asyncio.sleep(0.01)

    asyncio.create_task(reply_when_seen())
    resp = await client.send_chat_cmd("/u")
    assert resp == {"type": "ok"}
    sent = json.loads(ws.outgoing[0])
    assert sent["cmd"] == "/u"
    assert "corrId" in sent

    await client.close()


@pytest.mark.asyncio
async def test_send_chat_cmd_timeout():
    ws = FakeWS()
    client = SimplexChatClient(
        "ws://test",
        request_timeout=0.1,
        connect_factory=lambda _u: _make_async(ws),
    )
    await client.connect()
    with pytest.raises(asyncio.TimeoutError):
        await client.send_chat_cmd("/u")
    # The pending entry should have been cleaned up.
    assert client._pending == {}
    await client.close()


async def _make_async(value):
    return value


@pytest.mark.asyncio
async def test_events_iterator_yields_unsolicited_messages():
    ws = FakeWS()
    client = _make_client(ws)
    await client.connect()

    ws.server_send({"resp": {"type": "newChatItems", "chatItems": []}})

    received = []

    async def consume():
        async for ev in client.events():
            received.append(ev)
            if len(received) >= 1:
                break

    await asyncio.wait_for(consume(), timeout=1.0)
    assert received == [{"type": "newChatItems", "chatItems": []}]

    await client.close()


@pytest.mark.asyncio
async def test_api_get_active_user_parses_shape():
    ws = FakeWS()
    client = _make_client(ws)
    await client.connect()

    async def reply():
        await asyncio.sleep(0.01)
        req = json.loads(ws.outgoing[-1])
        ws.server_send(
            {
                "corrId": req["corrId"],
                "resp": {
                    "type": "activeUser",
                    "user": {
                        "userId": 7,
                        "profile": {"displayName": "hermes"},
                    },
                },
            }
        )

    asyncio.create_task(reply())
    user = await client.api_get_active_user()
    assert isinstance(user, ActiveUser)
    assert user.user_id == 7
    assert user.display_name == "hermes"
    assert user.raw["userId"] == 7

    await client.close()


@pytest.mark.asyncio
async def test_api_get_active_user_wrong_type_raises():
    ws = FakeWS()
    client = _make_client(ws)
    await client.connect()

    async def reply():
        await asyncio.sleep(0.01)
        req = json.loads(ws.outgoing[-1])
        ws.server_send({"corrId": req["corrId"], "resp": {"type": "chatCmdError"}})

    asyncio.create_task(reply())
    with pytest.raises(SimplexProtocolError):
        await client.api_get_active_user()

    await client.close()


@pytest.mark.asyncio
async def test_api_get_groups_parses_list():
    ws = FakeWS()
    client = _make_client(ws)
    await client.connect()

    async def reply():
        await asyncio.sleep(0.01)
        req = json.loads(ws.outgoing[-1])
        ws.server_send(
            {
                "corrId": req["corrId"],
                "resp": {
                    "type": "groupsList",
                    "groups": [
                        {"groupInfo": {"groupId": 12, "displayName": "alpha"}},
                        {"groupInfo": {"groupId": 13, "displayName": "beta"}},
                        # malformed entry — should be skipped, not crash
                        {"groupInfo": {"displayName": "no id"}},
                    ],
                },
            }
        )

    asyncio.create_task(reply())
    groups = await client.api_get_groups()
    assert len(groups) == 2
    assert all(isinstance(g, GroupInfo) for g in groups)
    assert (groups[0].group_id, groups[0].display_name) == (12, "alpha")
    assert (groups[1].group_id, groups[1].display_name) == (13, "beta")

    await client.close()


@pytest.mark.asyncio
async def test_api_send_text_message_to_group_command_format():
    ws = FakeWS()
    client = _make_client(ws)
    await client.connect()

    async def reply():
        await asyncio.sleep(0.01)
        req = json.loads(ws.outgoing[-1])
        ws.server_send(
            {
                "corrId": req["corrId"],
                "resp": {
                    "type": "newChatItems",
                    "chatItems": [
                        {"chatItem": {"meta": {"itemId": 4242}}},
                    ],
                },
            }
        )

    asyncio.create_task(reply())
    result = await client.api_send_text_message_to_group(12, 'hi "world"')
    assert isinstance(result, SendResponse)
    assert result.item_id == 4242

    sent = json.loads(ws.outgoing[-1])
    assert sent["cmd"].startswith("/_send #12 json ")
    body = json.loads(sent["cmd"].removeprefix("/_send #12 json "))
    assert body == [
        {"msgContent": {"type": "text", "text": 'hi "world"'}, "mentions": {}}
    ]

    await client.close()


@pytest.mark.asyncio
async def test_api_send_returns_none_item_id_when_missing():
    ws = FakeWS()
    client = _make_client(ws)
    await client.connect()

    async def reply():
        await asyncio.sleep(0.01)
        req = json.loads(ws.outgoing[-1])
        ws.server_send(
            {
                "corrId": req["corrId"],
                "resp": {"type": "newChatItems", "chatItems": []},
            }
        )

    asyncio.create_task(reply())
    result = await client.api_send_text_message_to_group(12, "hi")
    assert result.item_id is None

    await client.close()


@pytest.mark.asyncio
async def test_close_fails_pending_requests():
    ws = FakeWS()
    client = _make_client(ws)
    await client.connect()

    async def issue():
        return await client.send_chat_cmd("/u")

    task = asyncio.create_task(issue())
    await asyncio.sleep(0.05)  # let the request reach the wire
    await client.close()

    with pytest.raises(SimplexConnectionClosed):
        await task


@pytest.mark.asyncio
async def test_recv_loop_drops_non_json_frames():
    ws = FakeWS()
    client = _make_client(ws)
    await client.connect()

    # Push garbage, then a real event.
    ws.server_send("not json at all")
    ws.server_send({"resp": {"type": "newChatItems"}})

    received = []

    async def consume():
        async for ev in client.events():
            received.append(ev)
            break

    await asyncio.wait_for(consume(), timeout=1.0)
    assert received == [{"type": "newChatItems"}]

    await client.close()


@pytest.mark.asyncio
async def test_send_after_close_raises():
    ws = FakeWS()
    client = _make_client(ws)
    await client.connect()
    await client.close()
    with pytest.raises(SimplexConnectionClosed):
        await client.send_chat_cmd("/u")


@pytest.mark.asyncio
async def test_async_context_manager():
    ws = FakeWS()

    async def factory(_u: str):
        return ws

    async with SimplexChatClient(
        "ws://test", connect_factory=factory
    ) as client:
        assert client._ws is ws
    assert ws.closed
