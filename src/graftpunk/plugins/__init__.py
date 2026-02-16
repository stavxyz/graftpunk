"""Plugin system for graftpunk using entry points.

This module provides discovery and loading of plugins registered via
Python entry points. Plugins can provide:

- Storage backends (graftpunk.storage)
- Keepalive handlers (graftpunk.keepalive_handlers)
- Site plugins (graftpunk.plugins)

Example plugin registration in pyproject.toml:
    [project.entry-points."graftpunk.keepalive_handlers"]
    mysite = "mypackage.handler:MySiteHandler"

    [project.entry-points."graftpunk.plugins"]
    mysite = "mypackage.cli:MySiteCommands"
"""

import warnings
from functools import lru_cache
from importlib.metadata import entry_points
from typing import Any
from urllib.parse import urlparse

from graftpunk.exceptions import CommandError, PluginError
from graftpunk.logging import get_logger
from graftpunk.plugins.cli_plugin import (
    SUPPORTED_API_VERSIONS,
    CLIPluginProtocol,
    CommandContext,
    CommandGroupMeta,
    CommandMetadata,
    CommandResult,
    CommandSpec,
    LoginConfig,
    LoginStep,
    PluginConfig,
    PluginParamSpec,
    SitePlugin,
    build_plugin_config,
    command,
)
from graftpunk.plugins.export import (
    flatten_dict,
    get_downloads_dir,
    json_to_csv,
    json_to_pdf,
    ordered_keys,
)
from graftpunk.plugins.formatters import (
    OutputFormatter,
    discover_formatters,
)
from graftpunk.plugins.output_config import (
    ColumnDisplayConfig,
    ColumnFilter,
    OutputConfig,
    ViewConfig,
    apply_column_filter,
    auto_detect_columns,
    extract_view_data,
    parse_view_arg,
)
from graftpunk.plugins.python_loader import (
    PythonDiscoveryError,
    PythonDiscoveryResult,
    discover_python_plugins,
)
from graftpunk.plugins.yaml_plugin import create_yaml_plugins
from graftpunk.tokens import Token, TokenConfig

__all__ = [
    # Base classes and decorators
    "SitePlugin",
    "command",
    "CLIPluginProtocol",
    "CommandContext",
    "CommandGroupMeta",
    "CommandMetadata",
    "CommandResult",
    "CommandSpec",
    "PluginParamSpec",
    # Configuration
    "LoginConfig",
    "LoginStep",
    "PluginConfig",
    "Token",
    "TokenConfig",
    "build_plugin_config",
    # Constants
    "SUPPORTED_API_VERSIONS",
    # Formatters
    "OutputFormatter",
    "discover_formatters",
    # Output configuration
    "ColumnDisplayConfig",
    "ColumnFilter",
    "OutputConfig",
    "ViewConfig",
    "apply_column_filter",
    "auto_detect_columns",
    "extract_view_data",
    "parse_view_arg",
    # Export utilities
    "flatten_dict",
    "get_downloads_dir",
    "json_to_csv",
    "json_to_pdf",
    "ordered_keys",
    # Exceptions
    "CommandError",
    # Utilities
    "infer_site_name",
    # Discovery functions
    "discover_plugins",
    "discover_storage_backends",
    "discover_keepalive_handlers",
    "discover_site_plugins",
    "get_keepalive_handler",
    "get_storage_backend",
    "load_handler_from_string",
    "list_available_plugins",
    # Python plugin discovery
    "PythonDiscoveryError",
    "PythonDiscoveryResult",
    "discover_python_plugins",
    # Shared plugin discovery (all sources)
    "discover_all_plugins",
    "get_plugin",
]

LOG = get_logger(__name__)


def infer_site_name(url: str) -> str:
    """Infer a site name from a URL by extracting the base domain name.

    Strips common prefixes (www, api, app, m) and the TLD to produce
    a short, CLI-friendly name.

    Args:
        url: Full URL like "https://httpbin.org" or bare domain like "httpbin.org".

    Returns:
        Simplified site name (e.g. "httpbin"), or empty string if
        no name can be inferred.
    """
    # Parse the URL to extract the hostname
    parsed = urlparse(url)
    hostname = parsed.hostname or parsed.path.split("/")[0]
    if not hostname:
        return ""

    name = hostname.lower()

    # Strip common subdomain prefixes
    for prefix in ("www.", "api.", "app.", "m."):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break

    # Take main domain name (second-to-last part before TLD)
    parts = name.split(".")
    if len(parts) >= 2:
        name = parts[-2]

    # Make CLI-friendly
    name = name.replace("-", "_")
    return name


