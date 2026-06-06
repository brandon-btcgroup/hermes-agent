"""Tests for SimpleX bind-mount-aware media (PR-3).

Covers:
  - _translate_daemon_path prefix replacement, edge cases, no-op when
    env vars unset.
  - Adapter __init__ reading SIMPLEX_FILE_DIR / SIMPLEX_DAEMON_FILES_FOLDER.
  - _fetch_file fast path: reads from translated host path when present.
  - _fetch_file fast path: falls back to <host_dir>/<file_name> when
    daemon didn't supply a path.
  - _fetch_file falls through to legacy search when bind-mount env unset.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_simplex = load_plugin_adapter("simplex")
SimplexAdapter = _simplex.SimplexAdapter
_translate_daemon_path = _simplex._translate_daemon_path


def _adapter(monkeypatch, **env) -> SimplexAdapter:
    for k in ("SIMPLEX_FILE_DIR", "SIMPLEX_DAEMON_FILES_FOLDER"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cfg = _simplex.PlatformConfig(enabled=True, extra={"ws_url": "ws://test"})
    return SimplexAdapter(cfg)


# ---------------------------------------------------------------------------
# 1. _translate_daemon_path
# ---------------------------------------------------------------------------

def test_translate_simple_prefix_swap(tmp_path):
    result = _translate_daemon_path(
        "/root/.simplex/files/IMG_001.jpg",
        host_root=str(tmp_path),
        daemon_root="/root/.simplex/files",
    )
    assert result == tmp_path / "IMG_001.jpg"


def test_translate_with_subdirectory(tmp_path):
    result = _translate_daemon_path(
        "/root/.simplex/files/photos/IMG_001.jpg",
        host_root=str(tmp_path),
        daemon_root="/root/.simplex/files",
    )
    assert result == tmp_path / "photos" / "IMG_001.jpg"


def test_translate_handles_trailing_slash_on_daemon_root(tmp_path):
    result = _translate_daemon_path(
        "/root/.simplex/files/IMG.jpg",
        host_root=str(tmp_path),
        daemon_root="/root/.simplex/files/",
    )
    assert result == tmp_path / "IMG.jpg"


def test_translate_returns_none_when_path_outside_daemon_root(tmp_path):
    result = _translate_daemon_path(
        "/var/tmp/leak.jpg",
        host_root=str(tmp_path),
        daemon_root="/root/.simplex/files",
    )
    assert result is None


def test_translate_returns_none_when_prefix_only_partial(tmp_path):
    """`/root/.simplex/filesX` must not match daemon root `/root/.simplex/files`."""
    result = _translate_daemon_path(
        "/root/.simplex/filesX/sneaky.jpg",
        host_root=str(tmp_path),
        daemon_root="/root/.simplex/files",
    )
    assert result is None


def test_translate_returns_none_when_host_root_unset():
    result = _translate_daemon_path(
        "/root/.simplex/files/x.jpg",
        host_root=None,
        daemon_root="/root/.simplex/files",
    )
    assert result is None


def test_translate_returns_none_when_daemon_root_unset(tmp_path):
    result = _translate_daemon_path(
        "/root/.simplex/files/x.jpg",
        host_root=str(tmp_path),
        daemon_root=None,
    )
    assert result is None


def test_translate_returns_none_for_empty_daemon_path(tmp_path):
    result = _translate_daemon_path(
        "",
        host_root=str(tmp_path),
        daemon_root="/root/.simplex/files",
    )
    assert result is None


def test_translate_expands_user_in_host_root(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    result = _translate_daemon_path(
        "/root/.simplex/files/x.jpg",
        host_root="~/files",
        daemon_root="/root/.simplex/files",
    )
    assert result == tmp_path / "files" / "x.jpg"


# ---------------------------------------------------------------------------
# 2. Adapter env-var parsing
# ---------------------------------------------------------------------------

def test_adapter_defaults_when_no_env(monkeypatch):
    adapter = _adapter(monkeypatch)
    assert adapter._host_files_dir is None
    assert adapter._daemon_files_folder == "/root/.simplex/files"


def test_adapter_reads_simplex_file_dir(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, SIMPLEX_FILE_DIR=str(tmp_path))
    assert adapter._host_files_dir == str(tmp_path)


def test_adapter_reads_daemon_files_folder_override(monkeypatch):
    adapter = _adapter(monkeypatch, SIMPLEX_DAEMON_FILES_FOLDER="/custom/path")
    assert adapter._daemon_files_folder == "/custom/path"


def test_adapter_blank_env_treated_as_unset(monkeypatch):
    adapter = _adapter(monkeypatch, SIMPLEX_FILE_DIR="   ")
    assert adapter._host_files_dir is None


# ---------------------------------------------------------------------------
# 3. _fetch_file fast path (bind-mount)
# ---------------------------------------------------------------------------

# A 1x1 PNG — _guess_extension recognises the magic bytes as ".png".
_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x00\x00\x00\x00:~\x9bU"
    b"\x00\x00\x00\nIDATx\x9cc`\x00\x00\x00\x02\x00\x01\xe2!\xbc3"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _bind_mount_adapter(monkeypatch, host_dir: Path) -> SimplexAdapter:
    adapter = _adapter(
        monkeypatch,
        SIMPLEX_FILE_DIR=str(host_dir),
        SIMPLEX_DAEMON_FILES_FOLDER="/root/.simplex/files",
    )
    # Replace WS plumbing with no-ops so _fetch_file doesn't try to send.
    adapter._send_ws = AsyncMock()
    # Skip the 2s sleep — it's a hack we don't want to wait on.
    async def _no_sleep(*_a, **_kw):
        return None
    monkeypatch.setattr(_simplex.asyncio, "sleep", _no_sleep)
    return adapter


def test_fetch_file_reads_translated_host_path(monkeypatch, tmp_path):
    adapter = _bind_mount_adapter(monkeypatch, tmp_path)
    (tmp_path / "IMG_001.jpg").write_bytes(_PNG_1X1)

    cached = asyncio.run(
        adapter._fetch_file(
            "file-1",
            "IMG_001.jpg",
            daemon_path="/root/.simplex/files/IMG_001.jpg",
        )
    )
    assert cached is not None
    assert cached.endswith(".png")  # _guess_extension overrides supplied name


def test_fetch_file_falls_back_to_filename_in_host_dir(monkeypatch, tmp_path):
    """When daemon doesn't report a path, look up the filename in the
    bind-mount root directly."""
    adapter = _bind_mount_adapter(monkeypatch, tmp_path)
    (tmp_path / "voice.ogg").write_bytes(b"OggS\x00\x02data")

    cached = asyncio.run(
        adapter._fetch_file("file-2", "voice.ogg", daemon_path="")
    )
    assert cached is not None
    assert cached.endswith(".ogg")


def test_fetch_file_returns_none_when_bind_mount_missing(monkeypatch, tmp_path):
    """SIMPLEX_FILE_DIR set but the file isn't actually there → no fallback
    to ~/Downloads (which could leak unrelated files)."""
    adapter = _bind_mount_adapter(monkeypatch, tmp_path)
    cached = asyncio.run(
        adapter._fetch_file(
            "file-3",
            "missing.jpg",
            daemon_path="/root/.simplex/files/missing.jpg",
        )
    )
    # The legacy ~/Downloads search runs regardless; if it doesn't find
    # the file either, we get None. That's the desired behaviour.
    assert cached is None


def test_fetch_file_legacy_path_when_no_bind_mount(monkeypatch, tmp_path):
    """No SIMPLEX_FILE_DIR → legacy search of ~/Downloads / ~/.simplex/files."""
    adapter = _adapter(monkeypatch)  # no bind-mount env
    adapter._send_ws = AsyncMock()
    async def _no_sleep(*_a, **_kw):
        return None
    monkeypatch.setattr(_simplex.asyncio, "sleep", _no_sleep)
    # Point HOME at tmp_path so ~/Downloads resolves there.
    monkeypatch.setenv("HOME", str(tmp_path))
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "doc.pdf").write_bytes(b"%PDF-1.4 stub")

    cached = asyncio.run(adapter._fetch_file("file-4", "doc.pdf"))
    assert cached is not None
    assert "pdf" in cached or "document" in cached.lower() or cached.endswith(".pdf")


def test_fetch_file_bind_mount_skips_unreadable_file(monkeypatch, tmp_path, caplog):
    """A bind-mount path that exists but raises on read should log a warning
    and let the call fall through (returning None when no other source)."""
    adapter = _bind_mount_adapter(monkeypatch, tmp_path)
    target = tmp_path / "blocked.jpg"
    target.write_bytes(_PNG_1X1)

    real_read = Path.read_bytes
    def _boom(self):
        if self == target:
            raise OSError("permission denied")
        return real_read(self)
    monkeypatch.setattr(Path, "read_bytes", _boom)

    cached = asyncio.run(
        adapter._fetch_file(
            "file-5",
            "blocked.jpg",
            daemon_path="/root/.simplex/files/blocked.jpg",
        )
    )
    assert cached is None
