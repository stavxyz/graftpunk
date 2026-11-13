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

import contextlib
import hashlib
import io
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http import cookiejar
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable
from urllib.parse import urlparse

import dill as pickle
import requests

from graftpunk._distribution import install_command
from graftpunk.config import get_settings
from graftpunk.encryption import decrypt_data, encrypt_data
from graftpunk.exceptions import (
    EncryptionError,
    MissingEncryptionKeyError,
    SessionExpiredError,
    SessionNotFoundError,
    StorageError,
)
from graftpunk.logging import get_logger
from graftpunk.session_identity import (
    ACCOUNT_RULE,
    GP_ACCOUNT_ATTR,
    GP_SESSION_NAME_ATTR,
    resolve_account_session,
    split_session_name,
    validate_session_name,  # public re-export
)
from graftpunk.storage.base import (
    SessionMetadata,
    check_slot_name,
    metadata_to_dict,
)

if TYPE_CHECKING:
    from pathlib import Path

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
# The sessions directory a cached local backend was built for. The config
# directory is read live (#282), so a GRAFTPUNK_CONFIG_DIR changed after the
# backend was built must reach the backend too, not only the settings.
_session_storage_backend_dir: "Path | None" = None


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

    global _session_storage_backend, _session_storage_backend_dir

    settings = get_settings()
    local = settings.storage_backend == "local"
    if _session_storage_backend is not None and (
        not local
        or _session_storage_backend_dir is None
        or _session_storage_backend_dir == settings.sessions_dir
    ):
        return _session_storage_backend

    _session_storage_backend = _create_backend(settings.storage_backend)
    _session_storage_backend_dir = settings.sessions_dir if local else None

    return _session_storage_backend


def _reset_session_storage_backend() -> None:
    """Reset the session storage backend (for testing)."""
    global _session_storage_backend, _session_storage_backend_dir
    _session_storage_backend = None
    _session_storage_backend_dir = None


def _extract_session_metadata(session: Any, session_name: str) -> dict[str, Any]:
    """Extract metadata from a session object.

    Args:
        session: Session object
        session_name: Session name

    Returns:
        Dictionary with extracted metadata
    """
    from graftpunk.cookies import _browser_cookie_domain

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
            if isinstance(cookie, cookiejar.Cookie):
                domains.add(_browser_cookie_domain(cookie))
            elif hasattr(cookie, "domain"):
                # A custom session's cookie objects; _browser_cookie_domain
                # reads attributes only http.cookiejar.Cookie has.
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


def _stamp_session_slot(session: Any, name: str) -> None:
    """Record on *session* the cache slot this copy came from or went to.

    The stamp is the one thing that lets a bare-name write-back find the slot a
    bare-name load resolved to (:func:`update_session_cookies`, #174). It is a
    memory-only rider (``_MEMORY_ONLY_SESSION_RIDER_ATTRS`` in
    :mod:`graftpunk.tokens`): every loader that has a key, and the saver, set it
    from the key it just used, so it never outlives the key it describes. The
    bytes loader (:func:`load_session_for_api_from_bytes`) has no key and leaves
    the session unstamped.

    The stamp is advisory, so no failure to record it may fail the load or save
    that carried it. A session object that refuses the attribute keeps working
    without it, and only loses the bare-name write-back redirect.
    """
    try:
        setattr(session, GP_SESSION_NAME_ATTR, name)
    except Exception as exc:  # noqa: BLE001 (an advisory stamp never fails its caller)
        LOG.debug(
            "session_slot_stamp_skipped",
            name=name,
            session_type=type(session).__name__,
            error=str(exc),
        )


