# Upstream Sync Playbook — keeping this fork current with NousResearch/hermes-agent

> Living runbook. Re-read and update the "Last sync" + "Known conflict hotspots"
> sections every time you pull upstream. The whole point is that updating gets
> easier each round because the hard-won knowledge lives here.

## Repo topology

| Remote | URL | Role |
|--------|-----|------|
| `origin` | github.com/brandon-btcgroup/hermes-agent | **our fork** (push here) |
| `upstream` | github.com/NousResearch/hermes-agent | the project we forked from (pull from here) |

### Branch model
- **`main`** — tracks `upstream/main`. Keep it a *clean mirror* of upstream. Never commit our features here; only ever fast-forward it. A `git pull` on `main` is **safe and correct** (it just fast-forwards to upstream).
- **`feat/simplex-*`** — our actual customization work (SimpleX adapter extensions). This is what carries our 12 SimpleX commits. The "0.16" in the name = the SimpleX cutover era it was started in, *not* the hermes version.

## One-time / each-session setup

```bash
git fetch upstream --tags          # refresh upstream refs + version tags
```

## Step 1 — How far behind are we? (read-only, run every time)

```bash
# main should report 0  0  (clean mirror). If left>0, someone committed to main — investigate.
git rev-list --left-right --count main...upstream/main

# our feature branch: "<ahead>  <behind>" relative to upstream
git rev-list --left-right --count feat/simplex-0.16...upstream/main

# version sanity
git show upstream/main:pyproject.toml | grep -i '^version'   # upstream version
git show feat/simplex-0.16:pyproject.toml | grep -i '^version'

# our own commits that need replaying
git log --oneline upstream/main..feat/simplex-0.16
```

## Step 2 — Where will it hurt? (conflict forecast)

Files that BOTH we and upstream touched since our branch point are the conflict
surface. Everything else (our brand-new files) merges clean.

```bash
base=$(git merge-base feat/simplex-0.16 upstream/main)
# overlapping files = conflict candidates
comm -12 <(git diff --name-only $base feat/simplex-0.16 | sort) \
         <(git diff --name-only $base upstream/main | sort)
# severity: compare churn on each side (added/removed)
for f in <files from above>; do
  echo "=== $f ==="; \
  echo -n 'upstream: '; git diff --numstat $base upstream/main -- "$f"; \
  echo -n 'us:       '; git diff --numstat $base feat/simplex-0.16 -- "$f"; \
done
```

## Step 3 — Update procedure (recommended)

`main` needs nothing but a fast-forward. The work is replaying our feature branch
onto the new upstream. Always do it on a throwaway branch first.

```bash
# 1. mirror upstream into main (safe fast-forward)
git checkout main && git pull   # tracks upstream/main → fast-forwards

# 2. tag a safety net before touching the feature branch
git tag pre-sync-$(date +%Y%m%d) feat/simplex-0.16

# 3. trial the replay on a scratch branch (never rebase the real one blind)
git checkout -b sync-trial feat/simplex-0.16
git rebase upstream/main          # OR: git merge upstream/main  (see note)
#   resolve conflicts (hotspots below), then:
#   - run the SimpleX tests:  uv run pytest tests/gateway/test_simplex_*.py
#   - if good, fast-forward the real branch to sync-trial and delete the trial
```

**Rebase vs merge:** rebase gives clean linear history (preferred — our branch is
only ~12 commits) but you resolve conflicts per-commit. If adapter.py conflicts
get painful across many commits, a single `git merge upstream/main` resolves the
whole delta once. Pick merge when adapter.py has been heavily reworked upstream.

**Lockfiles:** never hand-merge `uv.lock`. Take upstream's, then regenerate:
`uv lock` (or accept upstream and re-add our extras).

## Known conflict hotspots (UPDATE THIS as you learn)

| File | Why it conflicts | Strategy |
|------|------------------|----------|
| `plugins/platforms/simplex/adapter.py` | **The hard one — 18 conflict hunks** (verified 2026-06-26 trial merge). Both sides heavily rewrite it. Upstream landed native SimpleX groups/attachments/auto-accept that *overlap* our outbound/inbound media work. | Resolve by hand. First check whether upstream now does natively what our patch added — don't re-apply superseded code. See breakdown below. |
| `pyproject.toml` | We add the `[simplex]` extra (websockets); upstream edits deps. | **Auto-merges clean** (verified) — no action. |
| `tests/gateway/test_simplex_plugin.py` | Both add tests. **1 conflict hunk** (~line 251). | Keep both test sets; fix fixtures to match new adapter API. |
| `uv.lock` | Always diverges. **1 conflict hunk.** | Don't merge — take upstream + `uv lock`. |
| `website/docs/user-guide/messaging/simplex.md` | Both edit the SimpleX doc. | **Auto-merges clean** (verified) — no action. |

### adapter.py conflict breakdown (from 2026-06-26 trial merge of upstream c377e954f)

**Trivial — union-merge, keep both sides (~5 hunks, minutes):**
- Module docstring + import block (`io`/`shutil`/`subprocess` ours vs `re` upstream)
  and the `typing` import line (`Tuple` ours). Just keep all imports.

