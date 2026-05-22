"""Tests for SimpleX per-group missed-message replay (PR-2).

Covers:
  - ReplayState cursor file persistence + dedupe ring semantics
  - Adapter _send_and_wait corrId/response Future plumbing
  - Adapter _replay_group pagination + dedupe interaction
  - Adapter _handle_new_chat_item dedupe check + cursor advance

The adapter module is loaded via the shared ``_plugin_adapter_loader``
so it lives under a unique sys.modules key. ``_replay`` is loaded via
the same explicit-path importlib pattern under a parent-package context
so its relative-import-free shape stays a regular submodule.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.gateway._plugin_adapter_loader import load_plugin_adapter


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SIMPLEX_DIR = _REPO_ROOT / "plugins" / "platforms" / "simplex"


def _load_replay_module() -> ModuleType:
    name = "plugin_simplex_replay"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, _SIMPLEX_DIR / "_replay.py")
    if spec is None or spec.loader is None:
        raise ImportError("could not load _replay module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


_replay = _load_replay_module()
_simplex = load_plugin_adapter("simplex")
SimplexAdapter = _simplex.SimplexAdapter


# ---------------------------------------------------------------------------
# 1. ReplayState — pure state
# ---------------------------------------------------------------------------

def test_replay_state_load_missing_file_is_noop(tmp_path):
    state = _replay.ReplayState(tmp_path / "cursors.json")
    state.load()
    assert state.known_groups() == []
    assert state.get_cursor(1) is None


def test_replay_state_load_recovers_from_corrupt_file(tmp_path):
    path = tmp_path / "cursors.json"
    path.write_text("not-json", encoding="utf-8")
    state = _replay.ReplayState(path)
    state.load()
    assert state.known_groups() == []


def test_replay_state_persists_atomically(tmp_path):
    path = tmp_path / "cursors.json"
    state = _replay.ReplayState(path)
    state.update_cursor(1, 100)
    state.update_cursor(2, 200)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == _replay.CURSOR_FILE_VERSION
    assert payload["groups"] == {"1": 100, "2": 200}


def test_replay_state_round_trip(tmp_path):
    path = tmp_path / "cursors.json"
    state = _replay.ReplayState(path)
    state.update_cursor(1, 100)
    state.update_cursor(2, 200)

    state2 = _replay.ReplayState(path)
    state2.load()
    assert sorted(state2.known_groups()) == [1, 2]
    assert state2.get_cursor(1) == 100
    assert state2.get_cursor(2) == 200


def test_replay_state_cursor_never_rewinds(tmp_path):
    state = _replay.ReplayState(tmp_path / "cursors.json")
    state.update_cursor(1, 100)
    state.update_cursor(1, 50)  # earlier id — must not rewind
    assert state.get_cursor(1) == 100


def test_replay_state_dedupe_ring_evicts_oldest(tmp_path):
    state = _replay.ReplayState(tmp_path / "cursors.json", dedupe_size=3)
    state.mark_dispatched(1, 10)
    state.mark_dispatched(1, 11)
    state.mark_dispatched(1, 12)
    state.mark_dispatched(1, 13)  # evicts (1, 10)

    assert state.already_dispatched(1, 11)
    assert state.already_dispatched(1, 12)
    assert state.already_dispatched(1, 13)
    assert not state.already_dispatched(1, 10)


def test_replay_state_mark_dispatched_is_idempotent(tmp_path):
    state = _replay.ReplayState(tmp_path / "cursors.json", dedupe_size=2)
    state.mark_dispatched(1, 10)
    state.mark_dispatched(1, 10)  # no-op
    state.mark_dispatched(1, 11)
    # If the duplicate had been treated as new, (1, 10) would have been
    # evicted by adding (1, 11). It should still be present.
    assert state.already_dispatched(1, 10)
    assert state.already_dispatched(1, 11)


# ---------------------------------------------------------------------------
# 2. Adapter env-var parsing
# ---------------------------------------------------------------------------

def _adapter(monkeypatch, **env) -> SimplexAdapter:
    for k in (
        "SIMPLEX_REPLAY_DISABLED",
        "SIMPLEX_REPLAY_MAX_ITEMS",
        "SIMPLEX_REPLAY_PAGE_SIZE",
    ):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cfg = _simplex.PlatformConfig(enabled=True, extra={"ws_url": "ws://test"})
    return SimplexAdapter(cfg)


def test_adapter_replay_enabled_by_default(monkeypatch):
    adapter = _adapter(monkeypatch)
    assert adapter._replay_disabled is False
    assert adapter._replay_max_items == _simplex._REPLAY_DEFAULT_MAX_ITEMS
    assert adapter._replay_page_size == _simplex._REPLAY_DEFAULT_PAGE_SIZE


def test_adapter_replay_disabled_env(monkeypatch):
    adapter = _adapter(monkeypatch, SIMPLEX_REPLAY_DISABLED="true")
    assert adapter._replay_disabled is True


def test_adapter_replay_max_items_env(monkeypatch):
    adapter = _adapter(monkeypatch, SIMPLEX_REPLAY_MAX_ITEMS="50")
    assert adapter._replay_max_items == 50


def test_adapter_replay_page_size_env(monkeypatch):
    adapter = _adapter(monkeypatch, SIMPLEX_REPLAY_PAGE_SIZE="10")
    assert adapter._replay_page_size == 10


def test_adapter_replay_invalid_env_falls_back_to_defaults(monkeypatch):
    adapter = _adapter(
        monkeypatch,
        SIMPLEX_REPLAY_MAX_ITEMS="not-a-number",
        SIMPLEX_REPLAY_PAGE_SIZE="also-bad",
    )
    assert adapter._replay_max_items == _simplex._REPLAY_DEFAULT_MAX_ITEMS
    assert adapter._replay_page_size == _simplex._REPLAY_DEFAULT_PAGE_SIZE


# ---------------------------------------------------------------------------
# 3. _send_and_wait corrId/Future plumbing
# ---------------------------------------------------------------------------

def test_send_and_wait_returns_matching_response(monkeypatch):
    adapter = _adapter(monkeypatch)
    sent: list[dict] = []

    async def _capture_send(payload):
        sent.append(payload)

    adapter._send_ws = _capture_send  # type: ignore[assignment]
    adapter._ws = MagicMock()  # truthy

    async def run():
        async def feed_response():
            # Wait until the future is registered, then deliver a response.
            while not adapter._pending_responses:
                await asyncio.sleep(0)
            corr_id = next(iter(adapter._pending_responses.keys()))
            await adapter._handle_event(
                {"corrId": corr_id, "resp": {"type": "apiChat", "chat": {}}}
            )

        feeder = asyncio.create_task(feed_response())
        resp = await adapter._send_and_wait("/_get chat #1 count=10", timeout=2.0)
        await feeder
        return resp

    resp = asyncio.run(run())
    assert resp is not None
    assert resp["resp"]["type"] == "apiChat"
    assert sent and sent[0]["cmd"] == "/_get chat #1 count=10"
    assert adapter._pending_responses == {}


def test_send_and_wait_times_out_cleanly(monkeypatch):
    adapter = _adapter(monkeypatch)

    async def _noop(payload):
        pass

    adapter._send_ws = _noop  # type: ignore[assignment]
    adapter._ws = MagicMock()

    async def run():
        return await adapter._send_and_wait("/u", timeout=0.05)

    resp = asyncio.run(run())
    assert resp is None
    assert adapter._pending_responses == {}


def test_send_and_wait_returns_none_when_ws_closed(monkeypatch):
    adapter = _adapter(monkeypatch)
    adapter._ws = None

    async def run():
        return await adapter._send_and_wait("/u", timeout=2.0)

    assert asyncio.run(run()) is None


# ---------------------------------------------------------------------------
# 4. _replay_group dispatches and updates cursor
# ---------------------------------------------------------------------------

def _make_chat_response(*, group_id: int, items: list[dict]) -> dict:
    return {
        "corrId": "irrelevant",
        "resp": {
            "type": "apiChat",
            "chat": {
                "chatInfo": {
                    "type": "group",
                    "groupInfo": {"groupId": group_id, "displayName": f"g{group_id}"},
                },
                "chatItems": items,
            },
        },
    }


def _make_text_item(*, item_id: int, text: str) -> dict:
    return {
        "meta": {"itemId": item_id, "itemStatus": {"type": "rcvNew"}},
        "content": {"msgContent": {"type": "text", "text": text}},
        "chatItemMember": {"memberId": "user-1", "displayName": "Bob"},
    }


def test_replay_group_dispatches_paginated_items(monkeypatch, tmp_path):
    adapter = _adapter(
        monkeypatch,
        SIMPLEX_REPLAY_PAGE_SIZE="2",
        SIMPLEX_REPLAY_MAX_ITEMS="10",
    )
    adapter._replay_state = _replay.ReplayState(tmp_path / "cursors.json")
    adapter._replay_state.update_cursor(7, 100)
    adapter._running = True

    dispatched: list[str] = []
    adapter.handle_message = AsyncMock(
        side_effect=lambda ev: dispatched.append(ev.text)
    )

    pages = [
        _make_chat_response(
            group_id=7,
            items=[
                _make_text_item(item_id=101, text="a"),
                _make_text_item(item_id=102, text="b"),
            ],
        ),
        _make_chat_response(
            group_id=7,
            items=[_make_text_item(item_id=103, text="c")],
        ),
    ]
    cmds: list[str] = []

    async def fake_send_and_wait(cmd, *, timeout=10):
        cmds.append(cmd)
        return pages.pop(0) if pages else None

    adapter._send_and_wait = fake_send_and_wait  # type: ignore[assignment]

    asyncio.run(adapter._replay_group(7, 100))
    assert dispatched == ["a", "b", "c"]
    assert cmds == [
        "/_get chat #7 after=100 count=2",
        "/_get chat #7 after=102 count=2",
    ]
    assert adapter._replay_state.get_cursor(7) == 103


def test_replay_group_stops_at_max_items(monkeypatch, tmp_path):
    adapter = _adapter(
        monkeypatch,
        SIMPLEX_REPLAY_PAGE_SIZE="5",
        SIMPLEX_REPLAY_MAX_ITEMS="2",
    )
    adapter._replay_state = _replay.ReplayState(tmp_path / "cursors.json")
    adapter._replay_state.update_cursor(7, 100)
    adapter._running = True

    dispatched: list[str] = []
    adapter.handle_message = AsyncMock(
        side_effect=lambda ev: dispatched.append(ev.text)
    )

    page = _make_chat_response(
        group_id=7,
        items=[
            _make_text_item(item_id=101, text="a"),
            _make_text_item(item_id=102, text="b"),
            _make_text_item(item_id=103, text="c"),
        ],
    )

    async def fake_send_and_wait(cmd, *, timeout=10):
        return page

    adapter._send_and_wait = fake_send_and_wait  # type: ignore[assignment]

    asyncio.run(adapter._replay_group(7, 100))
    assert dispatched == ["a", "b"]


def test_replay_group_aborts_on_timeout(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch)
    adapter._replay_state = _replay.ReplayState(tmp_path / "cursors.json")
    adapter._replay_state.update_cursor(7, 100)
    adapter._running = True

    adapter.handle_message = AsyncMock()

    async def fake_send_and_wait(cmd, *, timeout=10):
        return None  # simulate timeout / WS-closed

    adapter._send_and_wait = fake_send_and_wait  # type: ignore[assignment]
    asyncio.run(adapter._replay_group(7, 100))
    adapter.handle_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# 5. Live message dedupe through _handle_new_chat_item
# ---------------------------------------------------------------------------

def test_live_handler_skips_already_dispatched(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch)
    adapter._replay_state = _replay.ReplayState(tmp_path / "cursors.json")
    adapter._replay_state.mark_dispatched(7, 42)
    adapter.handle_message = AsyncMock()

    wrapper = {
        "chatInfo": {
            "type": "group",
            "groupInfo": {"groupId": 7, "displayName": "g7"},
        },
        "chatItem": _make_text_item(item_id=42, text="dup"),
    }
    asyncio.run(adapter._handle_new_chat_item(wrapper))
    adapter.handle_message.assert_not_awaited()


def test_live_handler_dispatches_and_advances_cursor(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch)
    adapter._replay_state = _replay.ReplayState(tmp_path / "cursors.json")
    adapter.handle_message = AsyncMock()

    wrapper = {
        "chatInfo": {
            "type": "group",
            "groupInfo": {"groupId": 7, "displayName": "g7"},
        },
        "chatItem": _make_text_item(item_id=42, text="fresh"),
    }
    asyncio.run(adapter._handle_new_chat_item(wrapper))
    adapter.handle_message.assert_awaited_once()
    assert adapter._replay_state.get_cursor(7) == 42
    assert adapter._replay_state.already_dispatched(7, 42)


def test_live_handler_without_replay_state_dispatches_normally(monkeypatch):
    adapter = _adapter(monkeypatch, SIMPLEX_REPLAY_DISABLED="true")
    adapter._replay_state = None
    adapter.handle_message = AsyncMock()

    wrapper = {
        "chatInfo": {
            "type": "group",
            "groupInfo": {"groupId": 7, "displayName": "g7"},
        },
        "chatItem": _make_text_item(item_id=42, text="hi"),
    }
    asyncio.run(adapter._handle_new_chat_item(wrapper))
    adapter.handle_message.assert_awaited_once()


def test_live_handler_passes_through_dm_without_dedupe(monkeypatch, tmp_path):
    """DMs aren't deduped — cursor logic is groups-only in this PR."""
    adapter = _adapter(monkeypatch)
    adapter._replay_state = _replay.ReplayState(tmp_path / "cursors.json")
    adapter.handle_message = AsyncMock()

    wrapper = {
        "chatInfo": {
            "type": "direct",
            "contact": {"contactId": 42, "displayName": "Alice"},
        },
        "chatItem": _make_text_item(item_id=1, text="hi"),
    }
    asyncio.run(adapter._handle_new_chat_item(wrapper))
    adapter.handle_message.assert_awaited_once()
    # No cursor for the contact id — group cursor logic skipped for DMs.
    assert adapter._replay_state.known_groups() == []
