---
type: spec
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
  (pydantic's existing `env_file` source) → field default. The
  workstation file outranks cwd `.env` because statics are injected into
  `os.environ` and pydantic ranks env above dotenv — this is intended
  and documented: the workstation file is the machine-global answer, the
  cwd `.env` remains a per-project override *only* for values the
  workstation file doesn't set.
- **YAML plugin headers:** real env → workstation file → existing
  `PluginError` ("Environment variable $X is not set").

Other env reads exist and are **not** instrumented — by the laziness
contract they work with static values (via bootstrap injection) but not
command values. Known import-time accesses (static-only, documented):
`GRAFTPUNK_LOG_LEVEL` / `GRAFTPUNK_LOG_FORMAT` (`cli/main.py`, read at
module scope for early logging — bootstrap injection precedes them, so
file *statics* do work), `GRAFTPUNK_SESSION` (`session_context.py:27`),
`GP_DOWNLOADS_DIR` (`plugins/export.py:35`), and `settings.plugins_dir`
(accessed during import-time plugin discovery — a command value there
would evaluate at import; keep it static).

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
  instance), cached per-process.
- `WorkstationEnv.inject_static() -> None` — bootstrap: for each static
  entry whose name is not already in `os.environ`, set it.
- `WorkstationEnv.lookup(name: str) -> str | None` — **the public
  resolution entry point; the only implementation of the precedence
  rule.** Order: `os.environ` (hit → return it, including values
  injected at bootstrap) → file entry (static → its value; command →
  memoized result, else execute now, memoize, return) → `None`.
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

### Bootstrap hook — module scope of `cli/main.py`

Immediately after imports, **before** the module-level
`configure_logging` call (currently `cli/main.py:41-44`) and before
`register_plugin_commands(app)` (currently `cli/main.py:810-812`):

```python
from graftpunk import workstation_env
workstation_env.load().inject_static()
```

This ordering is the design's load-bearing invariant, so it is owned
structurally, not positionally: a lifecycle-faithful test (Testing #1)
imports the real CLI module fresh and asserts injected statics are
visible in `get_settings()` — any future re-ordering that constructs
settings before injection fails that test. Because injection precedes
the early logging config, file *statics* for
`GRAFTPUNK_LOG_LEVEL`/`GRAFTPUNK_LOG_FORMAT` take effect too.

Rationale for module scope rather than `main_callback()`: the settings
singleton is constructed at import time — `register_plugin_commands`
drives plugin discovery, which calls `get_settings()`
(`plugins/yaml_loader.py:452`, `plugins/python_loader.py:127`) — so any
hook inside a Typer callback runs after the singleton is frozen.

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
only when — the workstation file contains command-valued `GRAFTPUNK_*`
entries for fields pydantic did not receive from any source
(`model_fields_set`): `get_settings()` wraps the singleton in a
`LazySettings` proxy (~50 lines, defined in `config.py`):

- `__getattr__(field)`: if `field` is in the lazy overlay (command
  entry `GRAFTPUNK_<FIELD>` exists, field not provided by env/.env,
  not yet resolved) → `workstation_env.load().lookup(...)`, coerce via
  the field's pydantic `TypeAdapter`, cache on the proxy, return.
  Otherwise delegate to the underlying model. Failed command → the
  field's pydantic default (no `GraftpunkSettings` field is required,
  so there is no missing-field error path; a bad value surfaces
  downstream, e.g. `get_storage_config()`'s `ValueError` or a
  browser-not-found failure).
- When the file has no command-valued `GRAFTPUNK_*` entries (the common
  case), `get_settings()` returns the raw model — zero overhead, zero
  behavior change.

Consequences, per the laziness contract: `gp --help` constructs the
proxy but accesses no lazy field → zero evaluations. A command-valued
`GRAFTPUNK_BROWSER_EXECUTABLE_PATH` evaluates when session start first
reads it (`session.py:130`) — inside a login, exactly where the cost
belongs. A command-valued `plugins_dir` would evaluate at import
(discovery accesses it); the contract says keep such fields static.

### Consumption point (c) — YAML plugin header expansion

`expand_env_vars` (`plugins/yaml_loader.py:124-146`), invoked at
command-execution time from `plugins/yaml_plugin.py:126-128`, currently
raises `PluginError` when `os.environ` lacks `${VAR}`. It gains a
one-line fallback: on miss, consult `workstation_env.load().lookup(var)`
before raising. This makes API-key-style headers first-class lazy
consumers — the evaluation happens when that plugin command actually
runs.

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
| `gp config get NAME --resolve` | Evaluate a command value and print the result (explicit opt-in to printing a secret, same posture as `op read`). Static values print verbatim. Failed command → its stderr and exit 1. |
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
3. **Lazy field semantics:** command-valued `GRAFTPUNK_*` entry → not
   evaluated at `get_settings()`; evaluated exactly once on first field
   access; real-env-provided field never consults the file
   (`model_fields_set` respected); failed command → pydantic default;
   raw model returned (no proxy) when no command entries exist.
4. **`lookup()` precedence:** real env beats file (including bootstrap-
   injected statics); file beats `None`; command memoized (exactly one
   subprocess across repeated lookups); failure memoized (no re-run);
   empty-string env falls through to the file tier (intended change,
   asserted).
5. **Credential chain:** env miss → file resolve (subprocess mocked) →
   prompt fallback on command failure; envvar-override plugins resolve
   by their override names.
6. **YAML header fallback:** `${VAR}` miss in `os.environ` resolves from
   the file at command execution; still raises `PluginError` when both
   miss.
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