**Semantic — needs real decisions (~13 hunks). Clusters:**
- `__init__` state (~L449): ours = `_pending_responses` + replay state; upstream =
  text-batch state (`_pending_text_batch_tasks`/`_pending_text_batches`). Keep BOTH.
- `disconnect()` (~L604): ours fails pending futures; upstream cancels batch timers +
  `_mark_disconnected`. Keep BOTH cleanup paths.
- `_handle_event` corrId handling (~L742): ⚠️ **upstream independently built a
  `_pending_responses` correlation mechanism too** — different from ours. Don't
  duplicate; reconcile into one path.
- Group sender extraction (~L925) + file handling (~L968): upstream's native groups
  overlap our `chatDir.groupMember` logic. Reconcile.
- Replay dedupe (~L1080) and send/_send_and_wait machinery (~L1276, biggest hunk).
- **Media senders (~L1700–1883): ⚠️ duplicate `send_image_file`/`send_voice` defs
  appear after merge** — upstream added native attachment senders alongside ours.
  This is the prime "is our patch now superseded?" decision point. Pick one impl.
- `_standalone_send` region (~L2025).

**Effort estimate:** bounded but real — roughly a focused half-day for someone who
knows the adapter. ~5 trivial hunks are mechanical; the work is the corrId
reconciliation and deciding which media-sender implementation wins (ours vs
upstream native). Run `uv run pytest tests/gateway/test_simplex_*.py` after.

Our **new** files never conflict (safe): `_replay.py`, `_ws_client.py`, `cli.py`,
`test_simplex_bind_mount.py`, `test_simplex_cli.py`, `test_simplex_outbound_media.py`,
`test_simplex_replay.py`, `plans/*`.

## ⚠️ Strategic check before each sync

Upstream actively develops SimpleX too. Before replaying our patches, diff
upstream's SimpleX commits and ask: *has upstream made any of our customizations
redundant?* Drop superseded patches instead of fighting conflicts to keep them.

```bash
git log --oneline $base..upstream/main -- plugins/platforms/simplex/
```

## What worked (2026-06-26 sync to v2026.6.19) — READ THIS FIRST next time

The decisive lesson: **the SimpleX suite is the contract.** Resolve adapter.py to
whatever makes all 5 `tests/gateway/test_simplex_*.py` files green — that proves
both our features and upstream's tested behaviour survive. Run it with the venv:
`./venv/bin/pytest tests/gateway/test_simplex_*.py -q` (NOTE: `python`/`uv run`
weren't on PATH in the sandbox; `.venv` had no pytest — use **`./venv/bin/pytest`**).

Winning strategy for adapter.py (18 hunks): **reverse-merge.** Don't hand-resolve
hunks — `git checkout --theirs` first to see what pure-upstream breaks (the failing
tests = exact feature gaps), then `git checkout HEAD -- adapter.py` to base on OURS
and graft only upstream's *additive* deltas on top. Our replay + bind-mount media
have NO upstream equivalent, so "favor upstream" can't apply to them; favouring
upstream only resolved the genuinely-duplicated plumbing.

Concrete grafts that were needed this round:
- Upstream added **DOCUMENT classification** for non-image/non-audio inbound files
  + a shared-filesystem direct-path fallback. Grafted into our handler alongside
  the bind-mount fetch (containerised) path.
- Upstream renamed the handler `_handle_new_chat_item` → **`_handle_chat_item`**.
  Converged our name + all test refs to match (reduces future conflict surface).
- `pyproject.toml`, `simplex.md` auto-merged. `uv.lock` → take upstream's.
- Text batching: re-implemented ourselves (not taken from upstream) so it is
  **replay-safe** — skipped during replay, cursor advances only on flush. **On by
  default** at `HERMES_SIMPLEX_TEXT_BATCH_DELAY=0.8`; set to 0 to disable. Commit
  `3d37080bd`. NOT grafted: upstream's `MEDIA:<path>` tag handling in `send()` (our
  `send()` uses the multiline-safe structured `/_send … json` form; upstream's plain
  `@id text` DM path truncates at the first newline).

## ⚠️ 0.18 lesson: the unit suite is NOT the framework contract

