"""Python plugin loader for auto-discovery from plugins directory.

This module handles loading Python plugins from ~/.config/graftpunk/plugins/*.py
Similar to yaml_loader.py but for Python files containing SitePlugin subclasses.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from graftpunk.config import get_settings
from graftpunk.logging import get_logger
from graftpunk.plugins.cli_plugin import SitePlugin

if TYPE_CHECKING:
    from types import ModuleType

LOG = get_logger(__name__)


@dataclass(frozen=True)
class PythonDiscoveryError:
    """Error encountered while loading a Python plugin file.

    Attributes:
        filepath: Path to the Python file that failed to load.
        error: Human-readable error message describing the failure.
    """

    filepath: Path
    error: str


@dataclass
class PythonDiscoveryResult:
    """Result of Python plugin discovery.

    Supports partial success: plugins that fail to load are recorded as
    errors while valid plugins are still returned.

    Attributes:
        plugins: Successfully loaded SitePlugin instances.
        errors: Errors for plugins that could not be loaded.
    """

    plugins: list[SitePlugin] = field(default_factory=list)
    errors: list[PythonDiscoveryError] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        """Return True if any load errors occurred."""
        return bool(self.errors)


def _load_module_from_file(filepath: Path) -> ModuleType:
    """Load a Python module from a file path.

    Args:
        filepath: Path to the Python file to load.

    Returns:
        The loaded module.

    Raises:
        ImportError: If module cannot be loaded.
    """
    module_name = f"graftpunk_plugin_{filepath.stem}"
    spec = importlib.util.spec_from_file_location(module_name, filepath)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create module spec for {filepath}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        del sys.modules[module_name]
        raise
    return module


def _find_siteplugin_classes(module: ModuleType) -> list[type[SitePlugin]]:
    """Find all SitePlugin subclasses in a module.

    Only returns classes that are defined in the module itself (not imported).

    Args:
        module: The module to inspect.

    Returns:
        List of SitePlugin subclass types found in the module.
    """
    plugin_classes: list[type[SitePlugin]] = []

    for _name, obj in inspect.getmembers(module, inspect.isclass):
        # Skip if not defined in this module
        if obj.__module__ != module.__name__:
            continue
        # Skip the SitePlugin base class itself
        if obj is SitePlugin:
            continue
        # Check if it's a SitePlugin subclass
        if issubclass(obj, SitePlugin):
            plugin_classes.append(obj)

    return plugin_classes


def discover_python_plugins() -> PythonDiscoveryResult:
    """Discover all Python plugins in the config directory.

    Looks for *.py files in ~/.config/graftpunk/plugins/ and loads any
    classes that subclass SitePlugin. Files starting with underscore
    (like __init__.py or _helpers.py) are skipped.

    Supports partial success: valid plugins are returned even if some files
    fail to load. Check result.has_errors to see if any failures occurred.

    Returns:
        PythonDiscoveryResult containing loaded plugins and any errors.
    """
    settings = get_settings()
    plugins_dir = settings.config_dir / "plugins"

    if not plugins_dir.exists():
        LOG.debug("python_plugins_dir_not_found", path=str(plugins_dir))
        return PythonDiscoveryResult()

    result = PythonDiscoveryResult()

    for py_file in plugins_dir.glob("*.py"):
        if py_file.name.startswith("_"):
            LOG.debug("python_plugin_skipped_underscore", path=str(py_file))
            continue

        try:
            module = _load_module_from_file(py_file)
            plugin_classes = _find_siteplugin_classes(module)

            for plugin_class in plugin_classes:
                try:
                    instance = plugin_class()
                    result.plugins.append(instance)
                    LOG.info(
                        "python_plugin_loaded",
                        site_name=instance.site_name,
                        path=str(py_file),
                    )
                except (SystemExit, KeyboardInterrupt):
                    raise
                except Exception as exc:
                    LOG.warning(
                        "python_plugin_instantiation_failed",
                        path=str(py_file),
                        class_name=plugin_class.__name__,
                        error=str(exc),
                    )
                    result.errors.append(
                        PythonDiscoveryError(
                            filepath=py_file,
                            error=f"Failed to instantiate {plugin_class.__name__}: {exc}",
                        )
                    )

        except Exception as exc:
            LOG.exception("python_plugin_load_failed", path=str(py_file), error=str(exc))
            result.errors.append(PythonDiscoveryError(filepath=py_file, error=str(exc)))

    return result
