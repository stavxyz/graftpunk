---
type: plan
validated:
  sha: 54eac3244f3d4a3a493e3c23e58f6591b78a7377
  date: 2026-09-07T21:55:59Z
  reviewers: [fact-check, solid-hygiene]
  findings:
    critical: 0
    important: 1
    medium: 8
    low: 7
    nitpick: 1
  net_negative_raised: 1
  net_negative_addressed: 1
  net_negative_remaining: 0
---

# Operating Session Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `SitePlugin.get_session()` loads the same account the CLI or the client already resolved for that invocation, and the login identity stamp that mutated the plugin instance is deleted (#174).

**Architecture:** One new dependency-light module, `src/graftpunk/session_scope.py`, holds the operating session (the slot, plus the account identifier during a login) in a `contextvars.ContextVar` for the duration of one dispatch, and owns the one precedence primitive `resolve_load_target(base, explicit)` that every "which session does this code mean" question goes through. The scope is set in exactly one place, `_run_handler_with_limits` in `client.py`, which is the single execution pipeline both dispatchers already call, so neither dispatcher touches the scope and a third would get it for free. The login command sets the scope around the login callable. The author-facing entry points that read the plugin instance today (`get_session`, `cache_login_session`) read the scope instead, base-scoped so a scope set for one plugin can never steer another. Nothing mutates a plugin instance.

**Tech Stack:** Python 3.11+, Typer CLI, requests, structlog, pytest (`uv run pytest`), ruff, ty 0.0.75.

**Spec:** `docs/superpowers/specs/2026-09-07-operating-session-scope-design.md` (approved; read it alongside this plan, and resolve any conflict in favour of the spec, except where a task carries a Design note recording a reviewed deviation).

## Global Constraints

- Python `>=3.11` typing throughout: `X | None`, never `Optional[X]`; `from __future__ import annotations` at the top of every new module, matching the files being modified.
- structlog event-style logging where a module logs at all: `LOG = get_logger(__name__)` at module scope, event name first, values as keyword arguments. `session_scope.py` logs nothing, by design: it is imported by the plugin base class (`plugins/cli_plugin.py`) and by the shared execution pipeline (`client.py`), so it has to stay free of their dependencies. It imports only `graftpunk.session_identity`.
- Gate command, green at every commit: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
- Tests assert behaviour through public surfaces and the real `fresh_backend` cache. Never assert mock-call counts (`assert_called_once_with`, `call_count`); where a seam must be patched, assert on the values the seam received (`mock.call_args[0][1]`).
- Placeholders only, in tests and docs: `myshop`, `alice@example.com`, `bob@example.com`, `fmtsite`. Never name a real site, store, account, or domain.
- No em dashes or en dashes anywhere in new prose, docstrings, comments, or commit messages. Use commas, colons, parentheses, or separate sentences.
- No attribution trailers in commits. Commit subjects are `type(scope): subject` and reference `#174`.
- `cache_session`'s signature does not change and it stays read-free.
- `CommandContext._operating_session_name` and `ctx.save_session()` are untouched and remain the recommended explicit channel for a command handler. The scope is derived from that field, never written beside it.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/graftpunk/session_scope.py` (new) | Owns `OperatingSession`, the ContextVar, the three accessors, and `resolve_load_target`, the one place the explicit/scope/bare precedence lives. Pure in-process state; no logging, no storage, no CLI. |
| `src/graftpunk/client.py` | `_run_handler_with_limits`, the one pipeline both dispatchers call, sets the scope from `ctx._operating_session_name` around the handler. |
| `src/graftpunk/plugins/cli_plugin.py` | `get_session` and `cache_login_session` both call `resolve_load_target`; two `browser_session` comments change. |
| `src/graftpunk/cli/login_commands.py` | `make_login_body` sets the scope and gains the "cached outside the target slot" advisory; `_stamp_login_identity`, `_MISSING`, and two now-unused imports are deleted. |
| `src/graftpunk/plugins/__init__.py` | Re-exports `cache_login_session` beside `SitePlugin`. |
| `tests/unit/cli_harness.py` | Gains `cacheable_browser_session`, the one factory for a session whose rider attributes survive the pickle round-trip. |
| `tests/unit/test_session_scope.py` (new) | The module's own contract, including the `asyncio.run` propagation proof and `resolve_load_target`'s table. |
| `tests/unit/test_api_session_resolution.py` | `get_session`'s precedence chain against the real backend. |
| `tests/unit/test_client.py` | The direct test that the pipeline sets and clears the scope. |
| `tests/unit/test_multi_account_e2e.py` | End to end through `invoke_plugin_app` and `GraftpunkClient`, so both dispatch paths are covered. |
| `tests/unit/test_login_identity.py` | The stamp's tests are deleted; the funnel's fallback and the hand-written login tests move to the scope; the by-hand migration case is covered. |
| `docs/HOW_IT_WORKS.md`, `CHANGELOG.md` | Author-facing documentation of the behaviour change. |

---

### Task 1: `session_scope` module

**Files:**
- Create: `src/graftpunk/session_scope.py`
- Test: `tests/unit/test_session_scope.py`

**Interfaces:**
- Consumes: `graftpunk.session_identity.split_session_name` and `graftpunk.session_identity.validate_session_name` (`src/graftpunk/session_identity.py:63` (`def split_session_name(name: str) -> tuple[str, str | None]:`), `src/graftpunk/session_identity.py:74` (`def validate_session_name(name: str) -> None:`)). Nothing else, so the plugin base class and the execution pipeline can both import it without dragging in storage or CLI dependencies.
- Produces:
  - `OperatingSession` (frozen dataclass) with fields `name: str` and `identifier: str | None = None`.
  - `operating_session(name: str, identifier: str | None = None) -> Iterator[OperatingSession]`, a `@contextmanager` that yields the `OperatingSession` it set.
  - `current_operating_session() -> OperatingSession | None`.
  - `operating_session_for(base: str) -> OperatingSession | None`.
  - `resolve_load_target(base: str, explicit: str | None = None) -> tuple[str, bool]`, returning `(name, resolve)` for the cache loaders. Tasks 2 and 4 both call it, so the precedence chain has exactly one implementation.

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
    resolve_load_target,
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


class TestResolveLoadTarget:
    """The one precedence chain: explicit, then scope, then the bare base."""

    def test_explicit_labelled_name_loads_exact(self) -> None:
        assert resolve_load_target("myshop", "myshop@alice") == ("myshop@alice", False)

    def test_explicit_bare_name_resolves(self) -> None:
        assert resolve_load_target("myshop", "myshop") == ("myshop", True)

    def test_a_scope_for_the_base_loads_exact(self) -> None:
        """The dispatcher already resolved that name, so a second listing is waste."""
        with operating_session("myshop@bob"):
            assert resolve_load_target("myshop") == ("myshop@bob", False)

    def test_a_scope_for_a_foreign_base_is_ignored(self) -> None:
        with operating_session("othersite@bob"):
            assert resolve_load_target("myshop") == ("myshop", True)

    def test_no_scope_falls_back_to_the_resolving_base(self) -> None:
        assert resolve_load_target("myshop") == ("myshop", True)

    def test_an_explicit_name_wins_over_the_scope(self) -> None:
        with operating_session("myshop@bob"):
            assert resolve_load_target("myshop", "myshop@alice") == ("myshop@alice", False)
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
The shared execution pipeline sets it around every command handler and the
login command sets it around the login callable; author-facing entry points
(``SitePlugin.get_session``, ``cache_login_session``) read it instead of
reading state off the plugin instance, so nothing has to mutate a shared
object for the length of a flow.

This module is imported by the plugin base class
(``graftpunk.plugins.cli_plugin``) and by the shared execution pipeline
(``graftpunk.client``), so it has to stay free of their dependencies: it
imports only :mod:`graftpunk.session_identity`, and it never logs.

This is NOT the ambient pin. ``session_context`` reads ``GRAFTPUNK_SESSION``
and ``.gp-session`` from the environment, which the library path deliberately
ignores (#181). The scope is in-process state written by the dispatcher that
already applied the whole precedence chain, so the library and the CLI honour
it alike.
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
    "resolve_load_target",
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
        ValueError: *name* is not a legal session name. Every caller passes a
            name that already loaded or that the CLI already validated, so
            this is a programming-error guard rather than a user path.
    """
    validate_session_name(name)
    scope = OperatingSession(name=name, identifier=identifier)
    token = _CURRENT.set(scope)
    try:
        yield scope
    finally:
        _CURRENT.reset(token)


def current_operating_session() -> OperatingSession | None:
    """The scope in effect, or ``None`` outside any dispatch.

    The unguarded read, kept as the seam tests and diagnostics need to see
    what a dispatch actually set. Production code calls
    :func:`operating_session_for` instead, which applies the base-scoped
    guard: a scope set for one plugin must never answer for another.
    """
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


def resolve_load_target(base: str, explicit: str | None = None) -> tuple[str, bool]:
    """The slot *base* means right now, and whether the loader should resolve it.

    One chain with one owner. Code that has to answer "which session does this
    mean" asks here rather than re-deriving it:

    1. *explicit*, when given: it wins outright. A labelled name is exact; a
       bare name is a BASE and the loader resolves it (the pin contract).
    2. Otherwise the operating session scope, when one is set for *base*. The
       dispatcher that set it already resolved that name against the listing,
       so the load is exact-only and a second listing would be waste.
    3. Otherwise *base* itself, resolving, which is the pre-#174 behaviour.

    Args:
        base: The plugin's base session name.
        explicit: A name the caller already holds, or ``None``.

    Returns:
        ``(name, resolve)``, ready for
        :func:`graftpunk.cache.load_session_for_api` and its tuple variant. A
        caller that is writing rather than loading uses ``name`` and ignores
        ``resolve``.
    """
    if explicit is not None:
        return (explicit, split_session_name(explicit)[1] is None)
    scope = operating_session_for(base)
    if scope is not None:
        return (scope.name, False)
    return (base, True)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_session_scope.py -q`

