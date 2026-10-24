"""Run-time execution pipeline for plugin CLI commands.

Execution ONLY (the construction factory lives in ``cli/command_factory.py``).
``run_plugin_command`` is the verbatim 12-step pipeline formerly inlined in
``_create_plugin_command``'s callback: session load -> observe -> token inject
-> execute_plugin_command -> 403-refresh -> session persist -> format_output,
with the CommandError/PluginError/generic error->exit funnel.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any, Literal, NoReturn

import requests
import typer
from rich.console import Console

from graftpunk import _distribution
from graftpunk import console as gp_console
from graftpunk.cache import list_sessions, load_session_for_api_resolved, update_session_cookies
from graftpunk.cli.command_factory import BUILTIN_OPTIONS
from graftpunk.cli.errors import exit_ambiguous_session, exit_missing_browser_stack
from graftpunk.cookies import BrowserCookieSession
from graftpunk.exceptions import (
    AmbiguousSessionError,
    BrowserError,
    CommandError,
    PluginError,
    SessionInvalidatedError,
    SessionNotFoundError,
    TokenPatternMismatchError,
)
from graftpunk.logging import get_logger, log_secrets
from graftpunk.observe import ObservabilityContext, build_observe_context
from graftpunk.observe.run import redact_text
from graftpunk.plugins.cli_plugin import CLIPluginProtocol, CommandContext, CommandSpec
from graftpunk.plugins.formatters import format_output
from graftpunk.session_context import get_active_session
from graftpunk.session_identity import compute_operating_session_name, split_session_name

if TYPE_CHECKING:
    from graftpunk.observe.run import Redaction

LOG = get_logger(__name__)
_format_console = Console()


def _driver_of(session: object) -> Any:
    """The WebDriver behind a ``BrowserSession``, ``None`` for any other session.

    Looked up in ``sys.modules`` because graftpunk-lite never loads
    ``graftpunk.session``. An object can be a ``BrowserSession`` only once that
    module is loaded, so an unloaded module means "not one".
    """
    module = sys.modules.get("graftpunk.session")
    if module is None or not isinstance(session, module.BrowserSession):
        return None
    return session.driver


def _exit_session_load_failed(plugin: CLIPluginProtocol, exc: Exception) -> NoReturn:
    gp_console.error(f"Failed to load session: {exc}")
    LOG.exception("session_load_failed", plugin=plugin.site_name)
    raise SystemExit(1) from exc


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
    # CHANGED: built-in pop defaults come from the shared BUILTIN_OPTIONS
    # contract (declared in command_factory) instead of inline literals.
    output_format = kwargs.pop("format", BUILTIN_OPTIONS["format"])
    # CHANGED: typer passes --view as a list (or None) -- normalize to tuple.
    view_args: tuple[str, ...] = tuple(kwargs.pop("view", BUILTIN_OPTIONS["view"]) or ())
    output_path: str = kwargs.pop("output", BUILTIN_OPTIONS["output"])
    explicit_session: str = kwargs.pop("session", BUILTIN_OPTIONS["session"])
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
    operating_name = ""
    try:
        if needs_session:
            # Session-identity policy stays logging-free (#151); log the
            # base-scoped ambient-ignore decision here, at the one CLI call
            # site, instead of importing graftpunk.logging into that module.
            ambient: str | None = None
            ambient_pins = False
            if not explicit_session:
                # Read ONCE here and hand the value to the chain, so the log
                # and the decision cannot disagree and the env/file is read
                # a single time per invocation.
                ambient = get_active_session()
                if ambient:
                    ambient_pins = split_session_name(ambient)[0] == plugin.session_name
                    if not ambient_pins:
                        LOG.debug(
                            "ambient_session_ignored_foreign_base",
                            ambient=ambient,
                            base=plugin.session_name,
                        )
            # list_sessions is passed UNCALLED: a pinned --session or a
            # matching ambient pin must not pay for a listing (#151).
            operating_name = compute_operating_session_name(
                explicit_session or None,
                plugin.session_name,
                list_sessions,
                ambient=ambient,
            )
            # A pin may name a BASE (--session myshop, GRAFTPUNK_SESSION=myshop):
            # the loader resolves it and reports the slot it landed on, which
            # from here on IS the operating name — for the ctx and for the
            # write-back below, which keys off it literally (#182). A name that
            # came out of the chain's resolution tier instead resolved against
            # the same listing already, so it loads exact-only rather than
            # paying for a second one.
            # Only a BARE pin asks the loader to resolve: the fallback matches
            # on the base, so resolving a labelled pin can only list uselessly.
            pinned = bool(explicit_session) or ambient_pins
            session, operating_name = load_session_for_api_resolved(
                operating_name,
                resolve=pinned and split_session_name(operating_name)[1] is None,
            )
        else:
            session = BrowserCookieSession()
    except SessionNotFoundError:
        # operating_name is "" when the failure came from the resolution step
        # itself; name the plugin's base session rather than an empty string.
        named = operating_name or plugin.session_name
        gp_console.error(f"Session '{named}' not found. Please create a session first.")
        raise SystemExit(1) from None
    except PluginError as exc:
        gp_console.error(f"Plugin error: {exc}")
        raise SystemExit(1) from exc
    except AmbiguousSessionError as exc:
        exit_ambiguous_session(exc)
    except ValueError as exc:
        if isinstance(exc, OSError):
            # io.UnsupportedOperation is both: an I/O failure, not a bad name.
            _exit_session_load_failed(plugin, exc)
        # A pin that cannot name a session (--session ../../x, a garbage
        # GRAFTPUNK_SESSION): a caller/config error, refused before storage.
        gp_console.error(f"Invalid session name: {exc}")
        raise SystemExit(1) from exc
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        module = _distribution.missing_browser_module(exc)
        if module is not None:
            exit_missing_browser_stack(
                ctx.command_path if ctx is not None else f"gp {plugin.site_name}", module
            )
        _exit_session_load_failed(plugin, exc)

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
        driver = _driver_of(session)
    except (BrowserError, AttributeError):
        driver = None
        LOG.debug(
            "driver_not_available_for_observe",
            plugin=plugin.site_name,
        )
    # A command runs on a requests session, not a browser: with no driver the
    # run records that session's HTTP traffic instead, scrubbed of the
    # plugin's secret credentials and the command's secret options as each
    # entry is recorded.
    option_secrets = cmd_spec.secret_values(kwargs)
    secrets = _observe_secrets(plugin, observe_mode, option_secrets)
    # Every log line from here on, the handler's and the recorder's included,
    # is scrubbed of the secret options.
    with log_secrets(option_secrets):
        observe_ctx = build_observe_context(
            plugin.site_name,
            backend_type,
            driver,
            observe_mode,
            http_session=session if driver is None else None,
            redact=secrets,
        )
        observe_ctx.start()
        try:
            _inject_tokens_and_run(
                plugin,
                cmd_spec,
                session,
                needs_session=needs_session,
                operating_name=operating_name,
                observe_ctx=observe_ctx,
                output_format=output_format,
                format_is_explicit=format_is_explicit,
                view_args=view_args,
                output_path=output_path,
                kwargs=kwargs,
                option_secrets=option_secrets,
            )
        finally:
            observe_ctx.finish(redact=secrets)


def _observe_secrets(
    plugin: CLIPluginProtocol, observe_mode: str, option_secrets: tuple[str, ...] = ()
) -> Redaction:
    """What a plugin command's run is scrubbed of: every login credential gp can
    read (:func:`graftpunk.cli.login_commands.known_credentials`), split as a
    login run's are (:meth:`graftpunk.observe.run.Redaction.from_credentials`),
    and the values of the command's secret options, as secrets."""
    from graftpunk.observe.run import Redaction

    if observe_mode == "off":
        return Redaction()
    from graftpunk.cli.login_commands import known_credentials

    try:
        credentials = Redaction.from_credentials(known_credentials(plugin))
    except Exception as exc:  # noqa: BLE001 - the run is still written, unscrubbed of these
        LOG.error("observe_secrets_unavailable", error=str(exc), exc_type=type(exc).__name__)
        credentials = Redaction()
    return Redaction(credentials.secrets + option_secrets, credentials.identifiers)


