# RFC: Typer-native plugin command construction

- **Date:** 2026-07-19
- **Status:** Proposed
- **Supersedes:** the incremental patches on `fix/typer-vendored-click-group-mounting` (PR #137), which patched the symptoms site-by-site. This RFC replaces that approach with a root-cause fix.

## Problem

graftpunk's `gp <site> …` plugin CLI is broken under **typer ≥ 0.26**. On a fresh install that resolves a modern typer:

- `gp <site>` reports **"No such command"** — plugin subcommands don't mount.
- Running a mounted command (e.g. `gp shopkeep export list`) **crashes during argument parsing** (`AttributeError` on the context; or, in the current click 8.3.x line, options silently arrive as `None` with their declared defaults dropped).

typer < 0.26 (what `uv.lock` currently pins: 0.21.1) is unaffected, so graftpunk's own CI never sees the break — but any consumer resolving typer ≥ 0.26 (e.g. grocerbot, whose environment resolves typer 0.26.8) gets a broken `gp`.

## Root cause (source-verified)

typer 0.26 **vendored its own copy of Click** into `typer._click` (self-documented as "adapted from Click 8.3.1" — `typer/_click/__init__.py:1-3`). graftpunk's CLI **hand-builds external `click.Option`/`click.Argument`/`click.Group` objects and hands them to Typer's runtime**, which now parses them with its *vendored* Click. Click's parse path assumes the `Parameter` and the `Context` come from **one** Click implementation — it reads per-instance context state that only that Click's `Context.__init__` establishes:

- **Crash:** external `Parameter.handle_parse_result` reads context state the vendored `Context.__init__` never set (`_param_default_explicit` in click 8.2.x; the `UNSET`-sentinel machinery in 8.3.x). Cross-implementation `Parameter`↔`Context` is an unsupported contract.
- **Dropped defaults (reproducible in the current 8.3.1 / typer 0.27 pair):** external Click's missing-value ladder uses an `UNSET` sentinel (`click/_utils.py:22`, `click/core.py:2308-2333`); Typer's vendored `Context.lookup_default` returns **`None`** (`typer/_click/core.py:445`). External Click tests `None is not UNSET` → true → binds the value to `None` (source mislabeled `DEFAULT_MAP`) and **skips the block that would apply the Option's real default**.
- **Context / ParameterSource:** external `click.get_current_context()` and Typer's live context use **two different thread-local stacks** (`click/globals.py` vs `typer/_click/globals.py`), and `ParameterSource` is **two distinct enum classes**. External-Click lookups from inside a Typer callback see nothing / never compare equal.

The three symptoms are one thing: **a cross-Click-boundary.** They are not Typer bugs to patch around.

## Goals

- `gp <site> <command>` mounts and executes correctly on **typer 0.21 through the latest** (0.27+), verified in CI on both generations.
- The plugin author contract is **unchanged**: `SitePlugin`, `CommandSpec`, `PluginParamSpec`, `@command`, `LoginConfig`, `get_commands()` — real plugins (`graftpunk-plugins-grocerbot` shopkeep, `surety`) work untouched.
- **Zero external-Click objects enter Typer's runtime** anywhere under `cli/`.
- Remove the accumulated boundary workarounds rather than add more.

## Non-goals

- Changing the `SitePlugin`/`CommandSpec`/`PluginParamSpec` public API.
- Supporting Click constructs no plugin uses (`click.Choice`, `click.Path`, custom `click.ParamType`, param callbacks) — these are **rejected loudly** (see Param mapping), to be added only if a real plugin needs them.
- Touching the pandas/session/observe/token internals of the execution pipeline — that logic is preserved verbatim; only the parameter plumbing changes.

## Approach: Typer-native construction (signature synthesis)

Stop hand-building Click params. Let **Typer** build every parameter with **its** Click, so there is no boundary. Concretely (Reader-verified against typer 0.27 source):

- `typer.get_params_from_function` reads `inspect.signature(func, eval_str=True)` (`typer/utils.py:108`) — which **honors a `func.__signature__` override**. Synthesizing a function's `__signature__`/`__annotations__` therefore fully controls the params Typer builds.
- Registering the function via the **public** `app.command()(fn)` gets Typer's context-injection and type convertors for free (`get_callback`, `typer/main.py:1496-1527`).
- A `ctx: typer.Context` parameter is injected natively by Typer (its own context) — so explicit-vs-default detection uses `ctx.get_parameter_source(...)` against Typer's own `ParameterSource`, with no external Click anywhere.
- Groups compose via sub-`typer.Typer()` + `app.add_typer(sub, name=…)`, nesting to any depth (`typer/main.py:915`, `1302-1310`).

The load-bearing `get_params_from_function` is **byte-identical between typer 0.26.8 and 0.27**, and the surface relied on (`app.command`, `add_typer`, `typer.Option/Argument/Context`) is Typer's **public** API. Rejected alternatives: Tier-A `ParamMeta` + hand-written callback wrapper (works but leans on internals and re-implements Typer's wrapper); pure-external-Click CLI (larger rewrite, discards Typer's ergonomics).

## Design

### 1. New module: `cli/command_factory.py`

A single builder that turns a `(plugin, CommandSpec)` into a Typer-registered command:

- Synthesize a function whose `__signature__` is: `ctx: typer.Context`, then one parameter per `PluginParamSpec`, then the three built-ins `--format/-f`, `--view`, `--output/-o` — each declared with `typer.Option(...)` / `typer.Argument(...)` and a **real type object** annotation.
- The function **body is the existing execution pipeline** (session load → observe → token inject → `execute_plugin_command` → 403-refresh → session persist → `format_output`), closed over `plugin`/`cmd_spec`. It reads `ctx` and the parsed params from its kwargs.
- Register with `app.command(name=cmd_spec.name, **cmd_spec.click_kwargs)(fn)` (command-level `help`/`hidden`/`deprecated`/`epilog` pass through as today).

### 2. Param mapping: `PluginParamSpec` → Typer parameter

`PluginParamSpec` stays `{name, is_option, click_kwargs}`. A pure function maps `click_kwargs` onto a `typer.Option`/`typer.Argument` declaration, supporting exactly the surface real plugins + tests use:

| `click_kwargs` key | Typer mapping |
|---|---|
| `type` (`str`/`int`/`bool`/`float`) | the annotation type |
| `required` | option: no default / `...`; argument: required by position |
| `default` | Typer `Option/Argument` default |
| `help` | `help=` |
| `is_flag` (incl. bool-default-`False` auto-detect) | `bool` flag param |
| `show_default` | `show_default=` |
| `envvar` | `envvar=` |
| `nargs=-1` | variadic (`list`/tuple annotation) |

- Option flag name is derived as `--{name.replace('_','-')}` (unchanged); short flags only on the three built-ins.
- A **required option** (e.g. surety's `docno`) maps to a required flag, not a positional.
- **Any `click_kwargs` key outside the supported set** (`callback`, `click.Choice`/`click.Path` instances, custom `ParamType`, etc.) raises `PluginError` at registration with a clear message naming the plugin, command, param, and unsupported key. No silent behavior drift; expand the table when a real plugin needs more.

### 3. Group composition

`register_plugin_commands` builds, per plugin, a `typer.Typer()` sub-app; for each grouped command (`cmd_spec.group`, dotted) it walks/creates nested `typer.Typer()` sub-apps and registers the leaf command on the innermost. The site sub-app is attached to the root app via `add_typer(name=site_name, help=plugin.help_text)`. This **replaces `_ensure_group_hierarchy`** and the manual group building.

### 4. Deletions (the boundary-hack layer)

Because Typer now owns building, mounting, and parsing, the following become dead and are removed:

- `GraftpunkApp.__call__` override + `add_plugin_group` + `_plugin_groups` (plugin sub-apps are attached with `add_typer`; Typer's normal invocation runs them). `GraftpunkApp` collapses to a plain `typer.Typer` (or a thin subclass retained only if it still owns `_plugin_session_map`/teardown bookkeeping).
- `_is_command_group`, `_build_click_app`, `_ensure_group_hierarchy`, `_usage_error_types`/`_USAGE_ERRORS`, `_current_context`.
- The callback's typer-≥0.26 compensations: the "re-apply declared defaults when the vendored runtime left them `None`" loop and the "`--format` carries no Click default so absent==None" hack — Typer applies defaults natively, and explicit-vs-default becomes `ctx.get_parameter_source("format")`.

### 5. Execution pipeline (preserved, plumbing modernized)

The 12-step callback behavior is preserved exactly (session `needs_session` resolution and the `SessionNotFoundError`/`PluginError`/generic → `SystemExit(1)` funnel; `session.gp_base_url`; observe context from the root ctx `obj`; token injection; `CommandContext` construction; `execute_plugin_command(spec, ctx, plugin_formatters=…, **kwargs)`; 403 token-refresh retry with `_session_dirty`; `update_session_cookies` on `saves_session`/dirty; `format_output(..., user_explicit=…, view_args=…, output_path=…)`; the `CommandError`/`PluginError`/generic error→exit funnel). Two mechanical changes:

- `format`/`view`/`output` are ordinary Typer options now; the body reads them from kwargs with their Typer-applied defaults.
- `user_explicit` (the `--format` hint gate) = `ctx.get_parameter_source("format") == ParameterSource.COMMANDLINE`, using Typer's context and Typer's enum.

`execute_plugin_command`, `CommandContext`, and `GraftpunkClient._execute_command` are **unchanged** — they're already backend-agnostic and never touched Click.

### 6. `login` command

The auto-registered, **param-less** login command is produced by the same factory with a fixed zero-parameter signature (credentials come from env vars / interactive prompt inside the login callable, unchanged).

### 7. Full `cli/` audit

Sweep every module under `cli/` for external-Click usage and route it through Typer / the vendored Click that Typer runs:

- `login_commands.py`: `create_login_command` goes through the factory; any `click.prompt` for interactive credentials uses `typer.prompt`.
- `main.py`: static commands are already Typer decorators; replace any lingering `click.Choice`/`click.Path`/`click.get_current_context` with `typer.Option(...)`-native equivalents / `typer.Context`.
- Result: **no `import click` for object construction anywhere in `cli/`.** (Where the *vendored* enum/context is genuinely needed, import from what Typer re-exports, never from external `click` while a Typer context is live.)

## Testing & verification

- **Permanent typer-version CI matrix.** Add a CI dimension that installs `typer==0.21.*` and `typer` latest (≥0.26) and runs the plugin CLI suite under each. This is the guard that would have caught the original break (the pinned `uv.lock` hides it).
- **Version-agnostic unit tests** that assert the invariants regardless of installed typer (a command's declared default is applied when the flag is absent; `--format`-explicit detection; nested-group mounting; required option vs argument; variadic arg; param-less login).
- **Real-plugin end-to-end**, on both typer generations: `gp shopkeep {login, export list/get, reorder, import run}` and `gp surety <commands>` — mount, parse, and execute (read-only where live).
- Green on the existing 3.11/3.12/3.13 matrix.

## Risks & mitigations

- **Reliance on Typer internals.** Mitigated by staying on the public `app.command`/`add_typer`/`typer.Option/Argument/Context` surface (Tier B); the one internal contract (`inspect.signature` honoring `__signature__`) is stable and covered by the version matrix.
- **Dynamic-signature edge cases** (variadic args, bool flags, required options). Mitigated by the version-agnostic invariant tests enumerating each construct, plus fail-loud rejection of anything unmapped.
- **Behavioral drift in help/error rendering.** Accepted: the documented interface (names, options, args, types, defaults, runtime behavior) is preserved; incidental help-panel/error formatting becomes Typer's, consistent with the rest of `gp`.
- **`CommandResult`/formatter hierarchy.** Untouched — `format_output` and the 3-level override system are consumed exactly as today.

## Rollout

1. Land this refactor on `refactor/typer-native-plugin-commands`; close PR #137 as superseded.
2. Keep `typer` **unpinned** (`>=0.9.0`); the version matrix — not a pin — is the safeguard.
3. Release as graftpunk **1.10.0** (behavior fix + internal refactor; no public API change) via the existing tag-push Trusted-Publishing pipeline.
4. grocerbot picks it up by bumping its floor to `graftpunk>=1.10.0` and re-locking; `gp shopkeep …` then works under grocerbot's typer 0.26.8.

## References (source)

- typer construction: `typer/main.py:1173` (`get_command`), `:1373`/`:1632` (params/convertors, `get_click_param`), `:1496-1527` (`get_callback` + context injection), `:915`/`:1302-1310` (`add_typer` + recursion); `typer/utils.py:107-186` (`get_params_from_function`, `inspect.signature`+`get_type_hints`); `typer/models.py:620/280/388/516/200`.
- Click boundary: `click/core.py:2294-2335` (`consume_value`/`UNSET` ladder), `:716` (`lookup_default`→`UNSET`), `:143/158` (`ParameterSource` enum), `:440` (`_parameter_source` init); `typer/_click/core.py:445` (`lookup_default`→`None`), `:107` (vendored `ParameterSource`); `click/globals.py` vs `typer/_click/globals.py` (separate stacks).
- graftpunk contract: `plugins/cli_plugin.py` (`PluginParamSpec`, `CommandSpec`, `@command`, `LoginConfig`), `client.py` (`execute_plugin_command`), `cli/plugin_commands.py` (`_create_plugin_command` + callback), `cli/login_commands.py`, `plugins/formatters.py` (`format_output`).
