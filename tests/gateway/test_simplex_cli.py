"""Tests for the ``hermes simplex`` CLI helpers (cli.py + _ws_client.py).

Covers argparse wiring, the list/join dispatcher, and the request/response
shape of the minimal WS client used by the CLI. The platform adapter
proper has its own test file (``test_simplex_plugin.py``).

To keep relative imports inside cli.py working (``from ._ws_client import
SimplexChatClient``) we load both modules under a unique parent-package
name in ``sys.modules`` so they cannot collide with sibling platform
plugins on the same xdist worker.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SIMPLEX_DIR = _REPO_ROOT / "plugins" / "platforms" / "simplex"
_PARENT_PKG = "plugin_simplex_pkg"


def _ensure_parent_package() -> None:
    if _PARENT_PKG in sys.modules:
        return
    parent = ModuleType(_PARENT_PKG)
    parent.__path__ = [str(_SIMPLEX_DIR)]
    sys.modules[_PARENT_PKG] = parent


def _load_simplex_module(name: str) -> ModuleType:
    """Load ``plugins/platforms/simplex/<name>.py`` as ``<parent>.<name>``."""
    _ensure_parent_package()
    qualified = f"{_PARENT_PKG}.{name}"
    cached = sys.modules.get(qualified)
    if cached is not None:
        return cached
    path = _SIMPLEX_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(qualified, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(qualified, None)
        raise
    return module


# Skip the whole module if websockets isn't installed — _ws_client imports it
# at top level by design (CLI is opt-in via the [simplex] extra).
pytest.importorskip("websockets")

# Load _ws_client first so cli's relative import resolves out of sys.modules.
_ws_client = _load_simplex_module("_ws_client")
_cli = _load_simplex_module("cli")


# ---------------------------------------------------------------------------
# 1. Parser wiring
# ---------------------------------------------------------------------------

def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes simplex")
    _cli.register_cli(parser)
    return parser


def test_register_cli_adds_list_action():
    parser = _make_parser()
    args = parser.parse_args(["list"])
    assert args.simplex_action == "list"


def test_register_cli_adds_join_action_with_link():
    parser = _make_parser()
    args = parser.parse_args(["join", "simplex:/contact#abc"])
    assert args.simplex_action == "join"
    assert args.invite_link == "simplex:/contact#abc"


def test_register_cli_join_accepts_timeout_override():
    parser = _make_parser()
    args = parser.parse_args(["join", "link", "--timeout", "30"])
    assert args.timeout == pytest.approx(30.0)


def test_register_cli_ws_url_override_is_global():
    parser = _make_parser()
    args = parser.parse_args(["--ws-url", "ws://host:1234", "list"])
    assert args.ws_url == "ws://host:1234"


# ---------------------------------------------------------------------------
# 2. _resolve_ws_url
# ---------------------------------------------------------------------------

def _ns(**kw) -> argparse.Namespace:
    defaults = {"ws_url": None}
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def test_resolve_ws_url_prefers_arg(monkeypatch):
    monkeypatch.setenv("SIMPLEX_WS_URL", "ws://env:1")
    assert _cli._resolve_ws_url(_ns(ws_url="ws://arg:2")) == "ws://arg:2"


def test_resolve_ws_url_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("SIMPLEX_WS_URL", "ws://env:1")
    assert _cli._resolve_ws_url(_ns()) == "ws://env:1"


def test_resolve_ws_url_returns_none_when_unset(monkeypatch, capsys):
    monkeypatch.delenv("SIMPLEX_WS_URL", raising=False)
    assert _cli._resolve_ws_url(_ns()) is None
    assert "SIMPLEX_WS_URL" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 3. _ws_client response parsing
# ---------------------------------------------------------------------------

def _stub_send(client, response: dict) -> list[str]:
    """Replace client.send_chat_cmd with a stub that records cmds."""
    sends: list[str] = []

    async def _fake(cmd: str, *, timeout=None):
        sends.append(cmd)
        return response

    client.send_chat_cmd = _fake  # type: ignore[assignment]
    return sends


def test_api_get_active_user_parses_profile():
    client = _ws_client.SimplexChatClient("ws://test")
    sends = _stub_send(
        client,
        {"type": "activeUser", "user": {"userId": 42, "profile": {"displayName": "Alice"}}},
    )
    user = asyncio.run(client.api_get_active_user())
    assert user.user_id == 42
    assert user.display_name == "Alice"
    assert sends == ["/u"]


def test_api_get_active_user_raises_on_wrong_type():
    client = _ws_client.SimplexChatClient("ws://test")
    _stub_send(client, {"type": "chatError"})
    with pytest.raises(_ws_client.SimplexProtocolError):
        asyncio.run(client.api_get_active_user())


def test_api_get_groups_collects_entries():
    client = _ws_client.SimplexChatClient("ws://test")
    _stub_send(
        client,
        {
            "type": "groupsList",
            "groups": [
                {"groupInfo": {"groupId": 1, "displayName": "G1"}},
                {"groupInfo": {"groupId": 2, "groupProfile": {"displayName": "G2"}}},
                {"groupInfo": {}},
            ],
        },
    )
    groups = asyncio.run(client.api_get_groups())
    assert [(g.group_id, g.display_name) for g in groups] == [(1, "G1"), (2, "G2")]


def test_api_get_contacts_collects_entries():
    client = _ws_client.SimplexChatClient("ws://test")
    _stub_send(
        client,
        {
            "type": "contactsList",
            "contacts": [
                {"contactId": 7, "localDisplayName": "bob"},
                {"contactId": 8, "profile": {"displayName": "Carol"}},
                {"id": 9, "displayName": "dave"},
                {},
            ],
        },
    )
    contacts = asyncio.run(client.api_get_contacts())
    assert [(c.contact_id, c.display_name) for c in contacts] == [
        (7, "bob"),
        (8, "Carol"),
        (9, "dave"),
    ]


def test_api_connect_sends_invitation_link():
    client = _ws_client.SimplexChatClient("ws://test")
    sends = _stub_send(client, {"type": "sentConfirmation"})
    asyncio.run(client.api_connect("simplex:/contact#abc"))
    assert sends == ["/c simplex:/contact#abc"]


# ---------------------------------------------------------------------------
# 4. send_chat_cmd corrId/response plumbing
# ---------------------------------------------------------------------------

class _FakeWebSocket:
    """Async-iterable WS stand-in that replays scripted frames after send()."""

    def __init__(self, frames: list[dict]) -> None:
        self.sent: list[str] = []
        self._recv_queue: asyncio.Queue = asyncio.Queue()
        for f in frames:
            self._recv_queue.put_nowait(json.dumps(f))

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        await self._recv_queue.put(None)

    def __aiter__(self) -> "_FakeWebSocket":
        return self

    async def __anext__(self) -> str:
        item = await self._recv_queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


def test_send_chat_cmd_resolves_matching_corr_id():
    async def run() -> None:
        ws = _FakeWebSocket(
            [{"corrId": "1", "resp": {"type": "activeUser", "user": {}}}]
        )

        async def factory(_url: str) -> _FakeWebSocket:
            return ws

        async with _ws_client.SimplexChatClient(
            "ws://test", connect_factory=factory
        ) as client:
            resp = await client.send_chat_cmd("/u", timeout=2.0)
            assert resp == {"type": "activeUser", "user": {}}
            sent = json.loads(ws.sent[0])
            assert sent["cmd"] == "/u"
            assert sent["corrId"] == "1"

    asyncio.run(run())


def test_send_chat_cmd_drops_orphan_events():
    async def run() -> None:
        ws = _FakeWebSocket(
            [
                {"resp": {"type": "newChatItems", "chatItems": []}},
                {"corrId": "1", "resp": {"type": "activeUser", "user": {}}},
            ]
        )

        async def factory(_url: str) -> _FakeWebSocket:
            return ws

        async with _ws_client.SimplexChatClient(
            "ws://test", connect_factory=factory
        ) as client:
            resp = await client.send_chat_cmd("/u", timeout=2.0)
            assert resp["type"] == "activeUser"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 5. Dispatcher integration — simplex_command with mocked client
# ---------------------------------------------------------------------------

def _patch_client_class(monkeypatch, *, active_user, groups=None, contacts=None):
    """Replace ``_ws_client.SimplexChatClient`` with a factory returning a mock.

    Because cli.py uses ``from ._ws_client import SimplexChatClient`` inside
    its handlers, the resolution goes through ``sys.modules`` for the
    package's ``_ws_client`` submodule. Patching the attribute on the
    loaded module is therefore picked up by the lazy import.
    """
    client = MagicMock()
    client.api_get_active_user = AsyncMock(return_value=active_user)
    client.api_get_groups = AsyncMock(return_value=groups or [])
    client.api_get_contacts = AsyncMock(return_value=contacts or [])
    client.api_connect = AsyncMock(return_value={"type": "sentConfirmation"})

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=cm)
    monkeypatch.setattr(_ws_client, "SimplexChatClient", factory)
    return client


def test_simplex_command_list_prints_user_and_groups(monkeypatch, capsys):
    monkeypatch.setenv("SIMPLEX_WS_URL", "ws://test")
    _patch_client_class(
        monkeypatch,
        active_user=_ws_client.ActiveUser(user_id=1, display_name="Alice", raw={}),
        groups=[
            _ws_client.GroupInfo(group_id=10, display_name="alpha", raw={}),
            _ws_client.GroupInfo(group_id=20, display_name="beta", raw={}),
        ],
        contacts=[
            _ws_client.ContactInfo(contact_id=5, display_name="bob", raw={}),
        ],
    )
    _cli.simplex_command(_ns(simplex_action="list"))
    out = capsys.readouterr().out
    assert "Alice" in out
    assert "10  alpha" in out
    assert "20  beta" in out
    assert "5  bob" in out


def test_simplex_command_join_polls_until_group_appears(monkeypatch, capsys):
    monkeypatch.setenv("SIMPLEX_WS_URL", "ws://test")
    before = [_ws_client.GroupInfo(group_id=1, display_name="existing", raw={})]
    after = before + [
        _ws_client.GroupInfo(group_id=99, display_name="brand-new", raw={})
    ]
    poll_returns = [before, after]

    client = _patch_client_class(
        monkeypatch,
        active_user=_ws_client.ActiveUser(user_id=1, display_name="A", raw={}),
    )
    client.api_get_groups = AsyncMock(side_effect=lambda: poll_returns.pop(0))

    monkeypatch.setattr(_cli, "JOIN_POLL_INTERVAL_S", 0.01)

    _cli.simplex_command(
        _ns(
            simplex_action="join",
            invite_link="simplex:/contact#xyz",
            timeout=5,
        )
    )
    out = capsys.readouterr().out
    assert "brand-new" in out
    assert "groupId=99" in out
    client.api_connect.assert_awaited_once_with("simplex:/contact#xyz")


def test_simplex_command_unknown_action_exits_with_usage(capsys):
    with pytest.raises(SystemExit) as exc:
        _cli.simplex_command(_ns(simplex_action=None))
    assert exc.value.code == 2
    assert "usage" in capsys.readouterr().err
