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

Capabilities:
  - Inbound text + image/file/voice/video from configured groups.
  - Outbound text + image/file/voice/video via ``/_send``.
  - Self-echo filtered via ``chatDir.type == 'groupRcv'``.
  - Allowlist enforced on the sender's display name
    (SIMPLEX_ALLOWED_USERS), with the same shape as Signal/WhatsApp.
  - Missed-message replay on reconnect via per-group cursors stored at
    ``$HERMES_HOME/simplex/cursors.json``.

Media bind-mount:
  simplex-chat reads/writes attachments under a "files folder" inside its
  container (default ``/root/.simplex/files``). For Hermes (running outside
  the container) to deliver attachments to the agent and to send outbound
  files, the container's files folder must be bind-mounted to a host
  directory readable by Hermes. The host path is configured via
  ``SIMPLEX_FILE_DIR`` (default ``$HERMES_HOME/cache/simplex-files``); the
  container path is ``SIMPLEX_DAEMON_FILES_FOLDER`` (default
  ``/root/.simplex/files``). When the host path isn't readable on connect,
  media is disabled with a warning and text-only operation continues.

Known gaps (tracked for v2.1+):
  - Direct messages, reactions, streaming edits, typing indicators.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import random
import shutil
import subprocess
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple
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

# Hard cap on the in-memory dedupe set; bounds memory regardless of traffic.
_DEDUPE_RING_SIZE = 1024

# Replay paginates /_get chat in batches of this size.
_REPLAY_PAGE_SIZE = 50

# Default outbound thumbnail size for image/video — small enough to keep the
# /_send command JSON compact, large enough that phone clients render a usable
# preview before the full file downloads.
_THUMBNAIL_MAX_PX = 224

# File-size guardrail for inbound media; matches a reasonable per-message
# default and is overridable via SIMPLEX_MAX_FILE_BYTES.
_DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MiB

# Inbound file-download wait window. simplex-chat emits newChatItems the
# moment the file invitation is received, well before the XFTP download
# completes; the adapter buffers the chat item and polls /_get chat for
# rcvComplete, falling back to a text placeholder after the deadline.
_DEFAULT_FILE_WAIT_S = 60.0
_FILE_POLL_INTERVAL_S = 3.0



def check_simplex_requirements() -> bool:
    """Return True iff the env has enough config for the SimpleX adapter."""
    return bool(os.getenv("SIMPLEX_WS_URL") and os.getenv("SIMPLEX_GROUP_IDS"))


def _hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes")


def _cursor_file_path() -> Path:
    return _hermes_home() / "simplex" / "cursors.json"


def _find_chat_item_with_file(
    chat_items: List[Dict[str, Any]], file_id: int
) -> Optional[Dict[str, Any]]:
    for entry in chat_items:
        if not isinstance(entry, dict):
            continue
        f = entry.get("file") or {}
        if f.get("fileId") == file_id:
            return entry
    return None


def _max_item_id(chat_items: List[Dict[str, Any]]) -> Optional[int]:
    best: Optional[int] = None
    for entry in chat_items:
        meta = (entry.get("meta") or {}) if isinstance(entry, dict) else {}
        raw = meta.get("itemId")
        if isinstance(raw, int) and (best is None or raw > best):
            best = raw
    return best


def _default_file_dir() -> Path:
    return _hermes_home() / "cache" / "simplex-files"


def _make_image_thumbnail(path: Path) -> Optional[str]:
    """Return a base64 data URL for an image thumbnail, or None if Pillow
    is unavailable or the source can't be decoded."""
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return None
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail((_THUMBNAIL_MAX_PX, _THUMBNAIL_MAX_PX))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=70)
            data = buf.getvalue()
    except Exception as e:
        logger.debug("simplex: thumbnail generation failed for %s: %r", path, e)
        return None
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def _probe_duration_seconds(path: Path) -> Optional[int]:
    """Return integer duration of an audio/video file via ffprobe, or None."""
    if shutil.which("ffprobe") is None:
        return None
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except Exception as e:
        logger.debug("simplex: ffprobe failed for %s: %r", path, e)
        return None
    try:
        return max(0, int(round(float(proc.stdout.strip()))))
    except ValueError:
        return None


