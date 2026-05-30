---
title: SimpleX migration — handoff notes
date: 2026-05-21
updated: 2026-05-29
status: production branch hardened (d31a04341, json send form); sender fix upstreamed as PR #35046; issue #3 (silent drop) still the open live-retest item
---

# SimpleX migration — handoff

## Update — 2026-05-29 (read this first)

A community bug report, **NousResearch/hermes-agent issue #30150**
(reporter `flyingeagles123`), independently surfaced three upstream-adapter
bugs. It is marked a **duplicate of open PR #26433** (`fix/gateway-simplex-bugs`,
author `daimon-nous`, **OPEN, not merged**). Cross-referencing it against
our own TRACE findings reshaped the plan:

**New consolidated branch: `fix/simplex-event-dispatch`** (off `main` =
upstream `4cc18877c`, built + tested + **pushed to `brandon-btcgroup/hermes-agent`**;
on the AI server the fork remote is `fork`). It carries **four** fixes in
`plugins/platforms/simplex/adapter.py`:

| Fix | What | Overlap |
|---|---|---|
| 1. `/_start` on WS connect | daemon only pushes async events to connections that issued `/_start`; without it inbound messages are stored but never delivered (this is the source of the `WS idle, forcing reconnect` warnings) | **new — not in our original notes**; = issue #30150 bug 1 / PR #26433 |
| 2. `newChatItems` resp-nesting | read `event["resp"]["chatItems"]` (batch items nest under `resp`, not top level) | = our old "upstream bug #1" / issue bug 2 / PR #26433 |
| 3. sender from `chatDir.groupMember` | with `chatItemMember` fallback for old payloads | = our old "upstream bug #2"; **NOT in PR #26433**; matches large feature PRs #4666 / #27978 |
| 4. outbound `/_send @id text` / `/_send #id text` | replaces invalid `@[id]`/`#[id]` bracket syntax in `send()` + `_standalone_send()` | = issue bug 3 / PR #26433 |

Tests: `scripts/run_tests.sh tests/gateway/test_simplex_plugin.py` →
**29 passed, 0 failed** (added 2 regression tests: resp-unwrap dispatch,
chatDir.groupMember sender). `check-windows-footguns.py --diff main` clean.
Commit `4f1641e29`.

**Decisions this implies:**

- **Do NOT open our own PR-6 for the resp-unwrap fix** — PR #26433
  already covers bugs 1/2/4. Comment on / +1 #26433 instead.
- **Fix 3 (sender extraction) is our one novel upstream contribution.**
  It is absent from #26433 but present in the large feature PRs #4666
  and #27978. If #26433 merges first, rebase this branch down to just
  fix 3 and offer it as a focused follow-up.
- **`/_start` (fix 1) was never in our original diagnosis.** In our prior
  TRACE runs events *were* arriving, so something else (old fork still
  subscribed? shared daemon?) masked it. This is the most likely reason
  inbound behaviour was inconsistent — **re-verify carefully.**

**Issue #3 (silent drop in `_process_message_background`) is still
unresolved and is NOT addressed by any of the four fixes above** — it
sits downstream of everything they touch. The next live retest on the AI
server (checkout `fix/simplex-event-dispatch`) is specifically to see
whether, now that `/_start` and correct dispatch are in place, a phone
message produces an actual end-to-end reply — or whether issue #3 still
swallows it. Follow the issue-#3 debugging plan below if it stays silent.

Related upstream threads: issue #30150, PR #26433 (open), #26480, #27120,
#4666, #27978.

---

## Update — 2026-05-29 (part 2): upstream PR opened + outbound send hardened

### Sender fix upstreamed as a focused PR

