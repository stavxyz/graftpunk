"""CLI error-boundary helpers: enrich, render, exit."""

import functools
from collections.abc import Callable
from typing import NoReturn, ParamSpec, TypeVar

from graftpunk import _distribution
from graftpunk import console as gp_console
from graftpunk.cache import get_session_metadata
from graftpunk.exceptions import AmbiguousSessionError


def exit_ambiguous_session(
    exc: AmbiguousSessionError, *, backend_override: str | None = None
) -> NoReturn:
    """Enrich the candidates with account identifiers, render, exit(1).

    Identifiers are fetched lazily — for the candidates only, best-effort
    (a candidate with no metadata degrades to its bare name) — so the
    console renderer stays a pure formatter.

    Args:
        exc: The ambiguity raised by resolution.
        backend_override: Storage backend the caller was given, so the
            identifiers come from the same backend the candidates do.
    """
    candidates: list[tuple[str, str | None]] = []
    for name in exc.candidates:
        identifier: str | None = None
        try:
            meta = get_session_metadata(name, backend_override=backend_override)
            identifier = meta.get("account_identifier") if meta else None
        except Exception:  # noqa: BLE001 — display is best-effort
            identifier = None
        candidates.append((name, identifier))
    gp_console.render_ambiguous_session(exc.base_name, candidates)
    raise SystemExit(1)


def exit_missing_browser_stack(what: str, module: str) -> NoReturn:
    """Say that *what* needs the browser stack and how to add it, then exit(1).

    Args:
        what: The command, as the user typed it (``gp observe go``).
        module: The browser module that failed to import.
    """
    gp_console.error(
        f"{what} needs the browser automation stack, which is not installed "
        f"(no module named {module!r}). Install it with: "
        f"{_distribution.install_commands('browser')}"
    )
    raise SystemExit(1)


_P = ParamSpec("_P")
_R = TypeVar("_R")


def needs_browser_stack(command: Callable[_P, _R]) -> Callable[_P, _R]:
    """Turn a missing browser module into the install hint and exit(1).

    For a command that drives a browser or loads a cached ``BrowserSession``.
    On graftpunk-lite without ``[browser]`` such a command fails importing one
    of ``_distribution.BROWSER_MODULES``; this names the extra to install
    instead of printing a traceback. Any other import failure propagates.
    """

    @functools.wraps(command)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return command(*args, **kwargs)
        except ImportError as exc:
            module = _distribution.missing_browser_module(exc)
            if module is None:
                raise
            # Duck-typed: newer Typer passes its vendored click Context, which
            # is not a typer.Context.
            paths = (getattr(value, "command_path", None) for value in kwargs.values())
            exit_missing_browser_stack(
                next((path for path in paths if isinstance(path, str)), "This command"), module
            )

    return wrapper
