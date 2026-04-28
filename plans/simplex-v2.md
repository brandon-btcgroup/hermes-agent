# SimpleX adapter — v2 plan

## Goals

Close the two highest-impact gaps from the v1 ship list while the integration is fresh:

1. **Reliability** — stop silently dropping messages when Hermes is offline.
2. **Capability** — match the media surface of the other messengers (image, file, voice, video). v1 was text-only.

Direct messages, reactions, streaming edits, short-link resolution, and `hermes simplex create` are explicitly deferred to v2.1+.

## Slice 1 — Missed-message replay

**Problem.** simplex-chat stores messages received while Hermes is offline but does **not** re-emit them when a fresh WS client connects. The user sees a delivered message; Hermes never processes it.

**Approach.** Persist a per-group "last seen item id" cursor on disk; on reconnect, fetch newer items via `/_get_chat` and dispatch them through the same handler as live events.

**Steps.**

1. Cursor store at `$HERMES_HOME/simplex/cursors.json` — `{group_id: last_item_id}`, written after each successful dispatch (debounced) and after replay.
2. `SimplexChatClient.api_get_chat(chat_ref, count, after_id)` — wraps `/_get_chat #<gid> count=N after=<id>`.
3. On reconnect, before re-subscribing the live event stream: for each subscribed group, walk `api_get_chat` from `cursor[gid]` forward in pages of 50 until exhausted or `SIMPLEX_REPLAY_MAX` (default 200) is hit.
4. Feed each replayed `ChatItem` through the existing `_handle_chat_item_update` path so allowlist, self-echo filter, and message-type checks all apply. De-dupe by `itemId` against an in-memory recent-set to defend against the daemon also re-emitting on the live stream.
5. New env: `SIMPLEX_REPLAY_MAX` (cap), `SIMPLEX_REPLAY_DISABLE` (escape hatch). Document in the setup guide.

**Edge cases.**
- Cursor file missing on first run after upgrade → seed cursors from current `/_get_chat ... count=1` per group, treat as "caught up", no replay.
- `lastItemId` no longer present (message TTL expired on daemon) → daemon returns from oldest; cap by `SIMPLEX_REPLAY_MAX` and log a warning.
- Long downtime → bounded by cap; user sees "(replayed N missed messages)" log line, not a flood.

**Acceptance.**
- Stop Hermes, send 5 messages from phone, restart Hermes → all 5 are processed.
- Same scenario with 500 messages and `SIMPLEX_REPLAY_MAX=200` → 200 newest are processed, 300 dropped with a warning, cursor advances to the latest.
- Live stream messages received during replay are not double-processed.

**Estimate.** ~1 day. Cursor IO and de-dupe set are standard; the daemon API is straightforward.

## Slice 2 — Media (image, file, voice, video)

**Precondition — shared file path.** simplex-chat reads/writes files at `/root/.simplex/files/` inside the container. Hermes runs outside the container and needs to (a) read inbound files and (b) hand the daemon a path it can read for outbound. Two options:

- **Bind-mount the daemon's file dir into Hermes' cache** — add a host volume `~/.hermes/cache/simplex-files:/root/.simplex/files` to the Quadlet. Documented as the supported config; refuse to enable media if the path isn't readable on Hermes side.
- **Skip it and HTTP-fetch from the daemon** — not viable, daemon has no HTTP file endpoint.

Settle on bind-mount. New env: `SIMPLEX_FILE_DIR` (host path, default `~/.hermes/cache/simplex-files`). Adapter checks readability on connect; if missing, logs `MEDIA_DISABLED` and falls back to text-only without a fatal error.

### Inbound (image, file, voice, video)

