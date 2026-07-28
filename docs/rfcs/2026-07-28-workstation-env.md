---
type: spec
---

# Workstation environment file + `gp config` (lazy credential & settings loading)

## Problem

graftpunk resolves login credentials from environment variables
(`{SITE}_{FIELD}`, e.g. `SHOPKEEP_USERNAME`) and its own settings from
`GRAFTPUNK_*` variables (pydantic-settings, `config.py`). Neither is
persisted anywhere: every `gp <site> login` requires the operator to have
exported the right variables in the current shell — in practice
`export SHOPKEEP_PASSWORD=$(op read "op://…")` retyped per shell, per
machine-reboot, per session expiry. Settings vars have the same problem:
on a machine with no system Chrome, forgetting
`GRAFTPUNK_BROWSER_EXECUTABLE_PATH` fails login with "could not find a
valid chrome browser binary."

The operator wants a workstation-level file: set the variable → command
mappings once, and have `gp` load them no matter which shell or directory
it runs from — **lazily**, so commands like `$(op read …)` (hundreds of
ms + a possible biometric prompt) run only when a command actually needs
that variable.

## Requirements

1. **Just work** — no shell profile edits, no aliases, no direnv; works
   identically from any cwd and in subprocesses that exec `gp`.
2. **No latency for irrelevant operations** — `gp bek export list` must
   never pay for `BEK_PASSWORD=$(op read …)`; `gp --help` must never
   trigger a 1Password biometric prompt.
3. **Optimize/cache where possible** — never evaluate the same command
   twice in one process; never persist a resolved secret to disk.
4. **Convention over configuration** — no new naming scheme: file keys
   are exactly the env var names graftpunk already consumes.
5. **CLI-managed** — a `gp config` command family (git-config-shaped)
   reads, writes, and inspects the file.
6. **Covers graftpunk settings too** — `GRAFTPUNK_*` vars (e.g.
   `GRAFTPUNK_BROWSER_EXECUTABLE_PATH`) live in the same file and are
   visible to pydantic-settings and to child processes.

## Design overview

One file, two-phase loading, three-tier resolution:

```
~/.config/graftpunk/env          # the workstation env file
        │
        ├── PHASE 1 (eager, CLI startup): static values → os.environ
        │            (only where the real environment doesn't already
        │             define the variable — real env always wins)
        │
        └── PHASE 2 (deferred): $(…) command values are REGISTERED,
                     not run. They evaluate only at graftpunk's two
                     env-consumption chokepoints:
                       a) credential resolution in login_commands.py
                       b) GraftpunkSettings construction (config.py)
                     Results are memoized per-process.
```

Resolution order for a credential field (extends the existing two-tier
chain in `login_commands.py`; the existing order is unchanged, one tier
is inserted):

```
1. real environment variable            (existing)
2. workstation env file                 (NEW — evaluates $(…) on demand)
3. interactive prompt                   (existing)
```

## The file

- **Path:** `<config_dir>/env` where `config_dir` is
  `GraftpunkSettings.config_dir` (default `~/.config/graftpunk`,
  overridable via `GRAFTPUNK_CONFIG_DIR`). Note the bootstrap
  subtlety in "Loading semantics" below.
- **Format:** line-oriented `NAME=value`.
  - `NAME` matches `[A-Za-z_][A-Za-z0-9_]*`.
  - `value` is everything after the first `=`, trimmed of surrounding
    whitespace; surrounding single or double quotes are stripped if the
    value is fully quoted. No escape processing beyond that.
  - A value **containing** `$(` anywhere is a **command value**; it is
    executed with `/bin/sh -c 'printf %s "<value>"'`-equivalent
    semantics — concretely, `sh -c 'printf %s ' + value` wrapped so the
    whole value string (including any literal text around the `$(…)`)
    undergoes normal shell command substitution. Trailing newline of the
    result is stripped (matching `$(…)` shell semantics).
  - Any other value is a **static value**, used verbatim.
  - `#` at line start (after optional whitespace) is a comment; blank
    lines allowed. Inline `#` is NOT treated as a comment (values may
    contain `#`).
  - Malformed lines (no `=`, bad name): warn once with `file:line`,
    skip the line, continue.
- **Permissions:** created `0600`; every CLI write re-asserts `0600`.
  On read, if the file is group- or world-readable, log a one-line
  warning (do not refuse — the operator may have reasons).
- **Example:**

