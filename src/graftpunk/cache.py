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
import importlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable
from urllib.parse import urlparse

import dill as pickle
import requests

from graftpunk._distribution import install_command
from graftpunk.config import get_settings
from graftpunk.encryption import decrypt_data, encrypt_data, resolve_key
from graftpunk.exceptions import (
    EncryptionError,
    GraftpunkError,
    InvalidSessionRecord,
    RealSessionClassUnavailable,
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
from graftpunk.session_legacy import NoSessionStateError, state_from_pickle
from graftpunk.session_record import (
    LegacyBytes,
    Record,
    Unrecordable,
    classify,
    kind_of,
    record_to_state,
    refusal_before_state,
    state_to_record,
)
from graftpunk.session_riders import SESSION_CLASS_PATHS
from graftpunk.session_view import StoredSession
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

# The name errors give the session load_session_for_api_from_bytes reads, which has none.
_BYTES_LABEL = "<bytes>"

# What a write-back skipped for want of the browser stack tells the user.
_WRITE_BACK_REWRITE_HINT = (
    "the next login, or a write-back from a full install, rewrites the session"
)


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
    from graftpunk.cookies import cookie_domains, domain_from_cookies

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
        metadata["cookie_domains"] = cookie_domains(cookie_list)
    else:
        metadata["cookie_count"] = 0
        metadata["cookie_domains"] = []

    # Fallback: infer domain from cookie domains when current_url is missing
    if "domain" not in metadata and cookies and (domain := domain_from_cookies(cookies)):
        metadata["domain"] = domain

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
    :mod:`graftpunk.session_riders`): every loader that has a key, and the saver, set it
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


def _encode_fresh(session: Any, *, name: str | None = None) -> bytes:
    """The bytes a fresh save of *session* stores, in the configured format.

    Under ``record`` a session the codec cannot hold is still saved, as a
    pickle, with one ``session_not_recordable`` warning; the setting never
    makes a save fail.
    """
    if get_settings().session_format == "record":
        kind = kind_of(session)
        if kind is None:
            cls = type(session)
            refusal: Unrecordable | bytes = Unrecordable(
                "type", f"{cls.__module__}.{cls.__qualname__} is not a stored session class"
            )
        else:
            # pickle.dumps below calls __getstate__ again: a pickle cannot be
            # built from a state dict this side of the object's own reduce.
            refusal = refusal_before_state(kind, session) or state_to_record(
                kind, session.__getstate__()
            )
        if isinstance(refusal, bytes):
            return refusal
        LOG.warning(
            "session_not_recordable",
            name=name,
            key=refusal.key,
            reason=refusal.reason,
            hint="the session is saved as a pickle instead",
        )
    return pickle.dumps(session)


def _encode_as(session: Any) -> bytes:
    """The bytes for re-saving *session* in the format it was loaded from.

    A :class:`StoredSession` was loaded from a record and is written as one; any
    other object was unpickled whole and is written as a pickle. Reads no
    setting.

    Raises:
        TypeError: *session* is a view with no stored kind, which is never written.
        ValueError: A view holds a value a record cannot (the message names the key).
    """
    if not isinstance(session, StoredSession):
        return pickle.dumps(session)
    if not session.writable or session.kind is None:
        raise TypeError("a stored session with no kind cannot be written")
    encoded = state_to_record(session.kind, session.stored_state(), session.extra)
    if isinstance(encoded, Unrecordable):
        raise ValueError(f"cannot write {encoded.key!r} to a record: {encoded.reason}")
    return encoded


