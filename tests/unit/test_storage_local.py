"""Tests for local storage backend."""

from datetime import UTC, datetime, timedelta

import pytest

from bsc.exceptions import SessionExpiredError, SessionNotFoundError
from bsc.storage.base import SessionMetadata
from bsc.storage.local import LocalSessionStorage


class TestLocalSessionStorage:
    """Tests for LocalSessionStorage."""

    @pytest.fixture
    def storage(self, tmp_path):
        """Create a LocalSessionStorage instance."""
        return LocalSessionStorage(base_dir=tmp_path)

    @pytest.fixture
    def sample_metadata(self):
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

    def test_save_and_load_session(self, storage, sample_metadata):
        """Test saving and loading a session."""
        encrypted_data = b"encrypted session data"

        # Save
        location = storage.save_session(
            "test-session",
            encrypted_data,
            sample_metadata,
        )
        assert "test-session" in location

        # Load
        loaded_data, loaded_metadata = storage.load_session("test-session")
        assert loaded_data == encrypted_data
        assert loaded_metadata.name == sample_metadata.name
        assert loaded_metadata.checksum == sample_metadata.checksum
        assert loaded_metadata.domain == sample_metadata.domain

    def test_list_sessions(self, storage, sample_metadata):
        """Test listing sessions."""
        # Initially empty
        assert storage.list_sessions() == []

        # Save some sessions
        storage.save_session("session-a", b"data", sample_metadata)
        storage.save_session("session-b", b"data", sample_metadata)

        # List should return sorted names
        sessions = storage.list_sessions()
        assert sessions == ["session-a", "session-b"]

    def test_delete_session(self, storage, sample_metadata):
        """Test deleting a session."""
        storage.save_session("to-delete", b"data", sample_metadata)
        assert "to-delete" in storage.list_sessions()

        # Delete
        result = storage.delete_session("to-delete")
        assert result is True
        assert "to-delete" not in storage.list_sessions()

        # Delete non-existent
        result = storage.delete_session("non-existent")
        assert result is False

    def test_get_session_metadata(self, storage, sample_metadata):
        """Test getting session metadata without full load."""
        storage.save_session("test-session", b"data", sample_metadata)

        metadata = storage.get_session_metadata("test-session")
        assert metadata is not None
        assert metadata.name == "test-session"
        assert metadata.domain == sample_metadata.domain
        assert metadata.cookie_count == sample_metadata.cookie_count

        # Non-existent session
        assert storage.get_session_metadata("non-existent") is None

    def test_update_session_metadata(self, storage, sample_metadata):
        """Test updating session status."""
        storage.save_session("update-test", b"data", sample_metadata)

        # Update status
        result = storage.update_session_metadata("update-test", status="logged_out")
        assert result is True

        # Verify update
        metadata = storage.get_session_metadata("update-test")
        assert metadata is not None
        assert metadata.status == "logged_out"

    def test_update_invalid_status_raises_error(self, storage, sample_metadata):
        """Test that invalid status raises ValueError."""
        storage.save_session("invalid-status", b"data", sample_metadata)

        with pytest.raises(ValueError, match="Invalid status"):
            storage.update_session_metadata("invalid-status", status="invalid")

    def test_load_nonexistent_session_raises_error(self, storage):
        """Test that loading non-existent session raises SessionNotFoundError."""
        with pytest.raises(SessionNotFoundError):
            storage.load_session("non-existent")

    def test_load_expired_session_raises_error(self, storage):
        """Test that loading expired session raises SessionExpiredError."""
        now = datetime.now(UTC)
        expired_metadata = SessionMetadata(
            name="expired",
            checksum="abc",
            created_at=now - timedelta(hours=48),
            modified_at=now - timedelta(hours=48),
            expires_at=now - timedelta(hours=24),  # Expired
            domain="example.com",
            current_url=None,
            cookie_count=0,
            cookie_domains=[],
            status="active",
        )

        storage.save_session("expired", b"data", expired_metadata)

        with pytest.raises(SessionExpiredError):
            storage.load_session("expired")


class TestSessionMetadata:
    """Tests for SessionMetadata dataclass."""

    def test_metadata_is_frozen(self):
        """Test that SessionMetadata is immutable."""
        now = datetime.now(UTC)
        metadata = SessionMetadata(
            name="test",
            checksum="abc",
            created_at=now,
            modified_at=now,
            expires_at=None,
            domain="example.com",
            current_url=None,
            cookie_count=0,
            cookie_domains=[],
        )

        with pytest.raises(AttributeError):
            metadata.name = "changed"  # type: ignore

    def test_metadata_defaults(self):
        """Test SessionMetadata default values."""
        now = datetime.now(UTC)
        metadata = SessionMetadata(
            name="test",
            checksum="abc",
            created_at=now,
            modified_at=now,
            expires_at=None,
            domain=None,
            current_url=None,
            cookie_count=0,
            cookie_domains=[],
        )

        assert metadata.status == "active"
