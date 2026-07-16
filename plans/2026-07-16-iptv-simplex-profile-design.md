# iptv SimpleX profile — design

**Date:** 2026-07-16
**Branch:** feat/simplex-0.16
**Author:** Brandon Jones (with Claude)
**Status:** Approved design — ready for implementation plan

## Goal

Give the hermes-agent **`iptv` profile** its own SimpleX presence so it can send and
receive messages on channels dedicated to it — independent of the existing `default`
profile's SimpleX identity. Do it on the **ai-server only** (`hermes-ai` →
`ai-server.localdomain`), reusing the proven patterns already running there, for low
long-term maintenance. Also reconcile config drift and an authorization exposure found
on the `default` profile.

Two channels are ultimately wanted for iptv:

1. **Admin channel** — a SimpleX group where the operator (Brandon) directs the iptv
   agent with full command access.
2. **Notify channel** — a SimpleX group for a subset of users who receive on-demand
   notifications about iptv-system events and can send a *constrained* set of
   replies/commands ("receive + limited replies").

The notify channel's per-user command scoping needs **new adapter code** and is
deliberately deferred to Phase 2 (see below). Phase 1 delivers the iptv identity and
the admin channel only.

## Key facts driving the design (verified in code + on the server)

- `hermes --profile iptv gateway run` is supported. `--profile` is a global pre-argparse
  flag that re-roots `HERMES_HOME` to `~/.hermes/profiles/iptv/` (own `config.yaml`,
  `.env`, gateway PID). One gateway per profile is fine.
  (`hermes_cli/_parser.py:15-23`, `hermes_cli/profiles.py`, `hermes_cli/gateway.py:624-651`.)
- **One WS URL = one simplex-chat daemon = one SimpleX identity.** A separate identity
  requires a separate daemon (new data volume) on a new port. The adapter connects to a
  single `ws_url` (`plugins/platforms/simplex/adapter.py:393`).
- **Home channel is only a default outbound target** (cron/notify delivery), NOT a
  permission concept (`gateway/config.py:776-781`).
- **Inbound authorization** lives in core `_is_user_authorized`
  (`gateway/authz_mixin.py:264`): `ALLOW_ALL` → env allowlist → pairing store → global →
  deny. For SimpleX groups, `sender_id = "group:<id>"` (whole-group granularity;
  per-member gating is not wired for SimpleX today) (`adapter.py:742-786`).
- `SIMPLEX_GROUP_ALLOWED` and `SIMPLEX_AUTO_ACCEPT` are declared in `plugin.yaml` but
  **not consumed anywhere** — dead knobs. `SIMPLEX_GROUP_IDS` is not read by current code.
- Config precedence: the live adapter is driven primarily by the profile's `.env`
  (`SIMPLEX_WS_URL`, `SIMPLEX_HOME_CHANNEL[_NAME]`, `SIMPLEX_ALLOWED_USERS`,
  `SIMPLEX_ALLOW_ALL_USERS`); `config.yaml`'s `platforms.simplex` only needs
  `enabled: true`.

### Existing server layout (for cloning)

- Daemon: Podman **Quadlet** unit from
  `~/.config/containers/systemd/simplex-chat-hermes.container`, image
  `simplex-chat-hermes:latest`, volume `simplex-chat-hermes-data:/root/.simplex`, files
  mount `~/.hermes/cache/simplex-files`, `PublishPort 127.0.0.1:5225:5225`, env file
  `simplex-chat-hermes.env` (holds `SMP_SERVER`, `XFTP_SERVER` pointing at
  `simplex.alt255.casa` / `xftp.alt255.casa`). Image entrypoint runs simplex-chat on an
  internal port and socat-exposes 5225 inside the container, so no `-p` is passed.
- Gateway: systemd `--user` unit `hermes-gateway.service`
  (`ExecStart=…/.venv/bin/python -m hermes_cli.main gateway run`, `Restart=always`,
  cgroup cleanup in `ExecStopPost`, `WantedBy=default.target`).
- Serve profiles already use the pattern: `hermes-serve-iptv.service` runs
  `… main --profile=iptv serve --host 0.0.0.0 --port 9120`.

## Phase 1 — iptv SimpleX stack (this pass)

### 1. New daemon container (new identity)

Create `~/.config/containers/systemd/simplex-chat-iptv.container` as a clone of
`simplex-chat-hermes.container` with these differences:

| Setting | default (existing) | iptv (new) |
|---|---|---|
| `ContainerName` | `simplex-chat-hermes` | `simplex-chat-iptv` |
| Data volume | `simplex-chat-hermes-data` | **`simplex-chat-iptv-data`** (→ new identity) |
| Files mount | `%h/.hermes/cache/simplex-files` | `%h/.hermes/cache/simplex-iptv-files` |
| `PublishPort` | `127.0.0.1:5225:5225` | **`127.0.0.1:5226:5225`** |
| `--create-bot-display-name` | `hermes` | `hermes-iptv` |
| Image | `simplex-chat-hermes:latest` | reuse the same image |
| `EnvironmentFile` | `simplex-chat-hermes.env` | new `simplex-chat-iptv.env` (copy SMP/XFTP) |

Then: create the files-cache dir, `systemctl --user daemon-reload`,
`systemctl --user start simplex-chat-iptv.service`, confirm it is listening on
`127.0.0.1:5226`.

### 2. iptv profile `.env` (`~/.hermes/profiles/iptv/.env` — already exists)

Read the current file first; it may carry stale/templated SimpleX values from a prior
copy — overwrite only the SimpleX keys. Target state:

