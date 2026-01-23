"""Plugin system for BSC using entry points.

This module provides discovery and loading of plugins registered via
Python entry points. Plugins can provide:

- Storage backends (bsc.storage)
- Keepalive handlers (bsc.keepalive_handlers)
- Site authentication plugins (bsc.plugins)

Example plugin registration in pyproject.toml:
    [project.entry-points."bsc.keepalive_handlers"]
    mysite = "mypackage.handler:MySiteHandler"

    [project.entry-points."bsc.plugins"]
    mysite = "mypackage.plugin:MySitePlugin"
"""

from importlib.metadata import entry_points
from typing import Any

from bsc.logging import get_logger

LOG = get_logger(__name__)

# Entry point group names
STORAGE_GROUP = "bsc.storage"
KEEPALIVE_HANDLERS_GROUP = "bsc.keepalive_handlers"
PLUGINS_GROUP = "bsc.plugins"


def discover_plugins(group: str) -> dict[str, Any]:
    """Discover all installed plugins in a group via entry points.

    Args:
        group: Entry point group name (e.g., "bsc.keepalive_handlers").

    Returns:
        Dictionary mapping plugin name to loaded plugin class/object.

    Example:
        >>> handlers = discover_plugins("bsc.keepalive_handlers")
        >>> if "humaninterest" in handlers:
        ...     handler = handlers["humaninterest"]()
    """
    plugins: dict[str, Any] = {}

    try:
        eps = entry_points(group=group)
    except TypeError:
        # Python 3.9 compatibility
        all_eps = entry_points()
        eps = all_eps.get(group, all_eps.__class__())  # type: ignore[arg-type]

    for ep in eps:
        try:
            plugin = ep.load()
            plugins[ep.name] = plugin
            LOG.debug("plugin_loaded", group=group, name=ep.name)
        except Exception as exc:
            LOG.warning(
                "plugin_load_failed",
                group=group,
                name=ep.name,
                error=str(exc),
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
    """Discover all installed site authentication plugins.

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
