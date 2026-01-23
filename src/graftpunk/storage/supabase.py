"""Supabase session storage backend for Lambda/stateless environments."""

import random
import time
from datetime import UTC, datetime
from typing import Any, cast

from graftpunk.exceptions import SessionExpiredError, SessionNotFoundError, StorageError
from graftpunk.logging import get_logger
from graftpunk.storage.base import (
    SessionMetadata,
    parse_datetime_iso,
)

LOG = get_logger(__name__)

# Storage configuration
CACHE_CONTROL_SECONDS = 3600  # 1 hour


class SupabaseSessionStorage:
    """Supabase session storage for Lambda/stateless environments.

    Storage architecture:
    - Encrypted session pickle → Supabase Storage bucket
    - Metadata → session_cache database table (queryable, TTL)

    The encryption key is stored in Supabase Vault and retrieved
    by the encryption module when GRAFTPUNK_STORAGE_BACKEND=supabase.
    """

    def __init__(
        self,
        url: str,
        service_key: str,
        bucket_name: str = "sessions",
        max_retries: int = 5,
        base_delay: float = 1.0,
    ) -> None:
        """Initialize Supabase session storage.

        Args:
            url: Supabase project URL
            service_key: Supabase service role key
            bucket_name: Storage bucket name for sessions
            max_retries: Maximum retry attempts
            base_delay: Base delay in seconds for exponential backoff
        """
        try:
            from supabase import Client, create_client
        except ImportError as exc:
            raise StorageError(
                "supabase package is required for Supabase storage. "
                "Install with: pip install graftpunk[supabase]"
            ) from exc

        # Normalize URL (ensure trailing slash for Supabase client)
        normalized_url = url.rstrip("/") + "/"

        self.client: Client = create_client(normalized_url, service_key)
        self.bucket_name = bucket_name
        self.max_retries = max_retries
        self.base_delay = base_delay

        LOG.info(
            "supabase_session_storage_initialized",
            url=normalized_url,
            bucket=bucket_name,
        )

    def save_session(
        self,
        name: str,
        encrypted_data: bytes,
        metadata: SessionMetadata,
    ) -> str:
        """Save encrypted session to Supabase.

        Args:
            name: Session identifier
            encrypted_data: Already-encrypted session bytes
            metadata: Session metadata

        Returns:
            Storage location (bucket/path)

        Raises:
            StorageError: If save fails after retries
        """
        from httpx import HTTPStatusError

        last_exception = None

        for attempt in range(self.max_retries):
            try:
                return self._do_save(name, encrypted_data, metadata)
            except (HTTPStatusError, ConnectionError, TimeoutError, OSError) as e:
                last_exception = e
                if not self._is_retryable_error(e):
                    raise StorageError(f"Session save failed: {e}") from e

                delay = min(
                    self.base_delay * (2**attempt),
                    30.0,  # max delay
                )
                delay = delay * (0.5 + random.random() * 0.5)  # noqa: S311

                if attempt < self.max_retries - 1:
                    LOG.warning(
                        "session_save_retry",
                        attempt=attempt + 1,
                        max_attempts=self.max_retries,
                        delay=delay,
                        error=str(e),
                    )
                    time.sleep(delay)

        raise StorageError(
            f"Session save failed after {self.max_retries} attempts"
        ) from last_exception

    def _is_retryable_error(self, error: Exception) -> bool:
        """Check if an error is retryable (5xx, 429, connection/timeout errors)."""
        from httpx import HTTPStatusError

        if isinstance(error, HTTPStatusError):
            return error.response.status_code >= 500 or error.response.status_code == 429
        return isinstance(error, (ConnectionError, TimeoutError))

    def _do_save(
        self,
        name: str,
        encrypted_data: bytes,
        metadata: SessionMetadata,
    ) -> str:
        """Actual save logic without retry.

        Args:
            name: Session identifier
            encrypted_data: Already-encrypted session bytes
            metadata: Session metadata

        Returns:
            Storage location (bucket/path)
        """
        from storage3.exceptions import StorageApiError
        from storage3.types import FileOptions

        LOG.info("session_save_started", name=name, backend="supabase")

        # Ensure bucket exists
        self._ensure_bucket_exists()

        # Build storage path
        storage_path = f"{name}/session.pickle"

        # Upload to Storage with upsert fallback
        file_options: FileOptions = {
            "cache-control": str(CACHE_CONTROL_SECONDS),
            "content-type": "application/octet-stream",
        }
        storage = self.client.storage.from_(self.bucket_name)

        try:
            storage.upload(file=encrypted_data, path=storage_path, file_options=file_options)
        except StorageApiError as e:
            if e.status == 409 or e.status == "409":
                # File exists, use update to replace it
                storage.update(file=encrypted_data, path=storage_path, file_options=file_options)
            else:
                raise

        # Upsert metadata to database
        location = f"{self.bucket_name}/{storage_path}"
        self._upsert_metadata(metadata, storage_path)

        LOG.info("session_save_completed", name=name, location=location, backend="supabase")
        return location

    def load_session(
        self,
        name: str,
    ) -> tuple[bytes, SessionMetadata]:
        """Load encrypted session from Supabase.

        Args:
            name: Session identifier

        Returns:
            Tuple of (encrypted_data, metadata)

        Raises:
            SessionNotFoundError: If session doesn't exist
            SessionExpiredError: If session TTL exceeded
        """
        from httpx import HTTPStatusError
        from postgrest.exceptions import APIError
        from storage3.exceptions import StorageApiError

        LOG.info("session_load_started", name=name, backend="supabase")

        # Query metadata from database first (faster than Storage listing)
        try:
            result = (
                self.client.table("session_cache")
                .select("*")
                .eq("provider", name)
                .maybe_single()
                .execute()
            )
        except HTTPStatusError as e:
            LOG.error("session_metadata_query_failed", name=name, error=str(e))
            # Only raise SessionNotFoundError for actual 404 responses
            if e.response.status_code == 404:
                raise SessionNotFoundError(f"Session '{name}' not found") from e
            raise StorageError(f"Failed to query session '{name}': {e}") from e
        except APIError as e:
            LOG.error("session_metadata_query_failed", name=name, error=str(e))
            raise StorageError(f"Failed to query session '{name}': {e}") from e

        if result is None or not result.data:
            LOG.warning("session_not_found", name=name)
            raise SessionNotFoundError(f"Session '{name}' not found")

        # Cast to dict since maybe_single() returns a single row
        data = cast(dict[str, Any], result.data)

        # Check TTL
        expires_at_raw = data.get("expires_at")
        if expires_at_raw and isinstance(expires_at_raw, str):
            expires_at_str = expires_at_raw
            try:
                # Handle different datetime formats from Supabase
                expires_at_str = expires_at_str.replace("Z", "+00:00")
                expires_at = datetime.fromisoformat(expires_at_str)
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                if datetime.now(UTC) > expires_at:
                    LOG.warning("session_expired_ttl", name=name, expires_at=expires_at_str)
                    raise SessionExpiredError(
                        f"Session '{name}' has expired (TTL). Run 'gp clear' and re-login."
                    )
            except (ValueError, TypeError) as exc:
                LOG.warning(
                    "invalid_expires_at",
                    name=name,
                    expires_at=expires_at_str,
                    error=str(exc),
                )

        # Download encrypted session from Storage
        storage_path_val = data.get("storage_path")
        if not storage_path_val or not isinstance(storage_path_val, str):
            LOG.error("session_storage_path_missing", name=name)
            raise SessionExpiredError(f"Session '{name}' has missing storage path")
        storage_path: str = storage_path_val

        try:
            encrypted_data = self.client.storage.from_(self.bucket_name).download(storage_path)
        except (HTTPStatusError, StorageApiError) as e:
            LOG.error("session_download_failed", name=name, path=storage_path, error=str(e))
            raise SessionNotFoundError(f"Session '{name}' not found in storage") from e

        # Convert database row to SessionMetadata
        metadata = self._row_to_metadata(data)

        LOG.info("session_load_completed", name=name, backend="supabase")
        return encrypted_data, metadata

    def list_sessions(self) -> list[str]:
        """List all session names.

        Returns:
            Sorted list of session names
        """
        from httpx import HTTPStatusError
        from postgrest.exceptions import APIError

        try:
            result = self.client.table("session_cache").select("provider").execute()

            if result is None or not result.data:
                return []

            # Cast to list of dicts since we're selecting rows
            rows = cast(list[dict[str, Any]], result.data)
            names = [
                str(row["provider"]) for row in rows if isinstance(row, dict) and "provider" in row
            ]
            LOG.debug("session_list_completed", count=len(names), backend="supabase")
            return sorted(names)
        except (HTTPStatusError, APIError) as e:
            LOG.error("failed_to_list_sessions", error=str(e), backend="supabase")
            return []

    def delete_session(self, name: str) -> bool:
        """Delete a session.

        Args:
            name: Session identifier

        Returns:
            True if deleted, False if not found
        """
        from httpx import HTTPStatusError
        from postgrest.exceptions import APIError
        from storage3.exceptions import StorageApiError

        storage_path = f"{name}/session.pickle"
        deleted_storage = False
        deleted_db = False

        # Delete from Storage
        try:
            self.client.storage.from_(self.bucket_name).remove([storage_path])
            deleted_storage = True
            LOG.debug("session_storage_deleted", name=name, path=storage_path)
        except (HTTPStatusError, StorageApiError) as e:
            # Storage file might not exist, continue to delete DB entry
            LOG.warning("session_storage_delete_failed", name=name, error=str(e))

        # Delete from database
        try:
            result = self.client.table("session_cache").delete().eq("provider", name).execute()
            # Check if any rows were deleted
            if result and result.data:
                deleted_db = True
            LOG.debug("session_db_deleted", name=name)
        except (HTTPStatusError, APIError) as e:
            LOG.warning("session_db_delete_failed", name=name, error=str(e))

        if deleted_storage or deleted_db:
            LOG.info("session_delete_completed", name=name, backend="supabase")
            return True

        return False

    def get_session_metadata(self, name: str) -> SessionMetadata | None:
        """Get session metadata without loading the full session.

        Args:
            name: Session identifier

        Returns:
            SessionMetadata if session exists, None otherwise
        """
        from httpx import HTTPStatusError
        from postgrest.exceptions import APIError

        try:
            result = (
                self.client.table("session_cache")
                .select("*")
                .eq("provider", name)
                .maybe_single()
                .execute()
            )

            if result is None or not result.data:
                return None

            # Cast to dict since maybe_single() returns a single row
            row = cast(dict[str, Any], result.data)
            return self._row_to_metadata(row)
        except (HTTPStatusError, APIError) as e:
            LOG.warning("failed_to_get_session_metadata", name=name, error=str(e))
            return None

    def update_session_metadata(
        self,
        name: str,
        status: str | None = None,
    ) -> bool:
        """Update session metadata fields.

        Args:
            name: Session identifier
            status: New status value

        Returns:
            True if updated, False if session not found

        Raises:
            ValueError: If status is not a valid value
        """
        from httpx import HTTPStatusError
        from postgrest.exceptions import APIError

        if status is not None and status not in ("active", "logged_out"):
            raise ValueError(f"Invalid status '{status}'. Must be 'active' or 'logged_out'")

        try:
            update_data: dict[str, Any] = {"modified_at": datetime.now(UTC).isoformat()}
            if status is not None:
                update_data["status"] = status

            result = (
                self.client.table("session_cache")
                .update(update_data)
                .eq("provider", name)
                .execute()
            )

            if result and result.data:
                LOG.info("session_metadata_updated", name=name, status=status, backend="supabase")
                return True
            return False
        except (HTTPStatusError, APIError) as e:
            LOG.error("failed_to_update_session_metadata", name=name, error=str(e))
            return False

    def _ensure_bucket_exists(self) -> None:
        """Create sessions bucket if it doesn't exist."""
        from httpx import HTTPStatusError
        from storage3.exceptions import StorageApiError

        try:
            self.client.storage.create_bucket(
                self.bucket_name,
                options={"public": False},
            )
            LOG.info("session_bucket_created", bucket=self.bucket_name)
        except HTTPStatusError as e:
            if e.response.status_code == 409:
                LOG.debug("session_bucket_already_exists", bucket=self.bucket_name)
                return
            raise
        except StorageApiError as e:
            if e.status == 409 or e.status == "409":
                LOG.debug("session_bucket_already_exists", bucket=self.bucket_name)
                return
            raise

    def _upsert_metadata(self, metadata: SessionMetadata, storage_path: str) -> None:
        """Upsert session metadata to database.

        Args:
            metadata: Session metadata
            storage_path: Path in storage bucket

        Raises:
            StorageError: If metadata upsert fails
        """
        from httpx import HTTPStatusError
        from postgrest.exceptions import APIError

        try:
            self.client.table("session_cache").upsert(
                {
                    "provider": metadata.name,
                    "storage_path": storage_path,
                    "checksum": metadata.checksum,
                    "expires_at": metadata.expires_at.isoformat() if metadata.expires_at else None,
                    "domain": metadata.domain,
                    "current_url": metadata.current_url,
                    "cookie_count": metadata.cookie_count,
                    "cookie_domains": metadata.cookie_domains,
                    "created_at": metadata.created_at.isoformat(),
                    "modified_at": metadata.modified_at.isoformat(),
                    "status": metadata.status,
                },
                on_conflict="provider",
            ).execute()

            LOG.debug("session_metadata_upserted", name=metadata.name)
        except (HTTPStatusError, APIError) as e:
            LOG.error("failed_to_upsert_session_metadata", name=metadata.name, error=str(e))
            raise StorageError(
                f"Failed to save session metadata for '{metadata.name}'. "
                "Storage data was uploaded but metadata is missing - please retry."
            ) from e

    def _row_to_metadata(self, data: dict[str, Any]) -> SessionMetadata:
        """Convert database row to SessionMetadata."""
        return SessionMetadata(
            name=data.get("provider", ""),
            checksum=data.get("checksum", ""),
            created_at=parse_datetime_iso(data.get("created_at")) or datetime.now(UTC),
            modified_at=parse_datetime_iso(data.get("modified_at")) or datetime.now(UTC),
            expires_at=parse_datetime_iso(data.get("expires_at")),
            domain=data.get("domain"),
            current_url=data.get("current_url"),
            cookie_count=data.get("cookie_count", 0),
            cookie_domains=data.get("cookie_domains", []),
            status=data.get("status", "active"),
        )