def _inject_tokens_and_run(
    plugin: CLIPluginProtocol,
    cmd_spec: CommandSpec,
    session: Any,
    *,
    needs_session: bool,
    operating_name: str,
    observe_ctx: ObservabilityContext,
    output_format: Any,
    format_is_explicit: bool,
    view_args: tuple[str, ...],
    output_path: str,
    kwargs: dict[str, Any],
    option_secrets: tuple[str, ...] = (),
) -> None:
    """Token injection, execution, the 403 retry, write-back and output.

    An error message or log line that can carry what the handler sent (a URL,
    an exception's text) is scrubbed of *option_secrets* first."""
    from graftpunk.client import execute_plugin_command

    # --- Token injection (CLI-specific ValueError handling) ---
    token_config = getattr(plugin, "token_config", None)
    if token_config is not None and needs_session:
        from graftpunk.tokens import (
            prepare_session as _prepare_tokens,
        )

        base_url = getattr(plugin, "base_url", "")
        try:
            _prepare_tokens(session, token_config, base_url)
        except SessionInvalidatedError as exc:
            gp_console.error(f"Token extraction failed: {exc}")
            gp_console.info(
                f"The session is no longer authenticated; run: gp {plugin.site_name} login"
            )
            raise SystemExit(1) from exc
        except TokenPatternMismatchError as exc:
            gp_console.error(f"Token extraction failed: {exc}")
            gp_console.info("The site's response no longer matches the Token config; update it.")
            raise SystemExit(1) from exc
        except requests.RequestException as exc:
            # An http extraction re-raises the request's failure (tokens.extract_token).
            gp_console.error(f"Token extraction failed: {redact_text(str(exc), option_secrets)}")
            gp_console.info("The token page could not be fetched; check the network and retry.")
            raise SystemExit(1) from exc
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
            _operating_session_name=operating_name,
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
                    url=exc.response.url,
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

        # Persist session if requested. operating_name is the name the load
        # resolved to (#182) — update_session_cookies uses it literally, so
        # keying this off the raw pin would drop the refresh.
        if (cmd_spec.saves_session or cmd_ctx._session_dirty) and needs_session:
            update_session_cookies(session, operating_name)

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
        gp_console.error(redact_text(exc.user_message, option_secrets))
        raise SystemExit(1) from exc
    except PluginError as exc:
        gp_console.error(redact_text(f"Plugin error: {exc}", option_secrets))
        raise SystemExit(1) from exc
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        error = redact_text(str(exc), option_secrets)
        # The traceback is kept: under log_secrets the logger formats it into
        # ``exception`` and scrubs it (graftpunk.logging.log_secrets).
        LOG.exception(
            "plugin_command_failed",
            plugin=plugin.site_name,
            command=cmd_spec.name,
            error=error,
            exc_type=type(exc).__name__,
        )
        gp_console.error(f"Command failed: {error}")
        raise SystemExit(1) from exc
