# ai-server — architecture reference

> Living document. Snapshot of what runs on the Hermes **ai-server** so future work
> starts with context instead of re-discovery. Update it whenever the topology changes.
>
> **Last verified:** 2026-07-16 (live inspection over SSH).
> **Legend:** ✅ live &nbsp;·&nbsp; 🟡 planned/in-progress (see
> `plans/2026-07-16-iptv-simplex-profile-plan.md`).

## Host

| Item | Value |
|---|---|
| SSH alias | `hermes-ai` → `ai-server.localdomain` (see `~/.ssh/config`) |
| OS | Fedora (kernel 6.17.x), `x86_64` |
| Service user | `ai-admin` (home `/var/home/ai-admin`, `%h`; `/home` symlinks to `/var/home`) |
| Hermes install | `~/.hermes/hermes-agent` (Python `.venv`), `HERMES_HOME=~/.hermes` |
| Orchestration | systemd **user** units (`systemctl --user …`) + Podman **Quadlet** |

## Deployment & code integrity ⚠️

- **Code lives at** `~/.hermes/hermes-agent`, **editable uv install** (`.venv`). The
  running Python processes import from disk at start — a `git checkout` does **not**
  affect a running process until it is restarted.
- **Deploy branch:** `feat/simplex-0.16` (the fork). Both it and local `main` now track
  **`fork/*`** (`fork` = `brandon-btcgroup`). Reinstall after a code change with:
  `cd ~/.hermes/hermes-agent && uv sync --extra simplex --extra homeassistant`, then
  restart the affected services.
- **Required extras:** `simplex` (websockets) + `homeassistant` (`aiohttp`). A plain
  `uv sync` (no extras) **drops `aiohttp` and breaks Home Assistant.** telegram/slack/etc.
  lazy-install on first use and are not needed here.
- **Footgun (fixed 2026-07-16):** local `main` used to track **upstream** (`origin` =
  NousResearch). On 2026-07-12 a `checkout main; pull --ff-only origin main` silently
  reverted the box to upstream's SimpleX plugin; it ran upstream — not the fork — until
  2026-07-16. `main` has been repointed to `fork/main` so this can't recur. Verify the
  live code is the fork: the SimpleX adapter should show many
  `TEXT_BATCH_DELAY|replay|groupMember` markers (fork ≈ dozens; upstream ≈ 3), and
  `hermes simplex --help` must list `{list,join}`.

## System overview

```mermaid
flowchart TB
    subgraph phones["Operators / users (SimpleX mobile)"]
        admin["Brandon (admin)"]
        users["iptv notify users 🟡"]
    end

    subgraph relays["Self-hosted SimpleX relays (alt255.casa)"]
        smp["SMP relay<br/>simplex.alt255.casa:5223"]
        xftp["XFTP file relay<br/>xftp.alt255.casa:5225"]
    end

    subgraph server["ai-server (ai-admin, systemd --user)"]
        subgraph dmn["simplex-chat daemons (Podman Quadlet, loopback only)"]
            d1["simplex-chat-hermes ✅<br/>127.0.0.1:5225 · identity 'hermes'"]
            d2["simplex-chat-iptv 🟡<br/>127.0.0.1:5226 · identity 'hermes-iptv'"]
        end
        subgraph gw["Gateways (messaging bridge)"]
            g1["hermes-gateway ✅<br/>profile: default"]
            g2["hermes-gateway-iptv 🟡<br/>profile: iptv"]
        end
        subgraph srv["Desktop backends (hermes serve)"]
            s1["hermes-serve ✅<br/>default · :9119"]
            s2["hermes-serve-iptv ✅<br/>iptv · :9120"]
        end
        ha["Home Assistant platform ✅"]
    end

    admin -->|"admin group (simplex:1)"| smp
    users -.->|"iptv-notify group 🟡"| smp
    smp <--> d1
    smp <--> d2
    xftp <--> d1
    xftp <--> d2
    d1 <-->|"ws://localhost:5225"| g1
    d2 -.->|"ws://localhost:5226"| g2
    g1 --- ha
```

**Read it as:** operators talk to the bots over SimpleX; the self-hosted SMP/XFTP relays
carry the encrypted traffic; each `simplex-chat` daemon is one SimpleX identity, exposed
on loopback only; a per-profile **gateway** consumes a daemon and bridges messages to the
Hermes agent for that profile. `hermes serve` backends are the Desktop-app API, separate
from messaging.

## systemd `--user` services

| Unit | Status | Role | Key detail |
|---|---|---|---|
| `simplex-chat-hermes.service` | ✅ | SimpleX daemon, default identity | Quadlet; `127.0.0.1:5225`; volume `simplex-chat-hermes-data` |
| `hermes-gateway.service` | ✅ | Messaging gateway, **default** profile | `hermes_cli.main gateway run`; `Restart=always` |
| `hermes-serve.service` | ✅ | Desktop backend, default | `serve --host 0.0.0.0 --port 9119` |
| `hermes-serve-iptv.service` | ✅ | Desktop backend, iptv | `--profile=iptv serve … --port 9120` |
| `simplex-chat-iptv.service` | 🟡 | SimpleX daemon, iptv identity | Quadlet; `127.0.0.1:5226`; volume `simplex-chat-iptv-data` |
| `hermes-gateway-iptv.service` | 🟡 | Messaging gateway, **iptv** profile | `--profile=iptv gateway run` |

