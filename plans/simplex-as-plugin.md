# SimpleX as a hermes-agent platform plugin — concrete plan

Branch: `feat/simplex-plugin` (off `main`, just cut).
Replaces the in-tree fork on `feat/simplex-platform` (3,749 lines across 24 files).

## Verified facts

1. **Platform enum is plugin-aware.** `Platform._missing_()` (gateway/config.py:111)
   creates dynamic enum members on demand for any directory under
   `plugins/platforms/<name>/` that has `__init__.py` + `plugin.yaml`. So
   `Platform("simplex")` works without editing the enum.
2. **GatewayRunner checks the plugin registry first** (gateway/run.py ~4195).
   Plugin platforms get dispatched generically — the `elif platform ==
   Platform.SIMPLEX:` branch we added at line 4249 is pure dead weight.
3. **Auth env-var lookup is registry-driven** (gateway/run.py:4437-4443).
   `register_platform(allowed_users_env=…, allow_all_env=…)` is enough.
4. **IRC and Teams have ZERO upstream touchpoints** in toolsets.py,
   prompt_builder.py, display_config.py, or status.py. Confirmed by grep.
   The plugin contract handles all of them via `register_platform` kwargs:
   `platform_hint=`, `emoji=`, `max_message_length=`, `pii_safe=`, etc.
5. **CLI subcommands** are plugin-extensible via `ctx.register_cli_command(name,
   setup_fn)` (hermes_cli/plugins.py:301).

## File-by-file: what gets dropped

| Original file | Diff | Action on `feat/simplex-plugin` |
|---|---|---|
| `gateway/config.py` `SIMPLEX = "simplex"` | +2 | DROP — `_missing_()` handles it |
| `gateway/config.py` `_PLATFORM_CONNECTED_CHECKERS` entry | +3 | DROP — register via `is_connected=` kwarg |
| `gateway/config.py` `_apply_env_overrides` SimpleX block | +29 | DROP — adapter reads env in its own `__init__` (IRC pattern) |
| `gateway/run.py:4249` SimpleX adapter dispatch | +7 | DROP — registry handles it |
| `gateway/run.py:4408,4435` SIMPLEX_ALLOWED_USERS / ALLOW_ALL | (in maps) | DROP — register via `allowed_users_env=`, `allow_all_env=` |
| `agent/prompt_builder.py` `PLATFORM_HINTS["simplex"]` | +10 | DROP — register via `platform_hint=` |
| `gateway/display_config.py` `_PLATFORM_DEFAULTS["simplex"]` | +1 | DROP — register via `emoji=` (and friends) |
| `hermes_cli/main.py` `add_simplex_subparser` import + call | +6 | DROP — register via `register_cli_command` |
| `hermes_cli/platforms.py` SIMPLEX entry | +1 | DROP — registry-driven |
| `hermes_cli/status.py` SimpleX env tuple | +1 | DROP (cosmetic — see "Accepted limits") |
| `tools/send_message_tool.py` SIMPLEX branch + `_send_simplex` | +44 | DROP — falls through to `_send_via_adapter` (live gateway path) |
| `toolsets.py` `hermes-simplex` toolset entry | +8 | DROP (cosmetic — see "Accepted limits") |
| `gateway/platforms/simplex.py` (1159 lines) | +1159 | MOVE → `plugins/platforms/simplex/adapter.py` |
| `gateway/platforms/simplex_client.py` (327 lines) | +327 | MOVE → `plugins/platforms/simplex/client.py` |
| `hermes_cli/simplex.py` (228 lines) | +228 | MOVE → `plugins/platforms/simplex/cli.py`, called via `register_cli_command` |
| `tests/gateway/test_simplex.py` (985 lines) | +985 | MOVE → `tests/plugins/platforms/simplex/test_adapter.py` |
| `tests/gateway/test_simplex_client.py` (349 lines) | +349 | MOVE → `tests/plugins/platforms/simplex/test_client.py` |
| `tests/e2e/test_simplex_smoke.py` (52 lines) | +52 | KEEP IN PLACE (e2e is e2e) |
| `simplex-chat-hermes.{env,container}.example` | +103 | MOVE → `plugins/platforms/simplex/examples/` |
| `website/docs/user-guide/messaging/simplex.md` | +318 | KEEP — docs path stays the same |
| `website/docs/user-guide/messaging/index.md` | +1 | KEEP |
| `website/docs/reference/environment-variables.md` | +7 | KEEP |
| `pyproject.toml` `websockets` optional dep | +2 | KEEP — but namespace under `[project.optional-dependencies] simplex` |
| `README.md` mention | +4 | KEEP |
| `.env.example` SimpleX block | +8 | KEEP |
| `plans/simplex-v2.md` | +97 | KEEP (historical) |

