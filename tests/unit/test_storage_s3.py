"""Tests for S3 storage backend."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from graftpunk.exceptions import SessionNotFoundError

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


class TestSaveSession:
    """Tests for save_session method."""

    def test_save_session_uploads_pickle_and_metadata(
        self, storage, mock_s3_client, sample_metadata
    ):
        """Test that save uploads both pickle and metadata files."""
        encrypted_data = b"encrypted-pickle-data"

        location = storage.save_session("test-session", encrypted_data, sample_metadata)

        assert location == "s3://test-bucket/sessions/test-session/session.pickle"
        assert mock_s3_client.put_object.call_count == 2

        # Verify pickle upload
        pickle_call = mock_s3_client.put_object.call_args_list[0]
        assert pickle_call.kwargs["Bucket"] == "test-bucket"
        assert pickle_call.kwargs["Key"] == "sessions/test-session/session.pickle"
        assert pickle_call.kwargs["Body"] == encrypted_data
        assert pickle_call.kwargs["ContentType"] == "application/octet-stream"

        # Verify metadata upload
        metadata_call = mock_s3_client.put_object.call_args_list[1]
        assert metadata_call.kwargs["Key"] == "sessions/test-session/metadata.json"
        assert metadata_call.kwargs["ContentType"] == "application/json"


class TestLoadSession:
    """Tests for load_session method."""

    def test_load_session_success(self, storage, mock_s3_client, sample_metadata):
        """Test successful session load."""
        metadata_dict = {
            "name": "test-session",
            "checksum": "abc123",
            "created_at": sample_metadata.created_at.isoformat(),
            "modified_at": sample_metadata.modified_at.isoformat(),
            "expires_at": sample_metadata.expires_at.isoformat(),
            "domain": "example.com",
            "current_url": "https://example.com/dashboard",
            "cookie_count": 5,
            "cookie_domains": ["example.com", ".example.com"],
            "status": "active",
        }
        mock_metadata_body = MagicMock()
        mock_metadata_body.read.return_value = json.dumps(metadata_dict).encode()

        mock_session_body = MagicMock()
        mock_session_body.read.return_value = b"encrypted-data"

        def get_object_side_effect(**kwargs):
            if kwargs["Key"].endswith("metadata.json"):
                return {"Body": mock_metadata_body}
            return {"Body": mock_session_body}

        mock_s3_client.get_object.side_effect = get_object_side_effect

        data, metadata = storage.load_session("test-session")

        assert data == b"encrypted-data"
        assert metadata.name == "test-session"
        assert metadata.domain == "example.com"

    def test_load_session_not_found(self, storage, mock_s3_client):
        """Test load raises SessionNotFoundError when metadata not found."""
        from botocore.exceptions import ClientError

        error_response = {
            "Error": {"Code": "NoSuchKey"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }
        mock_s3_client.get_object.side_effect = ClientError(error_response, "GetObject")

        with pytest.raises(SessionNotFoundError, match="not found"):
            storage.load_session("nonexistent")

    def test_load_session_expired_ttl(self, storage, mock_s3_client):
        """Test load raises SessionExpiredError when TTL has passed."""
        from graftpunk.exceptions import SessionExpiredError

        now = datetime.now(UTC)
        past = now - timedelta(hours=24)

        metadata_dict = {
            "name": "expired-session",
            "checksum": "abc",
            "created_at": (now - timedelta(hours=48)).isoformat(),
            "modified_at": (now - timedelta(hours=48)).isoformat(),
            "expires_at": past.isoformat(),
            "domain": "example.com",
            "current_url": None,
            "cookie_count": 0,
            "cookie_domains": [],
            "status": "active",
        }
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps(metadata_dict).encode()
        mock_s3_client.get_object.return_value = {"Body": mock_body}

        with pytest.raises(SessionExpiredError, match="expired"):
            storage.load_session("expired-session")
