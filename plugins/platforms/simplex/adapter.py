"""SimpleX Chat platform adapter (Hermes plugin).

Connects to a simplex-chat daemon running in WebSocket mode.
Inbound messages arrive via a persistent WebSocket connection.
Outbound messages use the same WebSocket with JSON commands.

This adapter ships as a Hermes platform plugin under
``plugins/platforms/simplex/``. The Hermes plugin loader scans the
directory at startup, calls ``register(ctx)``, and the platform
becomes available to ``gateway/run.py`` and ``tools/send_message_tool``
through the registry — no edits to core files are required.

SimpleX chat daemon setup:
    simplex-chat -p 5225          # start daemon on port 5225
    # or via Docker:
    # docker run -p 5225:5225 simplexchat/simplex-chat-cli -p 5225

Required environment variables:
    SIMPLEX_WS_URL             WebSocket URL of the daemon
                               (default: ws://127.0.0.1:5225)

Optional environment variables:
    SIMPLEX_ALLOWED_USERS      Comma-separated contact IDs (allowlist)
    SIMPLEX_ALLOW_ALL_USERS    Set 'true' to allow all contacts
    SIMPLEX_HOME_CHANNEL       Default contact/group ID for cron delivery
    SIMPLEX_HOME_CHANNEL_NAME  Human label for the home channel
    SIMPLEX_REPLAY_DISABLED    Set 'true' to disable missed-message replay
                               on reconnect (default: enabled).
    SIMPLEX_REPLAY_MAX_ITEMS   Cap on items replayed per group per
                               reconnect (default: 200).
    SIMPLEX_REPLAY_PAGE_SIZE   Pagination size for /_get chat (default: 50).
    SIMPLEX_FILE_DIR           Host directory mirrored to the daemon's
                               files folder via bind-mount. Used for
                               both inbound media (translate
                               daemon-reported paths to host paths) and
                               outbound media (stage files so the
                               daemon can read them). Unset = inbound
                               falls back to ~/Downloads /
                               ~/.simplex/files / /tmp/simplex_files
                               search; outbound degrades to text-only.
    SIMPLEX_DAEMON_FILES_FOLDER The container-side path the daemon
                               reports for files in chat events.
                               Defaults to /root/.simplex/files. Used
                               together with SIMPLEX_FILE_DIR for
                               prefix-replacement path translation in
                               both directions.

The ``websockets`` Python package is imported lazily — the plugin is
discoverable and `hermes setup` can describe it even when websockets is
not installed. ``check_requirements()`` returns False until the package
is present, so the gateway will not attempt to instantiate the adapter.

Outbound media uses Pillow + ffmpeg/ffprobe opportunistically — when
present, outbound images get inline thumbnails, voice/video get
duration metadata, and videos get a poster frame. All are best-effort:
missing tools just mean a less-rich preview on the recipient's phone,
not a failed send.
"""

import asyncio
import base64
import io
import json
import logging
import os
import random
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Lazy import: BasePlatformAdapter and friends live in the main repo.
# Imported at module top because they're stdlib-only inside Hermes — no
# external dependency that would block the plugin from loading.
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_bytes,
    cache_audio_from_bytes,
    cache_document_from_bytes,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_MESSAGE_LENGTH = 16_000  # SimpleX has no hard limit; keep chunking sane
TYPING_INTERVAL = 10.0
WS_RETRY_DELAY_INITIAL = 2.0
WS_RETRY_DELAY_MAX = 60.0
HEALTH_CHECK_INTERVAL = 30.0
HEALTH_CHECK_STALE_THRESHOLD = 120.0

# Correlation ID prefix for requests we send so we can ignore our own echoes.
_CORR_PREFIX = "hermes-"

# Replay defaults — overridable via SIMPLEX_REPLAY_* env vars.
_REPLAY_DEFAULT_MAX_ITEMS = 200
_REPLAY_DEFAULT_PAGE_SIZE = 50
_REPLAY_RESPONSE_TIMEOUT_S = 15.0

# Outbound media defaults — see _make_image_thumbnail, _probe_duration_*
_THUMBNAIL_MAX_PX = 224
_OUTBOUND_FETCH_TIMEOUT_S = 30.0
_FFPROBE_TIMEOUT_S = 10.0
_FFMPEG_TIMEOUT_S = 15.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_comma_list(value: str) -> List[str]:
    """Split a comma-separated string into a stripped list."""
    return [v.strip() for v in value.split(",") if v.strip()]


def _guess_extension(data: bytes) -> str:
    """Guess file extension from magic bytes."""
    if data[:4] == b"\x89PNG":
        return ".png"
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[:4] == b"GIF8":
        return ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:4] == b"%PDF":
        return ".pdf"
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return ".mp4"
    if data[:4] == b"OggS":
        return ".ogg"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return ".mp3"
    return ".bin"


def _is_image_ext(ext: str) -> bool:
    return ext.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _is_audio_ext(ext: str) -> bool:
    return ext.lower() in {".mp3", ".wav", ".ogg", ".m4a", ".aac"}


def _translate_daemon_path(
    daemon_path: str,
    *,
    host_root: Optional[str],
    daemon_root: Optional[str],
) -> Optional[Path]:
    """Map a daemon-reported file path to the corresponding host path.

    Returns None if translation isn't possible (either env var unset, or
    the daemon path doesn't sit under daemon_root). The caller falls
    back to the legacy search-known-dirs flow.
    """
    if not daemon_path or not host_root or not daemon_root:
        return None
    # Normalise trailing separators so the prefix compare is exact.
    d_root = daemon_root.rstrip("/\\")
    if not (daemon_path == d_root or daemon_path.startswith(d_root + "/")):
        return None
    suffix = daemon_path[len(d_root):].lstrip("/")
    host = Path(host_root).expanduser() / suffix if suffix else Path(host_root).expanduser()
    return host


