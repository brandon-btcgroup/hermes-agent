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

### Docker Compose (text-only, simplest)

For a text-only bot using SimpleX's public servers, save this as
`docker-compose.yml`:

```yaml
services:
  simplex-chat:
    image: simplexchat/simplex-chat:latest
    command:
      - "--create-bot-display-name=hermes"
      - "--create-bot-allow-files"
      - "--auto-accept-files"
      - "52428800"
      - "--files-folder"
      - "/root/.simplex/files"
      - "--temp-folder"
      - "/root/.simplex/files/.xftp-tmp"
      - "-p"
      - "5225"
    ports:
      - "127.0.0.1:5225:5225"   # loopback only — WS has NO auth
    volumes:
      - simplex-chat-data:/root/.simplex
      - ${HOME}/.hermes/cache/simplex-files:/root/.simplex/files
    restart: unless-stopped

volumes:
  simplex-chat-data:
```

Then `docker compose up -d`. Why each flag:

- `--create-bot-allow-files` — sets the bot profile's `files: yes` preference at creation. Without it, peer clients refuse to send attachments to the bot. (If your bot is already running without this flag, the recovery is a runtime `/set files yes` via the WS — see [Troubleshooting](#troubleshooting).)
- `--auto-accept-files 52428800` — auto-accept incoming files up to 50 MiB. The boolean short form `-a` alone is silently a no-op in some builds; prefer the long form with an explicit byte limit.
- `--files-folder` + `--temp-folder` — both must live on the same filesystem (the bind-mount), otherwise XFTP downloads fail with `Invalid cross-device link` when the daemon tries to atomically move the decrypted file into place. Default `--temp-folder` is `/tmp`, which causes EXDEV on any container with a separate files mount.

### Quadlet / rootless Podman

For Bazzite, Fedora Atomic, or any rootless-Podman host, use the
templates in this repo:

```bash
cp simplex-chat-hermes.container.example   ~/.config/containers/systemd/simplex-chat-hermes.container
cp simplex-chat-hermes.env.example         ~/.config/containers/systemd/simplex-chat-hermes.env
$EDITOR ~/.config/containers/systemd/simplex-chat-hermes.env       # fill in SMP/XFTP URLs
mkdir -p ~/.hermes/cache/simplex-files
systemctl --user daemon-reload
systemctl --user enable --now simplex-chat-hermes
```

The Quadlet template assumes self-hosted SMP + XFTP servers. Drop the
`-s` and `--xftp-server` lines from `Exec=` to use SimpleX's defaults.

### Self-hosted SMP and XFTP servers

If you run your own SMP and XFTP servers, both URLs in your env file must
include the cert fingerprint **and** the basic-auth token, in the form:

```
<proto>://<base64-fingerprint>:<auth-token>@<host>:<port>
```

Find the canonical URLs in your server's INI. For `smp-server`,
`/etc/opt/simplex/smp-server.ini` has `server_address=`. For
`xftp-server`, `/etc/opt/simplex-xftp/file-server.ini` does.

The auth token gates queue / file creation on the server. Without it, the
phone client gets `unknownServers` errors from the bot daemon when it
tries to download attachments.

