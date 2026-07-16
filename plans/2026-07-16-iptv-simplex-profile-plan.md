# iptv SimpleX Profile Implementation Plan

> **For agentic workers:** This is an operations plan executed against a live server
> (`hermes-ai` → `ai-server.localdomain`) over SSH, with one operator-in-the-loop step.
> Steps use checkbox (`- [ ]`) syntax. Server-side files (Quadlet units, systemd units,
> profile `.env`) are NOT in the git repo — there is nothing to `git commit` for those;
> each task instead ends with a verification command + expected output. Only Task 6 (the
> Phase 2 brief) is a repo file and gets committed.

**Goal:** Give the hermes-agent `iptv` profile its own SimpleX identity + admin channel
via a dedicated daemon and gateway on the ai-server, and reconcile the `default`
profile's config drift + allow-all exposure.

**Architecture:** Clone the proven `simplex-chat-hermes` Quadlet daemon and
`hermes-gateway` systemd unit for the `iptv` profile — a second simplex-chat daemon
(new data volume = new identity) on `127.0.0.1:5226`, and `hermes --profile=iptv gateway
run`. Admin channel is a SimpleX group. No multiplexing gateway; no simplex-bridge.

**Tech Stack:** Podman Quadlet (`.container` units), systemd `--user` services,
simplex-chat daemon, hermes_cli, SimpleX self-hosted SMP/XFTP relays (`*.alt255.casa`).

## Global Constraints

- All work on host `hermes-ai` (`ssh hermes-ai`), user `ai-admin`, `%h` =
  `/var/home/ai-admin`. `~/.hermes` = base (default profile) `HERMES_HOME`;
  `~/.hermes/profiles/iptv` = iptv profile `HERMES_HOME`.
- Daemon WS protocol has NO auth — every published port stays bound to `127.0.0.1` only.
- Reuse the existing image `simplex-chat-hermes:latest` and the existing SMP/XFTP relay
  values; do not create new relays.
- iptv daemon: host port **5226**, container port 5225, data volume
  **`simplex-chat-iptv-data`**, bot display name **`hermes-iptv`**.
- Never break the running `default` profile (`simplex-chat-hermes` on 5225,
  `hermes-gateway.service`). Reconcile it only with the careful lock-out-proof sequence
  in Task 5.
- When editing any existing `.env`, read it first and change ONLY the named keys.
- systemd is user-scoped: use `systemctl --user …` and `journalctl --user -u …`.

---

## File / unit map (all on `hermes-ai` unless noted)

- Create: `~/.config/containers/systemd/simplex-chat-iptv.container` — iptv daemon Quadlet unit
- Create: `~/.config/containers/systemd/simplex-chat-iptv.env` — iptv daemon relay env (SMP/XFTP)
- Create: `~/.hermes/cache/simplex-iptv-files/` — iptv daemon file store (bind mount)
- Create: `~/.config/systemd/user/hermes-gateway-iptv.service` — iptv gateway unit
- Modify: `~/.hermes/profiles/iptv/.env` — iptv profile SimpleX keys
- Modify: `~/.hermes/.env` — default profile reconcile (Task 5)
- Create (repo): `plans/2026-07-16-iptv-notify-channel-brief.md` — Phase 2 brief (Task 6)

---

## Task 1: Stand up the iptv simplex-chat daemon

**Files:**
- Create: `~/.config/containers/systemd/simplex-chat-iptv.container`
- Create: `~/.config/containers/systemd/simplex-chat-iptv.env`
- Create: `~/.hermes/cache/simplex-iptv-files/`

**Interfaces:**
- Produces: a simplex-chat daemon listening on `127.0.0.1:5226` with a fresh SimpleX
  identity (bot display name `hermes-iptv`), consumed by Task 2 (pairing) and Task 4
  (gateway `SIMPLEX_WS_URL=ws://localhost:5226`).

- [ ] **Step 1: Copy the relay env from the working default daemon**

```bash
ssh hermes-ai 'cp ~/.config/containers/systemd/simplex-chat-hermes.env \
  ~/.config/containers/systemd/simplex-chat-iptv.env && \
  grep -c SMP_SERVER ~/.config/containers/systemd/simplex-chat-iptv.env'
```
Expected: prints `1` (SMP_SERVER present; XFTP_SERVER carried over too).

