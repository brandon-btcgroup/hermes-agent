# SimpleX Chat

[SimpleX Chat](https://simplex.chat/) is a private, decentralised messaging platform where users own their contacts and groups. Unlike other platforms, SimpleX assigns no persistent user IDs — every contact is identified by an opaque internal ID generated at connection time, which makes it one of the most private messengers available.

## Prerequisites

- The **simplex-chat** CLI installed and running as a daemon
- Python package **websockets** (`pip install websockets`)

## Install simplex-chat

Download the latest release from the [simplex-chat GitHub releases](https://github.com/simplex-chat/simplex-chat/releases) page, or via Docker:

```bash
# Linux / macOS binary
curl -L https://github.com/simplex-chat/simplex-chat/releases/latest/download/simplex-chat-ubuntu-22_04-x86-64 -o simplex-chat
chmod +x simplex-chat

# Or Docker
docker run -p 5225:5225 simplexchat/simplex-chat -p 5225
```

## Start the daemon

```bash
simplex-chat -p 5225
```

The daemon listens on WebSocket at `ws://127.0.0.1:5225` by default.

## Run via Docker Compose

For a long-running bot it's usually cleaner to manage the daemon under
Docker Compose so it restarts with the host and stores its profile on
a named volume. The recipe below also covers attachment delivery —
without the `--create-bot-allow-files` flag at first launch, peers
silently refuse to send files to the bot.

```yaml title="docker-compose.yml"
services:
  simplex-chat:
    image: simplexchat/simplex-chat:latest
    command:
      - -p
      - "5225"
      - --create-bot-allow-files          # bot profile accepts file sends
      - --auto-accept-files               # auto-accept incoming attachments
      - "52428800"                        # …up to 50 MiB (boolean is short for `true 1048576`)
    ports:
      - "127.0.0.1:5225:5225"             # bind to loopback only — WS is unauthenticated
    volumes:
      - simplex-chat-data:/root/.simplex                            # daemon profile state
      - ${HOME}/.hermes/cache/simplex-files:/root/.simplex/files    # host-readable attachments
    restart: unless-stopped

volumes:
  simplex-chat-data:
```

Bring it up with `docker compose up -d`.

Why each piece matters:

- **`--create-bot-allow-files`** sets the bot profile's `files: yes` preference at creation. Without it, peer SimpleX clients refuse to send attachments to the bot. If your bot is already running without this flag, the recovery is a runtime `/set files yes` via the WS.
- **`--auto-accept-files 52428800`** auto-accepts files up to 50 MiB. The integer form is a size cap; pass plain `true` for unlimited.
- **Loopback-only port binding** keeps the unauthenticated WebSocket off the network. If you need remote access put it behind a TLS+auth reverse proxy (nginx / Caddy / Traefik).
- **`simplex-chat-data` volume** persists the daemon's profile, contacts, and message keys across container restarts.
- **`${HOME}/.hermes/cache/simplex-files` bind-mount** lets Hermes read inbound attachments directly off the host filesystem. Hermes runs outside the container, so without this bind-mount it has no way to access files the daemon receives. The host directory is configurable via `SIMPLEX_FILE_DIR` (see [Environment variables](#configure-hermes)); the container side should match the daemon's `files` folder, which is `/root/.simplex/files` by default and overridable via `SIMPLEX_DAEMON_FILES_FOLDER`.

The daemon listens on WebSocket at `ws://127.0.0.1:5225` once it's up. Verify with `docker compose logs -f simplex-chat`; the first run will print the bot's connection link, which peer clients use to add the bot as a contact.

## Configure Hermes

### Via setup wizard

```bash
hermes setup gateway
```

Select **SimpleX Chat** and follow the prompts.

### Via environment variables

Add these to `~/.hermes/.env`:

```
SIMPLEX_WS_URL=ws://127.0.0.1:5225
SIMPLEX_ALLOWED_USERS=<contact-id-1>,<contact-id-2>
SIMPLEX_HOME_CHANNEL=<contact-id>
```

| Variable | Required | Description |
|---|---|---|
| `SIMPLEX_WS_URL` | Yes | WebSocket URL of the simplex-chat daemon |
| `SIMPLEX_ALLOWED_USERS` | Recommended | Comma-separated contact IDs allowed to use the agent |
| `SIMPLEX_ALLOW_ALL_USERS` | Optional | Set `true` to allow every contact (use carefully) |
| `SIMPLEX_HOME_CHANNEL` | Optional | Default contact ID for cron job delivery |
| `SIMPLEX_HOME_CHANNEL_NAME` | Optional | Human label for the home channel |

## Find your contact ID

After starting the daemon, open a conversation with your agent contact. The contact ID will appear in session logs or via `hermes send_message action=list`.

You can also enumerate everything the daemon already knows about with:

```bash
hermes simplex list
```

This prints the active user, every contact (with numeric `contactId`), and every joined group (with numeric `groupId`). Copy IDs from the output into `SIMPLEX_HOME_CHANNEL` (for cron-delivery defaults) or `SIMPLEX_ALLOWED_USERS` (the allowlist).

To join a new group via an invitation link without leaving the terminal:

```bash
hermes simplex join "https://simplex.chat/contact#..."
```

The command polls until the new group materialises (default 120s), then prints the new `groupId`. Both commands honour `SIMPLEX_WS_URL` from `~/.hermes/.env`; override per-invocation with `--ws-url`.

## Authorization

By default **all contacts are denied**. You must either:

1. Set `SIMPLEX_ALLOWED_USERS` to a comma-separated list of contact IDs, or
2. Use **DM pairing** — send any message to the bot and it will reply with a pairing code. Enter that code via `hermes gateway pair`.

## Using SimpleX with cron jobs

```python
cronjob(
    action="create",
    schedule="every 1h",
    deliver="simplex",          # uses SIMPLEX_HOME_CHANNEL
    prompt="Check for alerts and summarise."
)
```

Or target a specific contact:

```python
send_message(target="simplex:<contact-id>", message="Done!")
```

## Privacy notes

- SimpleX never reveals phone numbers or email addresses — contacts use opaque IDs
- The connection between Hermes and the daemon is local WebSocket (`ws://127.0.0.1:5225`) — no data leaves your machine
- Messages are end-to-end encrypted by the SimpleX protocol before reaching the daemon

## Troubleshooting

**"Cannot reach daemon"** — Ensure `simplex-chat -p 5225` is running and the port matches `SIMPLEX_WS_URL`.

**"websockets not installed"** — Run `pip install websockets`.

**Messages not received** — Check that the contact's ID is in `SIMPLEX_ALLOWED_USERS` or approve them via DM pairing.
