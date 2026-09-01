---
type: spec
---

# Session account identity — multi-account slots keyed by plugin + account

**Status:** design v2, awaiting validation (v1 blocked in review: the plugin-command path had no resolution owner)
**Issue:** [#151](https://github.com/stavxyz/graftpunk/issues/151)
**Scope:** the session cache's naming and metadata contract, login-time labelling, an **operating-session-name owner** at every command entry point, and the CLI/Python surfaces that show or pick sessions. No change to encryption, storage backends' key schemes, or the keepalive daemon.

## Problem

The session cache is keyed by the plugin's `session_name` alone — a fixed string per plugin — at a user-wide path (`~/.config/graftpunk/sessions`), shared by every shell, agent and process running as that user, and `cache_session` overwrites the slot silently (`src/graftpunk/cache.py:260` (`def cache_session`)). Metadata records the site, cookie counts and timestamps but not *who is signed in* (`src/graftpunk/storage/base.py:46` (`class SessionMetadata`)). So one `gp <plugin> login` as a different account silently repoints every other shell, agent and process at whatever account it just authenticated, with no signal at write time and no way to tell at read time. This happened in practice on 2026-08-24 with two agents on one plugin.

## Decisions (made with the owner, 2026-08-26)

1. **Multi-account slots**, not single-slot-plus-warning: sessions are keyed by plugin **and** account, so a second account's login creates a second slot instead of evicting the first.
2. **Credential-derived label with `--as` override**: the account label defaults to the slugified login identifier the CLI already holds; `gp <site> login --as <label>` overrides. No server round-trip.
3. **Resolution precedence** `--session` flag > `GRAFTPUNK_SESSION` env > `.gp-session` in cwd, then: exactly one cached session for the plugin → use it; several → refuse with a typed error listing candidates. Never "most recent wins". (Flag > env > per-directory file matches git/AWS CLI/pyenv convention and graftpunk's existing order: `src/graftpunk/session_context.py:64` (`def resolve_session`), `src/graftpunk/session_context.py:14` (`def get_active_session`).)

## Design

### Naming

A session name becomes `<session_name>` or `<session_name>@<label>`, e.g. `myshop` or `myshop@alice`.

- Validation permits **at most one `@`**, not as the first or last character; the charset on each side stays `[a-z0-9][a-z0-9_-]*` (today's rule: `src/graftpunk/cache.py:55` (`_SESSION_NAME_RE = re.compile`)); dots remain forbidden.
- The full `name@label` string is what flows everywhere a session name flows today — local directory names, S3/Supabase object keys, `.gp-session`, `GRAFTPUNK_SESSION`, `--session`, `gp session use`. Storage backends need **no key-scheme changes**; the implementation plan must verify `@` is accepted by each backend's path/key handling and by the local directory layout.
- **The charset is a backend contract:** the `SessionStorageBackend` protocol docstring (`src/graftpunk/storage/base.py:135` (`class SessionStorageBackend`)) states the session-name charset (including the single `@`) so every future backend inherits the obligation, rather than it living silently in `cache.py`.
- A session cached with no derivable identity keeps the bare legacy name. Every pre-upgrade session already has a bare name, so **no migration** is needed or performed.

### The `session_identity` module (grammar + resolution policy, no storage imports)

A new small module, `src/graftpunk/session_identity.py`, owns everything about what a session name *means*. It imports nothing from `cache.py` (callers hand it data), so naming/identity policy sits above storage mechanism and stays cycle-free:

- `split_session_name(name) -> (base, label | None)` and its inverse `join_session_name(base, label)` — the **sole owners of the `name@label` grammar**. Validation, resolution, metadata extraction, display and `get_plugin_for_session` all call them; nothing else splits on `@`.
- `validate_session_name(name)` **moves here** (from `src/graftpunk/cache.py:63` (`def validate_session_name`)), gaining the one-`@` rule; `cache.py` re-imports it so existing callers and imports keep working.
- `resolve_account_session(base_name: str, existing_names: Iterable[str]) -> str` — the candidate-selection policy: candidates are `base_name` itself plus any name whose `split_session_name` base equals `base_name`; exactly one → return it; zero → return `base_name` (today's behaviour; the downstream not-found path is unchanged); several → raise `AmbiguousSessionError` carrying the candidates. Callers supply `existing_names` from `list_sessions` (`src/graftpunk/cache.py:683` (`def list_sessions() -> list[str]:`)) — names only, so resolution never pays the per-session metadata fetches of `list_sessions_with_metadata` (a remote round-trip each on S3/Supabase). Metadata for the error's account display is fetched lazily, for the candidates only, by the error renderer.
- `GP_ACCOUNT_ATTR` — the name of the session attribute that carries the identifier (see Metadata), owned here so no other module hard-codes the string (the codebase precedent is `tokens.py`'s `_CACHE_ATTR`).

> **Design note (2026-08-26, v2):** v1 homed the grammar helper and the resolver in `cache.py`. Review flagged the accretion — `cache.py` is already storage orchestration plus backend wiring — and the import cycle a cache-importing policy module would create. Policy now lives in `session_identity`, mechanism stays in `cache.py`, and the dependency points one way (cache → session_identity).

### The operating session name (one owner per invocation)

Every entry point computes the **operating session name** exactly once — `--session` flag > `GRAFTPUNK_SESSION` > `.gp-session` (via the existing `src/graftpunk/session_context.py:64` (`def resolve_session`)) > `resolve_account_session(base, list_sessions())` — and that one string is the key for **both the load and every write-back** of that invocation. `plugin.session_name` is demoted, explicitly, to *base name: an input to resolution*, never a storage key at command time.

Where it lives per surface:

- **Plugin commands** (`gp <site> <cmd>` — the surface where the 2026-08-24 collision actually happened): today this path bypasses all selection — `src/graftpunk/cli/plugin_runtime.py:79` (`session = plugin.get_session() if needs_session else requests.Session()`) loads via the plugin's own `get_session` (`src/graftpunk/plugins/cli_plugin.py:697` (`def get_session(self) -> requests.Session:`)), which reads the ambient `self.session_name`, and plugin commands have no `--session` option (`src/graftpunk/cli/command_factory.py:144` (`BUILTIN_OPTIONS: dict[str, Any] = {"format": "json", "view": (), "output": ""}`)). The design: `session` joins `BUILTIN_OPTIONS`, so every synthesized plugin command accepts `--session <name>`; the runtime computes the operating name at command entry, loads via `load_session_for_api(operating_name)` (`src/graftpunk/cache.py:539` (`def load_session_for_api`)) instead of `plugin.get_session()`, carries it in `CommandContext` (the field exists: `src/graftpunk/cli/plugin_runtime.py:163` (`_session_name=(plugin.session_name if needs_session else "")`)), and keys the post-command write-back with it (`src/graftpunk/cli/plugin_runtime.py:207` (`update_session_cookies(session, plugin.session_name)`) changes to the operating name).
- **Python API**: `GraftpunkClient` (`src/graftpunk/client.py:200` (`class GraftpunkClient`)) gains `session: str | None = None`. `__init__` computes the operating name once — the pin if given, else `resolve_account_session(plugin.session_name, list_sessions())` — stores it, and **both** load and the write-backs at `src/graftpunk/client.py:402` (`update_session_cookies(session, plugin.session_name)`) and `src/graftpunk/client.py:431` (`update_session_cookies(self._session, self._plugin.session_name)`) use it. One resolution implementation shared with the CLI; `AmbiguousSessionError` propagates to the caller.
- **`session` / `observe` commands**: already flow through `resolve_session` + `resolve_session_name`; the latter (`src/graftpunk/cli/plugin_commands.py:379` (`def resolve_session_name`)) keeps only its site-name→base-name mapping and then calls `resolve_account_session`. Names already containing `@`, and names that are not plugin site names, pass through unchanged.
- **`get_plugin_for_session`** (`src/graftpunk/cli/plugin_commands.py:364` (`def get_plugin_for_session`)) compares by the `split_session_name` base, so labelled sessions still map back to their plugin.

> **Design note (2026-08-26, v2):** v1 wired resolution into `resolve_session_name` and the client only, leaving the plugin-command path — the one the issue is about — loading by the ambient attribute, and left write-backs keyed to the bare name (a resolved `myshop@alice` would have refreshed cookies into a new bare `myshop` slot, recreating the bug). The invariant is now explicit: **load key == store key == the operating session name, computed once per invocation.**

### Metadata

`SessionMetadata` gains **one** optional field, defaulting to `None` so all three backends deserialize pre-upgrade metadata unchanged:

- `account_identifier: str | None` — the unslugified login identifier (e.g. the username or email typed into the form), for display. This answers "whose session is this?" without a network call — the issue's core ask.

The label needs no field: it is derived from the session name via `split_session_name` wherever it is displayed, so the name stays the single source of truth.

**The identifier rides the session object**, following the existing idiom for session-derived metadata (`session_name`, `current_url`): the login path sets the attribute named by `GP_ACCOUNT_ATTR` once, `_extract_session_metadata` (`src/graftpunk/cache.py:177` (`def _extract_session_metadata`)) reads it via `getattr` like the fields it already collects, and — because sessions are pickled whole — the attribute survives load/save round-trips. `cache_session`'s signature does **not** change and it stays **read-free**: every existing writer, most importantly the refresh path `update_session_cookies` (`src/graftpunk/cache.py:641` (`def update_session_cookies`)), preserves the identity for free because it re-caches the same (unpickled) object.

### Login flow

- `gp <site> login` gains `--as <label>` (validated: same charset as the label half of a session name).
- **One owner for derivation — the CLI login wrapper.** `make_login_body` (`src/graftpunk/cli/login_commands.py:143` (`def make_login_body`)) is where the resolved credentials dict and the secret policy (`src/graftpunk/cli/login_commands.py:32` (`SECRET_KEYWORDS = frozenset`)) already live, and it wraps **both** the declarative path and hand-written `login(credentials)` methods (credentials resolved at `src/graftpunk/cli/login_commands.py:181` (`credentials: dict[str, str] = {}`)). It derives the **identifier**: iterate the identifier keywords `username`, `email`, `login`, `identifier`, `user` in that order (keyword-major — for each keyword, scan fields in declaration order; the `user`⊂`username` overlap is intentional because `username` is checked first), taking the first field whose name contains the keyword; if none matches, the first non-secret field. The **label** is the slugified identifier; `--as` overrides the label but not the recorded identifier.
- **Replacement warning — at the login boundary, not in the write path.** Before invoking the login callable, the wrapper fetches the target slot's stored metadata **once** via `get_session_metadata` (`src/graftpunk/cache.py:222` (`def get_session_metadata`)); after a successful login it compares stored vs derived identifier and, when **both are present and unequal**, emits a structlog WARNING (reaching the console via the standard stderr logging config). `cache_session` stays identity-agnostic and read-free, so refresh writes pay no metadata fetch. A missing identifier on either side never warns; same-identifier re-login stays a silent refresh.
- **Transport — the plugin instance, then the session object.** The wrapper stamps the plugin instance before invoking the login callable: `plugin.session_name = join_session_name(base, label)` (an instance attribute shadowing the class attribute) and the identifier under `GP_ACCOUNT_ATTR`. The cache sites all read `plugin.session_name` already — the generated flows (`src/graftpunk/plugins/login_engine.py:735` (`async def _run_nodriver_steps`), `src/graftpunk/plugins/login_engine.py:847` (`def _generate_selenium_login`)) and the `browser_session()` helpers custom logins cache through (`src/graftpunk/plugins/cli_plugin.py:1066` (`async def browser_session`), `src/graftpunk/plugins/cli_plugin.py:1105` (`def browser_session_sync`)) — so the name needs no new plumbing. Each of those four sites caches through **one shared funnel helper** (`_cache_login_session(plugin, session)`, beside the engine's existing session helpers) that copies the `GP_ACCOUNT_ATTR` attribute from plugin to session and calls `cache_session` — the copy is a function call, not an invariant each site must remember, and a future login flow has one obvious thing to call.
- **Scope of the stamp:** login-flow-scoped, by contract — applied immediately before the login callable, meaningful only until the login's cache write completes. Longer-lived hosts (keepalive, embedded clients) read the operating session name from the resolution owner, never from the plugin instance.
- **`@command`-decorated logins** are the one path with no wrapper and no credentials dict (`src/graftpunk/cli/login_commands.py:50` (`return login_method(plugin) is not None`) — callables carrying `_command_meta` are excluded): they cache under the bare legacy name with no identifier, exactly today's behaviour; `--as` does not apply (it is an option of `gp <site> login`, which they bypass).
- After a successful login, `gp` prints the full session name it cached. If the shell's current selection (env or `.gp-session`) resolves to a *different* session of the same plugin, it prints a one-line hint naming `gp session use <name>`. It never writes `.gp-session` itself.

### Typed error

`AmbiguousSessionError(GraftpunkError)` joins `src/graftpunk/exceptions.py:83` (`class TokenExtractionError`)'s file, carrying the base name and the candidate session names. **One shared renderer** — a small function beside the `gp_console` helpers — prints the candidates (with lazily fetched account identifiers) plus the three ways to pick (`--session`, `GRAFTPUNK_SESSION`, `gp session use`). Every CLI boundary that can see the error calls that renderer: the plugin-runtime error boundary, the `session_commands` call sites, and the `observe` callback — the rendering exists once, whichever door the error exits through. It is **not** a `ValueError`: nothing raised `ValueError` on this path before, so there is no compatibility to preserve.

### CLI surfaces

- `gp session list` (`src/graftpunk/cli/session_commands.py:63` (`def session_list`)) gains an **Account** column: `account_identifier`, else the label split from the name, else `—`.
- `gp session show` prints the identifier and the label (derived from the name).
- `session use` / `clear` / `export` operate on full session names already and need no changes beyond accepting `@` (which the validation change provides).

### Keepalive

The daemon keys on session names (`src/graftpunk/keepalive/state.py:57` (`current_session: str`)); distinct accounts are distinct names, so multi-account works without changes. Noted as a test concern only.

## Error handling summary

| Situation | Behaviour |
|---|---|
| Several sessions cached, none selected (any surface) | `AmbiguousSessionError` → shared renderer lists candidates and pick mechanisms; exit non-zero |
| Selected session does not exist | unchanged (existing not-found path) |
| Re-login, same identifier | silent refresh (unchanged) |
| Login over a slot whose stored identifier is present and differs | WARNING at the login boundary; cache proceeds |
| Refresh write (no login) | never warns, never reads metadata |
| `--as` with invalid characters | `ValueError` at the CLI boundary, message names the allowed charset |

## Testing

- `session_identity`: grammar round-trip (bare, labelled, inverse), one-`@` validation (not at edges, two rejected, all previously valid names still valid), `resolve_account_session` matrix over {zero, one bare, one labelled, several} candidates.
- Operating-name precedence per surface: plugin command with `--session` / env / `.gp-session` / nothing × {one, several} cached; the same matrix through `GraftpunkClient` (pin and unpinned).
- **Load key == store key:** after resolving to `myshop@alice`, the post-command write-back and the client write-backs update `myshop@alice`, and no bare `myshop` slot appears.
- Label derivation in `make_login_body`: keyword-major field pick (each keyword), non-secret fallback, slugification, `--as` override; a hand-written `login(credentials)` method gets the same derivation; a `@command`-decorated login falls back to the bare name with no identifier.
- Replacement warning: fires only at the login boundary when both identifiers present and unequal; refresh writes perform no metadata read (assert via a counting fake backend).
- Identity survives a load → `update_session_cookies` re-cache round-trip (the pickled attribute is preserved).
- `SessionMetadata` round-trip with and without `account_identifier` on all three backends' serializers; pre-upgrade metadata (field absent) still loads.
- `get_plugin_for_session` maps `myshop@alice` back to the plugin.
- The shared renderer is exercised from a plugin command and a session command; the client re-raises rather than rendering.
- `gp session list` shows the Account column; `gp session show` shows both values.
- End-to-end: two logins with different identifiers on one plugin coexist; commands refuse until pinned; pinning each way works; keepalive tracks each name independently.

## Non-goals

- Server-verified identity (a per-plugin "who am I" hook can layer on later; the recorded identifier is what the user typed, not what the server confirmed — stated in the `account_identifier` docstring).
- Per-process or per-worktree cache isolation; the cache stays user-wide (shared across all of the user's shells and processes) by design.
- Auto-writing `.gp-session` on login.
- Changing what `.gp-session` / `GRAFTPUNK_SESSION` contain (they hold full session names, as today).