- [ ] **Step 2: Create the files-cache directory**

```bash
ssh hermes-ai 'mkdir -p ~/.hermes/cache/simplex-iptv-files && \
  ls -ld ~/.hermes/cache/simplex-iptv-files'
```
Expected: directory exists, owned by `ai-admin`.

- [ ] **Step 3: Write the iptv Quadlet `.container` unit**

Write this file on the server (heredoc, no shell expansion). It is the
`simplex-chat-hermes.container` with the 5 documented changes (name, volume, files
mount, port, display name):

```bash
ssh hermes-ai 'cat > ~/.config/containers/systemd/simplex-chat-iptv.container' <<'UNIT'
[Unit]
Description=simplex-chat daemon (dedicated to Hermes iptv profile)
After=network-online.target
Wants=network-online.target

[Container]
Image=localhost/simplex-chat-hermes:latest
ContainerName=simplex-chat-iptv
Volume=%h/.hermes/cache/simplex-iptv-files:/root/.simplex/files:Z

# Separate named volume => separate SimpleX identity / keys / DB.
Volume=simplex-chat-iptv-data:/root/.simplex

# Loopback only — simplex-chat's WS protocol has NO authentication.
PublishPort=127.0.0.1:5226:5225

EnvironmentFile=%h/.config/containers/systemd/simplex-chat-iptv.env

Exec=--create-bot-display-name=hermes-iptv --create-bot-allow-files -s ${SMP_SERVER} --auto-accept-files 52428800 --files-folder /root/.simplex/files --temp-folder /root/.simplex/files/.xftp-tmp -l info

[Service]
Restart=on-failure
TimeoutStartSec=900
EnvironmentFile=%h/.config/containers/systemd/simplex-chat-iptv.env

[Install]
WantedBy=default.target
UNIT
```
Then confirm it wrote:
```bash
ssh hermes-ai 'grep -E "ContainerName|PublishPort|iptv-data|display-name" ~/.config/containers/systemd/simplex-chat-iptv.container'
```
Expected: shows `ContainerName=simplex-chat-iptv`, `PublishPort=127.0.0.1:5226:5225`,
`simplex-chat-iptv-data`, `--create-bot-display-name=hermes-iptv`.

- [ ] **Step 4: Reload systemd and start the daemon**

```bash
ssh hermes-ai 'systemctl --user daemon-reload && \
  systemctl --user start simplex-chat-iptv.service && sleep 5 && \
  systemctl --user is-active simplex-chat-iptv.service'
```
Expected: `active`.

- [ ] **Step 5: Verify it is listening on 5226 with a fresh identity**

```bash
ssh hermes-ai 'ss -tlnp | grep 127.0.0.1:5226; \
  podman ps --filter name=simplex-chat-iptv --format "{{.Names}} {{.Status}}"; \
  podman volume ls | grep simplex-chat-iptv-data'
```
Expected: a LISTEN line on `127.0.0.1:5226`; `simplex-chat-iptv Up …`; the
`simplex-chat-iptv-data` volume exists. If it fails to start, inspect with
`ssh hermes-ai 'journalctl --user -u simplex-chat-iptv.service --no-pager -n 40'`.

---

## Task 2: Pair the operator with the iptv bot and create the admin group

**This is operator-in-the-loop (Brandon + SimpleX phone app). Do not automate the phone side.**

**Interfaces:**
- Consumes: the running daemon from Task 1 (`ws://localhost:5226`).
- Produces: an admin SimpleX **group** containing Brandon + the `hermes-iptv` bot, whose
  numeric group id is discovered in Task 3.

- [ ] **Step 1: Retrieve the bot's connection address**

Get the fresh bot's own SimpleX address to share to the phone. Try the fork CLI first:
```bash
ssh hermes-ai 'cd ~/.hermes/hermes-agent && \
  SIMPLEX_WS_URL=ws://localhost:5226 .venv/bin/python -m hermes_cli.main --profile=iptv simplex list 2>&1 | head -30'
```
Expected: the CLI connects to `ws://localhost:5226` (may show no groups yet — that's
fine; we need it to confirm connectivity). If the CLI cannot print an address, obtain it
directly from the daemon:
```bash
ssh hermes-ai 'command -v websocat >/dev/null && \
  (echo "/show_address" ; sleep 2) | websocat ws://127.0.0.1:5226 2>/dev/null | head || \
  echo "NO websocat — retrieve address via SimpleX CLI /address command"'
```
Expected: a `simplex:/…` address string, OR a clear signal to fall back. Record the
address. (If no address exists yet, run the daemon's `/address` create command via the
same WS.)

