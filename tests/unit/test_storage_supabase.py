"""Tests for Supabase storage backend.

Note: These tests mock the Supabase client to avoid requiring actual
Supabase credentials in unit tests.
"""

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


def _make_table_chain(mock_client):
    """Helper to build a mock table chain for Supabase builder pattern."""
    mock_table = MagicMock()
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.maybe_single.return_value = mock_table
    mock_table.delete.return_value = mock_table
    mock_table.update.return_value = mock_table
    mock_table.upsert.return_value = mock_table
    mock_client.table.return_value = mock_table
    return mock_table


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

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.return_value = MagicMock(data=[])

        sessions = storage.list_sessions()
        assert sessions == []

    @patch("supabase.create_client")
    def test_list_sessions_with_results(self, mock_create_client):
        """Test listing sessions with results."""
        storage, mock_client = _make_storage(mock_create_client)

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.return_value = MagicMock(
            data=[
                {"provider": "session-b"},
                {"provider": "session-a"},
            ]
        )

        sessions = storage.list_sessions()
        assert sessions == ["session-a", "session-b"]

    @patch("supabase.create_client")
    def test_list_sessions_none_result(self, mock_create_client):
        """Test listing sessions when result.data is None."""
        storage, mock_client = _make_storage(mock_create_client)

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.return_value = MagicMock(data=None)

        sessions = storage.list_sessions()
        assert sessions == []

    @patch("supabase.create_client")
    def test_list_sessions_api_error_returns_empty(self, mock_create_client):
        """Test that API errors in list_sessions return empty list."""
        from postgrest.exceptions import APIError

        storage, mock_client = _make_storage(mock_create_client)

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.side_effect = APIError({"message": "error"})

        sessions = storage.list_sessions()
        assert sessions == []

    @patch("supabase.create_client")
    def test_get_session_metadata_not_found(self, mock_create_client):
        """Test getting metadata for non-existent session."""
        storage, mock_client = _make_storage(mock_create_client)

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.return_value = MagicMock(data=None)

        metadata = storage.get_session_metadata("non-existent")
        assert metadata is None

    @patch("supabase.create_client")
    def test_get_session_metadata_success(self, mock_create_client):
        """Test getting metadata for existing session."""
        storage, mock_client = _make_storage(mock_create_client)
        now = datetime.now(UTC)

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.return_value = MagicMock(
            data={
                "provider": "my-session",
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
        )

        metadata = storage.get_session_metadata("my-session")
        assert metadata is not None
        assert metadata.name == "my-session"
        assert metadata.checksum == "sha256abc"
        assert metadata.domain == "example.com"
        assert metadata.cookie_count == 3

    @patch("supabase.create_client")
    def test_get_session_metadata_api_error_returns_none(self, mock_create_client):
        """Test that API errors in get_session_metadata return None."""
        from postgrest.exceptions import APIError

        storage, mock_client = _make_storage(mock_create_client)

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.side_effect = APIError({"message": "db error"})

        metadata = storage.get_session_metadata("test")
        assert metadata is None

    @patch("supabase.create_client")
    def test_delete_session(self, mock_create_client):
        """Test deleting a session."""
        storage, mock_client = _make_storage(mock_create_client)

        # Set up storage mock
        mock_storage_bucket = MagicMock()
        mock_client.storage.from_.return_value = mock_storage_bucket
        mock_storage_bucket.remove.return_value = None

        # Set up table mock
        mock_table = _make_table_chain(mock_client)
        mock_table.execute.return_value = MagicMock(data=[{"id": 1}])

        result = storage.delete_session("test-session")
        assert result is True

    @patch("supabase.create_client")
    def test_delete_session_storage_fails_db_succeeds(self, mock_create_client):
        """Test delete when storage removal fails but DB delete succeeds."""
        from httpx import HTTPStatusError, Request, Response

        storage, mock_client = _make_storage(mock_create_client)

        # Storage raises error
        mock_request = MagicMock(spec=Request)
        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 404
        mock_client.storage.from_.return_value.remove.side_effect = HTTPStatusError(
            "Not found", request=mock_request, response=mock_response
        )

        # DB delete succeeds
        mock_table = _make_table_chain(mock_client)
        mock_table.execute.return_value = MagicMock(data=[{"id": 1}])

        result = storage.delete_session("test-session")
        assert result is True

    @patch("supabase.create_client")
    def test_delete_session_both_fail(self, mock_create_client):
        """Test delete when both storage and DB deletion fail."""
        from httpx import HTTPStatusError, Request, Response
        from postgrest.exceptions import APIError

        storage, mock_client = _make_storage(mock_create_client)

        # Storage raises error
        mock_request = MagicMock(spec=Request)
        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 500
        mock_client.storage.from_.return_value.remove.side_effect = HTTPStatusError(
            "Server error", request=mock_request, response=mock_response
        )

        # DB also raises error
        mock_table = _make_table_chain(mock_client)
        mock_table.execute.side_effect = APIError({"message": "db error"})

        result = storage.delete_session("test-session")
        assert result is False

    @patch("supabase.create_client")
    def test_delete_session_db_no_rows_deleted(self, mock_create_client):
        """Test delete when DB returns no matching rows."""
        storage, mock_client = _make_storage(mock_create_client)

        mock_client.storage.from_.return_value.remove.return_value = None

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.return_value = MagicMock(data=[])

        # Storage succeeded, so still returns True
        result = storage.delete_session("test-session")
        assert result is True

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

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.return_value = MagicMock(data=[{"provider": "test"}])

        result = storage.update_session_metadata("test", status="logged_out")
        assert result is True

    @patch("supabase.create_client")
    def test_update_session_metadata_not_found(self, mock_create_client):
        """Test update returns False when session not found."""
        storage, mock_client = _make_storage(mock_create_client)

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.return_value = MagicMock(data=[])

        result = storage.update_session_metadata("nonexistent", status="active")
        assert result is False

    @patch("supabase.create_client")
    def test_update_session_metadata_api_error(self, mock_create_client):
        """Test update returns False on API error."""
        from postgrest.exceptions import APIError

        storage, mock_client = _make_storage(mock_create_client)

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.side_effect = APIError({"message": "db error"})

        result = storage.update_session_metadata("test", status="active")
        assert result is False


class TestRowToMetadata:
    """Tests for _row_to_metadata conversion."""

    @patch("supabase.create_client")
    def test_row_to_metadata_full_data(self, mock_create_client):
        """Test conversion of a complete database row to SessionMetadata."""
        storage, _mock_client = _make_storage(mock_create_client)
        now = datetime.now(UTC)

        row = {
            "provider": "my-session",
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

        metadata = storage._row_to_metadata(row)
        assert metadata.name == "my-session"
        assert metadata.checksum == "abc123"
        assert metadata.domain == "example.com"
        assert metadata.current_url == "https://example.com/page"
        assert metadata.cookie_count == 10
        assert metadata.cookie_domains == ["example.com", ".example.com"]
        assert metadata.status == "active"
        assert metadata.expires_at is not None

    @patch("supabase.create_client")
    def test_row_to_metadata_minimal_data(self, mock_create_client):
        """Test conversion with minimal/missing fields uses defaults."""
        storage, _mock_client = _make_storage(mock_create_client)

        row = {}

        metadata = storage._row_to_metadata(row)
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
    def test_row_to_metadata_with_z_suffix_datetime(self, mock_create_client):
        """Test conversion handles Supabase Z-suffix datetime strings."""
        storage, _mock_client = _make_storage(mock_create_client)

        row = {
            "provider": "test",
            "checksum": "x",
            "created_at": "2024-06-15T12:00:00Z",
            "modified_at": "2024-06-15T13:00:00Z",
            "expires_at": "2024-06-16T12:00:00Z",
        }

        metadata = storage._row_to_metadata(row)
        assert metadata.created_at.year == 2024
        assert metadata.created_at.month == 6
        assert metadata.expires_at is not None


class TestDoSave:
    """Tests for _do_save method."""

    @patch("supabase.create_client")
    def test_do_save_success(self, mock_create_client, sample_metadata):
        """Test successful save uploads data and upserts metadata."""
        storage, mock_client = _make_storage(mock_create_client)

        # Mock bucket creation (already exists, no-op)
        mock_client.storage.create_bucket.return_value = None

        # Mock storage upload
        mock_storage_bucket = MagicMock()
        mock_client.storage.from_.return_value = mock_storage_bucket
        mock_storage_bucket.upload.return_value = {"Key": "test-session/session.pickle"}

        # Mock metadata upsert
        mock_table = _make_table_chain(mock_client)
        mock_table.execute.return_value = MagicMock(data=[{"id": 1}])

        location = storage._do_save("test-session", b"encrypted-data", sample_metadata)

        assert location == "sessions/test-session/session.pickle"
        mock_storage_bucket.upload.assert_called_once()

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

        # Mock metadata upsert
        mock_table = _make_table_chain(mock_client)
        mock_table.execute.return_value = MagicMock(data=[{"id": 1}])

        location = storage._do_save("test-session", b"encrypted-data", sample_metadata)

        assert location == "sessions/test-session/session.pickle"
        mock_storage_bucket.update.assert_called_once()

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


class TestUpsertMetadata:
    """Tests for _upsert_metadata method."""

    @patch("supabase.create_client")
    def test_upsert_metadata_success(self, mock_create_client, sample_metadata):
        """Test successful metadata upsert."""
        storage, mock_client = _make_storage(mock_create_client)

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.return_value = MagicMock(data=[{"id": 1}])

        storage._upsert_metadata(sample_metadata, "test-session/session.pickle")

        mock_client.table.assert_called_with("session_cache")

    @patch("supabase.create_client")
    def test_upsert_metadata_api_error_raises_storage_error(
        self, mock_create_client, sample_metadata
    ):
        """Test that API errors in upsert raise StorageError."""
        from postgrest.exceptions import APIError

        storage, mock_client = _make_storage(mock_create_client)

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.side_effect = APIError({"message": "db error"})

        with pytest.raises(StorageError, match="Failed to save session metadata"):
            storage._upsert_metadata(sample_metadata, "test-session/session.pickle")

    @patch("supabase.create_client")
    def test_upsert_metadata_with_no_expires_at(self, mock_create_client):
        """Test upsert metadata when expires_at is None."""
        now = datetime.now(UTC)
        metadata = SessionMetadata(
            name="no-expiry",
            checksum="abc",
            created_at=now,
            modified_at=now,
            expires_at=None,
            domain=None,
            current_url=None,
            cookie_count=0,
            cookie_domains=[],
            status="active",
        )

        storage, mock_client = _make_storage(mock_create_client)

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.return_value = MagicMock(data=[{"id": 1}])

        storage._upsert_metadata(metadata, "no-expiry/session.pickle")

        # Verify the upsert call was made
        mock_client.table.assert_called_with("session_cache")


class TestLoadSession:
    """Tests for load_session method."""

    @patch("supabase.create_client")
    def test_load_session_success(self, mock_create_client):
        """Test successful session load."""
        storage, mock_client = _make_storage(mock_create_client)
        now = datetime.now(UTC)
        future = now + timedelta(hours=24)

        # Mock metadata query
        mock_table = _make_table_chain(mock_client)
        mock_table.execute.return_value = MagicMock(
            data={
                "provider": "test-session",
                "checksum": "abc123",
                "created_at": now.isoformat(),
                "modified_at": now.isoformat(),
                "expires_at": future.isoformat(),
                "domain": "example.com",
                "current_url": "https://example.com",
                "cookie_count": 5,
                "cookie_domains": ["example.com"],
                "status": "active",
                "storage_path": "test-session/session.pickle",
            }
        )

        # Mock storage download
        mock_client.storage.from_.return_value.download.return_value = b"encrypted-data"

        data, metadata = storage.load_session("test-session")

        assert data == b"encrypted-data"
        assert metadata.name == "test-session"
        assert metadata.domain == "example.com"

    @patch("supabase.create_client")
    def test_load_session_not_found_empty_result(self, mock_create_client):
        """Test load raises SessionNotFoundError when result.data is empty."""
        storage, mock_client = _make_storage(mock_create_client)

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.return_value = MagicMock(data=None)

        with pytest.raises(SessionNotFoundError, match="not found"):
            storage.load_session("nonexistent")

    @patch("supabase.create_client")
    def test_load_session_expired_ttl(self, mock_create_client):
        """Test load raises SessionExpiredError when TTL has passed."""
        storage, mock_client = _make_storage(mock_create_client)
        now = datetime.now(UTC)
        past = now - timedelta(hours=24)

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.return_value = MagicMock(
            data={
                "provider": "expired-session",
                "checksum": "abc",
                "created_at": (now - timedelta(hours=48)).isoformat(),
                "modified_at": (now - timedelta(hours=48)).isoformat(),
                "expires_at": past.isoformat(),
                "domain": "example.com",
                "current_url": None,
                "cookie_count": 0,
                "cookie_domains": [],
                "status": "active",
                "storage_path": "expired-session/session.pickle",
            }
        )

        with pytest.raises(SessionExpiredError, match="expired"):
            storage.load_session("expired-session")

    @patch("supabase.create_client")
    def test_load_session_missing_storage_path(self, mock_create_client):
        """Test load raises SessionExpiredError when storage_path is missing."""
        storage, mock_client = _make_storage(mock_create_client)
        now = datetime.now(UTC)

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.return_value = MagicMock(
            data={
                "provider": "test",
                "checksum": "abc",
                "created_at": now.isoformat(),
                "modified_at": now.isoformat(),
                "expires_at": None,
                "domain": None,
                "current_url": None,
                "cookie_count": 0,
                "cookie_domains": [],
                "status": "active",
                # storage_path is missing
            }
        )

        with pytest.raises(SessionExpiredError, match="missing storage path"):
            storage.load_session("test")

    @patch("supabase.create_client")
    def test_load_session_download_fails_raises_not_found(self, mock_create_client):
        """Test load raises SessionNotFoundError when download fails."""
        from httpx import HTTPStatusError, Request, Response

        storage, mock_client = _make_storage(mock_create_client)
        now = datetime.now(UTC)

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.return_value = MagicMock(
            data={
                "provider": "test",
                "checksum": "abc",
                "created_at": now.isoformat(),
                "modified_at": now.isoformat(),
                "expires_at": None,
                "domain": None,
                "current_url": None,
                "cookie_count": 0,
                "cookie_domains": [],
                "status": "active",
                "storage_path": "test/session.pickle",
            }
        )

        mock_request = MagicMock(spec=Request)
        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 404
        mock_client.storage.from_.return_value.download.side_effect = HTTPStatusError(
            "Not found", request=mock_request, response=mock_response
        )

        with pytest.raises(SessionNotFoundError, match="not found in storage"):
            storage.load_session("test")

    @patch("supabase.create_client")
    def test_load_session_api_error_raises_storage_error(self, mock_create_client):
        """Test load raises StorageError on postgrest APIError."""
        from postgrest.exceptions import APIError

        storage, mock_client = _make_storage(mock_create_client)

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.side_effect = APIError({"message": "db error"})

        with pytest.raises(StorageError, match="Failed to query"):
            storage.load_session("test")

    @patch("supabase.create_client")
    def test_load_session_invalid_expires_at_continues(self, mock_create_client):
        """Test load continues when expires_at is an invalid datetime string."""
        storage, mock_client = _make_storage(mock_create_client)
        now = datetime.now(UTC)

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.return_value = MagicMock(
            data={
                "provider": "test",
                "checksum": "abc",
                "created_at": now.isoformat(),
                "modified_at": now.isoformat(),
                "expires_at": "not-a-date",
                "domain": None,
                "current_url": None,
                "cookie_count": 0,
                "cookie_domains": [],
                "status": "active",
                "storage_path": "test/session.pickle",
            }
        )

        mock_client.storage.from_.return_value.download.return_value = b"encrypted-data"

        data, metadata = storage.load_session("test")
        assert data == b"encrypted-data"

    @patch("supabase.create_client")
    def test_load_session_z_suffix_expires_at(self, mock_create_client):
        """Test load handles Supabase Z-suffix datetime for expires_at."""
        storage, mock_client = _make_storage(mock_create_client)
        now = datetime.now(UTC)
        future = now + timedelta(hours=24)

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.return_value = MagicMock(
            data={
                "provider": "test",
                "checksum": "abc",
                "created_at": now.isoformat(),
                "modified_at": now.isoformat(),
                "expires_at": future.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "domain": None,
                "current_url": None,
                "cookie_count": 0,
                "cookie_domains": [],
                "status": "active",
                "storage_path": "test/session.pickle",
            }
        )

        mock_client.storage.from_.return_value.download.return_value = b"data"

        data, metadata = storage.load_session("test")
        assert data == b"data"


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


class TestSupabaseExceptionHandling:
    """Tests for Supabase exception handling."""

    @patch("supabase.create_client")
    def test_load_session_http_404_raises_session_not_found(self, mock_create_client):
        """Test that HTTP 404 raises SessionNotFoundError."""
        from httpx import HTTPStatusError, Request, Response

        storage, mock_client = _make_storage(mock_create_client)

        mock_request = MagicMock(spec=Request)
        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 404

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.side_effect = HTTPStatusError(
            "Not found", request=mock_request, response=mock_response
        )

        with pytest.raises(SessionNotFoundError, match="not found"):
            storage.load_session("non-existent")

    @patch("supabase.create_client")
    def test_load_session_http_500_raises_storage_error(self, mock_create_client):
        """Test that HTTP 500 raises StorageError."""
        from httpx import HTTPStatusError, Request, Response

        storage, mock_client = _make_storage(mock_create_client)

        mock_request = MagicMock(spec=Request)
        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 500

        mock_table = _make_table_chain(mock_client)
        mock_table.execute.side_effect = HTTPStatusError(
            "Server error", request=mock_request, response=mock_response
        )

        with pytest.raises(StorageError, match="Failed to query"):
            storage.load_session("test-session")


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
