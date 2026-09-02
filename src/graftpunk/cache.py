"""Session caching and persistence using dill (enhanced pickle).

This module provides session storage functionality with pluggable backends:
- Local filesystem (default): ~/.config/graftpunk/sessions/
- Supabase (GRAFTPUNK_STORAGE_BACKEND=supabase): Supabase Storage
- S3 (GRAFTPUNK_STORAGE_BACKEND=s3): S3-compatible storage (AWS, R2, MinIO)

Backend selection is automatic based on GRAFTPUNK_STORAGE_BACKEND environment variable.

Thread Safety:
    This module uses a global cached storage backend for performance. The cache
    is NOT thread-safe. This is acceptable for the current single-threaded CLI
    usage pattern. If using graftpunk in a multi-threaded application, external
    synchronization is required when calling cache functions.
"""

import hashlib
import io
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable
from urllib.parse import urlparse

import dill as pickle
import requests

from graftpunk.config import get_settings
from graftpunk.encryption import decrypt_data, encrypt_data
from graftpunk.exceptions import EncryptionError, SessionExpiredError, SessionNotFoundError
from graftpunk.logging import get_logger
from graftpunk.session_identity import (
    GP_ACCOUNT_ATTR,
    validate_session_name,  # noqa: F401  (public re-export)
)
from graftpunk.storage.base import SessionMetadata, metadata_to_dict

if TYPE_CHECKING:
    from graftpunk.graftpunk_session import GraftpunkSession
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

# Ephemeral security headers that should not be copied from browser sessions
# to API sessions.  These are per-request tokens (e.g. WAF sensor blobs) that
# would cause stale-blob rejections if replayed.
_EPHEMERAL_HEADERS: frozenset[str] = frozenset({"x-csrf-token"})


# Global session storage backend (lazy-loaded)
_session_storage_backend: "SessionStorageBackend | None" = None


_VALID_BACKEND_TYPES = {"local", "supabase", "s3"}


def _create_backend(backend_type: str) -> "SessionStorageBackend":
    """Create a new storage backend instance for the given type.

    Args:
        backend_type: Storage backend type ("local", "supabase", or "s3").

    Returns:
        A new SessionStorageBackend instance.

    Raises:
        ValueError: If backend_type is not a supported value.
    """
    if backend_type not in _VALID_BACKEND_TYPES:
        raise ValueError(
            f"Unsupported storage backend: {backend_type!r}. "
            f"Supported: {', '.join(sorted(_VALID_BACKEND_TYPES))}"
        )

    settings = get_settings()
    config = settings.get_storage_config(backend_type=backend_type)

    if backend_type == "supabase":
        from graftpunk.storage.supabase import SupabaseSessionStorage

        return SupabaseSessionStorage(
            url=config["url"],
            service_key=config["service_key"],
            bucket_name=config.get("bucket_name", "sessions"),
        )

    if backend_type == "s3":
        from graftpunk.storage.s3 import S3SessionStorage

        return S3SessionStorage(
            bucket=config["bucket"],
            region=config.get("region"),
            endpoint_url=config.get("endpoint_url"),
            max_retries=config.get("retry_max_attempts", 5),
            base_delay=config.get("retry_base_delay", 1.0),
        )

    from graftpunk.storage.local import LocalSessionStorage

    return LocalSessionStorage(base_dir=config["base_dir"])


def _get_session_storage_backend(
    backend_override: str | None = None,
) -> "SessionStorageBackend":
    """Get or create the session storage backend.

    Returns the appropriate backend based on GRAFTPUNK_STORAGE_BACKEND env var:
    - "local" (default): Returns LocalSessionStorage
    - "supabase": Returns SupabaseSessionStorage
    - "s3": Returns S3SessionStorage

    Args:
        backend_override: If set, create and return a fresh backend instance
            for this type without caching it in the global singleton. Used by
            the ``--storage-backend`` CLI flag.

    Returns:
        A SessionStorageBackend instance.
    """
    if backend_override is not None:
        return _create_backend(backend_override)

    global _session_storage_backend

    if _session_storage_backend is not None:
        return _session_storage_backend

    settings = get_settings()
    _session_storage_backend = _create_backend(settings.storage_backend)

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
    metadata["account_identifier"] = getattr(session, GP_ACCOUNT_ATTR, None)

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

    # Fallback: infer domain from cookie domains when current_url is missing
    if "domain" not in metadata and metadata.get("cookie_domains"):
        # Use the first non-empty cookie domain, stripping leading dot
        for cookie_domain in metadata["cookie_domains"]:
            if cookie_domain:
                metadata["domain"] = cookie_domain.lstrip(".")
                break

    return metadata