- [ ] **Step 2 (operator): Connect from the phone and form the group**

Brandon, in the SimpleX mobile app:
1. Add the `hermes-iptv` bot using the address from Step 1 (accept the connection).
2. Create a new SimpleX **group** (e.g. name it `iptv-admin`).
3. Add the `hermes-iptv` bot to that group; wait for it to join/confirm.

Confirm the bot accepted the contact:
```bash
ssh hermes-ai 'journalctl --user -u simplex-chat-iptv.service --no-pager -n 30 | grep -iE "contact|group|accepted|joined"'
```
Expected: log lines showing the new contact and group membership.

---

## Task 3: Configure the iptv profile `.env`

**Files:**
- Modify: `~/.hermes/profiles/iptv/.env` (exists; ~16 KB — change only SimpleX keys)

**Interfaces:**
- Consumes: the admin group from Task 2.
- Produces: iptv profile SimpleX config consumed by the gateway in Task 4.

- [ ] **Step 1: Discover the admin group id**

```bash
ssh hermes-ai 'cd ~/.hermes/hermes-agent && \
  .venv/bin/python -m hermes_cli.main --profile=iptv simplex list 2>&1 | head -40'
```
Expected: a listing that includes the `iptv-admin` group with a numeric id. Record it as
`<GID>` (the chat id is `group:<GID>`).

- [ ] **Step 2: Back up and inspect the current iptv `.env` SimpleX keys**

```bash
ssh hermes-ai 'cp ~/.hermes/profiles/iptv/.env ~/.hermes/profiles/iptv/.env.bak-20260716 && \
  grep -nE "^SIMPLEX_" ~/.hermes/profiles/iptv/.env || echo "(no SIMPLEX_ keys yet)"'
```
Expected: a backup is made; existing `SIMPLEX_*` lines (if any) are listed so we know
which to replace vs append.

- [ ] **Step 3: Set the SimpleX keys (replace-in-place or append)**

Run this idempotent updater (replaces each key if present, else appends). Substitute the
real `<GID>` and a display name:
```bash
ssh hermes-ai 'F=~/.hermes/profiles/iptv/.env; \
  set_kv() { grep -q "^$1=" "$F" && sed -i "s|^$1=.*|$1=$2|" "$F" || echo "$1=$2" >> "$F"; }; \
  set_kv SIMPLEX_WS_URL "ws://localhost:5226"; \
  set_kv SIMPLEX_HOME_CHANNEL "group:<GID>"; \
  set_kv SIMPLEX_HOME_CHANNEL_NAME "iptv-admin"; \
  set_kv SIMPLEX_ALLOWED_USERS "group:<GID>"; \
  set_kv SIMPLEX_ALLOW_ALL_USERS "false"; \
  grep -E "^SIMPLEX_" "$F"'
```
Expected: the five keys print with the intended values; no duplicate keys.

- [ ] **Step 4: Confirm the profile enables the simplex platform**

```bash
ssh hermes-ai 'grep -A3 -iE "^\s*simplex:" ~/.hermes/profiles/iptv/config.yaml | head'
```
Expected: `simplex:` block with `enabled: true`. If missing, add
`platforms:\n  simplex:\n    enabled: true` to that `config.yaml`.

---

## Task 4: Create and start the iptv gateway service

**Files:**
- Create: `~/.config/systemd/user/hermes-gateway-iptv.service`

**Interfaces:**
- Consumes: iptv daemon (Task 1), iptv `.env` (Task 3).
- Produces: a running `hermes --profile=iptv gateway run` process bridging SimpleX to
  the iptv agent.

- [ ] **Step 1: Write the gateway unit (clone of `hermes-gateway.service`)**

