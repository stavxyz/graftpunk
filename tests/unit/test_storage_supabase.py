"""Tests for Supabase storage backend.

Note: These tests mock the Supabase client to avoid requiring actual
Supabase credentials in unit tests.

The Supabase storage backend uses pure file-based storage:
- {session_name}/session.pickle - Encrypted session data
- {session_name}/metadata.json - Session metadata (JSON)
"""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

# Skip all tests in this module if supabase is not installed
pytest.importorskip("supabase")

from graftpunk.exceptions import SessionExpiredError, SessionNotFoundError, StorageError
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


def _make_storage(mock_create_client):
    """Helper to create a SupabaseSessionStorage with mocked client.

    Returns (storage, mock_client) tuple.
    """
    mock_client = MagicMock()
    mock_create_client.return_value = mock_client
    from graftpunk.storage.supabase import SupabaseSessionStorage

    storage = SupabaseSessionStorage(
        url="https://test.supabase.co",
        service_key="test-key",
        bucket_name="sessions",
    )
    return storage, mock_client


class TestSupabaseSessionStorage:
    """Tests for SupabaseSessionStorage (mocked)."""

    @patch("supabase.create_client")
    def test_storage_initialization(self, mock_create_client):
        """Test that storage initializes correctly."""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        from graftpunk.storage.supabase import SupabaseSessionStorage

        storage = SupabaseSessionStorage(
            url="https://test.supabase.co",
            service_key="test-key",
            bucket_name="test-bucket",
        )

        assert storage.bucket_name == "test-bucket"
        mock_create_client.assert_called_once()

    @patch("supabase.create_client")
    def test_list_sessions_empty(self, mock_create_client):
        """Test listing sessions when none exist."""
        storage, mock_client = _make_storage(mock_create_client)

        mock_client.storage.from_.return_value.list.return_value = []

        sessions = storage.list_sessions()
        assert sessions == []

    @patch("supabase.create_client")
    def test_list_sessions_with_results(self, mock_create_client):
        """Test listing sessions with results."""
        storage, mock_client = _make_storage(mock_create_client)

        mock_client.storage.from_.return_value.list.return_value = [
            {"name": "session-b"},
            {"name": "session-a"},
        ]

        sessions = storage.list_sessions()
        assert sessions == ["session-a", "session-b"]

    @patch("supabase.create_client")
    def test_list_sessions_none_result(self, mock_create_client):
        """Test listing sessions when result is None."""
        storage, mock_client = _make_storage(mock_create_client)

        mock_client.storage.from_.return_value.list.return_value = None

        sessions = storage.list_sessions()
        assert sessions == []

    @patch("supabase.create_client")
    def test_list_sessions_storage_error_returns_empty(self, mock_create_client):
        """Test that Storage errors in list_sessions return empty list."""
        from storage3.exceptions import StorageApiError

        storage, mock_client = _make_storage(mock_create_client)

        mock_client.storage.from_.return_value.list.side_effect = StorageApiError(
            "error", code="500", status=500
        )

        sessions = storage.list_sessions()
        assert sessions == []

    @patch("supabase.create_client")
    def test_list_sessions_filters_hidden_files(self, mock_create_client):
        """Test that hidden files/folders are filtered out."""
        storage, mock_client = _make_storage(mock_create_client)

        mock_client.storage.from_.return_value.list.return_value = [
            {"name": "session-a"},
            {"name": ".emptyFolderPlaceholder"},
            {"name": "session-b"},
        ]

        sessions = storage.list_sessions()
        assert sessions == ["session-a", "session-b"]

    @patch("supabase.create_client")
    def test_get_session_metadata_not_found(self, mock_create_client):
        """Test getting metadata for non-existent session."""
        from storage3.exceptions import StorageApiError

        storage, mock_client = _make_storage(mock_create_client)

        mock_client.storage.from_.return_value.download.side_effect = StorageApiError(
            "Not found", code="404", status=404
        )

        metadata = storage.get_session_metadata("non-existent")
        assert metadata is None

    @patch("supabase.create_client")
    def test_get_session_metadata_success(self, mock_create_client):
        """Test getting metadata for existing session."""
        storage, mock_client = _make_storage(mock_create_client)
        now = datetime.now(UTC)

        metadata_dict = {
            "name": "my-session",
            "checksum": "sha256abc",
            "created_at": now.isoformat(),
            "modified_at": now.isoformat(),
            "expires_at": None,
            "domain": "example.com",
            "current_url": "https://example.com",
            "cookie_count": 3,
            "cookie_domains": ["example.com"],
            "status": "active",
        }
        mock_client.storage.from_.return_value.download.return_value = json.dumps(
            metadata_dict
        ).encode("utf-8")

        metadata = storage.get_session_metadata("my-session")
        assert metadata is not None
        assert metadata.name == "my-session"
        assert metadata.checksum == "sha256abc"
        assert metadata.domain == "example.com"
        assert metadata.cookie_count == 3

    @patch("supabase.create_client")
    def test_get_session_metadata_http_error_returns_none(self, mock_create_client):
        """Test that HTTP errors in get_session_metadata return None."""
        from httpx import HTTPStatusError, Request, Response

        storage, mock_client = _make_storage(mock_create_client)

        mock_request = MagicMock(spec=Request)
        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 500
        mock_client.storage.from_.return_value.download.side_effect = HTTPStatusError(
            "Server error", request=mock_request, response=mock_response
        )

        metadata = storage.get_session_metadata("test")
        assert metadata is None

    @patch("supabase.create_client")
    def test_delete_session(self, mock_create_client):
        """Test deleting a session."""
        storage, mock_client = _make_storage(mock_create_client)

        mock_storage_bucket = MagicMock()
        mock_client.storage.from_.return_value = mock_storage_bucket
        mock_storage_bucket.remove.return_value = None

        result = storage.delete_session("test-session")
        assert result is True

        # Should have called remove for both session.pickle and metadata.json
        assert mock_storage_bucket.remove.call_count == 2

    @patch("supabase.create_client")
    def test_delete_session_one_file_fails(self, mock_create_client):
        """Test delete when one file removal fails but other succeeds."""
        from storage3.exceptions import StorageApiError

        storage, mock_client = _make_storage(mock_create_client)

        mock_storage_bucket = MagicMock()
        mock_client.storage.from_.return_value = mock_storage_bucket
        # First call succeeds, second fails
        mock_storage_bucket.remove.side_effect = [
            None,
            StorageApiError("Not found", code="404", status=404),
        ]

        result = storage.delete_session("test-session")
        # Still returns True because at least one file was deleted
        assert result is True

    @patch("supabase.create_client")
    def test_delete_session_both_files_fail(self, mock_create_client):
        """Test delete when both file removals fail."""
        from storage3.exceptions import StorageApiError

        storage, mock_client = _make_storage(mock_create_client)

        mock_storage_bucket = MagicMock()
        mock_client.storage.from_.return_value = mock_storage_bucket
        mock_storage_bucket.remove.side_effect = StorageApiError(
            "Not found", code="404", status=404
        )

        result = storage.delete_session("test-session")
        assert result is False

    @patch("supabase.create_client")
    def test_update_session_metadata_invalid_status(self, mock_create_client):
        """Test that invalid status raises ValueError."""
        storage, _mock_client = _make_storage(mock_create_client)

        with pytest.raises(ValueError, match="Invalid status"):
            storage.update_session_metadata("test", status="invalid-status")

    @patch("supabase.create_client")
    def test_update_session_metadata_success(self, mock_create_client):
        """Test successful metadata update."""
        storage, mock_client = _make_storage(mock_create_client)
        now = datetime.now(UTC)

        # Mock get_session_metadata (called internally)
        metadata_dict = {
            "name": "test",
            "checksum": "abc",
            "created_at": now.isoformat(),
            "modified_at": now.isoformat(),
            "expires_at": None,
            "domain": None,
            "current_url": None,
            "cookie_count": 0,
            "cookie_domains": [],
            "status": "active",
        }
        mock_storage_bucket = MagicMock()
        mock_client.storage.from_.return_value = mock_storage_bucket
        mock_storage_bucket.download.return_value = json.dumps(metadata_dict).encode("utf-8")
        mock_storage_bucket.upload.return_value = {"Key": "test/metadata.json"}

        result = storage.update_session_metadata("test", status="logged_out")
        assert result is True

    @patch("supabase.create_client")
    def test_update_session_metadata_not_found(self, mock_create_client):
        """Test update returns False when session not found."""
        from storage3.exceptions import StorageApiError

        storage, mock_client = _make_storage(mock_create_client)

        mock_client.storage.from_.return_value.download.side_effect = StorageApiError(
            "Not found", code="404", status=404
        )

        result = storage.update_session_metadata("nonexistent", status="active")
        assert result is False

    @patch("supabase.create_client")
    def test_update_session_metadata_upload_conflict_uses_update(self, mock_create_client):
        """Test that 409 conflict on upload falls back to update."""
        from storage3.exceptions import StorageApiError

        storage, mock_client = _make_storage(mock_create_client)
        now = datetime.now(UTC)

        metadata_dict = {
            "name": "test",
            "checksum": "abc",
            "created_at": now.isoformat(),
            "modified_at": now.isoformat(),
            "expires_at": None,
            "domain": None,
            "current_url": None,
            "cookie_count": 0,
            "cookie_domains": [],
            "status": "active",
        }
        mock_storage_bucket = MagicMock()
        mock_client.storage.from_.return_value = mock_storage_bucket
        mock_storage_bucket.download.return_value = json.dumps(metadata_dict).encode("utf-8")
        mock_storage_bucket.upload.side_effect = StorageApiError("Conflict", code="409", status=409)
        mock_storage_bucket.update.return_value = {"Key": "test/metadata.json"}

        result = storage.update_session_metadata("test", status="logged_out")
        assert result is True
        mock_storage_bucket.update.assert_called_once()


