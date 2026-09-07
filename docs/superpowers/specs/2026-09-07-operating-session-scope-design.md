---
type: spec
---

# Operating session scope: `get_session()` agrees with the CLI, and the login stamp retires

**Issue:** https://github.com/stavxyz/graftpunk/issues/174 (the two remaining halves)
**Date:** 2026-09-07
**Status:** approved 2026-09-07 (plan validated at 5b8dc51)

## Problem

Sessions are keyed by plugin and account (`base@label`). Two residuals from that
work are still open, and they share a root: the plugin instance is the only
channel author-facing code has for "which account is this invocation about",
and the plugin instance is the wrong place for per-invocation state.

1. `SitePlugin.get_session()` loads by the bare `self.session_name`
   (`src/graftpunk/plugins/cli_plugin.py:1199` (`return load_session_for_api(self.session_name)`)). The CLI
   runtime resolves the operating name itself and never calls it
   (`src/graftpunk/cli/plugin_runtime.py:55` (`def run_plugin_command`)), so an
   author who calls `self.get_session()` from a command handler while the CLI is
   pinned to `myshop@bob` gets whatever the bare name resolves to: `myshop@alice`
   when it is the only slot, or `AmbiguousSessionError` when two accounts are
   cached. The write-back half of this was fixed by PR #188; the load half is
   this document.

2. `_stamp_login_identity` (`src/graftpunk/cli/login_commands.py:157` (`def _stamp_login_identity`)) temporarily overwrites `plugin.session_name`
   and the `GP_ACCOUNT_ATTR` attribute on the plugin instance for one login flow
   so that hand-written `login(credentials)` methods and the `browser_session()`
   helpers (`src/graftpunk/plugins/cli_plugin.py:1081` (`async def browser_session`),
   `src/graftpunk/plugins/cli_plugin.py:1122` (`def browser_session_sync`)),
   which cache through `cache_login_session(self, session)` with no arguments,
   land on the account-qualified slot. It mutates a shared object, has a
   read-only-property failure mode it can only warn about, and its restore
   depends on a `finally` that a plugin holding a reference across the flow can
   still observe mid-flight.

## Design

One new, small mechanism serves both halves: an in-process **operating session
scope**, a `contextvars.ContextVar` holding the operating name (and, during a
login, the account identifier) for the duration of one dispatch. The runtime and
the client set it around a command; the login command sets it around the login
callable. Author-facing entry points that today read the plugin instance read the
scope instead. The plugin instance is never mutated.

### New module: `src/graftpunk/session_scope.py`

```python
@dataclass(frozen=True)
class OperatingSession:
    name: str                       # the slot, e.g. "myshop@alice" or "myshop"
    identifier: str | None = None   # the unslugified login identifier, login flows only

@contextmanager
def operating_session(name: str, identifier: str | None = None) -> Iterator[OperatingSession]:
    """Set the scope for the duration of the block; restores the prior value on exit
    (ContextVar.set/reset with the token), including when the block raises."""

def current_operating_session() -> OperatingSession | None:
    """The scope in effect, or None outside any dispatch."""

def operating_session_for(base: str) -> OperatingSession | None:
    """The scope in effect if its name's base is *base*, else None.

    The base-scoped guard: a scope set for plugin A must never steer plugin B's
    get_session() or cache write. Same rule as the ambient-pin guard in
    plugin_runtime (#176)."""
```

`name` is validated with `validate_session_name` on entry, so a scope can never
hold a name the cache would refuse. The module imports only `session_identity`
(for `split_session_name` and `validate_session_name`). That floor is set by
its actual importers: the plugin base class
(`src/graftpunk/plugins/cli_plugin.py:1202` (`def cache_login_session(`)), the
shared execution pipeline
(`src/graftpunk/client.py:81` (`def _run_handler_with_limits`)), and the login
command (`src/graftpunk/cli/login_commands.py:241` (`def make_login_body`)).
Staying below all three keeps it free of storage and CLI dependencies, so none
of them can form a cycle through it.

