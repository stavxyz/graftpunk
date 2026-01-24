"""Dynamic CLI command registration for plugins.

This module discovers and registers plugin commands with Typer at startup.
Both Python plugins (via entry points) and YAML plugins are supported.
"""

from __future__ import annotations

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
console = Console()

# Store plugin Click groups for later registration
_plugin_groups: dict[str, click.Group] = {}


def _map_param_type(param_type: type) -> type[Any]:
    """Map Python types to Click-compatible types."""
    if param_type in (str, int, float, bool):
        return param_type
    return str


def _create_click_command(
    plugin: CLIPluginProtocol,
    cmd_spec: CommandSpec,
) -> click.Command:
    """Create a Click command from a CommandSpec."""
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
        except Exception as exc:
            LOG.error(
                "plugin_command_failed",
                plugin=plugin.site_name,
                command=cmd_spec.name,
                error=str(exc),
            )
            console.print(f"[red]Command failed: {exc}[/red]")
            raise SystemExit(1) from None

    return click.Command(
        name=cmd_spec.name,
        callback=callback,
        params=params,
        help=cmd_spec.help_text or f"Run {cmd_spec.name} command",
    )


def register_plugin_commands(app: typer.Typer) -> dict[str, str]:
    """Discover and register all plugin commands with a Typer app."""
    registered: dict[str, str] = {}
    all_plugins: list[CLIPluginProtocol] = []

    # Discover Python plugins
    try:
        python_plugins = discover_cli_plugins()
        for name, plugin_class in python_plugins.items():
            try:
                instance = plugin_class()
                all_plugins.append(instance)
                LOG.debug("python_plugin_discovered", name=name)
            except Exception as exc:
                LOG.warning("python_plugin_instantiation_failed", name=name, error=str(exc))
    except Exception as exc:
        LOG.warning("python_plugin_discovery_failed", error=str(exc))

    # Discover YAML plugins
    try:
        yaml_plugins = create_yaml_plugins()
        all_plugins.extend(yaml_plugins)
    except Exception as exc:
        LOG.warning("yaml_plugin_discovery_failed", error=str(exc))

    # Register each plugin
    for plugin in all_plugins:
        try:
            site_name = plugin.site_name
            if not site_name:
                LOG.warning("plugin_missing_site_name", plugin=type(plugin).__name__)
                continue

            if site_name in registered:
                LOG.warning("duplicate_plugin_site_name", site_name=site_name)
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

            # Store for later injection
            _plugin_groups[site_name] = plugin_group
            registered[site_name] = plugin.help_text
            LOG.info("plugin_registered", site_name=site_name)

        except Exception as exc:
            LOG.warning("plugin_registration_failed", plugin=type(plugin).__name__, error=str(exc))

    return registered


def inject_plugin_commands(click_group: click.Group) -> None:
    """Inject registered plugin commands into a Click group.

    This should be called after Typer creates its Click group.
    """
    for site_name, plugin_group in _plugin_groups.items():
        click_group.add_command(plugin_group, name=site_name)