class TestMetadataConversion:
    """Tests for _metadata_to_dict and _dict_to_metadata conversion."""

    @patch("supabase.create_client")
    def test_dict_to_metadata_full_data(self, mock_create_client):
        """Test conversion of a complete dict to SessionMetadata."""
        storage, _mock_client = _make_storage(mock_create_client)
        now = datetime.now(UTC)

        data = {
            "name": "my-session",
            "checksum": "abc123",
            "created_at": now.isoformat(),
            "modified_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
            "domain": "example.com",
            "current_url": "https://example.com/page",
            "cookie_count": 10,
            "cookie_domains": ["example.com", ".example.com"],
            "status": "active",
        }

        metadata = storage._dict_to_metadata(data)
        assert metadata.name == "my-session"
        assert metadata.checksum == "abc123"
        assert metadata.domain == "example.com"
        assert metadata.current_url == "https://example.com/page"
        assert metadata.cookie_count == 10
        assert metadata.cookie_domains == ["example.com", ".example.com"]
        assert metadata.status == "active"
        assert metadata.expires_at is not None

    @patch("supabase.create_client")
    def test_dict_to_metadata_minimal_data(self, mock_create_client):
        """Test conversion with minimal/missing fields uses defaults."""
        storage, _mock_client = _make_storage(mock_create_client)

        data = {}

        metadata = storage._dict_to_metadata(data)
        assert metadata.name == ""
        assert metadata.checksum == ""
        assert metadata.domain is None
        assert metadata.current_url is None
        assert metadata.cookie_count == 0
        assert metadata.cookie_domains == []
        assert metadata.status == "active"
        assert metadata.expires_at is None
        # created_at and modified_at should default to now(UTC)
        assert metadata.created_at is not None
        assert metadata.modified_at is not None

    @patch("supabase.create_client")
    def test_dict_to_metadata_with_z_suffix_datetime(self, mock_create_client):
        """Test conversion handles Supabase Z-suffix datetime strings."""
        storage, _mock_client = _make_storage(mock_create_client)

        data = {
            "name": "test",
            "checksum": "x",
            "created_at": "2024-06-15T12:00:00Z",
            "modified_at": "2024-06-15T13:00:00Z",
            "expires_at": "2024-06-16T12:00:00Z",
        }

        metadata = storage._dict_to_metadata(data)
        assert metadata.created_at.year == 2024
        assert metadata.created_at.month == 6
        assert metadata.expires_at is not None

    @patch("supabase.create_client")
    def test_metadata_to_dict_roundtrip(self, mock_create_client, sample_metadata):
        """Test that metadata can be converted to dict and back."""
        storage, _mock_client = _make_storage(mock_create_client)

        # Convert to dict
        data = storage._metadata_to_dict(sample_metadata)

        # Convert back to metadata
        restored = storage._dict_to_metadata(data)

        assert restored.name == sample_metadata.name
        assert restored.checksum == sample_metadata.checksum
        assert restored.domain == sample_metadata.domain
        assert restored.cookie_count == sample_metadata.cookie_count
        assert restored.status == sample_metadata.status


