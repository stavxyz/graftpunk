"""graftpunk - Turn any website into an API.

Graft scriptable access onto authenticated web services.
Log in once, script forever.

This package provides:
- Encrypted browser session persistence
- Stealth browser automation
- Pluggable storage backends (local, Supabase, S3)
- Keepalive daemon for session maintenance
- Plugin architecture for site-specific commands

Example:
    >>> from graftpunk import BrowserSession, cache_session, load_session_for_api
    >>> session = BrowserSession(headless=False)
    >>> # Log in manually in the browser...
    >>> cache_session(session, "mysite")
    >>> # Later, use like an API:
    >>> api = load_session_for_api("mysite")
    >>> response = api.get("https://mysite.com/api/data")
"""

from importlib import import_module as _import_module
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import TYPE_CHECKING, Any

from graftpunk.backends import BrowserBackend, get_backend, list_backends, register_backend
from graftpunk.cache import (
    cache_session,
    clear_session_cache,
    get_session_metadata,
    list_sessions,
    list_sessions_with_metadata,
    load_session,
    load_session_for_api,
    load_session_for_api_from_bytes,
    update_session_status,
    validate_session_name,
)
from graftpunk.client import GraftpunkClient
from graftpunk.config import GraftpunkSettings, get_settings
from graftpunk.encryption import decrypt_data, encrypt_data, get_encryption_key
from graftpunk.exceptions import (
    BrowserError,
    EncryptionError,
    GraftpunkError,
    SessionExpiredError,
    SessionNotFoundError,
)
from graftpunk.graftpunk_session import get_role_headers, list_roles, register_role
from graftpunk.storage.base import SessionMetadata, SessionStorageBackend

# Browser-only symbols, resolved lazily by __getattr__ below. Declared here so
# type checkers and IDEs still see them (PEP 562 lazy attributes are invisible to
# static analysis otherwise).
if TYPE_CHECKING:
    from graftpunk.session import BrowserSession
    from graftpunk.stealth import create_stealth_driver

_LAZY_BROWSER_SYMBOLS = {
    "BrowserSession": ("graftpunk.session", "BrowserSession"),
    "create_stealth_driver": ("graftpunk.stealth", "create_stealth_driver"),
}


def __getattr__(name: str) -> Any:  # PEP 562 lazy attribute loading
    """Lazy-load browser-only symbols so `import graftpunk` works without the
    browser stack (selenium/requestium/etc.) installed — e.g. under Pyodide.

    A missing browser stack is reported as an ImportError naming the extra to
    install. Without that translation the caller sees the raw transitive failure
    (``No module named 'httpie'``), which names a package they never asked for
    and gives no hint that ``pip install 'graftpunk[browser]'`` is the fix.

    Note: ImportError (not AttributeError) is deliberate — this is a missing
    dependency, and ``except ImportError`` is what callers reach for. The
    trade-off is that ``hasattr(graftpunk, "BrowserSession")`` propagates rather
    than returning False, so probe for the browser stack with try/except
    ImportError instead of hasattr. (The two cannot both be satisfied:
    ImportError and AttributeError have incompatible C layouts and cannot be
    combined into one exception class.)
    """
    target = _LAZY_BROWSER_SYMBOLS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = target
    try:
        module = _import_module(module_name)
    except ImportError as exc:
        raise ImportError(
            f"graftpunk.{name} requires the browser automation stack, which is not "
            f"installed. Install it with: pip install 'graftpunk[browser]'"
        ) from exc
    return getattr(module, attr)


def __dir__() -> list[str]:
    """Include the lazily-loaded browser symbols in dir()/tab-completion.

    PEP 562 ``__getattr__`` does not feed ``dir()``, so without this the two
    browser symbols silently disappear from introspection.
    """
    return sorted(set(globals()) | set(_LAZY_BROWSER_SYMBOLS))


# Single source of truth: the version lives in pyproject.toml and is read back
# from the installed package metadata. There is no literal to bump (or forget),
# so __version__ can never drift from the packaged version.
try:
    __version__ = _pkg_version("graftpunk")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"

__all__ = [
    # Version
    "__version__",
    # Python API
    "GraftpunkClient",
    # Session management
    "BrowserSession",
    "create_stealth_driver",
    # Browser backends
    "BrowserBackend",
    "get_backend",
    "list_backends",
    "register_backend",
    # Header roles
    "register_role",
    "list_roles",
    "get_role_headers",
    # Cache operations
    "cache_session",
    "load_session",
    "load_session_for_api",
    "load_session_for_api_from_bytes",
    "list_sessions",
    "list_sessions_with_metadata",
    "clear_session_cache",
    "get_session_metadata",
    "update_session_status",
    "validate_session_name",
    # Encryption
    "encrypt_data",
    "decrypt_data",
    "get_encryption_key",
    # Storage
    "SessionMetadata",
    "SessionStorageBackend",
    # Configuration
    "GraftpunkSettings",
    "get_settings",
    # Exceptions
    "GraftpunkError",
    "BrowserError",
    "SessionExpiredError",
    "SessionNotFoundError",
    "EncryptionError",
]
