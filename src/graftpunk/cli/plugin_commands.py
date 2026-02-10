"""Dynamic CLI command registration for plugins.

This module discovers and registers plugin commands with Typer at startup.
Python plugins (via entry points), Python file plugins (from the plugins
directory), and YAML plugins are all supported.
"""

from __future__ import annotations

import atexit
from dataclasses import dataclass, field
from typing import Any, Literal

import click
import requests
import typer
import typer.core
import typer.main
from rich.console import Console

from graftpunk import console as gp_console
from graftpunk.cache import update_session_cookies
from graftpunk.cli.login_commands import (
    create_login_command,
    resolve_login_callable,
    resolve_login_fields,
)
from graftpunk.exceptions import BrowserError, CommandError, PluginError, SessionNotFoundError
from graftpunk.logging import get_logger
from graftpunk.observe import build_observe_context
from graftpunk.plugins import discover_all_plugins
from graftpunk.plugins.cli_plugin import (
    CLIPluginProtocol,
    CommandContext,
    CommandSpec,
)
from graftpunk.plugins.formatters import discover_formatters, format_output

LOG = get_logger(__name__)
_format_console = Console()

_registered_plugins_for_teardown: list[CLIPluginProtocol] = []


def _teardown_all_plugins() -> None:
    """Call teardown() on all registered plugins in reverse order.

    Exceptions are logged but do not propagate — every plugin gets
    its teardown called regardless of individual failures.
    """
    for plugin in reversed(_registered_plugins_for_teardown):
        try:
            plugin.teardown()
        except Exception as exc:  # noqa: BLE001
            LOG.error("plugin_teardown_failed", plugin=plugin.site_name, error=str(exc))


atexit.register(_teardown_all_plugins)


class GraftpunkApp(typer.Typer):
    """Extended Typer application with plugin command support.

    Manages dynamically-registered plugin Click groups and injects them
    into the Click group that Typer builds on each invocation.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._plugin_groups: dict[str, click.Group] = {}

    def add_plugin_group(self, name: str, group: click.Group) -> None:
        """Register a plugin command group for injection at runtime.

        Raises:
            ValueError: If a group with this name is already registered.
        """
        if name in self._plugin_groups:
            raise ValueError(f"Plugin group '{name}' is already registered")
        self._plugin_groups[name] = group

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Build the Click group, inject plugin commands, then run."""
        click_app = typer.main.get_command(self)
        if isinstance(click_app, click.Group):
            for name, group in self._plugin_groups.items():
                click_app.add_command(group, name=name)
        try:
            return click_app.main(standalone_mode=False)
        except click.UsageError as exc:
            gp_console.error(str(exc))
            raise SystemExit(2) from None


# Map site_name → session_name for alias resolution in core commands
_plugin_session_map: dict[str, str] = {}

# Track registered plugin sources for collision detection
_registered_plugin_sources: dict[str, str] = {}  # site_name → source description


@dataclass(frozen=True)
class PluginDiscoveryError:
    """Error encountered during plugin discovery or registration.

    Attributes:
        plugin_name: Name of the plugin that failed, or a placeholder like
            "python-plugins" or "yaml-plugins" for batch discovery failures.
        error: Human-readable error message.
        phase: Stage where the error occurred. One of:
            - "discovery": Failed to discover plugins from entry points or YAML files.
            - "instantiation": Plugin class found but constructor raised an error.
            - "registration": Plugin instantiated but command registration failed.
    """

    plugin_name: str
    error: str
    phase: Literal["discovery", "instantiation", "registration"]


@dataclass
class PluginDiscoveryResult:
    """Result of plugin discovery containing registered plugins and errors.

    Attributes:
        registered: Dictionary mapping plugin site_name to help_text for
            successfully registered plugins.
        errors: List of errors encountered during discovery/registration.
    """

    registered: dict[str, str] = field(default_factory=dict)
    errors: list[PluginDiscoveryError] = field(default_factory=list)

    def add_error(
        self,
        plugin_name: str,
        error: str,
        phase: Literal["discovery", "instantiation", "registration"],
    ) -> None:
        """Add an error to the result."""
        self.errors.append(PluginDiscoveryError(plugin_name, error, phase))

    @property
    def has_errors(self) -> bool:
        """Return True if any errors occurred."""
        return bool(self.errors)


