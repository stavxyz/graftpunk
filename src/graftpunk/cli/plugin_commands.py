"""Dynamic CLI command registration for plugins.

This module discovers and registers plugin commands with Typer at startup.
Both Python plugins (via entry points) and YAML plugins are supported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import click
import typer
from rich.console import Console

from graftpunk.exceptions import PluginError, SessionNotFoundError
from graftpunk.logging import get_logger
from graftpunk.plugins import discover_cli_plugins
from graftpunk.plugins.cli_plugin import CLIPluginProtocol, CommandSpec
from graftpunk.plugins.formatters import format_output
from graftpunk.plugins.yaml_plugin import YAMLSitePlugin, create_yaml_plugins

LOG = get_logger(__name__)
console = Console(stderr=True)

# Store plugin Click groups for later registration
_plugin_groups: dict[str, click.Group] = {}


@dataclass
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
    phase: str


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

    def add_error(self, plugin_name: str, error: str, phase: str) -> None:
        """Add an error to the result."""
        self.errors.append(PluginDiscoveryError(plugin_name, error, phase))

    @property
    def has_errors(self) -> bool:
        """Return True if any errors occurred."""
        return bool(self.errors)


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


def _create_click_command(
    plugin: CLIPluginProtocol,
    cmd_spec: CommandSpec,
) -> click.Command:
    """Create a Click command from a CommandSpec.

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

    params.append(
        click.Option(
            ["--format", "-f"],
            type=click.Choice(["json", "table", "raw"]),
            default="json",
            help="Output format: json, table, raw",
        )
    )

    def callback(**kwargs: Any) -> None:
        output_format = kwargs.pop("format", "json")

        requires_session = True
        if isinstance(plugin, YAMLSitePlugin):
            requires_session = plugin.requires_session

        try:
            if requires_session:
                session = plugin.get_session()
            else:
                import requests

                session = requests.Session()
        except SessionNotFoundError:
            console.print(
                f"[red]Session '{plugin.session_name}' not found.[/red]\n\n"
                f"Please create a session first."
            )
            raise SystemExit(1) from None
        except PluginError as exc:
            console.print(f"[red]Plugin error: {exc}[/red]")
            raise SystemExit(1) from None
        except Exception as exc:
            console.print(f"[red]Failed to load session: {exc}[/red]")
            LOG.exception("session_load_failed", plugin=plugin.site_name)
            raise SystemExit(1) from None

        try:
            result = cmd_spec.handler(session, **kwargs)
            format_output(result, output_format, console)
        except (SystemExit, KeyboardInterrupt):
            raise  # Let these propagate normally
        except Exception as exc:
            LOG.exception(
                "plugin_command_failed",
                plugin=plugin.site_name,
                command=cmd_spec.name,
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            console.print(f"[red]Command failed: {exc}[/red]")
            raise SystemExit(1) from None

    return click.Command(
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

    console.print(f"[yellow]Warning: {len(result.errors)} plugin(s) failed to load[/yellow]")
    for error in result.errors[:3]:
        console.print(f"  [dim]{error.plugin_name}: {error.error}[/dim]")
    if len(result.errors) > 3:
        console.print(f"  [dim]... and {len(result.errors) - 3} more[/dim]")


def register_plugin_commands(app: typer.Typer, *, notify_errors: bool = True) -> dict[str, str]:
    """Discover and register all plugin commands with a Typer app.

    Discovers both Python plugins (via entry points) and YAML plugins
    from the config directory. Failed plugins are logged and skipped.

    Args:
        app: Typer application to register commands with.
        notify_errors: If True, print errors to stderr for user visibility.

    Returns:
        Dictionary mapping plugin site_name to help_text.
    """
    result = PluginDiscoveryResult()
    all_plugins: list[CLIPluginProtocol] = []

    # Discover Python plugins
    try:
        python_plugins = discover_cli_plugins()
        for name, plugin_class in python_plugins.items():
            try:
                instance = plugin_class()
                all_plugins.append(instance)
                LOG.debug("python_plugin_discovered", name=name)
            except PluginError as exc:
                LOG.warning("python_plugin_instantiation_failed", name=name, error=str(exc))
                result.add_error(name, str(exc), "instantiation")
            except Exception as exc:
                LOG.exception(
                    "python_plugin_instantiation_unexpected",
                    name=name,
                    error=str(exc),
                    exc_type=type(exc).__name__,
                )
                result.add_error(name, str(exc), "instantiation")
    except Exception as exc:
        LOG.exception("python_plugin_discovery_failed", error=str(exc))
        result.add_error("python-plugins", str(exc), "discovery")

    # Discover YAML plugins
    try:
        yaml_plugins, yaml_errors = create_yaml_plugins()
        all_plugins.extend(yaml_plugins)
        # Aggregate any YAML file load errors
        for yaml_error in yaml_errors:
            result.add_error(
                str(yaml_error.filepath.name), yaml_error.error, "discovery"
            )
    except Exception as exc:
        LOG.exception("yaml_plugin_discovery_failed", error=str(exc))
        result.add_error("yaml-plugins", str(exc), "discovery")

    # Register each plugin
    for plugin in all_plugins:
        try:
            site_name = plugin.site_name
            if not site_name:
                LOG.warning("plugin_missing_site_name", plugin=type(plugin).__name__)
                result.add_error(type(plugin).__name__, "missing site_name", "registration")
                continue

            if site_name in result.registered:
                LOG.warning("duplicate_plugin_site_name", site_name=site_name)
                result.add_error(
                    site_name, "duplicate site_name (already registered)", "registration"
                )
                continue

            # Create Click group for this plugin
            plugin_group = click.Group(
                name=site_name,
                help=plugin.help_text or f"Commands for {site_name}",
            )

            # Add commands to the group
            commands = plugin.get_commands()
            for cmd_name, cmd_spec in commands.items():
                try:
                    click_cmd = _create_click_command(plugin, cmd_spec)
                    plugin_group.add_command(click_cmd, name=cmd_name)
                    LOG.debug("command_registered", plugin=site_name, command=cmd_name)
                except Exception as exc:
                    LOG.warning(
                        "command_registration_failed",
                        plugin=site_name,
                        command=cmd_name,
                        error=str(exc),
                    )
                    result.add_error(f"{site_name}.{cmd_name}", str(exc), "registration")

            # Store for later injection
            _plugin_groups[site_name] = plugin_group
            result.registered[site_name] = plugin.help_text
            LOG.info("plugin_registered", site_name=site_name)

        except Exception as exc:
            LOG.warning("plugin_registration_failed", plugin=type(plugin).__name__, error=str(exc))
            result.add_error(type(plugin).__name__, str(exc), "registration")

    # Notify user of any errors
    if notify_errors:
        _notify_plugin_errors(result)

    return result.registered


def inject_plugin_commands(click_group: click.Group) -> None:
    """Inject registered plugin commands into a Click group.

    This should be called after Typer creates its Click group,
    adding all previously registered plugin command groups.

    Args:
        click_group: Click group to add plugin commands to.
    """
    for site_name, plugin_group in _plugin_groups.items():
        click_group.add_command(plugin_group, name=site_name)
