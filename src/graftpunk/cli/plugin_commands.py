"""Dynamic CLI command registration for plugins.

This module discovers and registers plugin commands with Typer at startup.
Python plugins (via entry points), Python file plugins (from the plugins
directory), and YAML plugins are all supported.
"""

from __future__ import annotations

import asyncio
import atexit
import time
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
from graftpunk.plugins import discover_site_plugins
from graftpunk.plugins.cli_plugin import (
    SUPPORTED_API_VERSIONS,
    CLIPluginProtocol,
    CommandContext,
    CommandSpec,
)
from graftpunk.plugins.formatters import discover_formatters, format_output
from graftpunk.plugins.python_loader import discover_python_plugins
from graftpunk.plugins.yaml_plugin import create_yaml_plugins

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


_last_execution: dict[str, float] = {}


def _enforce_rate_limit(command_key: str, rate_limit: float) -> None:
    """Enforce a minimum interval between executions of a command.

    Sleeps if the command was executed too recently, ensuring at least
    ``rate_limit`` seconds between consecutive calls.

    Args:
        command_key: Unique key for the command (e.g. "plugin.command").
        rate_limit: Minimum seconds between executions.
    """
    now = time.monotonic()
    last = _last_execution.get(command_key)
    if last is not None:
        elapsed = now - last
        if elapsed < rate_limit:
            time.sleep(rate_limit - elapsed)
    _last_execution[command_key] = time.monotonic()


