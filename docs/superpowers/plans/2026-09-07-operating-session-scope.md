# Operating Session Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `SitePlugin.get_session()` loads the same account the CLI or the client already resolved for that invocation, and the login identity stamp that mutated the plugin instance is deleted (#174).

**Architecture:** One new dependency-light module, `src/graftpunk/session_scope.py`, holds the operating session (the slot, plus the account identifier during a login) in a `contextvars.ContextVar` for the duration of one dispatch. The two dispatchers set it around the handler they already resolved a name for (`plugin_runtime.run_plugin_command` and `GraftpunkClient._execute_command`), and the login command sets it around the login callable. The author-facing entry points that read the plugin instance today (`get_session`, `cache_login_session`) read the scope instead, base-scoped so a scope set for one plugin can never steer another. Nothing mutates a plugin instance.

**Tech Stack:** Python 3.11+, Typer CLI, requests, structlog, pytest (`uv run pytest`), ruff, ty 0.0.75.

**Spec:** `docs/superpowers/specs/2026-09-07-operating-session-scope-design.md` (approved; read it alongside this plan, and resolve any conflict in favour of the spec).

## Global Constraints

- Python `>=3.11` typing throughout: `X | None`, never `Optional[X]`; `from __future__ import annotations` at the top of every new module, matching the files being modified.
- structlog event-style logging where a module logs at all: `LOG = get_logger(__name__)` at module scope, event name first, values as keyword arguments. `session_scope.py` logs nothing, by design: it imports only `graftpunk.session_identity` so `cache.py`, `cli_plugin.py` and the CLI can all import it without a cycle.
- Gate command, green at every commit: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
- Tests assert behaviour through public surfaces and the real `fresh_backend` cache. Never assert mock-call counts (`assert_called_once_with`, `call_count`); where a seam must be patched, assert on the values the seam received (`mock.call_args[0][1]`).
- Placeholders only, in tests and docs: `myshop`, `alice@example.com`, `bob@example.com`, `fmtsite`. Never name a real site, store, account, or domain.
- No em dashes or en dashes anywhere in new prose, docstrings, comments, or commit messages. Use commas, colons, parentheses, or separate sentences.
- No attribution trailers in commits. Commit subjects are `type(scope): subject` and reference `#174`.
- `cache_session`'s signature does not change and it stays read-free.
- `CommandContext._operating_session_name` and `ctx.save_session()` are untouched and remain the recommended explicit channel for a command handler.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/graftpunk/session_scope.py` (new) | Owns `OperatingSession`, the ContextVar, and the three accessors. Pure in-process state; no logging, no storage, no CLI. |
| `src/graftpunk/plugins/cli_plugin.py` | `get_session` gains the precedence chain; `cache_login_session`'s fallback reads the scope; two `browser_session` comments change. |
| `src/graftpunk/cli/plugin_runtime.py` | Sets the scope around the handler call and its 403 retry. |
| `src/graftpunk/client.py` | Same, around `_run_handler_with_limits` and its retry. |
| `src/graftpunk/cli/login_commands.py` | `make_login_body` sets the scope; `_stamp_login_identity`, `_MISSING`, and three now-unused imports are deleted. |
| `src/graftpunk/plugins/__init__.py` | Re-exports `cache_login_session` and `operating_session` beside `SitePlugin`. |
| `tests/unit/test_session_scope.py` (new) | The module's own contract, including the `asyncio.run` propagation proof. |
| `tests/unit/test_api_session_resolution.py` | `get_session`'s precedence chain against the real backend. |
| `tests/unit/test_multi_account_e2e.py` | End to end through `invoke_plugin_app` and `GraftpunkClient`. |
| `tests/unit/test_login_identity.py` | The stamp's tests are deleted; the funnel's fallback and the hand-written login tests move to the scope. |
| `docs/HOW_IT_WORKS.md`, `CHANGELOG.md` | Author-facing documentation of the behaviour change. |

---

### Task 1: `session_scope` module

**Files:**
- Create: `src/graftpunk/session_scope.py`
- Test: `tests/unit/test_session_scope.py`

**Interfaces:**
- Consumes: `graftpunk.session_identity.split_session_name` and `graftpunk.session_identity.validate_session_name` (`src/graftpunk/session_identity.py:63` (`def split_session_name(name: str) -> tuple[str, str | None]:`), `src/graftpunk/session_identity.py:74` (`def validate_session_name(name: str) -> None:`)). Nothing else, so the module is importable from `cache.py`, `cli_plugin.py`, and the CLI without a cycle.
- Produces:
  - `OperatingSession` (frozen dataclass) with fields `name: str` and `identifier: str | None = None`.
  - `operating_session(name: str, identifier: str | None = None) -> Iterator[OperatingSession]`, a `@contextmanager` that yields the `OperatingSession` it set.
  - `current_operating_session() -> OperatingSession | None`.
  - `operating_session_for(base: str) -> OperatingSession | None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_session_scope.py`:

```python
"""The operating session scope: the slot one dispatch is about (#174)."""

from __future__ import annotations

import asyncio

import pytest

from graftpunk.session_scope import (
    OperatingSession,
    current_operating_session,
    operating_session,
    operating_session_for,
)


def test_no_scope_outside_a_dispatch() -> None:
    assert current_operating_session() is None
    assert operating_session_for("myshop") is None


def test_set_read_and_restore() -> None:
    with operating_session("myshop@alice", "alice@example.com") as scope:
        assert scope == OperatingSession("myshop@alice", "alice@example.com")
        current = current_operating_session()
        assert current is not None
        assert current.name == "myshop@alice"
        assert current.identifier == "alice@example.com"
    assert current_operating_session() is None


def test_identifier_defaults_to_none() -> None:
    with operating_session("myshop"):
        current = current_operating_session()
        assert current is not None
        assert current.identifier is None


def test_restores_when_the_block_raises() -> None:
    """The reset is in a finally: a failed handler must not leak the scope."""
    with pytest.raises(RuntimeError), operating_session("myshop@alice"):
        raise RuntimeError("handler blew up")
    assert current_operating_session() is None


def test_for_base_matches_a_labelled_scope() -> None:
    with operating_session("myshop@alice"):
        scope = operating_session_for("myshop")
        assert scope is not None
        assert scope.name == "myshop@alice"


def test_for_base_matches_a_bare_scope() -> None:
    with operating_session("myshop"):
        scope = operating_session_for("myshop")
        assert scope is not None
        assert scope.name == "myshop"


def test_for_base_ignores_a_foreign_base() -> None:
    """A scope set for one plugin must never steer another (the base-scoped guard)."""
    with operating_session("othersite@bob"):
        assert operating_session_for("myshop") is None
        assert current_operating_session() is not None


def test_nesting_restores_the_outer_value() -> None:
    with operating_session("myshop@alice"):
        with operating_session("myshop@bob"):
            inner = current_operating_session()
            assert inner is not None
            assert inner.name == "myshop@bob"
        outer = current_operating_session()
        assert outer is not None
        assert outer.name == "myshop@alice"
    assert current_operating_session() is None


