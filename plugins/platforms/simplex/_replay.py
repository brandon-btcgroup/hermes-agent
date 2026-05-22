"""Per-group missed-message replay state for the SimpleX adapter.

Two responsibilities, narrowly scoped:

  1. Persist the last item id we successfully dispatched for each group
     to a JSON cursor file under ``$HERMES_HOME/simplex/cursors.json``.
     On reconnect, the adapter consults the cursor and pulls everything
     newer via ``/_get chat #<gid> after=<cursor>`` so messages sent while
     Hermes was offline aren't lost.

  2. Track recently-dispatched ``(group_id, item_id)`` tuples in an
     in-memory ring so the replay stream and the live ``newChatItems``
     stream can coexist without double-processing the items at the
     overlap.

The replay loop itself lives in the adapter; this module is pure state.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import deque
from pathlib import Path
from typing import Deque, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

CURSOR_FILE_VERSION = 1
DEFAULT_DEDUPE_SIZE = 1024


class ReplayState:
    """Cursor file + dedupe ring, loaded lazily.

    All file I/O is best-effort: if the cursor file is missing, corrupt,
    or unwritable, replay degrades gracefully (no replay, but no crash).
    """

    def __init__(
        self,
        cursor_path: Path,
        *,
        dedupe_size: int = DEFAULT_DEDUPE_SIZE,
    ) -> None:
        self._path = cursor_path
        self._cursors: Dict[int, int] = {}
        self._ring: Deque[Tuple[int, int]] = deque(maxlen=dedupe_size)
        self._ring_set: set[Tuple[int, int]] = set()
        self._loaded = False

    def load(self) -> None:
        """Read cursors from disk. Idempotent and safe to call repeatedly."""
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as e:
            logger.warning("simplex replay: cannot read cursor file %s: %s", self._path, e)
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("simplex replay: cursor file %s is corrupt: %s", self._path, e)
            return
        groups = data.get("groups") if isinstance(data, dict) else None
        if not isinstance(groups, dict):
            return
        for gid_str, item_id in groups.items():
            try:
                self._cursors[int(gid_str)] = int(item_id)
            except (TypeError, ValueError):
                continue

    def get_cursor(self, group_id: int) -> Optional[int]:
        return self._cursors.get(group_id)

    def known_groups(self) -> list[int]:
        """Group IDs we have cursors for — useful for selective replay."""
        return list(self._cursors.keys())

    def update_cursor(self, group_id: int, item_id: int) -> None:
        """Advance the cursor monotonically and persist.

        Skips persistence if the new id isn't larger than the stored one
        (so out-of-order arrivals don't rewind the cursor).
        """
        existing = self._cursors.get(group_id, -1)
        if item_id <= existing:
            return
        self._cursors[group_id] = item_id
        self._persist()

    def already_dispatched(self, group_id: int, item_id: int) -> bool:
        return (group_id, item_id) in self._ring_set

    def mark_dispatched(self, group_id: int, item_id: int) -> None:
        key = (group_id, item_id)
        if key in self._ring_set:
            return
        if len(self._ring) == self._ring.maxlen:
            evicted = self._ring[0]
            self._ring_set.discard(evicted)
        self._ring.append(key)
        self._ring_set.add(key)

    def _persist(self) -> None:
        """Write cursors atomically (tmp + replace). Best-effort."""
        payload = {
            "version": CURSOR_FILE_VERSION,
            "groups": {str(gid): cid for gid, cid in self._cursors.items()},
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("simplex replay: cannot create %s: %s", self._path.parent, e)
            return
        tmp_path: Optional[Path] = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                prefix=".cursors-", suffix=".tmp", dir=str(self._path.parent)
            )
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"))
            tmp_path.replace(self._path)
            tmp_path = None
        except OSError as e:
            logger.warning("simplex replay: cannot persist cursor file %s: %s", self._path, e)
        finally:
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