The 0.18 merge passed all 131 SimpleX unit tests but **broke the live gateway**:
upstream widened the adapter contract and our overrides kept the old signatures.
Two runtime `TypeError`s that no unit test caught (they call adapters directly,
not via the framework's real convention):

1. `connect()` — framework now calls `adapter.connect(is_reconnect=...)`
   (`gateway/run.py`). Ours was `connect(self)`. → SimpleX dead at startup.
2. **Media senders** — framework + base class dispatch by keyword
   (`send_image_file(image_path=)`, `send_voice(audio_path=)`,
   `send_video(video_path=)`, `send_document(file_path=)`,
   `send_animation(animation_url=)`). Ours took positional `path`. → all
   outbound media would `TypeError`.

Fixed in `458ae5e0d` (signatures aligned to `base.py` + `**kwargs` on senders;
regression tests exercise the real keyword convention). **After any future
sync, run this signature-drift audit** (catches the whole class at once):

```bash
./venv/bin/python -c "
import inspect
from tests.gateway._plugin_adapter_loader import load_plugin_adapter
A = load_plugin_adapter('simplex').SimplexAdapter
from gateway.platforms.base import BasePlatformAdapter as B
for n,f in inspect.getmembers(A, inspect.isfunction):
    if n.startswith('__') or not hasattr(B,n) or getattr(A,n) is getattr(B,n): continue
    so=inspect.signature(f); sb=inspect.signature(getattr(B,n))
    if any(p.kind==p.VAR_KEYWORD for p in so.parameters.values()): continue
    miss=set(sb.parameters)-set(so.parameters)
    if miss: print('DRIFT', n, sorted(miss))
"
```

**Still open after 0.18 (non-blocking):** `hermes simplex list/join` CLI is not
registered on 0.18 — the plugin's `register_cli`/`ctx.register_cli_command`
wiring didn't survive the merge (the discovery gate `_plugin_cli_discovery_needed`
is fine; registration is the gap). Bot is unaffected. TODO: reconcile plugin CLI
registration against 0.18 `hermes_cli/main.py`.

## Deployment (2026-07-12) — ai-server.localdomain

- Server was on `fix/simplex-event-dispatch` **v0.15.1** (never cut to 0.16).
  Jumped straight to **0.18.2** on `feat/simplex-0.16`.
- `.env` already compatible (`SIMPLEX_HOME_CHANNEL`/`SIMPLEX_WS_URL`/…); no migration.
- Gateway = systemd `--user` `hermes-gateway.service` (uses `.venv`); SimpleX daemon
  = podman `simplex-chat-hermes.service` on `127.0.0.1:5225` (untouched).
- **Two-venv footgun:** `~/.local/bin/hermes` pointed at a stale `venv` (new source,
  old deps); the *service* correctly uses `.venv`. Reinstalled deps into `.venv`
  (`uv pip install --python .venv/bin/python -e ".[all,dev,simplex]"`) and repointed
  the CLI symlink → `.venv`. Backups: `~/.hermes/{.env,config.yaml}.bak-20260712`,
  `~/.local/bin/hermes.bak-0.15`. Rollback branch: `fix/simplex-event-dispatch`
  @ `d31a04341`; safety tag `pre-sync018-20260712`.
- **Verified working:** gateway active, `NRestarts=0`; live WS `ESTAB` from hermes
  (pid) → daemon `:5225`; running `connect()` sig = `(self, *, is_reconnect=False)`.

## Last sync state

- **Date:** 2026-07-12 — **DONE (trial).** Merge committed on branch
  `sync-trial-0.18` (`7a6a4af29`), built **on top of `sync-trial`** (not the stale
  `feat/simplex-0.16`). Safety tag `pre-sync018-20260712` → `sync-trial` (`3d37080bd`).
- **Upstream HEAD merged:** `7b5ba2054` · version **0.18.2** · tag `v2026.7.7.2`
- **Result:** SimpleX suite **131 passed / 1 skipped**; plugin loads cleanly against
  the 0.18.2 base adapter.
- **This round was EASY — key insight:** because the June sync already reconciled
  `adapter.py` into `sync-trial`, and **upstream has not touched
  `plugins/platforms/simplex/` since** (same 4 commits as June), `adapter.py` did
  **NOT re-conflict**. The 2170-commit 0.17→0.18 gap was almost entirely non-simplex
  framework churn. **Only conflict this round: `uv.lock`** (took upstream + `uv lock`;
  `websockets` re-resolved fine). `pyproject.toml` + `simplex.md` auto-merged.
- **Semantic-drift watch (no textual conflict, tests green):** `gateway/platforms/base.py`
  changed **+402/−85** — new authorization layer (`set_authorization_check` /
  `_is_sender_authorized`) and MEDIA-tag path machinery (`_normalize_media_tag_path`,
  `strip_media_directives_for_display`). Our adapter subclasses base cleanly and the
  suite passes; if we ever wire `SIMPLEX_ALLOWED_USERS` into the new auth hook, revisit here.
- **Branch topology note:** `feat/simplex-0.16` (0.16.0) **never received** the June
  `sync-trial` work — it is 2 syncs behind. The leading branch is now `sync-trial-0.18`.
- **Next:** review `sync-trial-0.18`, then fast-forward `feat/simplex-0.16` (or a fresh
  `feat/simplex-0.18`) to it; also fast-forward `main` to `upstream/main`. Then clean up
  old `sync-trial` + `pre-sync-*` tags.

### Prior sync (2026-06-26 → 0.17.0 / `v2026.6.19`)
- Merge `ca73fb863` on branch `sync-trial`; SimpleX suite 125 passed / 1 skipped.
  Grafted upstream DOCUMENT classification + `_handle_chat_item` rename. This is the
  base `sync-trial-0.18` builds on. **Never integrated into `feat/simplex-0.16`.**