def _cache_by_ext(data: bytes, file_name: str) -> str:
    """Route bytes to the right cache helper based on magic-byte sniffing."""
    ext = _guess_extension(data)
    if _is_image_ext(ext):
        return cache_image_from_bytes(data, ext)
    if _is_audio_ext(ext):
        return cache_audio_from_bytes(data, ext)
    return cache_document_from_bytes(data, file_name)


def _is_video_ext(ext: str) -> bool:
    return ext.lower() in {".mp4", ".mov", ".webm", ".mkv"}


# ---------------------------------------------------------------------------
# Outbound-media helpers (best-effort previews)
# ---------------------------------------------------------------------------
#
# These produce richer previews on the recipient's phone client but are
# all optional — Pillow / ffprobe / ffmpeg may not be installed. Missing
# tools just degrade the preview, never break the send.


def _make_image_thumbnail(path: Path) -> Optional[str]:
    """Return a base64 data-URL JPEG thumbnail, or None if Pillow is
    unavailable or the source can't be decoded."""
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
            capture_output=True, text=True,
            timeout=_FFPROBE_TIMEOUT_S, check=True,
        )
    except Exception as e:
        logger.debug("simplex: ffprobe failed for %s: %r", path, e)
        return None
    try:
        return max(0, int(round(float(proc.stdout.strip()))))
    except ValueError:
        return None


def _extract_video_poster(path: Path) -> Optional[str]:
    """Return a base64 data-URL JPEG poster frame via ffmpeg, or None."""
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
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT_S, check=True,
        )
    except Exception as e:
        logger.debug("simplex: ffmpeg poster extraction failed for %s: %r", path, e)
        return None
    if not proc.stdout:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(proc.stdout).decode("ascii")