def get_session_metadata(
    name: str,
    backend_override: str | None = None,
) -> dict[str, Any] | None:
    """Get session metadata without loading the full session.

    This is a lightweight way to check session status, timestamps, etc.
    without deserializing the encrypted pickle file.

    Args:
        name: Session name.
        backend_override: If set, use this backend type instead of the default.

    Returns:
        Metadata dict if session exists, None otherwise.
    """
    backend = _get_session_storage_backend(backend_override=backend_override)
    metadata = backend.get_session_metadata(name)
    if metadata is None:
        return None

    # Convert SessionMetadata to dict for API compatibility
    return metadata_to_dict(metadata)


def cache_session(session: T, session_name: str | None = None) -> str:
    """Cache a session with metadata.

    Storage location depends on GRAFTPUNK_STORAGE_BACKEND:
    - "local" (default): sessions/{session_name}/session.pickle + metadata.json
    - "supabase": Supabase Storage bucket (file-pair pattern)
    - "s3": S3-compatible storage bucket (file-pair pattern)

    Args:
        session: Session object to cache (must be picklable).
        session_name: Optional session name. If not provided, tries to get from session.

    Returns:
        Storage location string.
    """
    if session_name is None:
        # Try to get session_name from the session object
        session_name = getattr(session, "session_name", "default")

    validate_session_name(session_name)

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
            account_identifier=raw_metadata.get("account_identifier"),
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
    - "supabase": Supabase Storage (file-pair pattern)
    - "s3": S3-compatible storage (file-pair pattern)

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

        # Unpickle (encrypted data has already been validated via Fernet MAC).
        # NOTE: this deliberately uses the plain unpickler, not
        # _deserialize_browserfree: load_session returns the REAL cached object,
        # which update_session_cookies mutates and re-pickles. Stubbing the class
        # here would let a lean install silently re-save the session as a stub
        # type and lose its browser state. Callers that only need cookies/headers
        # should use load_session_for_api_from_bytes, which is browser-free by
        # design.
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
    except ModuleNotFoundError as exc:
        # A cached BrowserSession pickle names graftpunk.session, which imports
        # the browser stack. On a base install (no [browser] extra) that raises
        # ModuleNotFoundError from inside pickle.loads — which is neither an
        # UnpicklingError nor a RuntimeError, so without this it escaped raw as
        # "No module named 'httpie'".
        LOG.error("failed_to_load_session_missing_browser_stack", name=name, error=str(exc))
        raise ImportError(
            f"Loading the cached session '{name}' requires the browser automation "
            f"stack, which is not installed. Install it with: "
            f"pip install 'graftpunk[browser]'\n"
            f"(To read cookies/headers from a session WITHOUT the browser stack, use "
            f"load_session_for_api_from_bytes.)\n"
            f"(underlying import failure: {exc})"
        ) from exc
    except (pickle.UnpicklingError, RuntimeError) as exc:
        LOG.error(
            "failed_to_load_session",
            name=name,
            error=str(exc),
            backend=settings.storage_backend,
        )
        raise SessionExpiredError(f"Failed to load session '{name}': {exc}") from exc


