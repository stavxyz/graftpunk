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
