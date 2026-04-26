"""``hermes simplex`` CLI subcommands.

Two helpers for users running a vanilla ``simplex-chat`` daemon, with no
``simplex-bridge`` or other tooling required:

  hermes simplex list            — print groupId, displayName for every
                                   group the daemon already knows about.
  hermes simplex join <link>     — join a SimpleX group via invitation
                                   link, poll until the new group
                                   materialises, print the new groupId,
                                   and append it to ``~/.hermes/.env``
                                   under ``SIMPLEX_GROUP_IDS``.

These commands are how a fresh user discovers the numeric ``groupId``
that ``SIMPLEX_GROUP_IDS`` requires.  Without them, the daemon WS has no
self-service "list groups by name and pick one" UX.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Optional

logger = logging.getLogger(__name__)


JOIN_POLL_INTERVAL_S = 2.0
JOIN_POLL_TIMEOUT_S = 120.0


def cmd_simplex(args) -> None:
    """Dispatcher for ``hermes simplex <action>``."""
    action = getattr(args, "simplex_action", None) or "list"
    if action == "list":
        _run(_cmd_list(args))
    elif action == "join":
        _run(_cmd_join(args))
    else:
        print(f"Unknown simplex action: {action}", file=sys.stderr)
        sys.exit(2)


def _run(coro) -> None:
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)


def _resolve_ws_url(args) -> Optional[str]:
    url = getattr(args, "ws_url", None) or os.getenv("SIMPLEX_WS_URL", "")
    url = (url or "").strip()
    if not url:
        print(
            "✗ SIMPLEX_WS_URL is not set. Either pass --ws-url or set it in "
            "~/.hermes/.env (e.g. ws://localhost:5225).",
            file=sys.stderr,
        )
        return None
    return url


async def _cmd_list(args) -> None:
    from gateway.platforms.simplex_client import SimplexChatClient

    ws_url = _resolve_ws_url(args)
    if not ws_url:
        sys.exit(2)

    async with SimplexChatClient(ws_url) as client:
        try:
            user = await client.api_get_active_user()
        except Exception as e:
            print(f"✗ Failed to read active user from {ws_url}: {e}", file=sys.stderr)
            sys.exit(1)
        groups = await client.api_get_groups()

    print(f"Active user: {user.display_name} (id={user.user_id})")
    if not groups:
        print("No groups joined yet. Use: hermes simplex join <invite-link>")
        return
    print(f"{len(groups)} group(s):")
    width = max(len(str(g.group_id)) for g in groups)
    for g in groups:
        print(f"  {str(g.group_id).rjust(width)}  {g.display_name}")


async def _cmd_join(args) -> None:
    from gateway.platforms.simplex_client import SimplexChatClient

    invite_link = (getattr(args, "invite_link", "") or "").strip()
    if not invite_link:
        print("✗ usage: hermes simplex join <invite-link>", file=sys.stderr)
        sys.exit(2)

    ws_url = _resolve_ws_url(args)
    if not ws_url:
        sys.exit(2)

    print(f"→ Connecting to {ws_url}...")
    async with SimplexChatClient(ws_url) as client:
        try:
            user = await client.api_get_active_user()
        except Exception as e:
            print(f"✗ Daemon at {ws_url} is not ready: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"  Connected as: {user.display_name} (id={user.user_id})")

        before = await client.api_get_groups()
        before_ids = {g.group_id for g in before}

        print("→ Sending /c <invitation>...")
        connect_resp = await client.api_connect(invite_link)
        resp_type = connect_resp.get("type") if isinstance(connect_resp, dict) else None
        if resp_type and "error" in resp_type.lower():
            print(f"✗ Daemon rejected the invitation: {connect_resp}", file=sys.stderr)
            sys.exit(1)

        print(
            f"→ Waiting up to {int(JOIN_POLL_TIMEOUT_S)}s for the group to "
            "materialise..."
        )
        deadline = asyncio.get_running_loop().time() + JOIN_POLL_TIMEOUT_S
        new_group = None
        elapsed = 0
        while True:
            await asyncio.sleep(JOIN_POLL_INTERVAL_S)
            elapsed += JOIN_POLL_INTERVAL_S
            if asyncio.get_running_loop().time() > deadline:
                break
            try:
                current = await client.api_get_groups()
            except Exception as e:
                logger.debug("simplex join: poll failed: %r", e)
                continue
            added = [g for g in current if g.group_id not in before_ids]
            if added:
                new_group = added[0]
                break
            if int(elapsed) % 10 == 0:
                print(f"  ...still waiting ({int(elapsed)}s)")

    if new_group is None:
        print(
            f"✗ No new group appeared after {int(JOIN_POLL_TIMEOUT_S)}s.\n"
            f"  The connection may still be in progress — try "
            f"'hermes simplex list' in a minute.",
            file=sys.stderr,
        )
        sys.exit(1)

    gid = new_group.group_id
    print()
    print(f"✓ Joined: {new_group.display_name} (groupId={gid})")
    _persist_group_id(gid, getattr(args, "no_save", False))


def _persist_group_id(group_id: int, no_save: bool) -> None:
    if no_save:
        print(
            "Add this id to SIMPLEX_GROUP_IDS in ~/.hermes/.env "
            "(comma-separated)."
        )
        return
    try:
        from hermes_cli.config import get_env_value, save_env_value, is_managed
    except Exception as e:
        print(f"  (could not persist: {e})")
        return
    if is_managed():
        print(
            "Managed install detected — not modifying ~/.hermes/.env.\n"
            f"Add {group_id} to SIMPLEX_GROUP_IDS in your config yourself."
        )
        return

    existing = (get_env_value("SIMPLEX_GROUP_IDS") or "").strip()
    ids = [g.strip() for g in existing.split(",") if g.strip()]
    if str(group_id) in ids:
        print(f"  (already present in SIMPLEX_GROUP_IDS)")
        return
    ids.append(str(group_id))
    save_env_value("SIMPLEX_GROUP_IDS", ",".join(ids))
    print(f"  Saved to ~/.hermes/.env: SIMPLEX_GROUP_IDS={','.join(ids)}")
    print("  Restart Hermes (or 'systemctl --user restart hermes-gateway') to apply.")


def add_simplex_subparser(subparsers) -> None:
    """Register ``hermes simplex ...`` parsers.  Called from main.setup_parser."""
    simplex_parser = subparsers.add_parser(
        "simplex",
        help="SimpleX Chat utilities (list joined groups, join via invite link)",
    )
    simplex_parser.add_argument(
        "--ws-url",
        dest="ws_url",
        default=None,
        help="Override SIMPLEX_WS_URL for this invocation.",
    )
    sub = simplex_parser.add_subparsers(dest="simplex_action")

    list_parser = sub.add_parser(
        "list", help="List groups the simplex-chat daemon is currently joined to"
    )
    list_parser.set_defaults(func=cmd_simplex, simplex_action="list")

    join_parser = sub.add_parser(
        "join",
        help="Join a SimpleX group via invitation link, print the new groupId",
    )
    join_parser.add_argument(
        "invite_link",
        help="The SimpleX invitation link (https://simplex.chat/contact#... or "
        "https://simplex.chat/invitation#...)",
    )
    join_parser.add_argument(
        "--no-save",
        action="store_true",
        help="Don't append the groupId to ~/.hermes/.env (just print it).",
    )
    join_parser.set_defaults(func=cmd_simplex, simplex_action="join")

    # Default action when 'hermes simplex' is invoked with no subcommand.
    simplex_parser.set_defaults(func=cmd_simplex, simplex_action="list")
