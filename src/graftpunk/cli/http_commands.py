"""Ad-hoc HTTP requests with cached session cookies.

Provides the ``gp http`` command group for making authenticated HTTP
requests using session cookies cached by graftpunk plugins. Supports
browser-like headers, JSON/form bodies, token injection, and built-in
observability via HAR capture.
"""

from __future__ import annotations

import datetime
import os
import sys
from typing import Annotated

import requests
import typer

from graftpunk import console as gp_console
from graftpunk.cache import load_session_for_api
from graftpunk.logging import get_logger
from graftpunk.observe import OBSERVE_BASE_DIR
from graftpunk.observe.storage import ObserveStorage
from graftpunk.session_context import resolve_session

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


def _make_request(
    method: str,
    url: str,
    *,
    session_name: str | None = None,
    no_session: bool = False,
    browser_headers: bool = True,
    json_body: str | None = None,
    form_data: str | None = None,
    extra_headers: list[str] | None = None,
    timeout: float = 30.0,
) -> requests.Response:
    """Make an HTTP request, optionally using a cached graftpunk session.

    Args:
        method: HTTP method (GET, POST, etc.).
        url: Target URL.
        session_name: Session name to load. Falls back to ``resolve_session()``.
        no_session: When True, use a bare ``requests.Session`` without cookies.
        browser_headers: Whether to keep auto-detected browser header profiles.
            When False, clears ``_gp_header_profiles`` so the session sends
            only its base headers.
        json_body: JSON string body (mutually exclusive with form_data).
        form_data: Form-encoded body string (mutually exclusive with json_body).
        extra_headers: List of ``"Name: value"`` header strings.
        timeout: Request timeout in seconds.

    Returns:
        The HTTP response.

    Raises:
        typer.Exit: If session cannot be resolved or loaded.
    """
    session: requests.Session
    if no_session:
        gp_console.info("No session — making unauthenticated request")
        session = requests.Session()
    else:
        resolved = session_name or resolve_session(None)
        if not resolved:
            gp_console.error(
                "No session specified. Use --session, GRAFTPUNK_SESSION, "
                "gp session use, or --no-session."
            )
            raise typer.Exit(1)
        try:
            session = load_session_for_api(resolved)
        except Exception as exc:  # noqa: BLE001 — CLI boundary
            gp_console.error(f"Failed to load session '{resolved}': {exc}")
            raise typer.Exit(1) from exc

    if not browser_headers and hasattr(session, "_gp_header_profiles"):
        session._gp_header_profiles = {}  # type: ignore[assignment]

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
    resolved = None if no_session else (session_name or resolve_session(None))
    from graftpunk.cli.plugin_commands import get_plugin_for_session

    plugin = get_plugin_for_session(resolved) if resolved else None

    token_config = getattr(plugin, "token_config", None) if plugin else None
    if token_config is not None:
        from graftpunk.tokens import prepare_session

        base_url = getattr(plugin, "base_url", "")
        prepare_session(session, token_config, base_url)

    try:
        response = session.request(method, url, **kwargs)
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
        except ValueError as exc:
            gp_console.error(f"Token refresh failed: {exc}")
            return response  # Return the original 403 response
        response = session.request(method, url, **kwargs)

    return response


def _save_observe_data(
    session_name: str,
    method: str,
    url: str,
    response: requests.Response,
    request_body: str | None = None,
) -> ObserveStorage | None:
    """Save request/response data as a HAR entry.

    Args:
        session_name: Session name for directory organization.
        method: HTTP method used.
        url: Request URL.
        response: The HTTP response.
        request_body: Optional request body text.

    Returns:
        ObserveStorage instance if saved, None on error.
    """
    try:
        run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
        storage = ObserveStorage(OBSERVE_BASE_DIR, session_name, run_id)

        # Build minimal HAR entry
        har_entry: dict[str, object] = {
            "startedDateTime": datetime.datetime.now(tz=datetime.UTC).isoformat(),
            "request": {
                "method": method.upper(),
                "url": url,
                "headers": [{"name": k, "value": v} for k, v in response.request.headers.items()],  # type: ignore[union-attribute]
                "bodySize": len(request_body) if request_body else 0,
            },
            "response": {
                "status": response.status_code,
                "statusText": response.reason or "",
                "headers": [{"name": k, "value": v} for k, v in response.headers.items()],
                "content": {
                    "size": len(response.content),
                    "mimeType": response.headers.get("Content-Type", ""),
                    "text": response.text[:50000],  # Cap at 50KB
                },
                "bodySize": len(response.content),
            },
            "time": response.elapsed.total_seconds() * 1000,
        }

        if request_body:
            har_entry["request"] = {
                **har_entry["request"],  # type: ignore[arg-type]
                "postData": {
                    "mimeType": response.request.headers.get("Content-Type", ""),  # type: ignore[union-attribute]
                    "text": request_body,
                },
            }

        storage.write_har([har_entry])
        return storage
    except Exception as exc:  # noqa: BLE001 — observability is best-effort
        LOG.warning("observe_save_failed", error=str(exc))
        gp_console.warn(f"Failed to save request trace: {exc}")
        return None


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
        f" [dim]{response.reason}[/dim]"
        f" [dim]({response.elapsed.total_seconds():.2f}s)[/dim]"
    )
    sys.stdout.write(response.text)
    if response.text and not response.text.endswith("\n"):
        sys.stdout.write("\n")


def _http_command(method: str) -> typer.models.CommandFunctionType:
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

        resolved_json: str | None = None
        if json_body is not None:
            resolved_json = _resolve_json_body(json_body)

        response = _make_request(
            method,
            url,
            session_name=session,
            no_session=no_session,
            browser_headers=not no_browser_headers,
            json_body=resolved_json,
            form_data=data,
            extra_headers=header,
            timeout=timeout,
        )

        # Observe: save HAR data by default
        if not no_observe:
            if no_session:
                from graftpunk.plugins import infer_site_name

                resolved_namespace = infer_site_name(url) or "unknown"
            else:
                resolved_namespace = session or resolve_session(None) or "unknown"
            request_body = resolved_json or data
            _save_observe_data(resolved_namespace, method, url, response, request_body)

        _print_response(response, body_only=body_only, verbose=verbose)

    command.__doc__ = f"Make an HTTP {method.upper()} request."
    return command  # type: ignore[invalid-return-type]


# Register HTTP method commands
for _method in ("get", "post", "put", "patch", "delete", "head", "options"):
    http_app.command(_method)(_http_command(_method))