```bash
# graftpunk workstation environment — managed by `gp config`
GRAFTPUNK_BROWSER_EXECUTABLE_PATH=/Users/me/Library/Caches/ms-playwright/chromium-1232/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing

SHOPKEEP_USERNAME=$(op read "op://grocerbot/lightspeed-backoffice-grocerbot/username")
SHOPKEEP_PASSWORD=$(op read "op://grocerbot/lightspeed-backoffice-grocerbot/password")
SHOPKEEP_STORE=the-french-co
```

## Components

### `src/graftpunk/workstation_env.py` (new)

The single owner of file knowledge. Public surface:

- `load() -> WorkstationEnv` — parse the file (absent file → empty
  instance), cached per-process (module-level singleton, same pattern as
  `config.get_settings()`).
- `WorkstationEnv.inject_static() -> None` — Phase 1: for each static
  entry whose name is not already in `os.environ`, set it. Called once
  from CLI startup.
- `WorkstationEnv.resolve(name: str) -> str | None` — Phase 2: return
  the memoized result; else if `name` has a command value, execute it
  (`subprocess.run(["/bin/sh", "-c", …], capture_output=True, text=True)`),
  memoize, return. Static names return their value (covers callers that
  consult the file directly before Phase 1 ran, e.g. settings
  bootstrap). Unknown name → `None`. Command failure (non-zero exit) →
  log `workstation_env_command_failed` with the var name and the
  command's stderr, memoize the failure (don't re-run a failing command
  in the same process), return `None`.
- `WorkstationEnv.entries() -> list[Entry]` — for `gp config list`;
  `Entry` carries `name`, `raw_value`, `kind` (`static` | `command`),
  `line_no`.
- Writer functions used by the CLI: `set_entry(name, value)`,
  `unset_entry(name)` — **surgical line edits**: replace the existing
  `NAME=` line in place, append new names at end of file; never
  reserialize, so comments, blank lines, and ordering survive. Writes
  go to a temp file in the same directory + `os.replace` (atomic), then
  `chmod 0600`.

No imports from `graftpunk.config` at module import time except for the
config-dir path helper (see bootstrap note) — keep this module import-light
so CLI startup stays fast.

### Phase 1 hook — CLI startup (`cli/main.py`)

At the top of `main_callback()` (the root Typer callback,
`cli/main.py:79`, which runs before any subcommand): call
`workstation_env.load().inject_static()`. Cost is one small-file parse.
This makes static values (browser path, store name) visible to
pydantic-settings, plugins, and child processes for the entire
invocation.

### Phase 2 hook (a) — credential resolution (`cli/login_commands.py`)

In `_build_login_body()`'s `body()` (the `for field_name in fields:`
loop, currently env-hit → else prompt): insert the file tier:

```python
env_value = os.environ.get(envvar)
if not env_value:
    env_value = workstation_env.load().resolve(envvar)   # NEW
if env_value:
    credentials[field_name] = env_value
else:
    credentials[field_name] = typer.prompt(…)            # unchanged
```

`resolve()` runs `$(op read …)` here — the only moment a credential
command executes. A failed command logs and falls through to the prompt,
so login degrades to exactly today's behavior.

### Phase 2 hook (b) — settings construction (`config.py`)

`GraftpunkSettings` is a per-process singleton built on first
`get_settings()` call. Because Phase 1 has already injected static
`GRAFTPUNK_*` values into `os.environ` before any subcommand runs,
pydantic-settings sees them with **no changes to `config.py`'s field
machinery**. For *command-valued* `GRAFTPUNK_*` entries, `get_settings()`
gains one pre-step: before constructing `GraftpunkSettings`, iterate the
file's command entries whose names start with `GRAFTPUNK_` and are not
already in `os.environ`; `resolve()` each and put the result in
`os.environ`. This runs once per process, only for command entries the
operator chose to make commands.

**Documented convention (not enforced):** settings values should be
static; commands are for credentials. A command-valued `GRAFTPUNK_*` var
costs its evaluation on first settings touch in every `gp` process — the
operator opted in by writing it.

**Bootstrap subtlety:** the file path comes from
`settings.config_dir`, but settings construction may now consult the
file — a cycle. Break it by computing the file path *without*
`get_settings()`: read `GRAFTPUNK_CONFIG_DIR` from the real environment
directly, defaulting to `~/.config/graftpunk` (mirroring the field
default). A `GRAFTPUNK_CONFIG_DIR` entry *inside* the workstation env
file is explicitly unsupported (documented; the file cannot relocate
itself).

### `gp config` command family (`cli/config_commands.py`, new)

A `typer.Typer` sub-app attached in `main.py` alongside the existing
`session_app` / `http_app` (`app.add_typer(config_app)`):

