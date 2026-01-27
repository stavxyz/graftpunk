"""Browser automation backends for graftpunk.

This module provides pluggable browser backends with a unified interface.
The default backend is "selenium" (using undetected-chromedriver).

Available backends:
    - selenium: Selenium + undetected-chromedriver (default)
    - nodriver: CDP-direct Chrome automation (requires: pip install graftpunk[nodriver])

Note: "legacy" is accepted as an alias for "selenium" for backward compatibility.

Example:
    >>> from graftpunk.backends import get_backend, list_backends
    >>> print(list_backends())
    ['legacy', 'nodriver', 'selenium']
    >>> backend = get_backend("selenium", headless=True)
    >>> with backend:
    ...     backend.navigate("https://example.com")
    ...     print(backend.page_title)
"""

from importlib import import_module
from typing import Any

from graftpunk.backends.base import BrowserBackend

# Backend registry maps names to module:class paths
# Using strings enables lazy loading - dependencies only imported when used
_BACKEND_REGISTRY: dict[str, str] = {
    "selenium": "graftpunk.backends.selenium:SeleniumBackend",
    "legacy": "graftpunk.backends.selenium:SeleniumBackend",  # Alias for backward compat
    "nodriver": "graftpunk.backends.nodriver:NoDriverBackend",
}


def get_backend(name: str = "selenium", **kwargs: Any) -> BrowserBackend:
    """Get a browser backend instance by name.

    This is the primary factory function for creating browser backends.
    Backends are lazily imported to avoid loading dependencies that
    aren't installed.

    Args:
        name: Backend identifier. One of: "selenium", "nodriver".
            Default is "selenium". ("legacy" is accepted as an alias for "selenium")
        **kwargs: Backend-specific initialization options passed to
            the backend constructor. Common options include:
            - headless: bool (selenium default True, nodriver default False)
            - use_stealth: bool (selenium only, default True)
            - profile_dir: Path | None
            - default_timeout: int (default 15)

    Returns:
        Configured BrowserBackend instance (not started).
        Call .start() or use as context manager to start the browser.

    Raises:
        ValueError: If backend name is not recognized.
        ImportError: If backend dependencies are not installed.

    Example:
        >>> backend = get_backend("selenium", headless=False, use_stealth=True)
        >>> backend.start()
        >>> backend.navigate("https://example.com")
        >>> backend.stop()

        >>> # Or use as context manager
        >>> with get_backend("selenium") as backend:
        ...     backend.navigate("https://example.com")
    """
    if name not in _BACKEND_REGISTRY:
        available = ", ".join(sorted(_BACKEND_REGISTRY.keys()))
        raise ValueError(f"Unknown backend '{name}'. Available backends: {available}")

    # Parse module:class path
    module_class_path = _BACKEND_REGISTRY[name]
    module_path, class_name = module_class_path.rsplit(":", 1)

    # Lazy import the backend module
    try:
        module = import_module(module_path)
    except ImportError as exc:
        raise ImportError(
            f"Backend '{name}' requires additional dependencies. "
            f"Failed to import {module_path}: {exc}"
        ) from exc

    # Get the backend class
    backend_class = getattr(module, class_name)

    # Instantiate and return
    return backend_class(**kwargs)


def list_backends() -> list[str]:
    """List available backend names.

    Returns:
        Sorted list of registered backend names.

    Example:
        >>> list_backends()
        ['legacy', 'selenium']
    """
    return sorted(_BACKEND_REGISTRY.keys())


def register_backend(name: str, module_class_path: str) -> None:
    """Register a custom backend.

    This allows third-party packages to register their own backends
    that implement the BrowserBackend protocol.

    Args:
        name: Backend identifier (e.g., "mybackend").
        module_class_path: Import path in format "module.path:ClassName".

    Raises:
        ValueError: If name is already registered or path format is invalid.

    Example:
        >>> register_backend("mybackend", "mypackage.backends:MyBackend")
        >>> backend = get_backend("mybackend")
    """
    if name in _BACKEND_REGISTRY:
        raise ValueError(f"Backend '{name}' is already registered")

    if ":" not in module_class_path:
        raise ValueError(
            f"Invalid module_class_path '{module_class_path}'. "
            "Expected format: 'module.path:ClassName'"
        )

    _BACKEND_REGISTRY[name] = module_class_path


__all__ = [
    "BrowserBackend",
    "get_backend",
    "list_backends",
    "register_backend",
]