def _extract_video_poster(path: Path) -> Optional[str]:
    """Return a base64 data URL for a video poster frame via ffmpeg, or None."""
    if shutil.which("ffmpeg") is None:
        return None
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-loglevel", "error", "-y",
                "-ss", "1", "-i", str(path),
                "-vframes", "1",
                "-vf", f"scale={_THUMBNAIL_MAX_PX}:-1",
                "-f", "image2", "-",
            ],
            capture_output=True, timeout=15, check=True,
        )
    except Exception as e:
        logger.debug("simplex: ffmpeg poster extraction failed for %s: %r", path, e)
        return None
    if not proc.stdout:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(proc.stdout).decode("ascii")


def _resolve_local_path(url_or_path: str) -> Optional[Path]:
    """Return a local Path for a ``file://`` URL or bare path; None for http(s)."""
    if not url_or_path:
        return None
    if url_or_path.startswith("file://"):
        return Path(urllib.parse.unquote(url_or_path[7:]))
    if "://" in url_or_path:
        return None
    return Path(url_or_path)


async def _fetch_remote_to(temp_path: Path, url: str, *, timeout: float = 30.0) -> None:
    """Download an http(s) URL to ``temp_path``. Raises on failure."""
    def _do_fetch() -> None:
        with urllib.request.urlopen(url, timeout=timeout) as resp, open(temp_path, "wb") as out:
            shutil.copyfileobj(resp, out)
    await asyncio.to_thread(_do_fetch)


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _build_outbound_msg_content(
    kind: str, host_path: Path, caption: Optional[str]
) -> Dict[str, Any]:
    """Construct the msgContent dict for an outbound /_send.

    Synchronous: does ffprobe/ffmpeg/PIL work; safe to call from a thread.
    """
    text = caption or ""
    if kind == "image":
        thumb = _make_image_thumbnail(host_path)
        out: Dict[str, Any] = {"type": "image", "text": text}
        if thumb:
            out["image"] = thumb
        return out
    if kind == "file":
        return {"type": "file", "text": text}
    if kind == "voice":
        out = {"type": "voice", "text": text}
        duration = _probe_duration_seconds(host_path)
        out["duration"] = duration if duration is not None else 0
        return out
    if kind == "video":
        out = {"type": "video", "text": text}
        duration = _probe_duration_seconds(host_path)
        out["duration"] = duration if duration is not None else 0
        poster = _extract_video_poster(host_path)
        if poster:
            out["image"] = poster
        return out
    raise ValueError(f"unknown media kind: {kind}")


