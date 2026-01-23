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

from graftpunk.cache import (
    cache_session,
    clear_session_cache,
    get_session_metadata,
    list_sessions,
    list_sessions_with_metadata,
    load_session,
    load_session_for_api,
    update_session_status,
)
from graftpunk.config import GraftpunkSettings, get_settings
from graftpunk.encryption import decrypt_data, encrypt_data, get_encryption_key
from graftpunk.exceptions import (
    BrowserError,
    EncryptionError,
    GraftpunkError,
    SessionExpiredError,
    SessionNotFoundError,
)
from graftpunk.session import BrowserSession
from graftpunk.stealth import create_stealth_driver
from graftpunk.storage.base import SessionMetadata, SessionStorageBackend

__version__ = "0.1.0"

__all__ = [
    # Version
    "__version__",
    # Session management
    "BrowserSession",
    "create_stealth_driver",
    # Cache operations
    "cache_session",
    "load_session",
    "load_session_for_api",
    "list_sessions",
    "list_sessions_with_metadata",
    "clear_session_cache",
    "get_session_metadata",
    "update_session_status",
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
