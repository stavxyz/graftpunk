---
type: spec
validated:
  sha: dbf1db293582c116f7eea4a5bfa9e92e81fb6a2d
  date: 2026-07-28T19:45:46Z
  reviewers: [fact-check, solid-hygiene]
  findings:
    critical: 0
    important: 6
    medium: 1
    low: 3
    nitpick: 0
  net_negative_remaining: 0
---

# Workstation environment file + `gp config` (lazy credential & settings loading)

## Problem

graftpunk resolves login credentials from environment variables — the
derived `{SITE}_{FIELD}` names (e.g. `SHOPKEEP_USERNAME`), or per-plugin
`username_envvar`/`password_envvar` overrides (`cli/login_commands.py:108-123`)
— and its own settings from `GRAFTPUNK_*` variables (pydantic-settings,
`config.py`). Credentials are not persisted anywhere; settings can only be
persisted in a **cwd-relative** `.env` file (pydantic-settings
`env_file=".env"`, `config.py:94`), which fails "works from any cwd." In
practice `export SHOPKEEP_PASSWORD=$(op read "op://…")` is retyped per
shell, and on a machine with no system Chrome, forgetting
`GRAFTPUNK_BROWSER_EXECUTABLE_PATH` fails login with "could not find a
valid chrome browser binary."

The operator wants a workstation-level file: set the variable → command
mappings once, and have `gp` load them no matter which shell or directory
it runs from — **lazily**, so commands like `$(op read …)` (hundreds of
ms + a possible biometric prompt) run only when something actually
consumes that variable.

## Requirements

1. **Just work** — no shell profile edits, no aliases, no direnv; works
   identically from any cwd and in subprocesses that exec `gp`.
2. **No latency for irrelevant operations** — `gp bek export list` must
   never pay for `BEK_PASSWORD=$(op read …)`; `gp --help` must never
   trigger a 1Password biometric prompt, even when command-valued
   `GRAFTPUNK_*` entries exist.
3. **Optimize/cache where possible** — never evaluate the same command
   twice in one process; never persist a resolved secret to disk.
4. **Convention over configuration** — no new naming scheme: file keys
   are exactly the env var names graftpunk already consumes.
5. **CLI-managed** — a `gp config` command family (git-config-shaped)
   reads, writes, and inspects the file.
6. **Covers graftpunk settings too** — `GRAFTPUNK_*` vars (e.g.
   `GRAFTPUNK_BROWSER_EXECUTABLE_PATH`) live in the same file, are
   visible to pydantic-settings and child processes, and may themselves
   be command-valued without violating requirement 2.

## The laziness contract (design principle)

Laziness is **access-driven**: a command value evaluates at the moment
some code first asks for that variable, and never before. It is the
responsibility of graftpunk core and plugin authors to **place access
intentionally** — read credentials through the login resolution chain,
read settings inside command bodies via `get_settings().<field>`, and
never read lazily-resolvable variables at module import time. The design
does not defensively engineer around careless early access; instead it
documents exactly which variables early access makes static-only (see
"Known import-time accesses" below).

## Design overview

One file, two loading mechanisms, one precedence rule:

```
~/.config/graftpunk/env          # the workstation env file
        │
        ├── STATIC values: injected into os.environ at process bootstrap
        │     (module scope of cli/main.py, BEFORE logging config and
        │      plugin registration), only where the real environment
        │      doesn't already define the name — real env always wins.
        │
        └── COMMAND values ($(…)): registered, never run at bootstrap.
              They evaluate on first access, at three instrumented
              consumption points:
                a) credential resolution   (cli/login_commands.py)
                b) settings field access   (lazy proxy over GraftpunkSettings)
                c) YAML plugin ${VAR} header expansion (plugins/yaml_loader.py)
              Results (and failures) are memoized per-process.
```

Precedence, owned by a single lookup function (see Components):

