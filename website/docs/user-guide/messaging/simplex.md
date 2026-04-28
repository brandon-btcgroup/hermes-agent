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

## What works

- Inbound text messages from configured groups → full Hermes session, slash commands, cron, skills, tool calls.
- Inbound + outbound **media** — image, file, voice, and video — over a shared bind-mount (see [Media setup](#media-setup) below).
- Outbound text replies.
- Self-echo filtering (Hermes ignores its own messages).
- Allowlist on the sender's display name (`SIMPLEX_ALLOWED_USERS`).
- Automatic reconnection if the daemon restarts or the network blips.
- Cron delivery: `cronjob action="create" deliver="simplex" target="<group_id>" ...`.
- Missed-message replay across Hermes restarts. On reconnect, the adapter walks `/_get chat` forward from a per-group cursor stored at `~/.hermes/simplex/cursors.json` and dispatches anything new, capped by `SIMPLEX_REPLAY_MAX` (default 200).

## Media setup

simplex-chat reads and writes attachments under a "files folder" inside its
container (default `/root/.simplex/files`). For Hermes — running outside the
container — to deliver attachments to the agent and to send outbound files,
that folder must be **bind-mounted to a host directory readable by Hermes**.

Add the mount to your daemon's run config. Example for a Quadlet `.container` file:

```ini
Volume=%h/.hermes/cache/simplex-files:/root/.simplex/files:Z
```

Or for `docker-compose.yml`:

```yaml
services:
  simplex-chat:
    image: simplexchat/simplex-chat:latest
    volumes:
      - simplex-chat-data:/root/.simplex
      - ~/.hermes/cache/simplex-files:/root/.simplex/files
```

Then in `~/.hermes/.env`:

```bash
SIMPLEX_FILE_DIR=~/.hermes/cache/simplex-files          # host path; default
SIMPLEX_DAEMON_FILES_FOLDER=/root/.simplex/files        # container path; default
SIMPLEX_MAX_FILE_BYTES=52428800                         # 50 MiB; default
```

On connect, the adapter writes a probe file to `SIMPLEX_FILE_DIR`. If the path
is missing or unwritable, media is disabled with a warning and **text continues
to work**.

**Inbound flow.** When a phone sends an image/file/voice/video, simplex-chat
writes it to the container files folder; with the bind-mount, the file appears
on the host at `SIMPLEX_FILE_DIR/<fileName>` and Hermes surfaces it to the
agent as a `media_urls=[host_path]` event.

**Outbound flow.** `send_image`, `send_image_file`, `send_voice`, and
`send_video` stage the source file into `SIMPLEX_FILE_DIR/<uuid>-<name>` and
hand the daemon the matching container path via `/_send`'s `fileSource`
field. Image and video msgContent include a base64 thumbnail when Pillow is
available; voice/video include a duration probed by `ffprobe` if installed.

## What's not yet implemented

- **Direct messages.** Hermes only listens in groups for now. To talk 1:1, create a 2-person group.
- **Manual auto-receive recovery.** If the daemon was started without auto-receive enabled, files stay in `rcvInvitation` and the adapter's polling never sees them complete. Pass `--auto-accept-files <bytes>` to the daemon (or `-a` for unlimited). The adapter handles the wait-for-download race for files the daemon *does* accept; it cannot accept on the daemon's behalf.
- **Streaming replies via message edits.** `apiUpdateChatItem` exists at the protocol level, but how SimpleX phone clients render in-place edits is unverified. Tracked behind a real-device test.
- **Reactions.** Need to verify protocol support before scoping.
- **Typing indicators.** Permanent gap — simplex-chat itself has no typing API. Not an adapter limitation; would require upstream simplex-chat to add it.
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
| `SIMPLEX_REPLAY_MAX` | no | Maximum missed messages replayed per group on reconnect (default 200) |
| `SIMPLEX_REPLAY_DISABLE` | no | `true` to skip missed-message replay entirely |
| `SIMPLEX_FILE_DIR` | for media | Host path bind-mounted to the daemon's files folder (default `~/.hermes/cache/simplex-files`) |
| `SIMPLEX_DAEMON_FILES_FOLDER` | no | Container-side files folder (default `/root/.simplex/files`) |
| `SIMPLEX_MAX_FILE_BYTES` | no | Reject inbound/outbound files above this size in bytes (default 52428800 = 50 MiB) |
| `SIMPLEX_FILE_WAIT_S` | no | Seconds to wait for inbound XFTP downloads to finish before falling back to a text placeholder (default 60). Set to `0` to disable polling and downgrade pending files immediately. |