class TestDoSave:
    """Tests for _do_save method."""

    @patch("supabase.create_client")
    def test_do_save_success(self, mock_create_client, sample_metadata):
        """Test successful save uploads both session data and metadata."""
        storage, mock_client = _make_storage(mock_create_client)

        # Mock bucket creation (already exists, no-op)
        mock_client.storage.create_bucket.return_value = None

        # Mock storage upload
        mock_storage_bucket = MagicMock()
        mock_client.storage.from_.return_value = mock_storage_bucket
        mock_storage_bucket.upload.return_value = {"Key": "test-session/session.pickle"}

        location = storage._do_save("test-session", b"encrypted-data", sample_metadata)

        assert location == "sessions/test-session/session.pickle"
        # Should have uploaded both session.pickle and metadata.json
        assert mock_storage_bucket.upload.call_count == 2

    @patch("supabase.create_client")
    def test_do_save_upload_conflict_uses_update(self, mock_create_client, sample_metadata):
        """Test that 409 conflict on upload falls back to update."""
        from storage3.exceptions import StorageApiError

        storage, mock_client = _make_storage(mock_create_client)

        # Mock bucket creation
        mock_client.storage.create_bucket.return_value = None

        # Mock storage upload with 409 conflict
        mock_storage_bucket = MagicMock()
        mock_client.storage.from_.return_value = mock_storage_bucket
        conflict_error = StorageApiError("Duplicate", code="409", status=409)
        mock_storage_bucket.upload.side_effect = conflict_error
        mock_storage_bucket.update.return_value = {"Key": "test-session/session.pickle"}

        location = storage._do_save("test-session", b"encrypted-data", sample_metadata)

        assert location == "sessions/test-session/session.pickle"
        # Should have called update for both files after upload failed
        assert mock_storage_bucket.update.call_count == 2

    @patch("supabase.create_client")
    def test_do_save_upload_non_409_error_raises(self, mock_create_client, sample_metadata):
        """Test that non-409 StorageApiError is re-raised."""
        from storage3.exceptions import StorageApiError

        storage, mock_client = _make_storage(mock_create_client)

        mock_client.storage.create_bucket.return_value = None

        mock_storage_bucket = MagicMock()
        mock_client.storage.from_.return_value = mock_storage_bucket
        server_error = StorageApiError("Server error", code="500", status=500)
        mock_storage_bucket.upload.side_effect = server_error

        with pytest.raises(StorageApiError):
            storage._do_save("test-session", b"encrypted-data", sample_metadata)


