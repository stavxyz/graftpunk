"""S3-compatible session storage backend.

Supports AWS S3, Cloudflare R2, MinIO, and other S3-compatible storage services.

Environment Variables:
    AWS_ACCESS_KEY_ID: AWS access key ID (or R2/MinIO equivalent)
    AWS_SECRET_ACCESS_KEY: AWS secret access key (or R2/MinIO equivalent)
    AWS_REGION: AWS region (optional, set to 'auto' for Cloudflare R2)

Configuration:
    - bucket: S3 bucket name for session storage
    - region: AWS region (use 'auto' for Cloudflare R2)
    - endpoint_url: Custom endpoint for R2/MinIO (e.g., https://<account>.r2.cloudflarestorage.com)
    - max_retries: Maximum retry attempts for transient failures (default: 3)
    - base_delay: Base delay in seconds for exponential backoff (default: 1.0)

Storage Structure:
    sessions/{name}/session.pickle - Encrypted session data
    sessions/{name}/metadata.json - Session metadata
"""

import json
import random
import time
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


class S3SessionStorage:
    """S3-compatible session storage backend.

    Stores sessions in S3-compatible storage (AWS S3, Cloudflare R2, MinIO).
    Each session is stored as:
    - sessions/{name}/session.pickle - Encrypted session bytes
    - sessions/{name}/metadata.json - JSON metadata for TTL/querying

    Features:
    - Supports AWS S3, Cloudflare R2, and MinIO
    - Exponential backoff retry for transient failures
    - Client injection for testing
    - Region='auto' handling for Cloudflare R2
    """

    def __init__(
        self,
        bucket: str,
        region: str | None = None,
        endpoint_url: str | None = None,
        max_retries: int = 3,
        base_delay: float = 1.0,
        client: Any | None = None,
    ) -> None:
        """Initialize S3 session storage.

        Args:
            bucket: S3 bucket name for session storage
            region: AWS region (use 'auto' for Cloudflare R2, which ignores region)
            endpoint_url: Custom endpoint URL for R2/MinIO
            max_retries: Maximum retry attempts for transient failures
            base_delay: Base delay in seconds for exponential backoff
            client: Optional pre-configured boto3 S3 client (for testing)

        Raises:
            StorageError: If boto3 is not installed
        """
        self.bucket = bucket
        self.region = region
        self.endpoint_url = endpoint_url
        self.max_retries = max_retries
        self.base_delay = base_delay

        if client is not None:
            self._client = client
        else:
            self._client = self._create_client()

        LOG.info(
            "s3_session_storage_initialized",
            bucket=bucket,
            region=region,
            endpoint_url=endpoint_url,
        )

    def _create_client(self) -> Any:
        """Create boto3 S3 client.

        Returns:
            Configured boto3 S3 client

        Raises:
            StorageError: If boto3 is not installed
        """
        try:
            import boto3
        except ImportError as exc:
            raise StorageError(
                "boto3 is required for S3 storage. "
                "Install with: pip install graftpunk[s3] or pip install boto3"
            ) from exc

        # Build client kwargs
        client_kwargs: dict[str, Any] = {}

        # Handle region='auto' for Cloudflare R2 (ignores region)
        if self.region and self.region != "auto":
            client_kwargs["region_name"] = self.region

        if self.endpoint_url:
            client_kwargs["endpoint_url"] = self.endpoint_url

        return boto3.client("s3", **client_kwargs)

    def _session_key(self, name: str) -> str:
        """Generate S3 key for session pickle data.

        Args:
            name: Session identifier

        Returns:
            S3 object key path
        """
        return f"sessions/{name}/session.pickle"

    def _metadata_key(self, name: str) -> str:
        """Generate S3 key for session metadata.

        Args:
            name: Session identifier

        Returns:
            S3 object key path
        """
        return f"sessions/{name}/metadata.json"

    def _with_retry(self, operation: str, func, *args, **kwargs):
        """Execute function with exponential backoff retry.

        Args:
            operation: Name of operation for logging
            func: Function to execute
            *args: Positional arguments for func
            **kwargs: Keyword arguments for func

        Returns:
            Result of func(*args, **kwargs)

        Raises:
            StorageError: If operation fails after max_retries attempts
        """
        from botocore.exceptions import ClientError

        last_exception = None
        for attempt in range(self.max_retries):
            try:
                return func(*args, **kwargs)
            except ClientError as e:
                last_exception = e
                error_code = e.response.get("Error", {}).get("Code", "")
                status_code = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)

                # Non-retryable errors (4xx except throttling)
                if status_code < 500 and error_code not in ("Throttling", "SlowDown"):
                    raise StorageError(f"{operation} failed: {e}") from e

                # Retryable - exponential backoff with jitter
                delay = min(self.base_delay * (2**attempt), 30.0)
                delay = delay * (0.5 + random.random() * 0.5)  # noqa: S311

                LOG.warning(
                    "s3_operation_retry",
                    operation=operation,
                    attempt=attempt + 1,
                    max_attempts=self.max_retries,
                    delay=delay,
                    error=str(e),
                )

                if attempt < self.max_retries - 1:
                    time.sleep(delay)
            except (ConnectionError, TimeoutError, OSError) as e:
                last_exception = e
                delay = min(self.base_delay * (2**attempt), 30.0)
                delay = delay * (0.5 + random.random() * 0.5)  # noqa: S311

                LOG.warning(
                    "s3_connection_retry",
                    operation=operation,
                    attempt=attempt + 1,
                    delay=delay,
                    error=str(e),
                )

                if attempt < self.max_retries - 1:
                    time.sleep(delay)

        raise StorageError(
            f"{operation} failed after {self.max_retries} attempts"
        ) from last_exception

    def save_session(
        self,
        name: str,
        encrypted_data: bytes,
        metadata: SessionMetadata,
    ) -> str:
        """Save encrypted session to S3.

        Args:
            name: Session identifier
            encrypted_data: Already-encrypted session bytes
            metadata: Session metadata

        Returns:
            S3 URI (s3://bucket/key)

        Raises:
            StorageError: If save fails after retries
        """
        session_key = self._session_key(name)
        metadata_key = self._metadata_key(name)

        # Save encrypted session data with retry
        self._with_retry(
            "save_session_data",
            self._client.put_object,
            Bucket=self.bucket,
            Key=session_key,
            Body=encrypted_data,
            ContentType="application/octet-stream",
        )

        # Save metadata as JSON with retry
        metadata_json = json.dumps(metadata_to_dict(metadata), indent=2)
        self._with_retry(
            "save_session_metadata",
            self._client.put_object,
            Bucket=self.bucket,
            Key=metadata_key,
            Body=metadata_json.encode("utf-8"),
            ContentType="application/json",
        )

        location = f"s3://{self.bucket}/{session_key}"
        LOG.info("session_saved", name=name, location=location)
        return location

    def load_session(
        self,
        name: str,
    ) -> tuple[bytes, SessionMetadata]:
        """Load encrypted session from S3.

        Args:
            name: Session identifier

        Returns:
            Tuple of (encrypted_data, metadata)

        Raises:
            SessionNotFoundError: If session doesn't exist
            SessionExpiredError: If session TTL exceeded
            StorageError: If load fails after retries
        """
        from botocore.exceptions import ClientError

        metadata_key = self._metadata_key(name)
        session_key = self._session_key(name)

        # Load metadata first (to check TTL before downloading large pickle)
        try:
            metadata_response = self._client.get_object(
                Bucket=self.bucket,
                Key=metadata_key,
            )
            metadata_json = metadata_response["Body"].read().decode("utf-8")
            metadata = dict_to_metadata(json.loads(metadata_json))
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("NoSuchKey", "404"):
                raise SessionNotFoundError(f"Session '{name}' not found") from e
            raise StorageError(f"Failed to load session '{name}': {e}") from e

        # Check TTL
        if metadata.expires_at and datetime.now(UTC) > metadata.expires_at:
            raise SessionExpiredError(
                f"Session '{name}' expired at {metadata.expires_at.isoformat()}"
            )

        # Load encrypted session data
        try:
            session_response = self._client.get_object(
                Bucket=self.bucket,
                Key=session_key,
            )
            encrypted_data = session_response["Body"].read()
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("NoSuchKey", "404"):
                raise SessionNotFoundError(
                    f"Session '{name}' data not found (metadata exists)"
                ) from e
            raise StorageError(f"Failed to load session '{name}': {e}") from e

        LOG.info("session_loaded", name=name, size=len(encrypted_data))
        return encrypted_data, metadata

    def list_sessions(self) -> list[str]:
        """List all session names in the bucket.

        Returns:
            Sorted list of session names
        """
        sessions: set[str] = set()
        paginator = self._client.get_paginator("list_objects_v2")

        try:
            for page in paginator.paginate(Bucket=self.bucket):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    # Extract session name from path: sessions/{name}/session.pickle
                    if key.startswith("sessions/") and "/" in key[9:]:
                        session_name = key.split("/")[1]
                        sessions.add(session_name)
        except Exception as e:
            LOG.warning("list_sessions_failed", error=str(e))
            return []

        return sorted(sessions)

    def delete_session(self, name: str) -> bool:
        """Delete session data and metadata from S3.

        Args:
            name: Session identifier

        Returns:
            True if deleted, False if not found
        """
        session_key = self._session_key(name)
        metadata_key = self._metadata_key(name)

        deleted = False
        for key in (session_key, metadata_key):
            try:
                self._client.delete_object(Bucket=self.bucket, Key=key)
                deleted = True
            except Exception as e:
                LOG.debug("delete_object_failed", key=key, error=str(e))

        if deleted:
            LOG.info("session_deleted", name=name)
        return deleted

    def get_session_metadata(self, name: str) -> SessionMetadata | None:
        """Get session metadata without loading session data.

        Args:
            name: Session identifier

        Returns:
            SessionMetadata if session exists, None otherwise
        """
        from botocore.exceptions import ClientError

        metadata_key = self._metadata_key(name)

        try:
            response = self._client.get_object(Bucket=self.bucket, Key=metadata_key)
            metadata_json = response["Body"].read().decode("utf-8")
            return dict_to_metadata(json.loads(metadata_json))
        except ClientError:
            return None
        except Exception as e:
            LOG.warning("get_session_metadata_failed", name=name, error=str(e))
            return None

    def update_session_metadata(
        self,
        name: str,
        status: str | None = None,
    ) -> bool:
        """Update session metadata (currently only status field).

        Args:
            name: Session identifier
            status: New status value

        Returns:
            True if updated, False if session not found

        Raises:
            ValueError: If status is not a valid value
        """
        if status is not None and status not in ("active", "logged_out"):
            raise ValueError(f"Invalid status: {status}. Must be 'active' or 'logged_out'")

        metadata = self.get_session_metadata(name)
        if metadata is None:
            return False

        from dataclasses import replace

        updates: dict[str, Any] = {"modified_at": datetime.now(UTC)}
        if status is not None:
            updates["status"] = status

        new_metadata = replace(metadata, **updates)
        metadata_key = self._metadata_key(name)
        metadata_json = json.dumps(metadata_to_dict(new_metadata), indent=2)

        try:
            self._client.put_object(
                Bucket=self.bucket,
                Key=metadata_key,
                Body=metadata_json.encode("utf-8"),
                ContentType="application/json",
            )
            LOG.info("metadata_updated", name=name, status=status)
            return True
        except Exception as e:
            LOG.error("metadata_update_failed", name=name, error=str(e))
            return False