See the [Security notes](#security-notes) section below for the full
deployment caveats; the headline is that the WS port is unauthenticated
and must stay on `127.0.0.1` (or behind a TLS+auth proxy).

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

**Hermes replies "I didn't get the image" / pictures arrive as 0-byte files**
You're hitting the XFTP download path. Several things have to be right:

1. **Bind-mount.** The daemon's files folder must be exposed to Hermes on the host. Check `~/.hermes/cache/simplex-files/` is writable from your user *and* visible to the daemon at `/root/.simplex/files/` (or whatever `SIMPLEX_DAEMON_FILES_FOLDER` is).
2. **Auto-accept enabled.** `podman exec simplex-chat-hermes ps -ef | grep simplex-chat` should show `--auto-accept-files <bytes>` in the args. The boolean short form `-a` alone is silently a no-op in some builds.
3. **`--temp-folder` on the same filesystem as `--files-folder`.** If the temp folder is the default `/tmp`, XFTP downloads complete in `/tmp/` then fail to atomically rename into the bind-mounted files folder with `unsupported operation (Invalid cross-device link)`. The fix is `--temp-folder /root/.simplex/files/.xftp-tmp` (a subdir of the bind-mount).
4. **XFTP server URL has fingerprint AND auth token.** Check `~/.config/containers/systemd/simplex-chat-hermes.env`'s `XFTP_SERVER` is `xftp://<fingerprint>:<token>@host:port`, not just `xftp://<fingerprint>@host:port`. Without the token, the daemon errors with `fileNotApproved` and `unknownServers`.
5. **Bot profile allows files.** If the bot was created without `--create-bot-allow-files`, peer clients see the bot's `files: no` preference and refuse to send. Recovery without recreating the bot — send `/set files yes` over the daemon WS:

   ```bash
   ~/.hermes/hermes-agent/venv/bin/python -c '
   import asyncio, json, websockets
   async def go():
       async with websockets.connect("ws://localhost:5225") as ws:
           await ws.send(json.dumps({"corrId":"1","cmd":"/set files yes"}))
           print(await asyncio.wait_for(ws.recv(), 5.0))
   asyncio.run(go())
   '
   ```

   Confirm by re-querying — `memberProfile.preferences.files.allow` should flip from `"no"` to `"yes"`.

If the file does land on the host but Hermes still says "didn't get the image", you're hitting the dispatch race window — the photo arrived after `SIMPLEX_FILE_WAIT_S` (default 60 s). Increase that env var if your XFTP server is slow.

**`fileNotApproved` errors with `unknownServers`**
The XFTP server URL referenced by the file invitation isn't in the daemon's known-server list. Add `--xftp-server <url>` to the daemon's `Exec=` line (one-time write to its DB), then drop the flag back off — leaving it on permanently triggers `UNIQUE constraint failed: protocol_servers.user_id, protocol_servers.host, protocol_servers.port` on subsequent restarts.

If the server URL was added without the basic-auth token and you need to fix the existing DB row instead of recreating it, stop the daemon and update directly:

```bash
DBROOT=$(podman volume inspect simplex-chat-hermes-data --format '{{.Mountpoint}}')
systemctl --user stop simplex-chat-hermes
~/.hermes/hermes-agent/venv/bin/python -c "
import sqlite3
con = sqlite3.connect('$DBROOT/simplex_v1_chat.db')
con.execute(\"UPDATE protocol_servers SET basic_auth=? WHERE host=? AND port=? AND protocol=?\",
            ('YOUR-AUTH-TOKEN', 'xftp.example.com', '5225', 'xftp'))
con.commit()
print(list(con.execute(\"SELECT host, port, basic_auth, protocol FROM protocol_servers\")))
"
systemctl --user start simplex-chat-hermes
```

**Inbound queue silent after a daemon restart**
After multiple back-to-back daemon restarts, the bot's SMP queue subscriptions can take 30 s – several minutes to re-establish. If text messages from your phone aren't reaching Hermes (and `journalctl --user -u hermes-gateway` shows no `simplex` activity), wait a few minutes; the subscription usually catches up. If it doesn't, restart the daemon one more time — the next clean startup re-subscribes all queues.

**Daemon won't start with `UNIQUE constraint failed: protocol_servers`**
The `--smp-server` or `--xftp-server` flag is trying to insert a server URL that already exists in the daemon's DB but with different details (auth token added/changed). Remove the offending flag from `Exec=`; the existing entry is what gets used. If you need to *update* the URL of an existing entry, do it with a SQLite `UPDATE` (see the snippet above) rather than re-passing the flag.

**`commitBuffer: invalid argument (cannot encode character ...)`**
Cosmetic only — simplex-chat tried to print a Unicode character (em-dash, emoji) to a stdout stream that doesn't support it. Doesn't affect message delivery.

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