def _api_session_from_session(source: SessionLike) -> "GraftpunkSession":
    """Build a browser-free GraftpunkSession from a session-like object
    (the module's SessionLike Protocol — cookies + headers), copying header
    roles and token caches when present.

    Shared by load_session_for_api (from a cached BrowserSession) and
    load_session_for_api_from_bytes (from a browser-free deserialize).
    """
    from graftpunk.graftpunk_session import GraftpunkSession
    from graftpunk.tokens import _CACHE_ATTR, _CSRF_TOKENS_ATTR

    header_roles = getattr(source, "_gp_header_roles", {})
    api_session = GraftpunkSession(header_roles=header_roles)

    if hasattr(source, "cookies"):
        api_session.cookies = source.cookies
        LOG.debug("copied_cookies_from_session", cookie_count=len(source.cookies))

    if hasattr(source, "headers"):
        # Copy headers from browser session, but skip requests-library defaults
        # that would clobber browser identity headers extracted from roles.
        # The pickled BrowserSession (a requests.Session) carries default headers
        # like User-Agent: python-requests/2.x — copying them overwrites the
        # Chrome UA that _apply_browser_identity() set during GraftpunkSession init.
        # Also skip ephemeral security headers (e.g. X-CSRF-TOKEN from WAFs like
        # Akamai Bot Manager) that are per-request and would cause stale-blob
        # rejections if replayed.
        _requests_defaults = requests.utils.default_headers()
        for key, value in source.headers.items():
            if key in _requests_defaults and _requests_defaults[key] == value:
                continue
            if key.lower() in _EPHEMERAL_HEADERS:
                LOG.debug("skipped_ephemeral_header", header=key)
                continue
            api_session.headers[key] = value
        LOG.debug("copied_headers_from_session")

    token_cache = getattr(source, _CACHE_ATTR, None)
    if token_cache:
        setattr(api_session, _CACHE_ATTR, token_cache)
        LOG.debug("copied_cached_tokens_from_session", count=len(token_cache))

    csrf_tokens = getattr(source, _CSRF_TOKENS_ATTR, None)
    if csrf_tokens is not None:
        setattr(api_session, _CSRF_TOKENS_ATTR, dict(csrf_tokens))
        LOG.debug("copied_csrf_tokens_from_session", count=len(csrf_tokens))

    return api_session


class _Stub:
    """Placeholder for a class the browser-free path cannot import (the browser
    stack is absent). Unpickling restores state via __setstate__ below.

    cookies/headers/_gp_header_roles and the cached-token dict are plain data in
    BrowserSession.__getstate__ (session.py:581-582), so they land directly on
    the stub. NOTE: __getstate__ does NOT serialize `_gp_csrf_tokens`, so csrf
    tokens are absent from the pickle entirely — the stub cannot and does not
    surface them.
    """

    def __setstate__(self, state: object) -> None:
        # pickle/dill deliver either a dict, or a (dict, slotstate) 2-tuple for
        # __slots__-bearing classes. Handle both; fail loud on any other shape
        # rather than silently discarding state.
        if isinstance(state, tuple) and len(state) == 2:
            dict_state, slot_state = state
            if dict_state:
                self.__dict__.update(dict_state)
            for key, value in (slot_state or {}).items():
                setattr(self, key, value)
        elif isinstance(state, dict):
            self.__dict__.update(state)
        else:
            raise TypeError(f"_Stub cannot restore pickle state of type {type(state)!r}")


class _BrowserFreeUnpickler(pickle.Unpickler):  # pickle is dill (see top import)
    """Unpickler that stubs classes it cannot import BECAUSE the browser stack is
    absent (graftpunk.session.BrowserSession -> requestium/selenium/httpie), so a
    cached session's plain state deserializes browser-free.

    Only import-family errors are converted to stubs. Any OTHER resolution error
    (a genuinely broken module, a renamed-but-present symbol) propagates — it
    must not be silently masked into a stub, which the A4 fixture could never
    catch (stubbing still lands the plain __dict__ and the test keeps passing).

    THIS IS NOT A SECURITY BOUNDARY. Despite the name, it imposes no restriction
    on what a payload may construct: ``super().find_class`` resolves any
    importable module/attribute, so a hostile pickle can still execute code
    (``__reduce__`` -> ``os.system``) exactly as the stdlib unpickler would.
    "Stub" here means "tolerate classes that are ABSENT", not "reject classes
    that are dangerous". The only thing preventing a forged payload from ever
    reaching this class is Fernet's authenticated encryption — i.e. possession
    of the key. Callers must treat the key as the trust boundary and never
    unpickle a blob from one trust domain with a key from another.
    """

    def find_class(self, module: str, name: str) -> type:
        try:
            return super().find_class(module, name)
        except ImportError:
            return type(name, (_Stub,), {})