## Final layout

```
plugins/platforms/simplex/
├── __init__.py          # from .adapter import register
├── plugin.yaml          # name/kind/version/requires_env
├── adapter.py           # SimplexAdapter(BasePlatformAdapter) + register(ctx)
├── client.py            # SimplexChatClient (WS protocol)
├── cli.py               # add_simplex_subparser + handlers
└── examples/
    ├── env.example
    └── container.example
```

## Accepted limits (cosmetic)

Three things would *not* work with a pure plugin:

1. **`hermes status` won't list SimpleX as a row** — the table is a static
   dict in status.py keyed by capitalized name. Either upstream needs to
   make this registry-driven, or we live without it. Low impact: `hermes
   gateway status` still shows it.
2. **No dedicated `hermes-simplex` toolset** — toolsets.py is a static
   dict. The adapter is still reachable through `hermes-gateway`, just
   without a finer-grained per-platform toolset name.
3. **One-shot sends (`send_message` tool / cron without live gateway)** —
   currently routes through `_send_via_adapter` which needs a live
   gateway adapter. If a cron job needs to send to a SimpleX group while
   no gateway is running, it would fail. The in-tree fork's `_send_simplex`
   was a workaround. **If this matters** we file a small upstream PR adding
   a "one-shot send" capability to the plugin contract; otherwise drop it.

All three could become small upstream PRs to the hermes-agent project (each
benefits IRC, Teams, and any future plugin equally — they're not
SimpleX-specific gaps).

## Cherry-pick strategy

We do NOT cherry-pick the original commits — they touched cross-cutting
files we are dropping. Instead, we extract just the file CONTENT from
`feat/simplex-platform`:

```
git show feat/simplex-platform:gateway/platforms/simplex.py        > plugins/platforms/simplex/adapter.py
git show feat/simplex-platform:gateway/platforms/simplex_client.py > plugins/platforms/simplex/client.py
git show feat/simplex-platform:hermes_cli/simplex.py               > plugins/platforms/simplex/cli.py
git show feat/simplex-platform:tests/gateway/test_simplex.py       > tests/plugins/platforms/simplex/test_adapter.py
git show feat/simplex-platform:tests/gateway/test_simplex_client.py > tests/plugins/platforms/simplex/test_client.py
git show feat/simplex-platform:tests/e2e/test_simplex_smoke.py     > tests/e2e/test_simplex_smoke.py
git show feat/simplex-platform:simplex-chat-hermes.env.example     > plugins/platforms/simplex/examples/env.example
git show feat/simplex-platform:simplex-chat-hermes.container.example > plugins/platforms/simplex/examples/container.example
git show feat/simplex-platform:website/docs/user-guide/messaging/simplex.md > website/docs/user-guide/messaging/simplex.md
# … docs files etc.
```

Then edit each file:
- adapter.py: replace `Platform.SIMPLEX` with `Platform("simplex")`, drop the
  `super().__init__(config, Platform.SIMPLEX)` enum import path. Add the
  `register(ctx)` function at the bottom (mirroring `irc/adapter.py`'s
  `register()`).
- client.py: just an import-path adjustment.
- cli.py: turn `add_simplex_subparser(subparsers)` into a `setup_fn` that
  `register_cli_command` calls.
- tests: swap `Platform.SIMPLEX` → `Platform("simplex")`, update import paths.

Estimated work: ~1 day of mechanical edits + test runs.

## Commits we'd make on `feat/simplex-plugin`

1. `feat(simplex): bundled platform plugin (adapter + client + cli)`
2. `test(simplex): port adapter + client + e2e tests under plugins/platforms/`
3. `docs(simplex): refresh setup guide for plugin layout`
4. `chore(simplex): pin websockets under [optional-dependencies] simplex`

Each one stands alone; reviewer can read in order.

## Open question to confirm with user

Of the three "accepted limits" above — which (if any) are dealbreakers?
That decides whether we need to file 1-3 small upstream PRs before merging
this branch, or whether we can ship as-is.