def cache_session(
    session: T,
    session_name: str | None = None,
    *,
    account_rule: int | None = ACCOUNT_RULE,
    key: bytes | None = None,
) -> str:
    """Cache a session with metadata.

    Storage location depends on GRAFTPUNK_STORAGE_BACKEND:
    - "local" (default): sessions/{session_name}/session.pickle + metadata.json
    - "supabase": Supabase Storage bucket (file-pair pattern)
    - "s3": S3-compatible storage bucket (file-pair pattern)

    Args:
        session: Session object to cache (must be picklable).
        session_name: Optional session name. If not provided, tries to get from session.
        account_rule: The account-label rule to record. A write-back passes the
            slot's recorded value, so a slot graftpunk 1.24.0 named stays
            recognisable as one (#293).
        key: Optional raw Fernet key to encrypt with, for a host without
            graftpunk's key file or vault (see :func:`encrypt_data`). When
            None, the configured key is used. Read the session back with the
            same key (``load_session_for_api_from_bytes(..., key=)``).

    Returns:
        Storage location string.

    Raises:
        EncryptionError: The key is malformed, or none was given and the
            configured source has none.
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
        encrypted_data = encrypt_data(pickled_data, key=key)

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
            account_rule=account_rule,
        )

        # Save to backend
        location = backend.save_session(session_name, encrypted_data, metadata)
        # Stamp the in-memory object with the slot it now lives in, so a later
        # write-back through this object knows where it belongs (#174). Set
        # after the pickle above on purpose: the name describes this copy, not
        # the bytes, and the blob must stay free of it.
        _stamp_session_slot(session, session_name)
        LOG.info("wrote_session_to_backend", name=session_name, location=location)
        return location

    except (pickle.PickleError, RuntimeError, OSError) as exc:
        LOG.error("failed_to_cache_session", name=session_name, error=str(exc))
        raise


def load_session(
    name: str, backend_override: str | None = None, *, key: bytes | None = None
) -> SessionLike:
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
        backend_override: If set, use this backend type instead of the default.
        key: Optional raw Fernet key to decrypt with instead of the configured
            one (see :func:`graftpunk.encryption.decrypt_data`).

    Returns:
        Loaded session object.

    Raises:
        SessionNotFoundError: If session file doesn't exist.
        SessionExpiredError: If session cannot be decrypted or has invalid structure.
        MissingEncryptionKeyError: No key was given and the configured backend
            has no key file to read (see :func:`graftpunk.encryption.get_encryption_key`).
    """
    session, _metadata = _load_session_with_metadata(
        name, backend_override=backend_override, key=key
    )
    return session


