"""Tests for the SimplexAdapter (event filter, send routing, env loading)."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from gateway.config import Platform, PlatformConfig, load_gateway_config
from gateway.platforms.base import MessageEvent, SendResult
from gateway.platforms.simplex import (
    SimplexAdapter,
    check_simplex_requirements,
)
from gateway.platforms.simplex_client import SendResponse


# ── Platform enum + config ──────────────────────────────────────────────


def test_platform_enum_value():
    assert Platform.SIMPLEX.value == "simplex"


def test_check_simplex_requirements_missing(monkeypatch):
    monkeypatch.delenv("SIMPLEX_WS_URL", raising=False)
    monkeypatch.delenv("SIMPLEX_GROUP_IDS", raising=False)
    assert check_simplex_requirements() is False


def test_check_simplex_requirements_present(monkeypatch):
    monkeypatch.setenv("SIMPLEX_WS_URL", "ws://x:5225")
    monkeypatch.setenv("SIMPLEX_GROUP_IDS", "12")
    assert check_simplex_requirements() is True


def test_env_loading_roundtrip(monkeypatch):
    monkeypatch.setenv("SIMPLEX_WS_URL", "ws://daemon:5225")
    monkeypatch.setenv("SIMPLEX_GROUP_IDS", "12, 13 , 14")
    monkeypatch.setenv("SIMPLEX_HOME_GROUP_ID", "12")
    monkeypatch.setenv("SIMPLEX_MAX_RECONNECT_DELAY_S", "120")

    cfg = load_gateway_config()
    sx = cfg.platforms[Platform.SIMPLEX]
    assert sx.enabled is True
    assert sx.extra["ws_url"] == "ws://daemon:5225"
    assert sx.extra["group_ids"] == ["12", "13", "14"]
    assert sx.extra["max_reconnect_delay_s"] == 120
    assert sx.home_channel.chat_id == "12"
    assert Platform.SIMPLEX in cfg.get_connected_platforms()


def test_get_connected_platforms_drops_simplex_without_groups(monkeypatch):
    monkeypatch.setenv("SIMPLEX_WS_URL", "ws://daemon:5225")
    monkeypatch.delenv("SIMPLEX_GROUP_IDS", raising=False)
    cfg = load_gateway_config()
    # Platform is added but not "connected" — group_ids is required.
    assert Platform.SIMPLEX not in cfg.get_connected_platforms()


# ── Adapter init ─────────────────────────────────────────────────────────


def _make_adapter(group_ids=("12", "13"), ws_url="ws://daemon:5225") -> SimplexAdapter:
    cfg = PlatformConfig(enabled=True)
    cfg.extra = {
        "ws_url": ws_url,
        "group_ids": list(group_ids),
        "max_reconnect_delay_s": 60,
    }
    return SimplexAdapter(cfg)


def test_adapter_init_parses_config():
    a = _make_adapter()
    assert a.platform == Platform.SIMPLEX
    assert a._allowed_group_ids == {"12", "13"}
    assert a._ws_url == "ws://daemon:5225"
    assert a._max_reconnect_delay == 60


@pytest.mark.asyncio
async def test_connect_fails_without_ws_url():
    cfg = PlatformConfig(enabled=True)
    cfg.extra = {"ws_url": "", "group_ids": ["12"]}
    a = SimplexAdapter(cfg)
    assert (await a.connect()) is False
    assert a.fatal_error_code == "MISSING_CONFIG"


@pytest.mark.asyncio
async def test_connect_fails_without_group_ids():
    cfg = PlatformConfig(enabled=True)
    cfg.extra = {"ws_url": "ws://x", "group_ids": []}
    a = SimplexAdapter(cfg)
    assert (await a.connect()) is False
    assert a.fatal_error_code == "MISSING_CONFIG"


# ── Event filter (dispatch) ──────────────────────────────────────────────


def _group_rcv_text(group_id, text="hi", sender="brandon", item_id=42):
    return {
        "chatInfo": {
            "type": "group",
            "groupInfo": {"groupId": group_id, "displayName": f"group {group_id}"},
        },
        "chatItem": {
            "chatDir": {
                "type": "groupRcv",
                "groupMember": {
                    "localDisplayName": sender,
                    "memberProfile": {"displayName": sender},
                },
            },
            "content": {"type": "rcvMsgContent", "msgContent": {"type": "text", "text": text}},
            "meta": {"itemId": item_id},
        },
    }


@pytest.mark.asyncio
async def test_dispatch_routes_groupRcv_text():
    a = _make_adapter(group_ids=["12"])
    received: list[MessageEvent] = []

    async def handler(ev: MessageEvent):
        received.append(ev)
        return None

    a.set_message_handler(handler)
    await a._dispatch_chat_item(_group_rcv_text(12, text="hello there"))
    # handle_message dispatches in the background; let it run.
    await asyncio.sleep(0.05)
    for t in list(a._background_tasks):
        try:
            await t
        except Exception:
            pass
    assert len(received) == 1
    ev = received[0]
    assert ev.text == "hello there"
    assert ev.source.platform == Platform.SIMPLEX
    assert ev.source.chat_id == "12"
    assert ev.source.user_id == "brandon"
    assert ev.message_id == "42"


@pytest.mark.asyncio
async def test_dispatch_drops_groupSnd_self_echo():
    a = _make_adapter(group_ids=["12"])
    received: list[MessageEvent] = []
    a.set_message_handler(lambda ev: received.append(ev) or None)
    item = _group_rcv_text(12)
    item["chatItem"]["chatDir"]["type"] = "groupSnd"
    await a._dispatch_chat_item(item)
    await asyncio.sleep(0.02)
    assert received == []


@pytest.mark.asyncio
async def test_dispatch_drops_unsubscribed_group():
    a = _make_adapter(group_ids=["12"])
    received: list[MessageEvent] = []
    a.set_message_handler(lambda ev: received.append(ev) or None)
    await a._dispatch_chat_item(_group_rcv_text(99))  # not in allowlist
    await asyncio.sleep(0.02)
    assert received == []


@pytest.mark.asyncio
async def test_dispatch_drops_non_text_content():
    a = _make_adapter(group_ids=["12"])
    received: list[MessageEvent] = []
    a.set_message_handler(lambda ev: received.append(ev) or None)
    item = _group_rcv_text(12)
    item["chatItem"]["content"]["msgContent"]["type"] = "image"
    await a._dispatch_chat_item(item)
    await asyncio.sleep(0.02)
    assert received == []


@pytest.mark.asyncio
async def test_dispatch_drops_dm_chat_type():
    a = _make_adapter(group_ids=["12"])
    received: list[MessageEvent] = []
    a.set_message_handler(lambda ev: received.append(ev) or None)
    item = _group_rcv_text(12)
    item["chatInfo"]["type"] = "direct"
    await a._dispatch_chat_item(item)
    await asyncio.sleep(0.02)
    assert received == []


@pytest.mark.asyncio
async def test_dispatch_synthesizes_message_id_when_missing():
    a = _make_adapter(group_ids=["12"])
    received: list[MessageEvent] = []

    async def handler(ev: MessageEvent):
        received.append(ev)
        return None

    a.set_message_handler(handler)
    item = _group_rcv_text(12)
    item["chatItem"]["meta"] = {}  # no itemId
    await a._dispatch_chat_item(item)
    await asyncio.sleep(0.05)
    for t in list(a._background_tasks):
        try:
            await t
        except Exception:
            pass
    assert len(received) == 1
    assert received[0].message_id  # synthesized uuid


# ── Send ────────────────────────────────────────────────────────────────


class _FakeClient:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.next_item_id: int | None = 7

    async def api_send_text_message_to_group(self, group_id, text):
        self.sent.append((group_id, text))
        return SendResponse(item_id=self.next_item_id, raw={"type": "newChatItems"})


@pytest.mark.asyncio
async def test_send_routes_to_client():
    a = _make_adapter(group_ids=["12"])
    a._client = _FakeClient()
    result = await a.send("12", "hello world")
    assert isinstance(result, SendResult)
    assert result.success is True
    assert result.message_id == "7"
    assert a._client.sent == [(12, "hello world")]


@pytest.mark.asyncio
async def test_send_fails_with_non_numeric_chat_id():
    a = _make_adapter()
    a._client = _FakeClient()
    result = await a.send("not-a-number", "hi")
    assert result.success is False
    assert "numeric" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_send_fails_when_not_connected():
    a = _make_adapter()
    a._client = None
    result = await a.send("12", "hi")
    assert result.success is False
    assert result.retryable is True


@pytest.mark.asyncio
async def test_send_truncates_long_messages():
    a = _make_adapter(group_ids=["12"])
    a._client = _FakeClient()
    long_msg = "x" * 5000
    result = await a.send("12", long_msg)
    assert result.success is True
    sent_text = a._client.sent[0][1]
    assert len(sent_text) == 4000


@pytest.mark.asyncio
async def test_send_lock_serializes_concurrent_sends():
    """Two concurrent sends should execute serially under _send_lock."""
    a = _make_adapter(group_ids=["12"])

    order: list[str] = []

    class TracingClient:
        async def api_send_text_message_to_group(self, group_id, text):
            order.append(f"start-{text}")
            await asyncio.sleep(0.05)
            order.append(f"end-{text}")
            return SendResponse(item_id=1, raw={})

    a._client = TracingClient()
    await asyncio.gather(a.send("12", "A"), a.send("12", "B"))
    # Each send completes before the next starts → no interleaving.
    assert order in (
        ["start-A", "end-A", "start-B", "end-B"],
        ["start-B", "end-B", "start-A", "end-A"],
    )


# ── send_typing / send_image (no-ops) ────────────────────────────────────


@pytest.mark.asyncio
async def test_send_typing_is_noop():
    a = _make_adapter()
    a._client = _FakeClient()
    # Returns None and does not call the client.
    assert await a.send_typing("12") is None
    assert a._client.sent == []


@pytest.mark.asyncio
async def test_send_image_returns_unimplemented():
    a = _make_adapter()
    result = await a.send_image("12", "https://example.com/cat.png", caption="hi")
    assert result.success is False
    assert "not implemented" in (result.error or "").lower()


# ── get_chat_info ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_chat_info_uses_cached_name():
    a = _make_adapter()
    a._known_groups = {12: "alpha"}
    info = await a.get_chat_info("12")
    assert info == {"name": "alpha", "type": "group", "chat_id": "12"}


# ── Integration: send_message_tool routing ──────────────────────────────


def test_send_message_tool_platform_map_includes_simplex():
    """The send_message tool must recognize 'simplex' as a target platform."""
    import inspect
    from tools import send_message_tool as mod

    src = inspect.getsource(mod)
    assert '"simplex": Platform.SIMPLEX' in src
    assert "_send_simplex" in src


def test_cron_scheduler_platform_map_includes_simplex():
    import inspect
    from cron import scheduler as mod

    src = inspect.getsource(mod)
    assert '"simplex": Platform.SIMPLEX' in src


# ── Auth maps in run.py ─────────────────────────────────────────────────


def test_run_py_authorization_maps_include_simplex():
    import inspect
    from gateway import run as mod

    src = inspect.getsource(mod)
    assert "SIMPLEX_ALLOWED_USERS" in src
    assert "SIMPLEX_ALLOW_ALL_USERS" in src