def _deserialize_browserfree(decrypted: bytes) -> object:
    """Deserialize decrypted session bytes without importing the browser stack."""
    return _BrowserFreeUnpickler(io.BytesIO(decrypted)).load()


def load_session_for_api(name: str) -> requests.Session:
    """Load cached session for API use (no browser launched).

    This extracts cookies and headers from a cached BrowserSession
    and creates a GraftpunkSession that can be used for API calls
    without launching a browser. If the cached session has header
    roles (captured during login), they are applied automatically.

    No browser *process* is started — but this does go through
    :func:`load_session`, which reconstructs the real cached object and so needs
    the browser *package* importable (the ``[browser]`` extra). For a genuinely
    browser-free load — e.g. under Pyodide / Cloudflare Workers — use
    :func:`load_session_for_api_from_bytes`, which stubs the browser classes
    instead of importing them.

    Args:
        name: Session name (without .session.pickle extension).

    Returns:
        GraftpunkSession with cookies, headers, and header roles
        from the cached session.

    Raises:
        SessionNotFoundError: If session file doesn't exist.
        SessionExpiredError: If session cannot be unpickled.
        ImportError: If the browser stack needed to reconstruct the cached
            object is not installed (see note above).
    """
    try:
        browser_session = load_session(name)
    except FileNotFoundError as exc:
        LOG.error("session_not_found_for_api", name=name)
        raise SessionNotFoundError(
            f"No cached session found for '{name}'. Please login first."
        ) from exc

    header_roles = getattr(browser_session, "_gp_header_roles", {})
    api_session = _api_session_from_session(browser_session)
    LOG.info(
        "created_api_session_from_cached_session",
        name=name,
        has_header_roles=bool(header_roles),
        role_count=len(header_roles),
    )
    return api_session


def load_session_for_api_from_bytes(
    encrypted: bytes, *, key: bytes | None = None
) -> requests.Session:
    """Build a browser-free API session directly from encrypted session bytes.

    For callers that already hold the encrypted blob (e.g. a Cloudflare Worker
    that read it through an R2 binding) and cannot/should not go through a
    storage backend or the browser stack. Decrypts, deserializes browser-free,
    and extracts cookies/headers/roles/tokens into a GraftpunkSession.

    Args:
        encrypted: the Fernet(pickle(...)) blob.
        key: optional raw Fernet key. When given, decrypt with it directly —
            for environments where graftpunk's key file/vault does not exist
            (a Worker holding the key as a secret). When None, use the normal
            `decrypt_data` key sources.

    Security note: integrity and authenticity come SOLELY from the Fernet key —
    a valid MAC is the only thing standing between these bytes and the
    unpickler, which resolves whatever classes the payload names (see
    :class:`_BrowserFreeUnpickler`). The structure check below is a shape guard,
    NOT a safety control: it runs after unpickling, so it cannot prevent code a
    forged payload would already have executed. Never call this with a blob and
    a key from different trust domains, and treat the key as the security
    boundary it is.

    Raises:
        EncryptionError: if the key is malformed, or the bytes cannot be
            decrypted with it (wrong/rotated key, corrupted blob). Deliberately
            NOT remapped to SessionExpiredError: "I can't decrypt this" is an
            operator/config problem (fix the key) while "this session is
            unusable" prompts a re-login, and conflating them sends the caller
            down the wrong path.
        SessionExpiredError: if the decrypted payload cannot be deserialized, or
            the recovered object lacks the expected session structure.
    """
    # EncryptionError from decrypt_data intentionally propagates (see Raises).
    decrypted = decrypt_data(encrypted, key=key)
    try:
        source = _deserialize_browserfree(decrypted)
    except Exception as exc:  # noqa: BLE001 — any deserialization failure is an unusable session
        raise SessionExpiredError(f"Failed to deserialize session bytes: {exc}") from exc

    # isinstance against the runtime-checkable protocol does the same shape check
    # as two hasattr calls while also narrowing `object` for the type checker on
    # the _api_session_from_session(source: SessionLike) call below.
    if not isinstance(source, SessionLike):
        raise SessionExpiredError("Session bytes have invalid structure.")

    try:
        return _api_session_from_session(source)
    except Exception as exc:  # noqa: BLE001 — structurally present but malformed contents
        raise SessionExpiredError(f"Session bytes have unusable cookies/headers: {exc}") from exc


