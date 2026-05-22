"""Tests for SimpleX outbound media (PR-5).

Covers:
  - Helper functions: thumbnail / duration / poster degrade gracefully
    when their optional deps (Pillow, ffmpeg, ffprobe) are missing.
  - _build_outbound_msg_content shape for each kind.
  - _stage_for_send: in-place use, copy-into-staging, remote download.
  - Adapter send_image_file / send_voice / send_video / send_document /
    send_animation: build /_send command with correct fileSource and
    target syntax.
  - Fallback to text when SIMPLEX_FILE_DIR is unset.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_simplex = load_plugin_adapter("simplex")
SimplexAdapter = _simplex.SimplexAdapter


_PIL_AVAILABLE = True
try:
    from PIL import Image  # noqa: F401
except ImportError:
    _PIL_AVAILABLE = False

_FFPROBE_AVAILABLE = shutil.which("ffprobe") is not None
_FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None


# Test fixtures — small valid binaries.
_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x00\x00\x00\x00:~\x9bU"
    b"\x00\x00\x00\nIDATx\x9cc`\x00\x00\x00\x02\x00\x01\xe2!\xbc3"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _adapter(monkeypatch, **env) -> SimplexAdapter:
    for k in (
        "SIMPLEX_FILE_DIR",
        "SIMPLEX_DAEMON_FILES_FOLDER",
    ):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cfg = _simplex.PlatformConfig(enabled=True, extra={"ws_url": "ws://test"})
    adapter = SimplexAdapter(cfg)
    # Capture _send_ws payloads instead of actually writing to a WS.
    adapter._send_ws = AsyncMock()  # type: ignore[assignment]
    return adapter


# ---------------------------------------------------------------------------
# 1. Helper degradation
# ---------------------------------------------------------------------------

def test_make_image_thumbnail_returns_none_without_pillow(monkeypatch, tmp_path):
    # Force the import to fail even if PIL is installed.
    monkeypatch.setitem(sys.modules, "PIL", None)
    img = tmp_path / "x.png"
    img.write_bytes(_PNG_1X1)
    assert _simplex._make_image_thumbnail(img) is None


@pytest.mark.skipif(not _PIL_AVAILABLE, reason="Pillow not installed")
def test_make_image_thumbnail_returns_data_url(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(_PNG_1X1)
    result = _simplex._make_image_thumbnail(img)
    assert result is not None
    assert result.startswith("data:image/jpeg;base64,")


def test_make_image_thumbnail_handles_corrupt_file(tmp_path):
    img = tmp_path / "bad.png"
    img.write_bytes(b"not a png")
    # Either Pillow returns None (corrupt) or Pillow isn't installed; both fine.
    assert _simplex._make_image_thumbnail(img) is None


def test_probe_duration_returns_none_when_ffprobe_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(_simplex.shutil, "which", lambda name: None)
    assert _simplex._probe_duration_seconds(tmp_path / "x.mp3") is None


def test_extract_video_poster_returns_none_when_ffmpeg_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(_simplex.shutil, "which", lambda name: None)
    assert _simplex._extract_video_poster(tmp_path / "x.mp4") is None


# ---------------------------------------------------------------------------
# 2. _build_outbound_msg_content shape
# ---------------------------------------------------------------------------

def test_build_image_msg_content_includes_caption(tmp_path, monkeypatch):
    monkeypatch.setattr(_simplex, "_make_image_thumbnail", lambda _p: None)
    out = _simplex._build_outbound_msg_content("image", tmp_path / "x.jpg", "look!")
    assert out == {"type": "image", "text": "look!"}


def test_build_image_msg_content_attaches_thumbnail_when_available(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        _simplex, "_make_image_thumbnail", lambda _p: "data:image/jpeg;base64,xyz"
    )
    out = _simplex._build_outbound_msg_content("image", tmp_path / "x.jpg", None)
    assert out["type"] == "image"
    assert out["image"] == "data:image/jpeg;base64,xyz"


def test_build_voice_msg_content_includes_duration(tmp_path, monkeypatch):
    monkeypatch.setattr(_simplex, "_probe_duration_seconds", lambda _p: 42)
    out = _simplex._build_outbound_msg_content("voice", tmp_path / "v.ogg", None)
    assert out == {"type": "voice", "text": "", "duration": 42}


def test_build_voice_msg_content_zero_duration_when_probe_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(_simplex, "_probe_duration_seconds", lambda _p: None)
    out = _simplex._build_outbound_msg_content("voice", tmp_path / "v.ogg", None)
    assert out["duration"] == 0


def test_build_video_msg_content_with_poster(tmp_path, monkeypatch):
    monkeypatch.setattr(_simplex, "_probe_duration_seconds", lambda _p: 15)
    monkeypatch.setattr(
        _simplex, "_extract_video_poster", lambda _p: "data:image/jpeg;base64,p"
    )
    out = _simplex._build_outbound_msg_content("video", tmp_path / "v.mp4", "clip")
    assert out["type"] == "video"
    assert out["text"] == "clip"
    assert out["duration"] == 15
    assert out["image"] == "data:image/jpeg;base64,p"


def test_build_file_msg_content_is_minimal(tmp_path):
    out = _simplex._build_outbound_msg_content("file", tmp_path / "x.pdf", "doc")
    assert out == {"type": "file", "text": "doc"}


def test_build_msg_content_rejects_unknown_kind(tmp_path):
    with pytest.raises(ValueError):
        _simplex._build_outbound_msg_content("nonsense", tmp_path / "x", None)


# ---------------------------------------------------------------------------
# 3. _stage_for_send
# ---------------------------------------------------------------------------

def test_stage_returns_none_without_bind_mount(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch)
    src = tmp_path / "a.jpg"
    src.write_bytes(_PNG_1X1)
    assert asyncio.run(adapter._stage_for_send(str(src))) is None


def test_stage_uses_file_in_place_when_under_bind_mount(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, SIMPLEX_FILE_DIR=str(tmp_path))
    src = tmp_path / "a.jpg"
    src.write_bytes(_PNG_1X1)
    staged = asyncio.run(adapter._stage_for_send(str(src)))
    assert staged is not None
    host_path, name = staged
    assert host_path == src
    assert name == "a.jpg"


def test_stage_copies_file_outside_bind_mount(monkeypatch, tmp_path):
    bind = tmp_path / "bind"
    bind.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    src = elsewhere / "photo.jpg"
    src.write_bytes(_PNG_1X1)

    adapter = _adapter(monkeypatch, SIMPLEX_FILE_DIR=str(bind))
    staged = asyncio.run(adapter._stage_for_send(str(src)))
    assert staged is not None
    host_path, name = staged
    assert host_path.parent == bind
    assert name.endswith("-photo.jpg")
    assert host_path.read_bytes() == _PNG_1X1


def test_stage_resolves_file_url(monkeypatch, tmp_path):
    bind = tmp_path / "bind"
    bind.mkdir()
    src = tmp_path / "x.jpg"
    src.write_bytes(_PNG_1X1)

    adapter = _adapter(monkeypatch, SIMPLEX_FILE_DIR=str(bind))
    staged = asyncio.run(adapter._stage_for_send(f"file://{src}"))
    assert staged is not None
    host_path, _ = staged
    assert host_path.exists()


def test_stage_downloads_remote_url(monkeypatch, tmp_path):
    bind = tmp_path / "bind"
    bind.mkdir()

    def fake_fetch(target_path, url, *, timeout=None):
        target_path.write_bytes(b"downloaded")

    monkeypatch.setattr(_simplex, "_fetch_remote_to", fake_fetch)
    adapter = _adapter(monkeypatch, SIMPLEX_FILE_DIR=str(bind))
    staged = asyncio.run(adapter._stage_for_send("https://example.com/photo.png"))
    assert staged is not None
    host_path, name = staged
    assert host_path.parent == bind
    assert name.endswith("-photo.png")
    assert host_path.read_bytes() == b"downloaded"


def test_stage_returns_none_when_download_fails(monkeypatch, tmp_path):
    bind = tmp_path / "bind"
    bind.mkdir()

    def boom(*_a, **_kw):
        raise OSError("network down")

    monkeypatch.setattr(_simplex, "_fetch_remote_to", boom)
    adapter = _adapter(monkeypatch, SIMPLEX_FILE_DIR=str(bind))
    staged = asyncio.run(adapter._stage_for_send("https://example.com/x.png"))
    assert staged is None


# ---------------------------------------------------------------------------
# 4. _container_path_for
# ---------------------------------------------------------------------------

def test_container_path_uses_daemon_root_default(monkeypatch):
    adapter = _adapter(monkeypatch, SIMPLEX_FILE_DIR="/tmp/host")
    assert adapter._container_path_for("a.jpg") == "/root/.simplex/files/a.jpg"


def test_container_path_respects_daemon_folder_env(monkeypatch):
    adapter = _adapter(
        monkeypatch,
        SIMPLEX_FILE_DIR="/tmp/host",
        SIMPLEX_DAEMON_FILES_FOLDER="/data/files/",
    )
    assert adapter._container_path_for("a.jpg") == "/data/files/a.jpg"


# ---------------------------------------------------------------------------
# 5. Adapter outbound media send paths
# ---------------------------------------------------------------------------

def _sent_payload(adapter) -> dict:
    """Return the first payload _send_ws was awaited with."""
    assert adapter._send_ws.await_count == 1
    return adapter._send_ws.await_args.args[0]


def test_send_image_file_to_group_emits_send_command(monkeypatch, tmp_path):
    bind = tmp_path / "bind"
    bind.mkdir()
    src = bind / "photo.jpg"
    src.write_bytes(_PNG_1X1)
    monkeypatch.setattr(_simplex, "_make_image_thumbnail", lambda _p: None)

    adapter = _adapter(monkeypatch, SIMPLEX_FILE_DIR=str(bind))
    result = asyncio.run(
        adapter.send_image_file("group:42", str(src), caption="look")
    )
    assert result.success

    payload = _sent_payload(adapter)
    assert payload["cmd"].startswith("/_send #42 json [")
    # Body JSON is the part after "json ".
    body = json.loads(payload["cmd"].split("json ", 1)[1])
    assert body[0]["msgContent"] == {"type": "image", "text": "look"}
    assert body[0]["fileSource"]["filePath"] == "/root/.simplex/files/photo.jpg"


def test_send_image_file_to_dm_uses_at_prefix(monkeypatch, tmp_path):
    bind = tmp_path / "bind"
    bind.mkdir()
    src = bind / "img.jpg"
    src.write_bytes(_PNG_1X1)
    monkeypatch.setattr(_simplex, "_make_image_thumbnail", lambda _p: None)

    adapter = _adapter(monkeypatch, SIMPLEX_FILE_DIR=str(bind))
    asyncio.run(adapter.send_image_file("777", str(src)))
    payload = _sent_payload(adapter)
    assert payload["cmd"].startswith("/_send @777 json [")


def test_send_image_url_downloads_then_sends(monkeypatch, tmp_path):
    bind = tmp_path / "bind"
    bind.mkdir()

    def fake_fetch(target_path, url, *, timeout=None):
        target_path.write_bytes(_PNG_1X1)

    monkeypatch.setattr(_simplex, "_fetch_remote_to", fake_fetch)
    monkeypatch.setattr(_simplex, "_make_image_thumbnail", lambda _p: None)
    adapter = _adapter(monkeypatch, SIMPLEX_FILE_DIR=str(bind))

    asyncio.run(adapter.send_image("group:42", "https://example.com/img.png"))
    payload = _sent_payload(adapter)
    assert payload["cmd"].startswith("/_send #42 json [")


def test_send_image_falls_back_to_text_without_bind_mount(monkeypatch):
    adapter = _adapter(monkeypatch)
    # No SIMPLEX_FILE_DIR set → degrade to text URL.
    asyncio.run(adapter.send_image("group:42", "https://example.com/img.png"))
    payload = _sent_payload(adapter)
    # Text path: send() uses the #[id] / @[id] grammar, not /_send.
    assert payload["cmd"] == "#[42] https://example.com/img.png"


def test_send_voice_builds_voice_msg_content(monkeypatch, tmp_path):
    bind = tmp_path / "bind"
    bind.mkdir()
    src = bind / "memo.ogg"
    src.write_bytes(b"OggS")
    monkeypatch.setattr(_simplex, "_probe_duration_seconds", lambda _p: 10)
    adapter = _adapter(monkeypatch, SIMPLEX_FILE_DIR=str(bind))

    asyncio.run(adapter.send_voice("group:1", str(src)))
    body = json.loads(_sent_payload(adapter)["cmd"].split("json ", 1)[1])
    assert body[0]["msgContent"] == {"type": "voice", "text": "", "duration": 10}


def test_send_video_includes_poster_when_available(monkeypatch, tmp_path):
    bind = tmp_path / "bind"
    bind.mkdir()
    src = bind / "clip.mp4"
    src.write_bytes(b"ftyp-stub")
    monkeypatch.setattr(_simplex, "_probe_duration_seconds", lambda _p: 7)
    monkeypatch.setattr(
        _simplex, "_extract_video_poster", lambda _p: "data:image/jpeg;base64,p"
    )
    adapter = _adapter(monkeypatch, SIMPLEX_FILE_DIR=str(bind))

    asyncio.run(adapter.send_video("group:1", str(src), caption="clip"))
    body = json.loads(_sent_payload(adapter)["cmd"].split("json ", 1)[1])
    mc = body[0]["msgContent"]
    assert mc["type"] == "video"
    assert mc["duration"] == 7
    assert mc["image"] == "data:image/jpeg;base64,p"
    assert mc["text"] == "clip"


def test_send_document_builds_file_msg_content(monkeypatch, tmp_path):
    bind = tmp_path / "bind"
    bind.mkdir()
    src = bind / "report.pdf"
    src.write_bytes(b"%PDF-1.4")
    adapter = _adapter(monkeypatch, SIMPLEX_FILE_DIR=str(bind))

    asyncio.run(adapter.send_document("group:9", str(src), caption="weekly"))
    body = json.loads(_sent_payload(adapter)["cmd"].split("json ", 1)[1])
    assert body[0]["msgContent"] == {"type": "file", "text": "weekly"}


def test_send_animation_aliases_to_video(monkeypatch, tmp_path):
    bind = tmp_path / "bind"
    bind.mkdir()
    src = bind / "anim.mp4"
    src.write_bytes(b"ftyp-stub")
    monkeypatch.setattr(_simplex, "_probe_duration_seconds", lambda _p: 3)
    monkeypatch.setattr(_simplex, "_extract_video_poster", lambda _p: None)
    adapter = _adapter(monkeypatch, SIMPLEX_FILE_DIR=str(bind))

    asyncio.run(adapter.send_animation("group:9", str(src)))
    body = json.loads(_sent_payload(adapter)["cmd"].split("json ", 1)[1])
    assert body[0]["msgContent"]["type"] == "video"