# Entry point group names
STORAGE_GROUP = "graftpunk.storage"
KEEPALIVE_HANDLERS_GROUP = "graftpunk.keepalive_handlers"
PLUGINS_GROUP = "graftpunk.plugins"


def discover_plugins(group: str) -> dict[str, Any]:
    """Discover all installed plugins in a group via entry points.

    Args:
        group: Entry point group name (e.g., "graftpunk.keepalive_handlers").

    Returns:
        Dictionary mapping plugin name to loaded plugin class/object.

    Example:
        >>> handlers = discover_plugins("graftpunk.keepalive_handlers")
        >>> if "mysite" in handlers:
        ...     handler = handlers["mysite"]()
    """
    plugins: dict[str, Any] = {}
    eps = entry_points(group=group)

    for ep in eps:
        try:
            plugin = ep.load()
            plugins[ep.name] = plugin
            LOG.debug("plugin_loaded", group=group, name=ep.name)
        except Exception as exc:
            LOG.exception(
                "plugin_load_failed",
                group=group,
                name=ep.name,
                error=str(exc),
            )
            warnings.warn(
                f"Plugin '{ep.name}' failed to load: {exc}",
                UserWarning,
                stacklevel=2,
            )

    LOG.info("plugins_discovered", group=group, count=len(plugins))
    return plugins


def discover_storage_backends() -> dict[str, type]:
    """Discover all installed storage backends.

    Returns:
        Dictionary mapping backend name to storage class.
    """
    return discover_plugins(STORAGE_GROUP)


def discover_keepalive_handlers() -> dict[str, type]:
    """Discover all installed keepalive handlers.

    Returns:
        Dictionary mapping handler name to handler class.
    """
    return discover_plugins(KEEPALIVE_HANDLERS_GROUP)


def discover_site_plugins() -> dict[str, type]:
    """Discover all installed site plugins (authentication and CLI command plugins).

    Returns:
        Dictionary mapping plugin name to plugin class.
    """
    return discover_plugins(PLUGINS_GROUP)


def get_keepalive_handler(name: str) -> Any | None:
    """Get a specific keepalive handler by name.

    Args:
        name: Handler name (as registered in entry points).

    Returns:
        Handler class, or None if not found.
    """
    handlers = discover_keepalive_handlers()
    return handlers.get(name)


def get_storage_backend(name: str) -> type | None:
    """Get a specific storage backend by name.

    Args:
        name: Backend name (as registered in entry points).

    Returns:
        Storage backend class, or None if not found.
    """
    backends = discover_storage_backends()
    return backends.get(name)


def load_handler_from_string(handler_spec: str) -> Any:
    """Load a handler from a module:class specification string.

    This allows specifying handlers via CLI without entry point registration.

    Args:
        handler_spec: String in format "module.path:ClassName".

    Returns:
        Instantiated handler class.

    Raises:
        ValueError: If handler_spec format is invalid.
        ImportError: If module cannot be imported.
        AttributeError: If class not found in module.

    Example:
        >>> handler = load_handler_from_string("mypackage.handler:MySiteHandler")
    """
    if ":" not in handler_spec:
        raise ValueError(
            f"Invalid handler specification: '{handler_spec}'. "
            "Expected format: 'module.path:ClassName'"
        )

    module_path, class_name = handler_spec.rsplit(":", 1)

    try:
        import importlib

        module = importlib.import_module(module_path)
        handler_class = getattr(module, class_name)
        LOG.info("handler_loaded_from_string", spec=handler_spec)
        return handler_class()
    except ImportError as exc:
        raise ImportError(f"Cannot import module '{module_path}': {exc}") from exc
    except AttributeError as exc:
        raise AttributeError(
            f"Class '{class_name}' not found in module '{module_path}': {exc}"
        ) from exc


