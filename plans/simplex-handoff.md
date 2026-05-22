---
title: SimpleX migration — handoff notes
date: 2026-05-21
status: paused, server reverted to feat/simplex-plugin
---

# SimpleX migration — handoff

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