Expected: PASS, 21 passed (11 module-level tests, of which the parametrized invalid-name one contributes 5, plus 6 in `TestResolveLoadTarget`).

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`

Expected: all four green. Nothing imports the new module yet, so the rest of the suite is unaffected.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/session_scope.py tests/unit/test_session_scope.py
git commit -m "feat(session-scope): add the in-process operating session scope (#174)"
```

> **Design note (2026-09-07):** the re-review asked whether `current_operating_session` earns its place, since no production code calls it. It does, as the deliberate test and diagnostic seam: `operating_session_for` is the guarded read production code uses, and a test proving a dispatch set nothing at all needs the unguarded one. Its docstring now says which is which, so the next reader does not reach for it in production code by mistake.

---

### Task 2: `get_session` follows the one precedence chain

**Files:**
- Modify: `src/graftpunk/plugins/cli_plugin.py:713` (`        """Load the graftpunk session for API calls."""`) (the `CLIPluginProtocol` declaration, lines 712 to 714), the `SitePlugin` method that runs from its `def` on line 1161 to the return at `src/graftpunk/plugins/cli_plugin.py:1199` (`        return load_session_for_api(self.session_name)`), and the import block after `src/graftpunk/plugins/cli_plugin.py:59` (`from graftpunk.observe import NoOpObservabilityContext, ObservabilityContext`)
- Test: `tests/unit/test_api_session_resolution.py` (append)

**Interfaces:**
- Consumes: `resolve_load_target` from Task 1; `load_session_for_api(name: str, *, resolve: bool = True) -> requests.Session` (`src/graftpunk/cache.py:698` (`def load_session_for_api(name: str, *, resolve: bool = True) -> requests.Session:`)).
- Produces: `SitePlugin.get_session(self, session_name: str | None = None) -> requests.Session`, and the same signature on `CLIPluginProtocol`. Later tasks call it with no argument from inside a scope.

The precedence chain itself lives in `resolve_load_target` (Task 1), so this method is the `requires_session` short-circuit plus one call plus one load. `requires_session=False` still short-circuits to a plain `requests.Session()` before anything is resolved, unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_api_session_resolution.py`. Add `operating_session` to the imports by inserting this line after the `graftpunk.session_identity` import at `tests/unit/test_api_session_resolution.py:38` (`from graftpunk.session_identity import GP_ACCOUNT_ATTR, GP_SESSION_NAME_ATTR`):

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

These reuse `_cached_session`, the factory already defined in this file at `tests/unit/test_api_session_resolution.py:56` (`def _cached_session(account: str) -> BrowserSession:`). Task 3 introduces a shared `cacheable_browser_session` in the CLI harness for the tests that have no such local factory; consolidating this file's copy onto it is deliberately out of scope (see Task 3's Design note).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_api_session_resolution.py -q -k "get_session"`

Expected: FAIL. `test_get_session_explicit_labelled_name_loads_that_slot` fails with `TypeError: get_session() takes 1 positional argument but 2 were given`; `test_get_session_follows_the_scope_with_two_accounts_cached` fails with `AmbiguousSessionError`.

- [ ] **Step 3: Write the implementation**

In `src/graftpunk/plugins/cli_plugin.py`, add the scope import directly below the existing observe import at `src/graftpunk/plugins/cli_plugin.py:59` (`from graftpunk.observe import NoOpObservabilityContext, ObservabilityContext`), which is where alphabetical order among the `graftpunk.*` imports puts it:

```python
from graftpunk.session_scope import resolve_load_target
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

        Which slot is loaded is decided by
        :func:`graftpunk.session_scope.resolve_load_target`: *session_name*
        when given (a labelled name is exact, a bare one is a base the loader
        resolves), else the operating session scope when one is set for this
        plugin's base name (loaded exact-only, since whoever set it already
        resolved it), else the bare ``self.session_name``, resolving. A scope
        set for another plugin's base is ignored, so one plugin's dispatch
        never steers another's load.

        This returns a SECOND ``requests.Session``, not ``ctx.session``.

        Args:
            session_name: An explicit slot or base name, overriding the scope
                and the plugin's base name. This is the channel for callers
                outside a dispatch: scripts, tests, and library code that
                already holds a name.

        Raises:
            SessionNotFoundError: Nothing is cached under the name that was
                loaded. On the scope path a miss is a miss, since that load is
                exact-only.
            AmbiguousSessionError: A resolving load (an explicit bare name, or
                no scope) found several cached sessions sharing the base and
                nothing selected one; the error names every candidate.
            ValueError: The name reaching the loader is not a legal session
                name.
        """
        if not self.requires_session:
            return requests.Session()
        name, resolve = resolve_load_target(self.session_name, session_name)
        return load_session_for_api(name, resolve=resolve)
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

> **Design note (2026-09-07):** the spec writes the precedence chain out inside `get_session`. The SOLID review found the same chain about to be inlined a second time in `cache_login_session` (Task 4), so it is factored into `resolve_load_target` in `session_scope.py` and both call sites use it. Behaviour is what the spec specifies: the spec's step 1 already says an explicit name loads with `resolve = split_session_name(name)[1] is None`, which `resolve_load_target` implements literally, so a labelled explicit name loads exact-only and does not pay for a listing on a miss. (The first draft of this plan called `load_session_for_api(session_name)` with the default `resolve=True` there, which silently diverged from the spec on that one path; the exception raised on a labelled miss, `SessionNotFoundError`, is the same either way.)

---

### Task 3: the execution pipeline sets the scope

**Files:**
- Modify: `src/graftpunk/client.py:81` (`def _run_handler_with_limits(`) through `src/graftpunk/client.py:145` (`    raise last_exc`), plus its import block
- Modify: `tests/unit/cli_harness.py` (append the shared session factory)
- Test: `tests/unit/test_client.py` (append to `TestRunHandlerWithLimits`), `tests/unit/test_multi_account_e2e.py` (append)

**Interfaces:**
- Consumes: `operating_session` from Task 1; `SitePlugin.get_session()` from Task 2; `CommandContext._operating_session_name` (`src/graftpunk/plugins/cli_plugin.py:250` (`    _operating_session_name: str = field(default="", repr=False)`)), which both dispatchers already populate with the slot they resolved and loaded.
- Produces: `cacheable_browser_session(identifier: str | None = None, backend_type: str = "nodriver") -> BrowserSession` in `tests/unit/cli_harness.py`. No new production names. The observable contract is that a handler running under a session-requiring command sees `current_operating_session().name == <the slot that was loaded>`, and a handler under `requires_session=False` sees `None`.

**Why one place.** `_run_handler_with_limits` is the single pipeline every dispatcher goes through. The CLI runtime calls `execute_plugin_command` (`src/graftpunk/client.py:148` (`def execute_plugin_command(`)), whose whole body after defaulting the rate-limit state is that one call, under the comment at `src/graftpunk/client.py:182-183` (`    # Execute with retry / rate-limit`). `GraftpunkClient._execute_command` (`src/graftpunk/client.py:382` (`    def _execute_command(self, spec: CommandSpec, **kwargs: Any) -> CommandResult:`)) calls it directly at the head of the block opened by `src/graftpunk/client.py:496-498` (`        # 4. Execute with retry/rate-limit; 403 token refresh`), and a second time inside that same block's 403 retry. Setting the scope there means neither dispatcher is touched, the `needs_session` condition is not duplicated (it is already baked into the empty-string convention on `ctx._operating_session_name`), each 403 retry re-enters the scope for free, and a future dispatcher gets the behaviour without knowing about it.

- [ ] **Step 1: Write the failing tests**

First append the shared factory to `tests/unit/cli_harness.py`. Widen the typing import at `tests/unit/cli_harness.py:10` (`from typing import Any`) to:

```python
from typing import TYPE_CHECKING, Any
```

add this block directly below `tests/unit/cli_harness.py:11` (`from unittest.mock import patch`), separated by a blank line:

```python
if TYPE_CHECKING:
    from graftpunk.session import BrowserSession
```

and append this function at the end of the file:

```python
def cacheable_browser_session(
    identifier: str | None = None, backend_type: str = "nodriver"
) -> BrowserSession:
    """A session whose rider attributes survive the cache's pickle round-trip.

    A plain ``requests.Session`` pickles through its ``__attrs__`` whitelist,
    which drops the rider attributes, so a test that caches one and loads it
    back cannot tell two accounts apart: the account identifier is gone.
    ``BrowserSession`` round-trips them in ``__getstate__``/``__setstate__``.
    Built with ``__new__`` so no browser process starts and no driver is
    required: this is a cache fixture, not a live session.

    Args:
        identifier: The account identifier to stamp, or None for a session
            with no recorded account.
        backend_type: The backend the cached session claims to come from.

    Returns:
        A ``BrowserSession`` ready to hand to ``cache_session``.
    """
    import requests

    from graftpunk.session import BrowserSession
    from graftpunk.session_identity import GP_ACCOUNT_ATTR

    session = BrowserSession.__new__(BrowserSession)
    requests.Session.__init__(session)
    session._backend_type = backend_type
    if identifier is not None:
        setattr(session, GP_ACCOUNT_ATTR, identifier)
    return session
```

Then append to `tests/unit/test_client.py`, inside `class TestRunHandlerWithLimits` (`tests/unit/test_client.py:814` (`class TestRunHandlerWithLimits:`)):

```python
    def test_the_operating_scope_is_set_around_the_handler(self) -> None:
        """#174: the one pipeline owns the scope, derived from the ctx field."""
        from graftpunk.session_scope import current_operating_session

        seen: dict = {}

        def handler(ctx, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            scope = current_operating_session()
            seen["name"] = scope.name if scope else None
            return {"ok": True}

        ctx = CommandContext(
            session=MagicMock(),
            plugin_name="testplugin",
            command_name="test",
            api_version=1,
            _operating_session_name="myshop@bob",
        )

        result = _run_handler_with_limits(handler, ctx, _make_spec("cmd"), {})

        assert result == {"ok": True}
        assert seen["name"] == "myshop@bob"
        assert current_operating_session() is None

    def test_an_empty_operating_name_sets_no_scope(self) -> None:
        """A requires_session=False command carries an empty name and no scope."""
        from graftpunk.session_scope import current_operating_session

        seen: dict = {}

        def handler(ctx, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            seen["scope"] = current_operating_session()
            return {"ok": True}

        _run_handler_with_limits(handler, self._make_ctx(), _make_spec("cmd"), {})

        assert seen["scope"] is None
```

Then append to `tests/unit/test_multi_account_e2e.py`, which proves both dispatch paths reach the one owner:

```python
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
        from tests.unit.cli_harness import cacheable_browser_session, invoke_plugin_app

        cache_session(cacheable_browser_session("alice@example.com"), "myshop@alice")
        cache_session(cacheable_browser_session("bob@example.com"), "myshop@bob")

        seen: dict = {}
        result = invoke_plugin_app(
            _whoami_plugin(seen), ["fmtsite", "whoami", "--session", "myshop@bob"]
        )

        assert result.exit_code == 0, result.output
        assert seen["account"] == "bob@example.com"

    def test_client_pin_steers_get_session_inside_the_handler(self, fresh_backend) -> None:  # noqa: ANN001
        from unittest.mock import patch

        from graftpunk.client import GraftpunkClient
        from tests.unit.cli_harness import cacheable_browser_session

        cache_session(cacheable_browser_session("alice@example.com"), "myshop@alice")
        cache_session(cacheable_browser_session("bob@example.com"), "myshop@bob")

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

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_client.py -q -k "operating_scope or operating_name" && NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_multi_account_e2e.py -q -k "OperatingScope"`

Expected: FAIL. `test_the_operating_scope_is_set_around_the_handler` fails on `assert seen["name"] == "myshop@bob"` because `seen["name"]` is `None`; `test_cli_pin_steers_get_session_inside_the_handler` fails on `assert result.exit_code == 0` (the handler's `AmbiguousSessionError` reaches the CLI boundary funnel and becomes `SystemExit(1)`); `test_client_pin_steers_get_session_inside_the_handler` fails by raising `AmbiguousSessionError` out of `client.execute("whoami")`. All three have the same cause: nothing sets a scope, so `get_session()` resolves `myshop` on its own terms. `test_an_empty_operating_name_sets_no_scope` and `test_a_sessionless_command_sets_no_scope` pass already and are regression guards.

- [ ] **Step 3: Write the implementation**

In `src/graftpunk/client.py`, add the from-import after `import time` and before `from typing import Any` (`src/graftpunk/client.py:28` (`from typing import Any`)), which is where ruff's isort places it among the stdlib imports:

```python
from contextlib import AbstractContextManager, nullcontext
```

and add on the line after the closing paren of the `graftpunk.session_identity` import block, the block that opens at `src/graftpunk/client.py:44` (`from graftpunk.session_identity import (`) and closes four lines later:

```python
from graftpunk.session_scope import operating_session
```

Then replace `_run_handler_with_limits` in full (lines 81 to 145, from `def _run_handler_with_limits(` through `    raise last_exc`) with:

```python
def _run_handler_with_limits(
    handler: Any,
    ctx: CommandContext,
    spec: CommandSpec,
    rate_limit_state: dict[str, float],
    **kwargs: Any,
) -> Any:
    """Execute handler with retry and rate-limit support.

    Retries the handler up to ``spec.max_retries`` times with
    exponential backoff on transient failures.

    The handler runs inside the operating session scope named by
    ``ctx._operating_session_name``, so author-facing code in the handler
    (``self.get_session()``) loads the slot the dispatcher already resolved
    instead of resolving the plugin's bare base name on its own terms (#174).
    This is the one pipeline every dispatcher goes through (the CLI runtime
    via ``execute_plugin_command``, ``GraftpunkClient`` directly), so the scope
    has a single owner and derives from the field the context already carries
    rather than from a second copy of the condition. An empty name, which is
    what a ``requires_session=False`` command carries, sets no scope: there is
    no account for it to be about.

    The name understates what this now does: it is also the one place a
    handler is invoked from, which is why the scope belongs here. It is left
    as it is deliberately, since renaming a function the test suite imports by
    name buys nothing this change needs.

    Args:
        handler: The command handler callable.
        ctx: CommandContext to pass to the handler.
        spec: CommandSpec with retry/rate-limit configuration.
        rate_limit_state: Mutable dict tracking last-execution times.
        **kwargs: Additional keyword arguments for the handler.

    Returns:
        The handler's return value.

    Raises:
        Exception: The last exception if all attempts fail.
    """
    attempts = 1 + spec.max_retries
    last_exc: Exception | None = None
    command_key = f"{ctx.plugin_name}.{spec.name}"
    scope: AbstractContextManager[object] = (
        operating_session(ctx._operating_session_name)
        if ctx._operating_session_name
        else nullcontext()
    )

    with scope:
        for attempt in range(attempts):
            try:
                if spec.rate_limit:
                    _enforce_shared_rate_limit(
                        command_key,
                        spec.rate_limit,
                        rate_limit_state,
                    )
                result = handler(ctx, **kwargs)
                if asyncio.iscoroutine(result):
                    LOG.warning(
                        "async_handler_auto_executed",
                        command=spec.name,
                        plugin=ctx.plugin_name,
                    )
                    result = asyncio.run(result)
                return result
            except (
                requests.RequestException,
                ConnectionError,
                TimeoutError,
                OSError,
            ) as exc:
                last_exc = exc
                if attempt < attempts - 1:
                    backoff = 2**attempt
                    LOG.warning(
                        "command_retry",
                        command=spec.name,
                        attempt=attempt + 1,
                        backoff=backoff,
                    )
                    time.sleep(backoff)

    assert last_exc is not None  # for type narrowing
    raise last_exc
```

Neither `src/graftpunk/cli/plugin_runtime.py` nor `GraftpunkClient._execute_command` changes: both already build a `CommandContext` carrying the resolved name (`src/graftpunk/cli/plugin_runtime.py:219` (`            _operating_session_name=operating_name,`), `src/graftpunk/client.py:493` (`            _operating_session_name=(operating_name if needs_session else ""),`)), and that field is now the only input the scope needs.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_client.py tests/unit/test_multi_account_e2e.py tests/unit/test_plugin_runtime_session.py -q`

Expected: PASS, including the pre-existing retry, rate-limit and async-handler tests in `TestRunHandlerWithLimits`, whose contexts carry an empty operating name and so set no scope.

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`

Expected: all four green.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/client.py tests/unit/cli_harness.py tests/unit/test_client.py tests/unit/test_multi_account_e2e.py
git commit -m "feat(client): the shared pipeline sets the operating session scope (#174)"
```

> **Design note (2026-09-07):** the first draft of this plan had each dispatcher set the scope beside the `CommandContext` field it already writes, which is how the spec then read. The SOLID review flagged that as writing the same fact twice, with the `needs_session` condition duplicated beside a field that already holds it. The scope is now set once, in `_run_handler_with_limits`, the single pipeline both dispatchers already call, derived from `ctx._operating_session_name`; the spec's Design section has since been updated to the same shape, so the plan and the spec agree. Both dispatch paths are still covered end to end by the tests above, and the 403 retries re-enter the scope because each is a fresh call into the pipeline.
>
> **Design note (2026-09-07):** the re-review noted that `_run_handler_with_limits` no longer names everything the function does. It is not renamed: the name is private, the test suite imports it directly, and churning those call sites buys no behaviour. The docstring says plainly that this is also the one place a handler is invoked from, which is why the scope lives here.
>
> **Design note (2026-09-07):** the SOLID review flagged the test helper that builds a `BrowserSession` via `__new__` and `_backend_type`. That idiom is already copied into four test files, six times: `tests/unit/test_account_metadata.py:63` (`    session = BrowserSession.__new__(BrowserSession)`) and `tests/unit/test_account_metadata.py:207` (`    stored = BrowserSession.__new__(BrowserSession)`), `tests/unit/test_api_session_resolution.py:65` (`    session = BrowserSession.__new__(BrowserSession)`), `tests/unit/test_client.py:68` (`    stored = BrowserSession.__new__(BrowserSession)`), and twice in `tests/unit/test_session.py`, at `tests/unit/test_session.py:18-23` (`    def test_session_name_setter(self):`) and `tests/unit/test_session.py:27-32` (`    def test_session_name_getter_uses_cached_name(self):`). So this task adds one shared factory, `cacheable_browser_session`, in `tests/unit/cli_harness.py` and every new test in this plan that needs a cacheable session uses it. Consolidating the existing copies is out of scope here and belongs in a follow-up; note it in the PR description. No production constructor is added: `__new__` without a driver is a test-only shape.

---

### Task 4: retire the login identity stamp

**Files:**
- Modify: `src/graftpunk/cli/login_commands.py:156` (`@contextmanager`) through `src/graftpunk/cli/login_commands.py:210` (`            setattr(plugin, GP_ACCOUNT_ATTR, prior_attr)`) (delete `_stamp_login_identity`), `src/graftpunk/cli/login_commands.py:45` (`_MISSING = object()`) and the comment above it (delete), the `GP_ACCOUNT_ATTR` entry on line 30 of the import block that opens at `src/graftpunk/cli/login_commands.py:29` (`from graftpunk.session_identity import (`) (delete that entry), `src/graftpunk/cli/login_commands.py:334` (`                _stamp_login_identity(plugin, label, identifier),`) (replace), `src/graftpunk/cli/login_commands.py:389` (`            gp_console.info(f"Session name: {target_name}")`) (insert the new advisory after it), and the two stdlib imports that only the stamp used
- Modify: `src/graftpunk/plugins/cli_plugin.py:1112` (`            # No explicit name/identifier: the instance state (the login`), `src/graftpunk/plugins/cli_plugin.py:1153` (`            # Instance fallback by contract -- see browser_session() above.`), and `src/graftpunk/plugins/cli_plugin.py:1221` (`    session_name = name or plugin.session_name`) through `src/graftpunk/plugins/cli_plugin.py:1222` (`    account = identifier if identifier is not None else getattr(plugin, GP_ACCOUNT_ATTR, None)`)
- Test: `tests/unit/test_login_identity.py` (delete `TestStamp`, rewrite the funnel fallback test and the hand-written login helper and tests, add seven tests)

**Interfaces:**
- Consumes: `operating_session` and `operating_session_for` from Task 1. `resolve_load_target` is not used here: see this task's Design note on why the funnel decides the scope itself. `make_login_body` already computes `target_name` and `identifier` before the call (`src/graftpunk/cli/login_commands.py:307` (`        target_name = (`)), so nothing new has to be derived.
- Produces: `cache_login_session(plugin, session, *, name=None, identifier=None) -> str` with an unchanged signature and an unchanged "explicit first" rule; only the source of its fallback changes. Nothing writes `GP_ACCOUNT_ATTR` onto a plugin instance any more. One new log event, `login_cached_outside_target_slot`.

**Behaviour change to carry into the docs (Task 5):** during `gp <site> login --as bob`, `self.session_name` read inside a hand-written `login()` is now `myshop`, the class attribute, always. A login that caches through the helpers or through `cache_login_session(self, session)` is unaffected. One that calls `cache_session(session, self.session_name)` by hand now lands on the bare slot, and the CLI now says so after the login instead of leaving it silent. The generated flows are untouched: they receive `session_name` and `account_identifier` explicitly (`src/graftpunk/plugins/login_engine.py:645` (`def _generate_nodriver_login(plugin: SitePlugin) -> Any:`), `src/graftpunk/plugins/login_engine.py:869` (`def _generate_selenium_login(plugin: SitePlugin) -> Any:`)).

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_login_identity.py`:

1. Replace the module docstring (line 1) with:

```python
"""Login derives, scopes, funnels and warns about account identity (#151, #174)."""
```

2. Delete the whole `TestStamp` class (`tests/unit/test_login_identity.py:15` (`class TestStamp:`) through line 98, ending at `            assert plugin.session_name == "myshop"`), leaving `class TestFunnel:` as the first class in the file.

3. Replace `test_instance_fallback_for_author_facing_paths` (`tests/unit/test_login_identity.py:121` (`    def test_instance_fallback_for_author_facing_paths(self) -> None:`) through the end of that method, line 136) with these two tests, which use the real cache rather than a patched seam:

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

    def test_an_explicit_name_does_not_borrow_the_scope_identity(self, fresh_backend) -> None:  # noqa: ANN001
        """The slot and the account come from one decision, never from two tiers.

        A caller naming the slot is naming the account too, or recording none.
        Taking the name from the argument and the identifier from the scope
        would stamp whoever the login command was about onto a session that
        caller never named.
        """
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
            used = cache_login_session(plugin, session, name="myshop@bob")

        assert used == "myshop@bob"
        assert not hasattr(session, GP_ACCOUNT_ATTR)
        assert list_sessions() == ["myshop@bob"]
        assert get_session_metadata("myshop@bob")["account_identifier"] is None
```

4. Replace `_hand_written_login_plugin` (`tests/unit/test_login_identity.py:307` (`def _hand_written_login_plugin(seen: dict | None = None):  # noqa: ANN001, ANN202`) through line 324) with a version that records the base name it sees and caches through the funnel, and add three more plugin factories beside it:

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


def _by_hand_login_plugin():  # noqa: ANN202
    """A login that caches under ``self.session_name`` by hand: the migration case.

    With the stamp gone this writes the bare ``myshop`` while the CLI targeted
    ``myshop@bob``, which is exactly the silent divergence the post-login
    advisory has to catch.
    """
    from graftpunk.cache import cache_session
    from graftpunk.plugins.cli_plugin import SitePlugin

    class ShopPlugin(SitePlugin):
        site_name = "fmtsite"
        base_url = "https://fmt.example.com"
        session_name = "myshop"

        def login(self, credentials: dict[str, str]) -> bool:
            """Sign in to fmtsite."""
            cache_session(requests.Session(), self.session_name)
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

5. Replace the three `TestLoginWrapperOrchestration` tests that reach the login body. `test_derived_label_names_the_cached_session` (`tests/unit/test_login_identity.py:349` (`    def test_derived_label_names_the_cached_session(self) -> None:`)), `test_as_flag_overrides_the_label`, and `test_advisory_failure_never_fails_a_successful_login` become these, which drop the `cache_session` patch and assert against the real backend instead:

```python
    def test_derived_label_names_the_cached_session(self, fresh_backend) -> None:  # noqa: ANN001
        from graftpunk.cache import get_session_metadata, list_sessions

        seen: dict = {}
        result = _invoke(_hand_written_login_plugin(seen), ["fmtsite", "login"], **_CREDS)

        assert result.exit_code == 0, result.output
        assert "Session name: myshop@alice" in result.output
        # The bare base name, inside the plugin's own login(): the stamp is gone.
        assert seen["session_name"] == "myshop"
        assert seen["credentials"] == {"username": "alice", "password": "x"}
        # What the scope carried is what the cache received.
        assert list_sessions() == ["myshop@alice"]
        assert get_session_metadata("myshop@alice")["account_identifier"] == "alice"
        # The happy path says nothing extra.
        assert "This login did not write" not in result.output

    def test_as_flag_overrides_the_label(self, fresh_backend) -> None:  # noqa: ANN001
        from graftpunk.cache import list_sessions

        seen: dict = {}
        result = _invoke(
            _hand_written_login_plugin(seen), ["fmtsite", "login", "--as", "work"], **_CREDS
        )

        assert result.exit_code == 0, result.output
        assert "Session name: myshop@work" in result.output
        assert seen["session_name"] == "myshop"
        assert list_sessions() == ["myshop@work"]

    def test_advisory_failure_never_fails_a_successful_login(self, fresh_backend) -> None:  # noqa: ANN001
        """The verdict is sealed before the advisory block: a raise there is a hint lost."""
        with patch(
            "graftpunk.cli.login_commands.get_active_session",
            side_effect=FileNotFoundError("cwd was removed"),
        ):
            result = _invoke(_hand_written_login_plugin(), ["fmtsite", "login"], **_CREDS)

        assert result.exit_code == 0, result.output
        assert "Logged in to fmtsite (session cached)" in result.output
        assert "Login failed" not in result.output
```

The other two tests in that class stay exactly as they are. `test_bad_as_label_names_the_label_the_user_typed` never reaches the login callable, and `test_slot_recorded_for_another_account_warns_after_success` already runs under `fresh_backend`, so the helper's new real cache write lands in that test's own temporary backend and the warning it asserts is still computed from the metadata read before the attempt.

6. Append these four tests to the same class:

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

    def test_a_by_hand_cache_under_the_bare_name_warns_after_success(
        self,
        fresh_backend,  # noqa: ANN001
    ) -> None:
        """#174: the login succeeded, but nothing landed on the labelled slot."""
        from structlog.testing import capture_logs

        from graftpunk.cache import list_sessions

        with capture_logs() as logs:
            result = _invoke(
                _by_hand_login_plugin(),
                ["fmtsite", "login", "--as", "bob"],
                FMTSITE_USERNAME="bob@example.com",
                FMTSITE_PASSWORD="x",
            )

        # Advisory only: the login itself still succeeded.
        assert result.exit_code == 0, result.output
        assert "Logged in to fmtsite (session cached)" in result.output
        # It wrote the bare slot, not the one the CLI targeted.
        assert list_sessions() == ["myshop"]
        assert "This login did not write 'myshop@bob'" in result.output
        assert "cache_login_session" in result.output
        assert "login_cached_outside_target_slot" in [e["event"] for e in logs]

    def test_a_by_hand_write_warns_even_when_the_target_slot_already_exists(
        self,
        fresh_backend,  # noqa: ANN001
    ) -> None:
        """Existence is not the question; movement is.

        An earlier login left ``myshop@bob`` cached. This login targets the
        same slot, writes the bare base instead, and leaves that slot stale. A
        check that only asked whether the slot exists would say nothing, and
        the user would go on using a session this login never refreshed.
        """
        from graftpunk.cache import cache_session, get_session_metadata
        from tests.unit.cli_harness import cacheable_browser_session

        cache_session(cacheable_browser_session("bob@example.com"), "myshop@bob")
        before = get_session_metadata("myshop@bob")["modified_at"]

        result = _invoke(
            _by_hand_login_plugin(),
            ["fmtsite", "login", "--as", "bob"],
            FMTSITE_USERNAME="bob@example.com",
            FMTSITE_PASSWORD="x",
        )

        assert result.exit_code == 0, result.output
        assert "This login did not write 'myshop@bob'" in result.output
        # The pre-existing slot really was left untouched.
        assert get_session_metadata("myshop@bob")["modified_at"] == before

    def test_an_unlabelled_target_never_warns(self, fresh_backend) -> None:  # noqa: ANN001
        """No label means the bare base IS the target, so a by-hand write is correct."""
        from graftpunk.cache import list_sessions

        result = _invoke(
            _by_hand_login_plugin(),
            ["fmtsite", "login"],
            FMTSITE_USERNAME="!!!",
            FMTSITE_PASSWORD="x",
        )

        assert result.exit_code == 0, result.output
        assert "Session name: myshop" in result.output
        assert list_sessions() == ["myshop"]
        assert "This login did not write" not in result.output

    def test_a_failed_login_leaves_no_scope_behind(self) -> None:
        from graftpunk.session_scope import current_operating_session

        result = _invoke(_raising_login_plugin(), ["fmtsite", "login"], **_CREDS)

        assert result.exit_code != 0
        assert "Login failed: the site was down" in result.output
        assert current_operating_session() is None
```

`test_an_unlabelled_target_never_warns` pins the no-label branch by value rather than by field name: the login command's default fields for a hand-written login are `username` and `password`, and `username` is always identifier-shaped, so the label has to be defeated by a value that slugifies to nothing. `derive_account_identity({"username": "!!!", ...})` returns `("!!!", None)`, because `slugify("!!!")` is the empty string and a label that cannot be produced yields `None` (`src/graftpunk/session_identity.py:174` (`    label = slugify(identifier)`)). With no label, `target_name` is the bare `myshop`, which is exactly what the by-hand login wrote, and the labelled-only advisory never runs.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_login_identity.py -q`

Expected: FAIL. `test_scope_fallback_for_author_facing_paths` fails on `assert used == "myshop@alice"` (the funnel still reads the instance and returns `"myshop"`); `test_derived_label_names_the_cached_session` fails on `assert seen["session_name"] == "myshop"` (the stamp still overwrites it with `"myshop@alice"`); `test_a_by_hand_cache_under_the_bare_name_warns_after_success` and `test_a_by_hand_write_warns_even_when_the_target_slot_already_exists` both fail on `assert "This login did not write 'myshop@bob'" in result.output`, and the first also on `assert list_sessions() == ["myshop"]` because the stamp still makes `self.session_name` read `myshop@bob`. `test_browser_session_sync_login_lands_on_the_scoped_slot` passes today via the stamp and must keep passing after the change.

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
    split_session_name,
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

6. Insert the new advisory into the existing post-login advisory block, directly after `src/graftpunk/cli/login_commands.py:389` (`            gp_console.info(f"Session name: {target_name}")`) and before `            current = get_active_session()`:

```python
            # The line above says what the CLI computed, not what landed. On a
            # labelled target, compare the slot across the login: the ONE
            # pre-attempt fetch already captured it, so a second fetch here
            # answers "did this login write the slot it named". Existence alone
            # would not: a hand-written login caching under self.session_name by
            # hand now writes the bare base (#174), and if the labelled slot
            # happened to exist already from an earlier login, an existence test
            # would stay silent while the user keeps using a stale session. A
            # pre-attempt fetch that failed leaves stored_before None; a slot
            # that exists afterwards is then taken as landed, because nothing
            # here can prove otherwise. This sits inside the advisory try: a
            # storage hiccup costs the hint, not the login.
            if split_session_name(target_name)[1] is not None:
                stored_after = get_session_metadata(target_name)
                landed = stored_after is not None and (
                    stored_before is None
                    or stored_after.get("modified_at") != stored_before.get("modified_at")
                )
                if not landed:
                    LOG.warning(
                        "login_cached_outside_target_slot",
                        plugin=plugin.site_name,
                        target=target_name,
                    )
                    gp_console.warn(
                        f"This login did not write '{target_name}'. A login that caches "
                        "by hand should call cache_login_session(self, session), or "
                        "accept session_name and account_identifier keyword arguments."
                    )
```

`stored_before` is the pre-attempt capture `make_login_body` already takes for `target_name` (`src/graftpunk/cli/login_commands.py:321` (`            stored_before = get_session_metadata(target_name)`)), so this reuses one notion of "did this login land on its target" rather than inventing a second. `get_session_metadata` is already imported at `src/graftpunk/cli/login_commands.py:20` (`from graftpunk.cache import get_session_metadata`), it returns `dict | None` with `modified_at` as an ISO-8601 string (`src/graftpunk/storage/base.py:102` (`        "modified_at": metadata.modified_at.isoformat(),`)), and this block is already inside the `try` whose `except` logs `post_login_advisory_failed`, so a storage failure here can never fail a successful login.

In `src/graftpunk/plugins/cli_plugin.py`:

7. Widen the scope import added by Task 2 to:

```python
from graftpunk.session_scope import operating_session_for, resolve_load_target
```

8. Replace the two-line comment at lines 1112 to 1113 inside `browser_session`:

```python
            # No explicit name/identifier: the operating session scope, set by
            # the login command around the login callable, IS this path's input.
```

9. Replace the comment at line 1153 inside `browser_session_sync`:

```python
            # Scope fallback by contract -- see browser_session() above.
```

10. Replace the body and docstring of `cache_login_session` (`src/graftpunk/plugins/cli_plugin.py:1202` (`def cache_login_session(`) onward) with:

```python
def cache_login_session(
    plugin: SitePlugin,
    session: Any,
    *,
    name: str | None = None,
    identifier: str | None = None,
) -> str:
    """The one login-flow cache funnel. Explicit arguments first.

    *name* when given wins outright: the generated login flows pass it, and so
    does any caller that already knows the slot. Otherwise the operating
    session scope the login command set around the login callable supplies both
    the slot and the account, and failing that the plugin's bare base name
    does. The scope is base-scoped, so another plugin's login can never rename
    this write. That is how a hand-written ``login()`` caching through
    ``browser_session`` or ``browser_session_sync`` lands on the
    account-qualified slot with nothing mutating the plugin instance (#174).

    The scope is consulted ONCE, and the slot and the identifier come from the
    same decision: an explicit *name* means the caller is naming the slot, so
    the identifier falls back to nothing rather than to the scope's. Pairing a
    slot from one tier with an account from another would record whoever the
    login command happened to be about against a session it did not name.

    The identifier rides the session object so ``_extract_session_metadata``
    records it. Returns the session name used.
    """
    from graftpunk.session_identity import GP_ACCOUNT_ATTR

    scope = operating_session_for(plugin.session_name) if name is None else None
    session_name = name if name is not None else (scope.name if scope else plugin.session_name)
    account = identifier if identifier is not None else (scope.identifier if scope else None)
    if account is not None:
        setattr(session, GP_ACCOUNT_ATTR, account)
    cache_session(session, session_name)
    return session_name
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_login_identity.py tests/unit/test_plugin_commands.py -q`

Expected: PASS, including the generated-flow tests (`test_nodriver_flow_caches_under_the_threaded_name`, `test_selenium_flow_caches_under_the_threaded_name`), which pass `session_name` and `account_identifier` explicitly and are unaffected, and the `TestBrowserSessionContextManager` tests in `test_plugin_commands.py`.

Note for the executor: `test_browser_session_sync_login_lands_on_the_scoped_slot` patches `cache_session`, so nothing really lands in the cache and the new advisory fires in that test's output. That is correct behaviour under a mocked cache; the test asserts only on what the funnel received, so it is unaffected. No other test asserts the absence of that text except the two that check the happy path with a real backend.

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

> **Design note (2026-09-07):** the spec writes `cache_login_session`'s fallback as an inline chain identical to `get_session`'s, and an earlier revision of this plan routed both through `resolve_load_target`. The re-review found that sharing it paired a slot from one tier with an account from another: with an explicit `name` and a scope set, the write took the caller's slot and the scope's identifier. `cache_login_session` now decides the scope once, so the slot and the account always come from the same tier and an explicit name records no borrowed identity. `resolve_load_target` still serves `get_session`, which needs the `resolve` flag the pin contract defines and has no identifier to pair.
>
> **Design note (2026-09-07):** the first version of the post-login advisory tested whether the labelled slot exists. The re-review pointed out that a slot left over from an earlier login makes that test pass while this login wrote somewhere else, so the user is left on a stale session with no warning. It now compares the slot across the login using `stored_before`, the capture `make_login_body` already takes before the attempt, so there is one notion of whether a login landed on its target and no second fetch tier.
>
> **Design note (2026-09-07):** the spec accepts, without comment, that a `login()` calling `cache_session(session, self.session_name)` by hand now writes the bare slot. The SOLID review classed the silence as the real defect: the login prints "Session name: myshop@bob" and exits 0 while nothing exists under `myshop@bob`. Step 3 item 6 adds one advisory on the labelled path only, after success, reusing the existing post-login advisory block so it can never fail a login, and costing one extra metadata read on that path. The migration itself is unchanged, and stays documented in Task 5.

---

### Task 5: exports, author docs, and changelog

**Files:**
- Modify: `src/graftpunk/plugins/__init__.py:26` (`from graftpunk.plugins.cli_plugin import (`) block and `src/graftpunk/plugins/__init__.py:73` (`    "SitePlugin",`) in `__all__`
- Modify: `docs/HOW_IT_WORKS.md:146` (`slot (or vanish into one that does not exist).`) and the "Custom Login Method" section that opens at `docs/HOW_IT_WORKS.md:572` (`### 2. Custom Login Method (Python)`)
- Modify: `CHANGELOG.md:8` (`## [Unreleased]`)

**Interfaces:**
- Consumes: `cache_login_session` (Task 4's final form).
- Produces: `from graftpunk.plugins import cache_login_session` works. Nothing else depends on this task.

**Export audit at HEAD.** `SitePlugin` is exported from `src/graftpunk/plugins/__init__.py` only (imported at line 38, listed in `__all__` at line 73). The top-level `src/graftpunk/__init__.py` does **not** export `SitePlugin` at all: its `__all__` (`src/graftpunk/__init__.py:138` (`__all__ = [`)) covers the client, session, cache, storage, config and exception surfaces, and no plugin base class. `cache_login_session` is public in `cli_plugin.py` but is exported from neither package `__init__`, so the spec's parenthetical "public, exported alongside `SitePlugin`" is aspirational at HEAD and this task makes it true. Only `src/graftpunk/plugins/__init__.py` changes; the top-level `__init__.py` is left alone, since adding a plugin-authoring name there would be a new surface the spec does not ask for.

- [ ] **Step 1: Add the export**

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

Extend `__all__`'s first group so the name sits beside `SitePlugin`:

```python
__all__ = [
    # Base classes and decorators
    "SitePlugin",
    "cache_login_session",
    "command",
    "CLIPluginProtocol",
```

Nothing from `graftpunk.session_scope` is re-exported. `operating_session` is set by the dispatchers and by the login command; a plugin author has no reason to call it, and `get_session(session_name=...)` is the channel for code running outside a dispatch.

- [ ] **Step 2: Verify the export resolves**

Run: `NO_COLOR=1 FORCE_COLOR= uv run python -c "from graftpunk.plugins import SitePlugin, cache_login_session; print(SitePlugin.__name__, cache_login_session.__name__)"`

Expected: `SitePlugin cache_login_session`

- [ ] **Step 3: Update `docs/HOW_IT_WORKS.md`**

In the "Multi-Account Sessions" section, insert this paragraph immediately after the paragraph whose last line is `docs/HOW_IT_WORKS.md:146` (`slot (or vanish into one that does not exist).`), separated by a blank line on each side:

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

In the section that opens at `docs/HOW_IT_WORKS.md:572` (`### 2. Custom Login Method (Python)`), insert this paragraph immediately after line 598, the one-line paragraph beginning "The browser_session() and browser_session_sync() context managers handle browser lifecycle, cookie transfer, and session caching automatically.", separated by a blank line on each side:

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
slot, and the CLI says so after the login: switch it to
`cache_login_session(self, session)`, or declare `session_name` and
`account_identifier` as keyword arguments on `login()` and the CLI passes both
in.
```

- [ ] **Step 4: Update `CHANGELOG.md`**

Under `## [Unreleased]`, add an `### Added` section directly above the existing `### Changed`, and add one bullet to the top of `### Changed`:

```markdown
### Added

- **The operating session scope** ([#174](https://github.com/stavxyz/graftpunk/issues/174)). A new internal `graftpunk.session_scope` module holds the session slot one dispatch is about in a `contextvars.ContextVar`, and owns `resolve_load_target(base, explicit)`, the one place the explicit-then-scope-then-bare precedence lives. The shared execution pipeline sets the scope around every command handler from the operating name the dispatcher already resolved, and the login command sets it around the login callable, so both the CLI and `GraftpunkClient` behave the same way without either of them managing the scope itself.
- **`SitePlugin.get_session(session_name=None)`** ([#174](https://github.com/stavxyz/graftpunk/issues/174)). `get_session()` now follows one chain: an explicit `session_name`, then the operating session scope, then the plugin's bare base name. Called from a command handler it loads the slot the invocation is pinned to, so `self.get_session()` under `--session myshop@bob` returns bob's session where it used to resolve `myshop` on its own terms and raise `AmbiguousSessionError` with two accounts cached. An explicit name wins over both tiers: a labelled name loads exactly, a bare name resolves. Outside a command, nothing sets a scope and the behaviour is unchanged. `cache_login_session` is now exported from `graftpunk.plugins` beside `SitePlugin`.

### Changed

- **`CLIPluginProtocol.get_session` gained an optional `session_name` parameter** ([#174](https://github.com/stavxyz/graftpunk/issues/174)). A plugin that subclasses `SitePlugin` is unaffected. A plugin that satisfies the protocol structurally, with its own `get_session(self) -> requests.Session`, no longer matches it: add `session_name: str | None = None` to that signature. Nothing calls the protocol method with an argument, so an implementation that accepts and ignores it is enough.

- **The login identity stamp is retired, and a hand-written `login()` now reads the bare base name** ([#174](https://github.com/stavxyz/graftpunk/issues/174)). `gp <site> login --as bob` used to temporarily overwrite `plugin.session_name` and an account attribute on the plugin instance for the length of one login flow. It now sets the operating session scope instead and never mutates the instance. **Behaviour change:** inside your own `login()`, `self.session_name` always reads the class attribute (`myshop`), where during a login it used to read `myshop@bob`. A `login()` that caches through `browser_session()`, `browser_session_sync()`, or `cache_login_session(self, session)` is unaffected and still lands on `myshop@bob` with the identifier recorded. A `login()` that calls `cache_session(session, self.session_name)` by hand now writes the bare `myshop` slot; a successful login that leaves the account-qualified slot empty now warns and names the fix, rather than reporting a session name that nothing was cached under. The migration is one line: call `cache_login_session(self, session)` (exported from `graftpunk.plugins`), or declare `session_name` and `account_identifier` keyword arguments on `login()`, which the CLI already passes to any login that accepts them. Generated declarative logins are unaffected: they receive both values explicitly.
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

> **Design note (2026-09-07):** widening `CLIPluginProtocol.get_session` is a typing change for anyone who implements the protocol structurally rather than by subclassing `SitePlugin`: their zero-argument `get_session` stops satisfying it. The re-review asked for that to be stated where such an implementer would look, so the CHANGELOG Changed section names it and gives the one-line signature fix.
>
> **Design note (2026-09-07):** the spec's migration sentence said this change would export `operating_session` from `graftpunk.plugins` beside `SitePlugin`. The SOLID review classed that as a net-negative public surface: no author calls it, since the dispatchers and the login command are the only setters, and `get_session(session_name=...)` already serves code running outside a dispatch. Only `cache_login_session` is exported. The spec sentence has been amended in place to say so.

---

## Self-review notes

**Spec coverage.** Every section of the spec maps to a task: the new module and its propagation proof to Task 1; half 1 (`get_session` precedence, the `CLIPluginProtocol` declaration, the docstring) to Task 2; the scope around the handler and the end-to-end tests through both dispatch paths to Task 3; half 2 (the stamp's deletion, the funnel's fallback, the `browser_session` comments, the test surgery) to Task 4; the documentation and changelog section to Task 5. The spec's "What does not change" list is honoured: no task changes `CommandContext`'s shape or semantics, `load_session_for_api_resolved`, `update_session_cookies`, the rider registry, the pin contract, or the client's construction-time validation.

**Signature consistency.** `operating_session(name, identifier=None)`, `current_operating_session()`, `operating_session_for(base)`, and `resolve_load_target(base, explicit=None) -> tuple[str, bool]` are defined once in Task 1 and used with those exact spellings in Tasks 2, 3, and 4. `get_session(self, session_name: str | None = None)` is defined in Task 2 and called with zero or one positional argument thereafter. `cache_login_session(plugin, session, *, name=None, identifier=None) -> str` keeps its HEAD signature. `cacheable_browser_session(identifier=None, backend_type="nodriver")` is defined in Task 3 and used only there.

**Verified against HEAD.** Every line citation in this plan was read at the commit this branch is on, and every line number is therefore HEAD-relative. Earlier tasks shift the numbers in the files they touch (Task 2 adds one import line to `cli_plugin.py` and rewrites `get_session`, so every Task 4 citation below line 59 of that file moves down; Task 3 lengthens `_run_handler_with_limits`, so the `client.py` citations move down too), so locate each edit by the quoted anchor text, which is authoritative, rather than by the number. Four notes for the executor:

- The spec points at `src/graftpunk/client.py:493` (`            _operating_session_name=(operating_name if needs_session else ""),`) and names the enclosing method `GraftpunkClient.execute`. The line is correct, but the method is `GraftpunkClient._execute_command` (`src/graftpunk/client.py:382` (`    def _execute_command(self, spec: CommandSpec, **kwargs: Any) -> CommandResult:`)); `execute` (`src/graftpunk/client.py:331` (`    def execute(self, *args: str, **kwargs: Any) -> CommandResult:`)) only resolves the command and delegates. After the reviewed change in Task 3 neither method is edited at all, since the scope is set in `_run_handler_with_limits`.
- The spec calls `cache_login_session` "public, exported alongside `SitePlugin`". At HEAD it is exported from neither package `__init__`. Task 5 adds it to `src/graftpunk/plugins/__init__.py`, which is the only place `SitePlugin` is exported from.
- The spec's Design section names the shared pipeline as the scope's single owner, which is exactly what Task 3 implements, so the plan and the spec agree. Task 3's first Design note records how that shape was arrived at.
- The spec's `cache_login_session` snippet inlines the precedence chain and reads the identifier from the scope independently of the name. Task 4's first Design note records why the funnel decides the scope once instead, so an explicit name never borrows the scope's identity, and why `resolve_load_target` serves only `get_session`.
