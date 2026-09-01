---
type: spec
---

# Session account identity — multi-account slots keyed by plugin + account

**Status:** design, awaiting validation
**Issue:** [#151](https://github.com/stavxyz/graftpunk/issues/151)
**Scope:** the session cache's naming and metadata contract, login-time labelling, session resolution, and the CLI/Python surfaces that show or pick sessions. No change to encryption, storage backends' key schemes, or the keepalive daemon.

## Problem

The session cache is keyed by the plugin's `session_name` alone — a fixed string per plugin — at a machine-wide path, and `cache_session` overwrites the slot silently (`src/graftpunk/cache.py:260` (`def cache_session`)). Metadata records the site, cookie counts and timestamps but not *who is signed in* (`src/graftpunk/storage/base.py:46` (`class SessionMetadata`)). So one `gp <plugin> login` as a different account silently repoints every other shell, agent and process on the machine, with no signal at write time and no way to tell at read time. This happened in practice on 2026-08-24 with two agents on one plugin.

## Decisions (made with the owner, 2026-08-26)

1. **Multi-account slots**, not single-slot-plus-warning: sessions are keyed by plugin **and** account, so a second account's login creates a second slot instead of evicting the first.
2. **Credential-derived label with `--as` override**: the account label defaults to the slugified login identifier the engine already holds; `gp <site> login --as <label>` overrides. No server round-trip.
3. **Resolution precedence** `--session` flag > `GP_SESSION` env > `.gp-session` in cwd, then: exactly one cached session for the plugin → use it; several → refuse with a typed error listing candidates. Never "most recent wins". (Flag > env > per-directory file matches git/AWS CLI/pyenv convention and graftpunk's existing order: `src/graftpunk/session_context.py:64` (`def resolve_session`), `src/graftpunk/session_context.py:14` (`def get_active_session`).)

## Design

### Naming

A session name becomes `<session_name>` or `<session_name>@<label>`, e.g. `myshop` or `myshop@alice`.

- `validate_session_name` (`src/graftpunk/cache.py:63` (`def validate_session_name`)) permits **at most one `@`**, not as the first or last character; the charset on each side stays `[a-z0-9][a-z0-9_-]*` (`src/graftpunk/cache.py:55` (`_SESSION_NAME_RE`)); dots remain forbidden.
- The full `name@label` string is what flows everywhere a session name flows today — local directory names, S3/Supabase object keys, `.gp-session`, `GP_SESSION`, `--session`, `gp session use`. Storage backends need **no key-scheme changes**; the implementation plan must verify `@` is accepted by each backend's path/key handling (`src/graftpunk/storage/base.py:135` (`class SessionStorageBackend`)) and by the local directory layout.
- A session cached with no derivable identity keeps the bare legacy name. Every pre-upgrade session already has a bare name, so **no migration** is needed or performed.

### Metadata

`SessionMetadata` gains two optional fields, defaulting to `None` so all three backends deserialize pre-upgrade metadata unchanged:

- `account_label: str | None` — the label half of the session name, verbatim.
- `account_identifier: str | None` — the unslugified login identifier (e.g. the username or email typed into the form), for display. This answers "whose session is this?" without a network call — the issue's core ask.

`_extract_session_metadata` (`src/graftpunk/cache.py:177` (`def _extract_session_metadata`)) passes them through when the caller provides them; `cache_session` grows optional `account_label=None, account_identifier=None` keyword parameters.

### Login flow

- `gp <site> login` gains `--as <label>` (validated: same charset as the label half of a session name).
- **Declarative path** (login engine, which caches at `src/graftpunk/plugins/login_engine.py:843` (`cache_session(session, plugin.session_name)`)): the engine has the resolved credentials dict (`src/graftpunk/cli/login_commands.py:143` (`def make_login_body`)). The **identifier** is the value of the first field whose name contains one of `username`, `email`, `login`, `identifier`, `user` (checked in that order); if none matches, the first field that is not secret per the existing policy (`src/graftpunk/cli/login_commands.py:32` (`SECRET_KEYWORDS`)). The **label** is the slugified identifier; `--as` overrides the label but not the recorded identifier.
- **Custom `login()` methods** (the engine cannot see credentials): label from `--as` if given, else the bare legacy name — exactly today's behaviour.
- After a successful login, `gp` prints the full session name it cached. If the shell's current selection (env or `.gp-session`) resolves to a *different* session of the same plugin, it prints a one-line hint naming `gp session use <name>`. It never writes `.gp-session` itself.
- **Replacement warning:** when `cache_session` overwrites an existing session whose stored `account_identifier` differs from the incoming one (possible only on the bare legacy slot or a reused `--as` label), it logs a WARNING and the CLI surfaces it. Same-identifier re-login stays a silent refresh.

### Resolution

`resolve_session_name` (`src/graftpunk/cli/plugin_commands.py:379` (`def resolve_session_name`)) currently maps a plugin's site name to its single `session_name`. It becomes account-aware:

1. If the name is already a full session name (contains `@`, or is not a plugin site name), return it unchanged.
2. Otherwise list the plugin's cached sessions — the bare `session_name` plus any `session_name@*` — via `list_sessions_with_metadata` (`src/graftpunk/cache.py:693` (`def list_sessions_with_metadata`)).
3. Exactly one → return it. Zero → return the bare `session_name` (today's behaviour; the downstream not-found path is unchanged). More than one → raise `AmbiguousSessionError`.

The flag/env/file precedence is untouched and sits **above** this: an explicit selection short-circuits resolution as it does today.

### Typed error

`AmbiguousSessionError(GraftpunkError)` joins `src/graftpunk/exceptions.py:83` (`class TokenExtractionError`)'s file, carrying the plugin name and the candidate session names. The CLI catches it and prints the candidates plus the three ways to pick (`--session`, `GP_SESSION`, `gp session use`). It is **not** a `ValueError`: nothing raised `ValueError` on this path before, so there is no compatibility to preserve.

### Python API

`GraftpunkClient` (`src/graftpunk/client.py:200` (`class GraftpunkClient`)) gains an optional `session: str | None = None` constructor argument. When set, commands load that session; when unset, the same resolution (including `AmbiguousSessionError` on several) applies, so library consumers get the same safety as the CLI.

### CLI surfaces

- `gp session list` (`src/graftpunk/cli/session_commands.py:63` (`def session_list`)) gains an **Account** column: `account_identifier`, else `account_label`, else `—`.
- `gp session show` prints both account fields.
- `session use` / `clear` / `export` operate on full session names already and need no changes beyond accepting `@` (which the validation change provides).

### Keepalive

The daemon keys on session names; distinct accounts are distinct names, so multi-account works without changes. Noted as a test concern only.

## Error handling summary

| Situation | Behaviour |
|---|---|
| Several sessions cached, none selected | `AmbiguousSessionError` → CLI lists candidates and pick mechanisms; exit non-zero |
| Selected session does not exist | unchanged (existing not-found path) |
| Re-login, same identifier | silent refresh (unchanged) |
| Re-login over a slot with a different stored identifier | WARNING logged and printed; cache proceeds |
| `--as` with invalid characters | `ValueError` at the CLI boundary, message names the allowed charset |

## Testing

- `validate_session_name`: one `@` allowed, not at the edges; two `@` rejected; all previously valid names still valid.
- Resolution matrix: {flag, env, file, nothing} × {zero, one bare, one labelled, several} cached sessions.
- Label derivation: keyword field pick (each keyword), non-secret fallback, slugification, `--as` override, custom-login fallback to the bare name.
- Replacement warning fires only on identifier mismatch; silent on match and on first cache.
- `SessionMetadata` serialization round-trip with and without the new fields on all three backends' serializers; pre-upgrade metadata (fields absent) still loads.
- `GraftpunkClient`: `session=` pin honoured; `AmbiguousSessionError` raised when unpinned with several cached.
- `gp session list` shows the Account column; `gp session show` shows both fields.
- End-to-end: two logins with different identifiers on one plugin coexist; commands refuse until pinned; pinning each way (flag, env, file) works.

## Non-goals

- Server-verified identity (a per-plugin "who am I" hook can layer on later; the recorded identifier is what the user typed, not what the server confirmed — stated in the `account_identifier` docstring).
- Per-process or per-worktree cache isolation; the cache stays machine-wide by design.
- Auto-writing `.gp-session` on login.
- Changing what `.gp-session` / `GP_SESSION` contain (they hold full session names, as today).
