"""BSC (Browser Session Cache) - A pluggable browser session caching library.

This package provides:
- Encrypted browser session persistence
- Stealth browser automation
- Pluggable storage backends (local, Supabase, S3)
- Keepalive daemon with handler protocol
- Plugin architecture for site-specific authentication

Example:
    >>> from bsc import BrowserSession, cache_session, load_session
    >>> session = BrowserSession(headless=True)
    >>> # ... authenticate ...
    >>> cache_session(session, "mysite")
    >>> # Later:
    >>> session = load_session("mysite")
"""

from bsc.cache import (
    cache_session,
    clear_session_cache,
    get_session_metadata,
    list_sessions,
    list_sessions_with_metadata,
    load_session,
    load_session_for_api,
    update_session_status,
)
from bsc.config import BSCSettings, get_settings
from bsc.encryption import decrypt_data, encrypt_data, get_encryption_key
from bsc.exceptions import (
    BrowserError,
    BSCError,
    EncryptionError,
    SessionExpiredError,
    SessionNotFoundError,
)
from bsc.session import BrowserSession
from bsc.stealth import create_stealth_driver
from bsc.storage.base import SessionMetadata, SessionStorageBackend

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
    "BSCSettings",
    "get_settings",
    # Exceptions
    "BSCError",
    "BrowserError",
    "SessionExpiredError",
    "SessionNotFoundError",
    "EncryptionError",
]
