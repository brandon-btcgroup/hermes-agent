---
sidebar_position: 7
title: "SimpleX"
description: "Set up Hermes Agent as a SimpleX Chat bot via a self-hosted simplex-chat daemon"
---

# SimpleX Setup

Hermes connects to [SimpleX Chat](https://simplex.chat/) — a privacy-focused, decentralized messenger with no user identifiers — through the official `simplex-chat` daemon running in headless mode. The adapter speaks the daemon's WebSocket JSON-RPC protocol directly; no extra bridge or sidecar is needed.

SimpleX has the strongest metadata-minimization story of any mainstream messenger: there is no account, phone number, or username. Routing identifiers are short-lived per-connection. This makes it a good fit for agent workflows where you want privacy on the network path between you and your bot.

:::info One Python dep
The SimpleX adapter pulls in [`websockets`](https://pypi.org/project/websockets/) via the `[simplex]` extra. Install with `pip install -e ".[all]"` or `pip install -e ".[simplex]"`.
:::

---

## Prerequisites

- **simplex-chat** — the official Haskell daemon, running headless and reachable from your Hermes host. Recommended deployment: Docker.
- **A SimpleX client on your phone** — to message the bot.
- **A SimpleX group** — Hermes only listens in groups in v1, not direct chats. Create one on your phone and invite Hermes via the group invitation link.

---

## 1. Run the simplex-chat daemon

The simplest path is Docker. Save this as `docker-compose.yml` somewhere convenient:

```yaml
services:
  simplex-chat:
    image: simplexchat/simplex-chat:latest
    command:
      - "--create-bot-display-name=hermes"
      - "-p"
      - "5225"
    ports:
      - "5225:5225"      # expose to your trusted LAN; do NOT expose publicly
    volumes:
      - simplex-chat-data:/root/.simplex
    restart: unless-stopped

volumes:
  simplex-chat-data:
```

Then:

```bash
docker compose up -d
```

The daemon's WebSocket is **unauthenticated** — anyone with network access to port 5225 can read your messages and impersonate the bot. Keep it on a trusted LAN, or front it with a TLS+auth reverse proxy if you must expose it.

---

## 2. Configure Hermes

In `~/.hermes/.env`:

```bash
SIMPLEX_WS_URL=ws://localhost:5225          # or ws://simplex-chat.lan:5225
SIMPLEX_GROUP_IDS=                          # filled in by the join step below
SIMPLEX_ALLOWED_USERS=<your-display-name>   # the SimpleX display name you'll
                                            # message Hermes from
```

You can also set `SIMPLEX_HOME_GROUP_ID` (default group for cron jobs that say `deliver=simplex` without a target) and `SIMPLEX_MAX_RECONNECT_DELAY_S` (default 60).

---

## 3. Join a group via invite link

On your phone: create a SimpleX group, generate an invitation link, and copy it.

Then on the Hermes host:

```bash
hermes simplex join "<paste-the-invitation-link>"
```

The CLI:

1. Connects to your daemon at `SIMPLEX_WS_URL`.
2. Snapshots the existing group list.
3. Sends the invitation.
4. Polls until the new group materializes (up to 120 s).
5. Prints the new numeric `groupId`.
6. Appends it to `SIMPLEX_GROUP_IDS` in `~/.hermes/.env`.

If you joined the group out-of-band (or want to confirm what's already configured):

```bash
hermes simplex list
```

---

## 4. Restart Hermes

```bash
# bare process
hermes

# systemd user unit
systemctl --user restart hermes-gateway
```

Send a message in the group from your phone. Hermes should reply within a few seconds on a healthy SMP path.

---

## What works in v1

- Inbound text messages from configured groups → full Hermes session, slash commands, cron, skills, tool calls.
- Outbound text replies.
- Self-echo filtering (Hermes ignores its own messages).
- Allowlist on the sender's display name (`SIMPLEX_ALLOWED_USERS`).
- Automatic reconnection if the daemon restarts or the network blips.
- Cron delivery: `cronjob action="create" deliver="simplex" target="<group_id>" ...`.

## What's not in v1

- **Direct messages.** Hermes only listens in groups for now. To talk 1:1, create a 2-person group.
- **Images, files, voice, video.** Protocol-supported by simplex-chat (`MsgContent` types `image`, `file`, `voice`, `video`); just not implemented yet in this adapter. Tracked for v1.1+.
- **Streaming replies via message edits.** `apiUpdateChatItem` exists at the protocol level, but how SimpleX phone clients render in-place edits is unverified. Tracked for v1.1+ behind a real-device test.
- **Reactions.** Need to verify protocol support before scoping.
- **Typing indicators.** Permanent gap — simplex-chat itself has no typing API. Not an adapter limitation; would require upstream simplex-chat to add it.
- **Missed-message replay across Hermes downtime.** simplex-chat stores messages it received while Hermes was offline but does not re-emit them when a fresh WS client connects. The user sees a delivered message but Hermes never processes it. Workaround: keep Hermes running. A proper fix (replay via `/_get_chat` on reconnect) is tracked for v1.1.

## Security notes

- The daemon's WS port is unauthenticated by design — simplex-chat assumes the network path is trusted (loopback or LAN). Anyone on that network can read your group messages and send messages as the bot. Treat `:5225` exposure with the same care as a database port.
- The SimpleX protocol itself between the daemon and SMP servers is end-to-end encrypted; nothing about the Hermes integration changes that.
- `SIMPLEX_ALLOWED_USERS` matches the sender's display name string. Display names are user-controlled and not unique within a group — for low-trust groups, prefer a 2-person group with just you and Hermes.

---

## Troubleshooting

**`hermes simplex list` says "no active user"**
The simplex-chat daemon hasn't finished initialising or wasn't started with `--create-bot-display-name`. Check `docker logs simplex-chat`.

**"DAEMON_UNREACHABLE" in Hermes logs**
Hermes can't reach `SIMPLEX_WS_URL`. Verify the URL is right (note `ws://`, not `http://`) and that the daemon's port is bound on an interface Hermes can reach.

**Group joined on phone but doesn't appear in `hermes simplex list`**
The invitation handshake takes seconds-to-minutes. Wait, then re-list. If the daemon was offline when you generated the invite, regenerate it.

**Sender shows as "unknown"**
The adapter reads `chatDir.groupMember.localDisplayName` (with a fallback to `memberProfile.displayName`). If both are empty, the sender hasn't set a profile name in SimpleX yet — set one under Settings → Your profile and resend.

---

## Reference

| Env var | Required | Description |
|---|---|---|
| `SIMPLEX_WS_URL` | yes | WebSocket URL of the simplex-chat daemon (e.g. `ws://localhost:5225`) |
| `SIMPLEX_GROUP_IDS` | yes | Comma-separated numeric group IDs to listen in. Populate via `hermes simplex join`. |
| `SIMPLEX_ALLOWED_USERS` | strongly recommended | Comma-separated SimpleX display names allowed to talk to the bot |
| `SIMPLEX_ALLOW_ALL_USERS` | no | `true` to disable the allowlist (not recommended) |
| `SIMPLEX_HOME_GROUP_ID` | no | Default group for cron deliveries with `deliver=simplex` and no target |
| `SIMPLEX_MAX_RECONNECT_DELAY_S` | no | Cap on exponential backoff between reconnect attempts (default 60) |
