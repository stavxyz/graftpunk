"""Session caching and persistence using dill (enhanced pickle).

This module provides session storage functionality with pluggable backends:
- Local filesystem (default): ~/.config/graftpunk/sessions/
- Supabase (GRAFTPUNK_STORAGE_BACKEND=supabase): Supabase Storage + database

Backend selection is automatic based on GRAFTPUNK_STORAGE_BACKEND environment variable.

Thread Safety:
    This module uses a global cached storage backend for performance. The cache
    is NOT thread-safe. This is acceptable for the current single-threaded CLI
    usage pattern. If using graftpunk in a multi-threaded application, external
    synchronization is required when calling cache functions.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable
from urllib.parse import urlparse

import dill as pickle
import requests

from graftpunk.config import get_settings
from graftpunk.encryption import decrypt_data, encrypt_data
from graftpunk.exceptions import EncryptionError, SessionExpiredError, SessionNotFoundError
from graftpunk.logging import get_logger
from graftpunk.storage.base import SessionMetadata

if TYPE_CHECKING:
    from graftpunk.storage.base import SessionStorageBackend


@runtime_checkable
class SessionLike(Protocol):
    """Protocol for objects that can be loaded as sessions.

    This protocol defines the minimum interface expected from session objects
    returned by load_session(). It ensures type safety while allowing both
    BrowserSession and requestium.Session objects.
    """

    cookies: Any
    headers: Any


LOG = get_logger(__name__)

T = TypeVar("T")

# Global session storage backend (lazy-loaded)
_session_storage_backend: "SessionStorageBackend | None" = None


def _get_session_storage_backend() -> "SessionStorageBackend":
    """Get or create the session storage backend.

    Returns the appropriate backend based on GRAFTPUNK_STORAGE_BACKEND env var:
    - "local" (default): Returns LocalSessionStorage
    - "supabase": Returns SupabaseSessionStorage
    """
    global _session_storage_backend

    if _session_storage_backend is not None:
        return _session_storage_backend

    settings = get_settings()

    if settings.storage_backend == "supabase":
        # Lazy import to avoid circular imports
        from graftpunk.storage.supabase import SupabaseSessionStorage

        config = settings.get_storage_config()
        _session_storage_backend = SupabaseSessionStorage(
            url=config["url"],
            service_key=config["service_key"],
            bucket_name=config.get("bucket_name", "sessions"),
        )
    else:
        # Default: local filesystem backend
        from graftpunk.storage.local import LocalSessionStorage

        _session_storage_backend = LocalSessionStorage(base_dir=settings.sessions_dir)

    return _session_storage_backend


def _reset_session_storage_backend() -> None:
    """Reset the session storage backend (for testing)."""
    global _session_storage_backend
    _session_storage_backend = None


def _extract_session_metadata(session: Any, session_name: str) -> dict[str, Any]:
    """Extract metadata from a session object.

    Args:
        session: Session object
        session_name: Session name

    Returns:
        Dictionary with extracted metadata
    """
    metadata: dict[str, Any] = {"name": session_name}

    # Extract domain from current_url
    current_url = getattr(session, "current_url", None)
    if current_url:
        metadata["current_url"] = current_url
        parsed = urlparse(current_url)
        metadata["domain"] = parsed.hostname

    # Extract cookie info
    cookies = getattr(session, "cookies", None)
    if cookies:
        cookie_list = list(cookies)
        metadata["cookie_count"] = len(cookie_list)
        # Get unique domains from cookies
        domains = set()
        for cookie in cookie_list:
            if hasattr(cookie, "domain"):
                domains.add(cookie.domain)
        metadata["cookie_domains"] = sorted(domains)
    else:
        metadata["cookie_count"] = 0
        metadata["cookie_domains"] = []

    return metadata


def get_session_metadata(name: str) -> dict[str, Any] | None:
    """Get session metadata without loading the full session.

    This is a lightweight way to check session status, timestamps, etc.
    without deserializing the encrypted pickle file.

    Args:
        name: Session name.

    Returns:
        Metadata dict if session exists, None otherwise.
    """
    backend = _get_session_storage_backend()
    metadata = backend.get_session_metadata(name)
    if metadata is None:
        return None

    # Convert SessionMetadata to dict for API compatibility
    return {
        "name": metadata.name,
        "checksum": metadata.checksum,
        "created_at": metadata.created_at.isoformat(),
        "modified_at": metadata.modified_at.isoformat(),
        "expires_at": metadata.expires_at.isoformat() if metadata.expires_at else None,
        "domain": metadata.domain,
        "current_url": metadata.current_url,
        "cookie_count": metadata.cookie_count,
        "cookie_domains": metadata.cookie_domains,
        "status": metadata.status,
    }


def cache_session(session: T, session_name: str | None = None) -> str:
    """Cache a session with metadata.

    Storage location depends on GRAFTPUNK_STORAGE_BACKEND:
    - "local" (default): sessions/{session_name}/session.pickle + metadata.json
    - "supabase": Supabase Storage bucket + session_cache database table

    Args:
        session: Session object to cache (must be picklable).
        session_name: Optional session name. If not provided, tries to get from session.

    Returns:
        Storage location string.
    """
    if session_name is None:
        # Try to get session_name from the session object
        session_name = getattr(session, "session_name", "default")

    backend = _get_session_storage_backend()
    settings = get_settings()

    try:
        # Pickle and encrypt session data
        LOG.info("caching_session", name=session_name, backend=settings.storage_backend)
        pickled_data = pickle.dumps(session)
        checksum = hashlib.sha256(pickled_data).hexdigest()
        encrypted_data = encrypt_data(pickled_data)

        # Extract metadata from session
        raw_metadata = _extract_session_metadata(session, session_name)
        now = datetime.now(UTC)

        # Build SessionMetadata object with configurable TTL
        metadata = SessionMetadata(
            name=session_name,
            checksum=checksum,
            created_at=now,
            modified_at=now,
            expires_at=now + timedelta(hours=settings.session_ttl_hours),
            domain=raw_metadata.get("domain"),
            current_url=raw_metadata.get("current_url"),
            cookie_count=raw_metadata.get("cookie_count", 0),
            cookie_domains=raw_metadata.get("cookie_domains", []),
            status="active",
        )

        # Save to backend
        location = backend.save_session(session_name, encrypted_data, metadata)
        LOG.info("wrote_session_to_backend", name=session_name, location=location)
        return location

    except (pickle.PickleError, RuntimeError, OSError) as exc:
        LOG.error("failed_to_cache_session", name=session_name, error=str(exc))
        raise


def load_session(name: str) -> SessionLike:
    """Load a cached session.

    Storage location depends on GRAFTPUNK_STORAGE_BACKEND:
    - "local" (default): Local filesystem
    - "supabase": Supabase Storage + database

    Security Notes:
        This function deserializes pickle data. Pickle can execute arbitrary
        code during deserialization.

        Threat Model:
        - Sessions are encrypted with Fernet (AES-128-CBC + HMAC)
        - SHA256 checksum validation before unpickling (defense-in-depth)
        - Runtime validation is performed after unpickling to detect corrupted data
        - Fernet provides HMAC authentication (SHA256) to detect tampering

        Recommendation: Only run this tool on trusted machines.

    Args:
        name: Session name.

    Returns:
        Loaded session object.

    Raises:
        SessionNotFoundError: If session file doesn't exist.
        SessionExpiredError: If session cannot be decrypted or has invalid structure.
    """
    backend = _get_session_storage_backend()
    settings = get_settings()

    try:
        # Load encrypted data from backend
        encrypted_data, metadata = backend.load_session(name)

        # Decrypt
        try:
            decrypted_data = decrypt_data(encrypted_data)
        except EncryptionError as exc:
            LOG.error("session_decryption_failed", name=name, backend=settings.storage_backend)
            raise SessionExpiredError(
                f"Session '{name}' cannot be decrypted. The session file may be corrupted "
                "or the encryption key has changed. Please run 'graftpunk clear' and re-login."
            ) from exc

        # Verify checksum (required for new sessions, skipped for legacy)
        # Legacy sessions (loaded from flat file structure) have empty checksum
        if metadata.checksum:
            actual_checksum = hashlib.sha256(decrypted_data).hexdigest()
            if actual_checksum != metadata.checksum:
                LOG.error("session_checksum_mismatch", name=name, backend=settings.storage_backend)
                raise SessionExpiredError(
                    f"Session '{name}' failed integrity check. Run 'gp clear' and re-login."
                )
        else:
            # Legacy session without checksum - log warning but allow loading
            LOG.warning(
                "session_checksum_missing_legacy",
                name=name,
                hint="Consider re-saving session to add checksum",
            )

        # Unpickle (encrypted data has already been validated via Fernet MAC)
        session = pickle.loads(decrypted_data)  # noqa: S301

        # Runtime validation: verify unpickled object has expected attributes
        if not hasattr(session, "cookies") or not hasattr(session, "headers"):
            raise SessionExpiredError(
                f"Session '{name}' has invalid structure. Run 'gp clear' and re-login."
            )

        LOG.info("successfully_loaded_session", name=name, backend=settings.storage_backend)
        return session

    except (SessionNotFoundError, SessionExpiredError):
        raise
    except (pickle.UnpicklingError, RuntimeError) as exc:
        LOG.error(
            "failed_to_load_session",
            name=name,
            error=str(exc),
            backend=settings.storage_backend,
        )
        raise SessionExpiredError(f"Failed to load session '{name}': {exc}") from exc


def load_session_for_api(name: str) -> requests.Session:
    """Load cached session for API use (no browser required).

    This extracts cookies and headers from a cached BrowserSession
    and creates a plain requests.Session that can be used for API calls
    without launching a browser.

    Args:
        name: Session name (without .session.pickle extension).

    Returns:
        requests.Session with cookies and headers from cached session.

    Raises:
        SessionNotFoundError: If session file doesn't exist.
        SessionExpiredError: If session cannot be unpickled.
    """
    try:
        browser_session = load_session(name)
    except FileNotFoundError as exc:
        LOG.error("session_not_found_for_api", name=name)
        raise SessionNotFoundError(
            f"No cached session found for '{name}'. Please login first."
        ) from exc

    # Create a plain requests.Session with cookies and headers
    api_session = requests.Session()

    # Copy cookies from browser session
    if hasattr(browser_session, "cookies"):
        api_session.cookies = browser_session.cookies
        LOG.debug(
            "copied_cookies_from_session",
            cookie_count=len(browser_session.cookies),
        )

    # Copy headers from browser session
    if hasattr(browser_session, "headers"):
        api_session.headers.update(browser_session.headers)
        LOG.debug("copied_headers_from_session")

    LOG.info("created_api_session_from_cached_session", name=name)
    return api_session


def list_sessions() -> list[str]:
    """List all cached session names.

    Returns:
        Sorted list of session names.
    """
    backend = _get_session_storage_backend()
    return backend.list_sessions()


def list_sessions_with_metadata() -> list[dict[str, Any]]:
    """List all cached sessions with metadata.

    Returns:
        List of dicts with session metadata including:
        - name, domain, current_url
        - cookie_count, cookie_domains
        - created_at, modified_at
        - path (to session directory or file)
    """
    backend = _get_session_storage_backend()
    settings = get_settings()
    names = backend.list_sessions()

    results = []
    for name in names:
        metadata = backend.get_session_metadata(name)
        if metadata is not None:
            results.append(
                {
                    "name": metadata.name,
                    "checksum": metadata.checksum,
                    "created_at": metadata.created_at.isoformat(),
                    "modified_at": metadata.modified_at.isoformat(),
                    "expires_at": metadata.expires_at.isoformat() if metadata.expires_at else None,
                    "domain": metadata.domain,
                    "current_url": metadata.current_url,
                    "cookie_count": metadata.cookie_count,
                    "cookie_domains": metadata.cookie_domains,
                    "status": metadata.status,
                    "path": str(settings.sessions_dir / name),
                }
            )

    return sorted(results, key=lambda x: x.get("modified_at", ""), reverse=True)


def clear_session_cache(session_name: str | None = None) -> list[str]:
    """Clear cached sessions.

    Args:
        session_name: If provided, clear only this session. Otherwise, clear all.

    Returns:
        List of removed session names.
    """
    backend = _get_session_storage_backend()
    removed: list[str] = []

    if session_name:
        # Clear specific session
        if backend.delete_session(session_name):
            removed.append(session_name)
        return removed

    # Clear all sessions
    for name in backend.list_sessions():
        if backend.delete_session(name):
            removed.append(name)

    return removed


def update_session_status(name: str, status: str) -> None:
    """Update session status in metadata.

    Args:
        name: Session name.
        status: New status ("active" or "logged_out").

    Raises:
        SessionNotFoundError: If session doesn't exist.
        ValueError: If status is not a valid value.
    """
    if status not in ("active", "logged_out"):
        raise ValueError(f"Invalid status '{status}'. Must be 'active' or 'logged_out'")

    backend = _get_session_storage_backend()
    if not backend.update_session_metadata(name, status=status):
        raise SessionNotFoundError(f"Session '{name}' not found")

    LOG.info("updated_session_status", name=name, status=status)