def _create_plugin_command(
    plugin: CLIPluginProtocol,
    cmd_spec: CommandSpec,
) -> typer.core.TyperCommand:
    """Create a TyperCommand from a CommandSpec.

    Builds Click parameters from the spec's ``PluginParamSpec`` list
    (splatting each param's ``click_kwargs`` into ``click.Option()`` or
    ``click.Argument()``), wires up the execution callback, and returns
    a ``TyperCommand`` ready for group registration.

    Args:
        plugin: Plugin instance that owns the command.
        cmd_spec: Command specification with handler and params.

    Returns:
        TyperCommand ready for registration in a plugin group.
    """
    params: list[click.Parameter] = []

    positional_params = [p for p in cmd_spec.params if not p.is_option]
    option_params = [p for p in cmd_spec.params if p.is_option]

    for param in positional_params:
        try:
            params.append(click.Argument([param.name], **param.click_kwargs))
        except TypeError as exc:
            raise PluginError(
                f"Invalid click_kwargs for argument '{param.name}': {exc}. "
                f"Received kwargs: {param.click_kwargs}"
            ) from exc

    for param in option_params:
        option_name = f"--{param.name.replace('_', '-')}"
        try:
            params.append(click.Option([option_name], **param.click_kwargs))
        except TypeError as exc:
            raise PluginError(
                f"Invalid click_kwargs for option '{param.name}': {exc}. "
                f"Received kwargs: {param.click_kwargs}"
            ) from exc

    available_formats = list(discover_formatters().keys())
    params.append(
        click.Option(
            ["--format", "-f"],
            type=click.Choice(available_formats),
            default="json",
            help=f"Output format: {', '.join(available_formats)}",
        )
    )

    def callback(**kwargs: Any) -> None:
        from graftpunk.client import execute_plugin_command

        output_format = kwargs.pop("format", "json")
        click_ctx = click.get_current_context(silent=True)
        # Only COMMANDLINE counts as explicit — DEFAULT and DEFAULT_MAP
        # mean the user did not actively choose, so the hint should apply.
        format_is_explicit = (
            click_ctx is not None
            and click_ctx.get_parameter_source("format") == click.core.ParameterSource.COMMANDLINE
        )

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
        observe_mode: Literal["off", "full"] = "off"
        if click_ctx is not None:
            parent = click_ctx.find_root()
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
            ctx = CommandContext(
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
                    ctx,
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
                    ctx._session_dirty = True
                    result = execute_plugin_command(
                        cmd_spec,
                        ctx,
                        **kwargs,
                    )
                else:
                    raise

            # Persist session if requested
            if (cmd_spec.saves_session or ctx._session_dirty) and needs_session:
                update_session_cookies(session, plugin.session_name)

            format_output(
                result,
                output_format,
                _format_console,
                user_explicit=format_is_explicit,
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

    cmd_kw = dict(cmd_spec.click_kwargs)
    cmd_kw.setdefault("help", f"Run {cmd_spec.name} command")
    return typer.core.TyperCommand(
        name=cmd_spec.name,
        callback=callback,
        params=params,
        **cmd_kw,
    )


def _notify_plugin_errors(result: PluginDiscoveryResult) -> None:
    """Print plugin discovery errors to stderr for user visibility.

    Only shows errors if there are any. Output is limited to the first 3 errors
    to avoid overwhelming the user; additional errors are summarized with a count.
    """
    if not result.has_errors:
        return

    gp_console.warn(f"{len(result.errors)} plugin(s) failed to load")
    for error in result.errors[:3]:
        gp_console.info(f"{error.plugin_name}: {error.error}")
    if len(result.errors) > 3:
        gp_console.info(f"... and {len(result.errors) - 3} more")


def _get_plugin_source(plugin: CLIPluginProtocol) -> str:
    """Determine a human-readable source description for a plugin.

    Returns a string like ``"entry_point:module.name"``,
    ``"yaml:/path/to/file.yaml"``, ``"file:/path/to/plugin.py"``,
    or ``"unknown:ClassName"`` as a fallback.

    Args:
        plugin: The plugin instance to describe.

    Returns:
        A source description string.
    """
    # YAML plugins store the source filepath
    source_file = getattr(plugin, "_source_file", None)
    if source_file is not None:
        return f"yaml:{source_file}"

    # Python file plugins store the source filepath
    plugin_file = getattr(plugin, "_plugin_file", None)
    if plugin_file is not None:
        return f"file:{plugin_file}"

    # Entry point plugins: use the module name
    module = getattr(type(plugin), "__module__", None)
    if module and not module.startswith("graftpunk.plugins.yaml"):
        return f"entry_point:{module}"

    return f"unknown:{type(plugin).__name__}"


def _ensure_group_hierarchy(parent_group: click.Group, dotted_path: str) -> click.Group:
    """Create nested Click groups for a dotted path like 'accounts.statements'.

    Walks each segment of the dotted path, creating Click groups as needed.
    If a segment already exists as a Group, it is reused. If a non-group
    command exists with the same name, a warning is logged and traversal stops.

    Args:
        parent_group: The top-level Click group to nest under.
        dotted_path: Dot-separated group path (e.g. "accounts.statements").

    Returns:
        The innermost Click group for the path.
    """
    current = parent_group
    for part in dotted_path.split("."):
        existing = current.commands.get(part)
        if existing is None:
            # Use TyperGroup (not click.Group) so nested subcommand help
            # output gets the same rich formatting as top-level groups.
            new_group = typer.core.TyperGroup(
                name=part,
                no_args_is_help=True,
                rich_markup_mode="rich",
                context_settings={"help_option_names": ["-h", "--help"]},
            )
            current.add_command(new_group, name=part)
            current = new_group
        elif isinstance(existing, click.Group):
            current = existing
        else:
            # Conflict: a command already exists with this name
            LOG.warning("command_group_conflict", name=part, path=dotted_path)
            break
    return current


def register_plugin_commands(app: typer.Typer, *, notify_errors: bool = True) -> dict[str, str]:
    """Discover and register all plugin commands with a Typer app.

    Discovers Python plugins (via entry points), Python file plugins
    (from the plugins directory), and YAML plugins from the config directory.
    Failed plugins are logged and skipped.

    Args:
        app: Typer application to register commands with.
        notify_errors: If True, print errors to stderr for user visibility.

    Returns:
        Dictionary mapping plugin site_name to help_text.

    Raises:
        PluginError: If two plugins register the same site_name.
    """
    _registered_plugin_sources.clear()
    _plugin_session_map.clear()
    _registered_plugins_for_teardown.clear()
    result = PluginDiscoveryResult()

    # Use shared discovery (clear cache so CLI always gets fresh results)
    discover_all_plugins.cache_clear()
    all_plugins = discover_all_plugins()

    # Register each plugin (already filtered for config errors,
    # missing site_name, and unsupported api_version)
    for plugin in all_plugins:
        try:
            site_name = plugin.site_name

            # Collision detection: fail fast if two plugins share a site_name
            source = _get_plugin_source(plugin)
            if site_name in _registered_plugin_sources:
                existing_source = _registered_plugin_sources[site_name]
                raise PluginError(
                    f"Plugin name collision: '{site_name}' is already registered "
                    f"by {existing_source}. New source: {source}. "
                    f"Rename one of the plugins."
                )

            # Use TyperGroup instead of plain click.Group so plugin help
            # output uses the same rich formatting as the main app.
            plugin_group = typer.core.TyperGroup(
                name=site_name,
                help=plugin.help_text or f"Commands for {site_name}",
                no_args_is_help=True,
                rich_markup_mode="rich",
                context_settings={"help_option_names": ["-h", "--help"]},
            )

            # Add commands to the group
            command_list = plugin.get_commands()
            for cmd_spec in command_list:
                try:
                    click_cmd = _create_plugin_command(plugin, cmd_spec)
                    if cmd_spec.group is None:
                        # Top-level command
                        plugin_group.add_command(click_cmd, name=cmd_spec.name)
                    else:
                        # Grouped command -- ensure parent groups exist
                        group = _ensure_group_hierarchy(plugin_group, cmd_spec.group)
                        group.add_command(click_cmd, name=cmd_spec.name)
                    LOG.debug(
                        "command_registered",
                        plugin=site_name,
                        command=cmd_spec.name,
                        group=cmd_spec.group,
                    )
                except Exception as exc:  # noqa: BLE001 — plugin boundary: unknown plugin code may raise anything
                    LOG.warning(
                        "command_registration_failed",
                        plugin=site_name,
                        command=cmd_spec.name,
                        error=str(exc),
                    )
                    result.add_error(f"{site_name}.{cmd_spec.name}", str(exc), "registration")

            # Auto-register login command if plugin has login capability
            try:
                login_callable = resolve_login_callable(plugin)
                if login_callable is not None:
                    login_fields = resolve_login_fields(plugin)
                    login_cmd = create_login_command(plugin, login_callable, login_fields)
                    plugin_group.add_command(login_cmd, name="login")
                    LOG.debug("login_command_registered", plugin=site_name)
            except Exception as exc:  # noqa: BLE001 — plugin boundary: unknown plugin code may raise anything
                LOG.warning(
                    "login_command_registration_failed",
                    plugin=site_name,
                    exc_info=True,
                )
                result.add_error(f"{site_name}.login", str(exc), "registration")

            # Run plugin setup hook
            try:
                plugin.setup()
                LOG.debug("plugin_setup_complete", plugin=site_name)
            except Exception as exc:  # noqa: BLE001
                LOG.exception("plugin_setup_failed", plugin=site_name, error=str(exc))
                result.add_error(site_name, f"setup() failed: {exc}", "registration")
                continue  # Skip this plugin

            # Register group with the app (if it's a GraftpunkApp)
            _registered_plugin_sources[site_name] = source
            if isinstance(app, GraftpunkApp):
                app.add_plugin_group(site_name, plugin_group)
            _plugin_session_map[site_name] = plugin.session_name
            _registered_plugins_for_teardown.append(plugin)
            result.registered[site_name] = plugin.help_text
            LOG.info("plugin_registered", site_name=site_name, source=source)

        except PluginError:
            raise  # Re-raise collision errors immediately
        except Exception as exc:  # noqa: BLE001 — plugin boundary: unknown plugin code may raise anything
            LOG.warning("plugin_registration_failed", plugin=type(plugin).__name__, error=str(exc))
            result.add_error(type(plugin).__name__, str(exc), "registration")

    # Notify user of any errors
    if notify_errors:
        _notify_plugin_errors(result)

    return result.registered


def get_plugin_for_session(session_name: str) -> CLIPluginProtocol | None:
    """Look up the plugin instance that owns a given session name.

    Args:
        session_name: The session name to look up.

    Returns:
        The plugin instance, or None if no plugin owns this session.
    """
    for plugin in _registered_plugins_for_teardown:
        if _plugin_session_map.get(plugin.site_name) == session_name:
            return plugin
    return None


def resolve_session_name(name: str) -> str:
    """Resolve a name to a session name via plugin site_name mapping.

    If name matches a registered plugin's site_name, returns that plugin's
    session_name. Otherwise returns name unchanged (it may be a literal
    session name).
    """
    return _plugin_session_map.get(name, name)
