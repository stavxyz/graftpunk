---
type: spec
validated:
  sha: ecb69f08145faf8f8af0c7305f4275a7c08dca14
  date: 2026-07-19T18:49:11Z
  reviewers: [fact-check, solid-hygiene]
  findings:
    critical: 0
    important: 3
    medium: 4
    low: 5
    nitpick: 3
  net_negative_remaining: 0
---

# RFC: Typer-native plugin command construction

- **Date:** 2026-07-19
- **Status:** Proposed
- **Supersedes:** the incremental patches on `fix/typer-vendored-click-group-mounting` (PR #137), which patched the symptoms site-by-site. This RFC replaces that approach with a root-cause fix.

## Problem

graftpunk's `gp <site> …` plugin CLI is broken under **typer ≥ 0.26**. On a fresh install that resolves a modern typer:

- `gp <site>` reports **"No such command"** — plugin subcommands don't mount.
- Running a mounted command (e.g. `gp shopkeep export list`) **crashes during argument parsing** (an `AttributeError` on the context in the current click 8.4.x line; with click 8.3.x, options instead silently arrive as `None` with their declared defaults dropped). *(Verified 2026-07-19: was incorrect — 8.3.x is not the current click line; grocerbot's installed click is 8.4.2, where the symptom is the crash.)*

typer < 0.26 (what `uv.lock` currently pins: 0.21.1) is unaffected, so graftpunk's own CI never sees the break — but any consumer resolving typer ≥ 0.26 (e.g. grocerbot, whose environment resolves typer 0.26.8) gets a broken `gp`.

## Root cause (source-verified)

typer 0.26 **vendored its own copy of Click** into `typer._click` (self-documented as "adapted from Click 8.3.1" — `typer/_click/__init__.py:1-3`). graftpunk's CLI **hand-builds external `click.Option`/`click.Argument`/`click.Group` objects and hands them to Typer's runtime**, which now parses them with its *vendored* Click. Click's parse path assumes the `Parameter` and the `Context` come from **one** Click implementation — it reads per-instance context state that only that Click's `Context.__init__` establishes:

- **Crash:** external `Parameter.handle_parse_result` reads context state the vendored `Context.__init__` never set (`_param_default_explicit` in click 8.4.x — grocerbot's installed 8.4.2 initializes it at `click/core.py:509` and reads it at `:2683`; the `UNSET`-sentinel machinery in 8.3.x). Cross-implementation `Parameter`↔`Context` is an unsupported contract. *(Verified 2026-07-19: was incorrect — `_param_default_explicit` was introduced in click 8.4.0, not 8.2.x.)*
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

- `typer.utils.get_params_from_function` reads `inspect.signature(func, eval_str=True)` (`typer/utils.py:108`) — which **honors a `func.__signature__` override**. Synthesizing a function's `__signature__`/`__annotations__` therefore fully controls the params Typer builds.
- Registering the function via the **public** `app.command()(fn)` gets Typer's context-injection and type convertors for free (`get_callback`, `typer/main.py:1496-1527`).
- A `ctx: typer.Context` parameter is injected natively by Typer (its own context) — so explicit-vs-default detection uses `ctx.get_parameter_source(...)` against Typer's own `ParameterSource`, with no external Click anywhere.
- Groups compose via sub-`typer.Typer()` + `app.add_typer(sub, name=…)`, nesting to any depth (`typer/main.py:915`, `1302-1310`).

The load-bearing `get_params_from_function` is **byte-identical between typer 0.26.8 and 0.27**, and the surface relied on (`app.command`, `add_typer`, `typer.Option/Argument/Context`) is Typer's **public** API. Rejected alternatives: Tier-A `ParamMeta` + hand-written callback wrapper (works but leans on internals and re-implements Typer's wrapper); pure-external-Click CLI (larger rewrite, discards Typer's ergonomics).

## Design

### 1. New module: `cli/command_factory.py`

A single builder that turns a `(plugin, CommandSpec)` into a Typer-registered command:

- Synthesize a function whose `__signature__` is: `ctx: typer.Context`, then one parameter per `PluginParamSpec`, then the three built-ins `--format/-f`, `--view`, `--output/-o` — each declared with `typer.Option(...)` / `typer.Argument(...)` and a **real type object** annotation.
- The factory is **body-parameterized**: it takes the signature spec *and a body callable*, and synthesizes the registered function around the body it is given — construction and execution stay separate responsibilities.
- The plugin-command body is the existing execution pipeline, extracted verbatim behind its own named seam — `run_plugin_command(plugin, cmd_spec, ctx, **kwargs)` (session load → observe → token inject → `execute_plugin_command` → 403-refresh → session persist → `format_output`). The factory closes it over `plugin`/`cmd_spec`; it reads `ctx` and the parsed params from its kwargs.
- Register with `app.command(name=cmd_spec.name, **cmd_spec.click_kwargs)(fn)` (command-level `help`/`hidden`/`deprecated`/`epilog` pass through as today).

> **Design note (2026-07-19):** revised per SOLID review — the factory owns construction only (signature synthesis, param mapping, registration); the run-time pipeline lives behind its own named function so future session/retry/formatting changes land in the pipeline, not the construction module. This also lets §6's login command reuse the factory with a different body (composition, not internal branching).

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

- Option flag name is derived as `--{name.replace('_','-')}` (unchanged); short flags exist only on two of the built-ins (`--format/-f`, `--output/-o`; `--view` has none).
- A **required option** (e.g. surety's `docno`) maps to a required flag, not a positional.
- **Any `click_kwargs` key outside the supported set** (`callback`, `click.Choice`/`click.Path` instances, custom `ParamType`, etc.) raises `PluginError` at registration with a clear message naming the plugin, command, param, and unsupported key. No silent behavior drift; expand the table when a real plugin needs more.
- The table above **becomes the documented contract for `click_kwargs`** — the `cli_plugin.py` docstrings that promise the dict is "splatted directly into `click.Option()`" are updated to describe this mapping instead. The `click_kwargs` name is retained for API stability; a rename/alias is deliberately out of scope (future, separate deprecation).

> **Design note (2026-07-19):** per SOLID review — `click_kwargs` is now graftpunk's own small param contract interpreted by the Typer mapper, no longer a raw Click passthrough; the docs must say so to keep plugin authors' mental model accurate.

### 3. Group composition

`register_plugin_commands` builds, per plugin, a `typer.Typer()` sub-app; for each grouped command (`cmd_spec.group`, dotted) it walks/creates nested `typer.Typer()` sub-apps and registers the leaf command on the innermost. The site sub-app is attached to the root app via `add_typer(name=site_name, help=plugin.help_text)`. This **replaces `_ensure_group_hierarchy`** and the manual group building. A name collision between a group segment and an existing leaf command — which today is logged as `command_group_conflict` and silently mangles the group — becomes a loud `PluginError` at registration.

### 4. Deletions (the boundary-hack layer)

Because Typer now owns building, mounting, and parsing, the following become dead and are removed:

- `GraftpunkApp.__call__` override + `add_plugin_group` + `_plugin_groups` (plugin sub-apps are attached with `add_typer`; Typer's normal invocation runs them). `GraftpunkApp` collapses **fully** to a plain `typer.Typer` — the session map (`_plugin_session_map`) and teardown bookkeeping (`_registered_plugins_for_teardown` + `atexit` hook) are already module-level state in `plugin_commands.py`, not class state, and keep that single module-level owner unchanged. *(Verified 2026-07-19: was incorrect — the earlier "thin subclass retained for bookkeeping" branch can never trigger; the class holds no such state.)*
- `_ensure_group_hierarchy` and its `isinstance(..., click.Group)` group-walk. **Baseline:** this deletion list is written against `main` — PR #137 is closed unmerged (Rollout step 1), so its `_is_command_group`/`_build_click_app` helpers never land and need no removal here. *(Verified 2026-07-19: was incorrect — the earlier list named symbols that exist only in superseded/abandoned #137 work-in-progress, not in any committed baseline.)*
- The callback's cross-Click context lookup: at HEAD, explicit-`--format` detection calls external `click.get_current_context(silent=True)` and compares against external `click.core.ParameterSource.COMMANDLINE` (`plugin_commands.py:226-231`) — lookups that return `None`/never match under a vendored-Click runtime. Replaced by the Typer-injected `ctx: typer.Context` + Typer's own `ParameterSource` (§5); Typer applies option defaults natively. *(Verified 2026-07-19: was incorrect — the earlier bullet described "typer-≥0.26 compensation" code that exists only in abandoned work-in-progress; at HEAD `--format` is declared with `default="json"`.)*

### 5. Execution pipeline (preserved, plumbing modernized)

The 12-step callback behavior is preserved exactly (session `needs_session` resolution and the `SessionNotFoundError`/`PluginError`/generic → `SystemExit(1)` funnel; `session.gp_base_url`; observe context from the root ctx `obj`; token injection; `CommandContext` construction; `execute_plugin_command(spec, ctx, plugin_formatters=…, **kwargs)`; 403 token-refresh retry with `_session_dirty`; `update_session_cookies` on `saves_session`/dirty; `format_output(..., user_explicit=…, view_args=…, output_path=…)`; the `CommandError`/`PluginError`/generic error→exit funnel). Two mechanical changes:

- `format`/`view`/`output` are ordinary Typer options now; the body reads them from kwargs with their Typer-applied defaults.
- `user_explicit` (the `--format` hint gate) = `ctx.get_parameter_source("format") == ParameterSource.COMMANDLINE`, using Typer's context and Typer's enum.

`execute_plugin_command`, `CommandContext`, and `GraftpunkClient._execute_command` are **unchanged** — they're already backend-agnostic and never touched Click.

### 6. `login` command

The auto-registered, **param-less** login command is produced by the same body-parameterized factory (§1) with a fixed zero-parameter signature and login's own body: credential resolution — env vars / interactive prompts, exactly as today (`login_commands.py:128-146`) — followed by the login callable and session caching. The resolved credentials are passed *into* the login callable; the callable does not gather them itself. *(Verified 2026-07-19: was incorrect — credential gathering happens in the generated command body, not inside the login callable; the factory-synthesized body must keep that resolution step.)*

> **Design note (2026-07-19):** per SOLID review — because the factory takes a body callable (§1), login composes through the same construction path with a different body; no special-casing inside the factory.

### 7. Full `cli/` audit

Sweep every module under `cli/` for external-Click usage and route it through Typer / the vendored Click that Typer runs:

- `login_commands.py`: `create_login_command` goes through the factory; any `click.prompt` for interactive credentials uses `typer.prompt`.
- `main.py`: static commands are already Typer decorators; replace any lingering `click.Choice`/`click.Path`/`click.get_current_context` with `typer.Option(...)`-native equivalents / `typer.Context`.
- Result: **no `import click` for object construction anywhere in `cli/`.** (Where the *vendored* enum/context is genuinely needed, import from what Typer re-exports, never from external `click` while a Typer context is live.)
- The invariant is **mechanically enforced, not left to convention**: a permanent guard test (or lint rule) fails the suite on any `import click` / `from click import …` under `src/graftpunk/cli/`, so the boundary cannot silently regrow.

> **Design note (2026-07-19):** per SOLID review — the original break was an unguarded cross-implementation boundary CI never exercised; a one-time audit decays, an import-ban guard doesn't.

## Testing & verification

- **Permanent typer-version CI matrix.** Add a CI dimension that installs `typer==0.21.*` and `typer` latest (≥0.26) and runs the plugin CLI suite under each. This is the guard that would have caught the original break (the pinned `uv.lock` hides it).
- **Version-agnostic unit tests** that assert the invariants regardless of installed typer (a command's declared default is applied when the flag is absent; `--format`-explicit detection; nested-group mounting; required option vs argument; variadic arg; param-less login).
- **Real-plugin end-to-end**, on both typer generations: `gp shopkeep {login, export list/get, reorder, import run}` and `gp surety <commands>` — mount, parse, and execute (read-only where live).
- **Import-ban guard test** — `cli/` contains no external-Click imports (the §7 invariant, enforced permanently).
- Green on the existing 3.11/3.12/3.13 matrix.

## Risks & mitigations

- **Reliance on Typer internals.** Mitigated by staying on the public `app.command`/`add_typer`/`typer.Option/Argument/Context` surface (Tier B); the one internal contract (`inspect.signature` honoring `__signature__`) is stable and covered by the version matrix.
- **Dynamic-signature edge cases** (variadic args, bool flags, required options). Mitigated by the version-agnostic invariant tests enumerating each construct, plus fail-loud rejection of anything unmapped.
- **Behavioral drift in help/error rendering.** Accepted: the documented interface (names, options, args, types, defaults, runtime behavior) is preserved; incidental help-panel/error formatting becomes Typer's, consistent with the rest of `gp`.
- **`CommandResult`/formatter hierarchy.** Untouched — `format_output` and the 3-level override system are consumed exactly as today.

## Rollout

1. Land this refactor on `refactor/typer-native-plugin-commands`; close PR #137 as superseded.
2. No upper bound on `typer`; **raise the floor to the oldest matrix-tested version** (`typer>=0.21`) so declared support is a subset of tested support. The version matrix — not an upper pin — remains the safeguard.

   > **Design note (2026-07-19):** per SOLID review — `>=0.9.0` claimed 0.9–0.20 compatibility that nothing tests (the same metadata/reality gap that hid the original break, mirrored). A floor raise is not an upper pin; it aligns metadata with the CI matrix.
3. Release as graftpunk **1.10.0** (behavior fix + internal refactor; no public API change) via the existing tag-push Trusted-Publishing pipeline.
4. grocerbot picks it up by bumping its floor to `graftpunk>=1.10.0` and re-locking; `gp shopkeep …` then works under grocerbot's typer 0.26.8.

## References (source)

- typer construction: `typer/main.py:1173` (`get_command`), `:1373`/`:1632` (params/convertors, `get_click_param`), `:1496-1527` (`get_callback` + context injection), `:915`/`:1302-1310` (`add_typer` + recursion); `typer/utils.py:107-186` (`get_params_from_function`, `inspect.signature`+`get_type_hints`); `typer/models.py:620/280/388/516/200`.
- Click boundary: `click/core.py:2294-2335` (`consume_value`/`UNSET` ladder), `:716` (`lookup_default`→`UNSET`), `:143/157` (`ParameterSource` enum / `COMMANDLINE`), `:440` (`_parameter_source` init); `typer/_click/core.py:445` (`lookup_default`→`None`), `:107` (vendored `ParameterSource`); `click/globals.py` vs `typer/_click/globals.py` (separate stacks).
- graftpunk contract: `plugins/cli_plugin.py` (`PluginParamSpec`, `CommandSpec`, `@command`, `LoginConfig`), `client.py` (`execute_plugin_command`), `cli/plugin_commands.py` (`_create_plugin_command` + callback), `cli/login_commands.py`, `plugins/formatters.py` (`format_output`).