def _execute_with_limits(
    handler: Any, ctx: CommandContext, spec: CommandSpec, **kwargs: Any
) -> Any:
    """Execute a command handler with retry and rate-limit support.

    Retries the handler up to ``spec.max_retries`` times with exponential
    backoff on failure. Rate-limiting is enforced before each attempt.

    Args:
        handler: The command handler callable.
        ctx: CommandContext to pass to the handler.
        spec: CommandSpec with retry/rate-limit configuration.
        **kwargs: Additional keyword arguments for the handler.

    Returns:
        The handler's return value.

    Raises:
        Exception: The last exception if all attempts fail.
    """
    attempts = 1 + spec.max_retries
    last_exc: Exception | None = None
    command_key = f"{ctx.plugin_name}.{spec.name}"

    # Rate limiting applies on each attempt. During retries, the backoff sleep
    # already provides spacing; any additional rate-limit delay is conservative.
    for attempt in range(attempts):
        try:
            if spec.rate_limit:
                _enforce_rate_limit(command_key, spec.rate_limit)
            result = handler(ctx, **kwargs)
            if asyncio.iscoroutine(result):
                LOG.warning(
                    "async_handler_auto_executed",
                    command=spec.name,
                    plugin=ctx.plugin_name,
                    hint="Async handlers work but are not officially supported in v1. "
                    "Consider using a synchronous handler.",
                )
                result = asyncio.run(result)
            return result
        except (requests.RequestException, ConnectionError, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt < attempts - 1:
                backoff = 2**attempt
                LOG.warning(
                    "command_retry",
                    command=spec.name,
                    attempt=attempt + 1,
                    backoff=backoff,
                )
                gp_console.warn(
                    f"Command '{spec.name}' failed ({exc}), "
                    f"retrying in {backoff}s ({attempt + 1}/{spec.max_retries})..."
                )
                time.sleep(backoff)

    # last_exc is always set: attempts >= 1 (validated in CommandSpec.__post_init__)
    assert last_exc is not None  # for type narrowing
    raise last_exc


def _map_param_type(param_type: type) -> type[Any]:
    """Map Python types to Click-compatible types.

    Args:
        param_type: Python type to map.

    Returns:
        Click-compatible type (str, int, float, bool), defaulting to str.
    """
    if param_type in (str, int, float, bool):
        return param_type
    return str


def _create_plugin_command(
    plugin: CLIPluginProtocol,
    cmd_spec: CommandSpec,
) -> typer.core.TyperCommand:
    """Create a Typer command from a CommandSpec.

    Args:
        plugin: Plugin instance that owns the command.
        cmd_spec: Command specification with handler and params.

    Returns:
        Click Command ready for registration.
    """
    params: list[click.Parameter] = []

    positional_params = [p for p in cmd_spec.params if not p.is_option]
    option_params = [p for p in cmd_spec.params if p.is_option]

    for param in positional_params:
        click_type = _map_param_type(param.param_type)
        params.append(
            click.Argument(
                [param.name],
                type=click_type,
                required=param.required,
                default=param.default,
            )
        )

    for param in option_params:
        click_type = _map_param_type(param.param_type)
        option_name = f"--{param.name.replace('_', '-')}"
        params.append(
            click.Option(
                [option_name],
                type=click_type,
                required=param.required,
                default=param.default,
                help=param.help_text,
            )
        )

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
        output_format = kwargs.pop("format", "json")

        # Per-command requires_session override
        needs_session = (
            cmd_spec.requires_session
            if cmd_spec.requires_session is not None
            else plugin.requires_session
        )

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
        except Exception as exc:  # noqa: BLE001 — CLI boundary: present user-friendly error instead of traceback
            gp_console.error(f"Failed to load session: {exc}")
            LOG.exception("session_load_failed", plugin=plugin.site_name)
            raise SystemExit(1) from exc

        # Set gp_base_url on the session so xhr()/navigate()/form_submit()
        # can resolve relative Referer paths.
        base_url = getattr(plugin, "base_url", "")
        if base_url and hasattr(session, "gp_base_url"):
            setattr(session, "gp_base_url", base_url)  # noqa: B010 — avoids ty error on requests.Session

        # Build observability context from CLI --observe flag
        click_ctx = click.get_current_context(silent=True)
        observe_mode: Literal["off", "full"] = "off"
        if click_ctx is not None:
            parent = click_ctx.find_root()
            observe_mode = (parent.obj or {}).get("observe_mode", "off")

        backend_type = plugin.backend
        try:
            driver = session.driver  # type: ignore[attr-defined]
        except (BrowserError, AttributeError):
            driver = None
            LOG.debug("driver_not_available_for_observe", plugin=plugin.site_name)
        observe_ctx = build_observe_context(
            plugin.site_name,
            backend_type,
            driver,
            observe_mode,
        )
        if observe_mode != "off" and driver is None:
            gp_console.warn(
                f"Observability capture unavailable for '{plugin.site_name}': "
                f"no browser driver. Only event logging will work."
            )

        # Auto-inject tokens if plugin declares token_config
        token_config = getattr(plugin, "token_config", None)
        if token_config is not None and needs_session:
            from graftpunk.tokens import prepare_session as _prepare_tokens

            base_url = getattr(plugin, "base_url", "")
            try:
                _prepare_tokens(session, token_config, base_url)
            except ValueError as exc:
                gp_console.error(f"Token extraction failed: {exc}")
                raise SystemExit(1) from exc

        try:
            ctx = CommandContext(
                session=session,
                plugin_name=plugin.site_name,
                command_name=cmd_spec.name,
                api_version=plugin.api_version,
                base_url=getattr(plugin, "base_url", ""),
                config=getattr(plugin, "_plugin_config", None),
                observe=observe_ctx,
                _session_name=plugin.session_name if needs_session else "",
            )

            try:
                result = _execute_with_limits(cmd_spec.handler, ctx, cmd_spec, **kwargs)
            except requests.exceptions.HTTPError as exc:
                if (
                    exc.response is not None
                    and exc.response.status_code == 403
                    and token_config is not None
                ):
                    from graftpunk.tokens import clear_cached_tokens
                    from graftpunk.tokens import prepare_session as _prep

                    LOG.info(
                        "token_403_retry",
                        command=cmd_spec.name,
                        url=exc.response.url if exc.response else "unknown",
                    )
                    clear_cached_tokens(session)
                    _prep(session, token_config, getattr(plugin, "base_url", ""))
                    # Mark session dirty so update_session_cookies() is called on exit,
                    # persisting the freshly re-extracted token cache for future commands.
                    ctx._session_dirty = True
                    result = _execute_with_limits(cmd_spec.handler, ctx, cmd_spec, **kwargs)
                else:
                    raise

            # Persist session if requested (decorator or explicit ctx.save_session())
            if (cmd_spec.saves_session or ctx._session_dirty) and needs_session:
                update_session_cookies(session, plugin.session_name)

            format_output(result, output_format, _format_console)
        except (SystemExit, KeyboardInterrupt):
            raise  # Let these propagate normally
        except CommandError as exc:
            gp_console.error(exc.user_message)
            raise SystemExit(1) from exc
        except PluginError as exc:
            gp_console.error(f"Plugin error: {exc}")
            raise SystemExit(1) from exc
        except Exception as exc:  # noqa: BLE001 — CLI boundary: present user-friendly error instead of traceback
            LOG.exception(
                "plugin_command_failed",
                plugin=plugin.site_name,
                command=cmd_spec.name,
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            gp_console.error(f"Command failed: {exc}")
            raise SystemExit(1) from exc

    return typer.core.TyperCommand(
        name=cmd_spec.name,
        callback=callback,
        params=params,
        help=cmd_spec.help_text or f"Run {cmd_spec.name} command",
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
    all_plugins: list[CLIPluginProtocol] = []

    # Discover Python plugins
    try:
        python_plugins = discover_site_plugins()
        for name, plugin_class in python_plugins.items():
            try:
                instance = plugin_class()
                all_plugins.append(instance)
                LOG.debug("python_plugin_discovered", name=name)
            except PluginError as exc:
                LOG.warning("python_plugin_instantiation_failed", name=name, error=str(exc))
                result.add_error(name, str(exc), "instantiation")
            except Exception as exc:  # noqa: BLE001 — plugin boundary: unknown plugin code may raise anything
                LOG.exception(
                    "python_plugin_instantiation_unexpected",
                    name=name,
                    error=str(exc),
                    exc_type=type(exc).__name__,
                )
                result.add_error(name, str(exc), "instantiation")
    except Exception as exc:  # noqa: BLE001 — plugin boundary: unknown plugin code may raise anything
        LOG.exception("python_plugin_discovery_failed", error=str(exc))
        result.add_error("python-plugins", str(exc), "discovery")

    # Discover YAML plugins
    try:
        yaml_plugins, yaml_errors = create_yaml_plugins()
        all_plugins.extend(yaml_plugins)
        # Aggregate any YAML file load errors
        for yaml_error in yaml_errors:
            result.add_error(str(yaml_error.filepath.name), yaml_error.error, "discovery")
    except Exception as exc:  # noqa: BLE001 — plugin boundary: unknown plugin code may raise anything
        LOG.exception("yaml_plugin_discovery_failed", error=str(exc))
        result.add_error("yaml-plugins", str(exc), "discovery")

    # Discover Python plugins from files
    try:
        python_file_result = discover_python_plugins()
        all_plugins.extend(python_file_result.plugins)
        # Aggregate any Python file load errors
        for py_error in python_file_result.errors:
            result.add_error(str(py_error.filepath.name), py_error.error, "discovery")
    except Exception as exc:  # noqa: BLE001 — plugin boundary: unknown plugin code may raise anything
        LOG.exception("python_file_plugin_discovery_failed", error=str(exc))
        result.add_error("python-file-plugins", str(exc), "discovery")

    # Register each plugin
    for plugin in all_plugins:
        try:
            # Skip plugins that failed config validation during __init_subclass__
            config_error = getattr(plugin, "_plugin_config_error", None)
            if config_error:
                LOG.warning(
                    "plugin_config_error",
                    plugin=type(plugin).__name__,
                    error=str(config_error),
                )
                result.add_error(type(plugin).__name__, str(config_error), "registration")
                continue

            site_name = plugin.site_name
            if not site_name:
                LOG.warning("plugin_missing_site_name", plugin=type(plugin).__name__)
                result.add_error(type(plugin).__name__, "missing site_name", "registration")
                continue

            # Collision detection: fail fast if two plugins share a site_name
            source = _get_plugin_source(plugin)
            if site_name in _registered_plugin_sources:
                existing_source = _registered_plugin_sources[site_name]
                raise PluginError(
                    f"Plugin name collision: '{site_name}' is already registered "
                    f"by {existing_source}. New source: {source}. "
                    f"Rename one of the plugins."
                )

            # API version check: reject plugins targeting unsupported versions
            if plugin.api_version not in SUPPORTED_API_VERSIONS:
                result.add_error(
                    site_name,
                    f"Plugin requires api_version {plugin.api_version}, "
                    f"but graftpunk supports {sorted(SUPPORTED_API_VERSIONS)}. "
                    f"Upgrade graftpunk or downgrade the plugin.",
                    "registration",
                )
                continue

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