- **Credentials:** real env → workstation file → interactive prompt.
- **Settings (`GRAFTPUNK_*`):** real env → workstation file → cwd `.env`
  (pydantic's existing `env_file` source) → field default — and this
  order holds for **both value kinds**. Statics: injected into
  `os.environ`, and pydantic ranks env above dotenv. Command values
  (allowlisted fields only): the proxy's source-aware gate skips a
  field only when the *real environment* provides it; a merely
  `.env`-provided field still yields to the workstation command value.
  The workstation file is the machine-global answer; the cwd `.env`
  remains a per-project override *only* for values the workstation file
  doesn't set.
- **YAML plugin headers:** real env → workstation file → existing
  `PluginError` ("Environment variable $X is not set"), resolved
  through the same single `lookup()` entry point.

Other env reads exist and are **not** instrumented — by the laziness
contract they work with static values (via bootstrap injection) but not
command values. Static-only is also the rule for `GRAFTPUNK_*` fields
consumed by **model-internal** methods/properties (`supabase_url`,
`supabase_service_key`, `s3_bucket` via `get_storage_config()`;
`config_dir` via the `sessions_dir` property) — internal reads bind to
the raw model and cannot see the lazy overlay, so command values there
are warned about and ignored (see Consumption point (b)); supply those
secrets via the real environment. Known import-time accesses
(static-only, documented):
`GRAFTPUNK_LOG_LEVEL` / `GRAFTPUNK_LOG_FORMAT` (`cli/main.py`, read at
module scope for early logging — bootstrap injection precedes them, so
file *statics* do work), `GRAFTPUNK_SESSION` (`session_context.py:27`),
`GP_DOWNLOADS_DIR` (`plugins/export.py:35`), and `settings.config_dir`
(accessed during import-time plugin discovery, which reads
`settings.config_dir / "plugins"` — `yaml_loader.py:453`,
`python_loader.py:128`; there is no separate `plugins_dir` setting, and
an in-file `GRAFTPUNK_CONFIG_DIR` is already unsupported per § The file).

## The file

- **Path:** `<config_dir>/env` where `<config_dir>` comes from the shared
  settings-free resolver `graftpunk.paths.config_dir()` (see Components):
  `GRAFTPUNK_CONFIG_DIR` from the **real environment**, else
  `~/.config/graftpunk`. A `GRAFTPUNK_CONFIG_DIR` set only in a cwd
  `.env` moves `settings.config_dir` (pydantic still honors it) but does
  NOT move the workstation env file — documented as unsupported; don't
  do that. An entry for `GRAFTPUNK_CONFIG_DIR` *inside* the file is
  likewise unsupported (the file cannot relocate itself).
- **Format:** line-oriented `NAME=value`.
  - `NAME` matches `[A-Za-z_][A-Za-z0-9_]*`.
  - `value` is everything after the first `=`, trimmed of surrounding
    whitespace. Quote handling runs **first**: if the value is fully
    single-quoted, the quotes are stripped and the value is **always
    static** — the escape hatch for a literal `$(`. If fully
    double-quoted, the quotes are stripped and command detection
    proceeds. No other escape processing.
  - After quote handling, a value is a **command value** iff the entire
    value is one command substitution: it matches `^\$\(.*\)$`. The
    inner text (between `$(` and the final `)`) is executed via
    `subprocess.run(["/bin/sh", "-c", inner], capture_output=True,
    text=True)`; stdout with exactly one trailing newline stripped is
    the result (matching `$(…)` shell semantics). Values that merely
    *contain* `$(` mixed with literal text are unsupported: treated as
    static, with a one-time warning naming the line.
  - Any other value is a **static value**, used verbatim.
  - `#` at line start (after optional whitespace) is a comment; blank
    lines allowed. Inline `#` is NOT a comment (values may contain `#`).
  - Malformed lines (no `=`, bad name): warn once with `file:line`,
    skip the line, continue.
  - Duplicate `NAME` lines: last one wins (shell semantics).

> **Design note (2026-07-28):** this is deliberately a *second* env-file
> dialect, distinct from the cwd `.env` pydantic parses — command-value
> semantics can't be expressed in dotenv. To keep the delta enumerable:
> the format diverges from dotenv conventions **only** where command
> values require it (quote-kind meaning, whole-value `$()`, inline `#`
> kept literal), `gp config list` surfaces each entry's `kind`
> (`static`/`command`) so misclassifications are visible, and this
> section is the single home of the divergence list.
- **Permissions:** created `0600`; every CLI write re-asserts `0600`.
  On read, if the file is group- or world-readable, log a one-line
  warning (do not refuse).
- **Example:**

```bash
# graftpunk workstation environment — managed by `gp config`
GRAFTPUNK_BROWSER_EXECUTABLE_PATH=/Users/me/Library/Caches/ms-playwright/chromium-1232/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing

SHOPKEEP_USERNAME=$(op read "op://grocerbot/lightspeed-backoffice-grocerbot/username")
SHOPKEEP_PASSWORD=$(op read "op://grocerbot/lightspeed-backoffice-grocerbot/password")
SHOPKEEP_STORE_NAME=the-french-co
```

File keys for login fields must match the **derived** env var names —
`{SITE}_{FIELD}` (so shopkeep's `store_name` field is
`SHOPKEEP_STORE_NAME`), or, for plugins that define
`username_envvar`/`password_envvar`, those override names. The design
handles overrides automatically because resolution is keyed on the final
computed envvar.

## Components

### `src/graftpunk/paths.py` (new, tiny)

The single owner of "where is the config dir," shared by pydantic and
the workstation-env module so the two can never diverge:

- `config_dir() -> Path` — `GRAFTPUNK_CONFIG_DIR` from the real
  environment, else `~/.config/graftpunk`. **Settings-free and
  side-effect-free**: no `get_settings()`, no directory creation
  (`GraftpunkSettings.__init__` creates directories, `config.py:99-106`
  — this helper must not).
- `workstation_env_path() -> Path` — `config_dir() / "env"`.
- `GraftpunkSettings.config_dir`'s field default becomes
  `default_factory=paths.config_dir` (pydantic env sources still
  override it as today).

### `src/graftpunk/workstation_env.py` (new)

The single owner of file knowledge **and** of the env-beats-file
precedence rule. Import-light (imports `paths`, never `config`).

- `load() -> WorkstationEnv` — parse the file (absent file → empty
  instance), cached per-process. **Cache coherence contract:** the
  writer functions (`set_entry`/`unset_entry`) invalidate this cache,
  so a write-then-lookup in one process (tests, future in-process
  callers) always sees fresh entries.
- `ensure_bootstrap() -> None` — idempotent module-level function:
  `load()` + `inject_static()` once per process. Called by
  `get_settings()` (see Bootstrap section) and by `cli/main.py`.
- `WorkstationEnv.inject_static() -> None` — bootstrap: for each static
  entry whose name is not already in `os.environ`, set it.
- `WorkstationEnv.lookup(name: str) -> str | None` — **the public
  resolution entry point; the only implementation of the precedence
  rule.** Order: `os.environ` (hit → return it, including values
  injected at bootstrap; an **empty-string value counts as a miss**) →
  file entry (static → its value; command → memoized result, else
  execute now, memoize, return) → `None`.
  Command failure (non-zero exit): log
  `workstation_env_command_failed` with the var name and the command's
  stderr, memoize the failure (never re-run a failing command in the
  same process), return `None`.
- `WorkstationEnv.command_entry_names() -> set[str]` — names of
  command-valued entries (the lazy-settings proxy consults this).
- `WorkstationEnv.entries() -> list[Entry]` — for `gp config list`;
  `Entry` carries `name`, `raw_value`, `kind` (`static` | `command`),
  `line_no`.
- Writer functions used by the CLI: `set_entry(name, value)`,
  `unset_entry(name)`. Surgical line edits — replace the existing
  `NAME=` line in place (last occurrence when duplicated, with a
  warning listing the others), append new names at end of file; never
  reserialize, so comments, blank lines, and ordering survive.
  `unset_entry` removes **all** occurrences of `NAME` (git-config
  `--unset-all` semantics; duplicates are never intentional), warning
  when more than one line was removed. Writes go to a temp file in the
  same directory + `os.replace` (atomic), then `chmod 0600`.

Callers never sequence "check os.environ, then the file" by hand —
that inversion-prone duplication is what `lookup()` exists to prevent.

### Bootstrap — `ensure_bootstrap()`, owned by the settings chokepoint

Static injection is **entry-path-agnostic**: `workstation_env` exposes
an idempotent `ensure_bootstrap()` (parse file if needed, inject
statics, mark done), and `get_settings()` calls it as its first
statement — *before* first model construction. The ordering invariant
("statics precede the singleton") is thereby owned structurally at the
one chokepoint every consumer goes through: the CLI, and equally
in-process **library consumers** (grocerbot drives
`GraftpunkClient("shopkeep")` without ever importing `graftpunk.cli`)
get identical behavior for statics and command values alike. No mixed
reach: both mechanisms live at the same depth.

`cli/main.py` additionally calls `ensure_bootstrap()` at module scope,
immediately after imports and **before** the module-level
`configure_logging` call (currently `cli/main.py:41-44`) — needed only
so file *statics* for `GRAFTPUNK_LOG_LEVEL`/`GRAFTPUNK_LOG_FORMAT`
take effect before early logging config (which reads `os.environ`
directly and never constructs settings). Idempotence makes the double
call free.

Rationale for not hooking `main_callback()`: the settings singleton is
constructed at import time — `register_plugin_commands`
(`cli/main.py:810-812`) drives plugin discovery, which calls
`get_settings()` (`plugins/yaml_loader.py:452`,
`plugins/python_loader.py:127`) — so any hook inside a Typer callback
runs after the singleton is frozen. With `ensure_bootstrap()` inside
`get_settings()`, that import-time construction is itself what
triggers injection, in order, everywhere. A lifecycle-faithful test
(Testing #1) imports the real CLI module fresh and asserts injected
statics are visible in `get_settings()`.

### Consumption point (a) — credential resolution (`cli/login_commands.py`)

In `make_login_body()`'s `body()` (the `for field_name in fields:` loop,
`cli/login_commands.py:121-134`), the env lookup is replaced by the
single precedence-owning entry point:

```python
env_value = workstation_env.load().lookup(envvar)   # env → file
if env_value:
    credentials[field_name] = env_value
else:
    credentials[field_name] = typer.prompt(…)        # unchanged
```

`lookup()` runs `$(op read …)` here — for credentials, the only moment
a command executes. A failed command logs and falls through to the
prompt, so login degrades to exactly today's behavior. Note one
deliberate behavior change: an env var set to the **empty string**
previously logged `login_envvar_empty` and prompted; it now falls
through to the file tier first, then the prompt — intended.

### Consumption point (b) — lazy settings field access (`config.py`)

`get_settings()` keeps constructing the `GraftpunkSettings` singleton
exactly as today (pydantic reads real env, injected statics, and cwd
`.env` at construction). What changes is the return value when — and
only when — the workstation file contains command-valued entries for
**proxy-safe** `GRAFTPUNK_*` fields: `get_settings()` wraps the
singleton in a `LazySettings` proxy (~50 lines, defined in `config.py`).

**The overlay is bounded by an explicit allowlist, not implied
universality.** The proxy intercepts only external attribute access;
reads that happen *inside* the model's own methods and properties
(`get_storage_config()`'s `self.supabase_service_key`/`self.s3_bucket`,
the `sessions_dir` property's `self.config_dir`) bind to the raw model
and can never see the overlay. Therefore:

- `PROXY_SAFE_FIELDS` (in `config.py`, next to the model) enumerates
  the leaf fields consumed only via external attribute access — today:
  `browser_executable_path` (and future leaves added deliberately, with
  the review question "is this field read model-internally?").
- A command value targeting any **non**-allowlisted `GRAFTPUNK_*` field
  warns at load time ("command values are unsupported for
  GRAFTPUNK_X; treat as static-only") and is ignored by the overlay.
  Fields consumed by model-internal methods (`supabase_*`, `s3_*`,
  `config_dir`) join the documented static-only list alongside the
  import-time accesses — real env remains the way to supply those
  secrets.

Proxy behavior for allowlisted fields:

- `__getattr__(field)`: if `field` is in the lazy overlay (allowlisted,
  command entry exists, **not provided by the real environment**, not
  yet resolved) → `workstation_env.load().lookup(...)`, coerce via the
  field's pydantic `TypeAdapter`, cache on the proxy, return. Otherwise
  delegate to the underlying model. Failed command → the field's
  pydantic default (no `GraftpunkSettings` field is required, so there
  is no missing-field error path; a bad value surfaces downstream,
  e.g. a browser-not-found failure).
- **Source-aware precedence gate:** the overlay skips a field only when
  its env var name is present in `os.environ` (real env or
  bootstrap-injected static — env wins). A field provided merely by a
  cwd `.env` still yields to the workstation command value, so the
  documented order — real env → workstation file → cwd `.env` → default
  — holds for **both** value kinds and is decided in exactly one place
  (this gate). `model_fields_set` is not used for precedence: it cannot
  distinguish env-provided from dotenv-provided.
- The overlay's env-var names are derived from the model's own
  settings metadata (`env_prefix` + field aliases), not re-assembled by
  hand — pydantic stays the single authority for the field→envvar
  mapping.
- When the file has no command-valued entries for allowlisted fields
  (the common case), `get_settings()` returns the raw model — zero
  overhead, zero behavior change.

Consequences, per the laziness contract: `gp --help` constructs the
proxy but accesses no lazy field → zero evaluations. A command-valued
`GRAFTPUNK_BROWSER_EXECUTABLE_PATH` evaluates when `BrowserSession`
construction first reads it (`session.py:130`) — inside a login, exactly
where the cost belongs. A command-valued `config_dir` would evaluate at
import (discovery accesses `settings.config_dir`) — but an in-file
`GRAFTPUNK_CONFIG_DIR` is already unsupported (see § The file).

### Consumption point (c) — YAML plugin header expansion

`expand_env_vars` (`plugins/yaml_loader.py:124-146`), invoked at
command-execution time from `plugins/yaml_plugin.py:126-128`, currently
reads `os.environ.get` itself and raises `PluginError` when `${VAR}` is
unset. Its env read is **replaced** by the single precedence-owning
entry point — symmetrical with consumption point (a): the replacer
resolves each `${VAR}` via `workstation_env.load().lookup(var)` alone
and raises the existing `PluginError` on `None`. No call-site
env-then-file sequencing survives anywhere; `lookup()` remains the only
implementation of the precedence rule across all three consumers. This
makes API-key-style headers first-class lazy consumers — evaluation
happens when that plugin command actually runs.

One deliberate behavior change, same as point (a) and documented once
in `lookup()`'s contract: an **empty-string** environment variable
counts as a miss and falls through to the file tier (previously
`expand_env_vars` treated empty-as-set). Unified semantics across all
three consumption points.

### `gp config` command family (`cli/config_commands.py`, new)

**Namespace migration:** `gp config` already exists as a plain command —
`@app.command("config")` at `cli/main.py:784-803`, a settings display
panel advertised in the root help text (`cli/main.py:61`). That command
is **removed** and its behavior absorbed: `config_app` is a
`typer.Typer(invoke_without_command=True)` whose callback, when invoked
with no subcommand, renders the same settings display — so bare
`gp config` keeps its current behavior verbatim, and the display is also
available explicitly as `gp config show`. The root help text is updated
in the same change.

| Command | Behavior |
|---|---|
| `gp config` | (no subcommand) Current settings display — unchanged behavior, migrated home. |
| `gp config show` | Same display, explicit verb. |
| `gp config path` | Print the workstation env file's absolute path (whether or not it exists). |
| `gp config list` | Print `NAME=raw_value` per entry, file order. Commands display unevaluated. Nothing is masked — the convention keeps secrets behind `$(…)`; a literal secret stored against convention IS printed, an acknowledged residual exposure consistent with the 0600 posture. Absent/empty file → no output, exit 0. |
| `gp config get NAME` | Print the raw stored value. Exit 1, message on stderr, if unset. |
| `gp config get NAME --resolve` | Evaluate a command value and print the result (explicit opt-in to printing a secret, same posture as `op read`). Static values print verbatim. Failed command → its stderr and exit 1. Deliberately inspects the file's stored value directly, bypassing the env→file precedence — it answers "what does the *file* say," not "what would `lookup()` return"; the one intentional exception to lookup()-only resolution. |
| `gp config set NAME VALUE` | Validate `NAME` against `[A-Za-z_][A-Za-z0-9_]*`; write/replace surgically; create file (0600) and parent dir if needed. `VALUE` stored verbatim — the operator's shell quoting (`'$(op read …)'`) is what defers evaluation. **Guardrail:** when `NAME` contains a secret keyword (`password`/`secret`/`token`/`key`, matching `login_commands.py:106`) and `VALUE` contains no `$(`, warn: the operator's shell may have already substituted an unquoted `$(op read …)`, and a plaintext secret is being written to disk. Warn, don't refuse. |
| `gp config unset NAME` | Remove **all** occurrences (see writer semantics). Exit 0 even if absent (idempotent). |
| `gp config edit` | Open the file in `$VISUAL`, else `$EDITOR`, else `vi`; create the file first if absent. |

## Error handling summary

| Condition | Behavior |
|---|---|
| File absent | All tiers act as empty; zero output, zero cost beyond one `stat`. |
| Malformed line | One warning (`file:line`), line skipped. |
| Mixed literal+`$(…)` value | Treated as static; one-time warning naming the line. |
| Command fails at credential time | Error log with var name + command stderr → fall through to interactive prompt. |
| Command fails at settings access | Same log → field keeps its pydantic default (no required fields exist); surfaces downstream if it matters. |
| Command fails at YAML header expansion | Same log → existing `PluginError` ("Environment variable $X is not set"). |
| Duplicate `NAME` lines | Read: last wins. `set`: replaces last, warns about the others. `unset`: removes all, warns if >1. |
| File not writable on `set`/`unset` | Clear error, exit 1. |
| World/group-readable file | One-line warning on load. |

## Security posture

- Resolved secrets exist only in process memory (and in `os.environ` of
  the running `gp` process and its children for statics — exactly where
  the operator previously exported them by hand). Lazily-resolved
  command results are held on the proxy/memo, not exported to
  `os.environ`.
- Nothing resolved is ever written to disk; the memo cache is
  per-process.
- The file is `0600` because command strings reveal vault structure and
  because someone will eventually put a literal secret in it — `gp
  config set`'s guardrail warns at the moment that's about to happen,
  and `list`/`get` printing raw values is the acknowledged residual
  exposure of that failure mode.
- Command values are arbitrary code execution **by design** (the file is
  operator-owned config, same trust class as `~/.zshrc`). `gp config
  set` does not evaluate anything.

## Non-goals

- No secret-manager integration beyond "run a command" (op, vault CLI,
  pass — they're all just commands).
- No cross-process caching of resolved secrets (1Password's own session
  unlock already amortizes biometric prompts).
- No per-site file sharding — laziness is per-variable.
- No instrumentation of every env read in the codebase — the laziness
  contract governs; un-instrumented reads get statics only.
- No `.env` deprecation: pydantic's cwd `.env` source stays, one rung
  below the workstation file in the documented precedence.
- No Windows support beyond what `/bin/sh` availability implies.
- Does not fix Python-3.14 nodriver importability; orthogonal.

## Testing

`tests/unit/test_workstation_env.py`, `tests/unit/test_paths.py`,
additions to CLI tests. Items 1–2 are the load-bearing,
**lifecycle-faithful** tests: they drive the real entry path (fresh
import of `graftpunk.cli.main` with a scrubbed `sys.modules` and
`reset_settings()`, tmp `GRAFTPUNK_CONFIG_DIR`, typer `CliRunner`), not
phases called in an assumed order.

1. **Real-order static visibility:** file with static
   `GRAFTPUNK_BROWSER_EXECUTABLE_PATH`; fresh-import the CLI module (its
   import runs bootstrap injection then plugin discovery); assert
   `get_settings().browser_executable_path` equals the file value —
   this test fails if anyone ever moves injection after settings
   construction.
2. **Laziness regression:** file with command-valued credential AND
   command-valued `GRAFTPUNK_*` entries; run `gp --help` and one
   non-login plugin command through the real entry path; spy on
   `subprocess.run` — assert **zero** invocations from
   `workstation_env`.
3. **Lazy field semantics:** command-valued entry for an allowlisted
   field → not evaluated at `get_settings()`; evaluated exactly once on
   first field access; **source-aware gate:** real-env-provided field
   never consults the file, but a field provided only by cwd `.env`
   yields to the workstation command value (documented order holds for
   both value kinds); failed command → pydantic default; raw model
   returned (no proxy) when no allowlisted command entries exist;
   command value targeting a NON-allowlisted `GRAFTPUNK_*` field warns
   at load and is ignored by the overlay; overlay env names derived
   from model metadata, not hand-assembled.
4. **`lookup()` precedence:** real env beats file (including bootstrap-
   injected statics); file beats `None`; command memoized (exactly one
   subprocess across repeated lookups); failure memoized (no re-run);
   empty-string env falls through to the file tier (intended change,
   asserted); `set_entry`/`unset_entry` invalidate the `load()` cache
   (write-then-lookup sees fresh entries).
5. **Credential chain:** env miss → file resolve (subprocess mocked) →
   prompt fallback on command failure; envvar-override plugins resolve
   by their override names.
6. **YAML header resolution:** `${VAR}` resolves through `lookup()`
   alone at command execution (env hit, file static, file command each
   asserted); raises `PluginError` on `None`; empty-string env falls
   through to the file tier here too (unified semantics asserted).
7. **Parser:** quote handling order (single-quoted `'$(x)'` is static;
   double-quoted `"$(x)"` is a command; bare `$(x)` is a command; mixed
   `pre$(x)post` is static + warning); inline `#` kept; malformed lines
   warn and skip; duplicates → last wins.
8. **Writer round-trip:** `set` preserves every non-target line
   byte-for-byte; `set` of a new name appends; `unset` removes all
   occurrences; 0600 after every write; atomic (temp + replace);
   secret-keyword literal-value guardrail warns.
9. **CLI verbs:** every verb via `CliRunner` — bare `gp config` renders
   the migrated settings display; `show`; `get` (hit, miss, `--resolve`
   static, `--resolve` command via mocked subprocess); `set` (new,
   replace, bad name, guardrail); `unset` (present, absent, duplicated);
   `list`; `path`; `edit` (`$VISUAL`/`$EDITOR`/`vi` precedence, mocked).
10. **Paths:** `paths.config_dir()` honors real-env
    `GRAFTPUNK_CONFIG_DIR` without constructing settings and creates no
    directories; pydantic's `config_dir` default routes through it.
11. **Library reach:** a consumer that never imports `graftpunk.cli`
    (constructs `GraftpunkClient` / calls `get_settings()` directly)
    sees file statics and allowlisted command values identically to the
    CLI — `ensure_bootstrap()` inside `get_settings()` asserted via a
    fresh-import test that touches no CLI module.