> Gateways are **not** multiplexed — one gateway per profile, deliberately, for isolation.

## Ports (all loopback / localhost)

| Port | Owner | Notes |
|---|---|---|
| 5225 | `simplex-chat-hermes` daemon | container 5225; WS protocol has **no auth** → loopback only |
| 5226 | `simplex-chat-iptv` daemon 🟡 | container 5225 → host 5226 |
| 9119 | `hermes-serve` (default) | Desktop backend API |
| 9120 | `hermes-serve-iptv` | Desktop backend API |

## SimpleX topology

- **Relays (self-hosted, `alt255.casa`):** SMP `smp://…@simplex.alt255.casa:5223`,
  XFTP `xftp://…@xftp.alt255.casa:5225`. Full URLs (with credentials) live in
  `~/.config/containers/systemd/simplex-chat-<name>.env` — **not** in this doc.
- **Identity = data volume.** Each daemon's SimpleX identity/keys/DB live in its named
  Podman volume (`simplex-chat-hermes-data`, `simplex-chat-iptv-data`). A new volume =
  a new identity/QR.
- **Daemon image:** `localhost/simplex-chat-hermes:latest` (built from the repo's
  `Dockerfile.simplex`). The iptv daemon reuses this same image with a different volume.
- **File transfer:** inbound/outbound media bind-mounted at
  `~/.hermes/cache/simplex-files` (default) / `~/.hermes/cache/simplex-iptv-files` (iptv),
  auto-accept up to 50 MB.
- **Channels (SimpleX groups):**
  - default profile home channel = `simplex:1` (group **"hermes-agent"**) — startup /
    cron / notification target.
  - iptv admin channel 🟡 = group `group:<GID>` (name `iptv-admin`) on the iptv daemon.
  - iptv notify channel 🟡 = a second group for a subset of users (receive + limited
    replies); design pending — see Phase 2 brief.

## Profiles

Each profile is a self-contained `HERMES_HOME`:

| Profile | HERMES_HOME | SimpleX identity | Gateway |
|---|---|---|---|
| `default` | `~/.hermes` | `simplex-chat-hermes` (5225) | `hermes-gateway` ✅ |
| `iptv` | `~/.hermes/profiles/iptv` | `simplex-chat-iptv` (5226) 🟡 | `hermes-gateway-iptv` 🟡 |

Config precedence for the SimpleX adapter: the profile's **`.env`** is authoritative
(`SIMPLEX_WS_URL`, `SIMPLEX_HOME_CHANNEL[_NAME]`, `SIMPLEX_ALLOWED_USERS`,
`SIMPLEX_ALLOW_ALL_USERS`); `config.yaml`'s `platforms.simplex` only needs
`enabled: true`.

## Authorization model (important)

- Inbound authz is core (`gateway/authz_mixin.py`): `ALLOW_ALL` → env allowlist →
  pairing store → global → **deny**.
- SimpleX authorizes a **group as a whole** (`sender_id = group:<id>`); per-member
  gating inside a group is **not wired** for SimpleX today. (Phase 2 addresses this for
  the notify channel.)
- `SIMPLEX_GROUP_ALLOWED` and `SIMPLEX_AUTO_ACCEPT` are declared in `plugin.yaml` but
  **not consumed** — do not rely on them.
- ⚠️ **Posture note:** default profile historically ran `SIMPLEX_ALLOW_ALL_USERS=true`
  (any user could command the bot). Being locked down to an allowlist as part of the
  iptv work — see plan Task 5.

## Key paths (on `hermes-ai`)

| Path | What |
|---|---|
| `~/.hermes/` | default profile `HERMES_HOME` (config.yaml, `.env`, logs, kanban.db, memories, cron, hooks, sandboxes, platforms) |
| `~/.hermes/profiles/iptv/` | iptv profile `HERMES_HOME` |
| `~/.hermes/hermes-agent/` | the code checkout + `.venv` |
| `~/.hermes/logs/agent.log` | gateway/agent log |
| `~/.hermes/cache/simplex*-files/` | SimpleX media stores (bind-mounted into daemons) |
| `~/.config/containers/systemd/*.container` + `*.env` | Podman Quadlet daemon units |
| `~/.config/systemd/user/hermes-*.service` | gateway/serve units |

## Quick health check

```bash
ssh hermes-ai 'systemctl --user is-active \
  simplex-chat-hermes.service hermes-gateway.service \
  hermes-serve.service hermes-serve-iptv.service; \
  ss -tlnp | grep -E "127.0.0.1:(5225|5226|9119|9120)"'
```

## Related docs

- `plans/2026-07-16-iptv-simplex-profile-design.md` — iptv profile design (spec)
- `plans/2026-07-16-iptv-simplex-profile-plan.md` — iptv implementation plan
- `plans/2026-07-16-iptv-notify-channel-brief.md` — Phase 2 notify-channel brief (🟡 pending)
- `plans/2026-06-06-hermes-0.16-cutover-runbook.md` — 0.16 cutover / env-var conventions