| Command | Behavior |
|---|---|
| `gp config path` | Print the file's absolute path (whether or not it exists). |
| `gp config list` | Print `NAME=raw_value` per entry, file order. Commands display unevaluated. Nothing is masked — the convention keeps secrets behind `$(…)`, which is safe to show. Absent/empty file → no output, exit 0. |
| `gp config get NAME` | Print the raw stored value. Exit 1, message on stderr, if unset. |
| `gp config get NAME --resolve` | Evaluate a command value and print the result (explicit opt-in to printing a secret, same posture as `op read`). Static values print verbatim. Failed command → its stderr and exit 1. |
| `gp config set NAME VALUE` | Validate `NAME` against `[A-Za-z_][A-Za-z0-9_]*`; write/replace surgically; create file (0600) and parent dir if needed. `VALUE` stored verbatim — the operator's shell quoting (`'$(op read …)'`) is what defers evaluation. |
| `gp config unset NAME` | Remove the entry. Exit 0 even if absent (idempotent). |
| `gp config edit` | Open the file in `$EDITOR` (fall back to `$VISUAL`, then `vi`); create the file first if absent. |

## Error handling summary

| Condition | Behavior |
|---|---|
| File absent | All tiers act as empty; zero output, zero cost beyond one `stat`. |
| Malformed line | One warning (`file:line`), line skipped. |
| Command fails at credential time | Error log with var name + command stderr → fall through to interactive prompt. |
| Command fails at settings time | Same log → pydantic's normal missing-field behavior. |
| Duplicate `NAME` lines | Last one wins (shell semantics); `gp config set` replaces the last occurrence and warns about the duplicates. |
| File not writable on `set`/`unset` | Clear error, exit 1. |
| World/group-readable file | One-line warning on load. |

## Security posture

- Resolved secrets exist only in process memory (and in `os.environ` of
  the running `gp` process and its children — which is exactly where the
  operator previously exported them by hand).
- Nothing resolved is ever written to disk; the memo cache is
  per-process.
- The file itself contains commands and non-secret literals; it is
  `0600` anyway because command strings reveal vault structure and
  because someone will eventually put a literal secret in it.
- Command values are arbitrary code execution **by design** (the file is
  operator-owned config, same trust class as `~/.zshrc`). `gp config
  set` does not evaluate anything.

## Non-goals

- No secret-manager integration beyond "run a command" (op, vault CLI,
  pass, anything — they're all just commands).
- No cross-process caching of resolved secrets (1Password's own session
  unlock already amortizes biometric prompts).
- No per-site file sharding — laziness is per-variable, so one file
  costs nothing extra.
- No enforcement that settings values are static (documented convention
  only).
- No Windows support beyond what `/bin/sh` availability implies (matches
  the project's current posture).
- Does not fix Python-3.14 nodriver importability; that constraint is
  orthogonal (login must still run from a venv whose nodriver imports).

## Testing

`tests/unit/test_workstation_env.py` (new) + additions to CLI tests:

1. **Parser:** comments/blank lines preserved conceptually (entries carry
   line numbers); quoted values unquoted; inline `#` kept; malformed
   lines warn and skip; duplicate names → last wins.
2. **Writer round-trip:** `set` on a file with comments preserves every
   non-target line byte-for-byte; `set` of a new name appends; `unset`
   removes exactly one line; file mode is 0600 after every write; write
   is atomic (temp + replace).
3. **Phase 1:** statics land in `os.environ`; a real env var is never
   overwritten; command values are **not** in `os.environ` after
   startup.
4. **Laziness regression (load-bearing):** with a command entry present,
   running a non-login CLI command executes **zero** subprocesses from
   this module (spy on `subprocess.run`).
5. **Phase 2a:** credential resolution order — real env beats file;
   file beats prompt; command evaluated exactly once per process
   (memoized); failing command → warning + prompt fallback.
6. **Phase 2b:** command-valued `GRAFTPUNK_*` entry resolves at first
   `get_settings()`; static `GRAFTPUNK_*` visible via Phase 1 with no
   settings-code involvement.
7. **CLI:** every verb via typer's `CliRunner` — `get` (hit, miss,
   `--resolve` static, `--resolve` command via mocked subprocess), `set`
   (new, replace, bad name), `unset` (present, absent), `list`, `path`,
   `edit` (`$EDITOR` invocation mocked).
8. **Bootstrap:** file path honors real-env `GRAFTPUNK_CONFIG_DIR`
   without constructing settings.