def update_session_cookies(api_session: requests.Session, session_name: str) -> None:
    """Persist an API session's cookies and token cache back to the session cache.

    Loads the original cached session (preserving browser metadata),
    updates its cookies and token cache from the API session, and saves it back.
    This is best-effort — failures are logged but do not raise.

    Args:
        api_session: The API session with potentially updated cookies and token cache.
        session_name: Name of the cached session to update.
    """
    try:
        original = load_session(session_name)
    except Exception as exc:  # noqa: BLE001 — best-effort save
        LOG.warning(
            "session_save_skipped_load_failed",
            session_name=session_name,
            error=str(exc),
        )
        return

    try:
        from graftpunk.tokens import _CACHE_ATTR, _CSRF_TOKENS_ATTR

        original.cookies.update(api_session.cookies)
        # Persist token cache and CSRF tokens from working session
        token_cache = getattr(api_session, _CACHE_ATTR, None)
        if token_cache is not None:
            setattr(original, _CACHE_ATTR, token_cache)
        csrf_tokens = getattr(api_session, _CSRF_TOKENS_ATTR, None)
        if csrf_tokens is not None:
            setattr(original, _CSRF_TOKENS_ATTR, dict(csrf_tokens))
        # A BrowserSession round-trips GP_ACCOUNT_ATTR through pickle on its own
        # (session.py's __getstate__/__setstate__); a plain requests.Session
        # does not (it pickles via its __attrs__ whitelist). Fall back to the
        # previously stored metadata so identity survives this refresh either way.
        if getattr(original, GP_ACCOUNT_ATTR, None) is None:
            stored_metadata = get_session_metadata(session_name)
            if stored_metadata is not None and stored_metadata.get("account_identifier"):
                setattr(original, GP_ACCOUNT_ATTR, stored_metadata["account_identifier"])
        cache_session(original, session_name)
        LOG.info("session_cookies_updated", session_name=session_name)
    except Exception as exc:  # noqa: BLE001 — best-effort save
        LOG.warning(
            "session_save_failed",
            session_name=session_name,
            error=str(exc),
        )


def list_sessions() -> list[str]:
    """List all cached session names.

    Returns:
        Sorted list of session names.
    """
    backend = _get_session_storage_backend()
    return backend.list_sessions()


def list_sessions_with_metadata(
    backend_override: str | None = None,
) -> list[dict[str, Any]]:
    """List all cached sessions with metadata.

    Args:
        backend_override: If set, use this backend type instead of the default.

    Returns:
        List of dicts with session metadata including:
        - name, domain, current_url
        - cookie_count, cookie_domains
        - created_at, modified_at
        - storage_backend, storage_location
    """
    backend = _get_session_storage_backend(backend_override=backend_override)
    names = backend.list_sessions()

    results = []
    for name in names:
        metadata = backend.get_session_metadata(name)
        if metadata is not None:
            results.append(metadata_to_dict(metadata))

    return sorted(results, key=lambda x: x.get("modified_at", ""), reverse=True)


def clear_session_cache(
    session_name: str | None = None,
    backend_override: str | None = None,
) -> list[str]:
    """Clear cached sessions.

    Args:
        session_name: If provided, clear only this session. Otherwise, clear all.
        backend_override: If set, use this backend type instead of the default.

    Returns:
        List of removed session names.
    """
    backend = _get_session_storage_backend(backend_override=backend_override)
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
