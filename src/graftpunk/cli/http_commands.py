"""Ad-hoc HTTP requests with cached session cookies.

Provides the ``gp http`` command group for making authenticated HTTP
requests using session cookies cached by graftpunk plugins. Supports
browser-like headers, JSON/form bodies, token injection, and built-in
observability via HAR capture.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, cast

import requests
import typer
from rich.markup import escape

if TYPE_CHECKING:
    from graftpunk.graftpunk_session import GraftpunkSession

from graftpunk import _distribution
from graftpunk import console as gp_console
from graftpunk.cache import load_session_for_api_resolved, no_account_cached_message
from graftpunk.cli.errors import exit_missing_browser_stack
from graftpunk.cookies import BrowserCookieSession
from graftpunk.exceptions import SessionInvalidatedError, SessionNotFoundError
from graftpunk.logging import get_logger
from graftpunk.observe import ObservabilityContext, build_observe_context
from graftpunk.session_context import resolve_session
from graftpunk.session_identity import split_session_name

LOG = get_logger(__name__)

http_app = typer.Typer(
    name="http",
    help="Make ad-hoc HTTP requests with cached session cookies.",
    no_args_is_help=True,
)


def _resolve_json_body(json_arg: str) -> str:
    """Resolve JSON body from inline string, ``@filename``, or ``@-`` (stdin).

    Args:
        json_arg: Inline JSON string, ``@path/to/file.json``, or ``@-`` for stdin.

    Returns:
        JSON string ready to send as request body.

    Raises:
        typer.BadParameter: If file not found or stdin read fails.
    """
    if json_arg == "@-":
        return sys.stdin.read()
    if json_arg.startswith("@"):
        filepath = json_arg[1:]
        if not os.path.isfile(filepath):
            raise typer.BadParameter(f"File not found: {filepath}")
        with open(filepath) as f:
            return f.read()
    return json_arg


# CLI shorthand → internal role name.
# "navigate" is friendlier than "navigation" on the command line.
_ROLE_ALIASES: dict[str, str] = {
    "navigate": "navigation",
}


def _resolve_role_name(name: str) -> str:
    """Resolve a CLI role name to the internal role name.

    Applies aliases (e.g. ``"navigate"`` → ``"navigation"``) and
    passes everything else through unchanged.
    """
    return _ROLE_ALIASES.get(name, name)


def _role_help_text() -> str:
    """Build dynamic help text for ``--role`` showing registered roles."""
    from graftpunk.graftpunk_session import list_roles

    names = list_roles()
    return f"Header role ({', '.join(names)}, or plugin-defined)."


def _dispatch_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    role: str | None = None,
    **kwargs: Any,
) -> requests.Response:
    """Dispatch a request using a role or plain ``session.request()``.

    When *role* is set and the session supports roles
    (i.e. is a ``GraftpunkSession``), delegates to
    ``session.request_with_role()``.  This works for built-in
    roles (xhr, navigation, form) **and** any custom roles
    registered by plugins.

    Args:
        session: The session to use.
        method: HTTP method.
        url: Target URL.
        role: Header role name — built-in or custom.
        **kwargs: Passed through to the request method.

    Returns:
        The HTTP response.
    """
    if role is not None:
        if not hasattr(session, "request_with_role"):
            LOG.warning(
                "role_ignored_session_lacks_support",
                role=role,
                session_type=type(session).__name__,
            )
            raise ValueError(
                f"--role '{role}' requires a GraftpunkSession, but got {type(session).__name__}"
            )
        resolved = _resolve_role_name(role)
        gp_session = cast("GraftpunkSession", session)
        return gp_session.request_with_role(resolved, method, url, **kwargs)
    return session.request(method, url, **kwargs)


def _make_request(
    method: str,
    url: str,
    *,
    session_name: str | None = None,
    no_session: bool = False,
    browser_headers: bool = True,
    role: str | None = None,
    json_body: str | None = None,
    form_data: str | None = None,
    extra_headers: list[str] | None = None,
    timeout: float = 30.0,
    on_session: Callable[[requests.Session, str | None], None] | None = None,
) -> tuple[requests.Response, str | None]:
    """Make an HTTP request, optionally using a cached graftpunk session.

    Args:
        method: HTTP method (GET, POST, etc.).
        url: Target URL.
        session_name: Session name to load — a plugin/base name is put through
            account resolution here. Falls back to ``resolve_session()``.
        no_session: When True, use a session without cookies
            (:class:`graftpunk.cookies.BrowserCookieSession`).
        browser_headers: Whether to keep auto-detected browser header roles.
            When False, clears ``_gp_header_roles`` so the session sends
            only its base headers.
        role: Header role to use — built-in (``"xhr"``,
            ``"navigate"``, ``"form"``) or any custom name defined by
            a plugin's ``header_roles`` dict.  Requires a
            ``GraftpunkSession``.  When None, falls back to
            ``session.request()`` (default behavior).
        json_body: JSON string body (mutually exclusive with form_data).
        form_data: Form-encoded body string (mutually exclusive with json_body).
        extra_headers: List of ``"Name: value"`` header strings.
        timeout: Request timeout in seconds.
        on_session: Called with the ready session and the operating session
            name just before the request is sent (``gp http`` starts its
            observe recording there).

    Returns:
        The HTTP response and the operating session name it was made with
        (None with ``no_session``). The caller needs that name — resolution
        happens once, here, where the load happens, and every downstream use
        (the observe run dir) must key off the SAME name, not the raw
        argument (#151).

    Raises:
        typer.Exit: If session cannot be resolved or loaded.
    """
    session: requests.Session
    resolved: str | None = None
    if no_session:
        gp_console.info("No session: making an unauthenticated request")
        session = BrowserCookieSession()
    else:
        resolved = session_name or resolve_session(None)
        if not resolved:
            gp_console.error(
                "No session specified. Use --session, GRAFTPUNK_SESSION, "
                "gp session use, or --no-session."
            )
            raise typer.Exit(1)
        # gp http joins account resolution: a plugin/base name with exactly one
        # cached account loads that account; several exit with the pick-one
        # list, the same way every other surface behaves (#151). Imported
        # lazily — plugin_commands imports the CLI stack this module is part of.
        from graftpunk.cli.plugin_commands import (
            is_registered_site_name,
            resolve_session_name_or_exit,
        )

        # A registered site name already had resolve_session_name_or_exit
        # consult the listing (the exact-hit check on the base, and the
        # account fallback on that same listing) -- asking the loader to
        # resolve again would repeat that listing on the not-found path
        # (#178). A pin can still be a BARE base name here for any OTHER
        # name (one that is not a registered plugin passes through
        # resolve_session_name unchanged): the loader resolves it and
        # reports the slot it loaded, which is the name every downstream
        # use must key off -- the observe run dir included (#182). An
        # already-resolved name is an exact hit and lists nothing.
        is_site_name = is_registered_site_name(resolved)
        resolved = resolve_session_name_or_exit(resolved)
        try:
            session, resolved = load_session_for_api_resolved(
                resolved,
                resolve=(not is_site_name) and split_session_name(resolved)[1] is None,
            )
        except Exception as exc:  # noqa: BLE001 — CLI boundary
            module = _distribution.missing_browser_module(exc)
            if module is not None:
                exit_missing_browser_stack(
                    f"gp http {method.lower()} with a cached session", module
                )
            # resolve=False for a registered site name means
            # resolve_session_name_or_exit already exhausted the account
            # listing for this base; the backend's own miss message
            # ("Session 'x' not found") is technically correct but loses
            # that context, so restore the actionable wording
            # load_session_for_api_resolved itself would have raised for
            # the same case (#178, PR #189 review).
            message = (
                no_account_cached_message(resolved)
                if is_site_name and isinstance(exc, SessionNotFoundError)
                else str(exc)
            )
            LOG.error("session_load_failed", session_name=resolved, error=message)
            gp_console.error(f"Failed to load session '{resolved}': {message}")
            raise typer.Exit(1) from exc

    if not browser_headers and hasattr(session, "clear_header_roles"):
        cast("GraftpunkSession", session).clear_header_roles()

    # Apply extra headers from --header flags
    for header_str in extra_headers or []:
        if ":" not in header_str:
            gp_console.error(f"Invalid header format (expected 'Name: value'): {header_str}")
            raise typer.Exit(1)
        name, _, value = header_str.partition(":")
        session.headers[name.strip()] = value.strip()

    # Prepare request kwargs
    kwargs: dict[str, object] = {"timeout": timeout}

    if json_body is not None:
        kwargs["data"] = json_body
        session.headers["Content-Type"] = "application/json"
    elif form_data is not None:
        kwargs["data"] = form_data
        session.headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    # Token injection from plugin session map (skip for bare sessions)
    from graftpunk.cli.plugin_commands import get_plugin_for_session

    plugin = get_plugin_for_session(resolved) if resolved else None

    token_config = getattr(plugin, "token_config", None) if plugin else None
    if token_config is not None:
        from graftpunk.tokens import prepare_session

        base_url = getattr(plugin, "base_url", "")
        prepare_session(session, token_config, base_url)

    # Merge plugin-defined header roles into the session so that
    # --role can reference custom names declared by the plugin.
    plugin_roles = getattr(plugin, "header_roles", None) if plugin else None
    if plugin_roles and hasattr(session, "merge_header_roles"):
        for role_name in plugin_roles:
            if role_name in getattr(session, "_gp_header_roles", {}):
                LOG.debug(
                    "plugin_role_overwrites_captured",
                    role=role_name,
                    plugin=getattr(plugin, "site_name", "unknown"),
                )
        cast("GraftpunkSession", session).merge_header_roles(plugin_roles)

    if on_session is not None:
        on_session(session, resolved)

    try:
        response = _dispatch_request(session, method, url, role=role, **kwargs)
    except ValueError as exc:
        gp_console.error(str(exc))
        raise typer.Exit(1) from exc
    except requests.exceptions.ConnectionError as exc:
        gp_console.error(f"Connection failed: {exc}")
        raise typer.Exit(1) from exc
    except requests.exceptions.Timeout as exc:
        gp_console.error(f"Request timed out: {exc}")
        raise typer.Exit(1) from exc
    except requests.exceptions.RequestException as exc:
        gp_console.error(f"Request failed: {exc}")
        raise typer.Exit(1) from exc

    # Token refresh on 403: clear cached tokens and retry once
    if response.status_code == 403 and token_config is not None:
        from graftpunk.tokens import clear_cached_tokens
        from graftpunk.tokens import prepare_session as _prepare_retry

        LOG.info("token_refresh_on_403", url=url)
        clear_cached_tokens(session)
        base_url = getattr(plugin, "base_url", "")
        try:
            _prepare_retry(session, token_config, base_url)
        except SessionInvalidatedError as exc:
            gp_console.error(f"Token refresh failed: {exc}")
            gp_console.info("The session is no longer authenticated; log in again.")
            return response, resolved  # Return the original 403 response
        except ValueError as exc:
            gp_console.error(f"Token refresh failed: {exc}")
            return response, resolved  # Return the original 403 response
        try:
            response = _dispatch_request(session, method, url, role=role, **kwargs)
        except requests.exceptions.RequestException as exc:
            gp_console.error(f"Retry request failed: {exc}")
            raise typer.Exit(1) from exc

    return response, resolved


def _start_observe(
    http_session: requests.Session, operating_session: str | None, url: str
) -> ObservabilityContext:
    """Start recording *http_session* into a new observe run, and return the run.

    The run is filed under the operating session name ``_make_request``
    resolved, not the raw argument (#151), or for ``--no-session`` under the
    site name inferred from *url*. Each request the session then sends is one
    HAR entry per redirect hop, timed as it is sent
    (:class:`graftpunk.observe.http_capture.HttpCaptureBackend`). The caller
    calls ``finish()`` on the result.
    """
    namespace = operating_session
    if not namespace:
        from graftpunk.plugins import infer_site_name

        namespace = infer_site_name(url) or ""
        if not namespace:
            LOG.warning("namespace_inference_failed", url=url, fallback="unknown")
            namespace = "unknown"
    run = build_observe_context(namespace, "", None, "full", http_session=http_session)
    run.start()
    return run


def _print_response(
    response: requests.Response,
    *,
    body_only: bool = False,
    verbose: bool = False,
) -> None:
    """Print response to stdout.

    Args:
        response: HTTP response to print.
        body_only: If True, print only the response body.
        verbose: If True, show request and response headers.
    """
    if body_only:
        sys.stdout.write(response.text)
        return

    if verbose:
        # Print request info
        req = response.request
        assert req is not None  # Always set after Response is created
        gp_console.info(f"> {req.method} {req.url}")
        for key, value in req.headers.items():
            gp_console.info(f"> {key}: {value}")
        gp_console.info(">")

        # Print response info
        gp_console.info(f"< HTTP {response.status_code} {response.reason}")
        for key, value in response.headers.items():
            gp_console.info(f"< {key}: {value}")
        gp_console.info("<")

    # Default: status line + body
    status_color = "green" if response.ok else "red"
    gp_console.err_console.print(
        f"[{status_color}]HTTP {response.status_code}[/{status_color}]"
        f" [dim]{escape(str(response.reason))}[/dim]"
        f" [dim]({response.elapsed.total_seconds():.2f}s)[/dim]"
    )
    sys.stdout.write(response.text)
    if response.text and not response.text.endswith("\n"):
        sys.stdout.write("\n")


def _http_command(method: str) -> Callable[..., None]:
    """Factory that creates a Typer command for the given HTTP method.

    Args:
        method: HTTP method name (GET, POST, etc.).

    Returns:
        A Typer-compatible command function.
    """

    def command(
        url: Annotated[str, typer.Argument(help="Target URL")],
        session: Annotated[
            str | None,
            typer.Option("--session", "-s", help="Session name"),
        ] = None,
        no_session: Annotated[
            bool,
            typer.Option("--no-session", help="Make request without a cached session"),
        ] = False,
        json_body: Annotated[
            str | None,
            typer.Option("--json", "-j", help="JSON body (inline, @file, @- for stdin)"),
        ] = None,
        data: Annotated[
            str | None,
            typer.Option("--data", "-d", help="Form-encoded body"),
        ] = None,
        header: Annotated[
            list[str] | None,
            typer.Option("--header", "-H", help="Extra header(s), format 'Name: value'"),
        ] = None,
        role: Annotated[
            str | None,
            typer.Option(
                "--role",
                help=_role_help_text(),
            ),
        ] = None,
        no_browser_headers: Annotated[
            bool,
            typer.Option("--no-browser-headers", help="Disable browser-like headers"),
        ] = False,
        body_only: Annotated[
            bool,
            typer.Option("--body-only", help="Output only response body (pipe-friendly)"),
        ] = False,
        verbose: Annotated[
            bool,
            typer.Option("--verbose", "-v", help="Show request and response headers"),
        ] = False,
        no_observe: Annotated[
            bool,
            typer.Option("--no-observe", help="Disable observe (HAR) capture"),
        ] = False,
        timeout: Annotated[
            float,
            typer.Option("--timeout", help="Request timeout in seconds"),
        ] = 30.0,
    ) -> None:
        if no_session and session:
            gp_console.error("Cannot use --session and --no-session together.")
            raise typer.Exit(1)

        if role is not None and no_session:
            gp_console.error("--role requires a session (roles need a GraftpunkSession).")
            raise typer.Exit(1)

        resolved_json: str | None = None
        if json_body is not None:
            resolved_json = _resolve_json_body(json_body)

        # Observe (on by default): record the request as it is sent.
        runs: list[ObservabilityContext] = []

        def record(http_session: requests.Session, operating: str | None) -> None:
            if not no_observe:
                runs.append(_start_observe(http_session, operating, url))

        try:
            response, _operating_session = _make_request(
                method,
                url,
                session_name=session,
                no_session=no_session,
                browser_headers=not no_browser_headers,
                role=role,
                json_body=resolved_json,
                form_data=data,
                extra_headers=header,
                timeout=timeout,
                on_session=record,
            )
        finally:
            for run in runs:
                run.finish()

        _print_response(response, body_only=body_only, verbose=verbose)

    command.__doc__ = f"Make an HTTP {method.upper()} request."
    return command


# Register HTTP method commands
for _method in ("get", "post", "put", "patch", "delete", "head", "options"):
    http_app.command(_method)(_http_command(_method))