- Filed **issue #35045** and opened **PR #35046**
  (`brandon-btcgroup:fix/simplex-group-sender` → `NousResearch:main`),
  containing **only** the `chatDir.groupMember` sender fix + one regression
  test. PR body discloses the overlap (defers the other three bugs to #26433;
  notes #4666/#27978 carry the same change in larger form) and offers to
  fold/close. Also left a +1 comment on #26433 with the sender diff and a
  note that its `send()` change fixes DM only and leaves group send broken.
- Branch `fix/simplex-group-sender` is off latest upstream; 28/28 SimpleX
  tests pass. Full-suite failures on this machine are **pre-existing on
  unmodified `main`** (anthropic adapter, gateway service/WSL/systemd, TUI —
  macOS-environment), confirmed by running the same files on `689ef5e23`.

### Why we did NOT open a group-send PR

Group send is **not** an uncovered gap. Three different fixes already exist
upstream:

| PR | Group-send form |
|---|---|
| #4666 | `/_send #<id> json [{"msgContent":{"type":"text","text":…}}]` |
| #27978 | `/_send #<id> json [...]` (same) |
| #26480 | `#<quoted_group_name> <body>` (name-based) |
| #26433 | leaves it broken (`#[<id>]`) — DM only |

The `#4666`/`#27978` **json** form is more robust than a `text` shorthand:
it escapes newlines/backslashes, whereas `/_send #<id> text <body>` truncates
the body at the first newline. A competing focused PR would be redundant and
use an inferior form, so we skipped it.

### Production branch hardened to the json form

`fix/simplex-event-dispatch` commit **`d31a04341`** switches `send()` and
`_standalone_send()` (group **and** DM) from `/_send <ref> text <body>` to
`/_send <ref> json [{"msgContent":{"type":"text","text":<body>}}]`. This fixes
silent truncation of **multi-line agent replies** and aligns outbound text
with where upstream is heading. SimpleX suite now 30/30 (added
`test_send_escapes_multiline_body`). Pushed to fork as a fast-forward
(`8d2154540..d31a04341`) — redeploy is `git reset --hard
fork/fix/simplex-event-dispatch` + `hermes gateway restart` (no dep reinstall;
adapter-only change, editable install).

> Rebase note: when #26433 merges, `send()` will conflict — upstream changes
> the DM line to `@{display_name}` (needs its `_contact_names` polling
> subsystem), we use `/_send @<id> json`. Keep ours unless maintainers
> standardize on the display-name form; ours doesn't depend on cached names
> and handles groups. The `simplex-known-good-20260529` tag predates this
> json change, so rolling back to it also reverts the multi-line fix.

---


Picking up: we paused mid-migration from Brandon's old standalone fork
(`feat/simplex-plugin`) onto upstream's bundled `simplex-platform`
plugin (which NousResearch merged in commit `09d9724a0`). The old fork
is back in production on the AI server. Five feature PRs and several
upstream bug fixes are built and pushed but not yet opened against
NousResearch.

---

## Quick context — why we did this

1. NousResearch merged a SimpleX platform plugin upstream
   (`09d9724a0 feat(gateway): add SimpleX Chat platform plugin`, author:
   Mibayy). Upstream's plugin is **746 lines** vs our fork's **1278
   lines**.
2. Our fork carries extras upstream doesn't: CLI for group discovery,
   per-group replay cursors, bind-mount-aware media, docker-compose
   recipe, thumbnail/poster/duration helpers for outbound media.
3. Plan: rebase ourselves onto upstream as the base, contribute our
   extras as separate focused PRs, retire the standalone fork over
   time. Goal is to stop carrying a 530-line diff against an actively-
   developed plugin file.

---

## Branch inventory (all pushed to `brandon-btcgroup/hermes-agent`)

| Branch | Status | Purpose | Tests |
|---|---|---|---|
| `feat/simplex-plugin` | **In production on AI server** | Brandon's original 1278-line fork — text + media round-trip works | n/a (production) |
| `main` | Synced to upstream `4cc18877c` | Tracking NousResearch | n/a |
| `feat/simplex-cli-discovery` | Built, pushed, **not opened** | PR-1: `hermes simplex list\|join` + `_ws_client.py` | 17 new ✓ |
| `feat/simplex-replay-cursors` | Built, pushed, **not opened** | PR-2: per-group cursor file + dedupe ring | 22 new ✓ |
| `feat/simplex-bind-mount-media` | Built, pushed, **not opened** | PR-3: `SIMPLEX_FILE_DIR` host↔container path translation for inbound | 18 new ✓ |
| `docs/simplex-docker-compose` | Built, pushed, **not opened** | PR-4: docker-compose recipe in `simplex.md` | docs only |
| `feat/simplex-outbound-media` | Built, pushed, **not opened** | PR-5: `send_image_file`/voice/video/document via daemon `/_send` with thumbnail/duration/poster | 27 new ✓ (1 PIL-gated skip) |
| `test/simplex-all` | Built, pushed, **deploy did not work** | Integration of all 5 PRs + merge-conflict resolutions | 111 ✓ aggregated |
| `debug/simplex-event-trace` | Built, pushed, **debug-only** | TRACE-laden integration branch we used to isolate the upstream bugs |

PRs that didn't get opened against `NousResearch/hermes-agent`:
**none yet**.

---

## What works on `test/simplex-all`

Unit tests:

```bash
scripts/run_tests.sh tests/gateway/test_simplex_*.py
# 111 passing, 1 skipped (PIL not installed locally; CI exercises it)
```

`hermes simplex list` against a live daemon — returns the active user
and joined groups end-to-end.

Adapter loads cleanly under `hermes_plugins.simplex_platform`. Plugin
loader picks it up via `plugin.yaml`.

WebSocket connects to the daemon. Replay-cursor file `cursors.json` is
created in `$HERMES_HOME/simplex/` on the first dispatched group
message.

---

## What does NOT work on `test/simplex-all`

**Inbound messages don't reach the agent** when running against the
production daemon on the AI server. The bot stays silent. Reverting
to `feat/simplex-plugin` restores normal operation immediately, so
the daemon and the agent path are both healthy — it's something on
the upstream-adapter side that doesn't fit the production daemon's
behaviour.

We have TRACE evidence for **two real upstream bugs** and **one
unresolved silent drop** further down.

### Upstream bug #1 — `event["resp"]` is not unwrapped before dispatch

The simplex-chat daemon emits `newChatItems` events shaped as:

```json
{ "resp": { "type": "newChatItems", "chatItems": [...] } }
```

but `_handle_event` in upstream's adapter reads `event.get("chatItems")`
at the top level, which is empty for this layout. Result: every group
message is silently dropped before reaching `_handle_new_chat_item`.

Verified with this TRACE line from the AI server:
```
SimpleX TRACE: event_keys=['resp'] top_type=None resp_type='newChatItems' corrId=None
```

**Fix** (already on `debug/simplex-event-trace`):

```python
async def _handle_event(self, event: dict) -> None:
    inner = event.get("resp") if isinstance(event.get("resp"), dict) else event
    resp_type = inner.get("type") or event.get("type") or ""
    # ... corrId echo filter unchanged ...
    if resp_type == "newChatItem":
        await self._handle_new_chat_item(inner)
    elif resp_type == "newChatItems":
        items = inner.get("chatItems") or event.get("chatItems") or []
        for item_wrapper in items:
            await self._handle_new_chat_item(item_wrapper)
```

**Action:** open as a small focused PR against `NousResearch/hermes-agent`
when work resumes. Standalone fix, no dependencies on the other 5 PRs.

### Upstream bug #2 — sender extracted from wrong key

Upstream reads `chat_item.get("chatItemMember")` for the group sender.
The production daemon (currently simplex-chat 3.x running in
`localhost/simplex-chat-hermes:latest`) emits sender info under
`chat_item.chatDir.groupMember` instead. When the legacy key is
missing, `sender_id` falls back to `chat_id` (`"group:1"`) — not a
real user — which then fails downstream allowlist matching.

Verified with this TRACE line from the AI server:
```
SimpleX TRACE NCI: ... chat_item_keys=['chatDir', 'content', 'mentions', 'meta', 'reactions']
```

(Note: no `chatItemMember` in the keys list.) And after the fix:
```
SimpleX TRACE SENDER: sender_id='TjdJQnpkazF2MFdpTXhNYw==' sender_name='Brandon' chat_dir_type='groupRcv'
```

**Fix** (already on `debug/simplex-event-trace`):

```python
chat_dir = chat_item.get("chatDir") or {}
member = (
    chat_dir.get("groupMember")
    or chat_item.get("chatItemMember")
    or {}
)
if is_group and member:
    member_profile = member.get("memberProfile") or {}
    sender_id = str(
        member.get("memberId")
        or member.get("groupMemberId")
        or member.get("id")
        or chat_id
    )
    sender_name = (
        member.get("displayName")
        or member.get("localDisplayName")
        or member_profile.get("displayName")
        or sender_id
    )
```

**Action:** open as a separate small focused PR (or fold into the same
PR as bug #1 since they're tightly related — both are "the daemon's
event shape doesn't match what upstream parses").

### Unresolved issue #3 — silent drop in `_process_message_background`

Even with both upstream fixes applied, the message:

- reaches `_handle_event` ✓
- parses correctly (text, group, rcvNew) ✓
- extracts the correct sender ✓
- passes the message-handler attachment check (`self._message_handler`
  is set) ✓
- reaches `handle_message` ✓
- `handle_message` calls `_start_session_processing` which spawns
  `_process_message_background` as a task ✓

…but the background task produces zero log output afterwards. No
typing indicator, no `_run_processing_hook` activity, no agent
reasoning, no LLM call, no error, no exception. Just silence.

Both `_message_handler` and `_session_store` are confirmed attached
(via TRACE). `coerce_plaintext_gateway_command` is a no-op for our
group text. Allowlist is bypassed via `SIMPLEX_ALLOW_ALL_USERS=true`
**and** the memberId is in `SIMPLEX_ALLOWED_USERS` (both).

Possible candidates we did not investigate:

- The background task raises an exception that's never retrieved
  (asyncio normally logs "Task exception was never retrieved" on GC,
  but maybe that log is going somewhere we didn't check).
- Something in `_process_message_background`'s typing-indicator setup
  fails silently when `send_typing` is the no-op upstream provides.
- The `_keep_typing` introspection (`inspect.signature` of upstream's
  no-op) hits an edge case.
- `_run_processing_hook("on_processing_start", event)` blocks or
  raises silently.
- `MessageEvent`'s `raw_message=wrapper` carries the full nested daemon
  payload, including base64-padded ids with `==`, and something
  downstream that serialises events for logging/storage chokes on it.

**Next-session debugging plan:**

1. On `debug/simplex-event-trace`, override `_process_message_background`
   in `SimplexAdapter` to wrap the parent's body in a try/except that
   logs at WARNING. That'll surface any silent exception.
2. Alternative: temporarily set `PYTHONASYNCIODEBUG=1` in the gateway's
   systemd unit and look for "Task exception was never retrieved"
   warnings.
3. If still silent, add WARNING-level traces inside
   `_process_message_background` itself (a fork of base.py just for
   debugging, never to be merged).
4. Once root-caused, decide whether it's an upstream bug worth a third
   PR or something specific to our deployment.

---

## What still has to happen before the 5 feature PRs go to upstream

The 5 feature PRs are **not blocked by issue #3** in code — they all
ship complete code with passing tests. They ARE blocked by issue #3
in **confidence**: until inbound messaging works against the real
daemon, we don't have a live integration test for the assumptions
each PR makes.

Suggested ordering when we resume:

> **Superseded by the 2026-05-29 update above.** Step 1 below assumed no
> upstream PR existed; PR #26433 now covers the resp-unwrap + `/_start` +
> `/_send` fixes, and our consolidated `fix/simplex-event-dispatch` branch
> carries all four (incl. sender extraction). Treat steps 2–4 as still
> valid once issue #3 is root-caused.

1. **First** — open PR-6 (resp unwrap + sender extraction) against
   NousResearch as a standalone upstream bug fix. This is the
   highest-value contribution of the entire session and doesn't
   depend on any of our extras.
2. **Then** — debug issue #3 to root cause.
3. **Then** — only after issue #3 is fixed and we have a working
   integration branch on the AI server, open PR-1 through PR-5.
4. PR-5 (outbound media) depends architecturally on PR-3's env vars
   (`SIMPLEX_FILE_DIR`, `SIMPLEX_DAEMON_FILES_FOLDER`). Open PR-3
   first, mention PR-5 as a planned follow-up in the description.

---

## AI server deployment state

- `~/.hermes/hermes-agent` on the AI server has remote `fork` pointing
  at `brandon-btcgroup/hermes-agent` (the personal fork) and `origin`
  pointing at `NousResearch/hermes-agent` (upstream).
- Currently checked out: `feat/simplex-plugin` (in production).
- `~/.hermes/.env` carries the legacy `SIMPLEX_GROUP_IDS=1` and
  `SIMPLEX_HOME_CHANNEL=1` config which only the old fork's adapter
  reads. Upstream's adapter ignores `SIMPLEX_GROUP_IDS` entirely.
- `simplex-chat-hermes` podman container runs the daemon, port
  `127.0.0.1:5225 → 5225` via `socat`. Daemon flags include
  `--create-bot-display-name=hermes`, `--create-bot-allow-files`,
  `--auto-accept-files 52428800`, custom SMP server
  `simplex.alt255.casa:5223`.
- The bot's group on Brandon's phone: `hermes-agent` (groupId 1).
- DEBUG logging in `.env`: `HERMES_LOG_LEVEL=DEBUG`.
- Test allowlist mods that should probably be reverted on the server
  before next session: `SIMPLEX_ALLOWED_USERS=Brandon,TjdJQnpkazF2MFdpTXhNYw==`
  (added the base64 memberId during debug) and `SIMPLEX_ALLOW_ALL_USERS=true`.

---

## Diff summary on `test/simplex-all` vs upstream

```
plugins/platforms/simplex/__init__.py        (no change)
plugins/platforms/simplex/adapter.py         +~600 lines, modified
plugins/platforms/simplex/cli.py             new, ~200 lines
plugins/platforms/simplex/_ws_client.py      new, ~250 lines
plugins/platforms/simplex/_replay.py         new, ~140 lines
website/docs/user-guide/messaging/simplex.md +57 lines (CLI + docker-compose)
tests/gateway/test_simplex_cli.py            new, 17 tests
tests/gateway/test_simplex_replay.py         new, 22 tests
tests/gateway/test_simplex_bind_mount.py     new, 18 tests
tests/gateway/test_simplex_outbound_media.py new, 27 tests
```

Plus the merge commits resolving conflicts between PR-3 and PR-5 in
`__init__` env-var reads, helper sections, and `Tuple` import.

---

## Things to NOT forget when resuming

- Branches still live on the fork; nothing was force-pushed or
  deleted. Run `git fetch fork --prune` to refresh from the remote
  state.
- Local venv on the Mac dev box: `~/Development/hermes-agent/venv`,
  Python 3.11.15 (uv-managed). Recreate with
  `uv pip install -e ".[all,dev]"` if it disappears.
- `scripts/run_tests.sh` is the canonical CI-parity runner. Don't
  use raw `pytest`.
- `scripts/check-windows-footguns.py --diff main` should be clean
  before any PR is opened. All 5 feature branches passed last time.
- CONTRIBUTING.md branch-naming convention is `feat/`, `fix/`,
  `docs/`, `test/`, `refactor/`. PR-6 (resp unwrap + sender
  extraction) should land as `fix/simplex-event-dispatch`.
- The `[simplex]` packaging extra referenced by the install-hint
  strings in our code (`pip install -e '.[simplex]'`) does **not**
  exist in upstream's `pyproject.toml`. Either add it in the first
  PR to upstream, or relax the hint string to `pip install
  websockets`. Doesn't affect deployment because `websockets` is
  already a transitive dep.