```bash
ssh hermes-ai 'cat > ~/.config/systemd/user/hermes-gateway-iptv.service' <<'UNIT'
[Unit]
Description=Hermes Agent Gateway - iptv profile (Messaging Platform Integration)
After=network-online.target simplex-chat-iptv.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/home/ai-admin/.hermes/hermes-agent/.venv/bin/python -m hermes_cli.main --profile=iptv gateway run
WorkingDirectory=/var/home/ai-admin/.hermes
Environment="PATH=/var/home/ai-admin/.hermes/hermes-agent/venv/bin:/var/home/ai-admin/.hermes/hermes-agent/node_modules/.bin:/home/ai-admin/.nvm/versions/node/v25.9.0/bin:/home/ai-admin/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="VIRTUAL_ENV=/home/ai-admin/.hermes/hermes-agent/.venv"
Environment="HERMES_HOME=/var/home/ai-admin/.hermes"
Restart=always
RestartSec=5
KillMode=mixed
KillSignal=SIGTERM
ExecStopPost=-/home/ai-admin/.hermes/hermes-agent/.venv/bin/python -m gateway.cgroup_cleanup
TimeoutStopSec=90
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
UNIT
```
Confirm:
```bash
ssh hermes-ai 'grep -E "ExecStart|--profile" ~/.config/systemd/user/hermes-gateway-iptv.service'
```
Expected: `ExecStart=…python -m hermes_cli.main --profile=iptv gateway run`.

- [ ] **Step 2: Reload, enable and start**

```bash
ssh hermes-ai 'systemctl --user daemon-reload && \
  systemctl --user enable --now hermes-gateway-iptv.service && sleep 8 && \
  systemctl --user is-active hermes-gateway-iptv.service'
```
Expected: `active`.

- [ ] **Step 3: Verify SimpleX connected on the iptv profile**

```bash
ssh hermes-ai '.hermes/hermes-agent/.venv/bin/python -c "import json;d=json.load(open(\"/var/home/ai-admin/.hermes/profiles/iptv/gateway_state.json\"));print(d[\"platforms\"].get(\"simplex\"))"'
```
Expected: a dict with `"state": "connected"`. Also check the log:
```bash
ssh hermes-ai 'journalctl --user -u hermes-gateway-iptv.service --no-pager -n 30 | grep -iE "simplex|connected|home"'
```
Expected: `SimpleX: connected to ws://localhost:5226` and a home-channel startup line.

- [ ] **Step 4 (operator): End-to-end message check**

Brandon sends a message in the `iptv-admin` group. Then:
```bash
ssh hermes-ai 'journalctl --user -u hermes-gateway-iptv.service --no-pager -n 40 | grep -iE "message|authorized|agent|reply|denied"'
```
Expected: the inbound message is received and authorized (NOT denied), and the iptv agent
processes it / replies in the group. **Task acceptance: Brandon can two-way chat with the
iptv profile in the admin group; the default profile is unaffected** (verify with
`ssh hermes-ai 'systemctl --user is-active hermes-gateway.service simplex-chat-hermes.service'`
→ both `active`).

---

## Task 5: Reconcile the default profile `.env`

**Files:**
- Modify: `~/.hermes/.env`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: a drift-free, locked-down default profile config. Independent of Tasks 1-4.

- [ ] **Step 1: Back up and capture Brandon's real SimpleX sender-id**

```bash
ssh hermes-ai 'cp ~/.hermes/.env ~/.hermes/.env.bak-20260716; \
  grep -iE "authorized|sender|from .*simplex|user=" ~/.hermes/logs/agent.log | tail -30'
```
Expected: a backup is made; log lines reveal the real inbound `sender_id` for Brandon
(contact id and/or `group:1`). Record the exact value(s) as `<BRANDON_ID>`. If the log
does not show it, temporarily raise verbosity / send a test DM and re-check before
proceeding.

- [ ] **Step 2: Remove the dead var and set a correct allowlist (allow_all STILL true)**

```bash
ssh hermes-ai 'F=~/.hermes/.env; \
  sed -i "/^SIMPLEX_GROUP_IDS=/d" "$F"; \
  sed -i "s|^SIMPLEX_ALLOWED_USERS=.*|SIMPLEX_ALLOWED_USERS=<BRANDON_ID>,group:1|" "$F"; \
  grep -E "^SIMPLEX_(GROUP_IDS|ALLOWED_USERS|ALLOW_ALL_USERS)" "$F"'
```
Expected: no `SIMPLEX_GROUP_IDS` line; `SIMPLEX_ALLOWED_USERS` now holds the real id +
`group:1`; `SIMPLEX_ALLOW_ALL_USERS=true` still present (unchanged for now).

