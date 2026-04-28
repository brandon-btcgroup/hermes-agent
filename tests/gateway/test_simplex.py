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


# ── Replay: cursor IO + dedupe + paginated walk ────────────────────────


class _ReplayClient:
    """Minimal SimplexChatClient stand-in for replay tests.

    Holds a flat queue of bare chatItems; api_get_chat slices off ``count``
    items whose itemId > after_id, mirroring the daemon's contract. ``calls``
    records every (count, after_id) tuple for assertions.
    """

    def __init__(self, items: list[dict] | None = None):
        self._items = list(items or [])
        self.calls: list[tuple[int, int | None]] = []
        self.sent: list[tuple[int, str]] = []

    async def api_get_chat(self, *, group_id, count, after_id=None):
        self.calls.append((count, after_id))
        if after_id is None:
            return self._items[:count]
        eligible = [
            it
            for it in self._items
            if isinstance((it.get("meta") or {}).get("itemId"), int)
            and it["meta"]["itemId"] > after_id
        ]
        return eligible[:count]

    async def api_send_text_message_to_group(self, group_id, text):
        self.sent.append((group_id, text))
        return SendResponse(item_id=None, raw={})


def _replay_item(item_id, text="hi", sender="brandon"):
    """A bare chatItem (no chatInfo) — what /_get chat returns."""
    return {
        "chatDir": {
            "type": "groupRcv",
            "groupMember": {
                "localDisplayName": sender,
                "memberProfile": {"displayName": sender},
            },
        },
        "content": {"type": "rcvMsgContent", "msgContent": {"type": "text", "text": text}},
        "meta": {"itemId": item_id},
    }


def test_cursor_roundtrip_via_load_save(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    a = _make_adapter(group_ids=["12"])
    assert a._cursors == {}
    a._advance_cursor("12", 100)
    a._advance_cursor("12", 105)
    a._advance_cursor("12", 99)  # ignored — older

    # Fresh adapter should pick up the persisted cursor.
    b = _make_adapter(group_ids=["12"])
    assert b._cursors == {"12": 105}


def test_cursor_corrupt_file_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cursor_path = tmp_path / "simplex" / "cursors.json"
    cursor_path.parent.mkdir(parents=True)
    cursor_path.write_text("not json")
    a = _make_adapter(group_ids=["12"])
    assert a._cursors == {}


@pytest.mark.asyncio
async def test_dispatch_skips_duplicate_item_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    a = _make_adapter(group_ids=["12"])
    received: list[MessageEvent] = []

    async def handler(ev: MessageEvent):
        received.append(ev)
        return None

    a.set_message_handler(handler)
    item = _group_rcv_text(12, item_id=42)
    await a._dispatch_chat_item(item)
    await a._dispatch_chat_item(item)  # same itemId
    await asyncio.sleep(0.05)
    for t in list(a._background_tasks):
        try:
            await t
        except Exception:
            pass
    assert len(received) == 1


@pytest.mark.asyncio
async def test_dispatch_advances_cursor_after_handle(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    a = _make_adapter(group_ids=["12"])
    a.set_message_handler(lambda ev: None)
    await a._dispatch_chat_item(_group_rcv_text(12, item_id=42))
    await asyncio.sleep(0.05)
    for t in list(a._background_tasks):
        try:
            await t
        except Exception:
            pass
    assert a._cursors == {"12": 42}


@pytest.mark.asyncio
async def test_replay_seeds_cursor_on_first_connect(tmp_path, monkeypatch):
    """First connect with no cursor: capture the latest itemId, dispatch nothing."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    a = _make_adapter(group_ids=["12"])
    received: list[MessageEvent] = []
    a.set_message_handler(lambda ev: received.append(ev) or None)
    a._client = _ReplayClient(items=[_replay_item(99)])

    await a._replay_missed_messages()

    assert received == []
    assert a._cursors == {"12": 99}
    # Single seeding call with count=1, no after_id.
    assert a._client.calls == [(1, None)]


@pytest.mark.asyncio
async def test_replay_walks_forward_from_cursor(tmp_path, monkeypatch):
    """Cursor present: paginate /_get chat after the cursor, dispatch each item.

    Verifies the replay walk by intercepting _dispatch_chat_item directly,
    which bypasses the base adapter's session-coalescing layer (that layer
    has its own coverage; what matters here is what replay hands it).
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    a = _make_adapter(group_ids=["12"])
    a._cursors["12"] = 50

    dispatched: list[dict] = []

    async def fake_dispatch(item: dict) -> None:
        dispatched.append(item)

    a._dispatch_chat_item = fake_dispatch  # type: ignore[assignment]
    a._client = _ReplayClient(
        items=[_replay_item(51, "first"), _replay_item(52, "second")]
    )

    await a._replay_missed_messages()

    texts = [
        d["chatItem"]["content"]["msgContent"]["text"] for d in dispatched
    ]
    assert texts == ["first", "second"]
    # First (and only) page request was after_id=50.
    assert a._client.calls[0][1] == 50
    # Each dispatched item is wrapped with the group's chatInfo.
    for d in dispatched:
        assert d["chatInfo"]["groupInfo"]["groupId"] == 12


@pytest.mark.asyncio
async def test_replay_caps_at_max(tmp_path, monkeypatch):
    """SIMPLEX_REPLAY_MAX caps the daemon request, not just the dispatched count.

    With 5 items available and cap=3, the first request asks for count=3
    and the loop exits without a second fetch.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("SIMPLEX_REPLAY_MAX", "3")
    a = _make_adapter(group_ids=["12"])
    a._cursors["12"] = 0
    dispatched: list[dict] = []

    async def fake_dispatch(item: dict) -> None:
        dispatched.append(item)

    a._dispatch_chat_item = fake_dispatch  # type: ignore[assignment]
    a._client = _ReplayClient(items=[_replay_item(i) for i in (1, 2, 3, 4, 5)])

    await a._replay_missed_messages()

    assert len(dispatched) == 3
    assert a._client.calls == [(3, 0)]
    # Items 4 and 5 not dispatched.
    dispatched_ids = [
        d["chatItem"]["meta"]["itemId"] for d in dispatched
    ]
    assert dispatched_ids == [1, 2, 3]


@pytest.mark.asyncio
async def test_replay_disabled_skips_walk(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("SIMPLEX_REPLAY_DISABLE", "true")
    a = _make_adapter(group_ids=["12"])
    a._cursors["12"] = 50
    received: list[MessageEvent] = []
    a.set_message_handler(lambda ev: received.append(ev) or None)
    a._client = _ReplayClient(items=[_replay_item(51)])

    await a._replay_missed_messages()
    assert received == []
    assert a._client.calls == []
