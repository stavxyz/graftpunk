"""Dynamic CLI command registration for plugins.

This module discovers and registers plugin commands with Typer at startup.
Python plugins (via entry points), Python file plugins (from the plugins
directory), and YAML plugins are all supported.
"""

from __future__ import annotations

import atexit
from dataclasses import dataclass, field
from typing import Any, Literal

import typer

from graftpunk import console as gp_console
from graftpunk.cli.login_commands import (
    resolve_login_callable,
    resolve_login_fields,
)
from graftpunk.exceptions import PluginError
from graftpunk.logging import get_logger
from graftpunk.plugins import discover_all_plugins
from graftpunk.plugins.cli_plugin import (
    CLIPluginProtocol,
    CommandSpec,
)

LOG = get_logger(__name__)

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


# Command-level click_kwargs contract (same closed, fail-loud policy as the
# param-level contract in command_factory.OPTION_KEYS/ARGUMENT_KEYS).
COMMAND_KEYS = frozenset({"help", "short_help", "hidden", "deprecated", "epilog"})


def _build_site_app(plugin: CLIPluginProtocol, result: PluginDiscoveryResult) -> typer.Typer:
    """Build one plugin's command tree as a Typer sub-app.

    Leaf commands are factory-synthesized functions whose body closes over
    (plugin, cmd_spec) and delegates to run_plugin_command. Dotted
    ``cmd_spec.group`` paths become nested Typer sub-apps.

    Error policy (single owner -- consumed verbatim by this task's tests):
    ``PluginError`` means a plugin-author CONTRACT VIOLATION (group/command
    name collision, duplicate command, unsupported click_kwargs) and ESCAPES
    this function; register_plugin_commands converts it into a whole-plugin
    skip plus a user-visible error. Only unexpected non-PluginError failures
    are contained per-command. (Previously: a command_group_conflict warning
    that silently mangled the group.)
    """
    from graftpunk.cli.command_factory import synthesize_command_fn
    from graftpunk.cli.plugin_runtime import run_plugin_command

    site_app = typer.Typer(
        name=plugin.site_name,
        help=plugin.help_text or f"Commands for {plugin.site_name}",
        no_args_is_help=True,
    )
    # dotted-path -> sub-app registry ("" is the site root)
    group_apps: dict[str, typer.Typer] = {"": site_app}
    registered_names: dict[str, set[str]] = {"": set()}

    def _group_app(dotted: str) -> typer.Typer:
        if dotted in group_apps:
            return group_apps[dotted]
        parent_path, _, segment = dotted.rpartition(".")
        parent = _group_app(parent_path)
        if segment in registered_names[parent_path]:
            raise PluginError(
                f"plugin '{plugin.site_name}': group '{dotted}' collides with an "
                f"existing command named '{segment}'"
            )
        sub = typer.Typer(name=segment, no_args_is_help=True)
        parent.add_typer(sub, name=segment)
        group_apps[dotted] = sub
        registered_names[parent_path].add(segment)
        registered_names[dotted] = set()
        return sub

    def _make_body(spec: CommandSpec):
        def body(ctx: typer.Context, **kwargs: Any) -> None:
            run_plugin_command(plugin, spec, ctx, **kwargs)

        return body

    for cmd_spec in plugin.get_commands():
        try:
            target = _group_app(cmd_spec.group or "")
            group_path = cmd_spec.group or ""
            if cmd_spec.name in registered_names[group_path]:
                raise PluginError(
                    f"plugin '{plugin.site_name}': duplicate command "
                    f"'{cmd_spec.name}' in group '{group_path or '<root>'}'"
                )
            cmd_kw = dict(cmd_spec.click_kwargs)
            unsupported = set(cmd_kw) - COMMAND_KEYS
            if unsupported:
                raise PluginError(
                    f"plugin '{plugin.site_name}', command '{cmd_spec.name}': "
                    f"unsupported command-level click_kwargs key(s): "
                    f"{', '.join(sorted(unsupported))}. "
                    f"Supported: {', '.join(sorted(COMMAND_KEYS))}."
                )
            help_text = cmd_kw.pop("help", f"Run {cmd_spec.name} command")
            fn = synthesize_command_fn(
                name=cmd_spec.name,
                param_specs=list(cmd_spec.params),
                body=_make_body(cmd_spec),
                plugin_name=plugin.site_name,
                help_text=help_text,
            )
            target.command(name=cmd_spec.name, help=help_text, **cmd_kw)(fn)
            registered_names[group_path].add(cmd_spec.name)
            LOG.debug(
                "command_registered",
                plugin=plugin.site_name,
                command=cmd_spec.name,
                group=cmd_spec.group,
            )
        except PluginError:
            raise  # contract violation -- loud, escapes (see docstring policy)
        except Exception as exc:  # noqa: BLE001 — plugin boundary (unexpected plugin defects)
            LOG.warning(
                "command_registration_failed",
                plugin=plugin.site_name,
                command=cmd_spec.name,
                error=str(exc),
            )
            result.add_error(f"{plugin.site_name}.{cmd_spec.name}", str(exc), "registration")

    # Auto-register login command if plugin has login capability
    try:
        login_callable = resolve_login_callable(plugin)
        if login_callable is not None:
            from graftpunk.cli.login_commands import create_login_fn

            if "login" in registered_names[""]:
                raise PluginError(
                    f"plugin '{plugin.site_name}': login command collides with an "
                    f"existing root command named 'login'"
                )
            login_fields = resolve_login_fields(plugin)
            login_fn = create_login_fn(plugin, login_callable, login_fields)
            site_app.command(name="login", help=login_fn.__doc__)(login_fn)
            registered_names[""].add("login")
            LOG.debug("login_command_registered", plugin=plugin.site_name)
    except PluginError:
        raise  # contract violation -- loud, escapes (see docstring policy)
    except Exception as exc:  # noqa: BLE001 — plugin boundary
        LOG.warning(
            "login_command_registration_failed",
            plugin=plugin.site_name,
            exc_info=True,
        )
        result.add_error(f"{plugin.site_name}.login", str(exc), "registration")

    return site_app


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

            try:
                site_app = _build_site_app(plugin, result)
            except PluginError as exc:
                # Contract violation inside this plugin: skip the WHOLE plugin
                # loudly (recorded + printed via _notify_plugin_errors) rather
                # than mounting a silently-mangled command tree. The site-name
                # collision PluginError raised earlier in this loop body still
                # propagates via the outer `except PluginError: raise`.
                LOG.warning("plugin_contract_violation", plugin=site_name, error=str(exc))
                result.add_error(site_name, str(exc), "registration")
                continue

            # Run plugin setup hook
            try:
                plugin.setup()
                LOG.debug("plugin_setup_complete", plugin=site_name)
            except Exception as exc:  # noqa: BLE001
                LOG.exception("plugin_setup_failed", plugin=site_name, error=str(exc))
                result.add_error(site_name, f"setup() failed: {exc}", "registration")
                continue  # Skip this plugin

            _registered_plugin_sources[site_name] = source
            app.add_typer(site_app, name=site_name)
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
