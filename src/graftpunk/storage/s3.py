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
from datetime import UTC, datetime
from typing import Any

from graftpunk.exceptions import SessionExpiredError, SessionNotFoundError, StorageError
from graftpunk.logging import get_logger
from graftpunk.storage.base import SessionMetadata, parse_datetime_iso

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

        # Save encrypted session data
        self._client.put_object(
            Bucket=self.bucket,
            Key=session_key,
            Body=encrypted_data,
            ContentType="application/octet-stream",
        )

        # Save metadata as JSON
        metadata_json = json.dumps(self._metadata_to_dict(metadata), indent=2)
        self._client.put_object(
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
            metadata = self._dict_to_metadata(json.loads(metadata_json))
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

    def _metadata_to_dict(self, metadata: SessionMetadata) -> dict[str, Any]:
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
        }

    def _dict_to_metadata(self, data: dict[str, Any]) -> SessionMetadata:
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
        )

    def list_sessions(self) -> list[str]:
        """List all session names.

        Returns:
            Sorted list of session names
        """
        raise NotImplementedError("list_sessions not yet implemented")

    def delete_session(self, name: str) -> bool:
        """Delete a session.

        Args:
            name: Session identifier

        Returns:
            True if deleted, False if not found
        """
        raise NotImplementedError("delete_session not yet implemented")

    def get_session_metadata(self, name: str) -> SessionMetadata | None:
        """Get session metadata without loading the full session.

        Args:
            name: Session identifier

        Returns:
            SessionMetadata if session exists, None otherwise
        """
        raise NotImplementedError("get_session_metadata not yet implemented")

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
        raise NotImplementedError("update_session_metadata not yet implemented")