- [ ] **Step 3: Restart and verify Brandon is authorized via the allowlist**

Restart with allow_all still true, then confirm access works:
```bash
ssh hermes-ai 'systemctl --user restart hermes-gateway.service && sleep 8 && \
  systemctl --user is-active hermes-gateway.service'
```
Expected: `active`. Brandon sends a test message to the default bot; confirm it is
handled:
```bash
ssh hermes-ai 'journalctl --user -u hermes-gateway.service --no-pager -n 20 | grep -iE "authorized|message|denied"'
```
Expected: message authorized/handled (not denied).

- [ ] **Step 4: Flip allow_all off and verify lock-down**

```bash
ssh hermes-ai 'sed -i "s|^SIMPLEX_ALLOW_ALL_USERS=.*|SIMPLEX_ALLOW_ALL_USERS=false|" ~/.hermes/.env && \
  systemctl --user restart hermes-gateway.service && sleep 8 && \
  grep ^SIMPLEX_ALLOW_ALL_USERS ~/.hermes/.env'
```
Expected: `SIMPLEX_ALLOW_ALL_USERS=false`. Brandon sends another test message → still
handled. **Task acceptance:** Brandon retains access with `allow_all=false`; an
un-allowlisted contact is denied (verify from logs the next time an unknown sender
appears, or with a second test identity if available). If Brandon is wrongly locked out,
restore instantly: `ssh hermes-ai 'cp ~/.hermes/.env.bak-20260716 ~/.hermes/.env && systemctl --user restart hermes-gateway.service'`.

---

## Task 6: Write the Phase 2 notify-channel brief (repo doc)

**Files:**
- Create (repo): `plans/2026-07-16-iptv-notify-channel-brief.md`

**Interfaces:**
- Consumes: the live Phase 1 setup (reference the real admin group + daemon so the brief
  is concrete).
- Produces: a self-contained brief Brandon feeds to the iptv hermes-agent.

- [ ] **Step 1: Write the brief**

Create `plans/2026-07-16-iptv-notify-channel-brief.md` containing, at minimum:
- The requirement: a second SimpleX **group** (`iptv-notify`) for a subset of users;
  they **receive** iptv-system notifications and may send a **limited** set of
  commands/queries — not full control.
- The current gap (with file:line seams): SimpleX authorizes groups whole
  (`plugins/platforms/simplex/adapter.py:742-786`, `sender_id=group:<id>`); core has
  WeCom-style per-group `allow_from`/`group_policy` (`gateway/authz_mixin.py:183-247`)
  that the SimpleX adapter does not populate; group-member extraction exists in history
  (`fix(simplex): extract group sender from chatDir.groupMember`) — confirm whether real
  per-member ids are available; `SIMPLEX_GROUP_ALLOWED` is declared-but-unused
  (`plugin.yaml`).
- The `simplex-bridge` option and its honest trade-offs (separate WS protocol → new
  hermes adapter; second server; does NOT itself provide per-member command scoping).
- Constraints: low maintenance, ai-server-first, consistent with the group-based Phase 1
  admin channel; the live admin group is `group:<GID>` on daemon `ws://localhost:5226`.
- An explicit ask to the agent: "propose 2-3 implementation options with trade-offs and a
  recommendation; do not write code yet."

- [ ] **Step 2: Commit the brief**

```bash
cd /Users/brandonjones/Development/hermes-agent && \
git add plans/2026-07-16-iptv-notify-channel-brief.md && \
git commit -m "docs(iptv): Phase 2 brief — notify-channel limited-replies for iptv agent

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```
Expected: commit succeeds on `feat/simplex-0.16`.

---

## Self-review notes

- **Spec coverage:** Task 1 = daemon; Tasks 2-4 = iptv profile `.env` + gateway + admin
  group (Phase 1); Task 5 = default reconcile incl. careful allow-all lockdown; Task 6 =
  Phase 2 brief. All spec sections mapped.
- **Known risks / open items:** (a) bot-address retrieval in Task 2 Step 1 may need the
  daemon's native `/address` command if `websocat`/CLI paths don't yield it — resolve
  live. (b) `ExecStopPost=… cgroup_cleanup` in the iptv unit runs against base
  `HERMES_HOME`; harmless but not iptv-scoped — acceptable, note for future. (c) Task 5
  Step 4 "unknown sender denied" is best-effort to verify without a second identity.
