"""Supabase session storage backend for Lambda/stateless environments.

Uses Supabase Storage bucket for both session data and metadata.
Follows the same file-pair pattern as local and S3 backends:
- {session_name}/session.pickle - Encrypted session data
- {session_name}/metadata.json - Session metadata (JSON)

This is a BREAKING CHANGE from the previous implementation that used
Supabase database table for metadata. Users with existing sessions
will need to re-login.
"""

import json
import random
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from graftpunk.exceptions import SessionExpiredError, SessionNotFoundError, StorageError
from graftpunk.logging import get_logger
from graftpunk.storage.base import (
    SessionMetadata,
    dict_to_metadata,
    metadata_to_dict,
)

LOG = get_logger(__name__)

# Storage configuration
CACHE_CONTROL_SECONDS = 3600  # 1 hour


class SupabaseSessionStorage:
    """Supabase session storage for Lambda/stateless environments.

    Storage architecture (file-pair pattern):
    - {session_name}/session.pickle - Encrypted session bytes
    - {session_name}/metadata.json - Session metadata (JSON)

    This matches the local and S3 storage patterns, storing both
    session data and metadata in the same Supabase Storage bucket.
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

    def _session_path(self, name: str) -> str:
        """Generate storage path for session pickle data.

        Args:
            name: Session identifier

        Returns:
            Storage path
        """
        return f"{name}/session.pickle"

    def _metadata_path(self, name: str) -> str:
        """Generate storage path for session metadata.

        Args:
            name: Session identifier

        Returns:
            Storage path
        """
        return f"{name}/metadata.json"

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

        storage = self.client.storage.from_(self.bucket_name)

        # Build storage paths
        session_path = self._session_path(name)
        metadata_path = self._metadata_path(name)

        # Upload session pickle with upsert fallback
        session_options: FileOptions = {
            "cache-control": str(CACHE_CONTROL_SECONDS),
            "content-type": "application/octet-stream",
        }

        try:
            storage.upload(file=encrypted_data, path=session_path, file_options=session_options)
        except StorageApiError as e:
            if e.status == 409 or e.status == "409":
                # File exists, use update to replace it
                storage.update(file=encrypted_data, path=session_path, file_options=session_options)
            else:
                raise

        # Upload metadata JSON
        metadata_json = json.dumps(metadata_to_dict(metadata), indent=2)
        metadata_options: FileOptions = {
            "cache-control": str(CACHE_CONTROL_SECONDS),
            "content-type": "application/json",
        }

        try:
            storage.upload(
                file=metadata_json.encode("utf-8"),
                path=metadata_path,
                file_options=metadata_options,
            )
        except StorageApiError as e:
            if e.status == 409 or e.status == "409":
                storage.update(
                    file=metadata_json.encode("utf-8"),
                    path=metadata_path,
                    file_options=metadata_options,
                )
            else:
                raise

        location = f"{self.bucket_name}/{session_path}"
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
        from storage3.exceptions import StorageApiError

        LOG.info("session_load_started", name=name, backend="supabase")

        storage = self.client.storage.from_(self.bucket_name)
        metadata_path = self._metadata_path(name)
        session_path = self._session_path(name)

        # Load metadata first (to check TTL before downloading large pickle)
        try:
            metadata_bytes = storage.download(metadata_path)
            metadata_json = metadata_bytes.decode("utf-8")
            metadata = dict_to_metadata(json.loads(metadata_json))
        except (HTTPStatusError, StorageApiError) as e:
            LOG.warning("session_metadata_not_found", name=name, error=str(e))
            raise SessionNotFoundError(f"Session '{name}' not found") from e

        # Check TTL
        if metadata.expires_at and datetime.now(UTC) > metadata.expires_at:
            expires_at_str = metadata.expires_at.isoformat()
            LOG.warning("session_expired_ttl", name=name, expires_at=expires_at_str)
            raise SessionExpiredError(
                f"Session '{name}' has expired (TTL). Run 'gp clear' and re-login."
            )

        # Download encrypted session data
        try:
            encrypted_data = storage.download(session_path)
        except (HTTPStatusError, StorageApiError) as e:
            LOG.error("session_download_failed", name=name, path=session_path, error=str(e))
            raise SessionNotFoundError(f"Session '{name}' not found in storage") from e

        LOG.info("session_load_completed", name=name, backend="supabase")
        return encrypted_data, metadata

    def list_sessions(self) -> list[str]:
        """List all session names.

        Returns:
            Sorted list of session names

        Raises:
            StorageError: If listing fails due to Supabase errors
        """
        from httpx import HTTPStatusError
        from storage3.exceptions import StorageApiError

        try:
            storage = self.client.storage.from_(self.bucket_name)
            # List top-level folders in the bucket
            result = storage.list()

            if not result:
                return []

            # Each session is a folder containing session.pickle and metadata.json
            # The list() call returns folder/file entries at root level
            sessions: set[str] = set()
            for item in result:
                # Supabase Storage returns objects with 'name' field
                # Folders appear as entries; we extract session names from paths
                name = item.get("name", "")
                if name and not name.startswith("."):
                    # This could be a folder name directly, or we may need to
                    # parse paths if list returns full paths
                    sessions.add(name)

            LOG.debug("session_list_completed", count=len(sessions), backend="supabase")
            return sorted(sessions)
        except (HTTPStatusError, StorageApiError) as e:
            LOG.error("failed_to_list_sessions", error=str(e), backend="supabase")
            raise StorageError(f"Failed to list sessions: {e}") from e

    def delete_session(self, name: str) -> bool:
        """Delete a session.

        Args:
            name: Session identifier

        Returns:
            True if deleted successfully

        Raises:
            StorageError: If delete fails due to Supabase errors (except NotFound)
        """
        from httpx import HTTPStatusError
        from storage3.exceptions import StorageApiError

        session_path = self._session_path(name)
        metadata_path = self._metadata_path(name)

        storage = self.client.storage.from_(self.bucket_name)

        # Delete both files from storage
        for path in (session_path, metadata_path):
            try:
                storage.remove([path])
                LOG.debug("session_file_deleted", name=name, path=path)
            except (HTTPStatusError, StorageApiError) as e:
                # 404/NotFound is acceptable - file didn't exist
                status = getattr(e, "status", None) or getattr(getattr(e, "response", None), "status_code", None)
                if status not in (404, "404"):
                    LOG.error("session_file_delete_failed", name=name, path=path, error=str(e))
                    raise StorageError(f"Failed to delete '{path}': {e}") from e

        LOG.info("session_delete_completed", name=name, backend="supabase")
        return True

    def get_session_metadata(self, name: str) -> SessionMetadata | None:
        """Get session metadata without loading the full session.

        Args:
            name: Session identifier

        Returns:
            SessionMetadata if session exists, None otherwise

        Raises:
            StorageError: If fetch fails due to Supabase errors (except NotFound)
        """
        from httpx import HTTPStatusError
        from storage3.exceptions import StorageApiError

        try:
            storage = self.client.storage.from_(self.bucket_name)
            metadata_path = self._metadata_path(name)
            metadata_bytes = storage.download(metadata_path)
            metadata_json = metadata_bytes.decode("utf-8")
            return dict_to_metadata(json.loads(metadata_json))
        except (HTTPStatusError, StorageApiError) as e:
            # 404/NotFound means session doesn't exist - return None
            status = getattr(e, "status", None) or getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, "404"):
                return None
            LOG.error("get_session_metadata_failed", name=name, error=str(e))
            raise StorageError(f"Failed to get metadata for '{name}': {e}") from e

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
            StorageError: If update fails due to Supabase errors
        """
        from httpx import HTTPStatusError
        from storage3.exceptions import StorageApiError
        from storage3.types import FileOptions

        if status is not None and status not in ("active", "logged_out"):
            raise ValueError(f"Invalid status '{status}'. Must be 'active' or 'logged_out'")

        # Fetch current metadata
        metadata = self.get_session_metadata(name)
        if metadata is None:
            return False

        # Create updated metadata
        updates: dict[str, Any] = {"modified_at": datetime.now(UTC)}
        if status is not None:
            updates["status"] = status

        new_metadata = replace(metadata, **updates)

        # Re-upload metadata JSON
        try:
            storage = self.client.storage.from_(self.bucket_name)
            metadata_path = self._metadata_path(name)
            metadata_json = json.dumps(metadata_to_dict(new_metadata), indent=2)
            metadata_options: FileOptions = {
                "cache-control": str(CACHE_CONTROL_SECONDS),
                "content-type": "application/json",
            }

            try:
                storage.upload(
                    file=metadata_json.encode("utf-8"),
                    path=metadata_path,
                    file_options=metadata_options,
                )
            except StorageApiError as e:
                if e.status == 409 or e.status == "409":
                    storage.update(
                        file=metadata_json.encode("utf-8"),
                        path=metadata_path,
                        file_options=metadata_options,
                    )
                else:
                    raise

            LOG.info("session_metadata_updated", name=name, status=status, backend="supabase")
            return True
        except (HTTPStatusError, StorageApiError) as e:
            LOG.error("failed_to_update_session_metadata", name=name, error=str(e))
            raise StorageError(f"Failed to update metadata for '{name}': {e}") from e

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