class TestEnsureBucketExists:
    """Tests for _ensure_bucket_exists method."""

    @patch("supabase.create_client")
    def test_ensure_bucket_creates_new_bucket(self, mock_create_client):
        """Test bucket creation when bucket does not exist."""
        storage, mock_client = _make_storage(mock_create_client)

        mock_client.storage.create_bucket.return_value = None

        storage._ensure_bucket_exists()
        mock_client.storage.create_bucket.assert_called_once_with(
            "sessions", options={"public": False}
        )

    @patch("supabase.create_client")
    def test_ensure_bucket_http_409_already_exists(self, mock_create_client):
        """Test that HTTP 409 is silently handled (bucket already exists)."""
        from httpx import HTTPStatusError, Request, Response

        storage, mock_client = _make_storage(mock_create_client)

        mock_request = MagicMock(spec=Request)
        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 409
        mock_client.storage.create_bucket.side_effect = HTTPStatusError(
            "Conflict", request=mock_request, response=mock_response
        )

        # Should not raise
        storage._ensure_bucket_exists()

    @patch("supabase.create_client")
    def test_ensure_bucket_http_non_409_raises(self, mock_create_client):
        """Test that non-409 HTTP errors are re-raised."""
        from httpx import HTTPStatusError, Request, Response

        storage, mock_client = _make_storage(mock_create_client)

        mock_request = MagicMock(spec=Request)
        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 500
        mock_client.storage.create_bucket.side_effect = HTTPStatusError(
            "Server error", request=mock_request, response=mock_response
        )

        with pytest.raises(HTTPStatusError):
            storage._ensure_bucket_exists()

    @patch("supabase.create_client")
    def test_ensure_bucket_storage_api_409_already_exists(self, mock_create_client):
        """Test that StorageApiError with 409 is silently handled."""
        from storage3.exceptions import StorageApiError

        storage, mock_client = _make_storage(mock_create_client)

        err = StorageApiError("Already exists", code="409", status=409)
        mock_client.storage.create_bucket.side_effect = err

        # Should not raise
        storage._ensure_bucket_exists()

    @patch("supabase.create_client")
    def test_ensure_bucket_storage_api_409_string_status(self, mock_create_client):
        """Test that StorageApiError with string '409' status is handled."""
        from storage3.exceptions import StorageApiError

        storage, mock_client = _make_storage(mock_create_client)

        err = StorageApiError("Already exists", code="409", status="409")
        mock_client.storage.create_bucket.side_effect = err

        # Should not raise
        storage._ensure_bucket_exists()

    @patch("supabase.create_client")
    def test_ensure_bucket_storage_api_non_409_raises(self, mock_create_client):
        """Test that non-409 StorageApiError is re-raised."""
        from storage3.exceptions import StorageApiError

        storage, mock_client = _make_storage(mock_create_client)

        err = StorageApiError("Forbidden", code="403", status=403)
        mock_client.storage.create_bucket.side_effect = err

        with pytest.raises(StorageApiError):
            storage._ensure_bucket_exists()