def _store(
    name: str,
    payload: bytes,
    session: Any,
    *,
    account_rule: int | None,
    key: bytes | None,
    metadata: SessionMetadata | None = None,
) -> str:
    """Checksum, encrypt and save *payload* under *name*; return the location.

    With no *metadata*, new metadata is built from *session* (new times,
    status ``active``). A given *metadata* is saved as it is, with only its
    checksum replaced.
    """
    checksum = hashlib.sha256(payload).hexdigest()
    encrypted = encrypt_data(payload, key=key)
    if metadata is None:
        raw = _extract_session_metadata(session, name)
        now = datetime.now(UTC)
        metadata = SessionMetadata(
            name=name,
            checksum=checksum,
            created_at=now,
            modified_at=now,
            expires_at=now + timedelta(hours=get_settings().session_ttl_hours),
            domain=raw.get("domain"),
            current_url=raw.get("current_url"),
            cookie_count=raw.get("cookie_count", 0),
            cookie_domains=raw.get("cookie_domains", []),
            status="active",
            account_identifier=raw.get("account_identifier"),
            account_rule=account_rule,
        )
    else:
        metadata = replace(metadata, checksum=checksum)
    return _get_session_storage_backend().save_session(name, encrypted, metadata)


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

    settings = get_settings()

    try:
        LOG.info("caching_session", name=session_name, backend=settings.storage_backend)
        payload = _encode_fresh(session, name=session_name)
        location = _store(session_name, payload, session, account_rule=account_rule, key=key)
        # Stamp the in-memory object with the slot it now lives in, so a later
        # write-back through this object knows where it belongs (#174). Set
        # after the payload above on purpose: the name describes this copy, not
        # the bytes, and the blob must stay free of it.
        _stamp_session_slot(session, session_name)
        _recheck_bare_slot_after_write(session_name)
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
        NewerSessionRecord: The session was stored by a newer release.
        RealSessionClassUnavailable: The class the session was saved as cannot
            be imported (a browser session on graftpunk-lite without
            ``[browser]``); an ``ImportError`` carrying the install hint.
        MissingEncryptionKeyError: No key was given and the configured backend
            has no key file to read (see :func:`graftpunk.encryption.get_encryption_key`).
    """
    stored, metadata = _read_stored(name, key=key, backend_override=backend_override)
    session = _restore(stored, name=name)
    _finish_loaded(session, name, metadata)
    return session


def _read_stored(
    name: str, *, key: bytes | None, backend_override: str | None = None
) -> tuple[Record | LegacyBytes, SessionMetadata]:
    """The session stored under *name*, decrypted, checksum-checked and
    classified, with the ``SessionMetadata`` read in the same backend round-trip.

    Raises:
        SessionNotFoundError: No session is stored under *name*.
        SessionExpiredError: The session cannot be decrypted, fails its
            checksum, or is an invalid record.
        NewerSessionRecord: The session is a record of a newer version.
        MissingEncryptionKeyError: No key was given and there is none to read.
        EncryptionError: The key cannot be obtained (Vault unreachable, the
            supabase package or settings missing) or is malformed. Both are
            passed through, not reported as an expired session, because the
            fix is to provide a good key, not to clear the session.
    """
    backend = _get_session_storage_backend(backend_override=backend_override)
    settings = get_settings()

    try:
        encrypted_data, metadata = backend.load_session(name)
    except RuntimeError as exc:
        _log_load_failure(name, exc)
        raise SessionExpiredError(f"Failed to load session '{name}': {exc}") from exc

    # Obtain and check the key outside the conversion below: a key that is
    # missing, unreachable or malformed is fixed by providing a good one,
    # never by clearing the session (on a shared bucket, every host's).
    # Only a key that fails to decrypt these bytes means the session is
    # unreadable.
    fernet_key = resolve_key(key)
    try:
        decrypted_data = decrypt_data(encrypted_data, key=fernet_key)
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

    try:
        return _classified(decrypted_data, name=name), metadata
    except SessionExpiredError as exc:
        _log_load_failure(name, exc)
        raise


def _log_load_failure(name: str, exc: BaseException) -> None:
    # Only for a load from the store: the bytes loader has no backend to name,
    # and its failures have never been logged, only raised.
    LOG.error(
        "failed_to_load_session",
        name=name,
        error=str(exc),
        backend=get_settings().storage_backend,
    )


def _classified(decrypted: bytes, *, name: str) -> Record | LegacyBytes:
    """:func:`classify`, reporting an invalid record as an expired session, as
    a corrupt pickle is. ``NewerSessionRecord`` propagates: the release that
    wrote the record can still read it, so it must not be cleared as expired.
    """
    try:
        return classify(decrypted)
    except InvalidSessionRecord as exc:
        raise SessionExpiredError(f"Failed to load session '{name}': {exc}") from exc


def _restore(stored: Record | LegacyBytes, *, name: str) -> Any:
    """The object that was saved: a record restored as its class, a pickle unpickled whole."""
    if isinstance(stored, Record):
        return _restore_record(stored, name=name)
    return _unpickle_whole(stored, name=name)


def _load_for_write_back(stored: Record | LegacyBytes, *, name: str) -> Any:
    """The object a write-back changes and saves again: a record as a
    :class:`StoredSession`, which needs no session class and is written back as
    a record with its unknown keys, and a legacy pickle unpickled whole, which
    is written back as a pickle (:func:`_encode_as`). Reads no setting.

    Raises:
        RealSessionClassUnavailable: A legacy pickle needs the browser stack,
            which is not installed.
        SessionExpiredError: A legacy pickle that does not unpickle.
    """
    if isinstance(stored, Record):
        return StoredSession.from_record(stored)
    return _unpickle_whole(stored, name=name)


def _real_class_unavailable(name: str, exc: ImportError) -> RealSessionClassUnavailable:
    # Info, not error: the exception carries the same facts, and the CLI turns
    # it into the install hint, so an error line here would only repeat it
    # above that hint.
    LOG.info("failed_to_load_session_missing_browser_stack", name=name, error=str(exc))
    return RealSessionClassUnavailable(
        f"Loading the cached session '{name}' requires the browser automation "
        f"stack, which is not installed. Install it with: "
        f"{install_command('browser')}\n"
        f"(To read cookies/headers from a session WITHOUT the browser stack, use "
        f"load_session_for_api or load_session_for_api_from_bytes.)\n"
        f"(underlying import failure: {exc})"
    )


def _unpickle_whole(legacy: LegacyBytes, *, name: str) -> Any:
    """Unpickle a legacy session with its real classes, so no state it holds is lost.

    Deliberately the plain unpickler, not the placeholder one: the object is
    handed back to be changed and saved again (``update_session_cookies``),
    and a placeholder saved back would lose the session's browser state.

    Raises:
        RealSessionClassUnavailable: A module the pickle names is not
            installed (the browser stack on graftpunk-lite); the
            ``ModuleNotFoundError`` is its cause, where
            ``_distribution.missing_browser_module`` finds it.
        SessionExpiredError: The bytes do not unpickle, or a class's
            ``__setstate__`` refuses the state they hold.
    """
    try:
        # Fernet authenticated these bytes; the key is the trust boundary.
        return pickle.loads(legacy.data)  # noqa: S301
    except ModuleNotFoundError as exc:
        # A BrowserSession pickle names graftpunk.session, which imports the
        # browser stack; without this the failure escapes raw as
        # "No module named 'httpie'".
        raise _real_class_unavailable(name, exc) from exc
    except (ImportError, GraftpunkError):
        # Any other import failure is an install problem, and a graftpunk
        # error a class's __setstate__ raises already says what is wrong;
        # neither is a session to clear. (A class missing from a module that
        # imports raises AttributeError, and is reported below with its text.)
        raise
    except Exception as exc:  # noqa: BLE001 (a state its class refuses is an unusable session)
        _log_load_failure(name, exc)
        raise SessionExpiredError(f"Failed to load session '{name}': {exc}") from exc


def _restore_record(record: Record, *, name: str) -> Any:
    """*record* restored as the class its kind names, through that class's own
    ``__setstate__``, so the restore logic exists once.

    Raises:
        RealSessionClassUnavailable: The class cannot be imported (kind
            ``browser`` on graftpunk-lite); the import failure is its cause.
    """
    module, qualname = next(
        path for path, kind in SESSION_CLASS_PATHS.items() if kind == record.kind
    )
    try:
        cls = getattr(importlib.import_module(module), qualname)
    except ImportError as exc:
        raise _real_class_unavailable(name, exc) from exc
    obj = cls.__new__(cls)
    obj.__setstate__(record_to_state(record))
    return obj


def _invalid_structure(name: str) -> SessionExpiredError:
    if name == _BYTES_LABEL:
        # The caller holds bytes, not a store slot it could clear.
        return SessionExpiredError(
            "Session bytes have invalid structure: they hold no usable cookies and headers."
        )
    return SessionExpiredError(
        f"Session '{name}' has invalid structure. Run 'gp clear' and re-login."
    )


def _check_structure(session: Any, name: str) -> None:
    """Refuse a loaded object without readable cookies and headers.

    Reading is the check: a view's ``cookies`` rebuilds a jar from what the
    state holds, so a value that is not one fails here with whatever it
    raises, not only ``AttributeError``.
    """
    try:
        session.cookies  # noqa: B018
        session.headers  # noqa: B018
    except Exception as exc:  # noqa: BLE001 (any failure to read them is invalid structure)
        raise _invalid_structure(name) from exc


def _api_session_of(source: Any, name: str) -> "GraftpunkSession":
    """:func:`_api_session_from_session`, reporting contents it cannot use as
    an expired session rather than whatever they raised."""
    try:
        return _api_session_from_session(source)
    except ImportError:
        # A module the builder imports is missing: an install problem.
        raise
    except Exception as exc:  # noqa: BLE001 (structurally present but malformed contents)
        if name == _BYTES_LABEL:
            raise SessionExpiredError(
                f"Session bytes have unusable cookies/headers: {exc}"
            ) from exc
        _log_load_failure(name, exc)
        raise SessionExpiredError(f"Session '{name}' has unusable cookies/headers: {exc}") from exc


def _finish_loaded(session: Any, name: str, metadata: SessionMetadata) -> None:
    """What a load from the store does to the object it read, real or a view."""
    _check_structure(session, name)
    # The slot this object came out of, for any write-back that follows
    # (#174). Set here rather than in the stored bytes so it can never be stale.
    _stamp_session_slot(session, name)
    # Metadata recorded under the current rule is the authority on the
    # account: a login may have replaced a 1.24.0 record there without
    # touching the stored session (set_session_account). Taking it here means
    # any save of this copy writes the replaced value, and the stored session
    # loses the old one (#293).
    if metadata.account_rule == ACCOUNT_RULE:
        with contextlib.suppress(Exception):
            setattr(session, GP_ACCOUNT_ATTR, metadata.account_identifier)
    LOG.info("successfully_loaded_session", name=name, backend=get_settings().storage_backend)


def _stored_view(stored: Record | LegacyBytes, *, name: str) -> StoredSession:
    """A stored session as a view: a record's state, or the state dict a legacy
    pickle holds, read without importing the session classes.

    Raises:
        SessionExpiredError: The pickle does not unpickle, or holds no
            cookies and headers.
        ImportError, GraftpunkError: Raised by a class the pickle names while
            it is restored, as :func:`load_session` lets them propagate.
    """
    if isinstance(stored, Record):
        return StoredSession.from_record(stored)
    try:
        legacy = state_from_pickle(stored.data)
    except NoSessionStateError as exc:
        raise _invalid_structure(name) from exc
    except (ImportError, GraftpunkError):
        # find_class stubs a module that is not installed, so an ImportError
        # here came from code the payload ran: an install problem, as a
        # graftpunk error already says what is wrong. Neither is a session to
        # clear.
        raise
    except Exception as exc:  # noqa: BLE001 (any unpickling failure is an unusable session)
        raise SessionExpiredError(f"Failed to deserialize session '{name}': {exc}") from exc
    if legacy.unresolved:
        LOG.debug("session_classes_stubbed", name=name, classes=list(legacy.unresolved))
    if "cookies" not in legacy.state or "headers" not in legacy.state:
        # A view reads an absent key as a default, so _check_structure would
        # pass it; the state itself is what lacks them.
        raise _invalid_structure(name)
    return StoredSession(legacy.kind, legacy.state)


def _load_view(name: str, *, key: bytes | None) -> StoredSession:
    """The session stored under *name* as a view, with the loaded-object steps applied."""
    stored, metadata = _read_stored(name, key=key)
    try:
        view = _stored_view(stored, name=name)
    except SessionExpiredError as exc:
        _log_load_failure(name, exc)
        raise
    _finish_loaded(view, name, metadata)
    return view


def _api_session_from_session(source: SessionLike) -> "GraftpunkSession":
    """Build a browser-free GraftpunkSession from a session-like object
    (the module's SessionLike Protocol: cookies + headers), copying header
    roles and token caches when present.

    The one builder of an API session: every API loader hands it a
    :class:`~graftpunk.session_view.StoredSession` of the stored session.
    """
    from graftpunk.client_headers import is_library_default
    from graftpunk.cookies import use_browser_cookie_policy
    from graftpunk.graftpunk_session import GraftpunkSession, replayable_roles
    from graftpunk.session_riders import _HEADER_ROLES_ATTR, SESSION_RIDER_ATTRS

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
        # Copy headers from the saved session, but skip the HTTP library's own
        # defaults, whichever requests version saved them: a saved
        # User-Agent: python-requests/2.x would overwrite the browser UA that
        # _apply_browser_identity() set during GraftpunkSession init (#52, #399),
        # and a session before 1.41.0 can carry Chrome's Accept-Encoding (#370).
        # Also skip ephemeral security headers (e.g. X-CSRF-TOKEN from WAFs like
        # Akamai Bot Manager) that are per-request and would cause stale-blob
        # rejections if replayed.
        for key, value in source.headers.items():
            if is_library_default(key, value):
                continue
            if key.lower() in _EPHEMERAL_HEADERS:
                LOG.debug("skipped_ephemeral_header", header=key)
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


# Bare names _warn_if_bare_slot_shadows_labelled has listed for, with or
# without siblings, so a bare name costs one successful listing per process
# and a long-running process does not warn on every load (#197). cache_session
# drops a name when it writes a labelled slot under it, so a sibling this
# process creates is seen by the next load.
_bare_slot_sibling_checked: set[str] = set()


def _recheck_bare_slot_after_write(name: str) -> None:
    """After writing slot *name*, let the next load of its bare base look for
    siblings again when *name* is labelled: it is a new sibling (#197)."""
    base, label = split_session_name(name)
    if label is not None:
        _bare_slot_sibling_checked.discard(base)


def _warn_if_bare_slot_shadows_labelled(name: str) -> None:
    """Check once per process whether the loaded bare slot *name* has labelled siblings, and warn.

    Since #176 ``gp <site> login`` writes ``base@label`` slots, so a bare slot
    that still answers a bare-name load is usually the stale one from before
    the first labelled login. The load keeps returning it (the pin contract in
    :func:`load_session_for_api_resolved`); this only says so. Siblings are
    matched by splitting on the ``@`` separator, never by prefix: ``myshop``
    has no sibling ``myshopextra@bob``. A listing failure costs only the
    warning, never the load that already succeeded.

    A name is marked checked once a listing succeeds, siblings or not, so
    it lists once per process; a failed listing is tried again on the next
    load. A labelled slot this process caches under the name clears the
    mark (:func:`cache_session`). A sibling another process creates later
    goes unwarned until this process restarts.
    """
    if name in _bare_slot_sibling_checked:
        return
    try:
        cached = list_sessions()
    except Exception as exc:  # noqa: BLE001 (an advisory check never fails the load)
        LOG.debug("session_bare_slot_sibling_check_skipped", name=name, error=str(exc))
        return
    _bare_slot_sibling_checked.add(name)
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
            f"or run 'gp session clear {name}' if the bare slot is stale. Anything "
            f"that reads the slot '{name}' by that exact name, outside graftpunk's "
            "resolution, stops working once it is cleared, so move such readers to "
            "the labelled name first; add --force where the clear runs unattended"
        ),
    )


