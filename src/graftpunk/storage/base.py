"""Base protocols and data classes for session storage backends."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from typing import Any


def parse_datetime_iso(value: str | None) -> datetime | None:
    """Parse ISO datetime string to datetime with UTC timezone.

    Handles both standard ISO format and Supabase format (with Z suffix).
    Returns None for empty/None values. Makes naive datetimes UTC-aware.

    Args:
        value: ISO datetime string, or None

    Returns:
        datetime with UTC timezone, or None if value is empty/None

    Examples:
        >>> parse_datetime_iso("2024-01-15T12:00:00+00:00")
        datetime.datetime(2024, 1, 15, 12, 0, tzinfo=datetime.timezone.utc)
        >>> parse_datetime_iso("2024-01-15T12:00:00Z")
        datetime.datetime(2024, 1, 15, 12, 0, tzinfo=datetime.timezone.utc)
        >>> parse_datetime_iso(None)
        None
    """
    if not value:
        return None
    try:
        # Handle Supabase Z suffix (ISO 8601 format)
        value = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)
        # Make naive datetimes UTC-aware
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class SessionMetadata:
    """Immutable metadata for cached sessions.

    This dataclass stores metadata about a session without the actual session data.
    Used for querying, TTL checks, and display without loading the full session.

    Note:
        Frozen dataclass prevents accidental modification.
        Use dataclasses.replace() to create modified copies.

    Attributes:
        name: Session identifier (e.g., "humaninterest")
        checksum: SHA-256 hash of the unencrypted session pickle data
        created_at: When the session was first cached
        modified_at: When the session was last updated
        expires_at: TTL expiration time (None = no expiration)
        domain: Primary domain the session was created for
        current_url: URL when session was cached
        cookie_count: Number of cookies in the session
        cookie_domains: List of domains cookies belong to
        status: Session status ("active", "logged_out")
        storage_backend: Backend that stored this session (e.g., "local", "s3")
        storage_location: Where the session is stored (path or URI)
    """

    name: str
    checksum: str
    created_at: datetime
    modified_at: datetime
    expires_at: datetime | None
    domain: str | None
    current_url: str | None
    cookie_count: int
    cookie_domains: list[str]
    status: str = "active"
    storage_backend: str = ""
    storage_location: str = ""


def metadata_to_dict(metadata: SessionMetadata) -> dict[str, "Any"]:
    """Convert SessionMetadata to JSON-serializable dict.

    Args:
        metadata: Session metadata object

    Returns:
        Dictionary suitable for JSON serialization
    """
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
        "storage_backend": metadata.storage_backend,
        "storage_location": metadata.storage_location,
    }


def dict_to_metadata(data: dict[str, "Any"]) -> SessionMetadata:
    """Convert dict to SessionMetadata.

    Args:
        data: Dictionary from JSON deserialization

    Returns:
        SessionMetadata object
    """
    return SessionMetadata(
        name=data.get("name", ""),
        checksum=data.get("checksum", ""),
        created_at=parse_datetime_iso(data.get("created_at")) or datetime.now(UTC),
        modified_at=parse_datetime_iso(data.get("modified_at")) or datetime.now(UTC),
        expires_at=parse_datetime_iso(data.get("expires_at")),
        domain=data.get("domain"),
        current_url=data.get("current_url"),
        cookie_count=data.get("cookie_count", 0),
        cookie_domains=data.get("cookie_domains", []),
        status=data.get("status", "active"),
        storage_backend=data.get("storage_backend", ""),
        storage_location=data.get("storage_location", ""),
    )


class SessionStorageBackend(Protocol):
    """Protocol defining session storage backend interface.

    Implementations handle storage only - encryption is done by caller.
    This follows the existing StorageBackend pattern used for documents.

    All methods work with already-encrypted data, maintaining separation
    of concerns between encryption (encryption.py) and storage (this module).
    """

    def save_session(
        self,
        name: str,
        encrypted_data: bytes,
        metadata: SessionMetadata,
    ) -> str:
        """Save encrypted session data and return storage location.

        Args:
            name: Session identifier (e.g., "humaninterest")
            encrypted_data: Already-encrypted session bytes (Fernet)
            metadata: Session metadata for querying and TTL

        Returns:
            Storage location identifier (path or URI)

        Raises:
            OSError: If save fails (local filesystem)
            StorageError: If save fails after retries (remote storage)
        """
        ...

    def load_session(
        self,
        name: str,
    ) -> tuple[bytes, SessionMetadata]:
        """Load encrypted session data and metadata.

        Args:
            name: Session identifier

        Returns:
            Tuple of (encrypted_data, metadata)

        Raises:
            SessionNotFoundError: If session doesn't exist
            SessionExpiredError: If session TTL exceeded
        """
        ...

    def list_sessions(self) -> list[str]:
        """List all session names.

        Returns:
            Sorted list of session names
        """
        ...

    def delete_session(self, name: str) -> bool:
        """Delete a session.

        Args:
            name: Session identifier

        Returns:
            True if deleted, False if not found
        """
        ...

    def get_session_metadata(self, name: str) -> SessionMetadata | None:
        """Get session metadata without loading the full session.

        This is a lightweight way to check session status, timestamps, etc.
        without loading the encrypted session data.

        Args:
            name: Session identifier

        Returns:
            SessionMetadata if session exists, None otherwise
        """
        ...

    def update_session_metadata(
        self,
        name: str,
        status: str | None = None,
    ) -> bool:
        """Update session metadata fields.

        Args:
            name: Session identifier
            status: New status value (e.g., "logged_out")

        Returns:
            True if updated, False if session not found

        Raises:
            ValueError: If status is not a valid value
        """
        ...