def _load_session_with_metadata(
    name: str, backend_override: str | None = None, *, key: bytes | None = None
) -> tuple[SessionLike, SessionMetadata]:
    """Load a cached session together with the SessionMetadata already fetched
    during that same backend read.

    ``load_session`` delegates here and discards the metadata half; callers
    that need both (e.g. ``update_session_cookies``, which must recover
    ``account_identifier`` when the unpickled session dropped it) call this
    directly instead of pairing ``load_session`` with a second metadata read.

    Args:
        name: Session name to load.
        backend_override: If set, use this backend type instead of the default.
        key: Optional raw Fernet key; None uses the configured key.

    Returns:
        The deserialized session and the ``SessionMetadata`` read in the same
        backend round-trip.

    Raises:
        SessionNotFoundError: No session is stored under *name*.
        SessionExpiredError: The session is expired, cannot be decrypted, or
            fails its checksum/deserialization.
        MissingEncryptionKeyError: No key was given and there is none to read.
            Passed through, not reported as an expired session, because the fix
            is to provide the key, not to clear the session.
    """
    backend = _get_session_storage_backend(backend_override=backend_override)
    settings = get_settings()

    try:
        # Load encrypted data from backend
        encrypted_data, metadata = backend.load_session(name)

        # Decrypt
        try:
            decrypted_data = decrypt_data(encrypted_data, key=key)
        except MissingEncryptionKeyError:
            # No key at all is fixed by providing one, never by clearing the
            # session (which on a shared bucket is every host's session).
            raise
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
        # here would let graftpunk-lite without [browser] silently re-save the
        # session as a stub type and lose its browser state. Callers that only
        # need cookies/headers should use load_session_for_api_from_bytes, which
        # is browser-free by design.
        session = pickle.loads(decrypted_data)  # noqa: S301

        # Runtime validation: verify unpickled object has expected attributes
        if not hasattr(session, "cookies") or not hasattr(session, "headers"):
            raise SessionExpiredError(
                f"Session '{name}' has invalid structure. Run 'gp clear' and re-login."
            )

        # The slot this object came out of, for any write-back that follows
        # (#174). Set here rather than in the pickle so it can never be stale.
        _stamp_session_slot(session, name)
        # Metadata recorded under the current rule is the authority on the
        # account: a login may have replaced a 1.24.0 record there without
        # touching the pickle (set_session_account). Taking it here means any
        # save of this copy writes the replaced value, and the pickle loses the
        # old one (#293).
        if metadata.account_rule == ACCOUNT_RULE:
            with contextlib.suppress(Exception):
                setattr(session, GP_ACCOUNT_ATTR, metadata.account_identifier)

        LOG.info("successfully_loaded_session", name=name, backend=settings.storage_backend)
        return session, metadata

    except (SessionNotFoundError, SessionExpiredError, MissingEncryptionKeyError):
        raise
    except ModuleNotFoundError as exc:
        # A cached BrowserSession pickle names graftpunk.session, which imports
        # the browser stack. On graftpunk-lite without [browser] that raises
        # ModuleNotFoundError from inside pickle.loads — which is neither an
        # UnpicklingError nor a RuntimeError, so without this it escaped raw as
        # "No module named 'httpie'". Info, not error: the ImportError below
        # carries the same facts, and the CLI turns it into the install hint,
        # so an error line here would only repeat it above that hint.
        LOG.info("failed_to_load_session_missing_browser_stack", name=name, error=str(exc))
        raise ImportError(
            f"Loading the cached session '{name}' requires the browser automation "
            f"stack, which is not installed. Install it with: "
            f"{install_command('browser')}\n"
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
    from graftpunk.client_headers import client_sets_header
    from graftpunk.cookies import use_browser_cookie_policy
    from graftpunk.graftpunk_session import GraftpunkSession, replayable_roles
    from graftpunk.tokens import _HEADER_ROLES_ATTR, SESSION_RIDER_ATTRS

    # Roles a capture before 1.41.0 saved can hold headers the client sets itself
    # (#370); the rider loop below leaves this attribute to the constructor.
    header_roles = replayable_roles(getattr(source, _HEADER_ROLES_ATTR, None) or {})
    api_session = GraftpunkSession(header_roles=header_roles)

    if hasattr(source, "cookies"):
        # A loaded jar carries the standard library's class (how this release
        # pickles BrowserCookiePolicy) or HTTPie's (1.21.0 and earlier); give
        # it the browser policy.
        api_session.cookies = use_browser_cookie_policy(source.cookies)
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
            if client_sets_header(key):
                # A session saved before 1.41.0 can carry Chrome's Accept-Encoding,
                # which identity extraction put on its headers (#370).
                continue
            api_session.headers[key] = value
        LOG.debug("copied_headers_from_session")

    # Every rider travels, from the one registry: token cache, csrf tokens,
    # header roles and the account identifier. Hand-listing here is what let
    # the identifier fall off the API session, wiping it from metadata on the
    # next cache_session (#151).
    for attr in SESSION_RIDER_ATTRS:
        value = getattr(source, attr, None)
        if not value or attr == _HEADER_ROLES_ATTR:
            continue
        setattr(api_session, attr, dict(value) if isinstance(value, dict) else value)
        LOG.debug("copied_session_rider", attr=attr)

    return api_session


class _Stub:
    """Placeholder for a class the browser-free path cannot import (the browser
    stack is absent). Unpickling restores state via __setstate__ below.

    cookies/headers and the pickled riders (``PICKLED_SESSION_RIDER_ATTRS``)
    are plain data in ``BrowserSession.__getstate__``, so they land directly on
    the stub. NOTE: that subset deliberately excludes the memory-only riders
    (``_MEMORY_ONLY_SESSION_RIDER_ATTRS`` in :mod:`graftpunk.tokens`), which
    are therefore absent from the pickle entirely: the stub cannot and does not
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


def no_account_cached_message(name: str) -> str:
    """The actionable message for a bare *name* with nothing cached under it.

    Shared by :func:`load_session_for_api_resolved` and any CLI surface that
    already knows account resolution was exhausted for *name* (a registered
    site name whose listing ``resolve_session_name_or_exit`` already
    consulted), so the wording never drifts between the exception this
    module raises for that case and the one a caller renders for the same
    case (#178, PR #189 review).
    """
    return (
        f"No session cached for '{name}', and no account is cached under it. "
        "Run 'gp <site> login' first."
    )


# Bare names _warn_if_bare_slot_shadows_labelled has already checked, with or
# without siblings, so a bare name costs at most one listing per process and a
# long-running process warns once per name rather than on every load (#197).
_bare_slot_sibling_checked: set[str] = set()


def _warn_if_bare_slot_shadows_labelled(name: str) -> None:
    """Check once per process whether the loaded bare slot *name* has labelled siblings, and warn.

    Since #176 ``gp <site> login`` writes ``base@label`` slots, so a bare slot
    that still answers a bare-name load is usually the stale one from before
    the first labelled login. The load keeps returning it (the pin contract in
    :func:`load_session_for_api_resolved`); this only says so. Siblings are
    matched by splitting on the ``@`` separator, never by prefix: ``myshop``
    has no sibling ``myshopextra@bob``. A listing failure costs only the
    warning, never the load that already succeeded.

    Each bare name is checked once per process whatever the result, so a
    name lists at most once: a sibling created later in the same process, or
    one a failed listing could not see, goes unwarned.
    """
    if name in _bare_slot_sibling_checked:
        return
    _bare_slot_sibling_checked.add(name)
    try:
        cached = list_sessions()
    except Exception as exc:  # noqa: BLE001 (an advisory check never fails the load)
        LOG.debug("session_bare_slot_sibling_check_skipped", name=name, error=str(exc))
        return
    siblings = sorted(
        other
        for other in cached
        if split_session_name(other)[0] == name and split_session_name(other)[1] is not None
    )
    if not siblings:
        return
    LOG.warning(
        "session_bare_slot_shadows_labelled",
        name=name,
        labelled=siblings,
        hint=(
            f"loaded the bare slot '{name}'; load a labelled slot by its full name, "
            f"or run 'gp session clear {name}' if the bare slot is stale"
        ),
    )


def load_session_for_api_resolved(
    name: str, *, resolve: bool = True
) -> tuple[requests.Session, str]:
    """Load a cached session for API use AND report the name it came from.

    Same load as :func:`load_session_for_api` (see it for what the returned
    session carries and why no browser process starts); this variant returns
    the pair so a caller can key its write-backs off the slot that was
    actually loaded.

    **The pin contract.** A bare *name* (``"mysite"``) names a BASE, not a
    slot, and is resolved here, at load time:

    1. a slot cached under the bare name itself wins: an exact hit, which
       lists only to warn when labelled siblings exist (see below);
    2. otherwise the single labelled account under that base
       (:func:`graftpunk.session_identity.resolve_account_session`), at the
       cost of ONE ``list_sessions()`` call on this miss path only;
    3. otherwise :class:`~graftpunk.exceptions.AmbiguousSessionError` naming
       the candidates, or :class:`~graftpunk.exceptions.SessionNotFoundError`
       when nothing is cached for the base at all.

    A labelled *name* (``"mysite@alice"``) is exact: a miss is a miss, never
    another account's session.

    Because step 1 answers first, this deliberately does NOT raise where the
    CLI's unpinned resolution would: with both ``mysite`` and ``mysite@alice``
    cached, ``mysite`` loads the bare slot rather than refusing. It does log
    one ``session_bare_slot_shadows_labelled`` warning per bare name per
    process naming the labelled siblings (#197), at the cost of one
    ``list_sessions()`` call on the first such exact hit per bare name per
    process, siblings or not; with ``resolve=False`` the load is exact-only
    and lists nothing, so it does not warn.

    Whatever slot is loaded is the operating name for every write-back of that
    invocation: pass the returned name — never the requested one — to
    :func:`update_session_cookies`, which uses its argument literally (#182).
    The returned session also carries that name as a memory-only rider, so a
    write-back keyed off the bare base name follows it to the same slot (#174);
    passing the returned name explicitly says so at the call site.

    Args:
        name: Session name (without .session.pickle extension). May be a bare
            base name (``"mysite"``) or a full ``base@label`` name.
        resolve: Whether the bare-base fallback (step 2 above) applies. Pass
            ``False`` when *name* already came out of
            :func:`graftpunk.session_identity.compute_operating_session_name`'s
            resolution tier: it resolved against the same listing, so a second
            one would be a wasted round-trip and a miss is genuinely a miss.

    Returns:
        ``(session, loaded_name)`` — the API session, and the cache key it was
        loaded from (equal to *name* on an exact hit).

    Raises:
        ValueError: *name* is not a legal session name. Checked first: a name
            that cannot exist never reaches storage or a listing.
        SessionNotFoundError: Nothing is cached under *name* exactly and (with
            ``resolve=True``) no account is cached under it either; the
            original miss is kept as ``__cause__``.
        AmbiguousSessionError: Nothing is cached under *name* exactly and more
            than one cached session shares its base; the error names every
            candidate.
        SessionExpiredError: The session cannot be unpickled.
        ImportError: The browser stack needed to reconstruct the cached object
            is not installed (see :func:`load_session_for_api`).
    """
    validate_session_name(name)

    miss: SessionNotFoundError | None = None
    loaded_name = name
    try:
        browser_session = load_session(name)
    except SessionNotFoundError as exc:
        if not resolve:
            # INFO, not WARNING: this is invisible under the library's
            # WARNING default and useful when tracing with logging turned
            # up. The raised SessionNotFoundError is the signal a caller
            # acts on, not this log line (#178, PR #189 review).
            LOG.info("session_not_found_for_api", name=name, resolve=False)
            raise
        miss = exc

    if miss is not None:
        # Deliberately OUTSIDE the except block: an AmbiguousSessionError from
        # here is the whole story, and must not drag the miss along as
        # "During handling of the above exception..." (its __context__). The
        # miss itself is kept in `miss` and re-attached explicitly below.
        resolved = resolve_account_session(name, list_sessions())
        if resolved == name:
            LOG.info("session_not_found_for_api", name=name, resolve=True)
            raise SessionNotFoundError(no_account_cached_message(name)) from miss
        LOG.info("api_session_resolved_account", requested=name, resolved=resolved)
        browser_session = load_session(resolved)
        loaded_name = resolved
    elif resolve and split_session_name(name)[1] is None:
        _warn_if_bare_slot_shadows_labelled(name)

    header_roles = getattr(browser_session, "_gp_header_roles", {})
    api_session = _api_session_from_session(browser_session)
    # The copy inherits the source's stamp through SESSION_RIDER_ATTRS, so this
    # is belt and braces: it stamps the copy anyway, for a source object that
    # refused the stamp (_stamp_session_slot is advisory and swallows that).
    _stamp_session_slot(api_session, loaded_name)
    LOG.info(
        "created_api_session_from_cached_session",
        name=loaded_name,
        has_header_roles=bool(header_roles),
        role_count=len(header_roles),
        **({"requested": name} if loaded_name != name else {}),
    )
    return api_session, loaded_name


def load_session_for_api(name: str, *, resolve: bool = True) -> requests.Session:
    """Load cached session for API use (no browser launched).

    This extracts cookies and headers from a cached BrowserSession
    and creates a GraftpunkSession that can be used for API calls
    without launching a browser. If the cached session has header
    roles (captured during login), they are applied automatically.

    No browser *process* is started — but this does go through
    :func:`load_session`, which reconstructs the real cached object and so needs
    the browser *package* importable (on graftpunk-lite, its ``[browser]`` extra).
    For a browser-free load (under Pyodide or Cloudflare Workers, for example),
    use :func:`load_session_for_api_from_bytes`, which stubs the browser classes
    instead of importing them.

    A thin wrapper over :func:`load_session_for_api_resolved`, which owns the
    bare-name pin contract in full: since #176 ``gp <site> login`` writes
    sessions as ``base@label``, so a bare *name* is tried as an exact key
    first and only then resolved against the single cached account for that
    base, with one ``list_sessions()`` call on that miss path, and one on the
    first bare exact hit per name per process to warn about labelled
    siblings. A slot cached under the bare
    name itself always wins, so this differs from the CLI's unpinned
    resolution, which refuses when several accounts share a base.

    **If you will persist changes to the returned session** (cookies, tokens)
    with :func:`update_session_cookies`, prefer
    :func:`load_session_for_api_resolved` and key the write-back off the name
    it RETURNS, which states the operating slot at the call site. This function
    throws the loaded name away, which is the whole reason it is
    read-only-shaped. The returned session still carries the loaded slot name
    as a memory-only rider, so ``update_session_cookies(session, name)`` with
    the bare base name follows it to the slot the load landed on (#174).

    Args:
        name: Session name (without .session.pickle extension). May be a bare
            base name (``"mysite"``) or a full ``base@label`` name.
        resolve: Whether the bare-base fallback applies; ``False`` means exact
            only (see :func:`load_session_for_api_resolved`).

    Returns:
        GraftpunkSession with cookies, headers, and header roles
        from the cached session.

    Raises:
        ValueError: *name* is not a legal session name.
        SessionNotFoundError: Nothing is cached under *name* exactly and no
            account is cached under it either.
        AmbiguousSessionError: Nothing is cached under *name* exactly and more
            than one cached session shares its base; the error names every
            candidate.
        SessionExpiredError: If session cannot be unpickled.
        ImportError: If the browser stack needed to reconstruct the cached
            object is not installed (see note above).
    """
    api_session, _loaded_name = load_session_for_api_resolved(name, resolve=resolve)
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


def _write_back_target(api_session: requests.Session, session_name: str) -> str:
    """The slot a write-back of *api_session* under *session_name* falls back to
    when no slot is cached under *session_name* itself.

    The answer is *session_name* itself in every case but one: the session was
    last loaded from or saved to a slot in *session_name*'s own family, and
    *session_name* is the bare base of that family. Then the write follows the
    stamped slot, because that is the session the caller actually holds (#174).

    A labelled *session_name*, a stamp from a different base, and an unstamped
    session all stay literal, and nothing here lists or resolves. The caller
    consults this only after the literal slot has already missed, so an existing
    bare slot is never passed over.
    """
    # A non-string stamp is not a slot name (a test double answers every
    # getattr), and only a real one may redirect a write.
    loaded_name = getattr(api_session, GP_SESSION_NAME_ATTR, None)
    if not isinstance(loaded_name, str) or not loaded_name or loaded_name == session_name:
        return session_name
    if split_session_name(loaded_name)[0] != session_name:
        return session_name

    LOG.info(
        "session_write_back_follows_loaded_slot",
        requested=session_name,
        target=loaded_name,
    )
    return loaded_name


def update_session_cookies(
    api_session: requests.Session, session_name: str, *, key: bytes | None = None
) -> None:
    """Persist an API session's cookies and token cache back to the session cache.

    Loads the original cached session (preserving browser metadata),
    updates its cookies and token cache from the API session, and saves it back.
    This is best-effort — failures are logged but do not raise.

    *session_name* is tried LITERALLY first, and a slot cached under it wins
    outright, including a bare ``mysite`` slot. Only when that name has no slot
    of its own does the one fallback apply (:func:`_write_back_target`): a bare
    *session_name* whose family the session was last loaded from or saved to
    writes to that stamped slot, so ``mysite`` refreshes the ``mysite@alice``
    the session came from. That covers the author-facing pair
    ``update_session_cookies(plugin.get_session(), plugin.session_name)``, whose
    refresh used to be dropped with only a log line (#174). Everything else is
    verbatim: a bare name that names an existing slot, a labelled name, a
    session stamped with another base, and an unstamped session all write
    exactly where they are told. This can therefore never fork a bare ``mysite``
    slot open beside the ``mysite@alice`` a session was loaded from, and it
    performs no listing and no account resolution.

    The stamp records the last load or save, so it is the slot the caller's
    object currently corresponds to. A bare name with neither a slot of its own
    nor a stamped one is still a logged no-op, so callers that hold the
    operating name from the load (``load_session_for_api_resolved``'s second
    element, the CLI's resolved operating name, or ``ctx.save_session()``) keep
    passing it (#182): it states the target at the call site.

    Args:
        api_session: The API session with potentially updated cookies and token cache.
        session_name: Name of the cached session to update, used verbatim unless
            no slot exists under it and it is the bare base of the slot
            *api_session* was last loaded from or saved to.
        key: Optional raw Fernet key, for a host without the key file: the
            stored session is read and written back with it. None uses the
            configured key.
    """
    target = session_name
    try:
        try:
            original, stored_metadata = _load_session_with_metadata(session_name, key=key)
        except SessionNotFoundError:
            # The literal name answers first, so an existing slot is never
            # passed over; only its absence lets the session's own stamp name
            # the target (#174). A re-raise here lands in the warning below.
            target = _write_back_target(api_session, session_name)
            if target == session_name:
                raise
            original, stored_metadata = _load_session_with_metadata(target, key=key)
    except Exception as exc:  # noqa: BLE001 — best-effort save
        LOG.warning(
            "session_save_skipped_load_failed",
            session_name=target,
            requested=session_name,
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
        # identifier already returned by this load's metadata (stored_metadata,
        # from the same backend read — no extra backend call) so identity
        # survives this refresh either way.
        # A slot under the current account rule already took its account from
        # the metadata at load (#293).
        if getattr(original, GP_ACCOUNT_ATTR, None) is None and stored_metadata.account_identifier:
            setattr(original, GP_ACCOUNT_ATTR, stored_metadata.account_identifier)
        cache_session(original, target, account_rule=stored_metadata.account_rule, key=key)
        LOG.info("session_cookies_updated", session_name=target)
    except Exception as exc:  # noqa: BLE001 — best-effort save
        LOG.warning(
            "session_save_failed",
            session_name=target,
            requested=session_name,
            error=str(exc),
        )


def list_sessions(backend_override: str | None = None) -> list[str]:
    """List all cached session names.

    Args:
        backend_override: If set, use this backend type instead of the default.

    Returns:
        Sorted list of session names.
    """
    backend = _get_session_storage_backend(backend_override=backend_override)
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


def list_incomplete_sessions(backend_override: str | None = None) -> list[str]:
    """Slots holding objects but no complete session, which no listing shows.

    A crash during a save, an older release, or a hand-made copy beside a
    slot's files can leave one. Clearing every session removes them.
    """
    backend = _get_session_storage_backend(backend_override=backend_override)
    return backend.incomplete_sessions()


def remove_session_objects(name: str, backend_override: str | None = None) -> list[str]:
    """Delete everything stored under the slot *name*; return the keys or paths deleted.

    Unlike :func:`clear_session_cache`, which returns session names, this
    reports each object, so a caller can show that nothing was left behind.

    Raises:
        ValueError: *name* is not a session name (``""``, ``.``, ``..``, a
            path separator), before any backend is created or called.
    """
    check_slot_name(name)
    backend = _get_session_storage_backend(backend_override=backend_override)
    return backend.remove_session(name)


class ClearedSessions(list[str]):
    """The session names a clear removed, as a list, with ``skipped``: the names a
    sweep left in place (an unsafe name, a slot resolving outside the sessions
    directory, a storage error, or files a delete left behind), each logged once."""

    def __init__(self, removed: list[str], skipped: list[str] | None = None) -> None:
        super().__init__(removed)
        self.skipped: tuple[str, ...] = tuple(skipped or ())


def clear_session_cache(
    session_name: str | None = None,
    backend_override: str | None = None,
) -> ClearedSessions:
    """Clear cached sessions.

    Clearing every session never stops partway: a slot that cannot be cleared
    safely or completely is logged once (``session_not_cleared``), left out of
    the result, and named in ``skipped``, and the sweep goes on.

    Args:
        session_name: If provided, clear only this session. Otherwise, clear all.
        backend_override: If set, use this backend type instead of the default.

    Returns:
        The removed session names (a list), with ``skipped``.

    Raises:
        ValueError: *session_name* is unsafe to delete under
            (:func:`graftpunk.storage.base.check_slot_name`).
        StorageError: The backend failed clearing *session_name*.
    """
    backend = _get_session_storage_backend(backend_override=backend_override)
    removed: list[str] = []

    if session_name:
        # Clear specific session
        if backend.delete_session(session_name):
            removed.append(session_name)
        return ClearedSessions(removed)

    # Clear all sessions, and the slots a failed save left without metadata:
    # they are not listed, but loading one says to clear.
    skipped: list[str] = []
    leftovers = list_incomplete_sessions(backend_override=backend_override)
    for name in [*backend.list_sessions(), *leftovers]:
        try:
            # check_slot_name refuses a name under which deleting would be
            # unsafe; the local backend also refuses a slot resolving outside
            # the sessions directory.
            deleted = backend.delete_session(name)
        except (ValueError, StorageError) as exc:
            LOG.warning("session_not_cleared", name=name, error=str(exc))
            skipped.append(name)
            continue
        if deleted:
            removed.append(name)

    return ClearedSessions(removed, skipped)


def copy_session(
    source: str, dest: str, *, account_identifier: str | None, key: bytes | None = None
) -> None:
    """Copy the session cached under *source* to *dest*, recording *account_identifier*.

    The session is unpickled and pickled again, so the names and identifier it
    carries inside its pickle are replaced too: ``BrowserSession`` keeps its
    session name and account identifier there. Created, modified and expiry
    times and status are kept, since it is the same session under another
    name; the metadata records the current account rule. The copy is read back
    and its checksum compared before this returns, so a caller may delete
    *source* afterwards. *source* equal to *dest* rewrites the slot in place.
    *source* itself is never deleted here.

    *key*, when given, is the raw Fernet key both slots are read and written
    with, for a host without the key file; None uses the configured key.

    Raises:
        ValueError: *dest* is not a legal session name.
        SessionNotFoundError: Nothing is cached under *source*.
        SessionExpiredError: *source* is expired or cannot be read, or the copy
            did not read back as written.
        ImportError: *source* needs the browser stack, which is not installed.
        MissingEncryptionKeyError: No key was given and there is none to read.
        Exception: Whatever the backend raises when saving *dest*.
    """
    validate_session_name(dest)
    backend = _get_session_storage_backend()
    session, metadata = _load_session_with_metadata(source, key=key)
    setattr(session, GP_ACCOUNT_ATTR, account_identifier)
    if hasattr(session, "_session_name"):
        setattr(session, "_session_name", dest)  # noqa: B010 (the name a BrowserSession pickles)
    _stamp_session_slot(session, dest)
    pickled_data = pickle.dumps(session)
    checksum = hashlib.sha256(pickled_data).hexdigest()
    copied = replace(
        metadata,
        name=dest,
        checksum=checksum,
        account_identifier=account_identifier,
        account_rule=ACCOUNT_RULE,
    )
    backend.save_session(dest, encrypt_data(pickled_data, key=key), copied)
    stored_data, stored_metadata = backend.load_session(dest)
    if (
        stored_metadata.checksum != checksum
        or hashlib.sha256(decrypt_data(stored_data, key=key)).hexdigest() != checksum
    ):
        raise SessionExpiredError(f"Session '{dest}' did not read back as written.")


def set_session_account(name: str, account_identifier: str | None) -> bool:
    """Record *account_identifier* on *name* under the current account rule.

    Only the metadata is rewritten: the session is neither loaded nor saved,
    so an expired or unreadable slot is updated too. A later write-back takes
    the identifier from this metadata rather than from the pickle
    (:func:`update_session_cookies`). Returns False when nothing is cached
    under *name*.
    """
    backend = _get_session_storage_backend()
    return backend.update_session_metadata(
        name, account_identifier=account_identifier, account_rule=ACCOUNT_RULE
    )


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