Why a ContextVar and not a parameter alone: the case the issue describes is an
author inside a command handler, who holds `ctx` but not a name, and whose call
site is `self.get_session()` with nothing to pass. A parameter serves callers
that already hold a name (scripts, tests); the scope serves the ones that do
not. Why not the plugin instance: that is the stamp, and its defects are the
second half of the issue. Why not `session_context.py`: that module is the
on-disk ambient pin (`.gp-session`, `GRAFTPUNK_SESSION`,
`src/graftpunk/session_context.py:14` (`def get_active_session`)), which the
library path deliberately ignores (#181). The scope is in-process state set by
the dispatcher that already resolved the name, so it is honoured by library and
CLI alike.

Propagation facts the design rests on, from the Python 3.12 documentation:
an asyncio Task "copies the current context and later runs its coroutine in the
copied context" when no `context` is given (asyncio-task.html, `asyncio.Task`),
and `asyncio.Runner.run` runs in "the runner's default context" when none is
passed (asyncio-runner.html). The login command runs async logins via
`asyncio.run(login_method(...))` inside the scope; a test in this design proves
the scope is visible inside that coroutine rather than relying on the docs.
Threads do not inherit context ("Every thread will have a different top-level
Context object", contextvars.html, Manual Context Management); the scope is set
and read on the dispatching thread, and `GraftpunkClient` already serialises its
first load under a lock, so nothing here crosses a thread.

### Half 1: `get_session()` follows explicit, then scope, then bare

`SitePlugin.get_session(self, session_name: str | None = None) -> requests.Session`
(the method whose body is `src/graftpunk/plugins/cli_plugin.py:1199` (`return load_session_for_api(self.session_name)`)); the
`get_session` declaration on `CLIPluginProtocol` (`src/graftpunk/plugins/cli_plugin.py:657` (`class CLIPluginProtocol`))
gains the same optional parameter.

Precedence, one chain:

1. `session_name` given: load it. A labelled name loads exact; a bare name is a
   base and the loader resolves it (the pin contract:
   `resolve = split_session_name(name)[1] is None`).
2. Else `operating_session_for(self.session_name)` is set: load that name. A
   labelled scope name is exact by construction; a bare one is a base and
   resolves like any bare name. On the dispatch path the bare slot, when it
   exists, answers before any listing.
3. Else the bare `self.session_name`, resolving, exactly as today.

The scope has one owner: the shared execution pipeline both dispatchers
already call, `_run_handler_with_limits` (`src/graftpunk/client.py:81` (`def _run_handler_with_limits`)),
sets it from `ctx._operating_session_name` around the handler, and sets none
when that field is empty (a `requires_session=False` command). Both dispatchers
already populate that field when they build the `CommandContext`
(`src/graftpunk/cli/plugin_runtime.py:219` (`_operating_session_name=operating_name,`);
`src/graftpunk/client.py:493` (`_operating_session_name=(operating_name if needs_session else ""),`)),
so neither is edited, the `needs_session` condition is written once, each 403
retry re-enters the scope on its own, and a dispatcher added later gets the
scope for free.

> **Design note (2026-09-07):** the first draft had each dispatcher set the
> scope beside the field it already writes; the validation review flagged two
> carriers of one fact with nothing keeping them in step, which is the class of
> disagreement #174 exists to close. The pipeline is the single owner instead.

`get_session()` returns a **second** `requests.Session`, not `ctx.session`;
that is unchanged and stays documented. What changes is that the second one is
now the same account.

### Half 2: the login stamp retires; the scope carries the identity

`make_login_body` (`src/graftpunk/cli/login_commands.py:241` (`def make_login_body`))
already computes `target_name` and `identifier` before the call
(`src/graftpunk/cli/login_commands.py:307` (`target_name = (`)). The `with`
at `src/graftpunk/cli/login_commands.py:334` (`_stamp_login_identity(plugin, label, identifier),`)
becomes `operating_session(target_name, identifier)`. `_stamp_login_identity`,
its `_MISSING` sentinel, and the `GP_ACCOUNT_ATTR` import in `login_commands.py`
are deleted.

`cache_login_session` (`src/graftpunk/plugins/cli_plugin.py:1202` (`def cache_login_session`)) keeps its signature and its "explicit first" rule;
its fallback reads the scope instead of the instance:

```python
scope = operating_session_for(plugin.session_name) if name is None else None
session_name = name if name is not None else (scope.name if scope else plugin.session_name)
account = identifier if identifier is not None else (scope.identifier if scope else None)
if account is None:
    try:
        stored = get_session_metadata(session_name)
    except Exception as exc:
        LOG.warning("login_identity_carry_forward_failed", session=session_name, ...)
        stored = None
    account = stored.get("account_identifier") if stored else None
```

> **Design note (2026-09-07):** the scope is read only when *name* is None, so
> an explicit name never borrows the scope's identity: a caller naming the slot
> is naming the account too, or recording none. The carry-forward covers the
> other direction. A command scope carries no identifier (only a login sets
> one), so a write under one would otherwise record `account_identifier=None`
> over the account the slot already names; reading the slot's stored metadata
> once, here in the funnel, keeps `cache_session` read-free while a refresh
> write preserves the account. That read is advisory: it runs on the login
> path, where the session in hand is the thing worth keeping, so a storage
> failure logs `login_identity_carry_forward_failed` and the write goes ahead
> recording no account. Only the read is wrapped; a `cache_session` failure
> still raises.

The `getattr(plugin, GP_ACCOUNT_ATTR, None)` read goes away; nothing writes that
attribute on a plugin any more. The generated login flows are untouched: they
receive `session_name` and `account_identifier` explicitly
(`src/graftpunk/plugins/login_engine.py:645` (`def _generate_nodriver_login`),
`src/graftpunk/plugins/login_engine.py:869` (`def _generate_selenium_login`)).
`browser_session()` and `browser_session_sync()` keep calling
`cache_login_session(self, session)` with no arguments; their comments change
from "the login stamp" to "the operating session scope".

**Behaviour change for hand-written logins.** During `gp <site> login --as bob`,
`self.session_name` used to read `myshop@bob` inside the plugin's own `login()`;
it now reads `myshop`, the class attribute, always. A hand-written login that
caches through the helpers or through `cache_login_session(self, session)` is
unaffected. One that calls `cache_session(session, self.session_name)` by hand
now lands on the bare slot instead of the labelled one. The migration is one
line: call `cache_login_session(self, session)` (public; this change exports it
from `graftpunk.plugins` beside `SitePlugin`. `operating_session` is not
exported and stays internal to `graftpunk.session_scope`: there is no author
use case for setting the scope by hand, since the dispatchers set it, and the
explicit `session_name` parameter on `get_session()` is the channel for code
running outside a dispatch) or accept `session_name` and `account_identifier`
as keyword arguments, which `_accepted_login_kwargs`
(`src/graftpunk/cli/login_commands.py:125` (`def _accepted_login_kwargs`))
already passes to any login that declares them. The CHANGELOG records this
under Changed; the author docs (`docs/HOW_IT_WORKS.md`, "Custom Login Method")
say which name a hand-written login should cache under and how. Backwards
compatibility for by-hand `cache_session(session, self.session_name)` callers is
deliberately not preserved: keeping the stamp for their sake is keeping the
defect.

### What does not change

- `CommandContext._operating_session_name` and `ctx.save_session()`: the
  handler's explicit channel stays, and remains the recommended one.
- `load_session_for_api`, `load_session_for_api_resolved`,
  `update_session_cookies`, the rider registry, the pin contract, resolution
  precedence, ambient pins.
- `GraftpunkClient`'s construction-time pin validation and lazy resolution.

### Error handling

- `operating_session(name)` with an invalid name raises `ValueError` from
  `validate_session_name` before setting anything. Both dispatchers pass names
  that already loaded, so this is a programming-error guard, not a user path.
- `get_session()` errors are unchanged in kind: `SessionNotFoundError`,
  `AmbiguousSessionError` (only on a bare name, from whichever tier supplied
  it), `ValueError` on an invalid explicit name. A labelled name loads exact,
  so it can only miss.
- A scope leaking past its block is prevented by `ContextVar.reset(token)` in a
  `finally`, and tested with a raising block.

### Testing

Tests assert behaviour through the public surfaces and a fresh backend
(`fresh_backend` fixture), never mock-call counts.

- `session_scope`: set/read/restore; restore after a raise; `operating_session_for`
  returns None for a foreign base; nesting restores the outer value; invalid
  name refused; the scope is visible inside `asyncio.run(coro())` started
  within the block (the propagation proof).
- `get_session()`: explicit labelled name loads exact; explicit bare name
  resolves a single account and raises `AmbiguousSessionError` on two; a
  labelled scope for the same base loads that slot exactly with two accounts
  cached (the issue's scenario, no ambiguity error); a bare scope resolves like
  any bare name; scope for a foreign base is ignored and
  the bare path runs; no scope behaves as today.
- End to end through the CLI harness (`invoke_plugin_app`): a handler that
  calls `self.get_session()` under `--session myshop@bob` with `myshop@alice`
  and `myshop@bob` cached gets bob's session (its `GP_ACCOUNT_ATTR` is
  `bob@example.com`). Same through `GraftpunkClient(session="myshop@bob")`.
- Login: `gp <site> login --as bob` with a hand-written `login()` that caches
  through `browser_session_sync()` (browser mocked as the existing tests do)
  lands on `myshop@bob` with the identifier recorded; `self.session_name` read
  inside `login()` is the bare base; a login that raises leaves no scope behind;
  the generated flows still cache under the explicit name they are handed.
- The `TestStamp` class in `tests/unit/test_login_identity.py` is deleted with
  the stamp; `test_instance_fallback_for_author_facing_paths` becomes a
  scope-fallback test; the `seen["identifier"]` assertion in the hand-written
  login tests (`tests/unit/test_login_identity.py:321` (`seen["identifier"] = getattr(self, GP_ACCOUNT_ATTR, None)`)) is replaced by
  asserting what the cache received.

### Documentation

- `docs/HOW_IT_WORKS.md`, "Custom Login Method": one paragraph on where the
  account-qualified name comes from during a login and the two supported ways
  to cache (helpers, or `cache_login_session`), and one line under the session
  resolution section that `get_session()` follows the CLI's resolution inside a
  command and accepts an explicit name outside one.
- `CHANGELOG.md` `[Unreleased]`: Added (`session_scope`, `get_session(session_name=)`),
  Changed (stamp retired; the `self.session_name` reading inside a hand-written
  login), both referencing #174.
- Issue #174 closes with the PR.