class TestLoadSession:
    """Tests for load_session method."""

    @patch("supabase.create_client")
    def test_load_session_success(self, mock_create_client):
        """Test successful session load."""
        storage, mock_client = _make_storage(mock_create_client)
        now = datetime.now(UTC)
        future = now + timedelta(hours=24)

        metadata_dict = {
            "name": "test-session",
            "checksum": "abc123",
            "created_at": now.isoformat(),
            "modified_at": now.isoformat(),
            "expires_at": future.isoformat(),
            "domain": "example.com",
            "current_url": "https://example.com",
            "cookie_count": 5,
            "cookie_domains": ["example.com"],
            "status": "active",
        }

        mock_storage_bucket = MagicMock()
        mock_client.storage.from_.return_value = mock_storage_bucket

        # First download is metadata, second is session data
        mock_storage_bucket.download.side_effect = [
            json.dumps(metadata_dict).encode("utf-8"),
            b"encrypted-data",
        ]

        data, metadata = storage.load_session("test-session")

        assert data == b"encrypted-data"
        assert metadata.name == "test-session"
        assert metadata.domain == "example.com"

    @patch("supabase.create_client")
    def test_load_session_not_found_metadata_missing(self, mock_create_client):
        """Test load raises SessionNotFoundError when metadata doesn't exist."""
        from storage3.exceptions import StorageApiError

        storage, mock_client = _make_storage(mock_create_client)

        mock_client.storage.from_.return_value.download.side_effect = StorageApiError(
            "Not found", code="404", status=404
        )

        with pytest.raises(SessionNotFoundError, match="not found"):
            storage.load_session("nonexistent")

    @patch("supabase.create_client")
    def test_load_session_expired_ttl(self, mock_create_client):
        """Test load raises SessionExpiredError when TTL has passed."""
        storage, mock_client = _make_storage(mock_create_client)
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

        mock_client.storage.from_.return_value.download.return_value = json.dumps(
            metadata_dict
        ).encode("utf-8")

        with pytest.raises(SessionExpiredError, match="expired"):
            storage.load_session("expired-session")

    @patch("supabase.create_client")
    def test_load_session_data_download_fails(self, mock_create_client):
        """Test load raises SessionNotFoundError when session data download fails."""
        from storage3.exceptions import StorageApiError

        storage, mock_client = _make_storage(mock_create_client)
        now = datetime.now(UTC)

        metadata_dict = {
            "name": "test",
            "checksum": "abc",
            "created_at": now.isoformat(),
            "modified_at": now.isoformat(),
            "expires_at": None,
            "domain": None,
            "current_url": None,
            "cookie_count": 0,
            "cookie_domains": [],
            "status": "active",
        }

        mock_storage_bucket = MagicMock()
        mock_client.storage.from_.return_value = mock_storage_bucket
        # Metadata download succeeds, session data download fails
        mock_storage_bucket.download.side_effect = [
            json.dumps(metadata_dict).encode("utf-8"),
            StorageApiError("Not found", code="404", status=404),
        ]

        with pytest.raises(SessionNotFoundError, match="not found in storage"):
            storage.load_session("test")

    @patch("supabase.create_client")
    def test_load_session_http_error_raises_not_found(self, mock_create_client):
        """Test load raises SessionNotFoundError on HTTP error."""
        from httpx import HTTPStatusError, Request, Response

        storage, mock_client = _make_storage(mock_create_client)

        mock_request = MagicMock(spec=Request)
        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 404
        mock_client.storage.from_.return_value.download.side_effect = HTTPStatusError(
            "Not found", request=mock_request, response=mock_response
        )

        with pytest.raises(SessionNotFoundError, match="not found"):
            storage.load_session("test")


