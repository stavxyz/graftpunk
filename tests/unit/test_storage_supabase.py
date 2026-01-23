"""Tests for Supabase storage backend.

Note: These tests mock the Supabase client to avoid requiring actual
Supabase credentials in unit tests.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from graftpunk.exceptions import SessionNotFoundError, StorageError
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
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        from graftpunk.storage.supabase import SupabaseSessionStorage

        # Set up mock chain for empty result
        mock_table = MagicMock()
        mock_table.select.return_value = mock_table
        mock_table.execute.return_value = MagicMock(data=[])
        mock_client.table.return_value = mock_table

        storage = SupabaseSessionStorage(
            url="https://test.supabase.co",
            service_key="test-key",
        )

        sessions = storage.list_sessions()
        assert sessions == []

    @patch("supabase.create_client")
    def test_list_sessions_with_results(self, mock_create_client):
        """Test listing sessions with results."""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        from graftpunk.storage.supabase import SupabaseSessionStorage

        # Set up mock chain
        mock_table = MagicMock()
        mock_table.select.return_value = mock_table
        mock_table.execute.return_value = MagicMock(
            data=[
                {"provider": "session-a"},
                {"provider": "session-b"},
            ]
        )
        mock_client.table.return_value = mock_table

        storage = SupabaseSessionStorage(
            url="https://test.supabase.co",
            service_key="test-key",
        )

        sessions = storage.list_sessions()
        assert sessions == ["session-a", "session-b"]

    @patch("supabase.create_client")
    def test_get_session_metadata_not_found(self, mock_create_client):
        """Test getting metadata for non-existent session."""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        from graftpunk.storage.supabase import SupabaseSessionStorage

        # Set up mock chain for empty result
        mock_table = MagicMock()
        mock_table.select.return_value = mock_table
        mock_table.eq.return_value = mock_table
        mock_table.maybe_single.return_value = mock_table
        mock_table.execute.return_value = MagicMock(data=None)
        mock_client.table.return_value = mock_table

        storage = SupabaseSessionStorage(
            url="https://test.supabase.co",
            service_key="test-key",
        )

        metadata = storage.get_session_metadata("non-existent")
        assert metadata is None

    @patch("supabase.create_client")
    def test_delete_session(self, mock_create_client):
        """Test deleting a session."""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        from graftpunk.storage.supabase import SupabaseSessionStorage

        # Set up mock chain
        mock_storage = MagicMock()
        mock_storage.from_.return_value = mock_storage
        mock_storage.remove.return_value = None
        mock_client.storage = mock_storage

        mock_table = MagicMock()
        mock_table.delete.return_value = mock_table
        mock_table.eq.return_value = mock_table
        mock_table.execute.return_value = MagicMock(data=[{"id": 1}])
        mock_client.table.return_value = mock_table

        storage = SupabaseSessionStorage(
            url="https://test.supabase.co",
            service_key="test-key",
        )

        result = storage.delete_session("test-session")
        assert result is True

    @patch("supabase.create_client")
    def test_update_session_metadata_invalid_status(self, mock_create_client):
        """Test that invalid status raises ValueError."""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        from graftpunk.storage.supabase import SupabaseSessionStorage

        storage = SupabaseSessionStorage(
            url="https://test.supabase.co",
            service_key="test-key",
        )

        with pytest.raises(ValueError, match="Invalid status"):
            storage.update_session_metadata("test", status="invalid-status")


class TestSupabaseExceptionHandling:
    """Tests for Supabase exception handling."""

    @patch("supabase.create_client")
    def test_load_session_http_404_raises_session_not_found(self, mock_create_client):
        """Test that HTTP 404 raises SessionNotFoundError."""
        from httpx import HTTPStatusError, Request, Response

        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        from graftpunk.storage.supabase import SupabaseSessionStorage

        # Create a proper HTTPStatusError
        mock_request = MagicMock(spec=Request)
        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 404

        # Set up mock to raise HTTPStatusError with 404
        mock_table = MagicMock()
        mock_table.select.return_value = mock_table
        mock_table.eq.return_value = mock_table
        mock_table.maybe_single.return_value = mock_table
        mock_table.execute.side_effect = HTTPStatusError(
            "Not found", request=mock_request, response=mock_response
        )
        mock_client.table.return_value = mock_table

        storage = SupabaseSessionStorage(
            url="https://test.supabase.co",
            service_key="test-key",
        )

        with pytest.raises(SessionNotFoundError, match="not found"):
            storage.load_session("non-existent")

    @patch("supabase.create_client")
    def test_load_session_http_500_raises_storage_error(self, mock_create_client):
        """Test that HTTP 500 raises StorageError."""
        from httpx import HTTPStatusError, Request, Response

        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        from graftpunk.storage.supabase import SupabaseSessionStorage

        # Create a proper HTTPStatusError
        mock_request = MagicMock(spec=Request)
        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 500

        # Set up mock to raise HTTPStatusError with 500
        mock_table = MagicMock()
        mock_table.select.return_value = mock_table
        mock_table.eq.return_value = mock_table
        mock_table.maybe_single.return_value = mock_table
        mock_table.execute.side_effect = HTTPStatusError(
            "Server error", request=mock_request, response=mock_response
        )
        mock_client.table.return_value = mock_table

        storage = SupabaseSessionStorage(
            url="https://test.supabase.co",
            service_key="test-key",
        )

        with pytest.raises(StorageError, match="Failed to query"):
            storage.load_session("test-session")


class TestRetryLogic:
    """Tests for retry logic in Supabase storage."""

    @patch("supabase.create_client")
    def test_is_retryable_error_http_500(self, mock_create_client):
        """Test that HTTP 500 errors are retryable."""
        from httpx import HTTPStatusError, Request, Response

        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        from graftpunk.storage.supabase import SupabaseSessionStorage

        storage = SupabaseSessionStorage(
            url="https://test.supabase.co",
            service_key="test-key",
        )

        mock_request = MagicMock(spec=Request)
        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 500
        error = HTTPStatusError("Server error", request=mock_request, response=mock_response)

        assert storage._is_retryable_error(error) is True

    @patch("supabase.create_client")
    def test_is_retryable_error_http_429(self, mock_create_client):
        """Test that HTTP 429 (rate limit) errors are retryable."""
        from httpx import HTTPStatusError, Request, Response

        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        from graftpunk.storage.supabase import SupabaseSessionStorage

        storage = SupabaseSessionStorage(
            url="https://test.supabase.co",
            service_key="test-key",
        )

        mock_request = MagicMock(spec=Request)
        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 429
        error = HTTPStatusError("Rate limited", request=mock_request, response=mock_response)

        assert storage._is_retryable_error(error) is True

    @patch("supabase.create_client")
    def test_is_retryable_error_http_400(self, mock_create_client):
        """Test that HTTP 400 errors are not retryable."""
        from httpx import HTTPStatusError, Request, Response

        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        from graftpunk.storage.supabase import SupabaseSessionStorage

        storage = SupabaseSessionStorage(
            url="https://test.supabase.co",
            service_key="test-key",
        )

        mock_request = MagicMock(spec=Request)
        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 400
        error = HTTPStatusError("Bad request", request=mock_request, response=mock_response)

        assert storage._is_retryable_error(error) is False

    @patch("supabase.create_client")
    def test_is_retryable_error_connection_error(self, mock_create_client):
        """Test that connection errors are retryable."""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        from graftpunk.storage.supabase import SupabaseSessionStorage

        storage = SupabaseSessionStorage(
            url="https://test.supabase.co",
            service_key="test-key",
        )

        error = ConnectionError("Connection refused")
        assert storage._is_retryable_error(error) is True

    @patch("supabase.create_client")
    def test_is_retryable_error_timeout_error(self, mock_create_client):
        """Test that timeout errors are retryable."""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        from graftpunk.storage.supabase import SupabaseSessionStorage

        storage = SupabaseSessionStorage(
            url="https://test.supabase.co",
            service_key="test-key",
        )

        error = TimeoutError("Connection timed out")
        assert storage._is_retryable_error(error) is True