1. Extend `_handle_chat_item_update`: when `msgContent.type ∈ {image, file, voice, video}`, do not early-return on the v1 "text only" check.
2. Read the `chatItem.file` envelope: `fileId`, `fileName`, `fileSize`, `fileStatus`. If the daemon hasn't auto-received it, call `/_set_file_to_receive <fileId>` and re-poll on `rcvFileComplete`.
3. Resolve to host path via `SIMPLEX_FILE_DIR / fileName`; surface a `MessageEvent` with the right `MessageType` (`PHOTO`, `FILE`, `VOICE`, `VIDEO`) plus the local path.
4. For voice/video, capture `msgContent.duration` (seconds) into `MessageEvent` metadata so downstream features (transcription, players) can use it.
5. Cap inbound size — `SIMPLEX_MAX_FILE_BYTES` (default 25 MB); reject larger files with a clear chat-side reply.

### Outbound (`send_image`, `send_image_file`, `send_voice`, `send_video`)

For each method: copy the source file into `SIMPLEX_FILE_DIR` (rename to a UUID to dodge collisions), construct `msgContent`:

- `image` — `{type: "image", text: caption, image: <base64-data-url-of-thumbnail>}` + `filePath: <name>`. Generate a 224×224 JPEG thumbnail via PIL.
- `file` — `{type: "file", text: caption}` + `filePath: <name>`. No thumbnail.
- `voice` — `{type: "voice", text: "", duration: <seconds>}` + `filePath: <name>`. Probe duration via `ffprobe`.
- `video` — `{type: "video", text: caption, duration: <seconds>, image: <base64-poster>}` + `filePath: <name>`. Probe duration + extract poster frame at 1s via `ffmpeg`.

Replace the v1 stub `send_image` that returns `error="not implemented"`. Override `send_voice`, `send_video`, `send_image_file` with the same pattern.

**Acceptance.**
- Phone → group: send a JPG, MP4 (≤30 s), voice note, PDF. Each arrives in Hermes with the correct `MessageType` and a readable host path. Hermes can echo metadata back as text.
- Hermes → group: an LLM tool that produces a chart PNG ends up rendered in the SimpleX phone client with caption.
- Voice note recorded on phone is delivered with `duration` metadata; Hermes' transcription pipeline (if enabled) processes it normally.
- File over `SIMPLEX_MAX_FILE_BYTES` is rejected with a user-visible reply, not silently swallowed.

**Estimate.**
- Image + file: ~1 day. Bind-mount + thumbnail are the only new mechanics.
- Voice + video: ~½ day on top of image/file. ffprobe/ffmpeg already installed by the install script; the daemon's voice/video MsgContent shape is a small variation on image. Worth including in v2.0.

Total slice 2: ~1.5 days.

## Out of scope (defer to v2.1+)

- Direct messages (`chatType=direct` events). Small change but bumps the test matrix; ship after media settles.
- Reactions. Need to verify protocol coverage in the simplex-chat WS API first.
- Streaming replies via `apiUpdateChatItem`. Gated on a real-device test of how phone clients render in-place edits.
- Short-link resolution in the adapter (and/or smp-server `web` config docs). Solves the URL-encoding mess we hit on install but isn't a daily-driver issue once you've joined.
- `hermes simplex create` — daemon-side group creation + invite link emit. Same category as short-link.
- Captured fixtures from a real daemon, replacing the synthetic test fixtures. Ongoing quality work; covered ad-hoc as bugs surface.

## Risks

- **Bind-mount path drift.** If users follow our docs but the daemon is configured with a non-default file dir (`-f`), mount mapping breaks silently. Mitigation: doc it loudly, and have the adapter compare a daemon-reported path (via `/_files_folder` if it exists, else config) against `SIMPLEX_FILE_DIR` on connect.
- **File auto-receive flag.** simplex-chat has a per-user setting for auto-receiving files. If the daemon was started without it, every inbound file needs an explicit `/_set_file_to_receive`. Build for the explicit path; auto-receive becomes an optimization.
- **Volume-mount permissions on rootless Podman.** The daemon's `/root/.simplex/files` is owned inside the container; the host bind target may be `ai-admin`-owned. UID mapping needs to be verified in the Bazzite Quadlet template.

## Sequencing

Slice 1 first — it's self-contained, it's the biggest reliability win, and it keeps users from losing trust in the integration while Slice 2 is in flight.

Then Slice 2 in two PRs: image + file first, voice + video as a follow-up. Each is shippable on its own.