def _build_outbound_msg_content(
    kind: str, host_path: Path, caption: Optional[str]
) -> Dict[str, Any]:
    """Construct the msgContent dict for an outbound /_send command.

    ``kind`` is one of: image, voice, video, file. Synchronous and safe
    to call from a thread (does PIL / ffprobe / ffmpeg subprocess work).
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
        duration = _probe_duration_seconds(host_path)
        return {"type": "voice", "text": text, "duration": duration if duration is not None else 0}
    if kind == "video":
        out = {"type": "video", "text": text}
        duration = _probe_duration_seconds(host_path)
        out["duration"] = duration if duration is not None else 0
        poster = _extract_video_poster(host_path)
        if poster:
            out["image"] = poster
        return out
    raise ValueError(f"unknown media kind: {kind}")


def _resolve_url_to_local(url_or_path: str) -> Optional[Path]:
    """For local-only URLs (file://) or bare paths, return the Path.

    Returns None for http(s) URLs (the caller must download those).
    """
    if not url_or_path:
        return None
    if url_or_path.startswith("file://"):
        return Path(urllib.parse.unquote(url_or_path[7:]))
    if "://" not in url_or_path:
        return Path(url_or_path)
    return None


def _fetch_remote_to(
    temp_path: Path, url: str, *, timeout: float = _OUTBOUND_FETCH_TIMEOUT_S
) -> None:
    """Download a URL to a local path (synchronous, blocking)."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        with open(temp_path, "wb") as f:
            shutil.copyfileobj(response, f)


# ---------------------------------------------------------------------------
# SimpleX Adapter
# ---------------------------------------------------------------------------

class SimplexAdapter(BasePlatformAdapter):
    """SimpleX Chat adapter using the simplex-chat daemon WebSocket API.

    Instantiated by the ``adapter_factory`` passed to
    ``ctx.register_platform()`` in :func:`register`.
    """

    def __init__(self, config: PlatformConfig, **kwargs):
        platform = Platform("simplex")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}
        self.ws_url = extra.get("ws_url", "ws://127.0.0.1:5225").rstrip("/")

        # Running state
        self._ws = None  # websockets connection
        self._ws_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        self._typing_tasks: Dict[str, asyncio.Task] = {}
        self._running = False
        self._last_ws_activity = 0.0

        # Track sent correlation IDs to filter echoes
        self._pending_corr_ids: set = set()
        self._max_pending_corr = 200

        # corrId → Future for requests where we actually want the response
        # (replay's /groups and /_get chat). Echoes still go through
        # _pending_corr_ids; only callers of _send_and_wait insert here.
        self._pending_responses: Dict[str, asyncio.Future] = {}

        # Missed-message replay state — initialised lazily on first connect
        # so importing the module without HERMES_HOME doesn't touch disk.
        self._replay_disabled = (
            os.getenv("SIMPLEX_REPLAY_DISABLED", "").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        try:
            self._replay_max_items = max(
                0, int(os.getenv("SIMPLEX_REPLAY_MAX_ITEMS", str(_REPLAY_DEFAULT_MAX_ITEMS)))
            )
        except ValueError:
            self._replay_max_items = _REPLAY_DEFAULT_MAX_ITEMS
        try:
            self._replay_page_size = max(
                1, int(os.getenv("SIMPLEX_REPLAY_PAGE_SIZE", str(_REPLAY_DEFAULT_PAGE_SIZE)))
            )
        except ValueError:
            self._replay_page_size = _REPLAY_DEFAULT_PAGE_SIZE
        self._replay_state = None  # set in connect()

        # Bind-mount mapping for containerised daemons. When set, file
        # paths the daemon emits (rooted at daemon_files_folder) are
        # translated to the host-visible directory at host_files_dir
        # — used for both inbound (translate daemon path → host read) and
        # outbound (stage file at host path, tell daemon the container
        # equivalent). Unset → inbound falls back to legacy directory
        # search; outbound degrades to text-only.
        self._host_files_dir: Optional[str] = (
            os.getenv("SIMPLEX_FILE_DIR", "").strip() or None
        )
        self._daemon_files_folder: str = (
            os.getenv("SIMPLEX_DAEMON_FILES_FOLDER", "").strip()
            or "/root/.simplex/files"
        )
        # Serialise outbound /_send so concurrent send_image / send calls
        # from cron jobs + live messages don't interleave on the WS.
        self._send_lock = asyncio.Lock()

        logger.info("SimpleX adapter initialized: url=%s", self.ws_url)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Connect to the simplex-chat daemon and start the WebSocket listener."""
        try:
            import websockets  # noqa: F401
        except ImportError:
            logger.error(
                "SimpleX: 'websockets' package not installed. "
                "Run: pip install websockets"
            )
            return False

        if not self.ws_url:
            logger.error("SimpleX: SIMPLEX_WS_URL is required")
            return False

        # Quick connectivity check — try to open and immediately close
        try:
            import websockets as _wsclient
            async with _wsclient.connect(self.ws_url, open_timeout=10):
                pass
        except Exception as e:
            logger.error("SimpleX: cannot reach daemon at %s: %s", self.ws_url, e)
            return False

        self._running = True
        self._last_ws_activity = time.time()
        self._init_replay_state()
        self._ws_task = asyncio.create_task(self._ws_listener())
        self._health_task = asyncio.create_task(self._health_monitor())

        logger.info("SimpleX: connected to %s", self.ws_url)
        return True

    def _init_replay_state(self) -> None:
        """Set up _replay_state lazily — needs HERMES_HOME, may not be safe
        to call from __init__ during plugin discovery."""
        if self._replay_disabled or self._replay_state is not None:
            return
        try:
            from hermes_constants import get_hermes_home
            cursor_path = get_hermes_home() / "simplex" / "cursors.json"
        except Exception as e:
            logger.warning(
                "SimpleX: replay disabled — could not resolve HERMES_HOME: %s", e
            )
            self._replay_disabled = True
            return
        from ._replay import ReplayState
        state = ReplayState(cursor_path)
        try:
            state.load()
        except Exception:
            logger.exception("SimpleX: replay cursor load failed; continuing fresh")
        self._replay_state = state

    async def disconnect(self) -> None:
        """Stop WebSocket listener and clean up."""
        self._running = False

        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        for task in self._typing_tasks.values():
            task.cancel()
        self._typing_tasks.clear()

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        # Fail any awaiters that were waiting on a response so they don't
        # hang past disconnect.
        for fut in list(self._pending_responses.values()):
            if not fut.done():
                fut.cancel()
        self._pending_responses.clear()

        logger.info("SimpleX: disconnected")

    # ------------------------------------------------------------------
    # WebSocket listener
    # ------------------------------------------------------------------

    async def _ws_listener(self) -> None:
        """Maintain a persistent WebSocket connection to the daemon."""
        import websockets as _wsclient
        import websockets as _wsexc

        backoff = WS_RETRY_DELAY_INITIAL

        while self._running:
            try:
                logger.debug("SimpleX WS: connecting to %s", self.ws_url)
                async with _wsclient.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    self._ws = ws
                    backoff = WS_RETRY_DELAY_INITIAL
                    self._last_ws_activity = time.time()
                    logger.info("SimpleX WS: connected")

                    # Replay missed messages before processing live events.
                    # Failures are logged and skipped — replay is best-effort.
                    if self._replay_state is not None:
                        try:
                            await self._replay_missed_items()
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.exception(
                                "SimpleX: replay failed; continuing with live stream"
                            )

                    async for raw in ws:
                        if not self._running:
                            break
                        self._last_ws_activity = time.time()
                        try:
                            msg = json.loads(raw)
                            await self._handle_event(msg)
                        except json.JSONDecodeError:
                            logger.debug("SimpleX WS: invalid JSON: %.100s", raw)
                        except Exception:
                            logger.exception("SimpleX WS: error handling event")

            except asyncio.CancelledError:
                break
            except _wsexc.WebSocketException as e:
                if self._running:
                    logger.warning(
                        "SimpleX WS: error: %s (reconnecting in %.0fs)", e, backoff
                    )
            except Exception as e:
                if self._running:
                    logger.warning(
                        "SimpleX WS: unexpected error: %s (reconnecting in %.0fs)",
                        e, backoff,
                    )
            finally:
                self._ws = None

            if self._running:
                jitter = backoff * 0.2 * random.random()
                await asyncio.sleep(backoff + jitter)
                backoff = min(backoff * 2, WS_RETRY_DELAY_MAX)

    # ------------------------------------------------------------------
    # Health monitor
    # ------------------------------------------------------------------

    async def _health_monitor(self) -> None:
        """Force reconnect if the WebSocket has been idle too long."""
        while self._running:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            if not self._running:
                break

            elapsed = time.time() - self._last_ws_activity
            if elapsed > HEALTH_CHECK_STALE_THRESHOLD:
                logger.warning(
                    "SimpleX: WS idle for %.0fs, forcing reconnect", elapsed
                )
                self._last_ws_activity = time.time()
                if self._ws:
                    try:
                        await self._ws.close()
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # Inbound event handling
    # ------------------------------------------------------------------

    async def _handle_event(self, event: dict) -> None:
        """Dispatch a daemon event to the appropriate handler."""
        # The daemon wraps events in {"resp": {"type": ..., ...}}; the
        # top-level "type" / "chatItems" fields are empty. Unwrap once so
        # the rest of the dispatch can read either layout uniformly.
        inner = event.get("resp") if isinstance(event.get("resp"), dict) else event
        resp_type = inner.get("type") or event.get("type") or ""

        # Responses to our own commands carry a hermes- corrId. Resolve any
        # pending Future for /_send_and_wait callers; otherwise drop as echo.
        corr_id = event.get("corrId", "")
        if corr_id and corr_id.startswith(_CORR_PREFIX):
            self._pending_corr_ids.discard(corr_id)
            fut = self._pending_responses.pop(corr_id, None)
            if fut is not None and not fut.done():
                fut.set_result(event)
            return

        if resp_type == "newChatItem":
            await self._handle_new_chat_item(inner)
        elif resp_type == "newChatItems":
            # Batch variant — process each item. Items may sit at either
            # the top of the event or inside the resp wrapper.
            items = inner.get("chatItems") or event.get("chatItems") or []
            for item_wrapper in items:
                await self._handle_new_chat_item(item_wrapper)
        # Ignore all other event types (delivery receipts, contact updates, etc.)

    async def _handle_new_chat_item(self, wrapper: dict) -> None:
        """Process a single newChatItem event into a MessageEvent."""
        # The daemon wraps the chat item differently depending on version;
        # normalise both layouts.
        chat_info = wrapper.get("chatInfo") or wrapper.get("chat") or {}
        chat_item = wrapper.get("chatItem") or wrapper.get("item") or {}
        logger.warning(
            "SimpleX TRACE NCI: wrapper_keys=%s chat_info_type=%r chat_info_keys=%s "
            "chat_item_keys=%s msg_content_type=%r direction=%r",
            sorted(wrapper.keys()),
            chat_info.get("type"),
            sorted(chat_info.keys()),
            sorted(chat_item.keys()),
            (chat_item.get("content") or {}).get("msgContent", {}).get("type"),
            ((chat_item.get("meta") or {}).get("itemStatus") or {}).get("type"),
        )

        # Only process messages (not calls, deleted items, etc.)
        item_content = chat_item.get("content") or {}
        msg_content = item_content.get("msgContent") or {}
        if not msg_content:
            return

        # Filter out messages sent by us (direction == "snd")
        meta = chat_item.get("meta") or {}
        direction = (meta.get("itemStatus") or {}).get("type", "")
        if direction in {"sndSent", "sndSentDirect", "sndSentViaProxy", "sndNew"}:
            return

        # Determine chat type and IDs
        chat_type_raw = chat_info.get("type", "")
        is_group = chat_type_raw in {"group", "groupInfo"}

        if is_group:
            group_info = chat_info.get("groupInfo") or chat_info.get("group") or {}
            group_id = str(group_info.get("groupId") or group_info.get("id") or "")
            group_name = group_info.get("displayName") or group_info.get("groupProfile", {}).get("displayName", "")
            chat_id = f"group:{group_id}" if group_id else ""
            chat_name = group_name
        else:
            contact_info = chat_info.get("contact") or {}
            contact_id = str(contact_info.get("contactId") or contact_info.get("id") or "")
            contact_name = (
                contact_info.get("displayName")
                or contact_info.get("localDisplayName")
                or contact_id
            )
            chat_id = contact_id
            chat_name = contact_name

        if not chat_id:
            logger.debug("SimpleX: ignoring event with no chat_id")
            return

        # Sender — for groups the message includes a chatItemMember sub-object
        member = chat_item.get("chatItemMember") or {}
        if is_group and member:
            sender_id = str(member.get("memberId") or member.get("id") or chat_id)
            sender_name = (
                member.get("displayName")
                or member.get("localDisplayName")
                or sender_id
            )
        else:
            sender_id = chat_id
            sender_name = chat_name

        # Extract text
        text = msg_content.get("text") or ""

        # Media attachments
        media_urls: List[str] = []
        media_types: List[str] = []
        file_info = chat_item.get("file") or {}
        if file_info and file_info.get("fileStatus") not in {"cancelled", "error"}:
            file_id = file_info.get("fileId")
            file_name = file_info.get("fileName", "file")
            # The daemon may already have the file on disk under
            # fileSource.filePath (container-side path). Pass it through
            # so _fetch_file can translate it via the bind-mount mapping.
            file_source = file_info.get("fileSource") or {}
            daemon_path = (
                file_source.get("filePath")
                or file_info.get("filePath")
                or ""
            )
            if file_id:
                try:
                    cached = await self._fetch_file(
                        file_id, file_name, daemon_path=daemon_path
                    )
                    if cached:
                        ext = cached.rsplit(".", 1)[-1]
                        if _is_image_ext("." + ext):
                            media_types.append("image/" + ext.replace("jpg", "jpeg"))
                        elif _is_audio_ext("." + ext):
                            media_types.append("audio/" + ext)
                        else:
                            media_types.append("application/octet-stream")
                        media_urls.append(cached)
                except Exception:
                    logger.exception("SimpleX: failed to fetch file %s", file_id)

        # Timestamp
        ts_str = meta.get("itemTs") or meta.get("createdAt") or ""
        try:
            timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            timestamp = datetime.now(tz=timezone.utc)

        # Build source
        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type="group" if is_group else "dm",
            user_id=sender_id,
            user_name=sender_name,
        )

        # Message type
        msg_type = MessageType.TEXT
        if media_types:
            if any(mt.startswith("audio/") for mt in media_types):
                msg_type = MessageType.VOICE
            elif any(mt.startswith("image/") for mt in media_types):
                msg_type = MessageType.PHOTO

        event_obj = MessageEvent(
            source=source,
            text=text,
            message_type=msg_type,
            media_urls=media_urls,
            media_types=media_types,
            timestamp=timestamp,
            raw_message=wrapper,
        )

        # Replay dedupe: groups only (item ids aren't unique across chats,
        # and DM replay isn't implemented yet). Skip if we've already seen
        # this (group_id, item_id) tuple in the dedupe ring, otherwise mark
        # and dispatch. The cursor advances after a successful dispatch so
        # a crash mid-handler doesn't skip the message on next start.
        if self._replay_state is not None and is_group:
            try:
                gid_int = int(group_id)
                item_id_raw = meta.get("itemId")
                item_id_int = int(item_id_raw) if item_id_raw is not None else None
            except (TypeError, ValueError):
                gid_int = None
                item_id_int = None
            if gid_int is not None and item_id_int is not None:
                if self._replay_state.already_dispatched(gid_int, item_id_int):
                    return
                self._replay_state.mark_dispatched(gid_int, item_id_int)
                await self.handle_message(event_obj)
                self._replay_state.update_cursor(gid_int, item_id_int)
                return

        await self.handle_message(event_obj)

    async def _fetch_file(
        self,
        file_id: Any,
        file_name: str,
        *,
        daemon_path: str = "",
    ) -> Optional[str]:
        """Ask the daemon to receive and return a file attachment.

        ``daemon_path`` is the path the daemon reports for the file in its
        chat event, e.g. ``/root/.simplex/files/IMG_001.jpg`` when the
        daemon runs in a container. When SIMPLEX_FILE_DIR is set we
        translate the container prefix to the host bind-mount and read
        the file directly; otherwise we fall back to the legacy
        search-known-dirs flow.
        """
        # simplex-chat exposes `/api/v1/files/{fileId}` on an HTTP port
        # when started with --http-port. However, the canonical WebSocket API
        # does not have a direct binary download command; files are stored on
        # the local filesystem after the daemon accepts them.
        #
        # We request acceptance first, then read from the daemon's local path.
        corr_id = self._make_corr_id()
        cmd = {
            "corrId": corr_id,
            "cmd": f"/freceive {file_id}",
        }
        await self._send_ws(cmd)
        # The daemon will emit a chatItemUpdated event when the file lands;
        # for simplicity we just wait briefly and rely on the daemon's default path.
        await asyncio.sleep(2)

        # Fast path: when the daemon told us the file path and we know the
        # bind-mount layout, read directly from the host-side directory.
        if self._host_files_dir:
            host_candidate = _translate_daemon_path(
                daemon_path,
                host_root=self._host_files_dir,
                daemon_root=self._daemon_files_folder,
            )
            # Fallback within bind-mount: if path translation failed but a
            # filename came through, try <host_dir>/<file_name>.
            if host_candidate is None and file_name:
                host_candidate = Path(self._host_files_dir).expanduser() / file_name
            if host_candidate is not None and host_candidate.exists():
                try:
                    data = host_candidate.read_bytes()
                except OSError as e:
                    logger.warning(
                        "SimpleX: cannot read bind-mounted file %s: %s",
                        host_candidate, e,
                    )
                else:
                    return _cache_by_ext(data, file_name)

        # Legacy search — for non-containerised daemons.
        for search_dir in (
            os.path.expanduser("~/Downloads"),
            os.path.expanduser("~/.simplex/files"),
            "/tmp/simplex_files",
        ):
            candidate = os.path.join(search_dir, file_name)
            if os.path.exists(candidate):
                with open(candidate, "rb") as f:
                    data = f.read()
                return _cache_by_ext(data, file_name)
        return None

    # ------------------------------------------------------------------
    # Outbound messages
    # ------------------------------------------------------------------

    def _make_corr_id(self) -> str:
        """Generate a unique correlation ID for a request."""
        corr_id = f"{_CORR_PREFIX}{int(time.time() * 1000)}-{random.randint(0, 9999)}"
        self._pending_corr_ids.add(corr_id)
        if len(self._pending_corr_ids) > self._max_pending_corr:
            # Trim oldest — sets are unordered so just clear the oldest half
            to_remove = list(self._pending_corr_ids)[:self._max_pending_corr // 2]
            self._pending_corr_ids -= set(to_remove)
        return corr_id

    async def _send_ws(self, payload: dict) -> None:
        """Send a JSON payload over the WebSocket, queuing if not yet connected."""
        import websockets as _wsexc
        ws = self._ws
        if not ws:
            logger.debug("SimpleX: WS not connected, dropping outbound command")
            return
        try:
            await ws.send(json.dumps(payload))
        except _wsexc.ConnectionClosed:
            logger.warning("SimpleX: WS closed while sending")
        except Exception as e:
            logger.warning("SimpleX: WS send error: %s", e)

    async def _send_and_wait(
        self, cmd_str: str, *, timeout: float = _REPLAY_RESPONSE_TIMEOUT_S
    ) -> Optional[dict]:
        """Send a chat command and await the response with the matching corrId.

        Returns the full response dict (with corrId, resp, etc.) or None on
        timeout / WS-closed. Used by the replay loop; not exposed for normal
        message-sending which stays fire-and-forget.
        """
        if not self._ws:
            return None
        corr_id = self._make_corr_id()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_responses[corr_id] = fut
        try:
            await self._send_ws({"corrId": corr_id, "cmd": cmd_str})
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_responses.pop(corr_id, None)
            logger.debug("SimpleX: response timeout for %r", cmd_str)
            return None
        except Exception as e:
            self._pending_responses.pop(corr_id, None)
            logger.debug("SimpleX: _send_and_wait failed for %r: %s", cmd_str, e)
            return None

    # ------------------------------------------------------------------
    # Missed-message replay
    # ------------------------------------------------------------------

    async def _replay_missed_items(self) -> None:
        """Replay missed messages for each group with a stored cursor.

        Idempotent across reconnects — dispatched items go through the
        same dedupe ring as live events. Replay is per-group only; DM
        replay would need contact-id cursors which this PR doesn't add.
        """
        state = self._replay_state
        if state is None:
            return
        cursors = state.known_groups()
        if not cursors:
            logger.debug("SimpleX: no replay cursors, skipping replay")
            return
        logger.info("SimpleX: replaying missed items for %d group(s)", len(cursors))
        for group_id in cursors:
            cursor = state.get_cursor(group_id)
            if cursor is None:
                continue
            try:
                await self._replay_group(group_id, cursor)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("SimpleX: replay failed for group %s", group_id)

    async def _replay_group(self, group_id: int, after_id: int) -> None:
        """Fetch and dispatch chatItems for one group after the cursor."""
        page_size = self._replay_page_size
        max_items = self._replay_max_items
        dispatched = 0
        next_after = after_id
        while dispatched < max_items and self._running:
            count = min(page_size, max_items - dispatched)
            cmd = f"/_get chat #{group_id} after={next_after} count={count}"
            resp = await self._send_and_wait(cmd)
            if resp is None:
                return
            inner = resp.get("resp") or {}
            if inner.get("type") != "apiChat":
                logger.debug(
                    "SimpleX: replay group %s expected apiChat, got %r",
                    group_id, inner.get("type"),
                )
                return
            chat = inner.get("chat") or {}
            chat_info = chat.get("chatInfo") or {}
            items = chat.get("chatItems") or []
            if not items:
                return
            for chat_item in items:
                meta = chat_item.get("meta") or {}
                item_id = meta.get("itemId")
                if isinstance(item_id, int) and item_id > next_after:
                    next_after = item_id
                # Wrap into the shape _handle_new_chat_item expects (chatInfo
                # alongside the bare chatItem from the /_get response).
                wrapper = {"chatInfo": chat_info, "chatItem": chat_item}
                await self._handle_new_chat_item(wrapper)
                dispatched += 1
                if dispatched >= max_items:
                    break
            if len(items) < count:
                # Caught up — daemon returned fewer than requested.
                return
        if dispatched:
            logger.info(
                "SimpleX: replayed %d item(s) for group %s (cursor now %s)",
                dispatched, group_id, next_after,
            )

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text message to a contact or group."""
        corr_id = self._make_corr_id()

        if chat_id.startswith("group:"):
            group_id = chat_id[6:]
            cmd_str = f"#[{group_id}] {content}"
        else:
            cmd_str = f"@[{chat_id}] {content}"

        payload = {
            "corrId": corr_id,
            "cmd": cmd_str,
        }

        await self._send_ws(payload)
        return SendResult(success=True)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """SimpleX does not expose a typing indicator API — no-op."""
        pass

    # ------------------------------------------------------------------
    # Outbound media — daemon /_send with fileSource
    # ------------------------------------------------------------------
    #
    # SimpleX has no native "upload bytes over WS" — outbound files must
    # already exist on the daemon's filesystem. When SIMPLEX_FILE_DIR is
    # set, we stage the file into the host-side bind-mount and tell the
    # daemon the equivalent container path. When unset, all of these
    # degrade to text-only (URL pasted in the body).

    def _container_path_for(self, file_name: str) -> str:
        """Return the container-visible path for a file already staged
        under SIMPLEX_FILE_DIR. Mirror of _translate_daemon_path."""
        d_root = self._daemon_files_folder.rstrip("/\\")
        return f"{d_root}/{file_name}" if file_name else d_root

    async def _stage_for_send(self, src_url_or_path: str) -> Optional[Tuple[Path, str]]:
        """Materialise the source into SIMPLEX_FILE_DIR and return
        (host_path, file_name). Returns None when bind-mount isn't
        configured or the source can't be obtained.

        - file:// or bare path inside SIMPLEX_FILE_DIR → no copy, used as-is.
        - file:// or bare path outside  → copied with a stable basename.
        - http(s):// → downloaded into SIMPLEX_FILE_DIR.
        """
        if not self._host_files_dir:
            return None
        host_root = Path(self._host_files_dir).expanduser()
        try:
            host_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(
                "SimpleX: cannot create staging dir %s: %s", host_root, e
            )
            return None

        local = _resolve_url_to_local(src_url_or_path)
        if local is not None:
            try:
                local_resolved = local.resolve()
                host_resolved = host_root.resolve()
            except OSError:
                local_resolved, host_resolved = local, host_root
            if str(local_resolved).startswith(str(host_resolved) + os.sep) or local_resolved == host_resolved:
                # Already in the bind-mount; no copy needed.
                return local, local.name
            # Copy into the staging dir under the original basename, with a
            # uuid prefix to avoid collisions across simultaneous sends.
            staged_name = f"{uuid.uuid4().hex[:8]}-{local.name}"
            staged = host_root / staged_name
            try:
                shutil.copyfile(local, staged)
            except OSError as e:
                logger.warning("SimpleX: cannot stage %s into %s: %s", local, staged, e)
                return None
            return staged, staged_name

        # Remote URL — download. Use the URL's basename if it looks like a
        # filename, otherwise fall back to a uuid + guessed extension.
        parsed = urllib.parse.urlparse(src_url_or_path)
        url_name = Path(parsed.path).name or ""
        staged_name = f"{uuid.uuid4().hex[:8]}-{url_name}" if url_name else f"{uuid.uuid4().hex}.bin"
        staged = host_root / staged_name
        try:
            await asyncio.to_thread(_fetch_remote_to, staged, src_url_or_path)
        except Exception as e:
            logger.warning("SimpleX: cannot download %s: %s", src_url_or_path, e)
            return None
        return staged, staged_name

    async def _send_media(
        self,
        chat_id: str,
        kind: str,
        source: str,
        caption: Optional[str],
    ) -> SendResult:
        """Stage a media file and dispatch a /_send command with fileSource.

        Falls back to a text message when SIMPLEX_FILE_DIR isn't configured.
        """
        if not self._host_files_dir:
            fallback = f"{caption}\n{source}".strip() if caption else source
            return await self.send(chat_id, fallback)

        staged = await self._stage_for_send(source)
        if staged is None:
            fallback = f"{caption}\n{source}".strip() if caption else source
            return await self.send(chat_id, fallback)
        host_path, basename = staged
        container_path = self._container_path_for(basename)

        try:
            msg_content = await asyncio.to_thread(
                _build_outbound_msg_content, kind, host_path, caption
            )
        except ValueError as e:
            logger.warning("SimpleX: %s", e)
            return SendResult(success=False, error=str(e))

        body_json = json.dumps(
            [{
                "msgContent": msg_content,
                "fileSource": {"filePath": container_path},
                "mentions": {},
            }]
        )
        target = (
            f"#{chat_id[6:]}" if chat_id.startswith("group:") else f"@{chat_id}"
        )
        corr_id = self._make_corr_id()
        payload = {"corrId": corr_id, "cmd": f"/_send {target} json {body_json}"}

        async with self._send_lock:
            await self._send_ws(payload)
        return SendResult(success=True)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image, downloading the URL into the bind-mount staging
        dir first. Degrades to a text URL when SIMPLEX_FILE_DIR is unset."""
        return await self._send_media(chat_id, "image", image_url, caption)

    async def send_image_file(
        self,
        chat_id: str,
        path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return await self._send_media(chat_id, "image", path, caption)

    async def send_voice(
        self,
        chat_id: str,
        path: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return await self._send_media(chat_id, "voice", path, None)

    async def send_video(
        self,
        chat_id: str,
        path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return await self._send_media(chat_id, "video", path, caption)

    async def send_document(
        self,
        chat_id: str,
        path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return await self._send_media(chat_id, "file", path, caption)

    async def send_animation(
        self,
        chat_id: str,
        path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        # SimpleX doesn't distinguish animation from video on the wire.
        return await self._send_media(chat_id, "video", path, caption)

    async def get_chat_info(self, chat_id: str) -> dict:
        """Return basic chat info."""
        if chat_id.startswith("group:"):
            return {"chat_id": chat_id, "type": "group", "name": chat_id[6:]}
        return {"chat_id": chat_id, "type": "dm", "name": chat_id}


# ---------------------------------------------------------------------------
# Plugin entry-point hooks
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """Plugin gate: require SIMPLEX_WS_URL AND the websockets package.

    Returning False keeps the platform out of ``get_connected_platforms()``
    so the gateway never instantiates the adapter when the dependency is
    missing or no daemon URL is configured.
    """
    if not os.getenv("SIMPLEX_WS_URL"):
        return False
    try:
        import websockets  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config) -> bool:
    """Validate that the platform config has enough info to connect."""
    extra = getattr(config, "extra", {}) or {}
    ws_url = os.getenv("SIMPLEX_WS_URL") or extra.get("ws_url", "")
    return bool(ws_url)


def is_connected(config) -> bool:
    """Check whether SimpleX is configured (env or config.yaml)."""
    extra = getattr(config, "extra", {}) or {}
    ws_url = os.getenv("SIMPLEX_WS_URL") or extra.get("ws_url", "")
    return bool(ws_url)


def _env_enablement() -> dict | None:
    """Seed ``PlatformConfig.extra`` from env vars during gateway config load.

    Called by the platform registry's env-enablement hook BEFORE adapter
    construction, so ``gateway status`` and ``get_connected_platforms()``
    reflect env-only configuration without instantiating the WebSocket
    client. Returns ``None`` when SimpleX isn't minimally configured.

    The special ``home_channel`` key in the returned dict is handled by
    the core hook — it becomes a proper ``HomeChannel`` dataclass on the
    ``PlatformConfig`` rather than being merged into ``extra``.
    """
    ws_url = os.getenv("SIMPLEX_WS_URL", "").strip()
    if not ws_url:
        return None
    seed: dict = {"ws_url": ws_url}
    home = os.getenv("SIMPLEX_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("SIMPLEX_HOME_CHANNEL_NAME", "").strip() or home,
        }
    return seed


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Open an ephemeral WebSocket to the daemon, send, and close.

    Used by ``tools/send_message_tool._send_via_adapter`` when the gateway
    runner is not in this process (e.g. ``hermes cron`` running as a
    separate process from ``hermes gateway``). Without this hook,
    ``deliver=simplex`` cron jobs fail with "No live adapter for platform".

    ``thread_id`` and ``force_document`` are accepted for signature parity
    with other plugins but are not meaningful here. ``media_files`` is
    accepted but only the text body is delivered — SimpleX requires the
    daemon's filesystem-backed file flow which an ephemeral connection
    cannot drive safely.
    """
    try:
        import websockets as _wsclient
    except ImportError:
        return {"error": "websockets not installed. Run: pip install websockets"}

    extra = getattr(pconfig, "extra", {}) or {}
    ws_url = os.getenv("SIMPLEX_WS_URL") or extra.get("ws_url", "ws://127.0.0.1:5225")
    if not ws_url:
        return {"error": "SimpleX standalone send: SIMPLEX_WS_URL is required"}

    try:
        if chat_id.startswith("group:"):
            group_id = chat_id[6:]
            cmd_str = f"#[{group_id}] {message}"
        else:
            cmd_str = f"@[{chat_id}] {message}"

        payload = {
            "corrId": f"hermes-snd-{int(time.time() * 1000)}",
            "cmd": cmd_str,
        }

        async with _wsclient.connect(ws_url, open_timeout=10, close_timeout=5) as ws:
            await ws.send(json.dumps(payload))
            # Give the daemon a moment to process the command before closing.
            await asyncio.sleep(0.5)

        return {"success": True, "platform": "simplex", "chat_id": chat_id}
    except Exception as e:
        return {"error": f"SimpleX send failed: {e}"}


def interactive_setup() -> None:
    """Minimal stdin wizard for ``hermes setup gateway`` → SimpleX.

    Prompts for the WebSocket URL and the optional allowlist / home channel.
    Writes to ``~/.hermes/.env`` via ``hermes_cli.config``.
    """
    print()
    print("SimpleX Chat setup")
    print("------------------")
    print("Requirements:")
    print("  1. simplex-chat daemon running (e.g. `simplex-chat -p 5225`).")
    print("  2. Python package `websockets` installed (`pip install websockets`).")
    print()

    try:
        from hermes_cli.config import get_env_value, save_env_value
    except ImportError:
        print("hermes_cli.config not available; set SIMPLEX_* vars manually in ~/.hermes/.env")
        return

    def _prompt(var: str, prompt: str, *, secret: bool = False) -> None:
        existing = get_env_value(var) if callable(get_env_value) else None
        suffix = " [keep current]" if existing else ""
        try:
            if secret:
                import getpass
                value = getpass.getpass(f"{prompt}{suffix}: ")
            else:
                value = input(f"{prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if value:
            save_env_value(var, value)

    _prompt("SIMPLEX_WS_URL", "Daemon WebSocket URL (default ws://127.0.0.1:5225)")
    _prompt("SIMPLEX_ALLOWED_USERS", "Allowed contact IDs (comma-separated; blank=skip)")
    _prompt("SIMPLEX_HOME_CHANNEL", "Home channel contact/group ID (or empty)")
    print("Done. Make sure the simplex-chat daemon is running before starting the gateway.")


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name="simplex",
        label="SimpleX Chat",
        adapter_factory=lambda cfg: SimplexAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["SIMPLEX_WS_URL"],
        install_hint="pip install websockets   # SimpleX adapter requires the websockets package",
        setup_fn=interactive_setup,
        # Env-driven auto-configuration: seeds PlatformConfig.extra so
        # env-only setups show up in `hermes gateway status` without
        # instantiating the adapter.
        env_enablement_fn=_env_enablement,
        # Cron home-channel delivery support — `deliver=simplex` cron jobs
        # route to SIMPLEX_HOME_CHANNEL when set.
        cron_deliver_env_var="SIMPLEX_HOME_CHANNEL",
        # Out-of-process cron delivery. Without this hook, deliver=simplex
        # cron jobs fail with "No live adapter" when cron runs separately
        # from the gateway.
        standalone_sender_fn=_standalone_send,
        # Auth env vars for _is_user_authorized() integration
        allowed_users_env="SIMPLEX_ALLOWED_USERS",
        allow_all_env="SIMPLEX_ALLOW_ALL_USERS",
        # SimpleX has no hard line length; we still chunk for sanity.
        max_message_length=MAX_MESSAGE_LENGTH,
        # Display
        emoji="🔒",
        # SimpleX uses opaque contact IDs only — no phone numbers or
        # email addresses to redact.
        pii_safe=True,
        allow_update_command=True,
        # LLM guidance
        platform_hint=(
            "You are chatting via SimpleX Chat, a private decentralised "
            "messenger. Contacts are identified by opaque internal IDs, "
            "not phone numbers or usernames. SimpleX supports standard "
            "markdown formatting. There is no typing indicator and no "
            "hard message length limit, but keep responses conversational."
        ),
    )

    # CLI surface: `hermes simplex list|join`. Lazy import so missing
    # optional deps (websockets) don't block platform registration.
    try:
        from .cli import register_cli, simplex_command
    except ImportError as e:
        logger.debug("simplex: CLI commands unavailable (%r)", e)
        return
    ctx.register_cli_command(
        name="simplex",
        help="Discover SimpleX contacts/groups and join via invitation link",
        setup_fn=register_cli,
        handler_fn=simplex_command,
        description=(
            "Operator helpers for SimpleX Chat: list the contacts and "
            "groups the daemon is connected to, or join a group via an "
            "invitation link. Useful for discovering the numeric IDs "
            "needed by SIMPLEX_HOME_CHANNEL and SIMPLEX_ALLOWED_USERS."
        ),
    )