```
SIMPLEX_WS_URL=ws://localhost:5226
SIMPLEX_HOME_CHANNEL=group:<admin-group-id>
SIMPLEX_HOME_CHANNEL_NAME=<admin channel display name>
SIMPLEX_ALLOWED_USERS=group:<admin-group-id>
SIMPLEX_ALLOW_ALL_USERS=false
```

(Home channel + allowlist are filled in after step 4 yields the group id.) Ensure
`config.yaml` under this profile has `platforms.simplex.enabled: true` (it does).

### 3. New gateway unit `hermes-gateway-iptv.service`

Clone `hermes-gateway.service`; change only the command to
`… -m hermes_cli.main --profile iptv gateway run` and mirror how `hermes-serve-iptv`
handles `HERMES_HOME`/`--profile` (keep `Restart=always`, `RestartSec`, `KillMode`,
`ExecStopPost` cgroup cleanup, `WantedBy=default.target`). Do **not** enable
`gateway.multiplex_profiles` — dedicated gateways were chosen for isolation.

### 4. Admin-channel pairing (interactive, operator-in-the-loop)

1. Start the iptv daemon (step 1).
2. From Brandon's SimpleX phone app, connect to the `hermes-iptv` bot (obtain the bot's
   address/QR from the daemon), then create a SimpleX **group**, add the bot to it.
3. Read the group id back with `hermes --profile iptv simplex list` (fork CLI from
   `feat/simplex-cli-discovery`).
4. Put `group:<id>` into the iptv `.env` for `SIMPLEX_HOME_CHANNEL` and
   `SIMPLEX_ALLOWED_USERS` (step 2).
5. `systemctl --user enable --now hermes-gateway-iptv.service`; verify a startup
   notification lands in the admin group and that a message from Brandon is authorized
   and acted on.

### Acceptance criteria (Phase 1)

- `simplex-chat-iptv` container running, listening on `127.0.0.1:5226`, separate identity.
- `hermes-gateway-iptv.service` active, `simplex` state `connected` in the iptv
  profile's `gateway_state.json`.
- Brandon can send a message in the admin group and the iptv agent responds; the agent
  can post to the admin group. The `default` profile and its daemon are unaffected.

## Default-profile reconcile (this pass)

Performed carefully so the operator is never locked out.

1. **Remove dead `SIMPLEX_GROUP_IDS`** from `~/.hermes/.env` (not read by current code).
2. **Close the allow-all exposure.** Live `.env` has `SIMPLEX_ALLOW_ALL_USERS=true`,
   which lets *any* user who messages the bot command it; the `SIMPLEX_ALLOWED_USERS`
   allowlist (currently `Brandon,<id>` — the plain name `Brandon` almost certainly does
   not match the real sender-id format) is effectively bypassed. Sequence:
   1. Capture Brandon's **real** SimpleX sender-id (from the gateway/agent logs or the
      daemon) and the `hermes-agent` home group id (`group:1`).
   2. Set `SIMPLEX_ALLOWED_USERS` to the real id(s) + `group:1`; drop the non-matching
      `Brandon` token.
   3. Verify Brandon is still authorized **with `allow_all` still true**.
   4. Only then set `SIMPLEX_ALLOW_ALL_USERS=false` and restart `hermes-gateway`;
      re-verify Brandon still has access and an unknown user is denied.
3. Keep the working `SIMPLEX_HOME_CHANNEL` value (default home channel `simplex:1`).

### Acceptance criteria (reconcile)

- `default` `.env` free of dead vars; `allow_all=false`; Brandon retains access; an
  un-allowlisted user is denied. Gateway reconnects cleanly.

## Phase 2 — notify-channel limited-replies (doc-only this pass)

Deliverable: a **context/design brief** (separate doc) that Brandon can feed to the iptv
hermes-agent so the agent can propose its own implementation. The brief must contain:

- The requirement: a notify SimpleX group for a subset of users; **receive + limited
  replies** (a constrained command surface, not full control).
- The exact code seams and the gap:
  - SimpleX authorizes groups whole (`sender_id = group:<id>`); per-member gating is not
    wired (`adapter.py:742-786`).
  - Core already has per-group `allow_from` / `group_policy` machinery used by WeCom
    (`gateway/authz_mixin.py:183-247`) that the SimpleX adapter does **not** populate.
  - Commit history shows group-member extraction exists
    (`fix(simplex): extract group sender from chatDir.groupMember`) — worth confirming
    whether real member ids are available to feed per-member authz.
  - `SIMPLEX_GROUP_ALLOWED` is declared-but-unused and could be repurposed.
- The `simplex-bridge` option (`github.com/brandon-btcgroup/simplex-bridge`): a
  multi-client auth + group-scoping + offline-buffering WS layer whose example config
  already references `iptv-admin` / `iptv-notifications`. Note its trade-offs — separate
  protocol (needs a new hermes bridge-client adapter), second server, and it does **not**
  itself provide per-member command scoping (that stays hermes policy). Included so the
  agent can weigh it honestly.
- Constraints: low maintenance, prefer ai-server-only, consistent with the group-based
  channel model chosen in Phase 1.

## Out of scope

- Any notify-channel implementation code (Phase 2 is a brief only).
- Multiplexing gateway; simplex-bridge integration; changes to other profiles.

## Open items to resolve during implementation

- Read the existing 16 KB iptv `.env` before editing; overwrite only SimpleX keys.
- Confirm exact `--profile`/`HERMES_HOME` handling for the gateway unit by mirroring the
  working `hermes-serve-iptv.service`.
- Determine how Brandon connects to the `hermes-iptv` bot (address/QR retrieval from the
  fresh daemon) for the pairing step.