def list_available_plugins() -> dict[str, list[str]]:
    """List all available plugins organized by group.

    Returns:
        Dictionary mapping group name to list of plugin names.
    """
    return {
        "storage": list(discover_storage_backends().keys()),
        "keepalive_handlers": list(discover_keepalive_handlers().keys()),
        "plugins": list(discover_site_plugins().keys()),
    }


@lru_cache(maxsize=1)
def discover_all_plugins() -> tuple[CLIPluginProtocol, ...]:
    """Discover plugins from all sources and return valid ones.

    Combines entry-point plugins, YAML plugins, and Python file plugins.
    Filters out plugins that have config errors, missing site_name,
    or unsupported api_version.

    Results are cached; call ``discover_all_plugins.cache_clear()``
    to force re-discovery.

    Returns:
        Tuple of valid CLIPluginProtocol instances from all sources.
    """
    all_plugins: list[CLIPluginProtocol] = []

    # 1. Entry-point plugins (need instantiation)
    try:
        ep_plugins = discover_site_plugins()
        for name, plugin_class in ep_plugins.items():
            try:
                instance = plugin_class()
                all_plugins.append(instance)
                LOG.debug("ep_plugin_discovered", name=name)
            except PluginError as exc:
                LOG.warning(
                    "ep_plugin_instantiation_failed",
                    name=name,
                    error=str(exc),
                )
            except Exception as exc:  # noqa: BLE001
                LOG.exception(
                    "ep_plugin_instantiation_unexpected",
                    name=name,
                    error=str(exc),
                    exc_type=type(exc).__name__,
                )
    except Exception as exc:  # noqa: BLE001
        LOG.exception("ep_plugin_discovery_failed", error=str(exc))

    # 2. YAML plugins (already instantiated)
    try:
        yaml_plugins, yaml_errors = create_yaml_plugins()
        all_plugins.extend(yaml_plugins)
        for yaml_error in yaml_errors:
            LOG.warning(
                "yaml_plugin_error",
                filepath=str(yaml_error.filepath),
                error=yaml_error.error,
            )
    except Exception as exc:  # noqa: BLE001
        LOG.exception("yaml_plugin_discovery_failed", error=str(exc))

    # 3. Python file plugins (already instantiated)
    try:
        py_result = discover_python_plugins()
        all_plugins.extend(py_result.plugins)
        for py_error in py_result.errors:
            LOG.warning(
                "python_file_plugin_error",
                filepath=str(py_error.filepath),
                error=py_error.error,
            )
    except Exception as exc:  # noqa: BLE001
        LOG.exception(
            "python_file_plugin_discovery_failed",
            error=str(exc),
        )

    # Filter out invalid plugins
    valid: list[CLIPluginProtocol] = []
    for plugin in all_plugins:
        config_error = getattr(plugin, "_plugin_config_error", None)
        if config_error:
            LOG.warning(
                "plugin_config_error",
                plugin=type(plugin).__name__,
                error=str(config_error),
            )
            continue

        if not plugin.site_name:
            LOG.warning(
                "plugin_missing_site_name",
                plugin=type(plugin).__name__,
            )
            continue

        if plugin.api_version not in SUPPORTED_API_VERSIONS:
            LOG.warning(
                "plugin_unsupported_api_version",
                plugin=type(plugin).__name__,
                api_version=plugin.api_version,
                supported=sorted(SUPPORTED_API_VERSIONS),
            )
            continue

        valid.append(plugin)

    LOG.info("all_plugins_discovered", count=len(valid))
    return tuple(valid)


def get_plugin(name: str) -> CLIPluginProtocol:
    """Look up a plugin by site_name.

    Args:
        name: The site_name to look up.

    Returns:
        The matching CLIPluginProtocol instance.

    Raises:
        PluginError: If no plugin with the given name is found.
    """
    plugins = discover_all_plugins()
    for plugin in plugins:
        if plugin.site_name == name:
            return plugin

    available = sorted(p.site_name for p in plugins)
    raise PluginError(f"Plugin '{name}' is unknown. Available plugins: {available}")
