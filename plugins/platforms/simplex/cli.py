"""``hermes simplex`` CLI subcommands.

Two helpers for users running a ``simplex-chat`` daemon. SimpleX exposes
contacts and groups by opaque numeric IDs, so a fresh user has no
self-service way to discover what to put in ``SIMPLEX_HOME_CHANNEL`` or
``SIMPLEX_ALLOWED_USERS``. These commands close that gap:

  hermes simplex list           — print {contacts, groups} with numeric
                                  IDs and display names.
  hermes simplex join <link>    — join a SimpleX group via invitation
                                  link, poll until the new group
                                  materialises, print the new groupId.

Neither command writes to ``~/.hermes/.env`` — they print the IDs and let
the user paste them where they want (``SIMPLEX_HOME_CHANNEL`` for a single
cron-delivery default, ``SIMPLEX_ALLOWED_USERS`` for the allowlist).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Optional

logger = logging.getLogger(__name__)

JOIN_POLL_INTERVAL_S = 2.0
JOIN_POLL_TIMEOUT_S = 120.0


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Wire ``simplex {list,join}`` into ``hermes <plugin>`` argparse.

    Called by the plugin loader via ``ctx.register_cli_command(...)``.
    """
    subparser.add_argument(
        "--ws-url",
        dest="ws_url",
        default=None,
        help="Override SIMPLEX_WS_URL for this invocation.",
    )
    sub = subparser.add_subparsers(dest="simplex_action")

    list_p = sub.add_parser(
        "list",
        help="List contacts and groups the daemon is currently connected to.",
    )
    list_p.set_defaults(simplex_action="list")

    join_p = sub.add_parser(
        "join",
        help="Join a SimpleX group via invitation link; print the new groupId.",
    )
    join_p.add_argument(
        "invite_link",
        help=(
            "The SimpleX invitation link "
            "(https://simplex.chat/contact#... or simplex:/...)."
        ),
    )
    join_p.add_argument(
        "--timeout",
        type=float,
        default=JOIN_POLL_TIMEOUT_S,
        help=(
            f"Seconds to wait for the new group to materialise "
            f"(default: {int(JOIN_POLL_TIMEOUT_S)})."
        ),
    )
    join_p.set_defaults(simplex_action="join")


def simplex_command(args: argparse.Namespace) -> None:
    """Dispatcher for ``hermes simplex <action>``.

    Called by the plugin loader's ``handler_fn`` when the user runs
    ``hermes simplex ...``.
    """
    action = getattr(args, "simplex_action", None)
    if action == "list":
        _run(_cmd_list(args))
    elif action == "join":
        _run(_cmd_join(args))
    else:
        print("usage: hermes simplex {list,join} ...", file=sys.stderr)
        sys.exit(2)


def _run(coro) -> None:
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        print()
        sys.exit(130)


def _resolve_ws_url(args: argparse.Namespace) -> Optional[str]:
    url = getattr(args, "ws_url", None) or os.getenv("SIMPLEX_WS_URL", "")
    url = (url or "").strip()
    if not url:
        print(
            "SIMPLEX_WS_URL is not set. Pass --ws-url or set it in "
            "~/.hermes/.env (e.g. ws://127.0.0.1:5225).",
            file=sys.stderr,
        )
        return None
    return url


async def _cmd_list(args: argparse.Namespace) -> None:
    from ._ws_client import SimplexChatClient

    ws_url = _resolve_ws_url(args)
    if not ws_url:
        sys.exit(2)

    async with SimplexChatClient(ws_url) as client:
        try:
            user = await client.api_get_active_user()
        except Exception as e:
            print(f"failed to read active user from {ws_url}: {e}", file=sys.stderr)
            sys.exit(1)
        try:
            contacts = await client.api_get_contacts()
        except Exception as e:
            logger.debug("simplex list: api_get_contacts failed: %r", e)
            contacts = []
        try:
            groups = await client.api_get_groups()
        except Exception as e:
            logger.debug("simplex list: api_get_groups failed: %r", e)
            groups = []

    print(f"Active user: {user.display_name} (userId={user.user_id})")
    print()

    if contacts:
        print(f"Contacts ({len(contacts)}):")
        width = max(len(str(c.contact_id)) for c in contacts)
        for c in contacts:
            print(f"  {str(c.contact_id).rjust(width)}  {c.display_name}")
    else:
        print("Contacts: (none)")
    print()

    if groups:
        print(f"Groups ({len(groups)}):")
        width = max(len(str(g.group_id)) for g in groups)
        for g in groups:
            print(f"  {str(g.group_id).rjust(width)}  {g.display_name}")
    else:
        print("Groups: (none — use 'hermes simplex join <invite-link>' to join one)")


async def _cmd_join(args: argparse.Namespace) -> None:
    from ._ws_client import SimplexChatClient

    invite_link = (getattr(args, "invite_link", "") or "").strip()
    if not invite_link:
        print("usage: hermes simplex join <invite-link>", file=sys.stderr)
        sys.exit(2)

    ws_url = _resolve_ws_url(args)
    if not ws_url:
        sys.exit(2)

    timeout = float(getattr(args, "timeout", JOIN_POLL_TIMEOUT_S))

    async with SimplexChatClient(ws_url) as client:
        try:
            before = await client.api_get_groups()
        except Exception as e:
            print(f"failed to enumerate existing groups: {e}", file=sys.stderr)
            sys.exit(1)
        before_ids = {g.group_id for g in before}

        try:
            await client.api_connect(invite_link)
        except Exception as e:
            print(f"failed to send connect command: {e}", file=sys.stderr)
            sys.exit(1)

        print(
            f"Waiting up to {int(timeout)}s for the group to materialise..."
        )
        deadline = asyncio.get_running_loop().time() + timeout
        new_group = None
        elapsed = 0.0
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(JOIN_POLL_INTERVAL_S)
            elapsed += JOIN_POLL_INTERVAL_S
            try:
                current = await client.api_get_groups()
            except Exception as e:
                logger.debug("simplex join: poll failed: %r", e)
                continue
            added = [g for g in current if g.group_id not in before_ids]
            if added:
                new_group = added[0]
                break
            if int(elapsed) and int(elapsed) % 10 == 0:
                print(f"  ...still waiting ({int(elapsed)}s)")

    if new_group is None:
        print(
            f"No new group appeared after {int(timeout)}s. "
            "The connection may still be in progress — try "
            "'hermes simplex list' in a minute.",
            file=sys.stderr,
        )
        sys.exit(1)

    print()
    print(f"Joined: {new_group.display_name} (groupId={new_group.group_id})")
    print()
    print("To use this group with Hermes, add the ID to ~/.hermes/.env:")
    print(f"  SIMPLEX_HOME_CHANNEL={new_group.group_id}     # cron-delivery default")
    print(f"  # or append to SIMPLEX_ALLOWED_USERS for allowlist")
    print("Then restart the gateway to apply.")
