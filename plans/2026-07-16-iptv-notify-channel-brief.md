# iptv notify-channel — design brief (for the iptv hermes-agent)

**Date:** 2026-07-16
**Audience:** the **iptv** hermes-agent (this document is fed to you as context — read it,
then propose approaches; do **not** write code yet).
**Status:** Phase 2 brief. Phase 1 (dedicated iptv SimpleX identity + admin channel) is
already live.

---

## 1. What you're being asked to solve

Add a **second SimpleX group** for the `iptv` profile — call it **`iptv-notify`** — with a
different authorization shape from the existing admin channel:

- **Outbound:** the iptv agent posts **notifications** to this group **on demand** when
  certain things happen in the iptv system (e.g. a stream goes down, a job fails, a health
  check flips). A *subset of users* (not just the operator) are members and receive these.
- **Inbound (the hard part):** those same users may send a **constrained set** of
  commands/queries that the agent acts on — "receive + limited replies." They must **not**
  get full control of the agent the way the operator does in the admin channel.

Contrast with what already exists:

| Channel | Who | Inbound authority |
|---|---|---|
| `iptv-admin` (live) | operator (Brandon) only | **full** agent control |
| `iptv-notify` (this brief) | a subset of users | **limited** — receive + a small command surface |

Your job: figure out the cleanest, lowest-maintenance way to implement the `iptv-notify`
authority model, and present options. A recommendation at the end, not code.

---

## 2. The live environment you're running in (verified 2026-07-16)

- **Host:** `ai-server` (Fedora, systemd `--user`, Podman Quadlet). You run as the `iptv`
  profile (`HERMES_HOME=~/.hermes/profiles/iptv`).
- **Your SimpleX identity:** dedicated daemon `simplex-chat-iptv` on `127.0.0.1:5226`
  (bot display name `hermes-iptv`, separate data volume = separate identity). Loopback
  only; the daemon WS protocol has **no auth**.
- **Your gateway:** `hermes-gateway-iptv.service` running `hermes --profile=iptv gateway
  run`, connected to `ws://localhost:5226`.
- **Admin channel:** SimpleX group **`1`** (`iptv-admin`) on your daemon.
  `SIMPLEX_HOME_CHANNEL=1` (default outbound target), `SIMPLEX_ALLOWED_USERS=<operator
  member id>`, `SIMPLEX_ALLOW_ALL_USERS=false`.
- **Relays:** self-hosted SMP `simplex.alt255.casa:5223`, XFTP `xftp.alt255.casa:5225`.
- **Model:** `ppq-autoclaw` via the `litellm` provider (`http://localhost:4000/v1`).
- Full topology: `docs/ai-server-architecture.md`.

---

## 3. How authorization actually works here (important, verified)

Several of these were confirmed live during Phase 1 and correct earlier assumptions:

1. **Inbound authz is core** (`gateway/authz_mixin.py`, `_is_user_authorized`): order is
   per-platform `ALLOW_ALL` → env allowlist (`SIMPLEX_ALLOWED_USERS`) → pairing store →
   global `GATEWAY_ALLOW_ALL_USERS` → **deny**. It is **binary**: a sender is either
   authorized (→ full agent turn) or denied (→ dropped). There is **no built-in notion of
   "this user may run only these commands."**
2. **Group messages are attributed to the individual member.** The deployed adapter sets
   `sender_id` to the sender's **per-membership member id** (NOT `group:<id>`). Verified:
   an operator message in a group logged `Unauthorized user: <memberId> (Brandon)` until
   that member id was allowlisted. **Consequence: per-member gating within one group is
   possible** — you can distinguish who sent a group message.
3. **Member ids are local and per-daemon.** SimpleX assigns no global user id. Your daemon
   mints its own local id for each member. The same human has a *different* id on the
   default daemon vs. yours. So `iptv-notify` members' ids must be captured **on your
   daemon** (`5226`).
4. **Display-name matching** is supported in `SIMPLEX_ALLOWED_USERS` but is **spoofable**
   (any group member can rename themselves) — unsuitable for a security boundary.
5. **Dead knobs:** `SIMPLEX_GROUP_ALLOWED` and `SIMPLEX_AUTO_ACCEPT` are declared in
   `plugin.yaml` but **not consumed**. Don't rely on them (but `SIMPLEX_GROUP_ALLOWED`
   could be *repurposed* if you extend the adapter).
