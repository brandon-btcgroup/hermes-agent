"""End-to-end smoke test against a real simplex-chat daemon.

Skipped by default.  Enable with:

  SIMPLEX_E2E=1 \
  SIMPLEX_WS_URL=ws://localhost:5225 \
  pytest tests/e2e/test_simplex_smoke.py -v

The ``SIMPLEX_E2E=1`` gate keeps this off CI and out of normal pytest
runs even when SIMPLEX_WS_URL is set in the dev shell.
"""

from __future__ import annotations

import os

import pytest


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("SIMPLEX_E2E", "").strip() not in ("1", "true", "yes"),
        reason="SIMPLEX_E2E not enabled (set SIMPLEX_E2E=1 to run)",
    ),
]


@pytest.mark.asyncio
async def test_active_user_handshake():
    """Connect to the daemon and read the active user."""
    from gateway.platforms.simplex_client import SimplexChatClient

    ws_url = os.getenv("SIMPLEX_WS_URL", "ws://localhost:5225")
    async with SimplexChatClient(ws_url) as client:
        user = await client.api_get_active_user()
        assert user.user_id is not None
        # display_name may be empty for newly-created profiles, just type-check.
        assert isinstance(user.display_name, str)


@pytest.mark.asyncio
async def test_groups_list_shape():
    """``/groups`` returns a list (possibly empty) of well-formed entries."""
    from gateway.platforms.simplex_client import SimplexChatClient

    ws_url = os.getenv("SIMPLEX_WS_URL", "ws://localhost:5225")
    async with SimplexChatClient(ws_url) as client:
        groups = await client.api_get_groups()
        for g in groups:
            assert isinstance(g.group_id, int)
            assert isinstance(g.display_name, str)