@pytest.mark.parametrize("name", ["", "MyShop@Alice", "../../x", "myshop@", "a@b@c"])
def test_an_invalid_name_is_refused(name: str) -> None:
    with pytest.raises(ValueError), operating_session(name):
        pass  # pragma: no cover - the context manager raises before it yields


def test_an_invalid_name_leaves_the_outer_scope_untouched() -> None:
    """Validation runs before the ContextVar is set, so a refusal changes nothing."""
    with operating_session("myshop@alice"):
        with pytest.raises(ValueError), operating_session("MyShop@Alice"):
            pass  # pragma: no cover - the context manager raises before it yields
        current = current_operating_session()
        assert current is not None
        assert current.name == "myshop@alice"


def test_the_scope_is_visible_inside_asyncio_run() -> None:
    """The propagation proof, not the docs: the login command runs async logins
    via asyncio.run() inside the scope, and the coroutine must see it."""
    seen: list[str | None] = []

    async def coro() -> None:
        scope = current_operating_session()
        seen.append(scope.name if scope else None)

    with operating_session("myshop@bob", "bob@example.com"):
        asyncio.run(coro())

    assert seen == ["myshop@bob"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_session_scope.py -q`

Expected: collection error, `ModuleNotFoundError: No module named 'graftpunk.session_scope'`.

- [ ] **Step 3: Write the implementation**

Create `src/graftpunk/session_scope.py`:

```python
"""The operating session scope: which account one dispatch is about (#174).

An in-process :class:`contextvars.ContextVar` holding the session slot that
the dispatcher already resolved, plus the account identifier during a login.
The CLI runtime, ``GraftpunkClient`` and the login command set it around the
code they invoke; author-facing entry points (``SitePlugin.get_session``,
``cache_login_session``) read it instead of reading state off the plugin
instance, so nothing has to mutate a shared object for the length of a flow.

This is NOT the ambient pin. ``session_context`` reads ``GRAFTPUNK_SESSION``
and ``.gp-session`` from the environment, which the library path deliberately
ignores (#181). The scope is in-process state written by the dispatcher that
already applied the whole precedence chain, so the library and the CLI honour
it alike.

The module imports only :mod:`graftpunk.session_identity`, so ``cache.py``,
``cli_plugin.py`` and the CLI can all import it without an import cycle.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from graftpunk.session_identity import split_session_name, validate_session_name

__all__ = [
    "OperatingSession",
    "current_operating_session",
    "operating_session",
    "operating_session_for",
]


@dataclass(frozen=True)
class OperatingSession:
    """The slot one dispatch operates on, and who it belongs to.

    Attributes:
        name: The session slot, ``myshop@alice`` or the bare ``myshop``.
        identifier: The unslugified login identifier. Set by login flows
            only; ``None`` everywhere else, including every command dispatch.
    """

    name: str
    identifier: str | None = None


_CURRENT: contextvars.ContextVar[OperatingSession | None] = contextvars.ContextVar(
    "graftpunk_operating_session", default=None
)


@contextmanager
def operating_session(name: str, identifier: str | None = None) -> Iterator[OperatingSession]:
    """Set the operating session for the duration of the block.

    The prior value is restored on exit, including when the block raises, so a
    scope can never outlive the dispatch that set it. Nesting works: the inner
    scope wins while it is open, and the outer one is visible again after.

    An ``asyncio`` task started inside the block inherits the scope, because a
    task copies the current context when none is given, so a coroutine run via
    ``asyncio.run`` from inside a ``with`` block sees it.

    Args:
        name: The session slot this dispatch operates on. Validated before
            anything is set, so a scope can never hold a name the cache would
            refuse.
        identifier: The unslugified account identifier, for login flows.

    Yields:
        The :class:`OperatingSession` that is now in effect.

    Raises:
        ValueError: *name* is not a legal session name. Both dispatchers pass
            a name that already loaded, so this is a programming-error guard
            rather than a user path.
    """
    validate_session_name(name)
    scope = OperatingSession(name=name, identifier=identifier)
    token = _CURRENT.set(scope)
    try:
        yield scope
    finally:
        _CURRENT.reset(token)


def current_operating_session() -> OperatingSession | None:
    """The scope in effect, or ``None`` outside any dispatch."""
    return _CURRENT.get()


def operating_session_for(base: str) -> OperatingSession | None:
    """The scope in effect if its name's base is *base*, else ``None``.

    The base-scoped guard: a scope set for plugin A must never steer plugin
    B's ``get_session()`` or cache write. This is the same rule the ambient
    pin already applies in ``plugin_runtime`` (#176).
    """
    scope = _CURRENT.get()
    if scope is None:
        return None
    return scope if split_session_name(scope.name)[0] == base else None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_session_scope.py -q`

Expected: PASS, 12 passed (the parametrized invalid-name test contributes 5).

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`

Expected: all four green. Nothing imports the new module yet, so the rest of the suite is unaffected.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/session_scope.py tests/unit/test_session_scope.py
git commit -m "feat(session-scope): add the in-process operating session scope (#174)"
```

---

### Task 2: `get_session` follows explicit, then scope, then bare

**Files:**
- Modify: `src/graftpunk/plugins/cli_plugin.py:713` (`        """Load the graftpunk session for API calls."""`) (the `CLIPluginProtocol` declaration, lines 712 to 714), `src/graftpunk/plugins/cli_plugin.py:1162` (`        """Load the graftpunk session for API calls.`) through `src/graftpunk/plugins/cli_plugin.py:1199` (`        return load_session_for_api(self.session_name)`) (the `SitePlugin` method), and the import block at `src/graftpunk/plugins/cli_plugin.py:56` (`from graftpunk.cache import cache_session, load_session_for_api`)
- Test: `tests/unit/test_api_session_resolution.py` (append)

**Interfaces:**
- Consumes: `operating_session_for` from Task 1; `load_session_for_api(name: str, *, resolve: bool = True) -> requests.Session` (`src/graftpunk/cache.py:698` (`def load_session_for_api(name: str, *, resolve: bool = True) -> requests.Session:`)).
- Produces: `SitePlugin.get_session(self, session_name: str | None = None) -> requests.Session`, and the same signature on `CLIPluginProtocol`. Later tasks call it with no argument from inside a scope.

Precedence, one chain:

1. `session_name` given: load it. A labelled name is exact; a bare name is a base and the loader resolves it.
2. Else `operating_session_for(self.session_name)` is set: load that name with `resolve=False`. The dispatcher that set the scope already resolved it against the listing, and the name is by construction a slot that loaded moments ago, so a second listing would be waste.
3. Else the bare `self.session_name`, resolving, exactly as today.

`requires_session=False` still short-circuits to a plain `requests.Session()` before any of this, unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_api_session_resolution.py`. Add `operating_session` to the imports by inserting this line after `src/graftpunk/session_identity` import at `tests/unit/test_api_session_resolution.py:38` (`from graftpunk.session_identity import GP_ACCOUNT_ATTR, GP_SESSION_NAME_ATTR`):

```python
from graftpunk.session_scope import operating_session
```

Then append at the end of the file:

```python
def _scope_plugin():  # noqa: ANN202
    """A plugin whose base session name is ``myshop``, with no session of its own."""

    class MyPlugin(SitePlugin):
        site_name = "myshop"
        session_name = "myshop"
        help_text = "Test plugin"

    return MyPlugin()


def test_get_session_explicit_labelled_name_loads_that_slot(fresh_backend) -> None:  # noqa: ANN001
    """An explicit name is the top of the chain, and a labelled one is exact."""
    cache_session(_cached_session("alice@example.com"), "myshop@alice")
    cache_session(_cached_session("bob@example.com"), "myshop@bob")

    api = _scope_plugin().get_session("myshop@bob")

    assert getattr(api, GP_ACCOUNT_ATTR) == "bob@example.com"


def test_get_session_explicit_bare_name_resolves_a_single_account(fresh_backend) -> None:  # noqa: ANN001
    cache_session(_cached_session("alice@example.com"), "myshop@alice")

    api = _scope_plugin().get_session("myshop")

    assert getattr(api, GP_ACCOUNT_ATTR) == "alice@example.com"


def test_get_session_explicit_bare_name_refuses_two_accounts(fresh_backend) -> None:  # noqa: ANN001
    cache_session(_cached_session("alice@example.com"), "myshop@alice")
    cache_session(_cached_session("bob@example.com"), "myshop@bob")

    with pytest.raises(AmbiguousSessionError) as exc:
        _scope_plugin().get_session("myshop")

    assert exc.value.candidates == ["myshop@alice", "myshop@bob"]


def test_get_session_follows_the_scope_with_two_accounts_cached(fresh_backend) -> None:  # noqa: ANN001
    """The issue's scenario: the CLI pinned myshop@bob, the handler calls get_session()."""
    cache_session(_cached_session("alice@example.com"), "myshop@alice")
    cache_session(_cached_session("bob@example.com"), "myshop@bob")

    with operating_session("myshop@bob"):
        api = _scope_plugin().get_session()

    assert getattr(api, GP_ACCOUNT_ATTR) == "bob@example.com"


def test_get_session_loads_the_scope_exact_only(fresh_backend) -> None:  # noqa: ANN001
    """The scope names a slot the dispatcher already resolved: a miss is a miss.

    With ``resolve=True`` this would have fallen back to ``myshop@alice``, so
    the raise is what proves the scope path does not resolve.
    """
    cache_session(_cached_session("alice@example.com"), "myshop@alice")

    with operating_session("myshop@bob"), pytest.raises(SessionNotFoundError):
        _scope_plugin().get_session()


def test_get_session_ignores_a_scope_for_a_foreign_base(fresh_backend) -> None:  # noqa: ANN001
    """A scope set while running another plugin must not steer this one."""
    cache_session(_cached_session("alice@example.com"), "myshop@alice")
    cache_session(_cached_session("carol@example.com"), "othersite@carol")

    with operating_session("othersite@carol"):
        api = _scope_plugin().get_session()

    assert getattr(api, GP_ACCOUNT_ATTR) == "alice@example.com"


def test_get_session_without_a_scope_still_refuses_two_accounts(fresh_backend) -> None:  # noqa: ANN001
    """Unchanged where nothing set a scope: the bare base resolves on its own terms."""
    cache_session(_cached_session("alice@example.com"), "myshop@alice")
    cache_session(_cached_session("bob@example.com"), "myshop@bob")

    with pytest.raises(AmbiguousSessionError):
        _scope_plugin().get_session()
```

The no-scope single-account case is already covered by `tests/unit/test_api_session_resolution.py:289` (`def test_site_plugin_get_session_resolves_bare_base_name(fresh_backend) -> None:`), which must keep passing unchanged.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_api_session_resolution.py -q -k "get_session"`

Expected: FAIL. `test_get_session_explicit_labelled_name_loads_that_slot` fails with `TypeError: get_session() takes 1 positional argument but 2 were given`; `test_get_session_follows_the_scope_with_two_accounts_cached` fails with `AmbiguousSessionError`.

- [ ] **Step 3: Write the implementation**

In `src/graftpunk/plugins/cli_plugin.py`, add the scope import directly below the existing cache import at `src/graftpunk/plugins/cli_plugin.py:56` (`from graftpunk.cache import cache_session, load_session_for_api`), keeping alphabetical order among the `graftpunk.*` imports (after `from graftpunk.observe import ...`):

```python
from graftpunk.session_scope import operating_session_for
```

Replace the `CLIPluginProtocol` declaration (lines 712 to 714) with:

```python
    def get_session(self, session_name: str | None = None) -> requests.Session:
        """Load the graftpunk session for API calls."""
        ...
```

Replace the whole `SitePlugin.get_session` method (lines 1161 to 1199) with:

```python
    def get_session(self, session_name: str | None = None) -> requests.Session:
        """Load the graftpunk session for API calls.

        If ``requires_session`` is False, returns a plain ``requests.Session``.

        Which slot is loaded follows one chain:

        1. *session_name*, when given. A labelled name (``myshop@alice``) is
           exact; a bare name is a BASE and the loader resolves it (the pin
           contract). This is the channel for callers outside a dispatch:
           scripts, tests, and library code that already holds a name.
        2. Otherwise the operating session scope, when one is set for this
           plugin's base name. Both dispatchers set it around the handler with
           the name they resolved and loaded, so ``self.get_session()`` inside
           a command handler agrees with ``--session`` (and with
           ``GraftpunkClient(session=...)``) instead of resolving the bare base
           on its own terms. The scope loads exact-only: the dispatcher already
           resolved that name against the listing.
        3. Otherwise the bare ``self.session_name``, resolving.

        A scope set for another plugin's base is ignored, so one plugin's
        dispatch never steers another's load.

        This returns a SECOND ``requests.Session``, not ``ctx.session``. What
        changed in #174 is that it is now the same account.

        The session this returns may be ``base@label`` while
        ``self.session_name`` is still bare. Persisting with
        ``update_session_cookies(session, self.session_name)`` lands on the
        slot that was loaded: the session carries that slot name in memory,
        and a write-back whose argument is the slot's bare base follows it.
        ``ctx.save_session()`` from a command handler, and the operating name
        the CLI resolved (``load_session_for_api_resolved`` returns it), both
        remain exact and say the target at the call site.

        Args:
            session_name: An explicit slot or base name, overriding the scope
                and the plugin's base name.

        Raises:
            SessionNotFoundError: Nothing is cached under the name that was
                loaded. On the scope path a miss is a miss, since that load is
                exact-only.
            AmbiguousSessionError: The bare path (an explicit bare name, or no
                scope) found several cached sessions sharing the base and
                nothing selected one; the error names every candidate.
            ValueError: *session_name* is not a legal session name.
        """
        if not self.requires_session:
            return requests.Session()
        if session_name is not None:
            return load_session_for_api(session_name)
        scope = operating_session_for(self.session_name)
        if scope is not None:
            return load_session_for_api(scope.name, resolve=False)
        return load_session_for_api(self.session_name)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_api_session_resolution.py -q`

Expected: PASS, every test in the file including the pre-existing ones.

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`

Expected: all four green.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/plugins/cli_plugin.py tests/unit/test_api_session_resolution.py
git commit -m "feat(plugins): get_session follows explicit, then scope, then bare (#174)"
```

---

### Task 3: both dispatchers set the scope around the handler

**Files:**
- Modify: `src/graftpunk/cli/plugin_runtime.py:223` (`            result = execute_plugin_command(`) through `src/graftpunk/cli/plugin_runtime.py:259` (`                raise`), plus its import block
- Modify: `src/graftpunk/client.py:497` (`        try:`) through `src/graftpunk/client.py:519` (`                raise`), plus its import block
- Test: `tests/unit/test_multi_account_e2e.py` (append)

**Interfaces:**
- Consumes: `operating_session` from Task 1; `SitePlugin.get_session()` from Task 2; `operating_name` (already a local in both dispatchers) and `needs_session` (already a local in both).
- Produces: no new public names. The observable contract is that a handler running under a session-requiring command sees `current_operating_session().name == <the slot that was loaded>`, and a handler under `requires_session=False` sees `None`.

One `with` per dispatcher covers both the call and its 403-refresh retry, so it is one context manager, not two.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_multi_account_e2e.py`:

```python
def _cache_loadable(name: str, identifier: str) -> None:
    """Cache a session whose account identifier survives the pickle round-trip.

    A plain ``requests.Session`` pickles through its ``__attrs__`` whitelist,
    which drops the rider attributes, so the LOADED session would carry no
    identifier and the assertions below could not tell the accounts apart.
    ``BrowserSession`` round-trips them.
    """
    from graftpunk.session import BrowserSession
    from graftpunk.session_identity import GP_ACCOUNT_ATTR

    stored = BrowserSession.__new__(BrowserSession)
    requests.Session.__init__(stored)
    stored._backend_type = "nodriver"
    setattr(stored, GP_ACCOUNT_ATTR, identifier)
    cache_session(stored, name)


def _whoami_plugin(seen: dict):  # noqa: ANN001, ANN202
    """A plugin whose handler calls ``self.get_session()`` and records the account."""
    from graftpunk.plugins.cli_plugin import SitePlugin, command
    from graftpunk.session_identity import GP_ACCOUNT_ATTR

    class ShopPlugin(SitePlugin):
        site_name = "fmtsite"
        base_url = "https://fmt.example.com"
        session_name = "myshop"
        requires_session = True

        @command(help="Report the account get_session loads")
        def whoami(self, ctx):  # noqa: ANN001, ANN202
            api = self.get_session()
            seen["account"] = getattr(api, GP_ACCOUNT_ATTR, None)
            return {"ok": True}

    return ShopPlugin()


class TestGetSessionFollowsTheOperatingScope:
    """#174 end to end: the author-facing load agrees with the dispatcher's pin."""

    def test_cli_pin_steers_get_session_inside_the_handler(self, fresh_backend) -> None:  # noqa: ANN001
        from tests.unit.cli_harness import invoke_plugin_app

        _cache_loadable("myshop@alice", "alice@example.com")
        _cache_loadable("myshop@bob", "bob@example.com")

        seen: dict = {}
        result = invoke_plugin_app(
            _whoami_plugin(seen), ["fmtsite", "whoami", "--session", "myshop@bob"]
        )

        assert result.exit_code == 0, result.output
        assert seen["account"] == "bob@example.com"

    def test_client_pin_steers_get_session_inside_the_handler(self, fresh_backend) -> None:  # noqa: ANN001
        from unittest.mock import patch

        from graftpunk.client import GraftpunkClient

        _cache_loadable("myshop@alice", "alice@example.com")
        _cache_loadable("myshop@bob", "bob@example.com")

        seen: dict = {}
        plugin = _whoami_plugin(seen)
        with (
            patch("graftpunk.client.get_plugin", return_value=plugin),
            GraftpunkClient("fmtsite", session="myshop@bob") as client,
        ):
            client.execute("whoami")

        assert seen["account"] == "bob@example.com"

    def test_a_sessionless_command_sets_no_scope(self, fresh_backend) -> None:  # noqa: ANN001
        """There is no account for a requires_session=False command to be about."""
        from graftpunk.plugins.cli_plugin import SitePlugin, command
        from graftpunk.session_scope import current_operating_session
        from tests.unit.cli_harness import invoke_plugin_app

        seen: dict = {}

        class FreePlugin(SitePlugin):
            site_name = "fmtsite"
            base_url = "https://fmt.example.com"
            session_name = "myshop"
            requires_session = False

            @command(help="No session needed")
            def ping(self, ctx):  # noqa: ANN001, ANN202
                seen["scope"] = current_operating_session()
                return {"ok": True}

        result = invoke_plugin_app(FreePlugin(), ["fmtsite", "ping"])

        assert result.exit_code == 0, result.output
        assert seen["scope"] is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_multi_account_e2e.py -q -k "OperatingScope"`

Expected: FAIL. Both pin tests fail on `assert seen["account"] == "bob@example.com"` with `AmbiguousSessionError` surfacing from the handler (the CLI case exits non-zero, the client case raises), because nothing set a scope and `get_session()` resolved `myshop` on its own terms. `test_a_sessionless_command_sets_no_scope` passes already and is a regression guard.

- [ ] **Step 3: Write the implementation**

In `src/graftpunk/cli/plugin_runtime.py`, change the stdlib import block. Replace the line `from typing import Any, Literal` with:

```python
from contextlib import AbstractContextManager, nullcontext
from typing import Any, Literal
```

and add, directly after `src/graftpunk/cli/plugin_runtime.py:37` (`from graftpunk.session_identity import compute_operating_session_name, split_session_name`):

```python
from graftpunk.session_scope import operating_session
```

Then replace the inner execute block (lines 222 to 259, from the inner `try:` up to and including the `raise` that ends the `else:` branch) with:

```python
        # The operating session scope: author-facing code inside the handler
        # (``self.get_session()``) resolves to the slot this invocation already
        # loaded, instead of resolving the bare base on its own terms (#174).
        # One ``with`` covers the call and its 403-refresh retry. A
        # requires_session=False command sets no scope: there is no account for
        # it to be about.
        scope: AbstractContextManager[object] = (
            operating_session(operating_name) if needs_session else nullcontext()
        )
        with scope:
            try:
                result = execute_plugin_command(
                    cmd_spec,
                    cmd_ctx,
                    plugin_formatters=getattr(plugin, "format_overrides", None) or None,
                    **kwargs,
                )
            except requests.exceptions.HTTPError as exc:
                if (
                    exc.response is not None
                    and exc.response.status_code == 403
                    and token_config is not None
                ):
                    from graftpunk.tokens import clear_cached_tokens
                    from graftpunk.tokens import (
                        prepare_session as _prep,
                    )

                    LOG.info(
                        "token_403_retry",
                        command=cmd_spec.name,
                        url=(exc.response.url if exc.response else "unknown"),
                    )
                    clear_cached_tokens(session)
                    _prep(
                        session,
                        token_config,
                        getattr(plugin, "base_url", ""),
                    )
                    cmd_ctx._session_dirty = True
                    result = execute_plugin_command(
                        cmd_spec,
                        cmd_ctx,
                        plugin_formatters=getattr(plugin, "format_overrides", None) or None,
                        **kwargs,
                    )
                else:
                    raise
```

The `update_session_cookies` and `format_output` calls that follow stay at their current indentation, outside the `with`: the write-back keys off `operating_name` explicitly and needs no scope.

In `src/graftpunk/client.py`, add to the stdlib imports, after `import asyncio`:

```python
from contextlib import AbstractContextManager, nullcontext
```

(keeping `import threading` and `import time` in place, with the `from` import following the plain ones as ruff's isort orders them), and add directly below the closing paren of the `graftpunk.session_identity` import block at `src/graftpunk/client.py:48` (the `)` closing the block that starts at `src/graftpunk/client.py:44` (`from graftpunk.session_identity import (`)):

```python
from graftpunk.session_scope import operating_session
```

Then replace the step-4 block (lines 496 to 519, from `        # 4. Execute with retry/rate-limit; 403 token refresh` down to and including the `                raise` that ends the `else:` branch) with:

```python
        # 4. Execute with retry/rate-limit; 403 token refresh.
        # The operating session scope, so ``self.get_session()`` inside a
        # handler loads the slot this client resolved rather than resolving
        # the bare base itself (#174). One ``with`` covers the call and its
        # retry; a requires_session=False command sets no scope.
        scope: AbstractContextManager[object] = (
            operating_session(operating_name) if needs_session else nullcontext()
        )
        with scope:
            try:
                result = _run_handler_with_limits(
                    spec.handler, ctx, spec, self._last_execution, **kwargs
                )
            except requests.exceptions.HTTPError as exc:
                if (
                    exc.response is not None
                    and exc.response.status_code == 403
                    and token_config is not None
                ):
                    LOG.info(
                        "token_403_retry",
                        command=spec.name,
                        url=(exc.response.url if exc.response else "unknown"),
                    )
                    clear_cached_tokens(session)
                    prepare_session(session, token_config, base_url)
                    self._session_dirty = True
                    result = _run_handler_with_limits(
                        spec.handler, ctx, spec, self._last_execution, **kwargs
                    )
                else:
                    raise
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_multi_account_e2e.py tests/unit/test_plugin_runtime_session.py tests/unit/test_client.py -q`

Expected: PASS, including the pre-existing runtime and client tests.

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`

Expected: all four green.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/cli/plugin_runtime.py src/graftpunk/client.py tests/unit/test_multi_account_e2e.py
git commit -m "feat(runtime): set the operating session scope around command handlers (#174)"
```

---

### Task 4: retire the login identity stamp

**Files:**
- Modify: `src/graftpunk/cli/login_commands.py:156` (`@contextmanager`) through `src/graftpunk/cli/login_commands.py:210` (`            setattr(plugin, GP_ACCOUNT_ATTR, prior_attr)`) (delete `_stamp_login_identity`), `src/graftpunk/cli/login_commands.py:45` (`_MISSING = object()`) and the comment above it (delete), `src/graftpunk/cli/login_commands.py:30` (`    GP_ACCOUNT_ATTR,`) (delete), `src/graftpunk/cli/login_commands.py:334` (`                _stamp_login_identity(plugin, label, identifier),`) (replace), and the two stdlib imports that only the stamp used
- Modify: `src/graftpunk/plugins/cli_plugin.py:1112` (`            # No explicit name/identifier: the instance state (the login`), `src/graftpunk/plugins/cli_plugin.py:1153` (`            # Instance fallback by contract -- see browser_session() above.`), and `src/graftpunk/plugins/cli_plugin.py:1221` (`    session_name = name or plugin.session_name`) through `src/graftpunk/plugins/cli_plugin.py:1222` (`    account = identifier if identifier is not None else getattr(plugin, GP_ACCOUNT_ATTR, None)`)
- Test: `tests/unit/test_login_identity.py` (delete `TestStamp`, rewrite the funnel fallback test and the hand-written login helper and tests, add three tests)

**Interfaces:**
- Consumes: `operating_session` and `operating_session_for` from Task 1. `make_login_body` already computes `target_name` and `identifier` before the call (`src/graftpunk/cli/login_commands.py:307` (`        target_name = (`)), so nothing new has to be derived.
- Produces: `cache_login_session(plugin, session, *, name=None, identifier=None) -> str` with an unchanged signature and an unchanged "explicit first" rule; only its fallback source changes. Nothing writes `GP_ACCOUNT_ATTR` onto a plugin instance any more.

**Behaviour change to carry into the docs (Task 5):** during `gp <site> login --as bob`, `self.session_name` read inside a hand-written `login()` is now `myshop`, the class attribute, always. A login that caches through the helpers or through `cache_login_session(self, session)` is unaffected. One that calls `cache_session(session, self.session_name)` by hand now lands on the bare slot. The generated flows are untouched: they receive `session_name` and `account_identifier` explicitly (`src/graftpunk/plugins/login_engine.py:645` (`def _generate_nodriver_login`), `src/graftpunk/plugins/login_engine.py:869` (`def _generate_selenium_login`)).

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_login_identity.py`:

1. Replace the module docstring (line 1) with:

```python
"""Login derives, scopes, funnels and warns about account identity (#151, #174)."""
```

2. Delete the whole `TestStamp` class (`tests/unit/test_login_identity.py:15` (`class TestStamp:`) through line 98, ending at `            assert plugin.session_name == "myshop"`), leaving `class TestFunnel:` as the first class in the file.

3. Replace `test_instance_fallback_for_author_facing_paths` (`tests/unit/test_login_identity.py:121` (`    def test_instance_fallback_for_author_facing_paths(self) -> None:`) through the end of that method, line 135) with these two tests, which use the real cache rather than a patched seam:

```python
    def test_scope_fallback_for_author_facing_paths(self, fresh_backend) -> None:  # noqa: ANN001
        """browser_session callers pass nothing; the operating scope is the input."""
        from graftpunk.cache import get_session_metadata, list_sessions
        from graftpunk.plugins.cli_plugin import SitePlugin, cache_login_session
        from graftpunk.session_scope import operating_session

        class P(SitePlugin):
            site_name = "fmtsite"
            base_url = "https://fmt.example.com"
            session_name = "myshop"

        plugin = P()
        session = requests.Session()
        with operating_session("myshop@alice", "alice@example.com"):
            used = cache_login_session(plugin, session)

        assert used == "myshop@alice"
        assert getattr(session, GP_ACCOUNT_ATTR) == "alice@example.com"
        assert list_sessions() == ["myshop@alice"]
        assert get_session_metadata("myshop@alice")["account_identifier"] == "alice@example.com"

    def test_a_scope_for_another_base_does_not_steer_the_funnel(self, fresh_backend) -> None:  # noqa: ANN001
        """The base-scoped guard: another plugin's login must not rename this cache write."""
        from graftpunk.cache import list_sessions
        from graftpunk.plugins.cli_plugin import SitePlugin, cache_login_session
        from graftpunk.session_scope import operating_session

        class P(SitePlugin):
            site_name = "fmtsite"
            base_url = "https://fmt.example.com"
            session_name = "myshop"

        plugin = P()
        with operating_session("othersite@bob", "bob@example.com"):
            used = cache_login_session(plugin, requests.Session())

        assert used == "myshop"
        assert list_sessions() == ["myshop"]
```

4. Replace `_hand_written_login_plugin` (`tests/unit/test_login_identity.py:307` (`def _hand_written_login_plugin(seen: dict | None = None):  # noqa: ANN001, ANN202`) through line 324) with a version that records the base name it sees and caches through the funnel, so the tests can assert what the cache actually received:

```python
def _hand_written_login_plugin(seen: dict | None = None):  # noqa: ANN001, ANN202
    """A plugin whose ``login(credentials)`` records, caches through the funnel, succeeds."""
    from graftpunk.plugins.cli_plugin import SitePlugin, cache_login_session

    class ShopPlugin(SitePlugin):
        site_name = "fmtsite"
        base_url = "https://fmt.example.com"
        session_name = "myshop"

        def login(self, credentials: dict[str, str]) -> bool:
            """Sign in to fmtsite."""
            if seen is not None:
                seen["credentials"] = dict(credentials)
                # The class attribute, always: nothing mutates the instance
                # for the length of a login flow any more (#174).
                seen["session_name"] = self.session_name
            cache_login_session(self, requests.Session())
            return True

    return ShopPlugin()


def _raising_login_plugin():  # noqa: ANN202
    """A plugin whose ``login(credentials)`` raises."""
    from graftpunk.plugins.cli_plugin import SitePlugin

    class ShopPlugin(SitePlugin):
        site_name = "fmtsite"
        base_url = "https://fmt.example.com"
        session_name = "myshop"

        def login(self, credentials: dict[str, str]) -> bool:
            """Sign in to fmtsite."""
            raise RuntimeError("the site was down")

    return ShopPlugin()


def _browser_login_plugin():  # noqa: ANN202
    """A hand-written login that caches through ``browser_session_sync()``."""
    from graftpunk.plugins.cli_plugin import SitePlugin

    class ShopPlugin(SitePlugin):
        site_name = "fmtsite"
        base_url = "https://fmt.example.com"
        session_name = "myshop"
        backend = "selenium"

        def login(self, credentials: dict[str, str]) -> bool:
            """Sign in to fmtsite."""
            with self.browser_session_sync() as (session, driver):
                driver.get(f"{self.base_url}/login")
            return True

    return ShopPlugin()
```

5. Replace the three `TestLoginWrapperOrchestration` tests that reach the login body and assert on the stamp, and append three new ones. `test_derived_label_names_the_cached_session` (`tests/unit/test_login_identity.py:349` (`    def test_derived_label_names_the_cached_session(self) -> None:`)), `test_as_flag_overrides_the_label`, and `test_advisory_failure_never_fails_a_successful_login` become:

```python
    def test_derived_label_names_the_cached_session(self) -> None:
        seen: dict = {}
        with patch("graftpunk.plugins.cli_plugin.cache_session") as mock_cache:
            result = _invoke(_hand_written_login_plugin(seen), ["fmtsite", "login"], **_CREDS)
        assert result.exit_code == 0, result.output
        assert "Session name: myshop@alice" in result.output
        # The bare base name, inside the plugin's own login(): the stamp is gone.
        assert seen["session_name"] == "myshop"
        assert seen["credentials"] == {"username": "alice", "password": "x"}
        # What the scope carried is what the cache received.
        cached_session, cached_name = mock_cache.call_args[0]
        assert cached_name == "myshop@alice"
        assert getattr(cached_session, GP_ACCOUNT_ATTR) == "alice"

    def test_as_flag_overrides_the_label(self) -> None:
        seen: dict = {}
        with patch("graftpunk.plugins.cli_plugin.cache_session") as mock_cache:
            result = _invoke(
                _hand_written_login_plugin(seen), ["fmtsite", "login", "--as", "work"], **_CREDS
            )
        assert result.exit_code == 0, result.output
        assert "Session name: myshop@work" in result.output
        assert seen["session_name"] == "myshop"
        assert mock_cache.call_args[0][1] == "myshop@work"

    def test_advisory_failure_never_fails_a_successful_login(self) -> None:
        """The verdict is sealed before the advisory block: a raise there is a hint lost."""
        with (
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch(
                "graftpunk.cli.login_commands.get_active_session",
                side_effect=FileNotFoundError("cwd was removed"),
            ),
        ):
            result = _invoke(_hand_written_login_plugin(), ["fmtsite", "login"], **_CREDS)
        assert result.exit_code == 0, result.output
        assert "Logged in to fmtsite (session cached)" in result.output
        assert "Login failed" not in result.output
```

The other two tests in that class stay exactly as they are. `test_bad_as_label_names_the_label_the_user_typed` never reaches the login callable, and `test_slot_recorded_for_another_account_warns_after_success` runs under `fresh_backend`, so the helper's new real cache write lands in that test's own temporary backend and the warning it asserts is still computed from the metadata read before the attempt.

Append to the same class:

```python
    def test_browser_session_sync_login_lands_on_the_scoped_slot(self) -> None:
        """A hand-written login caching through the helper reaches myshop@bob (#174)."""
        from unittest.mock import MagicMock

        with (
            patch("graftpunk.BrowserSession") as mock_bs,
            patch("graftpunk.plugins.cli_plugin.cache_session") as mock_cache,
        ):
            instance = mock_bs.return_value
            instance.driver = MagicMock()
            instance.transfer_driver_cookies_to_session = MagicMock()
            instance.quit = MagicMock()
            result = _invoke(
                _browser_login_plugin(),
                ["fmtsite", "login", "--as", "bob"],
                FMTSITE_USERNAME="bob@example.com",
                FMTSITE_PASSWORD="x",
            )

        assert result.exit_code == 0, result.output
        cached_session, cached_name = mock_cache.call_args[0]
        assert cached_name == "myshop@bob"
        assert getattr(cached_session, GP_ACCOUNT_ATTR) == "bob@example.com"

    def test_a_failed_login_leaves_no_scope_behind(self) -> None:
        from graftpunk.session_scope import current_operating_session

        result = _invoke(_raising_login_plugin(), ["fmtsite", "login"], **_CREDS)

        assert result.exit_code != 0
        assert "Login failed: the site was down" in result.output
        assert current_operating_session() is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_login_identity.py -q`

Expected: FAIL. `test_scope_fallback_for_author_facing_paths` fails on `assert used == "myshop@alice"` (the funnel still reads the instance and returns `"myshop"`); `test_derived_label_names_the_cached_session` fails on `assert seen["session_name"] == "myshop"` (the stamp still overwrites it with `"myshop@alice"`); `test_browser_session_sync_login_lands_on_the_scoped_slot` passes today via the stamp and must keep passing after the change.

- [ ] **Step 3: Write the implementation**

In `src/graftpunk/cli/login_commands.py`:

1. Replace the stdlib import lines 11 and 12 (`from collections.abc import Callable, Iterator` and `from contextlib import contextmanager`) with just:

```python
from collections.abc import Callable
```

Only `_stamp_login_identity` used `Iterator` and `contextmanager`; `Callable` is still used by `login_method`, `_accepted_login_kwargs` and `make_login_body`.

2. Replace the `graftpunk.session_identity` import block (lines 29 to 34) with:

```python
from graftpunk.session_identity import (
    derive_account_identity,
    join_session_name,
    validate_account_label,
)
from graftpunk.session_scope import operating_session
```

3. Delete lines 43 to 45, the `_MISSING` sentinel and its two-line comment:

```python
# "attribute absent" sentinel for the login stamp's save/restore (None is a
# legitimate stored value, so it cannot mark absence).
_MISSING = object()
```

4. Delete the whole `_stamp_login_identity` function, from its `@contextmanager` decorator at line 156 through line 210 (`            setattr(plugin, GP_ACCOUNT_ATTR, prior_attr)`), leaving `_warn_if_slot_changes_hands_post` as the next definition.

5. Replace `src/graftpunk/cli/login_commands.py:334` (`                _stamp_login_identity(plugin, label, identifier),`) with:

```python
                operating_session(target_name, identifier),
```

`target_name` and `identifier` are both already in scope, computed a few lines above at line 305 onward.

In `src/graftpunk/plugins/cli_plugin.py`:

6. Replace the two-line comment at lines 1112 to 1113 inside `browser_session`:

```python
            # No explicit name/identifier: the operating session scope, set by
            # the login command around the login callable, IS this path's input.
```

7. Replace the comment at line 1153 inside `browser_session_sync`:

```python
            # Scope fallback by contract -- see browser_session() above.
```

8. Replace the body and docstring of `cache_login_session` (`src/graftpunk/plugins/cli_plugin.py:1202` (`def cache_login_session(`) onward) with:

```python
def cache_login_session(
    plugin: SitePlugin,
    session: Any,
    *,
    name: str | None = None,
    identifier: str | None = None,
) -> str:
    """The one login-flow cache funnel. Explicit arguments first.

    Callers that hold the operating name and identifier pass them explicitly:
    the generated login flows receive both from ``make_login_body``. The
    fallback reads the operating session scope, which the login command sets
    around the login callable, so the author-facing paths (a hand-written
    ``login()`` caching through ``browser_session`` or ``browser_session_sync``,
    or calling this directly) land on the account-qualified slot without
    anything mutating the plugin instance (#174). The scope is base-scoped, so
    a scope set for another plugin is ignored and the plugin's own base name is
    used. The identifier rides the session object so
    ``_extract_session_metadata`` records it. Returns the session name used.
    """
    from graftpunk.session_identity import GP_ACCOUNT_ATTR

    scope = operating_session_for(plugin.session_name)
    session_name = name or (scope.name if scope else plugin.session_name)
    account = identifier if identifier is not None else (scope.identifier if scope else None)
    if account is not None:
        setattr(session, GP_ACCOUNT_ATTR, account)
    cache_session(session, session_name)
    return session_name
```

`operating_session_for` is already imported at module scope by Task 2.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_login_identity.py tests/unit/test_plugin_commands.py -q`

Expected: PASS, including the generated-flow tests (`test_nodriver_flow_caches_under_the_threaded_name`, `test_selenium_flow_caches_under_the_threaded_name`), which pass `session_name` and `account_identifier` explicitly and are unaffected, and the `TestBrowserSessionContextManager` tests in `test_plugin_commands.py`.

Also confirm no reference to the deleted names survives:

Run: `grep -rn "_stamp_login_identity" src/ tests/ ; grep -n "_MISSING" src/graftpunk/cli/login_commands.py`

Expected: no output from either. (`_MISSING` is also the name of an unrelated local constant in `tests/unit/test_login_field_fill.py`, so scope the second grep to `login_commands.py` as written.)

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`

Expected: all four green. Ruff would flag `Iterator`, `contextmanager`, or `GP_ACCOUNT_ATTR` as unused imports if step 3's import edits were missed.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/cli/login_commands.py src/graftpunk/plugins/cli_plugin.py tests/unit/test_login_identity.py
git commit -m "refactor(login): retire the identity stamp for the operating session scope (#174)"
```

---

### Task 5: exports, author docs, and changelog

**Files:**
- Modify: `src/graftpunk/plugins/__init__.py:26` (`from graftpunk.plugins.cli_plugin import (`) block and `src/graftpunk/plugins/__init__.py:73` (`    "SitePlugin",`) in `__all__`
- Modify: `docs/HOW_IT_WORKS.md:146` (`slot (or vanish into one that does not exist).`) and `docs/HOW_IT_WORKS.md:598` (`The `browser_session()` and `browser_session_sync()` context managers handle browser lifecycle, cookie transfer, and session caching automatically.`)
- Modify: `CHANGELOG.md:8` (`## [Unreleased]`)

**Interfaces:**
- Consumes: `cache_login_session` (Task 4's final form) and `operating_session` (Task 1).
- Produces: `from graftpunk.plugins import cache_login_session, operating_session` works. Nothing else depends on this task.

**Export audit at HEAD.** `SitePlugin` is exported from `src/graftpunk/plugins/__init__.py` only (imported at line 38, listed in `__all__` at line 73). The top-level `src/graftpunk/__init__.py` does **not** export `SitePlugin` at all: its `__all__` (`src/graftpunk/__init__.py:138` (`__all__ = [`)) covers the client, session, cache, storage, config and exception surfaces, and no plugin base class. `cache_login_session` is public in `cli_plugin.py` but is exported from neither package `__init__`, so the spec's parenthetical "public, exported alongside `SitePlugin`" is aspirational at HEAD and this task makes it true. Only `src/graftpunk/plugins/__init__.py` changes; the top-level `__init__.py` is left alone, since adding a plugin-authoring name there would be a new surface the spec does not ask for.

- [ ] **Step 1: Add the exports**

In `src/graftpunk/plugins/__init__.py`, add `cache_login_session` to the `graftpunk.plugins.cli_plugin` import block, in alphabetical position after `build_plugin_config`:

```python
from graftpunk.plugins.cli_plugin import (
    SUPPORTED_API_VERSIONS,
    CLIPluginProtocol,
    CommandContext,
    CommandGroupMeta,
    CommandMetadata,
    CommandResult,
    CommandSpec,
    LoginConfig,
    LoginStep,
    PluginConfig,
    PluginParamSpec,
    SitePlugin,
    build_plugin_config,
    cache_login_session,
    command,
)
```

Add the scope import directly after the `graftpunk.plugins.yaml_plugin` import (`src/graftpunk/plugins/__init__.py:68` (`from graftpunk.plugins.yaml_plugin import create_yaml_plugins`)):

```python
from graftpunk.session_scope import operating_session
```

Extend `__all__`'s first group so the two names sit beside `SitePlugin`:

```python
__all__ = [
    # Base classes and decorators
    "SitePlugin",
    "cache_login_session",
    "command",
    "operating_session",
    "CLIPluginProtocol",
```

- [ ] **Step 2: Verify the exports resolve**

Run: `NO_COLOR=1 FORCE_COLOR= uv run python -c "from graftpunk.plugins import SitePlugin, cache_login_session, operating_session; print(cache_login_session.__name__, operating_session.__name__)"`

Expected: `cache_login_session operating_session`

- [ ] **Step 3: Update `docs/HOW_IT_WORKS.md`**

In the "Multi-Account Sessions" section, insert this paragraph immediately after the paragraph that ends `slot (or vanish into one that does not exist).` (`docs/HOW_IT_WORKS.md:146`), separated by a blank line on each side:

```markdown
Inside a plugin command handler, `self.get_session()` follows the resolution
the CLI (or `GraftpunkClient`) already performed for that invocation, so it
loads the slot the command is pinned to rather than resolving the plugin's bare
base name a second time. It still returns a second `requests.Session`, not
`ctx.session`; what it no longer does is disagree with the pin. Outside a
command (a script, a test, library code that already holds a name), pass the
name: `plugin.get_session("myshop@alice")` loads that slot exactly, and
`plugin.get_session("myshop")` resolves the base.
```

In "### 2. Custom Login Method (Python)", insert this paragraph immediately after the line at `docs/HOW_IT_WORKS.md:598` (`The `browser_session()` and `browser_session_sync()` context managers handle browser lifecycle, cookie transfer, and session caching automatically.`), separated by a blank line on each side:

```markdown
**Where the account-qualified name comes from.** During `gp mysite login`, the
CLI derives the account label from the login identifier (or takes it from
`--as`) and sets the operating session scope around your `login()` call. Two
ways of caching pick that name up for you: the `browser_session()` and
`browser_session_sync()` helpers, which cache on their success path, and
`cache_login_session(self, session)` (exported from `graftpunk.plugins`) when
you build the session yourself. Inside `login()`, `self.session_name` is the
plugin's bare base name (`mysite`), never the account-qualified slot, because
nothing mutates the plugin instance. A `login()` that calls
`cache_session(session, self.session_name)` by hand therefore writes the bare
slot: switch it to `cache_login_session(self, session)`, or declare
`session_name` and `account_identifier` as keyword arguments on `login()` and
the CLI passes both in.
```

- [ ] **Step 4: Update `CHANGELOG.md`**

Under `## [Unreleased]`, add an `### Added` section directly above the existing `### Changed`, and add one bullet to the top of `### Changed`:

```markdown
### Added

- **The operating session scope** ([#174](https://github.com/stavxyz/graftpunk/issues/174)). A new `graftpunk.session_scope` module holds the session slot one dispatch is about in a `contextvars.ContextVar`: `operating_session(name, identifier=None)` sets it for the duration of a block, `current_operating_session()` reads it, and `operating_session_for(base)` reads it only when the slot belongs to that base. The CLI runtime and `GraftpunkClient` set it around every command handler with the name they already resolved and loaded, and the login command sets it around the login callable. `operating_session` and `cache_login_session` are now exported from `graftpunk.plugins`.
- **`SitePlugin.get_session(session_name=None)`** ([#174](https://github.com/stavxyz/graftpunk/issues/174)). `get_session()` now follows one chain: an explicit `session_name`, then the operating session scope, then the plugin's bare base name. Called from a command handler it loads the slot the invocation is pinned to, so `self.get_session()` under `--session myshop@bob` returns bob's session where it used to resolve `myshop` on its own terms and raise `AmbiguousSessionError` with two accounts cached. An explicit name wins over both tiers: a labelled name loads exactly, a bare name resolves. Outside a command, nothing sets a scope and the behaviour is unchanged.

### Changed

- **The login identity stamp is retired, and a hand-written `login()` now reads the bare base name** ([#174](https://github.com/stavxyz/graftpunk/issues/174)). `gp <site> login --as bob` used to temporarily overwrite `plugin.session_name` and an account attribute on the plugin instance for the length of one login flow. It now sets the operating session scope instead and never mutates the instance. **Behaviour change:** inside your own `login()`, `self.session_name` always reads the class attribute (`myshop`), where during a login it used to read `myshop@bob`. A `login()` that caches through `browser_session()`, `browser_session_sync()`, or `cache_login_session(self, session)` is unaffected and still lands on `myshop@bob` with the identifier recorded. A `login()` that calls `cache_session(session, self.session_name)` by hand now writes the bare `myshop` slot. The migration is one line: call `cache_login_session(self, session)` (exported from `graftpunk.plugins`), or declare `session_name` and `account_identifier` keyword arguments on `login()`, which the CLI already passes to any login that accepts them. Generated declarative logins are unaffected: they receive both values explicitly.
```

The existing `_operating_session_name` rename bullet stays as the second entry under `### Changed`.

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`

Expected: all four green.

Then check the lines this task added for the punctuation rule, which looks only at added lines and so cannot trip over the pre-existing dashes elsewhere in those files:

Run: `git diff -U0 -- CHANGELOG.md docs/HOW_IT_WORKS.md | grep "^+[^+]" | grep "[—–]"`

Expected: no output. Pre-existing lines in those files are out of scope and must not be reflowed.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/plugins/__init__.py docs/HOW_IT_WORKS.md CHANGELOG.md
git commit -m "docs(sessions): document the operating session scope and export the funnel (#174)"
```

---

## Self-review notes

**Spec coverage.** Every section of the spec maps to a task: the new module and its propagation proof to Task 1; half 1 (`get_session` precedence, the `CLIPluginProtocol` declaration, the docstring) to Task 2; the two dispatchers and the end-to-end tests to Task 3; half 2 (the stamp's deletion, the funnel's fallback, the `browser_session` comments, the test surgery) to Task 4; the documentation and changelog section to Task 5. The spec's "What does not change" list is honoured: no task touches `CommandContext`, `load_session_for_api_resolved`, `update_session_cookies`, the rider registry, the pin contract, or the client's construction-time validation.

**Signature consistency.** `operating_session(name, identifier=None)`, `current_operating_session()`, and `operating_session_for(base)` are defined once in Task 1 and used with those exact spellings in Tasks 2, 3, 4, and 5. `get_session(self, session_name: str | None = None)` is defined in Task 2 and called with zero or one positional argument thereafter. `cache_login_session(plugin, session, *, name=None, identifier=None) -> str` keeps its HEAD signature.

**Verified against HEAD.** Every line citation in this plan was read at the commit this branch is on, and every line number is therefore HEAD-relative. Earlier tasks shift the numbers in the files they touch (Task 2 adds one import line to `cli_plugin.py` and rewrites `get_session`, so every Task 4 citation below line 56 of that file moves down), so locate each edit by the quoted anchor text, which is authoritative, rather than by the number. Two further notes for the executor:

- The spec says "`GraftpunkClient.execute`, where its `CommandContext` is built (`src/graftpunk/client.py:493`)". Line 493 is correct, but the method is `GraftpunkClient._execute_command` (`src/graftpunk/client.py:382` (`    def _execute_command(self, spec: CommandSpec, **kwargs: Any) -> CommandResult:`)); `execute` (`src/graftpunk/client.py:331` (`    def execute(self, *args: str, **kwargs: Any) -> CommandResult:`)) only resolves the command and delegates. Task 3 edits `_execute_command`.
- The spec calls `cache_login_session` "public, exported alongside `SitePlugin`". At HEAD it is exported from neither package `__init__`. Task 5 adds it, along with `operating_session`, to `src/graftpunk/plugins/__init__.py`, which is the only place `SitePlugin` is exported from.