6. **Core has per-group policy machinery** (`gateway/authz_mixin.py:183-247`,
   WeCom-style `group_policy` / `allow_from`) that the SimpleX adapter **does not currently
   populate**. Whether wiring the SimpleX adapter into it is the right lever is one thing to
   evaluate — **verify against the live code before relying on it**, since this brief's
   line numbers may drift.

**The crux:** the allowlist gets you *who may talk to the agent at all*, but not *what a
given user may make it do*. "Limited replies" is a **command-surface** problem, and the
current stack has no first-class answer. That's the gap to design around.

---

## 4. Design directions to evaluate (non-exhaustive)

Weigh at least these; propose others if better. For each: how it scopes commands, where the
code touches, maintenance cost, and failure modes.

- **A. Command-filter middleware.** Intercept inbound messages from the `iptv-notify`
  group before they reach the agent; allow only messages matching a small,
  explicitly-configured command grammar (e.g. `status`, `restart <name>`, `mute <id>`),
  reject/ignore the rest with a canned reply. Full agent access stays admin-only. Consider
  where this hook lives (adapter vs. gateway vs. a pre-agent filter) and how the command
  list is configured per channel.
- **B. Restricted toolset / persona per channel.** Route `iptv-notify` messages to the
  agent with a **constrained toolset** (read-only / a handful of iptv ops tools) and a
  system prompt that refuses out-of-scope requests. Uses the existing toolset config
  (`platform_toolsets`, `disabled_toolsets` in `config.yaml`) rather than new authz code.
  Evaluate whether toolset scoping can be made **per-group** (not just per-profile), since
  admin and notify share one profile/daemon.
- **C. Second profile for notify.** Give `iptv-notify` its own profile (its own restricted
  config/toolset), at the cost of another gateway + daemon. Cleaner isolation, more moving
  parts — weigh against the low-maintenance goal.
- **D. simplex-bridge** (`github.com/brandon-btcgroup/simplex-bridge`, already running):
  a multi-client auth + group-scoping + offline-buffering WS layer; its example config even
  references `iptv-admin`/`iptv-notifications`. **But**: it scopes *which groups a client
  sees*, **not** *what commands are allowed* (that stays hermes policy), it speaks a
  **different WS protocol** (would need a new hermes bridge-client adapter), and it adds a
  **second server**. Include it in the comparison and be honest about whether it earns its
  keep here vs. doing everything on the ai-server.

Note the outbound side is comparatively easy: the adapter can already send to a specific
group (`send()` to `group:<id>`), so posting notifications to `iptv-notify` is
straightforward once the group exists. The **event source** — how the iptv system tells you
"something happened" (webhook? cron? the `~/.hermes/iptv-ops` tooling? a poll of
`tvip.casa/health`?) — is an integration point you should define, but it's separable from
the authority design.

---

## 5. Constraints & preferences

- **Low maintenance over time** and **do it the right way** — the operator's explicit
  priorities.
- **Prefer everything on the ai-server**; only reach for the bridge/second server if it
  clearly wins.
- **Consistent with Phase 1:** group-based channels, per-member ids for anything
  security-facing (never display names), `allow_all=false`.
- **Fail closed:** an unrecognized/out-of-scope request from a notify user must never fall
  through to full agent control.

---

## 6. What to produce (your deliverable)

1. **2-3 concrete approaches** with trade-offs (scoping mechanism, code touch-points,
   maintenance, failure modes), mapped against the constraints above.
2. **A recommendation** with reasoning.
3. **Open questions** for the operator (e.g. the exact command grammar for notify users;
   the event source for notifications; whether notify users are few/static or dynamic).
4. **Do NOT write implementation code yet** — this is a design step. Verify any code claims
   in §3/§4 against the live tree first (line numbers may have drifted).

---

## 7. Open questions to raise back

- What is the concrete **command grammar** notify users should have (the minimal useful
  set)?
- Who are the notify users — a **small static** set (capture member ids once) or
  **dynamic** (needs a self-serve pairing flow)?
- What **events** trigger notifications, and where do they originate (iptv-ops, a webhook,
  a health poll)?
- Should notify users get **replies to their queries only**, or also **unsolicited**
  broadcasts (or both)?