class TestSaveSession:
    """Tests for save_session retry logic."""

    @patch("supabase.create_client")
    def test_save_session_non_retryable_error_raises_immediately(
        self, mock_create_client, sample_metadata
    ):
        """Test that non-retryable HTTP errors raise StorageError immediately."""
        from httpx import HTTPStatusError, Request, Response

        storage, mock_client = _make_storage(mock_create_client)
        storage.max_retries = 3

        # _do_save will be called, and it calls _ensure_bucket_exists first
        mock_client.storage.create_bucket.return_value = None

        mock_request = MagicMock(spec=Request)
        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 400

        mock_client.storage.from_.return_value.upload.side_effect = HTTPStatusError(
            "Bad request", request=mock_request, response=mock_response
        )

        with pytest.raises(StorageError, match="Session save failed"):
            storage.save_session("test", b"data", sample_metadata)

    @patch("graftpunk.storage.supabase.time.sleep")
    @patch("supabase.create_client")
    def test_save_session_retries_on_connection_error(
        self, mock_create_client, mock_sleep, sample_metadata
    ):
        """Test that ConnectionError triggers retries then raises StorageError."""
        storage, mock_client = _make_storage(mock_create_client)
        storage.max_retries = 2
        storage.base_delay = 0.01

        mock_client.storage.create_bucket.return_value = None
        mock_client.storage.from_.return_value.upload.side_effect = ConnectionError(
            "Connection refused"
        )

        with pytest.raises(StorageError, match="after 2 attempts"):
            storage.save_session("test", b"data", sample_metadata)

        # Should have slept between retries (max_retries - 1 sleeps)
        assert mock_sleep.call_count == 1


class TestRetryLogic:
    """Tests for retry logic in Supabase storage."""

    @patch("supabase.create_client")
    def test_is_retryable_error_http_500(self, mock_create_client):
        """Test that HTTP 500 errors are retryable."""
        from httpx import HTTPStatusError, Request, Response

        storage, _mock_client = _make_storage(mock_create_client)

        mock_request = MagicMock(spec=Request)
        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 500
        error = HTTPStatusError("Server error", request=mock_request, response=mock_response)

        assert storage._is_retryable_error(error) is True

    @patch("supabase.create_client")
    def test_is_retryable_error_http_429(self, mock_create_client):
        """Test that HTTP 429 (rate limit) errors are retryable."""
        from httpx import HTTPStatusError, Request, Response

        storage, _mock_client = _make_storage(mock_create_client)

        mock_request = MagicMock(spec=Request)
        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 429
        error = HTTPStatusError("Rate limited", request=mock_request, response=mock_response)

        assert storage._is_retryable_error(error) is True

    @patch("supabase.create_client")
    def test_is_retryable_error_http_400(self, mock_create_client):
        """Test that HTTP 400 errors are not retryable."""
        from httpx import HTTPStatusError, Request, Response

        storage, _mock_client = _make_storage(mock_create_client)

        mock_request = MagicMock(spec=Request)
        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 400
        error = HTTPStatusError("Bad request", request=mock_request, response=mock_response)

        assert storage._is_retryable_error(error) is False

    @patch("supabase.create_client")
    def test_is_retryable_error_connection_error(self, mock_create_client):
        """Test that connection errors are retryable."""
        storage, _mock_client = _make_storage(mock_create_client)

        error = ConnectionError("Connection refused")
        assert storage._is_retryable_error(error) is True

    @patch("supabase.create_client")
    def test_is_retryable_error_timeout_error(self, mock_create_client):
        """Test that timeout errors are retryable."""
        storage, _mock_client = _make_storage(mock_create_client)

        error = TimeoutError("Connection timed out")
        assert storage._is_retryable_error(error) is True

    @patch("supabase.create_client")
    def test_is_retryable_error_generic_os_error(self, mock_create_client):
        """Test that generic OSError is not retryable."""
        storage, _mock_client = _make_storage(mock_create_client)

        error = OSError("generic error")
        assert storage._is_retryable_error(error) is False
