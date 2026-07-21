"""Run-time execution pipeline for plugin CLI commands.

Execution ONLY (the construction factory lives in ``cli/command_factory.py``).
``run_plugin_command`` is the verbatim 12-step pipeline formerly inlined in
``_create_plugin_command``'s callback: session load -> observe -> token inject
-> execute_plugin_command -> 403-refresh -> session persist -> format_output,
with the CommandError/PluginError/generic error->exit funnel.
"""

from __future__ import annotations

from typing import Any, Literal

import requests
import typer
from rich.console import Console

from graftpunk import console as gp_console
from graftpunk.cache import update_session_cookies
from graftpunk.cli.command_factory import BUILTIN_OPTIONS
from graftpunk.exceptions import BrowserError, CommandError, PluginError, SessionNotFoundError
from graftpunk.logging import get_logger
from graftpunk.observe import build_observe_context
from graftpunk.plugins.cli_plugin import CLIPluginProtocol, CommandContext, CommandSpec
from graftpunk.plugins.formatters import format_output

LOG = get_logger(__name__)
_format_console = Console()


def format_source_is_commandline(ctx: typer.Context) -> bool:
    """True when --format was explicitly passed on the command line.

    Compares the parameter source by NAME, not enum identity: typer<0.26 runs
    external Click, typer>=0.26 runs its vendored fork, and their
    ``ParameterSource`` enums are distinct classes whose members never compare
    equal across the boundary. ``source.name`` is stable on both.
    """
    source = ctx.get_parameter_source("format")
    return source is not None and source.name == "COMMANDLINE"


def run_plugin_command(
    plugin: CLIPluginProtocol,
    cmd_spec: CommandSpec,
    ctx: typer.Context,
    **kwargs: Any,
) -> None:
    from graftpunk.client import execute_plugin_command

    # CHANGED: built-in pop defaults come from the shared BUILTIN_OPTIONS
    # contract (declared in command_factory) instead of inline literals.
    output_format = kwargs.pop("format", BUILTIN_OPTIONS["format"])
    # CHANGED: typer passes --view as a list (or None) -- normalize to tuple.
    view_args: tuple[str, ...] = tuple(kwargs.pop("view", BUILTIN_OPTIONS["view"]) or ())
    output_path: str = kwargs.pop("output", BUILTIN_OPTIONS["output"])
    # CHANGED: explicit --format detection now uses the Typer-injected context
    # (name-compare; see format_source_is_commandline) instead of external
    # click.get_current_context()/click.core.ParameterSource.
    format_is_explicit = format_source_is_commandline(ctx)

    # Per-command requires_session override
    needs_session = (
        cmd_spec.requires_session
        if cmd_spec.requires_session is not None
        else plugin.requires_session
    )

    # --- Session loading (CLI-specific error handling) ---
    try:
        session = plugin.get_session() if needs_session else requests.Session()
    except SessionNotFoundError:
        gp_console.error(
            f"Session '{plugin.session_name}' not found. Please create a session first."
        )
        raise SystemExit(1) from None
    except PluginError as exc:
        gp_console.error(f"Plugin error: {exc}")
        raise SystemExit(1) from exc
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        gp_console.error(f"Failed to load session: {exc}")
        LOG.exception("session_load_failed", plugin=plugin.site_name)
        raise SystemExit(1) from exc

    # Set gp_base_url for relative Referer resolution
    base_url = getattr(plugin, "base_url", "")
    if base_url and hasattr(session, "gp_base_url"):
        setattr(session, "gp_base_url", base_url)  # noqa: B010

    # --- Observability context (CLI-specific) ---
    # CHANGED: root-context lookup uses the injected Typer ctx (same
    # find_root()/obj API) instead of external click.get_current_context().
    observe_mode: Literal["off", "full"] = "off"
    if ctx is not None:
        parent = ctx.find_root()
        observe_mode = (parent.obj or {}).get("observe_mode", "off")

    backend_type = plugin.backend
    try:
        driver = session.driver  # type: ignore[attr-defined]
    except (BrowserError, AttributeError):
        driver = None
        LOG.debug(
            "driver_not_available_for_observe",
            plugin=plugin.site_name,
        )
    observe_ctx = build_observe_context(
        plugin.site_name,
        backend_type,
        driver,
        observe_mode,
    )
    if observe_mode != "off" and driver is None:
        gp_console.warn(
            f"Observability capture unavailable for "
            f"'{plugin.site_name}': no browser driver. "
            f"Only event logging will work."
        )

    # --- Token injection (CLI-specific ValueError handling) ---
    token_config = getattr(plugin, "token_config", None)
    if token_config is not None and needs_session:
        from graftpunk.tokens import (
            prepare_session as _prepare_tokens,
        )

        base_url = getattr(plugin, "base_url", "")
        try:
            _prepare_tokens(session, token_config, base_url)
        except ValueError as exc:
            gp_console.error(f"Token extraction failed: {exc}")
            raise SystemExit(1) from exc

    # --- Build context and execute via shared pipeline ---
    try:
        cmd_ctx = CommandContext(  # CHANGED: renamed local (ctx is the Typer context)
            session=session,
            plugin_name=plugin.site_name,
            command_name=cmd_spec.name,
            api_version=plugin.api_version,
            base_url=getattr(plugin, "base_url", ""),
            config=getattr(plugin, "_plugin_config", None),
            observe=observe_ctx,
            _session_name=(plugin.session_name if needs_session else ""),
        )

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

        # Persist session if requested
        if (cmd_spec.saves_session or cmd_ctx._session_dirty) and needs_session:
            update_session_cookies(session, plugin.session_name)

        format_output(
            result,
            output_format,
            _format_console,
            user_explicit=format_is_explicit,
            view_args=view_args,
            output_path=output_path,
            plugin_formatters=getattr(plugin, "format_overrides", None) or None,
        )
    except (SystemExit, KeyboardInterrupt):
        raise
    except CommandError as exc:
        gp_console.error(exc.user_message)
        raise SystemExit(1) from exc
    except PluginError as exc:
        gp_console.error(f"Plugin error: {exc}")
        raise SystemExit(1) from exc
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        LOG.exception(
            "plugin_command_failed",
            plugin=plugin.site_name,
            command=cmd_spec.name,
            error=str(exc),
            exc_type=type(exc).__name__,
        )
        gp_console.error(f"Command failed: {exc}")
        raise SystemExit(1) from exc