def load_session_for_api_resolved(
    name: str, *, resolve: bool = True, key: bytes | None = None
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
    invocation: pass the returned name (never the requested one) to
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
        key: Optional raw Fernet key to decrypt with, for a host without
            graftpunk's key file or vault; None uses the configured key.

    Returns:
        ``(session, loaded_name)``: the API session, and the cache key it was
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
        SessionExpiredError: The stored session cannot be decrypted or read.
        NewerSessionRecord: The session was stored by a newer release.
    """
    validate_session_name(name)

    miss: SessionNotFoundError | None = None
    loaded_name = name
    try:
        browser_session = _load_view(name, key=key)
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
        browser_session = _load_view(resolved, key=key)
        loaded_name = resolved
    elif resolve and split_session_name(name)[1] is None:
        _warn_if_bare_slot_shadows_labelled(name)

    header_roles = getattr(browser_session, "_gp_header_roles", {})
    api_session = _api_session_of(browser_session, loaded_name)
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


def load_session_for_api(
    name: str, *, resolve: bool = True, key: bytes | None = None
) -> requests.Session:
    """Load cached session for API use (no browser launched).

    This extracts cookies and headers from a cached BrowserSession
    and creates a GraftpunkSession that can be used for API calls
    without launching a browser. If the cached session has header
    roles (captured during login), they are applied automatically.

    No browser is started and no browser package is imported: the stored
    session is read as plain state, whether it was stored as a record or as a
    pickle, so this works on graftpunk-lite without its ``[browser]`` extra.

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
        key: Optional raw Fernet key; None uses the configured key.

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
        SessionExpiredError: The stored session cannot be decrypted or read.
        NewerSessionRecord: The session was stored by a newer release.
    """
    api_session, _loaded_name = load_session_for_api_resolved(name, resolve=resolve, key=key)
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
        encrypted: the Fernet-encrypted stored session (a record or a pickle).
        key: optional raw Fernet key. When given, decrypt with it directly,
            for environments where graftpunk's key file/vault does not exist
            (a Worker holding the key as a secret). When None, use the normal
            `decrypt_data` key sources.

    Security note: integrity and authenticity come SOLELY from the Fernet key:
    a valid MAC is the only thing standing between these bytes and the
    unpickler a legacy pickle goes through, which resolves whatever classes the
    payload names (see :mod:`graftpunk.session_legacy`). The structure check is a shape guard,
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
        SessionExpiredError: if the decrypted payload cannot be deserialized, is
            an invalid record, or lacks the expected session structure.
        NewerSessionRecord: the session was stored by a newer release.
    """
    # EncryptionError from decrypt_data intentionally propagates (see Raises).
    decrypted = decrypt_data(encrypted, key=key)
    source = _stored_view(_classified(decrypted, name=_BYTES_LABEL), name=_BYTES_LABEL)
    # No name and no metadata, so none of _finish_loaded: the session stays unstamped.
    _check_structure(source, _BYTES_LABEL)

    return _api_session_of(source, _BYTES_LABEL)


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
    updates its cookies and token cache from the API session, and saves it back
    in the format it was stored in, whatever ``GRAFTPUNK_SESSION_FORMAT`` says:
    a record stays a record with its unknown keys (and needs no browser stack),
    and a pickle stays a pickle. This is best-effort: failures are logged
    (``session_save_skipped_load_failed`` or ``session_save_failed``, each with
    a ``reason``) but do not raise.

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
            stored, stored_metadata = _read_stored(session_name, key=key)
        except SessionNotFoundError:
            # The literal name answers first, so an existing slot is never
            # passed over; only its absence lets the session's own stamp name
            # the target (#174). A re-raise here lands in the warning below.
            target = _write_back_target(api_session, session_name)
            if target == session_name:
                raise
            stored, stored_metadata = _read_stored(target, key=key)
        original = _load_for_write_back(stored, name=target)
        _finish_loaded(original, target, stored_metadata)
    except Exception as exc:  # noqa: BLE001 (best-effort save)
        hint = (
            {"hint": _WRITE_BACK_REWRITE_HINT}
            if isinstance(exc, RealSessionClassUnavailable)
            else {}
        )
        LOG.warning(
            "session_save_skipped_load_failed",
            session_name=target,
            requested=session_name,
            error=str(exc),
            reason=type(exc).__name__,
            **hint,
        )
        return

    try:
        from graftpunk.session_riders import _CACHE_ATTR, _CSRF_TOKENS_ATTR

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
        # from the same backend read, with no extra backend call) so identity
        # survives this refresh either way.
        # A slot under the current account rule already took its account from
        # the metadata at load (#293).
        if getattr(original, GP_ACCOUNT_ATTR, None) is None and stored_metadata.account_identifier:
            setattr(original, GP_ACCOUNT_ATTR, stored_metadata.account_identifier)
        # The guard and events cache_session gave this save through 1.44.0.
        validate_session_name(target)
        LOG.info("caching_session", name=target, backend=get_settings().storage_backend)
        try:
            # A record is written back as a record and a pickle as a pickle,
            # whatever the format setting. A value a record cannot hold raises
            # here, before anything is written.
            payload = _encode_as(original)
            location = _store(
                target, payload, original, account_rule=stored_metadata.account_rule, key=key
            )
        except (pickle.PickleError, RuntimeError, OSError) as exc:
            LOG.error("failed_to_cache_session", name=target, error=str(exc))
            raise
        _recheck_bare_slot_after_write(target)
        LOG.info("wrote_session_to_backend", name=target, location=location)
        LOG.info("session_cookies_updated", session_name=target)
    except Exception as exc:  # noqa: BLE001 (best-effort save)
        LOG.warning(
            "session_save_failed",
            session_name=target,
            requested=session_name,
            error=str(exc),
            reason=type(exc).__name__,
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

    The session is read and written again in the format it is stored in (a
    record stays a record, unknown keys included, and needs no browser stack;
    a pickle stays a pickle), so the name and identifier it carries inside are
    replaced too: a browser session keeps its session name and account
    identifier there. Created, modified and expiry
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
        NewerSessionRecord: *source* was stored by a newer release.
        ImportError: *source* is a pickle that needs the browser stack, which
            is not installed (``RealSessionClassUnavailable``).
        MissingEncryptionKeyError: No key was given and there is none to read.
        Exception: Whatever the backend raises when saving *dest*.
    """
    validate_session_name(dest)
    backend = _get_session_storage_backend()
    stored, metadata = _read_stored(source, key=key)
    session = _load_for_write_back(stored, name=source)
    _finish_loaded(session, source, metadata)
    setattr(session, GP_ACCOUNT_ATTR, account_identifier)
    # A kind whose stored state holds no name gains none: there the attribute
    # stays on this object and is not written.
    session.session_name = dest
    _stamp_session_slot(session, dest)
    payload = _encode_as(session)
    checksum = hashlib.sha256(payload).hexdigest()
    _store(
        dest,
        payload,
        session,
        account_rule=ACCOUNT_RULE,
        key=key,
        metadata=replace(
            metadata, name=dest, account_identifier=account_identifier, account_rule=ACCOUNT_RULE
        ),
    )
    _recheck_bare_slot_after_write(dest)
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
