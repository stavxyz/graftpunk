"""Tests for S3 storage backend."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("boto3")

from graftpunk.storage.base import SessionMetadata


@pytest.fixture
def sample_metadata():
    """Create sample session metadata."""
    now = datetime.now(UTC)
    return SessionMetadata(
        name="test-session",
        checksum="abc123",
        created_at=now,
        modified_at=now,
        expires_at=now + timedelta(hours=24),
        domain="example.com",
        current_url="https://example.com/dashboard",
        cookie_count=5,
        cookie_domains=["example.com", ".example.com"],
        status="active",
    )


@pytest.fixture
def mock_s3_client():
    """Create a mock boto3 S3 client."""
    return MagicMock()


@pytest.fixture
def storage(mock_s3_client):
    """Create S3SessionStorage with mocked client."""
    from graftpunk.storage.s3 import S3SessionStorage

    return S3SessionStorage(
        bucket="test-bucket",
        region="us-east-1",
        endpoint_url="https://test.r2.example.com",
        client=mock_s3_client,
    )


class TestS3SessionStorageInit:
    """Tests for S3SessionStorage initialization."""

    def test_storage_initialization_with_injected_client(self, mock_s3_client):
        """Test that storage initializes correctly with injected client."""
        from graftpunk.storage.s3 import S3SessionStorage

        storage = S3SessionStorage(
            bucket="test-bucket",
            endpoint_url="https://test.r2.example.com",
            client=mock_s3_client,
        )
        assert storage.bucket == "test-bucket"
        assert storage.endpoint_url == "https://test.r2.example.com"

    @patch("boto3.client")
    def test_storage_creates_client_when_not_injected(self, mock_boto_client):
        """Test that storage creates boto3 client when not injected."""
        from graftpunk.storage.s3 import S3SessionStorage

        mock_boto_client.return_value = MagicMock()
        storage = S3SessionStorage(
            bucket="test-bucket",
            region="us-west-2",
            endpoint_url="https://r2.example.com",
        )
        mock_boto_client.assert_called_once_with(
            "s3",
            region_name="us-west-2",
            endpoint_url="https://r2.example.com",
        )
        assert storage.bucket == "test-bucket"

    @patch("boto3.client")
    def test_storage_skips_region_when_auto(self, mock_boto_client):
        """Test that region='auto' is treated as no region (for R2)."""
        from graftpunk.storage.s3 import S3SessionStorage

        mock_boto_client.return_value = MagicMock()
        S3SessionStorage(
            bucket="test-bucket",
            region="auto",
            endpoint_url="https://r2.example.com",
        )
        mock_boto_client.assert_called_once_with(
            "s3",
            endpoint_url="https://r2.example.com",
        )