def _classify_msg_content(mc_type: Optional[str]) -> Tuple[Optional[MessageType], Optional[str]]:
    """Map a simplex-chat msgContent.type to (MessageType, MIME) for inbound."""
    if mc_type == "text":
        return MessageType.TEXT, None
    if mc_type == "image":
        return MessageType.PHOTO, "image/jpeg"
    if mc_type == "file":
        return MessageType.DOCUMENT, "application/octet-stream"
    if mc_type == "voice":
        return MessageType.VOICE, "audio/ogg"
    if mc_type == "video":
        return MessageType.VIDEO, "video/mp4"
    return None, None


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

        # Replay state: per-group last-dispatched itemId persisted to disk,
        # plus an in-memory ring of recently-dispatched ids that lets replay
        # and the live stream coexist without double-processing.
        self._replay_disabled = (
            os.getenv("SIMPLEX_REPLAY_DISABLE", "").strip().lower() in {"1", "true", "yes"}
        )
        try:
            self._replay_max = max(0, int(os.getenv("SIMPLEX_REPLAY_MAX", "200")))
        except ValueError:
            self._replay_max = 200
        self._cursor_path: Path = _cursor_file_path()
        self._cursors: Dict[str, int] = self._load_cursors()
        self._seen_item_ids: Deque[int] = deque(maxlen=_DEDUPE_RING_SIZE)
        self._seen_item_set: set[int] = set()

        # Media: bind-mount that the daemon and Hermes both see.
        host_dir_raw = extra.get("file_dir")
        self._file_dir: Path = (
            Path(host_dir_raw).expanduser() if host_dir_raw else _default_file_dir()
        )
        self._daemon_files_folder: str = (
            extra.get("daemon_files_folder") or "/root/.simplex/files"
        )
        try:
            self._max_file_bytes = max(
                0, int(os.getenv("SIMPLEX_MAX_FILE_BYTES", str(_DEFAULT_MAX_FILE_BYTES)))
            )
        except ValueError:
            self._max_file_bytes = _DEFAULT_MAX_FILE_BYTES
        try:
            self._file_wait_s = max(
                0.0, float(os.getenv("SIMPLEX_FILE_WAIT_S", str(_DEFAULT_FILE_WAIT_S)))
            )
        except ValueError:
            self._file_wait_s = _DEFAULT_FILE_WAIT_S
        # Set on connect once the bind-mount is verified writable.
        self._media_enabled: bool = False
        # In-flight media items keyed by daemon fileId; entries are scrubbed
        # by their poll task on completion or timeout.
        self._pending_media: Dict[int, asyncio.Task] = {}

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
        await self._replay_missed_messages()
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
        **kwargs,
    ) -> SendResult:
        return await self._send_media(
            chat_id, image_url, caption=caption, kind="image"
        )

    async def send_image_file(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_media(
            chat_id, file_path, caption=caption, kind="image"
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_media(
            chat_id, file_path, caption=caption, kind="file"
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        **kwargs,
    ) -> SendResult:
        return await self._send_media(chat_id, audio_path, caption=None, kind="voice")

    async def send_video(
        self,
        chat_id: str,
        video_url: str,
        caption: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_media(
            chat_id, video_url, caption=caption, kind="video"
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
        self._verify_media_dir()
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
                await self._replay_missed_messages()
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
        mc_type = msg_content.get("type")
        message_type, media_mime = _classify_msg_content(mc_type)
        if message_type is None:
            return  # unsupported content type (link, unknown extensions)

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
        # Dedupe: replay and the live stream can both surface the same
        # itemId in the seam around reconnect; let the first one win.
        if isinstance(item_id, int):
            if item_id in self._seen_item_set:
                return
            self._note_seen(item_id)
        message_id = str(item_id) if isinstance(item_id, int) else uuid4().hex

        if message_type != MessageType.TEXT:
            host_path, resolution = self._resolve_inbound_file(chat_item, gid_str)
            if resolution == "pending" and self._file_wait_s > 0:
                # File still downloading. Buffer the item and let a background
                # poll task dispatch it once rcvComplete fires (or fall back
                # after SIMPLEX_FILE_WAIT_S). Cursor advances anyway: the
                # message is "seen" even if its file ultimately fails.
                if isinstance(item_id, int):
                    self._advance_cursor(gid_str, item_id)
                self._spawn_pending_media_poll(
                    item=item,
                    chat_item=chat_item,
                    chat_info=chat_info,
                    gid_str=gid_str,
                    sender=sender,
                    message_id=message_id,
                    text=text,
                    message_type=message_type,
                    media_mime=media_mime,
                )
                return
            if host_path is None:
                # "disabled" / "rejected" — won't recover by waiting.
                fname = ((chat_item.get("file") or {}).get("fileName")) or "(file)"
                text = (
                    f"[simplex {message_type.value} — {fname} not delivered]"
                    + (f" {text}" if text else "")
                )
                message_type = MessageType.TEXT
                media_urls: List[str] = []
                media_types: List[str] = []
            else:
                media_urls = [str(host_path)]
                media_types = [media_mime or "application/octet-stream"]
        else:
            media_urls = []
            media_types = []

        await self._build_and_dispatch(
            item=item,
            chat_info=chat_info,
            gid_str=gid_str,
            sender=sender,
            message_id=message_id,
            text=text,
            message_type=message_type,
            media_urls=media_urls,
            media_types=media_types,
        )

        if isinstance(item_id, int):
            self._advance_cursor(gid_str, item_id)

    async def _build_and_dispatch(
        self,
        *,
        item: Dict[str, Any],
        chat_info: Dict[str, Any],
        gid_str: str,
        sender: str,
        message_id: str,
        text: str,
        message_type: MessageType,
        media_urls: List[str],
        media_types: List[str],
    ) -> None:
        group_info = chat_info.get("groupInfo") or {}
        source = self.build_source(
            chat_id=gid_str,
            chat_name=group_info.get("displayName", gid_str),
            chat_type="group",
            user_id=sender,
            user_name=sender,
        )
        event_obj = MessageEvent(
            text=text,
            message_type=message_type,
            source=source,
            message_id=message_id,
            raw_message=item,
            media_urls=media_urls,
            media_types=media_types,
        )
        try:
            await self.handle_message(event_obj)
        except Exception as e:
            logger.exception("simplex: handle_message raised: %r", e)

    # ── Outbound media ──────────────────────────────────────────────────

    async def _send_media(
        self,
        chat_id: str,
        source_url: str,
        *,
        caption: Optional[str],
        kind: str,
    ) -> SendResult:
        if not self._media_enabled:
            return SendResult(
                success=False,
                error=(
                    "simplex: media disabled — bind-mount the daemon's files "
                    "folder to SIMPLEX_FILE_DIR and restart Hermes"
                ),
            )
        if self._client is None:
            return SendResult(success=False, error="simplex: not connected", retryable=True)
        try:
            gid = int(chat_id)
        except (TypeError, ValueError):
            return SendResult(success=False, error=f"simplex: chat_id {chat_id!r} is not numeric")

        try:
            host_path, basename = await self._stage_outbound_file(source_url)
        except Exception as e:
            logger.warning("simplex: stage_outbound_file failed: %r", e)
            return SendResult(success=False, error=f"simplex: cannot stage file: {e}")

        if self._max_file_bytes:
            try:
                size = host_path.stat().st_size
            except OSError:
                size = 0
            if size > self._max_file_bytes:
                _safe_unlink(host_path)
                return SendResult(
                    success=False,
                    error=f"simplex: file exceeds SIMPLEX_MAX_FILE_BYTES ({size} > {self._max_file_bytes})",
                )

        msg_content = await asyncio.to_thread(
            _build_outbound_msg_content, kind, host_path, caption
        )
        container_path = self._container_path_for(basename)

        async with self._send_lock:
            try:
                resp = await self._client.api_send_message_with_file_to_group(
                    gid, msg_content, container_path
                )
            except Exception as e:
                logger.warning("simplex: api_send_message_with_file failed: %r", e)
                return SendResult(success=False, error=str(e), retryable=True)
        return SendResult(
            success=True,
            message_id=str(resp.item_id) if resp.item_id is not None else None,
            raw_response=resp.raw,
        )

    async def _stage_outbound_file(self, source_url: str) -> Tuple[Path, str]:
        """Copy/download the source into SIMPLEX_FILE_DIR with a uuid-prefixed
        name; return (host_path, basename). Raises on failure."""
        local = _resolve_local_path(source_url)
        if local is not None:
            if not local.exists():
                raise FileNotFoundError(f"source not found: {local}")
            ext = local.suffix or ""
            basename = f"{uuid4().hex}{ext}"
            dest = self._file_dir / basename
            await asyncio.to_thread(shutil.copy2, local, dest)
            return dest, basename
        # http(s) URL — download it
        parsed = urllib.parse.urlparse(source_url)
        ext = Path(parsed.path).suffix or ""
        basename = f"{uuid4().hex}{ext}"
        dest = self._file_dir / basename
        await _fetch_remote_to(dest, source_url)
        return dest, basename

    def _container_path_for(self, basename: str) -> str:
        # The daemon resolves filePath relative to its own filesystem; with
        # the recommended bind-mount, the basename in SIMPLEX_FILE_DIR maps
        # 1:1 into the daemon_files_folder.
        folder = self._daemon_files_folder.rstrip("/")
        return f"{folder}/{basename}"

    # ── Inbound media ───────────────────────────────────────────────────

    def _verify_media_dir(self) -> None:
        """Ensure SIMPLEX_FILE_DIR is usable; otherwise disable media + warn."""
        try:
            self._file_dir.mkdir(parents=True, exist_ok=True)
            probe = self._file_dir / ".hermes-simplex-write-test"
            probe.write_text("ok")
            probe.unlink()
        except OSError as e:
            logger.warning(
                "simplex: media disabled — %s is not writable (%r). "
                "Bind-mount the daemon's files folder to this host path "
                "to enable image/file/voice/video. Text continues to work.",
                self._file_dir, e,
            )
            self._media_enabled = False
            return
        self._media_enabled = True

    def _resolve_inbound_file(
        self, chat_item: Dict[str, Any], gid_str: str
    ) -> Tuple[Optional[Path], str]:
        """Resolve an inbound attachment to (host_path, status_tag).

        - ("ready", path)   — file is on disk; dispatch immediately with media
        - ("pending", None) — XFTP transfer hasn't completed yet; caller may
          buffer and poll
        - ("disabled", None) / ("rejected", None) — won't recover by waiting
          (media not enabled, file too large, missing fileName, missing file
          on host after rcvComplete); caller should downgrade to text
        """
        if not self._media_enabled:
            logger.debug("simplex: media disabled, dropping attachment in group %s", gid_str)
            return None, "disabled"
        file_envelope = chat_item.get("file") or {}
        fname = file_envelope.get("fileName")
        if not isinstance(fname, str) or not fname:
            logger.debug("simplex: inbound media has no fileName")
            return None, "rejected"
        size = file_envelope.get("fileSize")
        if isinstance(size, int) and self._max_file_bytes and size > self._max_file_bytes:
            logger.warning(
                "simplex: rejecting inbound file %r in group %s — %d bytes exceeds SIMPLEX_MAX_FILE_BYTES=%d",
                fname, gid_str, size, self._max_file_bytes,
            )
            return None, "rejected"
        status = (file_envelope.get("fileStatus") or {}).get("type")
        if status not in {"rcvComplete", "sndStored", "sndComplete", None}:
            return None, "pending"
        host_path = self._file_dir / fname
        if not host_path.exists():
            logger.warning(
                "simplex: file %r reported %s but %s is missing on host — "
                "check that SIMPLEX_FILE_DIR matches the daemon bind-mount",
                fname, status, host_path,
            )
            return None, "rejected"
        return host_path, "ready"

    def _spawn_pending_media_poll(
        self,
        *,
        item: Dict[str, Any],
        chat_item: Dict[str, Any],
        chat_info: Dict[str, Any],
        gid_str: str,
        sender: str,
        message_id: str,
        text: str,
        message_type: MessageType,
        media_mime: Optional[str],
    ) -> None:
        """Buffer an in-flight media item; poll /_get chat for rcvComplete."""
        file_envelope = chat_item.get("file") or {}
        file_id = file_envelope.get("fileId")
        if not isinstance(file_id, int):
            return
        if file_id in self._pending_media:
            return  # already polling
        task = asyncio.create_task(
            self._poll_pending_media(
                file_id=file_id,
                item=item,
                chat_item=chat_item,
                chat_info=chat_info,
                gid_str=gid_str,
                sender=sender,
                message_id=message_id,
                text=text,
                message_type=message_type,
                media_mime=media_mime,
            ),
            name=f"simplex-pending-file-{file_id}",
        )
        self._pending_media[file_id] = task
        task.add_done_callback(lambda _t, fid=file_id: self._pending_media.pop(fid, None))

    async def _poll_pending_media(
        self,
        *,
        file_id: int,
        item: Dict[str, Any],
        chat_item: Dict[str, Any],
        chat_info: Dict[str, Any],
        gid_str: str,
        sender: str,
        message_id: str,
        text: str,
        message_type: MessageType,
        media_mime: Optional[str],
    ) -> None:
        deadline = asyncio.get_event_loop().time() + self._file_wait_s
        try:
            gid = int(gid_str)
        except ValueError:
            return
        host_path: Optional[Path] = None
        while asyncio.get_event_loop().time() < deadline:
            try:
                await asyncio.sleep(_FILE_POLL_INTERVAL_S)
            except asyncio.CancelledError:
                return
            if self._client is None:
                continue
            try:
                # Fetch a small window around the original item so we can
                # spot the same fileId with its updated status.
                items = await self._client.api_get_chat(group_id=gid, count=10)
            except Exception as e:
                logger.debug("simplex: pending-media poll fetch failed: %r", e)
                continue
            updated = _find_chat_item_with_file(items, file_id)
            if updated is None:
                continue
            new_status = ((updated.get("file") or {}).get("fileStatus") or {}).get("type")
            if new_status in {"rcvComplete", "sndStored", "sndComplete"}:
                fname = (updated.get("file") or {}).get("fileName")
                if isinstance(fname, str):
                    candidate = self._file_dir / fname
                    if candidate.exists():
                        host_path = candidate
                break
        if host_path is None:
            logger.warning(
                "simplex: file %d in group %s did not complete within %.0fs — "
                "delivering text placeholder",
                file_id, gid_str, self._file_wait_s,
            )
            fname = ((chat_item.get("file") or {}).get("fileName")) or "(file)"
            await self._build_and_dispatch(
                item=item,
                chat_info=chat_info,
                gid_str=gid_str,
                sender=sender,
                message_id=message_id,
                text=(
                    f"[simplex {message_type.value} — {fname} not delivered]"
                    + (f" {text}" if text else "")
                ),
                message_type=MessageType.TEXT,
                media_urls=[],
                media_types=[],
            )
            return
        await self._build_and_dispatch(
            item=item,
            chat_info=chat_info,
            gid_str=gid_str,
            sender=sender,
            message_id=message_id,
            text=text,
            message_type=message_type,
            media_urls=[str(host_path)],
            media_types=[media_mime or "application/octet-stream"],
        )

    # ── Replay ──────────────────────────────────────────────────────────

    def _note_seen(self, item_id: int) -> None:
        if len(self._seen_item_ids) == self._seen_item_ids.maxlen:
            evicted = self._seen_item_ids[0]
            self._seen_item_set.discard(evicted)
        self._seen_item_ids.append(item_id)
        self._seen_item_set.add(item_id)

    def _advance_cursor(self, gid_str: str, item_id: int) -> None:
        prev = self._cursors.get(gid_str)
        if prev is not None and prev >= item_id:
            return
        self._cursors[gid_str] = item_id
        self._save_cursors()

    def _load_cursors(self) -> Dict[str, int]:
        try:
            raw = self._cursor_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as e:
            logger.warning("simplex: failed to read cursor file %s: %r", self._cursor_path, e)
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("simplex: cursor file %s is corrupt; ignoring", self._cursor_path)
            return {}
        groups = data.get("groups") if isinstance(data, dict) else None
        if not isinstance(groups, dict):
            return {}
        out: Dict[str, int] = {}
        for k, v in groups.items():
            if isinstance(v, int):
                out[str(k)] = v
        return out

    def _save_cursors(self) -> None:
        try:
            self._cursor_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cursor_path.with_suffix(self._cursor_path.suffix + ".tmp")
            payload = json.dumps({"version": 1, "groups": self._cursors}, sort_keys=True)
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self._cursor_path)
        except OSError as e:
            logger.warning("simplex: failed to write cursor file %s: %r", self._cursor_path, e)

    async def _replay_missed_messages(self) -> None:
        """Walk /_get chat forward from each group's cursor and dispatch."""
        if self._replay_disabled or self._client is None:
            return
        for gid_str in sorted(self._allowed_group_ids):
            try:
                gid = int(gid_str)
            except ValueError:
                continue
            after_id = self._cursors.get(gid_str)
            if after_id is None:
                # First connect for this group: seed the cursor at the most
                # recent item without dispatching anything, so we don't
                # replay the entire history on initial install.
                await self._seed_cursor(gid, gid_str)
                continue
            await self._replay_group(gid, gid_str, after_id)

    async def _seed_cursor(self, gid: int, gid_str: str) -> None:
        if self._client is None:
            return
        try:
            items = await self._client.api_get_chat(group_id=gid, count=1)
        except Exception as e:
            logger.warning("simplex: cursor seed failed for group %s: %r", gid_str, e)
            return
        latest = _max_item_id(items)
        if latest is not None:
            self._cursors[gid_str] = latest
            self._save_cursors()

    async def _replay_group(self, gid: int, gid_str: str, after_id: int) -> None:
        assert self._client is not None
        replayed = 0
        cursor = after_id
        remaining = self._replay_max
        while remaining > 0:
            page_size = min(_REPLAY_PAGE_SIZE, remaining)
            try:
                items = await self._client.api_get_chat(
                    group_id=gid, count=page_size, after_id=cursor
                )
            except Exception as e:
                logger.warning("simplex: replay fetch failed for group %s: %r", gid_str, e)
                return
            if not items:
                break
            chat_info = {
                "type": "group",
                "groupInfo": {
                    "groupId": gid,
                    "displayName": self._known_groups.get(gid, gid_str),
                },
            }
            for chat_item in items:
                await self._dispatch_chat_item({"chatInfo": chat_info, "chatItem": chat_item})
                replayed += 1
            new_cursor = _max_item_id(items)
            if new_cursor is None or new_cursor <= cursor:
                break
            cursor = new_cursor
            remaining -= len(items)
            if len(items) < page_size:
                break
        if replayed:
            logger.info(
                "simplex: replayed %d missed message(s) in group %s%s",
                replayed,
                gid_str,
                " (capped)" if replayed >= self._replay_max else "",
            )
